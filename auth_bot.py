# ============================================================
#  MusicLSP — Бот авторизації/оплати
#  Автор: Lesiv
# ============================================================

import os, logging, secrets, string, sqlite3, datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

AUTH_BOT_TOKEN = os.environ.get("AUTH_BOT_TOKEN", "")
ADMIN_ID       = 1293055247
DB_PATH        = "musiclsp.db"   # та сама база що й основний бот
AUTHOR         = "Lesiv"

# ── ВАЖЛИВО: Вкажи свої реквізити нижче ──────────────────────────────────────
PAYMENT_DETAILS = """
💳 Карта (Monobank): <code>4441 1111 2222 3333</code>
або
₿ USDT (TRC20): <code>TВашАдресаТутПишіть</code>
"""
# ─────────────────────────────────────────────────────────────────────────────

PLANS = {
    "week":  {"days": 7,  "price": 0.50, "label": "7 днів — $0.50"},
    "month": {"days": 30, "price": 2.00, "label": "30 днів — $2.00"},
}

def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def gen_key():
    return "LSP-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(12))

def add_key(key, days, plan):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO keys(key,days,plan) VALUES(?,?,?)", (key, days, plan))

def extend_sub(uid, days):
    now = datetime.datetime.utcnow()
    with db() as c:
        u = c.execute("SELECT sub_exp FROM users WHERE id=?", (uid,)).fetchone()
        base = now
        if u and u["sub_exp"]:
            base = max(datetime.datetime.fromisoformat(u["sub_exp"]), now)
        new_exp = (base + datetime.timedelta(days=days)).isoformat()
        c.execute("UPDATE users SET sub_exp=? WHERE id=?", (new_exp, uid))

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kb = [
        [InlineKeyboardButton("💎 7 днів — $0.50",  callback_data="buy:week")],
        [InlineKeyboardButton("💎 30 днів — $2.00", callback_data="buy:month")],
    ]
    await update.message.reply_text(
        f"💳 <b>MusicLSP — Оплата</b>\n\n"
        f"Обери тариф і отримай ключ для @MusicLSP_bot\n\n"
        f"• 7 днів — <b>$0.50</b>\n"
        f"• 30 днів — <b>$2.00</b>\n\n"
        f"👤 Автор: {AUTHOR}",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
    )

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data.startswith("buy:"):
        plan = data[4:]
        info = PLANS.get(plan, {})
        kb = [
            [InlineKeyboardButton("✅ Я оплатив", callback_data=f"paid:{plan}:{uid}")],
            [InlineKeyboardButton("◀️ Назад", callback_data="back")],
        ]
        await q.message.edit_text(
            f"💳 <b>{info.get('label','')}</b>\n\n"
            f"1️⃣ Відправ <b>${info.get('price','?')}</b> на:\n{PAYMENT_DETAILS}\n"
            f"2️⃣ В коментарі вкажи ID: <code>{uid}</code>\n\n"
            f"3️⃣ Натисни «Я оплатив» — адмін перевірить і вишле ключ\n"
            f"⚡️ До 15 хвилин",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
        )

    elif data.startswith("paid:"):
        _, plan, payer_id = data.split(":")
        info = PLANS.get(plan, {})
        try:
            user = await q.get_bot().get_chat(int(payer_id))
            uname = f"@{user.username}" if user.username else str(payer_id)
        except: uname = str(payer_id)

        kb = [
            [InlineKeyboardButton("✅ Підтвердити і надіслати ключ", callback_data=f"confirm:{plan}:{payer_id}")],
            [InlineKeyboardButton("❌ Відхилити", callback_data=f"reject:{payer_id}")],
        ]
        await q.get_bot().send_message(
            ADMIN_ID,
            f"💰 <b>Новий платіж!</b>\n\n"
            f"👤 {uname} (<code>{payer_id}</code>)\n"
            f"💎 {info.get('label','')}\n"
            f"💵 ${info.get('price','?')}",
            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML"
        )
        await q.message.edit_text(
            "⏳ <b>Запит відправлено!</b>\n\nАдмін перевірить і надішле ключ.\nЗазвичай до 15 хвилин ⚡️",
            parse_mode="HTML"
        )

    elif data.startswith("confirm:"):
        _, plan, payer_id = data.split(":")
        info = PLANS.get(plan, {})
        days = info.get("days", 7)
        key = gen_key()
        add_key(key, days, plan)
        try:
            await q.get_bot().send_message(
                int(payer_id),
                f"🎉 <b>Оплату підтверджено!</b>\n\n"
                f"🔑 Твій ключ:\n<code>{key}</code>\n\n"
                f"1. Перейди в @MusicLSP_bot\n"
                f"2. Натисни 💎 Підписка → 🔑 Ввести ключ\n"
                f"3. Встав ключ\n\n"
                f"Приємного прослуховування! 🎵",
                parse_mode="HTML"
            )
            await q.message.edit_text(f"✅ Ключ надіслано юзеру {payer_id}:\n<code>{key}</code>", parse_mode="HTML")
        except Exception as e:
            await q.message.edit_text(f"❌ Помилка відправки юзеру.\nКлюч: <code>{key}</code>", parse_mode="HTML")

    elif data.startswith("reject:"):
        payer_id = data[7:]
        try:
            await q.get_bot().send_message(int(payer_id), "❌ Оплату не підтверджено. Зв'яжись з адміном.")
        except: pass
        await q.message.edit_text("❌ Платіж відхилено.")

    elif data == "back":
        kb = [
            [InlineKeyboardButton("💎 7 днів — $0.50",  callback_data="buy:week")],
            [InlineKeyboardButton("💎 30 днів — $2.00", callback_data="buy:month")],
        ]
        await q.message.edit_text("💳 <b>Обери тариф:</b>", reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

# ── Адмін: видати доступ вручну ───────────────────────────────────────────────
async def cmd_give(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    if len(ctx.args) != 2:
        await update.message.reply_text("❗ Формат: /give USER_ID ДНІВ\nПриклад: /give 123456789 30")
        return
    try:
        target_id, days = int(ctx.args[0]), int(ctx.args[1])
        key = gen_key()
        plan = "week" if days <= 7 else "month"
        add_key(key, days, plan)
        await ctx.bot.send_message(
            target_id,
            f"🎁 <b>Тобі надано доступ від адміна!</b>\n\n"
            f"🔑 Ключ: <code>{key}</code>\nДнів: {days}\n\n"
            f"Введи в @MusicLSP_bot → 💎 Підписка → 🔑 Ввести ключ",
            parse_mode="HTML"
        )
        await update.message.reply_text(f"✅ Ключ надіслано юзеру {target_id} на {days} днів.")
    except Exception as e:
        await update.message.reply_text(f"❌ Помилка: {e}")

def main():
    app = Application.builder().token(AUTH_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("give",  cmd_give))
    app.add_handler(CallbackQueryHandler(on_callback))
    logger.info("✅ MusicLSPauth запущено!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
