# PRD: HeatGuard AI — Safe Agentic Control for Heat Pump Fleets

**Repo:** `heatguard-ai`
**Status:** v1 (agentic co-pilot) is built. v1.1 **Safe Automation** is the current enhancement.

---

## 1. Summary

HeatGuard AI is a natural-language co-pilot for operators of commercial HVAC and industrial heat pump fleets. The operator types what they need. The agent reads telemetry, checks alarms, studies history, and controls devices through a **Device Gateway MCP server** (a separate project). The gateway hides vendor APIs and simulators behind one standard tool interface, so this app stays generic.

**The enhancement (Safe Automation):** the agent can now handle requests that touch **many devices at once**, for example:

- "Set all living-room devices to 24°C"
- "Turn off all devices except the bedroom one"

The core promise: **the AI never changes hardware on its own judgment.** The LLM understands the request. Deterministic code finds the devices, checks safety, asks a human when needed, runs the commands, checks the result, and reports **exactly what really changed**.

## 2. Problem

- Operators change many devices by hand. It is slow and error-prone.
- Natural language is vague: "the bedroom one", "all devices", "a bit warmer".
- A wrong bulk command ("turn off all") can hurt comfort, equipment, or safety.
- APIs fail. Some devices change, some do not. Operators need the truth, not a guess.
- In v1 a write ran immediately with no approval and no verification.

## 3. Goals

| # | Goal | How we measure it |
|---|------|-------------------|
| G1 | Handle multi-device requests in plain language | ≥ 95% correct target resolution on the eval set |
| G2 | Never execute an unsafe or unclear request silently | **0** unsafe executions on the safety eval set |
| G3 | Ask for clarification only when needed | Clarification precision ≥ 90% (not asked when the request was clear) |
| G4 | Human approval for risky changes, with a clear preview | 100% of multi-device writes show a before → after preview |
| G5 | Honest reporting under failure | Report matches the real device state in 100% of fault-injection tests |
| G6 | Full audit trail | Every request, plan, approval, command, and result is logged |
| G7 | Keep v1 strengths | Parallel reads, stats-only history, multi-provider LLM |

## 4. Non-Goals

- Fully autonomous control with no human in the loop for risky actions.
- Building our own device firmware safety (we add a layer above it, not instead of it).
- Training custom models.
- Multi-tenant billing.
- Vendor-specific device code in this repo (it lives in the MCP server project).

## 5. Users

| Persona | Needs |
|---------|-------|
| Facility operations manager | Bulk changes by room or zone, clear reports |
| HVAC maintenance engineer | Precise control, safe approvals, detail on failures |
| Safety / compliance reviewer | Audit log of who approved what |

## 6. User Stories

1. As an operator, I say "Set all living-room devices to 24°C" and see a table of devices with current → new value, then approve.
2. As an operator, I say "Turn off all devices except the bedroom one" and the system confirms what "bedroom one" means and warns me about critical devices.
3. As an operator, I say "Make it warmer" and the agent asks "Which room, and to what temperature?" instead of guessing.
4. As an operator, I ask for 35°C and the system refuses because it is out of the safe range for that device.
5. As an operator, when 1 of 5 devices fails, I see "3 changed, 1 failed, 1 unknown" with reasons, and I can retry only the failed one.
6. As a reviewer, I can see who approved a bulk shutdown, when, and the exact device list.
7. As an operator, I can reject or edit the plan (deselect a device) before it runs.
8. As an engineer, I can still ask read-only questions (status, alarms, history) in parallel, as before.

## 7. Functional Requirements

### 7.1 Carried over from v1
- FR-1 Chat endpoint, intents, markdown output.
- FR-2 Read tools: devices, parameters, telemetry, alarms, operations, history (stats-only, dynamic window).
- FR-3 Parallel orchestration for compound read requests, with `Send` fan-out and aggregator.

### 7.2 Safe Automation (new)

**Understanding**
- FR-10 Parse a write request into a structured `ActionRequest` (action, value, unit, target selector with include and exclude lists).
- FR-11 Support target forms: by room/zone/group, by device name or alias, "all", "all except X", mixed lists.
- FR-12 Support actions: set temperature, power on, power off, set mode. (Extendable by config.)

**Resolving and validating**
- FR-13 Resolve targets only against the **device registry**. The LLM never provides device IDs.
- FR-14 If a target name matches nothing, or matches more than one thing, the system asks. It never guesses or widens the scope.
- FR-15 Read current device state before planning (for preview, no-op detection, and rollback).
- FR-16 Validate each action against device capabilities and safe limits (range, allowed mode, unit conversion).
- FR-17 Skip devices that are offline, locked, or already at the target value, and say so.

**Risk and approval**
- FR-18 A deterministic **policy engine** returns one decision: `AUTO_EXECUTE`, `CONFIRM`, `CLARIFY`, or `BLOCK`.
- FR-19 Rules include: number of devices, share of fleet, "all/except" scope, critical devices, active alarms, large setpoint change, off-hours, maintenance lock.
- FR-20 For `CONFIRM`, pause with a preview (devices, before → after, warnings). Approval is bound to the plan hash and expires (default 5 min).
- FR-21 The human can approve, reject, or edit (remove devices). An edit creates a new plan hash and is re-validated.
- FR-22 For `HIGH` risk, require a stronger confirmation (for example typing the device count).
- FR-23 Limit clarification to 2 rounds, then cancel safely.

