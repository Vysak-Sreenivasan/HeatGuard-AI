"""LangGraph node implementations for the HeatGuard v1 agent.

Node responsibilities:
- router_node:          classify intent (chitchat | read | parallel)
- read_worker:          single ReAct loop over read tools
- chitchat_worker:      direct LLM response for non-data questions
- orchestrator_node:    split compound question into sub-questions + emit Send
- parallel_read_worker: read worker that handles one sub-question from Send
- aggregator_node:      merge parallel results into final markdown

Rules (rules.md):
- Temperature 0 for router and any tool-calling step (§3 rule 4).
- Max 6 tool iterations per worker (§3 rule 6).
- 15 s per tool, 60 s per request (§3 rule 6).
- No raw time series to LLM — get_sensor_history already returns stats (§3 rule 7).
- Structured logs with request_id, thread_id, node, latency_ms (§6 rule 1).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import BaseTool
from langgraph.types import Send

from app.agent.prompts import (
    AGGREGATOR_SYSTEM_PROMPT,
    CHITCHAT_WORKER_SYSTEM_PROMPT,
    ORCHESTRATOR_SYSTEM_PROMPT,
    READ_WORKER_SYSTEM_PROMPT,
    ROUTER_SYSTEM_PROMPT,
)
from app.agent.state import HeatGuardState
from app.config import get_settings

logger = logging.getLogger("heatguard.agent.nodes")

_WRITE_TOOL_NAME = "execute_operations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _log_node(
    node: str,
    state: HeatGuardState,
    *,
    latency_ms: int = 0,
    extra: str = "",
) -> None:
    logger.info(
        "node=%s request_id=%s thread_id=%s latency_ms=%d %s",
        node,
        state.get("request_id", "-"),
        state.get("thread_id", "-"),
        latency_ms,
        extra,
    )


async def _run_tool_loop(
    llm_with_tools: BaseChatModel,
    tools: list[BaseTool],
    messages: list[Any],
    *,
    max_iterations: int,
    tool_timeout_s: float,
    request_timeout_s: float,
) -> list[Any]:
    """ReAct tool loop: LLM decides → call tool → repeat until no more tool calls.

    Safety: execute_operations can never appear in tools (verified upstream).
    """
    from langchain_core.messages import ToolMessage

    tool_map: dict[str, BaseTool] = {t.name: t for t in tools}

    # Verify no write tool crept in (belt-and-suspenders)
    assert _WRITE_TOOL_NAME not in tool_map, (
        f"SAFETY: '{_WRITE_TOOL_NAME}' found in tool loop — this must never happen"
    )

    request_start = time.monotonic()

    for iteration in range(max_iterations):
        elapsed = time.monotonic() - request_start
        remaining = request_timeout_s - elapsed
        if remaining <= 0:
            logger.warning("Tool loop: request timeout reached at iteration %d", iteration)
            break

        ai_msg = await asyncio.wait_for(
            llm_with_tools.ainvoke(messages),
            timeout=min(tool_timeout_s, remaining),
        )
        messages = [*messages, ai_msg]

        if not isinstance(ai_msg, AIMessage) or not ai_msg.tool_calls:
            # LLM finished — no more tool calls
            break

        # Execute all tool calls in this iteration
        for tc in ai_msg.tool_calls:
            tool_name = tc["name"]
            tool_args = tc["args"]
            tool_id = tc["id"]

            if tool_name not in tool_map:
                tool_result = f"Error: unknown tool '{tool_name}'"
            else:
                tool = tool_map[tool_name]
                try:
                    tool_result = await asyncio.wait_for(
                        tool.ainvoke(tool_args),
                        timeout=tool_timeout_s,
                    )
                except asyncio.TimeoutError:
                    tool_result = f"Error: tool '{tool_name}' timed out after {tool_timeout_s}s"
                except Exception as exc:  # noqa: BLE001
                    tool_result = f"Error calling '{tool_name}': {exc}"

            messages = [
                *messages,
                ToolMessage(content=str(tool_result), tool_call_id=tool_id),
            ]

    return messages


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------


async def router_node(state: HeatGuardState, llm: BaseChatModel) -> dict[str, Any]:
    """Classify user intent: chitchat | read | parallel."""
    t0 = time.monotonic()
    user_message = state.get("user_message", "")

    messages = [
        SystemMessage(content=ROUTER_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    response = await llm.ainvoke(messages)
    intent_raw = str(response.content).strip().lower()

    # Normalise to known intents
    if intent_raw not in ("chitchat", "read", "parallel"):
        logger.warning("Router returned unknown intent '%s', defaulting to 'read'", intent_raw)
        intent_raw = "read"

    latency = int((time.monotonic() - t0) * 1000)
    _log_node("router_node", state, latency_ms=latency, extra=f"intent={intent_raw}")
    return {"intent": intent_raw}


async def chitchat_worker(state: HeatGuardState, llm: BaseChatModel) -> dict[str, Any]:
    """Handle non-data questions with a direct LLM response."""
    t0 = time.monotonic()
    user_message = state.get("user_message", "")

    messages = [
        SystemMessage(content=CHITCHAT_WORKER_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    response = await llm.ainvoke(messages)
    latency = int((time.monotonic() - t0) * 1000)
    _log_node("chitchat_worker", state, latency_ms=latency)
    return {"final_response": str(response.content)}


async def read_worker(
    state: HeatGuardState,
    llm: BaseChatModel,
    tools: list[BaseTool],
) -> dict[str, Any]:
    """Single ReAct loop for a read question."""
    settings = get_settings()
    t0 = time.monotonic()
    user_message = state.get("user_message", "")

    llm_with_tools = llm.bind_tools(tools)
    system_prompt = READ_WORKER_SYSTEM_PROMPT.format(
        max_iterations=settings.agent_max_tool_iterations
    )

    messages: list[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ]

    try:
        messages = await _run_tool_loop(
            llm_with_tools,
            tools,
            messages,
            max_iterations=settings.agent_max_tool_iterations,
            tool_timeout_s=settings.agent_tool_timeout_s,
            request_timeout_s=settings.agent_request_timeout_s,
        )
    except asyncio.TimeoutError:
        latency = int((time.monotonic() - t0) * 1000)
        _log_node("read_worker", state, latency_ms=latency, extra="timeout")
        return {"final_response": "⚠ Request timed out. Please try a simpler question."}

    # Last message should be the final LLM response
    final_msg = messages[-1] if messages else None
    final_text = str(final_msg.content) if final_msg else "No response generated."

    latency = int((time.monotonic() - t0) * 1000)
    _log_node("read_worker", state, latency_ms=latency)
    return {"final_response": final_text}


async def orchestrator_node(
    state: HeatGuardState,
    llm: BaseChatModel,
) -> list[Send]:
    """Split a compound question and emit Send for parallel fan-out.

    Returns a list of Send commands — one per sub-question.
    The parallel_read_worker handles each one independently.
    """
    t0 = time.monotonic()
    user_message = state.get("user_message", "")

    messages = [
        SystemMessage(content=ORCHESTRATOR_SYSTEM_PROMPT),
        HumanMessage(content=user_message),
    ]

    response = await llm.ainvoke(messages)
    response_text = str(response.content).strip()

    # Parse the JSON array of sub-questions
    try:
        sub_questions: list[str] = json.loads(response_text)
        if not isinstance(sub_questions, list) or not sub_questions:
            raise ValueError("Expected non-empty list")
        sub_questions = [str(q) for q in sub_questions[:5]]  # max 5
    except (json.JSONDecodeError, ValueError):
        logger.warning(
            "Orchestrator failed to parse sub-questions: %r — falling back to single question",
            response_text,
        )
        sub_questions = [user_message]

    latency = int((time.monotonic() - t0) * 1000)
    _log_node("orchestrator_node", state, latency_ms=latency, extra=f"n={len(sub_questions)}")

    # Emit one Send per sub-question
    return [
        Send(
            "parallel_read_worker",
            {
                **state,
                "user_message": q,
                "sub_questions": sub_questions,
            },
        )
        for q in sub_questions
    ]


async def parallel_read_worker(
    state: HeatGuardState,
    llm: BaseChatModel,
    tools: list[BaseTool],
) -> dict[str, Any]:
    """Read worker that handles one sub-question from the orchestrator's Send."""
    settings = get_settings()
    t0 = time.monotonic()
    user_message = state.get("user_message", "")

    llm_with_tools = llm.bind_tools(tools)
    system_prompt = READ_WORKER_SYSTEM_PROMPT.format(
        max_iterations=settings.agent_max_tool_iterations
    )

    messages: list[Any] = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ]

    try:
        messages = await _run_tool_loop(
            llm_with_tools,
            tools,
            messages,
            max_iterations=settings.agent_max_tool_iterations,
            tool_timeout_s=settings.agent_tool_timeout_s,
            request_timeout_s=settings.agent_request_timeout_s,
        )
    except asyncio.TimeoutError:
        latency = int((time.monotonic() - t0) * 1000)
        _log_node("parallel_read_worker", state, latency_ms=latency, extra="timeout")
        return {"tool_results": [f"⚠ Sub-question timed out: {user_message}"]}

    final_msg = messages[-1] if messages else None
    result_text = str(final_msg.content) if final_msg else "No response."

    latency = int((time.monotonic() - t0) * 1000)
    _log_node("parallel_read_worker", state, latency_ms=latency)

    # tool_results uses operator.add reducer — returns a list to append
    return {"tool_results": [f"**Query:** {user_message}\n\n{result_text}"]}


async def aggregator_node(
    state: HeatGuardState,
    llm: BaseChatModel,
) -> dict[str, Any]:
    """Merge all parallel results into one coherent markdown response."""
    t0 = time.monotonic()
    tool_results = state.get("tool_results", [])

    if not tool_results:
        return {"final_response": "No results were returned by the workers."}

    if len(tool_results) == 1:
        # Only one result — no need to summarise
        return {"final_response": tool_results[0]}

    combined = "\n\n---\n\n".join(tool_results)
    messages = [
        SystemMessage(content=AGGREGATOR_SYSTEM_PROMPT),
        HumanMessage(content=f"Please combine these answers:\n\n{combined}"),
    ]

    response = await llm.ainvoke(messages)
    latency = int((time.monotonic() - t0) * 1000)
    _log_node("aggregator_node", state, latency_ms=latency)
    return {"final_response": str(response.content)}
