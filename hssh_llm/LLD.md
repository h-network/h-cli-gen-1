# LLM Team — Low-Level Design (`hssh_llm/`)

## 1. Overview

The LLM team owns the **h-ssh integration** into h-cli — the code, skills, command library, and audit specifications that enable Claude to orchestrate network devices via parallel SSH/NETCONF. We maintain the h-ssh Python tool, adapt it for non-interactive execution inside the Core container, and write the skill files that teach Claude when and how to use it.

h-ssh is not a service with its own container. It is a Python CLI tool that runs inside the Core container via the existing `run_command` MCP tool. The model generates h-ssh commands, the Asimov firewall clears them, and Core executes them — the same path as any other shell command. Our job is to make that path work reliably and safely.

**What we own:**
- h-ssh Python source code (adapted for h-cli integration)
- Skill files teaching Claude h-ssh usage patterns
- Command shortcut library (`.cmd` files for common operations)
- h-ssh audit log specification
- Integration tests

**What we do NOT own:**
- Core Dockerfile (Core team copies our code at build time)
- Asimov firewall rules (Architect owns `groundRules.md` and `blocked-patterns.txt`)
- TimescaleDB schema for h-ssh metrics (Monitor team)
- The `run_command` MCP tool (Core team)

---

## 2. File Responsibilities

```
hssh_llm/
├── LLD.md                         # This document
├── REPLY.md                       # Task report (branch-only, deleted before merge)
│
├── h-ssh/                         # h-ssh Python tool (adapted for h-cli)
│   ├── h-ssh.py                   # Main CLI entry point (argparse)
│   ├── core/
│   │   ├── __init__.py
│   │   ├── runner.py              # Parallel execution engine (ThreadPoolExecutor)
│   │   ├── output.py              # Output formatters (human, JSON, quiet)
│   │   └── audit.py               # Edit operation JSONL audit logger
│   ├── transports/
│   │   ├── __init__.py
│   │   ├── junos.py               # Juniper hybrid: paramiko SSH (show) + PyEZ NETCONF (config)
│   │   ├── arista.py              # Arista eAPI (stub — requires pyeapi)
│   │   ├── generic.py             # Generic SSH (paramiko)
│   │   ├── rest.py                # REST API transport (httpx) — for NetBox, LibreNMS, etc.
│   │   ├── telnet.py              # Raw-socket telnet — Eve-NG consoles, legacy devices
│   │   └── base.py                # Abstract transport interface
│   └── commands/                  # Command shortcut library (.cmd files)
│       ├── bgp.cmd
│       ├── ospf.cmd
│       ├── interfaces.cmd
│       ├── routes.cmd
│       └── lldp.cmd
│
├── skills/                        # h-ssh skill files (deployed to skills/public/)
│   ├── hssh-show.md               # Show command patterns and examples
│   ├── hssh-edit.md               # Edit command patterns, safety workflow
│   └── hssh-troubleshoot.md       # Troubleshooting workflows (multi-step)
│
└── tests/                         # Integration tests
    ├── test_cli.py                # CLI flag parsing, output format validation
    ├── test_json_output.py        # JSON output schema compliance
    └── test_noninteractive.py     # Non-TTY behavior (B1, B2 compliance)
```

### 2.1 `h-ssh.py` — CLI Entry Point

Single-file entry point that parses arguments, loads device targets, and dispatches to the execution engine.

**CLI interface (h-cli integration flags):**

| Flag | Type | Purpose |
|------|------|---------|
| `--user` | string | SSH username (existing) |
| `--password` | string | SSH/eAPI password (B1 — replaces interactive prompt) |
| `--devices` | path | CSV device inventory (existing) |
| `--target` | repeatable | Inline device `name:host:vendor` (P1.2) |
| `-sC` | string | Show command mode (existing) |
| `-eC` | string | Edit command mode — single command (existing) |
| `-eD` | path | Edit directory mode — per-device config files (existing) |
| `-eB` | string | Edit broadcast mode — same command to all (existing) |
| `--json` | flag | JSON output instead of human-readable (P1.1) |
| `--quiet` | flag | Suppress status banners and progress lines (P1.3) |
| `--log-json` | path | JSONL structured log output (P1.4) |
| `--audit-log` | path | Edit operation audit trail (P2.2) |
| `-y` | flag | Skip confirmation prompts (existing) |
| `--dry-run` | flag | Show diffs without committing (existing) |
| `--commit-confirmed` | int | Auto-rollback timeout in minutes (existing) |
| `--workers` | int | Parallel worker count, default 8 (existing) |
| `--session-timeout` | int | SSH session timeout, default 30s (existing) |
| `--command-timeout` | int | Per-command timeout, default 120s (existing) |
| `--job` | path | JSON job file with per-device commands (use `-` for stdin) |
| `--save-output` | path | Save per-device output to directory (existing) |

