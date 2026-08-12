"""
Раздел "Достижения".

Идея:
    Плоский список ачивок (см. ACHIEVEMENTS) с наградой в опыте/
    кристаллах/монетах. Факт открытия хранится в таблице
    user_achievements (см. database.py) — по одной строке на пару
    (user_id, achv_id), открытие необратимо.

    Ачивки сгруппированы по категориям (см. CATEGORIES) — панда,
    огород, пекарня, рынок, общее. Категория — метка "category" в
    описании ачивки, определяет и подпись на карточке, и то, какие
    ачивки попадают в пейджер конкретного раздела (см. "Меню" ниже).

Выдача (unlock):
    Единственная точка входа для ДРУГИХ модулей — unlock(user_id,
    achv_id). Дергается ими самими в момент выполнения условия
    (первое кормление панды, первый урожай, нужный уровень и т.п.) —
    здесь эти условия не отслеживаются, achives.py только хранит
    список ачивок и умеет их выдавать.

    Идемпотентна: если ачивка уже открыта, INSERT OR IGNORE ничего не
    вставит (rowcount == 0) и unlock() тихо вернёт None — безопасно
    звать из нескольких мест на один и тот же триггер (например, и из
    "тихого" автосбора, и из фонового уведомления о созревшем фрукте).
    Атомарность обеспечивает сам SQLite (PRIMARY KEY на паре полей) —
    так же, как _collect_plot_if_matches в garden.py, отдельный
    database.user_lock() тут не нужен.

Уведомление игрока:
    unlock() только начисляет награду и возвращает словарь с
    результатом (или None) — САМ текст "🏆 Открыто новое достижение —
    «...»!" (жирный заголовок + курсивом название ачивки, курсивом же
    строка награды, см. format_unlock_text/_UNLOCK_HEADER) достраивает
    и шлёт вызывающий код ВСЕГДА ОТДЕЛЬНЫМ сообщением (свой
    .answer()/.send_message()), а не приклеивает к тексту другого
    уведомления (сбор урожая, готовая выпечка, левелап и т.п.) — так
    игрок видит открытие ачивки как отдельное, заметное событие, даже
    если оно произошло попутно с чем-то ещё в тот же момент.

Меню:
    /achievements — двухшаговое:
      1) список категорий (ach:cats) — заголовок со общим счётчиком
         (сколько раз всего были открыты ачивки, все игроки, все
         ачивки) и кнопка на каждый раздел со своим счётчиком
         "открыто/всего" внутри раздела;
      2) после выбора раздела (ach:cat:<cat_id>) — сразу карточка
         первой ачивки ЭТОГО раздела, дальше пейджер по ачивкам
         только внутри него: [◀️] [индикатор страницы, напр. "2/3" —
         именно в рамках раздела] [▶️], индикатор некликабелен
         (ach:noop). Отдельной строкой — "◀️ К разделам"
         (ach:cats), чтобы вернуться к списку категорий.
    На границах раздела (первая/последняя ачивка) стрелка пейджера
    просто остаётся на месте, без зацикливания на соседний раздел.

    Карточка ачивки:
      - статус — бинарный, "✅ Выполнено" (+ дата) или "❌ Не
        выполнено"; шкала (_progress_bar) под статусом рисует то же
        самое в виде "▰▰▰▰▰▰▰▰▰▰ 100%" / "▱▱▱▱▱▱▱▱▱▱ 0%" — achives.py
        не отслеживает промежуточные игровые условия (см. докстринг
        выше), поэтому шкала всегда полностью пустая или полностью
        заполненная, без промежуточных %;
      - "👥 Выполнили: N игроков" — сколько РАЗНЫХ игроков открыли
        именно эту ачивку (get_completions_count).
      Форматирование: всё, кроме текста описания, — <b>жирным</b>;
      описание — <i>курсивом</i>.

Подключение в main.py:
    import achives
    dp.include_router(achives.router)

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import time
from datetime import datetime
from typing import Awaitable, Callable

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import prof
import shop

router = Router(name="achives")


# ==========================
#   ПРОГРЕСС СЧЁТНЫХ АЧИВОК
# ==========================
# achives.py по-прежнему НЕ отслеживает игровые условия сам (см.
# докстринг модуля) — но для счётных ачивок ("Погладить панду 10 раз"
# и т.п.) уже готовое текущее значение обычно лежит в БД того модуля,
# который эту ачивку выдаёт (panda.py/garden.py/...). PROGRESS_PROVIDERS —
# реестр "achv_id -> (цель, async-функция(user_id) -> текущее значение)",
# который эти модули сами заполняют при импорте (см. panda.py, секция
# "АЧИВКИ ПАНДЫ" — там же, где считаются сами счётчики). Карточка
# ачивки (см. _achv_page_text) проверяет реестр и, если для этой
# ачивки есть провайдер, рисует реальный "X/Y" и процент вместо
# бинарного 0%/100%; если провайдера нет — показывает как раньше,
# только по факту "открыта / не открыта".
PROGRESS_PROVIDERS: dict[str, tuple[int, Callable[[int], Awaitable[int]]]] = {}

# Провайдеры для СЧЁТНЫХ ачивок категории "general" — регистрируются прямо
# здесь (а не в prof.py, как для панды/сада описано в докстринге выше),
# т.к. top-level "import achives" в prof.py завёл бы цикл импортов
# (achives.py уже импортирует prof на верхнем уровне, см. add_xp в
# prof.py и комментарии там же). achives.py, наоборот, prof уже
# импортировал — так что PROGRESS_PROVIDERS для "общих" ачивок безопаснее
# заполнить отсюда.
PROGRESS_PROVIDERS.update(
    {
        # Рыночные счётные ачивки (shop.py) — тот же приём, что и для
        # "общих" ачивок ниже: achives.py уже импортирует shop на
        # верхнем уровне (и shop не импортирует achives обратно на
        # верхнем уровне, только локально внутри функций — см.
        # docstring shop.py), так что цикла импортов тут нет.
        "market_buy_50": (50, shop.get_total_bought),
        "market_buy_250": (250, shop.get_total_bought),
        "market_spent_10000": (10_000, shop.get_total_spent),
        "market_sold_100": (100, shop.get_total_sold),
        "market_earned_20000": (20_000, shop.get_total_earned),
        "market_loyal_customer": (5, shop.get_distinct_sellers_count),
        "market_own_clientele": (5, shop.get_distinct_buyers_count),
        "general_xp_10000": (10_000, prof.get_xp),
        "general_gifts_sent_10": (10, prof.get_gifts_sent_count),
        "general_gifts_received_10": (10, prof.get_gifts_received_count),
        "general_all_gifts": (len(prof.GIFTS), prof.get_gift_types_count),
        "general_login_streak_7": (7, prof.get_login_streak),
        "general_login_streak_30": (30, prof.get_login_streak),
        "general_login_streak_100": (100, prof.get_login_streak),
        "general_donate_total_1000": (1_000, prof.get_donate_total),
        "general_coins_earned_10000": (10_000, prof.get_coins_earned),
    }
)


async def _get_progress(achv_id: str, user_id: int) -> tuple[int, int] | None:
    """(текущее, цель) для achv_id, если для него зарегистрирован
    провайдер, иначе None. Текущее значение подрезается до цели —
    достигнув условия, игрок либо уже открыл ачивку (см. is_done в
    _achv_page_text), либо вот-вот откроет, показывать "12/10" смысла
    нет."""
    provider = PROGRESS_PROVIDERS.get(achv_id)
    if provider is None:
        return None
    target, get_current = provider
    current = await get_current(user_id)
    return min(current, target), target


# ==========================
#   КАТЕГОРИИ
# ==========================

CATEGORIES = {
    "panda": {"ru": "Панда", "en": "Panda"},
    "garden": {"ru": "Сад", "en": "Garden"},
    "bakery": {"ru": "Пекарня", "en": "Bakery"},
    "market": {"ru": "Рынок", "en": "Market"},
    "general": {"ru": "Общее", "en": "General"},
}

CATEGORY_ORDER = ["panda", "garden", "bakery", "market", "general"]

# Кастомные тг-эмодзи для категорий (кнопки раздела в _categories_keyboard
# и заголовок карточки ачивки в _achv_page_text). У "Пекарни" отдельного
# кастомного эмодзи нет — используется обычный юникодный 🥐 (см.
# CATEGORY_FALLBACK_EMOJI). Для текста сообщений эмодзи вставляется через
# _ce()/<tg-emoji> (см. ниже), для кнопок — через icon_custom_emoji_id,
# тег <tg-emoji> в тексте кнопки не рендерится (тот же нюанс, что и у
# остальных кнопочных эмодзи в файле).
CATEGORY_EMOJI_ID = {
    "panda": "5344057622628671718",    # 🙂
    "garden": "5422407403884798028",   # 🍀
    "market": "5920332557466997677",   # 🏪
    "general": "5438496463044752972",  # ⭐️
}
CATEGORY_FALLBACK_EMOJI = {
    "panda": "🙂",
    "garden": "🍀",
    "bakery": "🥐",
    "market": "🏪",
    "general": "⭐️",
}


def _category_icon_text(cat_id: str) -> str:
    """Иконка категории для текста сообщения (заголовок карточки
    ачивки) — кастомный тг-эмодзи через _ce(), либо обычный глиф, если
    для категории нет кастомного id (сейчас — только "Пекарня")."""
    emoji_id = CATEGORY_EMOJI_ID.get(cat_id)
    glyph = CATEGORY_FALLBACK_EMOJI[cat_id]
    return _ce(emoji_id, glyph) if emoji_id else glyph



# ==========================
#   СПИСОК АЧИВОК
# ==========================
# reward: xp начисляется через prof.add_xp, crystals — через
# prof.add_crystals, coins — через shop.add_balance. 0 — не начислять.
# category — id из CATEGORIES, определяет раздел меню.

ACHIEVEMENTS = {
    "panda_full_stats": {
        "category": "panda",
        "name": {"ru": "Верный друг", "en": "Loyal friend"},
        "description": {
            "ru": (
                "Позаботьтесь о своей панде так, чтобы одновременно все три "
                "её показателя — голод, настроение и дружба — достигли "
                "100%. Кормите её вовремя, играйте и уделяйте внимание "
                "каждый день, пока все шкалы не заполнятся до предела."
            ),
            "en": (
                "Take care of your panda until all three of its stats — "
                "hunger, mood, and friendship — reach 100% at the same "
                "time. Feed it regularly, play with it, and give it "
                "attention every day until every bar is completely full."
            ),
        },
        "reward": {"xp": 500, "crystals": 5, "coins": 250},
    },
    "first_harvest": {
        "category": "garden",
        "name": {"ru": "Первый урожай", "en": "First harvest"},
        "description": {
            "ru": (
                "Вырастите на грядке хотя бы одно растение до полного "
                "созревания и соберите урожай в свою корзину. Ачивка "
                "выдаётся сразу после первого успешного сбора фрукта "
                "на огороде."
            ),
            "en": (
                "Grow at least one plant on your plot until it fully "
                "ripens, then collect the fruit into your basket. The "
                "achievement unlocks right after your very first "
                "successful harvest in the garden."
            ),
        },
        "reward": {"xp": 200, "crystals": 0, "coins": 100},
    },
    "level_10": {
        "category": "general",
        "name": {"ru": "Опытный игрок", "en": "Experienced player"},
        "description": {
            "ru": (
                "Наберите достаточно опыта, чтобы поднять уровень своего "
                "профиля до 10-го. Опыт начисляется за большинство "
                "активностей в боте — кормление панды, сбор урожая, "
                "выпечку и другие действия."
            ),
            "en": (
                "Earn enough experience to raise your profile level to "
                "10. XP is awarded for most activities in the bot — "
                "feeding the panda, harvesting, baking, and other "
                "actions."
            ),
        },
        # xp: 0 НАМЕРЕННО — это ачивка "достигни уровня N". Если она сама
        # даёт опыт, её награда может протолкнуть игрока через СЛЕДУЮЩИЙ
        # уровневый порог ещё внутри того же add_xp(), а его награда —
        # через следующий, и т.д.: одна естественная левелапа устраивает
        # каскад из всех уровневых ачивок разом (было именно так — см.
        # обсуждение в чате: одно достижение 5-го уровня раскрывало все
        # пороги вплоть до 50-го). Крист��ллы/монеты остаются — они не
        # влияют на level_from_xp() и каскад не создают.
        "reward": {"xp": 0, "crystals": 5, "coins": 800},
    },
    "gift_giver": {
        "category": "general",
        "name": {"ru": "Щедрость", "en": "Generosity"},
        "description": {
            "ru": (
                "Отправьте подарок другому игроку через профиль или "
                "купите подарок самому себе в разделе подарков. Неважно, "
                "кому предназначен подарок — главное, сам факт покупки."
            ),
            "en": (
                "Send a gift to another player through the profile, or "
                "buy a gift for yourself in the gifts section. It "
                "doesn't matter who the gift is for — what matters is "
                "making the purchase."
            ),
        },
        "reward": {"xp": 250, "crystals": 0, "coins": 500},
    },
    "first_feed": {
        "category": "panda",
        "name": {"ru": "Первое кормление", "en": "First feeding"},
        "description": {
            "ru": (
                "Покормите свою панду в первый раз — зайдите в раздел с "
                "пандой и используйте любой корм. Это самое первое "
                "взаимодействие с питомцем, с которого начинается уход "
                "за ним."
            ),
            "en": (
                "Feed your panda for the very first time — open the "
                "panda section and use any food item. This is the very "
                "first interaction with your pet, the starting point of "
                "taking care of it."
            ),
        },
        "reward": {"xp": 50, "crystals": 0, "coins": 50},
    },
    "first_listing": {
        "category": "market",
        "name": {"ru": "Торговец", "en": "Trader"},
        "description": {
            "ru": (
                "Создайте свой первый лот на рынке — выставьте на "
                "продажу любой предмет из инвентаря. Ачивка не зависит "
                "от того, купит ли кто-то ваш товар, важен сам факт "
                "выставления лота."
            ),
            "en": (
                "Create your first listing on the market — put any item "
                "from your inventory up for sale. The achievement "
                "doesn't depend on whether anyone buys it — placing the "
                "listing is enough."
            ),
        },
        "reward": {"xp": 80, "crystals": 0, "coins": 150},
    },

    # ---- Рынок: доп. ачивки (счётчики/пороги/разовые события — см.
    # shop.py, секция "рыночные ачивки") ----
    "market_first_purchase": {
        "category": "market",
        "name": {"ru": "Первая покупка на рынке", "en": "First market purchase"},
        "description": {
            "ru": (
                "Купите свой первый лот у другого игрока на рынке — "
                "заплатите Pn и заберите товар себе. Не имеет значения, "
                "что именно куплено, важен сам факт первой покупки у "
                "другого игрока."
            ),
            "en": (
                "Buy your very first listing from another player on the "
                "market — pay Pn and take the item for yourself. It "
                "doesn't matter what you buy — what counts is your "
                "first purchase from another player."
            ),
        },
        "reward": {"xp": 100, "crystals": 0, "coins": 0},
    },
    "market_buy_50": {
        "category": "market",
        "name": {"ru": "Ярмарка изобилия", "en": "Fair of plenty"},
        "description": {
            "ru": (
                "Купите на рынке 50 товаров суммарно — любых, фрукты и "
                "выпечку вместе, за все ваши покупки лотов у других "
                "игроков."
            ),
            "en": (
                "Buy 50 items on the market in total — any kind, fruit "
                "and bakery combined, across all your purchases from "
                "other players."
            ),
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "market_buy_250": {
        "category": "market",
        "name": {"ru": "Оптовик", "en": "Wholesale buyer"},
        "description": {
            "ru": (
                "Купите на рынке 250 товаров суммарно — любых, фрукты и "
                "выпечку вместе. Долгий путь постоянного покупателя, "
                "заметно дольше «Ярмарки изобилия»."
            ),
            "en": (
                "Buy 250 items on the market in total — any kind, fruit "
                "and bakery combined. A longer road than \u00abFair of "
                "plenty\u00bb, for a truly regular buyer."
            ),
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 1500},
    },
    "market_spent_10000": {
        "category": "market",
        "name": {"ru": "Мешок с деньгами", "en": "Bag of money"},
        "description": {
            "ru": (
                "Потратьте на рынке 10 000 Pn суммарно, покупая товары "
                "у других игроков — учитываются все ваши покупки лотов."
            ),
            "en": (
                "Spend 10,000 Pn in total on the market, buying items "
                "from other players — all your listing purchases count."
            ),
        },
        "reward": {"xp": 900, "crystals": 6, "coins": 0},
    },
    "market_sold_100": {
        "category": "market",
        "name": {"ru": "Удачная сделка", "en": "Good deal"},
        "description": {
            "ru": (
                "Продайте на рынке 100 товаров суммарно — считаются и "
                "лоты, купленные другими игроками, и товары, сброшенные "
                "боту через мгновенную продажу, любого типа."
            ),
            "en": (
                "Sell 100 items on the market in total — both listings "
                "bought by other players and instant sales to the bot "
                "count, any item type."
            ),
        },
        "reward": {"xp": 700, "crystals": 5, "coins": 700},
    },
    "market_earned_20000": {
        "category": "market",
        "name": {"ru": "Рыночная империя", "en": "Market empire"},
        "description": {
            "ru": (
                "Заработайте на рынке 20 000 Pn суммарно — учитываются "
                "деньги и от проданных лотов, и от мгновенной продажи "
                "боту, любого типа товара."
            ),
            "en": (
                "Earn 20,000 Pn on the market in total — money from "
                "listings sold to other players and from instant sales "
                "to the bot both count, any item type."
            ),
        },
        "reward": {"xp": 2000, "crystals": 15, "coins": 0},
    },
    "market_full_shelves": {
        "category": "market",
        "name": {"ru": "Все полки заняты", "en": "Every shelf taken"},
        "description": {
            "ru": (
                "Выставьте на рынок максимум лотов одновременно — все "
                "10 из 10 доступных мест. Как только количество активных "
                "лотов достигнет предела, ачивка сразу засчитается."
            ),
            "en": (
                "Have the maximum number of listings active on the "
                "market at once — all 10 out of 10 available slots. As "
                "soon as your active listings reach the limit, the "
                "achievement unlocks."
            ),
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 300},
    },
    "market_cancel_listing": {
        "category": "market",
        "name": {"ru": "Передумал", "en": "Changed my mind"},
        "description": {
            "ru": (
                "Снимите свой лот с продажи хотя бы один раз — товар "
                "вернётся в корзину, а рынок запомнит, что вы передумали."
            ),
            "en": (
                "Remove your own listing from the market at least once "
                "— the item returns to your basket, and the market "
                "remembers you changed your mind."
            ),
        },
        "reward": {"xp": 100, "crystals": 0, "coins": 100},
    },
    "market_big_deal": {
        "category": "market",
        "name": {"ru": "Крупная сделка", "en": "Big deal"},
        "description": {
            "ru": (
                "Купите или продайте один лот на рынке на сумму от "
                "2000 Pn разом — считается общая стоимость лота (цена "
                "за штуку × количество), а не сумма отдельных покупок."
            ),
            "en": (
                "Buy or sell a single listing on the market worth "
                "2,000 Pn or more at once — the listing's total value "
                "(price per unit × quantity) counts, not separate "
                "purchases added together."
            ),
        },
        "reward": {"xp": 500, "crystals": 4, "coins": 500},
    },
    "market_loyal_customer": {
        "category": "market",
        "name": {"ru": "Постоянный клиент", "en": "Regular customer"},
        "description": {
            "ru": (
                "Купите товар у 5 разных продавцов на рынке — "
                "считаются именно разные игроки, а не количество "
                "покупок."
            ),
            "en": (
                "Buy items from 5 different sellers on the market — "
                "distinct players count, not the number of purchases."
            ),
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "market_own_clientele": {
        "category": "market",
        "name": {"ru": "Своя клиентура", "en": "A clientele of your own"},
        "description": {
            "ru": (
                "Продайте товар 5 разным покупателям на рынке — "
                "считаются именно разные игроки, купившие ваши лоты."
            ),
            "en": (
                "Sell items to 5 different buyers on the market — "
                "distinct players who bought your listings count."
            ),
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "market_diverse_trader": {
        "category": "market",
        "name": {"ru": "Разносторонний торговец", "en": "Well-rounded trader"},
        "description": {
            "ru": (
                "Купите на рынке хотя бы раз и фрукт, и выпечку — "
                "любых видов, главное, чтобы среди покупок был хотя бы "
                "один товар каждой из двух категорий."
            ),
            "en": (
                "Buy at least one fruit and one bakery item on the "
                "market — any kind, as long as your purchases cover "
                "both categories at least once each."
            ),
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 300},
    },
    "market_max_price": {
        "category": "market",
        "name": {"ru": "Дорогая штучка", "en": "Pricey little thing"},
        "description": {
            "ru": (
                "Выставьте лот по максимально возможной цене для этого "
                "товара — по верхней границе его ценового диапазона."
            ),
            "en": (
                "List an item at the highest possible price for it — "
                "the upper bound of its price range."
            ),
        },
        "reward": {"xp": 250, "crystals": 1, "coins": 250},
    },
    "market_min_price": {
        "category": "market",
        "name": {"ru": "Распродажа", "en": "Clearance sale"},
        "description": {
            "ru": (
                "Выставьте лот по минимально возможной цене для этого "
                "товара — по нижней границе его ценового диапазона."
            ),
            "en": (
                "List an item at the lowest possible price for it — "
                "the lower bound of its price range."
            ),
        },
        "reward": {"xp": 250, "crystals": 1, "coins": 250},
    },
    "market_spend_it_all": {
        "category": "market",
        "name": {"ru": "Всё до копейки", "en": "Every last coin"},
        "description": {
            "ru": (
                "Потратьте на рынке весь баланс до последнего Pn одной "
                "покупкой — после сделки на счету должно остаться ровно "
                "0 Pn."
            ),
            "en": (
                "Spend your entire balance down to the last Pn on a "
                "single market purchase — after the deal your balance "
                "should be exactly 0 Pn."
            ),
        },
        "reward": {"xp": 350, "crystals": 2, "coins": 0},
    },

    "first_donate": {
        "category": "general",
        "name": {"ru": "Первый донат", "en": "First donation"},
        "description": {
            "ru": (
                "Приобретите любое количество кристаллов через магазин "
                "доната. Сумма покупки не важна — засчитывается любая, "
                "даже минимальная транзакция."
            ),
            "en": (
                "Purchase any amount of crystals through the donation "
                "shop. The amount doesn't matter — even the smallest "
                "transaction counts."
            ),
        },
        "reward": {"xp": 500, "crystals": 0, "coins": 500},
    },
    "level_25": {
        "category": "general",
        "name": {"ru": "Мастер", "en": "Master"},
        "description": {
            "ru": (
                "Продолжайте копить опыт и доберитесь до 25-го уровня "
                "профиля. Это заметно более долгий путь, чем 10-й "
                "уровень, и требует стабильной активности в разных "
                "разделах бота."
            ),
            "en": (
                "Keep earning experience and reach profile level 25. "
                "This is a noticeably longer journey than level 10 and "
                "requires steady activity across different parts of the "
                "bot."
            ),
        },
        # xp: 0 НАМЕРЕННО — та же причина, что у level_10 выше (разрыв
        # каскада уровневых ачивок).
        "reward": {"xp": 0, "crystals": 15, "coins": 5000},
    },
    "panda_skin": {
        "category": "panda",
        "name": {"ru": "Модник", "en": "Fashionista"},
        "description": {
            "ru": (
                "Получите любой облик (скин) для своей панды — купите "
                "его в магазине или получите иным способом, если это "
                "предусмотрено событиями. Ачивка выдаётся за сам факт "
                "обладания обликом."
            ),
            "en": (
                "Obtain any skin for your panda — buy one in the shop or "
                "get it another way if events allow it. The achievement "
                "is awarded simply for owning a skin."
            ),
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 2500},
    },
    "first_bake": {
        "category": "bakery",
        "name": {"ru": "Кондитер", "en": "Confectioner"},
        "description": {
            "ru": (
                "Испеките что-нибудь в пекарне в первый раз — выберите "
                "любой рецепт и завершите процесс выпечки. Неважно, "
                "какое именно блюдо вы приготовите, главное — первый "
                "успешный результат."
            ),
            "en": (
                "Bake something in the bakery for the first time — pick "
                "any recipe and finish the baking process. It doesn't "
                "matter what exactly you make — what counts is your "
                "first successful bake."
            ),
        },
        "reward": {"xp": 250, "crystals": 8, "coins": 650},
    },

    # ---- Пекарня: доп. ачивки (счётчики/пороги — см. bakery.py,
    # секция "АЧИВКИ ПЕКАРНИ") ----
    "bakery_taster": {
        "category": "bakery",
        "name": {"ru": "Дегустатор", "en": "Taster"},
        "description": {
            "ru": "Испечь 5 разных видов выпечки — любых, по одному разу каждый.",
            "en": "Bake 5 different kinds of pastry — any ones, once each.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 300},
    },
    "bakery_all_recipes": {
        "category": "bakery",
        "name": {"ru": "Все рецепты хотя бы раз", "en": "All recipes tried"},
        "description": {
            "ru": "Испечь все 14 доступных рецептов хотя бы по одному разу.",
            "en": "Bake all 14 available recipes at least once each.",
        },
        "reward": {"xp": 1800, "crystals": 12, "coins": 2500},
    },
    "bakery_royal_table": {
        "category": "bakery",
        "name": {"ru": "Королевский стол", "en": "Royal table"},
        "description": {
            "ru": "Испечь «Королевский тропический торт» — самый сложный рецепт пекарни.",
            "en": "Bake the \u00abRoyal tropical cake\u00bb — the bakery's most demanding recipe.",
        },
        "reward": {"xp": 600, "crystals": 5, "coins": 800},
    },
    "bakery_brownie_master": {
        "category": "bakery",
        "name": {"ru": "Мастер брауни", "en": "Brownie master"},
        "description": {
            "ru": "Испечь «Грушевый шоколадный брауни» 10 раз.",
            "en": "Bake the \u00abPear chocolate brownie\u00bb 10 times.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "bakery_classic_lover": {
        "category": "bakery",
        "name": {"ru": "Верен классике", "en": "Loyal to the classics"},
        "description": {
            "ru": "Испечь «Яблочный пирог» 25 раз.",
            "en": "Bake \u00abApple pie\u00bb 25 times.",
        },
        "reward": {"xp": 500, "crystals": 4, "coins": 500},
    },
    "bakery_baked_50": {
        "category": "bakery",
        "name": {"ru": "Постоянный пекарь", "en": "Regular baker"},
        "description": {
            "ru": "Испечь 50 изделий — любых, суммарно.",
            "en": "Bake 50 items in total — any recipes combined.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "bakery_baked_250": {
        "category": "bakery",
        "name": {"ru": "Промышленный масштаб", "en": "Industrial scale"},
        "description": {
            "ru": "Испечь 250 изделий суммарно.",
            "en": "Bake 250 items in total.",
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 2000},
    },
    "bakery_baked_1000": {
        "category": "bakery",
        "name": {"ru": "Пекарня-легенда", "en": "Legendary bakery"},
        "description": {
            "ru": "Испечь 1000 изделий суммарно.",
            "en": "Bake 1000 items in total.",
        },
        "reward": {"xp": 4000, "crystals": 25, "coins": 6000},
    },
    "bakery_both_ovens": {
        "category": "bakery",
        "name": {"ru": "Обе печи в деле", "en": "Both ovens at work"},
        "description": {
            "ru": "Одновременно запустить выпечку в обеих печах.",
            "en": "Have both ovens baking something at the same time.",
        },
        "reward": {"xp": 150, "crystals": 0, "coins": 150},
    },
    "bakery_no_idle_7": {
        "category": "bakery",
        "name": {"ru": "Ни минуты простоя", "en": "Not a minute idle"},
        "description": {
            "ru": "7 дней подряд держать хотя бы одну печь занятой.",
            "en": "Keep at least one oven busy for 7 days in a row.",
        },
        "reward": {"xp": 600, "crystals": 4, "coins": 600},
    },
    "bakery_first_purchase": {
        "category": "bakery",
        "name": {"ru": "Первая покупка", "en": "First purchase"},
        "description": {
            "ru": "Купить ингредиент в лавке пекарни впервые.",
            "en": "Buy an ingredient in the bakery shop for the first time.",
        },
        "reward": {"xp": 50, "crystals": 0, "coins": 50},
    },
    "bakery_bulk_50": {
        "category": "bakery",
        "name": {"ru": "Про запас", "en": "Stocking up"},
        "description": {
            "ru": "Купить за раз 50 единиц одного ингредиента.",
            "en": "Buy 50 units of one ingredient in a single purchase.",
        },
        "reward": {"xp": 200, "crystals": 1, "coins": 200},
    },
    "bakery_chocolate_100": {
        "category": "bakery",
        "name": {"ru": "Шоколадный магнат", "en": "Chocolate magnate"},
        "description": {
            "ru": "Купить 100 шоколада суммарно — самого дорогого ингредиента лавки.",
            "en": "Buy 100 chocolate in total — the shop's most expensive ingredient.",
        },
        "reward": {"xp": 350, "crystals": 2, "coins": 400},
    },
    "bakery_full_ingredients": {
        "category": "bakery",
        "name": {"ru": "Полная кладовая", "en": "Full pantry"},
        "description": {
            "ru": "Иметь на складе одновременно хотя бы по 10 каждого из 6 ингредиентов.",
            "en": "Hold at least 10 of each of the 6 ingredients in stock at the same time.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "bakery_big_spender_10000": {
        "category": "bakery",
        "name": {"ru": "Крупный транжира", "en": "Big spender"},
        "description": {
            "ru": "Потратить в лавке пекарни 10 000 Pn суммарно.",
            "en": "Spend 10,000 Pn in the bakery shop in total.",
        },
        "reward": {"xp": 900, "crystals": 6, "coins": 0},
    },
    "bakery_showcase_20": {
        "category": "bakery",
        "name": {"ru": "Полная витрина", "en": "Full showcase"},
        "description": {
            "ru": "Накопить на витрине пекарни 20 изделий одновременно.",
            "en": "Have 20 items on the bakery showcase at the same time.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 300},
    },
    "bakery_market_sell_20": {
        "category": "bakery",
        "name": {"ru": "Кондитерская лавка", "en": "Pastry stall"},
        "description": {
            "ru": "Продать 20 изделий выпечки на рынке.",
            "en": "Sell 20 pastry items on the market.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "bakery_market_earn_5000": {
        "category": "bakery",
        "name": {"ru": "Сладкий бизнес", "en": "Sweet business"},
        "description": {
            "ru": "Заработать 5000 Pn суммарно с продажи выпечки на рынке.",
            "en": "Earn 5,000 Pn in total from selling pastries on the market.",
        },
        "reward": {"xp": 1200, "crystals": 8, "coins": 0},
    },
    "bakery_feed_panda_20": {
        "category": "bakery",
        "name": {"ru": "Сладкоежка", "en": "Sweet tooth"},
        "description": {
            "ru": "Покормить панду выпечкой — любой — 20 раз.",
            "en": "Feed the panda pastry — any kind — 20 times.",
        },
        "reward": {"xp": 350, "crystals": 2, "coins": 350},
    },
    "bakery_feed_cake_10": {
        "category": "bakery",
        "name": {"ru": "Праздничный торт", "en": "Celebration cake"},
        "description": {
            "ru": (
                "Покормить панду именно тортом/гато — любым из «тяжёлых» "
                "рецептов с временем выпечки от 16 минут — 10 раз."
            ),
            "en": (
                "Feed the panda an actual cake/gateau — any \u00abheavy\u00bb "
                "recipe with a bake time of 16 minutes or more — 10 times."
            ),
        },
        "reward": {"xp": 450, "crystals": 3, "coins": 450},
    },
    "bakery_garden_to_oven": {
        "category": "bakery",
        "name": {"ru": "Из сада в печь", "en": "From garden to oven"},
        "description": {
            "ru": "Использовать в выпечке фрукты всех видов, что растут в саду — каждый хотя бы раз.",
            "en": "Use every kind of fruit grown in the garden in baking — each at least once.",
        },
        "reward": {"xp": 700, "crystals": 5, "coins": 700},
    },
    "bakery_night_shift": {
        "category": "bakery",
        "name": {"ru": "Ночная смена", "en": "Night shift"},
        "description": {
            "ru": "Забрать готовую выпечку из печи между 00:00 и 05:00.",
            "en": "Collect a finished bake from the oven between 00:00 and 05:00.",
        },
        "reward": {"xp": 250, "crystals": 2, "coins": 250},
    },

    # ---- Панда: доп. ачивки ----
    "panda_first_pet": {
        "category": "panda",
        "name": {"ru": "Первое поглаживание", "en": "First pat"},
        "description": {
            "ru": "Погладить панду первый раз.",
            "en": "Pet your panda for the very first time.",
        },
        "reward": {"xp": 40, "crystals": 0, "coins": 40},
    },
    "panda_pet_10": {
        "category": "panda",
        "name": {"ru": "Ласковый хозяин", "en": "Gentle owner"},
        "description": {
            "ru": "Погладить панду 10 раз.",
            "en": "Pet your panda 10 times.",
        },
        "reward": {"xp": 150, "crystals": 0, "coins": 150},
    },
    "panda_pet_100": {
        "category": "panda",
        "name": {"ru": "Мастер поглаживаний", "en": "Petting master"},
        "description": {
            "ru": "Погладить панду 100 раз.",
            "en": "Pet your panda 100 times.",
        },
        "reward": {"xp": 900, "crystals": 8, "coins": 1200},
    },
    "panda_feed_10": {
        "category": "panda",
        "name": {"ru": "Кормилец", "en": "Feeder"},
        "description": {
            "ru": "Покормить панду 10 раз.",
            "en": "Feed your panda 10 times.",
        },
        "reward": {"xp": 120, "crystals": 0, "coins": 120},
    },
    "panda_feed_100": {
        "category": "panda",
        "name": {"ru": "Заботливый хозяин", "en": "Caring owner"},
        "description": {
            "ru": "Покормить панду 100 раз.",
            "en": "Feed your panda 100 times.",
        },
        "reward": {"xp": 700, "crystals": 6, "coins": 900},
    },
    "panda_feed_500": {
        "category": "panda",
        "name": {"ru": "Идеальный уход", "en": "Perfect care"},
        "description": {
            "ru": "Покормить панду 500 раз.",
            "en": "Feed your panda 500 times.",
        },
        "reward": {"xp": 2500, "crystals": 18, "coins": 4000},
    },
    "panda_mood_100": {
        "category": "panda",
        "name": {"ru": "Хорошее настроение", "en": "Good mood"},
        "description": {
            "ru": "Довести настроение панды до 100%.",
            "en": "Raise your panda's mood to 100%.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 200},
    },
    "panda_hunger_100": {
        "category": "panda",
        "name": {"ru": "Сытая панда", "en": "Well fed"},
        "description": {
            "ru": "Довести сытость панды до 100%.",
            "en": "Raise your panda's hunger stat to 100%.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 200},
    },
    "panda_friendship_100": {
        "category": "panda",
        "name": {"ru": "Крепкая дружба", "en": "Strong bond"},
        "description": {
            "ru": "Довести дружбу панды до 100%.",
            "en": "Raise your panda's friendship stat to 100%.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 300},
    },
    "panda_streak_7": {
        "category": "panda",
        "name": {"ru": "Неделя заботы", "en": "Week of care"},
        "description": {
            "ru": "Заботиться о панде 7 дней подряд.",
            "en": "Take care of your panda 7 days in a row.",
        },
        "reward": {"xp": 350, "crystals": 3, "coins": 400},
    },
    "panda_streak_30": {
        "category": "panda",
        "name": {"ru": "Месяц заботы", "en": "Month of care"},
        "description": {
            "ru": "Заботиться о панде 30 дней подряд.",
            "en": "Take care of your panda 30 days in a row.",
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 2000},
    },
    "panda_streak_100": {
        "category": "panda",
        "name": {"ru": "100 дней вместе", "en": "100 days together"},
        "description": {
            "ru": "Заботиться о панде 100 дней подряд.",
            "en": "Take care of your panda 100 days in a row.",
        },
        "reward": {"xp": 4000, "crystals": 25, "coins": 6000},
    },
    "panda_never_hungry_week": {
        "category": "panda",
        "name": {"ru": "Ни разу голодной", "en": "Never hungry"},
        "description": {
            "ru": "Неделю не давать сытости панды упасть ниже 50%.",
            "en": "Keep your panda's hunger stat above 50% for a whole week.",
        },
        "reward": {"xp": 500, "crystals": 4, "coins": 500},
    },
    "panda_skins_3": {
        "category": "panda",
        "name": {"ru": "Коллекционер", "en": "Collector"},
        "description": {
            "ru": "Собрать 3 облика для панды.",
            "en": "Collect 3 skins for your panda.",
        },
        "reward": {"xp": 800, "crystals": 6, "coins": 1000},
    },
    "panda_skins_5": {
        "category": "panda",
        "name": {"ru": "Модный гардероб", "en": "Fashion closet"},
        "description": {
            "ru": "Собрать 5 обликов для панды.",
            "en": "Collect 5 skins for your panda.",
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 2000},
    },
    "panda_skins_all": {
        "category": "panda",
        "name": {"ru": "Икона стиля", "en": "Style icon"},
        "description": {
            "ru": "Собрать все доступные облики панды.",
            "en": "Collect every available panda skin.",
        },
        "reward": {"xp": 3500, "crystals": 20, "coins": 5000},
    },
    "panda_named": {
        "category": "panda",
        "name": {"ru": "Как тебя зовут?", "en": "What's your name?"},
        "description": {
            "ru": "Дать панде имя.",
            "en": "Give your panda a name.",
        },
        "reward": {"xp": 60, "crystals": 0, "coins": 60},
    },
    "panda_birthday": {
        "category": "panda",
        "name": {"ru": "Годовщина", "en": "Anniversary"},
        "description": {
            "ru": "Прожить с пандой ровно год.",
            "en": "Spend a full year with your panda.",
        },
        "reward": {"xp": 3000, "crystals": 20, "coins": 4000},
    },

    # ---- Огород: доп. ачивки ----
    "garden_first_plant": {
        "category": "garden",
        "name": {"ru": "Первая посадка", "en": "First planting"},
        "description": {
            "ru": "Посадить первое растение.",
            "en": "Plant your very first crop.",
        },
        "reward": {"xp": 50, "crystals": 0, "coins": 50},
    },
    "garden_harvest_10": {
        "category": "garden",
        "name": {"ru": "Сборщик", "en": "Picker"},
        "description": {
            "ru": "Собрать 10 фруктов.",
            "en": "Harvest 10 fruits.",
        },
        "reward": {"xp": 150, "crystals": 0, "coins": 150},
    },
    "garden_harvest_100": {
        "category": "garden",
        "name": {"ru": "Опытный садовод", "en": "Skilled gardener"},
        "description": {
            "ru": "Собрать 100 фруктов.",
            "en": "Harvest 100 fruits.",
        },
        "reward": {"xp": 700, "crystals": 5, "coins": 900},
    },
    "garden_harvest_1000": {
        "category": "garden",
        "name": {"ru": "Урожайный год", "en": "Bumper harvest"},
        "description": {
            "ru": "Собрать 1000 фруктов.",
            "en": "Harvest 1000 fruits.",
        },
        "reward": {"xp": 3000, "crystals": 20, "coins": 4500},
    },
    "garden_all_plots_full": {
        "category": "garden",
        "name": {"ru": "Все грядки заняты", "en": "Every plot full"},
        "description": {
            "ru": "Занять все 3 грядки одновременно.",
            "en": "Have all 3 plots planted at the same time.",
        },
        "reward": {"xp": 200, "crystals": 1, "coins": 250},
    },
    "garden_all_plots_ripe": {
        "category": "garden",
        "name": {"ru": "Тройной урожай", "en": "Triple harvest"},
        "description": {
            "ru": "Собрать урожай сразу со всех 3 грядок в один заход.",
            "en": "Harvest all 3 plots at once, in a single visit.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 400},
    },
    "garden_all_crops": {
        "category": "garden",
        "name": {"ru": "Ботаник", "en": "Botanist"},
        "description": {
            "ru": "Вырастить все 8 видов культур хотя бы по разу.",
            "en": "Grow all 8 crop types at least once.",
        },
        "reward": {"xp": 900, "crystals": 7, "coins": 1000},
    },
    "garden_one_crop_20": {
        "category": "garden",
        "name": {"ru": "Специалист по бамбуку", "en": "Bamboo specialist"},
        "description": {
            "ru": "Собрать одну и ту же культуру 20 раз.",
            "en": "Harvest the same crop 20 times.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 300},
    },
    "garden_streak_7": {
        "category": "garden",
        "name": {"ru": "Неделя в саду", "en": "Week in the garden"},
        "description": {
            "ru": "Заходить в сад 7 дней подряд.",
            "en": "Visit the garden 7 days in a row.",
        },
        "reward": {"xp": 350, "crystals": 3, "coins": 400},
    },
    "garden_streak_30": {
        "category": "garden",
        "name": {"ru": "Месяц в саду", "en": "Month in the garden"},
        "description": {
            "ru": "Заходить в сад 30 дней подряд.",
            "en": "Visit the garden 30 days in a row.",
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 2000},
    },
    "garden_pineapple_first": {
        "category": "garden",
        "name": {"ru": "Экзотика", "en": "Exotic"},
        "description": {
            "ru": "Впервые собрать ананас — самую долгую по времени роста культуру.",
            "en": "Harvest a pineapple for the first time — the slowest-growing crop.",
        },
        "reward": {"xp": 200, "crystals": 1, "coins": 250},
    },
    "garden_bamboo_100": {
        "category": "garden",
        "name": {"ru": "Бамбуковая ферма", "en": "Bamboo farm"},
        "description": {
            "ru": "Собрать 100 бамбука.",
            "en": "Harvest 100 bamboo.",
        },
        "reward": {"xp": 400, "crystals": 3, "coins": 500},
    },
    "garden_basket_50": {
        "category": "garden",
        "name": {"ru": "Полная корзина", "en": "Full basket"},
        "description": {
            "ru": "Накопить в корзине 50 фруктов одновременно, не продавая их.",
            "en": "Accumulate 50 fruits in your basket at once, without selling any.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 400},
    },
    "garden_feed_from_basket": {
        "category": "garden",
        "name": {"ru": "Прямо с грядки", "en": "Straight from the plot"},
        "description": {
            "ru": "Покормить панду фруктом из корзины.",
            "en": "Feed your panda a fruit from the basket.",
        },
        "reward": {"xp": 100, "crystals": 0, "coins": 100},
    },
    "garden_sell_50": {
        "category": "garden",
        "name": {"ru": "Огородный бизнес", "en": "Garden business"},
        "description": {
            "ru": "Продать 50 фруктов на рынке.",
            "en": "Sell 50 fruits on the market.",
        },
        "reward": {"xp": 500, "crystals": 3, "coins": 700},
    },
    "garden_instant_sell": {
        "category": "garden",
        "name": {"ru": "Быстрая сделка", "en": "Quick deal"},
        "description": {
            "ru": "Мгновенно продать фрукт боту, не выставляя лот на рынке.",
            "en": "Instantly sell a fruit to the bot instead of listing it on the market.",
        },
        "reward": {"xp": 150, "crystals": 0, "coins": 200},
    },
    "garden_harvest_night": {
        "category": "garden",
        "name": {"ru": "Ночной сбор", "en": "Night harvest"},
        "description": {
            "ru": "Собрать урожай после полуночи.",
            "en": "Harvest a crop after midnight.",
        },
        "reward": {"xp": 150, "crystals": 0, "coins": 200},
    },
    "garden_harvest_10000": {
        "category": "garden",
        "name": {"ru": "Легенда огорода", "en": "Garden legend"},
        "description": {
            "ru": "Собрать 10 000 фруктов суммарно.",
            "en": "Harvest 10,000 fruits in total.",
        },
        "reward": {"xp": 5000, "crystals": 30, "coins": 8000},
    },

    # ---- Общее: доп. ачивки ----
    "general_level_5": {
        "category": "general",
        "name": {"ru": "Новичок освоился", "en": "Settling in"},
        "description": {
            "ru": "Достичь 5 уровня.",
            "en": "Reach profile level 5.",
        },
        # xp: 0 — см. подробный комментарий у level_10 (разрыв каскада).
        "reward": {"xp": 0, "crystals": 1, "coins": 200},
    },
    "general_level_15": {
        "category": "general",
        "name": {"ru": "Уверенный игрок", "en": "Confident player"},
        "description": {
            "ru": "Достичь 15 уровня.",
            "en": "Reach profile level 15.",
        },
        # xp: 0 — см. level_10.
        "reward": {"xp": 0, "crystals": 8, "coins": 1200},
    },
    "general_level_20": {
        "category": "general",
        "name": {"ru": "Продвинутый", "en": "Advanced"},
        "description": {
            "ru": "Достичь 20 уровня.",
            "en": "Reach profile level 20.",
        },
        # xp: 0 — см. level_10.
        "reward": {"xp": 0, "crystals": 10, "coins": 1800},
    },
    "general_level_30": {
        "category": "general",
        "name": {"ru": "Ветеран", "en": "Veteran"},
        "description": {
            "ru": "Достичь 30 уровня.",
            "en": "Reach profile level 30.",
        },
        # xp: 0 — см. level_10.
        "reward": {"xp": 0, "crystals": 18, "coins": 3500},
    },
    "general_level_40": {
        "category": "general",
        "name": {"ru": "Элита", "en": "Elite"},
        "description": {
            "ru": "Достичь 40 уровня.",
            "en": "Reach profile level 40.",
        },
        # xp: 0 — см. level_10.
        "reward": {"xp": 0, "crystals": 25, "coins": 5500},
    },
    "general_level_50": {
        "category": "general",
        "name": {"ru": "Легенда", "en": "Legend"},
        "description": {
            "ru": "Достичь 50 уровня.",
            "en": "Reach profile level 50.",
        },
        # xp: 0 — см. level_10.
        "reward": {"xp": 0, "crystals": 35, "coins": 8000},
    },
    "general_xp_10000": {
        "category": "general",
        "name": {"ru": "Кладезь опыта", "en": "Wealth of experience"},
        "description": {
            "ru": "Набрать суммарно 10 000 опыта.",
            "en": "Earn 10,000 XP in total.",
        },
        # xp: 0 — эта ачивка сама триггерится по накопленному XP (new_xp
        # >= 10000), поэтому щедрая XP-награда здесь так же опасна, как и
        # у уровневых ачивок выше (см. level_10) — может протолкнуть
        # игрока сразу через несколько уровневых порогов.
        "reward": {"xp": 0, "crystals": 3, "coins": 600},
    },
    "general_login_streak_7": {
        "category": "general",
        "name": {"ru": "Неделя с ботом", "en": "Week with the bot"},
        "description": {
            "ru": "Заходить в бота 7 дней подряд.",
            "en": "Open the bot 7 days in a row.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 350},
    },
    "general_login_streak_30": {
        "category": "general",
        "name": {"ru": "Месяц с ботом", "en": "Month with the bot"},
        "description": {
            "ru": "Заходить в бота 30 дней подряд.",
            "en": "Open the bot 30 days in a row.",
        },
        "reward": {"xp": 1500, "crystals": 10, "coins": 2000},
    },
    "general_login_streak_100": {
        "category": "general",
        "name": {"ru": "100 дней подряд", "en": "100-day streak"},
        "description": {
            "ru": "Заходить в бота 100 дней подряд.",
            "en": "Open the bot 100 days in a row.",
        },
        "reward": {"xp": 4000, "crystals": 25, "coins": 6000},
    },
    "general_gifts_sent_10": {
        "category": "general",
        "name": {"ru": "Щедрая душа", "en": "Generous soul"},
        "description": {
            "ru": "Отправить 10 подарков другим игрокам.",
            "en": "Send 10 gifts to other players.",
        },
        "reward": {"xp": 500, "crystals": 3, "coins": 700},
    },
    "general_gifts_received_10": {
        "category": "general",
        "name": {"ru": "Всеобщий любимец", "en": "Everyone's favorite"},
        "description": {
            "ru": "Получить 10 подарков от других игроков.",
            "en": "Receive 10 gifts from other players.",
        },
        "reward": {"xp": 400, "crystals": 2, "coins": 500},
    },
    "general_donate_total_1000": {
        "category": "general",
        "name": {"ru": "Крупный вклад", "en": "Big contributor"},
        "description": {
            "ru": "Суммарно потратить 1000 кристаллов.",
            "en": "Spend 1000 crystals in total.",
        },
        "reward": {"xp": 800, "crystals": 0, "coins": 1500},
    },
    "general_daily_gift_limit": {
        "category": "general",
        "name": {"ru": "Дневной максимум", "en": "Daily maxed out"},
        "description": {
            "ru": "Исчерпать дневной лимит подарков (себе или другу) за день.",
            "en": "Hit the daily gift limit (for yourself or a friend) in a single day.",
        },
        "reward": {"xp": 300, "crystals": 2, "coins": 400},
    },
    "general_rare_gift": {
        "category": "general",
        "name": {"ru": "Редкий подарок", "en": "Rare gift"},
        "description": {
            "ru": "Купить или отправить редкий подарок (кит, жемчужина и другие дорогие подарки).",
            "en": "Buy or send a rare gift (whale, pearl, and other high-tier gifts).",
        },
        "reward": {"xp": 600, "crystals": 4, "coins": 800},
    },
    "general_all_gifts": {
        "category": "general",
        "name": {"ru": "Полная коллекция", "en": "Full collection"},
        "description": {
            "ru": "Купить или отправить каждый из 15 видов подарков хотя бы раз.",
            "en": "Buy or send every one of the 15 gift types at least once.",
        },
        "reward": {"xp": 2000, "crystals": 15, "coins": 3000},
    },
    "general_coins_earned_10000": {
        "category": "general",
        "name": {"ru": "Богач", "en": "Wealthy"},
        "description": {
            "ru": "Заработать суммарно 10 000 монет.",
            "en": "Earn 10,000 coins in total.",
        },
        "reward": {"xp": 500, "crystals": 3, "coins": 0},
    },
    "general_anniversary": {
        "category": "general",
        "name": {"ru": "С днём рождения аккаунта!", "en": "Account anniversary"},
        "description": {
            "ru": "Играть в боте ровно год со дня регистрации.",
            "en": "Play the bot for exactly one year since registration.",
        },
        "reward": {"xp": 3000, "crystals": 20, "coins": 4000},
    },
}

# Порядок показа / листания внутри раздела — отдельно от ключей
# словаря (dict сохраняет порядок вставки, но явный список надёжнее,
# если ACHIEVEMENTS позже будет пополняться не по порядку).
ACHIEVEMENT_ORDER = [
    # --- панда ---
    "panda_full_stats",
    "first_feed",
    "panda_first_pet",
    "panda_pet_10",
    "panda_pet_100",
    "panda_feed_10",
    "panda_feed_100",
    "panda_feed_500",
    "panda_mood_100",
    "panda_hunger_100",
    "panda_friendship_100",
    "panda_streak_7",
    "panda_streak_30",
    "panda_streak_100",
    "panda_never_hungry_week",
    "panda_skin",
    "panda_skins_3",
    "panda_skins_5",
    "panda_skins_all",
    "panda_named",
    "panda_birthday",
    # --- огород ---
    "first_harvest",
    "garden_first_plant",
    "garden_harvest_10",
    "garden_harvest_100",
    "garden_harvest_1000",
    "garden_all_plots_full",
    "garden_all_plots_ripe",
    "garden_all_crops",
    "garden_one_crop_20",
    "garden_streak_7",
    "garden_streak_30",
    "garden_pineapple_first",
    "garden_bamboo_100",
    "garden_basket_50",
    "garden_feed_from_basket",
    "garden_sell_50",
    "garden_instant_sell",
    "garden_harvest_night",
    "garden_harvest_10000",
    # --- пекарня ---
    "first_bake",
    "bakery_taster",
    "bakery_all_recipes",
    "bakery_royal_table",
    "bakery_brownie_master",
    "bakery_classic_lover",
    "bakery_baked_50",
    "bakery_baked_250",
    "bakery_baked_1000",
    "bakery_both_ovens",
    "bakery_no_idle_7",
    "bakery_first_purchase",
    "bakery_bulk_50",
    "bakery_chocolate_100",
    "bakery_full_ingredients",
    "bakery_big_spender_10000",
    "bakery_showcase_20",
    "bakery_market_sell_20",
    "bakery_market_earn_5000",
    "bakery_feed_panda_20",
    "bakery_feed_cake_10",
    "bakery_garden_to_oven",
    "bakery_night_shift",
    # --- рынок ---
    "first_listing",
    "market_first_purchase",
    "market_buy_50",
    "market_buy_250",
    "market_spent_10000",
    "market_sold_100",
    "market_earned_20000",
    "market_full_shelves",
    "market_cancel_listing",
    "market_big_deal",
    "market_loyal_customer",
    "market_own_clientele",
    "market_diverse_trader",
    "market_max_price",
    "market_min_price",
    "market_spend_it_all",
    # --- общее ---
    "level_10",
    "gift_giver",
    "first_donate",
    "level_25",
    "general_level_5",
    "general_level_15",
    "general_level_20",
    "general_level_30",
    "general_level_40",
    "general_level_50",
    "general_xp_10000",
    "general_login_streak_7",
    "general_login_streak_30",
    "general_login_streak_100",
    "general_gifts_sent_10",
    "general_gifts_received_10",
    "general_donate_total_1000",
    "general_daily_gift_limit",
    "general_rare_gift",
    "general_all_gifts",
    "general_coins_earned_10000",
    "general_anniversary",
]


def _achievements_in_category(cat_id: str) -> list[str]:
    """id ачивок раздела cat_id в порядке ACHIEVEMENT_ORDER."""
    return [a for a in ACHIEVEMENT_ORDER if ACHIEVEMENTS[a]["category"] == cat_id]


# ==========================
#   ВЫДАЧА / ХРАНЕНИЕ
# ==========================

async def get_unlocked_map(user_id: int) -> dict[str, float]:
    """achv_id -> unix-время открытия, для всех открытых ачивок игрока."""
    db = await database.get_db()
    async with db.execute(
        "SELECT achv_id, unlocked_at FROM user_achievements WHERE user_id = ?", (user_id,)
    ) as cursor:
        return {row["achv_id"]: row["unlocked_at"] async for row in cursor}


async def get_unlocked(user_id: int) -> set[str]:
    """Множество id уже открытых ачивок игрока."""
    return set((await get_unlocked_map(user_id)).keys())


async def get_total_unlocks_count() -> int:
    """Сколько раз всего были открыты ачивки — все игроки, все ачивки,
    каждое открытие = +1 (для шапки списка категорий)."""
    db = await database.get_db()
    async with db.execute("SELECT COUNT(*) AS cnt FROM user_achievements") as cursor:
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


async def get_completions_count(achv_id: str) -> int:
    """Сколько РАЗНЫХ игроков открыли конкретную ачивку achv_id."""
    db = await database.get_db()
    async with db.execute(
        "SELECT COUNT(DISTINCT user_id) AS cnt FROM user_achievements WHERE achv_id = ?",
        (achv_id,),
    ) as cursor:
        row = await cursor.fetchone()
        return row["cnt"] if row else 0


async def unlock(user_id: int, achv_id: str) -> dict | None:
    """Выдаёт ачивку achv_id игроку, если она ещё не открыта: начисляет
    награду и помечает как открытую. Возвращает {"achv_id":, "achv":,
    "level_info":} при успешной выдаче (level_info — результат
    prof.add_xp, пригодится вызывающему для строки про левелап), либо
    None, если ачивка уже была открыта раньше — вызывать безопасно на
    каждый триггер, без предварительной проверки "уже есть или нет"."""
    achv = ACHIEVEMENTS[achv_id]

    db = await database.get_db()
    cursor = await db.execute(
        "INSERT OR IGNORE INTO user_achievements (user_id, achv_id, unlocked_at) VALUES (?, ?, ?)",
        (user_id, achv_id, time.time()),
    )
    await database.commit()
    if cursor.rowcount == 0:
        return None  # уже была открыта раньше

    reward = achv["reward"]
    level_info = None
    if reward["xp"]:
        level_info = await prof.add_xp(user_id, reward["xp"])
    if reward["crystals"]:
        await prof.add_crystals(user_id, reward["crystals"])
    if reward["coins"]:
        await shop.add_balance(user_id, reward["coins"])
    # Экономическая операция (начислена награда) — сохраняем немедленно,
    # как и остальные такие операции в боте.
    await database.flush()

    return {"achv_id": achv_id, "achv": achv, "level_info": level_info}


# ==========================
#   ТЕКСТЫ
# ==========================

def format_unlock_text(lang: str, result: dict) -> str:
    """Короткий блок уведомления об открытой ачивке (кастомный эмодзи
    вместо 🏆 перед заголовком, свой — перед строкой награды, см.
    _UNLOCK_HEADER/REWARD_LINE_EMOJI_ID). Шлётся ВСЕГДА отдельным
    сообщением (свой .answer()/.send_message()), а не приклеивается к тексту другого уведомления (сбор урожая, готовая
    выпечка, левелап и т.п.) — так игрок видит открытие ачивки как
    отдельное, заметное событие, даже если оно произошло попутно с
    чем-то ещё. Не включает строку про левелап — если она нужна,
    вызывающий код сам добавляет prof.level_up_notice() ОТДЕЛЬНОЙ
    строкой/сообщением, как и в остальном боте."""
    achv = result["achv"]
    reward_line = _reward_line(lang, achv["reward"])

    header = _UNLOCK_HEADER[lang].format(name=achv["name"][lang])
    reward_ce = _ce(REWARD_LINE_EMOJI_ID, "🍭")
    return f"<blockquote>{header}</blockquote>\n{reward_ce} <i>{reward_line}</i>"


def _ce(emoji_id: str, glyph: str) -> str:
    """<tg-emoji> для текста сообщения (в отличие от кнопок — там нужен
    отдельный параметр icon_custom_emoji_id, см. тот же приём в
    prof.py/donate.py)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'


