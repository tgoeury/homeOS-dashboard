"""
HomeOS — modules/comfort_engine.py
═══════════════════════════════════════════════════════════════════════════════
Moteur de confort : planification volets/fenêtres sur 24 h via le modèle
thermique prédictif (GRU multi-résolution).

Code extrait et intégré depuis modules/home_model/ (projet autonome supprimé) :
  · pipeline de données (météo OpenMeteo + features solaires)
  · stratégie de planification (recherche aléatoire + rollout + coût de confort)

Données météo : OpenMeteo hourly (past_days=2, forecast_days=2), mises en cache
dans data_cache sous la clé 'weather.inference' (TTL 10 min).
Modèles ONNX : ./models/limited.onnx  (et full.onnx pour model_status())
Les métadonnées (colonnes, stats de normalisation, hyperparamètres) sont
embarquées directement dans les fichiers ONNX (champ 'home_model_meta').

Note sur le modèle « limited » : il prédit les températures intérieures sans
prendre les capteurs intérieurs en entrée — seulement météo + solaire + état
volets/fenêtres. Les colonnes indoor__* n'apparaissent donc pas dans la table
de features d'inférence.

API publique : model_status(), run_inference(), RoomPlan
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import importlib
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

import requests


class _LazyModule:
    """Proxy d'import paresseux : le module sous-jacent n'est réellement importé
    qu'au premier accès attribut. numpy + onnxruntime + pandas pèsent ~65 MB de RSS
    et ne servent QUE pendant l'inférence confort (déclenchée à la demande par
    l'utilisateur, jamais dans le steady-state). Sur RPi 3 (1 Go RAM), les charger
    en permanence au démarrage est du gaspillage. Tous les usages `np.`/`pd.`/`ort.`
    restent inchangés ; les annotations `pd.DataFrame` sont des chaînes (cf.
    `from __future__ import annotations`) donc jamais évaluées à l'import."""

    def __init__(self, name: str):
        object.__setattr__(self, "_lazy_name", name)
        object.__setattr__(self, "_lazy_mod", None)

    def __getattr__(self, attr):
        mod = object.__getattribute__(self, "_lazy_mod")
        if mod is None:
            mod = importlib.import_module(object.__getattribute__(self, "_lazy_name"))
            object.__setattr__(self, "_lazy_mod", mod)
        return getattr(mod, attr)


np  = _LazyModule("numpy")
ort = _LazyModule("onnxruntime")
pd  = _LazyModule("pandas")

from modules.data_cache import data_cache

logger = logging.getLogger(__name__)

# ── Chemins ───────────────────────────────────────────────────────────────────

_ROOT      = Path(__file__).parent.parent
MODELS_DIR = _ROOT / "models"

# ── Config HomeOS (géolocalisation + pièces) ─────────────────────────────────

try:
    import config as _hcfg
    _LATITUDE      = _hcfg.GEO_LATITUDE
    _LONGITUDE     = _hcfg.GEO_LONGITUDE
    _DEFAULT_ROOMS = [room_id for room_id, *_ in _hcfg.ROOMS]
    _N_CANDIDATES  = getattr(_hcfg, "COMFORT_N_CANDIDATES", 500)
except Exception:
    _LATITUDE      = 45.55
    _LONGITUDE     = 6.22
    _DEFAULT_ROOMS = ["salon", "chambre1", "chambre2", "bureau"]
    _N_CANDIDATES  = 500

# ── Constantes (extraites de home_model/config.py) ───────────────────────────

SEP = "__"
SAMPLE_INTERVAL_MINUTES  = 2
PREDICTION_HORIZON_STEPS = 1

RESOLUTION_SEGMENTS: list[dict] = [
    {"duration_minutes": 120, "resolution_minutes": 2},
    {"duration_minutes": 240, "resolution_minutes": 10},
    {"duration_minutes": 720, "resolution_minutes": 30},
]
HISTORY_HOURS = sum(s["duration_minutes"] for s in RESOLUTION_SEGMENTS) / 60.0

HOUSE_STATE_TYPES = ["shutter", "window"]
HOUSE_FACES       = {"N": 0.0, "E": 90.0, "S": 180.0, "W": 270.0}

