"""Tests unitaires pour modules/comfort_engine.py.

Couvre la logique pure (pas de checkpoint requis) :
  · model_status() selon présence des fichiers .pt
  · helpers schedule : _n_blocks, _random_schedule, _schedule_to_steps
  · confort : _room_bounds, _comfort_cost, _block_reasons
  · offsets  : _compute_window_offsets
  · solaire  : _solar_position, _compute_solar_features
  · pipeline : _parse_weather_response, _make_house_state_df, _build_feature_table
  · format   : _schedule_to_plan
  · API      : _plan_to_room_plans, RoomPlan
"""
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest

import modules.comfort_engine as ce
from modules.comfort_engine import (
    COMFORT_TEMP_MAX,
    COMFORT_TEMP_MIN,
    HOUSE_STATE_TYPES,
    PLANNING_BLOCK_HOURS,
    PLANNING_HORIZON_HOURS,
    REASON_COOL,
    REASON_MAINTAIN,
    REASON_WARM,
    STATUS_FULL,
    STATUS_LIMITED,
    STATUS_NONE,
    RoomPlan,
    SEP,
    _block_reasons,
    _build_feature_table,
    _comfort_cost,
    _compute_solar_features,
    _compute_window_offsets,
    _make_house_state_df,
    _n_blocks,
    _parse_weather_response,
    _plan_to_room_plans,
    _random_schedule,
    _resolution_segments_for,
    _room_bounds,
    _schedule_to_plan,
    _schedule_to_steps,
    _solar_position,
    model_status,
    run_inference,
)


# ── Utilitaire : réponse OpenMeteo minimale ───────────────────────────────────

def _make_openmeteo_response(n_past: int = 4, n_future: int = 4) -> dict:
    """Réponse OpenMeteo synthétique avec n_past heures passées + n_future futures."""
    now = pd.Timestamp.utcnow().floor("h")
    times = pd.date_range(now - pd.Timedelta(hours=n_past), periods=n_past + n_future, freq="1h")
    return {
        "hourly": {
            "time":               times.strftime("%Y-%m-%dT%H:%M").tolist(),
            "temperature_2m":     [15.0 + i for i in range(n_past + n_future)],
            "shortwave_radiation": [max(0.0, 300.0 * (i - n_past + 1)) for i in range(n_past + n_future)],
            "cloud_cover":        [50.0] * (n_past + n_future),
        }
    }


# ── model_status ──────────────────────────────────────────────────────────────

def test_model_status_none_empty_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "MODELS_DIR", tmp_path)
    assert model_status() == STATUS_NONE


def test_model_status_limited(tmp_path, monkeypatch):
    (tmp_path / "limited.onnx").touch()
    monkeypatch.setattr(ce, "MODELS_DIR", tmp_path)
    assert model_status() == STATUS_LIMITED


def test_model_status_full_takes_priority(tmp_path, monkeypatch):
    (tmp_path / "limited.onnx").touch()
    (tmp_path / "full.onnx").touch()
    monkeypatch.setattr(ce, "MODELS_DIR", tmp_path)
    assert model_status() == STATUS_FULL


def test_run_inference_returns_error_when_no_model(tmp_path, monkeypatch):
    monkeypatch.setattr(ce, "MODELS_DIR", tmp_path)
    result = run_inference({})
    assert result["status"] == "error"
    assert result["rooms"] == []
    assert "modèle" in result["error"].lower()


# ── _n_blocks ─────────────────────────────────────────────────────────────────

def test_n_blocks_standard():
    assert _n_blocks(24.0, 2.0) == 12


def test_n_blocks_fractional_rounds():
    assert _n_blocks(5.0, 2.0) == 2    # round(2.5) = 2


def test_n_blocks_one_block():
    assert _n_blocks(2.0, 2.0) == 1


# ── _room_bounds ──────────────────────────────────────────────────────────────

def test_room_bounds_defaults_when_no_ranges():
    lo, hi = _room_bounds("salon", None)
    assert lo == COMFORT_TEMP_MIN
    assert hi == COMFORT_TEMP_MAX


