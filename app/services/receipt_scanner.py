"""Multimodal Gemini Vision receipt scanner and bank transaction reconciler."""

from __future__ import annotations

import asyncio
import base64
import logging
import re
import time
import uuid
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from google import genai

from app.config import get_settings
from app.data.database import (
    get_transactions,
    save_receipt_record,
    upsert_transactions,
)
from app.data.mcc_catalog import MCC_CATEGORIES, resolve_mcc_category
from app.schemas import (
    MatchedTransactionSummary,
    ReceiptExtractionPayload,
    ReceiptLineItem,
    ReceiptScanResponse,
    TransactionRecord,
)

logger = logging.getLogger(__name__)

_KYIV_TZ = timezone(timedelta(hours=2))
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_ALLOWED_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}
_PLACEHOLDER_KEYS = {"", "your_gemini_api_key_here", "changeme", "none", "null"}

_KNOWN_CATEGORIES: frozenset[str] = frozenset(MCC_CATEGORIES.values()) | {
    "Дохід та поповнення",
    "Інше",
}

_STOP_TOKENS: frozenset[str] = frozenset(
    {
        "тов",
        "пп",
        "фоп",
        "тзов",
        "ат",
        "пат",
        "прат",
        "ltd",
        "llc",
        "inc",
        "фуд",
        "food",
        "маркет",
        "market",
        "магазин",
        "shop",
        "store",
        "ua",
        "чек",
        "м",
        "вул",
    }
)

_MERCHANT_ALIAS_GROUPS: tuple[frozenset[str], ...] = (
    frozenset({"сільпо", "silpo"}),
    frozenset({"атб", "atb"}),
    frozenset({"фора", "fora"}),
    frozenset({"novus", "новус"}),
    frozenset({"ашан", "auchan"}),
    frozenset({"розетка", "rozetka"}),
    frozenset({"аврора", "avrora"}),
    frozenset({"варус", "varus"}),
    frozenset({"метро", "metro"}),
    frozenset({"окко", "okko"}),
    frozenset({"wog", "вог"}),
    frozenset({"glovo", "глово"}),
    frozenset({"mcdonald", "макдональдз", "макдональдс"}),
    frozenset({"алло", "allo"}),
    frozenset({"comfy", "комфі"}),
    frozenset({"пошта", "poshta"}),
)

_TOKEN_TO_ALIASES: dict[str, frozenset[str]] = {
    token: group
    for group in _MERCHANT_ALIAS_GROUPS
    for token in group
}

VISION_PROMPT = """Проаналізуй зображення фіскального або торгового чека та витягни структуровані дані у JSON.
Правила:
1. `merchant`: коротка зрозуміла назва магазину/сервісу (наприклад, `"Сільпо"`, `"АТБ"`, `"Novus"`, `"Glovo"`, `"Rozetka"`). Якщо надруковано юридичну назву на кшталт `ТОВ "СІЛЬПО-ФУД"`, поверни `"Сільпо"`.
2. `receipt_date`: дата покупки строго у форматі `YYYY-MM-DD` (перетвори DD.MM.YYYY у YYYY-MM-DD).
3. `total_uah`: фінальна сума до сплати у гривнях (число з десятковою крапкою).
4. `mcc_guess`: найточніший 4-значний код MCC (наприклад, 5411 для супермаркетів/продуктів, 5812/5814 для кафе/ресторанів, 5912 для аптек, 5541 для АЗС, 5732 для електроніки).
5. `category`: категорія українською (`Продукти`, `Кафе та ресторани`, `Транспорт та авто`, `Аптеки та здоров'я`, `Техніка та електроніка`, `Одяг та взуття`, `Покупки та хобі`, `Зв'язок та інтернет`, `Пошта та доставка`, `Підписки та сервіси`, `Спорт та фітнес`, `Інше`).
6. `items`: список позицій чека (`name`, `quantity`, `price_uah` — підсумкова ціна позиції в грн).
"""