COMFORT_TEMP_MIN           = 19.0
COMFORT_TEMP_MAX           = 26.0
PLANNING_HORIZON_HOURS     = 24.0
PLANNING_BLOCK_HOURS       = 2.0
PLANNING_EVAL_STEP_MINUTES = 30.0
PLANNING_N_CANDIDATES      = _N_CANDIDATES
RANDOM_SEED                = 42

_OPENMETEO_URL          = "https://api.open-meteo.com/v1/forecast"
_WEATHER_INFERENCE_TTL  = 600   # 10 min entre deux appels OpenMeteo pour l'inférence

# ── Statuts publics ───────────────────────────────────────────────────────────

STATUS_FULL    = "full"
STATUS_LIMITED = "limited"
STATUS_NONE    = "none"

_REASON_LABELS = {
    "refroidir": "REFROIDIR",
    "rechauffer": "CHAUFFER",
    "maintenir":  "MAINTENIR",
}


# ═══════════════════════════════════════════════════════════════════════════════
# DATACLASS PUBLIC
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class RoomPlan:
    room_id:   str
    room_name: str
    actions:   list[str]
    until:     str


# ═══════════════════════════════════════════════════════════════════════════════
# API PUBLIQUE
# ═══════════════════════════════════════════════════════════════════════════════

def model_status() -> str:
    """'full', 'limited' ou 'none' selon les modèles ONNX présents dans ./models/."""
    if (MODELS_DIR / "full.onnx").exists():
        return STATUS_FULL
    if (MODELS_DIR / "limited.onnx").exists():
        return STATUS_LIMITED
    return STATUS_NONE


_INFERENCE_CACHE: dict[tuple, tuple[float, dict]] = {}


def run_inference(comfort_ranges: dict[str, tuple[float, float]]) -> dict:
    """Lance l'inférence directement en mémoire (plus de subprocess).

    Mémoïsé par plages de confort, TTL aligné sur _WEATHER_INFERENCE_TTL (10 min) :
    la météo d'inférence est elle-même cachée sur cette durée, donc deux clics
    rapprochés avec les mêmes sliders referaient un calcul strictement identique.

    Retourne :
      {"status": "ok"|"error", "error": str|None, "rooms": [RoomPlan, ...]}
    """
    if model_status() == STATUS_NONE:
        return {"status": "error", "error": "Aucun modèle prédictif disponible.", "rooms": []}

    key = tuple(sorted(comfort_ranges.items())) if comfort_ranges else ()
    cached = _INFERENCE_CACHE.get(key)
    if cached is not None and (time.time() - cached[0]) < _WEATHER_INFERENCE_TTL:
        return cached[1]

    try:
        result   = _plan(comfort_ranges=comfort_ranges)
        response = {"status": "ok", "error": None, "rooms": _plan_to_room_plans(result)}
        _INFERENCE_CACHE[key] = (time.time(), response)
        return response
    except Exception as exc:
        logger.exception("run_inference: échec")
        return {"status": "error", "error": str(exc), "rooms": []}


# ═══════════════════════════════════════════════════════════════════════════════
# FEATURES SOLAIRES  (d'après home_model/data/solar.py — algorithme NOAA)
# ═══════════════════════════════════════════════════════════════════════════════

def _solar_position(
    timestamps: pd.DatetimeIndex, latitude: float, longitude: float,
) -> pd.DataFrame:
    ts = timestamps.tz_localize("UTC") if timestamps.tz is None else timestamps.tz_convert("UTC")
    doy      = ts.dayofyear.to_numpy(dtype=float)
    hour_utc = (ts.hour + ts.minute / 60.0 + ts.second / 3600.0).to_numpy(dtype=float)
    n_days   = np.where(ts.is_leap_year, 366.0, 365.0)
    gamma    = 2 * np.pi / n_days * (doy - 1 + (hour_utc - 12) / 24)

    eqtime = 229.18 * (
        0.000075
        + 0.001868 * np.cos(gamma)   - 0.032077 * np.sin(gamma)
        - 0.014615 * np.cos(2*gamma) - 0.040849 * np.sin(2*gamma)
    )
    decl = (
        0.006918
        - 0.399912 * np.cos(gamma)   + 0.070257 * np.sin(gamma)
        - 0.006758 * np.cos(2*gamma) + 0.000907 * np.sin(2*gamma)
        - 0.002697 * np.cos(3*gamma) + 0.001480 * np.sin(3*gamma)
    )
    ha_deg = (hour_utc * 60 + eqtime + 4 * longitude) / 4 - 180
    lat    = np.radians(latitude)
    ha     = np.radians(ha_deg)

    cos_z = np.clip(np.sin(lat)*np.sin(decl) + np.cos(lat)*np.cos(decl)*np.cos(ha), -1.0, 1.0)
    elev  = 90.0 - np.degrees(np.arccos(cos_z))

    sin_z = np.sin(np.arccos(cos_z))
    with np.errstate(divide="ignore", invalid="ignore"):
        cos_az = (np.sin(decl) - np.sin(lat)*cos_z) / (np.cos(lat)*sin_z)
    cos_az = np.clip(np.nan_to_num(cos_az, nan=1.0), -1.0, 1.0)
    az     = np.degrees(np.arccos(cos_az))
    az     = np.where(ha_deg > 0, 360.0 - az, az)

    return pd.DataFrame({"elevation_deg": elev, "azimuth_deg": az}, index=timestamps)


