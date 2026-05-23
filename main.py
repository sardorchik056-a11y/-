import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
import sqlite3
import os
import time
import requests
import threading
from datetime import datetime

BOT_TOKEN = "8918670807:AAHFkCF8kemTCIVlbeLfmRkPUd6gk3wdKVo"
CRYPTOBOT_TOKEN = "562214:AABJIaVpSkcIR7FvY7B8Oh3TszuqCUgi0Tk"
ADMIN_IDS = [8118184388, 8276697984, 8115654734]

bot = telebot.TeleBot(BOT_TOKEN)

try:
    bot.remove_webhook()
    print("✅ Вебхук удалён")
except:
    pass
time.sleep(1)

DB_FILE = "bot.db"

user_states = {}
active_invoices = {}
user_stock_cap = {}

EMOJI_CATALOG   = "6030776052345737530"
EMOJI_REFERRAL  = "5258513401784573443"
EMOJI_SUPPORT   = "5357069174512303778"
EMOJI_TERMS     = "5258501105293205250"
EMOJI_BALANCE   = "5258204546391351475"
EMOJI_BACK      = "6039539366177541657"
EMOJI_PAY       = "6030776052345737530"
EMOJI_CANCEL    = "6039539366177541657"
EMOJI_REF_LINK  = "5260730055880876557"
EMOJI_REF_STATS = "5258330865674494479"
EMOJI_HOME      = "5260399854500191689"
EMOJI_INVITE    = "5258513401784573443"
EMOJI_BUY       = "5258185631355378853"
EMOJI_DEPOSIT   = "6039496266180726678"
EMOJI_CUSTOM    = "6039496266180726678"
EMOJI_AGREE     = "6041720006973067267"
EMOJI_CUSTOMM = "5258215846450305872"
_db_lock = threading.Lock()
_conn: sqlite3.Connection = None

def _open_connection():
    global _conn
    _conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=15)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    _conn.execute("PRAGMA synchronous=NORMAL")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA cache_size=-8000")
    _conn.commit()

def db_exec(query: str, params=(), fetchone=False, fetchall=False):
    with _db_lock:
        cur = _conn.execute(query, params)
        _conn.commit()
        if fetchone:
            return cur.fetchone()
        if fetchall:
            return cur.fetchall()
        return cur

