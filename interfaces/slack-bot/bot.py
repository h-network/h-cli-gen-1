"""h-cli Slack Bot — async command interface with Redis task queue."""

import asyncio
import hashlib
import hmac as hmac_mod
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
import httpx
import redis.asyncio as aioredis
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_sdk.web.async_client import AsyncWebClient

from hcli_logging import get_logger, get_audit_logger

logger = get_logger(__name__, service="slack")
audit = get_audit_logger("slack")

# ── Config ───────────────────────────────────────────────────────────────
SLACK_BOT_TOKEN = os.environ["SLACK_BOT_TOKEN"]
SLACK_APP_TOKEN = os.environ["SLACK_APP_TOKEN"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "300"))

ALLOWED_USERS: set[str] = set()
_raw = os.environ.get("ALLOWED_USERS", "")
if _raw.strip():
    for uid in _raw.split(","):
        uid = uid.strip()
        if uid:
            ALLOWED_USERS.add(uid)

if not ALLOWED_USERS:
    logger.warning(
        "ALLOWED_USERS is empty — no users are authorized. "
        "The bot will reject all messages."
    )

RESULT_HMAC_KEY = os.environ.get("RESULT_HMAC_KEY", "")
if not RESULT_HMAC_KEY:
    raise RuntimeError("RESULT_HMAC_KEY not set — run install.sh to generate one")

# Slack limits
SLACK_MSG_MAX_LEN = 3000      # practical display limit
SLACK_SNIPPET_MAX_LEN = 15000  # above this, upload as file
SLACK_MRKDWN_MAX_LEN = 40000  # hard API limit

# Redis keys — same contracts as telegram-bot
REDIS_TASKS_KEY = "hcli:tasks"
REDIS_RESULT_PREFIX = "hcli:results:"
REDIS_CONTROL_PREFIX = "hcli:control:"
SESSION_HISTORY_PREFIX = "hcli:session_history:"
SESSION_SIZE_PREFIX = "hcli:session_size:"
SESSION_CHUNK_DIR = os.environ.get("SESSION_CHUNK_DIR", "/var/log/hcli/sessions")
NOTIFY_POLL_FALLBACK = 10
TEACH_PREFIX = "hcli:teach:"
TEACH_TTL = 3600
STATS_KEY_PREFIX = "hcli:stats:"
SKILLS_DIRS = ["/app/skills/public", "/app/skills/private"]

# Activity stream settings
ACTIVITY_IDLE_TIMEOUT = 30
ACTIVITY_MAX_COMMANDS = 8
ACTIVITY_CMD_MAX_LEN = int(os.environ.get("ACTIVITY_CMD_MAX_LEN", "150"))
ACTIVITY_EDIT_INTERVAL = 1
LONG_RUNNING_THRESHOLD = 30
LONG_RUNNING_UPDATE_INTERVAL = 10

# Per-channel state (ephemeral — resets on restart)
_chat_model: dict[str, str] = {}        # channel_id → "haiku" or "opus"
_verbose_mode: dict[str, bool] = {}     # channel_id → verbose toggle

_CHAT_NAMES = {}
for _pair in os.environ.get("CHAT_NAMES", "").split(","):
    if ":" in _pair:
        _cid, _name = _pair.strip().split(":", 1)
        _CHAT_NAMES[_cid.strip()] = _name.strip()

_background_tasks: set[asyncio.Task] = set()

# Grafana action system
_ACTION_RE = re.compile(r'\[action:(\w+):([^\]]+)\]')
GRAFANA_INTERNAL_URL = os.environ.get("GRAFANA_INTERNAL_URL", "")
GRAFANA_ADMIN_PASSWORD = os.environ.get("GRAFANA_ADMIN_PASSWORD", "")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "")
GRAFANA_API_TOKEN = os.environ.get("GRAFANA_API_TOKEN", "")

# ── Redis pool ───────────────────────────────────────────────────────────
_redis_pool: aioredis.ConnectionPool | None = None
_redis_client: aioredis.Redis | None = None


async def _get_redis() -> aioredis.Redis:
    global _redis_pool, _redis_client
    if _redis_client is None:
        _redis_pool = aioredis.ConnectionPool.from_url(
            REDIS_URL, decode_responses=True,
            socket_connect_timeout=5, socket_timeout=10,
        )
        _redis_client = aioredis.Redis(connection_pool=_redis_pool)
        logger.info("Redis connection pool created (%s)", REDIS_URL.split("@")[-1])
    return _redis_client


