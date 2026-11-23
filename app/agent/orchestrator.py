"""Multi-agent orchestration pipeline, security guard, and live SSE streaming for MonoPilot AI."""

from __future__ import annotations

import asyncio
import contextlib
import re
import time
from collections.abc import AsyncIterator
from typing import Any, Literal

from app.agent.mono_agent import MonoPilotAgent
from app.schemas import AgentStageTrace, ChatMessage, ChatResponse, ToolTrace

SpecialistRole = Literal["sql_analyst", "risk_auditor", "savings_advisor"]

_SECURITY_REFUSAL_MESSAGE = (
    "Запит заблоковано політикою безпеки (Read-Only SQL Guard): "
    "модифікація бази даних або зміна системних правил заборонені."
)

_DANGEROUS_SQL_PATTERN = re.compile(
    r"\b("
    r"DROP\s+(?:TABLE|DATABASE|VIEW|INDEX)|"
    r"DELETE\s+FROM|"
    r"TRUNCATE\s+(?:TABLE\s+)?\w+|"
    r"ALTER\s+TABLE|"
    r"ATTACH\s+DATABASE|"
    r"DETACH\s+DATABASE|"
    r"INSERT\s+(?:OR\s+\w+\s+)?INTO|"
    r"UPDATE\s+(?:transactions|budgets|jars|receipts|sync_meta|eval_runs)\s+SET|"
    r"UPDATE\s+\w+\s+SET\s+\w+\s*=|"
    r"CREATE\s+(?:TABLE|TRIGGER)|"
    r"REPLACE\s+INTO|"
    r"PRAGMA\s+\w+|"
    r"LOAD_EXTENSION"
    r")\b",
    flags=re.IGNORECASE,
)

_STACKED_SQL_PATTERN = re.compile(
    r";\s*(?:DROP|DELETE|TRUNCATE|ALTER|INSERT|UPDATE|CREATE|ATTACH|DETACH|PRAGMA)\b",
    flags=re.IGNORECASE,
)

_PROMPT_OVERRIDE_PATTERN = re.compile(
    r"("
    r"ignore\s+(?:all\s+)?(?:previous|prior|above|system)\s+(?:instructions?|rules?|prompt)|"
    r"disregard\s+(?:all\s+)?(?:previous|prior|system)\s+(?:instructions?|rules?)|"
    r"forget\s+(?:all\s+)?(?:previous|prior)\s+(?:instructions?|rules?)|"
    r"override\s+system\s+(?:prompt|instructions?|rules?)|"
    r"bypass\s+(?:security|read-only|sql\s+guard)|"
    r"забудь\s+(?:всі\s+)?(?:попередні\s+)?(?:інструкції|правила)|"
    r"ігноруй\s+(?:всі\s+)?(?:попередні\s+)?(?:інструкції|правила|обмеження)|"
    r"проігноруй\s+(?:всі\s+)?(?:попередні\s+)?(?:інструкції|правила)"
    r")",
    flags=re.IGNORECASE,
)

_SQL_MUTATION_OR_LEAK_PATTERN = re.compile(
    r"\b("
    r"drop|delete|truncate|alter|insert|update|attach|detach|create|replace|"
    r"pragma|vacuum|execute|sql|table|database|"
    r"видали\w*|очист\w*|зміни\w*|знищ\w*|перезапиш\w*|модифіку\w*|обнул\w*|"
    r"таблиц\w*|баз[ауи]|системн\w*\s+промпт|system\s+prompt|api[_\s]?key"
    r")\b",
    flags=re.IGNORECASE,
)

_NATURAL_DB_DESTRUCTION_PATTERN = re.compile(
    r"\b(?:видали(?:ти)?|очист(?:и|ити)|знищ(?:и|ити)|зміни(?:ти)?)\s+"
    r"(?:всі\s+)?(?:дані|рядки|записи|транзакці[їй]|таблиц[юі])\s+"
    r"(?:з|у|в)\s+(?:баз[іи]|таблиц[іи]|sqlite|db|transactions|budgets|jars)\b",
    flags=re.IGNORECASE,
)

_SPECIALIST_SPECS: dict[SpecialistRole, dict[str, str]] = {
    "sql_analyst": {
        "agent_role": "sql_analyst",
        "label": "SQL Analyst Agent",
        "summary": "Агрегація транзакцій, категорій та курсів через Read-Only SQL",
    },
    "risk_auditor": {
        "agent_role": "risk_auditor",
        "label": "Risk & Subscriptions Auditor",
        "summary": "Аудит регулярних списань, аномалій та SQL-безпеки",
    },
    "savings_advisor": {
        "agent_role": "savings_advisor",
        "label": "Mono Jars & Savings Strategist",
        "summary": "Моделювання бюджету, банок та What-If сценаріїв",
    },
}

