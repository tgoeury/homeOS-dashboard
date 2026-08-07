"""
HomeOS — modules/data_cache.py
Cache persistant SQLite3 — source unique de vérité pour HomeOS.

Deux tables :
  cache   — dernière valeur connue par clé (key/value/unit/source/updated_at)
  history — séries temporelles (name/ts/source/value/unit) avec write-on-change

Clés cache :
  weather.snapshot           — données météo complètes (sérialisées)
  sensor.<pièce>.<mesure>   — capteurs (temperature, humidity, luminosity)
  network.devices            — liste des appareils LAN [{ip, name}, …]
  network.dns.blocked/rate/total/countries
  log.last.<name>            — dernière signature (source,value,unit,bucket) pour write-on-change
                               le bucket change toutes les LOG_HEARTBEAT_INTERVAL secondes,
                               forçant un enregistrement périodique même si la valeur est stable

Séries history (name) :
  sensor_<room>_temperature / humidity / luminosity
  plant_<id>_soil_moisture
  window_<id>_contact
  weather_temperature / humidity / wind_speed / pressure / sky
  weather_temperature_daily_mean — moyennes journalières extérieures
                                   (backfill OpenMeteo archive, cf.
                                   extended_dashboard/tools/fetch_weather_history.py)
  network_devices_count / dns_blocked / dns_rate
  enedis_daily
"""

import csv
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DB_PATH = Path(__file__).parent.parent / "data" / "cache.db"

# Ancienneté maximale autorisée par catégorie de clé (secondes)
LOG_HEARTBEAT_INTERVAL: float = 1800  # 30 min — force un point en DB même si la valeur est stable

STALE_THRESHOLDS: dict[str, float] = {
    "weather": 12 * 3600,   # 12 h
    "sensor":   1 * 3600,   #  1 h
    "network":  1 * 3600,   #  1 h
}


