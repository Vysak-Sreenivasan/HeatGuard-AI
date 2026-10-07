"""Unit tests for approval token signing and idempotency key helpers (Phase 3A)."""

import time

from app.security import (
    compute_idempotency_key,
    compute_params_hash,
    create_approval_token,
    verify_approval_token_local,
)

SECRET = "test-signing-secret-at-least-32-chars-long!!"


def test_compute_params_hash():
    params1 = {"temp": 22.0, "mode": "heat"}
    params2 = {"mode": "heat", "temp": 22.0}
    # Deterministic regardless of dict key order
    assert compute_params_hash(params1) == compute_params_hash(params2)
    assert compute_params_hash(params1).startswith("sha256:")


def test_approval_token_valid():
    params = {"target_temp": 21.5}
    token = create_approval_token(
        plan_id="plan-123",
        plan_hash="sha256:abc123def456",
        device_id="hp-001",
        operation_id="set_temperature",
        params=params,
        secret=SECRET,
    )

    assert isinstance(token, str)
    assert "." in token

    is_valid, err = verify_approval_token_local(
        token=token,
        expected_device_id="hp-001",
        expected_operation_id="set_temperature",
        expected_params=params,
        secret=SECRET,
    )
    assert is_valid is True
    assert err is None


def test_approval_token_expired():
    params = {"target_temp": 21.5}
    # Expired in past
    token = create_approval_token(
        plan_id="plan-123",
        plan_hash="sha256:abc123def456",
        device_id="hp-001",
        operation_id="set_temperature",
        params=params,
        exp=int(time.time()) - 10,
        secret=SECRET,
    )

    is_valid, err = verify_approval_token_local(
        token=token,
        expected_device_id="hp-001",
        expected_operation_id="set_temperature",
        expected_params=params,
        secret=SECRET,
    )
    assert is_valid is False
    assert err == "TOKEN_EXPIRED"


def test_approval_token_tampered_signature():
    params = {"target_temp": 21.5}
    token = create_approval_token(
        plan_id="plan-123",
        plan_hash="sha256:abc123def456",
        device_id="hp-001",
        operation_id="set_temperature",
        params=params,
        secret=SECRET,
    )

    raw_payload, sig = token.split(".", 1)
    tampered_sig = ("0" if sig[-1] != "0" else "1") + sig[1:]
    tampered_token = f"{raw_payload}.{tampered_sig}"

    is_valid, err = verify_approval_token_local(
        token=tampered_token,
        expected_device_id="hp-001",
        expected_operation_id="set_temperature",
        expected_params=params,
        secret=SECRET,
    )
    assert is_valid is False
    assert err == "INVALID_APPROVAL"


def test_approval_token_wrong_secret():
    params = {"target_temp": 21.5}
    token = create_approval_token(
        plan_id="plan-123",
        plan_hash="sha256:abc123def456",
        device_id="hp-001",
        operation_id="set_temperature",
        params=params,
        secret=SECRET,
    )

    is_valid, err = verify_approval_token_local(
        token=token,
        expected_device_id="hp-001",
        expected_operation_id="set_temperature",
        expected_params=params,
        secret="different-secret-key-that-does-not-match!",
    )
    assert is_valid is False
    assert err == "INVALID_APPROVAL"


def test_approval_token_mismatched_device():
    params = {"target_temp": 21.5}
    token = create_approval_token(
        plan_id="plan-123",
        plan_hash="sha256:abc123def456",
        device_id="hp-001",
        operation_id="set_temperature",
        params=params,
        secret=SECRET,
    )

    is_valid, err = verify_approval_token_local(
        token=token,
        expected_device_id="hp-002",  # Different device!
        expected_operation_id="set_temperature",
        expected_params=params,
        secret=SECRET,
    )
    assert is_valid is False
    assert err == "INVALID_APPROVAL"


def test_idempotency_key_deterministic():
    key1 = compute_idempotency_key("plan-1", "hp-1", "power_off")
    key2 = compute_idempotency_key("plan-1", "hp-1", "power_off")
    key3 = compute_idempotency_key("plan-1", "hp-2", "power_off")

    assert key1 == key2
    assert key1 != key3
    assert key1.startswith("idem:")
