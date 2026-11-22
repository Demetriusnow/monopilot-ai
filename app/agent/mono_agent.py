"""Gemini Interactions API agent orchestrator and deterministic fallback for MonoPilot AI."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from google import genai

from app.agent.tools import TOOL_DECLARATIONS, execute_tool
from app.config import get_settings
from app.data.database import get_transaction_stats
from app.schemas import ChatMessage, ChatResponse, ToolTrace

logger = logging.getLogger(__name__)

_PLACEHOLDER_KEYS = frozenset({"", "your_gemini_api_key_here", "changeme", "none", "null"})

SYSTEM_INSTRUCTION = """Ти — MonoPilot AI, персональний банківський аналітичний AI-асистент для даних Monobank в Україні.

СХЕМА БАЗИ ДАНИХ SQLite (для інструмента `run_sql_analytics`):
1. Таблиця `transactions`:
   - `id` (TEXT) — унікальний ідентифікатор транзакції
   - `account_id` (TEXT) — рахунок
   - `source` (TEXT) — джерело (`'demo'` або `'monobank'`)
   - `timestamp` (INTEGER) — Unix-час у секундах
   - `tx_date` (TEXT) — дата у форматі `YYYY-MM-DD`
   - `tx_month` (TEXT) — місяць у форматі `YYYY-MM`
   - `description` (TEXT) — назва мерчанта або опис операції
   - `mcc` (INTEGER) — код категорії мерчанта (MCC)
   - `category` (TEXT) — категорія українською: `'Продукти'`, `'Кафе та ресторани'`, `'Транспорт та авто'`, `'Зв''язок та інтернет'`, `'Аптеки та здоров''я'`, `'Підписки та сервіси'`, `'Пошта та доставка'`, `'Техніка та електроніка'`, `'Одяг та взуття'`, `'Спорт та фітнес'`, `'Покупки та хобі'`, `'Перекази'`, `'Дохід та поповнення'`, `'Інше'`
   - `amount_minor` (INTEGER) — сума у копійках
   - `amount_uah` (REAL) — сума в гривнях. КРИТИЧНО: для витрат (`is_expense = 1`) значення `amount_uah` ВІД'ЄМНІ! Тому для суми витрат ЗАВЖДИ використовуй `ROUND(SUM(ABS(amount_uah)), 2)`, а для доходів (`is_expense = 0`) — `ROUND(SUM(amount_uah), 2)`.
   - `cashback_uah` (REAL) — нарахований кешбек у грн
   - `balance_uah` (REAL) — залишок після операції у грн
   - `is_expense` (INTEGER) — `1` для витрат, `0` для надходжень
2. Таблиця `budgets`:
   - `category` (TEXT) — назва категорії
   - `monthly_limit_uah` (REAL) — місячний ліміт у грн

ПРАВИЛА РОБОТИ (СУВОРІ):
1. НІКОЛИ не вигадуй числа, суми, транзакції, дати, курси валют або категорії та НЕ рахуй суми в голові.
2. ЗАВЖДИ спочатку викликай потрібні інструменти (`run_sql_analytics`, `get_spending_summary`, `detect_subscriptions`, `detect_spending_anomalies`, `forecast_monthly_budget`, `get_monobank_currency_rates`) для отримання точних фактів з бази даних або Monobank API.
3. Якщо потрібно поєднати кілька зрізів даних, можеш викликати декілька інструментів паралельно або послідовно.
4. Якщо `run_sql_analytics` повернув помилку синтаксису або обмеження безпеки, виправ SQL-запит (дозволені лише `SELECT` або `WITH ... SELECT`) і повтори виклик.
5. Відповідай українською мовою (або мовою запиту користувача, якщо він пише англійською), чітко, лаконічно та структуровано (списки, ключові суми у ₴ / грн, конкретні рекомендації на основі реальних цифр).
"""


_TRANSIENT_ERROR_MARKERS: tuple[str, ...] = (
    "503",
    "429",
    "service_unavailable",
    "service unavailable",
    "unavailable",
    "high demand",
    "timeout",
    "timed out",
    "overloaded",
    "resource_exhausted",
)


def _is_missing_api_key(api_key: str) -> bool:
    """Return True when Gemini API key is absent or still set to a template placeholder."""
    normalized = api_key.strip()
    if normalized.lower() in _PLACEHOLDER_KEYS:
        return True
    return normalized.lower().startswith("your_")


def _is_transient_gemini_error(exc: Exception) -> bool:
    """Return True when an exception indicates transient capacity, rate limit, or timeout."""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status_code = getattr(exc, "status_code", None)
    if status_code in {429, 500, 502, 503, 504}:
        return True
    combined = f"{type(exc).__name__} {exc}".lower()
    return any(marker in combined for marker in _TRANSIENT_ERROR_MARKERS)


def _strip_thought_steps(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of history omitting model-specific thought steps before cross-model failover."""
    return [dict(item) for item in history if item.get("type") != "thought"]


