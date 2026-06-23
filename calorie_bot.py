import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, CallbackQueryHandler
)
import google.generativeai as genai
import aiosqlite

# ─── CONFIG ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "YOUR_TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_API_KEY")
DB_PATH = "calories.db"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ─── GEMINI SETUP ─────────────────────────────────────────────────────────────
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-2.0-flash")

GEMINI_SYSTEM_PROMPT = """Ты — эксперт-диетолог. Пользователь напишет тебе что он съел.
Твоя задача — проанализировать блюда и вернуть JSON со следующей структурой (ТОЛЬКО JSON, без пояснений):

{
  "items": [
    {"name": "Название блюда", "calories": 123, "protein": 12.5, "fat": 8.3, "carbs": 15.2}
  ],
  "total": {
    "calories": 123,
    "protein": 12.5,
    "fat": 8.3,
    "carbs": 15.2
  },
  "summary": "Короткий комментарий на русском о приёме пищи (1-2 предложения)"
}

Все числа — округли до 1 знака после запятой. Если не можешь распознать блюдо — угадай ближайший аналог.
ВАЖНО: верни ТОЛЬКО JSON без markdown-блоков и пояснений."""

# ─── DATABASE ─────────────────────────────────────────────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS meals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                date TEXT NOT NULL,
                description TEXT NOT NULL,
                calories REAL,
                protein REAL,
                fat REAL,
                carbs REAL,
                items_json TEXT,
                summary TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS last_bot_message (
                user_id INTEGER PRIMARY KEY,
                message_id INTEGER
            )
        """)
        await db.commit()

async def save_meal(user_id, description, total, items, summary):
    today = datetime.now().strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO meals (user_id, date, description, calories, protein, fat, carbs, items_json, summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id, today, description,
            total["calories"], total["protein"], total["fat"], total["carbs"],
            json.dumps(items, ensure_ascii=False), summary
        ))
        await db.commit()

async def get_meals_by_period(user_id, days):
    start_date = (datetime.now() - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT date, description, calories, protein, fat, carbs, summary
            FROM meals
            WHERE user_id = ? AND date >= ?
            ORDER BY created_at DESC
        """, (user_id, start_date))
        return await cursor.fetchall()

