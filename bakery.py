"""
Раздел "Пекарня".

Идея:
    Игрок печёт торты и выпечку в печах (аналог грядок в саду). Каждый
    рецепт требует:
      1. Немного фруктов из корзины сада (garden_inventory) — их нельзя
         купить, только вырастить в саду.
      2. Немного "сухих" ингредиентов (мука, сахар, масло, яйца,
         молоко, шоколад) — их, наоборот, НЕЛЬЗЯ вырастить, только
         купить в лавке пекарни за Pn (та же валюта, что и на рынке —
         см. shop.py).

    Выпечка, как и урожай в саду, готовится реальное время. Как только
    она готова — печь автоматически перекладывает готовое изделие на
    витрину (bakery_pantry) и присылает игроку уведомление, вручную
    ничего забирать не нужно (та же схема, что в garden.py: фоновая
    asyncio-задача + подстраховка при каждом обращении к печам).

    С витрины готовую выпечку можно продать на рынке (см. shop.py,
    лоты и мгновенная продажа боту) либо скормить панде — но кормление
    теперь делается только из раздела "Моя панда" (panda.py), сама
    пекарня панду напрямую больше не кормит. Выпечка восполняет голод
    сильнее одного фрукта (см. RECIPES: hunger_restore_min/_max,
    roll_hunger_restore — используется panda.py).

Лавка пекарни:
    Отдельный подраздел "Пекарня" → "Лавка ингредиентов". Там за Pn
    продаются ТОЛЬКО ингредиенты (INGREDIENTS) — мука, сахар, масло,
    яйца, молоко, шоколад. Фруктов там нет и не будет: фрукты — только
    из сада.

Зависимости от других модулей:
    - garden.py: CROPS (эмодзи/названия фруктов), get_inventory,
      take_from_basket_bulk — пекарня ничего не знает о механике
      выращивания, только читает и списывает готовые фрукты из корзины.
    - shop.py: get_balance, charge_balance — общая валюта Pn, платёж за
      ингредиенты списывается точно так же, как и любая другая
      Pn-операция в боте. С обратной стороны shop.py тоже зависит от
      bakery.py (get_pantry/add_to_pantry/take_from_pantry_bulk,
      RECIPES/RECIPE_ORDER) — выпечку теперь можно выставлять на рынок
      как лот наравне с фруктами.
    - panda.py: get_pantry/take_from_pantry/roll_hunger_restore — панда
      сама читает витрину пекарни и кормится из неё напрямую, минуя
      этот модуль (см. комментарий в panda.py). restore_hunger при этом
      по-прежнему живёт в panda.py, тем же способом, что и фрукты сада.

Хранение:
    Общая база данных бота (см. database.py) — единое asyncio-соединение,
    WAL-режим, батч-коммиты, кроме операций с переходом Pn (списание за
    ингредиенты) — они, как и в shop.py, сохраняются на диск немедленно.
    Гонки между параллельными запросами одного игрока закрыты личным
    локом — database.user_lock(user_id).

Подключение в main.py:
    import bakery
    dp.include_router(bakery.router)   # после garden.router и shop.router

    # до первого обращения к разделу "Пекарня" — создаёт таблицы
    # счётчиков ачивок пекарни (см. секцию "АЧИВКИ ПЕКАРНИ" ниже) и
    # регистрирует PROGRESS_PROVIDERS:
    await bakery.ensure_achv_tables()

    # до start_polling — иначе после рестарта потеряются уведомления
    # о готовности выпечки, поставленной в печь до перезапуска:
    await bakery.reschedule_pending_bakes(bot)

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import asyncio
import logging
import random
import time

import aiosqlite
from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import garden
import shop

logger = logging.getLogger(__name__)

router = Router(name="bakery")


# ==========================
#   СОСТОЯНИЯ (FSM)
# ==========================

class BakeryStates(StatesGroup):
    waiting_quantity = State()


# ==========================
#   НАСТРОЙКИ
# ==========================

BAKERY_OVEN_COUNT = 4

# Сколько печей доступно бесплатно с самого начала (первая страница
# целиком) — остальные (BAKERY_BASE_OVEN_COUNT..BAKERY_OVEN_COUNT-1)
# платные, см. OVEN_UNLOCK_COST/unlock_oven ниже. Ачивка "Обе печи в
# деле" (bakery_both_ovens) намеренно привязана именно к этому базовому
# числу — см. _check_bakery_achievements, — иначе с добавлением платных
# печей она стала бы куда сложнее задуманного.
BAKERY_BASE_OVEN_COUNT = 2

# Пагинация печей на экране пекарни — по OVENS_PER_PAGE штук на страницу.
OVENS_PER_PAGE = 2

# Стоимость открытия дополнительных печей (индекс печи -> цена в Pn).
OVEN_UNLOCK_COST = {
    2: 15000,
    3: 85000,
}


# ==========================
#   УЛУЧШЕНИЕ ПЕЧЕЙ
# ==========================
# По аналогии с улучшением грядок в garden.py (см. там же докстринг
# раздела "УЛУЧШЕНИЕ ГРЯДОК" — тот же принцип один в один): у каждой
# печи есть уровень от 1 до OVEN_UPGRADE_MAX_LEVEL, хранится в
# bakery_oven_levels (заводится лениво в ensure_achv_tables ниже),
# отсутствие строки = уровень 1. Каждый уровень сверх первого сокращает
# время выпечки ЛЮБОГО рецепта именно в этой печи — линейно, поровну на
# уровень, так что к 10 уровню суммарное ускорение ровно
# OVEN_UPGRADE_MAX_SPEEDUP (см. _oven_time_factor). Улучшать можно
# только пустую печь — та же причина, что и у грядок (не пересчитывать
# задним числом уже тикающий таймер и не переставлять фоновую задачу).
OVEN_UPGRADE_MAX_LEVEL = 10

# Максимальное ускорение на 10 уровне: время выпечки сокращается не
# более чем в 4 раза (т.е. становится 25% от базового).
OVEN_UPGRADE_MAX_SPEEDUP = 4.0

# Стоимость перехода НА уровень N (ключ — целевой уровень 2..10) в Pn.
# Геометрическая прогрессия от 5000 (2 уровень) до 250000 (10 уровень).
OVEN_UPGRADE_COST = {
    2: 5000,
    3: 8200,
    4: 13300,
    5: 21700,
    6: 35400,
    7: 57700,
    8: 94000,
    9: 153300,
    10: 250000,
}


def _oven_time_factor(level: int) -> float:
    """Множитель к времени выпечки для уровня печи level: 1.0 на уровне 1
    (без ускорения), линейно убывает до 1/OVEN_UPGRADE_MAX_SPEEDUP на
    уровне OVEN_UPGRADE_MAX_LEVEL (см. докстринг раздела выше)."""
    level = max(1, min(OVEN_UPGRADE_MAX_LEVEL, level))
    min_factor = 1 / OVEN_UPGRADE_MAX_SPEEDUP
    return 1 - (level - 1) * (1 - min_factor) / (OVEN_UPGRADE_MAX_LEVEL - 1)


def _oven_speedup_percent(level: int) -> int:
    """На сколько % сократилось время выпечки на уровне level относительно
    базового (уровень 1) — для тоста после улучшения (см. on_upgrade_oven)."""
    return round((1 - _oven_time_factor(level)) * 100)


def _effective_bake_seconds(recipe_id: str, level: int) -> float:
    """Время выпечки recipe_id в печи уровня level, с учётом ускорения
    от уровня печи (см. _oven_time_factor)."""
    return RECIPES[recipe_id]["bake_seconds"] * _oven_time_factor(level)


BAR_LENGTH = 10
BAR_FILLED = "▰"
BAR_EMPTY = "▱"

QUICK_QUANTITIES = [1, 5, 10]
MAX_BUY_QUANTITY = 999


def _render_bar(percent: float) -> str:
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * BAR_LENGTH)
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_LENGTH - filled)


# ==========================
#   ИНГРЕДИЕНТЫ (покупаются в лавке пекарни за Pn — фруктов тут нет)
# ==========================

INGREDIENTS = {
    "flour": {
        "emoji": "🌾",
        "price": 150,
        "name": {"ru": "Мука", "en": "Flour"},
    },
    "sugar": {
        "emoji": "🍬",
        "price": 180,
        "name": {"ru": "Сахар", "en": "Sugar"},
    },
    "butter": {
        "emoji": "🧈",
        "price": 250,
        "name": {"ru": "Масло", "en": "Butter"},
    },
    "egg": {
        "emoji": "🥚",
        "price": 120,
        "name": {"ru": "Яйца", "en": "Eggs"},
    },
    "milk": {
        "emoji": "🥛",
        "price": 160,
        "name": {"ru": "Молоко", "en": "Milk"},
    },
    "chocolate": {
        "emoji": "🍫",
        "price": 350,
        "name": {"ru": "Шоколад", "en": "Chocolate"},
    },
}

INGREDIENT_ORDER = ["flour", "sugar", "butter", "egg", "milk", "chocolate"]


# ==========================
#   РЕЦЕПТЫ
# ==========================
# ingredients — сколько купленных ингредиентов нужно (INGREDIENTS)
# fruits — сколько фруктов из корзины сада нужно (garden.CROPS)
# bake_seconds — время выпечки в реальных секундах
# hunger_restore_min / _max — диапазон восполнения голода панды при
# кормлении этим изделием (та же механика "броска", что и у фруктов —
# см. garden.roll_hunger_restore)

RECIPES = {
    "tangerine_muffin": {
        "emoji": "🧁",
        "ingredients": {"flour": 2, "sugar": 1, "egg": 1},
        "fruits": {"tangerine": 2},
        "bake_seconds": 8 * 60,
        "hunger_restore_min": 12,
        "hunger_restore_max": 20,
        "name": {"ru": "Мандариновый маффин", "en": "Tangerine muffin"},
        "flavor": {
            "ru": "Лёгкий и цитрусовый — маленькая радость на один укус.",
            "en": "Light and citrusy — a small joy in a single bite.",
        },
    },
    "apple_pie": {
        "emoji": "🥧",
        "ingredients": {"flour": 3, "sugar": 1, "butter": 1},
        "fruits": {"apple": 2},
        "bake_seconds": 12 * 60,
        "hunger_restore_min": 15,
        "hunger_restore_max": 24,
        "name": {"ru": "Яблочный пирог", "en": "Apple pie"},
        "flavor": {
            "ru": "Хрустящая корочка и мягкая начинка — классика, которая не подводит.",
            "en": "Crisp crust, soft filling — a classic that never disappoints.",
        },
    },
    "pear_tart": {
        "emoji": "🍰",
        "ingredients": {"flour": 2, "sugar": 1, "egg": 1, "butter": 1},
        "fruits": {"pear": 2},
        "bake_seconds": 14 * 60,
        "hunger_restore_min": 16,
        "hunger_restore_max": 25,
        "name": {"ru": "Грушевый тарт", "en": "Pear tart"},
        "flavor": {
            "ru": "Нежное тесто и тающие ломтики груши — тарт для неспешного чаепития.",
            "en": "Delicate pastry and melting pear slices — a tart for a slow afternoon.",
        },
    },
    "grape_cupcake": {
        "emoji": "🎂",
        "ingredients": {"flour": 2, "egg": 2, "milk": 1},
        "fruits": {"grape": 3},
        "bake_seconds": 16 * 60,
        "hunger_restore_min": 17,
        "hunger_restore_max": 26,
        "name": {"ru": "Виноградный кекс", "en": "Grape cupcake"},
        "flavor": {
            "ru": "Влажный мякиш с ягодами винограда внутри — простое домашнее лакомство.",
            "en": "A moist crumb studded with grapes — a simple homemade treat.",
        },
    },
    "banana_bread": {
        "emoji": "🍞",
        "ingredients": {"flour": 3, "egg": 1, "butter": 1},
        "fruits": {"banana": 2},
        "bake_seconds": 18 * 60,
        "hunger_restore_min": 18,
        "hunger_restore_max": 28,
        "name": {"ru": "Банановый хлеб", "en": "Banana bread"},
        "flavor": {
            "ru": "Плотный, сытный, с насыщенным банановым ароматом.",
            "en": "Dense, filling, with a deep banana aroma.",
        },
    },
    "mango_cheesecake": {
        "emoji": "🍮",
        "ingredients": {"flour": 2, "sugar": 2, "egg": 2, "milk": 1},
        "fruits": {"mango": 2},
        "bake_seconds": 22 * 60,
        "hunger_restore_min": 20,
        "hunger_restore_max": 30,
        "name": {"ru": "Манговый чизкейк", "en": "Mango cheesecake"},
        "flavor": {
            "ru": "Кремовый, тропический, с ярким манговым акцентом сверху.",
            "en": "Creamy and tropical, finished with a bright mango layer.",
        },
    },
    "pineapple_gateau": {
        "emoji": "🎂",
        "ingredients": {"flour": 3, "sugar": 2, "butter": 2, "chocolate": 1},
        "fruits": {"pineapple": 2},
        "bake_seconds": 28 * 60,
        "hunger_restore_min": 24,
        "hunger_restore_max": 34,
        "name": {"ru": "Ананасовый торт", "en": "Pineapple gateau"},
        "flavor": {
            "ru": "Главное блюдо витрины: сладко-кислый ананас и шоколад в каждом слое.",
            "en": "The showcase's centerpiece: sweet-tart pineapple and chocolate in every layer.",
        },
    },
    # --- страница 2 ---
    "apple_crumble": {
        "emoji": "🍪",
        "ingredients": {"flour": 2, "butter": 2, "sugar": 1},
        "fruits": {"apple": 3},
        "bake_seconds": 10 * 60,
        "hunger_restore_min": 13,
        "hunger_restore_max": 21,
        "name": {"ru": "Яблочный крамбл", "en": "Apple crumble"},
        "flavor": {
            "ru": "Хрустящая масляная крошка поверх мягких печёных яблок.",
            "en": "A crunchy buttery topping over soft baked apples.",
        },
    },
    "grape_banana_waffle": {
        "emoji": "🧇",
        "ingredients": {"flour": 3, "egg": 1, "milk": 1},
        "fruits": {"grape": 2, "banana": 1},
        "bake_seconds": 20 * 60,
        "hunger_restore_min": 19,
        "hunger_restore_max": 27,
        "name": {"ru": "Виноградно-банановая вафля", "en": "Grape-banana waffle"},
        "flavor": {
            "ru": "Хрустящие вафли со сладким виноградом и бананом внутри.",
            "en": "Crisp waffles filled with sweet grapes and banana.",
        },
    },
    "mango_tangerine_tart": {
        "emoji": "🍡",
        "ingredients": {"flour": 2, "sugar": 2, "butter": 1, "egg": 1},
        "fruits": {"mango": 1, "tangerine": 2},
        "bake_seconds": 19 * 60,
        "hunger_restore_min": 18,
        "hunger_restore_max": 27,
        "name": {"ru": "Манго-мандариновый тарт", "en": "Mango-tangerine tart"},
        "flavor": {
            "ru": "Тропическое манго встречается с яркой цитрусовой ноткой мандарина.",
            "en": "Tropical mango meets a bright citrus note of tangerine.",
        },
    },
    "pear_chocolate_brownie": {
        "emoji": "🍫",
        "ingredients": {"flour": 2, "sugar": 1, "butter": 1, "chocolate": 2},
        "fruits": {"pear": 2},
        "bake_seconds": 24 * 60,
        "hunger_restore_min": 21,
        "hunger_restore_max": 30,
        "name": {"ru": "Грушевый шоколадный брауни", "en": "Pear chocolate brownie"},
        "flavor": {
            "ru": "Насыщенный шоколадный брауни с сочными кусочками груши.",
            "en": "Rich chocolate brownie studded with juicy pear.",
        },
    },
    "banana_pineapple_muffin": {
        "emoji": "🍩",
        "ingredients": {"flour": 2, "sugar": 1, "egg": 1, "milk": 1},
        "fruits": {"banana": 1, "pineapple": 1},
        "bake_seconds": 17 * 60,
        "hunger_restore_min": 17,
        "hunger_restore_max": 25,
        "name": {"ru": "Банана-ананасовый маффин", "en": "Banana-pineapple muffin"},
        "flavor": {
            "ru": "Тропический дуэт банана и ананаса в мягком маффине.",
            "en": "A tropical banana-and-pineapple duo in a soft muffin.",
        },
    },
    # --- сложные рецепты (требуют всех/почти всех ингредиентов и нескольких фруктов) ---
    "triple_fruit_gateau": {
        "emoji": "🥮",
        "ingredients": {"flour": 4, "sugar": 3, "butter": 2, "egg": 2, "milk": 1, "chocolate": 1},
        "fruits": {"apple": 2, "pear": 2, "grape": 2},
        "bake_seconds": 35 * 60,
        "hunger_restore_min": 30,
        "hunger_restore_max": 42,
        "name": {"ru": "Тройной фруктовый гато", "en": "Triple fruit gateau"},
        "flavor": {
            "ru": "Многослойный десерт из яблока, груши и винограда — уходит почти вся лавка ингредиентов.",
            "en": "A multi-layered dessert of apple, pear, and grape — it uses almost every ingredient in the shop.",
        },
    },
    "royal_tropical_cake": {
        "emoji": "👑",
        "ingredients": {"flour": 4, "sugar": 3, "butter": 2, "egg": 2, "milk": 2, "chocolate": 2},
        "fruits": {"mango": 2, "pineapple": 2, "tangerine": 2},
        "bake_seconds": 45 * 60,
        "hunger_restore_min": 34,
        "hunger_restore_max": 48,
        "name": {"ru": "Королевский тропический торт", "en": "Royal tropical cake"},
        "flavor": {
            "ru": "Вершина мастерства пекарни: манго, ананас и мандарин под шоколадной глазурью. Требует всех ингредиентов лавки.",
            "en": "The bakery's masterpiece: mango, pineapple, and tangerine under a chocolate glaze. Calls for every ingredient in the shop.",
        },
    },
}

RECIPE_ORDER = [
    # --- страница 1 ---
    "tangerine_muffin",
    "apple_pie",
    "pear_tart",
    "grape_cupcake",
    "banana_bread",
    "mango_cheesecake",
    "pineapple_gateau",
    # --- страница 2 ---
    "apple_crumble",
    "grape_banana_waffle",
    "mango_tangerine_tart",
    "pear_chocolate_brownie",
    "banana_pineapple_muffin",
    "triple_fruit_gateau",
    "royal_tropical_cake",
]

RECIPES_PER_PAGE = 7


# ==========================
#   ЭКОНОМИКА ВЫПЕЧКИ (себестоимость и цены продажи)
# ==========================
# Себестоимость рецепта = стоимость купленных ингредиентов (INGREDIENTS)
# + рыночная стоимость потраченных фруктов (по средней точке диапазона
# shop.PRICE_RANGES для соответствующего фрукта — то есть фрукт учитывается
# по цене, за которую его можно было бы продать напрямую, не тратя на
# выпечку). Именно от этой себестоимости считаются:
#   - цена мгновенного выкупа боту (INSTANT_SELL_MARGIN) — фиксированная,
#     не зависит от текущих лотов на рынке (в отличие от фруктов, где
#     мгновенная продажа идёт через shop.instant_sell_unit_price по общей
#     формуле "-40% от средней цены лотов");
#   - допустимый диапазон цены при выставлении лота на рынок
#     (MARKET_MIN_MARGIN..MARKET_MAX_MARGIN).
# Числа сознательно не захардкожены в отдельную таблицу (как раньше
# shop.BAKERY_PRICE_RANGES) — они всегда пересчитываются от актуальных
# INGREDIENTS/RECIPES/shop.PRICE_RANGES, поэтому подорожание ингредиентов
# в лавке автоматически подтягивает и цену готовой выпечки, не давая
# рецептам снова уйти в минус.

INSTANT_SELL_MARGIN = 1.25   # выкуп ботом: +25% к себестоимости (внутри 20–30%)
MARKET_MIN_MARGIN = 1.20     # мин. цена лота на рынке: +20% к себестоимости
MARKET_MAX_MARGIN = 1.50     # макс. цена лота на рынке: +50% к себестоимости

PRICE_ROUND_STEP = 10        # округление итоговых цен до кратного 10


def _round_price(value: float, step: int = PRICE_ROUND_STEP) -> int:
    return max(1, int(round(value / step) * step))


def get_recipe_cost(recipe_id: str) -> int:
    """Себестоимость изделия: сумма цен купленных ингредиентов +
    рыночная стоимость фруктов (по средней точке shop.PRICE_RANGES)."""
    recipe = RECIPES[recipe_id]

    ingredients_cost = sum(
        INGREDIENTS[ingredient_id]["price"] * qty
        for ingredient_id, qty in recipe["ingredients"].items()
    )

    fruits_cost = 0.0
    for fruit_id, qty in recipe["fruits"].items():
        lo, hi = shop.PRICE_RANGES[fruit_id]
        fruits_cost += (lo + hi) / 2 * qty

    return round(ingredients_cost + fruits_cost)


def get_instant_sell_price(recipe_id: str) -> int:
    """Цена мгновенного выкупа боту — фикс. +25% к себестоимости,
    не зависит от рыночных лотов (используется вместо общей формулы
    shop.instant_sell_unit_price для item_type == ITEM_BAKERY)."""
    return _round_price(get_recipe_cost(recipe_id) * INSTANT_SELL_MARGIN)


def get_market_price_range(recipe_id: str) -> tuple[int, int]:
    """Допустимый диапазон цены за штуку при выставлении лота на рынок:
    от +20% до +50% к себестоимости."""
    cost = get_recipe_cost(recipe_id)
    lo = _round_price(cost * MARKET_MIN_MARGIN)
    hi = _round_price(cost * MARKET_MAX_MARGIN)
    return lo, hi


def roll_hunger_restore(recipe_id: str) -> int:
    """Каждый вызов — новый случайный % восполнения голода для этого
    изделия, в пределах его диапазона (hunger_restore_min..max)."""
    recipe = RECIPES[recipe_id]
    return random.randint(recipe["hunger_restore_min"], recipe["hunger_restore_max"])


# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

BUTTON_TEXT = {
    "ru": "🥐 Пекарня",
    "en": "🥐 Bakery",
}

# Кастомные премиум-эмодзи (tg://emoji?id=...) для инлайн-кнопок пекарни —
# по аналогии с main.py: вместо обычного юникод-эмодзи в тексте кнопки
# используется параметр icon_custom_emoji_id.
SHOP_BUTTON_EMOJI_ID = "6010183144450299916"   # 🏠 Лавка
BAKE_BUTTON_EMOJI_ID = "5424972470023104089"   # 🔥 Испечь
OVEN_LOCK_EMOJI_ID = "5296369303661067030"   # 🔒 Печь закрыта / открыть печь
COIN_BUTTON_EMOJI_ID = "5449418135381759397"   # 🪙 монета (цена в кнопках лавки)
QTY_CUSTOM_BUTTON_EMOJI_ID = "5370951118698339120"   # ✏️ Своё количество
BACK_BUTTON_EMOJI_ID = "6039539366177541657"   # ⬅️ Назад
PAGE_PREV_EMOJI_ID = "5255703720078879038"   # 🔙 Пред. (пагинация рецептов)
PAGE_NEXT_EMOJI_ID = "5253767677670862169"   # 🔜 След. (пагинация рецептов)
OVEN_UPGRADE_EMOJI_ID = "5449683594425410231"   # 🔼 Улучшить печь

# Для текста (не кнопки) кастомный эмодзи вставляется HTML-тегом
# <tg-emoji>, а не icon_custom_emoji_id — используется в заголовке лавки.
SHOP_TITLE_EMOJI = f'<tg-emoji emoji-id="{SHOP_BUTTON_EMOJI_ID}">🛒</tg-emoji>'

SHOWCASE_TITLE_EMOJI_ID = "5985762518750990451"
SHOWCASE_TITLE_EMOJI = f'<tg-emoji emoji-id="{SHOWCASE_TITLE_EMOJI_ID}">🍽</tg-emoji>'

TEXTS = {
    "ru": {
        "title": "🥐 <b>Пекарня</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "oven_baking": "{emoji} <b>{name}</b> — {percent}%\n<i>Готово через {time}</i>",
        "oven_button_baking": "{emoji} {percent}%",
        "oven_empty_button": "Испечь",
        "info_alert": "{emoji} {name}\n{bar} {percent}%\nГотово через {time}",
        "showcase_title": f"<b>{SHOWCASE_TITLE_EMOJI} Витрина:</b>",
        "showcase_empty": "<i>пока пусто — испеките что-нибудь</i>",
        "showcase_item": "{emoji} {name} ×{count}",
        "pantry_hint": "<i>Продайте на рынке или скормите панде в разделе «Моя панда».</i>",
        "shop_button": "Лавка ингредиентов",
        "back_button": "Назад",
        "choose_recipe_title": "🔥 <b>Что испечь?</b> <i>(стр. {page}/{total})</i>",
        "page_prev_button": "Пред.",
        "page_next_button": "След.",
        "recipe_line": "{emoji} <b>{name}</b> — <i>{time}</i>\n<i>{ingredients_line}</i>",
        "recipe_button": "{emoji} {name}",
        "ingredients_need_line": "Нужно: {items}",
        "baking_started_toast": "В печь отправлено: {emoji} {name}\nБудет готово через {time}",
        "oven_taken_toast": "В этой печи уже что-то готовится.",
        "not_enough_toast": "Не хватает ингредиентов или фруктов для этого рецепта.",
        "no_free_oven_toast": "Все печи заняты — дождитесь готовой выпечки.",
        "auto_baked_notice": "<i>{emoji} {name} готов(о) и уже на витрине! 🍽\nЗа это дали +{xp} XP</i>",
        "feed_redirect_toast": "🐼 Покормить панду теперь можно в разделе «Моя панда».",
        "time_min_sec": "{minutes} мин {seconds} сек",
        "time_sec": "{seconds} сек",
        # --- лавка ингредиентов ---
        "shop_title": f"{SHOP_TITLE_EMOJI} <b>Лавка ингредиентов</b>",
        "shop_hint": "<i>Фрукты здесь не продаются — их можно только вырастить в саду.</i>",
        "shop_balance_line": f"{shop.CE_BALANCE} <b>Баланс:</b> <b>{{balance}} {shop.CURRENCY}</b>",
        "shop_item_line": f"{{emoji}} <b>{{name}}</b> — <i>{shop.CURRENCY}{{price}}/шт · есть: {{count}}</i>",
        "buy_button": "{price} | {emoji} {name}",
        "qty_button": "×{qty} — {total}",
        "qty_custom_button": "Своё число",
        "choose_qty_title": f"{{emoji}} <b>{{name}}</b>\nСколько купить? Цена: {shop.CURRENCY}{{price}}/шт",
        "ask_qty": "{emoji} <b>{name}</b>\n<i>Введите количество (число):</i>",
        "qty_invalid": "Введите целое число от 1 до {max}.",
        "bought_toast": f"Куплено: {{emoji}} {{name}} ×{{count}} за {{total}} {shop.CURRENCY_PLAIN}",
        "not_enough_pn_toast": "Не хватает Pn для покупки.",
        # --- пагинация печей / открытие платных печей ---
        "title_page_suffix": " <i>(стр. {page}/{total})</i>",
        "oven_locked_line": f'<tg-emoji emoji-id="5296369303661067030">🔒</tg-emoji> <i>Печь закрыта</i>',
        "unlock_oven_button": "Открыть — {cost}",
        "unlocked_oven_toast": "🔥 Открыта новая печь!",
        "unlock_oven_not_enough_toast": f"Не хватает {shop.CURRENCY_PLAIN} для этой покупки.",
        "unlock_oven_already_toast": "Эта печь уже открыта.",
        # --- улучшение печей ---
        "upgrade_oven_button": "Улучшить — {cost}",
        "oven_level_line": '<tg-emoji emoji-id="5431816358675366190">🆙</tg-emoji> <i>Уровень: {level}/{max_level}</i>',
        "upgrade_oven_not_enough_toast": f"Не хватает {shop.CURRENCY_PLAIN} для улучшения печи.",
        "upgrade_oven_busy_toast": "Нельзя улучшать печь, пока в ней что-то готовится.",
        "upgrade_oven_max_toast": "Эта печь уже улучшена до максимума.",
        "upgrade_oven_done_toast": "🔧 Печь улучшена до {level} уровня! Время выпечки сокращено на {percent}%.",
    },
    "en": {
        "title": "🥐 <b>Bakery</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "oven_baking": "{emoji} <b>{name}</b> — {percent}%\n<i>Ready in {time}</i>",
        "oven_button_baking": "{emoji} {percent}%",
        "oven_empty_button": "Bake",
        "info_alert": "{emoji} {name}\n{bar} {percent}%\nReady in {time}",
        "showcase_title": f"<b>{SHOWCASE_TITLE_EMOJI} Showcase:</b>",
        "showcase_empty": "<i>empty for now — bake something</i>",
        "showcase_item": "{emoji} {name} ×{count}",
        "pantry_hint": "<i>Sell it on the market, or feed it to the panda from the \"My panda\" section.</i>",
        "shop_button": "Ingredients shop",
        "back_button": "Back",
        "choose_recipe_title": "🔥 <b>What to bake?</b> <i>(page {page}/{total})</i>",
        "page_prev_button": "Prev",
        "page_next_button": "Next",
        "recipe_line": "{emoji} <b>{name}</b> — <i>{time}</i>\n<i>{ingredients_line}</i>",
        "recipe_button": "{emoji} {name}",
        "ingredients_need_line": "Needs: {items}",
        "baking_started_toast": "In the oven: {emoji} {name}\nReady in {time}",
        "oven_taken_toast": "Something is already baking in this oven.",
        "not_enough_toast": "Not enough ingredients or fruit for this recipe.",
        "no_free_oven_toast": "All ovens are busy — wait for the current bake.",
        "auto_baked_notice": "<i>{emoji} {name} is ready and on the showcase! 🍽\nGained +{xp} XP for it</i>",
        "feed_redirect_toast": "🐼 You can feed the panda from the \"My panda\" section now.",
        "time_min_sec": "{minutes}m {seconds}s",
        "time_sec": "{seconds}s",
        # --- ingredients shop ---
        "shop_title": f"{SHOP_TITLE_EMOJI} <b>Ingredients shop</b>",
        "shop_hint": "<i>No fruit sold here — grow it in the garden instead.</i>",
        "shop_balance_line": f"{shop.CE_BALANCE} <b>Balance:</b> <b>{{balance}} {shop.CURRENCY}</b>",
        "shop_item_line": f"{{emoji}} <b>{{name}}</b> — <i>{shop.CURRENCY}{{price}}/ea · have: {{count}}</i>",
        "buy_button": "{price} | {emoji} {name}",
        "qty_button": "×{qty} — {total}",
        "qty_custom_button": "Custom amount",
        "choose_qty_title": f"{{emoji}} <b>{{name}}</b>\nHow many? Price: {shop.CURRENCY}{{price}}/ea",
        "ask_qty": "{emoji} <b>{name}</b>\n<i>Enter a quantity (number):</i>",
        "qty_invalid": "Enter a whole number from 1 to {max}.",
        "bought_toast": f"Bought: {{emoji}} {{name}} ×{{count}} for {{total}} {shop.CURRENCY_PLAIN}",
        "not_enough_pn_toast": "Not enough Pn for this purchase.",
        # --- oven pagination / unlocking paid ovens ---
        "title_page_suffix": " <i>(page {page}/{total})</i>",
        "oven_locked_line": f'<tg-emoji emoji-id="5296369303661067030">🔒</tg-emoji> <i>Oven locked</i>',
        "unlock_oven_button": "Unlock — {cost}",
        "unlocked_oven_toast": "🔥 A new oven is unlocked!",
        "unlock_oven_not_enough_toast": f"Not enough {shop.CURRENCY_PLAIN} for this purchase.",
        "unlock_oven_already_toast": "This oven is already unlocked.",
        # --- oven upgrades ---
        "upgrade_oven_button": "Upgrade — {cost}",
        "oven_level_line": '<tg-emoji emoji-id="5431816358675366190">🆙</tg-emoji> <i>Level: {level}/{max_level}</i>',
        "upgrade_oven_not_enough_toast": f"Not enough {shop.CURRENCY_PLAIN} to upgrade this oven.",
        "upgrade_oven_busy_toast": "Can't upgrade an oven while something is baking in it.",
        "upgrade_oven_max_toast": "This oven is already at max level.",
        "upgrade_oven_done_toast": "🔧 Oven upgraded to level {level}! Baking time reduced by {percent}%.",
    },
}


def _format_duration(seconds: float, lang: str) -> str:
    t = TEXTS[lang]
    seconds = max(0, int(round(seconds)))
    minutes, secs = divmod(seconds, 60)
    if minutes > 0:
        return t["time_min_sec"].format(minutes=minutes, seconds=secs)
    return t["time_sec"].format(seconds=secs)


def _format_recipe_requirements(lang: str, recipe: dict) -> str:
    parts = []
    for ing_id, need in recipe["ingredients"].items():
        ing = INGREDIENTS[ing_id]
        parts.append(f"{ing['emoji']}×{need}")
    for crop_id, need in recipe.get("fruits", {}).items():
        crop = garden.CROPS[crop_id]
        parts.append(f"{crop['emoji']}×{need}")
    return TEXTS[lang]["ingredients_need_line"].format(items=" ".join(parts))


# ==========================
#   ХРАНИЛИЩЕ (общая БД — см. database.py)
# ==========================
#
# Своего соединения и своих таблиц этот модуль не создаёт — всё общее
# для бота, в database.py.


async def get_ingredients(user_id: int) -> dict[str, int]:
    db = await database.get_db()
    async with db.execute(
        "SELECT ingredient_id, count FROM bakery_ingredients WHERE user_id = ? AND count > 0",
        (user_id,),
    ) as cursor:
        return {row["ingredient_id"]: row["count"] async for row in cursor}


async def _add_ingredients(user_id: int, ingredient_id: str, count: int) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO bakery_ingredients (user_id, ingredient_id, count) VALUES (?, ?, ?)
        ON CONFLICT (user_id, ingredient_id) DO UPDATE SET count = count + excluded.count
        """,
        (user_id, ingredient_id, count),
    )
    await database.commit()


