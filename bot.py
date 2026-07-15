import logging
import re
import time
import asyncio
import sqlite3
from collections import defaultdict
from telegram import Update, ChatPermissions, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ==================== НАСТРОЙКИ ====================

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

TOKEN = "8637462837:AAFygcu0eLNbXwhOMRPwuDwiry_bx8ij5KM"

ADMIN_IDS = [5460879396, 8176145729, 1087968824]

FLOOD_LIMIT = 8
FLOOD_TIME = 15
FLOOD_MUTE_DURATION = 300

# ==================== БАЗА ДАННЫХ (ОПТИМИЗИРОВАНА) ====================

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

# ==================== КОМПИЛИРОВАННЫЕ РЕГУЛЯРКИ (БЫСТРЕЕ) ====================

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
    re.compile(r'\+?\d{1,3}[\s\-]?\(?\d{1,4}\)?[\s\-]?\d{1,4}[\s\-]?\d{1,4}[\s\-]?\d{1,4}'),
    re.compile(r'\+?\d[\d\-]{8,}\d'),
    re.compile(r'\+\d{1,3}\s?\d{1,4}\s?\d{1,4}\s?\d{1,4}'),
    re.compile(r'8\s?\(?\d{3}\)?\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'8-?\(?\d{3}\)-?\d{3}-?\d{2}-?\d{2}'),
    re.compile(r'\+7\s?\(?\d{3}\)?\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'7\s?\(?\d{3}\)?\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'\d{3}[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}'),
    re.compile(r'\+7\s?\d{3}\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'7\s?\d{3}\s?\d{3}\s?\d{2}\s?\d{2}'),
    re.compile(r'\+?[0-9]{10,15}'),
]

EMAIL_PATTERN = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', re.IGNORECASE)

# ==================== СПИСКИ СЛОВ (ОПТИМИЗИРОВАНЫ) ====================

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
    # Транслитерация
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
    for pattern in PHONE_PATTERNS:
        if pattern.search(text):
            return True
    return False

def detect_email(text: str) -> bool:
    if not text:
        return False
    return bool(EMAIL_PATTERN.search(text))

# ==================== ФУНКЦИИ ВРЕМЕНИ ====================

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

# ==================== ПРОВЕРКА АДМИНА ====================

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        return True
    if update.effective_chat.type == "private":
        return True
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return member.status in ['administrator', 'creator']
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
    try:
        await context.bot.send_message(chat_id=5460879396, text=text)
    except:
        pass

# ==================== КОМАНДЫ ====================

async def cmd_start(update, context):
    if not await is_admin(update, context):
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
    if not await is_admin(update, context):
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
    await update.message.reply_text(
        "ПРАВИЛА ГРУППЫ\n\n"
        "1. Без оскорблений и провокаций\n"
        "2. Без 18+ и насилия\n"
        "3. Не флудим/не спамим\n"
        "4. Не сливаем личные данные\n"
        "5. Без ссылок и рекламы\n"
        "6. Без номеров телефонов и email"
    )

# ==================== КОМАНДЫ НАКАЗАНИЙ ====================

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
    if not await is_admin(update, context):
        return
    target, user_id = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    duration = parse_time(update.message.text)
    duration_text = format_duration(duration) if duration > 0 else "навсегда"
    set_muted(user_id, chat_id, duration if duration > 0 else 31536000)
    try:
        await context.bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=int(time.time()) + (duration if duration > 0 else 31536000)
        )
        await context.bot.send_message(chat_id, text=f"@{name} замучен на {duration_text}.\nПравила: /rules")
        db.reset_all_warns(user_id, chat_id)
        await send_admin_log(context, f"MUTE\nTarget: @{name}\nDuration: {duration_text}")
    except Exception as e:
        await update.effective_user.send_message(f"Ошибка: {e}")

async def cmd_unmute(update, context):
    if not await is_admin(update, context):
        return
    target, user_id = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    remove_mute(user_id, chat_id)
    try:
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=True))
        await context.bot.send_message(chat_id, text=f"Мут для @{name} снят.\nПравила: /rules")
        await send_admin_log(context, f"UNMUTE\nTarget: @{name}")
    except Exception as e:
        await update.effective_user.send_message(f"Ошибка: {e}")

async def cmd_ban(update, context):
    if not await is_admin(update, context):
        return
    target, user_id = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    duration = parse_time(update.message.text)
    duration_text = format_duration(duration) if duration > 0 else "навсегда"
    try:
        await context.bot.ban_chat_member(chat_id, user_id, until_date=int(time.time()) + duration if duration > 0 else None)
        await context.bot.send_message(chat_id, text=f"@{name} забанен на {duration_text}.\nПравила: /rules")
        db.reset_all_warns(user_id, chat_id)
        await send_admin_log(context, f"BAN\nTarget: @{name}\nDuration: {duration_text}")
    except Exception as e:
        await update.effective_user.send_message(f"Ошибка: {e}")

async def cmd_unban(update, context):
    if not await is_admin(update, context):
        return
    target, user_id = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    try:
        await context.bot.unban_chat_member(chat_id, user_id)
        await context.bot.send_message(chat_id, text=f"Бан для @{name} снят.\nПравила: /rules")
        await send_admin_log(context, f"UNBAN\nTarget: @{name}")
    except Exception as e:
        await update.effective_user.send_message(f"Ошибка: {e}")

