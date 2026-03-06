#!/usr/bin/env python3
"""h-ssh — Parallel SSH/NETCONF tool for network device orchestration."""

import argparse
import csv
import getpass
import json as json_mod
import os
import sys
import time
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from core.runner import Device, run_parallel
from core.output import (
    print_start, print_result, format_human, format_json,
    format_summary, print_summary_human, write_log_json,
)
from core.audit import write_audit_entry


def parse_target(value: str) -> Device:
    """Parse target string: NAME:HOST[:PORT][:VENDOR].

    Handles URL-style hosts (e.g., nb:https://netbox.example.com:8443:rest)
    and port-style targets (e.g., SW1:192.168.1.100:5000:telnet-ios).
    """
    first_colon = value.find(":")
    if first_colon < 0:
        raise argparse.ArgumentTypeError(f"Invalid target format: {value!r} (expected name:host or name:host:vendor)")

    name = value[:first_colon]
    remainder = value[first_colon + 1:]

    if "://" in remainder:
        # URL-style host: vendor is after the last colon (if not a port number)
        last_colon = remainder.rfind(":")
        if last_colon > 0:
            potential_vendor = remainder[last_colon + 1:]
            if not potential_vendor.isdigit():
                host = remainder[:last_colon]
                return Device(name=name, host=host, vendor=potential_vendor)
        # No vendor or last segment is a port — default vendor
        return Device(name=name, host=remainder, vendor="junos")
    else:
        # Simple split for non-URL hosts
        parts = value.split(":")
        if len(parts) == 2:
            return Device(name=parts[0], host=parts[1], vendor="junos")
        elif len(parts) == 3:
            # Could be name:host:vendor OR name:host:port (if port is numeric)
            if parts[2].isdigit():
                return Device(name=parts[0], host=parts[1], vendor="junos", port=int(parts[2]))
            return Device(name=parts[0], host=parts[1], vendor=parts[2])
        elif len(parts) == 4:
            return Device(name=parts[0], host=parts[1], port=int(parts[2]), vendor=parts[3])
        else:
            raise argparse.ArgumentTypeError(f"Invalid target format: {value!r} (expected name:host[:port][:vendor])")


def load_devices_csv(path: str) -> list[Device]:
    devices = []
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = row.get("name", row.get("hostname", ""))
            host = row.get("host", row.get("ip", ""))
            vendor = row.get("vendor", row.get("transport", "junos"))
            if name and host:
                devices.append(Device(name=name, host=host, vendor=vendor))
    return devices


def resolve_command(command: str, vendor: str, cmd_dir: Path) -> str:
    cmd_file = cmd_dir / f"{command}.cmd"
    if cmd_file.exists():
        for line in cmd_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if ":" in line:
                v, cmd = line.split(":", 1)
                if v.strip() == vendor:
                    return cmd.strip()
        # Fallback: first non-comment line with a colon
        for line in cmd_file.read_text().splitlines():
            line = line.strip()
            if line.startswith("#") or not line:
                continue
            if ":" in line:
                return line.split(":", 1)[1].strip()
    return command


def load_edit_directory(path: str, devices: list[Device]) -> dict[str, str]:
    payloads = {}
    dir_path = Path(path)
    for dev in devices:
        # Try name.set, name.conf, name.txt
        for ext in (".set", ".conf", ".txt", ""):
            candidate = dir_path / f"{dev.name}{ext}"
            if candidate.exists():
                payloads[dev.name] = candidate.read_text()
                break
    return payloads