async def _take_ingredients(user_id: int, ingredient_id: str, count: int) -> bool:
    """Атомарно списывает count единиц ингредиента, если их хватает.
    Как и в garden.take_from_basket_bulk — условный UPDATE, а не
    отдельные SELECT+UPDATE, чтобы не было окна для дюпа."""
    db = await database.get_db()
    cursor = await db.execute(
        "UPDATE bakery_ingredients SET count = count - ? WHERE user_id = ? AND ingredient_id = ? AND count >= ?",
        (count, user_id, ingredient_id, count),
    )
    await database.commit()
    return cursor.rowcount > 0


async def buy_ingredient(user_id: int, ingredient_id: str, count: int) -> tuple[int, list[str]] | None:
    """Покупает count единиц ингредиента за Pn. Возвращает (потраченную
    сумму, id вновь достигнутых ачивок лавки — см. _bump_shop_achv_state),
    либо None, если Pn не хватило (в этом случае ничего не списывается и
    не выдаётся).

    Лок на user_id держится на всё время операции: сначала проверяем и
    списываем Pn через shop.charge_balance (она рассчитана именно на
    вызов под уже открытым локом — см. её докстринг), затем начисляем
    ингредиент. Экономическая операция — сохраняется на диск немедленно."""
    if count <= 0:
        return None
    price = INGREDIENTS[ingredient_id]["price"] * count

    async with database.user_lock(user_id):
        charged = await shop.charge_balance(user_id, price)
        if not charged:
            return None
        await _add_ingredients(user_id, ingredient_id, count)
        await database.flush()

    achv_ids = await _bump_shop_achv_state(user_id, ingredient_id, count, price)
    return price, achv_ids


