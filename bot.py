#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MusicLSP v3.1 — Deezer + SoundCloud + Spotify
Робота без cookies, Deezer API для пошуку, SoundCloud/Deezer/Spotify для завантаження
"""

import os
import logging
import asyncio
import tempfile
import datetime
import sqlite3
import urllib.parse

# Спробуємо підключити psycopg2 (PostgreSQL), якщо немає — використаємо SQLite
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
import hashlib
import base64
import json
import io
import random
import subprocess
import re
import zipfile
from pathlib import Path
from contextlib import closing

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)
import yt_dlp

# ─── Логування ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Конфіг ───────────────────────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("MAIN_BOT_TOKEN", "")
ADMIN_ID = 1293055247
# Database: PostgreSQL (Railway/Neon) або SQLite (fallback)
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = False

if DATABASE_URL and POSTGRES_AVAILABLE:
    try:
        # Railway дає DATABASE_URL у форматі postgres://, psycopg2 потребує postgresql://
        db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        # Тестуємо підключення
        test_conn = psycopg2.connect(db_url, sslmode='require')
        test_conn.close()
        USE_POSTGRES = True
        logger.info("✅ Using PostgreSQL database")
    except Exception as e:
        logger.warning(f"PostgreSQL connection failed: {e}, falling back to SQLite")
        USE_POSTGRES = False

if not USE_POSTGRES:
    # Fallback на SQLite
    DB_PATH = "musiclsp_v3.db"
    logger.info(f"Using SQLite: {DB_PATH}")
MAX_MB = 50
DEF_QUALITY = "192"
AUTHOR = "Lesiv"
BOT_NAME = "MusicLSP"
AUTH_BOT = "@MusicLSPauth_bot"
SEARCH_PER_PAGE = 10

# ─── Deezer ARL Cookie (для повного завантаження, не обов'язково) ─────────────
DEEZER_ARL = os.environ.get("DEEZER_ARL", "")

# Спробуємо прочитати ARL з cookies файлу (якщо є)
DEEZER_COOKIES_FILE = "www.deezer.com_cookies.txt"
if not DEEZER_ARL and os.path.exists(DEEZER_COOKIES_FILE):
    try:
        with open(DEEZER_COOKIES_FILE, 'r') as f:
            for line in f:
                if line.strip().startswith('.deezer.com') and '	arl	' in line:
                    parts = line.strip().split('	')
                    if len(parts) >= 7:
                        DEEZER_ARL = parts[6]
                        logger.info("✅ Deezer ARL loaded from cookies file")
                        break
    except Exception as e:
        logger.warning(f"Failed to read Deezer cookies file: {e}")

if DEEZER_ARL:
    logger.info("✅ Deezer ARL configured — full track downloads enabled")
else:
    logger.info("⚠️ Deezer ARL not found — only 30s previews available")

# ─── Spotify Credentials# ─── Deezer ARL Cookie (для повного завантаження, не обов'язково) ─────────────
DEEZER_ARL = os.environ.get("DEEZER_ARL", "")

# Спробуємо прочитати ARL з cookies файлу (якщо є)
DEEZER_COOKIES_FILE = "www.deezer.com_cookies.txt"
if not DEEZER_ARL and os.path.exists(DEEZER_COOKIES_FILE):
    try:
        with open(DEEZER_COOKIES_FILE, 'r') as f:
            for line in f:
                if line.strip().startswith('.deezer.com') and '	arl	' in line:
                    parts = line.strip().split('	')
                    if len(parts) >= 7:
                        DEEZER_ARL = parts[6]
                        logger.info("✅ Deezer ARL loaded from cookies file")
                        break
    except Exception as e:
        logger.warning(f"Failed to read Deezer cookies file: {e}")

if DEEZER_ARL:
    logger.info("✅ Deezer ARL configured — full track downloads enabled")
else:
    logger.info("⚠️ Deezer ARL not found — only 30s previews available")

# ─── Spotify Credentials (з env) ──────────────────────────────────────────────
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
# Якщо не заповнені — Spotify функції не працюватимуть

# ─── Genius API Token (з env) ─────────────────────────────────────────────────
GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN", "")
# Якщо не заповнений — тексти пісень не працюватимуть

# ─── Free vs Premium Limits ───────────────────────────────────────────────────
FREE_LIMITS = {
    "library_max": 20,
    "artist_songs": 10,
    "batch_download": 0,
    "zip_albums": False,
    "playlists": False,
    "radio": False,
    "spotify_sync": False,
    "recognition": False,
    "lyrics": False,
    "ai_recommend": False,
    "notifications": False,
    "quality_options": ["192"],
    "batch_options": [],
}

PREMIUM_LIMITS = {
    "library_max": 999999,
    "artist_songs": 50,
    "batch_download": 100,
    "zip_albums": True,
    "playlists": True,
    "radio": True,
    "spotify_sync": True,
    "recognition": True,
    "lyrics": True,
    "ai_recommend": True,
    "notifications": True,
    "quality_options": ["192", "320"],
    "batch_options": [20, 50, 100],
}

# ─── Spotify Token Cache ──────────────────────────────────────────────────────
_spotify_token = None
_spotify_token_expires = 0

def get_spotify_token():
    global _spotify_token, _spotify_token_expires
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    if _spotify_token and now < _spotify_token_expires - 60:
        return _spotify_token
    auth_str = f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}"
    auth_bytes = base64.b64encode(auth_str.encode()).decode()
    headers = {
        "Authorization": f"Basic {auth_bytes}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}
    try:
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            headers=headers, data=data, timeout=10
        )
        resp.raise_for_status()
        token_data = resp.json()
        _spotify_token = token_data["access_token"]
        _spotify_token_expires = now + token_data.get("expires_in", 3600)
        logger.info("Spotify token obtained")
        return _spotify_token
    except Exception as e:
        logger.error(f"Spotify auth failed: {e}")
        return None

def spotify_request(endpoint):
    token = get_spotify_token()
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/{endpoint}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        if resp.status_code == 401:
            global _spotify_token
            _spotify_token = None
            token = get_spotify_token()
            if token:
                headers = {"Authorization": f"Bearer {token}"}
                resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Spotify API error ({endpoint}): {e}")
        return None

def get_spotify_album(album_id):
    return spotify_request(f"albums/{album_id}")

def search_spotify_albums(query, limit=10):
    import urllib.parse
    queries = [query, f"{query} album", f"album:{query}"]
    all_items = []
    seen_ids = set()
    for q in queries:
        encoded = urllib.parse.quote(q)
        data = spotify_request(f"search?q={encoded}&type=album&limit={limit}")
        if data and "albums" in data:
            items = data["albums"].get("items", [])
            for item in items:
                item_id = item.get("id")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    all_items.append(item)
            if len(all_items) >= 5:
                break
    return all_items

def fmt_dur_ms(ms):
    if not ms:
        return "—"
    total_sec = ms // 1000
    h, rem = divmod(total_sec, 3600)
    m, s = divmod(rem, 60)
    if h > 0:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"

def fmt_dur(s):
    if not s:
        return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"

# ═══════════════════════════════════════════════════════════════════════════════
#  DEEZER API — ПОШУК ТА ІНФОРМАЦІЯ (без авторизації)
# ═══════════════════════════════════════════════════════════════════════════════

def dz_search_tracks(query, limit=15):
    """Search tracks on Deezer public API (no auth required)."""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.deezer.com/search/track?q={encoded}&limit={limit}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        tracks = []
        for item in data.get("data", []):
            artist_name = item.get("artist", {}).get("name", "Unknown")
            tracks.append({
                "title": f"{artist_name} - {item.get('title', 'Unknown')}",
                "url": item.get("link", ""),
                "id": str(item.get("id", "")),
                "duration": fmt_dur(item.get("duration", 0)),
                "channel": artist_name,
                "source": "deezer",
                "preview_url": item.get("preview", ""),
                "album": item.get("album", {}).get("title", ""),
                "cover": item.get("album", {}).get("cover", ""),
            })
        logger.info(f"Deezer search: {len(tracks)} results for '{query}'")
        return tracks
    except Exception as e:
        logger.warning(f"Deezer search failed: {e}")
        return []

def dz_search_albums(query, limit=10):
    """Search albums on Deezer public API."""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.deezer.com/search/album?q={encoded}&limit={limit}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        albums = []
        for item in data.get("data", []):
            artist_name = item.get("artist", {}).get("name", "Unknown")
            albums.append({
                "id": str(item.get("id", "")),
                "name": item.get("title", "Unknown"),
                "artist": artist_name,
                "release_date": item.get("release_date", "—"),
                "year": item.get("release_date", "—")[:4] if item.get("release_date") else "—",
                "total_tracks": item.get("nb_tracks", 0),
                "image_url": item.get("cover", ""),
                "source": "deezer",
                "deezer_id": str(item.get("id", "")),
            })
        return albums
    except Exception as e:
        logger.warning(f"Deezer album search failed: {e}")
        return []

def dz_get_album_tracks(album_id):
    """Get album tracks from Deezer."""
    try:
        url = f"https://api.deezer.com/album/{album_id}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        album = resp.json()

        tracks = []
        total_duration = 0
        for i, track in enumerate(album.get("tracks", {}).get("data", [])):
            dur = track.get("duration", 0)
            total_duration += dur
            artist_name = track.get("artist", {}).get("name", "Unknown")
            tracks.append({
                "name": track.get("title", "Unknown"),
                "artists": artist_name,
                "duration": fmt_dur(dur),
                "duration_sec": dur,
                "track_number": i + 1,
                "deezer_id": str(track.get("id", "")),
                "url": track.get("link", ""),
            })

        artist_name = album.get("artist", {}).get("name", "Unknown")
        return {
            "id": album_id,
            "name": album.get("title", "Unknown Album"),
            "artist": artist_name,
            "release_date": album.get("release_date", "—"),
            "year": album.get("release_date", "—")[:4] if album.get("release_date") else "—",
            "total_tracks": len(tracks),
            "tracks": tracks,
            "total_duration": fmt_dur(total_duration),
            "total_duration_sec": total_duration,
            "label": album.get("label", "—"),
            "image_url": album.get("cover", ""),
            "source": "deezer",
        }
    except Exception as e:
        logger.error(f"Deezer album tracks error: {e}")
        return None

def dz_get_artist_top(artist_name, limit=20):
    """Get top tracks by artist from Deezer."""
    try:
        encoded = urllib.parse.quote(artist_name)
        url = f"https://api.deezer.com/search/track?q=artist:%22{encoded}%22&limit={limit}"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        tracks = []
        for item in data.get("data", []):
            artist = item.get("artist", {}).get("name", "Unknown")
            tracks.append({
                "title": f"{artist} - {item.get('title', 'Unknown')}",
                "url": item.get("link", ""),
                "id": str(item.get("id", "")),
                "duration": fmt_dur(item.get("duration", 0)),
                "channel": artist,
                "source": "deezer",
            })
        return tracks
    except Exception as e:
        logger.warning(f"Deezer artist top failed: {e}")
        return []

# ─── MUSIC_GENRES ────────────────────────────────────────────────────────────
MUSIC_GENRES = {
    "uk": {
        "pop": "🎤 Поп", "rock": "🎸 Рок", "hiphop": "🎧 Хіп-хоп",
        "electronic": "🎹 Електро", "jazz": "🎷 Джаз", "classical": "🎻 Класика",
        "metal": "🤘 Метал", "rnb": "💃 R&B", "country": "🤠 Кантрі",
        "folk": "🪕 Фолк", "blues": "🔵 Блюз", "reggae": "🌴 Реггі",
        "latin": "💃 Латина", "kpop": "🇰🇷 K-pop", "indie": "🎨 Інді",
        "punk": "🧷 Панк", "disco": "🕺 Діско", "funk": "🎺 Фанк",
        "soul": "❤️ Соул", "techno": "🔊 Техно",
    },
    "ru": {
        "pop": "🎤 Поп", "rock": "🎸 Рок", "hiphop": "🎧 Хип-хоп",
        "electronic": "🎹 Электро", "jazz": "🎷 Джаз", "classical": "🎻 Классика",
        "metal": "🤘 Метал", "rnb": "💃 R&B", "country": "🤠 Кантри",
        "folk": "🪕 Фолк", "blues": "🔵 Блюз", "reggae": "🌴 Регги",
        "latin": "💃 Латина", "kpop": "🇰🇷 K-pop", "indie": "🎨 Инди",
        "punk": "🧷 Панк", "disco": "🕺 Диско", "funk": "🎺 Фанк",
        "soul": "❤️ Соул", "techno": "🔊 Техно",
    },
    "en": {
        "pop": "🎤 Pop", "rock": "🎸 Rock", "hiphop": "🎧 Hip-Hop",
        "electronic": "🎹 Electronic", "jazz": "🎷 Jazz", "classical": "🎻 Classical",
        "metal": "🤘 Metal", "rnb": "💃 R&B", "country": "🤠 Country",
        "folk": "🪕 Folk", "blues": "🔵 Blues", "reggae": "🌴 Reggae",
        "latin": "💃 Latin", "kpop": "🇰🇷 K-pop", "indie": "🎨 Indie",
        "punk": "🧷 Punk", "disco": "🕺 Disco", "funk": "🎺 Funk",
        "soul": "❤️ Soul", "techno": "🔊 Techno",
    },
    "fr": {
        "pop": "🎤 Pop", "rock": "🎸 Rock", "hiphop": "🎧 Hip-Hop",
        "electronic": "🎹 Électro", "jazz": "🎷 Jazz", "classical": "🎻 Classique",
        "metal": "🤘 Metal", "rnb": "💃 R&B", "country": "🤠 Country",
        "folk": "🪕 Folk", "blues": "🔵 Blues", "reggae": "🌴 Reggae",
        "latin": "💃 Latine", "kpop": "🇰🇷 K-pop", "indie": "🎨 Indie",
        "punk": "🧷 Punk", "disco": "🕺 Disco", "funk": "🎺 Funk",
        "soul": "❤️ Soul", "techno": "🔊 Techno",
    },
}

# ─── Мови ─────────────────────────────────────────────────────────────────────
LANGUAGES = {
    "uk": "🇺🇦 Українська",
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
    "fr": "🇫🇷 Français",
}

TEXTS = {
    "welcome": {
        "uk": "🎵 <b>Вітаємо в {bot}!</b>\n\n💿 <b>Free</b> — пошук, завантаження, бібліотека (20)\n💎 <b>Premium</b> — ZIP, плейлисти, радіо, статистика, схожа музика",
        "ru": "🎵 <b>Добро пожаловать в {bot}!</b>\n\n💿 <b>Free</b> — поиск, скачивание, библиотека (20)\n💎 <b>Premium</b> — ZIP, плейлисты, радио, статистика, похожая музыка",
        "en": "🎵 <b>Welcome to {bot}!</b>\n\n💿 <b>Free</b> — search, download, library (20)\n💎 <b>Premium</b> — ZIP, playlists, radio, stats, similar music",
        "fr": "🎵 <b>Bienvenue sur {bot}!</b>\n\n💿 <b>Free</b> — recherche, téléchargement, bibliothèque (20)\n💎 <b>Premium</b> — ZIP, playlists, radio, stats, musique similaire",
    },
    "premium_only": {
        "uk": "⛔ Тільки для <b>Premium</b>\n💎 Оформити → /subscription",
        "ru": "⛔ Только для <b>Premium</b>\n💎 Оформить → /subscription",
        "en": "⛔ <b>Premium</b> only\n💎 Get → /subscription",
        "fr": "⛔ Uniquement <b>Premium</b>\n💎 Obtenir → /subscription",
    },
    "library_full": {
        "uk": "📚 Бібліотека повна ({max})\nВидали або оформи Premium 💎",
        "ru": "📚 Библиотека полна ({max})\nУдали или оформи Premium 💎",
        "en": "📚 Library full ({max})\nRemove or get Premium 💎",
        "fr": "📚 Bibliothèque pleine ({max})\nSupprime ou passe Premium 💎",
    },
}

def tx(key, lang, **kw):
    s = TEXTS.get(key, {})
    t = s.get(lang) or s.get("en") or f"[{key}]"
    return t.format(**kw) if kw else t

# ─── Хелпери для callback_data ────────────────────────────────────────────────
def url_hash(url):
    return hashlib.md5(url.encode("utf-8")).hexdigest()[:8]

def cache_url(bot_data, url, title="", artist=""):
    bot_data.setdefault("url_cache", {})
    h = url_hash(url)
    bot_data["url_cache"][h] = {
        "url": url,
        "title": title,
        "artist": artist,
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }
    _clean_url_cache(bot_data)
    return h

def get_cached_url(bot_data, h):
    return bot_data.get("url_cache", {}).get(h, {})

def _clean_url_cache(bot_data):
    cache = bot_data.get("url_cache", {})
    now = datetime.datetime.now(datetime.timezone.utc)
    to_delete = []
    for h, data in cache.items():
        try:
            ts = datetime.datetime.fromisoformat(data.get("ts", "2000-01-01"))
            if (now - ts).total_seconds() > 3600:
                to_delete.append(h)
        except Exception:
            to_delete.append(h)
    for h in to_delete:
        cache.pop(h, None)

# ─── База даних ───────────────────────────────────────────────────────────────
class DBWrapper:
    """Wrapper for both PostgreSQL and SQLite connections."""
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres
        self._cursor = None

    def __enter__(self):
        if self.is_postgres:
            self._cursor = self.conn.cursor()
            return self._cursor
        else:
            return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.is_postgres:
            if self._cursor:
                self._cursor.close()
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()
        else:
            if exc_type is None:
                self.conn.commit()
            else:
                self.conn.rollback()
            self.conn.close()

    def execute(self, query, params=()):
        """Execute query - works for both PostgreSQL and SQLite."""
        if self.is_postgres:
            cur = self.conn.cursor()
            try:
                # Convert ? to %s for PostgreSQL
                pg_query = query.replace('?', '%s')
                cur.execute(pg_query, params)
                return cur
            finally:
                cur.close()
        else:
            return self.conn.execute(query, params)

    def fetchone(self):
        """Fetch one row."""
        if self.is_postgres:
            # This should be called on cursor, not connection
            raise RuntimeError("Use cursor.fetchone() for PostgreSQL")
        else:
            # SQLite connection doesn't have fetchone
            raise RuntimeError("This shouldn't be called directly")

    def fetchall(self):
        """Fetch all rows."""
        if self.is_postgres:
            raise RuntimeError("Use cursor.fetchall() for PostgreSQL")
        else:
            raise RuntimeError("This shouldn't be called directly")

def db():
    if USE_POSTGRES:
        db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(db_url, sslmode='require')
        return DBWrapper(conn, is_postgres=True)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return DBWrapper(conn, is_postgres=False)

def init_postgres():
    """Initialize PostgreSQL tables."""
    db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    conn = psycopg2.connect(db_url, sslmode='require')
    try:
        with conn.cursor() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    lang TEXT DEFAULT 'uk',
                    joined TIMESTAMP,
                    is_premium BOOLEAN DEFAULT FALSE,
                    premium_since TIMESTAMP,
                    state TEXT DEFAULT ''
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS library (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    title TEXT,
                    artist TEXT,
                    url TEXT,
                    kind TEXT DEFAULT 'track',
                    added TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS history (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    title TEXT,
                    artist TEXT,
                    played TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS playlists (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    name TEXT,
                    description TEXT,
                    created TIMESTAMP,
                    updated TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS playlist_tracks (
                    id SERIAL PRIMARY KEY,
                    playlist_id BIGINT,
                    title TEXT,
                    artist TEXT,
                    url TEXT,
                    duration TEXT,
                    added TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS listening_stats (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    track_title TEXT,
                    artist TEXT,
                    duration_sec INTEGER,
                    played_at TIMESTAMP,
                    source TEXT DEFAULT 'download'
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS radio_sessions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    seed_artist TEXT,
                    seed_track TEXT,
                    tracks_json TEXT,
                    current_idx INTEGER DEFAULT 0,
                    created TIMESTAMP,
                    updated TIMESTAMP
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS recognized_tracks (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    title TEXT,
                    artist TEXT,
                    recognized_at TIMESTAMP,
                    audio_hash TEXT
                )
            """)
            conn.commit()
            logger.info("✅ PostgreSQL tables initialized")
    except Exception as e:
        logger.error(f"PostgreSQL init error: {e}")
        raise
    finally:
        conn.close()

