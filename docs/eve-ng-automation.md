# EVE-NG Automation Notes

Hard-won operational knowledge from automating EVE-NG PRO with h-cli. These notes cover what actually works, what doesn't, and the fastest methods for lab automation.

---

## The Golden Rule: SSH > REST API

The EVE-NG REST API is slow and partially broken. SSH to the EVE-NG host is 6x faster and more reliable.

| Method | Speed | Reliability | Auth |
|--------|-------|-------------|------|
| REST API | ~4 minutes per lab | Broken endpoints, session timeouts | Cookie-based, expires ~10min |
| SSH + unl_wrapper | ~42 seconds per lab | Solid | SSH keys (already set up) |

**Use the REST API only for read-only queries where SSH isn't available** (e.g., `GET /api/status`, `GET /api/list/templates/`). For all lab operations: **SSH first**.

---

## SSH Command Reference

### Lab Management

```bash
# List all labs
ssh eve-host "ls -la /opt/unetlab/labs/*.unl"

# Read lab topology (XML)
ssh eve-host "cat /opt/unetlab/labs/{labname}.unl"

# Create/edit lab (write XML file)
ssh eve-host "sudo tee /opt/unetlab/labs/{labname}.unl" < local_file.xml

# Fix permissions after editing
ssh eve-host "sudo chown www-data:www-data /opt/unetlab/labs/{labname}.unl"

# Delete lab
ssh eve-host "sudo /opt/unetlab/wrappers/unl_wrapper -a delete -T 0 -F /opt/unetlab/labs/{labname}.unl"
```

### Node Control (via unl_wrapper)

```bash
# Start all nodes in a lab
ssh eve-host "sudo /opt/unetlab/wrappers/unl_wrapper -a start -T 0 -F /opt/unetlab/labs/{labname}.unl"

# Start single node
ssh eve-host "sudo /opt/unetlab/wrappers/unl_wrapper -a start -T 0 -D {node_id} -F /opt/unetlab/labs/{labname}.unl"

# Stop all nodes
ssh eve-host "sudo /opt/unetlab/wrappers/unl_wrapper -a stop -T 0 -F /opt/unetlab/labs/{labname}.unl -m 1"

# Wipe node configs
ssh eve-host "sudo /opt/unetlab/wrappers/unl_wrapper -a wipe -T 0 -F /opt/unetlab/labs/{labname}.unl"
```

---

## Dynamic Console Port Discovery

EVE-NG PRO assigns console ports dynamically (range 1-65000). Ports change every time nodes restart. You **must** discover them at runtime.

```bash
# Find all console ports with router names
ssh eve-host "pgrep -a qemu_wrapper | grep '{labname}'"
```

**Example output:**
```
76678 /opt/unetlab/wrappers/qemu_wrapper -C 56611 -T 0 -D 1 -t R1 -F ...
77583 /opt/unetlab/wrappers/qemu_wrapper -C 51035 -T 0 -D 2 -t R2 -F ...
125361 /opt/unetlab/wrappers/qemu_wrapper -C 33735 -T 0 -D 3 -t R3 -F ...
```

**Key flags:**
| Flag | Meaning |
|------|---------|
| `-C <port>` | Console telnet port (the one you need) |
| `-T <tenant>` | Tenant ID (usually 0) |
| `-D <id>` | Device/Node ID |
| `-t <name>` | Router name |

---

## Console Automation via Telnet

Once you have the dynamic port, connect via telnet for fully autonomous router interaction.

### Using h-ssh (recommended)

h-ssh has a built-in telnet transport (`hssh_llm/h-ssh/transports/telnet.py`) with ANSI stripping, `--More--` handling, per-vendor prompt detection, and login sequences. Use `telnet-*` vendor types:

```bash
# Single device
h-ssh.py --user root --target R1:eve-host:56611:telnet-junos --show "show ospf neighbor"

# Parallel batch (all routers)
h-ssh.py --user root --job - --json --quiet <<'EOF'
[
  {"target": "R1:eve-host:56611:telnet-junos", "show": "show ospf neighbor"},
  {"target": "R2:eve-host:51035:telnet-junos", "show": "show ospf neighbor"}
]
EOF
```

### Raw socket connection (Python)

> **Note:** Python 3.13 removed `telnetlib`. Use raw sockets instead.

```python
import socket
import time

host = "eve-host-ip"
port = 56611  # From dynamic discovery

sock = socket.create_connection((host, port), timeout=10)
time.sleep(2)
sock.sendall(b"\n")
time.sleep(1)
```

### Juniper vJunOS: Discover Router Info

