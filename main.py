import asyncio
import logging
import re

from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import admin
import database
import panda
import garden
import shop
import bakery
import prof
import donate
import achives
import leaders

# ==========================
#   НАСТРОЙКИ
# ==========================
# ВАЖНО: параметр style="primary" у кнопок требует aiogram >= 3.30.0
# (поддержка Telegram Bot API 9.4). Если стоит более старая версия:
#   pip install -U aiogram --break-system-packages

BOT_TOKEN = "8841055640:AAE65cYHaE9XVEo2fQLwZ5kPxrR1Fncqm5Q"

logging.basicConfig(level=logging.INFO)

bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher(storage=MemoryStorage())
router = Router()


# ==========================
#   ПОДКЛЮЧЕНИЕ РОУТЕРОВ
# ==========================
# Вынесено в отдельную функцию и вызывается только при запуске файла как
# скрипта (см. `if __name__ == "__main__":` в самом низу), а не на уровне
# модуля. Причина: разделам бота (например, panda.py — кнопка "Назад" из
# реплай-меню "Облики") нужно иметь возможность безопасно сделать
# `import main`, чтобы брать актуальные TEXTS/main_menu_keyboard() и не
# держать у себя устаревшую копию главного меню. Но main.py запускается
# как __main__, а не как модуль "main", поэтому `import main` из другого
# модуля заново выполняет этот файл целиком под именем "main". Если бы
# dp.include_router(...) вызывались на уровне модуля (как раньше), это
# привело бы к попытке повторно подключить уже подключённые роутеры
# (admin.router, panda.router и т.д.) и падению с
# RuntimeError("Router is already attached") — из-за чего кнопка "Назад"
# вообще переставала отвечать. Теперь при таком повторном импорте эта
# функция просто не вызывается.
def setup_routers() -> None:
    # Учёт игроков для админ-панели (admin.py): нужен на уровне апдейта
    # целиком, а не конкретного роутера, — иначе игроки, ни разу не
    # писавшие /start, не попадут в таблицу users и админ не сможет найти
    # их по @username.
    dp.update.outer_middleware(admin.UserTrackingMiddleware())
    # Дневной стрик заходов в бота — для общих ачивок general_login_streak_7/
    # 30/100 (achives.py). Тоже на уровне апдейта целиком (см. prof.py,
    # LoginStreakMiddleware), а не конкретного роутера — иначе стрик считался
    # бы только для действий в разделе "Профиль".
    dp.update.outer_middleware(prof.LoginStreakMiddleware())
    dp.include_router(router)
    dp.include_router(admin.router)
    dp.include_router(panda.router)
    dp.include_router(garden.router)
    dp.include_router(shop.router)
    dp.include_router(bakery.router)
    dp.include_router(prof.router)
    dp.include_router(donate.router)
    dp.include_router(achives.router)
    dp.include_router(leaders.router)


# ==========================
#   КАРТИНКА ДЛЯ /START
# ==========================
# Раньше картинка, отправляемая вместе с выбором языка на /start,
# загружалась командой /setimg1 (реплаем на фото) и хранилась отдельным
# файлом bot_data.json. Теперь она — часть общей системы "картинок
# разделов" в админ-панели (admin.py: admin:sections, ключ "start"),
# вместе с картинками для Достижений/Сада/Пекарни/Профиля/Донатов —
# см. admin.get_section_image/set_section_image ниже.


# ==========================
#   СОСТОЯНИЯ (FSM)
# ==========================

class Onboarding(StatesGroup):
    choosing_language = State()
    choosing_gender = State()
    finished = State()


class TransferFlow(StatesGroup):
    """Передача фруктов (из корзины сада), выпечки (с витрины пекарни),
    монет (Pn) или кристаллов другому игроку: /передать (и алиасы) ->
    получатель (реплай или @username/ID) -> источник (корзина/витрина/
    монеты/кристаллы) -> для корзины/витрины ещё и конкретный предмет ->
    количество."""

    waiting_target = State()
    choosing_amount = State()


# ==========================
#   ТЕКСТЫ И ЛОКАЛИЗАЦИЯ
# ==========================

LANGUAGES = {
    "ru": "🇷🇺 Русский",
    "en": "🇬🇧 English",
}

CUSTOM_EMOJI_ID = "5798415229255687356"
CUSTOM_EMOJI = f'<tg-emoji emoji-id="{CUSTOM_EMOJI_ID}">🔹</tg-emoji>'

GENDER_TEXT_EMOJI_ID = "5292122921035133343"
GENDER_TEXT_EMOJI = f'<tg-emoji emoji-id="{GENDER_TEXT_EMOJI_ID}">👤</tg-emoji>'

MALE_EMOJI_ID = "5314678809373450691"
FEMALE_EMOJI_ID = "5316790060677341262"

FINAL_EMOJI_ID = "5461151367559141950"
FINAL_EMOJI = f'<tg-emoji emoji-id="{FINAL_EMOJI_ID}">🎉</tg-emoji>'

START_BUTTON_EMOJI_ID = "5456140674028019486"

MENU_OPENED_EMOJI_ID = "5406595657878220722"
MENU_OPENED_EMOJI = f'<tg-emoji emoji-id="{MENU_OPENED_EMOJI_ID}">🚀</tg-emoji>'

GARDEN_EMOJI_ID = "5422407403884798028"
MARKET_EMOJI_ID = "5920332557466997677"
PANDA_EMOJI_ID = "5344057622628671718"
PROFILE_EMOJI_ID = "5413450253883944453"  # тот же id, что и prof.CE_PROFILE (🤑)
LOOKS_EMOJI_ID = "6032625495328165724"
DONATE_EMOJI_ID = "5251273203615031474"  # тот же id, что и donate.CRYSTAL_EMOJI_ID
# Тот же id, что и achives.UNLOCK_HEADER_EMOJI_ID (🎖) — там им же
# оформлен заголовок уведомления об открытой ачивке и шапка раздела
# "Достижения", здесь — иконка кнопки в главном меню.
ACHIEVEMENTS_EMOJI_ID = "5150415989841593609"
LEADERS_EMOJI_ID = "5413566144986503832"

