"""
HomeOS — modules/anomaly_engine.py
═══════════════════════════════════════════════════════════════════════════════
Détection d'anomalies sur les séries capteurs de data/cache.db, par Isolation
Forest (Liu, Ting & Zhou, ICDM 2008), implémenté en numpy pur.

Alimente la boîte « Alertes ML » de la page Système. Remplace les valeurs
aléatoires qui y tenaient lieu de maquette : les scores sont désormais calculés
sur l'historique réel de chaque capteur.

── Pourquoi une implémentation maison ────────────────────────────────────────
sklearn.ensemble.IsolationForest est la référence, mais scikit-learn tire scipy :
~110 Mo d'image Docker et ~90 Mo de RSS pour une seule boîte d'interface, sur une
cible à 1 Go de RAM (RPi 3). L'algorithme tient en une centaine de lignes de
numpy — déjà une dépendance du moteur de confort, et importé paresseusement.

── Ce qui est mesuré ─────────────────────────────────────────────────────────
Chaque série `sensor_<scope>_<field>` est rééchantillonnée sur une grille
régulière (GRID_S) couvrant TRAIN_WINDOW_S, puis décrite par cinq variables
sans dimension, donc comparables d'un capteur à l'autre :

  z      écart à la médiane de la série, en IQR      → valeur hors plage habituelle
  d1     variation sur un pas de grille, en IQR      → pic / chute rapide
  d3h    écart à la moyenne mobile 3 h, en IQR       → dérive lente
  gap    log1p(âge du relevé / intervalle médian)    → capteur intermittent
  xroom  écart à la médiane inter-pièces, en IQR     → corrélation inter-pièces rompue

Seuls les pas de grille où le dernier relevé a moins de MAX_HOLD_S entrent dans
l'entraînement : ailleurs la valeur maintenue ne décrit plus le capteur, mais son
silence — dont la règle ci-dessous se charge.

Les lignes de toutes les séries d'un même `field` sont mises en commun pour
entraîner UNE forêt par field (température, hygrométrie) : plus de données
d'entraînement, et un capteur isolé bénéficie du comportement des autres. La
normalisation par capteur (médiane / IQR propres à la série) est ce qui rend
cette mise en commun légitime — une pièce structurellement plus froide qu'une
autre ne ressort pas comme anomalie.

── Silence capteur ───────────────────────────────────────────────────────────
Un capteur muet depuis plus de SILENT_AGE_S est signalé par une règle
déterministe, AVANT la forêt, et non par elle. C'est délibéré : un capteur
tombé depuis trois jours remplit la moitié de la fenêtre d'entraînement de
points « silencieux », qui cessent donc d'être rares — une forêt d'isolation les
jugerait normaux. La forêt reste chargée du reste, y compris des silences
courts et intermittents via la variable `gap`.

── Score affiché ─────────────────────────────────────────────────────────────
Le score brut d'Isolation Forest vaut s = 2^(−E[h(x)]/c(ψ)) ∈ (0,1) : 0,5 quand
la longueur de chemin moyenne égale celle attendue d'un point quelconque (donc
« typique »), et tend vers 1 pour un point isolé en peu de coupes. L'interface
en affiche une remise à l'échelle affine bornée à [0, 1] (cf. RAW_TYPICAL /
RAW_ISOLATED), qui conserve les seuils d'affichage existants — 0,3 ATTENTION,
0,5 ANOMALIE.

── Coût ──────────────────────────────────────────────────────────────────────
L'entraînement n'est pas gratuit (~0,3 s sur SSD, quelques secondes sur RPi 3)
et la page Système se rafraîchit toutes les 8 s. `get_scores()` sert donc
toujours le dernier résultat connu et déclenche le recalcul dans un thread de
fond : aucun callback Dash n'attend jamais la forêt.

API publique : get_scores(), compute_scores(), SensorScore, IsolationForest
═══════════════════════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import math
import threading
import time
from dataclasses import dataclass

from modules.data_cache import data_cache
from modules.lazy import LazyModule

logger = logging.getLogger(__name__)

np = LazyModule("numpy")

MODEL_NAME = "IsolationForest · numpy"

# ── Fenêtre et grille ─────────────────────────────────────────────────────────
TRAIN_WINDOW_S = 7 * 86400   # profondeur d'historique servant d'entraînement
GRID_S         = 900         # pas de rééchantillonnage (15 min)
DRIFT_STEPS    = 12          # 12 pas = 3 h, fenêtre de la moyenne mobile
# Un capteur est déclaré silencieux au-delà de SILENT_FACTOR fois son intervalle
# de relevé habituel, avec SILENT_AGE_S pour plancher. Le seuil doit être relatif
# aux habitudes du capteur : un SNZB-02LD de frigo émet toutes les 30 min, deux
# battements manqués suffiraient à franchir un seuil fixe d'une heure, alors
# qu'un capteur bavard peut se taire vingt minutes sans que ce soit normal.
SILENT_AGE_S   = 3600
SILENT_FACTOR  = 4

# Le même seuil borne les pas de grille retenus pour l'entraînement : un pas dont
# le relevé maintenu est plus vieux ne dit RIEN des valeurs du capteur, seulement
# qu'il se taisait à cet instant — ce dont la règle de silence se charge déjà.
# L'inclure serait activement nuisible : un capteur qui n'émet que par salves
# (78 relevés sur 7 j, observé en exploitation) remplirait 88 % de la fenêtre de
# valeurs figées, et la forêt apprendrait ce plateau plutôt que le capteur.
# Couverture minimale, en pas de grille, pour qu'un capteur soit jugeable :
# l'équivalent de 12 h. Il s'agit de pouvoir estimer une médiane et un IQR
# robustes et de donner de la matière à la forêt, pas d'exiger une plage
# continue — ces pas peuvent être dispersés n'importe où dans les 7 jours, et le
# sont en pratique dès que la collecte a connu des interruptions.
MIN_GRID_ROWS  = int(12 * 3600 // GRID_S)

# ── Hyperparamètres de la forêt ───────────────────────────────────────────────
# Les valeurs par défaut de sklearn, qui sont aussi celles recommandées par le
# papier : 100 arbres, sous-échantillon ψ=256. Le coût ne dépend NI du nombre de
# capteurs NI de la profondeur d'historique — seulement de ces deux nombres —,
# donc rien n'oblige à les rogner pour le RPi 3.
N_ESTIMATORS = 100
MAX_SAMPLES  = 256
RANDOM_SEED  = 42

# ── Calibrage du score affiché ────────────────────────────────────────────────
# Le score brut vaut 0,5 pour un point de longueur de chemin exactement moyenne,
# mais la bande « normale » d'une vraie fenêtre de 7 jours s'étend jusqu'à ~0,58 :
# les cinq variables sont corrélées (un pic fait bouger z, d1 ET d3h) et les
# queues sont épaisses. Mesuré sur trois séries de température synthétiques
# calibrées sur du relevé Zigbee réel (cycle jour/nuit, σ = 0,15 °C) : bande
# calme 0,46–0,58, pic de +0,8 °C en 15 min 0,63, pic de +5 °C 0,73.
# La transformation affine ci-dessous place donc ATTENTION (0,3) à un brut de
# 0,625 et ANOMALIE (0,5) à 0,675 — et sature à 0,80.
RAW_TYPICAL  = 0.55
RAW_ISOLATED = 0.80

# Fraîcheur du résultat servi par get_scores() avant déclenchement d'un recalcul.
REFRESH_TTL = 600

# Seuils d'affichage — miroirs de ceux de _anomaly_row (modules/callbacks.py).
THRESH_WARN  = 0.3
THRESH_ALERT = 0.5

_EULER = 0.5772156649015329


@dataclass(frozen=True)
class SensorScore:
    """Score d'anomalie d'un capteur, prêt à l'affichage.

    score  ∈ [0, 1] — 0 = comportement typique, 1 = franchement isolé.
    status NORMAL / ATTENTION / ANOMALIE / SILENCIEUX / APPRENTISSAGE.
    detail phrase courte expliquant le score (title HTML de la ligne).
    """
    scope:  str
    field:  str
    score:  float
    status: str
    detail: str


# ══════════════════════════════════════════════════════════════════════════════
# Isolation Forest
# ══════════════════════════════════════════════════════════════════════════════

def _c(n):
    """Longueur de chemin moyenne d'une recherche infructueuse dans un BST de n
    nœuds — le facteur de normalisation c(n) du papier. Accepte un scalaire ou
    un tableau ; vaut 0 pour n <= 1 (feuille pure, aucune correction)."""
    arr = np.atleast_1d(np.asarray(n, dtype=np.float64))
    out = np.zeros_like(arr)
    m   = arr > 1
    out[m] = 2 * (np.log(arr[m] - 1) + _EULER) - 2 * (arr[m] - 1) / arr[m]
    return out if np.ndim(n) else float(out[0])


class _Tree:
    """Arbre d'isolation stocké à plat : un tableau par attribut de nœud.

    La représentation plate n'est pas une coquetterie — elle permet de faire
    descendre TOUS les points en même temps, niveau par niveau, en indexation
    numpy (cf. _path_lengths). Une descente point par point en Python coûterait
    plusieurs secondes sur RPi 3.
    """

    __slots__ = ("feature", "threshold", "left", "right", "size", "is_leaf")

    def __init__(self):
        self.feature:   list[int]   = []
        self.threshold: list[float] = []
        self.left:      list[int]   = []
        self.right:     list[int]   = []
        self.size:      list[int]   = []   # nb de points, feuilles seulement
        self.is_leaf:   list[bool]  = []

    def _add(self) -> int:
        self.feature.append(-1)
        self.threshold.append(0.0)
        self.left.append(-1)
        self.right.append(-1)
        self.size.append(0)
        self.is_leaf.append(True)
        return len(self.feature) - 1

    def freeze(self) -> None:
        """Convertit les listes en tableaux numpy — indexation vectorielle."""
        self.feature   = np.asarray(self.feature,   dtype=np.int32)
        self.threshold = np.asarray(self.threshold, dtype=np.float64)
        self.left      = np.asarray(self.left,      dtype=np.int32)
        self.right     = np.asarray(self.right,     dtype=np.int32)
        self.size      = np.asarray(self.size,      dtype=np.int32)
        self.is_leaf   = np.asarray(self.is_leaf,   dtype=bool)


class IsolationForest:
    """Isolation Forest (Liu, Ting & Zhou, 2008) — numpy, sans dépendance.

    Interface volontairement calquée sur celle de sklearn : fit(X) puis
    score_samples(X). Une différence : `score_samples` retourne le score BRUT du
    papier, dans (0, 1) et croissant avec l'anormalité, là où sklearn retourne
    son opposé. Cf. l'en-tête du module pour la conversion en score affiché.
    """

    def __init__(self, n_estimators: int = N_ESTIMATORS,
                 max_samples: int = MAX_SAMPLES,
                 seed: int = RANDOM_SEED):
        self.n_estimators = n_estimators
        self.max_samples  = max_samples
        self.seed         = seed
        self._trees: list[_Tree] = []
        self._psi = 0

    def fit(self, X) -> "IsolationForest":
        X   = np.asarray(X, dtype=np.float64)
        n   = X.shape[0]
        psi = min(self.max_samples, n)
        # Hauteur maximale du papier : au-delà de log2(ψ), les points restants
        # sont dans la masse et leur profondeur exacte n'apporte plus rien.
        limit = max(1, int(math.ceil(math.log2(psi))) if psi > 1 else 1)
        rng   = np.random.default_rng(self.seed)

        self._psi   = psi
        self._trees = []
        for _ in range(self.n_estimators):
            idx  = rng.choice(n, size=psi, replace=False) if psi < n else np.arange(n)
            tree = _Tree()
            self._grow(tree, X[idx], np.arange(psi), 0, limit, rng)
            tree.freeze()
            self._trees.append(tree)
        return self

    def _grow(self, tree: _Tree, X, idx, depth: int, limit: int, rng) -> int:
        node = tree._add()
        n    = idx.size
        if depth >= limit or n <= 1:
            tree.size[node] = int(n)
            return node

        sub  = X[idx]
        lo   = sub.min(axis=0)
        hi   = sub.max(axis=0)
        # Une variable constante sur ce sous-ensemble ne peut pas séparer :
        # la tirer produirait une coupe dégénérée (tout d'un côté) et un arbre
        # de profondeur inutile. Sans variable exploitable, on ferme en feuille.
        usable = np.flatnonzero(hi > lo)
        if usable.size == 0:
            tree.size[node] = int(n)
            return node

        f = int(usable[rng.integers(usable.size)])
        q = float(rng.uniform(lo[f], hi[f]))
        mask = sub[:, f] < q
        # rng.uniform inclut sa borne basse : q == lo[f] donne une coupe qui ne
        # sépare rien. Cas improbable mais qui produirait une chaîne de nœuds
        # dégénérés jusqu'à `limit` — on ferme en feuille.
        if not mask.any() or mask.all():
            tree.size[node] = int(n)
            return node

        tree.is_leaf[node]   = False
        tree.feature[node]   = f
        tree.threshold[node] = q
        left  = self._grow(tree, X, idx[mask],  depth + 1, limit, rng)
        right = self._grow(tree, X, idx[~mask], depth + 1, limit, rng)
        tree.left[node]  = left
        tree.right[node] = right
        return node

    @staticmethod
    def _path_lengths(tree: _Tree, X):
        """Profondeur atteinte par chaque ligne de X dans `tree`, corrigée par
        c(taille de la feuille). Descente vectorisée : à chaque tour de boucle,
        tous les points encore en vol avancent d'un niveau."""
        n     = X.shape[0]
        node  = np.zeros(n, dtype=np.int32)
        depth = np.zeros(n, dtype=np.float64)
        rows  = np.arange(n)
        while True:
            moving = ~tree.is_leaf[node]
            if not moving.any():
                break
            cur  = node[moving]
            vals = X[rows[moving], tree.feature[cur]]
            node[moving] = np.where(vals < tree.threshold[cur],
                                    tree.left[cur], tree.right[cur])
            depth[moving] += 1.0
        return depth + _c(tree.size[node])

    def score_samples(self, X):
        """Score brut s(x, ψ) = 2^(−E[h(x)]/c(ψ)) ∈ (0, 1), croissant avec
        l'anormalité. ~0,5 = point typique, → 1 = point isolé."""
        X = np.asarray(X, dtype=np.float64)
        if not self._trees:
            raise RuntimeError("IsolationForest.score_samples appelé avant fit()")
        total = np.zeros(X.shape[0], dtype=np.float64)
        for tree in self._trees:
            total += self._path_lengths(tree, X)
        norm = _c(self._psi)
        if norm <= 0:
            return np.full(X.shape[0], 0.5)
        return 2.0 ** (-(total / len(self._trees)) / norm)


