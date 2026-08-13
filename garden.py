"""
Раздел "Сад".

Идея:
    Игрок выращивает на грядках фрукты. Выращивание занимает реальное
    время (от 5 до 30 минут в зависимости от культуры). Как только фрукт
    созревает, он автоматически перекладывается в "корзину" (инвентарь),
    а игроку приходит уведомление в чат — вручную собирать ничего не
    нужно. Из корзины фрукт скармливается панде — каждый восполняет
    голод не на фиксированный процент, а на случайный, заново "бросаемый"
    в момент кормления (см. CROPS: hunger_restore_min / _max ниже).

Культуры (8 шт.), диапазоны восполнения голода при кормлении:
    Бамбук: 4–10%
    Мандарины, Яблоко, Груша, Виноград: 5–12%
    Банан, Манго, Ананас: 7–15%

    Игроку факт рандома нигде не показывается: в UI нет цифры до
    кормления, а после кормления показывается только фактический
    результат конкретной попытки.

Грядки:
    У игрока GARDEN_PLOT_COUNT грядок. Каждая грядка либо пуста, либо
    на ней что-то растёт (crop_id + planted_at). Прогресс роста, как и
    в panda.py, считается "лениво" — чистой математикой по таймстампу
    посадки при каждом обращении. Момент созревания дополнительно
    отслеживается фоновой asyncio-задачей (создаётся при посадке): она
    "спит" до нужного времени, затем сама переносит урожай в корзину и
    шлёт игроку уведомление. При старте бота нужно вызвать
    reschedule_pending_harvests(bot) — она заново создаёт такие задачи
    для всего, что уже растёт, иначе после перезапуска процесса
    уведомления по уже посаженным грядкам потеряются. Сам перенос в
    корзину подстрахован и без фоновой задачи: при любом обращении к
    грядкам (_get_plots) всё, что уже созрело, тихо собирается сама
    функция — так что "зависшего" урожая не бывает в принципе.

Кормление панды:
    Готовый фрукт из корзины скармливается панде через
    panda.restore_hunger(user_id, percent) — эта функция восполняет
    голод частично, в отличие от panda.feed_panda (полный сброс).

Хранение:
    Общая база данных бота (см. database.py) — единое asyncio-соединение
    на весь процесс, WAL-режим, запись "стопками" (батч-коммиты). Гонки
    между параллельными запросами одного игрока (двойной тап и т.п.)
    закрыты персональным локом — database.user_lock(user_id).

Подключение в main.py:
    import garden
    dp.include_router(garden.router)

    # Один раз при старте — лениво создаёт garden_harvest_counts и
    # garden_achv_state, если их ещё нет (нужны ачивкам сада), и
    # заодно регистрирует в achives.PROGRESS_PROVIDERS провайдеры
    # прогресса для счётных ачивок сада (см. ниже, секция "ПРОГРЕСС
    # СЧЁТНЫХ АЧИВОК САДА") — чтобы карточка ачивки показывала
    # реальный "X/Y" и процент, а не всегда бинарные 0%/100%:
    await garden.ensure_achv_tables()

    # до start_polling — иначе после рестарта потеряются уведомления
    # о созревании урожая, посаженного до перезапуска:
    await garden.reschedule_pending_harvests(bot)

Ачивки, требующие вызова со стороны market.py (эта ачивка не может
быть отслежена изнутри garden.py, т.к. сама продажа лота происходит
там):
    garden_sell_50    — market.py должен звать garden.record_market_sale(
                         user_id, count) в момент, когда лот РЕАЛЬНО
                         продан другому игроку (не при выставлении!).
    garden_instant_sell — market.py должен звать garden.record_instant_sell(
                         user_id) в момент мгновенной продажи фрукта боту.

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import asyncio
import logging
import random
import time

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database

logger = logging.getLogger(__name__)

router = Router(name="garden")


# ==========================
#   НАСТРОЙКИ
# ==========================

GARDEN_PLOT_COUNT = 6

# Сколько грядок доступно бесплатно с самого начала (не требуют
# открытия за монеты) — первая страница целиком. Остальные
# (GARDEN_BASE_PLOT_COUNT..GARDEN_PLOT_COUNT-1) — платные, см.
# PLOT_UNLOCK_COST/unlock_plot ниже. Ачивка "Все грядки заняты"
# (garden_all_plots_full) намеренно привязана именно к этому базовому
# числу, а не к GARDEN_PLOT_COUNT — иначе с добавлением платных грядок
# она стала бы куда сложнее, чем задумывалась изначально.
GARDEN_BASE_PLOT_COUNT = 3

# Пагинация грядок на экране сада — по PLOTS_PER_PAGE штук на страницу.
PLOTS_PER_PAGE = 3

# Стоимость открытия дополнительных грядок (индекс грядки -> цена в Pn).
PLOT_UNLOCK_COST = {
    3: 5000,
    4: 15000,
    5: 50000,
}

# Кастомный премиум-эмодзи "замок" — для кнопки открытия платной
# грядки и для пометки "Грядка закрыта" в тексте (см. bakery.py:
# аналогичные *_EMOJI_ID константы, тот же приём).
PLOT_LOCK_EMOJI_ID = "5296369303661067030"

# Кастомные премиум-эмодзи для кнопок пагинации ("◀️ Пред. страница" /
# "След. страница ▶️") — те же самые ID, что уже используются в
# bakery.py (см. там PAGE_PREV_EMOJI_ID/PAGE_NEXT_EMOJI_ID) — единый
# визуальный стиль пагинации во всех разделах бота.
PAGE_PREV_EMOJI_ID = "5255703720078879038"   # 🔙
PAGE_NEXT_EMOJI_ID = "5253767677670862169"   # 🔜


# ==========================
#   УЛУЧШЕНИЕ ГРЯДОК
# ==========================
# У каждой грядки (независимо от PLOT_UNLOCK_COST — это про открытие
# самой грядки, а это про её "прокачку") есть уровень от 1 до
# PLOT_UPGRADE_MAX_LEVEL. Уровень хранится в отдельной таблице
# garden_plot_levels (заводится лениво в ensure_achv_tables, см. ниже) —
# отсутствие строки означает уровень 1 (никогда не улучшалась).
#
# Каждый уровень сверх первого сокращает время выращивания ЛЮБОЙ
# культуры именно на этой грядке — линейно, поровну на каждый уровень,
# так что к 10 уровню суммарное ускорение ровно PLOT_UPGRADE_MAX_SPEEDUP
# (см. _plot_time_factor). Улучшать можно только пустую грядку (пока на
# ней ничего не растёт) — иначе пришлось бы задним числом пересчитывать
# уже тикающий таймер урожая и переставлять фоновую задачу автосбора
# (см. _schedule_auto_harvest), а так эффект уровня просто "запекается"
# в момент посадки (plant_crop) и дальше не меняется до следующего
# сбора — как и разовый бонус от привилегии (_privilege_speedup_offset).
PLOT_UPGRADE_MAX_LEVEL = 10

# Максимальное ускорение на 10 уровне: время выращивания сокращается не
# более чем в 4 раза (т.е. становится 25% от базового).
PLOT_UPGRADE_MAX_SPEEDUP = 4.0

# Стоимость перехода НА уровень N (ключ — целевой уровень 2..10) в Pn.
# Геометрическая прогрессия от 3000 (2 уровень) до 150000 (10 уровень).
PLOT_UPGRADE_COST = {
    2: 3000,
    3: 4900,
    4: 8000,
    5: 13000,
    6: 21200,
    7: 34600,
    8: 56400,
    9: 92000,
    10: 150000,
}


def _plot_time_factor(level: int) -> float:
    """Множитель к времени роста для уровня грядки level: 1.0 на уровне 1
    (без ускорения), линейно убывает до 1/PLOT_UPGRADE_MAX_SPEEDUP на
    уровне PLOT_UPGRADE_MAX_LEVEL (см. докстринг раздела выше)."""
    level = max(1, min(PLOT_UPGRADE_MAX_LEVEL, level))
    min_factor = 1 / PLOT_UPGRADE_MAX_SPEEDUP
    return 1 - (level - 1) * (1 - min_factor) / (PLOT_UPGRADE_MAX_LEVEL - 1)


def _effective_grow_seconds(crop_id: str, level: int) -> float:
    """Время выращивания crop_id на грядке уровня level, с учётом
    ускорения от уровня грядки (см. _plot_time_factor)."""
    return CROPS[crop_id]["grow_seconds"] * _plot_time_factor(level)

# Пороги общего счётчика собранных фруктов (см. _bump_harvest_count) —
# garden_harvest_10/100/1000/10000.
_TOTAL_HARVEST_THRESHOLDS = [
    (10, "garden_harvest_10"),
    (100, "garden_harvest_100"),
    (1000, "garden_harvest_1000"),
    (10000, "garden_harvest_10000"),
]

BAR_LENGTH = 10
BAR_FILLED = "▰"
BAR_EMPTY = "▱"


def _render_bar(percent: float) -> str:
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * BAR_LENGTH)
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_LENGTH - filled)


# ==========================
#   КУЛЬТУРЫ
# ==========================
# grow_seconds — время выращивания в реальных секундах
# hunger_restore_min / hunger_restore_max — диапазон, в котором каждый раз
# заново случайным образом определяется, на сколько % восполнится голод
# панды при кормлении именно этим фруктом (см. roll_hunger_restore)

CROPS = {
    "bamboo": {
        "emoji": "🎋",
        "grow_seconds": 5 * 60,
        "hunger_restore_min": 4,
        "hunger_restore_max": 10,
        "name": {"ru": "Бамбук", "en": "Bamboo"},
        "flavor": {
            "ru": "Молодые сочные побеги — то, с чего начинается день любой панды.",
            "en": "Fresh tender shoots — the way any panda's day should start.",
        },
    },
    "tangerine": {
        "emoji": "🍊",
        "grow_seconds": 10 * 60,
        "hunger_restore_min": 5,
        "hunger_restore_max": 12,
        "name": {"ru": "Мандарины", "en": "Tangerines"},
        "flavor": {
            "ru": "Яркие и ароматные, с тонкой сладкой кислинкой — поднимают настроение с первого укуса.",
            "en": "Bright, fragrant, faintly tart — lifts the mood from the very first bite.",
        },
    },
    "apple": {
        "emoji": "🍎",
        "grow_seconds": 15 * 60,
        "hunger_restore_min": 5,
        "hunger_restore_max": 12,
        "name": {"ru": "Яблоко", "en": "Apple"},
        "flavor": {
            "ru": "Хрустящее и наливное — простое лакомство, которое никогда не приедается.",
            "en": "Crisp and juicy — a simple treat that never gets old.",
        },
    },
    "pear": {
        "emoji": "🍐",
        "grow_seconds": 18 * 60,
        "hunger_restore_min": 5,
        "hunger_restore_max": 12,
        "name": {"ru": "Груша", "en": "Pear"},
        "flavor": {
            "ru": "Тающая мякоть с лёгкой цветочной нотой — фрукт для тех, кто не спешит.",
            "en": "Melting flesh with a hint of floral sweetness — a fruit for the unhurried.",
        },
    },
    "grape": {
        "emoji": "🍇",
        "grow_seconds": 12 * 60,
        "hunger_restore_min": 5,
        "hunger_restore_max": 12,
        "name": {"ru": "Виноград", "en": "Grapes"},
        "flavor": {
            "ru": "Гроздь тугих сладких ягод — маленький праздник в каждой из них.",
            "en": "A bunch of plump, sweet berries — a tiny celebration in every one.",
        },
    },
    "banana": {
        "emoji": "🍌",
        "grow_seconds": 20 * 60,
        "hunger_restore_min": 7,
        "hunger_restore_max": 15,
        "name": {"ru": "Банан", "en": "Banana"},
        "flavor": {
            "ru": "Питательный и мягкий — заряжает энергией надолго.",
            "en": "Filling and soft — energy that lasts for hours.",
        },
    },
    "mango": {
        "emoji": "🥭",
        "grow_seconds": 25 * 60,
        "hunger_restore_min": 7,
        "hunger_restore_max": 15,
        "name": {"ru": "Манго", "en": "Mango"},
        "flavor": {
            "ru": "Тропический деликатес с густым медовым вкусом.",
            "en": "A tropical delicacy with a rich, honeyed flavor.",
        },
    },
    "pineapple": {
        "emoji": "🍍",
        "grow_seconds": 30 * 60,
        "hunger_restore_min": 7,
        "hunger_restore_max": 15,
        "name": {"ru": "Ананас", "en": "Pineapple"},
        "flavor": {
            "ru": "Царь сада: сладкий, сочный, с игристой кислинкой.",
            "en": "The garden's king of fruit: sweet, juicy, with a sparkling tartness.",
        },
    },
}

CROP_ORDER = ["bamboo", "tangerine", "apple", "pear", "grape", "banana", "mango", "pineapple"]


def roll_hunger_restore(crop_id: str) -> int:
    """Каждый вызов — новый случайный % восполнения голода для этого
    фрукта, в пределах его диапазона (hunger_restore_min..max)."""
    crop = CROPS[crop_id]
    return random.randint(crop["hunger_restore_min"], crop["hunger_restore_max"])


# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

BUTTON_TEXT = {
    "ru": "Сад",
    "en": "Garden",
}

TEXTS = {
    "ru": {
        "title": "🌿 <b>Сад</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "plot_growing": "{emoji} <b>{name}</b> — {percent}%\n<i>Готово через {time}</i>",
        "plot_button_growing": "{emoji} {percent}%",
        "info_alert": "{emoji} {name}\n{bar} {percent}%\nГотово через {time}",
        "basket_title": "<b>🧺 Корзина:</b>",
        "basket_empty": "<i>пока пусто — дождитесь первого урожая</i>",
        "basket_item": "{emoji} ×{count}",
        "plant_button": "🌱 Посадить",
        "back_button": "◀️ Назад",
        "choose_crop_title": "🌱 <b>Что посадить?</b>",
        "crop_button": "{emoji} {name} · {time}",
        "planted_toast": "Посажено: {emoji} {name}",
        "plot_taken_toast": "На этой грядке уже что-то растёт.",
        "auto_harvested_notice": "<i>{emoji} {name} созрел(а) и уже в корзине! 🧺\nЗа это дали +{xp} XP</i>",
        "no_free_plot_toast": "Все грядки заняты — дождитесь урожая.",
        "time_min_sec": "{minutes} мин {seconds} сек",
        "time_sec": "{seconds} сек",
        "title_page_suffix": " <i>(стр. {page}/{total})</i>",
        "plot_locked_line": f'<tg-emoji emoji-id="5296369303661067030">🔒</tg-emoji> <i>Грядка закрыта</i>',
        "unlock_button": "Открыть — {cost}",
        "unlocked_toast": "🌱 Открыта новая грядка!",
        "unlock_not_enough_toast": "Не хватает монет, чтобы открыть эту грядку.",
        "unlock_already_toast": "Эта грядка уже открыта.",
        "page_prev_button": "Пред. страница",
        "page_next_button": "След. страница",
        "upgrade_button": "🔧 Улучшить — {cost}",
        "plot_level_line": "<i>🔧 Уровень: {level}/{max_level}</i>",
        "upgrade_not_enough_toast": "Не хватает монет для улучшения грядки.",
        "upgrade_busy_toast": "Нельзя улучшать грядку, пока на ней что-то растёт.",
        "upgrade_max_toast": "Эта грядка уже улучшена до максимума.",
        "upgrade_done_toast": "🔧 Грядка улучшена до {level} уровня! Выращивание стало быстрее.",
    },
    "en": {
        "title": "🌿 <b>Garden</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "plot_growing": "{emoji} <b>{name}</b> — {percent}%\n<i>Ready in {time}</i>",
        "plot_button_growing": "{emoji} {percent}%",
        "info_alert": "{emoji} {name}\n{bar} {percent}%\nReady in {time}",
        "basket_title": "<b>🧺 Basket:</b>",
        "basket_empty": "<i>empty for now — wait for the first harvest</i>",
        "basket_item": "{emoji} ×{count}",
        "plant_button": "🌱 Plant",
        "back_button": "◀️ Back",
        "choose_crop_title": "🌱 <b>What to plant?</b>",
        "crop_button": "{emoji} {name} · {time}",
        "planted_toast": "Planted: {emoji} {name}",
        "plot_taken_toast": "Something is already growing on this plot.",
        "auto_harvested_notice": "<i>{emoji} {name} is ripe and already in the basket! 🧺\nGained +{xp} XP for it</i>",
        "no_free_plot_toast": "All plots are taken — wait for the harvest.",
        "time_min_sec": "{minutes}m {seconds}s",
        "time_sec": "{seconds}s",
        "title_page_suffix": " <i>(page {page}/{total})</i>",
        "plot_locked_line": f'<tg-emoji emoji-id="5296369303661067030">🔒</tg-emoji> <i>Plot locked</i>',
        "unlock_button": "Unlock — {cost}",
        "unlocked_toast": "🌱 A new plot is unlocked!",
        "unlock_not_enough_toast": "Not enough coins to unlock this plot.",
        "unlock_already_toast": "This plot is already unlocked.",
        "page_prev_button": "Prev page",
        "page_next_button": "Next page",
        "upgrade_button": "🔧 Upgrade — {cost}",
        "plot_level_line": "<i>🔧 Level: {level}/{max_level}</i>",
        "upgrade_not_enough_toast": "Not enough coins to upgrade this plot.",
        "upgrade_busy_toast": "Can't upgrade a plot while something is growing on it.",
        "upgrade_max_toast": "This plot is already at max level.",
        "upgrade_done_toast": "🔧 Plot upgraded to level {level}! Growing is faster now.",
    },
}


def _format_duration(seconds: float, lang: str) -> str:
    t = TEXTS[lang]
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    if minutes > 0:
        return t["time_min_sec"].format(minutes=minutes, seconds=secs)
    return t["time_sec"].format(seconds=secs)


# ==========================
#   ХРАНИЛИЩЕ (общая БД — см. database.py)
# ==========================
#
# Своего соединения и своих таблиц этот модуль больше не создаёт —
# и то, и другое теперь общее для всего бота, в database.py.


async def _get_plots(user_id: int) -> tuple[list[aiosqlite.Row], list[dict]]:
    """Возвращает (грядки, achv_results) — GARDEN_PLOT_COUNT грядок,
    создавая недостающие пустыми. Перед этим тихо собирает всё, что
    уже созрело, — подстраховка на случай, если фоновая задача ещё не
    сработала; achv_results — ачивки, реально выданные ЭТОЙ подстраховкой
    (см. _auto_collect_ready), их должен показать вызывающий хендлер —
    сама _get_plots ничего игроку не отправляет."""
    achv_results = await _auto_collect_ready(user_id)
    db = await database.get_db()
    async with db.execute(
        "SELECT * FROM garden_plots WHERE user_id = ? ORDER BY plot_index", (user_id,)
    ) as cursor:
        rows = {row["plot_index"]: row for row in await cursor.fetchall()}

    missing = [i for i in range(GARDEN_PLOT_COUNT) if i not in rows]
    for i in missing:
        await db.execute(
            "INSERT INTO garden_plots (user_id, plot_index, crop_id, planted_at) VALUES (?, ?, NULL, NULL)",
            (user_id, i),
        )
    if missing:
        await database.commit()
        async with db.execute(
            "SELECT * FROM garden_plots WHERE user_id = ? ORDER BY plot_index", (user_id,)
        ) as cursor:
            return await cursor.fetchall(), achv_results

    return [rows[i] for i in range(GARDEN_PLOT_COUNT)], achv_results


async def get_inventory(user_id: int) -> dict[str, int]:
    db = await database.get_db()
    async with db.execute(
        "SELECT crop_id, count FROM garden_inventory WHERE user_id = ? AND count > 0", (user_id,)
    ) as cursor:
        return {row["crop_id"]: row["count"] async for row in cursor}


async def _privilege_speedup_offset(user_id: int, duration_seconds: float) -> float:
    """Сколько секунд отнять от времени старта роста, если у игрока
    активна привилегия с ускорением (donate.py: PRIVILEGE_TIERS,
    speedup_percent). 0, если привилегии нет. "Задним числом" сдвигаем
    planted_at на этот офсет в plant_crop ниже — весь остальной код
    (расчёт elapsed/remaining/процента, автосбор по таймеру, фоновый
    опрос неубранных грядок) везде считает от planted_at, так что одной
    правки в момент посадки достаточно, чтобы ускорение применилось
    всюду. Локальный импорт donate — donate.py не импортирует garden.py
    на верхнем уровне, но чтобы не рисковать циклом (donate.py в свою
    очередь дёргает prof.py, который тоже может импортировать что-то из
    игровых модулей), импортируем здесь же, по аналогии с "import
    achives"/"import prof" в других функциях этого файла."""
    import donate

    active = await donate.get_active_privilege(user_id)
    if active is None:
        return 0.0
    percent = active["tier"]["speedup_percent"]
    if not percent:
        return 0.0
    return duration_seconds * percent / 100


