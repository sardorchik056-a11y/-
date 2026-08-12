"""
Клиент для приёма платежей через CryptoBot и xRocket — как альтернатива
Telegram Stars в разделе "Донаты" (donate.py).

Оба сервиса работают одинаково с точки зрения этого модуля:
    1. create_*_invoice(...)  — создаём инвойс на сумму в USD, получаем
       ссылку на оплату (pay_url) и id инвойса у провайдера;
    2. get_*_invoice_status(...) — периодически (или по кнопке "Проверить
       оплату") спрашиваем у провайдера, оплачен ли инвойс.

Ни у CryptoBot, ни у xRocket здесь НЕ используются вебхуки — у бота нет
своего HTTPS-сервера (он работает через long polling, см. main.py), а
поднимать отдельный веб-сервер только ради вебхуков избыточно. Вместо
этого donate.py:
    - предлагает пользователю кнопку "✅ Я оплатил / Проверить оплату"
      (мгновенная проверка по требованию);
    - и параллельно фоновым циклом (см. donate.py: start_crypto_poll_loop)
      сам периодически опрашивает все неоплаченные инвойсы — так деньги
      зачислятся, даже если пользователь не нажал кнопку проверки.

==========================
  НАСТРОЙКА (ОБЯЗАТЕЛЬНО)
==========================
Чтобы способы оплаты реально заработали, нужно вписать сюда токены:

CryptoBot:
    1. Открыть @CryptoBot в Telegram -> Crypto Pay -> Create App.
    2. Скопировать выданный API Token в CRYPTOBOT_API_TOKEN ниже.

xRocket:
    1. Открыть @xRocket в Telegram -> Settings -> Pay API (или
       обратиться в поддержку @TonRocketSupportBot) -> получить токен.
    2. Скопировать токен в XROCKET_API_TOKEN ниже.

Пока токен пустой — соответствующий способ оплаты просто не показывается
пользователю (см. is_cryptobot_enabled/is_xrocket_enabled), бот не падает.

ВАЖНО про xRocket: рабочий базовый адрес API — https://pay.xrocket.tg,
эндпоинты "/tg-invoices" (создание) и "/tg-invoices/{id}" (проверка),
авторизация заголовком "Rocket-Pay-Key: <токен>" (без "Bearer"). Именно
эта схема подтверждена рабочим конфигом в donate.py — используем её
здесь один в один, без перебора альтернативных доменов/путей.
"""

import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


# ==========================
#   ТОКЕНЫ (ВПИШИТЕ СВОИ)
# ==========================

CRYPTOBOT_API_TOKEN = "582363:AALEf7JOugnrQyrkMHzH5UrO7pdOjjYnTQy"   # токен из @CryptoBot -> Crypto Pay -> Create App
XROCKET_API_TOKEN = "034cea3212dcfe762c3dc3093"     # токен из @xRocket -> Pay API


def is_cryptobot_enabled() -> bool:
    return bool(CRYPTOBOT_API_TOKEN)


def is_xrocket_enabled() -> bool:
    return bool(XROCKET_API_TOKEN)


# Сколько ждать оплату, прежде чем инвойс сгорит (в секундах) — совпадает
# с интервалом, в течение которого фоновый цикл donate.py его опрашивает.
INVOICE_EXPIRES_IN = 1800  # 30 минут

_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=15)


# ==========================
#   CRYPTOBOT (Crypto Pay API)
# ==========================
# Документация: https://help.send.tg/en/articles/10279948-crypto-pay-api

CRYPTOBOT_API_BASE = "https://pay.crypt.bot/api"


async def _cryptobot_request(method: str, params: dict) -> Optional[dict]:
    headers = {"Crypto-Pay-API-Token": CRYPTOBOT_API_TOKEN}
    url = f"{CRYPTOBOT_API_BASE}/{method}"
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.post(url, headers=headers, data=params) as resp:
                data = await resp.json()
    except Exception:
        logger.exception("CryptoBot API request failed: %s", method)
        return None

    if not data.get("ok"):
        logger.warning("CryptoBot API error in %s: %s", method, data.get("error"))
        return None
    return data.get("result")


