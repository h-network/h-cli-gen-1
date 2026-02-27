# Core Module — Low-Level Design

## 1. Overview

The Core module is the **execution engine** of h-cli. It runs inside an isolated
Debian container and exposes two MCP (Model Context Protocol) servers over SSE:

| Service | File | Port | Tool |
|---------|------|------|------|
| Command execution | `mcp_server.py` | 8083 | `run_command` |
| Semantic memory | `memory_server.py` | 8084 | `memory_search` |

Core never sees raw user input. Every `run_command` call has already survived
two Asimov firewall layers in the Orchestration container before reaching us.
The Orchestration layer (`llm/claude-code/`) is the sole consumer of both endpoints.

---

## 2. File Responsibilities

```
core/
├── Dockerfile           Container image: base packages, users, deps
├── entrypoint.sh        Runtime init: SSH keys, sudo, log dirs, service start
├── mcp_server.py        Primary MCP server — shell command execution
├── memory_server.py     Optional MCP server — Qdrant-backed semantic search
└── requirements.txt     Python dependencies for both servers
```

### 2.1 `mcp_server.py`

Single-tool MCP server that executes shell commands inside the container.

**Constants:**
- `MAX_OUTPUT_BYTES = 500KB` — output truncation ceiling
- `MAX_CONCURRENT = 5` — concurrent subprocess limit

**Tool: `run_command(command: str) → str`**
- Concurrency gate: `threading.Semaphore(5)`, non-blocking acquire. Returns
  busy error if limit reached (audit-logged as `error: "busy"`)
- Runs `command` via `subprocess.run(shell=True)`
- Captures combined stdout + stderr as text
- Hard timeout: 240 seconds
- Truncates output at 500 KB, appends `[OUTPUT TRUNCATED]` marker
- Returns formatted string: `Exit code: N\n\n<output>`
- Every invocation is audit-logged (command text, exit code, output length, truncation flag)

**Transport:** FastMCP SSE on `0.0.0.0:8083`

### 2.2 `memory_server.py`

Optional single-tool MCP server for semantic search over curated Q&A pairs
stored in Qdrant.