class DataCache:
    """
    Cache persistant basé sur SQLite3 — mémoire à court terme du système.

    Stocke des valeurs JSON horodatées sous des clés hiérarchiques
    (ex. "sensor.salon.temperature"). Fournit une méthode d'inspection
    de fraîcheur par catégorie pour alimenter les badges de statut.
    Thread-safe via un verrou interne.
    """

    def __init__(self, path: Path = _DB_PATH):
        self._path = path
        self._lock = threading.Lock()
        self._cx: sqlite3.Connection | None = None
        self._init_db()
        self._migrate_legacy_csvs()

    def _connect(self) -> sqlite3.Connection:
        # Connexion SQLite persistante, réutilisée pour tous les appels (round 4) :
        # ouvrir une connexion par requête est coûteux sur la microSD d'un RPi 3
        # (~24 ouvertures/5 s rien que pour update_sensors). Tous les accès passent
        # par self._lock, donc une connexion unique partagée est sûre ;
        # check_same_thread=False autorise son usage depuis le thread MQTT et Dash.
        # _connect() est toujours appelé sous self._lock → création paresseuse sûre.
        if self._cx is None:
            self._cx = sqlite3.connect(str(self._path), check_same_thread=False)
            # PRAGMA non fatals : journal_mode=WAL écrit dans l'en-tête du
            # fichier, donc échoue sur une base montée en lecture seule (les
            # dashboards peuvent la monter :ro). Le mode WAL est de toute façon
            # persistant — la base créée par data_broker l'est déjà. Sans ce
            # garde-fou, l'exception du PRAGMA faisait échouer TOUTES les
            # lectures suivantes de la connexion.
            for pragma in ("PRAGMA journal_mode=WAL", "PRAGMA synchronous=NORMAL"):
                try:
                    self._cx.execute(pragma)
                except sqlite3.Error as e:
                    logger.warning("DataCache: %s ignoré — %s", pragma, e)
        return self._cx

    def _init_db(self) -> None:
        """
        Crée le schéma si besoin. Tolérant à l'échec : les dashboards (main et
        extended) ne sont que des LECTEURS de la base créée par data_broker, et
        peuvent la monter en lecture seule (docker run -v …/data:/app/data:ro).
        Dans ce cas les CREATE … IF NOT EXISTS échouent, sans que ce soit une
        erreur : le schéma est déjà là. On journalise et on continue — planter à
        l'import rendrait le dashboard inutilisable sur un montage :ro.
        """
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("DataCache: création de %s impossible — %s", self._path.parent, e)
        try:
            self._create_schema()
        except Exception as e:
            logger.warning("DataCache: schéma non créé (base en lecture seule ?) — %s", e)

    def _create_schema(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key        TEXT PRIMARY KEY,
                    value      TEXT NOT NULL,
                    unit       TEXT DEFAULT '',
                    source     TEXT DEFAULT '',
                    updated_at REAL NOT NULL
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    name   TEXT NOT NULL,
                    ts     REAL NOT NULL,
                    source TEXT DEFAULT '',
                    value  TEXT NOT NULL,
                    unit   TEXT DEFAULT '',
                    PRIMARY KEY (name, ts)
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_name_ts ON history(name, ts)"
            )
            # Index COUVRANT (name, ts, value) : les lectures de graphes sont
            # toutes de la forme « valeurs d'une série sur une fenêtre de temps ».
            # Avec le seul index (name, ts), SQLite trouve les ts par l'index
            # puis va chercher `value` ligne par ligne dans la table — autant
            # d'accès aléatoires, ce qui domine le coût sur la microSD d'un
            # RPi 3. Le triplet permet de répondre depuis l'index seul.
            # Mesuré sur une base de 41 Mo, cache chaud : 206 ms → 140 ms pour
            # 79 000 points ; le gain est plus net à froid. Coût : ~25 % de
            # taille de base en plus, et un index à maintenir en écriture
            # (quelques centaines de lignes/minute côté data_broker).
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_history_name_ts_value "
                "ON history(name, ts, value)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS ytdlp_jobs (
                    id         TEXT PRIMARY KEY,
                    ts         TEXT NOT NULL,
                    url        TEXT NOT NULL,
                    params     TEXT NOT NULL DEFAULT '{}',
                    folder     TEXT NOT NULL DEFAULT '',
                    files      TEXT NOT NULL DEFAULT '[]',
                    status     TEXT NOT NULL DEFAULT 'running',
                    created_at REAL NOT NULL
                )
            """)

    @contextmanager
    def transaction(self):
        """
        Expose la connexion partagée, sous verrou, le temps d'une transaction.

        Destinée aux modules qui gèrent leurs propres tables dans la même base
        (timer_service et sa table timer_presets) : sans cela, chacun rouvrait sa
        propre connexion SQLite avec ses propres PRAGMA — code dupliqué, second
        lecteur WAL et un descripteur de fichier de plus, pour la même base.
        """
        with self._lock, self._connect() as conn:
            yield conn

    def write(self, key: str, value, unit: str = "", source: str = "") -> None:
        """Insère ou met à jour une entrée dans le cache."""
        payload = json.dumps(value, ensure_ascii=False)
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        """
                        INSERT INTO cache (key, value, unit, source, updated_at)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value      = excluded.value,
                            unit       = excluded.unit,
                            source     = excluded.source,
                            updated_at = excluded.updated_at
                        """,
                        (key, payload, unit, source, time.time()),
                    )
            except Exception as e:
                logger.error("DataCache.write(%s): %s", key, e)

    def read(self, key: str) -> dict | None:
        """
        Retourne {"value": …, "unit": …, "source": …, "updated_at": float}
        ou None si la clé est absente du cache.
        La valeur est désérialisée depuis JSON.
        """
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT value, unit, source, updated_at FROM cache WHERE key = ?",
                    (key,),
                ).fetchone()
            if row is None:
                return None
            return {
                "value":      json.loads(row[0]),
                "unit":       row[1],
                "source":     row[2],
                "updated_at": row[3],
            }
        except Exception as e:
            logger.error("DataCache.read(%s): %s", key, e)
            return None

    # ── Séries temporelles ────────────────────────────────────────────────────

    def log_raw(self, name: str, ts: float, value, unit: str, source: str) -> None:
        """Insère une ligne dans history à un timestamp précis, sans déduplication."""
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT OR IGNORE INTO history (name,ts,source,value,unit) VALUES (?,?,?,?,?)",
                        (name, ts, source, str(value), unit),
                    )
            except Exception as e:
                logger.error("DataCache.log_raw(%s): %s", name, e)

    def log_raw_many(self, rows: list[tuple]) -> None:
        """Insère plusieurs lignes history en une seule transaction. rows: [(name, ts, source, value, unit), ...]"""
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.executemany(
                        "INSERT OR REPLACE INTO history (name,ts,source,value,unit) VALUES (?,?,?,?,?)",
                        rows,
                    )
            except Exception as e:
                logger.error("DataCache.log_raw_many: %s", e)

    def update_history_ts(self, name: str, old_ts: float, new_ts: float) -> None:
        """Met à jour le timestamp d'une ligne history (fenêtre glissante capteurs)."""
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE OR IGNORE history SET ts=? WHERE name=? AND ts=?",
                        (new_ts, name, old_ts),
                    )
            except Exception as e:
                logger.error("DataCache.update_history_ts(%s): %s", name, e)

    def log(self, name: str, value, unit: str, source: str,
            force: bool = False) -> None:
        """
        Enregistre une entrée dans la table history avec write-on-change + heartbeat.

        N'écrit que si (source, value, unit, bucket) diffère de la dernière entrée connue,
        où bucket = int(now // LOG_HEARTBEAT_INTERVAL). Cela garantit un point en base
        toutes les 30 min même si la valeur ne change pas, rendant les graphes continus.
        La signature est mémorisée dans la table cache (clé log.last.<name>).
        force=True : bypass write-on-change, toujours écrire (à utiliser à la source MQTT).
        """
        now     = time.time()
        bucket  = int(now // LOG_HEARTBEAT_INTERVAL)
        sig_new = (str(source), str(value), str(unit), bucket)
        sig_key = f"log.last.{name}"
        with self._lock:
            try:
                with self._connect() as conn:
                    if not force:
                        row = conn.execute(
                            "SELECT value FROM cache WHERE key=?", (sig_key,)
                        ).fetchone()
                        if row and tuple(json.loads(row[0])) == sig_new:
                            has_data = conn.execute(
                                "SELECT 1 FROM history WHERE name=? LIMIT 1", (name,)
                            ).fetchone()
                            if has_data:
                                return
                    conn.execute(
                        "INSERT OR REPLACE INTO history (name,ts,source,value,unit) "
                        "VALUES (?,?,?,?,?)",
                        (name, now, source, str(value), unit),
                    )
                    conn.execute(
                        "INSERT INTO cache (key,value,unit,source,updated_at) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                        "unit=excluded.unit, source=excluded.source, updated_at=excluded.updated_at",
                        (sig_key, json.dumps(list(sig_new)), "", "datalogger", now),
                    )
            except Exception as e:
                logger.error("DataCache.log(%s): %s", name, e)

    def read_history(
        self,
        name: str,
        since_ts: float | None = None,
        limit: int | None = None,
        until_ts: float | None = None,
    ) -> list[dict]:
        """
        Retourne [{ts, source, value, unit}, …] trié ASC pour la série <name>.
        since_ts : borne basse INCLUSE (ts >= since_ts).
        until_ts : borne haute EXCLUE (ts < until_ts) — filtrer en SQL plutôt qu'en
                   Python évite de désérialiser des lignes qu'on jette ensuite.
        limit    : nombre maximum de lignes retournées — les PLUS RÉCENTES, le
                   résultat restant trié ASC.
        """
        try:
            where  = "name=?"
            params: list = [name]
            if since_ts is not None:
                where += " AND ts >= ?"
                params.append(since_ts)
            if until_ts is not None:
                where += " AND ts < ?"
                params.append(until_ts)
            cols = "ts, source, value, unit"
            if limit is None:
                query = f"SELECT {cols} FROM history WHERE {where} ORDER BY ts ASC"
            else:
                # Les N plus récentes : on trie DESC pour la limite, puis on
                # réordonne ASC — un LIMIT sur un ORDER BY ASC renverrait les
                # plus ANCIENNES.
                query = (f"SELECT * FROM (SELECT {cols} FROM history WHERE {where} "
                         f"ORDER BY ts DESC LIMIT {int(limit)}) ORDER BY ts ASC")
            with self._lock, self._connect() as conn:
                rows = conn.execute(query, params).fetchall()
            return [{"ts": r[0], "source": r[1], "value": r[2], "unit": r[3]}
                    for r in rows]
        except Exception as e:
            logger.error("DataCache.read_history(%s): %s", name, e)
            return []

    # Les séries numériques sont stockées en TEXT (la colonne value accueille
    # aussi des libellés : weather_sky, window_*_contact…). Ce garde-fou écarte
    # les valeurs non numériques AVANT le CAST, sans quoi SQLite convertirait
    # "True" en 0.0 et polluerait les courbes.
    _NUMERIC_GUARD = "(value GLOB '[0-9]*' OR value GLOB '-[0-9]*')"

    def read_series(
        self,
        name: str,
        since_ts: float | None = None,
        until_ts: float | None = None,
    ) -> list[tuple[float, float]]:
        """
        Retourne [(ts, valeur_float), …] trié ASC — variante numérique et
        allégée de read_history() pour les graphes.

        Trois économies par rapport à read_history() + conversion en Python,
        déterminantes sur un RPi 3 où une série de capteur dépasse 100 000
        lignes : deux colonnes au lieu de quatre, le CAST fait par SQLite (en C)
        au lieu d'un float() par ligne, et aucun dict intermédiaire.
        """
        try:
            query  = f"SELECT ts, CAST(value AS REAL) FROM history WHERE name=? AND {self._NUMERIC_GUARD}"
            params: list = [name]
            if since_ts is not None:
                query += " AND ts >= ?"
                params.append(since_ts)
            if until_ts is not None:
                query += " AND ts < ?"
                params.append(until_ts)
            query += " ORDER BY ts ASC"
            with self._lock, self._connect() as conn:
                return conn.execute(query, params).fetchall()
        except Exception as e:
            logger.error("DataCache.read_series(%s): %s", name, e)
            return []

    def read_history_latest(self, name: str) -> dict | None:
        """
        Dernière ligne de la série <name>, ou None si la série est vide.

        À préférer systématiquement à read_history(name)[-1] : l'index
        (name, ts) rend la requête O(1), là où lire toute la série pour n'en
        garder que le dernier point coûtait ~300 ms sur une série de 100 000
        lignes (et bien davantage sur microSD).
        """
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT ts, source, value, unit FROM history WHERE name=? "
                    "ORDER BY ts DESC LIMIT 1", (name,),
                ).fetchone()
            if row is None:
                return None
            return {"ts": row[0], "source": row[1], "value": row[2], "unit": row[3]}
        except Exception as e:
            logger.error("DataCache.read_history_latest(%s): %s", name, e)
            return None

    def series_extremes(self, name: str, since_ts: float | None = None) -> dict | None:
        """
        Extrema numériques de la série <name> :
        {"min", "min_ts", "max", "max_ts"} ou None si aucune valeur numérique.

        Calculé par SQLite : MIN()/MAX() sont des agrégats, et la forme
        `MAX(CAST(value AS REAL)), ts` exploite l'extension bare-column de
        SQLite, qui renvoie le ts de la ligne où l'extremum a été atteint.
        Évite de transférer toute la série pour en tirer deux points.
        """
        try:
            where  = f"name=? AND {self._NUMERIC_GUARD}"
            params: list = [name]
            if since_ts is not None:
                where += " AND ts >= ?"
                params.append(since_ts)
            with self._lock, self._connect() as conn:
                lo = conn.execute(
                    f"SELECT MIN(CAST(value AS REAL)), ts FROM history WHERE {where}",
                    params).fetchone()
                hi = conn.execute(
                    f"SELECT MAX(CAST(value AS REAL)), ts FROM history WHERE {where}",
                    params).fetchone()
            if not lo or lo[0] is None:
                return None
            return {"min": lo[0], "min_ts": lo[1], "max": hi[0], "max_ts": hi[1]}
        except Exception as e:
            logger.error("DataCache.series_extremes(%s): %s", name, e)
            return None

    # ── Jobs yt-dlp ───────────────────────────────────────────────────────────

    def log_ytdlp_job(self, job_id: str, ts: str, url: str,
                      params: dict, folder: str) -> None:
        """Enregistre un nouveau job yt-dlp en base."""
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO ytdlp_jobs "
                        "(id, ts, url, params, folder, status, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'running', ?)",
                        (job_id, ts, url, json.dumps(params), folder, time.time()),
                    )
            except Exception as e:
                logger.error("DataCache.log_ytdlp_job: %s", e)

    def update_ytdlp_job(self, job_id: str, status: str,
                         files: list | None = None) -> None:
        """Met à jour le statut et la liste de fichiers d'un job yt-dlp."""
        with self._lock:
            try:
                with self._connect() as conn:
                    conn.execute(
                        "UPDATE ytdlp_jobs SET status=?, files=? WHERE id=?",
                        (status, json.dumps(files or []), job_id),
                    )
            except Exception as e:
                logger.error("DataCache.update_ytdlp_job: %s", e)

    # ── Migration CSV → SQLite (one-shot au premier démarrage) ────────────────

    def _migrate_legacy_csvs(self) -> None:
        """
        Importe tous les CSV de data/history/ dans la table history,
        puis renomme les fichiers en .csv.migrated pour ne pas les re-traiter.

        Format CSV attendu : timestamp,source,value,unit  (ISO 8601 → ts Unix)
        Exception : enedis_daily_consumption.csv  → timestamp converti en midi local.
        """
        history_dir = self._path.parent.parent / "data" / "history"
        if not history_dir.exists():
            return

        csv_files = sorted(p for p in history_dir.glob("*.csv")
                           if not p.name.endswith(".migrated"))
        if not csv_files:
            return

        total = 0
        with self._lock, self._connect() as conn:
            for path in csv_files:
                try:
                    with open(path, newline="", encoding="utf-8") as f:
                        rows = list(csv.DictReader(f))
                except Exception as e:
                    logger.error("DataCache._migrate_legacy_csvs: lecture %s — %s", path.name, e)
                    continue

                # Nom de série : stem du fichier, sauf pour Enedis
                if path.stem == "enedis_daily_consumption":
                    series_name = "enedis_daily"
                else:
                    series_name = path.stem

                imported = 0
                for row in rows:
                    try:
                        raw_ts = row.get("timestamp", "")
                        if series_name == "enedis_daily":
                            d  = date.fromisoformat(raw_ts[:10])
                            ts = datetime(d.year, d.month, d.day, 12, 0).timestamp()
                        else:
                            ts = datetime.fromisoformat(raw_ts).timestamp()
                        conn.execute(
                            "INSERT OR IGNORE INTO history (name,ts,source,value,unit) "
                            "VALUES (?,?,?,?,?)",
                            (series_name, ts,
                             row.get("source", ""), row.get("value", ""), row.get("unit", "")),
                        )
                        imported += 1
                    except Exception:
                        continue

                path.rename(path.with_suffix(".csv.migrated"))
                total += imported
                logger.info("DataCache: %s — %d lignes importées", series_name, imported)

        logger.info("DataCache: migration terminée — %d lignes au total", total)

    # ── Purge de l'historique ─────────────────────────────────────────────────

    def purge_history(self, keep_days: int = 90,
                      keep_forever_prefixes: tuple = ("enedis", "weather_temperature_daily_mean")) -> int:
        """
        Supprime les lignes history plus vieilles que `keep_days`, sauf les séries
        dont le nom commence par un des préfixes de `keep_forever_prefixes` :
          enedis_daily                   — ~3 ans de données utiles, volume négligeable
          weather_temperature_daily_mean — backfill OpenMeteo archive mis en regard
                                           de la conso Enedis (1 point/jour, et le
                                           re-télécharger coûte des appels API)
        Retourne le nombre de lignes supprimées.
        """
        cutoff = time.time() - keep_days * 86400
        like_excl = " AND ".join(f"name NOT LIKE '{p}%'" for p in keep_forever_prefixes)
        try:
            with self._lock:
                with self._connect() as conn:
                    cur = conn.execute(
                        f"DELETE FROM history WHERE ts < ? AND {like_excl}", (cutoff,)
                    )
                return cur.rowcount
        except Exception as e:
            logger.error("DataCache.purge_history: %s", e)
            return 0

    # ── Fraîcheur du cache ────────────────────────────────────────────────────

    def is_reachable(self) -> bool:
        """True si la base répond. Un simple SELECT 1 — ne lit aucune table.

        À préférer à `get_stats()` pour un test de vie : get_stats() fait un
        COUNT(*) et un GROUP BY sur toute la table history (231 000 lignes en
        exploitation courante), soit ~46 ms par appel sur SSD et bien davantage
        sur la microSD d'un RPi 3.
        """
        try:
            with self._lock, self._connect() as conn:
                conn.execute("SELECT 1").fetchone()
            return True
        except Exception:
            return False

    def newest_cache_ts(self, prefixes: tuple[str, ...]) -> float | None:
        """
        Horodatage le plus récent parmi les clés cache commençant par un des
        `prefixes` (ex. ("sensor.", "plant.")), ou None si aucune ne correspond.

        La table cache tient une ligne par clé (~80 en exploitation) : la lire
        intégralement coûte ~0,05 ms, contre ~92 ms pour chercher le MAX(ts) des
        séries history équivalentes. Les deux tables étant écrites dans la MÊME
        transaction par data_broker (cf. record_measurements), la réponse est
        identique.
        """
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("SELECT key, updated_at FROM cache").fetchall()
        except Exception as e:
            logger.error("DataCache.newest_cache_ts: %s", e)
            return None
        tss = [ts for key, ts in rows if key.startswith(prefixes)]
        return max(tss) if tss else None

    def newest_data_ts(self) -> float | None:
        """
        Horodatage de la donnée la plus récente écrite en base, toutes sources
        confondues (MAX sur la table cache), ou None si la base est vide.

        Sert l'indicateur de fraîcheur globale "database" (topbar + page
        Système). Comme newest_cache_ts, s'appuie sur `cache` (~80 lignes) et
        non sur `history` : data_broker écrit les deux dans la même transaction.
        """
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute("SELECT MAX(updated_at) FROM cache").fetchone()
            return row[0] if row and row[0] is not None else None
        except Exception as e:
            logger.error("DataCache.newest_data_ts: %s", e)
            return None

    def newest_history_ts(self, name: str) -> float | None:
        """
        Horodatage de la dernière ligne de la série `name`, ou None si vide.
        Requête indexée en O(log n) — à préférer à un MAX(ts) sur un préfixe de
        nom, qui dégénère en parcours d'index complet.
        """
        try:
            with self._lock, self._connect() as conn:
                row = conn.execute(
                    "SELECT ts FROM history WHERE name=? ORDER BY ts DESC LIMIT 1",
                    (name,),
                ).fetchone()
            return row[0] if row else None
        except Exception as e:
            logger.error("DataCache.newest_history_ts(%s): %s", name, e)
            return None

    def get_stale_categories(self) -> dict[str, float]:
        """
        Retourne {catégorie: âge_en_secondes} pour les catégories dont
        la clé LA PLUS RÉCENTE dépasse son seuil de fraîcheur configuré.
        Les clés orphelines (anciennes rooms renommées, etc.) sont ignorées.
        Exemple : {"weather": 50400.0, "sensor": 4200.0}
        """
        now = time.time()
        cat_latest: dict[str, float] = {}   # catégorie → timestamp le plus récent
        try:
            with self._lock, self._connect() as conn:
                rows = conn.execute("SELECT key, updated_at FROM cache").fetchall()
            for key, updated_at in rows:
                cat = key.split(".")[0]
                if cat in STALE_THRESHOLDS:
                    cat_latest[cat] = max(cat_latest.get(cat, 0.0), updated_at)
        except Exception as e:
            logger.error("DataCache.get_stale_categories: %s", e)
            return {}

        return {
            cat: now - latest
            for cat, latest in cat_latest.items()
            if (now - latest) > STALE_THRESHOLDS[cat]
        }

    # ── Statistiques (mini-dashboard data_broker) ─────────────────────────────

    def get_stats(self) -> dict:
        """
        Retourne des statistiques d'introspection sur la DB, pour le
        mini-dashboard de supervision du data_broker :
          {
            "db_size_bytes": int,
            "cache_rows": int,
            "history_rows": int,
            "series": [{"name", "rows", "oldest_ts", "newest_ts"}, …],  # tri par newest_ts desc
          }
        N'écrit rien — lecture seule, sûre à appeler à haute fréquence.
        """
        try:
            db_size = self._path.stat().st_size if self._path.exists() else 0
        except OSError:
            db_size = 0
        try:
            with self._lock, self._connect() as conn:
                cache_rows = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
                history_rows = conn.execute("SELECT COUNT(*) FROM history").fetchone()[0]
                series_rows = conn.execute("""
                    SELECT name, COUNT(*), MIN(ts), MAX(ts)
                    FROM history GROUP BY name ORDER BY MAX(ts) DESC
                """).fetchall()
            series = [
                {"name": name, "rows": rows, "oldest_ts": oldest, "newest_ts": newest}
                for name, rows, oldest, newest in series_rows
            ]
            return {
                "db_size_bytes": db_size,
                "cache_rows": cache_rows,
                "history_rows": history_rows,
                "series": series,
            }
        except Exception as e:
            logger.error("DataCache.get_stats: %s", e)
            return {"db_size_bytes": db_size, "cache_rows": 0, "history_rows": 0, "series": []}


data_cache = DataCache()