# Кастомные эмодзи для карточки ачивки (_achv_page_text) — везде через
# <tg-emoji>, т.к. это обычный текст сообщения (в отличие от кнопок ниже).
STATUS_EMOJI_ID = "5262844652964303985"        # 💡 — перед меткой "Статус"
DONE_EMOJI_ID = "5390846416530716134"          # ✅ — статус "выполнено"
NOT_DONE_EMOJI_ID = "5328093774650293878"      # 🌟 — статус "не выполнено"
COMPLETIONS_EMOJI_ID = "5891207662678317861"   # 👤 — перед "Выполнили: N игроков"
DESCRIPTION_EMOJI_ID = "5334544901428229844"   # ℹ️ — перед описанием условия
REWARD_EMOJI_ID = "5411383738959405731"        # 🎁 — перед строкой награды

# Кастомные эмодзи для кнопок пейджера/раздела (_pager_keyboard) — тут
# ТОЛЬКО через icon_custom_emoji_id, тег <tg-emoji> в тексте кнопки не
# рендерится (см. тот же нюанс в prof.py/donate.py/panda.py).
BACK_TO_CATEGORIES_EMOJI_ID = "5258236805890710909"  # ⬅️ — кнопка "К разделам"
PAGE_NEXT_EMOJI_ID = "5253767677670862169"           # 🔜 — вперёд (тот же id, что и в prof.py)
PAGE_PREV_EMOJI_ID = "5255703720078879038"           # 🔙 — назад (тот же id, что и в prof.py)


