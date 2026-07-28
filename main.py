# -*- coding: utf-8 -*-
"""
XYLT exchange — Telegram-бот для обмена USDT/GRAM -> RUB
aiogram 3.x + SQLite (aiosqlite)

Установка зависимостей:
    pip install aiogram aiosqlite aiohttp

Запуск:
    python main.py

Перед запуском заполните константы в блоке CONFIG ниже
(токен бота, id админов, при необходимости токен CryptoBot Pay API).
"""

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

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
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.exceptions import TelegramBadRequest

# ============================== CONFIG =====================================

BOT_TOKEN = "8651956926:AAG3ML1uGBPQOgrM5WAMl3kXaRLvVxTHCsw"

# Telegram user_id админов, у которых есть доступ к /admin
ADMIN_IDS: set[int] = {8118184388}

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
        await self._conn.execute(
            "INSERT OR IGNORE INTO rates (id) VALUES (1)"
        )
        await self._conn.execute(
            "INSERT OR IGNORE INTO settings (id) VALUES (1)"
        )
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

# ============================== FSM STATES =================================


class ProfileForm(StatesGroup):
    phone = State()
    fio = State()
    bank = State()


class ExchangeFlow(StatesGroup):
    choosing_currency = State()
    filling_form = State()
    waiting_phone = State()
    waiting_fio = State()
    waiting_bank = State()
    waiting_receipt = State()


class SupportForm(StatesGroup):
    waiting_message = State()


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
        # окно переходит через полночь
        active = now >= start or now <= end
    return active, float(settings["night_boost_bonus"])


async def compute_rate(currency: str, amount: float) -> float:
    """Возвращает курс ₽/$ (или ₽/GRAM) с учётом суммы и ночного буста."""
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


def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="💱 Обменять"), KeyboardButton(text="📊 Курсы")],
            [KeyboardButton(text="✍️ Мои профиль"), KeyboardButton(text="💭 Поддержка")],
        ],
        resize_keyboard=True,
    )


def back_kb(text="⬅️ Назад", cb="back_to_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text=text, callback_data=cb)]]
    )


def currency_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💲 USDT → RUB", callback_data="cur_USDT")],
            [InlineKeyboardButton(text="💎 GRAM → RUB", callback_data="cur_GRAM")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
        ]
    )


def anketa_kb(user_row) -> InlineKeyboardMarkup:
    phone_ok = "✅" if user_row and user_row["phone"] else ""
    fio_ok = "✅" if user_row and user_row["fio"] else ""
    bank_ok = "✅" if user_row and user_row["bank"] else ""
    rows = [
        [InlineKeyboardButton(text=f"📞 Указать телефон {phone_ok}", callback_data="set_phone")],
        [InlineKeyboardButton(text=f"👤 Указать ФИО {fio_ok}", callback_data="set_fio")],
        [InlineKeyboardButton(text=f"🏦 Выбрать банк {bank_ok}", callback_data="set_bank")],
    ]
    if user_row and user_row["phone"] and user_row["fio"] and user_row["bank"]:
        rows.append([InlineKeyboardButton(text="💵 Отправить USDT", callback_data="go_send")])
    rows.append([InlineKeyboardButton(text="📊 Курсы", callback_data="show_rates")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def banks_kb() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=b, callback_data=f"bank_{b}")] for b in BANKS]
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="fill_form")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✔️ Подтвердить заявку", callback_data=f"confirm_{req_id}")],
            [InlineKeyboardButton(text="❌ Отменить", callback_data=f"cancel_{req_id}")],
        ]
    )


def rating_kb(req_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text=str(i), callback_data=f"rate_{req_id}_{i}")
            for i in range(1, 6)
        ]]
    )


def admin_main_kb() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📊 Стата", callback_data="a_stats")],
        [InlineKeyboardButton(text="📋 Заявки", callback_data="a_requests")],
        [InlineKeyboardButton(text="💱 Курсы", callback_data="a_rates")],
        [InlineKeyboardButton(text="🔔 Ночной буст", callback_data="a_boost")],
        [InlineKeyboardButton(text="👤 Юзеры", callback_data="a_users")],
        [InlineKeyboardButton(text="🚫 Блокировки", callback_data="a_blocks")],
        [InlineKeyboardButton(text="💬 Поддержка", callback_data="a_support")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="a_broadcast")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="a_settings")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="a_home")]]
    )


