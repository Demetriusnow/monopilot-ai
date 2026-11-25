"""FastAPI application entry point for MonoPilot AI."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import get_settings
from app.data.database import get_transaction_stats, init_db
from app.data.demo_seed import load_demo_dataset
from app.services.monobank_client import sync_monobank_statement

_PLACEHOLDER_MONO_TOKENS = frozenset({"", "your_monobank_token_here", "changeme", "none", "null"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite schema; auto-sync Monobank if MONOBANK_TOKEN is set, else seed demo data."""
    await asyncio.to_thread(init_db)
    settings = get_settings()
    mono_token = (settings.monobank_token or "").strip()
    stats = await asyncio.to_thread(get_transaction_stats)

    if (
        mono_token
        and mono_token.lower() not in _PLACEHOLDER_MONO_TOKENS
        and not mono_token.lower().startswith("your_")
        and stats.get("active_mode") != "monobank"
    ):
        try:
            await sync_monobank_statement(token=mono_token, days=31)
            stats = await asyncio.to_thread(get_transaction_stats)
        except Exception:
            pass

    if int(stats.get("total_transactions", 0)) == 0 and stats.get("active_mode") != "monobank":
        await asyncio.to_thread(load_demo_dataset)
    yield


app = FastAPI(
    title="MonoPilot AI",
    description="Fintech AI Agent over Monobank Open API",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)

_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


@app.get("/manifest.webmanifest", include_in_schema=False)
async def serve_manifest() -> FileResponse:
    """Serve the Web App Manifest at root scope for PWA installation."""
    manifest_file = _STATIC_DIR / "manifest.webmanifest"
    if not manifest_file.exists():
        raise HTTPException(status_code=404, detail="manifest.webmanifest not found.")
    return FileResponse(manifest_file, media_type="application/manifest+json")


@app.get("/sw.js", include_in_schema=False)
async def serve_service_worker() -> FileResponse:
    """Serve the root-scoped Service Worker with no-cache headers."""
    sw_file = _STATIC_DIR / "sw.js"
    if not sw_file.exists():
        raise HTTPException(status_code=404, detail="sw.js not found.")
    return FileResponse(
        sw_file,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    """Serve the single-page web UI entrypoint."""
    index_file = _STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html not found.")
    return FileResponse(index_file)
