"""Tests de la température ressentie (modules/callbacks._steadman)."""
import pytest

from modules.callbacks import _steadman


@pytest.mark.parametrize("T, H", [
    (10.6, 82),   # cas signalé : renvoyait 30.6 °C
    (0.0, 95),
    (-5.0, 100),
    (5.0, 60),
    (15.0, 90),
    (19.0, 50),
    (21.0, 45),
])
def test_cool_conditions_stay_close_to_air_temp(T, H):
    """Hors domaine de Rothfusz, le ressenti reste égal (ou très proche) de T."""
    assert T <= _steadman(T, H) < T + 1.0


@pytest.mark.parametrize("T, H, expected", [
    (32.2, 50, 35.0),   # 90 °F / 50 % → HI ≈ 95 °F (table NWS)
    (35.0, 60, 45.0),   # 95 °F / 60 % → HI ≈ 113-114 °F
    (26.7, 90, 30.0),   # 80 °F / 90 % → HI ≈ 86 °F (ajustement H > 85 %)
])
def test_hot_conditions_match_nws_table(T, H, expected):
    assert _steadman(T, H) == pytest.approx(expected, abs=1.0)


def test_monotonic_in_temperature_at_high_humidity():
    """Pas de saut ni de bosse à la transition simple ↔ Rothfusz."""
    vals = [_steadman(t / 2, 85) for t in range(-20, 80)]
    assert all(b >= a for a, b in zip(vals, vals[1:]))
