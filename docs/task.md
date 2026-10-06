
# Tasks: HeatGuard AI

**Legend:** `[x]` done (v1) · `[ ]` planned · **P0** must · **P1** should · **P2** nice to have
**Sizes:** S ≈ ½ day · M ≈ 1–2 days · L ≈ 3–5 days

---

## Phase 0–3: v1 Baseline (done)

- [x] FastAPI app, config with `LLM_PROVIDER`, direct device API client (replaced by the MCP client in Phase 3A), Docker + Nginx
- [x] React + TypeScript + Vite + Tailwind chat UI and device list
- [x] State types, read tools, `get_sensor_history` with dynamic windows and Pandas stats
- [x] `router_node`, single workers, `_run_tool_loop`
- [x] Orchestrator, `dispatch_workers` with `Send`, parallel workers, `operator.add` reducer, aggregator
- [x] `POST /api/chat`, `GET /api/controllers`, `GET /api/telemetry/{id}`, `/health`

---

## Phase 3A: MCP Integration and Local Docker (do first)

The device gateway is a **separate project**. Its own task list is in `mcp-server.md`. Tasks here are for this repo.

### 3A.1 MCP client (P0, M)
- [ ] `mcp_client.py`: Streamable HTTP session, bearer auth, reconnect with backoff, per-call timeouts
- [ ] Load tools list at startup; **schema pinning** (hash name + description + input schema); fail on change
- [ ] `LLM_TOOL_ALLOWLIST` → convert only allow-listed **read** tools with `langchain-mcp-adapters`
- [ ] Executor helper that calls `execute_operations` directly (never exposed to the LLM)
- [ ] Map MCP errors to ledger statuses (see `design.md` section 13)
- [ ] Remove the old `device.py` HTTP client and direct vendor calls

### 3A.2 Approval token (P0, S)
- [ ] Sign `approval_token` (HMAC) with `plan_id`, `plan_hash`, `device_id`, `operation_id`, `params_hash`, `exp`
- [ ] Add `idempotency_key` to every write
- [ ] Shared secret from env (`APPROVAL_SIGNING_SECRET`)

### 3A.3 Docker Compose (P0, M)
- [ ] `docker-compose.yml` with `mcp-server`, `postgres`, `backend`, `frontend` (see `architecture.md` section 15)
- [ ] Health checks and `depends_on: service_healthy`
- [ ] `.env.example` (`MCP_AUTH_TOKEN`, `APPROVAL_SIGNING_SECRET`, `POSTGRES_PASSWORD`, `LLM_PROVIDER`)
- [ ] Ollama on host reachable from backend (`host.docker.internal`)
- [ ] README: run both projects with one `docker compose up --build`

### 3A.4 Contract tests (P0, M)
- [ ] Test that the simulator server exposes the expected tools and schemas
- [ ] Test: write without token is rejected by the server
- [ ] Test: LLM tool list contains no write tool

---

## Phase 4: Safe Automation Foundation (v1.1) — **start here**

### 4.1 Device registry (P0, L)
- [ ] Postgres `devices` table: room, zone, tags, aliases, criticality, limits
- [ ] Sync from the gateway (`get_available_devices`) + overlay merge; cache with TTL
- [ ] Admin endpoints `GET/PUT /api/registry/devices`
- [ ] "Needs labels" list; unlabeled devices treated as protected in bulk off
- [ ] Seed script and fixtures for tests
- **Done when:** "living room" resolves to a stable device list in tests

### 4.2 Schemas and plan hash (P0, M)
- [ ] Pydantic: `ActionRequest`, `TargetSelector`, `PlannedAction`, `ExecutionPlan`, `DeviceResult`
- [ ] Canonical `plan_hash` (sorted, stable JSON → sha256)
- [ ] Postgres tables: `plans`, `plan_actions`, `approvals`, `action_results`, `audit_log`

### 4.3 Postgres checkpointer (P0, M)
- [ ] Add `AsyncPostgresSaver` and `thread_id` handling
- [ ] Test: pause at `interrupt()`, restart server, resume works

---

## Phase 5: Understanding and Resolving (v1.1)

### 5.1 Intent parser (P0, M)
- [ ] `parse_intent` node with structured output and Pydantic validation
- [ ] Retry once on invalid output, then `CLARIFY`
- [ ] Few-shot examples: all, except, relative words, missing unit, mixed units, multiple actions
- [ ] Add `automation` label to router; route all writes here
- [ ] Remove `execute_operations` from LLM tool lists