CHOOSE_LANGUAGE_TEXT = (
    f"{CUSTOM_EMOJI} <i>Выберите удобный для вас язык</i>\n"
    "<code>·  ·  ·  ◆  ·  ·  ·</code>\n"
    f"{CUSTOM_EMOJI} <i>Choose your preferred language</i>"
)

TEXTS = {
    "ru": {
        "choose_gender": f"{GENDER_TEXT_EMOJI} <i><b>Выберите ваш пол</b></i>\n\n<i>Это поможет нам настроить бота специально для вас.</i>",
        "male": "Мужской",
        "female": "Женский",
        "final_message": (
            f"{FINAL_EMOJI} <i><b>Отлично, всё готово!</b></i>\n\n"
            "<i>Если хотите узнать, как всё устроено — загляните в гайд ниже.</i>"
        ),
        "guide_button": "📖 Гайд",
        "start_button": "Начинаем!",
        "menu_garden": "Сад",
        "menu_market": "Рынок",
        "menu_panda": "Моя панда",
        "menu_bakery": "🥐 Пекарня",
        "menu_profile": "Профиль",
        "menu_looks": "Облики",
        "menu_donate": "Донаты",
        "menu_achievements": "Достижения",
        "menu_leaders": "Лидеры",
        "menu_opened": f"{MENU_OPENED_EMOJI} <i><b>Ну что, начинаем! Желаю удачи!</b></i>",
        "balance_command_line": f"{shop.CE_BALANCE} <b>{{coins}}</b> | {prof.CE_CRYSTAL} <b>{{crystals}}</b>",
    },
    "en": {
        "choose_gender": f"{GENDER_TEXT_EMOJI} <i><b>Choose your gender</b></i>\n\n<i>This will help us personalize the bot for you.</i>",
        "male": "Male",
        "female": "Female",
        "final_message": (
            f"{FINAL_EMOJI} <i><b>Great, everything is ready!</b></i>\n\n"
            "<i>If you'd like to see how it works — check out the guide below.</i>"
        ),
        "guide_button": "📖 Guide",
        "start_button": "Let's go!",
        "menu_garden": "Garden",
        "menu_market": "Market",
        "menu_panda": "My panda",
        "menu_bakery": "🥐 Bakery",
        "menu_profile": "Profile",
        "menu_looks": "Looks",
        "menu_donate": "Donate",
        "menu_achievements": "Achievements",
        "menu_leaders": "Leaders",
        "menu_opened": f"{MENU_OPENED_EMOJI} <i><b>Alright, let's begin! Good luck!</b></i>",
        "balance_command_line": f"{shop.CE_BALANCE} <b>{{coins}}</b> | {prof.CE_CRYSTAL} <b>{{crystals}}</b>",
    },
}


# ==========================
#   ТЕКСТЫ — ПЕРЕДАЧА ПРЕДМЕТОВ (/передать)
# ==========================

TRANSFER_TEXTS = {
    "ru": {
        "ask_target": (
            "🎁 <i>Кому хотите передать?</i>\n"
            "<i>Ответьте этой командой на сообщение игрока, либо укажите его @username или ID.</i>"
        ),
        "target_not_found": "⚠️ <i>Не нашёл такого игрока — либо он ни разу не писал боту, либо в username опечатка.</i>",
        "target_self": "❌ <i>Нельзя передать самому себе.</i>",
        "choose_source": "📦 <i>Откуда передать?</i>",
        "btn_basket": "🧺 Корзина",
        "btn_showcase": "🍽 Витрина",
        "btn_coins": "Монеты",
        "btn_crystals": "Кристаллы",
        "empty_basket": "<i>У вас пока нет фруктов в корзине.</i>",
        "empty_showcase": "<i>У вас пока нет выпечки на витрине.</i>",
        "empty_coins": f"<i>У вас пока нет {shop.CURRENCY}.</i>",
        "empty_crystals": f"<i>У вас пока нет {prof.CE_CRYSTAL} кристаллов.</i>",
        "choose_item": "<i>Что хотите передать?</i>",
        "item_button": "{emoji} {name} ×{count}",
        "ask_qty": "<i>Сколько {emoji} {name} передать?</i>\n<i>Сейчас у вас: {count}</i>",
        "ask_qty_currency": "<i>Сколько {currency_icon} передать?</i>\n<i>Сейчас у вас: {count}</i>",
        "qty_invalid": "⚠️ <i>Введите положительное целое число.</i>",
        "qty_too_much": "⚠️ <i>У вас только {count} шт. — введите число не больше этого.</i>",
        "qty_too_much_currency": "⚠️ <i>У вас только {count} — введите число не больше этого.</i>",
        "sent_confirm": "✅ <b>Передано</b> {target}: {emoji} {name} ×{count}",
        "sent_confirm_currency": "✅ <b>Передано</b> {target}: {amount} {currency_icon}",
        "received_notice": "🎁 <b>{sender}</b> передал(а) вам: {emoji} {name} ×{count}",
        "received_notice_currency": "🎁 <b>{sender}</b> передал(а) вам {amount} {currency_icon}",
    },
    "en": {
        "ask_target": (
            "🎁 <i>Who would you like to send this to?</i>\n"
            "<i>Reply to the player's message with this command, or give their @username or ID.</i>"
        ),
        "target_not_found": "⚠️ <i>Couldn't find that player — either they've never messaged the bot, or the username is off.</i>",
        "target_self": "❌ <i>You can't send items to yourself.</i>",
        "choose_source": "📦 <i>Send from where?</i>",
        "btn_basket": "🧺 Basket",
        "btn_showcase": "🍽 Showcase",
        "btn_coins": "Coins",
        "btn_crystals": "Crystals",
        "empty_basket": "<i>You don't have any fruit in your basket yet.</i>",
        "empty_showcase": "<i>You don't have any baked goods on your showcase yet.</i>",
        "empty_coins": f"<i>You don't have any {shop.CURRENCY} yet.</i>",
        "empty_crystals": f"<i>You don't have any {prof.CE_CRYSTAL} crystals yet.</i>",
        "choose_item": "<i>What would you like to send?</i>",
        "item_button": "{emoji} {name} ×{count}",
        "ask_qty": "<i>How many {emoji} {name} to send?</i>\n<i>You currently have: {count}</i>",
        "ask_qty_currency": "<i>How many {currency_icon} to send?</i>\n<i>You currently have: {count}</i>",
        "qty_invalid": "⚠️ <i>Enter a positive whole number.</i>",
        "qty_too_much": "⚠️ <i>You only have {count} — enter a number no higher than that.</i>",
        "qty_too_much_currency": "⚠️ <i>You only have {count} — enter a number no higher than that.</i>",
        "sent_confirm": "✅ <b>Sent</b> to {target}: {emoji} {name} ×{count}",
        "sent_confirm_currency": "✅ <b>Sent</b> to {target}: {amount} {currency_icon}",
        "received_notice": "🎁 <b>{sender}</b> sent you: {emoji} {name} ×{count}",
        "received_notice_currency": "🎁 <b>{sender}</b> sent you {amount} {currency_icon}",
    },
}


