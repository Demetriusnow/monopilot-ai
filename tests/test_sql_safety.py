"""Tests for read-only SQL validation and safe SQLite execution."""

from __future__ import annotations

import pytest

from app.data.database import execute_safe_select, init_db, validate_readonly_sql
from app.data.demo_seed import load_demo_dataset


@pytest.fixture()
def seeded_db(tmp_path) -> str:
    """Create and seed an isolated temporary SQLite database with demo transactions."""
    db_path = str(tmp_path / "sql_safety.db")
    init_db(db_path=db_path)
    load_demo_dataset(db_path=db_path, reset=True)
    return db_path


def test_execute_safe_select_aggregate_query(seeded_db: str) -> None:
    """Valid SELECT aggregation executes safely and returns columns, rows, and row_count."""
    sql = (
        "SELECT category, COUNT(*) AS tx_count, ROUND(SUM(ABS(amount_uah)), 2) AS total_uah "
        "FROM transactions WHERE is_expense = 1 GROUP BY category ORDER BY total_uah DESC"
    )
    result = execute_safe_select(sql, max_rows=20, db_path=seeded_db)

    assert result["columns"] == ["category", "tx_count", "total_uah"]
    assert result["row_count"] > 0
    assert len(result["rows"]) == result["row_count"]
    assert result["rows"][0]["tx_count"] > 0
    assert result["rows"][0]["total_uah"] > 0


def test_execute_safe_select_cte_with_query(seeded_db: str) -> None:
    """Valid WITH ... SELECT CTE queries are allowed and return accurate data."""
    sql = (
        "WITH sub_tx AS ("
        "SELECT description, ABS(amount_uah) AS amt "
        "FROM transactions "
        "WHERE category = 'Підписки та сервіси' AND is_expense = 1"
        ") "
        "SELECT description, COUNT(*) AS charges, ROUND(AVG(amt), 2) AS avg_uah "
        "FROM sub_tx GROUP BY description ORDER BY charges DESC"
    )
    result = execute_safe_select(sql, max_rows=50, db_path=seeded_db)

    assert result["columns"] == ["description", "charges", "avg_uah"]
    assert result["row_count"] == 4
    assert len(result["rows"]) == result["row_count"]
    assert all(row["charges"] == 3 for row in result["rows"])
    assert any("netflix" in row["description"].lower() for row in result["rows"])


def test_execute_safe_select_respects_max_rows(seeded_db: str) -> None:
    """Row limit cap prevents unbounded result sets and marks truncated=True."""
    result = execute_safe_select(
        "SELECT id, description FROM transactions ORDER BY timestamp DESC",
        max_rows=5,
        db_path=seeded_db,
    )

    assert result["row_count"] == 5
    assert len(result["rows"]) == 5
    assert result["truncated"] is True


def test_validate_readonly_sql_strips_trailing_semicolon() -> None:
    """Single trailing semicolon and whitespace are normalized cleanly."""
    assert validate_readonly_sql("  SELECT 1 AS ok;  ") == "SELECT 1 AS ok"
    assert validate_readonly_sql("WITH c AS (SELECT 1 AS n) SELECT n FROM c") == (
        "WITH c AS (SELECT 1 AS n) SELECT n FROM c"
    )


@pytest.mark.parametrize(
    "unsafe_sql",
    [
        "DROP TABLE transactions",
        "DELETE FROM transactions",
        "INSERT INTO transactions (id) VALUES ('evil')",
        "UPDATE transactions SET amount_uah = 0",
        "PRAGMA table_info(transactions)",
        "ATTACH DATABASE 'evil.db' AS evil",
        "SELECT * FROM transactions; DROP TABLE transactions",
        "SELECT 1 -- comment",
        "SELECT /* comment */ 1",
        "",
        "   ",
        "ALTER TABLE transactions ADD COLUMN hacked TEXT",
        "CREATE TABLE evil (id TEXT)",
    ],
)
def test_validate_readonly_sql_rejects_unsafe_queries(unsafe_sql: str) -> None:
    """Mutating statements, pragmas, stacked statements, comments, and empty SQL raise ValueError."""
    with pytest.raises(ValueError):
        validate_readonly_sql(unsafe_sql)


def test_execute_safe_select_blocks_write_attempt(seeded_db: str) -> None:
    """Attempting a mutating statement through execute_safe_select fails and leaves DB intact."""
    before = execute_safe_select("SELECT COUNT(*) AS cnt FROM transactions", db_path=seeded_db)
    with pytest.raises(ValueError):
        execute_safe_select("DELETE FROM transactions", db_path=seeded_db)
    after = execute_safe_select("SELECT COUNT(*) AS cnt FROM transactions", db_path=seeded_db)

    assert before["rows"][0]["cnt"] == after["rows"][0]["cnt"]
