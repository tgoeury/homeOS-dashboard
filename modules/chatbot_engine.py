"""
HomeOS — modules/chatbot_engine.py
═══════════════════════════════════════════════════════════════
Moteur de communication chatbot — Synology Chat via webhooks.

Flux de données :
  [Synology Chat bot] → POST /webhook/chat      → add_incoming_message() → store → display

Architecture prévue pour migration en daemon autonome :
  1. Remplacer le store in-memory par une queue persistante (Redis / SQLite)
  2. Ajouter start() / stop() pour une boucle asyncio ou thread de polling
  3. Exposer une légère API REST/socket pour découpler le daemon du serveur Dash
═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import TypedDict

log = logging.getLogger(__name__)


# ── Types ──────────────────────────────────────────────────────────────────────

class Message(TypedDict):
    role: str    # "user" | "bot" | "system"
    text: str
    ts:   float  # epoch seconds


# ── Store thread-safe ──────────────────────────────────────────────────────────

_lock:         threading.Lock = threading.Lock()
_messages:     list[Message]  = []
_last_ping_ok: bool           = False


# ── Interface publique ─────────────────────────────────────────────────────────

def get_messages() -> list[Message]:
    """Retourne une copie thread-safe de la liste des messages."""
    with _lock:
        return list(_messages)


def add_incoming_message(text: str, username: str = "Bot") -> None:
    """
    Ajoute un message reçu depuis Synology Chat.
    Appelé par le webhook Flask (/webhook/chat) dans app.py.
    """
    _append({"role": "bot", "text": text, "ts": time.time()})
    log.info("[chatbot] ← %s: %r", username, text)


def clear_messages() -> None:
    """Vide le store de messages (affiché et en mémoire)."""
    with _lock:
        _messages.clear()
    log.debug("[chatbot] Messages effacés.")


def get_connection_status() -> bool:
    """
    Retourne True si le dernier envoi vers Synology a réussi (HTTP 200).
    False au démarrage (aucun envoi effectué).
    """
    return _last_ping_ok


# ── Helpers privés ─────────────────────────────────────────────────────────────

def _append(msg: Message) -> None:
    with _lock:
        _messages.append(msg)


def fmt_time(ts: float) -> str:
    """Formate un timestamp epoch en HH:MM:SS."""
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S")