# ── Slack app ────────────────────────────────────────────────────────────
app = AsyncApp(token=SLACK_BOT_TOKEN)


# ── Helpers ──────────────────────────────────────────────────────────────
def _chat_dir_name(chat_id: str) -> str:
    return _CHAT_NAMES.get(str(chat_id), str(chat_id))


def _chat_tasks_key(chat_id: str) -> str:
    return f"hcli:chat:{chat_id}:tasks"


def _thread_chat_id(channel: str, thread_ts: str | None) -> str:
    if thread_ts:
        return f"{channel}:{thread_ts}"
    return channel


def authorized(user_id: str) -> bool:
    return user_id in ALLOWED_USERS


def _verify_result(task_id: str, result: dict) -> bool:
    expected = result.get("hmac", "")
    msg = f"{task_id}:{result.get('output', '')}:{result.get('completed_at', '')}"
    computed = hmac_mod.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()
    return hmac_mod.compare_digest(expected, computed)


def markdown_to_slack_mrkdwn(text: str) -> str:
    """Convert standard markdown to Slack mrkdwn format."""
    placeholders: list[str] = []

    def _placeholder(content: str) -> str:
        idx = len(placeholders)
        placeholders.append(content)
        return f"\x00PH{idx}\x00"

    # 1. Preserve fenced code blocks
    def _code_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = m.group(2)
        return _placeholder(f"```{lang}\n{code}```")

    text = re.sub(r"```(\w*)\n?(.*?)```", _code_block, text, flags=re.DOTALL)

    # 2. Preserve inline code
    def _inline_code(m: re.Match) -> str:
        return _placeholder(f"`{m.group(1)}`")

    text = re.sub(r"`([^`]+)`", _inline_code, text)

    # 3. Links [text](url) → <url|text>
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"<\2|\1>", text)

    # 4. Bold **text** → *text*
    text = re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)

    # 5. Italic *text* → _text_ (only standalone, not inside words)
    text = re.sub(r"(?<!\w)\*([^*]+?)\*(?!\w)", r"_\1_", text)

    # 6. Headers # ... → *bold text*
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)

    # 7. Bullet lists — Slack handles - and * natively, normalize to •
    text = re.sub(r"^[\-\*]\s+", "  • ", text, flags=re.MULTILINE)

    # 8. Strip horizontal rules
    text = re.sub(r"^-{3,}$", "", text, flags=re.MULTILINE)

    # 9. Restore placeholders
    for idx, content in enumerate(placeholders):
        text = text.replace(f"\x00PH{idx}\x00", content)

    return text.strip()


async def _handle_graph_action(
    client: AsyncWebClient, channel: str, thread_ts: str, payload: str,
) -> None:
    """Fetch Grafana render PNG and upload as file in thread."""
    auth: tuple[str, str] | None = None
    headers: dict[str, str] = {}

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
        async with httpx.AsyncClient(verify=False) as http:
            resp = await http.get(payload, auth=auth, headers=headers, timeout=30)
    except httpx.HTTPError as e:
        logger.error("Graph fetch failed: %s", e)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts, text="Failed to fetch graph.",
        )
        return

    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
        await client.files_upload_v2(
            channel=channel, thread_ts=thread_ts,
            content=resp.content, filename="graph.png", title="Grafana Graph",
        )
    else:
        logger.warning(
            "Graph render failed: HTTP %d, content-type=%s",
            resp.status_code, resp.headers.get("content-type", ""),
        )
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"Failed to render graph (HTTP {resp.status_code}).",
        )


