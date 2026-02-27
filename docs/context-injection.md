# Context Injection: Plain Text vs JSONL Replay

## The Problem

Claude Code's `claude -p` CLI supports `--resume <session-id>` to continue conversations. Under the hood, this replays the entire JSONL session file — every user message, assistant response, tool call, tool result, file snapshot, and queue operation — as structured input to the API.

For a 2-hour conversation with 73 user turns, that's **1.3MB of JSONL** and **130,312 input tokens**. Per message. Every time.

On API pricing (Opus), that's **$0.71 per message** just for context — before the model even starts thinking about the answer.

## The Solution

Instead of replaying JSONL, inject conversation history as plain text:

1. Store each user/assistant exchange in Redis as it happens
2. On the next message, read the history, format as markdown, prepend to the user's message
3. Start each `claude -p` call with a fresh `--session-id`

```
## Recent conversation (same session)
Below is your conversation history with this user.
Lines marked ASSISTANT are YOUR previous replies.

[22:31] **USER**: hi

[22:31] **ASSISTANT**: Hello! I'm h-cli, your engineering assistant.

[22:32] **USER**: what did we do today?

[22:32] **ASSISTANT**: Based on the most recent session chunk...
---

<actual new message here>
```

The JSONL file is still written (Claude Code does this automatically with `--session-id`), but it's never replayed into the context window. It serves as an audit trail and training data archive.

## Results

Same conversation (2026-02-15, 23:04 → 01:06 UTC, 73 user turns), same question (`"what did we talk about earlier?"`), same container:

```
Method A: --resume (JSONL replay)
  JSONL size:    1,299 KB (781 lines)
  Input tokens:  130,312
  Cost:          $0.7153
  API duration:  6,719ms

Method B: Plain text injection
  Context size:  30 KB (capped)
  Input tokens:  37,207
  Cost:          $0.1385
  API duration:  12,662ms
```

| Metric | --resume | Plain text | Savings |
|--------|----------|------------|---------|
| Input tokens | 130,312 | 37,207 | **71% fewer** |
| Cost per call | $0.7153 | $0.1385 | **81% cheaper** |

## Why It Works

JSONL sessions carry overhead that has zero conversational value:

- **Tool call JSON** — `{"type": "tool_use", "name": "run_command", "input": {"command": "nmap ..."}}`
- **Tool results** — full stdout/stderr captured as structured objects
- **File snapshots** — `file-history-snapshot` entries for every file touched
- **Queue operations** — `dequeue`/`enqueue` metadata
- **Nested structure** — content arrays with type discriminators, IDs, timestamps

Plain text strips all of this down to what actually matters for conversation continuity: **who said what, and when**.

For an infrastructure assistant like h-cli, the user-assistant dialogue carries the context. The tool call details are implementation artifacts — useful for training and auditing, not for maintaining the conversation thread.

## The Tradeoff

**What you lose:**
- The model doesn't see its own previous tool calls. If the user says "run that again," the model needs enough conversational context to infer what "that" was from the dialogue, not from a tool_use record.
- Longer conversations may lose nuance as plain text is capped (30KB Redis history + 50KB session chunks in h-cli's case).

**What you gain:**
- 71% fewer input tokens per message
- 81% lower cost on API pricing
- More headroom in the 200K context window before hitting limits
- Faster time-to-first-token (less input to process)
- JSONL still archived for audit trail, training data, and debugging

**Side effect:**
- With more headroom, the model tends to write longer responses. A conciseness directive in CLAUDE.md counteracts this — costs ~140 input tokens but saves 83% of output tokens ([test data](test-cases/brevity-directive-output-savings.md)).

## Implementation

h-cli implements this in three tiers:

```
┌──────────────────────────────────────────────────────────┐
│  Tier 1: Redis Session History (< 24h)                   │
│  Recent turns in Redis list, formatted as markdown,      │
│  prepended to user message. Lightweight, real-time.      │
├──────────────────────────────────────────────────────────┤
│  Tier 2: Session Chunks (> 24h)                          │
│  When accumulated size > 100KB, history dumped to text   │
│  files. Up to 50KB injected into system prompt.          │
├──────────────────────────────────────────────────────────┤
│  Tier 3: Skills (per-message, automatic)                 │
│  Matched skill files from skills/ directory. Injected    │
│  into system prompt after chunks. 20KB budget.           │
├──────────────────────────────────────────────────────────┤
│  Tier 4: Vector Memory (permanent, optional)             │
│  Curated Q&A pairs in Qdrant. Semantic search via        │
│  memory_search tool. For long-term knowledge.            │
├──────────────────────────────────────────────────────────┤
│  Archive: JSONL Files (always written, never replayed)   │
│  Claude Code's native session files. Rich structured     │
│  data for auditing, debugging, and future model training.│
└──────────────────────────────────────────────────────────┘
```

Key files:
- `orchestration/worker.py` — `_build_conversation_context()` formats Redis history, `build_system_prompt()` injects chunks + skills
- `llm/claude-code/CLAUDE.md` — conciseness directive to counteract verbose responses

## Applicability

This technique applies to any system using `claude -p --resume` in a loop:
- Chatbots and conversational agents
- CI/CD pipelines with multi-step Claude interactions
- Any headless Claude Code deployment processing sequential tasks

If your conversation history is primarily dialogue (not heavy tool use where the model needs to see its own previous tool results), plain text injection will save significant tokens with minimal quality loss.

Full test data: [resume-vs-plaintext-context.md](test-cases/resume-vs-plaintext-context.md)

Output savings: [brevity-directive-output-savings.md](test-cases/brevity-directive-output-savings.md) — the conciseness directive alone cuts output tokens by 83%, reducing cost by 48% and response time by 77%. Combined with plain text injection, total token usage drops ~85%.
