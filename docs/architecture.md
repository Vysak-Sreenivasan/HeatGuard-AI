# Architecture: HeatGuard AI

## 1. Core Idea

> **The LLM proposes. Code decides. A human approves risk. Read-back proves the result.**

The LLM is great at understanding language. It is not a safe place for rules, device IDs, or the truth about what changed. So we split the work:

| Layer | Owner | Examples |
|-------|-------|----------|
| Understand | **LLM** | Parse "turn off all except bedroom" into a structured request; phrase questions; summarize |
| Decide and act | **Deterministic code** | Find devices, check limits, score risk, require approval, run commands, retry rules, verify, build the report |
| Approve | **Human** | Approve, reject, or edit risky plans |

## 2. System Context

```mermaid
flowchart LR
    Op([Operator]) --> UI[React Dashboard]
    UI --> NG[Nginx]
    NG --> API[FastAPI]
    API --> AG[LangGraph Agent]
    AG --> LLM[(LLM: Ollama / OpenAI / Anthropic)]
    AG --> SA[Safe Automation Engine]
    SA --> REG[(Device Registry)]
    SA --> POL[Policy Engine]
    SA --> EX[Executor + Verifier]
    EX -->|write tool, direct call| MC[MCP client]
    AG -->|read tools, allow-list| MC
    MC -->|Streamable HTTP + token| MS[Device Gateway MCP Server<br/>separate project]
    MS --> AD[Adapters: simulator / vendor APIs]
    AD --> HW[Heat pumps or simulated devices]
    AG --> PG[(Postgres: checkpoints, plans, audit)]
```

## 3. Components

| Component | Tech | Job |
|-----------|------|-----|
| Frontend | React 18, TypeScript, Vite, Tailwind | Chat, device list, **clarification and approval cards**, **result report** |
| API | FastAPI | `/api/chat`, `/api/chat/resume`, plans, controllers, telemetry, health |
| Agent graph | LangGraph | Routing, read workers, parallel fan-out, **safe write pipeline with `interrupt()`** |
| Safe Automation Engine | Plain Python (no LLM) | Resolver, validator, planner, policy, executor, verifier, reporter |
| Device Registry | Postgres + cache | Devices with room, zone, tags, aliases, criticality, capability limits |
| Policy | YAML + Python | Risk rules and thresholds (versioned) |
| MCP client | `mcp` SDK + `langchain-mcp-adapters` | Talks to the Device Gateway: read tools for the LLM (allow-list), write tool for the executor only |
| Device Gateway MCP server | Python MCP SDK (FastMCP), separate repo | Device tools, adapters, simulator, per-command validation (see `mcp-server.md`) |
| Persistence | Postgres | LangGraph checkpointer, `plans`, `plan_actions`, `audit_log` |
| Analytics | Pandas | History stats (as in v1) |

## 4. Backend Layout

```
backend/app/
  main.py  config.py  mcp_client.py   # only place that talks to the gateway
  agent/
    graph.py  nodes.py  state.py  tools.py        # tools.py = READ tools only for the LLM
    prompts.py
  automation/                                    # NEW: no LLM calls inside, except parser/summary wrappers
    schemas.py        # ActionRequest, TargetSelector, PlannedAction, ExecutionPlan, DeviceResult
    registry.py       # load/cache devices + rooms/aliases/criticality
    resolver.py       # selector -> devices (exact, alias, fuzzy w/ threshold)
    validator.py      # capabilities, ranges, unit conversion, no-op, offline
    planner.py        # reads current state, builds ExecutionPlan + plan_hash
    policy.py         # risk rules -> AUTO_EXECUTE | CONFIRM | CLARIFY | BLOCK
    executor.py       # bounded concurrency, timeouts, idempotency, retry rules, circuit breaker
    verifier.py       # read-back and compare
    reporter.py       # ledger -> report (table + summary input)
    audit.py          # write audit rows
  policy.yaml        # thresholds and rules
```

**Key change from v1:** the write tool `execute_operations` is never given to the LLM. It is an MCP tool on the gateway that only `executor.py` calls, after policy and approval, with an idempotency key and a signed approval token (section 14).

## 5. Agent Graph

