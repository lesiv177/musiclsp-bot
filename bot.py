# ============================================================
#  MusicLSP — Частина 1: Імпорти, конфіг, БД, пошук
#  Оновлено: повернено старий пошук альбомів через YouTube
# ============================================================

import os, logging, asyncio, tempfile, datetime, secrets, string, sqlite3, hashlib
import static_ffmpeg

static_ffmpeg.add_paths()
from pathlib import Path
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
import yt_dlp

# ─── Логування ────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# ─── Конфіг ───────────────────────────────────────────────────────────────────
BOT_TOKEN    = os.environ.get("MAIN_BOT_TOKEN", "")
ADMIN_ID     = 1293055247
DB_PATH      = "musiclsp.db"
TRIAL_DAYS   = 3
MAX_MB       = 50
DEF_QUALITY  = "192"
AUTHOR       = "Lesiv"
BOT_NAME     = "MusicLSP"
AUTH_BOT     = "@MusicLSPauth_bot"
REF_LIMIT    = 3
SEARCH_PER_PAGE = 10
PLANS = {
    "week":  {"days": 7,  "price": 0.5,  "label": "7 днів — $0.50"},
    "month": {"days": 30, "price": 2.0,  "label": "30 днів — $2.00"},
}

# ─── Мови ─────────────────────────────────────────────────────────────────────
LANGUAGES = {
    "uk": "🇺🇦 Українська", "ru": "🇷🇺 Русский",   "en": "🇬🇧 English",
    "de": "🇩🇪 Deutsch",    "fr": "🇫🇷 Français",   "es": "🇪🇸 Español",
    "pl": "🇵🇱 Polski",     "tr": "🇹🇷 Türkçe",     "ar": "🇸🇦 العربية",
    "zh": "🇨🇳 中文",
}

TEXTS = {
    "welcome": {
        "uk": "🎵 <b>Ласкаво просимо до {bot}!</b>\n\n✅ У тебе є <b>{trial} дні безкоштовного доступу</b>\n\n👤 <i>Автор: {author}</i>",
        "ru": "🎵 <b>Добро пожаловать в {bot}!</b>\n\n✅ У тебя есть <b>{trial} дня бесплатного доступа</b>\n\n👤 <i>Автор: {author}</i>",
        "en": "🎵 <b>Welcome to {bot}!</b>\n\n✅ You have <b>{trial} days of free access</b>\n\n👤 <i>Author: {author}</i>",
        "de": "🎵 <b>Willkommen bei {bot}!</b>\n\n✅ Du hast <b>{trial} Tage kostenlosen Zugang</b>\n\n👤 <i>Autor: {author}</i>",
        "fr": "🎵 <b>Bienvenue sur {bot}!</b>\n\n✅ Vous avez <b>{trial} jours d'accès gratuit</b>\n\n👤 <i>Auteur: {author}</i>",
        "es": "🎵 <b>¡Bienvenido a {bot}!</b>\n\n✅ Tienes <b>{trial} días de acceso gratuito</b>\n\n👤 <i>Autor: {author}</i>",
        "pl": "🎵 <b>Witamy w {bot}!</b>\n\n✅ Masz <b>{trial} dni bezpłatnego dostępu</b>\n\n👤 <i>Autor: {author}</i>",
        "tr": "🎵 <b>{bot}'a Hoş Geldiniz!</b>\n\n✅ <b>{trial} günlük ücretsiz erişiminiz</b> var\n\n👤 <i>Yazar: {author}</i>",
        "ar": "🎵 <b>مرحباً بك في {bot}!</b>\n\n✅ لديك <b>{trial} أيام وصول مجاني</b>\n\n👤 <i>المؤلف: {author}</i>",
        "zh": "🎵 <b>欢迎来到 {bot}!</b>\n\n✅ 您有 <b>{trial} 天免费访问</b>\n\n👤 <i>作者: {author}</i>",
    },
    "no_access": {
        "uk": "⛔ Підписка закінчилась.\n\nОформи підписку → /subscription",
        "ru": "⛔ Подписка истекла.\n\nОформи подписку → /subscription",
        "en": "⛔ Subscription expired.\n\nGet subscription → /subscription",
        "de": "⛔ Abonnement abgelaufen.\n\n→ /subscription",
        "fr": "⛔ Abonnement expiré.\n\n→ /subscription",
        "es": "⛔ Suscripción expirada.\n\n→ /subscription",
        "pl": "⛔ Subskrypcja wygasła.\n\n→ /subscription",
        "tr": "⛔ Abonelik sona erdi.\n\n→ /subscription",
        "ar": "⛔ انتهى الاشتراك.\n\n→ /subscription",
        "zh": "⛔ 订阅已过期。\n\n→ /subscription",
    },
}

def tx(key, lang, **kw):
    s = TEXTS.get(key, {})
    t = s.get(lang) or s.get("en") or f"[{key}]"
    return t.format(**kw) if kw else t

# ─── Хелпери для callback_data ────────────────────────────────────────────────
def url_hash(url):
    return hashlib.md5(url.encode('utf-8')).hexdigest()[:8]

def cache_url(bot_data, url, title="", artist=""):
    bot_data.setdefault("url_cache", {})
    h = url_hash(url)
    bot_data["url_cache"][h] = {"url": url, "title": title, "artist": artist, "ts": datetime.datetime.utcnow().isoformat()}
    _clean_url_cache(bot_data)
    return h

def get_cached_url(bot_data, h):
    return bot_data.get("url_cache", {}).get(h, {})

def _clean_url_cache(bot_data):
    cache = bot_data.get("url_cache", {})
    now = datetime.datetime.utcnow()
    to_delete = []
    for h, data in cache.items():
        try:
            ts = datetime.datetime.fromisoformat(data.get("ts", "2000-01-01"))
            if (now - ts).total_seconds() > 3600:
                to_delete.append(h)
        except:
            to_delete.append(h)
    for h in to_delete:
        cache.pop(h, None)