async def safe_edit(cb: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=kb)

# ============================== USER ROUTER =================================

user_router = Router(name="user")


@user_router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    await db.ensure_user(message.from_user.id, message.from_user.username)
    text = (
        "👋 Добро пожаловать в XYLT exchange\n"
        "💱Меняем USDT/GRAM → RUB\n"
        "📈Лучший курс на рынке"
    )
    await message.answer(text, reply_markup=main_menu_kb())


async def show_profile(user_id: int, username: str | None) -> str:
    await db.ensure_user(user_id, username)
    u = await db.get_user(user_id)
    return (
        "👤Ваш профиль\n"
        f"👤ID: {u['user_id']}\n"
        f"📅С нами с: {u['joined_at']}\n"
        "📊Статистика:\n"
        f"• Оборот: {fmt(u['turnover'])} USDT\n"
        f"• Сумма обменов: {fmt(u['total_rub'])} ₽"
    )


@user_router.message(F.text == "✍️ Мои профиль")
async def profile_handler(message: Message):
    text = await show_profile(message.from_user.id, message.from_user.username)
    await message.answer(text, reply_markup=back_kb())


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
            f"🔥 Ночной буст: {settings['night_boost_start']}–{settings['night_boost_end']} МСК "
            f"+ {settings['night_boost_bonus']}₽ к курсу"
            + (" (сейчас активен)" if boosted else "")
        )

    t1, t2, t3 = rates["usdt_tier1"], rates["usdt_tier2"], rates["usdt_tier3"]
    if boosted:
        t1, t2, t3 = t1 + bonus, t2 + bonus, t3 + bonus
        gram = rates["gram_rate"] + bonus
    else:
        gram = rates["gram_rate"]

    text = (
        "💱 Актуальные курсы XYLT\n\n"
        "💎 USDT/GRAM → RUB\n"
        f"• до 150$: {t1:.2f} ₽/$\n"
        f"• 150–300$: {t2:.2f} ₽/$\n"
        f"• 300$+: {t3:.2f} ₽/$\n"
        f"• GRAM: {gram:.2f} ₽\n\n"
        f"⌛ В обработке: {pending} заявок\n"
    )
    if boost_line:
        text += boost_line + "\n"
    text += (
        "\n💰 Минималка:\n"
        f"• USDT: {rates['min_usdt']} USDT\n"
        f"• GRAM: {rates['min_gram']} GRAM "
        "( бот сам высчитывает сумму кратное 1100₽ )\n\n"
        "🔜 Работаем 24/7"
    )
    return text


@user_router.message(F.text == "📊 Курсы")
async def rates_handler(message: Message):
    await message.answer(await rates_text(), reply_markup=back_kb())


@user_router.callback_query(F.data == "show_rates")
async def rates_cb(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer(await rates_text(), reply_markup=back_kb())


@user_router.message(F.text == "💭 Поддержка")
async def support_handler(message: Message):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="💬 Написать в поддержку",
                                  url=f"https://t.me/{SUPPORT_USERNAME}")
        ]]
    )
    await message.answer(
        "💭 Поддержка\nЕсли у вас вопрос по обмену — напишите нам напрямую:",
        reply_markup=kb,
    )


@user_router.callback_query(F.data == "back_to_menu")
async def back_to_menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb, "Главное меню 👇")


# ---- Обмен ----

@user_router.message(F.text == "💱 Обменять")
async def exchange_start(message: Message, state: FSMContext):
    await state.set_state(ExchangeFlow.choosing_currency)
    await message.answer(
        "💱 Выберите валюту для обмена\nОтдаёте → Получаете",
        reply_markup=currency_kb(),
    )


@user_router.callback_query(F.data.startswith("cur_"))
async def choose_currency(cb: CallbackQuery, state: FSMContext):
    currency = cb.data.split("_", 1)[1]
    await state.update_data(currency=currency)
    await cb.answer()
    text = (
        f"📝 Предоставьте реквизиты для обмена {currency}\n"
        "Заполните анкету — это быстро."
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➡️ Заполнить", callback_data="fill_form")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back_to_menu")],
        ]
    )
    await safe_edit(cb, text, kb)


