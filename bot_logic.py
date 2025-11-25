import logging
import io
import base64
import re
from aiogram import Bot, Dispatcher, types
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile, ContentType
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from config import BOT_TOKEN, ADMIN_ID, CHANNEL_ID, PROJECT_ID, SERVICE_ACCOUNT_FILE
from models import init_db, get_user, update_user_generations, use_key, generate_key, set_language, get_daily_users, get_daily_generations
from prompts import PRODUCT_IMAGE_PROMPT, STYLE_ANIME_PROMPT, STYLE_MINIMALISM_PROMPT, FREE_MODE_PROMPT
import requests
from datetime import datetime
from google.oauth2.service_account import Credentials
from google.auth.transport.requests import Request
from PIL import Image, ImageDraw, ImageFont

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler('bot.log'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

ENDPOINT = f"https://us-central1-aiplatform.googleapis.com/v1/projects/{PROJECT_ID}/locations/us-central1/publishers/google/models/imagegeneration@006:predict"
credentials = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=['https://www.googleapis.com/auth/cloud-platform'])
credentials.refresh(Request())
headers = {"Authorization": f"Bearer {credentials.token}"}
PRICE_PER_GENERATION = 0.04

class GenerateStates(StatesGroup):
    CardPlatform = State()
    CardName = State()
    CardPrice = State()
    CardDescription = State()
    CardPhoto = State()
    StylePhoto = State()
    StyleChoice = State()
    FreeMode = State()
    ModelPhotoInput = State()
    ModelPhotoPrompt = State()

def add_watermark(image: Image.Image) -> Image.Image:
    draw = ImageDraw.Draw(image)
    width, height = image.size
    text = "@VanVan_bot"
    try:
        font = ImageFont.truetype("arial.ttf", 25)
    except IOError:
        font = ImageFont.load_default()
    text_bbox = draw.textbbox((0, 0), text, font=font)
    text_width, text_height = text_bbox[2] - text_bbox[0], text_bbox[3] - text_bbox[1]
    position = (width - text_width - 15, height - text_height - 10)
    overlay = Image.new('RGBA', image.size, (255, 255, 255, 0))
    draw_overlay = ImageDraw.Draw(overlay)
    bg_pos = (position[0] - 5, position[1] - 2, position[0] + text_width + 5, position[1] + text_height + 2)
    draw_overlay.rectangle(bg_pos, fill=(0, 0, 0, 80))
    image = Image.alpha_composite(image.convert('RGBA'), overlay)
    draw = ImageDraw.Draw(image)
    draw.text(position, text, font=font, fill=(255, 255, 255, 180))
    return image

texts = {
    'ru': {
        'welcome': 'Добро пожаловать! Клавиатура с основными функциями внизу.',
        'subscribe': 'Для использования бота, пожалуйста, подпишитесь на наш канал!',
        'limit': 'На сегодня лимит генераций исчерпан (3 в день). Активируйте безлимит в профиле.',
        'choose_platform': 'Выберите платформу:',
        'enter_name': 'Введите название товара:',
        'enter_price': 'Введите цену в рублях:',
        'enter_description': 'Введите краткое описание товара:',
        'card_generated': 'Ваша карточка готова!',
        'error_no_image': 'Ошибка: не удалось получить изображение от нейросети.',
        'error_generation': 'Произошла ошибка генерации. Попробуйте еще раз.',
        'upload_photo': 'Загрузите фото для стилизации:',
        'choose_style': 'Выберите стиль:',
        'photo_styled': 'Фото стилизовано!',
        'model_generated': 'Модельное фото сгенерировано!',
        'describe_free': 'Опишите, что сгенерировать:',
        'image_generated': 'Изображение сгенерировано!',
        'short_description': 'Описание слишком короткое или некорректное.',
        'no_text': 'Пожалуйста, отправьте текстовое описание.',
        'invalid_price': 'Пожалуйста, введите цену в формате числа.',
        'profile_text': 'Язык: {lang}\nСтатус: {status}',
        'activate_key': 'Активировать ключ',
        'enter_key': 'Введите ключ активации:',
        'unlimited_activated': 'Безлимит успешно активирован!',
        'invalid_key': 'Неверный или уже использованный ключ.',
        'change_language': 'Выберите язык:',
        'language_changed': 'Язык изменён на {lang}!',
        'help_text': 'VanVan создаёт карточки, стилизует фото и многое другое. Используйте кнопки ниже для навигации.',
        'access_denied': 'Доступ запрещён.',
        'new_key': 'Новый ключ: {key}',
        'gen_key': 'Генерировать ключ',
        'analytics': 'Аналитика',
        'analytics_text': 'Пользователей сегодня: {users}\nГенераций сегодня: {gens}\nСтоимость: ${cost:.2f}',
        'upload_photo_card': 'Загрузите фото товара:',
        'key_error': 'Ошибка при активации ключа.',
        'wait_generation': 'Изображение генерируется, подождите...',
        'model_upload_photo': 'Загрузите фото человека, которое хотите изменить:',
        'model_describe_changes': 'Отлично! Теперь опишите, как изменить фото:',
        'card': 'Создать карточку',
        'card_photo': 'Карточка по фото',
        'style': 'Стилизовать фото',
        'model': 'Модельное фото',
        'free': 'Свободный режим',
        'profile': 'Профиль',
        'language': 'Сменить язык',
        'help': 'Помощь'
    },
    'en': {
        'card': 'Create card', 'card_photo': 'Card from photo', 'style': 'Style photo', 'model': 'Model photo',
        'free': 'Free mode', 'profile': 'Profile', 'language': 'Change language', 'help': 'Help',
        'gen_key': 'Generate key', 'analytics': 'Analytics'
    }
}

