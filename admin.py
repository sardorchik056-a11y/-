"""
Раздел "Админ-панель" (/admin).

Доступ:
    Только для user_id из ADMIN_IDS ниже. Ограничение стоит на уровне
    роутера (router.message.filter(...) / router.callback_query.filter(...)),
    поэтому для всех остальных пользователей хендлеры этого модуля
    попросту не существуют — ни /admin, ни колбэки "admin:..." не
    сработают, что бы они ни присылали.

Функции:
    - Выдать Pn игроку (по @username или числовому user_id)
    - Выдать фрукты игроку (по @username или числовому user_id)
    - Статистика бота (игроки, панды, экономика, рынок)
    - Профиль игрока (панда/баланс/корзина/статистика рынка одним экраном)
    - Рассылка сообщения всем, кто хотя бы раз писал боту

Поиск игрока по username:
    Telegram Bot API не даёт способа узнать user_id по @username "с
    нуля" — только если бот уже видел апдейт от этого пользователя.
    Поэтому модуль ведёт собственную таблицу users (схема — в
    database.py), которая обновляется на КАЖДЫЙ входящий апдейт от
    любого пользователя через UserTrackingMiddleware ниже. Если игрок
    ни разу не писал боту (или только что сменил username, а после
    этого ещё не прислал ни одного сообщения) — поиск его не найдёт,
    это ограничение Bot API, а не баг.

    Числовой user_id, в отличие от username, можно использовать и для
    игрока, которого бот ещё не видел (например, ID узнали из другого
    источника) — начисление Pn/фруктов пройдёт, но отправить игроку
    уведомление в этом случае не получится (бот ещё не имеет с ним
    открытого чата), это тихо игнорируется.

Подключение в main.py:
    import admin
    ...
    dp.update.outer_middleware(admin.UserTrackingMiddleware())
    dp.include_router(admin.router)   # до panda/garden/shop

Настройка:
    Впишите свой Telegram user_id в ADMIN_IDS ниже. Узнать его можно,
    например, у @userinfobot. Пока список пуст, админ-панель никому не
    видна.

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import asyncio
import datetime
import html
import logging
import time

import aiosqlite
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, TelegramObject, User
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import donate
import garden
import panda
import prof
import shop
import bakery

logger = logging.getLogger(__name__)

router = Router(name="admin")


# ==========================
#   НАСТРОЙКИ
# ==========================

# TODO: впишите сюда свой Telegram user_id (и id других админов, если
# нужно). Узнать его можно, например, у @userinfobot. Пока множество
# пустое — админ-панель недоступна вообще никому (пример заполнения —
# в закомментированной строке ниже; не забудьте раскомментировать и
# подставить настоящий id, а не оставлять его как есть).
ADMIN_IDS: set[int] = set()
ADMIN_IDS = {8118184388}

router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))

# задержка между сообщениями рассылки — защита от лимитов Bot API
# (~30 сообщений/сек суммарно по всем чатам)
BROADCAST_DELAY_SECONDS = 0.05


# ==========================
#   СОСТОЯНИЯ (FSM)
# ==========================

class AdminStates(StatesGroup):
    give_pn_target = State()
    give_pn_amount = State()
    give_crystals_target = State()
    give_crystals_amount = State()
    give_fruit_target = State()
    give_fruit_amount = State()
    give_bakery_target = State()
    give_bakery_amount = State()
    lookup_target = State()
    broadcast_text = State()
    broadcast_button_choice = State()
    broadcast_button_text = State()
    broadcast_button_url = State()
    broadcast_confirm = State()
    skin_upload_photo = State()
    section_upload_photo = State()
    link_upload_url = State()
    adlink_title = State()


# ==========================
#   ТАБЛИЦА users (см. database.py)
# ==========================
#
# Владелец таблицы users — этот модуль (по аналогии с тем, как garden.py
# владеет garden_plots/garden_inventory, хотя схема создаётся централизованно
# в database.py). Запись обновляется на каждый апдейт от любого игрока —
# см. UserTrackingMiddleware ниже — и служит только для двух вещей:
# поиска user_id по @username и списка получателей рассылки.

async def track_user(user_id: int, username: str | None, first_name: str | None) -> None:
    """Запоминает/обновляет базовые сведения об игроке. Не экономическая
    операция — коммитится обычной "стопкой" (database.commit), без
    немедленного flush()."""
    db = await database.get_db()
    now = time.time()
    await db.execute(
        """
        INSERT INTO users (user_id, username, first_name, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            username = excluded.username,
            first_name = excluded.first_name,
            last_seen = excluded.last_seen
        """,
        (user_id, username, first_name, now, now),
    )
    await database.commit()


async def get_user_row(user_id: int) -> aiosqlite.Row | None:
    db = await database.get_db()
    async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cursor:
        return await cursor.fetchone()


async def find_user_by_username(username: str) -> aiosqlite.Row | None:
    db = await database.get_db()
    async with db.execute(
        "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
    ) as cursor:
        return await cursor.fetchone()


async def get_all_user_ids() -> list[int]:
    db = await database.get_db()
    async with db.execute("SELECT user_id FROM users") as cursor:
        return [row["user_id"] async for row in cursor]


# ==========================
#   ИЗОБРАЖЕНИЯ РАЗДЕЛОВ
# ==========================
# Единая картинка на раздел (в отличие от скинов панды, где картинка —
# на каждый скин отдельно, см. panda.py: get_skin_image). Раздел
# "start" — это картинка, которая раньше отправлялась вместе с выбором
# языка на /start (main.py, раньше хранилась в bot_data.json через
# /setimg1 — теперь тоже здесь, вместе с остальными).
#
# Ключ раздела -> заголовок в списке в админке. Порядок здесь и
# определяет порядок кнопок в разделе "🖼 Изображения разделов".
SECTION_TITLES: dict[str, str] = {
    "achievements": "🎖 Достижения",
    "garden": "🌱 Сад",
    "bakery": "🥐 Пекарня",
    "profile": "👤 Профиль",
    "donate": "💎 Донаты",
    "leaders": "🏆 Лидеры",
    "start": "🚀 Картинка для /start",
}
SECTION_ORDER: list[str] = list(SECTION_TITLES.keys())


async def get_section_image(section_key: str) -> str | None:
    """Возвращает file_id картинки раздела, либо None, если не задана —
    вызывается из модулей разделов (achives.py/garden.py/bakery.py/
    prof.py/donate.py/leaders.py) и из main.py (раздел "start") при построении
    экрана, чтобы решить, отправлять ли фото вместо/вместе с текстом."""
    db = await database.get_db()
    async with db.execute(
        "SELECT file_id FROM section_images WHERE section_key = ?", (section_key,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["file_id"] if row else None


async def get_section_images() -> dict[str, str]:
    """Все заданные картинки разделов разом — {section_key: file_id}."""
    db = await database.get_db()
    async with db.execute("SELECT section_key, file_id FROM section_images") as cursor:
        return {row["section_key"]: row["file_id"] async for row in cursor}


async def set_section_image(section_key: str, file_id: str) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO section_images (section_key, file_id, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (section_key) DO UPDATE SET
            file_id = excluded.file_id,
            updated_at = excluded.updated_at
        """,
        (section_key, file_id, time.time()),
    )
    await database.commit()


async def clear_section_image(section_key: str) -> None:
    db = await database.get_db()
    await db.execute("DELETE FROM section_images WHERE section_key = ?", (section_key,))
    await database.commit()


# ==========================
#   ССЫЛКИ (Новости / Наш чат)
# ==========================
# Кнопки-ссылки в разделе "Профиль -> Настройки" (prof.py: _settings_keyboard),
# ведущие на новостной канал/чат бота. Ссылка задаётся здесь, в админке, а
# не хардкодом в prof.py, — чтобы её можно было поменять без деплоя.
# Своей таблицы под это изначально не было — заводим её лениво, тем же
# приёмом, что и prof.py для колонки crystals (см. _ensure_gift_schema
# там) — CREATE TABLE IF NOT EXISTS при первом обращении.
#
# Если ссылка не задана — кнопка в настройках просто не показывается
# (см. prof.py: _settings_keyboard), чтобы не вести в никуда.
LINK_TITLES: dict[str, str] = {
    "news": "📣 Новости",
    "chat": "💬 Наш чат",
}
LINK_ORDER: list[str] = list(LINK_TITLES.keys())

_links_schema_ready = False
_links_schema_lock = asyncio.Lock()


async def _ensure_links_schema() -> None:
    global _links_schema_ready
    if _links_schema_ready:
        return
    async with _links_schema_lock:
        if _links_schema_ready:
            return
        db = await database.get_db()
        await db.execute(
            "CREATE TABLE IF NOT EXISTS bot_links ("
            "link_key TEXT PRIMARY KEY, url TEXT NOT NULL, updated_at REAL)"
        )
        await database.commit()
        _links_schema_ready = True


async def get_link(link_key: str) -> str | None:
    await _ensure_links_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT url FROM bot_links WHERE link_key = ?", (link_key,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["url"] if row else None


async def get_links() -> dict[str, str]:
    """Все заданные ссылки разом — {link_key: url}. Используется в
    prof.py: _settings_keyboard при построении клавиатуры настроек."""
    await _ensure_links_schema()
    db = await database.get_db()
    async with db.execute("SELECT link_key, url FROM bot_links") as cursor:
        return {row["link_key"]: row["url"] async for row in cursor}