@user_router.callback_query(F.data == "fill_form")
async def fill_form(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ExchangeFlow.filling_form)
    await cb.answer()
    user = await db.get_user(cb.from_user.id)
    phone = user["phone"] if user and user["phone"] else "не указан"
    fio = user["fio"] if user and user["fio"] else "не указано"
    bank = user["bank"] if user and user["bank"] else "не выбран"
    text = (
        "✍️ Анкета обмена\nЗаполните по шагам:\n\n"
        f"📞 Телефон — {phone}\n"
        f"👤 ФИО — {fio}\n"
        f"🏦 Банк — {bank}"
    )
    await safe_edit(cb, text, anketa_kb(user))


@user_router.callback_query(F.data == "set_phone")
async def set_phone_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ExchangeFlow.waiting_phone)
    await cb.answer()
    await cb.message.answer("📞 Отправьте номер телефона в формате +79991234567")


@user_router.message(ExchangeFlow.waiting_phone)
async def set_phone_value(message: Message, state: FSMContext):
    phone = message.text.strip()
    if not PHONE_RE.match(phone):
        await message.answer("⚠️ Похоже, номер некорректный. Введите ещё раз, например +79991234567")
        return
    await db.update_user_field(message.from_user.id, "phone", phone)
    await state.set_state(ExchangeFlow.filling_form)
    user = await db.get_user(message.from_user.id)
    await message.answer("✅ Телефон сохранён.", reply_markup=anketa_kb(user))


@user_router.callback_query(F.data == "set_fio")
async def set_fio_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ExchangeFlow.waiting_fio)
    await cb.answer()
    await cb.message.answer("👤 Введите ФИО получателя перевода (как в банке)")


@user_router.message(ExchangeFlow.waiting_fio)
async def set_fio_value(message: Message, state: FSMContext):
    fio = message.text.strip()
    if len(fio.split()) < 2 or len(fio) > 100:
        await message.answer("⚠️ Введите ФИО полностью, например: Иванов Иван Иванович")
        return
    await db.update_user_field(message.from_user.id, "fio", fio)
    await state.set_state(ExchangeFlow.filling_form)
    user = await db.get_user(message.from_user.id)
    await message.answer("✅ ФИО сохранено.", reply_markup=anketa_kb(user))


@user_router.callback_query(F.data == "set_bank")
async def set_bank_cb(cb: CallbackQuery, state: FSMContext):
    await state.set_state(ExchangeFlow.waiting_bank)
    await cb.answer()
    await safe_edit(cb, "🏦 Выберите банк для получения перевода:", banks_kb())


@user_router.callback_query(F.data.startswith("bank_"))
async def set_bank_value(cb: CallbackQuery, state: FSMContext):
    bank = cb.data.split("_", 1)[1]
    await db.update_user_field(cb.from_user.id, "bank", bank)
    await state.set_state(ExchangeFlow.filling_form)
    await cb.answer("Банк сохранён")
    user = await db.get_user(cb.from_user.id)
    text = (
        "✍️ Анкета обмена\nЗаполните по шагам:\n\n"
        f"📞 Телефон — {user['phone'] or 'не указан'}\n"
        f"👤 ФИО — {user['fio'] or 'не указано'}\n"
        f"🏦 Банк — {user['bank']}"
    )
    await safe_edit(cb, text, anketa_kb(user))


@user_router.callback_query(F.data == "go_send")
async def go_send(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    currency = data.get("currency", "USDT")
    rates = await db.get_rates()
    min_amount = rates["min_usdt"] if currency == "USDT" else rates["min_gram"]
    await state.set_state(ExchangeFlow.waiting_receipt)
    await cb.answer()
    text = (
        f"💸 Отправьте {currency} и пришлите сюда чек из CryptoBot\n\n"
        f"💵 Минимальная сумма: {min_amount} {currency}\n\n"
        "После оплаты пришлите чек (переслать сообщение с чеком, скриншот "
        "или укажите сумму текстом)."
    )
    await safe_edit(cb, text, back_kb("⬅️ Назад", "fill_form"))


AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d+)?)")


