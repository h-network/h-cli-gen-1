#!/usr/bin/env bash
# maintenance.sh — Index completed conversations into Qdrant collections.
#
# Reads the dispatcher's audit log, correlates task_started/task_completed
# pairs by task_id, and appends Q&A pairs to the conversations collection
# JSONL for Core to auto-index on next restart.
#
# Usage: ./maintenance.sh
# Idempotent — tracks last processed byte offset to avoid duplicates.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
AUDIT_LOG="${REPO_ROOT}/logs/claude/audit.log"
COLLECTIONS_DIR="${REPO_ROOT}/data/collections/conversations"
OFFSET_FILE="${COLLECTIONS_DIR}/.last_offset"
MIN_ANSWER_LENGTH=50

log() { echo "[$(date -u '+%Y-%m-%d %H:%M:%S UTC')] maintenance: $*"; }

# ── Preflight checks ─────────────────────────────────────────────────────

if [ ! -f "$AUDIT_LOG" ]; then
    log "No audit log found at ${AUDIT_LOG}, nothing to index."
    exit 0
fi

mkdir -p "$COLLECTIONS_DIR"

# Read last processed offset (byte position)
last_offset=0
if [ -f "$OFFSET_FILE" ]; then
    last_offset=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
    # Validate it's a number
    if ! [[ "$last_offset" =~ ^[0-9]+$ ]]; then
        last_offset=0
    fi
fi

current_size=$(stat -c%s "$AUDIT_LOG" 2>/dev/null || stat -f%z "$AUDIT_LOG" 2>/dev/null || echo 0)

if [ "$current_size" -le "$last_offset" ]; then
    # Log was rotated (smaller than offset) — reset
    if [ "$current_size" -lt "$last_offset" ]; then
        log "Audit log rotated (size ${current_size} < offset ${last_offset}), resetting offset."
        last_offset=0
    else
        log "No new audit entries since last run."
        exit 0
    fi
fi

log "Processing audit log from byte ${last_offset} (file size: ${current_size})."

# ── Process new entries ───────────────────────────────────────────────────

# Extract new lines and correlate task_started + task_completed pairs.
# Uses Python for reliable JSON parsing — stdlib only.

new_pairs=$(tail -c "+$((last_offset + 1))" "$AUDIT_LOG" | python3 -c '
import json
import sys
from datetime import datetime

starts = {}  # task_id -> user_message
pairs = []

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        entry = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        continue

    task_id = entry.get("task_id")
    if not task_id:
        continue

    # task_started has user_message field
    if "user_message" in entry:
        starts[task_id] = entry["user_message"]
    # task_completed has output field
    elif "output" in entry:
        question = starts.get(task_id)
        if not question:
            continue
        answer = entry["output"]
        # Skip short answers
        if len(answer) < '"$MIN_ANSWER_LENGTH"':
            continue
        # Skip error/abort/timeout outputs
        if answer.startswith("Error:") or answer.startswith("Task aborted"):
            continue
        # Extract date from timestamp for source tag
        ts = entry.get("timestamp", "")
        try:
            date_str = ts[:10]  # "2026-03-06"
        except (IndexError, TypeError):
            date_str = datetime.utcnow().strftime("%Y-%m-%d")
        pair = {
            "question": question,
            "answer": answer,
            "source": f"conversation:{date_str}",
        }
        print(json.dumps(pair, ensure_ascii=False))
        del starts[task_id]  # Prevent duplicate if re-run with overlap
')

if [ -z "$new_pairs" ]; then
    log "No new completed conversations to index."
    echo "$current_size" > "$OFFSET_FILE"
    exit 0
fi

# Write to dated files — Core only indexes new/changed files
total=0
echo "$new_pairs" | python3 -c '
import json, sys
from collections import defaultdict

by_date = defaultdict(list)
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        record = json.loads(line)
        date_str = record.get("source", "").replace("conversation:", "")
        if not date_str:
            date_str = "unknown"
        by_date[date_str].append(line)
    except (json.JSONDecodeError, ValueError):
        continue

for date_str, lines in sorted(by_date.items()):
    print(f"{date_str}\t{len(lines)}")
    for l in lines:
        print(l)
' | while IFS= read -r line; do
    if [[ "$line" == *$'\t'* ]]; then
        # Header line: date<tab>count
        current_date=$(echo "$line" | cut -f1)
        current_count=$(echo "$line" | cut -f2)
        dated_file="${COLLECTIONS_DIR}/conversations_${current_date}.jsonl"
    else
        # Data line: append to dated file
        echo "$line" >> "$dated_file"
        total=$((total + 1))
    fi
done

pair_count=$(echo "$new_pairs" | wc -l)

# Update offset
echo "$current_size" > "$OFFSET_FILE"

log "Indexed ${pair_count} conversation(s) across dated files in ${COLLECTIONS_DIR}"
