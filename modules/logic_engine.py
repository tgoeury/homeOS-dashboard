"""
HomeOS — modules/logic_engine.py
═══════════════════════════════════════════════════════════════
Module logique du chatbot.
Stub initial : forwarde les messages tels quels (mode FORWARD).

Roadmap d'évolution :
  FORWARD → relay direct, aucun traitement — implémenté ✓
  ML      → Isolation Forest / modèle local sur données capteurs (TODO)
  CLAUDE  → API Anthropic Claude (claude-sonnet-4-6 ou opus-4-7) (TODO)

Pour migrer vers un daemon autonome :
  1. Extraire ce module dans un processus séparé
  2. Remplacer les appels directs par IPC (socket Unix / queue Redis)
  3. Implémenter les modes ML et CLAUDE dans leurs méthodes dédiées
     — ML    : charger le modèle sklearn une fois au démarrage, inférer à chaque message
     — CLAUDE: instancier anthropic.Anthropic(), appeler client.messages.create(...)
               avec un system prompt contextualisant les données capteurs HomeOS
═══════════════════════════════════════════════════════════════
"""
from __future__ import annotations

import logging
from enum import Enum

log = logging.getLogger(__name__)


# ── Mode de traitement ─────────────────────────────────────────────────────────

class LogicMode(Enum):
    """Mode de traitement actif du moteur logique."""
    FORWARD = "forward"   # Relay direct, aucun traitement (stub)
    ML      = "ml"        # Machine learning local (non implémenté)
    CLAUDE  = "claude"    # API Anthropic Claude (non implémenté)


# ── Configuration ──────────────────────────────────────────────────────────────

_CURRENT_MODE: LogicMode = LogicMode.FORWARD


# ── Interface publique ─────────────────────────────────────────────────────────

def process_message(text: str) -> str:
    """
    Traite un message entrant et retourne la réponse à envoyer.

    Mode FORWARD : retourne le message tel quel (stub).
    Mode ML      : TODO — analyse contextuelle via modèle local.
    Mode CLAUDE  : TODO — appel API Anthropic (claude-sonnet-4-6).
    """
    if _CURRENT_MODE == LogicMode.FORWARD:
        log.debug("[logic_engine] FORWARD: %r", text)
        return text
    raise NotImplementedError(
        f"Mode '{_CURRENT_MODE.value}' non encore implémenté."
    )


def get_mode() -> LogicMode:
    """Retourne le mode de traitement actif."""
    return _CURRENT_MODE


def set_mode(mode: LogicMode) -> None:
    """Change le mode de traitement (tests / future API)."""
    global _CURRENT_MODE
    log.info("[logic_engine] Mode → %s", mode.value)
    _CURRENT_MODE = mode


def generate_reply(sent_text: str) -> str | None:
    """
    Génère la réponse du bot après envoi d'un message.

    Mode FORWARD : accusé de réception simple.
    Mode ML      : TODO — réponse contextuelle via modèle local.
    Mode CLAUDE  : TODO — réponse générée par l'API Anthropic.

    Retourne None si aucune réponse automatique ne doit être affichée.
    """
    if _CURRENT_MODE == LogicMode.FORWARD:
        return f"Message recu : {sent_text}"
    # Les modes avancés génèreront leur propre réponse
    return None


def is_operational() -> bool:
    """
    Retourne True si le moteur logique est opérationnel.
    Le mode FORWARD est toujours opérationnel.
    Les modes ML/CLAUDE lèvent NotImplementedError si appelés.
    """
    return True
