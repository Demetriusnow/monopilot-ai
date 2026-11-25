"""SQLite persistence and read-only SQL execution layer for MonoPilot AI."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.schemas import EvalRunReport, TransactionRecord

_REQUIRED_TABLES: frozenset[str] = frozenset(
    {"transactions", "sync_meta", "budgets", "jars", "receipts", "eval_runs"}
)

DEFAULT_JARS: tuple[tuple[str, str, str, float, float, float], ...] = (
    ("jar_macbook", "MacBook Pro M4", "💻", 85000.0, 28500.0, 7500.0),
    ("jar_cushion", "Фінансова подушка", "🛡️", 110000.0, 46000.0, 9000.0),
    ("jar_travel", "Відпустка у Карпатах", "🏔️", 24000.0, 9500.0, 3000.0),
)

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
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        existing = {str(r["name"]) for r in rows}
    if not _REQUIRED_TABLES.issubset(existing):
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

            CREATE TABLE IF NOT EXISTS jars (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL,
                target_uah REAL NOT NULL,
                current_uah REAL NOT NULL,
                monthly_contribution_uah REAL NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS receipts (
                id TEXT PRIMARY KEY,
                merchant TEXT NOT NULL,
                receipt_date TEXT NOT NULL,
                total_uah REAL NOT NULL,
                category TEXT NOT NULL,
                items_json TEXT NOT NULL,
                matched_tx_id TEXT,
                match_confidence REAL NOT NULL DEFAULT 0.0,
                added_tx_id TEXT,
                raw_model TEXT NOT NULL,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS eval_runs (
                id TEXT PRIMARY KEY,
                created_at INTEGER NOT NULL,
                mode TEXT NOT NULL,
                total_cases INTEGER NOT NULL,
                passed_cases INTEGER NOT NULL,
                numeric_accuracy_pct REAL NOT NULL,
                tool_accuracy_pct REAL NOT NULL,
                security_block_rate_pct REAL NOT NULL,
                avg_latency_ms REAL NOT NULL,
                details_json TEXT NOT NULL
            );
            """
        )
        budget_count = conn.execute("SELECT COUNT(*) FROM budgets").fetchone()[0]
        jar_count = conn.execute("SELECT COUNT(*) FROM jars").fetchone()[0]
        conn.commit()

    if budget_count == 0:
        seed_default_budgets(db_path=db_path)
    if jar_count == 0:
        seed_default_jars(db_path=db_path)


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


def prepare_monobank_account_storage(
    account_id: str,
    db_path: str | None = None,
) -> None:
    """Remove demo/CSV rows and rows belonging to other accounts before Monobank sync."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute(
            "DELETE FROM transactions WHERE source != 'monobank' OR account_id != ?",
            (account_id,),
        )
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
    if meta_mode == "monobank" or mono_count > 0:
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


def seed_default_jars(db_path: str | None = None, reset: bool = False) -> None:
    """Seed realistic Ukrainian Mono Jars ('Банки') if empty or when resetting to demo."""
    now_ts = int(time.time())
    with closing(_connect_rw(db_path)) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jars (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                icon TEXT NOT NULL,
                target_uah REAL NOT NULL,
                current_uah REAL NOT NULL,
                monthly_contribution_uah REAL NOT NULL,
                created_at INTEGER NOT NULL
            )
            """
        )
        if reset:
            conn.execute("DELETE FROM jars")
        conn.executemany(
            """
            INSERT OR IGNORE INTO jars (
                id, name, icon, target_uah, current_uah, monthly_contribution_uah, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [(jid, name, icon, target, curr, monthly, now_ts) for (jid, name, icon, target, curr, monthly) in DEFAULT_JARS],
        )
        conn.commit()


def replace_jars_with_monobank(
    jar_rows: list[tuple[str, str, str, float, float, float]],
    db_path: str | None = None,
) -> None:
    """Replace stored jars with live Monobank jars fetched from /personal/client-info."""
    resolved = _ensure_db_ready(db_path)
    now_ts = int(time.time())
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute("DELETE FROM jars")
        if jar_rows:
            conn.executemany(
                """
                INSERT INTO jars (
                    id, name, icon, target_uah, current_uah, monthly_contribution_uah, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (jid, name, icon, target_uah, current_uah, monthly_uah, now_ts + idx)
                    for idx, (jid, name, icon, target_uah, current_uah, monthly_uah) in enumerate(jar_rows)
                ],
            )
        conn.commit()


