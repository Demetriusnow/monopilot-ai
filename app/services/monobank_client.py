"""Monobank Open API client for currency rates, client info, Jars, and personal card statements."""

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
    prepare_monobank_account_storage,
    replace_jars_with_monobank,
    set_sync_meta,
    upsert_transactions,
)
from app.data.mcc_catalog import resolve_mcc_category
from app.schemas import CurrencyRate, MonobankAccountOption, TransactionRecord

ISO_CURRENCY_CODES: dict[int, str] = {
    980: "UAH",
    840: "USD",
    978: "EUR",
    985: "PLN",
    826: "GBP",
}

_CARD_TYPE_LABELS: dict[str, str] = {
    "black": "Чорна картка",
    "white": "Біла картка",
    "platinum": "Platinum картка",
    "iron": "IRON картка",
    "fop": "Рахунок ФОП",
    "yellow": "Дитяча картка",
    "eaid": "єПідтримка",
    "madeinukraine": "Національний кешбек",
}

_CARD_TYPE_PRIORITY: dict[str, int] = {
    "black": 0,
    "iron": 1,
    "platinum": 2,
    "white": 3,
    "fop": 4,
    "yellow": 5,
    "eaid": 8,
    "madeinukraine": 9,
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
_MONOBANK_CLIENT_INFO_URL = "https://api.monobank.ua/personal/client-info"
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
            currency_b="UAH" if int(code_b) == 980 else cur_b,
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


def _format_account_label(acc: dict[str, Any]) -> tuple[str, str, float, int]:
    """Return (display_label, ccy_str, own_balance_uah, credit_limit_minor) for a Monobank account."""
    ccy_code = int(acc.get("currencyCode", 980))
    ccy = ISO_CURRENCY_CODES.get(ccy_code, str(ccy_code))
    raw_type = str(acc.get("type") or "black").strip()
    type_label = _CARD_TYPE_LABELS.get(raw_type.lower(), f"Картка {raw_type}")

    pans = acc.get("maskedPan") or []
    suffix = ""
    if isinstance(pans, list) and pans:
        pan_str = str(pans[0]).strip()
        if len(pan_str) >= 4:
            suffix = f" •• {pan_str[-4:]}"
    elif acc.get("iban"):
        iban_str = str(acc.get("iban")).strip()
        if len(iban_str) >= 4:
            suffix = f" •• {iban_str[-4:]}"

    balance_minor = int(acc.get("balance", 0))
    credit_limit_minor = int(acc.get("creditLimit", 0))
    own_balance = round((balance_minor - credit_limit_minor) / 100.0, 2)
    label = f"{type_label} ({ccy}{suffix})"
    return label, ccy, own_balance, credit_limit_minor


async def fetch_monobank_client_info(token: str) -> dict[str, Any]:
    """Fetch client profile, accounts, and Jars from Monobank /personal/client-info."""
    cleaned_token = (token or "").strip()
    if not cleaned_token:
        raise MonobankAuthError("Monobank API token is required.")

    async with httpx.AsyncClient(timeout=12.0) as client:
        response = await client.get(
            _MONOBANK_CLIENT_INFO_URL,
            headers={"X-Token": cleaned_token},
        )

    if response.status_code in (401, 403):
        raise MonobankAuthError("Недійсний токен Monobank (HTTP 401/403). Перевірте токен з api.monobank.ua.")

    if response.status_code == 429:
        raise MonobankRateLimitError("Забагато запитів до Monobank API (HTTP 429). Спробуйте через хвилину.")

    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("Unexpected Monobank client-info format.")
    return data


def _select_monobank_account(
    accounts: list[dict[str, Any]],
    requested_account_id: str,
) -> tuple[str, dict[str, Any] | None, list[MonobankAccountOption]]:
    """Resolve requested or primary UAH Monobank account and build selectable options list."""
    options: list[MonobankAccountOption] = []
    by_id: dict[str, dict[str, Any]] = {}

    sorted_raw = sorted(
        [a for a in accounts if isinstance(a, dict) and a.get("id")],
        key=lambda a: (
            0 if int(a.get("currencyCode", 980)) == 980 else 1,
            _CARD_TYPE_PRIORITY.get(str(a.get("type") or "").lower(), 6),
        ),
    )

    for acc in sorted_raw:
        acc_id = str(acc["id"])
        by_id[acc_id] = acc
        label, ccy, own_bal, _ = _format_account_label(acc)
        options.append(
            MonobankAccountOption(
                account_id=acc_id,
                label=label,
                currency=ccy,
                balance_uah=own_bal,
                card_type=str(acc.get("type") or "black"),
            )
        )

    req = (requested_account_id or "0").strip()
    if req and req not in ("0", "auto") and req in by_id:
        return req, by_id[req], options

    # Auto-pick best UAH account: prefer active card with non-zero balance or credit activity, else top priority UAH card
    uah_candidates = [a for a in sorted_raw if int(a.get("currencyCode", 980)) == 980]
    if uah_candidates:
        active_uah = [
            a
            for a in uah_candidates
            if str(a.get("type") or "").lower() not in ("eaid", "madeinukraine")
            and (
                (int(a.get("balance", 0)) - int(a.get("creditLimit", 0))) != 0
                or int(a.get("balance", 0)) != 0
            )
        ]
        chosen = active_uah[0] if active_uah else uah_candidates[0]
        return str(chosen["id"]), chosen, options

    if sorted_raw:
        return str(sorted_raw[0]["id"]), sorted_raw[0], options

    return req or "0", None, options


def _sync_jars_from_client_info(
    raw_jars: list[dict[str, Any]],
    db_path: str | None = None,
) -> int:
    """Import real Monobank Jars ('Банки') with exact ISO currency (UAH/USD/EUR) into SQLite."""
    valid_rows: list[tuple[str, str, str, str, float, float, float]] = []
    for raw in raw_jars:
        if not isinstance(raw, dict):
            continue
        jar_id = str(raw.get("id") or "").strip()
        title = str(raw.get("title") or "").strip()
        if not jar_id or not title:
            continue

        ccy_code = int(raw.get("currencyCode") or 980)
        ccy = ISO_CURRENCY_CODES.get(ccy_code, "UAH")

        current_val = max(0.0, round(int(raw.get("balance") or 0) / 100.0, 2))
        goal_minor = raw.get("goal")
        default_goal = 10000.0 if ccy == "UAH" else 1000.0
        if goal_minor is not None and int(goal_minor) > 0:
            target_val = round(int(goal_minor) / 100.0, 2)
        else:
            target_val = max(round(current_val * 2.0, 2), default_goal)

        min_monthly = 500.0 if ccy == "UAH" else 25.0
        fallback_monthly = 2000.0 if ccy == "UAH" else 100.0
        monthly_est = (
            max(min_monthly, round((target_val - current_val) / 6.0, 0))
            if target_val > current_val
            else fallback_monthly
        )
        valid_rows.append((f"mono_{jar_id}", title, "🫙", ccy, target_val, current_val, monthly_est))

    replace_jars_with_monobank(valid_rows, db_path=db_path)
    return len(valid_rows)


async def list_monobank_accounts(token: str) -> list[MonobankAccountOption]:
    """Fetch selectable Monobank cards/accounts from /personal/client-info without triggering 60s lock."""
    info = await fetch_monobank_client_info(token)
    _, _, options = _select_monobank_account(info.get("accounts") or [], "0")
    return options


def _normalize_monobank_item(
    item: dict[str, Any],
    account_id: str,
    credit_limit_minor: int = 0,
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
    raw_balance_minor = int(item.get("balance", 0))
    own_balance_uah = round((raw_balance_minor - credit_limit_minor) / 100.0, 2)

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
        balance_uah=own_balance_uah,
        is_expense=amount_minor < 0,
        comment=str(item.get("comment") or ""),
    )


async def sync_monobank_statement(
    token: str,
    account_id: str = "0",
    days: int = 31,
    db_path: str | None = None,
) -> tuple[int, int]:
    """Fetch client-info + personal statement from Monobank API, clear demo data, and upsert into SQLite."""
    cleaned_token = (token or "").strip()
    if not cleaned_token:
        raise MonobankAuthError("Monobank API token is required.")

    if not (1 <= days <= 31):
        raise ValueError("days must be between 1 and 31.")

    init_db(db_path=db_path)

    # 1. Always fetch live profile, accounts, card balance, and real Jars first
    #    (/personal/client-info does NOT share the 60-second /personal/statement lock)
    client_name = ""
    resolved_account_id = (account_id or "0").strip() or "0"
    card_label = "Гривня (UAH · Картка Monobank)"
    own_balance_uah: float | None = None
    credit_limit_minor = 0
    options: list[MonobankAccountOption] = []

    try:
        client_info = await fetch_monobank_client_info(cleaned_token)
        client_name = str(client_info.get("name") or "").strip()
        resolved_account_id, chosen_acc, options = _select_monobank_account(
            client_info.get("accounts") or [],
            resolved_account_id,
        )
        if chosen_acc is not None:
            card_label, _, own_balance_uah, credit_limit_minor = _format_account_label(chosen_acc)
        if isinstance(client_info.get("jars"), list):
            _sync_jars_from_client_info(client_info["jars"], db_path=db_path)
    except (MonobankAuthError, MonobankRateLimitError):
        raise
    except Exception:
        pass

    # Check 60s statement cooldown: if same account was already synced <60s ago, save refreshed
    # client-info & Jars immediately and return existing statement count without hitting HTTP 429
    last_sync_raw = get_sync_meta("last_mono_sync", db_path=db_path)
    if last_sync_raw is not None:
        try:
            elapsed = time.time() - float(last_sync_raw)
        except ValueError:
            elapsed = _MONO_COOLDOWN_SEC + 1
        if elapsed < _MONO_COOLDOWN_SEC:
            prev_meta_raw = get_sync_meta("mono_account_info", db_path=db_path)
            prev_acc_id = None
            if prev_meta_raw:
                try:
                    prev_acc_id = json.loads(prev_meta_raw).get("account_id")
                except (json.JSONDecodeError, TypeError, ValueError):
                    prev_acc_id = None
            current_stats = get_transaction_stats(db_path=db_path)
            if current_stats.get("active_mode") == "monobank" and prev_acc_id == resolved_account_id:
                account_meta = {
                    "client_name": client_name,
                    "account_id": resolved_account_id,
                    "card_label": card_label,
                    "own_balance_uah": own_balance_uah if own_balance_uah is not None else 0.0,
                    "credit_limit_uah": round(credit_limit_minor / 100.0, 2),
                    "accounts": [opt.model_dump() for opt in options],
                }
                set_sync_meta("mono_account_info", json.dumps(account_meta, ensure_ascii=False), db_path=db_path)
                return int(current_stats["total_transactions"]), int(current_stats["total_transactions"])

            wait_sec = max(1, int(_MONO_COOLDOWN_SEC - elapsed) + 1)
            raise MonobankRateLimitError(
                f"Ліміт Monobank API на виписку (1 запит на 60 сек): зачекайте ще {wait_sec} сек."
            )

    # 2. Fetch statement for the resolved account
    to_ts = int(time.time())
    from_ts = to_ts - (days * 86400)
    url = f"{_MONOBANK_STATEMENT_URL}/{resolved_account_id}/{from_ts}/{to_ts}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, headers={"X-Token": cleaned_token})

    if response.status_code in (401, 403):
        raise MonobankAuthError("Недійсний токен Monobank (HTTP 401/403).")

    if response.status_code == 429:
        raise MonobankRateLimitError(
            "Перевищено ліміт запитів Monobank API (HTTP 429). Зачекайте 60 секунд і спробуйте знову."
        )

    response.raise_for_status()
    raw_items = response.json()
    if not isinstance(raw_items, list):
        raise ValueError("Unexpected Monobank statement response format.")

    records = [
        _normalize_monobank_item(
            item,
            account_id=resolved_account_id,
            credit_limit_minor=credit_limit_minor,
        )
        for item in raw_items
    ]

    # 3. Replace demo/CSV/other-card rows so demo transactions NEVER pollute live Monobank data
    prepare_monobank_account_storage(resolved_account_id, db_path=db_path)
    synced_count = upsert_transactions(records, db_path=db_path)

    if own_balance_uah is None and records:
        sorted_recs = sorted(records, key=lambda r: r.timestamp, reverse=True)
        own_balance_uah = sorted_recs[0].balance_uah

    account_meta = {
        "client_name": client_name,
        "account_id": resolved_account_id,
        "card_label": card_label,
        "own_balance_uah": own_balance_uah if own_balance_uah is not None else 0.0,
        "credit_limit_uah": round(credit_limit_minor / 100.0, 2),
        "accounts": [opt.model_dump() for opt in options],
    }
    set_sync_meta("mono_account_info", json.dumps(account_meta, ensure_ascii=False), db_path=db_path)
    set_sync_meta("last_mono_sync", str(int(time.time())), db_path=db_path)
    set_sync_meta("active_mode", "monobank", db_path=db_path)

    stats = get_transaction_stats(db_path=db_path)
    return synced_count, int(stats["total_transactions"])
