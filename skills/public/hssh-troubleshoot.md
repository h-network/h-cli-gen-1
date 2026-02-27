---
keywords: troubleshoot, diagnose, debug, down, flapping, unreachable, timeout, network problem
rules:
  - ALWAYS use --json and -y on every h-ssh call
  - Start broad across all devices, then narrow to failed ones — never guess
  - Use h-ssh for structured parallel queries, tmux for interactive step-by-step debugging
---
# h-ssh — Troubleshooting Workflows

Use h-ssh to run diagnostic commands across multiple devices in parallel — via SSH, telnet, or REST API.

## Common Patterns — SSH

**Connectivity check:**
```bash
h-ssh.py --user hcli -sC "show interfaces terse" --target R1:10.0.1.1 --json -y
```

**BGP neighbor state:**
```bash
h-ssh.py --user hcli -sC bgp --target R1:10.0.1.1 --target R2:10.0.1.2 --json -y
```

**OSPF adjacency check:**
```bash
h-ssh.py --user hcli -sC ospf --target R1:10.0.1.1 --target R2:10.0.1.2 --json -y
```

**Route table summary:**
```bash
h-ssh.py --user hcli -sC routes --target R1:10.0.1.1 --json -y
```

## Common Patterns — Telnet (EVE-NG / Console)

**Lab device diagnostics (console ports):**
```bash
h-ssh.py --user admin -sC "show interfaces terse" \
  --target R1:192.168.1.100:5001:telnet-junos \
  --target R2:192.168.1.100:5002:telnet-junos \
  --json -y
```

**IOS device via console:**
```bash
h-ssh.py --user admin -sC "show ip route" \
  --target SW1:192.168.1.100:5000:telnet-ios --json -y
```

## Common Patterns — REST API

**Query device inventory:**
```bash
echo '[
  {"target": "netbox:https://netbox.example.com:rest",
   "show": "/api/dcim/devices/?status=active",
   "auth": {"scheme": "bearer", "token": "YOUR_API_TOKEN"}}
]' | h-ssh.py --user hcli --job - --json --quiet
```

## Mixed Diagnostics — All Transports

Query production routers, lab devices, and APIs in a single parallel run:

```bash
echo '[
  {"target": "CR1:10.0.1.1:junos", "show": "show bgp summary"},
  {"target": "LAB-R1:192.168.1.100:5001:telnet-junos", "show": "show bgp summary"},
  {"target": "netbox:https://netbox.example.com:rest",
   "show": "/api/dcim/devices/?status=failed",
   "auth": {"scheme": "bearer", "token": "YOUR_API_TOKEN"}}
]' | h-ssh.py --user hcli --job - --json --quiet
```

## Iterative Workflow
1. Start broad — check interfaces, routing protocols across all devices
2. Narrow down — focus on failed devices with specific show commands
3. Compare — run same command on working vs. broken device
4. Report — summarize findings with device names, states, and suggested action

For interactive debugging (step-by-step CLI exploration), use tmux panes instead of h-ssh.
