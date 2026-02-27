---
keywords: h-ssh, hssh, show, bgp, ospf, interfaces, routes, lldp, network devices, check, router, switch
---
# h-ssh — Show Commands

h-ssh runs show commands on network devices in parallel via SSH.

## Quick Reference

```bash
# Single device
h-ssh.py --user hcli -sC "show bgp summary" --target CR1:10.0.1.1:junos --json -y

# Multiple devices (parallel)
h-ssh.py --user hcli -sC "show interfaces terse" \
  --target CR1:10.0.1.1:junos \
  --target CR2:10.0.1.2:junos \
  --json -y

# Command shortcuts (bgp, ospf, interfaces, routes, lldp)
h-ssh.py --user hcli -sC bgp --target CR1:10.0.1.1 --json -y

# Different commands per device (job file via stdin)
echo '[
  {"target": "CR1:10.0.1.1:junos", "show": "show bgp summary"},
  {"target": "CR2:10.0.1.2:junos", "show": "show interfaces terse"}
]' | h-ssh.py --user hcli --job - --json --quiet
```

## Output

With `--json`, returns a JSON array:
```json
[
  {"device": "CR1", "host": "10.0.1.1", "vendor": "junos", "ok": true, "command": "show bgp summary", "output": "...", "duration_ms": 380},
  {"device": "CR2", "host": "10.0.1.2", "vendor": "junos", "ok": false, "error": "connection timeout", "duration_ms": 30000}
]
```

## Rules
- Always use `--json` for machine-parseable output
- Always use `-y` to prevent hanging on prompts
- `--target` format: `name:host:vendor` (vendor defaults to `junos`)
- Parse JSON output to summarize results for the user
- Report both successes and failures clearly
