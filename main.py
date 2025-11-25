import os
import datetime
import logging
import asyncio
import json
from collections import deque
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart, Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, \
    BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
import google.generativeai as genai
from PIL import Image
from io import BytesIO
from supabase import create_client, Client
import uuid
import aiofiles
from log_sender import send_generation_log, close_log_bot_session, send_log_message, send_edit_log, set_log_level

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('bot.log', encoding='utf-8'),
        logging.StreamHandler()
    ]
)

# Добавляем обработчик для отправки логов в Telegram
from log_sender import TelegramLogHandler
telegram_handler = TelegramLogHandler()
# Устанавливаем формат для логов в Telegram
formatter = logging.Formatter('%(name)s - %(levelname)s - %(message)s')
telegram_handler.setFormatter(formatter)
logging.getLogger().addHandler(telegram_handler)

logger = logging.getLogger(__name__)

# Загрузка переменных из .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv('BOT_TOKEN')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY')
CHANNEL_ID = os.getenv('CHANNEL_ID')
SUPABASE_URL = os.getenv('SUPABASE_URL')
SUPABASE_KEY = os.getenv('SUPABASE_KEY')
ADMIN_ID = int(os.getenv('ADMIN_ID'))
ADMIN_USERNAME = os.getenv('ADMIN_USERNAME')
INSTAGRAM_URL = os.getenv('INSTAGRAM_URL', 'https://instagram.com/vanvan_ai')


# Настройка Gemini
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel('gemini-2.5-flash-image-preview')

# Настройка Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Инициализация бота и диспетчера
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(bot=bot, storage=storage)

# Хранение истории сообщений (до 5 сообщений на пользователя) и контекста
user_message_history = {}
user_context_memory = {}

# Защита от банкротства - лимиты генераций в месяц
BANKRUPTCY_PROTECTION = {
    'free': {'max_images': 280, 'cost_gbp': 1.0},
    'minimum': {'max_images': 1400, 'cost_gbp': 5.0},
    'basic': {'max_images': 2800, 'cost_gbp': 10.0},
    'professional': {'max_images': 5600, 'cost_gbp': 20.0},
    'unlimited': {'max_images': 7000, 'cost_gbp': 25.0}
}


# Состояния для FSM
class Form(StatesGroup):
    generate = State()
    activate_key = State()
    create_key = State()
    broadcast = State()
    search_user = State()
    gift = State()
    mute = State()
    message_user = State()
    
    set_referral_reward = State()
    set_subscription_prices = State()
    subscription_details = State()
    feedback = State()
    view_user_images = State()

    image_composition_first_image = State()
    image_composition_second_image = State()
    image_composition_prompt = State()





# Функции для работы с БД
def safe_supabase_execute(query):
    try:
        return query.execute()
    except Exception as e:
        logger.error(f"Supabase error: {str(e)}")
        return type('obj', (object,), {'data': None})()


def get_user(user_id, username=None, first_name=None, referrer_id=None):
    logger.info(f"Получение/создание пользователя: {user_id}")
    response = safe_supabase_execute(supabase.table('users').select('*').eq('user_id', user_id))

    if response.data:
        user = response.data[0]
        updates = {}
        if username and user.get('username') != username:
            updates['username'] = username
        if first_name and user.get('first_name') != first_name:
            updates['first_name'] = first_name
        if updates:
            safe_supabase_execute(supabase.table('users').update(updates).eq('user_id', user_id))
        return user
    else:
        data = {
            'user_id': user_id,
            'is_admin': (user_id == ADMIN_ID),
            'subscription_expires_at': None,
            'banned': False,
            'muted_until': None,
            'daily_gen_limit': 3,
            'daily_edit_limit': 1,
            'referral_gen_bonus': 0,
            'referral_edit_bonus': 0,
            'monthly_generations': 0,
            'total_generations': 0,
            'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'last_activity': datetime.datetime.now(datetime.timezone.utc).isoformat()
        }
        if username:
            data['username'] = username
        if first_name:
            data['first_name'] = first_name

        safe_supabase_execute(supabase.table('users').insert(data))
        logger.info(f"Создан новый пользователь: {user_id}")
        # --- Log New User to Telegram ---
        try:
            loop = asyncio.get_running_loop()
            new_user_msg = f"**New User Joined**\n\n- **ID:** `{user_id}`\n- **Username:** @{username}\n- **First Name:** {first_name}"
            loop.create_task(send_log_message(new_user_msg, level="SUCCESS", icon="👤"))
        except Exception as e:
            logger.error(f"Failed to schedule new user log: {e}")
        # --------------------------------

        if referrer_id and referrer_id != user_id:
            try:
                referrer = get_user(referrer_id)
                if referrer and not is_banned(referrer):
                    settings = get_referral_settings()
                    safe_supabase_execute(supabase.table('referrals').insert({
                        'referrer_id': referrer_id,
                        'referred_id': user_id,
                        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }))
                    safe_supabase_execute(supabase.table('users').update({
                        'referral_gen_bonus': referrer.get('referral_gen_bonus', 0) + settings['gen_reward'],
                        'referral_edit_bonus': referrer.get('referral_edit_bonus', 0) + settings['edit_reward'],
                        'last_activity': datetime.datetime.now(datetime.timezone.utc).isoformat()
                    }).eq('user_id', referrer_id))
                    logger.info(f"Реферал {user_id} добавлен для {referrer_id}")
                    # --- Log New Referral to Telegram ---
                    try:
                        loop = asyncio.get_running_loop()
                        ref_msg = (f"**New Referral**\n\n"
                                   f"- **Referrer ID:** `{referrer_id}`\n"
                                   f"- **New User ID:** `{user_id}` (@{username})")
                        loop.create_task(send_log_message(ref_msg, level="INFO", icon="🤝"))
                    except Exception as e:
                        logger.error(f"Failed to schedule referral log: {e}")
                    # ------------------------------------
            except Exception as e:
                logger.error(f"Ошибка добавления реферала {user_id} для {referrer_id}: {str(e)}")
        return data


def update_user_activity(user_id):
    safe_supabase_execute(supabase.table('users').update({
        'last_activity': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }).eq('user_id', user_id))


def get_referral_settings():
    response = safe_supabase_execute(
        supabase.table('referral_settings').select('*').order('updated_at', desc=True).limit(1))
    return response.data[0] if response.data else {'gen_reward': 3, 'edit_reward': 3}


def update_referral_settings(gen_reward, edit_reward):
    safe_supabase_execute(supabase.table('referral_settings').insert({
        'gen_reward': gen_reward,
        'edit_reward': edit_reward,
        'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }))
    logger.info(f"Обновлены реферальные награды: {gen_reward} ген, {edit_reward} ред")


def get_subscription_plans():
    response = safe_supabase_execute(supabase.table('subscription_plans').select('*'))
    if not response.data:
        create_optimal_plans()
        response = safe_supabase_execute(supabase.table('subscription_plans').select('*'))
    return response.data if response.data else []


def create_optimal_plans():
    optimal_plans = [
        {
            'plan_name': 'Минимум',
            'price_rub': 149,
            'gen_limit': 20,
            'edit_limit': 10,
            'duration_days': 7,
            'monthly_limit': 1400
        },
        {
            'plan_name': 'Базовый',
            'price_rub': 399,
            'gen_limit': 50,
            'edit_limit': 25,
            'duration_days': 30,
            'monthly_limit': 2800
        },
        {
            'plan_name': 'Профессиональный',
            'price_rub': 799,
            'gen_limit': 150,
            'edit_limit': 75,
            'duration_days': 30,
            'monthly_limit': 5600
        },
        {
            'plan_name': 'Бесконечно',
            'price_rub': 1499,
            'gen_limit': 100,  # 100 генераций в час
            'edit_limit': 30,  # 30 редактирований в час
            'duration_days': 30,
            'monthly_limit': 7000
        }
    ]
    for plan in optimal_plans:
        safe_supabase_execute(supabase.table('subscription_plans').insert(plan))
    logger.info("Созданы оптимальные тарифные планы")


def update_subscription_plan(plan_name, price_rub, gen_limit, edit_limit, duration_days):
    monthly_limit = BANKRUPTCY_PROTECTION.get(plan_name.lower(), {}).get('max_images', 7000)
    safe_supabase_execute(supabase.table('subscription_plans').update({
        'price_rub': price_rub,
        'gen_limit': gen_limit,
        'edit_limit': edit_limit,
        'duration_days': duration_days,
        'monthly_limit': monthly_limit,
        'updated_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }).eq('plan_name', plan_name))
    logger.info(
        f"Обновлён тариф {plan_name}: {price_rub}р, {gen_limit or 'безлимит'} ген, {edit_limit or 'безлимит'} ред, {duration_days} дней")


def is_subscription_active(user):
    if not user or user.get('subscription_expires_at') is None:
        return False
    try:
        expires_at = datetime.datetime.fromisoformat(user['subscription_expires_at'].replace('Z', '+00:00'))
        return expires_at > datetime.datetime.now(datetime.timezone.utc)
    except Exception as e:
        logger.error(f"Ошибка проверки подписки: {str(e)}")
        return False


def get_subscription_expiry_text(user):
    if not is_subscription_active(user):
        return "Нет активной подписки."
    try:
        expires_at = datetime.datetime.fromisoformat(user['subscription_expires_at'].replace('Z', '+00:00'))
        return f"Активна до {expires_at.strftime('%d.%m.%Y %H:%M')}"
    except Exception as e:
        logger.error(f"Ошибка получения даты подписки: {str(e)}")
        return "Ошибка получения даты"


def get_daily_gen_limit(user):
    if not user:
        return 3
    if is_subscription_active(user):
        return float('inf') if user.get('daily_gen_limit') is None else user['daily_gen_limit']
    return 3 + user.get('referral_gen_bonus', 0)


def get_daily_edit_limit(user):
    if not user:
        return 1
    if is_subscription_active(user):
        return float('inf') if user.get('daily_edit_limit') is None else user['daily_edit_limit']
    return 1 + user.get('referral_edit_bonus', 0)


def get_monthly_gen_limit(user):
    if not user:
        return BANKRUPTCY_PROTECTION['free']['max_images']
    if not is_subscription_active(user):
        return BANKRUPTCY_PROTECTION['free']['max_images']
    plans = get_subscription_plans()
    user_plan = None
    if user.get('daily_gen_limit') == 20:
        user_plan = 'minimum'
    elif user.get('daily_gen_limit') == 50:
        user_plan = 'basic'
    elif user.get('daily_gen_limit') == 150:
        user_plan = 'professional'
    elif user.get('daily_gen_limit') == 100:
        user_plan = 'unlimited'
    return BANKRUPTCY_PROTECTION.get(user_plan, {'max_images': 280})['max_images']


def is_banned(user):
    return user.get('banned', False) if user else False


def is_muted(user):
    if not user or user.get('muted_until') is None:
        return False
    try:
        muted_until = datetime.datetime.fromisoformat(user['muted_until'].replace('Z', '+00:00'))
        return muted_until > datetime.datetime.now(datetime.timezone.utc)
    except Exception as e:
        logger.error(f"Ошибка проверки мута: {str(e)}")
        return False


def get_today_usage(user_id):
    today = datetime.date.today().isoformat()
    response = safe_supabase_execute(supabase.table('usage').select('*').eq('user_id', user_id).eq('date', today))

    if response.data:
        return response.data[0]
    else:
        safe_supabase_execute(supabase.table('usage').insert({
            'user_id': user_id,
            'date': today,
            'generations': 0,
            'edits': 0
        }))
        logger.info(f"Создана запись использования для {user_id} на {today}")
        return {'generations': 0, 'edits': 0}


def get_hourly_usage(user_id):
    one_hour_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=1)).isoformat()
    response = safe_supabase_execute(
        supabase.table('images')
        .select('created_at')
        .eq('user_id', user_id)
        .gte('created_at', one_hour_ago)
    )
    return len(response.data) if response.data else 0


def get_total_usage(user_id):
    response_gen = safe_supabase_execute(supabase.table('usage').select('generations').eq('user_id', user_id))
    total_gen = sum(row['generations'] for row in response_gen.data) if response_gen.data else 0

    response_edit = safe_supabase_execute(supabase.table('usage').select('edits').eq('user_id', user_id))
    total_edit = sum(row['edits'] for row in response_edit.data) if response_edit.data else 0

    return total_gen, total_edit


def get_monthly_usage(user_id):
    first_day_of_month = datetime.date.today().replace(day=1).isoformat()
    response = safe_supabase_execute(
        supabase.table('usage')
        .select('generations')
        .eq('user_id', user_id)
        .gte('date', first_day_of_month)
    )
    monthly_gen = sum(row['generations'] for row in response.data) if response.data else 0
    return monthly_gen


def increment_usage(user_id, type='generation'):
    today = datetime.date.today().isoformat()
    usage = get_today_usage(user_id)

    if type == 'generation':
        new_gen = usage.get('generations', 0) + 1
        safe_supabase_execute(
            supabase.table('usage').update({'generations': new_gen}).eq('user_id', user_id).eq('date', today))

        user = get_user(user_id)
        monthly_gen = get_monthly_usage(user_id)
        total_gen = user.get('total_generations', 0) + 1

        safe_supabase_execute(supabase.table('users').update({
            'monthly_generations': monthly_gen,
            'total_generations': total_gen
        }).eq('user_id', user_id))

        logger.info(f"Инкремент генераций для {user_id}: {new_gen}")
        return new_gen
    else:
        new_edit = usage.get('edits', 0) + 1
        safe_supabase_execute(
            supabase.table('usage').update({'edits': new_edit}).eq('user_id', user_id).eq('date', today))
        logger.info(f"Инкремент редактирований для {user_id}: {new_edit}")
        return new_edit


def create_key(duration_minutes):
    key = str(uuid.uuid4())
    duration = None if duration_minutes == 0 else duration_minutes
    safe_supabase_execute(supabase.table('keys').insert({
        'key': key,
        'used': False,
        'duration_minutes': duration,
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }))
    logger.info(f"Создан ключ: {key} на {duration or 'навсегда'} минут")
    return key


def activate_key(user_id, key):
    response = safe_supabase_execute(supabase.table('keys').select('*').eq('key', key).eq('used', False))

    if response.data:
        duration_minutes = response.data[0]['duration_minutes']
        expires_at = None if duration_minutes is None else (
                datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            minutes=duration_minutes)).isoformat()

        if duration_minutes is None: # Permanent key
            safe_supabase_execute(supabase.table('users').update({
                'subscription_expires_at': expires_at,
                'daily_gen_limit': 100,
                'daily_edit_limit': 30
            }).eq('user_id', user_id))
        else: # Temporary key
            safe_supabase_execute(supabase.table('users').update({
                'subscription_expires_at': expires_at,
                'daily_gen_limit': None,  # Unlimited
                'daily_edit_limit': 35
            }).eq('user_id', user_id))

        safe_supabase_execute(supabase.table('keys').update({'used': True}).eq('key', key))

        logger.info(f"Ключ {key} активирован для {user_id} до {expires_at or 'навсегда'}")
        return True, duration_minutes

    logger.warning(f"Недействительный ключ {key} для {user_id}")
    return False, 0


async def gift_subscription(admin_id, target_user_id, plan_name):
    admin_user = get_user(admin_id)
    if not admin_user or not admin_user.get('is_admin'):
        return False

    plans = get_subscription_plans()
    plan = next((p for p in plans if p['plan_name'].lower() == plan_name.lower()), None)

    if not plan:
        logger.error(f"Тариф {plan_name} не найден")
        return False

    expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
        days=plan['duration_days'])).isoformat()
    safe_supabase_execute(supabase.table('users').update({
        'subscription_expires_at': expires_at,
        'daily_gen_limit': plan['gen_limit'],
        'daily_edit_limit': plan['edit_limit']
    }).eq('user_id', target_user_id))

    logger.info(f"Админ {admin_id} подарил подписку {plan_name} пользователю {target_user_id}")

    try:
        await bot.send_message(target_user_id,
                               f"Поздравляем! Вы получили подписку {plan_name} на VanVanAi на {plan['duration_days']} дней!")
    except Exception as e:
        logger.error(f"Ошибка уведомления {target_user_id}: {str(e)}")

    return True


def mute_user(admin_id, target_user_id, duration_minutes):
    admin_user = get_user(admin_id)
    if not admin_user or not admin_user.get('is_admin'):
        return False

    muted_until = None if duration_minutes == 0 else (
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=duration_minutes)).isoformat()
    safe_supabase_execute(supabase.table('users').update({'muted_until': muted_until}).eq('user_id', target_user_id))

    logger.info(f"Админ {admin_id} выдал мут {target_user_id} на {duration_minutes or 'навсегда'} минут")
    return True