@user_router.message(ExchangeFlow.waiting_receipt)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    currency = data.get("currency", "USDT")
    user = await db.get_user(message.from_user.id)
    if not (user and user["phone"] and user["fio"] and user["bank"]):
        await message.answer("⚠️ Сначала заполните анкету полностью.")
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

    await message.answer(
        f"🔎 Чек получен, заявка #{req_id} отправлена на проверку.\n"
        "Обычно это занимает ~3 мин.",
    )

    # уведомление админам
    caption = (
        f"🆕 Новая заявка #{req_id}\n"
        f"Пользователь: {message.from_user.id} (@{message.from_user.username})\n"
        f"Валюта: {currency}\n"
        f"Похоже, сумма в чеке: {amount_guess if amount_guess else 'не распознана'}\n"
        f"Банк: {user['bank']}\nФИО: {user['fio']}\nТелефон: {user['phone']}"
    )
    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="✏️ Указать сумму и подтвердить",
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
    await safe_edit(cb, f"🔎 Ищем оператора для обмена #{req_id}\n⏱ Обычно это занимает ~3 мин")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(text="🙋 Взять заявку в работу",
                                  callback_data=f"a_take_{req_id}")
        ]]
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"✔️ Пользователь подтвердил заявку #{req_id}, ищем оператора.",
                reply_markup=kb,
            )
        except Exception as e:
            log.warning("Не удалось уведомить админа %s: %s", admin_id, e)


@user_router.callback_query(F.data.startswith("cancel_"))
async def user_cancel_request(cb: CallbackQuery):
    req_id = int(cb.data.split("_", 1)[1])
    req = await db.get_request(req_id)
    if not req:
        await cb.answer()
        return
    await db.set_request_status(req_id, "cancelled")
    await cb.answer("Заявка отменена")
    await safe_edit(cb, f"❌ Заявка #{req_id} отменена.")


@user_router.callback_query(F.data.startswith("rate_"))
async def user_rate_request(cb: CallbackQuery):
    _, req_id, stars = cb.data.split("_")
    await db.set_rating(int(req_id), int(stars))
    await cb.answer("Спасибо за оценку!")
    await safe_edit(cb, f"⭐ Спасибо за оценку ({stars}/5)!")

# ============================== ADMIN ROUTER ================================

admin_router = Router(name="admin")
admin_router.message.filter(lambda m: is_admin(m.from_user.id))
admin_router.callback_query.filter(lambda c: is_admin(c.from_user.id))


@admin_router.message(Command("admin"))
async def admin_panel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 XYLT Admin", reply_markup=admin_main_kb())


@admin_router.callback_query(F.data == "a_home")
async def admin_home_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.answer()
    await safe_edit(cb, "🛠 XYLT Admin", admin_main_kb())


@admin_router.callback_query(F.data == "a_stats")
async def admin_stats(cb: CallbackQuery):
    s = await db.stats()
    text = (
        "📊 Статистика XYLT\n\n"
        f"👥 Пользователей: {s['users']}\n"
        f"✅ Завершено обменов: {s['completed']}\n"
        f"💰 Общий объём: {fmt(s['total_amount'])} (USDT/GRAM)\n"
        f"💸 Выплачено: {fmt(s['total_rub'])} ₽\n"
        f"⌛ В обработке: {s['pending']}"
    )
    await cb.answer()
    await safe_edit(cb, text, admin_back_kb())


@admin_router.callback_query(F.data == "a_requests")
async def admin_requests(cb: CallbackQuery):
    reqs = await db.pending_requests()
    await cb.answer()
    if not reqs:
        await safe_edit(cb, "📋 Активных заявок нет.", admin_back_kb())
        return
    rows = []
    for r in reqs:
        label = f"#{r['id']} {r['currency']} [{r['status']}]"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"a_view_{r['id']}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="a_home")])
    await safe_edit(cb, "📋 Активные заявки:", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("a_view_"))
