"""
HomeOS main_dashboard — config.example.py
══════════════════════════════════════════════════════════════
Fichier modèle — copier en config.py et remplir les valeurs.
    cp config.example.py config.py
config.py est ignoré par git (.gitignore racine, motif "config.py").

main_dashboard ne détient plus de connexion live vers MQTT/OpenMeteo/Enedis/
nmap/NextDNS/NAS Synology — data_broker (application indépendante) les
centralise dans data/cache.db, que ce dashboard se contente de lire.
ENEDIS_TOKEN/ENEDIS_PRM ci-dessous ne servent PAS à appeler l'API Enedis
(main_dashboard ne le fait plus) : ils ne font que déterminer si l'onglet
Énergie doit s'afficher, exactement comme PLEX_TOKEN pour l'onglet Musique.
Renseignez-les à l'identique de data_broker/config.py.
══════════════════════════════════════════════════════════════
"""

# ── Identité du logement ──────────────────────────────────────
HOME_NAME = "Mon Appartement"   # Affiché dans le topbar
HOME_CITY = "Paris"             # Nom de la ville (affichage uniquement)

# ── Localisation géographique ──────────────────────────────────────────────
# Utilisée par le moteur de confort (position solaire) — doit correspondre à
# data_broker/config.py. GEO_LABEL est la simple étiquette affichée en page Météo.
GEO_LATITUDE  = 48.8566
GEO_LONGITUDE = 2.3522
GEO_TIMEZONE  = "Europe/Paris"   # Fuseau horaire IANA
GEO_LABEL     = f"{HOME_CITY} // EXT. 48°51'N 2°21'E"

# ── Serveur Dash ──────────────────────────────────────────────
DASH_HOST  = "0.0.0.0"
DASH_DEBUG = False

# ── Ports des services HomeOS ─────────────────────────────────
# Ce fichier est partagé par les trois services de l'écosystème : chacun lit SON
# port ici. main_dashboard écoute donc sur MAIN_DASHBOARD_PORT.
# Les boutons topbar "◂ DATA BROKER" et "EXTENDED DASHBOARD ▸" construisent leur
# URL côté client comme http://<hostname-utilisé>:<PORT>/ (le LAN accède au
# dashboard par plusieurs IP). Renseigner *_URL pour forcer une adresse complète
# si le service tourne sur un autre hôte.
MAIN_DASHBOARD_PORT     = 8050   # port d'écoute de CE service
MAIN_DASHBOARD_URL      = ""   # ex. "http://192.168.1.10:8050/" — laisser vide sinon
DATA_BROKER_PORT        = 8060
DATA_BROKER_URL         = ""   # ex. "http://192.168.1.10:8060/" — laisser vide sinon
EXTENDED_DASHBOARD_PORT = 8070
EXTENDED_DASHBOARD_URL  = ""   # ex. "http://192.168.1.10:8070/" — laisser vide sinon

# ── Intervalles de rafraîchissement (millisecondes) ───────────
INTERVAL_CLOCK_MS     = 1_000
INTERVAL_SENSORS_MS   = 5_000
INTERVAL_SYSTEM_MS    = 8_000
INTERVAL_WEATHER_MS   = 600_000
INTERVAL_MINUTEURS_MS = 1_000
INTERVAL_CHATBOT_MS   = 2_000

# ── Plex ─────────────────────────────────────────────────────
# PLEX_TOKEN vide = onglet Musique désactivé (la page affiche un panneau d'aide).
PLEX_HOST        = "192.168.1.X"
PLEX_PORT        = 32400
PLEX_TOKEN       = "VOTRE_TOKEN_PLEX"     # Préférences Plex → Réseau
PLEX_SERVER_NAME = "VOTRE_SERVEUR_PLEX"   # nom du serveur tel qu'il apparaît dans Plex
# Optionnel — utilisateur géré (Plex Home) : laisser vide pour se connecter
# directement au serveur avec le token admin ci-dessus.
PLEX_HOME_USER   = ""