def test_room_bounds_custom_range():
    lo, hi = _room_bounds("salon", {"salon": (18.0, 22.0)})
    assert lo == 18.0 and hi == 22.0


def test_room_bounds_missing_room_falls_back_to_global():
    lo, hi = _room_bounds("bureau", {"salon": (18.0, 22.0)})
    assert lo == COMFORT_TEMP_MIN


# ── _comfort_cost ─────────────────────────────────────────────────────────────

def test_comfort_cost_zero_in_range():
    temps = np.array([[21.0, 22.0]])
    assert _comfort_cost(temps) == 0.0


def test_comfort_cost_below_min():
    temps = np.array([[15.0]])
    assert _comfort_cost(temps) == pytest.approx(COMFORT_TEMP_MIN - 15.0)


def test_comfort_cost_above_max():
    temps = np.array([[30.0]])
    assert _comfort_cost(temps) == pytest.approx(30.0 - COMFORT_TEMP_MAX)


def test_comfort_cost_custom_ranges_per_room():
    temps         = np.array([[25.0, 17.0]])
    rooms         = ["salon", "bureau"]
    comfort_ranges = {"salon": (19.0, 24.0), "bureau": (18.0, 22.0)}
    cost = _comfort_cost(temps, rooms, comfort_ranges)
    assert cost == pytest.approx(1.0 + 1.0)   # salon +1°C, bureau -1°C


def test_comfort_cost_symmetric():
    temps = np.array([[COMFORT_TEMP_MIN - 2.0, COMFORT_TEMP_MAX + 2.0]])
    assert _comfort_cost(temps) == pytest.approx(4.0)


# ── _block_reasons ────────────────────────────────────────────────────────────

def _make_eval_rows(n: int, now_idx: int = 0) -> np.ndarray:
    """n eval rows espacées de 15 pas (30 min à 2 min/pas)."""
    return np.array([now_idx + 15 * (k + 1) for k in range(n)])


def test_block_reasons_maintain():
    temps     = {"salon": np.full(2, 21.0)}
    eval_rows = _make_eval_rows(2)
    reasons   = _block_reasons(temps, eval_rows, now_idx=0, n_blocks=1, block_hours=1.0)
    assert reasons["salon"] == [REASON_MAINTAIN]


def test_block_reasons_warm():
    temps     = {"salon": np.full(2, 15.0)}
    eval_rows = _make_eval_rows(2)
    reasons   = _block_reasons(temps, eval_rows, now_idx=0, n_blocks=1, block_hours=1.0)
    assert reasons["salon"] == [REASON_WARM]


def test_block_reasons_cool():
    temps     = {"salon": np.full(2, 30.0)}
    eval_rows = _make_eval_rows(2)
    reasons   = _block_reasons(temps, eval_rows, now_idx=0, n_blocks=1, block_hours=1.0)
    assert reasons["salon"] == [REASON_COOL]


def test_block_reasons_multiple_blocks():
    # Block 0 : chaud  → row 10  = 20 min depuis now (< 60 → bloc 0)
    # Block 1 : normal → rows 40,55 = 80,110 min depuis now (> 60 → bloc 1)
    temps     = {"salon": np.array([30.0, 21.0, 21.0])}
    eval_rows = np.array([10, 40, 55])
    reasons   = _block_reasons(temps, eval_rows, now_idx=0, n_blocks=2, block_hours=1.0)
    assert reasons["salon"][0] == REASON_COOL
    assert reasons["salon"][1] == REASON_MAINTAIN


def test_block_reasons_custom_comfort_ranges():
    temps          = {"salon": np.full(2, 23.0)}
    eval_rows      = _make_eval_rows(2)
    comfort_ranges = {"salon": (24.0, 28.0)}
    reasons        = _block_reasons(temps, eval_rows, now_idx=0, n_blocks=1, block_hours=1.0,
                                    comfort_ranges=comfort_ranges)
    assert reasons["salon"] == [REASON_WARM]


# ── _compute_window_offsets ───────────────────────────────────────────────────

def test_window_offsets_last_is_zero():
    offsets = _compute_window_offsets()
    assert offsets[-1] == 0


