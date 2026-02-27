---
keywords: tmux, pane, console, interactive, debug, session, terminal, cli, long-running, screen, bootstrap, provision
rules:
  - NEVER capture-pane without piping through tail -20 (or tail -30 max) — raw scrollback burns context tokens
  - ALWAYS use send-keys -l for text — never omit the -l flag
  - ALWAYS send Enter as a separate send-keys call — never combine with text
  - ALWAYS close panes when done — no orphans
---
# tmux — Pane Sessions & Parallel Bootstrap

tmux panes serve two roles: interactive sessions for unpredictable work, and the **primary transport for telnet bootstrap** of factory-default devices. Both use the same send-keys/capture-pane pattern.

## When to use tmux vs h-ssh

| Use h-ssh | Use tmux |
|-----------|----------|
| Show commands on booted devices (SSH) | **Bootstrap factory-default devices (telnet)** |
| Parallel structured commands | Console access (serial/telnet) |
| JSON-parseable output needed | Interactive CLI sessions (configure mode, debug) |
| Same command on many devices | Long-running processes (tcpdump, monitor) |
| Config deploy with dry-run | Exploratory debugging (step-by-step) |

**Rule of thumb:** if the device has SSH and you want structured output, use h-ssh. If the device only has telnet (factory default, console port), or you need to interact/explore, use tmux.

## Opening a pane

Split the current window to create a work pane:

```bash
tmux split-window -h          # horizontal split (side by side)
tmux split-window -v          # vertical split (top/bottom)
```

Target a specific session/window:
```bash
tmux split-window -h -t mysession:0
```

## Sending commands

**Always use `-l` (literal) and send Enter separately.** This prevents shell interpretation issues and lets you control exactly what is typed.

```bash
tmux send-keys -t mysession:0.1 -l "show interfaces terse"
tmux send-keys -t mysession:0.1 Enter
```

**Multiple commands — send each as a pair:**
```bash
tmux send-keys -t mysession:0.1 -l "configure"
tmux send-keys -t mysession:0.1 Enter
# wait for prompt, then next command
tmux send-keys -t mysession:0.1 -l "set system host-name R1"
tmux send-keys -t mysession:0.1 Enter
```

**Never combine text and Enter in one send-keys call.** Always separate them.

## Reading pane output

**CRITICAL: ALWAYS pipe `capture-pane` through `tail`. Never dump raw `capture-pane` output — full scrollback buffers burn context tokens and can blow out your budget.**

```bash
# CORRECT — always tail
tmux capture-pane -t mysession:0.1 -p | tail -20

# WRONG — never do this
tmux capture-pane -t mysession:0.1 -p
tmux capture-pane -t mysession:0.1 -p -S -500
```

Use `tail -20` by default. Use `tail -30` max when you need more context. Never go higher.

**Workflow: send a command, wait, then read the result:**
```bash
tmux send-keys -t mysession:0.1 -l "show route summary"
tmux send-keys -t mysession:0.1 Enter
sleep 2
tmux capture-pane -t mysession:0.1 -p | tail -20
```

Adjust the sleep based on expected command duration. For slow commands (e.g. debug output), wait longer or poll with `capture-pane | tail` until the prompt returns.

## Common use cases

### Console access (serial/telnet)
```bash
tmux split-window -h
tmux send-keys -t mysession:0.1 -l "telnet 192.168.1.1 23"
tmux send-keys -t mysession:0.1 Enter
sleep 2
# read login prompt
tmux capture-pane -t mysession:0.1 -p | tail -20
# send credentials
tmux send-keys -t mysession:0.1 -l "admin"
tmux send-keys -t mysession:0.1 Enter
sleep 1
tmux send-keys -t mysession:0.1 -l "password123"
tmux send-keys -t mysession:0.1 Enter
```

### Debug / monitor sessions
```bash
tmux split-window -v
tmux send-keys -t mysession:0.1 -l "ssh admin@10.0.1.1"
tmux send-keys -t mysession:0.1 Enter
sleep 3
# start debug (long-running, streams output)
tmux send-keys -t mysession:0.1 -l "monitor traffic interface ge-0/0/0"
tmux send-keys -t mysession:0.1 Enter
# let it run, capture periodically
sleep 10
tmux capture-pane -t mysession:0.1 -p | tail -20
```

