"""Security helpers: approval token signing (HMAC) and idempotency key generation.

Token format mirrors the Device Gateway's tokens.py so the server can verify.
Format: base64url(canonical_payload_json).hmac_sha256_hex

Rules enforced (rules.md §2.4, §2.9):
- Signing secret comes from env only (never hardcoded, never logged).
- Every write call carries an idempotency_key AND a signed approval_token.
- No token → no write call (enforced in executor.py).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

from app.config import get_settings


# ---------------------------------------------------------------------------
# Canonical JSON serialization (must match the gateway's implementation)
# ---------------------------------------------------------------------------


def canonical_json_bytes(data: Any) -> bytes:
    """Deterministic JSON bytes with sorted keys — used for HMAC and hashing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


# ---------------------------------------------------------------------------
# Params hash
# ---------------------------------------------------------------------------


def compute_params_hash(params: dict[str, Any]) -> str:
    """SHA-256 of canonical JSON params.

    Bound into the approval token so the server can verify the exact params.
    """
    h = hashlib.sha256(canonical_json_bytes(params)).hexdigest()
    return f"sha256:{h}"


# ---------------------------------------------------------------------------
# Idempotency key
# ---------------------------------------------------------------------------


def compute_idempotency_key(plan_id: str, device_id: str, action: str) -> str:
    """Deterministic key = sha256(plan_id:device_id:action).

    The server enforces idempotency: same key → same result (cached).
    (architecture.md §14.2, rules.md §2.5 rule 1)
    """
    raw = f"{plan_id}:{device_id}:{action}"
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return f"idem:{h[:32]}"  # 32 hex chars is sufficient and readable in logs


# ---------------------------------------------------------------------------
# Approval token creation
# ---------------------------------------------------------------------------


def create_approval_token(
    plan_id: str,
    plan_hash: str,
    device_id: str,
    operation_id: str,
    params: dict[str, Any],
    *,
    exp: int | None = None,
    secret: str | None = None,
) -> str:
    """Generate an HMAC-SHA256 approval token for one device command.

    The token binds: plan_id, plan_hash, device_id, operation_id, params_hash, exp.
    The Device Gateway verifies all six fields and the signature.

    Args:
        plan_id: The execution plan ID.
        plan_hash: The plan's SHA-256 hash (must match what the gateway stored).
        device_id: The exact device this token is valid for.
        operation_id: The operation (e.g. "set_temperature").
        params: The command parameters (e.g. {"setpoint_c": 24.0}).
        exp: Unix timestamp expiry. Defaults to now + token_max_age_s from config.
        secret: Override the signing secret (for testing only).

    Returns:
        Token string: ``base64url(payload_json).hmac_sha256_hex``
    """
    settings = get_settings()
    secret_key = (secret or settings.approval_signing_secret.get_secret_value()).encode("utf-8")

    if exp is None:
        exp = int(time.time()) + settings.token_max_age_s

    params_hash = compute_params_hash(params)
    payload: dict[str, Any] = {
        "plan_id": plan_id,
        "plan_hash": plan_hash,
        "device_id": device_id,
        "operation_id": operation_id,
        "params_hash": params_hash,
        "exp": exp,
    }

    payload_bytes = canonical_json_bytes(payload)
    sig = hmac.new(secret_key, payload_bytes, hashlib.sha256).hexdigest()
    payload_b64 = base64.urlsafe_b64encode(payload_bytes).decode("ascii").rstrip("=")
    return f"{payload_b64}.{sig}"


# ---------------------------------------------------------------------------
# Approval token verification (client-side sanity check)
# ---------------------------------------------------------------------------


def verify_approval_token_local(
    token: str,
    expected_device_id: str,
    expected_operation_id: str,
    expected_params: dict[str, Any],
    *,
    secret: str | None = None,
    current_time: int | None = None,
) -> tuple[bool, str | None]:
    """Verify a token locally before sending it to the gateway.

    Returns (is_valid, error_code_or_None).
    Error codes: "INVALID_APPROVAL", "TOKEN_EXPIRED"

    Note: The gateway performs the authoritative verification.
    This is a defence-in-depth check on the client side.
    """
    settings = get_settings()
    secret_key = (secret or settings.approval_signing_secret.get_secret_value()).encode("utf-8")

    parts = token.strip().split(".")
    if len(parts) != 2:
        return False, "INVALID_APPROVAL"

    payload_b64, signature = parts
    rem = len(payload_b64) % 4
    if rem:
        payload_b64 += "=" * (4 - rem)

    try:
        payload_bytes = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        payload: dict[str, Any] = json.loads(payload_bytes.decode("utf-8"))
    except Exception:  # noqa: BLE001
        return False, "INVALID_APPROVAL"

    # Signature
    canonical_bytes = canonical_json_bytes(payload)
    expected_sig = hmac.new(secret_key, canonical_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected_sig.lower()):
        return False, "INVALID_APPROVAL"

    # Expiry
    now = int(time.time()) if current_time is None else current_time
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or now > exp:
        return False, "TOKEN_EXPIRED"

    # Bound fields
    if payload.get("device_id") != expected_device_id:
        return False, "INVALID_APPROVAL"
    if payload.get("operation_id") != expected_operation_id:
        return False, "INVALID_APPROVAL"

    expected_hash = compute_params_hash(expected_params)
    if payload.get("params_hash") != expected_hash:
        return False, "INVALID_APPROVAL"

    return True, None