async def plant_crop(user_id: int, plot_index: int, crop_id: str, lang: str) -> float | None:
    """Сажает культуру на грядку. Возвращает таймстамп посадки, либо None,
    если грядка занята. lang сохраняется вместе с грядкой, чтобы уведомление
    о созревании потом пришло на нужном языке.

    Лок + условный UPDATE (WHERE crop_id IS NULL) исключают ситуацию, когда
    два быстрых тапа по разным культурам почти одновременно проходят
    проверку "грядка свободна" и один урожай тихо затирает другой.

    Время роста берётся с поправкой на уровень грядки (см.
    _effective_grow_seconds/PLOT_UPGRADE_COST) — чем выше уровень, тем
    короче базовое время ДО применения ускорения от привилегии ниже.

    Если у игрока активна привилегия с ускорением роста — возвращаемый
    planted_at "задним числом" сдвинут в прошлое (см.
    _privilege_speedup_offset), поэтому созреет культура раньше на тот
    же процент, а сам таймстамп по-прежнему честно отражает момент,
    от которого нужно отсчитывать рост."""
    level = await _get_single_plot_level(user_id, plot_index)
    grow_seconds = _effective_grow_seconds(crop_id, level)
    speedup_offset = await _privilege_speedup_offset(user_id, grow_seconds)

    async with database.user_lock(user_id):
        db = await database.get_db()
        planted_at = time.time() - speedup_offset

        # Гарантируем, что строка для этой грядки существует (её могло
        # ещё не быть, если игрок сажает раньше первого открытия сада).
        await db.execute(
            """
            INSERT INTO garden_plots (user_id, plot_index, crop_id, planted_at, lang)
            VALUES (?, ?, NULL, NULL, NULL)
            ON CONFLICT (user_id, plot_index) DO NOTHING
            """,
            (user_id, plot_index),
        )

        cursor = await db.execute(
            """
            UPDATE garden_plots
            SET crop_id = ?, planted_at = ?, lang = ?
            WHERE user_id = ? AND plot_index = ? AND crop_id IS NULL
            """,
            (crop_id, planted_at, lang, user_id, plot_index),
        )
        await database.commit()

        if cursor.rowcount == 0:
            return None
        return planted_at