# ─── База даних ───────────────────────────────────────────────────────────────
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT, lang TEXT DEFAULT 'uk',
            joined TEXT, trial_exp TEXT, sub_exp TEXT,
            referred_by INTEGER, ref_days INTEGER DEFAULT 0, state TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS keys (
            key TEXT PRIMARY KEY, days INTEGER, plan TEXT,
            used_by INTEGER DEFAULT NULL, used_at TEXT DEFAULT NULL
        );
        CREATE TABLE IF NOT EXISTS referrals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            inviter INTEGER, invitee INTEGER, created TEXT
        );
        CREATE TABLE IF NOT EXISTS daily_ref (
            user_id INTEGER, date TEXT, count INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, date)
        );
        CREATE TABLE IF NOT EXISTS library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, title TEXT, artist TEXT,
            url TEXT, kind TEXT DEFAULT 'track', added TEXT
        );
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, title TEXT, artist TEXT, played TEXT
        );
        CREATE TABLE IF NOT EXISTS promocodes (
            code TEXT PRIMARY KEY, discount INTEGER,
            uses_left INTEGER, created TEXT
        );
        CREATE TABLE IF NOT EXISTS playlists (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, name TEXT, description TEXT,
            created TEXT, updated TEXT
        );
        CREATE TABLE IF NOT EXISTS playlist_tracks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            playlist_id INTEGER, title TEXT, artist TEXT,
            url TEXT, duration TEXT, added TEXT,
            FOREIGN KEY(playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
        );
        """)

# ── Хелпери БД ────────────────────────────────────────────────────────────────
def get_user(uid):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def create_user(uid, username, ref=None):
    now = datetime.datetime.utcnow().isoformat()
    exp = (datetime.datetime.utcnow() + datetime.timedelta(days=TRIAL_DAYS)).isoformat()
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users (id,username,joined,trial_exp,referred_by) VALUES(?,?,?,?,?)",
                  (uid, username, now, exp, ref))

def get_lang(uid):
    u = get_user(uid)
    return u["lang"] if u else "en"

def set_lang(uid, lang):
    with db() as c:
        c.execute("UPDATE users SET lang=? WHERE id=?", (lang, uid))

def get_state(uid):
    u = get_user(uid)
    return u["state"] if u else ""

def set_state(uid, state):
    with db() as c:
        c.execute("UPDATE users SET state=? WHERE id=?", (state, uid))

def has_access(uid):
    u = get_user(uid)
    if not u: return False
    now = datetime.datetime.utcnow()
    if u["sub_exp"] and datetime.datetime.fromisoformat(u["sub_exp"]) > now:
        return True
    if u["trial_exp"] and datetime.datetime.fromisoformat(u["trial_exp"]) > now:
        return True
    return False

def extend_sub(uid, days):
    u = get_user(uid)
    now = datetime.datetime.utcnow()
    base = now
    if u and u["sub_exp"]:
        base = max(datetime.datetime.fromisoformat(u["sub_exp"]), now)
    new_exp = (base + datetime.timedelta(days=days)).isoformat()
    with db() as c:
        c.execute("UPDATE users SET sub_exp=? WHERE id=?", (new_exp, uid))

def get_sub_status(uid):
    u = get_user(uid)
    now = datetime.datetime.utcnow()
    if u and u["sub_exp"]:
        e = datetime.datetime.fromisoformat(u["sub_exp"])
        if e > now: return "active", e.strftime("%d.%m.%Y")
    if u and u["trial_exp"]:
        e = datetime.datetime.fromisoformat(u["trial_exp"])
        if e > now: return "trial", e.strftime("%d.%m.%Y")
    return "expired", "—"

def use_key(key, uid):
    with db() as c:
        r = c.execute("SELECT * FROM keys WHERE key=? AND used_by IS NULL", (key,)).fetchone()
        if not r: return None
        c.execute("UPDATE keys SET used_by=?, used_at=? WHERE key=?",
                  (uid, datetime.datetime.utcnow().isoformat(), key))
        return r["days"]

def add_key(key, days, plan):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO keys(key,days,plan) VALUES(?,?,?)", (key, days, plan))

def can_ref(uid):
    today = datetime.date.today().isoformat()
    with db() as c:
        r = c.execute("SELECT count FROM daily_ref WHERE user_id=? AND date=?", (uid, today)).fetchone()
        return (r["count"] if r else 0) < REF_LIMIT

def add_referral(inviter, invitee):
    now = datetime.datetime.utcnow().isoformat()
    today = datetime.date.today().isoformat()
    with db() as c:
        c.execute("INSERT INTO referrals(inviter,invitee,created) VALUES(?,?,?)", (inviter, invitee, now))
        c.execute("INSERT INTO daily_ref(user_id,date,count) VALUES(?,?,1) ON CONFLICT(user_id,date) DO UPDATE SET count=count+1", (inviter, today))
        c.execute("UPDATE users SET ref_days=ref_days+1 WHERE id=?", (inviter,))
    extend_sub(inviter, 1)

def get_ref_stats(uid):
    with db() as c:
        cnt = c.execute("SELECT COUNT(*) as c FROM referrals WHERE inviter=?", (uid,)).fetchone()["c"]
        u = get_user(uid)
        return {"count": cnt, "days": u["ref_days"] if u else 0}

def add_library(uid, title, artist, url, kind="track"):
    now = datetime.datetime.utcnow().isoformat()
    with db() as c:
        ex = c.execute("SELECT id FROM library WHERE user_id=? AND url=?", (uid, url)).fetchone()
        if not ex:
            c.execute("INSERT INTO library(user_id,title,artist,url,kind,added) VALUES(?,?,?,?,?,?)",
                      (uid, title, artist, url, kind, now))
            return True
    return False

def get_library(uid):
    with db() as c:
        return c.execute("SELECT * FROM library WHERE user_id=? ORDER BY added DESC", (uid,)).fetchall()

def del_library(uid, lid):
    with db() as c:
        c.execute("DELETE FROM library WHERE id=? AND user_id=?", (lid, uid))

def add_history(uid, title, artist):
    now = datetime.datetime.utcnow().isoformat()
    with db() as c:
        c.execute("INSERT INTO history(user_id,title,artist,played) VALUES(?,?,?,?)", (uid, title, artist, now))

def get_stats_user(uid):
    with db() as c:
        dl = c.execute("SELECT COUNT(*) as c FROM history WHERE user_id=?", (uid,)).fetchone()["c"]
        lb = c.execute("SELECT COUNT(*) as c FROM library WHERE user_id=?", (uid,)).fetchone()["c"]
    return {"dl": dl, "lib": lb}

def create_promo(code, discount, uses):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO promocodes(code,discount,uses_left,created) VALUES(?,?,?,?)",
                  (code, discount, uses, datetime.datetime.utcnow().isoformat()))

def use_promo(code):
    with db() as c:
        r = c.execute("SELECT * FROM promocodes WHERE code=? AND uses_left>0", (code,)).fetchone()
        if not r: return None
        c.execute("UPDATE promocodes SET uses_left=uses_left-1 WHERE code=?", (code,))
        return r["discount"]

def get_all_users():
    with db() as c:
        return c.execute("SELECT * FROM users").fetchall()

def get_global_stats():
    now = datetime.datetime.utcnow()
    with db() as c:
        total = c.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
        active = trial = 0
        for u in c.execute("SELECT * FROM users").fetchall():
            if u["sub_exp"] and datetime.datetime.fromisoformat(u["sub_exp"]) > now:
                active += 1
            elif u["trial_exp"] and datetime.datetime.fromisoformat(u["trial_exp"]) > now:
                trial += 1
        keys_used = c.execute("SELECT COUNT(*) as c FROM keys WHERE used_by IS NOT NULL").fetchone()["c"]
        keys_total = c.execute("SELECT COUNT(*) as c FROM keys").fetchone()["c"]
    return {"total": total, "active": active, "trial": trial, "ku": keys_used, "kt": keys_total}

# ─── ПЛЕЙЛИСТИ (НОВЕ) ──────────────────────────────────────────────────────────
def create_playlist(uid, name, description=""):
    now = datetime.datetime.utcnow().isoformat()
    with db() as c:
        c.execute("INSERT INTO playlists(user_id,name,description,created,updated) VALUES(?,?,?,?,?)",
                  (uid, name, description, now, now))
        return c.lastrowid

def get_playlists(uid):
    with db() as c:
        return c.execute("SELECT * FROM playlists WHERE user_id=? ORDER BY updated DESC", (uid,)).fetchall()

def get_playlist(pid):
    with db() as c:
        pl = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
        tracks = c.execute("SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY id", (pid,)).fetchall()
        return pl, tracks

def add_track_to_playlist(pid, title, artist, url, duration=""):
    now = datetime.datetime.utcnow().isoformat()
    with db() as c:
        c.execute("INSERT INTO playlist_tracks(playlist_id,title,artist,url,duration,added) VALUES(?,?,?,?,?,?)",
                  (pid, title, artist, url, duration, now))
        c.execute("UPDATE playlists SET updated=? WHERE id=?", (now, pid))

def delete_playlist(uid, pid):
    with db() as c:
        c.execute("DELETE FROM playlists WHERE id=? AND user_id=?", (pid, uid))

def delete_playlist_track(pid, tid):
    with db() as c:
        c.execute("DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", (tid, pid))

# ─── Музика: YouTube/SoundCloud пошук ─────────────────────────────────────────

def fmt_dur(s):
    if not s: return "—"
    m, sec = divmod(int(s), 60)
    return f"{m}:{sec:02d}"

def get_yt_opts(extra=None, use_cookies=True):
    """Повертає базові опції yt-dlp з обходом блокувань"""
    opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "noplaylist": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.youtube.com/",
        "headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["web"],
                "player_skip": ["webpage", "configs", "js"],
            }
        },
    }
    if extra:
        opts.update(extra)
    return opts

def yt_search(query, limit=10):
    """Пошук YouTube з fallback'ами"""
    opts = get_yt_opts()
    opts["extractor_args"] = {"youtube": {"player_client": ["web"], "player_skip": ["webpage", "configs", "js"]}}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            r = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
    except Exception as e:
        logger.warning(f"yt_search web failed: {e}")
        try:
            opts = get_yt_opts()
            opts["extractor_args"] = {"youtube": {"player_client": ["android"], "player_skip": ["webpage", "configs", "js"]}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                r = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        except Exception as e2:
            logger.error(f"yt_search fallback also failed: {e2}")
            return []
    
    tracks = []
    for e in (r.get("entries") or []):
        if not e: continue
        dur = e.get("duration", 0)
        if dur and dur > 900: continue
        tracks.append({
            "title": e.get("title", "Unknown"),
            "url": f"https://www.youtube.com/watch?v={e['id']}",
            "id": e["id"],
            "duration": fmt_dur(dur),
            "channel": e.get("channel") or e.get("uploader") or "—",
            "source": "youtube",
        })
    return tracks

def sc_search(query, limit=5):
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            r = ydl.extract_info(f"scsearch{limit}:{query}", download=False)
        except: return []
    tracks = []
    for e in (r.get("entries") or []):
        if not e: continue
        tracks.append({
            "title": e.get("title", "Unknown"),
            "url": e.get("webpage_url") or e.get("url", ""),
            "id": e.get("id", ""),
            "duration": fmt_dur(e.get("duration", 0)),
            "channel": e.get("uploader") or "SoundCloud",
            "source": "soundcloud",
        })
    return tracks

def search_all(query, limit=10):
    yt = yt_search(query, limit)
    sc = sc_search(query, 5)
    return (yt + sc)[:limit + 5]

def artist_songs(artist, limit=50):
    return yt_search(f"{artist} official audio", limit)

# ─── ПОШУК АЛЬБОМІВ (СТАРИЙ РОБОЧИЙ ВАРІАНТ) ───────────────────────────────
def search_album_tracks(album_name, limit=20):
    """
    Пошук альбому через YouTube плейлисти.
    Якщо не знайшло плейлист — шукає окремі треки.
    """
    try:
        opts = get_yt_opts({"playlistend": limit})
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Шукаємо плейлисти з альбомом
            search_query = f"ytsearch5:{album_name} album playlist"
            r = ydl.extract_info(search_query, download=False)
            
            for entry in (r.get("entries") or []):
                if not entry: continue
                title_lower = entry.get("title", "").lower()
                if "playlist" in title_lower or "album" in title_lower:
                    playlist_url = entry.get("url") or f"https://www.youtube.com/playlist?list={entry.get('id', '')}"
                    if "playlist?list=" in playlist_url:
                        try:
                            pr = ydl.extract_info(playlist_url, download=False)
                            tracks = []
                            for i, e in enumerate(pr.get("entries") or []):
                                if not e: continue
                                tracks.append({
                                    "title": e.get("title", "Unknown"),
                                    "url": f"https://www.youtube.com/watch?v={e['id']}",
                                    "id": e["id"],
                                    "duration": fmt_dur(e.get("duration", 0)),
                                    "channel": e.get("channel") or e.get("uploader") or "—",
                                    "source": "youtube",
                                })
                            if len(tracks) >= 3:
                                return tracks[:limit]
                        except:
                            continue
    except Exception as ex:
        logger.warning(f"Album playlist search failed: {ex}")
    
    # Fallback: шукаємо окремі треки
    tracks = yt_search(f"{album_name} album", limit + 10)
    filtered = []
    for t in tracks:
        title_lower = t["title"].lower()
        if any(bad in title_lower for bad in ["reaction", "review", "cover", "live", "concert"]):
            continue
        filtered.append(t)
    
    return filtered[:limit]

def get_top100():
    try:
        opts = get_yt_opts({"playlistend": 100})
        with yt_dlp.YoutubeDL(opts) as ydl:
            r = ydl.extract_info(
                "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI",
                download=False
            )
        tracks = []
        for i, e in enumerate(r.get("entries") or []):
            if not e: continue
            tracks.append({
                "title": e.get("title", "Unknown"),
                "url": f"https://www.youtube.com/watch?v={e['id']}",
                "duration": fmt_dur(e.get("duration", 0)),
                "channel": e.get("channel") or "—",
                "rank": i + 1, "source": "youtube",
            })
        return tracks[:100]
    except Exception as ex:
        logger.error(f"Top100: {ex}")
        return yt_search("top hits 2024", 50)
# ============================================================
#  MusicLSP — Частина 2: Завантаження, асинхронні обгортки, клавіатури
#  Оновлено: ZIP-архіви для платних, плейлисти
# ============================================================

import zipfile
import io

def download_mp3(url, out_dir, quality="192"):
    """
    Завантажує MP3 з обходом блокувань YouTube.
    """
    base_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.youtube.com/",
        "headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        },
    }
    
    clients = [
        {"player_client": ["web"], "player_skip": ["webpage", "configs", "js"]},
        {"player_client": ["android"], "player_skip": ["webpage", "configs", "js"]},
        {"player_client": ["ios"], "player_skip": ["webpage", "configs", "js"]},
        {"player_client": ["tv_embedded"], "player_skip": ["webpage", "configs", "js"]},
        {"player_client": ["web_embedded"], "player_skip": ["webpage", "configs", "js"]},
    ]
    
    for i, client in enumerate(clients):
        try:
            opts = dict(base_opts)
            opts["extractor_args"] = {"youtube": client}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    for f in Path(out_dir).glob("*.mp3"):
                        return str(f)
        except Exception as e:
            err_str = str(e).lower()
            if "sign in" in err_str or "bot" in err_str:
                logger.warning(f"Attempt {i+1}: YouTube вимагає авторизації")
            else:
                logger.warning(f"Download attempt {i+1} failed: {e}")
            continue
    
    if not has_cookies_file:
        try:
            import browser_cookie3
            opts = dict(base_opts)
            opts.pop("cookiefile", None)
            opts["cookiesfrombrowser"] = ("chrome",)
            opts["extractor_args"] = {"youtube": {"player_client": ["web"], "player_skip": ["webpage", "configs", "js"]}}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if info:
                    for f in Path(out_dir).glob("*.mp3"):
                        return str(f)
        except ImportError:
            logger.warning("browser_cookie3 not installed")
        except Exception as e:
            logger.warning(f"Browser cookies failed: {e}")
    
    logger.error(f"All download attempts failed for {url}")
    return None

