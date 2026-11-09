"""Tests unitaires pour modules/anomaly_engine.py — Isolation Forest numpy.

Trois niveaux :
  · l'algorithme lui-même (_c, IsolationForest) — propriétés du papier
  · l'extraction de variables sur des séries écrites en base
  · compute_scores / get_scores — verdicts et service non bloquant
"""
import time

import numpy as np
import pytest

from modules import anomaly_engine as ae
from modules.anomaly_engine import IsolationForest, SensorScore
from modules.data_cache import DataCache


@pytest.fixture
def dc(tmp_path):
    return DataCache(path=tmp_path / "test.db")


@pytest.fixture(autouse=True)
def _clean_cache():
    """Le résultat mémorisé est un état de module — il ne doit pas fuir d'un
    test à l'autre."""
    ae.reset_cache()
    yield
    # Laisser finir un éventuel thread de calcul lancé par le test : une fois le
    # monkeypatch défait, il lirait la vraie base.
    deadline = time.time() + 30
    while ae._state["running"] and time.time() < deadline:
        time.sleep(0.01)
    ae.reset_cache()


def _write_series(dc, scope: str, now: float, values, step: float = 600.0,
                  field: str = "temperature") -> None:
    """Écrit `values` dans history sous `sensor_<scope>_<field>`, le dernier
    point à `now` et un pas de `step` secondes en remontant."""
    n = len(values)
    dc.log_raw_many([
        (f"sensor_{scope}_{field}", now - (n - 1 - i) * step, "test", f"{v:.3f}", "°C")
        for i, v in enumerate(values)
    ])


def _calm(n: int = 1008, base: float = 21.0, seed: int = 0) -> np.ndarray:
    """7 jours de température plausible : cycle jour/nuit + bruit de capteur."""
    rng = np.random.default_rng(seed)
    t   = np.arange(n) * 600.0
    return base + 1.5 * np.sin(2 * np.pi * t / 86400) + rng.normal(0, 0.15, n)


# ── c(n) — normalisation du papier ────────────────────────────────────────────

def test_c_is_zero_for_single_node():
    assert ae._c(1) == 0.0
    assert ae._c(0) == 0.0


def test_c_matches_reference_values():
    # c(n) = 2·(ln(n−1) + γ) − 2·(n−1)/n
    assert ae._c(2) == pytest.approx(2 * ae._EULER - 1.0, abs=1e-12)
    assert ae._c(256) == pytest.approx(10.244, abs=1e-3)


def test_c_accepts_arrays():
    out = ae._c(np.array([1, 2, 256]))
    assert out.shape == (3,)
    assert out[0] == 0.0
    assert out[2] == pytest.approx(ae._c(256))


# ── IsolationForest ───────────────────────────────────────────────────────────

def test_score_samples_before_fit_raises():
    with pytest.raises(RuntimeError):
        IsolationForest().score_samples(np.zeros((1, 5)))


def test_scores_stay_in_unit_interval():
    X = np.random.default_rng(1).normal(size=(300, 5))
    s = IsolationForest().fit(X).score_samples(X)
    assert s.shape == (300,)
    assert np.all((s > 0.0) & (s < 1.0))


def test_outlier_scores_above_inliers():
    rng = np.random.default_rng(2)
    X = rng.normal(size=(400, 5))
    X[0] = 15.0                       # point franchement isolé
    s = IsolationForest().fit(X).score_samples(X)
    assert s[0] > s[1:].max()
    assert s[0] > 0.7                 # bien au-dessus du point « typique » (0,5)


def test_typical_point_scores_near_one_half():
    X = np.random.default_rng(3).normal(size=(400, 5))
    s = IsolationForest().fit(X).score_samples(X)
    assert np.median(s) == pytest.approx(0.5, abs=0.08)


def test_constant_column_does_not_break_growth():
    """Une variable constante ne peut pas séparer : l'arbre doit se fermer en
    feuille plutôt que produire une coupe dégénérée."""
    X = np.column_stack([np.random.default_rng(4).normal(size=200), np.ones(200)])
    s = IsolationForest().fit(X).score_samples(X)
    assert np.all(np.isfinite(s))


def test_all_identical_rows_are_all_typical():
    X = np.ones((100, 5))
    s = IsolationForest().fit(X).score_samples(X)
    assert np.allclose(s, s[0])


def test_seed_makes_scores_reproducible():
    X = np.random.default_rng(5).normal(size=(200, 5))
    a = IsolationForest(seed=7).fit(X).score_samples(X)
    b = IsolationForest(seed=7).fit(X).score_samples(X)
    assert np.array_equal(a, b)


