"""Device Registry — Phase 4.1

Loads device metadata from the MCP gateway (get_available_devices),
overlays local configuration (room, zone, alias, criticality overrides),
and caches the result with a TTL.

Rules (rules.md §1.2 rule 5):
- Pure and testable: no LLM calls.
- All I/O is async.
- Device access only through mcp_client.py.

References: architecture.md §4, design.md §9
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("heatguard.automation.registry")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Device:
    """Canonical device record as stored in the registry."""

    device_id: str
    name: str
    room: str = ""
    zone: str = ""
    tags: list[str] = field(default_factory=list)  # type: ignore[assignment]
    aliases: list[str] = field(default_factory=list)  # type: ignore[assignment]
    criticality: str = "normal"  # "normal" | "protected" | "critical"
    capabilities: dict[str, Any] = field(default_factory=dict)  # type: ignore[assignment]
    raw: dict[str, Any] = field(default_factory=dict)  # original gateway payload

    @property
    def is_critical(self) -> bool:
        return self.criticality == "critical"

    @property
    def is_protected(self) -> bool:
        return self.criticality in ("critical", "protected")

    def matches_name(self, text: str) -> bool:
        """Case-insensitive match on name and aliases."""
        norm = _normalize(text)
        return norm == _normalize(self.name) or any(
            norm == _normalize(a) for a in self.aliases
        )

    def matches_room(self, text: str) -> bool:
        return _normalize(text) == _normalize(self.room)

    def matches_zone(self, text: str) -> bool:
        return _normalize(text) == _normalize(self.zone)

    def matches_tag(self, text: str) -> bool:
        norm = _normalize(text)
        return any(norm == _normalize(t) for t in self.tags)


def _normalize(s: str) -> str:
    """Lower-case, strip punctuation for fuzzy-safe comparison."""
    import re

    return re.sub(r"[\s\-_]+", " ", s.lower()).strip()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# Type alias for the overlay config (loaded from env or a YAML file)
OverlayMap = dict[str, dict[str, Any]]


class DeviceRegistry:
    """Thread-safe async device registry with TTL cache.

    Usage:
        registry = DeviceRegistry(mcp_client, overlay=overlay_map)
        await registry.refresh()                 # or call_tool on demand
        devices = await registry.get_all()
        room_devs = await registry.by_room("living room")
    """

    def __init__(
        self,
        mcp_client: Any,  # MCPClient; avoid circular import
        overlay: OverlayMap | None = None,
        ttl_seconds: float = 60.0,
    ) -> None:
        self._client = mcp_client
        self._overlay: OverlayMap = overlay or {}
        self._ttl = ttl_seconds
        self._cache: list[Device] = []
        self._cache_ts: float = 0.0
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_all(self) -> list[Device]:
        """Return all devices, refreshing the cache if stale."""
        await self._ensure_fresh()
        return list(self._cache)

    async def get_by_id(self, device_id: str) -> Device | None:
        """Return a single device by ID, or None if not found."""
        await self._ensure_fresh()
        for d in self._cache:
            if d.device_id == device_id:
                return d
        return None

    async def by_room(self, room: str) -> list[Device]:
        """All devices in a room (case-insensitive)."""
        await self._ensure_fresh()
        return [d for d in self._cache if d.matches_room(room)]

    async def by_zone(self, zone: str) -> list[Device]:
        """All devices in a zone (case-insensitive)."""
        await self._ensure_fresh()
        return [d for d in self._cache if d.matches_zone(zone)]

    async def by_tag(self, tag: str) -> list[Device]:
        """All devices with a given tag (case-insensitive)."""
        await self._ensure_fresh()
        return [d for d in self._cache if d.matches_tag(tag)]

    async def by_name(self, name: str) -> list[Device]:
        """Devices whose name or alias exactly matches (case-insensitive)."""
        await self._ensure_fresh()
        return [d for d in self._cache if d.matches_name(name)]

    async def unlabeled(self) -> list[Device]:
        """Devices missing a room label — treated as protected in bulk-off."""
        await self._ensure_fresh()
        return [d for d in self._cache if not d.room]

    async def refresh(self) -> None:
        """Force a cache refresh from the gateway (bypasses TTL)."""
        async with self._lock:
            await self._load()

    def total(self) -> int:
        return len(self._cache)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _ensure_fresh(self) -> None:
        if time.monotonic() - self._cache_ts > self._ttl:
            async with self._lock:
                # Double-checked locking
                if time.monotonic() - self._cache_ts > self._ttl:
                    await self._load()

    async def _load(self) -> None:
        """Fetch devices from MCP gateway and apply overlay."""
        logger.info("DeviceRegistry: refreshing from gateway")
        try:
            raw_list = await self._client.call_tool("get_available_devices", {})
        except Exception as exc:
            logger.error("DeviceRegistry: failed to fetch devices: %s", exc)
            # Keep stale cache; don't blow up
            return

        if not isinstance(raw_list, list):
            logger.warning("DeviceRegistry: unexpected payload type %s", type(raw_list))
            return

        devices: list[Device] = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("device_id") or item.get("id") or "")
            if not device_id:
                continue

            # Base values from gateway
            name = str(item.get("name") or device_id)
            room = str(item.get("room") or "")
            zone = str(item.get("zone") or "")
            tags = list(item.get("tags") or [])
            capabilities = dict(item.get("capabilities") or {})

            # Overlay merge (local overrides gateway labels + criticality)
            ov = self._overlay.get(device_id, {})
            room = str(ov.get("room", room))
            zone = str(ov.get("zone", zone))
            tags = list(ov.get("tags", tags))
            aliases = list(ov.get("aliases") or [])
            criticality = str(ov.get("criticality", "normal"))
            if criticality not in ("normal", "protected", "critical"):
                logger.warning(
                    "DeviceRegistry: unknown criticality '%s' for %s, defaulting to normal",
                    criticality,
                    device_id,
                )
                criticality = "normal"

            devices.append(
                Device(
                    device_id=device_id,
                    name=name,
                    room=room,
                    zone=zone,
                    tags=tags,
                    aliases=aliases,
                    criticality=criticality,
                    capabilities=capabilities,
                    raw=item,
                )
            )

        self._cache = devices
        self._cache_ts = time.monotonic()
        logger.info(
            "DeviceRegistry: loaded %d devices (%d unlabeled)",
            len(devices),
            sum(1 for d in devices if not d.room),
        )

    def to_dict_list(self) -> list[dict[str, Any]]:
        """Serialize the current cache for the admin endpoint."""
        return [
            {
                "device_id": d.device_id,
                "name": d.name,
                "room": d.room,
                "zone": d.zone,
                "tags": d.tags,
                "aliases": d.aliases,
                "criticality": d.criticality,
                "capabilities": d.capabilities,
            }
            for d in self._cache
        ]
