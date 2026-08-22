"""
Раздел "Рынок".

Идея:
    Собственная валюта — Pn (Пандакоины). Игрок может:
      1. Выставить свои фрукты из корзины сада на продажу другим
         игрокам по собственной цене (в Pn за штуку).
      2. Купить чужой лот целиком — Pn списываются у покупателя и
         зачисляются продавцу, фрукты переходят покупателю в корзину.
      3. Снять свой лот с продажи — фрукты возвращаются в корзину.
      4. Мгновенно продать фрукты боту напрямую, без ожидания
         покупателя — но по цене на 40% ниже базовой рыночной
         (см. INSTANT_SELL_DISCOUNT / BASE_PRICES).

    Список лотов можно фильтровать по конкретному виду фрукта.

Зависимость от сада:
    Рынок ничего не знает о механике выращивания — он только читает
    и изменяет корзину через garden.get_inventory / garden.add_to_basket /
    garden.take_from_basket_bulk и подписи/эмодзи культур из garden.CROPS.

Хранение:
    Общая база данных бота (см. database.py) — единое asyncio-соединение
    на весь процесс, WAL-режим, запись "стопками" (батч-коммиты), кроме
    операций с переходом Pn/товара между игроками — те сохраняются на
    диск немедленно (database.flush()). Гонки между параллельными
    запросами одного игрока закрыты локом — database.user_lock(user_id).

Подключение в main.py:
    import shop
    dp.include_router(shop.router)   # после panda.router и garden.router

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import time

import aiosqlite
from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import garden

router = Router(name="shop")


# ==========================
#   СОСТОЯНИЯ (FSM)
# ==========================

class ShopStates(StatesGroup):
    waiting_price = State()
    waiting_quantity = State()


# ==========================
#   НАСТРОЙКИ
# ==========================

PAGE_SIZE = 5
QUICK_QUANTITIES = [1, 5, 10]
MAX_LISTING_COUNT = 999
MAX_LISTINGS_PER_PLAYER = 10

# Цена мгновенного выкупа ботом — на столько ниже средней текущей
# ставки на этот фрукт на рынке (см. get_average_market_price).
INSTANT_SELL_DISCOUNT = 0.4

CURRENCY_EMOJI_ID = "5449418135381759397"
# Валюта отображается кастомным premium-emoji, но он рендерится ТОЛЬКО
# там, где Telegram поддерживает HTML-разметку/entities — в тексте
# сообщений. Тексты кнопок (InlineKeyboardButton) и алерты
# callback.answer(show_alert=True) HTML не поддерживают вообще — там
# тег <tg-emoji> показался бы как сырой текст. Поэтому два варианта:
#   CURRENCY       — для текста сообщений (HTML parse_mode)
#   CURRENCY_PLAIN — для кнопок и toast-алертов
CURRENCY = f'<tg-emoji emoji-id="{CURRENCY_EMOJI_ID}">🪙</tg-emoji>'
CURRENCY_PLAIN = "🪙"
CURRENCY_FULL = {
    "ru": "Пандакоины",
    "en": "Pandacoins",
}

# Допустимый диапазон цены за 1 штуку (Pn), который игрок может
# назначить своему лоту — свой для каждой культуры. Используется и
# как запасное значение "средней ставки" для мгновенной продажи, если
# на рынке пока вообще нет активных лотов на этот фрукт.
PRICE_RANGES = {
    "bamboo": (100, 250),
    "tangerine": (110, 260),
    "grape": (115, 270),
    "apple": (120, 280),
    "pear": (125, 290),
    "banana": (135, 310),
    "mango": (145, 330),
    "pineapple": (150, 350),
}

# Кастомные emoji (из премиум-пака) для оформления текстов и кнопок
# раздела "Рынок". НЕ используются для эмодзи фруктов — те приходят
# из garden.CROPS и не трогаются.
EMOJI_STATS_ID = "5231200819986047254"    # 📊 статистика / средняя цена
EMOJI_CART_ID = "5229064374403998351"     # 🛍 покупки / кнопка "купить"
EMOJI_EARNED_ID = "5409048419211682843"   # 💵 продано / заработано (уведомление о продаже)
EMOJI_INSTANT_ID = "5456140674028019486"  # ⚡ мгновенная продажа
EMOJI_CANCEL_ID = "5210952531676504517"   # ❌ снять с продажи
EMOJI_CHECK_ID = "5206607081334906820"    # ✔️ успешное действие
EMOJI_SEARCH_ID = "5231012545799666522"   # 🔍 фильтр
EMOJI_PIN_ID = "5397782960512444700"      # 📌 справочная информация
EMOJI_TREND_ID = "5244837092042750681"    # 📈 диапазон цен
EMOJI_INFO_ID = "5334544901428229844"     # ℹ️ "как это работает"
EMOJI_BALANCE_ID = "5224257782013769471"  # 💰 баланс
EMOJI_BOUGHT_ID = "5449683594425410231"   # 🔼 куплено
EMOJI_SOLD_ID = "5447183459602669338"     # 🔽 продано
EMOJI_NUM1_ID = "5830126888357468979"     # 1️⃣
EMOJI_NUM2_ID = "5830254543375441108"     # 2️⃣
EMOJI_NUM3_ID = "5827786453303696733"     # 3️⃣
EMOJI_MARKET_ID = "5920332557466997677"   # 🏪 тот же, что на кнопке "Рынок" в главном меню (main.py)
EMOJI_LOOK_ID = "5210956306952758910"     # 👀 кнопка "Смотреть лоты"
EMOJI_MYLISTINGS_ID = "5884479287171485878"  # 📦 кнопка "Мои лоты"
EMOJI_SELL_ID = "5397916757333654639"     # ➕ кнопка "Выставить на продажу"
EMOJI_METEOR_ID = "5224607267797606837"   # ☄️ кнопка "Быстрая продажа боту"
EMOJI_SELLER_ID = "5436382926818256347"   # 🤠 продавец лота
EMOJI_NEXT_PAGE_ID = "5253767677670862169"  # 🔜 кнопка "вперёд" в пагинации
EMOJI_PREV_PAGE_ID = "5255703720078879038"  # 🔙 кнопка "назад" в пагинации
EMOJI_BACK_ID = "6039539366177541657"     # ⬅️ кнопка "Назад" (выход в предыдущее меню)


def _ce(emoji_id: str, glyph: str) -> str:
    """HTML-тег кастомного emoji для сообщений (parse_mode=HTML).
    Не использовать в текстах алертов callback.answer(show_alert=True) —
    там HTML не рендерится и тег покажется как обычный текст."""
    return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'


CE_STATS = _ce(EMOJI_STATS_ID, "📊")
CE_CART = _ce(EMOJI_CART_ID, "🛍")
CE_EARNED = _ce(EMOJI_EARNED_ID, "💵")
CE_INSTANT = _ce(EMOJI_INSTANT_ID, "⚡")
CE_CANCEL = _ce(EMOJI_CANCEL_ID, "❌")
CE_CHECK = _ce(EMOJI_CHECK_ID, "✔️")
CE_SEARCH = _ce(EMOJI_SEARCH_ID, "🔍")
CE_PIN = _ce(EMOJI_PIN_ID, "📌")
CE_TREND = _ce(EMOJI_TREND_ID, "📈")
CE_INFO = _ce(EMOJI_INFO_ID, "ℹ️")
CE_BALANCE = _ce(EMOJI_BALANCE_ID, "💰")
CE_BOUGHT = _ce(EMOJI_BOUGHT_ID, "🔼")
CE_SOLD = _ce(EMOJI_SOLD_ID, "🔽")
CE_NUM1 = _ce(EMOJI_NUM1_ID, "1️⃣")
CE_NUM2 = _ce(EMOJI_NUM2_ID, "2️⃣")
CE_NUM3 = _ce(EMOJI_NUM3_ID, "3️⃣")
CE_MARKET = _ce(EMOJI_MARKET_ID, "🏪")
CE_MYLISTINGS = _ce(EMOJI_MYLISTINGS_ID, "📦")
CE_SELL = _ce(EMOJI_SELL_ID, "➕")
CE_SELLER = _ce(EMOJI_SELLER_ID, "🤠")


# ==========================
#   ТИПЫ ТОВАРА (фрукты сада / выпечка пекарни)
# ==========================
#
# Рынок раньше торговал только фруктами (crop_id ссылался напрямую на
# garden.CROPS). Теперь лот может быть либо фруктом, либо готовым
# изделием из пекарни — колонка crop_id в shop_listings хранит id
# товара в обоих случаях, а новая колонка item_type ("crop"/"bakery")
# говорит, как её читать. Эта секция — единственное место, которое
# знает, как сходить за метаданными/остатками для каждого из двух
# видов; остальной код рынка работает с ITEM_CROP/ITEM_BAKERY, не
# заботясь о том, что за модуль (garden/bakery) стоит за ними.

ITEM_CROP = "crop"
ITEM_BAKERY = "bakery"

# bakery.py на этапе импорта строит свой TEXTS и обращается к
# shop.CE_BALANCE / shop.CURRENCY / shop.CURRENCY_PLAIN — а он же сам
# импортирует shop.py (см. bakery.py: "import shop"). Чтобы разорвать
# цикл, импортируем bakery только здесь — ПОСЛЕ того, как нужные ему
# CE_*/CURRENCY* константы уже объявлены выше в этом файле.
import bakery

# Диапазоны цен на выпечку раньше жили тут отдельной статичной таблицей
# (BAKERY_PRICE_RANGES) и были рассинхронизированы с реальной себестоимостью
# рецептов — из-за этого продажа выпечки почти всегда уходила в минус.
# Теперь диапазон считается динамически от себестоимости рецепта (сумма
# цен ингредиентов + рыночная стоимость фруктов) — см.
# bakery.get_market_price_range / bakery.get_recipe_cost. Так цена всегда
# остаётся окупаемой, даже если цены на ингредиенты в лавке пекарни
# поменяются.


def _item_order(item_type: str) -> list:
    return garden.CROP_ORDER if item_type == ITEM_CROP else bakery.RECIPE_ORDER


def _item_meta(item_type: str, item_id: str) -> dict:
    return garden.CROPS[item_id] if item_type == ITEM_CROP else bakery.RECIPES[item_id]


def _price_range(item_type: str, item_id: str) -> tuple[int, int]:
    if item_type == ITEM_CROP:
        return PRICE_RANGES[item_id]
    return bakery.get_market_price_range(item_id)


async def _get_type_inventory(user_id: int, item_type: str) -> dict[str, int]:
    if item_type == ITEM_CROP:
        return await garden.get_inventory(user_id)
    return await bakery.get_pantry(user_id)


async def _take_from_inventory(user_id: int, item_type: str, item_id: str, count: int) -> bool:
    if item_type == ITEM_CROP:
        return await garden.take_from_basket_bulk(user_id, item_id, count)
    return await bakery.take_from_pantry_bulk(user_id, item_id, count)


async def _add_to_inventory(user_id: int, item_type: str, item_id: str, count: int) -> None:
    if item_type == ITEM_CROP:
        await garden.add_to_basket(user_id, item_id, count)
    else:
        await bakery.add_to_pantry(user_id, item_id, count)


CATEGORY_EMOJI = {ITEM_CROP: "🍎", ITEM_BAKERY: "🥐"}
CATEGORY_NAME = {
    ITEM_CROP: {"ru": "Фрукты", "en": "Fruit"},
    ITEM_BAKERY: {"ru": "Выпечка", "en": "Bakery"},
}


def _category_label(lang: str, item_type: str) -> tuple[str, str]:
    return CATEGORY_EMOJI[item_type], CATEGORY_NAME[item_type][lang]


def _encode_filter(item_type: str, item_id: str | None) -> str:
    """item_id=None кодирует "вся категория" (все фрукты / вся выпечка)."""
    return f"{item_type}_{item_id if item_id is not None else 'all'}"


def _decode_filter(token: str):
    """Возвращает (item_type, item_id):
    - (None, None) — фильтра нет, показываем все товары;
    - (item_type, None) — вся категория (все фрукты / вся выпечка);
    - (item_type, item_id) — конкретный товар."""
    if token == "all":
        return None, None
    item_type, item_id = token.split("_", 1)
    if item_id == "all":
        return item_type, None
    return item_type, item_id


async def get_average_market_price(item_type: str, item_id: str) -> float:
    """Средняя цена за штуку среди активных лотов на этот товар. Если
    сейчас лотов нет — берётся середина диапазона цен как ориентир."""
    db = await database.get_db()
    async with db.execute(
        "SELECT AVG(price) AS avg_price FROM shop_listings WHERE item_type = ? AND crop_id = ?",
        (item_type, item_id),
    ) as cursor:
        row = await cursor.fetchone()

    if row is not None and row["avg_price"] is not None:
        return row["avg_price"]

    lo, hi = _price_range(item_type, item_id)
    return (lo + hi) / 2


async def instant_sell_unit_price(item_type: str, item_id: str) -> int:
    if item_type == ITEM_BAKERY:
        # У выпечки своя, независимая от текущих лотов на рынке цена
        # выкупа — фикс. +25% к себестоимости рецепта (см.
        # bakery.get_instant_sell_price). Общая формула "-40% от средней
        # цены лотов" тут не подходит: для дорогой выпечки при узком
        # рыночном диапазоне она легко уводит выкуп ниже себестоимости.
        return bakery.get_instant_sell_price(item_id)

    avg = await get_average_market_price(item_type, item_id)
    price = round(avg * (1 - INSTANT_SELL_DISCOUNT))
    return max(1, price)


# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

BUTTON_TEXT = {
    "ru": "Рынок",
    "en": "Market",
}

TEXTS = {
    "ru": {
        "title": f"{CE_MARKET} <b>Рынок</b>",
        "welcome_title": f"{CE_MARKET} <b>Приветствуем на фруктовом рынке!</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "balance_line": f"{CE_BALANCE} <b>Баланс:</b> <b>{{balance}} {CURRENCY}</b>",
        "stats_header": f"{CE_STATS} <b>Ваша статистика</b>",
        "stats_bought_line": f"{CE_BOUGHT} <b>Куплено:</b> <b>{{bought}} шт.</b> · <b>Потрачено:</b> <b>{{spent}} {CURRENCY}</b>",
        "stats_sold_line": f"{CE_SOLD} <b>Продано:</b> <b>{{sold}} шт.</b> · <b>Заработано:</b> <b>{{earned}} {CURRENCY}</b>",
        "main_hint": (
            f"<blockquote>{CE_INFO} <b>Как это работает:</b>\n"
            f"<i>{CE_NUM1} Выставляете фрукты по своей цене в допустимом диапазоне</i>\n"
            f"<i>{CE_NUM2} Другой игрок покупает лот целиком</i>\n"
            f"<i>{CE_NUM3} Либо продаёте боту мгновенно — быстро, но −40% от рынка</i></blockquote>"
        ),
        "btn_browse": "Смотреть лоты",
        "btn_my_listings": "Мои лоты",
        "btn_sell": "Выставить на продажу",
        "btn_instant": "Быстрая продажа боту",
        "back_button": "Назад",
        "prev_page_button": "Пред.",
        "next_page_button": "След.",
        "filter_button": "Фильтр по товару",
        "filter_title": f"{CE_SEARCH} <b>Фильтр по товару</b>\n<i>Выберите категорию</i>",
        "filter_all": "Все товары",
        "filter_category_title": f"{CE_SEARCH} <b>{{emoji}} {{name}}</b>\n<i>Выберите товар или всю категорию</i>",
        "filter_category_all": "Вся категория: {name}",
        "filter_active_line": "<i>Фильтр: {emoji} {name}</i>",
        "page_line": "<i>Страница {page}/{pages}</i>",
        "empty_market": "<i>Пока здесь пусто — загляните позже или выставьте свой лот первым!</i>",
        "listing_item_line": "{emoji} <b>{name}</b> ×{count}",
        "listing_price_line": f"{CE_BALANCE} <b>Цена:</b> <b>{{price}} {CURRENCY}</b>/шт · <b>Итого:</b> <b>{{total}} {CURRENCY}</b>",
        "listing_info_header": f"{CE_PIN} <b>Для справки:</b>",
        "listing_range_line": f"{CE_TREND} <i>Диапазон цен: {{min}}–{{max}} {CURRENCY}/шт</i>",
        "listing_avg_line": f"{CE_STATS} <i>Средняя цена сейчас: {{avg}} {CURRENCY}/шт</i>",
        "listing_seller_line": f"{CE_SELLER} <i>Продавец: {{seller}}</i>",
        "listing_button": f"{{price}}/шт — {{emoji}} {{name}} ×{{count}}",
        "buy_button": f"Купить за {{total}} {CURRENCY_PLAIN}",
        "seller_label": "Игрок №{id}",
        "bought_toast": f"Куплено: {{emoji}} {{name}} ×{{count}} — списано {{total}} {CURRENCY_PLAIN}",
        "sold_notice_toast": f"{CE_EARNED} Ваш лот куплен: {{emoji}} {{name}} ×{{count}} — начислено {{total}} {CURRENCY}",
        "not_enough_balance_toast": f"Недостаточно {CURRENCY_PLAIN} для этой покупки.",
        "listing_gone_toast": "Этот лот уже продан или снят с продажи.",
        "own_listing_toast": "Нельзя купить собственный лот — снимите его через «Мои лоты», если передумали.",
        "my_listings_title": f"{CE_MYLISTINGS} <b>Мои лоты</b>",
        "my_listing_line": (
            "{emoji} <b>{name}</b> ×{count}\n"
            f"{CE_BALANCE} <b>Цена:</b> <b>{{price}} {CURRENCY}</b>/шт · <b>Итого:</b> <b>{{total}} {CURRENCY}</b>"
        ),
        "my_listing_button": f"{{price}}/шт — {{emoji}} {{name}} ×{{count}}",
        "cancel_button": "Снять с продажи",
        "no_my_listings": "<i>У вас пока нет активных лотов на рынке.</i>",
        "cancelled_toast": "Лот снят, фрукты возвращены в корзину.",
        "sell_category_title": f"{CE_SELL} <b>Что выставить на продажу?</b>\n<i>Выберите категорию товара</i>",
        "cat_fruits_button": "🍎 Фрукты",
        "cat_bakery_button": "🥐 Выпечка",
        "category_empty_toast": "Здесь пока пусто — нечего выставить из этой категории.",
        "sell_choose_title": f"{CE_SELL} <b>Что выставить на продажу?</b>\n<i>Выберите фрукт из корзины сада</i>",
        "sell_choose_title_bakery": f"{CE_SELL} <b>Что выставить на продажу?</b>\n<i>Выберите изделие из витрины пекарни</i>",
        "sell_empty_basket": "🧺 Корзина пуста — сначала соберите фрукты в саду.",
        "sell_empty_pantry": "🥐 Витрина пекарни пуста — сначала испеките что-нибудь.",
        "sell_item_button": "{emoji} {name} ×{count}",
        "sell_qty_title": (
            "{emoji} <b>{name}</b>\n"
            "<i>Сколько штук выставить на продажу? В корзине: {available}</i>"
        ),
        "qty_button": "×{qty}",
        "qty_all_button": "Всё ({available})",
        "qty_custom_button": "Своё количество",
        "ask_quantity": (
            "{emoji} <b>{name}</b>\n"
            "<i>Введите количество штук (от 1 до {available}):</i>"
        ),
        "quantity_invalid": "<i>Неверное количество — ожидалось целое число от 1 до {available}. Ввод отменён, попробуйте снова.</i>",
        "input_cancel_button": "❌ Отмена",
        "input_cancelled_toast": "Ввод отменён.",
        "ask_price": (
            "{emoji} <b>{name}</b> ×{count}\n"
            f"<i>Назначьте цену за 1 штуку в {CURRENCY} — для этого фрукта "
            f"допустимо от {{min_price}} до {{max_price}} {CURRENCY}:</i>"
        ),
        "price_invalid": f"<i>Неверная цена — ожидалось целое число от {{min_price}} до {{max_price}} {CURRENCY}. Ввод отменён, попробуйте снова.</i>",
        "listed_success": (
            f"{CE_CHECK} <b>Лот выставлен на рынок!</b>\n"
            f"<i>{{emoji}} {{name}} ×{{count}} по <b>{{price}} {CURRENCY}</b>/шт "
            f"(итого <b>{{total}} {CURRENCY}</b>)</i>"
        ),
        "sell_failed_toast": "Не хватает фруктов в корзине — возможно, они уже были потрачены.",
        "listing_limit_toast": f"У вас уже максимум лотов на рынке ({MAX_LISTINGS_PER_PLAYER}) — сначала снимите один из «Моих лотов», чтобы выставить новый.",
        "instant_category_title": (
            f"{CE_INSTANT} <b>Быстрая продажа боту</b>\n"
            "<i>Бот покупает сразу, без ожидания покупателя. Выберите категорию:</i>"
        ),
        "instant_choose_title": (
            f"{CE_INSTANT} <b>Быстрая продажа боту</b>\n"
            "<i>Бот покупает сразу, но на 40% дешевле рыночной цены. "
            "Выберите фрукт из корзины:</i>"
        ),
        "instant_choose_title_bakery": (
            f"{CE_INSTANT} <b>Быстрая продажа боту</b>\n"
            "<i>Бот покупает сразу, по фиксированной цене чуть выше себестоимости. "
            "Выберите изделие из витрины пекарни:</i>"
        ),
        "instant_item_button": f"{{price}}/шт · {{emoji}} {{name}} ×{{count}}",
        "instant_qty_title": (
            "{emoji} <b>{name}</b>\n"
            f"<i>Бот покупает по {CE_BALANCE}{{price}} {CURRENCY}/шт. В корзине: "
            "{available}. Сколько продать?</i>"
        ),
        "instant_sold_toast": f"Продано боту: {{emoji}} {{name}} ×{{count}} — получено {{total}} {CURRENCY_PLAIN}",
        "instant_empty_basket": "🧺 Корзина пуста — сначала соберите фрукты в саду.",
        "instant_empty_pantry": "🥐 Витрина пекарни пуста — сначала испеките что-нибудь.",
    },
    "en": {
        "title": f"{CE_MARKET} <b>Market</b>",
        "welcome_title": f"{CE_MARKET} <b>Welcome to the fruit market!</b>",
        "separator": "<b><code>·  ·  ·  ◆  ·  ·  ·</code></b>",
        "balance_line": f"{CE_BALANCE} <b>Balance:</b> <b>{{balance}} {CURRENCY}</b>",
        "stats_header": f"{CE_STATS} <b>Your stats</b>",
        "stats_bought_line": f"{CE_BOUGHT} <b>Bought:</b> <b>{{bought}} pcs</b> · <b>Spent:</b> <b>{{spent}} {CURRENCY}</b>",
        "stats_sold_line": f"{CE_SOLD} <b>Sold:</b> <b>{{sold}} pcs</b> · <b>Earned:</b> <b>{{earned}} {CURRENCY}</b>",
        "main_hint": (
            f"<blockquote>{CE_INFO} <b>How it works:</b>\n"
            f"<i>{CE_NUM1} List your fruit at your own price within the allowed range</i>\n"
            f"<i>{CE_NUM2} Another player buys the listing in full</i>\n"
            f"<i>{CE_NUM3} Or sell straight to the bot — instant, but −40% below market</i></blockquote>"
        ),
        "btn_browse": "Browse listings",
        "btn_my_listings": "My listings",
        "btn_sell": "List for sale",
        "btn_instant": "Instant sell to bot",
        "back_button": "Back",
        "prev_page_button": "Prev",
        "next_page_button": "Next",
        "filter_button": "Filter by item",
        "filter_title": f"{CE_SEARCH} <b>Filter by item</b>\n<i>Choose a category</i>",
        "filter_all": "All items",
        "filter_category_title": f"{CE_SEARCH} <b>{{emoji}} {{name}}</b>\n<i>Choose an item or the whole category</i>",
        "filter_category_all": "Whole category: {name}",
        "filter_active_line": "<i>Filter: {emoji} {name}</i>",
        "page_line": "<i>Page {page}/{pages}</i>",
        "empty_market": "<i>Nothing here yet — check back later or be the first to list something!</i>",
        "listing_item_line": "{emoji} <b>{name}</b> ×{count}",
        "listing_price_line": f"{CE_BALANCE} <b>Price:</b> <b>{{price}} {CURRENCY}</b>/ea · <b>Total:</b> <b>{{total}} {CURRENCY}</b>",
        "listing_info_header": f"{CE_PIN} <b>For reference:</b>",
        "listing_range_line": f"{CE_TREND} <i>Price range: {{min}}–{{max}} {CURRENCY}/ea</i>",
        "listing_avg_line": f"{CE_STATS} <i>Current average: {{avg}} {CURRENCY}/ea</i>",
        "listing_seller_line": f"{CE_SELLER} <i>Seller: {{seller}}</i>",
        "listing_button": f"{{price}}/ea — {{emoji}} {{name}} ×{{count}}",
        "buy_button": f"Buy for {{total}} {CURRENCY_PLAIN}",
        "seller_label": "Player #{id}",
        "bought_toast": f"Bought: {{emoji}} {{name}} ×{{count}} — {{total}} {CURRENCY_PLAIN} spent",
        "sold_notice_toast": f"{CE_EARNED} Your listing was bought: {{emoji}} {{name}} ×{{count}} — {{total}} {CURRENCY} earned",
        "not_enough_balance_toast": f"Not enough {CURRENCY_PLAIN} for this purchase.",
        "listing_gone_toast": "This listing has already been sold or removed.",
        "own_listing_toast": "You can't buy your own listing — cancel it in \"My listings\" if you changed your mind.",
        "my_listings_title": f"{CE_MYLISTINGS} <b>My listings</b>",
        "my_listing_line": (
            "{emoji} <b>{name}</b> ×{count}\n"
            f"{CE_BALANCE} <b>Price:</b> <b>{{price}} {CURRENCY}</b>/ea · <b>Total:</b> <b>{{total}} {CURRENCY}</b>"
        ),
        "my_listing_button": f"{{price}}/ea — {{emoji}} {{name}} ×{{count}}",
        "cancel_button": "Remove listing",
        "no_my_listings": "<i>You don't have any active listings on the market yet.</i>",
        "cancelled_toast": "Listing removed, fruit returned to your basket.",
        "sell_category_title": f"{CE_SELL} <b>What would you like to sell?</b>\n<i>Choose an item category</i>",
        "cat_fruits_button": "🍎 Fruit",
        "cat_bakery_button": "🥐 Bakery",
        "category_empty_toast": "Nothing here yet — there's nothing to list from this category.",
        "sell_choose_title": f"{CE_SELL} <b>What would you like to sell?</b>\n<i>Choose a fruit from your garden basket</i>",
        "sell_choose_title_bakery": f"{CE_SELL} <b>What would you like to sell?</b>\n<i>Choose an item from the bakery showcase</i>",
        "sell_empty_basket": "🧺 Your basket is empty — go pick some fruit in the garden first.",
        "sell_empty_pantry": "🥐 The bakery showcase is empty — bake something first.",
        "sell_item_button": "{emoji} {name} ×{count}",
        "sell_qty_title": (
            "{emoji} <b>{name}</b>\n"
            "<i>How many to list? In basket: {available}</i>"
        ),
        "qty_button": "×{qty}",
        "qty_all_button": "All ({available})",
        "qty_custom_button": "Custom amount",
        "ask_quantity": (
            "{emoji} <b>{name}</b>\n"
            "<i>Enter the quantity (1 to {available}):</i>"
        ),
        "quantity_invalid": "<i>Invalid quantity — expected a whole number from 1 to {available}. Input cancelled, please try again.</i>",
        "input_cancel_button": "❌ Cancel",
        "input_cancelled_toast": "Input cancelled.",
        "ask_price": (
            "{emoji} <b>{name}</b> ×{count}\n"
            f"<i>Set the price for 1 unit in {CURRENCY} — this fruit "
            f"allows {{min_price}} to {{max_price}} {CURRENCY}:</i>"
        ),
        "price_invalid": f"<i>Invalid price — expected a whole number from {{min_price}} to {{max_price}} {CURRENCY}. Input cancelled, please try again.</i>",
        "listed_success": (
            f"{CE_CHECK} <b>Listed on the market!</b>\n"
            f"<i>{{emoji}} {{name}} ×{{count}} at <b>{{price}} {CURRENCY}</b>/ea "
            f"(total <b>{{total}} {CURRENCY}</b>)</i>"
        ),
        "sell_failed_toast": "Not enough fruit in your basket — it may have already been spent.",
        "listing_limit_toast": f"You already have the maximum number of listings ({MAX_LISTINGS_PER_PLAYER}) — remove one from \"My listings\" first to add a new one.",
        "instant_category_title": (
            f"{CE_INSTANT} <b>Instant sell to bot</b>\n"
            "<i>The bot buys immediately, no need to wait for a buyer. Choose a category:</i>"
        ),
        "instant_choose_title": (
            f"{CE_INSTANT} <b>Instant sell to bot</b>\n"
            "<i>The bot buys immediately, at 40% below market price. "
            "Choose a fruit from your basket:</i>"
        ),
        "instant_choose_title_bakery": (
            f"{CE_INSTANT} <b>Instant sell to bot</b>\n"
            "<i>The bot buys immediately, at a fixed price just above production cost. "
            "Choose an item from the bakery showcase:</i>"
        ),
        "instant_item_button": f"{{price}}/ea · {{emoji}} {{name}} ×{{count}}",
        "instant_qty_title": (
            "{emoji} <b>{name}</b>\n"
            f"<i>The bot buys at {CE_BALANCE}{{price}} {CURRENCY}/ea. In basket: "
            "{available}. How many to sell?</i>"
        ),
        "instant_sold_toast": f"Sold to bot: {{emoji}} {{name}} ×{{count}} — {{total}} {CURRENCY_PLAIN} earned",
        "instant_empty_basket": "🧺 Your basket is empty — go pick some fruit in the garden first.",
        "instant_empty_pantry": "🥐 The bakery showcase is empty — bake something first.",
    },
}


# ==========================
#   ХРАНИЛИЩЕ (общая БД — см. database.py)
# ==========================
#
# Своего соединения и своих таблиц этот модуль больше не создаёт —
# и то, и другое теперь общее для всего бота, в database.py.


# --- баланс ---

async def get_balance(user_id: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT balance FROM shop_balance WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    return row["balance"] if row else 0


async def _change_balance(user_id: int, delta: int) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO shop_balance (user_id, balance) VALUES (?, ?)
        ON CONFLICT (user_id) DO UPDATE SET balance = balance + excluded.balance
        """,
        (user_id, delta),
    )
    await database.commit()

    # Общая ачивка "Богач" (general_coins_earned_10000, achives.py) —
    # считает ЗАРАБОТАННОЕ (не текущий баланс — он может тратиться), а
    # _change_balance — единственная низкоуровневая точка, через которую
    # проходит вообще любое начисление/списание Pn в боте (add_balance,
    # buy_listing — и покупателю, и продавцу, instant_sell, charge_balance),
    # так что это самое надёжное место для счётчика. Только начисления
    # (delta > 0) — списание не отматывает "заработано" назад. Локальный
    # импорт (prof.py импортирует shop на верхнем уровне — обратный
    # импорт тут завёл бы цикл, тот же приём, что и в garden.py/bakery.py).
    # Без database.user_lock — по той же причине, что и у самой
    # _change_balance: вызывается и из мест, уже держащих лок на этот
    # user_id (add_balance), и из мест без лока вовсе (buy_listing —
    # для seller_id); helper в prof.py спроектирован как lock-free.
    if delta > 0:
        import prof

        await prof.bump_coins_earned_no_lock(user_id, delta)


