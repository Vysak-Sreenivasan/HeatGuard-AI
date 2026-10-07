"""FastAPI application entry point for HeatGuard AI.

Lifespan:
1. Connect MCPClient → pin tool schemas → build LLM tool list
2. Build LangGraph agent graph
3. Register routes
4. Serve
5. On shutdown: close MCP session

Routes (v1 scope):
  GET  /health
  POST /api/chat
  GET  /api/controllers   (stub for frontend compatibility)
  GET  /api/telemetry/{device_id}  (stub for frontend compatibility)

Rules (rules.md §5):
- Validate inputs with Pydantic.
- Return {"status": ..., "response": ...} or {"error": {"code", "message"}}.
- No stack traces to clients.
- /health is public; all /api routes will require auth in Phase 10.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.agent.graph import build_graph, build_llm
from app.agent.state import HeatGuardState
from app.agent.tools import build_llm_tools
from app.automation.policy import PolicyConfig, load_policy_config
from app.automation.registry import DeviceRegistry
from app.config import get_settings
from app.db import create_app_tables, setup_checkpointer
from app.mcp_client import MCPConnectionError, close_mcp_client, init_mcp_client

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------

logging.basicConfig(level=logging.INFO)
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.add_log_level,
        structlog.processors.JSONRenderer(),
    ]
)
logger = logging.getLogger("heatguard.main")

# ---------------------------------------------------------------------------
# Application state (no global mutable state — held in app.state)
# ---------------------------------------------------------------------------

_GRAPH: Any = None  # compiled LangGraph


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Startup and shutdown lifecycle."""
    settings = get_settings()
    logger.info("HeatGuard AI starting up | provider=%s", settings.llm_provider)

    # 1. Set up database tables + LangGraph checkpointer (Phase 4.3)
    checkpointer = None
    try:
        await create_app_tables(settings.database_url)
        checkpointer = await setup_checkpointer(settings.database_url)
    except Exception as exc:  # noqa: BLE001
        logger.error("Database setup failed: %s", exc)
        logger.warning("Starting without Postgres checkpointer — interrupt/resume disabled")

    # 2. Load policy config from policy.yaml (Phase 6.1)
    policy_config: PolicyConfig = PolicyConfig()  # safe defaults
    try:
        import os

        policy_path = os.path.join(os.path.dirname(__file__), "policy.yaml")
        with open(policy_path) as f:
            policy_data = yaml.safe_load(f) or {}
        policy_config = load_policy_config(policy_data)
        logger.info("Policy loaded: version=%s", policy_config.version)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to load policy.yaml, using safe defaults: %s", exc)

    # 3. Connect MCP client
    try:
        mcp_client = await init_mcp_client(settings)
    except MCPConnectionError as exc:
        logger.error("Failed to connect to MCP gateway: %s", exc)
        logger.warning("Starting without MCP connection — /health will report disconnected")
        mcp_client = None

    # 4. Build device registry (Phase 4.1)
    registry: DeviceRegistry | None = None
    if mcp_client is not None:
        registry = DeviceRegistry(mcp_client=mcp_client, ttl_seconds=60.0)
        try:
            await registry.refresh()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Initial registry load failed (will retry on next request): %s", exc)

    # 5. Build LLM and agent graph (only if MCP connected)
    global _GRAPH  # noqa: PLW0603
    if mcp_client is not None:
        try:
            tools = build_llm_tools(mcp_client)
            llm = build_llm(settings)
            _GRAPH = build_graph(
                tools=tools,
                llm=llm,
                settings=settings,
                checkpointer=checkpointer,
            )
            logger.info("Agent graph ready")
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to build agent graph: %s", exc)
            _GRAPH = None
    else:
        _GRAPH = None

    # Store in app.state for access in routes
    app.state.mcp_client = mcp_client
    app.state.graph = _GRAPH
    app.state.registry = registry
    app.state.policy_config = policy_config
    app.state.checkpointer = checkpointer

    yield

    # Shutdown
    logger.info("HeatGuard AI shutting down…")
    await close_mcp_client()


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="HeatGuard AI",
    description="Safe Agentic Control for Heat Pump Fleets",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # tightened in Phase 10 with auth
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Error handler — never leak stack traces to clients
# ---------------------------------------------------------------------------


@app.exception_handler(Exception)
async def _generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": {"code": "INTERNAL_ERROR", "message": "An internal error occurred."}},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health", tags=["ops"])
async def health(request: Request) -> dict[str, Any]:
    """Public health endpoint (no auth required)."""
    settings = get_settings()
    mcp_client = getattr(request.app.state, "mcp_client", None)
    registry: DeviceRegistry | None = getattr(request.app.state, "registry", None)
    policy_config = getattr(request.app.state, "policy_config", None)
    mcp_status = "connected" if (mcp_client and mcp_client.is_connected) else "disconnected"

    return {
        "status": "ok",
        "mcp": mcp_status,
        "llm_provider": settings.llm_provider,
        "agent_ready": request.app.state.graph is not None,
        "checkpointer": "postgres" if getattr(request.app.state, "checkpointer", None) else "none",
        "registry_devices": registry.total() if registry else 0,
        "policy_version": policy_config.version if policy_config else "unset",
    }


# ---------------------------------------------------------------------------
# Device Registry admin endpoints (Phase 4.1)
# ---------------------------------------------------------------------------


