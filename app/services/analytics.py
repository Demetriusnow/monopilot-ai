"""Pure financial analytics, subscription detection, anomaly detection, and budget forecasting."""

from __future__ import annotations

import calendar
import json
import re
import statistics
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from app.data.database import (
    get_budgets,
    get_sync_meta,
    get_transaction_stats,
    get_transactions,
)
from app.schemas import (
    AnomalyItem,
    BudgetForecast,
    CategorySpend,
    CurrencyRate,
    DailySpend,
    DashboardSummary,
    MonobankAccountOption,
    SubscriptionItem,
    TransactionRecord,
)


def _resolve_anchor_date(transactions: list[TransactionRecord]) -> date:
    """Return latest transaction date in dataset or today's date when empty."""
    if not transactions:
        return date.today()
    return max(date.fromisoformat(tx.tx_date) for tx in transactions)


def _normalize_merchant_key(description: str) -> str:
    """Normalize merchant description for grouping recurring charges."""
    cleaned = (description or "").strip().lower()
    cleaned = re.sub(r"[#*]\w+", " ", cleaned)
    cleaned = re.sub(r"\b\d+\b", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-*#_/")
    return cleaned or (description or "").strip().lower()


def detect_subscriptions(
    min_occurrences: int = 2,
    db_path: str | None = None,
) -> list[SubscriptionItem]:
    """Detect recurring monthly subscriptions across all stored expense transactions."""
    all_txs = get_transactions(limit=100_000, db_path=db_path)
    expenses = [tx for tx in all_txs if tx.is_expense]
    expenses.sort(key=lambda tx: (tx.timestamp, tx.id))

    grouped: dict[str, list[TransactionRecord]] = defaultdict(list)
    for tx in expenses:
        key = _normalize_merchant_key(tx.description)
        if key:
            grouped[key].append(tx)

    required_count = max(2, int(min_occurrences))
    results: list[SubscriptionItem] = []

    for group in grouped.values():
        if len(group) < required_count:
            continue

        dates = [date.fromisoformat(tx.tx_date) for tx in group]
        intervals = [
            (dates[i] - dates[i - 1]).days for i in range(1, len(dates))
        ]
        if not intervals:
            continue

        median_interval = statistics.median(intervals)
        if not (24.0 <= median_interval <= 36.0):
            continue

        amounts = [abs(tx.amount_uah) for tx in group]
        avg_amount = sum(amounts) / len(amounts)
        if avg_amount <= 0:
            continue

        rel_std = (statistics.stdev(amounts) / avg_amount) if len(amounts) >= 2 else 0.0
        if rel_std > 0.25:
            continue

        avg_rounded = round(avg_amount, 2)
        avg_interval = round(sum(intervals) / len(intervals), 1)
        last_date = dates[-1]
        next_date = last_date + timedelta(days=round(median_interval))

        results.append(
            SubscriptionItem(
                merchant=group[-1].description,
                category=group[-1].category,
                avg_amount_uah=avg_rounded,
                annual_cost_uah=round(avg_rounded * 12, 2),
                occurrences=len(group),
                avg_interval_days=avg_interval,
                last_charge_date=last_date.isoformat(),
                next_expected_date=next_date.isoformat(),
            )
        )

    results.sort(key=lambda item: (-item.avg_amount_uah, item.merchant))
    return results


def _robust_baseline(values: list[float], min_amount_uah: float) -> list[float]:
    """Filter extreme singleton spikes out of a reference pool when possible."""
    if len(values) < 4:
        return values
    med = statistics.median(values)
    cap = max(med * 5.0, min_amount_uah * 1.5)
    trimmed = [v for v in values if v <= cap]
    return trimmed if len(trimmed) >= 2 else values


def detect_anomalies(
    days: int = 90,
    z_threshold: float = 2.0,
    min_amount_uah: float = 1500.0,
    db_path: str | None = None,
) -> list[AnomalyItem]:
    """Identify anomalously high expenses within the target day window."""
    all_txs = get_transactions(limit=100_000, db_path=db_path)
    all_expenses = [tx for tx in all_txs if tx.is_expense]
    if not all_expenses:
        return []

    anchor_date = _resolve_anchor_date(all_txs)
    start_str = (anchor_date - timedelta(days=max(1, int(days)) - 1)).isoformat()
    end_str = anchor_date.isoformat()
    window_expenses = [
        tx for tx in all_expenses if start_str <= tx.tx_date <= end_str
    ]

    anomalies: list[AnomalyItem] = []

    for tx in window_expenses:
        amt = abs(tx.amount_uah)
        if amt < min_amount_uah:
            continue

        cat_others = [
            abs(other.amount_uah)
            for other in all_expenses
            if other.category == tx.category and other.id != tx.id
        ]
        if len(cat_others) >= 2 and amt < statistics.mean(cat_others) * 1.8:
            continue

        if len(cat_others) >= 3:
            baseline = _robust_baseline(cat_others, min_amount_uah)
        else:
            all_others = [
                abs(other.amount_uah)
                for other in all_expenses
                if other.id != tx.id
            ]
            if not all_others:
                continue
            baseline = _robust_baseline(all_others, min_amount_uah)

        mean_val = statistics.mean(baseline)
        if mean_val <= 0:
            continue

        std_val = statistics.stdev(baseline) if len(baseline) >= 2 else 0.0
        ratio = amt / mean_val

        if std_val > 1e-6:
            z_score = (amt - mean_val) / std_val
            flagged = z_score >= z_threshold and ratio >= 2.0
        else:
            z_score = ratio
            flagged = ratio >= max(2.0, z_threshold)

        if not flagged:
            continue

        anomalies.append(
            AnomalyItem(
                tx_id=tx.id,
                tx_date=tx.tx_date,
                description=tx.description,
                category=tx.category,
                amount_uah=round(amt, 2),
                category_avg_uah=round(mean_val, 2),
                ratio_to_avg=round(ratio, 2),
                reason=(
                    f"Витрата {amt:.2f} ₴ у категорії «{tx.category}» "
                    f"перевищує типовий чек ({mean_val:.2f} ₴) у {ratio:.1f}x разів "
                    f"(z={z_score:.1f})."
                ),
            )
        )

    anomalies.sort(key=lambda item: (-item.amount_uah, item.tx_date))
    return anomalies


def forecast_month_budget(
    monthly_target_uah: float | None = None,
    db_path: str | None = None,
) -> BudgetForecast:
    """Forecast current month end spending and compare against configured budgets."""
    all_txs = get_transactions(limit=100_000, db_path=db_path)
    today = date.today()

    if all_txs:
        latest_dt = _resolve_anchor_date(all_txs)
        if latest_dt.year == today.year and latest_dt.month == today.month:
            ref_dt = max(today, latest_dt)
        else:
            ref_dt = latest_dt
    else:
        ref_dt = today

    current_month = ref_dt.strftime("%Y-%m")
    prev_month = (ref_dt.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    days_in_month = calendar.monthrange(ref_dt.year, ref_dt.month)[1]
    days_elapsed = max(1, ref_dt.day)

    spent_so_far_uah = round(
        sum(
            abs(tx.amount_uah)
            for tx in all_txs
            if tx.is_expense and tx.tx_month == current_month
        ),
        2,
    )
    previous_month_total_uah = round(
        sum(
            abs(tx.amount_uah)
            for tx in all_txs
            if tx.is_expense and tx.tx_month == prev_month
        ),
        2,
    )

    if monthly_target_uah is not None:
        total_monthly_budget_uah = round(float(monthly_target_uah), 2)
    else:
        total_monthly_budget_uah = round(
            sum(get_budgets(db_path=db_path).values()), 2
        )

    daily_burn_rate_uah = round(spent_so_far_uah / days_elapsed, 2)
    projected_month_total_uah = round(
        (spent_so_far_uah / days_elapsed) * days_in_month, 2
    )
    projected_delta_vs_budget_uah = round(
        projected_month_total_uah - total_monthly_budget_uah, 2
    )

    days_remaining = max(0, days_in_month - days_elapsed)
    remaining_budget = max(0.0, total_monthly_budget_uah - spent_so_far_uah)
    safe_daily_allowance_uah = (
        round(remaining_budget / days_remaining, 2)
        if days_remaining > 0
        else round(remaining_budget, 2)
    )

    if total_monthly_budget_uah <= 0:
        status = "on_track"
    elif (
        spent_so_far_uah > total_monthly_budget_uah
        or projected_month_total_uah > total_monthly_budget_uah
    ):
        status = "over_budget"
    elif projected_month_total_uah >= 0.85 * total_monthly_budget_uah:
        status = "warning"
    else:
        status = "on_track"

    return BudgetForecast(
        current_month=current_month,
        days_elapsed=days_elapsed,
        days_in_month=days_in_month,
        spent_so_far_uah=spent_so_far_uah,
        daily_burn_rate_uah=daily_burn_rate_uah,
        projected_month_total_uah=projected_month_total_uah,
        previous_month_total_uah=previous_month_total_uah,
        total_monthly_budget_uah=total_monthly_budget_uah,
        projected_delta_vs_budget_uah=projected_delta_vs_budget_uah,
        safe_daily_allowance_uah=safe_daily_allowance_uah,
        status=status,
    )


def get_spending_summary(
    days: int = 30,
    month: str | None = None,
    category: str | None = None,
    db_path: str | None = None,
) -> dict[str, Any]:
    """Summarize expenses, income, top categories, and top merchants for a period."""
    all_txs = get_transactions(limit=100_000, db_path=db_path)

    if month:
        filtered = [tx for tx in all_txs if tx.tx_month == month]
        period_label = month
    else:
        period_days = max(1, int(days))
        anchor_date = _resolve_anchor_date(all_txs)
        start_str = (anchor_date - timedelta(days=period_days - 1)).isoformat()
        end_str = anchor_date.isoformat()
        filtered = [tx for tx in all_txs if start_str <= tx.tx_date <= end_str]
        period_label = f"{start_str}..{end_str}"

    if category:
        cat_target = category.strip().lower()
        filtered = [tx for tx in filtered if tx.category.lower() == cat_target]

    total_expense = round(
        sum(abs(tx.amount_uah) for tx in filtered if tx.is_expense), 2
    )
    total_income = round(
        sum(tx.amount_uah for tx in filtered if not tx.is_expense), 2
    )
    total_cashback = round(sum(tx.cashback_uah for tx in filtered), 2)

    cat_totals: dict[str, list[Any]] = {}
    merchant_totals: dict[tuple[str, str], list[Any]] = {}

    for tx in filtered:
        if not tx.is_expense:
            continue
        amt = abs(tx.amount_uah)
        if tx.category not in cat_totals:
            cat_totals[tx.category] = [0.0, 0]
        cat_totals[tx.category][0] += amt
        cat_totals[tx.category][1] += 1

        m_key = (tx.description, tx.category)
        if m_key not in merchant_totals:
            merchant_totals[m_key] = [0.0, 0]
        merchant_totals[m_key][0] += amt
        merchant_totals[m_key][1] += 1

    top_categories = [
        {
            "category": cat,
            "total_uah": round(vals[0], 2),
            "tx_count": vals[1],
            "share_pct": round((vals[0] / total_expense) * 100.0, 2)
            if total_expense > 0
            else 0.0,
        }
        for cat, vals in sorted(
            cat_totals.items(), key=lambda item: item[1][0], reverse=True
        )[:10]
    ]

    top_merchants = [
        {
            "merchant": m_key[0],
            "description": m_key[0],
            "category": m_key[1],
            "total_uah": round(vals[0], 2),
            "tx_count": vals[1],
        }
        for m_key, vals in sorted(
            merchant_totals.items(), key=lambda item: item[1][0], reverse=True
        )[:10]
    ]

    return {
        "period": period_label,
        "total_expense_uah": total_expense,
        "total_income_uah": total_income,
        "net_cashflow_uah": round(total_income - total_expense, 2),
        "total_cashback_uah": total_cashback,
        "tx_count": len(filtered),
        "top_categories": top_categories,
        "top_merchants": top_merchants,
    }


def get_dashboard_summary(
    days: int = 30,
    db_path: str | None = None,
    currency_rates: list[CurrencyRate] | None = None,
) -> DashboardSummary:
    """Build complete dashboard analytics payload for the specified day window."""
    period_days = max(1, int(days))
    stats = get_transaction_stats(db_path=db_path)
    all_txs = get_transactions(limit=100_000, db_path=db_path)
    budgets = get_budgets(db_path=db_path)

    anchor_date = _resolve_anchor_date(all_txs)
    start_date = anchor_date - timedelta(days=period_days - 1)
    start_str = start_date.isoformat()
    end_str = anchor_date.isoformat()

    window_txs = [tx for tx in all_txs if start_str <= tx.tx_date <= end_str]

    total_expense_uah = round(
        sum(abs(tx.amount_uah) for tx in window_txs if tx.is_expense), 2
    )
    total_income_uah = round(
        sum(tx.amount_uah for tx in window_txs if not tx.is_expense), 2
    )
    net_cashflow_uah = round(total_income_uah - total_expense_uah, 2)
    total_cashback_uah = round(sum(tx.cashback_uah for tx in window_txs), 2)
    current_balance_uah = round(all_txs[0].balance_uah, 2) if all_txs else 0.0
    account_owner: str | None = None
    account_label: str | None = None
    selected_account_id: str | None = None
    available_accounts: list[MonobankAccountOption] = []

    active_mode_str = str(stats["active_mode"])
    if active_mode_str == "monobank":
        raw_mono_meta = get_sync_meta("mono_account_info", db_path=db_path)
        if raw_mono_meta:
            try:
                parsed_meta = json.loads(raw_mono_meta)
                if isinstance(parsed_meta, dict):
                    if parsed_meta.get("own_balance_uah") is not None:
                        current_balance_uah = round(float(parsed_meta["own_balance_uah"]), 2)
                    account_owner = str(parsed_meta.get("client_name") or "").strip() or None
                    account_label = str(parsed_meta.get("card_label") or "").strip() or None
                    selected_account_id = str(parsed_meta.get("account_id") or "").strip() or None
                    raw_opts = parsed_meta.get("accounts")
                    if isinstance(raw_opts, list):
                        available_accounts = [MonobankAccountOption(**opt) for opt in raw_opts if isinstance(opt, dict)]
            except (json.JSONDecodeError, TypeError, ValueError):
                pass
    elif active_mode_str == "csv":
        account_label = "Імпортована виписка (CSV)"

    cat_map: dict[str, list[Any]] = {}
    daily_map: dict[str, list[float]] = {
        (start_date + timedelta(days=offset)).isoformat(): [0.0, 0.0]
        for offset in range(period_days)
    }

    for tx in window_txs:
        day_bucket = daily_map.get(tx.tx_date)
        if tx.is_expense:
            amt = abs(tx.amount_uah)
            if day_bucket is not None:
                day_bucket[0] += amt
            if tx.category not in cat_map:
                cat_map[tx.category] = [0.0, 0]
            cat_map[tx.category][0] += amt
            cat_map[tx.category][1] += 1
        else:
            if day_bucket is not None:
                day_bucket[1] += tx.amount_uah

    categories: list[CategorySpend] = []
    for cat, (total_val, cnt) in sorted(
        cat_map.items(), key=lambda item: item[1][0], reverse=True
    ):
        total_rounded = round(total_val, 2)
        limit = budgets.get(cat)
        categories.append(
            CategorySpend(
                category=cat,
                total_uah=total_rounded,
                tx_count=cnt,
                share_pct=round((total_val / total_expense_uah) * 100.0, 2)
                if total_expense_uah > 0
                else 0.0,
                budget_limit_uah=limit,
                budget_used_pct=round((total_rounded / limit) * 100.0, 2)
                if limit is not None and limit > 0
                else None,
            )
        )

    daily_series = [
        DailySpend(
            date=d_str,
            expense_uah=round(vals[0], 2),
            income_uah=round(vals[1], 2),
        )
        for d_str, vals in daily_map.items()
    ]

    return DashboardSummary(
        mode=active_mode_str,
        period_days=period_days,
        total_transactions=len(window_txs),
        total_expense_uah=total_expense_uah,
        total_income_uah=total_income_uah,
        net_cashflow_uah=net_cashflow_uah,
        total_cashback_uah=total_cashback_uah,
        current_balance_uah=current_balance_uah,
        account_owner=account_owner,
        account_label=account_label,
        selected_account_id=selected_account_id,
        available_accounts=available_accounts,
        categories=categories,
        daily_series=daily_series,
        subscriptions=detect_subscriptions(db_path=db_path),
        anomalies=detect_anomalies(days=period_days, db_path=db_path),
        forecast=forecast_month_budget(db_path=db_path),
        currency_rates=list(currency_rates) if currency_rates is not None else [],
    )
