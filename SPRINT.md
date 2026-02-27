# Sprint — Bug Fixes & Code Cleanup

**Goal**: Fix broken abort, replace wasteful result polling, split oversized dispatcher, add missing reliability (state tracking, crash recovery), remove dead code.
**Context**: Read `HLD.md` first — all architectural decisions are there. Discussion history in `docs/decisions/` if you need the reasoning.
**Test environment**: h-srv, directory `~/h-cli-new/`, stack `h-cli-dev-*`

---

## Phase 1 — Orchestration: Dispatcher Split + Task Lifecycle

**Team**: orchestration
**Branch prefix**: `orchestration/`
**Files**: `orchestration/dispatcher.py` → split into `orchestration/bus.py`, `orchestration/worker.py`, `orchestration/dispatcher.py`

### Tasks

- [x] **1.1** Extract Redis operations from dispatcher.py into `bus.py`
  - Key constants (single source of truth)
  - Task state machine (SET `hcli:task:{id}:state` on every transition)
  - State notification (PUBLISH `hcli:task:{id}:notify` after every SET)
  - Control channel subscription (SUBSCRIBE `hcli:control:{id}` per task)
  - HMAC result signing
  - Stats/metrics Redis writes
  - Crash recovery: on startup SCAN for orphaned `running` states → set `failed`

- [x] **1.2** Extract Claude invocation into `worker.py`
  - System prompt building (ground rules + context + skills)
  - Skill loading and keyword matching
  - Session context building (Redis history + disk chunks)
  - Claude process spawning and output parsing
  - Session history recording and chunking
  - Idle session sweep

- [x] **1.3** Thin out `dispatcher.py` to main loop only
  - BLPOP loop
  - Hand task to worker, report state via bus
  - Signal handling (SIGTERM)
  - Docker healthcheck heartbeat

- [x] **1.4** Implement abort via control channel
  - On task start: subscribe to `hcli:control:{task_id}` in a thread
  - On abort message: `os.killpg(proc.pid, SIGKILL)`, set state=aborted, write result
  - On task end: unsubscribe
  - Make Popen handle accessible to control thread (thread-safe shared reference)

- [x] **1.5** Validate task JSON schema on pickup
  - Required: task_id (UUID), message, chat_id, user_id, submitted_at (ISO-8601)
  - Optional: model (default opus)
  - Reject malformed tasks: state=failed, write error result

---

## Phase 2 — LLM: Dockerfile Update

**Team**: llm
**Branch prefix**: `llm/`
**Files**: `llm/claude-code/Dockerfile`

### Tasks

- [x] **2.1** Update Dockerfile COPY lines
  - Add `COPY orchestration/bus.py .`
  - Add `COPY orchestration/worker.py .`
  - Keep `COPY orchestration/dispatcher.py .`
  - Add any new pip dependencies from orchestration

- [x] **2.2** Verify entrypoint
  - Confirm CMD stays `python3 -u dispatcher.py`
  - Test container builds and starts correctly

---

## Phase 3 — Interface: Subscribe Model + Abort Fix

**Team**: interface
**Branch prefix**: `interface/`
**Files**: `interfaces/telegram-bot/bot.py`

### Tasks

- [x] **3.1** Fix /abort — switch to control channel
  - Replace `SET hcli:abort:{task_id}` with `PUBLISH hcli:control:{task_id} {"action": "abort"}`
  - Remove self-signed fake result (dispatcher writes real result after kill)
  - Dual-pattern migration shim added then removed in Phase 4

- [x] **3.2** Replace result polling with subscribe model
  - Subscribe to `hcli:task:{id}:notify` channel
  - Immediately do one GET check (race condition: result arrived before subscribe)
  - Wait for PUBLISH or timeout
  - Keep slow-poll GET every 10s as safety net
  - On notification: GET result, verify HMAC, deliver

- [x] **3.3** Update task JSON schema
  - `submitted_at` (ISO-8601) was already present in task payload
  - All required fields confirmed: task_id, message, chat_id, user_id, submitted_at

- [x] **3.4** Rename pending key
  - `hcli:pending:{chat_id}` → `hcli:chat:{chat_id}:tasks`
  - Updated all references in bot.py

- [x] **3.5** Handle audit stream gaps
  - Don't wait forever for completion events after abort
  - Use task state as source of truth, audit events as supplementary

---

## Phase 4 — Infrastructure: Cleanup + Test

**Team**: architect (coordinates across teams)
**Files**: `docker-compose.yml`, `HLD.md`

### Tasks

- [x] **4.1** Remove dead keys and old code
  - Removed `hcli:abort:{id}` pattern (dual-pattern shim) from bot.py
  - `hcli:pending:{id}` was already replaced by `hcli:chat:{id}:tasks` in Phase 3
  - Old result polling loop was already replaced by subscribe model in Phase 3

- [x] **4.2** Update HLD
  - Fixed HMAC format in key contracts table
  - Removed dead keys section (cleanup done)
  - Task lifecycle, Redis namespace, network topology already up to date

- [x] **4.3** Test on h-srv (2026-02-26)
  - Deployed to `h-cli-dev-*` stack on h-srv (`~/h-cli-new/`)
  - PASS: send message → state=running → state=completed, output="4", HMAC signed
  - PASS: /abort → control channel → state=aborted, output="Task aborted by user"
  - PASS: /cancel → LREM removes task from queue before dispatcher pickup
  - PASS: malformed task (no message) → state=failed, output="Error: missing 'message' field"
  - PASS: dispatcher restart → orphaned running → state=failed, proper error result
  - NOTE: core healthcheck shows unhealthy (pre-existing, curl vs SSE mismatch — MCP functional)

---

## Execution Order

Phases 1-3 can run in parallel (different teams, different files). Phase 4 runs after all three are merged.

```
Phase 1 (orchestration) ──┐
Phase 2 (llm)            ──┼── Phase 4 (infrastructure + test)
Phase 3 (interface)      ──┘
```

## Dependencies

- Phase 2 blocked on Phase 1 (need final filenames)
- Phase 3 needs Phase 1 contracts (notify channel name, control channel schema)
- Phase 4 needs all three merged to main

## Key Rules

- All teams: read `docs/decisions/architect-report-redis.md` for decisions, `docs/decisions/discussion.md` for context
- All teams: REPLY.md in your directory, push before signaling done
- All teams: `git diff --stat` before every commit — stay in scope
- All teams: read TeamTracker.md — violations are logged permanently