# Кастомный эмодзи вместо 🏆 перед заголовком уведомления об открытой
# ачивке, и отдельный — перед строкой награды (см. format_unlock_text/
# _UNLOCK_HEADER выше).
UNLOCK_HEADER_EMOJI_ID = "5150415989841593609"
REWARD_LINE_EMOJI_ID = "5339463150083271333"

# Заголовок уведомления об открытой ачивке — {name} подставляется
# название конкретной ачивки; заголовок целиком жирным и в blockquote,
# строка награды под ним — целиком курсивом (см. format_unlock_text выше).
_UNLOCK_HEADER = {
    "ru": f'{_ce(UNLOCK_HEADER_EMOJI_ID, "🎖")} <b>Открыто новое достижение — «{{name}}»!</b>',
    "en": f'{_ce(UNLOCK_HEADER_EMOJI_ID, "🎖")} <b>New achievement unlocked — “{{name}}”!</b>',
}


def _reward_line(lang: str, reward: dict) -> str:
    """"Награда: 500 опыта | 5 💎 | 250 монет" — с явным префиксом-меткой
    и разделителем "|" между значениями (одно значение — без "|")."""
    bits = []
    if reward["xp"]:
        bits.append(f"{reward['xp']} XP" if lang == "en" else f"{reward['xp']} опыта")
    if reward["crystals"]:
        bits.append(f"{reward['crystals']} {prof.CE_CRYSTAL}")
    if reward["coins"]:
        bits.append(f"{reward['coins']} {shop.CURRENCY}")
    label = "Reward" if lang == "en" else "Награда"
    return f"{label}: " + " | ".join(bits)


