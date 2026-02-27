# h-cli — High-Level Design

## 1. What It Is

h-cli is an AI-powered engineering assistant accessed via messaging interfaces. Users send natural language requests, the system routes them through a security firewall, executes commands on infrastructure, and returns results.

## 2. System Architecture

```
┌──────────┐    ┌────────────┐    ┌───────┐    ┌───────────────┐    ┌──────────┐    ┌──────┐
│ User     │───►│ Interface  │───►│ Redis │───►│ Orchestration │───►│ Firewall │───►│ Core │
│          │◄───│            │◄───│       │◄───│               │◄───│          │◄───│      │
└──────────┘    └────────────┘    └───────┘    └───────────────┘    └──────────┘    └──────┘
                                  task tracker    dispatcher          MCP (direct)
```

### Message Flow

1. **User → Interface**: Message arrives via frontend (Telegram, future: web, CLI)
2. **Interface → Redis**: Task queued as JSON (`hcli:tasks`), state set to `queued`
3. **Redis → Orchestration**: Dispatcher picks up task via BLPOP, state set to `running`
4. **Orchestration → LLM Plugin**: Context injection (session history, chunks, vector memory), invokes AI model
5. **LLM Plugin → Firewall**: Tool calls pass through two-layer security (pattern denylist + LLM gate)
6. **Firewall → Core**: Approved commands execute via MCP (direct SSE, standard protocol)
7. **Core → Redis**: Audit events published (fire-and-forget)
8. **Orchestration → Redis → Interface → User**: HMAC-signed result delivered, state set to `completed`

### Redis Role

Redis is the **task tracker** — it manages task lifecycle, delivers results, carries audit events, and stores sessions. It does NOT carry MCP tool calls. MCP stays standard and direct between the firewall proxy and Core.

**What goes through Redis**: task queue, task state, control channels (abort/cancel), result delivery, state notifications, audit events, sessions, stats.

**What goes direct**: MCP tool calls (firewall → Core SSE on :8083), memory search (memory proxy → Core SSE on :8084).

## 3. Modules

| Module | Directory | Responsibility |
|--------|-----------|---------------|
| **Interfaces** | `interfaces/` | User-facing frontends. Each plugin (e.g. `telegram-bot/`) is self-contained. |
| **Orchestration** | `orchestration/` | Task tracker and dispatcher. Redis task lifecycle, state machine, control channels, HMAC signing, context injection, session management. |
| **LLM** | `llm/` | AI framework plugins. Each plugin (e.g. `claude-code/`) owns its firewall, memory proxy, and config. |
| **Core** | `core/` | Command execution (MCP), vector memory search, infrastructure access. |
| **Monitor** | `monitor/` | Observability. Grafana dashboards, TimescaleDB schema, trace export. |
| **Security** | `security/` | CVE scanning, pattern analysis. |
| **h-ssh** | `hssh_llm/` | SSH infrastructure integration. Multi-transport CLI tool (Arista, JunOS, telnet, REST, generic) with network command templates and LLM skill files. |
| **Shared** | `shared/` | Cross-module libraries (logging). |
| **Skills** | `skills/` | Injected skill files (public + private). |

## 4. Network Topology

Two Docker networks segment services. No ports exposed to host except Grafana.

```
┌─── h-network-frontend ──────────────────────────────────────┐
│                                                               │
│   Interface (telegram-bot)  ◄──only──►  Redis ──bridge──┐     │
│                                                          │     │
│   CVE checker                     Orchestration/LLM      │     │
│                                   Grafana                │     │
└──────────────────────────────────────────────────────────│────┘
                                                           │
┌─── h-network-backend ───────────────────────────────────│────┐
│                                                          │     │
│   Orchestration/LLM (claude) ──MCP (SSE)──► Core   Redis ┘     │
│                                                               │
│   TimescaleDB    Qdrant    Grafana (:2405 exposed)            │
│   Grafana-renderer                                            │
└───────────────────────────────────────────────────────────────┘

Frontend only:  telegram-bot, CVE checker
Backend only:   Core, TimescaleDB, Qdrant, Grafana-renderer
Both networks:  Redis, Orchestration/LLM (claude), Grafana
```