**Configuration (env vars):**
- `QDRANT_HOST` (default: `h-cli-qdrant`)
- `QDRANT_PORT` (default: `6333`, falls back to default on invalid value with warning)
- `QDRANT_API_KEY` (required — server won't start without it)

**Constants:**
- `COLLECTION = "hcli_memory"`
- `EMBEDDING_MODEL = "all-MiniLM-L6-v2"` (384-dimensional vectors)

**Initialization (`_init()`):**
1. Connects to Qdrant with API key auth
2. Creates `hcli_memory` collection if absent (cosine distance, 384 dims)
3. Loads fastembed `all-MiniLM-L6-v2` model into memory
4. On any failure: closes Qdrant client if partially created, resets both
   globals to `None`, and re-raises — no dangling connections

**Tool: `memory_search(query: str, limit: int = 5) → str`**
- Embeds `query` with fastembed
- Searches Qdrant for top-k nearest vectors (limit clamped to 1–20)
- Returns markdown-formatted results with score, question, answer, source
- Returns generic error string on failure (never raises, never leaks internals)

**Transport:** FastMCP SSE on `0.0.0.0:8084`

### 2.3 `entrypoint.sh`

Bash init script that runs as root before dropping to `hcli` (uid 1000).
Executes four sequential phases:

```
Phase 1: Log directories
  └─ mkdir -p $LOG_DIR/core, $LOG_DIR/memory
  └─ chown to hcli

Phase 2: SSH key setup
  └─ If /tmp/ssh-keys-staging has files:
     ├─ Copy to /home/hcli/.ssh/ (read-only mount → writable)
     ├─ Set permissions: 600 private keys, 644 pub/config/known_hosts
     ├─ Remove .gitkeep / README.md artifacts
     └─ Create default SSH config if none provided
  └─ Otherwise: skip

Phase 3: Sudo whitelist
  └─ If SUDO_COMMANDS env var set:
     ├─ Split on comma, resolve each to absolute path via `command -v`
     ├─ Write /etc/sudoers.d/hcli with NOPASSWD for resolved paths
     └─ Skip unresolvable commands with WARNING
  └─ Otherwise: remove sudoers file, sudo disabled

Phase 4: Memory server (supervised background)
  └─ Always start memory_server.py in background as hcli (via gosu)
     └─ Trap SIGTERM/SIGINT to forward to memory server PID
     └─ Monitor subshell logs unexpected exits
  └─ memory_server.py handles Qdrant unavailability gracefully
     (_init failure caught → tool returns "not initialized")

Final: exec gosu hcli "$@"  →  drops to hcli, runs CMD (mcp_server.py)
```

### 2.4 `Dockerfile`

Multi-stage build on `debian:12-slim` (configurable via `CORE_BASE_IMAGE` ARG).

**Layer order:**
1. System packages — network tools (nmap, dig, traceroute, mtr, tcpdump, whois,
   curl, wget, ssh, ping, iproute2, netcat, telnet, net-tools)
2. User creation — `hcli` (uid 1000, home `/home/hcli`)
3. Playwright + Chromium — for `browser_interact` capability
4. Shared library — `hcli_logging` installed from `shared/` build context
5. App dependencies — `requirements.txt` via pip
6. App code — `mcp_server.py`, `memory_server.py`
7. Entrypoint — `/entrypoint.sh`

**Exposed ports:** 8083 (MCP), 8084 (memory)

### 2.5 `requirements.txt`

| Package | Version | Used By |
|---------|---------|---------|
| `mcp[cli]` | >=1.26,<2 | FastMCP framework (both servers) |
| `fastembed` | >=0.5,<1 | Local embedding model (memory server) |
| `qdrant-client` | >=1.12,<2 | Vector DB client (memory server) |

Additionally, `hcli_logging` is installed from `shared/` at build time (not in
requirements.txt).

---

## 3. Position in System Flow

Core is **Step 7** in the 9-step message lifecycle (see Architect LLD section 3).
It does not see user messages, session history, or task metadata. It receives
only pre-filtered MCP tool calls and returns plain-text results.

### 3.1 End-to-end context

```
User ──► Telegram ──► telegram-bot ──► Redis ──► dispatcher ──► Claude Code CLI
                                                                      │
                                                              Claude decides to
                                                              call run_command
                                                                      │
                                                                      ▼
                                                               ┌─────────────┐
                                                               │  firewall   │
                                                               │  (Asimov)   │
                                                               │             │
                                                               │ Layer 1:    │
                                                               │  Pattern    │
                                                               │  denylist   │
                                                               │  (~80 rules)│
                                                               │             │
                                                               │ Layer 2:    │
                                                               │  Haiku gate │
                                                               │  (stateless │
                                                               │   LLM check)│
                                                               └──────┬──────┘
                                                                      │
                                                              Both layers pass
                                                                      │
                                                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ CORE (Step 7) — h-network-backend only                                      │
│                                                                             │
│   :8083 ◄── run_command(command)     :8084 ◄── memory_search(query, limit) │
│         ──► "Exit code: N\n\n..."           ──► "### Result 1 (score: ...)" │
└─────────────────────────────────────────────────────────────────────────────┘
                        │                                     │
                        ▼                                     ▼
                   Target hosts (SSH)                    Qdrant (HTTP)
```

After core returns, the result flows back through Claude Code CLI → dispatcher
→ HMAC signing → Redis → telegram-bot → Telegram → user (Steps 8-9).

### 3.2 What we receive

| Tool | Caller | Via | What arrives |
|------|--------|-----|--------------|
| `run_command` | Claude Code CLI | firewall.py → SSE :8083 | Single `command` string, already cleared by pattern denylist + Haiku gate |
| `memory_search` | Claude Code CLI | memory_proxy.py → SSE :8084 | `query` string + optional `limit` int |

**We never see:** task_id, chat_id, user_id, session history, original user
message, HMAC keys, Redis state, or any task metadata. The firewall strips
all context — we receive only the tool arguments.

### 3.3 What we hand back

| Tool | Return format | Consumed by |
|------|---------------|-------------|
| `run_command` | `"Exit code: N\n\n{stdout+stderr}"` | Claude Code CLI (feeds into LLM reasoning) |
| `memory_search` | Markdown-formatted results with scores | Claude Code CLI (feeds into LLM reasoning) |

Our return values become part of Claude's tool_result. Claude uses them to
formulate a response, which the dispatcher then captures, HMAC-signs, and
stores in Redis for telegram-bot to deliver.

### 3.4 Contracts we depend on

| Contract | Owner | What we trust |
|----------|-------|---------------|
| Command pre-filtering | Orchestration (firewall.py) | Commands reaching us have passed the pattern denylist and Haiku gate — we execute without re-filtering |
| Network isolation | Architect (docker-compose.yml) | Only claude-code can reach :8083/:8084 via h-network-backend — no direct access from telegram-bot or external |
| SSH keys | Architect (install.sh) | Valid ed25519 keypair mounted read-only at /tmp/ssh-keys-staging |
| Qdrant availability | Architect (docker-compose.yml) | When vectordb profile enabled, Qdrant is healthy on h-network-backend :6333 |
| Shared logging | Architect (shared/hcli_logging) | Log handlers write to $LOG_DIR/{service}/ with rotation |
| Timeout cascade | Architect (docs/SECURITY-HARDENING.md item 23) | Core timeout (240s) < dispatcher timeout (280s) < telegram timeout (300s) — our timeout fires first |

### 3.5 Contracts we provide

| Contract | Consumer | What we guarantee |
|----------|----------|-------------------|
| MCP SSE on :8083 | Orchestration (firewall.py) | `run_command` accepts any string, returns exit code + output, never raises |
| MCP SSE on :8084 | Orchestration (memory_proxy.py) | `memory_search` accepts query + limit, returns markdown or error string, never raises |
| Output truncation | Orchestration | Output capped at 500KB — consumer will never receive unbounded data |
| Audit trail | Operations | Every command logged: command_exec (before), command_result (after, including timeouts and errors) |
| Healthcheck | Docker | SSE endpoint on :8083 responds within 2s |

---

## 4. Architecture

```
                        Orchestration container (llm/claude-code)
                        ┌─────────────────────────────────────┐
                        │ firewall.py ──► pattern denylist     │
                        │              ──► Haiku gate          │
                        │              ──► SSE forward ────────┼──── :8083 ──┐
                        │                                      │             │
                        │ memory_proxy.py ─────────────────────┼──── :8084 ──┤
                        └─────────────────────────────────────┘             │
                                                                            │
                  ┌─────────────────────────────────────────────────────────┘
                  │
                  ▼
                  ┌─────────────────────────────────────────────┐
                  │           Core Container (Debian)           │
                  │           h-network-backend only             │
                  │                                             │
                  │  ┌──────────────┐    ┌──────────────────┐  │
                  │  │ mcp_server   │    │ memory_server     │  │
                  │  │ :8083        │    │ :8084             │  │
                  │  │              │    │                   │  │
                  │  │ run_command  │    │ memory_search     │  │
                  │  │   │         │    │   │               │  │
                  │  │   ▼         │    │   ▼               │  │
                  │  │ subprocess  │    │ fastembed + qdrant │──── HTTP ──► Qdrant
                  │  │ (shell=True)│    │                   │  │
                  │  └──────────────┘    └──────────────────┘  │
                  │                                             │
                  │  User: hcli (uid 1000)                     │
                  │  SSH keys: /home/hcli/.ssh/                │
                  │  Sudo: whitelisted commands only            │
                  │  Caps: NET_RAW, NET_ADMIN                  │
                  └─────────────────────────────────────────────┘
                         │
                         │ SSH
                         ▼
                    Target hosts
```

---

## 5. Data Flow

### 5.1 Command Execution (Step 7 of system flow)

```
firewall.py (Orchestration) ──► SSE /sse (port 8083)
  │                                │
  │ Command already passed:        │
  │  ✓ Pattern denylist (~80)      │
  │  ✓ Haiku gate (stateless)      │
  │                                ▼
  │                         FastMCP routes to run_command(command)
  │                                │
  │                                ├─ Semaphore gate (max 5 concurrent)
  │                                │  └─ If full → audit "busy", return error
  │                                │
  │                                ├─ audit.info("command_exec", command=...)
  │                                │
  │                                ▼
  │                         subprocess.run(command, shell=True, timeout=240)
  │                                │
  │                                ├─ stdout + stderr combined
  │                                ├─ original_length captured
  │                                ├─ Truncated if > 500KB
  │                                │
  │                                ▼
  │                         audit.info("command_result",
  │                           exit_code, output_length, truncated)
  │                                │
  │                                ▼
  │                         Return "Exit code: N\n\n<output>"
  │                                │
  ◄────────────────────────────────┘
  │
  ▼ Result enters Claude's context → formulates response → dispatcher
```

### 5.2 Memory Search

```
memory_proxy.py (Orchestration) ──► SSE /sse (port 8084)
                                       │
                                       ▼
                                FastMCP routes to memory_search(query, limit)
                                       │
                                       ▼
                                fastembed.embed(query) → 384-dim vector
                                       │
                                       ▼
                                qdrant.search(collection="hcli_memory", vector, limit)
                                       │
                                       ▼
                                Format results as markdown (score, Q, A, source)
                                       │
                                       ▼
                                Return formatted string
```

### 5.3 Container Startup

```
docker start
  │
  ▼
entrypoint.sh (runs as root)
  │
  ├─ Create /var/log/hcli/{core,memory}
  ├─ Copy SSH keys from read-only staging mount
  ├─ Build /etc/sudoers.d/hcli from SUDO_COMMANDS
  ├─ Set up SIGTERM/SIGINT trap for memory server cleanup
  ├─ Healthcheck Qdrant → start memory_server.py (supervised background, as hcli)
  │
  ▼
exec gosu hcli python3 -u mcp_server.py  (PID 1, as hcli)
```

---

## 6. Interfaces

### 6.1 Inbound (we serve)

| Endpoint | Port | Transport | Caller | Via | Tool |
|----------|------|-----------|--------|-----|------|
| `/sse` | 8083 | MCP over SSE | Claude Code CLI | firewall.py (Orchestration) | `run_command` |
| `/sse` | 8084 | MCP over SSE | Claude Code CLI | memory_proxy.py (Orchestration) | `memory_search` |

### 6.2 Outbound (we call)

| Target | Protocol | Purpose | Config |
|--------|----------|---------|--------|
| Qdrant | HTTP :6333 | Vector search | `QDRANT_HOST`, `QDRANT_PORT`, `QDRANT_API_KEY` |
| Target hosts | SSH | Remote command execution | Keys in `/home/hcli/.ssh/` |

### 6.3 Environment Variables

| Variable | Required | Default | Used By |
|----------|----------|---------|---------|
| `QDRANT_HOST` | For memory | `h-cli-qdrant` | `memory_server.py` |
| `QDRANT_PORT` | For memory | `6333` | `memory_server.py` |
| `QDRANT_API_KEY` | For memory | — | `memory_server.py`, `entrypoint.sh` |
| `SUDO_COMMANDS` | No | — | `entrypoint.sh` |
| `LOG_DIR` | No | `/var/log/hcli` | `entrypoint.sh` |
| `LOG_LEVEL` | No | `INFO` | `hcli_logging` |
| `MCP_SERVER_PORT` | No | `8083` | docker-compose (informational) |
| `NETBOX_URL` | No | — | Available in shell env |
| `NETBOX_API_TOKEN` | No | — | Available in shell env |
| `GRAFANA_URL` | No | — | Available in shell env |
| `GRAFANA_API_TOKEN` | No | — | Available in shell env |
| `EVE_NG_URL` | No | — | Available in shell env |
| `EVE_NG_USERNAME` | No | — | Available in shell env |
| `EVE_NG_PASSWORD` | No | — | Available in shell env |
| `LAMBDAAPI` | No | — | Available in shell env |
| `TIMESCALE_URL` | No | — | Available in shell env |

Service API credentials (NetBox, Grafana, EVE-NG, Lambda, TimescaleDB) are
injected into the container environment so that commands executed via
`run_command` can access them — the Python code itself does not read them.

---

## 7. Internal Design Decisions

### 7.1 Single-tool-per-server pattern
Each MCP server exposes exactly one tool. This keeps the servers minimal and
independently restartable. The command server is the primary process (PID 1);
the memory server is a conditional background process.

### 7.2 Shell execution via `shell=True`
`run_command` uses `shell=True` deliberately — the tool's purpose is to provide
full shell access (pipes, redirects, env vars) to the orchestration layer.
Security is enforced externally by the Asimov firewall's two layers (pattern
denylist + Haiku gate) in the Orchestration container. Core does not
re-validate — that would duplicate the enforcement point and create conflicting
rulesets.