_TOOL_ROLE_MAP: dict[str, SpecialistRole] = {
    "run_sql_analytics": "sql_analyst",
    "get_spending_summary": "sql_analyst",
    "get_monobank_currency_rates": "sql_analyst",
    "detect_subscriptions": "risk_auditor",
    "detect_spending_anomalies": "risk_auditor",
    "forecast_monthly_budget": "savings_advisor",
    "get_mono_jars": "savings_advisor",
    "simulate_what_if_savings": "savings_advisor",
}

_SQL_ANALYST_KEYWORDS: tuple[str, ...] = (
    "витрат",
    "дохід",
    "надходж",
    "транзакц",
    "категор",
    "мерчант",
    "магазин",
    "топ",
    "скільки",
    "сума",
    "середн",
    "баланс",
    "кешбек",
    "курс",
    "валют",
    "долар",
    "євро",
    "злотий",
    "usd",
    "eur",
    "pln",
    "gbp",
    "sql",
    "select",
    "звіт",
    "статистик",
    "spend",
    "summary",
    "currency",
    "rate",
)

_RISK_AUDITOR_KEYWORDS: tuple[str, ...] = (
    "підписк",
    "регуляр",
    "сервіс",
    "subscription",
    "recurring",
    "netflix",
    "spotify",
    "youtube",
    "apple",
    "icloud",
    "аномал",
    "великі",
    "незвич",
    "підозріл",
    "викид",
    "сплеск",
    "anomaly",
    "anomalies",
    "outlier",
    "unusual",
    "large",
    "ризик",
    "аудит",
    "безпек",
    "шахра",
    "скасувати",
    "відмовитись",
)

_SAVINGS_ADVISOR_KEYWORDS: tuple[str, ...] = (
    "бюджет",
    "прогноз",
    "ліміт",
    "темп",
    "burn",
    "forecast",
    "budget",
    "allowance",
    "банк",
    "накопич",
    "ціль",
    "цілі",
    "macbook",
    "подушк",
    "відпуст",
    "карпат",
    "jar",
    "що якщо",
    "what-if",
    "what if",
    "скоротити",
    "зменшити",
    "скасувати",
    "відмовитись",
    "швидше",
    "заощад",
    "зеконом",
    "оптиміз",
)


def is_prompt_injection_attempt(message: str) -> tuple[bool, str | None]:
    """Detect SQL DML/DDL injection or prompt-override manipulation attempts."""
    text = (message or "").strip()
    if not text:
        return False, None

    if _DANGEROUS_SQL_PATTERN.search(text) or _STACKED_SQL_PATTERN.search(text):
        return True, _SECURITY_REFUSAL_MESSAGE

    if _PROMPT_OVERRIDE_PATTERN.search(text) and _SQL_MUTATION_OR_LEAK_PATTERN.search(text):
        return True, _SECURITY_REFUSAL_MESSAGE

    if _NATURAL_DB_DESTRUCTION_PATTERN.search(text):
        return True, _SECURITY_REFUSAL_MESSAGE

    return False, None


def plan_subagents(message: str) -> list[dict[str, str]]:
    """Select active specialist sub-agents based on user query intent."""
    query = (message or "").lower()
    selected: list[SpecialistRole] = []

    if any(kw in query for kw in _SQL_ANALYST_KEYWORDS):
        selected.append("sql_analyst")
    if any(kw in query for kw in _RISK_AUDITOR_KEYWORDS):
        selected.append("risk_auditor")
    if any(kw in query for kw in _SAVINGS_ADVISOR_KEYWORDS):
        selected.append("savings_advisor")

    if not selected:
        selected.append("sql_analyst")

    return [dict(_SPECIALIST_SPECS[role]) for role in selected]


def _chunk_text(text: str, max_chunk_chars: int = 32) -> list[str]:
    """Split answer text into concise streaming chunks while preserving exact content."""
    if not text:
        return []
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for token in re.findall(r"\S+\s*|\s+", text):
        if current and current_len + len(token) > max_chunk_chars:
            chunks.append("".join(current))
            current = [token]
            current_len = len(token)
        else:
            current.append(token)
            current_len += len(token)
    if current:
        chunks.append("".join(current))
    return chunks


