"""
Раздел "Профиль".

Показывает игроку сводную карточку:
    - имя/юзернейм/ID в Telegram,
    - дату первого захода в бота,
    - баланс Pn (через shop.get_balance),
    - уровень и опыт (шкала XP, растущая по уровням),
    - репутацию — два независимых счётчика, 🔥 красный и 🔥 синий.

Опыт (XP):
    Хранится как накопленный total (users.xp), уровень считается на
    лету функцией level_from_xp() по растущей кривой (см. XP_BASE/
    XP_GROWTH/MAX_LEVEL ниже). Кривая ограничена сверху: максимум —
    150 уровень, суммарно на него нужно ≈500 000 XP. Каждый порог
    округляется до "красивого" числа (_round_nice) — шаг округления
    растёт вместе с уровнем, чтобы не было значений вроде 267.

    Начислять опыт умеет add_xp(user_id, amount) — публичная точка
    входа для ДРУГИХ модулей. Сейчас её вызывают:
      - garden.py — за каждый созревший и собранный фрукт (+50–80 XP,
        см. garden._collect_plot_if_matches);
      - bakery.py — за каждую готовую выпечку (+250–450 XP, см.
        bakery._collect_oven_if_matches).
    Оба модуля импортируют prof локально, внутри функции (а не в
    начале файла), — иначе получился бы цикл импортов: prof → shop →
    bakery → garden → prof. И тот, и другой вызывают add_xp уже ПОСЛЕ
    выхода из своего "async with database.user_lock(user_id):" — сам
    add_xp тоже берёт этот лок, а он не реентерабельный (обычный
    asyncio.Lock), так что начисление опыта изнутри чужого лока на
    того же user_id было бы дедлоком.

Репутация (🔥 красная / 🔥 синяя):
    Два отдельных счётчика вместо одной шкалы — просто числа рядом со
    своим огоньком, без процентов и титулов. Начисление под них пока
    не подключено нигде в боте — в будущем за них планируются подарки
    (см. add_reputation_red / add_reputation_blue: заготовки на
    будущее, сейчас не вызываются).

Хранение:
    Общая база данных бота (см. database.py) — данные лежат прямо в
    таблице users (её же ведёт admin.UserTrackingMiddleware: username/
    first_name/first_seen туда пишутся на КАЖДЫЙ апдейт ещё до роутеров,
    так что строка для игрока к моменту открытия профиля уже есть).

Подключение в main.py:
    import prof
    dp.include_router(prof.router)   # порядок относительно других не важен

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import asyncio
import datetime
import html

import aiosqlite
from aiogram import BaseMiddleware, F, Router
from aiogram.enums import MessageEntityType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.utils.text_decorations import html_decoration

import database
import shop

router = Router(name="prof")


# ==========================
#   КАСТОМНЫЕ ЭМОДЗИ
# ==========================
#
# Рендерятся только там, где Telegram поддерживает HTML-разметку/entities
# (текст сообщений) — тег <tg-emoji> в тексте кнопки (KeyboardButton) или
# в toast-алерте (callback.answer(show_alert=True)) показался бы как
# сырой текст (см. тот же нюанс в shop.py, CURRENCY / CURRENCY_PLAIN).
# Поэтому BUTTON_TEXT здесь — просто "Профиль"/"Profile" без эмодзи: сам
# кастомный эмодзи профиля (тот же id, что и у CE_PROFILE выше) вешается
# на кнопку через отдельный параметр KeyboardButton — icon_custom_emoji_id
# (см. main.py, PROFILE_EMOJI_ID / main_menu_keyboard) — это единственный
# способ показать кастомный эмодзи именно НА кнопке.

def _ce(emoji_id: str, glyph: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'


EMOJI_PROFILE_ID = "5413450253883944453"
EMOJI_LEVEL_ID = "5408869739982246969"
EMOJI_NAME_ID = "5290026118001211063"
EMOJI_USERNAME_ID = "5298737505678407110"
EMOJI_ID_ID = "5282843764451195532"
EMOJI_JOINED_ID = "5413879192267805083"
EMOJI_REP_RED_ID = "5852868430952144622"
EMOJI_REP_BLUE_ID = "5859548930458523065"
# Строка "Статус" в профиле — показывается, только если у игрока
# активна привилегия (donate.py: PRIVILEGE_TIERS), см. _build_profile_text.
EMOJI_STATUS_ID = "5907025791006283345"
EMOJI_CRYSTAL_ID = "5251273203615031474"
EMOJI_GIFT_ID = "5359664288241829619"
# Кастомный эмодзи для кнопки "Купить себе" — передаётся через параметр
# icon_custom_emoji_id у InlineKeyboardButton (Bot API 9.4+), а не через
# тег <tg-emoji> в тексте кнопки: у inline-кнопок текст не поддерживает
# HTML/entities, зато есть отдельный параметр icon_custom_emoji_id,
# показывающий кастомный эмодзи перед текстом (см. _gift_info_keyboard).
# Как и icon у профиля в main.py (PROFILE_EMOJI_ID), но для inline-кнопок.
EMOJI_GIFT_BUY_ID = "5222113468051629260"
EMOJI_PRICE_ID = "5287231198098117669"
EMOJI_GIVES_ID = "5305699699204837855"
EMOJI_BACK_ID = "6039539366177541657"
# Пагинация в списке подарков: 🔜 — вперёд (следующая страница),
# 🔙 — назад (предыдущая страница).
EMOJI_PAGE_NEXT_ID = "5253767677670862169"
EMOJI_PAGE_PREV_ID = "5255703720078879038"

# Раздел "Настройки" (см. секцию "экран Настройки" ниже): те же принципы,
# что и у EMOJI_GIFT_BUY_ID выше — на inline-кнопках эмодзи только через
# icon_custom_emoji_id, а в тексте сообщений (title/name_prompt) — через
# тег <tg-emoji>, т.е. через _ce()/CE_ константы.
EMOJI_SETTINGS_ID = "5341715473882955310"
EMOJI_EDIT_NAME_ID = "5370951118698339120"
EMOJI_LANGUAGE_ID = "5447410659077661506"
# Кнопки-ссылки "Новости"/"Наш чат" в настройках — url ведёт на канал/чат,
# заданный из админки (см. admin.py: LINK_TITLES/get_link/set_link).
# Иконки — тем же способом, что и остальные inline-кнопки настроек:
# icon_custom_emoji_id, а не тег <tg-emoji> в тексте (на кнопках он не
# рендерится, см. main.py: TransferFlow/_TRANSFER_COIN_ICON_ID).
EMOJI_NEWS_ID = "5424818078833715060"
EMOJI_CHAT_ID = "5443038326535759644"

# Раздел "Друзья" (реферальная система) — те же принципы, что и у
# остальных кастомных эмодзи выше: EMOJI_FRIENDS_ID вешается на кнопку
# профиля через icon_custom_emoji_id (inline-кнопки не поддерживают
# HTML в тексте), остальные — через тег <tg-emoji> (_ce/CE_ константы),
# т.к. используются внутри текста самого раздела/уведомлений.
EMOJI_FRIENDS_ID = "5332724926216428039"
EMOJI_REGULAR_ID = "5262690351969215936"
EMOJI_LINK_ID = "5271604874419647061"
EMOJI_COIN_ID = "5449418135381759397"
EMOJI_INVITED_ID = "6033125983572201397"
EMOJI_PREMIUM_ID = "5267500801240092311"
EMOJI_PARTY_ID = "5461151367559141950"

CE_FRIENDS = _ce(EMOJI_FRIENDS_ID, "📇")
CE_REGULAR = _ce(EMOJI_REGULAR_ID, "📃")
CE_LINK = _ce(EMOJI_LINK_ID, "🔗")
CE_COIN = _ce(EMOJI_COIN_ID, "🪙")
CE_INVITED = _ce(EMOJI_INVITED_ID, "👥")
CE_PREMIUM = _ce(EMOJI_PREMIUM_ID, "⭐")
CE_PARTY = _ce(EMOJI_PARTY_ID, "🎉")

CE_PROFILE = _ce(EMOJI_PROFILE_ID, "🤑")
CE_LEVEL = _ce(EMOJI_LEVEL_ID, "⭐")
CE_NAME = _ce(EMOJI_NAME_ID, "🤩")
CE_USERNAME = _ce(EMOJI_USERNAME_ID, "🌟")
CE_ID = _ce(EMOJI_ID_ID, "🖥")
CE_JOINED = _ce(EMOJI_JOINED_ID, "🗓")
CE_REP_RED = _ce(EMOJI_REP_RED_ID, "🔥")
CE_REP_BLUE = _ce(EMOJI_REP_BLUE_ID, "🔥")
CE_STATUS = _ce(EMOJI_STATUS_ID, "⭐")

# Кастомные — для обычных сообщений/edit_text (HTML рендерится).
CE_CRYSTAL = _ce(EMOJI_CRYSTAL_ID, "💎")
CE_GIFT = _ce(EMOJI_GIFT_ID, "🎁")
CE_PRICE = _ce(EMOJI_PRICE_ID, "💰")
CE_GIVES = _ce(EMOJI_GIVES_ID, "🍀")
CE_SETTINGS = _ce(EMOJI_SETTINGS_ID, "⚙️")
CE_EDIT_NAME = _ce(EMOJI_EDIT_NAME_ID, "✏️")
CE_LANGUAGE = _ce(EMOJI_LANGUAGE_ID, "🌐")
# Обычные юникод-эмодзи — для мест, которые могут уйти в toast-алерт
# (callback.answer(show_alert=True)): там HTML-теги вроде <tg-emoji>
# не рендерятся и показались бы как сырой текст.
CE_CRYSTAL_PLAIN = "💎"


# ==========================
#   ОПЫТ И УРОВНИ
# ==========================
#
# Опыта на level+1 нужно XP_BASE * XP_GROWTH**(level-1), округлённо до
# "красивого" числа (_round_nice — шаг округления растёт вместе с
# уровнем, поэтому никогда не будет порога вроде 267, только 100/110/
# 250/1200 и т.п.). Кривая чисто математическая (без захардкоженной
# таблицы), но заканчивается на MAX_LEVEL — дальше расти некуда.
# Константы подобраны так, чтобы суммарно на MAX_LEVEL уровень
# требовалось ≈500 000 XP.

XP_BASE = 100
XP_GROWTH = 1.0354
MAX_LEVEL = 150


def _round_nice(value: float) -> int:
    """Округляет требование до круглого числа. Шаг округления растёт
    вместе с самим значением, чтобы ранние уровни не слипались в одно
    и то же число, а поздние не пестрили лишними цифрами."""
    if value < 200:
        step = 10
    elif value < 1000:
        step = 20
    elif value < 3000:
        step = 50
    elif value < 8000:
        step = 100
    else:
        step = 200
    return int(round(value / step) * step)


def _xp_for_level(level: int) -> int:
    """Сколько опыта нужно НАБРАТЬ, чтобы перейти с level на level+1.
    Для level >= MAX_LEVEL — некуда расти дальше, возвращает 0."""
    if level >= MAX_LEVEL:
        return 0
    return _round_nice(XP_BASE * (XP_GROWTH ** (level - 1)))


def level_from_xp(xp: int) -> tuple[int, int, int]:
    """Переводит суммарный опыт в (уровень, опыт_внутри_уровня,
    опыт_нужен_до_следующего). На MAX_LEVEL прогресс всегда "0 из 0"
    (максимум уже достигнут, дальше копить нечего)."""
    level = 1
    remaining = max(0, xp)
    while level < MAX_LEVEL:
        needed = _xp_for_level(level)
        if remaining < needed:
            return level, remaining, needed
        remaining -= needed
        level += 1
    return MAX_LEVEL, 0, 0


def _progress_bar(current: int, total: int, length: int = 10) -> str:
    if total <= 0:
        filled = length
    else:
        filled = round(length * current / total)
    filled = max(0, min(length, filled))
    return "▰" * filled + "▱" * (length - filled)


# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

BUTTON_TEXT = {
    "ru": "Профиль",
    "en": "Profile",
}

TEXTS = {
    "ru": {
        "title": f"{CE_PROFILE} <b>Профиль</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "name_line": f"{CE_NAME} <b>Имя:</b> {{name}}",
        "username_line": f"{CE_USERNAME} <b>Юзернейм:</b> {{username}}",
        "no_username": "<i>не указан</i>",
        "id_line": f"{CE_ID} <b>ID:</b> <code>{{user_id}}</code>",
        "joined_line": f"{CE_JOINED} <b>В боте с:</b> {{date}}",
        "status_line": f"{CE_STATUS} <b>Статус:</b> <b>{{name}}</b>",
        "level_line": f"{CE_LEVEL} <b>Уровень {{level}}</b>",
        "xp_line": "{bar} <code>{current}/{needed}</code> XP",
        "reputation_line": f"{CE_REP_RED} <b>{{red}}</b>      {CE_REP_BLUE} <b>{{blue}}</b>",
        "balance_line": f"{shop.CE_BALANCE} <b>Баланс:</b> <b>{{balance}} {shop.CURRENCY}</b>",
        "crystals_line": f"{CE_CRYSTAL} <b>Кристаллы:</b> <b>{{crystals}}</b>",
        "gifts_button": "Подарки",
        "friends_button": "Друзья",
        "settings_button": "Настройки",
    },
    "en": {
        "title": f"{CE_PROFILE} <b>Profile</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "name_line": f"{CE_NAME} <b>Name:</b> {{name}}",
        "username_line": f"{CE_USERNAME} <b>Username:</b> {{username}}",
        "no_username": "<i>not set</i>",
        "id_line": f"{CE_ID} <b>ID:</b> <code>{{user_id}}</code>",
        "joined_line": f"{CE_JOINED} <b>In the bot since:</b> {{date}}",
        "status_line": f"{CE_STATUS} <b>Status:</b> <b>{{name}}</b>",
        "level_line": f"{CE_LEVEL} <b>Level {{level}}</b>",
        "xp_line": "{bar} <code>{current}/{needed}</code> XP",
        "reputation_line": f"{CE_REP_RED} <b>{{red}}</b>      {CE_REP_BLUE} <b>{{blue}}</b>",
        "balance_line": f"{shop.CE_BALANCE} <b>Balance:</b> <b>{{balance}} {shop.CURRENCY}</b>",
        "crystals_line": f"{CE_CRYSTAL} <b>Crystals:</b> <b>{{crystals}}</b>",
        "gifts_button": "Gifts",
        "friends_button": "Friends",
        "settings_button": "Settings",
    },
}


# --- экран "Настройки" ---
#
# Две настройки: язык интерфейса (RU/EN) и отображаемое имя в профиле.
# Язык хранится там же, где и весь остальной онбординг (users.lang, см.
# database.py) — переключение здесь использует database.save_lang,
# отдельную от save_onboarding (та пишет язык лишь один раз, при самом
# первом /start, и заодно взводит флаг onboarded). Имя — в users.display_name
# (database.py: USERS_PROFILE_COLUMNS, database.save_display_name).

# Флаг + название языка для кнопок — буквально те же подписи, что и в
# main.py (LANGUAGES), но без импорта main на уровне модуля: main.py сам
# импортирует prof.py (dp.include_router(prof.router)), так что обратный
# import main здесь наверху дал бы цикл. Локальный `import main` внутри
# самого хендлера смены языка (см. change_language ниже) — безопасен, тот
# же приём, что уже используют panda.py/admin.py для main_menu_keyboard().
SETTINGS_LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
}

# Ограничение на длину нового имени — считается по message.text (видимым
# символам), а не по итоговому HTML: кастомный эмодзи в message.text — это
# один символ-заглушка (сам <tg-emoji>-тег появляется только в html_text),
# так что лимит остаётся честным "визуальным" ограничением независимо от
# того, сколько кастомных эмодзи или другого форматирования в имени.
NAME_MAX_LENGTH = 14

# Имя игрока — не только в его собственном профиле, но и там, где его
# видят ДРУГИЕ (лидерборд в leaders.py, уведомление получателю подарка
# ниже в этом файле). Поэтому в HTML имени разрешено только чистое
# визуальное форматирование, применённое штатной панелью Telegram
# (жирный/курсив/подчёркнутый/зачёркнутый/спойлер/кастомный эмодзи).
# Сущности, порождающие кликабельные/навигационные элементы или
# крупные блоки разметки (ссылка, упоминание, цитата, код), сюда
# намеренно НЕ входят — иначе игрок мог бы замаскировать под своё имя
# кликабельную ссылку (например, на фишинговый сайт) и она бы
# показывалась всем остальным игрокам в публичном лидерборде.
_NAME_ALLOWED_ENTITY_TYPES = {
    MessageEntityType.BOLD,
    MessageEntityType.ITALIC,
    MessageEntityType.UNDERLINE,
    MessageEntityType.STRIKETHROUGH,
    MessageEntityType.SPOILER,
    MessageEntityType.CUSTOM_EMOJI,
}


def _sanitize_name_html(message: Message) -> str:
    """Строит безопасный HTML для отображаемого имени: как и
    message.html_text, экранирует сырой текст и сохраняет визуальное
    форматирование/кастомные эмодзи, но отбрасывает любые сущности вне
    _NAME_ALLOWED_ENTITY_TYPES (ссылки, упоминания, код, цитаты и т.п.),
    прежде чем передавать их в html_decoration — тот же helper, которым
    aiogram строит message.html_text под капотом."""
    entities = message.entities or []
    safe_entities = [e for e in entities if e.type in _NAME_ALLOWED_ENTITY_TYPES]
    return html_decoration.unparse(message.text or "", safe_entities)

SETTINGS_TEXTS = {
    "ru": {
        "title": f"{CE_SETTINGS} <b>Настройки</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "language_label": f"{CE_LANGUAGE} <b>Язык интерфейса</b>",
        "language_hint": "<i>Выберите язык — все разделы бота сразу переключатся на него.</i>",
        "name_button": "Изменить имя",
        "back_button": "Назад",
        "cancel_button": "✖️ Отмена",
        "news_button": "Новости",
        "chat_button": "Наш чат",
        "lang_changed": "✅ <b>Язык изменён на {name}</b>",
        "name_prompt": (
            f"{CE_EDIT_NAME} <b>Новое имя для профиля</b>\n\n"
            "<i>Отправьте текст — до {limit} символов, в одну строку. "
            "Поддерживаются кастомные эмодзи (Telegram Premium) и обычное "
            "форматирование (жирный, курсив) — можно отправить сообщение "
            "с ними, оно сохранится как есть. Это меняет только то, как имя "
            "выглядит в профиле бота, — ваше настоящее имя в Telegram не "
            "затрагивается.</i>"
        ),
        "name_invalid": "❌ <i>Имя не может быть пустым или состоять из нескольких строк. Попробуйте ещё раз.</i>",
        "name_too_long": "❌ <i>Слишком длинное имя — максимум {limit} символов. Попробуйте ещё раз.</i>",
        "name_changed": "✅ <b>Имя обновлено!</b>",
    },
    "en": {
        "title": f"{CE_SETTINGS} <b>Settings</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "language_label": f"{CE_LANGUAGE} <b>Interface language</b>",
        "language_hint": "<i>Choose a language — every section of the bot switches to it right away.</i>",
        "name_button": "Change name",
        "back_button": "Back",
        "cancel_button": "✖️ Cancel",
        "news_button": "News",
        "chat_button": "Our chat",
        "lang_changed": "✅ <b>Language changed to {name}</b>",
        "name_prompt": (
            f"{CE_EDIT_NAME} <b>New profile name</b>\n\n"
            "<i>Send a text message — up to {limit} characters, single line. "
            "Custom emoji (Telegram Premium) and regular formatting (bold, "
            "italic) are supported — send a message with them and it will be "
            "kept as is. This only changes how your name looks inside the "
            "bot's profile — your real Telegram name is not affected.</i>"
        ),
        "name_invalid": "❌ <i>The name can't be empty or span multiple lines. Try again.</i>",
        "name_too_long": "❌ <i>Name is too long — {limit} characters max. Try again.</i>",
        "name_changed": "✅ <b>Name updated!</b>",
    },
}


# --- экран "Подарки" ---

PAGE_SIZE = 5

# Два независимых дневных лимита на огонёк от подарков — DAILY_GIFT_LIMIT
# на покупку себе и DAILY_GIFT_LIMIT на отправку другу (см. секцию
# "ДНЕВНЫЕ ЛИМИТЫ НА ОГОНЁК" ниже). Задан здесь, а не рядом с той
# секцией, т.к. нужен уже в текстах ошибок GIFTS_TEXTS чуть ниже.
DAILY_GIFT_LIMIT = 10_000

GIFTS_TEXTS = {
    "ru": {
        "title": f"{CE_GIFT} <b>Подарки</b>",
        "subtitle": (
            f"<i>Дарите репутацию себе или другу — {CE_REP_RED} красный или {CE_REP_BLUE} синий огонь.</i>\n"
            "<i>Первые 3 подарка — за {currency}, остальные — за {crystal} кристаллы.</i>"
        ),
        "balance_line": f"{shop.CE_BALANCE} <b>{{balance}} {shop.CURRENCY}</b>      {CE_CRYSTAL} <b>{{crystals}}</b>",
        "page_indicator": "{page}/{total}",
        "back_button": "Назад",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "gift_cost_line": f"{CE_PRICE} <b>Цена:</b> {{cost}} {{currency_icon}}",
        "gift_reward_line": f"{CE_GIVES} <b>Даёт:</b> +{{amount}} {{fire}}",
        "gift_about": (
            "<i>Каждый такой подарок начисляет {fire} <b>{color}</b> огонь — он "
            "отображается в вашем профиле, в разделе «Репутация», и виден всем, "
            "кто открывает вашу карточку. Чем больше огня накоплено, тем выше "
            "ваш статус в игре.</i>"
        ),
        "color_red": "красный",
        "color_blue": "синий",
        "send_button": "Подарить",
        "buy_button": "Купить себе",
        "cancel_button": "✖️ Отмена",
        "not_found": "❌ Такого подарка не существует.",
        "insufficient_coins": f"❌ Не хватает {shop.CURRENCY_PLAIN}. Нужно: {{cost}}",
        "insufficient_crystals": f"❌ Не хватает {CE_CRYSTAL_PLAIN} кристаллов. Нужно: {{cost}}",
        # Без HTML — показывается в toast-алерте (callback.answer(show_alert=True)),
        # там теги вроде <i> не рендерятся (см. комментарий про BUTTON_TEXT выше).
        "daily_limit_buy": "❌ Дневной лимит покупок себе — {used}/{limit}🔥!",
        # С HTML — показывается обычным сообщением (message.answer), курсив рендерится.
        "daily_limit_send": "<i>❌ Дневной лимит подарков другу — {used}/{limit}🔥!</i>",
        "buy_success": "✅ Подарок вручён! +{amount} {fire}",
        "send_prompt": (
            "{emoji} <b>{name}</b>\n\n"
            "<i>Отправьте @username или ID получателя.</i>"
        ),
        "target_not_found": "❌ <i>Пользователь не найден. Проверьте @username или ID и попробуйте ещё раз.</i>",
        "cant_send_self": "❌ <i>Себе дарить нельзя — воспользуйтесь кнопкой «Купить себе».</i>",
        "send_success": f"✅ Подарок <b>{{name}}</b> отправлен {{target}}! Списано {{cost}} {{currency_icon}}, ему(ей) начислено +{{amount}} {{fire}}",
        "received": "{emoji} <b>{sender}</b> подарил(а) вам подарок <b>{name}</b>! +{amount} {fire}",
    },
    "en": {
        "title": f"{CE_GIFT} <b>Gifts</b>",
        "subtitle": (
            f"<i>Gift reputation to yourself or a friend — {CE_REP_RED} red or {CE_REP_BLUE} blue fire.</i>\n"
            "<i>The first 3 gifts cost {currency}, the rest cost {crystal} crystals.</i>"
        ),
        "balance_line": f"{shop.CE_BALANCE} <b>{{balance}} {shop.CURRENCY}</b>      {CE_CRYSTAL} <b>{{crystals}}</b>",
        "page_indicator": "{page}/{total}",
        "back_button": "Back",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "gift_cost_line": f"{CE_PRICE} <b>Cost:</b> {{cost}} {{currency_icon}}",
        "gift_reward_line": f"{CE_GIVES} <b>Gives:</b> +{{amount}} {{fire}}",
        "gift_about": (
            "<i>Every such gift adds {fire} <b>{color}</b> fire — it's shown in "
            "your profile, in the Reputation section, and is visible to anyone "
            "who opens your card. The more fire you have, the higher your "
            "status in the game.</i>"
        ),
        "color_red": "red",
        "color_blue": "blue",
        "send_button": "Send",
        "buy_button": "Buy for myself",
        "cancel_button": "✖️ Cancel",
        "not_found": "❌ This gift doesn't exist.",
        "insufficient_coins": f"❌ Not enough {shop.CURRENCY_PLAIN}. Needed: {{cost}}",
        "insufficient_crystals": f"❌ Not enough {CE_CRYSTAL_PLAIN} crystals. Needed: {{cost}}",
        # No HTML — shown in a toast alert (callback.answer(show_alert=True)),
        # tags like <i> don't render there (see the BUTTON_TEXT comment above).
        "daily_limit_buy": "❌ Daily limit for buying yourself gifts — {used}/{limit}🔥!",
        # HTML — shown as a regular message (message.answer), italics render fine.
        "daily_limit_send": "<i>❌ Daily limit for gifting friends — {used}/{limit}🔥!</i>",
        "buy_success": "✅ Gift claimed! +{amount} {fire}",
        "send_prompt": (
            "{emoji} <b>{name}</b>\n\n"
            "<i>Send the recipient's @username or ID.</i>"
        ),
        "target_not_found": "❌ <i>User not found. Check the @username or ID and try again.</i>",
        "cant_send_self": "❌ <i>You can't gift yourself — use the “Buy for myself” button instead.</i>",
        "send_success": f"✅ Gift <b>{{name}}</b> sent to {{target}}! Spent {{cost}} {{currency_icon}}, they received +{{amount}} {{fire}}",
        "received": "{emoji} <b>{sender}</b> sent you a gift — <b>{name}</b>! +{amount} {fire}",
    },
}

_CURRENCY_ICON = {"coins": shop.CURRENCY, "crystals": CE_CRYSTAL}
# Кастомные — для обычных сообщений/edit_text, там HTML рендерится.
_FIRE_ICON = {"red": CE_REP_RED, "blue": CE_REP_BLUE}
# Обычные юникод-эмодзи — только для toast-алертов (callback.answer(show_alert=True)):
# там HTML-теги вроде <tg-emoji> не рендерятся и показались бы как сырой текст
# (см. комментарий про BUTTON_TEXT выше).
_FIRE_ICON_PLAIN = {"red": "🔴🔥", "blue": "🔵🔥"}


# --- экран "Друзья" (реферальная система) ---
#
# Игрок делится персональной ссылкой вида
# https://t.me/<bot_username>?start=ref<user_id> — по ней новый игрок
# попадает в бота с payload'ом "ref<id>" (см. main.py: cmd_start,
# CommandStart(deep_link=True)), это лишь ЗАПОМИНАЕТ, кто кого пригласил
# (database.set_referrer). Сама награда пригласившему начисляется позже
# и только один раз — сразу как приглашённый выберет язык И пол (см.
# credit_referral ниже, вызывается из main.py: process_gender). Просто
# переход по ссылке / голый /start наградой не считается.
#
# Размер награды зависит от того, есть ли у приглашённого Telegram
# Premium (aiogram/Bot API: user.is_premium) — это статус самого
# Telegram, а не донат-привилегия бота (donate.py).
REFERRAL_REWARD_NORMAL = {"crystals": 2, "coins": 100}
REFERRAL_REWARD_PREMIUM = {"crystals": 5, "coins": 250}

FRIENDS_TEXTS = {
    "ru": {
        "title": f"{CE_FRIENDS} <b>Друзья</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "intro": "<i>Приглашайте друзей в бота и получайте награду за каждого!</i>",
        "reward_line": f"{CE_REGULAR} <b>Обычный:</b> +{CE_CRYSTAL}2, +{CE_COIN}100",
        "reward_premium_line": f"{CE_PREMIUM} <b>С Telegram Premium:</b> +{CE_CRYSTAL}5, +{CE_COIN}250",
        "reward_note": "<i>Награда зачисляется автоматически после выбора языка и пола.</i>",
        "link_label": f"{CE_LINK} <b>Ваша ссылка:</b>",
        "invited_line": f"{CE_INVITED} <b>Приглашено друзей:</b> {{count}}",
        "earned_crystals_line": f"{CE_CRYSTAL} <b>Заработано кристаллов:</b> {{crystals}}",
        "earned_coins_line": f"{CE_COIN} <b>Заработано монет:</b> {{coins}}",
        "back_button": "Назад",
        "friend_joined": (
            f"{CE_PARTY} <b>{{name}}</b> присоединился(лась) по вашей ссылке!\n"
            f"{CE_CRYSTAL} +{{crystals}}   {CE_COIN} +{{coins}}"
        ),
        # То же самое, но приглашённый — обладатель Telegram Premium
        # (см. prof.credit_referral: REFERRAL_REWARD_PREMIUM) — отмечаем
        # звёздочкой, чтобы было видно, за что награда больше обычной.
        "friend_joined_premium": (
            f"{CE_PARTY} {CE_PREMIUM} <b>{{name}}</b> присоединился(лась) по вашей ссылке!\n"
            f"{CE_CRYSTAL} +{{crystals}}   {CE_COIN} +{{coins}}"
        ),
    },
    "en": {
        "title": f"{CE_FRIENDS} <b>Friends</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "intro": "<i>Invite friends to the bot and get a reward for each one!</i>",
        "reward_line": f"{CE_REGULAR} <b>Regular:</b> +{CE_CRYSTAL}2, +{CE_COIN}100",
        "reward_premium_line": f"{CE_PREMIUM} <b>With Telegram Premium:</b> +{CE_CRYSTAL}5, +{CE_COIN}250",
        "reward_note": "<i>The reward is credited automatically after your friend picks a language and gender.</i>",
        "link_label": f"{CE_LINK} <b>Your link:</b>",
        "invited_line": f"{CE_INVITED} <b>Friends invited:</b> {{count}}",
        "earned_crystals_line": f"{CE_CRYSTAL} <b>Crystals earned:</b> {{crystals}}",
        "earned_coins_line": f"{CE_COIN} <b>Coins earned:</b> {{coins}}",
        "back_button": "Back",
        "friend_joined": (
            f"{CE_PARTY} <b>{{name}}</b> joined via your link!\n"
            f"{CE_CRYSTAL} +{{crystals}}   {CE_COIN} +{{coins}}"
        ),
        "friend_joined_premium": (
            f"{CE_PARTY} {CE_PREMIUM} <b>{{name}}</b> joined via your link!\n"
            f"{CE_CRYSTAL} +{{crystals}}   {CE_COIN} +{{coins}}"
        ),
    },
}

_bot_username_cache: str | None = None


async def _get_bot_username(bot) -> str:
    """Кэширует username бота в памяти процесса — нужен только для
    сборки реферальной ссылки, меняться в рантайме не может."""
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


async def _build_friends_text(lang: str, user_id: int, bot) -> str:
    t = FRIENDS_TEXTS[lang]
    username = await _get_bot_username(bot)
    link = f"https://t.me/{username}?start=ref{user_id}"
    stats = await database.get_referral_stats(user_id)

    lines = [
        t["title"],
        t["separator"],
        t["intro"],
        "",
        t["reward_line"],
        t["reward_premium_line"],
        t["reward_note"],
        "",
        t["link_label"],
        f"<code>{link}</code>",
        "",
        t["invited_line"].format(count=stats["count"]),
        t["earned_crystals_line"].format(crystals=stats["crystals"]),
        t["earned_coins_line"].format(coins=stats["coins"]),
    ]
    return "\n".join(lines)


def _friends_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = FRIENDS_TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["back_button"],
        callback_data="friends:back",
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


async def credit_referral(user, bot) -> None:
    """Публичная точка входа — начисляет награду тому, кто пригласил
    `user`, если это ещё не было сделано. Вызывать из main.py сразу
    после того, как `user` выбрал язык И пол (см. комментарий в начале
    секции "экран Друзья" выше — простой /start наградой не считается).
    Идемпотентно: повторный вызов для того же приглашённого ничего не
    начислит (см. database.mark_referral_rewarded)."""
    referrer_id = await database.get_referrer(user.id)
    if referrer_id is None or referrer_id == user.id:
        return

    rewarded_now = await database.mark_referral_rewarded(user.id)
    if not rewarded_now:
        return

    is_premium = bool(getattr(user, "is_premium", False))
    reward = REFERRAL_REWARD_PREMIUM if is_premium else REFERRAL_REWARD_NORMAL

    await add_crystals(referrer_id, reward["crystals"])
    await shop.add_balance(referrer_id, reward["coins"])
    await database.bump_referral_earned(referrer_id, reward["crystals"], reward["coins"])

    # Уведомляем пригласившего — best effort, как и остальные подобные
    # уведомления в этом файле (получатель мог заблокировать бота и т.п.).
    # Если приглашённый — обладатель Telegram Premium, используем
    # отдельный текст со звёздочкой (friend_joined_premium), чтобы было
    # видно, за что начислена повышенная награда.
    try:
        onboarding = await database.get_onboarding(referrer_id)
        ref_lang = (onboarding["lang"] if onboarding else None) or "ru"
        friend_name = html.escape(
            user.first_name or (f"@{user.username}" if user.username else str(user.id))
        )
        text_key = "friend_joined_premium" if is_premium else "friend_joined"
        await bot.send_message(
            referrer_id,
            FRIENDS_TEXTS[ref_lang][text_key].format(
                name=friend_name, crystals=reward["crystals"], coins=reward["coins"]
            ),
        )
    except Exception:
        pass


@router.callback_query(F.data == "friends:open")
async def open_friends(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _build_friends_text(lang, callback.from_user.id, callback.bot)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_friends_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == "friends:back")
async def close_friends(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _build_profile_text(lang, callback.from_user)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_profile_keyboard(lang))
    await callback.answer()


def _fmt_date(ts: float, lang: str) -> str:
    dt = datetime.datetime.fromtimestamp(ts)
    if lang == "en":
        return dt.strftime("%b %d, %Y")
    return dt.strftime("%d.%m.%Y")


LEVEL_UP_LINE = {
    "ru": "✨ <i>Новый уровень — {level}</i> ✨",
    "en": "✨ <i>New level — {level}</i> ✨",
}


def level_up_notice(lang: str, level: int) -> str:
    """Короткая курсивная строка про левелап — добавляется другими
    модулями (garden/bakery) к своим уведомлениям, когда add_xp()
    вернул leveled_up=True."""
    return LEVEL_UP_LINE.get(lang, LEVEL_UP_LINE["ru"]).format(level=level)


def _achv_level_up_text(lang: str, achv_result: dict | None) -> str | None:
    """Если XP-награда ОТКРЫТОЙ ачивки (achives.unlock()) сама по себе
    протащила игрока на новый уровень — level_info внутри achv_result
    (см. achives.unlock: level_info — результат prof.add_xp) — отдаёт
    готовую строку level_up_notice для ОТДЕЛЬНОГО сообщения, как и
    везде в боте. Иначе — None (никакого левелапа не произошло).

    level_info["new_level"] — это уже ИТОГОВЫЙ уровень после начисления
    всего amount разом (см. add_xp/level_from_xp: уровень считается по
    суммарному XP, а не инкрементами +1), поэтому даже если награда
    перепрыгнула сразу несколько уровней (например, был 2-й, а стал
    сразу 4-й, минуя 3-й), здесь вернётся ОДНА строка про 4-й уровень —
    ровно то же поведение, что и у garden.py/bakery.py."""
    if not achv_result:
        return None
    level_info = achv_result.get("level_info")
    if level_info and level_info.get("leveled_up"):
        return level_up_notice(lang, level_info["new_level"])
    return None


# ==========================
#   ХРАНИЛИЩЕ (общая БД — см. database.py)
# ==========================
#
# Своей таблицы этот модуль не заводит — xp/reputation_red/reputation_blue
# живут прямо в users (см. database.USERS_PROFILE_COLUMNS), first_seen/
# username/first_name туда уже пишет admin.UserTrackingMiddleware на
# каждый апдейт, ещё до того, как он доходит до роутеров.

async def _get_user_row(user_id: int) -> aiosqlite.Row | None:
    db = await database.get_db()
    async with db.execute(
        "SELECT first_seen, xp, reputation_red, reputation_blue, display_name "
        "FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


async def get_xp(user_id: int) -> int:
    row = await _get_user_row(user_id)
    return row["xp"] if row else 0


async def get_reputation(user_id: int) -> tuple[int, int]:
    """Возвращает (красная, синяя) — оба счётчика разом."""
    row = await _get_user_row(user_id)
    if not row:
        return 0, 0
    return row["reputation_red"], row["reputation_blue"]


async def _apply_privilege_xp_bonus(user_id: int, amount: int) -> int:
    """Прибавляет бонус к опыту от активной привилегии (donate.py:
    PRIVILEGE_TIERS, exp_bonus_percent) — только к положительному
    начислению, штрафы (amount < 0, сюда и не передаются — см. add_xp)
    бонусом не усиливаем. Локальный импорт donate — donate.py сам не
    импортирует prof на верхнем уровне ради этого, но чтобы не
    рисковать циклом (prof <-> donate оба довольно "центральные"
    модули), импортируем здесь же, по тому же принципу, что и
    "import achives" чуть ниже."""
    import donate

    active = await donate.get_active_privilege(user_id)
    if active is None:
        return amount
    percent = active["tier"]["exp_bonus_percent"]
    if not percent:
        return amount
    return round(amount * (1 + percent / 100))


async def add_xp(user_id: int, amount: int) -> dict:
    """Публичная точка входа для НАЧИСЛЕНИЯ опыта другими модулями —
    сейчас её вызывают garden.py (сбор урожая), bakery.py (готовая
    выпечка) и achives.py (награда опытом за ачивку).

    amount может быть отрицательным (штраф). Возвращает словарь с
    результатом — удобно сразу показать игроку "🎉 Новый уровень!",
    если leveled_up=True, а также unlocked_achievements — список
    результатов achives.unlock() для ачивок "10 уровень"/"25 уровень"
    (пусто, если порог не пройден или ачивка уже была открыта раньше).

    Если у игрока активна привилегия с бонусом к опыту (donate.py:
    PRIVILEGE_TIERS, exp_bonus_percent) — положительное amount
    увеличивается на этот процент ДО начисления, единой точкой для
    всех источников опыта сразу (сад/пекарня/ачивки), без необходимости
    трогать каждый вызывающий модуль по отдельности."""
    if amount > 0:
        amount = await _apply_privilege_xp_bonus(user_id, amount)

    async with database.user_lock(user_id):
        db = await database.get_db()
        row = await _get_user_row(user_id)
        old_xp = row["xp"] if row else 0
        old_level, _, _ = level_from_xp(old_xp)

        new_xp = max(0, old_xp + amount)
        await db.execute(
            "UPDATE users SET xp = ? WHERE user_id = ?", (new_xp, user_id)
        )
        await database.commit()

    new_level, xp_into, xp_needed = level_from_xp(new_xp)

    # Лог для лидерборда "Уровень" (leaders.py) — только реальный
    # прирост (new_xp - old_xp), а не запрошенный amount: то же самое
    # клиппинг-по-нулю уже случился выше. Штрафы (delta <= 0) в лог не
    # идут — лидерборд считает НАБРАННЫЙ опыт за период, а не сальдо.
    # Импорт локальный просто чтобы не тянуть leaders.py в каждый
    # модуль, вызывающий add_xp, без реальной необходимости (сам
    # leaders.py prof.py не импортирует — цикла тут нет).
    xp_delta = new_xp - old_xp
    if xp_delta > 0:
        import leaders

        await leaders.log_xp_gain(user_id, xp_delta)

    # Ачивки за уровень — уже ЗА ПРЕДЕЛАМИ user_lock выше (та же причина,
    # что и у garden.py/bakery.py при начислении опыта: achives.unlock()
    # сам берёт этот же лок внутри prof.add_xp/add_crystals, повторный
    # захват был бы дедлоком). Импорт локальный — achives.py импортирует
    # prof на верхнем уровне, обратный импорт тут завёл бы цикл.
    import achives

    unlocked_achievements = []
    if new_level >= 10:
        result = await achives.unlock(user_id, "level_10")
        if result:
            unlocked_achievements.append(result)
    if new_level >= 25:
        result = await achives.unlock(user_id, "level_25")
        if result:
            unlocked_achievements.append(result)
    # Общие ачивки за уровень/суммарный опыт (achives.py, category="general") —
    # та же логика, что и level_10/level_25 выше, просто больше порогов.
    for threshold, achv_id in (
        (5, "general_level_5"),
        (15, "general_level_15"),
        (20, "general_level_20"),
        (30, "general_level_30"),
        (40, "general_level_40"),
        (50, "general_level_50"),
    ):
        if new_level >= threshold:
            result = await achives.unlock(user_id, achv_id)
            if result:
                unlocked_achievements.append(result)
    if new_xp >= 10_000:
        result = await achives.unlock(user_id, "general_xp_10000")
        if result:
            unlocked_achievements.append(result)

    return {
        "old_level": old_level,
        "new_level": new_level,
        "leveled_up": new_level > old_level,
        "xp": new_xp,
        "xp_into_level": xp_into,
        "xp_needed": xp_needed,
        "unlocked_achievements": unlocked_achievements,
    }


async def add_reputation_red(user_id: int, amount: int) -> int:
    """Заготовка на будущее — под 🔥 красную репутацию (за неё позже
    планируются подарки). Сейчас не вызывается нигде в боте. Возвращает
    итоговое значение счётчика (не уходит в минус)."""
    return await _add_reputation(user_id, "reputation_red", amount)


async def add_reputation_blue(user_id: int, amount: int) -> int:
    """То же самое, но под 🔥 синюю репутацию."""
    return await _add_reputation(user_id, "reputation_blue", amount)


async def _add_reputation(user_id: int, column: str, amount: int) -> int:
    async with database.user_lock(user_id):
        db = await database.get_db()
        row = await _get_user_row(user_id)
        old_value = row[column] if row else 0
        new_value = max(0, old_value + amount)
        await db.execute(
            f"UPDATE users SET {column} = ? WHERE user_id = ?", (new_value, user_id)
        )
        await database.commit()

    # Лог для лидерборда "Огоньки" (leaders.py) — те же правила, что и
    # у xp_delta в add_xp выше: только реальный прирост, штрафы не
    # логируются. reputation_red и reputation_blue вместе считаются
    # одним метриком "fire" — на карточке подарка это один и тот же
    # 🔥, просто двух цветов (см. докстринг модуля).
    rep_delta = new_value - old_value
    if rep_delta > 0:
        import leaders

        await leaders.log_fire_gain(user_id, rep_delta)

    return new_value


# ==========================
#   ВАЛЮТА "КРИСТАЛЛЫ" (премиум-валюта — трата на подарки)
# ==========================
#
# Своей колонки под неё изначально не было ни в database.py, ни в
# shop.py, а заводить новый файл/трогать чужие модули нельзя — поэтому
# колонка добавляется лениво прямо отсюда, тем же способом, что и
# database._ensure_columns (ALTER TABLE, если колонки ещё нет), при
# первом обращении к любой из функций ниже.

_gift_schema_ready = False
_gift_schema_lock = asyncio.Lock()


async def _ensure_gift_schema() -> None:
    global _gift_schema_ready
    if _gift_schema_ready:
        return
    async with _gift_schema_lock:
        if _gift_schema_ready:
            return
        db = await database.get_db()
        async with db.execute("PRAGMA table_info(users)") as cursor:
            existing = {row["name"] async for row in cursor}
        if "crystals" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN crystals INTEGER NOT NULL DEFAULT 0"
            )
            await database.flush()
        if "buy_daily_amount" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN buy_daily_amount INTEGER NOT NULL DEFAULT 0"
            )
            await database.flush()
        if "buy_daily_date" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN buy_daily_date TEXT"
            )
            await database.flush()
        if "send_daily_amount" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN send_daily_amount INTEGER NOT NULL DEFAULT 0"
            )
            await database.flush()
        if "send_daily_date" not in existing:
            await db.execute(
                "ALTER TABLE users ADD COLUMN send_daily_date TEXT"
            )
            await database.flush()
        _gift_schema_ready = True


# ==========================
#   СЧЁТЧИКИ ДЛЯ "ОБЩИХ" АЧИВОК (achives.py, category="general")
# ==========================
#
# achives.py сам не отслеживает игровые условия (см. его докстринг) —
# счётчики, нужные конкретно общим ачивкам (отправленные/полученные
# подарки, стрик заходов в бота, коллекция видов подарков), живут
# здесь же, рядом с остальным состоянием профиля/подарков, той же
# ленивой миграцией (ALTER TABLE / CREATE TABLE IF NOT EXISTS), что и
# _ensure_gift_schema выше.

_general_achv_schema_ready = False
_general_achv_schema_lock = asyncio.Lock()


async def _ensure_general_achv_schema() -> None:
    global _general_achv_schema_ready
    if _general_achv_schema_ready:
        return
    async with _general_achv_schema_lock:
        if _general_achv_schema_ready:
            return
        db = await database.get_db()
        async with db.execute("PRAGMA table_info(users)") as cursor:
            existing = {row["name"] async for row in cursor}
        for column in (
            "gifts_sent_count",
            "gifts_received_count",
            "login_streak_count",
            "donate_total_crystals",
            "coins_earned_total",
        ):
            if column not in existing:
                await db.execute(
                    f"ALTER TABLE users ADD COLUMN {column} INTEGER NOT NULL DEFAULT 0"
                )
                await database.flush()
        if "login_streak_date" not in existing:
            await db.execute("ALTER TABLE users ADD COLUMN login_streak_date TEXT")
            await database.flush()
        # Виды подарков (покупка себе ИЛИ отправка другу — неважно), с
        # которыми игрок хоть раз взаимодействовал — для general_all_gifts
        # ("собрать" все 15 видов). PRIMARY KEY на паре — INSERT OR IGNORE
        # идемпотентен, как и user_achievements в achives.py.
        await db.execute(
            "CREATE TABLE IF NOT EXISTS gift_types_seen ("
            "user_id INTEGER NOT NULL, gift_id INTEGER NOT NULL, "
            "PRIMARY KEY (user_id, gift_id))"
        )
        await database.flush()
        _general_achv_schema_ready = True


# Подарки, которые считаются "редкими/дорогими" для ачивки general_rare_gift
# ("кит, жемчужина и другие дорогие подарки") — самый дорогой подарок в
# каждом из 5 наборов GIFTS (id 3, 6, 9, 12, 15, см. секцию "ПОДАРКИ" ниже).
RARE_GIFT_IDS = {3, 6, 9, 12, 15}


async def _bump_counter(user_id: int, column: str) -> int:
    """+1 к одной из простых числовых колонок-счётчиков (gifts_sent_count/
    gifts_received_count) в users. Возвращает новое значение."""
    await _ensure_general_achv_schema()
    async with database.user_lock(user_id):
        db = await database.get_db()
        await db.execute(
            f"UPDATE users SET {column} = {column} + 1 WHERE user_id = ?",
            (user_id,),
        )
        await database.commit()
        async with db.execute(
            f"SELECT {column} AS value FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else 0


async def get_gifts_sent_count(user_id: int) -> int:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT gifts_sent_count FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["gifts_sent_count"] if row and row["gifts_sent_count"] is not None else 0


async def get_gifts_received_count(user_id: int) -> int:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT gifts_received_count FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return (
        row["gifts_received_count"]
        if row and row["gifts_received_count"] is not None
        else 0
    )


async def _mark_gift_type_seen(user_id: int, gift_id: int) -> None:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    await db.execute(
        "INSERT OR IGNORE INTO gift_types_seen (user_id, gift_id) VALUES (?, ?)",
        (user_id, gift_id),
    )
    await database.commit()


async def get_gift_types_count(user_id: int) -> int:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM gift_types_seen WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["cnt"] if row else 0


async def add_donate_total(user_id: int, amount: int) -> int:
    """+amount к пожизненному счётчику купленных за донат кристаллов —
    для general_donate_total_1000 ("суммарно потратить 1000 кристаллов"
    на донат; не путать с тратой кристаллов на подарки — это отдельный
    счётчик именно покупок, см. donate.py: on_successful_payment).
    Возвращает новое значение."""
    await _ensure_general_achv_schema()
    async with database.user_lock(user_id):
        db = await database.get_db()
        await db.execute(
            "UPDATE users SET donate_total_crystals = donate_total_crystals + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await database.commit()
        async with db.execute(
            "SELECT donate_total_crystals AS value FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return row["value"] if row else 0


async def get_donate_total(user_id: int) -> int:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT donate_total_crystals FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["donate_total_crystals"] if row and row["donate_total_crystals"] is not None else 0


async def bump_coins_earned_no_lock(user_id: int, amount: int) -> None:
    """+amount к пожизненному счётчику ЗАРАБОТАННЫХ (не текущий баланс,
    который может тратиться и падать — а именно суммарно начисленных)
    монет — для general_coins_earned_10000. Вызывается из shop.py,
    прямо из _change_balance (см. комментарий там же) — БЕЗ
    database.user_lock, тем же способом, каким shop._change_balance
    сама меняет баланс (одна атомарная UPDATE, SQLite сериализует
    запись сам); лок здесь недопустим — _change_balance дергается и из
    мест, которые уже держат лок на этот же user_id (add_balance), и из
    мест без лока вовсе (buy_listing — на seller_id), так что общий
    helper обязан быть настолько же lock-free."""
    await _ensure_general_achv_schema()
    db = await database.get_db()
    await db.execute(
        "UPDATE users SET coins_earned_total = coins_earned_total + ? WHERE user_id = ?",
        (amount, user_id),
    )
    await database.commit()


async def get_coins_earned(user_id: int) -> int:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT coins_earned_total FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["coins_earned_total"] if row and row["coins_earned_total"] is not None else 0


async def _effective_daily_gift_limit(user_id: int) -> int:
    """Дневной лимит на подарки для конкретного игрока — обычно
    DAILY_GIFT_LIMIT, но выше, если у игрока активна привилегия
    (donate.py: PRIVILEGE_TIERS, gift_limit — 15к/25к/50к вместо базовых
    10к). Локальный импорт donate — по той же причине, что и в
    _apply_privilege_xp_bonus выше (не заводить цикл импортов)."""
    import donate

    active = await donate.get_active_privilege(user_id)
    if active is None:
        return DAILY_GIFT_LIMIT
    return max(DAILY_GIFT_LIMIT, active["tier"]["gift_limit"])


async def _is_daily_limit_maxed(user_id: int, amount_col: str, date_col: str) -> bool:
    """True, если сегодняшний счётчик дневного лимита подарков (покупка
    себе ИЛИ отправка другу — amount_col разный) уже достиг потолка
    (обычного DAILY_GIFT_LIMIT либо повышенного — см.
    _effective_daily_gift_limit) — для ачивки general_daily_gift_limit."""
    await _ensure_gift_schema()
    limit = await _effective_daily_gift_limit(user_id)
    db = await database.get_db()
    async with db.execute(
        f"SELECT {amount_col} AS amount, {date_col} AS date FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if not row or row["date"] != _today_utc():
        return False
    return (row["amount"] or 0) >= limit


async def _bump_login_streak(user_id: int) -> int | None:
    """Дневной стрик заходов в бота: +1, если предыдущий засчитанный
    день — вчера, сброс на 1 при разрыве (или первом визите), без
    изменений, если сегодня уже засчитано. Возвращает новое значение
    стрика ТОЛЬКО если он изменился сегодня впервые (иначе None —
    ачивки за стрик проверять не нужно, засчитывать нечего)."""
    await _ensure_general_achv_schema()
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT login_streak_count, login_streak_date FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        today = _today_utc()
        if row and row["login_streak_date"] == today:
            return None  # уже засчитан сегодня
        yesterday = (
            datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1)
        ).strftime("%Y-%m-%d")
        if row and row["login_streak_date"] == yesterday:
            new_streak = (row["login_streak_count"] or 0) + 1
        else:
            new_streak = 1
        await db.execute(
            "UPDATE users SET login_streak_count = ?, login_streak_date = ? WHERE user_id = ?",
            (new_streak, today, user_id),
        )
        await database.commit()
    return new_streak


async def get_login_streak(user_id: int) -> int:
    await _ensure_general_achv_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT login_streak_count FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["login_streak_count"] if row and row["login_streak_count"] is not None else 0


async def ensure_startup_schema() -> None:
    """Прогоняет ленивые ALTER TABLE-миграции (кристаллы + счётчики
    "общих" ачивок) один раз при старте бота — см. вызов в main.py:main().
    Раньше это происходило лениво при первом обращении к БД, а первым
    таким обращением почти всегда оказывалось самое первое нажатие
    инлайн-кнопки игроком (LoginStreakMiddleware срабатывает на каждый
    апдейт), из-за чего именно это первое действие после рестарта бота
    выглядело как заметное "зависание"."""
    await _ensure_gift_schema()
    await _ensure_general_achv_schema()


LOGIN_STREAK_THRESHOLDS = [
    (7, "general_login_streak_7"),
    (30, "general_login_streak_30"),
    (100, "general_login_streak_100"),
]


class LoginStreakMiddleware(BaseMiddleware):
    """Outer-middleware — считает дневной стрик заходов в бота на КАЖДЫЙ
    апдейт от игрока (а не только на /start или конкретный раздел), и
    выдаёт general_login_streak_7/30/100, когда стрик их достигает.
    Регистрируется в main.py рядом с admin.UserTrackingMiddleware:
        dp.update.outer_middleware(prof.LoginStreakMiddleware())
    Best-effort: любая ошибка (например, юзер уже заблокировал бота)
    тихо гасится — стрик не должен ронять обработку апдейта."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user is not None and not user.is_bot:
            try:
                new_streak = await _bump_login_streak(user.id)
                if new_streak is not None:
                    hit = [
                        achv_id
                        for threshold, achv_id in LOGIN_STREAK_THRESHOLDS
                        if new_streak == threshold
                    ]
                    if hit:
                        bot = data.get("bot")
                        if bot is not None:
                            await _notify_general_achievements(bot, user.id, hit)
            except Exception:
                pass
        return await handler(event, data)