async def add_balance(user_id: int, amount: int) -> int:
    """Публичная обёртка над _change_balance для ручной выдачи/коррекции
    баланса (используется админкой — admin.py). amount может быть
    отрицательным, чтобы списать Pn; в этом случае баланс не уводится
    в минус — списывается не больше, чем есть на счету. Экономическая
    операция — сохраняется на диск немедленно, как и остальные операции
    с переходом Pn в этом модуле. Возвращает итоговый баланс."""
    async with database.user_lock(user_id):
        if amount < 0:
            current = await get_balance(user_id)
            amount = -min(-amount, current)
        if amount != 0:
            await _change_balance(user_id, amount)
            await database.flush()
        return await get_balance(user_id)


async def charge_balance(user_id: int, amount: int) -> bool:
    """Списывает amount Pn (amount > 0), не давая балансу уйти в минус.
    Возвращает False, если Pn не хватило — в этом случае баланс не
    трогается вообще.

    В отличие от add_balance, эта функция НЕ берёт database.user_lock
    сама — она рассчитана на вызов из кода других модулей (например,
    bakery.py), которые уже держат свой user_lock(user_id) как часть
    более крупной операции (проверили ингредиенты, списали их, теперь
    списывают Pn — всё одним куском). Повторный захват того же лока
    внутри уже открытого `async with database.user_lock(...)` привёл бы
    к вечному ожиданию (asyncio.Lock не реентерабелен), поэтому вызывать
    charge_balance без внешнего лока небезопасно при параллельных
    запросах — используйте add_balance, если лок ещё не захвачен."""
    balance = await get_balance(user_id)
    if balance < amount:
        return False
    await _change_balance(user_id, -amount)
    await database.flush()
    return True


