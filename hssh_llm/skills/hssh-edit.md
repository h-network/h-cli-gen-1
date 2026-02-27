---
keywords: h-ssh edit, deploy, configure, config, commit, rollback, set, delete, network config
---
# h-ssh — Edit Commands (Safety-Critical)

h-ssh can push configuration changes to network devices. This is a destructive operation — always follow the safety workflow.

## Mandatory Safety Workflow

**Step 1 — Dry run (ALWAYS do this first):**
```bash
h-ssh.py --json -eB "set system ntp server 10.0.0.1" \
  --target CR1:10.0.1.1:junos --commit-confirmed 10 --dry-run -y
```

**Step 2 — Show diff to user, ask for confirmation.**

**Step 3 — Commit (only after user says yes):**
```bash
h-ssh.py --json -eB "set system ntp server 10.0.0.1" \
  --target CR1:10.0.1.1:junos --commit-confirmed 10 -y \
  --audit-log /var/log/hcli/hssh/audit.log
```

## Edit Modes
- `-eC CMD` — Single config command to all devices
- `-eB CMD` — Broadcast same config to all devices
- `-eD DIR` — Per-device config files (DIR/DEVICENAME.set)

## Rules
- **NEVER** skip `--dry-run` for the first pass
- **ALWAYS** include `--commit-confirmed N` (auto-rollback in N minutes)
- **ALWAYS** show the diff to the user and get confirmation before committing
- **ALWAYS** use `-y` and `--json`
- `--user` is optional — falls back to SSH config, then system user
- After commit, remind the user about the rollback timer
