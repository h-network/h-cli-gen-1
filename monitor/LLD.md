# Monitor Module — Low-Level Design

## 1. Overview

The monitor module is the **data infrastructure layer** of h-cli. It owns the
schemas, provisioning configs, and dashboard definitions that store and visualize
all operational metrics. It does **not** contain application code — other teams
(Orchestration, Core) write data into the stores this module defines.

### Scope

| In scope | Out of scope |
|----------|--------------|
| TimescaleDB schema & policies | INSERT logic (owned by Orchestration) |
| Grafana provisioning & dashboards | Docker service definitions (Architect) |
| Qdrant collection schemas | Application code that queries Qdrant (Core) |
| Retention, compression tuning | Redis stats cache (Core) |

---

## 2. File Inventory

```
monitor/
├── LLD.md                            # This document
├── init.sql                          # DDL: tables, hypertables, policies
├── datasource.yml                    # Grafana → TimescaleDB connection
├── dashboard.yml                     # Grafana dashboard provisioning
├── export-traces.py                  # Export correlated traces as JSONL
└── dashboards/
    └── h-cli-overview.json           # Main dashboard (15 panels)
```

---

## 3. Position in System Message Flow

The monitor module is a **passive observer** — it never participates in the
request/response path. It receives data written by other modules and serves
visualizations on demand.

### 3.1 Where We Sit in the Full Flow

Reference: Architect LLD section 3 — "Message Flow: Telegram In to Telegram Out"

```
User → Telegram → telegram-bot → Redis → dispatcher → firewall → core
                                                 │          │
                                          Step 8 │   Step 6 │
                                          (task  │   (gate  │
                                          done)  │   check) │
                                                 ▼          ▼
                                          ┌──────────────────────┐
                                          │     TimescaleDB      │
                                          │    hcli_metrics DB   │
                                          │                      │
                                          │  task_metrics ◄──┐   │
                                          │  tool_calls  ◄──┐│   │
                                          └─────────┬───────┘│   │
                                                    │        │   │
                                              SQL queries     │   │
                                                    │        │   │
                                                    ▼        │   │
                                          ┌──────────────────┘   │
                                          │      Grafana         │
                                          │  h-cli-overview      │
                                          │  dashboard           │
                                          └──────┬───────────────┘
                                                 │
                                    ┌────────────┼────────────┐
                                    ▼            ▼            ▼
                              Browser UI    /graph action   /stats
                              (port 2405)   (PNG render)    (Redis cache)
                                            → telegram-bot  → telegram-bot
```

### 3.2 Data Entry Points

**Step 6 — Firewall gate check** (Architect LLD 3.1, Step 6):
Every `run_command` call passes through the Asimov firewall. After each gate
decision, `llm/claude-code/firewall.py` INSERTs one row into `tool_calls` with the command,
result, blocked status, and latency. This happens whether the command is
allowed or denied.

**Step 8 — Result signing and storage** (Architect LLD 3.1, Step 8):
After Claude produces a response, `orchestration/dispatcher.py` logs task metrics to
TimescaleDB: token counts (input, output, cache_read, cache_create),
cost, duration, model, turns, and error status. The dispatcher also
updates `hcli:last_activity:{chat_id}` and checks session size — if
accumulated size exceeds 100KB or idle time exceeds 30 minutes, it
dumps the session to a chunk file (O-21 dual drain triggers). The
TimescaleDB INSERT is the last action before the result is pushed to
Redis for delivery.

### 3.3 Data Exit Points

**Grafana UI** (browser): Direct dashboard access on port 2405.

**`/graph` action** (Architect LLD 3.1, Step 9 context): Interface team's
Telegram action system calls Grafana's HTTP render API to generate PNG
screenshots of dashboard panels, sent back to the user in Telegram.

**`/stats` command**: Interface team reads `hcli:stats:{date}` Redis keys —
daily aggregates derived from `task_metrics`.

### 3.4 What We Do NOT Touch

- We never see the user's message content (only token counts)
- We never participate in the request/response path
- We never write to Redis (Orchestration owns `hcli:stats` writes and session keys including `hcli:last_activity`)
- We have no runtime code — only schema definitions and config files

**Pattern**: Event-sourced metrics with time-series storage.
Producers INSERT one row per event. Grafana queries aggregate on read.
No intermediate ETL, no message queue — direct SQL writes and reads.

---

## 4. Database Schema (`init.sql`)

