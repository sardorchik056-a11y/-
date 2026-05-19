import telebot
import requests
import threading
import time
import datetime
import re
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto

BOT_TOKEN       = "8628524678:AAH6AuW7KdTF-_-OiVfVH5i_LJH5NLSDg1I"
CRYPTOBOT_TOKEN = "583673:AAwGj7YtqTJZuSomTia1W08YRNo1udgrQiL"
CRYPTOBOT_API   = "https://pay.crypt.bot/api"

# ══════════════════════════════════════════════════════
#  👑  СПИСОК АДМИНИСТРАТОРОВ
# ══════════════════════════════════════════════════════
ADMIN_IDS = [8118184388, 8521752725]

PAYOUT_AMOUNT = 5.0
QUEUE_ENABLED = True

EMOJI_RULES   = "5258185631355378853"
EMOJI_BALANCE = "5258204546391351475"
EMOJI_SUBMIT  = "5449407131675558756"
EMOJI_HISTORY = "6030776052345737530"
EMOJI_STATS   = "5258330865674494479"
EMOJI_BACK    = "6039539366177541657"
EMOJI_ADMIN   = "5258185631355378853"
EMOJI_CHECK   = "5282843764451195532"
EMOJI_QUEUE   = "5323442290708985472"
EMOJI_WISS    = "5258043150110301407"

BANNER_FILE_ID = "AgACAgIAAxkBAAMSagsnVdxEcPVJgVA5Q83bPAi3iycAAiEeaxsEqVlIe-1MMXEG-AEBAAMCAAN5AAM7BA"

# ══════════════════════════════════════════════════════
#  Хранилища данных
# ══════════════════════════════════════════════════════
users_db           = {}   # user_id → dict
queue              = []   # очередь: список user_id
pending            = {}   # user_id → user_msg_id  (заявка на проверке)
pending_admin_msgs = {}   # user_id → [(admin_chat_id, admin_msg_id), ...]
withdraw_requests  = {}   # req_id  → dict
withdraw_counter   = [0]

# Режим очереди: "qr" или "kod"
queue_mode = {"value": "qr"}   # по умолчанию QR

# pending_kod_request[user_id] = {"phone": ..., "admin_msgs": [...]}  — ждём ввода кода
pending_kod_request = {}
# pending_kod_input[user_id] = True — пользователь вводит 6-значный код
pending_kod_input   = set()

# ── Ворк-сессия ──
work_session = {
    "active":       False,          # True = ворк включён
    "start_time":   None,           # datetime когда включили
    "entries":      [],             # список dict {user_id, phone, format, ts, result}
    # result: None | "stood" | "not_stood"
}

settings = {
    "payout": PAYOUT_AMOUNT,
    "rules": (
        '<b><tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> Правила сервиса:</b>\n\n'
        "├ <b>1.</b> Номер должен быть зарегистрирован на вас\n"
        "├ <b>2.</b> Номер не должен быть заблокирован\n"
        "├ <b>3.</b> QR-код должен быть чётким и читаемым\n"
        "├ <b>4.</b> Одна заявка в день с одного аккаунта\n"
        "├ <b>5.</b> При нарушении — бан без предупреждения\n"
        "╰ <b>6.</b> Выплата производится после проверки"
    ),
}

user_states       = {}    # user_id → state string or dict
waiting_for_photo = set()
waiting_for_qr    = set()
admin_states      = {}

# pending_scan_confirm[user_id] = True когда ждём "отсканировал" или "отмена"
pending_scan_confirm = {}

bot = telebot.TeleBot(BOT_TOKEN)

# ══════════════════════════════════════════════════════
#  Вспомогательные функции
# ══════════════════════════════════════════════════════
def get_user(user_id):
    if user_id not in users_db:
        users_db[user_id] = {
            "balance":        0.0,
            "numbers_rented": 0,
            "history":        [],
            "banned":         False,
            "username":       "",
            "first_name":     "",
        }
    return users_db[user_id]

def get_status(user):
    return "Активен" if user["numbers_rented"] >= 1 else "Неактивен"

def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def is_admin(user_id):
    return user_id in ADMIN_IDS

def em(eid, fallback="⭐"):
    return f'<tg-emoji emoji-id="{eid}">{fallback}</tg-emoji>'

def notify_all_admins(text="", markup=None, photo=None, caption=None):
    sent = []
    for admin_id in ADMIN_IDS:
        try:
            if photo:
                m = bot.send_photo(admin_id, photo, caption=caption,
                                   parse_mode="HTML", reply_markup=markup)
            else:
                m = bot.send_message(admin_id, text,
                                     parse_mode="HTML", reply_markup=markup)
            sent.append((admin_id, m.message_id))
        except Exception as e:
            print(f"[notify_admin {admin_id}] {e}")
    return sent

def notify_all_users(text):
    """Разослать уведомление всем пользователям."""
    count = 0
    for u_id in list(users_db.keys()):
        try:
            bot.send_message(u_id, text, parse_mode="HTML")
            count += 1
        except Exception:
            pass
    return count

def is_valid_phone(phone: str) -> bool:
    """Проверка формата: 11 цифр, начинается с 7 или 8."""
    cleaned = re.sub(r"[\s\-\(\)\+]", "", phone)
    return bool(re.match(r"^[78]\d{10}$", cleaned))

def format_phone(phone: str) -> str:
    """Нормализовать номер — убрать всё кроме цифр, заменить 8 на 7."""
    cleaned = re.sub(r"[\s\-\(\)\+]", "", phone)
    if cleaned.startswith("8"):
        cleaned = "7" + cleaned[1:]
    return cleaned

def fmt_phone_display(phone: str) -> str:
    """Красивый вид номера: +7XXXXXXXXXX."""
    p = format_phone(phone)
    return f"+{p}" if p else phone

# ══════════════════════════════════════════════════════
#  CryptoBot
# ══════════════════════════════════════════════════════
def cryptobot_create_check(amount: float, currency: str = "USDT") -> dict | None:
    try:
        resp = requests.post(
            f"{CRYPTOBOT_API}/createCheck",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN},
            json={"asset": currency, "amount": str(amount)},
            timeout=10,
        )
        data = resp.json()
        if data.get("ok"):
            return data["result"]
        return None
    except Exception as e:
        print(f"CryptoBot error: {e}")
        return None

# ══════════════════════════════════════════════════════
#  Фоновый поток — автообновление позиции каждые 5 мин
# ══════════════════════════════════════════════════════
def _queue_updater():
    while True:
        time.sleep(300)
        snapshot = list(queue)
        total    = len(snapshot)
        for i, user_id in enumerate(snapshot):
            pos = i + 1
            if pos == 1:
                continue
            try:
                bot.send_message(
                    user_id,
                    f"╭─────────────────────\n"
                    f'├ {em(EMOJI_QUEUE,"🔄")} <b>Обновление очереди</b>\n'
                    f"├\n"
                    f"├ Ваша позиция: <b>{pos}</b> из <b>{total}</b>\n"
                    f"╰─────────────────────",
                    parse_mode="HTML",
                )
            except Exception:
                pass

threading.Thread(target=_queue_updater, daemon=True).start()

# ══════════════════════════════════════════════════════
#  Тексты
# ══════════════════════════════════════════════════════
def queue_text(pos):
    total = len(queue)
    return (
        f"╭─────────────────────\n"
        f'├ <b>{em(EMOJI_QUEUE,"⏳")} Вы в очереди</b>\n'
        f"├\n"
        f"├ Ваша позиция: <b>{pos}</b> из <b>{total}</b>\n"
        f"├\n"
        f"├ Позиция обновляется каждые 5 минут\n"
        f"╰─────────────────────"
    )

def welcome_text(tg_user, user):
    name     = esc(tg_user.first_name or "—")
    username = f"@{esc(tg_user.username)}" if tg_user.username else "—"
    work_status = "🟢 Ворк активен" if work_session["active"] else "🔴 Ворк не активен"
    return (
        f"╭─────────────────────\n"
        f'├ <b><tg-emoji emoji-id="5260399854500191689">🎟</tg-emoji> {name}</b>\n'
        f'├ <tg-emoji emoji-id="5282843764451195532">🎟</tg-emoji> ID: <code>{tg_user.id}</code>\n'
        f'├ <tg-emoji emoji-id="5323442290708985472">🎟</tg-emoji> : {username}\n'
        f"├\n"
        f'├ <tg-emoji emoji-id="5258204546391351475">🎟</tg-emoji> Баланс: <b>${user["balance"]:.2f}</b>\n'
        f'├ <tg-emoji emoji-id="5449407131675558756">🎟</tg-emoji> Сдано: <b>{user["numbers_rented"]}</b> номеров\n'
        f'├ <tg-emoji emoji-id="5258185631355378853">🎟</tg-emoji> Статус: {get_status(user)}\n'
        f"├\n"
        f"├ {work_status}\n"
        f"╰─────────────────────"
    )

def rules_text():
    return settings["rules"]

def balance_text(user):
    if user["history"]:
        lines = ""
        for h in reversed(user["history"][-5:]):
            sign  = "+" if h["amount"] > 0 else ""
            lines += f"├ {h['date']} — <b>{sign}${h['amount']:.2f}</b> ({h['status']})\n"
    else:
        lines = "├ История пуста\n"
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_BALANCE,'💰')} Ваш баланс</b>\n"
        f"├\n"
        f'├ Доступно: <b>${user["balance"]:.2f}</b>\n'
        f"├\n"
        f"├ <b>Последние операции:</b>\n"
        f"{lines}"
        f"╰─────────────────────"
    )

