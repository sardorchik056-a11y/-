"""
Общая база данных бота.

Раньше каждый модуль (panda / garden / shop) открывал свой собственный
файл SQLite (panda.db, garden.db, shop.db) и коммитил (fsync на диск)
буквально после каждой мелкой операции. Это лишние файловые хендлы и
лишние синхронные сбросы на диск при каждом чихе.

Здесь — один файл БД (bot.db) и одно переиспользуемое asyncio-соединение
на весь бот. Все таблицы создаются тут же, при первом обращении.

Батчинг записи ("стопками"):
    Вместо немедленного db.commit() после каждой операции, запись
    помечается как "грязная" (dirty) вызовом commit(), а реальный
    commit() в SQLite происходит НЕ поштучно, а пачкой — раз в
    FLUSH_INTERVAL секунд фоновой задачей, либо сразу, если накопилось
    BATCH_MAX_PENDING несохранённых операций (защита от переполнения
    WAL, если вдруг разом прилетело много записей).

    Это даёт то же самое поведение для читающего кода (в пределах
    одного соединения незакоммиченные изменения всё равно видны сразу
    следующим SELECT'ом), но сильно снижает число реальных fsync'ов —
    меньше нагрузки на диск и меньше пиков потребления памяти на
    журнал WAL при частых записях, чем при коммите "по одному".

Использование в других модулях:
    import database

    db = await database.get_db()
    await db.execute("UPDATE ... WHERE ...", (...,))
    await database.commit()      # поставить в очередь на сохранение
    # если нужен гарантированный commit прямо сейчас:
    await database.flush()

Защита от гонок/дюпов (двойной тап, повтор запроса и т.п.):
    async with database.user_lock(user_id):
        ...прочитать состояние, посчитать, записать...
    См. подробности у user_lock() ниже.

Запуск/остановка бота (main.py):
    await database.init_db()     # один раз при старте
    ...
    await database.close_db()    # один раз при остановке (сбросит очередь)
"""

import asyncio
import logging
import weakref

import aiosqlite

logger = logging.getLogger(__name__)

DB_PATH = "bot.db"

# --- настройки батчинга коммитов ---
FLUSH_INTERVAL = 1.0        # сек между плановыми сбросами "стопки" на диск
BATCH_MAX_PENDING = 200     # принудительный сброс, если накопилось столько операций


# ==========================
#   СХЕМЫ ТАБЛИЦ (все модули — в одной базе)
# ==========================

