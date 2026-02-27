# Low-Level Design — telegram-bot

## 1. Overview

Single-file async Python service (`bot.py`) that acts as the user-facing front door to h-cli. It receives messages via the Telegram Bot API (long-polling), queues tasks to Redis, polls for results from the orchestration layer, and renders responses back to the user. It is stateless itself — all persistent state lives in Redis.

## 2. Position in System Flow

telegram-bot owns two steps of the Architect's 9-step message lifecycle (see root `LLD.md` §3):

- **Step 1 — Ingest**: Receive Telegram update, authenticate, build task JSON, `RPUSH` to Redis, spawn poller.
- **Step 9 — Delivery**: Pick up signed result from Redis, verify HMAC, convert markdown to Telegram HTML, split at 4096 chars, send to user.

Everything between steps 1 and 9 (dispatch, context injection, Claude invocation, firewall, core execution, result signing) is opaque to this module — we only see the Redis queue contracts.

```
         telegram-bot                 opaque to us                   telegram-bot
        ┌───────────┐    ┌─────────────────────────────────┐    ┌───────────────┐
Step 1  │ auth      │    │ Steps 2–8                       │    │ HMAC verify   │ Step 9
User ──►│ task JSON │──►Redis──► dispatcher ──► ... ──►Redis──►│ HTML render   │──► User
        │ RPUSH     │    │                                 │    │ send_long()   │
        └───────────┘    └─────────────────────────────────┘    └───────────────┘
```

### 2.1 What We Produce

| Artifact | Consumer | Format | Redis Key / Path |
|---|---|---|---|
| Task JSON | Orchestration (dispatcher) | JSON string with `task_id`, `message`, `user_id`, `chat_id`, `submitted_at`, `model` | `hcli:tasks` (RPUSH) |
| Pending task tracking | Self (for `/cancel`) | task_id string | `hcli:pending:{chat_id}` (RPUSH) |
| Session chunk files | Orchestration (Tier 2 context injection) | Plain text — header + timestamped turns | `/var/log/hcli/sessions/{chat_name}/chunk_{timestamp}.txt` |
| Teach mode turns | Self (for skill generation prompt) | JSON strings in Redis list | `hcli:teach:{chat_id}:turns` |

### 2.2 What We Consume

| Artifact | Producer | Format | Redis Key |
|---|---|---|---|
| Signed result | Orchestration (dispatcher) | JSON with `output`, `completed_at`, `usage`, `hmac` | `hcli:results:{task_id}` (GET, then DELETE) |
| Session UUID | Orchestration (dispatcher) | UUID string | `hcli:session:{chat_id}` (cleared by `/new`) |
| Session history | Orchestration (dispatcher) | JSON turn objects in list | `hcli:session_history:{chat_id}` (read + delete on `/new`) |
| Session byte counter | Orchestration (dispatcher) | Integer string | `hcli:session_size:{chat_id}` (cleared by `/new`) |
| Daily stats | Orchestration (dispatcher) | Hash with counters | `hcli:stats:{YYYY-MM-DD}` (read-only) |
| Audit events | Orchestration (dispatcher) | JSON with `command`, `status`, `duration_s` | `hcli:audit:{task_id}` (SUBSCRIBE) |

### 2.3 Session Chunking Handoff

Both telegram-bot and Orchestration independently write chunk files to the same shared volume. This is the only cross-module contract that uses the filesystem instead of Redis.

```
telegram-bot (_dump_session_chunk)              Orchestration (dump_session_chunk)
         │                                              │
         │  /new command only                           │  session expiry or size > 100KB
         ▼                                              ▼
  LRANGE hcli:session_history:{chat_id}          LRANGE hcli:session_history:{chat_id}
         │                                              │
         ▼                                              ▼
  Write chunk file                               Write chunk file
  (same path, same format)                       (same path, same format)
         │                                              │
         ▼                                              ▼
  DELETE session keys from Redis                 DELETE session keys from Redis
         │                                              │
         └────────── shared volume ─────────────────────┤
                                                        │
                                                        ▼
                                          Reads up to 50KB of recent
                                          chunks into system prompt
                                          (Tier 2 context injection)
```

**Note**: Two independent implementations exist — async in `bot.py` (L438–473), sync in `orchestration/dispatcher.py` (L308–348). Same file format, same Redis cleanup. This is a refactoring candidate for a shared utility, but works correctly as-is.