# ══════════════════════════════════════════════════════════════════════════════
# Extraction des variables
# ══════════════════════════════════════════════════════════════════════════════

def _norm(x, scale_ref=None):
    """Divise x par l'IQR de `scale_ref` (x par défaut) — mise à l'échelle
    robuste, insensible aux valeurs extrêmes que l'on cherche justement à
    détecter. Une série constante (IQR nul) retourne des zéros plutôt que des
    infinis : sans variation, il n'y a rien à isoler."""
    ref = x if scale_ref is None else scale_ref
    q1, q3 = np.percentile(ref, [25, 75])
    iqr = float(q3 - q1)
    if iqr <= 1e-9:
        std = float(np.std(ref))
        iqr = std if std > 1e-9 else 0.0
    if iqr == 0.0:
        return np.zeros_like(x)
    return x / iqr


def _resample(points, grid):
    """Rééchantillonne [(ts, valeur), …] sur `grid` par maintien de la dernière
    valeur connue. Retourne (valeurs, âge du relevé, masque de validité).

    Le maintien (et non l'interpolation) est le bon choix ici : un capteur
    Zigbee émet à intervalle irrégulier et la valeur reste vraie jusqu'au relevé
    suivant. C'est aussi ce qui rend `age` interprétable — il mesure le silence
    réel du capteur, pas un artefact de grille.
    """
    ts   = np.asarray([p[0] for p in points], dtype=np.float64)
    vals = np.asarray([p[1] for p in points], dtype=np.float64)
    idx  = np.searchsorted(ts, grid, side="right") - 1
    valid = idx >= 0
    safe  = np.clip(idx, 0, None)
    # Les pas antérieurs au premier relevé n'ont pas d'âge : grid − ts[0] y est
    # négatif, ce qui produirait un log1p(−…) infini ou NaN dans la variable
    # `gap`. Ces lignes sont écartées de l'entraînement par `valid`, mais une
    # valeur non finie n'a rien à faire dans la matrice pour autant.
    age = np.where(valid, grid - ts[safe], 0.0)
    return vals[safe], age, valid


