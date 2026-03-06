---
keywords: h-ssh, hssh, show, bgp, ospf, interfaces, routes, lldp, network devices, check, router, switch, rest, api, telnet
rules:
  - ALWAYS use --json and -y on every h-ssh call — no exceptions
  - Telnet target format requires port — name:host:port:vendor
  - REST auth requires --job with "auth" field — cannot use --target alone
  - Parse JSON output to summarize results — report both successes and failures
---
# h-ssh — Show Commands

h-ssh runs show commands on network devices in parallel via SSH, telnet, or REST API.

## Transports

| Transport | Vendor string | Target format | Use case |
|-----------|--------------|---------------|----------|
| SSH | `junos`, `arista`, `generic` | `name:host[:port]:vendor` | Production routers/switches with SSH |
| Telnet | `telnet-ios`, `telnet-junos`, `telnet-arista`, `telnet-nxos`, `telnet` | `name:host:port:vendor` | Console ports (EVE-NG), legacy devices |
| REST | `rest` | `name:https://host.example.com:rest` | APIs (NetBox, controllers, etc.) |

## Quick Reference — SSH

```bash
# Single device (uses ~/.ssh/config for user, port, key)
h-ssh.py -sC "show bgp summary" --target CR1:10.0.1.1:junos --json -y

# Explicit user override
h-ssh.py --user hcli -sC "show bgp summary" --target CR1:10.0.1.1:junos --json -y

# Multiple devices (parallel)
h-ssh.py -sC "show interfaces terse" \
  --target CR1:10.0.1.1:junos \
  --target CR2:10.0.1.2:junos \
  --json -y

# Custom SSH port via target
h-ssh.py -sC "show version" --target SRV1:server1.example.com:8023:generic --json -y

# Command shortcuts (bgp, ospf, interfaces, routes, lldp)
h-ssh.py -sC bgp --target CR1:10.0.1.1 --json -y
```

## Quick Reference — Telnet

```bash
# EVE-NG console port (port required)
h-ssh.py --user admin -sC "show ip route" \
  --target SW1:192.168.1.100:5000:telnet-ios --json -y

# Multiple telnet devices
h-ssh.py --user admin -sC "show interfaces terse" \
  --target R1:192.168.1.100:5001:telnet-junos \
  --target R2:192.168.1.100:5002:telnet-junos \
  --json -y
```

Telnet handles login sequences, ANSI stripping, and `--More--` pagination automatically.

## Quick Reference — REST API

REST auth is only supported via `--job` (not `--target`). Use the job file format:

```bash
echo '[
  {"target": "netbox:https://netbox.example.com:rest",
   "show": "/api/dcim/devices/",
   "auth": {"scheme": "bearer", "token": "YOUR_API_TOKEN"}}
]' | h-ssh.py --job - --json --quiet
```

REST `show()` = GET request to the API path. Pagination via `next` field is handled automatically.

**Auth schemes:** `bearer`, `basic` (value: `user:password`), `x-auth-token`, `token`, or any custom header name as the scheme.

## Mixed Job — SSH + REST + Telnet

Different transports in a single parallel job:

```bash
echo '[
  {"target": "CR1:10.0.1.1:junos", "show": "show bgp summary"},
  {"target": "netbox:https://netbox.example.com:rest",
   "show": "/api/dcim/devices/",
   "auth": {"scheme": "bearer", "token": "YOUR_API_TOKEN"}},
  {"target": "SW1:192.168.1.100:5000:telnet-ios", "show": "show ip route"}
]' | h-ssh.py --job - --json --quiet
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
- `--user` is optional — falls back to `~/.ssh/config` (User, Port, IdentityFile), then system user
- SSH `--target` format: `name:host[:port]:vendor` (vendor defaults to `junos`)
- Telnet `--target` format: `name:host:port:vendor` (port is required)
- REST auth requires `--job` with `"auth"` field — cannot use `--target` alone
- Parse JSON output to summarize results for the user
- Report both successes and failures clearly