**Key rules**:
- Two-network topology per security hardening (see `docs/SECURITY-HARDENING.md` items 3, F52).
- No service exposes ports to the host except Grafana (host :2405 → container :3000).
- Redis bridges both networks — it is the designated message bus between frontend and backend.
- Core is backend-only — serves MCP (SSE) and reaches Redis via backend. Never on frontend.
- Orchestration/LLM is on both networks: frontend for Redis (task lifecycle), backend for MCP calls to Core.
- telegram-bot is frontend-only — communicates via Redis only, never calls MCP directly.
- MCP stays standard: firewall proxy connects to Core via SSE directly.

## 5. Security Model

### Two-Layer Firewall

Every tool call passes through both layers before reaching Core:

1. **Pattern denylist** (deterministic, zero latency): ~80 blocked patterns across 12 categories. Case-insensitive substring match.
2. **LLM gate** (independent, stateless): Evaluates commands against ground rules. Has no conversation context — cannot be prompt-injected.

### Ground Rules (4-layer hierarchy)

```
Layer 1 — Base Laws (sacred)     : Protect infrastructure, obey operator, preserve self, stay bounded
Layer 2 — Security               : Credential protection, no self-access, no exfiltration, no escalation
Layer 3 — Operational Scope      : Engineering role, general queries, no impersonation
Layer 4 — Behavioral             : Honesty, brevity, graceful failure, explain denials
```

Lower layers cannot be overridden by higher.

### HMAC Result Signing

Results are HMAC-SHA256 signed by orchestration, verified by interface. Prevents Redis result spoofing.

## 6. Task Lifecycle

```
[*] → queued → running → completed
                      → failed
                      → timed_out
                      → aborted
    → cancelled
```

| State | Who sets it | What happens |
|-------|------------|--------------|
| `queued` | Interface | Task pushed to `hcli:tasks`, state key created |
| `running` | Orchestration | BLPOP picks it up, state updated |
| `completed` | Orchestration | Result written (HMAC-signed), state updated |
| `failed` | Orchestration | Error written, state updated |
| `timed_out` | Orchestration | Process killed, state updated |
| `aborted` | Interface → Orchestration | Interface publishes to `hcli:control:{id}`, orchestration kills process |
| `cancelled` | Interface | Task removed from queue (LREM) before orchestration picks it up |

Every state transition: `SET hcli:task:{id}:state` then `PUBLISH hcli:task:{id}:notify`.

## 7. Session & Memory Architecture

Three-tier memory system:

| Tier | Storage | TTL | Budget | Trigger |
|------|---------|-----|--------|---------|
| **Session history** | Redis list | < 24h | 30KB | Every message |
| **Session chunks** | Disk files | Permanent | 50KB | Size > 100KB or idle > 30min |
| **Vector memory** | Qdrant | Permanent | On-demand | MCP tool call |

Fresh session UUID per request — no JSONL replay. History injected as plain text into prompt.

## 8. Deployment

### Compose Profiles

| Profile | Services | Use Case |
|---------|----------|----------|
| default | Core, Redis, Interface, Orchestration/LLM | Minimum deployment |
| monitor | TimescaleDB, Grafana, Grafana Renderer | Metrics and dashboards |
| vectordb | Qdrant | Semantic memory search |
| tools | CVE checker | Security scanning |

### ENV_TAG Isolation

Container and network names include `ENV_TAG` for multi-instance coexistence on the same host (e.g., prod + dev).

## 9. Key Contracts

| Contract | Value | Between |
|----------|-------|---------|
| Task queue | `hcli:tasks` (Redis BLPOP) | Interface → Orchestration |
| Task state | `hcli:task:{id}:state` (SET + PUBLISH notify) | Orchestration → Interface |
| Control channel | `hcli:control:{id}` (PUBLISH abort/cancel) | Interface → Orchestration |
| Results | `hcli:results:{id}` (HMAC-signed, SET before notify) | Orchestration → Interface |
| Audit stream | `hcli:audit:{id}` (PUBLISH, fire-and-forget) | Core → Interface |
| MCP ports | 8083 (run_command), 8084 (memory_search) | Core ← LLM plugin (direct SSE) |
| HMAC format | `{task_id}:{output}:{completed_at}` SHA256 | Orchestration ↔ Interface |
| Timeout cascade | Interface 600s > Orchestration 600s > Gate 30s + Core 240s | All |
| Chat tasks | `hcli:chat:{chat_id}:tasks` (per-chat task list) | Interface (bot-owned) |

### Task JSON Schema

