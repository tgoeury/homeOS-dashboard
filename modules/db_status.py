"""
HomeOS main_dashboard — modules/db_status.py
Statut "connecté à la base de données" pour les données centralisées par
data_broker (MQTT/Zigbee2MQTT, météo, Enedis…).

main_dashboard ne détient plus les connexions directes à ces services — il ne
fait que lire data/cache.db, écrit par le processus data_broker indépendant.
Ce module remplace donc les anciennes sondes systemctl/process/TCP.

Deux lectures, deux échelles de temps :

  database_status()  — état global de la base, seul indicateur de collecte
                       affiché en topbar (badge DATABASE : connecté/non
                       connecté) et page Système (entrée "database" :
                       fraîcheur, seuil 24 h).
      DOWN     — base injoignable                                    → rouge
      OUTDATED — joignable, mais plus rien de neuf depuis 24 h
                 (ou base vide)                                      → orange
      UP       — donnée la plus récente < 24 h                       → vert

  zigbee_status()    — fraîcheur du seul pipeline Zigbee2MQTT, au seuil serré
                       d'1 h, pour le bandeau d'état de la page Capteurs.
      DOWN     — base injoignable, ou aucune mesure jamais reçue     → rouge
      OUTDATED — dernière mesure ≥ 1 h                               → orange
      UP       — dernière mesure < 1 h                               → vert

Coût : ce module est sollicité à chaque cycle de rafraîchissement (badge topbar
DATABASE toutes les 5 s, entrée "database" de la page Système toutes les 8 s,
page Capteurs). Chaque appel doit donc rester de l'ordre de la fraction de
milliseconde — d'où la lecture de la table `cache` (une ligne par clé) plutôt
que des agrégats sur `history` (231 000 lignes). Voir DataCache.newest_cache_ts
et DataCache.newest_data_ts.
"""

import time

from modules.data_cache import data_cache
from modules.theme import CP

FRESH_THRESHOLD_S = 3600  # 1 h — seuil du pipeline Zigbee, cf. spec

# Seuil de fraîcheur de l'indicateur global "database" (badge topbar DATABASE +
# entrée "database" de la page Système) : la base agrège des sources de cadences
# très différentes (capteurs à la minute, Enedis une fois par jour), le seuil
# pertinent pour "la base est-elle encore alimentée ?" est donc la journée.
DB_FRESH_THRESHOLD_S = 24 * 3600  # 24 h

UP, OUTDATED, DOWN = "UP", "OUTDATED", "DOWN"

_STATE_COLORS = {UP: CP["green"], OUTDATED: CP["orange"], DOWN: CP["red"]}

# Préfixes des clés de la table `cache` témoignant de l'activité Zigbee2MQTT.
# data_broker écrit `cache` et `history` dans la même transaction (cf.
# DataCache.record_measurements) : la clé cache la plus récente a donc le même
# horodatage que la dernière ligne history de la série correspondante.
_ZIGBEE_PREFIXES = ("sensor.", "plant.", "window.")


def _age_of_newest_cache(prefixes: tuple[str, ...]) -> float | None:
    """Âge (s) de la clé cache la plus récente commençant par un des préfixes."""
    newest = data_cache.newest_cache_ts(prefixes)
    return (time.time() - newest) if newest is not None else None


def zigbee_status() -> tuple[str, str]:
    """
    (état, couleur) du pipeline Zigbee2MQTT vu depuis la base : fraîcheur de la
    mesure la plus récente, tous appareils confondus (capteur/plante/fenêtre).
    Seuil UP/OUTDATED : FRESH_THRESHOLD_S (1 h).
    """
    if not data_cache.is_reachable():
        return DOWN, _STATE_COLORS[DOWN]

    age = _age_of_newest_cache(_ZIGBEE_PREFIXES)
    if age is None:
        state = DOWN
    else:
        state = UP if age <= FRESH_THRESHOLD_S else OUTDATED
    return state, _STATE_COLORS[state]


def is_connected() -> bool:
    """True si data/cache.db répond — état du badge topbar DATABASE (vert/rouge)."""
    return data_cache.is_reachable()


def database_status() -> tuple[str, str, float | None]:
    """
    Statut global de la base : (état, couleur, âge en secondes de la donnée la
    plus récente — None si base injoignable ou vide). Seuil : DB_FRESH_THRESHOLD_S.
    """
    if not data_cache.is_reachable():
        return DOWN, _STATE_COLORS[DOWN], None

    newest = data_cache.newest_data_ts()
    if newest is None:
        return OUTDATED, _STATE_COLORS[OUTDATED], None

    age = max(0.0, time.time() - newest)
    state = UP if age <= DB_FRESH_THRESHOLD_S else OUTDATED
    return state, _STATE_COLORS[state], age
