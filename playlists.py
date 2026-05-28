# ============================================================
#  MusicLSP — Плейлисти та Лайки (playlists.py)
# ============================================================

from bot import (
    db, get_user, get_lang, has_access, back_btn, main_kb,
    add_history, cache_url, get_cached_url, fmt_dur,
    tx, logger, InlineKeyboardButton, InlineKeyboardMarkup,
    Update, ContextTypes, asyncio, datetime, os
)

# ─── Плейлисти: БД функції ────────────────────────────────────────────────────

def create_playlist(uid, name, description=""):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    with db() as c:
        c.execute(
            "INSERT INTO playlists(user_id, name, description, created, updated) VALUES (?,?,?,?,?)",
            (uid, name, description, now, now)
        )
        return c.lastrowid

def get_playlists(uid):
    with db() as c:
        return c.execute(
            "SELECT * FROM playlists WHERE user_id=? ORDER BY updated DESC",
            (uid,)
        ).fetchall()

def get_playlist(pid):
    with db() as c:
        pl = c.execute("SELECT * FROM playlists WHERE id=?", (pid,)).fetchone()
        tracks = c.execute(
            "SELECT * FROM playlist_tracks WHERE playlist_id=? ORDER BY id",
            (pid,)
        ).fetchall()
        return pl, tracks

def add_track_to_playlist(pid, title, artist, url, duration=""):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    with db() as c:
        c.execute(
            "INSERT INTO playlist_tracks(playlist_id, title, artist, url, duration, added) VALUES (?,?,?,?,?,?)",
            (pid, title, artist, url, duration, now)
        )
        c.execute("UPDATE playlists SET updated=? WHERE id=?", (now, pid))

def delete_playlist(uid, pid):
    with db() as c:
        c.execute("DELETE FROM playlists WHERE id=? AND user_id=?", (pid, uid))

def delete_playlist_track(pid, tid):
    with db() as c:
        c.execute("DELETE FROM playlist_tracks WHERE id=? AND playlist_id=?", (tid, pid))

def rename_playlist(uid, pid, new_name):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    with db() as c:
        c.execute(
            "UPDATE playlists SET name=?, updated=? WHERE id=? AND user_id=?",
            (new_name, now, pid, uid)
        )

# ─── Лайки: БД функції ────────────────────────────────────────────────────────

def add_like(uid, title, artist, url):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    with db() as c:
        ex = c.execute(
            "SELECT id FROM likes WHERE user_id=? AND url=?",
            (uid, url)
        ).fetchone()
        if not ex:
            c.execute(
                "INSERT INTO likes(user_id, title, artist, url, added) VALUES (?,?,?,?,?)",
                (uid, title, artist, url, now)
            )
            return True
    return False

def get_likes(uid):
    with db() as c:
        return c.execute(
            "SELECT * FROM likes WHERE user_id=? ORDER BY added DESC",
            (uid,)
        ).fetchall()

def remove_like(uid, lid):
    with db() as c:
        c.execute("DELETE FROM likes WHERE id=? AND user_id=?", (lid, uid))

def is_liked(uid, url):
    with db() as c:
        return c.execute(
            "SELECT id FROM likes WHERE user_id=? AND url=?",
            (uid, url)
        ).fetchone() is not None

# ─── Хендлери: Плейлисти ───────────────────────────────────────────────────────