# --- panda.py ---
PANDA_COLUMNS = {
    "created_at": "REAL NOT NULL",
    "last_fed_at": "REAL NOT NULL",
    "mood": "REAL NOT NULL DEFAULT 100",
    "friendship": "REAL NOT NULL DEFAULT 0",
    "mood_ticks_applied": "INTEGER NOT NULL DEFAULT 0",
    "friend_ticks_applied": "INTEGER NOT NULL DEFAULT 0",
    "pet_window_start": "REAL NOT NULL DEFAULT 0",
    "pet_count": "INTEGER NOT NULL DEFAULT 0",
    "name": "TEXT",
    # Сколько раз имя уже было ПЛАТНО изменено (первая установка имени
    # бесплатна и в счётчик не идёт) — используется panda.py для расчёта
    # цены следующего переименования: RENAME_BASE_COST * 2**name_changes.
    "name_changes": "INTEGER NOT NULL DEFAULT 0",
    # Длительности (в реальных секундах) двух фаз голода текущего
    # "цикла кормления" — 100%->50% и 50%->0%. Каждый раз, когда панду
    # кормят полностью (feed_panda), эти значения перебрасываются
    # заново случайно в пределах HUNGER_PHASE1_/PHASE2_*_SECONDS.
    "hunger_phase1_seconds": "REAL NOT NULL DEFAULT 2400",
    "hunger_phase2_seconds": "REAL NOT NULL DEFAULT 10800",
    # Скины (см. panda.py: SKINS) — id купленного и надетого прямо сейчас
    # скина, либо NULL, если используется обычный вид панды по возрасту
    # (PANDA_STICKER_ID / PANDA_STICKER_ID_ADULT). Сам факт покупки
    # скина хранится отдельно, в таблице panda_skins ниже.
    "equipped_skin_id": "TEXT",
    # Уровень панды (1-25, см. panda.py: PANDA_LEVEL_MAX). Влияет на
    # длительность обеих фаз голода (hunger_duration_multiplier) — чем
    # выше уровень, тем дольше панда не голодает (вплоть до +300% на 25).
    # Прокачивается ВРУЧНУЮ игроком (см. panda.py: level_up_panda) —
    # не автоматически.
    "level": "INTEGER NOT NULL DEFAULT 1",
    # Сколько чудесного бамбука суммарно потрачено на прокачку уровня
    # за всё время (растёт только через level_up_panda). Отдельно от
    # текущего инвентаря ниже — это просто исторический счётчик,
    # никогда не уменьшается.
    "wonder_bamboo_fed": "INTEGER NOT NULL DEFAULT 0",
    # Сколько ЧУДЕСНОГО бамбука сейчас в инвентаре (ещё не потрачено на
    # уровень). Это отдельный, особый предмет — НЕ обычный бамбук из
    # сада (garden.py), тот этой системой никак не затрагивается и
    # остаётся как есть. Способ добычи чудесного бамбука пока нигде не
    # реализован в боте — это заготовка под будущий источник (задел —
    # см. panda.py: add_wonder_bamboo), скоро будет добавлен отдельно.
    "wonder_bamboo": "INTEGER NOT NULL DEFAULT 0",
    # --- "Дерево чудес" (см. panda.py: click_wonder_tree) ---
    # Роса и волшебный орех — редкие предметы, добываются только
    # кликами по дереву. Расходуются на прокачку уровня панды наравне
    # с wonder_bamboo/karma — см. panda.py: PANDA_LEVEL_COST /
    # level_up_panda (какие уровни каких ресурсов требуют — там же).
    "wonder_dew": "INTEGER NOT NULL DEFAULT 0",
    "magic_nut": "INTEGER NOT NULL DEFAULT 0",
    # Карма — начисляется кликами по дереву, когда не выпало ни одного
    # из редких предметов выше. Расходуется на прокачку уровня панды
    # (см. panda.py: PANDA_LEVEL_COST / level_up_panda).
    "karma": "INTEGER NOT NULL DEFAULT 0",
}

# --- garden.py ---
GARDEN_PLOTS_COLUMNS = {
    "lang": "TEXT",
}

# --- shop.py: лоты рынка теперь бывают двух видов — фрукты (crop) и
# готовая выпечка (bakery). Старые лоты (созданные до этого изменения)
# у всех имеют item_type = 'crop' по умолчанию, что верно — раньше на
# рынке продавались только фрукты.
SHOP_LISTINGS_COLUMNS = {
    "item_type": "TEXT NOT NULL DEFAULT 'crop'",
}

# --- shop.py: доп. флаги для рыночных ачивок ("Разносторонний
# торговец", achives.py) — покупал ли игрок хоть раз фрукт / хоть раз
# выпечку на рынке (у другого игрока). Оба флага 0/1, взводятся один
# раз и не сбрасываются.
SHOP_STATS_COLUMNS = {
    "bought_crop": "INTEGER NOT NULL DEFAULT 0",
    "bought_bakery": "INTEGER NOT NULL DEFAULT 0",
}

# --- bakery.py ---
BAKERY_OVENS_COLUMNS = {
    "lang": "TEXT",
}

# --- main.py (онбординг: язык/пол выбираются один раз) ---
USERS_ONBOARDING_COLUMNS = {
    "lang": "TEXT",
    "gender": "TEXT",
    "onboarded": "INTEGER NOT NULL DEFAULT 0",
}

