# Architecture

Twelve services across two isolated Docker networks, with optional profiles for monitoring, vector search, security scanning, and additional interfaces (web, Discord, Slack).

```
  +-----------+
  | Telegram  |---+
  +-----------+   |
  +-----------+   |      +----------------------------------------------------------+
  |   Slack   |---+      |                                                          |
  +-----------+   +----> |  +-------------+    +-------+    +--------------+        |
  +-----------+   | <--- |  | interface   | -> | Redis | -> | claude-code  |        |
  |  Discord  |---+      |  | bot         | <- |       | <- | (dispatcher) |        |
  +-----------+   |      |  +-------------+    +-------+    +------+-------+        |
  +-----------+   |      |   frontend only       bridges     |  both networks |
  |  Web UI   |---+      |                            claude -p (MCP)         |
  +-----------+           |               session context           |           |
                          |               (plain text inject)  +-----+------+    |
                          |                                  | firewall   |    |
                          |                                  | (MCP proxy)|    |
                          |                                  +-----+------+    |
                          |                                        |           |
                          |                                  +-----+------+    |
                          |                                  |    core    |    |
                          |                                  |  MCP server|    |
                          |                                  +------------+    |
                          |                                backend only        |
                          +----------------------------------------------------------+
```

**Flow**: User sends message via any interface (Telegram, Slack, Discord, or Web UI) → interface bot queues to Redis → concurrent dispatcher invokes Claude Code with session context → Claude calls `run_command()` → Asimov firewall checks the command (pattern denylist + independent LLM gate) → core executes → result signed with HMAC → delivered back to the originating interface.

Session history is stored in Redis lists. Session chunks are written as plain text files. JSONL files are written automatically by Claude Code CLI as an audit trail but are never replayed into the context window.

## Context Injection

Each `claude -p` invocation starts with a fresh session. Conversation continuity is maintained by injecting context as plain text, not by replaying JSONL sessions:

1. **Redis session history** (< 24h): Recent turns stored in Redis, formatted as plain text (`[HH:MM] ROLE: content`), prepended to the user's message (30KB cap).
2. **Session chunks** (> 24h): When accumulated size exceeds 100KB, the dispatcher dumps history to text files on disk. Up to 50KB of recent chunks are injected into the system prompt.
3. **Skills** (per-message): Matched skill files from `skills/` injected into the system prompt (20KB budget).
4. **Vector memory** (permanent, optional): Curated Q&A pairs in Qdrant, searchable via `memory_search` tool.

This approach uses [71% fewer tokens than JSONL session replay](context-injection.md) for the same conversation.

## Network Topology

Two Docker networks segment services. No ports exposed to host except Grafana.

```
Frontend only:  telegram-bot, CVE checker
Backend only:   Core, TimescaleDB, Qdrant, Grafana-renderer
Both networks:  Redis, Orchestration/LLM (claude-code), Grafana
```

- **Redis** bridges both networks as the designated message bus
- **Core** is backend-only — serves MCP (SSE) and reaches Redis via backend
- **telegram-bot** is frontend-only — communicates via Redis only, never calls MCP directly
- **claude-code** is on both networks: frontend for Redis, backend for MCP calls to Core

## Project Structure

```
h-cli/
├── docker-compose.yml          # Service definitions (two networks: frontend/backend)
├── install.sh                  # First-run setup
├── setup.sh                    # Environment setup
├── backup.sh                   # Backup automation
├── .env.template               # Configuration template
├── README.md
│
├── interfaces/                 # User-facing frontends
│   ├── telegram-bot/           # Telegram bot plugin (profile: telegram)
│   │   ├── Dockerfile
│   │   ├── bot.py              # /start, /help, /new, /run, /cancel, /abort, /status, /stats
│   │   ├── entrypoint.sh
│   │   └── requirements.txt
│   ├── web/                    # Web UI plugin (profile: web)
│   ├── discord-bot/            # Discord bot plugin (profile: discord)
│   └── slack-bot/              # Slack bot plugin (profile: slack)
│
├── orchestration/              # Task tracker and dispatcher
│   ├── bus.py                  # Redis task lifecycle, state machine, HMAC signing
│   ├── worker.py               # Claude invocation, context injection, skills, session chunking
│   └── dispatcher.py           # Thin main loop (BLPOP → hand off → signal)
│
├── llm/                        # AI framework plugins
│   ├── groundRules.md          # 4-layer safety rules (Asimov-inspired)
│   ├── blocked-patterns.txt    # Pattern denylist (~80 patterns, 12 categories)
│   ├── context.md.template     # Example context — copy to context.md
│   └── claude-code/            # Claude Code plugin
│       ├── Dockerfile          # Ubuntu + Node.js + Claude Code CLI + Python
│       ├── firewall.py         # Asimov firewall — MCP proxy with pattern denylist + Haiku gate
│       ├── mcp-config.json     # MCP server config (points to firewall proxy, not core directly)
│       ├── CLAUDE.md           # Bot persona — tool restrictions + memory search
│       └── entrypoint.sh
│
├── core/                       # Core service (tools + MCP server)
│   ├── Dockerfile
│   ├── mcp_server.py           # FastMCP SSE server exposing run_command tool (:8083)
│   ├── memory_server.py        # Qdrant vector search via memory_search tool (:8084)
│   ├── entrypoint.sh           # SSH key setup, sudo whitelist, log dir creation
│   └── requirements.txt
│
├── monitor/                    # Observability (profile: monitor)
│   ├── init.sql                # TimescaleDB schema
│   ├── datasource.yml          # Grafana datasource provisioning
│   ├── dashboard.yml           # Grafana dashboard provisioning
│   └── dashboards/             # Grafana dashboard JSON files
│
├── security/                   # Security tooling
│   └── cve-check/              # CVE scanner (profile: tools)
│       └── Dockerfile
│
├── hssh_llm/                   # h-ssh integration (multi-transport SSH tool)
│   ├── h-ssh/                  # CLI tool (transports: SSH, telnet, REST, generic)
│   ├── skills/                 # LLM skill files (show, troubleshoot, edit)
│   └── tests/
│
├── skills/                     # Injected skill files (public + private)
├── shared/                     # Cross-module libraries (structured JSON logging)
├── docs/                       # Documentation and test cases
├── logs/                       # Log output (bind-mounted into containers)
├── ssh-keys/                   # SSH keys mounted into core (gitignored)
└── data/                       # Persistent data (Redis, TimescaleDB, Grafana, Qdrant)
```
