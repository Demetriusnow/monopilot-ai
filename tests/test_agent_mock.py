"""Mocked Gemini Interactions API tool-calling loop tests for MonoPilotAgent."""

from __future__ import annotations

import copy
import time
from types import SimpleNamespace
from typing import Any

import pytest

from app.agent.mono_agent import MonoPilotAgent
from app.config import get_settings
from app.data.demo_seed import load_demo_dataset
from app.schemas import CurrencyRate


class _FakeInteractionsResource:
    """Fake google-genai client.interactions resource returning queued scripted turns."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        # Deep-copy input so subsequent in-place history mutations do not alter recorded turn snapshots.
        self.calls.append(copy.deepcopy(kwargs))
        if not self._responses:
            raise AssertionError("FakeInteractionsResource exhausted scripted turns.")
        return self._responses.pop(0)


class _FakeGeminiClient:
    """Lightweight fake google.genai.Client exposing .interactions.create(...)."""

    def __init__(self, responses: list[Any]) -> None:
        self.interactions = _FakeInteractionsResource(responses)


def _make_function_call_step(call_id: str, name: str, arguments: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call",
        id=call_id,
        name=name,
        arguments=arguments,
    )


def _make_model_output_interaction(interaction_id: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=interaction_id,
        status="completed",
        steps=[
            SimpleNamespace(
                type="model_output",
                content=[SimpleNamespace(type="text", text=text)],
            )
        ],
        output_text=text,
    )


@pytest.mark.asyncio
async def test_agent_single_tool_call_executes_real_subscriptions(tmp_path) -> None:
    """Turn 1 function_call executes detect_subscriptions against temp DB and sends function_result in Turn 2."""
    db_path = str(tmp_path / "agent_single.db")
    load_demo_dataset(db_path=db_path, reset=True)

    turn1 = SimpleNamespace(
        id="int_turn_1",
        status="requires_action",
        steps=[
            _make_function_call_step(
                call_id="fc_1",
                name="detect_subscriptions",
                arguments={"min_occurrences": 2},
            )
        ],
        output_text="",
    )
    turn2 = _make_model_output_interaction(
        interaction_id="int_turn_2",
        text="Виявлено регулярні підписки: Netflix.com (449.00 ₴/міс), Spotify (199.00 ₴/міс) та інші.",
    )
    fake_client = _FakeGeminiClient([turn1, turn2])

    agent = MonoPilotAgent(
        api_key="fake-gemini-key",
        model="gemini-3.8-flash",
        db_path=db_path,
        client=fake_client,
    )
    response = await agent.ask("Знайди всі мої регулярні підписки", days_context=90)

    assert len(fake_client.interactions.calls) == 2
    turn2_input = fake_client.interactions.calls[1]["input"]
    function_results = [
        step for step in turn2_input if isinstance(step, dict) and step.get("type") == "function_result"
    ]
    assert len(function_results) == 1
    assert function_results[0]["name"] == "detect_subscriptions"
    assert function_results[0]["call_id"] == "fc_1"
    assert "Netflix" in function_results[0]["result"][0]["text"]

    assert response.used_fallback is False
    assert "Netflix" in response.answer
    assert len(response.tool_traces) == 1
    trace = response.tool_traces[0]
    assert trace.tool_name == "detect_subscriptions"
    assert trace.arguments == {"min_occurrences": 2}
    assert trace.status == "ok"
    assert trace.sql_query is None
    assert isinstance(trace.result_preview, list)
    assert len(trace.result_preview) >= 5


@pytest.mark.asyncio
async def test_agent_parallel_tool_calls_sql_and_currency(tmp_path, monkeypatch) -> None:
    """Turn 1 parallel function_calls execute SQL analytics + FX rates and capture sql_query trace."""
    db_path = str(tmp_path / "agent_parallel.db")
    load_demo_dataset(db_path=db_path, reset=True)

    now_ts = int(time.time())
    fake_rates = [
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

    async def _mock_fetch_currency_rates(*args, **kwargs):
        return fake_rates

    monkeypatch.setattr("app.agent.tools.fetch_currency_rates", _mock_fetch_currency_rates)

    sql_query = "SELECT ROUND(SUM(ABS(amount_uah)), 2) AS total_expense_uah FROM transactions WHERE is_expense = 1"
    turn1 = SimpleNamespace(
        id="int_parallel_1",
        status="requires_action",
        steps=[
            _make_function_call_step(
                call_id="fc_sql_1",
                name="run_sql_analytics",
                arguments={"sql": sql_query},
            ),
            _make_function_call_step(
                call_id="fc_fx_2",
                name="get_monobank_currency_rates",
                arguments={},
            ),
        ],
        output_text="",
    )
    turn2 = _make_model_output_interaction(
        interaction_id="int_parallel_2",
        text="Загальні витрати пораховані за SQL-вибіркою, а курс USD/UAH становить 41.20 / 41.75.",
    )
    fake_client = _FakeGeminiClient([turn1, turn2])

    agent = MonoPilotAgent(
        api_key="fake-gemini-key",
        model="gemini-3.8-flash",
        db_path=db_path,
        client=fake_client,
    )
    response = await agent.ask(
        "Який зараз курс долара та скільки моїх витрат у перерахунку в USD?",
        days_context=30,
    )

    assert len(fake_client.interactions.calls) == 2
    turn2_input = fake_client.interactions.calls[1]["input"]
    function_results = [
        step for step in turn2_input if isinstance(step, dict) and step.get("type") == "function_result"
    ]
    assert [r["call_id"] for r in function_results] == ["fc_sql_1", "fc_fx_2"]

    assert response.used_fallback is False
    assert len(response.tool_traces) == 2

    sql_trace = response.tool_traces[0]
    assert sql_trace.tool_name == "run_sql_analytics"
    assert sql_trace.status == "ok"
    assert sql_trace.sql_query == sql_query
    assert sql_trace.result_preview["row_count"] == 1
    assert sql_trace.result_preview["rows"][0]["total_expense_uah"] > 0

    fx_trace = response.tool_traces[1]
    assert fx_trace.tool_name == "get_monobank_currency_rates"
    assert fx_trace.status == "ok"
    assert any(item["pair"] == "USD/UAH" for item in fx_trace.result_preview)


class _ModelAwareFakeInteractions:
    """Fake interactions resource that accepts dynamic handler(call_kwargs)."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        snapshot = copy.deepcopy(kwargs)
        self.calls.append(snapshot)
        return self._handler(snapshot)