async def admin_view_request(cb: CallbackQuery):
    req_id = int(cb.data.split("_", 2)[2])
    r = await db.get_request(req_id)
    await cb.answer()
    if not r:
        await safe_edit(cb, "Заявка не найдена.", admin_back_kb())
        return
    text = (
        f"Заявка #{r['id']}\n"
        f"Пользователь: {r['user_id']}\n"
        f"Валюта: {r['currency']}\n"
        f"Сумма: {r['amount']}\n"
        f"Курс: {r['rate']}\n"
        f"К выплате: {r['rub_amount']} ₽\n"
        f"Банк: {r['bank']}\nФИО: {r['fio']}\nТелефон: {r['phone']}\n"
        f"Статус: {r['status']}\n"
        f"Оператор: {r['operator_username'] or '-'}"
    )
    rows = []
    if r["status"] == "searching":
        rows.append([InlineKeyboardButton(text="🙋 Взять в работу",
                                           callback_data=f"a_take_{req_id}")])
    if r["status"] == "in_progress":
        rows.append([InlineKeyboardButton(text="✅ Завершить",
                                           callback_data=f"a_complete_{req_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="a_requests")])
    await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("a_review_"))
async def admin_review_request(cb: CallbackQuery, state: FSMContext):
    req_id = int(cb.data.split("_", 2)[2])
    await state.set_state(AdminReview.waiting_amount)
    await state.update_data(req_id=req_id)
    await cb.answer()
    await cb.message.answer(
        f"Введите фактическую сумму из чека для заявки #{req_id} (числом, например 250):"
    )


@admin_router.message(AdminReview.waiting_amount)
async def admin_review_amount(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    req_id = data["req_id"]
    try:
        amount = float(message.text.replace(",", "."))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("⚠️ Введите положительное число.")
        return

    req = await db.get_request(req_id)
    if not req:
        await message.answer("Заявка не найдена.")
        await state.clear()
        return

    rate = await compute_rate(req["currency"], amount)
    rub_amount = round(amount * rate, 2)
    await db.set_request_amount_rate(req_id, amount, rate, rub_amount)
    await state.clear()
    await message.answer(f"✅ Заявка #{req_id} обновлена и отправлена пользователю.")

    user_text = (
        "✔️ Чек принят!\n"
        f"💵 Сумма в чеке: {amount} {req['currency']}\n"
        f"📈 Курс: {rate} ₽/$\n"
        f"💸 Получаете: {fmt(rub_amount)} ₽\n"
        f"💳 Куда: {req['bank']}\n"
        f"👤 ФИО: {req['fio']}\n\n"
        "⚠️ После подтверждения заявка уходит в обработку."
    )
    try:
        await bot.send_message(req["user_id"], user_text, reply_markup=confirm_kb(req_id))
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
    await safe_edit(cb, f"🙋 Вы оператор заявки #{req_id}.")

    try:
        await bot.send_message(
            req["user_id"],
            "✔️ Оператор найден!\n"
            f"👤 Оператор: @{cb.from_user.username or 'оператор'}\n"
            f"✍️ Заявка #{req_id}\n"
            f"🤑 {req['amount']} {req['currency']} → {fmt(req['rub_amount'])} ₽\n"
            "Сейчас подключится к вашей заявке.",
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
    await safe_edit(cb, f"✅ Заявка #{req_id} завершена.")

    try:
        await bot.send_message(
            req["user_id"],
            f"🎉 Обмен #{req_id} завершён!\n"
            f"💵 {req['amount']} {req['currency']} → {fmt(req['rub_amount'])} ₽\n"
            f"💳 {req['bank']}\n"
            f"👤 Оператор: @{req['operator_username'] or 'оператор'}\n\n"
            "⭐️ Оцените работу сервиса:",
            reply_markup=rating_kb(req_id),
        )
    except Exception as e:
        log.warning("Не удалось уведомить пользователя %s: %s", req["user_id"], e)


# ---- Курсы ----

@admin_router.callback_query(F.data == "a_rates")
async def admin_rates(cb: CallbackQuery):
    r = await db.get_rates()
    text = (
        "💱 Текущие курсы:\n"
        f"до 150$: {r['usdt_tier1']}\n"
        f"150-300$: {r['usdt_tier2']}\n"
        f"300$+: {r['usdt_tier3']}\n"
        f"GRAM: {r['gram_rate']}\n"
        f"мин. USDT: {r['min_usdt']}\n"
        f"мин. GRAM: {r['min_gram']}"
    )
    rows = [
        [InlineKeyboardButton(text="✏️ до 150$", callback_data="editrate_usdt_tier1")],
        [InlineKeyboardButton(text="✏️ 150-300$", callback_data="editrate_usdt_tier2")],
        [InlineKeyboardButton(text="✏️ 300$+", callback_data="editrate_usdt_tier3")],
        [InlineKeyboardButton(text="✏️ GRAM", callback_data="editrate_gram_rate")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="a_home")],
    ]
    await cb.answer()
    await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data.startswith("editrate_"))
async def admin_edit_rate(cb: CallbackQuery, state: FSMContext):
    field = cb.data.split("_", 1)[1]
    await state.set_state(AdminRates.waiting_value)
    await state.update_data(field=field)
    await cb.answer()
    await cb.message.answer(f"Введите новое значение для {field}:")


@admin_router.message(AdminRates.waiting_value)
async def admin_edit_rate_value(message: Message, state: FSMContext):
    data = await state.get_data()
    field = data["field"]
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введите число.")
        return
    await db.set_rate_field(field, value)
    await state.clear()
    await message.answer(f"✅ {field} обновлено на {value}", reply_markup=admin_main_kb())


# ---- Ночной буст ----

@admin_router.callback_query(F.data == "a_boost")
async def admin_boost(cb: CallbackQuery):
    s = await db.get_settings()
    status = "включён ✅" if s["night_boost_enabled"] else "выключен ❌"
    text = (
        f"🔔 Ночной буст: {status}\n"
        f"Время: {s['night_boost_start']}–{s['night_boost_end']} МСК\n"
        f"Бонус: +{s['night_boost_bonus']} ₽"
    )
    toggle_text = "❌ Выключить" if s["night_boost_enabled"] else "✅ Включить"
    rows = [
        [InlineKeyboardButton(text=toggle_text, callback_data="boost_toggle")],
        [InlineKeyboardButton(text="✏️ Изменить время", callback_data="boost_time")],
        [InlineKeyboardButton(text="✏️ Изменить бонус", callback_data="boost_bonus")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="a_home")],
    ]
    await cb.answer()
    await safe_edit(cb, text, InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data == "boost_toggle")
async def admin_boost_toggle(cb: CallbackQuery):
    s = await db.get_settings()
    await db.set_setting_field("night_boost_enabled", 0 if s["night_boost_enabled"] else 1)
    await cb.answer("Изменено")
    await admin_boost(cb)


@admin_router.callback_query(F.data == "boost_time")
async def admin_boost_time(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminBoost.waiting_time)
    await cb.answer()
    await cb.message.answer("Введите время в формате ЧЧ:ММ-ЧЧ:ММ (например 01:00-09:00):")


@admin_router.message(AdminBoost.waiting_time)
async def admin_boost_time_value(message: Message, state: FSMContext):
    m = re.match(r"^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$", message.text.strip())
    if not m:
        await message.answer("⚠️ Формат: 01:00-09:00")
        return
    await db.set_setting_field("night_boost_start", m.group(1))
    await db.set_setting_field("night_boost_end", m.group(2))
    await state.clear()
    await message.answer("✅ Время буста обновлено.", reply_markup=admin_main_kb())


@admin_router.callback_query(F.data == "boost_bonus")
async def admin_boost_bonus(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminBoost.waiting_bonus)
    await cb.answer()
    await cb.message.answer("Введите бонус в рублях (например 0.75):")


@admin_router.message(AdminBoost.waiting_bonus)
async def admin_boost_bonus_value(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введите число.")
        return
    await db.set_setting_field("night_boost_bonus", value)
    await state.clear()
    await message.answer("✅ Бонус буста обновлён.", reply_markup=admin_main_kb())


# ---- Юзеры ----

@admin_router.callback_query(F.data == "a_users")
async def admin_users(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminUsers.waiting_id)
    await cb.answer()
    await cb.message.answer("Введите user_id для просмотра профиля:")


@admin_router.message(AdminUsers.waiting_id)
async def admin_users_lookup(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите числовой ID.")
        return
    u = await db.get_user(user_id)
    await state.clear()
    if not u:
        await message.answer("Пользователь не найден.", reply_markup=admin_main_kb())
        return
    text = (
        f"👤 ID: {u['user_id']} (@{u['username']})\n"
        f"С нами с: {u['joined_at']}\n"
        f"Телефон: {u['phone']}\nФИО: {u['fio']}\nБанк: {u['bank']}\n"
        f"Оборот: {fmt(u['turnover'])}\nВыплачено: {fmt(u['total_rub'])} ₽\n"
        f"Заблокирован: {'да' if u['is_blocked'] else 'нет'}"
    )
    await message.answer(text, reply_markup=admin_main_kb())


# ---- Блокировки ----

@admin_router.callback_query(F.data == "a_blocks")
async def admin_blocks(cb: CallbackQuery):
    rows = [
        [InlineKeyboardButton(text="🚫 Заблокировать по ID", callback_data="block_user")],
        [InlineKeyboardButton(text="✅ Разблокировать по ID", callback_data="unblock_user")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="a_home")],
    ]
    await cb.answer()
    await safe_edit(cb, "🚫 Блокировки пользователей", InlineKeyboardMarkup(inline_keyboard=rows))


@admin_router.callback_query(F.data == "block_user")
async def admin_block_user_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminUsers.waiting_block_id)
    await cb.answer()
    await cb.message.answer("Введите user_id для блокировки:")


@admin_router.message(AdminUsers.waiting_block_id)
async def admin_block_user_value(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите числовой ID.")
        return
    await db.update_user_field(user_id, "is_blocked", 1)
    await state.clear()
    await message.answer(f"🚫 Пользователь {user_id} заблокирован.", reply_markup=admin_main_kb())


@admin_router.callback_query(F.data == "unblock_user")
async def admin_unblock_user_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminUsers.waiting_unblock_id)
    await cb.answer()
    await cb.message.answer("Введите user_id для разблокировки:")


@admin_router.message(AdminUsers.waiting_unblock_id)
async def admin_unblock_user_value(message: Message, state: FSMContext):
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Введите числовой ID.")
        return
    await db.update_user_field(user_id, "is_blocked", 0)
    await state.clear()
    await message.answer(f"✅ Пользователь {user_id} разблокирован.", reply_markup=admin_main_kb())


# ---- Поддержка (тикеты) ----

@admin_router.callback_query(F.data == "a_support")
async def admin_support(cb: CallbackQuery):
    tickets = await db.open_tickets()
    await cb.answer()
    if not tickets:
        await safe_edit(cb, "💬 Открытых обращений нет.", admin_back_kb())
        return
    lines = [f"#{t['id']} от {t['user_id']}: {t['message'][:60]}" for t in tickets[:20]]
    await safe_edit(cb, "💬 Открытые обращения:\n\n" + "\n".join(lines), admin_back_kb())


# ---- Рассылка ----

@admin_router.callback_query(F.data == "a_broadcast")
async def admin_broadcast_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminBroadcastForm.waiting_text)
    await cb.answer()
    await cb.message.answer("📢 Введите текст рассылки для всех пользователей:")


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
    await message.answer(f"✅ Рассылка отправлена {sent} пользователям.", reply_markup=admin_main_kb())


