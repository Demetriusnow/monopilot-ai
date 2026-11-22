"""Monobank CSV and generic banking statement CSV importer."""

from __future__ import annotations

import csv
import hashlib
import io
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.data.database import (
    clear_transactions,
    get_transaction_stats,
    init_db,
    set_sync_meta,
    upsert_transactions,
)
from app.data.mcc_catalog import resolve_mcc_category
from app.schemas import TransactionRecord

_KYIV_TZ = timezone(timedelta(hours=2))

_DATE_FORMATS: tuple[str, ...] = (
    "%d.%m.%Y %H:%M:%S",
    "%d.%m.%Y %H:%M",
    "%d.%m.%Y",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y-%m-%d",
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y",
)

_CYRILLIC_TO_LATIN_I = str.maketrans({"і": "i", "ї": "i", "є": "e"})


def _decode_csv_bytes(raw_bytes: bytes) -> str:
    """Decode uploaded CSV bytes trying UTF-8 BOM, UTF-8, and Windows-1251."""
    if not raw_bytes:
        raise ValueError("Uploaded CSV file is empty.")
    for encoding in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            text = raw_bytes.decode(encoding)
            if text.strip():
                return text
        except UnicodeDecodeError:
            continue
    raise ValueError("Unable to decode CSV file. Supported encodings: UTF-8 and Windows-1251.")


def _detect_delimiter(sample_text: str) -> str:
    """Sniff CSV delimiter safely among comma, semicolon, and tab."""
    lines = [line for line in sample_text.splitlines() if line.strip()][:10]
    sample = "\n".join(lines)
    if not sample:
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        if dialect.delimiter in {",", ";", "\t"}:
            return dialect.delimiter
    except csv.Error:
        pass
    first_line = lines[0]
    if first_line.count(";") > first_line.count(","):
        return ";"
    if first_line.count("\t") > first_line.count(","):
        return "\t"
    return ","


def _normalize_header(raw_header: str) -> str:
    """Normalize header names for resilient Ukrainian and English column matching."""
    cleaned = (raw_header or "").strip().lower()
    cleaned = cleaned.replace("\u00a0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned


def _find_column(headers: list[str], mode: str) -> str | None:
    """Locate matching CSV header column by semantic role."""
    normalized = [(h, _normalize_header(h)) for h in headers if h is not None]

    if mode == "date":
        for orig, norm in normalized:
            norm_ascii = norm.translate(_CYRILLIC_TO_LATIN_I)
            if "дата" in norm or norm_ascii in {"date", "tx_date", "datetime", "time", "timestamp"}:
                return orig
        for orig, norm in normalized:
            if "date" in norm:
                return orig

    elif mode == "description":
        for orig, norm in normalized:
            if any(token in norm for token in ("деталі", "опис", "призначення")):
                return orig
            if norm in {"description", "merchant", "details", "name", "title"}:
                return orig

    elif mode == "mcc":
        for orig, norm in normalized:
            if norm in {"mcc", "мсс"} or "mcc" in norm:
                return orig

    elif mode == "amount":
        for orig, norm in normalized:
            if "сума" in norm and "картк" in norm:
                return orig
            if norm in {"amount_uah", "amount", "sum_uah"}:
                return orig
        for orig, norm in normalized:
            if (
                "сума" in norm
                and "операц" not in norm
                and "кешбек" not in norm
                and "коміс" not in norm
            ) or norm in {"sum", "value"}:
                return orig

    elif mode == "operation_amount":
        for orig, norm in normalized:
            if ("сума" in norm and "операц" in norm) or norm in {
                "operation_amount",
                "operation_amount_uah",
            }:
                return orig

    elif mode == "cashback":
        for orig, norm in normalized:
            if "кешбек" in norm or "cashback" in norm:
                return orig

    elif mode == "balance":
        for orig, norm in normalized:
            if "залишок" in norm or "balance" in norm:
                return orig

    elif mode == "comment":
        for orig, norm in normalized:
            if "коментар" in norm or norm == "comment":
                return orig

    return None


def _parse_datetime_to_kyiv(raw_value: str) -> datetime:
    """Parse Monobank or ISO timestamp strings into timezone-aware Kyiv datetime."""
    cleaned = (raw_value or "").strip()
    if not cleaned:
        raise ValueError("Empty date value.")

    for fmt in _DATE_FORMATS:
        try:
            dt = datetime.strptime(cleaned, fmt)
            return dt.replace(tzinfo=_KYIV_TZ)
        except ValueError:
            continue

    iso_candidate = cleaned.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=_KYIV_TZ)
        return dt.astimezone(_KYIV_TZ)
    except ValueError as exc:
        raise ValueError(f"Unsupported date format: '{cleaned}'") from exc


