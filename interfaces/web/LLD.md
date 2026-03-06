# Low-Level Design — web

## 1. Overview

Lightweight web chat interface for h-cli. FastAPI serves a single-page HTML UI with WebSocket for real-time bidirectional communication. Server-side markdown rendering, HTTP Basic Auth, same Redis contracts as telegram-bot and slack-bot. Zero JavaScript build toolchain.

## 2. Position in System Flow

Same two steps as all interfaces:

- **Step 1 — Ingest**: Receive message via WebSocket, authenticate (session cookie proves prior Basic Auth), build task JSON, `RPUSH` to Redis, spawn poller.
- **Step 9 — Delivery**: Pick up signed result from Redis, verify HMAC, render markdown to HTML (server-side), send over WebSocket.

```
         web                        opaque to us                        web
        ┌───────────┐    ┌─────────────────────────────────┐    ┌───────────────┐
Step 1  │ auth      │    │ Steps 2–8                       │    │ HMAC verify   │ Step 9
User ──►│ task JSON │──►Redis──► dispatcher ──► ... ──►Redis──►│ md → HTML     │──► User
        │ RPUSH     │    │                                 │    │ ws.send_json  │
        └───────────┘    └─────────────────────────────────┘    └───────────────┘
```

## 3. File Responsibilities

```
interfaces/web/
├── bot.py              FastAPI app — WebSocket handler, Redis lifecycle, commands
├── templates/
│   └── index.html      Single-page chat UI (Jinja2)
├── static/
│   ├── style.css       Dark theme CSS
│   └── ws.js           WebSocket client (~260 lines, native API)
├── Dockerfile          python:3.12-slim, non-root uid 1000
├── entrypoint.sh       Log dir creation, exec CMD
├── requirements.txt    fastapi, uvicorn, jinja2, redis, httpx, markdown
└── LLD.md              This file
```

### bot.py — Internal Sections

| Section | Purpose |
|---|---|
| Config | Env var loading, constants, Redis key prefixes |
| Redis pool | Async connection pool singleton |
| FastAPI app | App instance, static files, templates |
| Auth | HTTP Basic Auth verification (fail-closed) |
| Helpers | `_verify_result()`, `markdown_to_html()`, `_handle_graph_action()` |
| Session chunking | `_dump_session_chunk()` — same format as other interfaces |
| Activity stream | `_format_activity()`, `_stream_activity()` — JSON over WebSocket |
| Task queue | `_queue_task()` — concurrency check, RPUSH, spawn poller |
| Result polling | `_poll_result()` — pub/sub + GET fallback, HMAC verify, send result |
| Command handlers | `_handle_command()` — /run, /new, /cancel, /abort, /status, /stats, /model, /teach, /verbose, /skills, /help |
| Routes | `GET /` (HTML page), `WS /ws` (WebSocket endpoint) |
| Lifecycle | startup/shutdown hooks for Redis pool |
| Main | uvicorn runner |

## 4. Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    web container                          │
│                                                          │
│  Browser ──(HTTP Basic Auth)──▸ GET / ──▸ index.html     │
│         ──(WebSocket)──────────▸ /ws endpoint             │
│                                    │                      │
│                    ┌───────────────┼───────────────┐      │
│                    │               │               │      │
│              /commands       natural language   ping/pong  │
│                    │               │                      │
│                    ▼               ▼                      │
│              _handle_command  _queue_task()               │
│                                  │    │                   │
│                         RPUSH    │    │  asyncio.Task     │
│                                  ▼    ▼                   │
│                              Redis   _poll_result()       │
│                                         │                 │
│                                GET loop │                 │
│                                         ▼                 │
│                                  ws.send_json()           │
│                                    │    │                 │
│                              HTML  │    │ images          │
│                                    ▼    ▼                 │
│                              Browser  Grafana (httpx)     │
└──────────────────────────────────────────────────────────┘
```

## 5. Authentication Flow

```
Browser                          FastAPI
   │                                │
   │  GET / (no auth)               │
   │ ──────────────────────────────▸│
   │  401 + WWW-Authenticate: Basic │
   │ ◂──────────────────────────────│
   │                                │
   │  GET / (Authorization: Basic)  │
   │ ──────────────────────────────▸│
   │  verify_credentials()          │
   │  200 + Set-Cookie: hcli_session│
   │      + Set-Cookie: hcli_user   │
   │ ◂──────────────────────────────│
   │                                │
   │  WS /ws (Cookie: hcli_session, │
   │          Cookie: hcli_user)    │
   │ ──────────────────────────────▸│
   │  session cookie validates user │
   │  username from hcli_user cookie│
   │  WebSocket accepted            │
   │ ◂──────────────────────────────│
