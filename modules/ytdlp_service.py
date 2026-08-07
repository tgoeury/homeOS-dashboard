"""
HomeOS — modules/ytdlp_service.py
Téléchargement audio via yt-dlp : gestion de process, progression et tags ID3.

Flux :
  YtdlpService.start(url, params) → lance YtdlpJob dans un thread daemon
  YtdlpJob._run()                 → exécute yt-dlp, parse la progression
  YtdlpService.get_snapshot()     → état courant pour le polling Dash
  YtdlpService.apply_tags()       → écrit les tags ID3/Vorbis via mutagen
  YtdlpService.cancel()           → tue le process et supprime le dossier
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = Path(__file__).parent.parent / "data" / "downloads"
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

# Présence de ce fichier dans le dossier d'un job = "ne pas transférer ces fichiers vers
# le NAS, le job yt-dlp (téléchargement / conversion / tags ID3) n'est pas terminé".
LOCK_FILENAME = "ytdlp.lock"

# Regex sur les lignes [download]  45.2% of 5.67MiB at  1.23MiB/s ETA 00:03
_PROGRESS_RE = re.compile(
    r"\[download\]\s+([\d.]+)%\s+of\s+~?[\d.]+\S*\s+at\s+([\S]+)\s+ETA\s+([\S]+)"
)
_MEDIA_EXTS = frozenset((".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".mp4"))


class YtdlpJob:
    """Représente un téléchargement yt-dlp en cours ou terminé."""

    def __init__(self, url: str, params: dict) -> None:
        ts = time.strftime("%Y%m%d_%H%M%S")
        self.id     = ts
        self.url    = url
        self.params = params
        self.folder = DOWNLOAD_DIR / ts
        self.folder.mkdir(parents=True, exist_ok=True)
        (self.folder / LOCK_FILENAME).touch()
        self.process: subprocess.Popen | None = None
        self.status       = "running"
        self.progress_pct: float = 0.0
        self.progress_str = "Connexion…"
        self.files: list[str] = []
        self.metadata: dict   = {}
        self.error: str       = ""
        self._lock = threading.Lock()

    def _remove_lock(self) -> None:
        """Retire le fichier de verrouillage — le service de transfert peut reprendre la main."""
        (self.folder / LOCK_FILENAME).unlink(missing_ok=True)

    # ── Construction de la commande ───────────────────────────────────────────

    def _build_cmd(self) -> list[str]:
        fmt      = self.params.get("format", "mp3").lower()
        chapters = bool(self.params.get("chapters", False))

        out_tpl = (
            str(self.folder / "%(section_number)02d - %(section_title)s.%(ext)s")
            if chapters
            else str(self.folder / "%(title)s.%(ext)s")
        )

        if fmt == "mp4":
            cmd = [
                "yt-dlp",
                "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
                "--merge-output-format", "mp4",
                "--embed-metadata",
                "--write-info-json",
                "--output", out_tpl,
                "--newline",
            ]
        else:
            quality_ui    = int(self.params.get("quality", 8))
            quality_ytdlp = str(max(0, 10 - quality_ui))   # UI 10 = meilleure → yt-dlp 0
            cmd = [
                "yt-dlp",
                "--extract-audio",
                "--audio-format",  fmt,
                "--audio-quality", quality_ytdlp,
                "--embed-metadata",
                "--write-info-json",
                "--output", out_tpl,
                "--newline",
            ]

        if chapters:
            cmd.append("--split-chapters")
        cmd.append(self.url)
        return cmd

    # ── Parsing de la progression ─────────────────────────────────────────────

    def _parse_line(self, line: str) -> None:
        m = _PROGRESS_RE.search(line)
        if m:
            pct_s, speed, eta = m.groups()
            with self._lock:
                self.progress_pct = float(pct_s)
                self.progress_str = f"{pct_s}%  {speed}  ETA {eta}"
        elif "[download] 100%" in line:
            with self._lock:
                self.progress_pct = 99.0
                self.progress_str = "Conversion audio…"

    # ── Lecture des métadonnées ───────────────────────────────────────────────

    def _read_metadata(self) -> dict:
        """Lit le premier *.info.json et retourne des tags normalisés."""
        for p in sorted(self.folder.glob("*.info.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                year = data.get("release_year") or (
                    data["upload_date"][:4] if data.get("upload_date") else ""
                )
                return {
                    "title":       data.get("title", ""),
                    "artist":      data.get("artist") or data.get("uploader", ""),
                    "albumartist": (data.get("album_artist")
                                   or data.get("artist")
                                   or data.get("uploader", "")),
                    "album":       data.get("album", ""),
                    "year":        str(year) if year else "",
                }
            except Exception:
                continue
        # Fallback : inférence depuis le nom du premier fichier audio
        for p in sorted(self.folder.iterdir()):
            if p.suffix.lower() in _MEDIA_EXTS:
                return _infer_tags_from_filename(p.name)
        return {}

    # ── Thread principal ──────────────────────────────────────────────────────

    def _run(self) -> None:
        cmd = self._build_cmd()
        logger.info("YtdlpJob %s: %s", self.id, " ".join(cmd))
        output_lines: list[str] = []
        try:
            self.process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            for line in self.process.stdout:
                line = line.rstrip()
                logger.debug("YtdlpJob %s: %s", self.id, line)
                output_lines.append(line)
                with self._lock:
                    if self.status == "cancelled":
                        break
                self._parse_line(line)

            self.process.wait()
            with self._lock:
                if self.status == "cancelled":
                    return
                if self.process.returncode == 0:
                    self.files = [
                        f.name for f in sorted(self.folder.iterdir())
                        if f.suffix.lower() in _MEDIA_EXTS
                    ]
                    self.metadata     = self._read_metadata()
                    self.status       = "success"
                    self.progress_pct = 100.0
                    self.progress_str = "Terminé"
                else:
                    tail = "\n".join(output_lines[-20:])
                    logger.error("YtdlpJob %s: returncode=%d\n%s",
                                 self.id, self.process.returncode, tail)
                    self.status       = "failed"
                    self.error        = tail
                    self.progress_str = "Erreur yt-dlp (voir logs)"
                    self._remove_lock()
        except FileNotFoundError:
            with self._lock:
                self.status       = "failed"
                self.error        = "yt-dlp introuvable — installez-le : pip install yt-dlp"
                self.progress_str = self.error
                self._remove_lock()
        except Exception as exc:
            with self._lock:
                self.status       = "failed"
                self.error        = str(exc)
                self.progress_str = f"Erreur : {exc}"
                self._remove_lock()
        logger.info("YtdlpJob %s: status=%s files=%s", self.id, self.status, self.files)

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True, name=f"ytdlp-{self.id}").start()

    def cancel(self) -> None:
        with self._lock:
            self.status = "cancelled"
        if self.process and self.process.poll() is None:
            try:
                self.process.terminate()
            except Exception:
                pass
        shutil.rmtree(self.folder, ignore_errors=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "id":           self.id,
                "url":          self.url,
                "params":       dict(self.params),
                "status":       self.status,
                "progress_pct": self.progress_pct,
                "progress_str": self.progress_str,
                "files":        list(self.files),
                "metadata":     dict(self.metadata),
                "error":        self.error,
                "folder":       str(self.folder),
            }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_url(url: str) -> str:
    """Supprime les paramètres de tracking superflus d'une URL.

    Conserve uniquement le premier paramètre de la query string, ce qui suffit
    pour identifier la vidéo (ex. ?v=...) tout en éliminant list=, start_radio=, pp=…
    """
    parsed = urlparse(url.strip())
    first_param = parsed.query.split("&")[0] if parsed.query else ""
    return urlunparse(parsed._replace(query=first_param))


def _infer_tags_from_filename(filename: str) -> dict:
    """Déduit des tags ID3 basiques depuis le nom de fichier (format 'Artiste - Titre')."""
    stem = Path(filename).stem
    stem = re.sub(r"^\d+\s*[-–]\s*", "", stem)   # supprime préfixe "01 - "
    tags: dict = {"title": stem, "artist": "", "albumartist": "", "album": "", "year": ""}
    if " - " in stem:
        parts           = stem.split(" - ", 1)
        tags["artist"]      = parts[0].strip()
        tags["albumartist"] = parts[0].strip()
        tags["title"]       = parts[1].strip()
    return tags


# ── Service singleton ─────────────────────────────────────────────────────────

class YtdlpService:
    """Gestionnaire pour le job yt-dlp courant (un seul à la fois)."""

    def __init__(self) -> None:
        self._job: YtdlpJob | None = None
        self._lock = threading.Lock()

    def start(self, url: str, params: dict) -> tuple[str, str]:
        """Lance un téléchargement. Retourne (job_id, folder_path)."""
        job = YtdlpJob(_clean_url(url), params)
        with self._lock:
            self._job = job
        job.start()
        return job.id, str(job.folder)

    def cancel(self) -> None:
        with self._lock:
            job, self._job = self._job, None
        if job:
            job.cancel()

    def get_snapshot(self) -> dict | None:
        with self._lock:
            job = self._job
        return job.snapshot() if job else None

    def clear(self) -> None:
        with self._lock:
            self._job = None

    def release_lock(self, folder: str) -> None:
        """Supprime ytdlp.lock une fois le job entièrement terminé (tags ID3 inclus)."""
        Path(folder, LOCK_FILENAME).unlink(missing_ok=True)

    def apply_tags(self, files: list[str], folder: str, tags: dict,
                   single_file: bool = True) -> None:
        """Écrit les tags ID3 (MP3) ou Vorbis (FLAC) via mutagen."""
        try:
            from mutagen.easyid3 import EasyID3
            from mutagen.flac import FLAC
            from mutagen.mp3 import MP3
        except ImportError:
            logger.warning("apply_tags: mutagen non installé (pip install mutagen)")
            return

        folder_path = Path(folder)
        for fname in files:
            fpath = folder_path / fname
            if not fpath.exists():
                continue
            try:
                ext = fpath.suffix.lower()
                if ext == ".mp3":
                    try:
                        audio = MP3(fpath, ID3=EasyID3)
                    except Exception:
                        audio = EasyID3()
                        audio.save(fpath)
                        audio = MP3(fpath, ID3=EasyID3)
                    _apply_audio_tags(audio, tags, single_file, _ID3_KEYS, wrap_in_list=True)
                    audio.save()
                elif ext == ".flac":
                    audio = FLAC(fpath)
                    _apply_audio_tags(audio, tags, single_file, _VORBIS_KEYS, wrap_in_list=False)
                    audio.save()
                logger.debug("apply_tags: %s → OK", fname)
            except Exception as exc:
                logger.warning("apply_tags: %s — %s", fname, exc)


# Mapping (ui_key, audio_key) pour EasyID3 (MP3) et Vorbis (FLAC).
# La clé "title" n'est appliquée que si single_file=True.
_ID3_KEYS = (
    ("artist",      "artist"),
    ("albumartist", "albumartist"),
    ("album",       "album"),
    ("year",        "date"),
    ("title",       "title"),
)
_VORBIS_KEYS = (
    ("artist",      "ARTIST"),
    ("albumartist", "ALBUMARTIST"),
    ("album",       "ALBUM"),
    ("year",        "DATE"),
    ("title",       "TITLE"),
)


def _apply_audio_tags(audio, tags: dict, single_file: bool,
                      key_map: tuple, wrap_in_list: bool) -> None:
    """Applique les métadonnées d'un dict `tags` sur un objet audio mutagen.

    key_map      : séquence de (ui_key, audio_key) adaptée au format (ID3 ou Vorbis).
    wrap_in_list : True pour EasyID3 (valeurs en liste), False pour FLAC/Vorbis.
    La clé "title" est ignorée quand single_file=False (album multi-pistes).
    """
    for ui_key, audio_key in key_map:
        if ui_key == "title" and not single_file:
            continue
        if tags.get(ui_key):
            audio[audio_key] = [tags[ui_key]] if wrap_in_list else tags[ui_key]


ytdlp_service = YtdlpService()