def _parse_float(raw_value: str | None, *, required: bool = False) -> float | None:
    """Parse localized decimal string (supports commas and space thousands separators)."""
    if raw_value is None:
        if required:
            raise ValueError("Missing numeric value.")
        return None

    cleaned = raw_value.strip()
    cleaned = (
        cleaned.replace("\u00a0", "")
        .replace("\u202f", "")
        .replace(" ", "")
        .replace("₴", "")
    )
    cleaned = re.sub(r"(uah|грн)", "", cleaned, flags=re.IGNORECASE).strip()

    if cleaned in {"", "-", "—", "–"}:
        if required:
            raise ValueError(f"Missing numeric amount: '{raw_value}'")
        return None

    if "," in cleaned and "." in cleaned:
        if cleaned.rfind(",") > cleaned.rfind("."):
            cleaned = cleaned.replace(".", "").replace(",", ".")
        else:
            cleaned = cleaned.replace(",", "")
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        return float(cleaned)
    except ValueError as exc:
        if required:
            raise ValueError(f"Invalid numeric value: '{raw_value}'") from exc
        return None


def parse_monobank_csv(
    raw_bytes: bytes,
    account_id: str = "csv-import",
    source: str = "csv",
) -> list[TransactionRecord]:
    """Parse official Monobank CSV export or standard English CSV into TransactionRecord items."""
    text = _decode_csv_bytes(raw_bytes)
    delimiter = _detect_delimiter(text)
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)

    if not reader.fieldnames:
        raise ValueError("CSV header row not found.")

    headers = list(reader.fieldnames)
    date_col = _find_column(headers, "date")
    desc_col = _find_column(headers, "description")
    amount_col = _find_column(headers, "amount")
    mcc_col = _find_column(headers, "mcc")
    op_amount_col = _find_column(headers, "operation_amount")
    cashback_col = _find_column(headers, "cashback")
    balance_col = _find_column(headers, "balance")
    comment_col = _find_column(headers, "comment")

    missing = []
    if date_col is None:
        missing.append("date/дата")
    if desc_col is None:
        missing.append("description/деталі")
    if amount_col is None:
        missing.append("amount/сума")
    if missing:
        raise ValueError(
            f"Missing required CSV columns: {', '.join(missing)}. "
            f"Detected headers: {', '.join(h for h in headers if h)}"
        )

    records: list[TransactionRecord] = []
    duplicate_counts: dict[tuple[int, str, int], int] = defaultdict(int)

    for row in reader:
        if not row or not any((v or "").strip() for v in row.values()):
            continue

        raw_date = (row.get(date_col) or "").strip()
        raw_desc = (row.get(desc_col) or "").strip()
        raw_amount = row.get(amount_col)
        if not raw_date or not raw_desc or raw_amount is None or not str(raw_amount).strip():
            continue

        dt = _parse_datetime_to_kyiv(raw_date)
        amount_uah_raw = _parse_float(raw_amount, required=True)
        assert amount_uah_raw is not None
        amount_minor = int(round(amount_uah_raw * 100))
        amount_uah = round(amount_minor / 100.0, 2)

        op_amount_val = _parse_float(row.get(op_amount_col) if op_amount_col else None)
        operation_minor = (
            int(round(op_amount_val * 100)) if op_amount_val is not None else amount_minor
        )

        cashback_val = _parse_float(row.get(cashback_col) if cashback_col else None)
        cashback_uah = round(abs(cashback_val), 2) if cashback_val is not None else 0.0

        balance_val = _parse_float(row.get(balance_col) if balance_col else None)
        balance_uah = round(balance_val, 2) if balance_val is not None else 0.0

        mcc = 0
        if mcc_col:
            raw_mcc = re.sub(r"\D", "", str(row.get(mcc_col) or ""))
            if raw_mcc:
                mcc = int(raw_mcc)

        comment = (row.get(comment_col) or "").strip() if comment_col else ""
        ts = int(dt.timestamp())
        dup_key = (ts, raw_desc, amount_minor)
        occ_idx = duplicate_counts[dup_key]
        duplicate_counts[dup_key] += 1

        digest = hashlib.sha256(
            f"{ts}|{raw_desc}|{amount_minor}|{occ_idx}".encode("utf-8")
        ).hexdigest()

        category = resolve_mcc_category(mcc, description=raw_desc, amount_minor=amount_minor)

        records.append(
            TransactionRecord(
                id=f"csv_{digest[:16]}",
                account_id=account_id,
                source=source,
                timestamp=ts,
                tx_date=dt.strftime("%Y-%m-%d"),
                tx_month=dt.strftime("%Y-%m"),
                description=raw_desc,
                mcc=mcc,
                category=category,
                amount_minor=amount_minor,
                amount_uah=amount_uah,
                operation_amount_minor=operation_minor,
                currency_code=980,
                cashback_uah=cashback_uah,
                balance_uah=balance_uah,
                is_expense=amount_minor < 0,
                comment=comment,
            )
        )

    if not records:
        raise ValueError("No valid transaction rows were parsed from the uploaded CSV.")

    return records


def import_csv_transactions(
    raw_bytes: bytes,
    replace_existing: bool = True,
    db_path: str | None = None,
) -> tuple[int, int]:
    """Parse uploaded CSV transactions, store them in SQLite, and return (imported, total)."""
    records = parse_monobank_csv(raw_bytes)
    init_db(db_path=db_path)
    if replace_existing:
        clear_transactions(db_path=db_path)
    imported_count = upsert_transactions(records, db_path=db_path)
    set_sync_meta("active_mode", "csv", db_path=db_path)
    stats = get_transaction_stats(db_path=db_path)
    return imported_count, int(stats.get("total_transactions", imported_count))
