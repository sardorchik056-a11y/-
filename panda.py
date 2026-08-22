"""
Раздел "Моя панда".

Игровое время:
    1 реальный день = 5 дней панды -> игровое время идёт в 5 раз быстрее
    реального. 1 игровой час = 720 реальных секунд (86400 / (24 * 5)).

Возраст:
    считается от created_at, в игровых днях/часах.

Голод (0-100%):
    падает в две фазы с момента последнего кормления:
      1) 100% -> 50% за случайные 30-50 реальных минут;
      2) 50% -> 0% за случайные ещё 2-4 реальных часа.
    Длительности обеих фаз выбираются заново при каждом полном
    кормлении (feed_panda) и хранятся в БД (hunger_phase1_seconds /
    hunger_phase2_seconds), поэтому расчёт остаётся чистой функцией
    времени, а не требует фоновых задач.

Настроение и дружба (0-100, обычные "единицы"):
    - Поглаживание: +5 настроения, +3 дружбы. Не больше 3 раз подряд,
      затем 5 реальных минут "отдыха" перед следующей серией.
    - Пока голод < 50% — раз в ~10-15 реальных минут настроение падает
      на 5, а дружба на 3 (тикают одновременно, независимо друг от
      друга не выключаются). Прекращается, как только панду покормят
      и голод снова станет >= 50%.

Все эффекты времени (голод/настроение/дружба) считаются "лениво" —
чистой математикой по таймстампам при каждом обращении, без фоновых
задач и планировщиков, поэтому бот не тормозит независимо от того,
сколько времени прошло между заходами пользователя.

Хранение:
    Общая база данных бота (см. database.py) — единое asyncio-соединение
    на весь процесс, WAL-режим, запись "стопками" (батч-коммиты). Гонки
    между параллельными запросами одного игрока (двойной тап и т.п.)
    закрыты персональным локом — database.user_lock(user_id).

Подключение в main.py:
    import panda
    dp.include_router(panda.router)
    # Один раз при старте, до включения роутера (или сразу после) —
    # лениво создаёт panda_notify_state, panda_achv_state и
    # panda_penalty_state, если их ещё нет:
    await panda.ensure_notify_table()
    await panda.ensure_achv_state_table()
    await panda.ensure_penalty_state_table()
    # Фоновый цикл push-уведомлений "покормите/приласкайте панду" —
    # уже подключается отдельной задачей (см. main.py: main()). Штраф
    # за голодающую на 0% панду — отдельный, более частый фоновый цикл:
    #     asyncio.create_task(panda.start_penalty_loop(bot))

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import asyncio
import html
import logging
import random
import re
import time

import aiosqlite
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import garden
import shop
# Кормить панду теперь можно и готовой выпечкой — единственная точка
# входа для этого во всём боте: раздел "Пекарня" больше не кормит
# напрямую (там только продажа/выставление на рынок), см. bakery.py.
import bakery
import prof
import achives

router = Router(name="panda")


async def _safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
    """edit_text, который не падает, если Telegram считает новые текст и
    разметку буквально идентичными уже показанным (например, поглаживание
    панды, у которой настроение и дружба и так уже на максимуме — сам
    поглаживания счётчик в тексте карточки не отображается, поэтому
    видимо ничего не меняется). Telegram в этом случае отвечает
    "message is not modified", aiogram поднимает TelegramBadRequest —
    здесь эта конкретная ошибка тихо проглатывается, любая другая
    пробрасывается дальше как есть."""
    try:
        await message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise
logger = logging.getLogger(__name__)


# ==========================
#   СОСТОЯНИЯ (FSM)
# ==========================

class PandaStates(StatesGroup):
    waiting_name = State()


# ==========================
#   НАСТРОЙКИ / ИГРОВОЕ ВРЕМЯ
# ==========================

# Стикер панды, отправляется перед карточкой раздела
# Стикер молодой панды (возраст <= ADULT_AGE_DAYS дней панды)
PANDA_STICKER_ID = "CAACAgIAAxkBAAEF7q5qcZ2KYLrw_UQSNHup9AJiw5Q61QACDKQAAm3fiEu7AAHrmJtV3qM9BA"
# Стикер взрослой панды (возраст > ADULT_AGE_DAYS дней панды)
PANDA_STICKER_ID_ADULT = "CAACAgIAAxkBAAEF7rRqcZ9gPDNZUQFG8KFl8MWOeKc6NAACzqUAAkFUkUtyBjua9t1D0T0E"
ADULT_AGE_DAYS = 30

# --- скины панды (платные) ---
# Первые 4 в SKIN_ORDER продаются за обычную валюту (shop.CURRENCY),
# остальные — за кристаллы (см. поле "currency" у каждого скина ниже
# и CRYSTAL_EMOJI/prof.get_crystals).
# Каждый скин полностью переопределяет стикер панды (_pick_sticker),
# пока надет, — независимо от возраста.
SKINS = {
    "sun": {
        "sticker_id": "CAACAgIAAxkBAAEGAupqdcz9g5Jm4XF3XD5tpPfxQSJ8ewACyZ4AAo8PsEuWlcc_X-OnCj0E",
        "name": {"ru": "Солнечная панда", "en": "Solar Panda"},
        "description": {
            "ru": "Родилась на рассвете самого длинного дня в году — с тех пор в её шерсти будто застыл кусочек солнца.",
            "en": "Born at dawn on the longest day of the year — a sliver of sunlight has lived in her fur ever since.",
        },
        "price": 15000,
        "currency": "coins",
    },
    "explorer": {
        "sticker_id": "CAACAgIAAxkBAAEGAu5qdc0hmviJ5N8IByImtl94DesAAcEAAmqhAAJM_LBLkUt2aIj0eGU9BA",
        "name": {"ru": "Панда-исследователь", "en": "Explorer Panda"},
        "description": {
            "ru": "Обошла три континента с потрёпанной картой в лапах — и всё ради того самого, единственного бамбука.",
            "en": "She has crossed three continents with a worn map in her paws, chasing one legendary stalk of bamboo.",
        },
        "price": 25000,
        "currency": "coins",
    },
    "scientist": {
        "sticker_id": "CAACAgIAAxkBAAEGAvRqdc1Ogs1kScMWUYc0elHUWb4k2AACGZwAArYTsEvnmD_kES9wHz0E",
        "name": {"ru": "Панда-учёный", "en": "Scientist Panda"},
        "description": {
            "ru": "Ночами не гаснет свет в её маленькой лаборатории — где-то там рождается открытие, которое изменит бамбуковый мир.",
            "en": "The light in her tiny lab never goes out at night — somewhere in there, a discovery is quietly taking shape.",
        },
        "price": 45000,
        "currency": "coins",
    },
    "wild_forest": {
        "sticker_id": "CAACAgIAAxkBAAEGAvJqdc1Bjfo4H7j951XAPqPbZns_zAACUZwAAuh1sUtAVlLT1gr4yT0E",
        "name": {"ru": "Панда дикого леса", "en": "Wild Forest Panda"},
        "description": {
            "ru": "Не признаёт троп и расписаний — чащоба стала для неё домом, а тишина леса — лучшим собеседником.",
            "en": "No paths, no schedules — the thicket became her home, and the forest's silence her closest companion.",
        },
        "price": 75000,
        "currency": "coins",
    },
    "water_depths": {
        "sticker_id": "CAACAgIAAxkBAAEGAuRqdczEtYBM0m2aE7vsrFcbflJi1QACQZoAArO8sUtMffedecjt5j0E",
        "name": {"ru": "Панда водных глубин", "en": "Panda of the Water Depths"},
        "description": {
            "ru": "Выросла у ледяного горного озера, там, где вода помнит каждый шёпот — и до сих пор носит в глазах его глубину.",
            "en": "Raised by a glacial mountain lake, where the water remembers every whisper — its depths still linger in her eyes.",
        },
        "price": 900,
        "currency": "crystals",
    },
    "ninja": {
        "sticker_id": "CAACAgIAAxkBAAEGAvBqdc0vsSCVk7CLJO9tebm5wlWHKAACd7MAAqy5sUs8wn-rCgJ92z0E",
        "name": {"ru": "Панда-ниндзя", "en": "Ninja Panda"},
        "description": {
            "ru": "Училась у теней в горной школе — и теперь сама умеет исчезать так, что даже ветер её не выдаёт.",
            "en": "Trained by shadows in a mountain school — now she vanishes so completely that even the wind won't give her away.",
        },
        "price": 1300,
        "currency": "crystals",
    },
    "knight": {
        "sticker_id": "CAACAgIAAxkBAAEGAvZqdc1ZwQf4JUb-xfxvs2TTM_lLfwACNawAAluNsUtvpz44vnD_PD0E",
        "name": {"ru": "Панда-рыцарь", "en": "Knight Panda"},
        "description": {
            "ru": "Однажды поклялась беречь бамбуковую рощу — и с тех пор носит доспехи так, будто родилась в них.",
            "en": "She once swore to guard the bamboo grove — and has worn her armor ever since as if born in it.",
        },
        "price": 1800,
        "currency": "crystals",
    },
    "forest_lord": {
        "sticker_id": "CAACAgIAAxkBAAEGAuhqdczsEyszyQX-KLuYg6wgblqLfQACr5oAAslIqUslkyuofeHMpT0E",
        "name": {"ru": "Панда — повелитель леса", "en": "Lord of the Forest Panda"},
        "description": {
            "ru": "Знает каждое дерево по имени, а корни склоняются перед ней первыми — лес слушает её раньше, чем она заговорит.",
            "en": "She knows every tree by name, and the roots bow to her first — the forest listens before she even speaks.",
        },
        "price": 2400,
        "currency": "crystals",
    },
    "king_of_beasts": {
        "sticker_id": "CAACAgIAAxkBAAEGAuxqdc0SWmFHJwvkrsFlC3pU6B_6sgACo6sAAnnysEu8Q7yWb3i0WT0E",
        "name": {"ru": "Панда — король зверей", "en": "King of Beasts Panda"},
        "description": {
            "ru": "Начинала как обычный детёныш среди сотен других — а закончила тем, что весь лес признал в ней вожака.",
            "en": "She began as just another cub among hundreds — and ended up leading the entire forest as its king.",
        },
        "price": 3100,
        "currency": "crystals",
    },
    "mage": {
        "sticker_id": "CAACAgIAAxkBAAEGAvpqdc1ia7IpRzz0Olo6wpseneIkBwACjKIAAjAsqUufDB5XndoKCz0E",
        "name": {"ru": "Панда-маг", "en": "Mage Panda"},
        "description": {
            "ru": "Хранит древние заклинания бамбуковых мудрецов — говорят, одним взмахом лапы способна поднять целую рощу.",
            "en": "Keeper of the bamboo sages' oldest spells — legend says a single wave of her paw can raise an entire grove.",
        },
        "price": 3800,
        "currency": "crystals",
    },
    "king": {
        "sticker_id": "CAACAgIAAxkBAAEGAuZqdczZzhbipWFTnZzyX7v8F4cregAC-KcAA8ewS_k_I8_qUDs0PQQ",
        "name": {"ru": "Панда-король", "en": "King Panda"},
        "description": {
            "ru": "На рассвете старейшины возложили корону на её голову — и с тех пор лес кланяется ей первым.",
            "en": "At dawn, the elders placed the crown upon her head — and the forest has bowed to her first ever since.",
        },
        "price": 4400,
        "currency": "crystals",
    },
    "demon": {
        "sticker_id": "CAACAgIAAxkBAAEGAvxqdc1tM75U9Y-WvKJeYX6YsaFbPgAC_qYAAkLUsUv-boGGJ2Qj6z0E",
        "name": {"ru": "Панда-демон", "en": "Demon Panda"},
        "description": {
            "ru": "Пришла из самой тёмной легенды леса — но, к счастью для всех, бамбук она любит куда больше хаоса.",
            "en": "She stepped straight out of the forest's darkest legend — but luckily, she loves bamboo far more than chaos.",
        },
        "price": 5000,
        "currency": "crystals",
    },
}

# Порядок показа в каталоге — от дешёвых к дорогим.
SKIN_ORDER = [
    "sun", "explorer", "scientist", "wild_forest",
    "water_depths", "ninja", "knight",
    "forest_lord", "king_of_beasts", "mage",
    "king", "demon",
]

SECONDS_IN_DAY = 86400
PANDA_DAYS_PER_REAL_DAY = 5  # 1 реальный день = 5 дней панды
GAME_HOUR_SECONDS = SECONDS_IN_DAY / (24 * PANDA_DAYS_PER_REAL_DAY)  # 720 сек

# --- голод (два фона: 100%->50%, затем 50%->0%) ---
# Длительности каждой фазы случайны и перебрасываются заново при каждом
# полном кормлении (feed_panda) — конкретные значения для текущего
# "цикла" хранятся в БД, в hunger_phase1_seconds / hunger_phase2_seconds.
HUNGER_PHASE1_MIN_SECONDS = 30 * 60   # 30 мин
HUNGER_PHASE1_MAX_SECONDS = 50 * 60   # 50 мин
HUNGER_PHASE2_MIN_SECONDS = 2 * 3600  # 2 ч
HUNGER_PHASE2_MAX_SECONDS = 4 * 3600  # 4 ч
HUNGER_LOW_THRESHOLD = 50  # ниже этого % начинают падать настроение и дружба

# --- уровни панды (1-25) ---
# Прокачиваются ВРУЧНУЮ — накопил нужное количество ресурсов (карма /
# чудесный бамбук / роса / волшебный орех — набор зависит от уровня,
# см. PANDA_LEVEL_COST ниже) -> нажал "Повысить уровень" на экране
# "Уровень" (см. level_up_panda / on_open_level / on_level_up). Никакой
# автоматики — всё просто лежит в инвентаре, пока игрок сам не решит
# это потратить.
#
# ВАЖНО: чудесный бамбук — отдельный, особый предмет, никак не связан
# с обычным бамбуком из сада (garden.py, если он там есть) — тот этой
# системой не затрагивается и остаётся как есть. Способ ДОБЫТЬ именно
# чудесный бамбук пока нигде не реализован (скоро будет добавлен
# отдельно) — задел на будущее см. add_wonder_bamboo. Карма/роса/орех
# добываются кликами по "Дереву чудес" (см. click_wonder_tree).
#
# Чем выше уровень — тем дольше длятся обе фазы голода (см.
# hunger_duration_multiplier): линейно от +0% на 1 уровне до +300% на
# 25-м, т.е. на максимуме голод в 4 раза "медленнее".
PANDA_LEVEL_MAX = 25
PANDA_LEVEL_HUNGER_BONUS_MAX_PERCENT = 300

# Стоимость перехода на следующий уровень (ключ — уровень, на который
# переходим) — теперь это НЕ один ресурс, а словарь {ресурс: количество},
# где ресурс — это название колонки в таблице panda (см. database.py:
# PANDA_COLUMNS): "karma" / "wonder_bamboo" / "wonder_dew" / "magic_nut".
# Все перечисленные ресурсы списываются ЗА ОДИН РАЗ при нажатии кнопки
# "Повысить уровень" (см. level_up_panda) — только если хватает КАЖДОГО
# из них.
#
# Резервы под названия ресурсов ЖЁСТКО ЗАФИКСИРОВАНЫ здесь и в
# RESOURCE_EMOJI / RESOURCE_ORDER ниже — никогда не берутся из
# пользовательского ввода, поэтому подстановка названия колонки прямо в
# SQL (см. level_up_panda) безопасна.
#
# Стоимость подобрана так: уровни 2-4 — РОВНО как задал игрок (базовый
# пример: 1000 кармы + 2 бамбука + 1 роса + 2 ореха на 2 уровень, дальше
# по его цифрам). С 5 уровня и до 25-го — плавный рост с бОльшим
# разрывом между уровнями, подобранный так, чтобы РОВНО на 25 уровне
# получить 95 чудесного бамбука, 70 росы, 60 волшебных орехов (у
# каждого свой темп роста в процентах, чтобы точно попасть в цель).
# Карма продолжает расти тем же мягким темпом (~+12% к предыдущему),
# но КАЖДОЕ значение округлено до круглого числа (кратно 50) для
# красоты. Все 4 ресурса на всех 24 уровнях строго возрастают — ни
# единого повтора.
PANDA_LEVEL_COST: dict[int, dict[str, int]] = {
    2: {"karma": 1000, "wonder_bamboo": 2, "wonder_dew": 1, "magic_nut": 2},
    3: {"karma": 2500, "wonder_bamboo": 5, "wonder_dew": 3, "magic_nut": 3},
    4: {"karma": 5000, "wonder_bamboo": 7, "wonder_dew": 4, "magic_nut": 5},
    5: {"karma": 5600, "wonder_bamboo": 8, "wonder_dew": 5, "magic_nut": 6},
    6: {"karma": 6250, "wonder_bamboo": 9, "wonder_dew": 6, "magic_nut": 7},
    7: {"karma": 7000, "wonder_bamboo": 10, "wonder_dew": 7, "magic_nut": 8},
    8: {"karma": 7850, "wonder_bamboo": 11, "wonder_dew": 8, "magic_nut": 9},
    9: {"karma": 8800, "wonder_bamboo": 12, "wonder_dew": 9, "magic_nut": 10},
    10: {"karma": 9850, "wonder_bamboo": 14, "wonder_dew": 10, "magic_nut": 11},
    11: {"karma": 11050, "wonder_bamboo": 16, "wonder_dew": 11, "magic_nut": 12},
    12: {"karma": 12400, "wonder_bamboo": 18, "wonder_dew": 13, "magic_nut": 14},
    13: {"karma": 13900, "wonder_bamboo": 20, "wonder_dew": 15, "magic_nut": 16},
    14: {"karma": 15550, "wonder_bamboo": 23, "wonder_dew": 17, "magic_nut": 18},
    15: {"karma": 17400, "wonder_bamboo": 26, "wonder_dew": 19, "magic_nut": 20},
    16: {"karma": 19500, "wonder_bamboo": 29, "wonder_dew": 22, "magic_nut": 23},
    17: {"karma": 21850, "wonder_bamboo": 33, "wonder_dew": 25, "magic_nut": 26},
    18: {"karma": 24450, "wonder_bamboo": 37, "wonder_dew": 29, "magic_nut": 29},
    19: {"karma": 27400, "wonder_bamboo": 42, "wonder_dew": 33, "magic_nut": 33},
    20: {"karma": 30700, "wonder_bamboo": 48, "wonder_dew": 38, "magic_nut": 37},
    21: {"karma": 34400, "wonder_bamboo": 54, "wonder_dew": 44, "magic_nut": 42},
    22: {"karma": 38550, "wonder_bamboo": 61, "wonder_dew": 50, "magic_nut": 47},
    23: {"karma": 43200, "wonder_bamboo": 69, "wonder_dew": 57, "magic_nut": 53},
    24: {"karma": 48400, "wonder_bamboo": 78, "wonder_dew": 65, "magic_nut": 59},
    25: {"karma": 54200, "wonder_bamboo": 95, "wonder_dew": 70, "magic_nut": 60},
}

# Порядок отображения ресурсов на экране "Уровень" и в тостах — везде
# фиксированный, не зависит от порядка ключей в PANDA_LEVEL_COST.
RESOURCE_ORDER = ("karma", "wonder_bamboo", "wonder_dew", "magic_nut")
RESOURCE_EMOJI = {
    "karma": "✨",
    "wonder_bamboo": "🎋",
    "wonder_dew": "💧",
    "magic_nut": "🌰",
}


def hunger_duration_multiplier(level: int) -> float:
    """Во сколько раз длиннее обе фазы голода на данном уровне: 1.0 на
    1 уровне, 4.0 (т.е. +300%) на 25-м — линейно между ними."""
    level = max(1, min(PANDA_LEVEL_MAX, level))
    fraction = (level - 1) / (PANDA_LEVEL_MAX - 1)
    return 1 + fraction * (PANDA_LEVEL_HUNGER_BONUS_MAX_PERCENT / 100)


def next_level_cost(level: int) -> dict[str, int] | None:
    """Сколько каждого ресурса нужно накопить в инвентаре, чтобы вручную
    поднять уровень с level на level+1 (словарь {ресурс: количество}) —
    None, если панда уже на максимальном (PANDA_LEVEL_MAX) уровне."""
    if level >= PANDA_LEVEL_MAX:
        return None
    return PANDA_LEVEL_COST[level + 1]


# --- "Дерево чудес" (клик-механика, см. click_wonder_tree) ---
# За каждый клик по дереву — ровно один из четырёх исходов, шансы не
# пересекаются и идут в фиксированном порядке (бамбук -> роса -> орех
# -> карма как "утешительный приз" на оставшуюся вероятность):
#   5% — чудесный бамбук (тратится на уровень, см. level_up_panda);
#   5% — роса;
#   5% — волшебный орех;
#   85% (всё, что не выпало выше) — 10-50 кармы.
# Роса и волшебный орех сейчас только копятся в инвентаре — им, как и
# чудесному бамбуку, предстоит уйти на будущую прокачку уровня панды,
# но конкретный рецепт трат пока не решён. Карма — вовсе просто
# счётчик-заглушка без применения в игре.
TREE_BAMBOO_CHANCE = 0.05
TREE_DEW_CHANCE = 0.05
TREE_NUT_CHANCE = 0.05
TREE_KARMA_MIN = 10
TREE_KARMA_MAX = 50

# Минимальный интервал между засчитанными кликами по дереву (антиспам —
# см. on_tree_click). Telegram ограничивает частоту редактирования ОДНОГО
# и того же сообщения примерно 1 разом в секунду — если жать по кнопке
# быстрее, editMessageText начинает падать с "Too Many Requests". Клики
# чаще этого интервала просто игнорируются (см. _tree_click_last_ts) —
# ни БД, ни сообщение не трогаем, только короткий тост без анимации,
# чтобы это не выглядело как зависание кнопки.
TREE_CLICK_COOLDOWN_SECONDS = 0.4

# --- настроение и дружба (падают, пока голод < HUNGER_LOW_THRESHOLD) ---
# Тик — раз в 10-15 реальных минут. Поскольку эффект должен считаться
# "лениво" по одним лишь таймстампам (без фоновых задач), берём
# постоянный интервал тика — среднее 12.5 мин между 10 и 15 — это даёт
# то же практическое поведение ("примерно каждые 10-15 минут"), но
# остаётся чистой функцией времени.
MOOD_FRIEND_DECAY_TICK_SECONDS = 12.5 * 60
MOOD_DECAY_AMOUNT = 5
MOOD_MAX = 100
MOOD_DEFAULT = 100

FRIEND_DECAY_AMOUNT = 3
FRIEND_MAX = 100
FRIEND_DEFAULT = 0

# --- проактивные push-уведомления "панда голодна/грустит" ---
# Раньше это напоминание слалось реактивно, отдельным сообщением, почти
# после каждого действия в разделе "Моя панда" (кормление, глажка,
# открытие раздела и т.д.) — из-за этого при нескольких действиях подряд
# пользователь получал несколько одинаковых сообщений за пару секунд.
# Теперь вместо этого раз в NOTIFY_INTERVAL_SECONDS фоновый цикл
# (см. start_notify_loop) сам проверяет всех игроков и присылает
# уведомление ТОЛЬКО В МОМЕНТ пересечения порога — не при каждой
# проверке подряд, пока панда остаётся голодной/грустной (см. таблицу
# panda_notify_state и check_and_notify).
NOTIFY_INTERVAL_SECONDS = 2 * 3600  # раз в 2 часа
NOTIFY_HUNGER_THRESHOLD = 50   # присылаем, если голод ниже 50%
NOTIFY_MOOD_THRESHOLD = 25     # присылаем, если настроение ниже 25%

# --- штраф за голодающую панду (голод держится на 0%) ---
# Если панда непрерывно голодает на 0% HUNGER_ZERO_PENALTY_START_HOURS
# часов подряд — начинается штраф HUNGER_ZERO_PENALTY_AMOUNT Pn, и
# затем повторяется каждые HUNGER_ZERO_PENALTY_INTERVAL_HOURS час,
# пока голод так и остаётся на 0% (панду не покормили). До первого
# штрафа игрок получает два предупреждения — в моменты
# HUNGER_ZERO_WARNING_HOURS часов непрерывного голода на 0% (то есть
# за 2 и за 1 час до первого штрафа).
#
# Момент, когда голод падает до 0%, не хранится отдельной меткой в
# БД — он и так однозначно считается по формуле голода (см.
# calc_hunger_percent/_hunger_percent_to_elapsed):
#   t_zero = last_fed_at + hunger_phase1_seconds + hunger_phase2_seconds
# Дальше "сколько часов панда уже голодает на 0%" — просто
# (now - t_zero) / 3600, чистая функция времени, как и всё остальное
# в этом модуле (см. докстринг файла). Реальное состояние (какие
# предупреждения уже отправлены, сколько штрафных "тиков" уже списано
# за текущий цикл голода) хранится в panda_penalty_state — см. ниже.
HUNGER_ZERO_WARNING_HOURS = (3, 4)      # 2-е и 1-е предупреждение
HUNGER_ZERO_PENALTY_START_HOURS = 5     # первый штраф — после 5ч на 0%
HUNGER_ZERO_PENALTY_INTERVAL_HOURS = 1  # затем штраф каждый час
HUNGER_ZERO_PENALTY_AMOUNT = 1000
# Проверяем заметно чаще, чем общий NOTIFY_INTERVAL_SECONDS (2 часа) —
# иначе часовые пороги предупреждений/штрафа срабатывали бы с
# опозданием до двух часов. Пропущенные из-за паузы бота пороги всё
# равно не теряются (см. _check_and_penalize_one — досчитывает по
# факту прошедшего времени), но короткий интервал даёт предупреждениям
# и штрафу приходить вовремя при обычной работе бота.
PENALTY_CHECK_INTERVAL_SECONDS = 15 * 60  # раз в 15 минут

# --- поглаживание ---
PET_MOOD_GAIN = 5
PET_FRIEND_GAIN = 3
PET_MAX_USES = 3
PET_WINDOW_SECONDS = 5 * 60  # 5 минут

# --- имя ---
NAME_MIN_LENGTH = 3
NAME_MAX_LENGTH = 24
# Первая установка имени панде — бесплатна. Каждое следующее (платное)
# переименование стоит вдвое дороже предыдущего: 1000, 2000, 4000, ...
# (см. rename_cost() — считает по name_changes, сколько платных
# переименований уже было оплачено).
RENAME_BASE_COST = 1000

# --- кастомные эмодзи ---
NAME_EMOJI_ID = "5344057622628671718"
AGE_EMOJI_ID = "5452055425690123301"
MOOD_EMOJI_ID = "5388790256772331442"
FRIEND_EMOJI_ID = "5341581827385599962"
PET_BUTTON_EMOJI_ID = "5224346382894115159"
SETNAME_BUTTON_EMOJI_ID = "5372848794163486495"
# Стрелка "назад" — на ВСЕХ инлайн-кнопках "Назад" (экраны уровня,
# дерева, кормления и т.д. — используют общий text=t["back_button"]).
BACK_BUTTON_EMOJI_ID = "6039539366177541657"  # ⬅️
# Стрелка вверх — на кнопке "Повысить уровень" экрана "Уровень".
LEVEL_UP_BUTTON_EMOJI_ID = "5449683594425410231"  # 🔼
# Ёлка (тема "Дерева чудес") — на кнопке клика по дереву.
TREE_CLICK_BUTTON_EMOJI_ID = "5235703350166563973"  # 🎄
# Стрелка вверх и галочка — в тексте (не в кнопке) экрана "Уровень
# панды": заголовок и строка текущего уровня.
LEVEL_TITLE_EMOJI_ID = "5463122435425448565"  # ⬆️
BAMBOO_EMOJI_ID = "6035383199339648803"  # ✅ — для чудесного бамбука
# Замок на кнопке некупленного скина в реплай-меню "Облики"
SKIN_LOCKED_EMOJI_ID = "5296369303661067030"
# Мешок с монетами — теперь в строке баланса ("Баланс: ..."), НЕ в
# кнопках: это HTML-эмодзи, кнопки такое не рендерят (см. SKIN_BAG_EMOJI ниже)
SKIN_BAG_EMOJI_ID = "5224257782013769471"
# Эмодзи для строки цены в карточке скина ("Цена: ...") — отдельный от
# мешка баланса выше.
SKIN_PRICE_EMOJI_ID = "5287231198098117669"
# Эмодзи-монета для кнопки "Купить" (скины за обычную валюту) — тот же
# кастомный эмодзи, что и в shop.CURRENCY (валюта везде в боте одна,
# отдельный ID под неё не нужен; кнопки icon_custom_emoji_id принимают
# именно ID, а не HTML-тег, поэтому вытаскиваем ID регуляркой из
# <tg-emoji emoji-id="...">).
_shop_currency_id_match = re.search(r'emoji-id="(\d+)"', shop.CURRENCY)
SKIN_BUY_COIN_EMOJI_ID = _shop_currency_id_match.group(1) if _shop_currency_id_match else None
# Эмодзи-кристалл — вторая, "премиальная" валюта: за неё продаются
# скины дороже 4-го по счёту (см. SKINS ниже, поле "currency").
CRYSTAL_EMOJI_ID = "5251273203615031474"  # 🎁
SKIN_BUY_CRYSTAL_EMOJI_ID = CRYSTAL_EMOJI_ID
# Эмодзи речи/описания — перед текстом истории скина в карточке
SKIN_DESC_EMOJI_ID = "5443038326535759644"
# Стрелка "назад" на кнопке реплай-меню "Облики"
SKINS_MENU_BACK_EMOJI_ID = "5255703720078879038"  # 🔙
# Галочка "надето" — заменяет обычный кружок 🟢 и в статусе карточки
# скина, и в тосте после экипировки.
SKIN_EQUIPPED_EMOJI_ID = "5798517767304912593"  # ✔️
# Крестик "снять" — заменяет обычный кружок 🔴 на кнопке снятия скина.
SKIN_UNEQUIP_EMOJI_ID = "5210952531676504517"  # ❌

NAME_EMOJI = f'<tg-emoji emoji-id="{NAME_EMOJI_ID}">🐼</tg-emoji>'
AGE_EMOJI = f'<tg-emoji emoji-id="{AGE_EMOJI_ID}">🎂</tg-emoji>'
MOOD_EMOJI = f'<tg-emoji emoji-id="{MOOD_EMOJI_ID}">😊</tg-emoji>'
FRIEND_EMOJI = f'<tg-emoji emoji-id="{FRIEND_EMOJI_ID}">🤝</tg-emoji>'
SKIN_BAG_EMOJI = f'<tg-emoji emoji-id="{SKIN_BAG_EMOJI_ID}">💰</tg-emoji>'
SKIN_PRICE_EMOJI = f'<tg-emoji emoji-id="{SKIN_PRICE_EMOJI_ID}">💰</tg-emoji>'
SKIN_DESC_EMOJI = f'<tg-emoji emoji-id="{SKIN_DESC_EMOJI_ID}">💬</tg-emoji>'
CRYSTAL_EMOJI = f'<tg-emoji emoji-id="{CRYSTAL_EMOJI_ID}">🎁</tg-emoji>'
SKIN_EQUIPPED_EMOJI = f'<tg-emoji emoji-id="{SKIN_EQUIPPED_EMOJI_ID}">✔️</tg-emoji>'
LEVEL_TITLE_EMOJI = f'<tg-emoji emoji-id="{LEVEL_TITLE_EMOJI_ID}">⬆️</tg-emoji>'
BAMBOO_EMOJI = f'<tg-emoji emoji-id="{BAMBOO_EMOJI_ID}">✅</tg-emoji>'
TREE_TITLE_EMOJI = f'<tg-emoji emoji-id="{TREE_CLICK_BUTTON_EMOJI_ID}">🎄</tg-emoji>'

# RESOURCE_EMOJI (см. выше) объявлен раньше кастомных эмодзи-констант.
# ВАЖНО: RESOURCE_EMOJI используется и в тостах (callback.answer,
# show_alert=True) — а тосты НЕ поддерживают HTML/кастомные эмодзи,
# только обычный юникод-текст. Поэтому RESOURCE_EMOJI НЕ трогаем
# (бамбук там остаётся обычным 🎋), а для отображения В ТЕКСТЕ
# СООБЩЕНИЯ (экран "Уровень", поддерживает HTML) заводим отдельную
# копию с кастомным ✅ для бамбука — см. RESOURCE_EMOJI_RICH.
RESOURCE_EMOJI_RICH = dict(RESOURCE_EMOJI)
RESOURCE_EMOJI_RICH["wonder_bamboo"] = BAMBOO_EMOJI



# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

BUTTON_TEXT = {
    "ru": "Моя панда",
    "en": "My panda",
}

# Текст кнопки "Облики" в главном реплай-меню (main.py: TEXTS[..]["menu_looks"])
# — продублирован буквально, как и BUTTON_TEXT выше для "Моя панда".
LOOKS_BUTTON_TEXT = {
    "ru": "Облики",
    "en": "Looks",
}

TEXTS = {
    "ru": {
        "default_title": f"{NAME_EMOJI} <b>Моя панда</b>",
        "named_title": f"{NAME_EMOJI} <b>{{name}}</b>",
        "age_label": f"{AGE_EMOJI} <b>Возраст</b>",
        "age_value": "{days} дн. {hours} ч.",
        "level_label": "🎋 <b>Уровень</b>",
        "level_value": "{level}/25",
        "hunger_label": "🍖 <b>Голод</b>",
        "mood_label": f"{MOOD_EMOJI} <b>Настроение</b>",
        "friendship_label": f"{FRIEND_EMOJI} <b>Дружба</b>",
        "hunger_tier_full": "Я сыта и довольна!",
        "hunger_tier_ok": "Слегка проголодалась…",
        "hunger_tier_low": "Не откажусь перекусить",
        "hunger_tier_zero": "Очень хочу есть!",
        "mood_tier_great": "Мне сегодня отлично!",
        "mood_tier_good": "У меня хорошее настроение",
        "mood_tier_meh": "Настроение так себе…",
        "mood_tier_bad": "Мне немного грустно…",
        "friend_tier_best": "Мы неразлучны!",
        "friend_tier_strong": "Мы крепко дружим",
        "friend_tier_new": "Мы только знакомимся",
        "friend_tier_none": "Дружба ещё впереди",
        "call_to_feed": "🆘 <i>Скорее покормите панду — она совсем проголодалась!</i>",
        "call_to_pet": "💔 <i>Панде грустно, приласкайте её.</i>",
        "feed_button": "🍖 Покормить",
        "pet_button": "Погладить",
        "setname_button": "Дать имя",
        "rename_button": "Переименовать",
        "cancel_button": "Отменить",
        "rename_cancelled": "<i>Переименование отменено.</i>",
        "feed_choice_title": "🧺 <b>Чем покормить панду?</b>\n<i>Выберите что-нибудь из корзины сада или витрины пекарни.</i>",
        "feed_item_button": "{emoji} {name} ×{count}",
        "back_button": "Назад",
        "fed_toast": "{emoji} {name} — голод +{restore}%! 🍖",
        "already_full_toast": "Панда пока сыта, рано кормить.",
        "empty_basket_toast": "🧺 Нет запасов на корм — соберите фрукты в саду или испеките что-нибудь в пекарне.",
        "pet_toast": "Панде приятно! 🤗",
        "pet_cooldown_toast": "Панда устала от ласки, попробуйте через {minutes} мин.",
        "ask_name_free": "<i>✏️ Отправьте имя для панды одним сообщением (от {min_len} до {max_len} символов). Первая установка имени — бесплатно!</i>",
        "ask_name_paid": "<i>✏️ Отправьте новое имя для панды одним сообщением (от {min_len} до {max_len} символов).\n💰 Переименование стоит {cost} {currency}.</i>",
        "name_saved": "<i>Готово! Теперь панду зовут <b>{name}</b> 🐼</i>",
        "name_invalid": "<i>Имя должно быть от {min_len} до {max_len} символов — попробуйте ещё раз.</i>",
        "name_insufficient": "<i>Недостаточно {currency} для переименования — нужно {cost}.</i>",
        "skins_title": "🎨 <b>Облики панды</b>\n<i>Выберите скин, чтобы посмотреть его поближе.</i>",
        "skins_balance_line": f"{SKIN_BAG_EMOJI} <b>Баланс: {{coin_currency}} {{coin_balance}}  ·  {{crystal_currency}} {{crystal_balance}}</b>",
        "skin_catalog_locked": "{name}",
        "skin_catalog_owned": "{name}",
        "skin_catalog_equipped": "{name}",
        "skins_menu_back_button": "Назад",
        "skin_status_equipped": f"{SKIN_EQUIPPED_EMOJI} <b>Этот скин сейчас надет</b>",
        "skin_status_owned": "✅ <b>Скин куплен</b>",
        "skin_price_line": f"{SKIN_PRICE_EMOJI} <b>Цена: {{price}} {{currency}}</b>",
        "buy_button": "{price}",
        "wear_button": "🟢 Надеть",
        "unequip_button": "Снять",
        "skin_bought_toast": "✅ Скин куплен!",
        "skin_insufficient_toast": "Недостаточно {currency_word} — нужно {price}.",
        "currency_word_coins": "🪙",
        "currency_word_crystals": "🎁 кристаллов",
        "skin_equipped_toast": "✔️ Скин надет!",
        "skin_unequipped_toast": "Скин снят, панда вернулась к обычному виду.",
        "penalty_warning": (
            "⚠️ <i>Панда уже {hours} ч. голодает на 0%! Если не покормить, "
            "через {hours_left} ч. спишется штраф {amount} {currency}.</i>"
        ),
        "penalty_applied": (
            "💸 <i>Штраф за голодающую панду: −{amount} {currency}. "
            "Покормите её скорее, иначе штраф будет повторяться каждый час!</i>"
        ),
        "level_button": "Уровень",
        "level_screen_title": f"{LEVEL_TITLE_EMOJI} <b>Уровень панды</b>",
        "level_current_line": "<b>Текущий уровень: {level}/25</b>",
        "level_bonus_line": "<i>Бонус к длительности голода: +{bonus}%</i>",
        "level_next_line": "🎋 <b>До {level} уровня:</b>",
        "level_progress_line": "{have}/{need}",
        "level_max_line": "🏆 <b>Достигнут максимальный уровень!</b>",
        "level_have_line": f"В инвентаре: {{have}} {BAMBOO_EMOJI}",
        "res_name_karma": "Карма",
        "res_name_wonder_bamboo": "Чудесный бамбук",
        "res_name_wonder_dew": "Роса",
        "res_name_magic_nut": "Волшебный орех",
        "level_up_button": "Повысить уровень",
        "level_insufficient_toast": "Недостаточно ресурсов — нужно ещё: {need}.",
        "level_up_toast": "🎉 Уровень повышен!",
        "level_up_message": (
            "🎉 <i>Панда достигла <b>{level}</b> уровня! Теперь голод длится "
            "дольше — она сможет обходиться без еды примерно на {bonus}% дольше, чем на 1 уровне.</i>"
        ),
        "tree_button": "Дерево чудес",
        "tree_screen_title": f"{TREE_TITLE_EMOJI} <b>Дерево чудес</b>",
        "tree_intro": "<i>Прикоснитесь к дереву — вдруг оно поделится дарами!</i>",
        "tree_stock_bamboo": f"{BAMBOO_EMOJI} Чудесный бамбук: <b>{{count}}</b>",
        "tree_stock_dew": "💧 Роса: <b>{count}</b>",
        "tree_stock_nut": "🌰 Волшебный орех: <b>{count}</b>",
        "tree_stock_karma": "✨ Карма: <b>{count}</b>",
        "tree_click_button": "Собрать дары",
        "tree_toast_bamboo": "🎋 С дерева упал чудесный бамбук!",
        "tree_toast_dew": "💧 С листьев скатилась капля росы!",
        "tree_toast_nut": "🌰 Среди веток нашёлся волшебный орех!",
        "tree_toast_karma": "✨ +{amount} кармы",
        "tree_click_too_fast": "🌿 Не так быстро!",
    },
    "en": {
        "default_title": f"{NAME_EMOJI} <b>My panda</b>",
        "named_title": f"{NAME_EMOJI} <b>{{name}}</b>",
        "age_label": f"{AGE_EMOJI} <b>Age</b>",
        "age_value": "{days}d {hours}h",
        "level_label": "🎋 <b>Level</b>",
        "level_value": "{level}/25",
        "hunger_label": "🍖 <b>Hunger</b>",
        "mood_label": f"{MOOD_EMOJI} <b>Mood</b>",
        "friendship_label": f"{FRIEND_EMOJI} <b>Friendship</b>",
        "hunger_tier_full": "I'm full and happy!",
        "hunger_tier_ok": "A little peckish…",
        "hunger_tier_low": "Wouldn't mind a snack",
        "hunger_tier_zero": "I'm starving!",
        "mood_tier_great": "I feel amazing today!",
        "mood_tier_good": "I'm in a good mood",
        "mood_tier_meh": "Feeling so-so…",
        "mood_tier_bad": "I feel a bit down…",
        "friend_tier_best": "We're inseparable!",
        "friend_tier_strong": "We have a strong bond",
        "friend_tier_new": "We're just getting acquainted",
        "friend_tier_none": "Our friendship is just beginning",
        "call_to_feed": "🆘 <i>Feed the panda soon — it's starving!</i>",
        "call_to_pet": "💔 <i>The panda feels sad, give it some love.</i>",
        "feed_button": "🍖 Feed",
        "pet_button": "Pet",
        "setname_button": "Set name",
        "rename_button": "Rename",
        "cancel_button": "Cancel",
        "rename_cancelled": "<i>Renaming cancelled.</i>",
        "feed_choice_title": "🧺 <b>What should the panda eat?</b>\n<i>Pick something from the garden basket or the bakery showcase.</i>",
        "feed_item_button": "{emoji} {name} ×{count}",
        "back_button": "Back",
        "fed_toast": "{emoji} {name} — hunger +{restore}%! 🍖",
        "already_full_toast": "The panda is still full, too early to feed.",
        "empty_basket_toast": "🧺 Nothing to feed it with — pick some fruit in the garden or bake something first.",
        "pet_toast": "The panda enjoys it! 🤗",
        "pet_cooldown_toast": "The panda is tired of petting, try again in {minutes} min.",
        "ask_name_free": "<i>✏️ Send a name for the panda in one message ({min_len} to {max_len} characters). First naming is free!</i>",
        "ask_name_paid": "<i>✏️ Send a new name for the panda in one message ({min_len} to {max_len} characters).\n💰 Renaming costs {cost} {currency}.</i>",
        "name_saved": "<i>Done! The panda is now named <b>{name}</b> 🐼</i>",
        "name_invalid": "<i>The name must be {min_len}-{max_len} characters — try again.</i>",
        "name_insufficient": "<i>Not enough {currency} to rename — you need {cost}.</i>",
        "skins_title": "🎨 <b>Panda looks</b>\n<i>Pick a skin to take a closer look.</i>",
        "skins_balance_line": f"{SKIN_BAG_EMOJI} <b>Balance: {{coin_currency}} {{coin_balance}}  ·  {{crystal_currency}} {{crystal_balance}}</b>",
        "skin_catalog_locked": "{name}",
        "skin_catalog_owned": "{name}",
        "skin_catalog_equipped": "{name}",
        "skins_menu_back_button": "Back",
        "skin_status_equipped": f"{SKIN_EQUIPPED_EMOJI} <b>Currently worn</b>",
        "skin_status_owned": "✅ <b>Owned</b>",
        "skin_price_line": f"{SKIN_PRICE_EMOJI} <b>Price: {{price}} {{currency}}</b>",
        "buy_button": "{price}",
        "wear_button": "🟢 Wear",
        "unequip_button": "Take off",
        "skin_bought_toast": "✅ Skin purchased!",
        "skin_insufficient_toast": "Not enough {currency_word} — you need {price}.",
        "currency_word_coins": "🪙",
        "currency_word_crystals": "🎁 crystals",
        "skin_equipped_toast": "✔️ Skin equipped!",
        "skin_unequipped_toast": "Skin removed, the panda is back to normal.",
        "penalty_warning": (
            "⚠️ <i>The panda has been starving at 0% for {hours}h! If not fed, "
            "a {amount} {currency} penalty will be charged in {hours_left}h.</i>"
        ),
        "penalty_applied": (
            "💸 <i>Penalty for a starving panda: −{amount} {currency}. "
            "Feed it soon, or the penalty will repeat every hour!</i>"
        ),
        "level_button": "Level",
        "level_screen_title": f"{LEVEL_TITLE_EMOJI} <b>Panda level</b>",
        "level_current_line": "<b>Current level: {level}/25</b>",
        "level_bonus_line": "<i>Hunger duration bonus: +{bonus}%</i>",
        "level_next_line": "🎋 <b>To level {level}:</b>",
        "level_progress_line": "{have}/{need}",
        "level_max_line": "🏆 <b>Maximum level reached!</b>",
        "level_have_line": f"In inventory: {{have}} {BAMBOO_EMOJI}",
        "res_name_karma": "Karma",
        "res_name_wonder_bamboo": "Wonder bamboo",
        "res_name_wonder_dew": "Dew",
        "res_name_magic_nut": "Magic nut",
        "level_up_button": "Level up",
        "level_insufficient_toast": "Not enough resources — need: {need}.",
        "level_up_toast": "🎉 Level up!",
        "level_up_message": (
            "🎉 <i>The panda reached level <b>{level}</b>! Hunger now lasts "
            "longer — it can go without food about {bonus}% longer than at level 1.</i>"
        ),
        "tree_button": "Tree of Wonders",
        "tree_screen_title": f"{TREE_TITLE_EMOJI} <b>Tree of Wonders</b>",
        "tree_intro": "<i>Touch the tree — maybe it'll share its gifts!</i>",
        "tree_stock_bamboo": f"{BAMBOO_EMOJI} Wonder bamboo: <b>{{count}}</b>",
        "tree_stock_dew": "💧 Dew: <b>{count}</b>",
        "tree_stock_nut": "🌰 Magic nut: <b>{count}</b>",
        "tree_stock_karma": "✨ Karma: <b>{count}</b>",
        "tree_click_button": "Collect the gifts",
        "tree_toast_bamboo": "🎋 A wonder bamboo fell from the tree!",
        "tree_toast_dew": "💧 A drop of dew slid off the leaves!",
        "tree_toast_nut": "🌰 A magic nut turned up among the branches!",
        "tree_toast_karma": "✨ +{amount} karma",
        "tree_click_too_fast": "🌿 Not so fast!",
    },
}


# ==========================
#   ХРАНИЛИЩЕ (общая БД — см. database.py)
# ==========================
#
# Своего соединения и своей таблицы этот модуль больше не создаёт —
# и то, и другое теперь общее для всего бота, в database.py.


async def _fetch_row(db: aiosqlite.Connection, user_id: int) -> aiosqlite.Row | None:
    async with db.execute(
        "SELECT * FROM panda WHERE user_id = ?", (user_id,)
    ) as cursor:
        return await cursor.fetchone()


async def _get_or_create_panda_locked(user_id: int) -> aiosqlite.Row:
    """Как get_or_create_panda, но предполагает, что персональный лок
    пользователя уже захвачен вызывающим кодом (см. database.user_lock).
    INSERT OR IGNORE — на случай, если строка всё же успела появиться
    между SELECT и INSERT (не должно происходить при соблюдении
    дисциплины блокировок, но не будет падать с IntegrityError, если
    вдруг произойдёт)."""
    db = await database.get_db()
    row = await _fetch_row(db, user_id)
    if row is not None:
        return row

    now = time.time()
    phase1, phase2 = _roll_hunger_phases()
    await db.execute(
        """
        INSERT OR IGNORE INTO panda (
            user_id, created_at, last_fed_at, mood, friendship,
            mood_ticks_applied, friend_ticks_applied, pet_window_start, pet_count, name,
            hunger_phase1_seconds, hunger_phase2_seconds
        ) VALUES (?, ?, ?, ?, ?, 0, 0, 0, 0, NULL, ?, ?)
        """,
        (user_id, now, now, MOOD_DEFAULT, FRIEND_DEFAULT, phase1, phase2),
    )
    await database.commit()
    return await _fetch_row(db, user_id)


async def get_or_create_panda(user_id: int) -> aiosqlite.Row:
    """Возвращает строку панды, создавая её при первом обращении."""
    async with database.user_lock(user_id):
        return await _get_or_create_panda_locked(user_id)


# ==========================
#   РАСЧЁТЫ
# ==========================

def calc_age_days(created_at: float, now: float) -> float:
    real_days_elapsed = (now - created_at) / SECONDS_IN_DAY
    return real_days_elapsed * PANDA_DAYS_PER_REAL_DAY


def _roll_hunger_phases(level: int = 1) -> tuple[float, float]:
    """Случайно выбирает длительности двух фаз голода нового цикла
    кормления: HUNGER_PHASE1_* (100%->50%) и HUNGER_PHASE2_* (50%->0%),
    затем растягивает обе на hunger_duration_multiplier(level) — более
    высокий уровень панды (см. PANDA_LEVEL_* выше) держит её сытой
    дольше."""
    phase1 = random.uniform(HUNGER_PHASE1_MIN_SECONDS, HUNGER_PHASE1_MAX_SECONDS)
    phase2 = random.uniform(HUNGER_PHASE2_MIN_SECONDS, HUNGER_PHASE2_MAX_SECONDS)
    multiplier = hunger_duration_multiplier(level)
    return phase1 * multiplier, phase2 * multiplier


def calc_hunger_percent(
    last_fed_at: float,
    now: float,
    phase1_seconds: float = HUNGER_PHASE1_MAX_SECONDS,
    phase2_seconds: float = HUNGER_PHASE2_MAX_SECONDS,
) -> float:
    """Голод падает в две фазы: сперва со 100% до 50% за phase1_seconds,
    затем с 50% до 0% за phase2_seconds. Оба параметра — конкретные
    значения текущего цикла кормления данной панды (см. hunger_phase1_seconds
    / hunger_phase2_seconds в БД), а не глобальные константы."""
    elapsed = now - last_fed_at
    if elapsed <= 0:
        return 100.0
    if elapsed <= phase1_seconds:
        percent = 100 - (elapsed / phase1_seconds) * 50
    else:
        elapsed_phase2 = elapsed - phase1_seconds
        percent = 50 - (elapsed_phase2 / phase2_seconds) * 50
    return max(0.0, min(100.0, percent))


def _hunger_percent_to_elapsed(
    hunger: float, phase1_seconds: float, phase2_seconds: float
) -> float:
    """Обратная функция к calc_hunger_percent: сколько секунд должно было
    пройти с last_fed_at, чтобы голод был равен данному значению."""
    hunger = max(0.0, min(100.0, hunger))
    if hunger >= 50:
        return (100 - hunger) / 50 * phase1_seconds
    return phase1_seconds + (50 - hunger) / 50 * phase2_seconds


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _full_stats_achievement(row: aiosqlite.Row, hunger: float | None = None) -> list[str]:
    """['panda_full_stats'], если голод/настроение/дружба у панды разом
    на 100% прямо сейчас (row — актуальная, уже "уставшаяся" строка,
    как после _settle/feed_panda/restore_hunger/pet_panda), иначе [].

    hunger можно передать явно (см. on_feed_item) — если его не считать
    заново через time.time(), а взять то значение, которое уже точно
    вычислил restore_hunger в момент кормления, голод не успевает
    "утечь" за время между кормлением и проверкой ачивки (см. также
    _current_hunger и баг с недостижимой panda_hunger_100 в on_feed_item)."""
    if hunger is None:
        hunger = calc_hunger_percent(
            row["last_fed_at"], time.time(), row["hunger_phase1_seconds"], row["hunger_phase2_seconds"]
        )
    if hunger >= 100 and row["mood"] >= MOOD_MAX and row["friendship"] >= FRIEND_MAX:
        return ["panda_full_stats"]
    return []


async def _settle_locked(user_id: int) -> aiosqlite.Row:
    """Логика _settle, но без захвата лока — вызывать только когда
    database.user_lock(user_id) уже захвачен вызывающим кодом."""
    db = await database.get_db()
    row = await _get_or_create_panda_locked(user_id)
    now = time.time()

    last_fed_at = row["last_fed_at"]
    phase1_seconds = row["hunger_phase1_seconds"]
    phase2_seconds = row["hunger_phase2_seconds"]
    mood = row["mood"]
    friendship = row["friendship"]
    mood_ticks_applied = row["mood_ticks_applied"]
    friend_ticks_applied = row["friend_ticks_applied"]

    # --- падение настроения и дружбы, пока голод < 50% ---
    # Голод достигает ровно 50% в момент last_fed_at + phase1_seconds
    # (граница между первой и второй фазой голода).
    t_low_hunger = last_fed_at + phase1_seconds
    if now > t_low_hunger:
        eligible_ticks = int((now - t_low_hunger) // MOOD_FRIEND_DECAY_TICK_SECONDS)

        new_mood_ticks = max(0, eligible_ticks - mood_ticks_applied)
        if new_mood_ticks:
            mood = _clamp(mood - new_mood_ticks * MOOD_DECAY_AMOUNT, 0, MOOD_MAX)
            mood_ticks_applied = eligible_ticks

        new_friend_ticks = max(0, eligible_ticks - friend_ticks_applied)
        if new_friend_ticks:
            friendship = _clamp(friendship - new_friend_ticks * FRIEND_DECAY_AMOUNT, 0, FRIEND_MAX)
            friend_ticks_applied = eligible_ticks

    if mood != row["mood"] or friendship != row["friendship"]:
        await db.execute(
            """
            UPDATE panda
            SET mood = ?, friendship = ?, mood_ticks_applied = ?, friend_ticks_applied = ?
            WHERE user_id = ?
            """,
            (mood, friendship, mood_ticks_applied, friend_ticks_applied, user_id),
        )
        await database.commit()
        return await _fetch_row(db, user_id)

    return row


async def _settle(user_id: int) -> aiosqlite.Row:
    """Применяет накопившийся со временем эффект (падение настроения из-за
    низкого голода, падение дружбы из-за долгого голодания) и сохраняет
    результат. Чистая функция от времени — можно звать сколько угодно раз,
    результат всегда сходится к правильному состоянию.

    Захватывает персональный лок игрока — безопасно вызывать параллельно
    (в том числе из нескольких хендлеров сразу)."""
    async with database.user_lock(user_id):
        return await _settle_locked(user_id)


async def get_panda_state(user_id: int) -> aiosqlite.Row:
    """Публичная обёртка над _settle — актуальное (с учётом всего, что
    накопилось со временем) состояние панды игрока, для использования
    другими модулями (например, admin.py — карточка профиля игрока).
    Не предназначена для показа игроку напрямую внутри этого модуля —
    там для этого используется _settle (тот же эффект, приватное имя)."""
    return await _settle(user_id)


async def feed_panda(user_id: int) -> aiosqlite.Row:
    """Кормит панду: сбрасывает голод до 100%, счётчики завязанных на
    голод тиков и запускает новый цикл голода со свежими случайными
    длительностями фаз (30-50 мин до 50%, затем 2-4 ч до 0%)."""
    async with database.user_lock(user_id):
        row = await _settle_locked(user_id)  # применяем то, что накопилось до кормления
        db = await database.get_db()
        now = time.time()
        phase1, phase2 = _roll_hunger_phases(row["level"])
        await db.execute(
            """
            UPDATE panda
            SET last_fed_at = ?, mood_ticks_applied = 0, friend_ticks_applied = 0,
                hunger_phase1_seconds = ?, hunger_phase2_seconds = ?
            WHERE user_id = ?
            """,
            (now, phase1, phase2, user_id),
        )
        await database.commit()
        return await _fetch_row(db, user_id)


async def restore_hunger(user_id: int, percent: float) -> tuple[aiosqlite.Row, float]:
    """Восполняет голод на заданный процент (не до конца, в отличие от feed_panda).
    Используется садом: съеденный фрукт утоляет голод частично. Пересчитывает
    last_fed_at так, чтобы calc_hunger_percent сразу же отражал прибавку.

    Возвращает (row, new_hunger) — new_hunger нужно использовать для проверки
    ачивок ВМЕСТО повторного calc_hunger_percent(..., time.time(), ...) в
    вызывающем коде: last_fed_at здесь выставляется так, что голод в МОМЕНТ
    этого вызова равен new_hunger, но пока on_feed_item дойдёт до проверки
    ачивок (после нескольких await: commit, callback.answer, edit_text,
    _bump_feed_count и т.д.) реальное время уйдёт вперёд, и пересчитанный
    заново голод окажется чуть МЕНЬШЕ 100% — из-за чего "achv >= 100" почти
    никогда не срабатывает. Поэтому используем именно это, уже посчитанное
    здесь значение.

    Захват лока тут обязателен: без него два фрукта, скормленных почти
    одновременно, могли бы прочитать одно и то же исходное состояние и
    один из результатов "потерялся" бы (последняя запись просто
    перезаписывает первую)."""
    async with database.user_lock(user_id):
        await _settle_locked(user_id)
        db = await database.get_db()
        row = await _fetch_row(db, user_id)
        now = time.time()

        phase1_seconds = row["hunger_phase1_seconds"]
        phase2_seconds = row["hunger_phase2_seconds"]

        current_hunger = calc_hunger_percent(row["last_fed_at"], now, phase1_seconds, phase2_seconds)
        new_hunger = _clamp(current_hunger + percent, 0, 100)
        new_last_fed_at = now - _hunger_percent_to_elapsed(new_hunger, phase1_seconds, phase2_seconds)

        # ВАЖНО: если голод после частичного восполнения остаётся < 50%,
        # new_last_fed_at пересчитывается НАЗАД во времени (см.
        # _hunger_percent_to_elapsed) — а вместе с ним назад сдвигается и
        # t_low_hunger = last_fed_at + phase1_seconds, то есть точка,
        # с которой должны тикать распады настроения/дружбы. Раньше тут
        # счётчики просто обнулялись в 0, из-за чего следующий же _settle
        # видел "с t_low_hunger прошло N тиков, применено 0" и разом
        # накатывал весь накопившийся на новой шкале долг — настроение и
        # дружба обваливались сразу после кормления (и по той же причине
        # плыли числа от глажки, т.к. pet_panda тоже вызывает _settle
        # первым делом). Вместо обнуления пересчитываем счётчики так,
        # чтобы они соответствовали новой точке t_low_hunger на момент
        # "сейчас" — тогда задним числом накрученного долга не возникает.
        new_t_low_hunger = new_last_fed_at + phase1_seconds
        if now > new_t_low_hunger:
            new_ticks_applied = int((now - new_t_low_hunger) // MOOD_FRIEND_DECAY_TICK_SECONDS)
        else:
            new_ticks_applied = 0

        await db.execute(
            """
            UPDATE panda
            SET last_fed_at = ?, mood_ticks_applied = ?, friend_ticks_applied = ?
            WHERE user_id = ?
            """,
            (new_last_fed_at, new_ticks_applied, new_ticks_applied, user_id),
        )
        await database.commit()
        return await _fetch_row(db, user_id), new_hunger


# ==========================
#   ЧУДЕСНЫЙ БАМБУК / УРОВНИ ПАНДЫ
# ==========================
# Способ ДОБЫТЬ бамбук в инвентарь (garden/события/донат) пока нигде не
# подключён — add_wonder_bamboo ниже это только заготовка на будущее.
# Прокачка уровня — ручная (см. level_up_panda / on_open_level /
# on_level_up): бамбук просто копится в инвентаре, уровень поднимается
# только по нажатию игроком кнопки "Повысить уровень".

async def add_wonder_bamboo(user_id: int, amount: int) -> aiosqlite.Row:
    """Начисляет чудесный бамбук в инвентарь панды. Пока не вызывается
    ниоткуда в боте — задел под будущий источник добычи бамбука."""
    async with database.user_lock(user_id):
        await _get_or_create_panda_locked(user_id)
        db = await database.get_db()
        await db.execute(
            "UPDATE panda SET wonder_bamboo = wonder_bamboo + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await database.commit()
        return await _fetch_row(db, user_id)


# Время (time.time()) последнего ЗАСЧИТАННОГО клика по дереву на
# игрока — см. TREE_CLICK_COOLDOWN_SECONDS / on_tree_click. Обычный dict
# в памяти процесса (не БД): это чисто антиспам-троттлинг Telegram-
# запросов, а не игровое состояние — переживать рестарт бота ему не
# нужно, и незачем платить лишними обращениями к БД на каждый клик.
_tree_click_last_ts: dict[int, float] = {}

# Исход клика по дереву -> (колонка в таблице panda, шанс). Порядок
# важен: шансы проверяются по очереди, как отрезки на числовой прямой
# [0, 1) (см. click_wonder_tree) — с ним же завязан порядок кнопок нет,
# только порядок сравнения.
_TREE_OUTCOMES = (
    ("bamboo", "wonder_bamboo", TREE_BAMBOO_CHANCE),
    ("dew", "wonder_dew", TREE_DEW_CHANCE),
    ("nut", "magic_nut", TREE_NUT_CHANCE),
)


async def click_wonder_tree(user_id: int) -> tuple[aiosqlite.Row, str, int]:
    """Один клик по "Дереву чудес" (кликер, см. panda:tree_click).

    Бросает кубик 0..1 и раздаёт ровно один исход (см. _TREE_OUTCOMES /
    TREE_KARMA_MIN/MAX выше): редкий предмет (+1 к соответствующей
    колонке инвентаря) либо, если ни один из них не выпал — случайная
    карма. Возвращает (обновлённая_строка_панды, тип_исхода, количество),
    где тип_исхода — один из "bamboo"/"dew"/"nut"/"karma"."""
    roll = random.random()

    result = "karma"
    column = "karma"
    amount = random.randint(TREE_KARMA_MIN, TREE_KARMA_MAX)

    threshold = 0.0
    for outcome_id, outcome_column, chance in _TREE_OUTCOMES:
        threshold += chance
        if roll < threshold:
            result, column, amount = outcome_id, outcome_column, 1
            break

    async with database.user_lock(user_id):
        await _get_or_create_panda_locked(user_id)
        db = await database.get_db()
        # column берётся только из фиксированного _TREE_OUTCOMES/"karma"
        # выше, никогда из пользовательского ввода — подстановка в SQL
        # безопасна.
        await db.execute(
            f"UPDATE panda SET {column} = {column} + ? WHERE user_id = ?",
            (amount, user_id),
        )
        await database.commit()
        row = await _fetch_row(db, user_id)

    return row, result, amount


async def level_up_panda(user_id: int) -> tuple[aiosqlite.Row, bool]:
    """Пытается вручную поднять панду на следующий уровень: списывает
    ВСЕ ресурсы из next_level_cost(текущий_уровень) (карма/бамбук/роса/
    орех — какие есть в стоимости этого уровня) из инвентаря и
    инкрементирует level — но только если хватает КАЖДОГО из них и
    панда ещё не на максимальном (PANDA_LEVEL_MAX) уровне. Голод не
    трогает — новый множитель длительности фаз
    (hunger_duration_multiplier) применится начиная со следующей полной
    переброски фаз, то есть со следующего feed_panda, а не задним
    числом к уже идущему циклу.

    Возвращает (row, поднялся_ли_уровень). Если чего-то не хватило или
    уровень уже максимальный — row не меняется, False."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        # _settle_locked (не голый _fetch_row!) — гарантирует, что строка
        # панды существует (создаст при первом обращении, как и в
        # feed_panda/click_wonder_tree/add_wonder_bamboo), и заодно
        # применяет накопившийся распад настроения/дружбы, если игрок
        # почему-то попал сюда мимо экрана "Уровень" (там это уже делает
        # on_open_level через _settle).
        row = await _settle_locked(user_id)

        cost = next_level_cost(row["level"])
        if cost is None or any(row[res] < amount for res, amount in cost.items()):
            return row, False

        # Названия ресурсов (res) берутся только из фиксированного
        # PANDA_LEVEL_COST/RESOURCE_ORDER выше, никогда из
        # пользовательского ввода — подстановка в SQL безопасна (см.
        # аналогичный комментарий у click_wonder_tree).
        set_clauses = ["level = level + 1"]
        params: list[int] = []
        for res, amount in cost.items():
            set_clauses.append(f"{res} = {res} - ?")
            params.append(amount)
        if "wonder_bamboo" in cost:
            # Исторический счётчик суммарно потраченного бамбука на
            # уровни (см. database.py: PANDA_COLUMNS["wonder_bamboo_fed"])
            # — растёт только на потраченный именно бамбук.
            set_clauses.append("wonder_bamboo_fed = wonder_bamboo_fed + ?")
            params.append(cost["wonder_bamboo"])
        params.append(user_id)

        await db.execute(
            f"UPDATE panda SET {', '.join(set_clauses)} WHERE user_id = ?",
            params,
        )
        await database.commit()
        return await _fetch_row(db, user_id), True


