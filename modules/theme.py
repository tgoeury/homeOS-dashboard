"""
HomeOS — modules/theme.py
Palette et constantes visuelles Cyberpunk 2077.
Tailles augmentées (équivalent zoom 200%).
"""

# ── Palette ───────────────────────────────────────────────────────────────────
CP = {
    "bg0":        "#060810",
    "bg1":        "#0a0d15",
    "bg2":        "#0e1220",
    "bg3":        "#141828",
    "cyan":       "#00e5ff",
    "cyan2":      "#00b8cc",
    "cyan_dim":   "rgba(0,229,255,0.12)",
    "yellow":     "#ffe600",
    "yellow_dim": "rgba(255,230,0,0.10)",
    "red":        "#ff2a6d",
    "green":      "#39ff14",
    "orange":     "#ff6b35",
    "text":       "#c8e8ef",
    "text_dim":   "rgba(200,232,239,0.45)",
    "border":     "rgba(0,229,255,0.12)",
}

# ── Typographies ───────────────────────────────────────────────────────────────
FONT_MONO = "'Share Tech Mono', 'Courier New', monospace"
FONT_HUD  = "'Rajdhani', 'Arial Narrow', sans-serif"

# ── Helpers de style inline ────────────────────────────────────────────────────

def label_style(color: str = CP["text_dim"]) -> dict:
    return {
        "fontSize": "14px",
        "letterSpacing": "3px",
        "textTransform": "uppercase",
        "color": color,
        "fontFamily": FONT_MONO,
        "marginBottom": "4px",
    }


def value_style(size: str = "42px", color: str = CP["cyan"]) -> dict:
    return {
        "fontSize": size,
        "fontWeight": "700",
        "fontFamily": FONT_HUD,
        "color": color,
        "lineHeight": "1",
    }


def card_style(accent: str = CP["cyan"], extra: dict = None) -> dict:
    style = {
        "background": CP["bg2"],
        "border": f"1px solid {CP['border']}",
        "borderTop": f"2px solid {accent}",
        "padding": "18px 20px",
        "marginBottom": "8px",
        "clipPath": "polygon(0 0,calc(100% - 14px) 0,100% 14px,100% 100%,14px 100%,0 calc(100% - 14px))",
    }
    if extra:
        style.update(extra)
    return style


def badge_dot_style(color: str) -> dict:
    """Pastille ronde d'un badge de statut topbar.

    Partagée par le layout (état initial, dashboard_layout._status_badge) et par
    le callback qui la recolore (callbacks.update_badges) : les deux doivent
    produire exactement le même style, sans quoi la pastille change de taille au
    premier rafraîchissement.
    """
    return {
        "width": "9px", "height": "9px", "borderRadius": "50%",
        "background": color, "flexShrink": "0",
    }


def section_title_style() -> dict:
    return {
        "fontSize": "13px",
        "letterSpacing": "4px",
        "textTransform": "uppercase",
        "color": "rgba(0,229,255,0.5)",
        "fontFamily": FONT_MONO,
        "marginBottom": "12px",
    }


# ── Hauteurs de graphes ────────────────────────────────────────────────────────
WORLDMAP_HEIGHT = 420   # px — figure Plotly ET conteneur CSS dcc.Graph

# ── Thème Plotly ───────────────────────────────────────────────────────────────
PLOTLY_THEME = {
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor":  "rgba(0,0,0,0)",
    "font": {"color": CP["text_dim"], "family": FONT_MONO, "size": 13},
    "xaxis": {
        "showgrid": False, "zeroline": False,
        "tickfont": {"size": 12, "color": CP["text_dim"], "family": FONT_MONO},
    },
    "yaxis": {
        "showgrid": True, "gridcolor": "rgba(0,229,255,0.06)",
        "zeroline": False,
        "tickfont": {"size": 12, "color": CP["text_dim"], "family": FONT_MONO},
        "ticksuffix": "°",
    },
    "margin": {"l": 40, "r": 14, "t": 8, "b": 32},
    "showlegend": False,
}