def init_db():
    with _db_lock:
        _conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id           INTEGER PRIMARY KEY,
                username          TEXT,
                balance           REAL    NOT NULL DEFAULT 0.0,
                total_bought      INTEGER NOT NULL DEFAULT 0,
                referrer_id       INTEGER,
                referral_earnings REAL    NOT NULL DEFAULT 0.0,
                is_banned         INTEGER NOT NULL DEFAULT 0,
                is_approved       INTEGER NOT NULL DEFAULT 0,
                registered_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS referrals (
                referrer_id INTEGER NOT NULL,
                referral_id INTEGER NOT NULL,
                PRIMARY KEY (referrer_id, referral_id)
            );

            CREATE TABLE IF NOT EXISTS applications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                username     TEXT,
                status       TEXT NOT NULL DEFAULT 'pending',
                applied_at   TEXT
            );

            CREATE TABLE IF NOT EXISTS products (
                product_key TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                emoji       TEXT NOT NULL DEFAULT '📦',
                price       REAL NOT NULL,
                stock       INTEGER NOT NULL DEFAULT 0,
                description TEXT NOT NULL DEFAULT ''
            );

            CREATE TABLE IF NOT EXISTS product_items (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                product_key  TEXT NOT NULL,
                content      TEXT NOT NULL,
                is_used      INTEGER NOT NULL DEFAULT 0,
                added_at     TEXT
            );

            CREATE TABLE IF NOT EXISTS purchases (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL,
                product_key  TEXT    NOT NULL,
                quantity     INTEGER NOT NULL,
                amount       REAL    NOT NULL,
                purchased_at TEXT
            );

            CREATE TABLE IF NOT EXISTS udv_mode (
                user_id INTEGER PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        _conn.commit()

        count = _conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        if count == 0:
            _conn.executemany(
                "INSERT INTO products(product_key,name,emoji,price,stock,description) VALUES(?,?,?,?,?,?)",
                [
                    ('web_token', 'Web Token', '<tg-emoji emoji-id="5258503720928288433">🎟</tg-emoji>', 2.50, 0,
                     "Токен доступа, готов к использованию"),
                    ('json',      'JSON',       '<tg-emoji emoji-id="5258477770735885832">🎟</tg-emoji>', 3.00, 0,
                     "Полные данные в JSON формате"),
                    ('autoreg',   'Авторег',    '<tg-emoji emoji-id="6030400221232501136">🎟</tg-emoji>', 1.80, 0,
                     "Аккаунт зарегистрированный на SIM"),
                ]
            )
            _conn.commit()

def register_user(user_id: int, username: str = None):
    db_exec(
        """INSERT OR IGNORE INTO users
           (user_id, username, balance, total_bought, referral_earnings, is_banned, is_approved, registered_at)
           VALUES (?, ?, 0.0, 0, 0.0, 0, 0, ?)""",
        (user_id, username, str(datetime.now()))
    )

def get_user(user_id: int):
    return db_exec("SELECT * FROM users WHERE user_id=?", (user_id,), fetchone=True)

def get_all_users():
    return db_exec("SELECT * FROM users", fetchall=True)

def get_user_balance(user_id: int) -> float:
    row = db_exec("SELECT balance FROM users WHERE user_id=?", (user_id,), fetchone=True)
    return round(row["balance"], 2) if row else 0.0

def add_balance(user_id: int, amount: float):
    db_exec(
        "UPDATE users SET balance=ROUND(balance+?,2) WHERE user_id=?",
        (amount, user_id)
    )

def deduct_balance(user_id: int, amount: float) -> bool:
    with _db_lock:
        row = _conn.execute(
            "SELECT balance FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row and row["balance"] >= amount:
            _conn.execute(
                "UPDATE users SET balance=ROUND(balance-?,2) WHERE user_id=?",
                (amount, user_id)
            )
            _conn.commit()
            return True
    return False

def set_banned(user_id: int, banned: bool):
    db_exec("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))

def set_approved(user_id: int, approved: bool):
    db_exec("UPDATE users SET is_approved=? WHERE user_id=?", (1 if approved else 0, user_id))

def is_approved(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    row = db_exec("SELECT is_approved FROM users WHERE user_id=?", (user_id,), fetchone=True)
    return bool(row["is_approved"]) if row else False

def is_udv_mode_enabled(user_id: int) -> bool:
    """Проверяет включен ли режим UDV для пользователя"""
    row = db_exec("SELECT enabled FROM udv_mode WHERE user_id=?", (user_id,), fetchone=True)
    return bool(row["enabled"]) if row else False

def set_udv_mode(user_id: int, enabled: bool):
    """Включает/выключает режим UDV"""
    db_exec(
        "INSERT OR REPLACE INTO udv_mode (user_id, enabled) VALUES (?, ?)",
        (user_id, 1 if enabled else 0)
    )

def get_setting(key: str) -> str | None:
    row = db_exec("SELECT value FROM settings WHERE key=?", (key,), fetchone=True)
    return row["value"] if row else None

def set_setting(key: str, value: str):
    db_exec(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value)
    )

def create_application(user_id: int, username: str = None):
    """Создаёт заявку, если ещё нет активной/принятой."""
    existing = db_exec(
        "SELECT * FROM applications WHERE user_id=? AND status IN ('pending','approved')",
        (user_id,), fetchone=True
    )
    if existing:
        return False
    db_exec(
        "INSERT INTO applications(user_id,username,status,applied_at) VALUES(?,?,?,?)",
        (user_id, username, "pending", str(datetime.now()))
    )
    return True

def get_pending_applications():
    return db_exec(
        "SELECT * FROM applications WHERE status='pending'", fetchall=True
    )

def set_application_status(user_id: int, status: str):
    db_exec(
        "UPDATE applications SET status=? WHERE user_id=? AND status='pending'",
        (status, user_id)
    )

def add_referral(referrer_id: int, referral_id: int):
    db_exec(
        "INSERT OR IGNORE INTO referrals(referrer_id,referral_id) VALUES(?,?)",
        (referrer_id, referral_id)
    )
    db_exec(
        "UPDATE users SET referrer_id=? WHERE user_id=? AND referrer_id IS NULL",
        (referrer_id, referral_id)
    )

def get_referrals(referrer_id: int):
    return db_exec(
        "SELECT referral_id FROM referrals WHERE referrer_id=?",
        (referrer_id,), fetchall=True
    )

def add_referral_earning(referrer_id: int, amount: float):
    db_exec(
        """UPDATE users
           SET balance=ROUND(balance+?,2),
               referral_earnings=ROUND(referral_earnings+?,2)
           WHERE user_id=?""",
        (amount, amount, referrer_id)
    )

def get_all_products() -> dict:
    rows = db_exec("SELECT * FROM products", fetchall=True)
    return {r["product_key"]: dict(r) for r in rows}

def get_product(product_key: str):
    row = db_exec(
        "SELECT * FROM products WHERE product_key=?", (product_key,), fetchone=True
    )
    return dict(row) if row else None

def upsert_product(product_key, name, emoji, price, stock, description):
    db_exec(
        """INSERT INTO products(product_key,name,emoji,price,stock,description)
           VALUES(?,?,?,?,?,?)
           ON CONFLICT(product_key) DO UPDATE SET
             name=excluded.name, emoji=excluded.emoji,
             price=excluded.price, stock=excluded.stock,
             description=excluded.description""",
        (product_key, name, emoji, price, stock, description)
    )

def update_product_field(product_key: str, field: str, value):
    allowed = {"name", "emoji", "price", "stock", "description"}
    if field not in allowed:
        return
    db_exec(f"UPDATE products SET {field}=? WHERE product_key=?", (value, product_key))

def add_stock(product_key: str, amount: int):
    db_exec(
        "UPDATE products SET stock=stock+? WHERE product_key=?",
        (amount, product_key)
    )

def delete_product(product_key: str):
    db_exec("DELETE FROM products WHERE product_key=?", (product_key,))
    db_exec("DELETE FROM product_items WHERE product_key=?", (product_key,))

def set_product_items(product_key: str, items: list):
    """Заменяет весь набор текстов товара (фиксированный контент для выдачи всем)."""
    with _db_lock:
        _conn.execute("DELETE FROM product_items WHERE product_key=?", (product_key,))
        _conn.executemany(
            "INSERT INTO product_items(product_key,content,is_used,added_at) VALUES(?,?,0,?)",
            [(product_key, item, str(datetime.now())) for item in items]
        )
        _conn.commit()

def add_product_items(product_key: str, items: list):
    """Добавляет тексты к существующему набору товара."""
    with _db_lock:
        _conn.executemany(
            "INSERT INTO product_items(product_key,content,is_used,added_at) VALUES(?,?,0,?)",
            [(product_key, item, str(datetime.now())) for item in items]
        )
        _conn.commit()

def get_all_items(product_key: str) -> list:
    """Возвращает весь фиксированный набор текстов товара (не расходуются)."""
    rows = db_exec(
        "SELECT content FROM product_items WHERE product_key=? ORDER BY id",
        (product_key,), fetchall=True
    )
    return [r["content"] for r in rows]

def get_items_stats(product_key: str) -> dict:
    total = db_exec(
        "SELECT COUNT(*) as cnt FROM product_items WHERE product_key=?",
        (product_key,), fetchone=True
    )
    free = db_exec(
        "SELECT COUNT(*) as cnt FROM product_items WHERE product_key=? AND is_used=0",
        (product_key,), fetchone=True
    )
    used = db_exec(
        "SELECT COUNT(*) as cnt FROM product_items WHERE product_key=? AND is_used=1",
        (product_key,), fetchone=True
    )
    return {
        "total": total["cnt"] if total else 0,
        "free":  free["cnt"]  if free  else 0,
        "used":  used["cnt"]  if used  else 0,
    }

def delete_product_items(product_key: str):
    """Удаляет весь контент товара."""
    db_exec("DELETE FROM product_items WHERE product_key=?", (product_key,))

def add_purchase(user_id: int, product_key: str, quantity: int, amount: float):
    db_exec(
        """INSERT INTO purchases(user_id,product_key,quantity,amount,purchased_at)
           VALUES(?,?,?,?,?)""",
        (user_id, product_key, quantity, amount, str(datetime.now()))
    )
    db_exec(
        "UPDATE users SET total_bought=total_bought+? WHERE user_id=?",
        (quantity, user_id)
    )

def get_user_purchases(user_id: int):
    return db_exec(
        "SELECT * FROM purchases WHERE user_id=?", (user_id,), fetchall=True
    )

def get_all_purchases():
    return db_exec("SELECT * FROM purchases", fetchall=True)

def create_invoice(amount: float, user_id: int):
    url = "https://pay.crypt.bot/api/createInvoice"
    headers = {
        "Crypto-Pay-API-Token": CRYPTOBOT_TOKEN,
        "Content-Type": "application/json"
    }
    data = {
        "asset": "USDT",
        "amount": str(amount),
        "description": f"Пополнение баланса. User ID: {user_id}",
        "expires_in": 3600
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=10)
        result = r.json()
        if result.get("ok"):
            inv = result["result"]
            return inv["invoice_id"], inv["bot_invoice_url"]
    except Exception as e:
        print(f"Ошибка создания инвойса: {e}")
    return None, None

def check_invoice_status(invoice_id):
    url = "https://pay.crypt.bot/api/getInvoices"
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
    try:
        r = requests.get(url, headers=headers,
                         params={"invoice_ids": str(invoice_id)}, timeout=10)
        result = r.json()
        if result.get("ok"):
            items = result["result"].get("items", [])
            if items:
                return items[0].get("status")
    except Exception as e:
        print(f"Ошибка проверки инвойса: {e}")
    return None

def payment_watcher():
    while True:
        time.sleep(3)
        if not active_invoices:
            continue
        to_remove = []
        for invoice_id, info in list(active_invoices.items()):
            try:
                status = check_invoice_status(invoice_id)
                if status == "paid":
                    uid      = info["user_id"]
                    amount   = info["amount"]
                    chat_id  = info["chat_id"]
                    msg_id   = info["message_id"]

                    add_balance(uid, amount)
                    user_stock_cap[uid] = True

                    user = get_user(uid)
                    referrer_id = user["referrer_id"] if user else None
                    if referrer_id:
                        bonus = round(amount * 0.1, 2)
                        add_referral_earning(referrer_id, bonus)
                        try:
                            bot.send_message(int(referrer_id),
                                f"🎁 Ваш реферал пополнил баланс на {amount}$!\n"
                                f"💰 Вам начислено: +{bonus}$")
                        except:
                            pass

                    # Не показываем уведомление если включен режим UDV
                    if is_udv_mode_enabled(uid):
                        to_remove.append(invoice_id)
                        continue

                    text = (f'<tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> Оплата подтверждена!\n\n'
                            f'<tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Пополнено: {amount}$\n'
                            f'<tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Текущий баланс: {get_user_balance(uid)}$')
                    try:
                        bot.edit_message_caption(
                            caption=text, chat_id=chat_id, message_id=msg_id,
                            reply_markup=None, parse_mode="HTML")
                    except:
                        try:
                            bot.edit_message_text(text, chat_id=chat_id,
                                                  message_id=msg_id, reply_markup=None,
                                                  parse_mode="HTML")
                        except:
                            pass
                    to_remove.append(invoice_id)

                elif status == "expired":
                    try:
                        try:
                            bot.edit_message_caption(
                                caption="⏰ Счёт истёк. Создайте новый.",
                                chat_id=info["chat_id"], message_id=info["message_id"],
                                reply_markup=None, parse_mode="HTML")
                        except:
                            bot.edit_message_text(
                                "⏰ Счёт истёк. Создайте новый.",
                                chat_id=info["chat_id"], message_id=info["message_id"],
                                reply_markup=None, parse_mode="HTML")
                    except:
                        pass
                    to_remove.append(invoice_id)
            except Exception as e:
                print(f"Ошибка watcher: {e}")
        for inv_id in to_remove:
            active_invoices.pop(inv_id, None)

def apply_keyboard():
    """Клавиатура для неодобренного пользователя."""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("📋 Подать заявку", callback_data="send_application"))
    return kb

def oferta_keyboard():
    """Клавиатура после отправки заявки — кнопка ознакомления с офертой."""
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("✅ Ознакомился", callback_data="oferta_acknowledged"))
    return kb

# Айди кастомных эмодзи для кнопок главного меню
EMOJI_BTN_SHOP      = "5257965810634202885"   # Шоп
EMOJI_BTN_PURCHASES = "5258134813302332906"   # Мои покупки
EMOJI_BTN_DEPOSIT   = "5879814368572478751"   # Пополнить
EMOJI_BTN_REF       = "5258513401784573443"   # Рефералка
EMOJI_BTN_HELP      = "6035191085452497972"   # Help

# Айди кастомных эмодзи для кнопок каталога (замени на свои)
EMOJI_CAT_HEADER  = "5188212140133080599"   # Иконка заголовка ВИТРИНА
EMOJI_CAT_ITEM_1  = "5258503720928288433"   # Иконка 1-го товара в кнопке
EMOJI_CAT_ITEM_2  = "5258477770735885832"   # Иконка 2-го товара в кнопке
EMOJI_CAT_ITEM_3  = "6030400221232501136"   # Иконка 3-го товара в кнопке
EMOJI_CAT_DEPOSIT = "5879814368572478751"   # Иконка кнопки Пополнить
EMOJI_CAT_BACK    = "6039539366177541657"   # Иконка кнопки Назад
# Если товаров больше 3 — добавь EMOJI_CAT_ITEM_4 и т.д.
CATALOG_ITEM_EMOJIS = [EMOJI_CAT_ITEM_1, EMOJI_CAT_ITEM_2, EMOJI_CAT_ITEM_3]

# Айди кастомных эмодзи для раздела "Мои покупки" (замени на свои)
EMOJI_PUR_HEADER  = "ПОСТАВЬ_СВОЙ_ID"   # Иконка заголовка МОИ ПОКУПКИ
EMOJI_PUR_ITEM    = "ПОСТАВЬ_СВОЙ_ID"   # Иконка каждой покупки в кнопке
EMOJI_PUR_SHOP    = "ПОСТАВЬ_СВОЙ_ID"   # Иконка кнопки В ШОП
EMOJI_PUR_BACK    = "ПОСТАВЬ_СВОЙ_ID"   # Иконка кнопки Назад

def main_menu_keyboard(user_id=None):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton(" Шоп",        callback_data="catalog",
                             icon_custom_emoji_id=EMOJI_BTN_SHOP),
        InlineKeyboardButton(" Мои покупки", callback_data="my_purchases",
                             icon_custom_emoji_id=EMOJI_BTN_PURCHASES),
    )
    kb.row(
        InlineKeyboardButton(" Пополнить",  callback_data="balance",
                             icon_custom_emoji_id=EMOJI_BTN_DEPOSIT),
        InlineKeyboardButton(" Рефералка",  callback_data="referral",
                             icon_custom_emoji_id=EMOJI_BTN_REF),
    )
    kb.row(
        InlineKeyboardButton(" Help",       callback_data="support",
                             icon_custom_emoji_id=EMOJI_BTN_HELP),
    )
    return kb

def send_main_menu(message):
    user_id    = message.from_user.id
    username   = message.from_user.username
    first_name = message.from_user.first_name
    text       = get_profile_text(user_id, username, first_name)
    kb         = main_menu_keyboard(user_id)

    # Пробуем получить фото профиля пользователя
    profile_photo_id = None
    try:
        photos = bot.get_user_profile_photos(user_id, limit=1)
        if photos and photos.total_count > 0:
            profile_photo_id = photos.photos[0][-1].file_id
    except:
        pass

    if profile_photo_id:
        bot.send_photo(user_id, profile_photo_id, caption=text,
                       reply_markup=kb, parse_mode="HTML")
    else:
        # Фото нет — используем сохранённое фото меню (если есть)
        photo_id = get_setting("menu_photo_file_id")
        if photo_id:
            bot.send_photo(user_id, photo_id, caption=text,
                           reply_markup=kb, parse_mode="HTML")
        else:
            bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")

def catalog_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    products = list(get_all_products().items())
    for i, (key, p) in enumerate(products):
        emoji_id = CATALOG_ITEM_EMOJIS[i] if i < len(CATALOG_ITEM_EMOJIS) else EMOJI_CAT_ITEM_1
        kb.row(InlineKeyboardButton(
            f" {p['name']} | {p['price']}$",
            callback_data=f"buy_{key}",
            icon_custom_emoji_id=emoji_id
        ))
    kb.row(InlineKeyboardButton(
        " Пополнить", callback_data="balance",
        icon_custom_emoji_id=EMOJI_CAT_DEPOSIT
    ))
    kb.row(InlineKeyboardButton(
        " Назад", callback_data="back_to_menu",
        icon_custom_emoji_id=EMOJI_CAT_BACK
    ))
    return kb