### 4.1 `task_metrics` — one row per completed task

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `time` | TIMESTAMPTZ | NOT NULL | Event timestamp (hypertable partition key) |
| `task_id` | TEXT | NOT NULL | Unique task identifier |
| `chat_id` | TEXT | NULL | Telegram chat / session ID |
| `model` | TEXT | NULL | Claude model used (e.g. `claude-sonnet-4-20250514`) |
| `input_tokens` | INTEGER | 0 | Prompt tokens consumed |
| `output_tokens` | INTEGER | 0 | Completion tokens generated |
| `cache_read` | INTEGER | 0 | Prompt-cache hits (tokens) |
| `cache_create` | INTEGER | 0 | Prompt-cache writes (tokens) |
| `cost_usd` | DOUBLE PRECISION | 0 | Calculated monetary cost |
| `duration_ms` | INTEGER | 0 | End-to-end latency |
| `num_turns` | INTEGER | 1 | Agentic conversation turns |
| `is_error` | BOOLEAN | FALSE | Whether the task failed |

**TimescaleDB policies**:

| Policy | Value | Rationale |
|--------|-------|-----------|
| Hypertable partition | `time` column | Efficient time-range pruning |
| Compression | After **7 days** | Hot data stays uncompressed for fast queries |
| Compression segmentby | `chat_id, model` | Queries often filter by these columns |
| Retention | **90 days** | Prevents unbounded growth; 3 months of history |

### 4.2 `tool_calls` — one row per firewall gate decision

| Column | Type | Default | Purpose |
|--------|------|---------|---------|
| `time` | TIMESTAMPTZ | NOT NULL | Event timestamp (hypertable partition key) |
| `command` | TEXT | NOT NULL | Shell command attempted |
| `gate_result` | TEXT | NULL | Firewall decision reason |
| `blocked` | BOOLEAN | FALSE | Whether the command was blocked |
| `duration_ms` | INTEGER | 0 | Gate-check latency |
| `output_length` | INTEGER | 0 | stdout length if command ran |

**TimescaleDB policies**:

| Policy | Value | Rationale |
|--------|-------|-----------|
| Hypertable partition | `time` column | Same as above |
| Compression | After **7 days** | Same schedule as task_metrics |
| Compression segmentby | `blocked` | Queries commonly filter allowed vs blocked |
| Retention | **90 days** | Consistent lifecycle across tables |

### 4.3 Design Decisions

- **No indexes beyond hypertable**: TimescaleDB auto-creates time-based chunk
  indexes. Additional indexes not needed at current query volume.
- **`IF NOT EXISTS` on all DDL**: Script is safe to re-run (idempotent init).
- **No foreign keys**: Tables are independent event streams. No joins between
  them in any dashboard query.
- **`DOUBLE PRECISION` for cost**: Avoids `NUMERIC` overhead; 15-digit precision
  is sufficient for sub-cent cost tracking.

---

## 5. Grafana Provisioning

### 5.1 Datasource (`datasource.yml`)

```yaml
name: TimescaleDB
type: postgres
url:  h-cli-timescaledb:5432     # Docker-internal DNS
database: hcli_metrics
user: hcli
password: ${TIMESCALE_PASSWORD}  # Injected at container start
sslmode: disable                 # Internal network, no TLS needed
timescaledb: true                # Enables time_bucket() in query editor
```

Key: the `timescaledb: true` flag enables Grafana's TimescaleDB query macros
(`$__timeFilter`, `time_bucket`).

### 5.2 Dashboard Provider (`dashboard.yml`)

```yaml
name: h-cli
type: file
path: /etc/grafana/provisioning/dashboards/files
editable: true
disableDeletion: false
foldersFromFilesStructure: false
```

Grafana reads `h-cli-overview.json` from disk on startup. Edits in the UI are
allowed but do **not** persist to disk — the JSON in version control is the
source of truth.

---

## 6. Dashboard Layout (`dashboards/h-cli-overview.json`)

**UID**: `hcli-overview`
**Refresh**: 30 seconds
**Default range**: Last 24 hours
**Datasource variable**: `${DS_TIMESCALEDB}` (type `postgres`, hidden)

### 6.1 Grid Layout

The dashboard uses a 24-column Grafana grid. Panels are arranged in four rows:

```
Row 0 (y=0, h=4)    KPI stats — six 4-wide stat panels
┌──────┬──────┬──────┬──────┬──────┬──────┐
│Tasks │Cost  │Error%│Resp  │Tokens│Turns │
│Today │Today │ 24h  │Time  │Today │ 24h  │
└──────┴──────┴──────┴──────┴──────┴──────┘

Row 1 (y=4, h=8)    Time-series — two 12-wide charts
┌────────────────────┬────────────────────┐
│ Token Usage / Time │ Cost Over Time     │
└────────────────────┴────────────────────┘

Row 2 (y=12, h=8)   Time-series — two 12-wide charts
┌────────────────────┬────────────────────┐
│ Task Count & Errs  │ Response Time / T  │
└────────────────────┴────────────────────┘

Row 3 (y=20, h=8)   Distribution — three 8-wide panels
┌──────────────┬──────────────┬──────────────┐
│ Model Dist   │ Top Chats by │ Cache Hit    │
│ (pie)        │ Cost (table) │ Rate (line)  │
└──────────────┴──────────────┴──────────────┘

Row 4 (y=28, h=10)  Detail — full-width tables
┌────────────────────────────────────────────┐
│ Recent Tasks (50 rows)                     │
├────────────────────────────────────────────┤
│ Recent Tool Calls (50 rows)          y=38  │
└────────────────────────────────────────────┘
```

