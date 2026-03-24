import asyncio
import asyncpg
import os
import time
import logging
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_URL = os.getenv("CHANNEL_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
DATABASE_URL = os.getenv("DATABASE_URL")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
db_pool = None

# Хранилища
user_status_msg = {}
user_timer_task = {}
user_current_number = {}
active_request_in_channel = None
request_taken = False

# ========== ИНИЦИАЛИЗАЦИЯ БАЗЫ ДАННЫХ ==========
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
                balance DECIMAL(10,2) DEFAULT 0.00
            )
        ''')
        
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
    logger.info("База данных готова")

# ========== ФУНКЦИИ УПРАВЛЕНИЯ СООБЩЕНИЯМИ ==========
async def update_status_message(user_id: int, text: str, keyboard=None):
    if user_id in user_status_msg:
        try:
            await bot.edit_message_text(
                text,
                chat_id=user_id,
                message_id=user_status_msg[user_id],
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            return
        except:
            pass
    
    if user_id in user_status_msg:
        try:
            await bot.delete_message(user_id, user_status_msg[user_id])
        except:
            pass
    
    msg = await bot.send_message(user_id, text, reply_markup=keyboard, parse_mode="HTML")
    user_status_msg[user_id] = msg.message_id

async def delete_status_message(user_id: int):
    if user_id in user_status_msg:
        try:
            await bot.delete_message(user_id, user_status_msg[user_id])
        except:
            pass
        del user_status_msg[user_id]

async def start_timer(user_id: int, number: str, seconds: int = 600):
    if user_id in user_timer_task:
        user_timer_task[user_id].cancel()
    
    async def timer():
        remaining = seconds
        while remaining > 0:
            minutes = remaining // 60
            secs = remaining % 60
            text = f"<b>⏳ Этот номер недавно обрабатывался</b>\n<i>Его можно поставить повторно только через</i> <code>{minutes:02d}:{secs:02d}</code>\n\n<i>Отправьте другой номер или подождите</i>"
            await update_status_message(user_id, text)
            await asyncio.sleep(1)
            remaining -= 1
        
        await delete_status_message(user_id)
        if user_id in user_timer_task:
            del user_timer_task[user_id]
        if user_id in user_current_number:
            del user_current_number[user_id]
    
    user_timer_task[user_id] = asyncio.create_task(timer())

async def start_send_number(user_id: int, message=None):
    global request_taken, active_request_in_channel
    
    if request_taken:
        if message:
            await message.answer("<b>🔐 Ошибка!</b>\n<i>Данная заявка уже была принята другим пользователем, ожидайте новую.</i>", parse_mode="HTML")
        else:
            await bot.send_message(user_id, "<b>🔐 Ошибка!</b>\n<i>Данная заявка уже была принята другим пользователем, ожидайте новую.</i>", parse_mode="HTML")
        return
    
    request_taken = True
    
    if active_request_in_channel:
        try:
            await bot.delete_message(get_chat_id(), active_request_in_channel)
        except:
            pass
        active_request_in_channel = None
    
    async with db_pool.acquire() as conn:
        waiting = await conn.fetchval("SELECT waiting_for_number FROM users WHERE user_id = $1", user_id)
        
        if waiting:
            await bot.send_message(user_id, "<b>⏳ У вас уже есть активная заявка! Отправьте номер</b>", parse_mode="HTML")
            request_taken = False
            return
        
        await conn.execute("UPDATE users SET waiting_for_number = TRUE WHERE user_id = $1", user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="cancel_number")]
    ])
    
    await update_status_message(
        user_id,
        "<b>⏱️ Заявка принята!</b>\n<i>Отправьте ниже свой номер в любом формате</i>\n<i>Таймер на выполнение:</i> <code>1 мин</code>",
        keyboard
    )
    
    asyncio.create_task(timeout_number(user_id))

async def timeout_number(user_id: int):
    await asyncio.sleep(60)
    async with db_pool.acquire() as conn:
        waiting = await conn.fetchval("SELECT waiting_for_number FROM users WHERE user_id = $1", user_id)
        if waiting:
            await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
            await update_status_message(user_id, "<b>⏰ Время вышло. Заявка отменена</b>")
            await asyncio.sleep(3)
            await delete_status_message(user_id)

# ========== КЛАВИАТУРЫ ==========
def get_main_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Баланс")]],
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
    
    args = message.text.split()
    if len(args) > 1 and args[1] == "send_number":
        await start_send_number(user_id, message)
    else:
        channel_link = get_channel_link()
        await message.answer(
            f"<b>🔐 JetMax - твое богатое будущее!</b>\n<i>Для дальнейшей работы с ботом подпишитесь на канал:</i> {channel_link}",
            reply_markup=get_main_keyboard(),
            parse_mode="HTML"
        )

@dp.message(F.text == "Баланс")
async def show_balance(message: types.Message):
    user_id = message.from_user.id
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        balance = row["balance"] if row else 0.00
    
    await message.answer(
        f"<b>💳 Ваш текущий баланс:</b>\n<code>{balance:.2f} USDT</code>\n\n<i>Для вывода введите !send {balance:.2f}</i>",
        parse_mode="HTML"
    )

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
        [InlineKeyboardButton(text="Создать заявку", callback_data="admin_create")],
        [InlineKeyboardButton(text="Статистика", callback_data="admin_stats")]
    ])
    await message.answer(
        "<b>👨‍💼 Админ панель</b>\n<i>Выберите действие:</i>",
        reply_markup=keyboard,
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "admin_create")
async def admin_create(callback: types.CallbackQuery):
    global active_request_in_channel, request_taken
    
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Доступ запрещен")
        return
    
    chat_id = get_chat_id()
    bot_username = (await bot.get_me()).username
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Сдать номер", url=f"https://t.me/{bot_username}?start=send_number")]
    ])
    
    try:
        request_taken = False
        msg = await bot.send_message(
            chat_id,
            "<b>💼 Требуется номер для работы!</b>\n<i>⏱️ Нажмите кнопку снизу для сдачи</i>",
            reply_markup=keyboard,
            parse_mode="HTML"
        )
        active_request_in_channel = msg.message_id
        await callback.answer("Заявка создана")
    except Exception as e:
        await callback.answer("Ошибка")
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

# ========== ОТМЕНА ==========
@dp.callback_query(F.data == "cancel_number")
async def cancel_number(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.full_name
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
    
    await delete_status_message(user_id)
    await bot.send_message(user_id, "<b>❌ Заявка отменена</b>", parse_mode="HTML")
    await bot.send_message(
        ADMIN_ID,
        f"<b>🔐 Заявка отменена!</b>\n<i>Пользователь:</i> @{username} [<code>{user_id}</code>]",
        parse_mode="HTML"
    )
    await callback.answer()

# ========== ГЛАВНЫЙ ХЭНДЛЕР ==========
@dp.message()
async def handle_all_messages(message: types.Message):
    user_id = message.from_user.id
    text = message.text.strip()
    
    if text.startswith('/'):
        return
    
    async with db_pool.acquire() as conn:
        waiting_number = await conn.fetchval("SELECT waiting_for_number FROM users WHERE user_id = $1", user_id)
        
        if waiting_number:
            number = text
            
            if user_id in user_current_number and user_current_number[user_id] == number:
                return
            
            last_time = await conn.fetchval(
                "SELECT number_timestamp FROM users WHERE user_id = $1 AND current_number = $2",
                user_id, number
            )
            
            if last_time:
                elapsed = int(time.time()) - last_time
                if elapsed < 600:
                    user_current_number[user_id] = number
                    await start_timer(user_id, number, 600 - elapsed)
                    return
            
            await conn.execute("UPDATE users SET waiting_for_number = FALSE WHERE user_id = $1", user_id)
            
            username = message.from_user.username or message.from_user.full_name
            
            await conn.execute('''
                UPDATE users SET current_number = $1, number_timestamp = $2 WHERE user_id = $3
            ''', number, int(time.time()), user_id)
            
            await conn.execute('''
                INSERT INTO requests (user_id, username, number, status, created_at) 
                VALUES ($1, $2, $3, 'waiting_sms', $4)
            ''', user_id, username, number, int(time.time()))
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Запросить смс", callback_data=f"request_sms_{user_id}")],
                [InlineKeyboardButton(text="Отклонить заявку", callback_data=f"reject_{user_id}")]
            ])
            
            await bot.send_message(
                ADMIN_ID,
                f"<b>💼 Новая заявка от @{username} (ID: {user_id})</b>\n<i>Номер:</i> <code>{number}</code>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
            # Удаляем старое сообщение "Заявка принята! Отправьте номер"
            await delete_status_message(user_id)
            
            # Создаем НОВОЕ сообщение со статусом
            await update_status_message(
                user_id,
                "<b>💼 Номер принят!</b>\n<i>Отправьте в чат с ботом SMS для подтверждения номера (оно придет в течение 3-х минут)</i>\n\n<b>Статус: код еще не запрошен</b>"
            )
            return
        
        waiting_sms = await conn.fetchval("SELECT waiting_for_sms FROM users WHERE user_id = $1", user_id)
        
        if waiting_sms:
            await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
            
            sms_code = text
            
            row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
            if not row:
                await message.answer("<b>❌ Заявка не найдена</b>", parse_mode="HTML")
                return
            
            number = row["number"]
            username = message.from_user.username or message.from_user.full_name
            
            keyboard = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Номер встал", callback_data=f"accept_{user_id}_{sms_code}")],
                [InlineKeyboardButton(text="Номер Зарегистрирован", callback_data=f"registered_{user_id}")],
                [InlineKeyboardButton(text="Получена ошибка", callback_data=f"error_{user_id}")]
            ])
            
            await bot.send_message(
                ADMIN_ID,
                f"<b>👨‍💻 Получен код!</b>\n<i>Пользователь:</i> @{username} [<code>{user_id}</code>]\n<i>Код:</i> <code>{sms_code}</code>",
                reply_markup=keyboard,
                parse_mode="HTML"
            )
            
            await delete_status_message(user_id)
            await message.answer("<b>⏱️ Код отправлен!</b>\n<i>Ожидайте подтверждения номера (обычно занимает до 30-ти секунд)</i>", parse_mode="HTML")
            return

# ========== ДЕЙСТВИЯ АДМИНА ==========
@dp.callback_query(F.data.startswith("request_sms_"))
async def request_sms(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE requests SET status = 'waiting_sms' WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = TRUE WHERE user_id = $1", user_id)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отменить", callback_data="cancel_sms")]
    ])
    
    # МЕНЯЕМ существующее сообщение (статус меняется)
    await update_status_message(
        user_id,
        "<b>💼 Номер принят!</b>\n<i>Отправьте в чат с ботом SMS для подтверждения номера (оно придет в течение 3-х минут)</i>\n\n<b>Статус: в ожидании кода</b>",
        keyboard
    )
    
    asyncio.create_task(timeout_sms(user_id))
    await callback.answer("Запрос отправлен")
    await callback.message.delete_reply_markup()

async def timeout_sms(user_id: int):
    await asyncio.sleep(60)
    async with db_pool.acquire() as conn:
        waiting = await conn.fetchval("SELECT waiting_for_sms FROM users WHERE user_id = $1", user_id)
        if waiting:
            await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
            await update_status_message(user_id, "<b>⏰ Время вышло. Заявка отменена</b>")
            await asyncio.sleep(3)
            await delete_status_message(user_id)

@dp.callback_query(F.data.startswith("reject_"))
async def reject_request(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
        if row:
            number = row["number"]
            await conn.execute("UPDATE users SET number_timestamp = $1 WHERE user_id = $2 AND current_number = $3", 
                               int(time.time()), user_id, number)
        
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
    
    await delete_status_message(user_id)
    await bot.send_message(
        user_id,
        "<b>🔐 Заявка отклонена!</b>\n<i>Попробуйте снова или отправьте другой номер телефона</i>",
        parse_mode="HTML"
    )
    await callback.answer("Заявка отклонена")
    await callback.message.delete_reply_markup()

@dp.callback_query(F.data.startswith("accept_"))
async def number_accepted(callback: types.CallbackQuery):
    parts = callback.data.split("_")
    user_id = int(parts[1])
    sms_code = parts[2]
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
        if not row:
            await callback.answer("Заявка не найдена")
            return
        
        number = row["number"]
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        
        count = await conn.fetchval("SELECT COUNT(*) FROM approved_requests")
        request_number = 12 + count
        username = callback.from_user.username or callback.from_user.full_name
        
        await conn.execute('''
            INSERT INTO approved_requests (user_id, username, number, request_number, created_at)
            VALUES ($1, $2, $3, $4, $5)
        ''', user_id, username, number, request_number, int(time.time()))
        
        await conn.execute("UPDATE users SET balance = balance + 4.00 WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET current_number = NULL WHERE user_id = $1", user_id)
    
    await delete_status_message(user_id)
    await bot.send_message(
        user_id,
        f"<b>🎉 Номер принят!</b>\n<i>Вам успешно</i> <code>4.0$</code> <i>на баланс</i>\n\n<i>Номер заявки:</i> <code>#{request_number}</code>",
        parse_mode="HTML"
    )
    await callback.answer("Номер принят")
    await callback.message.delete_reply_markup()

@dp.callback_query(F.data.startswith("registered_"))
async def number_registered(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
        if row:
            number = row["number"]
            await conn.execute("UPDATE users SET number_timestamp = $1 WHERE user_id = $2 AND current_number = $3", 
                               int(time.time()), user_id, number)
        
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
    
    await delete_status_message(user_id)
    await bot.send_message(
        user_id,
        "<b>🔐 Номер уже зарегистрирован!</b>\n<i>Ожидайте создания следующей заявки в канале</i>",
        parse_mode="HTML"
    )
    await callback.answer("Номер зарегистрирован")
    await callback.message.delete_reply_markup()

@dp.callback_query(F.data.startswith("error_"))
async def got_error(callback: types.CallbackQuery):
    user_id = int(callback.data.split("_")[1])
    await callback.message.answer("<b>Введите причину ошибки:</b>", parse_mode="HTML")
    await callback.answer()
    
    @dp.message()
    async def get_error_reason(message: types.Message):
        if message.from_user.id != ADMIN_ID:
            return
        reason = message.text
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
            if row:
                number = row["number"]
                await conn.execute("UPDATE users SET number_timestamp = $1 WHERE user_id = $2 AND current_number = $3", 
                                   int(time.time()), user_id, number)
            
            await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
            await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
        
        await delete_status_message(user_id)
        await bot.send_message(user_id, f"<b>🔐 {reason}</b>", parse_mode="HTML")
        await message.answer("<b>✅ Причина отправлена</b>", parse_mode="HTML")
        dp.message.handlers.remove(get_error_reason)

@dp.callback_query(F.data == "cancel_sms")
async def cancel_sms(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT number FROM requests WHERE user_id = $1", user_id)
        if row:
            number = row["number"]
            await conn.execute("UPDATE users SET number_timestamp = $1 WHERE user_id = $2 AND current_number = $3", 
                               int(time.time()), user_id, number)
        
        await conn.execute("DELETE FROM requests WHERE user_id = $1", user_id)
        await conn.execute("UPDATE users SET waiting_for_sms = FALSE WHERE user_id = $1", user_id)
    
    await delete_status_message(user_id)
    await callback.message.answer("<b>❌ Заявка отменена</b>", parse_mode="HTML")
    await callback.answer()

# ========== ВЫВОД СРЕДСТВ ==========
@dp.message(Command("send"))
async def send_money(message: types.Message):
    user_id = message.from_user.id
    args = message.text.split()
    
    if len(args) != 2:
        await message.answer("<b>❌ Используйте: !send сумма</b>", parse_mode="HTML")
        return
    
    try:
        amount = float(args[1])
    except:
        await message.answer("<b>❌ Неверная сумма</b>", parse_mode="HTML")
        return
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        balance = row["balance"] if row else 0.00
        
        if amount > balance:
            await message.answer(f"<b>❌ Недостаточно средств</b>\n<i>Ваш баланс:</i> <code>{balance:.2f} USDT</code>", parse_mode="HTML")
            return
        
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, user_id)
    
    await message.answer(
        f"<b>✅ Заявка на вывод создана!</b>\n<i>Сумма:</i> <code>{amount:.2f} USDT</code>\n<i>Ожидайте обработки администратором</i>",
        parse_mode="HTML"
    )
    
    await bot.send_message(
        ADMIN_ID,
        f"<b>💰 Запрос на вывод!</b>\n<i>Пользователь:</i> @{message.from_user.username or message.from_user.full_name} [<code>{user_id}</code>]\n<i>Сумма:</i> <code>{amount:.2f} USDT</code>",
        parse_mode="HTML"
    )

# ========== ЗАПУСК ==========
async def main():
    await init_db()
    logger.info("Бот запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