**Exit codes:**
- `0` — all devices succeeded
- `1` — partial failure (some devices failed)
- `2` — setup failure (bad args, no devices, auth failure)

**Non-interactive behavior (B1 + B2):**

```python
# B1: Credential handling
if args.password:
    password = args.password
elif os.environ.get("HSSH_PASSWORD"):
    password = os.environ["HSSH_PASSWORD"]
elif ssh_key_available():
    password = None  # key auth
else:
    if not sys.stdin.isatty():
        print("ERROR: No credentials provided and stdin is not a TTY.", file=sys.stderr)
        sys.exit(2)
    password = getpass.getpass()

# B2: Confirmation handling
if not sys.stdin.isatty() and not args.yes:
    print("ERROR: Edit operation requires confirmation but stdin is not a TTY. Use -y.", file=sys.stderr)
    sys.exit(2)
```

### 2.2 `core/runner.py` — Parallel Execution Engine

Manages concurrent device connections via `ThreadPoolExecutor(max_workers=args.workers)`.

**Per-device execution:**
1. Select transport based on vendor (`junos` → PyEZ, `arista` → eAPI, `ssh`/default → paramiko)
2. Connect with timeout (`--session-timeout`, default 30s)
3. Execute command with timeout (`--command-timeout`, default 120s)
4. Capture output (stdout) and error state
5. Return `DeviceResult` dataclass

**DeviceResult schema:**
```python
@dataclass
class DeviceResult:
    device: str          # Device name
    host: str            # IP/hostname
    vendor: str          # Transport type
    ok: bool             # Success flag
    output: str          # Command output (stdout)
    error: str | None    # Error message if failed
    duration_ms: int     # Wall-clock time
    diff: str | None     # Config diff (edit mode only)
```

### 2.3 `core/output.py` — Output Formatters

Three output modes, selected by CLI flags:

**Human mode (default):**
```
[14:30:01] CR1 (10.0.1.1) — STARTED
[14:30:01] CR2 (10.0.1.2) — STARTED
[14:30:02] CR1 (10.0.1.1) — OK (380ms)
BGP peer 10.0.2.1 Established
BGP peer 10.0.3.1 Established
[14:30:31] CR2 (10.0.1.2) — FAIL (30000ms): connection timeout

Summary: 2 devices, 1 ok, 1 fail
```

**JSON mode (`--json`)** (P1.1):
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

**Quiet mode (`--quiet`)** (P1.3): Suppresses status banners (`STARTED`, `OK`, `FAIL` lines) and the summary. In JSON mode, outputs only the JSON array. In human mode, outputs only device command output.

**Summary line to stdout** (P1.5): Always printed after results (unless `--quiet`):
```
[h-ssh] {"targets": 2, "ok": 1, "fail": 1, "duration_ms": 30380, "mode": "show"}
```

### 2.4 `core/audit.py` — Edit Operation Audit Logger

Writes JSONL audit entries for configuration change operations (`-eC`, `-eD`, `-eB`). Separate from the general h-ssh log — this is a dedicated change-control trail.

**Audit entry schema** (P2.2):
```json
{
  "timestamp": "2026-02-20T14:31:00Z",
  "mode": "edit-broadcast",
  "device": "CR1",
  "host": "10.0.1.1",
  "vendor": "junos",
  "payload": "set interfaces ge-0/0/0 unit 0 family inet address 10.0.0.1/30",
  "dry_run": false,
  "commit_confirmed": 10,
  "ok": true,
  "diff": "[edit interfaces ge-0/0/0 unit 0]\n+  family inet { ... }"
}
```

