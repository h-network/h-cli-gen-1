import json
from datetime import datetime, timezone
from .runner import DeviceResult


def write_audit_entry(path: str, result: DeviceResult, mode: str,
                      payload: str, dry_run: bool = False,
                      confirmed_minutes: int = 0) -> None:
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mode": mode,
        "device": result.device,
        "host": result.host,
        "vendor": result.vendor,
        "payload": payload,
        "dry_run": dry_run,
        "commit_confirmed": confirmed_minutes,
        "ok": result.ok,
    }
    if result.diff:
        entry["diff"] = result.diff
    if result.error:
        entry["error"] = result.error
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
