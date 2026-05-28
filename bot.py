# ============================================================
#  MusicLSP — Частина 1 (ФІКС обкладинки MusicBrainz)
# ============================================================

# ... (попередній код без змін) ...

def mb_get_full_album_info(mbid):
    """
    Отримує повну інформацію про альбом з MusicBrainz за MBID.
    """
    url = f"https://musicbrainz.org/ws/2/release/{mbid}?inc=recordings+artists+labels&fmt=json"
    headers = {"User-Agent": f"MusicLSP/1.0 ({AUTHOR})"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.error(f"MusicBrainz release error: {e}")
        return None
    
    # ← ВИПРАВЛЕНО: Краща обробка обкладинок
    release_group_id = data.get("release-group", {}).get("id", "")
    image_url = ""
    
    # Спосіб 1: CoverArtArchive через release-group
    if release_group_id:
        try:
            cover_url = f"https://coverartarchive.org/release-group/{release_group_id}"
            logger.info(f"Trying CAA release-group: {cover_url}")
            cover_resp = requests.get(cover_url, headers=headers, timeout=10)
            if cover_resp.status_code == 200:
                cover_data = cover_resp.json()
                images = cover_data.get("images", [])
                if images:
                    # Беремо найбільшу обкладинку
                    image_url = images[0].get("thumbnails", {}).get("large", 
                              images[0].get("thumbnails", {}).get("small",
                              images[0].get("image", "")))
                    logger.info(f"Found CAA image (release-group): {image_url[:100]}")
            else:
                logger.warning(f"CAA release-group status: {cover_resp.status_code}")
        except Exception as e:
            logger.warning(f"CoverArt release-group error: {e}")
    
    # Спосіб 2: CoverArtArchive через release (якщо release-group не спрацював)
    if not image_url:
        try:
            cover_url = f"https://coverartarchive.org/release/{mbid}"
            logger.info(f"Trying CAA release: {cover_url}")
            cover_resp = requests.get(cover_url, headers=headers, timeout=10)
            if cover_resp.status_code == 200:
                cover_data = cover_resp.json()
                images = cover_data.get("images", [])
                if images:
                    image_url = images[0].get("thumbnails", {}).get("large",
                              images[0].get("thumbnails", {}).get("small",
                              images[0].get("image", "")))
                    logger.info(f"Found CAA image (release): {image_url[:100]}")
            else:
                logger.warning(f"CAA release status: {cover_resp.status_code}")
        except Exception as e:
            logger.warning(f"CoverArt release error: {e}")
    
    # Спосіб 3: MusicBrainz relationships (якщо є URL обкладинки)
    if not image_url:
        try:
            rel_url = f"https://musicbrainz.org/ws/2/release/{mbid}?inc=url-rels&fmt=json"
            rel_resp = requests.get(rel_url, headers=headers, timeout=10)
            if rel_resp.status_code == 200:
                rel_data = rel_resp.json()
                relations = rel_data.get("relations", [])
                for rel in relations:
                    if rel.get("type") == "cover art link":
                        image_url = rel.get("url", {}).get("resource", "")
                        if image_url:
                            logger.info(f"Found MB relation image: {image_url[:100]}")
                            break
        except Exception as e:
            logger.warning(f"MB relations error: {e}")
    
    # Спосіб 4: Discogs fallback (через пошук)
    if not image_url:
        try:
            discogs_url = f"https://api.discogs.com/database/search?release_title={requests.utils.quote(data.get('title', ''))}&artist={requests.utils.quote(data.get('artist-credit', [{}])[0].get('name', ''))}&type=release&key=YOUR_KEY&secret=YOUR_SECRET"
            # Примітка: для Discogs потрібен API ключ, тому це опціонально
            logger.info("Trying Discogs fallback...")
        except:
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
                "artists": ", ".join(a.get("name", "") for a in recording.get("artist-credit", [])),
                "duration_ms": dur_ms,
                "duration": fmt_dur_ms(dur_ms),
                "track_number": track.get("number", 0),
            })
    
    label = "—"
    labels = data.get("label-info", [])
    if labels:
        label = labels[0].get("label", {}).get("name", "—")
    
    artists = ", ".join(a.get("name", "") for a in data.get("artist-credit", []))
    release_date = data.get("date", "—")
    year = release_date[:4] if release_date and len(release_date) >= 4 else "—"
    
    result = {
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
        "image_url": image_url,  # ← тепер точно буде, якщо знайдено
        "image_urls": [image_url] if image_url else [],  # ← додано для fallback
        "album_type": data.get("release-group", {}).get("primary-type", "album"),
        "external_url": f"https://musicbrainz.org/release/{mbid}",
    }
    
    logger.info(f"MB Result image_url: {image_url[:100] if image_url else 'EMPTY'}")
    return result

