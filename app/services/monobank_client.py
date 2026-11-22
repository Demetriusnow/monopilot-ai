"""Monobank Open API client for currency rates and personal card statements."""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.data.database import (
    get_sync_meta,
    get_transaction_stats,
    init_db,
    set_sync_meta,
    upsert_transactions,
)
from app.data.mcc_catalog import resolve_mcc_category
from app.schemas import CurrencyRate, TransactionRecord

ISO_CURRENCY_CODES: dict[int, str] = {
    980: "UAH",
    840: "USD",
    978: "EUR",
    985: "PLN",
    826: "GBP",
}

_TARGET_PAIRS: tuple[tuple[int, int], ...] = (
    (840, 980),
    (978, 980),
    (985, 980),
    (826, 980),
    (978, 840),
)

_CURRENCY_CACHE_TTL_SEC = 300
_MONO_COOLDOWN_SEC = 60
_KYIV_TZ = timezone(timedelta(hours=2))

_MONOBANK_CURRENCY_URL = "https://api.monobank.ua/bank/currency"
_MONOBANK_STATEMENT_URL = "https://api.monobank.ua/personal/statement"


class MonobankRateLimitError(Exception):
    """Raised when Monobank API rate limit (60s cooldown or HTTP 429) is hit."""


class MonobankAuthError(Exception):
    """Raised when Monobank API rejects the token (HTTP 401/403 or missing token)."""


def _static_fallback_rates() -> list[CurrencyRate]:
    """Return sensible static UAH reference rates when offline and uncached."""
    now_ts = int(time.time())
    return [
        CurrencyRate(
            currency_a="USD",
            currency_b="UAH",
            pair="USD/UAH",
            rate_buy=41.20,
            rate_sell=41.75,
            rate_cross=None,
            updated_at=now_ts,
        ),
        CurrencyRate(
            currency_a="EUR",
            currency_b="UAH",
            pair="EUR/UAH",
            rate_buy=44.60,
            rate_sell=45.30,
            rate_cross=None,
            updated_at=now_ts,
        ),
        CurrencyRate(
            currency_a="PLN",
            currency_b="UAH",
            pair="PLN/UAH",
            rate_buy=None,
            rate_sell=None,
            rate_cross=10.35,
            updated_at=now_ts,
        ),
        CurrencyRate(
            currency_a="GBP",
            currency_b="UAH",
            pair="GBP/UAH",
            rate_buy=None,
            rate_sell=None,
            rate_cross=52.80,
            updated_at=now_ts,
        ),
    ]


def _load_cached_currency(
    db_path: str | None = None,
) -> tuple[int, list[CurrencyRate]] | None:
    """Load cached currency payload (cached_at, rates) from sync_meta if valid."""
    raw = get_sync_meta("currency_cache", db_path=db_path)
    if not raw:
        return None
    try:
        payload = json.loads(raw)
        cached_at = int(payload["cached_at"])
        rates = [CurrencyRate(**item) for item in payload["rates"]]
        if not rates:
            return None
        return cached_at, rates
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def _parse_currency_response(data: list[dict[str, Any]]) -> list[CurrencyRate]:
    """Filter and normalize Monobank public currency API items into CurrencyRate list."""
    pair_order = {pair: idx for idx, pair in enumerate(_TARGET_PAIRS)}
    matched: list[tuple[int, CurrencyRate]] = []

    for item in data:
        code_a = item.get("currencyCodeA")
        code_b = item.get("currencyCodeB")
        pair = (code_a, code_b)
        if pair not in pair_order:
            continue

        cur_a = ISO_CURRENCY_CODES.get(int(code_a), str(code_a))
        cur_b = ISO_CURRENCY_CODES.get(int(code_b), str(code_b))
        rate = CurrencyRate(
            currency_a=cur_a,
            currency_b=cur_b,
            pair=f"{cur_a}/{cur_b}",
            rate_buy=float(item["rateBuy"]) if item.get("rateBuy") is not None else None,
            rate_sell=float(item["rateSell"]) if item.get("rateSell") is not None else None,
            rate_cross=float(item["rateCross"]) if item.get("rateCross") is not None else None,
            updated_at=int(item.get("date", time.time())),
        )
        matched.append((pair_order[pair], rate))

    matched.sort(key=lambda entry: entry[0])
    return [entry[1] for entry in matched]


