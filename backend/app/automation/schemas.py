"""Pydantic models for Safe Automation (Phase 4+).

This file contains the data schemas only. No LLM calls, no I/O.
The automation pipeline itself is NOT built in Phase 3A — only these
models are stubbed so that mcp_client and security helpers can import them.

References: architecture.md §7, design.md §9
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# ActionRequest — structured LLM output (never contains device IDs)
# ---------------------------------------------------------------------------


class TargetSelector(BaseModel):
    """Target descriptor as parsed by the LLM. Never contains device IDs."""

    include: list[dict[str, str]] = Field(
        default_factory=list,
        description='[{"type": "room|zone|tag|device", "text": "living room"}]',
    )
    exclude: list[dict[str, str]] = Field(default_factory=list)
    scope: Literal["all", "named"] = "named"


class ActionRequest(BaseModel):
    """Structured intent parsed from the operator's natural-language request."""

    action: Literal["set_temperature", "power_on", "power_off", "set_mode"]
    value: float | None = None
    unit: Literal["C", "F"] | None = None
    target: TargetSelector = Field(default_factory=TargetSelector)
    ambiguities: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0, default=1.0)


# ---------------------------------------------------------------------------
# PlannedAction — one resolved command ready for execution
# ---------------------------------------------------------------------------


class PlannedAction(BaseModel):
    """One device-level planned command (device_id resolved by code, not LLM)."""

    device_id: str
    operation_id: str
    instruction_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    before: dict[str, Any] = Field(default_factory=dict)  # snapshot of current state
    target: dict[str, Any] = Field(default_factory=dict)  # intended after state
    status: Literal["PLANNED", "SKIPPED"] = "PLANNED"
    skip_reason: str | None = None


# ---------------------------------------------------------------------------
# ExecutionPlan — the full plan ready for policy + approval
# ---------------------------------------------------------------------------


class ExecutionPlan(BaseModel):
    """The complete execution plan for a set of device operations."""

    plan_id: str
    plan_hash: str  # sha256 over sorted canonical JSON of actions
    created_at: datetime = Field(default_factory=datetime.utcnow)
    expires_at: datetime
    actions: list[PlannedAction] = Field(default_factory=list)
    risk: Literal["LOW", "MEDIUM", "HIGH"] = "LOW"
    decision: Literal["AUTO_EXECUTE", "CONFIRM", "CLARIFY", "BLOCK"] = "CONFIRM"
    reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    policy_version: str = "unset"
    prompt_version: str = "unset"
    model: str = "unset"

    @classmethod
    def compute_hash(cls, actions: list[PlannedAction]) -> str:
        """Stable SHA-256 over sorted canonical JSON of planned actions.

        Sorted by device_id + operation_id so field ordering doesn't matter.
        """
        canonical = sorted(
            [a.model_dump(exclude={"status", "skip_reason"}) for a in actions],
            key=lambda x: f"{x['device_id']}:{x['operation_id']}",
        )
        blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# DeviceResult — per-device execution ledger entry
# ---------------------------------------------------------------------------


class DeviceResult(BaseModel):
    """Result for one device after execution and verification."""

    device_id: str
    status: Literal["VERIFIED", "ACCEPTED_UNVERIFIED", "FAILED", "SKIPPED", "UNKNOWN"]
    before: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] | None = None  # read-back state; None if unreadable
    attempts: int = 0
    error: str | None = None
    error_code: str | None = None
    latency_ms: int = 0


# ---------------------------------------------------------------------------
# Overall plan statuses
# ---------------------------------------------------------------------------

PlanStatus = Literal[
    "DRAFT",
    "NEEDS_CLARIFICATION",
    "AWAITING_APPROVAL",
    "APPROVED",
    "BLOCKED",
    "REJECTED",
    "EXPIRED",
    "EXECUTING",
    "COMPLETED",
    "PARTIAL",
    "FAILED",
    "CANCELLED",
]