def test_fit_handles_fewer_rows_than_max_samples():
    X = np.random.default_rng(6).normal(size=(10, 5))
    s = IsolationForest(max_samples=256).fit(X).score_samples(X)
    assert s.shape == (10,) and np.all(np.isfinite(s))


# ── compute_scores — verdicts ─────────────────────────────────────────────────

def test_short_history_is_not_judged(dc, monkeypatch):
    now = time.time()
    _write_series(dc, "salon", now, _calm(10))
    monkeypatch.setattr(ae, "data_cache", dc)
    res = ae.compute_scores([("salon", "temperature")], now=now)
    assert res[("salon", "temperature")].status == "APPRENTISSAGE"
    assert res[("salon", "temperature")].score == 0.0


def test_series_without_any_reading_is_omitted(dc, monkeypatch):
    """Capteur déclaré mais jamais vu : absent du résultat, pas noté à 0."""
    monkeypatch.setattr(ae, "data_cache", dc)
    assert ae.compute_scores([("fantome", "temperature")], now=time.time()) == {}


def test_bursty_sensor_is_not_judged(dc, monkeypatch):
    """Beaucoup de relevés, mais tous groupés sur quelques heures : la fenêtre
    de 7 j n'est presque pas couverte, le capteur n'est pas jugeable. C'est le
    cas qui rendait un simple comptage de relevés trompeur."""
    now = time.time()
    _write_series(dc, "salon", now, _calm(300, seed=50), step=30.0)  # 300 pts sur 2,5 h
    monkeypatch.setattr(ae, "data_cache", dc)
    score = ae.compute_scores([("salon", "temperature")], now=now)[("salon", "temperature")]
    assert score.status == "APPRENTISSAGE"


def test_stale_grid_rows_are_excluded_from_training(dc, monkeypatch):
    """Une longue coupure au milieu de la fenêtre ne doit pas peupler
    l'entraînement de valeurs figées."""
    now  = time.time()
    grid = now - ae.GRID_S * np.arange(672, -1, -1, dtype=np.float64)
    # Relevés sur la première et la dernière journée seulement.
    pts = ([(float(t), 20.0 + 0.1 * (i % 5)) for i, t in enumerate(grid[:96])]
           + [(float(t), 20.0 + 0.1 * (i % 5)) for i, t in enumerate(grid[-96:])])
    _, usable = ae._features(pts, grid)
    assert usable.sum() <= 2 * 96          # jamais les 673 pas de la grille
    assert usable[-1]                      # l'instant courant reste exploitable


def test_silent_sensor_is_flagged_by_rule(dc, monkeypatch):
    """Capteur muet depuis 6 h : signalé par la règle déterministe, pas par la
    forêt — d'où un score plat de 1,0 et un détail qui cite l'ancienneté."""
    now = time.time()
    _write_series(dc, "salon", now - 6 * 3600, _calm())
    monkeypatch.setattr(ae, "data_cache", dc)
    score = ae.compute_scores([("salon", "temperature")], now=now)[("salon", "temperature")]
    assert score.status == "SILENCIEUX"
    assert score.score == 1.0
    assert "6.0 h" in score.detail


def test_silence_threshold_follows_the_sensor_rhythm(dc, monkeypatch):
    """Un capteur lent (relevé toutes les 45 min, type SNZB-02LD de frigo) ne
    doit pas être déclaré muet pour deux battements manqués : le seuil suit son
    rythme habituel, et non une heure fixe."""
    now = time.time()
    _write_series(dc, "frigo", now - 5400, _calm(200, base=4.0), step=2700.0)
    monkeypatch.setattr(ae, "data_cache", dc)
    score = ae.compute_scores([("frigo", "temperature")], now=now)[("frigo", "temperature")]
    assert score.status != "SILENCIEUX"        # 1,5 h < 4 × 45 min
    # Au-delà de quatre intervalles, en revanche, il l'est bien.
    score = ae.compute_scores([("frigo", "temperature")],
                              now=now + 3 * 3600)[("frigo", "temperature")]
    assert score.status == "SILENCIEUX"


def test_fast_sensor_keeps_the_one_hour_floor(dc, monkeypatch):
    """À l'inverse, un capteur bavard ne déclenche pas au bout de 4 × 2 min :
    SILENT_AGE_S sert de plancher."""
    now = time.time()
    _write_series(dc, "salon", now - 1800, _calm(400), step=120.0)
    monkeypatch.setattr(ae, "data_cache", dc)
    score = ae.compute_scores([("salon", "temperature")], now=now)[("salon", "temperature")]
    assert score.status != "SILENCIEUX"        # 30 min < plancher d'une heure


