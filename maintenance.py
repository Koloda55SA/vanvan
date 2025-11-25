import os
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from dotenv import load_dotenv

# --- Базовая настройка ---
logging.basicConfig(level=logging.INFO)
load_dotenv()

# --- Переменные окружения ---
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
# Чтение каналов (можно несколько через запятую)
CHANNELS_STR = os.getenv('CHANNEL_ID')

if not TELEGRAM_TOKEN or not CHANNELS_STR:
    raise ValueError("TELEGRAM_TOKEN и CHANNEL_ID должны быть установлены в .env файле.")

# --- Инициализация бота ---
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# --- Функция для проверки подписки ---
async def is_subscribed(user_id: int) -> bool:
    try:
        channels = [ch.strip() for ch in CHANNELS_STR.split(',')]
        for channel in channels:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in ['member', 'administrator', 'creator']:
                return False
        return True
    except Exception as e:
        logging.error(f"Ошибка проверки подписки для пользователя {user_id}: {e}")
        # В случае ошибки (например, бот не админ в канале), доступ запрещается
        return False

# --- Обработчики сообщений ---
@dp.message(CommandStart())
async def handle_start(message: types.Message):
    user_id = message.from_user.id

    if await is_subscribed(user_id):
        # Пользователь подписан, показать сообщение о тех. работах
        maintenance_text = "🛠 Бот на техническом обслуживании до 13:00.\n\nПриносим извинения за неудобства."
        await message.answer(maintenance_text)
    else:
        # Пользователь не подписан, показать просьбу подписаться
        channels = [ch.strip() for ch in CHANNELS_STR.split(',')]
        buttons = []
        for channel in channels:
            try:
                chat = await bot.get_chat(channel)
                title = chat.title or channel
                # Используем готовую инвайт-ссылку, если есть, или генерируем новую
                url = chat.invite_link or await chat.export_invite_link()
                buttons.append([InlineKeyboardButton(text=f"📢 Подписаться на {title}", url=url)])
            except Exception as e:
                logging.error(f"Не удалось получить детали для канала {channel}: {e}")
                # Кнопка-фолбэк, если не удалось получить инвайт-ссылку
                buttons.append([InlineKeyboardButton(text=f"📢 Подписаться на {channel}", url=f"https://t.me/{channel.replace('@', '')}")])

        buttons.append([InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")])
        
        keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        await message.answer(
            "📢 Для использования бота необходимо подписаться на наши каналы. После подписки нажмите '✅ Я подписался'",
            reply_markup=keyboard
        )

@dp.callback_query(lambda c: c.data == 'check_subscription')
async def handle_check_subscription(callback_query: types.CallbackQuery):
    user_id = callback_query.from_user.id
    if await is_subscribed(user_id):
        await callback_query.message.delete() # Удаляем сообщение с кнопками
        maintenance_text = "🛠 Бот на техническом обслуживании до 22:00.\n\nПриносим извинения за неудобства."
        await callback_query.message.answer(maintenance_text)
        await callback_query.answer()
    else:
        await callback_query.answer("Вы еще не подписались на все каналы.", show_alert=True)


# --- Запуск бота ---
async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
