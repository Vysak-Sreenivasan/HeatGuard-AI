"""Agent state types — TypedDict used by LangGraph nodes.

All I/O goes through this shared state object.
No global mutable state (rules.md §1.2 rule 4).
"""

from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict


class HeatGuardState(TypedDict, total=False):
    """Shared state for the HeatGuard LangGraph agent.

    Fields:
        messages:        LangChain message list (human + AI + tool messages).
        intent:          Router output: "chitchat" | "read" | "parallel".
        sub_questions:   Orchestrator output — list of independent sub-questions for parallel fan-out.
        tool_results:    Results accumulated from parallel read workers (uses operator.add reducer).
        final_response:  The assembled markdown response string.
        request_id:      Unique ID for this request (for tracing and logs).
        thread_id:       LangGraph thread ID (for checkpointing and resume).
        user_message:    Raw user message text.
        error:           Error message if the graph fails (never exposed raw to client).
    """

    messages: list[Any]
    intent: str
    sub_questions: list[str]
    # Reducer: parallel workers append results; aggregator reads the full list
    tool_results: Annotated[list[str], operator.add]
    final_response: str
    request_id: str
    thread_id: str
    user_message: str
    error: str
