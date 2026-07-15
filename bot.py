```python
import logging
import re
import time
import asyncio
import sqlite3
from collections import defaultdict
from telegram import Update, ChatPermissions, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TOKEN = "8637462837:AAFygcu0eLNbXwhOMRPwuDwiry_bx8ij5KM"

# Список ID администраторов (владельцев)
ADMIN_IDS = [5460879396, 8176145729]

# Настройки флуда
FLOOD_LIMIT = 8
FLOOD_TIME = 15
FLOOD_MUTE_DURATION = 300

# ==================== БАЗА ДАННЫХ ====================

class Database:
    def __init__(self, db_path="warns.db"):
        self.db_path = db_path
        self.init_db()
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS warns (
                user_id INTEGER,
                chat_id INTEGER,
                category TEXT,
                count INTEGER,
                PRIMARY KEY (user_id, chat_id, category)
            )
        ''')
        conn.commit()
        conn.close()
    
    def get_warn(self, user_id, chat_id, category):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT count FROM warns WHERE user_id=? AND chat_id=? AND category=?',
            (user_id, chat_id, category)
        )
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else 0
    
    def add_warn(self, user_id, chat_id, category):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        current = self.get_warn(user_id, chat_id, category)
        new_count = current + 1
        cursor.execute(
            'INSERT OR REPLACE INTO warns (user_id, chat_id, category, count) VALUES (?, ?, ?, ?)',
            (user_id, chat_id, category, new_count)
        )
        conn.commit()
        conn.close()
        return new_count
    
    def reset_warns(self, user_id, chat_id, category):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM warns WHERE user_id=? AND chat_id=? AND category=?',
            (user_id, chat_id, category)
        )
        conn.commit()
        conn.close()
    
    def reset_all_warns(self, user_id, chat_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM warns WHERE user_id=? AND chat_id=?',
            (user_id, chat_id)
        )
        conn.commit()
        conn.close()

db = Database()

# ==================== ХРАНИЛИЩА ====================

muted_users = {}
user_messages = defaultdict(list)
pinned_messages = {}

# ==================== ФУНКЦИИ ДЛЯ ОБНАРУЖЕНИЯ ====================

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[^а-яёa-z0-9]', '', text)
    translit = {
        'a': 'а', 'b': 'б', 'c': 'ц', 'd': 'д', 'e': 'е', 'f': 'ф',
        'g': 'г', 'h': 'х', 'i': 'и', 'j': 'й', 'k': 'к', 'l': 'л',
        'm': 'м', 'n': 'н', 'o': 'о', 'p': 'п', 'q': 'к', 'r': 'р',
        's': 'с', 't': 'т', 'u': 'у', 'v': 'в', 'w': 'ш', 'x': 'кс',
        'y': 'ы', 'z': 'з'
    }
    for lat, rus in translit.items():
        text = text.replace(lat, rus)
    return text

def contains_word(text: str, word_list: list) -> bool:
    if not text:
        return False
    cleaned = clean_text(text)
    for word in word_list:
        word_clean = clean_text(word)
        if word_clean in cleaned:
            return True
    return False

# ==================== СПИСКИ СЛОВ ====================

INSULTS = [
    "даун", "олигофрен", "дегенерат", "слабоумный",
    "конченый", "конченая", "клоун",
    "ебалай", "еблашка", "ебло", "ебало",
    "соси", "сосал", "сосет", "отсоси", "сосун",
    "съеби", "съебал",
    "тварь", "тварьебаная", "сукаебаная",
]

MOTHER_TRIGGERS = ["мать", "мама", "мамочка", "мамаша", "мамашку", "мамочку", "маму"]

HARMLESS = [
    "дебил", "идиот", "кретин", "тупица", "бестолочь", "недоумок",
    "пустоголовый", "ограниченный", "недалекий", "крыса", "скотина",
    "скот", "предатель", "подонок", "отморозок", "мошенник", "вор",
    "жулик", "стукач", "доносчик", "провокатор", "страшила",
    "пугало", "горбатый", "кривой", "косой", "плешивый",
    "паршивый", "гнилой", "трухлявый", "дохлый", "падаль",
    "стерва", "стервоза", "деревянный", "завали", "завались",
    "отвали", "отвались", "катись", "вали", "свали", "свалил",
    "заткнись", "замолчи", "уймись", "сдуйся", "лопни", "тресни",
    "сгинь", "пропади", "убогий", "слепой", "глухой", "немой",
    "мерзавец", "негодяй", "сука", "бля", "блять", "нахуй",
    "ебать", "ебаный", "курва", "жопа", "срака", "говно",
    "дерьмо", "мусор", "пиздец", "мудак", "еблан",
    "мамашу", "мамашку", "мамочку", "маму", "мать", "мама", "мамочка", "мамаша",
    "дурак", "дурачок", "дурашка", "дурочка", "дуралей",
    "глупыш", "глупенький", "глупец", "тупенький",
    "тупик", "балбес", "балда", "олух", "простофиля",
    "раззява", "растяпа", "шалопай", "дебилка", "идиотка",
    "идиотик", "кретинка", "блин", "чёрт", "ёлки-палки",
    "ёмоё", "никчемный", "ничтожество", "пустоеместо",
    "неумеха", "неспособный", "рукикрюки", "криворукий",
    "обезьяна", "реветь", "слезы", "спрыгни", "спрыгнуть",
    "прыгай", "прыгни", "сигануть", "сигай", "удавись",
    "вешайся", "повесься", "отравись", "порежь", "порежся",
    "исчезни", "провались", "проваливай", "неживи",
    "сжизнью", "расстаться", "убивайся", "убейся",
    "захлопнись", "заколись", "застрелись", "глотку",
    "перережь", "выпейяду", "кончайжизнь", "гроб",
    "могила", "помри", "бомж", "алкаш", "пьяница",
    "наркоман", "нищий", "токсик", "хейтер", "агрессор",
    "абьюзер", "газлайтер", "манипулятор", "нарцисс",
    "чурка", "хач", "черный", "узкоглазый", "быдло",
    "шваль", "сволочь", "немощный", "инвалид", "калека",
]

ADULT_WORDS = [
    "порно", "секс", "насилие", "изнасилование", "педофил",
    "педофилия", "зоофил", "зоофилия", "сатанизм",
    "расчленение", "насильник", "педофильский",
]

CATEGORIES = {
    "insult": "Оскорбление",
    "adult": "18+/Насилие",
    "flood": "Флуд/Спам"
}

PUNISHMENTS = {
    "insult": {1: "warn", 2: "warn", 3: "mute_1h"},
    "adult": {1: "ban"},
    "flood": {1: "mute_5m"}
}

MUTE_DURATIONS = {
    "mute_5m": 300,
    "mute_1h": 3600,
}

app = Application.builder().token(TOKEN).build()

# ==================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ====================

def is_muted(user_id, chat_id):
    key = f"{user_id}_{chat_id}"
    if key in muted_users:
        if muted_users[key] > time.time():
            return True
        else:
            del muted_users[key]
    return False

def set_muted(user_id, chat_id, duration):
    key = f"{user_id}_{chat_id}"
    muted_users[key] = int(time.time()) + duration

def remove_mute(user_id, chat_id):
    key = f"{user_id}_{chat_id}"
    if key in muted_users:
        del muted_users[key]

def check_flood(user_id, chat_id):
    now = time.time()
    user_messages[user_id].append(now)
    user_messages[user_id] = [t for t in user_messages[user_id] if now - t <= FLOOD_TIME]
    if len(user_messages[user_id]) > FLOOD_LIMIT:
        user_messages[user_id] = []
        return True
    return False

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

# ==================== ПРОВЕРКА АДМИНОВ ====================

async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not update.effective_user:
        return False
    
    user_id = update.effective_user.id
    
    # Проверяем по списку ID
    if user_id in ADMIN_IDS:
        return True
    
    # Проверяем, есть ли у пользователя права администратора в группе
    try:
        member = await context.bot.get_chat_member(update.effective_chat.id, user_id)
        return member.status in ['administrator', 'creator']
    except:
        return False

async def is_target_creator(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int) -> bool:
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target_user_id)
        return member.status == 'creator'
    except:
        return False

async def is_target_admin(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int) -> bool:
    chat_id = update.effective_chat.id
    try:
        member = await context.bot.get_chat_member(chat_id, target_user_id)
        return member.status in ['administrator', 'creator']
    except:
        return False

async def delete_after_delay(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int, delay: int = 1):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except:
        pass

async def send_admin_log(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=ADMIN_IDS[0], text=text)
    except:
        pass

# ==================== ПОЛУЧЕНИЕ ПОЛЬЗОВАТЕЛЯ ====================

async def get_target_from_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
            elif re.match(r'^\d+[smhd]?$', arg):
                continue
    return None, None

def extract_time_from_command(text: str) -> int:
    if not text:
        return 0
    match = re.search(r'(\d+)([smhd])?', text)
    if match:
        value = int(match.group(1))
        unit = match.group(2) if match.group(2) else 'm'
        if unit == 's':
            return value
        elif unit == 'm':
            return value * 60
        elif unit == 'h':
            return value * 3600
        elif unit == 'd':
            return value * 86400
    return 0

async def check_target(update: Update, context: ContextTypes.DEFAULT_TYPE, target_user_id: int, target_name: str):
    bot_user = await context.bot.get_me()
    if target_user_id == bot_user.id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: невозможно применить действие к боту.")
        return False
    if await is_target_creator(update, context, target_user_id):
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: @{target_name} является владельцем группы.")
        return False
    if await is_target_admin(update, context, target_user_id):
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: @{target_name} является администратором.")
        return False
    return True

# ==================== КОМАНДЫ ====================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    await context.bot.send_message(chat_id=update.effective_user.id, text="Бот-модератор активирован. Используйте /help для списка команд.")

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        text = f"ID пользователя @{target.username or target.first_name}: {target.id}"
    else:
        user_id = update.effective_user.id
        text = f"Ваш ID: {user_id}"
    await context.bot.send_message(chat_id=update.effective_user.id, text=text)

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    help_text = """Доступные команды для администраторов:

/rules - показать правила группы
/mute [user] [время] - ограничить отправку сообщений
/unmute [user] - снять ограничение
/ban [user] [время] - заблокировать в группе
/unban [user] - разблокировать
/warn [user] - выдать предупреждение
/unwarn [user] - снять предупреждения
/id - показать ID пользователя (ответьте на сообщение)
/kick [user] - удалить из группы

Как указать пользователя:
1. Ответьте на его сообщение
2. Укажите ID: /mute 123456789

Для указания времени:
10s - секунды, 5m - минуты, 2h - часы, 1d - дни, 0 - навсегда"""
    await context.bot.send_message(chat_id=update.effective_user.id, text=help_text)

async def cmd_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rules_text = """ПРАВИЛА ГРУППЫ

1. Без оскорблений и провокаций
2. Без 18+ и насилия
3. Не флудим/не спамим
4. Не сливаем личные данные

Правила могут изменяться и дополняться."""
    await update.message.reply_text(rules_text)

# ==================== ОБРАБОТЧИК НОВЫХ УЧАСТНИКОВ (выдача тегов и прав) ====================

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выдаёт тег 'бибизяна' и настраивает права новому участнику"""
    for member in update.message.new_chat_members:
        user_id = member.id
        chat_id = update.effective_chat.id
        
        # 1. Выдаём тег "бибизяна"
        try:
            await context.bot.set_chat_administrator_custom_title(
                chat_id=chat_id,
                user_id=user_id,
                custom_title="бибизяна"
            )
        except:
            pass
        
        # 2. Настраиваем права (как на скриншоте)
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=user_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_send_polls=False,
                    can_send_audios=False,
                    can_send_documents=False,
                )
            )
        except:
            pass

# ==================== КОМАНДЫ НАКАЗАНИЙ ====================

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    if not await check_target(update, context, user_id, name):
        return
    duration = extract_time_from_command(update.message.text)
    if duration > 0:
        set_muted(user_id, update.effective_chat.id, duration)
        until_date = int(time.time()) + duration
        duration_text = format_duration(duration)
    else:
        set_muted(user_id, update.effective_chat.id, 31536000)
        until_date = int(time.time()) + 31536000
        duration_text = "навсегда"
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=until_date)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"@{name} замучен на {duration_text}.")
        db.reset_all_warns(user_id, update.effective_chat.id)
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Вы замучены в группе на {duration_text}.")
        except:
            pass
        await send_admin_log(context, f"MUTE\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}\nDuration: {duration_text}")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: {e}")

async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    bot_user = await context.bot.get_me()
    if user_id == bot_user.id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: невозможно применить действие к боту.")
        return
    remove_mute(user_id, update.effective_chat.id)
    try:
        await context.bot.restrict_chat_member(update.effective_chat.id, user_id, permissions=ChatPermissions(can_send_messages=True))
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Мут для @{name} снят.")
        try:
            await context.bot.send_message(chat_id=user_id, text="Ваш мут в группе снят.")
        except:
            pass
        await send_admin_log(context, f"UNMUTE\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: {e}")

async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    if not await check_target(update, context, user_id, name):
        return
    duration = extract_time_from_command(update.message.text)
    try:
        if duration > 0:
            await context.bot.ban_chat_member(update.effective_chat.id, user_id, until_date=int(time.time()) + duration)
            duration_text = format_duration(duration)
        else:
            await context.bot.ban_chat_member(update.effective_chat.id, user_id)
            duration_text = "навсегда"
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"@{name} забанен на {duration_text}.")
        db.reset_all_warns(user_id, update.effective_chat.id)
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Вы забанены в группе на {duration_text}.")
        except:
            pass
        await send_admin_log(context, f"BAN\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}\nDuration: {duration_text}")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: {e}")

async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    bot_user = await context.bot.get_me()
    if user_id == bot_user.id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: невозможно применить действие к боту.")
        return
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, user_id)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Бан для @{name} снят.")
        try:
            await context.bot.send_message(chat_id=user_id, text="Ваш бан в группе снят.")
        except:
            pass
        await send_admin_log(context, f"UNBAN\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: {e}")

async def cmd_kick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    if not await check_target(update, context, user_id, name):
        return
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, user_id)
        await context.bot.unban_chat_member(update.effective_chat.id, user_id)
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"@{name} кикнут.")
        db.reset_all_warns(user_id, update.effective_chat.id)
        await send_admin_log(context, f"KICK\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}")
    except Exception as e:
        await context.bot.send_message(chat_id=update.effective_user.id, text=f"Ошибка: {e}")

async def cmd_warn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    if not await check_target(update, context, user_id, name):
        return
    new_count = db.add_warn(user_id, update.effective_chat.id, "insult")
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"@{name} получил предупреждение ({new_count}).")
    try:
        await context.bot.send_message(chat_id=user_id, text=f"Вы получили предупреждение в группе. Количество: {new_count}.")
    except:
        pass
    await send_admin_log(context, f"WARN\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}\nCount: {new_count}")

async def cmd_unwarn(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    asyncio.create_task(delete_after_delay(context, update.effective_chat.id, update.message.message_id, 1))
    admin = update.effective_user
    target, user_id = await get_target_from_command(update, context)
    if not target and not user_id:
        await context.bot.send_message(chat_id=update.effective_user.id, text="Ошибка: ответьте на сообщение или укажите ID.")
        return
    if target:
        name = target.username or target.first_name
        user_id = target.id
    else:
        name = f"user_{user_id}"
    db.reset_all_warns(user_id, update.effective_chat.id)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"Предупреждения для @{name} сняты.")
    try:
        await context.bot.send_message(chat_id=user_id, text="Ваши предупреждения в группе сняты.")
    except:
        pass
    await send_admin_log(context, f"CLEAR WARNINGS\nAdmin: @{admin.username or admin.first_name}\nTarget: @{name}")

# ==================== ОТКРЕПЛЕНИЕ СООБЩЕНИЙ ====================

async def handle_pinned_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    try:
        chat = await context.bot.get_chat(chat_id)
        if not chat.pinned_message:
            return
        pinned_msg_id = chat.pinned_message.message_id
    except:
        return
    if chat_id in pinned_messages and pinned_messages[chat_id] == pinned_msg_id:
        return
    try:
        await context.bot.unpin_chat_message(chat_id=chat_id)
        pinned_messages[chat_id] = pinned_msg_id
        if update.effective_user:
            try:
                if await is_admin(update, context):
                    await context.bot.send_message(chat_id=update.effective_user.id, text="Сообщение автоматически откреплено. Закрепите повторно для постоянного.")
            except:
                pass
        logger.info(f"Сообщение {pinned_msg_id} откреплено в чате {chat_id}")
    except Exception as e:
        logger.error(f"Ошибка при откреплении: {e}")

# ==================== ОБРАБОТКА СООБЩЕНИЙ ====================

def contains_mother_insult(text: str) -> bool:
    if not text:
        return False
    text_lower = text.lower()
    has_mother = any(word in text_lower for word in ["мать", "мама", "мамочка", "мамаша", "мамашку", "мамочку", "маму"])
    if not has_mother:
        return False
    has_profanity = any(word in text_lower for word in ["сука", "бля", "хуй", "пизда", "блять", "нахуй", "ебать", "еблан", "мудак", "шлюха", "блядина", "курва", "хуйло", "хуесос"])
    return has_mother and has_profanity

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    if update.message.text:
        if check_flood(user_id, chat_id):
            category = "flood"
            duration = FLOOD_MUTE_DURATION
            set_muted(user_id, chat_id, duration)
            try:
                await update.message.delete()
            except:
                pass
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + duration)
            name = update.effective_user.username or update.effective_user.first_name
            await context.bot.send_message(chat_id=chat_id, text=f"@{name} замучен на 5 минут за флуд.")
            db.reset_all_warns(user_id, chat_id)
            try:
                await context.bot.send_message(chat_id=user_id, text="Вы замучены в группе на 5 минут за флуд.")
            except:
                pass
            await send_admin_log(context, f"AUTO MUTE - FLOOD\nTarget: @{name}\nDuration: 5 minutes")
            return
    
    if contains_word(text, INSULTS):
        try:
            await update.message.delete()
        except:
            pass
        new_count = db.add_warn(user_id, chat_id, "insult")
        name = update.effective_user.username or update.effective_user.first_name
        await context.bot.send_message(chat_id=chat_id, text=f"@{name} получил предупреждение ({new_count}) за оскорбление.")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Вы получили предупреждение за оскорбление. Количество: {new_count}.")
        except:
            pass
        await send_admin_log(context, f"AUTO WARN\nTarget: @{name}\nCount: {new_count}")
        if new_count >= 3:
            duration = MUTE_DURATIONS["mute_1h"]
            set_muted(user_id, chat_id, duration)
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + duration)
            await context.bot.send_message(chat_id=chat_id, text=f"@{name} замучен на 1 час за оскорбления.")
            db.reset_all_warns(user_id, chat_id)
        return
    
    if contains_mother_insult(text):
        try:
            await update.message.delete()
        except:
            pass
        new_count = db.add_warn(user_id, chat_id, "insult")
        name = update.effective_user.username or update.effective_user.first_name
        await context.bot.send_message(chat_id=chat_id, text=f"@{name} получил предупреждение ({new_count}) за оскорбление.")
        try:
            await context.bot.send_message(chat_id=user_id, text=f"Вы получили предупреждение за оскорбление. Количество: {new_count}.")
        except:
            pass
        await send_admin_log(context, f"AUTO WARN - MOTHER INSULT\nTarget: @{name}\nCount: {new_count}")
        if new_count >= 3:
            duration = MUTE_DURATIONS["mute_1h"]
            set_muted(user_id, chat_id, duration)
            await context.bot.restrict_chat_member(chat_id, user_id, permissions=ChatPermissions(can_send_messages=False), until_date=int(time.time()) + duration)
            await context.bot.send_message(chat_id=chat_id, text=f"@{name} замучен на 1 час за оскорбления.")
            db.reset_all_warns(user_id, chat_id)
        return
    
    if contains_word(text, ADULT_WORDS):
        try:
            await update.message.delete()
        except:
            pass
        name = update.effective_user.username or update.effective_user.first_name
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.send_message(chat_id=chat_id, text=f"@{name} забанен за 18+ контент.")
        db.reset_all_warns(user_id, chat_id)
        await send_admin_log(context, f"AUTO BAN\nTarget: @{name}\nReason: 18+")
        return

# ==================== РЕГИСТРАЦИЯ ====================

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

# Обработчик новых участников (выдача тегов и прав)
app.add_handler(MessageHandler(
    filters.StatusUpdate.NEW_CHAT_MEMBERS,
    handle_new_member
))

app.add_handler(MessageHandler(filters.StatusUpdate.PINNED_MESSAGE, handle_pinned_message))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.PHOTO & ~filters.COMMAND, handle_message))
app.add_handler(MessageHandler(filters.VIDEO & ~filters.COMMAND, handle_message))

print("Бот запущен!")
app.run_polling(allowed_updates=["message", "pinned_message"])
```
