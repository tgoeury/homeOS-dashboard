"""Tests unitaires pour modules/theme.py — palette et helpers de style."""
import pytest
from modules.theme import CP, FONT_MONO, FONT_HUD, label_style, value_style, card_style, section_title_style


# ── CP dict ───────────────────────────────────────────────────────────────────

class TestColorPalette:
    REQUIRED_KEYS = [
        "bg0", "bg1", "bg2", "bg3",
        "cyan", "cyan2", "cyan_dim",
        "yellow", "yellow_dim",
        "red", "green", "orange",
        "text", "text_dim", "border",
    ]

    def test_all_required_keys_present(self):
        for key in self.REQUIRED_KEYS:
            assert key in CP, f"Clé manquante dans CP : {key!r}"

    def test_hex_colors_start_with_hash(self):
        hex_keys = ["bg0", "bg1", "bg2", "bg3", "cyan", "cyan2", "yellow", "red", "green", "orange", "text"]
        for key in hex_keys:
            assert CP[key].startswith("#"), f"CP[{key!r}] devrait commencer par '#'"

    def test_dim_colors_are_rgba(self):
        for key in ["cyan_dim", "yellow_dim", "text_dim", "border"]:
            assert CP[key].startswith("rgba"), f"CP[{key!r}] devrait être rgba"

    def test_colors_are_strings(self):
        for key, val in CP.items():
            assert isinstance(val, str), f"CP[{key!r}] n'est pas une chaîne"


# ── FONT constants ────────────────────────────────────────────────────────────

class TestFonts:
    def test_font_mono_is_string(self):
        assert isinstance(FONT_MONO, str)

    def test_font_hud_is_string(self):
        assert isinstance(FONT_HUD, str)

    def test_font_mono_contains_monospace(self):
        assert "monospace" in FONT_MONO.lower()


# ── label_style() ─────────────────────────────────────────────────────────────

class TestLabelStyle:
    def test_returns_dict(self):
        assert isinstance(label_style(), dict)

    def test_has_font_size(self):
        assert "fontSize" in label_style()

    def test_default_color_is_text_dim(self):
        assert label_style()["color"] == CP["text_dim"]

    def test_custom_color_applied(self):
        assert label_style(color="#ff0000")["color"] == "#ff0000"

    def test_has_uppercase_transform(self):
        assert label_style()["textTransform"] == "uppercase"

    def test_has_font_family(self):
        assert "fontFamily" in label_style()


# ── value_style() ─────────────────────────────────────────────────────────────

class TestValueStyle:
    def test_returns_dict(self):
        assert isinstance(value_style(), dict)

    def test_default_color_is_cyan(self):
        assert value_style()["color"] == CP["cyan"]

    def test_custom_size(self):
        assert value_style(size="28px")["fontSize"] == "28px"

    def test_custom_color(self):
        assert value_style(color=CP["yellow"])["color"] == CP["yellow"]

    def test_has_font_weight(self):
        assert value_style()["fontWeight"] == "700"


# ── card_style() ─────────────────────────────────────────────────────────────

class TestCardStyle:
    def test_returns_dict(self):
        assert isinstance(card_style(), dict)

    def test_default_border_top_uses_cyan(self):
        style = card_style()
        assert CP["cyan"] in style["borderTop"]

    def test_custom_accent_in_border_top(self):
        style = card_style(accent=CP["yellow"])
        assert CP["yellow"] in style["borderTop"]

    def test_has_background(self):
        assert "background" in card_style()

    def test_extra_dict_merged(self):
        style = card_style(extra={"margin": "10px"})
        assert style["margin"] == "10px"

    def test_extra_does_not_overwrite_base_keys(self):
        style = card_style(extra={"padding": "0"})
        assert "padding" in style
        assert style["padding"] == "0"

    def test_extra_none_does_not_crash(self):
        assert card_style(extra=None) is not None


# ── section_title_style() ────────────────────────────────────────────────────

class TestSectionTitleStyle:
    def test_returns_dict(self):
        assert isinstance(section_title_style(), dict)

    def test_has_font_size(self):
        assert "fontSize" in section_title_style()

    def test_has_uppercase_transform(self):
        assert section_title_style()["textTransform"] == "uppercase"

    def test_has_font_family(self):
        assert "fontFamily" in section_title_style()