def test_window_offsets_sorted_descending():
    offsets = _compute_window_offsets()
    assert all(offsets[i] >= offsets[i + 1] for i in range(len(offsets) - 1))


def test_window_offsets_default_count():
    # 60 pts à 2min (0-2h) + 24 pts à 10min (2-6h) + 24 pts à 30min (6-18h) = 108
    offsets = _compute_window_offsets()
    assert len(offsets) == 108


def test_window_offsets_custom_segments():
    segs    = [{"duration_minutes": 60, "resolution_minutes": 2}]   # 30 points
    offsets = _compute_window_offsets(segs)
    assert len(offsets) == 30
    assert offsets[-1] == 0


def test_resolution_segments_for_truncates():
    segs = _resolution_segments_for(2.0)   # 2 h → seulement le 1er segment (0-2h)
    assert len(segs) == 1
    assert segs[0]["duration_minutes"] == 120


# ── _random_schedule / _schedule_to_steps ────────────────────────────────────

def test_random_schedule_shape():
    rng      = np.random.default_rng(0)
    rooms    = ["salon", "bureau"]
    schedule = _random_schedule(rooms, HOUSE_STATE_TYPES, 4.0, 2.0, rng)
    assert set(schedule.keys()) == {("salon", "shutter"), ("salon", "window"),
                                    ("bureau", "shutter"), ("bureau", "window")}
    for arr in schedule.values():
        assert len(arr) == _n_blocks(4.0, 2.0)
        assert set(np.unique(arr)).issubset({0.0, 1.0})


def test_schedule_to_steps_length():
    rng      = np.random.default_rng(1)
    schedule = _random_schedule(["salon"], HOUSE_STATE_TYPES, 4.0, 2.0, rng)
    steps    = _schedule_to_steps(schedule, n_steps=120, block_hours=2.0)
    for arr in steps.values():
        assert len(arr) == 120


def test_schedule_to_steps_values_repeated():
    schedule = {("salon", "shutter"): np.array([1.0, 0.0], dtype=np.float32)}
    spb      = int(round(2.0 * 60 / 2))   # 60 pas par bloc de 2h
    steps    = _schedule_to_steps(schedule, n_steps=120, block_hours=2.0)
    arr      = steps[("salon", "shutter")]
    assert np.all(arr[:spb] == 1.0)
    assert np.all(arr[spb:] == 0.0)


def test_schedule_to_steps_pads_when_short():
    schedule = {("salon", "shutter"): np.array([1.0], dtype=np.float32)}
    steps    = _schedule_to_steps(schedule, n_steps=200, block_hours=2.0)
    arr      = steps[("salon", "shutter")]
    assert len(arr) == 200
    assert arr[-1] == 1.0   # padded avec la dernière valeur


# ── _solar_position ───────────────────────────────────────────────────────────

def test_solar_elevation_in_valid_range():
    ts  = pd.date_range("2026-06-21 06:00", "2026-06-21 20:00", freq="1h", tz="UTC")
    pos = _solar_position(ts, latitude=45.55, longitude=6.22)
    assert (pos["elevation_deg"] >= -90).all()
    assert (pos["elevation_deg"] <= 90).all()


def test_solar_noon_positive_elevation():
    ts  = pd.DatetimeIndex(["2026-06-21T12:00:00+00:00"])
    pos = _solar_position(ts, latitude=45.55, longitude=6.22)
    assert pos["elevation_deg"].iloc[0] > 0


def test_solar_midnight_negative_elevation():
    ts  = pd.DatetimeIndex(["2026-06-21T00:00:00+00:00"])
    pos = _solar_position(ts, latitude=45.55, longitude=6.22)
    assert pos["elevation_deg"].iloc[0] < 0


def test_compute_solar_features_columns():
    ts   = pd.date_range("2026-06-21 10:00", periods=4, freq="2min", tz="UTC")
    feat = _compute_solar_features(ts)
    assert f"solar{SEP}elevation"              in feat.columns
    assert f"solar{SEP}azimuth"               in feat.columns
    assert f"solar{SEP}face_exposure{SEP}S"   in feat.columns
    assert len(feat) == 4