# --- prof.py (профиль: опыт/уровень и репутация игрока) ---
USERS_PROFILE_COLUMNS = {
    # Суммарный опыт — level_from_xp() в prof.py переводит его в уровень.
    "xp": "INTEGER NOT NULL DEFAULT 0",
    # Репутация — два независимых счётчика (🔥 красный / 🔥 синий),
    # начисление под них пока не подключено нигде в боте (в будущем —
    # подарки за них), но хранятся уже сейчас, чтобы включить без
    # новой миграции.
    "reputation_red": "INTEGER NOT NULL DEFAULT 0",
    "reputation_blue": "INTEGER NOT NULL DEFAULT 0",
    # Отображаемое имя в профиле (раздел "Настройки" -> "Изменить имя") —
    # НЕ первый попавшийся user.first_name из Telegram, а то, что игрок
    # сам задал через бота. Хранится уже готовым HTML (см. prof.py:
    # process_name_change — берётся из message.html_text, а не message.text,
    # именно поэтому здесь TEXT без экранирования: в него уже входят теги
    # вроде <tg-emoji emoji-id="..."> для кастомных эмодзи или <b>/<i>,
    # если игрок их использовал, — экранировать при чтении не нужно и
    # нельзя, иначе теги превратятся в видимый текст). NULL, пока игрок
    # имя не менял, — тогда профиль по-прежнему показывает user.first_name.
    "display_name": "TEXT",
}

# --- реферальная система (main.py: /start ref<id>, prof.py: раздел
# "Друзья") ---
# referred_by — кто пригласил этого игрока (ставится один раз, при самом
# первом /start по реферальной ссылке, и больше не перезаписывается —
# см. set_referrer). referral_rewarded — начислена ли уже награда
# пригласившему ЗА ЭТОГО игрока (взводится один раз, см.
# mark_referral_rewarded — защищает от повторного начисления, если игрок
# почему-то повторно пройдёт этот шаг онбординга). Награда даётся только
# после того, как приглашённый выбрал язык И пол — просто /start по
# ссылке ничего не начисляет, поэтому referred_by и referral_rewarded —
# два отдельных флага, а не один.
USERS_REFERRAL_COLUMNS = {
    "referred_by": "INTEGER",
    "referral_rewarded": "INTEGER NOT NULL DEFAULT 0",
    # Сколько всего кристаллов/монет заработано на рефералах — для
    # отображения в разделе "Друзья" (см. prof.py: bump_referral_earned).
    "referral_crystals_earned": "INTEGER NOT NULL DEFAULT 0",
    "referral_coins_earned": "INTEGER NOT NULL DEFAULT 0",
}


# ==========================
#   СОСТОЯНИЕ СОЕДИНЕНИЯ
# ==========================

_db: aiosqlite.Connection | None = None
_init_lock = asyncio.Lock()
_write_lock = asyncio.Lock()
_pending = 0
_dirty = False
_flush_task: asyncio.Task | None = None


async def get_db() -> aiosqlite.Connection:
    """Возвращает единое переиспользуемое соединение с БД, создавая
    его (и всю схему) при первом обращении."""
    global _db
    if _db is not None:
        return _db

    async with _init_lock:
        if _db is None:
            db = await aiosqlite.connect(DB_PATH)
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await _create_schema(db)
            await db.commit()
            _db = db
            _start_flush_task()
    return _db


async def init_db() -> aiosqlite.Connection:
    """Явная точка входа для инициализации БД при старте бота.
    Вызывать один раз в main.py перед стартом polling'а."""
    return await get_db()