```mermaid
flowchart TD
    S([START]) --> R[router_node]
    R -- chitchat --> C[chitchat_worker] --> E([END])
    R -- read --> RW[read_worker] --> E
    R -- parallel --> O[orchestrator_node]
    O --> D{dispatch_workers}
    D -- Send --> PR[parallel_read_worker] --> AGG[aggregator_node]
    D -- Send --> PI
    R -- automation --> PI
    subgraph SAFE[Safe Automation pipeline]
        PI[parse_intent LLM] --> RT[resolve_targets]
        RT --> VA[validate_and_snapshot]
        VA --> PL[build_plan]
        PL --> PO{policy_gate}
        PO -- CLARIFY --> CL[clarify interrupt]
        CL --> PI
        PO -- BLOCK --> RP
        PO -- CONFIRM --> AP[approval interrupt]
        AP -- approve --> RV[revalidate plan hash + TTL]
        AP -- reject --> RP
        AP -- edit --> PL
        PO -- AUTO_EXECUTE --> RV
        RV --> EX[execute_batches]
        EX --> VE[verify_readback]
        VE --> RP[report_node]
    end
    RP --> AGG
    AGG --> E
    RP --> E
```

Notes:
- `automation` is a new router intent for any write request. A single device is the same pipeline with N = 1.
- In a compound request ("check alarms and set living room to 24"), the orchestrator sends the read tasks as parallel workers and **one** automation task. All writes merge into **one plan**, so the operator approves once.
- `interrupt()` pauses the graph. State is saved by the Postgres checkpointer. The API resumes with `Command(resume=...)`.

## 6. Pipeline Stages (who decides what)

| # | Stage | Decider | Input → Output |
|---|-------|---------|----------------|
| 1 | `parse_intent` | **LLM** (structured output) | text → `ActionRequest` + `ambiguities[]` + `confidence` |
| 2 | `resolve_targets` | **Code** | selector → exact device IDs from registry (or `ambiguous` / `none`) |
| 3 | `validate_and_snapshot` | **Code** | check capabilities, range, mode; read current state; mark skips |
| 4 | `build_plan` | **Code** | `ExecutionPlan` with `plan_id`, `plan_hash`, before → after per device |
| 5 | `policy_gate` | **Code** | plan → `AUTO_EXECUTE` / `CONFIRM` / `CLARIFY` / `BLOCK` + reasons |
| 6 | `clarify` | **Human** (question text by code; LLM may rephrase) | answer → back to stage 1 or 2 |
| 7 | `approval` | **Human** | approve / reject / edit |
| 8 | `revalidate` | **Code** | hash match, TTL, drift check (state changed? alarm appeared?) |
| 9 | `execute_batches` | **Code** | commands with concurrency, retries, abort rule |
| 10 | `verify_readback` | **Code** | read device state, compare to target |
| 11 | `report_node` | **Code** builds facts; **LLM** writes a short summary from the ledger only | ledger → report |

### 6.1 Structured intent (LLM output)

```json
{
  "action": "power_off",
  "value": null,
  "unit": null,
  "target": {
    "include": [],
    "exclude": [{"type": "room", "text": "bedroom"}],
    "scope": "all"
  },
  "ambiguities": [],
  "confidence": 0.86
}
```

The LLM output is validated by Pydantic. Invalid output → one retry → then `CLARIFY`. It never returns device IDs.

### 6.2 Resolver rules (deterministic)

1. Exact match on room, zone, tag, device name, alias (case and punctuation insensitive).
2. Fuzzy match only above a high threshold, and the preview shows the match.
3. More than one candidate → `ambiguous` → clarify with options.
4. No match → `none` → clarify. **For "all except X": if X resolves to nothing, never fall back to "all".**
5. "The bedroom one" with several bedroom devices → ambiguous.

### 6.3 Policy engine (deterministic, config-driven)

| Rule | Example | Effect |
|------|---------|--------|
| Value out of safe range | 35°C setpoint | `BLOCK` |
| Value missing or relative ("warmer") | no number | `CLARIFY` |
| Unit unclear | "72" with no unit | `CLARIFY` |
| Target ambiguous or empty | "bedroom" has 2 devices | `CLARIFY` |
| Devices in plan > `auto_max_devices` (default 1) | living-room set | `CONFIRM` |
| Share of fleet affected > 30% | many devices | `CONFIRM`, risk `HIGH` |
| Power off + scope "all" or "all except" | shutdown | `CONFIRM`, risk `HIGH` |
| Plan includes a `critical` device (freeze protection, circulation pump) | power off | exclude by default + `HIGH` + warning |
| Active alarm on target device | alarm present | `CONFIRM` + warning |
| Setpoint change > 5°C | 18 → 26 | `CONFIRM` |
| Outside allowed hours or maintenance lock | night shutdown | `CONFIRM` or `BLOCK` per config |
| Mode not supported | "cool" on heat-only | `BLOCK` for that device |