**Log path:** Configurable via `--audit-log`. Default: `/var/log/hcli/hssh/audit.log`. h-cli sets this to the Core container's log volume.

### 2.5 `transports/` — Vendor Abstraction

Each transport implements the `BaseTransport` interface:

```python
class BaseTransport(ABC):
    @abstractmethod
    def connect(self, host, user, password=None, timeout=30): ...

    @abstractmethod
    def show(self, command, timeout=120) -> str: ...

    @abstractmethod
    def edit(self, payload, dry_run=False, confirmed_minutes=0) -> EditResult: ...

    @abstractmethod
    def close(self): ...
```

| Transport | File | Library | Auth | Edit Support |
|-----------|------|---------|------|-------------|
| `junos` | `junos.py` | paramiko + junos-eznc (PyEZ) | SSH key or password | Hybrid: paramiko for show, PyEZ NETCONF for config |
| `arista` | `arista.py` | pyeapi (stub) | Password required (eAPI) | Stub — raises NotImplementedError |
| `generic` | `generic.py` | paramiko | SSH key or password | Show-only (no structured edit) |
| `rest` | `rest.py` | httpx | Bearer, Basic, X-Auth-Token, custom header | GET for show, PATCH/POST/PUT for edit |
| `telnet-*` | `telnet.py` | stdlib socket | Username/password (login sequence) | IOS: config terminal; Junos: configure/commit |

**Host key policy** (P2.1): All transports use `WarningPolicy()` instead of `AutoAddPolicy()`. System `known_hosts` is loaded first:
```python
client.load_system_host_keys()
client.set_missing_host_key_policy(paramiko.WarningPolicy())
```

### 2.6 `commands/` — Command Shortcut Library

Baked-in `.cmd` files for common network operations. Each file contains the vendor-specific command string:

```
# bgp.cmd
junos: show bgp summary | no-more
arista: show ip bgp summary
generic: show ip bgp summary
```

Used via: `h-ssh.py -sC bgp` (resolves `bgp` → `bgp.cmd` → vendor-specific command).

### 2.7 `skills/` — Claude Skill Files

Skill files deployed to `skills/public/` at build time. Follow the Knowledge team's format (YAML frontmatter + Markdown body).

**hssh-show.md** — When and how to run show commands:
- Keywords: `h-ssh, hssh, show, bgp, ospf, interfaces, routes, lldp, network devices, check`
- Content: CLI examples, JSON output parsing, multi-device patterns, NetBox integration

**hssh-edit.md** — Configuration deployment workflow:
- Keywords: `h-ssh edit, deploy, configure, config, commit, rollback, set, delete`
- Content: Mandatory safety workflow (dry-run → user confirm → commit-confirmed), per-device configs, broadcast mode

**hssh-troubleshoot.md** — Multi-step diagnostic patterns:
- Keywords: `troubleshoot, diagnose, debug, down, flapping, unreachable, timeout`
- Content: Iterative workflows (ping → traceroute → show interface → show bgp → show log)

### 2.8 `tests/` — Integration Tests

| Test file | What it validates |
|-----------|-------------------|
| `test_cli.py` | Argument parsing, flag combinations, mutual exclusivity |
| `test_json_output.py` | JSON output matches schema (device, host, vendor, ok, output/error, duration_ms) |
| `test_noninteractive.py` | B1: exits with code 2 when no credentials and no TTY; B2: exits with code 2 on edit without `-y` and no TTY |

---

## 3. Position in System Flow

h-ssh commands flow through the standard 9-step message lifecycle (Architect LLD section 3). No new steps or services are introduced — h-ssh is invoked via the existing `run_command` tool.

```
User (Telegram): "check bgp on all routers"
        │
        ▼ (Steps 1-4: Telegram → bot → Redis → dispatcher)

Model (Claude): reads hssh-show.md skill, queries NetBox for device list
        │
        ▼ (Step 5: Claude generates command)

Claude: run_command("h-ssh.py --user hcli --json -sC bgp \
          --target CR1:10.0.1.1:junos --target CR2:10.0.1.2:junos -y")
        │
        ▼ (Step 6: Asimov firewall)

Firewall: Pattern denylist → PASS (h-ssh.py is not blocked)
          Haiku gate → PASS (show command, read-only, no risk)
        │
        ▼ (Step 7: Core execution)

Core: subprocess.run("h-ssh.py ...") → parallel SSH to CR1, CR2
        │
        ▼ (h-ssh returns JSON array + summary line)

Claude: parses JSON, summarizes:
        "BGP up on CR1 (2 peers). CR2 timed out — check connectivity?"
        │
        ▼ (Steps 8-9: HMAC sign → Redis → bot → Telegram → user)
```