def _typical_interval(points) -> float:
    """Intervalle médian entre deux relevés réels — référence du « rythme
    normal » d'un capteur qui, selon son modèle, émet toutes les 5 ou 30 min.
    Jamais sous le pas de grille, qui est la résolution du reste du calcul."""
    ts     = np.asarray([p[0] for p in points], dtype=np.float64)
    deltas = np.diff(ts)
    return max(float(np.median(deltas)) if deltas.size else GRID_S, GRID_S)


def _silence_limit(points) -> float:
    """Ancienneté au-delà de laquelle ce capteur est considéré comme muet."""
    return max(SILENT_AGE_S, SILENT_FACTOR * _typical_interval(points))


def _features(points, grid, cross_ref=None):
    """Matrice (len(grid), 5) des variables décrites en en-tête, et masque des
    lignes exploitables. `cross_ref` est la médiane inter-pièces déjà alignée
    sur la grille (None si le field n'a qu'un capteur)."""
    v, age, valid = _resample(points, grid)
    k     = DRIFT_STEPS
    fresh = valid & (age <= _silence_limit(points))

    med = float(np.median(v[valid])) if valid.any() else 0.0
    z   = _norm(v - med, scale_ref=v[valid] if valid.any() else v)

    # d1 et d3h sont des VITESSES : elles n'ont de sens que si les pas auxquels
    # elles se comparent portaient eux aussi un relevé frais. Au sortir d'un
    # silence de trois jours, v[i] − v[i−1] vaut l'écart entre la valeur du jour
    # et celle d'avant la coupure — un faux pic, qui n'a rien d'une variation
    # rapide. On remet ces deux variables à zéro plutôt que d'écarter la ligne :
    # elle reste décrite par `z` et par un `gap` élevé, qui est le vrai signal.
    d1 = np.zeros_like(v)
    d1[1:] = v[1:] - v[:-1]
    prev_fresh = np.concatenate(([False], fresh[:-1]))
    d1 = _norm(np.where(fresh & prev_fresh, d1, 0.0))

    # Moyenne mobile sur DRIFT_STEPS pas, par somme cumulée : le coût ne dépend
    # pas de la largeur de la fenêtre. Même somme cumulée sur `fresh` pour savoir
    # si la fenêtre est entièrement couverte.
    csum  = np.concatenate(([0.0], np.cumsum(v)))
    cfresh = np.concatenate(([0], np.cumsum(fresh)))
    d3h   = np.zeros_like(v)
    if v.size > k:
        roll        = (csum[k:-1] - csum[:-k - 1]) / k
        window_full = (cfresh[k:-1] - cfresh[:-k - 1]) == k
        d3h[k:]     = np.where(window_full, v[k:] - roll, 0.0)
    d3h = _norm(np.where(fresh, d3h, 0.0))

    gap = np.log1p(age / _typical_interval(points))

    if cross_ref is None:
        xroom = np.zeros_like(v)
    else:
        xroom = _norm(v - med - cross_ref)

    X = np.column_stack([z, d1, d3h, gap, xroom])
    # Les lignes précédant le premier relevé n'ont pas de valeur, et les
    # DRIFT_STEPS suivantes pas de moyenne mobile complète. Les lignes trop
    # anciennes ne décrivent qu'un silence — cf. MAX_HOLD_S.
    usable = fresh.copy()
    first  = int(np.argmax(valid)) if valid.any() else 0
    usable[:min(first + k, usable.size)] = False
    return X, usable