# ==========================
#   КЛАВИАТУРЫ
# ==========================

def language_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    for code, title in LANGUAGES.items():
        builder.button(text=title, callback_data=f"lang:{code}", style="primary")
    builder.adjust(2)
    return builder.as_markup()


def gender_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["male"],
        callback_data="gender:male",
        style="primary",
        icon_custom_emoji_id=MALE_EMOJI_ID,
    )
    builder.button(
        text=t["female"],
        callback_data="gender:female",
        style="primary",
        icon_custom_emoji_id=FEMALE_EMOJI_ID,
    )
    builder.adjust(2)
    return builder.as_markup()


def guide_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["guide_button"],
        url="https://telegra.ph/LazyPanda--Polnyj-gajd-dlya-igrokov-08-11",
        style="primary",
    )
    builder.button(
        text=t["start_button"],
        callback_data="start_now",
        style="primary",
        icon_custom_emoji_id=START_BUTTON_EMOJI_ID,
    )
    builder.adjust(1, 1)
    return builder.as_markup()


def guide_keyboard_started(lang: str) -> InlineKeyboardBuilder:
    """Та же клавиатура, но кнопка 'Начинаем!' уже нажата и больше не работает."""
    t = TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(
        text=t["guide_button"],
        url="https://telegra.ph/LazyPanda--Polnyj-gajd-dlya-igrokov-08-11",
        style="primary",
    )
    builder.button(
        text=t["start_button"],
        callback_data="start_now_dead",
        style="primary",
        icon_custom_emoji_id=START_BUTTON_EMOJI_ID,
    )
    builder.adjust(1, 1)
    return builder.as_markup()


def main_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    t = TEXTS[lang]
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=t["menu_garden"],
                    style="primary",
                    icon_custom_emoji_id=GARDEN_EMOJI_ID,
                ),
                KeyboardButton(
                    text=t["menu_market"],
                    style="primary",
                    icon_custom_emoji_id=MARKET_EMOJI_ID,
                ),
                KeyboardButton(
                    text=t["menu_panda"],
                    style="primary",
                    icon_custom_emoji_id=PANDA_EMOJI_ID,
                ),
            ],
            [
                KeyboardButton(
                    text=t["menu_bakery"],
                    style="primary",
                ),
                KeyboardButton(
                    text=t["menu_profile"],
                    style="primary",
                    icon_custom_emoji_id=PROFILE_EMOJI_ID,
                ),
                KeyboardButton(
                    text=t["menu_looks"],
                    style="primary",
                    icon_custom_emoji_id=LOOKS_EMOJI_ID,
                ),
            ],
            [
                KeyboardButton(
                    text=t["menu_donate"],
                    style="primary",
                    icon_custom_emoji_id=DONATE_EMOJI_ID,
                ),
                KeyboardButton(
                    text=t["menu_achievements"],
                    style="primary",
                    icon_custom_emoji_id=ACHIEVEMENTS_EMOJI_ID,
                ),
            ],
            [
                KeyboardButton(
                    text=t["menu_leaders"],
                    style="primary",
                    icon_custom_emoji_id=LEADERS_EMOJI_ID,
                ),
            ],
        ],
        resize_keyboard=True,
    )


async def update_message(callback: CallbackQuery, text: str, reply_markup=None) -> None:
    """Обновляет сообщение: если это фото — меняет подпись, иначе — текст."""
    if callback.message.photo:
        await callback.message.edit_caption(caption=text, reply_markup=reply_markup)
    else:
        await callback.message.edit_text(text, reply_markup=reply_markup)


# ==========================
#   ХЕНДЛЕРЫ
# ==========================

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, command: CommandObject):
    await state.clear()

    # Игрок уже проходил онбординг раньше (в т.ч. до рестарта бота —
    # FSM-хранилище это не переживает, поэтому сверяемся с БД) —
    # сразу открываем меню, без выбора языка/пола заново.
    onboarding = await database.get_onboarding(message.from_user.id)
    if onboarding is not None:
        lang = onboarding["lang"] or "ru"
        gender = onboarding["gender"] or ""

        await state.update_data(lang=lang, gender=gender)
        await state.set_state(Onboarding.finished)

        t = TEXTS[lang]
        await message.answer(
            t["menu_opened"],
            reply_markup=main_menu_keyboard(lang),
        )
        return

    # Реферальная ссылка (prof.py, раздел "Друзья"): /start ref<id> —
    # запоминаем, кто пригласил, только для по-настоящему новых игроков
    # (ветка выше уже вернулась бы для тех, кто онбординг прошёл раньше).
    # Сама награда пригласившему начисляется позже, только после того,
    # как этот игрок выберет язык И пол — см. process_gender ниже,
    # prof.credit_referral. database.set_referrer сам не даст
    # перезаписать уже сохранённого пригласившего повторным переходом
    # по чужой ссылке.
    payload = command.args or ""
    if payload.startswith("ref"):
        ref_part = payload[3:]
        if ref_part.isdigit():
            referrer_id = int(ref_part)
            if referrer_id != message.from_user.id:
                await database.set_referrer(message.from_user.id, referrer_id)
    elif payload.startswith("ad_"):
        # Рекламная ссылка из админки (admin.py: "📢 Рекламные ссылки") —
        # /start ad_<slug>. Засчитывается только для по-настоящему новых
        # игроков (см. ветку выше), "вступил(а)" отмечается позже, в
        # process_gender, — см. admin.record_ad_click/mark_ad_join.
        slug = payload[3:]
        if slug:
            await admin.record_ad_click(message.from_user.id, slug)

    await state.set_state(Onboarding.choosing_language)

    start_image = await admin.get_section_image("start")

    if start_image:
        await message.answer_photo(
            photo=start_image,
            caption=CHOOSE_LANGUAGE_TEXT,
            reply_markup=language_keyboard(),
        )
    else:
        await message.answer(
            CHOOSE_LANGUAGE_TEXT,
            reply_markup=language_keyboard(),
        )