**Chunk file format** (written by `_dump_session_chunk`, L440–473):

```
=== h-cli session chunk ===
Chat: {chat_id}
Session: /new
Chunked: {YYYYMMDDTHHMMSSz}
Turns: {count}
===

[2026-02-19 14:30:00 UTC] USER:
{message content}

---

[2026-02-19 14:30:05 UTC] ASSISTANT:
{response content}

---
```

The shared volume is mounted at `SESSION_CHUNK_DIR` (default `/var/log/hcli/sessions`) in both containers via `docker-compose.yml`.

## 3. File Responsibilities

```
interfaces/telegram-bot/
├── bot.py              Main application — all bot logic in one module
├── Dockerfile          Container image (python:3.12-slim, non-root user)
├── entrypoint.sh       Creates log dirs, then exec's CMD
└── requirements.txt    Pinned deps: python-telegram-bot, redis, httpx
```

### bot.py — Internal Sections

| Section (line range) | Purpose |
|---|---|
| Config (30–79) | Env var loading, constants, Redis key prefixes |
| Action system (81–140) | Regex-based `[action:type:payload]` extraction and dispatch |
| Model toggle (142–150) | Per-chat model preference (haiku/opus) + keyboard layout |
| HMAC verification (153–160) | Verify result integrity via SHA-256 HMAC |
| Helpers (163–295) | `authorized()`, `markdown_to_telegram_html()`, `send_long()`, `_redis()` |
| Auth wrapper (298–311) | `@auth_required` decorator — fail-closed allowlist check |
| Command handlers (314–584) | `/start`, `/help`, `/status`, `/stats`, `/skills`, `/new`, `/cancel`, `/abort`, `/run` |
| Task queueing (537–576) | `_queue_task()` — concurrency check, Redis RPUSH, spawn poller |
| Result polling (588–658) | `_poll_result()` — async loop, HMAC verify, teach buffering, send response |
| Keyboard handler (662–761) | Model toggle, stats, skills, teach mode start/end, verbose toggle |
| Message handler (764–770) | Catch-all for natural language — forwards to `_queue_task()` |
| Photo handler (773–803) | Download photo, base64 encode, save to disk, queue with data URI |
| App lifecycle (806–828) | `post_init` (Redis pool), `post_shutdown` (pool cleanup) |
| Main (831–861) | Handler registration, `run_polling()` |

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    telegram-bot container                │
│                                                         │
│  Telegram API ──(long poll)──▸ python-telegram-bot      │
│                                      │                  │
│                     ┌────────────────┼──────────────┐   │
│                     │                │              │   │
│                CommandHandler  KeyboardHandler  MessageHandler
│                     │                │              │   │
│                     └────────────────┼──────────────┘   │
│                                      ▼                  │
│                               _queue_task()             │
│                                  │    │                 │
│                         RPUSH    │    │  asyncio.Task   │
│                                  ▼    ▼                 │
│                              Redis   _poll_result()     │
│                                         │               │
│                                GET loop │               │
│                                         ▼               │
│                                    send_long()          │
│                                      │    │             │
│                              HTML msg│    │actions      │
│                                      ▼    ▼             │
│                              Telegram  Grafana (httpx)  │
└─────────────────────────────────────────────────────────┘
```

## 5. Data Flow

### 5.1 Normal Message Flow (Step 1 → Step 9)

```
User ──▸ Telegram API ──▸ handle_message()
                              │
                   Step 1     ▼
                         _queue_task()
                              │
                     ┌────────┴────────┐
                     │                 │
              RPUSH hcli:tasks    RPUSH hcli:pending:{chat_id}
              (task JSON)         (task_id for cancel tracking)
                     │
                     ▼
              asyncio.Task(_poll_result)
                     │
              ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
              Steps 2–8: opaque to us
              (dispatch, context, Claude,
               firewall, core, signing)
              ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─
                     │
              GET hcli:results:{task_id}  (1s interval, up to TASK_TIMEOUT)
                     │
                     ▼ (result found)
              HMAC verify ──▸ extract usage stats ──▸ buffer teach turns (if active)
                     │
                     ▼
   Step 9     send_long() ──▸ strip actions ──▸ markdown_to_telegram_html()
                     │                              │
                     ▼                              ▼
              split at 4096 chars             execute action handlers
              send as HTML chunks             (e.g. Grafana graph fetch)