async def pet_panda(user_id: int) -> tuple[bool, aiosqlite.Row, float]:
    """Гладит панду. Возвращает (успех, строка_панды, минут_до_след_попытки).

    Лок здесь закрывает окно, в которое два быстрых тапа подряд могли бы
    оба прочитать pet_count ниже лимита и оба пройти проверку — то есть
    погладить панду больше PET_MAX_USES раз за окно."""
    async with database.user_lock(user_id):
        await _settle_locked(user_id)
        db = await database.get_db()
        row = await _fetch_row(db, user_id)
        now = time.time()

        pet_window_start = row["pet_window_start"]
        pet_count = row["pet_count"]

        if now - pet_window_start >= PET_WINDOW_SECONDS:
            pet_window_start = now
            pet_count = 0

        if pet_count >= PET_MAX_USES:
            minutes_left = (PET_WINDOW_SECONDS - (now - pet_window_start)) / 60
            return False, row, max(minutes_left, 0.1)

        mood = _clamp(row["mood"] + PET_MOOD_GAIN, 0, MOOD_MAX)
        friendship = _clamp(row["friendship"] + PET_FRIEND_GAIN, 0, FRIEND_MAX)
        pet_count += 1

        await db.execute(
            """
            UPDATE panda
            SET mood = ?, friendship = ?, pet_window_start = ?, pet_count = ?
            WHERE user_id = ?
            """,
            (mood, friendship, pet_window_start, pet_count, user_id),
        )
        await database.commit()
        return True, await _fetch_row(db, user_id), 0.0