async def get_pantry(user_id: int) -> dict[str, int]:
    db = await database.get_db()
    async with db.execute(
        "SELECT recipe_id, count FROM bakery_pantry WHERE user_id = ? AND count > 0",
        (user_id,),
    ) as cursor:
        return {row["recipe_id"]: row["count"] async for row in cursor}


async def _add_to_pantry(user_id: int, recipe_id: str, count: int = 1) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO bakery_pantry (user_id, recipe_id, count) VALUES (?, ?, ?)
        ON CONFLICT (user_id, recipe_id) DO UPDATE SET count = count + excluded.count
        """,
        (user_id, recipe_id, count),
    )
    await database.commit()


async def take_from_pantry(user_id: int, recipe_id: str) -> bool:
    """Убирает 1 изделие с витрины. Возвращает False, если его там не
    было — тот же атомарный условный UPDATE, что и в garden.take_from_basket."""
    db = await database.get_db()
    cursor = await db.execute(
        "UPDATE bakery_pantry SET count = count - 1 WHERE user_id = ? AND recipe_id = ? AND count >= 1",
        (user_id, recipe_id),
    )
    await database.commit()
    return cursor.rowcount > 0


async def take_from_pantry_bulk(user_id: int, recipe_id: str, count: int) -> bool:
    """Убирает count изделий с витрины разом. Возвращает False, если их
    там не набралось столько — используется shop.py при выставлении
    выпечки на рынок и при мгновенной продаже боту. Тот же атомарный
    условный UPDATE, что и в take_from_pantry / garden.take_from_basket_bulk."""
    if count <= 0:
        return False
    db = await database.get_db()
    cursor = await db.execute(
        "UPDATE bakery_pantry SET count = count - ? WHERE user_id = ? AND recipe_id = ? AND count >= ?",
        (count, user_id, recipe_id, count),
    )
    await database.commit()
    return cursor.rowcount > 0


async def add_to_pantry(user_id: int, recipe_id: str, count: int = 1) -> None:
    """Публичная обёртка над _add_to_pantry — используется shop.py, чтобы
    вернуть выпечку в витрину при снятии лота с продажи или при покупке
    лота другим игроком."""
    await _add_to_pantry(user_id, recipe_id, count)
    # Подстраховка для "Полная витрина" (bakery_showcase_20) на путях,
    # не связанных с самой выпечкой (лот вернулся продавцу, лот купил
    # другой игрок) — основной путь с уведомлением игроку см. в
    # _collect_oven_if_matches. Тут — тихая идемпотентная выдача, как у
    # "Первой выпечки" при автосборе (см. докстринг модуля).
    if await _showcase_total(user_id) >= SHOWCASE_FULL_THRESHOLD:
        import achives

        await achives.unlock(user_id, "bakery_showcase_20")


# ==========================
#   АЧИВКИ ПЕКАРНИ — СЧЁТЧИКИ/ПОРОГИ
# ==========================
# Часть ачивок категории "bakery" (см. achives.ACHIEVEMENTS) требует
# данных, которых нет в основных таблицах выше: сколько всего испечено
# изделий и сколько именно каждого рецепта (bakery_recipe_counts),
# сколько Pn потрачено в лавке/шоколада куплено/заработано на рынке/
# скормлено панде (bakery_achv_state), какие виды фруктов сада уже
# использованы в выпечке (bakery_fruits_used). Таблицы заводятся лениво
# (IF NOT EXISTS) через ensure_achv_tables() — вызывать один раз при
# старте бота, см. main.py: main() (по аналогии с
# panda.ensure_achv_state_table/garden.ensure_achv_tables).
#
# Регистрация PROGRESS_PROVIDERS (карточка ачивки в achives.py — реальные
# "X/Y" вместо бинарных 0%/100%) для этого модуля происходит не на
# верхнем уровне файла через "import achives" (как в panda.py), а внутри
# ensure_achv_tables() — top-level "import achives" тут завёл бы цикл на
# этапе загрузки модулей (achives.py импортирует prof/shop, а те в
# конечном счёте импортируют bakery — та же причина, по которой
# achives импортируется локально в функциях ниже, см. также комментарий
# у "import shop" в начале файла). К моменту вызова ensure_achv_tables()
# из main() все модули уже импортированы, цикла можно не бояться.

BULK_PURCHASE_THRESHOLD = 50
CHOCOLATE_MAGNATE_THRESHOLD = 100
BIG_SPENDER_THRESHOLD = 10_000
FULL_PANTRY_THRESHOLD = 10
SHOWCASE_FULL_THRESHOLD = 20
MARKET_SELL_THRESHOLD = 20
MARKET_EARN_THRESHOLD = 5_000
FEED_PANDA_THRESHOLD = 20
FEED_CAKE_THRESHOLD = 10
OVEN_STREAK_THRESHOLD = 7
CAKE_BAKE_SECONDS_THRESHOLD = 16 * 60  # "тяжёлый" рецепт для bakery_feed_cake_10
SECONDS_IN_DAY = 24 * 60 * 60


async def ensure_achv_tables() -> None:
    """Создаёт таблицы счётчиков ачивок пекарни и регистрирует
    PROGRESS_PROVIDERS. Вызывать один раз при старте бота, ДО первого
    обращения к разделу "Пекарня" — иначе счётчики ниже упадут с
    "no such table"."""
    db = await database.get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_achv_state (
            user_id INTEGER PRIMARY KEY,
            total_baked INTEGER NOT NULL DEFAULT 0,
            ingredients_spent INTEGER NOT NULL DEFAULT 0,
            chocolate_bought INTEGER NOT NULL DEFAULT 0,
            market_sold INTEGER NOT NULL DEFAULT 0,
            market_earned INTEGER NOT NULL DEFAULT 0,
            fed_panda_count INTEGER NOT NULL DEFAULT 0,
            fed_panda_cake_count INTEGER NOT NULL DEFAULT 0,
            oven_streak_days INTEGER NOT NULL DEFAULT 0,
            last_oven_day INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    # Сколько раз испечён каждый конкретный рецепт — count > 0 значит
    # "рецепт пробовали хотя бы раз" (bakery_taster/bakery_all_recipes),
    # само число — для рецепт-специфичных ачивок (bakery_brownie_master/
    # bakery_classic_lover).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_recipe_counts (
            user_id INTEGER NOT NULL,
            recipe_id TEXT NOT NULL,
            count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, recipe_id)
        )
        """
    )
    # Множество видов фруктов сада, уже хоть раз ушедших на выпечку
    # (bakery_garden_to_oven) — без счётчика, факт наличия строки.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_fruits_used (
            user_id INTEGER NOT NULL,
            crop_id TEXT NOT NULL,
            PRIMARY KEY (user_id, crop_id)
        )
        """
    )
    # Какие платные печи (индекс >= BAKERY_BASE_OVEN_COUNT) игрок уже
    # открыл за монеты — см. OVEN_UNLOCK_COST/unlock_oven. Наличие
    # строки означает "открыта"; первые BAKERY_BASE_OVEN_COUNT печей
    # тут не хранятся — они открыты у всех по умолчанию.
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_oven_unlocks (
            user_id INTEGER NOT NULL,
            oven_index INTEGER NOT NULL,
            PRIMARY KEY (user_id, oven_index)
        )
        """
    )
    # Уровни улучшения печей (см. OVEN_UPGRADE_COST/upgrade_oven выше) —
    # отсутствие строки означает уровень 1 (не улучшалась).
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS bakery_oven_levels (
            user_id INTEGER NOT NULL,
            oven_index INTEGER NOT NULL,
            level INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (user_id, oven_index)
        )
        """
    )
    await database.commit()

    _register_progress_providers()


def _register_progress_providers() -> None:
    import achives

    achives.PROGRESS_PROVIDERS.update(
        {
            "bakery_taster": (5, _distinct_recipes_baked),
            "bakery_all_recipes": (len(RECIPE_ORDER), _distinct_recipes_baked),
            "bakery_brownie_master": (10, _progress_brownie_count),
            "bakery_classic_lover": (25, _progress_apple_pie_count),
            "bakery_baked_50": (50, _progress_total_baked),
            "bakery_baked_250": (250, _progress_total_baked),
            "bakery_baked_1000": (1000, _progress_total_baked),
            "bakery_no_idle_7": (OVEN_STREAK_THRESHOLD, _progress_oven_streak),
            "bakery_chocolate_100": (CHOCOLATE_MAGNATE_THRESHOLD, _progress_chocolate_bought),
            "bakery_full_ingredients": (len(INGREDIENT_ORDER), _progress_full_ingredients),
            "bakery_big_spender_10000": (BIG_SPENDER_THRESHOLD, _progress_ingredients_spent),
            "bakery_showcase_20": (SHOWCASE_FULL_THRESHOLD, _showcase_total),
            "bakery_market_sell_20": (MARKET_SELL_THRESHOLD, _progress_market_sold),
            "bakery_market_earn_5000": (MARKET_EARN_THRESHOLD, _progress_market_earned),
            "bakery_feed_panda_20": (FEED_PANDA_THRESHOLD, _progress_fed_panda),
            "bakery_feed_cake_10": (FEED_CAKE_THRESHOLD, _progress_fed_cake),
            "bakery_garden_to_oven": (len(garden.CROPS), _count_fruits_used),
        }
    )


async def _ensure_achv_state_row(db: aiosqlite.Connection, user_id: int) -> None:
    await db.execute("INSERT OR IGNORE INTO bakery_achv_state (user_id) VALUES (?)", (user_id,))


async def _get_achv_state(user_id: int) -> aiosqlite.Row | None:
    db = await database.get_db()
    async with db.execute(
        "SELECT total_baked, chocolate_bought, ingredients_spent, market_sold, "
        "market_earned, fed_panda_count, fed_panda_cake_count, oven_streak_days "
        "FROM bakery_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


# --- провайдеры прогресса (см. _register_progress_providers выше) ---

async def _progress_total_baked(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["total_baked"] if row else 0


async def _progress_chocolate_bought(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["chocolate_bought"] if row else 0


async def _progress_ingredients_spent(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["ingredients_spent"] if row else 0


async def _progress_market_sold(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["market_sold"] if row else 0


async def _progress_market_earned(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["market_earned"] if row else 0


async def _progress_fed_panda(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["fed_panda_count"] if row else 0


async def _progress_fed_cake(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["fed_panda_cake_count"] if row else 0


async def _progress_oven_streak(user_id: int) -> int:
    row = await _get_achv_state(user_id)
    return row["oven_streak_days"] if row else 0


async def _progress_full_ingredients(user_id: int) -> int:
    inv = await get_ingredients(user_id)
    return sum(1 for i in INGREDIENT_ORDER if inv.get(i, 0) >= FULL_PANTRY_THRESHOLD)


async def _progress_brownie_count(user_id: int) -> int:
    return await _get_recipe_count(user_id, "pear_chocolate_brownie")


async def _progress_apple_pie_count(user_id: int) -> int:
    return await _get_recipe_count(user_id, "apple_pie")


# --- рецепты: счётчики "испечено раз" (per-recipe и общий) ---

async def _bump_recipe_count(user_id: int, recipe_id: str) -> int:
    """+1 к счётчику испечённых изделий именно этого рецепта, возвращает
    новое значение (bakery_royal_table/bakery_brownie_master/
    bakery_classic_lover, а также "рецепт пробовали хотя бы раз" — см.
    _distinct_recipes_baked)."""
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO bakery_recipe_counts (user_id, recipe_id, count) VALUES (?, ?, 1)
        ON CONFLICT (user_id, recipe_id) DO UPDATE SET count = count + 1
        """,
        (user_id, recipe_id),
    )
    await database.commit()
    async with db.execute(
        "SELECT count FROM bakery_recipe_counts WHERE user_id = ? AND recipe_id = ?",
        (user_id, recipe_id),
    ) as cursor:
        return (await cursor.fetchone())["count"]


