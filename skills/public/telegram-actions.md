---
keywords: graph, image, picture, screenshot, dashboard, panel, chart, render, png, send me, show me, show, grafana, monitoring
rules:
  - NEVER guess dashboard UIDs or panel IDs — ALWAYS discover via API first
  - Render URL must use the actual resolved URL — never use $VARIABLE names in output
  - Always include text description before the action marker
---
# Telegram Actions

You can trigger rich Telegram responses (images, files, etc.) by embedding action markers in your response text. The bot extracts the markers, sends the text normally, then executes each action.

## Format

```
[action:TYPE:PAYLOAD]
```

Place the marker at the end of your response, after any text explanation.

## Available Actions

### `graph` — Grafana render

Sends a Grafana-rendered PNG as a Telegram photo.

**Payload:** Full Grafana render URL.

**Full dashboard** (preferred — shows all panels):
```
<BASE_URL>/render/d/{dashboard-uid}?orgId=1&from={from}&to={to}&width=1200&height=2000&kiosk
```

**Single panel** (only when user asks for a specific metric):
```
<BASE_URL>/render/d-solo/{dashboard-uid}?orgId=1&panelId={panel-id}&from={from}&to={to}&width=800&height=400
```

**Common time ranges:** `now-1h`, `now-6h`, `now-24h`, `now-7d`, `now-30d`

**Example — full dashboard:**
```
Here's the overview dashboard:

[action:graph:http://h-cli-grafana:3000/render/d/hcli-overview?orgId=1&from=now-24h&to=now&width=1200&height=2000&kiosk]
```

### MANDATORY: Discover before rendering

NEVER guess dashboard UIDs, panel IDs, or URLs. You MUST use `run_command` to discover them first.

There are two Grafana instances available:
- **Local stack** (`$GRAFANA_INTERNAL_URL`): h-cli's own dashboards. Auth: basic with admin / `$GRAFANA_ADMIN_PASSWORD`
- **External** (`$GRAFANA_URL`): infrastructure monitoring. Auth: Bearer `$GRAFANA_API_TOKEN`

**Step 1 — Pick the right instance and list dashboards:**

Local stack:
```
curl -s -u "admin:$GRAFANA_ADMIN_PASSWORD" "$GRAFANA_INTERNAL_URL/api/search?type=dash-db"
```

External:
```
curl -s -H "Authorization: Bearer $GRAFANA_API_TOKEN" "$GRAFANA_URL/api/search?type=dash-db"
```

**Step 2 — Get panels for a dashboard (same pattern, swap base URL and auth):**
```
curl -s <AUTH> "<BASE_URL>/api/dashboards/uid/{uid}"
```

**Step 3 — Build the render URL using the SAME base URL from the env var:**
```
<BASE_URL>/render/d-solo/{dashboard-uid}?orgId=1&panelId={panel-id}&from=now-24h&to=now&width=800&height=400
```

## Rules

- ALWAYS discover dashboards/panels via the API before rendering — never guess or hardcode
- The render URL base MUST be the actual resolved URL (e.g. https://monitor.hb-l.nl), NOT a $VARIABLE name — never use hostnames, ports, or URLs you constructed yourself
- Always include a text description before the action marker
- The marker is stripped from the message — the user never sees it
- One marker per action; multiple actions can appear in one response