def admin_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📦 Товары",            callback_data="admin_products"),
        InlineKeyboardButton("👥 Пользователи",      callback_data="admin_users"),
        InlineKeyboardButton("💰 Пополнения",        callback_data="admin_deposits"),
        InlineKeyboardButton("📢 Рассылка",          callback_data="admin_mailing"),
        InlineKeyboardButton("📊 Статистика",        callback_data="admin_stats"),
        InlineKeyboardButton("⚠️ Бан пользователя", callback_data="admin_ban"),
        InlineKeyboardButton("📋 Заявки",            callback_data="admin_applications"),
        InlineKeyboardButton("🔙 Выход",             callback_data="back_to_menu"),
    )
    return kb

def admin_products_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("➕ Добавить товар",        callback_data="add_product"),
        InlineKeyboardButton("✏️ Управление товаром",    callback_data="manage_product_list"),
        InlineKeyboardButton("📦 Добавить контент",      callback_data="add_items_select"),
        InlineKeyboardButton("📊 Статистика контента",   callback_data="items_stats_select"),
        InlineKeyboardButton("🗑 Очистить контент",      callback_data="clear_items_select"),
        InlineKeyboardButton("❌ Удалить товар",         callback_data="delete_product"),
        InlineKeyboardButton("◀️ Назад",                 callback_data="admin_panel"),
    )
    return kb

def manage_product_list_keyboard(action_prefix: str):
    kb = InlineKeyboardMarkup(row_width=1)
    for key, p in get_all_products().items():
        stats = get_items_stats(key)
        kb.add(InlineKeyboardButton(
            f"{p['emoji']} {p['name']} | {p['price']}$ | 📦{stats['free']} свободно",
            callback_data=f"{action_prefix}{key}"
        ))
    kb.add(InlineKeyboardButton("◀️ Назад", callback_data="admin_products"))
    return kb

def product_manage_keyboard(product_key: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("📦 Добавить контент",     callback_data=f"items_add_{product_key}"),
        InlineKeyboardButton("📊 Контент статистика",   callback_data=f"items_stat_{product_key}"),
        InlineKeyboardButton("💰 Изменить цену",        callback_data=f"prod_setprice_{product_key}"),
        InlineKeyboardButton("✏️ Изменить название",    callback_data=f"prod_setname_{product_key}"),
        InlineKeyboardButton("📝 Изменить описание",    callback_data=f"prod_setdesc_{product_key}"),
        InlineKeyboardButton("🎭 Изменить эмодзи",     callback_data=f"prod_setemoji_{product_key}"),
        InlineKeyboardButton("🔢 Изменить остаток",     callback_data=f"prod_setstock_{product_key}"),
        InlineKeyboardButton("📋 Полное редактирование", callback_data=f"prod_full_{product_key}"),
        InlineKeyboardButton("◀️ Назад",                callback_data="manage_product_list"),
    )
    return kb

def admin_users_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("📋 Список пользователей", callback_data="admin_user_list"),
        InlineKeyboardButton("🔍 Найти пользователя",   callback_data="admin_find_user"),
        InlineKeyboardButton("◀️ Назад",                callback_data="admin_panel"),
    )
    return kb

def admin_deposits_keyboard():
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("💰 Ручное зачисление", callback_data="admin_manual_deposit"),
        InlineKeyboardButton("◀️ Назад",             callback_data="admin_panel"),
    )
    return kb

def referral_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(" Моя ссылка",   callback_data="copy_ref_link",
                             icon_custom_emoji_id=EMOJI_REF_LINK),
        InlineKeyboardButton(" Мои рефералы", callback_data="my_referrals",
                             icon_custom_emoji_id=EMOJI_REF_STATS),
        InlineKeyboardButton(" Главное меню", callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_HOME),
    )
    return kb

def my_referrals_keyboard(has_referrals=False):
    kb = InlineKeyboardMarkup(row_width=2)
    if not has_referrals:
        kb.add(
            InlineKeyboardButton(" Моя ссылка",   callback_data="copy_ref_link",
                                 icon_custom_emoji_id=EMOJI_REF_LINK),
            InlineKeyboardButton(" Главное меню", callback_data="back_to_menu",
                                 icon_custom_emoji_id=EMOJI_HOME),
        )
    else:
        kb.add(
            InlineKeyboardButton(" Пригласить ещё", callback_data="referral",
                                 icon_custom_emoji_id=EMOJI_INVITE),
            InlineKeyboardButton(" Главное меню",   callback_data="back_to_menu",
                                 icon_custom_emoji_id=EMOJI_HOME),
        )
    return kb

def support_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(" Назад", callback_data="back_to_menu",
                                icon_custom_emoji_id=EMOJI_BACK))
    return kb

def terms_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(" Согласен", callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_AGREE),
        InlineKeyboardButton(" Назад",    callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_BACK),
    )
    return kb

def balance_info_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("Пополнить", callback_data="deposit_menu"),
        InlineKeyboardButton(" Назад", callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_BACK),
    )
    return kb

def balance_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.row(
        InlineKeyboardButton("5$",  callback_data="deposit_5"),
        InlineKeyboardButton("10$", callback_data="deposit_10"),
    )
    kb.row(
        InlineKeyboardButton("25$", callback_data="deposit_25"),
        InlineKeyboardButton("50$", callback_data="deposit_50"),
    )
    kb.row(
        InlineKeyboardButton("100$", callback_data="deposit_100"),
        InlineKeyboardButton("250$", callback_data="deposit_250"),
    )
    kb.row(InlineKeyboardButton(" Другая сумма", callback_data="deposit_custom",
                                icon_custom_emoji_id=EMOJI_CUSTOMM))
    kb.row(InlineKeyboardButton(" Назад", callback_data="balance",
                                icon_custom_emoji_id=EMOJI_BACK))
    return kb

def payment_keyboard(invoice_url: str):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(" Оплатить", url=invoice_url,
                             icon_custom_emoji_id=EMOJI_PAY),
        InlineKeyboardButton(" Отмена",   callback_data="cancel_payment",
                             icon_custom_emoji_id=EMOJI_CANCEL),
    )
    return kb

def buy_product_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton(" В каталог", callback_data="catalog",
                                icon_custom_emoji_id=EMOJI_BACK))
    return kb

def confirm_buy_keyboard(product_key: str, quantity: int, insufficient=False):
    kb = InlineKeyboardMarkup(row_width=2)
    if insufficient:
        kb.add(
            InlineKeyboardButton(" Пополнить баланс", callback_data="balance",
                                 icon_custom_emoji_id=EMOJI_BALANCE),
            InlineKeyboardButton(" Каталог",          callback_data="catalog",
                                 icon_custom_emoji_id=EMOJI_BACK),
        )
    else:
        kb.add(
            InlineKeyboardButton(" Купить", callback_data=f"confirm_buy_{product_key}_{quantity}",
                                 icon_custom_emoji_id=EMOJI_BUY),
            InlineKeyboardButton(" Отмена", callback_data="cancel_buy",
                                 icon_custom_emoji_id=EMOJI_CANCEL),
        )
    return kb

def after_buy_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(" Поддержка",    callback_data="support",
                             icon_custom_emoji_id=EMOJI_SUPPORT),
        InlineKeyboardButton(" Главное меню", callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_HOME),
    )
    return kb

def cancel_buy_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(" Каталог",      callback_data="catalog",
                             icon_custom_emoji_id=EMOJI_CATALOG),
        InlineKeyboardButton(" Главное меню", callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_HOME),
    )
    return kb

def cancel_payment_keyboard():
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton(" Баланс",       callback_data="balance",
                             icon_custom_emoji_id=EMOJI_BALANCE),
        InlineKeyboardButton(" Главное меню", callback_data="back_to_menu",
                             icon_custom_emoji_id=EMOJI_HOME),
    )
    return kb

def back_to_admin_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️Назад", callback_data="admin_panel"))
    return kb

def back_to_admin_users_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("◀️Назад", callback_data="admin_users"))
    return kb

def application_admin_keyboard(app_user_id: int):
    """Кнопки Принять/Отклонить для конкретной заявки."""
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("✅ Принять",   callback_data=f"app_approve_{app_user_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"app_reject_{app_user_id}"),
    )
    return kb

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def edit_message(chat_id, message_id, text, keyboard=None):
    """Умное редактирование: caption для фото-сообщений, text для обычных."""
    # Сначала пробуем edit_message_caption (работает если сообщение с фото)
    try:
        bot.edit_message_caption(
            caption=text, chat_id=chat_id, message_id=message_id,
            reply_markup=keyboard, parse_mode="HTML")
        return
    except Exception as e:
        if "there is no caption" not in str(e).lower() and "message is not modified" not in str(e).lower():
            pass  # сообщение не является фото — пробуем edit_message_text
    try:
        bot.edit_message_text(text, chat_id=chat_id, message_id=message_id,
                              reply_markup=keyboard, parse_mode="HTML")
    except:
        pass

def notify_admins_about_application(user_id: int, username: str = None):
    text = (f"📋 НОВАЯ ЗАЯВКА\n\n"
            f"👤 ID: {user_id}\n"
            f"Username: @{username or 'нет'}")
    kb = application_admin_keyboard(user_id)
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, reply_markup=kb)
        except:
            pass

def product_info_text(product_key: str, product: dict) -> str:
    stats = get_items_stats(product_key)
    text  = f"📦 ТОВАР: {product['emoji']} {product['name']}\n\n"
    text += f"🔑 Ключ: {product_key}\n"
    text += f"💰 Цена: {product['price']}$\n"
    text += f"📊 Остаток: {stats['free']} шт (всего: {stats['total']}, выдано: {stats['used']})\n"
    text += f"📝 Описание: {product['description']}\n"
    return text

def get_display_stock(user_id: int, real_stock: int) -> int:
    if user_stock_cap.get(user_id, False):
        return min(real_stock, 24)
    return real_stock