async def cmd_playlist(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    l = get_lang(uid)
    
    if not get_user(uid):
        await update.message.reply_text("❌ Спочатку /start")
        return
    
    playlists = get_playlists(uid)
    
    titles = {
        "uk": "📂 <b>Мої плейлисти</b>",
        "ru": "📂 <b>Мои плейлисты</b>",
        "en": "📂 <b>My Playlists</b>",
    }
    empty = {
        "uk": "Поки порожньо. Створи перший плейлист!",
        "ru": "Пока пусто. Создай первый плейлист!",
        "en": "Empty. Create your first playlist!",
    }
    create_btn = {
        "uk": "➕ Створити плейлист",
        "ru": "➕ Создать плейлист",
        "en": "➕ Create playlist",
    }
    
    kb = [[InlineKeyboardButton(create_btn.get(l, create_btn["en"]), callback_data="pl:create")]]
    
    for pl in playlists:
        name = pl["name"][:30]
        kb.append([InlineKeyboardButton(f"📁 {name}", callback_data=f"pl:open|{pl['id']}")])
    
    kb.append([back_btn(uid)])
    
    text = f"{titles.get(l, titles['en'])}\n\n"
    if not playlists:
        text += empty.get(l, empty["en"])
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def on_playlist_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    l = get_lang(uid)
    data = q.data
    
    # Створити плейлист
    if data == "pl:create":
        set_state(uid, "pl:create_name")
        prompts = {
            "uk": "📝 Введи назву плейлиста:",
            "ru": "📝 Введи название плейлиста:",
            "en": "📝 Enter playlist name:",
        }
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return
    
    # Відкрити плейлист
    if data.startswith("pl:open|"):
        pid = int(data.split("|")[1])
        pl, tracks = get_playlist(pid)
        
        if not pl:
            await q.message.reply_text("❌ Плейлист не знайдено.")
            return
        
        text = f"📁 <b>{pl['name']}</b>\n"
        if pl['description']:
            text += f"<i>{pl['description']}</i>\n"
        text += f"\n🎵 Треків: {len(tracks)}\n\n"
        
        kb = []
        
        # Кнопки треків
        for i, track in enumerate(tracks[:20]):
            text += f"{i+1}. {track['title'][:40]}\n"
            url_id = cache_url(ctx.application.bot_data, track['url'], track['title'], track['artist'])
            kb.append([
                InlineKeyboardButton(
                    f"▶️ {track['title'][:35]}",
                    callback_data=f"dlurl|{url_id}|{track['title'][:30]}|{track['artist'][:20]}"
                ),
                InlineKeyboardButton("🗑", callback_data=f"pl:deltrack|{pid}|{track['id']}")
            ])
        
        # Кнопки управління
        add_label = {"uk":"➕ Додати трек","ru":"➕ Добавить трек","en":"➕ Add track"}.get(l, "➕ Add track")
        rename_label = {"uk":"✏️ Перейменувати","ru":"✏️ Переименовать","en":"✏️ Rename"}.get(l, "✏️ Rename")
        del_label = {"uk":"🗑 Видалити","ru":"🗑 Удалить","en":"🗑 Delete"}.get(l, "🗑 Delete")
        zip_label = {"uk":"📦 ZIP","ru":"📦 ZIP","en":"📦 ZIP"}.get(l, "📦 ZIP")
        
        kb.append([
            InlineKeyboardButton(add_label, callback_data=f"pl:add|{pid}"),
            InlineKeyboardButton(zip_label, callback_data=f"pl:zip|{pid}")
        ])
        kb.append([
            InlineKeyboardButton(rename_label, callback_data=f"pl:rename|{pid}"),
            InlineKeyboardButton(del_label, callback_data=f"pl:delete|{pid}")
        ])
        kb.append([back_btn(uid)])
        
        try:
            await q.message.edit_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        except:
            await q.message.reply_text(text[:4096], reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
        return
    
    # Додати трек в плейлист
    if data.startswith("pl:add|"):
        pid = int(data.split("|")[1])
        ctx.application.bot_data["pl_add_id"] = pid
        set_state(uid, "pl:add_search")
        prompts = {
            "uk": "🔍 Введи назву треку для додавання:",
            "ru": "🔍 Введи название трека для добавления:",
            "en": "🔍 Enter song name to add:",
        }
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return
    
    # Видалити трек з плейлиста
    if data.startswith("pl:deltrack|"):
        parts = data.split("|")
        pid, tid = int(parts[1]), int(parts[2])
        delete_playlist_track(pid, tid)
        await q.answer("✅ Трек видалено!")
        # Оновлюємо відображення
        await on_playlist_cb(update, ctx)
        return
    
    # Перейменувати плейлист
    if data.startswith("pl:rename|"):
        pid = int(data.split("|")[1])
        ctx.application.bot_data["pl_rename_id"] = pid
        set_state(uid, "pl:rename")
        prompts = {
            "uk": "✏️ Введи нову назву:",
            "ru": "✏️ Введи новое название:",
            "en": "✏️ Enter new name:",
        }
        await q.message.reply_text(prompts.get(l, prompts["en"]))
        return
    
    # Видалити плейлист
    if data.startswith("pl:delete|"):
        pid = int(data.split("|")[1])
        delete_playlist(uid, pid)
        await q.answer("✅ Плейлист видалено!", show_alert=True)
        await cmd_playlist(update, ctx)
        return
    
    # ZIP плейлиста
    if data.startswith("pl:zip|"):
        if not has_access(uid):
            await q.message.reply_text(tx("no_access", l), parse_mode="HTML")
            return
        pid = int(data.split("|")[1])
        pl, tracks = get_playlist(pid)
        if not tracks:
            await q.message.reply_text("😔 Плейлист порожній.")
            return
        
        status = await q.message.reply_text(
            f"⬇️ Формую ZIP: <b>{pl['name']}</b> ({len(tracks)} треків)…",
            parse_mode="HTML"
        )
        
        # Імпортуємо create_album_zip з bot.py
        from bot import create_album_zip
        import io
        
        tracks_for_zip = [{
            "title": t["title"],
            "url": t["url"],
            "source": "youtube"
        } for t in tracks]
        
        quality = ctx.application.bot_data.get("quality", {}).get(uid, "192")
        zip_buffer = await create_album_zip(tracks_for_zip, quality)
        
        if not zip_buffer:
            await status.edit_text("❌ Помилка створення ZIP.")
            return
        
        size_mb = len(zip_buffer.getvalue()) / 1024 / 1024
        if size_mb > 2000:
            await status.edit_text(f"❌ ZIP {size_mb:.1f} МБ — завеликий.")
            return
        
        await status.edit_text("📤 Відправляю ZIP…")
        await q.message.reply_document(
            document=zip_buffer,
            filename=f"{pl['name'][:50]}.zip",
            caption=f"📁 <b>{pl['name']}</b>\n🎵 {len(tracks)} треків",
            parse_mode="HTML"
        )
        await status.delete()
        return

# ─── Хендлери: Лайки ───────────────────────────────────────────────────────────

async def cmd_likes(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    l = get_lang(uid)
    
    likes = get_likes(uid)
    
    titles = {
        "uk": "⭐ <b>Улюблені треки</b>",
        "ru": "⭐ <b>Избранные треки</b>",
        "en": "⭐ <b>Liked Songs</b>",
    }
    empty = {
        "uk": "Поки порожньо. Став ❤️ на треках!",
        "ru": "Пока пусто. Ставь ❤️ на треках!",
        "en": "Empty. Like some tracks!",
    }
    
    kb = []
    for like in likes[:40]:
        url_id = cache_url(ctx.application.bot_data, like['url'], like['title'], like['artist'])
        kb.append([
            InlineKeyboardButton(
                f"❤️ {like['title'][:35]}",
                callback_data=f"dlurl|{url_id}|{like['title'][:30]}|{like['artist'][:20]}"
            ),
            InlineKeyboardButton("💔", callback_data=f"like:remove|{like['id']}")
        ])
    
    kb.append([back_btn(uid)])
    
    text = titles.get(l, titles["en"])
    if not likes:
        text += f"\n\n{empty.get(l, empty['en'])}"
    else:
        text += f"\n\nВсього: {len(likes)}"
    
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def on_like_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data
    
    if data.startswith("like:remove|"):
        lid = int(data.split("|")[1])
        remove_like(uid, lid)
        await q.answer("💔 Видалено з улюблених", show_alert=True)
        await cmd_likes(update, ctx)
        return
    
    # Додати в лайки (викликається з кнопки при завантаженні треку)
    if data.startswith("like:add|"):
        url_id = data.split("|")[1]
        cached = get_cached_url(ctx.application.bot_data, url_id)
        url = cached.get("url", "")
        title = cached.get("title", "Unknown")
        artist = cached.get("artist", "")
        
        if not url:
            await q.answer("❌ Помилка", show_alert=True)
            return
        
        added = add_like(uid, title, artist, url)
        if added:
            await q.answer("❤️ Додано в улюблені!", show_alert=True)
        else:
            await q.answer("ℹ️ Вже в улюблених", show_alert=True)
        return

# ─── Допоміжні функції для плейлистів ─────────────────────────────────────────

def set_state(uid, state):
    with db() as c:
        c.execute("UPDATE users SET state=? WHERE id=?", (state, uid))

# ─── Обробка текстових повідомлень для плейлистів ─────────────────────────────

async def handle_playlist_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE, state, text):
    uid = update.effective_user.id
    l = get_lang(uid)
    
    # Створення плейлиста — назва
    if state == "pl:create_name":
        set_state(uid, "pl:create_desc")
        ctx.application.bot_data[f"pl_name_{uid}"] = text
        prompts = {
            "uk": "📝 Введи опис (або напиши 'пропустити'):",
            "ru": "📝 Введи описание (или напиши 'пропустить'):",
            "en": "📝 Enter description (or type 'skip'):",
        }
        await update.message.reply_text(prompts.get(l, prompts["en"]))
        return True
    
    # Створення плейлиста — опис
    if state == "pl:create_desc":
        name = ctx.application.bot_data.get(f"pl_name_{uid}", "New Playlist")
        desc = text if text.lower() not in ["пропустити", "пропустить", "skip", ""] else ""
        pid = create_playlist(uid, name, desc)
        set_state(uid, "")
        msgs = {
            "uk": f"✅ Плейлист <b>{name}</b> створено!\n\nВідкрий: /playlist",
            "ru": f"✅ Плейлист <b>{name}</b> создан!\n\nОткрой: /playlist",
            "en": f"✅ Playlist <b>{name}</b> created!\n\nOpen: /playlist",
        }
        await update.message.reply_text(msgs.get(l, msgs["en"]), parse_mode="HTML")
        return True
    
    # Додавання треку в плейлист
    if state == "pl:add_search":
        pid = ctx.application.bot_data.get("pl_add_id")
        if not pid:
            set_state(uid, "")
            return False
        
        # Шукаємо трек
        from bot import async_search
        results = await async_search(text, limit=5)
        
        if not results:
            await update.message.reply_text("😔 Нічого не знайдено.")
            return True
        
        kb = []
        for i, track in enumerate(results):
            kb.append([
                InlineKeyboardButton(
                    f"🎵 {track['title'][:40]} ({track['duration']})",
                    callback_data=f"pl:addconfirm|{pid}|{i}"
                )
            ])
        kb.append([InlineKeyboardButton("◀️ Назад", callback_data="m:home")])
        
        # Кешуємо результати
        ck = f"pl_add_{uid}_{pid}"
        ctx.application.bot_data.setdefault("cache", {})[ck] = results
        
        await update.message.reply_text(
            "🎵 <b>Обери трек для додавання:</b>",
            reply_markup=InlineKeyboardMarkup(kb),
            parse_mode="HTML"
        )
        return True
    
    # Перейменування плейлиста
    if state == "pl:rename":
        pid = ctx.application.bot_data.get("pl_rename_id")
        if pid:
            rename_playlist(uid, pid, text)
            set_state(uid, "")
            await update.message.reply_text(f"✅ Перейменовано на <b>{text}</b>!", parse_mode="HTML")
            return True
    
    return False

# ─── Реєстрація хендлерів ─────────────────────────────────────────────────────

def register_playlist_handlers(app):
    """Додає всі хендлери плейлистів та лайків до бота."""
    
    # Команди
    app.add_handler(CommandHandler("playlist", cmd_playlist))
    app.add_handler(CommandHandler("playlists", cmd_playlist))
    app.add_handler(CommandHandler("likes", cmd_likes))
    app.add_handler(CommandHandler("liked", cmd_likes))
    
    # Callbacks
    app.add_handler(CallbackQueryHandler(on_playlist_cb, pattern="^pl:"))
    app.add_handler(CallbackQueryHandler(on_like_cb, pattern="^like:"))