# ... (решта Частини 1 без змін) ...
# ============================================================
#  MusicLSP — Частина 2: Завантаження, асинхронні обгортки, клавіатури
# ============================================================

import zipfile
import io

COOKIES_PATH = "youtube_cookies.txt"  # ← ВИПРАВЛЕНО! Було "cookies.txt"

def get_yt_opts(extra=None):
    opts = {
        "quiet": True, "no_warnings": True, "extract_flat": True, "noplaylist": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.youtube.com/",
        "headers": {"Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"},
        "extractor_args": {"youtube": {"player_client": ["web"], "player_skip": ["webpage", "configs", "js"]}},
        "cookies": COOKIES_PATH,  # ← тепер "youtube_cookies.txt"
    }
    if extra: opts.update(extra)
    return opts

def download_mp3(url, out_dir, quality="192"):
    base_opts = {
        "format": "bestaudio/best",
        "outtmpl": os.path.join(out_dir, "%(title)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": quality}],
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "referer": "https://www.youtube.com/",
        "headers": {"Accept-Language": "en-US,en;q=0.9", "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"},
        "cookies": COOKIES_PATH,  # ← тепер "youtube_cookies.txt"
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
    logger.error(f"All download attempts failed for {url}")
    return None

async def async_search(query, limit=10):
    return await asyncio.get_event_loop().run_in_executor(None, search_all, query, limit)

async def async_artist(artist, limit=50):
    return await asyncio.get_event_loop().run_in_executor(None, artist_songs, artist, limit)

async def async_top100():
    return await asyncio.get_event_loop().run_in_executor(None, get_top100)

async def async_download(url, out_dir, quality="192"):
    return await asyncio.get_event_loop().run_in_executor(None, download_mp3, url, out_dir, quality)

async def async_spotify_album_info(album_id):
    return await asyncio.get_event_loop().run_in_executor(None, get_spotify_album_info, album_id)

async def async_search_spotify(query, limit=10):
    return await asyncio.get_event_loop().run_in_executor(None, search_spotify_and_format, query, limit)

async def async_mb_search(query, limit=10):
    return await asyncio.get_event_loop().run_in_executor(None, mb_search_album, query, limit)

async def async_mb_full_info(mbid):
    return await asyncio.get_event_loop().run_in_executor(None, mb_get_full_album_info, mbid)

async def async_find_track(track_name, artist_name):
    return await asyncio.get_event_loop().run_in_executor(None, find_track_for_download, track_name, artist_name)

# ─── ZIP-архів для альбому ────────────────────────────────────────────────────
async def create_album_zip(tracks, quality="192"):
    zip_buffer = io.BytesIO()
    downloaded = 0
    with tempfile.TemporaryDirectory() as tmp:
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, track in enumerate(tracks):
                if not track.get("url"):
                    continue
                try:
                    path = await async_download(track["url"], tmp, quality)
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
        "uk": ["🔍 Пошук", "💿 Альбоми", "📚 Бібліотека", "👤 Профіль", "💎 Підписка", "🎁 Реферал", "⚙️ Налаштування"],
        "ru": ["🔍 Поиск", "💿 Альбомы", "📚 Библиотека", "👤 Профиль", "💎 Подписка", "🎁 Реферал", "⚙️ Настройки"],
        "en": ["🔍 Search", "💿 Albums", "📚 Library", "👤 Profile", "💎 Subscription", "🎁 Referral", "⚙️ Settings"],
    }
    lb = labels.get(l, labels["en"])
    back_label = {"uk":"◀️ Назад","ru":"◀️ Назад","en":"◀️ Back"}.get(l,"◀️ Back")
    return InlineKeyboardMarkup([
        [btn(lb[0], "m:search"),  btn(lb[1], "m:albums")],
        [btn(lb[2], "m:library"), btn(lb[3], "m:profile")],
        [btn(lb[4], "m:sub"),    btn(lb[5], "m:ref")],
        [btn(lb[6], "m:settings")],
    ]), back_label