def _face_exposure(elev: np.ndarray, az: np.ndarray, face_az: float) -> np.ndarray:
    cos_inc = np.cos(np.radians(elev)) * np.cos(np.radians(az) - np.radians(face_az))
    return np.where(elev > 0, np.clip(cos_inc, 0.0, None), 0.0)


def _compute_solar_features(
    timestamps: pd.DatetimeIndex,
    latitude: float = _LATITUDE,
    longitude: float = _LONGITUDE,
    house_faces: dict[str, float] | None = None,
) -> pd.DataFrame:
    if house_faces is None:
        house_faces = HOUSE_FACES
    pos = _solar_position(timestamps, latitude, longitude)
    out = pd.DataFrame(index=timestamps)
    out[f"solar{SEP}elevation"] = pos["elevation_deg"].to_numpy()
    out[f"solar{SEP}azimuth"]   = pos["azimuth_deg"].to_numpy()
    for face, face_az in house_faces.items():
        out[f"solar{SEP}face_exposure{SEP}{face}"] = _face_exposure(
            pos["elevation_deg"].to_numpy(), pos["azimuth_deg"].to_numpy(), face_az,
        )
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# DONNÉES MÉTÉO  (OpenMeteo → data_cache → feature table)
# ═══════════════════════════════════════════════════════════════════════════════

def _fetch_weather_for_inference() -> pd.DataFrame:
    """Retourne un DataFrame horaire avec les colonnes weather__* pour l'inférence.

    Lit depuis data_cache (clé 'weather.inference') si le cache est valide
    (< _WEATHER_INFERENCE_TTL secondes), sinon récupère depuis OpenMeteo et
    met à jour data_cache.

    Colonnes produites :
      weather__outdoor_temperature  (°C)
      weather__solar_irradiance     (W/m²  — shortwave_radiation OpenMeteo)
      weather__cloud_cover          (%)
      weather__kind                 ('observed' | 'forecast')
    """
    cached = data_cache.read("weather.inference")
    if cached is not None and (time.time() - cached["updated_at"]) < _WEATHER_INFERENCE_TTL:
        return _parse_weather_response(cached["value"])

    params = {
        "latitude":     _LATITUDE,
        "longitude":    _LONGITUDE,
        "timezone":     "UTC",
        "hourly":       ["temperature_2m", "shortwave_radiation", "cloud_cover"],
        "past_days":    2,
        "forecast_days": 2,
    }
    try:
        resp = requests.get(_OPENMETEO_URL, params=params, timeout=15)
        resp.raise_for_status()
        raw = resp.json()
    except requests.RequestException as exc:
        if cached is not None:
            logger.warning("OpenMeteo injoignable, utilisation du cache stale : %s", exc)
            return _parse_weather_response(cached["value"])
        raise ValueError(f"Impossible de récupérer la météo pour l'inférence : {exc}") from exc

    data_cache.write("weather.inference", raw)
    return _parse_weather_response(raw)