async def _get_recipe_count(user_id: int, recipe_id: str) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT count FROM bakery_recipe_counts WHERE user_id = ? AND recipe_id = ?",
        (user_id, recipe_id),
    ) as cursor:
        row = await cursor.fetchone()
    return row["count"] if row else 0


async def _distinct_recipes_baked(user_id: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS c FROM bakery_recipe_counts WHERE user_id = ? AND count > 0",
        (user_id,),
    ) as cursor:
        return (await cursor.fetchone())["c"]


async def _bump_total_baked(user_id: int) -> int:
    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id)
    await db.execute(
        "UPDATE bakery_achv_state SET total_baked = total_baked + 1 WHERE user_id = ?", (user_id,)
    )
    await database.commit()
    async with db.execute(
        "SELECT total_baked FROM bakery_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        return (await cursor.fetchone())["total_baked"]


def _total_baked_achievements(total: int) -> list[str]:
    thresholds = [(50, "bakery_baked_50"), (250, "bakery_baked_250"), (1000, "bakery_baked_1000")]
    return [achv_id for need, achv_id in thresholds if total >= need]


def _recipe_specific_achievements(recipe_id: str, count: int) -> list[str]:
    achv_ids = []
    if recipe_id == "royal_tropical_cake":
        achv_ids.append("bakery_royal_table")
    if recipe_id == "pear_chocolate_brownie" and count >= 10:
        achv_ids.append("bakery_brownie_master")
    if recipe_id == "apple_pie" and count >= 25:
        achv_ids.append("bakery_classic_lover")
    return achv_ids


def _distinct_recipes_achievements(distinct: int) -> list[str]:
    achv_ids = []
    if distinct >= 5:
        achv_ids.append("bakery_taster")
    if distinct >= len(RECIPE_ORDER):
        achv_ids.append("bakery_all_recipes")
    return achv_ids


async def _showcase_total(user_id: int) -> int:
    pantry = await get_pantry(user_id)
    return sum(pantry.values())


# --- фрукты сада, использованные в выпечке (bakery_garden_to_oven) ---

async def _record_fruit_used(user_id: int, crop_id: str) -> None:
    db = await database.get_db()
    await db.execute(
        "INSERT OR IGNORE INTO bakery_fruits_used (user_id, crop_id) VALUES (?, ?)",
        (user_id, crop_id),
    )
    await database.commit()


async def _count_fruits_used(user_id: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS c FROM bakery_fruits_used WHERE user_id = ?", (user_id,)
    ) as cursor:
        return (await cursor.fetchone())["c"]


# --- печи: "обе сразу" и стрик "хотя бы одна занята каждый день" ---

async def _touch_oven_streak(user_id: int, oven_busy_now: bool) -> list[str]:
    """Обновляет стрик "хотя бы одна печь занята" по календарным
    (реальным) дням — та же схема, что и panda._record_care_day: один
    день, даже с несколькими проверками, засчитывается один раз;
    пропуск хотя бы одного дня сбрасывает стрик до 1. Возвращает
    ["bakery_no_idle_7"], если стрик впервые достиг порога."""
    if not oven_busy_now:
        return []

    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id)
    today = int(time.time() // SECONDS_IN_DAY)

    async with db.execute(
        "SELECT oven_streak_days, last_oven_day FROM bakery_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        state = await cursor.fetchone()

    if state["last_oven_day"] == today:
        return []  # сегодняшний день уже засчитан

    streak = state["oven_streak_days"] + 1 if state["last_oven_day"] == today - 1 else 1
    await db.execute(
        "UPDATE bakery_achv_state SET oven_streak_days = ?, last_oven_day = ? WHERE user_id = ?",
        (streak, today, user_id),
    )
    await database.commit()

    return ["bakery_no_idle_7"] if streak >= OVEN_STREAK_THRESHOLD else []


async def _check_bakery_achievements(user_id: int, ovens: list) -> list[str]:
    """Ачивки, не привязанные к конкретному действию, а проверяемые при
    каждой отрисовке главного экрана пекарни (см. _render_and_send):
    "Обе печи в деле" (обе печи заняты прямо сейчас), "Ни минуты
    простоя" (стрик дней) и "Из сада в печь" (все виды фруктов сада уже
    использованы). ovens — уже прочитанный список печей (см.
    _get_ovens), чтобы не запрашивать его повторно."""
    achv_ids = []

    busy = sum(1 for o in ovens if o["recipe_id"] is not None)
    if busy >= BAKERY_BASE_OVEN_COUNT:
        achv_ids.append("bakery_both_ovens")

    achv_ids += await _touch_oven_streak(user_id, busy > 0)

    if await _count_fruits_used(user_id) >= len(garden.CROPS):
        achv_ids.append("bakery_garden_to_oven")

    return achv_ids


# --- лавка ингредиентов: покупки (см. buy_ingredient) ---

async def _bump_shop_achv_state(user_id: int, ingredient_id: str, count: int, spent: int) -> list[str]:
    """Обновляет счётчики лавки (потрачено всего, куплено шоколада
    всего) и возвращает id вновь достигнутых ачивок лавки: "Первая
    покупка" (всегда, идемпотентно), "Про запас" (если count за этот
    раз достиг порога), "Шоколадный магнат"/"Крупный транжира"
    (накопительно), "Полная кладовая" (сейчас на складе хватает по
    каждому из 6 ингредиентов)."""
    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id)

    chocolate_delta = count if ingredient_id == "chocolate" else 0
    await db.execute(
        "UPDATE bakery_achv_state SET ingredients_spent = ingredients_spent + ?, "
        "chocolate_bought = chocolate_bought + ? WHERE user_id = ?",
        (spent, chocolate_delta, user_id),
    )
    await database.commit()

    async with db.execute(
        "SELECT ingredients_spent, chocolate_bought FROM bakery_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        state = await cursor.fetchone()

    achv_ids = ["bakery_first_purchase"]
    if count >= BULK_PURCHASE_THRESHOLD:
        achv_ids.append("bakery_bulk_50")
    if state["chocolate_bought"] >= CHOCOLATE_MAGNATE_THRESHOLD:
        achv_ids.append("bakery_chocolate_100")
    if state["ingredients_spent"] >= BIG_SPENDER_THRESHOLD:
        achv_ids.append("bakery_big_spender_10000")

    ingredients_inv = await get_ingredients(user_id)
    if all(ingredients_inv.get(i, 0) >= FULL_PANTRY_THRESHOLD for i in INGREDIENT_ORDER):
        achv_ids.append("bakery_full_ingredients")

    return achv_ids


# --- рынок: продажа выпечки (вызывается из shop.py) ---

async def bump_market_sold(user_id: int, count: int, earned: int) -> list[str]:
    """+count к проданной на рынке выпечке и +earned к заработанному с
    неё Pn — вызывается из shop.py и для лотов, и для мгновенной
    продажи боту (для этой ачивки — то же самое "продать на рынке").
    Возвращает id вновь достигнутых порогов ("Кондитерская лавка"/
    "Сладкий бизнес") — выдачу и уведомление делает вызывающий код, как
    и с остальными ачивками рынка (см. shop.py: "first_listing")."""
    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id)
    await db.execute(
        "UPDATE bakery_achv_state SET market_sold = market_sold + ?, market_earned = market_earned + ? "
        "WHERE user_id = ?",
        (count, earned, user_id),
    )
    await database.commit()

    async with db.execute(
        "SELECT market_sold, market_earned FROM bakery_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        state = await cursor.fetchone()

    achv_ids = []
    if state["market_sold"] >= MARKET_SELL_THRESHOLD:
        achv_ids.append("bakery_market_sell_20")
    if state["market_earned"] >= MARKET_EARN_THRESHOLD:
        achv_ids.append("bakery_market_earn_5000")
    return achv_ids


# --- кормление панды выпечкой (вызывается из panda.py) ---

def _is_cake_recipe(recipe_id: str) -> bool:
    return RECIPES[recipe_id]["bake_seconds"] >= CAKE_BAKE_SECONDS_THRESHOLD


async def bump_panda_fed(user_id: int, recipe_id: str) -> list[str]:
    """+1 к счётчику кормлений панды выпечкой, и, если recipe_id —
    "тяжёлый" рецепт (торт/гато, см. _is_cake_recipe), ещё +1 к счётчику
    кормлений именно тортом. Возвращает id вновь достигнутых порогов
    ("Сладкоежка"/"Праздничный торт") — выдачу делает panda.py, там же,
    где остальные ачивки кормления (см. panda.py: on_feed_item)."""
    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id)
    is_cake = _is_cake_recipe(recipe_id)
    if is_cake:
        await db.execute(
            "UPDATE bakery_achv_state SET fed_panda_count = fed_panda_count + 1, "
            "fed_panda_cake_count = fed_panda_cake_count + 1 WHERE user_id = ?",
            (user_id,),
        )
    else:
        await db.execute(
            "UPDATE bakery_achv_state SET fed_panda_count = fed_panda_count + 1 WHERE user_id = ?",
            (user_id,),
        )
    await database.commit()

    async with db.execute(
        "SELECT fed_panda_count, fed_panda_cake_count FROM bakery_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        state = await cursor.fetchone()

    achv_ids = []
    if state["fed_panda_count"] >= FEED_PANDA_THRESHOLD:
        achv_ids.append("bakery_feed_panda_20")
    if state["fed_panda_cake_count"] >= FEED_CAKE_THRESHOLD:
        achv_ids.append("bakery_feed_cake_10")
    return achv_ids


# --- общее: выдать+уведомить по списку id (см. использование ниже) ---

async def _notify_achievements(sender, user_id: int, lang: str, achv_ids: list[str]) -> None:
    """Выдаёт по очереди ачивки achv_ids (unlock идемпотентен — уже
    открытые тихо пропускаются) и шлёт отдельное уведомление на каждую
    реально выданную. sender — awaitable(text), обычно message.answer
    или callback.message.answer."""
    if not achv_ids:
        return
    import achives

    for achv_id in achv_ids:
        achv_result = await achives.unlock(user_id, achv_id)
        if achv_result:
            await sender(achives.format_unlock_text(lang, achv_result))


async def _get_ovens(user_id: int) -> list[aiosqlite.Row]:
    """Возвращает BAKERY_OVEN_COUNT печей, создавая недостающие пустыми.
    Перед этим тихо собирает всё, что уже готово — подстраховка на
    случай, если фоновая задача ещё не сработала (см. garden._get_plots)."""
    await _auto_collect_ready(user_id)
    db = await database.get_db()
    async with db.execute(
        "SELECT * FROM bakery_ovens WHERE user_id = ? ORDER BY oven_index", (user_id,)
    ) as cursor:
        rows = {row["oven_index"]: row for row in await cursor.fetchall()}

    missing = [i for i in range(BAKERY_OVEN_COUNT) if i not in rows]
    for i in missing:
        await db.execute(
            "INSERT INTO bakery_ovens (user_id, oven_index, recipe_id, started_at) VALUES (?, ?, NULL, NULL)",
            (user_id, i),
        )
    if missing:
        await database.commit()
        async with db.execute(
            "SELECT * FROM bakery_ovens WHERE user_id = ? ORDER BY oven_index", (user_id,)
        ) as cursor:
            return await cursor.fetchall()

    return [rows[i] for i in range(BAKERY_OVEN_COUNT)]


async def _get_unlocked_extra_ovens(user_id: int) -> set[int]:
    """Индексы ДОПОЛНИТЕЛЬНЫХ печей (>= BAKERY_BASE_OVEN_COUNT), уже
    открытых игроком за монеты (см. unlock_oven). Первые
    BAKERY_BASE_OVEN_COUNT печей сюда не входят — они открыты всегда,
    см. _is_oven_unlocked."""
    db = await database.get_db()
    async with db.execute(
        "SELECT oven_index FROM bakery_oven_unlocks WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {row["oven_index"] async for row in cursor}


def _is_oven_unlocked(oven_index: int, unlocked_extra: set[int]) -> bool:
    return oven_index < BAKERY_BASE_OVEN_COUNT or oven_index in unlocked_extra


async def unlock_oven(user_id: int, oven_index: int) -> str:
    """Открывает платную печь oven_index за монеты (OVEN_UNLOCK_COST).
    Тот же паттерн, что и buy_ingredient выше: лок на user_id,
    списание через shop.charge_balance, немедленный flush. Возвращает:
      "ok"          — открыто прямо сейчас
      "already"     — уже была открыта раньше (ничего не списано)
      "not_enough"  — не хватило монет (ничего не списано)
      "invalid"     — этот индекс вообще не подлежит открытию за монеты
                       (базовая печь либо индекс вне диапазона)"""
    if oven_index not in OVEN_UNLOCK_COST:
        return "invalid"

    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT 1 FROM bakery_oven_unlocks WHERE user_id = ? AND oven_index = ?",
            (user_id, oven_index),
        ) as cursor:
            if await cursor.fetchone():
                return "already"

        charged = await shop.charge_balance(user_id, OVEN_UNLOCK_COST[oven_index])
        if not charged:
            return "not_enough"

        await db.execute(
            "INSERT OR IGNORE INTO bakery_oven_unlocks (user_id, oven_index) VALUES (?, ?)",
            (user_id, oven_index),
        )
        await database.flush()

    return "ok"


async def _get_oven_levels(user_id: int) -> dict[int, int]:
    """Индекс печи -> её текущий уровень улучшения (см. OVEN_UPGRADE_COST/
    upgrade_oven). Печей без строки в bakery_oven_levels (никогда не
    улучшались) в словаре нет — см. _oven_level, которая для них
    возвращает уровень 1 по умолчанию."""
    db = await database.get_db()
    async with db.execute(
        "SELECT oven_index, level FROM bakery_oven_levels WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {row["oven_index"]: row["level"] async for row in cursor}


def _oven_level(levels: dict[int, int], oven_index: int) -> int:
    return levels.get(oven_index, 1)


async def _get_single_oven_level(user_id: int, oven_index: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT level FROM bakery_oven_levels WHERE user_id = ? AND oven_index = ?",
        (user_id, oven_index),
    ) as cursor:
        row = await cursor.fetchone()
    return row["level"] if row else 1


async def upgrade_oven(user_id: int, oven_index: int) -> str:
    """Повышает уровень печи oven_index на 1 (см. OVEN_UPGRADE_COST/
    OVEN_UPGRADE_MAX_LEVEL). Тот же паттерн, что и unlock_oven/upgrade_plot
    в garden.py. Возвращает:
      "ok"          — улучшено прямо сейчас
      "busy"        — в печи сейчас что-то готовится, улучшать нельзя
      "max_level"   — уже максимальный уровень
      "not_enough"  — не хватило монет (ничего не списано)"""
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT recipe_id FROM bakery_ovens WHERE user_id = ? AND oven_index = ?",
            (user_id, oven_index),
        ) as cursor:
            oven_row = await cursor.fetchone()
        if oven_row is not None and oven_row["recipe_id"] is not None:
            return "busy"

        async with db.execute(
            "SELECT level FROM bakery_oven_levels WHERE user_id = ? AND oven_index = ?",
            (user_id, oven_index),
        ) as cursor:
            level_row = await cursor.fetchone()
        current_level = level_row["level"] if level_row else 1
        if current_level >= OVEN_UPGRADE_MAX_LEVEL:
            return "max_level"

        next_level = current_level + 1
        charged = await shop.charge_balance(user_id, OVEN_UPGRADE_COST[next_level])
        if not charged:
            return "not_enough"

        await db.execute(
            """
            INSERT INTO bakery_oven_levels (user_id, oven_index, level) VALUES (?, ?, ?)
            ON CONFLICT (user_id, oven_index) DO UPDATE SET level = excluded.level
            """,
            (user_id, oven_index, next_level),
        )
        await database.flush()

    return "ok"


async def _privilege_speedup_offset(user_id: int, duration_seconds: float) -> float:
    """Сколько секунд отнять от времени старта выпечки, если у игрока
    активна привилегия с ускорением (donate.py: PRIVILEGE_TIERS,
    speedup_percent). 0, если привилегии нет. Тот же приём, что и в
    garden.py (см. там подробный комментарий): "задним числом" сдвигаем
    started_at, весь остальной код везде считает от него, так что одной
    правки в момент старта достаточно. Локальный импорт donate — по той
    же причине, что и "import garden"/остальные локальные импорты в
    этом файле не заводят цикл, а верхнеуровневый бы завёл."""
    import donate

    active = await donate.get_active_privilege(user_id)
    if active is None:
        return 0.0
    percent = active["tier"]["speedup_percent"]
    if not percent:
        return 0.0
    return duration_seconds * percent / 100


async def start_baking(user_id: int, oven_index: int, recipe_id: str, lang: str) -> float | str | None:
    """Запускает выпечку. Возвращает таймстамп начала при успехе.
    Возвращает "not_enough", если не хватает ингредиентов или фруктов
    (в этом случае НИЧЕГО не списывается — проверка идёт до списания).
    Возвращает None, если печь уже занята.

    Всё — под одним локом на user_id: сначала убеждаемся, что печь
    свободна, затем проверяем ингредиенты/фрукты (только читаем), и
    только когда точно хватает всего — списываем и ставим печься.
    Так рецепт либо применяется целиком, либо не трогает вообще ничего.

    Время выпечки берётся с поправкой на уровень печи (см.
    _effective_bake_seconds/OVEN_UPGRADE_COST) — чем выше уровень, тем
    короче базовое время ДО применения ускорения от привилегии ниже.

    Если у игрока активна привилегия с ускорением роста — возвращаемый
    started_at "задним числом" сдвинут в прошлое на её speedup_percent
    от полного времени выпечки (см. _privilege_speedup_offset)."""
    recipe = RECIPES[recipe_id]
    level = await _get_single_oven_level(user_id, oven_index)
    bake_seconds = _effective_bake_seconds(recipe_id, level)
    speedup_offset = await _privilege_speedup_offset(user_id, bake_seconds)

    async with database.user_lock(user_id):
        db = await database.get_db()

        # Гарантируем, что строка для этой печи существует.
        await db.execute(
            """
            INSERT INTO bakery_ovens (user_id, oven_index, recipe_id, started_at, lang)
            VALUES (?, ?, NULL, NULL, NULL)
            ON CONFLICT (user_id, oven_index) DO NOTHING
            """,
            (user_id, oven_index),
        )

        async with db.execute(
            "SELECT recipe_id FROM bakery_ovens WHERE user_id = ? AND oven_index = ?",
            (user_id, oven_index),
        ) as cursor:
            row = await cursor.fetchone()

        if row is not None and row["recipe_id"] is not None:
            await database.commit()
            return None

        ingredients_inv = await get_ingredients(user_id)
        for ing_id, need in recipe["ingredients"].items():
            if ingredients_inv.get(ing_id, 0) < need:
                await database.commit()
                return "not_enough"

        fruits_inv = await garden.get_inventory(user_id)
        for crop_id, need in recipe.get("fruits", {}).items():
            if fruits_inv.get(crop_id, 0) < need:
                await database.commit()
                return "not_enough"

        # Всего хватает — списываем и ставим печься. Лок на user_id не
        # даёт параллельному запросу того же игрока прошмыгнуть между
        # проверкой и списанием, поэтому дополнительные атомарные
        # условные UPDATE ниже — уже просто подстраховка.
        for ing_id, need in recipe["ingredients"].items():
            await _take_ingredients(user_id, ing_id, need)
        for crop_id, need in recipe.get("fruits", {}).items():
            await garden.take_from_basket_bulk(user_id, crop_id, need)
            # "Из сада в печь" (bakery_garden_to_oven) — сам факт, что
            # этот вид фрукта хоть раз ушёл на выпечку; проверка порога
            # (все виды сразу) — в _check_bakery_achievements.
            await _record_fruit_used(user_id, crop_id)

        started_at = time.time() - speedup_offset
        cursor = await db.execute(
            """
            UPDATE bakery_ovens
            SET recipe_id = ?, started_at = ?, lang = ?
            WHERE user_id = ? AND oven_index = ? AND recipe_id IS NULL
            """,
            (recipe_id, started_at, lang, user_id, oven_index),
        )
        await database.commit()

        if cursor.rowcount == 0:
            # Теоретически недостижимо под локом, но на всякий случай:
            # если печь всё же занята, возвращаем то, что уже списали.
            for ing_id, need in recipe["ingredients"].items():
                await _add_ingredients(user_id, ing_id, need)
            for crop_id, need in recipe.get("fruits", {}).items():
                await garden.add_to_basket(user_id, crop_id, need)
            return None

        return started_at


async def _collect_oven_if_matches(
    user_id: int, oven_index: int, recipe_id: str, started_at: float
) -> tuple[bool, int | None, dict | None, list[dict]]:
    """Идемпотентно переносит готовое изделие на витрину — та же схема,
    что и garden._collect_plot_if_matches (см. её подробный комментарий
    про защиту от дюпа условным UPDATE). Возвращает (collected,
    xp_gained, level_info, achv_results): при collected=True — сколько
    XP реально начислено за эту выпечку, результат prof.add_xp()
    целиком и список результатов achives.unlock() по ВСЕМ ачивкам,
    выполненным именно этой выпечкой ("Первая выпечка", счётчики по
    рецептам/общий, "Полная витрина", "Ночная смена" — их может
    набраться сразу несколько), иначе (False, None, None, [])."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        cursor = await db.execute(
            """
            UPDATE bakery_ovens
            SET recipe_id = NULL, started_at = NULL, lang = NULL
            WHERE user_id = ? AND oven_index = ? AND recipe_id = ? AND started_at = ?
            """,
            (user_id, oven_index, recipe_id, started_at),
        )

        if cursor.rowcount == 0:
            await database.commit()
            return False, None, None, []

        await db.execute(
            """
            INSERT INTO bakery_pantry (user_id, recipe_id, count) VALUES (?, ?, 1)
            ON CONFLICT (user_id, recipe_id) DO UPDATE SET count = count + 1
            """,
            (user_id, recipe_id),
        )
        await database.commit()

    # За опытом и ачивками — уже ЗА ПРЕДЕЛАМИ "async with
    # database.user_lock(...)" выше, по той же причине, что и в
    # garden._collect_plot_if_matches: prof.add_xp() сам берёт лок на
    # того же user_id, повторный вход был бы дедлоком. Импорт локальный
    # — иначе цикл: prof → shop → bakery (и achives → prof/shop → ... →
    # bakery — та же причина для achives).
    import prof
    import achives

    xp_gained = random.randint(250, 450)
    level_info = await prof.add_xp(user_id, xp_gained)

    total_baked = await _bump_total_baked(user_id)
    recipe_count = await _bump_recipe_count(user_id, recipe_id)
    distinct_recipes = await _distinct_recipes_baked(user_id)

    achv_ids = ["first_bake"]
    achv_ids += _total_baked_achievements(total_baked)
    achv_ids += _recipe_specific_achievements(recipe_id, recipe_count)
    achv_ids += _distinct_recipes_achievements(distinct_recipes)

    # "Ночная смена" — по серверному локальному времени в момент сбора
    # (обычно это момент готовности, см. _auto_bake_after_delay; при
    # сборе подстраховкой через _auto_collect_ready — момент, когда игрок
    # фактически заглянул в пекарню после готовности).
    if 0 <= time.localtime().tm_hour < 5:
        achv_ids.append("bakery_night_shift")

    if await _showcase_total(user_id) >= SHOWCASE_FULL_THRESHOLD:
        achv_ids.append("bakery_showcase_20")

    achv_results = []
    for achv_id in achv_ids:
        result = await achives.unlock(user_id, achv_id)
        if result:
            achv_results.append(result)

    return True, xp_gained, level_info, achv_results


async def _auto_collect_ready(user_id: int) -> None:
    """Тихо собирает всё, что уже готово, без уведомления (уведомление —
    забота фоновой задачи; это лишь подстраховка на случай, если она
    почему-то не сработала)."""
    db = await database.get_db()
    now = time.time()
    async with db.execute(
        "SELECT oven_index, recipe_id, started_at FROM bakery_ovens WHERE user_id = ? AND recipe_id IS NOT NULL",
        (user_id,),
    ) as cursor:
        rows = await cursor.fetchall()

    # Уровень печи не меняется, пока в ней что-то печётся (upgrade_oven
    # запрещает улучшение занятой печи), так что текущий уровень — тот
    # же, что был в момент старта выпечки, и его безопасно использовать.
    levels = await _get_oven_levels(user_id)

    for row in rows:
        recipe_id = row["recipe_id"]
        bake_seconds = _effective_bake_seconds(recipe_id, _oven_level(levels, row["oven_index"]))
        if now - row["started_at"] >= bake_seconds:
            await _collect_oven_if_matches(user_id, row["oven_index"], recipe_id, row["started_at"])


# ==========================
#   АВТОЗАВЕРШЕНИЕ И УВЕДОМЛЕНИЯ
# ==========================

_background_tasks: set[asyncio.Task] = set()


def _schedule_auto_bake(
    bot: Bot, user_id: int, oven_index: int, recipe_id: str, started_at: float, lang: str, level: int = 1
) -> None:
    """Создаёт фоновую задачу: как только выпечка будет готова, она сама
    переместится на витрину, а игроку придёт уведомление. level — уровень
    печи НА МОМЕНТ СТАРТА выпечки (см. _effective_bake_seconds) — см.
    аналогичный комментарий в garden._schedule_auto_harvest."""
    bake_seconds = _effective_bake_seconds(recipe_id, level)
    delay = max(0.0, bake_seconds - (time.time() - started_at))

    task = asyncio.create_task(
        _auto_bake_after_delay(bot, user_id, oven_index, recipe_id, started_at, lang, delay)
    )
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def _auto_bake_after_delay(
    bot: Bot,
    user_id: int,
    oven_index: int,
    recipe_id: str,
    started_at: float,
    lang: str,
    delay: float,
) -> None:
    if delay > 0:
        await asyncio.sleep(delay)

    collected, xp_gained, level_info, achv_results = await _collect_oven_if_matches(
        user_id, oven_index, recipe_id, started_at
    )
    if not collected:
        return  # уже собрано подстраховкой — повторное уведомление не нужно

    recipe = RECIPES[recipe_id]
    t = TEXTS.get(lang, TEXTS["ru"])
    text = t["auto_baked_notice"].format(
        emoji=recipe["emoji"], name=recipe["name"][lang], xp=xp_gained
    )
    if level_info and level_info["leveled_up"]:
        import prof

        text += "\n\n" + prof.level_up_notice(lang, level_info["new_level"])
    achievement_results = list(level_info["unlocked_achievements"]) if level_info else []
    achievement_results.extend(achv_results)
    if achievement_results:
        import achives

        for result in achievement_results:
            text += "\n\n" + achives.format_unlock_text(lang, result)
    try:
        await bot.send_message(user_id, text)
    except Exception:
        logger.warning(
            "Не удалось отправить уведомление о готовности выпечки пользователю %s",
            user_id,
            exc_info=True,
        )


async def reschedule_pending_bakes(bot: Bot) -> None:
    """Заново создаёт фоновые задачи автозавершения для всех печей, в
    которых что-то печётся. Вызывать один раз при старте бота (до
    start_polling) — см. garden.reschedule_pending_harvests, полностью
    та же идея."""
    db = await database.get_db()
    async with db.execute(
        "SELECT user_id, oven_index, recipe_id, started_at, lang FROM bakery_ovens WHERE recipe_id IS NOT NULL"
    ) as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        level = await _get_single_oven_level(row["user_id"], row["oven_index"])
        _schedule_auto_bake(
            bot,
            row["user_id"],
            row["oven_index"],
            row["recipe_id"],
            row["started_at"],
            row["lang"] or "ru",
            level,
        )


# ==========================
#   ОТРИСОВКА ГЛАВНОГО ЭКРАНА ПЕКАРНИ
# ==========================

def _build_bakery_view(
    lang: str,
    ovens: list[aiosqlite.Row],
    pantry: dict[str, int],
    page: int,
    unlocked_extra: set[int],
    levels: dict[int, int],
) -> tuple[str, object]:
    t = TEXTS[lang]
    now = time.time()

    total_pages = (BAKERY_OVEN_COUNT + OVENS_PER_PAGE - 1) // OVENS_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * OVENS_PER_PAGE
    page_ovens = ovens[start:start + OVENS_PER_PAGE]

    title = t["title"]
    if total_pages > 1:
        title += t["title_page_suffix"].format(page=page + 1, total=total_pages)
    lines = [title, t["separator"]]
    builder = InlineKeyboardBuilder()
    row_sizes = []

    for oven in page_ovens:
        oven_index = oven["oven_index"]
        recipe_id = oven["recipe_id"]

        if not _is_oven_unlocked(oven_index, unlocked_extra):
            # Платная печь, ещё не открытая — вместо выпечки показываем
            # цену открытия (см. OVEN_UNLOCK_COST/unlock_oven).
            lines.append(t["oven_locked_line"])
            lines.append("")
            builder.button(
                text=t["unlock_oven_button"].format(cost=OVEN_UNLOCK_COST[oven_index]),
                callback_data=f"bakery:unlock:{oven_index}",
                style="primary",
                icon_custom_emoji_id=OVEN_LOCK_EMOJI_ID,
            )
            row_sizes.append(1)
            continue

        level = _oven_level(levels, oven_index)
        # Кнопка "Улучшить" показывается парой с основной кнопкой печи
        # (испечь/готовится), пока печь не достигла максимального уровня —
        # см. OVEN_UPGRADE_MAX_LEVEL/upgrade_oven.
        can_upgrade = level < OVEN_UPGRADE_MAX_LEVEL

        if recipe_id is None:
            if level > 1:
                lines.append(
                    t["oven_level_line"].format(level=level, max_level=OVEN_UPGRADE_MAX_LEVEL)
                )
                lines.append("")
            builder.button(
                text=t["oven_empty_button"],
                callback_data=f"bakery:choose:{oven_index}",
                style="primary",
                icon_custom_emoji_id=BAKE_BUTTON_EMOJI_ID,
            )
            if can_upgrade:
                builder.button(
                    text=t["upgrade_oven_button"].format(cost=OVEN_UPGRADE_COST[level + 1]),
                    callback_data=f"bakery:upgrade:{oven_index}",
                    style="primary",
                    icon_custom_emoji_id=OVEN_UPGRADE_EMOJI_ID,
                )
                row_sizes.append(2)
            else:
                row_sizes.append(1)
            continue

        recipe = RECIPES[recipe_id]
        elapsed = now - oven["started_at"]
        bake_seconds = _effective_bake_seconds(recipe_id, level)
        percent = round(elapsed / bake_seconds * 100)
        remaining = bake_seconds - elapsed

        lines.append(
            t["oven_baking"].format(
                emoji=recipe["emoji"],
                name=recipe["name"][lang],
                percent=percent,
                time=_format_duration(remaining, lang),
            )
        )
        if level > 1:
            lines.append(
                t["oven_level_line"].format(level=level, max_level=OVEN_UPGRADE_MAX_LEVEL)
            )
        builder.button(
            text=t["oven_button_baking"].format(emoji=recipe["emoji"], percent=percent),
            callback_data=f"bakery:info:{oven_index}",
            style="primary",
        )
        if can_upgrade:
            builder.button(
                text=t["upgrade_oven_button"].format(cost=OVEN_UPGRADE_COST[level + 1]),
                callback_data=f"bakery:upgrade:{oven_index}",
                style="primary",
                icon_custom_emoji_id=OVEN_UPGRADE_EMOJI_ID,
            )
            row_sizes.append(2)
        else:
            row_sizes.append(1)
        lines.append("")

    # Навигация по страницам печей — показывается, только если страниц
    # больше одной (см. _build_recipe_choice — тот же паттерн).
    nav_count = 0
    if page > 0:
        builder.button(
            text=t["page_prev_button"],
            callback_data=f"bakery:ovenpage:{page - 1}",
            style="primary",
            icon_custom_emoji_id=PAGE_PREV_EMOJI_ID,
        )
        nav_count += 1
    if page < total_pages - 1:
        builder.button(
            text=t["page_next_button"],
            callback_data=f"bakery:ovenpage:{page + 1}",
            style="primary",
            icon_custom_emoji_id=PAGE_NEXT_EMOJI_ID,
        )
        nav_count += 1
    if nav_count:
        row_sizes.append(nav_count)

    lines.append(t["showcase_title"])
    if pantry:
        for recipe_id in RECIPE_ORDER:
            count = pantry.get(recipe_id, 0)
            if count <= 0:
                continue
            recipe = RECIPES[recipe_id]
            lines.append(t["showcase_item"].format(emoji=recipe["emoji"], name=recipe["name"][lang], count=count))
        lines.append(t["pantry_hint"])
    else:
        lines.append(t["showcase_empty"])

    text = "\n".join(lines).rstrip()

    builder.button(
        text=t["shop_button"],
        callback_data="bakery:shop",
        style="primary",
        icon_custom_emoji_id=SHOP_BUTTON_EMOJI_ID,
    )
    row_sizes.append(1)
    builder.adjust(*row_sizes)
    return text, builder.as_markup()


def _build_recipe_choice(lang: str, oven_index: int, level: int, page: int = 0) -> tuple[str, object]:
    t = TEXTS[lang]

    total_pages = (len(RECIPE_ORDER) + RECIPES_PER_PAGE - 1) // RECIPES_PER_PAGE
    page = max(0, min(page, total_pages - 1))
    start = page * RECIPES_PER_PAGE
    page_recipe_ids = RECIPE_ORDER[start:start + RECIPES_PER_PAGE]

    lines = [t["choose_recipe_title"].format(page=page + 1, total=total_pages), t["separator"]]

    builder = InlineKeyboardBuilder()
    row_sizes = []
    for recipe_id in page_recipe_ids:
        recipe = RECIPES[recipe_id]

        lines.append(
            t["recipe_line"].format(
                emoji=recipe["emoji"],
                name=recipe["name"][lang],
                # Время уже с поправкой на уровень этой печи (см.
                # _effective_bake_seconds) — чтобы игрок видел реальное
                # время ДО начала выпечки, а не базовое время рецепта.
                time=_format_duration(_effective_bake_seconds(recipe_id, level), lang),
                ingredients_line=_format_recipe_requirements(lang, recipe),
            )
        )
        lines.append("")

        builder.button(
            text=t["recipe_button"].format(emoji=recipe["emoji"], name=recipe["name"][lang]),
            callback_data=f"bakery:bake:{oven_index}:{recipe_id}",
            style="primary",
        )
        row_sizes.append(1)

    # Навигация по страницам — показывается, только если страниц больше одной.
    nav_count = 0
    if page > 0:
        builder.button(
            text=t["page_prev_button"],
            callback_data=f"bakery:recipepage:{oven_index}:{page - 1}",
            style="primary",
            icon_custom_emoji_id=PAGE_PREV_EMOJI_ID,
        )
        nav_count += 1
    if page < total_pages - 1:
        builder.button(
            text=t["page_next_button"],
            callback_data=f"bakery:recipepage:{oven_index}:{page + 1}",
            style="primary",
            icon_custom_emoji_id=PAGE_NEXT_EMOJI_ID,
        )
        nav_count += 1
    if nav_count:
        row_sizes.append(nav_count)

    builder.button(
        text=t["back_button"],
        callback_data=f"bakery:back:{oven_index // OVENS_PER_PAGE}",
        style="primary",
        icon_custom_emoji_id=BACK_BUTTON_EMOJI_ID,
    )
    row_sizes.append(1)

    builder.adjust(*row_sizes)

    text = "\n".join(lines).rstrip()
    return text, builder.as_markup()


def _build_shop_view(lang: str, balance: int, ingredients_inv: dict[str, int]) -> tuple[str, object]:
    t = TEXTS[lang]
    lines = [t["shop_title"], t["separator"], t["shop_balance_line"].format(balance=balance), ""]
    lines.append(t["shop_hint"])
    lines.append("")

    builder = InlineKeyboardBuilder()
    for ing_id in INGREDIENT_ORDER:
        ing = INGREDIENTS[ing_id]
        lines.append(
            t["shop_item_line"].format(
                emoji=ing["emoji"],
                name=ing["name"][lang],
                price=ing["price"],
                count=ingredients_inv.get(ing_id, 0),
            )
        )
        builder.button(
            text=t["buy_button"].format(emoji=ing["emoji"], name=ing["name"][lang], price=ing["price"]),
            callback_data=f"bakery:ingredient:{ing_id}",
            style="primary",
            icon_custom_emoji_id=COIN_BUTTON_EMOJI_ID,
        )

    builder.button(
        text=t["back_button"],
        callback_data="bakery:back:0",
        style="primary",
        icon_custom_emoji_id=BACK_BUTTON_EMOJI_ID,
    )
    builder.adjust(1)
    text = "\n".join(lines).rstrip()
    return text, builder.as_markup()


def _quantity_options() -> list[int]:
    return list(QUICK_QUANTITIES)


def _build_qty_choice(lang: str, ingredient_id: str) -> tuple[str, object]:
    t = TEXTS[lang]
    ing = INGREDIENTS[ingredient_id]
    text = t["choose_qty_title"].format(emoji=ing["emoji"], name=ing["name"][lang], price=ing["price"])

    builder = InlineKeyboardBuilder()
    for qty in _quantity_options():
        builder.button(
            text=t["qty_button"].format(qty=qty, total=qty * ing["price"]),
            callback_data=f"bakery:buyqty:{ingredient_id}:{qty}",
            style="primary",
            icon_custom_emoji_id=COIN_BUTTON_EMOJI_ID,
        )
    builder.button(
        text=t["qty_custom_button"],
        callback_data=f"bakery:buycustom:{ingredient_id}",
        style="primary",
        icon_custom_emoji_id=QTY_CUSTOM_BUTTON_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data="bakery:shop",
        style="primary",
        icon_custom_emoji_id=BACK_BUTTON_EMOJI_ID,
    )
    builder.adjust(len(_quantity_options()), 1, 1)
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
    user_id = message_or_callback.from_user.id
    ovens = await _get_ovens(user_id)
    pantry = await get_pantry(user_id)
    unlocked_extra = await _get_unlocked_extra_ovens(user_id)
    levels = await _get_oven_levels(user_id)
    text, markup = _build_bakery_view(lang, ovens, pantry, page, unlocked_extra, levels)

    # Картинка раздела (см. admin.py: admin:sections, ключ "bakery") —
    # если задана, экран пекарни отправляется/правится как фото с
    # текстом в подписи, иначе как обычно текстом. Локальный импорт —
    # admin.py сам импортирует bakery.py на верхнем уровне (цикл).
    import admin

    if edit:
        await admin.smart_edit(message_or_callback.message, text, reply_markup=markup)
        sender = message_or_callback.message.answer
    else:
        await admin.send_with_section_image(message_or_callback, "bakery", text, reply_markup=markup)
        sender = message_or_callback.answer

    # "Обе печи в деле" / "Ни минуты простоя" / "Из сада в печь" — не
    # привязаны к конкретному действию, проверяются при каждой
    # отрисовке главного экрана (см. _check_bakery_achievements).
    achv_ids = await _check_bakery_achievements(user_id, ovens)
    await _notify_achievements(sender, user_id, lang, achv_ids)


async def _render_shop_and_send(callback: CallbackQuery, lang: str) -> None:
    balance = await shop.get_balance(callback.from_user.id)
    ingredients_inv = await get_ingredients(callback.from_user.id)
    text, markup = _build_shop_view(lang, balance, ingredients_inv)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=markup)


# ==========================
#   ХЕНДЛЕРЫ
# ==========================

@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_bakery(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    await _render_and_send(message, lang, edit=False)


@router.callback_query(F.data.startswith("bakery:back:"))
async def on_back_to_bakery(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    page = int(callback.data.split(":")[2])
    await _render_and_send(callback, lang, edit=True, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("bakery:ovenpage:"))
async def on_oven_page(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    page = int(callback.data.split(":")[2])
    await _render_and_send(callback, lang, edit=True, page=page)
    await callback.answer()


@router.callback_query(F.data.startswith("bakery:unlock:"))
async def on_unlock_oven(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    oven_index = int(callback.data.split(":")[2])

    status = await unlock_oven(callback.from_user.id, oven_index)
    if status == "not_enough":
        await callback.answer(t["unlock_oven_not_enough_toast"], show_alert=True)
        return
    if status == "already":
        await callback.answer(t["unlock_oven_already_toast"])
    elif status == "ok":
        await callback.answer(t["unlocked_oven_toast"], show_alert=True)
    else:
        await callback.answer()
        return

    await _render_and_send(callback, lang, edit=True, page=oven_index // OVENS_PER_PAGE)


@router.callback_query(F.data.startswith("bakery:upgrade:"))
async def on_upgrade_oven(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    oven_index = int(callback.data.split(":")[2])

    # Подстраховка от протухшей клавиатуры — см. on_choose_recipe.
    unlocked_extra = await _get_unlocked_extra_ovens(callback.from_user.id)
    if not _is_oven_unlocked(oven_index, unlocked_extra):
        await callback.answer()
        return

    status = await upgrade_oven(callback.from_user.id, oven_index)
    if status == "not_enough":
        await callback.answer(t["upgrade_oven_not_enough_toast"], show_alert=True)
        return
    if status == "busy":
        await callback.answer(t["upgrade_oven_busy_toast"], show_alert=True)
        return
    if status == "max_level":
        await callback.answer(t["upgrade_oven_max_toast"], show_alert=True)
        return

    new_level = await _get_single_oven_level(callback.from_user.id, oven_index)
    percent = _oven_speedup_percent(new_level)
    await callback.answer(
        t["upgrade_oven_done_toast"].format(level=new_level, percent=percent), show_alert=True
    )
    await _render_and_send(callback, lang, edit=True, page=oven_index // OVENS_PER_PAGE)


@router.callback_query(F.data.startswith("bakery:choose:"))
async def on_choose_recipe(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    oven_index = int(callback.data.split(":")[2])

    # Подстраховка от протухшей клавиатуры: если печь платная и ещё не
    # открыта, печь в ней нельзя — экран выбора рецепта не открываем.
    unlocked_extra = await _get_unlocked_extra_ovens(callback.from_user.id)
    if not _is_oven_unlocked(oven_index, unlocked_extra):
        await callback.answer()
        return

    level = await _get_single_oven_level(callback.from_user.id, oven_index)
    text, markup = _build_recipe_choice(lang, oven_index, level, page=0)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("bakery:recipepage:"))
async def on_recipe_page(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    _, _, oven_index_str, page_str = callback.data.split(":")
    oven_index = int(oven_index_str)
    page = int(page_str)

    level = await _get_single_oven_level(callback.from_user.id, oven_index)
    text, markup = _build_recipe_choice(lang, oven_index, level, page=page)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("bakery:info:"))
async def on_oven_info(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    oven_index = int(callback.data.split(":")[2])
    ovens = await _get_ovens(callback.from_user.id)
    oven = next((o for o in ovens if o["oven_index"] == oven_index), None)

    if oven is None or oven["recipe_id"] is None:
        await callback.answer()
        return

    recipe = RECIPES[oven["recipe_id"]]
    level = await _get_single_oven_level(callback.from_user.id, oven_index)
    now = time.time()
    elapsed = now - oven["started_at"]
    bake_seconds = _effective_bake_seconds(oven["recipe_id"], level)
    percent = round(elapsed / bake_seconds * 100)
    remaining = bake_seconds - elapsed

    await callback.answer(
        t["info_alert"].format(
            emoji=recipe["emoji"],
            name=recipe["name"][lang],
            bar=_render_bar(percent),
            percent=percent,
            time=_format_duration(remaining, lang),
        ),
        show_alert=True,
    )


@router.callback_query(F.data.startswith("bakery:bake:"))
async def on_bake(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    _, _, oven_index_str, recipe_id = callback.data.split(":")
    oven_index = int(oven_index_str)
    recipe = RECIPES[recipe_id]

    # Подстраховка от протухшей клавиатуры — см. on_choose_recipe.
    unlocked_extra = await _get_unlocked_extra_ovens(callback.from_user.id)
    if not _is_oven_unlocked(oven_index, unlocked_extra):
        await callback.answer()
        return

    level = await _get_single_oven_level(callback.from_user.id, oven_index)

    result = await start_baking(callback.from_user.id, oven_index, recipe_id, lang)

    if result is None:
        await callback.answer(t["oven_taken_toast"], show_alert=True)
        return
    if result == "not_enough":
        await callback.answer(t["not_enough_toast"], show_alert=True)
        return

    started_at = result
    _schedule_auto_bake(callback.bot, callback.from_user.id, oven_index, recipe_id, started_at, lang, level)

    bake_seconds = _effective_bake_seconds(recipe_id, level)
    await callback.answer(
        t["baking_started_toast"].format(
            emoji=recipe["emoji"],
            name=recipe["name"][lang],
            time=_format_duration(max(0.0, bake_seconds - (time.time() - started_at)), lang),
        ),
        show_alert=True,
    )
    await _render_and_send(callback, lang, edit=True, page=oven_index // OVENS_PER_PAGE)


@router.callback_query(F.data.startswith("bakery:feed:"))
async def on_feed_panda(callback: CallbackQuery, state: FSMContext) -> None:
    """Пекарня больше не кормит панду напрямую — только продажа/лоты на
    рынке (см. shop.py) или выпечка новых изделий. Кормление теперь
    целиком живёт в разделе "Моя панда" (см. panda.py: panda:feed_item:*).
    Этот хендлер оставлен как безопасный редирект на случай, если у
    игрока в чате осталась старая клавиатура с кнопкой кормления,
    отрисованная до этого изменения."""
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    await callback.answer(t["feed_redirect_toast"], show_alert=True)
    await _render_and_send(callback, lang, edit=True)


# --- лавка ингредиентов ---

@router.callback_query(F.data == "bakery:shop")
async def on_open_shop(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    await _render_shop_and_send(callback, lang)
    await callback.answer()


@router.callback_query(F.data.startswith("bakery:ingredient:"))
async def on_ingredient_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    ingredient_id = callback.data.split(":")[2]

    text, markup = _build_qty_choice(lang, ingredient_id)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("bakery:buyqty:"))
async def on_buy_qty_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    _, _, ingredient_id, qty_str = callback.data.split(":")
    qty = int(qty_str)
    ing = INGREDIENTS[ingredient_id]

    result = await buy_ingredient(callback.from_user.id, ingredient_id, qty)
    if result is None:
        await callback.answer(t["not_enough_pn_toast"], show_alert=True)
        achv_ids = []
    else:
        total, achv_ids = result
        await callback.answer(
            t["bought_toast"].format(emoji=ing["emoji"], name=ing["name"][lang], count=qty, total=total),
            show_alert=True,
        )

    await _render_shop_and_send(callback, lang)
    await _notify_achievements(callback.message.answer, callback.from_user.id, lang, achv_ids)


@router.callback_query(F.data.startswith("bakery:buycustom:"))
async def on_buy_custom_request(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    ingredient_id = callback.data.split(":")[2]
    ing = INGREDIENTS[ingredient_id]

    await state.update_data(bakery_buy_ingredient=ingredient_id)
    await state.set_state(BakeryStates.waiting_quantity)

    await callback.answer()
    await callback.message.answer(t["ask_qty"].format(emoji=ing["emoji"], name=ing["name"][lang]))


def _is_navigation_text(raw: str) -> bool:
    """Похоже ли сообщение на попытку уйти в другой раздел/команду, а не
    на ввод количества. Нужно, чтобы состояние waiting_quantity не
    проглатывало нажатия кнопок меню (см. баг: пекарня/ачивки/донат не
    открывались, пока висело это состояние — сообщение перехватывалось
    здесь и отвечало "Введите число...").

    Локальный импорт main.py — main.py импортирует bakery.py на верхнем
    уровне, поэтому импортировать в обратную сторону можно только внутри
    функции (по аналогии с локальным `import admin` в этом же файле)."""
    if not raw:
        return False
    if raw.startswith("/"):
        return True

    import main

    triggers: set[str] = set()
    for group in (
        main.GARDEN_TRIGGERS,
        main.PROFILE_TRIGGERS,
        main.BAKERY_TRIGGERS,
        main.MARKET_TRIGGERS,
        main.PANDA_TRIGGERS,
        main.LEADERS_TRIGGERS,
        main.DONATE_TRIGGERS,
        main.ACHIEVEMENTS_TRIGGERS,
    ):
        triggers.update(group)

    button_texts: set[str] = set()
    for lang_texts in main.TEXTS.values():
        for key, value in lang_texts.items():
            if key.startswith("menu_") and key != "menu_opened":
                button_texts.add(value)
    button_texts.update(BUTTON_TEXT.values())

    normalized = raw.strip().lower()
    return normalized in triggers or raw.strip() in button_texts


@router.message(StateFilter(BakeryStates.waiting_quantity))
async def on_custom_qty_received(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    t = TEXTS[lang]

    data = await state.get_data()
    ingredient_id = data.get("bakery_buy_ingredient")
    if ingredient_id is None:
        await state.set_state(None)
        return

    raw = (message.text or "").strip()

    # Пользователь ушёл в другой раздел / ввёл команду, не закончив
    # ввод количества — сбрасываем зависшее состояние и отдаём апдейт
    # дальше по цепочке роутеров, чтобы сработал нужный хендлер
    # (open_bakery / donate.py / achives.py и т.д.), а не наш "неверное
    # число".
    if _is_navigation_text(raw):
        await state.set_state(None)
        raise SkipHandler

    if not raw.isdigit() or not (1 <= int(raw) <= MAX_BUY_QUANTITY):
        await message.answer(t["qty_invalid"].format(max=MAX_BUY_QUANTITY))
        return

    qty = int(raw)
    await state.set_state(None)
    ing = INGREDIENTS[ingredient_id]

    result = await buy_ingredient(message.from_user.id, ingredient_id, qty)
    if result is None:
        await message.answer(t["not_enough_pn_toast"])
        achv_ids = []
    else:
        total, achv_ids = result
        await message.answer(
            t["bought_toast"].format(emoji=ing["emoji"], name=ing["name"][lang], count=qty, total=total)
        )

    balance = await shop.get_balance(message.from_user.id)
    ingredients_inv = await get_ingredients(message.from_user.id)
    text, markup = _build_shop_view(lang, balance, ingredients_inv)
    await message.answer(text, reply_markup=markup)
    await _notify_achievements(message.answer, message.from_user.id, lang, achv_ids)