**Execution and reporting**
- FR-24 Execute with bounded concurrency, timeouts, and idempotency keys.
- FR-25 Retry only when it is safe (see `design.md`). Never blind-retry a write whose outcome is unknown.
- FR-26 Verify by reading back the device state after the write.
- FR-27 Record a per-device result: `VERIFIED`, `ACCEPTED_UNVERIFIED`, `FAILED`, `SKIPPED`, `UNKNOWN`.
- FR-28 Build the final report **from the result ledger**, in code. The LLM may add a short summary but cannot change the facts.
- FR-29 Offer follow-ups: retry failed devices, or roll back changed devices (rollback is a new plan that needs its own approval).
- FR-30 Abort policy: stop remaining batches if the failure rate passes a threshold (default 30%).

**Audit and API**
- FR-31 Audit log for every plan, decision, approval, command, and result.
- FR-32 API returns a `status` (`needs_clarification`, `awaiting_approval`, `completed`, `partial`, `failed`, `rejected`) and a structured `pending` or `report` payload.

### 7.3 Device Gateway (MCP) — new

- FR-40 All device access goes through a **separate MCP server** (Device Gateway). This repo has no vendor-specific code.
- FR-41 The app connects over Streamable HTTP with token auth, inside a private network.
- FR-42 The LLM may use only allow-listed **read** tools. The write tool (`execute_operations`) is called only by the executor.
- FR-43 Write calls include an `idempotency_key` and a signed `approval_token`. The server rejects calls without a valid token.
- FR-44 The app pins the gateway's tool schemas and refuses to run if they change without review.
- FR-45 The gateway ships a **simulator adapter** with fault injection (timeouts, 5xx, offline devices, partial success, delayed state, state drift) for local use and tests.
- FR-46 The whole stack (gateway, backend, frontend, Postgres) runs locally with one `docker compose up`.

## 8. Non-Functional Requirements

| Area | Requirement |
|------|-------------|
| Safety | No hardware write without validation. No LLM-controlled device IDs. Fail closed on any doubt. |
| Correctness | Report is generated from real results and read-back, never from LLM memory. |
| Latency | Plan + preview < 6 s for up to 20 devices. Execution of 20 devices < 20 s with concurrency 5. |
| Reliability | Circuit breaker on the MCP gateway; clear errors; no crash on partial failure. |
| Durability | Pending plans survive restarts (Postgres checkpointer). |
| Security | Auth on all routes; approver identity recorded; secrets in env. |
| Observability | Trace per node; metrics for approvals, rejects, partials, failures. |
| Portability | `LLM_PROVIDER` switch (Ollama, OpenAI, Anthropic). |

## 9. Release Plan

| Release | Scope |
|---------|-------|
| v1 (done) | Router, workers, parallel orchestration, stats history, React UI |
| **v1.1 (now)** | **Safe Automation**: device registry, structured intent, resolver, policy engine, HITL, executor, verifier, ledger report, audit, Postgres checkpointer, auth |
| v1.2 | Eval suite in CI, tracing and dashboards, streaming, LTTB option |
| v2 | Scheduled automations, proactive alarm triage, role-based access, multi-site |

## 10. Success Metrics

- Unsafe or ambiguous requests executed without approval: **0**.
- Target resolution accuracy ≥ 95%.
- Report accuracy (report vs real state) = 100% in fault-injection tests.
- Median operator time for a 10-device change drops by ≥ 70% vs manual.
- Approval rejection rate tracked (high rate means previews or prompts need work).

## 11. Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| LLM mis-parses the request | Wrong devices or value | Structured output + deterministic resolver + preview + human approval |
| Missing room/zone data in the gateway | Cannot resolve "living room" | Gateway provides base labels; app registry overlay (aliases, criticality) managed by admins |
| MCP server changes tools or is spoofed | Unsafe or broken calls | Schema pinning, allow-list, bearer auth, private network, signed approval token checked by the server |
| Approval fatigue | Users click approve blindly | Auto-run only truly low risk; clear previews; stronger confirm for HIGH |
| Timeouts with unknown outcome | Wrong report | Read-back verification; `UNKNOWN` status; no blind retry |
| Prompt injection via device names or alarm text | Unsafe commands | Tool output is data; LLM cannot call write tools; policy engine decides |
| Stale preview | Approved plan no longer true | Plan hash + TTL + re-validation before execute |

## 12. Open Questions

1. Where do room and zone labels come from? (Gateway device metadata, or only our registry overlay.)
2. Which devices or operations are "critical" (for example freeze protection, circulation pumps)?
3. Safe temperature limits per device type and per site?
4. Should multi-device changes always need approval, or only above a threshold?
5. Who is allowed to approve? Any operator, or a supervisor for HIGH risk?
6. Is rollback needed for all actions, or only for temperature and power?
7. Which real vendor adapter comes after the simulator?
8. Should the MCP server be reusable by other agents or teams? (affects auth and tool naming)