async def set_link(link_key: str, url: str) -> None:
    await _ensure_links_schema()
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO bot_links (link_key, url, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT (link_key) DO UPDATE SET
            url = excluded.url,
            updated_at = excluded.updated_at
        """,
        (link_key, url, time.time()),
    )
    await database.commit()


async def clear_link(link_key: str) -> None:
    await _ensure_links_schema()
    db = await database.get_db()
    await db.execute("DELETE FROM bot_links WHERE link_key = ?", (link_key,))
    await database.commit()


# ==========================
#   РЕКЛАМНЫЕ ССЫЛКИ (статистика переходов)
# ==========================
# Позволяет создать в админке отдельную диплинк-ссылку вида
# https://t.me/<bot>?start=ad_<slug> под конкретную рекламную кампанию
# (пост у блогера, объявление и т.п.) и увидеть, сколько людей по ней
# реально пришло. Механика полностью зеркалит реферальную ссылку из
# prof.py ("Друзья"): переход засчитывается только для по-настоящему
# новых игроков (main.py: cmd_start, та же ветка, что и payload
# "ref<id>"), а "вступил(а)" — как только игрок выбрал язык И пол
# (main.py: process_gender, рядом с prof.credit_referral). Один и тот
# же user_id может быть засчитан только за одну рекламную ссылку —
# той, по которой он перешёл первым (ad_link_visits.user_id — PRIMARY
# KEY), повторные переходы по другим ad-ссылкам того же пользователя
# на статистику уже не влияют.

_adlinks_schema_ready = False
_adlinks_schema_lock = asyncio.Lock()


async def _ensure_adlinks_schema() -> None:
    global _adlinks_schema_ready
    if _adlinks_schema_ready:
        return
    async with _adlinks_schema_lock:
        if _adlinks_schema_ready:
            return
        db = await database.get_db()
        await db.execute(
            "CREATE TABLE IF NOT EXISTS ad_links ("
            "slug TEXT PRIMARY KEY, title TEXT NOT NULL, created_at REAL NOT NULL)"
        )
        await db.execute(
            "CREATE TABLE IF NOT EXISTS ad_link_visits ("
            "user_id INTEGER PRIMARY KEY, slug TEXT NOT NULL, "
            "started_at REAL NOT NULL, joined INTEGER NOT NULL DEFAULT 0, joined_at REAL)"
        )
        # Статистика ссылки (клики/вступления) считается COUNT(*) по
        # slug — без индекса на большой базе это full table scan.
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_ad_link_visits_slug ON ad_link_visits(slug)"
        )
        await database.commit()
        _adlinks_schema_ready = True


def _slugify(title: str) -> str:
    """Грубая транслитерация + нормализация в slug для диплинк-параметра
    (Bot API допускает в start-параметре только [A-Za-z0-9_-], поэтому
    кириллицу и пробелы приходится заменять)."""
    table = {
        "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
        "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
        "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
        "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
        "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
    }
    out = []
    for ch in title.lower():
        if ch in table:
            out.append(table[ch])
        elif ch.isalnum() and ch.isascii():
            out.append(ch)
        elif ch in (" ", "-", "_"):
            out.append("_")
    slug = "".join(out).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:40] or "link"


async def _unique_ad_slug(base_slug: str) -> str:
    db = await database.get_db()
    slug = base_slug
    n = 2
    while True:
        async with db.execute("SELECT 1 FROM ad_links WHERE slug = ?", (slug,)) as cursor:
            if await cursor.fetchone() is None:
                return slug
        slug = f"{base_slug}_{n}"
        n += 1


# Сериализует создание ссылок: между проверкой "slug свободен" в
# _unique_ad_slug и самим INSERT в create_ad_link есть await-точки, и
# без лока два одновременных admin:adlink_new (например, два админа
# одновременно создают кампанию с одинаковым названием) могли бы оба
# пройти проверку с одним и тем же slug, а второй INSERT упал бы с
# IntegrityError (PRIMARY KEY). Сам одиночный INSERT/UPDATE в SQLite и
# так атомарен (см. database.py), лок нужен именно для связки
# "проверить -> вставить" из двух отдельных запросов.
_adlink_create_lock = asyncio.Lock()


async def create_ad_link(title: str) -> str:
    """Создаёт новую рекламную ссылку с заголовком title, возвращает её
    slug (уникальный, за счёт _unique_ad_slug + _adlink_create_lock)."""
    await _ensure_adlinks_schema()
    async with _adlink_create_lock:
        slug = await _unique_ad_slug(_slugify(title))
        db = await database.get_db()
        await db.execute(
            "INSERT INTO ad_links (slug, title, created_at) VALUES (?, ?, ?)",
            (slug, title, time.time()),
        )
        await database.commit()
    return slug


async def get_ad_links() -> list[aiosqlite.Row]:
    """Все рекламные ссылки, новые сверху."""
    await _ensure_adlinks_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT * FROM ad_links ORDER BY created_at DESC"
    ) as cursor:
        return [row async for row in cursor]


async def get_ad_link(slug: str) -> aiosqlite.Row | None:
    await _ensure_adlinks_schema()
    db = await database.get_db()
    async with db.execute("SELECT * FROM ad_links WHERE slug = ?", (slug,)) as cursor:
        return await cursor.fetchone()


async def get_ad_link_stats(slug: str) -> dict:
    """{'clicks': сколько новых игроков перешло по ссылке, 'joined':
    сколько из них реально прошли онбординг (язык + пол)}."""
    await _ensure_adlinks_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS clicks, COALESCE(SUM(joined), 0) AS joined "
        "FROM ad_link_visits WHERE slug = ?",
        (slug,),
    ) as cursor:
        row = await cursor.fetchone()
    return {"clicks": row["clicks"], "joined": row["joined"]}


async def get_all_ad_link_stats() -> dict[str, dict]:
    """{slug: {'clicks':.., 'joined':..}} одним запросом — для списка
    ссылок в админке, чтобы не дёргать БД по разу на каждую строку."""
    await _ensure_adlinks_schema()
    db = await database.get_db()
    async with db.execute(
        "SELECT slug, COUNT(*) AS clicks, COALESCE(SUM(joined), 0) AS joined "
        "FROM ad_link_visits GROUP BY slug"
    ) as cursor:
        return {row["slug"]: {"clicks": row["clicks"], "joined": row["joined"]} async for row in cursor}


async def delete_ad_link(slug: str) -> None:
    await _ensure_adlinks_schema()
    db = await database.get_db()
    await db.execute("DELETE FROM ad_links WHERE slug = ?", (slug,))
    await db.execute("DELETE FROM ad_link_visits WHERE slug = ?", (slug,))
    await database.commit()


async def record_ad_click(user_id: int, slug: str) -> None:
    """Засчитывает переход по рекламной ссылке — только для тех, кто
    ещё не был засчитан ни за одну ad-ссылку (PRIMARY KEY user_id, см.
    комментарий к разделу выше). Ничего не делает, если такой ссылки не
    существует (например, админ успел её удалить) или это повторный
    переход того же игрока — проверка существования и вставка сделаны
    ОДНИМ атомарным запросом (INSERT...SELECT...WHERE EXISTS), а не
    отдельными SELECT+INSERT, чтобы между ними не влезла гонка с
    admin:adlink_delete_confirm (иначе можно было бы создать "висячую"
    запись под уже удалённый slug)."""
    await _ensure_adlinks_schema()
    db = await database.get_db()
    await db.execute(
        "INSERT OR IGNORE INTO ad_link_visits (user_id, slug, started_at, joined) "
        "SELECT ?, ?, ?, 0 WHERE EXISTS (SELECT 1 FROM ad_links WHERE slug = ?)",
        (user_id, slug, time.time(), slug),
    )
    await database.commit()


async def mark_ad_join(user_id: int) -> None:
    """Отмечает, что игрок, пришедший по рекламной ссылке, прошёл
    онбординг (выбрал язык и пол) — вызывается из main.py:process_gender,
    рядом с prof.credit_referral. Идемпотентно (joined = 0 в WHERE)."""
    await _ensure_adlinks_schema()
    db = await database.get_db()
    await db.execute(
        "UPDATE ad_link_visits SET joined = 1, joined_at = ? WHERE user_id = ? AND joined = 0",
        (time.time(), user_id),
    )
    await database.commit()


_bot_username_cache: str | None = None


async def _get_bot_username(bot: Bot) -> str:
    global _bot_username_cache
    if _bot_username_cache is None:
        me = await bot.get_me()
        _bot_username_cache = me.username
    return _bot_username_cache


async def build_ad_link_url(bot: Bot, slug: str) -> str:
    username = await _get_bot_username(bot)
    return f"https://t.me/{username}?start=ad_{slug}"


async def send_with_section_image(
    message: Message, section_key: str, text: str, reply_markup=None
) -> None:
    """Отправляет НОВОЕ сообщение экрана раздела: если для раздела
    задана картинка (см. set_section_image) — одним сообщением-фото с
    текстом в подписи, иначе — обычным текстовым сообщением. У caption
    в Telegram лимит 1024 символа (у обычного текста — 4096), поэтому
    если текст раздела туда не влез — тихо откатываемся на текстовое
    сообщение без картинки, лишь бы не падать с ошибкой на ровном месте.
    Использовать в хендлере ПЕРВОГО открытия раздела (реплай-кнопка) —
    дальнейшая навигация внутри раздела правит уже это же сообщение,
    см. smart_edit()."""
    image = await get_section_image(section_key)
    if image:
        try:
            await message.answer_photo(image, caption=text, reply_markup=reply_markup)
            return
        except TelegramBadRequest as e:
            if "caption is too long" not in str(e).lower():
                raise
    await message.answer(text, reply_markup=reply_markup)


async def smart_edit(message: Message, text: str, reply_markup=None) -> None:
    """Обновляет уже существующее сообщение экрана раздела (переход
    между внутренними экранами — категории/страницы/подменю и т.п.):
    если сообщение — фото (было отправлено с картинкой раздела через
    send_with_section_image) — правит caption, иначе — обычный текст.
    Тип сообщения (текст <-> фото) через edit не меняется — так уже
    отправленное решает, каким способом его обновлять, а не то, задана
    ли картинка в admin.py прямо сейчас (админ мог удалить/сменить её
    уже после того, как это сообщение было отправлено). "message is not
    modified" (правим тем же текстом/разметкой, что уже показаны) —
    молча проглатывается, как и в donate._safe_edit_text."""
    try:
        if message.photo:
            await message.edit_caption(caption=text, reply_markup=reply_markup)
        else:
            await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


# ==========================
#   МИДЛВАРЬ УЧЁТА ПОЛЬЗОВАТЕЛЕЙ
# ==========================

class UserTrackingMiddleware(BaseMiddleware):
    """Записывает user_id/username каждого, кто присылает боту любой
    апдейт (сообщение, callback и т.п.) — единственный способ впоследствии
    искать игрока по @username в админке (см. docstring модуля).

    Подключается в main.py как dp.update.outer_middleware(...), то есть
    видит вообще все апдейты, а не только те, что дошли до конкретных
    роутеров/хендлеров. event_from_user в data кладёт встроенная
    UserContextMiddleware aiogram — она регистрируется автоматически
    внутри Dispatcher ещё до того, как подключается эта мидлварь."""

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user: User | None = data.get("event_from_user")
        if user is not None and not user.is_bot:
            await track_user(user.id, user.username, user.first_name)
        return await handler(event, data)


# ==========================
#   ПОИСК ЦЕЛЕВОГО ИГРОКА
# ==========================

async def resolve_target(raw: str) -> tuple[int, str | None] | None:
    """Разбирает ввод админа — @username, username без @ или числовой
    user_id — и возвращает (user_id, username) либо None, если игрок не
    найден. Для числового ID username может быть None (см. docstring
    модуля — числовой ID работает даже для игрока, которого бот ещё не
    видел; username требует, чтобы игрок хотя бы раз написал боту)."""
    raw = raw.strip()
    if raw.startswith("@"):
        raw = raw[1:]

    if not raw:
        return None

    if raw.isdigit():
        user_id = int(raw)
        row = await get_user_row(user_id)
        return user_id, (row["username"] if row else None)

    row = await find_user_by_username(raw)
    if row is None:
        return None
    return row["user_id"], row["username"]


def _fmt_target(user_id: int, username: str | None) -> str:
    if username:
        return f"@{html.escape(username)} (<code>{user_id}</code>)"
    return f"<code>{user_id}</code>"


def _fmt_dt(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


async def _notify_player(bot: Bot, user_id: int, text: str) -> None:
    """Пытается уведомить игрока о действии админа. Молча проглатывает
    ошибку — игрок мог заблокировать бота, либо (для чисто числового ID,
    которого бот ещё не видел) с ним ещё не открыт чат."""
    try:
        await bot.send_message(user_id, text)
    except Exception:
        pass


# ==========================
#   ТЕКСТЫ
# ==========================
# Админ-панель — инструмент разработчика, а не часть игры для игроков,
# поэтому, в отличие от panda/garden/shop, тексты не локализуются.

TEXT_MAIN_MENU = "🛠 <b>Админ-панель</b>\n<i>Выберите действие:</i>"
TEXT_NOT_ADMIN_CANCEL = "Нечего отменять."
TEXT_CANCELLED = "Отменено."

TEXT_ASK_TARGET = (
    "🔎 <i>Отправьте @username или числовой ID игрока{suffix}.</i>\n"
    "<i>Для отмены — /cancel</i>"
)
TEXT_TARGET_NOT_FOUND = (
    "⚠️ <i>Не нашёл такого игрока — либо он ни разу не писал боту, либо "
    "опечатка. Числовой ID можно ввести и для игрока, которого бот ещё "
    "не видел.</i>\n<i>Попробуйте ещё раз или /cancel</i>"
)

TEXT_ASK_PN_AMOUNT = (
    "💰 Игрок: {target}\n"
    "<i>Отправьте сумму Pn (целое число; отрицательное — чтобы списать).</i>"
)
TEXT_AMOUNT_INVALID = "⚠️ <i>Введите целое число, отличное от нуля.</i>"
TEXT_PN_GIVEN = "✅ {target}: баланс изменён на <b>{amount:+d} Pn</b>. Текущий баланс: <b>{balance} Pn</b>."
TEXT_PLAYER_PN_PLUS = "🎁 Администратор начислил вам {amount} {currency}!"
TEXT_PLAYER_PN_MINUS = "⚠️ Администратор списал {amount} {currency} с вашего баланса."

TEXT_ASK_CRYSTALS_AMOUNT = (
    f"{donate.CRYSTAL_EMOJI} Игрок: {{target}}\n"
    "<i>Отправьте количество кристаллов (целое число; отрицательное — чтобы списать).</i>"
)
TEXT_CRYSTALS_GIVEN = (
    f"✅ {{target}}: баланс изменён на <b>{{amount:+d}} {donate.CRYSTAL_EMOJI} кристаллов</b>. "
    f"Текущий баланс: <b>{{balance}} {donate.CRYSTAL_EMOJI}</b>."
)
TEXT_PLAYER_CRYSTALS_PLUS = f"🎁 Администратор начислил вам {{amount}} {donate.CRYSTAL_EMOJI} кристаллов!"
TEXT_PLAYER_CRYSTALS_MINUS = (
    f"⚠️ Администратор списал {{amount}} {donate.CRYSTAL_EMOJI} кристаллов с вашего баланса."
)

TEXT_CHOOSE_FRUIT = "🍎 Игрок: {target}\n<i>Выберите фрукт:</i>"
TEXT_ASK_FRUIT_QTY = "{emoji} {name} — игрок {target}\n<i>Сколько штук выдать?</i>"
TEXT_QTY_INVALID = "⚠️ <i>Введите целое положительное число.</i>"
TEXT_FRUIT_GIVEN = "✅ {target}: выдано <b>{count}× {emoji} {name}</b>."
TEXT_PLAYER_FRUIT_GIVEN = "🎁 Администратор выдал вам {count}× {emoji} {name}!"

TEXT_CHOOSE_BAKERY = "🥐 Игрок: {target}\n<i>Выберите изделие:</i>"
TEXT_ASK_BAKERY_QTY = "{emoji} {name} — игрок {target}\n<i>Сколько штук выдать?</i>"
TEXT_BAKERY_GIVEN = "✅ {target}: выдано <b>{count}× {emoji} {name}</b> (выпечка)."
TEXT_PLAYER_BAKERY_GIVEN = "🎁 Администратор выдал вам {count}× {emoji} {name}!"

TEXT_LOOKUP_HEADER = "👤 <b>Профиль игрока</b>"

TEXT_ASK_BROADCAST = (
    "📢 <i>Отправьте текст рассылки одним сообщением "
    "(HTML-разметка поддерживается).</i>\n<i>Для отмены — /cancel</i>"
)
TEXT_ASK_BROADCAST_BUTTON = "📢 <i>Добавить к сообщению кнопку со ссылкой?</i>"
TEXT_ASK_BUTTON_TEXT = (
    "🔘 <i>Отправьте текст для кнопки (например: «Подробнее» или «Перейти»).</i>\n"
    "<i>Для отмены — /cancel</i>"
)
TEXT_BUTTON_TEXT_INVALID = "⚠️ <i>Текст кнопки не может быть пустым. Попробуйте ещё раз.</i>"
TEXT_ASK_BUTTON_URL = (
    "🔗 <i>Теперь отправьте ссылку, куда будет вести кнопка "
    "(например, https://t.me/название).</i>\n<i>Для отмены — /cancel</i>"
)
TEXT_BUTTON_URL_INVALID = (
    "⚠️ <i>Это не похоже на ссылку — она должна начинаться с http:// или https://. "
    "Попробуйте ещё раз, или /cancel для отмены.</i>"
)
TEXT_BROADCAST_CONFIRM = (
    "📢 Разослать это сообщение <b>{count}</b> игрокам?\n"
    "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>\n{preview}"
)
TEXT_BROADCAST_CONFIRM_BUTTON_LINE = "\n\n<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>\n🔘 Кнопка: <b>{text}</b> → {url}"
TEXT_BROADCAST_STARTED = "📢 <i>Рассылка запущена…</i>"
TEXT_BROADCAST_DONE = "✅ Рассылка завершена: доставлено <b>{sent}</b>, не удалось <b>{failed}</b>."

TEXT_SKINS_LIST_TITLE = (
    "🎨 <b>Картинки скинов</b>\n"
    "<i>🖼 — картинка загружена, ▫️ — пока нет (используется стикер).\n"
    "Выберите скин, чтобы прикрепить или заменить изображение.</i>"
)
TEXT_SKIN_DETAIL = (
    "🎨 <b>{name}</b>\n"
    "Картинка: {status}\n\n"
    "<i>Загруженное изображение показывается игрокам вместо стикера — "
    "и в карточке скина, и на панде, когда скин надет.</i>"
)
TEXT_SKIN_STATUS_SET = "✅ загружена"
TEXT_SKIN_STATUS_UNSET = "— не задана (используется стикер)"
TEXT_SKIN_ASK_PHOTO = (
    "📤 Пришлите фото для скина «{name}» одним сообщением "
    "(именно как фото, не файлом/документом).\n<i>Для отмены — /cancel</i>"
)
TEXT_SKIN_PHOTO_INVALID = (
    "⚠️ <i>Это не похоже на фото. Пришлите изображение как фото (не документом), "
    "или /cancel для отмены.</i>"
)
TEXT_SKIN_IMAGE_SAVED = "✅ Картинка для скина «{name}» сохранена."
TEXT_SKIN_IMAGE_DELETED = "🗑 Картинка для скина «{name}» удалена — снова показывается стикер."

TEXT_SECTIONS_LIST_TITLE = (
    "🖼 <b>Изображения разделов</b>\n"
    "<i>🖼 — картинка загружена, ▫️ — пока нет.\n"
    "Выберите раздел, чтобы прикрепить или заменить изображение.</i>"
)
TEXT_SECTION_DETAIL = (
    "🖼 <b>{name}</b>\n"
    "Картинка: {status}\n\n"
    "<i>Загруженное изображение показывается игрокам в этом разделе "
    "вместо/вместе с текстом (в зависимости от того, как раздел его "
    "использует).</i>"
)
TEXT_SECTION_STATUS_SET = "✅ загружена"
TEXT_SECTION_STATUS_UNSET = "— не задана"
TEXT_SECTION_ASK_PHOTO = (
    "📤 Пришлите фото для раздела «{name}» одним сообщением "
    "(именно как фото, не файлом/документом).\n<i>Для отмены — /cancel</i>"
)
TEXT_SECTION_PHOTO_INVALID = (
    "⚠️ <i>Это не похоже на фото. Пришлите изображение как фото (не документом), "
    "или /cancel для отмены.</i>"
)
TEXT_SECTION_IMAGE_SAVED = "✅ Картинка для раздела «{name}» сохранена."
TEXT_SECTION_IMAGE_DELETED = "🗑 Картинка для раздела «{name}» удалена."

TEXT_LINKS_LIST_TITLE = (
    "🔗 <b>Ссылки (Профиль → Настройки)</b>\n"
    "<i>🔗 — ссылка задана, ▫️ — пока нет.\n"
    "Выберите пункт, чтобы прикрепить или заменить ссылку.</i>"
)
TEXT_LINK_DETAIL = (
    "🔗 <b>{name}</b>\n"
    "Ссылка: {status}\n\n"
    "<i>Если ссылка задана — в разделе «Профиль → Настройки» у игроков "
    "появляется кнопка «{name}», ведущая по ней. Если не задана — "
    "кнопка не показывается.</i>"
)
TEXT_LINK_STATUS_SET = "✅ {url}"
TEXT_LINK_STATUS_UNSET = "— не задана"
TEXT_LINK_ASK_URL = (
    "📤 Пришлите ссылку для «{name}» одним сообщением "
    "(например, https://t.me/название).\n<i>Для отмены — /cancel</i>"
)
TEXT_LINK_URL_INVALID = (
    "⚠️ <i>Это не похоже на ссылку — она должна начинаться с http:// или https://. "
    "Попробуйте ещё раз, или /cancel для отмены.</i>"
)
TEXT_LINK_SAVED = "✅ Ссылка для «{name}» сохранена."
TEXT_LINK_DELETED = "🗑 Ссылка для «{name}» удалена — кнопка перестанет показываться игрокам."

TEXT_ADLINKS_LIST_TITLE = (
    "📢 <b>Рекламные ссылки</b>\n"
    "<i>Каждая ссылка — отдельная кампания со своей статистикой: сколько "
    "новых игроков перешло и сколько из них реально начали играть "
    "(выбрали язык и пол).</i>"
)
TEXT_ADLINKS_EMPTY = "\n\n<i>Пока ни одной ссылки не создано.</i>"
TEXT_ADLINK_ROW = "{title} — {clicks} перех. / {joined} вступ."
TEXT_ADLINK_ASK_TITLE = (
    "📢 <i>Отправьте название рекламной кампании (например: «Инста, август» "
    "или «Блогер Х») — оно нужно только вам, для статистики.</i>\n"
    "<i>Для отмены — /cancel</i>"
)
TEXT_ADLINK_TITLE_INVALID = "⚠️ <i>Название не может быть пустым. Попробуйте ещё раз.</i>"
TEXT_ADLINK_CREATED = (
    "✅ Ссылка создана!\n\n"
    "🔗 <b>{title}</b>\n<code>{url}</code>\n\n"
    "<i>Отправьте её туда, где крутите рекламу — а здесь потом будет видно, "
    "сколько людей по ней пришло.</i>"
)
TEXT_ADLINK_DETAIL = (
    "📢 <b>{title}</b>\n"
    "🔗 <code>{url}</code>\n\n"
    "👥 Перешло (новых): <b>{clicks}</b>\n"
    "✅ Вступило (прошли онбординг): <b>{joined}</b>\n"
    "📈 Конверсия: <b>{rate}%</b>\n"
    "🗓 Создана: {created}"
)
TEXT_ADLINK_DELETE_CONFIRM = "🗑 Удалить ссылку «{title}» и всю её статистику? Это необратимо."
TEXT_ADLINK_DELETED = "🗑 Ссылка «{title}» удалена."


# ==========================
#   КЛАВИАТУРЫ / ЭКРАНЫ
# ==========================

def _build_main_menu() -> tuple[str, object]:
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 Выдать Pn", callback_data="admin:give_pn", style="primary")
    builder.button(
        text="Выдать кристаллы",
        callback_data="admin:give_crystals",
        style="primary",
        icon_custom_emoji_id=donate.CRYSTAL_EMOJI_ID,
    )
    builder.button(text="🍎 Выдать фрукты", callback_data="admin:give_fruit", style="primary")
    builder.button(text="🥐 Выдать выпечку", callback_data="admin:give_bakery", style="primary")
    builder.button(text="📊 Статистика", callback_data="admin:stats", style="primary")
    builder.button(text="👤 Профиль игрока", callback_data="admin:lookup", style="primary")
    builder.button(text="📢 Рассылка", callback_data="admin:broadcast", style="primary")
    builder.button(text="🖼 Изображения разделов", callback_data="admin:sections", style="primary")
    builder.button(text="🔗 Ссылки (Чат/Новости)", callback_data="admin:links", style="primary")
    builder.button(text="📢 Рекламные ссылки", callback_data="admin:adlinks", style="primary")
    builder.button(text="✖️ Закрыть", callback_data="admin:close", style="primary")
    builder.adjust(2, 2, 1, 1, 1, 1, 1, 1)
    return TEXT_MAIN_MENU, builder.as_markup()


def _back_to_menu_kb() -> object:
    builder = InlineKeyboardBuilder()
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    return builder.as_markup()


def _build_fruit_choice() -> object:
    builder = InlineKeyboardBuilder()
    for cid in garden.CROP_ORDER:
        crop = garden.CROPS[cid]
        builder.button(
            text=f"{crop['emoji']} {crop['name']['ru']}",
            callback_data=f"admin:fruit:{cid}",
            style="primary",
        )
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    builder.adjust(2)
    return builder.as_markup()


def _build_bakery_choice() -> object:
    builder = InlineKeyboardBuilder()
    for rid in bakery.RECIPE_ORDER:
        recipe = bakery.RECIPES[rid]
        builder.button(
            text=f"{recipe['emoji']} {recipe['name']['ru']}",
            callback_data=f"admin:bakery:{rid}",
            style="primary",
        )
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    builder.adjust(2)
    return builder.as_markup()


def _build_skins_list_kb(images: dict[str, str]) -> object:
    builder = InlineKeyboardBuilder()
    for skin_id in panda.SKIN_ORDER:
        skin = panda.SKINS[skin_id]
        icon = "🖼" if skin_id in images else "▫️"
        builder.button(
            text=f"{icon} {skin['name']['ru']}",
            callback_data=f"admin:skin:{skin_id}",
            style="primary",
        )
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    builder.adjust(2)
    return builder.as_markup()


def _build_skin_detail_kb(skin_id: str, has_image: bool) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📤 Загрузить/заменить фото",
        callback_data=f"admin:skin_upload:{skin_id}",
        style="primary",
    )
    if has_image:
        builder.button(
            text="🗑 Удалить фото", callback_data=f"admin:skin_delimg:{skin_id}", style="primary"
        )
    builder.button(text="◀️ К списку скинов", callback_data="admin:skins", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_sections_list_kb(images: dict[str, str]) -> object:
    builder = InlineKeyboardBuilder()
    for section_key in SECTION_ORDER:
        title = SECTION_TITLES[section_key]
        icon = "🖼" if section_key in images else "▫️"
        builder.button(
            text=f"{icon} {title}",
            callback_data=f"admin:section:{section_key}",
            style="primary",
        )
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_section_detail_kb(section_key: str, has_image: bool) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📤 Загрузить/заменить фото",
        callback_data=f"admin:section_upload:{section_key}",
        style="primary",
    )
    if has_image:
        builder.button(
            text="🗑 Удалить фото",
            callback_data=f"admin:section_delimg:{section_key}",
            style="primary",
        )
    builder.button(text="◀️ К списку разделов", callback_data="admin:sections", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_links_list_kb(links: dict[str, str]) -> object:
    builder = InlineKeyboardBuilder()
    for link_key in LINK_ORDER:
        title = LINK_TITLES[link_key]
        icon = "🔗" if link_key in links else "▫️"
        builder.button(
            text=f"{icon} {title}",
            callback_data=f"admin:link:{link_key}",
            style="primary",
        )
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_link_detail_kb(link_key: str, has_link: bool) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="📤 Задать/заменить ссылку",
        callback_data=f"admin:link_upload:{link_key}",
        style="primary",
    )
    if has_link:
        builder.button(
            text="🗑 Удалить ссылку",
            callback_data=f"admin:link_delete:{link_key}",
            style="primary",
        )
    builder.button(text="◀️ К списку ссылок", callback_data="admin:links", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_adlinks_list_kb(links: list[aiosqlite.Row], stats: dict[str, dict]) -> object:
    builder = InlineKeyboardBuilder()
    for row in links:
        s = stats.get(row["slug"], {"clicks": 0, "joined": 0})
        builder.button(
            text=TEXT_ADLINK_ROW.format(title=row["title"], clicks=s["clicks"], joined=s["joined"]),
            callback_data=f"admin:adlink:{row['slug']}",
            style="primary",
        )
    builder.button(text="➕ Новая ссылка", callback_data="admin:adlink_new", style="primary")
    builder.button(text="◀️ В меню", callback_data="admin:menu", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_adlink_detail_kb(slug: str) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🗑 Удалить ссылку", callback_data=f"admin:adlink_delete:{slug}", style="primary"
    )
    builder.button(text="◀️ К списку ссылок", callback_data="admin:adlinks", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _build_adlink_delete_confirm_kb(slug: str) -> object:
    builder = InlineKeyboardBuilder()
    builder.button(
        text="🗑 Да, удалить", callback_data=f"admin:adlink_delete_confirm:{slug}", style="primary"
    )
    builder.button(text="◀️ Отмена", callback_data=f"admin:adlink:{slug}", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _broadcast_button_choice_kb() -> object:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить кнопку", callback_data="admin:broadcast_add_button", style="primary")
    builder.button(text="Без кнопки", callback_data="admin:broadcast_skip_button", style="primary")
    builder.button(text="❌ Отмена", callback_data="admin:broadcast_cancel", style="primary")
    builder.adjust(1)
    return builder.as_markup()


def _broadcast_confirm_kb() -> object:
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Разослать", callback_data="admin:broadcast_confirm", style="primary")
    builder.button(text="❌ Отмена", callback_data="admin:broadcast_cancel", style="primary")
    builder.adjust(2)
    return builder.as_markup()


def _broadcast_message_kb(button_text: str | None, button_url: str | None) -> object | None:
    """Инлайн-клавиатура с кнопкой-ссылкой для рассылаемого сообщения,
    либо None, если админ решил отправить без кнопки (см.
    admin:broadcast_skip_button)."""
    if not button_text or not button_url:
        return None
    builder = InlineKeyboardBuilder()
    builder.button(text=button_text, url=button_url)
    return builder.as_markup()


# Окна "активности" для статистики — (ключ в _gather_stats/_build_stats_text,
# сколько секунд назад считать игрока активным). Порядок здесь = порядок
# строк в _build_stats_text.
ACTIVE_WINDOWS: list[tuple[str, int]] = [
    ("5m", 5 * 60),
    ("10m", 10 * 60),
    ("30m", 30 * 60),
    ("4h", 4 * 3600),
    ("24h", 24 * 3600),
    ("7d", 7 * 86400),
    ("30d", 30 * 86400),
]


async def _gather_stats() -> dict:
    db = await database.get_db()
    now = time.time()

    async def scalar(query: str, params: tuple = ()) -> int:
        async with db.execute(query, params) as cursor:
            row = await cursor.fetchone()
        return (row[0] if row is not None and row[0] is not None else 0)

    # Все окна активности — ОДНИМ запросом (по одному SUM(CASE ...) на
    # каждое окно), а не по отдельному "SELECT COUNT(*) ... WHERE
    # last_seen >= ?" на окно: иначе на большой таблице users это были бы
    # 7 последовательных полных проходов вместо одного.
    active_select = ", ".join(
        f"SUM(CASE WHEN last_seen >= ? THEN 1 ELSE 0 END) AS active_{key}"
        for key, _ in ACTIVE_WINDOWS
    )
    active_params = tuple(now - seconds for _, seconds in ACTIVE_WINDOWS)
    async with db.execute(f"SELECT {active_select} FROM users", active_params) as cursor:
        active_row = await cursor.fetchone()
    active_stats = {
        f"active_{key}": (active_row[f"active_{key}"] or 0) for key, _ in ACTIVE_WINDOWS
    }

    return {
        "total_users": await scalar("SELECT COUNT(*) FROM users"),
        **active_stats,
        "total_pandas": await scalar("SELECT COUNT(*) FROM panda"),
        "planted_plots": await scalar("SELECT COUNT(*) FROM garden_plots WHERE crop_id IS NOT NULL"),
        "basket_fruit_total": await scalar("SELECT COALESCE(SUM(count), 0) FROM garden_inventory"),
        "pantry_bakery_total": await scalar("SELECT COALESCE(SUM(count), 0) FROM bakery_pantry"),
        "total_pn": await scalar("SELECT COALESCE(SUM(balance), 0) FROM shop_balance"),
        "active_listings": await scalar("SELECT COUNT(*) FROM shop_listings"),
        "listed_fruit_volume": await scalar("SELECT COALESCE(SUM(count), 0) FROM shop_listings"),
        "total_bought": await scalar("SELECT COALESCE(SUM(total_bought), 0) FROM shop_stats"),
        "total_sold": await scalar("SELECT COALESCE(SUM(total_sold), 0) FROM shop_stats"),
    }


_ACTIVE_WINDOW_LABELS = {
    "5m": "5 мин",
    "10m": "10 мин",
    "30m": "30 мин",
    "4h": "4 часа",
    "24h": "24 часа",
    "7d": "неделю",
    "30d": "месяц",
}


async def _build_stats_text() -> str:
    s = await _gather_stats()
    active_line = " · ".join(
        f"{_ACTIVE_WINDOW_LABELS[key]}: <b>{s[f'active_{key}']}</b>" for key, _ in ACTIVE_WINDOWS
    )
    lines = [
        "📊 <b>Статистика бота</b>",
        "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        f"👥 Всего игроков: <b>{s['total_users']}</b>",
        "🟢 <b>Активны за:</b>",
        active_line,
        "",
        f"🐼 Заведено панд: <b>{s['total_pandas']}</b>",
        f"🌱 Занято грядок: <b>{s['planted_plots']}</b>",
        f"🧺 Фруктов в корзинах: <b>{s['basket_fruit_total']}</b>",
        f"🥐 Выпечки на витринах: <b>{s['pantry_bakery_total']}</b>",
        "",
        f"💰 {shop.CURRENCY} в обороте: <b>{s['total_pn']}</b>",
        f"📦 Активных лотов: <b>{s['active_listings']}</b> ({s['listed_fruit_volume']} шт. фруктов)",
        f"🔼 Всего куплено: <b>{s['total_bought']}</b> шт. · 🔽 Продано: <b>{s['total_sold']}</b> шт.",
    ]
    return "\n".join(lines)


async def _build_profile_text(user_id: int, username: str | None) -> str:
    user_row = await get_user_row(user_id)
    panda_row = await panda.get_panda_state(user_id)
    balance = await shop.get_balance(user_id)
    stats = await shop.get_stats(user_id)
    inventory = await garden.get_inventory(user_id)
    pantry = await bakery.get_pantry(user_id)

    now = time.time()
    hunger = panda.calc_hunger_percent(
        panda_row["last_fed_at"], now,
        panda_row["hunger_phase1_seconds"], panda_row["hunger_phase2_seconds"],
    )
    age_days = panda.calc_age_days(panda_row["created_at"], now)

    basket_parts = []
    for cid in garden.CROP_ORDER:
        count = inventory.get(cid, 0)
        if count > 0:
            crop = garden.CROPS[cid]
            basket_parts.append(f"{crop['emoji']} {crop['name']['ru']} ×{count}")
    basket_text = ", ".join(basket_parts) if basket_parts else "пусто"

    pantry_parts = []
    for rid in bakery.RECIPE_ORDER:
        count = pantry.get(rid, 0)
        if count > 0:
            recipe = bakery.RECIPES[rid]
            pantry_parts.append(f"{recipe['emoji']} {recipe['name']['ru']} ×{count}")
    pantry_text = ", ".join(pantry_parts) if pantry_parts else "пусто"

    panda_name = html.escape(panda_row["name"]) if panda_row["name"] else "(без имени)"

    lines = [
        TEXT_LOOKUP_HEADER,
        "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        f"ID: <code>{user_id}</code>",
        f"Username: {'@' + html.escape(username) if username else '—'}",
    ]
    if user_row is not None:
        lines.append(f"Впервые замечен: {_fmt_dt(user_row['first_seen'])}")
        lines.append(f"Последняя активность: {_fmt_dt(user_row['last_seen'])}")
    lines += [
        "",
        f"🐼 <b>Панда:</b> {panda_name}",
        f"Возраст: {age_days:.1f} панда-дн.",
        f"Голод: {round(hunger)}% · Настроение: {round(panda_row['mood'])}% · Дружба: {round(panda_row['friendship'])}%",
        "",
        f"💰 <b>Баланс:</b> {balance} {shop.CURRENCY}",
        f"Куплено: {stats['total_bought']} шт. ({stats['total_spent']} {shop.CURRENCY}) · "
        f"Продано: {stats['total_sold']} шт. ({stats['total_earned']} {shop.CURRENCY})",
        "",
        f"🧺 <b>Корзина:</b> {basket_text}",
        f"🥐 <b>Витрина пекарни:</b> {pantry_text}",
    ]
    return "\n".join(lines)


def _is_admin_state(raw_state: str | None) -> bool:
    return bool(raw_state) and raw_state.startswith("AdminStates:")


# ==========================
#   ХЕНДЛЕРЫ — ВХОД / НАВИГАЦИЯ
# ==========================

@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext) -> None:
    await state.set_state(None)
    text, markup = _build_main_menu()
    await message.answer(text, reply_markup=markup)


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    current = await state.get_state()
    if not _is_admin_state(current):
        await message.answer(TEXT_NOT_ADMIN_CANCEL)
        return
    await state.set_state(None)
    await message.answer(TEXT_CANCELLED)


@router.callback_query(F.data == "admin:menu")
async def on_back_to_menu(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "admin:close")
async def on_close(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await callback.answer()
    try:
        await callback.message.delete()
    except Exception:
        await callback.message.edit_text("Админ-панель закрыта.", reply_markup=None)


# ==========================
#   ХЕНДЛЕРЫ — ВЫДАТЬ Pn
# ==========================

@router.callback_query(F.data == "admin:give_pn")
async def on_give_pn_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.give_pn_target)
    await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — кому выдать Pn"))
    await callback.answer()


@router.message(StateFilter(AdminStates.give_pn_target))
async def on_give_pn_target(message: Message, state: FSMContext) -> None:
    target = await resolve_target((message.text or ""))
    if target is None:
        await message.answer(TEXT_TARGET_NOT_FOUND)
        return

    user_id, username = target
    await state.update_data(admin_target_id=user_id, admin_target_username=username)
    await state.set_state(AdminStates.give_pn_amount)
    await message.answer(TEXT_ASK_PN_AMOUNT.format(target=_fmt_target(user_id, username)))


@router.message(StateFilter(AdminStates.give_pn_amount))
async def on_give_pn_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    try:
        amount = int(raw)
    except ValueError:
        await message.answer(TEXT_AMOUNT_INVALID)
        return
    if amount == 0:
        await message.answer(TEXT_AMOUNT_INVALID)
        return

    data = await state.get_data()
    user_id = data["admin_target_id"]
    username = data.get("admin_target_username")

    new_balance = await shop.add_balance(user_id, amount)
    await state.set_state(None)

    await message.answer(
        TEXT_PN_GIVEN.format(target=_fmt_target(user_id, username), amount=amount, balance=new_balance)
    )

    notify = TEXT_PLAYER_PN_PLUS if amount > 0 else TEXT_PLAYER_PN_MINUS
    await _notify_player(bot, user_id, notify.format(amount=abs(amount), currency=shop.CURRENCY))


# ==========================
#   ХЕНДЛЕРЫ — ВЫДАТЬ КРИСТАЛЛЫ
# ==========================
# Полностью зеркалит "Выдать Pn" выше, только начисление идёт через
# prof.add_crystals/prof.get_crystals — это ЕДИНЫЙ баланс премиальной
# валюты (та же самая, что продаётся за Stars в donate.py и тратится в
# prof.py на подарки), счётчик и защита от гонок уже реализованы там,
# здесь просто переиспользуем. Отрицательная сумма списывает кристаллы —
# по аналогии с Pn.

@router.callback_query(F.data == "admin:give_crystals")
async def on_give_crystals_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.give_crystals_target)
    await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — кому выдать кристаллы"))
    await callback.answer()


@router.message(StateFilter(AdminStates.give_crystals_target))
async def on_give_crystals_target(message: Message, state: FSMContext) -> None:
    target = await resolve_target((message.text or ""))
    if target is None:
        await message.answer(TEXT_TARGET_NOT_FOUND)
        return

    user_id, username = target
    await state.update_data(admin_target_id=user_id, admin_target_username=username)
    await state.set_state(AdminStates.give_crystals_amount)
    await message.answer(TEXT_ASK_CRYSTALS_AMOUNT.format(target=_fmt_target(user_id, username)))


@router.message(StateFilter(AdminStates.give_crystals_amount))
async def on_give_crystals_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    try:
        amount = int(raw)
    except ValueError:
        await message.answer(TEXT_AMOUNT_INVALID)
        return
    if amount == 0:
        await message.answer(TEXT_AMOUNT_INVALID)
        return

    data = await state.get_data()
    user_id = data["admin_target_id"]
    username = data.get("admin_target_username")

    new_balance = await prof.add_crystals(user_id, amount)
    await state.set_state(None)

    await message.answer(
        TEXT_CRYSTALS_GIVEN.format(
            target=_fmt_target(user_id, username), amount=amount, balance=new_balance
        )
    )

    notify = TEXT_PLAYER_CRYSTALS_PLUS if amount > 0 else TEXT_PLAYER_CRYSTALS_MINUS
    await _notify_player(bot, user_id, notify.format(amount=abs(amount)))


# ==========================
#   ХЕНДЛЕРЫ — ВЫДАТЬ ФРУКТЫ
# ==========================

@router.callback_query(F.data == "admin:give_fruit")
async def on_give_fruit_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.give_fruit_target)
    await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — кому выдать фрукты"))
    await callback.answer()


@router.message(StateFilter(AdminStates.give_fruit_target))
async def on_give_fruit_target(message: Message, state: FSMContext) -> None:
    target = await resolve_target((message.text or ""))
    if target is None:
        await message.answer(TEXT_TARGET_NOT_FOUND)
        return

    user_id, username = target
    await state.update_data(admin_target_id=user_id, admin_target_username=username)
    await state.set_state(None)
    await message.answer(
        TEXT_CHOOSE_FRUIT.format(target=_fmt_target(user_id, username)),
        reply_markup=_build_fruit_choice(),
    )


@router.callback_query(F.data.startswith("admin:fruit:"))
async def on_fruit_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    crop_id = callback.data.split(":")[2]
    data = await state.get_data()
    user_id = data.get("admin_target_id")
    username = data.get("admin_target_username")

    if user_id is None:
        # состояние потеряно (например, бот перезапускался) — начинаем заново
        await callback.answer()
        await state.set_state(AdminStates.give_fruit_target)
        await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — кому выдать фрукты"))
        return

    crop = garden.CROPS[crop_id]
    await state.update_data(admin_fruit_crop=crop_id)
    await state.set_state(AdminStates.give_fruit_amount)
    await callback.answer()
    await callback.message.edit_text(
        TEXT_ASK_FRUIT_QTY.format(
            emoji=crop["emoji"], name=crop["name"]["ru"], target=_fmt_target(user_id, username)
        )
    )


@router.message(StateFilter(AdminStates.give_fruit_amount))
async def on_give_fruit_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(TEXT_QTY_INVALID)
        return

    qty = int(raw)
    data = await state.get_data()
    user_id = data["admin_target_id"]
    username = data.get("admin_target_username")
    crop_id = data["admin_fruit_crop"]
    crop = garden.CROPS[crop_id]

    await garden.add_to_basket(user_id, crop_id, qty)
    await state.set_state(None)

    await message.answer(
        TEXT_FRUIT_GIVEN.format(
            target=_fmt_target(user_id, username), count=qty, emoji=crop["emoji"], name=crop["name"]["ru"]
        )
    )
    await _notify_player(
        bot, user_id,
        TEXT_PLAYER_FRUIT_GIVEN.format(count=qty, emoji=crop["emoji"], name=crop["name"]["ru"]),
    )


# ==========================
#   ХЕНДЛЕРЫ — ВЫДАТЬ ВЫПЕЧКУ
# ==========================

@router.callback_query(F.data == "admin:give_bakery")
async def on_give_bakery_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.give_bakery_target)
    await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — кому выдать выпечку"))
    await callback.answer()


@router.message(StateFilter(AdminStates.give_bakery_target))
async def on_give_bakery_target(message: Message, state: FSMContext) -> None:
    target = await resolve_target((message.text or ""))
    if target is None:
        await message.answer(TEXT_TARGET_NOT_FOUND)
        return

    user_id, username = target
    await state.update_data(admin_target_id=user_id, admin_target_username=username)
    await state.set_state(None)
    await message.answer(
        TEXT_CHOOSE_BAKERY.format(target=_fmt_target(user_id, username)),
        reply_markup=_build_bakery_choice(),
    )


@router.callback_query(F.data.startswith("admin:bakery:"))
async def on_bakery_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    recipe_id = callback.data.split(":")[2]
    data = await state.get_data()
    user_id = data.get("admin_target_id")
    username = data.get("admin_target_username")

    if user_id is None:
        # состояние потеряно (например, бот перезапускался) — начинаем заново
        await callback.answer()
        await state.set_state(AdminStates.give_bakery_target)
        await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — кому выдать выпечку"))
        return

    recipe = bakery.RECIPES[recipe_id]
    await state.update_data(admin_bakery_recipe=recipe_id)
    await state.set_state(AdminStates.give_bakery_amount)
    await callback.answer()
    await callback.message.edit_text(
        TEXT_ASK_BAKERY_QTY.format(
            emoji=recipe["emoji"], name=recipe["name"]["ru"], target=_fmt_target(user_id, username)
        )
    )


@router.message(StateFilter(AdminStates.give_bakery_amount))
async def on_give_bakery_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await message.answer(TEXT_QTY_INVALID)
        return

    qty = int(raw)
    data = await state.get_data()
    user_id = data["admin_target_id"]
    username = data.get("admin_target_username")
    recipe_id = data["admin_bakery_recipe"]
    recipe = bakery.RECIPES[recipe_id]

    await bakery.add_to_pantry(user_id, recipe_id, qty)
    await state.set_state(None)

    await message.answer(
        TEXT_BAKERY_GIVEN.format(
            target=_fmt_target(user_id, username), count=qty, emoji=recipe["emoji"], name=recipe["name"]["ru"]
        )
    )
    await _notify_player(
        bot, user_id,
        TEXT_PLAYER_BAKERY_GIVEN.format(count=qty, emoji=recipe["emoji"], name=recipe["name"]["ru"]),
    )


# ==========================
#   ХЕНДЛЕРЫ — СТАТИСТИКА
# ==========================

@router.callback_query(F.data == "admin:stats")
async def on_stats(callback: CallbackQuery, state: FSMContext) -> None:
    text = await _build_stats_text()
    await callback.message.edit_text(text, reply_markup=_back_to_menu_kb())
    await callback.answer()


# ==========================
#   ХЕНДЛЕРЫ — ПРОФИЛЬ ИГРОКА
# ==========================

@router.callback_query(F.data == "admin:lookup")
async def on_lookup_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.lookup_target)
    await callback.message.edit_text(TEXT_ASK_TARGET.format(suffix=" — чей профиль показать"))
    await callback.answer()


@router.message(StateFilter(AdminStates.lookup_target))
async def on_lookup_target(message: Message, state: FSMContext) -> None:
    target = await resolve_target((message.text or ""))
    if target is None:
        await message.answer(TEXT_TARGET_NOT_FOUND)
        return

    user_id, username = target
    await state.set_state(None)
    text = await _build_profile_text(user_id, username)
    await message.answer(text, reply_markup=_back_to_menu_kb())


# ==========================
#   ХЕНДЛЕРЫ — РАССЫЛКА
# ==========================

@router.callback_query(F.data == "admin:broadcast")
async def on_broadcast_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast_text)
    await callback.message.edit_text(TEXT_ASK_BROADCAST)
    await callback.answer()


@router.message(StateFilter(AdminStates.broadcast_text))
async def on_broadcast_text(message: Message, state: FSMContext) -> None:
    text = message.html_text
    if not text:
        await message.answer(TEXT_QTY_INVALID)
        return

    await state.update_data(admin_broadcast_text=text)
    await state.set_state(AdminStates.broadcast_button_choice)
    await message.answer(TEXT_ASK_BROADCAST_BUTTON, reply_markup=_broadcast_button_choice_kb())


@router.callback_query(
    F.data == "admin:broadcast_add_button", StateFilter(AdminStates.broadcast_button_choice)
)
async def on_broadcast_add_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.broadcast_button_text)
    await callback.message.edit_text(TEXT_ASK_BUTTON_TEXT)
    await callback.answer()


@router.message(StateFilter(AdminStates.broadcast_button_text))
async def on_broadcast_button_text(message: Message, state: FSMContext) -> None:
    button_text = (message.text or "").strip()
    if not button_text:
        await message.answer(TEXT_BUTTON_TEXT_INVALID)
        return

    await state.update_data(admin_broadcast_button_text=button_text)
    await state.set_state(AdminStates.broadcast_button_url)
    await message.answer(TEXT_ASK_BUTTON_URL)


@router.message(StateFilter(AdminStates.broadcast_button_url))
async def on_broadcast_button_url(message: Message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(TEXT_BUTTON_URL_INVALID)
        return

    await state.update_data(admin_broadcast_button_url=url)
    await _show_broadcast_confirm(message, state)


@router.callback_query(
    F.data == "admin:broadcast_skip_button", StateFilter(AdminStates.broadcast_button_choice)
)
async def on_broadcast_skip_button(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(admin_broadcast_button_text=None, admin_broadcast_button_url=None)
    await callback.answer()
    await _show_broadcast_confirm(callback.message, state, edit=True)


async def _show_broadcast_confirm(target: Message, state: FSMContext, edit: bool = False) -> None:
    """Показывает экран подтверждения рассылки — общий для обеих веток
    (с кнопкой и без). target — сообщение, в которое отвечаем (edit=True,
    если нужно поправить его же, а не отправить новое, — см.
    on_broadcast_skip_button, где это callback.message)."""
    data = await state.get_data()
    text = data.get("admin_broadcast_text", "")
    button_text = data.get("admin_broadcast_button_text")
    button_url = data.get("admin_broadcast_button_url")

    await state.set_state(AdminStates.broadcast_confirm)

    count = len(await get_all_user_ids())
    preview = text
    if button_text and button_url:
        preview += TEXT_BROADCAST_CONFIRM_BUTTON_LINE.format(
            text=html.escape(button_text), url=html.escape(button_url)
        )
    confirm_text = TEXT_BROADCAST_CONFIRM.format(count=count, preview=preview)

    if edit:
        await target.edit_text(confirm_text, reply_markup=_broadcast_confirm_kb())
    else:
        await target.answer(confirm_text, reply_markup=_broadcast_confirm_kb())


@router.callback_query(F.data == "admin:broadcast_cancel", StateFilter(AdminStates.broadcast_confirm))
async def on_broadcast_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await callback.answer()
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:broadcast_cancel", StateFilter(AdminStates.broadcast_button_choice))
async def on_broadcast_cancel_at_button_choice(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    await callback.answer()
    text, markup = _build_main_menu()
    await callback.message.edit_text(text, reply_markup=markup)


@router.callback_query(F.data == "admin:broadcast_confirm", StateFilter(AdminStates.broadcast_confirm))
async def on_broadcast_confirm(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    data = await state.get_data()
    text = data.get("admin_broadcast_text")
    button_text = data.get("admin_broadcast_button_text")
    button_url = data.get("admin_broadcast_button_url")
    await state.set_state(None)
    await callback.answer()

    if not text:
        await callback.message.edit_text(TEXT_CANCELLED)
        return

    await callback.message.edit_text(TEXT_BROADCAST_STARTED)

    reply_markup = _broadcast_message_kb(button_text, button_url)
    user_ids = await get_all_user_ids()
    sent = 0
    failed = 0
    for user_id in user_ids:
        try:
            await bot.send_message(user_id, text, reply_markup=reply_markup)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(BROADCAST_DELAY_SECONDS)

    await callback.message.answer(TEXT_BROADCAST_DONE.format(sent=sent, failed=failed))


# ==========================
#   ХЕНДЛЕРЫ — КАРТИНКИ СКИНОВ
# ==========================
# Позволяет прикрепить для каждого скина панды (panda.py: SKINS) своё
# изображение — оно показывается игрокам вместо стикера (panda.py:
# get_skin_image / _send_panda_media / _send_skin_detail).

@router.callback_query(F.data == "admin:skins")
async def on_skins_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    images = await panda.get_skin_images()
    await callback.message.edit_text(TEXT_SKINS_LIST_TITLE, reply_markup=_build_skins_list_kb(images))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:skin:"))
async def on_skin_detail(callback: CallbackQuery, state: FSMContext) -> None:
    skin_id = callback.data.split(":", 2)[2]
    skin = panda.SKINS.get(skin_id)
    if skin is None:
        await callback.answer()
        return

    await state.set_state(None)
    image = await panda.get_skin_image(skin_id)
    text = TEXT_SKIN_DETAIL.format(
        name=html.escape(skin["name"]["ru"]),
        status=TEXT_SKIN_STATUS_SET if image else TEXT_SKIN_STATUS_UNSET,
    )
    kb = _build_skin_detail_kb(skin_id, bool(image))

    if image:
        # Показываем текущую картинку новым сообщением, отдельно от
        # редактируемой карточки со статусом/кнопками.
        await callback.message.answer_photo(image)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:skin_upload:"))
async def on_skin_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    skin_id = callback.data.split(":", 2)[2]
    skin = panda.SKINS.get(skin_id)
    if skin is None:
        await callback.answer()
        return

    await state.set_state(AdminStates.skin_upload_photo)
    await state.update_data(admin_skin_id=skin_id)
    await callback.message.edit_text(TEXT_SKIN_ASK_PHOTO.format(name=html.escape(skin["name"]["ru"])))
    await callback.answer()


@router.message(StateFilter(AdminStates.skin_upload_photo))
async def on_skin_upload_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    skin_id = data.get("admin_skin_id")
    skin = panda.SKINS.get(skin_id) if skin_id else None
    if skin is None:
        # состояние потеряно (например, бот перезапускался)
        await state.set_state(None)
        await message.answer(TEXT_CANCELLED)
        return

    if not message.photo:
        await message.answer(TEXT_SKIN_PHOTO_INVALID)
        return

    file_id = message.photo[-1].file_id
    await panda.set_skin_image(skin_id, file_id)
    await state.set_state(None)

    await message.answer(TEXT_SKIN_IMAGE_SAVED.format(name=html.escape(skin["name"]["ru"])))
    text = TEXT_SKIN_DETAIL.format(name=html.escape(skin["name"]["ru"]), status=TEXT_SKIN_STATUS_SET)
    await message.answer(text, reply_markup=_build_skin_detail_kb(skin_id, True))


@router.callback_query(F.data.startswith("admin:skin_delimg:"))
async def on_skin_delete_image(callback: CallbackQuery, state: FSMContext) -> None:
    skin_id = callback.data.split(":", 2)[2]
    skin = panda.SKINS.get(skin_id)
    if skin is None:
        await callback.answer()
        return

    await panda.clear_skin_image(skin_id)
    await callback.answer(TEXT_SKIN_IMAGE_DELETED.format(name=html.escape(skin["name"]["ru"])))

    text = TEXT_SKIN_DETAIL.format(name=html.escape(skin["name"]["ru"]), status=TEXT_SKIN_STATUS_UNSET)
    await callback.message.edit_text(text, reply_markup=_build_skin_detail_kb(skin_id, False))


# ==========================
#   ХЕНДЛЕРЫ — КАРТИНКИ РАЗДЕЛОВ
# ==========================
# Позволяет прикрепить по одной картинке на раздел ("Достижения",
# "Сад", "Пекарня", "Профиль", "Донаты") и на приветствие /start —
# см. get_section_image/set_section_image/clear_section_image выше.
# Сами разделы (achives.py/garden.py/bakery.py/prof.py/donate.py)
# должны вызывать admin.get_section_image("<ключ>") при построении
# своего экрана и, если картинка задана, отправлять её (например,
# через answer_photo с текстом в caption) вместо обычного текстового
# сообщения — по аналогии с тем, как main.py уже делает это для
# раздела "start" (см. cmd_start ниже, в main.py).

@router.callback_query(F.data == "admin:sections")
async def on_sections_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    images = await get_section_images()
    await callback.message.edit_text(TEXT_SECTIONS_LIST_TITLE, reply_markup=_build_sections_list_kb(images))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:section:"))
async def on_section_detail(callback: CallbackQuery, state: FSMContext) -> None:
    section_key = callback.data.split(":", 2)[2]
    title = SECTION_TITLES.get(section_key)
    if title is None:
        await callback.answer()
        return

    await state.set_state(None)
    image = await get_section_image(section_key)
    text = TEXT_SECTION_DETAIL.format(
        name=html.escape(title),
        status=TEXT_SECTION_STATUS_SET if image else TEXT_SECTION_STATUS_UNSET,
    )
    kb = _build_section_detail_kb(section_key, bool(image))

    if image:
        # Показываем текущую картинку новым сообщением, отдельно от
        # редактируемой карточки со статусом/кнопками.
        await callback.message.answer_photo(image)
    await callback.message.edit_text(text, reply_markup=kb)
    await callback.answer()


@router.callback_query(F.data.startswith("admin:section_upload:"))
async def on_section_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    section_key = callback.data.split(":", 2)[2]
    title = SECTION_TITLES.get(section_key)
    if title is None:
        await callback.answer()
        return

    await state.set_state(AdminStates.section_upload_photo)
    await state.update_data(admin_section_key=section_key)
    await callback.message.edit_text(TEXT_SECTION_ASK_PHOTO.format(name=html.escape(title)))
    await callback.answer()


@router.message(StateFilter(AdminStates.section_upload_photo))
async def on_section_upload_photo(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    section_key = data.get("admin_section_key")
    title = SECTION_TITLES.get(section_key) if section_key else None
    if title is None:
        # состояние потеряно (например, бот перезапускался)
        await state.set_state(None)
        await message.answer(TEXT_CANCELLED)
        return

    if not message.photo:
        await message.answer(TEXT_SECTION_PHOTO_INVALID)
        return

    file_id = message.photo[-1].file_id
    await set_section_image(section_key, file_id)
    await state.set_state(None)

    await message.answer(TEXT_SECTION_IMAGE_SAVED.format(name=html.escape(title)))
    text = TEXT_SECTION_DETAIL.format(name=html.escape(title), status=TEXT_SECTION_STATUS_SET)
    await message.answer(text, reply_markup=_build_section_detail_kb(section_key, True))


@router.callback_query(F.data.startswith("admin:section_delimg:"))
async def on_section_delete_image(callback: CallbackQuery, state: FSMContext) -> None:
    section_key = callback.data.split(":", 2)[2]
    title = SECTION_TITLES.get(section_key)
    if title is None:
        await callback.answer()
        return

    await clear_section_image(section_key)
    await callback.answer(TEXT_SECTION_IMAGE_DELETED.format(name=html.escape(title)))

    text = TEXT_SECTION_DETAIL.format(name=html.escape(title), status=TEXT_SECTION_STATUS_UNSET)
    await callback.message.edit_text(text, reply_markup=_build_section_detail_kb(section_key, False))


# ==========================
#   ХЕНДЛЕРЫ — ССЫЛКИ (Новости / Наш чат)
# ==========================
# Полностью зеркалит "Изображения разделов" выше, только вместо
# фото/file_id сохраняется URL (get_link/set_link/clear_link). Кнопки
# по этим ссылкам показываются игрокам в prof.py: раздел
# "Профиль -> Настройки" (_settings_keyboard) — только если ссылка
# задана, иначе кнопка просто не рисуется.

@router.callback_query(F.data == "admin:links")
async def on_links_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    links = await get_links()
    await callback.message.edit_text(TEXT_LINKS_LIST_TITLE, reply_markup=_build_links_list_kb(links))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:link:"))
async def on_link_detail(callback: CallbackQuery, state: FSMContext) -> None:
    link_key = callback.data.split(":", 2)[2]
    title = LINK_TITLES.get(link_key)
    if title is None:
        await callback.answer()
        return

    await state.set_state(None)
    url = await get_link(link_key)
    text = TEXT_LINK_DETAIL.format(
        name=html.escape(title),
        status=TEXT_LINK_STATUS_SET.format(url=html.escape(url)) if url else TEXT_LINK_STATUS_UNSET,
    )
    await callback.message.edit_text(text, reply_markup=_build_link_detail_kb(link_key, bool(url)))
    await callback.answer()


@router.callback_query(F.data.startswith("admin:link_upload:"))
async def on_link_upload_start(callback: CallbackQuery, state: FSMContext) -> None:
    link_key = callback.data.split(":", 2)[2]
    title = LINK_TITLES.get(link_key)
    if title is None:
        await callback.answer()
        return

    await state.set_state(AdminStates.link_upload_url)
    await state.update_data(admin_link_key=link_key)
    await callback.message.edit_text(TEXT_LINK_ASK_URL.format(name=html.escape(title)))
    await callback.answer()


@router.message(StateFilter(AdminStates.link_upload_url))
async def on_link_upload_url(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    link_key = data.get("admin_link_key")
    title = LINK_TITLES.get(link_key) if link_key else None
    if title is None:
        # состояние потеряно (например, бот перезапускался)
        await state.set_state(None)
        await message.answer(TEXT_CANCELLED)
        return

    url = (message.text or "").strip()
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(TEXT_LINK_URL_INVALID)
        return

    await set_link(link_key, url)
    await state.set_state(None)

    await message.answer(TEXT_LINK_SAVED.format(name=html.escape(title)))
    text = TEXT_LINK_DETAIL.format(
        name=html.escape(title), status=TEXT_LINK_STATUS_SET.format(url=html.escape(url))
    )
    await message.answer(text, reply_markup=_build_link_detail_kb(link_key, True))


@router.callback_query(F.data.startswith("admin:link_delete:"))
async def on_link_delete(callback: CallbackQuery, state: FSMContext) -> None:
    link_key = callback.data.split(":", 2)[2]
    title = LINK_TITLES.get(link_key)
    if title is None:
        await callback.answer()
        return

    await clear_link(link_key)
    await callback.answer(TEXT_LINK_DELETED.format(name=html.escape(title)))

    text = TEXT_LINK_DETAIL.format(name=html.escape(title), status=TEXT_LINK_STATUS_UNSET)
    await callback.message.edit_text(text, reply_markup=_build_link_detail_kb(link_key, False))


# ==========================
#   ХЕНДЛЕРЫ — РЕКЛАМНЫЕ ССЫЛКИ
# ==========================
# Создание/просмотр/удаление ad-ссылок и их статистики — см. описание
# механики в комментарии у раздела "РЕКЛАМНЫЕ ССЫЛКИ" выше (переход
# засчитывается в main.py:cmd_start, вступление — в
# main.py:process_gender рядом с prof.credit_referral).

@router.callback_query(F.data == "admin:adlinks")
async def on_adlinks_list(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(None)
    links = await get_ad_links()
    stats = await get_all_ad_link_stats()
    text = TEXT_ADLINKS_LIST_TITLE + (TEXT_ADLINKS_EMPTY if not links else "")
    await callback.message.edit_text(text, reply_markup=_build_adlinks_list_kb(links, stats))
    await callback.answer()


@router.callback_query(F.data == "admin:adlink_new")
async def on_adlink_new_start(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AdminStates.adlink_title)
    await callback.message.edit_text(TEXT_ADLINK_ASK_TITLE)
    await callback.answer()


@router.message(StateFilter(AdminStates.adlink_title))
async def on_adlink_new_title(message: Message, state: FSMContext, bot: Bot) -> None:
    title = (message.text or "").strip()
    if not title:
        await message.answer(TEXT_ADLINK_TITLE_INVALID)
        return

    slug = await create_ad_link(title)
    url = await build_ad_link_url(bot, slug)
    await state.set_state(None)

    await message.answer(TEXT_ADLINK_CREATED.format(title=html.escape(title), url=url))
    links = await get_ad_links()
    stats = await get_all_ad_link_stats()
    await message.answer(TEXT_ADLINKS_LIST_TITLE, reply_markup=_build_adlinks_list_kb(links, stats))


@router.callback_query(F.data.startswith("admin:adlink_delete_confirm:"))
async def on_adlink_delete_confirm(callback: CallbackQuery, state: FSMContext) -> None:
    slug = callback.data.split(":", 2)[2]
    link = await get_ad_link(slug)
    title = link["title"] if link else slug

    await delete_ad_link(slug)
    await callback.answer(TEXT_ADLINK_DELETED.format(title=title), show_alert=True)

    links = await get_ad_links()
    stats = await get_all_ad_link_stats()
    text = TEXT_ADLINKS_LIST_TITLE + (TEXT_ADLINKS_EMPTY if not links else "")
    await callback.message.edit_text(text, reply_markup=_build_adlinks_list_kb(links, stats))


@router.callback_query(F.data.startswith("admin:adlink_delete:"))
async def on_adlink_delete_ask(callback: CallbackQuery, state: FSMContext) -> None:
    slug = callback.data.split(":", 2)[2]
    link = await get_ad_link(slug)
    if link is None:
        await callback.answer()
        return

    await callback.message.edit_text(
        TEXT_ADLINK_DELETE_CONFIRM.format(title=html.escape(link["title"])),
        reply_markup=_build_adlink_delete_confirm_kb(slug),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("admin:adlink:"))
async def on_adlink_detail(callback: CallbackQuery, state: FSMContext, bot: Bot) -> None:
    slug = callback.data.split(":", 2)[2]
    link = await get_ad_link(slug)
    if link is None:
        await callback.answer()
        return

    await state.set_state(None)
    url = await build_ad_link_url(bot, slug)
    stats = await get_ad_link_stats(slug)
    rate = round(stats["joined"] / stats["clicks"] * 100) if stats["clicks"] else 0
    text = TEXT_ADLINK_DETAIL.format(
        title=html.escape(link["title"]),
        url=url,
        clicks=stats["clicks"],
        joined=stats["joined"],
        rate=rate,
        created=_fmt_dt(link["created_at"]),
    )
    await callback.message.edit_text(text, reply_markup=_build_adlink_detail_kb(slug))
    await callback.answer()