def rename_cost(row: aiosqlite.Row) -> int:
    """Стоимость установки/смены имени панды:
    - если имени ещё нет вообще — первая установка всегда бесплатна (0);
    - иначе — платное переименование, каждый раз вдвое дороже предыдущего:
      RENAME_BASE_COST * 2**name_changes, т.е. 1000, 2000, 4000, 8000, ...
      (name_changes — счётчик уже ОПЛАЧЕННЫХ переименований, первая
      бесплатная установка имени его не увеличивает)."""
    if not row["name"]:
        return 0
    return RENAME_BASE_COST * (2 ** row["name_changes"])


async def set_panda_name(user_id: int, name: str) -> str:
    """Устанавливает или платно меняет имя панды. Возвращает:
    - "ok" — имя успешно установлено/изменено,
    - "insufficient" — не хватает Pn на платное переименование (имя не
      трогаем в этом случае).

    Списание Pn и обновление имени происходят под одним и тем же локом
    database.user_lock(user_id) — поэтому баланс списывается через
    shop._change_balance() напрямую, а не через shop.add_balance()/
    shop.spend_balance() (которые сами берут этот же лок: asyncio.Lock
    не реентерабельный, повторный async with с тем же локом внутри
    того же task — гарантированный дедлок)."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        row = await _fetch_row(db, user_id)
        cost = rename_cost(row)

        if cost > 0:
            balance = await shop.get_balance(user_id)
            if balance < cost:
                return "insufficient"
            await shop._change_balance(user_id, -cost)
            await db.execute(
                "UPDATE panda SET name = ?, name_changes = name_changes + 1 WHERE user_id = ?",
                (name, user_id),
            )
            # Деньги списаны — экономическая операция, сохраняем на диск
            # немедленно, как и остальные такие операции в shop.py.
            await database.flush()
        else:
            await db.execute("UPDATE panda SET name = ? WHERE user_id = ?", (name, user_id))
            await database.commit()

        return "ok"


# ==========================
#   КРИСТАЛЛЫ (вторая валюта — только для скинов дороже 4-го)
# ==========================
# Раньше здесь была своя таблица (panda_crystals) — теперь это ОДИН общий
# баланс с donate.py (покупка за Stars) и разделом "Профиль" (подарки):
# счёт хранится в prof.py (users.crystals), а тут просто зовём
# prof.get_crystals()/prof.add_crystals()/prof._change_crystal_balance(),
# чтобы не было двух разных балансов "кристаллов" у одного игрока.


# ==========================
#   СКИНЫ (покупка/экипировка)
# ==========================

async def get_owned_skins(user_id: int) -> set[str]:
    """Возвращает множество id купленных игроком скинов (см. SKINS)."""
    db = await database.get_db()
    async with db.execute(
        "SELECT skin_id FROM panda_skins WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {row["skin_id"] async for row in cursor}


async def buy_skin(user_id: int, skin_id: str) -> str:
    """Покупает скин панды. Возвращает:
    - "ok" — скин успешно куплен;
    - "already_owned" — этот скин уже был куплен раньше;
    - "insufficient" — не хватает валюты на покупку.

    Валюта берётся из SKINS[skin_id]["currency"]: "coins" — обычная
    (shop.CURRENCY), "crystals" — вторая, премиальная — общий баланс с
    donate.py/prof.py (см. блок "КРИСТАЛЛЫ" выше).

    Списание идёт через shop._change_balance()/prof._change_crystal_balance()
    напрямую, а не через spend_balance()-обёртки — по той же причине,
    что и в set_panda_name: операция уже выполняется под захваченным
    database.user_lock(user_id), а asyncio.Lock не реентерабельный
    (повторный async with тем же локом внутри — гарантированный дедлок)."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT 1 FROM panda_skins WHERE user_id = ? AND skin_id = ?",
            (user_id, skin_id),
        ) as cursor:
            already = await cursor.fetchone()
        if already is not None:
            return "already_owned"

        skin = SKINS[skin_id]
        price = skin["price"]
        currency = skin["currency"]

        if currency == "crystals":
            balance = await prof.get_crystals(user_id)
            if balance < price:
                return "insufficient"
            await prof._change_crystal_balance(user_id, -price)
        else:
            balance = await shop.get_balance(user_id)
            if balance < price:
                return "insufficient"
            await shop._change_balance(user_id, -price)

        await db.execute(
            "INSERT INTO panda_skins (user_id, skin_id, purchased_at) VALUES (?, ?, ?)",
            (user_id, skin_id, time.time()),
        )
        # Деньги списаны — экономическая операция, сохраняем немедленно
        # (как и остальные такие операции в этом модуле).
        await database.flush()
        return "ok"


