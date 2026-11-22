"""SQLite persistence and read-only SQL execution layer for MonoPilot AI."""

from __future__ import annotations

import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.schemas import TransactionRecord

DEFAULT_BUDGETS: dict[str, float] = {
    "Продукти": 9500.0,
    "Кафе та ресторани": 4500.0,
    "Транспорт та авто": 3200.0,
    "Підписки та сервіси": 1500.0,
    "Аптеки та здоров'я": 1500.0,
    "Зв'язок та інтернет": 700.0,
    "Пошта та доставка": 800.0,
    "Покупки та хобі": 3000.0,
    "Спорт та фітнес": 1600.0,
    "Одяг та взуття": 2500.0,
    "Техніка та електроніка": 4000.0,
    "Перекази": 3000.0,
}

_FORBIDDEN_SQL_KEYWORDS: tuple[str, ...] = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "DROP",
    "ALTER",
    "CREATE",
    "REPLACE",
    "ATTACH",
    "DETACH",
    "PRAGMA",
    "VACUUM",
    "REINDEX",
    "TRIGGER",
    "EXEC",
    "EXECUTE",
    "OUTFILE",
    "DUMPFILE",
    "LOAD_EXTENSION",
)

_FORBIDDEN_SQL_PATTERN = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_SQL_KEYWORDS) + r")\b",
    flags=re.IGNORECASE,
)


def _resolve_db_path(db_path: str | None = None) -> Path:
    """Resolve SQLite database path from explicit argument or current settings."""
    if db_path:
        return Path(db_path)
    return Path(get_settings().database_path)


def _connect_rw(db_path: str | None = None) -> sqlite3.Connection:
    """Open a read-write SQLite connection, ensuring the parent directory exists."""
    resolved = _resolve_db_path(db_path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(resolved))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_db_ready(db_path: str | None = None) -> Path:
    """Ensure the SQLite file exists and schema is initialized before queries."""
    resolved = _resolve_db_path(db_path)
    if not resolved.exists() or resolved.stat().st_size == 0:
        init_db(db_path=str(resolved))
        return resolved

    with closing(_connect_rw(str(resolved))) as conn:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'"
        ).fetchone()
    if row is None:
        init_db(db_path=str(resolved))
    return resolved


def _row_to_transaction(row: sqlite3.Row) -> TransactionRecord:
    """Convert a SQLite row into a TransactionRecord Pydantic model."""
    return TransactionRecord(
        id=str(row["id"]),
        account_id=str(row["account_id"]),
        source=str(row["source"]),
        timestamp=int(row["timestamp"]),
        tx_date=str(row["tx_date"]),
        tx_month=str(row["tx_month"]),
        description=str(row["description"]),
        mcc=int(row["mcc"]),
        category=str(row["category"]),
        amount_minor=int(row["amount_minor"]),
        amount_uah=float(row["amount_uah"]),
        operation_amount_minor=int(row["operation_amount_minor"]),
        currency_code=int(row["currency_code"]),
        cashback_uah=float(row["cashback_uah"]),
        balance_uah=float(row["balance_uah"]),
        is_expense=bool(row["is_expense"]),
        comment=str(row["comment"]),
    )


def init_db(db_path: str | None = None) -> None:
    """Create SQLite tables and indexes, and seed default budgets if empty."""
    with closing(_connect_rw(db_path)) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS transactions (
                id TEXT PRIMARY KEY,
                account_id TEXT NOT NULL,
                source TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                tx_date TEXT NOT NULL,
                tx_month TEXT NOT NULL,
                description TEXT NOT NULL,
                mcc INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount_minor INTEGER NOT NULL,
                amount_uah REAL NOT NULL,
                operation_amount_minor INTEGER NOT NULL,
                currency_code INTEGER NOT NULL,
                cashback_uah REAL NOT NULL,
                balance_uah REAL NOT NULL,
                is_expense INTEGER NOT NULL,
                comment TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_transactions_tx_date
                ON transactions(tx_date);
            CREATE INDEX IF NOT EXISTS idx_transactions_tx_month
                ON transactions(tx_month);
            CREATE INDEX IF NOT EXISTS idx_transactions_category
                ON transactions(category);
            CREATE INDEX IF NOT EXISTS idx_transactions_source
                ON transactions(source);

            CREATE TABLE IF NOT EXISTS sync_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS budgets (
                category TEXT PRIMARY KEY,
                monthly_limit_uah REAL NOT NULL
            );
            """
        )
        budget_count = conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
        conn.commit()

    if budget_count == 0:
        seed_default_budgets(db_path=db_path)


def seed_default_budgets(db_path: str | None = None) -> None:
    """Insert default Ukrainian monthly category budgets if missing."""
    with closing(_connect_rw(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS budgets (
                category TEXT PRIMARY KEY,
                monthly_limit_uah REAL NOT NULL
            )
            """
        )
        conn.executemany(
            """
            INSERT OR IGNORE INTO budgets (category, monthly_limit_uah)
            VALUES (?, ?)
            """,
            list(DEFAULT_BUDGETS.items()),
        )
        conn.commit()