```

### 5.2 Task JSON (pushed to Redis — Step 1)

```json
{
  "task_id":      "uuid4",
  "message":      "user text or /run command",
  "user_id":      12345,
  "chat_id":      67890,
  "submitted_at": "ISO-8601",
  "model":        "opus | haiku"
}
```

### 5.3 Result JSON (read from Redis — Step 9)

```json
{
  "output":       "response text, may contain [action:graph:url] markers",
  "completed_at": "ISO-8601",
  "usage":        { "model", "input_tokens", "output_tokens", "cost_usd", "duration_ms" },
  "hmac":         "sha256 hex digest"
}
```

### 5.4 Session Chunking (`/new`)

```
cmd_new() ──▸ _dump_session_chunk()
                   │
                   ▼
            LRANGE hcli:session_history:{chat_id}
                   │
                   ▼
            Write to /var/log/hcli/sessions/{chat_name}/chunk_{timestamp}.txt
                   │
                   ▼
            DELETE hcli:session_history:{chat_id}
            DELETE hcli:session_size:{chat_id}
            DELETE hcli:session:{chat_id}
                   │
                   ▼
            Orchestration reads chunks for Tier 2 context injection (up to 50KB)
```

## 6. Redis Key Contracts

| Key | Type | TTL | Direction | Owner | Purpose |
|---|---|---|---|---|---|
| `hcli:tasks` | LIST | — | bot → dispatcher | bot writes, dispatcher reads | Task dispatch queue |
| `hcli:results:{task_id}` | STRING | 600s | dispatcher → bot | dispatcher writes, bot reads | Task result + HMAC |
| `hcli:pending:{chat_id}` | LIST | 2×TASK_TIMEOUT | bot ↔ bot | bot | Per-chat task tracking for `/cancel` and `/abort` |
| `hcli:abort:{task_id}` | STRING | TASK_TIMEOUT | bot → dispatcher | bot writes, dispatcher reads | Abort signal — dispatcher kills subprocess on sight |
| `hcli:session:{chat_id}` | STRING | — | dispatcher → bot | dispatcher writes, bot deletes on `/new` | Session UUID |
| `hcli:session_history:{chat_id}` | LIST | — | dispatcher → bot | dispatcher writes, bot reads + deletes on `/new` | Conversation turns (dumped to chunk) |
| `hcli:session_size:{chat_id}` | STRING | — | dispatcher → bot | dispatcher writes, bot deletes on `/new` | Byte counter for chunk rotation |
| `hcli:stats:{YYYY-MM-DD}` | HASH | 24h | dispatcher → bot | dispatcher writes, bot reads | Daily usage counters |
| `hcli:teach:{chat_id}` | STRING | 3600s | bot ↔ bot | bot | Teach mode flag |
| `hcli:teach:{chat_id}:turns` | LIST | 3600s | bot ↔ bot | bot | Buffered teach conversation |

## 7. Security Model

### Authentication
- **Fail-closed allowlist**: `ALLOWED_CHATS` env var parsed at startup. If empty, all requests rejected.
- **`@auth_required` decorator**: Applied to every command/message handler. Checks `chat_id ∈ ALLOWED_CHATS` before execution. Unauthorized attempts logged with `chat_id` + `user_id`.

### Result Integrity
- HMAC-SHA256 over `{task_id}:{output}:{completed_at}` using shared `RESULT_HMAC_KEY`.
- Constant-time comparison via `hmac.compare_digest()`.
- Failed verification → error message to user, audit log entry.

### Container Hardening
- `cap_drop: ALL`, `no-new-privileges`, `read_only` root fs.
- Non-root user (UID 1000).
- `tmpfs` for `/tmp` and `/run`.
- On `h-network-frontend` only — no direct backend access.

## 8. Subsystem Details

### 8.1 Markdown → Telegram HTML Converter

`markdown_to_telegram_html()` converts markdown to the subset of HTML that Telegram supports (`<b>`, `<i>`, `<code>`, `<pre>`, `<a>`).

**Strategy**: Placeholder extraction to avoid double-processing.

1. Extract fenced code blocks → `<pre>` placeholders
2. Extract inline code → `<code>` placeholders
3. Extract tables (consecutive `|` lines) → `<pre>` placeholders
4. HTML-escape remaining text (`&`, `<`, `>`)
5. Convert links `[text](url)` → `<a href>`
6. Convert `**bold**` → `<b>`, `*italic*` → `<i>`, `# headers` → `<b>`
7. Convert `- item` / `* item` → bullet `•`
8. Strip horizontal rules `---`
9. Restore placeholders