def ban_user(admin_id, target_user_id):
    admin_user = get_user(admin_id)
    if not admin_user or not admin_user.get('is_admin'):
        return False

    safe_supabase_execute(supabase.table('users').update({'banned': True}).eq('user_id', target_user_id))
    logger.info(f"Админ {admin_id} забанил {target_user_id}")
    return True


def delete_user(admin_id, target_user_id):
    admin_user = get_user(admin_id)
    if not admin_user or not admin_user.get('is_admin'):
        return False

    safe_supabase_execute(supabase.table('users').delete().eq('user_id', target_user_id))
    safe_supabase_execute(supabase.table('usage').delete().eq('user_id', target_user_id))
    safe_supabase_execute(supabase.table('referrals').delete().eq('referred_id', target_user_id))
    safe_supabase_execute(supabase.table('referrals').delete().eq('referrer_id', target_user_id))

    logger.info(f"Админ {admin_id} удалил {target_user_id}")
    return True


def get_all_users():
    response = safe_supabase_execute(supabase.table('users').select('user_id, username, first_name, is_admin, subscription_expires_at, banned, muted_until, daily_gen_limit, daily_edit_limit, referral_gen_bonus, referral_edit_bonus, monthly_generations, total_generations, created_at, last_activity'))
    return response.data if response.data else []


def get_all_channels():
    channels_str = os.getenv('CHANNEL_ID')
    if not channels_str:
        return []

    channel_usernames = [ch.strip() for ch in channels_str.split(',')]
    
    channels_list = []
    for username in channel_usernames:
        channels_list.append({
            'channel_username': username,
            'channel_title': username 
        })
    return channels_list





def search_users(query):
    response = safe_supabase_execute(
        supabase.table('users').select('*').or_(f"username.ilike.%{query}%,first_name.ilike.%{query}%"))
    return response.data if response.data else []


def get_analytics():
    # Используем правильный запрос для подсчета пользователей
    users_response = safe_supabase_execute(supabase.table('users').select('user_id'))
    total_users = len(users_response.data) if users_response.data else 0

    # Для остальной статистики тоже используем правильные запросы
    usage_response = safe_supabase_execute(supabase.table('usage').select('generations, edits'))
    total_generations = sum(row['generations'] for row in usage_response.data) if usage_response.data else 0
    total_edits = sum(row['edits'] for row in usage_response.data) if usage_response.data else 0

    today = datetime.date.today().isoformat()
    usage_today_response = safe_supabase_execute(
        supabase.table('usage')
        .select('user_id, generations, edits')
        .eq('date', today)
    )

    # Правильно считаем активных пользователей сегодня
    active_today = 0
    if usage_today_response.data:
        active_users_set = set()
        for row in usage_today_response.data:
            if row.get('generations', 0) > 0 or row.get('edits', 0) > 0:
                active_users_set.add(row['user_id'])
        active_today = len(active_users_set)

    # Правильно считаем премиум пользователей
    all_users = get_all_users()
    premium_users = len([u for u in all_users if is_subscription_active(u)]) if all_users else 0

    referrals_response = safe_supabase_execute(supabase.table('referrals').select('*'))
    total_referrals = len(referrals_response.data) if referrals_response.data else 0

    week_ago = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=7)).isoformat()
    new_users_week = len([u for u in all_users if
                          u.get('created_at') and u['created_at'] > week_ago]) if all_users else 0

    settings = get_referral_settings()
    plans = get_subscription_plans()

    return {
        'total_users': total_users,
        'total_generations': total_generations,
        'total_edits': total_edits,
        'active_today': active_today,
        'premium_users': premium_users,
        'total_referrals': total_referrals,
        'new_users_week': new_users_week,
        'gen_reward': settings['gen_reward'],
        'edit_reward': settings['edit_reward'],
        'plans': plans
    }


async def is_subscribed(user_id):
    try:
        channels = get_all_channels()
        if not channels:
            return True

        for channel in channels:
            channel_username = channel['channel_username']
            # Убираем @ для проверки подписки
            if channel_username.startswith('@'):
                channel_username = channel_username[1:]

            try:
                # Пробуем получить информацию о участнике канала
                member = await bot.get_chat_member(f"@{channel_username}", user_id)
                if member.status not in ['member', 'administrator', 'creator']:
                    logger.info(f"Пользователь {user_id} не подписан на канал {channel_username}")
                    return False
                else:
                    logger.info(f"Пользователь {user_id} подписан на канал {channel_username}")
            except Exception as e:
                logger.error(f"Ошибка проверки подписки на канал {channel_username}: {str(e)}")
                # Если не можем проверить канал, считаем что пользователь не подписан
                return False

        return True
    except Exception as e:
        logger.error(f"Общая ошибка проверки подписки {user_id}: {str(e)}")
        return False


# Функции для работы с памятью (5 сообщений)
def add_to_message_history(user_id, role, message_text):
    if user_id not in user_message_history:
        user_message_history[user_id] = deque(maxlen=5)

    user_message_history[user_id].append({
        'role': role,
        'text': message_text,
        'timestamp': datetime.datetime.now().isoformat()
    })
    logger.info(f"Добавлено сообщение в историю {user_id}: {role} - {message_text}")


def get_message_history(user_id):
    return list(user_message_history.get(user_id, []))


def clear_message_history(user_id):
    if user_id in user_message_history:
        user_message_history[user_id].clear()
        logger.info(f"История сообщений очищена для пользователя {user_id}")


def get_context_from_history(user_id):
    history = get_message_history(user_id)
    if not history:
        return ""

    context_parts = []
    for msg in history:
        role = "Пользователь" if msg['role'] == 'user' else "Ассистент"
        context_parts.append(f"{role}: {msg['text']}")

    return "\n".join(context_parts[-5:])


# Функции для работы с контекстной памятью
def update_user_context(user_id, context_data):
    user_context_memory[user_id] = {
        'preferences': context_data.get('preferences', {}),
        'last_theme': context_data.get('last_theme', ''),
        'style_preference': context_data.get('style_preference', ''),
        'updated_at': datetime.datetime.now().isoformat()
    }


def get_user_context(user_id):
    return user_context_memory.get(user_id, {})


# Функции для работы с изображениями пользователя
def save_user_image(user_id, prompt, image_data):
    image_id = str(uuid.uuid4())
    file_path = f"{user_id}/{image_id}.png"

    try:
        supabase.storage.from_('media').upload(file_path, image_data)
    except Exception as e:
        logger.error(f"Ошибка загрузки изображения в Storage: {str(e)}")
        return None

    public_url = supabase.storage.from_('media').get_public_url(file_path)

    safe_supabase_execute(supabase.table('images').insert({
        'image_id': image_id,
        'user_id': user_id,
        'prompt': prompt,
        'image_url': public_url,
        'created_at': datetime.datetime.now(datetime.timezone.utc).isoformat()
    }))
    logger.info(f"Сохранено изображение {image_id} для пользователя {user_id} в Storage")
    return image_id


def get_user_images(user_id, limit=10):
    response = safe_supabase_execute(
        supabase.table('images')
        .select('*')
        .eq('user_id', user_id)
        .order('created_at', desc=True)
        .limit(limit)
    )
    return response.data if response.data else []


def get_user_images_count(user_id):
    response = safe_supabase_execute(
        supabase.table('images')
        .select('image_id')
        .eq('user_id', user_id)
    )
    return len(response.data) if response.data else 0


def get_user_recent_activity(user_id, limit=20):
    images = get_user_images(user_id, limit)
    activities = []

    for img in images:
        activities.append({
            'type': 'generation',
            'prompt': img['prompt'],
            'image_url': img['image_url'],
            'created_at': img['created_at']
        })

    return activities