### 6.2 Panel Catalog

| ID | Title | Type | Source Table | Metric |
|----|-------|------|-------------|--------|
| 1 | Tasks Today | stat | task_metrics | `COUNT(*)` (24h) |
| 2 | Cost Today | stat | task_metrics | `SUM(cost_usd)` (24h) |
| 3 | Error Rate (24h) | stat | task_metrics | `% WHERE is_error` |
| 4 | Avg Response Time | stat | task_metrics | `AVG(duration_ms)/1000` |
| 5 | Tokens Today | stat | task_metrics | `SUM(input+output)` (24h) |
| 6 | Avg Turns (24h) | stat | task_metrics | `AVG(num_turns)` |
| 7 | Token Usage Over Time | timeseries | task_metrics | 4 series: input, output, cache_read, cache_create |
| 8 | Cost Over Time | timeseries | task_metrics | `SUM(cost_usd)` per 5 min |
| 9 | Task Count & Errors | timeseries | task_metrics | count + error count per 5 min |
| 10 | Response Time Over Time | timeseries | task_metrics | avg + max duration per 5 min |
| 11 | Model Distribution | piechart | task_metrics | `COUNT(*)` by model |
| 12 | Top Chats by Cost | table | task_metrics | grouped by chat_id, top 10 |
| 13 | Cache Hit Rate | timeseries | task_metrics | `cache_read / input_tokens` % |
| 14 | Recent Tasks | table | task_metrics | last 50 rows, all columns |
| 15 | Recent Tool Calls | table | tool_calls | last 50 rows, all columns |

### 6.3 Threshold Definitions

| Panel | Green | Yellow | Red |
|-------|-------|--------|-----|
| Tasks Today | < 50 | 50–99 | >= 100 |
| Cost Today (USD) | < $1 | $1–$4.99 | >= $5 |
| Error Rate | < 5% | 5–19% | >= 20% |
| Avg Response Time | < 10 s | 10–29 s | >= 30 s |
| Tokens Today | < 500 k | 500 k–1.9 M | >= 2 M |
| Avg Turns | < 5 | 5–14 | >= 15 |

### 6.4 Query Patterns

All time-series panels use the same structure:

```sql
SELECT time_bucket('5 minutes', time) AS time,
       <aggregation> AS "<label>"
FROM   <table>
WHERE  $__timeFilter(time)
GROUP BY 1
ORDER BY 1
```

- `$__timeFilter(time)` — Grafana macro, expands to
  `time BETWEEN <from> AND <to>` based on the dashboard time picker.
- `time_bucket('5 minutes', ...)` — TimescaleDB function that floors timestamps
  to 5-minute boundaries.
- Stat panels use `now() - INTERVAL '24 hours'` (fixed window, not dashboard
  range) so KPIs always show a rolling 24-hour summary.
- Division-by-zero is guarded with `CASE WHEN ... = 0 THEN 0 ELSE ...` in
  error-rate and cache-hit queries.

---

## 7. Interfaces & Contracts

### 7.1 Inbound — data written by Orchestration team

**`task_metrics` INSERT** (triggered at Architect LLD Step 8):

| Column | Source in orchestration/dispatcher.py | Required |
|--------|------------------------|----------|
| `time` | `now()` at insert time | Yes (NOT NULL) |
| `task_id` | From Redis task JSON `task_id` field | Yes (NOT NULL) |
| `chat_id` | From Redis task JSON `chat_id` field | No (DEFAULT NULL) |
| `model` | From Redis task JSON `model` field | No |
| `input_tokens` | Parsed from Claude stdout usage line | No (DEFAULT 0) |
| `output_tokens` | Parsed from Claude stdout usage line | No (DEFAULT 0) |
| `cache_read` | Parsed from Claude stdout usage line | No (DEFAULT 0) |
| `cache_create` | Parsed from Claude stdout usage line | No (DEFAULT 0) |
| `cost_usd` | Calculated from token counts x model pricing | No (DEFAULT 0) |
| `duration_ms` | `end_time - start_time` of Claude invocation | No (DEFAULT 0) |
| `num_turns` | Parsed from Claude stdout usage line | No (DEFAULT 1) |
| `is_error` | `True` if Claude process exit code != 0 | No (DEFAULT FALSE) |

Connection: Dispatcher uses `TIMESCALE_URL` env var (Architect LLD 6, Monitor section).