async def grant_skin_free(user_id: int, skin_id: str) -> bool:
    """Выдаёт скин панды бесплатно и навсегда — не покупка, а награда
    (сейчас единственный источник: бонусный скин привилегии Panda
    Premium, см. donate.py: buy_privilege). В отличие от buy_skin()
    не трогает валюту и не проверяет баланс. Выдача НЕ привязана к
    сроку самой привилегии — истечёт привилегия или нет, скин
    остаётся у игрока навсегда. Идемпотентна: если скин уже есть,
    просто ничего не делает. Возвращает True, если скин выдан именно
    этим вызовом, False — если он уже был у игрока раньше.

    ВАЖНО: как и buy_skin(), выдача скина засчитывается для ачивок
    "Модник" (panda_skin, за факт обладания любым скином) и порогов
    panda_skins_3/5/all (за количество). Раньше это не учитывалось —
    игрок, чей первый (а иногда и единственный) скин пришёл именно
    отсюда, а не из магазина, ачивки не получал вообще, пока не
    совершал обычную покупку. Здесь награда выдаётся "тихо" (без
    Message/lang — их у этой функции нет), возвращаемый список
    achv_ids вызывающий код (donate.py) может использовать, чтобы
    показать уведомления так же, как panda._unlock_all()."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT 1 FROM panda_skins WHERE user_id = ? AND skin_id = ?",
            (user_id, skin_id),
        ) as cursor:
            already = await cursor.fetchone()
        if already is not None:
            return False

        await db.execute(
            "INSERT INTO panda_skins (user_id, skin_id, purchased_at) VALUES (?, ?, ?)",
            (user_id, skin_id, time.time()),
        )
        await database.flush()

    # Вне database.user_lock — как и в on_buy_skin, achives.unlock сам
    # по себе идемпотентен и не требует захваченного лока игрока.
    await achives.unlock(user_id, "panda_skin")
    owned_skins = await get_owned_skins(user_id)
    for achv_id in _skin_count_achievements(len(owned_skins)):
        await achives.unlock(user_id, achv_id)
    return True


async def equip_skin(user_id: int, skin_id: str | None) -> bool:
    """Надевает купленный скин (skin_id), либо снимает текущий
    (skin_id=None — панда возвращается к обычному виду по возрасту).
    Возвращает False, если запрошенный скин ещё не куплен."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        if skin_id is not None:
            async with db.execute(
                "SELECT 1 FROM panda_skins WHERE user_id = ? AND skin_id = ?",
                (user_id, skin_id),
            ) as cursor:
                owned = await cursor.fetchone()
            if owned is None:
                return False

        await db.execute(
            "UPDATE panda SET equipped_skin_id = ? WHERE user_id = ?",
            (skin_id, user_id),
        )
        await database.commit()
        return True