### 7.3 Output truncation
500 KB hard cap prevents oversized tool responses from exhausting LLM context.
Original output length is captured before truncation and preserved in the
audit log. Truncation is flagged in both the return value and audit entry.

### 7.4 Lazy initialization for memory server
Qdrant client and embedding model are initialized once at startup via `_init()`,
not per-request. The globals `_qdrant` and `_embedder` are `None` until init
succeeds, providing a clean "not initialized" error path. If init fails
partway (e.g. embedder download fails after Qdrant connects), the cleanup
handler closes the Qdrant client and resets both globals to `None` — no
dangling connections.

### 7.5 Unconditional memory server startup
`entrypoint.sh` always starts `memory_server.py` in the background.
`memory_server.py` handles Qdrant unavailability gracefully: if `_init()`
fails (missing API key, unreachable Qdrant, model download error), the
exception is caught and the MCP server still starts on :8084. The
`memory_search` tool returns "not initialized" until Qdrant is available.
This ensures port 8084 is always listening, preventing "can't reach" errors
in the orchestration layer.

### 7.6 SSH key staging pattern
SSH keys are mounted read-only at `/tmp/ssh-keys-staging` and copied to
`/home/hcli/.ssh/` at startup. This avoids permission issues from Docker
bind mounts while keeping the source keys immutable.

