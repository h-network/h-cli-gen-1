"""h-cli worker — Claude invocation, prompt building, skills, sessions.

Handles everything related to executing a task: building the system prompt,
spawning the Claude process, managing session history, and skill injection.
Uses bus.py key constants for Redis operations.
"""

import json
import os
import re
import signal
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

import redis

from hcli_logging import get_logger, get_audit_logger
from bus import (
    SESSION_PREFIX, SESSION_SIZE_PREFIX, SESSION_HISTORY_PREFIX,
    LAST_ACTIVITY_PREFIX, SESSION_TTL, HISTORY_TTL,
)

logger = get_logger(__name__, service="claude")
audit = get_audit_logger("claude")

# ── File paths (container paths, set by Dockerfile COPY / compose volumes)

GROUND_RULES_PATH = "/app/groundRules.md"
CONTEXT_PATH = "/app/context.md"
MCP_CONFIG = "/app/mcp-config.json"
SKILLS_DIRS = ["/app/skills/public", "/app/skills/private"]
SESSION_CHUNK_DIR = "/var/log/hcli/sessions"

# ── Budgets ──────────────────────────────────────────────────────────────

MAX_SKILLS_BYTES = 20 * 1024
MAX_CONTEXT_INJECT = 30 * 1024
MAX_MEMORY_INJECT = 50 * 1024
MAX_SESSION_BYTES = 100 * 1024

# ── Config ───────────────────────────────────────────────────────────────

TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "600"))
IDLE_DUMP_SECONDS = int(os.environ.get("IDLE_DUMP_SECONDS", "1800"))

# ── Chat name mapping ────────────────────────────────────────────────────

_CHAT_NAMES = {}
for _pair in os.environ.get("CHAT_NAMES", "").split(","):
    if ":" in _pair:
        _cid, _name = _pair.strip().split(":", 1)
        _CHAT_NAMES[_cid.strip()] = _name.strip()


def _chat_dir_name(chat_id) -> str:
    return _CHAT_NAMES.get(str(chat_id), str(chat_id))


_CHAT_ID_RE = re.compile(r"^-?\d+$")


def _validate_chat_id(chat_id) -> bool:
    """Validate chat_id is a numeric ID (no path traversal)."""
    return bool(_CHAT_ID_RE.match(str(chat_id)))


# ── Prompt building ──────────────────────────────────────────────────────

def _load_base_prompt() -> str:
    parts = []
    for path in (GROUND_RULES_PATH, CONTEXT_PATH):
        try:
            with open(path) as f:
                parts.append(f.read().strip())
        except FileNotFoundError:
            logger.debug("Prompt file not found, skipping: %s", path)
    if not parts:
        parts.append(
            "You are h-cli, an AI engineering assistant. "
            "Use the available MCP tools to fulfill the user's request. "
            "Be concise. Return just the relevant output."
        )
    return "\n\n---\n\n".join(parts)


def _parse_skill_header(content: str):
    """Parse YAML-style header from a skill file.

    Returns (keywords set or None, rules list, body after header).
    """
    keywords = None
    rules = []
    if not content.startswith("---"):
        return keywords, rules, content

    end = content.find("---", 3)
    if end == -1:
        return keywords, rules, content

    header = content[3:end]
    body = content[end + 3:].lstrip("\n")
    in_rules = False
    for line in header.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("keywords:"):
            kw_str = stripped.split(":", 1)[1].strip()
            keywords = {k.strip().lower() for k in kw_str.split(",") if k.strip()}
            in_rules = False
        elif stripped.lower() == "rules:":
            in_rules = True
        elif in_rules and stripped.startswith("- "):
            rules.append(stripped[2:])
        elif in_rules and stripped and not stripped.startswith("-"):
            in_rules = False

    return keywords, rules, body


