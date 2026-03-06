# Low-Level Design — Claude Code Plugin (`llm/claude-code/`)

## 1. Overview

The Claude Code plugin provides the MCP security layer, memory proxy, and behavioral config for h-cli's Claude Code integration. It sits between the orchestration dispatcher (which spawns `claude -p`) and the Core container (which executes commands).

**Runtime**: Two on-demand MCP stdio servers spawned per Claude invocation — `firewall.py` and `memory_proxy.py`. The dispatcher that spawns them lives in `orchestration/` (see `orchestration/LLD.md`).

---

## 2. Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│  h-cli-claude container                                             │
│                                                                     │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  dispatcher.py  (orchestration/ — not this plugin)           │   │
│  │  Spawns `claude -p` with --mcp-config pointing to:           │   │
│  └──────────┬─────────────────────────────────────┬─────────────┘   │
│             │ stdio MCP                           │ stdio MCP       │
│  ┌──────────▼──────────────┐   ┌──────────────────▼──────────┐     │
│  │  firewall.py            │   │  memory_proxy.py            │     │
│  │  (MCP server: h-cli-core)│  │  (MCP server: h-cli-memory) │     │
│  │                         │   │                              │     │
│  │  Layer 1: Pattern deny  │   │  Forwards memory_search     │     │
│  │  Layer 2: Haiku gate    │   │  to core:8084 via SSE       │     │
│  │  Forward → core:8083    │   │                              │     │
│  └──────────┬──────────────┘   └──────────────┬───────────────┘     │
│             │ SSE (MCP client)                │ SSE (MCP client)    │
└─────────────┼─────────────────────────────────┼─────────────────────┘
              │                                 │
              ▼                                 ▼
     h-cli-core:8083                   h-cli-core:8084
     (run_command MCP)                 (memory_search MCP)
