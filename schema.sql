-- ВНИМАНИЕ: Этот скрипт полностью удалит все ваши существующие таблицы и данные.
-- Все пользователи, подписки и история генераций будут потеряны.

-- Удаляем существующие таблицы
DROP TABLE IF EXISTS public.images;
DROP TABLE IF EXISTS public.channels;
DROP TABLE IF EXISTS public.keys;
DROP TABLE IF EXISTS public.referrals;
DROP TABLE IF EXISTS public.usage;
DROP TABLE IF EXISTS public.referral_settings;
DROP TABLE IF EXISTS public.subscription_plans;
DROP TABLE IF EXISTS public.users;

-- Создаем таблицу users
CREATE TABLE public.users (
    user_id BIGINT PRIMARY KEY,
    username TEXT,
    first_name TEXT,
    is_admin BOOLEAN DEFAULT FALSE,
    subscription_expires_at TIMESTAMPTZ,
    banned BOOLEAN DEFAULT FALSE,
    muted_until TIMESTAMPTZ,
    daily_gen_limit INTEGER DEFAULT 3,
    daily_edit_limit INTEGER DEFAULT 1,
    referral_gen_bonus INTEGER DEFAULT 0,
    referral_edit_bonus INTEGER DEFAULT 0,
    monthly_generations INTEGER DEFAULT 0,
    total_generations INTEGER DEFAULT 0,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_activity TIMESTAMPTZ
);

-- Создаем таблицу subscription_plans
CREATE TABLE public.subscription_plans (
    plan_name TEXT PRIMARY KEY,
    price_rub INTEGER,
    gen_limit INTEGER,
    edit_limit INTEGER,
    duration_days INTEGER,
    monthly_limit INTEGER,
    updated_at TIMESTAMPTZ
);

-- Создаем таблицу referral_settings
CREATE TABLE public.referral_settings (
    id SERIAL PRIMARY KEY,
    gen_reward INTEGER,
    edit_reward INTEGER,
    updated_at TIMESTAMPTZ
);

-- Создаем таблицу usage
CREATE TABLE public.usage (
    user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
    date DATE,
    generations INTEGER DEFAULT 0,
    edits INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, date)
);

-- Создаем таблицу referrals
CREATE TABLE public.referrals (
    referrer_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
    referred_id BIGINT UNIQUE REFERENCES public.users(user_id) ON DELETE CASCADE,
    created_at TIMESTAMPTZ DEFAULT now(),
    PRIMARY KEY (referrer_id, referred_id)
);

-- Создаем таблицу keys
CREATE TABLE public.keys (
    key UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    used BOOLEAN DEFAULT FALSE,
    duration_minutes INTEGER,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Создаем таблицу channels
CREATE TABLE public.channels (
    channel_username TEXT PRIMARY KEY,
    channel_title TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Создаем таблицу images
CREATE TABLE public.images (
    image_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id BIGINT REFERENCES public.users(user_id) ON DELETE CASCADE,
    prompt TEXT,
    image_url TEXT,
    created_at TIMESTAMPTZ DEFAULT now()
);

-- Вставляем настройки реферальной программы по умолчанию
INSERT INTO public.referral_settings (gen_reward, edit_reward, updated_at)
VALUES (3, 3, now());
