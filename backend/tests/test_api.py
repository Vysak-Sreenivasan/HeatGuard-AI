"""API integration tests for /health and basic endpoints (Phase 3A)."""

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
def mock_app_state():
    # Setup dummy state so lifespan is not required for unit testing health
    class DummyClient:
        is_connected = True

    app.state.mcp_client = DummyClient()
    app.state.graph = object()
    yield app
    app.state.mcp_client = None
    app.state.graph = None


@pytest.mark.asyncio
async def test_health_endpoint(mock_app_state):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["mcp"] == "connected"
        assert "llm_provider" in data
        assert data["agent_ready"] is True


@pytest.mark.asyncio
async def test_health_endpoint_disconnected():
    app.state.mcp_client = None
    app.state.graph = None

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["mcp"] == "disconnected"
        assert data["agent_ready"] is False
