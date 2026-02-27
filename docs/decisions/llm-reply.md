# LLM Replies — Architecture Discussion

## Round 2: MCP Flow & Container Ownership

### The single-container model is correct. Don't split it.

The process tree inside the `claude-code` container is:

```
dispatcher.py (PID 1 via entrypoint)
  └── claude -p --mcp-config mcp-config.json
        ├── firewall.py    (stdio MCP, child process)
        └── memory_proxy.py (stdio MCP, child process)
```

This is a parent-child process tree, not a service mesh. Every link is either `exec` or stdio pipe. Splitting it across containers would mean replacing stdio MCP with network MCP (SSE), splitting dispatcher from Claude (no remote invocation API), or making bus.py a standalone networked service (a rewrite, not a refactor).

### Ownership model: file-level, not container-level

| What | Owner | Why |
|------|-------|-----|
| bus.py, worker.py, dispatcher.py | Orchestration | They write it, they maintain it |
| firewall.py, memory_proxy.py, mcp-config.json, CLAUDE.md | LLM | We write it, we maintain it |
| Dockerfile, entrypoint.sh | LLM | We own the container assembly |
| docker-compose.yml service definition | Architect/Core | Shared infrastructure config |

The Dockerfile is an assembly manifest. LLM owns the assembly because we own the runtime environment. Orchestration's code is a dependency we consume, not a foreign service we call.

### Summary

Keep single container. Ownership is file-level. LLM owns the container packaging, orchestration owns the Python modules we package. The MCP architecture requires co-location — stdio MCP servers must be child processes of Claude.

---

## Round 3: MCP Over Redis

### How firewall.py Would Change

Currently `_forward_to_core()` does SSE connect → MCP initialize → call_tool → wait → parse result. With Redis, the SSE client is replaced with Redis PUBLISH + SUBSCRIBE. The MCP SDK client is completely removed from firewall.py — only the stdio server side facing Claude stays.

### Latency Impact

| Path | Current (SSE) | Proposed (Redis) |
|------|--------------|--------------------|
| SSE connect + MCP init | ~5-15ms first call | 0 (no connection per call) |
| Per-call overhead | ~1-3ms | ~1-2ms |
| Total typical | ~5-20ms | ~2-5ms |

Redis is likely faster per call because there's no SSE connection setup. Current code creates a new SSE connection per tool call (no connection pooling).

### Haiku Gate Interaction

Zero impact. The gate check runs before `_forward_to_core()`. Only the Core transport changes.

### Recommendation

Use LPUSH/BRPOP for requests (guaranteed delivery), pub/sub for responses (fast, firewall is already subscribed).

### Risks

1. Pub/sub message loss if Core isn't subscribed
2. No backpressure — 50 simultaneous tool calls flood Core
3. Debugging harder — silent failures
4. Redis becomes harder SPOF
5. Large payloads need chunking

**Final outcome**: MCP stays standard and direct (AD-12). MCP-over-Redis rejected. Firewall code unchanged.