async def _notify_general_achievements(bot, user_id: int, achv_ids: list[str]) -> None:
    """Best-effort отправка уведомления об ачивке НЕ из хендлера (нет
    под рукой message/callback — например, из LoginStreakMiddleware) —
    сама достаёт язык игрока и шлёт отдельным сообщением через bot."""
    import achives

    onboarding = await database.get_onboarding(user_id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"
    for achv_id in achv_ids:
        result = await achives.unlock(user_id, achv_id)
        if result:
            try:
                await bot.send_message(user_id, achives.format_unlock_text(lang, result))
                lvl_text = _achv_level_up_text(lang, result)
                if lvl_text:
                    await bot.send_message(user_id, lvl_text)
            except Exception:
                pass


async def get_crystals(user_id: int) -> int:
    await _ensure_gift_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT crystals FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["crystals"] if row and row["crystals"] is not None else 0


async def add_crystals(user_id: int, amount: int) -> int:
    """Публичная точка входа для НАЧИСЛЕНИЯ кристаллов другими модулями —
    используется donate.py (покупка за Telegram Stars) и admin.py
    (ручная выдача из админ-панели). amount может быть отрицательным.
    Возвращает итоговый баланс (не уходит в минус). Сама захватывает
    database.user_lock — не вызывать из кода, который уже держит лок на
    этого же user_id (см. _change_crystal_balance ниже)."""
    async with database.user_lock(user_id):
        return await _change_crystal_balance(user_id, amount)


async def _change_crystal_balance(user_id: int, delta: int) -> int:
    """Низкоуровневое изменение баланса — БЕЗ захвата database.user_lock.
    Вызывать только если лок на user_id уже захвачен снаружи (например,
    panda.buy_skin — см. комментарий там: asyncio.Lock нереентерабельный,
    повторный async with тем же локом внутри — гарантированный дедлок).
    Клампит результат на 0 снизу, как и add_crystals. Возвращает итоговый
    баланс."""
    await _ensure_gift_schema()
    db = await database.get_db()
    current = await get_crystals(user_id)
    new_value = max(0, current + delta)
    await db.execute(
        "UPDATE users SET crystals = ? WHERE user_id = ?", (new_value, user_id)
    )
    await database.commit()
    return new_value


async def _spend_crystals(user_id: int, amount: int) -> bool:
    """Списывает кристаллы, если их хватает. Возвращает False, если нет
    (баланс при этом не трогается)."""
    async with database.user_lock(user_id):
        await _ensure_gift_schema()
        current = await get_crystals(user_id)
        if current < amount:
            return False
        db = await database.get_db()
        await db.execute(
            "UPDATE users SET crystals = ? WHERE user_id = ?",
            (current - amount, user_id),
        )
        await database.commit()
    return True


async def _get_coin_balance(user_id: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT balance FROM shop_balance WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["balance"] if row else 0


async def _spend_coins(user_id: int, amount: int) -> bool:
    """Списывает Pn напрямую из shop_balance (та же таблица, что и
    shop.get_balance читает). Возвращает False, если не хватает
    (баланс при этом не трогается)."""
    async with database.user_lock(user_id):
        current = await _get_coin_balance(user_id)
        if current < amount:
            return False
        db = await database.get_db()
        await db.execute(
            "INSERT INTO shop_balance (user_id, balance) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
            (user_id, current - amount),
        )
        await database.commit()
    return True


# ==========================
#   ДНЕВНЫЕ ЛИМИТЫ НА ОГОНЁК (репутацию) ЗА ПОДАРКИ
# ==========================
#
# Два НЕЗАВИСИМЫХ лимита по DAILY_GIFT_LIMIT огонька в сутки каждый:
#   - покупка подарка себе ("Купить себе" — buy_daily_amount/date),
#   - отправка подарка другу ("Подарить" — send_daily_amount/date).
# Один не расходует лимит другого — итого можно получить/раздать до
# 2 * DAILY_GIFT_LIMIT огонька в сутки суммарно (10к себе + 10к другу).
# Сутки — по UTC; при первом обращении в новые сутки счётчик сам
# обнуляется (сброс происходит лениво, в 00:00 UTC отдельного
# джоба/крона не нужно).
#
# ВАЖНО про защиту от дюпа/обхода лимита (двойной тап по кнопке,
# два быстрых сообщения с получателем и т.п.): проверка "уложились ли
# в лимит" и запись нового значения счётчика сделаны ОДНОЙ атомарной
# операцией (_reserve_daily) под тем же per-user asyncio.Lock, что и
# списание валюты (database.user_lock) — тем же локом, что использует
# ВЕСЬ модуль (см. docstring вверху файла: "add_xp тоже берёт этот
# лок... обычный asyncio.Lock", он не реентерабельный, но здесь нет
# вложенных acquire — резерв лимита, списание валюты и начисление
# репутации идут ПОСЛЕДОВАТЕЛЬНО, каждое в своём "async with", лок
# успевает освободиться между ними). Именно последовательность имеет
# значение: РЕЗЕРВ ЛИМИТА ИДЁТ ПЕРВЫМ, ДО списания валюты — так что
# даже параллельный двойной клик не может "проскочить" мимо лимита:
# вторая попытка увидит уже увеличенный счётчик и получит отказ ДО
# того, как у игрока спишутся деньги. Если после успешного резерва
# оплата вдруг не проходит (не хватило валюты — редкий, но возможный
# случай при гонке с другой тратой того же баланса), резерв
# ОТКАТЫВАЕТСЯ через _release_daily, чтобы неудачная попытка не жгла
# впустую чужой дневной лимит.

DAILY_GIFT_LIMIT = 10_000


def _today_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


async def _reserve_daily(
    user_id: int, amount: int, amount_col: str, date_col: str
) -> tuple[int, int] | None:
    """Атомарно (под database.user_lock — тем же локом, что и списание
    валюты) проверяет дневной лимит и, если укладываемся, СРАЗУ же
    увеличивает счётчик на amount — резервирование и проверка это одна
    операция, без разрыва между "прочитали" и "записали", поэтому два
    параллельных вызова (двойной тап) не могут оба пройти проверку.

    Лимит — обычно DAILY_GIFT_LIMIT, но выше при активной привилегии
    (см. _effective_daily_gift_limit).

    Возвращает None при успехе (лимит зарезервирован под этот подарок).
    Возвращает (использовано_сегодня, лимит) при отказе — в БД в этом
    случае ничего не меняется."""
    await _ensure_gift_schema()
    limit = await _effective_daily_gift_limit(user_id)
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            f"SELECT {amount_col}, {date_col} FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        today = _today_utc()
        current = row[amount_col] or 0 if row and row[date_col] == today else 0
        if current + amount > limit:
            return current, limit
        await db.execute(
            f"UPDATE users SET {amount_col} = ?, {date_col} = ? WHERE user_id = ?",
            (current + amount, today, user_id),
        )
        await database.commit()
    return None


async def _release_daily(user_id: int, amount: int, amount_col: str, date_col: str) -> None:
    """Откатывает ранее сделанный _reserve_daily — вызывается, если
    после успешного резерва лимита оплата подарка всё же не прошла
    (не хватило валюты). Если за это время уже наступили новые сутки
    UTC — счётчик и так обнулился под новую дату, откатывать нечего,
    трогать его не нужно (иначе можно случайно увести в минус чужой
    новый дневной лимит)."""
    await _ensure_gift_schema()
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            f"SELECT {amount_col}, {date_col} FROM users WHERE user_id = ?",
            (user_id,),
        ) as cursor:
            row = await cursor.fetchone()
        today = _today_utc()
        if not row or row[date_col] != today:
            return
        new_value = max(0, (row[amount_col] or 0) - amount)
        await db.execute(
            f"UPDATE users SET {amount_col} = ? WHERE user_id = ?",
            (new_value, user_id),
        )
        await database.commit()


async def _reserve_daily_buy(user_id: int, amount: int) -> tuple[int, int] | None:
    return await _reserve_daily(user_id, amount, "buy_daily_amount", "buy_daily_date")


async def _release_daily_buy(user_id: int, amount: int) -> None:
    await _release_daily(user_id, amount, "buy_daily_amount", "buy_daily_date")


async def _reserve_daily_send(user_id: int, amount: int) -> tuple[int, int] | None:
    return await _reserve_daily(user_id, amount, "send_daily_amount", "send_daily_date")


async def _release_daily_send(user_id: int, amount: int) -> None:
    await _release_daily(user_id, amount, "send_daily_amount", "send_daily_date")


# ==========================
#   ПОДАРКИ (раздел профиля)
# ==========================
#
# 15 подарков — каждый даёт репутацию (🔴 красный или 🔵 синий огонь,
# см. reputation_red/reputation_blue выше). Первые 3 покупаются за Pn
# (обычная игровая валюта, shop_balance), оставшиеся 12 — за кристаллы
# (премиум-валюта, см. секцию выше). Цены/награды для первых трёх
# заданы явно; дальше — тот же принцип (пары "красный дороже за
# красный, дешёвая кучка + дорогая кучка + один дорогой синий"),
# продолженный на 4 таких же "набора" в кристаллах с растущими ценами.
# Список полностью табличный — поправить конкретную цену/награду можно
# прямо здесь, ничего пересчитывать не нужно.

GIFTS = [
    # --- за монеты (Pn) ---
    {"id": 1, "currency": "coins", "cost": 5000, "reward": "red", "amount": 500,
     "name": {"ru": "Ромашка", "en": "Daisy"},
     "desc": {
         "ru": "Простой и тёплый подарок — с него обычно и начинают дарить друзьям.",
         "en": "A simple, warm gift — the classic way to start giving.",
     }},
    {"id": 2, "currency": "coins", "cost": 15000, "reward": "red", "amount": 1750,
     "name": {"ru": "Роза", "en": "Rose"},
     "desc": {
         "ru": "Классика жанра: красивый и щедрый жест внимания.",
         "en": "A classic: a beautiful, generous gesture of attention.",
     }},
    {"id": 3, "currency": "coins", "cost": 50000, "reward": "blue", "amount": 250,
     "name": {"ru": "Жемчужина", "en": "Pearl"},
     "desc": {
         "ru": "Редкая жемчужина из морских глубин — самый дорогой подарок за монеты.",
         "en": "A rare pearl from the deep sea — the priciest gift you can buy with coins.",
     }},
    # --- за кристаллы (набор 2) ---
    {"id": 4, "currency": "crystals", "cost": 50, "reward": "red", "amount": 750,
     "name": {"ru": "Агат", "en": "Agate"},
     "desc": {
         "ru": "Камень спокойствия и уверенности — недорогой, но приятный подарок.",
         "en": "A stone of calm and confidence — a small but pleasant gift.",
     }},
    {"id": 5, "currency": "crystals", "cost": 150, "reward": "red", "amount": 2500,
     "name": {"ru": "Дельфин", "en": "Dolphin"},
     "desc": {
         "ru": "Дружелюбный символ преданности и хорошего настроения.",
         "en": "A friendly symbol of loyalty and good mood.",
     }},
    {"id": 6, "currency": "crystals", "cost": 500, "reward": "blue", "amount": 400,
     "name": {"ru": "Кит", "en": "Whale"},
     "desc": {
         "ru": "Большой и величественный — редкий подарок с ледяным отблеском.",
         "en": "Big and majestic — a rare gift with a frosty glow.",
     }},
    # --- за кристаллы (набор 3) ---
    {"id": 7, "currency": "crystals", "cost": 100, "reward": "red", "amount": 1000,
     "name": {"ru": "Бабочка", "en": "Butterfly"},
     "desc": {
         "ru": "Лёгкий и красивый знак внимания.",
         "en": "A light, pretty token of attention.",
     }},
    {"id": 8, "currency": "crystals", "cost": 300, "reward": "red", "amount": 3500,
     "name": {"ru": "Единорог", "en": "Unicorn"},
     "desc": {
         "ru": "Волшебный подарок для тех, кто хочет выделиться.",
         "en": "A magical gift for those who want to stand out.",
     }},
    {"id": 9, "currency": "crystals", "cost": 1000, "reward": "blue", "amount": 600,
     "name": {"ru": "Кораблик", "en": "Ship"},
     "desc": {
         "ru": "Символ путешествий и прохлады далёких морей.",
         "en": "A symbol of travel and the chill of distant seas.",
     }},
    # --- за кристаллы (набор 4) ---
    {"id": 10, "currency": "crystals", "cost": 200, "reward": "red", "amount": 1500,
     "name": {"ru": "Звезда", "en": "Star"},
     "desc": {
         "ru": "Яркий подарок, который сразу заметен в профиле.",
         "en": "A bright gift that's hard to miss in a profile.",
     }},
    {"id": 11, "currency": "crystals", "cost": 600, "reward": "red", "amount": 5000,
     "name": {"ru": "Корона", "en": "Crown"},
     "desc": {
         "ru": "Показывает статус и уважение среди других игроков.",
         "en": "Shows status and respect among other players.",
     }},
    {"id": 12, "currency": "crystals", "cost": 2000, "reward": "blue", "amount": 900,
     "name": {"ru": "Яхта", "en": "Yacht"},
     "desc": {
         "ru": "Роскошный подарок с ледяным блеском — не для всех.",
         "en": "A luxurious gift with a frosty shine — not for everyone.",
     }},
    # --- за кристаллы (набор 5) ---
    {"id": 13, "currency": "crystals", "cost": 400, "reward": "red", "amount": 2250,
     "name": {"ru": "Кольцо", "en": "Ring"},
     "desc": {
         "ru": "Символ крепкой дружбы — или чего-то большего.",
         "en": "A symbol of strong friendship — or something more.",
     }},
    {"id": 14, "currency": "crystals", "cost": 1200, "reward": "red", "amount": 7500,
     "name": {"ru": "Ракета", "en": "Rocket"},
     "desc": {
         "ru": "Для тех, кто рвётся к вершине рейтинга.",
         "en": "For those racing straight to the top of the leaderboard.",
     }},
    {"id": 15, "currency": "crystals", "cost": 4000, "reward": "blue", "amount": 1400,
     "name": {"ru": "Алмаз", "en": "Diamond"},
     "desc": {
         "ru": "Самый редкий и дорогой подарок в игре.",
         "en": "The rarest and most expensive gift in the game.",
     }},
]

GIFTS_BY_ID = {gift["id"]: gift for gift in GIFTS}

# Кастомный эмодзи под каждый конкретный подарок (id подарка -> id эмодзи +
# юникод-глиф для fallback, если кастомный не рендерится). Используется в
# заголовке карточки подарка (шапка окна) вместо общего CE_GIFT/CE_GIFT_BUY,
# а также в кнопке списка (icon_custom_emoji_id) и в сообщениях о дарении.
GIFT_EMOJI = {
    1: ("5429269365759427913", "🌼"),   # Ромашка
    2: ("5363938656874673963", "🌹"),   # Роза
    3: ("5285374445081351432", "🎁"),   # Жемчужина
    4: ("5215506210622554221", "💝"),   # Агат
    5: ("5361995420396433801", "🐬"),   # Дельфин
    6: ("5400362079783770689", "🐳"),   # Кит
    7: ("5316558987141852841", "🦋"),   # Бабочка
    8: ("5208945510638957569", "🦄"),   # Единорог
    9: ("5188322825735267247", "⛵️"),  # Кораблик
    10: ("5447644863644320013", "⭐️"),  # Звезда
    11: ("5217822164362739968", "👑"),  # Корона
    12: ("6001395499628762723", "🎁"),  # Яхта
    13: ("5208961294643783842", "🎁"),  # Кольцо
    14: ("5188481279963715781", "🚀"),  # Ракета
    15: ("5235630047959727475", "💎"),  # Алмаз
}


def _gift_ce(gift_id: int) -> str:
    """<tg-emoji> конкретного подарка для текста (title карточки, шапка
    send_prompt/received). Если для id вдруг нет своего эмодзи — падаем
    обратно на общий CE_GIFT."""
    emoji_id, glyph = GIFT_EMOJI.get(gift_id, (EMOJI_GIFT_ID, "🎁"))
    return _ce(emoji_id, glyph)


def _gift_page(gift_id: int) -> int:
    """Номер страницы (с нуля), на которой находится подарок."""
    for index, gift in enumerate(GIFTS):
        if gift["id"] == gift_id:
            return index // PAGE_SIZE
    return 0


def _total_pages() -> int:
    return (len(GIFTS) + PAGE_SIZE - 1) // PAGE_SIZE


async def buy_gift(user_id: int, gift_id: int) -> dict:
    """Покупает подарок: списывает валюту и начисляет репутацию.
    Возвращает {"ok": False, "reason": "not_found"} — нет такого id;
    {"ok": False, "reason": "daily_limit", "gift": ..., "used": ...,
    "limit": ...} — дневной лимит покупок СЕБЕ (см. DAILY_GIFT_LIMIT /
    _effective_daily_gift_limit) исчерпан, валюта не трогалась;
    {"ok": False, "reason": "insufficient", "gift": ...} — не хватило
    валюты (лимит, зарезервированный на шаге проверки, откатывается
    обратно, реально ничего не списано); {"ok": True, "gift": ...,
    "new_value": ...} — подарок куплен, new_value — итоговое значение
    соответствующей репутации.

    Порядок намеренно такой: СНАЧАЛА атомарный резерв дневного лимита,
    ПОТОМ списание валюты — это и есть защита от дюпа при двойном
    тапе (см. комментарий в секции "ДНЕВНЫЕ ЛИМИТЫ" выше)."""
    gift = GIFTS_BY_ID.get(gift_id)
    if gift is None:
        return {"ok": False, "reason": "not_found"}

    reserve_result = await _reserve_daily_buy(user_id, gift["amount"])
    if reserve_result is not None:
        used, limit = reserve_result
        return {"ok": False, "reason": "daily_limit", "gift": gift, "used": used, "limit": limit}

    if gift["currency"] == "coins":
        spent = await _spend_coins(user_id, gift["cost"])
    else:
        spent = await _spend_crystals(user_id, gift["cost"])

    if not spent:
        await _release_daily_buy(user_id, gift["amount"])
        return {"ok": False, "reason": "insufficient", "gift": gift}

    if gift["reward"] == "red":
        new_value = await add_reputation_red(user_id, gift["amount"])
    else:
        new_value = await add_reputation_blue(user_id, gift["amount"])

    return {"ok": True, "gift": gift, "new_value": new_value}


# ==========================
#   ПОСТРОЕНИЕ ЭКРАНА
# ==========================

async def _display_name(user_id: int, first_name: str | None) -> str:
    """Отображаемое имя игрока — сначала сохранённое через "Настройки" ->
    "Изменить имя" (уже готовый HTML, см. USERS_PROFILE_COLUMNS["display_name"]
    в database.py), а если игрок его не менял — обычный user.first_name из
    Telegram (экранированный). Нужна отдельной функцией (а не только внутри
    _build_profile_text) — есть и другие места, где один игрок упоминается
    ПЕРЕД другим (например, уведомление получателю о подарке, см.
    gift_send ниже) и раньше брали first_name напрямую: после смены имени
    там по-прежнему "протекало" бы старое имя из Telegram."""
    row = await _get_user_row(user_id)
    if row and row["display_name"]:
        return row["display_name"]
    return html.escape(first_name or "—")


async def _build_profile_text(lang: str, user) -> str:
    t = TEXTS[lang]

    row = await _get_user_row(user.id)
    first_seen = row["first_seen"] if row else None
    xp = row["xp"] if row else 0
    rep_red = row["reputation_red"] if row else 0
    rep_blue = row["reputation_blue"] if row else 0

    balance = await shop.get_balance(user.id)
    crystals = await get_crystals(user.id)

    level, xp_into, xp_needed = level_from_xp(xp)

    name = row["display_name"] if row and row["display_name"] else html.escape(user.first_name or "—")
    username = (
        "@" + html.escape(user.username) if user.username else t["no_username"]
    )
    joined = _fmt_date(first_seen, lang) if first_seen else "—"

    # Строка "Статус" — только если у игрока сейчас активна привилегия
    # (donate.py: PRIVILEGE_TIERS). Локальный импорт donate — по тому же
    # принципу, что и в _apply_privilege_xp_bonus/_effective_daily_gift_limit
    # выше (цикл prof <-> donate).
    import donate

    active_privilege = await donate.get_active_privilege(user.id)

    lines = [
        t["title"],
        t["separator"],
        t["name_line"].format(name=name),
        t["username_line"].format(username=username),
        t["id_line"].format(user_id=user.id),
        t["joined_line"].format(date=joined),
    ]
    if active_privilege is not None:
        lines.append(t["status_line"].format(name=active_privilege["tier"]["name"]))
    lines += [
        "",
        t["balance_line"].format(balance=balance),
        t["crystals_line"].format(crystals=crystals),
        "",
        t["level_line"].format(level=level),
        t["xp_line"].format(
            bar=_progress_bar(xp_into, xp_needed), current=xp_into, needed=xp_needed
        ),
        "",
        t["reputation_line"].format(red=rep_red, blue=rep_blue),
    ]
    return "\n".join(lines)


# ==========================
#   КЛАВИАТУРЫ
# ==========================

def _profile_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["friends_button"],
        callback_data="friends:open",
        style="primary",
        icon_custom_emoji_id=EMOJI_FRIENDS_ID,
    )
    builder.button(
        text=t["gifts_button"],
        callback_data="gifts:open",
        style="primary",
        icon_custom_emoji_id=EMOJI_GIFT_ID,
    )
    builder.button(
        text=t["settings_button"],
        callback_data="settings:open",
        style="primary",
        icon_custom_emoji_id=EMOJI_SETTINGS_ID,
    )
    builder.adjust(2, 1)
    return builder.as_markup()