### 5.2 Resolver (P0, M)
- [ ] Exact match on room, zone, tag, name, alias (normalized)
- [ ] Fuzzy match with high threshold; show match in preview
- [ ] Return `resolved | ambiguous | none` with candidates
- [ ] "All except X": never expand when X unresolved
- [ ] Unit tests and property tests

### 5.3 Validator and snapshot (P0, M)
- [ ] Capability check using `list_operations` / `list_parameters`
- [ ] Range check, mode check, °F ↔ °C conversion
- [ ] Read current state (parallel reads) and mark `SKIPPED` for offline, locked, already-set
- [ ] Planner builds `PlannedAction` with `operation_id` and `instruction_id`

---

## Phase 6: Policy and Human-in-the-Loop (v1.1)

### 6.1 Policy engine (P0, L)
- [ ] `policy.yaml` loader with version
- [ ] Rules: range, missing value, unit unclear, ambiguity, device count, fleet share, power-off scope, critical device, active alarm, large delta, off-hours, mode support
- [ ] Strictest-wins combiner; fail closed on errors
- [ ] Property tests: never `AUTO_EXECUTE` for scope "all"
- [ ] Policy version stored on each plan

### 6.2 Clarification loop (P0, M)
- [ ] `clarify` node with `interrupt()`; options built from real matches
- [ ] Optional LLM rephrase with option-lock check
- [ ] Max 2 rounds, then safe cancel

### 6.3 Approval flow (P0, L)
- [ ] `approval` node with `interrupt()` and preview payload
- [ ] `POST /api/chat/resume` (approve, reject, edit, confirm text)
- [ ] Bind to `plan_hash`; TTL (default 5 min); `PLAN_EXPIRED` and `PLAN_HASH_MISMATCH` errors
- [ ] Edit flow: remove devices → rebuild plan → new hash → re-run policy
- [ ] HIGH risk typed confirmation
- [ ] Revalidate before execute (drift check: state, alarms, offline)
- [ ] Record approver identity

---

## Phase 7: Execution, Verification, Reporting (v1.1)

### 7.1 Executor (P0, L)
- [ ] Batches with semaphore (default 5), connect and total timeouts
- [ ] Idempotency keys
- [ ] Retry only idempotent absolute commands (max 2, backoff, connect or 5xx)
- [ ] No blind retry after timeout; read back first
- [ ] Abort when failure rate > threshold
- [ ] Circuit breaker for the MCP gateway

### 7.2 Verifier (P0, M)
- [ ] Read-back poll (up to 3 × 2 s) and compare to target
- [ ] Statuses: `VERIFIED`, `ACCEPTED_UNVERIFIED`, `FAILED`, `SKIPPED`, `UNKNOWN`
- [ ] Overall status: `COMPLETED`, `PARTIAL`, `FAILED`, `REJECTED`, `EXPIRED`

### 7.3 Reporter (P0, M)
- [ ] Ledger → report JSON and table rendering
- [ ] Optional LLM summary from ledger only; count-check and drop on mismatch
- [ ] Next actions: retry failed, check unknown, roll back

### 7.4 Follow-up plans (P1, M)
- [ ] `POST /api/plans/{id}/retry-failed`
- [ ] `POST /api/plans/{id}/rollback` (new plan using `before` values, needs approval)
- [ ] "Check unknown" re-reads state and updates ledger

### 7.5 Audit (P0, S)
- [ ] Append-only audit events for every stage
- [ ] Store `policy_version`, `prompt_version`, `model` on plans

---

## Phase 8: Frontend (v1.1)

- [ ] `ClarificationCard` with option buttons and free text (P0, M)
- [ ] `ApprovalCard`: change list, not-changed list with reasons, warnings, risk badge, countdown, typed confirm, Edit with checkboxes (P0, L)
- [ ] `ResultReportCard` with statuses (icon + text) and action buttons (P0, M)
- [ ] Restore pending card after reload via `thread_id` (P0, S)
- [ ] Handle pending plan + new message ("cancel or continue?") (P1, S)
- [ ] Rooms and zones in sidebar from registry (P1, M)
- [ ] Recent plans list (P2, M)
- [ ] Frontend tests (Vitest) for the three cards (P1, M)