### 8.2 Action System

LLM responses can embed `[action:type:payload]` markers. `send_long()` extracts them via regex before rendering, sends the text, then executes each action handler.

| Action | Handler | Behavior |
|---|---|---|
| `graph` | `_handle_graph_action` | Fetch Grafana render PNG via httpx, send as Telegram photo |

**Grafana URL resolution**: The handler matches the payload against two Grafana instances:
- **External** (`GRAFANA_URL`): HTTPS URLs → Bearer token auth
- **Internal** (`GRAFANA_INTERNAL_URL`): Other `/render/` URLs → HTTP basic auth (admin)

In both cases, the `/render/` path is extracted and rebuilt with the correct base URL to tolerate model-generated hostnames.

### 8.3 Teach Mode

Interactive skill creation flow:

1. User presses `📝 Teach` → sets `hcli:teach:{chat_id}` flag in Redis (TTL 1h)
2. During teach mode, `_poll_result()` buffers both user messages and assistant responses to `hcli:teach:{chat_id}:turns`
3. User presses `📖 End Teaching` → handler reads all buffered turns, formats them into a skill-generation prompt, queues it as a task
4. The LLM produces a skill draft for user review; user can approve to save

### 8.4 Model Toggle

In-memory dict `_chat_model[chat_id]` stores per-chat preference (`"haiku"` or `"opus"`, default `"opus"`). Included in task JSON so the dispatcher picks the right model. State is ephemeral — resets on bot restart.

### 8.5 Result Polling

`_poll_result()` runs as a fire-and-forget `asyncio.Task` (stored in `_background_tasks` set to prevent GC).

- Polls `GET hcli:results:{task_id}` every 1 second
- Verbose ON (default): spawns `_stream_activity` to show live command feed in an editable message; cancels it when result arrives
- Verbose OFF: sends typing indicator every 5 seconds instead (silent wait)
- Times out after `TASK_TIMEOUT` seconds (default 300)
- On result: verifies HMAC, appends usage stats as HTML comment, sends response
- Verbose mode toggled per-chat via `📡 Verbose` keyboard button

### 8.6 Activity Stream

`/activity` subscribes to Redis pub/sub channel `hcli:audit:{task_id}` and streams command execution to a single editable Telegram message.

