"""Database setup — Phase 4.3

Provides:
- Async PostgreSQL engine / session factory (for custom app tables)
- AsyncPostgresSaver setup for LangGraph checkpointing
- Schema migrations for app tables (devices registry, plans, audit_log)

Rules:
- All I/O is async.
- No global mutable state: engine/pool held in app.state.
- Connection string comes from config.py / env (rules.md §7).

References: architecture.md §7, §9
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("heatguard.db")

# ---------------------------------------------------------------------------
# DDL for application tables (run once at startup if not exists)
# ---------------------------------------------------------------------------

# Language: standard PostgreSQL 16
_SCHEMA_DDL = """
-- Device registry overlay: room/zone/alias/criticality overrides
CREATE TABLE IF NOT EXISTS devices (
    device_id       TEXT PRIMARY KEY,
    name            TEXT NOT NULL DEFAULT '',
    room            TEXT NOT NULL DEFAULT '',
    zone            TEXT NOT NULL DEFAULT '',
    tags            TEXT[] NOT NULL DEFAULT '{}',
    aliases         TEXT[] NOT NULL DEFAULT '{}',
    criticality     TEXT NOT NULL DEFAULT 'normal'
                        CHECK (criticality IN ('normal', 'protected', 'critical')),
    capabilities    JSONB NOT NULL DEFAULT '{}',
    raw             JSONB NOT NULL DEFAULT '{}',
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Execution plans
CREATE TABLE IF NOT EXISTS plans (
    plan_id         TEXT PRIMARY KEY,
    plan_hash       TEXT NOT NULL,
    thread_id       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'DRAFT',
    risk            TEXT NOT NULL DEFAULT 'LOW',
    decision        TEXT NOT NULL DEFAULT 'CONFIRM',
    policy_version  TEXT NOT NULL DEFAULT 'unset',
    prompt_version  TEXT NOT NULL DEFAULT 'unset',
    model           TEXT NOT NULL DEFAULT 'unset',
    reasons         JSONB NOT NULL DEFAULT '[]',
    warnings        JSONB NOT NULL DEFAULT '[]',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at      TIMESTAMPTZ NOT NULL,
    approved_by     TEXT,
    approved_at     TIMESTAMPTZ
);

-- Per-device planned actions
CREATE TABLE IF NOT EXISTS plan_actions (
    id              BIGSERIAL PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    device_id       TEXT NOT NULL,
    operation_id    TEXT NOT NULL,
    instruction_id  TEXT NOT NULL,
    params          JSONB NOT NULL DEFAULT '{}',
    before_state    JSONB NOT NULL DEFAULT '{}',
    target_state    JSONB NOT NULL DEFAULT '{}',
    status          TEXT NOT NULL DEFAULT 'PLANNED',
    skip_reason     TEXT,
    result_status   TEXT,
    after_state     JSONB,
    attempts        INT NOT NULL DEFAULT 0,
    error           TEXT,
    error_code      TEXT,
    latency_ms      INT NOT NULL DEFAULT 0,
    executed_at     TIMESTAMPTZ
);

-- Approval records
CREATE TABLE IF NOT EXISTS approvals (
    id              BIGSERIAL PRIMARY KEY,
    plan_id         TEXT NOT NULL REFERENCES plans(plan_id) ON DELETE CASCADE,
    plan_hash       TEXT NOT NULL,
    decision        TEXT NOT NULL,   -- 'APPROVED' | 'REJECTED' | 'EXPIRED'
    approver_id     TEXT,
    decided_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Append-only audit log (rules.md §2.8)
CREATE TABLE IF NOT EXISTS audit_log (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    plan_id         TEXT,
    thread_id       TEXT,
    node            TEXT NOT NULL,
    event           TEXT NOT NULL,
    detail          JSONB NOT NULL DEFAULT '{}'
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_plans_thread ON plans(thread_id);
CREATE INDEX IF NOT EXISTS idx_plan_actions_plan ON plan_actions(plan_id);
CREATE INDEX IF NOT EXISTS idx_audit_plan ON audit_log(plan_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
"""


async def create_app_tables(database_url: str) -> None:
    """Create application tables if they don't exist.

    Called once at startup before the LangGraph checkpointer setup.
    """
    # Use psycopg3 directly for DDL (not ORM — keep it simple and auditable)
    try:
        import psycopg  # psycopg3

        # Convert asyncpg-style URL to psycopg3 format
        url = _to_psycopg_url(database_url)
        async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
            await conn.execute(_SCHEMA_DDL)
        logger.info("App tables created/verified.")
    except Exception as exc:
        logger.error("Failed to create app tables: %s", exc)
        raise


async def setup_checkpointer(database_url: str) -> Any:
    """Build and setup the AsyncPostgresSaver for LangGraph.

    Returns:
        An AsyncPostgresSaver instance with the LangGraph checkpoint tables
        already migrated (saver.setup() called).

    The caller must store this in app.state and pass it to build_graph().
    """
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    url = _to_psycopg_url(database_url)
    saver = AsyncPostgresSaver.from_conn_string(url)
    # Creates the langgraph checkpoint tables (idempotent)
    await saver.setup()
    logger.info("AsyncPostgresSaver ready.")
    return saver


def _to_psycopg_url(url: str) -> str:
    """Convert postgresql:// to psycopg3-compatible URL."""
    # psycopg3 uses postgresql:// or postgres:// — both are fine.
    # asyncpg uses postgresql+asyncpg:// — strip that driver suffix if present.
    if "+asyncpg" in url:
        url = url.replace("+asyncpg", "")
    return url
