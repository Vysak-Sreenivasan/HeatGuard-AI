"""Policy Engine — Phase 6.1 (deterministic, config-driven).

Evaluates an ExecutionPlan against policy rules loaded from policy.yaml
and returns a PolicyDecision with the strictest verdict and reasons.

Rules (rules.md §2.2):
- Policy errors → CONFIRM at minimum, or BLOCK. Never AUTO_EXECUTE on error.
- Strictest rule wins.
- Pure and testable: no LLM calls, no I/O.

References: architecture.md §6.3, rules.md §2.2
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger("heatguard.automation.policy")

Decision = Literal["AUTO_EXECUTE", "CONFIRM", "CLARIFY", "BLOCK"]
Risk = Literal["LOW", "MEDIUM", "HIGH"]

# Strictness ordering (higher = stricter)
_STRICTNESS: dict[Decision, int] = {
    "AUTO_EXECUTE": 0,
    "CLARIFY": 1,
    "CONFIRM": 2,
    "BLOCK": 3,
}


@dataclass
class PolicyDecision:
    decision: Decision
    risk: Risk
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class PolicyConfig:
    """Typed view of policy.yaml thresholds."""

    version: str = "0.1"
    # Max devices the system will execute without human approval
    auto_max_devices: int = 1
    # Fleet share (0-1) above which HIGH risk kicks in
    fleet_share_high_risk: float = 0.30
    # Temperature safe range (Celsius)
    temp_min_c: float = 16.0
    temp_max_c: float = 30.0
    # Setpoint delta (Celsius) above which CONFIRM is required
    temp_delta_confirm_c: float = 5.0
    # Allowed hours window (24h, local time). None = no restriction.
    allowed_hours_start: int | None = None
    allowed_hours_end: int | None = None


def load_policy_config(policy_data: dict[str, Any]) -> PolicyConfig:
    """Load policy thresholds from the parsed policy.yaml dict."""
    try:
        return PolicyConfig(
            version=str(policy_data.get("version", "0.1")),
            auto_max_devices=int(policy_data.get("auto_max_devices", 1)),
            fleet_share_high_risk=float(policy_data.get("fleet_share_high_risk", 0.30)),
            temp_min_c=float(policy_data.get("temp_min_c", 16.0)),
            temp_max_c=float(policy_data.get("temp_max_c", 30.0)),
            temp_delta_confirm_c=float(policy_data.get("temp_delta_confirm_c", 5.0)),
            allowed_hours_start=policy_data.get("allowed_hours_start"),
            allowed_hours_end=policy_data.get("allowed_hours_end"),
        )
    except (TypeError, ValueError) as exc:
        # Fail closed: if config is corrupt, require CONFIRM on everything
        logger.error("PolicyConfig: failed to parse policy data: %s", exc)
        return PolicyConfig()


def _stricter(a: Decision, b: Decision) -> Decision:
    return a if _STRICTNESS[a] >= _STRICTNESS[b] else b


def _higher_risk(a: Risk, b: Risk) -> Risk:
    order: dict[Risk, int] = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    return a if order[a] >= order[b] else b


# ---------------------------------------------------------------------------
# Main policy gate
# ---------------------------------------------------------------------------


def evaluate_plan(
    actions: list[dict[str, Any]],
    config: PolicyConfig,
    fleet_total: int,
    alarms_by_device: dict[str, list[str]] | None = None,
) -> PolicyDecision:
    """Evaluate a list of planned actions against the policy config.

    Args:
        actions:          Serialized PlannedAction dicts (status PLANNED only).
        config:           Loaded PolicyConfig from policy.yaml.
        fleet_total:      Total number of known devices (for fleet share check).
        alarms_by_device: Map of device_id → list of active alarm strings.

    Returns:
        PolicyDecision with the strictest verdict and all fired reasons.

    This function is pure: it raises no exceptions and never calls I/O.
    On any error → fail closed (CONFIRM).
    """
    if alarms_by_device is None:
        alarms_by_device = {}

    try:
        return _evaluate_inner(actions, config, fleet_total, alarms_by_device)
    except Exception as exc:  # noqa: BLE001
        logger.error("PolicyEngine: unexpected error in evaluate_plan: %s", exc)
        return PolicyDecision(
            decision="CONFIRM",
            risk="HIGH",
            reasons=["Policy evaluation error — requiring manual approval (fail-closed)."],
        )


def _evaluate_inner(
    actions: list[dict[str, Any]],
    config: PolicyConfig,
    fleet_total: int,
    alarms_by_device: dict[str, list[str]],
) -> PolicyDecision:
    planned = [a for a in actions if a.get("status") == "PLANNED"]
    decision: Decision = "AUTO_EXECUTE"
    risk: Risk = "LOW"
    reasons: list[str] = []
    warnings: list[str] = []

    n = len(planned)

    # Rule: too many devices → CONFIRM
    if n > config.auto_max_devices:
        decision = _stricter(decision, "CONFIRM")
        reasons.append(
            f"{n} devices in plan exceeds auto_max_devices={config.auto_max_devices}."
        )

    # Rule: fleet share
    if fleet_total > 0 and (n / fleet_total) > config.fleet_share_high_risk:
        decision = _stricter(decision, "CONFIRM")
        risk = _higher_risk(risk, "HIGH")
        reasons.append(
            f"{n}/{fleet_total} devices ({100 * n // fleet_total}%) exceeds "
            f"fleet_share_high_risk={config.fleet_share_high_risk:.0%}."
        )

    for action in planned:
        op = str(action.get("operation_id", ""))
        params = dict(action.get("params") or {})
        before = dict(action.get("before") or {})
        target_state = dict(action.get("target") or {})
        device_id = str(action.get("device_id", ""))
        is_critical = bool(action.get("_is_critical", False))

        # Rule: temperature range
        setpoint = params.get("temperature")
        if setpoint is None:
            setpoint = target_state.get("temperature")
        if setpoint is not None:
            try:
                sp_float = float(setpoint)
                if sp_float < config.temp_min_c or sp_float > config.temp_max_c:
                    decision = _stricter(decision, "BLOCK")
                    risk = _higher_risk(risk, "HIGH")
                    reasons.append(
                        f"Device {device_id}: setpoint {sp_float}°C is outside safe range "
                        f"[{config.temp_min_c}°C, {config.temp_max_c}°C]."
                    )

                # Rule: large delta
                before_temp = before.get("temperature")
                if before_temp is not None:
                    delta = abs(sp_float - float(before_temp))
                    if delta > config.temp_delta_confirm_c:
                        decision = _stricter(decision, "CONFIRM")
                        reasons.append(
                            f"Device {device_id}: setpoint change of {delta:.1f}°C exceeds "
                            f"delta threshold ({config.temp_delta_confirm_c}°C)."
                        )
            except (TypeError, ValueError):
                # Missing or invalid temperature → CLARIFY
                decision = _stricter(decision, "CLARIFY")
                reasons.append(
                    f"Device {device_id}: temperature value is missing or invalid."
                )

        # Rule: power-off with scope "all" / "all except"
        if op == "power_off":
            decision = _stricter(decision, "CONFIRM")
            risk = _higher_risk(risk, "HIGH")
            reasons.append(f"Device {device_id}: power-off requires approval.")

        # Rule: critical device
        if is_critical:
            decision = _stricter(decision, "CONFIRM")
            risk = _higher_risk(risk, "HIGH")
            warnings.append(
                f"Device {device_id} is marked critical (freeze protection / circulation pump). "
                "It is excluded from bulk actions by default — confirm explicitly."
            )

        # Rule: active alarms
        active_alarms = alarms_by_device.get(device_id, [])
        if active_alarms:
            decision = _stricter(decision, "CONFIRM")
            warnings.append(
                f"Device {device_id} has active alarm(s): {', '.join(active_alarms[:3])}."
            )

    # Rule: no planned actions at all
    if not planned:
        decision = "CLARIFY"
        reasons.append("No actionable devices in plan.")

    return PolicyDecision(decision=decision, risk=risk, reasons=reasons, warnings=warnings)