async def fetch_currency_rates(
    db_path: str | None = None,
    force_refresh: bool = False,
) -> list[CurrencyRate]:
    """Fetch key currency rates from Monobank API with 5-minute SQLite cache and offline fallback."""
    init_db(db_path=db_path)
    cached = _load_cached_currency(db_path=db_path)
    now_ts = int(time.time())

    if (
        not force_refresh
        and cached is not None
        and (now_ts - cached[0]) < _CURRENCY_CACHE_TTL_SEC
    ):
        return cached[1]

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(_MONOBANK_CURRENCY_URL)

        if response.status_code == 429 or response.status_code >= 400:
            return cached[1] if cached is not None else _static_fallback_rates()

        payload = response.json()
        if not isinstance(payload, list):
            return cached[1] if cached is not None else _static_fallback_rates()

        rates = _parse_currency_response(payload)
        if not rates:
            return cached[1] if cached is not None else _static_fallback_rates()

        cache_blob = json.dumps(
            {
                "cached_at": now_ts,
                "rates": [r.model_dump() for r in rates],
            },
            ensure_ascii=False,
        )
        set_sync_meta("currency_cache", cache_blob, db_path=db_path)
        return rates
    except (httpx.HTTPError, ValueError, TypeError):
        return cached[1] if cached is not None else _static_fallback_rates()


def _normalize_monobank_item(
    item: dict[str, Any],
    account_id: str,
) -> TransactionRecord:
    """Convert a raw Monobank statement JSON object into TransactionRecord."""
    ts = int(item["time"])
    dt = datetime.fromtimestamp(ts, tz=_KYIV_TZ)
    amount_minor = int(item["amount"])
    description = str(item.get("description", ""))
    mcc = int(item.get("mcc", 0))
    category = resolve_mcc_category(
        mcc,
        description=description,
        amount_minor=amount_minor,
    )

    return TransactionRecord(
        id=str(item["id"]),
        account_id=account_id,
        source="monobank",
        timestamp=ts,
        tx_date=dt.strftime("%Y-%m-%d"),
        tx_month=dt.strftime("%Y-%m"),
        description=description,
        mcc=mcc,
        category=category,
        amount_minor=amount_minor,
        amount_uah=round(amount_minor / 100.0, 2),
        operation_amount_minor=int(item.get("operationAmount", amount_minor)),
        currency_code=int(item.get("currencyCode", 980)),
        cashback_uah=round(int(item.get("cashbackAmount", 0)) / 100.0, 2),
        balance_uah=round(int(item.get("balance", 0)) / 100.0, 2),
        is_expense=amount_minor < 0,
        comment=str(item.get("comment") or ""),
    )


async def sync_monobank_statement(
    token: str,
    account_id: str = "0",
    days: int = 31,
    db_path: str | None = None,
) -> tuple[int, int]:
    """Fetch personal statement from Monobank API and upsert into SQLite."""
    if not token or not token.strip():
        raise MonobankAuthError("Monobank API token is required.")

    if not (1 <= days <= 31):
        raise ValueError("days must be between 1 and 31.")

    init_db(db_path=db_path)
    last_sync_raw = get_sync_meta("last_mono_sync", db_path=db_path)
    if last_sync_raw is not None:
        try:
            elapsed = time.time() - float(last_sync_raw)
        except ValueError:
            elapsed = _MONO_COOLDOWN_SEC + 1
        if elapsed < _MONO_COOLDOWN_SEC:
            wait_sec = max(1, int(_MONO_COOLDOWN_SEC - elapsed) + 1)
            raise MonobankRateLimitError(
                f"Monobank API cooldown active. Retry in {wait_sec} seconds."
            )

    to_ts = int(time.time())
    from_ts = to_ts - (days * 86400)
    url = f"{_MONOBANK_STATEMENT_URL}/{account_id}/{from_ts}/{to_ts}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers={"X-Token": token.strip()})

    if response.status_code in (401, 403):
        raise MonobankAuthError("Invalid or unauthorized Monobank X-Token.")

    if response.status_code == 429:
        raise MonobankRateLimitError(
            "Monobank API rate limit exceeded (HTTP 429). Wait 60 seconds before retrying."
        )

    response.raise_for_status()
    raw_items = response.json()
    if not isinstance(raw_items, list):
        raise ValueError("Unexpected Monobank statement response format.")

    records = [
        _normalize_monobank_item(item, account_id=account_id) for item in raw_items
    ]
    synced_count = upsert_transactions(records, db_path=db_path)
    set_sync_meta("last_mono_sync", str(int(time.time())), db_path=db_path)
    set_sync_meta("active_mode", "monobank", db_path=db_path)

    stats = get_transaction_stats(db_path=db_path)
    return synced_count, int(stats["total_transactions"])