- Dedicated Redis connection for pub/sub (can't share with main pool)
- Events expected as JSON: `{"command": "...", "status": "started"|"completed", "duration_s": float}`
- On `started`: previous running command marked done, new entry added
- On `completed`: matching entry updated with duration
- Shows last 8 commands, truncates at 60 chars
- Rate-limits message edits to 1/second (Telegram throttle protection)
- Auto-stops after 30 seconds with no events (task likely finished)
- Integrated into `_poll_result` — when verbose mode is ON, the queue message becomes a live activity stream; when OFF, no queue message at all
- `_poll_result` cancels the stream task when the result arrives and edits the message to show done

### 8.7 Message Splitting

`send_long()` splits HTML output at `\n` boundaries to stay under Telegram's 4096-char limit. Falls back to plain text if HTML parsing fails (`BadRequest`).

Usage stats are appended as a Telegram expandable blockquote (`<blockquote expandable>`) extracted from a `<!-- stats: ... -->` HTML comment marker.

## 9. Keyboard Layout

Persistent `ReplyKeyboardMarkup` with 4 rows:

```
┌──────────┬──────────┐
│ ⚡ Fast   │ 🧠 Deep   │   Model toggle
├──────────┼──────────┤
│ 📊 Stats  │ 📚 Skills │   Quick access
├──────────┼──────────┤
│ 📝 Teach  │ 📖 End    │   Teach mode
├──────────┴──────────┤
│ 📡 Verbose          │   Toggle live activity stream
└─────────────────────┘
```

Keyboard buttons are matched by a `filters.Regex` handler registered before the catch-all `handle_message`, so they are intercepted and never queued as tasks.

## 10. Handler Registration Order

```python
1. CommandHandler("start")     # /start
2. CommandHandler("help")      # /help
3. CommandHandler("status")    # /status
4. CommandHandler("stats")     # /stats
5. CommandHandler("new")       # /new
6. CommandHandler("cancel")    # /cancel
7. CommandHandler("abort")     # /abort — kill running task
8. CommandHandler("run")       # /run <cmd>
9. MessageHandler(Regex)       # keyboard buttons (matched first)
10. MessageHandler(PHOTO)      # inbound photos → base64 data URI
11. MessageHandler(TEXT)       # catch-all natural language
```

Order matters: keyboard button regex is registered before the generic text handler to prevent buttons from being queued as tasks. Photo handler is registered before the text handler.

## 11. External Dependencies

| Dependency | Version | Purpose |
|---|---|---|
| `python-telegram-bot` | ≥22.6, <23 | Telegram Bot API wrapper (async) |
| `redis` | ≥7.1, <8 | Async Redis client (`redis.asyncio`) |
| `httpx` | ≥0.28, <1 | HTTP client for Grafana graph fetching |
| `hcli_logging` | internal | Structured JSON logging + audit trail (shared lib) |

## 12. Logging

Uses `hcli_logging` shared library. Two logger instances:

| Logger | Output | Content |
|---|---|---|
| `logger` (app) | `/var/log/hcli/telegram/app.log` + `error.log` | Operational events, warnings, errors |
| `audit` | `/var/log/hcli/telegram/audit.log` | Security/business events with structured extra fields |

**Audit events emitted:**

| Event | Extra Fields | When |
|---|---|---|
| `task_queued` | user_id, task_id, user_message | Task submitted |
| `task_completed` | user_id, task_id | Result received |
| `task_cancelled` | user_id, task_id | `/cancel` |
| `task_aborted` | user_id, task_id | `/abort` |
| `task_timeout` | user_id, task_id, timeout | Poll loop exhausted |
| `hmac_failed` | task_id | Result signature mismatch |
| `teach_start` | user_id, chat_id | Teach mode activated |
| `teach_end` | user_id, chat_id, turns | Teach mode ended |

## 13. Configuration Reference

| Env Var | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | yes | — | BotFather token |
| `ALLOWED_CHATS` | yes | — | Comma-separated chat IDs |
| `RESULT_HMAC_KEY` | yes | — | Shared HMAC secret (generated by install.sh) |
| `REDIS_URL` | no | `redis://redis:6379` | Redis connection string |
| `MAX_CONCURRENT_TASKS` | no | `3` | Queue depth limit |
| `TASK_TIMEOUT` | no | `300` | Poll timeout in seconds |
| `CHAT_NAMES` | no | — | `chat_id:name,...` for session chunk dirs |
| `SESSION_CHUNK_DIR` | no | `/var/log/hcli/sessions` | Session dump directory |
| `GRAFANA_URL` | no | — | External Grafana base URL |
| `GRAFANA_API_TOKEN` | no | — | External Grafana Bearer token |
| `GRAFANA_INTERNAL_URL` | no | — | Internal Grafana base URL |
| `GRAFANA_ADMIN_PASSWORD` | no | — | Internal Grafana admin password |
| `LOG_DIR` | no | `/var/log/hcli` | Log output directory |
| `LOG_LEVEL` | no | `INFO` | Logging level |

## 14. Design Decisions

| Decision | Rationale |
|---|---|
| Single-file `bot.py` | Module is small enough (~825 lines) that splitting adds indirection without clarity. All concerns are separated by sections. |
| Fire-and-forget polling tasks | Allows the bot to remain responsive while waiting for results. `_background_tasks` set prevents GC. |
| Placeholder-based markdown converter | Prevents double-escaping of code blocks/tables. Simpler than a full parser for the subset Telegram supports. |
| In-memory model toggle | Ephemeral by design — no persistence needed. Users can re-select after restart. |
| HMAC on results | Ensures the dispatcher (separate container) produced the result, not a rogue Redis writer. |
| Fail-closed auth | Empty allowlist = nobody authorized. Safer default than fail-open. |
| Action markers in response text | Decouples LLM output format from bot rendering. Bot strips markers, sends text, then executes side effects. Extensible via `_ACTION_HANDLERS` dict. |
| Stats as expandable blockquote | Shows usage data without cluttering the main response. Telegram's expandable blockquote keeps it collapsed by default. |
| Chunk files as plain text | Human-readable, greppable, no parser needed. Orchestration reads them as raw text into system prompt — binary format would add complexity for no benefit. |