async def _settings_keyboard(lang: str) -> InlineKeyboardBuilder:
    """Язык — в один ряд (как на онбординге в main.py: language_keyboard).
    Текущий язык выделяется цветом кнопки (style="success" — зелёная),
    а не галочкой/текстом, второй язык — обычный вид ("primary").
    Дальше — кнопки-ссылки "Новости"/"Наш чат" (только если ссылка
    задана из админки, см. admin.py: get_links/set_link — если админ
    ещё не указал ссылку, кнопка просто не показывается, чтобы не вести
    в никуда), затем кнопка смены имени и кнопка "Назад"."""
    t = SETTINGS_TEXTS[lang]
    builder = InlineKeyboardBuilder()
    for code, title in SETTINGS_LANGUAGES.items():
        builder.button(
            text=title,
            callback_data=f"settings:lang:{code}",
            style="success" if code == lang else "primary",
        )

    # Локальный импорт — та же причина, что и у остальных `import admin`
    # в этом файле (admin.py импортирует prof.py на уровне модуля,
    # обратный импорт наверху файла дал бы цикл).
    import admin

    links = await admin.get_links()
    link_rows = 0
    news_url = links.get("news")
    chat_url = links.get("chat")
    if news_url:
        builder.button(
            text=t["news_button"],
            url=news_url,
            style="primary",
            icon_custom_emoji_id=EMOJI_NEWS_ID,
        )
        link_rows += 1
    if chat_url:
        builder.button(
            text=t["chat_button"],
            url=chat_url,
            style="primary",
            icon_custom_emoji_id=EMOJI_CHAT_ID,
        )
        link_rows += 1

    builder.button(
        text=t["name_button"],
        callback_data="settings:name",
        style="primary",
        icon_custom_emoji_id=EMOJI_EDIT_NAME_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data="settings:back",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )

    sizes = [2]
    if link_rows:
        sizes.append(link_rows)
    sizes.extend([1, 1])
    builder.adjust(*sizes)
    return builder.as_markup()


