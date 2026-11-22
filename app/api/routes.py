"""FastAPI REST endpoints for MonoPilot AI."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, File, Header, HTTPException, Query, UploadFile

from app.agent.mono_agent import MonoPilotAgent
from app.config import get_settings
from app.data.database import (
    get_budgets,
    get_transaction_stats,
    get_transactions,
    upsert_budget,
)
from app.data.demo_seed import load_demo_dataset
from app.schemas import (
    BudgetItem,
    BudgetUpdateRequest,
    ChatRequest,
    ChatResponse,
    CurrencyRate,
    DashboardSummary,
    HealthResponse,
    SyncMonobankRequest,
    SyncResponse,
    TransactionRecord,
)
from app.services.analytics import get_dashboard_summary
from app.services.csv_importer import import_csv_transactions
from app.services.monobank_client import (
    MonobankAuthError,
    MonobankRateLimitError,
    fetch_currency_rates,
    sync_monobank_statement,
)

router = APIRouter(prefix="/api", tags=["MonoPilot API"])

_PLACEHOLDER_KEYS = frozenset({"", "your_gemini_api_key_here", "changeme", "none", "null"})


def _is_gemini_configured(api_key: str) -> bool:
    """Return True when a non-placeholder Gemini API key is configured."""
    normalized = (api_key or "").strip()
    if normalized.lower() in _PLACEHOLDER_KEYS:
        return False
    return not normalized.lower().startswith("your_")


@router.get("/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Return service health, configured Gemini model, active data mode, and row count."""
    settings = get_settings()
    stats = await asyncio.to_thread(get_transaction_stats)
    return HealthResponse(
        status="ok",
        model=settings.gemini_model,
        gemini_configured=_is_gemini_configured(settings.gemini_api_key),
        active_mode=str(stats.get("active_mode", "demo")),
        total_transactions=int(stats.get("total_transactions", 0)),
    )


@router.get("/dashboard", response_model=DashboardSummary)
async def get_dashboard(
    days: int = Query(30, ge=1, le=365),
) -> DashboardSummary:
    """Return full dashboard analytics including categories, series, subscriptions, anomalies, forecast, and FX."""
    currency_rates = await fetch_currency_rates()
    return await asyncio.to_thread(
        get_dashboard_summary,
        days=days,
        currency_rates=currency_rates,
    )


@router.get("/transactions", response_model=list[TransactionRecord])
async def list_transactions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    category: str | None = None,
    source: str | None = None,
) -> list[TransactionRecord]:
    """Return paginated transactions filtered optionally by category or source."""
    return await asyncio.to_thread(
        get_transactions,
        limit=limit,
        offset=offset,
        category=category,
        source=source,
    )


@router.get("/currency", response_model=list[CurrencyRate])
async def get_currency_rates(force_refresh: bool = False) -> list[CurrencyRate]:
    """Return live or cached Monobank currency exchange rates."""
    return await fetch_currency_rates(force_refresh=force_refresh)


@router.get("/budgets", response_model=list[BudgetItem])
async def list_budgets() -> list[BudgetItem]:
    """Return all configured monthly category spending limits."""
    budgets = await asyncio.to_thread(get_budgets)
    return [
        BudgetItem(category=cat, monthly_limit_uah=limit)
        for cat, limit in budgets.items()
    ]


@router.put("/budgets", response_model=list[BudgetItem])
async def update_budget(payload: BudgetUpdateRequest) -> list[BudgetItem]:
    """Create or update monthly spending limit for a category."""
    try:
        updated = await asyncio.to_thread(
            upsert_budget,
            payload.category,
            payload.monthly_limit_uah,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return [
        BudgetItem(category=cat, monthly_limit_uah=limit)
        for cat, limit in updated.items()
    ]


@router.post("/data/demo", response_model=SyncResponse)
async def reload_demo_data() -> SyncResponse:
    """Reset and load the realistic Ukrainian fintech demo dataset."""
    synced_count = await asyncio.to_thread(load_demo_dataset, reset=True)
    stats = await asyncio.to_thread(get_transaction_stats)
    return SyncResponse(
        status="ok",
        source="demo",
        synced_count=synced_count,
        total_in_db=int(stats.get("total_transactions", synced_count)),
        message=f"Завантажено {synced_count} демо-транзакцій.",
    )


@router.post("/data/sync-monobank", response_model=SyncResponse)
async def sync_monobank(
    payload: SyncMonobankRequest | None = Body(default=None),
    x_mono_token: str | None = Header(default=None, alias="X-Mono-Token"),
) -> SyncResponse:
    """Sync live Monobank personal statement using X-Mono-Token header or MONOBANK_TOKEN setting."""
    settings = get_settings()
    token = (x_mono_token or settings.monobank_token or "").strip()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Monobank token is required. Provide X-Mono-Token header or configure MONOBANK_TOKEN.",
        )

    days = max(1, min(payload.days if payload is not None else 31, 31))
    account_id = payload.account_id if payload is not None else "0"

    try:
        synced_count, total_in_db = await sync_monobank_statement(
            token=token,
            account_id=account_id,
            days=days,
        )
    except MonobankAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except MonobankRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    return SyncResponse(
        status="ok",
        source="monobank",
        synced_count=synced_count,
        total_in_db=total_in_db,
        message=f"Синхронізовано {synced_count} транзакцій з Monobank.",
    )


_MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024


@router.post("/data/upload-csv", response_model=SyncResponse)
async def upload_csv_statement(
    file: UploadFile = File(...),
    replace_existing: bool = Query(True),
) -> SyncResponse:
    """Import Monobank or standard banking CSV statement into SQLite."""
    raw_bytes = await file.read(_MAX_CSV_UPLOAD_BYTES + 1)
    if len(raw_bytes) > _MAX_CSV_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="CSV file exceeds maximum allowed size of 5 MB.",
        )
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded CSV file is empty.")

    try:
        imported_count, total_in_db = await asyncio.to_thread(
            import_csv_transactions,
            raw_bytes,
            replace_existing=replace_existing,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return SyncResponse(
        status="ok",
        source="csv",
        synced_count=imported_count,
        total_in_db=total_in_db,
        message=f"Імпортовано {imported_count} транзакцій із CSV-виписки.",
    )


@router.post("/chat", response_model=ChatResponse)
async def chat_with_agent(
    payload: ChatRequest,
    x_gemini_key: str | None = Header(default=None, alias="X-Gemini-Key"),
) -> ChatResponse:
    """Run the MonoPilot AI agent over SQLite analytics and return answer with tool execution traces."""
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    override_key = (x_gemini_key or "").strip() or None
    agent = MonoPilotAgent(api_key=override_key)
    return await agent.ask(
        message=payload.message,
        conversation_history=payload.history,
        days_context=payload.days_context,
    )
