# HomeOS — main_dashboard

Dashboard domotique auto-hébergé, pensé pour tourner en kiosque sur une tablette
murale et un Raspberry Pi. Interface Dash/Plotly à l'esthétique « cyberpunk »,
entièrement en français, qui agrège dans une seule page :

température et hygrométrie par pièce · humidité du sol des plantes · ouverture
des fenêtres · météo et prévisions · consommation électrique · lecteur musical
Plex · minuteurs de cuisine · supervision réseau et NAS · état de la machine ·
agent conversationnel · planification volets/fenêtres par modèle prédictif.

> **Aperçu** — dix onglets, un seul processus Python, aucune dépendance cloud
> hormis les API que vous configurez vous-même (OpenMeteo, Enedis, NextDNS).

---

## Sommaire

- [À quoi sert ce dépôt](#à-quoi-sert-ce-dépôt)
- [Place dans l'écosystème HomeOS](#place-dans-lécosystème-homeos)
- [Les dix onglets](#les-dix-onglets)
- [Installation](#installation)
- [Configuration](#configuration)
- [Utilisation](#utilisation)
- [Déploiement Docker](#déploiement-docker)
- [Architecture du code](#architecture-du-code)
- [Performance sur petit matériel](#performance-sur-petit-matériel)
- [Tests](#tests)
- [Limites connues](#limites-connues)

---

## À quoi sert ce dépôt

`main_dashboard` est **l'interface** de HomeOS. Il ne collecte presque rien
lui-même : il lit une base SQLite (`data/cache.db`) alimentée par un processus
séparé, [`data_broker`](../data_broker), et se contente de la mettre en forme.

Cette séparation est le choix structurant du projet. Elle permet :

- de **redémarrer le dashboard sans perdre de données** — la collecte continue ;
- d'**ouvrir le dashboard depuis plusieurs postes** sans multiplier les
  connexions MQTT ni les appels aux API externes ;
- de garder l'interface **réactive sur un RPi 3**, puisqu'aucun callback n'attend
  jamais le réseau (voir [Performance](#performance-sur-petit-matériel)).

Quelques intégrations restent **directes**, parce qu'elles répondent à une action
de l'utilisateur et n'ont pas de sens en collecte de fond : Plex, le chatbot,
les minuteurs, le téléchargeur yt-dlp et le moteur de confort.

## Place dans l'écosystème HomeOS

```
                       ┌──────────────────────────┐
  MQTT / Zigbee2MQTT   │                          │
  OpenMeteo            │       data_broker        │   collecte, écrit
  Enedis               │        (port 8060)       │   ───────────────►┐
  NextDNS / nmap       │                          │                   │
  NAS Synology         └──────────────────────────┘                   │
                                                              ┌───────▼────────┐
                       ┌──────────────────────────┐           │ data/cache.db  │
  Plex ────────────────┤     main_dashboard       │ lit       │   (SQLite)     │
  Synology Chat  ──────┤       (port 8050)        │◄──────────┤                │
  yt-dlp / ONNX  ──────┤        ce dépôt          │           └───────▲────────┘
                       └──────────────────────────┘                   │
                                                                      │
                       ┌──────────────────────────┐           lit     │
                       │   extended_dashboard     │◄─────────────────-┘
                       │       (port 8070)        │
                       └──────────────────────────┘
```

Les trois services **partagent le même `config.py`** et le même fichier
`data/cache.db`. Chacun y lit *son* port d'écoute : `MAIN_DASHBOARD_PORT` pour ce
dépôt, `DATA_BROKER_PORT` et `EXTENDED_DASHBOARD_PORT` pour les deux autres.

Les boutons **◂ DATA BROKER** et **EXTENDED DASHBOARD ▸** de la barre supérieure
construisent leur URL côté navigateur, à partir du hostname réellement utilisé
pour accéder à la page. C'est indispensable quand le LAN atteint la machine par
plusieurs adresses (`homeos.local`, `192.168.1.x`, `localhost`) : un lien codé en
dur enverrait la tablette murale sur une adresse qu'elle ne sait pas résoudre.
Renseignez `*_URL` pour forcer une adresse complète si un service tourne
ailleurs.

## Les dix onglets

| Onglet | Contenu | Source |
|---|---|---|
| **Accueil** | Météo extérieure, 3 capteurs au choix, lecteur musical, journal d'alertes, périphériques réseau | `cache.db` |
| **Capteurs** | Un panneau dépliable par pièce : température, hygrométrie, température ressentie, tendance sur 1 h, plantes, fenêtres, historique 24 h | `cache.db` |
| **Météo** | Conditions courantes, prévisions 7 jours, courbe horaire, comparaison intérieur/extérieur | `cache.db` (OpenMeteo via data_broker) |
| **Musique** | Lecteur Plex (lecture dans le navigateur), recherche, artistes, playlists, file d'attente, téléchargeur yt-dlp avec éditeur de tags ID3 | Plex (direct) |
| **Minuteurs** | Minuteurs multiples, presets classés par fréquence d'usage, alarme sonore et modal d'expiration | mémoire + `cache.db` |
| **Confort** | Plage de température souhaitée par pièce, puis inférence ONNX proposant un plan volets/fenêtres sur 24 h | modèle ONNX local |
| **Énergie** | Consommation Enedis : hier, mois en cours, mois précédent, barplot 30 jours et 13 mois, bascule kWh/€ | `cache.db` |
| **Réseau** | Appareils du LAN, statistiques NextDNS, volumes et santé du NAS, carte mondiale du trafic DNS | `cache.db` |
| **Système** | CPU, RAM, disque, température, uptime, état des services | psutil + `cache.db` |
| **Chatbot** | Conversation avec un agent LangGraph via API REST, plus réception des messages Synology Chat | API externe |

Les onglets dont le service n'est pas configuré (`PLEX_TOKEN`, `ENEDIS_TOKEN`,
`CHATBOT_API_URL` vides) affichent un panneau d'aide expliquant quoi renseigner,
au lieu d'une page vide ou d'une erreur.

## Installation

Prérequis : **Python 3.10+**, et `ffmpeg` + `libvlc` si vous voulez le
téléchargeur audio de l'onglet Musique.

```bash
git clone <url-du-dépôt> main_dashboard
cd main_dashboard

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.py config.py
$EDITOR config.py          # cf. section Configuration

python app.py              # http://<machine>:8050
```

`data_broker` doit tourner en parallèle et écrire dans le **même**
`data/cache.db`, sans quoi tous les indicateurs resteront à `--` / `DOWN`. Le
dashboard démarre quand même : il affiche l'absence de données plutôt que de
refuser de se lancer.

## Configuration

Tout tient dans `config.py`, copié depuis `config.example.py`.
**`config.py` n'est jamais commité** (voir `.gitignore`) : il contient vos
jetons d'API et les coordonnées GPS de votre logement.

### Clés principales

| Clé | Rôle |
|---|---|
| `HOME_NAME`, `HOME_CITY` | Libellés affichés dans la barre supérieure |
| `GEO_LATITUDE`, `GEO_LONGITUDE`, `GEO_TIMEZONE` | Position solaire du moteur de confort — doit correspondre à `data_broker/config.py` |
| `DASH_HOST`, `DASH_DEBUG` | `0.0.0.0` pour exposer sur le LAN ; `DASH_DEBUG=True` uniquement en développement |
| `MAIN_DASHBOARD_PORT` | **Port d'écoute de ce service** (8050 par défaut) |
| `DATA_BROKER_PORT`, `EXTENDED_DASHBOARD_PORT` | Ports des deux services voisins, pour les liens de la barre supérieure |
| `*_URL` | Force une URL absolue si le service voisin tourne sur une autre machine |
| `INTERVAL_*_MS` | Cadence de rafraîchissement de chaque famille de callbacks |
| `PLEX_*` | Serveur Plex ; `PLEX_TOKEN` vide désactive l'onglet Musique |
| `ENEDIS_TOKEN`, `ENEDIS_PRM`, `ELECTRICITY_PRICE_KWH` | Consommation électrique ; les deux premiers ne servent ici qu'à activer l'onglet — c'est `data_broker` qui interroge l'API |
| `SYNOLOGY_CHAT_*`, `CHATBOT_API_URL` | Agent conversationnel |
| `ALERT_*` | Seuils de couleur et d'alerte (température, hygrométrie, humidité du sol, frigo/congélateur) |
| `ROOMS`, `ZIGBEE_DEVICES` | **Doivent être identiques à `data_broker/config.py`** |

### Déclarer une pièce et ses capteurs

`ROOMS` décrit ce que l'interface affiche ; `ZIGBEE_DEVICES` fait le lien avec
les noms publiés par Zigbee2MQTT.

```python
ROOMS = [
    # (id, nom affiché, couleur, [capteurs])
    ("salon", "Salon", "green", [
        # (id capteur, libellé, valeur par défaut, couleur, sous-titre[, position accueil][, type])
        ("salon-temp",      "Température", "--°C", "green", "SNZB-02P · Zigbee2MQTT", 1),
        ("salon-hygro",     "Hygrométrie", "-- %", "green", "SNZB-02P · Zigbee2MQTT"),
        ("arum-salon",      "Arum",        "-- %", "green", "SGS01Z · Zigbee2MQTT"),
        ("salon-fenetre-1", "Fenêtre 1",   "--",   "green", "SNZB-04 · Zigbee2MQTT"),
    ]),
    ("cuisine", "Cuisine", "green", [
        ("cuisine-temp",     "Température", "--°C", "green", "SNZB-02P · Zigbee2MQTT"),
        ("cuisine-hygro",    "Hygrométrie", "-- %", "green", "SNZB-02P · Zigbee2MQTT"),
        ("frigo-temp",       "Frigo",       "--°C", "green", "SNZB-02LD · Zigbee2MQTT", "furniture"),
    ]),
]

ZIGBEE_DEVICES = {
    "TH4_salon":          {"type": "snzb02p", "room":   "salon"},
    "TH9_cuisine":        {"type": "snzb02p", "room":   "cuisine"},
    "TH10_frigo":         {"type": "snzb02ld", "room":  "frigo"},   # capteur "furniture"
    "MOIST3_arum-salon":  {"type": "sgs01z",  "plant":  "arum-salon"},
    "OPN4_salon":         {"type": "snzb04",  "window": "salon-fenetre-1"},
}
```

Quatre conventions à retenir :

- le suffixe de l'identifiant détermine la mesure lue : `-temp` → température,
  `-hygro` → humidité, `-lux` → luminosité ;
- un capteur référencé sous `"plant"` ou `"window"` dans `ZIGBEE_DEVICES` migre
  automatiquement dans la sous-section dépliable correspondante de la pièce ;
- le champ optionnel entier (`1`, `2` ou `3`) place le capteur sur la page
  d'accueil ; `0` ou absent = pas affiché ;
- le champ optionnel texte est le `sensor_type`, `"room"` par défaut. Un capteur
  `"furniture"` mesure un meuble (frigo, congélateur…) et non l'ambiance : il
  s'affiche côte à côte sous les capteurs de la pièce, sans température
  ressentie, et n'apparaît pas sur les graphes 24h. Son entrée `ZIGBEE_DEVICES`
  doit porter `"room"` = préfixe de son identifiant (`frigo-temp` → `"frigo"`),
  sinon ses relevés écraseraient ceux de la pièce qui l'héberge. Les seuils
  `ALERT_FRIDGE` / `ALERT_FREEZER` déclenchent une alerte accueil si
  `frigo-temp` / `congelateur-temp` passent au dessus.

Les deux champs optionnels sont reconnus par leur type : leur ordre est libre et
l'un peut être fourni sans l'autre.

Les couleurs sont des clés de la palette définie dans `modules/theme.py`
(`cyan`, `green`, `yellow`, `red`, `orange`, `cyan_dim`).

## Utilisation

Naviguez entre les onglets par la barre horizontale. Quelques points non
évidents :

- **Capteurs** — cliquez sur le nom d'une pièce pour la replier. Une flèche
  ↑ / ↓ / = compare la valeur courante à la moyenne de la dernière heure. Un tag
  `OUTDATED` apparaît au-delà de 15 min sans mise à jour (jaune), puis 3 h (rouge).
- **Musique** — la lecture se fait dans le navigateur, pas sur le serveur. Un
  clic sur une piste l'insère juste après le morceau en cours ; `▶` la joue
  immédiatement, `+` l'ajoute en fin de file.
- **Confort** — réglez les plages de température, puis **CALCULER**. L'inférence
  prend plusieurs secondes sur RPi : le bouton affiche « inférence en cours » et
  le résultat est mémorisé 10 minutes. Nécessite `models/limited.onnx`.
- **Énergie** — le bouton bascule entre kWh et euros ; le trait pointillé marque
  le 75ᵉ percentile.
- **Minuteurs** — les presets se classent d'eux-mêmes par fréquence d'usage.

### En production

`python app.py` démarre le serveur de développement de Werkzeug, qui le signale
d'ailleurs dans les logs. Pour un usage permanent, servez l'objet WSGI avec
gunicorn :

```bash
gunicorn app:server -b 0.0.0.0:8050 -w 1 --timeout 300
```

**Un seul worker** (`-w 1`) : le processus détient les minuteurs en mémoire, la
file d'attente Plex et les jobs yt-dlp en cours. Un second worker aurait son
propre état et une requête sur deux verrait des données différentes.

## Déploiement Docker

```bash
docker build -t homeos-main-dashboard .

docker run -d --name homeos-main-dashboard \
  -p 8050:8050 \
  -v $(pwd)/config.py:/app/config.py:ro \
  -v /srv/homeos-data:/app/data \
  -v $(pwd)/models:/app/models:ro \
  homeos-main-dashboard
```

**`/app/data` doit être le même répertoire hôte** que celui monté dans
`data_broker` et `extended_dashboard` — c'est le seul moyen de partager
`cache.db` entre les trois conteneurs. L'image est multi-arch (amd64 et arm64) ;
un RPi 3 nécessite un OS 64 bits.

## Architecture du code

```
app.py                      Point d'entrée : app Dash, webhook Synology,
                            callbacks clientside (lecteur audio, horloge, liens)
config.py                   Configuration locale — non versionnée
config.example.py           Modèle documenté à copier

modules/
  dashboard_layout.py       Construit tout le layout (les 10 pages, une seule
                            fois au démarrage) à partir de ROOMS
  callbacks.py              Tous les @callback, groupés par domaine
  theme.py                  Palette, typographies, helpers de style, thème Plotly
  data_cache.py             Accès SQLite : lecture cache/history, séries,
                            statistiques, purge
  db_status.py              État UP / OUTDATED / DOWN d'une source, déduit de la
                            fraîcheur des données en base
  sysinfo.py                CPU, RAM, disque, température, uptime (psutil + /proc)
  plex_client.py            Façade PlexAPI (recherche, albums, playlists, stream)
  ytdlp_service.py          Téléchargement audio en arrière-plan + tags ID3
  timer_service.py          Minuteurs en mémoire, presets persistés
  comfort_engine.py         Inférence ONNX + planification volets/fenêtres
  chatbot_engine.py         File de messages Synology Chat
  logic_engine.py           Aiguillage du traitement des messages (stub FORWARD)

assets/cyberpunk.css        Feuille de style (chargée automatiquement par Dash)
tests/                      137 tests unitaires
```

### Principes de conception

**Le layout est construit une seule fois.** `build_layout()` génère les dix
pages au démarrage ; la navigation ne fait que basculer un `display: none`. Aucun
onglet n'est reconstruit à l'affichage, au prix d'un HTML initial plus lourd.

**Les callbacks coûteux sont conditionnés à l'onglet visible.** Un `dcc.Store`
mémorise la page active ; les callbacks des pages masquées lèvent
`PreventUpdate` immédiatement. Sans cela, le graphe réseau et les figures
d'énergie seraient recalculés en permanence pour personne.

**Les figures Plotly sont des dictionnaires,** pas des objets `go.Figure`. Dash
sérialise en JSON de toute façon ; la validation de schéma de `graph_objects` est
un travail entièrement jeté ensuite (~15 à 57 ms par figure sur RPi).

**La base est la seule source de vérité.** Aucun état partagé entre callbacks
hormis les `dcc.Store` côté navigateur, ce qui rend chaque callback testable
isolément.

## Performance sur petit matériel

La cible est un **Raspberry Pi 3** (4 cœurs à 1,2 GHz, 1 Go de RAM, stockage
microSD). Le principe directeur : *aucun callback périodique ne doit faire de
travail proportionnel à la taille de la base*.

Mesures prises sur une base réelle de 50 Mo (231 000 lignes d'historique), en
médiane sur 5 exécutions, hors machine RPi — les valeurs y sont environ dix fois
supérieures, mais les rapports sont conservés :

| Callback | Cadence | Avant | Après |
|---|---|---|---|
| `update_badges` (badges de la barre supérieure) | 5 s | 247 ms | **0,3 ms** |
| `update_system` (page Système) | 8 s | 347 ms | **2,6 ms** |
| `update_zigbee_discovery` (page Capteurs) | 5 s | 163 ms | **0,6 ms** |
| `update_meteo` | 10 min | 19,7 ms | **0,9 ms** |
| `update_energie` (page Énergie) | 5 s | 6,3 ms | **1,4 ms** |
| `update_home_status` (journal d'accueil) | 5 s | 4,5 ms | **0,7 ms** |

L'essentiel du gain vient d'un seul défaut : l'état « connecté à la base » de
chaque source était calculé par un `COUNT(*)` et un `GROUP BY name` sur toute la
table `history`, exécuté **deux fois par source et par appel** — soit six
parcours complets de 231 000 lignes toutes les 5 secondes. La même information se
lit dans la table `cache`, qui tient une ligne par clé et que `data_broker` écrit
*dans la même transaction* que l'historique : 46 ms deviennent 0,05 ms pour une
réponse identique.

Les autres optimisations en place, par ordre d'importance :

- **connexion SQLite unique et persistante**, partagée par tous les modules —
  ouvrir une connexion par requête coûte cher sur microSD ;
- **index couvrant `(name, ts, value)`** sur `history` : les lectures de courbes
  sont satisfaites depuis l'index seul, sans accès aléatoire à la table ;
- **`read_history_latest()`** plutôt que `read_history(...)[-1]`, qui chargeait
  toute une série pour n'en garder que le dernier point ;
- **filtrage borné en SQL** (`since_ts` / `until_ts` / `limit`) plutôt qu'en
  Python : les graphiques 24 h ne désérialisent jamais l'historique complet ;
- **mémo de la série Enedis** invalidé par l'horodatage de sa dernière ligne :
  elle ne change qu'une fois par jour mais était relue et retriée toutes les 5 s ;
- **sondes de services mises en cache 60 s** : `systemctl` et les connexions TCP
  forkaient un sous-processus à chaque tick ;
- **`psutil.cpu_percent(interval=None)`** — la variante bloquante gaspillait
  200 ms par tick ;
- **imports paresseux** de numpy, pandas et onnxruntime (~65 Mo de RSS) dans
  `comfort_engine` : ils ne sont chargés qu'au premier clic sur CALCULER, ce qui
  compte sur une machine à 1 Go ;
- **graphiques 24 h rafraîchis toutes les 60 s** et non toutes les 5 s : un
  historique d'une journée ne change pas visiblement en 5 secondes.

Le profilage se refait facilement — chaque callback est une fonction ordinaire,
appelable hors de Dash :

```python
import time, modules.callbacks as CB
t0 = time.perf_counter(); CB.update_badges(1)
print(f"{(time.perf_counter() - t0) * 1000:.1f} ms")
```

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

137 tests couvrent le moteur de confort, l'état de la base, les minuteurs, le
thème et les moteurs de chat. Ils s'exécutent en environ une seconde et
n'utilisent ni réseau ni base de production (`tmp_path` de pytest).

## Limites connues

À savoir avant de se fier à l'affichage :

- **Le compteur « Messages MQTT » et les scores d'anomalie « Isolation Forest »
  de la page Système sont simulés** — respectivement un compteur incrémenté au
  hasard et des valeurs aléatoires. Ce sont des maquettes d'interface, pas des
  mesures. La carte « Points InfluxDB » est figée à 0 pour la même raison.
- Le statut `influxdb` reflète seulement l'accessibilité de la base SQLite :
  aucune série InfluxDB n'existe dans ce schéma.
- Le moteur de confort suppose volets et fenêtres **fermés** en état initial :
  les actionneurs ne sont pas encore intégrés, il propose un plan sans savoir
  lire l'état réel.
- `ENEDIS` reste en `OUTDATED` (orange) la majeure partie de la journée : la
  règle générale est « frais si moins d'une heure », alors qu'Enedis ne publie
  qu'une fois par jour. C'est voulu — « connecté mais pas récent » plutôt qu'une
  alerte.
- Un seul worker gunicorn est supporté (état en mémoire).

## Licence

Aucune licence n'est encore déclarée. Ajoutez un fichier `LICENSE` avant de
publier le dépôt si vous souhaitez autoriser la réutilisation — sans licence, le
code reste par défaut sous droit d'auteur exclusif.