def withdraw_text(user):
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_BALANCE,'💸')} Вывод средств</b>\n"
        f"├\n"
        f'├ Доступно: <b>${user["balance"]:.2f}</b>\n'
        f"├\n"
        f"├ Минимальная сумма: <b>$1.00</b>\n"
        f"├ Выплата через: <b>@CryptoBot</b>\n"
        f"├\n"
        f"├ Введите сумму для вывода\n"
        f"╰─────────────────────"
    )

def withdraw_confirm_text(amount: float, user):
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_BALANCE,'💸')} Подтверждение вывода</b>\n"
        f"├\n"
        f'├ Сумма: <b>${amount:.2f}</b>\n'
        f'├ Останется: <b>${user["balance"] - amount:.2f}</b>\n'
        f"├ Способ: <b>@CryptoBot (USDT)</b>\n"
        f"├\n"
        f"├ Подтвердите заявку на вывод\n"
        f"╰─────────────────────"
    )

def withdraw_pending_admin_text(req_id, user_id, amount, first_name, username):
    return (
        f"╭─────────────────────\n"
        f"├ <b>💸 Заявка на вывод #{req_id}</b>\n"
        f"├\n"
        f"├ Имя: {first_name}\n"
        f"├ Username: {username}\n"
        f"├ ID: <code>{user_id}</code>\n"
        f"├ Сумма: <b>${amount:.2f} USDT</b>\n"
        f"╰─────────────────────"
    )

def submit_price_text():
    amt = settings["payout"]
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_SUBMIT,'📦')} Сдать номер</b>\n"
        f"├\n"
        f"├ Выплата за номер: <b>${amt:.2f}</b>\n"
        f"├\n"
        f"├ Введите номер телефона\n"
        f"├ Пример: <code>+79001234567</code>\n"
        f"╰─────────────────────"
    )

def history_text(user):
    if not user["history"]:
        body = "├ История операций пуста\n"
    else:
        body = ""
        for h in reversed(user["history"][-10:]):
            sign  = "+" if h["amount"] > 0 else ""
            body += f"├ {h['date']} {sign}${h['amount']:.2f} — {h['status']}\n"
    return (
        f"╭─────────────────────\n"
        f'├ <b><tg-emoji emoji-id="6030776052345737530">🎟</tg-emoji> История операций</b>\n'
        f"├\n"
        f"{body}"
        f"╰─────────────────────"
    )

def statistics_text():
    return (
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_STATS,'📊')} Статистика</b>\n"
        f"├\n"
        f"├ Пользователей: <b>{len(users_db)}</b>\n"
        f"├ Сдано номеров: <b>{sum(u['numbers_rented'] for u in users_db.values())}</b>\n"
        f"├ Выплачено: <b>${sum(u['balance'] for u in users_db.values()):.2f}</b>\n"
        f"├ В очереди: <b>{len(queue)}</b>\n"
        f"├ На проверке: <b>{len(pending)}</b>\n"
        f"╰─────────────────────"
    )

def admin_top_stats_text():
    medal = {1: "🥇", 2: "🥈", 3: "🥉"}
    by_rented  = sorted(users_db.items(), key=lambda x: x[1]["numbers_rented"], reverse=True)[:20]
    by_balance = sorted(users_db.items(), key=lambda x: x[1]["balance"],        reverse=True)[:20]

    def row(i, uid, u, val):
        m    = medal.get(i, f"{i}.")
        name = esc(u.get("first_name") or str(uid))
        un   = f"@{esc(u['username'])}" if u.get("username") else "—"
        return f"├ {m} {name} ({un}) — <b>{val}</b>\n"

    text  = "╭─────────────────────\n"
    text += "├ 🏆 <b>ТОП-20 по сдаче номеров:</b>\n├\n"
    if by_rented:
        for i, (uid, u) in enumerate(by_rented, 1):
            text += row(i, uid, u, f"{u['numbers_rented']} шт.")
    else:
        text += "├ Нет данных\n"
    text += "├\n├ 💰 <b>ТОП-20 по балансу:</b>\n├\n"
    if by_balance:
        for i, (uid, u) in enumerate(by_balance, 1):
            text += row(i, uid, u, f"${u['balance']:.2f}")
    else:
        text += "├ Нет данных\n"
    text += "╰─────────────────────"
    return text

def work_session_list_text():
    """Список номеров текущей (или последней) ворк-сессии."""
    entries = work_session["entries"]
    if not entries:
        return "╭─────────────────────\n├ 📋 <b>Список номеров пуст</b>\n╰─────────────────────"

    start_str = work_session["start_time"].strftime("%d.%m %H:%M") if work_session["start_time"] else "—"
    text = (
        f"╭─────────────────────\n"
        f"├ 📋 <b>Ворк-сессия с {start_str}</b>\n"
        f"├ Всего номеров: <b>{len(entries)}</b>\n"
        f"├\n"
    )
    for idx, e in enumerate(entries, 1):
        u = users_db.get(e["user_id"], {})
        name = esc(u.get("first_name") or str(e["user_id"]))
        fmt  = "QR" if e["format"] == "qr" else "КОД"
        result_icon = ""
        if e["result"] == "stood":
            result_icon = " ✅"
        elif e["result"] == "not_stood":
            result_icon = " ❌"
        text += f"├ <b>{idx}.</b> {esc(e['phone'])} [{fmt}] — {name}{result_icon}\n"
    text += "╰─────────────────────"
    return text

# ══════════════════════════════════════════════════════
#  Клавиатуры
# ══════════════════════════════════════════════════════
def main_menu():
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("Правила",     callback_data="rules",
                             icon_custom_emoji_id=EMOJI_RULES),
        InlineKeyboardButton("Баланс",      callback_data="balance",
                             icon_custom_emoji_id=EMOJI_BALANCE),
    )
    m.row(InlineKeyboardButton("Сдать номер", callback_data="submit_number",
                               icon_custom_emoji_id=EMOJI_SUBMIT))
    m.row(
        InlineKeyboardButton("История",    callback_data="history",
                             icon_custom_emoji_id=EMOJI_HISTORY),
        InlineKeyboardButton("Статистика", callback_data="statistics",
                             icon_custom_emoji_id=EMOJI_STATS),
    )
    return m

def back_btn(target="back_menu"):
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("Назад", callback_data=target,
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def format_choice_menu():
    """Выбор формата: QR-код или КОД."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("📷 QR-код",  callback_data="fmt_qr"),
        InlineKeyboardButton("🔢 КОД",    callback_data="fmt_kod"),
    )
    m.row(InlineKeyboardButton("Назад", callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def send_qr_btn():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("✅ Отправить заявку", callback_data="send_qr"))
    m.row(InlineKeyboardButton("Изменить QR-код",    callback_data="attach_qr"))
    m.row(InlineKeyboardButton("Назад", callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def pending_menu():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("❌ Отменить заявку", callback_data="cancel_application"))
    m.row(InlineKeyboardButton("Назад", callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def balance_menu():
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("Вывести", callback_data="withdraw",
                               icon_custom_emoji_id=EMOJI_WISS))
    m.row(InlineKeyboardButton("Назад",   callback_data="back_menu",
                               icon_custom_emoji_id=EMOJI_BACK))
    return m

def withdraw_confirm_btn(amount: float):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Подтвердить",
                             callback_data=f"withdraw_confirm_{amount:.2f}"),
        InlineKeyboardButton("❌ Отмена", callback_data="balance"),
    )
    return m

def admin_withdraw_btn(req_id: int):
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Принять",   callback_data=f"wd_take_{req_id}"),
        InlineKeyboardButton("❌ Отклонить", callback_data=f"wd_reject_{req_id}"),
    )
    return m

def admin_review_qr_btn(user_id):
    """Кнопки для QR-заявки: встал/не встал."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Встал",    callback_data=f"stood_{user_id}"),
        InlineKeyboardButton("❌ Не встал", callback_data=f"not_stood_{user_id}"),
    )
    return m

def admin_review_kod_btn(user_id):
    """Кнопки для КОД-заявки: встал/не встал."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Встал",    callback_data=f"stood_{user_id}"),
        InlineKeyboardButton("❌ Не встал", callback_data=f"not_stood_{user_id}"),
    )
    return m

def admin_panel_menu():
    work_btn  = "🔴 СТОП ВОРК" if work_session["active"] else "🟢 СТАРТ ВОРК"
    mode_icon = "📷 QR-код" if queue_mode["value"] == "qr" else "🔢 КОД"
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton(work_btn, callback_data="adm_toggle_work"))
    m.row(
        InlineKeyboardButton(f"⚙️ Очередь: {mode_icon}", callback_data="adm_queue_mode"),
        InlineKeyboardButton("📋 Список номеров",          callback_data="adm_number_list"),
    )
    m.row(InlineKeyboardButton("📋 Список ворк-сессии", callback_data="adm_work_list"))
    m.row(InlineKeyboardButton("📊 Статистика",         callback_data="adm_stats"))
    m.row(InlineKeyboardButton("🏆 Топ пользователей",  callback_data="adm_top_stats"))
    m.row(
        InlineKeyboardButton("🔍 Проверка юзера",  callback_data="adm_check"),
        InlineKeyboardButton("💰 Выдать баланс",   callback_data="adm_give"),
    )
    m.row(
        InlineKeyboardButton("➖ Снять баланс",    callback_data="adm_take"),
        InlineKeyboardButton("🔄 Обнулить всех",   callback_data="adm_reset_all"),
    )
    m.row(InlineKeyboardButton("📢 Рассылка",          callback_data="adm_broadcast"))
    m.row(InlineKeyboardButton("💵 Изменить выплату",  callback_data="adm_payout"))
    return m

def work_list_admin_btn(entry_idx):
    """Кнопки «Отстоял / Не отстоял» для конкретной записи в ворк-списке."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Отстоял",     callback_data=f"ws_stood_{entry_idx}"),
        InlineKeyboardButton("❌ Не отстоял",  callback_data=f"ws_not_{entry_idx}"),
    )
    return m

