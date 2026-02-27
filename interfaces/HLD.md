# High-Level Design — Interfaces Layer

## 1. Purpose

The `interfaces/` directory contains all user-facing frontends for h-cli. Each frontend is a self-contained plugin that translates between a user-facing protocol (Telegram, web, CLI, etc.) and the orchestration layer via Redis.

Frontends own two steps of the message lifecycle:

- **Step 1 — Ingest**: Receive user input, authenticate, build a task JSON, push to Redis.
- **Step 9 — Delivery**: Pick up the signed result from Redis, format it for the target platform, deliver to the user.

Everything between steps 1 and 9 is opaque to the interfaces layer.

## 2. Current Plugins

| Plugin | Directory | Protocol | Status |
|--------|-----------|----------|--------|
| Telegram Bot | `interfaces/telegram-bot/` | Telegram Bot API (long-polling) | Production |

## 3. Adding a New Frontend

Each new frontend lives in its own directory under `interfaces/`:

```
interfaces/
├── telegram-bot/       Existing
├── web-ui/             Example future frontend
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
- **Payload**: JSON with `task_id`, `message`, `user_id`, `chat_id`, `submitted_at`, `model`

### Receiving results (orchestration → frontend)

Poll for the signed result:

- **Key**: `hcli:results:{task_id}` (STRING, GET then DELETE)
- **Payload**: JSON with `output`, `completed_at`, `usage`, `hmac`
- **Integrity**: HMAC-SHA256 over `{task_id}:{output}:{completed_at}` — frontends must verify before delivering

### Cancellation / abort

- **Key**: `hcli:abort:{task_id}` (STRING) — frontend writes, orchestration reads and kills the running task

### Session management

- `hcli:session:{chat_id}` — session UUID (orchestration writes, frontend clears on reset)
- `hcli:session_history:{chat_id}` — conversation turns (orchestration writes, frontend reads and clears on reset)
- `hcli:session_size:{chat_id}` — byte counter for chunk rotation

## 5. Design Principles

- **Self-contained plugins** — each frontend is independently deployable and testable. No cross-frontend dependencies.
- **Redis as the only interface** — frontends never call orchestration directly. Redis is the nervous system.
- **Fail-closed authentication** — every frontend must authenticate users before queuing tasks. Empty allowlists reject all requests.
- **HMAC verification** — every frontend must verify result integrity before delivery. Unverified results are rejected.
- **Platform-native rendering** — each frontend converts the orchestration response to its platform's format (HTML for Telegram, etc.).