# ==========================
#   ОТРИСОВКА КАРТОЧКИ ПАНДЫ
# ==========================

def _pick_tier(value: float, tiers: list[tuple[float, str]]) -> str:
    """tiers — [(порог, ключ_текста), ...] отсортировано по убыванию порога."""
    for threshold, key in tiers:
        if value >= threshold:
            return key
    return tiers[-1][1]


BAR_LENGTH = 10
BAR_FILLED = "▰"
BAR_EMPTY = "▱"


def _render_bar(percent: float) -> str:
    percent = max(0, min(100, percent))
    filled = round(percent / 100 * BAR_LENGTH)
    return BAR_FILLED * filled + BAR_EMPTY * (BAR_LENGTH - filled)


def _hunger_tier(hunger: float) -> str:
    if hunger <= 0:
        return "hunger_tier_zero"
    return _pick_tier(hunger, [
        (80, "hunger_tier_full"),
        (HUNGER_LOW_THRESHOLD, "hunger_tier_ok"),
        (0, "hunger_tier_low"),
    ])


def _mood_tier(mood: float) -> str:
    return _pick_tier(mood, [
        (80, "mood_tier_great"),
        (50, "mood_tier_good"),
        (20, "mood_tier_meh"),
        (0, "mood_tier_bad"),
    ])


def _friend_tier(friendship: float) -> str:
    return _pick_tier(friendship, [
        (80, "friend_tier_best"),
        (50, "friend_tier_strong"),
        (20, "friend_tier_new"),
        (0, "friend_tier_none"),
    ])


def _build_panda_view(lang: str, row: aiosqlite.Row, owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    now = time.time()

    age_days_total = calc_age_days(row["created_at"], now)
    days = int(age_days_total)
    hours = int(round((age_days_total - days) * 24))
    if hours == 24:
        days += 1
        hours = 0

    hunger = calc_hunger_percent(
        row["last_fed_at"], now, row["hunger_phase1_seconds"], row["hunger_phase2_seconds"]
    )
    mood = row["mood"]
    friendship = row["friendship"]

    title = t["named_title"].format(name=html.escape(row["name"])) if row["name"] else t["default_title"]

    level = row["level"]

    lines = [
        f"<b>{title}</b>",
        "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        f"<b>{t['age_label']}: {t['age_value'].format(days=days, hours=hours)}</b>",
        f"<b>{t['level_label']}: {t['level_value'].format(level=level)}</b>",
        "",
        f"<b>{t['hunger_label']} {round(hunger)}%</b>",
        f"<b>{_render_bar(hunger)}</b>",
        f"<i>«{t[_hunger_tier(hunger)]}»</i>",
        "",
        f"<b>{t['mood_label']} {round(mood)}%</b>",
        f"<b>{_render_bar(mood)}</b>",
        f"<i>«{t[_mood_tier(mood)]}»</i>",
        "",
        f"<b>{t['friendship_label']} {round(friendship)}%</b>",
        f"<b>{_render_bar(friendship)}</b>",
        f"<i>«{t[_friend_tier(friendship)]}»</i>",
    ]
    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    builder.button(text=t["feed_button"], callback_data=main.owner_cb(owner_id, "panda:feed"), style="primary")
    builder.button(
        text=t["pet_button"],
        callback_data=main.owner_cb(owner_id, "panda:pet"),
        style="primary",
        icon_custom_emoji_id=PET_BUTTON_EMOJI_ID,
    )
    builder.button(
        text=t["level_button"],
        callback_data=main.owner_cb(owner_id, "panda:level"),
        style="primary",
        icon_custom_emoji_id=LEVEL_TITLE_EMOJI_ID,
    )
    builder.button(
        text=t["tree_button"],
        callback_data=main.owner_cb(owner_id, "panda:tree"),
        style="primary",
        icon_custom_emoji_id=TREE_CLICK_BUTTON_EMOJI_ID,
    )
    builder.button(
        text=t["setname_button"] if not row["name"] else t["rename_button"],
        callback_data=main.owner_cb(owner_id, "panda:setname"),
        style="primary",
        icon_custom_emoji_id=SETNAME_BUTTON_EMOJI_ID,
    )
    builder.adjust(2, 2, 1)

    return text, builder.as_markup()


def _build_level_view(lang: str, row: aiosqlite.Row, owner_id: int) -> tuple[str, object]:
    """Экран "Уровень": текущий уровень, по одной шкале прогресса на
    каждый нужный для следующего уровня ресурс (карма/бамбук/роса/орех
    — какие есть в стоимости этого уровня, см. PANDA_LEVEL_COST) и
    кнопка ручного повышения (см. level_up_panda). На максимальном
    (PANDA_LEVEL_MAX) уровне кнопки повышения нет — только
    поздравительная строка."""
    import main

    t = TEXTS[lang]
    level = row["level"]
    bonus_now = round((hunger_duration_multiplier(level) - 1) * 100)
    cost = next_level_cost(level)

    lines = [
        t["level_screen_title"],
        "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        t["level_current_line"].format(level=level),
        t["level_bonus_line"].format(bonus=bonus_now),
        "",
    ]

    builder = InlineKeyboardBuilder()
    if cost is None:
        lines.append(t["level_max_line"])
    else:
        lines.append(t["level_next_line"].format(level=level + 1))
        for res in RESOURCE_ORDER:
            need = cost.get(res)
            if not need:
                continue
            have = row[res]
            have_capped = min(have, need)
            percent = have_capped / need * 100 if need else 100
            emoji = RESOURCE_EMOJI_RICH[res]
            lines.extend([
                f"{emoji} {t[f'res_name_{res}']}",
                f"<b>{_render_bar(percent)}</b>",
                f"<b>{t['level_progress_line'].format(have=have, need=need)}</b>",
                "",
            ])
        if lines and lines[-1] == "":
            lines.pop()
        builder.button(
            text=t["level_up_button"],
            callback_data=main.owner_cb(owner_id, "panda:level_up"),
            style="primary",
            icon_custom_emoji_id=LEVEL_UP_BUTTON_EMOJI_ID,
        )

    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "panda:level_back"),
        style="primary",
        icon_custom_emoji_id=BACK_BUTTON_EMOJI_ID,
    )
    builder.adjust(1)

    return "\n".join(lines), builder.as_markup()


def _build_tree_view(lang: str, row: aiosqlite.Row, owner_id: int) -> tuple[str, object]:
    """Экран "Дерево чудес" — клик-механика (см. click_wonder_tree):
    показывает текущий запас всех четырёх исходов клика и кнопку
    "Тряхнуть дерево". Экран остаётся на месте после каждого клика —
    меняются только цифры в тексте (см. on_tree_click)."""
    import main

    t = TEXTS[lang]

    lines = [
        t["tree_screen_title"],
        "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        t["tree_intro"],
        "",
        t["tree_stock_bamboo"].format(count=row["wonder_bamboo"]),
        t["tree_stock_dew"].format(count=row["wonder_dew"]),
        t["tree_stock_nut"].format(count=row["magic_nut"]),
        t["tree_stock_karma"].format(count=row["karma"]),
    ]

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["tree_click_button"],
        callback_data=main.owner_cb(owner_id, "panda:tree_click"),
        style="primary",
        icon_custom_emoji_id=TREE_CLICK_BUTTON_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "panda:tree_back"),
        style="primary",
        icon_custom_emoji_id=BACK_BUTTON_EMOJI_ID,
    )
    builder.adjust(1)

    return "\n".join(lines), builder.as_markup()


def _pick_sticker(row: aiosqlite.Row) -> str:
    # Надетый скин (см. SKINS) полностью переопределяет стикер, вне
    # зависимости от возраста панды — иначе купленный скин был бы виден
    # только у молодой (или только у взрослой) панды.
    skin_id = row["equipped_skin_id"]
    if skin_id and skin_id in SKINS:
        return SKINS[skin_id]["sticker_id"]
    age_days = calc_age_days(row["created_at"], time.time())
    return PANDA_STICKER_ID_ADULT if age_days > ADULT_AGE_DAYS else PANDA_STICKER_ID


def _build_feed_choice(
    lang: str, fruit_inventory: dict[str, int], pantry_inventory: dict[str, int], owner_id: int
) -> tuple[str, object]:
    """Экран выбора того, чем покормить панду — фрукты из корзины сада
    (garden.get_inventory / garden.CROPS) и готовая выпечка с витрины
    пекарни (bakery.get_pantry / bakery.RECIPES). Единственное место во
    всём боте, откуда панду можно покормить — раздел "Пекарня" сам по
    себе больше не кормит напрямую. Чудесный бамбук сюда не входит —
    он тратится не через кормление, а вручную на экране "Уровень"
    (см. panda:level / level_up_panda)."""
    import main

    t = TEXTS[lang]
    text = t["feed_choice_title"]

    builder = InlineKeyboardBuilder()
    for cid in garden.CROP_ORDER:
        count = fruit_inventory.get(cid, 0)
        if count <= 0:
            continue
        crop = garden.CROPS[cid]
        builder.button(
            text=t["feed_item_button"].format(
                emoji=crop["emoji"],
                name=crop["name"][lang],
                count=count,
            ),
            callback_data=main.owner_cb(owner_id, f"panda:feed_item:crop:{cid}"),
            style="primary",
        )
    for rid in bakery.RECIPE_ORDER:
        count = pantry_inventory.get(rid, 0)
        if count <= 0:
            continue
        recipe = bakery.RECIPES[rid]
        builder.button(
            text=t["feed_item_button"].format(
                emoji=recipe["emoji"],
                name=recipe["name"][lang],
                count=count,
            ),
            callback_data=main.owner_cb(owner_id, f"panda:feed_item:bakery:{rid}"),
            style="primary",
        )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "panda:feed_back"),
        style="primary",
        icon_custom_emoji_id=BACK_BUTTON_EMOJI_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