def _parse_weather_response(raw: dict) -> pd.DataFrame:
    """Convertit la réponse JSON OpenMeteo en DataFrame weather__* (index UTC)."""
    now_utc = pd.Timestamp.utcnow().tz_localize(None)   # naïf pour comparaison
    hourly  = raw["hourly"]
    times   = pd.to_datetime(hourly["time"])             # UTC car timezone=UTC dans la requête

    df = pd.DataFrame(index=times)
    df.index.name = "timestamp"
    df.index = df.index.tz_localize("UTC")

    df[f"weather{SEP}outdoor_temperature"] = hourly["temperature_2m"]
    df[f"weather{SEP}solar_irradiance"]    = hourly["shortwave_radiation"]
    df[f"weather{SEP}cloud_cover"]         = hourly["cloud_cover"]
    df[f"weather{SEP}kind"] = [
        "observed" if t.tz_localize(None) <= now_utc else "forecast"
        for t in times
    ]
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# ÉTAT VOLETS / FENÊTRES  (par défaut : tout fermé)
# ═══════════════════════════════════════════════════════════════════════════════

def _make_house_state_df(grid: pd.DatetimeIndex) -> pd.DataFrame:
    """Crée un DataFrame house__* avec toutes les colonnes à 0.0 (fermé).
    Sera alimenté par la domotique quand les actionneurs seront intégrés."""
    df = pd.DataFrame(index=grid)
    for room in _DEFAULT_ROOMS:
        for stype in HOUSE_STATE_TYPES:
            df[f"house{SEP}{room}{SEP}{stype}"] = 0.0
    return df


# ═══════════════════════════════════════════════════════════════════════════════
# PIPELINE DE FEATURES
# ═══════════════════════════════════════════════════════════════════════════════

@dataclass
class _FeatureStats:
    mean: np.ndarray
    std:  np.ndarray

    def transform(self, array: np.ndarray) -> np.ndarray:
        return ((array - self.mean) / self.std).astype(np.float32)

    def inverse_transform(self, array: np.ndarray) -> np.ndarray:
        return (array * self.std + self.mean).astype(np.float32)


def _resolution_segments_for(history_hours: float) -> list[dict]:
    remaining = history_hours * 60.0
    out: list[dict] = []
    for seg in RESOLUTION_SEGMENTS:
        if remaining <= 0:
            break
        dur = min(seg["duration_minutes"], remaining)
        out.append({"duration_minutes": dur, "resolution_minutes": seg["resolution_minutes"]})
        remaining -= dur
    return out


def _compute_window_offsets(
    resolution_segments: list[dict] | None = None,
    sample_interval_minutes: int = SAMPLE_INTERVAL_MINUTES,
) -> np.ndarray:
    """Décalages (pas de temps) des points de la fenêtre d'historique multi-résolution,
    triés du plus ancien (index 0) au plus récent (index -1, vaut 0)."""
    if resolution_segments is None:
        resolution_segments = RESOLUTION_SEGMENTS
    minutes_ago: list[float] = []
    cursor = 0.0
    for seg in resolution_segments:
        n = int(round(seg["duration_minutes"] / seg["resolution_minutes"]))
        for k in range(n):
            minutes_ago.append(cursor + k * seg["resolution_minutes"])
        cursor += seg["duration_minutes"]
    minutes_ago.sort(reverse=True)
    return np.asarray([round(m / sample_interval_minutes) for m in minutes_ago], dtype=np.int64)


def _select_columns(
    table: pd.DataFrame, prefixes: tuple[str, ...], exclude: tuple[str, ...] = (),
) -> list[str]:
    return [c for c in table.columns if c.startswith(prefixes) and c not in exclude]


def _build_feature_table() -> pd.DataFrame:
    """Construit la table de features pour l'inférence.

    Sources :
      · weather — OpenMeteo hourly (past_days=2 + forecast_days=2), mis en cache
                  dans data_cache sous 'weather.inference' (TTL 10 min)
      · house   — 0.0 par défaut (volets/fenêtres fermés ; sera alimenté par domotique)
      · solar   — calculé algorithmiquement (algorithme NOAA)

    Le modèle 'limited' ne prend pas les capteurs intérieurs en entrée
    (il les prédit) — les colonnes indoor__* n'ont donc pas leur place ici.
    """
    weather_df = _fetch_weather_for_inference()  # index UTC, résolution horaire

    # Grille 2 min couvrant l'intégralité de la fenêtre météo
    grid = pd.date_range(
        weather_df.index.min(), weather_df.index.max(),
        freq=f"{SAMPLE_INTERVAL_MINUTES}min", tz="UTC",
    )

    # Météo : interpolation linéaire horaire → 2 min
    kind_col = f"weather{SEP}kind"
    num_cols = [c for c in weather_df.columns if c != kind_col]
    w = weather_df.reindex(weather_df.index.union(grid)).sort_index()
    w[num_cols] = w[num_cols].interpolate(method="time")
    w[kind_col] = w[kind_col].ffill().bfill()
    w = w.reindex(grid)
    w[num_cols] = w[num_cols].ffill().bfill()

    table = pd.concat(
        [w, _make_house_state_df(grid), _compute_solar_features(grid)],
        axis=1,
    )
    table.index.name = "timestamp"
    return table


