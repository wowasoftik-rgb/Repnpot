import logging
import re
import sqlite3
from datetime import datetime

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

BOT_TOKEN = "8976399480:AAGpIrkKcLUfy5aBUHh8TMYLK2vm5kl0K1M"
DB_PATH = "reputation.db"

# Юзернейм бота без @ — нужен для кнопки "Добавить в свой чат".
# Посмотреть его можно у @BotFather.
BOT_USERNAME = "Reputationalex_bot"

# Telegram ID админов, которым можно ставить репутацию без ограничений.
# Узнать свой ID можно командой /myid в этом боте.
ADMIN_IDS = {8673321126}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Временное хранилище отзывов, ожидающих оценки через кнопки.
# Ключ: "chat_id:from_user_id" -> данные об адресате и тексте отзыва.
pending_reviews = {}


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            full_name TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS reputation (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            from_user_id INTEGER,
            to_user_id INTEGER,
            to_username TEXT,
            score INTEGER,
            review TEXT,
            ts TEXT
        )
    """)
    conn.commit()
    conn.close()


def save_user(user):
    if not user:
        return
    conn = db()
    conn.execute(
        "INSERT INTO users (user_id, username, full_name) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, full_name=excluded.full_name",
        (user.id, user.username, user.full_name)
    )
    conn.commit()
    conn.close()


def save_reputation(chat_id, from_user_id, to_user_id, to_username, score, review):
    conn = db()
    conn.execute(
        "INSERT INTO reputation (chat_id, from_user_id, to_user_id, to_username, score, review, ts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat_id, from_user_id, to_user_id, to_username, score, review, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def star_bar(score):
    full = round(score)
    full = max(0, min(5, full))
    return "⭐" * full + "☆" * (5 - full)


def format_reputation(to_user_id, to_username, to_display):
    """Собирает текст с репутацией человека — используется и в /и, и в кнопке Профиль."""
    conn = db()
    if to_user_id:
        rows = conn.execute(
            "SELECT score, review, ts FROM reputation WHERE to_user_id = ? ORDER BY ts DESC",
            (to_user_id,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT score, review, ts FROM reputation WHERE to_username = ? COLLATE NOCASE ORDER BY ts DESC",
            (to_username,)
        ).fetchall()
    conn.close()

    if not rows:
        return f"У {to_display} пока нет отзывов."

    avg = sum(r['score'] for r in rows) / len(rows)
    text = (
        f"📊 Репутация {to_display}\n"
        f"{star_bar(avg)}  {avg:.1f}/5  ({len(rows)} отзывов)\n\n"
        f"Последние отзывы:\n"
    )
    for r in rows[:5]:
        line = f"⭐ {r['score']}/5"
        if r['review']:
            line += f" — {r['review']}"
        text += line + "\n"
    return text


REP_PATTERN = re.compile(r'^\+\s*реп\b', re.IGNORECASE)
I_COMMAND_PATTERN = re.compile(r'^/и(?:@\S+)?(?:\s+(.*))?$', re.IGNORECASE)
SCORE_PATTERN = re.compile(r'\b([1-5])\b')
MENTION_PATTERN = re.compile(r'@(\w+)')


def score_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{i} ⭐", callback_data=f"rep|{i}") for i in range(1, 6)]
    ])


def start_menu_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton("➕ Добавить в свой чат", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")],
    ])


def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ Назад", callback_data="back_start")]
    ])


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    save_user(update.effective_user)
    text = (
        "Привет! Я бот репутации 👋\n\n"
        "В группе пиши +реп в ответ на сообщение человека, чтобы оценить его.\n"
        "Посмотреть репутацию — команда /и (ответом на сообщение или /и @username).\n\n"
        "Выбери действие:"
    )
    await update.effective_message.reply_text(text, reply_markup=start_menu_keyboard())


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user = query.from_user

    if query.data == "profile":
        text = "👤 " + format_reputation(user.id, None, user.full_name)
        await query.edit_message_text(text, reply_markup=back_keyboard())
    elif query.data == "back_start":
        text = (
            "Привет! Я бот репутации 👋\n\n"
            "В группе пиши +реп в ответ на сообщение человека, чтобы оценить его.\n"
            "Посмотреть репутацию — команда /и (ответом на сообщение или /и @username).\n\n"
            "Выбери действие:"
        )
        await query.edit_message_text(text, reply_markup=start_menu_keyboard())


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Ловит '+реп 5 текст' (сразу сохраняет) или '+реп текст' без числа
    (показывает кнопки 1-5 для оценки). Работает и по reply, и по @username."""
    message = update.effective_message
    if not message or not message.text:
        return

    text = message.text.strip()
    if not REP_PATTERN.match(text):
        return

    save_user(update.effective_user)
    rest = REP_PATTERN.sub('', text, count=1).strip()

    to_user_id = None
    to_username = None
    to_display = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        if target.is_bot:
            await message.reply_text("Ботам репутацию не ставим 🙂")
            return
        if target.id == update.effective_user.id:
            await message.reply_text("Нельзя ставить репутацию самому себе 🙅")
            return
        to_user_id = target.id
        to_username = target.username
        to_display = target.full_name
        save_user(target)
    else:
        m = MENTION_PATTERN.search(rest)
        if not m:
            await message.reply_text(
                "Не понял, кому ставить репутацию.\n"
                "Ответь на сообщение человека командой:\n"
                "+реп текст отзыва\n"
                "или укажи @username:\n"
                "+реп @username текст отзыва"
            )
            return
        to_username = m.group(1)
        if update.effective_user.username and to_username.lower() == update.effective_user.username.lower():
            await message.reply_text("Нельзя ставить репутацию самому себе 🙅")
            return
        to_display = f"@{to_username}"
        rest = MENTION_PATTERN.sub('', rest, count=1).strip()

    score_match = SCORE_PATTERN.search(rest)

    if score_match:
        # Оценка уже указана явно — сохраняем сразу, без кнопок.
        score = int(score_match.group(1))
        review = SCORE_PATTERN.sub('', rest, count=1).strip()
        save_reputation(message.chat_id, update.effective_user.id, to_user_id, to_username, score, review)
        reply = f"✅ Репутация {to_display} обновлена: {score}/5 {star_bar(score)}"
        if review:
            reply += f"\n«{review}»"
        await message.reply_text(reply)
        return

    # Оценки нет в тексте — просим выбрать через кнопки.
    key = f"{message.chat_id}:{update.effective_user.id}"
    pending_reviews[key] = {
        "to_user_id": to_user_id,
        "to_username": to_username,
        "to_display": to_display,
        "review": rest,
    }
    await message.reply_text(f"Оцени {to_display} от 1 до 5:", reply_markup=score_keyboard())