def _skin_button_specs(
    lang: str, owned: set[str], equipped_skin_id: str | None
) -> list[tuple[str, str, str, str | None]]:
    """Строит список (skin_id, текст_кнопки, style, icon_custom_emoji_id)
    для текущего состояния игрока (что куплено/что надето).
    Пересчитывается заново при каждом открытии меню. В подписи кнопки —
    только название скина, без цены (цена/валюта видна в карточке
    скина, открывающейся по нажатию). Статус скина показывается не
    эмодзи-префиксом в тексте (Telegram не рендерит такое в кнопках), а
    через icon_custom_emoji_id (замок — для некупленных) и
    style="success" (зелёная кнопка — для купленных)."""
    t = TEXTS[lang]
    specs: list[tuple[str, str, str, str | None]] = []
    for skin_id in SKIN_ORDER:
        skin = SKINS[skin_id]
        name = skin["name"][lang]
        if skin_id == equipped_skin_id:
            text = t["skin_catalog_equipped"].format(name=name)
            specs.append((skin_id, text, "success", None))
        elif skin_id in owned:
            text = t["skin_catalog_owned"].format(name=name)
            specs.append((skin_id, text, "success", None))
        else:
            # Цена в подписи кнопки больше не показывается — только
            # название скина, замок-иконка уже даёт понять, что скин не
            # куплен, а цену и валюту видно в карточке скина.
            text = t["skin_catalog_locked"].format(name=name)
            specs.append((skin_id, text, "primary", SKIN_LOCKED_EMOJI_ID))
    return specs


def _build_skins_menu(lang: str, owned: set[str], equipped_skin_id: str | None) -> ReplyKeyboardMarkup:
    """Реплай-меню "Облики" — кнопка "Назад" первой строкой, дальше по
    одной кнопке на скин в ряд (не по 2 — так название скина не режется
    и не приходится подбирать текст под ширину соседней кнопки)."""
    t = TEXTS[lang]
    specs = _skin_button_specs(lang, owned, equipped_skin_id)
    buttons = [
        KeyboardButton(text=text, style=style, icon_custom_emoji_id=icon_id)
        if icon_id is not None
        else KeyboardButton(text=text, style=style)
        for _skin_id, text, style, icon_id in specs
    ]

    rows = [[
        KeyboardButton(
            text=t["skins_menu_back_button"],
            style="primary",
            icon_custom_emoji_id=SKINS_MENU_BACK_EMOJI_ID,
        )
    ]]
    rows += [[button] for button in buttons]

    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def _build_skin_detail(
    lang: str, skin_id: str, owned: bool, equipped: bool, coin_balance: int, crystal_balance: int,
    owner_id: int,
) -> tuple[str, object]:
    """Карточка одного скина: короткая история персонажа и цена/статус,
    плюс кнопка действия (купить / надеть / снять). Баланс игрока (обе
    валюты) показывается в тексте карточки всегда, независимо от того,
    в какой валюте продаётся сам скин, — чтобы было видно, хватает ли
    денег, не выходя из карточки."""
    import main

    t = TEXTS[lang]
    skin = SKINS[skin_id]

    lines = [
        f"<b>{skin['name'][lang]}</b>",
        "<code>·  ·  ·  ◆  ·  ·  ·</code>",
        t["skins_balance_line"].format(
            coin_currency=shop.CURRENCY, coin_balance=coin_balance,
            crystal_currency=CRYSTAL_EMOJI, crystal_balance=crystal_balance,
        ),
        f"<blockquote>{SKIN_DESC_EMOJI} <i>{skin['description'][lang]}</i></blockquote>",
        "",
    ]
    if equipped:
        lines.append(t["skin_status_equipped"])
    elif owned:
        lines.append(t["skin_status_owned"])
    else:
        price_currency_html = shop.CURRENCY if skin["currency"] == "coins" else CRYSTAL_EMOJI
        lines.append(t["skin_price_line"].format(price=skin["price"], currency=price_currency_html))
    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    if not owned:
        buy_emoji_id = SKIN_BUY_COIN_EMOJI_ID if skin["currency"] == "coins" else SKIN_BUY_CRYSTAL_EMOJI_ID
        buy_kwargs = {}
        if buy_emoji_id is not None:
            buy_kwargs["icon_custom_emoji_id"] = buy_emoji_id
        builder.button(
            text=t["buy_button"].format(price=skin["price"]),
            callback_data=main.owner_cb(owner_id, f"panda:skin_buy:{skin_id}"),
            style="primary",
            **buy_kwargs,
        )
    elif equipped:
        builder.button(
            text=t["unequip_button"],
            callback_data=main.owner_cb(owner_id, f"panda:skin_unequip:{skin_id}"),
            style="primary",
            icon_custom_emoji_id=SKIN_UNEQUIP_EMOJI_ID,
        )
    else:
        builder.button(
            text=t["wear_button"], callback_data=main.owner_cb(owner_id, f"panda:skin_equip:{skin_id}"), style="primary"
        )
    builder.adjust(1)
    return text, builder.as_markup()


