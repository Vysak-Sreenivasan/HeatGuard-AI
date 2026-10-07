"""Device Resolver — Phase 5.2 (stub for Phase 4 wiring).

Resolves a TargetSelector (from the LLM parser) to a concrete list of
Device objects using the DeviceRegistry.

Rules (rules.md §2.3):
- Only code resolves device IDs — never the LLM.
- Ambiguous or empty match → ask with explicit options.
- "All except X": if X cannot be resolved, fail closed → ask.
- Fuzzy match only above a high threshold, and preview shows the match.

References: architecture.md §6.2, rules.md §2.2–2.3
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Literal

from app.automation.registry import Device, DeviceRegistry
from app.automation.schemas import TargetSelector

logger = logging.getLogger("heatguard.automation.resolver")

# Fuzzy match threshold (0–1). Only matches above this score are accepted.
_FUZZY_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ResolveResult:
    status: Literal["resolved", "ambiguous", "none"]
    devices: list[Device]
    # Candidate strings to show in a clarification question
    candidates: list[str]
    reason: str = ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def resolve_targets(
    selector: TargetSelector,
    registry: DeviceRegistry,
) -> ResolveResult:
    """Resolve a TargetSelector to a concrete device list.

    Returns:
        ResolveResult with status = "resolved" | "ambiguous" | "none".
        "resolved" means devices is non-empty and ready for planning.
        "ambiguous" or "none" means a clarification question must be shown.

    Safety: never expands "all except X" when X is unresolved (rules.md §2.2).
    """
    all_devices = await registry.get_all()

    if selector.scope == "all":
        included = list(all_devices)
    else:
        included = await _resolve_included(selector, registry)
        if included is None:
            # One of the include terms failed — ambiguous or no match
            return ResolveResult(
                status="none",
                devices=[],
                candidates=_all_names(all_devices),
                reason="Could not find any devices matching the target.",
            )

    # Resolve excludes. If ANY exclude cannot be resolved, fail closed.
    for ex_item in selector.exclude:
        ex_result = await _resolve_single(ex_item["text"], ex_item.get("type", "any"), registry)
        if ex_result.status == "none":
            # "All except X" with unresolved X → must ask (rules.md §2.2 rule 3)
            logger.warning(
                "Resolver: exclude '%s' could not be resolved — refusing to expand all",
                ex_item["text"],
            )
            return ResolveResult(
                status="none",
                devices=[],
                candidates=_all_names(all_devices),
                reason=(
                    f"I could not find any device matching '{ex_item['text']}' to exclude. "
                    "Please clarify which device to keep on."
                ),
            )
        if ex_result.status == "ambiguous":
            return ResolveResult(
                status="ambiguous",
                devices=[],
                candidates=[d.name for d in ex_result.devices],
                reason=(
                    f"'{ex_item['text']}' matches multiple devices. "
                    "Which one should be excluded?"
                ),
            )
        # Remove resolved excludes
        ex_ids = {d.device_id for d in ex_result.devices}
        included = [d for d in included if d.device_id not in ex_ids]

    if not included:
        return ResolveResult(
            status="none",
            devices=[],
            candidates=_all_names(all_devices),
            reason="No devices remain after applying the exclusion filter.",
        )

    return ResolveResult(status="resolved", devices=included, candidates=[])


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _resolve_included(
    selector: TargetSelector,
    registry: DeviceRegistry,
) -> list[Device] | None:
    """Resolve include terms and union them.

    Returns None if any term could not be resolved.
    """
    if not selector.include:
        return []

    matched: list[Device] = []
    for item in selector.include:
        result = await _resolve_single(item["text"], item.get("type", "any"), registry)
        if result.status != "resolved":
            return None
        # Union
        existing_ids = {d.device_id for d in matched}
        matched.extend(d for d in result.devices if d.device_id not in existing_ids)
    return matched


async def _resolve_single(
    text: str,
    kind: str,  # "room" | "zone" | "tag" | "device" | "any"
    registry: DeviceRegistry,
) -> ResolveResult:
    """Resolve a single target term."""
    all_devices = await registry.get_all()

    # Exact matches
    if kind in ("room", "any"):
        devices = await registry.by_room(text)
        if devices:
            return ResolveResult(status="resolved", devices=devices, candidates=[])

    if kind in ("zone", "any"):
        devices = await registry.by_zone(text)
        if devices:
            return ResolveResult(status="resolved", devices=devices, candidates=[])

    if kind in ("tag", "any"):
        devices = await registry.by_tag(text)
        if devices:
            return ResolveResult(status="resolved", devices=devices, candidates=[])

    if kind in ("device", "any"):
        devices = await registry.by_name(text)
        if len(devices) == 1:
            return ResolveResult(status="resolved", devices=devices, candidates=[])
        if len(devices) > 1:
            return ResolveResult(
                status="ambiguous",
                devices=devices,
                candidates=[d.name for d in devices],
                reason=f"'{text}' matches multiple devices.",
            )

    # Fuzzy match on device names only (as a fallback)
    all_names = [d.name for d in all_devices]
    close = get_close_matches(text, all_names, n=3, cutoff=_FUZZY_THRESHOLD)
    if len(close) == 1:
        fuzzy_device = next((d for d in all_devices if d.name == close[0]), None)
        if fuzzy_device:
            logger.info("Resolver: fuzzy matched '%s' → '%s'", text, close[0])
            return ResolveResult(status="resolved", devices=[fuzzy_device], candidates=[close[0]])
    if close:
        return ResolveResult(
            status="ambiguous",
            devices=[d for d in all_devices if d.name in close],
            candidates=close,
            reason=f"'{text}' is ambiguous — did you mean one of: {', '.join(close)}?",
        )

    return ResolveResult(
        status="none",
        devices=[],
        candidates=_all_names(all_devices),
        reason=f"No device found matching '{text}'.",
    )


def _all_names(devices: list[Device]) -> list[str]:
    return [d.name for d in devices]
