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
from app.data.database import get_transaction_stats, init_db
from app.data.demo_seed import load_demo_dataset


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize SQLite schema and seed demo data on first startup if database is empty."""
    await asyncio.to_thread(init_db)
    stats = await asyncio.to_thread(get_transaction_stats)
    if int(stats.get("total_transactions", 0)) == 0:
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


@app.get("/", include_in_schema=False)
async def serve_index() -> FileResponse:
    """Serve the single-page web UI entrypoint."""
    index_file = _STATIC_DIR / "index.html"
    if not index_file.exists():
        raise HTTPException(status_code=404, detail="Frontend index.html not found.")
    return FileResponse(index_file)
