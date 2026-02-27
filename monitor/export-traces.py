#!/usr/bin/env python3
"""Export correlated h-cli traces as structured JSONL for training data.

Joins dispatcher audit log (task_id, user message, response) with
firewall audit log (tool calls, allow/deny, output) by timestamp window.
Single-threaded dispatcher guarantees no task overlap.

Usage:
    ./monitor/export-traces.py                          # default: logs/ -> traces.jsonl
    ./monitor/export-traces.py -l /path/to/logs         # custom log dir
    ./monitor/export-traces.py -o training-data.jsonl    # custom output
    ./monitor/export-traces.py --since 2026-02-18       # only tasks after date
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone


def parse_jsonl(path):
    """Yield parsed JSON objects from a JSONL file."""
    if not os.path.isfile(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def parse_ts(ts_str):
    """Parse ISO timestamp string to datetime."""
    # Handle both Z and +00:00 suffixes
    ts_str = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(ts_str)


def load_tasks(audit_path):
    """Load task pairs (started + completed) from dispatcher audit log."""
    starts = {}
    tasks = []

    for entry in parse_jsonl(audit_path):
        task_id = entry.get("task_id")
        if not task_id:
            continue

        if "user_message" in entry:
            starts[task_id] = entry
        elif "output" in entry and task_id in starts:
            start = starts.pop(task_id)
            tasks.append({
                "task_id": task_id,
                "user_id": start.get("user_id"),
                "timestamp_start": start["timestamp"],
                "timestamp_end": entry["timestamp"],
                "user_message": start["user_message"],
                "response": entry["output"],
                "response_length": entry.get("output_length", len(entry["output"])),
            })

    return tasks


def load_firewall(audit_path):
    """Load firewall entries (allow/deny + output) sorted by timestamp."""
    entries = []
    for entry in parse_jsonl(audit_path):
        if "command" not in entry:
            continue
        entries.append(entry)
    entries.sort(key=lambda e: e["timestamp"])
    return entries


def correlate(tasks, firewall_entries):
    """Match firewall entries to tasks by timestamp window."""
    traces = []

    for task in tasks:
        t_start = parse_ts(task["timestamp_start"])
        t_end = parse_ts(task["timestamp_end"])

        # Collect firewall entries within this task's time window
        tool_calls = {}
        for entry in firewall_entries:
            t_entry = parse_ts(entry["timestamp"])
            if t_entry < t_start:
                continue
            if t_entry > t_end:
                break

            cmd = entry["command"]

            if "allowed" in entry:
                tool_calls[cmd] = {
                    "command": cmd,
                    "allowed": entry["allowed"],
                    "reason": entry.get("reason", ""),
                }
            elif "output_length" in entry and cmd in tool_calls:
                tool_calls[cmd]["output_length"] = entry["output_length"]

        trace = {
            "task_id": task["task_id"],
            "user_id": task["user_id"],
            "timestamp": task["timestamp_start"],
            "user_message": task["user_message"],
            "tool_calls": list(tool_calls.values()),
            "response": task["response"],
        }
        traces.append(trace)

    return traces


def main():
    parser = argparse.ArgumentParser(description="Export correlated h-cli traces")
    parser.add_argument("-l", "--log-dir", default="logs",
                        help="Log directory (default: logs/)")
    parser.add_argument("-o", "--output", default="traces.jsonl",
                        help="Output file (default: traces.jsonl)")
    parser.add_argument("--since", default=None,
                        help="Only include tasks after this date (YYYY-MM-DD)")
    args = parser.parse_args()

    dispatcher_audit = os.path.join(args.log_dir, "claude", "audit.log")
    firewall_audit = os.path.join(args.log_dir, "firewall", "audit.log")

    if not os.path.isfile(dispatcher_audit):
        print(f"Not found: {dispatcher_audit}", file=sys.stderr)
        sys.exit(1)

    # Load and correlate
    tasks = load_tasks(dispatcher_audit)
    firewall_entries = load_firewall(firewall_audit)
    traces = correlate(tasks, firewall_entries)

    # Filter by date if requested
    if args.since:
        since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        traces = [t for t in traces if parse_ts(t["timestamp"]) >= since]

    # Write output
    with open(args.output, "w") as f:
        for trace in traces:
            f.write(json.dumps(trace, ensure_ascii=False) + "\n")

    print(f"Exported {len(traces)} traces -> {args.output}")


if __name__ == "__main__":
    main()
