---
keywords: h-ssh edit, deploy, configure, config, commit, rollback, set, delete, network config
rules:
  - NEVER skip --dry-run for the first pass — always preview before committing
  - ALWAYS use --commit-confirmed N for SSH and telnet-junos — auto-rollback safety net
  - ALWAYS show the diff to the user and get explicit confirmation before committing
  - ALWAYS use -y and --json on every h-ssh call
---
# h-ssh — Edit Commands (Safety-Critical)

h-ssh can push configuration changes to network devices via SSH, telnet, or REST API. This is a destructive operation — always follow the safety workflow.

## Mandatory Safety Workflow

**Step 1 — Dry run (ALWAYS do this first):**
```bash
h-ssh.py --user hcli --json -eB "set system ntp server 10.0.0.1" \
  --target CR1:10.0.1.1:junos --commit-confirmed 10 --dry-run -y
```

**Step 2 — Show diff to user, ask for confirmation.**

**Step 3 — Commit (only after user says yes):**
```bash
h-ssh.py --user hcli --json -eB "set system ntp server 10.0.0.1" \
  --target CR1:10.0.1.1:junos --commit-confirmed 10 -y \
  --audit-log /var/log/hcli/hssh/audit.log
```

## Edit Modes
- `-eC CMD` — Single config command to all devices
- `-eB CMD` — Broadcast same config to all devices
- `-eD DIR` — Per-device config files (DIR/DEVICENAME.set)

## Telnet Edit

Same safety workflow applies. Telnet edit handles vendor-specific config modes automatically:

- **IOS/Arista/NXOS:** enters `configure terminal`, sends commands, runs `end`
- **Junos:** enters `configure`, sends commands, runs `show | compare` for diff, then `commit` or `rollback`

```bash
# Dry run via telnet to EVE-NG console
h-ssh.py --user admin --json -eB "set system host-name LAB-R1" \
  --target R1:192.168.1.100:5001:telnet-junos --commit-confirmed 10 --dry-run -y

# IOS device via telnet
h-ssh.py --user admin --json -eB "hostname SW1-NEW" \
  --target SW1:192.168.1.100:5000:telnet-ios --dry-run -y
```

`--commit-confirmed` works with `telnet-junos` (sends `commit confirmed N` in Junos config mode).

## REST Edit

REST edit payload is a JSON string specifying method, path, and body:

```bash
echo '[
  {"target": "netbox:https://netbox.example.com:rest",
   "edit": "{\"method\": \"PATCH\", \"path\": \"/api/dcim/devices/1/\", \"body\": {\"name\": \"R1-NEW\"}}",
   "auth": {"scheme": "bearer", "token": "YOUR_API_TOKEN"}}
]' | h-ssh.py --user hcli --job - --json --quiet --dry-run
```

- **Dry run:** GETs current state, diffs against proposed body (field-level diff)
- **Commit:** sends the actual PATCH/POST/PUT request
- Supported methods: `PATCH`, `POST`, `PUT`

## Rules
- **NEVER** skip `--dry-run` for the first pass
- **ALWAYS** include `--commit-confirmed N` for SSH and telnet-junos (auto-rollback in N minutes)
- **ALWAYS** show the diff to the user and get confirmation before committing
- **ALWAYS** use `-y` and `--json`
- REST edit requires `--job` with `"auth"` field — same as REST show
- After commit, remind the user about the rollback timer