Final decision is the **strictest** rule that fires. Anything unknown fails closed (`CONFIRM` or `CLARIFY`).

### 6.4 Execution rules

- **Read current state first.** If a device already has the target value, mark `SKIPPED (already set)`.
- **Batches:** run in batches (default 5 concurrent). Check failure rate after each batch. If the failure rate is above 30%, stop the rest (`SKIPPED (aborted)`).
- **Timeouts:** connect 3 s, total 10 s per command.
- **Idempotency:** each command carries `idempotency_key = plan_id + device_id + action`.
- **Retry:**
  - Absolute set commands (`set 24°C`, `power off`) are idempotent. Retry up to 2 times on connect errors or 5xx, with backoff.
  - Non-idempotent commands (toggle, increment): **no** automatic retry.
  - On a timeout after send, the outcome is unknown. Do **not** resend. Read back first, then decide.
- **Circuit breaker:** if the gateway fails repeatedly, stop sending and report.
- **Verify:** after success or timeout, read back (poll up to 3 times, 2 s apart). Compare to target.

### 6.5 Result statuses (per device)

| Status | Meaning |
|--------|---------|
| `VERIFIED` | Command accepted and read-back matches target |
| `ACCEPTED_UNVERIFIED` | API said OK, but read-back did not confirm in time |
| `FAILED` | API error after retries; state read-back shows unchanged (or could not be read) |
| `SKIPPED` | Not sent: offline, already set, locked, critical excluded, aborted, or removed by the human |
| `UNKNOWN` | Timeout and read-back also failed. We do not know the real state |

Overall plan status: `COMPLETED` (all VERIFIED or valid SKIPPED), `PARTIAL`, `FAILED`, `REJECTED`, `EXPIRED`.

### 6.6 Report rules

- The **table is rendered by code** from the ledger: device, before, target, after (read-back), status, reason.
- The LLM only gets the ledger JSON and writes a 2–3 line summary. It cannot add devices or change counts. A check compares counts in the summary to ledger counts; on mismatch, the summary is dropped and only the table is shown.
- Always state: how many changed, how many failed, how many unknown, how many skipped, and what the operator can do next (retry failed, roll back).

## 7. State and Data Model

```python
class ActionRequest(BaseModel):
    action: Literal["set_temperature","power_on","power_off","set_mode"]
    value: float | None; unit: Literal["C","F"] | None
    target: TargetSelector
    ambiguities: list[str] = []; confidence: float

class PlannedAction(BaseModel):
    device_id: str; operation_id: str; instruction_id: str
    params: dict; before: dict; target: dict
    status: Literal["PLANNED","SKIPPED"]; skip_reason: str | None

class ExecutionPlan(BaseModel):
    plan_id: str; plan_hash: str; created_at: datetime; expires_at: datetime
    actions: list[PlannedAction]
    risk: Literal["LOW","MEDIUM","HIGH"]
    decision: Literal["AUTO_EXECUTE","CONFIRM","CLARIFY","BLOCK"]
    reasons: list[str]; warnings: list[str]

class DeviceResult(BaseModel):
    device_id: str; status: str; before: dict; target: dict; after: dict | None
    attempts: int; error: str | None; latency_ms: int
```

Postgres tables: `devices` (registry), `plans`, `plan_actions`, `approvals`, `audit_log`, plus LangGraph checkpoint tables.

**Plan lifecycle:**

```mermaid
stateDiagram-v2
    [*] --> DRAFT
    DRAFT --> NEEDS_CLARIFICATION
    NEEDS_CLARIFICATION --> DRAFT: answer
    NEEDS_CLARIFICATION --> CANCELLED: 2 rounds or timeout
    DRAFT --> BLOCKED
    DRAFT --> AWAITING_APPROVAL
    DRAFT --> APPROVED: auto (low risk)
    AWAITING_APPROVAL --> APPROVED
    AWAITING_APPROVAL --> REJECTED
    AWAITING_APPROVAL --> EXPIRED
    AWAITING_APPROVAL --> DRAFT: edit
    APPROVED --> EXECUTING
    EXECUTING --> COMPLETED
    EXECUTING --> PARTIAL
    EXECUTING --> FAILED
```

## 8. Sequence: "Turn off all devices except the bedroom one"

