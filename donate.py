"""
Раздел "Донаты".

Показывает игроку меню из двух категорий:
    - "Привилегии" — пока пустая заглушка (готовим на будущее);
    - "Кристаллы" — покупка премиальной валюты бота (prof.py: get_crystals/
      add_crystals — тот же баланс, что показывается в профиле и тратится
      там на подарки, никакого отдельного счёта здесь не заводим) за
      Telegram Stars.

Кристаллы:
    6 готовых пакетов (49 / 129 / 259 / 589 / 1249 / 2499 кристаллов) плюс
    отдельная большая кнопка "Ввести своё количество" — вручную от 1 до
    100000 кристаллов. Курс везде фиксированный и простой: 1 звезда = 1
    кристалл (см. CRYSTAL_PACKAGES / CUSTOM_MIN / CUSTOM_MAX ниже).

Оплата — через встроенные Telegram Stars (валюта "XTR" в Bot API), ЧЕРЕЗ
ССЫЛКУ НА ИНВОЙС (createInvoiceLink), а не прямой отправкой инвойса
(sendInvoice/answerInvoice):
    - bot.create_invoice_link(...) генерирует обычную https-ссылку на
      оплату — Telegram открывает по ней тот же нативный экран оплаты
      Stars, что и при отправленном инвойсе, но без отдельного
      сообщения-инвойса в чате;
    - вместо этого мы САМИ обновляем (edit_text) уже показанное
      игроку сообщение — заменяем список пакетов/запрос суммы на
      текст с итогом покупки и кнопкой-ссылкой "Оплатить" (см.
      _pay_keyboard) — это и есть "обновление сообщения через линк
      инвойс", а не заготовленный aiogram-метод, который в проде не
      срабатывал;
    - pre_checkout_query подтверждается всегда сразу (см. on_pre_checkout) —
      это цифровой товар без внешних ограничений (наличие на складе и
      т.п.), отклонять нечего;
    - после успешной оплаты Telegram присылает обычное сообщение с
      message.successful_payment (независимо от того, каким способом
      был создан инвойс — прямой отправкой или по ссылке) — из его
      invoice_payload достаём количество кристаллов и начисляем через
      prof.add_crystals() (сам счёт кристаллов и его защита от гонок
      уже реализованы в prof.py — это тот же баланс, что и в разделе
      "Профиль"/подарки, здесь просто переиспользуем).

Кастомные эмодзи на инлайн-кнопках:
    aiogram/Telegram НЕ рендерит HTML-теги (<tg-emoji>) внутри текста
    инлайн-кнопки — кнопки вообще не поддерживают HTML/entities в
    тексте (тот же нюанс уже отмечен в prof.py и panda.py). Единственный
    способ показать кастомный эмодзи именно НА кнопке — отдельный
    параметр InlineKeyboardButton(icon_custom_emoji_id=...). Поэтому
    везде ниже, где нужен кастомный эмодзи на кнопке, он передаётся
    через builder.button(..., icon_custom_emoji_id=...), а не вставляется
    в текст кнопки.

Подключение в main.py:
    import donate
    dp.include_router(donate.router)   # порядок относительно других не важен

Зависимость:
    pip install aiogram --break-system-packages   # Stars нужен Bot API >= 7.4
"""

import asyncio
import logging
import time
from datetime import datetime

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, LabeledPrice, Message, PreCheckoutQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

import crypto_pay
import database
import prof
import achives
import shop
import panda

logger = logging.getLogger(__name__)

router = Router(name="donate")


# ==========================
#   СОСТОЯНИЯ (FSM)
# ==========================

class DonateStates(StatesGroup):
    waiting_custom_amount = State()
    waiting_custom_coins_amount = State()


# ==========================
#   КАСТОМНЫЕ ЭМОДЗИ
# ==========================
#
# CRYSTAL_EMOJI_ID — тот же кристалл, что и везде в боте (panda.py:
# CRYSTAL_EMOJI): премиальная валюта одна на весь бот, отдельный id под
# неё здесь не заводим, просто дублируем константу (по аналогии с тем,
# как panda.py дублирует у себя главное меню main.py — см. комментарий
# там же). Используется и в тексте сообщений (тег <tg-emoji>, там
# рендерится нормально), и на кнопках (через icon_custom_emoji_id).
CRYSTAL_EMOJI_ID = "5251273203615031474"
CRYSTAL_EMOJI = f'<tg-emoji emoji-id="{CRYSTAL_EMOJI_ID}">🎁</tg-emoji>'

# Монета — для кнопок раздела "Монеты" (в тексте сообщений там уже
# используется shop.CE_BALANCE — свой кастомный эмодзи монеты из shop.py,
# здесь та же монета, но отдельным id для кнопок, т.к. кнопки эмодзи из
# тега <tg-emoji> не рендерят — см. шапку файла).
COIN_EMOJI_ID = "5449418135381759397"

# Маски — тот же эмодзи, что уже используется в main.py/panda.py на
# кнопке реплай-меню "Облики" (LOOKS_EMOJI_ID/_MAIN_LOOKS_EMOJI_ID) —
# переиспользуем его же и здесь для кнопки "Привилегии", раз тематика
# та же ("статусы"/"роли").
PRIVILEGES_EMOJI_ID = "6032625495328165724"

# Стрелка "назад" — тот же id, что и в panda.py (SKINS_MENU_BACK_EMOJI_ID).
BACK_EMOJI_ID = "5255703720078879038"

# Крестик "отменить" — тот же id, что и в panda.py (SKIN_UNEQUIP_EMOJI_ID).
CANCEL_EMOJI_ID = "5210952531676504517"

# Звезда для строки "К оплате" на экране оплаты (pay_prompt) — только
# В ТЕКСТЕ (тег <tg-emoji>), кнопки HTML не рендерят, см. шапку файла.
PAY_STAR_EMOJI_ID = "5798819377088307477"
PAY_STAR_EMOJI = f'<tg-emoji emoji-id="{PAY_STAR_EMOJI_ID}">⭐️</tg-emoji>'

# Звезда для кнопки "Мои звёзды" — на кнопках эмодзи вешается только через
# icon_custom_emoji_id, тег в тексте кнопки не работает (см. выше).
MY_STARS_EMOJI_ID = "5267500801240092311"

# Обмен ("Курс") — кастомный эмодзи 💱 в тексте (курс упоминается и на
# экране запроса ручного количества — там тот же эмодзи).
RATE_EMOJI_ID = "5402186569006210455"
RATE_EMOJI = f'<tg-emoji emoji-id="{RATE_EMOJI_ID}">💱</tg-emoji>'

# Эмодзи для кнопок способов оплаты (экран "Выберите способ оплаты" —
# _method_keyboard ниже): 💵 для CryptoBot, 🚀 для xRocket. Для Stars
# переиспользуем PAY_STAR_EMOJI_ID (объявлен ниже) — та же звезда, что и
# в тексте "К оплате" на экране Stars.
METHOD_CRYPTOBOT_EMOJI_ID = "5798650400189980129"
METHOD_XROCKET_EMOJI_ID = "5798534328698805312"

# Кастомные эмодзи для экрана оплаты CryptoBot/xRocket (_crypto_pay_keyboard/
# pay_prompt_crypto_*) — только для этих двух способов, Stars их не
# используют (там уже свои PAY_STAR_EMOJI/MY_STARS_EMOJI_ID/CRYSTAL_EMOJI_ID).
#   - CRYPTO_PAY_TEXT_EMOJI_ID — 💵 в тексте "К оплате: $..." (тег
#     <tg-emoji>, в тексте рендерится нормально, см. шапку файла).
#   - CRYPTO_CHECK_BUTTON_EMOJI_ID — ⌛ на кнопке "Проверить оплату"
#     (через icon_custom_emoji_id — кнопки HTML/entities не поддерживают).
#   - CRYPTO_PAY_BUTTON_EMOJI_ID — 🔗 на кнопке-ссылке "Оплатить"
#     (через icon_custom_emoji_id, аналогично).
CRYPTO_PAY_TEXT_EMOJI_ID = "5255981634527704754"
CRYPTO_PAY_TEXT_EMOJI = f'<tg-emoji emoji-id="{CRYPTO_PAY_TEXT_EMOJI_ID}">💵</tg-emoji>'
CRYPTO_CHECK_BUTTON_EMOJI_ID = "5386367538735104399"
CRYPTO_PAY_BUTTON_EMOJI_ID = "5271604874419647061"

# Перо ✍️ — на кнопке "Ввести своё количество" (icon_custom_emoji_id, как и
# остальные кнопочные эмодзи выше), и та же иконка в тексте (тег
# <tg-emoji>) на экране запроса суммы (custom_ask) — та же тема.
CUSTOM_AMOUNT_EMOJI_ID = "5197269100878907942"
CUSTOM_AMOUNT_EMOJI = f'<tg-emoji emoji-id="{CUSTOM_AMOUNT_EMOJI_ID}">✍️</tg-emoji>'


