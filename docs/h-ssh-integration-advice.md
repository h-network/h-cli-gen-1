# h-ssh Integration Advice

**From**: h-cli Architect Team
**To**: h-ssh Project Architect
**Date**: 2026-02-20
**Subject**: Changes needed for seamless h-cli integration

---

## Executive Summary

h-cli is an AI-powered infrastructure management bot (Telegram → Claude Code → containerized execution). We plan to install h-ssh in our execution container so the AI model can use it via shell commands. Instead of SSH'ing into 10 devices one by one, the model runs `h-ssh.py -sC bgp --workers 10`.

Five teams reviewed the full h-ssh source code from their module's perspective. This document consolidates their findings into a prioritized list of changes the h-ssh creator should make.

**The bottom line**: h-ssh's core architecture (parallel execution, vendor abstraction, safety features) is excellent. The changes needed are all about making it work **non-interactively** — no prompts, structured output, and machine-readable logging.

---

## How h-cli Will Use h-ssh

```
User (Telegram): "check bgp on all routers"
        │
        ▼
Model (Claude): queries NetBox for device list
        │
        ▼
Model generates: h-ssh.py --user hcli --json -sC bgp --target CR1:10.0.1.1:junos --target CR2:10.0.1.2:junos -y
        │
        ▼
Core container: executes h-ssh via run_command (MCP tool)
        │
        ▼
h-ssh: parallel SSH/NETCONF to all devices
        │
        ▼
Model: parses JSON output, summarizes for user
        │
        ▼
User (Telegram): "BGP is up on CR1 and CR2. CR3 has 0 established peers — check the config?"
```

For configuration deployments:

```
User: sends whiteboard photo → "deploy this topology"
        │
        ▼
Model: extracts devices and connections from image
Model: generates per-device .set configs (CR1.set, CR2.set, ...)
Model: h-ssh.py -eD /tmp/configs/ --commit-confirmed 10 --dry-run -y --json
        │
        ▼
Model: shows dry-run diff to user, asks for confirmation
        │
        ▼
Model: h-ssh.py -eD /tmp/configs/ --commit-confirmed 10 -y --json
        │
        ▼
Entire lab configured in parallel, auto-rollback if not confirmed within 10 minutes
```

---

## Priority 0 — Blockers

These prevent h-ssh from working inside h-cli at all.

### B1. Non-interactive credential handling

**Problem**: h-ssh uses `input()` and `getpass.getpass()` for credentials. The model executes commands via `subprocess.run()` — there is no TTY. `input()` will raise `EOFError` or block forever.

**Fix**: Add a `--password` CLI flag and/or `HSSH_PASSWORD` environment variable.

```bash
# Current (blocks forever in non-interactive context):
h-ssh.py -sC bgp
# Username: <hangs>

# Needed:
h-ssh.py --user admin --password "$HSSH_PASSWORD" -sC bgp

# Or via env:
HSSH_USER=admin HSSH_PASSWORD=secret h-ssh.py -sC bgp
```

**Notes**:
- `--user` already exists and works for SSH key auth (Junos/generic). Good.
- Arista (eAPI) always requires a password — no SSH key path. This is the blocker.
- When both `--user` and `--password` are provided, never prompt. If credentials are missing and no SSH key works, fail immediately with a clear error.

### B2. Auto-skip interactive prompts when stdin is not a TTY

**Problem**: Edit operations prompt `Proceed with changes? [y/N]`. The `-y` flag exists, but if the model forgets it, the command hangs forever.

**Fix**: Detect `not sys.stdin.isatty()` and auto-accept (or auto-reject with a clear error). Fail-closed is better than hanging.

```python
# Suggested behavior:
if not sys.stdin.isatty() and not args.yes:
    print("ERROR: Edit operation requires confirmation but stdin is not a TTY. Use -y to skip.", file=sys.stderr)
    return 2
```

---

## Priority 1 — Core Integration

