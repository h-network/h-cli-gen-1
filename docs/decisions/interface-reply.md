# Interface Replies — Architecture Discussion

## Round 2: MCP Flow / Container Question

### Short answer

It doesn't affect me. Redis is my boundary. I don't care where the dispatcher lives.

### The Bot's View of the World

```
telegram-bot container          Redis           LLM container
┌──────────────┐            ┌─────────┐     ┌──────────────────────┐
│  bot.py      │◄──────────►│         │◄───►│  dispatcher.py       │
│              │  RPUSH     │  keys   │     │  bus.py              │
│  talks to    │  GET       │  pub/sub│     │  worker.py           │
│  Redis only  │  SUBSCRIBE │  lists  │     │  spawns claude-code  │
│              │  PUBLISH   │         │     │  which talks to Core │
└──────────────┘            └─────────┘     └──────────────────────┘
```

The bot pushes tasks to `hcli:tasks`, subscribes to audit/notify channels, reads results. That's it. Whether the thing on the other side of Redis is one file or three — the bot never knows and never needs to know.

### What Actually Matters to Me

1. **Redis is reachable** — As long as that's true, container topology is irrelevant.
2. **Contracts are honored** — Tasks get consumed, results appear with valid HMAC, state transitions publish to notify channels.
3. **HMAC key is shared** — Both bot and dispatcher need `RESULT_HMAC_KEY` in their env.
4. **Failure mode is the same** — If the LLM container dies, both Claude and the dispatcher go down. Already the case today.

### Summary

The dispatcher split is an internal refactor of code that lives behind Redis from my perspective. It could run in the LLM container, a separate container, or a Lambda function — the bot's code stays identical.

---

## Round 3: MCP Over Redis

### How Does This Affect Me?

Barely. The bot already talks to Redis only. The MCP layer is completely opaque — I push a task, I get a result.

### Single Network: Simplify or Complicate?

Simplify. Simpler docker-compose, no multi-network debugging. Bot's security doesn't depend on network isolation — it depends on ALLOWED_CHATS allowlist and HMAC verification.

### Redis as Single Point of Failure

Today Redis handles: task queue, results, session state, audit stream, stats. With MCP-over-Redis, add ALL tool calls and responses. If Redis goes down: today = bot can't queue tasks. Proposed = everything stops.

**My position**: Redis is already my SPOF. Adding MCP traffic doesn't change MY failure mode. But it changes the blast radius system-wide. That's an architect decision.

### Wins From My Perspective

1. No exposed service ports — reduced attack surface
2. Single network — simpler deployment
3. Consistent architecture — one monitoring story
4. Future interfaces just connect to Redis

### Summary

No code changes on my side either way. The real questions are about MCP reliability over pub/sub (Core/Orchestration problem) and Redis capacity (architect decision). Support the direction but MCP transport needs durability guarantees.

**Final outcome**: MCP stays standard and direct (AD-12). MCP-over-Redis rejected. Bot code unchanged (for this decision — subscribe model changes are separate sprint work).
