# Orchestration Replies — Architecture Discussion

## Round 2: MCP Container Question

### The Short Answer

Keep the single container. The split is about code ownership, not container isolation. Orchestration owns the logic (what to do with tasks), LLM owns the runtime (how to run Claude). They coexist in one process because separating them adds a second IPC hop for zero benefit.

### Current Runtime Architecture

One container (`claude-code`) runs everything:

```
claude-code container
├── /app/dispatcher.py      ← orchestration code (entrypoint)
├── /app/firewall.py        ← LLM code
├── /app/mcp-config.json    ← LLM config
├── /app/CLAUDE.md          ← LLM persona
├── /app/groundRules.md     ← LLM security policy
├── /app/skills/            ← volume mount
└── /var/log/hcli/sessions/ ← volume mount
```

The process flow is: `dispatcher.py` (Python, long-running) → BLPOP Redis → spawns `claude -p` (subprocess) → Claude talks to Core via MCP through `firewall.py` (stdio MCP proxy).

The container bridges both Docker networks (frontend for Redis, backend for Core MCP). This is the security boundary — it's the only thing that touches both sides.

### After the Split

Same container, three files instead of one:

```
claude-code container
├── /app/dispatcher.py      ← thin main loop (BLPOP, hand off, signal)
├── /app/bus.py             ← Redis task lifecycle (state, control, HMAC)
├── /app/worker.py          ← Claude invocation (prompt, skills, sessions)
├── /app/firewall.py        ← LLM code (unchanged)
├── /app/mcp-config.json    ← LLM config (unchanged)
├── ...
```

`dispatcher.py` imports `bus` and `worker`. That's it. No new containers, no new networks, no new IPC.

### Why Not Separate Containers

If orchestration gets its own container:

```
orchestration container (frontend network only)
  └── bus.py + dispatcher.py
  └── connects to Redis
  └── needs to talk to LLM container... how?

claude-code container (backend network only? both?)
  └── worker.py + firewall.py + claude
  └── connects to Core MCP
  └── needs tasks from orchestration... how?
```

Problems:
1. **Second IPC needed.** Orchestration picks up a task from Redis, then needs to send it to the LLM container. Via what? Another Redis queue? An HTTP API? A Unix socket? We'd be inventing a second message bus to avoid having two modules in one container.
2. **Network topology breaks.** The HLD says only the orchestration/LLM container bridges both networks. If we split them, either orchestration needs backend access (to talk to the LLM container) or the LLM container needs frontend access (to read Redis). Either way, the isolation model gets more complex, not simpler.
3. **Abort becomes harder.** The control channel subscriber (bus.py) needs to kill the Claude process (worker.py). If they're in different containers, that's a cross-container signal — much harder than `os.killpg()` on a subprocess in the same process tree.
4. **No operational benefit.** We don't need to scale orchestration independently from LLM — there's one dispatcher, one BLPOP, one task at a time.

### Ownership Boundary

| Concern | Owner | Where it lives |
|---------|-------|----------------|
| Redis keys, state machine, control channels, HMAC | Orchestration | `bus.py` |
| Claude invocation, prompt building, skills, sessions | Orchestration | `worker.py` |
| Main loop, BLPOP, signal handling | Orchestration | `dispatcher.py` |
| Dockerfile, base image, firewall, MCP config, persona | LLM | `llm/claude-code/` |
| Container runtime (how it's built and deployed) | LLM | `llm/claude-code/Dockerfile` |

Orchestration owns what the process **does**. LLM owns how the process is **built and configured**. The Dockerfile is a build artifact that COPYs files from both modules — that's normal, not a boundary violation.

### What LLM Team Needs to Do

Their Dockerfile currently has:
```dockerfile
COPY orchestration/dispatcher.py /app/dispatcher.py
```

It needs to become:
```dockerfile
COPY orchestration/dispatcher.py /app/dispatcher.py
COPY orchestration/bus.py /app/bus.py
COPY orchestration/worker.py /app/worker.py
```

That's the only cross-team change. Architect coordinates it.

---

## Round 3: MCP Over Redis

### Is This Feasible?

Yes, technically. But the complexity cost is high and the problems it solves are small.

### Current MCP Flow (what actually happens today)

```
Claude subprocess
  → stdio → firewall.py (pattern check + LLM gate)
    → SSE/HTTP → Core MCP server (run_command)
      → subprocess (ssh, dig, nmap, etc.)
    ← SSE/HTTP ← result
  ← stdio ← approved result
← stdout ← final answer
```

### Proposed MCP Flow

```
Claude subprocess
  → stdio → firewall.py (pattern check + LLM gate)
    → Redis LPUSH hcli:mcp:request (command + correlation_id)
    → Redis BLPOP hcli:mcp:response:{correlation_id} (wait for result)
      Core subscriber: BLPOP hcli:mcp:request → execute → LPUSH hcli:mcp:response:{correlation_id}
    ← result from Redis
  ← stdio ← approved result
← stdout ← final answer
```

### Key Risks

1. Pub/sub is fire-and-forget — must use LPUSH/BLPOP for MCP requests
2. Correlation complexity — unique ID per call, one bug = responses to wrong caller
3. Core loses concurrency — single BLPOP loop is one-at-a-time without thread pool
4. Firewall becomes blocking — async model doesn't play well with blocking Redis
5. Debugging harder — no tcpdump, need Redis MONITOR
6. Redis SPOF becomes more critical — every operation depends on it

### Assessment

Defer MCP-over-Redis. Ship the task lifecycle (control channels, state notifications) first. Those are high-value, low-risk. MCP-over-Redis can come later if network simplification is still wanted.

**Final outcome**: MCP stays standard and direct (AD-12). MCP-over-Redis rejected.