### 7.7 Sudo whitelist approach
Rather than granting blanket root access, `entrypoint.sh` dynamically builds a
sudoers file from the `SUDO_COMMANDS` env var. Each command is resolved to its
absolute path via `command -v`. Dangerous argument patterns are blocked
separately by the Asimov firewall's denylist (see section 3.4).

### 7.8 Process model
- **PID 1**: `mcp_server.py` (via `exec gosu hcli`)
- **Background**: `memory_server.py` (via `gosu hcli ... &`, supervised)

If the command server crashes, the container restarts (PID 1 exit). The memory
server is supervised: a trap forwards SIGTERM/SIGINT to the background PID on
shutdown, and a monitor subshell logs unexpected exits.

### 7.9 Logging strategy
Both servers use the shared `hcli_logging` library which writes JSON-lines to:
- `$LOG_DIR/core/app.log` — general application logs (DEBUG+)
- `$LOG_DIR/core/error.log` — warnings and errors only (WARNING+)
- `$LOG_DIR/core/audit.log` — structured audit trail (command, exit code, etc.)

Logs are rotated at 10 MB with 5 backup files.

### 7.10 Concurrency limit
`run_command` is gated by a `threading.Semaphore(5)`. Non-blocking acquire
rejects excess requests immediately with an audit-logged busy error. The
semaphore is released in a `finally` block to prevent leaks on any exit path
(normal, timeout, exception). This prevents fork-bombing the container via
retry storms or orchestration bugs.