async def send_response(
    client: AsyncWebClient, channel: str, thread_ts: str, text: str,
) -> None:
    """Send response with appropriate method based on length."""
    # Extract action markers
    actions: list[tuple[str, str]] = _ACTION_RE.findall(text)
    text = _ACTION_RE.sub("", text)

    # Extract stats marker
    stats_text = ""
    if "<!-- stats:" in text:
        parts = text.split("<!-- stats:", 1)
        text = parts[0].rstrip()
        stats_line = parts[1].split(" -->", 1)[0]
        stats_text = f"\n> {stats_line}"

    mrkdwn = markdown_to_slack_mrkdwn(text) + stats_text

    if len(mrkdwn) <= SLACK_MSG_MAX_LEN:
        # Short — send inline
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=mrkdwn, mrkdwn=True,
        )
    elif len(mrkdwn) <= SLACK_SNIPPET_MAX_LEN:
        # Medium — upload as snippet (renders inline, expandable)
        await client.files_upload_v2(
            channel=channel, thread_ts=thread_ts,
            content=mrkdwn, filename="response.md",
            title="h-cli response",
        )
    else:
        # Large — upload as file
        await client.files_upload_v2(
            channel=channel, thread_ts=thread_ts,
            content=mrkdwn, filename="response.md",
            title="h-cli response (large output)",
        )

    # Execute extracted actions
    for action_type, action_payload in actions:
        if action_type == "graph":
            try:
                await _handle_graph_action(client, channel, thread_ts, action_payload)
            except Exception:
                logger.exception("Action handler failed: %s", action_type)
        else:
            logger.warning("Unknown action type: %s", action_type)


# ── Session chunking ────────────────────────────────────────────────────
async def _dump_session_chunk(r: aioredis.Redis, chat_id: str) -> str | None:
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
            f.write("Session: /new\n")
            f.write(f"Chunked: {timestamp}\n")
            f.write(f"Turns: {len(turns)}\n")
            f.write("===\n\n")
            for turn_json in turns:
                turn = json.loads(turn_json)
                role = turn.get("role", "unknown").upper()
                ts = datetime.fromtimestamp(
                    turn.get("timestamp", 0), tz=timezone.utc,
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


# ── Activity stream ──────────────────────────────────────────────────────
def _format_activity(task_id: str, commands: list[dict], done: bool) -> str:
    icon = "\u2705" if done else "\u23f3"
    header = f"{icon} Task {task_id[:8]}"
    if not commands:
        return header + ("\n\nDone — no commands captured." if done else "")
    lines = [header, ""]
    now = time.monotonic()
    for entry in commands:
        cmd = entry["cmd"]
        if len(cmd) > ACTIVITY_CMD_MAX_LEN:
            cmd = cmd[:ACTIVITY_CMD_MAX_LEN - 3] + "..."
        lines.append(f"> {cmd}")
        if entry["done"]:
            dur = f" {entry['duration']:.1f}s" if entry.get("duration") is not None else ""
            lines.append(f"\u2713{dur}")
        else:
            started_at = entry.get("started_at")
            if started_at and (now - started_at) >= LONG_RUNNING_THRESHOLD:
                elapsed = int(now - started_at)
                lines.append(f"\u23f3 still running ({elapsed}s)")
            else:
                lines.append("\u23f3 running...")
        lines.append("")
    return "\n".join(lines).rstrip()


async def _stream_activity(
    chat_id: str, task_id: str, channel: str, ts: str,
    commands: list[dict], client: AsyncWebClient,
    r: aioredis.Redis | None = None,
) -> None:
    """Subscribe to audit channel and stream activity to editable Slack message."""
    audit_channel = f"hcli:audit:{task_id}"
    state_key = f"hcli:task:{task_id}:state"
    last_event_time = time.monotonic()
    last_edit_time = 0.0
    last_state_check = 0.0
    pending_edit = False
    STATE_CHECK_INTERVAL = 5

    pubsub_redis = aioredis.from_url(REDIS_URL, decode_responses=True)
    pubsub = pubsub_redis.pubsub()

    try:
        await pubsub.subscribe(audit_channel)

        while True:
            raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
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
                        if commands and not commands[-1]["done"]:
                            commands[-1]["done"] = True
                        commands.append({"cmd": cmd, "done": False, "duration": None, "started_at": now})
                    elif status in ("completed", "failed") and cmd:
                        for i in range(len(commands) - 1, -1, -1):
                            if commands[i]["cmd"] == cmd and not commands[i]["done"]:
                                commands[i]["done"] = True
                                commands[i]["duration"] = duration
                                break
                        else:
                            commands.append({"cmd": cmd, "done": True, "duration": duration})

                    if len(commands) > ACTIVITY_MAX_COMMANDS:
                        commands[:] = commands[-ACTIVITY_MAX_COMMANDS:]
                    pending_edit = True

            # Long-running command updates — show elapsed every 10s after 30s
            has_long_running = any(
                not e["done"] and e.get("started_at") and (now - e["started_at"]) >= LONG_RUNNING_THRESHOLD
                for e in commands
            )
            if has_long_running and now - last_edit_time >= LONG_RUNNING_UPDATE_INTERVAL:
                pending_edit = True

            # Check task state during idle
            idle_time = now - last_event_time
            if r and idle_time > STATE_CHECK_INTERVAL and now - last_state_check > STATE_CHECK_INTERVAL:
                last_state_check = now
                try:
                    state = await r.get(state_key)
                    if state in ("completed", "failed", "aborted", "timed_out", "cancelled"):
                        text = _format_activity(task_id, commands, done=True)
                        try:
                            await client.chat_update(channel=channel, ts=ts, text=text)
                        except Exception:
                            pass
                        return
                except aioredis.RedisError:
                    pass

            has_active = any(not e["done"] for e in commands)
            if idle_time > ACTIVITY_IDLE_TIMEOUT and not has_active:
                text = _format_activity(task_id, commands, done=True)
                try:
                    await client.chat_update(channel=channel, ts=ts, text=text)
                except Exception:
                    pass
                return

            # Rate-limited edit
            if pending_edit and now - last_edit_time >= ACTIVITY_EDIT_INTERVAL:
                text = _format_activity(task_id, commands, done=False)
                try:
                    await client.chat_update(channel=channel, ts=ts, text=text)
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
            await pubsub.unsubscribe(audit_channel)
            await pubsub.close()
            await pubsub_redis.aclose()
        except Exception:
            pass


# ── Task queue & result polling ──────────────────────────────────────────
async def _queue_task(
    client: AsyncWebClient, channel: str, thread_ts: str,
    user_id: str, message: str,
) -> None:
    """Check concurrency, queue task to Redis, spawn result poller."""
    r = await _get_redis()
    chat_id = _thread_chat_id(channel, thread_ts)

    depth = await r.llen(REDIS_TASKS_KEY)
    if depth >= MAX_CONCURRENT_TASKS:
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"Queue full ({depth}/{MAX_CONCURRENT_TASKS}). Try again later.",
        )
        return

    task_id = str(uuid.uuid4())
    task_payload = json.dumps({
        "task_id": task_id,
        "message": message,
        "user_id": user_id,
        "chat_id": chat_id,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "model": _chat_model.get(chat_id, "opus"),
        "source": "slack",
    })

    await r.rpush(REDIS_TASKS_KEY, task_payload)
    chat_tasks_key = _chat_tasks_key(chat_id)
    await r.rpush(chat_tasks_key, task_id)
    await r.expire(chat_tasks_key, TASK_TIMEOUT * 2)
    audit.info(
        "task_queued",
        extra={"user_id": user_id, "task_id": task_id, "user_message": message},
    )
    logger.info(
        "Task queued: %s (id=%s, model=%s)",
        message[:100], task_id, _chat_model.get(chat_id, "opus"),
    )

    poll_task = asyncio.create_task(
        _poll_result(client, channel, thread_ts, r, task_id, user_id, user_message=message),
    )
    _background_tasks.add(poll_task)
    poll_task.add_done_callback(_background_tasks.discard)