```mermaid
sequenceDiagram
    participant U as Operator
    participant L as LLM
    participant C as Code (engine)
    participant T as MCP Server (gateway)
    U->>L: "Turn off all devices except the bedroom one"
    L-->>C: ActionRequest(power_off, scope=all, exclude=room:bedroom)
    C->>C: resolve: 12 devices; bedroom = 2 devices (ambiguous)
    C-->>U: "Bedroom has 2 devices. Keep both on, or only one?" [options]
    U->>C: "Keep both"
    C->>T: read current state (10 devices)
    C->>C: policy: power_off + scope all => HIGH; 1 critical device auto-excluded
    C-->>U: Approval card (9 devices off, 1 critical kept, 2 bedroom kept) + type "9" to confirm
    U->>C: approve (plan_hash)
    C->>C: revalidate hash, TTL, drift
    C->>T: execute in batches (concurrency 5)
    T-->>C: 7 ok, 1 error, 1 timeout
    C->>T: read-back all 9
    C->>C: ledger: 7 VERIFIED, 1 FAILED, 1 VERIFIED (timeout but actually off)
    C-->>U: Report table + summary + [Retry failed] [Roll back]
```

## 9. Persistence and Resume

- Required now: `AsyncPostgresSaver` (needed for `interrupt()` across restarts).
- `thread_id` per chat. Pending clarification or approval is restored after reload.
- `POST /api/chat/resume` carries `{thread_id, plan_hash, decision, answers?, edits?}`.
- Approval is rejected if `plan_hash` differs or the plan has expired.

## 10. Security

- Auth on all `/api` routes. Approver identity stored in `approvals`.
- Roles (v2): viewer, operator, supervisor (HIGH risk approvals).
- LLM has **no write tool**. Prompt injection cannot trigger a write because the write path needs policy + approval.
- Device names and alarm text are sanitized and passed as data.
- Secrets in env vars only; never logged.

## 11. Observability

- Trace each node and each MCP tool call. Tag by `plan_id`.
- Metrics: plans by decision, approval rate, reject rate, edit rate, partial rate, `UNKNOWN` rate, verify mismatch rate, clarification rounds, latency per stage.
- Alerts: rising `UNKNOWN` or `FAILED`, circuit breaker open, policy `BLOCK` spikes.

## 12. Deployment

Docker Compose: `nginx`, `backend`, `postgres`. Frontend built and served by Nginx. Config through env and `policy.yaml` (mounted, versioned; policy version stored on each plan).

## 13. Decisions and Trade-offs

| Decision | Why | Trade-off |
|----------|-----|-----------|
| LLM has no write tool | Safety by design | More code to write |
| Registry overlay for rooms and criticality | LLM and API cannot know our rooms | Needs admin data and upkeep |
| Policy as code + YAML | Predictable, testable, auditable | Rules need tuning |
| Plan hash + TTL + revalidation | Prevents stale approvals | Extra check before execute |
| Read-back verification | Honest reports | Extra API calls and a few seconds |
| Per-device statuses incl. `UNKNOWN` | Truth under timeouts | Slightly more complex UI |
| Always confirm multi-device writes (default) | Safest start | More clicks; can relax with data |
| Abort threshold on failures | Avoid half-broken fleet | Might stop a plan that could finish |

---

## 14. Device Gateway via MCP (separate project)

Device access is **not** inside this repo. It lives in a separate project, the **Device Gateway MCP server** (`heatguard-mcp-server`, spec in `mcp-server.md`). This app is an MCP **client**.

**Why this split**
- The app stays generic. Vendor APIs, device quirks, and the simulator live behind one standard tool interface.
- The same gateway can serve other agents and tools later.
- The simulator adapter gives us a safe local backend with fault injection for tests and demos.

**Transport:** Streamable HTTP (works across containers; `stdio` does not). Bearer token auth. The server is only on the private Docker network.

### 14.1 Tool exposure (the most important rule)

| MCP tool | Kind | Who may call it |
|----------|------|-----------------|
| `get_available_devices`, `list_parameters`, `get_telemetry_data`, `get_device_alarms`, `list_operations`, `get_sensor_history` | read | LLM workers (through an **allow-list**) and the engine |
| `execute_operations` | **write** | **Only `executor.py`**, after policy and approval. Never bound to the LLM |

How it is enforced in the app:
1. `mcp_client.py` loads the server's tool list at startup.
2. **Read tools for the LLM:** only names in `LLM_TOOL_ALLOWLIST` are converted to LangChain tools (`langchain-mcp-adapters`). Tool annotations such as `readOnlyHint` are used as a second check, but **annotations are hints from the server, not trust**. The allow-list is the real control.
3. **Write tool:** `executor.py` calls `session.call_tool("execute_operations", ...)` directly. It is never added to any LLM tool list.
4. **Schema pinning:** store a hash of each tool's name, description, and input schema. If it changes, refuse to start (or alert) until reviewed. This guards against tool-poisoning and silent server changes.
5. Tool descriptions and outputs are untrusted **data**.

