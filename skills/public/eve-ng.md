---
keywords: eve-ng, eveng, eve, lab, topology, node, unetlab, unl, qemu, console port, bootstrap, factory default
rules:
  - NEVER assume console port order matches node creation order — ALWAYS query API or pgrep first
  - Run fixpermissions after ANY XML edit — no exceptions
  - Re-authenticate before long API sequences — cookie expiry (~10min idle) is the #1 failure mode
  - Atomic XML writes only — write to /tmp then mv, never write in-place
  - Always use tmux for bootstrap — netcat is blind, no output verification
  - NEVER use shell for-loops for API calls — agent makes each call directly to detect/react to auth failures
  - Read EVE_NG_URL, EVE_NG_USERNAME, EVE_NG_PASSWORD from env — don't prompt or guess
  - Strip trailing slash from EVE_NG_URL — double-slash breaks API paths
  - Never start/stop/wipe nodes via SSH/QEMU — lifecycle is REST API only
  - Use bulk endpoints (/nodes, /networks) — don't fetch per-node
  - Break bootstrap into small sequential steps — never one giant run_command
  - Send config in batches of 3 lines across all panes, sleep 1s between batches, verify only at the end
  - API user and GUI user MUST be the same — mismatched pods cause invisible nodes
  - Interface type is "ethernet" not "raw" — raw causes PHP parser $s errors
---
# EVE-NG Lab Automation

## Three Planes — never mix them

| Plane | Transport | Purpose |
|-------|-----------|---------|
| Control | REST API (HTTPS) | Lab CRUD, start/stop/wipe nodes |
| Management | SSH to EVE-NG host | XML editing, fixpermissions, port discovery |
| Data | Telnet/SSH to device consoles | Device configuration, verification |

QEMU lifecycle (start/stop/wipe) is REST API only — never run QEMU commands via SSH.

## Pod/Tenant Isolation

Each EVE-NG PRO user gets a unique pod ID (admin=pod 0, hcli=pod 1). Nodes from one pod are **invisible** to another. QEMU processes carry `-T <pod_id>`.

**API user and GUI user MUST be the same.** If the agent authenticates as `hcli` (pod 1) but the operator views the GUI as `admin` (pod 0), nodes appear missing and starts seem to fail. Our env: `EVE_NG_USERNAME=hcli`, operator uses `hcli` in GUI.

## Control Plane — REST API

### Credentials

Read `EVE_NG_URL`, `EVE_NG_USERNAME`, `EVE_NG_PASSWORD` from env. Strip trailing slash from URL before use.

### Auth

Session-based cookie auth. Cookie expires ~10 minutes idle.

```bash
curl -s -b /tmp/eve-cookies -c /tmp/eve-cookies \
  -X POST -H "Content-Type: application/json" \
  -d '{"username":"'"$EVE_NG_USERNAME"'","password":"'"$EVE_NG_PASSWORD"'"}' \
  "${EVE_NG_URL}/api/auth/login"
```

**Auth is the #1 failure mode.** Re-authenticate BEFORE long sequences, not after the first 401. Verify cookie validity before blaming endpoints.

### Lab creation workflow

```
 1. POST /api/auth/login                        — authenticate
 2. GET  /api/folders/                           — check if lab exists
 3. POST /api/labs                               — create lab
 4. GET  /api/list/templates/                    — available templates (CACHEABLE)
 5. GET  /api/list/templates/{template}          — template defaults (CACHEABLE)
 6. POST /api/labs/{lab}.unl/nodes               — create nodes (loop)
 7. GET  /api/list/networks                      — network types (CACHEABLE)
 8. POST /api/labs/{lab}.unl/networks            — create networks
 9. Wire interfaces                              — edit lab XML directly
10. GET  /api/labs/{lab}.unl/nodes               — verify topology (bulk)
11. GET  /api/labs/{lab}.unl/nodes/{id}/start    — start nodes individually
```

Cache steps 4/5/7. Use bulk `GET /nodes` and `GET /networks` — don't fetch per-node.

### Starting nodes

Bulk `/nodes/start` returns error 60027 on 6.4.0-78-PRO. Use individual `/nodes/{id}/start` (~1.5s each, no sleep needed).

**Agent makes each call directly — never a shell for-loop.** Auth cookies expire mid-loop; a blind loop can't detect or recover from 401s.

### Key errors

