# Orchestration — Low-Level Design

## Overview

The orchestration module is the task tracker and dispatcher of h-cli. It routes messages between interfaces (`interfaces/`) and the LLM worker (`llm/claude-code/`) via Redis, manages task lifecycle with a full state machine, and handles session continuity.

Split into three files: `bus.py` (Redis task lifecycle), `worker.py` (Claude invocation), `dispatcher.py` (thin main loop).

## Module Structure

```
orchestration/          ← this module
  bus.py                ← Redis task lifecycle, state machine, HMAC, key constants, metrics, crash recovery
  worker.py             ← Claude invocation, prompt building, skills, sessions, idle sweep
  dispatcher.py         ← Thin BLPOP loop, signal handling, heartbeat
  LLD.md                ← this file
```

All three files run inside the `claude-code` container. The Dockerfile at `llm/claude-code/Dockerfile` copies them into `/app/`. Orchestration owns the code, LLM owns the container packaging.

## Components

### bus.py — Redis Task Lifecycle

Single source of truth for all Redis key constants and task state management.

**TaskBus class provides:**
- `connect()` / `reconnect()` — Redis connection management (main + separate pub/sub connection)
- `blpop_task()` — block-pop from task queue
- `validate_task()` — JSON schema validation per HLD section 9
- `set_state()` — SET `hcli:task:{id}:state` + PUBLISH `hcli:task:{id}:notify` on every transition
- `store_result()` — HMAC-sign and store result
- `subscribe_control()` / `unsubscribe_control()` — control channel listener thread for abort
- `write_metrics()` — Redis counters + TimescaleDB
- `store_memory()` — raw conversation storage
- `recover_orphaned_tasks()` — on startup, SCAN for `running` states → set `failed`

### worker.py — Claude Invocation

Everything related to executing a task.

**Key functions:**
- `run_task(r, task, abort_event, proc_ref)` — full task execution: session management, context building, Claude spawn, output parsing. Returns `(output, metrics)`.
- `build_system_prompt(chat_id, message)` — loads ground rules + context + disk chunks + skill rules
- `manage_session(r, chat_id)` — handles session expiry recovery and size-based chunking
- `record_history(r, chat_id, message, output)` — writes turns to Redis history
- `sweep_idle_sessions(r)` — dumps sessions idle > IDLE_DUMP_SECONDS

**Abort support:** `run_task` receives `abort_event` (threading.Event) and `proc_ref` (mutable list). Sets `proc_ref[0]` to the Popen handle after spawning Claude, so the bus control thread can kill it on abort. Checks `abort_event` after `proc.communicate()` returns.

### dispatcher.py — Main Loop

Thin entry point (~100 lines). Connects bus and worker.

**Flow:**
1. Create TaskBus, connect, recover orphaned tasks
2. BLPOP `hcli:tasks` (30s timeout)
3. On timeout: sweep idle sessions, loop
4. On task: validate → set state `running` → subscribe control channel → `worker.run_task()` → determine final state → store result → write metrics → store memory → unsubscribe control → loop
5. On SIGTERM: finish current task, exit

## Container Path Mapping

| Container path | Repo source | How it gets there |
|---------------|-------------|-------------------|
| `/app/dispatcher.py` | `orchestration/dispatcher.py` | Dockerfile COPY |
| `/app/bus.py` | `orchestration/bus.py` | Dockerfile COPY |
| `/app/worker.py` | `orchestration/worker.py` | Dockerfile COPY |
| `/app/firewall.py` | `llm/claude-code/firewall.py` | Dockerfile COPY |
| `/app/mcp-config.json` | `llm/claude-code/mcp-config.json` | Dockerfile COPY |
| `/app/CLAUDE.md` | `llm/claude-code/CLAUDE.md` | Dockerfile COPY |
| `/app/groundRules.md` | `llm/groundRules.md` | Dockerfile COPY |
| `/app/blocked-patterns.txt` | `llm/blocked-patterns.txt` | docker-compose volume |
| `/app/skills/` | `skills/` | docker-compose volume |
| `/var/log/hcli/sessions/` | `logs/sessions/` | docker-compose volume |

## Task Lifecycle

```
[*] → queued → running → completed
                      → failed
                      → timed_out
                      → aborted
    → cancelled
```

Every state transition: `SET hcli:task:{id}:state` then `PUBLISH hcli:task:{id}:notify`.

| State | Who sets it | What happens |
|-------|------------|--------------|
| `queued` | Interface | Task pushed to `hcli:tasks`, state key created |
| `running` | bus.py | BLPOP picks it up, state updated |
| `completed` | bus.py | Result written (HMAC-signed), state updated |
| `failed` | bus.py | Error written, state updated |
| `timed_out` | bus.py | Process killed by timeout, state updated |
| `aborted` | bus.py | Control channel message → process killed → state updated |
| `cancelled` | Interface | Task removed from queue (LREM) before pickup |

