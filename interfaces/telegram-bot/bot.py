"""h-cli Telegram Bot — async command interface with Redis task queue."""

import asyncio
import base64
import functools
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import httpx
import redis.asyncio as aioredis
from telegram import BotCommand, ReplyKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from hcli_logging import get_logger, get_audit_logger

logger = get_logger(__name__, service="telegram")
audit = get_audit_logger("telegram")

# ── Config ───────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "300"))
ALLOWED_CHATS: set[int] = set()

_raw = os.environ.get("ALLOWED_CHATS", "")
if _raw.strip():
    for cid in _raw.split(","):
        cid = cid.strip()
        if not cid:
            continue
        try:
            ALLOWED_CHATS.add(int(cid))
        except ValueError:
            logger.warning("Invalid chat ID in ALLOWED_CHATS, skipping: %s", cid)

if not ALLOWED_CHATS:
    logger.warning(
        "ALLOWED_CHATS is empty — no users are authorized. "
        "The bot will reject all messages."
    )

RESULT_HMAC_KEY = os.environ.get("RESULT_HMAC_KEY", "")
if not RESULT_HMAC_KEY:
    raise RuntimeError("RESULT_HMAC_KEY not set — run install.sh to generate one")

TELEGRAM_MAX_LEN = 4096
REDIS_TASKS_KEY = "hcli:tasks"
REDIS_RESULT_PREFIX = "hcli:results:"
REDIS_CONTROL_PREFIX = "hcli:control:"  # abort control channel
SESSION_HISTORY_PREFIX = "hcli:session_history:"
SESSION_SIZE_PREFIX = "hcli:session_size:"
SESSION_CHUNK_DIR = os.environ.get("SESSION_CHUNK_DIR", "/var/log/hcli/sessions")
NOTIFY_POLL_FALLBACK = 10  # seconds between fallback GET checks during subscribe
TEACH_PREFIX = "hcli:teach:"  # teach mode flag + turns
TEACH_TTL = 3600              # 1h auto-expire if user forgets
_verbose_mode: dict[int, bool] = {}  # per-chat verbose toggle (default ON)
ACTIVITY_IDLE_TIMEOUT = 30   # seconds with no events before auto-unsubscribe
ACTIVITY_MAX_COMMANDS = 8    # max commands shown in activity message
ACTIVITY_CMD_MAX_LEN = 60    # truncate commands longer than this
ACTIVITY_EDIT_INTERVAL = 1   # min seconds between Telegram message edits

_CHAT_NAMES = {}
for _pair in os.environ.get("CHAT_NAMES", "").split(","):
    if ":" in _pair:
        _cid, _name = _pair.strip().split(":", 1)
        _CHAT_NAMES[_cid.strip()] = _name.strip()


def _chat_dir_name(chat_id) -> str:
    return _CHAT_NAMES.get(str(chat_id), str(chat_id))


def _chat_tasks_key(chat_id) -> str:
    """Redis key for per-chat task tracking list."""
    return f"hcli:chat:{chat_id}:tasks"

_background_tasks: set[asyncio.Task] = set()  # prevent GC of fire-and-forget tasks

# ── Action system ─────────────────────────────────────────────────────────
# LLM embeds [action:type:payload] markers in responses. The bot strips them
# from the text, sends the text normally, then executes each action.
_ACTION_RE = re.compile(r'\[action:(\w+):([^\]]+)\]')

# Local stack Grafana (monitor profile) — basic auth
GRAFANA_INTERNAL_URL = os.environ.get("GRAFANA_INTERNAL_URL", "")
GRAFANA_ADMIN_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "")
# External Grafana (user infra) — token auth
GRAFANA_URL = os.environ.get("GRAFANA_URL", "")
GRAFANA_API_TOKEN = os.environ.get("GRAFANA_API_TOKEN", "")


async def _handle_graph_action(update: Update, payload: str) -> None:
    """Fetch Grafana render PNG and send as Telegram photo."""
    auth: tuple[str, str] | None = None
    headers: dict[str, str] = {}

    # Match payload against known Grafana instances.
    # For both: extract /render/ path and rebuild with correct base URL,
    # since the model may misspell or guess the hostname.
    if GRAFANA_URL and "/render/" in payload and payload.startswith("https://"):
        render_path = payload[payload.index("/render/"):]
        payload = GRAFANA_URL.rstrip("/") + render_path
        headers["Authorization"] = f"Bearer {GRAFANA_API_TOKEN}"
    elif GRAFANA_INTERNAL_URL and GRAFANA_ADMIN_PASSWORD and "/render/" in payload:
        render_path = payload[payload.index("/render/"):]
        payload = GRAFANA_INTERNAL_URL.rstrip("/") + render_path
        auth = ("admin", GRAFANA_ADMIN_PASSWORD)
    else:
        logger.warning("Graph URL doesn't match any known Grafana: %s", payload[:100])
        return

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(
                payload, auth=auth, headers=headers, timeout=30,
            )
    except httpx.HTTPError as e:
        logger.error("Graph fetch failed: %s", e)
        await update.message.reply_text("Failed to fetch graph.")
        return

    if resp.status_code == 200 and resp.headers.get(
        "content-type", ""
    ).startswith("image/"):
        await update.message.reply_photo(photo=resp.content)
    else:
        logger.warning(
            "Graph render failed: HTTP %d, content-type=%s",
            resp.status_code, resp.headers.get("content-type", ""),
        )
        await update.message.reply_text(
            f"Failed to render graph (HTTP {resp.status_code})."
        )