async def async_search(query, limit=10):
    return await asyncio.get_event_loop().run_in_executor(None, search_all, query, limit)

async def async_artist(artist, limit=50):
    return await asyncio.get_event_loop().run_in_executor(None, artist_songs, artist, limit)

async def async_album_info(query):
    return await asyncio.get_event_loop().run_in_executor(None, search_album_info, query)

async def async_top100():
    return await asyncio.get_event_loop().run_in_executor(None, get_top100)

async def async_download(url, out_dir, quality="192"):
    return await asyncio.get_event_loop().run_in_executor(None, download_mp3, url, out_dir, quality)

# ─── ZIP-архів для альбому (ПЛАТНА ФІЧА) ─────────────────────────────────────
async def create_album_zip(tracks, quality="192"):
    """
    Створює ZIP-архів з усіма треками альбому.
    Повертає шлях до ZIP або None.
    """
    zip_buffer = io.BytesIO()
    downloaded = 0
    
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, track in enumerate(tracks):
                if not track.get("yt_url"):
                    continue
                try:
                    path = await async_download(track["yt_url"], tmp, quality)
                    if path and os.path.exists(path):
                        safe_name = f"{i+1:02d}. {track['title'][:50]}.mp3"
                        zf.write(path, safe_name)
                        downloaded += 1
                except Exception as e:
                    logger.error(f"ZIP track {i+1} failed: {e}")
                    continue
    
    if downloaded == 0:
        return None
    
    zip_buffer.seek(0)
    return zip_buffer

