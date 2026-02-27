---
keywords: stats, statistics, tokens, cost, usage, spending, expensive, cheap, metrics, how much, budget
rules:
  - Use $TIMESCALE_URL for database connection — never hardcode credentials
  - Query via run_command with python3 one-liner and psycopg2
  - Keep output concise — summarize, don't dump raw query results
---
# Metrics & Usage Stats

h-cli tracks token usage, cost, and performance per task in TimescaleDB.

## Quick stats (Redis counters)

For today's summary, tell the user to run `/stats` in Telegram.

## Querying TimescaleDB

Use `run_command` with a python one-liner. The connection string is in `$TIMESCALE_URL`.

### Examples

**Today's totals:**
```
python3 -c "
import os, psycopg2
conn = psycopg2.connect(os.environ['TIMESCALE_URL'])
cur = conn.cursor()
cur.execute(\"\"\"
  SELECT COUNT(*) as tasks,
         COALESCE(SUM(input_tokens),0) as input,
         COALESCE(SUM(output_tokens),0) as output,
         COALESCE(SUM(cache_read),0) as cache,
         ROUND(COALESCE(SUM(cost_usd),0)::numeric,4) as cost,
         ROUND(COALESCE(AVG(duration_ms),0)::numeric/1000,1) as avg_s
  FROM task_metrics WHERE time >= now() - INTERVAL '24 hours'
\"\"\")
r = cur.fetchone()
print(f'Tasks: {r[0]} | In: {r[1]:,} Out: {r[2]:,} Cache: {r[3]:,} | Cost: \${r[4]} | Avg: {r[5]}s')
conn.close()
"
```

**Cost by model (last 7 days):**
```
python3 -c "
import os, psycopg2
conn = psycopg2.connect(os.environ['TIMESCALE_URL'])
cur = conn.cursor()
cur.execute(\"\"\"
  SELECT model, COUNT(*) as tasks,
         ROUND(SUM(cost_usd)::numeric,4) as cost,
         SUM(input_tokens + output_tokens) as tokens
  FROM task_metrics WHERE time >= now() - INTERVAL '7 days'
  GROUP BY model ORDER BY cost DESC
\"\"\")
for r in cur.fetchall():
    print(f'{r[0]}: {r[1]} tasks, \${r[2]}, {r[3]:,} tokens')
conn.close()
"
```

**Daily cost breakdown (last 7 days):**
```
python3 -c "
import os, psycopg2
conn = psycopg2.connect(os.environ['TIMESCALE_URL'])
cur = conn.cursor()
cur.execute(\"\"\"
  SELECT time_bucket('1 day', time)::date as day,
         COUNT(*) as tasks,
         ROUND(SUM(cost_usd)::numeric,4) as cost
  FROM task_metrics WHERE time >= now() - INTERVAL '7 days'
  GROUP BY 1 ORDER BY 1
\"\"\")
for r in cur.fetchall():
    print(f'{r[0]}: {r[1]} tasks, \${r[2]}')
conn.close()
"
```

## Schema reference

Table `task_metrics`: time, task_id, chat_id, model, input_tokens, output_tokens, cache_read, cache_create, cost_usd, duration_ms, num_turns, is_error.

Adapt queries as needed for the user's question. Keep output concise.