def _progress_bar(
    done: bool, length: int = 10, progress: tuple[int, int] | None = None
) -> str:
    """Шкала показателя выполнения ачивки. Если для ачивки нет
    зарегистрированного провайдера прогресса (см. PROGRESS_PROVIDERS),
    achives.py по-прежнему знает только бинарный факт "открыта / не
    открыта" — шкала тогда полностью пустая или полностью заполненная.
    Если провайдер есть — progress = (текущее, цель), и шкала/процент
    отражают реальную долю выполнения."""
    if done:
        filled, percent = length, 100
    elif progress is not None:
        current, target = progress
        percent = int(current / target * 100) if target else 0
        percent = max(0, min(100, percent))
        filled = round(length * percent / 100)
    else:
        filled, percent = 0, 0
    bar = "▰" * filled + "▱" * (length - filled)
    return f"{bar} {percent}%"


def _categories_text(lang: str, unlocked: set[str], total_unlocks: int) -> str:
    header = (
        f"{_ce(UNLOCK_HEADER_EMOJI_ID, '🎖')} <b>Достижения</b>"
        if lang == "ru"
        else f"{_ce(UNLOCK_HEADER_EMOJI_ID, '🎖')} <b>Achievements</b>"
    )
    total = len(ACHIEVEMENTS)
    done = len(unlocked)
    sub = (
        f"{_ce(STATUS_EMOJI_ID, '💡')} Открыто: {done}/{total}"
        if lang == "ru"
        else f"{_ce(STATUS_EMOJI_ID, '💡')} Unlocked: {done}/{total}"
    )
    total_line = (
        f"{_ce(DONE_EMOJI_ID, '✅')} Всего выполнено ачивок игроками: {total_unlocks}"
        if lang == "ru"
        else f"{_ce(DONE_EMOJI_ID, '✅')} Total achievements completed by all players: {total_unlocks}"
    )
    hint = "Выберите раздел:" if lang == "ru" else "Choose a category:"
    return f"{header}\n<b>{sub}</b>\n<b>{total_line}</b>\n\n<b>{hint}</b>"