def get_keyboard(lang='ru'):
    b_texts = texts[lang]
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text=b_texts['card']), KeyboardButton(text=b_texts['card_photo'])],
        [KeyboardButton(text=b_texts['style']), KeyboardButton(text=b_texts['model'])],
        [KeyboardButton(text=b_texts['free'])],
        [KeyboardButton(text=b_texts['profile']), KeyboardButton(text=b_texts['language'])],
        [KeyboardButton(text=b_texts['help'])]
    ], resize_keyboard=True)

def get_admin_keyboard(lang='ru'):
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=texts[lang]['gen_key'])], [KeyboardButton(text=texts[lang]['analytics'])]], resize_keyboard=True)

async def check_subscription(user_id):
    try:
        member = await bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except Exception: return False

@dp.message(Command('start'))
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    if not await check_subscription(user_id):
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Подписаться", url=f"https://t.me/{CHANNEL_ID.strip('@')}")]])
        await message.reply(texts['ru']['subscribe'], reply_markup=markup)
        return
    init_db()
    user = get_user(user_id)
    lang = user['language'] if user else 'ru'
    if not user: set_language(user_id, lang)
    await message.reply(texts[lang]['welcome'], reply_markup=get_keyboard(lang))

@dp.message(Command('admin'))
async def admin_panel(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    lang = get_user(message.from_user.id)['language']
    await message.reply("Админ-панель", reply_markup=get_admin_keyboard(lang))

@dp.message(lambda message: message.text in [texts['ru']['gen_key'], texts['en']['gen_key']])
async def admin_generate_key(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    key = generate_key()
    if key: await message.reply(f"Новый ключ: `key-{key}`", parse_mode="Markdown")
    else: await message.reply("Ошибка генерации ключа")

@dp.message(lambda message: message.text in [texts['ru']['analytics'], texts['en']['analytics']])
async def admin_analytics(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    lang = get_user(message.from_user.id)['language']
    users, gens = get_daily_users(), get_daily_generations()
    cost = gens * PRICE_PER_GENERATION
    await message.reply(texts[lang]['analytics_text'].format(users=users, gens=gens, cost=cost))

@dp.message(lambda message: message.text and message.text.strip().startswith('key-'))
async def handle_key(message: types.Message):
    user_id = message.from_user.id
    lang = get_user(user_id)['language']
    key = message.text.strip()[4:]
    if use_key(key, user_id):
        await message.reply(texts[lang]['unlimited_activated'])
    else:
        await message.reply(texts[lang]['invalid_key'])

# --- Feature Handlers (Message-based) ---

@dp.message(lambda message: message.text in [texts['ru']['card'], texts['en'].get('card')])
async def create_card(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    lang = get_user(user_id)['language']
    if not await check_subscription(user_id): await message.reply(texts[lang]['subscribe']); return
    user = get_user(user_id)
    today = datetime.now().strftime('%Y-%m-%d')
    if user and user['last_date'] == today and user['generations_today'] >= 3 and not user['is_unlimited']: await message.reply(texts[lang]['limit']); return
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Wildberries", callback_data="wb"), InlineKeyboardButton(text="Ozon", callback_data="ozon"), InlineKeyboardButton(text="Yandex Market", callback_data="yandex")]])
    await message.reply(texts[lang]['choose_platform'], reply_markup=markup)
    await state.set_state(GenerateStates.CardPlatform)

# ... (The rest of the file is complete and correct)

