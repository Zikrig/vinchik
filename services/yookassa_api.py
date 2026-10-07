"""YooKassa payments. Subscriptions are a saved card plus our own renewal charge."""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

import httpx

from config import settings

logger = logging.getLogger(__name__)

API_PAYMENTS = "https://api.yookassa.ru/v3/payments"
_AMOUNT_RE = re.compile(r"(\d+(?:[.,]\d{1,2})?)")


class YooKassaError(RuntimeError):
    pass


def yookassa_configured() -> bool:
    return bool(settings.ukassa_shop_id.strip() and settings.ukassa_secret_key.strip())


def rub_amount(price_text: str) -> str | None:
    """Plan price as YooKassa '299.00'. Text without a number is not payable."""
    compact = (price_text or "").replace("\u00a0", "").replace(" ", "")
    match = _AMOUNT_RE.search(compact)
    if match is None:
        return None
    try:
        value = Decimal(match.group(1).replace(",", "."))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return f"{value.quantize(Decimal('0.01'))}"


def _auth() -> tuple[str, str]:
    return (settings.ukassa_shop_id.strip(), settings.ukassa_secret_key.strip())


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        desc = body.get("description") or body.get("code") or ""
        if desc:
            return str(desc)[:300]
    return (response.text or f"HTTP {response.status_code}")[:300]


async def _request(method: str, url: str, *, json_body: dict | None, idempotence_key: str | None) -> dict:
    if not yookassa_configured():
        raise YooKassaError("ЮKassa не настроена: нет UKASSA_ID или UKASSA_KEY.")
    headers = {"Content-Type": "application/json"}
    if idempotence_key:
        headers["Idempotence-Key"] = idempotence_key
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.request(
            method,
            url,
            json=json_body,
            headers=headers,
            auth=_auth(),
        )
    if response.status_code >= 400:
        logger.warning("yookassa %s %s -> %s", method, url, response.status_code)
        raise YooKassaError(_error_text(response))
    data = response.json()
    if not isinstance(data, dict):
        raise YooKassaError("Пустой ответ ЮKassa.")
    return data


def return_url() -> str:
    username = (settings.bot_username or "").strip().lstrip("@")
    if username:
        return f"https://t.me/{username}"
    return "https://yookassa.ru"


async def create_payment(
    *,
    amount: str,
    description: str,
    metadata: dict[str, str],
    idempotence_key: str,
    payment_method_id: str | None = None,
    save_payment_method: bool = False,
) -> dict:
    payload: dict = {
        "amount": {"value": amount, "currency": "RUB"},
        "capture": True,
        "description": description[:128],
        "metadata": metadata,
    }
    if payment_method_id:
        payload["payment_method_id"] = payment_method_id
    else:
        payload["confirmation"] = {"type": "redirect", "return_url": return_url()}
        payload["save_payment_method"] = save_payment_method
    return await _request(
        "POST",
        API_PAYMENTS,
        json_body=payload,
        idempotence_key=idempotence_key,
    )


async def get_payment(payment_id: str) -> dict:
    return await _request(
        "GET",
        f"{API_PAYMENTS}/{payment_id}",
        json_body=None,
        idempotence_key=None,
    )


def confirmation_url(payment: dict) -> str:
    confirmation = payment.get("confirmation") or {}
    return str(confirmation.get("confirmation_url") or "")


def new_idempotence_key() -> str:
    return str(uuid.uuid4())