async def cmd_kick(update, context):
    if not await is_admin(update, context):
        return
    target, _ = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    try:
        await context.bot.ban_chat_member(chat_id, target.id)
        await context.bot.unban_chat_member(chat_id, target.id)
        await context.bot.send_message(chat_id, text=f"@{name} кикнут.\nПравила: /rules")
        db.reset_all_warns(target.id, chat_id)
        await send_admin_log(context, f"KICK\nTarget: @{name}")
    except Exception as e:
        await update.effective_user.send_message(f"Ошибка: {e}")

async def cmd_warn(update, context):
    if not await is_admin(update, context):
        return
    target, user_id = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    new_count = db.add_warn(user_id, chat_id, "insult")
    await context.bot.send_message(chat_id, text=f"@{name} получил предупреждение ({new_count}).\nПравила: /rules")
    await send_admin_log(context, f"WARN\nTarget: @{name}\nCount: {new_count}")

async def cmd_unwarn(update, context):
    if not await is_admin(update, context):
        return
    target, user_id = await get_target(update, context)
    chat_id = update.effective_chat.id
    if not target:
        await update.effective_user.send_message("Ошибка: ответьте на сообщение или укажите ID.")
        return
    name = target.username or target.first_name
    db.reset_all_warns(user_id, chat_id)
    await context.bot.send_message(chat_id, text=f"Предупреждения для @{name} сняты.\nПравила: /rules")
    await send_admin_log(context, f"CLEAR WARNINGS\nTarget: @{name}")

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
    chat_id = update.effective_chat.id
    try:
        chat = await context.bot.get_chat(chat_id)
        if not chat.pinned_message:
            return
        pinned_msg_id = chat.pinned_message.message_id
    except:
        return
    if pinned_messages.get(chat_id) == pinned_msg_id:
        return
    try:
        await context.bot.unpin_chat_message(chat_id)
        pinned_messages[chat_id] = pinned_msg_id
        if update.effective_user and await is_admin(update, context):
            await update.effective_user.send_message("Сообщение откреплено. Закрепите повторно для постоянного.")
        logger.info(f"Сообщение {pinned_msg_id} откреплено в {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка открепления: {e}")

# ==================== ОСНОВНАЯ ОБРАБОТКА ====================

async def handle_message(update, context):
    if not update.message or not update.effective_user or update.effective_user.is_bot:
        return
    
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id
    
    if await is_admin(update, context):
        return
    
    if is_muted(user_id, chat_id):
        try:
            await update.message.delete()
        except:
            pass
        return
    
    text = update.message.text or update.message.caption or ""
    
    # Быстрые проверки (сначала самые вероятные)
    if detect_link(text):
        await update.message.delete()
        await context.bot.send_message(chat_id, f"@{update.effective_user.username or update.effective_user.first_name}, ссылки запрещены.\nПравила: /rules")
        return
    
    if detect_phone(text):
        await update.message.delete()
        await context.bot.send_message(chat_id, f"@{update.effective_user.username or update.effective_user.first_name}, номера телефонов запрещены.\nПравила: /rules")
        return
    
    if detect_email(text):
        await update.message.delete()
        await context.bot.send_message(chat_id, f"@{update.effective_user.username or update.effective_user.first_name}, email-адреса запрещены.\nПравила: /rules")
        return
    
    # Флуд
    if update.message.text and check_flood(user_id, chat_id):
        duration = FLOOD_MUTE_DURATION
        set_muted(user_id, chat_id, duration)
        await update.message.delete()
        await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + duration)
        name = update.effective_user.username or update.effective_user.first_name
        await context.bot.send_message(chat_id, f"@{name} замучен на 5 минут за флуд.\nПравила: /rules")
        db.reset_all_warns(user_id, chat_id)
        await send_admin_log(context, f"AUTO MUTE - FLOOD\nTarget: @{name}")
        return
    
    clean = clean_text(text)
    
    # Оскорбления
    if contains_word(clean, INSULTS):
        await update.message.delete()
        new_count = db.add_warn(user_id, chat_id, "insult")
        name = update.effective_user.username or update.effective_user.first_name
        await context.bot.send_message(chat_id, f"@{name} получил предупреждение ({new_count}) за оскорбление.\nПравила: /rules")
        await send_admin_log(context, f"AUTO WARN\nTarget: @{name}\nCount: {new_count}")
        if new_count >= 3:
            set_muted(user_id, chat_id, 3600)
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + 3600)
            await context.bot.send_message(chat_id, f"@{name} замучен на 1 час за оскорбления.\nПравила: /rules")
            db.reset_all_warns(user_id, chat_id)
        return
    
    # 18+
    if contains_word(clean, ADULT_WORDS):
        await update.message.delete()
        adult_count = db.add_warn(user_id, chat_id, "adult")
        name = update.effective_user.username or update.effective_user.first_name
        if adult_count == 1:
            await context.bot.send_message(chat_id, f"@{name}, предупреждение за 18+ контент. В следующий раз — бан.\nПравила: /rules")
            await send_admin_log(context, f"AUTO WARN - 18+\nTarget: @{name}")
        else:
            await context.bot.ban_chat_member(chat_id, user_id)
            await context.bot.send_message(chat_id, f"@{name} забанен за 18+ контент (повторное нарушение).\nПравила: /rules")
            db.reset_all_warns(user_id, chat_id)
            await send_admin_log(context, f"AUTO BAN - 18+\nTarget: @{name}")
        return

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

app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_member))
app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pinned_message))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message))

print("Бот запущен!")
app.run_polling(allowed_updates=["message", "pinned_message"])