def _load_matching_skills(message: str) -> str:
    """Load skill files whose keywords match the user's message.

    Tiered injection: only rules from the YAML header are injected for
    keyword-matched skills. Full skill index always appended.
    """
    msg_lower = message.lower()
    msg_words = set(re.findall(r"[a-z0-9][\w.-]*", msg_lower))

    all_skills = []
    matched_rules = []
    for skills_dir in SKILLS_DIRS:
        if not os.path.isdir(skills_dir):
            continue
        try:
            entries = os.listdir(skills_dir)
        except OSError:
            continue

        for fname in sorted(entries):
            if not fname.endswith(".md"):
                continue
            fpath = os.path.join(skills_dir, fname)
            if not os.path.isfile(fpath):
                continue
            try:
                with open(fpath) as f:
                    content = f.read()
            except OSError:
                continue

            skill_name = fname[:-3]
            keywords, rules, _body = _parse_skill_header(content)
            all_skills.append((skill_name, fpath))

            if keywords is None:
                if skill_name.lower() not in msg_words:
                    continue
            else:
                if not keywords & msg_words:
                    continue

            if rules:
                matched_rules.append((skill_name, rules))

    if not all_skills:
        return ""

    parts = []
    total = 0
    for skill_name, rules in matched_rules:
        block = f"## Rules: {skill_name}\n" + "\n".join(f"- {r}" for r in rules)
        size = len(block)
        if total + size > MAX_SKILLS_BYTES:
            break
        parts.append(block)
        total += size

    skill_list = "\n".join(f"- {name} → `{fpath}`" for name, fpath in all_skills)
    parts.append(f"## Available skills\n{skill_list}")

    if matched_rules:
        names = [m[0] for m in matched_rules]
        logger.info("Skills matched: %s (rules: %d bytes, index: %d skills)",
                     ", ".join(names), total, len(all_skills))
    else:
        logger.info("Skills: no keyword match (index: %d skills)", len(all_skills))
    return "\n\n".join(parts)


# ── Session chunks ───────────────────────────────────────────────────────

_CHUNK_TIMESTAMP_RE = re.compile(
    r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}):\d{2} UTC\]"
)


def _compact_chunk(text: str) -> str:
    """Strip chunk header, drop seconds from timestamps, collapse excess blanks."""
    first_bracket = text.find("\n[")
    if first_bracket != -1:
        text = text[first_bracket + 1:]
    text = _CHUNK_TIMESTAMP_RE.sub(lambda m: f"[{m.group(1)}]", text)
    text = text.replace("ASSISTANT:", "h-cli:")
    text = re.sub(r"\n---\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _load_recent_chunks(chat_id) -> str:
    """Read session chunk files from disk and return compacted content."""
    if not _validate_chat_id(chat_id):
        logger.warning("Invalid chat_id for chunk load, skipping: %s", chat_id)
        return ""
    chunk_dir = os.path.join(SESSION_CHUNK_DIR, _chat_dir_name(chat_id))
    if not os.path.isdir(chunk_dir):
        return ""
    chunks = sorted(
        (f for f in os.listdir(chunk_dir) if f.startswith("chunk_")),
        reverse=True,
    )
    if not chunks:
        return ""
    content = ""
    for chunk_file in chunks:
        try:
            with open(os.path.join(chunk_dir, chunk_file)) as f:
                text = _compact_chunk(f.read())
            if len(content) + len(text) > MAX_MEMORY_INJECT:
                remaining = MAX_MEMORY_INJECT - len(content)
                if remaining > 0 and not content:
                    content = text[:remaining]
                break
            content = text + "\n" + content
        except OSError:
            continue
    return content.strip()


def build_system_prompt(chat_id=None, message: str = "") -> str:
    """Build per-task system prompt with session memory and skills injected."""
    prompt = _load_base_prompt()
    if chat_id:
        memory = _load_recent_chunks(chat_id)
        if memory:
            prompt += f"\n\n---\n\n## History\n{memory}"
    if message:
        skills = _load_matching_skills(message)
        if skills:
            prompt += f"\n\n---\n\n## Skills\n{skills}"
    return prompt


# ── Conversation context ─────────────────────────────────────────────────

def _build_conversation_context(r: redis.Redis, chat_id) -> str:
    """Build recent conversation context from Redis, newest first, up to budget."""
    history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
    turns = r.lrange(history_key, 0, -1)
    if not turns:
        return ""
    formatted = []
    total = 0
    for turn_json in reversed(turns):
        try:
            turn = json.loads(turn_json)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Skipping malformed turn in history: %s",
                           str(turn_json)[:100])
            continue
        role_raw = turn.get("role", "unknown").upper()
        role = "h-cli" if role_raw == "ASSISTANT" else role_raw
        ts = datetime.fromtimestamp(
            turn.get("timestamp", 0), tz=timezone.utc
        ).strftime("%H:%M")
        content = turn.get("content", "")
        line = f"[{ts}] {role}: {content}"
        if total + len(line) > MAX_CONTEXT_INJECT:
            break
        formatted.append(line)
        total += len(line)
    formatted.reverse()
    return "\n".join(formatted)


# ── Session management ───────────────────────────────────────────────────