def back_btn(uid):
    l = get_lang(uid)
    labels = {"uk":"◀️ Назад","ru":"◀️ Назад","en":"◀️ Back"}
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
    await update.message.reply_text("🌍 <b>Choose your language / Оберіть мову</b>", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="HTML")

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

    if data.startswith("lang:"):
        set_lang(uid, data[5:])
        await q.message.delete()
        await show_welcome(q.message, uid)
        return

    if data == "m:home":
        kb, _ = main_kb(uid)
        try:
            await q.message.edit_text(f"🏠 <b>{BOT_NAME}</b>\n\nОбери дію 👇", reply_markup=kb, parse_mode="HTML")
        except:
            await q.message.reply_text(f"🏠 <b>{BOT_NAME}</b>\n\nОбери дію 👇", reply_markup=kb, parse_mode="HTML")
        return

    if data == "m:search":
        set_state(uid, "searching")
        prompts = {"uk":"🔍 Введи назву пісні або артиста:","ru":"🔍 Введи название песни или артиста:","en":"🔍 Enter song name or artist:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    if data == "m:albums":
        set_state(uid, "album_search")
        prompts = {
            "uk": "💿 Введи назву альбому:\n\n<i>Приклади:</i>\n• <code>Yanix SS 20</code>\n• <code>The Weeknd After Hours</code>\n• <code>Баста Гуф 2010</code>",
            "ru": "💿 Введи название альбома:\n\n<i>Примеры:</i>\n• <code>Yanix SS 20</code>",
            "en": "💿 Enter album name:\n\n<i>Examples:</i>\n• <code>Yanix SS 20</code>",
        }
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    if data == "m:library":
        await show_library(q.message, uid, ctx)
        return

    if data == "m:profile":
        await show_profile(q.message, uid)
        return

    if data == "m:sub":
        await show_sub(q.message, uid)
        return

    if data == "m:ref":
        await show_ref(q.message, uid, ctx)
        return

    if data == "m:settings":
        await show_settings(q.message, uid, ctx)
        return

    if data == "sub:key":
        set_state(uid, "enter_key")
        prompts = {"uk":"🔑 Введи ключ активації:","ru":"🔑 Введи ключ активации:","en":"🔑 Enter activation key:"}
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return

    if data == "sub:promo":
        set_state(uid, "enter_promo")
        await q.message.reply_text("🎟 Введи промокод:")
        return

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

    # Spotify альбом
    if data.startswith("sp_album|"):
        album_id = data.split("|", 1)[1]
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        await show_spotify_album(q.message, album_id, uid, ctx)
        return

    # Завантажити трек з альбому
    if data.startswith("sp_track|"):
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        parts = data.split("|", 2)
        album_ck = parts[1]
        track_idx = int(parts[2])
        album_data = ctx.application.bot_data.get("spotify_album_cache", {}).get(album_ck)
        if not album_data or track_idx >= len(album_data.get("tracks", [])):
            await q.message.reply_text("❌ Дані альбому застаріли. Шукай знову.")
            return
        track = album_data["tracks"][track_idx]
        status = await q.message.reply_text(f"🔍 Шукаю: <b>{track['name']}</b>…", parse_mode="HTML")
        result = await async_find_track(track["name"], track["artists"])
        if not result:
            await status.edit_text(f"😔 Не знайдено: <b>{track['name']}</b>", parse_mode="HTML")
            return
        await status.delete()
        await do_download(q.message, result["url"], track["name"], track["artists"], uid, ctx)
        return

    # ZIP альбому
    if data == "sp_albumzip":
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        album_ck = ctx.application.bot_data.get("last_spotify_album_ck", "")
        album_data = ctx.application.bot_data.get("spotify_album_cache", {}).get(album_ck)
        if not album_data:
            await q.message.reply_text("❌ Дані альбому застаріли.")
            return
        await do_download_spotify_album_zip(q.message, album_data, uid, ctx)
        return

    # Додати альбом в бібліотеку
    if data.startswith("sp_addlib|"):
        parts = data.split("|", 2)
        album_ck = parts[1]
        album_data = ctx.application.bot_data.get("spotify_album_cache", {}).get(album_ck)
        if album_data:
            first_track = album_data["tracks"][0] if album_data["tracks"] else {}
            result = await async_find_track(first_track.get("name", ""), album_data["artist"])
            url = result["url"] if result else album_data["external_url"]
            added = add_library(uid, album_data["name"], album_data["artist"], url, kind="album")
            await q.answer("✅ Альбом додано до бібліотеки!" if added else "ℹ️ Вже є в бібліотеці.", show_alert=True)
        return

    if data == "artist_input":
        set_state(uid, "artist_input")
        prompts = {"uk":"🎤 Введи ім'я артиста:","ru":"🎤 Введи имя артиста:","en":"🎤 Enter artist name:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

    if data == "dl20_input":
        set_state(uid, "dl20_input")
        prompts = {"uk":"⬇️ Введи ім'я артиста для завантаження 20 пісень:","ru":"⬇️ Введи имя артиста для скачивания 20 песен:","en":"⬇️ Enter artist name to download 20 songs:"}
        await q.message.edit_text(prompts.get(l, prompts["en"]), reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return

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
        ctx.application.bot_data.setdefault("quality", {})[uid] = q_val
        await q.answer(f"✅ Якість: {q_val} kbps", show_alert=True)
        return

    # MusicBrainz альбом
    if data.startswith("mb_album|"):
        mbid = data.split("|", 1)[1]
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        await show_mb_album(q.message, mbid, uid, ctx)
        return

    # Завантажити трек з MB альбому
    if data.startswith("mb_track|"):
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        parts = data.split("|", 2)
        album_ck = parts[1]
        track_idx = int(parts[2])
        album_data = ctx.application.bot_data.get("mb_album_cache", {}).get(album_ck)
        if not album_data or track_idx >= len(album_data.get("tracks", [])):
            await q.message.reply_text("❌ Дані альбому застаріли. Шукай знову.")
            return
        track = album_data["tracks"][track_idx]
        status = await q.message.reply_text(f"🔍 Шукаю: <b>{track['name']}</b>…", parse_mode="HTML")
        result = await async_find_track(track["name"], track["artists"])
        if not result:
            await status.edit_text(f"😔 Не знайдено: <b>{track['name']}</b>", parse_mode="HTML")
            return
        await status.delete()
        await do_download(q.message, result["url"], track["name"], track["artists"], uid, ctx)
        return

    # ZIP MB альбому
    if data == "mb_albumzip":
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        album_ck = ctx.application.bot_data.get("last_mb_album_ck", "")
        album_data = ctx.application.bot_data.get("mb_album_cache", {}).get(album_ck)
        if not album_data:
            await q.message.reply_text("❌ Дані альбому застаріли.")
            return
        await do_download_mb_album_zip(q.message, album_data, uid, ctx)
        return

    # Додати MB альбом в бібліотеку
    if data.startswith("mb_addlib|"):
        parts = data.split("|", 2)
        album_ck = parts[1]
        album_data = ctx.application.bot_data.get("mb_album_cache", {}).get(album_ck)
        if album_data:
            first_track = album_data["tracks"][0] if album_data["tracks"] else {}
            result = await async_find_track(first_track.get("name", ""), album_data["artist"])
            url = result["url"] if result else album_data["external_url"]
            added = add_library(uid, album_data["name"], album_data["artist"], url, kind="album")
            await q.answer("✅ Альбом додано до бібліотеки!" if added else "ℹ️ Вже є в бібліотеці.", show_alert=True)
        return
# ============================================================
#  MusicLSP — Частина 3: Повідомлення, MusicBrainz альбоми, адмін, main
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

    # Пошук альбому — MusicBrainz
    if state == "album_search":
        set_state(uid, "")
        if not has_access(uid):
            await update.message.reply_text(tx("no_access", l), parse_mode="HTML"); return
        await do_mb_album_search(update, text, uid, ctx)
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

# ─── MusicBrainz: Пошук альбомів ──────────────────────────────────────────────
async def do_mb_album_search(update, query, uid, ctx):
    msg = await update.message.reply_text(f"💿 Шукаю в MusicBrainz: <b>{query}</b>…", parse_mode="HTML")
    releases = await async_mb_search(query, limit=10)
    if not releases:
        await msg.edit_text(
            "😔 Альбоми не знайдено.\n\n"
            "💡 Спробуй:\n"
            "• Точнішу назву: <code>Yanix SS 20</code>\n"
            "• Формат: <code>Артист НазваАльбому</code>",
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
async def show_mb_album(msg, mbid, uid, ctx):
    status = await msg.reply_text("💿 Завантажую інформацію…", parse_mode="HTML")
    album = await async_mb_full_info(mbid)
    if not album:
        text = "❌ Не вдалося отримати дані. Спробуй інший альбом."
        await status.edit_text(text, reply_markup=InlineKeyboardMarkup([[back_btn(uid)]]), parse_mode="HTML")
        return
    
    # Логуємо image_url для діагностики
    logger.info(f"MB Album image_url: {album.get('image_url', 'NOT FOUND')}")
    
    import hashlib
    ck = hashlib.md5(f"{uid}_{mbid}".encode()).hexdigest()[:8]
    ctx.application.bot_data.setdefault("mb_album_cache", {})[ck] = album
    ctx.application.bot_data["last_mb_album_ck"] = ck
    
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
    add_label = {"uk":"📚 Додати в бібліотеку","ru":"📚 Добавить в библиотеку","en":"📚 Add to Library"}.get(l, "📚 Add to Library")
    kb.append([
        InlineKeyboardButton(zip_label, callback_data="mb_albumzip"),
        InlineKeyboardButton(add_label, callback_data=f"mb_addlib|{ck}")
    ])
    kb.append([back_btn(uid)])
    
    try:
        await status.delete()
    except:
        pass
    
    # Відправляємо з обкладинкою
    image_url = album.get("image_url", "")
    if image_url:
        try:
            logger.info(f"Sending MB photo with URL: {image_url[:100]}")
            await msg.reply_photo(
                photo=image_url,
                caption=text[:1024],
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.error(f"MB Photo send failed: {e}")
            # Спробуємо відправити як document (іноді працює краще)
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
    
    # Якщо немає фото або не відправилось — текст
    await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── MusicBrainz: Завантажити ZIP ─────────────────────────────────────────────
async def do_download_mb_album_zip(msg, album_data, uid, ctx):
    l = get_lang(uid)
    status = await msg.reply_text(
        f"⬇️ Завантажую альбом: <b>{album_data['name']}</b>\n"
        f"<i>Шукаю треки: SoundCloud → Spotify → YouTube → YT Music…</i>",
        parse_mode="HTML"
    )
    quality = ctx.application.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
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
    zip_buffer = await create_album_zip(tracks_with_url, quality)
    if not zip_buffer:
        await status.edit_text("❌ Помилка створення архіву.")
        return
    size_mb = len(zip_buffer.getvalue()) / 1024 / 1024
    if size_mb > 2000:
        await status.edit_text(f"❌ Архів {size_mb:.1f} МБ — завеликий.")
        return
    await status.edit_text("📤 Відправляю ZIP…")
    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]
    
    # ZIP з обкладинкою як thumbnail
    thumb = album_data.get("image_url", "")
    if thumb:
        try:
            thumb_resp = requests.get(thumb, timeout=10)
            if thumb_resp.status_code == 200:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_thumb:
                    tmp_thumb.write(thumb_resp.content)
                    tmp_thumb.flush()
                    await msg.reply_document(
                        document=zip_buffer,
                        thumbnail=tmp_thumb.name,
                        filename=f"{safe_name}.zip",
                        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків\n📍 Джерела: {sources}",
                        parse_mode="HTML"
                    )
                    os.unlink(tmp_thumb.name)
                    await status.delete()
                    return
        except Exception as e:
            logger.warning(f"MB ZIP thumbnail failed: {e}")
    
    await msg.reply_document(
        document=zip_buffer,
        filename=f"{safe_name}.zip",
        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків\n📍 Джерела: {sources}",
        parse_mode="HTML"
    )
    await status.delete()

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
        logger.warning(f"Library edit_text failed: {e}")
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
                await status.edit_text(
                    "❌ YouTube заблокував завантаження.\n\n"
                    "💡 Спробуй:\n"
                    "1. Оновити yt-dlp: <code>pip install -U yt-dlp</code>\n"
                    "2. Спробувати іншу пісню\n"
                    "3. Перевірити через годину",
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
            kb = [[InlineKeyboardButton(add_labels.get(l, add_labels["en"]), callback_data=f"addlib|{url_id}")]]
            with open(path, "rb") as f:
                await msg.reply_audio(
                    audio=f, title=title[:64], performer=artist[:64] or None,
                    filename=f"{title[:50]}.mp3", reply_markup=InlineKeyboardMarkup(kb)
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
                await status.edit_text("❌ YouTube вимагає авторизацію.\n\n💡 Спробуй іншу пісню або онови yt-dlp.", parse_mode="HTML")
            else:
                logger.error(f"DownloadError: {e}")
                await status.edit_text("❌ Помилка завантаження. Спробуй іншу пісню.")
        except Exception as e:
            logger.error(f"Download unexpected error: {e}")
            await status.edit_text("❌ Помилка завантаження. Спробуй іншу пісню.")

# ─── Spotify: Показати альбом ───────────────────────────────────────────────
async def show_spotify_album(msg, album_id, uid, ctx):
    """Показує інформацію про Spotify альбом з обкладинкою."""
    l = get_lang(uid)
    status = await msg.reply_text("💿 Завантажую інформацію…", parse_mode="HTML")
    
    album = await async_spotify_album_info(album_id)
    if not album:
        await status.edit_text("❌ Не вдалося отримати дані. Спробуй інший альбом.")
        return
    
    # Логуємо image_url для діагностики
    logger.info(f"Spotify Album image_url: {album.get('image_url', 'NOT FOUND')}")
    logger.info(f"Spotify Album image_urls: {album.get('image_urls', [])}")
    
    # Кешуємо альбом
    import hashlib
    ck = hashlib.md5(f"{uid}_{album_id}".encode()).hexdigest()[:8]
    ctx.application.bot_data.setdefault("spotify_album_cache", {})[ck] = album
    ctx.application.bot_data["last_spotify_album_ck"] = ck
    
    text = (
        f"📀 <b>{album['name']}</b>\n\n"
        f"🎤 <b>Виконавець:</b> {album['artist']}\n"
        f"📅 <b>Рік:</b> {album['year']}\n"
        f"🏷 <b>Лейбл:</b> {album['label']}\n"
        f"🎵 <b>Треків:</b> {album['total_tracks']}\n"
        f"⏱ <b>Тривалість:</b> {album['total_duration']}\n"
        f"🔥 <b>Популярність:</b> {album['popularity']}/100\n\n"
        f"🎧 <b>Треки:</b>\n"
    )
    
    kb = []
    for i, track in enumerate(album["tracks"]):
        text += f"{i+1}. {track['name']} — {track['duration']}\n"
        kb.append([
            InlineKeyboardButton(
                f"▶️ {i+1}. {track['name'][:35]} ({track['duration']})",
                callback_data=f"sp_track|{ck}|{i}"
            )
        ])
    
    zip_label = {"uk":"📦 Завантажити ZIP","ru":"📦 Скачать ZIP","en":"📦 Download ZIP"}.get(l, "📦 Download ZIP")
    add_label = {"uk":"📚 Додати в бібліотеку","ru":"📚 Добавить в библиотеку","en":"📚 Add to Library"}.get(l, "📚 Add to Library")
    kb.append([
        InlineKeyboardButton(zip_label, callback_data="sp_albumzip"),
        InlineKeyboardButton(add_label, callback_data=f"sp_addlib|{ck}")
    ])
    kb.append([back_btn(uid)])
    
    try:
        await status.delete()
    except:
        pass
    
    # Відправляємо з обкладинкою
    image_url = album.get("image_url", "")
    if not image_url and album.get("image_urls"):
        image_url = album["image_urls"][0]  # Беремо першу доступну
    
    if image_url:
        try:
            logger.info(f"Sending Spotify photo with URL: {image_url[:100]}")
            await msg.reply_photo(
                photo=image_url,
                caption=text[:1024],
                reply_markup=InlineKeyboardMarkup(kb),
                parse_mode="HTML"
            )
            return
        except Exception as e:
            logger.error(f"Spotify Photo send failed: {e}")
            # Спробуємо відправити як document
            try:
                await msg.reply_document(
                    document=image_url,
                    caption=text[:1024],
                    reply_markup=InlineKeyboardMarkup(kb),
                    parse_mode="HTML"
                )
                return
            except Exception as e2:
                logger.error(f"Spotify Document send also failed: {e2}")
    
    # Якщо немає фото або не відправилось — текст
    await msg.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ─── Spotify: Завантажити ZIP ───────────────────────────────────────────────
async def do_download_spotify_album_zip(msg, album_data, uid, ctx):
    l = get_lang(uid)
    status = await msg.reply_text(
        f"⬇️ Завантажую альбом: <b>{album_data['name']}</b>\n"
        f"<i>Шукаю треки: SoundCloud → Spotify → YouTube → YT Music…</i>",
        parse_mode="HTML"
    )
    quality = ctx.application.bot_data.get("quality", {}).get(uid, DEF_QUALITY)
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
    zip_buffer = await create_album_zip(tracks_with_url, quality)
    if not zip_buffer:
        await status.edit_text("❌ Помилка створення архіву.")
        return
    size_mb = len(zip_buffer.getvalue()) / 1024 / 1024
    if size_mb > 2000:
        await status.edit_text(f"❌ Архів {size_mb:.1f} МБ — завеликий.")
        return
    await status.edit_text("📤 Відправляю ZIP…")
    safe_name = f"{album_data['artist']} - {album_data['name']}"[:50]
    
    # ZIP з обкладинкою як thumbnail
    thumb = album_data.get("image_url", "")
    if not thumb and album_data.get("image_urls"):
        thumb = album_data["image_urls"][0]
    
    if thumb:
        try:
            thumb_resp = requests.get(thumb, timeout=10)
            if thumb_resp.status_code == 200:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp_thumb:
                    tmp_thumb.write(thumb_resp.content)
                    tmp_thumb.flush()
                    await msg.reply_document(
                        document=zip_buffer,
                        thumbnail=tmp_thumb.name,
                        filename=f"{safe_name}.zip",
                        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків\n📍 Джерела: {sources}",
                        parse_mode="HTML"
                    )
                    os.unlink(tmp_thumb.name)
                    await status.delete()
                    return
        except Exception as e:
            logger.warning(f"Spotify ZIP thumbnail failed: {e}")
    
    # Без обкладинки
    await msg.reply_document(
        document=zip_buffer,
        filename=f"{safe_name}.zip",
        caption=f"💿 <b>{album_data['name']}</b>\n🎤 {album_data['artist']}\n📦 {len(tracks_with_url)} треків\n📍 Джерела: {sources}",
        parse_mode="HTML"
    )
    await status.delete()

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
                    await ctx.bot.send_message(target_id, f"🎁 Адмін надав тобі доступ на <b>{days} днів</b>!\n\nПриємного прослуховування 🎵", parse_mode="HTML")
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
        "cache": {}, "quality": {}, "top100": [], "url_cache": {},
        "spotify_album_cache": {}, "last_spotify_album_ck": "",
        "mb_album_cache": {}, "last_mb_album_ck": ""
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
