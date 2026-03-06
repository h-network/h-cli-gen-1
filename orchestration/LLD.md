# Orchestration — Low-Level Design

## Overview

The orchestration module is the task tracker and dispatcher of h-cli. It routes messages between interfaces (`interfaces/`) and the LLM worker (`llm/claude-code/`) via Redis, manages task lifecycle with a full state machine, and handles session continuity.

Split into three files: `bus.py` (Redis task lifecycle), `worker.py` (Claude invocation), `dispatcher.py` (concurrent dispatch loop).

## Module Structure

```
orchestration/          ← this module
  bus.py                ← Redis task lifecycle, state machine, HMAC, key constants, metrics, crash recovery
  worker.py             ← Claude invocation, prompt building, skills, sessions, idle sweep
  dispatcher.py         ← Concurrent BLPOP loop, thread pool, per-chat serialization, signal handling
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

### dispatcher.py — Concurrent Main Loop

Entry point with thread pool for parallel task execution.

**Concurrency model:**
- `ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)` runs tasks in parallel
- `threading.Semaphore(MAX_CONCURRENT_TASKS)` gates the BLPOP loop — won't pop a task unless a slot is free
- Per-chat serialization via `threading.Lock` per `chat_id` — tasks from the same chat run sequentially to protect session history
- `MAX_CONCURRENT_TASKS=1` behaves identically to the original serial dispatcher

**Flow:**
1. Create TaskBus, connect, recover orphaned tasks, create thread pool
2. Acquire semaphore (blocks if all slots busy)
3. BLPOP `hcli:tasks` (30s timeout)
4. On timeout: release semaphore, sweep idle sessions, loop
5. On task: validate → submit `_handle_task()` to pool → loop immediately
6. `_handle_task()` (in worker thread): acquire per-chat lock → set state `running` → subscribe control → `worker.run_task()` → determine final state → store result → write metrics → store memory → unsubscribe control → release chat lock → release semaphore
7. On SIGTERM: `executor.shutdown(wait=True)` — finishes all in-flight tasks before exit

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

`bus.subscribe_control(task_id, ...)` starts a daemon thread per task that subscribes to `hcli:control:{task_id}`. On `{"action": "abort"}` message:
1. Sets `abort_event` (threading.Event)
2. Kills the Claude process group via `os.killpg(proc.pid, SIGKILL)`
3. Thread exits

The worker's `proc.communicate()` returns (process died), checks `abort_event`, returns abort result. Dispatcher sets state to `aborted`.

Control threads are tracked in `TaskBus._control_threads` dict keyed by `task_id`, protected by a lock. `unsubscribe_control(task_id)` cleanly shuts down the specific listener within ~1 second.

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

## Concurrency

### Thread Pool

The dispatcher uses `ThreadPoolExecutor` with `MAX_CONCURRENT_TASKS` workers. A `threading.Semaphore` gates the BLPOP loop: the main thread only pops a task when a slot is free, preventing unbounded memory growth.

### Per-Chat Serialization

Tasks from the same `chat_id` are serialized via per-chat `threading.Lock`s. This prevents:
- Interleaved `RPUSH` to session history (corrupted turn ordering)
- Concurrent `manage_session()` triggering double chunk dumps
- Race conditions on session size tracking

Tasks from different chat_ids run fully in parallel.

### Thread-Safety Guarantees

| Component | Thread-safe? | Mechanism |
|-----------|-------------|-----------|
| `bus.r` (Redis) | Yes | `redis-py` internal connection pool |
| `bus.set_state()` | Yes | Per-key Redis SET, no shared state |
| `bus.write_metrics()` | Yes | Independent pipeline per call; HINCRBY is atomic |
| `bus.subscribe_control()` | Yes | Per-task dict with lock |
| `bus.store_result()` | Yes | Per-key Redis SET |
| `bus._get_pg_pool()` | Yes | Double-checked lock; `ThreadedConnectionPool` |
| `worker.run_task()` | Yes | All state is local; subprocess is independent |
| `worker.sweep_idle_sessions()` | Main thread only | Called on BLPOP timeout, never from pool |

### Graceful Shutdown

SIGTERM sets `_shutdown` flag. Main loop stops popping tasks. `executor.shutdown(wait=True)` blocks until all in-flight tasks complete. Upper bound: `TASK_TIMEOUT` (each task has its own timeout).

## Conversation Auto-Indexing

`maintenance.sh` (repo root) extracts completed conversations from the audit log and writes them to Qdrant's collection directory for auto-indexing.

### Pipeline

```
logs/claude/audit.log (JSONL)
  → maintenance.sh (correlate task_started + task_completed by task_id)
  → data/collections/conversations/conversations_YYYY-MM-DD.jsonl (Q&A pairs, per-date files)
  → Core auto-indexes on restart (MiniLM embedding → Qdrant)
```

### How it works

1. Reads `logs/claude/audit.log` from last processed byte offset
2. Parses JSONL: correlates `task_started` entries (has `user_message`) with `task_completed` entries (has `output`) by `task_id`
3. Filters: skips errors/aborts/timeouts, skips short answers (< 50 chars)
4. Appends Q&A pairs to per-date files (`conversations_YYYY-MM-DD.jsonl`)
5. Saves byte offset to `.last_offset` marker file

### Idempotency

- Tracks byte offset in `data/collections/conversations/.last_offset`
- Handles log rotation: if file size < saved offset, resets to 0
- Safe to run on cron — only processes new entries

### Output format

```json
{"question": "show me the BGP status on router-01", "answer": "Here are the BGP neighbors...", "source": "conversation:2026-03-06"}
```

Core loads `data/collections/conversations/*.jsonl`, embeds the `question` field with MiniLM, and stores in Qdrant. The `memory_search` MCP tool can then retrieve relevant past conversations.

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `REDIS_URL` | `redis://redis:6379` | Redis connection |
| `MAX_CONCURRENT_TASKS` | `3` | Thread pool size and semaphore limit |
| `TASK_TIMEOUT` | `600` | Max seconds per task |
| `SESSION_TTL` | `28800` (8h) | Redis session key TTL |
| `HISTORY_TTL` | `86400` (24h) | Conversation history TTL |
| `IDLE_DUMP_SECONDS` | `1800` (30min) | Idle session dump threshold |
| `RESULT_HMAC_KEY` | (required) | HMAC signing key |
| `TIMESCALE_URL` | (optional) | PostgreSQL connection for metrics |
| `MAIN_MODEL` | `opus` | Model ID for opus/sonnet tasks |
| `FAST_MODEL` | `haiku` | Model ID for haiku tasks |
| `CHAT_NAMES` | (optional) | `chat_id:name` mapping for log directories |
