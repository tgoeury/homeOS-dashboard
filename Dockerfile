# syntax=docker/dockerfile:1
#
# HomeOS main_dashboard — dashboard principal (lecture seule sur data/cache.db
# pour les données collectées ; intégrations directes Plex/chatbot/confort/yt-dlp).
# Multi-arch : linux/amd64 (PC Ubuntu 22) et linux/arm64 (RPi 3 avec OS 64-bit)
#
# Build image locale :
#   docker build -t homeos-main-dashboard .
#
# Lancement :
#   docker run -d --name homeos-main-dashboard \
#     -p 8050:8050 \
#     -v $(pwd)/config.py:/app/config.py:ro \
#     -v /home/pi/homeos-data:/app/data \
#     -v $(pwd)/models:/app/models:ro \
#     homeos-main-dashboard
#
# IMPORTANT : le volume /app/data doit être LE MÊME répertoire hôte que celui
# monté dans data_broker (et extended_dashboard) — c'est le seul moyen de
# partager data/cache.db entre les trois conteneurs.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# --- Dépendances système ---
RUN apt-get update && apt-get install -y --no-install-recommends \
    # Bindings VLC requis par python-vlc (ytdlp_service)
    libvlc5 \
    libvlc-dev \
    # Conversion audio yt-dlp (webm → mp3/flac/…)
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# --- Dépendances Python ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- Code applicatif ---
COPY . .

# --- Utilisateur non-root ---
# data/ et models/ sont créés ici pour le cas sans volume ;
# quand les volumes sont montés, ces dirs sont remplacées à runtime.
RUN useradd --create-home appuser \
    && mkdir -p data models \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8050

CMD ["gunicorn", "app:server", "-b", "0.0.0.0:8050", "-w", "1", "--timeout", "300"]
