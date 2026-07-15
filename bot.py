import logging
import re
import time
import asyncio
from collections import defaultdict, deque
import sqlite3
from telegram import (
    Update, ChatPermissions, BotCommand, InlineKeyboardButton,
    InlineKeyboardMarkup, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, CallbackQueryHandler, PicklePersistence
)
from telegram.constants import ParseMode

logging.basicConfig(level=logging.WARNING, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = "8637462837:AAFygcu0eLNbXwhOMRPwuDwiry_bx8ij5KM"
ADMIN_IDS = {5460879396, 8176145729, 1087968824}

FLOOD_LIMIT = 8
FLOOD_TIME = 15
FLOOD_MUTE_DURATION = 300
ADMIN_MENTION = "Если заметите баги, пишите @yabrad"

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
                    user_id INTEGER,
                    chat_id INTEGER,
                    category TEXT,
                    count INTEGER,
                    PRIMARY KEY (user_id, chat_id, category)
                )
            ''')
            conn.execute("CREATE INDEX IF NOT EXISTS idx_warns_user ON warns(user_id, chat_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_warns_cat ON warns(category)")

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

muted_users = {}
user_messages = defaultdict(lambda: deque(maxlen=FLOOD_LIMIT))
pinned_messages = {}
MESSAGE_COUNTER = 0
pending_commands = {}

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
    if not text:
        return ""
    text = re.sub(r'[^а-яёa-z0-9]', '', text.lower())
    trans = str.maketrans('abcdefghijklmnopqrstuvwxyz', 'аацддефгхийкллмннопкпрсстувшксывз')
    return text.translate(trans)

def contains_word(text: str, word_set: set) -> bool:
    cleaned = clean_text(text)
    return any(clean_text(w) in cleaned for w in word_set)

def detect_link(text: str) -> bool:
    return any(p.search(text) for p in LINK_PATTERNS) if text else False

def detect_phone(text: str) -> bool:
    if not text:
        return False
    return any(p.search(w) for w in text.split() for p in PHONE_PATTERNS)

def detect_email(text: str) -> bool:
    return bool(EMAIL_PATTERN.search(text) if text else False)

def parse_time(time_str: str) -> int:
    if not time_str:
        return 0
    time_str = time_str.lower()
    if time_str.isdigit():
        return int(time_str) * 60
    m = re.match(r'(\d+)([smhd])', time_str)
    if m:
        v, u = int(m.group(1)), m.group(2)
        return v * {"s": 1, "m": 60, "h": 3600, "d": 86400}[u]
    return 0

def format_duration(seconds: int) -> str:
    if seconds == 0:
        return "навсегда"
    for div, unit in [(86400, "дней"), (3600, "часов"), (60, "минут")]:
        if seconds >= div:
            return f"{seconds // div} {unit}"
    return f"{seconds} секунд"

# ==================== ПРОВЕРКА АДМИНА ====================

async def is_command_from_real_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.message or not update.effective_user:
        return False
    chat_id = update.effective_chat.id
    user = update.effective_user

    if user.id in ADMIN_IDS:
        return True

    if update.message.sender_chat and update.message.sender_chat.id == chat_id:
        try:
            admins = await context.bot.get_chat_administrators(chat_id)
            return any(a.user.id == user.id for a in admins)
        except:
            return True
    return False

async def is_admin_in_chat(context, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except:
        return False

# ==================== МУТ И ФЛУД ====================

def is_muted(user_id: int, chat_id: int) -> bool:
    key = f"{user_id}_{chat_id}"
    if key in muted_users and muted_users[key] > time.time():
        return True
    muted_users.pop(key, None)
    return False

def set_muted(user_id: int, chat_id: int, duration: int):
    muted_users[f"{user_id}_{chat_id}"] = time.time() + (duration or 31536000)

def remove_mute(user_id, chat_id):
    muted_users.pop(f"{user_id}_{chat_id}", None)

def check_flood(user_id, chat_id):
    now = time.time()
    user_messages[user_id].append(now)
    user_messages[user_id] = [t for t in user_messages[user_id] if now - t <= FLOOD_TIME]
    if len(user_messages[user_id]) > FLOOD_LIMIT:
        user_messages[user_id] = []
        return True
    return False

# ==================== ОТПРАВКА ====================

async def send_with_counter(context, chat_id: int, text: str):
    global MESSAGE_COUNTER
    MESSAGE_COUNTER += 1
    await context.bot.send_message(chat_id=chat_id, text=text)
    if MESSAGE_COUNTER % 4 == 0:
        await context.bot.send_message(chat_id=chat_id, text=ADMIN_MENTION)

async def send_approval_request(context, admin_ids, command_type, target_name, target_id, duration, duration_text, requester_name, requester_id, chat_id):
    request_id = f"{int(time.time())}_{requester_id}_{target_id}"
    
    pending_commands[request_id] = {
        "command_type": command_type,
        "target_id": target_id,
        "chat_id": chat_id,
        "duration": duration,
        "requester_id": requester_id,
        "message_ids": {}
    }
    
    keyboard = [[
        InlineKeyboardButton("Разрешить", callback_data=f"a_{request_id}"),
        InlineKeyboardButton("Запретить", callback_data=f"d_{request_id}")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    text = (
        f"ЗАПРОС НА {command_type.upper()}\n\n"
        f"Кто запросил: @{requester_name} (ID: {requester_id})\n"
        f"Кого: @{target_name} (ID: {target_id})\n"
        f"Время: {duration_text}\n"
        f"Чат: {chat_id}"
    )
    
    for admin_id in admin_ids:
        try:
            msg = await context.bot.send_message(chat_id=admin_id, text=text, reply_markup=reply_markup)
            pending_commands[request_id]["message_ids"][admin_id] = msg.message_id
        except:
            pass

# ==================== ВЫПОЛНЕНИЕ КОМАНД ====================

async def process_approved_action(context, command_type: str, target_id: int, chat_id: int, duration: int = 0):
    if command_type == "mute":
        dur = duration or 31536000
        set_muted(target_id, chat_id, dur)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + dur)
    elif command_type == "unmute":
        remove_mute(target_id, chat_id)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=True))
    elif command_type == "ban":
        await context.bot.ban_chat_member(chat_id, target_id, until_date=int(time.time()) + duration if duration else None)
    elif command_type == "unban":
        await context.bot.unban_chat_member(chat_id, target_id)
    elif command_type == "kick":
        await context.bot.ban_chat_member(chat_id, target_id)
        await context.bot.unban_chat_member(chat_id, target_id)
    elif command_type == "warn":
        db.add_warn(target_id, chat_id, "insult")
    elif command_type == "unwarn":
        db.reset_all_warns(target_id, chat_id)
    
    db.reset_all_warns(target_id, chat_id)

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

async def handle_command_with_approval(update, context, command_type: str):
    if not update.message:
        return
    
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    
    target, target_id = await get_target(update, context)
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    
    name = target.username or target.first_name
    duration = parse_time(update.message.text)
    duration_text = format_duration(duration) if duration > 0 else "навсегда"
    
    if await is_command_from_real_admin(update, context):
        await process_approved_action(context, command_type, target_id, chat_id, duration)
        await update.message.delete()
        await update.effective_user.send_message(f"Команда {command_type} выполнена.")
        return
    
    requester_name = update.effective_user.username or update.effective_user.first_name
    await send_approval_request(
        context, list(ADMIN_IDS), command_type, name, target_id, duration, duration_text,
        requester_name, user_id, chat_id
    )
    await update.message.delete()
    await update.effective_user.send_message(f"Запрос на {command_type} @{name} отправлен админам.")

# ==================== КОМАНДЫ ====================

async def cmd_start(update, context):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    await update.effective_user.send_message("Бот-модератор активирован. Используйте /help.")

async def cmd_id(update, context):
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        text = f"ID: {target.id}"
    else:
        text = f"Ваш ID: {update.effective_user.id}"
    await update.message.reply_text(text)

async def cmd_help(update, context):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    await update.effective_user.send_message(
        "Команды:\n/rules - правила\n/mute [user] [время] - мут\n/unmute [user] - снять мут\n/ban [user] [время] - бан\n/unban [user] - снять бан\n/warn [user] - предупреждение\n/unwarn [user] - снять предупреждения\n/id - показать ID\n/kick [user] - кикнуть\n\nВремя: 10s, 5m, 2h, 1d, 0 - навсегда"
    )

async def cmd_rules(update, context):
    await update.message.reply_text(
        "ПРАВИЛА ГРУППЫ\n\n1. Без оскорблений и провокаций\n2. Без 18+ и насилия\n3. Не флудим/не спамим\n4. Не сливаем личные данные\n\nПравила могут изменяться и дополняться."
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
    
    if request_id not in pending_commands:
        await query.edit_message_text("Запрос устарел.")
        return
    
    cmd_data = pending_commands.pop(request_id)
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

# ==================== НОВЫЙ УЧАСТНИК ====================

async def handle_new_member(update, context):
    for member in update.message.new_chat_members:
        user_id = member.id
        chat_id = update.effective_chat.id
        try:
            await context.bot.set_chat_administrator_custom_title(chat_id, user_id, "бибизяна")
        except:
            pass
        try:
            await context.bot.restrict_chat_member(
                chat_id, user_id,
                permissions=ChatPermissions(
                    can_send_messages=True, can_send_media_messages=True,
                    can_send_other_messages=True, can_add_web_page_previews=True,
                    can_send_polls=False, can_send_audios=False, can_send_documents=False
                )
            )
        except:
            pass

# ==================== ОТКРЕПЛЕНИЕ ====================

async def handle_pinned_message(update, context):
    message = update.message or update.edited_message
    if not message or not message.pinned_message:
        return

    chat_id = message.chat.id
    pinned = message.pinned_message

    if pinned_messages.get(chat_id) == pinned.message_id:
        return

    is_from_channel = (
        pinned.sender_chat and 
        pinned.sender_chat.type == "channel"
    ) or getattr(pinned, 'is_automatic_forward', False)

    if is_from_channel:
        try:
            await context.bot.unpin_chat_message(chat_id=chat_id, message_id=pinned.message_id)
            pinned_messages[chat_id] = pinned.message_id
            logger.info(f"Откреплено авто-сообщение из канала {pinned.message_id}")
        except Exception as e:
            logger.error(f"Ошибка открепления: {e}")

# ==================== ОЧИСТКА ПАМЯТИ (JOBQUEUE) ====================

async def cleanup_memory(context: ContextTypes.DEFAULT_TYPE):
    """Очищает старые записи для экономии памяти"""
    # Очищаем старые мут-записи (которые уже истекли)
    now = time.time()
    expired_keys = [k for k, v in muted_users.items() if v < now]
    for k in expired_keys:
        muted_users.pop(k, None)
    
    # Очищаем историю сообщений, если она слишком большая
    if len(user_messages) > 100:
        user_messages.clear()
    
    logger.info(f"Память очищена. Мутов: {len(muted_users)}, очередей: {len(user_messages)}")

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

# ==================== ОСНОВНАЯ ОБРАБОТКА СООБЩЕНИЙ ====================

async def handle_message(update, context):
    if not update.message or not update.effective_user or update.effective_user.is_bot:
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if await is_command_from_real_admin(update, context):
        return
    
    if is_muted(user_id, chat_id):
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
    
    if update.message.text and check_flood(user_id, chat_id):
        duration = FLOOD_MUTE_DURATION
        set_muted(user_id, chat_id, duration)
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
            set_muted(user_id, chat_id, 3600)
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

# ==================== ЗАПУСК ====================

def main():
    # PicklePersistence для сохранения данных при перезапуске
    persistence = PicklePersistence(filepath="bot_data.pickle")
    
    app = Application.builder().token(TOKEN).persistence(persistence).build()
    
    # JobQueue для очистки памяти
    app.job_queue.run_repeating(cleanup_memory, interval=300)  # раз в 5 минут
    
    # Команды
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
    
    # Callback
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    # Message handlers
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
    app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pinned_message))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message))
    
    # Устанавливаем команды
    app.post_init = set_commands
    
    print("Бот запущен (оптимизированный + память + сохранение)!")
    app.run_polling(
        allowed_updates=["message", "callback_query", "pinned_message", "chat_member"],
        drop_pending_updates=True,
        poll_interval=0.5
    )

if __name__ == "__main__":
    main()