# ─── Клавіатури ───────────────────────────────────────────────────────────────
def main_kb(uid):
    l = get_lang(uid)
    btn = lambda text, data: InlineKeyboardButton(text, callback_data=data)
    labels = {
        "uk": ["🔍 Пошук", "💿 Альбоми", "📊 Топ 100", "📚 Бібліотека", "👤 Профіль", "💎 Підписка", "🎁 Реферал", "⚙️ Налаштування"],
        "ru": ["🔍 Поиск", "💿 Альбомы", "📊 Топ 100", "📚 Библиотека", "👤 Профиль", "💎 Подписка", "🎁 Реферал", "⚙️ Настройки"],
        "en": ["🔍 Search", "💿 Albums", "📊 Top 100", "📚 Library", "👤 Profile", "💎 Subscription", "🎁 Referral", "⚙️ Settings"],
        "de": ["🔍 Suchen", "💿 Alben", "📊 Top 100", "📚 Bibliothek", "👤 Profil", "💎 Abo", "🎁 Empfehlung", "⚙️ Einstellungen"],
        "fr": ["🔍 Chercher", "💿 Albums", "📊 Top 100", "📚 Bibliothèque", "👤 Profil", "💎 Abonnement", "🎁 Parrainage", "⚙️ Paramètres"],
        "es": ["🔍 Buscar", "💿 Álbumes", "📊 Top 100", "📚 Biblioteca", "👤 Perfil", "💎 Suscripción", "🎁 Referido", "⚙️ Ajustes"],
        "pl": ["🔍 Szukaj", "💿 Albumy", "📊 Top 100", "📚 Biblioteka", "👤 Profil", "💎 Subskrypcja", "🎁 Referral", "⚙️ Ustawienia"],
        "tr": ["🔍 Ara", "💿 Albümler", "📊 Top 100", "📚 Kütüphane", "👤 Profil", "💎 Abonelik", "🎁 Referans", "⚙️ Ayarlar"],
        "ar": ["🔍 بحث", "💿 ألبومات", "📊 أفضل 100", "📚 مكتبة", "👤 ملف", "💎 اشتراك", "🎁 إحالة", "⚙️ إعدادات"],
        "zh": ["🔍 搜索", "💿 专辑", "📊 前100", "📚 音乐库", "👤 个人", "💎 订阅", "🎁 推荐", "⚙️ 设置"],
    }
    lb = labels.get(l, labels["en"])
    back_label = {"uk":"◀️ Назад","ru":"◀️ Назад","en":"◀️ Back","de":"◀️ Zurück","fr":"◀️ Retour","es":"◀️ Volver","pl":"◀️ Wróć","tr":"◀️ Geri","ar":"◀️ رجوع","zh":"◀️ 返回"}.get(l,"◀️ Back")
    return InlineKeyboardMarkup([
        [btn(lb[0], "m:search"),  btn(lb[1], "m:albums")],
        [btn(lb[2], "m:top100"),  btn(lb[3], "m:library")],
        [btn(lb[4], "m:profile"), btn(lb[5], "m:sub")],
        [btn(lb[6], "m:ref"),     btn(lb[7], "m:settings")],
    ]), back_label

def back_btn(uid):
    l = get_lang(uid)
    labels = {"uk":"◀️ Назад","ru":"◀️ Назад","en":"◀️ Back","de":"◀️ Zurück","fr":"◀️ Retour","es":"◀️ Volver","pl":"◀️ Wróć","tr":"◀️ Geri","ar":"◀️ رجوع","zh":"◀️ 返回"}
    return InlineKeyboardButton(labels.get(l,"◀️ Back"), callback_data="m:home")

# ─── /start ───────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    ref = None
    if ctx.args:
        try:
            ref = int(ctx.args[0])
            if ref == uid: ref = None
        except: pass

    is_new = get_user(uid) is None
    create_user(uid, username, ref)

    if is_new and ref and get_user(ref):
        if can_ref(ref):
            add_referral(ref, uid)
            try:
                await ctx.bot.send_message(ref, "🎁 По твоєму запрошенню зареєструвався новий користувач!\n+1 день до підписки ✅")
            except: pass

    u = get_user(uid)
    if u and u["lang"] and u["lang"] != "uk":
        await show_welcome(update.message, uid)
        return

    keyboard = []
    row = []
    for code, name in LANGUAGES.items():
        row.append(InlineKeyboardButton(name, callback_data=f"lang:{code}"))
        if len(row) == 2:
            keyboard.append(row); row = []
    if row: keyboard.append(row)

    await update.message.reply_text(
        "🌍 <b>Choose your language / Оберіть мову</b>",
        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML"
    )

async def show_welcome(msg, uid):
    l = get_lang(uid)
    text = tx("welcome", l, bot=BOT_NAME, trial=TRIAL_DAYS, author=AUTHOR)
    kb, _ = main_kb(uid)
    await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")