# ---- Настройки ----

@admin_router.callback_query(F.data == "a_settings")
async def admin_settings(cb: CallbackQuery):
    r = await db.get_rates()
    rows = [
        [InlineKeyboardButton(text="✏️ Мин. USDT", callback_data="set_min_usdt")],
        [InlineKeyboardButton(text="✏️ Мин. GRAM", callback_data="set_min_gram")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="a_home")],
    ]
    await cb.answer()
    await safe_edit(
        cb,
        f"⚙️ Настройки\nМин. USDT: {r['min_usdt']}\nМин. GRAM: {r['min_gram']}",
        InlineKeyboardMarkup(inline_keyboard=rows),
    )


@admin_router.callback_query(F.data == "set_min_usdt")
async def set_min_usdt_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSettings.waiting_min_usdt)
    await cb.answer()
    await cb.message.answer("Введите новую минималку USDT:")


@admin_router.message(AdminSettings.waiting_min_usdt)
async def set_min_usdt_value(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введите число.")
        return
    await db.set_rate_field("min_usdt", value)
    await state.clear()
    await message.answer("✅ Обновлено.", reply_markup=admin_main_kb())


@admin_router.callback_query(F.data == "set_min_gram")
async def set_min_gram_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminSettings.waiting_min_gram)
    await cb.answer()
    await cb.message.answer("Введите новую минималку GRAM:")


@admin_router.message(AdminSettings.waiting_min_gram)
async def set_min_gram_value(message: Message, state: FSMContext):
    try:
        value = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("⚠️ Введите число.")
        return
    await db.set_rate_field("min_gram", value)
    await state.clear()
    await message.answer("✅ Обновлено.", reply_markup=admin_main_kb())


# ============================== SUPPORT (пользователь -> тикет) ============
# Если хотите принимать сообщения поддержки внутри бота (а не только через
# ЛС @xylt_admin), можно включить эту ветку — она сохранит любое сообщение
# от пользователя вне известных состояний как тикет поддержки.

@user_router.message(F.text & ~F.text.startswith("/"))
async def fallback_message(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is not None:
        return  # уже обрабатывается другим хэндлером/состоянием
    await message.answer(
        "Не понимаю команду. Используйте меню ниже 👇",
        reply_markup=main_menu_kb(),
    )


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
