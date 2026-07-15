import logging
import re
import time
import asyncio
import sqlite3
from collections import defaultdict
from telegram import Update, ChatPermissions, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = "8637462837:AAFygcu0eLNbXwhOMRPwuDwiry_bx8ij5KM"

ADMIN_IDS = [5460879396, 8176145729, 1087968824]

FLOOD_LIMIT = 8
FLOOD_TIME = 15
FLOOD_MUTE_DURATION = 300

MESSAGE_COUNTER = 0
ADMIN_MENTION = "Если заметите баги или ошибки, просьба написать админу о них @yabrad"

# ==================== БАЗА ДАННЫХ ====================

class Database:
    def __init__(self, db_path="warns.db"):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS warns (
                    user_id INTEGER,
                    chat_id INTEGER,
                    category TEXT,
                    count INTEGER,
                    PRIMARY KEY (user_id, chat_id, category)
                )
            ''')
    
    def get_warn(self, user_id, chat_id, category):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                'SELECT count FROM warns WHERE user_id=? AND chat_id=? AND category=?',
                (user_id, chat_id, category)
            )
            result = cur.fetchone()
            return result[0] if result else 0
    
    def add_warn(self, user_id, chat_id, category):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.execute(
                'SELECT count FROM warns WHERE user_id=? AND chat_id=? AND category=?',
                (user_id, chat_id, category)
            )
            current = cur.fetchone()
            new_count = (current[0] if current else 0) + 1
            conn.execute(
                'INSERT OR REPLACE INTO warns (user_id, chat_id, category, count) VALUES (?, ?, ?, ?)',
                (user_id, chat_id, category, new_count)
            )
            return new_count
    
    def reset_warns(self, user_id, chat_id, category):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'DELETE FROM warns WHERE user_id=? AND chat_id=? AND category=?',
                (user_id, chat_id, category)
            )
    
    def reset_all_warns(self, user_id, chat_id):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                'DELETE FROM warns WHERE user_id=? AND chat_id=?',
                (user_id, chat_id)
            )

db = Database()

# ==================== ХРАНИЛИЩА ====================

muted_users = {}
user_messages = defaultdict(list)
pinned_messages = {}

# ==================== КОМПИЛИРОВАННЫЕ РЕГУЛЯРКИ ====================

LINK_PATTERNS = [
    re.compile(r'https?://[^\s]+', re.IGNORECASE),
    re.compile(r'www\.[^\s]+', re.IGNORECASE),
    re.compile(r't\.me/[^\s]+', re.IGNORECASE),
    re.compile(r'telegram\.org/[^\s]+', re.IGNORECASE),
    re.compile(r'bit\.ly/[^\s]+', re.IGNORECASE),
    re.compile(r'clck\.ru/[^\s]+', re.IGNORECASE),
    re.compile(r'[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}/?', re.IGNORECASE),
]

PHONE_PATTERNS = [
    re.compile(r'^\+\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,4}[\s\-]?\d{1,4}'),
    re.compile(r'^\+\d[\d\-]{8,}\d'),
    re.compile(r'^\+\d{1,3}\s?\d{1,4}\s?\d{1,4}\s?\d{1,4}'),
    re.compile(r'^8\s?\(?\d{3}\)?\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'^8-?\(?\d{3}\)-?\d{3}-?\d{2}-?\d{2}'),
    re.compile(r'^\+7\s?\(?\d{3}\)?\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'^7\s?\(?\d{3}\)?\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'^\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'),
    re.compile(r'^\+7\s?\d{3}\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'^7\s?\d{3}\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'^\+?[0-9]{10,15}$'),
]

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)

INSULTS = {
    "даун", "олигофрен", "дегенерат", "слабоумный", "конченый", "конченая",
    "клоун", "ебалай", "еблашка", "ебло", "ебало", "соси", "сосал", "сосет",
    "отсоси", "сосун", "съеби", "съебал", "тварь", "тварьебаная", "сукаебаная"
}

ADULT_WORDS = {
    "порно", "секс", "насилие", "изнасилование", "педофил", "педофилия",
    "зоофил", "зоофилия", "сатанизм", "расчленение", "насильник", "педофильский"
}

# ==================== БЫСТРЫЕ ФУНКЦИИ ====================

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[^а-яёa-z0-9]', '', text)
    for lat, rus in [('a','а'),('b','б'),('c','ц'),('d','д'),('e','е'),('f','ф'),
                     ('g','г'),('h','х'),('i','и'),('j','й'),('k','к'),('l','л'),
                     ('m','м'),('n','н'),('o','о'),('p','п'),('q','к'),('r','р'),
                     ('s','с'),('t','т'),('u','у'),('v','в'),('w','ш'),('x','кс'),
                     ('y','ы'),('z','з')]:
        text = text.replace(lat, rus)
    return text

def contains_word(text: str, word_set: set) -> bool:
    if not text:
        return False
    cleaned = clean_text(text)
    for word in word_set:
        if clean_text(word) in cleaned:
            return True
    return False

def detect_link(text: str) -> bool:
    if not text:
        return False
    for pattern in LINK_PATTERNS:
        if pattern.search(text):
            return True
    return False

def detect_phone(text: str) -> bool:
    if not text:
        return False
    words = text.split()
    for word in words:
        for pattern in PHONE_PATTERNS:
            if pattern.search(word):
                return True
    return False

def detect_email(text: str) -> bool:
    if not text:
        return False
    return bool(EMAIL_PATTERN.search(text))

def parse_time(time_str: str) -> int:
    if not time_str:
        return 0
    time_str = time_str.lower()
    if time_str.isdigit():
        return int(time_str) * 60
    match = re.match(r'(\d+)([smhd])', time_str)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit == 's':
            return value
        elif unit == 'm':
            return value * 60
        elif unit == 'h':
            return value * 3600
        elif unit == 'd':
            return value * 86400
    return 0

def format_duration(seconds: int) -> str:
    if seconds == 0:
        return "навсегда"
    elif seconds < 60:
        return f"{seconds} секунд"
    elif seconds < 3600:
        return f"{seconds // 60} минут"
    elif seconds < 86400:
        return f"{seconds // 3600} часов"
    else:
        return f"{seconds // 86400} дней"

# ==================== ДИНАМИЧЕСКАЯ ПРОВЕРКА АДМИНА ====================

async def is_admin_in_chat(context, chat_id: int, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in ["administrator", "creator"]
    except:
        return False

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def is_muted(user_id, chat_id):
    key = f"{user_id}_{chat_id}"
    if key in muted_users:
        if muted_users[key] > time.time():
            return True
        del muted_users[key]
    return False

def set_muted(user_id, chat_id, duration):
    muted_users[f"{user_id}_{chat_id}"] = int(time.time()) + duration

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

async def delete_after_delay(context, chat_id, message_id, delay=1):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

async def send_admin_log(context, text):
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except:
            pass

# ==================== ФУНКЦИЯ ОТПРАВКИ С СЧЁТЧИКОМ ====================

async def send_with_counter(context, chat_id, text):
    global MESSAGE_COUNTER
    MESSAGE_COUNTER += 1
    await context.bot.send_message(chat_id=chat_id, text=text)
    
    if MESSAGE_COUNTER % 4 == 0:
        await context.bot.send_message(chat_id=chat_id, text=ADMIN_MENTION)

# ==================== ОТПРАВКА ЗАПРОСА АДМИНАМ ====================

async def send_approval_request(context, admin_ids, command_type, target_name, target_id, duration, duration_text, requester_name, requester_id, chat_id):
    request_id = f"{int(time.time())}_{requester_id}_{target_id}"
    
    if 'pending_commands' not in context.bot_data:
        context.bot_data['pending_commands'] = {}
    
    context.bot_data['pending_commands'][request_id] = {
        "command_type": command_type,
        "target_id": target_id,
        "chat_id": chat_id,
        "duration": duration,
        "requester_id": requester_id,
        "message_ids": {}
    }
    
    keyboard = [
        [
            InlineKeyboardButton("Разрешить", callback_data=f"a_{request_id}"),
            InlineKeyboardButton("Запретить", callback_data=f"d_{request_id}")
        ]
    ]
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
            msg = await context.bot.send_message(
                chat_id=admin_id,
                text=text,
                reply_markup=reply_markup
            )
            context.bot_data['pending_commands'][request_id]["message_ids"][admin_id] = msg.message_id
        except:
            pass

# ==================== ВСПОМОГАТЕЛЬНАЯ ФУНКЦИЯ ДЛЯ ВЫПОЛНЕНИЯ ====================

async def process_approved_action(context, command_type, target_id, chat_id, duration):
    if command_type == "mute":
        dur = duration if duration > 0 else 31536000
        set_muted(target_id, chat_id, dur)
        await context.bot.restrict_chat_member(
            chat_id, target_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + dur
        )
        await send_with_counter(context, chat_id, f"Пользователь замучен.\nПравила: /rules")
        db.reset_all_warns(target_id, chat_id)
        
    elif command_type == "unmute":
        remove_mute(target_id, chat_id)
        await context.bot.restrict_chat_member(chat_id, target_id, permissions=ChatPermissions(can_send_messages=True))
        await send_with_counter(context, chat_id, f"Мут снят.")
        
    elif command_type == "ban":
        dur = duration if duration > 0 else None
        await context.bot.ban_chat_member(chat_id, target_id, until_date=int(time.time()) + duration if duration > 0 else None)
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
        new_count = db.add_warn(target_id, chat_id, "insult")
        await send_with_counter(context, chat_id, f"Пользователь получил предупреждение ({new_count}).")
        
    elif command_type == "unwarn":
        db.reset_all_warns(target_id, chat_id)
        await send_with_counter(context, chat_id, f"Предупреждения сняты.")

# ==================== ОБЩАЯ ФУНКЦИЯ ДЛЯ КОМАНД ====================

async def handle_command_with_approval(update, context, command_type):
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    target, target_id = await get_target(update, context)
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    
    name = target.username or target.first_name
    
    if await is_admin_in_chat(context, chat_id, user_id):
        duration = parse_time(update.message.text)
        await process_approved_action(context, command_type, target_id, chat_id, duration)
        await update.message.delete()
        await update.effective_user.send_message(f"Команда {command_type} выполнена.")
        return
    
    duration = parse_time(update.message.text)
    duration_text = format_duration(duration) if duration > 0 else "навсегда"
    requester_name = update.effective_user.username or update.effective_user.first_name
    
    await send_approval_request(
        context, ADMIN_IDS, command_type, name, target_id, duration, duration_text,
        requester_name, user_id, chat_id
    )
    
    await update.message.delete()
    await update.effective_user.send_message(f"Запрос на {command_type} @{name} отправлен админам.")

# ==================== КОМАНДЫ ====================

async def cmd_start(update, context):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    await update.effective_user.send_message("Бот-модератор активирован. Используйте /help для списка команд.")

async def cmd_id(update, context):
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        text = f"ID пользователя @{target.username or target.first_name}: {target.id}"
    else:
        text = f"Ваш ID: {update.effective_user.id}"
    await update.message.reply_text(text)

async def cmd_help(update, context):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return
    await update.effective_user.send_message(
        "Доступные команды:\n"
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

async def cmd_rules(update, context):
    rules_text = """ПРАВИЛА ГРУППЫ

1. Без оскорблений и провокаций
2. Без 18+ и насилия
3. Не флудим/не спамим
4. Не сливаем личные данные

Правила могут изменяться и дополняться."""
    await update.message.reply_text(rules_text)

async def get_target(update, context):
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        return target, target.id
    if context.args:
        for arg in context.args:
            if arg.startswith('@'):
                try:
                    user = await context.bot.get_chat(arg)
                    return user, user.id
                except:
                    return None, None
            elif arg.isdigit():
                try:
                    user = await context.bot.get_chat(int(arg))
                    return user, int(arg)
                except:
                    return None, None
    return None, None

async def cmd_mute(update, context):
    await handle_command_with_approval(update, context, "mute")

async def cmd_unmute(update, context):
    await handle_command_with_approval(update, context, "unmute")

async def cmd_ban(update, context):
    await handle_command_with_approval(update, context, "ban")

async def cmd_unban(update, context):
    await handle_command_with_approval(update, context, "unban")

async def cmd_kick(update, context):
    await handle_command_with_approval(update, context, "kick")

async def cmd_warn(update, context):
    await handle_command_with_approval(update, context, "warn")

async def cmd_unwarn(update, context):
    await handle_command_with_approval(update, context, "unwarn")

# ==================== ОБРАБОТЧИК КНОПОК ====================

async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    
    logger.info(f"Callback received: {query.data} from user {query.from_user.id}")
    
    data = query.data
    admin_id = query.from_user.id
    
    if admin_id not in ADMIN_IDS:
        await query.edit_message_text("У вас нет прав.")
        return
    
    if not data.startswith('a_') and not data.startswith('d_'):
        await query.edit_message_text("Неизвестная команда.")
        return
    
    action = data[0]
    request_id = data[2:]
    
    pending = context.bot_data.get('pending_commands', {})
    if request_id not in pending:
        await query.edit_message_text("Запрос устарел или уже обработан.")
        return
    
    cmd_data = pending[request_id]
    command_type = cmd_data["command_type"]
    target_id = cmd_data["target_id"]
    chat_id = cmd_data["chat_id"]
    duration = cmd_data["duration"]
    requester_id = cmd_data["requester_id"]
    message_ids = cmd_data.get("message_ids", {})
    
    del context.bot_data['pending_commands'][request_id]
    
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
        logger.error(f"Ошибка при обработке callback: {e}")
        await query.edit_message_text(f"Ошибка: {e}")

# ==================== ОБРАБОТЧИК НОВЫХ УЧАСТНИКОВ ====================

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
    if not update.message or not update.message.pinned_message:
        return
    
    chat_id = update.effective_chat.id
    pinned_msg = update.message.pinned_message
    
    if pinned_messages.get(chat_id) == pinned_msg.message_id:
        return
    
    if pinned_msg.sender_chat and pinned_msg.sender_chat.type in ["channel", "supergroup"]:
        try:
            await context.bot.unpin_chat_message(chat_id, message_id=pinned_msg.message_id)
            pinned_messages[chat_id] = pinned_msg.message_id
            logger.info(f"Авто-откреплено сообщение из канала: {pinned_msg.message_id}")
        except Exception as e:
            logger.error(f"Не удалось открепить: {e}")

# ==================== ОСНОВНАЯ ОБРАБОТКА ====================

async def handle_message(update, context):
    if not update.message or not update.effective_user or update.effective_user.is_bot:
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if await is_admin_in_chat(context, chat_id, user_id):
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
        await send_admin_log(context, f"AUTO MUTE - FLOOD\nTarget: @{name}")
        return
    
    clean = clean_text(text)
    
    if contains_word(clean, INSULTS):
        await update.message.delete()
        new_count = db.add_warn(user_id, chat_id, "insult")
        name = update.effective_user.username or update.effective_user.first_name
        await send_with_counter(context, chat_id, f"@{name} получил предупреждение ({new_count}).")
        await send_admin_log(context, f"AUTO WARN\nTarget: @{name}\nCount: {new_count}")
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
            await send_admin_log(context, f"AUTO WARN - 18+\nTarget: @{name}")
        else:
            await context.bot.ban_chat_member(chat_id, user_id)
            await send_with_counter(context, chat_id, f"@{name} забанен за 18+ контент (повторное нарушение).\nПравила: /rules")
            db.reset_all_warns(user_id, chat_id)
            await send_admin_log(context, f"AUTO BAN - 18+\nTarget: @{name}")
        return

# ==================== НАСТРОЙКА ПОДСКАЗОК КОМАНД ====================

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

# ==================== ЗАПУСК ====================

app = Application.builder().token(TOKEN).build()

app.add_handler(CommandHandler("start", cmd_start))
app.add_handler(CommandHandler("help", cmd_help))
app.add_handler(CommandHandler("rules", cmd_rules))
app.add_handler(CommandHandler("id", cmd_id))
app.add_handler(CommandHandler("mute", cmd_mute))
app.add_handler(CommandHandler("unmute", cmd_unmute))
app.add_handler(CommandHandler("ban", cmd_ban))
app.add_handler(CommandHandler("unban", cmd_unban))
app.add_handler(CommandHandler("warn", cmd_warn))
app.add_handler(CommandHandler("unwarn", cmd_unwarn))
app.add_handler(CommandHandler("kick", cmd_kick))

app.add_handler(CallbackQueryHandler(handle_callback))

app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pinned_message))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message))

print("Бот запущен!")
app.run_polling(
    allowed_updates=["message", "callback_query", "pinned_message", "chat_member"],
    drop_pending_updates=True
)