@app.get("/api/registry/devices", tags=["registry"])
async def list_registry_devices(request: Request) -> dict[str, Any]:
    """Return the full device registry (room, zone, aliases, criticality).

    In Phase 10 this will require admin auth. For now it is open for dev.
    """
    registry: DeviceRegistry | None = getattr(request.app.state, "registry", None)
    if registry is None:
        return {"devices": [], "error": "Registry not available"}

    try:
        devices = registry.to_dict_list()
        return {"devices": devices, "total": len(devices)}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list registry devices: %s", exc)
        return {"devices": [], "error": "Failed to load registry"}


@app.put("/api/registry/devices/{device_id}", tags=["registry"])
async def update_registry_device(
    device_id: str,
    body: dict[str, Any],
    request: Request,
) -> dict[str, Any]:
    """Update overlay metadata for a single device (room, zone, aliases, criticality).

    The update is in-memory only; persist to Postgres in Phase 4.2 (not yet wired).
    Triggers a cache refresh so the new labels are used immediately.
    """
    registry: DeviceRegistry | None = getattr(request.app.state, "registry", None)
    if registry is None:
        raise HTTPException(status_code=503, detail="Registry not available")

    # Validate criticality value if provided
    criticality = body.get("criticality")
    if criticality is not None and criticality not in ("normal", "protected", "critical"):
        raise HTTPException(
            status_code=422,
            detail={"error": {"code": "INVALID_CRITICALITY", "message": "criticality must be normal | protected | critical"}},
        )

    # Merge into the registry overlay and force refresh
    registry._overlay[device_id] = {**registry._overlay.get(device_id, {}), **body}
    try:
        await registry.refresh()
    except Exception as exc:  # noqa: BLE001
        logger.error("Registry refresh failed after update: %s", exc)

    device = await registry.get_by_id(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found after refresh")

    return {
        "device_id": device.device_id,
        "name": device.name,
        "room": device.room,
        "zone": device.zone,
        "aliases": device.aliases,
        "criticality": device.criticality,
    }


# ---------------------------------------------------------------------------
# Chat endpoint
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=2000, description="Operator's message")
    thread_id: str | None = Field(
        default=None,
        description="LangGraph thread ID for conversation continuity",
    )


class ChatResponse(BaseModel):
    status: str
    intent: str | None = None
    thread_id: str
    response: str | None = None
    error: dict[str, str] | None = None


@app.post("/api/chat", response_model=ChatResponse, tags=["chat"])
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Process an operator's natural-language message through the agent graph."""
    graph = request.app.state.graph
    if graph is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": {
                    "code": "AGENT_NOT_READY",
                    "message": (
                        "The agent is not ready. The MCP gateway may be unreachable. "
                        "Check /health for status."
                    ),
                }
            },
        )

    request_id = str(uuid.uuid4())[:8]
    thread_id = body.thread_id or str(uuid.uuid4())

    logger.info(
        "chat request_id=%s thread_id=%s message_len=%d",
        request_id,
        thread_id,
        len(body.message),
    )

    initial_state: HeatGuardState = {
        "messages": [],
        "intent": "",
        "sub_questions": [],
        "tool_results": [],
        "final_response": "",
        "request_id": request_id,
        "thread_id": thread_id,
        "user_message": body.message,
        "error": "",
    }

    config = {"configurable": {"thread_id": thread_id}}

    t0 = time.monotonic()
    try:
        result = await graph.ainvoke(initial_state, config=config)
    except Exception as exc:  # noqa: BLE001
        latency = int((time.monotonic() - t0) * 1000)
        logger.error(
            "Agent error request_id=%s latency_ms=%d error=%s",
            request_id,
            latency,
            exc,
        )
        return ChatResponse(
            status="error",
            thread_id=thread_id,
            error={
                "code": "AGENT_ERROR",
                "message": "An error occurred while processing your request.",
            },
        )

    latency = int((time.monotonic() - t0) * 1000)
    final_response = result.get("final_response", "")
    intent = result.get("intent", "read")

    logger.info(
        "chat done request_id=%s thread_id=%s intent=%s latency_ms=%d",
        request_id,
        thread_id,
        intent,
        latency,
    )

    return ChatResponse(
        status="completed",
        intent=intent,
        thread_id=thread_id,
        response=final_response,
    )


# ---------------------------------------------------------------------------
# Legacy / compatibility stubs (frontend uses these)
# ---------------------------------------------------------------------------


@app.get("/api/controllers", tags=["devices"])
async def list_controllers(request: Request) -> dict[str, Any]:
    """List available devices from the MCP gateway."""
    mcp_client = request.app.state.mcp_client
    if mcp_client is None or not mcp_client.is_connected:
        return {"controllers": [], "error": "Gateway not connected"}

    try:
        devices = await mcp_client.call_tool("get_available_devices", {})
        return {"controllers": devices if isinstance(devices, list) else []}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to list controllers: %s", exc)
        return {"controllers": [], "error": str(exc)}


@app.get("/api/telemetry/{device_id}", tags=["devices"])
async def get_telemetry(device_id: str, request: Request) -> dict[str, Any]:
    """Get current telemetry for a specific device."""
    mcp_client = request.app.state.mcp_client
    if mcp_client is None or not mcp_client.is_connected:
        raise HTTPException(status_code=503, detail="Gateway not connected")

    try:
        data = await mcp_client.call_tool("get_telemetry_data", {"device_id": device_id})
        return data if isinstance(data, dict) else {"data": data}
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to get telemetry device_id=%s: %s", device_id, exc)
        raise HTTPException(status_code=502, detail="Failed to fetch telemetry") from exc