These are needed for the model to use h-ssh effectively.

### P1.1. Structured JSON output (`--json`)

**Problem**: h-ssh prints human-readable terminal text with banners, timestamps, and status lines. The model would need to regex-parse multi-device output, which is fragile — especially when device output itself contains text matching the banner format.

**Fix**: Add `--json` flag that outputs a JSON array of per-device results:

```json
[
  {
    "device": "CR1",
    "host": "10.0.1.1",
    "vendor": "junos",
    "ok": true,
    "output": "BGP peer 10.0.2.1 Established\nBGP peer 10.0.3.1 Established",
    "duration_ms": 380
  },
  {
    "device": "CR2",
    "host": "10.0.1.2",
    "vendor": "junos",
    "ok": false,
    "error": "connection timeout",
    "duration_ms": 30000
  }
]
```

This lets the model parse results programmatically. It can then summarize: "2 devices checked, 1 failure: CR2 timed out."

### P1.2. Inline device specification (`--target`)

**Problem**: h-ssh requires a `devices.csv` file. The model often gets device lists from user input ("check bgp on CR1 10.0.1.1 and CR2 10.0.1.2") or from NetBox queries. Writing a temp CSV requires an extra tool call.

**Fix**: Add a repeatable `--target` flag:

```bash
h-ssh.py -sC bgp \
  --target CR1:10.0.1.1:junos \
  --target CR2:10.0.1.2:arista \
  --target SW1:10.0.2.1:ssh
```

Format: `name:host:vendor` (vendor defaults to `junos` if omitted).

Keep `--devices` for inventory-based runs. `--target` is for ad-hoc use. Both should be mutually exclusive or combinable — your call.

### P1.3. Quiet mode (`--quiet`)

**Problem**: `[HH:MM:SS] CR1 STARTED`, `[HH:MM:SS] CR1 OK` status lines are noise when the model is the consumer. The user never sees stdout — they see the model's response.

**Fix**: Add `--quiet` / `-q` that suppresses all status lines and banners. Only output the results (or JSON when combined with `--json`).

### P1.4. Structured logging (`--log-json`)

**Problem**: Current `--log` writes a single flat line per invocation. No per-device breakdown, no timing, no success/failure counts.

**Fix**: Add `--log-json` (or make `--log` default to JSONL) that emits one record per invocation:

```json
{
  "timestamp": "2026-02-20T14:30:00Z",
  "mode": "show",
  "command": "show bgp summary | no-more",
  "transport": "junos",
  "targets_total": 10,
  "targets_ok": 9,
  "targets_fail": 1,
  "duration_ms": 4200,
  "dry_run": false,
  "devices": [
    {"name": "CR1", "ok": true, "duration_ms": 380, "output_length": 1240},
    {"name": "CR2", "ok": false, "duration_ms": 30000, "error": "connection timeout"}
  ]
}
```

This integrates with h-cli's JSONL-based audit pipeline and training data export.

### P1.5. Summary line to stdout

**Problem**: h-cli's training data export (`export-traces.py`) captures `run_command` stdout. A structured summary at the end of stdout lets the training pipeline extract h-ssh metadata without parsing the full output.

**Fix**: Print a single prefixed JSON line at the end of stdout:

```
[h-ssh] {"targets": 10, "ok": 9, "fail": 1, "duration_ms": 4200, "mode": "show"}
```

The prefix `[h-ssh]` makes it easy to grep. The model can also read this as feedback on its action.

---

## Priority 2 — Security & Observability

### P2.1. Fix `AutoAddPolicy()` in generic.py

**Problem**: `generic.py` uses `paramiko.AutoAddPolicy()` which silently accepts **and overwrites** changed host keys. h-cli's SSH config uses `StrictHostKeyChecking accept-new` — accepts new keys but rejects changed keys (protects against MITM).

**Fix**: Switch to `paramiko.WarningPolicy()` or configure paramiko to use the system `known_hosts` file:

```python
client.load_system_host_keys()
client.set_missing_host_key_policy(paramiko.WarningPolicy())
```

### P2.2. Edit operation audit log

**Problem**: h-ssh can push configuration changes to infrastructure devices. These are high-risk operations that should have their own audit trail — separate from the firewall audit (which sees one `run_command`) and separate from h-ssh's general log.

**Fix**: For edit operations (`-eC`, `-eD`, `-eB`), write to a dedicated JSONL audit log:

```json
{
  "timestamp": "2026-02-20T14:31:00Z",
  "mode": "edit-broadcast",
  "device": "CR1",
  "host": "10.0.1.1",
  "payload": "set interfaces ge-0/0/0 unit 0 ...",
  "dry_run": false,
  "commit_confirmed": 10,
  "ok": true,
  "diff": "..."
}
```

Log path: configurable via `--audit-log`. h-cli will point it to `logs/hssh/audit.log`.

---

## No Changes Needed

These aspects of h-ssh already work well for h-cli integration:

| Feature | Why it works |
|---------|-------------|
| **Exit codes** (0/1/2) | Model detects success/partial/setup failure |
| **`-y` flag** | Skips confirmation for automation |
| **`--dry-run`** | Model shows diffs before committing |
| **`--commit-confirmed`** | Auto-rollback safety net |
| **`--workers`** | Parallel execution, default 8 is fine |
| **`--save-output`** | Per-device output files for large results |
| **`-eD` mode** | Per-device configs from directory — parallel deployment |
| **`--session-timeout` / `--command-timeout`** | Defaults fit within h-cli's 280s dispatcher timeout |
| **SSH key auth** | h-cli container already has keys at `~/.ssh/` — paramiko finds them automatically |
| **Vendor auto-detection** | CSV `vendor` column maps directly to transport |
| **Command shortcuts** | `.cmd` files are small and bake into Docker image easily |

---

## How We'll Deploy h-ssh

For reference — this is what happens on our side (no h-ssh changes needed):

1. **Core Dockerfile**: `COPY h-ssh/ /app/h-ssh/` + `pip install paramiko junos-eznc pyeapi`
2. **SSH keys**: Reuse existing `/home/hcli/.ssh/` — no separate key management
3. **MCP integration**: Model invokes via existing `run_command` — no dedicated MCP tool
4. **Command library**: Baked into Docker image at `/app/h-ssh/commands/`
5. **Skill file**: A 3KB cheat sheet teaching the model when and how to use h-ssh
6. **Asimov safety rules**: Edit operations require `--dry-run` first, `--commit-confirmed` mandatory
7. **Metrics**: New `hssh_operations` table in TimescaleDB, Grafana panels for h-ssh ops
8. **Training data**: h-ssh runs flow through existing trace export pipeline

---

## Summary Table

| # | Change | Priority | Effort |
|---|--------|----------|--------|
| B1 | `--password` flag / `HSSH_PASSWORD` env var | **Blocker** | Small — add argparse flag, read env |
| B2 | Fail-closed when stdin is not a TTY | **Blocker** | Small — `isatty()` check |
| P1.1 | `--json` output format | **P1** | Medium — restructure output path |
| P1.2 | `--target name:host:vendor` flag | **P1** | Small — add to argparse + loader |
| P1.3 | `--quiet` flag | **P1** | Small — suppress print statements |
| P1.4 | `--log-json` structured logging | **P1** | Medium — per-device timing + JSONL |
| P1.5 | Summary JSON line to stdout | **P1** | Small — one print at exit |
| P2.1 | Fix `AutoAddPolicy()` | **P2** | Trivial — one line change |
| P2.2 | Edit audit log (JSONL) | **P2** | Medium — new logging path |

---

*This document was compiled from independent reviews by five h-cli teams (Interface, Orchestration, Core, Knowledge, Data), each analyzing the full h-ssh source code from their module's perspective.*