async def _get_unlocked_extra_plots(user_id: int) -> set[int]:
    """Индексы ДОПОЛНИТЕЛЬНЫХ грядок (>= GARDEN_BASE_PLOT_COUNT), уже
    открытых игроком за монеты (см. unlock_plot). Первые
    GARDEN_BASE_PLOT_COUNT грядок сюда не входят — они открыты всегда,
    см. _is_plot_unlocked."""
    db = await database.get_db()
    async with db.execute(
        "SELECT plot_index FROM garden_plot_unlocks WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {row["plot_index"] async for row in cursor}


def _is_plot_unlocked(plot_index: int, unlocked_extra: set[int]) -> bool:
    return plot_index < GARDEN_BASE_PLOT_COUNT or plot_index in unlocked_extra


async def unlock_plot(user_id: int, plot_index: int) -> str:
    """Открывает платную грядку plot_index за монеты (PLOT_UNLOCK_COST).
    Списание — через shop.charge_balance, та же Pn-экономика, что и
    everywhere else в боте (см. bakery.buy_ingredient — тот же паттерн:
    лок на user_id, проверка+списание одним вызовом, затем запись
    результата). Возвращает:
      "ok"          — открыто прямо сейчас
      "already"     — уже была открыта раньше (ничего не списано)
      "not_enough"  — не хватило монет (ничего не списано)
      "invalid"     — этот индекс вообще не подлежит открытию за монеты
                       (базовая грядка либо индекс вне диапазона)
    Локальный импорт shop — во избежание цикла импортов (shop.py в
    свою очередь импортирует garden.py на верхнем уровне, см. докстринг
    модуля bakery.py)."""
    if plot_index not in PLOT_UNLOCK_COST:
        return "invalid"

    import shop

    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT 1 FROM garden_plot_unlocks WHERE user_id = ? AND plot_index = ?",
            (user_id, plot_index),
        ) as cursor:
            if await cursor.fetchone():
                return "already"

        charged = await shop.charge_balance(user_id, PLOT_UNLOCK_COST[plot_index])
        if not charged:
            return "not_enough"

        await db.execute(
            "INSERT OR IGNORE INTO garden_plot_unlocks (user_id, plot_index) VALUES (?, ?)",
            (user_id, plot_index),
        )
        await database.flush()

    return "ok"