def _name_cancel_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = SETTINGS_TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["cancel_button"], callback_data="settings:name_cancel")
    builder.adjust(1)
    return builder.as_markup()


def _gifts_list_keyboard(lang: str, page: int) -> InlineKeyboardBuilder:
    """Список подарков постранично — по 5 штук, в кнопке только
    название (плюс огонёк нужного цвета, чтобы сразу было видно тип
    награды)."""
    t = GIFTS_TEXTS[lang]
    total = _total_pages()
    page = max(0, min(page, total - 1))
    start = page * PAGE_SIZE

    builder = InlineKeyboardBuilder()
    for gift in GIFTS[start:start + PAGE_SIZE]:
        builder.button(
            text=gift["name"][lang],
            callback_data=f"gift:info:{gift['id']}",
            style="primary",
            icon_custom_emoji_id=GIFT_EMOJI.get(gift["id"], (EMOJI_GIFT_ID,))[0],
        )

    # Навигация: 🔙 / номер страницы / 🔜 — на краях кнопки листания
    # заменяются на "заглушку" (noop), а не пропадают, чтобы раскладка
    # не прыгала между страницами. Кастомный эмодзи-иконка ставится
    # только у активной кнопки — у заглушки её нет, чтобы визуально
    # было видно, что кнопка неактивна.
    prev_cb = f"gifts:page:{page - 1}" if page > 0 else "noop"
    next_cb = f"gifts:page:{page + 1}" if page < total - 1 else "noop"
    if page > 0:
        builder.button(text=" ", callback_data=prev_cb, icon_custom_emoji_id=EMOJI_PAGE_PREV_ID)
    else:
        builder.button(text="·", callback_data="noop")
    builder.button(
        text=t["page_indicator"].format(page=page + 1, total=total),
        callback_data="noop",
    )
    if page < total - 1:
        builder.button(text=" ", callback_data=next_cb, icon_custom_emoji_id=EMOJI_PAGE_NEXT_ID)
    else:
        builder.button(text="·", callback_data="noop")

    builder.button(text=t["back_button"], callback_data="gifts:back", icon_custom_emoji_id=EMOJI_BACK_ID)

    builder.adjust(*([1] * len(GIFTS[start:start + PAGE_SIZE])), 3, 1)
    return builder.as_markup()


