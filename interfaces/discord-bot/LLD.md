# Low-Level Design — discord-bot

## 1. Overview

Single-file async Python service (`bot.py`) that acts as a Discord frontend for h-cli. It connects via Discord Gateway (outbound WebSocket), receives events and slash commands, queues tasks to Redis, polls for results from the orchestration layer, and delivers responses back to Discord channels/threads/DMs. It is stateless — all persistent state lives in Redis.

## 2. Position in System Flow

discord-bot owns the same two steps as all other interfaces:

- **Step 1 — Ingest**: Receive Discord event/command, authenticate, build task JSON, `RPUSH` to Redis, spawn poller.
- **Step 9 — Delivery**: Pick up signed result from Redis, verify HMAC, render to Discord message/embed, deliver to channel/thread.

```
         discord-bot                 opaque to us                   discord-bot
        ┌───────────┐    ┌─────────────────────────────────┐    ┌───────────────┐
Step 1  │ auth      │    │ Steps 2–8                       │    │ HMAC verify   │ Step 9
User ──►│ task JSON │──►Redis──► dispatcher ──► ... ──►Redis──►│ embed/msg     │──► User
        │ RPUSH     │    │                                 │    │ send/file     │
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
interfaces/discord-bot/
├── bot.py              Main application — all bot logic in one module
├── Dockerfile          Container image (python:3.12-slim, non-root user)
├── entrypoint.sh       Creates log dirs, then exec's CMD
├── requirements.txt    Pinned deps: discord.py, redis, httpx
└── LLD.md              This file
```

### bot.py — Internal Sections

| Section | Purpose |
|---|---|
| Config | Env var loading, constants, Redis key prefixes, guild IDs |
| Redis pool | Async Redis connection pool (module-level singleton) |
| Discord client | `discord.Client` + `CommandTree` for slash commands |
| Helpers | `authorized()`, `_verify_result()`, `_chat_tasks_key()`, `_thread_chat_id()` |
| Grafana actions | `_handle_graph_action()` — fetch and send graph PNGs |
| Response rendering | `send_response()` — plain message / embed / file based on length |
| Session chunking | `_dump_session_chunk()` — same format as other interfaces |
| Activity stream | `_format_activity()`, `_abort_view()`, `_stream_activity()` — live feed with abort button |
| Task queue | `_queue_task()` — concurrency check, Redis RPUSH, spawn poller |
| Result polling | `_poll_result()`, `_process_result()` — subscribe + GET fallback, HMAC verify |
| Slash commands | `/run`, `/new`, `/cancel`, `/abort`, `/status`, `/stats`, `/model`, `/teach`, `/verbose`, `/skills`, `/help` |
| Event handlers | `on_ready` (slash command sync), `on_message` (DMs + @mentions) |
| Main | `client.run()` with Gateway connection |