def _build_specialist_stages(
    planned: list[dict[str, str]],
    tool_traces: list[ToolTrace],
) -> list[AgentStageTrace]:
    """Map planned sub-agents and executed tool traces into completed AgentStageTrace items."""
    ordered_roles: list[SpecialistRole] = []
    for spec in planned:
        role = spec["agent_role"]  # type: ignore[assignment]
        if role not in ordered_roles:
            ordered_roles.append(role)

    for trace in tool_traces:
        mapped_role = _TOOL_ROLE_MAP.get(trace.tool_name, "sql_analyst")
        if mapped_role not in ordered_roles:
            ordered_roles.append(mapped_role)

    stages: list[AgentStageTrace] = []
    for role in ordered_roles:
        spec = _SPECIALIST_SPECS[role]
        role_traces = [
            trace
            for trace in tool_traces
            if _TOOL_ROLE_MAP.get(trace.tool_name, "sql_analyst") == role
        ]
        if role_traces:
            tool_names = ", ".join(trace.tool_name for trace in role_traces)
            duration_ms = round(sum(trace.duration_ms for trace in role_traces), 2)
            all_errored = all(trace.status == "error" for trace in role_traces)
            status = "error" if all_errored else "ok"
            summary = f"Виконано {len(role_traces)} інструм.: {tool_names}"
        else:
            duration_ms = 0.5
            status = "ok"
            summary = spec["summary"]

        stages.append(
            AgentStageTrace(
                agent_role=role,
                label=spec["label"],
                summary=summary,
                duration_ms=duration_ms,
                status=status,
            )
        )
    return stages


