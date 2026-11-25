"""Integration tests for FastAPI endpoints using isolated tmp_path SQLite and mocked FX."""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app
from app.schemas import CurrencyRate


def _fake_rates() -> list[CurrencyRate]:
    """Return deterministic offline currency rates for API tests."""
    now_ts = int(time.time())
    return [
        CurrencyRate(
            currency_a="USD",
            currency_b="UAH",
            pair="USD/UAH",
            rate_buy=41.20,
            rate_sell=41.75,
            rate_cross=None,
            updated_at=now_ts,
        ),
        CurrencyRate(
            currency_a="EUR",
            currency_b="UAH",
            pair="EUR/UAH",
            rate_buy=44.60,
            rate_sell=45.30,
            rate_cross=None,
            updated_at=now_ts,
        ),
    ]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Configure an isolated SQLite database and mock external Monobank HTTP calls."""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "api_test.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("MONOBANK_TOKEN", "")
    get_settings.cache_clear()

    async def _mock_fetch_currency_rates(*args, **kwargs):
        return _fake_rates()

    monkeypatch.setattr("app.api.routes.fetch_currency_rates", _mock_fetch_currency_rates)
    monkeypatch.setattr("app.agent.tools.fetch_currency_rates", _mock_fetch_currency_rates)

    with TestClient(app) as test_client:
        yield test_client

    get_settings.cache_clear()


def test_root_serves_frontend_html(client: TestClient) -> None:
    """GET / serves the web dashboard HTML containing MonoPilot AI branding."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers.get("content-type", "")
    assert "MonoPilot AI" in response.text


def test_health_endpoint(client: TestClient) -> None:
    """GET /api/health reports service OK and seeded demo transactions count."""
    response = client.get("/api/health")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["gemini_configured"] is False
    assert data["active_mode"] == "demo"
    assert data["total_transactions"] > 100


def test_reload_demo_endpoint(client: TestClient) -> None:
    """POST /api/data/demo cleanly resets and reloads the demo transaction dataset."""
    response = client.post("/api/data/demo")
    assert response.status_code == 200

    data = response.json()
    assert data["status"] == "ok"
    assert data["source"] == "demo"
    assert data["synced_count"] > 100
    assert data["total_in_db"] > 100


def test_dashboard_endpoint(client: TestClient) -> None:
    """GET /api/dashboard?days=30 returns categories, series, subscriptions, anomalies, forecast, and FX."""
    response = client.get("/api/dashboard?days=30")
    assert response.status_code == 200

    data = response.json()
    assert data["period_days"] == 30
    assert data["total_transactions"] > 0
    assert data["total_expense_uah"] > 0
    assert len(data["categories"]) > 0
    assert len(data["daily_series"]) > 0
    assert len(data["subscriptions"]) >= 5
    assert len(data["anomalies"]) >= 1
    assert "projected_month_total_uah" in data["forecast"]
    assert len(data["currency_rates"]) >= 2


def test_transactions_endpoint_limit(client: TestClient) -> None:
    """GET /api/transactions?limit=10 returns at most 10 valid transaction records."""
    response = client.get("/api/transactions?limit=10&offset=0")
    assert response.status_code == 200

    rows = response.json()
    assert 1 <= len(rows) <= 10
    assert {"id", "tx_date", "description", "category", "amount_uah"} <= set(rows[0].keys())


def test_chat_endpoint_fallback_without_gemini_key(client: TestClient) -> None:
    """POST /api/chat without a Gemini key runs deterministic tool fallback and returns tool traces."""
    response = client.post(
        "/api/chat",
        json={
            "message": "Знайди всі мої регулярні підписки та порахуй витрати за рік",
            "history": [],
            "days_context": 30,
        },
    )
    assert response.status_code == 200

    data = response.json()
    assert isinstance(data["answer"], str) and len(data["answer"].strip()) > 0
    assert data["used_fallback"] is True
    assert len(data["tool_traces"]) >= 1
    assert data["tool_traces"][0]["tool_name"] == "detect_subscriptions"
    assert data["tool_traces"][0]["status"] == "ok"


def test_sync_monobank_without_token_returns_400(client: TestClient) -> None:
    """POST /api/data/sync-monobank without X-Mono-Token or env token returns HTTP 400."""
    response = client.post(
        "/api/data/sync-monobank",
        json={"days": 31, "account_id": "0"},
    )
    assert response.status_code == 400


