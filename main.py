# -*- coding: utf-8 -*-
"""
XYLT exchange — Telegram-бот для обмена USDT/GRAM -> RUB
aiogram 3.x + SQLite (aiosqlite)

Вся навигация построена на ОДНОМ обновляемом сообщении с инлайн-кнопками:
у каждого пользователя (и у каждого админа) есть своё "экранное" сообщение,
которое бот редактирует при переходах между разделами, вместо отправки
новых сообщений и без обычной reply-клавиатуры.

Установка зависимостей:
    pip install aiogram aiosqlite aiohttp

Запуск:
    python main.py
"""

import asyncio
import logging
import math
import re
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.exceptions import TelegramBadRequest

# ============================== CONFIG =====================================

BOT_TOKEN = "8229572426:AAGRKLS4uJo-Lc2gso0ZTyoCDzH2V5NJl7c"

# Токен приложения Crypto Pay (@CryptoBot -> Crypto Pay -> Create App).
# Нужен для приёма оплаты через кнопку "Отправить" (createInvoice/getInvoices).
CRYPTO_PAY_TOKEN = "615285:AA5onf6fapHVeoaXtvrni2GQtIp09wdgD2y"
CRYPTO_PAY_API = "https://pay.crypt.bot/api"

# Telegram user_id админов, у которых есть доступ к /admin
ADMIN_IDS: set[int] = {8118184388, 8115654734}

# username саппорта, куда ведёт кнопка "Поддержка" (без @)
SUPPORT_USERNAME = "xylt_admin"

DB_PATH = "xylt.db"

MSK = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("xylt")

# ============================== DATABASE ===================================

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    phone TEXT,
    fio TEXT,
    bank TEXT,
    joined_at TEXT NOT NULL,
    turnover REAL NOT NULL DEFAULT 0,
    total_rub REAL NOT NULL DEFAULT 0,
    is_blocked INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    currency TEXT NOT NULL,
    amount REAL,
    rate REAL,
    rub_amount REAL,
    bank TEXT,
    fio TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'awaiting_receipt',
    operator_id INTEGER,
    operator_username TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    rating INTEGER
);

CREATE TABLE IF NOT EXISTS rates (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    usdt_tier1 REAL NOT NULL DEFAULT 89.20,
    usdt_tier2 REAL NOT NULL DEFAULT 89.65,
    usdt_tier3 REAL NOT NULL DEFAULT 90.09,
    gram_rate REAL NOT NULL DEFAULT 133.87,
    min_usdt REAL NOT NULL DEFAULT 11.66,
    min_gram REAL NOT NULL DEFAULT 7.77
);

CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    night_boost_enabled INTEGER NOT NULL DEFAULT 1,
    night_boost_start TEXT NOT NULL DEFAULT '01:00',
    night_boost_end TEXT NOT NULL DEFAULT '09:00',
    night_boost_bonus REAL NOT NULL DEFAULT 0.75
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    answered INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS broadcasts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    text TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    sent_count INTEGER NOT NULL DEFAULT 0
);
"""


class DB:
    """Тонкая обёртка над aiosqlite для всего бота."""

    def __init__(self, path: str):
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def connect(self):
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.execute("INSERT OR IGNORE INTO rates (id) VALUES (1)")
        await self._conn.execute("INSERT OR IGNORE INTO settings (id) VALUES (1)")
        await self._conn.commit()
        log.info("DB подключена: %s", self.path)

    async def close(self):
        if self._conn:
            await self._conn.close()

    # ---- users ----

    async def get_user(self, user_id: int) -> aiosqlite.Row | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            )
            return await cur.fetchone()

    async def ensure_user(self, user_id: int, username: str | None):
        user = await self.get_user(user_id)
        if user is None:
            async with self._lock:
                await self._conn.execute(
                    "INSERT INTO users (user_id, username, joined_at) VALUES (?, ?, ?)",
                    (user_id, username, datetime.now(MSK).strftime("%d.%m.%Y")),
                )
                await self._conn.commit()
        else:
            async with self._lock:
                await self._conn.execute(
                    "UPDATE users SET username = ? WHERE user_id = ?",
                    (username, user_id),
                )
                await self._conn.commit()

    async def update_user_field(self, user_id: int, field: str, value):
        assert field in ("phone", "fio", "bank", "is_blocked")
        async with self._lock:
            await self._conn.execute(
                f"UPDATE users SET {field} = ? WHERE user_id = ?", (value, user_id)
            )
            await self._conn.commit()

    async def add_turnover(self, user_id: int, amount: float, rub: float):
        async with self._lock:
            await self._conn.execute(
                "UPDATE users SET turnover = turnover + ?, total_rub = total_rub + ? "
                "WHERE user_id = ?",
                (amount, rub, user_id),
            )
            await self._conn.commit()

    async def all_user_ids(self) -> list[int]:
        async with self._lock:
            cur = await self._conn.execute("SELECT user_id FROM users")
            rows = await cur.fetchall()
            return [r["user_id"] for r in rows]

    async def stats(self) -> dict:
        async with self._lock:
            cur = await self._conn.execute("SELECT COUNT(*) c FROM users")
            users_count = (await cur.fetchone())["c"]
            cur = await self._conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE status = 'completed'"
            )
            completed = (await cur.fetchone())["c"]
            cur = await self._conn.execute(
                "SELECT COALESCE(SUM(amount),0) s FROM requests WHERE status='completed'"
            )
            total_amount = (await cur.fetchone())["s"]
            cur = await self._conn.execute(
                "SELECT COALESCE(SUM(rub_amount),0) s FROM requests WHERE status='completed'"
            )
            total_rub = (await cur.fetchone())["s"]
            cur = await self._conn.execute(
                "SELECT COUNT(*) c FROM requests WHERE status IN "
                "('awaiting_receipt','pending_review','searching','in_progress')"
            )
            pending = (await cur.fetchone())["c"]
            return {
                "users": users_count,
                "completed": completed,
                "total_amount": total_amount,
                "total_rub": total_rub,
                "pending": pending,
            }

    # ---- rates / settings ----

    async def get_rates(self) -> aiosqlite.Row:
        async with self._lock:
            cur = await self._conn.execute("SELECT * FROM rates WHERE id = 1")
            return await cur.fetchone()

    async def set_rate_field(self, field: str, value: float):
        assert field in ("usdt_tier1", "usdt_tier2", "usdt_tier3", "gram_rate",
                          "min_usdt", "min_gram")
        async with self._lock:
            await self._conn.execute(
                f"UPDATE rates SET {field} = ? WHERE id = 1", (value,)
            )
            await self._conn.commit()

    async def get_settings(self) -> aiosqlite.Row:
        async with self._lock:
            cur = await self._conn.execute("SELECT * FROM settings WHERE id = 1")
            return await cur.fetchone()

    async def set_setting_field(self, field: str, value):
        assert field in ("night_boost_enabled", "night_boost_start",
                          "night_boost_end", "night_boost_bonus")
        async with self._lock:
            await self._conn.execute(
                f"UPDATE settings SET {field} = ? WHERE id = 1", (value,)
            )
            await self._conn.commit()

    # ---- requests ----

    async def create_request(self, user_id: int, currency: str, fio: str,
                              phone: str, bank: str) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO requests (user_id, currency, fio, phone, bank, "
                "status, created_at) VALUES (?, ?, ?, ?, ?, 'awaiting_receipt', ?)",
                (user_id, currency, fio, phone, bank, datetime.now(MSK).isoformat()),
            )
            await self._conn.commit()
            return cur.lastrowid

    async def get_request(self, req_id: int) -> aiosqlite.Row | None:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM requests WHERE id = ?", (req_id,)
            )
            return await cur.fetchone()

    async def set_request_amount_rate(self, req_id: int, amount: float,
                                       rate: float, rub_amount: float):
        async with self._lock:
            await self._conn.execute(
                "UPDATE requests SET amount = ?, rate = ?, rub_amount = ?, "
                "status = 'pending_review' WHERE id = ?",
                (amount, rate, rub_amount, req_id),
            )
            await self._conn.commit()

    async def set_request_status(self, req_id: int, status: str):
        async with self._lock:
            await self._conn.execute(
                "UPDATE requests SET status = ? WHERE id = ?", (status, req_id)
            )
            await self._conn.commit()

    async def assign_operator(self, req_id: int, operator_id: int, username: str | None):
        async with self._lock:
            await self._conn.execute(
                "UPDATE requests SET status = 'in_progress', operator_id = ?, "
                "operator_username = ? WHERE id = ?",
                (operator_id, username, req_id),
            )
            await self._conn.commit()

    async def complete_request(self, req_id: int):
        async with self._lock:
            await self._conn.execute(
                "UPDATE requests SET status = 'completed', completed_at = ? "
                "WHERE id = ?",
                (datetime.now(MSK).isoformat(), req_id),
            )
            await self._conn.commit()

    async def set_rating(self, req_id: int, rating: int):
        async with self._lock:
            await self._conn.execute(
                "UPDATE requests SET rating = ? WHERE id = ?", (rating, req_id)
            )
            await self._conn.commit()

    async def pending_requests(self) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM requests WHERE status IN "
                "('pending_review','searching','in_progress') ORDER BY id DESC"
            )
            return await cur.fetchall()

    # ---- support ----

    async def add_ticket(self, user_id: int, text: str) -> int:
        async with self._lock:
            cur = await self._conn.execute(
                "INSERT INTO support_tickets (user_id, message, created_at) "
                "VALUES (?, ?, ?)",
                (user_id, text, datetime.now(MSK).isoformat()),
            )
            await self._conn.commit()
            return cur.lastrowid

    async def open_tickets(self) -> list[aiosqlite.Row]:
        async with self._lock:
            cur = await self._conn.execute(
                "SELECT * FROM support_tickets WHERE answered = 0 ORDER BY id DESC"
            )
            return await cur.fetchall()

    async def mark_ticket_answered(self, ticket_id: int):
        async with self._lock:
            await self._conn.execute(
                "UPDATE support_tickets SET answered = 1 WHERE id = ?", (ticket_id,)
            )
            await self._conn.commit()


db = DB(DB_PATH)

# ============================== CRYPTOBOT (@CryptoBot / @send) =============


async def cryptobot_create_invoice(asset: str, amount: float, description: str = "") -> dict | None:
    """Создаёт счёт на оплату через @CryptoBot (Crypto Pay API createInvoice)."""
    url = f"{CRYPTO_PAY_API}/createInvoice"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    payload = {
        "asset": asset,
        "amount": str(amount),
        "description": description or "Оплата обмена XYLT",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    return data["result"]
                log.warning("CryptoBot createInvoice error: %s", data)
                return None
    except Exception as e:
        log.warning("CryptoBot createInvoice exception: %s", e)
        return None


async def cryptobot_get_invoice(invoice_id: int) -> dict | None:
    """Проверяет статус счёта через @CryptoBot (Crypto Pay API getInvoices)."""
    url = f"{CRYPTO_PAY_API}/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}
    params = {"invoice_ids": str(invoice_id)}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, params=params, headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                data = await resp.json()
                if data.get("ok"):
                    items = data["result"]["items"]
                    return items[0] if items else None
                log.warning("CryptoBot getInvoices error: %s", data)
                return None
    except Exception as e:
        log.warning("CryptoBot getInvoices exception: %s", e)
        return None


# ============================== FSM STATES =================================


class ExchangeFlow(StatesGroup):
    choosing_currency = State()
    filling_form = State()
    waiting_phone = State()
    waiting_fio = State()
    waiting_bank = State()
    waiting_custom_bank = State()
    waiting_receipt = State()
    waiting_invoice_amount = State()
    waiting_invoice_payment = State()


class AdminRates(StatesGroup):
    waiting_value = State()


class AdminBoost(StatesGroup):
    waiting_time = State()
    waiting_bonus = State()


class AdminUsers(StatesGroup):
    waiting_id = State()
    waiting_block_id = State()
    waiting_unblock_id = State()


class AdminReview(StatesGroup):
    waiting_amount = State()


class AdminBroadcastForm(StatesGroup):
    waiting_text = State()


class AdminSettings(StatesGroup):
    waiting_min_usdt = State()
    waiting_min_gram = State()

# ============================== HELPERS =====================================

BANKS = ["Сбер", "Тинькофф", "ВТБ", "Альфа", "Райффайзен", "Другой"]

PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{9,17}\d$")

# ---- Премиум-эмодзи (по ID) ----
# С Bot API 9.4 кастомные эмодзи можно вставлять не только в текст сообщений
# (через <tg-emoji emoji-id="...">) но и в сами кнопки — через параметр
# icon_custom_emoji_id у InlineKeyboardButton/KeyboardButton (иконка показывается
# слева от текста кнопки). Требуется aiogram >= 3.30 и доступ бота к премиум-
# эмодзи (Premium-подписка у владельца бота ИЛИ купленный на Fragment юзернейм) —
# то же самое условие, что уже нужно было для эмодзи в тексте ниже.

def pe(char: str, emoji_id: str) -> str:
    """Возвращает HTML-тег кастомного (премиум) эмодзи для parse_mode=HTML (для текста)."""
    return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'


# Голые ID — используются и в pe() для текста, и в ibtn() для кнопок ниже.
ID_WAVE = "5413694143601842851"
ID_EXCHANGE = "5402186569006210455"
ID_CHART_UP = "5244837092042750681"
ID_BAR_CHART = "5231200819986047254"
ID_WRITE = "5197269100878907942"
ID_SUPPORT = "5904248647972820334"
ID_PERSON = "5258362837411045098"
ID_PERSON2 = "5258011929993026890"
ID_CALENDAR = "5890937706803894250"
ID_BACK = "6039539366177541657"
ID_DIAMOND = "5427168083074628963"
ID_HOURGLASS = "5386367538735104399"
ID_FIRE = "5424972470023104089"
ID_MONEYBAG = "5258204546391351475"
ID_SOON = "5440621591387980068"
ID_FORM = "5257965174979042426"
ID_PHONE = "6039605143601680423"
ID_BANK = "5332455502917949981"
ID_CASH = "5409048419211682843"
# Новые ID — для кнопок, для которых раньше не было своего эмодзи в тексте.
# Взяты из открытого примера с рабочими значениями icon_custom_emoji_id для
# Bot API 9.4 — перед боевым использованием стоит один раз проверить, что они
# у тебя отображаются (см. пояснение в конце ответа, как достать свои ID).
ID_CHECK = "5870633910337015697"      # ✅ галочка
ID_CROSS = "5870657884844462243"      # ❌ крестик
ID_MEGAPHONE = "6039422865189638057"  # 📣 мегафон

EMOJI_WAVE = pe("👋", ID_WAVE)
EMOJI_EXCHANGE = pe("💱", ID_EXCHANGE)
EMOJI_CHART_UP = pe("📈", ID_CHART_UP)
EMOJI_BAR_CHART = pe("📊", ID_BAR_CHART)
EMOJI_WRITE = pe("✍️", ID_WRITE)
EMOJI_SUPPORT = pe("💭", ID_SUPPORT)
EMOJI_PERSON = pe("👤", ID_PERSON)
EMOJI_PERSON2 = pe("👤", ID_PERSON2)
EMOJI_CALENDAR = pe("📅", ID_CALENDAR)
EMOJI_BACK = pe("⬅️", ID_BACK)
EMOJI_DIAMOND = pe("💎", ID_DIAMOND)
EMOJI_HOURGLASS = pe("⌛", ID_HOURGLASS)
EMOJI_FIRE = pe("🔥", ID_FIRE)
EMOJI_MONEYBAG = pe("💰", ID_MONEYBAG)
EMOJI_SOON = pe("🔜", ID_SOON)
EMOJI_FORM = pe("📝", ID_FORM)
EMOJI_PHONE = pe("📞", ID_PHONE)
EMOJI_BANK = pe("🏦", ID_BANK)
EMOJI_CASH = pe("💵", ID_CASH)


def ibtn(text: str, emoji_id: str = None, **kwargs) -> InlineKeyboardButton:
    """InlineKeyboardButton с кастомной emoji-иконкой перед текстом (Bot API 9.4+).
    emoji_id=None — обычная кнопка без иконки (как раньше)."""
    return InlineKeyboardButton(text=text, icon_custom_emoji_id=emoji_id, **kwargs)

WELCOME_TEXT = (
    f"{EMOJI_WAVE} <b>Добро пожаловать в XYLT exchange</b>\n\n"
    f"{EMOJI_EXCHANGE} <b>Меняем USDT/GRAM → RUB</b>\n\n"
    f"{EMOJI_CHART_UP} <b>Лучший курс на рынке</b>"
)

# user_id -> message_id единственного "экранного" сообщения этого пользователя.
# Именно его бот редактирует при каждом переходе, вместо отправки новых
# сообщений — так вся навигация идёт через один и тот же апдейт + инлайн-кнопки.
USER_ANCHOR: dict[int, int] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def now_msk_time() -> datetime:
    return datetime.now(MSK)


async def is_night_boost_active() -> tuple[bool, float]:
    settings = await db.get_settings()
    if not settings["night_boost_enabled"]:
        return False, 0.0
    try:
        start_h, start_m = map(int, settings["night_boost_start"].split(":"))
        end_h, end_m = map(int, settings["night_boost_end"].split(":"))
    except Exception:
        return False, 0.0
    now = now_msk_time()
    start = now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if start <= end:
        active = start <= now <= end
    else:
        active = now >= start or now <= end
    return active, float(settings["night_boost_bonus"])


async def compute_rate(currency: str, amount: float) -> float:
    rates = await db.get_rates()
    if currency == "GRAM":
        base = rates["gram_rate"]
    else:
        if amount < 150:
            base = rates["usdt_tier1"]
        elif amount < 300:
            base = rates["usdt_tier2"]
        else:
            base = rates["usdt_tier3"]
    boosted, bonus = await is_night_boost_active()
    if boosted:
        base += bonus
    return round(base, 2)


def fmt(n: float) -> str:
    return f"{n:,.2f}".replace(",", " ")


async def render(bot: Bot, chat_id: int, user_id: int, text: str,
                  kb: InlineKeyboardMarkup | None = None):
    """Редактирует единственное 'экранное' сообщение пользователя.

    Если сообщения ещё нет (например, оно было удалено или это первый
    контакт) — отправляет новое и запоминает его как якорь.
    """
    msg_id = USER_ANCHOR.get(user_id)
    if msg_id:
        try:
            await bot.edit_message_text(text, chat_id=chat_id, message_id=msg_id,
                                         reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return
            # сообщение недоступно (удалено/слишком старое) — пришлём новое
    sent = await bot.send_message(chat_id, text, reply_markup=kb)
    USER_ANCHOR[user_id] = sent.message_id


def back_kb(text="Назад", cb="back_to_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[ibtn(text, ID_BACK, callback_data=cb)]]
    )


def cancel_kb(cb_data: str, text="Отмена", emoji: bool = True) -> InlineKeyboardMarkup:
    if emoji:
        btn = ibtn(text, ID_BACK, callback_data=cb_data)
    else:
        btn = InlineKeyboardButton(text=text, callback_data=cb_data)
    return InlineKeyboardMarkup(inline_keyboard=[[btn]])


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ibtn("Обменять", ID_EXCHANGE, callback_data="menu_exchange"),
             ibtn("Курсы", ID_BAR_CHART, callback_data="menu_rates")],
            [ibtn("Мои профиль", ID_WRITE, callback_data="menu_profile"),
             ibtn("Поддержка", ID_SUPPORT, callback_data="menu_support")],
        ]
    )


def currency_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ibtn("USDT → RUB", ID_CASH, callback_data="cur_USDT")],
            [ibtn("GRAM → RUB", ID_DIAMOND, callback_data="cur_GRAM")],
            [ibtn("Назад", ID_BACK, callback_data="back_to_menu")],
        ]
    )


def anketa_text(user_row) -> str:
    phone = user_row["phone"] if user_row and user_row["phone"] else "не указан"
    fio = user_row["fio"] if user_row and user_row["fio"] else "не указано"
    bank = user_row["bank"] if user_row and user_row["bank"] else "не выбран"
    return (
        f"{EMOJI_WRITE} <b>Анкета обмена</b>\n"
        "<i>Заполните по шагам:</i>\n\n"
        f"{EMOJI_PHONE} Телефон — <b>{phone}</b>\n\n"
        f"{EMOJI_PERSON} ФИО — <b>{fio}</b>\n\n"
        f"{EMOJI_BANK} Банк — <b>{bank}</b>"
    )


def anketa_kb(user_row) -> InlineKeyboardMarkup:
    phone_ok = "✅" if user_row and user_row["phone"] else ""
    fio_ok = "✅" if user_row and user_row["fio"] else ""
    bank_ok = "✅" if user_row and user_row["bank"] else ""
    rows = [
        [ibtn(f"Указать телефон {phone_ok}", ID_PHONE, callback_data="set_phone")],
        [ibtn(f"Указать ФИО {fio_ok}", ID_PERSON, callback_data="set_fio")],
        [ibtn(f"Выбрать банк {bank_ok}", ID_BANK, callback_data="set_bank")],
    ]
    if user_row and user_row["phone"] and user_row["fio"] and user_row["bank"]:
        rows.append([ibtn("Отправить USDT", ID_CASH, callback_data="go_send")])
    rows.append([ibtn("Курсы", ID_BAR_CHART, callback_data="menu_rates")])
    rows.append([ibtn("Назад", ID_BACK, callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


BANK_EMOJI = {
    "Сбер": "🟢",
    "Тинькофф": "🟡",
    "ВТБ": "🔵",
    "Альфа": "🔴",
    "Райффайзен": "🟠",
    "Другой": "✏️",
}


def banks_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=f"{BANK_EMOJI.get(b, '🏦')} {b}", callback_data=f"bank_{b}")]
        for b in BANKS
    ]
    rows.append([ibtn("Назад", ID_BACK, callback_data="fill_form")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [ibtn("Подтвердить заявку", ID_CHECK, callback_data=f"confirm_{req_id}")],
            [ibtn("Отменить", ID_CROSS, callback_data=f"cancel_{req_id}")],
        ]
    )


def rating_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=f"{i}⭐️", callback_data=f"rate_{req_id}_{i}")
            for i in range(1, 6)
        ]]
    )


def admin_main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Стата", callback_data="a_stats"),
         InlineKeyboardButton(text="Заявки", callback_data="a_requests")],
        [InlineKeyboardButton(text="Курсы", callback_data="a_rates"),
         InlineKeyboardButton(text="Ночной буст", callback_data="a_boost")],
        [InlineKeyboardButton(text="Юзеры", callback_data="a_users"),
         InlineKeyboardButton(text="Блокировки", callback_data="a_blocks")],
        [InlineKeyboardButton(text="Поддержка", callback_data="a_support"),
         InlineKeyboardButton(text="Рассылка", callback_data="a_broadcast")],
        [InlineKeyboardButton(text="Настройки", callback_data="a_settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="Назад", callback_data="a_home")]]
    )


def rates_edit_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="до 150$", callback_data="editrate_usdt_tier1"),
         InlineKeyboardButton(text="150-300$", callback_data="editrate_usdt_tier2")],
        [InlineKeyboardButton(text="300$+", callback_data="editrate_usdt_tier3"),
         InlineKeyboardButton(text="GRAM", callback_data="editrate_gram_rate")],
        [InlineKeyboardButton(text="Назад", callback_data="a_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_rates_text(r) -> str:
    return (
        "<b>Текущие курсы</b>\n"
        f"до 150$: <b>{r['usdt_tier1']}</b>\n"
        f"150-300$: <b>{r['usdt_tier2']}</b>\n"
        f"300$+: <b>{r['usdt_tier3']}</b>\n"
        f"GRAM: <b>{r['gram_rate']}</b>\n\n"
        f"<i>мин. USDT (архив.): {r['min_usdt']}</i>\n"
        f"<i>мин. GRAM (архив.): {r['min_gram']}</i>"
    )


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    """Редактирует именно то сообщение, где была нажата кнопка (используется
    для разовых карточек-уведомлений админам, а не для основной навигации)."""
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)


async def show_profile_text(user_id: int, username: str | None) -> str:
    await db.ensure_user(user_id, username)
    u = await db.get_user(user_id)
    return (
        f"{EMOJI_PERSON2} <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <b>{u['user_id']}</b>\n"
        f"{EMOJI_CALENDAR} <i>С нами с: {u['joined_at']}</i>\n\n"
        f"{EMOJI_BAR_CHART} <b>Статистика</b>\n"
        f"• Оборот: <b>{fmt(u['turnover'])}</b> USDT\n"
        f"• Сумма обменов: <b>{fmt(u['total_rub'])}</b> ₽"
    )


def calc_min_for_rate(rate: float) -> float:
    """Сколько USDT/GRAM нужно по текущему курсу, чтобы сумма была не меньше 1100₽."""
    if not rate or rate <= 0:
        return 0.0
    return math.ceil((1100 / rate) * 100) / 100


async def rates_text() -> str:
    rates = await db.get_rates()
    boosted, bonus = await is_night_boost_active()
    settings = await db.get_settings()
    async with db._lock:
        cur = await db._conn.execute(
            "SELECT COUNT(*) c FROM requests WHERE status IN "
            "('pending_review','searching','in_progress')"
        )
        pending = (await cur.fetchone())["c"]

    boost_line = ""
    if settings["night_boost_enabled"]:
        boost_line = (
            f"{EMOJI_FIRE} <b>Ночной буст:</b> {settings['night_boost_start']}–{settings['night_boost_end']} МСК "
            f"<b>+{settings['night_boost_bonus']}₽</b> к курсу"
            + (" <i>(сейчас активен)</i>" if boosted else "")
        )

    t1, t2, t3 = rates["usdt_tier1"], rates["usdt_tier2"], rates["usdt_tier3"]
    if boosted:
        t1, t2, t3 = t1 + bonus, t2 + bonus, t3 + bonus
        gram = rates["gram_rate"] + bonus
    else:
        gram = rates["gram_rate"]

    text = (
        f"{EMOJI_EXCHANGE} <b>Актуальные курсы XYLT</b>\n\n"
        f"{EMOJI_DIAMOND} <b>USDT/GRAM → RUB</b>\n"
        f"• до 150$: <b>{t1:.2f} ₽/$</b>\n"
        f"• 150–300$: <b>{t2:.2f} ₽/$</b>\n"
        f"• 300$+: <b>{t3:.2f} ₽/$</b>\n"
        f"• GRAM: <b>{gram:.2f} ₽</b>\n\n"
        f"{EMOJI_HOURGLASS} <i>В обработке: {pending} заявок</i>\n"
    )
    if boost_line:
        text += boost_line + "\n"
    min_usdt_live = calc_min_for_rate(t1)
    min_gram_live = calc_min_for_rate(gram)

    text += (
        f"\n{EMOJI_MONEYBAG} <b>Минималка</b>\n"
        f"• USDT: <b>{min_usdt_live}</b> USDT <i>(≈1100₽)</i>\n"
        f"• GRAM: <b>{min_gram_live}</b> GRAM <i>(≈1100₽)</i>\n\n"
        f"{EMOJI_SOON} <i>Работаем 24/7</i>"
    )
    return text

# ============================== USER ROUTER =================================

user_router = Router(name="user")


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    await db.ensure_user(message.from_user.id, message.from_user.username)
    USER_ANCHOR.pop(message.from_user.id, None)
    await render(bot, message.chat.id, message.from_user.id, WELCOME_TEXT, main_menu_kb())


@user_router.callback_query(F.data == "back_to_menu")
async def back_to_menu_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, WELCOME_TEXT, main_menu_kb())


@user_router.callback_query(F.data == "menu_profile")
async def profile_cb(cb: CallbackQuery, bot: Bot):
    text = await show_profile_text(cb.from_user.id, cb.from_user.username)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, text, back_kb())


@user_router.callback_query(F.data == "menu_rates")
async def rates_cb(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, await rates_text(), back_kb())


@user_router.callback_query(F.data == "menu_support")
async def support_cb(cb: CallbackQuery, bot: Bot):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [ibtn("Написать в поддержку", ID_SUPPORT,
                  url=f"https://t.me/{SUPPORT_USERNAME}")],
            [ibtn("Назад", ID_BACK, callback_data="back_to_menu")],
        ]
    )
    await cb.answer()
    await render(
        bot, cb.message.chat.id, cb.from_user.id,
        f"{EMOJI_SUPPORT} <b>Поддержка</b>\n\n<i>Если у вас вопрос по обмену — напишите нам напрямую:</i>", kb,
    )


# ---- Обмен ----

@user_router.callback_query(F.data == "menu_exchange")
async def exchange_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(ExchangeFlow.choosing_currency)
    await cb.answer()
    await render(
        bot, cb.message.chat.id, cb.from_user.id,
        "💱 <b>Выберите валюту для обмена</b>\n\n<i>Отдаёте → Получаете</i>", currency_kb(),
    )


@user_router.callback_query(F.data.startswith("cur_"))
async def choose_currency(cb: CallbackQuery, state: FSMContext, bot: Bot):
    currency = cb.data.split("_", 1)[1]
    await state.update_data(currency=currency)
    await cb.answer()
    text = (
        f"{EMOJI_FORM} Предоставьте реквизиты для обмена <b>{currency}</b>\n\n"
        "<i>Заполните анкету — это быстро.</i>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Заполнить", callback_data="fill_form")],
            [ibtn("Назад", ID_BACK, callback_data="back_to_menu")],
        ]
    )
    await render(bot, cb.message.chat.id, cb.from_user.id, text, kb)


@user_router.callback_query(F.data == "fill_form")
async def fill_form(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(ExchangeFlow.filling_form)
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    await render(bot, cb.message.chat.id, cb.from_user.id, anketa_text(user), anketa_kb(user))


@user_router.callback_query(F.data == "set_phone")
async def set_phone_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(ExchangeFlow.waiting_phone)
    await cb.answer()
    await render(
        bot, cb.message.chat.id, cb.from_user.id,
        f"{EMOJI_PHONE} Отправьте номер телефона в формате <b>+79991234567</b>", cancel_kb("fill_form"),
    )


@user_router.message(ExchangeFlow.waiting_phone)
async def set_phone_value(message: Message, state: FSMContext, bot: Bot):
    phone = message.text.strip()
    if not PHONE_RE.match(phone):
        await render(
            bot, message.chat.id, message.from_user.id,
            "⚠️ <b>Похоже, номер некорректный.</b>\n\nВведите ещё раз, например <b>+79991234567</b>",
            cancel_kb("fill_form"),
        )
        return
    await db.update_user_field(message.from_user.id, "phone", phone)
    await state.set_state(ExchangeFlow.filling_form)
    user = await db.get_user(message.from_user.id)
    await render(bot, message.chat.id, message.from_user.id, anketa_text(user), anketa_kb(user))


@user_router.callback_query(F.data == "set_fio")
async def set_fio_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(ExchangeFlow.waiting_fio)
    await cb.answer()
    await render(
        bot, cb.message.chat.id, cb.from_user.id,
        f"{EMOJI_PERSON} Введите <b>ФИО</b> получателя перевода (как в банке)", cancel_kb("fill_form"),
    )


@user_router.message(ExchangeFlow.waiting_fio)
async def set_fio_value(message: Message, state: FSMContext, bot: Bot):
    fio = message.text.strip()
    if len(fio.split()) < 2 or len(fio) > 100:
        await render(
            bot, message.chat.id, message.from_user.id,
            "⚠️ <b>Введите ФИО полностью</b>, например: <i>Иванов Иван Иванович</i>",
            cancel_kb("fill_form"),
        )
        return
    await db.update_user_field(message.from_user.id, "fio", fio)
    await state.set_state(ExchangeFlow.filling_form)
    user = await db.get_user(message.from_user.id)
    await render(bot, message.chat.id, message.from_user.id, anketa_text(user), anketa_kb(user))


@user_router.callback_query(F.data == "set_bank")
async def set_bank_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(ExchangeFlow.waiting_bank)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 f"{EMOJI_BANK} <b>Выберите банк</b> для получения перевода:", banks_kb())


@user_router.callback_query(F.data.startswith("bank_"))
async def set_bank_value(cb: CallbackQuery, state: FSMContext, bot: Bot):
    bank = cb.data.split("_", 1)[1]
    if bank == "Другой":
        await state.set_state(ExchangeFlow.waiting_custom_bank)
        await cb.answer()
        await render(
            bot, cb.message.chat.id, cb.from_user.id,
            f"{EMOJI_BANK} Введите <b>название вашего банка</b>:",
            cancel_kb("set_bank"),
        )
        return
    await db.update_user_field(cb.from_user.id, "bank", bank)
    await state.set_state(ExchangeFlow.filling_form)
    await cb.answer("Банк сохранён")
    user = await db.get_user(cb.from_user.id)
    await render(bot, cb.message.chat.id, cb.from_user.id, anketa_text(user), anketa_kb(user))


@user_router.message(ExchangeFlow.waiting_custom_bank)
async def set_custom_bank_value(message: Message, state: FSMContext, bot: Bot):
    bank = message.text.strip()
    if not bank or len(bank) > 50:
        await render(
            bot, message.chat.id, message.from_user.id,
            "⚠️ <b>Введите корректное название банка</b> (до 50 символов).",
            cancel_kb("set_bank"),
        )
        return
    await db.update_user_field(message.from_user.id, "bank", bank)
    await state.set_state(ExchangeFlow.filling_form)
    user = await db.get_user(message.from_user.id)
    await render(bot, message.chat.id, message.from_user.id, anketa_text(user), anketa_kb(user))


@user_router.callback_query(F.data == "go_send")
async def go_send(cb: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    currency = data.get("currency", "USDT")
    rates = await db.get_rates()
    boosted, bonus = await is_night_boost_active()
    if currency == "USDT":
        rate = rates["usdt_tier1"] + bonus if boosted else rates["usdt_tier1"]
    else:
        rate = rates["gram_rate"] + bonus if boosted else rates["gram_rate"]
    min_amount = calc_min_for_rate(rate)
    await state.update_data(currency=currency)
    await state.set_state(ExchangeFlow.waiting_invoice_amount)
    await cb.answer()
    text = (
        f"{EMOJI_CASH} Введите сумму <b>{currency}</b>, которую хотите отправить\n\n"
        f"{EMOJI_MONEYBAG} Минимальная сумма: <b>{min_amount} {currency}</b> <i>(≈1100₽)</i>\n\n"
        "<i>Бот создаст счёт на оплату через</i> @CryptoBot <i>на указанную сумму.</i>"
    )
    await render(bot, cb.message.chat.id, cb.from_user.id, text, cancel_kb("fill_form"))


@user_router.message(ExchangeFlow.waiting_invoice_amount)
async def invoice_amount_value(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    currency = data.get("currency", "USDT")
    try:
        amount = float(message.text.replace(",", ".").strip())
        if amount <= 0:
            raise ValueError
    except ValueError:
        await render(
            bot, message.chat.id, message.from_user.id,
            "⚠️ <b>Введите положительное число.</b>", cancel_kb("fill_form"),
        )
        return

    rate_preview = await compute_rate(currency, amount)
    min_amount = calc_min_for_rate(rate_preview)
    if amount < min_amount:
        await render(
            bot, message.chat.id, message.from_user.id,
            f"⚠️ <b>Сумма меньше минимальной.</b>\n\nМинимум: <b>{min_amount} {currency}</b>",
            cancel_kb("fill_form"),
        )
        return

    invoice = await cryptobot_create_invoice(
        currency, amount, description=f"XYLT обмен {amount} {currency}"
    )
    if not invoice:
        await render(
            bot, message.chat.id, message.from_user.id,
            f"❌ <b>Не удалось создать счёт через</b> @CryptoBot.\n\n"
            "<i>Попробуйте ещё раз чуть позже.</i>",
            cancel_kb("fill_form"),
        )
        return

    await state.update_data(
        invoice_id=invoice["invoice_id"], invoice_amount=amount, currency=currency
    )
    await state.set_state(ExchangeFlow.waiting_invoice_payment)

    pay_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=f"💳 Оплатить {amount} {currency}", url=pay_url)],
            [InlineKeyboardButton(text="✅ Я оплатил",
                                   callback_data=f"check_invoice_{invoice['invoice_id']}")],
            [ibtn("Отмена", ID_BACK, callback_data="fill_form")],
        ]
    )
    text = (
        f"{EMOJI_EXCHANGE} <b>Счёт создан!</b>\n\n"
        f"{EMOJI_CASH} Сумма: <b>{amount} {currency}</b>\n\n"
        "<i>Оплатите по кнопке ниже, затем нажмите «Я оплатил».</i>"
    )
    await render(bot, message.chat.id, message.from_user.id, text, kb)


@user_router.callback_query(F.data.startswith("check_invoice_"))
async def check_invoice_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    invoice_id = int(cb.data.split("_", 2)[2])
    data = await state.get_data()
    amount = data.get("invoice_amount")
    currency = data.get("currency", "USDT")

    if amount is None:
        await cb.answer("Заявка уже неактуальна", show_alert=True)
        return

    invoice = await cryptobot_get_invoice(invoice_id)
    if not invoice or invoice.get("status") != "paid":
        await cb.answer("❌ Оплата ещё не найдена. Попробуйте снова через минуту.",
                         show_alert=True)
        return

    user = await db.get_user(cb.from_user.id)
    if not (user and user["phone"] and user["fio"] and user["bank"]):
        await cb.answer()
        await render(bot, cb.message.chat.id, cb.from_user.id,
                     "⚠️ <b>Сначала заполните анкету полностью.</b>", anketa_kb(user))
        return

    rate = await compute_rate(currency, amount)
    rub_amount = round(amount * rate, 2)
    req_id = await db.create_request(
        cb.from_user.id, currency, user["fio"], user["phone"], user["bank"]
    )
    await db.set_request_amount_rate(req_id, amount, rate, rub_amount)
    await state.clear()
    await cb.answer("✅ Оплата подтверждена!")

    user_text = (
        f"✔️ <b>Оплата получена!</b>\n\n"
        f"{EMOJI_CASH} Сумма: <b>{amount} {currency}</b>\n"
        f"{EMOJI_CHART_UP} Курс: <b>{rate} ₽/$</b>\n"
        f"{EMOJI_MONEYBAG} Получаете: <b>{fmt(rub_amount)} ₽</b>\n"
        f"{EMOJI_BANK} Куда: <b>{user['bank']}</b>\n"
        f"{EMOJI_PERSON} ФИО: <b>{user['fio']}</b>\n\n"
        "<i>Подтвердите заявку — и она уйдёт в обработку.</i>"
    )
    await render(bot, cb.message.chat.id, cb.from_user.id, user_text, confirm_kb(req_id))

    caption = (
        f"Оплачена заявка #{req_id} (через @CryptoBot)\n\n"
        f"Пользователь: {cb.from_user.id} (@{cb.from_user.username})\n"
        f"Валюта: {currency}\nСумма: {amount}\nКурс: {rate}\nК выплате: {fmt(rub_amount)} ₽\n\n"
        f"Банк: {user['bank']}\nФИО: {user['fio']}\nТелефон: {user['phone']}"
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, caption)
        except Exception as e:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)


AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


@user_router.message(ExchangeFlow.waiting_receipt)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    currency = data.get("currency", "USDT")
    user = await db.get_user(message.from_user.id)
    if not (user and user["phone"] and user["fio"] and user["bank"]):
        await render(bot, message.chat.id, message.from_user.id,
                     "⚠️ <b>Сначала заполните анкету полностью.</b>", anketa_kb(user))
        return

    amount_guess = None
    raw_text = message.text or message.caption or ""
    match = AMOUNT_RE.search(raw_text.replace(",", "."))
    if match:
        try:
            amount_guess = float(match.group(1))
        except ValueError:
            amount_guess = None

    req_id = await db.create_request(
        message.from_user.id, currency, user["fio"], user["phone"], user["bank"]
    )
    await state.clear()

    await render(
        bot, message.chat.id, message.from_user.id,
        f"🔎 Чек получен, заявка <b>#{req_id}</b> отправлена на проверку.\n\n"
        "<i>Обычно это занимает ~3 мин.</i>",
        back_kb(),
    )

    caption = (
        f"<b>Новая заявка #{req_id}</b>\n\n"
        f"Пользователь: <b>{message.from_user.id}</b> (@{message.from_user.username})\n"
        f"Валюта: <b>{currency}</b>\n"
        f"Похоже, сумма в чеке: <b>{amount_guess if amount_guess else 'не распознана'}</b>\n\n"
        f"Банк: <b>{user['bank']}</b>\nФИО: <b>{user['fio']}</b>\nТелефон: <b>{user['phone']}</b>"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Указать сумму и подтвердить",
                                  callback_data=f"a_review_{req_id}")
        ]]
    )
    for admin_id in ADMIN_IDS:
        try:
            if message.photo:
                await bot.send_photo(admin_id, message.photo[-1].file_id,
                                      caption=caption, reply_markup=kb)
            else:
                await bot.send_message(admin_id, caption, reply_markup=kb)
        except Exception as e:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)


@user_router.callback_query(F.data.startswith("confirm_"))
async def user_confirm_request(cb: CallbackQuery, bot: Bot):
    req_id = int(cb.data.split("_", 1)[1])
    req = await db.get_request(req_id)
    if not req or req["status"] != "pending_review":
        await cb.answer("Заявка уже неактуальна", show_alert=True)
        return
    await db.set_request_status(req_id, "searching")
    await cb.answer()
    await render(
        bot, cb.message.chat.id, cb.from_user.id,
        f"🔎 <b>Ищем оператора</b> для обмена #{req_id}\n\n⏱ <i>Обычно это занимает ~3 мин</i>",
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="Взять заявку в работу",
                                  callback_data=f"a_take_{req_id}")
        ]]
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"Пользователь подтвердил заявку <b>#{req_id}</b>, ищем оператора.",
                reply_markup=kb,
            )
        except Exception as e:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)


@user_router.callback_query(F.data.startswith("cancel_"))
async def user_cancel_request(cb: CallbackQuery, bot: Bot):
    req_id = int(cb.data.split("_", 1)[1])
    req = await db.get_request(req_id)
    if not req:
        await cb.answer()
        return
    await db.set_request_status(req_id, "cancelled")
    await cb.answer("Заявка отменена")
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 f"❌ Заявка <b>#{req_id}</b> отменена.", back_kb())


@user_router.callback_query(F.data.startswith("rate_"))
async def user_rate_request(cb: CallbackQuery, bot: Bot):
    _, req_id, stars = cb.data.split("_")
    await db.set_rating(int(req_id), int(stars))
    await cb.answer("Спасибо за оценку!")
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 f"⭐ <b>Спасибо за оценку ({stars}/5)!</b>", back_kb())


@user_router.message(F.text & ~F.text.startswith("/"))
async def fallback_message(message: Message, state: FSMContext, bot: Bot):
    current = await state.get_state()
    if current is not None:
        return  # уже обрабатывается другим хэндлером/состоянием
    await render(
        bot, message.chat.id, message.from_user.id,
        WELCOME_TEXT + "\n\n<i>Используйте кнопки ниже 👇</i>", main_menu_kb(),
    )

# ============================== ADMIN ROUTER ================================

admin_router = Router(name="admin")
admin_router.message.filter(lambda m: is_admin(m.from_user.id))
admin_router.callback_query.filter(lambda c: is_admin(c.from_user.id))


@admin_router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    USER_ANCHOR.pop(message.from_user.id, None)
    await render(bot, message.chat.id, message.from_user.id, "<b>XYLT Admin</b>", admin_main_kb())


@admin_router.callback_query(F.data == "a_home")
async def admin_home_cb(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.clear()
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, "<b>XYLT Admin</b>", admin_main_kb())


@admin_router.callback_query(F.data == "a_stats")
async def admin_stats(cb: CallbackQuery, bot: Bot):
    s = await db.stats()
    text = (
        "<b>Статистика XYLT</b>\n\n"
        f"Пользователей: <b>{s['users']}</b>\n"
        f"Завершено обменов: <b>{s['completed']}</b>\n"
        f"Общий объём: <b>{fmt(s['total_amount'])}</b> (USDT/GRAM)\n"
        f"Выплачено: <b>{fmt(s['total_rub'])}</b> ₽\n"
        f"В обработке: <b>{s['pending']}</b>"
    )
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, text, admin_back_kb())


@admin_router.callback_query(F.data == "a_requests")
async def admin_requests(cb: CallbackQuery, bot: Bot):
    reqs = await db.pending_requests()
    await cb.answer()
    if not reqs:
        await render(bot, cb.message.chat.id, cb.from_user.id,
                     "<b>Активных заявок нет.</b>", admin_back_kb())
        return
    rows = []
    for r in reqs:
        label = f"#{r['id']} {r['currency']} [{r['status']}]"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"a_view_{r['id']}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="a_home")])
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "<b>Активные заявки:</b>", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("a_view_"))
async def admin_view_request(cb: CallbackQuery, bot: Bot):
    req_id = int(cb.data.split("_", 2)[2])
    r = await db.get_request(req_id)
    await cb.answer()
    if not r:
        await render(bot, cb.message.chat.id, cb.from_user.id,
                     "<b>Заявка не найдена.</b>", admin_back_kb())
        return
    text = (
        f"<b>Заявка #{r['id']}</b>\n\n"
        f"Пользователь: <b>{r['user_id']}</b>\n"
        f"Валюта: <b>{r['currency']}</b>\n"
        f"Сумма: <b>{r['amount']}</b>\n"
        f"Курс: <b>{r['rate']}</b>\n"
        f"К выплате: <b>{r['rub_amount']} ₽</b>\n\n"
        f"Банк: <b>{r['bank']}</b>\nФИО: <b>{r['fio']}</b>\nТелефон: <b>{r['phone']}</b>\n\n"
        f"Статус: <b>{r['status']}</b>\n"
        f"Оператор: <b>{r['operator_username'] or '-'}</b>"
    )
    rows = []
    if r["status"] == "searching":
        rows.append([InlineKeyboardButton(text="Взять в работу",
                                           callback_data=f"a_take_{req_id}")])
    if r["status"] == "in_progress":
        rows.append([InlineKeyboardButton(text="Завершить",
                                           callback_data=f"a_complete_{req_id}")])
    rows.append([InlineKeyboardButton(text="Назад", callback_data="a_requests")])
    await render(bot, cb.message.chat.id, cb.from_user.id, text,
                 InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("a_review_"))
async def admin_review_request(cb: CallbackQuery, state: FSMContext):
    req_id = int(cb.data.split("_", 2)[2])
    await state.set_state(AdminReview.waiting_amount)
    await state.update_data(
        req_id=req_id, alert_chat_id=cb.message.chat.id, alert_msg_id=cb.message.message_id
    )
    await cb.answer()
    await safe_edit(
        cb, f"Введите фактическую сумму из чека для заявки <b>#{req_id}</b> (например 250):"
    )


@admin_router.message(AdminReview.waiting_amount)
async def admin_review_amount(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id = data["req_id"]
    alert_chat_id = data.get("alert_chat_id")
    alert_msg_id = data.get("alert_msg_id")
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("<b>Введите положительное число.</b>")
        return

    req = await db.get_request(req_id)
    if not req:
        await message.answer("<b>Заявка не найдена.</b>")
        await state.clear()
        return

    rate = await compute_rate(req["currency"], amount)
    rub_amount = round(amount * rate, 2)
    await db.set_request_amount_rate(req_id, amount, rate, rub_amount)
    await state.clear()

    confirm_text = (
        f"Заявка <b>#{req_id}</b>: {amount} {req['currency']} по курсу {rate} "
        "→ отправлено пользователю на подтверждение."
    )
    if alert_chat_id and alert_msg_id:
        try:
            await bot.edit_message_text(confirm_text, chat_id=alert_chat_id,
                                         message_id=alert_msg_id)
        except TelegramBadRequest:
            await message.answer(confirm_text)
    else:
        await message.answer(confirm_text)

    user_text = (
        "✔️ <b>Чек принят!</b>\n\n"
        f"💵 Сумма в чеке: <b>{amount} {req['currency']}</b>\n"
        f"📈 Курс: <b>{rate} ₽/$</b>\n"
        f"💸 Получаете: <b>{fmt(rub_amount)} ₽</b>\n"
        f"💳 Куда: <b>{req['bank']}</b>\n"
        f"👤 ФИО: <b>{req['fio']}</b>\n\n"
        "<i>⚠️ После подтверждения заявка уходит в обработку.</i>"
    )
    try:
        await render(bot, req["user_id"], req["user_id"], user_text, confirm_kb(req_id))
    except Exception as e:
        log.warning("Не удалось отправить пользователю %s: %s", req["user_id"], e)


@admin_router.callback_query(F.data.startswith("a_take_"))
async def admin_take_request(cb: CallbackQuery, bot: Bot):
    req_id = int(cb.data.split("_", 2)[2])
    req = await db.get_request(req_id)
    if not req or req["status"] != "searching":
        await cb.answer("Заявка недоступна", show_alert=True)
        return
    await db.assign_operator(req_id, cb.from_user.id, cb.from_user.username)
    await cb.answer("Вы взяли заявку в работу")
    await safe_edit(cb, f"Вы оператор заявки <b>#{req_id}</b>.")

    try:
        await render(
            bot, req["user_id"], req["user_id"],
            "✔️ <b>Оператор найден!</b>\n\n"
            f"👤 Оператор: @{cb.from_user.username or 'оператор'}\n"
            f"✍️ Заявка <b>#{req_id}</b>\n"
            f"🤑 <b>{req['amount']} {req['currency']}</b> → <b>{fmt(req['rub_amount'])} ₽</b>\n\n"
            "<i>Сейчас подключится к вашей заявке.</i>",
        )
    except Exception as e:
        log.warning("Не удалось уведомить пользователя %s: %s", req["user_id"], e)


@admin_router.callback_query(F.data.startswith("a_complete_"))
async def admin_complete_request(cb: CallbackQuery, bot: Bot):
    req_id = int(cb.data.split("_", 2)[2])
    req = await db.get_request(req_id)
    if not req or req["status"] != "in_progress":
        await cb.answer("Заявка недоступна", show_alert=True)
        return
    await db.complete_request(req_id)
    await db.add_turnover(req["user_id"], req["amount"], req["rub_amount"])
    await cb.answer("Заявка завершена")
    await safe_edit(cb, f"Заявка <b>#{req_id}</b> завершена.")

    try:
        await render(
            bot, req["user_id"], req["user_id"],
            f"🎉 <b>Обмен #{req_id} завершён!</b>\n\n"
            f"💵 <b>{req['amount']} {req['currency']}</b> → <b>{fmt(req['rub_amount'])} ₽</b>\n"
            f"💳 {req['bank']}\n"
            f"👤 Оператор: @{req['operator_username'] or 'оператор'}\n\n"
            "⭐️ <i>Оцените работу сервиса:</i>",
            rating_kb(req_id),
        )
    except Exception as e:
        log.warning("Не удалось уведомить пользователя %s: %s", req["user_id"], e)


# ---- Курсы ----

@admin_router.callback_query(F.data == "a_rates")
async def admin_rates(cb: CallbackQuery, bot: Bot):
    r = await db.get_rates()
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, admin_rates_text(r), rates_edit_kb())


@admin_router.callback_query(F.data.startswith("editrate_"))
async def admin_edit_rate(cb: CallbackQuery, state: FSMContext, bot: Bot):
    field = cb.data.split("_", 1)[1]
    await state.set_state(AdminRates.waiting_value)
    await state.update_data(field=field)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 f"Введите новое значение для <b>{field}</b>:", cancel_kb("a_rates", emoji=False))


@admin_router.message(AdminRates.waiting_value)
async def admin_edit_rate_value(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    field = data["field"]
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     f"Введите число для <b>{field}</b>:", cancel_kb("a_rates", emoji=False))
        return
    await db.set_rate_field(field, value)
    await state.clear()
    r = await db.get_rates()
    await render(bot, message.chat.id, message.from_user.id,
                 admin_rates_text(r), rates_edit_kb())


# ---- Ночной буст ----

def boost_text(s) -> str:
    status = "включён" if s["night_boost_enabled"] else "выключен"
    return (
        f"<b>Ночной буст:</b> {status}\n\n"
        f"Время: <b>{s['night_boost_start']}–{s['night_boost_end']} МСК</b>\n"
        f"Бонус: <b>+{s['night_boost_bonus']} ₽</b>"
    )


def boost_kb(s) -> InlineKeyboardMarkup:
    toggle_text = "Выключить" if s["night_boost_enabled"] else "Включить"
    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data="boost_toggle"),
         InlineKeyboardButton(text="Изменить время", callback_data="boost_time")],
        [InlineKeyboardButton(text="Изменить бонус", callback_data="boost_bonus"),
         InlineKeyboardButton(text="Назад", callback_data="a_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "a_boost")
async def admin_boost(cb: CallbackQuery, bot: Bot):
    s = await db.get_settings()
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id, boost_text(s), boost_kb(s))


@admin_router.callback_query(F.data == "boost_toggle")
async def admin_boost_toggle(cb: CallbackQuery, bot: Bot):
    s = await db.get_settings()
    await db.set_setting_field("night_boost_enabled", 0 if s["night_boost_enabled"] else 1)
    await cb.answer("Изменено")
    s = await db.get_settings()
    await render(bot, cb.message.chat.id, cb.from_user.id, boost_text(s), boost_kb(s))


@admin_router.callback_query(F.data == "boost_time")
async def admin_boost_time(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminBoost.waiting_time)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите время в формате <b>ЧЧ:ММ-ЧЧ:ММ</b> (например 01:00-09:00):",
                 cancel_kb("a_boost", emoji=False))


@admin_router.message(AdminBoost.waiting_time)
async def admin_boost_time_value(message: Message, state: FSMContext, bot: Bot):
    m = re.match(r"^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$", message.text.strip())
    if not m:
        await render(bot, message.chat.id, message.from_user.id,
                     "Формат: <b>01:00-09:00</b>", cancel_kb("a_boost", emoji=False))
        return
    await db.set_setting_field("night_boost_start", m.group(1))
    await db.set_setting_field("night_boost_end", m.group(2))
    await state.clear()
    s = await db.get_settings()
    await render(bot, message.chat.id, message.from_user.id, boost_text(s), boost_kb(s))


@admin_router.callback_query(F.data == "boost_bonus")
async def admin_boost_bonus(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminBoost.waiting_bonus)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите <b>бонус в рублях</b> (например 0.75):", cancel_kb("a_boost", emoji=False))


@admin_router.message(AdminBoost.waiting_bonus)
async def admin_boost_bonus_value(message: Message, state: FSMContext, bot: Bot):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     "Введите число.", cancel_kb("a_boost", emoji=False))
        return
    await db.set_setting_field("night_boost_bonus", value)
    await state.clear()
    s = await db.get_settings()
    await render(bot, message.chat.id, message.from_user.id, boost_text(s), boost_kb(s))


# ---- Юзеры ----

@admin_router.callback_query(F.data == "a_users")
async def admin_users(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminUsers.waiting_id)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите <b>user_id</b> для просмотра профиля:", cancel_kb("a_home", emoji=False))


@admin_router.message(AdminUsers.waiting_id)
async def admin_users_lookup(message: Message, state: FSMContext, bot: Bot):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     "<b>Введите числовой ID.</b>", cancel_kb("a_home", emoji=False))
        return
    u = await db.get_user(user_id)
    await state.clear()
    if not u:
        await render(bot, message.chat.id, message.from_user.id,
                     "<b>Пользователь не найден.</b>", admin_back_kb())
        return
    text = (
        f"ID: <b>{u['user_id']}</b> (@{u['username']})\n\n"
        f"С нами с: <b>{u['joined_at']}</b>\n\n"
        f"Телефон: <b>{u['phone']}</b>\nФИО: <b>{u['fio']}</b>\nБанк: <b>{u['bank']}</b>\n\n"
        f"Оборот: <b>{fmt(u['turnover'])}</b>\nВыплачено: <b>{fmt(u['total_rub'])} ₽</b>\n\n"
        f"Заблокирован: <b>{'да' if u['is_blocked'] else 'нет'}</b>"
    )
    await render(bot, message.chat.id, message.from_user.id, text, admin_back_kb())


# ---- Блокировки ----

def blocks_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Заблокировать по ID", callback_data="block_user"),
         InlineKeyboardButton(text="Разблокировать по ID", callback_data="unblock_user")],
        [InlineKeyboardButton(text="Назад", callback_data="a_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


@admin_router.callback_query(F.data == "a_blocks")
async def admin_blocks(cb: CallbackQuery, bot: Bot):
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "<b>Блокировки пользователей</b>", blocks_kb())


@admin_router.callback_query(F.data == "block_user")
async def admin_block_user_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminUsers.waiting_block_id)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите <b>user_id</b> для блокировки:", cancel_kb("a_blocks", emoji=False))


@admin_router.message(AdminUsers.waiting_block_id)
async def admin_block_user_value(message: Message, state: FSMContext, bot: Bot):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     "<b>Введите числовой ID.</b>", cancel_kb("a_blocks", emoji=False))
        return
    await db.update_user_field(user_id, "is_blocked", 1)
    await state.clear()
    await render(bot, message.chat.id, message.from_user.id,
                 f"Пользователь <b>{user_id}</b> заблокирован.", blocks_kb())


@admin_router.callback_query(F.data == "unblock_user")
async def admin_unblock_user_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminUsers.waiting_unblock_id)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите <b>user_id</b> для разблокировки:", cancel_kb("a_blocks", emoji=False))


@admin_router.message(AdminUsers.waiting_unblock_id)
async def admin_unblock_user_value(message: Message, state: FSMContext, bot: Bot):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     "<b>Введите числовой ID.</b>", cancel_kb("a_blocks", emoji=False))
        return
    await db.update_user_field(user_id, "is_blocked", 0)
    await state.clear()
    await render(bot, message.chat.id, message.from_user.id,
                 f"Пользователь <b>{user_id}</b> разблокирован.", blocks_kb())


# ---- Поддержка (тикеты) ----

@admin_router.callback_query(F.data == "a_support")
async def admin_support(cb: CallbackQuery, bot: Bot):
    tickets = await db.open_tickets()
    await cb.answer()
    if not tickets:
        await render(bot, cb.message.chat.id, cb.from_user.id,
                     "<b>Открытых обращений нет.</b>", admin_back_kb())
        return
    lines = [f"<b>#{t['id']}</b> от {t['user_id']}: {t['message'][:60]}" for t in tickets[:20]]
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "<b>Открытые обращения:</b>\n\n" + "\n".join(lines), admin_back_kb())


# ---- Рассылка ----

@admin_router.callback_query(F.data == "a_broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminBroadcastForm.waiting_text)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите <b>текст рассылки</b> для всех пользователей:", cancel_kb("a_home", emoji=False))


@admin_router.message(AdminBroadcastForm.waiting_text)
async def admin_broadcast_send(message: Message, state: FSMContext, bot: Bot):
    text = message.text
    await state.clear()
    ids = await db.all_user_ids()
    sent = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)  # избегаем flood-limit
        except Exception:
            continue
    async with db._lock:
        await db._conn.execute(
            "INSERT INTO broadcasts (text, sent_at, sent_count) VALUES (?, ?, ?)",
            (text, datetime.now(MSK).isoformat(), sent),
        )
        await db._conn.commit()
    await render(bot, message.chat.id, message.from_user.id,
                 f"Рассылка отправлена <b>{sent}</b> пользователям.", admin_main_kb())


# ---- Настройки ----

def settings_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="Мин. USDT", callback_data="set_min_usdt"),
         InlineKeyboardButton(text="Мин. GRAM", callback_data="set_min_gram")],
        [InlineKeyboardButton(text="Назад", callback_data="a_home")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def settings_text(r) -> str:
    return (
        "<b>Настройки</b>\n\n"
        f"Мин. USDT: <b>{r['min_usdt']}</b>\n"
        f"Мин. GRAM: <b>{r['min_gram']}</b>"
    )


@admin_router.callback_query(F.data == "a_settings")
async def admin_settings(cb: CallbackQuery, bot: Bot):
    r = await db.get_rates()
    await cb.answer()
    await render(
        bot, cb.message.chat.id, cb.from_user.id,
        settings_text(r),
        settings_kb(),
    )


@admin_router.callback_query(F.data == "set_min_usdt")
async def set_min_usdt_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminSettings.waiting_min_usdt)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите новую <b>минималку USDT</b>:", cancel_kb("a_settings", emoji=False))


@admin_router.message(AdminSettings.waiting_min_usdt)
async def set_min_usdt_value(message: Message, state: FSMContext, bot: Bot):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     "<b>Введите число.</b>", cancel_kb("a_settings", emoji=False))
        return
    await db.set_rate_field("min_usdt", value)
    await state.clear()
    r = await db.get_rates()
    await render(bot, message.chat.id, message.from_user.id,
                 settings_text(r), settings_kb())


@admin_router.callback_query(F.data == "set_min_gram")
async def set_min_gram_start(cb: CallbackQuery, state: FSMContext, bot: Bot):
    await state.set_state(AdminSettings.waiting_min_gram)
    await cb.answer()
    await render(bot, cb.message.chat.id, cb.from_user.id,
                 "Введите новую <b>минималку GRAM</b>:", cancel_kb("a_settings", emoji=False))


@admin_router.message(AdminSettings.waiting_min_gram)
async def set_min_gram_value(message: Message, state: FSMContext, bot: Bot):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await render(bot, message.chat.id, message.from_user.id,
                     "<b>Введите число.</b>", cancel_kb("a_settings", emoji=False))
        return
    await db.set_rate_field("min_gram", value)
    await state.clear()
    r = await db.get_rates()
    await render(bot, message.chat.id, message.from_user.id,
                 settings_text(r), settings_kb())


# ============================== ENTRYPOINT ==================================

async def main():
    await db.connect()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(admin_router)
    dp.include_router(user_router)

    log.info("Бот запускается...")
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