async def _get_plot_levels(user_id: int) -> dict[int, int]:
    """Индекс грядки -> её текущий уровень улучшения. Грядок без строки
    в garden_plot_levels (никогда не улучшались) в словаре нет — см.
    _plot_level, которая для них возвращает уровень 1 по умолчанию."""
    db = await database.get_db()
    async with db.execute(
        "SELECT plot_index, level FROM garden_plot_levels WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {row["plot_index"]: row["level"] async for row in cursor}


def _plot_level(levels: dict[int, int], plot_index: int) -> int:
    return levels.get(plot_index, 1)


async def _get_single_plot_level(user_id: int, plot_index: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT level FROM garden_plot_levels WHERE user_id = ? AND plot_index = ?",
        (user_id, plot_index),
    ) as cursor:
        row = await cursor.fetchone()
    return row["level"] if row else 1


async def upgrade_plot(user_id: int, plot_index: int) -> str:
    """Повышает уровень грядки plot_index на 1 (см. PLOT_UPGRADE_COST/
    PLOT_UPGRADE_MAX_LEVEL). Списание — через shop.charge_balance, тот
    же паттерн, что и в unlock_plot (лок на user_id, проверка+списание
    одним вызовом). Возвращает:
      "ok"          — улучшено прямо сейчас
      "busy"        — на грядке сейчас что-то растёт, улучшать нельзя
                       (см. докстринг раздела УЛУЧШЕНИЕ ГРЯДОК)
      "max_level"   — уже максимальный уровень
      "not_enough"  — не хватило монет (ничего не списано)
    Локальный импорт shop — по той же причине, что и в unlock_plot."""
    import shop

    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT crop_id FROM garden_plots WHERE user_id = ? AND plot_index = ?",
            (user_id, plot_index),
        ) as cursor:
            plot_row = await cursor.fetchone()
        if plot_row is not None and plot_row["crop_id"] is not None:
            return "busy"

        async with db.execute(
            "SELECT level FROM garden_plot_levels WHERE user_id = ? AND plot_index = ?",
            (user_id, plot_index),
        ) as cursor:
            level_row = await cursor.fetchone()
        current_level = level_row["level"] if level_row else 1
        if current_level >= PLOT_UPGRADE_MAX_LEVEL:
            return "max_level"

        next_level = current_level + 1
        charged = await shop.charge_balance(user_id, PLOT_UPGRADE_COST[next_level])
        if not charged:
            return "not_enough"

        await db.execute(
            """
            INSERT INTO garden_plot_levels (user_id, plot_index, level) VALUES (?, ?, ?)
            ON CONFLICT (user_id, plot_index) DO UPDATE SET level = excluded.level
            """,
            (user_id, plot_index, next_level),
        )
        await database.flush()

    return "ok"


async def _plant_achievements(user_id: int) -> list[dict]:
    """Ачивки, привязанные к самому факту посадки: "Первая посадка" —
    за первую посадку когда-либо, "Все грядки заняты" — если после
    этой посадки заняты сразу все GARDEN_PLOT_COUNT грядок. Звать сразу
    после успешного plant_crop (planted_at is not None)."""
    import achives

    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM garden_plots WHERE user_id = ? AND crop_id IS NOT NULL",
        (user_id,),
    ) as cursor:
        occupied = (await cursor.fetchone())["cnt"]

    achv_ids = ["garden_first_plant"]
    if occupied >= GARDEN_BASE_PLOT_COUNT:
        achv_ids.append("garden_all_plots_full")

    results = []
    for achv_id in achv_ids:
        result = await achives.unlock(user_id, achv_id)
        if result:
            results.append(result)
    return results


async def _bump_harvest_count(user_id: int, crop_id: str) -> tuple[int, int, int]:
    """+1 к счётчику собранного именно crop_id (в отдельной таблице
    garden_harvest_counts — не путать с garden_inventory: этот счётчик
    НЕ уменьшается при кормлении/продаже, копится вечно, для ачивок
    "Специалист по бамбуку"/"Бамбуковая ферма"/"Ботаник"/общих
    garden_harvest_N). Возвращает (сколько именно этого фрукта всего,
    сколько фруктов всего любых видов, сколько РАЗНЫХ видов уже
    собирались хотя бы раз)."""
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO garden_harvest_counts (user_id, crop_id, count) VALUES (?, ?, 1)
        ON CONFLICT (user_id, crop_id) DO UPDATE SET count = count + 1
        """,
        (user_id, crop_id),
    )
    await database.commit()
    async with db.execute(
        "SELECT crop_id, count FROM garden_harvest_counts WHERE user_id = ?", (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    counts = {r["crop_id"]: r["count"] for r in rows}
    return counts.get(crop_id, 0), sum(counts.values()), len(counts)


def _is_night_harvest(ts: float) -> bool:
    """"После полуночи" — 00:00–05:59 по времени сервера (глубокая ночь,
    а не буквально любая минута после 00:00 до следующего вечера)."""
    from datetime import datetime

    return datetime.fromtimestamp(ts).hour < 6


async def _collect_plot_if_matches(
    user_id: int, plot_index: int, crop_id: str, planted_at: float
) -> tuple[bool, int | None, dict | None, list[dict]]:
    """Идемпотентно переносит урожай в корзину, но только если на грядке
    всё ещё растёт именно та посадка (crop_id + planted_at совпадают).
    Возвращает (collected, xp_gained, level_info, achv_results): при
    collected=True — сколько XP реально начислено за этот фрукт,
    результат prof.add_xp() целиком (пригодится вызывающему, чтобы
    показать XP и левелап в уведомлении) и список результатов
    achives.unlock() ("Первый урожай" + все ачивки за счётчики/особые
    условия сбора этого конкретного фрукта, см. ниже — уже отфильтрован
    от None, т.е. содержит только реально выданные впервые), иначе
    (False, None, None, []).

    И фоновая задача автосбора, и подстраховка в _get_plots вызывают
    именно эту функцию — она может быть вызвана для одного и того же
    урожая из двух разных мест почти одновременно. Раньше здесь было
    SELECT, а потом отдельные UPDATE/INSERT — между ними было окно,
    в которое оба вызова успевали пройти проверку и оба зачисляли
    фрукт в корзину (дюп). Теперь единственная точка принятия решения —
    сам условный UPDATE (WHERE crop_id = ? AND planted_at = ?): SQLite
    выполняет его атомарно, поэтому "выиграть" гонку и зачислить фрукт
    может только один из двух одновременных вызовов. Лок на user_id —
    дополнительный слой (и защищает от параллельного плантинга/сбора
    той же грядки), сам по себе он тоже был бы достаточен, но условный
    UPDATE ничего не стоит и работает, даже если где-то лок случайно не
    захватили."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        cursor = await db.execute(
            """
            UPDATE garden_plots
            SET crop_id = NULL, planted_at = NULL, lang = NULL
            WHERE user_id = ? AND plot_index = ? AND crop_id = ? AND planted_at = ?
            """,
            (user_id, plot_index, crop_id, planted_at),
        )

        if cursor.rowcount == 0:
            # Кто-то другой (или мы сами в предыдущем вызове) уже собрал
            # именно этот урожай — второй раз зачислять фрукт нельзя.
            await database.commit()
            return False, None, None, []

        await db.execute(
            """
            INSERT INTO garden_inventory (user_id, crop_id, count) VALUES (?, ?, 1)
            ON CONFLICT (user_id, crop_id) DO UPDATE SET count = count + 1
            """,
            (user_id, crop_id),
        )
        await database.commit()

    # За опытом — уже ЗА ПРЕДЕЛАМИ "async with database.user_lock(...)"
    # выше: prof.add_xp() сам берёт этот же лок на того же user_id, а он
    # не реентерабельный (обычный asyncio.Lock) — начисление изнутри
    # ещё удерживаемого лока было бы дедлоком. Импорт — локальный,
    # чтобы не завести цикл: prof → shop → bakery → garden → prof (и
    # achives → prof/shop → ... → garden — та же причина для achives).
    import prof
    import achives

    xp_gained = random.randint(50, 80)
    level_info = await prof.add_xp(user_id, xp_gained)

    crop_count, total_count, distinct_count = await _bump_harvest_count(user_id, crop_id)
    inventory = await get_inventory(user_id)
    basket_total = sum(inventory.values())

    achv_ids = ["first_harvest"]
    achv_ids += [aid for need, aid in _TOTAL_HARVEST_THRESHOLDS if total_count >= need]
    if crop_count >= 20:
        achv_ids.append("garden_one_crop_20")
    if crop_id == "bamboo" and crop_count >= 100:
        achv_ids.append("garden_bamboo_100")
    if distinct_count >= len(CROPS):
        achv_ids.append("garden_all_crops")
    if crop_id == "pineapple":
        achv_ids.append("garden_pineapple_first")
    if basket_total >= 50:
        achv_ids.append("garden_basket_50")
    if _is_night_harvest(time.time()):
        achv_ids.append("garden_harvest_night")

    achv_results = []
    for achv_id in achv_ids:
        result = await achives.unlock(user_id, achv_id)
        if result:
            achv_results.append(result)

    return True, xp_gained, level_info, achv_results


