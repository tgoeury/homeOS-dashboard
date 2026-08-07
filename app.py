"""
HomeOS main_dashboard — app.py
Point d'entrée principal Dash.
Lancement : python app.py
Accès     : http://<host-machine>:<MAIN_DASHBOARD_PORT>  (8050 par défaut)

Ce dashboard ne détient plus de connexion live vers MQTT/OpenMeteo/Enedis/nmap/
NextDNS/NAS Synology : il lit ces données dans data/cache.db, alimentée par le
processus indépendant data_broker (voir ../data_broker). Plex, le chatbot
Synology Chat, les minuteurs, yt-dlp et le moteur de confort restent des
intégrations directes propres à cette application.
"""

import logging
import os
import threading
from dash import Dash
from flask import request as flask_request

import config as CFG
from config import DASH_HOST, MAIN_DASHBOARD_PORT, DASH_DEBUG, HOME_NAME
from version import APP_VERSION

_LOG_LEVEL = getattr(logging, os.environ.get("HOMEOS_LOG_LEVEL", "INFO").upper(), logging.INFO)
logging.basicConfig(
    level=_LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)

app = Dash(
    __name__,
    title=f"HomeOS v{APP_VERSION} — {HOME_NAME}",
    update_title=None,
    suppress_callback_exceptions=True,
)

from modules.dashboard_layout import build_layout
app.layout = build_layout()

import modules.callbacks  # noqa: F401 — enregistre les @callback

server = app.server  # WSGI entry point pour gunicorn (app:server)


# ── Pré-fetch au démarrage ─────────────────────────────────────────────────────
# Contrairement au dashboard historique, il n'y a plus de MQTT/météo/réseau à
# pré-chauffer ici (data_broker s'en charge) — seule la connexion Plex, propre
# à cette application, bénéficie d'un warmup pour ne pas bloquer le premier
# rendu de la page Musique.

def _prefetch() -> None:
    log = logging.getLogger("startup")
    log.info("Pré-connexion Plex…")
    try:
        from modules.plex_client import plex_client
        plex_client._connect()
        log.info("Pré-connexion Plex OK")
    except Exception as exc:
        log.warning("Pré-connexion Plex échouée : %s", exc)


threading.Thread(target=_prefetch, daemon=True, name="prefetch").start()


# ── Webhook entrant Synology Chat ──────────────────────────────────────────────
# Synology Chat (bot sortant) envoie un POST ici quand un utilisateur écrit au bot.
# Configurer l'URL dans Synology Chat : Intégrations → Bots → URL sortante
# → http://<IP_HOMEOS>:8050/webhook/chat

@app.server.route('/webhook/chat', methods=['POST'])
def receive_chat_webhook():
    # getattr + garde sur le vide : SYNOLOGY_CHAT_TOKEN peut manquer d'un
    # config.py (le webhook est optionnel). Sans cela, le premier message reçu
    # levait AttributeError → HTTP 500. On refuse en bloc plutôt que d'accepter
    # n'importe quelle requête quand aucun token n'est configuré.
    expected = getattr(CFG, "SYNOLOGY_CHAT_TOKEN", "")
    if not expected:
        logging.warning("[webhook] SYNOLOGY_CHAT_TOKEN non configuré — requête refusée")
        return 'Unauthorized', 401
    token = flask_request.form.get('token', '')
    if token != expected:
        logging.warning("[webhook] Token invalide reçu")
        return 'Unauthorized', 401
    text     = flask_request.form.get('text', '').strip()
    username = flask_request.form.get('username', 'Synology')
    if text:
        from modules import chatbot_engine
        chatbot_engine.add_incoming_message(text, username)
        logging.info("[webhook] Message reçu de %s : %r", username, text)
    return 'OK', 200

from dash import Output, Input

app.clientside_callback(
    """
    function(n) {
        function fmt(s) {
            s = Math.floor(s);
            return Math.floor(s / 60) + ':' + ('0' + (s % 60)).slice(-2);
        }
        var fill = {height: '100%', background: '#00e5ff', transition: 'width .3s ease'};
        var audio = document.getElementById('mu-audio');
        var prog, pos, dur, btn;
        var hasAudio = audio && !isNaN(audio.duration) && audio.duration > 0;
        if (hasAudio) {
            var pct = audio.currentTime / audio.duration * 100;
            btn  = audio.paused ? '▶' : '⏸';
            prog = Object.assign({}, fill, {width: pct + '%'});
            pos  = fmt(audio.currentTime);
            dur  = fmt(audio.duration);
        } else {
            prog = Object.assign({}, fill, {width: '0%'});
            pos = '0:00'; dur = '0:00'; btn = '▶';
        }
        /* Retourne les 4 valeurs pour l'onglet Musique ET les 4 miroirs pour l'Accueil */
        return [prog, pos, dur, btn, prog, pos, dur, btn];
    }
    """,
    Output("mu-prog-fill",      "style"),
    Output("mu-pos",            "children"),
    Output("mu-dur",            "children"),
    Output("btn-play",          "children"),
    Output("home-mu-prog-fill", "style"),
    Output("home-mu-pos",       "children"),
    Output("home-mu-dur",       "children"),
    Output("home-btn-play",     "children"),
    Input("interval-clock",    "n_intervals"),
)

app.clientside_callback(
    """
    function(n, n_home) {
        var audio = document.getElementById('mu-audio');
        if (audio && audio.src && audio.src !== window.location.href) {
            if (audio.paused) { audio.play(); }
            else              { audio.pause(); }
        }
        return '';
    }
    """,
    Output("mu-audio-ctrl",  "children"),
    Input("btn-play",        "n_clicks"),
    Input("home-btn-play",   "n_clicks"),
    prevent_initial_call=True,
)

app.clientside_callback(
    """
    function(expired_ids) {
        var a = document.getElementById('min-alarm');
        if (!a) return '';
        if (expired_ids && expired_ids.length > 0) {
            a.play().catch(function() {});
        } else {
            a.pause();
            a.currentTime = 0;
        }
        return '';
    }
    """,
    Output("min-alarm-ctrl", "children"),
    Input("min-expired-store", "data"),
)

# Boutons topbar "◂ DATA BROKER" / "EXTENDED DASHBOARD ▸" : l'URL est construite
# côté client à partir du hostname utilisé pour accéder à main_dashboard —
# nécessaire car ce même dashboard est généralement ouvert depuis plusieurs IP
# du LAN. Une *_URL renseignée dans config.py prime sur le port.
app.clientside_callback(
    """
    function(cfg) {
        function href(target) {
            if (!target) { return '#'; }
            if (target.url) { return target.url; }
            return window.location.protocol + '//' + window.location.hostname + ':' + target.port + '/';
        }
        if (!cfg) { return ['#', '#']; }
        return [href(cfg.broker), href(cfg.extended)];
    }
    """,
    Output("btn-open-broker", "href"),
    Output("btn-open-extended", "href"),
    Input("links-cfg", "data"),
)

if __name__ == "__main__":
    # Le config.py est partagé par les trois services HomeOS : chacun lit SON port.
    # app.run() = serveur de développement werkzeug (d'où son avertissement au
    # démarrage). En production, servir `app:server` avec gunicorn — cf. README.
    logging.info("HomeOS main_dashboard démarrage — http://%s:%s", DASH_HOST, MAIN_DASHBOARD_PORT)
    app.run(
        host=DASH_HOST,
        port=MAIN_DASHBOARD_PORT,
        debug=DASH_DEBUG,
    )
