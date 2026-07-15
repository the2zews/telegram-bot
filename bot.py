import logging
import re
import time
import asyncio
from collections import defaultdict, deque
import sqlite3
from typing import Dict, Optional
from telegram import (
    Update, ChatPermissions, BotCommand, InlineKeyboardButton,
    InlineKeyboardMarkup, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, PicklePersistence
)
from telegram.error import TelegramError

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = "8637462837:AAFygcu0eLNbXwhOMRPwuDwiry_bx8ij5KM"
ADMIN_IDS = {5460879396, 8176145729, 1087968824}
FLOOD_LIMIT = 8
FLOOD_TIME = 15
FLOOD_MUTE_DURATION = 300
ADMIN_MENTION = "Если заметите баги у бота, пишите @yabrad"
DISCUSSION_CHAT_ID = -1004328889951

ADMIN_CACHE = {}
ADMIN_CACHE_TTL = 30

# ==================== БАЗА ДАННЫХ ====================
class Database:
    def __init__(self, db_path="warns.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute('''
                CREATE TABLE IF NOT EXISTS warns (
                    user_id INTEGER, chat_id INTEGER, category TEXT,
                    count INTEGER, PRIMARY KEY (user_id, chat_id, category)
                )
            ''')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_warns_user ON warns(user_id, chat_id)")

    def _connect(self):
        return sqlite3.connect(self.db_path)

    def get_warn(self, user_id, chat_id, category):
        with self._connect() as conn:
            cur = conn.execute(
                'SELECT count FROM warns WHERE user_id=? AND chat_id=? AND category=?',
                (user_id, chat_id, category)
            )
            result = cur.fetchone()
            return result[0] if result else 0

    def add_warn(self, user_id, chat_id, category):
        with self._connect() as conn:
            cur = conn.execute(
                'SELECT count FROM warns WHERE user_id=? AND chat_id=? AND category=?',
                (user_id, chat_id, category)
            )
            current = cur.fetchone()
            new_count = (current[0] if current else 0) + 1
            conn.execute(
                'INSERT OR REPLACE INTO warns VALUES (?,?,?,?)',
                (user_id, chat_id, category, new_count)
            )
            return new_count

    def reset_all_warns(self, user_id, chat_id):
        with self._connect() as conn:
            conn.execute('DELETE FROM warns WHERE user_id=? AND chat_id=?', (user_id, chat_id))

db = Database()

# ==================== ХРАНИЛИЩА ====================
def get_muted_users(context): return context.bot_data.setdefault('muted_users', {})
def get_user_messages(context): return context.bot_data.setdefault('user_messages', defaultdict(lambda: deque(maxlen=FLOOD_LIMIT)))
def get_pinned_messages(context): return context.bot_data.setdefault('pinned_messages', {})
def get_message_counter(context): return context.bot_data.get('message_counter', 0)
def set_message_counter(context, value): context.bot_data['message_counter'] = value

# ==================== РЕГУЛЯРКИ ====================
LINK_PATTERNS = [re.compile(p, re.IGNORECASE) for p in [
    r'https?://[^\s]+', r'www\.[^\s]+', r't\.me/[^\s]+', r'telegram\.org/[^\s]+',
    r'bit\.ly/[^\s]+', r'clck\.ru/[^\s]+', r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/?'
]]
PHONE_PATTERNS = [re.compile(p) for p in [
    r'^\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,4}[\s\-]?\d{1,4}',
    r'^\+?\d[\d\-]{8,}\d', r'^\+?[0-9]{10,15}$'
]]
EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)

INSULTS = {"даун", "олигофрен", "дегенерат", "слабоумный", "конченый", "конченая", "клоун",
           "ебалай", "еблашка", "ебло", "ебало", "соси", "сосал", "сосет", "отсоси", "сосун",
           "съеби", "съебал", "тварь", "тварьебаная", "сукаебаная"}
ADULT_WORDS = {"порно", "секс", "насилие", "изнасилование", "педофил", "педофилия",
               "зоофил", "зоофилия", "сатанизм", "расчленение", "насильник", "педофильский"}

# ==================== УТИЛИТЫ ====================
def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'[^а-яёa-z0-9]', '', text.lower())
    trans = str.maketrans("abcdefghijklmnopqrstuvwxyz", "абвгдеёжзийклмнопрстуфхцчшщъыьэюя")
    return text.translate(trans)

def contains_word(text: str, word_set: set) -> bool:
    if not text: return False
    cleaned = clean_text(text)
    return any(clean_text(word) in cleaned for word in word_set)

def detect_link(text: str) -> bool:
    return any(p.search(text) for p in LINK_PATTERNS) if text else False

def detect_phone(text: str) -> bool:
    return any(p.search(text) for p in PHONE_PATTERNS) if text else False

def detect_email(text: str) -> bool:
    return bool(EMAIL_PATTERN.search(text)) if text else False

def parse_time(time_str: str) -> int:
    if not time_str: return 0
    time_str = time_str.lower()
    if time_str.isdigit(): return int(time_str) * 60
    m = re.match(r'(\d+)([smhd])', time_str)
    if m:
        v, u = int(m.group(1)), m.group(2)
        return v * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
    return 0

def format_duration(seconds: int) -> str:
    if seconds == 0: return "навсегда"
    for div, unit in [(86400, "дней"), (3600, "часов"), (60, "минут")]:
        if seconds >= div: return f"{seconds // div} {unit}"
    return f"{seconds} секунд"

# ==================== КЭШ ====================
def get_cached_admin_status(user_id: int, chat_id: int) -> Optional[bool]:
    key = f"{user_id}_{chat_id}"
    if key in ADMIN_CACHE:
        status, ts = ADMIN_CACHE[key]
        if time.time() - ts < ADMIN_CACHE_TTL: return status
        del ADMIN_CACHE[key]
    return None

def set_cached_admin_status(user_id: int, chat_id: int, status: bool):
    ADMIN_CACHE[f"{user_id}_{chat_id}"] = (status, time.time())

# ==================== ПРОВЕРКА АДМИНА ====================
async def is_command_from_real_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_user: return False
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if user_id in ADMIN_IDS: return True

    cached = get_cached_admin_status(user_id, chat_id)
    if cached is not None: return cached

    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        is_admin = member.status in ["administrator", "creator"]
        set_cached_admin_status(user_id, chat_id, is_admin)
        return is_admin
    except:
        set_cached_admin_status(user_id, chat_id, False)
        return False

# ==================== МУТ И ФЛУД ====================
def is_muted(context, user_id, chat_id):
    key = f"{user_id}_{chat_id}"
    muted = get_muted_users(context)
    if key in muted and muted[key] > time.time(): return True
    muted.pop(key, None)
    return False

def set_muted(context, user_id, chat_id, duration):
    get_muted_users(context)[f"{user_id}_{chat_id}"] = time.time() + (duration or 31536000)

def remove_mute(context, user_id, chat_id):
    get_muted_users(context).pop(f"{user_id}_{chat_id}", None)

def check_flood(context, user_id, chat_id):
    now = time.time()
    messages = get_user_messages(context)
    messages[user_id].append(now)
    while messages[user_id] and messages[user_id][0] < now - FLOOD_TIME:
        messages[user_id].popleft()
    if len(messages[user_id]) > FLOOD_LIMIT:
        messages[user_id].clear()
        return True
    return False

# ==================== ОТПРАВКА ====================
async def send_with_counter(context, chat_id, text):
    counter = get_message_counter(context) + 1
    set_message_counter(context, counter)
    await context.bot.send_message(chat_id=chat_id, text=text)
    if counter % 4 == 0:
        await context.bot.send_message(chat_id=chat_id, text=ADMIN_MENTION)

async def send_approval_request(context, command_type, target_name, target_id, duration, duration_text, requester_name, requester_id, chat_id):
    request_id = f"{int(time.time())}_{requester_id}_{target_id}"
    pending = context.bot_data.setdefault('pending_commands', {})
    pending[request_id] = {
        "command_type": command_type, "target_id": target_id, "chat_id": chat_id,
        "duration": duration, "requester_id": requester_id,
        "message_ids": {}, "created_at": time.time()
    }

    keyboard = [[
        InlineKeyboardButton("Разрешить", callback_data=f"a_{request_id}"),
        InlineKeyboardButton("Запретить", callback_data=f"d_{request_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    text = f"ЗАПРОС НА {command_type.upper()}\n\nКто запросил: @{requester_name} (ID: {requester_id})\nКого: @{target_name} (ID: {target_id})\nВремя: {duration_text}\nЧат: {chat_id}"

    all_admins = list(ADMIN_IDS)
    for admin_id in all_admins:
        try:
            msg = await context.bot.send_message(admin_id, text, reply_markup=reply_markup)
            pending[request_id]["message_ids"][admin_id] = msg.message_id
        except:
            pass

# ==================== ВЫПОЛНЕНИЕ ====================
async def process_approved_action(context, command_type, target_id, chat_id, duration=0):
    if command_type == "mute":
        dur = duration or 31536000
        set_muted(context, target_id, chat_id, dur)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + dur)
        await send_with_counter(context, chat_id, f"Пользователь замучен.\nПравила: /rules")
        db.reset_all_warns(target_id, chat_id)
    elif command_type == "unmute":
        remove_mute(context, target_id, chat_id)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=True))
        await send_with_counter(context, chat_id, f"Мут снят.")
    elif command_type == "ban":
        await context.bot.ban_chat_member(chat_id, target_id, until_date=int(time.time()) + duration if duration else None)
        duration_text = format_duration(duration) if duration > 0 else "навсегда"
        await send_with_counter(context, chat_id, f"Пользователь забанен на {duration_text}.\nПравила: /rules")
        db.reset_all_warns(target_id, chat_id)
    elif command_type == "unban":
        await context.bot.unban_chat_member(chat_id, target_id)
        await send_with_counter(context, chat_id, f"Бан снят.")
    elif command_type == "kick":
        await context.bot.ban_chat_member(chat_id, target_id)
        await context.bot.unban_chat_member(chat_id, target_id)
        await send_with_counter(context, chat_id, f"Пользователь кикнут.")
        db.reset_all_warns(target_id, chat_id)
    elif command_type == "warn":
        db.add_warn(target_id, chat_id, "insult")
        await send_with_counter(context, chat_id, f"Пользователь получил предупреждение.")
    elif command_type == "unwarn":
        db.reset_all_warns(target_id, chat_id)
        await send_with_counter(context, chat_id, f"Предупреждения сняты.")

async def get_target(update, context):
    if update.message.reply_to_message:
        t = update.message.reply_to_message.from_user
        return t, t.id
    for arg in context.args or []:
        try:
            if arg.startswith('@'):
                user = await context.bot.get_chat(arg)
            else:
                user = await context.bot.get_chat(int(arg))
            return user, user.id
        except:
            continue
    return None, None

async def handle_command_with_approval(update, context, command_type):
    if not update.message: return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id

    target, target_id = await get_target(update, context)
    if not target:
        try:
            await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        except:
            pass
        return

    name = target.username or target.first_name
    duration = parse_time(update.message.text)
    duration_text = format_duration(duration) if duration > 0 else "навсегда"

    if await is_command_from_real_admin(update, context):
        await process_approved_action(context, command_type, target_id, chat_id, duration)
        await update.message.delete()
        try:
            await update.effective_user.send_message(f"Команда {command_type} выполнена.")
        except:
            pass
        return

    requester_name = update.effective_user.username or update.effective_user.first_name
    await send_approval_request(
        context, command_type, name, target_id, duration, duration_text,
        requester_name, user_id, chat_id
    )
    await update.message.delete()
    try:
        await update.effective_user.send_message(f"Запрос на {command_type} @{name} отправлен админам.")
    except:
        pass

# ==================== КОМАНДЫ ====================
async def cmd_start(update, context):
    if not await is_command_from_real_admin(update, context): return
    try:
        await update.effective_user.send_message("Бот-модератор активирован. Используйте /help.")
    except:
        pass

async def cmd_id(update, context):
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        text = f"ID: {target.id}"
    else:
        text = f"Ваш ID: {update.effective_user.id}"
    await update.message.reply_text(text)

async def cmd_help(update, context):
    if not await is_command_from_real_admin(update, context): return
    help_text = (
        "Команды:\n"
        "/rules - правила группы\n"
        "/mute [user] [время] - мут\n"
        "/unmute [user] - снять мут\n"
        "/ban [user] [время] - бан\n"
        "/unban [user] - снять бан\n"
        "/warn [user] - предупреждение\n"
        "/unwarn [user] - снять предупреждения\n"
        "/id - показать ID\n"
        "/kick [user] - кикнуть\n\n"
        "Время: 10s, 5m, 2h, 1d, 0 - навсегда"
    )
    try:
        await context.bot.send_message(chat_id=update.effective_user.id, text=help_text)
    except:
        pass

async def cmd_rules(update, context):
    await update.message.reply_text(
        "ПРАВИЛА ГРУППЫ\n\n"
        "1. Без оскорблений и провокаций\n"
        "2. Без 18+ и насилия\n"
        "3. Не флудим/не спамим\n"
        "4. Не сливаем личные данные\n\n"
        "Правила могут изменяться и дополняться."
    )

# ==================== ОБРАБОТЧИК КНОПОК ====================
async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    logger.info(f"Callback: {query.data}")

    data = query.data
    admin_id = query.from_user.id

    if admin_id not in ADMIN_IDS:
        await query.edit_message_text("У вас нет прав.")
        return

    if not data.startswith(('a_', 'd_')):
        await query.edit_message_text("Неизвестная команда.")
        return

    action = data[0]
    request_id = data[2:]

    pending = context.bot_data.get('pending_commands', {})
    if request_id not in pending:
        await query.edit_message_text("Запрос устарел.")
        return

    cmd_data = pending.pop(request_id)
    command_type = cmd_data["command_type"]
    target_id = cmd_data["target_id"]
    chat_id = cmd_data["chat_id"]
    duration = cmd_data["duration"]
    requester_id = cmd_data["requester_id"]
    message_ids = cmd_data.get("message_ids", {})

    try:
        if action == 'a':
            await process_approved_action(context, command_type, target_id, chat_id, duration)
            result_text = f"Команда {command_type} выполнена."
            notify_text = f"Ваш запрос на {command_type} выполнен."
        else:
            result_text = f"Команда {command_type} отклонена."
            notify_text = f"Ваш запрос на {command_type} отклонен."

        await query.edit_message_text(result_text)

        try:
            await context.bot.send_message(chat_id=requester_id, text=notify_text)
        except:
            pass

        for other_admin, msg_id in message_ids.items():
            if other_admin != admin_id:
                try:
                    await context.bot.edit_message_text(
                        chat_id=other_admin,
                        message_id=msg_id,
                        text=f"Запрос обработан.\n\n{result_text}"
                    )
                except:
                    pass
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        await query.edit_message_text(f"Ошибка: {e}")

# ==================== ОТКРЕПЛЕНИЕ (ИСПРАВЛЕНО) ====================
async def handle_pinned_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Открепляет сообщения из канала в группе обсуждений"""
    
    # Проверяем, есть ли закреплённое сообщение
    if not update.message or not update.message.pinned_message:
        return
    
    chat_id = update.effective_chat.id
    pinned = update.message.pinned_message
    
    # Если это сообщение уже открепляли — пропускаем
    pinned_messages = get_pinned_messages(context)
    if pinned_messages.get(chat_id) == pinned.message_id:
        return
    
    # Проверяем, пришло ли сообщение из канала
    is_from_channel = False
    
    # Способ 1: через sender_chat
    if pinned.sender_chat and pinned.sender_chat.type == "channel":
        is_from_channel = True
    
    # Способ 2: через is_automatic_forward
    if hasattr(pinned, 'is_automatic_forward') and pinned.is_automatic_forward:
        is_from_channel = True
    
    # Способ 3: проверяем, есть ли у сообщения author_signature (обычно у каналов)
    if hasattr(pinned, 'author_signature') and pinned.author_signature:
        is_from_channel = True
    
    if is_from_channel:
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=pinned.message_id)
            pinned_messages[chat_id] = pinned.message_id
            logger.info(f"Откреплено сообщение из канала: {pinned.message_id}")
        except Exception as e:
            logger.error(f"Ошибка открепления: {e}")

# ==================== JOBQUEUE ====================
async def cleanup_pending_commands(context):
    pending = context.bot_data.get('pending_commands', {})
    now = time.time()
    to_remove = []
    for req_id, data in pending.items():
        if now - data.get('created_at', 0) > 600:
            to_remove.append(req_id)
    for req_id in to_remove:
        del pending[req_id]
    if to_remove:
        logger.info(f"Удалено {len(to_remove)} просроченных запросов")

async def unpin_channel_posts(context):
    chat_id = DISCUSSION_CHAT_ID
    try:
        chat = await context.bot.get_chat(chat_id)
        if chat.pinned_message:
            pinned = chat.pinned_message
            is_from_channel = (
                pinned.sender_chat and pinned.sender_chat.type == "channel"
            ) or getattr(pinned, 'is_automatic_forward', False)
            if is_from_channel:
                await context.bot.unpin_chat_message(chat_id, message_id=pinned.message_id)
                logger.info(f"JobQueue открепил сообщение из канала: {pinned.message_id}")
    except Exception as e:
        logger.error(f"JobQueue ошибка открепления: {e}")

# ==================== ОСНОВНАЯ ОБРАБОТКА ====================
async def handle_message(update, context):
    if not update.message or not update.effective_user or update.effective_user.is_bot:
        return

    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if await is_command_from_real_admin(update, context):
        return

    if is_muted(context, user_id, chat_id):
        try:
            await update.message.delete()
        except:
            pass
        return

    text = update.message.text or update.message.caption or ""

    if detect_link(text):
        await update.message.delete()
        await send_with_counter(context, chat_id, f"@{update.effective_user.username or update.effective_user.first_name}, ссылки запрещены.\nПравила: /rules")
        return

    if detect_phone(text):
        await update.message.delete()
        await send_with_counter(context, chat_id, f"@{update.effective_user.username or update.effective_user.first_name}, номера телефонов запрещены.\nПравила: /rules")
        return

    if detect_email(text):
        await update.message.delete()
        await send_with_counter(context, chat_id, f"@{update.effective_user.username or update.effective_user.first_name}, email-адреса запрещены.\nПравила: /rules")
        return

    if update.message.text and check_flood(context, user_id, chat_id):
        duration = FLOOD_MUTE_DURATION
        set_muted(context, user_id, chat_id, duration)
        await update.message.delete()
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + duration)
        name = update.effective_user.username or update.effective_user.first_name
        await send_with_counter(context, chat_id, f"@{name} замучен на 5 минут за флуд.\nПравила: /rules")
        db.reset_all_warns(user_id, chat_id)
        return

    clean = clean_text(text)

    if contains_word(clean, INSULTS):
        await update.message.delete()
        new_count = db.add_warn(user_id, chat_id, "insult")
        name = update.effective_user.username or update.effective_user.first_name
        await send_with_counter(context, chat_id, f"@{name} получил предупреждение ({new_count}).")
        if new_count >= 3:
            set_muted(context, user_id, chat_id, 3600)
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + 3600)
            await send_with_counter(context, chat_id, f"@{name} замучен на 1 час за оскорбления.\nПравила: /rules")
            db.reset_all_warns(user_id, chat_id)
        return

    if contains_word(clean, ADULT_WORDS):
        await update.message.delete()
        adult_count = db.add_warn(user_id, chat_id, "adult")
        name = update.effective_user.username or update.effective_user.first_name
        if adult_count == 1:
            await send_with_counter(context, chat_id, f"@{name}, предупреждение за 18+ контент. В следующий раз — бан.\nПравила: /rules")
        else:
            await context.bot.ban_chat_member(chat_id, user_id)
            await send_with_counter(context, chat_id, f"@{name} забанен за 18+ контент (повторное нарушение).\nПравила: /rules")
            db.reset_all_warns(user_id, chat_id)
        return

# ==================== ПОДСКАЗКИ ====================
async def set_commands(app):
    commands = [
        BotCommand("rules", "Правила группы"),
        BotCommand("mute", "Мут пользователя"),
        BotCommand("unmute", "Снять мут"),
        BotCommand("ban", "Забанить пользователя"),
        BotCommand("unban", "Снять бан"),
        BotCommand("warn", "Предупреждение"),
        BotCommand("unwarn", "Снять предупреждения"),
        BotCommand("id", "Показать ID"),
        BotCommand("kick", "Кикнуть пользователя"),
    ]
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllGroupChats())
    await app.bot.set_my_commands(commands, scope=BotCommandScopeAllPrivateChats())

# ==================== ОЧИСТКА ПАМЯТИ ====================
async def cleanup_memory(context):
    now = time.time()
    muted = get_muted_users(context)
    for key in list(muted.keys()):
        if muted[key] < now:
            muted.pop(key, None)
    messages = get_user_messages(context)
    if len(messages) > 100:
        messages.clear()

# ==================== ЗАПУСК ====================
async def post_init(app):
    await set_commands(app)
    if app.job_queue:
        app.job_queue.run_repeating(cleanup_pending_commands, interval=60, first=10)
        app.job_queue.run_repeating(unpin_channel_posts, interval=8, first=5)
        app.job_queue.run_repeating(cleanup_memory, interval=300)

def main():
    persistence = PicklePersistence(filepath="bot_data.pickle")
    app = Application.builder().token(TOKEN).persistence(persistence).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("rules", cmd_rules))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("mute", lambda u,c: handle_command_with_approval(u,c,"mute")))
    app.add_handler(CommandHandler("unmute", lambda u,c: handle_command_with_approval(u,c,"unmute")))
    app.add_handler(CommandHandler("ban", lambda u,c: handle_command_with_approval(u,c,"ban")))
    app.add_handler(CommandHandler("unban", lambda u,c: handle_command_with_approval(u,c,"unban")))
    app.add_handler(CommandHandler("warn", lambda u,c: handle_command_with_approval(u,c,"warn")))
    app.add_handler(CommandHandler("unwarn", lambda u,c: handle_command_with_approval(u,c,"unwarn")))
    app.add_handler(CommandHandler("kick", lambda u,c: handle_command_with_approval(u,c,"kick")))

    app.add_handler(CallbackQueryHandler(handle_callback))

    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pinned_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message))

    print("Бот запущен (оптимизированный + память + сохранение + JobQueue)!")
    app.run_polling(
        allowed_updates=["message", "callback_query", "pinned_message", "chat_member"],
        drop_pending_updates=True,
        poll_interval=0.5
    )

if __name__ == "__main__":
    main()
