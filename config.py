from dotenv import load_dotenv
import os

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
PROJECT_ID = os.getenv("PROJECT_ID")
SERVICE_ACCOUNT_FILE = os.getenv("SERVICE_ACCOUNT_FILE")

# Configuration for the logging bot
# ВАЖНО: Замените значения ниже на ваши реальные данные
LOG_BOT_TOKEN = "8068677237:AAFNFplNRLW76ZEKTf7QqoG_lSPIuYGPPQw"  # Токен вашего бота для отправки логов
LOG_CHAT_ID = "7458942659"      # ID чата/канала/пользователя для получения логов
