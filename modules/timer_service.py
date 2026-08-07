"""
HomeOS — modules/timer_service.py
Gestionnaire de minuteurs en mémoire + presets persistés dans cache.db.

Architecture :
  - Minuteurs actifs : dict thread-safe en mémoire (_timers)
  - Presets : table `timer_presets` dans data/cache.db, triés par uses DESC
  - ALARM_DATA_URI : beep WAV 880 Hz généré à l'import (aucun fichier audio requis)
"""

import base64
import io
import math
import struct
import threading
import time
import uuid
import wave

from modules.data_cache import data_cache

_lock    = threading.Lock()
_timers: dict[str, dict] = {}


# ── Beep WAV encodé en base64 data URI ───────────────────────────────────────

def _make_beep() -> str:
    """Génère un beep 880 Hz avec enveloppe fade-in/out, encodé en WAV data URI."""
    rate, dur, freq = 8000, 0.35, 880.0
    n = int(rate * dur)
    samples = [
        int(32767
            * math.sin(2 * math.pi * freq * i / rate)
            * min(1.0, min(i * 30 / rate, (dur - i / rate) * 30)))
        for i in range(n)
    ]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(struct.pack(f"<{n}h", *samples))
    return "data:audio/wav;base64," + base64.b64encode(buf.getvalue()).decode()


ALARM_DATA_URI: str = _make_beep()


# ── Presets SQLite ────────────────────────────────────────────────────────────

def _init_presets() -> None:
    """Crée la table timer_presets et la seed avec 5 presets par défaut si vide."""
    with data_cache.transaction() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS timer_presets (
                id         TEXT PRIMARY KEY,
                label      TEXT NOT NULL,
                duration_s INTEGER NOT NULL,
                uses       INTEGER DEFAULT 0
            )
        """)
        if not conn.execute("SELECT 1 FROM timer_presets LIMIT 1").fetchone():
            conn.executemany(
                "INSERT OR IGNORE INTO timer_presets VALUES (?, ?, ?, ?)",
                [
                    ("preset-3min",  "3 min",  180,  0),
                    ("preset-6min",  "6 min",  360,  0),
                    ("preset-9min",  "9 min",  540,  0),
                    ("preset-10min", "10 min", 600,  0),
                    ("preset-30min", "30 min", 1800, 0),
                ],
            )


_init_presets()


def get_presets() -> list[dict]:
    """Retourne les presets triés par uses décroissant."""
    with data_cache.transaction() as conn:
        rows = conn.execute(
            "SELECT id, label, duration_s, uses FROM timer_presets ORDER BY uses DESC"
        ).fetchall()
    return [{"id": r[0], "label": r[1], "duration_s": r[2], "uses": r[3]} for r in rows]


def increment_preset(preset_id: str) -> None:
    """Incrémente le compteur d'utilisation d'un preset."""
    with data_cache.transaction() as conn:
        conn.execute(
            "UPDATE timer_presets SET uses = uses + 1 WHERE id = ?", (preset_id,)
        )


# ── Minuteurs actifs (en mémoire, thread-safe) ───────────────────────────────

def next_timer_name() -> str:
    """Retourne 'Timer N' où N est le premier entier sans conflit avec les noms existants."""
    with _lock:
        existing = {t["name"] for t in _timers.values()}
    i = 1
    while f"Timer {i}" in existing:
        i += 1
    return f"Timer {i}"


def start_timer(name: str, total_s: int) -> str:
    """Crée et démarre un minuteur. Retourne son ID."""
    tid = str(uuid.uuid4())[:8]
    with _lock:
        _timers[tid] = {
            "id":          tid,
            "name":        name,
            "total_s":     total_s,
            "remaining_s": total_s,
            "started_at":  time.time(),
            "expired":     False,
        }
    return tid


def delete_timer(tid: str) -> None:
    """Supprime un minuteur par ID."""
    with _lock:
        _timers.pop(tid, None)


def delete_expired() -> None:
    """Supprime tous les minuteurs dont expired=True."""
    with _lock:
        to_del = [tid for tid, t in _timers.items() if t["expired"]]
        for tid in to_del:
            del _timers[tid]


def tick_and_get() -> list[dict]:
    """
    Recalcule remaining_s de chaque minuteur actif depuis son started_at,
    marque expired si remaining_s == 0, puis retourne la liste triée
    par remaining_s croissant (les plus urgents en premier).
    """
    now = time.time()
    with _lock:
        for t in _timers.values():
            if not t["expired"]:
                elapsed = int(now - t["started_at"])
                t["remaining_s"] = max(0, t["total_s"] - elapsed)
                if t["remaining_s"] == 0:
                    t["expired"] = True
        return sorted(list(_timers.values()), key=lambda t: t["remaining_s"])