```

### Multi-User Support

Two configuration modes:

1. **Multi-user** (`WEB_USERS`): `admin:pass1,engineer:pass2,noc:pass3` — comma-separated `username:password` pairs
2. **Single-user** (`WEB_USERNAME`/`WEB_PASSWORD`): legacy mode, used as fallback when `WEB_USERS` is not set

`WEB_USERS` takes priority. Fail-closed: empty user list = reject all.

### Per-User Sessions

Each user gets isolated sessions via the `chat_id` format: `web:{username}:{session_uuid}`. This ensures:
- Each user has their own conversation history
- Each user has their own session chunks
- Audit trail shows `user_id` = `web:{username}`

WebSocket auth relies on the session cookie set during the initial page load (which required Basic Auth). The `hcli_user` cookie carries the username to the WebSocket endpoint.

## 6. WebSocket Protocol

### Client → Server

```json
{"type": "message", "content": "check BGP on router-01"}
{"type": "message", "content": "/run ping 10.0.0.1"}
{"type": "ping"}
```

### Server → Client

```json
{"type": "task_queued", "task_id": "abc12345-..."}
{"type": "activity", "task_id": "abc12345", "done": false, "commands": [...]}
{"type": "result", "content": "<p>HTML</p>", "raw": "markdown", "stats": {...}, "task_id": "..."}
{"type": "system", "content": "Context cleared."}
{"type": "error", "content": "Queue full."}
{"type": "image", "content": "data:image/png;base64,..."}
{"type": "pong"}
```

## 7. Session Management

- `chat_id` = `web:{username}:{session_uuid}` (per-user isolation)
- Cookies: `hcli_session` (UUID), `hcli_user` (username) — both `HttpOnly`, `SameSite=Strict`, `Secure` (when SSL), `max_age=86400` (24h)
- `/new` command: dumps session chunk, clears Redis session keys
- Tab close: WebSocket disconnects, background pollers continue. Results are written to Redis by the poller. On reconnect, the session cookie maps to the same `chat_id`.

## 8. Long Output Handling

| Size | Strategy |
|---|---|
| Any | Full HTML rendered in scrollable message div |
| > 3,000 chars | "Download as .md" link appended to message |

No splitting needed — web has no message size limit. Code blocks get copy buttons via client-side JS.

## 9. Redis Key Contracts

Identical to telegram-bot and slack-bot. Task JSON includes `"source": "web"`.

## 10. Security Model

### Authentication
- HTTP Basic Auth with `WEB_USERS` (multi-user) or `WEB_USERNAME`/`WEB_PASSWORD` (single-user fallback)
- Fail-closed: empty user list = reject all requests (403)
- Constant-time comparison via `secrets.compare_digest()`
- Dummy comparison on unknown usernames to prevent timing-based enumeration

### Session
- `hcli_session` cookie UUID proves prior authentication
- `hcli_user` cookie carries username to WebSocket endpoint
- `HttpOnly` prevents JS access to cookies
- `SameSite=Strict` prevents CSRF
- `Secure` flag set when `WEB_SSL=true`

### Result Integrity
- Same HMAC-SHA256 as other interfaces

### Container Hardening
- Same posture: `cap_drop: ALL`, `no-new-privileges`, `read_only` rootfs
- Non-root user (UID 1000)
- `tmpfs` for `/tmp` and `/run`
- `h-network-frontend` only

## 11. Configuration Reference

| Env Var | Required | Default | Description |
|---|---|---|---|
| `WEB_USERS` | no | — | Multi-user: `user1:pass1,user2:pass2` (priority over single-user) |
| `WEB_USERNAME` | no | — | Single-user Basic Auth username (fallback) |
| `WEB_PASSWORD` | no | — | Single-user Basic Auth password (fallback) |
| `RESULT_HMAC_KEY` | yes | — | Shared HMAC secret |
| `REDIS_URL` | no | `redis://redis:6379` | Redis connection string |
| `WEB_PORT` | no | `8080` | HTTP listen port |
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

## 12. External Dependencies

| Dependency | Version | Purpose |
|---|---|---|
| `fastapi` | >=0.115, <1 | Async web framework, WebSocket support |
| `uvicorn` | >=0.34, <1 | ASGI server |
| `jinja2` | >=3.1, <4 | HTML templating |
| `markdown` | >=3.7, <4 | Server-side markdown → HTML |
| `redis` | >=7.1, <8 | Async Redis client |
| `httpx` | >=0.28, <1 | Grafana graph fetching |
| `hcli_logging` | internal | Structured JSON logging + audit trail |

## 13. Deployment

### Compose Profile

The web service is behind the `web` compose profile. It only starts when `COMPOSE_PROFILES` includes `web`:

```yaml
profiles: ["web"]
```

Enable via `setup.sh` (interactive) or manually in `.env`:
```
COMPOSE_PROFILES=web
```

### setup.sh Integration

When the user selects Web UI (option 4) during `setup.sh`, the script:
1. Prompts for `WEB_USERNAME` (defaults to `admin`)
2. Prompts for `WEB_PASSWORD` (auto-generates if left empty)
3. Adds `web` to `COMPOSE_PROFILES`

## 14. Design Decisions

| Decision | Rationale |
|---|---|
| FastAPI + uvicorn | Async-native, WebSocket built-in, minimal overhead |
| Server-side markdown | Zero frontend JS deps for rendering, consistent with other interfaces |
| HTTP Basic Auth | Simplest auth that works, fail-closed, no external OAuth dependency |
| Session cookie for WS auth | Browsers can't send Basic Auth headers on WebSocket upgrade — cookie proves prior auth |
| Single WebSocket | Bidirectional: messages + commands upstream, results + activity downstream |
| Dark theme | Terminal aesthetic matches the engineering audience |
| No external CDN | Self-contained — works in air-gapped environments |
| Copy buttons on code blocks | Added client-side for UX, only JS enhancement needed |