def get_budgets(db_path: str | None = None) -> dict[str, float]:
    """Return configured monthly category budgets in UAH."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        rows = conn.execute(
            "SELECT category, monthly_limit_uah FROM budgets ORDER BY category ASC"
        ).fetchall()
    return {str(row["category"]): float(row["monthly_limit_uah"]) for row in rows}


def upsert_budget(
    category: str,
    monthly_limit_uah: float,
    db_path: str | None = None,
) -> dict[str, float]:
    """Create or update the monthly UAH spending limit for a category."""
    cleaned_category = (category or "").strip()
    if not cleaned_category:
        raise ValueError("Category name cannot be empty.")
    limit_val = round(float(monthly_limit_uah), 2)
    if limit_val < 0:
        raise ValueError("Monthly budget limit must be non-negative.")

    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute(
            """
            INSERT INTO budgets (category, monthly_limit_uah)
            VALUES (?, ?)
            ON CONFLICT(category) DO UPDATE SET
                monthly_limit_uah = excluded.monthly_limit_uah
            """,
            (cleaned_category, limit_val),
        )
        conn.commit()
    return get_budgets(db_path=str(resolved))


def upsert_transactions(
    records: list[TransactionRecord],
    db_path: str | None = None,
) -> int:
    """Insert or update transaction records and return count processed."""
    resolved = _ensure_db_ready(db_path)
    if not records:
        return 0

    payload = [
        (
            r.id,
            r.account_id,
            r.source,
            r.timestamp,
            r.tx_date,
            r.tx_month,
            r.description,
            r.mcc,
            r.category,
            r.amount_minor,
            r.amount_uah,
            r.operation_amount_minor,
            r.currency_code,
            r.cashback_uah,
            r.balance_uah,
            1 if r.is_expense else 0,
            r.comment,
        )
        for r in records
    ]

    with closing(_connect_rw(str(resolved))) as conn:
        conn.executemany(
            """
            INSERT INTO transactions (
                id, account_id, source, timestamp, tx_date, tx_month,
                description, mcc, category, amount_minor, amount_uah,
                operation_amount_minor, currency_code, cashback_uah,
                balance_uah, is_expense, comment
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                account_id = excluded.account_id,
                source = excluded.source,
                timestamp = excluded.timestamp,
                tx_date = excluded.tx_date,
                tx_month = excluded.tx_month,
                description = excluded.description,
                mcc = excluded.mcc,
                category = excluded.category,
                amount_minor = excluded.amount_minor,
                amount_uah = excluded.amount_uah,
                operation_amount_minor = excluded.operation_amount_minor,
                currency_code = excluded.currency_code,
                cashback_uah = excluded.cashback_uah,
                balance_uah = excluded.balance_uah,
                is_expense = excluded.is_expense,
                comment = excluded.comment
            """,
            payload,
        )
        conn.commit()
    return len(records)


def clear_transactions(
    source: str | None = None,
    db_path: str | None = None,
) -> None:
    """Delete transactions optionally filtered by source."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        if source is not None:
            conn.execute("DELETE FROM transactions WHERE source = ?", (source,))
        else:
            conn.execute("DELETE FROM transactions")
        conn.commit()


def get_transactions(
    limit: int = 50,
    offset: int = 0,
    category: str | None = None,
    source: str | None = None,
    db_path: str | None = None,
) -> list[TransactionRecord]:
    """Fetch paginated transactions ordered by timestamp descending."""
    resolved = _ensure_db_ready(db_path)
    conditions: list[str] = []
    params: list[Any] = []

    if category is not None:
        conditions.append("category = ?")
        params.append(category)
    if source is not None:
        conditions.append("source = ?")
        params.append(source)

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    sql = (
        f"SELECT * FROM transactions {where_clause} "
        "ORDER BY timestamp DESC, id DESC LIMIT ? OFFSET ?"
    )
    params.extend([max(0, int(limit)), max(0, int(offset))])

    with closing(_connect_rw(str(resolved))) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_transaction(row) for row in rows]


