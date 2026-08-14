"""
Раздел "Лидеры".

Идея:
    Три категории лидербордов:
      - "level"  — по уровню игрока (prof.py: level_from_xp);
      - "fire"   — по количеству подаренных 🔥 огоньков (prof.py:
                   reputation_red/reputation_blue, начисляются через
                   подарки, см. prof.py: gift_send/process_gift_buy);
      - "achv"   — по количеству выполненных ачивок (achives.py:
                   user_achievements).
    У каждой категории — 5 периодов: сегодня / вчера / неделя / месяц /
    всё время. Топ-10 в каждом срезе.

Период vs. "всё время":
    "Достижения" — период считается ТОЧНО: у каждой открытой ачивки уже
    есть настоящая метка времени (user_achievements.unlocked_at, см.
    achives.py), поэтому здесь просто COUNT(*) по этому окну.

    "Уровень" и "Огоньки" хранятся в БД только как текущий ИТОГ (users.xp,
    users.reputation_red/blue) — без истории начислений посчитать
    "сколько именно за эту неделю" было бы нечем. Поэтому этот модуль
    сам ведёт лёгкий лог начислений (см. "СХЕМА" ниже: leader_events) —
    prof.py дергает log_xp_gain()/log_fire_gain() сразу после того, как
    начисляет опыт (add_xp) или репутацию (_add_reputation), с реальной
    (уже клиппнутой по нулю) разницей, штрафы (amount <= 0) не логируются.
    На "всё время" эти логи не нужны — там просто берётся текущий
    итог из users, так что старые игроки (заведшиеся ДО появления этого
    модуля) сразу попадают в лидерборд "всё время" наравне со всеми, но
    в срезах по периодам появятся только с момента, когда сыграна первая
    операция после подключения этого модуля.

    "Неделя"/"месяц" — скользящее окно (последние 7/30 дней от текущего
    момента), а не календарная неделя/месяц — так снимается вопрос "с
    какого дня неделя" (пн/вс) и часового пояса игроков.

Подключение в main.py:
    import leaders
    dp.include_router(leaders.router)
    Кнопка "Лидеры" — уже добавлена отдельным рядом в
    main.main_menu_keyboard(), с кастомным тг-эмодзи 🏆
    (main.LEADERS_EMOJI_ID, тот же id, что и LEADERS_EMOJI_ID здесь).

Подключение в prof.py (уже сделано):
    add_xp() — после клиппинга по нулю, локальный `import leaders` и
    `await leaders.log_xp_gain(user_id, new_xp - old_xp)` (только если
    разница положительная).
    _add_reputation() — то же самое, `await leaders.log_fire_gain(user_id,
    new_value - old_value)` после выхода из user_lock.

Зависимость:
    pip install aiosqlite --break-system-packages
"""

import html
import time
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import prof
import achives

router = Router(name="leaders")

TOP_LIMIT = 10


# ==========================
#   КАТЕГОРИИ И ПЕРИОДЫ
# ==========================

CATEGORIES = {
    "level": {"ru": "Уровень", "en": "Level"},
    "fire": {"ru": "Огоньки", "en": "Fire gifts"},
    "achv": {"ru": "Достижения", "en": "Achievements"},
}
CATEGORY_ORDER = ["level", "fire", "achv"]
# Юникод-фолбэк (используется только если для категории вдруг нет
# кастомного id — сейчас такого нет ни у одной, все три ниже покрыты).
CATEGORY_GLYPH = {"level": "⭐", "fire": "🔥", "achv": "🎖"}
# Кастомные тг-эмодзи — те же id, что уже используются для этого же
# смысла в других разделах: уровень — тот же, что и prof.CE_LEVEL на
# карточке профиля; достижения — тот же, что и заголовок раздела
# "Достижения"/уведомление об открытии ачивки в achives.py
# (UNLOCK_HEADER_EMOJI_ID). "Огоньки" в prof.py их вообще два —
# красная и синяя репутация (EMOJI_REP_RED_ID/EMOJI_REP_BLUE_ID), но
# здесь они считаются одним общим счётчиком, поэтому иконкой взят
# красный (тот же, что и prof.CE_REP_RED).
CATEGORY_EMOJI_ID = {
    "level": prof.EMOJI_LEVEL_ID,
    "fire": prof.EMOJI_REP_RED_ID,
    "achv": achives.UNLOCK_HEADER_EMOJI_ID,
}


