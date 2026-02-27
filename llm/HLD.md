# HLD — LLM Layer

## 1. Purpose

The LLM layer contains all AI framework plugins. Each plugin wraps a specific LLM tooling framework (CLI, SDK, or API) and exposes it as a self-contained, deployable unit that receives tasks from the orchestration layer.

## 2. Current Plugins

| Plugin | Directory | Framework |
|--------|-----------|-----------|
| claude-code | `llm/claude-code/` | Anthropic Claude Code CLI (`claude -p`) |

## 3. Plugin Structure

Every plugin is self-contained. No shared code between plugins.

Each plugin provides:

| File | Purpose |
|------|---------|
| `LLD.md` | Plugin-level low-level design |
| `Dockerfile` | Container build definition |
| `entrypoint.sh` | Container entrypoint |
| `firewall.py` | Security proxy (MCP tool gating) |
| `memory_proxy.py` | Memory search proxy |
| `mcp-config.json` | MCP server routing config |
| `CLAUDE.md` | Bot behavioral instructions |

Not all plugins need every file — this is the current structure for `claude-code/`. New plugins define their own file set based on their framework's requirements.

## 4. Contract with Orchestration

Plugins receive work via Redis pub/sub, mediated by the orchestration layer:

- **Input**: Task JSON popped from `hcli:tasks` (by the orchestration dispatcher)
- **Output**: HMAC-signed result JSON stored at `hcli:results:<task_id>`
- **Tool calls**: Forwarded to Core via MCP over SSE (gated by the plugin's firewall)

The orchestration layer owns the dispatcher that drives task execution. Plugins provide the MCP servers (firewall, memory proxy) and behavioral config (CLAUDE.md) that shape each invocation.

## 5. Adding a New Plugin

1. Create `llm/<plugin-name>/`
2. Add `LLD.md` describing the plugin's internals
3. Implement the required MCP servers and config for the framework
4. Register the plugin's Dockerfile in `docker-compose.yml` (core team)
5. Update this HLD with the new plugin entry
