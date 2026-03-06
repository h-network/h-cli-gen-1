# Low-Level Design — slack-bot

## 1. Overview

Single-file async Python service (`bot.py`) that acts as a Slack frontend for h-cli. It connects via Socket Mode (outbound WebSocket), receives events and slash commands, queues tasks to Redis, polls for results from the orchestration layer, and delivers responses back to Slack threads. It is stateless — all persistent state lives in Redis.

## 2. Position in System Flow

slack-bot owns the same two steps as telegram-bot:

- **Step 1 — Ingest**: Receive Slack event/command, authenticate, build task JSON, `RPUSH` to Redis, spawn poller.
- **Step 9 — Delivery**: Pick up signed result from Redis, verify HMAC, convert markdown to Slack mrkdwn, deliver to thread.

```
         slack-bot                   opaque to us                   slack-bot
        ┌───────────┐    ┌─────────────────────────────────┐    ┌───────────────┐
Step 1  │ auth      │    │ Steps 2–8                       │    │ HMAC verify   │ Step 9
User ──►│ task JSON │──►Redis──► dispatcher ──► ... ──►Redis──►│ mrkdwn render │──► User
        │ RPUSH     │    │                                 │    │ send/upload   │
        └───────────┘    └─────────────────────────────────┘    └───────────────┘
```

### 2.1 What We Produce

| Artifact | Consumer | Format | Redis Key / Path |
|---|---|---|---|
| Task JSON | Orchestration (dispatcher) | JSON with `task_id`, `message`, `user_id`, `chat_id`, `submitted_at`, `model`, `source` | `hcli:tasks` (RPUSH) |
| Pending task tracking | Self (for `/cancel`) | task_id string | `hcli:chat:{chat_id}:tasks` (RPUSH) |
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
| Audit events | Orchestration (dispatcher) | JSON with `command`, `status`, `duration_ms` | `hcli:audit:{task_id}` (SUBSCRIBE) |

## 3. File Responsibilities

```
interfaces/slack-bot/
├── bot.py              Main application — all bot logic in one module
├── Dockerfile          Container image (python:3.12-slim, non-root user)
├── entrypoint.sh       Creates log dirs, then exec's CMD
└── requirements.txt    Pinned deps: slack-bolt, slack-sdk, redis, httpx
```

### bot.py — Internal Sections

| Section | Purpose |
|---|---|
| Config | Env var loading, constants, Redis key prefixes |
| Redis pool | Async Redis connection pool (module-level singleton) |
| Slack app | `AsyncApp` instance via slack-bolt |
| Helpers | `authorized()`, `markdown_to_slack_mrkdwn()`, `send_response()`, `_get_redis()` |
| Session chunking | `_dump_session_chunk()` — same format as telegram-bot |
| Activity stream | `_format_activity()`, `_stream_activity()` — live command feed via editable messages |
| Task queue | `_queue_task()` — concurrency check, Redis RPUSH, spawn poller |
| Result polling | `_poll_result()` — async loop, HMAC verify, teach buffering, send response |
| Slash commands | `/run`, `/new`, `/cancel`, `/abort`, `/status`, `/stats`, `/model`, `/teach`, `/verbose`, `/skills`, `/help` |
| Event handlers | `app_mention` (channel mentions), `message` (DMs) |
| Main | Socket Mode handler startup |

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    slack-bot container                    │
│                                                          │
│  Slack API ──(Socket Mode WebSocket)──▸ slack-bolt       │
│                                             │            │
│                     ┌───────────────────────┼─────────┐  │
│                     │                       │         │  │
│               SlashCommands          app_mention   message│
│                     │                       │         │  │
│                     └───────────────────────┼─────────┘  │
│                                             ▼            │
│                                      _queue_task()       │
│                                         │    │           │
│                                RPUSH    │    │  asyncio   │
│                                         ▼    ▼           │
│                                     Redis   _poll_result()│
│                                                │         │
│                                       GET loop │         │
│                                                ▼         │
│                                       send_response()    │
│                                         │    │           │
│                                 mrkdwn  │    │ files     │
│                                         ▼    ▼           │
│                                    Slack  Grafana (httpx) │
└──────────────────────────────────────────────────────────┘
```

## 5. Slack-Specific Decisions

### 5.1 Threading

All responses go in threads:
- **Channel @mentions**: thread under the original message (`event.ts` or existing `thread_ts`)
- **DMs**: thread under each user message

This keeps channels clean and conversations organized.

### 5.2 Session Mapping

`chat_id` uses composite key format:
- **Threaded**: `{channel_id}:{thread_ts}` — each thread gets its own session
- **Unthreaded** (slash commands): `{channel_id}` — channel-level session

### 5.3 Long Output Handling

| Size | Strategy |
|---|---|
| ≤ 3,000 chars | Inline mrkdwn message |
| 3,001–15,000 chars | File upload as `.md` snippet (renders inline) |
| > 15,000 chars | File upload as `.md` attachment |

### 5.4 Markdown Conversion

`markdown_to_slack_mrkdwn()` converts standard markdown to Slack's mrkdwn:

| Markdown | Slack mrkdwn |
|---|---|
| `**bold**` | `*bold*` |
| `*italic*` | `_italic_` |
| `` `code` `` | `` `code` `` |
| ```` ```block``` ```` | ```` ```block``` ```` |
| `[text](url)` | `<url\|text>` |
| `# Header` | `*Header*` |