## 4. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    discord-bot container                   │
│                                                          │
│  Discord API ──(Gateway WebSocket)──▸ discord.py         │
│                                           │              │
│                     ┌────────────────────┼───────────┐   │
│                     │                    │           │   │
│               SlashCommands         on_message    Buttons │
│                     │               (DM+mention)     │   │
│                     └────────────────────┼───────────┘   │
│                                          ▼               │
│                                   _queue_task()          │
│                                      │    │              │
│                             RPUSH    │    │  asyncio     │
│                                      ▼    ▼              │
│                                  Redis   _poll_result()  │
│                                             │            │
│                                  SUBSCRIBE  │            │
│                                  +GET loop  │            │
│                                             ▼            │
│                                    send_response()       │
│                                      │    │    │         │
│                              message │ embed│  │ file    │
│                                      ▼    ▼    ▼         │
│                                  Discord  Grafana (httpx) │
└──────────────────────────────────────────────────────────┘
```

## 5. Discord-Specific Decisions

### 5.1 Threading

- **Channel @mentions**: Auto-create thread under the mention message, reply in thread
- **Existing threads**: Reply in the thread
- **DMs**: Direct reply, no threading (DMs don't support threads)
- **Slash commands**: Reply in current context (channel or thread)

### 5.2 Session Mapping

`chat_id` uses composite key format:
- **Threaded**: `{channel_id}:{thread_id}` — each thread gets its own session
- **Unthreaded** (DMs, slash commands): `{channel_id}` or `{user_id}` — scoped to context

### 5.3 Long Output Handling

| Size | Strategy |
|---|---|
| ≤ 2,000 chars | Plain message with Discord markdown |
| 2,001–4,096 chars | Embed with description field (green sidebar) |
| > 4,096 chars | Embed preview (200 chars) + `.md` file attachment |

### 5.4 Ephemeral Responses

Status-only commands send ephemeral responses (only visible to requester):
- `/status`, `/stats`, `/help`, `/verbose`, `/model`, `/skills`, `/abort`

### 5.5 Abort Button

Activity messages include a red "Abort" button (`discord.ui.Button` with danger style). Clicking it:
1. Checks authorization
2. Publishes abort to Redis control channel
3. Disables the button
4. Sends ephemeral confirmation

### 5.6 Slash Command Registration

Guild-specific registration via `DISCORD_GUILD_IDS` env var (instant updates). Falls back to global registration if no guild IDs configured (up to 1h propagation delay).

### 5.7 Grafana Actions

Same `[action:graph:url]` system as other interfaces. Graphs sent as file attachments (`discord.File`).

## 6. Redis Key Contracts

Identical to telegram-bot, slack-bot, and web (see HLD.md §4). Task JSON includes `"source": "discord"`.

## 7. Security Model

### Authentication
- **Fail-closed allowlist**: `ALLOWED_USERS` (Discord user IDs) + optional `ALLOWED_ROLES` (Discord role IDs). Both empty = all requests rejected.
- Every slash command and event handler checks `authorized()` before processing.
- Role-based auth checks `discord.Member.roles` — only works in guilds (not DMs).

### Result Integrity
- Same HMAC-SHA256 as all other interfaces: `{task_id}:{output}:{completed_at}` with shared `RESULT_HMAC_KEY`.
- Constant-time comparison via `hmac.compare_digest()`.

### Container Hardening
- Same posture as other interfaces: `cap_drop: ALL`, `no-new-privileges`, `read_only` rootfs.
- Non-root user (UID 1000).
- `tmpfs` for `/tmp` and `/run`.
- On `h-network-frontend` only.

## 8. Configuration Reference

| Env Var | Required | Default | Description |
|---|---|---|---|
| `DISCORD_BOT_TOKEN` | yes | — | Bot token from Developer Portal |
| `ALLOWED_USERS` | yes* | — | Comma-separated Discord user IDs |
| `ALLOWED_ROLES` | no | — | Comma-separated Discord role IDs (optional) |
| `RESULT_HMAC_KEY` | yes | — | Shared HMAC secret (generated by install.sh) |
| `DISCORD_GUILD_IDS` | no | — | Comma-separated guild IDs for instant command sync |
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

*At least one of `ALLOWED_USERS` or `ALLOWED_ROLES` must be set for any user to be authorized.

## 9. External Dependencies

| Dependency | Version | Purpose |
|---|---|---|
| `discord.py` | ≥2.4, <3 | Discord API wrapper (async, Gateway, slash commands) |
| `redis` | ≥7.1, <8 | Async Redis client (`redis.asyncio`) |
| `httpx` | ≥0.28, <1 | HTTP client for Grafana graph fetching |
| `hcli_logging` | internal | Structured JSON logging + audit trail (shared lib) |

## 10. Deployment

### Compose Profile

The discord-bot service is behind the `discord` compose profile. It only starts when `COMPOSE_PROFILES` includes `discord`:

```yaml
profiles: ["discord"]
```

Enable via `setup.sh` (interactive) or manually in `.env`:
```
COMPOSE_PROFILES=discord
```

### setup.sh Integration

When the user selects Discord (option 3) during `setup.sh`, the script:
1. Prompts for `DISCORD_BOT_TOKEN` (skips if already set)
2. Prompts for `DISCORD_ALLOWED_USERS` (comma-separated Discord user IDs)
3. Prompts for `DISCORD_GUILD_IDS` (comma-separated, for instant slash command sync)
4. Adds `discord` to `COMPOSE_PROFILES`

## 11. Design Decisions

| Decision | Rationale |
|---|---|
| Gateway (WebSocket) | Outbound-only — no ingress, firewall-friendly, matches Slack Socket Mode pattern |
| `discord.py` v2.x | De facto standard Python Discord library, native async, built-in slash commands |
| Single-file `bot.py` | Same rationale as other interfaces — module is small enough that splitting adds indirection |
| Guild-specific command sync | Instant updates vs up to 1h for global. Private bot doesn't need global registration |
| Embeds for medium output | Richer formatting than plain messages, colored sidebar for visual distinction |
| Ephemeral for status commands | Discord-idiomatic — keeps channels clean, only requester sees the response |
| Abort button on activity | Better UX than `/abort` command — visible, one-click, contextual |
| User ID + role-based auth | User IDs for individuals, roles for team-level access. Both fail-closed. |
| `source: "discord"` | Consistent with telegram/slack/web for audit trail distinction |
| Thread auto-creation | Channel mentions auto-create threads to keep conversations organized |