async def create_cryptobot_invoice(amount_usd: float, description: str, payload: str) -> Optional[dict]:
    """Создаёт инвойс на amount_usd долларов (currency_type=fiat, fiat=USD —
    сумма в долларах, платит пользователь в любой поддерживаемой крипте по
    актуальному курсу CryptoBot). Возвращает {"invoice_id", "pay_url"} либо
    None при ошибке."""
    result = await _cryptobot_request(
        "createInvoice",
        {
            "currency_type": "fiat",
            "fiat": "USD",
            "amount": f"{amount_usd:.2f}",
            "description": description[:1024],
            "payload": payload[:4000],
            "expires_in": INVOICE_EXPIRES_IN,
        },
    )
    if result is None:
        return None

    pay_url = result.get("bot_invoice_url") or result.get("pay_url")
    invoice_id = result.get("invoice_id")
    if not pay_url or invoice_id is None:
        return None
    return {"invoice_id": str(invoice_id), "pay_url": pay_url}


async def get_cryptobot_invoice_status(invoice_id: str) -> Optional[str]:
    """Возвращает статус инвойса: "active" / "paid" / "expired", либо None
    при ошибке запроса."""
    result = await _cryptobot_request("getInvoices", {"invoice_ids": invoice_id})
    if not result:
        return None
    items = result.get("items") if isinstance(result, dict) else result
    if not items:
        return None
    return items[0].get("status")


# ==========================
#   XROCKET (Rocket Pay API)
# ==========================
# Рабочая схема — та же, что подтверждена и используется в donate.py:
#   - базовый адрес БЕЗ префикса /api/v1: https://pay.xrocket.tg
#   - эндпоинты "/tg-invoices" и "/tg-invoices/{id}" (а не "/invoices")
#   - авторизация ТОЛЬКО заголовком "Rocket-Pay-Key: <token>" (без Bearer)
#   - тело ответа — {"data": {...}}, без обёртки "success"
# Прежняя версия этого файла пробовала несуществующий путь "/invoices",
# домен с "/api/v1" и Bearer-авторизацию — из-за этого инвойсы не
# создавались. Ничего этого больше не перебираем, используем то, что
# реально работает.

XROCKET_API_BASE = "https://pay.xrocket.tg"

# Валюта, в которой создаётся инвойс у xRocket — USDT практически равен
# 1 USD, поэтому используем сумму в USD как есть.
XROCKET_INVOICE_CURRENCY = "USDT"


async def _xrocket_request(http_method: str, path: str, json_body: Optional[dict] = None) -> Optional[dict]:
    """Запрос к Rocket Pay API. Возвращает содержимое поля "data" из
    ответа, либо None при сетевой ошибке / HTTP-ошибке / отсутствии
    "data" в ответе."""
    url = f"{XROCKET_API_BASE}{path}"
    headers = {"Rocket-Pay-Key": XROCKET_API_TOKEN}
    try:
        async with aiohttp.ClientSession(timeout=_HTTP_TIMEOUT) as session:
            async with session.request(http_method, url, headers=headers, json=json_body) as resp:
                status = resp.status
                data = await resp.json()
    except Exception:
        logger.exception("xRocket API request failed: %s %s", http_method, url)
        return None

    if status >= 400:
        logger.warning("xRocket API error (status %s) on %s %s: %s", status, http_method, url, data)
        return None

    result = data.get("data")
    if not result:
        logger.warning("xRocket API: no 'data' in response on %s %s: %s", http_method, url, data)
        return None
    return result


async def create_xrocket_invoice(amount_usd: float, description: str, payload: str) -> Optional[dict]:
    """Создаёт инвойс на amount_usd (в USDT ~= USD). Возвращает
    {"invoice_id", "pay_url"} либо None при ошибке."""
    result = await _xrocket_request(
        "POST",
        "/tg-invoices",
        {
            "amount": round(amount_usd, 2),
            "currency": XROCKET_INVOICE_CURRENCY,
            "description": description[:1000],
            "payload": payload[:4000],
            "expiredIn": INVOICE_EXPIRES_IN,
            "numPayments": 1,  # обязательное поле API
        },
    )
    if result is None:
        return None

    pay_url = result.get("link") or result.get("payLink") or result.get("url")
    invoice_id = result.get("id")
    if not pay_url or invoice_id is None:
        return None
    return {"invoice_id": str(invoice_id), "pay_url": pay_url}


async def get_xrocket_invoice_status(invoice_id: str) -> Optional[str]:
    """Возвращает статус инвойса: "active" / "paid" / "expired", либо None
    при ошибке запроса. xRocket помечает оплаченные инвойсы либо статусом
    "paid", либо флагом paid=true в зависимости от версии API — проверяем
    оба варианта на всякий случай."""
    result = await _xrocket_request("GET", f"/tg-invoices/{invoice_id}")
    if not result:
        return None

    status = result.get("status")
    if status:
        return str(status).lower()
    if result.get("paid") is True:
        return "paid"
    return "active"