def init_db():
    """Initialize database (PostgreSQL or SQLite)."""
    if USE_POSTGRES:
        init_postgres()
    else:
        init_sqlite()

def init_sqlite():
    """Initialize SQLite tables."""
    with closing(db()) as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY,
            username TEXT,
            lang TEXT DEFAULT 'uk',
            joined TEXT,
            is_premium INTEGER DEFAULT 0,
            premium_since TEXT,
            state TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            artist TEXT,
            url TEXT,
            kind TEXT DEFAULT 'track',
            added TEXT
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            artist TEXT,
            played TEXT
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            name TEXT,
            description TEXT,
            created TEXT,
            updated TEXT
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER,
            title TEXT,
            artist TEXT,
            url TEXT,
            duration TEXT,
            added TEXT,
            FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS listening_stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            track_title TEXT,
            artist TEXT,
            duration_sec INTEGER,
            played_at TEXT,
            source TEXT DEFAULT 'download'
        );
        CREATE TABLE IF NOT EXISTS radio_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            seed_artist TEXT,
            seed_track TEXT,
            tracks_json TEXT,
            current_idx INTEGER DEFAULT 0,
            created TEXT,
            updated TEXT
        );
        CREATE TABLE IF NOT EXISTS recognized_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            title TEXT,
            artist TEXT,
            recognized_at TEXT,
            audio_hash TEXT
        );
        """)

# ── Хелпери БД ────────────────────────────────────────────────────────────────
def get_user(uid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM users WHERE id = %s", (uid,))
            row = c.fetchone()
            if row:
                # Convert tuple to dict-like object
                cols = [desc[0] for desc in c.description]
                return dict(zip(cols, row))
            return None
        else:
            return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def create_user(uid, username):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            # PostgreSQL - use ON CONFLICT
            c.execute(
                "INSERT INTO users (id, username, joined) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (uid, username, now)
            )
        else:
            c.execute(
                "INSERT OR IGNORE INTO users (id, username, joined) VALUES (?, ?, ?)",
                (uid, username, now)
            )

def get_lang(uid):
    u = get_user(uid)
    return u["lang"] if u else "en"

def set_lang(uid, lang):
    with db() as c:
        if USE_POSTGRES:
            c.execute("UPDATE users SET lang = %s WHERE id = %s", (lang, uid))
        else:
            c.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))

def get_state(uid):
    u = get_user(uid)
    return u["state"] if u else ""

def set_state(uid, state):
    with db() as c:
        if USE_POSTGRES:
            c.execute("UPDATE users SET state = %s WHERE id = %s", (state, uid))
        else:
            c.execute("UPDATE users SET state=? WHERE id=?", (state, uid))

def is_premium(uid):
    u = get_user(uid)
    if u is None:
        return False
    if isinstance(u, dict):
        return bool(u.get("is_premium"))
    else:
        return bool(u["is_premium"])

def set_premium(uid, premium=True):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat() if premium else None
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "UPDATE users SET is_premium = %s, premium_since = %s WHERE id = %s",
                (premium, now, uid)
            )
        else:
            c.execute(
                "UPDATE users SET is_premium=?, premium_since=? WHERE id=?",
                (1 if premium else 0, now, uid)
            )

def get_limits(uid):
    return PREMIUM_LIMITS if is_premium(uid) else FREE_LIMITS

def add_library(uid, title, artist, url, kind="track"):
    limits = get_limits(uid)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT COUNT(*) as c FROM library WHERE user_id = %s", (uid,))
            count = c.fetchone()[0]
        else:
            count = c.execute("SELECT COUNT(*) as c FROM library WHERE user_id=?", (uid,)).fetchone()["c"]
        if count >= limits["library_max"]:
            return False, "full"
        if USE_POSTGRES:
            c.execute("SELECT id FROM library WHERE user_id = %s AND url = %s", (uid, url))
            ex = c.fetchone()
        else:
            ex = c.execute("SELECT id FROM library WHERE user_id=? AND url=?", (uid, url)).fetchone()
        if not ex:
            if USE_POSTGRES:
                c.execute(
                    "INSERT INTO library(user_id, title, artist, url, kind, added) VALUES(%s, %s, %s, %s, %s, %s)",
                    (uid, title, artist, url, kind, now)
                )
            else:
                c.execute(
                    "INSERT INTO library(user_id, title, artist, url, kind, added) VALUES(?,?,?,?,?,?)",
                    (uid, title, artist, url, kind, now)
                )
            return True, "added"
    return False, "exists"

def get_library(uid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM library WHERE user_id = %s ORDER BY added DESC", (uid,))
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            return [dict(zip(cols, row)) for row in rows]
        else:
            return c.execute("SELECT * FROM library WHERE user_id=? ORDER BY added DESC", (uid,)).fetchall()

def del_library(uid, lid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("DELETE FROM library WHERE id = %s AND user_id = %s", (lid, uid))
        else:
            c.execute("DELETE FROM library WHERE id=? AND user_id=?", (lid, uid))

def add_history(uid, title, artist):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "INSERT INTO history(user_id, title, artist, played) VALUES(%s, %s, %s, %s)",
                (uid, title, artist, now)
            )
        else:
            c.execute(
                "INSERT INTO history(user_id, title, artist, played) VALUES(?,?,?,?)",
                (uid, title, artist, now)
            )

def get_stats_user(uid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT COUNT(*) as c FROM history WHERE user_id = %s", (uid,))
            dl = c.fetchone()[0]
            c.execute("SELECT COUNT(*) as c FROM library WHERE user_id = %s", (uid,))
            lb = c.fetchone()[0]
            c.execute("SELECT COALESCE(SUM(duration_sec), 0) as s FROM listening_stats WHERE user_id = %s", (uid,))
            listen_time = c.fetchone()[0]
            c.execute("SELECT COUNT(DISTINCT artist) as c FROM listening_stats WHERE user_id = %s", (uid,))
            unique_artists = c.fetchone()[0] or 0
        else:
            dl = c.execute("SELECT COUNT(*) as c FROM history WHERE user_id=?", (uid,)).fetchone()["c"]
            lb = c.execute("SELECT COUNT(*) as c FROM library WHERE user_id=?", (uid,)).fetchone()["c"]
            listen_time = c.execute("SELECT SUM(duration_sec) as s FROM listening_stats WHERE user_id=?", (uid,)).fetchone()["s"] or 0
            unique_artists = c.execute("SELECT COUNT(DISTINCT artist) as c FROM listening_stats WHERE user_id=?", (uid,)).fetchone()["c"] or 0
    return {"dl": dl, "lib": lb, "listen_time": listen_time, "unique_artists": unique_artists}

def add_listening_stat(uid, title, artist, duration_sec=0, source="download"):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "INSERT INTO listening_stats(user_id, track_title, artist, duration_sec, played_at, source) VALUES(%s, %s, %s, %s, %s, %s)",
                (uid, title, artist, duration_sec, now, source)
            )
        else:
            c.execute(
                "INSERT INTO listening_stats(user_id, track_title, artist, duration_sec, played_at, source) VALUES(?,?,?,?,?,?)",
                (uid, title, artist, duration_sec, now, source)
            )

def get_listening_stats(uid, days=30):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT COUNT(*) as c FROM listening_stats WHERE user_id = %s AND played_at > %s", (uid, since))
            total = c.fetchone()[0]
            c.execute("""
                SELECT artist, COUNT(*) as c FROM listening_stats 
                WHERE user_id = %s AND played_at > %s GROUP BY artist ORDER BY c DESC LIMIT 5
            """, (uid, since))
            top_artists = c.fetchall()
            c.execute("""
                SELECT track_title, artist, COUNT(*) as c FROM listening_stats 
                WHERE user_id = %s AND played_at > %s GROUP BY track_title, artist ORDER BY c DESC LIMIT 5
            """, (uid, since))
            top_tracks = c.fetchall()
        else:
            total = c.execute(
                "SELECT COUNT(*) as c FROM listening_stats WHERE user_id=? AND played_at>?",
                (uid, since)
            ).fetchone()["c"]
            top_artists = c.execute("""
                SELECT artist, COUNT(*) as c FROM listening_stats 
                WHERE user_id=? AND played_at>? GROUP BY artist ORDER BY c DESC LIMIT 5
            """, (uid, since)).fetchall()
            top_tracks = c.execute("""
                SELECT track_title, artist, COUNT(*) as c FROM listening_stats 
                WHERE user_id=? AND played_at>? GROUP BY track_title, artist ORDER BY c DESC LIMIT 5
            """, (uid, since)).fetchall()
    return {"total": total, "top_artists": top_artists, "top_tracks": top_tracks}

def get_all_users():
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM users")
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            return [dict(zip(cols, row)) for row in rows]
        else:
            return c.execute("SELECT * FROM users").fetchall()

def get_recent_users(limit=20):
    """Get last N registered users."""
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM users ORDER BY joined DESC LIMIT %s", (limit,))
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            return [dict(zip(cols, row)) for row in rows]
        else:
            return c.execute("SELECT * FROM users ORDER BY joined DESC LIMIT ?", (limit,)).fetchall()

def get_global_stats():
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT COUNT(*) as c FROM users")
            total = c.fetchone()[0]
            c.execute("SELECT COUNT(*) as c FROM users WHERE is_premium = TRUE")
            premium = c.fetchone()[0]
        else:
            total = c.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            premium = c.execute(
                "SELECT COUNT(*) as c FROM users WHERE is_premium=1"
            ).fetchone()["c"]
    return {"total": total, "premium": premium, "free": total - premium}

# ─── ПЛЕЙЛИСТИ ────────────────────────────────────────────────────────────────
def create_playlist(uid, name, description=""):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "INSERT INTO playlists(user_id, name, description, created, updated) VALUES(%s, %s, %s, %s, %s) RETURNING id",
                (uid, name, description, now, now)
            )
            return c.fetchone()[0]
        else:
            c.execute(
                "INSERT INTO playlists(user_id, name, description, created, updated) VALUES(?,?,?,?,?)",
                (uid, name, description, now, now)
            )
            return c.lastrowid

def get_playlists(uid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM playlists WHERE user_id = %s ORDER BY updated DESC", (uid,))
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            return [dict(zip(cols, row)) for row in rows]
        else:
            return c.execute(
                "SELECT * FROM playlists WHERE user_id=? ORDER BY updated DESC", (uid,)
            ).fetchall()

def get_playlist(pid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM playlists WHERE id = %s", (pid,))
            row = c.fetchone()
            cols = [desc[0] for desc in c.description]
            pl = dict(zip(cols, row)) if row else None
            c.execute("SELECT * FROM playlist_tracks WHERE playlist_id = %s ORDER BY id", (pid,))
            rows = c.fetchall()
            cols = [desc[0] for desc in c.description]
            tracks = [dict(zip(cols, row)) for row in rows]
        else:
            pl = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
            tracks = c.execute(
                "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY id", (pid,)
            ).fetchall()
        return pl, tracks

def add_track_to_playlist(pid, title, artist, url, duration=""):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "INSERT INTO playlist_tracks(playlist_id, title, artist, url, duration, added) VALUES(%s, %s, %s, %s, %s, %s)",
                (pid, title, artist, url, duration, now)
            )
            c.execute("UPDATE playlists SET updated = %s WHERE id = %s", (now, pid))
        else:
            c.execute(
                "INSERT INTO playlist_tracks(playlist_id, title, artist, url, duration, added) VALUES(?,?,?,?,?,?)",
                (pid, title, artist, url, duration, now)
            )
            c.execute("UPDATE playlists SET updated=? WHERE id=?", (now, pid))

def delete_playlist(uid, pid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("DELETE FROM playlists WHERE id = %s AND user_id = %s", (pid, uid))
        else:
            c.execute("DELETE FROM playlists WHERE id=? AND user_id=?", (pid, uid))

def delete_playlist_track(pid, tid):
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "DELETE FROM playlist_tracks WHERE id = %s AND playlist_id = %s", (tid, pid)
            )
        else:
            c.execute(
                "DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", (tid, pid)
            )

# ─── РАДІО СЕСІЇ ──────────────────────────────────────────────────────────────
def create_radio_session(uid, seed_artist, seed_track, tracks):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tracks_json = json.dumps(tracks)
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "INSERT INTO radio_sessions(user_id, seed_artist, seed_track, tracks_json, created, updated) VALUES(%s, %s, %s, %s, %s, %s) RETURNING id",
                (uid, seed_artist, seed_track, tracks_json, now, now)
            )
            return c.fetchone()[0]
        else:
            c.execute(
                "INSERT INTO radio_sessions(user_id, seed_artist, seed_track, tracks_json, created, updated) VALUES(?,?,?,?,?,?)",
                (uid, seed_artist, seed_track, tracks_json, now, now)
            )
            return c.lastrowid

def get_radio_session(rid):
    with db() as c:
        if USE_POSTGRES:
            c.execute("SELECT * FROM radio_sessions WHERE id = %s", (rid,))
            row = c.fetchone()
            if row:
                cols = [desc[0] for desc in c.description]
                r = dict(zip(cols, row))
                return r, json.loads(r["tracks_json"])
        else:
            r = c.execute("SELECT * FROM radio_sessions WHERE id=?", (rid,)).fetchone()
            if r:
                return dict(r), json.loads(r["tracks_json"])
        return None, []

def update_radio_idx(rid, idx):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            c.execute(
                "UPDATE radio_sessions SET current_idx = %s, updated = %s WHERE id = %s",
                (idx, now, rid)
            )
        else:
            c.execute(
                "UPDATE radio_sessions SET current_idx=?, updated=? WHERE id=?",
                (idx, now, rid)
            )



async def handle_admin_input(update, ctx, state, text):
    """Handle admin panel inputs."""
    uid = update.effective_user.id
    set_state(uid, "")

    if state == "adm:premium":
        try:
            target_id = int(text.strip())
            set_premium(target_id, True)
            await update.message.reply_text(f"✅ Premium activated for {target_id}")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID")

    elif state == "adm:unpremium":
        try:
            target_id = int(text.strip())
            set_premium(target_id, False)
            await update.message.reply_text(f"✅ Premium deactivated for {target_id}")
        except ValueError:
            await update.message.reply_text("❌ Invalid user ID")

    elif state == "adm:broadcast":
        users = get_all_users()
        sent = 0
        for u in users:
            try:
                await ctx.bot.send_message(u["id"], text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass
        await update.message.reply_text(f"✅ Broadcast sent to {sent}/{len(users)} users")

    elif state == "adm:find":
        search = text.strip()
        with closing(db()) as c:
            if search.startswith("@"):
                if USE_POSTGRES:
                    c.execute("SELECT * FROM users WHERE username = %s", (search[1:],))
                else:
                    c.execute("SELECT * FROM users WHERE username=?", (search[1:],))
                row = c.fetchone()
            else:
                try:
                    if USE_POSTGRES:
                        c.execute("SELECT * FROM users WHERE id = %s", (int(search),))
                    else:
                        c.execute("SELECT * FROM users WHERE id=?", (int(search),))
                    row = c.fetchone()
                except ValueError:
                    row = None

            if row:
                if USE_POSTGRES:
                    cols = [desc[0] for desc in c.description]
                    user = dict(zip(cols, row))
                else:
                    user = dict(row)
                status = "💎 Premium" if (user.get("is_premium") or user.get("is_premium") == 1) else "💿 Free"
                await update.message.reply_text(
                    f"👤 User: {user['id']}\n@{user.get('username', '—')}\nStatus: {status}",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("❌ User not found")

# ═══════════════════════════════════════════════════════════════════════════════
#  YT-DLP — РОБОЧІ КЛІЄНТИ БЕЗ COOKIES
# ═══════════════════════════════════════════════════════════════════════════════

# Оновлені робочі клієнти (2026)
# Music Sources: Spotify + SoundCloud (YouTube removed - too many issues)
# SoundCloud works without authentication
# Spotify requires API credentials

# Базові опції yt-dlp — БЕЗ cookies
BASE_YTDL_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "extract_flat": True,
    "noplaylist": True,
    "socket_timeout": 30,
    "retries": 3,
    "fragment_retries": 3,
    "file_access_retries": 3,
    "extractor_retries": 3,
    # Мінімальний user-agent — yt-dlp сам додає потрібні headers
    # НЕ додаємо referer чи cookies — це викликає блокування
}



# ─── Пошук YouTube/SoundCloud ─────────────────────────────────────────────────


def sc_search(query, limit=15):
    """Search SoundCloud - works without authentication."""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            r = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
        except Exception as e:
            logger.warning(f"SoundCloud search failed: {e}")
            return []
    tracks = []
    for e in (r.get("entries") or []):
        if not e:
            continue
        tracks.append({
            "title": e.get("title", "Unknown"),
            "url": e.get("webpage_url") or e.get("url", ""),
            "id": e.get("id", ""),
            "duration": fmt_dur(e.get("duration", 0)),
            "channel": e.get("uploader") or "SoundCloud",
            "source": "soundcloud",
        })
    logger.info(f"SoundCloud search: {len(tracks)} results for '{query}'")
    return tracks

def search_all(query, limit=10):
    """Search SoundCloud → Deezer → Spotify. Return unified results."""
    results = []
    seen_urls = set()
    seen_titles = set()

    # 1. SoundCloud (основне джерело)
    sc = sc_search(query, limit)
    for track in sc:
        key = track["url"]
        if key and key not in seen_urls:
            seen_urls.add(key)
            seen_titles.add(track["title"].lower().strip())
            results.append(track)

    # 2. Deezer (додаткове джерело)
    dz = dz_search_tracks(query, limit)
    for track in dz:
        key = track["url"]
        title_key = track["title"].lower().strip()
        if key and key not in seen_urls and title_key not in seen_titles:
            seen_urls.add(key)
            seen_titles.add(title_key)
            results.append(track)

    # 3. Spotify (якщо є credentials)
    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        try:
            spotify_tracks = search_spotify_tracks(query, limit=limit)
            for track in spotify_tracks:
                key = track["url"]
                title_key = track["title"].lower().strip()
                if key and key not in seen_urls and title_key not in seen_titles:
                    seen_urls.add(key)
                    seen_titles.add(title_key)
                    results.append(track)
        except Exception as e:
            logger.warning(f"Spotify search failed: {e}")

    logger.info(f"Total search results for '{query}': {len(results)} (SC:{len(sc)}, DZ:{len(dz)})")
    return results[:limit + 5]


# ─── Пошук за жанром (Premium) ──────────────────────────────────────────────
async def show_genres(msg, uid, ctx):
    """Show genre selection keyboard."""
    l = get_lang(uid)
    genres = MUSIC_GENRES.get(l, MUSIC_GENRES["en"])

    kb = []
    row = []
    for key, name in genres.items():
        row.append(InlineKeyboardButton(name, callback_data=f"genre|{key}"))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)

    kb.append([back_btn(uid)])

    text = {
        "uk": "🎵 <b>Обери жанр:</b>",
        "ru": "🎵 <b>Выбери жанр:</b>",
        "en": "🎵 <b>Choose a genre:</b>",
    }.get(l, "🎵 <b>Choose a genre:</b>")

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def search_by_genre(msg, genre_key, uid, ctx):
    """Search popular tracks by genre."""
    l = get_lang(uid)
    genres = MUSIC_GENRES.get(l, MUSIC_GENRES["en"])
    genre_name = genres.get(genre_key, genre_key)

    status = await msg.reply_text(
        f"🔍 Шукаю <b>{genre_name}</b>…", parse_mode="HTML"
    )

    # Search queries for different genres
    genre_queries = {
        "pop": "popular pop music 2024",
        "rock": "best rock music 2024",
        "hiphop": "top hip hop rap 2024",
        "electronic": "electronic dance music EDM 2024",
        "jazz": "best jazz music",
        "classical": "classical music masterpieces",
        "metal": "heavy metal best songs",
        "rnb": "R&B soul music 2024",
        "country": "country music hits 2024",
        "folk": "folk acoustic music",
        "blues": "blues music classics",
        "reggae": "reggae music best",
        "latin": "latin pop reggaeton 2024",
        "kpop": "K-pop hits 2024",
        "indie": "indie music 2024",
        "punk": "punk rock music",
        "disco": "disco funk classics",
        "funk": "funk music grooves",
        "soul": "soul music classics",
        "techno": "techno house music 2024",
    }

    query = genre_queries.get(genre_key, f"{genre_key} music 2024")
    tracks = await async_search(query, limit=15)

    if not tracks:
        await status.edit_text("😔 Нічого не знайдено.")
        return

    await status.delete()

    # Show results
    ck = f"genre_{uid}_{genre_key}_{msg.message_id if hasattr(msg, 'message_id') else 0}"
    ctx.bot_data.setdefault("cache", {})[ck] = tracks

    kb = []
    for i, t in enumerate(tracks[:10]):
        icon = "🎵" if t.get("source") == "soundcloud" else "🟣" if t.get("source") == "deezer" else "🟢"
        kb.append([
            InlineKeyboardButton(
                f"{icon} {t['title'][:40]} ({t['duration']})",
                callback_data=f"dl|{i}|{ck}"
            )
        ])

    kb.append([back_btn(uid)])

    text = f"🎵 <b>{genre_name}</b> — знайдено {len(tracks)} треків:\n\nОбери пісню 👇"

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

def search_spotify_tracks(query, limit=5):
    """Search tracks in Spotify."""
    token = get_spotify_token()
    if not token:
        return []

    import urllib.parse
    encoded = urllib.parse.quote(query)
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://api.spotify.com/v1/search?q={encoded}&type=track&limit={limit}"

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        tracks = []
        for item in data.get("tracks", {}).get("items", []):
            artists = ", ".join(a.get("name", "") for a in item.get("artists", []))
            tracks.append({
                "title": f"{artists} - {item.get('name', 'Unknown')}",
                "url": item.get("external_urls", {}).get("spotify", ""),
                "id": item.get("id", ""),
                "duration": fmt_dur_ms(item.get("duration_ms", 0)),
                "channel": artists,
                "source": "spotify",
                "spotify_id": item.get("id", ""),
            })
        return tracks
    except Exception as e:
        logger.error(f"Spotify track search error: {e}")
        return []

def artist_songs(artist, limit=50):
    """Search artist songs on SoundCloud + Deezer."""
    results = []
    seen = set()

    # SoundCloud
    sc = sc_search(artist, limit)
    for t in sc:
        if t["url"] not in seen:
            seen.add(t["url"])
            results.append(t)

    # Deezer
    dz = dz_get_artist_top(artist, limit)
    for t in dz:
        if t["url"] not in seen:
            seen.add(t["url"])
            results.append(t)

    return results[:limit]

def find_track_for_download(track_name, artist_name):
    """Find track URL for download. Chain: SoundCloud → Deezer → Spotify."""

    queries = [
        f"{artist_name} {track_name}",
        f"{track_name} {artist_name}",
        track_name,
        artist_name,
    ]

    for query in queries:
        # 1. SoundCloud
        try:
            result = sc_search(query, limit=5)
            if result:
                for r in result:
                    title_lower = r["title"].lower()
                    if track_name.lower() in title_lower or artist_name.lower() in title_lower:
                        return {
                            "title": r["title"],
                            "url": r["url"],
                            "source": "soundcloud",
                        }
                return {
                    "title": result[0]["title"],
                    "url": result[0]["url"],
                    "source": "soundcloud",
                }
        except Exception as e:
            logger.warning(f"SC search failed for '{query}': {e}")

        # 2. Deezer
        try:
            dz = dz_search_tracks(query, limit=5)
            if dz:
                for d in dz:
                    title_lower = d["title"].lower()
                    if track_name.lower() in title_lower or artist_name.lower() in title_lower:
                        return {
                            "title": d["title"],
                            "url": d["url"],
                            "source": "deezer",
                        }
                return {
                    "title": dz[0]["title"],
                    "url": dz[0]["url"],
                    "source": "deezer",
                }
        except Exception as e:
            logger.warning(f"Deezer search failed for '{query}': {e}")

        # 3. Spotify
        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            try:
                spotify = search_spotify_tracks(query, limit=5)
                if spotify:
                    for s in spotify:
                        title_lower = s["title"].lower()
                        if track_name.lower() in title_lower or artist_name.lower() in title_lower:
                            return {
                                "title": s["title"],
                                "url": s["url"],
                                "source": "spotify",
                            }
                    return {
                        "title": spotify[0]["title"],
                        "url": spotify[0]["url"],
                        "source": "spotify",
                    }
            except Exception as e:
                logger.warning(f"Spotify search failed for '{query}': {e}")

    logger.error(f"Could not find track: {artist_name} - {track_name}")
    return None

# ─── SPOTIFY: Робота з альбомами ──────────────────────────────────────────────
def extract_spotify_album_id(text):
    text = text.strip()
    if "spotify.com/album/" in text:
        parts = text.split("album/")
        if len(parts) > 1:
            return parts[1].split("?")[0].split("/")[0]
    if len(text) == 22 and text.replace("-", "").replace("_", "").isalnum():
        return text
    return None

def get_spotify_album_info(album_id):
    album = get_spotify_album(album_id)
    if not album:
        return None
    tracks = []
    total_duration_ms = 0
    for track in album.get("tracks", {}).get("items", []):
        dur_ms = track.get("duration_ms", 0)
        total_duration_ms += dur_ms
        tracks.append({
            "name": track.get("name", "Unknown"),
            "artists": ", ".join(
                a.get("name", "") for a in track.get("artists", [])
            ),
            "duration_ms": dur_ms,
            "duration": fmt_dur_ms(dur_ms),
            "track_number": track.get("track_number", 0),
            "spotify_id": track.get("id", ""),
        })
    next_url = album.get("tracks", {}).get("next")
    while next_url:
        try:
            token = get_spotify_token()
            headers = {"Authorization": f"Bearer {token}"}
            resp = requests.get(next_url, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            for track in data.get("items", []):
                dur_ms = track.get("duration_ms", 0)
                total_duration_ms += dur_ms
                tracks.append({
                    "name": track.get("name", "Unknown"),
                    "artists": ", ".join(
                        a.get("name", "") for a in track.get("artists", [])
                    ),
                    "duration_ms": dur_ms,
                    "duration": fmt_dur_ms(dur_ms),
                    "track_number": track.get("track_number", 0),
                    "spotify_id": track.get("id", ""),
                })
            next_url = data.get("next")
        except Exception:
            break
    tracks.sort(key=lambda x: x["track_number"])
    images = album.get("images", [])
    image_url = images[0].get("url", "") if images else ""
    return {
        "id": album_id,
        "name": album.get("name", "Unknown Album"),
        "artist": ", ".join(
            a.get("name", "") for a in album.get("artists", [])
        ),
        "release_date": album.get("release_date", "—"),
        "year": album.get("release_date", "—")[:4]
        if album.get("release_date")
        else "—",
        "total_tracks": album.get("total_tracks", len(tracks)),
        "tracks": tracks,
        "total_duration_ms": total_duration_ms,
        "total_duration": fmt_dur_ms(total_duration_ms),
        "label": album.get("label", "—"),
        "image_url": image_url,
        "album_type": album.get("album_type", "album"),
        "external_url": album.get("external_urls", {}).get(
            "spotify", f"https://open.spotify.com/album/{album_id}"
        ),
    }

def search_spotify_and_format(query, limit=10):
    albums = search_spotify_albums(query, limit)
    results = []
    for album in albums:
        images = album.get("images", [])
        image_url = images[0].get("url", "") if images else ""
        results.append({
            "id": album.get("id", ""),
            "name": album.get("name", "Unknown"),
            "artist": ", ".join(
                a.get("name", "") for a in album.get("artists", [])
            ),
            "release_date": album.get("release_date", "—"),
            "year": album.get("release_date", "—")[:4]
            if album.get("release_date")
            else "—",
            "total_tracks": album.get("total_tracks", 0),
            "image_url": image_url,
        })
    return results

# ─── MusicBrainz API ──────────────────────────────────────────────────────────
def mb_search_album(query, limit=10):
    import urllib.parse
    encoded = urllib.parse.quote(query)
    url = f"https://musicbrainz.org/ws/2/release/?query=release:{encoded}&fmt=json&limit={limit}"
    headers = {"User-Agent": f"MusicLSP/3.0 ({AUTHOR})"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        releases = data.get("releases", [])
        logger.info(f"MusicBrainz '{query}': {len(releases)} results")
        return releases
    except Exception as e:
        logger.error(f"MusicBrainz search error: {e}")
        return []

def mb_get_full_album_info(mbid):
    url = f"https://musicbrainz.org/ws/2/release/{mbid}?inc=recordings+artists+labels&fmt=json"
    headers = {"User-Agent": f"MusicLSP/3.0 ({AUTHOR})"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"MusicBrainz release error: {e}")
        return None
    release_group_id = data.get("release-group", {}).get("id", "")
    image_url = ""
    if release_group_id:
        try:
            cover_url = f"https://coverartarchive.org/release-group/{release_group_id}"
            cover_resp = requests.get(cover_url, headers=headers, timeout=10)
            if cover_resp.status_code == 200:
                cover_data = cover_resp.json()
                images = cover_data.get("images", [])
                if images:
                    image_url = images[0].get("thumbnails", {}).get(
                        "large",
                        images[0].get("thumbnails", {}).get(
                            "small", images[0].get("image", "")
                        ),
                    )
        except Exception:
            pass
    if not image_url:
        try:
            cover_url = f"https://coverartarchive.org/release/{mbid}"
            cover_resp = requests.get(cover_url, headers=headers, timeout=10)
            if cover_resp.status_code == 200:
                cover_data = cover_resp.json()
                images = cover_data.get("images", [])
                if images:
                    image_url = images[0].get("thumbnails", {}).get(
                        "large",
                        images[0].get("thumbnails", {}).get(
                            "small", images[0].get("image", "")
                        ),
                    )
        except Exception:
            pass
    tracks = []
    total_duration_ms = 0
    for medium in data.get("media", []):
        for track in medium.get("tracks", []):
            recording = track.get("recording", {})
            dur_ms = recording.get("length", 0)
            total_duration_ms += dur_ms
            tracks.append({
                "name": recording.get("title", "Unknown"),
                "artists": ", ".join(
                    a.get("name", "")
                    for a in recording.get("artist-credit", [])
                ),
                "duration_ms": dur_ms,
                "duration": fmt_dur_ms(dur_ms),
                "track_number": track.get("number", 0),
            })
    label = "—"
    labels = data.get("label-info", [])
    if labels:
        label = labels[0].get("label", {}).get("name", "—")
    artists = ", ".join(
        a.get("name", "") for a in data.get("artist-credit", [])
    )
    release_date = data.get("date", "—")
    year = release_date[:4] if release_date and len(release_date) >= 4 else "—"
    return {
        "mbid": mbid,
        "name": data.get("title", "Unknown Album"),
        "artist": artists,
        "release_date": release_date,
        "year": year,
        "total_tracks": len(tracks),
        "tracks": tracks,
        "total_duration_ms": total_duration_ms,
        "total_duration": fmt_dur_ms(total_duration_ms),
        "label": label,
        "image_url": image_url,
        "album_type": data.get("release-group", {}).get("primary-type", "album"),
        "external_url": f"https://musicbrainz.org/release/{mbid}",
    }

def mb_format_album(release):
    title = release.get("title", "Unknown")
    artists = ", ".join(
        a.get("name", "") for a in release.get("artist-credit", [])
    )
    date = release.get("date", "—")
    year = date[:4] if date and len(date) >= 4 else "—"
    track_count = 0
    for medium in release.get("media", []):
        track_count += medium.get("track-count", 0)
    return {
        "mbid": release.get("id", ""),
        "name": title,
        "artist": artists,
        "year": year,
        "total_tracks": track_count,
        "release_date": date,
    }

# ─── Асинхронні обгортки ──────────────────────────────────────────────────────
async def async_search(query, limit=10):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, search_all, query, limit)

async def async_artist(artist, limit=50):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, artist_songs, artist, limit)

async def async_download(url, out_dir, quality="192"):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, download_mp3, url, out_dir, quality)

async def async_spotify_album_info(album_id):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, get_spotify_album_info, album_id)

async def async_search_spotify(query, limit=10):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, search_spotify_and_format, query, limit)

async def async_mb_search(query, limit=10):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, mb_search_album, query, limit)

async def async_mb_full_info(mbid):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, mb_get_full_album_info, mbid)

async def async_find_track(track_name, artist_name):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, find_track_for_download, track_name, artist_name)

# ─── Завантаження MP3 — БЕЗ COOKIES ─────────────────────────────────────────
def download_mp3(url, out_dir, quality="192"):
    """Download MP3 from SoundCloud, Deezer or Spotify."""

    base_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": quality,
            }
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 3,
        "fragment_retries": 3,
        "file_access_retries": 3,
        "extractor_retries": 3,
    }

    # Add Deezer ARL cookie if available and URL is from Deezer
    if "deezer.com" in url:
        if DEEZER_ARL:
            base_opts["cookies"] = {"arl": DEEZER_ARL}
            logger.info("Using Deezer ARL cookie for download")
        elif os.path.exists(DEEZER_COOKIES_FILE):
            base_opts["cookiesfrombrowser"] = ("firefox", None, None, DEEZER_COOKIES_FILE)
            logger.info("Using Deezer cookies file for download")

    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info:
            mp3_files = list(Path(out_dir).glob("*.mp3"))
            if mp3_files:
                logger.info(f"Download success: {mp3_files[0].name}")
                return str(mp3_files[0])
            # Конвертація якщо не mp3
            for ext in ["*.m4a", "*.webm", "*.opus", "*.ogg", "*.mp4"]:
                    files = list(Path(out_dir).glob(ext))
            if files:
                    input_file = str(files[0])
                    output_file = os.path.join(out_dir, f"{files[0].stem}.mp3")
                    try:
                        subprocess.run(
                            [
                                "ffmpeg",
                                "-i", input_file,
                                "-vn", "-ar", "44100", "-ac", "2",
                                "-b:a", f"{quality}k", "-y", output_file,
                            ],
                            check=True, capture_output=True, timeout=60,
                        )
                        if os.path.exists(output_file):
                            os.remove(input_file)
                            return output_file
                    except Exception as conv_e:
                        logger.warning(f"FFmpeg conversion failed: {conv_e}")
                        return input_file
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None

async def async_download_with_fallback(url, out_dir, quality="192"):
    result = await async_download(url, out_dir, quality)
    if result:
        return result
    logger.info("Trying alternative download method...")
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _alt_download, url, out_dir, quality)

def _alt_download(url, out_dir, quality="192"):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info:
            for ext in ["*.mp3", "*.m4a", "*.webm", "*.opus", "*.ogg", "*.mp4"]:
                files = list(Path(out_dir).glob(ext))
                if files:
                    return str(files[0])
    except Exception as e:
        logger.warning(f"Alternative download failed: {e}")
    return None

# ─── ZIP-архів для альбому — на диск, не в пам'ять ────────────────────────────
async def create_album_zip(tracks, quality="192", tmp_dir=None):
    if tmp_dir is None:
        tmp_dir = tempfile.mkdtemp()
    zip_path = os.path.join(tmp_dir, "album.zip")
    downloaded = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, track in enumerate(tracks):
            if not track.get("url"):
                continue
            try:
                track_dir = os.path.join(tmp_dir, f"track_{i}")
                os.makedirs(track_dir, exist_ok=True)
                path = await async_download_with_fallback(
                    track["url"], track_dir, quality
                )
                if path and os.path.exists(path):
                    safe_name = f"{i+1:02d}. {track['title'][:50]}.mp3"
                    zf.write(path, safe_name)
                    downloaded += 1
            except Exception as e:
                logger.error(f"ZIP track {i+1} failed: {e}")
                continue
    if downloaded == 0:
        return None
    return zip_path


# ═══════════════════════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ═══════════════════════════════════════════════════════════════════════════════

# ─── Клавіатури ───────────────────────────────────────────────────────────────
def main_kb(uid):
    """Generate main menu keyboard."""
    l = get_lang(uid)
    btn = lambda text, data: InlineKeyboardButton(text, callback_data=data)
    labels = {
        "uk": ["🔍 Пошук", "💿 Альбоми", "📚 Бібліотека", "👤 Профіль", "💎 Підписка", "🎁 Реферал", "⚙️ Налаштування"],
        "ru": ["🔍 Поиск", "💿 Альбомы", "📚 Библиотека", "👤 Профиль", "💎 Подписка", "🎁 Реферал", "⚙️ Настройки"],
        "en": ["🔍 Search", "💿 Albums", "📚 Library", "👤 Profile", "💎 Premium", "🎁 Invite", "⚙️ Settings"],
        "fr": ["🔍 Recherche", "💿 Albums", "📚 Bibliothèque", "👤 Profil", "💎 Premium", "🎁 Inviter", "⚙️ Paramètres"],
    }
    lb = labels.get(l, labels["en"])
    back_labels = {"uk": "◀️ Назад", "ru": "◀️ Назад", "en": "◀️ Back", "fr": "◀️ Retour"}
    return (
        InlineKeyboardMarkup([
            [btn(lb[0], "m:search"), btn(lb[1], "m:albums")],
            [btn(lb[2], "m:library"), btn(lb[3], "m:profile")],
            [btn(lb[4], "m:sub"), btn(lb[5], "m:ref")],
            [btn(lb[6], "m:settings")],
        ]),
        back_labels.get(l, "◀️ Back"),
    )

def back_btn(uid):
    l = get_lang(uid)
    labels = {"uk": "◀️ Назад", "ru": "◀️ Назад", "en": "◀️ Back"}
    return InlineKeyboardButton(
        labels.get(l, "◀️ Back"), callback_data="m:home"
    )

# ─── /start ───────────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Admin panel."""
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ Access denied")
        return
    await _show_admin_panel(update.message, uid)