### Interactive CLI (configure mode, shell)
```bash
tmux split-window -h
tmux send-keys -t mysession:0.1 -l "ssh admin@10.0.1.1"
tmux send-keys -t mysession:0.1 Enter
sleep 3
tmux send-keys -t mysession:0.1 -l "configure"
tmux send-keys -t mysession:0.1 Enter
sleep 1
# now in configure mode — send config commands
tmux send-keys -t mysession:0.1 -l "set interfaces ge-0/0/0 unit 0 family inet address 10.1.1.1/30"
tmux send-keys -t mysession:0.1 Enter
sleep 1
tmux send-keys -t mysession:0.1 -l "show | compare"
tmux send-keys -t mysession:0.1 Enter
sleep 2
tmux capture-pane -t mysession:0.1 -p | tail -30
```

### Long-running processes
```bash
tmux split-window -v
tmux send-keys -t mysession:0.1 -l "tcpdump -i eth0 -c 100"
tmux send-keys -t mysession:0.1 Enter
# check progress later
sleep 30
tmux capture-pane -t mysession:0.1 -p | tail -20
```

## Parallel bootstrap — N devices via N panes

tmux is the correct transport for bootstrapping multiple factory-default devices in parallel. Open one pane per device, send configs via send-keys, then sweep all panes with capture-pane to verify.

**Step 1 — Open panes (one per device):**
```bash
# Open N panes — each gets a telnet session to a console port
tmux split-window -h -t mysession:0
tmux send-keys -t mysession:0.1 -l "telnet EVE_HOST PORT1"
tmux send-keys -t mysession:0.1 Enter

tmux split-window -h -t mysession:0
tmux send-keys -t mysession:0.2 -l "telnet EVE_HOST PORT2"
tmux send-keys -t mysession:0.2 Enter

tmux split-window -h -t mysession:0
tmux send-keys -t mysession:0.3 -l "telnet EVE_HOST PORT3"
tmux send-keys -t mysession:0.3 Enter
```

**Step 2 — Send config to each pane:**
```bash
# Send same bootstrap commands to all panes (loop or sequential)
for pane in 1 2 3; do
  tmux send-keys -t mysession:0.$pane -l "configure"
  tmux send-keys -t mysession:0.$pane Enter
done
sleep 2
for pane in 1 2 3; do
  tmux send-keys -t mysession:0.$pane -l "set system root-authentication plain-text-password"
  tmux send-keys -t mysession:0.$pane Enter
done
```

**Step 3 — Sweep all panes to verify:**
```bash
# Check each pane's output after commands complete
for pane in 1 2 3; do
  echo "=== Pane $pane ==="
  tmux capture-pane -t mysession:0.$pane -p | tail -20
done
```

**Step 4 — Cleanup when done:**
```bash
for pane in 3 2 1; do
  tmux kill-pane -t mysession:0.$pane
done
```

**Why tmux, not netcat:** netcat fire-and-forget is blind — no output verification, no error detection, can't tell if a commit failed or the device was still booting. tmux gives full visibility: you send, you verify, you know. Use netcat only for truly idempotent throwaway operations where failure is acceptable.

## Closing / cleanup

**Always close panes when done.** Don't leave orphaned sessions.

```bash
# Gracefully exit whatever is running
tmux send-keys -t mysession:0.1 -l "exit"
tmux send-keys -t mysession:0.1 Enter
sleep 1

# Kill the pane
tmux kill-pane -t mysession:0.1
```

If the process won't exit cleanly, send Ctrl-C first:
```bash
tmux send-keys -t mysession:0.1 C-c
sleep 1
tmux send-keys -t mysession:0.1 -l "exit"
tmux send-keys -t mysession:0.1 Enter
sleep 1
tmux kill-pane -t mysession:0.1
```

## Listing active panes

Check what's running before creating new panes:
```bash
tmux list-panes -t mysession -F "#{pane_index}: #{pane_current_command} (#{pane_width}x#{pane_height})"
```

## Rules

- **NEVER run `capture-pane` without piping through `tail -20` (or `tail -30` max) — raw scrollback burns context tokens**
- Always use `send-keys -l` for text — never omit the `-l` flag
- Always send `Enter` as a separate `send-keys` call
- Always `capture-pane | tail` to verify command output before acting on results
- Always close panes when the task is done — no orphans
- Use `sleep` between send and capture to let commands complete
- For credentials, prefer environment variables over hardcoded values
- If a session already has work panes, reuse them instead of splitting more
