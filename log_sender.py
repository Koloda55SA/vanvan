import logging
import asyncio
import datetime
from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile
from config import LOG_BOT_TOKEN, LOG_CHAT_ID

# --- Управление уровнем логирования ---
logger = logging.getLogger(__name__)

LOG_LEVELS = {
    "ALL": logging.INFO,
    "ERRORS": logging.ERROR
}
CURRENT_LOG_LEVEL = "ALL"  # Уровень по умолчанию

def set_log_level(new_level: str):
    """Устанавливает уровень логов и отправляет уведомление в лог-чат."""
    global CURRENT_LOG_LEVEL
    if new_level.upper() in LOG_LEVELS:
        CURRENT_LOG_LEVEL = new_level.upper()
        logger.warning(f"Log level for Telegram has been set to {CURRENT_LOG_LEVEL}")
        # Отправляем уведомление о смене режима
        # Используем create_task, чтобы не блокировать основной поток
        asyncio.create_task(send_log_message(
            f"Уровень логов изменен на **{CURRENT_LOG_LEVEL}**", 
            level="WARNING", 
            icon="⚙️"
        ))
        return True
    return False

# -------------------------------------

# Создаем экземпляр бота для логирования
if LOG_BOT_TOKEN and "ВАШ" not in LOG_BOT_TOKEN:
    log_bot = Bot(token=LOG_BOT_TOKEN)
else:
    log_bot = None

async def send_log_message(message: str, level: str = "INFO", icon: str = None):
    """Отправляет универсальное, отформатированное лог-сообщение в чат Telegram."""
    level_no = getattr(logging, level.upper(), logging.INFO)
    required_level = LOG_LEVELS.get(CURRENT_LOG_LEVEL, logging.INFO)
    if level_no < required_level:
        return

    if not log_bot or not LOG_CHAT_ID or "ВАШ" in str(LOG_CHAT_ID):
        return

    default_icon = {
        "INFO": "ℹ️", "SUCCESS": "✅", "WARNING": "⚠️", "ERROR": "❌",
    }.get(level.upper(), "💬")

    display_icon = icon if icon else default_icon
    time_now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    formatted_message = f"{display_icon} **{level.upper()}**\n\n{message}\n\n⏰ `{time_now}`"

    try:
        await log_bot.send_message(chat_id=LOG_CHAT_ID, text=formatted_message, parse_mode='Markdown')
    except Exception as e:
        print(f"CRITICAL: Failed to send log message to Telegram: {e}")

async def send_generation_log(user_id, username, first_name, prompt, image_data: bytes):
    """Отправляет лог о новой генерации вместе с изображением."""
    if LOG_LEVELS[CURRENT_LOG_LEVEL] > logging.INFO:
        return
        
    if not log_bot or not LOG_CHAT_ID or "ВАШ" in str(LOG_CHAT_ID):
        return

    caption = (
        f"📸 **New Image Generation**\n\n"
        f"**User:**\n  - ID: `{user_id}`\n  - Username: @{username}\n  - First Name: {first_name}\n\n"
        f"**Prompt:**\n```\n{prompt}\n```"
    )
    try:
        input_file = BufferedInputFile(image_data, filename='generated_image.png')
        await log_bot.send_photo(chat_id=LOG_CHAT_ID, photo=input_file, caption=caption, parse_mode='Markdown')
    except Exception as e:
        print(f"CRITICAL: Failed to send generation log with photo: {e}")
        await send_log_message(caption, level="ERROR", icon="📸")

async def send_edit_log(user_id, username, first_name, prompt, image_data: bytes):
    """Отправляет лог о новом редактировании вместе с изображением."""
    if LOG_LEVELS[CURRENT_LOG_LEVEL] > logging.INFO:
        return

    if not log_bot or not LOG_CHAT_ID or "ВАШ" in str(LOG_CHAT_ID):
        return

    caption = (
        f"🖼️ **Image Edited**\n\n"
        f"**User:**\n  - ID: `{user_id}`\n  - Username: @{username}\n  - First Name: {first_name}\n\n"
        f"**Edit Prompt:**\n```\n{prompt}\n```"
    )
    try:
        input_file = BufferedInputFile(image_data, filename='edited_image.png')
        await log_bot.send_photo(chat_id=LOG_CHAT_ID, photo=input_file, caption=caption, parse_mode='Markdown')
    except Exception as e:
        print(f"CRITICAL: Failed to send edit log with photo: {e}")
        await send_log_message(caption, level="ERROR", icon="🖼️")

class TelegramLogHandler(logging.Handler):
    """Пользовательский обработчик логов для отправки записей в Telegram."""
    def emit(self, record):
        if 'log_sender' in record.name:
            return
        
        required_level = LOG_LEVELS.get(CURRENT_LOG_LEVEL, logging.INFO)
        if record.levelno < required_level:
            return

        log_entry = self.format(record)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(send_log_message(f"```\n{log_entry}\n```", level=record.levelname))
        except RuntimeError:
            pass

async def close_log_bot_session():
    """Корректно закрывает сессию лог-бота."""
    if log_bot:
        await log_bot.session.close()
        logger.info("Сессия лог-бота закрыта.")