# Клавиатуры
def get_user_keyboard(is_admin=False):
    keyboard = [
        [
            KeyboardButton(text="🎨 Сгенерировать изображение"),
            KeyboardButton(text="🖼️ Редактировать фото")
        ],
        [
            KeyboardButton(text="🎭 Скрестить фото")
        ],
        [
        ],
        [
            KeyboardButton(text="👤 Профиль"),
            KeyboardButton(text="🤝 Реферальная программа")
        ],
        [
            KeyboardButton(text="🔑 Активировать ключ"),
            KeyboardButton(text="💳 Купить подписку")
        ],
        [
            KeyboardButton(text="❓ Помощь"),
            KeyboardButton(text="✅ Проверить подписку")
        ],
        [
            KeyboardButton(text="💬 Обратная связь"),
            KeyboardButton(text="🧹 Очистить историю")
        ]
    ]
    if is_admin:
        keyboard.extend([
            [
                KeyboardButton(text="🗝️ Админ: Создать ключ"),
                KeyboardButton(text="👥 Админ: Список юзеров")
            ],
            [
                KeyboardButton(text="🔍 Админ: Поиск юзеров"),
                KeyboardButton(text="📈 Админ: Аналитика")
            ],
            [
                KeyboardButton(text="📢 Админ: Рассылка")
            ],
            [
                KeyboardButton(text="🎁 Админ: Изменить реф. награду"),
                KeyboardButton(text="💰 Админ: Изменить цены тарифов")
            ],
            [
                KeyboardButton(text="⚡ Админ: Оптимальные цены")
            ]
        ])
    return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True)


def get_cancel_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")]
    ])


def get_main_menu_keyboard(is_admin=False):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Главное меню", callback_data="back_main")]
    ])


def get_users_inline(users, action_prefix):
    keyboard = []
    for user in users[:50]:
        username = user.get('username') or user.get('first_name') or f"ID: {user.get('user_id')}"
        keyboard.append([InlineKeyboardButton(text=username, callback_data=f"{action_prefix}_{user['user_id']}")])
    keyboard.append([InlineKeyboardButton(text="Назад", callback_data="back_admin")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_user_actions_inline(user_id):
    keyboard = [
        [InlineKeyboardButton(text="Подарить Минимум", callback_data=f"gift_min_{user_id}")],
        [InlineKeyboardButton(text="Подарить Базовый", callback_data=f"gift_base_{user_id}")],
        [InlineKeyboardButton(text="Подарить Профессиональный", callback_data=f"gift_pro_{user_id}")],
        [InlineKeyboardButton(text="Подарить Бесконечно", callback_data=f"gift_unlim_{user_id}")],
        [InlineKeyboardButton(text="Выдать мут", callback_data=f"mute_{user_id}")],
        [InlineKeyboardButton(text="Забанить", callback_data=f"ban_{user_id}")],
        [InlineKeyboardButton(text="Удалить", callback_data=f"delete_{user_id}")],
        [InlineKeyboardButton(text="Отправить сообщение", callback_data=f"message_{user_id}")],
        [InlineKeyboardButton(text="Статистика", callback_data=f"stats_{user_id}")],
        [InlineKeyboardButton(text="Просмотр изображений", callback_data=f"view_images_{user_id}")],
        [InlineKeyboardButton(text="Назад", callback_data="back_users_list")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_subscription_plans_inline():
    plans = get_subscription_plans()
    keyboard = []
    for plan in plans:
        button_text = f"{plan['plan_name']} - {plan['price_rub']}₴"
        keyboard.append(
            [InlineKeyboardButton(text=button_text, callback_data=f"plan_details_{plan['plan_name'].lower()}")])
    keyboard.append([InlineKeyboardButton(text="Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def get_plan_details_inline(plan_name):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Купить подписку", callback_data=f"buy_{plan_name}")],
        [InlineKeyboardButton(text="Назад к тарифам", callback_data="back_subscriptions")]
    ])


def get_buy_subscription_inline(user_id, username, plan_name):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Купить", callback_data=f"confirm_buy_{plan_name}_{user_id}_{username}")
    ], [
        InlineKeyboardButton(text="Назад", callback_data=f"back_plan_{plan_name}")
    ]])


def get_feedback_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="5", callback_data="feedback_5")],
        [InlineKeyboardButton(text="4", callback_data="feedback_4")],
        [InlineKeyboardButton(text="3", callback_data="feedback_3")],
        [InlineKeyboardButton(text="2", callback_data="feedback_2")],
        [InlineKeyboardButton(text="1", callback_data="feedback_1")],
        [InlineKeyboardButton(text="Отмена", callback_data="cancel")]
    ])


def get_images_navigation_inline(user_id, current_index, total_images):
    keyboard = []
    if total_images > 1:
        row = []
        if current_index > 0:
            row.append(InlineKeyboardButton(text="← Назад", callback_data=f"img_prev_{user_id}_{current_index}"))
        if current_index < total_images - 1:
            row.append(InlineKeyboardButton(text="Вперед →", callback_data=f"img_next_{user_id}_{current_index}"))
        if row:
            keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="Закрыть", callback_data=f"close_images_{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)





