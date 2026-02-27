# Architect Report — Architecture Review & Sprint Scope

**Date**: 2026-02-26
**Discussion participants**: Orchestration, Interface, LLM, Core
**Rounds**: 3 (Redis nervous system → MCP container question → MCP over Redis)
**Verdict**: Architecture is sound — no redesign needed. Sprint focuses on fixing bugs, replacing wasteful patterns, and cleaning up code.

---

## Final Architecture

**Redis is the task tracker** — not the nervous system for everything. It tracks task lifecycle, delivers results, manages sessions, and carries audit events. It does NOT carry MCP tool calls.

**MCP stays standard and direct** — firewall.py talks to Core over SSE/MCP as it does today. No custom protocols, no Redis transport for tool calls. MCP ecosystem compatibility preserved.

**Two-network topology preserved** — the frontend/backend split stays per security hardening (items 3, F52). Network topology unchanged from current docker-compose.yml.

```
Frontend:  Bot ◄──► Redis ◄──► Claude/Dispatcher, Core, Grafana, CVE
Backend:   Claude/Dispatcher ──MCP──► Core, TimescaleDB, Qdrant, Grafana (:3000)
```

**What goes through Redis**: task queue, task state, control channels (abort), result delivery, state notifications, audit events, sessions, stats.

**What goes direct**: MCP tool calls (firewall → Core SSE), memory search (memory proxy → Core SSE).

---

## Architectural Decisions

### AD-1: State notification — SET + PUBLISH

Dispatcher does `SET hcli:task:{id}:state` AND `PUBLISH hcli:task:{id}:notify` on every state transition. Bot subscribes to notify channel with GET fallback + slow poll every 10s.

**Rule**: SET result with HMAC BEFORE publishing notify.

### AD-2: Task JSON schema

```json
{
  "task_id": "string (UUID, required)",
  "message": "string (required)",
  "chat_id": "int (required)",
  "user_id": "int (required)",
  "submitted_at": "string (ISO-8601, required)",
  "model": "string (optional, default 'opus', enum: opus/sonnet/haiku)"
}
```

Malformed tasks rejected with state=failed immediately.

### AD-3: Pending key — Keep, rename

`hcli:pending:{chat_id}` → `hcli:chat:{chat_id}:tasks`. Bot-owned, bot-managed. TTL = 2x TASK_TIMEOUT.

### AD-4: Abort vs Cancel

- **Cancel** = remove from queue before pickup. Bot does LREM + sets state=cancelled.
- **Abort** = kill running process. Bot publishes to control channel, dispatcher kills process, sets state=aborted.

### AD-5: Dispatcher split

- `orchestration/bus.py` — Redis task tracker (keys, state machine, HMAC, notifications, crash recovery)
- `orchestration/worker.py` — Task execution (Claude, skills, sessions)
- `orchestration/dispatcher.py` — Thin main loop

Boundary: bus knows Redis, worker knows Claude, dispatcher connects them.

### AD-6: /clear and /teach — Direct writes, deferred

Bot keeps direct Redis writes. Add PUBLISH notification later if needed.

### AD-7: Crash recovery — Startup scan

On boot, SCAN for orphaned `running` states, transition to `failed`.

### AD-8: Audit events — Fire-and-forget

Missing events are normal. Bot uses task state as source of truth, audit as supplementary. Core never publishes "aborted" events.

### AD-9: task_id passthrough — Documented debt

Smuggled through MCP tool parameter. Works, degrades gracefully. Preserve on any proxy refactor.

### AD-10: Single container

Dispatcher split is code-level, not container-level. MCP requires co-location (stdio). Orchestration owns the Python modules, LLM owns the container packaging.

### AD-11: Two Docker networks (kept)

~~Originally proposed merging to one network.~~ **Cancelled** — conflicts with security hardening items 3 and F52. The two-network split (frontend/backend) prevents telegram-bot from reaching Core:8083 directly and prevents Core from being reachable without going through the firewall. Network topology unchanged.

### AD-12: MCP stays standard

MCP-over-Redis was evaluated and rejected. Wins (one transport, no ports) didn't justify the costs (custom protocol, lost MCP compatibility, added complexity). The only direct service-to-service connection is firewall→Core MCP, and it works. Keep it.

---

## Contracts Summary

| From | To | Contract |
|------|----|----------|
| Orchestration | Interface | PUBLISH to `hcli:task:{id}:notify` on every state transition. SET result+HMAC BEFORE notify. |
| Orchestration | LLM | Final filenames (bus.py, worker.py, dispatcher.py). Confirm CMD stays `python3 -u dispatcher.py`. Any new pip deps. |
| Orchestration | All | Crash recovery on startup. Audit gaps are normal. |
| Interface | Orchestration | Task JSON schema compliance. Stop setting abort key, use control channel. |
| Interface | Core | Handle audit stream gaps gracefully. |
| LLM/Firewall | Core | Keep passing task_id through proxy. |
| Core | All | Audit is fire-and-forget. No aborted events from Core. |

---

## What Changes

| Component | Change |
|-----------|--------|
| docker-compose.yml | No network changes (two-network topology preserved) |
| orchestration/dispatcher.py | Split into bus.py + worker.py + dispatcher.py |
| orchestration/bus.py | Task lifecycle state machine, control channels, HMAC, crash recovery |
| orchestration/worker.py | Claude invocation, skills, sessions (extracted from dispatcher) |
| interfaces/telegram-bot/bot.py | Subscribe model replaces polling, /abort via control channel, schema update |
| llm/claude-code/Dockerfile | COPY bus.py + worker.py alongside dispatcher.py |
| Dead keys | Remove hcli:abort:{id}, hcli:pending:{id} |

## What Does NOT Change

- MCP architecture (firewall → Core over SSE, standard protocol)
- Core code (mcp_server.py, memory_server.py unchanged)
- Grafana + TimescaleDB
- Skill system
- Session chunking to disk
- Core's audit publishing pattern

---

## Migration Order

**Phase 1 — Orchestration**: Split dispatcher.py. Add task lifecycle, control channel, crash recovery.

**Phase 2 — LLM**: Update Dockerfile to COPY new files.

**Phase 3 — Interface**: Switch /abort to control channel. Subscribe model. Schema update. Rename pending key.

**Phase 4 — Infrastructure**: Remove dead keys. Remove old polling code. Update HLD. Test on h-srv.

Each phase independently deployable. Phase 3 needs dual-pattern support during transition (old + new paths active).

---

## Open Questions for Operator

1. **Timeline**: All 4 phases in one sprint or phased rollout?
2. **Remaining teams**: Should monitor/hssh/knowledge/security review, or proceed with the 4 core teams?
3. **Testing**: Dev instance first or direct to prod?