# ── _parse_weather_response ───────────────────────────────────────────────────

def test_parse_weather_response_columns():
    raw = _make_openmeteo_response()
    df  = _parse_weather_response(raw)
    assert f"weather{SEP}outdoor_temperature" in df.columns
    assert f"weather{SEP}solar_irradiance"    in df.columns
    assert f"weather{SEP}cloud_cover"         in df.columns
    assert f"weather{SEP}kind"               in df.columns


def test_parse_weather_response_kind_observed_past():
    raw = _make_openmeteo_response(n_past=4, n_future=0)
    df  = _parse_weather_response(raw)
    assert (df[f"weather{SEP}kind"] == "observed").all()


def test_parse_weather_response_kind_forecast_future():
    raw = _make_openmeteo_response(n_past=0, n_future=4)
    df  = _parse_weather_response(raw)
    # Le 1er point tombe sur l'heure courante (≤ now → observed) ;
    # les suivants sont strictement futurs → forecast.
    assert (df[f"weather{SEP}kind"].iloc[1:] == "forecast").all()


def test_parse_weather_response_index_is_utc():
    raw = _make_openmeteo_response()
    df  = _parse_weather_response(raw)
    assert df.index.tz is not None
    assert str(df.index.tz) == "UTC"


def test_parse_weather_response_length():
    n_past, n_future = 6, 8
    raw = _make_openmeteo_response(n_past, n_future)
    df  = _parse_weather_response(raw)
    assert len(df) == n_past + n_future


# ── _fetch_weather_for_inference (avec cache) ─────────────────────────────────

def test_fetch_weather_uses_cache_when_fresh(monkeypatch):
    raw = _make_openmeteo_response()
    cached = {"value": raw, "updated_at": time.time()}   # frais : < TTL

    mock_dc = MagicMock()
    mock_dc.read.return_value = cached
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    df = ce._fetch_weather_for_inference()
    mock_dc.read.assert_called_once_with("weather.inference")
    mock_dc.write.assert_not_called()                     # pas de refetch
    assert not df.empty


def test_fetch_weather_refetches_when_stale(monkeypatch):
    raw = _make_openmeteo_response()
    stale = {"value": raw, "updated_at": time.time() - 700}   # périmé (> 600 s)

    mock_dc = MagicMock()
    mock_dc.read.return_value = stale

    mock_resp = MagicMock()
    mock_resp.json.return_value = _make_openmeteo_response()
    mock_resp.raise_for_status = MagicMock()

    monkeypatch.setattr(ce, "data_cache", mock_dc)
    with patch("modules.comfort_engine.requests.get", return_value=mock_resp) as mock_get:
        df = ce._fetch_weather_for_inference()
        mock_get.assert_called_once()
        mock_dc.write.assert_called_once_with("weather.inference", mock_resp.json.return_value)
    assert not df.empty


def test_fetch_weather_uses_stale_cache_on_network_error(monkeypatch):
    """Si OpenMeteo est injoignable, on utilise le cache même périmé."""
    import requests as req_module
    raw = _make_openmeteo_response()
    stale = {"value": raw, "updated_at": time.time() - 700}

    mock_dc = MagicMock()
    mock_dc.read.return_value = stale
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    with patch("modules.comfort_engine.requests.get", side_effect=req_module.RequestException("timeout")):
        df = ce._fetch_weather_for_inference()
    assert not df.empty   # retourne le stale cache, ne lève pas


def test_fetch_weather_raises_when_no_cache_and_network_error(monkeypatch):
    import requests as req_module
    mock_dc = MagicMock()
    mock_dc.read.return_value = None   # aucun cache
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    with patch("modules.comfort_engine.requests.get", side_effect=req_module.RequestException("fail")):
        with pytest.raises(ValueError, match="météo"):
            ce._fetch_weather_for_inference()


# ── _make_house_state_df ──────────────────────────────────────────────────────