def test_sync_monobank_replaces_demo_data_and_loads_client_info_and_jars(
    client: TestClient,
    monkeypatch,
) -> None:
    """Syncing Monobank wipes demo rows, computes own card balance without credit limit, and syncs Jars."""
    now_ts = int(time.time())

    class _MockResponse:
        def __init__(self, status_code: int, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self) -> None:
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP {self.status_code}")

        def json(self):
            return self._payload

    class _MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str, headers: dict | None = None):
            assert headers and headers.get("X-Token") == "test_live_mono_token"
            if url.endswith("/personal/client-info"):
                return _MockResponse(
                    200,
                    {
                        "name": "Дмитро Заєць",
                        "accounts": [
                            {
                                "id": "acc_black_uah",
                                "type": "black",
                                "currencyCode": 980,
                                "balance": 6452050,  # 14,520.50 own + 50,000.00 credit limit
                                "creditLimit": 5000000,
                                "maskedPan": ["537541******4821"],
                            },
                            {
                                "id": "acc_white_uah",
                                "type": "white",
                                "currencyCode": 980,
                                "balance": 320000,
                                "creditLimit": 0,
                                "maskedPan": ["537541******9910"],
                            },
                        ],
                        "jars": [
                            {
                                "id": "jar_real_1",
                                "title": "На ноутбук",
                                "currencyCode": 980,
                                "balance": 2150000,
                                "goal": 5000000,
                            }
                        ],
                    },
                )
            if "/personal/statement/acc_black_uah/" in url:
                return _MockResponse(
                    200,
                    [
                        {
                            "id": "mono_tx_1",
                            "time": now_ts - 3600,
                            "description": "АТБ-Маркет",
                            "mcc": 5411,
                            "amount": -42550,
                            "operationAmount": -42550,
                            "currencyCode": 980,
                            "cashbackAmount": 425,
                            "balance": 6452050,
                        },
                        {
                            "id": "mono_tx_2",
                            "time": now_ts - 7200,
                            "description": "Поповнення картки",
                            "mcc": 4829,
                            "amount": 1500000,
                            "operationAmount": 1500000,
                            "currencyCode": 980,
                            "cashbackAmount": 0,
                            "balance": 6494600,
                        },
                    ],
                )
            raise AssertionError(f"Unexpected URL: {url}")

    monkeypatch.setattr("app.services.monobank_client.httpx.AsyncClient", _MockAsyncClient)

    sync_resp = client.post(
        "/api/data/sync-monobank",
        headers={"X-Mono-Token": "test_live_mono_token"},
        json={"days": 31, "account_id": "0"},
    )
    assert sync_resp.status_code == 200
    sync_data = sync_resp.json()
    assert sync_data["source"] == "monobank"
    assert sync_data["synced_count"] == 2
    assert sync_data["total_in_db"] == 2  # All 144 demo rows removed!
    assert sync_data["selected_account_id"] == "acc_black_uah"
    assert len(sync_data["accounts"]) == 2

    dash_resp = client.get("/api/dashboard?days=30")
    assert dash_resp.status_code == 200
    dash = dash_resp.json()
    assert dash["mode"] == "monobank"
    assert dash["total_transactions"] == 2
    assert dash["current_balance_uah"] == 14520.50
    assert dash["account_owner"] == "Дмитро Заєць"
    assert "4821" in (dash["account_label"] or "")

    jars_resp = client.get("/api/jars")
    assert jars_resp.status_code == 200
    jars = jars_resp.json()
    assert len(jars) == 1
    assert jars[0]["name"] == "На ноутбук"
    assert jars[0]["current_uah"] == 21500.0
    assert jars[0]["target_uah"] == 50000.0

    # Reset back to demo restores demo rows & default jars
    reset_resp = client.post("/api/data/demo")
    assert reset_resp.status_code == 200
    dash_after = client.get("/api/dashboard?days=30").json()
    assert dash_after["mode"] == "demo"
    assert dash_after["account_owner"] is None
    assert len(client.get("/api/jars").json()) >= 3


def test_pwa_manifest_service_worker_and_icons(client: TestClient) -> None:
    """Verify PWA manifest, root-scoped service worker, and PNG app icons are served."""
    manifest_resp = client.get("/manifest.webmanifest")
    assert manifest_resp.status_code == 200
    manifest_data = manifest_resp.json()
    assert manifest_data["display"] == "standalone"
    assert manifest_data["short_name"] == "MonoPilot"
    assert len(manifest_data["icons"]) >= 2

    sw_resp = client.get("/sw.js")
    assert sw_resp.status_code == 200
    assert "javascript" in sw_resp.headers.get("content-type", "")
    assert "CACHE_NAME" in sw_resp.text

    for icon_path in ("/static/icons/icon-192.png", "/static/icons/icon-512.png", "/static/icons/apple-touch-icon.png"):
        icon_resp = client.get(icon_path)
        assert icon_resp.status_code == 200
        assert icon_resp.content.startswith(b"\x89PNG\r\n\x1a\n")
