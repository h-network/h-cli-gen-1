---
keywords: troubleshoot, diagnose, debug, down, flapping, unreachable, timeout, network problem
---
# h-ssh — Troubleshooting Workflows

Use h-ssh to run diagnostic commands across multiple devices in parallel.

## Common Patterns

**Connectivity check:**
```bash
h-ssh.py -sC "show interfaces terse" --target R1:10.0.1.1 --json -y
```

**BGP neighbor state:**
```bash
h-ssh.py -sC bgp --target R1:10.0.1.1 --target R2:10.0.1.2 --json -y
```

**OSPF adjacency check:**
```bash
h-ssh.py -sC ospf --target R1:10.0.1.1 --target R2:10.0.1.2 --json -y
```

**Route table summary:**
```bash
h-ssh.py -sC routes --target R1:10.0.1.1 --json -y
```

**Device on non-standard SSH port:**
```bash
h-ssh.py -sC "show version" --target SRV1:server1.example.com:8023:ssh --json -y
```

## Iterative Workflow
1. Start broad — check interfaces, routing protocols across all devices
2. Narrow down — focus on failed devices with specific show commands
3. Compare — run same command on working vs. broken device
4. Report — summarize findings with device names, states, and suggested action

## Notes
- `--user` is optional — h-ssh respects `~/.ssh/config` (User, Port, IdentityFile)
- Use `--user` only when you need to override the SSH config