def test_make_house_state_df_all_zeros(monkeypatch):
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", ["salon", "bureau"])
    grid = pd.date_range("2026-06-21 10:00", periods=10, freq="2min", tz="UTC")
    df   = _make_house_state_df(grid)
    assert (df == 0.0).all().all()


def test_make_house_state_df_columns(monkeypatch):
    rooms = ["salon", "bureau"]
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", rooms)
    grid = pd.date_range("2026-06-21 10:00", periods=5, freq="2min", tz="UTC")
    df   = _make_house_state_df(grid)
    for room in rooms:
        assert f"house{SEP}{room}{SEP}shutter" in df.columns
        assert f"house{SEP}{room}{SEP}window"  in df.columns


def test_make_house_state_df_index_matches_grid(monkeypatch):
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", ["salon"])
    grid = pd.date_range("2026-06-21 10:00", periods=8, freq="2min", tz="UTC")
    df   = _make_house_state_df(grid)
    assert len(df) == 8
    assert df.index.equals(grid)


# ── _build_feature_table ──────────────────────────────────────────────────────

def test_build_feature_table_columns(monkeypatch):
    """Vérifie que toutes les familles de colonnes attendues sont présentes."""
    rooms = ["salon"]
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", rooms)

    raw = _make_openmeteo_response(n_past=4, n_future=4)
    mock_dc = MagicMock()
    mock_dc.read.return_value = {"value": raw, "updated_at": time.time()}
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    table = _build_feature_table()

    assert f"weather{SEP}outdoor_temperature" in table.columns
    assert f"weather{SEP}solar_irradiance"    in table.columns
    assert f"weather{SEP}cloud_cover"         in table.columns
    assert f"weather{SEP}kind"               in table.columns
    assert f"solar{SEP}elevation"             in table.columns
    assert f"house{SEP}salon{SEP}shutter"     in table.columns
    assert f"house{SEP}salon{SEP}window"      in table.columns


def test_build_feature_table_no_nan(monkeypatch):
    """Après interpolation et ffill/bfill, aucune valeur NaN ne doit subsister."""
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", ["salon"])

    raw = _make_openmeteo_response(n_past=4, n_future=4)
    mock_dc = MagicMock()
    mock_dc.read.return_value = {"value": raw, "updated_at": time.time()}
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    table = _build_feature_table()
    numeric_cols = table.select_dtypes(include="number")
    assert not numeric_cols.isnull().any().any()


def test_build_feature_table_house_state_zero(monkeypatch):
    """Les colonnes house__* doivent être toutes à 0 (défaut fermé)."""
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", ["salon"])

    raw = _make_openmeteo_response(n_past=4, n_future=4)
    mock_dc = MagicMock()
    mock_dc.read.return_value = {"value": raw, "updated_at": time.time()}
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    table = _build_feature_table()
    house_cols = [c for c in table.columns if c.startswith("house")]
    assert house_cols, "Aucune colonne house__* trouvée"
    assert (table[house_cols] == 0.0).all().all()


def test_build_feature_table_2min_resolution(monkeypatch):
    """La grille doit être à 2 min de résolution."""
    monkeypatch.setattr(ce, "_DEFAULT_ROOMS", ["salon"])

    raw = _make_openmeteo_response(n_past=2, n_future=2)
    mock_dc = MagicMock()
    mock_dc.read.return_value = {"value": raw, "updated_at": time.time()}
    monkeypatch.setattr(ce, "data_cache", mock_dc)

    table = _build_feature_table()
    diffs = pd.Series(table.index).diff().dropna()
    assert (diffs == pd.Timedelta(minutes=2)).all()


# ── _schedule_to_plan ─────────────────────────────────────────────────────────

def _make_constant_schedule(rooms, state_types, horizon_hours, block_hours, value=1) -> dict:
    n = _n_blocks(horizon_hours, block_hours)
    return {(room, stype): np.full(n, value, dtype=np.float32)
            for room in rooms for stype in state_types}