# ═══════════════════════════════════════════════════════════════════════════════
# CHARGEMENT DU MODÈLE ONNX
# ═══════════════════════════════════════════════════════════════════════════════

_MODEL_CACHE: tuple | None = None   # (mtime, sess, meta)


def _load_onnx_model(path: Path = MODELS_DIR / "limited.onnx") -> tuple[ort.InferenceSession, dict]:
    """Charge une session ONNX Runtime et extrait les métadonnées embarquées.

    Les métadonnées (colonnes, stats de normalisation, hyperparamètres) sont
    stockées dans le champ custom 'home_model_meta' du fichier ONNX sous forme
    de JSON. Les stats sont reconstruites en objets _FeatureStats numpy.

    Mise en cache module (clé = mtime du fichier) : recharger la session à
    chaque clic sur CALCULER coûte plusieurs centaines de ms sur RPi 3 pour
    un fichier qui ne change jamais en fonctionnement normal.

    Retourne : (session, meta_dict)
    """
    global _MODEL_CACHE
    mtime = path.stat().st_mtime
    if _MODEL_CACHE is not None and _MODEL_CACHE[0] == mtime:
        return _MODEL_CACHE[1], _MODEL_CACHE[2]

    sess = ort.InferenceSession(str(path))
    raw  = json.loads(sess.get_modelmeta().custom_metadata_map["home_model_meta"])

    def _to_stats(d: dict) -> _FeatureStats:
        return _FeatureStats(
            mean=np.array(d["mean"], dtype=np.float32),
            std=np.array(d["std"],  dtype=np.float32),
        )

    meta = {**raw,
            "limited_stats": _to_stats(raw["limited_stats"]),
            "target_stats":  _to_stats(raw["target_stats"])}
    _MODEL_CACHE = (mtime, sess, meta)
    return sess, meta


# ═══════════════════════════════════════════════════════════════════════════════
# STRATÉGIE — CONFORT  (d'après home_model/strategy/comfort.py)
# ═══════════════════════════════════════════════════════════════════════════════

REASON_MAINTAIN = "maintenir"
REASON_COOL     = "refroidir"
REASON_WARM     = "rechauffer"


def _room_bounds(
    room: str, comfort_ranges: dict[str, tuple[float, float]] | None,
) -> tuple[float, float]:
    if comfort_ranges and room in comfort_ranges:
        lo, hi = comfort_ranges[room]
        return float(lo), float(hi)
    return COMFORT_TEMP_MIN, COMFORT_TEMP_MAX


def _comfort_cost(
    temperatures: np.ndarray,
    rooms: list[str] | None = None,
    comfort_ranges: dict[str, tuple[float, float]] | None = None,
) -> float:
    if rooms is not None:
        t_min = np.array([_room_bounds(r, comfort_ranges)[0] for r in rooms])
        t_max = np.array([_room_bounds(r, comfort_ranges)[1] for r in rooms])
    else:
        t_min, t_max = COMFORT_TEMP_MIN, COMFORT_TEMP_MAX
    return float(
        (np.clip(t_min - temperatures, 0, None) + np.clip(temperatures - t_max, 0, None)).sum()
    )