# Триггеры команды баланса: работают и со слешем (/баланс), и без него
# (просто отправить текстом), регистр не важен.
BALANCE_TRIGGERS = ["б", "b", "bal", "balance", "бал", "баланс"]


def _is_balance_trigger(message: Message) -> bool:
    text = message.text or ""
    if text.startswith("/"):
        # Отрезаем слеш и возможный "@ИмяБота" (/balance@MyBot в группах)
        text = text[1:].split("@", 1)[0]
    return text.strip().lower() in BALANCE_TRIGGERS


@router.message(_is_balance_trigger)
async def cmd_balance(message: Message):
    onboarding = await database.get_onboarding(message.from_user.id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"

    coins = await shop.get_balance(message.from_user.id)
    crystals = await prof.get_crystals(message.from_user.id)

    t = TEXTS[lang]
    await message.reply(
        t["balance_command_line"].format(coins=coins, crystals=crystals)
    )


# ==========================
#   ТЕКСТОВЫЕ КОМАНДЫ-ЯРЛЫКИ ДЛЯ РАЗДЕЛОВ
# ==========================
# Каждый раздел уже открывается по нажатию кнопки реплай-меню — сам
# хендлер слушает F.text.in_(BUTTON_TEXT.values()) и не завязан на
# конкретное FSM-состояние (см. garden.py/shop.py/bakery.py/prof.py/
# donate.py/achives.py/leaders.py). Поэтому ярлыки ниже просто вызывают
# ту же самую функцию открытия раздела напрямую — это гарантированно
# тот же экран, что и по кнопке, без дублирования вёрстки/логики.
#
# "Корзина" — это часть экрана сада (там же, где грядки), а "витрина" —
# часть экрана пекарни, отдельных экранов под них нет, поэтому эти
# алиасы ведут в сад/пекарню соответственно.

def _text_trigger_filter(triggers: set[str]):
    def _filter(message: Message) -> bool:
        text = message.text or ""
        if text.startswith("/"):
            text = text[1:].split("@", 1)[0]
        return text.strip().lower() in triggers

    return _filter


GARDEN_TRIGGERS = {"сад", "garden", "корзина", "кор", "фрукты", "basket", "fruits"}
PROFILE_TRIGGERS = {"я", "проф", "профиль", "me", "prof", "profile"}
BAKERY_TRIGGERS = {"пек", "пекарня", "bakery", "bak", "вит", "витрина", "showcase", "vit"}
MARKET_TRIGGERS = {"рынок", "лоты", "market", "lots"}
PANDA_TRIGGERS = {"панда", "п", "кормить", "panda", "feed"}
LEADERS_TRIGGERS = {"лидеры", "лид", "топ", "leaders", "top"}
DONATE_TRIGGERS = {"донат", "дон", "donate", "don"}
ACHIEVEMENTS_TRIGGERS = {"ач", "достижение", "ачивки", "дос", "achievements", "ach"}


@router.message(_text_trigger_filter(GARDEN_TRIGGERS))
async def cmd_garden_shortcut(message: Message, state: FSMContext) -> None:
    await garden.open_garden(message, state)


@router.message(_text_trigger_filter(PROFILE_TRIGGERS))
async def cmd_profile_shortcut(message: Message, state: FSMContext) -> None:
    await prof.open_profile(message, state)


@router.message(_text_trigger_filter(BAKERY_TRIGGERS))
async def cmd_bakery_shortcut(message: Message, state: FSMContext) -> None:
    await bakery.open_bakery(message, state)


@router.message(_text_trigger_filter(MARKET_TRIGGERS))
async def cmd_market_shortcut(message: Message, state: FSMContext) -> None:
    await shop.open_market(message, state)


@router.message(_text_trigger_filter(PANDA_TRIGGERS))
async def cmd_panda_shortcut(message: Message, state: FSMContext) -> None:
    await panda.open_panda(message, state)


@router.message(_text_trigger_filter(LEADERS_TRIGGERS))
async def cmd_leaders_shortcut(message: Message, state: FSMContext) -> None:
    await leaders.open_leaders(message, state)


@router.message(_text_trigger_filter(DONATE_TRIGGERS))
async def cmd_donate_shortcut(message: Message, state: FSMContext) -> None:
    await donate.open_donate_menu(message, state)


@router.message(_text_trigger_filter(ACHIEVEMENTS_TRIGGERS))
async def cmd_achievements_shortcut(message: Message, state: FSMContext) -> None:
    await achives.open_achievements(message, state)


# ==========================
#   ПЕРЕДАЧА ПРЕДМЕТОВ ИГРОКУ (/передать)
# ==========================
# Игрок передаёт фрукты из своей корзины (garden), выпечку со своей
# витрины (bakery), монеты (Pn) или кристаллы другому игроку. Получатель —
# либо тот, чьё сообщение зареплаили этой командой, либо @username/ID,
# указанный сразу после слова-триггера (см. TRANSFER_TRIGGERS ниже),
# либо (если ничего из этого нет) запрашивается отдельным сообщением.
# Дальше — выбор источника (корзина/витрина/монеты/кристаллы); для
# корзины/витрины ещё и конкретного предмета, затем количество.
# Списание/начисление идёт теми же атомарными функциями, что использует
# рынок и остальной бот: garden.take_from_basket_bulk/add_to_basket,
# bakery.take_from_pantry_bulk/add_to_pantry для предметов;
# prof._spend_coins/shop.add_balance для монет; prof._spend_crystals/
# prof.add_crystals для кристаллов — гонки исключены на уровне
# database.user_lock (валюты) либо самого SQL UPDATE (предметы).

TRANSFER_TRIGGERS = {"передать", "дать", "пер", "gift", "give", "send"}

# Иконка+название валюты для сообщений (текст поддерживает HTML, поэтому
# тег <tg-emoji> там рендерится нормально) в разделе "Монеты"/"Кристаллы".
_TRANSFER_CURRENCY_ICON = {
    "coins": f"{shop.CE_BALANCE} {shop.CURRENCY}",
    "crystals": f"{prof.CE_CRYSTAL}",
}

# Для КНОПОК (в отличие от обычного текста) тег <tg-emoji> не рендерится —
# Telegram inline-кнопки не поддерживают HTML в тексте. Чтобы всё же
# показать премиальный кастомный эмодзи на кнопке, id нужно передавать
# отдельным полем icon_custom_emoji_id (см. builder.button ниже) — точно
# так же, как это уже сделано для кнопки "Выдать кристаллы" в
# admin._build_main_menu(). Для кристаллов id уже есть готовой константой
# (prof.EMOJI_CRYSTAL_ID); для монет у shop.py нет отдельной "сырой"
# id-константы — вытаскиваем её из HTML-тега shop.CE_BALANCE регуляркой.
_CUSTOM_EMOJI_TAG_RE = re.compile(r'<tg-emoji emoji-id="(\d+)">')


def _custom_emoji_id_from_tag(tag: str) -> str | None:
    match = _CUSTOM_EMOJI_TAG_RE.search(tag)
    return match.group(1) if match else None


_TRANSFER_COIN_ICON_ID = _custom_emoji_id_from_tag(shop.CE_BALANCE)
_TRANSFER_CRYSTAL_ICON_ID = prof.EMOJI_CRYSTAL_ID


def _transfer_command_filter(message: Message) -> bool:
    text = message.text or ""
    if text.startswith("/"):
        text = text[1:]
    parts = text.strip().split(maxsplit=1)
    if not parts:
        return False
    # На случай /give@ИмяБота аргумент — часть первого слова
    cmd = parts[0].split("@", 1)[0].lower()
    return cmd in TRANSFER_TRIGGERS


async def _get_lang_for(user_id: int) -> str:
    onboarding = await database.get_onboarding(user_id)
    return (onboarding["lang"] if onboarding else None) or "ru"


def _fmt_transfer_target(user_id: int, username: str | None) -> str:
    if username:
        return f"@{username}"
    return f"<code>{user_id}</code>"


def _transfer_source_keyboard(lang: str) -> InlineKeyboardBuilder:
    t = TRANSFER_TEXTS[lang]
    builder = InlineKeyboardBuilder()
    builder.button(text=t["btn_basket"], callback_data="transfer:source:garden", style="primary")
    builder.button(text=t["btn_showcase"], callback_data="transfer:source:bakery", style="primary")
    coin_kwargs = {"icon_custom_emoji_id": _TRANSFER_COIN_ICON_ID} if _TRANSFER_COIN_ICON_ID else {}
    builder.button(
        text=t["btn_coins"], callback_data="transfer:source:coins", style="primary", **coin_kwargs
    )
    builder.button(
        text=t["btn_crystals"],
        callback_data="transfer:source:crystals",
        style="primary",
        icon_custom_emoji_id=_TRANSFER_CRYSTAL_ICON_ID,
    )
    builder.adjust(2)
    return builder.as_markup()


def _is_navigation_text(raw: str) -> bool:
    """Похоже ли сообщение на попытку уйти в другой раздел/команду, а не
    на ввод данных для /передать. Нужно, чтобы TransferFlow.waiting_target
    / .choosing_amount не проглатывали нажатия кнопок меню, пока перевод
    не завершён (тот же баг, что чинили в bakery.py: состояние висит,
    и любое следующее сообщение ловится этим хендлером вместо открытия
    нужного раздела)."""
    raw = raw.strip()
    if not raw:
        return False
    if raw.startswith("/"):
        return True

    triggers: set[str] = set()
    for group in (
        GARDEN_TRIGGERS,
        PROFILE_TRIGGERS,
        BAKERY_TRIGGERS,
        MARKET_TRIGGERS,
        PANDA_TRIGGERS,
        LEADERS_TRIGGERS,
        DONATE_TRIGGERS,
        ACHIEVEMENTS_TRIGGERS,
    ):
        triggers.update(group)

    button_texts: set[str] = set()
    for lang_texts in TEXTS.values():
        for key, value in lang_texts.items():
            if key.startswith("menu_") and key != "menu_opened":
                button_texts.add(value)

    return raw.lower() in triggers or raw in button_texts


async def _ask_transfer_source(message: Message, state: FSMContext, lang: str, user_id: int, username: str | None) -> None:
    await state.update_data(transfer_target_id=user_id, transfer_target_username=username)
    await state.set_state(None)
    await message.reply(
        TRANSFER_TEXTS[lang]["choose_source"],
        reply_markup=_transfer_source_keyboard(lang),
    )


@router.message(_transfer_command_filter)
async def cmd_transfer_start(message: Message, state: FSMContext) -> None:
    lang = await _get_lang_for(message.from_user.id)
    t = TRANSFER_TEXTS[lang]

    text = message.text or ""
    if text.startswith("/"):
        text = text[1:]
    parts = text.strip().split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""

    target: tuple[int, str | None] | None = None

    if arg:
        target = await admin.resolve_target(arg)
        if target is None:
            await message.reply(t["target_not_found"])
            return
    elif message.reply_to_message and message.reply_to_message.from_user and not message.reply_to_message.from_user.is_bot:
        ru = message.reply_to_message.from_user
        target = (ru.id, ru.username)

    if target is None:
        await state.set_state(TransferFlow.waiting_target)
        await message.reply(t["ask_target"])
        return

    user_id, username = target
    if user_id == message.from_user.id:
        await message.reply(t["target_self"])
        return

    await _ask_transfer_source(message, state, lang, user_id, username)


@router.message(TransferFlow.waiting_target)
async def cmd_transfer_target_input(message: Message, state: FSMContext) -> None:
    raw = message.text or ""
    if _is_navigation_text(raw):
        await state.set_state(None)
        raise SkipHandler

    lang = await _get_lang_for(message.from_user.id)
    t = TRANSFER_TEXTS[lang]

    target = await admin.resolve_target(message.text or "")
    if target is None:
        await message.reply(t["target_not_found"])
        return

    user_id, username = target
    if user_id == message.from_user.id:
        await message.reply(t["target_self"])
        return

    await _ask_transfer_source(message, state, lang, user_id, username)


@router.callback_query(F.data.startswith("transfer:source:"))
async def cb_transfer_source(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang_for(callback.from_user.id)
    t = TRANSFER_TEXTS[lang]
    source = callback.data.split(":")[2]

    data = await state.get_data()
    target_id = data.get("transfer_target_id")
    if target_id is None:
        await callback.answer()
        await state.set_state(TransferFlow.waiting_target)
        await callback.message.edit_text(t["ask_target"])
        return

    if source in ("coins", "crystals"):
        if source == "coins":
            owned = await shop.get_balance(callback.from_user.id)
        else:
            owned = await prof.get_crystals(callback.from_user.id)
        empty_text = t["empty_coins"] if source == "coins" else t["empty_crystals"]

        if owned <= 0:
            await callback.answer()
            await callback.message.edit_text(empty_text)
            return

        await state.update_data(transfer_source=source, transfer_item_id=None)
        await state.set_state(TransferFlow.choosing_amount)
        await callback.answer()
        await callback.message.edit_text(
            t["ask_qty_currency"].format(
                currency_icon=_TRANSFER_CURRENCY_ICON[source], count=owned
            )
        )
        return

    if source == "garden":
        inventory = await garden.get_inventory(callback.from_user.id)
        items = [(cid, garden.CROPS[cid]) for cid in garden.CROP_ORDER if inventory.get(cid, 0) > 0]
        empty_text = t["empty_basket"]
    else:
        pantry = await bakery.get_pantry(callback.from_user.id)
        items = [(rid, bakery.RECIPES[rid]) for rid in bakery.RECIPE_ORDER if pantry.get(rid, 0) > 0]
        empty_text = t["empty_showcase"]
        inventory = pantry

    if not items:
        await callback.answer()
        await callback.message.edit_text(empty_text)
        return

    builder = InlineKeyboardBuilder()
    for item_id, item in items:
        builder.button(
            text=t["item_button"].format(
                emoji=item["emoji"], name=item["name"][lang], count=inventory[item_id]
            ),
            callback_data=f"transfer:item:{source}:{item_id}",
            style="primary",
        )
    builder.adjust(1)

    await callback.answer()
    await callback.message.edit_text(t["choose_item"], reply_markup=builder.as_markup())


@router.callback_query(F.data.startswith("transfer:item:"))
async def cb_transfer_item(callback: CallbackQuery, state: FSMContext) -> None:
    lang = await _get_lang_for(callback.from_user.id)
    t = TRANSFER_TEXTS[lang]
    _, _, source, item_id = callback.data.split(":", 3)

    data = await state.get_data()
    target_id = data.get("transfer_target_id")
    if target_id is None:
        await callback.answer()
        await state.set_state(TransferFlow.waiting_target)
        await callback.message.edit_text(t["ask_target"])
        return

    if source == "garden":
        inventory = await garden.get_inventory(callback.from_user.id)
        count = inventory.get(item_id, 0)
        item = garden.CROPS[item_id]
        empty_text = t["empty_basket"]
    else:
        pantry = await bakery.get_pantry(callback.from_user.id)
        count = pantry.get(item_id, 0)
        item = bakery.RECIPES[item_id]
        empty_text = t["empty_showcase"]

    if count <= 0:
        await callback.answer()
        await callback.message.edit_text(empty_text)
        return

    await state.update_data(transfer_source=source, transfer_item_id=item_id)
    await state.set_state(TransferFlow.choosing_amount)
    await callback.answer()
    await callback.message.edit_text(
        t["ask_qty"].format(emoji=item["emoji"], name=item["name"][lang], count=count)
    )


@router.message(TransferFlow.choosing_amount)
async def cmd_transfer_amount(message: Message, state: FSMContext, bot: Bot) -> None:
    raw = (message.text or "").strip()
    if _is_navigation_text(raw):
        await state.set_state(None)
        raise SkipHandler

    lang = await _get_lang_for(message.from_user.id)
    t = TRANSFER_TEXTS[lang]

    if not raw.isdigit() or int(raw) <= 0:
        await message.reply(t["qty_invalid"])
        return
    qty = int(raw)

    data = await state.get_data()
    target_id = data.get("transfer_target_id")
    target_username = data.get("transfer_target_username")
    source = data.get("transfer_source")
    item_id = data.get("transfer_item_id")

    # item_id обязателен только для предметных источников (корзина/
    # витрина) — для монет/кристаллов его нет, там переводится сам
    # баланс, поэтому transfer_item_id намеренно ставится в None (см.
    # cb_transfer_source), а не пропущен по ошибке.
    if target_id is None or source is None:
        await state.set_state(None)
        await message.reply(t["ask_target"])
        return
    if source in ("garden", "bakery") and item_id is None:
        await state.set_state(None)
        await message.reply(t["ask_target"])
        return

    sender_id = message.from_user.id

    if source in ("coins", "crystals"):
        if source == "coins":
            owned = await shop.get_balance(sender_id)
        else:
            owned = await prof.get_crystals(sender_id)

        if qty > owned:
            await message.reply(t["qty_too_much_currency"].format(count=owned))
            return

        if source == "coins":
            ok = await prof._spend_coins(sender_id, qty)
            if ok:
                await shop.add_balance(target_id, qty)
        else:
            ok = await prof._spend_crystals(sender_id, qty)
            if ok:
                await prof.add_crystals(target_id, qty)

        if not ok:
            # Гонка: баланс успел измениться между проверкой и списанием
            # (например, второй одновременный /передать) — атомарная
            # проверка внутри _spend_coins/_spend_crystals просто не
            # сработала, ничего не потерялось.
            fresh_owned = owned
            await message.reply(t["qty_too_much_currency"].format(count=fresh_owned))
            return

        await state.set_state(None)

        target_display = _fmt_transfer_target(target_id, target_username)
        currency_icon = _TRANSFER_CURRENCY_ICON[source]
        await message.reply(
            t["sent_confirm_currency"].format(
                target=target_display, amount=qty, currency_icon=currency_icon
            )
        )

        sender_display = (
            f"@{message.from_user.username}" if message.from_user.username else (message.from_user.first_name or "")
        )
        recipient_lang = await _get_lang_for(target_id)
        try:
            await bot.send_message(
                target_id,
                TRANSFER_TEXTS[recipient_lang]["received_notice_currency"].format(
                    sender=sender_display, amount=qty, currency_icon=currency_icon
                ),
            )
        except Exception:
            pass
        return

    if source == "garden":
        inventory = await garden.get_inventory(sender_id)
        owned = inventory.get(item_id, 0)
        item = garden.CROPS[item_id]
    else:
        pantry = await bakery.get_pantry(sender_id)
        owned = pantry.get(item_id, 0)
        item = bakery.RECIPES[item_id]

    if qty > owned:
        await message.reply(t["qty_too_much"].format(count=owned))
        return

    if source == "garden":
        ok = await garden.take_from_basket_bulk(sender_id, item_id, qty)
        if ok:
            await garden.add_to_basket(target_id, item_id, qty)
    else:
        ok = await bakery.take_from_pantry_bulk(sender_id, item_id, qty)
        if ok:
            await bakery.add_to_pantry(target_id, item_id, qty)

    if not ok:
        # Кто-то успел забрать/потратить предмет между проверкой и списанием
        # (например, второй одновременный /передать) — атомарный UPDATE
        # просто не сработал, ничего не потерялось.
        fresh_owned = owned  # для сообщения показываем последнее известное значение
        await message.reply(t["qty_too_much"].format(count=fresh_owned))
        return

    await state.set_state(None)

    target_display = _fmt_transfer_target(target_id, target_username)
    await message.reply(
        t["sent_confirm"].format(
            target=target_display, emoji=item["emoji"], name=item["name"][lang], count=qty
        )
    )

    sender_display = (
        f"@{message.from_user.username}" if message.from_user.username else (message.from_user.first_name or "")
    )
    recipient_lang = await _get_lang_for(target_id)
    try:
        await bot.send_message(
            target_id,
            TRANSFER_TEXTS[recipient_lang]["received_notice"].format(
                sender=sender_display, emoji=item["emoji"], name=item["name"][recipient_lang], count=qty
            ),
        )
    except Exception:
        pass


@router.callback_query(Onboarding.choosing_language, F.data.startswith("lang:"))
async def process_language(callback: CallbackQuery, state: FSMContext):
    lang = callback.data.split(":")[1]
    await state.update_data(lang=lang)
    await state.set_state(Onboarding.choosing_gender)

    t = TEXTS[lang]

    # Сначала гасим "часики" на кнопке (это мгновенно), и только потом
    # редактируем сообщение — само редактирование это отдельный сетевой
    # запрос к Telegram, и пока callback.answer() не вызван, кнопка у
    # пользователя продолжает крутить индикатор загрузки, из-за чего
    # кажется, что бот "долго думает".
    await callback.answer()
    await update_message(
        callback,
        t["choose_gender"],
        reply_markup=gender_keyboard(lang),
    )


@router.callback_query(Onboarding.choosing_gender, F.data.startswith("gender:"))
async def process_gender(callback: CallbackQuery, state: FSMContext):
    gender = callback.data.split(":")[1]
    data = await state.get_data()
    lang = data.get("lang", "ru")

    await state.update_data(gender=gender)
    await state.set_state(Onboarding.finished)

    t = TEXTS[lang]

    # См. комментарий в process_language выше — answer() до edit-запроса,
    # а не после.
    await callback.answer()
    await update_message(
        callback,
        t["final_message"],
        reply_markup=guide_keyboard(lang),
    )

    # Реферальная награда (prof.py, раздел "Друзья") — начисляется
    # тому, кто пригласил этого игрока, ровно в момент, когда игрок
    # выбрал язык И пол (оба шага онбординга пройдены). Простой /start
    # по ссылке без выбора языка/пола наградой не считается —
    # prof.credit_referral сама проверяет, что реферер вообще есть, и
    # что награда за этого игрока ещё не начислялась (идемпотентно).
    await prof.credit_referral(callback.from_user, callback.bot)

    # Статистика рекламных ссылок (admin.py: "📢 Рекламные ссылки") —
    # отмечаем "вступил(а)" в тот же момент, что и реферальную награду
    # выше. Если игрок пришёл не по рекламной ссылке — ничего не делает.
    await admin.mark_ad_join(callback.from_user.id)


@router.callback_query(F.data == "start_now")
async def process_start_now(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    lang = data.get("lang", "ru")
    gender = data.get("gender", "")

    t = TEXTS[lang]

    # Онбординг пройден — запоминаем в БД, чтобы при следующих /start
    # (в том числе после рестарта бота) выбор языка/пола больше не
    # показывался, а сразу открывалось меню.
    await database.save_onboarding(callback.from_user.id, lang, gender)

    await callback.answer()
    await callback.message.answer(
        t["menu_opened"],
        reply_markup=main_menu_keyboard(lang),
    )

    # Кнопка "Начинаем!" больше не должна работать после нажатия — заменяем
    # её callback_data на "мёртвый", кнопка остаётся видимой, но неактивной.
    await callback.message.edit_reply_markup(
        reply_markup=guide_keyboard_started(lang)
    )


@router.callback_query(F.data == "start_now_dead")
async def process_start_now_dead(callback: CallbackQuery):
    # Кнопка уже "мёртвая" — просто гасим часики, ничего не делаем.
    await callback.answer()



# ==========================
#   ЗАПУСК БОТА
# ==========================

async def main():
    # Общая БД на весь бот (panda/garden/shop живут в одном файле,
    # bot.db, и делят одно соединение) — создаём схему один раз,
    # до начала polling'а.
    await database.init_db()

    # Ленивые ALTER TABLE-миграции для "общих" ачивок и кристаллов
    # (prof.py: _ensure_general_achv_schema/_ensure_gift_schema) раньше
    # выполнялись только при первом обращении — то есть на первом же
    # апдейте после рестарта бота (LoginStreakMiddleware срабатывает на
    # КАЖДЫЙ апдейт, включая нажатие инлайн-кнопки выбора языка). Из-за
    # нескольких последовательных ALTER TABLE + отдельных синхронных
    # commit() это выглядело как "бот долго думает" именно на первом
    # действии игрока сразу после рестарта. Прогоняем миграции здесь,
    # до старта polling'а, чтобы к первому реальному апдейту всё уже
    # было готово.
    await prof.ensure_startup_schema()

    # Кристаллы (премиальная валюта) — общий баланс для донатов
    # (donate.py), выдачи из админки (admin.py) и покупки скинов
    # (panda.py); хранится в users.crystals, схему создаёт лениво само
    # prof.py при первом обращении — отдельная ensure-функция здесь
    # больше не нужна.

    # Таблица состояния push-уведомлений "панда голодна/грустит" —
    # тоже лениво, тоже только у panda.py.
    await panda.ensure_notify_table()

    # Таблица счётчиков/стриков для ачивок панды (кормления, поглаживания,
    # стрик заботы, "неделя без голода") — без неё каждый вызов
    # _bump_feed_count/_bump_pet_count/_record_care_day/_touch_never_hungry
    # падает с "no such table" ещё до того, как проверяются сами ачивки,
    # так что ни одна ачивка панды (включая простые вроде "Первое
    # кормление") вообще не выдаётся.
    await panda.ensure_achv_state_table()

    # Фоновый цикл push-уведомлений "покормите/приласкайте панду" —
    # раз в 2 часа сам проверяет всех игроков и шлёт уведомление тем, у
    # кого голод/настроение ниже порога (см. panda.py: start_notify_loop).
    # Раньше это уведомление слалось реактивно при каждом действии в
    # разделе "Моя панда", из-за чего дублировалось по нескольку раз
    # подряд — теперь это отдельная фоновая задача, не завязанная на
    # то, открывает ли игрок раздел вообще.
    asyncio.create_task(panda.start_notify_loop(bot))

    # Таблица состояния штрафа за голодающую на 0% панду (какие
    # предупреждения уже отправлены, сколько штрафов уже списано за
    # текущий цикл голода) — тоже лениво, тоже только у panda.py.
    await panda.ensure_penalty_state_table()

    # Фоновый цикл штрафа за голодающую панду — раз в 15 минут проверяет
    # всех игроков: если голод держится на 0% дольше 5 часов, списывает
    # 1000 Pn и повторяет это каждый час, пока панду не покормят; до
    # первого штрафа игрок получает два предупреждения (см. panda.py:
    # HUNGER_ZERO_* константы и start_penalty_loop). Отдельная задача от
    # start_notify_loop выше — интервал у неё намного короче, чтобы
    # часовые пороги предупреждений/штрафа срабатывали вовремя, а не с
    # опозданием до 2 часов.
    asyncio.create_task(panda.start_penalty_loop(bot))

    # Таблицы счётчиков для ачивок сада (garden_harvest_counts,
    # garden_achv_state) — по документации garden.py тоже должны
    # создаваться здесь при старте; на текущей базе они, похоже, уже
    # существуют (иначе сбор урожая падал бы целиком, а не только
    # уведомление), но вызов был потерян — добавляем, чтобы не
    # сломаться на чистой БД (CREATE TABLE IF NOT EXISTS, безопасно).
    await garden.ensure_achv_tables()

    # Заново планирует авто-сбор для всех грядок, которые уже что-то
    # растят — иначе после перезапуска бота уведомления о созревании
    # урожая, посаженного до рестарта, потерялись бы.
    await garden.reschedule_pending_harvests(bot)

    # Таблицы счётчиков для ачивок пекарни (сколько всего испечено,
    # потрачено в лавке ингредиентов, продано на рынке, скормлено панде
    # и т.п.) — без неё ачивки категории "Пекарня" не выдаются, см.
    # bakery.py: ensure_achv_tables (там же — регистрация
    # PROGRESS_PROVIDERS для счётных ачивок пекарни).
    await bakery.ensure_achv_tables()

    # Аналогично саду: заново планирует уведомления о готовности выпечки
    # для всех печей, в которых что-то уже печётся.
    await bakery.reschedule_pending_bakes(bot)

    # Таблица выставленных крипто-инвойсов (CryptoBot/xRocket, см.
    # donate.py) — без неё покупка через эти способы оплаты упадёт на
    # первом же INSERT. Плюс фоновый цикл, который сам проверяет оплату
    # таких инвойсов (у бота нет вебхуков — он работает через polling),
    # чтобы кристаллы/монеты зачислялись, даже если игрок не нажал
    # кнопку "Проверить оплату" вручную.
    await donate.ensure_crypto_table()
    asyncio.create_task(donate.start_crypto_poll_loop(bot))
    try:
        await dp.start_polling(bot)
    finally:
        # Сохраняет всё, что накопилось в "стопке" незакоммиченных
        # изменений, и закрывает единственное соединение с БД.
        await database.close_db()


if __name__ == "__main__":
    setup_routers()
    asyncio.run(main())