def test_schedule_to_plan_structure():
    now      = pd.Timestamp("2026-06-21T10:00:00+00:00")
    schedule = _make_constant_schedule(["salon"], ["shutter", "window"], 4.0, 2.0)
    result   = _schedule_to_plan(schedule, ["salon"], ["shutter", "window"], now, 4.0, 2.0)
    assert "rooms" in result and "salon" in result["rooms"]
    for interval in result["rooms"]["salon"]:
        assert "from" in interval and "to" in interval
        assert interval["shutter"] in ("open", "closed")
        assert interval["window"]  in ("open", "closed")


def test_schedule_to_plan_merges_identical_blocks():
    now      = pd.Timestamp("2026-06-21T10:00:00+00:00")
    # Tous les blocs identiques → un seul intervalle fusionné
    schedule = _make_constant_schedule(["salon"], ["shutter", "window"], 6.0, 2.0, value=1)
    result   = _schedule_to_plan(schedule, ["salon"], ["shutter", "window"], now, 6.0, 2.0,
                                  reasons={"salon": ["maintenir"] * 3})
    assert len(result["rooms"]["salon"]) == 1


def test_schedule_to_plan_reason_field():
    now      = pd.Timestamp("2026-06-21T10:00:00+00:00")
    schedule = _make_constant_schedule(["salon"], ["shutter"], 2.0, 2.0)
    result   = _schedule_to_plan(schedule, ["salon"], ["shutter"], now, 2.0, 2.0,
                                  reasons={"salon": ["refroidir"]})
    assert result["rooms"]["salon"][0]["reason"] == "refroidir"


# ── _plan_to_room_plans ───────────────────────────────────────────────────────

def _make_plan_dict(room_id: str, shutter: str, window: str, reason: str, until: str) -> dict:
    return {
        "generated_at": "2026-06-21T10:00:00+00:00",
        "horizon_hours": 2.0,
        "rooms": {
            room_id: [{
                "from": "2026-06-21T10:00:00+00:00",
                "to":   f"2026-06-21T{until}:00+00:00",
                "shutter": shutter, "window": window, "reason": reason,
            }]
        },
        "best_cost": 0.0, "n_candidates": 10,
    }


def test_plan_to_room_plans_basic():
    plan   = _make_plan_dict("salon", "open", "closed", "refroidir", "12:00")
    result = _plan_to_room_plans(plan)
    assert len(result) == 1
    rp = result[0]
    assert isinstance(rp, RoomPlan)
    assert rp.room_id == "salon"
    assert "VOLET OUVERT"   in rp.actions
    assert "FENÊTRE FERMÉE" in rp.actions
    assert "REFROIDIR"      in rp.actions
    assert rp.until == "12:00"


def test_plan_to_room_plans_warm():
    plan   = _make_plan_dict("bureau", "closed", "closed", "rechauffer", "14:00")
    result = _plan_to_room_plans(plan)
    rp = result[0]
    assert "VOLET FERMÉ"    in rp.actions
    assert "FENÊTRE FERMÉE" in rp.actions
    assert "CHAUFFER"       in rp.actions


def test_plan_to_room_plans_empty_intervals():
    plan = {
        "generated_at": "2026-06-21T10:00:00+00:00",
        "horizon_hours": 2.0,
        "rooms": {"salon": []},
        "best_cost": 0.0, "n_candidates": 10,
    }
    assert _plan_to_room_plans(plan) == []


def test_plan_to_room_plans_multiple_rooms():
    plan = {
        "generated_at": "2026-06-21T10:00:00+00:00",
        "horizon_hours": 2.0,
        "rooms": {
            "salon":  [{"from": "2026-06-21T10:00:00+00:00", "to": "2026-06-21T12:00:00+00:00",
                        "shutter": "open",   "window": "open",   "reason": "refroidir"}],
            "bureau": [{"from": "2026-06-21T10:00:00+00:00", "to": "2026-06-21T12:00:00+00:00",
                        "shutter": "closed", "window": "closed", "reason": "maintenir"}],
        },
        "best_cost": 0.5, "n_candidates": 10,
    }
    result = _plan_to_room_plans(plan)
    assert len(result) == 2
    ids = {rp.room_id for rp in result}
    assert ids == {"salon", "bureau"}