def load_jobs(path: str) -> tuple[list[Device], dict[str, str], dict[str, str], dict[str, str] | None, dict[str, dict] | None]:
    """Load a JSON job file. Returns (devices, per_device_commands, per_device_modes, edit_payloads, per_device_auth).

    Job file format — JSON array of objects:
    [
      {"target": "R1:192.168.178.120:junos", "show": "show bgp summary"},
      {"target": "netbox:https://netbox.example.com:rest", "show": "/api/dcim/devices/",
       "auth": {"scheme": "bearer", "token": "nbt_abc.xyz"}}
    ]

    Each entry must have:
      - "target": "NAME:HOST[:PORT][:VENDOR]"
      - Exactly one of: "show", "edit"
      - Optional: "auth": {"scheme": "...", "token": "..."}
    """
    if path == "-":
        data = json_mod.load(sys.stdin)
    else:
        with open(path) as f:
            data = json_mod.load(f)

    if not isinstance(data, list) or len(data) == 0:
        raise ValueError("Job file must be a non-empty JSON array")

    devices = []
    per_device_commands: dict[str, str] = {}
    per_device_modes: dict[str, str] = {}
    edit_payloads: dict[str, str] = {}
    per_device_auth: dict[str, dict] = {}

    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise ValueError(f"Job entry {i}: must be a JSON object")
        if "target" not in entry:
            raise ValueError(f"Job entry {i}: missing 'target'")

        dev = parse_target(entry["target"])

        # Optional port override from job entry
        if "port" in entry:
            dev.port = int(entry["port"])

        has_show = "show" in entry
        has_edit = "edit" in entry
        if has_show == has_edit:
            raise ValueError(f"Job entry {i} ({dev.name}): must have exactly one of 'show' or 'edit'")

        if has_show:
            per_device_commands[dev.name] = entry["show"]
            per_device_modes[dev.name] = "show"
        else:
            per_device_commands[dev.name] = entry["edit"]
            per_device_modes[dev.name] = "edit-broadcast"
            edit_payloads[dev.name] = entry["edit"]

        devices.append(dev)

        if "auth" in entry:
            per_device_auth[dev.name] = entry["auth"]

    return devices, per_device_commands, per_device_modes, edit_payloads if edit_payloads else None, per_device_auth if per_device_auth else None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="h-ssh", description="Parallel SSH/NETCONF network device orchestration")

    # Device selection
    target_group = p.add_mutually_exclusive_group()
    target_group.add_argument("--devices", metavar="CSV", help="CSV device inventory file")
    target_group.add_argument("--target", action="append", type=parse_target, dest="targets", metavar="NAME:HOST[:VENDOR]", help="Inline device (repeatable)")
    target_group.add_argument("--job", metavar="FILE", help="JSON job file with per-device commands (use - for stdin)")

    # Authentication
    p.add_argument("--user", default=os.environ.get("HSSH_USER"), help="SSH username (default: from SSH config or system user)")
    p.add_argument("--password", default=None, help="SSH/eAPI password (or use HSSH_PASSWORD env)")

    # Show mode
    p.add_argument("-sC", dest="show_command", metavar="CMD", help="Show command to execute")

    # Edit modes
    p.add_argument("-eC", dest="edit_command", metavar="CMD", help="Edit command (single command to all devices)")
    p.add_argument("-eD", dest="edit_directory", metavar="DIR", help="Edit directory (per-device config files)")
    p.add_argument("-eB", dest="edit_broadcast", metavar="CMD", help="Edit broadcast (same config to all)")

    # Output control
    p.add_argument("--json", action="store_true", help="JSON output")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress status banners")
    p.add_argument("--log-json", metavar="PATH", help="Write JSONL structured log")
    p.add_argument("--audit-log", metavar="PATH", default=os.environ.get("HSSH_AUDIT_LOG"), help="Edit audit log path")

    # Safety
    p.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts")
    p.add_argument("--dry-run", action="store_true", help="Show diffs without committing")
    p.add_argument("--commit-confirmed", type=int, default=0, metavar="MIN", help="Auto-rollback timeout in minutes")

    # Performance
    p.add_argument("--workers", type=int, default=int(os.environ.get("HSSH_WORKERS", "8")), help="Parallel worker count")
    p.add_argument("--session-timeout", type=int, default=int(os.environ.get("HSSH_SESSION_TIMEOUT", "30")), help="SSH session timeout (seconds)")
    p.add_argument("--command-timeout", type=int, default=int(os.environ.get("HSSH_COMMAND_TIMEOUT", "120")), help="Per-command timeout (seconds)")

    # Output saving
    p.add_argument("--save-output", metavar="DIR", help="Save per-device output to directory")

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    # --- Job file mode ---
    per_device_commands: dict[str, str] | None = None
    per_device_modes: dict[str, str] | None = None
    per_device_auth: dict[str, dict] | None = None

    if args.job:
        # --job is exclusive with -sC/-eC/-eB/-eD
        if any([args.show_command, args.edit_command, args.edit_directory, args.edit_broadcast]):
            print("ERROR: --job cannot be combined with -sC, -eC, -eD, or -eB.", file=sys.stderr)
            return 2
        try:
            devices, per_device_commands, per_device_modes, edit_payloads, per_device_auth = load_jobs(args.job)
        except (ValueError, json_mod.JSONDecodeError, argparse.ArgumentTypeError) as e:
            print(f"ERROR: Invalid job file: {e}", file=sys.stderr)
            return 2

        # Determine dominant mode for B2 check and summary
        modes = set(per_device_modes.values())
        has_edits = any(m.startswith("edit") for m in modes)
        mode = "job"
        command = ""

    else:
        # --- Resolve devices ---
        devices: list[Device] = []
        edit_payloads: dict[str, str] | None = None
        if args.targets:
            devices = args.targets
        elif args.devices:
            if not os.path.exists(args.devices):
                print(f"ERROR: Device file not found: {args.devices}", file=sys.stderr)
                return 2
            devices = load_devices_csv(args.devices)
        if not devices:
            print("ERROR: No devices specified. Use --target, --devices, or --job.", file=sys.stderr)
            return 2

        # --- Resolve mode and command ---
        mode = None
        command = ""
        cmd_dir = Path(__file__).resolve().parent / "commands"
        has_edits = False

        if args.show_command:
            mode = "show"
            command = args.show_command
        elif args.edit_command:
            mode = "edit-command"
            command = args.edit_command
            has_edits = True
        elif args.edit_directory:
            mode = "edit-directory"
            edit_payloads = load_edit_directory(args.edit_directory, devices)
            if not edit_payloads:
                print(f"ERROR: No config files found in {args.edit_directory} for specified devices.", file=sys.stderr)
                return 2
            has_edits = True
        elif args.edit_broadcast:
            mode = "edit-broadcast"
            command = args.edit_broadcast
            has_edits = True
        else:
            print("ERROR: No operation specified. Use -sC, -eC, -eD, -eB, or --job.", file=sys.stderr)
            return 2

        # Resolve command shortcuts per vendor
        if mode == "show":
            per_device_commands = {}
            for dev in devices:
                per_device_commands[dev.name] = resolve_command(command, dev.vendor, cmd_dir)

    # --- Credential resolution (B1) ---
    password = args.password
    if not password:
        password = os.environ.get("HSSH_PASSWORD")
    # SSH key auth is attempted by paramiko automatically

    # --- Edit confirmation check (B2) ---
    if has_edits and not args.dry_run:
        if not args.yes:
            if not sys.stdin.isatty():
                print("ERROR: Edit operation requires confirmation but stdin is not a TTY. Use -y to skip.", file=sys.stderr)
                return 2
            answer = input(f"Proceed with edit on {len(devices)} device(s)? [y/N] ")
            if answer.lower() not in ("y", "yes"):
                print("Aborted.", file=sys.stderr)
                return 2

    # --- Execute ---
    start_time = time.monotonic()

    def on_start(dev):
        if not args.json:
            print_start(dev, quiet=args.quiet)

    def on_complete(result):
        if not args.json:
            print_result(result, quiet=args.quiet)

    if mode == "show":
        actual_command = resolve_command(command, devices[0].vendor,
                                         Path(__file__).resolve().parent / "commands")
    elif mode == "job":
        actual_command = ""
    else:
        actual_command = command

    results = run_parallel(
        devices=devices,
        user=args.user,
        password=password,
        command=actual_command,
        mode=mode,
        workers=args.workers,
        session_timeout=args.session_timeout,
        command_timeout=args.command_timeout,
        edit_payloads=edit_payloads,
        per_device_commands=per_device_commands,
        per_device_modes=per_device_modes,
        per_device_auth=per_device_auth,
        dry_run=args.dry_run,
        confirmed_minutes=args.commit_confirmed,
        on_start=on_start if not args.json else None,
        on_complete=on_complete if not args.json else None,
    )

    total_ms = int((time.monotonic() - start_time) * 1000)

    # --- Output ---
    if args.json:
        print(format_json(results))
    else:
        output = format_human(results, quiet=args.quiet)
        if output:
            print(output)
        print_summary_human(results, quiet=args.quiet)

    # Summary line (P1.5)
    if not args.quiet:
        print(format_summary(results, mode, total_ms), file=sys.stderr)

    # --- Structured log (P1.4) ---
    if args.log_json:
        write_log_json(args.log_json, results, mode, actual_command, total_ms, args.dry_run)

    # --- Edit audit log (P2.2) ---
    if has_edits and args.audit_log:
        for r in results:
            payload = ""
            if edit_payloads and r.device in edit_payloads:
                payload = edit_payloads[r.device]
            elif per_device_commands and r.device in per_device_commands:
                payload = per_device_commands[r.device]
            elif command:
                payload = command
            write_audit_entry(args.audit_log, r, mode, payload,
                              dry_run=args.dry_run, confirmed_minutes=args.commit_confirmed)

    # --- Save per-device output ---
    if args.save_output:
        os.makedirs(args.save_output, exist_ok=True)
        for r in results:
            out_path = os.path.join(args.save_output, f"{r.device}.txt")
            with open(out_path, "w") as f:
                f.write(r.output if r.ok else f"ERROR: {r.error}")

    # --- Exit code ---
    if all(r.ok for r in results):
        return 0
    elif any(r.ok for r in results):
        return 1
    else:
        return 1


if __name__ == "__main__":
    sys.exit(main())