async def _create_schema(db: aiosqlite.Connection) -> None:
    # --- panda ---
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS panda (
            user_id INTEGER PRIMARY KEY
        )
        """
    )
    await _ensure_columns(db, "panda", PANDA_COLUMNS)

    # Купленные скины панды (см. panda.py: SKINS) — какие скины уже
    # оплачены игроком. Надет ли какой-то из них прямо сейчас — не
    # здесь, а в panda.equipped_skin_id (см. PANDA_COLUMNS выше).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS panda_skins (
            user_id INTEGER NOT NULL,
            skin_id TEXT NOT NULL,
            purchased_at REAL NOT NULL,
            PRIMARY KEY (user_id, skin_id)
        )
        """
    )

    # Изображения скинов, загруженные админом через /admin (см. panda.py:
    # get_skin_image/set_skin_image). Пока для скина нет строки здесь —
    # игрокам показывается обычный стикер (SKINS[skin_id]["sticker_id"]).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS skin_images (
            skin_id TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )

    # --- garden ---
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS garden_plots (
            user_id INTEGER NOT NULL,
            plot_index INTEGER NOT NULL,
            crop_id TEXT,
            planted_at REAL,
            lang TEXT,
            PRIMARY KEY (user_id, plot_index)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS garden_inventory (
            user_id INTEGER NOT NULL,
            crop_id TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, crop_id)
        )
        """
    )
    await _ensure_columns(db, "garden_plots", GARDEN_PLOTS_COLUMNS)

    # --- bakery ---
    # Ингредиенты (мука, сахар и т.п.) — покупаются в лавке пекарни за Pn,
    # фрукты сюда не входят: они по-прежнему берутся из корзины сада
    # (garden_inventory) и здесь не дублируются.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_ingredients (
            user_id INTEGER NOT NULL,
            ingredient_id TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, ingredient_id)
        )
        """
    )
    # Печи — те же "грядки", только для выпечки: либо пустая, либо в ней
    # печётся recipe_id, посаженный (тут — поставленный печься) в started_at.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_ovens (
            user_id INTEGER NOT NULL,
            oven_index INTEGER NOT NULL,
            recipe_id TEXT,
            started_at REAL,
            lang TEXT,
            PRIMARY KEY (user_id, oven_index)
        )
        """
    )
    # Витрина — готовая выпечка, ждущая, когда её скормят панде.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_pantry (
            user_id INTEGER NOT NULL,
            recipe_id TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, recipe_id)
        )
        """
    )
    await _ensure_columns(db, "bakery_ovens", BAKERY_OVENS_COLUMNS)

    # --- shop ---
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_balance (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seller_id INTEGER NOT NULL,
            crop_id TEXT NOT NULL,
            count INTEGER NOT NULL,
            price INTEGER NOT NULL,
            created_at REAL NOT NULL
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_stats (
            user_id INTEGER PRIMARY KEY,
            total_bought INTEGER NOT NULL DEFAULT 0,
            total_spent INTEGER NOT NULL DEFAULT 0,
            total_sold INTEGER NOT NULL DEFAULT 0,
            total_earned INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await _ensure_columns(db, "shop_stats", SHOP_STATS_COLUMNS)

    # Рыночные ачивки "Постоянный клиент"/"Своя клиентура" (achives.py)
    # считают РАЗНЫХ торговых партнёров игрока — отдельно тех, у кого он
    # покупал (direction='bought', partner_id — продавец), и тех, кому
    # он продавал (direction='sold', partner_id — покупатель). Одна
    # строка на уникальную пару (я, партнёр, направление) — повторные
    # сделки с тем же партнёром просто не создают новых строк
    # (INSERT OR IGNORE в shop.py), так что COUNT(*) по direction сразу
    # даёт число разных партнёров.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS shop_trade_partners (
            user_id INTEGER NOT NULL,
            partner_id INTEGER NOT NULL,
            direction TEXT NOT NULL,
            first_at REAL NOT NULL,
            PRIMARY KEY (user_id, partner_id, direction)
        )
        """
    )

    await _ensure_columns(db, "shop_listings", SHOP_LISTINGS_COLUMNS)

    # shop_listings читается почти всегда с фильтром по seller_id (мои
    # лоты / лимит лотов) или по item_type+crop_id (фильтр рынка) — без
    # индексов это full table scan, который с ростом числа лотов начинает
    # лагать.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_listings_seller ON shop_listings(seller_id)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_listings_crop ON shop_listings(crop_id)"
    )
    # Составной индекс под фильтр рынка по конкретному товару: теперь
    # crop_id не уникален глобально между типами (гипотетически id
    # фрукта и id рецепта выпечки могли бы совпасть), поэтому фильтр
    # всегда идёт по паре (item_type, crop_id).
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_shop_listings_type_crop ON shop_listings(item_type, crop_id)"
    )

    # --- achives.py ---
    # Одна строка на каждую открытую игроком ачивку — сам факт открытия
    # необратим, повторный INSERT OR IGNORE (см. achives.unlock) ничего
    # не сделает благодаря PRIMARY KEY на паре полей.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_achievements (
            user_id INTEGER NOT NULL,
            achv_id TEXT NOT NULL,
            unlocked_at REAL NOT NULL,
            PRIMARY KEY (user_id, achv_id)
        )
        """
    )

    # --- admin.py (учёт игроков для админ-панели) ---
    # Telegram Bot API не даёт способа узнать user_id по @username без
    # того, чтобы бот уже видел апдейт от этого пользователя, поэтому
    # admin.py ведёт свою табличку user_id <-> username, обновляемую на
    # каждый входящий апдейт (см. admin.UserTrackingMiddleware).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            first_seen REAL NOT NULL,
            last_seen REAL NOT NULL
        )
        """
    )
    # Поиск при выдаче Pn/фруктов идёт по username (регистронезависимо,
    # см. COLLATE NOCASE в admin.find_user_by_username) — без индекса
    # это full table scan на каждый такой поиск.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_username ON users(username COLLATE NOCASE)"
    )
    # Онбординг (main.py): язык и пол должны спрашиваться у игрока
    # только один раз — при первом /start. Флаг onboarded и сами
    # значения храним прямо в users, чтобы это переживало рестарт бота
    # (FSM-хранилище — MemoryStorage, оно сбрасывается при рестарте).
    await _ensure_columns(db, "users", USERS_ONBOARDING_COLUMNS)
    await _ensure_columns(db, "users", USERS_PROFILE_COLUMNS)
    await _ensure_columns(db, "users", USERS_REFERRAL_COLUMNS)
    # Кто кого пригласил ищется по referred_by (раздел "Друзья" — счётчик
    # успешных приглашений), без индекса на большой базе это full table
    # scan на каждое открытие раздела.
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by)"
    )

    # Изображения разделов бота ("Достижения"/"Сад"/"Пекарня"/"Профиль"/
    # "Донаты", а также картинка, отправляемая вместе с выбором языка на
    # /start), загружаемые админом через /admin (см. admin.py:
    # get_section_image/set_section_image/clear_section_image). Пока для
    # раздела нет строки здесь — картинка не отправляется, экран
    # раздела выглядит как раньше (чистый текст). Раньше картинка для
    # /start отдельно хранилась в bot_data.json — теперь тоже здесь,
    # под section_key = "start", вместе с остальными.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS section_images (
            section_key TEXT PRIMARY KEY,
            file_id TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
        """
    )

    # --- donate.py (привилегии) ---
    # Активная привилегия игрока ("Panda Plus"/"Panda VIP"/"Panda Premium",
    # см. donate.py: PRIVILEGE_TIERS) — одна строка на игрока, покупка
    # нового уровня перезаписывает старый (INSERT OR REPLACE, см.
    # donate.py: buy_privilege). Сами бонусы (ускорение роста в саду/
    # пекарне, лимиты на подарки, бонус к опыту, бонусный скин) здесь
    # ТОЛЬКО хранятся — применение эффектов в garden.py/bakery.py/prof.py/
    # panda.py ещё предстоит подключить отдельно.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS user_privileges (
            user_id INTEGER PRIMARY KEY,
            tier_id TEXT NOT NULL,
            purchased_at REAL NOT NULL,
            expires_at REAL NOT NULL
        )
        """
    )


async def _ensure_columns(db: aiosqlite.Connection, table: str, columns: dict[str, str]) -> None:
    """Лёгкая миграция: добавляет недостающие колонки, если бот
    обновился поверх уже существующей базы со старой схемой."""
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        existing = {row["name"] async for row in cursor}

    for column, ddl in columns.items():
        if column not in existing:
            await db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


# ==========================
#   БАТЧИНГ КОММИТОВ ("СТОПКАМИ")
# ==========================

def _start_flush_task() -> None:
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(_flush_loop())


async def _flush_loop() -> None:
    """Фоновая задача: раз в FLUSH_INTERVAL секунд сбрасывает на диск
    накопившиеся ("стопка") незакоммиченные изменения, если они есть."""
    try:
        while True:
            await asyncio.sleep(FLUSH_INTERVAL)
            await flush()
    except asyncio.CancelledError:
        pass


async def commit() -> None:
    """Помечает текущие изменения как готовые к сохранению и кладёт их
    в очередь ("стопку"). Реальный commit() выполнится пачкой — либо по
    таймеру (_flush_loop), либо немедленно, если очередь переполнилась.

    Использовать вместо `await db.commit()` после каждой записи."""
    global _dirty, _pending

    force = False
    async with _write_lock:
        _dirty = True
        _pending += 1
        if _pending >= BATCH_MAX_PENDING:
            force = True

    if force:
        await flush()


async def flush() -> None:
    """Немедленно сохраняет на диск все накопленные в "стопке"
    изменения. Вызывается автоматически по таймеру и при переполнении
    очереди, а также вручную — например, перед остановкой бота."""
    global _dirty, _pending

    if _db is None:
        return

    async with _write_lock:
        if _dirty:
            await _db.commit()
            _dirty = False
            _pending = 0


async def close_db() -> None:
    """Останавливает фоновый сброс, сохраняет всё, что накопилось в
    очереди, и закрывает соединение. Вызывать один раз при остановке
    бота (в main.py, в finally)."""
    global _db, _flush_task

    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None

    await flush()

    if _db is not None:
        await _db.close()
        _db = None


# ==========================
#   ОНБОРДИНГ (main.py: язык/пол — один раз для новичка)
# ==========================
#
# Строка в users для конкретного user_id гарантированно уже существует
# к моменту вызова этих функций из хендлеров: admin.UserTrackingMiddleware
# подключён как outer-middleware на dp.update и успевает вставить её
# (INSERT OR IGNORE) до того, как апдейт дойдёт до router'ов.

async def get_onboarding(user_id: int) -> dict | None:
    """Возвращает {"lang": ..., "gender": ...}, если игрок уже проходил
    онбординг раньше, иначе None (значит — показать выбор языка/пола)."""
    db = await get_db()
    async with db.execute(
        "SELECT lang, gender FROM users WHERE user_id = ? AND onboarded = 1",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    return {"lang": row["lang"], "gender": row["gender"]}


async def save_onboarding(user_id: int, lang: str, gender: str) -> None:
    """Помечает игрока как прошедшего онбординг — при следующих /start
    выбор языка и пола показываться больше не будет."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET lang = ?, gender = ?, onboarded = 1 WHERE user_id = ?",
        (lang, gender, user_id),
    )
    await commit()


