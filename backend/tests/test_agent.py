"""Tests for v1 LangGraph Agent (Phase 3A).

Tests:
1. Router classifying intent (chitchat, read, parallel)
2. State reducer operator.add accumulating results
3. Aggregator combining sub-results into final response
"""

import pytest
from langchain_core.messages import AIMessage

from app.agent.nodes import aggregator_node, router_node
from app.agent.state import HeatGuardState


class FakeRouterLLM:
    def __init__(self, output: str):
        self.output = output

    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content=self.output)


@pytest.mark.asyncio
async def test_router_node_chitchat():
    state: HeatGuardState = {
        "user_message": "Hello, who are you?",
        "request_id": "req-1",
        "thread_id": "thread-1",
    }
    fake_llm = FakeRouterLLM("chitchat")
    result = await router_node(state, llm=fake_llm)  # type: ignore
    assert result["intent"] == "chitchat"


@pytest.mark.asyncio
async def test_router_node_read():
    state: HeatGuardState = {
        "user_message": "What is the temperature in the living room?",
        "request_id": "req-2",
        "thread_id": "thread-1",
    }
    fake_llm = FakeRouterLLM("read")
    result = await router_node(state, llm=fake_llm)  # type: ignore
    assert result["intent"] == "read"


@pytest.mark.asyncio
async def test_router_node_parallel():
    state: HeatGuardState = {
        "user_message": "Check telemetry for unit 1 and unit 2",
        "request_id": "req-3",
        "thread_id": "thread-1",
    }
    fake_llm = FakeRouterLLM("parallel")
    result = await router_node(state, llm=fake_llm)  # type: ignore
    assert result["intent"] == "parallel"


class FakeAggregatorLLM:
    async def ainvoke(self, messages, **kwargs):
        return AIMessage(content="Combined summary of both units.")


@pytest.mark.asyncio
async def test_aggregator_node_combines_results():
    state: HeatGuardState = {
        "user_message": "Check unit 1 and unit 2",
        "tool_results": ["Unit 1 at 21C", "Unit 2 at 22C"],
        "request_id": "req-4",
        "thread_id": "thread-1",
    }
    fake_llm = FakeAggregatorLLM()
    result = await aggregator_node(state, llm=fake_llm)  # type: ignore
    assert result["final_response"] == "Combined summary of both units."


@pytest.mark.asyncio
async def test_aggregator_node_single_result():
    state: HeatGuardState = {
        "user_message": "Check unit 1",
        "tool_results": ["Unit 1 at 21C"],
        "request_id": "req-5",
        "thread_id": "thread-1",
    }
    fake_llm = FakeAggregatorLLM()
    result = await aggregator_node(state, llm=fake_llm)  # type: ignore
    assert result["final_response"] == "Unit 1 at 21C"
