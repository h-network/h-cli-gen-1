# Core Replies — Architecture Discussion

## Round 2: MCP Flow Question

### Short answer

No impact on Core. The dispatcher split is entirely inside the LLM container. Core's interface is MCP over SSE on :8083. We don't care what process made the call — only that `command` and optionally `task_id` arrive as tool parameters.

### The call chain today

```
dispatcher.py (has task_id from Redis BLPOP)
  → spawns Claude process (task_id in env/config)
    → Claude decides to call run_command
      → firewall.py intercepts (reads task_id from env, injects as parameter)
        → SSE :8083 → mcp_server.py receives run_command(command, task_id)
```

### The call chain after split

```
bus.py (has task_id from Redis BLPOP)
  → passes task_id to worker.py
    → worker.py spawns Claude process (task_id in env/config)
      → Claude decides to call run_command
        → firewall.py intercepts (reads task_id from env, injects as parameter)
          → SSE :8083 → mcp_server.py receives run_command(command, task_id)
```

From Core's perspective, these are identical.

### The one thing that could break

The task_id handoff chain has one more hop after the split. If bus.py doesn't pass task_id to worker.py, or worker.py doesn't set it in the Claude process environment, the firewall won't have it to inject, and Core's audit publishing silently stops working. This is invisible — no errors, no crashes, just missing audit events.

**Recommendation to Orchestration**: When you split, make task_id a required field on the bus→worker interface.

### Contracts (unchanged)

- Core serves MCP SSE on :8083, accepts `run_command(command, task_id="")`
- Core publishes to `hcli:audit:{task_id}` if task_id is non-empty and Redis is reachable
- Core never reads from Redis, never subscribes, never checks task state
- Audit publishing is fire-and-forget — missing events are normal

---

## Round 3: MCP Over Redis

### Summary

Feasible, not trivial. The business logic (subprocess execution, output truncation, audit logging, memory search) is unchanged. The transport layer is a full replacement — FastMCP SSE goes away, Redis request/response takes its place.

### Key changes

| Component | Change |
|-----------|--------|
| mcp_server.py | Replace FastMCP SSE with Redis Streams consumer loop + thread pool |
| memory_server.py | Same — Redis Streams instead of FastMCP SSE |
| entrypoint.sh | Remove memory server background start, remove SSE health check |
| Dockerfile | Remove EXPOSE 8083 8084 |
| requirements.txt | Remove mcp[cli], keep redis |

### Recommendation

Use Redis Streams (not pub/sub) for MCP calls, keeping pub/sub for audit events. Good architectural direction — eliminates task_id hack, removes exposed ports, simplifies topology.

### Wins

1. No exposed ports — Core has zero listening ports
2. task_id flows naturally in message envelope
3. Single communication backbone
4. Scaling path via consumer groups

### Risks

1. Redis is now SPOF for everything
2. Losing MCP ecosystem compatibility
3. Message ordering with concurrent processing
4. Debugging harder without SSE endpoints
5. Security boundary: who can write to request stream

**Final outcome**: MCP stays standard and direct (AD-12). MCP-over-Redis rejected. Core code unchanged.