def get_sync_meta(key: str, db_path: str | None = None) -> str | None:
    """Read a metadata value by key from sync_meta."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = ?",
            (key,),
        ).fetchone()
    return str(row["value"]) if row is not None else None


def set_sync_meta(key: str, value: str, db_path: str | None = None) -> None:
    """Upsert a metadata key-value entry in sync_meta."""
    resolved = _ensure_db_ready(db_path)
    now_ts = int(time.time())
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute(
            """
            INSERT INTO sync_meta (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, now_ts),
        )
        conn.commit()


def get_transaction_stats(db_path: str | None = None) -> dict[str, Any]:
    """Return total row count, active data mode, and min/max transaction dates."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        agg = conn.execute(
            """
            SELECT
                COUNT(*) AS total_transactions,
                MIN(tx_date) AS min_date,
                MAX(tx_date) AS max_date
            FROM transactions
            """
        ).fetchone()
        mono_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source = 'monobank'"
        ).fetchone()[0]
        csv_count = conn.execute(
            "SELECT COUNT(*) FROM transactions WHERE source = 'csv'"
        ).fetchone()[0]
        meta_row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'active_mode'"
        ).fetchone()

    total_transactions = int(agg["total_transactions"])
    meta_mode = str(meta_row["value"]) if meta_row is not None else None
    if mono_count > 0 or (meta_mode == "monobank" and total_transactions > 0):
        active_mode = "monobank"
    elif (meta_mode == "csv" and csv_count > 0) or (csv_count > 0 and total_transactions == csv_count):
        active_mode = "csv"
    else:
        active_mode = "demo"
    return {
        "total_transactions": total_transactions,
        "active_mode": active_mode,
        "min_date": str(agg["min_date"]) if agg["min_date"] is not None else None,
        "max_date": str(agg["max_date"]) if agg["max_date"] is not None else None,
    }


def validate_readonly_sql(sql: str) -> str:
    """Validate that an SQL query is a single, comment-free read-only SELECT/WITH statement."""
    if not isinstance(sql, str):
        raise ValueError("SQL query must be a string.")

    cleaned = sql.strip().rstrip(";").strip()
    if not cleaned:
        raise ValueError("SQL query cannot be empty.")

    if "--" in cleaned or "/*" in cleaned or "*/" in cleaned:
        raise ValueError("SQL comments (--, /*, */) are not permitted.")

    if ";" in cleaned:
        raise ValueError("Multiple or stacked SQL statements (';') are not permitted.")

    if not re.match(r"^(SELECT|WITH)\b", cleaned, flags=re.IGNORECASE):
        raise ValueError("Only read-only SELECT or WITH queries are allowed.")

    match = _FORBIDDEN_SQL_PATTERN.search(cleaned)
    if match:
        keyword = match.group(1).upper()
        raise ValueError(f"Forbidden SQL keyword detected: {keyword}")

    return cleaned


def execute_safe_select(
    sql: str,
    max_rows: int = 100,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Execute a validated read-only SELECT query in SQLite mode=ro and return rows."""
    cleaned_sql = validate_readonly_sql(sql)
    if max_rows < 1:
        raise ValueError("max_rows must be >= 1.")

    resolved = _ensure_db_ready(db_path)
    ro_uri = resolved.resolve().as_uri() + "?mode=ro"

    with closing(sqlite3.connect(ro_uri, uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON;")
        cursor = conn.execute(cleaned_sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        fetched = cursor.fetchmany(max_rows + 1)

    truncated = len(fetched) > max_rows
    rows_slice = fetched[:max_rows]
    rows_list = [dict(row) for row in rows_slice]

    return {
        "columns": columns,
        "rows": rows_list,
        "row_count": len(rows_list),
        "truncated": truncated,
        "executed_sql": cleaned_sql,
    }