async def _show_admin_panel(msg, uid):
    """Show admin panel inline."""
    l = get_lang(uid)
    texts = {
        "uk": "🔧 <b>Адмін панель</b>\n\nВибери дію:",
        "ru": "🔧 <b>Админ панель</b>\n\nВыбери действие:",
        "en": "🔧 <b>Admin panel</b>\n\nChoose action:",
        "fr": "🔧 <b>Panneau admin</b>\n\nChoisis l'action:",
    }
    back_labels = {"uk": "◀️ Назад", "ru": "◀️ Назад", "en": "◀️ Back", "fr": "◀️ Retour"}
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💎 Дати Premium", callback_data="adm:premium")],
        [InlineKeyboardButton("💿 Забрати Premium", callback_data="adm:unpremium")],
        [InlineKeyboardButton("📢 Розсилка", callback_data="adm:broadcast")],
        [InlineKeyboardButton("🔍 Знайти юзера", callback_data="adm:find")],
        [InlineKeyboardButton("👥 Останні 20 юзерів", callback_data="adm:users")],
        [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats")],
        [InlineKeyboardButton(back_labels.get(l, "◀️ Back"), callback_data="adm:panel")],
    ])
    try:
        await msg.edit_text(texts.get(l, texts["en"]), reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.reply_text(texts.get(l, texts["en"]), reply_markup=kb, parse_mode="HTML")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    create_user(uid, username)
    u = get_user(uid)
    if u and u["lang"] and u["lang"] != "uk":
        await show_welcome(update.message, uid)
        return
    keyboard = []
    row = []
    for code, name in LANGUAGES.items():
        row.append(InlineKeyboardButton(name, callback_data=f"lang:{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    await update.message.reply_text(
        "🌍 <b>Choose your language / Оберіть мову</b>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

async def show_welcome(msg, uid):
    l = get_lang(uid)
    text = tx("welcome", l, bot=BOT_NAME, author=AUTHOR)
    kb, _ = main_kb(uid)
    await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ─── Callbacks ────────────────────────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    l = get_lang(uid)

    if data.startswith("lang:"):
        set_lang(uid, data[5:])
        try:
            await q.message.delete()
        except Exception:
            pass
        await show_welcome(q.message, uid)
        return

    if data == "m:home":
        kb, _ = main_kb(uid)
        try:
            await q.message.edit_text(
                f"🏠 <b>{BOT_NAME}</b>\n\nОбери дію 👇",
                reply_markup=kb,
                parse_mode="HTML",
            )
        except Exception:
            await q.message.reply_text(
                f"🏠 <b>{BOT_NAME}</b>\n\nОбери дію 👇",
                reply_markup=kb,
                parse_mode="HTML",
            )
        return

    if data == "m:search":
        set_state(uid, "searching")
        prompts = {
            "uk": "🔍 Введи назву пісні або артиста:",
            "ru": "🔍 Введи название песни или артиста:",
            "en": "🔍 Enter song name or artist:",
            "fr": "🔍 Entre le nom de la chanson ou l'artiste:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    if data == "m:albums":
        set_state(uid, "album_search")
        prompts = {
            "uk": "💿 Введи назву альбому:\n\n<i>Приклади:</i>\n* <code>Yanix SS 20</code>",
            "ru": "💿 Введи название альбома:\n\n<i>Примеры:</i>\n* <code>Yanix SS 20</code>",
            "en": "💿 Enter album name:\n\n<i>Examples:</i>\n* <code>Yanix SS 20</code>",
            "fr": "💿 Entre le nom de l'album:\n\n<i>Exemples:</i>\n* <code>Yanix SS 20</code>",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    if data == "m:library":
        await show_library(q.message, uid, ctx)
        return

    if data == "m:profile":
        await show_profile(q.message, uid)
        return

    if data == "m:sub":
        await show_sub(q.message, uid, ctx)
        return

    if data == "m:ref":
        await show_ref(q.message, uid, ctx)
        return

    if data == "m:settings":
        await show_settings(q.message, uid, ctx)
        return

    if data == "m:genres":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_genres(q.message, uid, ctx)
        return

    if data.startswith("genre|"):
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        genre_key = data.split("|")[1]
        await search_by_genre(q.message, genre_key, uid, ctx)
        return

    # Premium menu
    if data == "m:premium_menu":
        await show_sub(q.message, uid, ctx)
        return

    # Admin callbacks
    if data == "adm:panel":
        await _show_admin_panel(q.message, uid)
        return

    if data.startswith("adm:"):
        if uid != ADMIN_ID:
            await q.answer("⛔ No access", show_alert=True)
            return

        if data == "adm:premium":
            set_state(uid, "adm:premium")
            await q.message.edit_text("🔧 Введи ID користувача для Premium:", reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]))
            return

        if data == "adm:unpremium":
            set_state(uid, "adm:unpremium")
            await q.message.edit_text("🔧 Введи ID користувача для скасування Premium:", reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]))
            return

        if data == "adm:broadcast":
            set_state(uid, "adm:broadcast")
            await q.message.edit_text("📢 Введи текст для розсилки:", reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]))
            return

        if data == "adm:find":
            set_state(uid, "adm:find")
            await q.message.edit_text("🔍 Введи ID або @username:", reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]))
            return

        if data == "adm:users":
            recent = get_recent_users(20)
            text = "👥 <b>Останні 20 юзерів:</b>\n\n"
            for i, u in enumerate(recent, 1):
                status = "💎" if u.get("is_premium") or u.get("is_premium") == 1 else "💿"
                name = u.get("username", "—")
                joined = str(u.get("joined", "—"))[:10]
                text += f"{i}. {status} <code>{u['id']}</code> @{name} ({joined})\n"
            await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm:panel")]]), parse_mode="HTML")
            return

        if data == "adm:stats":
            stats = get_global_stats()
            text = f"📊 <b>Статистика</b>\n\n👥 Всього: {stats['total']}\n💎 Premium: {stats['premium']}\n💿 Free: {stats['free']}"
            await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm:panel")]]), parse_mode="HTML")
            return

    # ZIP Albums (Premium)
    if data == "m:zip_albums":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        set_state(uid, "zip_album_search")
        prompts = {
            "uk": "📦 Введи назву альбому для ZIP:",
            "ru": "📦 Введи название альбома для ZIP:",
            "en": "📦 Enter album name for ZIP:",
            "fr": "📦 Entre le nom de l'album pour ZIP:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    # Playlists (Premium)
    if data == "m:playlists":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_playlists_menu(q.message, uid, ctx)
        return

    # Radio (Premium)
    if data == "m:radio":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        set_state(uid, "radio_input")
        prompts = {
            "uk": "📻 Введи артиста або пісню для радіо:",
            "ru": "📻 Введи артиста или песню для радио:",
            "en": "📻 Enter artist or song for radio:",
            "fr": "📻 Entre un artiste ou une chanson pour la radio:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return



    # Statistics (Premium)
    if data == "m:stats":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_stats(q.message, uid)
        return

    # Lyrics (Premium)
    
        set_state(uid, "lyrics_input")
        prompts = {
            "uk": "🎤 Введи назву пісні та артиста для тексту:",
            "ru": "🎤 Введи название песни и артиста для текста:",
            "en": "🎤 Enter song name and artist for lyrics:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    # AI Recommendations (Premium) — перейменовано на "Схожа музика"
    if data == "m:ai_recommend":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        set_state(uid, "ai_recommend_input")
        prompts = {
            "uk": "🤖 Введи артиста або жанр для пошуку схожої музики:",
            "ru": "🤖 Введи артиста или жанр для поиска похожей музыки:",
            "en": "🤖 Enter artist or genre to find similar music:",
            "fr": "🤖 Entre un artiste ou un genre pour trouver de la musique similaire:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    # Retry download
    if data.startswith("retry_dl|"):
        url_id = data.split("|")[1]
        cached = get_cached_url(ctx.bot_data, url_id)
        url = cached.get("url", "")
        title = cached.get("title", "трек")
        artist = cached.get("artist", "")
        if not url:
            await q.message.reply_text("❌ Посилання застаріло. Спробуй знайти знову.")
            return
        await do_download(q.message, url, title, artist, uid, ctx)
        return

    if data.startswith("dl|"):
        parts = data.split("|", 2)
        idx, ck = int(parts[1]), parts[2]
        tracks = ctx.bot_data.get("cache", {}).get(ck, [])
        if not tracks or idx >= len(tracks):
            await q.message.reply_text("❌ Застарів результат. Шукай знову.")
            return
        t = tracks[idx]
        await do_download(q.message, t["url"], t["title"], t.get("channel", ""), uid, ctx)
        return

    if data.startswith("dlurl|"):
        parts = data.split("|", 3)
        url_id = parts[1]
        title = parts[2] if len(parts) > 2 else "трек"
        artist = parts[3] if len(parts) > 3 else ""
        cached = get_cached_url(ctx.bot_data, url_id)
        url = cached.get("url", "")
        if not url:
            await q.message.reply_text("❌ Посилання застаріло. Спробуй знайти знову.")
            return
        await do_download(q.message, url, title, artist, uid, ctx)
        return

    # Spotify альбом
    if data.startswith("sp_album|"):
        album_id = data.split("|", 1)[1]
        await show_spotify_album(q.message, album_id, uid, ctx)
        return

    if data.startswith("sp_track|"):
        parts = data.split("|", 2)
        album_ck = parts[1]
        track_idx = int(parts[2])
        album_data = ctx.bot_data.get("spotify_album_cache", {}).get(album_ck)
        if not album_data or track_idx >= len(album_data.get("tracks", [])):
            await q.message.reply_text("❌ Дані альбому застаріли. Шукай знову.")
            return
        track = album_data["tracks"][track_idx]
        status = await q.message.reply_text(
            f"🔍 Шукаю: <b>{track['name']}</b>…", parse_mode="HTML"
        )
        result = await async_find_track(track["name"], track["artists"])
        if not result:
            await status.edit_text(
                f"😔 Не знайдено: <b>{track['name']}</b>", parse_mode="HTML"
            )
            return
        await status.delete()
        await do_download(
            q.message, result["url"], track["name"], track["artists"], uid, ctx
        )
        return

    if data == "sp_albumzip":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        album_ck = ctx.bot_data.get("last_spotify_album_ck", "")
        album_data = ctx.bot_data.get("spotify_album_cache", {}).get(album_ck)
        if not album_data:
            await q.message.reply_text("❌ Дані альбому застаріли.")
            return
        await do_download_spotify_album_zip(q.message, album_data, uid, ctx)
        return

    if data == "artist_input":
        limits = get_limits(uid)
        max_songs = limits["artist_songs"]
        if max_songs == 0:
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        set_state(uid, "artist_input")
        prompts = {
            "uk": f"🎤 Введи ім'я артиста (макс {max_songs} пісень):",
            "ru": f"🎤 Введи имя артиста (макс {max_songs} песен):",
            "en": f"🎤 Enter artist name (max {max_songs} songs):",
            "fr": f"🎤 Entre le nom de l'artiste (max {max_songs} chansons):",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    if data == "dl20_input":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        set_state(uid, "dl20_input")
        prompts = {
            "uk": "⬇️ Введи ім'я артиста для завантаження пісень:",
            "ru": "⬇️ Введи имя артиста для скачивания песен:",
            "en": "⬇️ Enter artist name to download songs:",
            "fr": "⬇️ Entre le nom de l'artiste pour télécharger:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    if data.startswith("addlib|"):
        url_id = data[7:]
        cached = get_cached_url(ctx.bot_data, url_id)
        url = cached.get("url", "")
        title = cached.get("title", "Unknown")
        artist = cached.get("artist", "")
        if not url:
            await q.answer("❌ Посилання застаріло.", show_alert=True)
            return
        added, status = add_library(uid, title, artist, url)
        if status == "full":
            await q.answer(
                tx("library_full", l, max=FREE_LIMITS["library_max"]),
                show_alert=True,
            )
        else:
            msg = "✅ Додано до бібліотеки!" if added else "ℹ️ Вже є в бібліотеці."
            await q.answer(msg, show_alert=True)
        return

    if data.startswith("libdel|"):
        del_library(uid, int(data[7:]))
        await show_library(q.message, uid, ctx)
        return

    if data.startswith("searchp|"):
        parts = data.split("|", 2)
        query = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await do_search_paged(q.message, query, uid, ctx, page, edit=True)
        return

    if data.startswith("quality|"):
        q_val = data[8:]
        if not is_premium(uid) and q_val != "192":
            await q.answer("⛔ Тільки 192kbps у Free версії!", show_alert=True)
            return
        ctx.bot_data.setdefault("quality", {})[uid] = q_val
        await q.answer(f"✅ Якість: {q_val} kbps", show_alert=True)
        return

    # MusicBrainz альбом
    if data.startswith("mb_album|"):
        mbid = data.split("|", 1)[1]
        await show_mb_album(q.message, mbid, uid, ctx)
        return

    if data.startswith("mb_track|"):
        parts = data.split("|", 2)
        album_ck = parts[1]
        track_idx = int(parts[2])
        album_data = ctx.bot_data.get("mb_album_cache", {}).get(album_ck)
        if not album_data or track_idx >= len(album_data.get("tracks", [])):
            await q.message.reply_text("❌ Дані альбому застаріли. Шукай знову.")
            return
        track = album_data["tracks"][track_idx]
        status = await q.message.reply_text(
            f"🔍 Шукаю: <b>{track['name']}</b>…", parse_mode="HTML"
        )
        result = await async_find_track(track["name"], track["artists"])
        if not result:
            await status.edit_text(
                f"😔 Не знайдено: <b>{track['name']}</b>", parse_mode="HTML"
            )
            return
        await status.delete()
        await do_download(
            q.message, result["url"], track["name"], track["artists"], uid, ctx
        )
        return

    if data == "mb_albumzip":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        album_ck = ctx.bot_data.get("last_mb_album_ck", "")
        album_data = ctx.bot_data.get("mb_album_cache", {}).get(album_ck)
        if not album_data:
            await q.message.reply_text("❌ Дані альбому застаріли.")
            return
        await do_download_mb_album_zip(q.message, album_data, uid, ctx)
        return

    # Playlist callbacks
    if data.startswith("pl_view|"):
        pid = int(data.split("|")[1])
        await show_playlist(q.message, pid, uid, ctx)
        return

    if data.startswith("pl_del|"):
        pid = int(data.split("|")[1])
        delete_playlist(uid, pid)
        await show_playlists_menu(q.message, uid, ctx)
        return

    if data.startswith("pl_trackdel|"):
        parts = data.split("|", 2)
        pid, tid = int(parts[1]), int(parts[2])
        delete_playlist_track(pid, tid)
        await show_playlist(q.message, pid, uid, ctx)
        return

    if data == "pl_create":
        set_state(uid, "playlist_create")
        prompts = {
            "uk": "📋 Введи назву нового плейлиста:",
            "ru": "📋 Введи название нового плейлиста:",
            "en": "📋 Enter new playlist name:",
        }
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return

    if data.startswith("pl_addtrack|"):
        pid = int(data.split("|")[1])
        set_state(uid, f"playlist_addtrack:{pid}")
        prompts = {
            "uk": "🎵 Введи назву пісні для додавання:",
            "ru": "🎵 Введи название песни для добавления:",
            "en": "🎵 Enter song name to add:",
        }
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return

    # Radio callbacks
    if data.startswith("radio_next|"):
        rid = int(data.split("|")[1])
        session, tracks = get_radio_session(rid)
        if not session:
            return
        idx = session["current_idx"] + 1
        if idx >= len(tracks):
            await q.message.reply_text("📻 Радіо сесія закінчилась. Почни нову!")
            return
        update_radio_idx(rid, idx)
        track = tracks[idx]
        await do_download(
            q.message, track["url"], track["title"], track.get("artist", ""), uid, ctx
        )
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Наступна", callback_data=f"radio_next|{rid}"), back_btn(uid)]
        ])
        await q.message.reply_text(
            f"📻 Радіо: {idx+1}/{len(tracks)}", reply_markup=kb, parse_mode="HTML"
        )
        return

    # Batch download size selection
    if data.startswith("batch_size|"):
        size = int(data.split("|")[1])
        set_state(uid, f"batch_download:{size}")
        prompts = {
            "uk": f"⬇️ Введи ім'я артиста для завантаження {size} пісень:",
            "ru": f"⬇️ Введи имя артиста для скачивания {size} песен:",
            "en": f"⬇️ Enter artist name to download {size} songs:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return


# ─── Повідомлення ─────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip() if update.message.text else ""
    l = get_lang(uid)
    state = get_state(uid)

    if not get_user(uid):
        create_user(uid, update.effective_user.username or "")

    # Artist — all songs
    if state == "artist_input":
        set_state(uid, "")
        limits = get_limits(uid)
        max_songs = limits["artist_songs"]
        if max_songs == 0:
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_artist(update.message, text, uid, ctx, max_songs)
        return

    # Batch download
    if state.startswith("batch_download:"):
        set_state(uid, "")
        size = int(state.split(":")[1])
        await batch_download(update.message, text, uid, ctx, size)
        return

    if state == "dl20_input":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await batch_download(update.message, text, uid, ctx, 20)
        return

    # Album search
    if state == "album_search":
        set_state(uid, "")
        await do_mb_album_search(update, text, uid, ctx)
        return

    # ZIP album search
    if state == "zip_album_search":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await do_zip_album_search(update, text, uid, ctx)
        return

    # Radio
    if state == "radio_input":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await start_radio(update.message, text, uid, ctx)
        return

    # AI Recommend
    if state == "ai_recommend_input":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await ai_recommend(update.message, text, uid, ctx)
        return

    # Playlist create
    if state == "playlist_create":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        pid = create_playlist(uid, text)
        texts = {
            "uk": f"✅ Плейлист '<b>{text}</b>' створено!",
            "ru": f"✅ Плейлист '<b>{text}</b>' создан!",
            "en": f"✅ Playlist '<b>{text}</b>' created!",
            "fr": f"✅ Playlist '<b>{text}</b>' créée!",
        }
        await update.message.reply_text(texts.get(l, texts["en"]), parse_mode="HTML")
        await show_playlists_menu(update.message, uid, ctx)
        return

    # Playlist add track
    if state.startswith("playlist_addtrack:"):
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        pid = int(state.split(":")[1])
        tracks = await async_search(text, limit=5)
        if not tracks:
            await update.message.reply_text("😔 " + {"uk":"Нічого не знайдено","ru":"Ничего не найдено","en":"Nothing found","fr":"Rien trouvé"}.get(l, "Nothing found"))
            return
        tr = tracks[0]
        add_track_to_playlist(pid, tr["title"], tr.get("channel", ""), tr["url"], tr["duration"])
        texts = {
            "uk": f"✅ <b>{tr['title']}</b> додано до плейлиста!",
            "ru": f"✅ <b>{tr['title']}</b> добавлено в плейлист!",
            "en": f"✅ <b>{tr['title']}</b> added to playlist!",
            "fr": f"✅ <b>{tr['title']}</b> ajouté à la playlist!",
        }
        await update.message.reply_text(texts.get(l, texts["en"]), parse_mode="HTML")
        await show_playlist(update.message, pid, uid, ctx)
        return

    # Admin input
    if uid == ADMIN_ID and state.startswith("adm:"):
        await handle_admin_input(update, ctx, state, text)
        return

    # Default search
    set_state(uid, "")
    await do_search_paged(update, text, uid, ctx, page=0, edit=False)



# ─── Пошук з пагінацією ───────────────────────────────────────────────────────
async def do_search_paged(update_or_msg, query, uid, ctx, page=0, edit=False):
    l = get_lang(uid)
    if edit:
        msg = update_or_msg
        await msg.edit_text(f"🔍 <b>{query}</b> (стор. {page+1})…", parse_mode="HTML")
    else:
        msg = await update_or_msg.message.reply_text(
            f"🔍 <b>{query}</b>…", parse_mode="HTML"
        )
    all_tracks = await async_search(query, limit=30)
    if not all_tracks:
        await msg.edit_text("😔 Нічого не знайдено.")
        return
    ck = f"search_{uid}_{msg.message_id}"
    ctx.bot_data.setdefault("cache", {})[ck] = all_tracks
    start = page * SEARCH_PER_PAGE
    end = start + SEARCH_PER_PAGE
    tracks = all_tracks[start:end]
    has_more = len(all_tracks) > end
    kb = []
    for i, t in enumerate(tracks):
        global_idx = start + i
        icon = "🎵" if t.get("source") == "soundcloud" else "🟣" if t.get("source") == "deezer" else "🟢"
        kb.append([
            InlineKeyboardButton(
                f"{icon} {t['title'][:42]} ({t['duration']})",
                callback_data=f"dl|{global_idx}|{ck}"
            )
        ])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Попередня", callback_data=f"searchp|{query}|{page-1}"
        ))
    if has_more:
        nav.append(InlineKeyboardButton(
            "➡️ Наступна", callback_data=f"searchp|{query}|{page+1}"
        ))
    if nav:
        kb.append(nav)
    if tracks:
        limits = get_limits(uid)
        max_songs = limits["artist_songs"]
        dl_label = f"⬇️ Скачати {limits['batch_options'][0]} пісень" if limits["batch_options"] else "⬇️ Batch (Premium)"
        all_label = f"🎤 Всі пісні ({max_songs})" if max_songs > 0 else "🎤 Всі пісні (Premium)"
        kb.append([
            InlineKeyboardButton(all_label, callback_data="artist_input"),
            InlineKeyboardButton(dl_label, callback_data="dl20_input" if is_premium(uid) else "m:sub"),
        ])
    kb.append([back_btn(uid)])
    text = f"🎶 <b>{query}</b> — {start+1}-{min(end, len(all_tracks))} з {len(all_tracks)}\n\nОбери пісню 👇"
    if edit:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    else:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── MusicBrainz: Пошук альбомів ──────────────────────────────────────────────
async def do_mb_album_search(update, query, uid, ctx):
    msg = await update.message.reply_text(
        f"💿 Шукаю в MusicBrainz: <b>{query}</b>…", parse_mode="HTML"
    )
    releases = await async_mb_search(query, limit=10)
    if not releases:
        await msg.edit_text(
            "😔 Альбоми не знайдено.\n\n"
            "💡 Спробуй:\n"
            "* Точнішу назву: <code>Yanix SS 20</code>\n"
            "* Формат: <code>Артист НазваАльбому</code>",
            parse_mode="HTML"
        )
        return
    kb = []
    for rel in releases[:8]:
        info = mb_format_album(rel)
        name = info["name"][:35]
        artist = info["artist"][:25]
        year = info["year"]
        tracks_count = info["total_tracks"]
        kb.append([
            InlineKeyboardButton(
                f"💿 {name} — {artist} ({year}, {tracks_count} треків)",
                callback_data=f"mb_album|{info['mbid']}"
            )
        ])
    kb.append([back_btn(uid)])
    await msg.edit_text(
        f"🎵 <b>Результати MusicBrainz:</b> «{query}»\n\nОбери альбом 👇",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )

# ─── MusicBrainz: Показати альбом ─────────────────────────────────────────────

async def show_spotify_album(msg, album_id, uid, ctx):
    """Show Spotify album info."""
    l = get_lang(uid)
    status = await msg.reply_text("💿 Завантажую інформацію…", parse_mode="HTML")

    album = await async_spotify_album_info(album_id)
    if not album:
        await status.edit_text("❌ Не вдалося отримати дані.")
        return

    ck = hashlib.md5(f"{uid}_{album_id}".encode()).hexdigest()[:8]
    ctx.bot_data.setdefault("spotify_album_cache", {})[ck] = album
    ctx.bot_data["last_spotify_album_ck"] = ck

    text = f"📀 <b>{album['name']}</b>\n\n🎤 {album['artist']}\n📅 {album['year']}\n🎵 {album['total_tracks']} треків"

    kb = []
    for i, track in enumerate(album["tracks"][:15]):
        kb.append([InlineKeyboardButton(
            f"▶️ {i+1}. {track['name'][:35]}",
            callback_data=f"sp_track|{ck}|{i}"
        )])

    kb.append([InlineKeyboardButton("📦 ZIP", callback_data="sp_albumzip")])
    kb.append([back_btn(uid)])

    await status.delete()
    await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def show_mb_album(msg, mbid, uid, ctx):
    status = await msg.reply_text("💿 Завантажую інформацію…", parse_mode="HTML")
    album = await async_mb_full_info(mbid)
    if not album:
        text = "❌ Не вдалося отримати дані. Спробуй інший альбом."
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    ck = hashlib.md5(f"{uid}_{mbid}".encode()).hexdigest()[:8]
    ctx.bot_data.setdefault("mb_album_cache", {})[ck] = album
    ctx.bot_data["last_mb_album_ck"] = ck

    text = (
        f"📀 <b>{album['name']}</b>\n\n"
        f"🎤 <b>Виконавець:</b> {album['artist']}\n"
        f"📅 <b>Рік:</b> {album['year']}\n"
        f"🏷 <b>Лейбл:</b> {album['label']}\n"
        f"🎵 <b>Треків:</b> {album['total_tracks']}\n"
        f"⏱ <b>Тривалість:</b> {album['total_duration']}\n\n"
        f"🎧 <b>Треки:</b>\n"
    )
    kb = []
    for i, track in enumerate(album["tracks"]):
        text += f"{i+1}. {track['name']} — {track['duration']}\n"
        kb.append([
            InlineKeyboardButton(
                f"▶️ {i+1}. {track['name'][:35]} ({track['duration']})",
                callback_data=f"mb_track|{ck}|{i}"
            )
        ])
    l = get_lang(uid)
    zip_label = {"uk":"📦 Завантажити ZIP","ru":"📦 Скачать ZIP","en":"📦 Download ZIP"}.get(l, "📦 Download ZIP")
    kb.append([InlineKeyboardButton(zip_label, callback_data="mb_albumzip")])
    kb.append([back_btn(uid)])

    try:
        await status.delete()
    except Exception:
        pass

    image_url = album.get("image_url", "")
    if image_url:
        try:
            await msg.reply_photo(
                photo=image_url,
                caption=text[:1024],
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.error(f"MB Photo send failed: {e}")
            try:
                await msg.reply_document(
                    document=image_url,
                    caption=text[:1024],
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="HTML"
                )
                return
            except Exception as e2:
                logger.error(f"MB Document send also failed: {e2}")

    await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── MusicBrainz: Завантажити ZIP ─────────────────────────────────────────────

# ─── Основна функція завантаження ────────────────────────────────────────────
async def do_download(msg, url, title, artist, uid, ctx):
    """Download track and send to user."""
    l = get_lang(uid)
    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)

    txts = {
        "uk": {"search": "🎵 Шукаю трек...", "dl": "⚡ Завантажую...", "send": "📀 Відправляю...", "done": "🎉 Готово!", "err": "💔 Не вийшло", "big": "😤 Завеликий файл"},
        "ru": {"search": "🎵 Ищу трек...", "dl": "⚡ Качаю...", "send": "📀 Отправляю...", "done": "🎉 Готово!", "err": "💔 Не вышло", "big": "😤 Слишком большой"},
        "en": {"search": "🎵 Finding track...", "dl": "⚡ Downloading...", "send": "📀 Sending...", "done": "🎉 Done!", "err": "💔 Failed", "big": "😤 Too big"},
        "fr": {"search": "🎵 Cherche le morceau...", "dl": "⚡ Télécharge...", "send": "📀 Envoie...", "done": "🎉 Terminé!", "err": "💔 Raté", "big": "😤 Trop gros"},
    }
    t = txts.get(l, txts["en"])

    status = await msg.reply_text(t["search"], parse_mode="HTML")

    with tempfile.TemporaryDirectory() as tmp:
        try:
            await status.edit_text(t["dl"], parse_mode="HTML")
            path = await async_download_with_fallback(url, tmp, quality)
            if not path or not os.path.exists(path):
                await status.edit_text(t["err"])
                return

            size_mb = os.path.getsize(path) / 1024 / 1024
            if size_mb > MAX_MB:
                await status.edit_text(t["big"])
                return

            await status.edit_text(t["send"], parse_mode="HTML")

            with open(path, "rb") as f:
                await msg.reply_audio(
                    audio=f,
                    title=title[:64],
                    performer=artist[:64],
                    filename=f"{title[:50]}.mp3"
                )

            await status.edit_text(t["done"])
            add_history(uid, title, artist)
            add_listening_stat(uid, title, artist, 0, "download")

            url_id = cache_url(ctx.bot_data, url, title, artist)
            add_txts = {
                "uk": "📚 Додати в бібліотеку",
                "ru": "📚 Добавить в библиотеку",
                "en": "📚 Add to library",
                "fr": "📚 Ajouter à la bibliothèque",
            }
            add_txt = add_txts.get(l, add_txts["en"])
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton(add_txt, callback_data=f"addlib|{url_id}")],
                [back_btn(uid)]
            ])
            await msg.reply_text("", reply_markup=kb)

        except Exception as e:
            logger.error(f"Download error: {e}")
            await status.edit_text(t["err"])