_ACTION_HANDLERS: dict[str, Callable] = {
    "graph": _handle_graph_action,
}

# ── Model toggle ─────────────────────────────────────────────────────────
_chat_model: dict[int, str] = {}  # chat_id → "haiku" or "opus"


def _model_keyboard():
    return ReplyKeyboardMarkup(
        [["⚡ Fast", "🧠 Deep"], ["📊 Stats", "📚 Skills"], ["📝 Teach", "📖 End Teaching"], ["📡 Verbose"]],
        resize_keyboard=True,
    )


def _verify_result(task_id: str, result: dict) -> bool:
    """Verify HMAC-SHA256 signature on a task result."""
    expected = result.get("hmac", "")
    msg = f"{task_id}:{result.get('output', '')}:{result.get('completed_at', '')}"
    computed = hmac.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, computed)


# ── Helpers ──────────────────────────────────────────────────────────────
def authorized(chat_id: int) -> bool:
    """Fail-closed: empty allowlist means nobody gets in."""
    return chat_id in ALLOWED_CHATS


def markdown_to_telegram_html(text: str) -> str:
    """Convert markdown to Telegram-supported HTML.

    Extracts protected blocks (code, inline code, tables) into placeholders
    before processing inline markdown, then restores them at the end.
    """
    placeholders: list[str] = []

    def _placeholder(content: str) -> str:
        idx = len(placeholders)
        placeholders.append(content)
        return f"\x00PH{idx}\x00"

    # 1. Extract fenced code blocks
    def _code_block(m: re.Match) -> str:
        code = m.group(2)
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return _placeholder(f"<pre>{escaped}</pre>")

    text = re.sub(r"```(\w*)\n?(.*?)```", _code_block, text, flags=re.DOTALL)

    # 2. Extract inline code
    def _inline_code(m: re.Match) -> str:
        code = m.group(1)
        escaped = code.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return _placeholder(f"<code>{escaped}</code>")

    text = re.sub(r"`([^`]+)`", _inline_code, text)

    # 3. Extract tables (consecutive lines starting with |)
    def _table_block(m: re.Match) -> str:
        lines = m.group(0).strip().split("\n")
        cleaned = []
        for line in lines:
            # Skip separator rows like |---|---|
            if re.match(r"^\|[\s\-:|]+\|$", line):
                continue
            # Strip leading/trailing pipes and clean cells
            cells = [c.strip() for c in line.strip("|").split("|")]
            cleaned.append(" | ".join(cells))
        table_text = "\n".join(cleaned)
        escaped = (
            table_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        )
        return _placeholder(f"<pre>{escaped}</pre>")

    text = re.sub(r"(?:^\|.+\|$\n?)+", _table_block, text, flags=re.MULTILINE)

    # 4. Escape HTML entities in remaining text
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    # 5. Markdown links [text](url)
    text = re.sub(
        r"\[([^\]]+)\]\(([^)]+)\)",
        r'<a href="\2">\1</a>',
        text,
    )

    # 6. Bold **text**
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)

    # 7. Italic *text* (but not inside words like file*name)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"<i>\1</i>", text)

    # 8. Headers # ... (strip hashes, make bold)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", text, flags=re.MULTILINE)

    # 9. Bullet lists (- item or * item at line start)
    text = re.sub(r"^[\-\*]\s+", "  \u2022 ", text, flags=re.MULTILINE)

    # 10. Strip horizontal rules
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)

    # 11. Restore placeholders
    for idx, content in enumerate(placeholders):
        text = text.replace(f"\x00PH{idx}\x00", content)

    return text.strip()


