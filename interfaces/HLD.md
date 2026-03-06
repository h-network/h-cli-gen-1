# High-Level Design — Interfaces Layer

## 1. Purpose

The `interfaces/` directory contains all user-facing frontends for h-cli. Each frontend is a self-contained plugin that translates between a user-facing protocol (Telegram, Slack, web, etc.) and the orchestration layer via Redis.

Frontends own two steps of the message lifecycle:

- **Step 1 — Ingest**: Receive user input, authenticate, build a task JSON, push to Redis.
- **Step 9 — Delivery**: Pick up the signed result from Redis, format it for the target platform, deliver to the user.

Everything between steps 1 and 9 is opaque to the interfaces layer.

## 2. Current Plugins

| Plugin | Directory | Protocol | Status |
|--------|-----------|----------|--------|
| Telegram Bot | `interfaces/telegram-bot/` | Telegram Bot API (long-polling) | Production |
| Slack Bot | `interfaces/slack-bot/` | Slack Socket Mode (WebSocket) | Production |
| Web UI | `interfaces/web/` | HTTP + WebSocket (FastAPI) | Production |

### Interface Comparison

| Feature | Telegram | Slack | Web |
|---|---|---|---|
| Connection | Long-polling (outbound) | Socket Mode (outbound WS) | HTTP + WebSocket |
| Auth | `ALLOWED_CHATS` (chat ID allowlist) | `ALLOWED_USERS` (Slack user ID allowlist) | HTTP Basic Auth |
| Message format | Telegram HTML subset | Slack mrkdwn | Full HTML (server-side markdown) |
| Message limit | 4,096 chars (split) | 3K inline / 15K snippet / file | Unlimited (scrollable) |
| Threading | Flat (reply to message) | Always in threads | WebSocket stream |
| Notifications | Telegram push | Slack notifications | Browser tab only |
| Activity stream | Editable message | Editable message | WebSocket JSON → DOM panel |
| Controls | Persistent keyboard buttons | Slash commands | Slash commands + buttons |
| Source field | — | `"source": "slack"` | `"source": "web"` |

## 3. Adding a New Frontend

Each new frontend lives in its own directory under `interfaces/`:

```
interfaces/
├── telegram-bot/       Telegram Bot API
├── slack-bot/          Slack Socket Mode
├── web/                FastAPI + WebSocket
├── <new-frontend>/     Future frontend
│   ├── LLD.md          Implementation details and contracts
│   ├── Dockerfile      Container image
│   └── ...             Source code
└── HLD.md              This file
```

Requirements for a new frontend:

1. **Own directory** — `interfaces/<name>/`
2. **Own LLD** — `interfaces/<name>/LLD.md` documenting implementation, Redis contracts, and security model
3. **Own Dockerfile** — self-contained container image
4. **No shared code** — frontends do not import from each other; shared libraries live in `shared/`
5. **Redis-only communication** — all interaction with orchestration goes through the Redis contracts below

## 4. Contract with Orchestration

Frontends communicate with the orchestration layer exclusively through Redis. There is no direct function call, HTTP, or gRPC interface.

### Sending tasks (frontend → orchestration)

Push a task JSON to the shared task queue:

- **Key**: `hcli:tasks` (LIST, RPUSH)
- **Payload**: JSON with `task_id`, `message`, `user_id`, `chat_id`, `submitted_at`, `model`, optionally `source`

### Receiving results (orchestration → frontend)

Subscribe for notification, then GET the result:

- **Notification**: `hcli:task:{task_id}:notify` (PUBSUB, SUBSCRIBE) — dispatcher publishes when result is ready
- **Result**: `hcli:results:{task_id}` (STRING, GET then DELETE)
- **Payload**: JSON with `output`, `completed_at`, `usage`, `hmac`
- **Integrity**: HMAC-SHA256 over `{task_id}:{output}:{completed_at}` — frontends must verify before delivering
- **Fallback**: Frontends also poll via GET every 10s as a safety net for missed pub/sub messages

### Cancellation / abort

- **Key**: `hcli:control:{task_id}` (PUBSUB) — frontend publishes `{"action": "abort"}`, dispatcher subscribes and kills the running task

### Per-chat task tracking

- **Key**: `hcli:chat:{chat_id}:tasks` (LIST) — frontend tracks task IDs per chat for `/cancel` and `/abort`

### Session management

- `hcli:session:{chat_id}` — session UUID (orchestration writes, frontend clears on reset)
- `hcli:session_history:{chat_id}` — conversation turns (orchestration writes, frontend reads and clears on reset)
- `hcli:session_size:{chat_id}` — byte counter for chunk rotation

### Activity stream

- `hcli:audit:{task_id}` (PUBSUB) — dispatcher publishes command execution events, frontend subscribes for live activity display
- Events: `{"command": "...", "status": "running"|"completed"|"failed", "duration_ms": float}`

### Task state

- `hcli:task:{task_id}:state` (STRING) — dispatcher writes task state, frontend reads during activity stream idle to detect early completion

## 5. Design Principles

- **Self-contained plugins** — each frontend is independently deployable and testable. No cross-frontend dependencies.
- **Redis as the only interface** — frontends never call orchestration directly. Redis is the nervous system.
- **Fail-closed authentication** — every frontend must authenticate users before queuing tasks. Empty allowlists reject all requests.
- **HMAC verification** — every frontend must verify result integrity before delivery. Unverified results are rejected.
- **Platform-native rendering** — each frontend converts the orchestration response to its platform's format (HTML for Telegram, mrkdwn for Slack, full HTML for web).
- **Co-existence** — multiple frontends can run in the same h-cli instance. Task UUIDs prevent collisions. The optional `source` field in task JSON enables audit trail distinction.