def _is_missing_api_key(api_key: str | None) -> bool:
    """Return True when Gemini API key is absent or still set to a template placeholder."""
    normalized = (api_key or "").strip()
    if normalized.lower() in _PLACEHOLDER_KEYS:
        return True
    return normalized.lower().startswith("your_")


def _normalize_mime_type(mime_type: str) -> str:
    """Normalize and validate an uploaded receipt image MIME type."""
    normalized = (mime_type or "").split(";")[0].strip().lower()
    if normalized == "image/jpg":
        normalized = "image/jpeg"
    if normalized not in _ALLOWED_MIME_TYPES:
        raise ValueError(
            f"Непідтримуваний формат файлу '{mime_type}'. Дозволено лише PNG, JPEG або WebP."
        )
    return normalized


def _parse_date_safe(raw_date: str) -> date:
    """Parse ISO or Ukrainian receipt date string into a date, falling back to today."""
    cleaned = (raw_date or "").strip()
    if not cleaned:
        return date.today()

    iso_prefix = cleaned[:10]
    try:
        return date.fromisoformat(iso_prefix)
    except ValueError:
        pass

    for fmt in ("%d.%m.%Y", "%d/%m/%Y", "%Y.%m.%d", "%Y/%m/%d", "%d-%m-%Y"):
        try:
            return datetime.strptime(cleaned[:10], fmt).date()
        except ValueError:
            continue

    match = re.search(r"\b(\d{2})[./-](\d{2})[./-](\d{4})\b", cleaned)
    if match:
        day_s, month_s, year_s = match.groups()
        try:
            return date(int(year_s), int(month_s), int(day_s))
        except ValueError:
            pass

    iso_match = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", cleaned)
    if iso_match:
        year_s, month_s, day_s = iso_match.groups()
        try:
            return date(int(year_s), int(month_s), int(day_s))
        except ValueError:
            pass

    return date.today()


def _normalize_receipt_date(raw_date: str) -> str:
    """Normalize receipt date string strictly to YYYY-MM-DD."""
    return _parse_date_safe(raw_date).isoformat()


def _extract_merchant_tokens(text: str) -> set[str]:
    """Extract normalized alphanumeric merchant tokens, excluding legal/generic noise."""
    raw_tokens = re.findall(r"[a-zа-яіїєґ0-9]+", (text or "").lower())
    meaningful = {
        tok
        for tok in raw_tokens
        if len(tok) >= 3 and not tok.isdigit() and tok not in _STOP_TOKENS
    }
    if meaningful:
        return meaningful
    return {tok for tok in raw_tokens if len(tok) >= 2 and not tok.isdigit()}


def _is_merchant_match(merchant: str, tx_description: str) -> bool:
    """Return True when receipt merchant tokens overlap with a transaction description."""
    merchant_tokens = _extract_merchant_tokens(merchant)
    if not merchant_tokens:
        return False

    desc_lower = (tx_description or "").lower()
    desc_tokens = _extract_merchant_tokens(desc_lower)

    expanded_merchant: set[str] = set()
    for token in merchant_tokens:
        expanded_merchant.add(token)
        expanded_merchant.update(_TOKEN_TO_ALIASES.get(token, ()))

    if expanded_merchant & desc_tokens:
        return True

    return any(tok in desc_lower for tok in expanded_merchant if len(tok) >= 3)


