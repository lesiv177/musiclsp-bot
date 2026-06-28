# -*- coding: utf-8 -*-
"""
MusicLSP v3.2 — Deezer + SoundCloud + Spotify + MusicBrainz
Виправлена версія: пагінація альбомів, дедуплікація, фільтр ремастерів
"""

import os
import logging
import asyncio
import tempfile
import datetime
import sqlite3
import urllib.parse

try:
    import psycopg2
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False

import hashlib
import base64
import json
import random
import subprocess
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
DATABASE_URL = os.environ.get("DATABASE_URL", "")
USE_POSTGRES = False

if DATABASE_URL and POSTGRES_AVAILABLE:
    try:
        db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        test_conn = psycopg2.connect(db_url, sslmode='require')
        test_conn.close()
        USE_POSTGRES = True
        logger.info("✅ Using PostgreSQL database")
    except Exception as e:
        logger.warning(f"PostgreSQL connection failed: {e}, falling back to SQLite")
        USE_POSTGRES = False

if not USE_POSTGRES:
    DB_PATH = "musiclsp_v3.db"
    logger.info(f"Using SQLite: {DB_PATH}")

MAX_MB = 50
DEF_QUALITY = "192"
AUTHOR = "Lesiv"
BOT_NAME = "MusicLSP"
AUTH_BOT = "@MusicLSPauth_bot"
SEARCH_PER_PAGE = 10
ALBUM_PER_PAGE = 8
ARTIST_PER_PAGE = 20

# ─── Deezer ARL Cookie ────────────────────────────────────────────────────────
DEEZER_ARL = os.environ.get("DEEZER_ARL", "")
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

# ─── Spotify Credentials ──────────────────────────────────────────────────────
SPOTIFY_CLIENT_ID = os.environ.get("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.environ.get("SPOTIFY_CLIENT_SECRET", "")

# ─── Genius API Token ─────────────────────────────────────────────────────────
GENIUS_TOKEN = os.environ.get("GENIUS_TOKEN", "")

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
#  DEEZER API
# ═══════════════════════════════════════════════════════════════════════════════

def dz_search_tracks(query, limit=30):
    """Search tracks on Deezer public API."""
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
        logger.info(f"Deezer tracks: {len(tracks)} results for '{query}'")
        return tracks
    except Exception as e:
        logger.warning(f"Deezer search failed: {e}")
        return []


def dz_search_albums(query, limit=30, offset=0):
    """Search albums on Deezer public API with pagination support."""
    try:
        encoded = urllib.parse.quote(query)
        url = f"https://api.deezer.com/search/album?q={encoded}&limit={limit}&index={offset}"
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
                "image_url": item.get("cover_big") or item.get("cover", ""),
                "source": "deezer",
                "deezer_id": str(item.get("id", "")),
            })
        logger.info(f"Deezer albums: {len(albums)} results for '{query}' (offset={offset})")
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

        if not album.get("tracks"):
            logger.warning(f"Deezer album {album_id} has no tracks")
            return None

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
            "image_url": album.get("cover_big") or album.get("cover", ""),
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


# ─── Дедуплікація альбомів ────────────────────────────────────────────────────

def normalize_album_key(album):
    """Створює нормалізований ключ для дедуплікації альбомів."""
    name = (album.get("name") or "").lower().strip()
    artist = (album.get("artist") or "").lower().strip()
    year = (album.get("year") or "").strip()

    # Прибираємо службові слова
    for word in ["(remastered)", "(remaster", "(deluxe", "(deluxe edition)",
                 "(deluxe version)", "(anniversary", "(special edition)",
                 "- remastered", "- remaster"]:
        name = name.replace(word, "").strip()

    name = " ".join(name.split())
    artist = " ".join(artist.split())

    return f"{artist}::{name}::{year}"


def merge_albums(*sources):
    """Об'єднує альбоми з різних джерел з дедуплікацією."""
    all_albums = []
    seen = set()

    for source_list in sources:
        if isinstance(source_list, Exception) or not source_list:
            continue
        for album in source_list:
            if not isinstance(album, dict):
                continue
            key = normalize_album_key(album)
            if key not in seen and album.get("name"):
                seen.add(key)
                all_albums.append(album)

    return all_albums


def filter_preferred_releases(releases, hide_remasters=True):
    """Фільтрує ремастери/deluxe, залишаючи оригінали."""
    if not releases or not hide_remasters:
        return releases

    keywords = [
        "remaster", "remastered", "deluxe", "anniversary",
        "special edition", "expanded", "bonus track", "reissue"
    ]

    filtered = []
    for r in releases:
        title = (r.get("title", "") or "").lower()
        if not any(kw in title for kw in keywords):
            filtered.append(r)

    return filtered if filtered else releases


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
def db():
    if USE_POSTGRES:
        db_url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(db_url, sslmode='require')
        return conn
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn


def _row_to_dict(row, cursor):
    """Конвертує PostgreSQL row у dict."""
    if row is None:
        return None
    cols = [desc[0] for desc in cursor.description]
    return dict(zip(cols, row))