```python
sock.sendall(b"show chassis hardware | no-more\n")
time.sleep(2)

sock.sendall(b"show version | no-more\n")
time.sleep(2)

sock.sendall(b"show interfaces terse | no-more\n")
time.sleep(2)

output = sock.recv(65535).decode('ascii', errors='ignore')
```

### Juniper vJunOS: Configure Router

```python
sock.sendall(b"configure\n")
time.sleep(1)

sock.sendall(b"set system host-name R1\n")
time.sleep(1)

# Root password REQUIRED for commit on Juniper
sock.sendall(b"set system root-authentication plain-text-password\n")
time.sleep(1)
sock.sendall(b"YourPassword\n")
time.sleep(1)
sock.sendall(b"YourPassword\n")
time.sleep(2)

sock.sendall(b"commit and-quit\n")
time.sleep(4)

output = sock.recv(65535).decode('ascii', errors='ignore')
sock.close()
```

---

## EVE-NG Interface Mapping (Critical)

**fxp0 is management only. Never use it for data plane.**

| Interface ID (XML) | Juniper Interface | Purpose |
|---------------------|-------------------|---------|
| `id="0"` | fxp0 | Management (auto-added, leave unconnected) |
| `id="1"` | ge-0/0/0 | Data plane |
| `id="2"` | ge-0/0/1 | Data plane |
| `id="3"` | ge-0/0/2 | Data plane |
| ... | ... | ... |

### P2P Topology in XML

Each point-to-point link needs its own invisible bridge (`visibility="0"`):

```xml
<network id="1" type="bridge" name="R1-R2" visibility="0" />
<network id="2" type="bridge" name="R2-R3" visibility="0" />
<network id="3" type="bridge" name="R3-R1" visibility="0" />
```

Then connect node interfaces to bridges in the `<interface>` elements.

---

## REST API Reference (read-only use)

```
POST   /api/auth/login              # Auth with credentials (session cookie)
GET    /api/status                   # System status
GET    /api/list/templates/          # Available node types
GET    /api/labs/{lab}.unl/topology  # Lab topology
POST   /api/labs/{lab}.unl/nodes     # Create node (works)
GET    /api/labs/{lab}.unl/nodes/{id}/start  # Start node (works)
```

**Known broken endpoints:**
- `POST /api/labs/{name}.unl` — Lab creation returns 404/412
- `PUT /api/labs/{name}.unl` — Same

**Session timeout:** ~10 minutes of inactivity. Plan accordingly.

**Lab locking:** Cannot modify via API if lab is open in the web UI (error 60061).

---

## OSPF Configuration (Juniper)

```
configure
set routing-options router-id <ip>
set protocols ospf area 0.0.0.0 interface <if-name>
set protocols ospf area 0.0.0.0 interface <if-name> interface-type p2p
commit and-quit
```

Verify:
```
show ospf neighbor
show route protocol ospf
```

---

## SSH Bootstrap (Enable SSH on Fresh Router)

Fresh Juniper vJunOS routers only have telnet console access. To enable SSH:

```
# Via telnet console, logged in as root
cli
configure
set system root-authentication plain-text-password
  # Enter password twice
set system login user hcli class super-user
set system login user hcli authentication plain-text-password
  # Enter password twice
set system login user hcli authentication ssh-ed25519 "<pubkey>"
set system services ssh
set system host-name R1
commit and-quit
```

After this, SSH directly instead of telnet.

---

## Juniper Quirks

- `commit` fails without `root-authentication` configured — always set a root password first
- `commit and-quit` commits and exits config mode in one step
- `| no-more` prevents paging in output (essential for automation)
- `run <command>` executes operational commands from config mode
- Default login: `root` with no password
- Model: vMX (virtual MX series), version 24.2R1-S2.5, runs FreeBSD kernel
- Interfaces: ge-0/0/0 through ge-0/0/9 (10 data ports) + fxp0 (management) + lo0

---

## Complete Autonomous Workflow Example

**User says:** "Create 3 routers, connect them as a ring, configure OSPF"

1. Generate XML topology with 3 nodes + 3 invisible bridges
2. SSH to EVE-NG host, write XML to `/opt/unetlab/labs/{lab}.unl`
3. Fix permissions (`chown www-data:www-data`)
4. Start nodes via `unl_wrapper`
5. Wait for boot (~30s for vJunOS)
6. Discover dynamic console ports via `pgrep -a qemu_wrapper`
7. For each router via telnet:
   - Set hostname, root password
   - Configure interface IPs (`/30` subnets)
   - Configure OSPF area 0 on P2P interfaces
   - Commit
8. Verify OSPF adjacencies: `show ospf neighbor`
9. Report back with summary

**Total time:** ~5 minutes from request to verified OSPF mesh.
