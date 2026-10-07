"""Tests for the policy engine (Phase 6.1).

Rules: pure unit tests — no LLM calls, no I/O, no real hardware (rules.md §4).
Mandatory safety property: policy must never return AUTO_EXECUTE for scope "all".
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.automation.policy import (
    PolicyConfig,
    PolicyDecision,
    evaluate_plan,
    load_policy_config,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DEFAULT_CFG = PolicyConfig()


def make_action(
    device_id: str = "dev-001",
    operation_id: str = "set_temperature",
    params: dict | None = None,
    before: dict | None = None,
    status: str = "PLANNED",
    is_critical: bool = False,
) -> dict:
    return {
        "device_id": device_id,
        "operation_id": operation_id,
        "params": params or {"temperature": 22.0},
        "before": before or {"temperature": 20.0},
        "target": {},
        "status": status,
        "_is_critical": is_critical,
    }


# ---------------------------------------------------------------------------
# Basic rule tests
# ---------------------------------------------------------------------------


def test_single_device_auto_execute() -> None:
    result = evaluate_plan([make_action()], DEFAULT_CFG, fleet_total=10)
    assert result.decision == "AUTO_EXECUTE"
    assert result.risk == "LOW"


def test_multi_device_requires_confirm() -> None:
    actions = [make_action(device_id=f"dev-{i:03d}") for i in range(3)]
    result = evaluate_plan(actions, DEFAULT_CFG, fleet_total=10)
    assert result.decision == "CONFIRM"


def test_temperature_out_of_range_blocks() -> None:
    action = make_action(params={"temperature": 35.0})  # > 30°C max
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision == "BLOCK"
    assert result.risk == "HIGH"
    assert any("safe range" in r for r in result.reasons)


def test_temperature_too_low_blocks() -> None:
    action = make_action(params={"temperature": 10.0})  # < 16°C min
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision == "BLOCK"


def test_large_delta_requires_confirm() -> None:
    action = make_action(
        params={"temperature": 26.0},
        before={"temperature": 18.0},  # delta = 8°C > 5°C threshold
    )
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision in ("CONFIRM", "BLOCK")
    assert any("delta" in r for r in result.reasons)


def test_fleet_share_high_risk() -> None:
    # 4 of 10 = 40% > 30% threshold → CONFIRM + HIGH
    actions = [make_action(device_id=f"dev-{i:03d}") for i in range(4)]
    result = evaluate_plan(actions, DEFAULT_CFG, fleet_total=10)
    assert result.risk == "HIGH"
    assert result.decision in ("CONFIRM", "BLOCK")


def test_power_off_requires_confirm() -> None:
    action = make_action(operation_id="power_off", params={})
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision in ("CONFIRM", "BLOCK")
    assert result.risk == "HIGH"


def test_critical_device_requires_confirm() -> None:
    action = make_action(is_critical=True)
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision in ("CONFIRM", "BLOCK")
    assert result.risk == "HIGH"
    assert any("critical" in w.lower() for w in result.warnings)


def test_active_alarm_requires_confirm() -> None:
    action = make_action()
    result = evaluate_plan(
        [action],
        DEFAULT_CFG,
        fleet_total=10,
        alarms_by_device={"dev-001": ["HIGH_TEMP_ALARM"]},
    )
    assert result.decision in ("CONFIRM", "BLOCK")
    assert any("alarm" in w.lower() for w in result.warnings)


def test_no_planned_actions_clarify() -> None:
    action = make_action(status="SKIPPED")
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision == "CLARIFY"


def test_strictest_wins() -> None:
    """BLOCK must win over CONFIRM."""
    actions = [
        make_action(params={"temperature": 35.0}),  # → BLOCK
        make_action(device_id="dev-002"),  # → CONFIRM (multi-device)
    ]
    result = evaluate_plan(actions, DEFAULT_CFG, fleet_total=10)
    assert result.decision == "BLOCK"


def test_load_policy_config() -> None:
    data = {
        "version": "1.0",
        "auto_max_devices": 3,
        "temp_min_c": 15.0,
        "temp_max_c": 28.0,
        "fleet_share_high_risk": 0.50,
        "temp_delta_confirm_c": 8.0,
    }
    cfg = load_policy_config(data)
    assert cfg.version == "1.0"
    assert cfg.auto_max_devices == 3
    assert cfg.temp_max_c == 28.0


def test_load_policy_config_bad_data() -> None:
    """Corrupt config must fail closed (use safe defaults, not crash)."""
    cfg = load_policy_config({"temp_max_c": "not-a-number"})
    # Should return safe defaults without raising
    assert isinstance(cfg, PolicyConfig)


def test_policy_error_fails_closed() -> None:
    """If evaluate_plan raises internally, it must return CONFIRM not raise."""
    # Pass a non-dict action to trigger an error inside
    result = evaluate_plan([None], DEFAULT_CFG, fleet_total=10)  # type: ignore[list-item]
    assert result.decision in ("CONFIRM", "BLOCK", "CLARIFY")


# ---------------------------------------------------------------------------
# Property tests — mandatory safety invariants (rules.md §4)
# ---------------------------------------------------------------------------


@given(
    n_devices=st.integers(min_value=2, max_value=50),
    fleet_total=st.integers(min_value=1, max_value=100),
)
@settings(max_examples=100)
def test_property_never_auto_execute_multi_device(n_devices: int, fleet_total: int) -> None:
    """Policy must NEVER return AUTO_EXECUTE when more than auto_max_devices=1 are PLANNED."""
    actions = [make_action(device_id=f"dev-{i:03d}") for i in range(n_devices)]
    cfg = PolicyConfig(auto_max_devices=1)
    result = evaluate_plan(actions, cfg, fleet_total=max(fleet_total, n_devices))
    assert result.decision != "AUTO_EXECUTE", (
        f"Safety violation: AUTO_EXECUTE returned for {n_devices} devices "
        f"(auto_max_devices=1)"
    )


@given(temperature=st.floats(min_value=31.0, max_value=100.0))
@settings(max_examples=50)
def test_property_always_block_out_of_range_high(temperature: float) -> None:
    """Setpoints above max_c must always be BLOCKED."""
    action = make_action(params={"temperature": temperature})
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision == "BLOCK", (
        f"Safety violation: setpoint {temperature}°C not blocked"
    )


@given(temperature=st.floats(min_value=-50.0, max_value=15.9))
@settings(max_examples=50)
def test_property_always_block_out_of_range_low(temperature: float) -> None:
    """Setpoints below min_c must always be BLOCKED."""
    action = make_action(params={"temperature": temperature})
    result = evaluate_plan([action], DEFAULT_CFG, fleet_total=10)
    assert result.decision == "BLOCK", (
        f"Safety violation: setpoint {temperature}°C not blocked"
    )