### 14.2 Defense in depth: server-side approval token

The server does not trust the app blindly. Every `execute_operations` call must carry:

| Field | Purpose |
|-------|---------|
| `idempotency_key` | `plan_id + device_id + action`; the server returns the same result on a repeat |
| `approval_token` | HMAC-signed by the app after policy (and human approval when required). Contains `plan_id`, `plan_hash`, `device_id`, `operation_id`, `params_hash`, `exp` |

The server verifies the signature, expiry, and that the token matches the exact command. It also enforces its own limits (range, capability, critical device rules). So a rogue client, or a prompt-injected agent without the signing secret, cannot change hardware even if it reaches the server.

### 14.3 MCP client behavior

- One long-lived session with reconnect and backoff.
- Per-call timeout (connect 3 s, total 10 s). MCP errors and results with `isError = true` count as failures.
- Circuit breaker on the client side.
- Read-back uses `get_telemetry_data` on the same device.
- Every call is traced with `plan_id`, tool name, device, latency, and result status.

### 14.4 Responsibilities by project

| Concern | This app | MCP server |
|---------|----------|------------|
| Understand language, resolve targets, plan, policy, approval, report | ✅ | |
| Device registry overlay (aliases, criticality) | ✅ | provides base labels (room, zone, tags) |
| Vendor protocols, auth to devices, adapters | | ✅ |
| Simulated devices and fault injection | | ✅ |
| Per-command validation and limits (last line of defense) | also | ✅ |
| Idempotency store for writes | sends key | ✅ enforces |
| Audit of commands received | ✅ (plan level) | ✅ (command level) |

## 15. Local Run with Docker Compose

Two repos side by side:

```
projects/
  heatguard-ai/            # this repo: backend, frontend, compose
  heatguard-mcp-server/    # device gateway MCP server (simulator adapter first)
```

`heatguard-ai/docker-compose.yml`:

```yaml
services:
  mcp-server:
    build: ../heatguard-mcp-server
    environment:
      ADAPTER: simulator
      SIM_SEED_FILE: /seed/devices.yaml
      FAULTS_ENABLED: "true"          # local and test only
      MCP_AUTH_TOKEN: ${MCP_AUTH_TOKEN}
      APPROVAL_SIGNING_SECRET: ${APPROVAL_SIGNING_SECRET}
    volumes:
      - ../heatguard-mcp-server/seed:/seed:ro
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request;urllib.request.urlopen('http://localhost:8001/health')"]
      interval: 5s
      retries: 10
    networks: [internal]
    ports: ["8001:8001"]              # optional: for MCP Inspector and debugging

  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
      POSTGRES_DB: heatguard
    volumes: [pgdata:/var/lib/postgresql/data]
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U postgres"]
      interval: 5s
      retries: 10
    networks: [internal]

  backend:
    build: ./backend
    environment:
      MCP_SERVER_URL: http://mcp-server:8001/mcp
      MCP_AUTH_TOKEN: ${MCP_AUTH_TOKEN}
      APPROVAL_SIGNING_SECRET: ${APPROVAL_SIGNING_SECRET}
      LLM_PROVIDER: ${LLM_PROVIDER:-ollama}
      OLLAMA_BASE_URL: http://host.docker.internal:11434
      DATABASE_URL: postgresql://postgres:${POSTGRES_PASSWORD}@postgres:5432/heatguard
    extra_hosts: ["host.docker.internal:host-gateway"]   # Linux: reach Ollama on the host
    depends_on:
      mcp-server: {condition: service_healthy}
      postgres: {condition: service_healthy}
    networks: [internal]

  frontend:
    build: ./frontend                 # multi-stage build served by Nginx, proxies /api to backend
    ports: ["8080:80"]
    depends_on: [backend]
    networks: [internal]

networks:
  internal: {}
volumes:
  pgdata: {}
```

Run: `docker compose up --build` → open `http://localhost:8080`.

Notes:
- The same `APPROVAL_SIGNING_SECRET` must be set on both sides.
- Production: set `FAULTS_ENABLED=false`, do not publish port 8001, use real secrets, and switch `ADAPTER` to a real vendor adapter.
- Debug the server alone with the MCP Inspector against `http://localhost:8001/mcp`.