def _achv_page_text(
    lang: str,
    achv_id: str,
    unlocked_map: dict[str, float],
    completions: int,
    progress: tuple[int, int] | None,
) -> str:
    achv = ACHIEVEMENTS[achv_id]
    cat_title = f"{_category_icon_text(achv['category'])} {CATEGORIES[achv['category']][lang]}"
    reward_line = _reward_line(lang, achv["reward"])

    is_done = achv_id in unlocked_map
    status_label = "Статус" if lang == "ru" else "Status"
    if is_done:
        date_str = datetime.fromtimestamp(unlocked_map[achv_id]).strftime("%d.%m.%Y")
        status_value = (
            f"{_ce(DONE_EMOJI_ID, '✅')} Выполнено · {date_str}"
            if lang == "ru"
            else f"{_ce(DONE_EMOJI_ID, '✅')} Completed · {date_str}"
        )
    else:
        status_value = (
            f"{_ce(NOT_DONE_EMOJI_ID, '🌟')} Не выполнено"
            if lang == "ru"
            else f"{_ce(NOT_DONE_EMOJI_ID, '🌟')} Not completed"
        )
    status = f"{_ce(STATUS_EMOJI_ID, '💡')} {status_label}: {status_value}"

    completions_line = (
        f"{_ce(COMPLETIONS_EMOJI_ID, '👤')} Выполнили: {completions} игроков"
        if lang == "ru"
        else f"{_ce(COMPLETIONS_EMOJI_ID, '👤')} Completed by: {completions} players"
    )

    lines = [
        f"<b>{cat_title}</b>",
        f"<b>{achv['name'][lang]}</b>",
        "",
        f"{_ce(DESCRIPTION_EMOJI_ID, 'ℹ️')} <i>{achv['description'][lang]}</i>",
        "",
        f"<b>{_ce(REWARD_EMOJI_ID, '🎁')} {reward_line}</b>",
        "",
        f"<b>{status}</b>",
    ]
    # Шкала/процент/"X из Y" — только там, где для ачивки зарегистрирован
    # провайдер прогресса (см. PROGRESS_PROVIDERS), т.е. это реально
    # многошаговый счётчик (10 кормлений, стрик и т.п.). Для ачивок без
    # промежуточных шагов (разовое действие вроде "погладить первый
    # раз", "все три шкалы разом на 100%") шкала не имеет смысла —
    # там только статус выполнено/не выполнено, без 0%/100%.
    if progress is not None:
        bar_line = _progress_bar(is_done, progress=progress)
        lines.append(f"<b>{bar_line}</b>")
        if not is_done:
            current, target = progress
            progress_label = "Прогресс" if lang == "ru" else "Progress"
            lines.append(f"<b>{progress_label}: {current}/{target}</b>")
    lines += [
        "",
        f"<b>{completions_line}</b>",
    ]
    return "\n".join(lines)