async def do_download_spotify_album_zip(msg, album_data, uid, ctx):
    """Download Spotify album as ZIP."""
    l = get_lang(uid)
    status = await msg.reply_text("⬇️ Завантажую альбом…", parse_mode="HTML")

    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    tracks_with_url = []

    for i, track in enumerate(album_data["tracks"][:10]):
        await status.edit_text(f"🔍 {i+1}/{len(album_data['tracks'])}: {track['name']}…", parse_mode="HTML")
        result = await async_find_track(track["name"], track["artists"])
        if result:
            tracks_with_url.append({
                "title": f"{track['artists']} — {track['name']}",
                "url": result["url"],
            })
        await asyncio.sleep(0.3)

    if not tracks_with_url:
        await status.edit_text("😔 Не знайдено жодного трека.")
        return

    await status.edit_text(f"⬇️ Завантажую {len(tracks_with_url)} треків…")

    tmp_dir = tempfile.mkdtemp()
    zip_path = await create_album_zip(tracks_with_url, quality, tmp_dir)

    if not zip_path:
        await status.edit_text("❌ Помилка створення архіву.")
        return

    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    if size_mb > 2000:
        await status.edit_text(f"❌ Архів завеликий ({size_mb:.1f} МБ).")
        return

    await status.edit_text("📤 Відправляю ZIP…")
    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]

    await msg.reply_document(
        document=open(zip_path, 'rb'),
        filename=f"{safe_name}.zip",
        caption=f"💿 {album_data['name']}\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків",
        parse_mode="HTML"
    )
    await status.delete()
    os.unlink(zip_path)