### 3.1 Edit Operation Flow (Safety-Critical)

Edit operations follow a mandatory two-phase workflow enforced by the skill file:

```
Phase 1 — Dry Run (model MUST do this first)

Claude: run_command("h-ssh.py --user hcli --json -eD /tmp/configs/ \
          --commit-confirmed 10 --dry-run -y")
        │
        ▼
h-ssh: connects, loads configs, computes diffs, does NOT commit
        │
        ▼
Claude: shows diffs to user, asks "Proceed with deployment?"
        │
        ▼
User: "yes" or "no"

Phase 2 — Commit (only after user confirmation)

Claude: run_command("h-ssh.py --user hcli --json -eD /tmp/configs/ \
          --commit-confirmed 10 -y --audit-log /var/log/hcli/hssh/audit.log")
        │
        ▼
h-ssh: commits configs with auto-rollback safety net (10 min)
        │
        ▼
Claude: reports results, reminds user to confirm within rollback window
```

The skill file (`hssh-edit.md`) instructs Claude to **always** use `--dry-run` first for edit operations and **always** include `--commit-confirmed`. The Asimov firewall provides defense-in-depth — the Haiku gate evaluates edit commands against `groundRules.md` Layer 1 (Law 0: protect infrastructure, no destructive ops without confirmation).

---

## 4. Deployment

h-ssh code is copied into the Core container at build time. No separate container, no new service.

### 4.1 Core Dockerfile Changes (Core Team)

```dockerfile
# Added by Core team to core/Dockerfile:
COPY hssh_llm/h-ssh/ /app/h-ssh/
RUN pip install --no-cache-dir paramiko junos-eznc pyeapi
ENV PATH="/app/h-ssh:${PATH}"
```

### 4.2 Skill File Deployment (Knowledge Team)

Our skill files are copied to `skills/public/` at deploy time:

```bash
cp hssh_llm/skills/hssh-show.md skills/public/
cp hssh_llm/skills/hssh-edit.md skills/public/
cp hssh_llm/skills/hssh-troubleshoot.md skills/public/
```

### 4.3 Volume Mounts (Architect)

| Volume | Mount Point | Mode | Purpose |
|--------|-------------|------|---------|
| `./logs/hssh` | `/var/log/hcli/hssh` | rw | h-ssh audit logs (edit operations) |
| `./hssh_llm/h-ssh/commands` | `/app/h-ssh/commands` | ro | Command shortcuts (hot-reload) |

### 4.4 Environment Variables

| Variable | Default | Used By | Purpose |
|----------|---------|---------|---------|
| `HSSH_PASSWORD` | — | `h-ssh.py` | Default device password (if not passed via `--password`) |
| `HSSH_USER` | — | `h-ssh.py` | Default SSH username (if not passed via `--user`) |
| `HSSH_AUDIT_LOG` | `/var/log/hcli/hssh/audit.log` | `core/audit.py` | Default audit log path |
| `HSSH_WORKERS` | `8` | `core/runner.py` | Default worker count |
| `HSSH_SESSION_TIMEOUT` | `30` | transports | Default SSH session timeout (seconds) |
| `HSSH_COMMAND_TIMEOUT` | `120` | transports | Default per-command timeout (seconds) |

All env vars are injected into the Core container via `docker-compose.yml` (Architect-owned).

---

## 5. Structured Logging (`--log-json`) (P1.4)

One JSONL record per invocation, written to the path specified by `--log-json`:

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

This integrates with h-cli's JSONL-based audit pipeline (`export-traces.py`) and training data export. The `[h-ssh]` summary line on stdout (P1.5) provides a lightweight alternative for the training pipeline to extract h-ssh metadata without parsing full output.

---

## 6. Security Model

### 6.1 Layers That Protect h-ssh Operations