def _step_to_dict(step: Any) -> dict[str, Any]:
    """Convert an SDK Interaction step model, dict, or mock object into a plain dictionary."""
    if hasattr(step, "model_dump"):
        try:
            dumped = step.model_dump(exclude_none=True)
        except TypeError:
            dumped = step.model_dump()
        if isinstance(dumped, dict):
            return dumped
    if isinstance(step, Mapping):
        return dict(step)

    step_dict: dict[str, Any] = {}
    for field in ("type", "id", "name", "arguments", "call_id", "result", "content", "signature", "summary"):
        if hasattr(step, field):
            val = getattr(step, field)
            if val is not None:
                step_dict[field] = val
    return step_dict


def _extract_text_from_steps(steps: list[Any]) -> str:
    """Extract concatenated text from trailing model_output steps as a fallback to output_text."""
    parts: list[str] = []
    collecting = False
    for raw_step in reversed(steps):
        step = _step_to_dict(raw_step)
        step_type = step.get("type")
        if step_type == "user_input":
            break
        if step_type == "thought":
            continue
        if step_type != "model_output":
            if collecting:
                break
            continue
        content = step.get("content")
        if not isinstance(content, list):
            continue
        for item in reversed(content):
            item_dict = _step_to_dict(item) if not isinstance(item, Mapping) else dict(item)
            if item_dict.get("type") == "text":
                collecting = True
                text_val = item_dict.get("text")
                if isinstance(text_val, str):
                    parts.append(text_val)
            elif collecting:
                break
        if collecting:
            break
    return "".join(reversed(parts)).strip()


def _parse_tool_arguments(raw_args: Any) -> dict[str, Any]:
    """Normalize tool call arguments from dict or JSON string into a dictionary."""
    if isinstance(raw_args, Mapping):
        return dict(raw_args)
    if isinstance(raw_args, str):
        try:
            parsed = json.loads(raw_args)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {}
    return {}