async def do_download_mb_album_zip(msg, album_data, uid, ctx):
    l = get_lang(uid)
    status = await msg.reply_text(
        f"⬇️ Завантажую альбом: <b>{album_data['name']}</b>\n"
        f"<i>Шукаю треки: SoundCloud → Spotify…</i>",
        parse_mode="HTML"
    )
    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    tracks_with_url = []
    total = len(album_data["tracks"])
    for i, track in enumerate(album_data["tracks"]):
        await status.edit_text(
            f"🔍 {i+1}/{total}: <b>{track['name']}</b>…",
            parse_mode="HTML"
        )
        result = await async_find_track(track["name"], track["artists"])
        if result:
            tracks_with_url.append({
                "title": f"{track['artists']} — {track['name']}",
                "url": result["url"],
                "source": result["source"],
            })
        await asyncio.sleep(0.3)
    if not tracks_with_url:
        await status.edit_text("😔 Не знайдено жодного трека.")
        return
    sources = ", ".join(set(t["source"] for t in tracks_with_url))
    await status.edit_text(
        f"⬇️ Завантажую {len(tracks_with_url)}/{total} треків…\n"
        f"<i>Джерела: {sources}</i>\n"
        f"<i>Формується ZIP…</i>",
        parse_mode="HTML"
    )

    tmp_dir = tempfile.mkdtemp()
    zip_path = await create_album_zip(tracks_with_url, quality, tmp_dir)
    if not zip_path:
        await status.edit_text("❌ Помилка створення архіву.")
        return

    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    if size_mb > 2000:
        await status.edit_text(f"❌ Архів {size_mb:.1f} МБ — завеликий.")
        return

    await status.edit_text("📤 Відправляю ZIP…")
    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]

    thumb = album_data.get("image_url", "")
    if thumb:
        try:
            thumb_resp = requests.get(thumb, timeout=10)
            if thumb_resp.status_code == 200:
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_thumb:
                    tmp_thumb.write(thumb_resp.content)
                    tmp_thumb.flush()
                    await msg.reply_document(
                        document=open(zip_path, 'rb'),
                        thumbnail=tmp_thumb.name,
                        filename=f"{safe_name}.zip",
                        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків\n📍 Джерела: {sources}",
                        parse_mode="HTML"
                    )
                    os.unlink(tmp_thumb.name)
                    await status.delete()
                    os.unlink(zip_path)
                    return
        except Exception as e:
            logger.warning(f"MB ZIP thumbnail failed: {e}")

    await msg.reply_document(
        document=open(zip_path, 'rb'),
        filename=f"{safe_name}.zip",
        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків\n📍 Джерела: {sources}",
        parse_mode="HTML"
    )
    await status.delete()
    os.unlink(zip_path)