def _gift_info_keyboard(lang: str, gift_id: int, page: int) -> InlineKeyboardBuilder:
    t = GIFTS_TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["send_button"],
        callback_data=f"gift:send:{gift_id}",
        style="primary",
        icon_custom_emoji_id=EMOJI_GIFT_ID,
    )
    builder.button(
        text=t["buy_button"],
        callback_data=f"gift:buy:{gift_id}",
        style="primary",
        icon_custom_emoji_id=EMOJI_GIFT_BUY_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=f"gifts:page:{page}",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def _send_cancel_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = GIFTS_TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["cancel_button"], callback_data="gift:send_cancel")
    builder.adjust(1)
    return builder.as_markup()


def _build_settings_text(lang: str) -> str:
    t = SETTINGS_TEXTS[lang]
    lines = [
        t["title"],
        t["separator"],
        t["language_label"],
        t["language_hint"],
    ]
    return "\n".join(lines)


async def _build_gifts_list_text(lang: str, user_id: int) -> str:
    t = GIFTS_TEXTS[lang]
    balance = await shop.get_balance(user_id)
    crystals = await get_crystals(user_id)
    lines = [
        t["title"],
        t["subtitle"].format(currency=shop.CURRENCY, crystal=CE_CRYSTAL),
        "",
        t["balance_line"].format(balance=balance, crystals=crystals),
    ]
    return "\n".join(lines)