# ==========================
#   ПРИВИЛЕГИИ
# ==========================
#
# 3 фиксированных уровня подписки на PRIVILEGE_DURATION_DAYS дней,
# покупаются за кристаллы (та же премиальная валюта, что и облики панды,
# см. panda.py: buy_skin). Повторная покупка (в т.ч. другого уровня)
# перезаписывает текущую привилегию и продлевает срок на
# PRIVILEGE_DURATION_DAYS дней заново от момента покупки (см. buy_privilege).
#
# ВАЖНО: здесь только каталог тарифов, тексты и сама покупка/хранение
# активной привилегии (user_privileges в database.py). Применение самих
# эффектов подключено в соответствующих модулях: ускорение роста —
# garden.py/bakery.py (plant_crop/start_baking, через
# _privilege_speedup_offset — "задним числом" сдвигают planted_at/
# started_at), увеличенный лимит на дарение/получение подарков —
# prof.py (_effective_daily_gift_limit, вызывается из _reserve_daily/
# _is_daily_limit_maxed), бонус к опыту — prof.py
# (_apply_privilege_xp_bonus, внутри add_xp — общей точки входа для
# опыта из сада/пекарни/ачивок). Бонусный скин 3-го уровня — это тот же
# "sun" ("Солнечная панда") из panda.py:SKINS, что продаётся и за монеты;
# выдаётся panda.grant_skin_free() уже ПОСЛЕ выхода из user_lock внутри
# buy_privilege (свой user_lock не реентерабелен, см. panda.py:buy_skin) —
# и НАВСЕГДА, независимо от истечения самой привилегии.
PRIVILEGE_DURATION_DAYS = 30

# Медали — как и остальные декоративные эмодзи на инлайн-кнопках можно
# было бы вешать через icon_custom_emoji_id, но здесь это самые обычные
# юникод-эмодзи прямо в тексте кнопки — Telegram их отображает без
# ограничений, в отличие от кастомных <tg-emoji>.
PRIVILEGE_TIERS: list[dict] = [
    {
        "id": "panda_plus",
        "name": "Panda Plus",
        "medal": "🥉",
        "price": 69,
        "speedup_percent": 10,
        "gift_limit": 15_000,
        "exp_bonus_percent": 0,
        "bonus_skin": None,
    },
    {
        "id": "panda_vip",
        "name": "Panda VIP",
        "medal": "🥈",
        "price": 149,
        "speedup_percent": 15,
        "gift_limit": 25_000,
        "exp_bonus_percent": 20,
        "bonus_skin": None,
    },
    {
        "id": "panda_premium",
        "name": "Panda Premium",
        "medal": "🥇",
        "price": 289,
        "speedup_percent": 30,
        "gift_limit": 50_000,
        "exp_bonus_percent": 30,
        "bonus_skin": "sun",  # id из panda.py:SKINS ("Солнечная панда")
    },
]

PRIVILEGE_TIERS_BY_ID: dict[str, dict] = {tier["id"]: tier for tier in PRIVILEGE_TIERS}


# ==========================
#   ПАКЕТЫ КРИСТАЛЛОВ
# ==========================
#
# Курс везде фиксированный: 1 звезда = 1 кристалл — стоимость пакета в
# звёздах всегда равна количеству кристаллов в нём, поэтому "stars"
# отдельно не хранится, а считается от "crystals" (см. _stars_for).

CRYSTAL_PACKAGES: list[int] = [49, 129, 259, 589, 1249, 2499]

# Диапазон для кнопки "Ввести своё количество".
CUSTOM_MIN = 1
CUSTOM_MAX = 100_000


def _stars_for(crystals: int) -> int:
    """Курс обмена: 1 звезда = 1 кристалл."""
    return crystals


# ==========================
#   ПАКЕТЫ МОНЕТ
# ==========================
#
# Та же цена в звёздах, что и у кристаллов (пакеты один в один совпадают
# по стоимости — 49/129/259/589/1249/2499 ⭐), но начисляется в 10 раз
# больше самой валюты: 1 звезда = 10 монет (см. _stars_for_coins). Отсюда
# и пакеты — просто CRYSTAL_PACKAGES, умноженные на 10 (49 → 490 монет за
# те же 49 ⭐, и т.д.).
COIN_PACKAGES: list[int] = [c * 10 for c in CRYSTAL_PACKAGES]

COIN_MULTIPLIER = 10

# Диапазон для кнопки "Ввести своё количество" — тоже ×10 от кристального.
CUSTOM_COINS_MIN = CUSTOM_MIN * COIN_MULTIPLIER
CUSTOM_COINS_MAX = CUSTOM_MAX * COIN_MULTIPLIER


def _stars_for_coins(coins: int) -> int:
    """Курс обмена монет: 1 звезда = 10 монет. Вызывающий код (custom-ввод)
    сам следит, что coins кратно COIN_MULTIPLIER — иначе целого числа
    звёзд не получится (см. on_custom_coin_amount_received)."""
    return coins // COIN_MULTIPLIER


# ==========================
#   СПОСОБЫ ОПЛАТЫ
# ==========================
#
# Помимо Telegram Stars, донат теперь можно оплатить через CryptoBot и
# xRocket (криптовалютой) — см. crypto_pay.py. У обоих курс переводится
# из звёзд в доллары по единому фиксированному курсу: 100 кристаллов =
# 100 звёзд = $1.3, т.е. 1 звезда = $0.013 (примерно соответствует
# реальному курсу Telegram Stars). Таким образом ценник в любом способе
# оплаты считается от одного и того же количества звёзд — см.
# _stars_for/_stars_for_coins выше.
STAR_USD_RATE = 0.013


def _usd_for_stars(stars: int) -> float:
    """Сумма в USD для инвойса CryptoBot/xRocket, соответствующая
    указанному количеству звёзд по курсу STAR_USD_RATE. Минимум 0.01 —
    некоторые провайдеры не принимают нулевые/отрицательные инвойсы."""
    return max(0.01, round(stars * STAR_USD_RATE, 2))


# Порядок здесь = порядок кнопок на экране выбора способа оплаты.
PAYMENT_METHODS: list[str] = ["stars", "cryptobot", "xrocket"]


def _method_available(method: str) -> bool:
    if method == "stars":
        return True
    if method == "cryptobot":
        return crypto_pay.is_cryptobot_enabled()
    if method == "xrocket":
        return crypto_pay.is_xrocket_enabled()
    return False


# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

# Текст кнопки реплай-меню "Донаты" — БЕЗ эмодзи в самом тексте: кастомный
# кристалл вешается на кнопку отдельным параметром icon_custom_emoji_id
# (main.py: main_menu_keyboard / panda.py: _build_main_menu_keyboard), по
# аналогии с "Сад"/"Рынок" и т.д. Значения здесь должны БУКВАЛЬНО совпадать
# с main.py: TEXTS[..]["menu_donate"] — это разные модули, но одна и та же
# кнопка, и F.text.in_(BUTTON_TEXT.values()) ниже matчит по тексту сообщения.
BUTTON_TEXT = {
    "ru": "Донаты",
    "en": "Donate",
}

