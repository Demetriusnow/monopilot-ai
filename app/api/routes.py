"""FastAPI REST & SSE endpoints for MonoPilot AI."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Body, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from app.agent.orchestrator import MultiAgentOrchestrator
from app.config import get_settings
from app.data.database import (
    get_budgets,
    get_latest_eval_run,
    get_receipt_records,
    get_sync_meta,
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
    EvalRunReport,
    HealthResponse,
    JarCreateUpdateRequest,
    JarRecord,
    MatchedTransactionSummary,
    MonobankAccountOption,
    ReceiptScanResponse,
    SyncMonobankRequest,
    SyncResponse,
    TransactionRecord,
    WhatIfRequest,
    WhatIfSimulationResponse,
)
from app.services.analytics import get_dashboard_summary
from app.services.csv_importer import import_csv_transactions
from app.services.jars_simulator import (
    create_or_update_jar,
    list_jars_enriched,
    remove_jar,
    simulate_what_if,
)
from app.services.monobank_client import (
    MonobankAuthError,
    MonobankRateLimitError,
    fetch_currency_rates,
    list_monobank_accounts,
    sync_monobank_statement,
)
from app.services.receipt_scanner import scan_receipt_image
from evals.run_evals import run_benchmark

router = APIRouter(prefix="/api", tags=["MonoPilot API"])

_PLACEHOLDER_KEYS = frozenset({"", "your_gemini_api_key_here", "changeme", "none", "null"})
_MAX_CSV_UPLOAD_BYTES = 5 * 1024 * 1024
_MAX_RECEIPT_UPLOAD_BYTES = 8 * 1024 * 1024


def _is_gemini_configured(api_key: str) -> bool:
    """Return True when a non-placeholder Gemini API key is configured."""
    normalized = (api_key or "").strip()
    if normalized.lower() in _PLACEHOLDER_KEYS:
        return False
    return not normalized.lower().startswith("your_")


def _json_sse_default(value: Any) -> Any:
    """Serialize Pydantic models and fallback objects cleanly for SSE JSON payloads."""
    if hasattr(value, "model_dump"):
        return value.model_dump()
    return str(value)


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


@router.get("/data/monobank-accounts", response_model=list[MonobankAccountOption])
async def get_monobank_accounts(
    x_mono_token: str | None = Header(default=None, alias="X-Mono-Token"),
) -> list[MonobankAccountOption]:
    """Return available Monobank accounts/cards for the provided or server-configured token."""
    settings = get_settings()
    token = (x_mono_token or settings.monobank_token or "").strip()
    if not token:
        raise HTTPException(
            status_code=400,
            detail="Monobank token is required.",
        )
    try:
        return await list_monobank_accounts(token=token)
    except MonobankAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except MonobankRateLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


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

    selected_id: str | None = None
    accounts: list[MonobankAccountOption] = []
    card_info_str = ""
    raw_meta = await asyncio.to_thread(get_sync_meta, "mono_account_info")
    if raw_meta:
        try:
            meta_obj = json.loads(raw_meta)
            selected_id = meta_obj.get("account_id")
            if isinstance(meta_obj.get("accounts"), list):
                accounts = [MonobankAccountOption(**opt) for opt in meta_obj["accounts"] if isinstance(opt, dict)]
            if meta_obj.get("card_label"):
                card_info_str = f" ({meta_obj['card_label']})"
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return SyncResponse(
        status="ok",
        source="monobank",
        synced_count=synced_count,
        total_in_db=total_in_db,
        selected_account_id=selected_id,
        accounts=accounts,
        message=f"Підключено Monobank{card_info_str}: синхронізовано {synced_count} операцій за {days} дн.",
    )


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
    """Run the MonoPilot AI multi-agent orchestrator over SQLite analytics."""
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    override_key = (x_gemini_key or "").strip() or None
    orchestrator = MultiAgentOrchestrator(api_key=override_key)
    return await orchestrator.run(
        message=payload.message,
        conversation_history=payload.history,
        days_context=payload.days_context,
    )


@router.post("/chat/stream")
async def stream_chat_with_agent(
    payload: ChatRequest,
    x_gemini_key: str | None = Header(default=None, alias="X-Gemini-Key"),
) -> StreamingResponse:
    """Stream Multi-Agent pipeline stages, tool traces, and token deltas over SSE."""
    if not payload.message.strip():
        raise HTTPException(status_code=400, detail="Message cannot be empty.")

    override_key = (x_gemini_key or "").strip() or None
    orchestrator = MultiAgentOrchestrator(api_key=override_key)

    async def _event_stream() -> AsyncIterator[str]:
        async for ev in orchestrator.stream(
            message=payload.message,
            conversation_history=payload.history,
            days_context=payload.days_context,
        ):
            event_name = str(ev.get("event") or "message")
            data_json = json.dumps(
                ev.get("data"),
                ensure_ascii=False,
                default=_json_sse_default,
            )
            yield f"event: {event_name}\ndata: {data_json}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/receipts/scan", response_model=ReceiptScanResponse)
async def scan_receipt(
    file: UploadFile = File(...),
    auto_add: bool = Query(False),
    x_gemini_key: str | None = Header(default=None, alias="X-Gemini-Key"),
) -> ReceiptScanResponse:
    """Scan an uploaded receipt image with Gemini Vision and reconcile against SQLite."""
    raw_bytes = await file.read(_MAX_RECEIPT_UPLOAD_BYTES + 1)
    if len(raw_bytes) > _MAX_RECEIPT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="Receipt image exceeds maximum allowed size of 8 MB.",
        )
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Uploaded receipt file is empty.")

    mime_type = (file.content_type or "image/png").strip() or "image/png"
    override_key = (x_gemini_key or "").strip() or None

    try:
        return await scan_receipt_image(
            raw_bytes=raw_bytes,
            mime_type=mime_type,
            api_key=override_key,
            auto_add=auto_add,
            is_demo=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/receipts/scan-demo", response_model=ReceiptScanResponse)
async def scan_demo_receipt(
    auto_add: bool = Query(False),
    x_gemini_key: str | None = Header(default=None, alias="X-Gemini-Key"),
) -> ReceiptScanResponse:
    """Scan the bundled demo receipt image and reconcile against demo transactions."""
    demo_path = Path(__file__).resolve().parent.parent / "static" / "demo-receipt.png"
    if not demo_path.exists():
        raise HTTPException(status_code=404, detail="Demo receipt image not found.")

    raw_bytes = await asyncio.to_thread(demo_path.read_bytes)
    override_key = (x_gemini_key or "").strip() or None

    try:
        return await scan_receipt_image(
            raw_bytes=raw_bytes,
            mime_type="image/png",
            api_key=override_key,
            auto_add=auto_add,
            is_demo=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/receipts")
async def list_receipts(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    """Return recently scanned receipts ordered newest-first."""
    return await asyncio.to_thread(get_receipt_records, limit=limit)


@router.get("/jars", response_model=list[JarRecord])
async def get_jars_list() -> list[JarRecord]:
    """Return all Mono Savings Jars enriched with progress percentage and goal horizon."""
    return await asyncio.to_thread(list_jars_enriched)


@router.post("/jars", response_model=list[JarRecord])
async def upsert_jar_endpoint(payload: JarCreateUpdateRequest) -> list[JarRecord]:
    """Create or update a Mono Savings Jar goal."""
    try:
        return await asyncio.to_thread(create_or_update_jar, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/jars/{jar_id}", response_model=list[JarRecord])
async def delete_jar_endpoint(jar_id: str) -> list[JarRecord]:
    """Delete a Mono Savings Jar by ID."""
    try:
        return await asyncio.to_thread(remove_jar, jar_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/jars/simulate", response_model=WhatIfSimulationResponse)
async def simulate_jars_what_if(payload: WhatIfRequest) -> WhatIfSimulationResponse:
    """Simulate category cuts and subscription cancellations to compute Jar timeline savings."""
    try:
        return await asyncio.to_thread(simulate_what_if, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/evals/latest", response_model=EvalRunReport | None)
async def get_latest_evals() -> EvalRunReport | None:
    """Return the most recent stored AI Eval & Red-Team Security Lab scorecard."""
    return await asyncio.to_thread(get_latest_eval_run)


@router.post("/evals/run", response_model=EvalRunReport)
async def run_evals_endpoint(
    mode: Literal["deterministic", "live"] = Query("deterministic"),
    x_gemini_key: str | None = Header(default=None, alias="X-Gemini-Key"),
) -> EvalRunReport:
    """Execute the 20-case golden benchmark and Red-Team Security Lab on an isolated temp DB."""
    override_key = (x_gemini_key or "").strip() or None
    try:
        return await run_benchmark(mode=mode, api_key=override_key)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