async def save_lang(user_id: int, lang: str) -> None:
    """Меняет только язык интерфейса, не трогая пол/onboarded — вызывается
    из prof.py (раздел "Профиль" -> "Настройки") при ручном переключении
    языка, в отличие от save_onboarding, который пишет язык один раз, при
    самом первом /start."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET lang = ? WHERE user_id = ?",
        (lang, user_id),
    )
    await commit()


async def save_display_name(user_id: int, name_html: str | None) -> None:
    """Сохраняет отображаемое имя игрока (уже готовым HTML — см. комментарий
    у USERS_PROFILE_COLUMNS["display_name"] выше). name_html = None сбрасывает
    имя обратно на user.first_name из Telegram (сейчас боту не нужно, но
    оставлено на будущее — например, если понадобится кнопка "Сбросить")."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET display_name = ? WHERE user_id = ?",
        (name_html, user_id),
    )
    await commit()


# ==========================
#   ПЕРСОНАЛЬНЫЕ БЛОКИРОВКИ (защита от гонок/дюпов)
# ==========================
#
# Многие операции (покормить панду, взять фрукт из корзины, купить лот,
# посадить культуру) устроены как "прочитать состояние -> посчитать ->
# записать". Если один и тот же игрок умудряется прислать два запроса
# почти одновременно (двойной тап по кнопке, повтор от самого Telegram
# при таймауте, читер с самописным клиентом) — между чтением и записью
# есть окно, в которое может влезть второй такой же запрос и оба
# "увидят" одно и то же старое состояние. Итог — либо дюп (получить
# эффект дважды, потратив ресурс один раз), либо потерянное обновление
# (одно из двух действий пропадает).
#
# Решение — персональный asyncio.Lock на user_id, общий для
# panda/garden/shop: любая операция для конкретного игрока оборачивается
# в `async with database.user_lock(user_id): ...`, и тогда для ОДНОГО
# игрока такие цепочки чтение-запись всегда выполняются строго по
# очереди, независимо от того, в каком модуле начались. Разные игроки
# при этом друг друга не блокируют — так что под нагрузкой это не лагает.
#
# Хранится в WeakValueDictionary: как только на Lock конкретного
# пользователя никто больше не ссылается (никто прямо сейчас не сидит
# внутри `async with`), сборщик мусора сам удаляет его из словаря — со
# временем не накапливается память на когда-либо писавших боту игроков.

