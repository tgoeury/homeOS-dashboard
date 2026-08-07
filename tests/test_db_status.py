"""Tests unitaires pour modules/db_status.py — statut tri-état UP/OUTDATED/DOWN.

Deux indicateurs, deux seuils : zigbee_status() (pipeline Zigbee2MQTT, 1 h) et
database_status() (base entière, 24 h).
"""
import time
from unittest.mock import patch

import pytest

from modules import db_status
from modules.data_cache import DataCache
from modules.theme import CP


@pytest.fixture
def dc(tmp_path):
    return DataCache(path=tmp_path / "test.db")


def _patched(dc):
    return patch.object(db_status, "data_cache", dc)


def _record_sensor(dc, key: str, value, age_s: float = 0.0) -> None:
    """Reproduit ce que data_broker écrit pour une mesure Zigbee : la dernière
    valeur connue dans `cache` ET le point de série dans `history`, au même
    horodatage (cf. DataCache.record_measurements côté data_broker)."""
    ts = time.time() - age_s
    dc.write(key, value, "°C", "test")
    dc.log_raw(key.replace(".", "_"), ts, value, "°C", "test")
    # `write` horodate à l'instant présent : on force l'âge voulu.
    with dc._lock, dc._connect() as conn:
        conn.execute("UPDATE cache SET updated_at=? WHERE key=?", (ts, key))


# ── zigbee_status() — seuil 1 h ───────────────────────────────────────────────

def test_down_when_no_data_ever(dc):
    with _patched(dc):
        state, color = db_status.zigbee_status()
    assert state == db_status.DOWN
    assert color == CP["red"]


def test_up_when_recent_measurement(dc):
    _record_sensor(dc, "sensor.salon.temperature", 21.0)
    with _patched(dc):
        state, color = db_status.zigbee_status()
    assert state == db_status.UP
    assert color == CP["green"]


def test_outdated_when_stale_measurement(dc):
    _record_sensor(dc, "sensor.salon.temperature", 21.0, age_s=7200)
    with _patched(dc):
        state, color = db_status.zigbee_status()
    assert state == db_status.OUTDATED
    assert color == CP["orange"]


def test_zigbee_takes_newest_across_prefixes(dc):
    """La fraîcheur Zigbee est celle de la source la plus récente, tous types
    d'appareils confondus (capteur / plante / fenêtre)."""
    _record_sensor(dc, "plant.arum.soil_moisture", 42.0, age_s=7200)
    _record_sensor(dc, "window.salon-fenetre-1.contact", True)
    with _patched(dc):
        state, _ = db_status.zigbee_status()
    assert state == db_status.UP


def test_zigbee_ignores_non_device_keys(dc):
    """Une clé fraîche d'une autre source (météo) ne rend pas le pipeline
    Zigbee sain : seuls les préfixes sensor./plant./window. comptent."""
    dc.write("weather.snapshot", {"temperature": 12.0}, source="openmeteo")
    with _patched(dc):
        state, _ = db_status.zigbee_status()
    assert state == db_status.DOWN


def test_down_when_db_unreachable(dc):
    with _patched(dc), patch.object(dc, "is_reachable", return_value=False):
        state, color = db_status.zigbee_status()
    assert state == db_status.DOWN
    assert color == CP["red"]


# ── database_status() / is_connected() — seuil 24 h ───────────────────────────

def test_database_up_when_recent_data(dc):
    _record_sensor(dc, "sensor.salon.temperature", 21.0, age_s=60)
    with _patched(dc):
        state, color, age = db_status.database_status()
    assert state == db_status.UP
    assert color == CP["green"]
    assert 55 <= age <= 90


def test_database_up_beyond_the_1h_zigbee_threshold(dc):
    """Le seuil global est la journée : une donnée de 5 h reste "récente" ici,
    alors que le même âge met zigbee_status() en OUTDATED."""
    _record_sensor(dc, "sensor.salon.temperature", 21.0, age_s=5 * 3600)
    with _patched(dc):
        db_state, _, _ = db_status.database_status()
        zigbee_state, _ = db_status.zigbee_status()
    assert db_state == db_status.UP
    assert zigbee_state == db_status.OUTDATED


def test_database_outdated_when_all_data_older_than_24h(dc):
    _record_sensor(dc, "sensor.salon.temperature", 21.0, age_s=30 * 3600)
    with _patched(dc):
        state, color, age = db_status.database_status()
    assert state == db_status.OUTDATED
    assert color == CP["orange"]
    assert age > db_status.DB_FRESH_THRESHOLD_S


def test_database_takes_newest_across_all_sources(dc):
    """L'âge rapporté est celui de la donnée la plus récente, toutes sources
    confondues — une série ancienne (Enedis) n'assombrit pas le diagnostic."""
    _record_sensor(dc, "enedis.daily", 12.3, age_s=30 * 3600)
    _record_sensor(dc, "sensor.salon.temperature", 21.0, age_s=120)
    with _patched(dc):
        state, _, age = db_status.database_status()
    assert state == db_status.UP
    assert age < 300


def test_database_outdated_when_empty(dc):
    """Base joignable mais vide : ce n'est pas une panne de connexion (rouge),
    c'est une absence de données (orange), et l'âge est indéterminé."""
    with _patched(dc):
        state, color, age = db_status.database_status()
    assert state == db_status.OUTDATED
    assert color == CP["orange"]
    assert age is None


def test_database_down_when_unreachable(dc):
    _record_sensor(dc, "sensor.salon.temperature", 21.0)
    with _patched(dc), patch.object(dc, "is_reachable", return_value=False):
        state, color, age = db_status.database_status()
    assert state == db_status.DOWN
    assert color == CP["red"]
    assert age is None


def test_is_connected_reflects_reachability(dc):
    with _patched(dc):
        assert db_status.is_connected() is True
    with _patched(dc), patch.object(dc, "is_reachable", return_value=False):
        assert db_status.is_connected() is False