# ==========================
#   КЛАВИАТУРЫ
# ==========================

_BACK_TO_CATEGORIES_TEXT = {"ru": "К разделам", "en": "To categories"}


def _categories_keyboard(lang: str, unlocked: set[str]) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for cat_id in CATEGORY_ORDER:
        achv_ids = _achievements_in_category(cat_id)
        done = sum(1 for a in achv_ids if a in unlocked)
        title = CATEGORIES[cat_id][lang]
        emoji_id = CATEGORY_EMOJI_ID.get(cat_id)
        if emoji_id:
            # Кастомный тг-эмодзи — только через icon_custom_emoji_id,
            # без глифа в тексте (тег <tg-emoji> в кнопках не рендерится).
            kb.button(
                text=f"{title} {done}/{len(achv_ids)}",
                callback_data=f"ach:cat:{cat_id}",
                icon_custom_emoji_id=emoji_id,
            )
        else:
            # "Пекарня" — обычный юникодный эмодзи, кастомного id нет,
            # его можно просто вставить прямо в текст кнопки.
            glyph = CATEGORY_FALLBACK_EMOJI[cat_id]
            kb.button(
                text=f"{glyph} {title} {done}/{len(achv_ids)}",
                callback_data=f"ach:cat:{cat_id}",
            )
    # По две кнопки в ряд, последняя (5-я) — отдельным рядом одна.
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def _pager_keyboard(lang: str, cat_id: str, idx: int, total: int) -> InlineKeyboardMarkup:
    prev_idx = max(0, idx - 1)
    next_idx = min(total - 1, idx + 1)

    kb = InlineKeyboardBuilder()
    kb.button(
        text=" ",
        callback_data=f"ach:catpage:{cat_id}:{prev_idx}",
        icon_custom_emoji_id=PAGE_PREV_EMOJI_ID,
    )
    kb.button(text=f"{idx + 1}/{total}", callback_data="ach:noop")
    kb.button(
        text=" ",
        callback_data=f"ach:catpage:{cat_id}:{next_idx}",
        icon_custom_emoji_id=PAGE_NEXT_EMOJI_ID,
    )
    kb.button(
        text=_BACK_TO_CATEGORIES_TEXT[lang],
        callback_data="ach:cats",
        icon_custom_emoji_id=BACK_TO_CATEGORIES_EMOJI_ID,
    )
    kb.adjust(3, 1)
    return kb.as_markup()