**`tool_calls` INSERT** (triggered at Architect LLD Step 6):

| Column | Source in llm/claude-code/firewall.py | Required |
|--------|----------------------|----------|
| `time` | `now()` at insert time | Yes (NOT NULL) |
| `command` | Raw command string from `run_command` MCP call | Yes (NOT NULL) |
| `gate_result` | Haiku gate response text (or pattern match reason) | No |
| `blocked` | `True` if either denylist or gate blocked | No (DEFAULT FALSE) |
| `duration_ms` | Gate check latency (pattern + Haiku combined) | No (DEFAULT 0) |
| `output_length` | `len(stdout)` if command executed, else 0 | No (DEFAULT 0) |

Connection: Firewall uses same `TIMESCALE_URL` env var.

**Dependency**: Both writers require the `monitor` compose profile to be active.
If TimescaleDB is not running, writes are skipped (not queued). This is by
design — metrics are optional, the main flow must not block on monitoring.

### 7.2 Outbound — data consumed by other teams

| Interface | Consumer | Protocol | Contract |
|-----------|----------|----------|----------|
| Dashboard rendering | Interface `/graph` action | HTTP GET `{GRAFANA_INTERNAL_URL}/render/d-solo/hcli-overview` | Returns PNG; params: `panelId`, `from`, `to`, `width`, `height` |
| Stats counters | Interface `/stats` command | Redis `hcli:stats:{YYYY-MM-DD}` hash | Written by Orchestration, derived from `task_metrics` |
| Vector search | Core `memory_server.py` | Qdrant HTTP API (port 6333) | Collection schemas owned by Data team |

### 7.3 Contracts We Depend On

| Contract | Owner | What happens if broken |
|----------|-------|----------------------|
| `TIMESCALE_URL` env var format | Architect (`.env.template`) | Dispatcher/firewall cannot connect — metrics silently lost |
| `TIMESCALE_PASSWORD` env var | Architect (`install.sh` generates) | Grafana cannot query — dashboard shows "no data" |
| `monitor` compose profile active | Architect (`docker-compose.yml`) | TimescaleDB + Grafana not started — entire module offline |
| `monitor/*.yml` volume mount in Grafana | Architect (`docker-compose.yml`) | Provisioning files not loaded — empty Grafana |
| `init.sql` executed on TimescaleDB start | Architect (`docker-compose.yml` init script) | Tables don't exist — INSERTs fail, dashboards empty |
| `GRAFANA_INTERNAL_URL` env var | Architect (`.env.template`) | Interface team's `/graph` renders fail |

---

## 8. Environment & Service Dependencies

### 8.1 Environment Variables

| Variable | Used in | Purpose |
|----------|---------|---------|
| `TIMESCALE_PASSWORD` | `datasource.yml` | DB password for user `hcli` |

### 8.2 Docker Services (configured by Architect, used by Data)

| Service | Internal DNS | Port | Purpose |
|---------|-------------|------|---------|
| TimescaleDB | `h-cli-timescaledb` | 5432 | Metrics storage |
| Grafana | `h-cli-grafana` | 2405 (host-mapped) | Dashboards & render |
| Qdrant | `h-cli-qdrant` | 6333/6334 | Vector search |

---

## 9. Data Lifecycle

```
Event occurs
    │
    ▼
INSERT into hypertable
    │
    ▼
Hot data (0–7 days)
  • Uncompressed
  • Fast queries
    │
    ▼  (automatic compression policy)
Compressed data (7–90 days)
  • Segmented by chat_id/model (task_metrics)
  • Segmented by blocked (tool_calls)
  • Read-only chunks, reduced storage
    │
    ▼  (automatic retention policy)
Dropped (> 90 days)
  • Chunks deleted entirely
  • No archive or backup
```

---

## 10. Design Rationale Summary

| Decision | Why |
|----------|-----|
| TimescaleDB over plain Postgres | `time_bucket()`, automatic chunk management, compression, retention policies — purpose-built for this workload |
| No application code in monitor/ | Separation of concerns: Data team owns infrastructure, other teams own the logic that writes/reads |
| File-based Grafana provisioning | GitOps: dashboard JSON is version-controlled, reproducible across environments |
| 5-minute time buckets | Balances granularity with query performance; sufficient for trend analysis and incident detection |
| 30-second dashboard refresh | Near-real-time visibility without overloading the database |
| 90-day retention | Three months covers billing cycles and trend analysis; prevents unbounded storage growth |
| 7-day compression delay | Keeps the most-queried recent data uncompressed for fast access |
| Separate tables (no FK) | task_metrics and tool_calls are independent event streams with different schemas and query patterns |
| `COALESCE` and `CASE WHEN` guards | Prevents NULL/division-by-zero in dashboard panels when no data exists |
| Transparent panels | Visual style choice — panels render without background, cleaner look |