async def rep_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        score = int(query.data.split("|")[1])
    except (IndexError, ValueError):
        return

    key = f"{query.message.chat_id}:{query.from_user.id}"
    pending = pending_reviews.pop(key, None)

    if not pending:
        await query.edit_message_text("⌛ Время вышло, начни заново: +реп текст отзыва")
        return

    save_reputation(
        query.message.chat_id,
        query.from_user.id,
        pending["to_user_id"],
        pending["to_username"],
        score,
        pending["review"],
    )

    reply = f"✅ Репутация {pending['to_display']} обновлена: {score}/5 {star_bar(score)}"
    if pending["review"]:
        reply += f"\n«{pending['review']}»"
    await query.edit_message_text(reply)


async def show_rep_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/и — своя репутация, /и @username — чужая, или ответом на сообщение."""
    message = update.effective_message
    if not message or not message.text:
        return

    m = I_COMMAND_PATTERN.match(message.text.strip())
    if not m:
        return

    args_str = (m.group(1) or "").strip()

    to_user_id = None
    to_username = None
    to_display = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        to_user_id = target.id
        to_display = target.full_name
    elif args_str:
        arg = args_str.split()[0].lstrip('@')
        to_username = arg
        to_display = f"@{arg}"
    else:
        to_user_id = update.effective_user.id
        to_display = update.effective_user.full_name

    text = format_reputation(to_user_id, to_username, to_display)
    await message.reply_text(text)


async def myid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает Telegram ID пользователя — чтобы добавить себя в ADMIN_IDS."""
    await update.effective_message.reply_text(f"Твой Telegram ID: {update.effective_user.id}")


async def admin_setrep(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/addrep — только для админов. Ставит любую оценку любому человеку,
    без ограничения 1-5 и без запрета на себя.
    Использование:
      ответом на сообщение: /addrep 100 текст отзыва
      или по нику:          /addrep @username 100 текст отзыва
    """
    message = update.effective_message
    user = update.effective_user

    if user.id not in ADMIN_IDS:
        await message.reply_text("Эта команда только для админов.")
        return

    args = list(context.args)

    to_user_id = None
    to_username = None
    to_display = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target = message.reply_to_message.from_user
        to_user_id = target.id
        to_username = target.username
        to_display = target.full_name
        save_user(target)
    elif args and args[0].startswith('@'):
        to_username = args[0].lstrip('@')
        to_display = f"@{to_username}"
        args = args[1:]
    else:
        await message.reply_text(
            "Использование:\n"
            "Ответом на сообщение: /addrep 100 текст отзыва\n"
            "Или по нику: /addrep @username 100 текст отзыва"
        )
        return

    if not args:
        await message.reply_text("Не указана оценка. Пример: /addrep 100 текст отзыва")
        return

    try:
        score = int(args[0])
    except ValueError:
        await message.reply_text("Оценка должна быть числом (можно любым, хоть отрицательным).")
        return

    review = " ".join(args[1:])

    save_reputation(message.chat_id, user.id, to_user_id, to_username, score, review)

    reply = f"👑 (админ) Репутация {to_display} изменена: {score}"
    if review:
        reply += f"\n«{review}»"
    await message.reply_text(reply)


def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("addrep", admin_setrep))
    app.add_handler(CommandHandler("myid", myid))
    app.add_handler(CallbackQueryHandler(rep_callback, pattern=r"^rep\|"))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^(profile|back_start)$"))
    # Регистрируем ДО handle_message, чтобы "/и" не улетал в обработчик +реп.
    app.add_handler(MessageHandler(filters.Regex(r'(?i)^/и'), show_rep_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Бот запущен")
    app.run_polling()


if __name__ == "__main__":
    main()