async def _auto_collect_ready(user_id: int) -> list[dict]:
    """Тихо собирает всё, что уже созрело у пользователя (уведомление о
    каждом отдельном фрукте — забота фоновой задачи; это лишь
    подстраховка на случай, если она почему-то не сработала). Возвращает
    ачивки, реально выданные в рамках ЭТОГО вызова — по одному сбору
    (см. _collect_plot_if_matches), плюс "Тройной урожай"
    (garden_all_plots_ripe), если в этот заход собраны сразу все
    GARDEN_PLOT_COUNT грядок — именно поэтому эта ачивка засчитывается
    только тут (открытие сада/просмотр грядки), а не в фоновой задаче,
    которая всегда собирает грядки по одной."""
    db = await database.get_db()
    now = time.time()
    async with db.execute(
        "SELECT plot_index, crop_id, planted_at FROM garden_plots WHERE user_id = ? AND crop_id IS NOT NULL",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    # Уровень грядки не меняется, пока на ней что-то растёт (upgrade_plot
    # запрещает улучшение занятой грядки), так что текущий уровень —
    # тот же, что был в момент посадки, и его безопасно использовать тут.
    levels = await _get_plot_levels(user_id)

    achv_results = []
    collected_count = 0
    for row in rows:
        crop_id = row["crop_id"]
        grow_seconds = _effective_grow_seconds(crop_id, _plot_level(levels, row["plot_index"]))
        if now - row["planted_at"] >= grow_seconds:
            collected, _xp, _level_info, results = await _collect_plot_if_matches(
                user_id, row["plot_index"], crop_id, row["planted_at"]
            )
            if collected:
                collected_count += 1
                achv_results.extend(results)

    if collected_count >= GARDEN_PLOT_COUNT:
        import achives

        result = await achives.unlock(user_id, "garden_all_plots_ripe")
        if result:
            achv_results.append(result)

    return achv_results


# ==========================
#   АВТОСБОР И УВЕДОМЛЕНИЯ
# ==========================

_background_tasks: set[asyncio.Task] = set()


def _schedule_auto_harvest(
    bot: Bot, user_id: int, plot_index: int, crop_id: str, planted_at: float, lang: str, level: int = 1
) -> None:
    """Создаёт фоновую задачу: как только грядка созреет, урожай сам
    переместится в корзину, а игроку придёт уведомление. level — уровень
    грядки НА МОМЕНТ ПОСАДКИ (см. _effective_grow_seconds) — он же
    "запекается" в planted_at через _privilege_speedup_offset в
    plant_crop, дальше на время роста этой конкретной посадки не влияет,
    даже если игрок теоретически как-то изменит уровень грядки (upgrade_plot
    и так запрещает улучшать занятую грядку — см. её докстринг)."""
    grow_seconds = _effective_grow_seconds(crop_id, level)
    delay = max(0.0, grow_seconds - (time.time() - planted_at))

    task = asyncio.create_task(
        _auto_harvest_after_delay(bot, user_id, plot_index, crop_id, planted_at, lang, delay)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _auto_harvest_after_delay(
    bot: Bot,
    user_id: int,
    plot_index: int,
    crop_id: str,
    planted_at: float,
    lang: str,
    delay: float,
) -> None:
    if delay > 0:
        await asyncio.sleep(delay)

    collected, xp_gained, level_info, achv_results = await _collect_plot_if_matches(
        user_id, plot_index, crop_id, planted_at
    )
    if not collected:
        return  # уже собрано подстраховкой — повторное уведомление не нужно

    crop = CROPS[crop_id]
    t = TEXTS.get(lang, TEXTS["ru"])
    text = t["auto_harvested_notice"].format(
        emoji=crop["emoji"], name=crop["name"][lang], n=plot_index + 1, xp=xp_gained
    )
    if level_info and level_info["leveled_up"]:
        import prof

        text += "\n\n" + prof.level_up_notice(lang, level_info["new_level"])
    achievement_results = list(achv_results)
    if achievement_results:
        import achives

        for result in achievement_results:
            text += "\n\n" + achives.format_unlock_text(lang, result)
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logger.warning(
            "Не удалось отправить уведомление о сборе урожая пользователю %s",
            user_id,
            exc_info=True,
        )


async def reschedule_pending_harvests(bot: Bot) -> None:
    """Заново создаёт фоновые задачи автосбора для всех грядок, на которых
    что-то растёт. Вызывать один раз при старте бота (до start_polling) —
    иначе после перезапуска процесса уведомления по уже посаженным
    грядкам потеряются (сам урожай при этом не пропадёт — его всё равно
    подберёт подстраховка в _get_plots при следующем открытии сада)."""
    db = await database.get_db()
    async with db.execute(
        "SELECT user_id, plot_index, crop_id, planted_at, lang FROM garden_plots WHERE crop_id IS NOT NULL"
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        level = await _get_single_plot_level(row["user_id"], row["plot_index"])
        _schedule_auto_harvest(
            bot,
            row["user_id"],
            row["plot_index"],
            row["crop_id"],
            row["planted_at"],
            row["lang"] or "ru",
            level,
        )


async def take_from_basket(user_id: int, crop_id: str) -> bool:
    """Убирает 1 фрукт из корзины. Возвращает False, если фрукта не было.

    Раньше это было "SELECT count -> проверить -> UPDATE count-1" —
    отдельными запросами, с окном гонки между ними: два параллельных
    запроса (двойной тап "покормить"/"продать") могли оба прочитать
    count=1, оба пройти проверку и оба решить, что фрукт есть, хотя он
    один — дюп-эксплойт (два эффекта за один потраченный ресурс).
    Теперь проверка "хватает ли" и списание — один атомарный запрос:
    SQLite либо применит его целиком, либо (если хватает — WHERE не
    совпадёт) не применит вообще, третьего не дано."""
    db = await database.get_db()
    cursor = await db.execute(
        "UPDATE garden_inventory SET count = count - 1 WHERE user_id = ? AND crop_id = ? AND count >= 1",
        (user_id, crop_id),
    )
    await database.commit()
    return cursor.rowcount > 0


async def take_from_basket_bulk(user_id: int, crop_id: str, count: int) -> bool:
    """Убирает сразу count фруктов из корзины. Возвращает False, если
    фруктов не хватает — в этом случае корзина не трогается вообще
    (используется рынком: выставление лота / мгновенная продажа боту).
    Атомарный условный UPDATE — та же защита от дюпа, что и в
    take_from_basket (см. комментарий там)."""
    if count <= 0:
        return False
    db = await database.get_db()
    cursor = await db.execute(
        "UPDATE garden_inventory SET count = count - ? WHERE user_id = ? AND crop_id = ? AND count >= ?",
        (count, user_id, crop_id, count),
    )
    await database.commit()
    return cursor.rowcount > 0


async def add_to_basket(user_id: int, crop_id: str, count: int = 1) -> None:
    """Добавляет count фруктов в корзину (используется рынком: возврат
    товара при снятии лота с продажи и передача фрукта покупателю)."""
    if count <= 0:
        return
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO garden_inventory (user_id, crop_id, count) VALUES (?, ?, ?)
        ON CONFLICT (user_id, crop_id) DO UPDATE SET count = count + excluded.count
        """,
        (user_id, crop_id, count),
    )
    await database.commit()


# ==========================
#   АЧИВКИ САДА — ДОП. ТАБЛИЦЫ
# ==========================
# garden_harvest_counts — счётчики собранного по каждой культуре,
# заводится и используется в _bump_harvest_count (см. выше, в блоке
# сбора урожая). garden_achv_state — всё остальное состояние ачивок
# сада: стрик визитов (garden_streak_7/30) и счётчик продаж на рынке
# (garden_sell_50, см. record_market_sale ниже — вызывается из
# market.py). Обе таблицы заводятся лениво (IF NOT EXISTS), по
# аналогии с panda_notify_state в panda.py. ensure_achv_tables()
# вызывается один раз при старте бота, см. main.py: main().

async def ensure_achv_tables() -> None:
    db = await database.get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS garden_harvest_counts (
            user_id INTEGER NOT NULL,
            crop_id TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, crop_id)
        )
        """
    )
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS garden_achv_state (
            user_id INTEGER PRIMARY KEY,
            visit_streak_days INTEGER NOT NULL DEFAULT 0,
            last_visit_day INTEGER NOT NULL DEFAULT 0,
            market_sales_count INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Какие платные грядки (индекс >= GARDEN_BASE_PLOT_COUNT) игрок уже
    # открыл за монеты — см. PLOT_UNLOCK_COST/unlock_plot. Наличие
    # строки означает "открыта"; первые GARDEN_BASE_PLOT_COUNT грядок
    # тут не хранятся — они открыты у всех по умолчанию.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS garden_plot_unlocks (
            user_id INTEGER NOT NULL,
            plot_index INTEGER NOT NULL,
            PRIMARY KEY (user_id, plot_index)
        )
        """
    )
    # Уровни улучшения грядок (см. PLOT_UPGRADE_COST/upgrade_plot выше) —
    # отсутствие строки означает уровень 1 (не улучшалась).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS garden_plot_levels (
            user_id INTEGER NOT NULL,
            plot_index INTEGER NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, plot_index)
        )
        """
    )
    await database.commit()
    _register_progress_providers()


# ==========================
#   ПРОГРЕСС СЧЁТНЫХ АЧИВОК САДА
# ==========================
# По аналогии с panda.py (см. там же секцию "АЧИВКИ ПАНДЫ —
# СЧЁТЧИКИ/СТРИКИ") регистрируем в achives.PROGRESS_PROVIDERS готовые
# функции user_id -> текущее значение для СЧЁТНЫХ ачивок сада — тогда
# карточка ачивки в achives.py покажет реальные "X/Y" и процент вместо
# всегда бинарных 0%/100%. Разовым ачивкам сада (первая посадка,
# тройной урожай со всех грядок разом, ночной сбор, экзотика,
# мгновенная продажа, кормление из корзины) провайдер не нужен — это
# одноразовое условие, а не процесс, шкала там ничего не добавляет
# (см. achives.py: _achv_page_text — шкала рисуется только если для
# ачивки есть провайдер).

async def _get_harvest_counts(user_id: int) -> dict[str, int]:
    """crop_id -> сколько всего собрано (см. _bump_harvest_count), без
    изменения счётчика — только для чтения текущего прогресса."""
    db = await database.get_db()
    async with db.execute(
        "SELECT crop_id, count FROM garden_harvest_counts WHERE user_id = ?", (user_id,)
    ) as cursor:
        rows = await cursor.fetchall()
    return {r["crop_id"]: r["count"] for r in rows}


async def _progress_total_harvest(user_id: int) -> int:
    """Для garden_harvest_10/100/1000/10000 — суммарно собрано любых
    культур."""
    return sum((await _get_harvest_counts(user_id)).values())


async def _progress_one_crop_max(user_id: int) -> int:
    """Для garden_one_crop_20 — сколько собрано САМОЙ частой из культур
    (условие ачивки — "одна и та же культура 20 раз", неважно какая)."""
    counts = await _get_harvest_counts(user_id)
    return max(counts.values(), default=0)


async def _progress_bamboo(user_id: int) -> int:
    """Для garden_bamboo_100 — собрано именно бамбука."""
    return (await _get_harvest_counts(user_id)).get("bamboo", 0)


async def _progress_distinct_crops(user_id: int) -> int:
    """Для garden_all_crops — сколько РАЗНЫХ культур уже собирались хотя
    бы по разу."""
    return len(await _get_harvest_counts(user_id))


async def _progress_basket_total(user_id: int) -> int:
    """Для garden_basket_50 — сколько фруктов сейчас лежит в корзине
    одновременно (в отличие от _progress_total_harvest это не
    накопительный счётчик, а текущий остаток — кормление/продажа его
    уменьшают, поэтому прогресс может и падать, это ожидаемо)."""
    return sum((await get_inventory(user_id)).values())


async def _progress_plots_occupied(user_id: int) -> int:
    """Для garden_all_plots_full — сколько грядок занято прямо сейчас."""
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS cnt FROM garden_plots WHERE user_id = ? AND crop_id IS NOT NULL",
        (user_id,),
    ) as cursor:
        return (await cursor.fetchone())["cnt"]


