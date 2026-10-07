"""Tests for the device registry (Phase 4.1).

Rules: no real hardware, no LLM calls (rules.md §4).
All tests use a fake MCP client that returns static device data.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.automation.registry import Device, DeviceRegistry, _normalize
from app.automation.schemas import TargetSelector
from app.automation.resolver import ResolveResult, resolve_targets


# ---------------------------------------------------------------------------
# Fake MCP client
# ---------------------------------------------------------------------------

FAKE_DEVICES = [
    {
        "device_id": "dev-001",
        "name": "Living Room HP",
        "room": "living room",
        "zone": "ground floor",
        "tags": ["heat-pump"],
        "capabilities": {"modes": ["heat", "cool"]},
    },
    {
        "device_id": "dev-002",
        "name": "Bedroom HP",
        "room": "bedroom",
        "zone": "first floor",
        "tags": ["heat-pump"],
        "capabilities": {"modes": ["heat"]},
    },
    {
        "device_id": "dev-003",
        "name": "Office HP",
        "room": "office",
        "zone": "ground floor",
        "tags": ["heat-pump", "priority"],
        "capabilities": {},
    },
    {
        "device_id": "dev-004",
        "name": "Circulation Pump",
        "room": "",
        "zone": "",
        "tags": ["pump", "critical"],
        "capabilities": {},
    },
]


def make_client(devices: list[dict[str, Any]] | None = None) -> AsyncMock:
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value=devices or FAKE_DEVICES)
    return client


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_loads_devices() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    devices = await reg.get_all()
    assert len(devices) == 4
    assert all(isinstance(d, Device) for d in devices)


@pytest.mark.asyncio
async def test_registry_by_room() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    devices = await reg.by_room("living room")
    assert len(devices) == 1
    assert devices[0].device_id == "dev-001"


@pytest.mark.asyncio
async def test_registry_by_zone() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    devices = await reg.by_zone("ground floor")
    assert len(devices) == 2
    ids = {d.device_id for d in devices}
    assert ids == {"dev-001", "dev-003"}


@pytest.mark.asyncio
async def test_registry_by_tag() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    devices = await reg.by_tag("pump")
    assert len(devices) == 1
    assert devices[0].device_id == "dev-004"


@pytest.mark.asyncio
async def test_registry_overlay_criticality() -> None:
    overlay = {"dev-004": {"criticality": "critical", "room": "basement"}}
    reg = DeviceRegistry(mcp_client=make_client(), overlay=overlay, ttl_seconds=60.0)
    await reg.refresh()
    device = await reg.get_by_id("dev-004")
    assert device is not None
    assert device.criticality == "critical"
    assert device.is_critical is True
    assert device.room == "basement"


@pytest.mark.asyncio
async def test_registry_overlay_alias() -> None:
    overlay = {"dev-001": {"aliases": ["lounge", "main room"]}}
    reg = DeviceRegistry(mcp_client=make_client(), overlay=overlay, ttl_seconds=60.0)
    await reg.refresh()
    by_alias = await reg.by_name("lounge")
    assert len(by_alias) == 1
    assert by_alias[0].device_id == "dev-001"


@pytest.mark.asyncio
async def test_registry_unlabeled() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    unlabeled = await reg.unlabeled()
    assert len(unlabeled) == 1
    assert unlabeled[0].device_id == "dev-004"


@pytest.mark.asyncio
async def test_registry_ttl_cache() -> None:
    """Verify that refresh is skipped within TTL."""
    call_count = [0]

    async def counting_call_tool(name: str, args: dict[str, Any]) -> Any:
        call_count[0] += 1
        return FAKE_DEVICES

    client = AsyncMock()
    client.call_tool = counting_call_tool

    reg = DeviceRegistry(mcp_client=client, ttl_seconds=10.0)
    await reg.get_all()  # triggers refresh
    await reg.get_all()  # should use cache
    assert call_count[0] == 1, "Expected only 1 MCP call within TTL"


@pytest.mark.asyncio
async def test_registry_handles_bad_payload() -> None:
    """Registry should not crash on unexpected payloads."""
    client = AsyncMock()
    client.call_tool = AsyncMock(return_value="not a list")
    reg = DeviceRegistry(mcp_client=client, ttl_seconds=60.0)
    # Should log a warning and not raise
    await reg.refresh()
    devices = await reg.get_all()
    assert devices == []


# ---------------------------------------------------------------------------
# Resolver tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_by_room() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    selector = TargetSelector(
        include=[{"type": "room", "text": "living room"}],
        scope="named",
    )
    result = await resolve_targets(selector, reg)
    assert result.status == "resolved"
    assert len(result.devices) == 1
    assert result.devices[0].device_id == "dev-001"


@pytest.mark.asyncio
async def test_resolver_scope_all() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    selector = TargetSelector(scope="all")
    result = await resolve_targets(selector, reg)
    assert result.status == "resolved"
    assert len(result.devices) == 4


@pytest.mark.asyncio
async def test_resolver_all_except_resolved() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    selector = TargetSelector(
        scope="all",
        exclude=[{"type": "room", "text": "bedroom"}],
    )
    result = await resolve_targets(selector, reg)
    assert result.status == "resolved"
    ids = {d.device_id for d in result.devices}
    assert "dev-002" not in ids


@pytest.mark.asyncio
async def test_resolver_all_except_unresolved_fails_closed() -> None:
    """Safety: 'all except X' with unresolved X must NOT expand to 'all'."""
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    selector = TargetSelector(
        scope="all",
        exclude=[{"type": "room", "text": "nonexistent room"}],
    )
    result = await resolve_targets(selector, reg)
    # Must NOT return resolved — must ask for clarification
    assert result.status == "none", (
        "Safety violation: resolver expanded 'all except <unresolved>' to all devices"
    )
    assert len(result.devices) == 0


@pytest.mark.asyncio
async def test_resolver_no_match_returns_none() -> None:
    reg = DeviceRegistry(mcp_client=make_client(), ttl_seconds=60.0)
    await reg.refresh()
    selector = TargetSelector(
        include=[{"type": "room", "text": "basement"}],
        scope="named",
    )
    result = await resolve_targets(selector, reg)
    assert result.status == "none"
    assert len(result.devices) == 0


def test_normalize() -> None:
    assert _normalize("Living Room") == "living room"
    assert _normalize("living-room") == "living room"
    assert _normalize("BEDROOM  ") == "bedroom"