# ─── ZIP альбом пошук (для Premium меню) ──────────────────────────────────────
async def do_zip_album_search(update, query, uid, ctx):
    msg = await update.message.reply_text(
        f"📦 Шукаю альбом для ZIP: <b>{query}</b>…", parse_mode="HTML"
    )
    spotify_results = await async_search_spotify(query, limit=5)
    mb_results = await async_mb_search(query, limit=5)

    kb = []
    for album in spotify_results[:3]:
        kb.append([
            InlineKeyboardButton(
                f"🟢 Spotify: {album['name'][:30]} — {album['artist'][:20]}",
                callback_data=f"sp_album|{album['id']}"
            )
        ])
    for rel in mb_results[:3]:
        info = mb_format_album(rel)
        kb.append([
            InlineKeyboardButton(
                f"🔴 MusicBrainz: {info['name'][:30]} — {info['artist'][:20]}",
                callback_data=f"mb_album|{info['mbid']}"
            )
        ])
    kb.append([back_btn(uid)])

    if not kb[:-1]:
        await msg.edit_text("😔 Альбоми не знайдено.")
        return

    await msg.edit_text(
        "📦 <b>Обери альбом для ZIP:</b>\n\n"
        "🟢 — Spotify (краща якість метаданих)\n"
        "🔴 — MusicBrainz (більше незалежної музики)",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )

# ─── Всі пісні артиста ────────────────────────────────────────────────────────
async def show_artist(msg, artist, uid, ctx, max_songs=10):
    """Show all songs by artist."""
    l = get_lang(uid)
    texts = {
        "uk": {"searching": "🔍 Шукаю", "empty": "😔 Нічого не знайдено", "songs": "пісень", "tracks": "треків"},
        "ru": {"searching": "🔍 Ищу", "empty": "😔 Ничего не найдено", "songs": "песен", "tracks": "треков"},
        "en": {"searching": "🔍 Searching", "empty": "😔 Nothing found", "songs": "songs", "tracks": "tracks"},
        "fr": {"searching": "🔍 Recherche", "empty": "😔 Rien trouvé", "songs": "chansons", "tracks": "morceaux"},
    }
    t = texts.get(l, texts["en"])

    status = await msg.reply_text(f"{t['searching']} <b>{artist}</b>…", parse_mode="HTML")
    tracks = await async_artist(artist, max_songs)
    if not tracks:
        await status.edit_text(t['empty'])
        return

    kb = []
    for tr in tracks:
        url_id = cache_url(ctx.bot_data, tr["url"], tr["title"], tr.get("channel", ""))
        kb.append([InlineKeyboardButton(
            f"🎵 {tr['title'][:42]} ({tr['duration']})",
            callback_data=f"dlurl|{url_id}|{tr['title'][:30]}|{tr['channel'][:20]}"
        )])
    kb.append([back_btn(uid)])

    per = 40
    for i, start in enumerate(range(0, len(kb) - 1, per)):
        chunk = kb[start:start+per] + ([kb[-1]] if start + per >= len(kb) - 1 else [])
        end = min(start + per, len(tracks))
        label = f"🎤 <b>{artist}</b> — {len(tracks)} {t['songs']}:"
        if i > 0:
            label = f"🎤 <b>{artist}</b> ({start+1}–{end}):"
        if i == 0:
            await status.edit_text(label, reply_markup=InlineKeyboardMarkup(chunk), parse_mode="HTML")
        else:
            await msg.reply_text(label, reply_markup=InlineKeyboardMarkup(chunk), parse_mode="HTML")

# ─── Batch download — з затримкою для rate limit ──────────────────────────────
async def batch_download(msg, artist, uid, ctx, size=20):
    status = await msg.reply_text(
        f"⬇️ Завантажую {size} пісень <b>{artist}</b>…\n<i>Кілька хвилин</i>",
        parse_mode="HTML"
    )
    tracks = await async_artist(artist, size)
    if not tracks:
        await status.edit_text("😔 Нічого не знайдено.")
        return
    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    ok = 0
    for i, t in enumerate(tracks):
        if i >= size:
            break
        await status.edit_text(
            f"⬇️ {i+1}/{size}: <b>{t['title'][:40]}</b>…", parse_mode="HTML"
        )
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await async_download_with_fallback(t["url"], tmp, quality)
                if path and os.path.exists(path) and os.path.getsize(path) / 1024 / 1024 <= MAX_MB:
                    with open(path, "rb") as f:
                        await msg.reply_audio(
                            audio=f,
                            title=t["title"][:64],
                            performer=t["channel"][:64],
                            filename=f"{t['title'][:50]}.mp3"
                        )
                    add_history(uid, t["title"], t["channel"])
                    add_listening_stat(uid, t["title"], t["channel"], 0, "batch")
                    ok += 1
                    # Rate limit: max 20 msg/min per chat
                    await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Batch: {e}")
    await status.edit_text(f"✅ Завантажено {ok} з {len(tracks)} пісень!")

# ─── Бібліотека ───────────────────────────────────────────────────────────────
async def show_library(msg, uid, ctx):
    """Show user library."""
    songs = get_library(uid)
    l = get_lang(uid)

    texts = {
        "uk": {"title": "📚 Моя бібліотека", "empty": "🎵 Шукай музику та додавай сюди!", "tracks": "треків"},
        "ru": {"title": "📚 Моя библиотека", "empty": "🎵 Ищи музыку и добавляй сюда!", "tracks": "треков"},
        "en": {"title": "📚 My Library", "empty": "🎵 Search music and add here!", "tracks": "tracks"},
        "fr": {"title": "📚 Ma Bibliothèque", "empty": "🎵 Cherche de la musique et ajoute ici!", "tracks": "morceaux"},
    }
    t = texts.get(l, texts["en"])

    if not songs:
        text = f"{t['title']}\n\n{t['empty']}"
        kb = InlineKeyboardMarkup([[back_btn(uid)]])
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except Exception:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return

    kb = []
    for s in songs[:40]:
        icon = "💿" if s["kind"] == "album" else "🎵"
        url_id = cache_url(ctx.bot_data, s["url"], s["title"], s["artist"])
        kb.append([
            InlineKeyboardButton(
                f"{icon} {s['title'][:35]}",
                callback_data=f"dlurl|{url_id}|{s['title'][:30]}|{s['artist'][:20]}"
            ),
            InlineKeyboardButton("🗑", callback_data=f"libdel|{s['id']}")
        ])
    kb.append([back_btn(uid)])

    text = f"{t['title']} — {len(songs)} {t['tracks']}:"
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Профіль ──────────────────────────────────────────────────────────────────
async def show_profile(msg, uid):
    u = get_user(uid)
    stats = get_stats_user(uid)
    status_text = "💎 Premium" if is_premium(uid) else "💿 Free"
    joined = str(u["joined"])[:10] if u and u["joined"] else "—"
    premium_since = str(u["premium_since"])[:10] if u and u["premium_since"] else "—"
    listen_hours = stats["listen_time"] // 3600 if stats["listen_time"] else 0

    text = (
        f"👤 <b>Профіль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 В боті з: {joined}\n"
        f"💎 Статус: <b>{status_text}</b>\n"
    )
    if is_premium(uid) and premium_since != "—":
        text += f"⭐ Premium з: {premium_since}\n"
    text += (
        f"\n📊 <b>Статистика:</b>\n"
        f"* Скачано: {stats['dl']}\n"
        f"* В бібліотеці: {stats['lib']}\n"
        f"* Унікальних артистів: {stats['unique_artists']}\n"
        f"* Час прослуховування: {listen_hours} год"
    )
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")