def _ce(emoji_id: str, glyph: str) -> str:
    return f'<tg-emoji emoji-id="{emoji_id}">{glyph}</tg-emoji>'


def _category_icon_text(cat_id: str) -> str:
    """Иконка категории для текста сообщения — кастомный тг-эмодзи
    (тот же id, что уже используется для этого же смысла в других
    разделах: уровень/огоньки — с карточки профиля, достижения — с
    заголовка раздела "Достижения")."""
    emoji_id = CATEGORY_EMOJI_ID.get(cat_id)
    glyph = CATEGORY_GLYPH[cat_id]
    return _ce(emoji_id, glyph) if emoji_id else glyph


PERIODS = {
    "today": {"ru": "Сегодня", "en": "Today"},
    "yesterday": {"ru": "Вчера", "en": "Yesterday"},
    "week": {"ru": "Неделя", "en": "Week"},
    "month": {"ru": "Месяц", "en": "Month"},
    "all": {"ru": "Всё время", "en": "All time"},
}
PERIOD_ORDER = ["today", "yesterday", "week", "month", "all"]
# Иконка на всех 5 кнопках периода — один и тот же 🗓, через
# icon_custom_emoji_id (см. _leaderboard_keyboard).
PERIOD_EMOJI_ID = "5413879192267805083"

# Иконки мест 1–10 в самой таблице лидеров (кастомные тг-эмодзи —
# 🥇🥈🥉 и далее нумерованные 4️⃣–🔟). Индекс списка = место - 1.
PLACE_EMOJI_ID = [
    "5440539497383087970",  # 🥇
    "5447203607294265305",  # 🥈
    "5453902265922376865",  # 🥉
    "5830434773088083875",  # 4️⃣
    "5827941630472100575",  # 5️⃣
    "5830446086031941275",  # 6️⃣
    "5830341589477629469",  # 7️⃣
    "5829992769413717353",  # 8️⃣
    "5830388872772591861",  # 9️⃣
    "5827670648100494160",  # 🔟
]
PLACE_GLYPH = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def _place_text(index: int) -> str:
    """Иконка места в таблице (index — с нуля). Кастомный тг-эмодзи для
    мест 1–10 (столько и выводится, см. TOP_LIMIT); дальше, на случай
    если TOP_LIMIT когда-нибудь увеличат, — просто "11.", "12." и т.д."""
    if index < len(PLACE_EMOJI_ID):
        return _ce(PLACE_EMOJI_ID[index], PLACE_GLYPH[index])
    return f"{index + 1}."


# ==========================
#   СХЕМА (лениво, тем же приёмом, что и prof.py:
#   _ensure_gift_schema/_ensure_general_achv_schema — не трогаем
#   database.py, просто CREATE TABLE IF NOT EXISTS при первом обращении)
# ==========================

_schema_ready = False


async def _ensure_schema() -> None:
    global _schema_ready
    if _schema_ready:
        return
    db = await database.get_db()
    await db.execute(
        "CREATE TABLE IF NOT EXISTS leader_events ("
        "user_id INTEGER NOT NULL, "
        "metric TEXT NOT NULL, "  # 'xp' | 'fire'
        "amount INTEGER NOT NULL, "
        "created_at REAL NOT NULL)"
    )
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_leader_events_metric_time "
        "ON leader_events(metric, created_at)"
    )
    await database.flush()
    _schema_ready = True


# ==========================
#   ХУКИ ЛОГИРОВАНИЯ (дергаются из prof.py)
# ==========================

