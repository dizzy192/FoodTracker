import os
import json
import sqlite3
import logging
import threading
from datetime import datetime, timedelta

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
import google.generativeai as genai

# ─── CONFIG ───────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_KEY")
DB_PATH = "/tmp/calories.db"

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── GEMINI ───────────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

PROMPT = """Ты эксперт-диетолог. Пользователь написал что съел.
Верни ТОЛЬКО JSON без markdown и пояснений:
{
  "items": [{"name": "блюдо", "calories": 0, "protein": 0, "fat": 0, "carbs": 0}],
  "total": {"calories": 0, "protein": 0, "fat": 0, "carbs": 0},
  "summary": "короткий комментарий"
}
Все числа округли до 1 знака. Пользователь написал: """

# ─── DATABASE (синхронная SQLite) ─────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS meals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER, date TEXT, description TEXT,
        calories REAL, protein REAL, fat REAL, carbs REAL,
        items_json TEXT, summary TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS last_msg (
        user_id INTEGER PRIMARY KEY, message_id INTEGER
    )""")
    conn.commit()
    conn.close()
    logger.info("DB initialized")

def save_meal(user_id, description, total, items, summary):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO meals (user_id,date,description,calories,protein,fat,carbs,items_json,summary) VALUES (?,?,?,?,?,?,?,?,?)",
        (user_id, datetime.now().strftime("%Y-%m-%d"), description,
         total["calories"], total["protein"], total["fat"], total["carbs"],
         json.dumps(items, ensure_ascii=False), summary)
    )
    conn.commit()
    conn.close()

def get_meals(user_id, days):
    start = (datetime.now() - timedelta(days=days-1)).strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        "SELECT date,description,calories,protein,fat,carbs,summary FROM meals WHERE user_id=? AND date>=? ORDER BY created_at DESC",
        (user_id, start)
    ).fetchall()
    conn.close()
    return rows

def get_last_msg(user_id):
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT message_id FROM last_msg WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row[0] if row else None

def save_last_msg(user_id, message_id):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO last_msg(user_id,message_id) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET message_id=excluded.message_id", (user_id, message_id))
    conn.commit()
    conn.close()

# ─── GEMINI CALL ──────────────────────────────────────────────────────────────
def call_gemini(text):
    try:
        resp = model.generate_content(PROMPT + text)
        raw = resp.text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

# ─── FORMATTING ───────────────────────────────────────────────────────────────
def fmt_meal(desc, data):
    items = data.get("items", [])
    total = data.get("total", {})
    lines = [f"*{desc[:50]}*\n"]
    for it in items:
        lines.append(f"• *{it['name']}* — {it['calories']} ккал | Б:{it['protein']}г Ж:{it['fat']}г У:{it['carbs']}г")
    lines.append(f"\n{'─'*25}")
    lines.append(f"*Итого:* {total.get('calories',0)} ккал")
    lines.append(f"Белки: {total.get('protein',0)}г | Жиры: {total.get('fat',0)}г | Углеводы: {total.get('carbs',0)}г")
    if data.get("summary"):
        lines.append(f"\n_{data['summary']}_")
    lines.append(f"\n🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)

def fmt_history(rows, period):
    if not rows:
        return f"За {period} записей нет."
    by_date = {}
    for date, desc, cal, prot, fat, carbs, summ in rows:
        if date not in by_date:
            by_date[date] = {"meals": [], "cal": 0, "prot": 0, "fat": 0, "carbs": 0}
        by_date[date]["meals"].append((desc, cal))
        by_date[date]["cal"] += cal or 0
        by_date[date]["prot"] += prot or 0
        by_date[date]["fat"] += fat or 0
        by_date[date]["carbs"] += carbs or 0
    lines = [f"*История за {period}*\n"]
    for date in sorted(by_date.keys(), reverse=True):
        d = datetime.strptime(date, "%Y-%m-%d")
        lines.append(f"*{d.strftime('%d.%m.%Y')}*")
        for desc, cal in by_date[date]["meals"]:
            lines.append(f"  • {desc[:30]} — {cal:.0f} ккал")
        t = by_date[date]
        lines.append(f"  Итого: {t['cal']:.0f} ккал | Б:{t['prot']:.1f}г Ж:{t['fat']:.1f}г У:{t['carbs']:.1f}г\n")
    return "\n".join(lines)

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def delete_prev(context, chat_id, user_id):
    mid = get_last_msg(user_id)
    if mid:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=mid)
        except Exception:
            pass

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"START from {update.effective_user.id}")
    await delete_prev(context, update.effective_chat.id, update.effective_user.id)
    text = ("*Привет! Я считаю калории.*\n\n"
            "Напиши что ты съел, например:\n"
            "_съел шаурму и выпил колу 0.5_\n\n"
            "/today — сводка за сегодня\n"
            "/week — за 7 дней\n"
            "/help — помощь")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Сегодня", callback_data="today"),
        InlineKeyboardButton("Неделя", callback_data="week")
    ]])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    save_last_msg(update.effective_user.id, msg.message_id)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_prev(context, update.effective_chat.id, update.effective_user.id)
    text = ("*Как пользоваться:*\n\n"
            "Просто напиши что ел в свободной форме:\n"
            "_2 яйца и тост с маслом_\n"
            "_бигмак, картошка фри, кола_\n\n"
            "/today — сегодня\n/week — 7 дней")
    msg = await update.message.reply_text(text, parse_mode="Markdown")
    save_last_msg(update.effective_user.id, msg.message_id)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"TODAY from {update.effective_user.id}")
    await delete_prev(context, update.effective_chat.id, update.effective_user.id)
    rows = get_meals(update.effective_user.id, 1)
    text = fmt_history(rows, "сегодня")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Неделя", callback_data="week")]])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    save_last_msg(update.effective_user.id, msg.message_id)

async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    logger.info(f"WEEK from {update.effective_user.id}")
    await delete_prev(context, update.effective_chat.id, update.effective_user.id)
    rows = get_meals(update.effective_user.id, 7)
    text = fmt_history(rows, "7 дней")
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="today")]])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    save_last_msg(update.effective_user.id, msg.message_id)

async def handle_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    logger.info(f"FOOD from {user_id}: {text}")

    try:
        await delete_prev(context, chat_id, user_id)
    except Exception as e:
        logger.error(f"delete error: {e}")

    thinking = await update.message.reply_text("Анализирую...")

    try:
        data = call_gemini(text)
        logger.info(f"Gemini: {data}")
    except Exception as e:
        logger.error(f"Gemini call error: {e}")
        await thinking.edit_text(f"Ошибка Gemini: {e}")
        return

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=thinking.message_id)
    except Exception:
        pass

    if not data:
        msg = await update.message.reply_text("Исчерпан лимит запросов. Попробуй позже.")
        save_last_msg(user_id, msg.message_id)
        return

    try:
        save_meal(user_id, text, data["total"], data["items"], data.get("summary", ""))
    except Exception as e:
        logger.error(f"save_meal error: {e}")

    try:
        response = fmt_meal(text, data)
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("Сегодня", callback_data="today"),
            InlineKeyboardButton("Неделя", callback_data="week")
        ]])
        msg = await update.message.reply_text(response, parse_mode="Markdown", reply_markup=kb)
        save_last_msg(user_id, msg.message_id)
        logger.info(f"Sent response to {user_id}")
    except Exception as e:
        logger.error(f"send error: {e}")
        msg = await update.message.reply_text(f"Ошибка отправки: {e}")
        save_last_msg(user_id, msg.message_id)

async def button_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
    except Exception:
        pass
    if query.data == "today":
        rows = get_meals(user_id, 1)
        text = fmt_history(rows, "сегодня")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Неделя", callback_data="week")]])
    else:
        rows = get_meals(user_id, 7)
        text = fmt_history(rows, "7 дней")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Сегодня", callback_data="today")]])
    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)
    save_last_msg(user_id, msg.message_id)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    init_db()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CallbackQueryHandler(button_cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food))
    logger.info("Bot starting...")
    app.run_polling(drop_pending_updates=True)