# ── Enedis — feature-gate uniquement, cf. note en tête de fichier ─────────────
ENEDIS_TOKEN = "VOTRE_TOKEN_ENEDIS"
ENEDIS_PRM   = "VOTRE_PDL_14_CHIFFRES"
ELECTRICITY_PRICE_KWH = 0.20

# ── Synology Chat Bot (intégration directe, propre à main_dashboard) ─────────
SYNOLOGY_CHAT_WEBHOOK_URL = "https://votre-synology/webapi/..."
SYNOLOGY_CHAT_TOKEN       = "VOTRE_TOKEN_BOT"

# ── Chatbot LangGraph ──────────────────────────────────────────────────────────
CHATBOT_API_URL = "http://chatbot:8000"

# ── Seuils d'alerte ───────────────────────────────────────────
ALERT_TEMP_MIN      = 16.0
ALERT_TEMP_MAX      = 27.0
ALERT_HYGRO_MIN     = 40
ALERT_HYGRO_MAX     = 55
ALERT_HUMI_MIN      = 20   # humidité sol plantes — arrosage requis en dessous
ALERT_HUMI_MAX      = 70
ALERT_PLANT_WATER_MIN = 20
# Capteurs de meuble (sensor_type="furniture", cf. ROOMS) : au dessus du seuil,
# la chaîne du froid est rompue → alerte dans le journal de l'onglet Accueil.
ALERT_FRIDGE        = 6    # °C — seuil du capteur "frigo-temp"
ALERT_FREEZER       = -10  # °C — seuil du capteur "congelateur-temp"

# ── Pièces et capteurs (doit correspondre à data_broker/config.py) ───────────
# Capteur : (sensor_id, label, défaut, couleur, unité[, landing_pos][, sensor_type])
#
# landing_pos (int, défaut 0) : position 1-3 sur la page d'accueil, 0 = absent.
#
# sensor_type (str, défaut "room") : nature de ce que mesure le capteur.
#   "room"      = ambiance de la pièce → tracé sur les graphes 24h, pris en
#                 compte dans la température ressentie.
#   "furniture" = un meuble/appareil (frigo, congélateur…) → affiché côte à côte
#                 sous les capteurs de la pièce, exclu des graphes 24h et du
#                 ressenti. Son device ZIGBEE_DEVICES doit porter
#                 "room": "<préfixe du sensor_id>" (ex. "frigo-temp" → "frigo")
#                 pour ne pas écraser les mesures de la pièce qui l'héberge.
#
# Les deux champs optionnels sont reconnus par leur type : ordre libre, et l'un
# peut être fourni sans l'autre.
ROOMS = [
    ("salon",   "Salon",   "cyan",   [
        ("salon-temp",  "Température", "21.4°C", "cyan",   "DHT22 · RPi Zero W #1"),
        ("salon-hygro", "Hygrométrie", "58 %",   "yellow", "DHT22 · RPi Zero W #1"),
    ]),
    ("bureau",  "Bureau",  "green",  [
        ("bureau-temp",  "Température", "--°C",  "green",  "DHT22 · RPi Zero W #2"),
        ("bureau-lux",   "Luminosité",  "-- lx", "cyan",   "BH1750 · RPi Zero W #2"),
    ]),
    ("cuisine", "Cuisine", "green",  [
        ("cuisine-temp",  "Température", "--°C", "green", "SNZB-02P · Zigbee2MQTT"),
        ("cuisine-hygro", "Hygrométrie", "-- %", "green", "SNZB-02P · Zigbee2MQTT"),
        ("frigo-temp",        "Frigo",       "--°C", "green", "SNZB-02LD · Zigbee2MQTT", "furniture"),
        ("congelateur-temp",  "Congélateur", "--°C", "green", "SNZB-02LD · Zigbee2MQTT", "furniture"),
    ]),
]

# device Zigbee2MQTT → pièce / meuble / plante / fenêtre
# (doit correspondre à data_broker/config.py)
ZIGBEE_DEVICES = {
    # "friendly_name": {"room": "salon", "type": "snzb02p"},
    # "frigo-z2m":     {"room": "frigo", "type": "snzb02ld"},   # capteur "furniture"
    # "plante-salon":  {"plant": "plante-salon"},
    # "fenetre-salon": {"window": "salon"},
}