# ─── Підписка ──────────────────────────────────────────────────────────────────
async def show_sub(msg, uid, ctx=None):
    l = get_lang(uid)
    premium = is_premium(uid)
    status_icon = "✅ Premium" if premium else "💿 Free"

    quality = DEF_QUALITY
    if ctx:
        quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)

    text = (
        f"💎 <b>Підписка</b>\n\n"
        f"Статус: <b>{status_icon}</b>\n\n"
    )

    if not premium:
        text += (
            f"💿 <b>Free:</b>\n"
            f"* Пошук та завантаження MP3 (192kbps)\n"
            f"* Бібліотека: до 20 записів\n"
            f"* Всі пісні артиста: до 10\n\n"
            f"💎 <b>Premium:</b>\n"
            f"* Якість: 192 / 320 kbps\n"
            f"* Бібліотека: необмежана\n"
            f"* ZIP альбоми\n"
            f"* Плейлисти\n"
            f"* Радіо режим\n"
            f"* Розпізнавання музики\n"
            f"* Тексти пісень\n"
            f"* Пошук схожої музики\n"
            f"* Batch download: 20/50/100\n"
            f"* Розширена статистика\n\n"
            f"💰 Оформити Premium: {AUTH_BOT}"
        )
    else:
        text += (
            f"⭐ <b>Premium активовано!</b>\n\n"
            f"Всі функції доступні 👇\n\n"
            f"🎵 Якість: {quality}kbps\n"
            f"📚 Бібліотека: необмежана\n"
            f"📦 ZIP: доступно\n"
            f"📻 Радіо: доступно\n"
            f"🎤 Тексти: доступно\n"
            f"🤖 Схожа музика: доступно"
        )

    kb = []
    if not premium:
        kb.append([InlineKeyboardButton("💳 Оформити Premium", url=f"https://t.me/{AUTH_BOT.replace('@', '')}")])
        kb.append([InlineKeyboardButton("⚙️ Налаштування", callback_data="m:settings")])
    else:
        kb.append([
            InlineKeyboardButton("📦 ZIP Альбоми", callback_data="m:zip_albums"),
            InlineKeyboardButton("📋 Плейлисти", callback_data="m:playlists")
        ])
        kb.append([
            InlineKeyboardButton("📻 Радіо", callback_data="m:radio"),
            InlineKeyboardButton("🎵 Жанри", callback_data="m:genres")
        ])
        kb.append([
            InlineKeyboardButton("📊 Статистика", callback_data="m:stats"),
            InlineKeyboardButton("🤖 Схожа музика", callback_data="m:ai_recommend")
        ])
        kb.append([InlineKeyboardButton("⚙️ Налаштування", callback_data="m:settings")])

    kb.append([back_btn(uid)])

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"show_sub edit_text failed: {e}")
        try:
            await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        except Exception as e2:
            logger.error(f"show_sub reply_text also failed: {e2}")

# ─── Реферал ──────────────────────────────────────────────────────────────────
async def show_ref(msg, uid, ctx):
    """Show referral link."""
    l = get_lang(uid)
    bot_info = await ctx.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={uid}"

    texts = {
        "uk": {"title": "🎁 Запроси друга", "desc": "Поділись посиланням — допоможи боту рости 🚀", "link": "🔗 Твоє посилання"},
        "ru": {"title": "🎁 Пригласи друга", "desc": "Поделись ссылкой — помоги боту расти 🚀", "link": "🔗 Твоя ссылка"},
        "en": {"title": "🎁 Invite a friend", "desc": "Share the link — help the bot grow 🚀", "link": "🔗 Your link"},
        "fr": {"title": "🎁 Invite un ami", "desc": "Partage le lien — aide le bot à grandir 🚀", "link": "🔗 Ton lien"},
    }
    t = texts.get(l, texts["en"])

    text = f"{t['title']}\n\n{t['desc']}\n\n{t['link']}:\n<code>{link}</code>"
    kb = InlineKeyboardMarkup([[back_btn(uid)]])
    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ─── Налаштування ─────────────────────────────────────────────────────────────
async def show_settings(msg, uid, ctx):
    """Show settings menu."""
    l = get_lang(uid)
    cur_q = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)

    texts = {
        "uk": {"title": "⚙️ Налаштування", "lang": "🌍 Мова", "quality": "🎵 Якість"},
        "ru": {"title": "⚙️ Настройки", "lang": "🌍 Язык", "quality": "🎵 Качество"},
        "en": {"title": "⚙️ Settings", "lang": "🌍 Language", "quality": "🎵 Quality"},
        "fr": {"title": "⚙️ Paramètres", "lang": "🌍 Langue", "quality": "🎵 Qualité"},
    }
    t = texts.get(l, texts["en"])

    lang_kb = []
    row = []
    for code, name in LANGUAGES.items():
        row.append(InlineKeyboardButton(name, callback_data=f"lang:{code}"))
        if len(row) == 2:
            lang_kb.append(row)
            row = []
    if row:
        lang_kb.append(row)

    limits = get_limits(uid)
    q_row = []
    for q in limits["quality_options"]:
        q_row.append(InlineKeyboardButton(
            f"{'✅ ' if q==cur_q else ''}{q}kbps",
            callback_data=f"quality|{q}"
        ))

    text = f"{t['title']}\n\n{t['lang']} | {t['quality']}:"
    kb = InlineKeyboardMarkup(lang_kb + [q_row, [back_btn(uid)]])

    try:
        await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ─── Плейлисти меню ───────────────────────────────────────────────────────────
async def show_playlists_menu(msg, uid, ctx):
    """Show playlists menu."""
    l = get_lang(uid)
    playlists = get_playlists(uid)

    texts = {
        "uk": {"title": "📋 Плейлисти", "create": "➕ Створити", "empty": "🎵 Створи свій перший плейлист!", "count": "шт."},
        "ru": {"title": "📋 Плейлисты", "create": "➕ Создать", "empty": "🎵 Создай свой первый плейлист!", "count": "шт."},
        "en": {"title": "📋 Playlists", "create": "➕ Create", "empty": "🎵 Create your first playlist!", "count": "items"},
        "fr": {"title": "📋 Playlists", "create": "➕ Créer", "empty": "🎵 Crée ta première playlist!", "count": "éléments"},
    }
    t = texts.get(l, texts["en"])

    kb = [[InlineKeyboardButton(t['create'], callback_data="pl_create")]]
    for pl in playlists:
        kb.append([
            InlineKeyboardButton(f"📁 {pl['name'][:30]}", callback_data=f"pl_view|{pl['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"pl_del|{pl['id']}")
        ])
    kb.append([back_btn(uid)])

    text = f"{t['title']} — {len(playlists)} {t['count']}"
    if not playlists:
        text += f"\n\n{t['empty']}"

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def show_playlist(msg, pid, uid, ctx):
    pl, tracks = get_playlist(pid)
    l = get_lang(uid)
    if not pl:
        await msg.reply_text("❌ Плейлист не знайдено.")
        return

    kb = []
    text = f"📁 <b>{pl['name']}</b>\n<i>{pl.get('description', '')}</i>\n\n"
    for i, t in enumerate(tracks):
        text += f"{i+1}. {t['title']} — {t['artist']} ({t['duration']})\n"
        url_id = cache_url(ctx.bot_data, t["url"], t["title"], t["artist"])
        kb.append([
            InlineKeyboardButton(
                f"▶️ {i+1}. {t['title'][:35]}",
                callback_data=f"dlurl|{url_id}|{t['title'][:30]}|{t['artist'][:20]}"
            ),
            InlineKeyboardButton("🗑", callback_data=f"pl_trackdel|{pid}|{t['id']}")
        ])

    kb.append([InlineKeyboardButton("➕ Додати трек", callback_data=f"pl_addtrack|{pid}")])
    kb.append([back_btn(uid)])

    try:
        await msg.edit_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Радіо ────────────────────────────────────────────────────────────────────
async def start_radio(msg, seed, uid, ctx):
    status = await msg.reply_text(
        f"📻 Створюю радіо на основі <b>{seed}</b>…", parse_mode="HTML"
    )

    seed_tracks = await async_search(seed, limit=5)
    if not seed_tracks:
        await status.edit_text("😔 Не вдалося створити радіо. Спробуй інший запит.")
        return

    radio_tracks = []
    for t in seed_tracks[:2]:
        similar = await async_artist(t.get("channel", seed), 10)
        radio_tracks.extend(similar)

    random_tracks = await async_search("popular music 2024", limit=10)
    radio_tracks.extend(random_tracks)

    random.shuffle(radio_tracks)
    radio_tracks = radio_tracks[:30]

    if not radio_tracks:
        await status.edit_text("😔 Не вдалося створити радіо.")
        return

    rid = create_radio_session(uid, seed, seed_tracks[0]["title"] if seed_tracks else seed, radio_tracks)

    await status.delete()

    first = radio_tracks[0]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏭ Наступна", callback_data=f"radio_next|{rid}")],
        [back_btn(uid)]
    ])

    await msg.reply_text(
        f"📻 <b>Радіо:</b> {seed}\n\n"
        f"🎵 <b>Зараз грає:</b> {first['title']}\n"
        f"👤 {first.get('channel', '—')}\n"
        f"⏱ {first['duration']}\n\n"
        f"📊 В черзі: {len(radio_tracks)} треків",
        reply_markup=kb,
        parse_mode="HTML"
    )

    await do_download(msg, first["url"], first["title"], first.get("channel", ""), uid, ctx)

# ─── Статистика прослуховування ───────────────────────────────────────────────
async def show_stats(msg, uid):
    l = get_lang(uid)
    s7 = get_listening_stats(uid, 7)
    s30 = get_listening_stats(uid, 30)
    txts = {
        "uk": {"t": "📊 Статистика", "d7": "📅 7 днів", "d30": "📅 30 днів", "tr": "🎵 Треків", "ta": "🎤 Топ артисти", "tt": "🎵 Топ треки", "tm": "разів"},
        "ru": {"t": "📊 Статистика", "d7": "📅 7 дней", "d30": "📅 30 дней", "tr": "🎵 Треков", "ta": "🎤 Топ артисты", "tt": "🎵 Топ треки", "tm": "раз"},
        "en": {"t": "📊 Statistics", "d7": "📅 7 days", "d30": "📅 30 days", "tr": "🎵 Tracks", "ta": "🎤 Top artists", "tt": "🎵 Top tracks", "tm": "times"},
        "fr": {"t": "📊 Statistiques", "d7": "📅 7 jours", "d30": "📅 30 jours", "tr": "🎵 Morceaux", "ta": "🎤 Top artistes", "tt": "🎵 Top morceaux", "tm": "fois"},
    }
    t = txts.get(l, txts["en"])
    nl = chr(10)
    txt = t["t"] + nl + nl
    txt += t["d7"] + ":" + nl + "  * " + t["tr"] + ": <b>" + str(s7["total"]) + "</b>" + nl + nl
    txt += t["d30"] + ":" + nl + "  * " + t["tr"] + ": <b>" + str(s30["total"]) + "</b>" + nl + nl
    if s30["top_artists"]:
        txt += t["ta"] + ":" + nl
        for i, (a, c) in enumerate(s30["top_artists"][:5], 1):
            txt += "  " + str(i) + ". <b>" + str(a) + "</b> - " + str(c) + " " + t["tm"] + nl
        txt += nl
    if s30["top_tracks"]:
        txt += t["tt"] + ":" + nl
        for i, (ti, a, c) in enumerate(s30["top_tracks"][:5], 1):
            txt += "  " + str(i) + ". <b>" + str(ti) + "</b> - " + str(a) + " (" + str(c) + " " + t["tm"] + ")" + nl
    kb = InlineKeyboardMarkup([[back_btn(uid)]])
    try:
        await msg.edit_text(txt[:4096], reply_markup=kb, parse_mode="HTML")
    except Exception:
        await msg.reply_text(txt[:4096], reply_markup=kb, parse_mode="HTML")

async def ai_recommend(msg, query, uid, ctx):
    """Find similar music based on artist or genre."""
    l = get_lang(uid)
    status = await msg.reply_text(f"🤖 Шукаю схожу музику для <b>{query}</b>…", parse_mode="HTML")
    seed_tracks = await async_search(query, limit=5)
    if not seed_tracks:
        await status.edit_text("😔 Не знайдено базовий трек. Спробуй інший запит.")
        return
    seed_artist = seed_tracks[0].get("channel", query)
    similar = await async_artist(seed_artist, limit=20)
    related_queries = [f"{seed_artist} similar", f"like {seed_artist}", f"artists similar to {seed_artist}"]
    for rq in related_queries:
        try:
            extra = await async_search(rq, limit=10)
            for t in extra:
                if t["url"] not in [x["url"] for x in similar]:
                    similar.append(t)
        except Exception:
            pass
    seen = set()
    unique = []
    for t in similar:
        if t["url"] not in seen:
            seen.add(t["url"])
            unique.append(t)
    similar = unique[:20]
    if not similar:
        await status.edit_text("😔 Не знайдено схожої музики.")
        return
    await status.delete()
    ck = f"ai_{uid}_{query}_{msg.message_id if hasattr(msg, 'message_id') else 0}"
    ctx.bot_data.setdefault("cache", {})[ck] = similar
    kb = []
    for i, t in enumerate(similar[:10]):
        icon = "🎵" if t.get("source") == "soundcloud" else "🟣" if t.get("source") == "deezer" else "🟢"
        kb.append([InlineKeyboardButton(f"{icon} {t['title'][:40]} ({t['duration']})", callback_data=f"dl|{i}|{ck}")])
    kb.append([back_btn(uid)])
    text = f"🤖 <b>Схожа музика для:</b> {query}\n🎤 <b>Базовий артист:</b> {seed_artist}\n\nЗнайдено {len(similar)} треків:\n\nОбери пісню 👇"
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # Хендлери команд
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))

    # Хендлери callback
    app.add_handler(CallbackQueryHandler(on_callback))

    # Хендлери повідомлень
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("🚀 MusicLSP v3.0 запускається...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