---

## 8. Security Model

| Layer | Mechanism | Scope |
|-------|-----------|-------|
| Pre-filtering (external) | Asimov firewall: pattern denylist + Haiku gate | Commands vetted before reaching core |
| Network isolation | Docker network `h-network-backend` | Only claude-code can reach us |
| User isolation | Runs as `hcli` (uid 1000), not root | All application code |
| Sudo whitelist | `/etc/sudoers.d/hcli` with NOPASSWD for specific binaries | Network tools only |
| Capabilities | `NET_RAW`, `NET_ADMIN` only | Required for nmap, tcpdump |
| SSH keys | Read-only mount, copied at init, proper permissions | Target host access |
| Qdrant auth | API key required | Vector DB access |

Core does **not** filter commands itself. The Asimov firewall in the
Orchestration container is the enforcement point — two layers (deterministic
pattern match + independent LLM evaluation) must both pass before a command
reaches core. This is by design: a single enforcement point avoids conflicting
rulesets and makes the security boundary auditable in one place.

---

## 9. Healthcheck

```
curl -sf --max-time 2 http://localhost:8083/sse >/dev/null 2>&1
```

Checks that the FastMCP SSE endpoint on port 8083 is accepting connections.
Exit codes 0 (success) and 28 (timeout after connection, normal for SSE) are
both treated as healthy.

- **Interval:** 30s
- **Timeout:** 5s
- **Retries:** 3