TEXTS = {
    "ru": {
        "donate_intro": (
            f"{CRYSTAL_EMOJI} <b>Донаты</b>\n\n"
            "<i>Спасибо, что заглянули! Поддержка проекта помогает нам развивать "
            "бота и добавлять новые фичи — а взамен вы получаете кое-что приятное.</i>\n\n"
            f"{shop.CE_BALANCE} <b>Баланс:</b> {{coins}} {shop.CURRENCY}\n"
            f"{CRYSTAL_EMOJI} <b>Кристаллы:</b> {{crystals}}\n\n"
            "Выберите раздел ниже 👇"
        ),
        "privileges_button": "Привилегии",
        "crystals_button": "Кристаллы",
        "coins_button": "Монеты",
        "back_button": "Назад",
        "privileges_text": (
            "🎭 <b>Привилегии</b>\n\n"
            f"<i>Статус на {PRIVILEGE_DURATION_DAYS} дней: ускоряет рост в саду и "
            "выпечку в пекарне, поднимает лимит на дарение и получение подарков и "
            "добавляет бонус к опыту. Чем выше уровень — тем сильнее эффект.</i>\n\n"
            "{active_line}\n"
            "<i>Выберите уровень, чтобы посмотреть подробности 👇</i>"
        ),
        "priv_active_line": f"{CRYSTAL_EMOJI} Сейчас активна: <b>{{name}}</b> · до <b>{{until}}</b>",
        "priv_no_active_line": "<i>Сейчас активной привилегии нет.</i>",
        "priv_tier_button": "🎁 {price} | {name}",
        "priv_benefit_speedup": "🌱 <b>-{percent}%</b> ко времени роста в саду и выпечки в пекарне",
        "priv_benefit_limit": "🎁 Лимит на дарение и получение подарков — до <b>{limit}</b>",
        "priv_benefit_exp": "✨ <b>+{percent}%</b> к получаемому опыту",
        "priv_benefit_skin": "🌞 Бонусный облик панды «Солнечная панда»",
        "privilege_detail_text": (
            "{medal} <b>{name}</b>\n"
            f"<i>Действует {PRIVILEGE_DURATION_DAYS} дней с момента покупки</i>\n\n"
            "<blockquote>{benefits}</blockquote>\n\n"
            f"{CRYSTAL_EMOJI} <b>Цена:</b> {{price}} кристаллов\n\n"
            "{status_line}"
        ),
        "priv_status_none": "<i>Нажмите «Купить», чтобы активировать.</i>",
        "priv_status_same": "<i>Этот уровень у вас уже активен — покупка продлит его ещё на {days} дней.</i>",
        "priv_status_other": "<i>Сейчас активен другой уровень — покупка заменит его на этот.</i>",
        "priv_buy_button": "Купить",
        "priv_bought": (
            "✅ <b>Привилегия активирована!</b>\n\n"
            "{medal} <b>{name}</b>\n"
            f"{CRYSTAL_EMOJI} Списано: <b>{{price}}</b>\n"
            "📅 Действует до: <b>{until}</b>\n\n"
            "{skin_note}"
        ),
        "priv_bought_skin_note": (
            "🌞 <i>Бонусный облик «Солнечная панда» уже добавлен в раздел "
            "«Облики» — навсегда, даже после окончания привилегии.</i>"
        ),
        "priv_insufficient": (
            f"{CRYSTAL_EMOJI} <b>Не хватает кристаллов</b>\n\n"
            "Нужно: <b>{price}</b>\n"
            "У вас: <b>{balance}</b>\n\n"
            "<i>Пополните баланс кристаллов и возвращайтесь за привилегией.</i>"
        ),
        "priv_topup_button": "Пополнить кристаллы",
        "crystals_text": (
            f"{CRYSTAL_EMOJI} <b>Кристаллы</b>\n\n"
            "<blockquote><i>Кристаллы — премиальная валюта бота. За них открываются самые "
            "редкие облики панды и другие плюшки, которые не купить за обычные "
            "монеты.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Курс:</b> 1 звезда = 1 кристалл.</i>\n\n"
            "<i>Выберите готовый пакет или введите своё количество кнопкой ниже 👇</i>"
        ),
        "crystals_text_usd": (
            f"{CRYSTAL_EMOJI} <b>Кристаллы</b>\n\n"
            "<blockquote><i>Кристаллы — премиальная валюта бота. За них открываются самые "
            "редкие облики панды и другие плюшки, которые не купить за обычные "
            "монеты.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Курс:</b> 100 кристаллов ≈ $1.3.</i>\n\n"
            "<i>Выберите готовый пакет или введите своё количество кнопкой ниже 👇</i>"
        ),
        "package_button": "{crystals}  ·  ⭐ {stars}",
        "package_button_usd": "{crystals}  ·  ${usd}",
        "custom_button": "Ввести своё количество",
        "custom_ask": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Отправьте одним сообщением, сколько кристаллов хотите купить "
            "— от {min} до {max}.</i>\n"
            f"<i>{RATE_EMOJI} Курс: 1 звезда = 1 кристалл.</i>"
        ),
        "custom_ask_usd": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Отправьте одним сообщением, сколько кристаллов хотите купить "
            "— от {min} до {max}.</i>\n"
            f"<i>{RATE_EMOJI} Курс: 100 кристаллов ≈ $1.3.</i>"
        ),
        "choose_method_crystals": (
            f"{CRYSTAL_EMOJI} <b>Кристаллы</b>\n\n"
            "<i>Выберите способ оплаты 👇</i>"
        ),
        "choose_method_coins": (
            f"{shop.CE_BALANCE} <b>Монеты</b>\n\n"
            "<i>Выберите способ оплаты 👇</i>"
        ),
        "method_stars_button": "Telegram Stars",
        "method_cryptobot_button": "CryptoBot",
        "method_xrocket_button": "xRocket",
        "pay_prompt_crypto_crystals": (
            f"{CRYSTAL_EMOJI} К зачислению: <b>{{amount}} кристаллов</b>\n"
            f"{CRYPTO_PAY_TEXT_EMOJI} К оплате: <b>${{usd}}</b>\n\n"
            "<i>Нажмите «Оплатить» — откроется страница оплаты {provider}. "
            "После оплаты нажмите «Проверить оплату»: кристаллы зачислятся "
            "автоматически, обычно в течение минуты.</i>"
        ),
        "pay_prompt_crypto_coins": (
            f"{shop.CE_BALANCE} К зачислению: <b>{{amount}} {shop.CURRENCY}</b>\n"
            f"{CRYPTO_PAY_TEXT_EMOJI} К оплате: <b>${{usd}}</b>\n\n"
            "<i>Нажмите «Оплатить» — откроется страница оплаты {provider}. "
            "После оплаты нажмите «Проверить оплату»: монеты зачислятся "
            "автоматически, обычно в течение минуты.</i>"
        ),
        "pay_link_button": "Оплатить ${usd}",
        "check_payment_button": "Проверить оплату",
        "check_payment_not_found": "Платёж пока не найден. Подождите немного после оплаты и нажмите снова.",
        "check_payment_already": "Этот платёж уже зачислен ✅",
        "crypto_error": (
            "⚠️ <b>Не удалось создать счёт на оплату.</b>\n\n"
            "<i>Попробуйте другой способ оплаты или повторите попытку чуть позже.</i>"
        ),
        "custom_invalid": "<i>Нужно отправить просто число — попробуйте ещё раз.</i>",
        "custom_out_of_range": "<i>Количество должно быть от {min} до {max} — попробуйте ещё раз.</i>",
        "custom_cancelled": "<i>Покупка отменена.</i>",
        "cancel_button": "Отменить",
        "pay_prompt": (
            f"{CRYSTAL_EMOJI} К зачислению: <b>{{amount}} кристаллов</b>\n"
            f"{PAY_STAR_EMOJI} К оплате: <b>{{stars}} Stars</b>\n\n"
            "<i>Нажмите на кнопку ниже, чтобы перейти к оплате. Кристаллы "
            "зачислятся на баланс автоматически сразу после оплаты.</i>"
        ),
        "pay_button": "Оплатить {stars} ⭐",
        "my_stars_button": "Мои звёзды",
        "invoice_title": "Кристаллы",
        "invoice_description": (
            "{amount} 🎁 кристаллов для бота. Зачисляются на баланс сразу после оплаты."
        ),
        "invoice_label": "{amount} кристаллов",
        "payment_success": (
            "✅ <b>Спасибо за поддержку!</b>\n"
            f"{CRYSTAL_EMOJI} Начислено: <b>{{amount}}</b>\n"
            f"{CRYSTAL_EMOJI} Баланс кристаллов: <b>{{balance}}</b>"
        ),
        "coins_text": (
            f"{shop.CE_BALANCE} <b>Монеты</b>\n\n"
            f"<blockquote><i>Обычная валюта бота — {shop.CURRENCY_PLAIN}. За них покупаются "
            "обычные подарки и товары в магазине.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Курс:</b> 1 звезда = {COIN_MULTIPLIER} монет.</i>\n\n"
            "<i>Выберите готовый пакет или введите своё количество кнопкой ниже 👇</i>"
        ),
        "coins_text_usd": (
            f"{shop.CE_BALANCE} <b>Монеты</b>\n\n"
            f"<blockquote><i>Обычная валюта бота — {shop.CURRENCY_PLAIN}. За них покупаются "
            "обычные подарки и товары в магазине.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Курс:</b> 100 кристаллов ≈ $1.3, монеты — по той же цене "
            "за эквивалент в звёздах.</i>\n\n"
            "<i>Выберите готовый пакет или введите своё количество кнопкой ниже 👇</i>"
        ),
        "custom_coins_ask": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Отправьте одним сообщением, сколько монет хотите купить "
            "— от {min} до {max} (кратно {step}).</i>\n"
            f"<i>{RATE_EMOJI} Курс: 1 звезда = {COIN_MULTIPLIER} монет.</i>"
        ),
        "custom_coins_ask_usd": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Отправьте одним сообщением, сколько монет хотите купить "
            "— от {min} до {max} (кратно {step}).</i>\n"
            f"<i>{RATE_EMOJI} Курс: 100 кристаллов ≈ $1.3, монеты — по той же цене за эквивалент в звёздах.</i>"
        ),
        "custom_coins_not_multiple": (
            "<i>Количество должно быть кратно {step} (иначе не выходит целое число звёзд) "
            "— попробуйте ещё раз.</i>"
        ),
        "pay_prompt_coins": (
            f"{shop.CE_BALANCE} К зачислению: <b>{{amount}} монет</b>\n"
            f"{PAY_STAR_EMOJI} К оплате: <b>{{stars}} Stars</b>\n\n"
            "<i>Нажмите на кнопку ниже, чтобы перейти к оплате. Монеты "
            "зачислятся на баланс автоматически сразу после оплаты.</i>"
        ),
        "invoice_title_coins": "Монеты",
        "invoice_description_coins": (
            "{amount} монет для бота. Зачисляются на баланс сразу после оплаты."
        ),
        "invoice_label_coins": "{amount} монет",
        "payment_success_coins": (
            "✅ <b>Спасибо за поддержку!</b>\n"
            f"{shop.CE_BALANCE} Начислено: <b>{{amount}}</b>\n"
            f"{shop.CE_BALANCE} Баланс монет: <b>{{balance}}</b>"
        ),
    },
    "en": {
        "donate_intro": (
            f"{CRYSTAL_EMOJI} <b>Donate</b>\n\n"
            "<i>Thanks for stopping by! Supporting the project helps us keep "
            "building new features — and you get something nice in return.</i>\n\n"
            f"{shop.CE_BALANCE} <b>Balance:</b> {{coins}} {shop.CURRENCY}\n"
            f"{CRYSTAL_EMOJI} <b>Crystals:</b> {{crystals}}\n\n"
            "Pick a section below 👇"
        ),
        "privileges_button": "Privileges",
        "crystals_button": "Crystals",
        "coins_button": "Coins",
        "back_button": "Back",
        "privileges_text": (
            "🎭 <b>Privileges</b>\n\n"
            f"<i>A {PRIVILEGE_DURATION_DAYS}-day status: speeds up growing in the garden and "
            "baking in the bakery, raises your gift giving/receiving limit, and adds "
            "a bonus to earned experience. The higher the tier, the stronger the effect.</i>\n\n"
            "{active_line}\n"
            "<i>Pick a tier to see the details 👇</i>"
        ),
        "priv_active_line": f"{CRYSTAL_EMOJI} Currently active: <b>{{name}}</b> · until <b>{{until}}</b>",
        "priv_no_active_line": "<i>No active privilege right now.</i>",
        "priv_tier_button": "🎁 {price} | {name}",
        "priv_benefit_speedup": "🌱 <b>-{percent}%</b> to growing time in the garden and baking time in the bakery",
        "priv_benefit_limit": "🎁 Gift giving/receiving limit — up to <b>{limit}</b>",
        "priv_benefit_exp": "✨ <b>+{percent}%</b> to earned experience",
        "priv_benefit_skin": "🌞 Bonus panda skin: \"Solar Panda\"",
        "privilege_detail_text": (
            "{medal} <b>{name}</b>\n"
            f"<i>Lasts {PRIVILEGE_DURATION_DAYS} days from purchase</i>\n\n"
            "<blockquote>{benefits}</blockquote>\n\n"
            f"{CRYSTAL_EMOJI} <b>Price:</b> {{price}} crystals\n\n"
            "{status_line}"
        ),
        "priv_status_none": "<i>Tap \"Buy\" to activate it.</i>",
        "priv_status_same": "<i>This tier is already active — buying it again extends it by {days} more days.</i>",
        "priv_status_other": "<i>A different tier is currently active — buying this one will replace it.</i>",
        "priv_buy_button": "Buy",
        "priv_bought": (
            "✅ <b>Privilege activated!</b>\n\n"
            "{medal} <b>{name}</b>\n"
            f"{CRYSTAL_EMOJI} Spent: <b>{{price}}</b>\n"
            "📅 Active until: <b>{until}</b>\n\n"
            "{skin_note}"
        ),
        "priv_bought_skin_note": (
            "🌞 <i>The bonus \"Solar Panda\" skin has been added to the \"Looks\" section — "
            "forever, even after the privilege ends.</i>"
        ),
        "priv_insufficient": (
            f"{CRYSTAL_EMOJI} <b>Not enough crystals</b>\n\n"
            "Needed: <b>{price}</b>\n"
            "You have: <b>{balance}</b>\n\n"
            "<i>Top up your crystal balance and come back for the privilege.</i>"
        ),
        "priv_topup_button": "Top up crystals",
        "crystals_text": (
            f"{CRYSTAL_EMOJI} <b>Crystals</b>\n\n"
            "<blockquote><i>Crystals are the bot's premium currency — they unlock the "
            "rarest panda skins and other perks that regular coins can't "
            "buy.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Rate:</b> 1 star = 1 crystal.</i>\n\n"
            "<i>Pick a package or enter your own amount below 👇</i>"
        ),
        "crystals_text_usd": (
            f"{CRYSTAL_EMOJI} <b>Crystals</b>\n\n"
            "<blockquote><i>Crystals are the bot's premium currency — they unlock the "
            "rarest panda skins and other perks that regular coins can't "
            "buy.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Rate:</b> 100 crystals ≈ $1.3.</i>\n\n"
            "<i>Pick a package or enter your own amount below 👇</i>"
        ),
        "package_button": "{crystals}  ·  ⭐ {stars}",
        "package_button_usd": "{crystals}  ·  ${usd}",
        "custom_button": "Enter your own amount",
        "custom_ask": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Send how many crystals you'd like to buy in one message — "
            "from {min} to {max}.</i>\n"
            f"<i>{RATE_EMOJI} Rate: 1 star = 1 crystal.</i>"
        ),
        "custom_ask_usd": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Send how many crystals you'd like to buy in one message — "
            "from {min} to {max}.</i>\n"
            f"<i>{RATE_EMOJI} Rate: 100 crystals ≈ $1.3.</i>"
        ),
        "choose_method_crystals": (
            f"{CRYSTAL_EMOJI} <b>Crystals</b>\n\n"
            "<i>Choose a payment method 👇</i>"
        ),
        "choose_method_coins": (
            f"{shop.CE_BALANCE} <b>Coins</b>\n\n"
            "<i>Choose a payment method 👇</i>"
        ),
        "method_stars_button": "Telegram Stars",
        "method_cryptobot_button": "CryptoBot",
        "method_xrocket_button": "xRocket",
        "pay_prompt_crypto_crystals": (
            f"{CRYSTAL_EMOJI} You'll receive: <b>{{amount}} crystals</b>\n"
            f"{CRYPTO_PAY_TEXT_EMOJI} To pay: <b>${{usd}}</b>\n\n"
            "<i>Tap \"Pay\" to open the {provider} payment page. Once you've "
            "paid, tap \"Check payment\" — crystals are credited automatically, "
            "usually within a minute.</i>"
        ),
        "pay_prompt_crypto_coins": (
            f"{shop.CE_BALANCE} You'll receive: <b>{{amount}} {shop.CURRENCY}</b>\n"
            f"{CRYPTO_PAY_TEXT_EMOJI} To pay: <b>${{usd}}</b>\n\n"
            "<i>Tap \"Pay\" to open the {provider} payment page. Once you've "
            "paid, tap \"Check payment\" — coins are credited automatically, "
            "usually within a minute.</i>"
        ),
        "pay_link_button": "Pay ${usd}",
        "check_payment_button": "Check payment",
        "check_payment_not_found": "Payment not found yet. Wait a bit after paying and try again.",
        "check_payment_already": "This payment has already been credited ✅",
        "crypto_error": (
            "⚠️ <b>Couldn't create the payment invoice.</b>\n\n"
            "<i>Try a different payment method or try again in a bit.</i>"
        ),
        "custom_invalid": "<i>Please send just a number — try again.</i>",
        "custom_out_of_range": "<i>Amount must be between {min} and {max} — try again.</i>",
        "custom_cancelled": "<i>Purchase cancelled.</i>",
        "cancel_button": "Cancel",
        "pay_prompt": (
            f"{CRYSTAL_EMOJI} You'll receive: <b>{{amount}} crystals</b>\n"
            f"{PAY_STAR_EMOJI} To pay: <b>{{stars}} Stars</b>\n\n"
            "<i>Tap the button below to pay. Crystals are credited to your "
            "balance automatically right after payment.</i>"
        ),
        "pay_button": "Pay {stars} ⭐",
        "my_stars_button": "My Stars",
        "invoice_title": "Crystals",
        "invoice_description": (
            "{amount} 🎁 crystals for the bot. Credited to your balance right after payment."
        ),
        "invoice_label": "{amount} crystals",
        "payment_success": (
            "✅ <b>Thanks for the support!</b>\n"
            f"{CRYSTAL_EMOJI} Credited: <b>{{amount}}</b>\n"
            f"{CRYSTAL_EMOJI} Crystal balance: <b>{{balance}}</b>"
        ),
        "coins_text": (
            f"{shop.CE_BALANCE} <b>Coins</b>\n\n"
            f"<blockquote><i>The bot's regular currency — {shop.CURRENCY_PLAIN}. Used to buy "
            "regular gifts and items in the shop.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Rate:</b> 1 star = {COIN_MULTIPLIER} coins.</i>\n\n"
            "<i>Pick a package or enter your own amount below 👇</i>"
        ),
        "coins_text_usd": (
            f"{shop.CE_BALANCE} <b>Coins</b>\n\n"
            f"<blockquote><i>The bot's regular currency — {shop.CURRENCY_PLAIN}. Used to buy "
            "regular gifts and items in the shop.</i></blockquote>\n\n"
            f"<i>{RATE_EMOJI} <b>Rate:</b> 100 crystals ≈ $1.3, coins are priced the same way "
            "per star-equivalent.</i>\n\n"
            "<i>Pick a package or enter your own amount below 👇</i>"
        ),
        "custom_coins_ask": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Send how many coins you'd like to buy in one message — "
            "from {min} to {max} (must be a multiple of {step}).</i>\n"
            f"<i>{RATE_EMOJI} Rate: 1 star = {COIN_MULTIPLIER} coins.</i>"
        ),
        "custom_coins_ask_usd": (
            f"<i>{CUSTOM_AMOUNT_EMOJI} Send how many coins you'd like to buy in one message — "
            "from {min} to {max} (must be a multiple of {step}).</i>\n"
            f"<i>{RATE_EMOJI} Rate: 100 crystals ≈ $1.3, coins are priced the same way per star-equivalent.</i>"
        ),
        "custom_coins_not_multiple": (
            "<i>Amount must be a multiple of {step} (otherwise it won't convert to a whole "
            "number of stars) — try again.</i>"
        ),
        "pay_prompt_coins": (
            f"{shop.CE_BALANCE} You'll receive: <b>{{amount}} coins</b>\n"
            f"{PAY_STAR_EMOJI} To pay: <b>{{stars}} Stars</b>\n\n"
            "<i>Tap the button below to pay. Coins are credited to your "
            "balance automatically right after payment.</i>"
        ),
        "invoice_title_coins": "Coins",
        "invoice_description_coins": (
            "{amount} coins for the bot. Credited to your balance right after payment."
        ),
        "invoice_label_coins": "{amount} coins",
        "payment_success_coins": (
            "✅ <b>Thanks for the support!</b>\n"
            f"{shop.CE_BALANCE} Credited: <b>{{amount}}</b>\n"
            f"{shop.CE_BALANCE} Coin balance: <b>{{balance}}</b>"
        ),
    },
}