def init_db():
    """Initialize database (PostgreSQL or SQLite)."""
    if USE_POSTGRES:
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
        finally:
            conn.close()
    else:
        with closing(db()) as conn:
            conn.executescript("""
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


# ── Хелпери БД (універсальні) ────────────────────────────────────────────────
def get_user(uid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT * FROM users WHERE id = %s", (uid,))
                row = c.fetchone()
                return _row_to_dict(row, c)
        else:
            return conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()


def create_user(uid, username):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO users (id, username, joined) VALUES (%s, %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (uid, username, now)
                )
            conn.commit()
        else:
            conn.execute(
                "INSERT OR IGNORE INTO users (id, username, joined) VALUES (?, ?, ?)",
                (uid, username, now)
            )
            conn.commit()
    finally:
        conn.close()


def get_lang(uid):
    u = get_user(uid)
    if u is None:
        return "en"
    if isinstance(u, dict):
        return u.get("lang") or "en"
    return u["lang"] or "en"


def set_lang(uid, lang):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("UPDATE users SET lang = %s WHERE id = %s", (lang, uid))
            conn.commit()
        else:
            conn.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))
            conn.commit()
    finally:
        conn.close()


def get_state(uid):
    u = get_user(uid)
    if u is None:
        return ""
    if isinstance(u, dict):
        return u.get("state") or ""
    return u["state"] or ""


def set_state(uid, state):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("UPDATE users SET state = %s WHERE id = %s", (state, uid))
            conn.commit()
        else:
            conn.execute("UPDATE users SET state=? WHERE id=?", (state, uid))
            conn.commit()
    finally:
        conn.close()


def is_premium(uid):
    u = get_user(uid)
    if u is None:
        return False
    if isinstance(u, dict):
        return bool(u.get("is_premium"))
    return bool(u["is_premium"])


def set_premium(uid, premium=True):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat() if premium else None
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "UPDATE users SET is_premium = %s, premium_since = %s WHERE id = %s",
                    (premium, now, uid)
                )
            conn.commit()
        else:
            conn.execute(
                "UPDATE users SET is_premium=?, premium_since=? WHERE id=?",
                (1 if premium else 0, now, uid)
            )
            conn.commit()
    finally:
        conn.close()


def get_limits(uid):
    return PREMIUM_LIMITS if is_premium(uid) else FREE_LIMITS


def add_library(uid, title, artist, url, kind="track"):
    limits = get_limits(uid)
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) as c FROM library WHERE user_id = %s", (uid,))
                count = c.fetchone()[0]
                if count >= limits["library_max"]:
                    return False, "full"
                c.execute("SELECT id FROM library WHERE user_id = %s AND url = %s", (uid, url))
                ex = c.fetchone()
                if not ex:
                    c.execute(
                        "INSERT INTO library(user_id, title, artist, url, kind, added) "
                        "VALUES(%s, %s, %s, %s, %s, %s)",
                        (uid, title, artist, url, kind, now)
                    )
            conn.commit()
            return True, "added" if not ex else False, "exists"
        else:
            count = conn.execute(
                "SELECT COUNT(*) as c FROM library WHERE user_id=?", (uid,)
            ).fetchone()["c"]
            if count >= limits["library_max"]:
                return False, "full"
            ex = conn.execute(
                "SELECT id FROM library WHERE user_id=? AND url=?", (uid, url)
            ).fetchone()
            if not ex:
                conn.execute(
                    "INSERT INTO library(user_id, title, artist, url, kind, added) "
                    "VALUES(?,?,?,?,?,?)",
                    (uid, title, artist, url, kind, now)
                )
                conn.commit()
                return True, "added"
    finally:
        conn.close()
    return False, "exists"


def get_library(uid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT * FROM library WHERE user_id = %s ORDER BY added DESC", (uid,))
                rows = c.fetchall()
                return [_row_to_dict(r, c) for r in rows]
        else:
            return conn.execute(
                "SELECT * FROM library WHERE user_id=? ORDER BY added DESC", (uid,)
            ).fetchall()
    finally:
        conn.close()


def del_library(uid, lid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("DELETE FROM library WHERE id = %s AND user_id = %s", (lid, uid))
            conn.commit()
        else:
            conn.execute("DELETE FROM library WHERE id=? AND user_id=?", (lid, uid))
            conn.commit()
    finally:
        conn.close()


def add_history(uid, title, artist):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO history(user_id, title, artist, played) VALUES(%s, %s, %s, %s)",
                    (uid, title, artist, now)
                )
            conn.commit()
        else:
            conn.execute(
                "INSERT INTO history(user_id, title, artist, played) VALUES(?,?,?,?)",
                (uid, title, artist, now)
            )
            conn.commit()
    finally:
        conn.close()


def get_stats_user(uid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) FROM history WHERE user_id = %s", (uid,))
                dl = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM library WHERE user_id = %s", (uid,))
                lb = c.fetchone()[0]
                c.execute(
                    "SELECT COALESCE(SUM(duration_sec), 0) FROM listening_stats WHERE user_id = %s",
                    (uid,)
                )
                listen_time = c.fetchone()[0]
                c.execute(
                    "SELECT COUNT(DISTINCT artist) FROM listening_stats WHERE user_id = %s",
                    (uid,)
                )
                unique_artists = c.fetchone()[0] or 0
        else:
            dl = conn.execute(
                "SELECT COUNT(*) as c FROM history WHERE user_id=?", (uid,)
            ).fetchone()["c"]
            lb = conn.execute(
                "SELECT COUNT(*) as c FROM library WHERE user_id=?", (uid,)
            ).fetchone()["c"]
            listen_time = conn.execute(
                "SELECT SUM(duration_sec) as s FROM listening_stats WHERE user_id=?",
                (uid,)
            ).fetchone()["s"] or 0
            unique_artists = conn.execute(
                "SELECT COUNT(DISTINCT artist) as c FROM listening_stats WHERE user_id=?",
                (uid,)
            ).fetchone()["c"] or 0
    finally:
        conn.close()
    return {
        "dl": dl, "lib": lb,
        "listen_time": listen_time,
        "unique_artists": unique_artists
    }


def add_listening_stat(uid, title, artist, duration_sec=0, source="download"):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO listening_stats(user_id, track_title, artist, duration_sec, "
                    "played_at, source) VALUES(%s, %s, %s, %s, %s, %s)",
                    (uid, title, artist, duration_sec, now, source)
                )
            conn.commit()
        else:
            conn.execute(
                "INSERT INTO listening_stats(user_id, track_title, artist, duration_sec, "
                "played_at, source) VALUES(?,?,?,?,?,?)",
                (uid, title, artist, duration_sec, now, source)
            )
            conn.commit()
    finally:
        conn.close()


def get_listening_stats(uid, days=30):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "SELECT COUNT(*) FROM listening_stats WHERE user_id = %s AND played_at > %s",
                    (uid, since)
                )
                total = c.fetchone()[0]
                c.execute("""
                    SELECT artist, COUNT(*) as c FROM listening_stats
                    WHERE user_id = %s AND played_at > %s
                    GROUP BY artist ORDER BY c DESC LIMIT 5
                """, (uid, since))
                top_artists = c.fetchall()
                c.execute("""
                    SELECT track_title, artist, COUNT(*) as c FROM listening_stats
                    WHERE user_id = %s AND played_at > %s
                    GROUP BY track_title, artist ORDER BY c DESC LIMIT 5
                """, (uid, since))
                top_tracks = c.fetchall()
        else:
            total = conn.execute(
                "SELECT COUNT(*) as c FROM listening_stats WHERE user_id=? AND played_at>?",
                (uid, since)
            ).fetchone()["c"]
            top_artists = conn.execute("""
                SELECT artist, COUNT(*) as c FROM listening_stats
                WHERE user_id=? AND played_at>? GROUP BY artist ORDER BY c DESC LIMIT 5
            """, (uid, since)).fetchall()
            top_tracks = conn.execute("""
                SELECT track_title, artist, COUNT(*) as c FROM listening_stats
                WHERE user_id=? AND played_at>? GROUP BY track_title, artist ORDER BY c DESC LIMIT 5
            """, (uid, since)).fetchall()
    finally:
        conn.close()
    return {"total": total, "top_artists": top_artists, "top_tracks": top_tracks}


def get_all_users():
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT * FROM users")
                rows = c.fetchall()
                return [_row_to_dict(r, c) for r in rows]
        else:
            return conn.execute("SELECT * FROM users").fetchall()
    finally:
        conn.close()


def get_recent_users(limit=20):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT * FROM users ORDER BY joined DESC LIMIT %s", (limit,))
                rows = c.fetchall()
                return [_row_to_dict(r, c) for r in rows]
        else:
            return conn.execute(
                "SELECT * FROM users ORDER BY joined DESC LIMIT ?", (limit,)
            ).fetchall()
    finally:
        conn.close()


def get_global_stats():
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT COUNT(*) FROM users")
                total = c.fetchone()[0]
                c.execute("SELECT COUNT(*) FROM users WHERE is_premium = TRUE")
                premium = c.fetchone()[0]
        else:
            total = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
            premium = conn.execute(
                "SELECT COUNT(*) as c FROM users WHERE is_premium=1"
            ).fetchone()["c"]
    finally:
        conn.close()
    return {"total": total, "premium": premium, "free": total - premium}


# ─── ПЛЕЙЛИСТИ ────────────────────────────────────────────────────────────────
def create_playlist(uid, name, description=""):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO playlists(user_id, name, description, created, updated) "
                    "VALUES(%s, %s, %s, %s, %s) RETURNING id",
                    (uid, name, description, now, now)
                )
                pid = c.fetchone()[0]
            conn.commit()
            return pid
        else:
            cur = conn.execute(
                "INSERT INTO playlists(user_id, name, description, created, updated) "
                "VALUES(?,?,?,?,?)",
                (uid, name, description, now, now)
            )
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()


def get_playlists(uid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "SELECT * FROM playlists WHERE user_id = %s ORDER BY updated DESC",
                    (uid,)
                )
                rows = c.fetchall()
                return [_row_to_dict(r, c) for r in rows]
        else:
            return conn.execute(
                "SELECT * FROM playlists WHERE user_id=? ORDER BY updated DESC", (uid,)
            ).fetchall()
    finally:
        conn.close()


def get_playlist(pid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT * FROM playlists WHERE id = %s", (pid,))
                pl = _row_to_dict(c.fetchone(), c)
                c.execute("SELECT * FROM playlist_tracks WHERE playlist_id = %s ORDER BY id", (pid,))
                tracks = [_row_to_dict(r, c) for r in c.fetchall()]
        else:
            pl = conn.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
            tracks = conn.execute(
                "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY id", (pid,)
            ).fetchall()
    finally:
        conn.close()
    return pl, tracks


def add_track_to_playlist(pid, title, artist, url, duration=""):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO playlist_tracks(playlist_id, title, artist, url, duration, added) "
                    "VALUES(%s, %s, %s, %s, %s, %s)",
                    (pid, title, artist, url, duration, now)
                )
                c.execute("UPDATE playlists SET updated = %s WHERE id = %s", (now, pid))
            conn.commit()
        else:
            conn.execute(
                "INSERT INTO playlist_tracks(playlist_id, title, artist, url, duration, added) "
                "VALUES(?,?,?,?,?,?)",
                (pid, title, artist, url, duration, now)
            )
            conn.execute("UPDATE playlists SET updated=? WHERE id=?", (now, pid))
            conn.commit()
    finally:
        conn.close()


def delete_playlist(uid, pid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("DELETE FROM playlists WHERE id = %s AND user_id = %s", (pid, uid))
            conn.commit()
        else:
            conn.execute("DELETE FROM playlists WHERE id=? AND user_id=?", (pid, uid))
            conn.commit()
    finally:
        conn.close()


def delete_playlist_track(pid, tid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "DELETE FROM playlist_tracks WHERE id = %s AND playlist_id = %s",
                    (tid, pid)
                )
            conn.commit()
        else:
            conn.execute(
                "DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", (tid, pid)
            )
            conn.commit()
    finally:
        conn.close()


# ─── РАДІО СЕСІЇ ──────────────────────────────────────────────────────────────
def create_radio_session(uid, seed_artist, seed_track, tracks):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tracks_json = json.dumps(tracks)
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "INSERT INTO radio_sessions(user_id, seed_artist, seed_track, "
                    "tracks_json, created, updated) VALUES(%s, %s, %s, %s, %s, %s) "
                    "RETURNING id",
                    (uid, seed_artist, seed_track, tracks_json, now, now)
                )
                rid = c.fetchone()[0]
            conn.commit()
            return rid
        else:
            cur = conn.execute(
                "INSERT INTO radio_sessions(user_id, seed_artist, seed_track, "
                "tracks_json, created, updated) VALUES(?,?,?,?,?,?)",
                (uid, seed_artist, seed_track, tracks_json, now, now)
            )
            conn.commit()
            return cur.lastrowid
    finally:
        conn.close()


def get_radio_session(rid):
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute("SELECT * FROM radio_sessions WHERE id = %s", (rid,))
                r = _row_to_dict(c.fetchone(), c)
                if r:
                    return r, json.loads(r["tracks_json"])
        else:
            r = conn.execute(
                "SELECT * FROM radio_sessions WHERE id=?", (rid,)
            ).fetchone()
            if r:
                return dict(r), json.loads(r["tracks_json"])
        return None, []
    finally:
        conn.close()


def update_radio_idx(rid, idx):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    conn = db()
    try:
        if USE_POSTGRES:
            with conn.cursor() as c:
                c.execute(
                    "UPDATE radio_sessions SET current_idx = %s, updated = %s WHERE id = %s",
                    (idx, now, rid)
                )
            conn.commit()
        else:
            conn.execute(
                "UPDATE radio_sessions SET current_idx=?, updated=? WHERE id=?",
                (idx, now, rid)
            )
            conn.commit()
    finally:
        conn.close()


# ═══════════════════════════════════════════════════════════════════════════════
#  ПОШУКОВІ ФУНКЦІЇ
# ═══════════════════════════════════════════════════════════════════════════════

def sc_search(query, limit=20):
    """Search SoundCloud."""
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True,
        "noplaylist": True, "socket_timeout": 30, "retries": 3,
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
    logger.info(f"SoundCloud: {len(tracks)} results for '{query}'")
    return tracks


def search_spotify_tracks(query, limit=20):
    """Search tracks in Spotify."""
    token = get_spotify_token()
    if not token:
        return []
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


def search_all(query, limit=30):
    """Search SoundCloud + Deezer + Spotify. Returns unified results."""
    results = []
    seen_urls = set()
    seen_titles = set()

    for track in sc_search(query, limit):
        key = track["url"]
        if key and key not in seen_urls:
            seen_urls.add(key)
            seen_titles.add(track["title"].lower().strip())
            results.append(track)

    for track in dz_search_tracks(query, limit):
        key = track["url"]
        title_key = track["title"].lower().strip()
        if key and key not in seen_urls and title_key not in seen_titles:
            seen_urls.add(key)
            seen_titles.add(title_key)
            results.append(track)

    if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
        for track in search_spotify_tracks(query, limit=limit // 2):
            key = track["url"]
            title_key = track["title"].lower().strip()
            if key and key not in seen_urls and title_key not in seen_titles:
                seen_urls.add(key)
                seen_titles.add(title_key)
                results.append(track)

    logger.info(f"Total tracks for '{query}': {len(results)}")
    return results[:limit]


def search_all_albums(query, limit_per_source=30, hide_remasters=True):
    """Search albums in MusicBrainz + Deezer. Returns unified + deduped results."""
    loop = asyncio.get_event_loop()
    mb_task = loop.run_in_executor(None, mb_search_album, query, limit_per_source)
    dz_task = loop.run_in_executor(None, dz_search_albums, query, limit_per_source)

    try:
        mb_results, dz_results = loop.run_until_complete(asyncio.gather(
            asyncio.ensure_future(_to_thread(mb_search_album, query, limit_per_source)),
            asyncio.ensure_future(_to_thread(dz_search_albums, query, limit_per_source))
        )) if False else (mb_task.result(), dz_task.result())
    except Exception:
        try:
            mb_results = mb_task.result() if hasattr(mb_task, 'result') else mb_search_album(query, limit_per_source)
            dz_results = dz_task.result() if hasattr(dz_task, 'result') else dz_search_albums(query, limit_per_source)
        except Exception as e:
            logger.error(f"Album search gather failed: {e}")
            return []

    if hide_remasters:
        mb_results = filter_preferred_releases(mb_results, hide_remasters=True)
        dz_results = filter_preferred_releases(dz_results, hide_remasters=True)

    merged = merge_albums(mb_results, dz_results)
    logger.info(f"Albums for '{query}': MB={len(mb_results)}, DZ={len(dz_results)}, merged={len(merged)}")
    return merged


async def _to_thread(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, func, *args, **kwargs)


async def async_search_albums(query, limit_per_source=30, hide_remasters=True):
    """Async album search: MusicBrainz + Deezer з дедуплікацією."""
    mb_task = _to_thread(mb_search_album, query, limit_per_source)
    dz_task = _to_thread(dz_search_albums, query, limit_per_source)

    try:
        results = await asyncio.gather(mb_task, dz_task, return_exceptions=True)
    except Exception as e:
        logger.error(f"async_search_albums gather failed: {e}")
        return []

    mb_results = results[0] if not isinstance(results[0], Exception) else []
    dz_results = results[1] if not isinstance(results[1], Exception) else []

    if hide_remasters:
        mb_results = filter_preferred_releases(mb_results, hide_remasters=True)
        dz_results = filter_preferred_releases(dz_results, hide_remasters=True)

    merged = merge_albums(mb_results, dz_results)
    logger.info(f"async_search_albums '{query}': MB={len(mb_results)}, DZ={len(dz_results)}, merged={len(merged)}")
    return merged


# ─── SPOTIFY: альбоми ─────────────────────────────────────────────────────────
def extract_spotify_album_id(text):
    text = text.strip()
    if "spotify.com/album/" in text:
        parts = text.split("album/")
        if len(parts) > 1:
            return parts[1].split("?")[0].split("/")[0]
    if len(text) == 22 and text.replace("-", "").replace("_", "").isalnum():
        return text
    return None


def get_spotify_album(album_id):
    return spotify_request(f"albums/{album_id}")


def search_spotify_albums(query, limit=20):
    queries = [query, f"{query} album", f"album:{query}"]
    all_items = []
    seen_ids = set()
    for q in queries:
        encoded = urllib.parse.quote(q)
        data = spotify_request(f"search?q={encoded}&type=album&limit={limit}")
        if data and "albums" in data:
            for item in data["albums"].get("items", []):
                item_id = item.get("id")
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    all_items.append(item)
            if len(all_items) >= 10:
                break
    return all_items


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
            "artists": ", ".join(a.get("name", "") for a in track.get("artists", [])),
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
                    "artists": ", ".join(a.get("name", "") for a in track.get("artists", [])),
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
        "artist": ", ".join(a.get("name", "") for a in album.get("artists", [])),
        "release_date": album.get("release_date", "—"),
        "year": album.get("release_date", "—")[:4] if album.get("release_date") else "—",
        "total_tracks": album.get("total_tracks", len(tracks)),
        "tracks": tracks,
        "total_duration_ms": total_duration_ms,
        "total_duration": fmt_dur_ms(total_duration_ms),
        "label": album.get("label", "—"),
        "image_url": image_url,
        "album_type": album.get("album_type", "album"),
        "external_url": album.get("external_urls", {}).get("spotify", f"https://open.spotify.com/album/{album_id}"),
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
            "artist": ", ".join(a.get("name", "") for a in album.get("artists", [])),
            "release_date": album.get("release_date", "—"),
            "year": album.get("release_date", "—")[:4] if album.get("release_date") else "—",
            "total_tracks": album.get("total_tracks", 0),
            "image_url": image_url,
        })
    return results


# ─── MusicBrainz API ──────────────────────────────────────────────────────────
def mb_search_album(query, limit=30):
    encoded = urllib.parse.quote(query)
    url = f"https://musicbrainz.org/ws/2/release/?query=release:{encoded}&fmt=json&limit={limit}"
    headers = {"User-Agent": f"MusicLSP/3.2 ({AUTHOR})"}
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
    headers = {"User-Agent": f"MusicLSP/3.2 ({AUTHOR})"}
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
                    a.get("name", "") for a in recording.get("artist-credit", [])
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


# ─── Artist songs ─────────────────────────────────────────────────────────────
def artist_songs(artist, limit=50):
    results = []
    seen = set()
    for t in sc_search(artist, limit):
        if t["url"] not in seen:
            seen.add(t["url"])
            results.append(t)
    for t in dz_get_artist_top(artist, limit):
        if t["url"] not in seen:
            seen.add(t["url"])
            results.append(t)
    return results[:limit]


# ─── Find track for download ──────────────────────────────────────────────────
def find_track_for_download(track_name, artist_name):
    queries = [
        f"{artist_name} {track_name}",
        f"{track_name} {artist_name}",
        track_name,
        artist_name,
    ]
    for query in queries:
        try:
            result = sc_search(query, limit=5)
            if result:
                for r in result:
                    title_lower = r["title"].lower()
                    if track_name.lower() in title_lower or artist_name.lower() in title_lower:
                        return {"title": r["title"], "url": r["url"], "source": "soundcloud"}
                return {"title": result[0]["title"], "url": result[0]["url"], "source": "soundcloud"}
        except Exception as e:
            logger.warning(f"SC search failed for '{query}': {e}")

        try:
            dz = dz_search_tracks(query, limit=5)
            if dz:
                for d in dz:
                    title_lower = d["title"].lower()
                    if track_name.lower() in title_lower or artist_name.lower() in title_lower:
                        return {"title": d["title"], "url": d["url"], "source": "deezer"}
                return {"title": dz[0]["title"], "url": dz[0]["url"], "source": "deezer"}
        except Exception as e:
            logger.warning(f"Deezer search failed for '{query}': {e}")

        if SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET:
            try:
                spotify = search_spotify_tracks(query, limit=5)
                if spotify:
                    for s in spotify:
                        title_lower = s["title"].lower()
                        if track_name.lower() in title_lower or artist_name.lower() in title_lower:
                            return {"title": s["title"], "url": s["url"], "source": "spotify"}
                    return {"title": spotify[0]["title"], "url": spotify[0]["url"], "source": "spotify"}
            except Exception as e:
                logger.warning(f"Spotify search failed for '{query}': {e}")

    logger.error(f"Could not find track: {artist_name} - {track_name}")
    return None


# ─── Асинхронні обгортки ──────────────────────────────────────────────────────
async def async_search(query, limit=30):
    return await _to_thread(search_all, query, limit)


async def async_artist(artist, limit=50):
    return await _to_thread(artist_songs, artist, limit)


async def async_download(url, out_dir, quality="192"):
    return await _to_thread(download_mp3, url, out_dir, quality)


async def async_spotify_album_info(album_id):
    return await _to_thread(get_spotify_album_info, album_id)


async def async_search_spotify(query, limit=10):
    return await _to_thread(search_spotify_and_format, query, limit)


async def async_mb_search(query, limit=30):
    return await _to_thread(mb_search_album, query, limit)


async def async_mb_full_info(mbid):
    return await _to_thread(mb_get_full_album_info, mbid)


async def async_find_track(track_name, artist_name):
    return await _to_thread(find_track_for_download, track_name, artist_name)


# ─── Завантаження MP3 ─────────────────────────────────────────────────────────
def download_mp3(url, out_dir, quality="192"):
    base_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": quality,
        }],
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "socket_timeout": 30, "retries": 3,
        "fragment_retries": 3, "file_access_retries": 3, "extractor_retries": 3,
    }
    if "deezer.com" in url:
        if DEEZER_ARL:
            base_opts["cookies"] = {"arl": DEEZER_ARL}
            logger.info("Using Deezer ARL cookie")
        elif os.path.exists(DEEZER_COOKIES_FILE):
            base_opts["cookiefile"] = DEEZER_COOKIES_FILE
            logger.info("Using Deezer cookies file")
    try:
        with yt_dlp.YoutubeDL(base_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info:
            mp3_files = list(Path(out_dir).glob("*.mp3"))
            if mp3_files:
                logger.info(f"Download success: {mp3_files[0].name}")
                return str(mp3_files[0])
            for ext in ["*.m4a", "*.webm", "*.opus", "*.ogg", "*.mp4"]:
                files = list(Path(out_dir).glob(ext))
                if files:
                    input_file = str(files[0])
                    output_file = os.path.join(out_dir, f"{files[0].stem}.mp3")
                    try:
                        subprocess.run([
                            "ffmpeg", "-i", input_file, "-vn", "-ar", "44100",
                            "-ac", "2", "-b:a", f"{quality}k", "-y", output_file,
                        ], check=True, capture_output=True, timeout=60)
                        if os.path.exists(output_file):
                            os.remove(input_file)
                            return output_file
                    except Exception as conv_e:
                        logger.warning(f"FFmpeg conversion failed: {conv_e}")
                        return input_file
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return None
# 📦 ЧАСТИНА 2 з 2

Це продовження коду. **Додай цей код в кінець файлу** `bot.py` після частини 1.

```python


# ═══════════════════════════════════════════════════════════════════════════════
#  ДОПОМІЖНІ ФУНКЦІЇ ЗАВАНТАЖЕННЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def async_download_with_fallback(url, out_dir, quality="192"):
    result = await async_download(url, out_dir, quality)
    if result:
        return result
    logger.info("Trying alternative download method...")
    return await _to_thread(_alt_download, url, out_dir, quality)


def _alt_download(url, out_dir, quality="192"):
    opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": False,
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
                path = await async_download_with_fallback(track["url"], track_dir, quality)
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
#  КЛАВІАТУРИ
# ═══════════════════════════════════════════════════════════════════════════════

def main_kb(uid):
    l = get_lang(uid)
    btn = lambda text, data: InlineKeyboardButton(text, callback_data=data)
    labels = {
        "uk": ["🔍 Пошук", "💿 Альбоми", "📚 Бібліотека", "👤 Профіль", "💎 Підписка", "🎁 Реферал", "⚙️ Налаштування"],
        "ru": ["🔍 Поиск", "💿 Альбомы", "📚 Библиотека", "👤 Профиль", "💎 Подписка", "🎁 Реферал", "⚙️ Настройки"],
        "en": ["🔍 Search", "💿 Albums", "📚 Library", "👤 Profile", "💎 Premium", "🎁 Invite", "⚙️ Settings"],
        "fr": ["🔍 Recherche", "💿 Albums", "📚 Bibliothèque", "👤 Profil", "💎 Premium", "🎁 Inviter", "⚙️ Paramètres"],
    }
    lb = labels.get(l, labels["en"])
    return (
        InlineKeyboardMarkup([
            [btn(lb[0], "m:search"), btn(lb[1], "m:albums")],
            [btn(lb[2], "m:library"), btn(lb[3], "m:profile")],
            [btn(lb[4], "m:sub"), btn(lb[5], "m:ref")],
            [btn(lb[6], "m:settings")],
        ]),
        "◀️ Back"
    )


def back_btn(uid):
    l = get_lang(uid)
    labels = {"uk": "◀️ Назад", "ru": "◀️ Назад", "en": "◀️ Back", "fr": "◀️ Retour"}
    return InlineKeyboardButton(labels.get(l, "◀️ Back"), callback_data="m:home")


# ═══════════════════════════════════════════════════════════════════════════════
#  КОМАНДИ
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    create_user(uid, username)
    u = get_user(uid)
    if u and u.get("lang") and u["lang"] != "uk":
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


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid != ADMIN_ID:
        await update.message.reply_text("⛔ Access denied")
        return
    await _show_admin_panel(update.message, uid)


async def _show_admin_panel(msg, uid):
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


# ═══════════════════════════════════════════════════════════════════════════════
#  CALLBACK HANDLER
# ═══════════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    l = get_lang(uid)

    # Language selection
    if data.startswith("lang:"):
        set_lang(uid, data[5:])
        try:
            await q.message.delete()
        except Exception:
            pass
        await show_welcome(q.message, uid)
        return

    # Home
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

    # Search menu
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

    # Albums menu
    if data == "m:albums":
        set_state(uid, "album_search")
        prompts = {
            "uk": "💿 Введи назву альбому:\n\n<i>Приклади:</i>\n* <code>Yanix SS 20</code>\n* <code>Imagine Dragons Mercury</code>\n* <code>Linkin Park Hybrid Theory</code>",
            "ru": "💿 Введи название альбома:\n\n<i>Примеры:</i>\n* <code>Yanix SS 20</code>",
            "en": "💿 Enter album name:\n\n<i>Examples:</i>\n* <code>Imagine Dragons Mercury</code>",
            "fr": "💿 Entre le nom de l'album:",
        }
        await q.message.edit_text(
            prompts.get(l, prompts["en"]),
            reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
            parse_mode="HTML",
        )
        return

    # Simple menus
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

    # Genres (Premium)
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
                is_p = u.get("is_premium") if isinstance(u, dict) else u["is_premium"]
                status = "💎" if is_p else "💿"
                name = u.get("username", "—") if isinstance(u, dict) else u["username"]
                joined = str(u.get("joined", "—"))[:10]
                uid_v = u.get("id") if isinstance(u, dict) else u["id"]
                text += f"{i}. {status} <code>{uid_v}</code> @{name} ({joined})\n"
            await q.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm:panel")]]), parse_mode="HTML"
            )
            return
        if data == "adm:stats":
            stats = get_global_stats()
            text = f"📊 <b>Статистика</b>\n\n👥 Всього: {stats['total']}\n💎 Premium: {stats['premium']}\n💿 Free: {stats['free']}"
            await q.message.edit_text(
                text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data="adm:panel")]]), parse_mode="HTML"
            )
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

    if data == "m:playlists":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_playlists_menu(q.message, uid, ctx)
        return

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

    if data == "m:stats":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_stats(q.message, uid)
        return

    # Lyrics (Premium) — ВИПРАВЛЕНО: додано if data == "m:lyrics"
    if data == "m:lyrics":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
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

    # Spotify album
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
        await do_download(q.message, result["url"], track["name"], track["artists"], uid, ctx)
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
        added, status_msg = add_library(uid, title, artist, url)
        if status_msg == "full":
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

    # Album pagination — НОВИЙ ОБРОБНИК
    if data.startswith("albumpage|"):
        parts = data.split("|", 2)
        query = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await do_album_search_paged(q.message, query, uid, ctx, page, edit=True)
        return

    if data.startswith("quality|"):
        q_val = data[8:]
        if not is_premium(uid) and q_val != "192":
            await q.answer("⛔ Тільки 192kbps у Free версії!", show_alert=True)
            return
        ctx.bot_data.setdefault("quality", {})[uid] = q_val
        await q.answer(f"✅ Якість: {q_val} kbps", show_alert=True)
        return

    # MusicBrainz album
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
        await do_download(q.message, result["url"], track["name"], track["artists"], uid, ctx)
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

    # Deezer album
    if data.startswith("dz_album|"):
        album_id = data.split("|", 1)[1]
        await show_dz_album(q.message, album_id, uid, ctx)
        return

    if data.startswith("dz_track|"):
        parts = data.split("|", 2)
        album_ck = parts[1]
        track_idx = int(parts[2])
        album_data = ctx.bot_data.get("dz_album_cache", {}).get(album_ck)
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
        await do_download(q.message, result["url"], track["name"], track["artists"], uid, ctx)
        return

    if data == "dz_albumzip":
        if not is_premium(uid):
            await q.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        album_ck = ctx.bot_data.get("last_dz_album_ck", "")
        album_data = ctx.bot_data.get("dz_album_cache", {}).get(album_ck)
        if not album_data:
            await q.message.reply_text("❌ Дані альбому застаріли.")
            return
        await do_download_dz_album_zip(q.message, album_data, uid, ctx)
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

    # Radio
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
        await do_download(q.message, track["url"], track["title"], track.get("artist", ""), uid, ctx)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("⏭ Наступна", callback_data=f"radio_next|{rid}"), back_btn(uid)]
        ])
        await q.message.reply_text(
            f"📻 Радіо: {idx+1}/{len(tracks)}", reply_markup=kb, parse_mode="HTML"
        )
        return

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


# ═══════════════════════════════════════════════════════════════════════════════
#  ОБРОБКА ПОВІДОМЛЕНЬ
# ═══════════════════════════════════════════════════════════════════════════════

async def handle_admin_input(update, ctx, state, text):
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
                uid_v = u.get("id") if isinstance(u, dict) else u["id"]
                await ctx.bot.send_message(uid_v, text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.5)
            except Exception:
                pass
        await update.message.reply_text(f"✅ Broadcast sent to {sent}/{len(users)} users")
    elif state == "adm:find":
        search = text.strip()
        conn = db()
        try:
            if search.startswith("@"):
                if USE_POSTGRES:
                    with conn.cursor() as c:
                        c.execute("SELECT * FROM users WHERE username = %s", (search[1:],))
                        row = c.fetchone()
                        user = _row_to_dict(row, c)
                else:
                    row = conn.execute("SELECT * FROM users WHERE username=?", (search[1:],)).fetchone()
                    user = dict(row) if row else None
            else:
                try:
                    target_id = int(search)
                    if USE_POSTGRES:
                        with conn.cursor() as c:
                            c.execute("SELECT * FROM users WHERE id = %s", (target_id,))
                            row = c.fetchone()
                            user = _row_to_dict(row, c)
                    else:
                        row = conn.execute("SELECT * FROM users WHERE id=?", (target_id,)).fetchone()
                        user = dict(row) if row else None
                except ValueError:
                    user = None
            if user:
                is_p = user.get("is_premium")
                status = "💎 Premium" if is_p else "💿 Free"
                await update.message.reply_text(
                    f"👤 User: {user['id']}\n@{user.get('username', '—')}\nStatus: {status}",
                    parse_mode="HTML"
                )
            else:
                await update.message.reply_text("❌ User not found")
        finally:
            conn.close()


async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip() if update.message.text else ""
    l = get_lang(uid)
    state = get_state(uid)

    if not get_user(uid):
        create_user(uid, update.effective_user.username or "")

    if state == "artist_input":
        set_state(uid, "")
        limits = get_limits(uid)
        max_songs = limits["artist_songs"]
        if max_songs == 0:
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await show_artist(update.message, text, uid, ctx, max_songs)
        return

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

    # Album search — ВИПРАВЛЕНО: тепер шукає в MB + Deezer з пагінацією
    if state == "album_search":
        set_state(uid, "")
        await do_album_search_paged(update, text, uid, ctx, page=0, edit=False)
        return

    if state == "zip_album_search":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await do_zip_album_search(update, text, uid, ctx)
        return

    if state == "radio_input":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await start_radio(update.message, text, uid, ctx)
        return

    if state == "ai_recommend_input":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await ai_recommend(update.message, text, uid, ctx)
        return

    if state == "lyrics_input":
        set_state(uid, "")
        if not is_premium(uid):
            await update.message.reply_text(tx("premium_only", l), parse_mode="HTML")
            return
        await get_lyrics(update.message, text, uid)
        return

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

    if uid == ADMIN_ID and state.startswith("adm:"):
        await handle_admin_input(update, ctx, state, text)
        return

    # Default search
    set_state(uid, "")
    await do_search_paged(update, text, uid, ctx, page=0, edit=False)


# ═══════════════════════════════════════════════════════════════════════════════
#  ПОШУК З ПАГІНАЦІЄЮ
# ═══════════════════════════════════════════════════════════════════════════════

async def do_search_paged(update_or_msg, query, uid, ctx, page=0, edit=False):
    l = get_lang(uid)
    if edit:
        msg = update_or_msg
        try:
            await msg.edit_text(f"🔍 <b>{query}</b> (стор. {page+1})…", parse_mode="HTML")
        except Exception:
            pass
    else:
        msg = await update_or_msg.message.reply_text(
            f"🔍 <b>{query}</b>…", parse_mode="HTML"
        )

    all_tracks = await async_search(query, limit=30)
    if not all_tracks:
        try:
            await msg.edit_text("😔 Нічого не знайдено.")
        except Exception:
            await msg.reply_text("😔 Нічого не знайдено.")
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
        nav.append(InlineKeyboardButton("◀️ Попередня", callback_data=f"searchp|{query}|{page-1}"))
    if has_more:
        nav.append(InlineKeyboardButton("➡️ Наступна", callback_data=f"searchp|{query}|{page+1}"))
    if nav:
        kb.append(nav)

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
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
#  АЛЬБОМ-ПОШУК З ПАГІНАЦІЄЮ (MusicBrainz + Deezer)
# ═══════════════════════════════════════════════════════════════════════════════

async def do_album_search_paged(update_or_msg, query, uid, ctx, page=0, edit=False):
    """Пошук альбомів у MusicBrainz + Deezer з пагінацією."""
    l = get_lang(uid)

    if edit:
        msg = update_or_msg
        try:
            await msg.edit_text(
                f"💿 <b>{query}</b> (стор. {page+1})…", parse_mode="HTML"
            )
        except Exception:
            pass
    else:
        if hasattr(update_or_msg, 'message'):
            msg = await update_or_msg.message.reply_text(
                f"💿 Шукаю альбоми: <b>{query}</b>…", parse_mode="HTML"
            )
        else:
            msg = await update_or_msg.reply_text(
                f"💿 Шукаю альбоми: <b>{query}</b>…", parse_mode="HTML"
            )

    # Шукаємо в обох джерелах паралельно
    albums = await async_search_albums(query, limit_per_source=30, hide_remasters=True)

    if not albums:
        try:
            await msg.edit_text(
                "😔 Альбоми не знайдено.\n\n"
                "💡 Спробуй:\n"
                "* Точнішу назву: <code>Yanix SS 20</code>\n"
                "* Формат: <code>Артист НазваАльбому</code>\n"
                "* Англійську: <code>Linkin Park Hybrid Theory</code>",
                reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    # Зберігаємо для пагінації
    ck = f"album_search_{uid}_{query}"
    ctx.bot_data.setdefault("album_cache", {})[ck] = albums

    start = page * ALBUM_PER_PAGE
    end = start + ALBUM_PER_PAGE
    page_albums = albums[start:end]
    has_more = len(albums) > end

    if not page_albums:
        try:
            await msg.edit_text("😔 Більше альбомів немає.")
        except Exception:
            pass
        return

    kb = []
    for album in page_albums:
        name = album.get("name", "Unknown")[:35]
        artist = album.get("artist", "Unknown")[:25]
        year = album.get("year", "—")
        tracks_count = album.get("total_tracks", 0)

        # Визначаємо джерело
        if album.get("mbid"):
            cb_data = f"mb_album|{album['mbid']}"
            icon = "🔴"
        elif album.get("deezer_id"):
            cb_data = f"dz_album|{album['deezer_id']}"
            icon = "🟣"
        else:
            continue

        label = f"{icon} {name} — {artist} ({year}, {tracks_count} 🎵)"
        kb.append([InlineKeyboardButton(label, callback_data=cb_data)])

    # Пагінація
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            "◀️ Попередня", callback_data=f"albumpage|{query}|{page-1}"
        ))
    if has_more:
        nav.append(InlineKeyboardButton(
            "➡️ Наступна", callback_data=f"albumpage|{query}|{page+1}"
        ))
    if nav:
        kb.append(nav)

    kb.append([back_btn(uid)])

    text = (
        f"💿 <b>{query}</b>\n"
        f"📊 Знайдено: <b>{len(albums)}</b> альбомів\n"
        f"📄 Сторінка: {page+1}/{(len(albums) + ALBUM_PER_PAGE - 1) // ALBUM_PER_PAGE}\n\n"
        f"🔴 — MusicBrainz\n"
        f"🟣 — Deezer\n\n"
        f"Обери альбом 👇"
    )
    try:
        await msg.edit_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
        )
    except Exception:
        await msg.reply_text(
            text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  DEEZER: Показати альбом
# ═══════════════════════════════════════════════════════════════════════════════

async def show_dz_album(msg, album_id, uid, ctx):
    status = await msg.reply_text("💿 Завантажую інформацію…", parse_mode="HTML")
    album = await _to_thread(dz_get_album_tracks, album_id)
    if not album:
        try:
            await status.edit_text(
                "❌ Не вдалося отримати дані.",
                reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
                parse_mode="HTML"
            )
        except Exception:
            await msg.reply_text("❌ Не вдалося отримати дані.")
        return

    ck = hashlib.md5(f"dz_{uid}_{album_id}".encode()).hexdigest()[:8]
    ctx.bot_data.setdefault("dz_album_cache", {})[ck] = album
    ctx.bot_data["last_dz_album_ck"] = ck

    text = (
        f"📀 <b>{album['name']}</b>\n\n"
        f"🎤 <b>Виконавець:</b> {album['artist']}\n"
        f"📅 <b>Рік:</b> {album['year']}\n"
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
                callback_data=f"dz_track|{ck}|{i}"
            )
        ])

    l = get_lang(uid)
    zip_label = {"uk":"📦 Завантажити ZIP","ru":"📦 Скачать ZIP","en":"📦 Download ZIP"}.get(l, "📦 Download ZIP")
    kb.append([InlineKeyboardButton(zip_label, callback_data="dz_albumzip")])
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
            logger.error(f"DZ Photo send failed: {e}")

    await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def do_download_dz_album_zip(msg, album_data, uid, ctx):
    l = get_lang(uid)
    status = await msg.reply_text(
        f"⬇️ Завантажую альбом: <b>{album_data['name']}</b>…",
        parse_mode="HTML"
    )
    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    tracks_with_url = []
    total = len(album_data["tracks"])
    for i, track in enumerate(album_data["tracks"]):
        try:
            await status.edit_text(
                f"🔍 {i+1}/{total}: <b>{track['name']}</b>…",
                parse_mode="HTML"
            )
        except Exception:
            pass
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

    await status.edit_text(f"⬇️ Завантажую {len(tracks_with_url)}/{total} треків…")
    tmp_dir = tempfile.mkdtemp()
    zip_path = await create_album_zip(tracks_with_url, quality, tmp_dir)
    if not zip_path:
        await status.edit_text("❌ Помилка створення архіву.")
        return

    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    if size_mb > 2000:
        await status.edit_text(f"❌ Архів {size_mb:.1f} МБ — завеликий.")
        return

    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]
    await msg.reply_document(
        document=open(zip_path, 'rb'),
        filename=f"{safe_name}.zip",
        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків",
        parse_mode="HTML"
    )
    await status.delete()
    try:
        os.unlink(zip_path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  SPOTIFY: Показати альбом
# ═══════════════════════════════════════════════════════════════════════════════

async def show_spotify_album(msg, album_id, uid, ctx):
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


# ═══════════════════════════════════════════════════════════════════════════════
#  MusicBrainz: Показати альбом
# ═══════════════════════════════════════════════════════════════════════════════

async def show_mb_album(msg, mbid, uid, ctx):
    status = await msg.reply_text("💿 Завантажую інформацію…", parse_mode="HTML")
    album = await async_mb_full_info(mbid)
    if not album:
        try:
            await status.edit_text(
                "❌ Не вдалося отримати дані.",
                reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]),
                parse_mode="HTML"
            )
        except Exception:
            pass
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
    await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
#  ЗАВАНТАЖЕННЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def do_download(msg, url, title, artist, uid, ctx):
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
    status = await msg.reply_text("⬇️ Завантажую альбом…", parse_mode="HTML")
    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    tracks_with_url = []
    for i, track in enumerate(album_data["tracks"][:50]):
        try:
            await status.edit_text(
                f"🔍 {i+1}/{len(album_data['tracks'])}: {track['name']}…",
                parse_mode="HTML"
            )
        except Exception:
            pass
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
    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]
    await msg.reply_document(
        document=open(zip_path, 'rb'),
        filename=f"{safe_name}.zip",
        caption=f"💿 {album_data['name']}\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків",
        parse_mode="HTML"
    )
    await status.delete()
    try:
        os.unlink(zip_path)
    except Exception:
        pass


async def do_download_mb_album_zip(msg, album_data, uid, ctx):
    status = await msg.reply_text(
        f"⬇️ Завантажую альбом: <b>{album_data['name']}</b>…",
        parse_mode="HTML"
    )
    quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    tracks_with_url = []
    total = len(album_data["tracks"])
    for i, track in enumerate(album_data["tracks"]):
        try:
            await status.edit_text(
                f"🔍 {i+1}/{total}: <b>{track['name']}</b>…",
                parse_mode="HTML"
            )
        except Exception:
            pass
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
    await status.edit_text(f"⬇️ Завантажую {len(tracks_with_url)}/{total} треків…")
    tmp_dir = tempfile.mkdtemp()
    zip_path = await create_album_zip(tracks_with_url, quality, tmp_dir)
    if not zip_path:
        await status.edit_text("❌ Помилка створення архіву.")
        return
    size_mb = os.path.getsize(zip_path) / 1024 / 1024
    if size_mb > 2000:
        await status.edit_text(f"❌ Архів {size_mb:.1f} МБ — завеликий.")
        return
    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]
    await msg.reply_document(
        document=open(zip_path, 'rb'),
        filename=f"{safe_name}.zip",
        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків",
        parse_mode="HTML"
    )
    await status.delete()
    try:
        os.unlink(zip_path)
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
#  ZIP АЛЬБОМ ПОШУК
# ═══════════════════════════════════════════════════════════════════════════════

async def do_zip_album_search(update, query, uid, ctx):
    msg = await update.message.reply_text(
        f"📦 Шукаю альбом для ZIP: <b>{query}</b>…", parse_mode="HTML"
    )
    albums = await async_search_albums(query, limit_per_source=10, hide_remasters=True)
    if not albums:
        await msg.edit_text("😔 Альбоми не знайдено.")
        return

    kb = []
    for album in albums[:8]:
        name = album.get("name", "Unknown")[:30]
        artist = album.get("artist", "Unknown")[:20]
        if album.get("mbid"):
            cb = f"mb_album|{album['mbid']}"
            icon = "🔴"
        elif album.get("deezer_id"):
            cb = f"dz_album|{album['deezer_id']}"
            icon = "🟣"
        else:
            continue
        kb.append([InlineKeyboardButton(
            f"{icon} {name} — {artist}", callback_data=cb
        )])
    kb.append([back_btn(uid)])
    await msg.edit_text(
        "📦 <b>Обери альбом для ZIP:</b>\n\n"
        "🔴 — MusicBrainz\n"
        "🟣 — Deezer",
        reply_markup=InlineKeyboardMarkup(kb),
        parse_mode="HTML"
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  АРТИСТ
# ═══════════════════════════════════════════════════════════════════════════════

async def show_artist(msg, artist, uid, ctx, max_songs=10):
    l = get_lang(uid)
    texts = {
        "uk": {"searching": "🔍 Шукаю", "empty": "😔 Нічого не знайдено", "songs": "пісень"},
        "ru": {"searching": "🔍 Ищу", "empty": "😔 Ничего не найдено", "songs": "песен"},
        "en": {"searching": "🔍 Searching", "empty": "😔 Nothing found", "songs": "songs"},
        "fr": {"searching": "🔍 Recherche", "empty": "😔 Rien trouvé", "songs": "chansons"},
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
        try:
            await status.edit_text(
                f"⬇️ {i+1}/{size}: <b>{t['title'][:40]}</b>…", parse_mode="HTML"
            )
        except Exception:
            pass
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
                    await asyncio.sleep(3)
            except Exception as e:
                logger.error(f"Batch: {e}")
    await status.edit_text(f"✅ Завантажено {ok} з {len(tracks)} пісень!")


# ═══════════════════════════════════════════════════════════════════════════════
#  ЖАНРИ
# ═══════════════════════════════════════════════════════════════════════════════

async def show_genres(msg, uid, ctx):
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
    l = get_lang(uid)
    genres = MUSIC_GENRES.get(l, MUSIC_GENRES["en"])
    genre_name = genres.get(genre_key, genre_key)
    status = await msg.reply_text(f"🔍 Шукаю <b>{genre_name}</b>…", parse_mode="HTML")
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
    tracks = await async_search(query, limit=20)
    if not tracks:
        await status.edit_text("😔 Нічого не знайдено.")
        return
    await status.delete()
    ck = f"genre_{uid}_{genre_key}"
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


# ═══════════════════════════════════════════════════════════════════════════════
#  БІБЛІОТЕКА / ПРОФІЛЬ / ПІДПИСКА / РЕФЕРАЛ / НАЛАШТУВАННЯ
# ═══════════════════════════════════════════════════════════════════════════════

async def show_library(msg, uid, ctx):
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
        icon = "💿" if s.get("kind") == "album" else "🎵"
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


async def show_profile(msg, uid):
    u = get_user(uid)
    stats = get_stats_user(uid)
    status_text = "💎 Premium" if is_premium(uid) else "💿 Free"

    if isinstance(u, dict):
        joined = str(u.get("joined") or "—")[:10]
        premium_since = str(u.get("premium_since") or "—")[:10]
    elif u:
        joined = str(u["joined"] or "—")[:10]
        premium_since = str(u["premium_since"] or "—")[:10]
    else:
        joined = "—"
        premium_since = "—"

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


async def show_sub(msg, uid, ctx=None):
    l = get_lang(uid)
    premium = is_premium(uid)
    status_icon = "✅ Premium" if premium else "💿 Free"
    quality = DEF_QUALITY
    if ctx:
        quality = ctx.bot_data.get("quality", {}).get(uid, DEF_QUALITY)

    text = f"💎 <b>Підписка</b>\n\nСтатус: <b>{status_icon}</b>\n\n"
    if not premium:
        text += (
            f"💿 <b>Free:</b>\n* Пошук та завантаження MP3 (192kbps)\n* Бібліотека: до 20 записів\n* Всі пісні артиста: до 10\n\n"
            f"💎 <b>Premium:</b>\n* Якість: 192 / 320 kbps\n* Бібліотека: необмежана\n* ZIP альбоми\n* Плейлисти\n* Радіо\n* Batch download: 20/50/100\n\n"
            f"💰 Оформити Premium: {AUTH_BOT}"
        )
    else:
        text += (
            f"⭐ <b>Premium активовано!</b>\n\n"
            f"🎵 Якість: {quality}kbps\n"
            f"📚 Бібліотека: необмежана\n"
            f"📦 ZIP: доступно\n📻 Радіо: доступно\n"
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
        kb.append([InlineKeyboardButton("🎤 Тексти", callback_data="m:lyrics")])
        kb.append([InlineKeyboardButton("⚙️ Налаштування", callback_data="m:settings")])
    kb.append([back_btn(uid)])

    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def show_ref(msg, uid, ctx):
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


async def show_settings(msg, uid, ctx):
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


# ═══════════════════════════════════════════════════════════════════════════════
#  ПЛЕЙЛИСТИ
# ═══════════════════════════════════════════════════════════════════════════════

async def show_playlists_menu(msg, uid, ctx):
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
        pl_name = pl.get("name", "—") if isinstance(pl, dict) else pl["name"]
        pl_id = pl.get("id") if isinstance(pl, dict) else pl["id"]
        kb.append([
            InlineKeyboardButton(f"📁 {pl_name[:30]}", callback_data=f"pl_view|{pl_id}"),
            InlineKeyboardButton("🗑", callback_data=f"pl_del|{pl_id}")
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
    pl_name = pl.get("name", "—") if isinstance(pl, dict) else pl["name"]
    pl_desc = pl.get("description", "") if isinstance(pl, dict) else pl["description"]
    text = f"📁 <b>{pl_name}</b>\n<i>{pl_desc}</i>\n\n"
    for i, t in enumerate(tracks):
        t_title = t.get("title", "—") if isinstance(t, dict) else t["title"]
        t_artist = t.get("artist", "—") if isinstance(t, dict) else t["artist"]
        t_dur = t.get("duration", "—") if isinstance(t, dict) else t["duration"]
        t_url = t.get("url", "") if isinstance(t, dict) else t["url"]
        t_id = t.get("id") if isinstance(t, dict) else t["id"]
        text += f"{i+1}. {t_title} — {t_artist} ({t_dur})\n"
        url_id = cache_url(ctx.bot_data, t_url, t_title, t_artist)
        kb.append([
            InlineKeyboardButton(
                f"▶️ {i+1}. {t_title[:35]}",
                callback_data=f"dlurl|{url_id}|{t_title[:30]}|{t_artist[:20]}"
            ),
            InlineKeyboardButton("🗑", callback_data=f"pl_trackdel|{pid}|{t_id}")
        ])

    kb.append([InlineKeyboardButton("➕ Додати трек", callback_data=f"pl_addtrack|{pid}")])
    kb.append([back_btn(uid)])
    try:
        await msg.edit_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception:
        await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ═══════════════════════════════════════════════════════════════════════════════
#  РАДІО
# ═══════════════════════════════════════════════════════════════════════════════

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
        reply_markup=kb, parse_mode="HTML"
    )
    await do_download(msg, first["url"], first["title"], first.get("channel", ""), uid, ctx)


# ═══════════════════════════════════════════════════════════════════════════════
#  СТАТИСТИКА
# ═══════════════════════════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════════════════════════
#  AI RECOMMEND
# ═══════════════════════════════════════════════════════════════════════════════

async def ai_recommend(msg, query, uid, ctx):
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
    ck = f"ai_{uid}_{query}"
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
#  LYRICS (Premium)
# ═══════════════════════════════════════════════════════════════════════════════

async def get_lyrics(msg, query, uid):
    """Пошук тексту пісні через Genius."""
    if not GENIUS_TOKEN:
        await msg.reply_text("❌ Genius API не налаштований.")
        return

    headers = {"Authorization": f"Bearer {GENIUS_TOKEN}"}
    try:
        resp = requests.get(
            f"https://api.genius.com/search?q={urllib.parse.quote(query)}",
            headers=headers, timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        hits = data.get("response", {}).get("hits", [])
        if not hits:
            await msg.reply_text(f"😔 Текст не знайдено для: {query}")
            return
        result = hits[0].get("result", {})
        title = result.get("title", "Unknown")
        artist = result.get("primary_artist", {}).get("name", "Unknown")
        url = result.get("url", "")
        await msg.reply_text(
            f"🎤 <b>{title}</b> — {artist}\n\n"
            f"🔗 <a href=\"{url}\">Відкрити повний текст на Genius</a>",
            parse_mode="HTML", disable_web_page_preview=True
        )
    except Exception as e:
        logger.error(f"Genius error: {e}")
        await msg.reply_text("❌ Помилка отримання тексту.")


# ═══════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN (MAIN_BOT_TOKEN) not set!")
        return

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("🚀 MusicLSP v3.2 запускається...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
