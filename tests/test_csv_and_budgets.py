"""Tests for Monobank CSV statement importer and interactive category budgets."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.data.database import init_db
from app.main import app
from app.schemas import CurrencyRate
from app.services.csv_importer import import_csv_transactions, parse_monobank_csv


def _sample_ukrainian_monobank_csv() -> bytes:
    csv_text = (
        "Дата i час операції,Деталі операції,MCC,Сума в валюті картки (UAH),"
        "Сума в валюті операції,Валюта,Курс,Сума кешбеку (UAH),Залишок після операції\n"
        '15.11.2026 09:30:00,"Зарплата / ФОП переказ",4829,"38 000,00","38 000,00",UAH,—,—,"52 450,00"\n'
        '16.11.2026 18:15:10,"Сільпо",5411,"-1 245,50","-1 245,50",UAH,—,"24,91","51 204,50"\n'
        '18.11.2026 10:00:00,"Netflix.com",5815,"-449,00","-449,00",UAH,—,—,"50 755,50"\n'
    )
    return csv_text.encode("utf-8-sig")


def test_parse_official_ukrainian_monobank_csv() -> None:
    """Parses UTF-8-BOM Ukrainian Monobank CSV with comma decimals and space thousands separators."""
    records = parse_monobank_csv(_sample_ukrainian_monobank_csv())
    assert len(records) == 3

    salary = records[0]
    assert salary.tx_date == "2026-11-15"
    assert salary.tx_month == "2026-11"
    assert salary.is_expense is False
    assert salary.amount_uah == pytest.approx(38000.0, abs=0.01)
    assert salary.category == "Дохід та поповнення"

    silpo = records[1]
    assert silpo.description == "Сільпо"
    assert silpo.mcc == 5411
    assert silpo.category == "Продукти"
    assert silpo.is_expense is True
    assert silpo.amount_uah == pytest.approx(-1245.50, abs=0.01)
    assert silpo.cashback_uah == pytest.approx(24.91, abs=0.01)
    assert silpo.balance_uah == pytest.approx(51204.50, abs=0.01)

    netflix = records[2]
    assert netflix.category == "Підписки та сервіси"
    assert netflix.amount_uah == pytest.approx(-449.00, abs=0.01)


def test_parse_english_semicolon_csv_and_idempotent_reimport(tmp_path) -> None:
    """Parses standard English semicolon CSV and avoids duplicates on re-import."""
    csv_bytes = (
        "date;description;mcc;amount_uah;cashback;balance\n"
        "2026-11-10 14:20:00;Glovo Delivery;5814;-420.50;8.40;15000.00\n"
        "2026-11-11;Spotify;5815;-199.00;0;14801.00\n"
    ).encode("utf-8")

    db_path = str(tmp_path / "csv_idempotent.db")
    init_db(db_path=db_path)

    cnt1, tot1 = import_csv_transactions(csv_bytes, replace_existing=False, db_path=db_path)
    cnt2, tot2 = import_csv_transactions(csv_bytes, replace_existing=False, db_path=db_path)

    assert cnt1 == 2
    assert tot1 == 2
    assert cnt2 == 2
    assert tot2 == 2


def test_parse_malformed_csv_raises_value_error() -> None:
    """Malformed CSV without required date/description/amount columns raises ValueError."""
    bad_csv = b"foo,bar,baz\n1,2,3\n"
    with pytest.raises(ValueError):
        parse_monobank_csv(bad_csv)

    with pytest.raises(ValueError):
        parse_monobank_csv(b"")


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """Isolated FastAPI TestClient with mocked external FX rates."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "csv_budget_api.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()

    async def _fake_fx(*args, **kwargs):
        now_ts = int(time.time())
        return [
            CurrencyRate(
                currency_a="USD",
                currency_b="UAH",
                pair="USD/UAH",
                rate_buy=41.2,
                rate_sell=41.7,
                updated_at=now_ts,
            )
        ]

    monkeypatch.setattr("app.api.routes.fetch_currency_rates", _fake_fx)
    monkeypatch.setattr("app.agent.tools.fetch_currency_rates", _fake_fx)

    with TestClient(app) as client:
        yield client

    get_settings.cache_clear()


def test_budgets_get_and_put_updates_dashboard_forecast(api_client: TestClient) -> None:
    """GET/PUT /api/budgets persists new monthly limits and recalculates dashboard forecast."""
    get_resp = api_client.get("/api/budgets")
    assert get_resp.status_code == 200
    budgets = get_resp.json()
    assert len(budgets) >= 10
    initial_total = sum(b["monthly_limit_uah"] for b in budgets)

    put_resp = api_client.put(
        "/api/budgets",
        json={"category": "Продукти", "monthly_limit_uah": 20000.0},
    )
    assert put_resp.status_code == 200
    updated = {b["category"]: b["monthly_limit_uah"] for b in put_resp.json()}
    assert updated["Продукти"] == pytest.approx(20000.0, abs=0.01)

    dash_resp = api_client.get("/api/dashboard?days=30")
    assert dash_resp.status_code == 200
    forecast_limit = dash_resp.json()["forecast"]["total_monthly_budget_uah"]
    assert forecast_limit > initial_total


def test_upload_csv_endpoint_switches_mode_and_updates_health(api_client: TestClient) -> None:
    """POST /api/data/upload-csv imports statements and switches active_mode to csv."""
    resp = api_client.post(
        "/api/data/upload-csv?replace_existing=true",
        files={"file": ("mono_statement.csv", _sample_ukrainian_monobank_csv(), "text/csv")},
    )
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["status"] == "ok"
    assert payload["source"] == "csv"
    assert payload["synced_count"] == 3
    assert payload["total_in_db"] == 3

    health = api_client.get("/api/health").json()
    assert health["active_mode"] == "csv"
    assert health["total_transactions"] == 3

    bad_resp = api_client.post(
        "/api/data/upload-csv",
        files={"file": ("bad.csv", b"col1,col2\na,b\n", "text/csv")},
    )
    assert bad_resp.status_code == 400
