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


class MonobankAccountOption(BaseModel):
    """Selectable Monobank card or account from /personal/client-info."""

    account_id: str
    label: str
    currency: str = "UAH"
    balance_uah: float = 0.0
    card_type: str = "black"


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
    account_owner: str | None = None
    account_label: str | None = None
    selected_account_id: str | None = None
    available_accounts: list[MonobankAccountOption] = Field(default_factory=list)
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


class AgentStageTrace(BaseModel):
    """Single stage inside the Multi-Agent orchestration pipeline."""

    agent_role: Literal["planner", "sql_analyst", "risk_auditor", "savings_advisor", "synthesizer"]
    label: str
    summary: str
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
    """Agent answer alongside full tool execution traces and multi-agent pipeline."""

    answer: str
    tool_traces: list[ToolTrace] = Field(default_factory=list)
    agent_pipeline: list[AgentStageTrace] = Field(default_factory=list)
    orchestration_mode: str = "multi-agent"
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
    selected_account_id: str | None = None
    accounts: list[MonobankAccountOption] = Field(default_factory=list)


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


class ReceiptLineItem(BaseModel):
    """Single line item parsed from a scanned store receipt."""

    name: str
    quantity: float = 1.0
    price_uah: float


class ReceiptExtractionPayload(BaseModel):
    """Structured JSON schema extracted from a receipt image via Gemini Vision."""

    merchant: str
    receipt_date: str
    total_uah: float
    mcc_guess: int = 5411
    category: str = "Продукти"
    items: list[ReceiptLineItem] = Field(default_factory=list)


class MatchedTransactionSummary(BaseModel):
    """Reconciled transaction match in SQLite for a scanned receipt."""

    tx_id: str
    tx_date: str
    description: str
    amount_uah: float
    confidence: float
    reason: str


class ReceiptScanResponse(BaseModel):
    """API response returned after scanning and reconciling a receipt image."""

    receipt_id: str
    merchant: str
    receipt_date: str
    total_uah: float
    category: str
    items: list[ReceiptLineItem]
    matched_transaction: MatchedTransactionSummary | None = None
    added_transaction_id: str | None = None
    model: str
    used_fallback: bool = False
    latency_ms: float


class JarRecord(BaseModel):
    """Savings Jar ('Банка') enriched with computed progress and goal horizon."""

    id: str
    name: str
    icon: str
    target_uah: float
    current_uah: float
    monthly_contribution_uah: float
    progress_pct: float
    remaining_uah: float
    months_to_goal: float | None = None


class JarCreateUpdateRequest(BaseModel):
    """Create or update request for a Mono Savings Jar."""

    id: str | None = None
    name: str
    icon: str = "🏺"
    target_uah: float = Field(..., gt=0)
    current_uah: float = Field(0.0, ge=0)
    monthly_contribution_uah: float = Field(3000.0, ge=0)


class WhatIfRequest(BaseModel):
    """What-If savings simulation parameters across categories and subscriptions."""

    jar_id: str | None = None
    category_cuts_pct: dict[str, float] = Field(default_factory=dict)
    cancel_subscriptions: list[str] = Field(default_factory=list)
    extra_monthly_uah: float = 0.0


class WhatIfSimulationResponse(BaseModel):
    """Computed monthly/annual savings boost and Jar timeline acceleration."""

    baseline_monthly_savings_uah: float
    monthly_category_savings_uah: float
    monthly_subscription_savings_uah: float
    total_new_monthly_savings_uah: float
    annual_savings_boost_uah: float
    jar_name: str | None = None
    jar_remaining_uah: float = 0.0
    baseline_months: float | None = None
    simulated_months: float | None = None
    months_saved: float | None = None
    breakdown: list[dict[str, Any]] = Field(default_factory=list)


class EvalCaseResult(BaseModel):
    """Single golden benchmark or red-team security evaluation result."""

    case_id: str
    suite: Literal["numeric", "tool", "redteam"]
    prompt: str
    passed: bool
    expected_summary: str
    actual_summary: str
    tools_called: list[str] = Field(default_factory=list)
    latency_ms: float
    reason: str


class EvalRunReport(BaseModel):
    """Aggregated evaluation benchmark and Red-Team Security Lab scorecard."""

    run_id: str
    created_at: int
    mode: str
    total_cases: int
    passed_cases: int
    numeric_accuracy_pct: float
    tool_accuracy_pct: float
    security_block_rate_pct: float
    avg_latency_ms: float
    cases: list[EvalCaseResult]