```json
{
  "task_id": "string (UUID, required)",
  "message": "string (required)",
  "chat_id": "int (required)",
  "user_id": "int (required)",
  "submitted_at": "string (ISO-8601, required)",
  "model": "string (optional, default 'opus', enum: opus/sonnet/haiku)"
}
```

## 10. Redis Key Namespace

| Pattern | Type | TTL | Owner |
|---------|------|-----|-------|
| `hcli:tasks` | List | none | Interface writes, Orchestration reads |
| `hcli:task:{id}:state` | String | 1h | Orchestration writes, Interface reads |
| `hcli:task:{id}:notify` | Channel | n/a | Orchestration publishes, Interface subscribes |
| `hcli:control:{id}` | Channel | n/a | Interface publishes, Orchestration subscribes |
| `hcli:audit:{id}` | Channel | n/a | Core publishes, Interface subscribes |
| `hcli:results:{id}` | String | 1h | Orchestration writes, Interface reads |
| `hcli:chat:{chat_id}:tasks` | List | 2x TASK_TIMEOUT | Interface (bot-owned) |
| `hcli:session:{chat}` | String | SESSION_TTL | Orchestration |
| `hcli:session_size:{chat}` | String | SESSION_TTL | Both |
| `hcli:session_history:{chat}` | List | SESSION_TTL | Both |
| `hcli:memory:{id}:{role}` | String | SESSION_TTL | Orchestration |
| `hcli:stats:{date}` | Hash | 48h | Both |
| `hcli:last_activity:{chat}` | String | SESSION_TTL | Orchestration |
| `hcli:teach:{chat}` | String | SESSION_TTL | Interface |

## 11. Module Boundaries

Each module is self-contained:

- **Interfaces**: Each frontend plugin has its own Dockerfile, LLD, and code. No shared code between frontends. Communicates with orchestration via Redis only.
- **LLM plugins**: Each framework has its own firewall, memory proxy, config, Dockerfile, and LLD. No shared code between plugins. Receives tasks from orchestration via Redis. Firewall connects to Core via MCP (direct SSE).
- **Orchestration**: Owns the dispatcher and Redis task lifecycle. Split into `bus.py` (Redis operations, state machine, HMAC), `worker.py` (Claude invocation, skills, sessions), and `dispatcher.py` (thin main loop). All three run inside the LLM container. Orchestration owns the code, LLM owns the container packaging.
- **Core**: Stateless command execution. Serves MCP endpoints (standard SSE). Publishes audit events to Redis (fire-and-forget). No knowledge of who's calling.
- **Monitor**: Read-only observability. Consumes audit logs and metrics. Never in the request path.
- **h-ssh**: SSH infrastructure integration. Self-contained CLI tool with multi-transport support, network command templates, and LLM skill files for natural language interaction.

## 12. Project Structure

```
/
├── docker-compose.yml       # Service definitions (two networks: frontend/backend)
├── install.sh               # First-run setup
├── setup.sh                 # Environment setup
├── backup.sh                # Backup automation
├── .env.template            # Configuration template
├── README.md                # Project documentation
│
├── interfaces/              # User-facing frontends
│   ├── HLD.md
│   └── telegram-bot/        # Telegram bot plugin
│
├── orchestration/           # Task tracker and dispatcher
│   ├── bus.py               # Redis task lifecycle, state machine, HMAC
│   ├── worker.py            # Claude invocation, skills, sessions
│   └── dispatcher.py        # Thin main loop (BLPOP, hand off, signal)
│
├── llm/                     # AI framework plugins
│   ├── HLD.md
│   ├── groundRules.md       # Security policy
│   ├── blocked-patterns.txt # Firewall denylist
│   └── claude-code/         # Claude Code plugin
│
├── core/                    # Command execution (MCP, standard SSE)
│
├── monitor/                 # Grafana, TimescaleDB, traces
│
├── security/                # CVE scanning
│   └── cve-check/
│
├── hssh_llm/                # h-ssh integration (multi-transport SSH tool)
│   ├── h-ssh/               # CLI tool (transports, commands, core)
│   ├── skills/              # LLM skill files (show, troubleshoot, edit)
│   └── tests/
│
├── shared/                  # Cross-module libraries (logging)
├── skills/                  # Injected skill files
├── docs/                    # Process documentation
└── ssh-keys/                # SSH keypair for targets
```