# --- статистика (для главного экрана рынка) ---

async def _add_stats(
    user_id: int, *, bought: int = 0, spent: int = 0, sold: int = 0, earned: int = 0
) -> None:
    db = await database.get_db()
    await db.execute(
        """
        INSERT INTO shop_stats (user_id, total_bought, total_spent, total_sold, total_earned)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            total_bought = total_bought + excluded.total_bought,
            total_spent = total_spent + excluded.total_spent,
            total_sold = total_sold + excluded.total_sold,
            total_earned = total_earned + excluded.total_earned
        """,
        (user_id, bought, spent, sold, earned),
    )
    await database.commit()


async def get_stats(user_id: int) -> dict:
    db = await database.get_db()
    async with db.execute(
        "SELECT total_bought, total_spent, total_sold, total_earned, "
        "bought_crop, bought_bakery FROM shop_stats WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return {
            "total_bought": 0, "total_spent": 0, "total_sold": 0, "total_earned": 0,
            "bought_crop": 0, "bought_bakery": 0,
        }
    return dict(row)


# Однозначные геттеры под сигнатуру PROGRESS_PROVIDERS в achives.py
# (Callable[[int], Awaitable[int]]) — карточка счётных рыночных ачивок
# ("Ярмарка изобилия", "Оптовик", "Мешок с деньгами", "Удачная сделка",
# "Рыночная империя") рисует по ним реальный прогресс "X/Y".