def get_jars(db_path: str | None = None) -> list[dict[str, Any]]:
    """Return all configured Mono Jars ordered by creation time and name."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        rows = conn.execute(
            "SELECT id, name, icon, target_uah, current_uah, monthly_contribution_uah, created_at "
            "FROM jars ORDER BY created_at ASC, name ASC"
        ).fetchall()
    return [dict(row) for row in rows]


def upsert_jar(
    jar_id: str,
    name: str,
    icon: str,
    target_uah: float,
    current_uah: float,
    monthly_contribution_uah: float,
    db_path: str | None = None,
) -> list[dict[str, Any]]:
    """Create or update a Mono Savings Jar and return all jars."""
    resolved = _ensure_db_ready(db_path)
    now_ts = int(time.time())
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute(
            """
            INSERT INTO jars (
                id, name, icon, target_uah, current_uah, monthly_contribution_uah, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                icon = excluded.icon,
                target_uah = excluded.target_uah,
                current_uah = excluded.current_uah,
                monthly_contribution_uah = excluded.monthly_contribution_uah
            """,
            (
                jar_id,
                name.strip(),
                (icon or "🏺").strip() or "🏺",
                round(float(target_uah), 2),
                round(float(current_uah), 2),
                round(float(monthly_contribution_uah), 2),
                now_ts,
            ),
        )
        conn.commit()
    return get_jars(db_path=str(resolved))


def delete_jar(jar_id: str, db_path: str | None = None) -> bool:
    """Delete a Mono Jar by ID and return True if a row was removed."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        cur = conn.execute("DELETE FROM jars WHERE id = ?", (jar_id,))
        conn.commit()
        return cur.rowcount > 0


def save_receipt_record(record: dict[str, Any], db_path: str | None = None) -> None:
    """Persist a scanned receipt summary and reconciliation metadata in SQLite."""
    resolved = _ensure_db_ready(db_path)
    now_ts = int(record.get("created_at") or time.time())
    items_raw = record.get("items", [])
    items_json = items_raw if isinstance(items_raw, str) else json.dumps(items_raw, ensure_ascii=False)
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute(
            """
            INSERT INTO receipts (
                id, merchant, receipt_date, total_uah, category,
                items_json, matched_tx_id, match_confidence, added_tx_id,
                raw_model, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                matched_tx_id = excluded.matched_tx_id,
                match_confidence = excluded.match_confidence,
                added_tx_id = excluded.added_tx_id
            """,
            (
                str(record["id"]),
                str(record["merchant"]),
                str(record["receipt_date"]),
                round(float(record["total_uah"]), 2),
                str(record["category"]),
                items_json,
                record.get("matched_tx_id"),
                float(record.get("match_confidence", 0.0)),
                record.get("added_tx_id"),
                str(record.get("raw_model", "")),
                now_ts,
            ),
        )
        conn.commit()


def get_receipt_records(limit: int = 20, db_path: str | None = None) -> list[dict[str, Any]]:
    """Return recently scanned receipts ordered newest-first."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        rows = conn.execute(
            "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        try:
            item["items"] = json.loads(item.pop("items_json", "[]"))
        except json.JSONDecodeError:
            item["items"] = []
        out.append(item)
    return out


def save_eval_run(report: EvalRunReport, db_path: str | None = None) -> None:
    """Persist an EvalRunReport JSON scorecard into SQLite."""
    resolved = _ensure_db_ready(db_path)
    details_json = report.model_dump_json()
    with closing(_connect_rw(str(resolved))) as conn:
        conn.execute(
            """
            INSERT INTO eval_runs (
                id, created_at, mode, total_cases, passed_cases,
                numeric_accuracy_pct, tool_accuracy_pct, security_block_rate_pct,
                avg_latency_ms, details_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                details_json = excluded.details_json
            """,
            (
                report.run_id,
                report.created_at,
                report.mode,
                report.total_cases,
                report.passed_cases,
                report.numeric_accuracy_pct,
                report.tool_accuracy_pct,
                report.security_block_rate_pct,
                report.avg_latency_ms,
                details_json,
            ),
        )
        conn.commit()


def get_latest_eval_run(db_path: str | None = None) -> EvalRunReport | None:
    """Load the most recent stored EvalRunReport, if any exists."""
    resolved = _ensure_db_ready(db_path)
    with closing(_connect_rw(str(resolved))) as conn:
        row = conn.execute(
            "SELECT details_json FROM eval_runs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    if row is None:
        return None
    return EvalRunReport.model_validate_json(str(row["details_json"]))
