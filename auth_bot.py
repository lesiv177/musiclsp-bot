#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MusicLSP Auth Bot — Telegram Stars Payment
Автоматична активація Premium через оплату Stars
"""

import os
import logging
import datetime

# Спробуємо підключити psycopg2 (PostgreSQL), якщо немає — використаємо SQLite
try:
    import psycopg2
    from psycopg2.extras import RealDictCursor
    POSTGRES_AVAILABLE = True
except ImportError:
    POSTGRES_AVAILABLE = False
import sqlite3

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    PreCheckoutQueryHandler, MessageHandler, ContextTypes, filters
)

# ─── Логування ────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── Конфіг ───────────────────────────────────────────────────────────────────
AUTH_BOT_TOKEN = os.environ.get("AUTH_BOT_TOKEN", "")
ADMIN_ID = 1293055247
AUTHOR = "Lesiv"

# Database: PostgreSQL (Railway) або SQLite (fallback)
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

# ─── Тарифи (Stars) ──────────────────────────────────────────────────────────
PLANS = {
    "test":   {"days": 7,  "stars": 3,   "label": "⭐ Тест: 7 днів — 3 Stars"},
    "month":  {"days": 30, "stars": 300, "label": "💎 Місяць: 30 днів — 300 Stars"},
    "quarter":{"days": 90, "stars": 750, "label": "👑 Квартал: 90 днів — 750 Stars"},
}

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

def init_db():
    """Initialize database tables (same as main bot)."""
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
                        premium_expires TIMESTAMP,
                        state TEXT DEFAULT ''
                    )
                """)
                c.execute("""
                    CREATE TABLE IF NOT EXISTS payments (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT,
                        plan TEXT,
                        stars INTEGER,
                        days INTEGER,
                        payment_date TIMESTAMP,
                        telegram_payment_charge_id TEXT
                    )
                """)
                conn.commit()
                logger.info("✅ PostgreSQL tables initialized")
        except Exception as e:
            logger.error(f"PostgreSQL init error: {e}")
            raise
        finally:
            conn.close()
    else:
        with db() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                username TEXT,
                lang TEXT DEFAULT 'uk',
                joined TEXT,
                is_premium INTEGER DEFAULT 0,
                premium_since TEXT,
                premium_expires TEXT,
                state TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                plan TEXT,
                stars INTEGER,
                days INTEGER,
                payment_date TEXT,
                telegram_payment_charge_id TEXT
            );
            """)

def get_user(uid):
    with db() as c:
        if USE_POSTGRES:
            cur = c.cursor()
            cur.execute("SELECT * FROM users WHERE id = %s", (uid,))
            row = cur.fetchone()
            if row:
                cols = [desc[0] for desc in cur.description]
                return dict(zip(cols, row))
            return None
        else:
            return c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()

def create_user(uid, username):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with db() as c:
        if USE_POSTGRES:
            cur = c.cursor()
            cur.execute(
                "INSERT INTO users (id, username, joined) VALUES (%s, %s, %s) ON CONFLICT (id) DO NOTHING",
                (uid, username, now)
            )
            c.commit()
        else:
            c.execute(
                "INSERT OR IGNORE INTO users (id, username, joined) VALUES (?, ?, ?)",
                (uid, username, now)
            )

def activate_premium(uid, days, plan, stars, charge_id):
    """Activate premium for user."""
    now = datetime.datetime.now(datetime.timezone.utc)
    expires = (now + datetime.timedelta(days=days)).isoformat()
    now_iso = now.isoformat()

    with db() as c:
        if USE_POSTGRES:
            cur = c.cursor()
            # Update user premium status
            cur.execute(
                """UPDATE users 
                   SET is_premium = TRUE, 
                       premium_since = %s, 
                       premium_expires = %s 
                   WHERE id = %s""",
                (now_iso, expires, uid)
            )
            # Record payment
            cur.execute(
                """INSERT INTO payments (user_id, plan, stars, days, payment_date, telegram_payment_charge_id)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (uid, plan, stars, days, now_iso, charge_id)
            )
            c.commit()
        else:
            c.execute(
                """UPDATE users 
                   SET is_premium = 1, 
                       premium_since = ?, 
                       premium_expires = ? 
                   WHERE id = ?""",
                (now_iso, expires, uid)
            )
            c.execute(
                """INSERT INTO payments (user_id, plan, stars, days, payment_date, telegram_payment_charge_id)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (uid, plan, stars, days, now_iso, charge_id)
            )

    logger.info(f"Premium activated for user {uid}: {days} days, plan={plan}")
    return expires

def is_premium_active(uid):
    """Check if user has active premium."""
    u = get_user(uid)
    if not u:
        return False

    if USE_POSTGRES:
        is_prem = u.get("is_premium", False)
        expires = u.get("premium_expires")
    else:
        is_prem = bool(u["is_premium"])
        expires = u["premium_expires"]

    if not is_prem or not expires:
        return False

    try:
        exp_dt = datetime.datetime.fromisoformat(expires)
        return datetime.datetime.now(datetime.timezone.utc) < exp_dt
    except:
        return False

# ─── Telegram Handlers ────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show payment menu."""
    uid = update.effective_user.id
    username = update.effective_user.username or ""
    create_user(uid, username)

    # Check current status
    active = is_premium_active(uid)
    u = get_user(uid)

    text = "💳 <b>MusicLSP — Підписка</b>\n\n"

    if active:
        expires = u.get("premium_expires", "—")
        if expires:
            try:
                exp_dt = datetime.datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.datetime.now(datetime.timezone.utc)).days
                text += f"✅ <b>Premium активно!</b>\n"
                text += f"📅 Закінчується: {expires[:10]}\n"
                text += f"⏳ Залишилось: {days_left} днів\n\n"
            except:
                text += "✅ <b>Premium активно!</b>\n\n"
    else:
        text += "💿 Зараз: <b>Free</b>\n\n"

    text += "Обери тариф і оплати Stars ⭐:\n\n"

    kb = [
        [InlineKeyboardButton(PLANS["test"]["label"], callback_data="buy:test")],
        [InlineKeyboardButton(PLANS["month"]["label"], callback_data="buy:month")],
        [InlineKeyboardButton(PLANS["quarter"]["label"], callback_data="buy:quarter")],
    ]

    if active:
        kb.append([InlineKeyboardButton("📊 Мій статус", callback_data="status")])

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle button clicks."""
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    data = q.data

    if data.startswith("buy:"):
        plan = data[4:]
        info = PLANS.get(plan)
        if not info:
            await q.message.edit_text("❌ Помилка: невідомий тариф")
            return

        # Send invoice for Stars payment
        title = f"MusicLSP Premium — {info['days']} днів"
        description = f"Доступ до Premium функцій на {info['days']} днів"
        payload = f"premium_{plan}_{uid}_{datetime.datetime.now().timestamp()}"
        currency = "XTR"  # Telegram Stars
        prices = [LabeledPrice(label=info["label"], amount=info["stars"])]

        await ctx.bot.send_invoice(
            chat_id=uid,
            title=title,
            description=description,
            payload=payload,
            provider_token="",  # Empty for Stars
            currency=currency,
            prices=prices,
            start_parameter="premium_payment"
        )

    elif data == "status":
        active = is_premium_active(uid)
        u = get_user(uid)

        if active and u:
            expires = u.get("premium_expires", "—")
            text = f"✅ <b>Premium активно!</b>\n\n"
            text += f"📅 Закінчується: {expires[:10]}\n"
            try:
                exp_dt = datetime.datetime.fromisoformat(expires)
                days_left = (exp_dt - datetime.datetime.now(datetime.timezone.utc)).days
                text += f"⏳ Залишилось: {days_left} днів"
            except:
                pass
        else:
            text = "💿 <b>Free</b>\n\nОформи Premium через /start"

        kb = [[InlineKeyboardButton("◀️ Назад", callback_data="back")]]
        await q.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif data == "back":
        await cmd_start(update, ctx)

async def precheckout_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle pre-checkout query."""
    query = update.pre_checkout_query
    # Always accept Stars payments
    await query.answer(ok=True)
    logger.info(f"Pre-checkout accepted for user {query.from_user.id}")

