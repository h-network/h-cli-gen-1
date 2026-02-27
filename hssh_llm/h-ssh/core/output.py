import json
import sys
from datetime import datetime, timezone
from .runner import DeviceResult, Device


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def print_start(device: Device, quiet: bool = False) -> None:
    if not quiet:
        print(f"[{_ts()}] {device.name} ({device.host}) — STARTED", file=sys.stderr)


def print_result(result: DeviceResult, quiet: bool = False) -> None:
    if quiet:
        return
    if result.ok:
        print(f"[{_ts()}] {result.device} ({result.host}) — OK ({result.duration_ms}ms)", file=sys.stderr)
    else:
        print(f"[{_ts()}] {result.device} ({result.host}) — FAIL ({result.duration_ms}ms): {result.error}", file=sys.stderr)


def format_human(results: list[DeviceResult], quiet: bool = False) -> str:
    lines = []
    for r in results:
        if r.ok and r.output:
            if not quiet:
                lines.append(f"--- {r.device} ({r.host}) ---")
            lines.append(r.output)
        elif not r.ok:
            lines.append(f"--- {r.device} ({r.host}) --- ERROR: {r.error}")
    return "\n".join(lines)


def format_json(results: list[DeviceResult]) -> str:
    out = []
    for r in results:
        entry = {
            "device": r.device,
            "host": r.host,
            "vendor": r.vendor,
            "ok": r.ok,
            "duration_ms": r.duration_ms,
        }
        if r.command is not None:
            entry["command"] = r.command
        if r.ok:
            entry["output"] = r.output
        else:
            entry["error"] = r.error or "unknown error"
        if r.diff is not None:
            entry["diff"] = r.diff
        out.append(entry)
    return json.dumps(out, indent=2)


def format_summary(results: list[DeviceResult], mode: str, total_ms: int) -> str:
    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    summary = {"targets": len(results), "ok": ok, "fail": fail, "duration_ms": total_ms, "mode": mode}
    return f"[h-ssh] {json.dumps(summary)}"


def print_summary_human(results: list[DeviceResult], quiet: bool = False) -> None:
    if quiet:
        return
    ok = sum(1 for r in results if r.ok)
    fail = len(results) - ok
    print(f"\nSummary: {len(results)} devices, {ok} ok, {fail} fail", file=sys.stderr)


def write_log_json(path: str, results: list[DeviceResult], mode: str,
                    command: str, total_ms: int, dry_run: bool = False) -> None:
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "command": command,
        "targets_total": len(results),
        "targets_ok": sum(1 for r in results if r.ok),
        "targets_fail": sum(1 for r in results if not r.ok),
        "duration_ms": total_ms,
        "dry_run": dry_run,
        "devices": [
            {"name": r.device, "ok": r.ok, "duration_ms": r.duration_ms,
             **({"output_length": len(r.output)} if r.ok else {"error": r.error or "unknown"})}
            for r in results
        ]
    }
    with open(path, "a") as f:
        f.write(json.dumps(record) + "\n")
