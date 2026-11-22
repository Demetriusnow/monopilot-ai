"""Tests for MCC categorization and financial analytics services."""

from __future__ import annotations

import pytest

from app.data.demo_seed import load_demo_dataset
from app.data.mcc_catalog import resolve_mcc_category
from app.schemas import BudgetForecast
from app.services.analytics import (
    detect_anomalies,
    detect_subscriptions,
    forecast_month_budget,
)


def test_resolve_mcc_category_rules() -> None:
    """Verify income, subscription keywords, standard MCC, and unknown fallback rules."""
    assert resolve_mcc_category(4829, description="Зарплата", amount_minor=500000) == "Дохід та поповнення"
    assert resolve_mcc_category(5815, description="Netflix.com", amount_minor=-44900) == "Підписки та сервіси"
    assert resolve_mcc_category(5999, description="Spotify Premium", amount_minor=-19900) == "Підписки та сервіси"
    assert resolve_mcc_category(5411, description="Сільпо", amount_minor=-62000) == "Продукти"
    assert resolve_mcc_category(9999, description="Невідомий сервіс", amount_minor=-15000) == "Інше"


def test_detect_subscriptions_on_demo_dataset(tmp_path) -> None:
    """Demo dataset yields at least 5 recurring subscriptions with accurate annual costs."""
    db_path = str(tmp_path / "analytics_subs.db")
    load_demo_dataset(db_path=db_path, reset=True)

    subs = detect_subscriptions(min_occurrences=2, db_path=db_path)

    assert len(subs) >= 5
    merchants_lower = [s.merchant.lower() for s in subs]
    for expected in ("netflix", "spotify", "youtube", "icloud", "kyivstar"):
        assert any(expected in name for name in merchants_lower), f"Missing subscription: {expected}"

    for sub in subs:
        assert sub.occurrences >= 2
        assert sub.avg_amount_uah > 0
        assert sub.annual_cost_uah == pytest.approx(sub.avg_amount_uah * 12.0, rel=0.1)
        assert len(sub.last_charge_date) == 10
        assert len(sub.next_expected_date) == 10


def test_detect_anomalies_flags_rozetka_and_catch(tmp_path) -> None:
    """High-value outlier transactions (Rozetka 16490 UAH and Catch 4350 UAH) are flagged."""
    db_path = str(tmp_path / "analytics_anomalies.db")
    load_demo_dataset(db_path=db_path, reset=True)

    anomalies = detect_anomalies(
        days=90,
        z_threshold=2.0,
        min_amount_uah=1500.0,
        db_path=db_path,
    )

    assert len(anomalies) >= 2
    rozetka = next((a for a in anomalies if "rozetka" in a.description.lower()), None)
    catch = next((a for a in anomalies if "catch" in a.description.lower()), None)

    assert rozetka is not None
    assert rozetka.amount_uah == pytest.approx(16490.0, abs=1.0)
    assert rozetka.ratio_to_avg > 1.5

    assert catch is not None
    assert catch.amount_uah == pytest.approx(4350.0, abs=1.0)
    assert catch.ratio_to_avg > 1.5


def test_forecast_month_budget_returns_valid_projection(tmp_path) -> None:
    """Budget forecast computes valid elapsed days, projected total, and budget status."""
    db_path = str(tmp_path / "analytics_forecast.db")
    load_demo_dataset(db_path=db_path, reset=True)

    forecast = forecast_month_budget(db_path=db_path)

    assert isinstance(forecast, BudgetForecast)
    assert forecast.days_elapsed >= 1
    assert forecast.days_in_month >= forecast.days_elapsed
    assert forecast.spent_so_far_uah >= 0.0
    assert forecast.daily_burn_rate_uah >= 0.0
    assert forecast.projected_month_total_uah >= forecast.spent_so_far_uah
    assert forecast.total_monthly_budget_uah > 0.0
    assert forecast.status in {"on_track", "warning", "over_budget"}

    custom_forecast = forecast_month_budget(monthly_target_uah=50000.0, db_path=db_path)
    assert custom_forecast.total_monthly_budget_uah == pytest.approx(50000.0, abs=0.01)