async def send_long(update: Update, text: str) -> None:
    """Send text as HTML, splitting at Telegram's 4096-char limit on line boundaries."""
    # Extract action markers before markdown conversion
    actions: list[tuple[str, str]] = _ACTION_RE.findall(text)
    text = _ACTION_RE.sub("", text)

    # Extract stats marker before markdown conversion
    stats_html = ""
    if "<!-- stats:" in text:
        parts = text.split("<!-- stats:", 1)
        text = parts[0].rstrip()
        stats_line = parts[1].split(" -->", 1)[0]
        stats_html = "\n<blockquote expandable>" + stats_line + "</blockquote>"
    html = markdown_to_telegram_html(text) + stats_html

    # Send text chunks
    while html:
        if len(html) <= TELEGRAM_MAX_LEN:
            chunk = html
            html = ""
        else:
            split_at = html.rfind('\n', 0, TELEGRAM_MAX_LEN)
            if split_at == -1:
                split_at = TELEGRAM_MAX_LEN
            chunk = html[:split_at]
            html = html[split_at:].lstrip('\n')
        try:
            await update.message.reply_text(chunk, parse_mode="HTML")
        except BadRequest as e:
            logger.warning("HTML parse failed, falling back to plain text: %s", e)
            await update.message.reply_text(chunk)

    # Execute extracted actions
    for action_type, payload in actions:
        handler = _ACTION_HANDLERS.get(action_type)
        if handler:
            try:
                await handler(update, payload)
            except Exception:
                logger.exception("Action handler failed: %s", action_type)
        else:
            logger.warning("Unknown action type: %s", action_type)


def _redis(context: ContextTypes.DEFAULT_TYPE) -> aioredis.Redis:
    return context.bot_data["redis"]


# ── Auth wrapper ─────────────────────────────────────────────────────────
def auth_required(handler):
    """Decorator that checks ALLOWED_CHATS before running the handler."""
    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        chat_id = update.effective_chat.id
        if not authorized(chat_id):
            logger.warning("Unauthorized access attempt", extra={
                "chat_id": chat_id,
                "user_id": update.effective_user.id,
            })
            await update.message.reply_text("Not authorized.")
            return
        return await handler(update, context)
    return wrapper


# ── Command handlers ─────────────────────────────────────────────────────
@auth_required
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "h-cli bot ready.\n"
        "Use /help to see available commands.",
        reply_markup=_model_keyboard(),
    )


@auth_required
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Send any message in natural language and I'll figure out the right tool.\n\n"
        "/run <command> — Execute a shell command directly\n"
        "/new    — Clear context, start a fresh conversation\n"
        "/cancel — Cancel the last queued task\n"
        "/abort  — Kill the currently running task\n"
        "/status — Show task queue depth\n"
        "/stats  — Today's usage stats (tokens, cost, tasks)\n"
        "/help   — This message\n\n"
        "📝 Teach — Start teaching a new skill. "
        "Chat normally, then press 📖 End Teaching to generate a skill draft."
    )


@auth_required
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    r = _redis(context)
    depth = await r.llen(REDIS_TASKS_KEY)
    await update.message.reply_text(f"Tasks in queue: {depth}")


STATS_KEY_PREFIX = "hcli:stats:"


@auth_required
async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show today's usage stats from Redis counters."""
    r = _redis(context)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = await r.hgetall(f"{STATS_KEY_PREFIX}{today}")

    if not stats:
        await update.message.reply_text("No stats for today yet.")
        return

    tasks = int(stats.get("tasks", 0))
    errors = int(stats.get("errors", 0))
    in_tok = int(stats.get("input_tokens", 0))
    out_tok = int(stats.get("output_tokens", 0))
    cache_r = int(stats.get("cache_read", 0))
    cost = float(stats.get("cost_usd", 0))
    dur_ms = int(stats.get("duration_ms", 0))
    turns = int(stats.get("num_turns", 0))

    gate_calls = int(stats.get("gate_calls", 0))
    gate_cost = float(stats.get("gate_cost_usd", 0))
    gate_in = int(stats.get("gate_input_tokens", 0))
    gate_out = int(stats.get("gate_output_tokens", 0))

    avg_dur = (dur_ms / tasks / 1000) if tasks else 0
    avg_turns = (turns / tasks) if tasks else 0
    error_pct = (100 * errors / tasks) if tasks else 0
    total_cost = cost + gate_cost

    lines = [
        f"Stats for {today}",
        f"Tasks: {tasks} ({errors} errors, {error_pct:.0f}%)",
        f"Tokens: {in_tok:,} in / {out_tok:,} out / {cache_r:,} cache",
        f"Avg response: {avg_dur:.1f}s ({avg_turns:.1f} turns)",
        f"Gate: {gate_calls} checks, {gate_in + gate_out:,} tokens",
        f"Cost: ${cost:.4f} main + ${gate_cost:.4f} gate = ${total_cost:.4f}",
    ]
    await update.message.reply_text("\n".join(lines))


SKILLS_DIRS = ["/app/skills/public", "/app/skills/private"]


