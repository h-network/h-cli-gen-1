---
keywords: troubleshoot, diagnose, debug, down, flapping, unreachable, timeout, network problem
---
# h-ssh — Troubleshooting Workflows

Use h-ssh to run diagnostic commands across multiple devices in parallel.

## Common Patterns

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

## Iterative Workflow
1. Start broad — check interfaces, routing protocols across all devices
2. Narrow down — focus on failed devices with specific show commands
3. Compare — run same command on working vs. broken device
4. Report — summarize findings with device names, states, and suggested action