async def get_total_bought(user_id: int) -> int:
    return (await get_stats(user_id))["total_bought"]


async def get_total_spent(user_id: int) -> int:
    return (await get_stats(user_id))["total_spent"]


async def get_total_sold(user_id: int) -> int:
    return (await get_stats(user_id))["total_sold"]


async def get_total_earned(user_id: int) -> int:
    return (await get_stats(user_id))["total_earned"]


# --- торговые партнёры (для ачивок "Постоянный клиент"/"Своя клиентура") ---

async def _mark_trade_partner(user_id: int, partner_id: int, direction: str) -> None:
    """Запоминает, что user_id хоть раз торговал с partner_id в эту
    сторону (direction: "bought" — купил у partner_id, "sold" — продал
    partner_id). INSERT OR IGNORE — повторная сделка с тем же партнёром
    не создаёт вторую строку, COUNT(*) по direction всегда равен числу
    РАЗНЫХ партнёров."""
    db = await database.get_db()
    await db.execute(
        "INSERT OR IGNORE INTO shop_trade_partners (user_id, partner_id, direction, first_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, partner_id, direction, time.time()),
    )
    await database.commit()


async def get_distinct_trade_partners_count(user_id: int, direction: str) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS c FROM shop_trade_partners WHERE user_id = ? AND direction = ?",
        (user_id, direction),
    ) as cursor:
        return (await cursor.fetchone())["c"]


async def get_distinct_sellers_count(user_id: int) -> int:
    """Сколько РАЗНЫХ продавцов, у которых user_id хоть раз покупал на
    рынке (для ачивки "Постоянный клиент")."""
    return await get_distinct_trade_partners_count(user_id, "bought")


async def get_distinct_buyers_count(user_id: int) -> int:
    """Сколько РАЗНЫХ покупателей, которым user_id хоть раз продавал на
    рынке (для ачивки "Своя клиентура")."""
    return await get_distinct_trade_partners_count(user_id, "sold")


async def _mark_item_type_bought(user_id: int, item_type: str) -> None:
    """Взводит флаг bought_crop/bought_bakery в shop_stats (для ачивки
    "Разносторонний торговец") — один раз выставленный флаг не
    сбрасывается, ON CONFLICT просто перезаписывает его тем же 1."""
    column = "bought_crop" if item_type == ITEM_CROP else "bought_bakery"
    db = await database.get_db()
    await db.execute(
        f"""
        INSERT INTO shop_stats (user_id, {column}) VALUES (?, 1)
        ON CONFLICT (user_id) DO UPDATE SET {column} = 1
        """,
        (user_id,),
    )
    await database.commit()


# --- пороги счётных рыночных ачивок (achives.py, категория "market") ---

_MARKET_BIG_DEAL_THRESHOLD = 2000


async def _market_sold_threshold_achievements(stats: dict) -> list[str]:
    """Пороговые ачивки продавца, общие для обоих способов продажи на
    рынке — и лотов, купленных другим игроком (buy_listing), и
    мгновенной продажи боту (instant_sell): "Удачная сделка"
    (total_sold) и "Рыночная империя" (total_earned) считают оба пути
    вместе, т.к. оба насквозь идут через один и тот же _add_stats."""
    achv_ids = []
    if stats["total_sold"] >= 100:
        achv_ids.append("market_sold_100")
    if stats["total_earned"] >= 20_000:
        achv_ids.append("market_earned_20000")
    return achv_ids


async def _market_instant_sell_achievements(user_id: int) -> list[str]:
    stats = await get_stats(user_id)
    return await _market_sold_threshold_achievements(stats)


async def _market_buy_achievements(
    buyer_id: int, seller_id: int, total: int, buyer_balance_before: int
) -> tuple[list[str], list[str]]:
    """Какие рыночные ачивки только что выполнены этой покупкой лота —
    отдельно для покупателя и для продавца. Вызывать ПОСЛЕ того, как
    _add_stats/_mark_trade_partner/_mark_item_type_bought для этой же
    сделки уже отработали (см. buy_listing) — здесь только читаем свежие
    значения счётчиков и сверяем с порогами. achives.unlock() идемпотентна,
    так что вернуть уже открытую ачивку безопасно — вызывающий код просто
    получит None для неё и ничего не пошлёт."""
    buyer_stats = await get_stats(buyer_id)
    seller_stats = await get_stats(seller_id)

    buyer_achv_ids = ["market_first_purchase"]
    if buyer_stats["total_bought"] >= 50:
        buyer_achv_ids.append("market_buy_50")
    if buyer_stats["total_bought"] >= 250:
        buyer_achv_ids.append("market_buy_250")
    if buyer_stats["total_spent"] >= 10_000:
        buyer_achv_ids.append("market_spent_10000")
    if buyer_stats["bought_crop"] and buyer_stats["bought_bakery"]:
        buyer_achv_ids.append("market_diverse_trader")
    if await get_distinct_sellers_count(buyer_id) >= 5:
        buyer_achv_ids.append("market_loyal_customer")
    if buyer_balance_before == total:
        buyer_achv_ids.append("market_spend_it_all")

    seller_achv_ids = await _market_sold_threshold_achievements(seller_stats)
    if await get_distinct_buyers_count(seller_id) >= 5:
        seller_achv_ids.append("market_own_clientele")

    if total >= _MARKET_BIG_DEAL_THRESHOLD:
        buyer_achv_ids.append("market_big_deal")
        seller_achv_ids.append("market_big_deal")

    return buyer_achv_ids, seller_achv_ids