def get_profile_text(user_id: int, username: str = None, first_name: str = None) -> str:
    user = get_user(user_id)
    balance      = round(user["balance"], 2) if user else 0.0
    total_bought = user["total_bought"]      if user else 0
    try:
        reg_date = datetime.strptime(user["registered_at"][:19], "%Y-%m-%d %H:%M:%S") if user and user["registered_at"] else datetime.now()
        days_in_bot = (datetime.now() - reg_date).days
    except:
        days_in_bot = 0
    name = first_name or username or "Пользователь"

    # Рейтинг: 12 блоков (10 заполненных, 2 пустых — по умолчанию)
    filled = min(10, total_bought)
    rating_bar = "█" * filled + "□" * (12 - filled)

    line = "──────────────────"
    text  = f'╭{line}╮\n'
    text += f'│ <b><tg-emoji emoji-id="5454158795729029479">🎟</tg-emoji> HER|SHOP  │  LVL 1  │  ТОРГОВЕЦ\n'
    text += f'├{line}┤\n'
    text += f'│\n'
    text += f'│ <tg-emoji emoji-id="5454130320095862431">🎟</tg-emoji> {name}\n'
    text += f'│ <tg-emoji emoji-id="5456125594397856810">🎟</tg-emoji> {user_id}\n'
    text += f'│ <tg-emoji emoji-id="5463216615468324631">🎟</tg-emoji> @{username or "none"}\n'
    text += f'│\n'
    text += f'│ <tg-emoji emoji-id="5199527184229751349">🎟</tg-emoji> {balance:.2f} <tg-emoji emoji-id="5406841020769936275">🎟</tg-emoji>\n'
    text += f'│ <tg-emoji emoji-id="5467839602301623490">🎟</tg-emoji> {total_bought} акков куплено\n'
    text += f'│ <tg-emoji emoji-id="5472213472441817609">🎟</tg-emoji> {days_in_bot} days в системе\n'
    text += f'│\n'
    text += f'│ <tg-emoji emoji-id="5460914671911460239">🎟</tg-emoji> Рейтинг: {rating_bar}\n'
    text += f'│ <tg-emoji emoji-id="5416081784641168838">🎟</tg-emoji> Статус: Active</b>\n'
    text += f'╰{line}╯\n'
    return text

@bot.message_handler(commands=["start"])
def start_command(message):
    user_id  = message.from_user.id
    username = message.from_user.username
    register_user(user_id, username)

    user = get_user(user_id)
    if user and user["is_banned"]:
        return

    args = message.text.split()
    if len(args) > 1 and args[1].isdigit():
        referrer_id = int(args[1])
        if (referrer_id != user_id
                and get_user(referrer_id) is not None
                and user and user["referrer_id"] is None):
            add_referral(referrer_id, user_id)

    send_main_menu(message)

@bot.message_handler(commands=["admin"])
def admin_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(user_id, "⛔ Нет доступа!")
        return
    text = ("👑 АДМИН ПАНЕЛЬ | MAX\n\n━━━━━━━━━━━━━━━\n\n"
            "1 — 📦 Товары\n2 — 👥 Пользователи\n"
            "3 — 💰 Пополнения\n4 — 📢 Рассылка\n"
            "5 — 📊 Статистика\n6 — ⚠️ Бан\n"
            "7 — 📋 Заявки\n\n━━━━━━━━━━━━━━━")
    bot.send_message(user_id, text, reply_markup=admin_keyboard())

@bot.message_handler(commands=["addfileid"])
def addfileid_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(user_id, "⛔ Нет доступа!")
        return
    user_states[user_id] = {"awaiting_menu_photo": True}
    bot.send_message(user_id, "📸 Отправьте фото, которое будет показываться в главном меню.")

@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    user_id = message.from_user.id
    state = user_states.get(user_id, {})
    if state.get("awaiting_menu_photo"):
        file_id = message.photo[-1].file_id
        set_setting("menu_photo_file_id", file_id)
        del user_states[user_id]
        bot.send_message(
            user_id,
            f"✅ Фото сохранено! Теперь главное меню будет отправляться с этим изображением.\n\nfile_id: <code>{file_id}</code>",
            parse_mode="HTML")

@bot.message_handler(commands=["tall"])
def tall_command(message):
    """Принять все ожидающие заявки."""
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(user_id, "⛔ Нет доступа!")
        return
    apps = get_pending_applications()
    if not apps:
        bot.send_message(user_id, "📋 Нет ожидающих заявок.")
        return
    count = 0
    for app in apps:
        uid = app["user_id"]
        set_application_status(uid, "approved")
        set_approved(uid, True)
        count += 1
        try:
            bot.send_message(uid,
                "✅ Ваша заявка одобрена!\n\n"
                "Добро пожаловать! Теперь у вас есть полный доступ к боту.")
        except:
            pass
    bot.send_message(user_id, f"✅ Принято заявок: {count}")

@bot.message_handler(commands=["call"])
def call_command(message):
    """Отклонить все ожидающие заявки."""
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(user_id, "⛔ Нет доступа!")
        return
    apps = get_pending_applications()
    if not apps:
        bot.send_message(user_id, "📋 Нет ожидающих заявок.")
        return
    count = 0
    for app in apps:
        uid = app["user_id"]
        set_application_status(uid, "rejected")
        count += 1
        try:
            bot.send_message(uid,
                "❌ Ваша заявка отклонена.\n\n"
                "Обратитесь в поддержку для уточнения причины.")
        except:
            pass
    bot.send_message(user_id, f"❌ Отклонено заявок: {count}")

@bot.message_handler(commands=["showb"])
def showb_command(message):
    user_id = message.from_user.id
    if not is_admin(user_id):
        bot.send_message(user_id, "⛔ Нет доступа!")
        return

    users     = get_all_users()
    purchases = get_all_purchases()

    total_balance = round(sum(u["balance"] for u in users), 2)
    total_purchases_amount = round(sum(p["amount"] for p in purchases), 2)

    deposit_stats = {}
    for p in purchases:
        uid = p["user_id"]
        deposit_stats[uid] = deposit_stats.get(uid, 0) + p["amount"]

    top_depositors = sorted(deposit_stats.items(), key=lambda x: x[1], reverse=True)[:5]
    users_with_balance = sum(1 for u in users if u["balance"] > 0)

    text = (
        "💰 ОБЩИЙ БАЛАНС И СТАТИСТИКА ПОПОЛНЕНИЙ\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        f"👥 Всего пользователей: {len(users)}\n"
        f"💵 Пользователей с балансом: {users_with_balance}\n"
        f"💰 Суммарный баланс всех юзеров: {total_balance}$\n"
        f"📦 Всего покупок: {len(purchases)}\n"
        f"📈 Общий оборот (покупки): {total_purchases_amount}$\n\n"
        "━━━━━━━━━━━━━━━\n\n"
        "🏆 ТОП-5 ПОКУПАТЕЛЕЙ:\n"
    )

    for i, (uid, amount) in enumerate(top_depositors, 1):
        u = get_user(uid)
        uname = f"@{u['username']}" if u and u["username"] else f"ID{uid}"
        text += f"{i}. {uname} — {round(amount, 2)}$\n"

    if not top_depositors:
        text += "Пока нет данных\n"

    text += "\n━━━━━━━━━━━━━━━"
    bot.send_message(user_id, text)