async def _get_garden_achv_counters(user_id: int) -> aiosqlite.Row | None:
    """visit_streak_days/market_sales_count без изменения — None, если
    строки ещё нет (игрок ни разу не заходил в сад и не продавал на
    рынке)."""
    db = await database.get_db()
    async with db.execute(
        "SELECT visit_streak_days, market_sales_count FROM garden_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _progress_streak(user_id: int) -> int:
    """Для garden_streak_7/30 — текущий стрик визитов в сад."""
    row = await _get_garden_achv_counters(user_id)
    return row["visit_streak_days"] if row else 0


async def _progress_market_sales(user_id: int) -> int:
    """Для garden_sell_50 — сколько фруктов продано на рынке суммарно."""
    row = await _get_garden_achv_counters(user_id)
    return row["market_sales_count"] if row else 0


def _register_progress_providers() -> None:
    """Регистрирует провайдеры прогресса счётных ачивок сада в
    achives.PROGRESS_PROVIDERS. Вызывается один раз из
    ensure_achv_tables() при старте бота — импорт achives здесь
    локальный, чтобы не завести цикл импортов (см. докстринг модуля)."""
    import achives

    achives.PROGRESS_PROVIDERS.update(
        {
            "garden_harvest_10": (10, _progress_total_harvest),
            "garden_harvest_100": (100, _progress_total_harvest),
            "garden_harvest_1000": (1000, _progress_total_harvest),
            "garden_harvest_10000": (10000, _progress_total_harvest),
            "garden_one_crop_20": (20, _progress_one_crop_max),
            "garden_bamboo_100": (100, _progress_bamboo),
            "garden_all_crops": (len(CROPS), _progress_distinct_crops),
            "garden_basket_50": (50, _progress_basket_total),
            "garden_all_plots_full": (GARDEN_BASE_PLOT_COUNT, _progress_plots_occupied),
            "garden_streak_7": (7, _progress_streak),
            "garden_streak_30": (30, _progress_streak),
            "garden_sell_50": (50, _progress_market_sales),
        }
    )


async def _ensure_achv_state_row(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO garden_achv_state (user_id) VALUES (?)", (user_id,)
    )


async def _record_garden_visit(user_id: int) -> list[str]:
    """Засчитывает сегодняшний (реальный) день как визит в сад — вызывать
    из open_garden (явное открытие раздела). Тот же день повторно стрик
    не двигает; пропуск дня сбрасывает стрик до 1. Возвращает достигнутые
    ачивки стрика (garden_streak_7/30)."""
    db = await database.get_db()
    now = time.time()
    today = int(now // 86400)

    await _ensure_achv_state_row(db, user_id)
    async with db.execute(
        "SELECT visit_streak_days, last_visit_day FROM garden_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        state = await cursor.fetchone()

    if state["last_visit_day"] == today:
        return []  # сегодня уже засчитано

    streak = state["visit_streak_days"] + 1 if state["last_visit_day"] == today - 1 else 1
    await db.execute(
        "UPDATE garden_achv_state SET visit_streak_days = ?, last_visit_day = ? WHERE user_id = ?",
        (streak, today, user_id),
    )
    await database.commit()

    if streak >= 30:
        return ["garden_streak_7", "garden_streak_30"]
    if streak >= 7:
        return ["garden_streak_7"]
    return []


async def record_market_sale(user_id: int, count: int = 1) -> list[dict]:
    """Вызывать из market.py в момент, когда лот игрока РЕАЛЬНО продан
    другому игроку (не при выставлении лота на продажу!). Увеличивает
    накопительный счётчик проданных на рынке фруктов и возвращает
    список результатов achives.unlock() для "Огородного бизнеса"
    (garden_sell_50) — пустой список, если порог ещё не достигнут или
    ачивка уже была выдана раньше."""
    import achives

    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id)
    await db.execute(
        "UPDATE garden_achv_state SET market_sales_count = market_sales_count + ? WHERE user_id = ?",
        (count, user_id),
    )
    await database.commit()
    async with db.execute(
        "SELECT market_sales_count FROM garden_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        total = (await cursor.fetchone())["market_sales_count"]

    if total < 50:
        return []
    result = await achives.unlock(user_id, "garden_sell_50")
    return [result] if result else []


async def record_instant_sell(user_id: int) -> dict | None:
    """Вызывать из market.py в момент мгновенной продажи фрукта боту
    (минуя выставление лота) — выдаёт "Быстрая сделка" (garden_instant_sell)
    с первого раза, идемпотентно (achives.unlock сам не выдаёт повторно)."""
    import achives

    return await achives.unlock(user_id, "garden_instant_sell")


# ==========================
#   ОТРИСОВКА КАРТОЧКИ САДА
# ==========================

def _build_garden_view(
    lang: str,
    plots: list[aiosqlite.Row],
    inventory: dict[str, int],
    page: int,
    unlocked_extra: set[int],
    levels: dict[int, int],
) -> tuple[str, object]:
    t = TEXTS[lang]
    now = time.time()

    total_pages = (GARDEN_PLOT_COUNT + PLOTS_PER_PAGE - 1) // PLOTS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * PLOTS_PER_PAGE
    page_plots = plots[start:start + PLOTS_PER_PAGE]

    title = t["title"]
    if total_pages > 1:
        title += t["title_page_suffix"].format(page=page + 1, total=total_pages)
    lines = [title, t["separator"]]

    builder = InlineKeyboardBuilder()
    row_sizes = []

    for plot in page_plots:
        plot_index = plot["plot_index"]
        crop_id = plot["crop_id"]

        if not _is_plot_unlocked(plot_index, unlocked_extra):
            # Платная грядка, ещё не открытая — вместо посадки/роста
            # показываем цену открытия (см. PLOT_UNLOCK_COST/unlock_plot).
            lines.append(t["plot_locked_line"])
            lines.append("")
            builder.button(
                text=t["unlock_button"].format(cost=PLOT_UNLOCK_COST[plot_index]),
                callback_data=f"garden:unlock:{plot_index}",
                style="primary",
                icon_custom_emoji_id=PLOT_LOCK_EMOJI_ID,
            )
            row_sizes.append(1)
            continue

        level = _plot_level(levels, plot_index)
        # Кнопка "Улучшить" показывается парой с основной кнопкой грядки
        # (посадить/рост), пока грядка не достигла максимального уровня —
        # см. PLOT_UPGRADE_MAX_LEVEL/upgrade_plot.
        can_upgrade = level < PLOT_UPGRADE_MAX_LEVEL

        if crop_id is None:
            # Пустая грядка ничем не описывается в тексте — только кнопка
            # (плюс кнопка улучшения), чтобы что-то на ней посадить.
            if level > 1:
                lines.append(
                    t["plot_level_line"].format(level=level, max_level=PLOT_UPGRADE_MAX_LEVEL)
                )
                lines.append("")
            builder.button(
                text=t["plant_button"],
                callback_data=f"garden:choose:{plot_index}",
                style="primary",
            )
            if can_upgrade:
                builder.button(
                    text=t["upgrade_button"].format(cost=PLOT_UPGRADE_COST[level + 1]),
                    callback_data=f"garden:upgrade:{plot_index}",
                    style="primary",
                )
                row_sizes.append(2)
            else:
                row_sizes.append(1)
            continue

        # Сюда попадают только ещё растущие грядки — всё созревшее уже
        # тихо переложено в корзину подстраховкой в _get_plots.
        crop = CROPS[crop_id]
        elapsed = now - plot["planted_at"]
        grow_seconds = _effective_grow_seconds(crop_id, level)
        percent = round(elapsed / grow_seconds * 100)
        remaining = grow_seconds - elapsed

        lines.append(
            t["plot_growing"].format(
                emoji=crop["emoji"],
                name=crop["name"][lang],
                percent=percent,
                time=_format_duration(remaining, lang),
            )
        )
        if level > 1:
            lines.append(
                t["plot_level_line"].format(level=level, max_level=PLOT_UPGRADE_MAX_LEVEL)
            )
        builder.button(
            text=t["plot_button_growing"].format(emoji=crop["emoji"], percent=percent),
            callback_data=f"garden:info:{plot_index}",
            style="primary",
        )
        if can_upgrade:
            builder.button(
                text=t["upgrade_button"].format(cost=PLOT_UPGRADE_COST[level + 1]),
                callback_data=f"garden:upgrade:{plot_index}",
                style="primary",
            )
            row_sizes.append(2)
        else:
            row_sizes.append(1)

        lines.append("")

    # Навигация по страницам грядок — показывается, только если страниц
    # больше одной (см. bakery._build_recipe_choice — тот же паттерн).
    nav_count = 0
    if page > 0:
        builder.button(
            text=t["page_prev_button"],
            callback_data=f"garden:page:{page - 1}",
            style="primary",
            icon_custom_emoji_id=PAGE_PREV_EMOJI_ID,
        )
        nav_count += 1
    if page < total_pages - 1:
        builder.button(
            text=t["page_next_button"],
            callback_data=f"garden:page:{page + 1}",
            style="primary",
            icon_custom_emoji_id=PAGE_NEXT_EMOJI_ID,
        )
        nav_count += 1
    if nav_count:
        row_sizes.append(nav_count)

    lines.append(t["basket_title"])
    if inventory:
        basket_line = "  ".join(
            t["basket_item"].format(emoji=CROPS[cid]["emoji"], count=count)
            for cid in CROP_ORDER
            if (count := inventory.get(cid, 0)) > 0
        )
        lines.append(basket_line)
    else:
        lines.append(t["basket_empty"])

    text = "\n".join(lines).rstrip()

    builder.adjust(*row_sizes)
    return text, builder.as_markup()


def _build_crop_choice(lang: str, plot_index: int, level: int) -> tuple[str, object]:
    t = TEXTS[lang]
    text = t["choose_crop_title"]

    # Кнопка "Назад" должна вернуть на ту же страницу грядок, с которой
    # была открыта эта грядка — вычисляем её из самого plot_index, не
    # прокидывая page отдельным параметром через весь путь вызовов.
    origin_page = plot_index // PLOTS_PER_PAGE

    builder = InlineKeyboardBuilder()
    for cid in CROP_ORDER:
        crop = CROPS[cid]
        builder.button(
            text=t["crop_button"].format(
                emoji=crop["emoji"],
                name=crop["name"][lang],
                # Время уже с поправкой на уровень этой грядки (см.
                # _effective_grow_seconds) — чтобы игрок видел реальное
                # время ДО посадки, а не базовое время культуры.
                time=_format_duration(_effective_grow_seconds(cid, level), lang),
            ),
            callback_data=f"garden:plant:{plot_index}:{cid}",
            style="primary",
        )
    builder.button(text=t["back_button"], callback_data=f"garden:back:{origin_page}", style="primary")
    builder.adjust(1)
    return text, builder.as_markup()


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


async def _render_and_send(message_or_callback, lang: str, edit: bool = False, page: int = 0) -> None:
    user_id = (
        message_or_callback.from_user.id
        if isinstance(message_or_callback, Message)
        else message_or_callback.from_user.id
    )
    plots, achv_results = await _get_plots(user_id)
    inventory = await get_inventory(user_id)
    unlocked_extra = await _get_unlocked_extra_plots(user_id)
    levels = await _get_plot_levels(user_id)
    text, markup = _build_garden_view(lang, plots, inventory, page, unlocked_extra, levels)

    # Картинка раздела (см. admin.py: admin:sections, ключ "garden") —
    # если задана, экран сада отправляется/правится как фото с текстом
    # в подписи (send_with_section_image/smart_edit), иначе как обычно
    # текстом. Локальный импорт — admin.py сам импортирует garden.py на
    # верхнем уровне (цикл).
    import admin

    if edit:
        await admin.smart_edit(message_or_callback.message, text, reply_markup=markup)
    else:
        await admin.send_with_section_image(message_or_callback, "garden", text, reply_markup=markup)

    if achv_results:
        import achives

        notify_target = (
            message_or_callback if isinstance(message_or_callback, Message) else message_or_callback.message
        )
        for result in achv_results:
            await notify_target.answer(achives.format_unlock_text(lang, result))


# ==========================
#   ХЕНДЛЕРЫ
# ==========================

@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_garden(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    await _render_and_send(message, lang, edit=False)

    # Ачивки стрика визитов ("Неделя в саду"/"Месяц в саду") — только
    # на явное открытие раздела из реплай-меню, не на каждый "Назад".
    streak_achv_ids = await _record_garden_visit(message.from_user.id)
    if streak_achv_ids:
        import achives

        for achv_id in streak_achv_ids:
            result = await achives.unlock(message.from_user.id, achv_id)
            if result:
                await message.answer(achives.format_unlock_text(lang, result))


@router.callback_query(F.data.startswith("garden:choose:"))
async def on_choose_crop(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    plot_index = int(callback.data.split(":")[2])

    # Подстраховка от протухшей клавиатуры: если грядка платная и ещё
    # не открыта, сажать на ней нельзя — экран посадки не открываем.
    unlocked_extra = await _get_unlocked_extra_plots(callback.from_user.id)
    if not _is_plot_unlocked(plot_index, unlocked_extra):
        await callback.answer()
        return

    level = await _get_single_plot_level(callback.from_user.id, plot_index)
    text, markup = _build_crop_choice(lang, plot_index, level)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("garden:back:"))
async def on_back_to_garden(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    page = int(callback.data.split(":")[2])
    await _render_and_send(callback, lang, edit=True, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("garden:page:"))
async def on_garden_page(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    page = int(callback.data.split(":")[2])
    await _render_and_send(callback, lang, edit=True, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("garden:unlock:"))
async def on_unlock_plot(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    plot_index = int(callback.data.split(":")[2])

    status = await unlock_plot(callback.from_user.id, plot_index)
    if status == "not_enough":
        await callback.answer(t["unlock_not_enough_toast"], show_alert=True)
        return
    if status == "already":
        await callback.answer(t["unlock_already_toast"])
    elif status == "ok":
        await callback.answer(t["unlocked_toast"], show_alert=True)
    else:
        await callback.answer()
        return

    await _render_and_send(callback, lang, edit=True, page=plot_index // PLOTS_PER_PAGE)


@router.callback_query(F.data.startswith("garden:upgrade:"))
async def on_upgrade_plot(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    plot_index = int(callback.data.split(":")[2])

    # Подстраховка от протухшей клавиатуры — см. on_choose_crop.
    unlocked_extra = await _get_unlocked_extra_plots(callback.from_user.id)
    if not _is_plot_unlocked(plot_index, unlocked_extra):
        await callback.answer()
        return

    status = await upgrade_plot(callback.from_user.id, plot_index)
    if status == "not_enough":
        await callback.answer(t["upgrade_not_enough_toast"], show_alert=True)
        return
    if status == "busy":
        await callback.answer(t["upgrade_busy_toast"], show_alert=True)
        return
    if status == "max_level":
        await callback.answer(t["upgrade_max_toast"], show_alert=True)
        return

    new_level = await _get_single_plot_level(callback.from_user.id, plot_index)
    await callback.answer(t["upgrade_done_toast"].format(level=new_level), show_alert=True)
    await _render_and_send(callback, lang, edit=True, page=plot_index // PLOTS_PER_PAGE)


@router.callback_query(F.data.startswith("garden:plant:"))
async def on_plant(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    _, _, plot_index_str, crop_id = callback.data.split(":")
    plot_index = int(plot_index_str)
    crop = CROPS[crop_id]

    # Подстраховка от протухшей клавиатуры — см. on_choose_crop.
    unlocked_extra = await _get_unlocked_extra_plots(callback.from_user.id)
    if not _is_plot_unlocked(plot_index, unlocked_extra):
        await callback.answer()
        return

    level = await _get_single_plot_level(callback.from_user.id, plot_index)

    planted_at = await plant_crop(callback.from_user.id, plot_index, crop_id, lang)
    if planted_at is None:
        await callback.answer(t["plot_taken_toast"], show_alert=True)
        return

    # Как только грядка созреет — фоновая задача сама переложит урожай
    # в корзину и пришлёт уведомление, ничего собирать вручную не нужно.
    _schedule_auto_harvest(
        callback.bot, callback.from_user.id, plot_index, crop_id, planted_at, lang, level
    )

    grow_seconds = _effective_grow_seconds(crop_id, level)
    await callback.answer(
        t["planted_toast"].format(
            emoji=crop["emoji"],
            name=crop["name"][lang],
            time=_format_duration(max(0.0, grow_seconds - (time.time() - planted_at)), lang),
        ),
        show_alert=True,
    )
    await _render_and_send(callback, lang, edit=True, page=plot_index // PLOTS_PER_PAGE)

    # Ачивки "Первая посадка"/"Все грядки заняты" — за сам факт посадки.
    import achives

    for result in await _plant_achievements(callback.from_user.id):
        await callback.message.answer(achives.format_unlock_text(lang, result))


@router.callback_query(F.data.startswith("garden:info:"))
async def on_plot_info(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    plot_index = int(callback.data.split(":")[2])
    plots, achv_results = await _get_plots(callback.from_user.id)

    if achv_results:
        import achives

        for result in achv_results:
            await callback.message.answer(achives.format_unlock_text(lang, result))

    plot = next((p for p in plots if p["plot_index"] == plot_index), None)

    if plot is None or plot["crop_id"] is None:
        # Либо грядка пуста, либо только что созрела и была тихо собрана
        # подстраховкой в _get_plots — в обоих случаях показывать нечего.
        await callback.answer()
        return

    crop = CROPS[plot["crop_id"]]
    level = await _get_single_plot_level(callback.from_user.id, plot_index)
    now = time.time()
    elapsed = now - plot["planted_at"]
    grow_seconds = _effective_grow_seconds(plot["crop_id"], level)
    percent = round(elapsed / grow_seconds * 100)
    remaining = grow_seconds - elapsed

    await callback.answer(
        t["info_alert"].format(
            emoji=crop["emoji"],
            name=crop["name"][lang],
            bar=_render_bar(percent),
            percent=percent,
            time=_format_duration(remaining, lang),
        ),
        show_alert=True,
    )