def admin_qr_notify_btn(user_id):
    """Кнопка для QR-заявки: отправить QR-код пользователю."""
    m = InlineKeyboardMarkup()
    m.row(InlineKeyboardButton("📤 Отправить QR-код", callback_data=f"admin_send_qr_{user_id}"))
    return m

def admin_kod_notify_btn(user_id):
    """Кнопки для КОД-заявки: запросить код / отмена номера."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("🔢 Запросить код",  callback_data=f"admin_req_kod_{user_id}"),
        InlineKeyboardButton("🚫 Отмена номера",  callback_data=f"admin_cancel_num_{user_id}"),
    )
    return m

def admin_kod_result_btn(user_id):
    """Кнопки после ввода кода пользователем: Отстоял / Не отстоял."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Отстоял",    callback_data=f"stood_{user_id}"),
        InlineKeyboardButton("❌ Не отстоял", callback_data=f"not_stood_{user_id}"),
    )
    return m

def scanned_or_cancel_btn():
    """Кнопки для пользователя после отправки QR-заявки."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Отсканировал", callback_data="user_scanned"),
        InlineKeyboardButton("❌ Отмена",        callback_data="cancel_application"),
    )
    return m

def queue_mode_choice_btn():
    """Кнопки выбора режима очереди."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("📷 QR-код", callback_data="adm_set_mode_qr"),
        InlineKeyboardButton("🔢 КОД",   callback_data="adm_set_mode_kod"),
    )
    return m

def number_list_entry_btn(user_id, entry_idx):
    """Кнопка одной записи в списке номеров (номер-@username)."""
    e   = work_session["entries"][entry_idx]
    u   = users_db.get(user_id, {})
    un  = f"@{u['username']}" if u.get("username") else str(user_id)
    lbl = f"{fmt_phone_display(e['phone'])}-{un}"
    return InlineKeyboardButton(lbl, callback_data=f"nl_entry_{entry_idx}")

def number_list_detail_btn(entry_idx):
    """Кнопки внутри записи списка номеров."""
    m = InlineKeyboardMarkup()
    m.row(
        InlineKeyboardButton("✅ Отстоял",    callback_data=f"ws_stood_{entry_idx}"),
        InlineKeyboardButton("❌ Не отстоял", callback_data=f"ws_not_{entry_idx}"),
    )
    m.row(InlineKeyboardButton("◀️ Назад", callback_data="adm_number_list"))
    return m

# ══════════════════════════════════════════════════════
#  Вспомогательная функция: завершение обработки
# ══════════════════════════════════════════════════════
def _finish_qr_review(target_id):
    pending.pop(target_id, None)
    pending_admin_msgs.pop(target_id, None)
    pending_scan_confirm.pop(target_id, None)
    if QUEUE_ENABLED and target_id not in queue:
        queue.append(target_id)

# ══════════════════════════════════════════════════════
#  Обработка вывода средств
# ══════════════════════════════════════════════════════
def _process_withdraw_take(req_id: int, chat_id: int, msg_id: int | None = None):
    req = withdraw_requests.get(req_id)
    if not req:
        bot.send_message(chat_id, f"❌ Заявка #{req_id} не найдена.")
        return
    if req["status"] != "pending":
        bot.send_message(chat_id, f"⚠️ Заявка #{req_id} уже обработана.")
        return

    amount  = req["amount"]
    user_id = req["user_id"]
    check   = cryptobot_create_check(amount)
    if check is None:
        bot.send_message(chat_id, f"❌ Ошибка создания чека CryptoBot для заявки #{req_id}.")
        return

    req["status"]    = "done"
    check_link       = check.get("bot_check_url") or check.get("check_url") or "—"
    req["check_url"] = check_link

    u = users_db.get(user_id)
    if u:
        for h in reversed(u["history"]):
            if h["status"] == "Вывод (ожидание)" and h["amount"] == -amount:
                h["status"] = "Вывод выплачен"
                break

    try:
        bot.send_message(
            user_id,
            f"╭─────────────────────\n"
            f"├ ✅ <b>Вывод одобрен!</b>\n"
            f"├\n"
            f"├ Сумма: <b>${amount:.2f} USDT</b>\n"
            f"├ Чек: <b>@CryptoBot</b>\n"
            f"├\n"
            f"├ Нажмите кнопку ниже для получения\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton("Получить средства", url=check_link)
            ),
        )
    except Exception:
        pass

    ok_text = (
        f"╭─────────────────────\n"
        f"├ ✅ <b>Заявка #{req_id} выплачена!</b>\n"
        f"├\n"
        f"├ 💸 Чек на <b>${amount:.2f} USDT</b>\n"
        f"├ 🔗 {check_link}\n"
        f"╰─────────────────────"
    )
    if msg_id:
        try:
            bot.edit_message_text(ok_text, chat_id, msg_id, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, ok_text, parse_mode="HTML")


def _process_withdraw_reject(req_id: int, chat_id: int, msg_id: int | None = None):
    req = withdraw_requests.get(req_id)
    if not req:
        bot.send_message(chat_id, f"❌ Заявка #{req_id} не найдена.")
        return
    if req["status"] != "pending":
        bot.send_message(chat_id, f"⚠️ Заявка #{req_id} уже обработана.")
        return

    amount  = req["amount"]
    user_id = req["user_id"]
    req["status"] = "rejected"

    u = get_user(user_id)
    u["balance"] += amount
    for h in reversed(u["history"]):
        if h["status"] == "Вывод (ожидание)" and h["amount"] == -amount:
            h["status"] = "Вывод отклонён"
            break

    try:
        bot.send_message(
            user_id,
            f"╭─────────────────────\n"
            f"├ ❌ <b>Вывод отклонён</b>\n"
            f"├\n"
            f"├ Возвращено: <b>${amount:.2f}</b>\n"
            f'├ Ваш баланс: <b>${u["balance"]:.2f}</b>\n'
            f"├\n"
            f"├ Обратитесь в поддержку за деталями\n"
            f"╰─────────────────────",
            parse_mode="HTML",
        )
    except Exception:
        pass

    rej_text = f"╭─────────────────────\n├ ❌ <b>Заявка #{req_id} отклонена.</b>\n╰─────────────────────"
    if msg_id:
        try:
            bot.edit_message_text(rej_text, chat_id, msg_id, parse_mode="HTML")
            return
        except Exception:
            pass
    bot.send_message(chat_id, rej_text, parse_mode="HTML")

# ══════════════════════════════════════════════════════
#  Команды
# ══════════════════════════════════════════════════════
@bot.message_handler(commands=["getfileid"])
def cmd_getfileid(message):
    waiting_for_photo.add(message.from_user.id)
    bot.send_message(message.chat.id, "Отправь фото — верну <b>file_id</b>", parse_mode="HTML")


@bot.message_handler(commands=["on"])
def cmd_on(message):
    """Включить ворк (команда для админа)."""
    if not is_admin(message.from_user.id):
        return
    if work_session["active"]:
        bot.send_message(message.chat.id, "✅ Ворк уже активен.")
        return
    work_session["active"]     = True
    work_session["start_time"] = datetime.datetime.now()
    work_session["entries"]    = []
    bot.send_message(message.chat.id, "🟢 <b>Ворк включён!</b> Пользователи уведомлены.", parse_mode="HTML")
    count = notify_all_users(
        "╭─────────────────────\n"
        "├ 🟢 <b>Ворк начался!</b>\n"
        "├\n"
        "├ Теперь вы можете сдать номер.\n"
        "╰─────────────────────"
    )
    print(f"[/on] уведомлено {count} пользователей")


@bot.message_handler(commands=["off"])
def cmd_off(message):
    """Выключить ворк (команда для админа)."""
    if not is_admin(message.from_user.id):
        return
    if not work_session["active"]:
        bot.send_message(message.chat.id, "🔴 Ворк уже выключен.")
        return
    work_session["active"] = False
    bot.send_message(message.chat.id, "🔴 <b>Ворк выключен!</b> Пользователи уведомлены.", parse_mode="HTML")
    count = notify_all_users(
        "╭─────────────────────\n"
        "├ 🔴 <b>Ворк завершён!</b>\n"
        "├\n"
        "├ Приём номеров остановлен.\n"
        "├ Ожидайте следующего ворка.\n"
        "╰─────────────────────"
    )
    print(f"[/off] уведомлено {count} пользователей")


@bot.message_handler(commands=["take"])
def cmd_take(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        pl = [f"#{r} — ${withdraw_requests[r]['amount']:.2f}"
              for r in withdraw_requests
              if withdraw_requests[r]["status"] == "pending"]
        if not pl:
            bot.send_message(message.chat.id, "📭 Нет ожидающих заявок на вывод.")
        else:
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ⏳ <b>Ожидающие заявки:</b>\n├\n"
                + "\n".join(f"├ {l}" for l in pl)
                + "\n╰─────────────────────\n\nИспользуй: <code>/take [номер]</code>",
                parse_mode="HTML",
            )
        return
    try:
        req_id = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажите числовой номер: <code>/take 4</code>", parse_mode="HTML")
        return
    _process_withdraw_take(req_id, message.chat.id)