# ==========================
#   ВСПОМОГАТЕЛЬНОЕ
# ==========================

async def _get_lang(state: FSMContext, user_id: int) -> str:
    """Возвращает язык пользователя — сперва из FSM-состояния (кэш), а при
    его отсутствии (например, после рестарта бота — MemoryStorage не
    переживает рестарт) напрямую из БД. См. подробный комментарий у
    аналогичной функции в panda.py."""
    data = await state.get_data()
    lang = data.get("lang")
    if lang:
        return lang

    onboarding = await database.get_onboarding(user_id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"
    await state.update_data(lang=lang)
    return lang


async def _safe_edit_text(message: Message, text: str, reply_markup=None) -> None:
    """Обновляет уже отправленное сообщение донат-экрана. Делегирует в
    admin.smart_edit, который сам решает — caption (если это фото,
    отправленное с картинкой раздела через send_with_section_image) или
    обычный edit_text — и глотает "message is not modified" (правим тем
    же текстом/разметкой, что уже показаны). Локальный импорт — admin.py
    сам импортирует donate.py на верхнем уровне (цикл)."""
    import admin

    await admin.smart_edit(message, text, reply_markup=reply_markup)


async def _edit_prompt_by_ids(bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> bool:
    """То же самое, что и _safe_edit_text выше, но когда под рукой нет
    объекта Message (а есть только chat_id/message_id, сохранённые в FSM —
    именно так это устроено в on_custom_amount_received/
    on_custom_coin_amount_received: там прилетает НОВОЕ сообщение от
    игрока с суммой, а редактировать нужно СТАРОЕ сообщение-запрос).

    Экран доната мог быть отправлен и как обычный текст, и как подпись к
    фото (см. admin.send_with_section_image) — bot.edit_message_text на
    фото-сообщении падает с TelegramBadRequest ("there is no text in the
    message to edit"), раньше это ошибочно трактовалось как "сообщение
    недоступно" и вместо правки отправлялось совсем новое сообщение.
    Здесь пробуем оба варианта редактирования и лишь если ни один не
    сработал (сообщение правда удалено и т.п.) — возвращаем False, и
    вызывающий код сам решает, отправлять ли новое сообщение.
    Возвращает True, если редактирование удалось."""
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return True  # уже показан тот же текст/разметка — считаем успехом
    try:
        await bot.edit_message_caption(
            chat_id=chat_id, message_id=message_id, caption=text, reply_markup=reply_markup
        )
        return True
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return True
    return False


def _donate_menu_keyboard(lang: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["crystals_button"],
        callback_data=main.owner_cb(owner_id, "donate:crystals"),
        style="primary",
        icon_custom_emoji_id=CRYSTAL_EMOJI_ID,
    )
    builder.button(
        text=t["coins_button"],
        callback_data=main.owner_cb(owner_id, "donate:coins"),
        style="primary",
        icon_custom_emoji_id=COIN_EMOJI_ID,
    )
    builder.button(
        text=t["privileges_button"],
        callback_data=main.owner_cb(owner_id, "donate:privileges"),
        style="primary",
        icon_custom_emoji_id=PRIVILEGES_EMOJI_ID,
    )
    builder.adjust(2, 1)
    return builder.as_markup()


def _back_keyboard(lang: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "donate:back"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _privileges_menu_keyboard(lang: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    for tier in PRIVILEGE_TIERS:
        builder.button(
            text=t["priv_tier_button"].format(name=tier["name"], price=tier["price"]),
            callback_data=main.owner_cb(owner_id, f"donate:priv:{tier['id']}"),
            style="primary",
            icon_custom_emoji_id=CRYSTAL_EMOJI_ID,
        )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "donate:back"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _privilege_detail_keyboard(lang: str, tier_id: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["priv_buy_button"],
        callback_data=main.owner_cb(owner_id, f"donate:priv_buy:{tier_id}"),
        style="success",
        icon_custom_emoji_id=CRYSTAL_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "donate:privileges"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _privilege_insufficient_keyboard(lang: str, tier_id: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["priv_topup_button"],
        callback_data=main.owner_cb(owner_id, "donate:crystals"),
        style="success",
        icon_custom_emoji_id=CRYSTAL_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, f"donate:priv:{tier_id}"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _method_button_text(t: dict, method: str) -> str:
    return {
        "stars": t["method_stars_button"],
        "cryptobot": t["method_cryptobot_button"],
        "xrocket": t["method_xrocket_button"],
    }[method]


def _method_icon_id(method: str) -> str:
    return {
        "stars": PAY_STAR_EMOJI_ID,
        "cryptobot": METHOD_CRYPTOBOT_EMOJI_ID,
        "xrocket": METHOD_XROCKET_EMOJI_ID,
    }[method]


def _method_keyboard(lang: str, kind: str, owner_id: int):
    import main

    """Экран выбора способа оплаты — показывается после выбора категории
    ("Кристаллы"/"Монеты"), до списка пакетов. Способы без вписанного
    токена (см. crypto_pay.py) не показываются вообще (_method_available)."""
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    for method in PAYMENT_METHODS:
        if not _method_available(method):
            continue
        builder.button(
            text=_method_button_text(t, method),
            callback_data=main.owner_cb(owner_id, f"donate:method:{kind}:{method}"),
            style="primary",
            icon_custom_emoji_id=_method_icon_id(method),
        )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "donate:back"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _crystals_keyboard(lang: str, method: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    for idx, crystals in enumerate(CRYSTAL_PACKAGES):
        if method == "stars":
            text = t["package_button"].format(crystals=crystals, stars=_stars_for(crystals))
        else:
            text = t["package_button_usd"].format(
                crystals=crystals, usd=f"{_usd_for_stars(_stars_for(crystals)):.2f}"
            )
        builder.button(
            text=text,
            callback_data=main.owner_cb(owner_id, f"donate:buy:{method}:{idx}"),
            style="primary",
            icon_custom_emoji_id=CRYSTAL_EMOJI_ID,
        )
    builder.button(
        text=t["custom_button"],
        callback_data=main.owner_cb(owner_id, f"donate:custom:{method}"),
        style="primary",
        icon_custom_emoji_id=CUSTOM_AMOUNT_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "donate:crystals"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def _coins_keyboard(lang: str, method: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    for idx, coins in enumerate(COIN_PACKAGES):
        if method == "stars":
            text = t["package_button"].format(crystals=coins, stars=_stars_for_coins(coins))
        else:
            text = t["package_button_usd"].format(
                crystals=coins, usd=f"{_usd_for_stars(_stars_for_coins(coins)):.2f}"
            )
        builder.button(
            text=text,
            callback_data=main.owner_cb(owner_id, f"donate:buycoin:{method}:{idx}"),
            style="primary",
            icon_custom_emoji_id=COIN_EMOJI_ID,
        )
    builder.button(
        text=t["custom_button"],
        callback_data=main.owner_cb(owner_id, f"donate:customcoin:{method}"),
        style="primary",
        icon_custom_emoji_id=CUSTOM_AMOUNT_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, "donate:coins"),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def _cancel_keyboard(lang: str, method: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["cancel_button"],
        callback_data=main.owner_cb(owner_id, f"donate:custom_cancel:{method}"),
        style="primary",
        icon_custom_emoji_id=CANCEL_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _cancel_coins_keyboard(lang: str, method: str, owner_id: int):
    import main

    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["cancel_button"],
        callback_data=main.owner_cb(owner_id, f"donate:customcoin_cancel:{method}"),
        style="primary",
        icon_custom_emoji_id=CANCEL_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _crypto_pay_keyboard(lang: str, pay_url: str, usd: float, row_id: int, back_callback: str, owner_id: int):
    import main

    """Клавиатура экрана оплаты через CryptoBot/xRocket: ссылка на оплату,
    кнопка ручной проверки платежа (row_id — id строки в crypto_invoices)
    и "Назад" — к списку пакетов этого способа оплаты."""
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["pay_link_button"].format(usd=f"{usd:.2f}"),
        url=pay_url,
        style="success",
        icon_custom_emoji_id=CRYPTO_PAY_BUTTON_EMOJI_ID,
    )
    builder.button(
        text=t["check_payment_button"],
        callback_data=main.owner_cb(owner_id, f"donate:checkpay:{row_id}"),
        style="primary",
        icon_custom_emoji_id=CRYPTO_CHECK_BUTTON_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, back_callback),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


def _pay_keyboard(lang: str, link: str, stars: int, owner_id: int, back_callback: str = "donate:crystals", icon_emoji_id: str = CRYSTAL_EMOJI_ID):
    import main

    """Кнопка-ссылка на оплату (Telegram Stars, см. _create_crystal_invoice_link) —
    зелёная (style="success"), чтобы визуально выделяться как основное
    действие на экране; кнопка-ссылка "Мои звёзды" (tg://stars/ — открывает
    у игрока его баланс Stars в самом Telegram, чтобы можно было свериться
    перед оплатой); и кнопка "Назад" — возвращает к списку пакетов, ссылка
    на оплату при этом не сгорает: если игрок передумал и вернулся, а потом
    снова выбрал тот же пакет, сгенерируется новая ссылка на новый инвойс.

    icon_emoji_id — какая валюта зачисляется: CRYSTAL_EMOJI_ID (по
    умолчанию, покупка кристаллов) или COIN_EMOJI_ID (покупка монет,
    см. вызовы с back_callback="donate:coins")."""
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["pay_button"].format(stars=stars),
        url=link,
        style="success",
        icon_custom_emoji_id=icon_emoji_id,
    )
    builder.button(
        text=t["my_stars_button"],
        url="tg://stars/",
        style="primary",
        icon_custom_emoji_id=MY_STARS_EMOJI_ID,
    )
    builder.button(
        text=t["back_button"],
        callback_data=main.owner_cb(owner_id, back_callback),
        style="primary",
        icon_custom_emoji_id=BACK_EMOJI_ID,
    )
    builder.adjust(1)
    return builder.as_markup()


async def _create_crystal_invoice_link(bot, lang: str, amount: int) -> str:
    """Генерирует https-ссылку на оплату amount кристаллов через Telegram
    Stars (createInvoiceLink) — вместо прямой отправки инвойса сообщением.
    payload вида "crystals:{amount}" разбирается в on_successful_payment."""
    t = TEXTS[lang]
    return await bot.create_invoice_link(
        title=t["invoice_title"],
        description=t["invoice_description"].format(amount=amount),
        payload=f"crystals:{amount}",
        provider_token="",  # Stars не идут через внешнего провайдера — оставляем пустым
        currency="XTR",
        prices=[LabeledPrice(label=t["invoice_label"].format(amount=amount), amount=_stars_for(amount))],
    )


async def _create_coin_invoice_link(bot, lang: str, amount: int) -> str:
    """То же самое, что и _create_crystal_invoice_link выше, но для монет:
    payload вида "coins:{amount}" — разбирается в on_successful_payment по
    первой части (crystals/coins), чтобы понять, какой баланс пополнять."""
    t = TEXTS[lang]
    return await bot.create_invoice_link(
        title=t["invoice_title_coins"],
        description=t["invoice_description_coins"].format(amount=amount),
        payload=f"coins:{amount}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(
            label=t["invoice_label_coins"].format(amount=amount),
            amount=_stars_for_coins(amount),
        )],
    )


def _format_date(ts: float, lang: str) -> str:
    """Дата окончания привилегии — dd.mm.yyyy для ru, mm/dd/yyyy для en."""
    dt = datetime.fromtimestamp(ts)
    return dt.strftime("%d.%m.%Y") if lang == "ru" else dt.strftime("%m/%d/%Y")


async def get_active_privilege(user_id: int) -> dict | None:
    """Возвращает активную привилегию игрока (запись из user_privileges +
    сам тариф из PRIVILEGE_TIERS_BY_ID), либо None, если её нет или срок
    истёк. Истёкшую строку не удаляем — следующая покупка её перезапишет,
    удалять нечего чистить отдельным фоновым job'ом."""
    db = await database.get_db()
    async with db.execute(
        "SELECT tier_id, expires_at FROM user_privileges WHERE user_id = ?", (user_id,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None or row["expires_at"] <= time.time():
        return None
    tier = PRIVILEGE_TIERS_BY_ID.get(row["tier_id"])
    if tier is None:
        return None
    return {"tier": tier, "expires_at": row["expires_at"]}


async def buy_privilege(user_id: int, tier_id: str) -> tuple[str, int]:
    """Покупает привилегию tier_id. Возвращает (результат, кристаллов_списано):
    - "ok" — куплено, кристаллы списаны, срок — сейчас + PRIVILEGE_DURATION_DAYS;
    - "insufficient" — не хватает кристаллов, ничего не списано.

    Списание — напрямую через prof._change_crystal_balance() под общим
    database.user_lock(user_id), по той же схеме, что и panda.buy_skin().

    Бонусный скин (tier["bonus_skin"]) выдаётся отдельным вызовом
    panda.grant_skin_free() УЖЕ ПОСЛЕ выхода из user_lock — сам
    grant_skin_free заново берёт этот лок на user_id, а он не
    реентерабелен (см. panda.py: buy_skin). Выдача идемпотентна и не
    привязана к сроку привилегии — при продлении/повторной покупке
    того же уровня скин просто не выдаётся повторно."""
    tier = PRIVILEGE_TIERS_BY_ID[tier_id]
    price = tier["price"]

    async with database.user_lock(user_id):
        balance = await prof.get_crystals(user_id)
        if balance < price:
            return "insufficient", 0

        await prof._change_crystal_balance(user_id, -price)

        now = time.time()
        expires_at = now + PRIVILEGE_DURATION_DAYS * 86400
        db = await database.get_db()
        await db.execute(
            """
            INSERT INTO user_privileges (user_id, tier_id, purchased_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                tier_id = excluded.tier_id,
                purchased_at = excluded.purchased_at,
                expires_at = excluded.expires_at
            """,
            (user_id, tier_id, now, expires_at),
        )
        await database.commit()

    if tier["bonus_skin"]:
        await panda.grant_skin_free(user_id, tier["bonus_skin"])

    return "ok", price


async def _privileges_intro_text(lang: str, user_id: int) -> str:
    t = TEXTS[lang]
    active = await get_active_privilege(user_id)
    if active is None:
        active_line = t["priv_no_active_line"]
    else:
        active_line = t["priv_active_line"].format(
            name=active["tier"]["name"],
            until=_format_date(active["expires_at"], lang),
        )
    return t["privileges_text"].format(active_line=active_line)


def _privilege_benefits_lines(lang: str, tier: dict) -> str:
    t = TEXTS[lang]
    lines = [
        t["priv_benefit_speedup"].format(percent=tier["speedup_percent"]),
        t["priv_benefit_limit"].format(limit=tier["gift_limit"]),
    ]
    if tier["exp_bonus_percent"]:
        lines.append(t["priv_benefit_exp"].format(percent=tier["exp_bonus_percent"]))
    if tier["bonus_skin"]:
        lines.append(t["priv_benefit_skin"])
    return "\n".join(lines)


async def _privilege_detail_text(lang: str, tier_id: str, user_id: int) -> str:
    t = TEXTS[lang]
    tier = PRIVILEGE_TIERS_BY_ID[tier_id]
    active = await get_active_privilege(user_id)

    if active is None:
        status_line = t["priv_status_none"]
    elif active["tier"]["id"] == tier_id:
        status_line = t["priv_status_same"].format(days=PRIVILEGE_DURATION_DAYS)
    else:
        status_line = t["priv_status_other"]

    return t["privilege_detail_text"].format(
        medal=tier["medal"],
        name=tier["name"],
        price=tier["price"],
        benefits=_privilege_benefits_lines(lang, tier),
        status_line=status_line,
    )


async def _donate_intro_text(lang: str, user_id: int) -> str:
    """Текст главного экрана донатов с балансом — монеты и кристаллы
    показываются РАЗДЕЛЬНЫМИ строками (не в одном ряду, как balance_line
    в prof.py), чтобы не мешать разные валюты в одном визуальном блоке."""
    coins = await shop.get_balance(user_id)
    crystals = await prof.get_crystals(user_id)
    return TEXTS[lang]["donate_intro"].format(coins=coins, crystals=crystals)


# ==========================
#   ХЕНДЛЕРЫ — НАВИГАЦИЯ
# ==========================

@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_donate_menu(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    text = await _donate_intro_text(lang, message.from_user.id)

    # Картинка раздела (см. admin.py: admin:sections, ключ "donate") —
    # если задана, экран донатов отправляется как фото с текстом в
    # подписи, иначе как обычно текстом. Локальный импорт — admin.py
    # сам импортирует donate.py на верхнем уровне (цикл).
    import admin

    await admin.send_with_section_image(message, "donate", text, reply_markup=_donate_menu_keyboard(lang, message.from_user.id))


@router.callback_query(F.data == "donate:back")
async def on_donate_back(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _donate_intro_text(lang, callback.from_user.id)
    await callback.answer()
    await _safe_edit_text(callback.message, text, reply_markup=_donate_menu_keyboard(lang, callback.from_user.id))


@router.callback_query(F.data == "donate:privileges")
async def on_privileges(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    text = await _privileges_intro_text(lang, callback.from_user.id)
    await callback.answer()
    await _safe_edit_text(callback.message, text, reply_markup=_privileges_menu_keyboard(lang, callback.from_user.id))


@router.callback_query(F.data.startswith("donate:priv:"))
async def on_privilege_detail(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    tier_id = callback.data.rsplit(":", 1)[-1]
    if tier_id not in PRIVILEGE_TIERS_BY_ID:
        await callback.answer()
        return

    text = await _privilege_detail_text(lang, tier_id, callback.from_user.id)
    await callback.answer()
    await _safe_edit_text(
        callback.message, text, reply_markup=_privilege_detail_keyboard(lang, tier_id, callback.from_user.id)
    )


@router.callback_query(F.data.startswith("donate:priv_buy:"))
async def on_privilege_buy(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    tier_id = callback.data.rsplit(":", 1)[-1]
    tier = PRIVILEGE_TIERS_BY_ID.get(tier_id)
    if tier is None:
        await callback.answer()
        return

    user_id = callback.from_user.id
    result, spent = await buy_privilege(user_id, tier_id)

    if result == "insufficient":
        balance = await prof.get_crystals(user_id)
        await callback.answer()
        await _safe_edit_text(
            callback.message,
            t["priv_insufficient"].format(price=tier["price"], balance=balance),
            reply_markup=_privilege_insufficient_keyboard(lang, tier_id, user_id),
        )
        return

    active = await get_active_privilege(user_id)
    skin_note = t["priv_bought_skin_note"] if tier["bonus_skin"] else ""
    await callback.answer(t["priv_buy_button"])
    await _safe_edit_text(
        callback.message,
        t["priv_bought"].format(
            medal=tier["medal"],
            name=tier["name"],
            price=spent,
            until=_format_date(active["expires_at"], lang),
            skin_note=skin_note,
        ).strip(),
        reply_markup=_back_keyboard(lang, user_id),
    )


@router.callback_query(F.data == "donate:crystals")
async def on_crystals(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    await callback.answer()
    await _safe_edit_text(
        callback.message, t["choose_method_crystals"], reply_markup=_method_keyboard(lang, "crystals", callback.from_user.id)
    )


@router.callback_query(F.data == "donate:coins")
async def on_coins(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    await callback.answer()
    await _safe_edit_text(
        callback.message, t["choose_method_coins"], reply_markup=_method_keyboard(lang, "coins", callback.from_user.id)
    )


@router.callback_query(F.data.startswith("donate:method:"))
async def on_method_selected(callback: CallbackQuery, state: FSMContext) -> None:
    """Способ оплаты выбран (Stars / CryptoBot / xRocket) — показываем
    список пакетов кристаллов/монет, уже в ценах этого способа оплаты."""
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    _, _, kind, method = parts
    if kind not in ("crystals", "coins") or not _method_available(method):
        await callback.answer()
        return

    await callback.answer()
    # Курс на экране пакетов должен соответствовать выбранному способу
    # оплаты: для Stars — в звёздах, для CryptoBot/xRocket — в долларах
    # (см. crystals_text_usd/coins_text_usd выше и _usd_for_stars).
    usd_suffix = "" if method == "stars" else "_usd"
    if kind == "crystals":
        await _safe_edit_text(
            callback.message, t[f"crystals_text{usd_suffix}"], reply_markup=_crystals_keyboard(lang, method, callback.from_user.id)
        )
    else:
        await _safe_edit_text(
            callback.message, t[f"coins_text{usd_suffix}"], reply_markup=_coins_keyboard(lang, method, callback.from_user.id)
        )


# ==========================
#   ОПЛАТА — ОБЩЕЕ ДЛЯ ВСЕХ СПОСОБОВ
# ==========================

async def _build_pay_screen(
    bot, lang: str, user_id: int, chat_id: int, message_id: int, kind: str, method: str, amount: int
) -> tuple[str, object]:
    """Готовит (текст, клавиатуру) экрана оплаты для указанного способа —
    Stars (ссылка createInvoiceLink, как раньше) либо CryptoBot/xRocket
    (инвойс через crypto_pay.py + строка в crypto_invoices на дальнейшую
    проверку). При ошибке создания крипто-инвойса возвращает экран с
    сообщением об ошибке вместо падения хендлера."""
    t = TEXTS[lang]
    stars = _stars_for(amount) if kind == "crystals" else _stars_for_coins(amount)

    if method == "stars":
        if kind == "crystals":
            link = await _create_crystal_invoice_link(bot, lang, amount)
            text = t["pay_prompt"].format(amount=amount, stars=stars)
            markup = _pay_keyboard(lang, link, stars, user_id, back_callback="donate:method:crystals:stars")
        else:
            link = await _create_coin_invoice_link(bot, lang, amount)
            text = t["pay_prompt_coins"].format(amount=amount, stars=stars)
            markup = _pay_keyboard(
                lang, link, stars, user_id, back_callback="donate:method:coins:stars", icon_emoji_id=COIN_EMOJI_ID
            )
        return text, markup

    # --- CryptoBot / xRocket ---
    usd = _usd_for_stars(stars)
    description = f"{amount} {'crystals' if kind == 'crystals' else 'coins'}"
    payload = f"{kind}:{amount}:{user_id}"

    if method == "cryptobot":
        invoice = await crypto_pay.create_cryptobot_invoice(usd, description, payload)
        provider_label = "CryptoBot"
    else:
        invoice = await crypto_pay.create_xrocket_invoice(usd, description, payload)
        provider_label = "xRocket"

    back_callback = f"donate:method:{kind}:{method}"
    if invoice is None:
        logger.warning("Failed to create %s invoice for user %s (%s %s)", method, user_id, kind, amount)
        builder = InlineKeyboardBuilder()
        import main

        builder.button(
            text=t["back_button"],
            callback_data=main.owner_cb(user_id, back_callback),
            style="primary",
            icon_custom_emoji_id=BACK_EMOJI_ID,
        )
        builder.adjust(1)
        return t["crypto_error"], builder.as_markup()

    row_id = await _create_pending_invoice(
        provider=method,
        provider_invoice_id=invoice["invoice_id"],
        user_id=user_id,
        kind=kind,
        amount=amount,
        lang=lang,
        chat_id=chat_id,
        message_id=message_id,
    )

    text_key = "pay_prompt_crypto_crystals" if kind == "crystals" else "pay_prompt_crypto_coins"
    text = t[text_key].format(amount=amount, usd=f"{usd:.2f}", provider=provider_label)
    markup = _crypto_pay_keyboard(lang, invoice["pay_url"], usd, row_id, back_callback, user_id)
    return text, markup


@router.callback_query(F.data.startswith("donate:buy:"))
async def on_buy_package(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    _, _, method, idx_raw = parts
    if not _method_available(method) or not idx_raw.isdigit() or not (0 <= int(idx_raw) < len(CRYSTAL_PACKAGES)):
        await callback.answer()
        return

    crystals = CRYSTAL_PACKAGES[int(idx_raw)]
    await callback.answer()

    text, markup = await _build_pay_screen(
        callback.bot, lang, callback.from_user.id, callback.message.chat.id, callback.message.message_id,
        "crystals", method, crystals,
    )
    await _safe_edit_text(callback.message, text, reply_markup=markup)


@router.callback_query(F.data.startswith("donate:buycoin:"))
async def on_buy_coin_package(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer()
        return
    _, _, method, idx_raw = parts
    if not _method_available(method) or not idx_raw.isdigit() or not (0 <= int(idx_raw) < len(COIN_PACKAGES)):
        await callback.answer()
        return

    coins = COIN_PACKAGES[int(idx_raw)]
    await callback.answer()

    text, markup = await _build_pay_screen(
        callback.bot, lang, callback.from_user.id, callback.message.chat.id, callback.message.message_id,
        "coins", method, coins,
    )
    await _safe_edit_text(callback.message, text, reply_markup=markup)


# ==========================
#   ХЕНДЛЕРЫ — "ВВЕСТИ СВОЁ КОЛИЧЕСТВО"
# ==========================

@router.callback_query(F.data.startswith("donate:custom:"))
async def on_custom_request(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    method = callback.data.rsplit(":", 1)[-1]
    if not _method_available(method):
        await callback.answer()
        return

    await state.set_state(DonateStates.waiting_custom_amount)
    await callback.answer()
    ask_key = "custom_ask" if method == "stars" else "custom_ask_usd"
    # Запрос суммы показываем через обновление (edit_text) уже показанной
    # игроку карточки с пакетами — той же техникой, что и итог покупки
    # (см. _pay_keyboard/_create_crystal_invoice_link выше и шапку файла),
    # а не отдельным новым сообщением. id и чат этого сообщения запоминаем
    # в FSM: как только сумма придёт, мы заменим именно ЕГО на итог с
    # кнопкой оплаты (on_custom_amount_received ниже). Способ оплаты тоже
    # запоминаем в FSM — сообщением с суммой он не передаётся.
    await _safe_edit_text(
        callback.message,
        t[ask_key].format(min=CUSTOM_MIN, max=CUSTOM_MAX),
        reply_markup=_cancel_keyboard(lang, method, callback.from_user.id),
    )
    await state.update_data(
        donate_prompt_chat_id=callback.message.chat.id,
        donate_prompt_message_id=callback.message.message_id,
        donate_pay_method=method,
    )


@router.callback_query(F.data.startswith("donate:custom_cancel:"))
async def on_custom_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    method = callback.data.rsplit(":", 1)[-1]

    await state.set_state(None)
    await callback.answer()
    usd_suffix = "" if method == "stars" else "_usd"
    await _safe_edit_text(
        callback.message, t[f"crystals_text{usd_suffix}"], reply_markup=_crystals_keyboard(lang, method, callback.from_user.id)
    )


@router.message(StateFilter(DonateStates.waiting_custom_amount))
async def on_custom_amount_received(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    t = TEXTS[lang]

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(t["custom_invalid"])
        return

    amount = int(raw)
    if not (CUSTOM_MIN <= amount <= CUSTOM_MAX):
        await message.answer(t["custom_out_of_range"].format(min=CUSTOM_MIN, max=CUSTOM_MAX))
        return

    data = await state.get_data()
    prompt_chat_id = data.get("donate_prompt_chat_id")
    prompt_message_id = data.get("donate_prompt_message_id")
    method = data.get("donate_pay_method", "stars")
    await state.set_state(None)

    if prompt_chat_id is None or prompt_message_id is None:
        prompt_chat_id, prompt_message_id = message.chat.id, message.message_id

    pay_text, pay_markup = await _build_pay_screen(
        message.bot, lang, message.from_user.id, prompt_chat_id, prompt_message_id, "crystals", method, amount,
    )

    edited = await _edit_prompt_by_ids(message.bot, prompt_chat_id, prompt_message_id, pay_text, pay_markup)
    if edited:
        return

    await message.answer(pay_text, reply_markup=pay_markup)


@router.callback_query(F.data.startswith("donate:customcoin:"))
async def on_custom_coin_request(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    method = callback.data.rsplit(":", 1)[-1]
    if not _method_available(method):
        await callback.answer()
        return

    await state.set_state(DonateStates.waiting_custom_coins_amount)
    await callback.answer()
    ask_key = "custom_coins_ask" if method == "stars" else "custom_coins_ask_usd"
    await _safe_edit_text(
        callback.message,
        t[ask_key].format(min=CUSTOM_COINS_MIN, max=CUSTOM_COINS_MAX, step=COIN_MULTIPLIER),
        reply_markup=_cancel_coins_keyboard(lang, method, callback.from_user.id),
    )
    await state.update_data(
        donate_prompt_chat_id=callback.message.chat.id,
        donate_prompt_message_id=callback.message.message_id,
        donate_pay_method=method,
    )


@router.callback_query(F.data.startswith("donate:customcoin_cancel:"))
async def on_custom_coin_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]
    method = callback.data.rsplit(":", 1)[-1]

    await state.set_state(None)
    await callback.answer()
    usd_suffix = "" if method == "stars" else "_usd"
    await _safe_edit_text(
        callback.message, t[f"coins_text{usd_suffix}"], reply_markup=_coins_keyboard(lang, method, callback.from_user.id)
    )


@router.message(StateFilter(DonateStates.waiting_custom_coins_amount))
async def on_custom_coin_amount_received(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    t = TEXTS[lang]

    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.answer(t["custom_invalid"])
        return

    amount = int(raw)
    if not (CUSTOM_COINS_MIN <= amount <= CUSTOM_COINS_MAX):
        await message.answer(
            t["custom_out_of_range"].format(min=CUSTOM_COINS_MIN, max=CUSTOM_COINS_MAX)
        )
        return

    # Курс 1 звезда = COIN_MULTIPLIER монет — сумма должна делиться нацело,
    # иначе не получится целого числа звёзд (и цены в USD) для инвойса.
    if amount % COIN_MULTIPLIER != 0:
        await message.answer(t["custom_coins_not_multiple"].format(step=COIN_MULTIPLIER))
        return

    data = await state.get_data()
    prompt_chat_id = data.get("donate_prompt_chat_id")
    prompt_message_id = data.get("donate_prompt_message_id")
    method = data.get("donate_pay_method", "stars")
    await state.set_state(None)

    if prompt_chat_id is None or prompt_message_id is None:
        prompt_chat_id, prompt_message_id = message.chat.id, message.message_id

    pay_text, pay_markup = await _build_pay_screen(
        message.bot, lang, message.from_user.id, prompt_chat_id, prompt_message_id, "coins", method, amount,
    )

    edited = await _edit_prompt_by_ids(message.bot, prompt_chat_id, prompt_message_id, pay_text, pay_markup)
    if edited:
        return

    await message.answer(pay_text, reply_markup=pay_markup)


# ==========================
#   НАЧИСЛЕНИЕ ПОКУПКИ (общее для Stars/CryptoBot/xRocket)
# ==========================

async def _apply_purchase(user_id: int, kind: str, amount: int) -> tuple[int, int]:
    """Зачисляет купленную валюту на баланс игрока. Возвращает
    (новый_баланс, эквивалент_в_звёздах) — второе идёт в donate_total
    (см. _finalize_purchase), чтобы ачивка "Крупный вклад" считалась
    одинаково независимо от способа оплаты и от того, кристаллы это или
    монеты (см. комментарий у _stars_for_coins)."""
    if kind == "crystals":
        balance = await prof.add_crystals(user_id, amount)
        return balance, amount
    await shop.add_balance(user_id, amount)
    balance = await shop.get_balance(user_id)
    return balance, _stars_for_coins(amount)


async def _finalize_purchase(bot, chat_id: int, user_id: int, kind: str, amount: int, lang: str) -> None:
    """Общий хвост успешной оплаты: зачисление баланса + сообщение об
    успехе + ачивки "Первый донат"/"Крупный вклад". Используется и для
    Stars (on_successful_payment), и для CryptoBot/xRocket
    (_finalize_crypto_invoice)."""
    t = TEXTS[lang]
    balance, stars_spent = await _apply_purchase(user_id, kind, amount)

    if kind == "crystals":
        text = t["payment_success"].format(amount=amount, balance=balance)
    else:
        text = t["payment_success_coins"].format(amount=amount, balance=balance)
    await bot.send_message(chat_id, text, reply_markup=_donate_menu_keyboard(lang, user_id))

    achv_result = await achives.unlock(user_id, "first_donate")
    if achv_result:
        await bot.send_message(chat_id, achives.format_unlock_text(lang, achv_result))

    donate_total = await prof.add_donate_total(user_id, stars_spent)
    if donate_total >= 1000:
        achv_result = await achives.unlock(user_id, "general_donate_total_1000")
        if achv_result:
            await bot.send_message(chat_id, achives.format_unlock_text(lang, achv_result))


# ==========================
#   ХРАНЕНИЕ И ПРОВЕРКА КРИПТО-ИНВОЙСОВ (CryptoBot / xRocket)
# ==========================
#
# У бота нет своего HTTPS-сервера для вебхуков (работает через long
# polling), поэтому оплату через CryptoBot/xRocket отслеживаем сами:
#   - crypto_invoices хранит каждый выставленный крипто-инвойс со
#     статусом 'active' -> 'paid'/'expired';
#   - кнопка "Проверить оплату" (on_check_payment) проверяет конкретный
#     инвойс по требованию — для мгновенной обратной связи игроку;
#   - фоновый цикл start_crypto_poll_loop подчищает и проверяет все
#     активные инвойсы сам, на случай если игрок кнопку не нажал.

_crypto_table_ready = False


async def ensure_crypto_table() -> None:
    """Создаёт таблицу crypto_invoices, если её ещё нет — лениво, по
    аналогии с panda.ensure_notify_table/bakery.ensure_achv_tables.
    Вызывается один раз при старте бота (см. main.py)."""
    global _crypto_table_ready
    if _crypto_table_ready:
        return
    db = await database.get_db()
    await db.execute(
        """
        CREATE TABLE IF NOT EXISTS crypto_invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            provider TEXT NOT NULL,
            provider_invoice_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            amount INTEGER NOT NULL,
            lang TEXT NOT NULL,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            created_at REAL NOT NULL
        )
        """
    )
    await database.commit()
    await database.flush()
    _crypto_table_ready = True


async def _create_pending_invoice(
    provider: str, provider_invoice_id: str, user_id: int, kind: str, amount: int,
    lang: str, chat_id: int, message_id: int,
) -> int:
    await ensure_crypto_table()
    db = await database.get_db()
    cursor = await db.execute(
        """
        INSERT INTO crypto_invoices
            (provider, provider_invoice_id, user_id, kind, amount, lang, chat_id, message_id, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)
        """,
        (provider, provider_invoice_id, user_id, kind, amount, lang, chat_id, message_id, time.time()),
    )
    await database.commit()
    return cursor.lastrowid


async def _mark_invoice_status(row_id: int, status: str, only_from: str = "active") -> bool:
    """Атомарно переводит инвойс в новый статус, только если сейчас он в
    статусе only_from — защита от двойного зачисления, если "Проверить
    оплату" и фоновый цикл сработают почти одновременно."""
    db = await database.get_db()
    cursor = await db.execute(
        "UPDATE crypto_invoices SET status = ? WHERE id = ? AND status = ?",
        (status, row_id, only_from),
    )
    await database.commit()
    return cursor.rowcount > 0


async def _finalize_crypto_invoice(bot, row) -> None:
    if not await _mark_invoice_status(row["id"], "paid"):
        return  # кто-то другой (кнопка/фоновый цикл) уже обработал этот инвойс
    await _finalize_purchase(bot, row["chat_id"], row["user_id"], row["kind"], row["amount"], row["lang"])


async def _check_invoice_row(bot, row) -> bool:
    """Проверяет статус одного инвойса у провайдера; при оплате —
    зачисляет. Возвращает True, если инвойс оплачен (только что или
    ранее)."""
    if row["status"] == "paid":
        return True
    if row["status"] != "active":
        return False

    if row["provider"] == "cryptobot":
        status = await crypto_pay.get_cryptobot_invoice_status(row["provider_invoice_id"])
    else:
        status = await crypto_pay.get_xrocket_invoice_status(row["provider_invoice_id"])

    if status == "paid":
        await _finalize_crypto_invoice(bot, row)
        return True

    if status == "expired" or (time.time() - row["created_at"]) > crypto_pay.INVOICE_EXPIRES_IN + 60:
        await _mark_invoice_status(row["id"], "expired")

    return False


@router.callback_query(F.data.startswith("donate:checkpay:"))
async def on_check_payment(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang(state, callback.from_user.id)
    t = TEXTS[lang]

    row_id_raw = callback.data.rsplit(":", 1)[-1]
    if not row_id_raw.isdigit():
        await callback.answer()
        return

    await ensure_crypto_table()
    db = await database.get_db()
    async with db.execute("SELECT * FROM crypto_invoices WHERE id = ?", (int(row_id_raw),)) as cursor:
        row = await cursor.fetchone()

    if row is None or row["user_id"] != callback.from_user.id:
        await callback.answer()
        return

    if row["status"] == "paid":
        await callback.answer(t["check_payment_already"], show_alert=True)
        return

    paid = await _check_invoice_row(callback.bot, row)
    if paid:
        await callback.answer(t["check_payment_already"])
    else:
        await callback.answer(t["check_payment_not_found"], show_alert=True)


CRYPTO_POLL_INTERVAL = 20  # сек между проходами фонового цикла


async def start_crypto_poll_loop(bot) -> None:
    """Фоновый цикл: раз в CRYPTO_POLL_INTERVAL секунд проверяет все
    активные крипто-инвойсы и зачисляет оплаченные — подстраховка на
    случай, если игрок не нажал "Проверить оплату" сам. Запускается один
    раз при старте бота (см. main.py), крутится, пока бот жив."""
    await ensure_crypto_table()
    while True:
        try:
            db = await database.get_db()
            async with db.execute("SELECT * FROM crypto_invoices WHERE status = 'active'") as cursor:
                rows = await cursor.fetchall()
            for row in rows:
                try:
                    await _check_invoice_row(bot, row)
                except Exception:
                    logger.exception("Crypto invoice poll failed for row %s", row["id"] if row else "?")
        except Exception:
            logger.exception("Crypto invoice poll loop iteration failed")
        await asyncio.sleep(CRYPTO_POLL_INTERVAL)


# ==========================
#   ХЕНДЛЕРЫ — ОПЛАТА (TELEGRAM STARS)
# ==========================

@router.pre_checkout_query()
async def on_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    """Цифровой товар без внешних ограничений (склад, доставка и т.п.) —
    подтверждаем всегда сразу. Telegram требует ответ в течение 10 секунд,
    иначе платёж автоматически отменяется."""
    await pre_checkout_query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)

    payload = message.successful_payment.invoice_payload
    parts = payload.split(":")
    if len(parts) != 2 or parts[0] not in ("crystals", "coins") or not parts[1].isdigit():
        # Платёж не от нас (или payload повреждён) — просто ничего не
        # начисляем, чтобы не зачислить валюту за чужой инвойс.
        return

    kind = parts[0]
    amount = int(parts[1])
    await _finalize_purchase(message.bot, message.chat.id, message.from_user.id, kind, amount, lang)