def test_recent_sensor_is_not_silent(dc, monkeypatch):
    now = time.time()
    _write_series(dc, "salon", now - 300, _calm())
    monkeypatch.setattr(ae, "data_cache", dc)
    score = ae.compute_scores([("salon", "temperature")], now=now)[("salon", "temperature")]
    assert score.status != "SILENCIEUX"


def test_calm_house_reports_no_anomaly(dc, monkeypatch):
    now = time.time()
    for i, room in enumerate(("salon", "cuisine", "bureau")):
        _write_series(dc, room, now, _calm(base=20.0 + i, seed=i))
    monkeypatch.setattr(ae, "data_cache", dc)
    res = ae.compute_scores(
        [(r, "temperature") for r in ("salon", "cuisine", "bureau")], now=now)
    assert all(s.status == "NORMAL" for s in res.values())


def test_temperature_spike_is_detected(dc, monkeypatch):
    """Un saut de +5 °C sur le dernier relevé doit sortir le capteur concerné,
    et lui seul."""
    now  = time.time()
    vals = {}
    for i, room in enumerate(("salon", "cuisine", "bureau")):
        vals[room] = _calm(base=20.0 + i, seed=i)
    vals["salon"][-1] += 5.0
    for room, v in vals.items():
        _write_series(dc, room, now, v)
    monkeypatch.setattr(ae, "data_cache", dc)
    res = ae.compute_scores([(r, "temperature") for r in vals], now=now)
    assert res[("salon", "temperature")].status == "ANOMALIE"
    assert res[("cuisine", "temperature")].status == "NORMAL"
    assert res[("bureau", "temperature")].status == "NORMAL"


def test_spike_scores_higher_than_calm(dc, tmp_path, monkeypatch):
    """Comparaison directe : la même série, avec et sans pic."""
    now  = time.time()
    base = _calm(seed=11)
    monkeypatch.setattr(ae, "data_cache", dc)
    _write_series(dc, "calme", now, base)
    spiked = base.copy()
    spiked[-1] += 4.0
    _write_series(dc, "pic", now, spiked)
    res = ae.compute_scores([("calme", "temperature"), ("pic", "temperature")], now=now)
    assert res[("pic", "temperature")].score > res[("calme", "temperature")].score


def test_fields_are_scored_by_separate_forests(dc, monkeypatch):
    now = time.time()
    _write_series(dc, "salon", now, _calm(seed=20))
    _write_series(dc, "salon", now, _calm(base=55.0, seed=21), field="humidity")
    monkeypatch.setattr(ae, "data_cache", dc)
    res = ae.compute_scores([("salon", "temperature"), ("salon", "humidity")], now=now)
    assert set(res) == {("salon", "temperature"), ("salon", "humidity")}
    assert "forêt temperature" in res[("salon", "temperature")].detail
    assert "forêt humidity"    in res[("salon", "humidity")].detail


def test_score_is_reproducible(dc, monkeypatch):
    now = time.time()
    _write_series(dc, "salon", now, _calm(seed=30))
    _write_series(dc, "cuisine", now, _calm(base=19.0, seed=31))
    monkeypatch.setattr(ae, "data_cache", dc)
    keys = [("salon", "temperature"), ("cuisine", "temperature")]
    a = ae.compute_scores(keys, now=now)
    b = ae.compute_scores(keys, now=now)
    assert {k: v.score for k, v in a.items()} == {k: v.score for k, v in b.items()}


def test_unreadable_database_does_not_raise(dc, monkeypatch):
    """read_series journalise et retourne [] sur erreur : le moteur doit rendre
    un résultat vide, pas propager."""
    monkeypatch.setattr(ae, "data_cache", dc)
    monkeypatch.setattr(dc, "read_series", lambda *a, **k: [])
    assert ae.compute_scores([("salon", "temperature")], now=time.time()) == {}


# ── Extraction de variables ───────────────────────────────────────────────────

def test_resample_holds_last_known_value():
    pts  = [(0.0, 10.0), (100.0, 20.0)]
    grid = np.array([-10.0, 0.0, 50.0, 100.0, 150.0])
    v, age, valid = ae._resample(pts, grid)
    assert not valid[0]                        # avant le premier relevé
    assert age[0] == 0.0                       # et non un âge négatif
    assert list(v[1:]) == [10.0, 10.0, 20.0, 20.0]
    assert list(age[1:]) == [0.0, 50.0, 0.0, 50.0]


def test_norm_returns_zeros_on_constant_series():
    """IQR nul : rien à isoler, donc zéro — surtout pas un infini."""
    assert np.all(ae._norm(np.zeros(50)) == 0.0)