class MultiAgentOrchestrator:
    """Multi-agent coordinator providing planning, security guard, and live SSE streaming."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        db_path: str | None = None,
        client: Any = None,
    ) -> None:
        self.agent = MonoPilotAgent(
            api_key=api_key,
            model=model,
            db_path=db_path,
            client=client,
        )
        self.api_key = self.agent.api_key
        self.model = self.agent.model
        self.db_path = self.agent.db_path

    def _build_planner_stage(
        self,
        planned: list[dict[str, str]],
        blocked: bool,
        duration_ms: float,
    ) -> AgentStageTrace:
        """Create the initial Planner AgentStageTrace summary."""
        if blocked:
            summary = "Виявлено небезпечний SQL/Prompt-Injection патерн — передано до Risk Auditor."
        else:
            labels = ", ".join(spec["label"] for spec in planned)
            summary = f"Активовано спеціалістів: {labels}."
        return AgentStageTrace(
            agent_role="planner",
            label="Planner",
            summary=summary,
            duration_ms=duration_ms,
            status="ok",
        )

    def _build_blocked_response(
        self,
        planner_stage: AgentStageTrace,
        refusal_message: str,
        active_mode: str,
        started: float,
    ) -> tuple[AgentStageTrace, ChatResponse]:
        """Build Risk Auditor blocked stage and security refusal ChatResponse."""
        risk_stage = AgentStageTrace(
            agent_role="risk_auditor",
            label=_SPECIALIST_SPECS["risk_auditor"]["label"],
            summary=refusal_message,
            duration_ms=0.1,
            status="blocked",
        )
        total_latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        response = ChatResponse(
            answer=refusal_message,
            tool_traces=[],
            agent_pipeline=[planner_stage, risk_stage],
            orchestration_mode="multi-agent",
            model=self.agent.model,
            total_latency_ms=total_latency_ms,
            mode=active_mode,
            used_fallback=False,
        )
        return risk_stage, response

    def _build_final_response(
        self,
        planner_stage: AgentStageTrace,
        planned: list[dict[str, str]],
        agent_response: ChatResponse,
        started: float,
    ) -> tuple[list[AgentStageTrace], AgentStageTrace, ChatResponse]:
        """Build specialist stages, Synthesizer stage, and final multi-agent ChatResponse."""
        specialist_stages = _build_specialist_stages(planned, agent_response.tool_traces)
        total_latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        specialist_sum = sum(s.duration_ms for s in specialist_stages)
        synth_duration_ms = round(
            max(0.2, total_latency_ms - planner_stage.duration_ms - specialist_sum),
            2,
        )
        synth_stage = AgentStageTrace(
            agent_role="synthesizer",
            label="Synthesizer",
            summary=(
                f"Синтезовано фінальну відповідь ({len(agent_response.tool_traces)} "
                f"інструм., {len(agent_response.answer)} символів)."
            ),
            duration_ms=synth_duration_ms,
            status="ok",
        )
        pipeline = [planner_stage, *specialist_stages, synth_stage]
        final_response = ChatResponse(
            answer=agent_response.answer,
            tool_traces=agent_response.tool_traces,
            agent_pipeline=pipeline,
            orchestration_mode="multi-agent",
            model=agent_response.model,
            total_latency_ms=total_latency_ms,
            mode=agent_response.mode,
            used_fallback=agent_response.used_fallback,
        )
        return specialist_stages, synth_stage, final_response

    async def run(
        self,
        message: str,
        conversation_history: list[ChatMessage] | None = None,
        days_context: int = 30,
    ) -> ChatResponse:
        """Execute full multi-agent pipeline and return ChatResponse with agent_pipeline."""
        started = time.perf_counter()
        planner_started = time.perf_counter()
        blocked, refusal = is_prompt_injection_attempt(message)
        planned = plan_subagents(message)
        planner_ms = round((time.perf_counter() - planner_started) * 1000.0, 2)
        planner_stage = self._build_planner_stage(planned, blocked=blocked, duration_ms=planner_ms)

        if blocked:
            active_mode = await self.agent._get_active_mode()
            _, blocked_response = self._build_blocked_response(
                planner_stage=planner_stage,
                refusal_message=refusal or _SECURITY_REFUSAL_MESSAGE,
                active_mode=active_mode,
                started=started,
            )
            return blocked_response

        agent_response = await self.agent.ask(
            message=message,
            conversation_history=conversation_history,
            days_context=days_context,
        )
        _, _, final_response = self._build_final_response(
            planner_stage=planner_stage,
            planned=planned,
            agent_response=agent_response,
            started=started,
        )
        return final_response

    async def stream(
        self,
        message: str,
        conversation_history: list[ChatMessage] | None = None,
        days_context: int = 30,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream live stage, tool, delta, and done SSE events while orchestrating execution."""
        started = time.perf_counter()
        planner_started = time.perf_counter()
        blocked, refusal = is_prompt_injection_attempt(message)
        planned = plan_subagents(message)
        planner_ms = round((time.perf_counter() - planner_started) * 1000.0, 2)
        planner_stage = self._build_planner_stage(planned, blocked=blocked, duration_ms=planner_ms)

        yield {"event": "stage", "data": planner_stage.model_dump()}

        if blocked:
            active_mode = await self.agent._get_active_mode()
            risk_stage, blocked_response = self._build_blocked_response(
                planner_stage=planner_stage,
                refusal_message=refusal or _SECURITY_REFUSAL_MESSAGE,
                active_mode=active_mode,
                started=started,
            )
            yield {"event": "stage", "data": risk_stage.model_dump()}
            for chunk in _chunk_text(blocked_response.answer):
                yield {"event": "delta", "data": {"text": chunk}}
            yield {"event": "done", "data": blocked_response.model_dump()}
            return

        for spec in planned:
            running_stage = AgentStageTrace(
                agent_role=spec["agent_role"],  # type: ignore[arg-type]
                label=spec["label"],
                summary=spec["summary"],
                duration_ms=0.0,
                status="running",
            )
            yield {"event": "stage", "data": running_stage.model_dump()}

        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _on_tool_trace(trace: ToolTrace) -> None:
            await queue.put(("tool", trace))

        async def _run_agent() -> None:
            try:
                resp = await self.agent.ask(
                    message=message,
                    conversation_history=conversation_history,
                    days_context=days_context,
                    on_tool_trace=_on_tool_trace,
                )
                await queue.put(("result", resp))
            except Exception as exc:
                await queue.put(("error", exc))

        agent_task = asyncio.create_task(_run_agent())
        agent_response: ChatResponse | None = None
        try:
            while True:
                kind, payload = await queue.get()
                if kind == "tool":
                    trace: ToolTrace = payload
                    yield {"event": "tool", "data": trace.model_dump()}
                elif kind == "result":
                    agent_response = payload
                    break
                elif kind == "error":
                    raise payload
        finally:
            if not agent_task.done():
                agent_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await agent_task
            else:
                with contextlib.suppress(Exception):
                    await agent_task

        assert agent_response is not None
        specialist_stages, synth_stage, final_response = self._build_final_response(
            planner_stage=planner_stage,
            planned=planned,
            agent_response=agent_response,
            started=started,
        )
        for stage in specialist_stages:
            yield {"event": "stage", "data": stage.model_dump()}
        yield {"event": "stage", "data": synth_stage.model_dump()}

        for chunk in _chunk_text(final_response.answer):
            yield {"event": "delta", "data": {"text": chunk}}

        yield {"event": "done", "data": final_response.model_dump()}
