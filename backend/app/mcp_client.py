"""MCP client — the ONLY place in this repo that talks to the Device Gateway.

Rules enforced here (rules.md §2.9):
1. Bearer auth on every request.
2. Only allow-listed READ tools are converted to LangChain tools.
3. execute_operations is NEVER in the LLM tool list (double-checked at startup).
4. Tool schemas are pinned (hash of name+description+input_schema).
   On mismatch, startup is aborted unless strict mode is disabled.
5. Per-call timeouts: connect 3 s, total 10 s.
6. Reconnect with exponential backoff (0.5 s → 30 s cap, max 10 retries).
7. Circuit breaker: opens after 5 consecutive failures, resets on success.
8. MCP errors and isError results are counted as failures.
9. Every call is traced (plan_id, tool, latency, result).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.tools import load_mcp_tools
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http
from mcp.types import Tool

from app.config import Settings, get_settings

logger = logging.getLogger("heatguard.mcp_client")

# ---------------------------------------------------------------------------
# Safety constant — this tool is NEVER given to the LLM.
# ---------------------------------------------------------------------------
_WRITE_TOOL_NAME = "execute_operations"

# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class MCPConnectionError(RuntimeError):
    """Gateway is unreachable or the connection was refused."""


class MCPTimeoutError(RuntimeError):
    """A per-call or total timeout was exceeded."""


class MCPToolError(RuntimeError):
    """The gateway returned isError=True or an MCP-level error."""


class SchemaChangedError(RuntimeError):
    """A tool schema hash differs from the pinned value."""


# ---------------------------------------------------------------------------
# Schema pinning helpers
# ---------------------------------------------------------------------------


def _tool_schema_hash(tool: Tool) -> str:
    """Deterministic SHA-256 of (name, description, input_schema)."""
    payload = {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.inputSchema,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _load_pins(path: str) -> dict[str, str]:
    p = Path(path)
    if not p.is_file():
        return {}
    with open(p) as f:
        return json.load(f)


def _save_pins(path: str, pins: dict[str, str]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(pins, f, indent=2, sort_keys=True)
    logger.info("Schema pins written to %s (%d tools)", path, len(pins))


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

_CB_THRESHOLD = 5  # consecutive failures → open
_CB_RESET_S = 30.0  # time before moving to half-open


class _CircuitBreaker:
    def __init__(self) -> None:
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.monotonic() - self._opened_at > _CB_RESET_S:
            # half-open: allow one probe
            return False
        return True

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _CB_THRESHOLD and self._opened_at is None:
            self._opened_at = time.monotonic()
            logger.error(
                "MCP circuit breaker OPENED after %d consecutive failures.",
                self._failures,
            )


# ---------------------------------------------------------------------------
# MCPClient
# ---------------------------------------------------------------------------


class MCPClient:
    """Long-lived MCP client with reconnect, schema pinning, and circuit breaker.

    Usage::

        client = MCPClient(settings)
        await client.connect()          # called once at startup
        tools = client.get_llm_tools() # safe read-only tools for the LLM
        result = await client.call_tool("get_available_devices", {})
        await client.close()
    """

    CONNECT_TIMEOUT_S: float = 3.0
    CALL_TIMEOUT_S: float = 10.0
    BACKOFF_BASE_S: float = 0.5
    BACKOFF_MAX_S: float = 30.0
    MAX_RETRIES: int = 10

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._session: ClientSession | None = None
        self._llm_tools: list[BaseTool] = []
        self._raw_tools: list[Tool] = []
        self._cb = _CircuitBreaker()
        self._connected = False

    # ------------------------------------------------------------------
    # Public: lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to the gateway, pin schemas, build LLM tool list."""
        await self._connect_with_retry()

    async def close(self) -> None:
        """Gracefully close the MCP session."""
        if self._session is not None:
            try:
                await self._session.__aexit__(None, None, None)
            except Exception as exc:  # noqa: BLE001
                logger.debug("MCP session close error (ignored): %s", exc)
            self._session = None
        self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Public: tool access
    # ------------------------------------------------------------------

    def get_llm_tools(self) -> list[BaseTool]:
        """Return ONLY allow-listed read tools for the LLM.

        Safety guarantee: execute_operations is never in this list.
        """
        return list(self._llm_tools)

    def get_raw_tools(self) -> list[Tool]:
        """Return all raw MCP Tool objects (for schema inspection / tests)."""
        return list(self._raw_tools)

    # ------------------------------------------------------------------
    # Public: direct tool call (for executor.py ONLY — not for the LLM)
    # ------------------------------------------------------------------

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        plan_id: str | None = None,
    ) -> Any:
        """Call a tool on the gateway directly (bypasses LLM).

        Used by executor.py to call execute_operations.
        Never wrap this in a LangChain tool.

        Raises: MCPConnectionError, MCPTimeoutError, MCPToolError
        """
        if self._session is None:
            raise MCPConnectionError("MCP session is not connected")

        if self._cb.is_open:
            raise MCPConnectionError("MCP circuit breaker is open — gateway unreachable")

        t0 = time.monotonic()
        logger.debug(
            "MCP call: tool=%s plan_id=%s args_keys=%s",
            name,
            plan_id,
            list(arguments.keys()),
        )

        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, arguments=arguments),
                timeout=self.CALL_TIMEOUT_S,
            )
        except asyncio.TimeoutError as exc:
            self._cb.record_failure()
            latency = int((time.monotonic() - t0) * 1000)
            logger.error(
                "MCP timeout: tool=%s plan_id=%s latency_ms=%d",
                name,
                plan_id,
                latency,
            )
            raise MCPTimeoutError(f"Tool {name!r} timed out after {self.CALL_TIMEOUT_S}s") from exc
        except Exception as exc:
            self._cb.record_failure()
            logger.error("MCP error: tool=%s error=%s", name, exc)
            raise MCPConnectionError(f"MCP transport error on tool {name!r}: {exc}") from exc

        latency = int((time.monotonic() - t0) * 1000)

        if result.isError:
            self._cb.record_failure()
            content_text = str(result.content) if result.content else "(no content)"
            logger.warning(
                "MCP tool error: tool=%s plan_id=%s latency_ms=%d content=%s",
                name,
                plan_id,
                latency,
                content_text,
            )
            raise MCPToolError(f"Tool {name!r} returned isError=True: {content_text}")

        self._cb.record_success()
        logger.debug("MCP ok: tool=%s plan_id=%s latency_ms=%d", name, plan_id, latency)

        # Unwrap content: typically a list with one TextContent item
        if result.content and len(result.content) == 1:
            item = result.content[0]
            if hasattr(item, "text"):
                try:
                    return json.loads(item.text)
                except json.JSONDecodeError:
                    return item.text
        return result.content

    # ------------------------------------------------------------------
    # Internal: connection and schema pinning
    # ------------------------------------------------------------------

    async def _connect_with_retry(self) -> None:
        backoff = self.BACKOFF_BASE_S
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                await asyncio.wait_for(self._do_connect(), timeout=self.CONNECT_TIMEOUT_S * 3)
                return
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning(
                    "MCP connect attempt %d/%d failed: %s (backoff %.1fs)",
                    attempt,
                    self.MAX_RETRIES,
                    exc,
                    backoff,
                )
                if attempt == self.MAX_RETRIES:
                    raise MCPConnectionError(
                        f"Cannot connect to MCP gateway after {self.MAX_RETRIES} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.BACKOFF_MAX_S)

    async def _do_connect(self) -> None:
        """Open one Streamable-HTTP session, fetch tools, pin schemas."""
        token = self._settings.mcp_auth_token.get_secret_value()
        headers = {"Authorization": f"Bearer {token}"}

        transport = streamable_http(
            url=self._settings.mcp_server_url,
            headers=headers,
            httpx_client_factory=lambda **kwargs: httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=self.CONNECT_TIMEOUT_S,
                    read=self.CALL_TIMEOUT_S,
                    write=self.CALL_TIMEOUT_S,
                    pool=self.CALL_TIMEOUT_S,
                ),
                **kwargs,
            ),
        )

        session = ClientSession(*await transport.__aenter__())
        await session.initialize()

        # Fetch all tools from the server
        tools_response = await session.list_tools()
        raw_tools: list[Tool] = tools_response.tools

        # Pin / verify schemas
        self._verify_and_update_pins(raw_tools)

        # Build LLM tool list (read tools only, via allow-list)
        allowlist = set(self._settings.llm_tool_allowlist)
        llm_tools = await load_mcp_tools(session)
        safe_llm_tools = [t for t in llm_tools if t.name in allowlist]

        # Safety double-check: write tool must never appear
        for tool in safe_llm_tools:
            if tool.name == _WRITE_TOOL_NAME:
                raise RuntimeError(
                    f"SAFETY VIOLATION: '{_WRITE_TOOL_NAME}' was about to be added to the "
                    "LLM tool list. This is forbidden by the safety contract (rules.md §2.9)."
                )

        self._session = session
        self._raw_tools = raw_tools
        self._llm_tools = safe_llm_tools
        self._connected = True

        logger.info(
            "MCP connected: url=%s total_tools=%d llm_tools=%d",
            self._settings.mcp_server_url,
            len(raw_tools),
            len(safe_llm_tools),
        )
        logger.info(
            "LLM tool names: %s",
            [t.name for t in safe_llm_tools],
        )

    def _verify_and_update_pins(self, tools: list[Tool]) -> None:
        """Compare tool schema hashes against stored pins.

        On first run (no pin file): save hashes and continue.
        On subsequent runs: reject any change if strict mode is on.
        """
        current: dict[str, str] = {t.name: _tool_schema_hash(t) for t in tools}
        stored = _load_pins(self._settings.mcp_schema_pins_file)

        if not stored:
            _save_pins(self._settings.mcp_schema_pins_file, current)
            logger.info(
                "Schema pins initialised (%d tools) at %s",
                len(current),
                self._settings.mcp_schema_pins_file,
            )
            return

        changed: list[str] = []
        for name, new_hash in current.items():
            old_hash = stored.get(name)
            if old_hash is None:
                logger.warning("New tool not in pins: %s (hash=%s)", name, new_hash)
                changed.append(name)
            elif old_hash != new_hash:
                logger.error(
                    "Schema changed for tool '%s': stored=%s new=%s",
                    name,
                    old_hash,
                    new_hash,
                )
                changed.append(name)

        removed = [n for n in stored if n not in current]
        for name in removed:
            logger.warning("Tool removed from server (was pinned): %s", name)

        if (changed or removed) and self._settings.mcp_schema_pin_strict:
            raise SchemaChangedError(
                f"MCP tool schemas changed for: {changed + removed}. "
                "Review changes and delete schema_pins.json to accept the new schemas. "
                "This is a safety check (rules.md §2.9 rule 5)."
            )

        # Update pins to include any new tools (non-strict path)
        _save_pins(self._settings.mcp_schema_pins_file, {**stored, **current})


# ---------------------------------------------------------------------------
# Module-level singleton (initialised during FastAPI lifespan)
# ---------------------------------------------------------------------------

_client: MCPClient | None = None


def get_mcp_client() -> MCPClient:
    """FastAPI dependency — returns the singleton MCPClient."""
    if _client is None:
        raise RuntimeError("MCP client has not been initialised. Call init_mcp_client() first.")
    return _client


async def init_mcp_client(settings: Settings | None = None) -> MCPClient:
    """Create, connect, and register the global MCPClient. Call once at startup."""
    global _client  # noqa: PLW0603
    _client = MCPClient(settings)
    await _client.connect()
    return _client


async def close_mcp_client() -> None:
    """Gracefully close the global MCPClient. Call at shutdown."""
    global _client  # noqa: PLW0603
    if _client is not None:
        await _client.close()
        _client = None
