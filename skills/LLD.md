# Skills Module — Low-Level Design

## Purpose

The skills module is a collection of keyword-tagged Markdown files that inject domain knowledge into the bot's system prompt on a per-message basis. We own the content and format — the Orchestration team's dispatcher handles loading.

## Directory Structure

```
skills/
├── README.md                      # Format specification (never injected)
├── LLD.md                         # This document
├── public/                        # Shared skills — tracked in git
│   ├── eve-ng.md                  # EVE-NG lab automation
│   ├── hssh-edit.md               # h-ssh config deployment (safety-critical)
│   ├── hssh-show.md               # h-ssh show commands (SSH, telnet, REST)
│   ├── hssh-troubleshoot.md       # h-ssh troubleshooting workflows
│   ├── stats.md                   # Token usage & cost queries
│   ├── telegram-actions.md        # Rich Telegram responses (Grafana graphs)
│   └── tmux-interaction.md        # tmux pane sessions & parallel bootstrap
└── private/                       # Deployment-specific — gitignored
    └── .gitkeep                   # Preserves directory in git
```

**Scopes:**
- `public/` — version-controlled, shared across deployments
- `private/` — gitignored, generated at runtime (e.g. via teach mode), persists per deployment

---

## Skill File Format

Every skill is a Markdown file with an optional YAML frontmatter header:

```markdown
---
keywords: word1, word2, multi-word phrase
---
# Title

Content injected into system prompt...
```

### Keyword Header Rules

| Rule | Detail |
|------|--------|
| Delimiter | Three dashes (`---`) above and below |
| Field | `keywords:` — comma-separated, case-insensitive |
| Matching | Any keyword found as a whole word in the user's message triggers the skill |
| Fallback | No header → matched by filename (e.g. `ospf.md` matches the word "ospf") |
| Exclusion | `README.md` has no keywords and its filename never appears in messages — never injected |

### Budget

Total injected skill content is capped at **20 KB per message**. If matched skills exceed this, content is truncated. Skills are loaded in sorted filename order, so alphabetically earlier files have priority.

---

## File Responsibilities

### README.md

**Role:** Format specification and onboarding guide for skill authors.

**Contents:**
- YAML frontmatter format with example (OSPF skill)
- Keyword matching rules (case-insensitive, whole-word, filename fallback)
- 20 KB budget constraint
- Instructions for adding new skills (create file → restart/remount → test)
- Bot-drafted skill workflow: drafts land in `/tmp/skills/` on the core container, reviewed via `docker cp`, approved by copying to `skills/`

**Not injected** — has no keywords and `README` never appears as a message keyword.

### public/stats.md

**Role:** Teaches the bot how to query token usage, cost, and performance metrics.

**Keywords:** `stats, statistics, tokens, cost, usage, spending, expensive, cheap, metrics, grafana, dashboard, how much, budget`

**Content sections:**

| Section | What it provides |
|---------|-----------------|
| Quick stats | Directs user to `/stats` Telegram command (Redis counters) |
| Today's totals | Python one-liner: task count, input/output/cache tokens, cost, avg duration |
| Cost by model | Python one-liner: per-model breakdown over 7 days |
| Daily breakdown | Python one-liner: daily task count + cost over 7 days |
| Schema reference | `task_metrics` table columns for ad-hoc queries |

**Data source:** TimescaleDB via `$TIMESCALE_URL` environment variable, queried with `psycopg2` through the bot's `run_command` tool.

**Schema:**
```
Table: task_metrics
Columns: time, task_id, chat_id, model, input_tokens, output_tokens,
         cache_read, cache_create, cost_usd, duration_ms, num_turns, is_error
```

### public/telegram-actions.md

**Role:** Teaches the bot how to embed action markers that trigger rich Telegram responses (images, graphs).

**Keywords:** `graph, image, picture, screenshot, dashboard, panel, chart, render, png, send me, show me, show, grafana, monitoring`

**Content sections:**

| Section | What it provides |
|---------|-----------------|
| Format | Action marker syntax: `[action:TYPE:PAYLOAD]` |
| `graph` action | Grafana render — full dashboard and single panel URL templates |
| Discovery | Mandatory API calls to find dashboard UIDs and panel IDs before rendering |
| Two instances | Local stack (`$GRAFANA_INTERNAL_URL`, basic auth) and external (`$GRAFANA_URL`, bearer token) |
| Rules | Never guess UIDs, resolve URLs (no `$VAR` in output), text before marker, marker stripped from message |

**Action marker contract:**

```
[action:graph:<fully-resolved-grafana-render-URL>]
```

- Placed at end of response text
- Bot strips the marker before sending text to user
- Bot executes the action (fetches PNG, sends as Telegram photo)
- Multiple markers allowed per response

**Discovery flow (mandatory before every render):**

```
Step 1: List dashboards    → GET <BASE_URL>/api/search?type=dash-db
Step 2: Get dashboard panels → GET <BASE_URL>/api/dashboards/uid/{uid}
Step 3: Build render URL    → <BASE_URL>/render/d-solo/{uid}?panelId={id}&...
```

**Environment variables:**

| Variable | Instance | Auth method |
|----------|----------|-------------|
| `GRAFANA_INTERNAL_URL` | Local stack | Basic: `admin:$GRAFANA_ADMIN_PASSWORD` |
| `GRAFANA_ADMIN_PASSWORD` | Local stack | (password) |
| `GRAFANA_URL` | External | Bearer: `$GRAFANA_API_TOKEN` |
| `GRAFANA_API_TOKEN` | External | (token) |

### private/.gitkeep

**Role:** Preserves the `private/` directory in git. All other files in `private/` are gitignored.