class MonoPilotAgent:
    """AI analytics agent powered by Gemini Interactions API with failover and local fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        db_path: str | None = None,
        client: Any = None,
    ) -> None:
        settings = get_settings()
        raw_key = api_key if api_key is not None else settings.gemini_api_key
        self.api_key = (raw_key or "").strip()
        self.model = model or settings.gemini_model
        self.fallback_models = [
            m for m in settings.fallback_model_list if m != self.model
        ]
        self.timeout_ms = settings.gemini_timeout_ms
        self.db_path = db_path or settings.database_path
        self.max_steps = settings.max_agent_steps
        self._client = client

    async def _create_interaction_with_failover(
        self,
        client: Any,
        candidate_models: list[str],
        active_idx: int,
        system_prompt: str,
        history: list[dict[str, Any]],
    ) -> tuple[Any, int, str]:
        """Call Gemini Interactions API and fail over across candidate models on transient errors."""
        last_exc: Exception | None = None
        for idx in range(active_idx, len(candidate_models)):
            current_model = candidate_models[idx]
            payload_history = _strip_thought_steps(history) if idx > active_idx else history
            try:
                interaction = await asyncio.to_thread(
                    client.interactions.create,
                    model=current_model,
                    store=False,
                    system_instruction=system_prompt,
                    input=payload_history,
                    tools=TOOL_DECLARATIONS,
                )
                return interaction, idx, current_model
            except Exception as exc:
                last_exc = exc
                if idx + 1 < len(candidate_models) and _is_transient_gemini_error(exc):
                    logger.warning(
                        "Gemini model '%s' transient error (%s); failing over to '%s'.",
                        current_model,
                        type(exc).__name__,
                        candidate_models[idx + 1],
                    )
                    continue
                raise

        assert last_exc is not None
        raise last_exc

    async def _get_active_mode(self) -> str:
        """Read the current active dataset mode ('demo', 'csv', or 'monobank') safely."""
        try:
            stats = await asyncio.to_thread(get_transaction_stats, self.db_path)
            return str(stats.get("active_mode", "demo"))
        except Exception:
            return "demo"

    async def ask(
        self,
        message: str,
        conversation_history: list[ChatMessage] | None = None,
        days_context: int = 30,
    ) -> ChatResponse:
        """Answer a user analytics query using Gemini Interactions API or local fallback."""
        if self._client is None and _is_missing_api_key(self.api_key):
            return await self._local_fallback(message, days_context)

        started = time.perf_counter()
        tool_traces: list[ToolTrace] = []
        active_mode = await self._get_active_mode()
        client = self._client or genai.Client(
            api_key=self.api_key,
            http_options={
                "timeout": self.timeout_ms,
                "retry_options": {
                    "attempts": 2,
                    "initial_delay": 0.5,
                    "max_delay": 2.0,
                },
            },
        )
        candidate_models = [self.model, *self.fallback_models]
        active_model_idx = 0
        active_model_name = candidate_models[0]

        history: list[dict[str, Any]] = []
        for msg in conversation_history or []:
            step_type = "user_input" if msg.role == "user" else "model_output"
            history.append(
                {
                    "type": step_type,
                    "content": [{"type": "text", "text": msg.content}],
                }
            )
        history.append(
            {
                "type": "user_input",
                "content": [{"type": "text", "text": message}],
            }
        )

        system_prompt = (
            f"{SYSTEM_INSTRUCTION}\n"
            f"Контекст інтерфейсу: стандартний період аналізу (days_context) = {days_context} днів."
        )

        answer = ""
        last_steps: list[Any] = []

        try:
            for _ in range(self.max_steps):
                interaction, used_idx, active_model_name = await self._create_interaction_with_failover(
                    client=client,
                    candidate_models=candidate_models,
                    active_idx=active_model_idx,
                    system_prompt=system_prompt,
                    history=history,
                )
                if used_idx > active_model_idx:
                    history[:] = _strip_thought_steps(history)
                active_model_idx = used_idx

                steps = getattr(interaction, "steps", None) or []
                last_steps = list(steps)

                function_calls: list[dict[str, Any]] = []
                for step in steps:
                    step_dict = _step_to_dict(step)
                    history.append(step_dict)
                    if step_dict.get("type") == "function_call":
                        function_calls.append(step_dict)

                if not function_calls:
                    output_text = getattr(interaction, "output_text", None)
                    if isinstance(output_text, str) and output_text.strip():
                        answer = output_text.strip()
                    else:
                        answer = _extract_text_from_steps(last_steps)
                    break

                for fc in function_calls:
                    fc_name = str(fc.get("name") or "")
                    fc_id = str(fc.get("id") or "")
                    fc_args = _parse_tool_arguments(fc.get("arguments"))
                    tool_result, trace = await execute_tool(
                        fc_name,
                        fc_args,
                        db_path=self.db_path,
                    )
                    tool_traces.append(trace)
                    history.append(
                        {
                            "type": "function_result",
                            "name": fc_name,
                            "call_id": fc_id,
                            "result": [
                                {
                                    "type": "text",
                                    "text": json.dumps(tool_result, ensure_ascii=False, default=str),
                                }
                            ],
                        }
                    )

            if not answer:
                answer = _extract_text_from_steps(last_steps)
            if not answer:
                fallback_resp = await self._local_fallback(message, days_context)
                if tool_traces:
                    fallback_resp.tool_traces = tool_traces + fallback_resp.tool_traces
                fallback_resp.total_latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
                return fallback_resp

            total_latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return ChatResponse(
                answer=answer,
                tool_traces=tool_traces,
                model=active_model_name,
                total_latency_ms=total_latency_ms,
                mode=active_mode,
                used_fallback=False,
            )
        except Exception as exc:
            logger.warning("Gemini interaction failed (%s); falling back to local mode.", type(exc).__name__)
            fallback_resp = await self._local_fallback(message, days_context)
            if tool_traces:
                fallback_resp.tool_traces = tool_traces + fallback_resp.tool_traces
            fallback_resp.answer = (
                "_(Примітка: запит до Gemini API не вдався, активовано локальний аналітичний режим.)_\n\n"
                + fallback_resp.answer
            )
            fallback_resp.total_latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
            return fallback_resp

    async def _local_fallback(self, message: str, days_context: int = 30) -> ChatResponse:
        """Deterministically route queries to real analytical tools without Gemini API."""
        started = time.perf_counter()
        query = message.lower()
        tool_traces: list[ToolTrace] = []
        sections: list[str] = []
        active_mode = await self._get_active_mode()

        wants_subs = any(
            kw in query
            for kw in (
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
            )
        )
        wants_anomalies = any(
            kw in query
            for kw in (
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
            )
        )
        wants_forecast = any(
            kw in query
            for kw in (
                "прогноз",
                "бюджет",
                "ліміт",
                "скільки можу",
                "можу витрачати",
                "до кінця місяця",
                "forecast",
                "budget",
                "burn",
                "allowance",
            )
        )
        wants_currency = any(
            kw in query
            for kw in (
                "курс",
                "валют",
                "долар",
                "євро",
                "злотий",
                "фунт",
                "usd",
                "eur",
                "pln",
                "gbp",
                "currency",
                "rate",
                "exchange",
            )
        )

        if wants_subs:
            subs_res, subs_trace = await execute_tool(
                "detect_subscriptions",
                {"min_occurrences": 2},
                db_path=self.db_path,
            )
            tool_traces.append(subs_trace)
            if isinstance(subs_res, list) and subs_res:
                monthly_total = sum(float(item.get("avg_amount_uah", 0.0)) for item in subs_res)
                annual_total = sum(float(item.get("annual_cost_uah", 0.0)) for item in subs_res)
                lines = [
                    f"**Виявлені регулярні підписки ({len(subs_res)}):** "
                    f"сумарно ~{monthly_total:,.2f} ₴/міс ({annual_total:,.2f} ₴/рік):"
                ]
                for item in subs_res:
                    lines.append(
                        f"- **{item['merchant']}** ({item['category']}): "
                        f"{float(item['avg_amount_uah']):,.2f} ₴/списання · "
                        f"{float(item['annual_cost_uah']):,.2f} ₴/рік · "
                        f"повторів: {item['occurrences']}, наступне: {item['next_expected_date']}"
                    )
                sections.append("\n".join(lines))
            else:
                sections.append("**Регулярні підписки:** за поточними транзакціями повторюваних підписок не виявлено.")

        if wants_anomalies:
            anom_res, anom_trace = await execute_tool(
                "detect_spending_anomalies",
                {"days": max(days_context, 90)},
                db_path=self.db_path,
            )
            tool_traces.append(anom_trace)
            if isinstance(anom_res, list) and anom_res:
                lines = [f"**Аномально великі витрати ({len(anom_res)}):**"]
                for item in anom_res[:8]:
                    lines.append(
                        f"- **{item['tx_date']} — {item['description']}** ({item['category']}): "
                        f"**{float(item['amount_uah']):,.2f} ₴** "
                        f"(середнє по категорії {float(item['category_avg_uah']):,.2f} ₴, "
                        f"x{float(item['ratio_to_avg']):.1f}) — {item['reason']}"
                    )
                sections.append("\n".join(lines))
            else:
                sections.append("**Аномалії витрат:** різких викидів за вибраний період не виявлено.")

        if wants_forecast:
            fc_res, fc_trace = await execute_tool(
                "forecast_monthly_budget",
                {},
                db_path=self.db_path,
            )
            tool_traces.append(fc_trace)
            if isinstance(fc_res, dict) and "error" not in fc_res:
                status_labels = {
                    "on_track": "в межах бюджету",
                    "warning": "наближення до ліміту",
                    "over_budget": "перевищення бюджету",
                }
                status_text = status_labels.get(str(fc_res.get("status")), str(fc_res.get("status")))
                delta = float(fc_res.get("projected_delta_vs_budget_uah", 0.0))
                delta_str = f"+{delta:,.2f} ₴" if delta > 0 else f"{delta:,.2f} ₴"
                sections.append(
                    "\n".join(
                        [
                            f"**Прогноз бюджету за {fc_res.get('current_month')}** "
                            f"(день {fc_res.get('days_elapsed')} з {fc_res.get('days_in_month')}, статус: **{status_text}**):",
                            f"- Витрачено з початку місяця: **{float(fc_res.get('spent_so_far_uah', 0.0)):,.2f} ₴**",
                            f"- Середній темп витрат (burn rate): **{float(fc_res.get('daily_burn_rate_uah', 0.0)):,.2f} ₴/день**",
                            f"- Прогноз до кінця місяця: **{float(fc_res.get('projected_month_total_uah', 0.0)):,.2f} ₴** "
                            f"(минулий місяць: {float(fc_res.get('previous_month_total_uah', 0.0)):,.2f} ₴)",
                            f"- Сумарний місячний бюджет: **{float(fc_res.get('total_monthly_budget_uah', 0.0)):,.2f} ₴** "
                            f"(очікувана дельта: {delta_str})",
                            f"- Безпечний денний ліміт до кінця місяця: **{float(fc_res.get('safe_daily_allowance_uah', 0.0)):,.2f} ₴/день**",
                        ]
                    )
                )

        if wants_currency:
            fx_res, fx_trace = await execute_tool(
                "get_monobank_currency_rates",
                {},
                db_path=self.db_path,
            )
            tool_traces.append(fx_trace)
            if isinstance(fx_res, list) and fx_res:
                lines = ["**Актуальні курси валют Monobank:**"]
                for rate in fx_res:
                    buy = rate.get("rate_buy")
                    sell = rate.get("rate_sell")
                    cross = rate.get("rate_cross")
                    if buy is not None and sell is not None:
                        rate_str = f"купівля **{float(buy):.2f} ₴** / продаж **{float(sell):.2f} ₴**"
                    elif cross is not None:
                        rate_str = f"крос-курс **{float(cross):.2f} ₴**"
                    else:
                        rate_str = "немає даних"
                    lines.append(f"- **{rate['pair']}**: {rate_str}")
                sections.append("\n".join(lines))
            else:
                sections.append("**Курси валют:** наразі не вдалося отримати дані Monobank.")

        if not sections:
            sum_res, sum_trace = await execute_tool(
                "get_spending_summary",
                {"days": days_context},
                db_path=self.db_path,
            )
            tool_traces.append(sum_trace)

            sql_query = (
                "SELECT category, COUNT(*) AS tx_count, ROUND(SUM(ABS(amount_uah)), 2) AS total_uah "
                "FROM transactions WHERE is_expense = 1 "
                "GROUP BY category ORDER BY total_uah DESC LIMIT 5"
            )
            sql_res, sql_trace = await execute_tool(
                "run_sql_analytics",
                {"sql": sql_query},
                db_path=self.db_path,
            )
            tool_traces.append(sql_trace)

            lines: list[str] = []
            if isinstance(sum_res, dict) and "error" not in sum_res:
                total_exp = float(sum_res.get("total_expense_uah", 0.0))
                total_inc = float(sum_res.get("total_income_uah", 0.0))
                cashback = float(sum_res.get("total_cashback_uah", 0.0))
                tx_count = int(sum_res.get("tx_count", 0))
                lines.append(f"**Фінансове зведення за останні {days_context} днів ({tx_count} операцій):**")
                lines.append(f"- Загальні витрати: **{total_exp:,.2f} ₴**")
                lines.append(f"- Загальні надходження: **{total_inc:,.2f} ₴**")
                lines.append(f"- Нарахований кешбек: **{cashback:,.2f} ₴**")

                top_cats = sum_res.get("top_categories") or []
                if top_cats:
                    lines.append("\n**Топ категорій за період:**")
                    for cat in top_cats[:5]:
                        lines.append(
                            f"- {cat.get('category')}: **{float(cat.get('total_uah', 0.0)):,.2f} ₴** "
                            f"({float(cat.get('share_pct', 0.0)):.1f}%, {cat.get('tx_count', 0)} оп.)"
                        )

                top_merchants = sum_res.get("top_merchants") or []
                if top_merchants:
                    lines.append("\n**Топ мерчантів за період:**")
                    for merch in top_merchants[:5]:
                        lines.append(
                            f"- {merch.get('description')}: **{float(merch.get('total_uah', 0.0)):,.2f} ₴** "
                            f"({merch.get('tx_count', 0)} оп.)"
                        )

            if isinstance(sql_res, dict) and sql_res.get("rows"):
                lines.append("\n**Топ категорій за весь період в БД (SQL):**")
                for row in sql_res["rows"]:
                    lines.append(
                        f"- {row['category']}: **{float(row['total_uah']):,.2f} ₴** ({row['tx_count']} оп.)"
                    )

            sections.append("\n".join(lines) if lines else "Дані транзакцій не знайдено.")

        total_latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return ChatResponse(
            answer="\n\n".join(sections),
            tool_traces=tool_traces,
            model=f"{self.model} (local-fallback)",
            total_latency_ms=total_latency_ms,
            mode=active_mode,
            used_fallback=True,
        )