async def log_xp_gain(user_id: int, amount: int) -> None:
    """Один прирост опыта — для периодных срезов лидерборда "Уровень".
    amount <= 0 игнорируется (штрафы в лидерборд не идут)."""
    if amount <= 0:
        return
    await _ensure_schema()
    db = await database.get_db()
    await db.execute(
        "INSERT INTO leader_events (user_id, metric, amount, created_at) "
        "VALUES (?, 'xp', ?, ?)",
        (user_id, amount, time.time()),
    )
    await database.commit()


async def log_fire_gain(user_id: int, amount: int) -> None:
    """Один прирост 🔥 (красной или синей репутации — для лидерборда
    обе считаются одним и тем же "огоньком") — для периодных срезов
    лидерборда "Огоньки". amount <= 0 игнорируется."""
    if amount <= 0:
        return
    await _ensure_schema()
    db = await database.get_db()
    await db.execute(
        "INSERT INTO leader_events (user_id, metric, amount, created_at) "
        "VALUES (?, 'fire', ?, ?)",
        (user_id, amount, time.time()),
    )
    await database.commit()


# ==========================
#   ГРАНИЦЫ ПЕРИОДА
# ==========================

def _period_bounds(period: str) -> tuple[float, float]:
    now = datetime.now()
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), now.timestamp()
    if period == "yesterday":
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = today_start - timedelta(days=1)
        return start.timestamp(), today_start.timestamp()
    if period == "week":
        return (now - timedelta(days=7)).timestamp(), now.timestamp()
    if period == "month":
        return (now - timedelta(days=30)).timestamp(), now.timestamp()
    # "all" — с начала эпохи и по сейчас.
    return 0.0, now.timestamp()


# ==========================
#   ИМЕНА ИГРОКОВ
# ==========================

async def _names_for(user_ids: list[int]) -> dict[int, str]:
    """Отображаемое имя для списка игроков разом (см. prof.py:
    _display_name — та же логика: сохранённое display_name -> @username
    -> first_name), без похода в prof.py по одному игроку за раз."""
    if not user_ids:
        return {}
    await _ensure_schema()
    db = await database.get_db()
    placeholders = ",".join("?" for _ in user_ids)
    async with db.execute(
        f"SELECT user_id, username, first_name, display_name "
        f"FROM users WHERE user_id IN ({placeholders})",
        user_ids,
    ) as cursor:
        rows = await cursor.fetchall()

    names: dict[int, str] = {}
    for row in rows:
        if row["display_name"]:
            names[row["user_id"]] = row["display_name"]
        elif row["username"]:
            names[row["user_id"]] = f"@{html.escape(row['username'])}"
        else:
            names[row["user_id"]] = html.escape(row["first_name"] or "—")
    return names


# ==========================
#   ЗАПРОСЫ ЛИДЕРБОРДА
# ==========================

async def _top_level(period: str) -> list[dict]:
    db = await database.get_db()
    if period == "all":
        async with db.execute(
            "SELECT user_id, xp FROM users WHERE xp > 0 "
            "ORDER BY xp DESC LIMIT ?",
            (TOP_LIMIT,),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            level, _, _ = prof.level_from_xp(row["xp"])
            result.append({"user_id": row["user_id"], "level": level, "xp": row["xp"]})
        return result

    await _ensure_schema()
    start, end = _period_bounds(period)
    # Тут раньше сортировали SQL-запросом по gained (сколько опыта
    # набрано за период), а показывали в списке итоговый текущий
    # уровень (level_from_xp(current_xp)) — два разных числа, поэтому
    # порядок в топе не совпадал с показанным уровнем (95 мог стоять
    # ниже 90). LIMIT в SQL тоже убран: он раньше отсекал топ по
    # gained ДО пересчёта на уровень, из-за чего игрок с высоким
    # уровнем, но небольшим приростом за период, мог вообще не попасть
    # в кандидаты. Теперь берём всех, кто был активен в периоде, и уже
    # в питоне сортируем и режем по TOP_LIMIT по тому же полю, которое
    # показываем — level (при равенстве — по xp).
    async with db.execute(
        "SELECT user_id, SUM(amount) AS gained FROM leader_events "
        "WHERE metric = 'xp' AND created_at >= ? AND created_at <= ? "
        "GROUP BY user_id",
        (start, end),
    ) as cursor:
        rows = await cursor.fetchall()

    result = []
    for row in rows:
        current_xp = await prof.get_xp(row["user_id"])
        level, _, _ = prof.level_from_xp(current_xp)
        result.append({
            "user_id": row["user_id"],
            "level": level,
            "xp": current_xp,
            "gained": row["gained"],
        })

    result.sort(key=lambda r: (r["level"], r["xp"]), reverse=True)
    return result[:TOP_LIMIT]


async def _top_fire(period: str) -> list[dict]:
    db = await database.get_db()
    if period == "all":
        async with db.execute(
            "SELECT user_id, reputation_red + reputation_blue AS fire FROM users "
            "WHERE reputation_red + reputation_blue > 0 "
            "ORDER BY fire DESC LIMIT ?",
            (TOP_LIMIT,),
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"user_id": row["user_id"], "fire": row["fire"]} for row in rows]

    await _ensure_schema()
    start, end = _period_bounds(period)
    async with db.execute(
        "SELECT user_id, SUM(amount) AS gained FROM leader_events "
        "WHERE metric = 'fire' AND created_at >= ? AND created_at <= ? "
        "GROUP BY user_id ORDER BY gained DESC LIMIT ?",
        (start, end, TOP_LIMIT),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"user_id": row["user_id"], "gained": row["gained"]} for row in rows]