@bot.message_handler(commands=["reject"])
def cmd_reject(message):
    if not is_admin(message.from_user.id):
        return
    parts = message.text.strip().split()
    if len(parts) < 2:
        bot.send_message(message.chat.id, "❌ Укажите номер: <code>/reject 4</code>", parse_mode="HTML")
        return
    try:
        req_id = int(parts[1])
    except ValueError:
        bot.send_message(message.chat.id, "❌ Укажите числовой номер.", parse_mode="HTML")
        return
    _process_withdraw_reject(req_id, message.chat.id)


@bot.message_handler(commands=["takeall"])
def cmd_takeall(message):
    if not is_admin(message.from_user.id):
        return
    ids = [r for r in withdraw_requests if withdraw_requests[r]["status"] == "pending"]
    if not ids:
        bot.send_message(message.chat.id, "📭 Нет ожидающих заявок.")
        return
    bot.send_message(message.chat.id, f"⏳ Обрабатываю {len(ids)} заявок...")
    done = failed = 0
    for req_id in ids:
        if cryptobot_create_check(withdraw_requests[req_id]["amount"]):
            _process_withdraw_take(req_id, message.chat.id)
            done += 1
        else:
            failed += 1
    bot.send_message(message.chat.id,
                     f"✅ Принято: <b>{done}</b>  |  ❌ Ошибок: <b>{failed}</b>",
                     parse_mode="HTML")


@bot.message_handler(commands=["rejectall"])
def cmd_rejectall(message):
    if not is_admin(message.from_user.id):
        return
    ids = [r for r in withdraw_requests if withdraw_requests[r]["status"] == "pending"]
    if not ids:
        bot.send_message(message.chat.id, "📭 Нет ожидающих заявок.")
        return
    for req_id in ids:
        _process_withdraw_reject(req_id, message.chat.id)
    bot.send_message(message.chat.id, f"❌ Отклонено: <b>{len(ids)}</b>", parse_mode="HTML")


@bot.message_handler(commands=["admin"])
def cmd_admin(message):
    if not is_admin(message.from_user.id):
        return
    work_status = "🟢 Ворк активен" if work_session["active"] else "🔴 Ворк не активен"
    bot.send_message(
        message.chat.id,
        f"╭─────────────────────\n"
        f"├ <b>{em(EMOJI_ADMIN,'👑')} Панель администратора</b>\n"
        f"├\n"
        f"├ Выплата за номер: <b>${settings['payout']:.2f}</b>\n"
        f"├ Пользователей: <b>{len(users_db)}</b>\n"
        f"├ Администраторов: <b>{len(ADMIN_IDS)}</b>\n"
        f"├ {work_status}\n"
        f"├ Номеров в сессии: <b>{len(work_session['entries'])}</b>\n"
        f"╰─────────────────────",
        parse_mode="HTML",
        reply_markup=admin_panel_menu(),
    )


@bot.message_handler(commands=["start", "menu"])
def start(message):
    uid  = message.from_user.id
    user = get_user(uid)
    user["username"]   = message.from_user.username or ""
    user["first_name"] = message.from_user.first_name or ""
    if user.get("banned"):
        bot.send_message(message.chat.id, "🚫 Вы заблокированы.")
        return
    text = welcome_text(message.from_user, user)
    if BANNER_FILE_ID:
        bot.send_photo(message.chat.id, BANNER_FILE_ID,
                       caption=text, parse_mode="HTML", reply_markup=main_menu())
    else:
        bot.send_message(message.chat.id, text, parse_mode="HTML", reply_markup=main_menu())

# ══════════════════════════════════════════════════════
#  Обработчики медиа
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=["photo"])
def handle_photo(message):
    uid = message.from_user.id

    if uid in waiting_for_photo:
        waiting_for_photo.discard(uid)
        file_id = message.photo[-1].file_id
        bot.send_message(message.chat.id,
                         f"✅ <b>file_id</b>:\n\n<code>{file_id}</code>",
                         parse_mode="HTML")
        return

    # ── Админ отправляет QR-код пользователю ──
    if is_admin(uid) and uid in admin_states:
        state = admin_states[uid]
        if state.get("action") == "send_qr_photo":
            target_id = state["target"]
            del admin_states[uid]
            file_id = message.photo[-1].file_id

            pending_scan_confirm[target_id] = True

            # Отправить QR пользователю
            try:
                bot.send_photo(
                    target_id,
                    file_id,
                    caption=(
                        f"╭─────────────────────\n"
                        f"├ <b>📷 Ваш QR-код</b>\n"
                        f"├\n"
                        f"├ Отсканируйте QR-код и нажмите кнопку ниже.\n"
                        f"╰─────────────────────"
                    ),
                    parse_mode="HTML",
                    reply_markup=scanned_or_cancel_btn(),
                )
                bot.send_message(message.chat.id, f"✅ QR-код отправлен пользователю <code>{target_id}</code>", parse_mode="HTML")
            except Exception as e:
                bot.send_message(message.chat.id, f"❌ Ошибка отправки: {e}")
            return

    if uid in waiting_for_qr:
        waiting_for_qr.discard(uid)
        file_id = message.photo[-1].file_id
        get_user(uid)["_pending_qr"] = file_id
        bot.send_photo(
            message.chat.id,
            file_id,
            caption=(
                f"╭─────────────────────\n"
                f"├ <b>📷 QR-код получен!</b>\n"
                f"├\n"
                f"├ Проверьте фото и нажмите\n"
                f"├ <b>«Отправить заявку»</b>\n"
                f"╰─────────────────────"
            ),
            parse_mode="HTML",
            reply_markup=send_qr_btn(),
        )

# ══════════════════════════════════════════════════════
#  Обработчик текста (состояния пользователя + админа)
# ══════════════════════════════════════════════════════
@bot.message_handler(content_types=["text"])
def handle_text(message):
    uid  = message.from_user.id
    text = message.text.strip()

    # ── Ввод номера телефона ──
    if user_states.get(uid) == "waiting_phone":
        phone = format_phone(text)
        if not is_valid_phone(phone):
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ❌ <b>Неверный формат номера</b>\n"
                "├\n"
                "├ Введите номер в формате:\n"
                "├ <code>+79001234567</code>\n"
                "╰─────────────────────",
                parse_mode="HTML",
            )
            return
        # Сохраняем номер и спрашиваем формат
        get_user(uid)["_pending_phone"] = phone
        user_states[uid] = "waiting_format"
        bot.send_message(
            message.chat.id,
            f"╭─────────────────────\n"
            f"├ 📱 Номер: <code>{esc(phone)}</code>\n"
            f"├\n"
            f"├ Выберите формат сдачи:\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=format_choice_menu(),
        )
        return

    # ── Пользователь вводит 6-значный код (КОД-заявка) ──
    if uid in pending_kod_input:
        # Принимаем любой текст как код (обычно 6 цифр)
        kod = text
        pending_kod_input.discard(uid)

        req = pending_kod_request.pop(uid, None)
        phone = req["phone"] if req else "—"
        name = esc(message.from_user.first_name or "—")
        username = f"@{esc(message.from_user.username)}" if message.from_user.username else "—"

        # Пересылаем код всем админам
        admin_kod_text = (
            f"╭─────────────────────\n"
            f"├ 🔢 <b>Пользователь ввёл код</b>\n"
            f"├\n"
            f"├ Имя: {name}\n"
            f"├ Username: {username}\n"
            f"├ ID: <code>{uid}</code>\n"
            f"├ Номер: <code>{esc(fmt_phone_display(phone))}</code>\n"
            f"├\n"
            f"├ Код: <b><code>{esc(kod)}</code></b>\n"
            f"╰─────────────────────"
        )
        notify_all_admins(text=admin_kod_text, markup=admin_kod_result_btn(uid))

        # Подтверждение пользователю
        bot.send_message(
            message.chat.id,
            "╭─────────────────────\n"
            "├ ✅ <b>Код отправлен!</b>\n"
            "├\n"
            "├ Ожидайте результата от администратора.\n"
            "╰─────────────────────",
            parse_mode="HTML",
        )
        return

    # ── Ввод суммы вывода ──
    if user_states.get(uid) == "waiting_withdraw_amount":
        del user_states[uid]
        raw = text.replace(",", ".")
        try:
            amount = float(raw)
        except ValueError:
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ❌ <b>Некорректная сумма</b>\n"
                "├ Введите число, например: <code>5.00</code>\n"
                "╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn("balance"),
            )
            return
        u = get_user(uid)
        if amount < 1.0:
            bot.send_message(
                message.chat.id,
                "╭─────────────────────\n"
                "├ ❌ <b>Минимальная сумма вывода — $1.00</b>\n"
                "╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn("balance"),
            )
            return
        if amount > u["balance"]:
            bot.send_message(
                message.chat.id,
                f"╭─────────────────────\n"
                f"├ ❌ <b>Недостаточно средств</b>\n"
                f'├ Доступно: <b>${u["balance"]:.2f}</b>\n'
                f"╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn("balance"),
            )
            return
        bot.send_message(
            message.chat.id,
            withdraw_confirm_text(amount, u),
            parse_mode="HTML",
            reply_markup=withdraw_confirm_btn(amount),
        )
        return

    if uid not in admin_states:
        return

    state  = admin_states[uid]
    action = state.get("action")

    # ── Рассылка ──
    if action == "broadcast":
        del admin_states[uid]
        count = 0
        for u_id in list(users_db.keys()):
            try:
                bot.send_message(u_id,
                                 f"<b>Сообщение от администратора:</b>\n\n{text}",
                                 parse_mode="HTML")
                count += 1
            except Exception:
                pass
        bot.send_message(message.chat.id,
                         f"✅ Рассылка отправлена <b>{count}</b> пользователям.",
                         parse_mode="HTML")

    # ── Проверка пользователя ──
    elif action == "check_user":
        del admin_states[uid]
        try:
            target_id = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            return
        u = users_db.get(target_id)
        if not u:
            bot.send_message(message.chat.id, "❌ Пользователь не найден")
            return
        in_q  = target_id in queue
        q_pos = f"Да (позиция {queue.index(target_id)+1})" if in_q else "Нет"
        bot.send_message(
            message.chat.id,
            f"╭─────────────────────\n"
            f"├ 👤 <b>Пользователь {target_id}</b>\n"
            f"├\n"
            f"├ 📛 Имя: {esc(u['first_name'])}\n"
            f"├ 🔗 Username: {'@'+esc(u['username']) if u['username'] else '—'}\n"
            f"├ 💰 Баланс: <b>${u['balance']:.2f}</b>\n"
            f"├ 📦 Сдано: <b>{u['numbers_rented']}</b>\n"
            f"├ 🔄 В очереди: {q_pos}\n"
            f"├ ⏳ На проверке: {'Да' if target_id in pending else 'Нет'}\n"
            f"├ 🚫 Бан: {'Да' if u.get('banned') else 'Нет'}\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup().row(
                InlineKeyboardButton(
                    "🚫 Забанить" if not u.get("banned") else "✅ Разбанить",
                    callback_data=f"adm_ban_{target_id}",
                )
            ),
        )

    elif action == "give_step1":
        try:
            admin_states[uid] = {"action": "give_step2", "target": int(text)}
            bot.send_message(message.chat.id, "💵 Введите сумму (например: 10):")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            del admin_states[uid]

    elif action == "give_step2":
        try:
            amount    = float(text)
            target_id = state["target"]
            u         = get_user(target_id)
            u["balance"] += amount
            u["history"].append({
                "date":   datetime.date.today().strftime("%d.%m"),
                "amount": amount, "status": "Пополнение",
            })
            del admin_states[uid]
            bot.send_message(
                message.chat.id,
                f"✅ Начислено <b>${amount:.2f}</b> пользователю <code>{target_id}</code>",
                parse_mode="HTML",
            )
            try:
                bot.send_message(target_id, f"💰 На ваш баланс начислено <b>${amount:.2f}</b>!", parse_mode="HTML")
            except Exception:
                pass
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    elif action == "take_step1":
        try:
            admin_states[uid] = {"action": "take_step2", "target": int(text)}
            bot.send_message(message.chat.id, "💸 Введите сумму для списания:")
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите числовой ID")
            del admin_states[uid]

    elif action == "take_step2":
        try:
            amount    = float(text)
            target_id = state["target"]
            u         = get_user(target_id)
            u["balance"] = max(0, u["balance"] - amount)
            u["history"].append({
                "date":   datetime.date.today().strftime("%d.%m"),
                "amount": -amount, "status": "Списание",
            })
            del admin_states[uid]
            bot.send_message(
                message.chat.id,
                f"✅ Списано <b>${amount:.2f}</b> у пользователя <code>{target_id}</code>",
                parse_mode="HTML",
            )
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    elif action == "set_payout":
        try:
            amount             = float(text)
            settings["payout"] = amount
            del admin_states[uid]
            bot.send_message(
                message.chat.id,
                f"✅ Выплата за номер изменена на <b>${amount:.2f}</b>",
                parse_mode="HTML",
            )
        except ValueError:
            bot.send_message(message.chat.id, "❌ Введите корректную сумму")
            del admin_states[uid]

    elif action == "reject_reason":
        target_id = state["target"]
        reason    = text
        del admin_states[uid]

        for (achat, amsg) in pending_admin_msgs.get(target_id, []):
            try:
                bot.edit_message_caption(
                    caption=f"❌ <b>ОТКЛОНЕНО</b>\n📝 Причина: {esc(reason)}",
                    chat_id=achat, message_id=amsg, parse_mode="HTML",
                )
            except Exception:
                pass

        _finish_qr_review(target_id)

        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ❌ <b>Ваша заявка отклонена</b>\n"
                f"├\n"
                f"├ 📝 Причина: {esc(reason)}\n"
                f"├\n"
                f"├ Вы добавлены обратно в очередь.\n"
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass
        bot.send_message(message.chat.id, "✅ Заявка отклонена, пользователь уведомлён.")