def _cross_reference(per_sensor, grid):
    """Médiane inter-capteurs, centrée par capteur, alignée sur la grille.

    Chaque capteur est d'abord ramené à son propre écart à sa médiane : c'est le
    PROFIL qu'on compare, pas le niveau absolu. Un salon à 22 °C et un bureau à
    18 °C montent et descendent ensemble ; ce qui doit alerter, c'est que l'un
    décroche du mouvement commun. Retourne None sous deux capteurs, où la
    notion n'a pas de sens.
    """
    if len(per_sensor) < 2:
        return None
    profiles = []
    for points in per_sensor:
        v, _age, valid = _resample(points, grid)
        if not valid.any():
            continue
        profiles.append(v - float(np.median(v[valid])))
    if len(profiles) < 2:
        return None
    return np.median(np.stack(profiles), axis=0)


# ══════════════════════════════════════════════════════════════════════════════
# Calcul des scores
# ══════════════════════════════════════════════════════════════════════════════

def _status(score: float) -> str:
    if score > THRESH_ALERT:
        return "ANOMALIE"
    if score > THRESH_WARN:
        return "ATTENTION"
    return "NORMAL"


def _fmt_age(seconds: float) -> str:
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} h"
    return f"{seconds / 86400:.1f} j"


def compute_scores(series: list[tuple[str, str]],
                   now: float | None = None) -> dict[tuple[str, str], SensorScore]:
    """Score d'anomalie de chaque capteur de `series`, une liste de couples
    (scope, field) — le scope est la pièce ou le meuble, le field la mesure
    (`temperature`, `humidity`), soit exactement la clé `sensor_<scope>_<field>`
    des séries de data/cache.db.

    Les capteurs SANS AUCUN relevé sur la fenêtre sont absents du résultat : le
    dictionnaire retourné peut donc être plus court que `series`.

    Appel SYNCHRONE : entraîne les forêts et retourne. Les appelants d'interface
    passent par get_scores(), qui ne bloque pas.
    """
    now = time.time() if now is None else now
    # Grille ancrée sur `now` — le dernier point est l'instant courant, celui
    # que l'on juge. Une grille partant du passé le ferait tomber n'importe où.
    steps = int(TRAIN_WINDOW_S // GRID_S)
    grid  = now - GRID_S * np.arange(steps, -1, -1, dtype=np.float64)
    start = float(grid[0])

    # Lecture — read_series filtre déjà les valeurs non numériques et fait le
    # CAST côté SQLite (cf. data_cache).
    raw: dict[tuple[str, str], list] = {}
    for scope, field in series:
        raw[(scope, field)] = data_cache.read_series(
            f"sensor_{scope}_{field}", since_ts=start,
        )

    out: dict[tuple[str, str], SensorScore] = {}
    by_field: dict[str, list[tuple[str, str]]] = {}
    for key, points in raw.items():
        scope, field = key
        if not points:
            # Aucun relevé du tout : capteur déclaré dans ROOMS mais jamais vu.
            # Absent du résultat plutôt que noté — il n'y a rien à en dire, et
            # son absence de la base est déjà signalée par les tags OUTDATED de
            # la page Capteurs et par le statut de la source en page Système.
            continue
        age   = now - points[-1][0]
        limit = _silence_limit(points)
        if age > limit:
            # Règle déterministe, hors forêt — cf. « Silence capteur » en en-tête.
            out[key] = SensorScore(
                scope, field, 1.0, "SILENCIEUX",
                f"aucun relevé depuis {_fmt_age(age)} — ce capteur en donne un "
                f"toutes les {_fmt_age(_typical_interval(points))} d'ordinaire",
            )
            continue
        by_field.setdefault(field, []).append(key)

    for field, keys in by_field.items():
        cross = _cross_reference([raw[k] for k in keys], grid)
        rows, tails = [], {}
        for key in keys:
            X, usable = _features(raw[key], grid, cross)
            covered = int(usable.sum())
            if covered < MIN_GRID_ROWS:
                out[key] = SensorScore(
                    key[0], field, 0.0, "APPRENTISSAGE",
                    f"{covered * GRID_S / 3600:.0f} h d'historique exploitable sur "
                    f"{TRAIN_WINDOW_S // 86400} j — il en faut "
                    f"{MIN_GRID_ROWS * GRID_S / 3600:.0f} pour juger",
                )
                continue
            rows.append(X[usable])
            tails[key] = X[-1]           # instant courant, celui qu'on juge
        if not tails:
            continue

        train = np.vstack(rows)
        forest = IsolationForest().fit(train)
        s_raw  = forest.score_samples(np.stack([tails[k] for k in tails]))
        shown  = np.clip((s_raw - RAW_TYPICAL) / (RAW_ISOLATED - RAW_TYPICAL), 0.0, 1.0)

        for key, raw_s, score in zip(tails, s_raw, shown):
            score = round(float(score), 2)
            out[key] = SensorScore(
                key[0], field, score, _status(score),
                f"score brut {float(raw_s):.3f} · forêt {field} sur "
                f"{train.shape[0]} points / {len(keys)} capteurs",
            )
    return out


# ══════════════════════════════════════════════════════════════════════════════
# Service : dernier résultat connu + recalcul en arrière-plan
# ══════════════════════════════════════════════════════════════════════════════

_lock  = threading.Lock()
_state: dict = {"ts": 0.0, "scores": None, "running": False, "gen": 0}


def get_scores(series: list[tuple[str, str]]) -> dict[tuple[str, str], SensorScore] | None:
    """Dernier jeu de scores calculé, ou None tant qu'aucun ne l'a été.

    Ne bloque JAMAIS : si le résultat a plus de REFRESH_TTL secondes, un thread
    de fond est lancé et l'ancien résultat (ou None) est servi immédiatement.
    C'est ce qui permet à update_system de tourner toutes les 8 s alors que
    l'entraînement dure plusieurs secondes sur RPi 3.
    """
    with _lock:
        stale = (time.time() - _state["ts"]) >= REFRESH_TTL
        if stale and not _state["running"]:
            _state["running"] = True
            threading.Thread(target=_refresh, args=(list(series), _state["gen"]),
                             name="anomaly-engine", daemon=True).start()
        return _state["scores"]


def _refresh(series: list[tuple[str, str]], gen: int) -> None:
    try:
        scores = compute_scores(series)
    except Exception as e:
        logger.error("anomaly_engine: calcul des scores échoué — %s", e, exc_info=True)
        scores = None
    with _lock:
        # Un reset_cache() survenu pendant le calcul invalide ce résultat : il
        # ne doit pas écraser l'état remis à zéro (ni le drapeau `running` d'un
        # éventuel thread lancé depuis).
        if gen != _state["gen"]:
            return
        if scores is not None:
            _state["scores"] = scores
        # Horodaté même en cas d'échec : sans cela, une base illisible
        # relancerait un thread à chaque tick de la page Système.
        _state["ts"]      = time.time()
        _state["running"] = False


def reset_cache() -> None:
    """Vide le résultat mémorisé — tests, et rechargement à chaud."""
    with _lock:
        _state.update(ts=0.0, scores=None, running=False, gen=_state["gen"] + 1)