| Error | Meaning | Fix |
|-------|---------|-----|
| 60027 | Bulk start broken | Individual `/nodes/{id}/start` |
| 60061 | Lab locked by GUI | Close browser tab or wait for session expiry |
| 401 | Cookie expired | Re-authenticate |

GUI does not reflect API state. If nodes appear missing, check pod mismatch first.

## Management Plane — XML

Labs are `.unl` files under `/opt/unetlab/labs/`. ALL attributes below are required — the PHP parser throws "Undefined variable $s" when any are missing.

### Minimal correct node XML

```xml
<node id="1" name="R1" type="qemu" template="vqfx" config="0" cpulimit="0">
  <interface id="0" name="fxp0" type="ethernet" network_id="99"
    vid="" labelpos="" stub="" width="" curviness=""
    beziercurviness="" round="" midpoint="" srcpos="" dstpos=""/>
  <interface id="1" name="ge-0/0/0" type="ethernet" network_id="1"
    vid="" labelpos="" stub="" width="" curviness=""
    beziercurviness="" round="" midpoint="" srcpos="" dstpos=""/>
</node>
```

**Interface `type` MUST be `"ethernet"`, not `"raw"`.** `type="raw"` causes the $s parser bug.

- `id="0"` = management interface, `id="1"+` = data plane
- `config="0"` = factory default, `config="1"` = saved startup config
- Cosmetic attributes (vid, labelpos, stub, etc.) can be empty but MUST be present

### Minimal correct network XML

```xml
<network id="1" name="R1-R2" type="bridge" smart="0" vlan8021ad=""
  style="" linkstyle="" color="" label="" icon=""
  width="" left="200" top="150" visibility="0"/>

<network id="99" name="mgmt" type="pnet0" smart="0" vlan8021ad=""
  style="" linkstyle="" color="" label="" icon=""
  width="" left="400" top="50" visibility="1"/>
```

- `type="bridge" visibility="0"` = P2P link (invisible), `type="pnet0"` = management bridge
- All cosmetic attributes (style, linkstyle, color, etc.) can be empty but MUST be present

### After any XML edit

```bash
/opt/unetlab/wrappers/unl_wrapper -a fixpermissions
```

Never skip this. Always use atomic writes: write to `/tmp/lab.unl.tmp` then `mv` to final path.

## Data Plane — Console Access

Console ports are dynamic — never hardcode. Discover via `GET /nodes` (bulk) or `pgrep -a qemu_wrapper` (parse `-C` port, `-D` node ID).

**ALWAYS verify port-to-node mapping before bootstrap.** This is the #1 bootstrap failure mode — wrong port = config pushed to wrong device.

### Bootstrap workflow

Use tmux (not netcat — netcat is blind). Break into small sequential agent calls, never one giant script.

```
1. Discover ports — GET /nodes bulk, build node-to-port mapping
2. Open tmux panes — one per device, telnet to verified port
3. Confirm identity — capture-pane | tail, check hostname/prompt
4. Send config in batches (see batched send pattern below)
5. Final verify — capture-pane | tail on each pane, report results
6. Verify SSH — confirm bootstrap complete
7. Kill panes — clean up
```

### Batched send pattern

Send commands in **batches of 3 lines** across all panes. Do NOT check terminal output between batches — sweeping after every command burns context and time.

```
For each batch of 3 config lines:
  For each pane:
    send-keys -l "line 1" → Enter
    send-keys -l "line 2" → Enter
    send-keys -l "line 3" → Enter
  sleep 1
Repeat until all config lines are sent.

After ALL batches are sent:
  For each pane:
    capture-pane | tail -20 → verify success
  Report results.
```

- **3 lines per batch** across all panes, then sleep 1s, then next batch
- **No capture-pane between batches** — trust the send, verify once at the end
- **One final sweep** after all commands are sent — check every pane, report pass/fail
- If the final check shows failures, re-send only the failed commands

## Decision Framework

| Situation | Plane | Tool |
|-----------|-------|------|
| Create/delete labs, start/stop nodes | Control | REST API |
| Edit lab XML, fix permissions | Management | SSH to host |
| Discover console ports | Control/Mgmt | GET /nodes or pgrep |
| Bootstrap factory-default device | Data | tmux + telnet |
| Show/edit commands on booted devices | Data | h-ssh |
| Debug interactively | Data | tmux pane |