# ══════════════════════════════════════════════════════
#  Обработчик callback-кнопок
# ══════════════════════════════════════════════════════
@bot.callback_query_handler(func=lambda call: True)
def callback_handler(call):
    bot.answer_callback_query(call.id)
    uid     = call.from_user.id
    chat_id = call.message.chat.id
    msg_id  = call.message.message_id
    data    = call.data
    user    = get_user(uid)

    def edit(text, markup=None):
        try:
            if call.message.photo:
                bot.edit_message_caption(caption=text, chat_id=chat_id,
                                         message_id=msg_id,
                                         parse_mode="HTML", reply_markup=markup)
            else:
                bot.edit_message_text(text, chat_id, msg_id,
                                      parse_mode="HTML", reply_markup=markup)
        except Exception as e:
            print(f"[edit] {e}")
            try:
                bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
            except Exception as e2:
                print(f"[edit fallback] {e2}")

    # ── Назад в меню ──
    if data == "back_menu":
        user_states.pop(uid, None)
        text = welcome_text(call.from_user, user)
        try:
            if BANNER_FILE_ID:
                bot.edit_message_media(
                    InputMediaPhoto(BANNER_FILE_ID, caption=text, parse_mode="HTML"),
                    chat_id, msg_id, reply_markup=main_menu(),
                )
            else:
                edit(text, main_menu())
        except Exception:
            edit(text, main_menu())

    elif data == "rules":
        edit(rules_text(), back_btn())

    elif data == "balance":
        user_states.pop(uid, None)
        edit(balance_text(user), balance_menu())

    elif data == "history":
        edit(history_text(user), back_btn())

    elif data == "statistics":
        edit(statistics_text(), back_btn())

    # ── Сдать номер ──
    elif data == "submit_number":
        if user.get("banned"):
            bot.answer_callback_query(call.id, "🚫 Вы заблокированы!", show_alert=True)
            return

        # Проверяем ворк
        if not work_session["active"]:
            edit(
                "╭─────────────────────\n"
                "├ 🔴 <b>Ворк не активен</b>\n"
                "├\n"
                "├ Сдача номеров сейчас не принимается.\n"
                "├ Ожидайте уведомления о начале ворка.\n"
                "╰─────────────────────",
                back_btn(),
            )
            return

        # Заявка уже на проверке
        if uid in pending:
            edit(
                "╭─────────────────────\n"
                "├ ⏳ <b>Заявка на проверке</b>\n"
                "├\n"
                "├ Ваша заявка уже отправлена администратору.\n"
                "├ Дождитесь решения или отмените заявку.\n"
                "╰─────────────────────",
                pending_menu(),
            )
            return

        # Очередь
        if QUEUE_ENABLED:
            if uid not in queue:
                queue.append(uid)
            pos = queue.index(uid) + 1
            if pos > 1:
                edit(queue_text(pos), back_btn())
                return

        # Запрашиваем номер телефона
        user_states[uid] = "waiting_phone"
        edit(submit_price_text(), back_btn())

    # ── Выбор формата ──
    elif data in ("fmt_qr", "fmt_kod"):
        if user_states.get(uid) != "waiting_format":
            return
        phone = user.get("_pending_phone", "")
        if not phone:
            edit("❌ Ошибка: номер не найден. Попробуйте снова.", back_btn())
            return
        # Проверяем соответствие режима очереди
        chosen = "qr" if data == "fmt_qr" else "kod"
        current_mode = queue_mode["value"]
        if chosen != current_mode:
            mode_label = "📷 QR-код" if current_mode == "qr" else "🔢 КОД"
            edit(
                f"╭─────────────────────\n"
                f"├ ⚠️ <b>Доступен только один формат</b>\n"
                f"├\n"
                f"├ Администратор принимает заявки\n"
                f"├ только в формате: <b>{mode_label}</b>\n"
                f"╰─────────────────────",
                format_choice_menu(),
            )
            return

        user["_pending_format"] = chosen
        user_states.pop(uid, None)

        if data == "fmt_qr":
            # ── QR: сообщаем пользователю, уведомляем админа ──
            if uid in queue:
                queue.remove(uid)
            pending[uid] = msg_id

            entry = {
                "user_id": uid,
                "phone":   phone,
                "format":  "qr",
                "ts":      datetime.datetime.now(),
                "result":  None,
            }
            work_session["entries"].append(entry)

            name     = esc(call.from_user.first_name or "—")
            username = f"@{esc(call.from_user.username)}" if call.from_user.username else "—"
            admin_text = (
                f"╭─────────────────────\n"
                f"├ <b>📷 Новая QR-заявка</b>\n"
                f"├\n"
                f"├ Имя: {name}\n"
                f"├ Username: {username}\n"
                f"├ ID: <code>{uid}</code>\n"
                f"├ Номер: <code>{esc(phone)}</code>\n"
                f"├ Дата: {datetime.date.today().strftime('%d.%m.%Y')}\n"
                f"├ Выплата: <b>${settings['payout']:.2f}</b>\n"
                f"╰─────────────────────"
            )
            sent = notify_all_admins(text=admin_text, markup=admin_qr_notify_btn(uid))
            pending_admin_msgs[uid] = sent

            edit(
                f"╭─────────────────────\n"
                f"├ <b>📷 QR-код выбран</b>\n"
                f"├\n"
                f"├ Номер: <code>{esc(phone)}</code>\n"
                f"├\n"
                f"├ ⏳ В течении <b>2 минут</b> администратор\n"
                f"├ пришлёт вам QR-код.\n"
                f"├ Пожалуйста, ожидайте.\n"
                f"╰─────────────────────",
                back_btn("cancel_application"),
            )

        else:
            # ── КОД: сообщаем пользователю, уведомляем админа ──
            if uid in queue:
                queue.remove(uid)
            pending[uid] = msg_id

            entry = {
                "user_id": uid,
                "phone":   phone,
                "format":  "kod",
                "ts":      datetime.datetime.now(),
                "result":  None,
            }
            work_session["entries"].append(entry)

            name     = esc(call.from_user.first_name or "—")
            username = f"@{esc(call.from_user.username)}" if call.from_user.username else "—"
            admin_text = (
                f"╭─────────────────────\n"
                f"├ <b>🔢 Новая КОД-заявка</b>\n"
                f"├\n"
                f"├ Имя: {name}\n"
                f"├ Username: {username}\n"
                f"├ ID: <code>{uid}</code>\n"
                f"├ Номер: <code>{esc(phone)}</code>\n"
                f"├ Дата: {datetime.date.today().strftime('%d.%m.%Y')}\n"
                f"├ Выплата: <b>${settings['payout']:.2f}</b>\n"
                f"╰─────────────────────"
            )
            sent = notify_all_admins(text=admin_text, markup=admin_kod_notify_btn(uid))
            pending_admin_msgs[uid] = sent
            pending_kod_request[uid] = {"phone": phone, "admin_msgs": sent}

            edit(
                f"╭─────────────────────\n"
                f"├ <b>🔢 КОД выбран</b>\n"
                f"├\n"
                f"├ Номер: <code>{esc(phone)}</code>\n"
                f"├\n"
                f"├ ⏳ В течении <b>5 минут</b> придёт запрос кода.\n"
                f"├ Пожалуйста, подождите.\n"
                f"╰─────────────────────",
                back_btn("cancel_application"),
            )

    # ── Прикрепить QR-код (устарело, оставляем на всякий случай) ──
    elif data == "attach_qr":
        waiting_for_qr.add(uid)
        edit(
            "╭─────────────────────\n"
            "├ <b>📷 Отправьте фото QR-кода</b>\n"
            "├\n"
            "├ Просто прикрепите изображение к чату\n"
            "╰─────────────────────",
            back_btn(),
        )

    # ── Отправить QR-код пользователю (кнопка у АДМИНА) ──
    elif data.startswith("admin_send_qr_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("admin_send_qr_")[1])
        # Запрашиваем у админа фото QR-кода
        admin_states[uid] = {"action": "send_qr_photo", "target": target_id}
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ 📷 <b>Отправьте фото QR-кода</b>\n"
            f"├\n"
            f"├ Пользователь: <code>{target_id}</code>\n"
            f"├ Отправьте фото — оно будет переслано юзеру\n"
            f"╰─────────────────────",
            parse_mode="HTML",
        )

    # ── Запросить код у пользователя (кнопка у АДМИНА) ──
    elif data.startswith("admin_req_kod_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("admin_req_kod_")[1])
        req = pending_kod_request.get(target_id)
        if not req:
            bot.answer_callback_query(call.id, "⚠️ Заявка не найдена или уже обработана", show_alert=True)
            return
        phone = req["phone"]
        pending_kod_input.add(target_id)
        # Уведомить пользователя
        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ <b>🔢 Введите код</b>\n"
                f"├\n"
                f"├ На номер <code>{esc(fmt_phone_display(phone))}</code>\n"
                f"├ был отправлен <b>6-значный код</b>.\n"
                f"├\n"
                f"├ Введите его сюда:\n"
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass
        # Обновить кнопки у всех админов
        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            bot.edit_message_text(
                (call.message.text or "") + f"\n\n⏳ <b>Код запрошен у пользователя</b>",
                chat_id, msg_id, parse_mode="HTML",
            )
        except Exception:
            pass

    # ── Отмена номера администратором (кнопка у АДМИНА) ──
    elif data.startswith("admin_cancel_num_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("admin_cancel_num_")[1])
        req = pending_kod_request.pop(target_id, None)
        pending.pop(target_id, None)
        pending_admin_msgs.pop(target_id, None)
        pending_kod_input.discard(target_id)

        # Удалить запись из ворк-сессии
        for e in reversed(work_session["entries"]):
            if e["user_id"] == target_id and e["result"] is None:
                e["result"] = "cancelled"
                break

        if QUEUE_ENABLED and target_id not in queue:
            queue.append(target_id)

        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ 🚫 <b>Номер убран из очереди</b>\n"
                f"├\n"
                f"├ Ваш номер был убран из очереди администратором.\n"
                f"╰─────────────────────",
                parse_mode="HTML",
                reply_markup=back_btn(),
            )
        except Exception:
            pass

        try:
            bot.edit_message_reply_markup(chat_id, msg_id, reply_markup=None)
            bot.edit_message_text(
                (call.message.text or "") + f"\n\n🚫 <b>Номер отменён</b>",
                chat_id, msg_id, parse_mode="HTML",
            )
        except Exception:
            pass

    # ── Отправить QR на проверку (старый флоу — не используется) ──
    elif data == "send_qr":
        bot.answer_callback_query(call.id, "Используйте новый флоу.", show_alert=True)

    # ── Пользователь нажал «Отсканировал» ──
    elif data == "user_scanned":
        if uid not in pending_scan_confirm:
            bot.answer_callback_query(call.id, "❌ Нет активной заявки!", show_alert=True)
            return
        pending_scan_confirm.pop(uid, None)
        edit(
            "╭─────────────────────\n"
            "├ ✅ <b>Спасибо!</b>\n"
            "├\n"
            "├ Ожидайте результата от администратора.\n"
            "╰─────────────────────",
            back_btn(),
        )
        # Уведомить админов
        notify_all_admins(f"ℹ️ Пользователь <code>{uid}</code> ({esc(users_db.get(uid, {}).get('first_name',''))}) нажал <b>«Отсканировал»</b>", markup=None)

    # ── Отмена заявки пользователем ──
    elif data == "cancel_application":
        if uid not in pending:
            bot.answer_callback_query(call.id, "❌ Нет активной заявки!", show_alert=True)
            return

        for (achat, amsg) in pending_admin_msgs.get(uid, []):
            try:
                bot.edit_message_caption(
                    caption="🚫 <b>ЗАЯВКА ОТМЕНЕНА ПОЛЬЗОВАТЕЛЕМ</b>\n"
                            f"ID: <code>{uid}</code>",
                    chat_id=achat, message_id=amsg, parse_mode="HTML",
                )
            except Exception:
                pass

        # Удалить из ворк-сессии (последнюю запись этого пользователя)
        for e in reversed(work_session["entries"]):
            if e["user_id"] == uid and e["result"] is None:
                e["result"] = "cancelled"
                break

        _finish_qr_review(uid)

        edit(
            "╭─────────────────────\n"
            "├ ✅ <b>Заявка отменена</b>\n"
            "├\n"
            "├ Вы добавлены в конец очереди.\n"
            "╰─────────────────────",
            back_btn(),
        )

    # ── Встал (QR или КОД) — начислить выплату ──
    elif data.startswith("stood_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("_")[1])
        u = get_user(target_id)
        payout = settings["payout"]
        u["balance"]        += payout
        u["numbers_rented"] += 1
        u["history"].append({
            "date":   datetime.date.today().strftime("%d.%m"),
            "amount": payout,
            "status": "Встал",
        })

        # Обновить ворк-запись
        phone_str = ""
        for e in reversed(work_session["entries"]):
            if e["user_id"] == target_id and e["result"] is None:
                e["result"]  = "stood"
                phone_str    = e["phone"]
                break

        _finish_qr_review(target_id)

        # Уведомление пользователю
        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ✅ <b>Встал!</b>\n"
                f"├\n"
                f"├ Ваш номер: <code>{esc(phone_str)}</code>\n"
                f"├ Начислено: <b>${payout:.2f}</b>\n"
                f'├ Баланс: <b>${u["balance"]:.2f}</b>\n'
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass

        # Обновить сообщение у админа
        try:
            if call.message.photo:
                bot.edit_message_caption(
                    caption=(call.message.caption or "") + f"\n\n✅ <b>ВСТАЛ</b> — начислено ${payout:.2f}",
                    chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
                )
            else:
                bot.edit_message_text(
                    (call.message.text or "") + f"\n\n✅ <b>ВСТАЛ</b> — начислено ${payout:.2f}",
                    chat_id, msg_id, parse_mode="HTML",
                )
        except Exception:
            pass

    # ── Не встал — без выплаты ──
    elif data.startswith("not_stood_"):
        if not is_admin(uid):
            return
        target_id = int(data.split("_")[2])

        phone_str = ""
        for e in reversed(work_session["entries"]):
            if e["user_id"] == target_id and e["result"] is None:
                e["result"] = "not_stood"
                phone_str   = e["phone"]
                break

        _finish_qr_review(target_id)

        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ❌ <b>Не встал</b>\n"
                f"├\n"
                f"├ Ваш номер: <code>{esc(phone_str)}</code>\n"
                f"├ К сожалению, выплата не начислена.\n"
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            if call.message.photo:
                bot.edit_message_caption(
                    caption=(call.message.caption or "") + "\n\n❌ <b>НЕ ВСТАЛ</b>",
                    chat_id=chat_id, message_id=msg_id, parse_mode="HTML",
                )
            else:
                bot.edit_message_text(
                    (call.message.text or "") + "\n\n❌ <b>НЕ ВСТАЛ</b>",
                    chat_id, msg_id, parse_mode="HTML",
                )
        except Exception:
            pass

    # ── Ворк-список: отстоял ──
    elif data.startswith("ws_stood_"):
        if not is_admin(uid):
            return
        try:
            entry_idx = int(data.split("ws_stood_")[1])
            e = work_session["entries"][entry_idx]
        except (IndexError, ValueError):
            bot.send_message(chat_id, "❌ Запись не найдена.")
            return

        if e["result"] is not None and e["result"] not in (None,):
            bot.answer_callback_query(call.id, "⚠️ Уже обработано", show_alert=True)
            return

        payout    = settings["payout"]
        target_id = e["user_id"]
        e["result"] = "stood"

        u = get_user(target_id)
        u["balance"]        += payout
        u["numbers_rented"] += 1
        u["history"].append({
            "date":   datetime.date.today().strftime("%d.%m"),
            "amount": payout,
            "status": "Отстоял",
        })

        _finish_qr_review(target_id)

        try:
            bot.send_message(
                target_id,
                f"╭─────────────────────\n"
                f"├ ✅ <b>Отстоял!</b>\n"
                f"├\n"
                f"├ Ваш номер: <code>{esc(e['phone'])}</code> отстоял\n"
                f"├ Начислено: <b>${payout:.2f}</b>\n"
                f'├ Баланс: <b>${u["balance"]:.2f}</b>\n'
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            bot.edit_message_text(
                work_session_list_text(),
                chat_id, msg_id,
                parse_mode="HTML",
                reply_markup=_build_work_list_markup(),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, f"✅ Начислено ${payout:.2f}")

    # ── Ворк-список: не отстоял ──
    elif data.startswith("ws_not_"):
        if not is_admin(uid):
            return
        try:
            entry_idx = int(data.split("ws_not_")[1])
            e = work_session["entries"][entry_idx]
        except (IndexError, ValueError):
            bot.send_message(chat_id, "❌ Запись не найдена.")
            return

        if e["result"] is not None:
            bot.answer_callback_query(call.id, "⚠️ Уже обработано", show_alert=True)
            return

        target_id   = e["user_id"]
        e["result"] = "not_stood"

        _finish_qr_review(target_id)

        try:
            bot.send_message(
                f"├\n"
                f"├ Ваш номер: <code>{esc(e['phone'])}</code> не отстоял\n"
                f"├ Выплата не начислена.\n"
                f"╰─────────────────────",
                parse_mode="HTML",
            )
        except Exception:
            pass

        try:
            bot.edit_message_text(
                work_session_list_text(),
                chat_id, msg_id,
                parse_mode="HTML",
                reply_markup=_build_work_list_markup(),
            )
        except Exception:
            pass
        bot.answer_callback_query(call.id, "❌ Не отстоял")

    # ── Переключение ворка из панели ──
    elif data == "adm_toggle_work":
        if not is_admin(uid):
            return
        if work_session["active"]:
            work_session["active"] = False
            notify_all_users(
                "╭─────────────────────\n"
                "├ 🔴 <b>Ворк завершён!</b>\n"
                "├\n"
                "├ Приём номеров остановлен.\n"
                "╰─────────────────────"
            )
            bot.send_message(chat_id, "🔴 <b>Ворк выключен</b>", parse_mode="HTML", reply_markup=admin_panel_menu())
        else:
            work_session["active"]     = True
            work_session["start_time"] = datetime.datetime.now()
            work_session["entries"]    = []
            notify_all_users(
                "╭─────────────────────\n"
                "├ 🟢 <b>Ворк начался!</b>\n"
                "├\n"
                "├ Теперь вы можете сдать номер.\n"
                "╰─────────────────────"
            )
            bot.send_message(chat_id, "🟢 <b>Ворк включён</b>", parse_mode="HTML", reply_markup=admin_panel_menu())

    # ── Список ворк-сессии (только история) ──
    elif data == "adm_work_list":
        if not is_admin(uid):
            return
        bot.send_message(
            chat_id,
            work_session_list_text(),
            parse_mode="HTML",
        )

    # ── Вывод ──
    elif data == "withdraw":
        if user.get("banned"):
            bot.answer_callback_query(call.id, "🚫 Вы заблокированы!", show_alert=True)
            return
        if user["balance"] < 1.0:
            bot.answer_callback_query(call.id, "❌ Недостаточно средств! Минимум $1.00", show_alert=True)
            return
        user_states[uid] = "waiting_withdraw_amount"
        try:
            if call.message.photo:
                bot.edit_message_caption(caption=withdraw_text(user), chat_id=chat_id,
                                         message_id=msg_id, parse_mode="HTML",
                                         reply_markup=back_btn("balance"))
            else:
                bot.edit_message_text(withdraw_text(user), chat_id, msg_id,
                                      parse_mode="HTML", reply_markup=back_btn("balance"))
        except Exception as e:
            print(f"[withdraw edit] {e}")
            bot.send_message(chat_id, withdraw_text(user), parse_mode="HTML", reply_markup=back_btn("balance"))

    elif data.startswith("withdraw_confirm_"):
        try:
            amount = float(data.split("withdraw_confirm_")[1])
        except Exception:
            return
        if user["balance"] < amount:
            bot.answer_callback_query(call.id, "❌ Недостаточно средств!", show_alert=True)
            return
        user["balance"] -= amount
        user["history"].append({
            "date":   datetime.date.today().strftime("%d.%m"),
            "amount": -amount,
            "status": "Вывод (ожидание)",
        })
        withdraw_counter[0] += 1
        req_id     = withdraw_counter[0]
        first_name = esc(call.from_user.first_name or "—")
        username   = (f"@{esc(call.from_user.username)}" if call.from_user.username else "—")
        withdraw_requests[req_id] = {
            "user_id":    uid,
            "amount":     amount,
            "status":     "pending",
            "first_name": first_name,
            "username":   username,
        }
        edit(
            f"╭─────────────────────\n"
            f"├ <b>✅ Заявка на вывод отправлена!</b>\n"
            f"├\n"
            f"├ Сумма: <b>${amount:.2f} USDT</b>\n"
            f"├ Номер заявки: <b>#{req_id}</b>\n"
            f"├\n"
            f"├ Ожидайте — администратор обработает\n"
            f"╰─────────────────────",
            back_btn(),
        )
        notify_all_admins(
            withdraw_pending_admin_text(req_id, uid, amount, first_name, username),
            markup=admin_withdraw_btn(req_id),
        )

    elif data.startswith("wd_take_"):
        if not is_admin(uid):
            return
        try:
            req_id = int(data.split("wd_take_")[1])
        except Exception:
            return
        _process_withdraw_take(req_id, chat_id, msg_id)

    elif data.startswith("wd_reject_"):
        if not is_admin(uid):
            return
        try:
            req_id = int(data.split("wd_reject_")[1])
        except Exception:
            return
        _process_withdraw_reject(req_id, chat_id, msg_id)

    # ── Очередь: показать выбор QR/КОД ──
    elif data == "adm_queue_mode":
        if not is_admin(uid):
            return
        mode_icon = "📷 QR-код" if queue_mode["value"] == "qr" else "🔢 КОД"
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ ⚙️ <b>Режим очереди</b>\n"
            f"├\n"
            f"├ Текущий режим: <b>{mode_icon}</b>\n"
            f"├\n"
            f"├ Выберите тип заявок, которые будут\n"
            f"├ приходить администратору:\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=queue_mode_choice_btn(),
        )

    # ── Установить режим QR ──
    elif data == "adm_set_mode_qr":
        if not is_admin(uid):
            return
        queue_mode["value"] = "qr"
        try:
            bot.edit_message_text(
                f"╭─────────────────────\n"
                f"├ ⚙️ <b>Режим очереди изменён</b>\n"
                f"├\n"
                f"├ ✅ Теперь принимаются только <b>QR-код</b> заявки\n"
                f"╰─────────────────────",
                chat_id, msg_id, parse_mode="HTML",
            )
        except Exception:
            bot.send_message(chat_id, "✅ Режим: <b>QR-код</b>", parse_mode="HTML")

    # ── Установить режим КОД ──
    elif data == "adm_set_mode_kod":
        if not is_admin(uid):
            return
        queue_mode["value"] = "kod"
        try:
            bot.edit_message_text(
                f"╭─────────────────────\n"
                f"├ ⚙️ <b>Режим очереди изменён</b>\n"
                f"├\n"
                f"├ ✅ Теперь принимаются только <b>КОД</b> заявки\n"
                f"╰─────────────────────",
                chat_id, msg_id, parse_mode="HTML",
            )
        except Exception:
            bot.send_message(chat_id, "✅ Режим: <b>КОД</b>", parse_mode="HTML")

    # ── Список номеров ──
    elif data == "adm_number_list":
        if not is_admin(uid):
            return
        entries = work_session["entries"]
        if not entries:
            bot.send_message(
                chat_id,
                "╭─────────────────────\n"
                "├ 📋 <b>Список номеров пуст</b>\n"
                "╰─────────────────────",
                parse_mode="HTML",
            )
            return
        # Строим клавиатуру: одна кнопка на каждый номер
        m = InlineKeyboardMarkup()
        for idx, e in enumerate(entries):
            u = users_db.get(e["user_id"], {})
            un = f"@{u['username']}" if u.get("username") else str(e["user_id"])
            result_mark = " ✅" if e["result"] == "stood" else (" ❌" if e["result"] == "not_stood" else "")
            lbl = f"{fmt_phone_display(e['phone'])}/{un}{result_mark}"
            m.row(InlineKeyboardButton(lbl, callback_data=f"nl_entry_{idx}"))
        start_str = work_session["start_time"].strftime("%d.%m %H:%M") if work_session["start_time"] else "—"
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ 📋 <b>Список номеров</b>\n"
            f"├ Сессия с {start_str}\n"
            f"├ Всего: <b>{len(entries)}</b>\n"
            f"╰─────────────────────",
            parse_mode="HTML",
            reply_markup=m,
        )

    # ── Детали конкретного номера из списка ──
    elif data.startswith("nl_entry_"):
        if not is_admin(uid):
            return
        try:
            entry_idx = int(data.split("nl_entry_")[1])
            e = work_session["entries"][entry_idx]
        except (IndexError, ValueError):
            bot.send_message(chat_id, "❌ Запись не найдена.")
            return
        u = users_db.get(e["user_id"], {})
        un = f"@{u['username']}" if u.get("username") else "—"
        name = esc(u.get("first_name") or str(e["user_id"]))
        fmt = "📷 QR-код" if e["format"] == "qr" else "🔢 КОД"
        ts_str = e["ts"].strftime("%d.%m.%Y %H:%M") if e.get("ts") else "—"
        if e["result"] == "stood":
            result_str = "✅ Отстоял"
        elif e["result"] == "not_stood":
            result_str = "❌ Не отстоял"
        elif e["result"] == "cancelled":
            result_str = "🚫 Отменён"
        else:
            result_str = "⏳ Ожидает"
        detail_text = (
            f"╭─────────────────────\n"
            f"├ 📋 <b>Номер #{entry_idx + 1}</b>\n"
            f"├\n"
            f"├ 📱 Номер: <code>{esc(fmt_phone_display(e['phone']))}</code>\n"
            f"├ 👤 Имя: {name}\n"
            f"├ 🔗 Username: {un}\n"
            f"├ 🆔 ID: <code>{e['user_id']}</code>\n"
            f"├ 📅 Дата: {ts_str}\n"
            f"├ 📂 Формат: {fmt}\n"
            f"├ 🏁 Статус: {result_str}\n"
            f"╰─────────────────────"
        )
        # Кнопки отстоял/неотстоял только если ещё не обработан
        if e["result"] is None:
            mk = number_list_detail_btn(entry_idx)
        else:
            mk = InlineKeyboardMarkup()
            mk.row(InlineKeyboardButton("◀️ Назад", callback_data="adm_number_list"))
        bot.send_message(chat_id, detail_text, parse_mode="HTML", reply_markup=mk)

    # ── Статистика (общая) ──
    elif data == "adm_stats":
        if not is_admin(uid):
            return
        work_status = "🟢 Активен" if work_session["active"] else "🔴 Выключен"
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ 📊 <b>Статистика бота</b>\n"
            f"├\n"
            f"├ 👥 Пользователей: <b>{len(users_db)}</b>\n"
            f"├ 📦 Всего сдано: <b>{sum(u['numbers_rented'] for u in users_db.values())}</b>\n"
            f"├ 💰 На балансах: <b>${sum(u['balance'] for u in users_db.values()):.2f}</b>\n"
            f"├ 🔄 В очереди: <b>{len(queue)}</b>\n"
            f"├ ⏳ На проверке: <b>{len(pending)}</b>\n"
            f"├ 💵 Выплата: <b>${settings['payout']:.2f}</b>\n"
            f"├ ⚙️ Ворк: {work_status}\n"
            f"├ 📋 В сессии: <b>{len(work_session['entries'])}</b> номеров\n"
            f"╰─────────────────────",
            parse_mode="HTML",
        )

    elif data == "adm_top_stats":
        if not is_admin(uid):
            return
        bot.send_message(chat_id, admin_top_stats_text(), parse_mode="HTML")

    elif data == "adm_check":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "check_user"}
        bot.send_message(chat_id, "🔍 Введите ID пользователя:")

    elif data == "adm_give":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "give_step1"}
        bot.send_message(chat_id, "💰 Введите ID пользователя для начисления:")

    elif data == "adm_take":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "take_step1"}
        bot.send_message(chat_id, "💸 Введите ID пользователя для списания:")

    elif data == "adm_reset_all":
        if not is_admin(uid):
            return
        mk = InlineKeyboardMarkup()
        mk.row(
            InlineKeyboardButton("✅ Да, обнулить", callback_data="adm_reset_confirm"),
            InlineKeyboardButton("❌ Отмена",        callback_data="adm_cancel"),
        )
        bot.send_message(chat_id, "⚠️ <b>Обнулить баланс ВСЕХ пользователей?</b>",
                         parse_mode="HTML", reply_markup=mk)

    elif data == "adm_reset_confirm":
        if not is_admin(uid):
            return
        for u in users_db.values():
            u["balance"] = 0.0
            u["history"].append({
                "date":   datetime.date.today().strftime("%d.%m"),
                "amount": 0,
                "status": "Обнуление",
            })
        try:
            bot.edit_message_text("✅ Балансы всех пользователей обнулены.", chat_id, msg_id)
        except Exception:
            bot.send_message(chat_id, "✅ Балансы всех пользователей обнулены.")

    elif data == "adm_cancel":
        if not is_admin(uid):
            return
        try:
            bot.delete_message(chat_id, msg_id)
        except Exception:
            pass

    elif data == "adm_broadcast":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "broadcast"}
        bot.send_message(chat_id, "📢 Введите текст рассылки:")

    elif data == "adm_payout":
        if not is_admin(uid):
            return
        admin_states[uid] = {"action": "set_payout"}
        bot.send_message(
            chat_id,
            f"💵 Текущая выплата: <b>${settings['payout']:.2f}</b>\n\nВведите новую сумму:",
            parse_mode="HTML",
        )

    elif data.startswith("adm_ban_"):
        if not is_admin(uid):
            return
        target_id   = int(data.split("_")[2])
        u           = get_user(target_id)
        u["banned"] = not u.get("banned", False)
        status      = "🚫 Заблокирован" if u["banned"] else "✅ Разблокирован"
        bot.send_message(chat_id, f"{status}: <code>{target_id}</code>", parse_mode="HTML")
        try:
            bot.send_message(
                target_id,
                "🚫 Вы заблокированы администратором." if u["banned"] else "✅ Ваш аккаунт разблокирован.",
            )
        except Exception:
            pass