async def successful_payment_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Handle successful payment — activate premium."""
    payment = update.message.successful_payment
    uid = update.effective_user.id
    payload = payment.invoice_payload
    stars = payment.total_amount
    charge_id = payment.telegram_payment_charge_id

    # Parse plan from payload: premium_PLAN_UID_TIMESTAMP
    try:
        parts = payload.split("_")
        plan = parts[1]
        plan_info = PLANS.get(plan)
        if not plan_info:
            await update.message.reply_text("❌ Помилка: невідомий тариф. Звернись до адміна.")
            return

        days = plan_info["days"]

        # Activate premium
        expires = activate_premium(uid, days, plan, stars, charge_id)

        # Send confirmation
        text = (
            f"🎉 <b>Оплату успішно завершено!</b>\n\n"
            f"💎 Premium активовано!\n"
            f"📅 Термін: {days} днів\n"
            f"⏳ Закінчується: {expires[:10]}\n\n"
            f"Переходь в @MusicLSP_bot і користуйся всіма функціями! 🎵"
        )

        kb = [[InlineKeyboardButton("🎵 Перейти в MusicLSP", url="https://t.me/MusicLSP_bot")]]
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

        # Notify admin
        try:
            user = await ctx.bot.get_chat(uid)
            uname = f"@{user.username}" if user.username else str(uid)
            await ctx.bot.send_message(
                ADMIN_ID,
                f"💰 <b>Нова оплата Stars!</b>\n\n"
                f"👤 {uname} (<code>{uid}</code>)\n"
                f"💎 {plan_info['label']}\n"
                f"⭐ {stars} Stars\n"
                f"📅 {days} днів",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Failed to notify admin: {e}")

    except Exception as e:
        logger.error(f"Payment processing error: {e}")
        await update.message.reply_text(
            "❌ Помилка активації. Звернись до адміна @Lesiv.",
            parse_mode="HTML"
        )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Check premium status."""
    uid = update.effective_user.id
    active = is_premium_active(uid)
    u = get_user(uid)

    if active and u:
        expires = u.get("premium_expires", "—")
        text = f"✅ <b>Premium активно!</b>\n\n"
        text += f"📅 Закінчується: {expires[:10]}\n"
        try:
            exp_dt = datetime.datetime.fromisoformat(expires)
            days_left = (exp_dt - datetime.datetime.now(datetime.timezone.utc)).days
            text += f"⏳ Залишилось: {days_left} днів"
        except:
            pass
    else:
        text = "💿 <b>Free</b>\n\nОформи Premium через /start"

    await update.message.reply_text(text, parse_mode="HTML")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    init_db()

    if not AUTH_BOT_TOKEN:
        logger.error("AUTH_BOT_TOKEN not set!")
        return

    app = Application.builder().token(AUTH_BOT_TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(PreCheckoutQueryHandler(precheckout_callback))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))

    logger.info("🚀 MusicLSP Auth Bot запущено!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