Runtime-generated skills (from teach mode or manual creation) live here. They follow the same format as public skills but are deployment-specific and not shared.

---

## Position in Message Flow

Reference: Architect LLD section 3 — end-to-end message lifecycle.

Skills are loaded between **Step 4** (context injection) and **Step 5** (Claude invocation). The dispatcher builds the system prompt and calls `_load_matching_skills(message)` in `worker.py` to inject our content:

```
Step 3: Dispatcher picks up task from Redis
       │
Step 4: Context injection (Redis history → user message, session chunks → system prompt)
       │
   ┌───┴────────────────────────────────────────────┐
   │ _load_matching_skills(message)                  │
   │                                                 │
   │ 1. Tokenize message into word set               │
   │ 2. Scan /app/skills/public/*.md                 │
   │    and  /app/skills/private/*.md                │
   │ 3. Parse YAML frontmatter per file              │
   │ 4. keyword ∩ msg_words ≠ ∅ → match              │
   │ 5. Inject RULES from matched skill headers      │
   │ 6. Append full skill index (all skill paths)    │
   │ 7. Enforce 20 KB cap on rules content           │
   └───┬─────────────────────────────────────────────┘
       │
       ▼
   System prompt = groundRules.md + context.md + session chunks + SKILL RULES + SKILL INDEX
       │
Step 5: claude -p --session-id {uuid} -- {message_with_history}
       │
Step 6–9: Firewall → Core → Result signing → Telegram delivery
```

**Tiered injection model:** Only the `rules:` entries from matched skill YAML headers are injected into the prompt — not the full skill body. A full skill index (all skill names and file paths) is always appended so the model knows what skills exist. This keeps prompt size minimal while giving the model actionable constraints.

**What we receive:** The user's raw message text (for keyword matching only — we never see chat_id, task_id, or session state).

**What we hand off:** Rules from matched skill headers + a full skill index, capped at 20 KB, injected into the system prompt as a `## Skills` section.

**What we don't control:** The matching algorithm, the injection order within the prompt, the budget enforcement logic — all owned by Orchestration in `worker.py`.

---

## Interfaces

### Outbound: Skill Files → Dispatcher

**Contract:** The dispatcher expects Markdown files in `public/` and `private/` with:
- Optional YAML frontmatter containing `keywords:` (comma-separated)
- Markdown body after the closing `---`
- No executable code (content is injected as text, not run)

**Mount point at runtime:** `./skills` is bind-mounted read-only to `/app/skills` in the claude-code and telegram-bot containers (see Architect LLD section 4.7).

### Outbound: Skill Content → Claude

Skills instruct Claude on:
- What tools to use (`run_command` for Python/curl)
- What environment variables are available
- What output format to produce (action markers, concise text)
- What constraints to follow (discover before guess, resolve URLs)

Action markers (e.g. `[action:graph:URL]`) produced by Claude based on skill instructions are extracted by telegram-bot in **Step 9** of the message flow — the bot strips the marker, sends text to the user, then executes the action (e.g. fetches Grafana PNG, sends as photo).

### Inbound: Teach Mode → private/

Bot-drafted skills are written to `/tmp/skills/` on the core container. After human review, approved drafts are copied into `skills/private/` (or `skills/public/` if they should be tracked).

### Dependencies on Other Teams

| Contract | Owner | What we depend on |
|----------|-------|-------------------|
| `_load_matching_skills()` | Orchestration | Keyword matching, budget enforcement, injection into system prompt |
| Volume mount `./skills:/app/skills:ro` | Architect | Our files accessible at runtime in the container |
| `[action:TYPE:PAYLOAD]` extraction | Interface | Bot parsing action markers from Claude's response |
| `run_command` MCP tool | Core | Skills reference this tool for Python/curl execution |
| Asimov firewall | Orchestration | Commands taught by skills are still gated (Steps 6–7) |

---

## Design Decisions

| Decision | Rationale |
|----------|-----------|
| **Keyword matching, not embeddings** | Simple, predictable, zero-latency — skill authors control exactly when a skill fires |
| **YAML frontmatter, not a registry** | Skills are self-describing; no separate config file to maintain |
| **Filename fallback** | Allows minimal skills without a header — just name the file well |
| **20 KB budget** | Prevents prompt bloat; forces skills to be concise and focused |
| **Public/private split** | Public = shared knowledge (git-tracked); Private = deployment-specific (e.g. internal URLs, credentials context) |
| **Read-only mount** | Skills are immutable at runtime; changes require a redeploy or volume remount |
| **Bot drafts to /tmp, not directly** | Prevents untrusted content from auto-loading; human review gate before promotion |
| **One topic per file** | Keeps matching precise — a stats question loads stats, not Grafana rendering |
| **Sorted file order for budget** | Deterministic; alphabetically earlier skills get priority when nearing the 20 KB cap |

---

## Constraints & Limitations

- **No skill versioning** — overwriting a file loses history (git tracks public, but private is ephemeral)
- **No priority/weight system** — all matched skills are equal; only file sort order affects budget overflow
- **No skill dependencies** — a skill can't require another skill to also be loaded
- **No content-based matching** — only keywords and filenames are checked, not the skill body
- **No caching** — skills are re-read from disk on every message (acceptable at current scale)
- **Single-word matching only** — multi-word keywords like `"how much"` only match if both words appear (as separate keyword entries), not as a phrase

---

## Adding a New Skill

1. Create `skills/public/<topic>.md` (or `private/` for deployment-specific)
2. Add YAML frontmatter with relevant keywords
3. Write concise, actionable content (instructions, commands, examples)
4. Keep under a few KB to stay within the 20 KB budget alongside other skills
5. No container rebuild needed — volume mount picks up changes on next message
6. Test by sending a message containing one of the keywords