def test_rate_features_are_muted_across_a_silence():
    """Au sortir d'une coupure, v[i] − v[i−1] enjambe le trou : ce n'est pas une
    variation rapide. d1 et d3h doivent être nuls sur cette ligne, `gap` non."""
    grid = 1_000_000.0 - ae.GRID_S * np.arange(200, -1, -1, dtype=np.float64)
    # 20 °C avant la coupure, 26 °C après — six heures de silence au milieu.
    pts = ([(float(t), 20.0) for t in grid[:100]]
           + [(float(t), 26.0) for t in grid[124:]])
    X, usable = ae._features(pts, grid)
    resume = 124                       # premier pas après la reprise
    assert X[resume, 1] == 0.0         # d1  : pas de faux pic de +6 °C
    assert X[resume, 2] == 0.0         # d3h : idem
    assert X[resume, 3] == 0.0         # gap : le relevé y est frais, justement
    # Le silence se lit sur les pas QUI SUIVENT le dernier relevé : `gap` y monte
    # tant que la ligne reste exploitable, puis la ligne sort de l'entraînement.
    assert usable[100] and X[100, 3] > 0.0
    assert not usable[110]


def test_cross_reference_needs_two_sensors():
    grid = np.arange(0.0, 1000.0, 100.0)
    pts  = [(0.0, 20.0), (500.0, 21.0)]
    assert ae._cross_reference([pts], grid) is None
    assert ae._cross_reference([pts, pts], grid) is not None


def test_cross_reference_is_level_independent():
    """Deux pièces au même profil mais 5 °C d'écart donnent une référence
    identique : c'est la forme qui est comparée, pas le niveau."""
    grid = np.arange(0.0, 10_000.0, 100.0)
    a = [(float(t), 20.0 + 0.5 * np.sin(t / 500)) for t in range(0, 10_000, 100)]
    b = [(float(t), 25.0 + 0.5 * np.sin(t / 500)) for t in range(0, 10_000, 100)]
    ref_ab = ae._cross_reference([a, b], grid)
    ref_aa = ae._cross_reference([a, a], grid)
    assert np.allclose(ref_ab, ref_aa)


def test_features_mask_grid_before_first_reading():
    now  = 1_000_000.0
    grid = now - ae.GRID_S * np.arange(100, -1, -1, dtype=np.float64)
    # Série ne couvrant que la seconde moitié de la grille
    pts  = [(float(t), 20.0 + (i % 3)) for i, t in enumerate(grid[50:])]
    X, usable = ae._features(pts, grid)
    assert X.shape == (101, 5)
    assert not usable[:50].any()
    assert usable[50 + ae.DRIFT_STEPS:].any()
    assert np.all(np.isfinite(X))


# ── get_scores — service non bloquant ─────────────────────────────────────────

def test_get_scores_returns_none_before_first_computation(dc, monkeypatch):
    monkeypatch.setattr(ae, "data_cache", dc)
    assert ae.get_scores([("salon", "temperature")]) is None


def test_get_scores_serves_result_once_computed(dc, monkeypatch):
    now = time.time()
    _write_series(dc, "salon", now, _calm(seed=40))
    monkeypatch.setattr(ae, "data_cache", dc)
    assert ae.get_scores([("salon", "temperature")]) is None   # lance le thread
    deadline = time.time() + 30
    while ae.get_scores([("salon", "temperature")]) is None and time.time() < deadline:
        time.sleep(0.05)
    res = ae.get_scores([("salon", "temperature")])
    assert res is not None
    assert isinstance(res[("salon", "temperature")], SensorScore)


def test_get_scores_does_not_recompute_within_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(ae, "compute_scores", lambda s, **k: calls.append(1) or {})
    ae.get_scores([("salon", "temperature")])
    deadline = time.time() + 5
    while ae._state["running"] and time.time() < deadline:
        time.sleep(0.01)
    for _ in range(5):
        ae.get_scores([("salon", "temperature")])
    assert len(calls) == 1


def test_failed_computation_is_timestamped(monkeypatch):
    """Un échec doit tout de même horodater : sans cela, une base illisible
    relancerait un thread à chaque tick de la page Système."""
    def boom(*_a, **_k):
        raise RuntimeError("base illisible")
    monkeypatch.setattr(ae, "compute_scores", boom)
    ae.get_scores([("salon", "temperature")])
    deadline = time.time() + 5
    while ae._state["running"] and time.time() < deadline:
        time.sleep(0.01)
    assert ae._state["ts"] > 0.0
    assert ae._state["scores"] is None
