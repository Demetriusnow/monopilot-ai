"""Mono Savings Jars ('Банки') CRUD helpers and What-If savings simulator."""

from __future__ import annotations

import uuid
from typing import Any

from app.data.database import delete_jar, get_jars, upsert_jar
from app.schemas import (
    JarCreateUpdateRequest,
    JarRecord,
    WhatIfRequest,
    WhatIfSimulationResponse,
)
from app.services.analytics import detect_subscriptions, get_dashboard_summary


def _enrich_jar(raw: dict[str, Any]) -> JarRecord:
    """Enrich a raw SQLite jar dictionary with progress percentage and goal horizon."""
    target = round(float(raw["target_uah"]), 2)
    curr = round(float(raw["current_uah"]), 2)
    monthly = round(float(raw["monthly_contribution_uah"]), 2)

    progress_pct = (
        round(max(0.0, min(100.0, (curr / target) * 100.0)), 1)
        if target > 0
        else 0.0
    )
    remaining = max(0.0, target - curr)
    remaining_uah = round(remaining, 2)

    if remaining <= 0:
        months_to_goal: float | None = 0.0
    elif monthly > 0:
        months_to_goal = round(remaining / monthly, 1)
    else:
        months_to_goal = None

    return JarRecord(
        id=str(raw["id"]),
        name=str(raw["name"]),
        icon=str(raw["icon"]),
        target_uah=target,
        current_uah=curr,
        monthly_contribution_uah=monthly,
        progress_pct=progress_pct,
        remaining_uah=remaining_uah,
        months_to_goal=months_to_goal,
    )


def list_jars_enriched(db_path: str | None = None) -> list[JarRecord]:
    """Return all Mono Savings Jars enriched with progress percentage and months to goal."""
    rows = get_jars(db_path=db_path)
    return [_enrich_jar(row) for row in rows]


def create_or_update_jar(
    req: JarCreateUpdateRequest,
    db_path: str | None = None,
) -> list[JarRecord]:
    """Create or update a Mono Savings Jar and return the enriched jar list."""
    cleaned_name = (req.name or "").strip()
    if not cleaned_name:
        raise ValueError("Назва Банки не може бути порожньою.")

    jar_id = (req.id or "").strip() or f"jar_{uuid.uuid4().hex[:8]}"
    upsert_jar(
        jar_id=jar_id,
        name=cleaned_name,
        icon=req.icon,
        target_uah=req.target_uah,
        current_uah=req.current_uah,
        monthly_contribution_uah=req.monthly_contribution_uah,
        db_path=db_path,
    )
    return list_jars_enriched(db_path=db_path)


def remove_jar(jar_id: str, db_path: str | None = None) -> list[JarRecord]:
    """Delete a Mono Savings Jar by ID and return remaining enriched jars."""
    cleaned_id = (jar_id or "").strip()
    if not cleaned_id or not delete_jar(cleaned_id, db_path=db_path):
        raise ValueError(f"Банку '{jar_id}' не знайдено.")
    return list_jars_enriched(db_path=db_path)


def simulate_what_if(
    req: WhatIfRequest,
    db_path: str | None = None,
) -> WhatIfSimulationResponse:
    """Simulate monthly/annual savings boost and Jar timeline acceleration."""
    jars = list_jars_enriched(db_path=db_path)

    selected_jar: JarRecord | None = None
    if req.jar_id:
        selected_jar = next((jar for jar in jars if jar.id == req.jar_id), None)
    if selected_jar is None and jars:
        selected_jar = jars[0]

    dash = get_dashboard_summary(days=30, db_path=db_path)
    subs = detect_subscriptions(db_path=db_path)

    cat_spend_exact: dict[str, float] = {
        c.category: float(c.total_uah) for c in dash.categories
    }
    cat_spend_lower: dict[str, tuple[str, float]] = {
        c.category.lower(): (c.category, float(c.total_uah)) for c in dash.categories
    }

    breakdown: list[dict[str, Any]] = []
    monthly_category_savings = 0.0

    for raw_cat, raw_pct in req.category_cuts_pct.items():
        pct = max(0.0, min(100.0, float(raw_pct)))
        if pct <= 0:
            continue

        cat_key = (raw_cat or "").strip()
        if cat_key in cat_spend_exact:
            label = cat_key
            cat_spend = cat_spend_exact[cat_key]
        else:
            label, cat_spend = cat_spend_lower.get(cat_key.lower(), (cat_key, 0.0))

        saving = round(cat_spend * (pct / 100.0), 2)
        monthly_category_savings += saving
        breakdown.append(
            {
                "type": "category_cut",
                "label": label,
                "cut_pct": round(pct, 1),
                "monthly_saving_uah": saving,
            }
        )

    monthly_subscription_savings = 0.0
    matched_sub_indices: set[int] = set()

    for raw_query in req.cancel_subscriptions:
        query = (raw_query or "").strip().lower()
        if not query:
            continue
        for idx, sub in enumerate(subs):
            if idx in matched_sub_indices:
                continue
            merchant_lower = sub.merchant.strip().lower()
            if (
                query == merchant_lower
                or query in merchant_lower
                or merchant_lower in query
            ):
                matched_sub_indices.add(idx)
                saving = round(float(sub.avg_amount_uah), 2)
                monthly_subscription_savings += saving
                breakdown.append(
                    {
                        "type": "subscription_cancel",
                        "label": sub.merchant,
                        "monthly_saving_uah": saving,
                        "annual_saving_uah": round(saving * 12.0, 2),
                    }
                )

    baseline = (
        round(selected_jar.monthly_contribution_uah, 2)
        if selected_jar is not None
        else 0.0
    )
    cat_save = round(monthly_category_savings, 2)
    sub_save = round(monthly_subscription_savings, 2)
    extra = round(max(0.0, float(req.extra_monthly_uah)), 2)

    if extra > 0:
        breakdown.append(
            {
                "type": "extra_monthly",
                "label": "Додатковий щомісячний внесок",
                "monthly_saving_uah": extra,
            }
        )

    total_new = round(baseline + cat_save + sub_save + extra, 2)
    annual_boost = round((cat_save + sub_save + extra) * 12.0, 2)

    jar_name = selected_jar.name if selected_jar is not None else None
    jar_remaining = (
        round(selected_jar.remaining_uah, 2) if selected_jar is not None else 0.0
    )

    baseline_months: float | None = None
    simulated_months: float | None = None
    months_saved: float | None = None

    if selected_jar is not None:
        if jar_remaining <= 0:
            baseline_months = 0.0
            simulated_months = 0.0
            months_saved = 0.0
        else:
            baseline_months = (
                round(jar_remaining / baseline, 1) if baseline > 0 else None
            )
            simulated_months = (
                round(jar_remaining / total_new, 1) if total_new > 0 else None
            )
            if baseline_months is not None and simulated_months is not None:
                months_saved = round(max(0.0, baseline_months - simulated_months), 1)

    return WhatIfSimulationResponse(
        baseline_monthly_savings_uah=baseline,
        monthly_category_savings_uah=cat_save,
        monthly_subscription_savings_uah=sub_save,
        total_new_monthly_savings_uah=total_new,
        annual_savings_boost_uah=annual_boost,
        jar_name=jar_name,
        jar_remaining_uah=jar_remaining,
        baseline_months=baseline_months,
        simulated_months=simulated_months,
        months_saved=months_saved,
        breakdown=breakdown,
    )