# Обработчики команд
@dp.message(CommandStart())
async def start(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    username = message.from_user.username
    first_name = message.from_user.first_name
    referrer_id = None

    if message.text.startswith('/start ref_'):
        try:
            referrer_id = int(message.text.split('ref_')[1])
        except (IndexError, ValueError):
            logger.warning(f"Неверный реферальный параметр: {message.text}")

    user = get_user(user_id, username, first_name, referrer_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены и не можете использовать бота.")
        return

    channels = get_all_channels()
    if channels and not await is_subscribed(user_id):
        keyboard_buttons = []
        for channel in channels:
            channel_username = channel['channel_username']
            if not channel_username.startswith('@'):
                channel_username = f"@{channel_username}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📢 Подписаться на {channel['channel_title']}",
                    url=f"https://t.me/{channel_username[1:]}"
                )
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        channel_list = "\n".join(
            [f"• {channel['channel_title']} ({channel['channel_username']})" for channel in channels])

        await message.answer(
            f"📢 Для использования бота необходимо подписаться на наши каналы:\n\n{channel_list}\n\nПосле подписки нажмите '✅ Я подписался'",
            reply_markup=keyboard
        )
        return

    welcome_text = f"""
🎉 Добро пожаловать в VanVanAi, {first_name or 'творческая душа'}!

Я — ваш персональный AI-художник, готовый воплотить любые идеи в жизнь. 

Что я умею:
🎨 **Генерировать изображения** — просто опишите, что хотите увидеть.
🖼️ **Редактировать фото** — изменяйте стиль, фон и добавляйте детали.
🎭 **Скрещивать фото** — создавайте уникальные коллажи из двух изображений.

✨ А также:
- Создавать карточки для маркетплейсов.
- Генерировать модельные фото и портреты.

🧠 **Новинка:** Я помню наш диалог, чтобы лучше понимать ваши идеи!

💡 **Совет:** Для наилучшего результата описывайте свои идеи подробно. Чем больше деталей, тем волшебнее получится магия!

👇 Начните творить, выбрав одну из кнопок ниже.
    """
    await message.answer(welcome_text, reply_markup=get_user_keyboard(user.get('is_admin', False)))
    add_to_message_history(user_id, 'user', '/start')


@dp.message(Command("help"))
async def help_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    help_text = f"""
🤖 VanVanAi - Помощь

Добро пожаловать в справочный центр! Здесь вы найдете описание всех функций бота.

📝 Основные команды:
🎨 **Сгенерировать изображение** - Создает уникальное изображение по вашему текстовому описанию.
🖼️ **Редактировать фото** - Изменяет ваше фото, применяя стили, меняя фон или добавляя объекты.
🎭 **Скрестить фото** - Объединяет два изображения в одно на основе ваших инструкций.
👤 **Профиль** - Показывает вашу статистику, лимиты и статус подписки.
🤝 **Реферальная программа** - Приглашайте друзей и получайте бонусы.
🔑 **Активировать ключ** - Введите ключ для активации премиум-доступа.
💳 **Купить подписку** - Просмотр и покупка тарифных планов.
💬 **Обратная связь** - Помогите нам стать лучше, оставив свой отзыв.
🧹 **Очистить историю** - Сбрасывает контекст диалога с ботом.

💡 Советы для лучших результатов:
- **Будьте детальны:** Чем подробнее ваш запрос, тем точнее результат.
- **Укажите стиль:** Добавляйте "в стиле аниме", "фотореализм", "маслом на холсте".
- **Пример:** "Кот в очках сидит за ноутбуком, неоновый свет, киберпанк".

📞 **Поддержка:** @{ADMIN_USERNAME}
📷 **Instagram:** {INSTAGRAM_URL}
    """

    await message.answer(help_text)
    add_to_message_history(user_id, 'user', '/help')


@dp.message(Command("cancel"))
async def cancel_command(message: types.Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    await message.answer("Действие отменено. Возвращаюсь в главное меню.",
                         reply_markup=get_user_keyboard(user.get('is_admin', False)))
    add_to_message_history(user_id, 'user', '/cancel')


@dp.message(Command("stats"))
async def stats_command(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return

    total_gen, total_edit = get_total_usage(user_id)
    usage = get_today_usage(user_id)
    referrals_response = safe_supabase_execute(supabase.table('referrals').select('*').eq('referrer_id', user_id))
    referral_count = len(referrals_response.data) if referrals_response.data else 0

    gen_limit = get_daily_gen_limit(user)
    edit_limit = get_daily_edit_limit(user)
    remaining_gen = "∞" if gen_limit == float('inf') else max(0, gen_limit - usage.get('generations', 0))
    remaining_edit = "∞" if edit_limit == float('inf') else max(0, edit_limit - usage.get('edits', 0))

    history = get_message_history(user_id)
    recent_activity = len(history)

    stats_text = f"""
📊 Ваша статистика

🎯 Активность:
Недавние запросы: {recent_activity}
Всего генераций: {total_gen}
Всего редактирований: {total_edit}

📈 Лимиты сегодня:
🎨 Генерации: {remaining_gen}/{gen_limit if gen_limit != float('inf') else '∞'}
🖼️ Редактирования: {remaining_edit}/{edit_limit if edit_limit != float('inf') else '∞'}

👥 Рефералы:
Приглашено друзей: {referral_count}

💡 Память: Бот помнит ваши последние {recent_activity} сообщений для контекста
    """

    await message.answer(stats_text, reply_markup=get_user_keyboard(user.get('is_admin', False)))
    add_to_message_history(user_id, 'user', '/stats')


# Основные обработчики
@dp.message(lambda message: message.text == "🎨 Сгенерировать изображение")
async def generate_image(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return
    if is_muted(user):
        await message.answer("Вы в муте.")
        return

    channels = get_all_channels()
    if channels and not await is_subscribed(user_id):
        keyboard_buttons = []
        for channel in channels:
            channel_username = channel['channel_username']
            if not channel_username.startswith('@'):
                channel_username = f"@{channel_username}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📢 Подписаться на {channel['channel_title']}",
                    url=f"https://t.me/{channel_username[1:]}"
                )
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        channel_list = "\n".join(
            [f"• {channel['channel_title']} ({channel['channel_username']})" for channel in channels])

        await message.answer(
            f"📢 Для использования бота необходимо подписаться на наши каналы:\n\n{channel_list}\n\nПосле подписки нажмите '✅ Я подписался'",
            reply_markup=keyboard
        )
        return

    await state.set_state(Form.generate)

    prompt_examples = """
🎨 Примеры промптов для вдохновения:

Реалистичные сцены:
"Закат над океаном, золотые облака, фотореалистично"
"Горный пейзаж с озером, утренний туман"

Фэнтези и аниме:
"Волшебный лес с светящимися грибами, стиль аниме"
"Дракон в древнем храме, цифровая живопись"

Портреты:
"Портрет девушки с рыжими волосами, мягкое освещение"
"Киберпанк персонаж с неоновыми имплантами"

Архитектура:
"Футуристический город с летающими машинами"
"Средневековый замок в тумане"

💡 Совет: Чем детальнее описание, тем лучше результат!
    """

    await message.answer(
        f"{prompt_examples}\n\n"
        "✍️ Введите ваш промпт для генерации изображения:\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'generate_image')


@dp.message(Form.generate)
async def process_generate(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте текстовое описание для генерации изображения.")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Генерация отменена.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    if is_subscription_active(user) and user.get('daily_gen_limit') == 100:
        hourly_usage = get_hourly_usage(user_id)
        if hourly_usage >= 100:
            await message.answer(
                "⚠️ Вы достигли часового лимита генераций (100/час).\n\n"
                "Пожалуйста, подождите немного перед следующей генерацией.",
                reply_markup=get_user_keyboard(user.get('is_admin', False))
            )
            await state.clear()
            return

    gen_limit = get_daily_gen_limit(user)
    usage = get_today_usage(user_id)

    if usage.get('generations', 0) >= gen_limit:
        await message.answer(
            f"⚠️ Лимит генераций исчерпан!\n\n"
            f"Сегодня использовано: {usage.get('generations', 0)}/{gen_limit if gen_limit != float('inf') else '∞'}\n"
            f"{get_subscription_expiry_text(user)}\n\n"
            f"💳 Выгодные тарифы для увеличения лимитов:\n"
            f"Минимум: 149р/7д (20 ген/день)\n"
            f"Базовый: 399р/30д (50 ген/день)\n"
            f"Профессиональный: 799р/30д (150 ген/день)\n\n"
            f"Напишите @{ADMIN_USERNAME} или купите подписку!",
            reply_markup=get_user_keyboard(user.get('is_admin', False))
        )
        await state.clear()
        return

    prompt = message.text.strip()
    if len(prompt) < 3:
        await message.answer("❌ Промпт слишком короткий. Введите подробное описание (минимум 3 символа).")
        return
    if len(prompt) > 1000:
        await message.answer("❌ Промпт слишком длинный. Максимум 1000 символов.")
        return

    add_to_message_history(user_id, 'user', prompt)
    context = get_context_from_history(user_id)

    user_context = get_user_context(user_id)
    if not user_context:
        user_context = {'preferences': {}, 'last_theme': '', 'style_preference': ''}

    if any(word in prompt.lower() for word in ['портрет', 'лицо', 'человек']):
        user_context['last_theme'] = 'portrait'
    elif any(word in prompt.lower() for word in ['пейзаж', 'город', 'природа']):
        user_context['last_theme'] = 'landscape'
    elif any(word in prompt.lower() for word in ['фэнтези', 'фантастика']):
        user_context['last_theme'] = 'fantasy'

    update_user_context(user_id, user_context)

    progress_msg = await message.answer("🔄 Генерация началась...\n[░░░░░░░░░░] 0%")

    try:
        for percent in [0, 20, 40, 60, 80, 100]:
            bars = '█' * (percent // 10) + '░' * (10 - percent // 10)
            status_text = "Анализирую запрос..." if percent < 30 else \
                "Создаю изображение..." if percent < 70 else \
                    "Финальная обработка..."

            await asyncio.sleep(1)
            await bot.edit_message_text(
                f"🔄 Генерация...\n[{bars}] {percent}%\n{status_text}",
                chat_id=message.chat.id,
                message_id=progress_msg.message_id
            )

        logger.info(f"Генерация для {user_id}: {prompt}")

        enhanced_prompt = prompt
        if context:
            enhanced_prompt = f"Контекст предыдущих сообщений:\n{context}\n\nТекущий запрос: {prompt}"

        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: model.generate_content(enhanced_prompt)
        )

        if not response.candidates:
            logger.error(f"Не удалось получить изображение от Gemini (пустые кандидаты). Response: {response}")
            await message.answer(
                "❌ Не удалось сгенерировать изображение. Ваш запрос мог быть заблокирован политикой безопасности.\n\n"
                "💡 Попробуйте:\n"
                "Изменить формулировку, сделав ее более нейтральной\n"
                "Убрать потенциально неоднозначные слова\n"
                "Использовать другой язык (русский/английский)"
            )
        else:
            response_handled = False
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data and hasattr(part.inline_data, 'data'):
                        img_data = part.inline_data.data
                        img = Image.open(BytesIO(img_data))

                        img_byte_arr = BytesIO()
                        img.save(img_byte_arr, format='PNG', optimize=True, quality=85)
                        img_byte_arr.seek(0)
                        img_data = img_byte_arr.getvalue()

                        input_file = BufferedInputFile(img_data, filename='generated_image.png')

                        await message.answer_photo(
                            input_file,
                            caption=f"🎨 Сгенерировано: '{prompt}'\n\n"
                                    f"💡 Хотите изменить что-то? Отправьте фото с описанием изменений!"
                        )
                        response_handled = True

                        await send_generation_log(user_id, message.from_user.username, message.from_user.first_name, prompt, img_data)
                        save_user_image(user_id, prompt, img_data)
                        break
                if response_handled:
                    break

            if not response_handled:
                logger.error(f"Не удалось получить изображение от Gemini. Response: {response}")
                await message.answer(
                    "❌ Не удалось сгенерировать изображение.\n\n"
                    "💡 Попробуйте:\n"
                    "Изменить формулировку\n"
                    "Добавить больше деталей\n"
                    "Указать стиль изображения\n"
                    "Использовать другой язык (русский/английский)"
                )
            else:
                if any(hasattr(part, 'inline_data') for cand in response.candidates for part in cand.content.parts):
                    increment_usage(user_id, 'generation')
                    add_to_message_history(user_id, 'assistant', f'Сгенерировано изображение: {prompt}')

                await message.answer(
                    "✅ Изображение готово!\n\n"
                    "Что дальше?\n"
                    "🎨 Сгенерировать еще\n"
                    "🖼️ Отредактировать это фото\n"
                    "📊 Посмотреть статистику",
                    reply_markup=get_user_keyboard(user.get('is_admin', False))
                )

    except Exception as e:
        logger.error(f"Ошибка генерации для {user_id}: {str(e)}")
        await message.answer(
            f"❌ Произошла ошибка при генерации:\n\n"
            f"`{str(e)}`\n\n"
            f"💡 Попробуйте:\n"
            f"Переформулировать запрос\n"
            f"Упростить описание\n"
            f"Попробовать позже"
        )

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=progress_msg.message_id)
    except:
        pass

    await state.clear()


@dp.message(lambda message: message.text == "🖼️ Редактировать фото")
async def edit_photo_prompt(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    edit_text = """
🖼️ **Редактор изображений**

Дайте волю своему воображению! Отправьте мне фотографию, и я изменю ее по вашему желанию.

📝 **Как это работает:**
1.  Отправьте фото (не как файл).
2.  В подписи к фото напишите, что нужно изменить.

💡 **Примеры идей для редактирования:**
-   *"Измени фон на ночной город с неоновыми огнями"*
-   *"Сделай это фото в стиле аниме 90-х"*
-   *"Добавь на стол чашку кофе и ноутбук"*
-   *"Перекрась машину в красный цвет"*

🎯 **Что можно сделать:**
-   Изменить фон и окружение
-   Применить художественные стили (поп-арт, киберпанк, фэнтези)
-   Добавить или убрать объекты
-   Изменить цвета и освещение

Готов творить магию. Жду ваше фото с инструкциями!
    """

    await message.answer(edit_text)
    add_to_message_history(user_id, 'user', 'edit_photo')


@dp.message(lambda message: message.photo and message.caption)
async def handle_photo_edit(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return
    if is_muted(user):
        await message.answer("Вы в муте.")
        return

    channels = get_all_channels()
    if channels and not await is_subscribed(user_id):
        keyboard_buttons = []
        for channel in channels:
            channel_username = channel['channel_username']
            if not channel_username.startswith('@'):
                channel_username = f"@{channel_username}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📢 Подписаться на {channel['channel_title']}",
                    url=f"https://t.me/{channel_username[1:]}"
                )
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        channel_list = "\n".join(
            [f"• {channel['channel_title']} ({channel['channel_username']})" for channel in channels])

        await message.answer(
            f"📢 Для использования бота необходимо подписаться на наши каналы:\n\n{channel_list}\n\nПосле подписки нажмите '✅ Я подписался'",
            reply_markup=keyboard
        )
        return

    edit_limit = get_daily_edit_limit(user)
    usage = get_today_usage(user_id)

    if usage.get('edits', 0) >= edit_limit:
        await message.answer(
            f"⚠️ Лимит редактирований исчерпан!\n\n"
            f"Сегодня использовано: {usage.get('edits', 0)}/{edit_limit if edit_limit != float('inf') else '∞'}\n"
            f"{get_subscription_expiry_text(user)}\n\n"
            f"💳 Выгодные тарифы для увеличения лимитов:\n"
            f"Минимум: 149р/7д (10 ред/день)\n"
            f"Базовый: 399р/30д (25 ред/день)\n"
            f"Профессиональный: 799р/30д (75 ред/день)\n\n"
            f"Напишите @{ADMIN_USERNAME} или купите подписку!",
            reply_markup=get_user_keyboard(user.get('is_admin', False))
        )
        return

    prompt = message.caption.strip()
    if len(prompt) < 3:
        await message.answer("❌ Описание изменений слишком короткое. Минимум 3 символа.")
        return

    photo = message.photo[-1]
    file_id = photo.file_id
    file_info = await bot.get_file(file_id)
    downloaded_file = await bot.download_file(file_info.file_path)

    img = Image.open(BytesIO(downloaded_file.read()))

    progress_msg = await message.answer("🔄 Редактирование началось...\n[░░░░░░░░░░] 0%")

    try:
        for percent in [0, 20, 40, 60, 80, 100]:
            bars = '█' * (percent // 10) + '░' * (10 - percent // 10)
            status_text = "Анализирую фото..." if percent < 30 else \
                "Применяю изменения..." if percent < 70 else \
                    "Финальная обработка..."

            await asyncio.sleep(1)
            await bot.edit_message_text(
                f"🔄 Редактирование...\n[{bars}] {percent}%\n{status_text}",
                chat_id=message.chat.id,
                message_id=progress_msg.message_id
            )

        logger.info(f"Редактирование для {user_id}: {prompt}")

        context = get_context_from_history(user_id)
        enhanced_prompt = f"Контекст: {context}\n\nИзменить изображение: {prompt}" if context else prompt

        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: model.generate_content([enhanced_prompt, img])
        )

        if not response.candidates:
            logger.error(f"Не удалось получить изображение от Gemini (пустые кандидаты). Response: {response}")
            await message.answer(
                "❌ Не удалось отредактировать изображение. Ваш запрос мог быть заблокирован политикой безопасности.\n\n"
                "💡 Попробуйте:\n"
                "Изменить формулировку, сделав ее более нейтральной\n"
                "Убрать потенциально неоднозначные слова\n"
                "Использовать другое фото"
            )
        else:
            response_handled = False
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data and hasattr(part.inline_data, 'data'):
                        img_data = part.inline_data.data
                        edited_img = Image.open(BytesIO(img_data))

                        img_byte_arr = BytesIO()
                        edited_img.save(img_byte_arr, format='PNG', optimize=True, quality=85)
                        img_byte_arr.seek(0)
                        img_data = img_byte_arr.getvalue()

                        input_file = BufferedInputFile(img_data, filename='edited_image.png')

                        await message.answer_photo(
                            input_file,
                            caption=f"🖼️ Отредактировано: '{prompt}'\n\n"
                                    f"💡 Хотите продолжить редактирование? Отправьте новое описание!"
                        )
                        response_handled = True

                        # --- Log Photo Edit to Telegram ---
                        await send_edit_log(user_id, message.from_user.username, message.from_user.first_name, prompt, img_data)
                        # ----------------------------------

                        save_user_image(user_id, f"Edit: {prompt}", img_data)
                        break
                if response_handled:
                    break

            if not response_handled:
                logger.error(f"Не удалось получить изображение от Gemini. Response: {response}")
                await message.answer(
                    "❌ Не удалось отредактировать изображение.\n\n"
                    "💡 Попробуйте:\n"
                    "Уточнить описание изменений\n"
                    "Сделать запрос проще\n"
                    "Использовать другое фото"
                )
            else:
                if any(hasattr(part, 'inline_data') for cand in response.candidates for part in cand.content.parts):
                    increment_usage(user_id, 'edit')
                    add_to_message_history(user_id, 'assistant', f'Отредактировано изображение: {prompt}')

                await message.answer(
                    "✅ Редактирование завершено!\n\n"
                    "Что дальше?\n"
                    "🎨 Сгенерировать новое\n"
                    "🖼️ Продолжить редактирование\n"
                    "📊 Посмотреть статистику",
                    reply_markup=get_user_keyboard(user.get('is_admin', False))
                )

    except Exception as e:
        logger.error(f"Ошибка редактирования для {user_id}: {str(e)}")
        await message.answer(
            f"❌ Произошла ошибка при редактировании:\n\n"
            f"`{str(e)}`\n\n"
            f"💡 Попробуйте:\n"
            f"Переформулировать описание\n"
            f"Упростить изменения\n"
            f"Попробовать позже"
        )

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=progress_msg.message_id)
    except:
        pass


@dp.message(lambda message: message.text == "🎭 Скрестить фото")
async def image_composition_start(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return
    if is_muted(user):
        await message.answer("Вы в муте.")
        return

    await state.set_state(Form.image_composition_first_image)
    await message.answer(
        "🎭 **Мастерская Скрещивания**\n\nШаг 1/3: Загрузите **первое изображение**.\n\nЭто может быть фото человека, объекта или основной фон.",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'image_composition_start')


@dp.message(Form.image_composition_first_image, lambda message: message.photo)
async def image_composition_first_image(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    update_user_activity(user_id)

    photo = message.photo[-1]
    file_id = photo.file_id
    
    await state.update_data(first_image_id=file_id)
    await state.set_state(Form.image_composition_second_image)
    
    await message.answer(
        "✅ Отлично!\n\nШаг 2/3: Теперь загрузите **второе изображение**.\n\nЭто может быть одежда, другой человек или объект, который вы хотите добавить.",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'image_composition_first_image')


@dp.message(Form.image_composition_second_image, lambda message: message.photo)
async def image_composition_second_image(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    update_user_activity(user_id)

    photo = message.photo[-1]
    file_id = photo.file_id
    
    await state.update_data(second_image_id=file_id)
    await state.set_state(Form.image_composition_prompt)
    
    await message.answer(
        "✅ Изображения на месте!\n\nШаг 3/3: Опишите, что нужно сделать.\n\nНапример: «Надень эту куртку на этого человека» или «Поставь этих людей рядом, чтобы они обнимались». Чем точнее, тем лучше!",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'image_composition_second_image')


@dp.message(Form.image_composition_prompt, lambda message: message.text)
async def image_composition_process(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Операция отменена.", reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    prompt = message.text.strip()
    if len(prompt) < 3:
        await message.answer("❌ Промпт слишком короткий. Введите подробное описание (минимум 3 символа).")
        return

    data = await state.get_data()
    first_image_id = data.get('first_image_id')
    second_image_id = data.get('second_image_id')

    if not first_image_id or not second_image_id:
        await message.answer("❌ Ошибка: не найдены изображения. Пожалуйста, начните заново.", reply_markup=get_user_keyboard(user.get('is_admin', False)))
        await state.clear()
        return

    progress_msg = await message.answer("🔄 Скрещиваю изображения...\n[░░░░░░░░░░] 0%")

    try:
        # Download images
        first_file_info = await bot.get_file(first_image_id)
        first_downloaded_file = await bot.download_file(first_file_info.file_path)
        first_img = Image.open(BytesIO(first_downloaded_file.read()))

        second_file_info = await bot.get_file(second_image_id)
        second_downloaded_file = await bot.download_file(second_file_info.file_path)
        second_img = Image.open(BytesIO(second_downloaded_file.read()))

        for percent in [0, 20, 40, 60, 80, 100]:
            bars = '█' * (percent // 10) + '░' * (10 - percent // 10)
            status_text = "Анализирую изображения..." if percent < 30 else \
                "Применяю магию..." if percent < 70 else \
                    "Финальная обработка..."
            await asyncio.sleep(1)
            await bot.edit_message_text(
                f"🔄 Скрещиваю...\n[{bars}] {percent}%\n{status_text}",
                chat_id=message.chat.id,
                message_id=progress_msg.message_id
            )

        logger.info(f"Скрещивание для {user_id}: {prompt}")

        # Call Gemini API
        response = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: model.generate_content([prompt, first_img, second_img])
        )

        if not response.candidates:
            logger.error(f"Не удалось получить изображение от Gemini (пустые кандидаты). Response: {response}")
            await message.answer(
                "❌ Не удалось скрестить изображения. Ваш запрос мог быть заблокирован политикой безопасности.\n\n"
                "💡 Попробуйте:\n"
                "Изменить формулировку, сделав ее более нейтральной\n"
                "Убрать потенциально неоднозначные слова\n"
                "Использовать другие фото"
            )
        else:
            response_handled = False
            for candidate in response.candidates:
                for part in candidate.content.parts:
                    if hasattr(part, 'inline_data') and part.inline_data and hasattr(part.inline_data, 'data'):
                        img_data = part.inline_data.data
                        img = Image.open(BytesIO(img_data))

                        img_byte_arr = BytesIO()
                        img.save(img_byte_arr, format='PNG', optimize=True, quality=85)
                        img_byte_arr.seek(0)
                        img_data = img_byte_arr.getvalue()

                        input_file = BufferedInputFile(img_data, filename='composition_image.png')

                        await send_generation_log(user_id, message.from_user.username, message.from_user.first_name, prompt, img_data)

                        await message.answer_photo(
                            input_file,
                            caption=f"🎭 Скрещивание завершено: '{prompt}'"
                        )
                        response_handled = True
                        save_user_image(user_id, f"Composition: {prompt}", img_data)
                        break
                if response_handled:
                    break

            if not response_handled:
                logger.error(f"Не удалось получить изображение от Gemini. Response: {response}")
                await message.answer(
                    "❌ Не удалось скрестить изображения.\n\n"
                    "💡 Попробуйте:\n"
                    "Уточнить описание изменений\n"
                    "Сделать запрос проще\n" "использовать другие фото"
                )
            else:
                increment_usage(user_id, 'edit') # Using 'edit' for now, can be changed
                add_to_message_history(user_id, 'assistant', f'Скрещено изображение: {prompt}')

                await message.answer(
                    "✅ Готово!\n\n"
                    "Что дальше?",
                    reply_markup=get_user_keyboard(user.get('is_admin', False))
                )

    except Exception as e:
        logger.error(f"Ошибка скрещивания для {user_id}: {str(e)}")
        await message.answer(
            f"❌ Произошла ошибка при скрещивании:\n\n"
            f"`{str(e)}`\n\n"
            f"💡 Попробуйте позже"
        )

    try:
        await bot.delete_message(chat_id=message.chat.id, message_id=progress_msg.message_id)
    except:
        pass

    await state.clear()


@dp.message(lambda message: message.text == "👤 Профиль")
async def profile(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return

    total_gen, total_edit = get_total_usage(user_id)
    usage = get_today_usage(user_id)
    gen_limit = get_daily_gen_limit(user)
    edit_limit = get_daily_edit_limit(user)
    remaining_gen = "∞" if gen_limit == float('inf') else max(0, gen_limit - usage.get('generations', 0))
    remaining_edit = "∞" if edit_limit == float('inf') else max(0, edit_limit - usage.get('edits', 0))

    profile_text = f"""
👤 Ваш профиль

📛 Username: @{user.get('username', 'Не указан')}
🆔 ID: {user_id}
💎 Подписка: {get_subscription_expiry_text(user)}

📈 Сегодня:
🎨 Генерации: {remaining_gen}/{gen_limit if gen_limit != float('inf') else '∞'}
🖼️ Редактирования: {remaining_edit}/{edit_limit if edit_limit != float('inf') else '∞'}

📊 Всего:
🎨 Генераций: {total_gen}
🖼️ Редактирований: {total_edit}

💡 Совет: Пригласите друзей для бонусных генераций!
    """

    await message.answer(profile_text, reply_markup=get_user_keyboard(user.get('is_admin', False)))
    add_to_message_history(user_id, 'user', 'profile')


@dp.message(lambda message: message.text == "🤝 Реферальная программа")
async def referral_program(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return

    bot_info = await bot.get_me()
    bot_username = bot_info.username

    settings = get_referral_settings()
    referrals_response = safe_supabase_execute(supabase.table('referrals').select('*').eq('referrer_id', user_id))
    referral_count = len(referrals_response.data) if referrals_response.data else 0
    referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"

    referral_text = f"""
🤝 Реферальная программа

👥 Ваши рефералы: {referral_count}
🎁 Награда за реферала:
🎨 +{settings['gen_reward']} генераций
🖼️ +{settings['edit_reward']} редактирований

📩 Пригласите друга:
Поделитесь ссылкой: {referral_link}

💡 Как это работает:
1. Друг переходит по ссылке и начинает использовать бота
2. Вы получаете бонусы автоматически
3. Бонусы суммируются с ежедневными лимитами

🎯 Безлимитные рефералы для всех пользователей!
    """

    await message.answer(referral_text, reply_markup=get_user_keyboard(user.get('is_admin', False)))
    add_to_message_history(user_id, 'user', 'referral_program')


@dp.message(lambda message: message.text == "🔑 Активировать ключ")
async def activate_key_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_banned(user):
        await message.answer("Вы забанены.")
        return

    await state.set_state(Form.activate_key)
    await message.answer(
        "🔑 Активация ключа подписки\n\n"
        "Введите ключ активации:\n\n"
        "💡 Ключ можно получить:\n"
        "После оплаты подписки\n"
        "От администратора @{ADMIN_USERNAME}\n"
        "В качестве бонуса\n\n"
        "❌ Для отмены введите /cancel".format(ADMIN_USERNAME=ADMIN_USERNAME),
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'activate_key')


@dp.message(Form.activate_key)
async def process_activate_key(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте ключ активации текстом.")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Активация ключа отменена.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    key = message.text.strip()
    success, duration = activate_key(user_id, key)

    if success:
        duration_text = "бессрочную" if duration is None else f"{duration} минут"
        await message.answer(
            f"✅ Ключ активирован!\n\n"
            f"🎉 Поздравляем! Вы получили {duration_text} подписку.\n"
            f"Теперь у вас увеличенные лимиты генерации!"
        )
    else:
        await message.answer(
            "❌ Недействительный ключ\n\n"
            "Проверьте:\n"
            "Правильность ввода ключа\n"
            "Не использовали ли вы уже этот ключ\n"
            "Активность ключа\n\n"
            "💡 Если проблемы сохраняются, напишите @{ADMIN_USERNAME}".format(ADMIN_USERNAME=ADMIN_USERNAME)
        )

    await message.answer("Возвращаюсь в главное меню:", reply_markup=get_user_keyboard(user.get('is_admin', False)))
    await state.clear()
    add_to_message_history(user_id, 'user', f'activate_key: {"success" if success else "failed"}')


@dp.message(lambda message: message.text == "💳 Купить подписку")
async def buy_subscription(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if is_subscription_active(user):
        await message.answer(
            f"💎 У вас уже есть активная подписка!\n\n"
            f"{get_subscription_expiry_text(user)}\n\n"
            f"Хотите продлить или изменить тариф?",
            reply_markup=get_subscription_plans_inline()
        )
    else:
        plans = get_subscription_plans()
        plans_text = "\n".join([
            f"• {plan['plan_name']}: {plan['price_rub']}₴/{plan['duration_days']}д - {plan['gen_limit'] or '∞'}🎨, {plan['edit_limit'] or '∞'}🖼️"
            for plan in plans])

        text = f"""
💎 Выберите тариф подписки:

{plans_text}

💡 Все тарифы включают:
🎨 Генерация изображений по промпту
🖼️ Редактирование фото с описанием
📦 Создание карточек для маркетплейсов
👗 Генерация модельных фото
🔄 Неиспользованные лимиты переносятся на следующий день

🚀 Выберите тариф для подробного описания:
        """
        await message.answer(text, reply_markup=get_subscription_plans_inline())
    add_to_message_history(user_id, 'user', 'buy_subscription')


@dp.message(lambda message: message.text == "❓ Помощь")
async def help_button(message: types.Message):
    await help_command(message)


@dp.message(lambda message: message.text == "✅ Проверить подписку")
async def check_subscription_button(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    channels = get_all_channels()
    if not channels:
        await message.answer(
            "✅ Нет обязательных каналов для подписки.\n\n"
            "Можете пользоваться всеми функциями бота.",
            reply_markup=get_user_keyboard(user.get('is_admin', False))
        )
        return

    if await is_subscribed(user_id):
        await message.answer(
            "✅ Вы подписаны на все каналы!\n\n"
            "Можете пользоваться всеми функциями бота.",
            reply_markup=get_user_keyboard(user.get('is_admin', False))
        )
    else:
        keyboard_buttons = []
        for channel in channels:
            channel_username = channel['channel_username']
            if not channel_username.startswith('@'):
                channel_username = f"@{channel_username}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📢 Подписаться на {channel['channel_title']}",
                    url=f"https://t.me/{channel_username[1:]}"
                )
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        channel_list = "\n".join(
            [f"• {channel['channel_title']} ({channel['channel_username']})" for channel in channels])

        await message.answer(
            f"❌ Вы не подписаны на все каналы\n\n"
            f"Для использования бота необходимо подписаться на:\n\n{channel_list}",
            reply_markup=keyboard
        )
    add_to_message_history(user_id, 'user', 'check_subscription')


@dp.message(lambda message: message.text == "🧹 Очистить историю")
async def clear_history(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    clear_message_history(user_id)
    user_context_memory.pop(user_id, None)

    await message.answer(
        "✅ История очищена!\n\n"
        "🧹 Удалены:\n"
        "История сообщений (5 последних)\n"
        "Контекстная память\n"
        "Временные настройки\n\n"
        "Бот будет генерировать изображения без учета предыдущих запросов.",
        reply_markup=get_user_keyboard(user.get('is_admin', False))
    )
    add_to_message_history(user_id, 'user', 'clear_history')


@dp.message(lambda message: message.text == "💬 Обратная связь")
async def feedback_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    await state.set_state(Form.feedback)

    feedback_text = f"""
💬 Обратная связь

Мы ценим ваше мнение! Пожалуйста, поделитесь:

⭐ Оцените бота от 1 до 5 звезд
📝 Напишите отзыв - что понравилось, что можно улучшить
🐞 Сообщите о проблеме - если что-то работает не так

📷 Наш Instagram: {INSTAGRAM_URL}

Ваши отзывы помогают нам становиться лучше!
    """

    await message.answer(feedback_text, reply_markup=get_feedback_keyboard())
    add_to_message_history(user_id, 'user', 'feedback')


@dp.message(Form.feedback)
async def process_feedback(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте ваш отзыв текстом.")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    feedback_text = message.text.strip()

    logger.info(f"Отзыв от {user_id}: {feedback_text}")

    try:
        await bot.send_message(
            ADMIN_ID,
            f"💬 Новый отзыв\n\n"
            f"👤 Пользователь: {message.from_user.full_name}\n"
            f"📛 Username: @{message.from_user.username or 'N/A'}\n"
            f"🆔 ID: {user_id}\n"
            f"📝 Текст: {feedback_text}\n"
            f"🕒 Время: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}"
        )
    except Exception as e:
        logger.error(f"Ошибка отправки отзыва админу: {e}")

    await message.answer(
        "✅ Спасибо за ваш отзыв!\n\n"
        "Мы обязательно учтем ваши пожелания для улучшения бота.",
        reply_markup=get_user_keyboard(user.get('is_admin', False))
    )
    await state.clear()
    add_to_message_history(user_id, 'user', f'feedback: {feedback_text}')


# Админские обработчики
@dp.message(lambda message: message.text == "🗝️ Админ: Создать ключ")
async def create_key_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    await state.set_state(Form.create_key)
    await message.answer(
        "🗝️ Создание ключа активации\n\n"
        "Введите длительность ключа в минутах:\n\n"
        "0 = бессрочный ключ (тариф Бесконечно)\n"
        "1440 = 1 день (24 часа)\n"
        "10080 = 1 неделя\n"
        "43200 = 1 месяц (30 дней)\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'admin_create_key')


@dp.message(Form.create_key)
async def process_create_key(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте длительность ключа в минутах (числом).")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Создание ключа отменено.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    try:
        duration = int(message.text.strip())
        if duration < 0:
            await message.answer("❌ Укажите положительное число минут (0 для бессрочного).")
            return

        key = create_key(duration)
        duration_text = "бессрочный" if duration == 0 else f"{duration} минут ({duration // 1440} дней)"

        await message.answer(
            f"✅ Ключ создан!\n\n"
            f"🔑 Ключ: `{key}`\n"
            f"⏰ Длительность: {duration_text}\n\n"
            f"⚠️ Сохраните ключ, он больше не будет показан!",
            parse_mode='Markdown'
        )

    except ValueError:
        await message.answer("❌ Укажите число минут (0 для бессрочного).")
        return

    await message.answer("Возвращаюсь в админ-панель:", reply_markup=get_user_keyboard(True))
    await state.clear()
    add_to_message_history(user_id, 'user', f'admin_created_key: {duration}min')


@dp.message(lambda message: message.text == "👥 Админ: Список юзеров")
async def list_users(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    users = get_all_users()
    if not users:
        await message.answer("📭 Нет зарегистрированных пользователей.")
        return

    active_users = len([u for u in users if not is_banned(u)])
    banned_users = len([u for u in users if is_banned(u)])
    premium_users = len([u for u in users if is_subscription_active(u)])

    await message.answer(
        f"👥 Статистика пользователей:\n\n"
        f"Всего пользователей: {len(users)}\n"
        f"Активных: {active_users}\n"
        f"Забаненных: {banned_users}\n"
        f"Премиум: {premium_users}\n\n"
        f"Выберите пользователя для действий:",
        reply_markup=get_users_inline(users, 'action')
    )
    add_to_message_history(user_id, 'user', 'admin_list_users')


@dp.message(lambda message: message.text == "🔍 Админ: Поиск юзеров")
async def search_user_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    await state.set_state(Form.search_user)
    await message.answer(
        "🔍 Поиск пользователей\n\n"
        "Введите для поиска:\n"
        "Имя пользователя\n"
        "Username (без @)\n"
        "ID пользователя\n"
        "Часть имени\n\n"
        "❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'admin_search_users')


@dp.message(Form.search_user)
async def process_search_user(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте текст для поиска пользователя.")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Поиск отменён.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    query = message.text.strip()
    if len(query) < 2:
        await message.answer("❌ Запрос слишком короткий. Введите минимум 2 символа.")
        return

    users = search_users(query)
    if not users:
        await message.answer("❌ Пользователи не найдены.")
    else:
        await message.answer(
            f"🔍 Найдено пользователей: {len(users)}\n"
            f"Выберите пользователя:",
            reply_markup=get_users_inline(users, 'action')
        )

    await state.clear()
    add_to_message_history(user_id, 'user', f'admin_searched: {query}')


@dp.message(lambda message: message.text == "📈 Админ: Аналитика")
async def analytics(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    stats = get_analytics()
    plans_text = "\n".join([
        f"• {plan['plan_name']}: {plan['price_rub']}₴, {plan['gen_limit'] or '∞'}🎨, {plan['edit_limit'] or '∞'}🖼️, {plan['duration_days']}д"
        for plan in stats['plans']])

    avg_activity = stats['total_generations'] / max(stats['total_users'], 1)

    text = f"""
📈 Аналитика VanVanAi

👥 Пользователи:
Всего: {stats['total_users']}


@dp.message(Command("loglevel"))
async def admin_set_loglevel(message: types.Message):
    user = get_user(message.from_user.id)
    if not user or not user.get('is_admin'):
        return await message.answer("🚫 Доступ запрещён.")

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Все логи", callback_data="set_log_level_ALL")],
        [InlineKeyboardButton(text="Только ошибки", callback_data="set_log_level_ERRORS")]
    ])
    await message.answer("Выберите уровень детализации логов для Telegram:", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith('set_log_level_'))
async def process_set_loglevel(callback_query: types.CallbackQuery):
    user = get_user(callback_query.from_user.id)
    if not user or not user.get('is_admin'):
        return await callback_query.answer("🚫 Доступ запрещён.", show_alert=True)

    level = callback_query.data.split('_')[-1]
    if set_log_level(level):
        # The notification is now sent from within set_log_level
        await callback_query.answer(f"Уровень логов установлен на {level}", show_alert=True)
        await callback_query.message.edit_text(f"✅ Уровень логов в Telegram изменен на: **{level}**")
    else:
        await callback_query.answer("Неверный уровень.", show_alert=True)



Активных сегодня: {stats['active_today']}
Новых за неделю: {stats['new_users_week']}
Премиум: {stats['premium_users']}
Средняя активность: {avg_activity:.1f} ген/польз

📊 Активность:
🎨 Всего генераций: {stats['total_generations']}
🖼️ Всего редактирований: {stats['total_edits']}

🤝 Реферальная программа:
Всего рефералов: {stats['total_referrals']}
Награда: +{stats['gen_reward']}🎨, +{stats['edit_reward']}🖼️

💰 Тарифы:
{plans_text}

📅 Дата отчета: {datetime.datetime.now().strftime('%d.%m.%Y %H:%M')}
    """
    await message.answer(text, reply_markup=get_user_keyboard(True))
    add_to_message_history(user_id, 'user', 'admin_analytics')


@dp.message(lambda message: message.text == "📢 Админ: Рассылка")
async def broadcast_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    users_count = len(get_all_users())
    channels_count = len(get_all_channels())

    await state.set_state(Form.broadcast)
    await message.answer(
        f"📢 Рассылка сообщений\n\n"
        f"Получатели:\n"
        f"👥 Пользователи: {users_count}\n"
        f"📰 Каналы: {channels_count}\n"
        f"Всего: {users_count + channels_count}\n\n"
        f"Введите сообщение для рассылки:\n\n"
        f"❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'admin_broadcast')


@dp.message(Form.broadcast)
async def process_broadcast(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте текст для рассылки.")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Рассылка отменена.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    message_text = message.text.strip()
    users = [u['user_id'] for u in get_all_users()]
    channels = get_all_channels()

    sent = 0
    failed = 0

    progress_msg = await message.answer(
        f"📢 Начинаю рассылку...\n\n"
        f"👥 Получателей: {len(users) + len(channels)}\n"
        f"📤 Отправлено: 0\n"
        f"❌ Ошибок: 0"
    )

    for i, uid in enumerate(users):
        try:
            await bot.send_message(uid, message_text)
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки {uid}: {str(e)}")
            failed += 1

        if (i + 1) % 10 == 0:
            await bot.edit_message_text(
                f"📢 Рассылка...\n\n"
                f"👥 Получателей: {len(users) + len(channels)}\n"
                f"📤 Отправлено: {sent}\n"
                f"❌ Ошибок: {failed}",
                chat_id=message.chat.id,
                message_id=progress_msg.message_id
            )
        await asyncio.sleep(0.1)

    for channel in channels:
        try:
            channel_username = channel['channel_username']
            if not channel_username.startswith('@'):
                channel_username = f"@{channel_username}"
            await bot.send_message(channel_username, message_text)
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки в канал {channel_username}: {str(e)}")
            failed += 1

    await bot.edit_message_text(
        f"✅ Рассылка завершена!\n\n"
        f"📤 Успешно отправлено: {sent}\n"
        f"❌ Ошибок: {failed}\n"
        f"👥 Всего получателей: {len(users) + len(channels)}",
        chat_id=message.chat.id,
        message_id=progress_msg.message_id
    )

    await message.answer("Возвращаюсь в админ-панель:", reply_markup=get_user_keyboard(True))
    await state.clear()
    add_to_message_history(user_id, 'user', f'admin_broadcast_sent: {sent} success, {failed} failed')





@dp.message(lambda message: message.text == "🎁 Админ: Изменить реф. награду")
async def set_referral_reward_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    settings = get_referral_settings()
    await state.set_state(Form.set_referral_reward)
    await message.answer(
        f"🎁 Изменение реферальных наград\n\n"
        f"Текущие награды за реферала:\n"
        f"🎨 Генераций: {settings['gen_reward']}\n"
        f"🖼️ Редактирований: {settings['edit_reward']}\n\n"
        f"Введите новые значения через пробел:\n"
        f"Формат: '5 3' (5 генераций, 3 редактирования)\n"
        f"Максимум: 50 каждой награды\n\n"
        f"❌ Для отмены введите /cancel",
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'admin_set_referral_reward')


@dp.message(Form.set_referral_reward)
async def process_set_referral_reward(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте два числа через пробел.")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение наград отменено.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    try:
        gen_reward, edit_reward = map(int, message.text.strip().split())
        if gen_reward < 0 or edit_reward < 0:
            await message.answer("❌ Награды не могут быть отрицательными.")
            return
        if gen_reward > 50 or edit_reward > 50:
            await message.answer("❌ Максимальное значение награды: 50.")
            return

        update_referral_settings(gen_reward, edit_reward)
        await message.answer(
            f"✅ Награды обновлены!\n\n"
            f"🎨 Генераций за реферала: {gen_reward}\n"
            f"🖼️ Редактирований за реферала: {edit_reward}\n\n"
            f"Изменения применяются к новым рефералам."
        )

    except ValueError:
        await message.answer("❌ Введите два числа через пробел (например: '5 3').")
        return

    await message.answer("Возвращаюсь в админ-панель:", reply_markup=get_user_keyboard(True))
    await state.clear()
    add_to_message_history(user_id, 'user', f'admin_referral_updated: {gen_reward} gen, {edit_reward} edit')


@dp.message(lambda message: message.text == "💰 Админ: Изменить цены тарифов")
async def set_subscription_prices_prompt(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    plans = get_subscription_plans()
    plans_text = "\n".join([
        f"• {plan['plan_name']}: {plan['price_rub']}₴, {plan['gen_limit'] or '∞'}🎨, {plan['edit_limit'] or '∞'}🖼️, {plan['duration_days']}д"
        for plan in plans])

    await state.set_state(Form.set_subscription_prices)
    await message.answer(
        f"💰 Изменение тарифов\n\n"
        f"Текущие тарифы:\n{plans_text}\n\n"
        f"Введите данные в формате:\n"
        f"`Название Цена Ген_лимит Ред_лимит Дни`\n\n"
        f"**Примеры:**\n"
        f"• `Минимум 149 20 10 7`\n"
        f"• `Бесконечно 1499 100 30 30`\n"
        f"• Для безлимитных лимитов укажите 'none'\n\n"
        f"**Доступные тарифы:**\n"
        f"• Минимум, Базовый, Профессиональный, Бесконечно\n\n"
        f"❌ Для отмены введите /cancel",
        parse_mode='Markdown',
        reply_markup=get_cancel_keyboard()
    )
    add_to_message_history(user_id, 'user', 'admin_set_subscription_prices')


@dp.message(Form.set_subscription_prices)
async def process_set_subscription_prices(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте данные в формате: `Название Цена Ген_лимит Ред_лимит Дни`")
        return

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if message.text == "/cancel":
        await state.clear()
        await message.answer("❌ Изменение тарифов отменено.",
                             reply_markup=get_user_keyboard(user.get('is_admin', False)))
        return

    try:
        parts = message.text.strip().split()
        if len(parts) != 5:
            raise ValueError("Неверный формат")

        plan_name, price_rub, gen_limit, edit_limit, duration_days = parts
        price_rub = int(price_rub)
        gen_limit = None if gen_limit.lower() == 'none' else int(gen_limit)
        edit_limit = None if edit_limit.lower() == 'none' else int(edit_limit)
        duration_days = int(duration_days)

        if plan_name not in ['Минимум', 'Базовый', 'Профессиональный', 'Бесконечно']:
            raise ValueError("Недопустимое название тарифа")

        if price_rub < 0 or duration_days < 0:
            await message.answer("❌ Цена и длительность не могут быть отрицательными.")
            return

        update_subscription_plan(plan_name, price_rub, gen_limit, edit_limit, duration_days)

        gen_display = gen_limit or '∞'
        edit_display = edit_limit or '∞'
        await message.answer(
            f"✅ Тариф обновлён!\n\n"
            f"💎 {plan_name}:\n"
            f"💰 Цена: {price_rub}₴\n"
            f"🎨 Лимит генераций: {gen_display}/день\n"
            f"🖼️ Лимит редактирований: {edit_display}/день\n"
            f"⏰ Длительность: {duration_days} дней"
        )

    except ValueError as e:
        await message.answer(
            "❌ Неверный формат!\n\n"
            "Введите: Название Цена Ген_лимит Ред_лимит Дни\n\n"
            "**Пример:** 'Минимум 149 20 10 7' или 'Бесконечно 1499 100 30 30'"
        )
        return

    await message.answer("Возвращаюсь в админ-панель:", reply_markup=get_user_keyboard(True))
    await state.clear()
    add_to_message_history(user_id, 'user', f'admin_plan_updated: {plan_name}')


@dp.message(lambda message: message.text == "⚡ Админ: Оптимальные цены")
async def set_optimal_prices(message: types.Message):
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    if not user.get('is_admin'):
        await message.answer("🚫 Доступ запрещён.")
        return

    optimal_plans = [
        ('Минимум', 149, 20, 10, 7),
        ('Базовый', 399, 50, 25, 30),
        ('Профессиональный', 799, 150, 75, 30),
        ('Бесконечно', 1499, 100, 30, 30)
    ]

    for plan_name, price_rub, gen_limit, edit_limit, duration_days in optimal_plans:
        update_subscription_plan(plan_name, price_rub, gen_limit, edit_limit, duration_days)

    await message.answer(
        "✅ Установлены оптимальные цены!\n\n"
        "💎 Новые тарифы:\n"
        "Минимум: 149р/7д (20 ген, 10 ред)\n"
        "Базовый: 399р/30д (50 ген, 25 ред)\n"
        "Профессиональный: 799р/30д (150 ген, 75 ред)\n"
        "Бесконечно: 1499р/30д (100 ген/час, 30 ред/час)\n\n"
        "🎯 Преимущества:\n"
        "В 2-3 раза дешевле конкурентов\n"
        "Доступные цены для всех пользователей\n"
        "Стабильная прибыль при росте аудитории",
        reply_markup=get_user_keyboard(True)
    )
    add_to_message_history(user_id, 'user', 'admin_set_optimal_prices')


# Обработчики callback-ов (кнопок)
@dp.callback_query()
async def button_handler(callback: types.CallbackQuery, state: FSMContext):
    data = callback.data
    user_id = callback.from_user.id
    logger.info(f"Нажата кнопка {data} пользователем {user_id}")

    try:
        if data == "cancel":
            await state.clear()
            user = get_user(user_id)
            await callback.message.edit_text("❌ Действие отменено.")
            await callback.message.answer(
                "Возвращаюсь в главное меню:",
                reply_markup=get_user_keyboard(user.get('is_admin', False))
            )

        elif data == "check_subscription":
            user = get_user(user_id)
            if await is_subscribed(user_id):
                await callback.message.delete()
                await callback.message.answer(
                    "✅ Спасибо за подписку! Теперь вам доступны все функции.",
                    reply_markup=get_user_keyboard(user.get('is_admin', False))
                )
                # --- Log Subscription to Telegram ---
                sub_msg = (f"**User Subscribed**\n\n"
                           f"- **ID:** `{user_id}`\n"
                           f"- **Username:** @{callback.from_user.username}")
                await send_log_message(sub_msg, level="SUCCESS", icon="✅")
                # ------------------------------------
                await callback.answer()
            else:
                await callback.answer(
                    "❌ Вы все еще не подписаны на все каналы. Пожалуйста, подпишитесь и попробуйте снова.",
                    show_alert=True
                )

        elif data.startswith('feedback_'):
            rating = data.split('_')[1]
            await callback.message.edit_text(
                f"⭐ Спасибо за оценку {rating}/5!\n\nТеперь вы можете написать текстовый отзыв:")
            await state.set_state(Form.feedback)

        elif data.startswith('back_'):
            back_to = data.split('_')[1]
            user = get_user(user_id)

            if back_to == 'main':
                await callback.message.edit_text("Возвращаюсь в главное меню:")
                await callback.message.answer(
                    "Выберите действие:",
                    reply_markup=get_user_keyboard(user.get('is_admin', False))
                )
            elif back_to == 'admin':
                await callback.message.edit_text("Возвращаюсь в админ-панель:")
                await callback.message.answer(
                    "Админ-меню:",
                    reply_markup=get_user_keyboard(True)
                )
            elif back_to == 'subscriptions':
                await show_subscription_plans(callback.message, user_id)
            elif back_to == 'users_list':
                users = get_all_users()
                await callback.message.edit_text(
                    "Выберите пользователя для действий:",
                    reply_markup=get_users_inline(users, 'action')
                )
            elif back_to.startswith('plan_'):
                plan_name = back_to.split('_')[1]
                await show_plan_details(callback.message, plan_name)

        elif data.startswith('plan_details_'):
            plan_name = data.split('_')[2]
            await show_plan_details(callback.message, plan_name)

        elif data.startswith('buy_'):
            plan_name = data.split('_')[1]
            await process_buy_subscription(callback, plan_name)

        elif data.startswith('confirm_buy_'):
            parts = data.split('_')
            plan_name = parts[2]
            target_user_id = parts[3]
            username = parts[4] if len(parts) > 4 else "Unknown"
            await process_confirm_buy(callback, plan_name, target_user_id, username)

        elif data.startswith('action_'):
            target_id = int(data.split('_')[1])
            await callback.message.edit_text(
                "Выберите действие для пользователя:",
                reply_markup=get_user_actions_inline(target_id)
            )

        elif data.startswith('stats_'):
            target_id = int(data.split('_')[1])
            await show_user_stats(callback, target_id)

        elif data.startswith('gift_'):
            plan_type = data.split('_')[1]
            target_id = int(data.split('_')[2])

            plan_mapping = {
                'min': 'минимум',
                'base': 'базовый',
                'pro': 'профессиональный',
                'unlim': 'бесконечно'
            }

            plan_name = plan_mapping.get(plan_type)
            if plan_name:
                await process_gift_subscription(callback, plan_name, target_id, user_id)
            else:
                await callback.message.edit_text("❌ Ошибка: неверный тип тарифа")

        elif data.startswith('mute_'):
            target_id = int(data.split('_')[1])
            await state.set_state(Form.mute)
            await state.update_data(target_id=target_id)
            await callback.message.edit_text(
                "🔇 Выдача мута пользователю\n\n"
                "Введите длительность мута в минутах:\n"
                "0 = снять мут\n"
                "1440 = 1 день\n"
                "10080 = 1 неделя\n\n"
                "❌ Для отмены введите /cancel"
            )

        elif data.startswith('ban_'):
            target_id = int(data.split('_')[1])
            if ban_user(user_id, target_id):
                await callback.message.edit_text("✅ Пользователь забанен.")
            else:
                await callback.message.edit_text("❌ Ошибка при бане пользователя.")

        elif data.startswith('delete_'):
            target_id = int(data.split('_')[1])
            if delete_user(user_id, target_id):
                await callback.message.edit_text("✅ Пользователь удалён.")
            else:
                await callback.message.edit_text("❌ Ошибка при удалении пользователя.")

        elif data.startswith('message_'):
            target_id = int(data.split('_')[1])
            await state.set_state(Form.message_user)
            await state.update_data(target_id=target_id)
            await callback.message.edit_text(
                "✉️ Отправка сообщения пользователю\n\n"
                "Введите сообщение:\n\n"
                "❌ Для отмены введите /cancel"
            )

        elif data.startswith('view_images_'):
            target_id = int(data.split('_')[1])
            await view_user_images(callback, target_id)

        elif data.startswith('img_prev_'):
            parts = data.split('_')
            target_id = int(parts[2])
            current_index = int(parts[3])
            await navigate_user_images(callback, target_id, current_index - 1)

        elif data.startswith('img_next_'):
            parts = data.split('_')
            target_id = int(parts[2])
            current_index = int(parts[3])
            await navigate_user_images(callback, target_id, current_index + 1)

        elif data.startswith('close_images_'):
            await callback.message.delete()

        

        else:
            await callback.answer("❌ Неизвестная команда")

    except Exception as e:
        logger.error(f"Ошибка в обработчике кнопки {data}: {str(e)}")
        await callback.answer("❌ Произошла ошибка")

    await callback.answer()
    add_to_message_history(user_id, 'user', f'callback: {data}')


@dp.message(Form.mute)
async def process_mute_user(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте длительность мута в минутах (числом).")
        return

    user_data = await state.get_data()
    target_id = user_data.get('target_id')
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    try:
        duration = int(message.text.strip())
        if duration < 0:
            await message.answer("❌ Длительность не может быть отрицательной.")
            return

        if mute_user(user_id, target_id, duration):
            duration_text = "снят" if duration == 0 else f"установлен на {duration} минут"
            await message.answer(f"✅ Мут {duration_text} для пользователя.")
        else:
            await message.answer("❌ Ошибка при установке мута.")
    except ValueError:
        await message.answer("❌ Укажите число минут (0 для снятия мута).")
        return

    await state.clear()
    await message.answer("Возвращаюсь в админ-панель:", reply_markup=get_user_keyboard(True))


@dp.message(Form.message_user)
async def process_message_user(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Пожалуйста, отправьте текст для сообщения пользователю.")
        return

    user_data = await state.get_data()
    target_id = user_data.get('target_id')
    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    try:
        await bot.send_message(
            target_id,
            f"📩 Сообщение от администратора\n\n{message.text}"
        )
        await message.answer("✅ Сообщение отправлено пользователю.")
    except Exception as e:
        logger.error(f"Ошибка отправки сообщения {target_id}: {str(e)}")
        await message.answer("❌ Не удалось отправить сообщение пользователю.")

    await state.clear()
    await message.answer("Возвращаюсь в админ-панель:", reply_markup=get_user_keyboard(True))





# Вспомогательные функции
async def show_subscription_plans(message, user_id):
    plans = get_subscription_plans()
    plans_text = "\n".join([
        f"• {plan['plan_name']}: {plan['price_rub']}₴/{plan['duration_days']}д - {plan['gen_limit'] or '∞'}🎨, {plan['edit_limit'] or '∞'}🖼️"
        for plan in plans])

    text = f"""
💎 Выберите тариф подписки:

{plans_text}

💡 Все тарифы включают:
🎨 Генерация изображений по промпту
🖼️ Редактирование фото с описанием
📦 Создание карточек для маркетплейсов
👗 Генерация модельных фото
🔄 Неиспользованные лимиты переносятся на следующий день

🎯 В 2-3 раза дешевле конкурентов!

Выберите тариф для подробного описания:
    """
    await message.answer(text, reply_markup=get_subscription_plans_inline())


async def show_plan_details(message, plan_name):
    plans = get_subscription_plans()
    plan = next((p for p in plans if p['plan_name'].lower() == plan_name), None)

    if not plan:
        await message.answer("❌ Тариф не найден.")
        return

    gen_display = plan['gen_limit'] or '∞'
    edit_display = plan['edit_limit'] or '∞'
    accumulation_note = "✓ Неиспользованные лимиты копятся" if plan['duration_days'] else ""

    competitor_price = {
        'минимум': '300-500р',
        'базовый': '800-1200р',
        'профессиональный': '1500-2000р',
        'бесконечно': '3000-5000р'
    }

    economy = competitor_price.get(plan_name, '')

    text = f"""
💎 Тариф: {plan['plan_name']}

💰 Стоимость: {plan['price_rub']}₴
⏰ Длительность: {plan['duration_days']} дней
🎨 Лимит генераций: {gen_display} в день
🖼️ Лимит редактирований: {edit_display} в день
{accumulation_note}

💪 Экономия: В 2-3 раза дешевле конкурентов ({economy})

✨ Включает все возможности:
Генерация изображений по текстовым промптам
Редактирование существующих фото
Создание карточек для маркетплейсов
Генерация модельных фото
Приоритетная обработка запросов

🚀 Идеально подходит для {{
    'начала работы с ИИ' if plan_name == 'минимум' else
    'регулярного использования' if plan_name == 'базовый' else
    'профессиональной работы' if plan_name == 'профессиональный' else
    'коммерческого использования и агентств'
    }}
    """
    await message.answer(text, reply_markup=get_plan_details_inline(plan_name))


async def process_buy_subscription(callback, plan_name):
    user_id = callback.from_user.id
    username = callback.from_user.username or callback.from_user.first_name or str(user_id)

    plans = get_subscription_plans()
    plan = next((p for p in plans if p['plan_name'].lower() == plan_name), None)

    if not plan:
        await callback.message.answer("❌ Тариф не найден.")
        return

    gen_display = plan['gen_limit'] or '∞'
    edit_display = plan['edit_limit'] or '∞'

    text = f"""
💳 Подтверждение выбора

✅ Вы выбрали тариф: {plan['plan_name']}

📋 Детали:
💰 Стоимость: {plan['price_rub']}₴
⏰ Длительность: {plan['duration_days']} дней
🎨 Лимит генераций: {gen_display}/день
🖼️ Лимит редактирований: {edit_display}/день

💡 После оплаты:
1. Напишите @{ADMIN_USERNAME}
2. Предоставьте скриншот оплаты
3. Получите ключ активации

⚡ Активация происходит мгновенно!
    """

    await callback.message.answer(text, reply_markup=get_buy_subscription_inline(user_id, username, plan_name))


async def process_confirm_buy(callback, plan_name, target_user_id, username):
    plans = get_subscription_plans()
    plan = next((p for p in plans if p['plan_name'].lower() == plan_name), None)

    if not plan:
        await callback.message.answer("❌ Тариф не найден.")
        return

    plan_display = f"{plan['plan_name']} ({plan['price_rub']}₴/{plan['duration_days']}дней)"

    await bot.send_message(
        ADMIN_ID,
        f"🛒 Новый заказ!\n\n"
        f"👤 Пользователь: {username} (ID: {target_user_id})\n"
        f"💎 Тариф: {plan_display}\n"
        f"🎨 Лимиты: {plan['gen_limit'] or '∞'} ген, {plan['edit_limit'] or '∞'} ред\n"
        f"💰 Сумма: {plan['price_rub']}₴\n"
        f"⏰ Ожидает оплаты и активации"
    )

    text = f"""
✅ Заявка отправлена!

📋 Детали заказа:
💎 Тариф: {plan['plan_name']}
💰 Стоимость: {plan['price_rub']}₴
⏰ Длительность: {plan['duration_days']} дней
🎨 Лимит генераций: {plan['gen_limit'] or '∞'}/день
🖼️ Лимит редактирований: {plan['edit_limit'] or '∞'}/день

📞 Дальнейшие действия:
1. Напишите @{ADMIN_USERNAME} для оплаты
2. Предоставьте скриншот оплаты
3. Получите ключ активации

⚡ Обычно активация занимает менее 5 минут!
    """

    await callback.message.answer(text)
    await callback.message.answer(
        "💬 Написать админу для оплаты:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="📞 Написать админу",
                url=f"https://t.me/{ADMIN_USERNAME[1:]}?text=Хочу%20купить%20тариф%20{plan['plan_name'].replace(' ', '%20')}"
            )
        ]])
    )


async def process_gift_subscription(callback, plan_name, target_id, admin_id):
    success = await gift_subscription(admin_id, target_id, plan_name)

    if success:
        await callback.message.edit_text(
            f"✅ Подписка подарена!\n\n"
            f"💎 Тариф: {plan_name}\n"
            f"👤 Пользователь: {target_id}"
        )
    else:
        await callback.message.edit_text("❌ Ошибка при выдаче подписки. Тариф не найден.")


async def show_user_stats(callback: types.CallbackQuery, target_id: int):
    user = get_user(target_id)
    total_gen, total_edit = get_total_usage(target_id)
    usage = get_today_usage(target_id)

    referrals_response = safe_supabase_execute(supabase.table('referrals').select('*').eq('referrer_id', target_id))
    referral_count = len(referrals_response.data) if referrals_response.data else 0

    username = user.get('username') or user.get('first_name') or f"ID: {target_id}"

    stats_text = f"""
📊 Статистика пользователя

👤 {username} (ID: {target_id})
🔑 Статус: {'🟢 Активен' if not is_banned(user) else '🔴 Забанен'}
🔇 Мут: {'✅ Нет' if not is_muted(user) else '🔇 Есть'}
💎 Подписка: {'✅ Активна' if is_subscription_active(user) else '❌ Нет'}

📈 Активность:
🎨 Всего генераций: {total_gen}
🖼️ Всего редактирований: {total_edit}
🎨 Сегодня: {usage.get('generations', 0)}
🖼️ Сегодня: {usage.get('edits', 0)}

👥 Рефералы:
Приглашено: {referral_count}

📅 Регистрация: {user.get('created_at', 'Неизвестно')[:10]}
    """

    await callback.message.answer(stats_text, reply_markup=get_user_actions_inline(target_id))


async def view_user_images(callback: types.CallbackQuery, target_id: int):
    images = get_user_images(target_id, limit=20)

    if not images:
        await callback.message.answer("📭 У пользователя нет сохраненных изображений.")
        return

    await show_user_image(callback, target_id, 0, len(images))


async def show_user_image(callback: types.CallbackQuery, target_id: int, index: int, total: int):
    images = get_user_images(target_id, limit=20)

    if index < 0 or index >= len(images):
        return

    image_url = images[index]['image_url']
    prompt = images[index]['prompt']
    created_at = images[index]['created_at'][:16]

    caption = f"🖼️ Изображение {index + 1}/{total}\n\n📝 Запрос: {prompt}\n📅 Создано: {created_at}"

    await callback.message.answer_photo(
        image_url,
        caption=caption,
        reply_markup=get_images_navigation_inline(target_id, index, total)
    )


async def navigate_user_images(callback: types.CallbackQuery, target_id: int, new_index: int):
    images = get_user_images(target_id, limit=20)

    if new_index < 0:
        new_index = 0
    elif new_index >= len(images):
        new_index = len(images) - 1

    try:
        await callback.message.delete()
    except:
        pass

    await show_user_image(callback, target_id, new_index, len(images))


# Обработчик любых текстовых сообщений (как промптов для генерации)
@dp.message()
async def handle_text_as_prompt(message: types.Message, state: FSMContext):
    if not message.text:
        return  # Игнорируем нетекстовые сообщения

    user_id = message.from_user.id
    user = get_user(user_id)
    update_user_activity(user_id)

    current_state = await state.get_state()
    if current_state:
        return

    if is_banned(user):
        await message.answer("Вы забанены.")
        return
    if is_muted(user):
        await message.answer("Вы в муте.")
        return

    channels = get_all_channels()
    if channels and not await is_subscribed(user_id):
        keyboard_buttons = []
        for channel in channels:
            channel_username = channel['channel_username']
            if not channel_username.startswith('@'):
                channel_username = f"@{channel_username}"
            keyboard_buttons.append([
                InlineKeyboardButton(
                    text=f"📢 Подписаться на {channel['channel_title']}",
                    url=f"https://t.me/{channel_username[1:]}"
                )
            ])

        keyboard_buttons.append([
            InlineKeyboardButton(text="✅ Я подписался", callback_data="check_subscription")
        ])

        keyboard = InlineKeyboardMarkup(inline_keyboard=keyboard_buttons)

        channel_list = "\n".join(
            [f"• {channel['channel_title']} ({channel['channel_username']})" for channel in channels])

        await message.answer(
            f"📢 Для использования бота необходимо подписаться на наши каналы:\n\n{channel_list}\n\nПосле подписки нажмите '✅ Я подписался'",
            reply_markup=keyboard
        )
        return

    if is_subscription_active(user) and user.get('daily_gen_limit') == 100:
        hourly_usage = get_hourly_usage(user_id)
        if hourly_usage >= 100:
            await message.answer(
                "⚠️ Вы достигли часового лимита генераций (100/час).\n\n"
                "Пожалуйста, подождите немного перед следующей генерацией.",
                reply_markup=get_user_keyboard(user.get('is_admin', False))
            )
            return

    gen_limit = get_daily_gen_limit(user)
    usage = get_today_usage(user_id)

    if usage.get('generations', 0) >= gen_limit:
        await message.answer(
            f"⚠️ Лимит генераций исчерпан!\n\n"
            f"Использовано: {usage.get('generations', 0)}/{gen_limit if gen_limit != float('inf') else '∞'}\n\n"
            f"💳 Выгодные тарифы для увеличения лимитов:\n"
            f"Минимум: 149р/7д (20 ген/день)\n"
            f"Базовый: 399р/30д (50 ген/день)\n"
            f"Профессиональный: 799р/30д (150 ген/день)\n\n"
            f"Нажмите 'Купить подписку' для выбора тарифа!",
            reply_markup=get_user_keyboard(user.get('is_admin', False))
        )
        return

    prompt = message.text.strip()
    if len(prompt) < 2:
        await message.answer("❌ Сообщение слишком короткое для генерации.")
        return

    await generate_image(message, state)


async def main():
    try:
        await send_log_message("Bot started polling.", level="INFO", icon="🚀")
        await dp.start_polling(bot)
    finally:
        await send_log_message("Bot stopped.", level="WARNING", icon="🛑")
        await bot.session.close()
        await close_log_bot_session()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped by user.")