async def _build_gift_info_text(lang: str, gift: dict, user_id: int) -> str:
    t = GIFTS_TEXTS[lang]
    fire = _FIRE_ICON[gift["reward"]]
    color = t["color_red"] if gift["reward"] == "red" else t["color_blue"]
    currency_icon = _CURRENCY_ICON[gift["currency"]]
    balance = await shop.get_balance(user_id)
    crystals = await get_crystals(user_id)
    lines = [
        f"{_gift_ce(gift['id'])} <b>{gift['name'][lang]}</b>",
        t["separator"],
        f"<i>{gift['desc'][lang]}</i>",
        "",
        t["gift_cost_line"].format(cost=gift["cost"], currency_icon=currency_icon),
        t["gift_reward_line"].format(amount=gift["amount"], fire=fire),
        "",
        t["gift_about"].format(fire=fire, color=color),
        "",
        t["balance_line"].format(balance=balance, crystals=crystals),
    ]
    return "\n".join(lines)


# ==========================
#   ДАРЕНИЕ ДРУГОМУ ИГРОКУ (по @username или ID)
# ==========================
#
# Отдельного состояния из main.py (Onboarding) сюда не тащим — это
# создало бы цикл импортов prof -> main -> prof. Вместо этого свой
# маленький FSM-стейт: пока он активен, следующее текстовое сообщение
# от игрока трактуется как @username/ID получателя. lang/gender,
# записанные в data через update_data, при смене состояния не
# теряются — state.set_state() их не трогает.