@auth_required
async def cmd_skills(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """List all loaded skills with their keywords."""
    lines = []
    total = 0
    for skills_dir in SKILLS_DIRS:
        if not os.path.isdir(skills_dir):
            continue
        scope = os.path.basename(skills_dir)
        try:
            entries = sorted(os.listdir(skills_dir))
        except OSError:
            continue
        for fname in entries:
            if not fname.endswith(".md") or fname == "README.md":
                continue
            fpath = os.path.join(skills_dir, fname)
            try:
                with open(fpath) as f:
                    content = f.read(500)  # only need the header
            except OSError:
                continue
            keywords = ""
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    for line in content[3:end].splitlines():
                        if line.strip().lower().startswith("keywords:"):
                            keywords = line.split(":", 1)[1].strip()
                            break
            name = fname[:-3]
            tag = "" if scope == "public" else " [private]"
            if keywords:
                lines.append(f"  \u2022 {name} — {keywords}{tag}")
            else:
                lines.append(f"  \u2022 {name}{tag}")
            total += 1

    if not lines:
        await update.message.reply_text("No skills loaded.")
        return
    header = f"\U0001f4da Skills ({total})"
    await update.message.reply_text(header + "\n" + "\n".join(lines))


async def _dump_session_chunk(r: aioredis.Redis, chat_id: int) -> str | None:
    """Dump session history from Redis to a chunk file on disk."""
    history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
    turns = await r.lrange(history_key, 0, -1)
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
            f.write(f"Session: /new\n")
            f.write(f"Chunked: {timestamp}\n")
            f.write(f"Turns: {len(turns)}\n")
            f.write("===\n\n")
            for turn_json in turns:
                turn = json.loads(turn_json)
                role = turn.get("role", "unknown").upper()
                ts = datetime.fromtimestamp(
                    turn.get("timestamp", 0), tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M:%S UTC")
                content = turn.get("content", "")
                f.write(f"[{ts}] {role}:\n{content}\n\n---\n\n")
    except OSError as e:
        logger.error("Failed to write session chunk %s: %s", chunk_path, e)
        return None

    await r.delete(history_key)
    await r.delete(f"{SESSION_SIZE_PREFIX}{chat_id}")
    logger.info("Session chunk saved: %s (%d turns)", chunk_path, len(turns))
    return chunk_path


@auth_required
async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dump session history to a chunk file, then clear the session."""
    r = _redis(context)
    chat_id = update.effective_chat.id
    chunk_path = await _dump_session_chunk(r, chat_id)
    await r.delete(f"hcli:session:{chat_id}")
    if chunk_path:
        await update.message.reply_text(
            f"Session saved to {os.path.basename(chunk_path)}. "
            "Context cleared — next message starts fresh.",
            reply_markup=_model_keyboard(),
        )
    else:
        await update.message.reply_text(
            "Context cleared. Next message starts fresh.",
            reply_markup=_model_keyboard(),
        )


@auth_required
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel the most recent pending/in-flight task for this chat."""
    r = _redis(context)
    chat_id = update.effective_chat.id
    chat_tasks_key = _chat_tasks_key(chat_id)

    # Pop the most recent pending task for this chat
    task_id = await r.rpop(chat_tasks_key)
    if not task_id:
        await update.message.reply_text("No queued tasks to cancel.")
        return

    # Try to remove from the dispatch queue (may already be picked up)
    tasks = await r.lrange(REDIS_TASKS_KEY, 0, -1)
    for raw_task in reversed(tasks):
        try:
            task = json.loads(raw_task)
        except json.JSONDecodeError:
            continue
        if task.get("task_id") == task_id:
            await r.lrem(REDIS_TASKS_KEY, -1, raw_task)
            break

    # Write a signed cancellation result so the subscriber picks it up
    output = "Task cancelled."
    completed_at = datetime.now(timezone.utc).isoformat()
    msg = f"{task_id}:{output}:{completed_at}"
    sig = hmac.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()
    result = json.dumps({
        "output": output,
        "completed_at": completed_at,
        "hmac": sig,
    })
    await r.set(f"{REDIS_RESULT_PREFIX}{task_id}", result, ex=TASK_TIMEOUT)

    audit.info(
        "task_cancelled",
        extra={"user_id": update.effective_user.id, "task_id": task_id},
    )
    await update.message.reply_text(f"Cancelled task {task_id[:8]}.")


@auth_required
async def cmd_abort(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Abort the currently running task — signal dispatcher to kill subprocess."""
    r = _redis(context)
    chat_id = update.effective_chat.id
    chat_tasks_key = _chat_tasks_key(chat_id)

    # Get the most recent task (still in-flight)
    task_id = await r.lindex(chat_tasks_key, -1)
    if not task_id:
        await update.message.reply_text("No active task to abort.")
        return

    # Signal the dispatcher to kill the subprocess via control channel
    control_channel = f"{REDIS_CONTROL_PREFIX}{task_id}"
    await r.publish(control_channel, json.dumps({"action": "abort"}))

    audit.info(
        "task_aborted",
        extra={"user_id": update.effective_user.id, "task_id": task_id},
    )
    await update.message.reply_text(f"Aborted task {task_id[:8]}.")


def _format_activity(task_id: str, commands: list[dict], done: bool) -> str:
    """Format the activity stream message."""
    icon = "\u2705" if done else "\u23f3"
    header = f"{icon} Task {task_id[:8]}"
    if not commands:
        return header + ("\n\nDone \u2014 no commands captured." if done else "")
    lines = [header, ""]
    for entry in commands:
        cmd = entry["cmd"]
        if len(cmd) > ACTIVITY_CMD_MAX_LEN:
            cmd = cmd[:ACTIVITY_CMD_MAX_LEN - 3] + "..."
        lines.append(f"> {cmd}")
        if entry["done"]:
            dur = f" {entry['duration']:.1f}s" if entry.get("duration") is not None else ""
            lines.append(f"\u2713{dur}")
        else:
            lines.append("\u23f3 running...")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _stream_activity(
    chat_id: int, task_id: str, msg, commands: list[dict],
    r: aioredis.Redis | None = None,
) -> None:
    """Subscribe to audit channel and stream activity to editable message.

    Checks task state on idle to detect abort/completion without waiting
    for the full idle timeout.
    """
    channel = f"hcli:audit:{task_id}"
    state_key = f"hcli:task:{task_id}:state"
    last_event_time = time.monotonic()
    last_edit_time = 0.0
    last_state_check = 0.0
    pending_edit = False
    STATE_CHECK_INTERVAL = 5  # check task state every 5s of idle

    # Dedicated connection for pub/sub (can't share with main pool)
    pubsub_redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = pubsub_redis.pubsub()

    try:
        await pubsub.subscribe(channel)

        while True:
            raw = await pubsub.get_message(
                ignore_subscribe_messages=True, timeout=1.0,
            )
            now = time.monotonic()

            if raw is not None and raw["type"] == "message":
                last_event_time = now
                try:
                    event = json.loads(raw["data"])
                except (json.JSONDecodeError, TypeError):
                    pass
                else:
                    cmd = event.get("command", "")
                    status = event.get("status", "")
                    duration_ms = event.get("duration_ms")
                    duration = duration_ms / 1000 if duration_ms is not None else None

                    if status == "running" and cmd:
                        # Mark previous running command as done
                        if commands and not commands[-1]["done"]:
                            commands[-1]["done"] = True
                        commands.append({"cmd": cmd, "done": False, "duration": None})
                    elif status in ("completed", "failed") and cmd:
                        # Find and update matching running command
                        for i in range(len(commands) - 1, -1, -1):
                            if commands[i]["cmd"] == cmd and not commands[i]["done"]:
                                commands[i]["done"] = True
                                commands[i]["duration"] = duration
                                break
                        else:
                            commands.append({"cmd": cmd, "done": True, "duration": duration})

                    # Keep last N (slice assign to preserve shared reference)
                    if len(commands) > ACTIVITY_MAX_COMMANDS:
                        commands[:] = commands[-ACTIVITY_MAX_COMMANDS:]
                    pending_edit = True

            # Check task state during idle — detect abort/completion early
            idle_time = now - last_event_time
            if r and idle_time > STATE_CHECK_INTERVAL and now - last_state_check > STATE_CHECK_INTERVAL:
                last_state_check = now
                try:
                    state = await r.get(state_key)
                    if state in ("completed", "failed", "aborted", "timed_out", "cancelled"):
                        text = _format_activity(task_id, commands, done=True)
                        try:
                            await msg.edit_text(text)
                        except Exception:
                            pass
                        return
                except aioredis.RedisError:
                    pass

            # Idle timeout — task likely done
            if idle_time > ACTIVITY_IDLE_TIMEOUT:
                text = _format_activity(task_id, commands, done=True)
                try:
                    await msg.edit_text(text)
                except Exception:
                    pass
                return

            # Rate-limited edit
            if pending_edit and now - last_edit_time >= ACTIVITY_EDIT_INTERVAL:
                text = _format_activity(task_id, commands, done=False)
                try:
                    await msg.edit_text(text)
                except Exception:
                    pass
                last_edit_time = now
                pending_edit = False

    except asyncio.CancelledError:
        return
    except Exception:
        logger.exception("Activity stream error for task %s", task_id[:8])
    finally:
        try:
            await pubsub.unsubscribe(channel)
            await pubsub.close()
            await pubsub_redis.aclose()
        except Exception:
            pass


async def _queue_task(
    update: Update, context: ContextTypes.DEFAULT_TYPE, message: str,
) -> None:
    """Check concurrency, queue task to Redis, poll for result."""
    r = _redis(context)
    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    depth = await r.llen(REDIS_TASKS_KEY)
    if depth >= MAX_CONCURRENT_TASKS:
        await update.message.reply_text(
            f"Queue full ({depth}/{MAX_CONCURRENT_TASKS}). Try again later."
        )
        return

    task_id = str(uuid.uuid4())
    task_payload = json.dumps({
        "task_id": task_id,
        "message": message,
        "user_id": uid,
        "chat_id": chat_id,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "model": _chat_model.get(chat_id, "opus"),
    })

    await r.rpush(REDIS_TASKS_KEY, task_payload)
    chat_tasks_key = _chat_tasks_key(chat_id)
    await r.rpush(chat_tasks_key, task_id)
    await r.expire(chat_tasks_key, TASK_TIMEOUT * 2)
    audit.info(
        "task_queued",
        extra={"user_id": uid, "task_id": task_id, "user_message": message},
    )
    logger.info("Task queued: %s (id=%s, model=%s)", message, task_id, _chat_model.get(chat_id, "opus"))

    poll_task = asyncio.create_task(
        _poll_result(update, r, task_id, uid, user_message=message)
    )
    _background_tasks.add(poll_task)
    poll_task.add_done_callback(_background_tasks.discard)


@auth_required
async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    command = " ".join(context.args) if context.args else ""
    if not command:
        await update.message.reply_text("Usage: /run <command>")
        return
    await _queue_task(update, context, command)


async def _poll_result(
    update: Update, r: aioredis.Redis, task_id: str, uid: int,
    user_message: str = "",
) -> None:
    """Subscribe to task notify channel, with GET fallback every 10s."""
    chat_id = update.effective_chat.id
    verbose = _verbose_mode.get(chat_id, True)
    activity_msg = None
    activity_task = None
    activity_commands: list[dict] = []  # shared with _stream_activity

    if verbose:
        activity_msg = await update.message.reply_text(
            _format_activity(task_id, [], done=False),
        )
        activity_task = asyncio.create_task(
            _stream_activity(chat_id, task_id, activity_msg, activity_commands, r=r),
        )
        _background_tasks.add(activity_task)
        activity_task.add_done_callback(_background_tasks.discard)

    chat_tasks_key = _chat_tasks_key(chat_id)
    result_key = f"{REDIS_RESULT_PREFIX}{task_id}"
    notify_channel = f"hcli:task:{task_id}:notify"

    # Dedicated connection for result notification subscribe
    notify_redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = notify_redis.pubsub()
    raw = None

    try:
        await pubsub.subscribe(notify_channel)

        # Immediate GET check — covers race where result arrived before subscribe
        try:
            raw = await r.get(result_key)
        except aioredis.RedisError as e:
            logger.warning("Redis error on initial GET for task %s: %s", task_id[:8], e)

        if raw is None:
            # Wait for notification with fallback GET every NOTIFY_POLL_FALLBACK seconds
            deadline = time.monotonic() + TASK_TIMEOUT
            while raw is None and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                timeout = min(NOTIFY_POLL_FALLBACK, max(remaining, 0.1))
                msg = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=timeout,
                )
                # Whether notification arrived or timeout fired, check for result
                try:
                    raw = await r.get(result_key)
                except aioredis.RedisError as e:
                    logger.warning("Redis error polling task %s: %s", task_id[:8], e)

                if raw is None and not verbose:
                    await update.effective_chat.send_action("typing")

        # Stop activity stream
        if activity_task and not activity_task.done():
            activity_task.cancel()

        if raw is None:
            # Timeout
            if activity_msg:
                try:
                    await activity_msg.edit_text(
                        _format_activity(task_id, activity_commands, done=True),
                    )
                except Exception:
                    pass
            await update.message.reply_text(
                f"Task {task_id[:8]} timed out after {TASK_TIMEOUT}s."
            )
            audit.info(
                "task_timeout",
                extra={"user_id": uid, "task_id": task_id, "timeout": TASK_TIMEOUT},
            )
            return

        # Got result — process it
        if activity_msg:
            try:
                await activity_msg.edit_text(
                    _format_activity(task_id, activity_commands, done=True),
                )
            except Exception:
                pass

        await r.delete(result_key)
        await r.lrem(chat_tasks_key, 1, task_id)
        try:
            result = json.loads(raw)
            if not _verify_result(task_id, result):
                logger.warning("HMAC verification failed for task %s", task_id)
                audit.warning(
                    "hmac_failed",
                    extra={"task_id": task_id},
                )
                output = "(error: result integrity check failed)"
            else:
                output = result.get("output", "(no output)")
                usage = result.get("usage")
                if usage:
                    in_t = usage.get("input_tokens", 0)
                    out_t = usage.get("output_tokens", 0)
                    cost = usage.get("cost_usd", 0)
                    dur = usage.get("duration_ms", 0)
                    dur_s = dur / 1000 if dur else 0
                    mdl = usage.get("model", "?")
                    stats = "{} \u2191 {:,} \u2193 {:,} | ${:.4f} | {:.1f}s".format(mdl, in_t, out_t, cost, dur_s)
                    output = output + "\n\n<!-- stats:" + stats + " -->"
        except json.JSONDecodeError:
            output = "(error: malformed result)"

        # Buffer teach turns if teach mode is active
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        if await r.exists(teach_key):
            turns_key = f"{TEACH_PREFIX}{chat_id}:turns"
            if user_message:
                await r.rpush(turns_key, json.dumps(
                    {"role": "user", "content": user_message}
                ))
            await r.rpush(turns_key, json.dumps(
                {"role": "assistant", "content": output}
            ))
            await r.expire(turns_key, TEACH_TTL)

        await send_long(update, output)
        audit.info(
            "task_completed",
            extra={"user_id": uid, "task_id": task_id},
        )

    except asyncio.CancelledError:
        return
    except aioredis.RedisError as e:
        logger.error("Redis error waiting for task %s: %s", task_id[:8], e)
        await update.message.reply_text(
            f"Lost connection to backend while waiting for task {task_id[:8]}."
        )
        audit.warning(
            "task_redis_error",
            extra={"user_id": uid, "task_id": task_id},
        )
    finally:
        if activity_task and not activity_task.done():
            activity_task.cancel()
        try:
            await pubsub.unsubscribe(notify_channel)
            await pubsub.close()
            await notify_redis.aclose()
        except Exception:
            pass
        try:
            await r.lrem(chat_tasks_key, 1, task_id)
        except aioredis.RedisError:
            pass


@auth_required
async def handle_keyboard_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle persistent keyboard button presses (model toggle + teach)."""
    text = update.message.text.strip()
    chat_id = update.effective_chat.id
    r = _redis(context)

    if text == "⚡ Fast":
        _chat_model[chat_id] = "haiku"
        await update.message.reply_text(
            "⚡ Fast mode (Haiku)",
            reply_markup=_model_keyboard(),
        )
    elif text == "🧠 Deep":
        _chat_model[chat_id] = "opus"
        await update.message.reply_text(
            "🧠 Deep mode (Opus)",
            reply_markup=_model_keyboard(),
        )
    elif text == "📊 Stats":
        await cmd_stats(update, context)
        return
    elif text == "📚 Skills":
        await cmd_skills(update, context)
        return
    elif text == "📡 Verbose":
        current = _verbose_mode.get(chat_id, True)
        _verbose_mode[chat_id] = not current
        state = "ON" if not current else "OFF"
        await update.message.reply_text(
            f"Verbose mode: {state}",
            reply_markup=_model_keyboard(),
        )
        return
    elif text == "📝 Teach":
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        await r.set(teach_key, "1", ex=TEACH_TTL)
        await update.message.reply_text(
            "📝 Teaching mode activated.\n"
            "Chat normally — all turns are being buffered.\n"
            "Press 📖 End Teaching when done.",
            reply_markup=_model_keyboard(),
        )
        audit.info("teach_start", extra={
            "user_id": update.effective_user.id, "chat_id": chat_id,
        })
    elif text == "📖 End Teaching":
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        turns_key = f"{TEACH_PREFIX}{chat_id}:turns"

        raw_turns = await r.lrange(turns_key, 0, -1)
        await r.delete(teach_key, turns_key)

        if not raw_turns:
            await update.message.reply_text(
                "No teaching data collected.",
                reply_markup=_model_keyboard(),
            )
            return

        # Format turns into a skill generation prompt
        session_lines = []
        for raw in raw_turns:
            turn = json.loads(raw)
            role = turn["role"].upper()
            content = turn["content"]
            session_lines.append(f"{role}: {content}")
        session_text = "\n\n".join(session_lines)

        MAX_TEACH_BYTES = 100_000
        if len(session_text) > MAX_TEACH_BYTES:
            logger.warning(
                "Teach session truncated: %d bytes -> %d bytes (chat %s)",
                len(session_text), MAX_TEACH_BYTES, chat_id,
            )
            session_text = session_text[-MAX_TEACH_BYTES:]

        prompt = (
            "You are extracting a reusable skill from a teaching session.\n\n"
            "RULES:\n"
            "- Extract the principle, not the instance. Generalize examples into rules.\n"
            "- Only write a rule if it was demonstrated or explicitly stated. No inferences.\n"
            "- Most skills involve: REST API calls, terminal commands, Playwright automation, or VNC sequences.\n"
            "- Keep it concise. One skill file, focused on one topic.\n\n"
            "OUTPUT FORMAT — show the full draft in your reply, do NOT write it to disk yet. Ask the user if they want to save it. If they say yes, write it to /tmp/skills/{topic}.md using run_command. If no, discard it.\n"
            "---\n"
            "keywords: (trigger words that should activate this skill)\n"
            "---\n"
            "# {Topic}\n\n"
            "## Trigger\n"
            "When does this skill activate? What\'s in scope, what\'s not?\n\n"
            "## Constraints\n"
            "Hard rules, non-negotiable.\n\n"
            "## Procedure\n"
            "Ordered steps with actual commands/API calls.\n\n"
            "## Anti-patterns\n"
            "What NOT to do. Exceptions to the rules.\n\n"
            f"Teaching session:\n{session_text}"
        )

        await update.message.reply_text(
            "📖 Generating skill draft...",
            reply_markup=_model_keyboard(),
        )
        audit.info("teach_end", extra={
            "user_id": update.effective_user.id, "chat_id": chat_id,
            "turns": len(raw_turns),
        })
        await _queue_task(update, context, prompt)


@auth_required
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle natural language messages — queue to Redis for Claude Code."""
    message = update.message.text.strip()
    if not message:
        return
    await _queue_task(update, context, message)


@auth_required
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle photo messages — download, base64 encode, queue with data URI."""
    photo = update.message.photo[-1]  # highest resolution
    file = await context.bot.get_file(photo.file_id)
    photo_bytes = await file.download_as_bytearray()

    # Save original photo to disk for training pipeline (best-effort)
    try:
        media_dir = Path(f"/var/log/hcli/media/{update.effective_chat.id}")
        media_dir.mkdir(parents=True, exist_ok=True)
        media_path = media_dir / f"{int(time.time())}_{photo.file_id[:8]}.jpg"
        with open(media_path, "wb") as f:
            f.write(bytes(photo_bytes))
    except OSError as e:
        logger.warning("Failed to save photo to disk: %s", e)

    b64 = base64.b64encode(bytes(photo_bytes)).decode()

    caption = (update.message.caption or "").strip()
    if caption:
        message = f"{caption}\n\ndata:image/jpeg;base64,{b64}"
    else:
        message = f"The user sent this image.\n\ndata:image/jpeg;base64,{b64}"

    await _queue_task(update, context, message)


@auth_required
async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle location messages — extract coordinates, queue as text."""
    loc = update.message.location
    message = f"User shared a location: latitude {loc.latitude}, longitude {loc.longitude}"

    if update.message.venue:
        venue = update.message.venue
        message = f"User shared a location: {venue.title} ({venue.address}) — latitude {loc.latitude}, longitude {loc.longitude}"

    await _queue_task(update, context, message)


# ── App lifecycle ────────────────────────────────────────────────────────
async def post_init(application: Application) -> None:
    pool = aioredis.ConnectionPool.from_url(
        REDIS_URL, decode_responses=True,
        socket_connect_timeout=5, socket_timeout=10,
    )
    application.bot_data["redis"] = aioredis.Redis(connection_pool=pool)
    application.bot_data["redis_pool"] = pool
    logger.info("Redis connection pool created (%s)", REDIS_URL.split("@")[-1])
    await application.bot.set_my_commands([
        BotCommand("start", "Initialize bot and show keyboard"),
        BotCommand("help", "Show available commands"),
        BotCommand("new", "Clear context, start fresh conversation"),
        BotCommand("run", "Execute a shell command directly"),
        BotCommand("cancel", "Cancel the last queued task"),
        BotCommand("abort", "Kill the currently running task"),
        BotCommand("status", "Show task queue depth"),
        BotCommand("stats", "Today's usage stats"),
    ])
    logger.info(
        "Bot started — allowed chats: %s, max tasks: %d, timeout: %ds",
        ALLOWED_CHATS or "(none)",
        MAX_CONCURRENT_TASKS,
        TASK_TIMEOUT,
    )


async def post_shutdown(application: Application) -> None:
    pool = application.bot_data.get("redis_pool")
    if pool:
        await pool.aclose()
        logger.info("Redis connection pool closed")


# ── Main ─────────────────────────────────────────────────────────────────
def main() -> None:
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("abort", cmd_abort))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(MessageHandler(
        filters.TEXT & filters.Regex(r"^(⚡ Fast|🧠 Deep|📊 Stats|📚 Skills|📝 Teach|📖 End Teaching|📡 Verbose)$"),
        handle_keyboard_button,
    ))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Starting Telegram bot polling...")
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
