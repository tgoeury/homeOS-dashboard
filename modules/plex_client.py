"""
HomeOS — modules/plex_client.py
Client PlexAPI : recherche, artistes récents, playlists, lecture locale (navigateur).
Singleton `plex_client` partagé par les callbacks.
"""

import logging
from typing import Optional

from plexapi.server import PlexServer
import config

logger = logging.getLogger(__name__)

PLEX_URL = f"http://{config.PLEX_HOST}:{config.PLEX_PORT}"


class PlexClient:
    """
    Façade PlexAPI pour HomeOS.

    Toutes les méthodes publiques gèrent la (re)connexion automatiquement :
    _connect() instancie PlexServer au premier appel et met en cache le résultat.
    En cas d'erreur réseau, _reset() vide le cache pour forcer une reconnexion
    au prochain appel.

    La lecture se fait entièrement côté navigateur (élément <audio> du dashboard,
    voir callbacks.py) : ce client ne pilote aucun client Plex distant.

    Le singleton `plex_client` est importé directement par callbacks.py.
    """

    def __init__(self):
        self._server: Optional[PlexServer] = None

    # ── Connexion ──────────────────────────────────────────────────────────────

    def _connect(self) -> Optional[PlexServer]:
        if self._server is None:
            try:
                if getattr(config, "PLEX_HOME_USER", ""):
                    # Managed user (Plex Home) : token admin → switchHomeUser → serveur
                    from plexapi.myplex import MyPlexAccount
                    account = MyPlexAccount(token=config.PLEX_TOKEN)
                    account = account.switchHomeUser(config.PLEX_HOME_USER)
                    resource = account.resource(config.PLEX_SERVER_NAME)
                    self._server = resource.connect()
                else:
                    self._server = PlexServer(PLEX_URL, config.PLEX_TOKEN)
                logger.info("Plex connecté : %s v%s", self._server.friendlyName, self._server.version)
            except Exception as e:
                logger.error("Plex connexion échouée : %s", e)
        return self._server

    def _reset(self):
        self._server = None

    def thumb_url(self, thumb: str) -> str:
        """Construit l'URL absolue d'une miniature Plex en ajoutant le token d'auth."""
        if not thumb:
            return ""
        return f"{PLEX_URL}{thumb}?X-Plex-Token={config.PLEX_TOKEN}"

    def _best_thumb(self, item) -> str:
        """Essaie thumb, parentThumb, grandparentThumb dans l'ordre."""
        for attr in ("thumb", "parentThumb", "grandparentThumb"):
            val = getattr(item, attr, None)
            if val:
                return self.thumb_url(val)
        return ""

    def _track_dict(self, t) -> dict:
        """Sérialise une piste PlexAPI en dict minimal pour le UI (title, artist, album, thumb, rating_key)."""
        return {
            "title":      t.title,
            "artist":     getattr(t, "grandparentTitle", ""),
            "album":      getattr(t, "parentTitle", ""),
            "thumb":      self._best_thumb(t),
            "rating_key": str(t.ratingKey),
        }

    def _playable_track_dict(self, t) -> dict:
        """Comme _track_dict, enrichi de l'URL de stream et de la durée (nécessaire pour la file d'attente)."""
        return {
            **self._track_dict(t),
            "stream_url": self._part_url(t),
            "duration":   self._dur_str(t.duration or 0),
        }

    # ── Recherche ──────────────────────────────────────────────────────────────

    def search_tracks(self, query: str, limit: int = 8) -> list:
        """Recherche des pistes par titre, artiste ou album. Retourne jusqu'à `limit` résultats."""
        server = self._connect()
        if not server or not query.strip():
            return []
        try:
            results = server.library.search(query, libtype="track", maxresults=limit)
            return [self._track_dict(t) for t in results]
        except Exception as e:
            logger.error("Plex search : %s", e)
            self._reset()
            return []

    # ── Artistes récents ───────────────────────────────────────────────────────

    def get_recent_artists(self, limit: int = 12) -> list:
        """
        Retourne les artistes les plus récemment écoutés (dédupliqués).
        Scanne les dernières pistes jouées et remonte à l'artiste parent.
        """
        server = self._connect()
        if not server:
            return []
        try:
            music = next(
                (s for s in server.library.sections() if s.type == "artist"), None
            )
            if not music:
                return []
            recent_tracks = music.search(
                libtype="track", sort="lastViewedAt:desc", maxresults=limit * 4
            )
            seen: dict[str, dict] = {}
            for t in recent_tracks:
                name = getattr(t, "grandparentTitle", "") or t.title
                rk   = str(getattr(t, "grandparentRatingKey", "") or "")
                if name and name not in seen:
                    seen[name] = {
                        "title":      name,
                        "thumb":      self.thumb_url(
                            getattr(t, "grandparentThumb", "")
                            or getattr(t, "parentThumb", "")
                            or getattr(t, "thumb", "")
                        ),
                        "rating_key": rk,
                    }
                if len(seen) >= limit:
                    break
            return list(seen.values())
        except Exception as e:
            logger.error("Plex artistes récents : %s", e)
            self._reset()
            return []

    # ── Playlists ──────────────────────────────────────────────────────────────

    def get_playlists(self) -> list:
        """Retourne toutes les playlists audio de la bibliothèque Plex."""
        server = self._connect()
        if not server:
            return []
        try:
            playlists = server.playlists(playlistType="audio")
            return [
                {
                    "title":      p.title,
                    "count":      getattr(p, "leafCount", 0),
                    "thumb":      self.thumb_url(
                        getattr(p, "thumb", "") or getattr(p, "composite", "")
                    ),
                    "rating_key": str(p.ratingKey),
                }
                for p in playlists
            ]
        except Exception as e:
            logger.error("Plex playlists : %s", e)
            self._reset()
            return []

    # ── Stream local (browser audio) ──────────────────────────────────────────

    def get_track_data(self, rating_key: str) -> Optional[dict]:
        """Retourne métadonnées + URL de stream directe pour lecture dans le navigateur.

        Retourne None si le rating_key pointe vers un Album ou un Artist
        (seules les pistes sont directement streamables).
        """
        server = self._connect()
        if not server or not rating_key:
            return None
        try:
            track = server.fetchItem(int(rating_key))
            if getattr(track, "type", "") != "track":
                logger.debug("Plex get_track_data %s : type=%s, attendu 'track'",
                             rating_key, getattr(track, "type", "?"))
                return None
            return self._playable_track_dict(track)
        except Exception as e:
            logger.error("Plex get_track_data %s : %s", rating_key, e)
            return None

    # ── Helpers internes ─────────────────────────────────────────────────────

    def _part_url(self, track) -> str:
        """Construit l'URL de stream direct depuis le premier media/part d'une piste."""
        try:
            part = track.media[0].parts[0]
            return f"{PLEX_URL}{part.key}?X-Plex-Token={config.PLEX_TOKEN}"
        except (IndexError, AttributeError, TypeError):
            return ""

    def _dur_str(self, ms: int) -> str:
        """Convertit une durée en millisecondes en chaîne M:SS."""
        s = (ms or 0) // 1000
        return f"{s // 60}:{s % 60:02d}"

    # ── Albums d'un artiste ───────────────────────────────────────────────────

    def get_artist_albums(self, rating_key: str) -> list:
        """Retourne les albums d'un artiste identifié par son rating_key Plex."""
        server = self._connect()
        if not server or not rating_key:
            return []
        try:
            artist = server.fetchItem(int(rating_key))
            return [
                {
                    "title":      a.title,
                    "thumb":      self._best_thumb(a),
                    "rating_key": str(a.ratingKey),
                    "year":       str(getattr(a, "year", "") or ""),
                }
                for a in artist.albums()
            ]
        except Exception as e:
            logger.error("Plex artist albums %s : %s", rating_key, e)
            return []

    # ── Pistes d'un album ────────────────────────────────────────────────────

    def get_album_tracks(self, rating_key: str) -> list:
        """Retourne les pistes d'un album identifié par son rating_key Plex."""
        server = self._connect()
        if not server or not rating_key:
            return []
        try:
            album = server.fetchItem(int(rating_key))
            return [self._track_dict(t) for t in album.tracks()]
        except Exception as e:
            logger.error("Plex album tracks %s : %s", rating_key, e)
            return []

    # ── Pistes d'une playlist ─────────────────────────────────────────────────

    def get_playlist_tracks(self, rating_key: str) -> list:
        """Retourne les pistes audio d'une playlist identifiée par son rating_key Plex."""
        server = self._connect()
        if not server or not rating_key:
            return []
        try:
            playlist = server.fetchItem(int(rating_key))
            return [
                self._track_dict(t)
                for t in playlist.items()
                if getattr(t, "type", "") == "track"
            ]
        except Exception as e:
            logger.error("Plex playlist tracks %s : %s", rating_key, e)
            return []

    # ── Contexte album complet (file d'attente prev/next) ────────────────────

    def get_album_context(self, rating_key: str) -> dict:
        """Retourne toutes les pistes de l'album + l'index de la piste actuelle, prêtes pour la file d'attente.

        Accepte un rating_key de type 'track' (idx positionné sur la piste)
        ou 'album' (idx=0, toutes les pistes de l'album retournées).
        """
        server = self._connect()
        if not server or not rating_key:
            return {"tracks": [], "idx": 0}
        try:
            item      = server.fetchItem(int(rating_key))
            item_type = getattr(item, "type", "")
            if item_type == "track":
                album         = item.album()
                if album is None:
                    return {"tracks": [], "idx": 0}
                track_rk_ref  = str(rating_key)
            elif item_type == "album":
                album         = item
                track_rk_ref  = None
            else:
                logger.debug("Plex album context %s : type=%s non géré", rating_key, item_type)
                return {"tracks": [], "idx": 0}
            tracks = []
            idx    = 0
            for i, t in enumerate(album.tracks()):
                if track_rk_ref and str(t.ratingKey) == track_rk_ref:
                    idx = i
                tracks.append(self._playable_track_dict(t))
            return {"tracks": tracks, "idx": idx}
        except Exception as e:
            logger.error("Plex album context %s : %s", rating_key, e)
            return {"tracks": [], "idx": 0}

    # ── Contexte playlist complet (lecture immédiate / ajout en fin de file) ──

    def get_playlist_context(self, rating_key: str) -> dict:
        """Retourne toutes les pistes d'une playlist, prêtes pour la file d'attente (idx=0)."""
        server = self._connect()
        if not server or not rating_key:
            return {"tracks": [], "idx": 0}
        try:
            playlist = server.fetchItem(int(rating_key))
            tracks = [
                self._playable_track_dict(t)
                for t in playlist.items()
                if getattr(t, "type", "") == "track"
            ]
            return {"tracks": tracks, "idx": 0}
        except Exception as e:
            logger.error("Plex playlist context %s : %s", rating_key, e)
            return {"tracks": [], "idx": 0}


plex_client = PlexClient()