class GiftFlow(StatesGroup):
    waiting_target = State()


# ==========================
#   СМЕНА ИМЕНИ В ПРОФИЛЕ
# ==========================
#
# По тому же принципу, что и GiftFlow выше: отдельный маленький FSM-стейт,
# пока он активен — следующее текстовое сообщение от игрока трактуется как
# новое имя, а не идёт в другие разделы бота.

class NameFlow(StatesGroup):
    waiting_name = State()


async def _clear_gift_flow(state: FSMContext) -> None:
    data = await state.get_data()
    data.pop("gift_send_id", None)
    await state.set_data(data)
    await state.set_state(None)


async def _resolve_target(raw: str) -> int | None:
    """@username или числовой ID -> user_id, если такой игрок уже
    известен боту (есть строка в users — как минимум один апдейт от
    него уже прошёл через admin.UserTrackingMiddleware)."""
    raw = raw.strip()
    if raw.startswith("@"):
        raw = raw[1:]
    if not raw:
        return None

    db = await database.get_db()

    if raw.isdigit():
        user_id = int(raw)
        async with db.execute(
            "SELECT 1 FROM users WHERE user_id = ?", (user_id,)
        ) as cursor:
            row = await cursor.fetchone()
        return user_id if row else None

    async with db.execute(
        "SELECT user_id FROM users WHERE username = ? COLLATE NOCASE", (raw,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["user_id"] if row else None


# ==========================
#   ХЕНДЛЕРЫ
# ==========================

async def _get_lang(state: FSMContext, user_id: int) -> str:
    """Возвращает язык игрока — сперва из FSM-состояния (кэш в памяти), а
    при его отсутствии (например, сразу после рестарта бота — MemoryStorage
    рестарт не переживает, см. main.py: dp = Dispatcher(storage=MemoryStorage()))
    — напрямую из БД, где он сохраняется онбордингом навсегда. Без этого
    отката игрок, выбравший английский, после рестарта бота видел бы этот
    раздел на русском, пока заново не пройдёт /start."""
    data = await state.get_data()
    lang = data.get("lang")
    if lang:
        return lang

    onboarding = await database.get_onboarding(user_id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"
    await state.update_data(lang=lang)
    return lang


def _callback_int(callback: CallbackQuery, index: int = 2) -> int | None:
    """Безопасно достаёт int-параметр (id подарка / номер страницы) из
    callback.data вида "gift:buy:{id}" — на случай испорченного или
    сфабрикованного callback_data (не должно случаться при обычном
    использовании, т.к. клавиатуры генерируем сами, но лучше не
    падать необработанным исключением на кривом вводе, чем полагаться
    на то, что данные всегда "свои"). Возвращает None, если сегмента
    нет или он не число."""
    parts = callback.data.split(":")
    if len(parts) <= index:
        return None
    try:
        return int(parts[index])
    except ValueError:
        return None


@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_profile(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    text = await _build_profile_text(lang, message.from_user)

    # Картинка раздела (см. admin.py: admin:sections, ключ "profile") —
    # если задана, экран профиля отправляется как фото с текстом в
    # подписи, иначе как обычно текстом. Локальный импорт — admin.py
    # сам импортирует prof.py на верхнем уровне (цикл).
    import admin

    await admin.send_with_section_image(message, "profile", text, reply_markup=_profile_keyboard(lang))

    # Общая ачивка "С днём рождения аккаунта!" — проверяется тут же, при
    # открытии профиля (первый и самый очевидный момент, где first_seen
    # игрока уже под рукой), а не отдельным фоновым джобом.
    row = await _get_user_row(message.from_user.id)
    first_seen = row["first_seen"] if row else None
    if first_seen:
        age_days = (
            datetime.datetime.now() - datetime.datetime.fromtimestamp(first_seen)
        ).days
        if age_days >= 365:
            import achives

            achv_result = await achives.unlock(message.from_user.id, "general_anniversary")
            if achv_result:
                await message.answer(achives.format_unlock_text(lang, achv_result))
                lvl_text = _achv_level_up_text(lang, achv_result)
                if lvl_text:
                    await message.answer(lvl_text)

    # Общая ачивка "Богач" (заработать суммарно 10 000 монет) — счётчик
    # копится сам, лениво, из shop._change_balance (см. bump_coins_earned_no_lock
    # выше); момент выдачи/уведомления — здесь, при открытии профиля,
    # т.к. у shop.py на уровне _change_balance нет доступа ни к bot, ни к
    # языку игрока, чтобы уведомить сразу в момент начисления.
    if await get_coins_earned(message.from_user.id) >= 10_000:
        import achives

        achv_result = await achives.unlock(message.from_user.id, "general_coins_earned_10000")
        if achv_result:
            await message.answer(achives.format_unlock_text(lang, achv_result))
            lvl_text = _achv_level_up_text(lang, achv_result)
            if lvl_text:
                await message.answer(lvl_text)


@router.callback_query(F.data == "gifts:open")
async def open_gifts(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _build_gifts_list_text(lang, callback.from_user.id)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_gifts_list_keyboard(lang, 0))
    await callback.answer()


@router.callback_query(F.data == "gifts:back")
async def back_to_profile(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _build_profile_text(lang, callback.from_user)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_profile_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == "noop")
async def noop(callback: CallbackQuery) -> None:
    # Заглушка / индикатор страницы — просто гасим часики.
    await callback.answer()


@router.callback_query(F.data == "settings:open")
async def open_settings(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = _build_settings_text(lang)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=await _settings_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == "settings:back")
async def close_settings(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _build_profile_text(lang, callback.from_user)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_profile_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data.startswith("settings:lang:"))
async def change_language(callback: CallbackQuery, state: FSMContext) -> None:
    new_lang = callback.data.split(":")[2] if len(callback.data.split(":")) > 2 else None
    if new_lang not in SETTINGS_TEXTS:
        # Испорченный/сфабрикованный callback_data — тот же принцип
        # защиты, что и в _callback_int выше: не падаем, просто гасим
        # часики.
        await callback.answer()
        return

    current_lang = await _get_lang(state, callback.from_user.id)
    if new_lang == current_lang:
        # Этот язык уже выбран — переключать нечего.
        await callback.answer()
        return

    # Сохраняем сразу в двух местах:
    #   - в БД (database.save_lang) — чтобы язык не потерялся после
    #     рестарта бота: MemoryStorage состояние FSM не переживает
    #     (см. комментарий в _get_lang выше);
    #   - в FSM-состоянии (state.update_data) — чтобы ВСЕ остальные
    #     разделы бота (сад, пекарня, донаты, ачивки и т.д. — у каждого
    #     своя копия _get_lang по тому же принципу: сначала state, потом
    #     БД) увидели новый язык сразу, без необходимости заново писать
    #     /start. FSMContext.data общий на игрока для всех роутеров
    #     сразу, независимо от того, какой из них его записал.
    await database.save_lang(callback.from_user.id, new_lang)
    await state.update_data(lang=new_lang)

    text = _build_settings_text(new_lang)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=await _settings_keyboard(new_lang))

    # Реплай-клавиатура главного меню (main.py: main_menu_keyboard) тоже
    # локализована и сама по себе не обновляется — без явной пересылки
    # она осталась бы на старом языке до следующего /start. Локальный
    # импорт main — тот же приём, что уже используют другие модули для
    # кнопки "Назад" (см. комментарий в main.py про `import main`),
    # нужен, чтобы не ловить цикл импортов на уровне модуля (main.py сам
    # делает `import prof` наверху).
    import main

    await callback.message.answer(
        SETTINGS_TEXTS[new_lang]["lang_changed"].format(name=SETTINGS_LANGUAGES[new_lang]),
        reply_markup=main.main_menu_keyboard(new_lang),
    )

    await callback.answer()


@router.callback_query(F.data == "settings:name")
async def open_name_change(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = SETTINGS_TEXTS[lang]

    await state.set_state(NameFlow.waiting_name)

    import admin

    await admin.smart_edit(
        callback.message,
        t["name_prompt"].format(limit=NAME_MAX_LENGTH),
        reply_markup=_name_cancel_keyboard(lang),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:name_cancel")
async def cancel_name_change(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    lang = await _get_lang(state, callback.from_user.id)
    text = _build_settings_text(lang)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=await _settings_keyboard(lang))
    await callback.answer()


@router.message(NameFlow.waiting_name)
async def process_name_change(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    t = SETTINGS_TEXTS[lang]

    raw_text = message.text or ""

    # Пусто, только пробелы, или несколько строк — отклоняем без записи в
    # БД: многострочное "имя" сломало бы вёрстку карточки профиля (там
    # оно всегда одна строка рядом с CE_NAME).
    if not raw_text.strip() or "\n" in raw_text:
        await message.answer(t["name_invalid"], reply_markup=_name_cancel_keyboard(lang))
        return

    # Лимит — по видимым символам (message.text): кастомный эмодзи там
    # занимает один символ-заглушку независимо от того, как он выглядит,
    # так что лимит остаётся честным вне зависимости от форматирования.
    if len(raw_text) > NAME_MAX_LENGTH:
        await message.answer(
            t["name_too_long"].format(limit=NAME_MAX_LENGTH),
            reply_markup=_name_cancel_keyboard(lang),
        )
        return

    # _sanitize_name_html (а не message.html_text напрямую!) — конвертирует
    # entities сообщения (в т.ч. кастомные эмодзи Telegram Premium, жирный/
    # курсив/спойлер) в HTML тем же способом, что и aiogram, но сначала
    # отбрасывает сущности вроде text_link/mention (см. комментарий у
    # _NAME_ALLOWED_ENTITY_TYPES выше) — иначе игрок мог бы вставить в своё
    # имя кликабельную ссылку, которая показывалась бы всем в лидерборде
    # (leaders.py) и в уведомлении получателю подарка (ниже в этом файле).
    # Результат по-прежнему готовый безопасный HTML, поэтому в профиле
    # (_build_profile_text) и везде далее он подставляется как есть, без
    # html.escape.
    name_html = _sanitize_name_html(message)

    await database.save_display_name(message.from_user.id, name_html)
    await state.set_state(None)

    await message.answer(t["name_changed"])

    text = await _build_profile_text(lang, message.from_user)

    import admin

    await admin.send_with_section_image(message, "profile", text, reply_markup=_profile_keyboard(lang))


@router.callback_query(F.data.startswith("gifts:page:"))
async def show_gifts_page(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    page = _callback_int(callback)
    if page is None:
        await callback.answer()
        return
    text = await _build_gifts_list_text(lang, callback.from_user.id)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_gifts_list_keyboard(lang, page))
    await callback.answer()


@router.callback_query(F.data.startswith("gift:info:"))
async def show_gift_info(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = GIFTS_TEXTS[lang]
    gift_id = _callback_int(callback)
    gift = GIFTS_BY_ID.get(gift_id) if gift_id is not None else None

    if gift is None:
        await callback.answer(t["not_found"], show_alert=True)
        return

    page = _gift_page(gift_id)
    text = await _build_gift_info_text(lang, gift, callback.from_user.id)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_gift_info_keyboard(lang, gift_id, page))
    await callback.answer()


@router.callback_query(F.data.startswith("gift:buy:"))
async def process_gift_buy(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = GIFTS_TEXTS[lang]
    gift_id = _callback_int(callback)
    if gift_id is None:
        await callback.answer(t["not_found"], show_alert=True)
        return

    result = await buy_gift(callback.from_user.id, gift_id)

    if not result["ok"]:
        if result["reason"] == "insufficient":
            gift = result["gift"]
            if gift["currency"] == "coins":
                await callback.answer(
                    t["insufficient_coins"].format(cost=gift["cost"]), show_alert=True
                )
            else:
                await callback.answer(
                    t["insufficient_crystals"].format(cost=gift["cost"]), show_alert=True
                )
        elif result["reason"] == "daily_limit":
            await callback.answer(
                t["daily_limit_buy"].format(used=result["used"], limit=result["limit"]),
                show_alert=True,
            )
        else:
            await callback.answer(t["not_found"], show_alert=True)
        return

    gift = result["gift"]
    fire = _FIRE_ICON_PLAIN[gift["reward"]]
    await callback.answer(
        t["buy_success"].format(amount=gift["amount"], fire=fire), show_alert=True
    )

    # Баланс (Pn/кристаллы) изменился — перерисовываем карточку подарка,
    # чтобы цифры внизу были актуальными.
    page = _gift_page(gift_id)
    text = await _build_gift_info_text(lang, gift, callback.from_user.id)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_gift_info_keyboard(lang, gift_id, page))

    # Ачивка "Щедрость" — за подарок себе тоже засчитывается, наравне с
    # отправкой другому (см. process_gift_target). Локальный импорт —
    # achives.py импортирует prof на верхнем уровне.
    import achives

    achv_result = await achives.unlock(callback.from_user.id, "gift_giver")
    if achv_result:
        await callback.message.answer(achives.format_unlock_text(lang, achv_result))
        lvl_text = _achv_level_up_text(lang, achv_result)
        if lvl_text:
            await callback.message.answer(lvl_text)

    # Общие ачивки, завязанные на сам факт покупки конкретного подарка
    # (не на "кому предназначен" — см. process_gift_target для отправки).
    await _mark_gift_type_seen(callback.from_user.id, gift_id)
    general_ids = []
    if gift_id in RARE_GIFT_IDS:
        general_ids.append("general_rare_gift")
    if await get_gift_types_count(callback.from_user.id) >= len(GIFTS):
        general_ids.append("general_all_gifts")
    if await _is_daily_limit_maxed(callback.from_user.id, "buy_daily_amount", "buy_daily_date"):
        general_ids.append("general_daily_gift_limit")
    for achv_id in general_ids:
        result2 = await achives.unlock(callback.from_user.id, achv_id)
        if result2:
            await callback.message.answer(achives.format_unlock_text(lang, result2))
            lvl_text = _achv_level_up_text(lang, result2)
            if lvl_text:
                await callback.message.answer(lvl_text)


@router.callback_query(F.data.startswith("gift:send:"))
async def gift_send_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = GIFTS_TEXTS[lang]
    gift_id = _callback_int(callback)
    gift = GIFTS_BY_ID.get(gift_id) if gift_id is not None else None

    if gift is None:
        await callback.answer(t["not_found"], show_alert=True)
        return

    await state.update_data(gift_send_id=gift_id)
    await state.set_state(GiftFlow.waiting_target)

    text = t["send_prompt"].format(name=gift["name"][lang], emoji=_gift_ce(gift_id))

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=_send_cancel_keyboard(lang))
    await callback.answer()


@router.callback_query(F.data == "gift:send_cancel")
async def gift_send_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    data = await state.get_data()
    gift_id = data.get("gift_send_id")
    await _clear_gift_flow(state)

    gift = GIFTS_BY_ID.get(gift_id)

    import admin

    if gift is not None:
        page = _gift_page(gift_id)
        text = await _build_gift_info_text(lang, gift, callback.from_user.id)
        await admin.smart_edit(callback.message, text, reply_markup=_gift_info_keyboard(lang, gift_id, page))
    else:
        text = await _build_gifts_list_text(lang, callback.from_user.id)
        await admin.smart_edit(callback.message, text, reply_markup=_gifts_list_keyboard(lang, 0))
    await callback.answer()


@router.message(GiftFlow.waiting_target)
async def process_gift_target(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    t = GIFTS_TEXTS[lang]

    data = await state.get_data()
    gift_id = data.get("gift_send_id")
    gift = GIFTS_BY_ID.get(gift_id)
    if gift is None:
        await _clear_gift_flow(state)
        return

    raw = (message.text or "").strip()

    # Не блокируем обычную навигацию: если это наша же кнопка "Профиль" —
    # выходим из режима ввода получателя и просто открываем профиль.
    if raw in BUTTON_TEXT.values():
        await _clear_gift_flow(state)
        await open_profile(message, state)
        return

    target_id = await _resolve_target(raw)
    if target_id is None:
        await message.answer(t["target_not_found"], reply_markup=_send_cancel_keyboard(lang))
        return

    if target_id == message.from_user.id:
        await message.answer(t["cant_send_self"], reply_markup=_send_cancel_keyboard(lang))
        return

    reserve_result = await _reserve_daily_send(message.from_user.id, gift["amount"])
    if reserve_result is not None:
        used, limit = reserve_result
        await _clear_gift_flow(state)
        await message.answer(t["daily_limit_send"].format(used=used, limit=limit))
        return

    if gift["currency"] == "coins":
        spent = await _spend_coins(message.from_user.id, gift["cost"])
    else:
        spent = await _spend_crystals(message.from_user.id, gift["cost"])

    if not spent:
        await _release_daily_send(message.from_user.id, gift["amount"])
        await _clear_gift_flow(state)
        key = "insufficient_coins" if gift["currency"] == "coins" else "insufficient_crystals"
        await message.answer(t[key].format(cost=gift["cost"]))
        return

    if gift["reward"] == "red":
        await add_reputation_red(target_id, gift["amount"])
    else:
        await add_reputation_blue(target_id, gift["amount"])

    await _clear_gift_flow(state)

    fire = _FIRE_ICON[gift["reward"]]
    target_label = html.escape(raw if raw.startswith("@") else f"@{raw}" if not raw.isdigit() else raw)
    await message.answer(
        t["send_success"].format(
            name=gift["name"][lang],
            target=target_label,
            cost=gift["cost"],
            currency_icon=_CURRENCY_ICON[gift["currency"]],
            amount=gift["amount"],
            fire=fire,
        )
    )

    # Уведомляем получателя, если это возможно (бот не заблокирован и
    # хотя бы раз видел его апдейт) — best effort, ошибку молча гасим.
    try:
        recipient_onboarding = await database.get_onboarding(target_id)
        recipient_lang = recipient_onboarding["lang"] if recipient_onboarding else "ru"
        sender_name = await _display_name(message.from_user.id, message.from_user.first_name)
        await message.bot.send_message(
            target_id,
            GIFTS_TEXTS[recipient_lang]["received"].format(
                sender=sender_name,
                name=gift["name"][recipient_lang],
                amount=gift["amount"],
                fire=fire,
                emoji=_gift_ce(gift["id"]),
            ),
        )
    except Exception:
        pass

    # Ачивка "Щедрость" — за отправку подарка другому, наравне с покупкой
    # себе (см. process_gift_buy). Локальный импорт по той же причине.
    import achives

    achv_result = await achives.unlock(message.from_user.id, "gift_giver")
    if achv_result:
        await message.answer(achives.format_unlock_text(lang, achv_result))
        lvl_text = _achv_level_up_text(lang, achv_result)
        if lvl_text:
            await message.answer(lvl_text)

    # Общие ачивки за отправку: счётчики "отправил"/"получил" у отправителя
    # и получателя по отдельности, плюс те же условия по видам подарков и
    # дневному лимиту, что и в process_gift_buy (счётчик лимита здесь свой —
    # send_daily_amount/date, отдельный от покупки себе).
    sent_count = await _bump_counter(message.from_user.id, "gifts_sent_count")
    received_count = await _bump_counter(target_id, "gifts_received_count")
    await _mark_gift_type_seen(message.from_user.id, gift["id"])

    sender_general_ids = []
    if sent_count >= 10:
        sender_general_ids.append("general_gifts_sent_10")
    if gift["id"] in RARE_GIFT_IDS:
        sender_general_ids.append("general_rare_gift")
    if await get_gift_types_count(message.from_user.id) >= len(GIFTS):
        sender_general_ids.append("general_all_gifts")
    if await _is_daily_limit_maxed(message.from_user.id, "send_daily_amount", "send_daily_date"):
        sender_general_ids.append("general_daily_gift_limit")
    for achv_id in sender_general_ids:
        result2 = await achives.unlock(message.from_user.id, achv_id)
        if result2:
            await message.answer(achives.format_unlock_text(lang, result2))
            lvl_text = _achv_level_up_text(lang, result2)
            if lvl_text:
                await message.answer(lvl_text)

    # Достижение получателя ("Всеобщий любимец") — шлём отдельным
    # сообщением получателю, best-effort, как и уведомление о подарке
    # чуть выше (получатель может заблокировать бота и т.п.).
    if received_count >= 10:
        await _notify_general_achievements(message.bot, target_id, ["general_gifts_received_10"])
