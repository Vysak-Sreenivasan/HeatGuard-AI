"""LangGraph agent graph builder for HeatGuard AI v1.

Graph topology:
    START → router_node
    router_node → chitchat_worker  (intent == "chitchat")
    router_node → read_worker      (intent == "read")
    router_node → orchestrator_node (intent == "parallel")
    orchestrator_node → [Send(parallel_read_worker, …), …]
    parallel_read_worker → aggregator_node  (via tool_results reducer)
    read_worker → END
    chitchat_worker → END
    aggregator_node → END

LLM selection: driven by LLM_PROVIDER config (no code change needed to switch).
"""

from __future__ import annotations

import functools
import logging
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph

from app.agent.nodes import (
    aggregator_node,
    chitchat_worker,
    dispatch_workers,
    orchestrator_node,
    parallel_read_worker,
    read_worker,
    router_node,
)
from app.agent.state import HeatGuardState
from app.config import Settings, get_settings

logger = logging.getLogger("heatguard.agent.graph")


# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------


def build_llm(settings: Settings | None = None) -> BaseChatModel:
    """Build the LLM based on LLM_PROVIDER env var (no code change to switch)."""
    cfg = settings or get_settings()

    if cfg.llm_provider == "openai":
        from langchain_openai import ChatOpenAI

        return ChatOpenAI(
            model=cfg.openai_model,
            api_key=cfg.openai_api_key,
            temperature=cfg.llm_temperature,
        )

    if cfg.llm_provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(  # type: ignore[call-arg]
            model_name=cfg.anthropic_model,
            api_key=cfg.anthropic_api_key,
            temperature=cfg.llm_temperature,
        )

    # Default: ollama
    from langchain_ollama import ChatOllama

    return ChatOllama(
        model=cfg.ollama_model,
        base_url=cfg.ollama_base_url,
        temperature=cfg.llm_temperature,
    )


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------


def build_graph(
    tools: list[BaseTool],
    llm: BaseChatModel | None = None,
    settings: Settings | None = None,
    checkpointer: Any | None = None,
) -> Any:
    """Build and compile the HeatGuard LangGraph agent.

    Args:
        tools:        Read-only LLM tools from MCPClient.get_llm_tools().
        llm:          Optional pre-built LLM (built from settings if not provided).
        settings:     Optional settings override.
        checkpointer: AsyncPostgresSaver instance for persistent checkpointing
                      (required for interrupt/resume; Phase 4.3).

    Returns:
        Compiled LangGraph graph (CompiledGraph).
    """
    cfg = settings or get_settings()
    _llm = llm or build_llm(cfg)

    builder: StateGraph = StateGraph(HeatGuardState)

    # --- Register nodes (partial-apply dependencies) ----------------------
    builder.add_node("router_node", functools.partial(router_node, llm=_llm))
    builder.add_node("chitchat_worker", functools.partial(chitchat_worker, llm=_llm))
    builder.add_node("read_worker", functools.partial(read_worker, llm=_llm, tools=tools))
    builder.add_node(
        "orchestrator_node",
        functools.partial(orchestrator_node, llm=_llm),
    )
    builder.add_node(
        "parallel_read_worker",
        functools.partial(parallel_read_worker, llm=_llm, tools=tools),
    )
    builder.add_node("aggregator_node", functools.partial(aggregator_node, llm=_llm))

    # --- Edges ------------------------------------------------------------
    builder.add_edge(START, "router_node")

    # Router conditional routing
    def _route_by_intent(state: HeatGuardState) -> str:
        intent = state.get("intent", "read")
        if intent == "chitchat":
            return "chitchat_worker"
        if intent == "parallel":
            return "orchestrator_node"
        return "read_worker"

    builder.add_conditional_edges(
        "router_node",
        _route_by_intent,
        {
            "chitchat_worker": "chitchat_worker",
            "read_worker": "read_worker",
            "orchestrator_node": "orchestrator_node",
        },
    )

    # Orchestrator emits Send objects via dispatch_workers — LangGraph handles fan-out
    builder.add_conditional_edges(
        "orchestrator_node",
        dispatch_workers,  # type: ignore[arg-type]
        ["parallel_read_worker"],
    )

    builder.add_edge("parallel_read_worker", "aggregator_node")
    builder.add_edge("chitchat_worker", END)
    builder.add_edge("read_worker", END)
    builder.add_edge("aggregator_node", END)

    # Pass the checkpointer if provided (needed for interrupt() / resume)
    graph = builder.compile(checkpointer=checkpointer)
    logger.info(
        "Agent graph compiled: provider=%s tools=%s checkpointer=%s",
        cfg.llm_provider,
        [t.name for t in tools],
        "postgres" if checkpointer is not None else "none",
    )
    return graph