@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    user_id    = call.from_user.id
    username   = call.from_user.username
    message_id = call.message.message_id
    chat_id    = call.message.chat.id
    data       = call.data

    user = get_user(user_id)
    if user and user["is_banned"]:
        bot.answer_callback_query(call.id)
        return

    if data == "send_application":
        if is_approved(user_id):
            bot.answer_callback_query(call.id, "✅ У вас уже есть доступ!", show_alert=True)
            return
        created = create_application(user_id, username)
        if created:
            try:
                bot.edit_message_text(
                    "✅ Ваша заявка отправлена на проверку администрации.\n\n"
                    "📄 Изучите оферту: https://graph.org/PRAVILA-05-12-296",
                    chat_id=chat_id, message_id=message_id,
                    reply_markup=oferta_keyboard()
                )
            except:
                bot.send_message(user_id,
                    "✅ Ваша заявка отправлена на проверку администрации.\n\n"
                    "📄 Изучите оферту: https://graph.org/PRAVILA-05-12-296",
                    reply_markup=oferta_keyboard())
            notify_admins_about_application(user_id, username)
        else:
            bot.answer_callback_query(call.id,
                "⏳ Ваша заявка уже отправлена, ожидайте ответа.", show_alert=True)
        bot.answer_callback_query(call.id)
        return

    if data == "oferta_acknowledged":
        try:
            bot.edit_message_text(
                "⏳ Ожидайте — мы уведомим вас о решении.",
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except:
            bot.send_message(user_id, "⏳ Ожидайте — мы уведомим вас о решении.")
        bot.answer_callback_query(call.id)
        return

    if data.startswith("app_approve_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        target_id = int(data[len("app_approve_"):])
        set_application_status(target_id, "approved")
        set_approved(target_id, True)
        target = get_user(target_id)
        uname = f"@{target['username']}" if target and target["username"] else f"ID{target_id}"
        try:
            bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            bot.edit_message_text(
                f"✅ Заявка от {uname} — ПРИНЯТА",
                chat_id=chat_id, message_id=message_id
            )
        except:
            bot.send_message(chat_id, f"✅ Заявка от {uname} принята.")
        try:
            bot.send_message(target_id,
                "✅ Ваша заявка одобрена!\n\n"
                "Добро пожаловать! Теперь у вас есть полный доступ к боту.")
        except:
            pass
        bot.answer_callback_query(call.id, "✅ Принято!")
        return

    if data.startswith("app_reject_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        target_id = int(data[len("app_reject_"):])
        set_application_status(target_id, "rejected")
        target = get_user(target_id)
        uname = f"@{target['username']}" if target and target["username"] else f"ID{target_id}"
        try:
            bot.edit_message_text(
                f"❌ Заявка от {uname} — ОТКЛОНЕНА",
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )
        except:
            bot.send_message(chat_id, f"❌ Заявка от {uname} отклонена.")
        try:
            bot.send_message(target_id,
                "❌ Ваша заявка отклонена.\n\n"
                "Обратитесь в поддержку для уточнения причины.")
        except:
            pass
        bot.answer_callback_query(call.id, "❌ Отклонено!")
        return

    if not is_approved(user_id):
        bot.answer_callback_query(call.id,
            "⛔ У вас нет доступа. Подайте заявку.", show_alert=True)
        return

    if data == "back_to_menu":
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id,
                     get_profile_text(user_id, username, call.from_user.first_name),
                     main_menu_keyboard(user_id))
        bot.answer_callback_query(call.id)

    elif data == "my_purchases":
        purchases = get_user_purchases(user_id)
        line = "──────────────────"
        # Берём последние 50 покупок
        last_purchases = list(purchases)[-50:]
        total_qty    = sum(p["quantity"] for p in last_purchases)
        total_amount = round(sum(p["amount"] for p in last_purchases), 2)

        text  = f"╭{line}╮\n"
        text += f'│ <tg-emoji emoji-id="{EMOJI_PUR_HEADER}">🎟</tg-emoji> МОИ ПОКУПКИ\n'
        text += f"├{line}┤\n"
        if not last_purchases:
            text += "│\n│ У вас пока нет покупок.\n│\n"
        else:
            text += f"│ 📊 Всего: {total_qty} шт. | {total_amount}$\n"
            text += f"│ ПОКАЗЫВАЕТ ПОСЛЕДНИЕ 50 покупок\n"
        text += f"╰{line}╯"

        # Кнопки: каждая покупка отдельной строкой
        kb = InlineKeyboardMarkup(row_width=1)
        for i, p in enumerate(last_purchases, 1):
            prod = get_product(p["product_key"])
            prod_name = prod["name"] if prod else p["product_key"]
            date_str  = str(p["purchased_at"])[:10]
            kb.row(InlineKeyboardButton(
                f" {i} {prod_name} | {p['amount']}$ | {date_str}",
                callback_data="noop",
                icon_custom_emoji_id=EMOJI_PUR_ITEM
            ))
        kb.row(
            InlineKeyboardButton(" В ШОП", callback_data="catalog",
                                 icon_custom_emoji_id=EMOJI_PUR_SHOP),
            InlineKeyboardButton(" Назад", callback_data="back_to_menu",
                                 icon_custom_emoji_id=EMOJI_PUR_BACK),
        )
        edit_message(chat_id, message_id, text, kb)
        bot.answer_callback_query(call.id)

    elif data == "catalog":
        products = get_all_products()
        line = "──────────────────"
        text  = f"╭{line}╮\n"
        text += f'│ <tg-emoji emoji-id="5188212140133080599">🎟</tg-emoji> ВИТРИНА\n'
        text += f"├{line}┤\n"
        text += "│\n"
        for key, p in products.items():
            display_stock = get_display_stock(user_id, p["stock"])
            stock_icon = '<tg-emoji emoji-id="5206607081334906820">🎟</tg-emoji>' if display_stock > 0 else '<tg-emoji emoji-id="5210952531676504517">🎟</tg-emoji>'
            text += f"│ {p['emoji']} {p['name']}\n"
            text += f"│ {stock_icon} {display_stock} шт.\n"
            text += f'│ <tg-emoji emoji-id="5199527184229751349">🎟</tg-emoji> {p['price']}$\n'
            text += "│\n"
        text += f"├{line}┤\n"
        text += f'│ <tg-emoji emoji-id="5199934729381502417">🎟</tg-emoji> Выбери товар\n'
        text += f"╰{line}╯"
        edit_message(chat_id, message_id, text, catalog_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "referral":
        u            = get_user(user_id)
        bot_username = bot.get_me().username
        ref_link     = f"https://t.me/{bot_username}?start={user_id}"
        refs         = get_referrals(user_id)
        ref_earn     = u["referral_earnings"] if u else 0
        bal          = u["balance"] if u else 0
        text  = '<tg-emoji emoji-id="5258513401784573443">🎟</tg-emoji> РЕФЕРАЛЬНАЯ ПРОГРАММА\n\n'
        text += f"Ваша реферальная ссылка:\n<code>{ref_link}</code>\n\n"
        text += "━━━━━━━━━━━━━━━\n\n"
        text += f'<tg-emoji emoji-id="5258513401784573443">🎟</tg-emoji> Приглашено: {len(refs)}\n'
        text += f'<tg-emoji emoji-id="5890848474563352982">🎟</tg-emoji> Заработано: {ref_earn}$\n'
        text += f'<tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: {round(bal,2)}$\n\n'
        text += "━━━━━━━━━━━━━━━\n\n За каждую покупку реферала вы получаете 10%."
        edit_message(chat_id, message_id, text, referral_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "my_referrals":
        refs = get_referrals(user_id)
        if not refs:
            edit_message(chat_id, message_id,
                         " МОИ РЕФЕРАЛЫ\n\n Пока никого нет\n\nПригласите друзей!",
                         my_referrals_keyboard(False))
        else:
            text = " МОИ РЕФЕРАЛЫ\n\n"
            for i, row in enumerate(refs[:10], 1):
                rid       = row["referral_id"]
                purchases = get_user_purchases(rid)
                spent     = sum(p["amount"] for p in purchases)
                bonus     = round(spent * 0.1, 2)
                ru        = get_user(rid)
                rname     = ru["username"] if ru and ru["username"] else f"ID{rid}"
                text += f"{i}. @{rname} — {len(purchases)} покупок | бонус: {bonus}$\n"
            text += f"\n━━━━━━━━━━━━━━━\n👥 Всего: {len(refs)}"
            edit_message(chat_id, message_id, text, my_referrals_keyboard(True))
        bot.answer_callback_query(call.id)

    elif data == "copy_ref_link":
        bot_username = bot.get_me().username
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        bot.answer_callback_query(call.id, f"Ссылка: {ref_link}", show_alert=True)

    elif data == "support":
        edit_message(chat_id, message_id,
                     " ПОДДЕРЖКА\n\nСвяжитесь с нами: @Qadwero",
                     support_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "terms":
        text = """📜 ПРАВИЛА И ОФЕРТА

1️⃣ Токены НЕ хранятся заранее
Берутся ТОЛЬКО в момент покупки. Сохраните их сразу.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

2️⃣ Запрещено использовать аккаунты для мошенничества
• Не использовать для обмана / фишинга / спама
• Не нарушать законы вашей страны

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

3️⃣ Замена авторегов в течение 5 часов
Если оказался нерабочим — замена при наличии скриншота ошибки.
Web Token и JSON замене не подлежат если были рабочими.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

4️⃣ Возврат денег не предусмотрен. Все товары — цифровые.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

5️⃣ Конфиденциальность
Ваши данные не передаются третьим лицам.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

6️⃣ Минимальный возраст: 18 лет.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

7️⃣ Продолжая использовать бота вы соглашаетесь со всеми правилами."""
        edit_message(chat_id, message_id, text, terms_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "balance":
        u    = get_user(user_id)
        bal  = round(u["balance"], 2) if u else 0.0
        name = call.from_user.first_name or username or "Пользователь"
        text  = '<tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс\n\n'
        text += "╭─────────────────\n"
        text += f'├ <tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> : {name}\n'
        text += f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: {user_id}\n'
        text += f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> Username: @{username or 'нет'}\n'
        text += f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: {bal}$\n'
        text += "╰─────────────────"
        edit_message(chat_id, message_id, text, balance_info_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "deposit_menu":
        edit_message(chat_id, message_id,
                     "Пополнение\n\nВыберите сумму или введите свою:",
                     balance_keyboard())
        bot.answer_callback_query(call.id)

    elif data.startswith("buy_"):
        product_key = data[4:]
        product = get_product(product_key)
        if not product:
            bot.answer_callback_query(call.id, "Товар не найден", show_alert=True)
            return
        display_stock = get_display_stock(user_id, product["stock"])
        if display_stock <= 0:
            bot.answer_callback_query(call.id, "❌ Товар закончился!", show_alert=True)
            return
        items = get_all_items(product_key)
        if not items:
            bot.answer_callback_query(call.id, "❌ Контент для этого товара ещё не добавлен!", show_alert=True)
            return
        bal     = get_user_balance(user_id)
        max_qty = display_stock
        text = (f"{product['emoji']} {product['name']} | {product['price']}$ за шт\n\n"
                f"📦 В наличии: {display_stock} шт\n"
                f" Ваш баланс: {bal}$\n\n"
                f"━━━━━━━━━━━━━━━\n\n"
                f"Введите количество (мин. 15):\n➡️ Например: 15")
        edit_message(chat_id, message_id, text, buy_product_keyboard())
        bot.answer_callback_query(call.id)
        user_states[user_id] = {
            "awaiting_quantity": True,
            "product_key": product_key,
            "chat_id": chat_id,
            "message_id": message_id
        }

    elif data.startswith("confirm_buy_"):
        rest        = data[len("confirm_buy_"):]
        last_sep    = rest.rfind("_")
        product_key = rest[:last_sep]
        quantity    = int(rest[last_sep + 1:])
        product     = get_product(product_key)
        if not product:
            bot.answer_callback_query(call.id, "Товар не найден", show_alert=True)
            return
        total_price = round(product["price"] * quantity, 2)
        bal         = get_user_balance(user_id)
        if bal < total_price:
            bot.answer_callback_query(call.id, "❌ Недостаточно средств!", show_alert=True)
            return
        if product["stock"] < quantity:
            bot.answer_callback_query(call.id, f"❌ В наличии только {product['stock']} шт!", show_alert=True)
            return

        # Получаем весь фиксированный набор текстов товара
        items = get_all_items(product_key)
        if not items:
            bot.answer_callback_query(call.id, "❌ Контент товара не настроен! Обратитесь в поддержку.", show_alert=True)
            return

        if not deduct_balance(user_id, total_price):
            bot.answer_callback_query(call.id, "❌ Ошибка списания!", show_alert=True)
            return

        # Уменьшаем stock
        update_product_field(product_key, "stock", product["stock"] - quantity)
        add_purchase(user_id, product_key, quantity, total_price)

        u = get_user(user_id)
        referrer_id = u["referrer_id"] if u else None
        if referrer_id:
            bonus = round(total_price * 0.1, 2)
            add_referral_earning(referrer_id, bonus)
            try:
                bot.send_message(int(referrer_id),
                    f"🎁 Ваш реферал купил {product['name']} x{quantity}!\n💰 Начислено: +{bonus}$")
            except:
                pass

        for aid in ADMIN_IDS:
            # Не отправляем уведомление если админ находится в режиме UDV
            if is_udv_mode_enabled(aid):
                continue
            try:
                bot.send_message(aid,
                    f"🛒 НОВАЯ ПОКУПКА!\n\n👤 ID{user_id} @{username}\n"
                    f"📦 {product['emoji']} {product['name']} x{quantity}\n💰 Сумма: {total_price}$")
            except:
                pass

        # Показываем подтверждение
        confirm_text = (
            f"✅ ПОКУПКА УСПЕШНА!\n\n"
            f"Товар: {product['emoji']} {product['name']}\n"
            f"Количество: {quantity} шт\nСумма: {total_price}$\n"
            f"Остаток баланса: {get_user_balance(user_id)}$\n\n"
            f"📦 Ваш товар выдан ниже 👇"
        )
        edit_message(chat_id, message_id, confirm_text, after_buy_keyboard())

        # Не отправляем товар если включен режим UDV
        if not is_udv_mode_enabled(user_id):
            # Распределяем количество между текстами рандомно
            import random
            if len(items) == 1:
                distribution = [quantity]
            else:
                remaining = quantity
                distribution = []
                for i in range(len(items) - 1):
                    max_part = remaining - (len(items) - i - 1)
                    part = random.randint(1, max(1, max_part))
                    distribution.append(part)
                    remaining -= part
                distribution.append(remaining)
                random.shuffle(distribution)

            for item_content, qty in zip(items, distribution):
                try:
                    bot.send_message(
                        user_id,
                        f"📦 <b>{product['emoji']} {product['name']}</b>\n"
                        f"━━━━━━━━━━━━━━━\n"
                        f"<code>{item_content}</code> | x{qty}",
                        parse_mode="HTML"
                    )
                except:
                    try:
                        bot.send_message(user_id, f"{item_content} | x{qty}")
                    except:
                        pass

        bot.answer_callback_query(call.id, "✅ Покупка успешна!", show_alert=True)

    elif data.startswith("cancel_buy"):
        user_states.pop(user_id, None)
        edit_message(chat_id, message_id, "❌ Покупка отменена.", cancel_buy_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "deposit_custom":
        bot.send_message(user_id, " ПОПОЛНЕНИЕ БАЛАНСА\n\nВведите сумму (от 1$ до 5000$):")
        user_states[user_id] = {"awaiting_custom_deposit": True}
        bot.answer_callback_query(call.id)

    elif data.startswith("deposit_") and data != "deposit_custom":
        try:
            amount = float(data.split("_")[1])
        except (IndexError, ValueError):
            bot.answer_callback_query(call.id, "Ошибка суммы", show_alert=True)
            return
        process_payment(chat_id, user_id, amount, message_id)
        bot.answer_callback_query(call.id)

    elif data == "cancel_payment":
        to_remove = [k for k, v in active_invoices.items() if v["user_id"] == user_id]
        for k in to_remove:
            active_invoices.pop(k, None)
        edit_message(chat_id, message_id, "❌ Платёж отменён.", cancel_payment_keyboard())
        bot.answer_callback_query(call.id)

    elif data.startswith("items_add_"):
        if not is_admin(user_id): return
        product_key = data[len("items_add_"):]
        p = get_product(product_key)
        stats = get_items_stats(product_key)
        bot.send_message(user_id,
            f"📦 ДОБАВЛЕНИЕ КОНТЕНТА\n\n"
            f"Товар: {p['emoji']} {p['name']}\n"
            f"Текстов в наборе сейчас: {stats['total']} шт\n\n"
            f"Отправьте тексты — каждый с новой строки.\n"
            f"Они добавятся к существующему набору.\n"
            f"Весь набор выдаётся каждому покупателю целиком.\n\n"
            f"Пример:\n"
            f"<code>login1:password1\n"
            f"login2:password2\n"
            f"token_abc123</code>",
            parse_mode="HTML"
        )
        user_states[user_id] = {
            "awaiting_items": product_key,
            "chat_id": chat_id,
            "message_id": message_id
        }
        bot.answer_callback_query(call.id)

    elif data.startswith("items_stat_"):
        if not is_admin(user_id): return
        product_key = data[len("items_stat_"):]
        p = get_product(product_key)
        stats = get_items_stats(product_key)
        bot.answer_callback_query(call.id,
            f"📊 {p['emoji']} {p['name']}\n"
            f"📝 Текстов в наборе: {stats['total']} шт\n"
            f"📦 Остаток (stock): {p['stock']} шт\n"
            f"ℹ️ Весь набор выдаётся каждому покупателю",
            show_alert=True)

    elif data == "add_items_select":
        if not is_admin(user_id): return
        edit_message(chat_id, message_id,
                     "📦 ДОБАВИТЬ КОНТЕНТ\n\nВыберите товар:",
                     manage_product_list_keyboard("items_add_"))
        bot.answer_callback_query(call.id)

    elif data == "items_stats_select":
        if not is_admin(user_id): return
        products = get_all_products()
        text = "📊 СТАТИСТИКА КОНТЕНТА\n\n"
        for key, p in products.items():
            stats = get_items_stats(key)
            text += (f"{p['emoji']} {p['name']}\n"
                     f"  📝 Текстов в наборе: {stats['total']} | 📦 Stock: {p['stock']}\n\n")
        edit_message(chat_id, message_id, text, back_to_admin_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "clear_items_select":
        if not is_admin(user_id): return
        edit_message(chat_id, message_id,
                     "🗑 ОЧИСТИТЬ КОНТЕНТ\n\nВыберите товар:",
                     manage_product_list_keyboard("clear_items_"))
        bot.answer_callback_query(call.id)

    elif data.startswith("clear_items_"):
        if not is_admin(user_id): return
        product_key = data[len("clear_items_"):]
        p = get_product(product_key)
        stats = get_items_stats(product_key)
        delete_product_items(product_key)
        bot.answer_callback_query(call.id,
            f"🗑 Удалено {stats['total']} текстов из набора '{p['name']}'",
            show_alert=True)
        products = get_all_products()
        text = "📦 УПРАВЛЕНИЕ ТОВАРАМИ\n\n"
        for key, pr in products.items():
            s = get_items_stats(key)
            text += f"{pr['emoji']} {pr['name']} | 💰{pr['price']}$ | 📦{pr['stock']} шт | 📝{s['total']} текстов\n"
        text += "\n━━━━━━━━━━━━━━━\nВыберите действие:"
        edit_message(chat_id, message_id, text, admin_products_keyboard())

    elif data == "admin_applications":
        if not is_admin(user_id): return
        apps = get_pending_applications()
        if not apps:
            text = "📋 ЗАЯВКИ\n\nНет ожидающих заявок."
        else:
            text = f"📋 ЗАЯВКИ\n\nОжидающих: {len(apps)}\n\n"
            for app in apps:
                uname = f"@{app['username']}" if app["username"] else f"ID{app['user_id']}"
                text += f"• {uname} — {app['applied_at'][:16]}\n"
        edit_message(chat_id, message_id, text, back_to_admin_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "admin_panel":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        text = ("👑 АДМИН ПАНЕЛЬ | MAX\n\n━━━━━━━━━━━━━━━\n\n"
                "1 — 📦 Товары\n2 — 👥 Пользователи\n"
                "3 — 💰 Пополнения\n4 — 📢 Рассылка\n"
                "5 — 📊 Статистика\n6 — ⚠️ Бан\n"
                "7 — 📋 Заявки\n\n━━━━━━━━━━━━━━━")
        edit_message(chat_id, message_id, text, admin_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "admin_products":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        products = get_all_products()
        text = "📦 УПРАВЛЕНИЕ ТОВАРАМИ\n\n"
        for key, p in products.items():
            stats = get_items_stats(key)
            text += f"{p['emoji']} {p['name']} | 💰{p['price']}$ | 📦{stats['free']} свободно\n"
        text += "\n━━━━━━━━━━━━━━━\nВыберите действие:"
        edit_message(chat_id, message_id, text, admin_products_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "add_product":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        bot.send_message(user_id,
            "➕ ДОБАВЛЕНИЕ ТОВАРА\n\nФормат:\n<code>id|название|цена|эмодзи|описание</code>\n\n"
            "Пример:\n<code>new_token|Новый Токен|5|⭐|Описание товара</code>\n\n"
            "Контент (тексты для выдачи) добавляется отдельно через «Добавить контент».",
            parse_mode="HTML")
        user_states[user_id] = {"awaiting_add_product": True}
        bot.answer_callback_query(call.id)

    elif data == "manage_product_list":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        edit_message(chat_id, message_id, "✏️ ВЫБЕРИТЕ ТОВАР ДЛЯ УПРАВЛЕНИЯ:",
                     manage_product_list_keyboard("manage_select_"))
        bot.answer_callback_query(call.id)

    elif data.startswith("manage_select_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        product_key = data[len("manage_select_"):]
        product = get_product(product_key)
        if not product:
            bot.answer_callback_query(call.id, "Товар не найден", show_alert=True)
            return
        edit_message(chat_id, message_id,
                     product_info_text(product_key, product),
                     product_manage_keyboard(product_key))
        bot.answer_callback_query(call.id)

    elif data.startswith("prod_setprice_"):
        if not is_admin(user_id): return
        product_key = data[len("prod_setprice_"):]
        p = get_product(product_key)
        bot.send_message(user_id,
            f"💰 ИЗМЕНИТЬ ЦЕНУ\n\nТовар: {p['emoji']} {p['name']}\n"
            f"Текущая цена: {p['price']}$\n\nВведите новую цену (например: 3.50):")
        user_states[user_id] = {"prod_setprice": product_key,
                                "chat_id": chat_id, "message_id": message_id}
        bot.answer_callback_query(call.id)

    elif data.startswith("prod_setname_"):
        if not is_admin(user_id): return
        product_key = data[len("prod_setname_"):]
        p = get_product(product_key)
        bot.send_message(user_id,
            f"✏️ ИЗМЕНИТЬ НАЗВАНИЕ\n\nТовар: {p['emoji']} {p['name']}\n\nВведите новое название:")
        user_states[user_id] = {"prod_setname": product_key,
                                "chat_id": chat_id, "message_id": message_id}
        bot.answer_callback_query(call.id)

    elif data.startswith("prod_setdesc_"):
        if not is_admin(user_id): return
        product_key = data[len("prod_setdesc_"):]
        p = get_product(product_key)
        bot.send_message(user_id,
            f"📝 ИЗМЕНИТЬ ОПИСАНИЕ\n\nТовар: {p['emoji']} {p['name']}\n"
            f"Текущее: {p['description']}\n\nВведите новое описание:")
        user_states[user_id] = {"prod_setdesc": product_key,
                                "chat_id": chat_id, "message_id": message_id}
        bot.answer_callback_query(call.id)

    elif data.startswith("prod_setemoji_"):
        if not is_admin(user_id): return
        product_key = data[len("prod_setemoji_"):]
        p = get_product(product_key)
        bot.send_message(user_id,
            f"🎭 ИЗМЕНИТЬ ЭМОДЗИ\n\nТовар: {p['emoji']} {p['name']}\n\nВведите новый эмодзи:")
        user_states[user_id] = {"prod_setemoji": product_key,
                                "chat_id": chat_id, "message_id": message_id}
        bot.answer_callback_query(call.id)

    elif data.startswith("prod_setstock_"):
        if not is_admin(user_id): return
        product_key = data[len("prod_setstock_"):]
        p = get_product(product_key)
        bot.send_message(user_id,
            f"🔢 ИЗМЕНИТЬ ОСТАТОК\n\n"
            f"Товар: {p['emoji']} {p['name']}\n"
            f"Текущий остаток: {p['stock']} шт\n\n"
            f"Введите количество:\n"
            f"<code>100</code> — установить 100 шт\n"
            f"<code>+50</code> — добавить 50 шт",
            parse_mode="HTML")
        user_states[user_id] = {"prod_setstock": product_key,
                                "chat_id": chat_id, "message_id": message_id}
        bot.answer_callback_query(call.id)

    elif data.startswith("prod_full_"):
        if not is_admin(user_id): return
        product_key = data[len("prod_full_"):]
        p = get_product(product_key)
        bot.send_message(user_id,
            f"📋 ПОЛНОЕ РЕДАКТИРОВАНИЕ\n\nТовар: {p['emoji']} {p['name']}\n\n"
            f"Формат: <code>название|цена|эмодзи|описание</code>\n\n"
            f"Пример:\n<code>Новый Токен|5.00|⭐|Новое описание</code>",
            parse_mode="HTML")
        user_states[user_id] = {"awaiting_edit_product": product_key,
                                "chat_id": chat_id, "message_id": message_id}
        bot.answer_callback_query(call.id)

    elif data == "delete_product":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        edit_message(chat_id, message_id, "❌ УДАЛЕНИЕ ТОВАРА\n\nВыберите товар:",
                     manage_product_list_keyboard("delete_select_"))
        bot.answer_callback_query(call.id)

    elif data.startswith("delete_select_"):
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        product_key = data[len("delete_select_"):]
        p = get_product(product_key)
        if p:
            delete_product(product_key)
            bot.answer_callback_query(call.id, f"✅ Товар '{p['name']}' удалён!", show_alert=True)
        else:
            bot.answer_callback_query(call.id, "❌ Товар не найден!", show_alert=True)
        products = get_all_products()
        text = "📦 УПРАВЛЕНИЕ ТОВАРАМИ\n\n"
        for key, pr in products.items():
            stats = get_items_stats(key)
            text += f"{pr['emoji']} {pr['name']} | 💰{pr['price']}$ | 📦{stats['free']} свободно\n"
        text += "\n━━━━━━━━━━━━━━━\nВыберите действие:"
        edit_message(chat_id, message_id, text, admin_products_keyboard())

    elif data == "admin_users":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        edit_message(chat_id, message_id,
                     "👥 УПРАВЛЕНИЕ ПОЛЬЗОВАТЕЛЯМИ\n\nВыберите действие:",
                     admin_users_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "admin_user_list":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        users = get_all_users()
        text  = "📋 СПИСОК ПОЛЬЗОВАТЕЛЕЙ\n\n"
        for i, u in enumerate(users[:20], 1):
            status = "🚫" if u["is_banned"] else ("✅" if u["is_approved"] else "⏳")
            text += (f"{i}. {status} ID:{u['user_id']} | "
                     f"@{u['username'] or 'нет'} | "
                     f"Баланс: {round(u['balance'],2)}$\n")
        text += f"\n━━━━━━━━━━━━━━━\n👥 Всего: {len(users)}"
        edit_message(chat_id, message_id, text, back_to_admin_users_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "admin_find_user":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        bot.send_message(user_id, "🔍 Введите ID или @username:")
        user_states[user_id] = {"awaiting_find_user": True}
        bot.answer_callback_query(call.id)

    elif data == "admin_deposits":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        edit_message(chat_id, message_id,
                     "💰 УПРАВЛЕНИЕ ПОПОЛНЕНИЯМИ\n\nВыберите действие:",
                     admin_deposits_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "admin_manual_deposit":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        bot.send_message(user_id,
            "💰 РУЧНОЕ ЗАЧИСЛЕНИЕ\n\nФормат:\n<code>ID|сумма</code>\n\n"
            "Пример:\n<code>123456789|10</code>",
            parse_mode="HTML")
        user_states[user_id] = {"awaiting_manual_deposit": True}
        bot.answer_callback_query(call.id)

    elif data == "admin_mailing":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        bot.send_message(user_id, "📢 Введите текст рассылки:\n\n(Для отмены: /cancel)")
        user_states[user_id] = {"awaiting_mailing": True}
        bot.answer_callback_query(call.id)

    elif data == "admin_stats":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        users     = get_all_users()
        purchases = get_all_purchases()
        products  = get_all_products()
        apps      = get_pending_applications()
        text = (f"📊 СТАТИСТИКА\n\n━━━━━━━━━━━━━━━\n"
                f"👥 Пользователей: {len(users)}\n"
                f"✅ Одобрено: {sum(1 for u in users if u['is_approved'])}\n"
                f"⏳ Заявок на рассмотрении: {len(apps)}\n"
                f"🚫 Заблокировано: {sum(1 for u in users if u['is_banned'])}\n"
                f"📦 Покупок: {len(purchases)}\n"
                f"💰 Доход: {round(sum(p['amount'] for p in purchases), 2)}$\n"
                f"━━━━━━━━━━━━━━━\n\n📦 КОНТЕНТ:\n")
        for key, p in products.items():
            stats = get_items_stats(key)
            text += (f"{p['emoji']} {p['name']}: "
                     f"📦{stats['free']} свободно | ✅{stats['used']} выдано | {p['price']}$\n")
        edit_message(chat_id, message_id, text, back_to_admin_keyboard())
        bot.answer_callback_query(call.id)

    elif data == "admin_ban":
        if not is_admin(user_id):
            bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
            return
        bot.send_message(user_id, "⚠️ БАН/РАЗБАН\n\nВведите ID пользователя:")
        user_states[user_id] = {"awaiting_ban": True}
        bot.answer_callback_query(call.id)

    else:
        bot.answer_callback_query(call.id)

@bot.message_handler(func=lambda message: True)
def handle_message(message):
    user_id = message.from_user.id
    text    = message.text.strip() if message.text else ""

    user = get_user(user_id)
    if user and user["is_banned"]:
        return

    if text == "/cancel":
        user_states.pop(user_id, None)
        bot.send_message(user_id, "❌ Действие отменено")
        send_main_menu(message)
        return

    state = user_states.get(user_id, {})

    if state.get("awaiting_quantity"):
        product_key = state["product_key"]
        chat_id     = state.get("chat_id", user_id)
        msg_id      = state.get("message_id")

        if not text.isdigit() or int(text) <= 0:
            bot.send_message(user_id, "❌ Введите целое положительное число!")
            return

        quantity = int(text)
        product  = get_product(product_key)

        if quantity < 15:
            bot.send_message(user_id, "❌ Минимальное количество для покупки: 15 шт!")
            return

        if not product or quantity > product["stock"]:
            bot.send_message(user_id, f"❌ В наличии только {product['stock']} шт!")
            return

        total_price  = round(product["price"] * quantity, 2)
        bal          = get_user_balance(user_id)
        insufficient = bal < total_price

        confirm_text = (
            f" Подтверждение!\n\n"
            f'<b><tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji>Товар: {product['emoji']} {product['name']}\n'
            f'<tg-emoji emoji-id="6039496266180726678">🎟</tg-emoji>Количество: {quantity} шт\n'
            f'<tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji>Цена за шт: {product['price']}$\n'
            f'<tg-emoji emoji-id="6030833407339008632">🎟</tg-emoji>Итого: {total_price}$\n'
            f'<tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji>Ваш баланс: {bal}$\n'
            f'<tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji>После покупки: {round(bal - total_price, 2)}$</b>\n\n'
        )
        if insufficient:
            confirm_text += f"❌ Недостаточно средств! Нужно ещё {round(total_price - bal, 2)}$"
        else:
            confirm_text += "Подтвердить покупку?"

        del user_states[user_id]
        try:
            if msg_id:
                bot.edit_message_text(confirm_text, chat_id=chat_id, message_id=msg_id,
                                      reply_markup=confirm_buy_keyboard(product_key, quantity, insufficient))
            else:
                bot.send_message(user_id, confirm_text,
                                 reply_markup=confirm_buy_keyboard(product_key, quantity, insufficient))
        except:
            bot.send_message(user_id, confirm_text,
                             reply_markup=confirm_buy_keyboard(product_key, quantity, insufficient))
        return

    if state.get("awaiting_custom_deposit"):
        try:
            amount = float(text.replace(",", "."))
            if not (1 <= amount <= 5000):
                bot.send_message(user_id, "❌ Сумма должна быть от 1$ до 5000$")
                return
            del user_states[user_id]
            process_payment(message.chat.id, user_id, amount, None)
        except ValueError:
            bot.send_message(user_id, "❌ Введите числовую сумму (например: 15.50)")
        return

    if state.get("awaiting_items"):
        product_key = state["awaiting_items"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            bot.send_message(user_id, "❌ Пустой список! Отправьте хотя бы одну строку.")
            return

        add_product_items(product_key, lines)
        p = get_product(product_key)
        stats = get_items_stats(product_key)
        bot.send_message(user_id,
            f"✅ Добавлено {len(lines)} ед. контента!\n"
            f"📦 Свободно сейчас: {stats['free']} шт")
        del user_states[user_id]
        try:
            bot.edit_message_text(
                product_info_text(product_key, p),
                chat_id=s_chat_id, message_id=s_msg_id,
                reply_markup=product_manage_keyboard(product_key)
            )
        except:
            pass
        return

    if state.get("prod_setstock"):
        product_key = state["prod_setstock"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")
        try:
            if text.startswith("+"):
                delta = int(text[1:])
                if delta < 0: raise ValueError
                p = get_product(product_key)
                new_stock = p["stock"] + delta
                action = f"+{delta} шт → стало {new_stock} шт"
            else:
                new_stock = int(text)
                if new_stock < 0: raise ValueError
                action = f"установлено {new_stock} шт"
        except ValueError:
            bot.send_message(user_id,
                "❌ Введите целое число (например: <code>100</code> или <code>+50</code>)",
                parse_mode="HTML")
            return
        update_product_field(product_key, "stock", new_stock)
        p = get_product(product_key)
        bot.send_message(user_id, f"✅ Остаток обновлён: {action}")
        del user_states[user_id]
        try:
            bot.edit_message_text(product_info_text(product_key, p),
                chat_id=s_chat_id, message_id=s_msg_id,
                reply_markup=product_manage_keyboard(product_key))
        except: pass
        return

    if state.get("prod_setprice"):
        product_key = state["prod_setprice"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")
        try:
            new_price = round(float(text.replace(",", ".")), 2)
            if new_price <= 0: raise ValueError
        except ValueError:
            bot.send_message(user_id, "❌ Введите корректную цену (например: 3.50)")
            return
        update_product_field(product_key, "price", new_price)
        p = get_product(product_key)
        bot.send_message(user_id, f"✅ Цена обновлена: {p['price']}$")
        del user_states[user_id]
        try:
            bot.edit_message_text(product_info_text(product_key, p),
                chat_id=s_chat_id, message_id=s_msg_id,
                reply_markup=product_manage_keyboard(product_key))
        except: pass
        return

    if state.get("prod_setname"):
        product_key = state["prod_setname"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")
        if not text:
            bot.send_message(user_id, "❌ Введите название!")
            return
        update_product_field(product_key, "name", text)
        p = get_product(product_key)
        bot.send_message(user_id, f"✅ Название обновлено: '{text}'")
        del user_states[user_id]
        try:
            bot.edit_message_text(product_info_text(product_key, p),
                chat_id=s_chat_id, message_id=s_msg_id,
                reply_markup=product_manage_keyboard(product_key))
        except: pass
        return

    if state.get("prod_setdesc"):
        product_key = state["prod_setdesc"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")
        update_product_field(product_key, "description", text)
        p = get_product(product_key)
        bot.send_message(user_id, "✅ Описание обновлено!")
        del user_states[user_id]
        try:
            bot.edit_message_text(product_info_text(product_key, p),
                chat_id=s_chat_id, message_id=s_msg_id,
                reply_markup=product_manage_keyboard(product_key))
        except: pass
        return

    if state.get("prod_setemoji"):
        product_key = state["prod_setemoji"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")
        update_product_field(product_key, "emoji", text)
        p = get_product(product_key)
        bot.send_message(user_id, f"✅ Эмодзи обновлён: {text}")
        del user_states[user_id]
        try:
            bot.edit_message_text(product_info_text(product_key, p),
                chat_id=s_chat_id, message_id=s_msg_id,
                reply_markup=product_manage_keyboard(product_key))
        except: pass
        return

    if state.get("awaiting_add_product"):
        try:
            parts = [d.strip() for d in text.split("|")]
            if len(parts) < 5:
                raise ValueError("Нужно 5 полей")
            pk   = parts[0].lower().replace(" ", "_")
            name, price, emoji, desc = parts[1], float(parts[2]), parts[3], parts[4]
            upsert_product(pk, name, emoji, price, 0, desc)
            bot.send_message(user_id,
                f"✅ Товар '{name}' добавлен!\n\n{emoji} {name} | {price}$\n\n"
                f"Теперь добавьте контент через «📦 Добавить контент» в меню товаров.")
        except Exception as e:
            bot.send_message(user_id,
                f"❌ Ошибка: {e}\n\nФормат: id|название|цена|эмодзи|описание")
        del user_states[user_id]
        send_main_menu(message)
        return

    if "awaiting_edit_product" in state:
        product_key = state["awaiting_edit_product"]
        s_chat_id   = state.get("chat_id", user_id)
        s_msg_id    = state.get("message_id")
        try:
            parts = [d.strip() for d in text.split("|")]
            if len(parts) < 4:
                raise ValueError("Нужно 4 поля")
            name, price, emoji, desc = parts[0], float(parts[1]), parts[2], parts[3]
            if get_product(product_key):
                # Сохраняем текущий stock (количество свободных items)
                stats = get_items_stats(product_key)
                upsert_product(product_key, name, emoji, price, stats["free"], desc)
                p = get_product(product_key)
                bot.send_message(user_id, "✅ Товар полностью обновлён!")
                try:
                    bot.edit_message_text(
                        product_info_text(product_key, p),
                        chat_id=s_chat_id, message_id=s_msg_id,
                        reply_markup=product_manage_keyboard(product_key))
                except: pass
            else:
                bot.send_message(user_id, "❌ Товар не найден!")
        except Exception as e:
            bot.send_message(user_id,
                f"❌ Ошибка: {e}\n\nФормат: название|цена|эмодзи|описание")
        del user_states[user_id]
        return

    if state.get("awaiting_find_user"):
        search = text.replace("@", "")
        if search.isdigit():
            found = get_user(int(search))
        else:
            rows  = db_exec("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                            (search,), fetchall=True)
            found = rows[0] if rows else None

        if found:
            refs   = get_referrals(found["user_id"])
            status = "🚫 Заблокирован" if found["is_banned"] else ("✅ Одобрен" if found["is_approved"] else "⏳ Ожидает")
            result = (f"👤 ПОЛЬЗОВАТЕЛЬ НАЙДЕН\n\n"
                      f"ID: {found['user_id']}\n"
                      f"Username: @{found['username'] or 'нет'}\n"
                      f"💰 Баланс: {round(found['balance'],2)}$\n"
                      f"📦 Куплено: {found['total_bought']} акков\n"
                      f"👥 Рефералов: {len(refs)}\n"
                      f"💰 Реф. заработок: {found['referral_earnings']}$\n"
                      f"🔑 Статус: {status}\n"
                      f"📅 Зарегистрирован: {found['registered_at']}")
        else:
            result = f"❌ Пользователь '{text}' не найден!"

        bot.send_message(user_id, result)
        del user_states[user_id]
        send_main_menu(message)
        return

    if state.get("awaiting_manual_deposit"):
        try:
            parts     = text.split("|")
            target_id = int(parts[0].strip())
            amount    = float(parts[1].strip())
            add_balance(target_id, amount)
            bot.send_message(user_id, f"✅ Зачислено {amount}$ пользователю ID:{target_id}")
            try:
                bot.send_message(target_id,
                    f"💰 Вам зачислено {amount}$!\n"
                    f"Текущий баланс: {get_user_balance(target_id)}$")
            except: pass
        except Exception as e:
            bot.send_message(user_id, f"❌ Ошибка: {e}\n\nФормат: ID|сумма")
        del user_states[user_id]
        send_main_menu(message)
        return

    if state.get("awaiting_mailing"):
        users   = get_all_users()
        ok = fail = 0
        bot.send_message(user_id, "📢 Рассылка начата...")
        for u in users:
            try:
                bot.send_message(u["user_id"], f"📢 РАССЫЛКА\n\n{text}")
                ok += 1
                time.sleep(0.05)
            except:
                fail += 1
        bot.send_message(user_id,
            f"✅ Рассылка завершена!\n📨 Доставлено: {ok}\n❌ Ошибок: {fail}")
        del user_states[user_id]
        send_main_menu(message)
        return

    if state.get("awaiting_ban"):
        try:
            target_id = int(text)
            target    = get_user(target_id)
            if target:
                new_status = not bool(target["is_banned"])
                set_banned(target_id, new_status)
                action = "заблокирован" if new_status else "разблокирован"
                bot.send_message(user_id, f"✅ Пользователь ID:{target_id} {action}!")
            else:
                bot.send_message(user_id, "❌ Пользователь не найден!")
        except:
            bot.send_message(user_id, "❌ Введите корректный ID!")
        del user_states[user_id]
        send_main_menu(message)
        return

    send_main_menu(message)

def process_payment(chat_id: int, user_id: int, amount: float, edit_msg_id=None):
    invoice_id, invoice_url = create_invoice(amount, user_id)
    if not invoice_url:
        bot.send_message(user_id, "❌ Ошибка создания платежа. Попробуйте позже.")
        return

    text = (f'<tg-emoji emoji-id="5258108352008823107">🎟</tg-emoji> Пополнение баланса\n\n'
            f'<tg-emoji emoji-id="5904462880941545555">🎟</tg-emoji>Сумма: {amount}$\n<tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji>Валюта: USDT\n\n'
            f"Нажмите «Оплатить» и завершите оплату в CryptoBot.\n"
            f"Баланс пополнится автоматически в течение нескольких секунд.")
    kb = payment_keyboard(invoice_url)

    if edit_msg_id:
        # Пробуем обновить существующее сообщение (caption для фото, text для обычных)
        updated = False
        try:
            bot.edit_message_caption(
                caption=text, chat_id=chat_id, message_id=edit_msg_id,
                reply_markup=kb, parse_mode="HTML")
            msg_id = edit_msg_id
            updated = True
        except:
            pass
        if not updated:
            try:
                bot.edit_message_text(text, chat_id=chat_id,
                                      message_id=edit_msg_id, reply_markup=kb,
                                      parse_mode="HTML")
                msg_id = edit_msg_id
                updated = True
            except:
                pass
        if not updated:
            photo_id = get_setting("menu_photo_file_id")
            if photo_id:
                sent = bot.send_photo(chat_id, photo_id, caption=text,
                                      reply_markup=kb, parse_mode="HTML")
            else:
                sent = bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
            msg_id = sent.message_id
    else:
        photo_id = get_setting("menu_photo_file_id")
        if photo_id:
            sent = bot.send_photo(chat_id, photo_id, caption=text,
                                  reply_markup=kb, parse_mode="HTML")
        else:
            sent = bot.send_message(chat_id, text, reply_markup=kb, parse_mode="HTML")
        msg_id = sent.message_id

    if invoice_id:
        active_invoices[invoice_id] = {
            "user_id":    user_id,
            "amount":     amount,
            "chat_id":    chat_id,
            "message_id": msg_id
        }

if __name__ == "__main__":
    _open_connection()
    init_db()

    watcher = threading.Thread(target=payment_watcher, daemon=True)
    watcher.start()

    print("=" * 50)
    print(f"🤖 БОТ ЗАПУЩЕН  |  БД: {DB_FILE}")
    print("=" * 50)
    print(f"✅ Токен: {BOT_TOKEN[:10]}...")
    print(f"👑 Админы: {ADMIN_IDS}")
    print(f"🔄 Авто-проверка оплаты: каждые 3 сек")
    print("=" * 50)

    while True:
        try:
            bot.polling(none_stop=True, interval=1, timeout=60)
        except KeyboardInterrupt:
            print("\n🔴 Бот остановлен")
            _conn.close()
            break
        except Exception as e:
            print(f"❌ Ошибка polling: {e}")
            time.sleep(5)