async def _get_lang(state: FSMContext, user_id: int) -> str:
    """Возвращает язык пользователя.

    FSM-состояние (MemoryStorage) хранится только в памяти процесса и
    полностью пропадает при каждом перезапуске бота. Раньше при
    отсутствии lang в state тут молча подставлялось "ru" — из-за этого
    уже показанные пользователю реплай-кнопки (в т.ч. в разделе
    "Облики") переставали корректно работать после рестарта: и язык
    сбивался, и завязанные на state флаги (panda_menu) терялись, пока
    пользователь не отправлял /start заново (а /start подтягивает
    lang/gender из БД и кладёт их обратно в state).

    Теперь при пустом state язык подтягивается напрямую из БД
    (database.save_onboarding сохраняет его там при онбординге) и
    кэшируется обратно в state, чтобы не ходить в БД на каждый клик."""
    data = await state.get_data()
    lang = data.get("lang")
    if lang:
        return lang

    onboarding = await database.get_onboarding(user_id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"
    await state.update_data(lang=lang)
    return lang


# ==========================
#   ПРОАКТИВНЫЕ УВЕДОМЛЕНИЯ (фоновый цикл, см. main.py: main())
# ==========================
#
# Раньше уведомление "покормите/приласкайте панду" слалось реактивно —
# отдельным сообщением почти после каждого действия в разделе "Моя
# панда" (кормление, глажка, открытие раздела и т.д.). Из-за этого при
# нескольких действиях подряд один и тот же игрок получал несколько
# одинаковых уведомлений за пару секунд.
#
# Теперь это отдельный фоновый цикл (start_notify_loop), который раз в
# NOTIFY_INTERVAL_SECONDS сам проходит по всем игрокам и присылает
# уведомление, если голод/настроение ниже порога — независимо от того,
# открывал ли игрок раздел "Моя панда" вообще. Чтобы не слать одно и то
# же уведомление на каждой следующей проверке подряд, пока панда
# остаётся голодной/грустной, факт уже отправленного уведомления
# запоминается в panda_notify_state и сбрасывается только когда
# голод/настроение снова поднимаются выше порога — то есть уведомление
# приходит именно в момент пересечения порога, а не долбит каждые 2 часа.
#
# Собственной таблицы под это в database.py нет — заводим её сами,
# лениво (IF NOT EXISTS), по аналогии с panda_crystals/panda_skins.
# ensure_notify_table() вызывается один раз при старте бота, см.
# main.py: main().

async def ensure_notify_table() -> None:
    db = await database.get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS panda_notify_state (
            user_id INTEGER PRIMARY KEY,
            hunger_notified INTEGER NOT NULL DEFAULT 0,
            mood_notified INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await database.commit()


async def _get_notify_flags(user_id: int) -> tuple[bool, bool]:
    db = await database.get_db()
    async with db.execute(
        "SELECT hunger_notified, mood_notified FROM panda_notify_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return False, False
    return bool(row["hunger_notified"]), bool(row["mood_notified"])


async def _set_notify_flags(user_id: int, hunger_notified: bool, mood_notified: bool) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO panda_notify_state (user_id, hunger_notified, mood_notified)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            hunger_notified = excluded.hunger_notified,
            mood_notified = excluded.mood_notified
        """,
        (user_id, int(hunger_notified), int(mood_notified)),
    )


async def _get_all_panda_user_ids() -> list[int]:
    db = await database.get_db()
    async with db.execute("SELECT user_id FROM panda") as cursor:
        rows = await cursor.fetchall()
    return [row["user_id"] for row in rows]


async def _check_and_notify_one(bot, user_id: int) -> None:
    """Проверяет одного игрока и, если нужно, шлёт push-уведомление —
    ровно в момент пересечения порога (см. комментарий к разделу выше)."""
    async with database.user_lock(user_id):
        row = await _settle_locked(user_id)
        await database.commit()

        now = time.time()
        hunger = calc_hunger_percent(
            row["last_fed_at"], now, row["hunger_phase1_seconds"], row["hunger_phase2_seconds"]
        )
        mood = row["mood"]

        hunger_low = hunger < NOTIFY_HUNGER_THRESHOLD
        mood_low = mood < NOTIFY_MOOD_THRESHOLD

        hunger_notified, mood_notified = await _get_notify_flags(user_id)

        onboarding = await database.get_onboarding(user_id)
        lang = (onboarding["lang"] if onboarding else None) or "ru"
        t = TEXTS[lang]

        send_hunger = hunger_low and not hunger_notified
        send_mood = mood_low and not mood_notified

        if send_hunger or send_mood:
            try:
                if send_hunger:
                    await bot.send_message(user_id, t["call_to_feed"])
                if send_mood:
                    await bot.send_message(user_id, t["call_to_pet"])
            except Exception:
                # Игрок мог заблокировать бота и т.п. — не роняем весь
                # цикл проверки остальных игроков из-за одного сбоя.
                logger.warning("panda notify: failed to message user %s", user_id, exc_info=True)

        # Флаги обновляем независимо от того, отправляли мы что-то
        # только что или нет — как только показатель снова поднимается
        # выше порога, флаг сбрасывается, и при следующем падении
        # уведомление придёт заново.
        await _set_notify_flags(user_id, hunger_low, mood_low)
        await database.commit()


async def check_and_notify(bot) -> None:
    """Один проход фоновой проверки — по всем игрокам, у кого вообще
    есть панда."""
    for user_id in await _get_all_panda_user_ids():
        try:
            await _check_and_notify_one(bot, user_id)
        except Exception:
            logger.exception("panda notify: error checking user %s", user_id)


async def start_notify_loop(bot) -> None:
    """Фоновый цикл: раз в NOTIFY_INTERVAL_SECONDS проверяет всех
    игроков и шлёт уведомления проголодавшимся/загрустившим пандам.
    Запускается один раз при старте бота как отдельная asyncio-задача,
    см. main.py: main() (asyncio.create_task(panda.start_notify_loop(bot)))."""
    while True:
        try:
            await check_and_notify(bot)
        except Exception:
            logger.exception("panda notify: loop iteration failed")
        await asyncio.sleep(NOTIFY_INTERVAL_SECONDS)


# ==========================
#   ШТРАФ ЗА ГОЛОДАЮЩУЮ ПАНДУ (голод держится на 0%)
# ==========================
# См. константы HUNGER_ZERO_* выше. Своей таблицы под это в database.py
# нет — заводим лениво (IF NOT EXISTS), по аналогии с
# panda_notify_state/panda_achv_state. ensure_penalty_state_table()
# вызывается один раз при старте бота, см. main.py: main().
#
# cycle_fed_at — last_fed_at панды на момент последней обработанной
# проверки: как только реальный last_fed_at меняется (панду покормили —
# feed_panda/restore_hunger сдвигают last_fed_at вперёд), это значит
# начался новый "цикл голода", и warned_stage/penalty_ticks_applied
# сбрасываются — иначе после следующего падения голода до 0% штраф
#(или его часть предупреждений) не сработал бы заново.
# warned_stage — сколько из HUNGER_ZERO_WARNING_HOURS предупреждений
# уже отправлено в текущем цикле (0, 1 или 2).
# penalty_ticks_applied — сколько штрafных "тиков" по
# HUNGER_ZERO_PENALTY_AMOUNT уже списано в текущем цикле голода.

async def ensure_penalty_state_table() -> None:
    db = await database.get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS panda_penalty_state (
            user_id INTEGER PRIMARY KEY,
            cycle_fed_at REAL NOT NULL DEFAULT 0,
            warned_stage INTEGER NOT NULL DEFAULT 0,
            penalty_ticks_applied INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await database.commit()


async def _get_penalty_state(user_id: int) -> tuple[float, int, int]:
    db = await database.get_db()
    async with db.execute(
        "SELECT cycle_fed_at, warned_stage, penalty_ticks_applied "
        "FROM panda_penalty_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return 0.0, 0, 0
    return row["cycle_fed_at"], row["warned_stage"], row["penalty_ticks_applied"]


async def _set_penalty_state(
    user_id: int, cycle_fed_at: float, warned_stage: int, penalty_ticks_applied: int
) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO panda_penalty_state
            (user_id, cycle_fed_at, warned_stage, penalty_ticks_applied)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
            cycle_fed_at = excluded.cycle_fed_at,
            warned_stage = excluded.warned_stage,
            penalty_ticks_applied = excluded.penalty_ticks_applied
        """,
        (user_id, cycle_fed_at, warned_stage, penalty_ticks_applied),
    )


async def _check_and_penalize_one(bot, user_id: int) -> None:
    """Проверяет одного игрока: если панда голодает на 0% достаточно
    долго — шлёт предупреждение(я) и/или списывает штраф. Считает всё
    по факту прошедшего времени (см. комментарий к константам выше),
    поэтому корректно "досчитывает" пропущенные пороги, даже если бот
    какое-то время не работал или проверка запоздала."""
    async with database.user_lock(user_id):
        row = await _settle_locked(user_id)

        cycle_fed_at, warned_stage, ticks_applied = await _get_penalty_state(user_id)
        is_new_cycle = row["last_fed_at"] != cycle_fed_at
        if is_new_cycle:
            # Панду покормили после прошлой проверки (или это вообще
            # первая проверка) — начинается новый цикл голода, старые
            # предупреждения/штрафы к нему не относятся.
            cycle_fed_at, warned_stage, ticks_applied = row["last_fed_at"], 0, 0

        t_zero = cycle_fed_at + row["hunger_phase1_seconds"] + row["hunger_phase2_seconds"]
        now = time.time()

        if now <= t_zero:
            # Голод ещё выше 0% — штрафовать/предупреждать нечего.
            # Сохраняем только если только что произошёл сброс цикла.
            if is_new_cycle:
                await _set_penalty_state(user_id, cycle_fed_at, warned_stage, ticks_applied)
                await database.commit()
            return

        elapsed_hours = (now - t_zero) / 3600

        onboarding = await database.get_onboarding(user_id)
        lang = (onboarding["lang"] if onboarding else None) or "ru"
        t = TEXTS[lang]

        # Предупреждения — по одному разу за цикл каждое, в порядке
        # возрастания HUNGER_ZERO_WARNING_HOURS.
        for stage, warn_hour in enumerate(HUNGER_ZERO_WARNING_HOURS, start=1):
            if warned_stage >= stage or elapsed_hours < warn_hour:
                continue
            hours_left = max(0, HUNGER_ZERO_PENALTY_START_HOURS - warn_hour)
            try:
                await bot.send_message(
                    user_id,
                    t["penalty_warning"].format(
                        hours=warn_hour,
                        hours_left=hours_left,
                        amount=HUNGER_ZERO_PENALTY_AMOUNT,
                        currency=shop.CURRENCY,
                    ),
                )
            except Exception:
                logger.warning("panda penalty: failed to warn user %s", user_id, exc_info=True)
            warned_stage = stage

        # Штраф — начиная с HUNGER_ZERO_PENALTY_START_HOURS часов на 0%,
        # затем ещё по разу за каждый следующий полный
        # HUNGER_ZERO_PENALTY_INTERVAL_HOURS час, пока голод остаётся
        # на 0%. due_ticks — сколько штрафов ДОЛЖНО было накопиться к
        # этому моменту; если бот проверял реже (или был выключен) и
        # пропустил несколько часовых порогов подряд, недостающее
        # списывается одной суммой при следующей проверке — деньги не
        # "прощаются" из-за редких проверок.
        if elapsed_hours >= HUNGER_ZERO_PENALTY_START_HOURS:
            due_ticks = (
                int((elapsed_hours - HUNGER_ZERO_PENALTY_START_HOURS) // HUNGER_ZERO_PENALTY_INTERVAL_HOURS)
                + 1
            )
            missed_ticks = due_ticks - ticks_applied
            if missed_ticks > 0:
                owed = HUNGER_ZERO_PENALTY_AMOUNT * missed_ticks
                balance = await shop.get_balance(user_id)
                # Не уводим баланс в минус — если Pn не хватает на
                # весь причитающийся штраф, списываем сколько есть.
                charge = min(owed, balance)
                if charge > 0:
                    await shop._change_balance(user_id, -charge)
                    # Экономическая операция (списание Pn) — сохраняем
                    # немедленно, как и остальные такие операции в этом
                    # модуле (см. buy_skin/set_panda_name выше).
                    await database.flush()
                    try:
                        await bot.send_message(
                            user_id,
                            t["penalty_applied"].format(
                                amount=charge, currency=shop.CURRENCY
                            ),
                        )
                    except Exception:
                        logger.warning(
                            "panda penalty: failed to notify user %s", user_id, exc_info=True
                        )
                ticks_applied = due_ticks

        await _set_penalty_state(user_id, cycle_fed_at, warned_stage, ticks_applied)
        await database.commit()


async def check_and_penalize(bot) -> None:
    """Один проход фоновой проверки штрафа — по всем игрокам, у кого
    вообще есть панда."""
    for user_id in await _get_all_panda_user_ids():
        try:
            await _check_and_penalize_one(bot, user_id)
        except Exception:
            logger.exception("panda penalty: error checking user %s", user_id)


async def start_penalty_loop(bot) -> None:
    """Фоновый цикл: раз в PENALTY_CHECK_INTERVAL_SECONDS проверяет
    всех игроков и штрафует тех, чья панда достаточно долго голодает
    на 0% (с двумя предупреждениями до первого штрафа — см. константы
    HUNGER_ZERO_* выше). Запускается один раз при старте бота как
    отдельная asyncio-задача, см. main.py: main()
    (asyncio.create_task(panda.start_penalty_loop(bot)))."""
    while True:
        try:
            await check_and_penalize(bot)
        except Exception:
            logger.exception("panda penalty: loop iteration failed")
        await asyncio.sleep(PENALTY_CHECK_INTERVAL_SECONDS)


# ==========================
#   АЧИВКИ ПАНДЫ — СЧЁТЧИКИ/СТРИКИ
# ==========================
# Часть ачивок категории "панда" (см. achives.ACHIEVEMENTS) требует
# данных, которых нет в таблице panda: сколько раз всего покормили/
# погладили панду, сколько дней подряд о ней заботились, когда голод
# последний раз падал ниже 50%. Заводим отдельную таблицу лениво
# (IF NOT EXISTS), по аналогии с panda_notify_state выше —
# ensure_achv_state_table() вызывается один раз при старте бота,
# см. main.py: main() (рядом с ensure_notify_table()).
#
# care_streak_days / last_care_day — "заботиться N дней подряд"
# (panda_streak_7/30/100): last_care_day — номер календарного дня
# (реальное время, //SECONDS_IN_DAY), в который был последний
# засчитанный уход (кормление ИЛИ глажка, см. _record_care_day);
# один и тот же день, даже с несколькими уходами, засчитывается
# только один раз и не двигает стрик дальше; пропуск хотя бы одного
# дня сбрасывает стрик до 1.
#
# safe_since / was_low_hunger — с какого момента голод панды не падал
# ниже 50% без перерыва (panda_never_hungry_week, см. _touch_never_hungry):
# safe_since сбрасывается на "сейчас" при каждом свежем переходе голода
# ниже 50%; was_low_hunger — было ли состояние "ниже 50%" уже учтено,
# чтобы не сбрасывать таймер повторно на каждом вызове, пока панда
# остаётся голодной.

NEVER_HUNGRY_WEEK_SECONDS = 7 * SECONDS_IN_DAY
PANDA_BIRTHDAY_REAL_DAYS = 365


async def ensure_achv_state_table() -> None:
    db = await database.get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS panda_achv_state (
            user_id INTEGER PRIMARY KEY,
            total_feeds INTEGER NOT NULL DEFAULT 0,
            total_pets INTEGER NOT NULL DEFAULT 0,
            care_streak_days INTEGER NOT NULL DEFAULT 0,
            last_care_day INTEGER NOT NULL DEFAULT 0,
            safe_since REAL NOT NULL DEFAULT 0,
            was_low_hunger INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    await database.commit()


async def _ensure_achv_state_row(db: aiosqlite.Connection, user_id: int, now: float) -> None:
    """INSERT OR IGNORE строки panda_achv_state — safe_since дефолтится
    на "сейчас", если строка ещё не существовала (для игроков, у
    которых панда уже была заведена до появления этой таблицы, это
    просто означает, что их "чистая неделя" начинает отсчитываться
    с первого взаимодействия после обновления бота)."""
    await db.execute(
        "INSERT OR IGNORE INTO panda_achv_state (user_id, safe_since) VALUES (?, ?)",
        (user_id, now),
    )


def _current_hunger(row: aiosqlite.Row) -> float:
    return calc_hunger_percent(
        row["last_fed_at"], time.time(), row["hunger_phase1_seconds"], row["hunger_phase2_seconds"]
    )


async def _touch_never_hungry(user_id: int, row: aiosqlite.Row) -> list[str]:
    """Обновляет safe_since/was_low_hunger по актуальному голоду row и
    возвращает ["panda_never_hungry_week"], если полные 7 дней подряд
    голод не опускался ниже 50%. Звать после каждого кормления/глажки —
    иначе свежий переход голода ниже 50% может остаться незамеченным
    до следующего взаимодействия игрока с пандой."""
    now = time.time()
    is_low = _current_hunger(row) < 50

    db = await database.get_db()
    await _ensure_achv_state_row(db, user_id, now)
    async with db.execute(
        "SELECT safe_since, was_low_hunger FROM panda_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        state = await cursor.fetchone()

    if is_low and not state["was_low_hunger"]:
        await db.execute(
            "UPDATE panda_achv_state SET safe_since = ?, was_low_hunger = 1 WHERE user_id = ?",
            (now, user_id),
        )
        await database.commit()
        return []
    if not is_low and state["was_low_hunger"]:
        await db.execute(
            "UPDATE panda_achv_state SET was_low_hunger = 0 WHERE user_id = ?", (user_id,)
        )
        await database.commit()

    if not is_low and (now - state["safe_since"]) >= NEVER_HUNGRY_WEEK_SECONDS:
        return ["panda_never_hungry_week"]
    return []


async def _get_achv_counters(user_id: int) -> aiosqlite.Row | None:
    """Текущие значения счётчиков ачивок панды без их изменения — для
    achives.PROGRESS_PROVIDERS (карточка ачивки хочет знать "сколько
    уже сделано", не увеличивая счётчик заново). None, если строки ещё
    нет (игрок вообще ни разу не кормил/не гладил панду)."""
    db = await database.get_db()
    async with db.execute(
        "SELECT total_feeds, total_pets, care_streak_days FROM panda_achv_state WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _progress_total_feeds(user_id: int) -> int:
    row = await _get_achv_counters(user_id)
    return row["total_feeds"] if row else 0


async def _progress_total_pets(user_id: int) -> int:
    row = await _get_achv_counters(user_id)
    return row["total_pets"] if row else 0


async def _progress_care_streak(user_id: int) -> int:
    row = await _get_achv_counters(user_id)
    return row["care_streak_days"] if row else 0


# Регистрируем провайдеры прогресса для счётных ачивок панды — карточка
# ачивки в achives.py покажет реальные "X/Y" и процент вместо всегда
# бинарных 0%/100% (см. achives.py: PROGRESS_PROVIDERS/_get_progress).
achives.PROGRESS_PROVIDERS.update(
    {
        "panda_pet_10": (10, _progress_total_pets),
        "panda_pet_100": (100, _progress_total_pets),
        "panda_feed_10": (10, _progress_total_feeds),
        "panda_feed_100": (100, _progress_total_feeds),
        "panda_feed_500": (500, _progress_total_feeds),
        "panda_streak_7": (7, _progress_care_streak),
        "panda_streak_30": (30, _progress_care_streak),
        "panda_streak_100": (100, _progress_care_streak),
    }
)


async def _bump_feed_count(user_id: int) -> int:
    """+1 к общему счётчику кормлений (panda_feed_10/100/500), возвращает
    новое значение счётчика."""
    db = await database.get_db()
    now = time.time()
    await _ensure_achv_state_row(db, user_id, now)
    await db.execute(
        "UPDATE panda_achv_state SET total_feeds = total_feeds + 1 WHERE user_id = ?", (user_id,)
    )
    await database.commit()
    async with db.execute(
        "SELECT total_feeds FROM panda_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        return (await cursor.fetchone())["total_feeds"]


async def _bump_pet_count(user_id: int) -> int:
    """+1 к общему счётчику поглаживаний (panda_first_pet/pet_10/pet_100),
    возвращает новое значение счётчика."""
    db = await database.get_db()
    now = time.time()
    await _ensure_achv_state_row(db, user_id, now)
    await db.execute(
        "UPDATE panda_achv_state SET total_pets = total_pets + 1 WHERE user_id = ?", (user_id,)
    )
    await database.commit()
    async with db.execute(
        "SELECT total_pets FROM panda_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        return (await cursor.fetchone())["total_pets"]


async def _record_care_day(user_id: int) -> list[str]:
    """Засчитывает сегодняшний (реальный) день как день заботы о панде —
    вызывается и из кормления, и из глажки. Возвращает достигнутые
    ачивки стрика (panda_streak_7/30/100), если сегодня стрик впервые
    пересёк соответствующий порог."""
    db = await database.get_db()
    now = time.time()
    today = int(now // SECONDS_IN_DAY)

    await _ensure_achv_state_row(db, user_id, now)
    async with db.execute(
        "SELECT care_streak_days, last_care_day FROM panda_achv_state WHERE user_id = ?", (user_id,)
    ) as cursor:
        state = await cursor.fetchone()

    if state["last_care_day"] == today:
        return []  # сегодняшний день уже засчитан, стрик не двигаем

    streak = state["care_streak_days"] + 1 if state["last_care_day"] == today - 1 else 1
    await db.execute(
        "UPDATE panda_achv_state SET care_streak_days = ?, last_care_day = ? WHERE user_id = ?",
        (streak, today, user_id),
    )
    await database.commit()

    if streak >= 100:
        return ["panda_streak_7", "panda_streak_30", "panda_streak_100"]
    if streak >= 30:
        return ["panda_streak_7", "panda_streak_30"]
    if streak >= 7:
        return ["panda_streak_7"]
    return []


def _feed_count_achievements(total: int) -> list[str]:
    thresholds = [(10, "panda_feed_10"), (100, "panda_feed_100"), (500, "panda_feed_500")]
    return [achv_id for need, achv_id in thresholds if total >= need]


def _pet_count_achievements(total: int) -> list[str]:
    thresholds = [(1, "panda_first_pet"), (10, "panda_pet_10"), (100, "panda_pet_100")]
    return [achv_id for need, achv_id in thresholds if total >= need]


def _skin_count_achievements(owned_count: int) -> list[str]:
    thresholds = [(3, "panda_skins_3"), (5, "panda_skins_5"), (len(SKINS), "panda_skins_all")]
    return [achv_id for need, achv_id in thresholds if owned_count >= need]


async def _unlock_all(user_id: int, lang: str, message: Message, achv_ids: list[str]) -> None:
    """Выдаёт по очереди все ачивки из achv_ids (unlock сам по себе
    идемпотентен — уже открытые тихо возвращают None) и шлёт отдельное
    уведомление на каждую реально выданную. Общий хвост для хендлеров
    ниже, чтобы не дублировать один и тот же цикл в каждом из них."""
    for achv_id in achv_ids:
        achv_result = await achives.unlock(user_id, achv_id)
        if achv_result:
            await message.answer(achives.format_unlock_text(lang, achv_result))


def _cancel_keyboard(lang: str, owner_id: int):
    """Кнопка отмены переименования — намеренно без кастомного эмодзи,
    в отличие от остальных кнопок раздела."""
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["cancel_button"], callback_data=main.owner_cb(owner_id, "panda:setname_cancel"), style="primary")
    builder.adjust(1)
    return builder.as_markup()


# ==========================
#   ХЕНДЛЕРЫ
# ==========================

@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_panda(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    # На случай, если пользователь был в реплай-меню "Облики" и попал
    # сюда не через кнопку "Назад" (например, ввёл /start) — снимаем
    # флаг, чтобы _match_skin_label/_match_skins_back больше не ловили
    # его обычные сообщения.
    await state.update_data(panda_menu=None)
    row = await _settle(message.from_user.id)
    text, markup = _build_panda_view(lang, row, message.from_user.id)
    await message.answer_sticker(_pick_sticker(row))
    await message.answer(text, reply_markup=markup)

    # Ачивка "Годовщина" — проверяем при каждом открытии раздела, а не
    # только в момент кормления/глажки, чтобы она выдавалась даже если
    # игрок просто открыл карточку панды и ничего не делал в этот день.
    real_days = (time.time() - row["created_at"]) / SECONDS_IN_DAY
    if real_days >= PANDA_BIRTHDAY_REAL_DAYS:
        achv_result = await achives.unlock(message.from_user.id, "panda_birthday")
        if achv_result:
            await message.answer(achives.format_unlock_text(lang, achv_result))


@router.callback_query(F.data == "panda:feed")
async def on_feed_panda(callback: CallbackQuery, state: FSMContext) -> None:
    """'Покормить' больше не кормит мгновенно и полностью — вместо этого
    открывает выбор конкретного фрукта из корзины сада."""
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    user_id = callback.from_user.id
    row = await _settle(user_id)
    now = time.time()

    hunger_now = calc_hunger_percent(
        row["last_fed_at"], now, row["hunger_phase1_seconds"], row["hunger_phase2_seconds"]
    )
    if hunger_now >= 100:
        await callback.answer(t["already_full_toast"])
        return

    inventory = await garden.get_inventory(user_id)
    pantry = await bakery.get_pantry(user_id)
    if not any(count > 0 for count in inventory.values()) and not any(count > 0 for count in pantry.values()):
        await callback.answer(t["empty_basket_toast"], show_alert=True)
        return

    text, markup = _build_feed_choice(lang, inventory, pantry, user_id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("panda:feed_item:"))
async def on_feed_item(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    _, _, item_type, item_id = callback.data.split(":")
    user_id = callback.from_user.id

    if item_type == "crop":
        taken = await garden.take_from_basket(user_id, item_id)
    else:
        taken = await bakery.take_from_pantry(user_id, item_id)

    if not taken:
        # Товар закончился (например, скормлен/продан в параллельном
        # запросе) — обновляем экран выбора актуальным содержимым.
        await callback.answer(t["empty_basket_toast"])
        inventory = await garden.get_inventory(user_id)
        pantry = await bakery.get_pantry(user_id)
        row = None
        if any(count > 0 for count in inventory.values()) or any(count > 0 for count in pantry.values()):
            text, markup = _build_feed_choice(lang, inventory, pantry, user_id)
        else:
            row = await _settle(user_id)
            text, markup = _build_panda_view(lang, row, user_id)
        await _safe_edit_text(callback.message, text, reply_markup=markup)
        return

    if item_type == "crop":
        item = garden.CROPS[item_id]
        restore = garden.roll_hunger_restore(item_id)
    else:
        item = bakery.RECIPES[item_id]
        restore = bakery.roll_hunger_restore(item_id)

    row, hunger_after_feed = await restore_hunger(user_id, restore)

    await callback.answer(
        t["fed_toast"].format(
            emoji=item["emoji"], name=item["name"][lang], restore=restore
        )
    )
    text, markup = _build_panda_view(lang, row, user_id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)

    # Ачивки: "Первое кормление" — за сам факт кормления; "Верный друг" —
    # если после этого кормления голод/настроение/дружба разом на 100%;
    # плюс счётчики кормлений/стрика/недели без голода (см. раздел
    # "АЧИВКИ ПАНДЫ — СЧЁТЧИКИ/СТРИКИ" выше).
    total_feeds = await _bump_feed_count(user_id)
    never_hungry = await _touch_never_hungry(user_id, row)
    care_streaks = await _record_care_day(user_id)
    # Ачивки лавки/раздела "Пекарня" за кормление именно выпечкой
    # ("Сладкоежка"/"Праздничный торт") — счётчики живут в bakery.py,
    # тут только запрашиваем и выдаём (см. panda.py: _unlock_all).
    bakery_fed_achvs = await bakery.bump_panda_fed(user_id, item_id) if item_type == "bakery" else []
    achv_ids = [
        "first_feed",
        *_full_stats_achievement(row, hunger_after_feed),
        *_feed_count_achievements(total_feeds),
        *(["panda_hunger_100"] if hunger_after_feed >= 100 else []),
        *never_hungry,
        *care_streaks,
        # "Прямо с грядки" (garden_feed_from_basket, категория "сад") —
        # засчитывается тут, а не в garden.py, т.к. само кормление
        # (списание из корзины/выпечки) происходит именно здесь.
        *(["garden_feed_from_basket"] if item_type == "crop" else []),
        *bakery_fed_achvs,
    ]
    await _unlock_all(user_id, lang, callback.message, achv_ids)


@router.callback_query(F.data == "panda:feed_back")
async def on_feed_back(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    row = await _settle(callback.from_user.id)
    text, markup = _build_panda_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "panda:level")
async def on_open_level(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает экран "Уровень" — текущий уровень, шкала прогресса
    накопленного чудесного бамбука и кнопка ручного повышения."""
    lang = await _get_lang(state, callback.from_user.id)
    row = await _settle(callback.from_user.id)
    text, markup = _build_level_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "panda:level_up")
async def on_level_up(callback: CallbackQuery, state: FSMContext) -> None:
    """Ручное повышение уровня — списывает все нужные ресурсы (карма/
    бамбук/роса/орех — какие есть в стоимости) из инвентаря (см.
    level_up_panda). Прокачка НЕ автоматическая: срабатывает только по
    нажатию этой кнопки, и только если в инвентаре хватает КАЖДОГО
    ресурса из next_level_cost(текущий_уровень)."""
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    user_id = callback.from_user.id

    row, leveled_up = await level_up_panda(user_id)

    if not leveled_up:
        cost = next_level_cost(row["level"])
        if cost is not None:
            missing = [
                f"{max(0, amount - row[res])} {RESOURCE_EMOJI[res]}"
                for res, amount in cost.items()
                if row[res] < amount
            ]
            if missing:
                await callback.answer(
                    t["level_insufficient_toast"].format(need=", ".join(missing)),
                    show_alert=True,
                )
            else:
                await callback.answer()
        else:
            await callback.answer()
        return

    await callback.answer(t["level_up_toast"])
    text, markup = _build_level_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    bonus = round((hunger_duration_multiplier(row["level"]) - 1) * 100)
    await callback.message.answer(t["level_up_message"].format(level=row["level"], bonus=bonus))


@router.callback_query(F.data == "panda:level_back")
async def on_level_back(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    row = await _settle(callback.from_user.id)
    text, markup = _build_panda_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "panda:tree")
async def on_open_tree(callback: CallbackQuery, state: FSMContext) -> None:
    """Открывает экран "Дерево чудес" — запас редких предметов/кармы и
    кнопка "Тряхнуть дерево" (см. click_wonder_tree)."""
    lang = await _get_lang(state, callback.from_user.id)
    row = await _settle(callback.from_user.id)
    text, markup = _build_tree_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "panda:tree_click")
async def on_tree_click(callback: CallbackQuery, state: FSMContext) -> None:
    """Один клик по дереву — кликер без лимита нажатий, но с коротким
    антиспам-кулдауном между ЗАСЧИТАННЫМИ кликами (см.
    TREE_CLICK_COOLDOWN_SECONDS): Telegram не даёт редактировать одно и
    то же сообщение чаще примерно раза в секунду, а тут это происходит
    на каждый клик. Клик быстрее кулдауна просто игнорируется — ни БД,
    ни сообщение не трогаем, только лёгкий тост-подсказка."""
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    user_id = callback.from_user.id

    now = time.time()
    last = _tree_click_last_ts.get(user_id, 0.0)
    if now - last < TREE_CLICK_COOLDOWN_SECONDS:
        await callback.answer(t["tree_click_too_fast"])
        return
    _tree_click_last_ts[user_id] = now

    row, result, amount = await click_wonder_tree(user_id)

    if result == "karma":
        toast = t["tree_toast_karma"].format(amount=amount)
    else:
        toast = t[f"tree_toast_{result}"]
    await callback.answer(toast)

    text, markup = _build_tree_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data == "panda:tree_back")
async def on_tree_back(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    row = await _settle(callback.from_user.id)
    text, markup = _build_panda_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "panda:pet")
async def on_pet_panda(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    success, row, minutes_left = await pet_panda(callback.from_user.id)

    if not success:
        await callback.answer(t["pet_cooldown_toast"].format(minutes=int(minutes_left) + 1))
        return

    await callback.answer(t["pet_toast"])
    text, markup = _build_panda_view(lang, row, callback.from_user.id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)

    # Ачивка "Верный друг" — глажка тоже может довести настроение/дружбу
    # до 100% (голод при этом не меняется, но мог уже быть на 100%);
    # плюс счётчики поглаживаний/стрика/недели без голода (см. раздел
    # "АЧИВКИ ПАНДЫ — СЧЁТЧИКИ/СТРИКИ" выше).
    user_id = callback.from_user.id
    total_pets = await _bump_pet_count(user_id)
    never_hungry = await _touch_never_hungry(user_id, row)
    care_streaks = await _record_care_day(user_id)
    achv_ids = [
        *_full_stats_achievement(row),
        *_pet_count_achievements(total_pets),
        *(["panda_mood_100"] if row["mood"] >= MOOD_MAX else []),
        *(["panda_friendship_100"] if row["friendship"] >= FRIEND_MAX else []),
        *never_hungry,
        *care_streaks,
    ]
    await _unlock_all(user_id, lang, callback.message, achv_ids)


@router.callback_query(F.data == "panda:setname")
async def on_setname_request(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    row = await _settle(callback.from_user.id)
    cost = rename_cost(row)

    await state.set_state(PandaStates.waiting_name)
    await callback.answer()
    if cost > 0:
        await callback.message.answer(
            t["ask_name_paid"].format(
                min_len=NAME_MIN_LENGTH, max_len=NAME_MAX_LENGTH, cost=cost, currency=shop.CURRENCY
            ),
            reply_markup=_cancel_keyboard(lang, callback.from_user.id),
        )
    else:
        await callback.message.answer(
            t["ask_name_free"].format(min_len=NAME_MIN_LENGTH, max_len=NAME_MAX_LENGTH),
            reply_markup=_cancel_keyboard(lang, callback.from_user.id),
        )


@router.callback_query(F.data == "panda:setname_cancel")
async def on_setname_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    """Отмена переименования — работает и на первой (бесплатной)
    установке имени, и на платном переименовании."""
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    await state.set_state(None)
    await callback.answer()

    await callback.message.edit_text(t["rename_cancelled"])

    row = await _settle(callback.from_user.id)
    text, markup = _build_panda_view(lang, row, callback.from_user.id)
    await callback.message.answer(text, reply_markup=markup)


@router.message(StateFilter(PandaStates.waiting_name))
async def on_setname_received(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    t = TEXTS[lang]

    raw_name = (message.text or "").strip()
    if not raw_name or not (NAME_MIN_LENGTH <= len(raw_name) <= NAME_MAX_LENGTH):
        await message.answer(t["name_invalid"].format(min_len=NAME_MIN_LENGTH, max_len=NAME_MAX_LENGTH))
        return

    result = await set_panda_name(message.from_user.id, raw_name)
    await state.set_state(None)

    if result == "insufficient":
        row = await _settle(message.from_user.id)
        cost = rename_cost(row)
        await message.answer(t["name_insufficient"].format(cost=cost, currency=shop.CURRENCY))
        text, markup = _build_panda_view(lang, row, message.from_user.id)
        await message.answer_sticker(_pick_sticker(row))
        await message.answer(text, reply_markup=markup)
        return

    row = await _settle(message.from_user.id)
    text, markup = _build_panda_view(lang, row, message.from_user.id)
    await message.answer(t["name_saved"].format(name=html.escape(raw_name)))
    await message.answer_sticker(_pick_sticker(row))
    await message.answer(text, reply_markup=markup)

    # Ачивка "Как тебя зовут?" — за сам факт того, что панде дали имя
    # (первая установка или платное переименование — неважно).
    achv_result = await achives.unlock(message.from_user.id, "panda_named")
    if achv_result:
        await message.answer(achives.format_unlock_text(lang, achv_result))


# ==========================
#   ХЕНДЛЕРЫ — ОБЛИКИ (реплай-меню скинов)
# ==========================

@router.message(F.text.in_(LOOKS_BUTTON_TEXT.values()))
async def on_open_looks_menu(message: Message, state: FSMContext) -> None:
    """Открывает реплай-меню "Облики" — отдельное от "Моя панда",
    вызывается из главного реплай-меню бота (см. main.py)."""
    lang = await _get_lang(state, message.from_user.id)
    t = TEXTS[lang]
    user_id = message.from_user.id

    owned = await get_owned_skins(user_id)
    row = await _settle(user_id)
    coin_balance = await shop.get_balance(user_id)
    crystal_balance = await prof.get_crystals(user_id)

    await state.update_data(panda_menu="skins")
    markup = _build_skins_menu(lang, owned, row["equipped_skin_id"])
    balance_line = t["skins_balance_line"].format(
        coin_balance=coin_balance, coin_currency=shop.CURRENCY,
        crystal_balance=crystal_balance, crystal_currency=CRYSTAL_EMOJI,
    )
    await message.answer(f"{t['skins_title']}\n{balance_line}", reply_markup=markup)


async def _match_skins_back(message: Message, state: FSMContext):
    """Срабатывает на кнопку "Назад" реплай-меню "Облики".

    Раньше это дополнительно проверялось по флагу panda_menu=="skins" в
    FSM-состоянии — но это состояние живёт только в памяти процесса
    (MemoryStorage) и пропадает при каждом перезапуске бота. Из-за
    этого уже показанная пользователю кнопка "Назад" переставала
    отвечать сразу после рестарта — вплоть до повторного /start (после
    которого пользователь заново открывал "Облики" и флаг
    выставлялся снова). Текст этой кнопки ("Назад" без стрелки) больше
    нигде в боте не используется (в остальных разделах — "◀️ Назад"),
    поэтому флаг не нужен — матчим напрямую по тексту."""
    if not message.text:
        return False
    lang = await _get_lang(state, message.from_user.id)
    t = TEXTS[lang]
    return message.text == t["skins_menu_back_button"]


@router.message(_match_skins_back)
async def on_skins_menu_back(message: Message, state: FSMContext) -> None:
    """Возврат из меню "Облики" в главное реплай-меню бота.

    Берём TEXTS/main_menu_keyboard() напрямую из main.py, а не держим
    локальную копию — раньше копия была нужна, потому что `import main`
    из хендлера заново выполнял main.py целиком (он запускается как
    __main__, а не как модуль "main") и падал на повторном
    dp.include_router(...). Теперь main.py подключает роутеры только
    внутри `if __name__ == "__main__":` (см. main.setup_routers), поэтому
    такой импорт безопасен и меню больше не рассинхронизируется.
    """
    import main as _main

    lang = await _get_lang(state, message.from_user.id)
    await state.update_data(panda_menu=None)
    await message.answer(
        _main.TEXTS[lang]["menu_opened"],
        reply_markup=_main.main_menu_keyboard(lang),
    )


async def _match_skin_label(message: Message, state: FSMContext):
    """Срабатывает, если в тексте сообщения встречается название одного
    из скинов (см. комментарий ниже про устойчивость к устаревшим
    подписям кнопок).

    Раньше это дополнительно проверялось по флагу panda_menu=="skins" в
    FSM-состоянии — но оно живёт только в памяти процесса (MemoryStorage)
    и пропадает при каждом перезапуске бота, из-за чего уже показанные
    пользователю кнопки скинов переставали отвечать вплоть до
    повторного /start. Названия скинов сами по себе достаточно уникальны,
    поэтому флаг больше не требуется."""
    if not message.text:
        return False

    lang = await _get_lang(state, message.from_user.id)
    user_id = message.from_user.id

    # Сопоставляем по вхождению названия скина в текст, а не по полной
    # подписи кнопки целиком: статусный префикс/суффикс (🔒 цена / ✅ /
    # 🟢) на уже показанной пользователю клавиатуре мог устареть после
    # покупки/надевания скина в другом (инлайн) сообщении — а вот само
    # название в подписи не меняется никогда.
    skin_id = next(
        (sid for sid in SKIN_ORDER if SKINS[sid]["name"][lang] in message.text),
        None,
    )
    if skin_id is None:
        return False

    owned_skins = await get_owned_skins(user_id)
    row = await _settle(user_id)
    return {"skin_id": skin_id, "row": row, "owned_skins": owned_skins}


@router.message(_match_skin_label)
async def on_skin_selected(
    message: Message, state: FSMContext, skin_id: str, row: aiosqlite.Row, owned_skins: set[str]
) -> None:
    """Карточка конкретного скина, открытая из реплай-меню "Облики":
    сначала отдельным сообщением — стикер скина, затем — описание/цена
    и инлайн-кнопка (Купить / Надеть / Снять)."""
    lang = await _get_lang(state, message.from_user.id)
    skin = SKINS[skin_id]
    owned = skin_id in owned_skins
    equipped = row["equipped_skin_id"] == skin_id
    coin_balance = await shop.get_balance(message.from_user.id)
    crystal_balance = await prof.get_crystals(message.from_user.id)

    await message.answer_sticker(skin["sticker_id"])
    text, markup = _build_skin_detail(
        lang, skin_id, owned, equipped, coin_balance, crystal_balance, message.from_user.id
    )
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("panda:skin_buy:"))
async def on_buy_skin(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    skin_id = callback.data.split(":", 2)[2]
    skin = SKINS.get(skin_id)
    if skin is None:
        await callback.answer()
        return

    user_id = callback.from_user.id
    result = await buy_skin(user_id, skin_id)

    if result == "insufficient":
        currency_word = t["currency_word_coins"] if skin["currency"] == "coins" else t["currency_word_crystals"]
        await callback.answer(
            t["skin_insufficient_toast"].format(price=skin["price"], currency_word=currency_word),
            show_alert=True,
        )
        return

    await callback.answer(t["skin_bought_toast"] if result == "ok" else None)

    row = await _settle(user_id)
    owned_skins = await get_owned_skins(user_id)
    equipped = row["equipped_skin_id"] == skin_id
    coin_balance = await shop.get_balance(user_id)
    crystal_balance = await prof.get_crystals(user_id)
    text, markup = _build_skin_detail(
        lang, skin_id, skin_id in owned_skins, equipped, coin_balance, crystal_balance, user_id
    )
    await _safe_edit_text(callback.message, text, reply_markup=markup)

    if result == "ok":
        achv_result = await achives.unlock(user_id, "panda_skin")
        if achv_result:
            await callback.message.answer(achives.format_unlock_text(lang, achv_result))
        # "Коллекционер"/"Модный гардероб"/"Икона стиля" — за количество
        # уже купленных обликов (owned_skins выше уже учитывает эту покупку).
        await _unlock_all(
            user_id, lang, callback.message, _skin_count_achievements(len(owned_skins))
        )


@router.callback_query(F.data.startswith("panda:skin_equip:"))
async def on_equip_skin(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    skin_id = callback.data.split(":", 2)[2]
    if skin_id not in SKINS:
        await callback.answer()
        return

    user_id = callback.from_user.id
    ok = await equip_skin(user_id, skin_id)
    if not ok:
        # Скин не куплен (например, кто-то успел сбросить прогресс
        # параллельно) — просто гасим часики без изменения экрана.
        await callback.answer()
        return

    await callback.answer(t["skin_equipped_toast"])
    coin_balance = await shop.get_balance(user_id)
    crystal_balance = await prof.get_crystals(user_id)
    text, markup = _build_skin_detail(lang, skin_id, True, True, coin_balance, crystal_balance, user_id)
    await _safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("panda:skin_unequip:"))
async def on_unequip_skin(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    skin_id = callback.data.split(":", 2)[2]
    if skin_id not in SKINS:
        await callback.answer()
        return

    await equip_skin(callback.from_user.id, None)

    await callback.answer(t["skin_unequipped_toast"])
    coin_balance = await shop.get_balance(callback.from_user.id)
    crystal_balance = await prof.get_crystals(callback.from_user.id)
    text, markup = _build_skin_detail(
        lang, skin_id, True, False, coin_balance, crystal_balance, callback.from_user.id
    )
    await _safe_edit_text(callback.message, text, reply_markup=markup)