# ==========================
#   МЕНЮ
# ==========================

BUTTON_TEXT = {
    "ru": "Достижения",
    "en": "Achievements",
}
# Без юникодного 🏆 в тексте — иконка теперь кастомный тг-эмодзи
# (UNLOCK_HEADER_EMOJI_ID / 🎖) на самой кнопке, через icon_custom_emoji_id
# в main.py (main_menu_keyboard). Текст ДОЛЖЕН совпадать буквально с
# main.TEXTS[lang]["menu_achievements"] — фильтр F.text.in_(BUTTON_TEXT.values())
# ниже матчит именно текст сообщения, а icon_custom_emoji_id в него не входит.


async def _get_lang(state: FSMContext, user_id: int) -> str:
    data = await state.get_data()
    lang = data.get("lang")
    if lang:
        return lang

    onboarding = await database.get_onboarding(user_id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"
    await state.update_data(lang=lang)
    return lang


async def _render_categories(user_id: int, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    unlocked = await get_unlocked(user_id)
    total_unlocks = await get_total_unlocks_count()
    text = _categories_text(lang, unlocked, total_unlocks)
    keyboard = _categories_keyboard(lang, unlocked)
    return text, keyboard


async def _render_category_page(
    user_id: int, lang: str, cat_id: str, idx: int
) -> tuple[str, InlineKeyboardMarkup]:
    achv_ids = _achievements_in_category(cat_id)
    idx = max(0, min(idx, len(achv_ids) - 1))
    achv_id = achv_ids[idx]

    unlocked_map = await get_unlocked_map(user_id)
    completions = await get_completions_count(achv_id)
    progress = await _get_progress(achv_id, user_id)

    text = _achv_page_text(lang, achv_id, unlocked_map, completions, progress)
    keyboard = _pager_keyboard(lang, cat_id, idx, len(achv_ids))
    return text, keyboard


@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_achievements(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    text, keyboard = await _render_categories(message.from_user.id, lang)

    # Картинка раздела (см. admin.py: admin:sections, ключ "achievements")
    # — если задана, экран отправляется как фото с текстом в подписи,
    # иначе как обычно текстом. Локальный импорт — admin.py сам
    # импортирует achives.py на верхнем уровне (цикл).
    import admin

    await admin.send_with_section_image(message, "achievements", text, reply_markup=keyboard)


@router.callback_query(F.data == "ach:cats")
async def show_categories(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text, keyboard = await _render_categories(callback.from_user.id, lang)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ach:cat:"))
async def open_category(callback: CallbackQuery, state: FSMContext) -> None:
    cat_id = callback.data.split(":", 2)[2]
    lang = await _get_lang(state, callback.from_user.id)
    text, keyboard = await _render_category_page(callback.from_user.id, lang, cat_id, 0)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data.startswith("ach:catpage:"))
async def show_category_page(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, cat_id, idx_str = callback.data.split(":")
    lang = await _get_lang(state, callback.from_user.id)
    text, keyboard = await _render_category_page(
        callback.from_user.id, lang, cat_id, int(idx_str)
    )

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "ach:noop")
async def noop(callback: CallbackQuery) -> None:
    """Кнопка-индикатор страницы ("2/3") — не листает, просто гасит
    часики у клиента."""
    await callback.answer()