| Layer | Owner | Mechanism | Scope |
|-------|-------|-----------|-------|
| Pattern denylist | Architect | Substring match in `blocked-patterns.txt` | Blocks destructive patterns before gate |
| Haiku gate | Orchestration | Stateless LLM evaluation vs. `groundRules.md` | Evaluates h-ssh commands against safety laws |
| h-ssh `-y` / TTY check | Us | `isatty()` fail-closed | Prevents hanging on unattended confirmation |
| `--commit-confirmed` | Us | Auto-rollback timer | Safety net for config deployments |
| `--dry-run` | Us | Diff without commit | Mandatory first step in edit workflow |
| SSH `WarningPolicy` | Us | Reject changed host keys | MITM protection (P2.1) |
| Core subprocess timeout | Core | 240s hard kill | Prevents runaway h-ssh processes |

### 6.2 Asimov Interaction

h-ssh commands are evaluated by the same Asimov firewall as any other `run_command` invocation. The Haiku gate sees the full command string (e.g., `h-ssh.py -eB "set interfaces ..." -y --commit-confirmed 10`) and evaluates it against:

- **Law 0** (protect infrastructure): edit operations are infrastructure changes — the gate will require `--dry-run` or `--commit-confirmed` based on ground rules training
- **Law 1** (obey operator): show commands are always allowed
- **Law 3** (stay within boundaries): h-ssh connects to authorized hosts via existing SSH keys

### 6.3 Credential Security

- **SSH keys:** Reuse existing `/home/hcli/.ssh/` in the Core container — no separate key management
- **Passwords:** Via `--password` flag or `HSSH_PASSWORD` env var — never persisted to disk, never logged
- **eAPI credentials:** Same mechanism — `HSSH_PASSWORD` for Arista devices that require password auth
- **No credential in audit logs:** The audit logger (`core/audit.py`) explicitly excludes password fields

---

## 7. Contracts

### 7.1 What We Receive

| From | What | Via |
|------|------|-----|
| Claude (model) | h-ssh command strings | `run_command` MCP tool through Asimov firewall |
| Core container | Shell execution environment | `subprocess.run(shell=True)` |
| Architect | SSH keys | `/home/hcli/.ssh/` (mounted at container startup) |
| Architect | Device credentials | `HSSH_PASSWORD` env var |
| Network devices | SSH/NETCONF/eAPI responses | Network (h-network-backend → target hosts) |

### 7.2 What We Produce

| To | What | Via |
|----|------|-----|
| Claude (model) | JSON device results + summary line | stdout (captured by `run_command`) |
| Audit pipeline | Edit operation JSONL | `--audit-log` file path |
| Training pipeline | Invocation JSONL | `--log-json` file path |
| Operations | Exit code (0/1/2) | Process return code |

### 7.3 Dependencies on Other Teams

| Contract | Owner | What we depend on |
|----------|-------|-------------------|
| `run_command` MCP tool | Core | Executes our CLI, returns stdout + exit code |
| Core Dockerfile integration | Core | `COPY h-ssh/` + `pip install` our dependencies |
| Asimov firewall | Orchestration | Clears h-ssh commands before they reach Core |
| Skill file loading | Orchestration (dispatcher) | Keyword-matches and injects our skills into system prompt |
| Skill file format | Knowledge | YAML frontmatter + Markdown body convention |
| Volume mounts | Architect | Log directory, command library mount |
| SSH key setup | Architect (install.sh) | Valid ed25519 keypair for target devices |
| Timeout cascade | Architect | h-ssh timeouts (120s per-command) < Core timeout (240s) < dispatcher timeout (280s) |

### 7.4 Contracts We Provide

| Contract | Consumer | What we guarantee |
|----------|----------|-------------------|
| CLI interface | Claude (via skills) | Flags documented in skills, exit codes 0/1/2, JSON output schema |
| JSON output schema | Claude, training pipeline | Array of `{device, host, vendor, ok, output/error, duration_ms}` |
| Summary line | Training pipeline (`export-traces.py`) | `[h-ssh] {JSON}` on stdout, greppable |
| Edit audit JSONL | Operations, compliance | One record per device per edit operation |
| Non-interactive safety | Core | Never hangs on missing TTY — exits with code 2 |
| Timeout compliance | Architect | Per-command timeout (120s) fits within Core's 240s limit |