# ─── Callbacks ────────────────────────────────────────────────────────────────
async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    l = get_lang(uid)

    # Мова
    if data.startswith("lang:"):
        set_lang(uid, data[5:])
        await q.message.delete()
        await show_welcome(q.message, uid)
        return

    # Головне меню
    if data == "m:home":
        kb, _ = main_kb(uid)
        try:
            await q.message.edit_text(f"🏠 <b>{BOT_NAME}</b>\n\nОбери дію 👇", reply_markup=kb, parse_mode="HTML")
        except:
            await q.message.reply_text(f"🏠 <b>{BOT_NAME}</b>\n\nОбери дію 👇", reply_markup=kb, parse_mode="HTML")
        return

    # Пошук
    if data == "m:search":
        set_state(uid, "searching")
        prompts = {"uk":"🔍 Введи назву пісні або артиста:","ru":"🔍 Введи название песни или артиста:","en":"🔍 Enter song name or artist:","de":"🔍 Songname oder Künstler:","fr":"🔍 Nom de chanson ou artiste:","es":"🔍 Nombre de canción o artista:","pl":"🔍 Nazwa piosenki lub artysty:","tr":"🔍 Şarkı adı veya sanatçı:","ar":"🔍 أدخل اسم الأغنية أو الفنان:","zh":"🔍 输入歌曲或艺术家:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    # Альбоми
    if data == "m:albums":
        set_state(uid, "album_search")
        prompts = {"uk":"💿 Введи назву альбому (наприклад: «Гуф 2009» або «Баста Гуф 2010»):","ru":"💿 Введи название альбома:","en":"💿 Enter album name:","de":"💿 Albumname eingeben:","fr":"💿 Entrez le nom de l'album:","es":"💿 Ingresa el nombre del álbum:","pl":"💿 Wprowadź nazwę albumu:","tr":"💿 Albüm adını girin:","ar":"💿 أدخل اسم الألبوم:","zh":"💿 输入专辑名称:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    # Топ 100
    if data == "m:top100":
        await show_top100(q.message, uid, ctx)
        return

    # Бібліотека
    if data == "m:library":
        await show_library(q.message, uid, ctx)
        return

    # Профіль
    if data == "m:profile":
        await show_profile(q.message, uid)
        return

    # Підписка
    if data == "m:sub":
        await show_sub(q.message, uid)
        return

    # Реферал
    if data == "m:ref":
        await show_ref(q.message, uid, ctx)
        return

    # Налаштування
    if data == "m:settings":
        await show_settings(q.message, uid, ctx)
        return

    # Ввід ключа
    if data == "sub:key":
        set_state(uid, "enter_key")
        prompts = {"uk":"🔑 Введи ключ активації:","ru":"🔑 Введи ключ активации:","en":"🔑 Enter activation key:"}
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return

    # Промокод
    if data == "sub:promo":
        set_state(uid, "enter_promo")
        await q.message.reply_text("🎟 Введи промокод:")
        return

    # Завантажити трек по кешу
    if data.startswith("dl|"):
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        parts = data.split("|", 2)
        idx, ck = int(parts[1]), parts[2]
        tracks = ctx.application.bot_data.get("cache", {}).get(ck, [])
        if not tracks or idx >= len(tracks):
            await q.message.reply_text("❌ Застарів результат. Шукай знову."); return
        t = tracks[idx]
        await do_download(q.message, t["url"], t["title"], t.get("channel",""), uid, ctx)
        return

    # Завантажити по URL (через хеш)
    if data.startswith("dlurl|"):
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        parts = data.split("|", 3)
        url_id = parts[1]
        title = parts[2] if len(parts) > 2 else "трек"
        artist = parts[3] if len(parts) > 3 else ""
        
        cached = get_cached_url(ctx.application.bot_data, url_id)
        url = cached.get("url", "")
        if not url:
            await q.message.reply_text("❌ Посилання застаріло. Спробуй знайти знову."); return
        
        await do_download(q.message, url, title, artist, uid, ctx)
        return

    # === НОВЕ: Завантаження треку з альбому ===
    if data.startswith("albumtrack|"):
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        parts = data.split("|", 2)
        album_ck = parts[1]
        track_idx = int(parts[2])
        
        album_data = ctx.application.bot_data.get("album_cache", {}).get(album_ck)
        if not album_data or track_idx >= len(album_data.get("tracks", [])):
            await q.message.reply_text("❌ Дані альбому застаріли. Шукай знову.")
            return
        
        track = album_data["tracks"][track_idx]
        if not track.get("yt_url"):
            await q.message.reply_text("❌ Не знайдено посилання на цей трек.")
            return
        
        await do_download(q.message, track["yt_url"], track["title"], album_data["artist"], uid, ctx)
        return

    # === НОВЕ: Завантажити весь альбом ZIP ===
    if data == "albumzip":
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        
        album_ck = ctx.application.bot_data.get("last_album_ck", "")
        album_data = ctx.application.bot_data.get("album_cache", {}).get(album_ck)
        
        if not album_data:
            await q.message.reply_text("❌ Дані альбому застаріли.")
            return
        
        await do_download_album_zip(q.message, album_data, uid, ctx)
        return

    # === НОВЕ: Додати альбом у плейлист ===
    if data.startswith("addpl|"):
        parts = data.split("|", 2)
        album_ck = parts[1]
        album_data = ctx.application.bot_data.get("album_cache", {}).get(album_ck)
        if album_data:
            # Зберігаємо в бібліотеку як "album"
            first_track = album_data["tracks"][0] if album_data["tracks"] else {}
            url = first_track.get("yt_url", "")
            added = add_library(uid, album_data["title"], album_data["artist"], url, kind="album")
            await q.answer("✅ Альбом додано до бібліотеки!" if added else "ℹ️ Вже є в бібліотеці.", show_alert=True)
        return

    # Всі пісні артиста — ВВЕДЕННЯ ІМЕНІ
    if data == "artist_input":
        set_state(uid, "artist_input")
        prompts = {"uk":"🎤 Введи ім'я артиста:","ru":"🎤 Введи имя артиста:","en":"🎤 Enter artist name:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    # Скачати 20 пісень — ВВЕДЕННЯ ІМЕНІ
    if data == "dl20_input":
        set_state(uid, "dl20_input")
        prompts = {"uk":"⬇️ Введи ім'я артиста для завантаження 20 пісень:","ru":"⬇️ Введи имя артиста для скачивания 20 песен:","en":"⬇️ Enter artist name to download 20 songs:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    # Додати трек в бібліотеку
    if data.startswith("addlib|"):
        url_id = data[7:]
        cached = get_cached_url(ctx.application.bot_data, url_id)
        url = cached.get("url", "")
        title = cached.get("title", "Unknown")
        artist = cached.get("artist", "")
        
        if not url:
            await q.answer("❌ Посилання застаріло.", show_alert=True)
            return
            
        added = add_library(uid, title, artist, url)
        await q.answer("✅ Додано до бібліотеки!" if added else "ℹ️ Вже є в бібліотеці.", show_alert=True)
        return

    # Видалити з бібліотеки
    if data.startswith("libdel|"):
        del_library(uid, int(data[7:]))
        await show_library(q.message, uid, ctx)
        return

    # Топ 100 сторінка
    if data.startswith("t100p|"):
        page = int(data[6:])
        tracks = ctx.application.bot_data.get("top100", [])
        await show_top100_page(q.message, uid, tracks, page, edit=True)
        return

    # Пагінація пошуку
    if data.startswith("searchp|"):
        parts = data.split("|", 2)
        query = parts[1]
        page = int(parts[2]) if len(parts) > 2 else 0
        await do_search_paged(q.message, query, uid, ctx, page, edit=True)
        return

    # Якість
    if data.startswith("quality|"):
        q_val = data[8:]
        ctx.application.bot_data.setdefault("quality", {})[uid] = q_val
        await q.answer(f"✅ Якість: {q_val} kbps", show_alert=True)
        return
# ============================================================
#  MusicLSP — Частина 3: Повідомлення, завантаження, адмін, main
#  ОНОВЛЕНО: Альбоми з MusicBrainz, ZIP, плейлисти
# ============================================================

# ─── Повідомлення ─────────────────────────────────────────────────────────────
async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text.strip()
    l = get_lang(uid)
    state = get_state(uid)

    if not get_user(uid):
        create_user(uid, update.effective_user.username or "")

    # Ключ активації
    if state == "enter_key":
        set_state(uid, "")
        days = use_key(text, uid)
        if days:
            extend_sub(uid, days)
            msgs = {"uk":f"✅ Ключ активовано! Доступ на <b>{days} днів</b>.","ru":f"✅ Ключ активирован! Доступ на <b>{days} дней</b>.","en":f"✅ Key activated! Access for <b>{days} days</b>."}
            await update.message.reply_text(msgs.get(l, msgs["en"]), parse_mode="HTML")
        else:
            errs = {"uk":"❌ Невірний або вже використаний ключ.","ru":"❌ Неверный или использованный ключ.","en":"❌ Invalid or used key."}
            await update.message.reply_text(errs.get(l, errs["en"]))
        return

    # Промокод
    if state == "enter_promo":
        set_state(uid, "")
        disc = use_promo(text.upper())
        if disc:
            await update.message.reply_text(f"✅ Промокод активовано! Знижка <b>{disc}%</b>\nПокажи при оплаті в {AUTH_BOT}", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ Невірний або вичерпаний промокод.")
        return

    # Пошук альбому через MusicBrainz
    if state == "album_search":
        set_state(uid, "")
        if not has_access(uid):
            await update.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        await do_album_search_mb(update, text, uid, ctx)
        return

    # ВВЕДЕННЯ АРТИСТА — всі пісні
    if state == "artist_input":
        set_state(uid, "")
        if not has_access(uid):
            await update.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        await show_artist(update.message, text, uid, ctx)
        return

    # ВВЕДЕННЯ АРТИСТА — скачати 20 пісень
    if state == "dl20_input":
        set_state(uid, "")
        if not has_access(uid):
            await update.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        await batch_download(update.message, text, uid, ctx)
        return

    # Адмін введення
    if uid == ADMIN_ID and state.startswith("adm:"):
        await handle_admin_input(update, ctx, state, text)
        return

    # Звичайний пошук з пагінацією
    set_state(uid, "")
    if not has_access(uid):
        await update.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
    await do_search_paged(update, text, uid, ctx, page=0, edit=False)

# ─── Пошук з пагінацією ───────────────────────────────────────────────────────
async def do_search_paged(update_or_msg, query, uid, ctx, page=0, edit=False):
    l = get_lang(uid)
    
    if edit:
        msg = update_or_msg
        await msg.edit_text(f"🔍 <b>{query}</b> (стор. {page+1})…", parse_mode="HTML")
    else:
        msg = await update_or_msg.message.reply_text(f"🔍 <b>{query}</b>…", parse_mode="HTML")
    
    all_tracks = await async_search(query, limit=30)
    
    if not all_tracks:
        await msg.edit_text("😔 Нічого не знайдено.")
        return

    ck = f"search_{uid}_{msg.message_id}"
    ctx.application.bot_data.setdefault("cache", {})[ck] = all_tracks

    start = page * SEARCH_PER_PAGE
    end = start + SEARCH_PER_PAGE
    tracks = all_tracks[start:end]
    has_more = len(all_tracks) > end

    kb = []
    for i, t in enumerate(tracks):
        global_idx = start + i
        icon = "🎵" if t.get("source") == "youtube" else "☁️"
        kb.append([InlineKeyboardButton(f"{icon} {t['title'][:42]} ({t['duration']})", callback_data=f"dl|{global_idx}|{ck}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ Попередня", callback_data=f"searchp|{query}|{page-1}"))
    if has_more:
        nav.append(InlineKeyboardButton("➡️ Наступна", callback_data=f"searchp|{query}|{page+1}"))
    if nav:
        kb.append(nav)

    if tracks:
        artist = tracks[0]["channel"]
        dl_labels = {"uk":"⬇️ Скачати 20 пісень","ru":"⬇️ Скачать 20 песен","en":"⬇️ Download 20 songs"}
        all_labels = {"uk":"🎤 Всі пісні артиста","ru":"🎤 Все песни артиста","en":"🎤 All artist songs"}
        kb.append([
            InlineKeyboardButton(all_labels.get(l, all_labels["en"]), callback_data="artist_input"),
            InlineKeyboardButton(dl_labels.get(l, dl_labels["en"]), callback_data="dl20_input"),
        ])
    
    kb.append([back_btn(uid)])

    text = f"🎶 <b>{query}</b> — {start+1}-{min(end, len(all_tracks))} з {len(all_tracks)}\n\nОбери пісню 👇"
    
    if edit:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    else:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Пошук альбому через MusicBrainz ──────────────────────────────────────────
async def do_album_search_mb(update, query, uid, ctx):
    l = get_lang(uid)
    msg = await update.message.reply_text(f"💿 Шукаю альбом: <b>{query}</b>…", parse_mode="HTML")
    
    album_data = await async_album_info(query)
    
    if not album_data or not album_data.get("tracks"):
        await msg.edit_text(
            "😔 Альбом не знайдено в базі.\n\nСпробую знайти через YouTube…",
            parse_mode="HTML"
        )
        await do_album_search_fallback(update, query, uid, ctx, msg)
        return
    
    album_ck = f"album_{uid}_{msg.message_id}"
    ctx.application.bot_data.setdefault("album_cache", {})[album_ck] = album_data
    ctx.application.bot_data["last_album_ck"] = album_ck
    
    tags_str = ", ".join(album_data.get("tags", [])) if album_data.get("tags") else ""
    wiki_text = album_data.get("wiki", "")[:200] + "..." if len(album_data.get("wiki", "")) > 200 else album_data.get("wiki", "")
    
    text = (
        f"💿 <b>{album_data['title']}</b>\n"
        f"🎤 {album_data['artist']}\n"
        f"📅 {album_data['year'] or 'Невідомо'}\n"
    )
    if album_data.get("label"):
        text += f"🏷️ {album_data['label']}\n"
    if tags_str:
        text += f"📝 {tags_str}\n"
    text += (
        f"🔢 {album_data['track_count']} треків\n"
        f"⏱️ {album_data['total_duration']}\n"
    )
    if album_data.get("listeners"):
        text += f"👥 {int(album_data['listeners']):,} слухачів\n"
    if wiki_text:
        text += f"\n📖 <i>{wiki_text}</i>\n"
    text += f"\n🎵 <b>Треклист:</b>"
    
    kb = []
    show_tracks = album_data["tracks"][:15]
    for i, track in enumerate(show_tracks):
        btn_text = f"{track['position'] or i+1}. {track['title'][:35]} ({track['duration']})"
        kb.append([InlineKeyboardButton(btn_text, callback_data=f"albumtrack|{album_ck}|{i}")])
    
    if len(album_data["tracks"]) > 15:
        kb.append([InlineKeyboardButton(f"➕ Ще {len(album_data['tracks']) - 15} треків", callback_data=f"albummore|{album_ck}|15")])
    
    action_row = []
    action_row.append(InlineKeyboardButton("📚 До бібліотеки", callback_data=f"addpl|{album_ck}"))
    
    if has_access(uid):
        action_row.append(InlineKeyboardButton("⬇️ ZIP всі", callback_data="albumzip"))
    
    kb.append(action_row)
    kb.append([back_btn(uid)])
    
    image_url = album_data.get("image", "")
    if image_url:
        try:
            await msg.delete()
            await update.message.reply_photo(
                photo=image_url,
                caption=text,
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.warning(f"Failed to send album cover: {e}")
    
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def do_album_search_fallback(update, query, uid, ctx, msg):
    tracks = await async_search(f"{query} album", limit=20)
    if not tracks:
        await msg.edit_text("😔 Альбом не знайдено.")
        return
    
    ck = f"alb_{uid}_{msg.message_id}"
    ctx.application.bot_data.setdefault("cache", {})[ck] = tracks
    
    kb = []
    for i, t in enumerate(tracks[:10]):
        kb.append([InlineKeyboardButton(f"🎵 {t['title'][:42]} ({t['duration']})", callback_data=f"dl|{i}|{ck}")])
    
    url_id = cache_url(ctx.application.bot_data, tracks[0]["url"], query, "Album") if tracks else ""
    add_labels = {"uk":"📚 Додати альбом в бібліотеку","ru":"📚 Добавить альбом в библиотеку","en":"📚 Add album to library"}
    l = get_lang(uid)
    kb.append([InlineKeyboardButton(add_labels.get(l, add_labels["en"]), callback_data=f"addalbum|{query}|{url_id}")])
    kb.append([back_btn(uid)])
    
    await msg.edit_text(
        f"💿 <b>{query}</b> — знайдено {len(tracks)} треків\n\nОбери пісню 👇",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
    )

# ─── Завантаження ZIP альбому ────────────────────────────────────────────────
async def do_download_album_zip(msg, album_data, uid, ctx):
    l = get_lang(uid)
    status = await msg.reply_text("⬇️ Створюю ZIP-архів…\n<i>Це може зайняти кілька хвилин</i>", parse_mode="HTML")
    
    quality = ctx.application.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    
    try:
        zip_buffer = await create_album_zip(album_data["tracks"], quality)
        
        if not zip_buffer:
            await status.edit_text("❌ Не вдалось завантажити жодного треку.")
            return
        
        size_mb = len(zip_buffer.getvalue()) / 1024 / 1024
        if size_mb > 50:
            await status.edit_text(f"❌ Архів {size_mb:.1f}МБ — завеликий для Telegram.")
            return
        
        await status.edit_text("📤 Відправляю архів…")
        
        safe_name = f"{album_data['artist'][:30]} - {album_data['title'][:30]}.zip"
        safe_name = "".join(c for c in safe_name if c.isalnum() or c in " -_.")
        
        await msg.reply_document(
            document=zip_buffer,
            filename=safe_name,
            caption=f"💿 <b>{album_data['title']}</b>\n🎤 {album_data['artist']}\n📦 {len(album_data['tracks'])} треків",
            parse_mode="HTML"
        )
        await status.delete()
        
    except Exception as e:
        logger.error(f"ZIP download error: {e}")
        await status.edit_text("❌ Помилка створення архіву. Спробуй завантажити треки окремо.")

# ─── Всі пісні артиста ────────────────────────────────────────────────────────
async def show_artist(msg, artist, uid, ctx):
    status = await msg.reply_text(f"🔍 <b>{artist}</b>…", parse_mode="HTML")
    tracks = await async_artist(artist, 50)
    if not tracks:
        await status.edit_text("😔 Нічого не знайдено."); return

    kb = []
    for t in tracks:
        url_id = cache_url(ctx.application.bot_data, t["url"], t["title"], t.get("channel", ""))
        kb.append([InlineKeyboardButton(f"🎵 {t['title'][:42]} ({t['duration']})", callback_data=f"dlurl|{url_id}|{t['title'][:30]}|{t['channel'][:20]}")])
    kb.append([back_btn(uid)])

    per = 40
    for i, start in enumerate(range(0, len(kb) - 1, per)):
        chunk = kb[start:start+per] + ([kb[-1]] if start + per >= len(kb) - 1 else [])
        end = min(start + per, len(tracks))
        if i == 0:
            await status.edit_text(f"🎤 <b>{artist}</b> — {len(tracks)} пісень:", reply_markup=InlineKeyboardMarkup(chunk), parse_mode="HTML")
        else:
            await msg.reply_text(f"🎤 <b>{artist}</b> ({start+1}–{end}):", reply_markup=InlineKeyboardMarkup(chunk), parse_mode="HTML")

# ─── Batch download ───────────────────────────────────────────────────────────
async def batch_download(msg, artist, uid, ctx):
    status = await msg.reply_text(f"⬇️ Завантажую 20 пісень <b>{artist}</b>…\n<i>Кілька хвилин</i>", parse_mode="HTML")
    tracks = await async_artist(artist, 20)
    if not tracks:
        await status.edit_text("😔 Нічого не знайдено."); return

    quality = ctx.application.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    ok = 0
    for i, t in enumerate(tracks):
        await status.edit_text(f"⬇️ {i+1}/20: <b>{t['title'][:40]}</b>…", parse_mode="HTML")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                path = await async_download(t["url"], tmp, quality)
                if path and os.path.exists(path) and os.path.getsize(path) / 1024 / 1024 <= MAX_MB:
                    with open(path, "rb") as f:
                        await msg.reply_audio(audio=f, title=t["title"][:64], performer=t["channel"][:64], filename=f"{t['title'][:50]}.mp3")
                    add_history(uid, t["title"], t["channel"])
                    ok += 1
            except Exception as e:
                logger.error(f"Batch: {e}")
    await status.edit_text(f"✅ Завантажено {ok} з {len(tracks)} пісень!")

# ─── Топ 100 ──────────────────────────────────────────────────────────────────
async def show_top100(msg, uid, ctx):
    try:
        status = await msg.edit_text("📊 Завантажую Топ 100…")
    except:
        status = await msg.reply_text("📊 Завантажую Топ 100…")
    tracks = await async_top100()
    ctx.application.bot_data["top100"] = tracks
    await show_top100_page(status, uid, tracks, 0, edit=True)

async def show_top100_page(msg, uid, tracks, page, edit=False):
    per = 20
    start = page * per
    end = min(start + per, len(tracks))
    chunk = tracks[start:end]

    kb = []
    for i, t in enumerate(chunk):
        rank = t.get("rank", start + i + 1)
        url_id = cache_url(msg.get_bot().bot_data if hasattr(msg, 'get_bot') else {}, t["url"], t["title"], "")
        kb.append([InlineKeyboardButton(f"#{rank} {t['title'][:40]}", callback_data=f"dlurl|{url_id}|{t['title'][:30]}")])

    nav = []
    if page > 0: nav.append(InlineKeyboardButton("◀️", callback_data=f"t100p|{page-1}"))
    if end < len(tracks): nav.append(InlineKeyboardButton("▶️", callback_data=f"t100p|{page+1}"))
    if nav: kb.append(nav)
    kb.append([back_btn(uid)])

    text = f"📊 <b>Топ 100</b> — #{start+1}–#{end}:"
    if edit:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    else:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Бібліотека ───────────────────────────────────────────────────────────────
async def show_library(msg, uid, ctx):
    songs = get_library(uid)
    l = get_lang(uid)
    titles = {"uk":"📚 Моя бібліотека","ru":"📚 Моя библиотека","en":"📚 My Library"}
    empty = {"uk":"Поки порожньо. Шукай музику і додавай!","ru":"Пока пусто. Ищи музыку и добавляй!","en":"Empty. Search and add songs!"}

    if not songs:
        text = f"{titles.get(l,titles['en'])}\n\n{empty.get(l,empty['en'])}"
        kb = InlineKeyboardMarkup([[back_btn(uid)]])
        try:
            await msg.edit_text(text, reply_markup=kb, parse_mode="HTML")
        except:
            await msg.reply_text(text, reply_markup=kb, parse_mode="HTML")
        return

    kb = []
    for s in songs[:40]:
        icon = "💿" if s["kind"] == "album" else "🎵"
        url_id = cache_url(ctx.application.bot_data, s["url"], s["title"], s["artist"])
        kb.append([
            InlineKeyboardButton(f"{icon} {s['title'][:35]}", callback_data=f"dlurl|{url_id}|{s['title'][:30]}|{s['artist'][:20]}"),
            InlineKeyboardButton("🗑", callback_data=f"libdel|{s['id']}")
        ])
    kb.append([back_btn(uid)])

    text = f"{titles.get(l,titles['en'])} — {len(songs)} записів:"
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as e:
        logger.warning(f"Library edit_text failed, using reply_text: {e}")
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Профіль ──────────────────────────────────────────────────────────────────
async def show_profile(msg, uid):
    u = get_user(uid)
    stats = get_stats_user(uid)
    ref = get_ref_stats(uid)
    status, expires = get_sub_status(uid)
    status_icons = {"active":"✅ Активна","trial":"🆓 Пробна","expired":"❌ Закінчилась"}
    joined = u["joined"][:10] if u and u["joined"] else "—"

    text = (
        f"👤 <b>Профіль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"📅 В боті з: {joined}\n"
        f"💎 Підписка: {status_icons.get(status,'—')} (до {expires})\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• Скачано: {stats['dl']}\n"
        f"• В бібліотеці: {stats['lib']}\n"
        f"• Рефералів: {ref['count']}\n"
        f"• Зароблено днів: {ref['days']}"
    )
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
    except:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")

# ─── Підписка ─────────────────────────────────────────────────────────────────
async def show_sub(msg, uid):
    l = get_lang(uid)
    status, expires = get_sub_status(uid)
    icons = {"active":"✅ Активна","trial":"🆓 Пробна","expired":"❌ Закінчилась"}
    text = (
        f"💎 <b>Підписка</b>\n\n"
        f"Статус: {icons.get(status,'—')} (до {expires})\n\n"
        f"💰 <b>Тарифи:</b>\n• 7 днів — $0.50\n• 30 днів — $2.00\n\n"
        f"Оплата: {AUTH_BOT}"
    )
    kb = [
        [InlineKeyboardButton("💳 Оплатити", url=f"https://t.me/MusicLSPauth_bot")],
        [InlineKeyboardButton("🔑 Ввести ключ", callback_data="sub:key"),
         InlineKeyboardButton("🎟 Промокод", callback_data="sub:promo")],
        [back_btn(uid)],
    ]
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Реферал ──────────────────────────────────────────────────────────────────
async def show_ref(msg, uid, ctx):
    stats = get_ref_stats(uid)
    bot_info = await ctx.bot.get_me()
    link = f"https://t.me/{bot_info.username}?start={uid}"
    text = (
        f"🎁 <b>Реферальна програма</b>\n\n"
        f"Запроси друга — отримай +1 день!\nМакс: 3 на день\n\n"
        f"👥 Рефералів: <b>{stats['count']}</b>\n"
        f"📅 Зароблено днів: <b>{stats['days']}</b>\n\n"
        f"🔗 Твоє посилання:\n<code>{link}</code>"
    )
    try:
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
    except:
        await msg.reply_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")

# ─── Налаштування ─────────────────────────────────────────────────────────────
async def show_settings(msg, uid, ctx):
    cur_q = ctx.application.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
    lang_kb = []
    row = []
    for code, name in LANGUAGES.items():
        row.append(InlineKeyboardButton(name, callback_data=f"lang:{code}"))
        if len(row) == 2: lang_kb.append(row); row = []
    if row: lang_kb.append(row)
    q_row = [InlineKeyboardButton(f"{'✅' if q==cur_q else ''}{q}kbps", callback_data=f"quality|{q}") for q in ["128","192","320"]]
    try:
        await msg.edit_text("⚙️ <b>Налаштування</b>\n\n🌍 Мова | 🎵 Якість MP3:", reply_markup=InlineKeyboardMarkup(lang_kb + [q_row, [back_btn(uid)]]), parse_mode="HTML")
    except:
        await msg.reply_text("⚙️ <b>Налаштування</b>\n\n🌍 Мова | 🎵 Якість MP3:", reply_markup=InlineKeyboardMarkup(lang_kb + [q_row, [back_btn(uid)]]), parse_mode="HTML")

# ─── Завантаження треку ──────────────────────────────────────────────────────
async def do_download(msg, url, title, artist, uid, ctx):
    l = get_lang(uid)
    status = await msg.reply_text(f"⬇️ Завантажую: <b>{title[:50]}</b>…\n<i>(10–30 сек)</i>", parse_mode="HTML")
    quality = ctx.application.bot_data.get("quality", {}).get(uid, DEF_QUALITY)

    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = await async_download(url, tmp, quality)
            if not path or not os.path.exists(path):
                has_cookies = os.path.exists("youtube_cookies.txt")
                if not has_cookies:
                    await status.edit_text(
                        "❌ YouTube заблокував завантаження.\n\n"
                        "💡 <b>Потрібен cookies файл!</b>\n\n"
                        "1. Встанови розширення «Get cookies.txt LOCALLY» в Chrome\n"
                        "2. Зайди на youtube.com (будь авторизованим)\n"
                        "3. Експортуй cookies → збережи як <code>youtube_cookies.txt</code>\n"
                        "4. Завантаж файл у корінь проєкту на Railway\n\n"
                        "Або онови yt-dlp: <code>pip install -U yt-dlp</code>",
                        parse_mode="HTML"
                    )
                else:
                    await status.edit_text(
                        "❌ Не вдалось завантажити навіть з cookies.\n\n"
                        "💡 Спробуй:\n"
                        "1. Оновити cookies файл (він міг застаріти)\n"
                        "2. Оновити yt-dlp: <code>pip install -U yt-dlp</code>\n"
                        "3. Спробувати іншу пісню",
                        parse_mode="HTML"
                    )
                return

            size = os.path.getsize(path) / 1024 / 1024
            if size > MAX_MB:
                await status.edit_text(f"❌ Файл {size:.1f}МБ — завеликий для Telegram.")
                return

            await status.edit_text("📤 Відправляю…")
            
            url_id = cache_url(ctx.application.bot_data, url, title, artist)
            
            add_labels = {"uk":"📚 До бібліотеки","ru":"📚 В библиотеку","en":"📚 Add to Library"}
            kb = [[
                InlineKeyboardButton(
                    add_labels.get(l, add_labels["en"]), 
                    callback_data=f"addlib|{url_id}"
                ),
            ]]
            
            with open(path, "rb") as f:
                await msg.reply_audio(
                    audio=f, 
                    title=title[:64], 
                    performer=artist[:64] or None, 
                    filename=f"{title[:50]}.mp3", 
                    reply_markup=InlineKeyboardMarkup(kb)
                )
            add_history(uid, title, artist)
            await status.delete()

        except yt_dlp.utils.DownloadError as e:
            error_msg = str(e).lower()
            if "age" in error_msg or "restrict" in error_msg:
                await status.edit_text("❌ Відео має обмеження за віком. Спробуй іншу пісню.")
            elif "private" in error_msg:
                await status.edit_text("❌ Приватне відео. Спробуй іншу пісню.")
            elif "removed" in error_msg or "deleted" in error_msg:
                await status.edit_text("❌ Відео видалено. Спробуй іншу пісню.")
            elif "unavailable" in error_msg:
                await status.edit_text("❌ Відео недоступне в твоїй країні. Спробуй іншу пісню.")
            elif "sign in" in error_msg or "login" in error_msg or "bot" in error_msg:
                await status.edit_text(
                    "❌ YouTube вимагає авторизацію (cookies).\n\n"
                    "💡 Створи <code>youtube_cookies.txt</code> через розширення «Get cookies.txt LOCALLY»",
                    parse_mode="HTML"
                )
            else:
                logger.error(f"DownloadError: {e}")
                await status.edit_text("❌ Помилка завантаження. Спробуй іншу пісню.")
        except Exception as e:
            logger.error(f"Download unexpected error: {e}")
            await status.edit_text("❌ Помилка завантаження. Спробуй іншу пісню.")

# ─── Адмін ────────────────────────────────────────────────────────────────────
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    s = get_global_stats()
    kb = [
        [InlineKeyboardButton("📊 Статистика", callback_data="adm:stats"),
         InlineKeyboardButton("👥 Юзери", callback_data="adm:users")],
        [InlineKeyboardButton("🔑 Новий ключ", callback_data="adm:key"),
         InlineKeyboardButton("🎟 Промокод", callback_data="adm:promo")],
        [InlineKeyboardButton("📢 Розсилка", callback_data="adm:broadcast"),
         InlineKeyboardButton("🔑 Всі ключі", callback_data="adm:keys")],
        [InlineKeyboardButton("👤 Дати доступ", callback_data="adm:give"),
         InlineKeyboardButton("🎟 Промокоди", callback_data="adm:promos")],
    ]
    await update.message.reply_text(
        f"🔧 <b>Адмін панель</b>\n\n"
        f"👥 Всього: <b>{s['total']}</b>\n"
        f"💎 Активних: <b>{s['active']}</b>\n"
        f"🆓 Пробних: <b>{s['trial']}</b>\n"
        f"🔑 Ключів: {s['ku']}/{s['kt']}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
    )

async def on_admin_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != ADMIN_ID: return
    await q.answer()
    data = q.data

    if data == "adm:stats":
        s = get_global_stats()
        await q.message.reply_text(f"📊 Всього: {s['total']}\n💎 Активних: {s['active']}\n🆓 Пробних: {s['trial']}\n🔑 Ключів: {s['ku']}/{s['kt']}")

    elif data == "adm:users":
        users = get_all_users()
        text = "👥 <b>Останні 20 юзерів:</b>\n\n"
        for u in list(users)[-20:]:
            text += f"• <code>{u['id']}</code> @{u['username'] or '—'}\n"
        await q.message.reply_text(text, parse_mode="HTML")

    elif data == "adm:key":
        set_state(ADMIN_ID, "adm:newkey")
        await q.message.reply_text("🔑 Введи кількість днів (7 або 30):")

    elif data == "adm:give":
        set_state(ADMIN_ID, "adm:give")
        await q.message.reply_text("👤 Введи: <code>USER_ID КІЛЬКІСТЬ_ДНІВ</code>\n\nПриклад: <code>123456789 30</code>", parse_mode="HTML")

    elif data == "adm:broadcast":
        set_state(ADMIN_ID, "adm:broadcast")
        await q.message.reply_text("📢 Введи текст розсилки:")

    elif data == "adm:promo":
        set_state(ADMIN_ID, "adm:promo")
        await q.message.reply_text("🎟 Введи: <code>КОД ВІДСОТОК КІЛЬКІСТЬ</code>\n\nПриклад: <code>SAVE20 20 100</code>", parse_mode="HTML")

    elif data == "adm:keys":
        with db() as c:
            keys = c.execute("SELECT * FROM keys ORDER BY rowid DESC LIMIT 20").fetchall()
        if not keys:
            await q.message.reply_text("Ключів немає."); return
        text = "🔑 <b>Останні ключі:</b>\n\n"
        for k in keys:
            used = f"✅ юзер {k['used_by']}" if k["used_by"] else "⏳ Вільний"
            text += f"<code>{k['key']}</code> — {k['days']}д — {used}\n"
        await q.message.reply_text(text, parse_mode="HTML")

    elif data == "adm:promos":
        with db() as c:
            promos = c.execute("SELECT * FROM promocodes").fetchall()
        if not promos:
            await q.message.reply_text("Промокодів немає."); return
        text = "🎟 <b>Промокоди:</b>\n\n"
        for p in promos:
            text += f"<code>{p['code']}</code> — {p['discount']}% — залишилось: {p['uses_left']}\n"
        await q.message.reply_text(text, parse_mode="HTML")

async def handle_admin_input(update, ctx, state, text):
    uid = update.effective_user.id
    set_state(uid, "")

    if state == "adm:newkey":
        try:
            days = int(text)
            key = "LSP-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))
            plan = "week" if days <= 7 else "month"
            add_key(key, days, plan)
            await update.message.reply_text(f"✅ Ключ створено:\n<code>{key}</code>\nДнів: {days}", parse_mode="HTML")
        except:
            await update.message.reply_text("❌ Введи число днів.")

    elif state == "adm:give":
        parts = text.split()
        if len(parts) == 2:
            try:
                target_id, days = int(parts[0]), int(parts[1])
                extend_sub(target_id, days)
                try:
                    await ctx.bot.send_message(
                        target_id,
                        f"🎁 Адмін надав тобі доступ на <b>{days} днів</b>!\n\nПриємного прослуховування 🎵",
                        parse_mode="HTML"
                    )
                except: pass
                await update.message.reply_text(f"✅ Юзеру <code>{target_id}</code> додано {days} днів доступу.", parse_mode="HTML")
            except:
                await update.message.reply_text("❌ Формат: USER_ID ДНІВ\nПриклад: 123456789 30")
        else:
            await update.message.reply_text("❌ Формат: USER_ID ДНІВ\nПриклад: 123456789 30")

    elif state == "adm:broadcast":
        users = get_all_users()
        sent = 0
        for u in users:
            try:
                await ctx.bot.send_message(u["id"], text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.05)
            except: pass
        await update.message.reply_text(f"✅ Розіслано {sent}/{len(users)}")

    elif state == "adm:promo":
        parts = text.split()
        if len(parts) == 3:
            try:
                code, disc, uses = parts[0].upper(), int(parts[1]), int(parts[2])
                create_promo(code, disc, uses)
                await update.message.reply_text(f"✅ Промокод:\n<code>{code}</code>\nЗнижка: {disc}%\nВикористань: {uses}", parse_mode="HTML")
            except:
                await update.message.reply_text("❌ Помилка. Формат: КОД ВІДСОТОК КІЛЬКІСТЬ")
        else:
            await update.message.reply_text("❌ Формат: КОД ВІДСОТОК КІЛЬКІСТЬ")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data.update({
        "cache": {}, 
        "quality": {}, 
        "top100": [], 
        "url_cache": {},
        "album_cache": {},
        "last_album_ck": ""
    })

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_admin_cb, pattern="^adm:"))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    logger.info("✅ MusicLSP запущено!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