@pytest.mark.asyncio
async def test_agent_failover_from_503_to_fallback_model(tmp_path, monkeypatch) -> None:
    """When primary model returns 503 high-demand error, agent automatically fails over to fallback model."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite,gemini-3.1-flash-lite")
    get_settings.cache_clear()

    db_path = str(tmp_path / "agent_failover.db")
    load_demo_dataset(db_path=db_path, reset=True)

    def _handler(call_kwargs: dict[str, Any]) -> Any:
        model = call_kwargs["model"]
        if model == "gemini-3.8-flash":
            raise RuntimeError("503 Service Unavailable: high demand")
        input_steps = call_kwargs["input"]
        has_tool_result = any(
            isinstance(s, dict) and s.get("type") == "function_result"
            for s in input_steps
        )
        if not has_tool_result:
            return SimpleNamespace(
                id="int_fallback_1",
                status="requires_action",
                steps=[
                    _make_function_call_step(
                        call_id="fc_sub_1",
                        name="detect_subscriptions",
                        arguments={"min_occurrences": 2},
                    )
                ],
                output_text="",
            )
        return _make_model_output_interaction(
            interaction_id="int_fallback_2",
            text="Підписки перевірено через резервну модель: Netflix.com, Spotify та інші.",
        )

    fake_client = SimpleNamespace(interactions=_ModelAwareFakeInteractions(_handler))
    try:
        agent = MonoPilotAgent(
            api_key="fake-gemini-key",
            model="gemini-3.8-flash",
            db_path=db_path,
            client=fake_client,
        )
        response = await agent.ask("Знайди всі мої підписки")
    finally:
        get_settings.cache_clear()

    assert response.used_fallback is False
    assert response.model == "gemini-3.5-flash-lite"
    assert "Netflix" in response.answer
    assert len(response.tool_traces) == 1
    assert response.tool_traces[0].tool_name == "detect_subscriptions"
    assert [c["model"] for c in fake_client.interactions.calls] == [
        "gemini-3.8-flash",
        "gemini-3.5-flash-lite",
        "gemini-3.5-flash-lite",
    ]


@pytest.mark.asyncio
async def test_agent_mid_turn_failover_strips_thought_steps(tmp_path, monkeypatch) -> None:
    """Mid-conversation model switch removes previous model thought steps cleanly."""
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.8-flash")
    monkeypatch.setenv("GEMINI_FALLBACK_MODELS", "gemini-3.5-flash-lite")
    get_settings.cache_clear()

    db_path = str(tmp_path / "agent_mid_failover.db")
    load_demo_dataset(db_path=db_path, reset=True)

    turn1 = SimpleNamespace(
        id="int_t1",
        status="requires_action",
        steps=[
            SimpleNamespace(type="thought", signature="sig_model_a_only"),
            _make_function_call_step(
                call_id="fc_sum_1",
                name="get_spending_summary",
                arguments={"days": 30},
            ),
        ],
        output_text="",
    )

    def _handler(call_kwargs: dict[str, Any]) -> Any:
        model = call_kwargs["model"]
        input_steps = call_kwargs["input"]
        has_tool_result = any(
            isinstance(s, dict) and s.get("type") == "function_result"
            for s in input_steps
        )
        if not has_tool_result:
            return turn1
        if model == "gemini-3.8-flash":
            raise RuntimeError("503 Service Unavailable during turn 2")
        return _make_model_output_interaction(
            interaction_id="int_t2_lite",
            text="Підсумок витрат успішно пораховано після переключення моделі.",
        )

    interactions_mock = _ModelAwareFakeInteractions(_handler)
    fake_client = SimpleNamespace(interactions=interactions_mock)
    try:
        agent = MonoPilotAgent(
            api_key="fake-gemini-key",
            model="gemini-3.8-flash",
            db_path=db_path,
            client=fake_client,
        )
        response = await agent.ask("Скільки я витратив за 30 днів?")
    finally:
        get_settings.cache_clear()

    assert response.used_fallback is False
    assert response.model == "gemini-3.5-flash-lite"
    assert len(response.tool_traces) == 1
    assert interactions_mock.calls[-1]["model"] == "gemini-3.5-flash-lite"
    last_input_types = [s.get("type") for s in interactions_mock.calls[-1]["input"] if isinstance(s, dict)]
    assert "thought" not in last_input_types
    assert "function_call" in last_input_types
    assert "function_result" in last_input_types