# ══════════════════════════════════════════════════════
#  Вспомогательная: клавиатура ворк-списка
# ══════════════════════════════════════════════════════
def _build_work_list_markup():
    """Кнопки Отстоял/Не отстоял для каждой необработанной записи."""
    m = InlineKeyboardMarkup()
    for idx, e in enumerate(work_session["entries"]):
        if e["result"] in (None,):
            label = f"{esc(e['phone'])} [{('QR' if e['format']=='qr' else 'КОД')}]"
            m.row(
                InlineKeyboardButton(f"✅ {label}", callback_data=f"ws_stood_{idx}"),
                InlineKeyboardButton(f"❌ {label}", callback_data=f"ws_not_{idx}"),
            )
    return m if work_session["entries"] else None


# ══════════════════════════════════════════════════════
#  Вспомогательная: КОД-заявка (без фото)
# ══════════════════════════════════════════════════════
def _submit_kod_entry(uid, chat_id, msg_id, phone, call):
    """Оформить заявку по формату КОД (без QR-фото)."""
    if uid in queue:
        queue.remove(uid)

    pending[uid] = msg_id

    entry = {
        "user_id": uid,
        "phone":   phone,
        "format":  "kod",
        "ts":      datetime.datetime.now(),
        "result":  None,
    }
    work_session["entries"].append(entry)

    name     = esc(call.from_user.first_name or "—")
    username = f"@{esc(call.from_user.username)}" if call.from_user.username else "—"
    admin_text = (
        f"╭─────────────────────\n"
        f"├ <b>🔢 Новая КОД-заявка</b>\n"
        f"├\n"
        f"├ Имя: {name}\n"
        f"├ Username: {username}\n"
        f"├ ID: <code>{uid}</code>\n"
        f"├ Номер: <code>{esc(phone)}</code>\n"
        f"├ Дата: {datetime.date.today().strftime('%d.%m.%Y')}\n"
        f"├ Выплата: <b>${settings['payout']:.2f}</b>\n"
        f"╰─────────────────────"
    )

    sent = notify_all_admins(
        text=admin_text,
        markup=admin_review_kod_btn(uid),
    )
    pending_admin_msgs[uid] = sent

    try:
        if call.message.photo:
            bot.edit_message_caption(
                caption=(
                    f"╭─────────────────────\n"
                    f"├ <b>✅ КОД-заявка отправлена!</b>\n"
                    f"├\n"
                    f"├ Номер: <code>{esc(phone)}</code>\n"
                    f"├ Ожидайте решения администратора.\n"
                    f"╰─────────────────────"
                ),
                chat_id=chat_id, message_id=msg_id,
                parse_mode="HTML", reply_markup=pending_menu(),
            )
        else:
            bot.edit_message_text(
                f"╭─────────────────────\n"
                f"├ <b>✅ КОД-заявка отправлена!</b>\n"
                f"├\n"
                f"├ Номер: <code>{esc(phone)}</code>\n"
                f"├ Ожидайте решения администратора.\n"
                f"╰─────────────────────",
                chat_id, msg_id, parse_mode="HTML", reply_markup=pending_menu(),
            )
    except Exception:
        bot.send_message(
            chat_id,
            f"╭─────────────────────\n"
            f"├ <b>✅ КОД-заявка отправлена!</b>\n"
            f"├ Номер: <code>{esc(phone)}</code>\n"
            f"╰─────────────────────",
            parse_mode="HTML", reply_markup=pending_menu(),
        )


# ══════════════════════════════════════════════════════
if __name__ == "__main__":
    print("✅ Бот запущен...")
    print(f"   💵 Выплата: ${settings['payout']:.2f}")
    print(f"   👑 Admin IDs: {ADMIN_IDS}")
    bot.infinity_polling()