def _block_reasons(
    temperatures_by_room: dict[str, np.ndarray],
    eval_rows: np.ndarray,
    now_idx: int,
    n_blocks: int,
    block_hours: float,
    comfort_ranges: dict[str, tuple[float, float]] | None = None,
) -> dict[str, list[str]]:
    block_minutes    = block_hours * 60.0
    minutes_from_now = (eval_rows - now_idx) * SAMPLE_INTERVAL_MINUTES
    block_indices    = np.minimum((minutes_from_now // block_minutes).astype(int), n_blocks - 1)
    reasons: dict[str, list[str]] = {}
    for room, temps in temperatures_by_room.items():
        t_min, t_max = _room_bounds(room, comfort_ranges)
        row: list[str] = []
        for k in range(n_blocks):
            bt = temps[block_indices == k]
            if bt.size == 0:
                row.append(REASON_MAINTAIN)
            elif (bt < t_min).any():
                row.append(REASON_WARM)
            elif (bt > t_max).any():
                row.append(REASON_COOL)
            else:
                row.append(REASON_MAINTAIN)
        reasons[room] = row
    return reasons


# ═══════════════════════════════════════════════════════════════════════════════
# STRATÉGIE — SCHEDULE  (d'après home_model/strategy/schedule.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _n_blocks(horizon_hours: float, block_hours: float) -> int:
    return int(round(horizon_hours / block_hours))


def _random_schedule(
    rooms: list[str], state_types: list[str],
    horizon_hours: float, block_hours: float, rng: np.random.Generator,
) -> dict[tuple[str, str], np.ndarray]:
    n = _n_blocks(horizon_hours, block_hours)
    return {
        (room, stype): rng.integers(0, 2, size=n).astype(np.float32)
        for room in rooms for stype in state_types
    }


def _schedule_to_steps(
    schedule: dict[tuple[str, str], np.ndarray], n_steps: int, block_hours: float,
) -> dict[tuple[str, str], np.ndarray]:
    spb = int(round(block_hours * 60 / SAMPLE_INTERVAL_MINUTES))
    out: dict[tuple[str, str], np.ndarray] = {}
    for key, blocks in schedule.items():
        steps = np.repeat(blocks, spb)
        if len(steps) < n_steps:
            steps = np.concatenate([steps, np.full(n_steps - len(steps), steps[-1], dtype=steps.dtype)])
        out[key] = steps[:n_steps]
    return out


# ═══════════════════════════════════════════════════════════════════════════════
# STRATÉGIE — ROLLOUT  (d'après home_model/strategy/rollout.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _find_now_index(table: pd.DataFrame) -> int:
    """Index de la dernière ligne météo 'observed' — sépare historique et futur."""
    kind_col = f"weather{SEP}kind"
    observed = (table[kind_col] == "observed").to_numpy()
    if not observed.any():
        raise ValueError("Aucune donnée météo 'observed' : impossible de situer 'maintenant'.")
    return int(np.flatnonzero(observed)[-1])


class _PlanningContext:
    """Pré-calcule la fenêtre de données commune à tous les plannings candidats."""

    def __init__(
        self, table: pd.DataFrame, checkpoint: dict,
        horizon_hours: float = PLANNING_HORIZON_HOURS,
        eval_step_minutes: float = PLANNING_EVAL_STEP_MINUTES,
    ) -> None:
        self.limited_columns: list[str] = checkpoint["limited_columns"]
        table_lim = set(_select_columns(
            table, (f"weather{SEP}", f"solar{SEP}", f"house{SEP}"), exclude=(f"weather{SEP}kind",)
        ))
        if table_lim != set(self.limited_columns):
            missing = set(self.limited_columns) - table_lim
            extra   = table_lim - set(self.limited_columns)
            raise ValueError(
                "Les colonnes 'limited' de la table ne correspondent pas au checkpoint "
                f"(manquantes: {sorted(missing)}, en trop: {sorted(extra)}). "
                "Réentraînez le modèle."
            )

        self.offsets       = _compute_window_offsets(_resolution_segments_for(checkpoint["history_hours"]))
        self.horizon_steps = checkpoint["horizon_steps"]
        self.limited_stats = checkpoint["limited_stats"]
        self.target_stats  = checkpoint["target_stats"]

        self.now_idx   = _find_now_index(table)
        max_offset     = int(self.offsets.max())
        if self.now_idx - max_offset < 0:
            raise ValueError("Pas assez d'historique pour construire la fenêtre du modèle.")

        requested                = int(round(horizon_hours * 60.0 / SAMPLE_INTERVAL_MINUTES))
        max_future               = len(table) - 1 - self.now_idx
        self.horizon_steps_count = min(requested, max_future)
        if self.horizon_steps_count <= 0:
            raise ValueError("Aucune donnée future (prévisions météo) disponible.")

        lo = self.now_idx - max_offset
        hi = self.now_idx + self.horizon_steps_count
        self.lo = lo
        self.hi = hi
        self.window_array        = table[self.limited_columns].to_numpy(dtype=np.float32)[lo : hi + 1]
        self.future_start_local  = self.now_idx - lo + 1

        eval_step = max(1, int(round(eval_step_minutes / SAMPLE_INTERVAL_MINUTES)))
        eval_rows = np.arange(self.now_idx + eval_step, hi + 1, eval_step)
        if len(eval_rows) == 0:
            eval_rows = np.asarray([hi])

        rows_mat = (eval_rows - self.horizon_steps)[:, None] - self.offsets[None, :]
        self.local_rows_matrix = rows_mat - lo
        if self.local_rows_matrix.min() < 0 or self.local_rows_matrix.max() > hi - lo:
            raise ValueError("Fenêtre de planification mal alignée (index hors limites).")

        self.house_state_cols: dict[tuple[str, str], int] = {}
        for room in _DEFAULT_ROOMS:
            for stype in HOUSE_STATE_TYPES:
                col = f"house{SEP}{room}{SEP}{stype}"
                if col in self.limited_columns:
                    self.house_state_cols[(room, stype)] = self.limited_columns.index(col)

        target_cols = checkpoint["target_columns"]
        self.temperature_target_indices = [i for i, c in enumerate(target_cols) if c.endswith(f"{SEP}temperature")]
        self.temperature_target_rooms   = [target_cols[i].split(SEP)[1] for i in self.temperature_target_indices]
        self.eval_rows     = eval_rows
        self.now_timestamp = table.index[self.now_idx]

    def _predict(
        self, sess: ort.InferenceSession, schedule_steps: dict[tuple[str, str], np.ndarray],
    ) -> np.ndarray:
        arr = self.window_array.copy()
        end = self.future_start_local + self.horizon_steps_count
        for key, col in self.house_state_cols.items():
            arr[self.future_start_local : end, col] = schedule_steps[key]
        arr_norm = self.limited_stats.transform(arr)
        batch    = arr_norm[self.local_rows_matrix]
        outputs  = sess.run(["predictions"], {"x_limited": batch})[0]
        return self.target_stats.inverse_transform(outputs)

    def evaluate(
        self, sess: ort.InferenceSession, schedule_steps: dict[tuple[str, str], np.ndarray],
        comfort_ranges: dict[str, tuple[float, float]] | None = None,
    ) -> float:
        pred = self._predict(sess, schedule_steps)
        return _comfort_cost(pred[:, self.temperature_target_indices], self.temperature_target_rooms, comfort_ranges)

    def predict_temperatures(
        self, sess: ort.InferenceSession, schedule_steps: dict[tuple[str, str], np.ndarray],
    ) -> dict[str, np.ndarray]:
        pred = self._predict(sess, schedule_steps)
        return {
            room: pred[:, idx]
            for room, idx in zip(self.temperature_target_rooms, self.temperature_target_indices)
        }


# ═══════════════════════════════════════════════════════════════════════════════
# STRATÉGIE — FORMAT  (d'après home_model/strategy/format.py)
# ═══════════════════════════════════════════════════════════════════════════════

_STATE_LABELS = {0: "closed", 1: "open"}


def _schedule_to_plan(
    schedule: dict[tuple[str, str], np.ndarray],
    rooms: list[str], state_types: list[str],
    now: pd.Timestamp, horizon_hours: float, block_hours: float,
    reasons: dict[str, list[str]] | None = None,
) -> dict:
    """Convertit un planning par créneaux en dict JSON par pièce (intervalles fusionnés)."""
    n           = _n_blocks(horizon_hours, block_hours)
    block_delta = pd.Timedelta(hours=block_hours)
    rooms_plan: dict[str, list[dict]] = {}

    for room in rooms:
        states       = [tuple(schedule[(room, stype)][k] for stype in state_types) for k in range(n)]
        room_reasons = reasons[room] if reasons else None

        def merge_key(k: int) -> tuple:
            return (states[k], room_reasons[k]) if room_reasons else (states[k],)

        intervals: list[dict] = []
        bs = 0
        for k in range(1, n + 1):
            if k < n and merge_key(k) == merge_key(bs):
                continue
            interval = {
                "from": (now + bs * block_delta).isoformat(),
                "to":   (now + k  * block_delta).isoformat(),
            }
            for stype, val in zip(state_types, states[bs]):
                interval[stype] = _STATE_LABELS[int(val)]
            if room_reasons:
                interval["reason"] = room_reasons[bs]
            intervals.append(interval)
            bs = k
        rooms_plan[room] = intervals

    return {"generated_at": now.isoformat(), "horizon_hours": horizon_hours, "rooms": rooms_plan}


# ═══════════════════════════════════════════════════════════════════════════════
# PLANIFICATEUR  (d'après home_model/strategy/planner.py)
# ═══════════════════════════════════════════════════════════════════════════════

def _plan(
    n_candidates: int = PLANNING_N_CANDIDATES,
    horizon_hours: float = PLANNING_HORIZON_HOURS,
    block_hours: float = PLANNING_BLOCK_HOURS,
    eval_step_minutes: float = PLANNING_EVAL_STEP_MINUTES,
    seed: int = RANDOM_SEED,
    comfort_ranges: dict[str, tuple[float, float]] | None = None,
) -> dict:
    sess, meta = _load_onnx_model()
    table = _build_feature_table()
    ctx   = _PlanningContext(table, meta, horizon_hours=horizon_hours, eval_step_minutes=eval_step_minutes)

    rooms       = _DEFAULT_ROOMS
    state_types = HOUSE_STATE_TYPES
    rng         = np.random.default_rng(seed)

    best_schedule: dict[tuple[str, str], np.ndarray] | None = None
    best_cost = float("inf")
    for _ in range(n_candidates):
        candidate = _random_schedule(rooms, state_types, horizon_hours, block_hours, rng)
        steps     = _schedule_to_steps(candidate, ctx.horizon_steps_count, block_hours)
        cost      = ctx.evaluate(sess, steps, comfort_ranges)
        if cost < best_cost:
            best_cost, best_schedule = cost, candidate
            if best_cost == 0.0:   # borne inférieure de _comfort_cost (confort parfait) — arrêt anticipé
                break

    eff_horizon = ctx.horizon_steps_count * SAMPLE_INTERVAL_MINUTES / 60.0
    eff_blocks  = min(_n_blocks(horizon_hours, block_hours), _n_blocks(eff_horizon, block_hours))
    if eff_blocks < _n_blocks(horizon_hours, block_hours):
        best_schedule = {k: v[:eff_blocks] for k, v in best_schedule.items()}

    best_steps    = _schedule_to_steps(best_schedule, ctx.horizon_steps_count, block_hours)
    temps_by_room = ctx.predict_temperatures(sess, best_steps)
    reasons       = _block_reasons(
        temps_by_room, ctx.eval_rows, ctx.now_idx, eff_blocks, block_hours, comfort_ranges,
    )
    result = _schedule_to_plan(
        best_schedule, rooms, state_types,
        now=ctx.now_timestamp,
        horizon_hours=eff_blocks * block_hours,
        block_hours=block_hours,
        reasons=reasons,
    )
    result["best_cost"]    = best_cost
    result["n_candidates"] = n_candidates
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# CONVERSION RÉSULTAT → RoomPlan
# ═══════════════════════════════════════════════════════════════════════════════

def _plan_to_room_plans(result: dict) -> list[RoomPlan]:
    """Convertit le dict de _plan() en liste de RoomPlan pour le dashboard.

    _schedule_to_plan fusionnant déjà les créneaux consécutifs identiques,
    intervals[0] couvre directement la durée du premier bloc stable.
    """
    room_plans: list[RoomPlan] = []
    for room_id, intervals in result["rooms"].items():
        if not intervals:
            continue
        first   = intervals[0]
        actions = [
            "VOLET OUVERT"    if first.get("shutter") == "open"   else "VOLET FERMÉ",
            "FENÊTRE OUVERTE" if first.get("window")  == "open"   else "FENÊTRE FERMÉE",
            _REASON_LABELS.get(first.get("reason", ""), str(first.get("reason", "")).upper()),
        ]
        until = pd.Timestamp(first["to"]).strftime("%H:%M")
        room_plans.append(RoomPlan(room_id=room_id, room_name=room_id, actions=actions, until=until))
    return room_plans