async def _top_achv(period: str) -> list[dict]:
    db = await database.get_db()
    start, end = _period_bounds(period)
    async with db.execute(
        "SELECT user_id, COUNT(*) AS cnt FROM user_achievements "
        "WHERE unlocked_at >= ? AND unlocked_at <= ? "
        "GROUP BY user_id ORDER BY cnt DESC, MIN(unlocked_at) ASC LIMIT ?",
        (start, end, TOP_LIMIT),
    ) as cursor:
        rows = await cursor.fetchall()
    return [{"user_id": row["user_id"], "cnt": row["cnt"]} for row in rows]


async def _top_for(cat_id: str, period: str) -> list[dict]:
    if cat_id == "level":
        return await _top_level(period)
    if cat_id == "fire":
        return await _top_fire(period)
    return await _top_achv(period)


# ==========================
#   ФОРМАТИРОВАНИЕ
# ==========================

_EMPTY_TEXT = {
    "ru": "Пока здесь пусто — в этом периоде никто не набрал ни одного очка.",
    "en": "Nobody's scored anything in this period yet.",
}


def _row_value_text(lang: str, cat_id: str, period: str, row: dict) -> str:
    if cat_id == "level":
        label = "Уровень" if lang == "ru" else "Level"
        return f"{_category_icon_text('level')} {label} {row['level']}"
    if cat_id == "fire":
        icon = _category_icon_text("fire")
        if period == "all":
            return f"{icon} {row['fire']}"
        return f"{icon} +{row['gained']}"
    # achv
    count = row["cnt"]
    label = "ачивок" if lang == "ru" else "achievements"
    return f"{_category_icon_text('achv')} {count} {label}"


async def _render_leaderboard(lang: str, cat_id: str, period: str) -> str:
    rows = await _top_for(cat_id, period)
    cat_title = CATEGORIES[cat_id][lang]
    period_title = PERIODS[period][lang]
    icon = _category_icon_text(cat_id)

    lines = [f"<b>{icon} {cat_title} — {period_title}</b>", ""]

    if not rows:
        lines.append(_EMPTY_TEXT[lang])
        return "\n".join(lines)

    names = await _names_for([row["user_id"] for row in rows])
    for i, row in enumerate(rows):
        place = _place_text(i)
        name = names.get(row["user_id"], f"ID {row['user_id']}")
        value = _row_value_text(lang, cat_id, period, row)
        lines.append(f"{place} <b>{name}</b> — {value}")

    return "\n".join(lines)


# ==========================
#   КЛАВИАТУРЫ
# ==========================
#
# Один экран, а не двухшаговое меню: сверху ряд из 3 кнопок-категорий,
# снизу ряд из 5 кнопок-периодов — переключение любой из них сразу
# перерисовывает таблицу лидеров под новую пару (cat_id, period), без
# промежуточных "выберите категорию" / "выберите период" экранов.
# Выбранная кнопка в каждом ряду помечается точками по бокам.