### Crash Recovery

On startup, `bus.recover_orphaned_tasks()` SCANs for `hcli:task:*:state` keys with value `running` and transitions them to `failed` with an error result. This handles dispatcher crashes mid-task.

### Control Channel (abort)

`bus.subscribe_control()` starts a daemon thread that subscribes to `hcli:control:{task_id}`. On `{"action": "abort"}` message:
1. Sets `abort_event` (threading.Event)
2. Kills the Claude process group via `os.killpg(proc.pid, SIGKILL)`
3. Thread exits

The worker's `proc.communicate()` returns (process died), checks `abort_event`, returns abort result. Dispatcher sets state to `aborted`.

Uses `pubsub.get_message(timeout=1.0)` in a loop with a stop event, so `unsubscribe_control()` can cleanly shut down the thread within ~1 second.

## Redis Key Namespace

All key constants defined in `bus.py` (single source of truth).

| Pattern | Type | TTL | Owner |
|---------|------|-----|-------|
| `hcli:tasks` | List | none | Interface writes, Orchestration reads |
| `hcli:task:{id}:state` | String | 1h | Orchestration writes, Interface reads |
| `hcli:task:{id}:notify` | Channel | n/a | Orchestration publishes, Interface subscribes |
| `hcli:control:{id}` | Channel | n/a | Interface publishes, Orchestration subscribes |
| `hcli:audit:{id}` | Channel | n/a | Core publishes, Interface subscribes |
| `hcli:results:{id}` | String | 1h | Orchestration writes, Interface reads |
| `hcli:session:{chat}` | String | SESSION_TTL | Orchestration |
| `hcli:session_size:{chat}` | String | HISTORY_TTL | Both |
| `hcli:session_history:{chat}` | List | HISTORY_TTL | Both |
| `hcli:memory:{id}:{role}` | String | SESSION_TTL | Orchestration |
| `hcli:stats:{date}` | Hash | 48h | Both |
| `hcli:last_activity:{chat}` | String | HISTORY_TTL | Orchestration |

## Session Continuity

Three-tier memory, managed by worker.py:

1. **Redis history** (< 24h) — `hcli:session_history:{chat_id}`, prepended to each message as formatted text
2. **Disk chunks** (> 24h) — when session exceeds 100KB, dumped to `/var/log/hcli/sessions/{chat_id}/chunk_*.txt`, up to 50KB injected into system prompt
3. **Vector memory** (permanent, optional) — Qdrant, queried via `memory_search` MCP tool

Session rotation triggers:
- Accumulated size > 100KB (`MAX_SESSION_BYTES`)
- Idle > 30min (`IDLE_DUMP_SECONDS`) — swept on BLPOP timeout
- Session TTL expiry — dump before starting fresh

## HMAC Result Signing

Results are HMAC-SHA256 signed by `bus.store_result()`:

```
message = "{task_id}:{output}:{completed_at}"
signature = HMAC-SHA256(RESULT_HMAC_KEY, message)
```

Interface verifies signature before delivering result to user.

## Skill Injection

Worker loads skills from `/app/skills/{public,private}/` (mounted from repo `skills/`):
- Parse YAML header for keywords and rules
- Keyword match against user message
- Inject matched rules into system prompt (20KB budget)
- Always append full skill index so the agent can `cat` any skill on demand

## Metrics

Dual write on task completion via `bus.write_metrics()`:
- **Redis** `hcli:stats:{date}` — hash with counters (tasks, tokens, cost, errors) for `/stats` command
- **TimescaleDB** `task_metrics` table — full row per task for Grafana dashboards

## Task JSON Schema

Per HLD section 9:

```json
{
  "task_id": "string (UUID, required)",
  "message": "string (required, accepts legacy 'command' fallback)",
  "chat_id": "int (required)",
  "user_id": "int (required)",
  "submitted_at": "string (ISO-8601, added by interface — not yet enforced)",
  "model": "string (optional, default 'opus', enum: opus/sonnet/haiku)"
}
```

Validation in `bus.validate_task()`: rejects missing task_id or message (sets state=failed). Warns on missing chat_id/user_id but allows through for backward compatibility.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `TASK_TIMEOUT` | `600` | Max seconds per task |
| `SESSION_TTL` | `28800` (8h) | Redis session key TTL |
| `HISTORY_TTL` | `86400` (24h) | Conversation history TTL |
| `IDLE_DUMP_SECONDS` | `1800` (30min) | Idle session dump threshold |
| `RESULT_HMAC_KEY` | (required) | HMAC signing key |
| `TIMESCALE_URL` | (optional) | PostgreSQL connection for metrics |
| `MAIN_MODEL` | `opus` | Model ID for opus/sonnet tasks |
| `FAST_MODEL` | `haiku` | Model ID for haiku tasks |
| `CHAT_NAMES` | (optional) | `chat_id:name` mapping for log directories |