_user_locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = weakref.WeakValueDictionary()


def user_lock(user_id: int) -> asyncio.Lock:
    """Возвращает персональный Lock игрока, создавая его при первом
    обращении. Синхронная функция без await — вызывать безопасно, гонок
    при самом создании лока быть не может (событийный цикл однопоточный,
    а внутри нет ни одной точки переключения)."""
    lock = _user_locks.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _user_locks[user_id] = lock
    return lock


# ==========================
#   РЕФЕРАЛЬНАЯ СИСТЕМА (main.py: /start ref<id>, prof.py: "Друзья")
# ==========================

async def set_referrer(user_id: int, referrer_id: int) -> None:
    """Запоминает, кто пригласил игрока — только если у него ещё нет
    пригласившего (WHERE referred_by IS NULL), чтобы повторный переход
    по чужой реферальной ссылке не перезаписал первого пригласившего.
    Вызывать только для НОВЫХ игроков (до прохождения онбординга) —
    см. main.py: cmd_start."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET referred_by = ? WHERE user_id = ? AND referred_by IS NULL",
        (referrer_id, user_id),
    )
    await commit()


async def get_referrer(user_id: int) -> int | None:
    """Возвращает user_id того, кто пригласил игрока, либо None, если
    его никто не приглашал (или это ещё не заходило в базу)."""
    db = await get_db()
    async with db.execute(
        "SELECT referred_by FROM users WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["referred_by"] if row and row["referred_by"] is not None else None


async def mark_referral_rewarded(user_id: int) -> bool:
    """Атомарно взводит флаг 'награда за этого реферала уже начислена'.
    Возвращает True, если флаг только что взведён этим вызовом (т.е.
    награду нужно начислить), и False, если он уже был взведён раньше
    (защита от повторного начисления — например, если игрок каким-то
    образом повторно пройдёт выбор пола)."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE users SET referral_rewarded = 1 WHERE user_id = ? AND referral_rewarded = 0",
        (user_id,),
    )
    await commit()
    return cursor.rowcount > 0