# --- лоты ---

async def get_active_listing_count(seller_id: int) -> int:
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS c FROM shop_listings WHERE seller_id = ?", (seller_id,)
    ) as cursor:
        return (await cursor.fetchone())["c"]


async def create_listing(seller_id: int, item_type: str, item_id: str, count: int, price: int) -> int | None | str:
    """Списывает count товара (фруктов из корзины либо изделий с
    витрины пекарни — смотря по item_type) у продавца и создаёт лот.
    Возвращает id лота; None, если товара не хватило; "limit",
    если у продавца уже максимум активных лотов (MAX_LISTINGS_PER_PLAYER).

    Лок на seller_id: без него два лота, выставленные почти
    одновременно, могли бы оба пройти проверку "лимит лотов ещё не
    достигнут" и в сумме превысить MAX_LISTINGS_PER_PLAYER."""
    async with database.user_lock(seller_id):
        active = await get_active_listing_count(seller_id)
        if active >= MAX_LISTINGS_PER_PLAYER:
            return "limit"

        taken = await _take_from_inventory(seller_id, item_type, item_id, count)
        if not taken:
            return None

        db = await database.get_db()
        cursor = await db.execute(
            "INSERT INTO shop_listings (seller_id, item_type, crop_id, count, price, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (seller_id, item_type, item_id, count, price, time.time()),
        )
        # Экономическая операция — сохраняем на диск сразу, не дожидаясь
        # плановой "стопки": если бот упадёт сразу после этого момента,
        # товар не должен потеряться, "зависнув" нигде.
        await database.flush()
        return cursor.lastrowid


async def get_active_listings(filter_token: str, page: int) -> tuple[list[aiosqlite.Row], int]:
    db = await database.get_db()
    item_type, item_id = _decode_filter(filter_token)
    if item_type is None:
        where, params = "", ()
    elif item_id is None:
        where, params = "WHERE item_type = ?", (item_type,)
    else:
        where, params = "WHERE item_type = ? AND crop_id = ?", (item_type, item_id)

    async with db.execute(f"SELECT COUNT(*) AS c FROM shop_listings {where}", params) as cursor:
        total = (await cursor.fetchone())["c"]

    offset = page * PAGE_SIZE
    async with db.execute(
        f"SELECT * FROM shop_listings {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + (PAGE_SIZE, offset),
    ) as cursor:
        rows = await cursor.fetchall()

    return rows, total


async def get_my_listings(user_id: int, page: int) -> tuple[list[aiosqlite.Row], int]:
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(*) AS c FROM shop_listings WHERE seller_id = ?", (user_id,)
    ) as cursor:
        total = (await cursor.fetchone())["c"]

    offset = page * PAGE_SIZE
    async with db.execute(
        "SELECT * FROM shop_listings WHERE seller_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (user_id, PAGE_SIZE, offset),
    ) as cursor:
        rows = await cursor.fetchall()

    return rows, total


async def get_listing(listing_id: int) -> aiosqlite.Row | None:
    db = await database.get_db()
    async with db.execute(
        "SELECT * FROM shop_listings WHERE id = ?", (listing_id,)
    ) as cursor:
        return await cursor.fetchone()