DEFAULT_CATEGORY = "level"
DEFAULT_PERIOD = "all"


def _leaderboard_keyboard(lang: str, cat_id: str, period: str) -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    for c_id in CATEGORY_ORDER:
        title = CATEGORIES[c_id][lang]
        text = f"• {title} •" if c_id == cat_id else title
        emoji_id = CATEGORY_EMOJI_ID.get(c_id)
        if emoji_id:
            # Кастомный тг-эмодзи — только через icon_custom_emoji_id,
            # тег <tg-emoji> в тексте кнопки не рендерится (тот же
            # нюанс, что и в achives.py/main.py).
            kb.button(
                text=text,
                callback_data=f"lead:set:{c_id}:{period}",
                icon_custom_emoji_id=emoji_id,
            )
        else:
            glyph = CATEGORY_GLYPH[c_id]
            kb.button(text=f"{glyph} {text}", callback_data=f"lead:set:{c_id}:{period}")
    # Периоды — двумя рядами: "Сегодня/Вчера/Неделя" сверху,
    # "Месяц/Всё время" снизу.
    for p_id in PERIOD_ORDER:
        title = PERIODS[p_id][lang]
        text = f"• {title} •" if p_id == period else title
        kb.button(
            text=text,
            callback_data=f"lead:set:{cat_id}:{p_id}",
            icon_custom_emoji_id=PERIOD_EMOJI_ID,
        )
    kb.adjust(3, 3, 2)
    return kb.as_markup()


# ==========================
#   МЕНЮ
# ==========================

BUTTON_TEXT = {
    "ru": "Лидеры",
    "en": "Leaders",
}
# Текст ДОЛЖЕН совпадать буквально с main.TEXTS[lang]["menu_leaders"] —
# см. тот же нюанс в achives.py: F.text.in_(BUTTON_TEXT.values()) ниже
# матчит именно текст сообщения. Эмодзи на кнопке теперь через
# icon_custom_emoji_id=main.LEADERS_EMOJI_ID (см. main.py:
# main_menu_keyboard) — тег <tg-emoji> в тексте кнопки не рендерится,
# поэтому в самом BUTTON_TEXT его быть не должно.
LEADERS_EMOJI_ID = "5413566144986503832"


async def _get_lang(state: FSMContext, user_id: int) -> str:
    data = await state.get_data()
    lang = data.get("lang")
    if lang:
        return lang

    onboarding = await database.get_onboarding(user_id)
    lang = (onboarding["lang"] if onboarding else None) or "ru"
    await state.update_data(lang=lang)
    return lang


@router.message(F.text.in_(BUTTON_TEXT.values()))
async def open_leaders(message: Message, state: FSMContext) -> None:
    lang = await _get_lang(state, message.from_user.id)
    text = await _render_leaderboard(lang, DEFAULT_CATEGORY, DEFAULT_PERIOD)
    keyboard = _leaderboard_keyboard(lang, DEFAULT_CATEGORY, DEFAULT_PERIOD)

    # Картинка раздела (админ-панель, admin:sections, ключ "leaders") —
    # тот же приём, что и у "Достижения" в achives.py. Локальный
    # импорт — admin.py импортирует часть разделов на верхнем уровне,
    # безопаснее не тянуть его сюда заранее.
    import admin

    await admin.send_with_section_image(message, "leaders", text, reply_markup=keyboard)


@router.callback_query(F.data.startswith("lead:set:"))
async def switch_leaderboard(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, cat_id, period = callback.data.split(":")
    lang = await _get_lang(state, callback.from_user.id)

    text = await _render_leaderboard(lang, cat_id, period)
    keyboard = _leaderboard_keyboard(lang, cat_id, period)

    import admin

    await admin.smart_edit(callback.message, text, reply_markup=keyboard)
    await callback.answer()


@router.callback_query(F.data == "lead:noop")
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()