def dump_session_chunk(r: redis.Redis, chat_id: str,
                       session_id: str) -> str | None:
    """Dump accumulated session history to a text file and clear Redis state."""
    if not _validate_chat_id(chat_id):
        logger.warning("Invalid chat_id for chunk dump, skipping: %s", chat_id)
        return None
    history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
    turns = r.lrange(history_key, 0, -1)
    if not turns:
        return None

    chunk_dir = os.path.join(SESSION_CHUNK_DIR, _chat_dir_name(chat_id))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    chunk_path = os.path.join(chunk_dir, f"chunk_{timestamp}.txt")

    try:
        os.makedirs(chunk_dir, exist_ok=True)
        with open(chunk_path, "w") as f:
            f.write("=== h-cli session chunk ===\n")
            f.write(f"Chat: {chat_id}\n")
            f.write(f"Session: {session_id}\n")
            f.write(f"Chunked: {timestamp}\n")
            f.write(f"Turns: {len(turns)}\n")
            f.write("===\n\n")
            for turn_json in turns:
                try:
                    turn = json.loads(turn_json)
                except (json.JSONDecodeError, TypeError):
                    logger.warning("Skipping malformed turn in chunk dump: %s",
                                   str(turn_json)[:100])
                    continue
                role = turn.get("role", "unknown").upper()
                ts = datetime.fromtimestamp(
                    turn.get("timestamp", 0), tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
                content = turn.get("content", "")
                f.write(f"[{ts}] {role}:\n{content}\n\n---\n\n")
    except OSError as e:
        logger.error("Failed to write session chunk %s: %s", chunk_path, e)
        return None

    # Only clear Redis state after successful file write
    r.delete(history_key)
    r.delete(f"{SESSION_SIZE_PREFIX}{chat_id}")

    logger.info("Session chunk saved: %s (%d turns)", chunk_path, len(turns))
    return chunk_path


def manage_session(r: redis.Redis, chat_id) -> str:
    """Handle session expiry and chunking. Returns a fresh session_id."""
    if chat_id:
        # Expiry recovery: dump history if session key gone but history remains
        if not r.exists(f"{SESSION_PREFIX}{chat_id}"):
            history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
            if r.llen(history_key) > 0:
                chunk_path = dump_session_chunk(r, str(chat_id), "expired")
                if chunk_path:
                    logger.info("Saved expired session history for chat %s -> %s",
                                chat_id, chunk_path)

        # Size-based chunking
        size_key = f"{SESSION_SIZE_PREFIX}{chat_id}"
        current_size = int(r.get(size_key) or 0)
        if current_size > MAX_SESSION_BYTES:
            old_session = r.get(f"{SESSION_PREFIX}{chat_id}")
            if old_session:
                chunk_path = dump_session_chunk(r, str(chat_id), old_session)
                r.delete(f"{SESSION_PREFIX}{chat_id}")
                if chunk_path:
                    logger.info("Session for chat %s chunked at %d bytes -> %s",
                                chat_id, current_size, chunk_path)

    session_id = str(uuid.uuid4())
    logger.info("Session %s for chat %s", session_id, chat_id)
    return session_id


def record_history(r: redis.Redis, chat_id, original_message: str, output: str):
    """Record conversation turn in session history."""
    if not chat_id:
        return
    # Strip base64 image data URIs before storing
    history_message = re.sub(
        r'data:image/[^;]+;base64,[A-Za-z0-9+/=]+',
        '[image]',
        original_message,
    )
    exchange_size = len(history_message) + len(output)
    size_key = f"{SESSION_SIZE_PREFIX}{chat_id}"
    r.incrby(size_key, exchange_size)
    r.expire(size_key, HISTORY_TTL)

    history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
    turn_user = json.dumps({
        "role": "user", "content": history_message, "timestamp": time.time(),
    })
    turn_asst = json.dumps({
        "role": "assistant", "content": output, "timestamp": time.time(),
    })
    r.rpush(history_key, turn_user, turn_asst)
    r.expire(history_key, HISTORY_TTL)
    r.set(f"{LAST_ACTIVITY_PREFIX}{chat_id}", time.time(), ex=HISTORY_TTL)


def persist_session(r: redis.Redis, chat_id, session_id: str):
    """Persist session ID for reuse."""
    if chat_id:
        r.set(f"{SESSION_PREFIX}{chat_id}", session_id, ex=SESSION_TTL)


# ── Claude invocation ────────────────────────────────────────────────────

def run_task(r: redis.Redis, task: dict, abort_event: threading.Event,
             proc_ref: list) -> tuple[str, dict]:
    """Execute a task: build context, spawn claude, parse result.

    Returns (output, metrics). Metrics dict includes is_error, timed_out,
    aborted flags for the dispatcher to determine final state.
    """
    task_id = task["task_id"]
    message = task.get("message", "")
    original_message = message
    chat_id = task.get("chat_id")
    user_id = task.get("user_id", "unknown")

    logger.info("Processing task %s: %s", task_id, message)
    audit.info(
        "task_started",
        extra={"task_id": task_id, "user_message": message, "user_id": user_id},
    )

    # Session management
    session_id = manage_session(r, chat_id)

    # Prepend recent conversation context
    if chat_id:
        recent = _build_conversation_context(r, chat_id)
        if recent:
            now = datetime.now(timezone.utc).strftime("%H:%M")
            message = f"{recent}\n[{now}] USER: {message}"

    # Build system prompt
    system_prompt = build_system_prompt(chat_id, original_message)

    # Map model names
    _model_map = {
        "opus": os.environ.get("MAIN_MODEL", "opus"),
        "sonnet": os.environ.get("MAIN_MODEL", "opus"),
        "haiku": os.environ.get("FAST_MODEL", "haiku"),
    }
    raw_model = task.get("model", "opus")
    task_model = _model_map.get(raw_model, raw_model)

    cmd = [
        "claude", "-p",
        "--output-format", "json",
        "--tools", "",
        "--mcp-config", MCP_CONFIG,
        "--allowedTools", "mcp__h-cli-core__run_command,mcp__h-cli-memory__memory_search",
        "--model", task_model,
        "--system-prompt", system_prompt,
        "--session-id", session_id,
        "--", message,
    ]

    output = ""
    metrics = {"is_error": False, "timed_out": False, "aborted": False}

    try:
        env = os.environ.copy()
        env["TASK_ID"] = task_id
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env=env,
        )
        proc_ref[0] = proc  # Make visible to abort thread

        try:
            stdout, stderr = proc.communicate(timeout=TASK_TIMEOUT)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            output = f"Error: Claude Code timed out after {TASK_TIMEOUT} seconds"
            metrics = {"is_error": True, "timed_out": True, "aborted": False}
            logger.warning("Task %s timed out", task_id)
            record_history(r, chat_id, original_message, output)
            persist_session(r, chat_id, session_id)
            return output, metrics

        if abort_event.is_set():
            output = "Task aborted by user"
            metrics = {"is_error": True, "timed_out": False, "aborted": True}
            record_history(r, chat_id, original_message, output)
            persist_session(r, chat_id, session_id)
            return output, metrics

        if stderr:
            logger.debug("claude stderr: %s", stderr.strip())

        raw_out = stdout.strip()
        if raw_out:
            try:
                result_json = json.loads(raw_out)
                output = result_json.get("result", "")
                if not output and result_json.get("is_error"):
                    output = "; ".join(
                        result_json.get("errors", ["(error, no output)"])
                    )
                usage = result_json.get("usage", {})
                model_usage = result_json.get("modelUsage", {})
                model_name = next(iter(model_usage), task_model)
                metrics = {
                    "model": model_name,
                    "input_tokens": usage.get("input_tokens", 0),
                    "output_tokens": usage.get("output_tokens", 0),
                    "cache_read": usage.get("cache_read_input_tokens", 0),
                    "cache_create": usage.get("cache_creation_input_tokens", 0),
                    "cost_usd": result_json.get("total_cost_usd", 0) or 0,
                    "duration_ms": result_json.get("duration_ms", 0) or 0,
                    "num_turns": result_json.get("num_turns", 1) or 1,
                    "is_error": result_json.get("is_error", False),
                    "timed_out": False,
                    "aborted": False,
                }
            except json.JSONDecodeError:
                logger.warning("Failed to parse claude JSON output, using raw text")
                output = raw_out

        if not output:
            output = stderr.strip() if stderr else "(no output from Claude)"

    except Exception as e:
        output = f"Error: {e}"
        metrics = {"is_error": True, "timed_out": False, "aborted": False}
        logger.exception("Task %s failed", task_id)

    # Record history and persist session
    record_history(r, chat_id, original_message, output)
    persist_session(r, chat_id, session_id)

    audit.info(
        "task_completed",
        extra={"task_id": task_id, "output_length": len(output), "output": output},
    )
    logger.info("Task %s completed (%d chars)", task_id, len(output))

    return output, metrics


# ── Idle session sweep ───────────────────────────────────────────────────

def sweep_idle_sessions(r: redis.Redis):
    """Proactively dump sessions that have been idle longer than IDLE_DUMP_SECONDS."""
    cursor = 0
    while True:
        cursor, keys = r.scan(
            cursor, match=f"{LAST_ACTIVITY_PREFIX}*", count=50,
        )
        for key in keys:
            chat_id = key[len(LAST_ACTIVITY_PREFIX):]
            try:
                last_ts = float(r.get(key) or 0)
            except (ValueError, TypeError):
                continue
            if time.time() - last_ts <= IDLE_DUMP_SECONDS:
                continue
            history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
            if r.llen(history_key) > 0:
                chunk_path = dump_session_chunk(r, chat_id, "idle")
                if chunk_path:
                    logger.info("Idle dump for chat %s -> %s", chat_id, chunk_path)
            r.delete(key)
        if cursor == 0:
            break