---

## 8. Inline Device Specification (`--target`) (P1.2)

The `--target` flag enables ad-hoc device targeting without a CSV inventory file:

```bash
h-ssh.py -sC bgp \
  --target CR1:10.0.1.1:junos \
  --target CR2:10.0.1.2:arista \
  --target SW1:10.0.2.1:ssh
```

**Format:** `name:host:vendor` — vendor defaults to `junos` if omitted (`CR1:10.0.1.1` → vendor `junos`).

**Mutual exclusivity:** `--target` and `--devices` are mutually exclusive. If both are provided, exit with code 2.

This is critical for h-cli integration because the model often constructs device lists dynamically from user input ("check bgp on CR1 and CR2") or NetBox queries. Writing a temporary CSV would require an extra `run_command` call.

---

## 9. External Dependencies

| Package | Version | Used By | Notes |
|---------|---------|---------|-------|
| `paramiko` | >=3.0 | `generic.py`, `junos.py` | SSH client |
| `junos-eznc` | >=2.7.0 | `junos.py` | Juniper NETCONF (PyEZ) — config path |
| `httpx` | >=0.27 | `rest.py` | REST API transport (thread-safe sync client) |

`pyeapi` is not currently required — the Arista transport is a stub. `telnet.py` uses stdlib only (`socket`).

No additional Python packages beyond the above. All other functionality uses stdlib (`argparse`, `concurrent.futures`, `json`, `dataclasses`, `pathlib`, `logging`).

Installed in the Core container at build time via `pip install --no-cache-dir`.

---

## 10. Design Decisions

| Decision | Rationale |
|----------|-----------|
| **No dedicated MCP tool** | h-ssh runs via the existing `run_command` — adding a dedicated tool would bypass the Asimov firewall and require Core team changes for no benefit |
| **JSON output mode** | Machine-parseable output lets Claude programmatically extract per-device results instead of regex-parsing human-readable banners |
| **Fail-closed on missing TTY** | Exit with code 2 instead of hanging — the model sees a clear error and can report it to the user |
| **`--target` for inline devices** | Saves one `run_command` call per invocation — the model doesn't need to write a temp CSV first |
| **`WarningPolicy` for SSH** | Accepts new host keys (first connection) but rejects changed keys (MITM protection), matching h-cli's SSH config |
| **Mandatory `--commit-confirmed` in skill** | Auto-rollback safety net — even if the model makes a mistake, config reverts within the timeout window |
| **Mandatory `--dry-run` first in skill** | User sees diffs before any change is committed — defense-in-depth beyond the Asimov firewall |
| **Separate audit log for edits** | Edit operations are high-risk infrastructure changes that need their own change-control trail, separate from the general h-ssh execution log and the firewall audit |
| **`[h-ssh]` summary line prefix** | Greppable by `export-traces.py` without parsing the full command output — enables h-ssh metadata extraction in the training pipeline |
| **Vendor default to `junos`** | Juniper is the primary target network vendor — reduces verbosity for the common case |
| **Command shortcuts (.cmd files)** | Small, vendor-aware command maps that let the model use short names (`bgp`, `ospf`) instead of memorizing per-vendor syntax |

---

## 11. Integration Status

Based on the h-cli integration advice (`docs/h-ssh-integration-advice.md`):

| # | Change | Priority | Status |
|---|--------|----------|--------|
| B1 | `--password` flag / `HSSH_PASSWORD` env var | **Blocker** | Specified |
| B2 | Fail-closed when stdin is not a TTY | **Blocker** | Specified |
| P1.1 | `--json` output format | **P1** | Specified |
| P1.2 | `--target name:host:vendor` flag | **P1** | Specified |
| P1.3 | `--quiet` flag | **P1** | Specified |
| P1.4 | `--log-json` structured logging | **P1** | Specified |
| P1.5 | Summary JSON line to stdout | **P1** | Specified |
| P2.1 | Fix `AutoAddPolicy()` → `WarningPolicy()` | **P2** | Specified |
| P2.2 | Edit audit log (JSONL) | **P2** | Specified |

All items from the integration advice are addressed in this LLD. Implementation is next.