async def bump_referral_earned(user_id: int, crystals: int, coins: int) -> None:
    """Прибавляет к счётчикам 'всего заработано на рефералах' — только
    для отображения в разделе "Друзья" (см. prof.py), сам баланс тут не
    трогается (им занимаются prof.add_crystals/shop.add_balance)."""
    db = await get_db()
    await db.execute(
        "UPDATE users SET referral_crystals_earned = referral_crystals_earned + ?, "
        "referral_coins_earned = referral_coins_earned + ? WHERE user_id = ?",
        (crystals, coins, user_id),
    )
    await commit()


async def get_referral_stats(user_id: int) -> dict:
    """Статистика для раздела "Друзья": сколько друзей уже принесли
    награду (count), и сколько суммарно кристаллов/монет на них
    заработано."""
    db = await get_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM users WHERE referred_by = ? AND referral_rewarded = 1",
        (user_id,),
    ) as cursor:
        count_row = await cursor.fetchone()
    async with db.execute(
        "SELECT referral_crystals_earned, referral_coins_earned FROM users WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        totals_row = await cursor.fetchone()
    return {
        "count": count_row["cnt"] if count_row else 0,
        "crystals": totals_row["referral_crystals_earned"] if totals_row else 0,
        "coins": totals_row["referral_coins_earned"] if totals_row else 0,
    }