### 5.5 Slash Commands vs Keyboard Buttons

Telegram uses persistent keyboard buttons. Slack doesn't have an equivalent, so all interactive features are slash commands:

| Telegram Button | Slack Command |
|---|---|
| ⚡ Fast / 🧠 Deep | `/model fast` / `/model deep` |
| 📊 Stats | `/stats` |
| 📚 Skills | `/skills` |
| 📝 Teach / 📖 End Teaching | `/teach` / `/teach end` |
| 📡 Verbose | `/verbose` |

### 5.6 Grafana Actions

Same `[action:graph:url]` system as telegram-bot. Difference: graphs are uploaded as files via `files_upload_v2` instead of sent as Telegram photos.

## 6. Redis Key Contracts

Identical to telegram-bot (see HLD.md §4). One addition:

| Field | Value | Purpose |
|---|---|---|
| `source` in task JSON | `"slack"` | Distinguishes Slack tasks from Telegram in logs/stats |

## 7. Security Model

### Authentication
- **Fail-closed allowlist**: `ALLOWED_USERS` env var (Slack user IDs). If empty, all requests rejected.
- Every slash command and event handler checks `authorized(user_id)` before processing.

### Result Integrity
- Same HMAC-SHA256 as telegram-bot: `{task_id}:{output}:{completed_at}` with shared `RESULT_HMAC_KEY`.
- Constant-time comparison via `hmac.compare_digest()`.

### Container Hardening
- Same posture as telegram-bot: `cap_drop: ALL`, `no-new-privileges`, `read_only` rootfs.
- Non-root user (UID 1000).
- `tmpfs` for `/tmp` and `/run`.
- On `h-network-frontend` only.

## 8. Configuration Reference

| Env Var | Required | Default | Description |
|---|---|---|---|
| `SLACK_BOT_TOKEN` | yes | — | Bot user OAuth token (`xoxb-...`) |
| `SLACK_APP_TOKEN` | yes | — | App-level token for Socket Mode (`xapp-...`) |
| `ALLOWED_USERS` | yes | — | Comma-separated Slack user IDs |
| `RESULT_HMAC_KEY` | yes | — | Shared HMAC secret (generated by install.sh) |
| `REDIS_URL` | no | `redis://redis:6379` | Redis connection string |
| `MAX_CONCURRENT_TASKS` | no | `3` | Queue depth limit |
| `TASK_TIMEOUT` | no | `300` | Poll timeout in seconds |
| `CHAT_NAMES` | no | — | `channel_id:name,...` for session chunk dirs |
| `SESSION_CHUNK_DIR` | no | `/var/log/hcli/sessions` | Session dump directory |
| `GRAFANA_URL` | no | — | External Grafana base URL |
| `GRAFANA_API_TOKEN` | no | — | External Grafana Bearer token |
| `GRAFANA_INTERNAL_URL` | no | — | Internal Grafana base URL |
| `GRAFANA_ADMIN_PASSWORD` | no | — | Internal Grafana admin password |
| `LOG_DIR` | no | `/var/log/hcli` | Log output directory |
| `LOG_LEVEL` | no | `INFO` | Logging level |

## 9. External Dependencies

| Dependency | Version | Purpose |
|---|---|---|
| `slack-bolt` | ≥1.20, <2 | Slack framework (async, Socket Mode) |
| `slack-sdk` | ≥3.33, <4 | Slack Web API client |
| `redis` | ≥7.1, <8 | Async Redis client (`redis.asyncio`) |
| `httpx` | ≥0.28, <1 | HTTP client for Grafana graph fetching |
| `hcli_logging` | internal | Structured JSON logging + audit trail (shared lib) |

## 10. Deployment

### Compose Profile

The slack-bot service is behind the `slack` compose profile. It only starts when `COMPOSE_PROFILES` includes `slack`:

```yaml
profiles: ["slack"]
```

Enable via `setup.sh` (interactive) or manually in `.env`:
```
COMPOSE_PROFILES=slack
```

### setup.sh Integration

When the user selects Slack (option 2) during `setup.sh`, the script:
1. Prompts for `SLACK_BOT_TOKEN` (xoxb-..., skips if already set)
2. Prompts for `SLACK_APP_TOKEN` (xapp-..., skips if already set)
3. Prompts for `SLACK_ALLOWED_USERS` (comma-separated Slack user IDs)
4. Adds `slack` to `COMPOSE_PROFILES`

## 11. Design Decisions

| Decision | Rationale |
|---|---|
| Socket Mode | Outbound-only WebSocket — no ingress, firewall-friendly, matches telegram-bot pattern |
| Single-file `bot.py` | Same rationale as telegram-bot — module is small enough that splitting adds indirection |
| `source` field in task JSON | Enables audit trail distinction between Slack and Telegram without contract changes |
| Thread-based sessions | Keeps channel conversations isolated. Composite `chat_id` requires no contract change |
| File upload for long output | Slack's native file rendering is superior to message splitting |
| Slash commands for all controls | No persistent keyboard in Slack — slash commands are the idiomatic equivalent |
| Same HMAC/auth model | Consistent security posture across all interfaces |
