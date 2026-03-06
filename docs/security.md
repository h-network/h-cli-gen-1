# Security

45 security items implemented. Full audit trail: [SECURITY-HARDENING.md](SECURITY-HARDENING.md)

## Ground Rules: A TCP/IP Model for AI Safety

The safety rules in [groundRules.md](../groundRules.md) are structured as a **layered protocol stack** — the same encapsulation model that governs TCP/IP and the OSI reference model, applied to AI agent governance.

```
┌─────────────────────────────────────────────────┐
│  Layer 4 — Behavioral Directives                │  Honesty, brevity, explain denials
│  (cannot override any lower layer)              │
├─────────────────────────────────────────────────┤
│  Layer 3 — Operational Scope                    │  Infrastructure only, no impersonation
│  (cannot override Layer 1 or 2)                 │
├─────────────────────────────────────────────────┤
│  Layer 2 — Security                             │  No credential leaks, no self-access,
│  (cannot override Layer 1)                      │  no exfiltration, no escalation
├─────────────────────────────────────────────────┤
│  Layer 1 — Base Laws (Asimov-inspired)          │  Protect infra, obey operator,
│  (sacred, immutable)                            │  preserve self, stay in boundaries
└─────────────────────────────────────────────────┘
```

**The insight:** In networking, the physical layer cannot be violated from the application layer. You can write whatever HTTP headers you want — the electrons on the wire don't care. The same principle applies here: a Layer 4 directive (be helpful) cannot override a Layer 1 law (don't destroy infrastructure). No amount of conversational pressure at the application layer can change the physics.

**Why this matters for AI safety:**

Most AI safety frameworks use flat rule lists — "don't do X, don't do Y." Flat lists have no conflict resolution mechanism. When rule 3 says "be helpful" and rule 7 says "don't run dangerous commands," which wins? The answer depends on the model's interpretation, which is non-deterministic.

The layered model eliminates ambiguity: **lower layers always win.** If a user asks the agent to do something helpful (Layer 4) that requires privilege escalation (violates Layer 2), the answer is always no. There's no judgment call, no weighing of trade-offs. The layer hierarchy is the conflict resolution mechanism.

**The two enforcement points:**

The ground rules are loaded into two independent systems:

1. **The main model's system prompt** — behavioral guidance. The agent can discuss the rules, reference them, reason about them. But testing proved it will not reliably enforce them on its own. (See [test case: gate vs prompt enforcement](test-cases/gate-vs-prompt-enforcement.md))

2. **Haiku's gate prompt** — actual enforcement. Stateless, no conversation context, no memory. Sees only the ground rules and the raw command. The layered structure gives the gate a clear decision framework: identify which layer the command touches, check if it violates that layer or any layer below it.

The system prompt is documentation. The gate is enforcement. The layered model makes enforcement deterministic.

## Highlights

- **Asimov firewall**: MCP proxy between Claude and core. Two layers: deterministic pattern denylist (always active, zero latency) + independent Haiku gate check (on by default, resistant to conversational prompt injection)
- **Network isolation**: Two Docker networks — `h-network-frontend` and `h-network-backend`. Redis bridges both (designated message bus). Claude-code and Grafana are on both. Core is backend-only. Telegram-bot is frontend-only.
- **Fail-closed auth**: `ALLOWED_CHATS` allowlist — empty = nobody gets in
- **Non-root**: All containers run as `hcli` (uid 1000), not root
- **Capabilities**: `NET_RAW`/`NET_ADMIN` on core only; `cap_drop: ALL` + `no-new-privileges` on telegram-bot and claude-code; `read_only` rootfs on telegram-bot
- **Sudo whitelist**: only commands in `SUDO_COMMANDS` are allowed via sudo (resolved to full paths, fail-closed)
- **HMAC-signed results**: Dispatcher signs, telegram-bot verifies. Prevents Redis result spoofing.
- **Redis auth**: password-protected, 2GB memory cap, LRU eviction, RDB + AOF persistence
- **Session chunking**: Auto-rotate at 100KB. Two injection budgets: 30KB Redis history (prepended to user message) + 50KB session chunks (injected into system prompt)
- **Tool restriction**: Claude Code restricted to `mcp__h-cli-core__run_command` and `mcp__h-cli-memory__memory_search` only
- **Pinned deps**: all Python packages pinned to major version ranges, base images pinned

## Container Privileges

| Container | User | Capabilities | Rootfs | Networks |
|-----------|------|-------------|--------|----------|
| `telegram-bot` | `hcli` (1000) | None (`cap_drop: ALL`) | Read-only | frontend only |
| `redis` | `redis` (default) | Default | Writable | frontend + backend |
| `claude-code` | `hcli` (1000) | None (`cap_drop: ALL`) | Writable | frontend + backend |
| `core` | `hcli` (1000) | `NET_RAW`, `NET_ADMIN` | Writable | backend only |
| `grafana` | `grafana` (default) | Default | Writable | frontend + backend |
| `timescaledb` | `postgres` (default) | Default | Writable | backend only |
| `grafana-renderer` | `node` (default) | Default | Writable | backend only |
| `qdrant` | `qdrant` (default) | Default | Writable | backend only |
| `web-ui` | `hcli` (1000) | None (`cap_drop: ALL`) | Read-only | frontend only |
| `discord-bot` | `hcli` (1000) | None (`cap_drop: ALL`) | Read-only | frontend only |
| `slack-bot` | `hcli` (1000) | None (`cap_drop: ALL`) | Read-only | frontend only |
| `cve-check` | `hcli` (1000) | None (`cap_drop: ALL`) | Writable | frontend only |

## Data Access

| Container | Redis | Filesystem writes | Secrets it holds |
|-----------|-------|-------------------|------------------|
| `telegram-bot` | Read/write (task queue + results) | Logs only | `TELEGRAM_BOT_TOKEN`, `REDIS_PASSWORD`, `RESULT_HMAC_KEY`, `GRAFANA_ADMIN_PASSWORD`, `GRAFANA_API_TOKEN` |
| `redis` | N/A (is the store) | `/data` (RDB + AOF) | `REDIS_PASSWORD` |
| `claude-code` | Read/write (tasks, sessions, memory) | Logs, session chunks, `~/.claude/` | `REDIS_PASSWORD`, `RESULT_HMAC_KEY`, Claude credentials (volume) |
| `core` | Write (PUBLISH audit events) | Logs only | SSH keys (copied at startup), integration tokens (NetBox, Grafana, EVE-NG), `QDRANT_API_KEY` |

## Sudo Whitelist (core only)

Commands in `SUDO_COMMANDS` are resolved to full paths at startup. Default:

```
nmap, tcpdump, traceroute, mtr, ping, ss, ip, iptables
```

Everything else is denied. Fail-closed — if a command isn't in the list, sudo refuses it.

## Optional Integrations

| Integration | Container | Access | Required scope |
|-------------|-----------|--------|----------------|
| NetBox | `core` | REST API (read) | Read-only API token recommended |
| Grafana | `core`, `telegram-bot` | REST API (read, render) | Viewer role token recommended |
| EVE-NG | `core` | REST API (read/write) | Lab user credentials |

Core holds NetBox and EVE-NG tokens exclusively. Grafana tokens are shared with telegram-bot for inline graph rendering (`[action:graph:URL]`).