async def _poll_result(
    client: AsyncWebClient, channel: str, thread_ts: str,
    r: aioredis.Redis, task_id: str, user_id: str,
    user_message: str = "",
) -> None:
    """Subscribe to task notify channel, with GET fallback every 10s."""
    chat_id = _thread_chat_id(channel, thread_ts)
    verbose = _verbose_mode.get(chat_id, True)
    activity_msg_ts = None
    activity_task = None
    activity_commands: list[dict] = []

    if verbose:
        resp = await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=_format_activity(task_id, [], done=False),
        )
        activity_msg_ts = resp["ts"]
        activity_task = asyncio.create_task(
            _stream_activity(
                chat_id, task_id, channel, activity_msg_ts,
                activity_commands, client, r=r,
            ),
        )
        _background_tasks.add(activity_task)
        activity_task.add_done_callback(_background_tasks.discard)

    chat_tasks_key = _chat_tasks_key(chat_id)
    result_key = f"{REDIS_RESULT_PREFIX}{task_id}"
    notify_channel = f"hcli:task:{task_id}:notify"

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
            deadline = time.monotonic() + TASK_TIMEOUT
            while raw is None and time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                timeout = min(NOTIFY_POLL_FALLBACK, max(remaining, 0.1))
                await pubsub.get_message(ignore_subscribe_messages=True, timeout=timeout)
                try:
                    raw = await r.get(result_key)
                except aioredis.RedisError as e:
                    logger.warning("Redis error polling task %s: %s", task_id[:8], e)

        # Stop activity stream
        if activity_task and not activity_task.done():
            activity_task.cancel()

        if raw is None:
            # Timeout
            if activity_msg_ts:
                try:
                    await client.chat_update(
                        channel=channel, ts=activity_msg_ts,
                        text=_format_activity(task_id, activity_commands, done=True),
                    )
                except Exception:
                    pass
            await client.chat_postMessage(
                channel=channel, thread_ts=thread_ts,
                text=f"Task {task_id[:8]} timed out after {TASK_TIMEOUT}s.",
            )
            audit.info(
                "task_timeout",
                extra={"user_id": user_id, "task_id": task_id, "timeout": TASK_TIMEOUT},
            )
            return

        # Got result
        if activity_msg_ts:
            try:
                await client.chat_update(
                    channel=channel, ts=activity_msg_ts,
                    text=_format_activity(task_id, activity_commands, done=True),
                )
            except Exception:
                pass

        await r.delete(result_key)
        await r.lrem(chat_tasks_key, 1, task_id)
        try:
            result = json.loads(raw)
            if not _verify_result(task_id, result):
                logger.warning("HMAC verification failed for task %s", task_id)
                audit.warning("hmac_failed", extra={"task_id": task_id})
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
                    stats = "{} \u2191 {:,} \u2193 {:,} | ${:.4f} | {:.1f}s".format(
                        mdl, in_t, out_t, cost, dur_s,
                    )
                    output = output + "\n\n<!-- stats:" + stats + " -->"
        except json.JSONDecodeError:
            output = "(error: malformed result)"

        # Buffer teach turns if teach mode is active
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        if await r.exists(teach_key):
            turns_key = f"{TEACH_PREFIX}{chat_id}:turns"
            if user_message:
                await r.rpush(turns_key, json.dumps(
                    {"role": "user", "content": user_message},
                ))
            await r.rpush(turns_key, json.dumps(
                {"role": "assistant", "content": output},
            ))
            await r.expire(turns_key, TEACH_TTL)

        await send_response(client, channel, thread_ts, output)
        audit.info(
            "task_completed",
            extra={"user_id": user_id, "task_id": task_id},
        )

    except asyncio.CancelledError:
        return
    except aioredis.RedisError as e:
        logger.error("Redis error waiting for task %s: %s", task_id[:8], e)
        await client.chat_postMessage(
            channel=channel, thread_ts=thread_ts,
            text=f"Lost connection to backend while waiting for task {task_id[:8]}.",
        )
        audit.warning(
            "task_redis_error",
            extra={"user_id": user_id, "task_id": task_id},
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


# ── Slash commands ───────────────────────────────────────────────────────
@app.command("/help")
async def cmd_help(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    await client.chat_postMessage(
        channel=channel,
        text=(
            "Send any message and I'll figure out the right tool.\n\n"
            "`/run <command>` — Execute a shell command directly\n"
            "`/new` — Clear context, start a fresh conversation\n"
            "`/cancel` — Cancel the last queued task\n"
            "`/abort` — Kill the currently running task\n"
            "`/status` — Show task queue depth\n"
            "`/stats` — Today's usage stats (tokens, cost, tasks)\n"
            "`/model fast|deep` — Switch model (Haiku/Opus)\n"
            "`/teach` — Start teaching a new skill\n"
            "`/teach end` — End teaching, generate skill draft\n"
            "`/verbose` — Toggle live activity stream\n"
            "`/help` — This message"
        ),
    )


@app.command("/run")
async def cmd_run(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    text = command.get("text", "").strip()
    channel = command["channel_id"]
    if not text:
        await client.chat_postMessage(channel=channel, text="Usage: `/run <command>`")
        return

    # Use channel as thread context for slash commands (no thread_ts available)
    await _queue_task(client, channel, None, user_id, text)


@app.command("/new")
async def cmd_new(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    r = await _get_redis()
    chat_id = channel  # slash commands don't have thread context
    chunk_path = await _dump_session_chunk(r, chat_id)
    await r.delete(f"hcli:session:{chat_id}")
    if chunk_path:
        await client.chat_postMessage(
            channel=channel,
            text=f"Session saved to {os.path.basename(chunk_path)}. Context cleared — next message starts fresh.",
        )
    else:
        await client.chat_postMessage(
            channel=channel, text="Context cleared. Next message starts fresh.",
        )


@app.command("/cancel")
async def cmd_cancel(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    r = await _get_redis()
    chat_id = channel
    chat_tasks_key = _chat_tasks_key(chat_id)

    task_id = await r.rpop(chat_tasks_key)
    if not task_id:
        await client.chat_postMessage(channel=channel, text="No queued tasks to cancel.")
        return

    tasks = await r.lrange(REDIS_TASKS_KEY, 0, -1)
    for raw_task in reversed(tasks):
        try:
            task = json.loads(raw_task)
        except json.JSONDecodeError:
            continue
        if task.get("task_id") == task_id:
            await r.lrem(REDIS_TASKS_KEY, -1, raw_task)
            break

    output = "Task cancelled."
    completed_at = datetime.now(timezone.utc).isoformat()
    msg = f"{task_id}:{output}:{completed_at}"
    sig = hmac_mod.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256,
    ).hexdigest()
    result = json.dumps({
        "output": output, "completed_at": completed_at, "hmac": sig,
    })
    await r.set(f"{REDIS_RESULT_PREFIX}{task_id}", result, ex=TASK_TIMEOUT)

    audit.info("task_cancelled", extra={"user_id": user_id, "task_id": task_id})
    await client.chat_postMessage(channel=channel, text=f"Cancelled task {task_id[:8]}.")


@app.command("/abort")
async def cmd_abort(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    r = await _get_redis()
    chat_id = channel
    chat_tasks_key = _chat_tasks_key(chat_id)

    task_id = await r.lindex(chat_tasks_key, -1)
    if not task_id:
        await client.chat_postMessage(channel=channel, text="No active task to abort.")
        return

    control_channel = f"{REDIS_CONTROL_PREFIX}{task_id}"
    await r.publish(control_channel, json.dumps({"action": "abort"}))

    audit.info("task_aborted", extra={"user_id": user_id, "task_id": task_id})
    await client.chat_postMessage(channel=channel, text=f"Aborted task {task_id[:8]}.")


@app.command("/status")
async def cmd_status(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    r = await _get_redis()
    depth = await r.llen(REDIS_TASKS_KEY)
    await client.chat_postMessage(
        channel=command["channel_id"], text=f"Tasks in queue: {depth}",
    )


@app.command("/stats")
async def cmd_stats(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    r = await _get_redis()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = await r.hgetall(f"{STATS_KEY_PREFIX}{today}")

    channel = command["channel_id"]
    if not stats:
        await client.chat_postMessage(channel=channel, text="No stats for today yet.")
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
        f"*Stats for {today}*",
        f"Tasks: {tasks} ({errors} errors, {error_pct:.0f}%)",
        f"Tokens: {in_tok:,} in / {out_tok:,} out / {cache_r:,} cache",
        f"Avg response: {avg_dur:.1f}s ({avg_turns:.1f} turns)",
        f"Gate: {gate_calls} checks, {gate_in + gate_out:,} tokens",
        f"Cost: ${cost:.4f} main + ${gate_cost:.4f} gate = ${total_cost:.4f}",
    ]
    await client.chat_postMessage(channel=channel, text="\n".join(lines))


@app.command("/model")
async def cmd_model(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    text = command.get("text", "").strip().lower()

    if text == "fast":
        _chat_model[channel] = "haiku"
        await client.chat_postMessage(channel=channel, text="\u26a1 Fast mode (Haiku)")
    elif text == "deep":
        _chat_model[channel] = "opus"
        await client.chat_postMessage(channel=channel, text="\U0001f9e0 Deep mode (Opus)")
    else:
        current = _chat_model.get(channel, "opus")
        await client.chat_postMessage(
            channel=channel,
            text=f"Current model: *{current}*\nUsage: `/model fast` or `/model deep`",
        )


@app.command("/teach")
async def cmd_teach(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    r = await _get_redis()
    chat_id = channel
    text = command.get("text", "").strip().lower()

    if text == "end":
        # End teaching
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        turns_key = f"{TEACH_PREFIX}{chat_id}:turns"

        raw_turns = await r.lrange(turns_key, 0, -1)
        await r.delete(teach_key, turns_key)

        if not raw_turns:
            await client.chat_postMessage(
                channel=channel, text="No teaching data collected.",
            )
            return

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
            "OUTPUT FORMAT — show the full draft in your reply, do NOT write it to disk yet. "
            "Ask the user if they want to save it. If they say yes, write it to /tmp/skills/{topic}.md "
            "using run_command. If no, discard it.\n"
            "---\n"
            "keywords: (trigger words that should activate this skill)\n"
            "---\n"
            "# {Topic}\n\n"
            "## Trigger\n"
            "When does this skill activate? What's in scope, what's not?\n\n"
            "## Constraints\n"
            "Hard rules, non-negotiable.\n\n"
            "## Procedure\n"
            "Ordered steps with actual commands/API calls.\n\n"
            "## Anti-patterns\n"
            "What NOT to do. Exceptions to the rules.\n\n"
            f"Teaching session:\n{session_text}"
        )

        await client.chat_postMessage(
            channel=channel, text="\U0001f4d6 Generating skill draft...",
        )
        audit.info("teach_end", extra={
            "user_id": user_id, "chat_id": chat_id, "turns": len(raw_turns),
        })
        await _queue_task(client, channel, None, user_id, prompt)
    else:
        # Start teaching
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        await r.set(teach_key, "1", ex=TEACH_TTL)
        await client.chat_postMessage(
            channel=channel,
            text=(
                "\U0001f4dd Teaching mode activated.\n"
                "Chat normally — all turns are being buffered.\n"
                "Use `/teach end` when done."
            ),
        )
        audit.info("teach_start", extra={"user_id": user_id, "chat_id": chat_id})


@app.command("/verbose")
async def cmd_verbose(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
    current = _verbose_mode.get(channel, True)
    _verbose_mode[channel] = not current
    state = "ON" if not current else "OFF"
    await client.chat_postMessage(channel=channel, text=f"Verbose mode: {state}")


@app.command("/skills")
async def cmd_skills(ack, command, client):
    await ack()
    user_id = command["user_id"]
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    channel = command["channel_id"]
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
                    content = f.read(500)
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
                lines.append(f"  • {name} — {keywords}{tag}")
            else:
                lines.append(f"  • {name}{tag}")
            total += 1

    if not lines:
        await client.chat_postMessage(channel=channel, text="No skills loaded.")
        return
    header = f"\U0001f4da *Skills ({total})*"
    await client.chat_postMessage(channel=channel, text=header + "\n" + "\n".join(lines))


# ── Event handlers ───────────────────────────────────────────────────────
@app.event("app_mention")
async def handle_mention(event, client):
    """Handle @mentions in channels — reply in thread."""
    user_id = event.get("user", "")
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    text = event.get("text", "")
    # Strip the bot mention from the message
    text = re.sub(r"<@[A-Z0-9]+>\s*", "", text).strip()
    if not text:
        return

    channel = event["channel"]
    # Reply in thread: use existing thread_ts or start thread from this message
    thread_ts = event.get("thread_ts") or event.get("ts")
    await _queue_task(client, channel, thread_ts, user_id, text)


@app.event("message")
async def handle_dm(event, client):
    """Handle DM messages — queue to Redis."""
    # Skip bot messages, message_changed, etc.
    if event.get("subtype"):
        return

    user_id = event.get("user", "")
    if not authorized(user_id):
        logger.warning("Unauthorized access attempt", extra={"user_id": user_id})
        return

    text = event.get("text", "").strip()
    if not text:
        return

    channel = event["channel"]
    thread_ts = event.get("thread_ts") or event.get("ts")
    await _queue_task(client, channel, thread_ts, user_id, text)


# ── Main ─────────────────────────────────────────────────────────────────
async def main():
    r = await _get_redis()
    logger.info(
        "Bot starting — allowed users: %s, max tasks: %d, timeout: %ds",
        ALLOWED_USERS or "(none)",
        MAX_CONCURRENT_TASKS,
        TASK_TIMEOUT,
    )

    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(main())