async def buy_listing(listing_id: int, buyer_id: int):
    """Покупает лот целиком. Возвращает:
    - dict с полями лота (уже удалённого из таблицы) при успехе, плюс
      "achv_ids_buyer"/"achv_ids_seller" — списки id рыночных ачивок
      (achives.py), только что выполненных этой сделкой для покупателя
      и продавца соответственно (пороги уже проверены внутри, вызывающему
      коду остаётся только achives.unlock() по каждому id — сама unlock()
      идемпотентна, так что лишний id в списке не страшен),
    - "own", если это лот самого покупателя,
    - "insufficient", если не хватает Pn,
    - None, если лота уже нет (продан/снят).

    Лок на buyer_id — самое важное место во всём магазине: без него
    один и тот же покупатель мог бы почти одновременно купить два
    разных лота, оба раза пройдя проверку баланса по одному и тому же
    "старому" значению (например, баланс 100, два лота по 100 —
    обе проверки видят balance=100 >= 100 и пропускают обе покупки,
    хотя денег хватало только на одну). С локом такие запросы одного
    покупателя всегда обрабатываются строго по очереди, и вторая
    проверка баланса уже видит списание от первой покупки.

    Атомарный DELETE ... WHERE id = ? с проверкой rowcount отдельно
    защищает от того, что тот же лот купят два РАЗНЫХ покупателя
    одновременно — тут лок бы не помог (лочим-то покупателя, а не
    продавца/лот), поэтому нужен именно атомарный DELETE."""
    async with database.user_lock(buyer_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT * FROM shop_listings WHERE id = ?", (listing_id,)
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None
        if row["seller_id"] == buyer_id:
            return "own"

        total = row["count"] * row["price"]
        balance = await get_balance(buyer_id)
        if balance < total:
            return "insufficient"

        deleted = await db.execute("DELETE FROM shop_listings WHERE id = ?", (listing_id,))
        if deleted.rowcount == 0:
            # лот успели купить/снять параллельно (другим покупателем)
            await database.commit()
            return None

        await _change_balance(buyer_id, -total)
        await _change_balance(row["seller_id"], total)
        await _add_stats(buyer_id, bought=row["count"], spent=total)
        await _add_stats(row["seller_id"], sold=row["count"], earned=total)
        await _add_to_inventory(buyer_id, row["item_type"], row["crop_id"], row["count"])

        # --- рыночные ачивки (achives.py, категория "market") ---
        # Запись счётчиков ПЕРЕД проверкой порогов — _market_buy_achievements
        # читает их же значения сразу после обновления (в пределах одного
        # соединения незакоммиченные изменения видны следующим SELECT'ом,
        # см. database.py). balance — это баланс ДО списания (см. выше),
        # он нужен именно такой для ачивки "Всё до копейки".
        await _mark_trade_partner(buyer_id, row["seller_id"], "bought")
        await _mark_trade_partner(row["seller_id"], buyer_id, "sold")
        await _mark_item_type_bought(buyer_id, row["item_type"])
        buyer_achv_ids, seller_achv_ids = await _market_buy_achievements(
            buyer_id, row["seller_id"], total, balance
        )

        # Деньги и товар одновременно сменили владельца — экономическая
        # операция, сохраняем на диск немедленно, а не пачкой позже.
        await database.flush()

        result = dict(row)
        result["achv_ids_buyer"] = buyer_achv_ids
        result["achv_ids_seller"] = seller_achv_ids
        return result


async def cancel_listing(listing_id: int, user_id: int) -> aiosqlite.Row | None:
    """Снимает свой лот с продажи, возвращает фрукты в корзину."""
    async with database.user_lock(user_id):
        db = await database.get_db()
        async with db.execute(
            "SELECT * FROM shop_listings WHERE id = ? AND seller_id = ?",
            (listing_id, user_id),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        deleted = await db.execute(
            "DELETE FROM shop_listings WHERE id = ? AND seller_id = ?", (listing_id, user_id)
        )
        if deleted.rowcount == 0:
            await database.commit()
            return None

        await _add_to_inventory(user_id, row["item_type"], row["crop_id"], row["count"])
        await database.flush()
        return row


async def instant_sell(user_id: int, item_type: str, item_id: str, count: int) -> int | None:
    """Мгновенно продаёт count штук товара боту. Возвращает начисленную
    сумму в Pn, либо None, если товара не хватило."""
    async with database.user_lock(user_id):
        taken = await _take_from_inventory(user_id, item_type, item_id, count)
        if not taken:
            return None

        total = await instant_sell_unit_price(item_type, item_id) * count
        await _change_balance(user_id, total)
        await _add_stats(user_id, sold=count, earned=total)
        await database.flush()
        return total


async def _notify_market_sell_achv(sender, user_id: int, item_type: str, count: int, total: int, lang: str) -> None:
    """Хвост мгновенной продажи боту (instant_sell), общий для обоих
    хендлеров (сообщение/callback), чтобы не дублировать один и тот же
    код: сначала пороговые рыночные ачивки продавца ("Удачная сделка"/
    "Рыночная империя", achives.py — их считает total_sold/total_earned,
    общий для лотов и мгновенной продажи, см. _market_sold_threshold_achievements),
    затем — если продана именно выпечка — ачивки пекарни ("Кондитерская
    лавка"/"Сладкий бизнес", см. bakery.bump_market_sold)."""
    import achives

    achv_ids = list(await _market_instant_sell_achievements(user_id))
    if item_type == ITEM_BAKERY:
        achv_ids += await bakery.bump_market_sold(user_id, count, total)

    for achv_id in achv_ids:
        achv_result = await achives.unlock(user_id, achv_id)
        if achv_result:
            await sender(achives.format_unlock_text(lang, achv_result))


# ==========================
#   ВСПОМОГАТЕЛЬНОЕ
# ==========================

def _seller_label(lang: str, seller_id: int) -> str:
    return TEXTS[lang]["seller_label"].format(id=str(seller_id)[-4:])


def _quantity_options(available: int) -> list[int]:
    return sorted({q for q in QUICK_QUANTITIES if q <= available} | {available})


async def _get_lang(state: FSMContext) -> str:
    data = await state.get_data()
    return data.get("lang", "ru")


def _build_cancel_keyboard(lang: str, owner_id: int) -> object:
    import main

    """Клавиатура с одной кнопкой "Отмена" — прикрепляется к сообщениям,
    которые ждут текстовый ввод от игрока (цена/количество своими
    руками, см. ShopStates.waiting_price/waiting_quantity). Даёт выход
    из ожидания без необходимости прислать хоть что-то текстом — см.
    on_cancel_input."""
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["input_cancel_button"], callback_data=main.owner_cb(owner_id, "shop:cancel_input"), style="primary")
    builder.adjust(1)
    return builder.as_markup()


# ==========================
#   ОТРИСОВКА ЭКРАНОВ
# ==========================

async def _build_market_main(lang: str, user_id: int, balance: int) -> tuple[str, object]:
    import main

    owner_id = user_id
    t = TEXTS[lang]
    stats = await get_stats(user_id)
    text = "\n".join([
        t["welcome_title"],
        t["separator"],
        t["balance_line"].format(balance=balance),
        "",
        t["stats_header"],
        t["stats_bought_line"].format(bought=stats["total_bought"], spent=stats["total_spent"]),
        t["stats_sold_line"].format(sold=stats["total_sold"], earned=stats["total_earned"]),
        t["separator"],
        t["main_hint"],
    ])

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["btn_browse"],
        callback_data=main.owner_cb(owner_id, "shop:browse:all:0"),
        style="primary",
        icon_custom_emoji_id=EMOJI_LOOK_ID,
    )
    builder.button(
        text=t["btn_my_listings"],
        callback_data=main.owner_cb(owner_id, "shop:mylistings:0"),
        style="primary",
        icon_custom_emoji_id=EMOJI_MYLISTINGS_ID,
    )
    builder.button(
        text=t["btn_sell"],
        callback_data=main.owner_cb(owner_id, "shop:sell_choose"),
        style="primary",
        icon_custom_emoji_id=EMOJI_SELL_ID,
    )
    builder.button(
        text=t["btn_instant"],
        callback_data=main.owner_cb(owner_id, "shop:instant_choose"),
        style="primary",
        icon_custom_emoji_id=EMOJI_METEOR_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


async def _build_browse(lang: str, filter_token: str, page: int, owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    listings, total = await get_active_listings(filter_token, page)
    pages = max(1, -(-total // PAGE_SIZE))  # округление вверх
    page = min(page, pages - 1) if total else 0

    lines = [t["title"], t["separator"]]
    item_type, item_id = _decode_filter(filter_token)
    if item_type is not None and item_id is not None:
        item = _item_meta(item_type, item_id)
        lines.append(t["filter_active_line"].format(emoji=item["emoji"], name=item["name"][lang]))
    elif item_type is not None:
        cat_emoji, cat_name = _category_label(lang, item_type)
        lines.append(t["filter_active_line"].format(emoji=cat_emoji, name=cat_name))
    if total:
        lines.append(t["page_line"].format(page=page + 1, pages=pages))

    builder = InlineKeyboardBuilder()
    button_count = 0

    if not listings:
        lines.append("")
        lines.append(t["empty_market"])
    else:
        for row in listings:
            item = _item_meta(row["item_type"], row["crop_id"])
            builder.button(
                text=t["listing_button"].format(
                    emoji=item["emoji"], name=item["name"][lang], count=row["count"], price=row["price"]
                ),
                callback_data=main.owner_cb(owner_id, f"shop:view:{row['id']}:{filter_token}:{page}"),
                style="primary",
                icon_custom_emoji_id=CURRENCY_EMOJI_ID,
            )
            button_count += 1

    sizes = [1] * button_count

    has_prev = page > 0
    has_next = page < pages - 1
    if has_prev or has_next:
        if has_prev:
            builder.button(
                text=t["prev_page_button"],
                callback_data=main.owner_cb(owner_id, f"shop:browse:{filter_token}:{page - 1}"),
                style="primary",
                icon_custom_emoji_id=EMOJI_PREV_PAGE_ID,
            )
        if has_next:
            builder.button(
                text=t["next_page_button"],
                callback_data=main.owner_cb(owner_id, f"shop:browse:{filter_token}:{page + 1}"),
                style="primary",
                icon_custom_emoji_id=EMOJI_NEXT_PAGE_ID,
            )
        sizes.append(2 if (has_prev and has_next) else 1)

    builder.button(
        text=t["filter_button"],
        callback_data=main.owner_cb(owner_id, "shop:filter"),
        style="primary",
        icon_custom_emoji_id=EMOJI_SEARCH_ID,
    )
    sizes.append(1)
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:back"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    sizes.append(1)

    builder.adjust(*sizes)
    text = "\n".join(lines).rstrip()
    return text, builder.as_markup()


async def _build_listing_detail(lang: str, row: aiosqlite.Row, filter_token: str, page: int, owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    item_type = row["item_type"]
    item = _item_meta(item_type, row["crop_id"])
    total = row["count"] * row["price"]
    lo, hi = _price_range(item_type, row["crop_id"])
    avg = round(await get_average_market_price(item_type, row["crop_id"]))

    text = "\n".join([
        t["listing_item_line"].format(emoji=item["emoji"], name=item["name"][lang], count=row["count"]),
        t["listing_price_line"].format(price=row["price"], total=total),
        "",
        t["listing_info_header"],
        t["listing_range_line"].format(min=lo, max=hi),
        t["listing_avg_line"].format(avg=avg),
        "",
        t["listing_seller_line"].format(seller=_seller_label(lang, row["seller_id"])),
    ])

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["buy_button"].format(total=total),
        callback_data=main.owner_cb(owner_id, f"shop:buy:{row['id']}:{filter_token}:{page}"),
        style="primary",
        icon_custom_emoji_id=EMOJI_CHECK_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, f"shop:browse:{filter_token}:{page}"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


def _build_filter_categories(lang: str, owner_id: int) -> tuple[str, object]:
    import main

    """Первый шаг фильтра — выбор категории (фрукты/выпечка), а не сразу
    общий список всех товаров вперемешку. Список конкретных товаров
    внутри категории — см. _build_filter_items."""
    t = TEXTS[lang]
    text = t["filter_title"]

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["filter_all"],
        callback_data=main.owner_cb(owner_id, "shop:browse:all:0"),
        style="primary",
        icon_custom_emoji_id=EMOJI_SEARCH_ID,
    )
    builder.button(
        text=t["cat_fruits_button"],
        callback_data=main.owner_cb(owner_id, f"shop:filtercat:{ITEM_CROP}"),
        style="primary",
    )
    builder.button(
        text=t["cat_bakery_button"],
        callback_data=main.owner_cb(owner_id, f"shop:filtercat:{ITEM_BAKERY}"),
        style="primary",
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:browse:all:0"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


def _build_filter_items(lang: str, item_type: str, owner_id: int) -> tuple[str, object]:
    import main

    """Второй шаг фильтра — конкретный товар внутри уже выбранной
    категории, плюс возможность отфильтровать по всей категории сразу."""
    t = TEXTS[lang]
    cat_emoji, cat_name = _category_label(lang, item_type)
    text = t["filter_category_title"].format(emoji=cat_emoji, name=cat_name)

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["filter_category_all"].format(name=cat_name),
        callback_data=main.owner_cb(owner_id, f"shop:browse:{_encode_filter(item_type, None)}:0"),
        style="primary",
        icon_custom_emoji_id=EMOJI_SEARCH_ID,
    )
    for item_id in _item_order(item_type):
        item = _item_meta(item_type, item_id)
        builder.button(
            text=f"{item['emoji']} {item['name'][lang]}",
            callback_data=main.owner_cb(owner_id, f"shop:browse:{_encode_filter(item_type, item_id)}:0"),
            style="primary",
        )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:filter"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


async def _build_my_listings(lang: str, user_id: int, page: int) -> tuple[str, object]:
    import main

    owner_id = user_id
    t = TEXTS[lang]
    listings, total = await get_my_listings(user_id, page)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(page, pages - 1) if total else 0

    lines = [t["my_listings_title"], t["separator"]]
    if total:
        lines.append(t["page_line"].format(page=page + 1, pages=pages))

    builder = InlineKeyboardBuilder()
    button_count = 0

    if not listings:
        lines.append("")
        lines.append(t["no_my_listings"])
    else:
        for row in listings:
            item = _item_meta(row["item_type"], row["crop_id"])
            builder.button(
                text=t["my_listing_button"].format(
                    emoji=item["emoji"], name=item["name"][lang], count=row["count"], price=row["price"]
                ),
                callback_data=main.owner_cb(owner_id, f"shop:viewmine:{row['id']}:{page}"),
                style="primary",
                icon_custom_emoji_id=CURRENCY_EMOJI_ID,
            )
            button_count += 1

    sizes = [1] * button_count

    has_prev = page > 0
    has_next = page < pages - 1
    if has_prev or has_next:
        if has_prev:
            builder.button(
                text=t["prev_page_button"],
                callback_data=main.owner_cb(owner_id, f"shop:mylistings:{page - 1}"),
                style="primary",
                icon_custom_emoji_id=EMOJI_PREV_PAGE_ID,
            )
        if has_next:
            builder.button(
                text=t["next_page_button"],
                callback_data=main.owner_cb(owner_id, f"shop:mylistings:{page + 1}"),
                style="primary",
                icon_custom_emoji_id=EMOJI_NEXT_PAGE_ID,
            )
        sizes.append(2 if (has_prev and has_next) else 1)

    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:back"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    sizes.append(1)

    builder.adjust(*sizes)
    text = "\n".join(lines).rstrip()
    return text, builder.as_markup()


def _build_my_listing_detail(lang: str, row: aiosqlite.Row, page: int, owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    item = _item_meta(row["item_type"], row["crop_id"])
    total = row["count"] * row["price"]

    text = "\n".join([
        t["my_listings_title"],
        t["separator"],
        t["my_listing_line"].format(
            emoji=item["emoji"], name=item["name"][lang], count=row["count"], price=row["price"], total=total
        ),
    ])

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["cancel_button"],
        callback_data=main.owner_cb(owner_id, f"shop:cancel:{row['id']}:{page}"),
        style="primary",
        icon_custom_emoji_id=EMOJI_CANCEL_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, f"shop:mylistings:{page}"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


def _build_category_choice(lang: str, mode: str, owner_id: int) -> tuple[str, object]:
    import main

    """Первый шаг продажи/мгновенной продажи — выбор категории товара
    (фрукты сада / выпечка пекарни), только после него — список
    конкретных товаров этой категории (см. _build_item_choice)."""
    t = TEXTS[lang]
    text = t["sell_category_title"] if mode == "sell" else t["instant_category_title"]
    prefix = "shop:sell_cat" if mode == "sell" else "shop:instant_cat"

    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["cat_fruits_button"],
        callback_data=main.owner_cb(owner_id, f"{prefix}:{ITEM_CROP}"),
        style="primary",
    )
    builder.button(
        text=t["cat_bakery_button"],
        callback_data=main.owner_cb(owner_id, f"{prefix}:{ITEM_BAKERY}"),
        style="primary",
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:back"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


def _build_sell_choice(lang: str, item_type: str, inventory: dict[str, int], owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    text = t["sell_choose_title"] if item_type == ITEM_CROP else t["sell_choose_title_bakery"]

    builder = InlineKeyboardBuilder()
    for item_id in _item_order(item_type):
        count = inventory.get(item_id, 0)
        if count <= 0:
            continue
        item = _item_meta(item_type, item_id)
        builder.button(
            text=t["sell_item_button"].format(emoji=item["emoji"], name=item["name"][lang], count=count),
            callback_data=main.owner_cb(owner_id, f"shop:sell_item:{item_type}:{item_id}"),
            style="primary",
        )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:sell_choose"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


async def _build_instant_choice(lang: str, item_type: str, inventory: dict[str, int], owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    text = t["instant_choose_title"] if item_type == ITEM_CROP else t["instant_choose_title_bakery"]

    builder = InlineKeyboardBuilder()
    for item_id in _item_order(item_type):
        count = inventory.get(item_id, 0)
        if count <= 0:
            continue
        item = _item_meta(item_type, item_id)
        builder.button(
            text=t["instant_item_button"].format(
                emoji=item["emoji"],
                name=item["name"][lang],
                count=count,
                price=await instant_sell_unit_price(item_type, item_id),
            ),
            callback_data=main.owner_cb(owner_id, f"shop:instant_item:{item_type}:{item_id}"),
            style="primary",
            icon_custom_emoji_id=CURRENCY_EMOJI_ID,
        )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "shop:instant_choose"),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


async def _build_qty_choice(lang: str, item_type: str, item_id: str, available: int, mode: str, owner_id: int) -> tuple[str, object]:
    import main

    t = TEXTS[lang]
    item = _item_meta(item_type, item_id)

    if mode == "sell":
        text = t["sell_qty_title"].format(emoji=item["emoji"], name=item["name"][lang], available=available)
        prefix = "shop:sellqty"
        back_cb = f"shop:sell_cat:{item_type}"
    else:
        text = t["instant_qty_title"].format(
            emoji=item["emoji"],
            name=item["name"][lang],
            available=available,
            price=await instant_sell_unit_price(item_type, item_id),
        )
        prefix = "shop:instantqty"
        back_cb = f"shop:instant_cat:{item_type}"

    builder = InlineKeyboardBuilder()
    for qty in _quantity_options(available):
        label = t["qty_all_button"].format(available=available) if qty == available else t["qty_button"].format(qty=qty)
        builder.button(text=label, callback_data=main.owner_cb(owner_id, f"{prefix}:{item_type}:{item_id}:{qty}"), style="primary")
    builder.button(
        text=t["qty_custom_button"],
        callback_data=main.owner_cb(owner_id, f"shop:customqty:{mode}:{item_type}:{item_id}"),
        style="primary",
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, back_cb),
        style="primary",
        icon_custom_emoji_id=EMOJI_BACK_ID,
    )
    builder.adjust(1)
    return text, builder.as_markup()


# ==========================
#   ХЕНДЛЕРЫ
# ==========================

@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_market(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state)
    balance = await get_balance(message.from_user.id)
    text, markup = await _build_market_main(lang, message.from_user.id, balance)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == "shop:back")
async def on_back_to_main(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    balance = await get_balance(callback.from_user.id)
    text, markup = await _build_market_main(lang, callback.from_user.id, balance)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


# --- просмотр лотов ---

@router.callback_query(F.data.startswith("shop:browse:"))
async def on_browse(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    _, _, filter_id, page_str = callback.data.split(":")
    text, markup = await _build_browse(lang, filter_id, int(page_str), callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:view:"))
async def on_view_listing(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, listing_id_str, filter_id, page_str = callback.data.split(":")
    listing_id = int(listing_id_str)
    page = int(page_str)

    row = await get_listing(listing_id)
    if row is None:
        await callback.answer(t["listing_gone_toast"], show_alert=True)
        text, markup = await _build_browse(lang, filter_id, page, callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=markup)
        return

    text, markup = await _build_listing_detail(lang, row, filter_id, page, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "shop:filter")
async def on_filter_menu(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    text, markup = _build_filter_categories(lang, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:filtercat:"))
async def on_filter_category_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    item_type = callback.data.split(":")[2]
    text, markup = _build_filter_items(lang, item_type, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:buy:"))
async def on_buy(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, listing_id_str, filter_id, page_str = callback.data.split(":")
    listing_id = int(listing_id_str)
    page = int(page_str)

    result = await buy_listing(listing_id, callback.from_user.id)

    if result is None:
        await callback.answer(t["listing_gone_toast"], show_alert=True)
    elif result == "own":
        await callback.answer(t["own_listing_toast"], show_alert=True)
    elif result == "insufficient":
        await callback.answer(t["not_enough_balance_toast"], show_alert=True)
    else:
        item = _item_meta(result["item_type"], result["crop_id"])
        total = result["count"] * result["price"]
        await callback.answer(
            t["bought_toast"].format(emoji=item["emoji"], name=item["name"][lang], count=result["count"], total=total),
            show_alert=True,
        )

        # Рыночные ачивки покупателя (achives.py, категория "market") —
        # уже посчитаны внутри buy_listing (result["achv_ids_buyer"]),
        # тут только выдаём и шлём уведомления отдельными сообщениями.
        import achives

        for achv_id in result["achv_ids_buyer"]:
            achv_result = await achives.unlock(callback.from_user.id, achv_id)
            if achv_result:
                await callback.message.answer(achives.format_unlock_text(lang, achv_result))

        # уведомляем продавца, если получится
        try:
            seller_lang = lang
            seller_t = TEXTS[seller_lang]
            await callback.bot.send_message(
                result["seller_id"],
                seller_t["sold_notice_toast"].format(
                    emoji=item["emoji"], name=item["name"][seller_lang], count=result["count"], total=total
                ),
            )
            # Рыночные ачивки продавца — та же идея, что и для покупателя
            # выше, только уже посчитаны как result["achv_ids_seller"].
            for achv_id in result["achv_ids_seller"]:
                achv_result = await achives.unlock(result["seller_id"], achv_id)
                if achv_result:
                    await callback.bot.send_message(
                        result["seller_id"], achives.format_unlock_text(seller_lang, achv_result)
                    )
            # Ачивки пекарни за продажу выпечки на рынке ("Кондитерская
            # лавка"/"Сладкий бизнес") — счётчики в bakery.py, тут только
            # выдаём и уведомляем продавца тем же сообщением-хвостом.
            if result["item_type"] == ITEM_BAKERY:
                achv_ids = await bakery.bump_market_sold(result["seller_id"], result["count"], total)
                for achv_id in achv_ids:
                    achv_result = await achives.unlock(result["seller_id"], achv_id)
                    if achv_result:
                        await callback.bot.send_message(
                            result["seller_id"], achives.format_unlock_text(seller_lang, achv_result)
                        )
        except Exception:
            pass

    text, markup = await _build_browse(lang, filter_id, page, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)


# --- мои лоты ---

@router.callback_query(F.data.startswith("shop:mylistings:"))
async def on_my_listings(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    page = int(callback.data.split(":")[2])
    text, markup = await _build_my_listings(lang, callback.from_user.id, page)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:viewmine:"))
async def on_view_my_listing(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, listing_id_str, page_str = callback.data.split(":")
    listing_id = int(listing_id_str)
    page = int(page_str)

    row = await get_listing(listing_id)
    if row is None or row["seller_id"] != callback.from_user.id:
        await callback.answer(t["listing_gone_toast"], show_alert=True)
        text, markup = await _build_my_listings(lang, callback.from_user.id, page)
        await callback.message.edit_text(text, reply_markup=markup)
        return

    text, markup = _build_my_listing_detail(lang, row, page, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:cancel:"))
async def on_cancel_listing(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, listing_id_str, page_str = callback.data.split(":")
    listing_id = int(listing_id_str)
    page = int(page_str)

    row = await cancel_listing(listing_id, callback.from_user.id)
    if row is None:
        await callback.answer(t["listing_gone_toast"], show_alert=True)
    else:
        await callback.answer(t["cancelled_toast"])

        # Ачивка "Передумал" — за сам факт снятия лота с продажи, вне
        # зависимости от того, какой именно товар это был.
        import achives

        achv_result = await achives.unlock(callback.from_user.id, "market_cancel_listing")
        if achv_result:
            await callback.message.answer(achives.format_unlock_text(lang, achv_result))

    text, markup = await _build_my_listings(lang, callback.from_user.id, page)
    await callback.message.edit_text(text, reply_markup=markup)


# --- выставить на продажу ---

@router.callback_query(F.data == "shop:sell_choose")
async def on_sell_choose(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    active = await get_active_listing_count(callback.from_user.id)
    if active >= MAX_LISTINGS_PER_PLAYER:
        await callback.answer(t["listing_limit_toast"], show_alert=True)
        return

    fruit_inv = await garden.get_inventory(callback.from_user.id)
    pantry_inv = await bakery.get_pantry(callback.from_user.id)
    if not any(count > 0 for count in fruit_inv.values()) and not any(count > 0 for count in pantry_inv.values()):
        await callback.answer(t["sell_empty_basket"], show_alert=True)
        return

    text, markup = _build_category_choice(lang, mode="sell", owner_id=callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:sell_cat:"))
async def on_sell_category_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]
    item_type = callback.data.split(":")[2]

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    if not any(count > 0 for count in inventory.values()):
        await callback.answer(t["category_empty_toast"], show_alert=True)
        return

    text, markup = _build_sell_choice(lang, item_type, inventory, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:sell_item:"))
async def on_sell_item_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    _, _, item_type, item_id = callback.data.split(":")

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    available = inventory.get(item_id, 0)
    if available <= 0:
        text, markup = _build_sell_choice(lang, item_type, inventory, callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer(TEXTS[lang]["sell_failed_toast"])
        return

    text, markup = await _build_qty_choice(lang, item_type, item_id, available, mode="sell", owner_id=callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:sellqty:"))
async def on_sell_qty_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, item_type, item_id, qty_str = callback.data.split(":")
    qty = int(qty_str)
    item = _item_meta(item_type, item_id)

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    if inventory.get(item_id, 0) < qty:
        await callback.answer(t["sell_failed_toast"], show_alert=True)
        text, markup = _build_sell_choice(lang, item_type, inventory, callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=markup)
        return

    await state.update_data(shop_sell_type=item_type, shop_sell_item=item_id, shop_sell_qty=qty)
    await state.set_state(ShopStates.waiting_price)

    lo, hi = _price_range(item_type, item_id)
    await callback.answer()
    await callback.message.answer(
        t["ask_price"].format(emoji=item["emoji"], name=item["name"][lang], count=qty, min_price=lo, max_price=hi),
        reply_markup=_build_cancel_keyboard(lang, callback.from_user.id),
    )


@router.message(StateFilter(ShopStates.waiting_price))
async def on_price_received(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    data = await state.get_data()
    item_type = data.get("shop_sell_type")
    item_id = data.get("shop_sell_item")
    qty = data.get("shop_sell_qty")

    if item_type is None or item_id is None or qty is None:
        await state.set_state(None)
        return

    lo, hi = _price_range(item_type, item_id)
    raw = (message.text or "").strip()
    if not raw.isdigit() or not (lo <= int(raw) <= hi):
        # Ждём ввод только 1 раз — если игрок прислал не то (или вообще
        # что угодно, не число в допустимом диапазоне), НЕ продолжаем
        # бесконечно ждать следующую попытку: сбрасываем состояние и
        # предлагаем начать заново с экрана рынка.
        await state.set_state(None)
        await message.answer(t["price_invalid"].format(min_price=lo, max_price=hi))
        balance = await get_balance(message.from_user.id)
        text, markup = await _build_market_main(lang, message.from_user.id, balance)
        await message.answer(text, reply_markup=markup)
        return

    price = int(raw)
    await state.set_state(None)

    listing_id = await create_listing(message.from_user.id, item_type, item_id, qty, price)
    if listing_id == "limit":
        await message.answer(t["listing_limit_toast"])
        return
    if listing_id is None:
        await message.answer(t["sell_failed_toast"])
        return

    item = _item_meta(item_type, item_id)
    await message.answer(
        t["listed_success"].format(
            emoji=item["emoji"], name=item["name"][lang], count=qty, price=price, total=qty * price
        )
    )
    balance = await get_balance(message.from_user.id)
    text, markup = await _build_market_main(lang, message.from_user.id, balance)
    await message.answer(text, reply_markup=markup)

    # Ачивки рынка за сам факт создания лота. Импорт локальный, чтобы не
    # завести цикл: shop → achives → prof → shop.
    import achives

    listing_achv_ids = ["first_listing"]
    # "Дорогая штучка" / "Распродажа" — цена лота ровно на границе
    # допустимого диапазона для этого товара.
    if price == hi:
        listing_achv_ids.append("market_max_price")
    if price == lo:
        listing_achv_ids.append("market_min_price")
    # "Все полки заняты" — после создания этого лота у игрока ровно
    # MAX_LISTINGS_PER_PLAYER активных лотов одновременно.
    active = await get_active_listing_count(message.from_user.id)
    if active >= MAX_LISTINGS_PER_PLAYER:
        listing_achv_ids.append("market_full_shelves")

    for achv_id in listing_achv_ids:
        achv_result = await achives.unlock(message.from_user.id, achv_id)
        if achv_result:
            await message.answer(achives.format_unlock_text(lang, achv_result))


# --- отмена ввода (общая для цены и количества) ---

@router.callback_query(F.data == "shop:cancel_input")
async def on_cancel_input(callback: CallbackQuery, state: FSMContext) -> None:
    """Кнопка "Отмена" на сообщениях, ждущих текстовый ввод (своя цена/
    своё количество, см. ShopStates.waiting_price/waiting_quantity).
    Сбрасывает состояние без необходимости присылать что-либо текстом —
    раньше единственным выходом было прислать корректное число, а любой
    неверный ввод заставлял бота ждать снова и снова, без конца."""
    lang = await _get_lang(state)
    t = TEXTS[lang]

    await state.set_state(None)
    await callback.answer(t["input_cancelled_toast"])

    balance = await get_balance(callback.from_user.id)
    text, markup = await _build_market_main(lang, callback.from_user.id, balance)
    await callback.message.edit_text(text, reply_markup=markup)


# --- своё количество (общее для продажи и мгновенного выкупа) ---

@router.callback_query(F.data.startswith("shop:customqty:"))
async def on_custom_qty_request(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, mode, item_type, item_id = callback.data.split(":")
    item = _item_meta(item_type, item_id)

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    available = inventory.get(item_id, 0)
    if available <= 0:
        await callback.answer(t["sell_failed_toast"], show_alert=True)
        if mode == "sell":
            text, markup = _build_sell_choice(lang, item_type, inventory, callback.from_user.id)
        else:
            text, markup = await _build_instant_choice(lang, item_type, inventory, callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=markup)
        return

    await state.update_data(
        shop_qty_type=item_type, shop_qty_item=item_id, shop_qty_mode=mode, shop_qty_available=available
    )
    await state.set_state(ShopStates.waiting_quantity)

    await callback.answer()
    await callback.message.answer(
        t["ask_quantity"].format(emoji=item["emoji"], name=item["name"][lang], available=available),
        reply_markup=_build_cancel_keyboard(lang, callback.from_user.id),
    )


@router.message(StateFilter(ShopStates.waiting_quantity))
async def on_quantity_received(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    data = await state.get_data()
    item_type = data.get("shop_qty_type")
    item_id = data.get("shop_qty_item")
    mode = data.get("shop_qty_mode")

    if item_type is None or item_id is None or mode is None:
        await state.set_state(None)
        return

    # перепроверяем актуальный остаток на случай, если корзина/витрина
    # изменилась, пока игрок печатал число
    inventory = await _get_type_inventory(message.from_user.id, item_type)
    available = inventory.get(item_id, 0)

    raw = (message.text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= available):
        # Как и с ценой — ждём ввод только 1 раз, без бесконечного цикла
        # повторных попыток: сбрасываем состояние и отправляем обратно
        # на экран рынка.
        await state.set_state(None)
        await message.answer(t["quantity_invalid"].format(available=available))
        balance = await get_balance(message.from_user.id)
        text, markup = await _build_market_main(lang, message.from_user.id, balance)
        await message.answer(text, reply_markup=markup)
        return

    qty = int(raw)
    await state.set_state(None)
    item = _item_meta(item_type, item_id)

    if mode == "sell":
        await state.update_data(shop_sell_type=item_type, shop_sell_item=item_id, shop_sell_qty=qty)
        await state.set_state(ShopStates.waiting_price)
        lo, hi = _price_range(item_type, item_id)
        await message.answer(
            t["ask_price"].format(emoji=item["emoji"], name=item["name"][lang], count=qty, min_price=lo, max_price=hi),
            reply_markup=_build_cancel_keyboard(lang, message.from_user.id),
        )
        return

    # mode == "instant"
    total = await instant_sell(message.from_user.id, item_type, item_id, qty)
    if total is None:
        await message.answer(t["sell_failed_toast"])
        return

    await message.answer(
        t["instant_sold_toast"].format(emoji=item["emoji"], name=item["name"][lang], count=qty, total=total)
    )
    balance = await get_balance(message.from_user.id)
    text, markup = await _build_market_main(lang, message.from_user.id, balance)
    await message.answer(text, reply_markup=markup)
    await _notify_market_sell_achv(message.answer, message.from_user.id, item_type, qty, total, lang)


# --- мгновенная продажа боту ---

@router.callback_query(F.data == "shop:instant_choose")
async def on_instant_choose(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    fruit_inv = await garden.get_inventory(callback.from_user.id)
    pantry_inv = await bakery.get_pantry(callback.from_user.id)
    if not any(count > 0 for count in fruit_inv.values()) and not any(count > 0 for count in pantry_inv.values()):
        await callback.answer(t["instant_empty_basket"], show_alert=True)
        return

    text, markup = _build_category_choice(lang, mode="instant", owner_id=callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:instant_cat:"))
async def on_instant_category_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]
    item_type = callback.data.split(":")[2]

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    if not any(count > 0 for count in inventory.values()):
        toast = t["instant_empty_pantry"] if item_type == ITEM_BAKERY else t["instant_empty_basket"]
        await callback.answer(toast, show_alert=True)
        return

    text, markup = await _build_instant_choice(lang, item_type, inventory, callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:instant_item:"))
async def on_instant_item_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    _, _, item_type, item_id = callback.data.split(":")

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    available = inventory.get(item_id, 0)
    if available <= 0:
        text, markup = await _build_instant_choice(lang, item_type, inventory, callback.from_user.id)
        await callback.message.edit_text(text, reply_markup=markup)
        await callback.answer(TEXTS[lang]["sell_failed_toast"])
        return

    text, markup = await _build_qty_choice(lang, item_type, item_id, available, mode="instant", owner_id=callback.from_user.id)
    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("shop:instantqty:"))
async def on_instant_qty_chosen(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state)
    t = TEXTS[lang]

    _, _, item_type, item_id, qty_str = callback.data.split(":")
    qty = int(qty_str)
    item = _item_meta(item_type, item_id)

    total = await instant_sell(callback.from_user.id, item_type, item_id, qty)
    if total is None:
        await callback.answer(t["sell_failed_toast"], show_alert=True)
    else:
        await callback.answer(
            t["instant_sold_toast"].format(emoji=item["emoji"], name=item["name"][lang], count=qty, total=total),
            show_alert=True,
        )

    inventory = await _get_type_inventory(callback.from_user.id, item_type)
    if any(count > 0 for count in inventory.values()):
        text, markup = await _build_instant_choice(lang, item_type, inventory, callback.from_user.id)
    else:
        balance = await get_balance(callback.from_user.id)
        text, markup = await _build_market_main(lang, callback.from_user.id, balance)
    await callback.message.edit_text(text, reply_markup=markup)

    if total is not None:
        await _notify_market_sell_achv(callback.message.answer, callback.from_user.id, item_type, qty, total, lang)
