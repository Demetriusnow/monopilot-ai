"""Agent tool declarations and async execution dispatcher for MonoPilot AI."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from pydantic import BaseModel

from app.data.database import execute_safe_select
from app.schemas import ToolTrace, WhatIfRequest
from app.services.analytics import (
    detect_anomalies,
    detect_subscriptions,
    forecast_month_budget,
    get_spending_summary,
)
from app.services.monobank_client import fetch_currency_rates

TOOL_DECLARATIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "name": "run_sql_analytics",
        "description": (
            "Execute a read-only SQLite SELECT or WITH ... SELECT query against the user's "
            "banking database. Tables: "
            "transactions(id, account_id, source, timestamp, tx_date, tx_month, description, mcc, "
            "category, amount_minor, amount_uah, cashback_uah, balance_uah, is_expense) "
            "and budgets(category, monthly_limit_uah). "
            "IMPORTANT: amount_uah is NEGATIVE for expenses (is_expense = 1), so always use "
            "ROUND(SUM(ABS(amount_uah)), 2) when calculating expense totals."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "A valid SQLite read-only SELECT or WITH ... SELECT query. "
                        "Mutating statements (INSERT, UPDATE, DELETE, DROP, PRAGMA, etc.) are rejected."
                    ),
                }
            },
            "required": ["sql"],
        },
    },
    {
        "type": "function",
        "name": "get_spending_summary",
        "description": (
            "Retrieve aggregated expenses, income, net cashflow, cashback, top spending "
            "categories, and top merchants over a lookback period or specific month/category."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback period in days when month is not specified (default: 30).",
                },
                "month": {
                    "type": "string",
                    "description": "Optional specific month filter in YYYY-MM format (e.g. '2026-11').",
                },
                "category": {
                    "type": "string",
                    "description": "Optional category name filter (e.g. 'Продукти', 'Кафе та ресторани', 'Транспорт та авто').",
                },
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "detect_subscriptions",
        "description": (
            "Detect recurring monthly subscriptions and regular service charges, returning "
            "average charge amount in UAH, annualized cost, occurrence count, and next expected charge date."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "min_occurrences": {
                    "type": "integer",
                    "description": "Minimum number of recurring charges required to detect a subscription (default: 2).",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "detect_spending_anomalies",
        "description": (
            "Find unusually large expense transactions and statistical spending outliers "
            "compared to category averages."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Lookback window in days for anomaly detection (default: 90).",
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "forecast_monthly_budget",
        "description": (
            "Calculate current month daily burn rate, projected end-of-month spending total, "
            "comparison against budget, and safe daily spending allowance in UAH."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "monthly_target_uah": {
                    "type": "number",
                    "description": (
                        "Optional custom monthly budget target in UAH. "
                        "Leave omitted to use configured category budgets."
                    ),
                }
            },
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_monobank_currency_rates",
        "description": (
            "Fetch live Monobank exchange rates in UAH for major currencies (USD, EUR, PLN, GBP)."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "get_mono_jars",
        "description": (
            "Retrieve all Monobank savings jars ('Банки') with current balance, target UAH, "
            "monthly contribution, completion percentage, remaining UAH, and estimated months to goal."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "type": "function",
        "name": "simulate_what_if_savings",
        "description": (
            "Simulate a What-If monthly savings scenario by cutting spending categories by a percentage, "
            "cancelling recurring subscriptions, and/or adding extra monthly UAH contributions toward a Mono Jar."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "jar_id": {
                    "type": "string",
                    "description": (
                        "Optional target Mono Jar ID (e.g. 'jar_macbook', 'jar_cushion', 'jar_travel')."
                    ),
                },
                "category_cuts_pct": {
                    "type": "object",
                    "description": (
                        "Optional map of Ukrainian category name to percentage spending cut (0-100), "
                        "e.g. {'Кафе та ресторани': 30, 'Покупки та хобі': 20}."
                    ),
                },
                "cancel_subscriptions": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional list of recurring subscription merchant names to cancel, "
                        "e.g. ['Netflix.com', 'Spotify']."
                    ),
                },
                "extra_monthly_uah": {
                    "type": "number",
                    "description": "Optional additional monthly contribution in UAH (default: 0.0).",
                },
            },
            "required": [],
        },
    },
]


def _to_json_compatible(value: Any) -> Any:
    """Recursively convert Pydantic models and nested containers into plain Python types."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_to_json_compatible(item) for item in value]
    if isinstance(value, tuple):
        return [_to_json_compatible(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _to_json_compatible(v) for k, v in value.items()}
    return value


def _build_result_preview(data: Any, max_chars: int = 3000, max_items: int = 10) -> Any:
    """Keep structured dict/list preview intact while bounding oversized payloads."""
    try:
        serialized_len = len(json.dumps(data, ensure_ascii=False, default=str))
    except TypeError:
        return str(data)[:max_chars]

    if serialized_len <= max_chars:
        return data

    if isinstance(data, list):
        sliced = data[:max_items]
        while len(sliced) > 1 and len(json.dumps(sliced, ensure_ascii=False, default=str)) > max_chars:
            sliced = sliced[: max(1, len(sliced) // 2)]
        return sliced

    if isinstance(data, dict):
        trimmed: dict[str, Any] = {}
        for key, value in data.items():
            if isinstance(value, list):
                item_slice = value[:max_items]
                while (
                    len(item_slice) > 1
                    and len(json.dumps(item_slice, ensure_ascii=False, default=str)) > max_chars // 2
                ):
                    item_slice = item_slice[: max(1, len(item_slice) // 2)]
                trimmed[key] = item_slice
                if len(value) > len(item_slice):
                    trimmed["_preview_truncated"] = True
            elif isinstance(value, str) and len(value) > 500:
                trimmed[key] = value[:500] + "..."
                trimmed["_preview_truncated"] = True
            else:
                trimmed[key] = value

        if len(json.dumps(trimmed, ensure_ascii=False, default=str)) <= max_chars * 2:
            return trimmed

        raw_preview = json.dumps(trimmed, ensure_ascii=False, default=str)[:max_chars]
        return {"preview_text": raw_preview + "...", "_preview_truncated": True}

    if isinstance(data, str) and len(data) > max_chars:
        return data[:max_chars] + "..."

    return data


def _build_what_if_request(args: dict[str, Any]) -> WhatIfRequest:
    """Normalize tool call arguments into a validated WhatIfRequest payload."""
    jar_id_raw = args.get("jar_id")
    jar_id = str(jar_id_raw).strip() if jar_id_raw is not None and str(jar_id_raw).strip() else None

    cuts_raw = args.get("category_cuts_pct")
    if isinstance(cuts_raw, str):
        try:
            cuts_raw = json.loads(cuts_raw)
        except json.JSONDecodeError:
            cuts_raw = {}
    category_cuts_pct: dict[str, float] = {}
    if isinstance(cuts_raw, dict):
        for k, v in cuts_raw.items():
            if v is not None:
                category_cuts_pct[str(k).strip()] = float(v)

    cancel_raw = args.get("cancel_subscriptions")
    if isinstance(cancel_raw, str):
        try:
            parsed_cancel = json.loads(cancel_raw)
            cancel_raw = parsed_cancel if isinstance(parsed_cancel, list) else [cancel_raw]
        except json.JSONDecodeError:
            cancel_raw = [part.strip() for part in cancel_raw.split(",") if part.strip()]
    cancel_subscriptions: list[str] = []
    if isinstance(cancel_raw, list):
        cancel_subscriptions = [str(item).strip() for item in cancel_raw if str(item).strip()]

    extra_raw = args.get("extra_monthly_uah")
    extra_monthly_uah = float(extra_raw) if extra_raw is not None else 0.0

    return WhatIfRequest(
        jar_id=jar_id,
        category_cuts_pct=category_cuts_pct,
        cancel_subscriptions=cancel_subscriptions,
        extra_monthly_uah=extra_monthly_uah,
    )


async def execute_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    db_path: str | None = None,
) -> tuple[Any, ToolTrace]:
    """Execute a declared analytics tool and return its serialized payload and ToolTrace."""
    args = dict(arguments or {})
    sql_query = (
        str(args["sql"]).strip()
        if name == "run_sql_analytics" and args.get("sql") is not None
        else None
    )
    started = time.perf_counter()

    try:
        if name == "run_sql_analytics":
            sql = args.get("sql")
            if not isinstance(sql, str) or not sql.strip():
                raise ValueError("Parameter 'sql' must be a non-empty SQL SELECT query string.")
            raw_result = await asyncio.to_thread(execute_safe_select, sql=sql, db_path=db_path)
        elif name == "get_spending_summary":
            days = int(args["days"]) if args.get("days") is not None else 30
            month = str(args["month"]).strip() if args.get("month") else None
            category = str(args["category"]).strip() if args.get("category") else None
            raw_result = await asyncio.to_thread(
                get_spending_summary,
                days=days,
                month=month,
                category=category,
                db_path=db_path,
            )
        elif name == "detect_subscriptions":
            min_occ = int(args["min_occurrences"]) if args.get("min_occurrences") is not None else 2
            raw_result = await asyncio.to_thread(
                detect_subscriptions,
                min_occurrences=min_occ,
                db_path=db_path,
            )
        elif name == "detect_spending_anomalies":
            days = int(args["days"]) if args.get("days") is not None else 90
            raw_result = await asyncio.to_thread(
                detect_anomalies,
                days=days,
                db_path=db_path,
            )
        elif name == "forecast_monthly_budget":
            target_raw = args.get("monthly_target_uah")
            target = float(target_raw) if target_raw is not None else None
            raw_result = await asyncio.to_thread(
                forecast_month_budget,
                monthly_target_uah=target,
                db_path=db_path,
            )
        elif name == "get_monobank_currency_rates":
            raw_result = await fetch_currency_rates(db_path=db_path)
        elif name == "get_mono_jars":
            from app.services.jars_simulator import list_jars_enriched

            raw_result = await asyncio.to_thread(list_jars_enriched, db_path=db_path)
        elif name == "simulate_what_if_savings":
            from app.services.jars_simulator import simulate_what_if

            req = _build_what_if_request(args)
            raw_result = await asyncio.to_thread(simulate_what_if, req, db_path=db_path)
        else:
            raise ValueError(f"Unknown tool requested: {name}")

        serialized = _to_json_compatible(raw_result)
        preview = _build_result_preview(serialized)
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        trace = ToolTrace(
            tool_name=name,
            arguments=args,
            sql_query=sql_query,
            result_preview=preview,
            duration_ms=duration_ms,
            status="ok",
        )
        return serialized, trace
    except Exception as exc:
        duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
        error_payload = {"error": str(exc)}
        trace = ToolTrace(
            tool_name=name,
            arguments=args,
            sql_query=sql_query,
            result_preview=error_payload,
            duration_ms=duration_ms,
            status="error",
        )
        return error_payload, trace