def _strip_json_fences(raw_text: str) -> str:
    """Remove accidental Markdown ```json code fences surrounding JSON output."""
    cleaned = (raw_text or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def _step_to_dict(step: Any) -> dict[str, Any]:
    """Convert an Interactions API step model, mapping, or mock object into a dict."""
    if hasattr(step, "model_dump"):
        try:
            dumped = step.model_dump(exclude_none=True)
        except TypeError:
            dumped = step.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(step, Mapping):
        return dict(step)
    out: dict[str, Any] = {}
    for field in ("type", "content", "text"):
        if hasattr(step, field):
            val = getattr(step, field)
            if val is not None:
                out[field] = val
    return out


def _extract_interaction_text(interaction: Any) -> str:
    """Extract text payload from Interaction.output_text or trailing model_output steps."""
    output_text = getattr(interaction, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    steps = getattr(interaction, "steps", None) or []
    parts: list[str] = []
    collecting = False
    for raw_step in reversed(steps):
        step = _step_to_dict(raw_step)
        step_type = step.get("type")
        if step_type == "user_input":
            break
        if step_type == "thought":
            continue
        if step_type != "model_output":
            if collecting:
                break
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for item in reversed(content):
            item_dict = dict(item) if isinstance(item, Mapping) else _step_to_dict(item)
            if item_dict.get("type") == "text":
                collecting = True
                text_val = item_dict.get("text")
                if isinstance(text_val, str):
                    parts.append(text_val)
            elif collecting:
                break
        if collecting:
            break
    return "".join(reversed(parts)).strip()


def _demo_receipt_payload() -> ReceiptExtractionPayload:
    """Return exact deterministic extraction matching app/static/demo-receipt.png."""
    return ReceiptExtractionPayload(
        merchant="Сільпо",
        receipt_date="2026-11-20",
        total_uah=1070.00,
        mcc_guess=5411,
        category="Продукти",
        items=[
            ReceiptLineItem(name="Кава Jacobs Monarch 200г", quantity=1.0, price_uah=289.00),
            ReceiptLineItem(name="Сир Брі Президент 125г", quantity=1.0, price_uah=195.50),
            ReceiptLineItem(name="Лосось філе охолоджений", quantity=1.0, price_uah=349.00),
            ReceiptLineItem(name="Авокадо Хасс (2 шт)", quantity=1.0, price_uah=138.00),
            ReceiptLineItem(name="Багет французький", quantity=1.0, price_uah=42.50),
            ReceiptLineItem(name="Молоко Галичина 2.5%", quantity=1.0, price_uah=56.00),
        ],
    )


def reconcile_receipt_with_transactions(
    merchant: str,
    receipt_date: str,
    total_uah: float,
    db_path: str | None = None,
) -> MatchedTransactionSummary | None:
    """Match a scanned receipt against recent SQLite expense transactions."""
    transactions = get_transactions(limit=500, db_path=db_path)
    if not transactions:
        return None

    rc_d = _parse_date_safe(receipt_date)
    target_total = abs(float(total_uah))

    best_key: tuple[float, float, int, int] | None = None
    best_match: MatchedTransactionSummary | None = None

    for tx in transactions:
        if not tx.is_expense:
            continue
        try:
            tx_d = date.fromisoformat(tx.tx_date[:10])
        except ValueError:
            continue

        date_diff = abs((tx_d - rc_d).days)
        if date_diff > 14:
            continue

        tx_amount = round(abs(tx.amount_uah), 2)
        amt_diff_rel = abs(tx_amount - target_total) / max(target_total, 1.0)
        merchant_matched = _is_merchant_match(merchant, tx.description)

        score = 0.0
        reasons: list[str] = []

        if amt_diff_rel <= 0.03 and date_diff <= 4:
            score += 0.55
            reasons.append("точна сума" if amt_diff_rel <= 0.005 else "сума ±3%")
        elif amt_diff_rel <= 0.15:
            score += 0.35
            reasons.append("близька сума (±15%)")

        if merchant_matched:
            score += 0.40
            reasons.append("збіг мерчанта")

        if date_diff <= 2:
            score += 0.15
            reasons.append(f"дата ±{date_diff} дн.")
        elif date_diff <= 7:
            score += 0.08
            reasons.append(f"дата ±{date_diff} дн.")

        if score < 0.60:
            continue

        rank_key = (round(score, 6), -amt_diff_rel, -date_diff, tx.timestamp)
        if best_key is None or rank_key > best_key:
            best_key = rank_key
            best_match = MatchedTransactionSummary(
                tx_id=tx.id,
                tx_date=tx.tx_date,
                description=tx.description,
                amount_uah=tx_amount,
                confidence=round(min(1.0, score), 2),
                reason="Збіг: " + ", ".join(reasons),
            )

    return best_match


async def _extract_via_gemini_vision(
    raw_bytes: bytes,
    mime_type: str,
    api_key: str,
    client: Any = None,
) -> tuple[ReceiptExtractionPayload, str]:
    """Call Gemini Interactions API Vision with model failover and JSON schema validation."""
    settings = get_settings()
    candidate_models = [settings.gemini_model, *settings.fallback_model_list]
    if not candidate_models:
        candidate_models = ["gemini-3.8-flash"]

    active_client = client or genai.Client(
        api_key=api_key,
        http_options={
            "timeout": settings.gemini_timeout_ms,
            "retry_options": {
                "attempts": 1 if len(candidate_models) > 1 else 2,
                "initial_delay": 0.5,
                "max_delay": 2.0,
            },
        },
    )

    b64_image = base64.b64encode(raw_bytes).decode("ascii")
    schema = ReceiptExtractionPayload.model_json_schema()

    last_exc: Exception | None = None
    for idx, model_name in enumerate(candidate_models):
        try:
            interaction = await asyncio.to_thread(
                active_client.interactions.create,
                model=model_name,
                store=False,
                input=[
                    {
                        "type": "user_input",
                        "content": [
                            {"type": "text", "text": VISION_PROMPT},
                            {
                                "type": "image",
                                "data": b64_image,
                                "mime_type": mime_type,
                            },
                        ],
                    }
                ],
                response_format={
                    "type": "text",
                    "mime_type": "application/json",
                    "schema": schema,
                },
            )
            raw_text = _extract_interaction_text(interaction)
            cleaned_json = _strip_json_fences(raw_text)
            if not cleaned_json:
                raise ValueError(f"Empty Vision response from model '{model_name}'.")
            payload = ReceiptExtractionPayload.model_validate_json(cleaned_json)
            return payload, model_name
        except Exception as exc:
            last_exc = exc
            if idx + 1 < len(candidate_models):
                logger.warning(
                    "Gemini Vision model '%s' failed (%s); failing over to '%s'.",
                    model_name,
                    type(exc).__name__,
                    candidate_models[idx + 1],
                )

    assert last_exc is not None
    raise last_exc


def _resolve_receipt_category(payload: ReceiptExtractionPayload) -> str:
    """Normalize category via MCC catalog while preserving valid semantic overrides."""
    amount_minor = -int(round(abs(float(payload.total_uah)) * 100))
    resolved = resolve_mcc_category(
        int(payload.mcc_guess or 5411),
        payload.merchant,
        amount_minor,
    )
    if resolved != "Інше":
        return resolved
    raw_cat = (payload.category or "").strip()
    if raw_cat in _KNOWN_CATEGORIES:
        return raw_cat
    return resolved


async def scan_receipt_image(
    raw_bytes: bytes,
    mime_type: str = "image/png",
    api_key: str | None = None,
    auto_add: bool = False,
    is_demo: bool = False,
    db_path: str | None = None,
    client: Any = None,
) -> ReceiptScanResponse:
    """Scan a receipt image using Gemini Vision (or demo fallback) and reconcile with SQLite."""
    if not raw_bytes:
        raise ValueError("Файл зображення чека порожній.")
    if len(raw_bytes) > _MAX_IMAGE_BYTES:
        raise ValueError("Розмір зображення перевищує максимально допустимі 8 МБ.")

    normalized_mime = _normalize_mime_type(mime_type)
    started = time.perf_counter()

    settings = get_settings()
    effective_key = (api_key if api_key is not None else settings.gemini_api_key).strip()
    missing_key = _is_missing_api_key(effective_key)

    if client is None and missing_key and not is_demo:
        raise ValueError(
            "Для розпізнавання власного фото потрібен GEMINI_API_KEY (або скористайтеся демо-чеком Сільпо)."
        )

    used_fallback = False
    model_used = "local-vision-fallback"
    payload: ReceiptExtractionPayload

    if client is None and missing_key:
        payload = _demo_receipt_payload()
        used_fallback = True
    else:
        try:
            payload, model_used = await _extract_via_gemini_vision(
                raw_bytes=raw_bytes,
                mime_type=normalized_mime,
                api_key=effective_key,
                client=client,
            )
        except Exception as exc:
            if not is_demo:
                raise ValueError(
                    f"Не вдалося розпізнати чек через Gemini Vision: {exc}"
                ) from exc
            logger.warning(
                "All Gemini Vision models failed on demo receipt (%s); using local fallback.",
                type(exc).__name__,
            )
            payload = _demo_receipt_payload()
            used_fallback = True
            model_used = "local-vision-fallback"

    merchant = (payload.merchant or "").strip() or "Магазин"
    normalized_date = _normalize_receipt_date(payload.receipt_date)
    total_uah = round(abs(float(payload.total_uah)), 2)
    if total_uah <= 0 and payload.items:
        total_uah = round(sum(abs(float(item.price_uah)) for item in payload.items), 2)
    category = _resolve_receipt_category(payload)

    items = [
        ReceiptLineItem(
            name=(item.name or "").strip() or "Позиція",
            quantity=round(float(item.quantity or 1.0), 3),
            price_uah=round(abs(float(item.price_uah)), 2),
        )
        for item in payload.items
    ]

    matched_transaction = await asyncio.to_thread(
        reconcile_receipt_with_transactions,
        merchant=merchant,
        receipt_date=normalized_date,
        total_uah=total_uah,
        db_path=db_path,
    )

    receipt_id = uuid.uuid4().hex[:12]
    added_transaction_id: str | None = None

    if auto_add and matched_transaction is None and total_uah > 0:
        rc_d = _parse_date_safe(normalized_date)
        tx_dt = datetime(rc_d.year, rc_d.month, rc_d.day, 18, 0, tzinfo=_KYIV_TZ)
        amount_minor = -int(round(total_uah * 100))

        # Preserve realistic running balance if transactions already exist in SQLite.
        latest_txs = await asyncio.to_thread(get_transactions, limit=1, db_path=db_path)
        prev_balance = float(latest_txs[0].balance_uah) if latest_txs else 0.0
        new_balance = round(prev_balance - total_uah, 2) if latest_txs else 0.0

        new_tx_id = f"rcpt_{receipt_id}"
        new_tx = TransactionRecord(
            id=new_tx_id,
            account_id="0",
            source="receipt",
            timestamp=int(tx_dt.timestamp()),
            tx_date=normalized_date,
            tx_month=normalized_date[:7],
            description=merchant,
            mcc=int(payload.mcc_guess or 5411),
            category=category,
            amount_minor=amount_minor,
            amount_uah=-total_uah,
            operation_amount_minor=amount_minor,
            currency_code=980,
            cashback_uah=0.0,
            balance_uah=new_balance,
            is_expense=True,
            comment=f"Автододано зі скану чека ({receipt_id})",
        )
        await asyncio.to_thread(upsert_transactions, [new_tx], db_path=db_path)
        added_transaction_id = new_tx_id

    await asyncio.to_thread(
        save_receipt_record,
        {
            "id": receipt_id,
            "merchant": merchant,
            "receipt_date": normalized_date,
            "total_uah": total_uah,
            "category": category,
            "items": [item.model_dump() for item in items],
            "matched_tx_id": matched_transaction.tx_id if matched_transaction else None,
            "match_confidence": matched_transaction.confidence if matched_transaction else 0.0,
            "added_tx_id": added_transaction_id,
            "raw_model": model_used,
            "created_at": int(time.time()),
        },
        db_path=db_path,
    )

    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    return ReceiptScanResponse(
        receipt_id=receipt_id,
        merchant=merchant,
        receipt_date=normalized_date,
        total_uah=total_uah,
        category=category,
        items=items,
        matched_transaction=matched_transaction,
        added_transaction_id=added_transaction_id,
        model=model_used,
        used_fallback=used_fallback,
        latency_ms=latency_ms,
    )