```

### 2.1 Contract with Orchestration

The dispatcher (in `orchestration/`) spawns `claude -p --mcp-config llm/claude-code/mcp-config.json`. This plugin provides:

- **MCP servers**: `firewall.py` (gated command execution) and `memory_proxy.py` (memory search)
- **Behavioral config**: `CLAUDE.md` (tone, tool usage rules, memory search instructions)
- **Security config**: `llm/groundRules.md` (gate context), `llm/blocked-patterns.txt` (pattern denylist)

**Contracts we depend on**:
| Contract | Provider | Mechanism |
|----------|----------|-----------|
| Core MCP (port 8083) | Core team | SSE endpoint for `run_command` |
| Core MCP (port 8084) | Core team | SSE endpoint for `memory_search` |
| `llm/groundRules.md` | Architect | Firewall gate context |
| `llm/blocked-patterns.txt` | Architect | Firewall pattern denylist |
| `TASK_ID` env var | Orchestration dispatcher | Injected into Claude subprocess env |

---

## 3. File Responsibilities

### 3.1 `firewall.py` — Asimov Firewall (MCP Proxy)

**Purpose**: Security gate between Claude and Core. Exposes `run_command` as an MCP tool over stdio, forwards approved commands to Core over SSE.

| Section | Lines | Responsibility |
|---------|-------|----------------|
| Pattern loading | 44–65 | Loads denylist from env var + file, one pattern per line |
| Ground rules loading | 68–75 | Reads `groundRules.md` at startup (fails hard if gate is enabled) |
| `_normalize_command()` | 174–185 | Lowercase, strip quotes, collapse whitespace |
| `_pattern_check()` | 188–194 | Substring match against all blocked patterns |
| `_gate_check()` | 197–273 | Spawns `claude -p` with Haiku model, passes only rules + command |
| `_forward_to_core()` | 276–300 | SSE MCP client → `core:8083` `run_command`, injects `TASK_ID` from env |
| `run_command()` | 303–351 | Main MCP tool: pattern check → gate check → forward |
| Metrics | 116–166 | Redis gate counters + TimescaleDB `tool_calls` table |

**Key design decisions**:
- **MCP server name is `h-cli-core`**: Deliberate. The firewall impersonates Core's tool namespace so the dispatcher's `--allowedTools mcp__h-cli-core__run_command` works without knowing the proxy exists. Claude sees one tool; the firewall is transparent.
- **task_id passthrough via env**: Dispatcher sets `TASK_ID` env var on the Claude subprocess. The firewall reads it from `os.environ` and injects it into every `run_command` call forwarded to Core.
- **Gate isolation**: The Haiku gate sees zero conversation context. Only `groundRules.md` + the raw command string. Resistant to conversational prompt injection.
- **Fail-closed**: Ambiguous gate responses default to DENY. Timeouts default to DENY. Gate errors default to DENY.
- **Gate env override**: `GATE_BASE_URL`, `GATE_AUTH_TOKEN`, `GATE_API_KEY` env vars can point the gate at a different Anthropic endpoint.
- **Pattern denylist is defense-in-depth**: Fast trip wire (~2ms) before the slower gate check (~2-3s).

### 3.2 `memory_proxy.py` — Memory Search Proxy

**Purpose**: Thin stdio-to-SSE bridge. No security gating — memory search is read-only.

| Section | Lines | Responsibility |
|---------|-------|----------------|
| `_forward_to_memory()` | 24–44 | SSE MCP client → `core:8084` `memory_search`, generic dict forwarding |
| `memory_search()` | 47–65 | MCP tool definition (query + collection + limit params) |

**Key design decisions**:
- **No gate**: Memory search is read-only semantic search. No destructive potential.
- **Separate MCP server name**: `h-cli-memory` for clean tool namespace separation.

### 3.3 `mcp-config.json` — MCP Tool Routing

Maps MCP server names to their stdio launch commands:

```json
{
  "h-cli-core":   "python3 /app/firewall.py",
  "h-cli-memory": "python3 /app/memory_proxy.py"
}
```

Claude Code spawns both as child processes when `--mcp-config` is passed.

### 3.4 `CLAUDE.md` — Bot Behavioral Instructions

Loaded by Claude Code as its project-level instruction file. Defines:
- Tone: brutally concise, no emoji, no hand-holding
- Output format: plain markdown only (Telegram bot converts to HTML)
- Tool usage: always use `run_command`, never modify config files
- Memory search: use `memory_search` before researching from scratch
- Skills: automatic injection, no action needed from the model

### 3.5 `Dockerfile` — Container Build

```
Base:       ubuntu:24.04
Runtime:    Node.js 22 (for Claude Code CLI), Python 3 (for MCP servers)
Claude CLI: @anthropic-ai/claude-code@2.1.39
Python deps: redis>=7.1, mcp>=1.26, psycopg2-binary>=2.9, hcli_logging (shared/)
User:       hcli (non-root, UID 1000)
Entrypoint: /entrypoint.sh → python3 -u dispatcher.py
```

COPY paths reference `llm/claude-code/` for plugin files and `orchestration/` for `bus.py`, `worker.py`, and `dispatcher.py`. Build context is the repo root.

### 3.6 `entrypoint.sh` — Container Entrypoint

Minimal wrapper. Logs startup identity, then `exec "$@"` to hand off to the CMD.

---

## 4. Security Architecture

### 4.1 Defense Layers

| Layer | Mechanism | Latency | Fail Mode |
|-------|-----------|---------|-----------|
| **Pattern denylist** | Substring match on normalized command | ~2ms | Open (pass-through) |
| **Haiku gate** | Independent LLM evaluates command vs. groundRules | ~2-3s | Closed (DENY) |

### 4.2 Threat Model Notes

- **Conversational injection**: The gate is immune — it sees only rules + command, zero chat history.
- **Command-embedded injection**: Theoretical surface. The command string is interpolated into the gate prompt. Mitigated by the deterministic pattern layer.
- **Pattern evasion**: Normalization (lowercase, quote-strip, whitespace-collapse) raises the bar but is not exhaustive. The gate is the real enforcement layer.

---

## 5. Metrics & Observability

### 5.1 Redis Counters (per day)

Gate metrics in `hcli:stats:{YYYY-MM-DD}`: `gate_calls`, `gate_input_tokens`, `gate_output_tokens`, `gate_cost_usd`, `gate_duration_ms`

### 5.2 TimescaleDB Tables

| Table | Columns | Source |
|-------|---------|--------|
| `tool_calls` | time, command, gate_result, blocked, duration_ms, output_length | firewall |

### 5.3 Logging

All modules use `hcli_logging` (shared library):
- `app.log` — operational logs (INFO+)
- `error.log` — errors only
- `audit.log` — structured JSON lines for security-relevant events (gate decisions, command forwarding)

---

## 6. Configuration Reference

### Environment Variables (plugin-owned)

| Variable | Default | Used By | Purpose |
|----------|---------|---------|---------|
| `REDIS_URL` | `redis://redis:6379` | firewall | Redis connection (gate metrics) |
| `TIMESCALE_URL` | (empty) | firewall | TimescaleDB DSN (optional, tool call logging) |
| `GATE_CHECK` | `true` | firewall | Enable/disable Haiku gate |
| `GATE_MODEL` | `haiku` | firewall | Model for gate evaluation |
| `GATE_BASE_URL` | (empty) | firewall | Override ANTHROPIC_BASE_URL for gate |
| `GATE_AUTH_TOKEN` | (empty) | firewall | Override ANTHROPIC_AUTH_TOKEN for gate |
| `GATE_API_KEY` | (empty) | firewall | Override ANTHROPIC_API_KEY for gate |
| `GROUND_RULES_PATH` | `/app/groundRules.md` | firewall | Path to ground rules file |
| `CORE_SSE_URL` | `http://h-cli-core:8083/sse` | firewall | Core MCP SSE endpoint |
| `MEMORY_SSE_URL` | `http://h-cli-core:8084/sse` | memory_proxy | Memory MCP SSE endpoint |
| `BLOCKED_PATTERNS` | (empty) | firewall | Pipe-separated denylist patterns |
| `BLOCKED_PATTERNS_FILE` | (empty) | firewall | Path to file with denylist patterns |
| `LOG_DIR` | `/var/log/hcli` | hcli_logging | Log file directory |
| `LOG_LEVEL` | `INFO` | hcli_logging | Logging level |

Dispatcher-owned env vars (e.g., `RESULT_HMAC_KEY`, `SESSION_TTL`, `HISTORY_TTL`) are documented in `orchestration/LLD.md`.

---

## 7. External Dependencies

| Dependency | Version | Purpose |
|------------|---------|---------|
| `@anthropic-ai/claude-code` | 2.1.39 | Claude CLI (gate subprocess) |
| `redis` (Python) | >=7.1, <8 | Redis client (gate metrics) |
| `mcp` (Python) | >=1.26, <2 | MCP server/client SDK |
| `psycopg2-binary` | >=2.9, <3 | TimescaleDB (optional, tool call logging) |
| `hcli_logging` | local (shared/) | Structured logging |
