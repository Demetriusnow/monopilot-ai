"""Shared Pydantic v2 schemas for MonoPilot AI."""

from typing import Any, Literal
from pydantic import BaseModel, Field


class TransactionRecord(BaseModel):
    """Normalized bank transaction stored in SQLite."""

    id: str
    account_id: str = "0"
    source: str = "demo"
    timestamp: int
    tx_date: str
    tx_month: str
    description: str
    mcc: int
    category: str
    amount_minor: int
    amount_uah: float
    operation_amount_minor: int
    currency_code: int = 980
    cashback_uah: float = 0.0
    balance_uah: float = 0.0
    is_expense: bool = True
    comment: str = ""


class CurrencyRate(BaseModel):
    """Monobank currency exchange rate entry."""

    currency_a: str
    currency_b: str
    pair: str
    rate_buy: float | None = None
    rate_sell: float | None = None
    rate_cross: float | None = None
    updated_at: int


class CategorySpend(BaseModel):
    """Aggregated category spending metrics and budget utilization."""

    category: str
    total_uah: float
    tx_count: int
    share_pct: float
    budget_limit_uah: float | None = None
    budget_used_pct: float | None = None


class DailySpend(BaseModel):
    """Daily aggregated expenses and income."""

    date: str
    expense_uah: float
    income_uah: float


class SubscriptionItem(BaseModel):
    """Detected recurring subscription pattern."""

    merchant: str
    category: str
    avg_amount_uah: float
    annual_cost_uah: float
    occurrences: int
    avg_interval_days: float
    last_charge_date: str
    next_expected_date: str


class AnomalyItem(BaseModel):
    """Flagged anomalously high expense transaction."""

    tx_id: str
    tx_date: str
    description: str
    category: str
    amount_uah: float
    category_avg_uah: float
    ratio_to_avg: float
    reason: str


class BudgetForecast(BaseModel):
    """Current month burn rate and end-of-month projection."""

    current_month: str
    days_elapsed: int
    days_in_month: int
    spent_so_far_uah: float
    daily_burn_rate_uah: float
    projected_month_total_uah: float
    previous_month_total_uah: float
    total_monthly_budget_uah: float
    projected_delta_vs_budget_uah: float
    safe_daily_allowance_uah: float
    status: Literal["on_track", "warning", "over_budget"]


class DashboardSummary(BaseModel):
    """Full dashboard analytics payload."""

    mode: str
    period_days: int
    total_transactions: int
    total_expense_uah: float
    total_income_uah: float
    net_cashflow_uah: float
    total_cashback_uah: float
    current_balance_uah: float
    categories: list[CategorySpend]
    daily_series: list[DailySpend]
    subscriptions: list[SubscriptionItem]
    anomalies: list[AnomalyItem]
    forecast: BudgetForecast
    currency_rates: list[CurrencyRate]


class ToolTrace(BaseModel):
    """Execution trace for a single agent tool call."""

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    sql_query: str | None = None
    result_preview: Any = None
    duration_ms: float
    status: str = "ok"


class ChatMessage(BaseModel):
    """Single message in conversation history."""

    role: Literal["user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    """Incoming user prompt for MonoPilot AI agent."""

    message: str
    history: list[ChatMessage] = Field(default_factory=list)
    days_context: int = 30


class ChatResponse(BaseModel):
    """Agent answer alongside full tool execution traces."""

    answer: str
    tool_traces: list[ToolTrace] = Field(default_factory=list)
    model: str
    total_latency_ms: float
    mode: str
    used_fallback: bool = False


class SyncMonobankRequest(BaseModel):
    """Request payload for syncing live Monobank statements."""

    days: int = 31
    account_id: str = "0"


class SyncResponse(BaseModel):
    """Result of demo seeding or Monobank statement sync."""

    status: str
    source: str
    synced_count: int
    total_in_db: int
    message: str


class BudgetItem(BaseModel):
    """Configured monthly spending budget for a category."""

    category: str
    monthly_limit_uah: float


class BudgetUpdateRequest(BaseModel):
    """Request payload for creating or updating a category monthly limit."""

    category: str
    monthly_limit_uah: float = Field(..., ge=0)


class HealthResponse(BaseModel):
    """Service health and configuration summary."""

    status: str
    model: str
    gemini_configured: bool
    active_mode: str
    total_transactions: int