async def get_last_bot_message(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT message_id FROM last_bot_message WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None

async def save_last_bot_message(user_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO last_bot_message (user_id, message_id)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET message_id = excluded.message_id
        """, (user_id, message_id))
        await db.commit()

# ─── GEMINI ANALYSIS ──────────────────────────────────────────────────────────
async def analyze_food(text: str) -> dict | None:
    try:
        prompt = f"{GEMINI_SYSTEM_PROMPT}\n\nПользователь написал: {text}"
        response = model.generate_content(prompt)
        raw = response.text.strip()
        # Clean possible markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        return json.loads(raw.strip())
    except Exception as e:
        logger.error(f"Gemini error: {e}")
        return None

# ─── FORMATTING ───────────────────────────────────────────────────────────────
def format_meal_response(description: str, data: dict) -> str:
    items = data.get("items", [])
    total = data.get("total", {})
    summary = data.get("summary", "")

    lines = [f"🍽 *{description}*\n"]
    for item in items:
        lines.append(
            f"• *{item['name']}*\n"
            f"  🔥 {item['calories']} ккал · "
            f"💪 Б: {item['protein']}г · "
            f"🧈 Ж: {item['fat']}г · "
            f"🌾 У: {item['carbs']}г"
        )

    lines.append(f"\n{'─'*28}")
    lines.append(
        f"📊 *Итого:*\n"
        f"🔥 Калории: *{total.get('calories', 0)}* ккал\n"
        f"💪 Белки: *{total.get('protein', 0)}* г\n"
        f"🧈 Жиры: *{total.get('fat', 0)}* г\n"
        f"🌾 Углеводы: *{total.get('carbs', 0)}* г"
    )

    if summary:
        lines.append(f"\n💬 _{summary}_")

    lines.append(f"\n🕐 {datetime.now().strftime('%d.%m.%Y %H:%M')}")
    return "\n".join(lines)

def format_history(rows: list, period_name: str) -> str:
    if not rows:
        return f"📭 За {period_name} записей нет."

    # Group by date
    by_date = {}
    for date, desc, cal, prot, fat, carbs, summ in rows:
        if date not in by_date:
            by_date[date] = {"meals": [], "totals": {"cal": 0, "prot": 0, "fat": 0, "carbs": 0}}
        by_date[date]["meals"].append((desc, cal, prot, fat, carbs))
        by_date[date]["totals"]["cal"] += cal or 0
        by_date[date]["totals"]["prot"] += prot or 0
        by_date[date]["totals"]["fat"] += fat or 0
        by_date[date]["totals"]["carbs"] += carbs or 0

    lines = [f"📅 *История за {period_name}*\n"]
    grand = {"cal": 0, "prot": 0, "fat": 0, "carbs": 0}

    for date in sorted(by_date.keys(), reverse=True):
        d = datetime.strptime(date, "%Y-%m-%d")
        day_label = d.strftime("%d.%m.%Y (%A)").replace(
            "Monday", "Пн").replace("Tuesday", "Вт").replace("Wednesday", "Ср").replace(
            "Thursday", "Чт").replace("Friday", "Пт").replace("Saturday", "Сб").replace(
            "Sunday", "Вс")
        lines.append(f"📆 *{day_label}*")
        t = by_date[date]["totals"]
        for desc, cal, prot, fat, carbs in by_date[date]["meals"]:
            short = desc[:35] + "…" if len(desc) > 35 else desc
            lines.append(f"  • {short} — {cal:.0f} ккал")
        lines.append(
            f"  ▶ Итого: 🔥{t['cal']:.0f} · 💪{t['prot']:.1f}г · 🧈{t['fat']:.1f}г · 🌾{t['carbs']:.1f}г\n"
        )
        for k in grand:
            grand[k] += t[k]

    if len(by_date) > 1:
        lines.append(f"{'─'*28}")
        lines.append(
            f"📊 *Всего за период:*\n"
            f"🔥 {grand['cal']:.0f} ккал · 💪 {grand['prot']:.1f}г · "
            f"🧈 {grand['fat']:.1f}г · 🌾 {grand['carbs']:.1f}г"
        )

    return "\n".join(lines)

# ─── HELPERS ──────────────────────────────────────────────────────────────────
async def delete_last_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int):
    msg_id = await get_last_bot_message(user_id)
    if msg_id:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass

# ─── HANDLERS ─────────────────────────────────────────────────────────────────
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_last_message(context, update.effective_chat.id, update.effective_user.id)
    text = (
        "👋 *Привет! Я твой калорийный дневник.*\n\n"
        "Просто напиши мне что ты съел, например:\n"
        "_съел 1 шаурму и 1 колу 0.5_\n"
        "_выпил латте и съел круассан_\n\n"
        "📌 *Команды:*\n"
        "/today — сводка за сегодня\n"
        "/week — история за 7 дней\n"
        "/help — помощь"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Сегодня", callback_data="today"),
         InlineKeyboardButton("📅 Неделя", callback_data="week")]
    ])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    await save_last_bot_message(update.effective_user.id, msg.message_id)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await delete_last_message(context, update.effective_chat.id, update.effective_user.id)
    text = (
        "📖 *Как пользоваться ботом:*\n\n"
        "Просто напиши что ты ел — в свободной форме:\n"
        "• _съел 2 яйца и тост с маслом_\n"
        "• _выпил протеиновый коктейль 30г_\n"
        "• _бигмак, картошка фри средняя, кола 0.4_\n\n"
        "Я сам разберу порции и посчитаю КБЖУ 🧮\n\n"
        "📌 *Команды:*\n"
        "/today — 🔥 сводка за сегодня\n"
        "/week — 📅 история за 7 дней\n"
        "/start — 🏠 главное меню"
    )
    msg = await update.message.reply_text(text, parse_mode="Markdown")
    await save_last_bot_message(update.effective_user.id, msg.message_id)

async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await delete_last_message(context, chat_id, user_id)

    rows = await get_meals_by_period(user_id, 1)
    text = format_history(rows, "сегодня")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 За неделю", callback_data="week")
    ]])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    await save_last_bot_message(user_id, msg.message_id)

async def week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    await delete_last_message(context, chat_id, user_id)

    rows = await get_meals_by_period(user_id, 7)
    text = format_history(rows, "7 дней")
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📊 Сегодня", callback_data="today")
    ]])
    msg = await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    await save_last_bot_message(user_id, msg.message_id)

async def handle_food(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    text = update.message.text.strip()

    # Delete previous bot message
    await delete_last_message(context, chat_id, user_id)

    # Show "thinking" message
    thinking = await update.message.reply_text("⏳ Анализирую...", parse_mode="Markdown")

    data = await analyze_food(text)

    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=thinking.message_id)
    except Exception:
        pass

    if not data:
        msg = await update.message.reply_text(
            "❌ Не удалось распознать. Попробуй описать иначе, например:\n_съел шаурму и выпил колу_",
            parse_mode="Markdown"
        )
        await save_last_bot_message(user_id, msg.message_id)
        return

    await save_meal(user_id, text, data["total"], data["items"], data.get("summary", ""))
    response_text = format_meal_response(text, data)

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Сводка за сегодня", callback_data="today"),
         InlineKeyboardButton("📅 За неделю", callback_data="week")]
    ])

    msg = await update.message.reply_text(response_text, parse_mode="Markdown", reply_markup=kb)
    await save_last_bot_message(user_id, msg.message_id)

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    chat_id = query.message.chat_id

    if query.data == "today":
        rows = await get_meals_by_period(user_id, 1)
        text = format_history(rows, "сегодня")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 За неделю", callback_data="week")
        ]])
    elif query.data == "week":
        rows = await get_meals_by_period(user_id, 7)
        text = format_history(rows, "7 дней")
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("📊 Сегодня", callback_data="today")
        ]])
    else:
        return

    # Delete old message, send new
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=query.message.message_id)
    except Exception:
        pass

    msg = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown", reply_markup=kb)
    await save_last_bot_message(user_id, msg.message_id)

# ─── MAIN ─────────────────────────────────────────────────────────────────────
async def main():
    await init_db()

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("week", week_cmd))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_food))

    logger.info("Bot started!")
    await app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    app_instance = None
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(main())
    finally:
        loop.close()