---

## Phase 9: Compound Requests (v1.1)

- [ ] Orchestrator produces read tasks + one merged automation task (P0, M)
- [ ] Run read workers in parallel with pipeline up to policy gate (P1, M)
- [ ] Show read results together with approval card (P1, S)
- [ ] Conflict detection between write actions in one message → `CLARIFY` (P1, S)

---

## Phase 10: Security (v1.1)

- [ ] Auth on all `/api` routes; user identity in audit (P0, L)
- [ ] Rate limit `/api/chat` (P1, S)
- [ ] Sanitize device-provided strings; delimit tool data in prompts (P0, S)
- [ ] Role model (viewer, operator, supervisor) (P2, L, in v2)

---

## Phase 11: Testing and Evals

- [ ] Use the simulator MCP server in fault-injection mode: timeout, 5xx, slow, partial success, offline device, delayed state, state drift (P0, M; built in the MCP server project)
- [ ] Unit tests: resolver, validator, policy, conversions, hash (P0, M)
- [ ] Property tests with `hypothesis` for safety invariants (P1, M)
- [ ] Graph tests: interrupt, resume, reject, expire, edit, clarify loops (P0, L)
- [ ] Mandatory safety tests from `rules.md` section 4 (P0, M)
- [ ] Golden datasets: parse (150), resolve, safety, clarification (P0, L)
- [ ] Eval runner and CI gate; compare models (Ollama small vs cloud) (P1, L)
- [ ] Report accuracy test: report == fake-device truth (P0, M)

---

## Phase 12: Observability and Ops (v1.2)

- [ ] Structured logs with `plan_id`, `thread_id`, `node` (P0, S)
- [ ] Tracing for nodes and MCP tool calls (P0, M)
- [ ] Metrics: decisions, approval and reject rates, partial and `UNKNOWN` rates, verify mismatches (P1, M)
- [ ] Alerts: circuit open, rising `FAILED` or `UNKNOWN` (P1, M)
- [ ] CI: lint, types, tests, evals, Docker build (P0, M)
- [ ] `.env.example`, runbook, staging with the simulator gateway (P1, M)

---

## Phase 13: Later (v1.2 / v2)

- [ ] Streaming progress per device via SSE (P1, L)
- [ ] Short cache for devices and operations (P1, S)
- [ ] LTTB downsampling option for history (P2, M)
- [ ] Scheduled automations ("every night at 10 set bedrooms to 18°C") (P1, L)
- [ ] Proactive alarm triage (P1, L)
- [ ] Role-based approvals, supervisor for HIGH risk (P1, L)
- [ ] Learn thresholds from approval and reject data (P2, M)
- [ ] Multi-site support (P2, L)

---

## Suggested Sprints

| Sprint | Focus | Tasks |
|--------|-------|-------|
| 1 | Foundation | 3A (MCP client, token, compose), simulator gateway (start, other repo), 4.1, 4.2, 4.3, 5.2 (resolver) |
| 2 | Understand and decide | 5.1, 5.3, 6.1, mandatory safety tests |
| 3 | Human loop | 6.2, 6.3, Phase 8 (clarification + approval cards) |
| 4 | Execute and report | 7.1 to 7.5, report card, fault-injection tests |
| 5 | Harden and ship | Phase 9, Phase 10, evals in CI, Phase 12 basics, pilot |

## Demo Script (for review or interview)

1. "Set all living-room devices to 24°C" → approval card (3 devices, before → after) → approve → verified report.
2. "Make it warmer" → clarification question.
3. "Set living room to 35" → blocked with reason.
4. "Turn off all devices except the bedroom one" → bedroom ambiguity → approval with critical device protected, typed confirm.
5. Fault injection on: 1 failure, 1 timeout → partial report with retry and roll back buttons.

## Open Questions (blockers)

1. Source of room and zone labels? (affects 4.1)
2. List of critical devices and operations? (affects 6.1)
3. Safe temperature limits per device type and site? (affects 5.3, 6.1)
4. Always confirm multi-device writes, or use a threshold? (affects 6.1)
5. Who can approve HIGH risk plans? (affects 6.3, 10)
6. Is rollback required for all actions? (affects 7.4)
7. Which real vendor adapter comes after the simulator? (affects the MCP server project)
8. Will other agents or teams use the gateway? (affects auth and tool naming)
