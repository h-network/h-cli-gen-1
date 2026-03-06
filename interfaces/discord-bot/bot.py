"""h-cli Discord Bot — async command interface with Redis task queue."""

import asyncio
import hashlib
import hmac as hmac_mod
import io
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone

import discord
from discord import app_commands
import httpx
import redis.asyncio as aioredis

from hcli_logging import get_logger, get_audit_logger

logger = get_logger(__name__, service="discord")
audit = get_audit_logger("discord")

# ── Config ───────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "300"))

ALLOWED_USERS: set[int] = set()
_raw = os.environ.get("ALLOWED_USERS", "")
if _raw.strip():
    for uid in _raw.split(","):
        uid = uid.strip()
        if uid:
            try:
                ALLOWED_USERS.add(int(uid))
            except ValueError:
                logger.warning("Invalid user ID in ALLOWED_USERS, skipping: %s", uid)

ALLOWED_ROLES: set[int] = set()
_raw_roles = os.environ.get("ALLOWED_ROLES", "")
if _raw_roles.strip():
    for rid in _raw_roles.split(","):
        rid = rid.strip()
        if rid:
            try:
                ALLOWED_ROLES.add(int(rid))
            except ValueError:
                logger.warning("Invalid role ID in ALLOWED_ROLES, skipping: %s", rid)

if not ALLOWED_USERS and not ALLOWED_ROLES:
    logger.warning(
        "ALLOWED_USERS and ALLOWED_ROLES are both empty — no users are authorized. "
        "The bot will reject all messages."
    )

RESULT_HMAC_KEY = os.environ.get("RESULT_HMAC_KEY", "")
if not RESULT_HMAC_KEY:
    raise RuntimeError("RESULT_HMAC_KEY not set — run install.sh to generate one")

# Discord limits
DISCORD_MSG_MAX_LEN = 2000
DISCORD_EMBED_MAX_LEN = 4096
DISCORD_FILE_THRESHOLD = 4096

# Redis keys — same contracts as telegram-bot, slack-bot, web
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
_chat_model: dict[str, str] = {}
_verbose_mode: dict[str, bool] = {}

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

# Guild registration
GUILD_IDS: list[int] = []
_raw_guilds = os.environ.get("DISCORD_GUILD_IDS", "")
if _raw_guilds.strip():
    for gid in _raw_guilds.split(","):
        gid = gid.strip()
        if gid:
            try:
                GUILD_IDS.append(int(gid))
            except ValueError:
                logger.warning("Invalid guild ID in DISCORD_GUILD_IDS, skipping: %s", gid)

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


# ── Discord client ───────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.dm_messages = True

client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)


# ── Helpers ──────────────────────────────────────────────────────────────
def _chat_dir_name(chat_id: str) -> str:
    return _CHAT_NAMES.get(str(chat_id), str(chat_id))


def _chat_tasks_key(chat_id: str) -> str:
    return f"hcli:chat:{chat_id}:tasks"


def _thread_chat_id(channel_id: int, thread_id: int | None) -> str:
    if thread_id and thread_id != channel_id:
        return f"{channel_id}:{thread_id}"
    return str(channel_id)


def authorized(member_or_user: discord.Member | discord.User) -> bool:
    """Fail-closed: empty allowlists means nobody gets in."""
    if member_or_user.id in ALLOWED_USERS:
        return True
    if ALLOWED_ROLES and isinstance(member_or_user, discord.Member):
        for role in member_or_user.roles:
            if role.id in ALLOWED_ROLES:
                return True
    return False


def _verify_result(task_id: str, result: dict) -> bool:
    expected = result.get("hmac", "")
    msg = f"{task_id}:{result.get('output', '')}:{result.get('completed_at', '')}"
    computed = hmac_mod.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac_mod.compare_digest(expected, computed)


# ── Grafana actions ──────────────────────────────────────────────────────
async def _handle_graph_action(
    channel: discord.abc.Messageable, thread: discord.Thread | None,
    payload: str,
) -> None:
    """Fetch Grafana render PNG and send as file attachment."""
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
        target = thread or channel
        await target.send("Failed to fetch graph.")
        return

    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
        target = thread or channel
        await target.send(file=discord.File(io.BytesIO(resp.content), filename="graph.png"))
    else:
        logger.warning(
            "Graph render failed: HTTP %d, content-type=%s",
            resp.status_code, resp.headers.get("content-type", ""),
        )
        target = thread or channel
        await target.send(f"Failed to render graph (HTTP {resp.status_code}).")


# ── Response rendering ───────────────────────────────────────────────────
async def send_response(
    channel: discord.abc.Messageable, thread: discord.Thread | None,
    text: str, stats_data: dict | None = None,
) -> None:
    """Send response with appropriate method based on length."""
    target = thread or channel

    # Extract action markers
    actions: list[tuple[str, str]] = _ACTION_RE.findall(text)
    text = _ACTION_RE.sub("", text)

    # Strip stats HTML comment if present
    if "<!-- stats:" in text:
        text = text.split("<!-- stats:", 1)[0].rstrip()

    # Build stats footer
    stats_footer = ""
    if stats_data:
        stats_footer = "{} \u2191 {:,} \u2193 {:,} | ${:.4f} | {:.1f}s".format(
            stats_data.get("model", "?"),
            stats_data.get("input_tokens", 0),
            stats_data.get("output_tokens", 0),
            stats_data.get("cost_usd", 0),
            stats_data.get("duration_s", 0),
        )

    text = text.strip()

    if len(text) <= DISCORD_MSG_MAX_LEN:
        # Short — plain message
        msg = text
        if stats_footer:
            msg += f"\n-# {stats_footer}"
        if len(msg) <= DISCORD_MSG_MAX_LEN:
            await target.send(msg)
        else:
            await target.send(text)
            await target.send(f"-# {stats_footer}")
    elif len(text) <= DISCORD_EMBED_MAX_LEN:
        # Medium — embed
        embed = discord.Embed(description=text, color=discord.Color.green())
        if stats_footer:
            embed.set_footer(text=stats_footer)
        await target.send(embed=embed)
    else:
        # Large — file attachment + truncated embed
        f = discord.File(io.BytesIO(text.encode()), filename="response.md")
        embed = discord.Embed(
            description=text[:200] + "...\n\n*Full response attached as file.*",
            color=discord.Color.green(),
        )
        if stats_footer:
            embed.set_footer(text=stats_footer)
        await target.send(embed=embed, file=f)

    # Execute extracted actions
    for action_type, action_payload in actions:
        if action_type == "graph":
            try:
                await _handle_graph_action(channel, thread, action_payload)
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


def _abort_view(task_id: str) -> discord.ui.View:
    """Create a View with an Abort button for the given task."""
    view = discord.ui.View(timeout=TASK_TIMEOUT)
    button = discord.ui.Button(
        label="Abort",
        style=discord.ButtonStyle.danger,
        custom_id=f"abort:{task_id}",
    )

    async def abort_callback(interaction: discord.Interaction):
        if not authorized(interaction.user):
            await interaction.response.send_message(
                "Not authorized.", ephemeral=True,
            )
            return
        r = await _get_redis()
        control_channel = f"{REDIS_CONTROL_PREFIX}{task_id}"
        await r.publish(control_channel, json.dumps({"action": "abort"}))
        audit.info("task_aborted", extra={
            "user_id": str(interaction.user.id), "task_id": task_id,
        })
        await interaction.response.send_message(
            f"Aborted task {task_id[:8]}.", ephemeral=True,
        )
        button.disabled = True
        button.label = "Aborted"
        try:
            await interaction.message.edit(view=view)
        except Exception:
            pass

    button.callback = abort_callback
    view.add_item(button)
    return view


async def _stream_activity(
    chat_id: str, task_id: str,
    activity_msg: discord.Message,
    commands: list[dict],
    r: aioredis.Redis | None = None,
) -> None:
    """Subscribe to audit channel and stream activity to editable Discord message."""
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
                    event_status = event.get("status", "")
                    duration_ms = event.get("duration_ms")
                    duration = duration_ms / 1000 if duration_ms is not None else None

                    if event_status == "running" and cmd:
                        if commands and not commands[-1]["done"]:
                            commands[-1]["done"] = True
                        commands.append({"cmd": cmd, "done": False, "duration": None, "started_at": now})
                    elif event_status in ("completed", "failed") and cmd:
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

            # Long-running command updates
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
                            await activity_msg.edit(content=text, view=None)
                        except Exception:
                            pass
                        return
                except aioredis.RedisError:
                    pass

            has_active = any(not e["done"] for e in commands)
            if idle_time > ACTIVITY_IDLE_TIMEOUT and not has_active:
                text = _format_activity(task_id, commands, done=True)
                try:
                    await activity_msg.edit(content=text, view=None)
                except Exception:
                    pass
                return

            # Rate-limited edit
            if pending_edit and now - last_edit_time >= ACTIVITY_EDIT_INTERVAL:
                text = _format_activity(task_id, commands, done=False)
                try:
                    await activity_msg.edit(content=text)
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
    channel: discord.abc.Messageable, thread: discord.Thread | None,
    user_id: str, message: str,
) -> None:
    """Check concurrency, queue task to Redis, spawn result poller."""
    r = await _get_redis()
    target = thread or channel
    channel_id = channel.id if hasattr(channel, "id") else 0
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)

    depth = await r.llen(REDIS_TASKS_KEY)
    if depth >= MAX_CONCURRENT_TASKS:
        await target.send(f"Queue full ({depth}/{MAX_CONCURRENT_TASKS}). Try again later.")
        return

    task_id = str(uuid.uuid4())
    task_payload = json.dumps({
        "task_id": task_id,
        "message": message,
        "user_id": user_id,
        "chat_id": chat_id,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "model": _chat_model.get(chat_id, "opus"),
        "source": "discord",
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
        _poll_result(channel, thread, r, task_id, user_id, user_message=message),
    )
    _background_tasks.add(poll_task)
    poll_task.add_done_callback(_background_tasks.discard)


async def _poll_result(
    channel: discord.abc.Messageable, thread: discord.Thread | None,
    r: aioredis.Redis, task_id: str, user_id: str,
    user_message: str = "",
) -> None:
    """Subscribe to task notify channel, with GET fallback every 10s."""
    target = thread or channel
    channel_id = channel.id if hasattr(channel, "id") else 0
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)
    verbose = _verbose_mode.get(chat_id, True)
    activity_msg = None
    activity_task = None
    activity_commands: list[dict] = []

    if verbose:
        view = _abort_view(task_id)
        activity_msg = await target.send(
            _format_activity(task_id, [], done=False),
            view=view,
        )
        activity_task = asyncio.create_task(
            _stream_activity(chat_id, task_id, activity_msg, activity_commands, r=r),
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
            if activity_msg:
                try:
                    await activity_msg.edit(
                        content=_format_activity(task_id, activity_commands, done=True),
                        view=None,
                    )
                except Exception:
                    pass
            await target.send(f"Task {task_id[:8]} timed out after {TASK_TIMEOUT}s.")
            audit.info(
                "task_timeout",
                extra={"user_id": user_id, "task_id": task_id, "timeout": TASK_TIMEOUT},
            )
            return

        # Got result
        if activity_msg:
            try:
                await activity_msg.edit(
                    content=_format_activity(task_id, activity_commands, done=True),
                    view=None,
                )
            except Exception:
                pass

        await r.delete(result_key)
        await r.lrem(chat_tasks_key, 1, task_id)

        output, stats_data = _process_result(task_id, raw)

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

        await send_response(channel, thread, output, stats_data)
        audit.info(
            "task_completed",
            extra={"user_id": user_id, "task_id": task_id},
        )

    except asyncio.CancelledError:
        return
    except aioredis.RedisError as e:
        logger.error("Redis error waiting for task %s: %s", task_id[:8], e)
        try:
            await target.send(
                f"Lost connection to backend while waiting for task {task_id[:8]}.",
            )
        except Exception:
            pass
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


def _process_result(task_id: str, raw: str) -> tuple[str, dict | None]:
    """Parse and verify a result from Redis. Returns (output, stats_dict)."""
    try:
        result = json.loads(raw)
        if not _verify_result(task_id, result):
            logger.warning("HMAC verification failed for task %s", task_id)
            audit.warning("hmac_failed", extra={"task_id": task_id})
            return "(error: result integrity check failed)", None

        output = result.get("output", "(no output)")
        output = _ACTION_RE.sub("", output)

        stats_data = None
        usage = result.get("usage")
        if usage:
            stats_data = {
                "model": usage.get("model", "?"),
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cost_usd": usage.get("cost_usd", 0),
                "duration_s": (usage.get("duration_ms", 0) or 0) / 1000,
            }

        if "<!-- stats:" in output:
            output = output.split("<!-- stats:", 1)[0].rstrip()

        return output, stats_data
    except json.JSONDecodeError:
        return "(error: malformed result)", None


# ── Slash commands ───────────────────────────────────────────────────────
@tree.command(name="run", description="Execute a command directly")
@app_commands.describe(command="The command to execute")
async def cmd_run(interaction: discord.Interaction, command: str):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    await interaction.response.send_message(f"> {command}")
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    if thread:
        channel = client.get_channel(thread.parent_id) or channel
    await _queue_task(channel, thread, str(interaction.user.id), command)


@tree.command(name="new", description="Clear context, start fresh conversation")
async def cmd_new(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    r = await _get_redis()
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    channel_id = thread.parent_id if thread else channel.id
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)
    chunk_path = await _dump_session_chunk(r, chat_id)
    await r.delete(f"hcli:session:{chat_id}")
    if chunk_path:
        await interaction.response.send_message(
            f"Session saved to {os.path.basename(chunk_path)}. Context cleared — next message starts fresh.",
        )
    else:
        await interaction.response.send_message("Context cleared. Next message starts fresh.")


@tree.command(name="cancel", description="Cancel the last queued task")
async def cmd_cancel(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    r = await _get_redis()
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    channel_id = thread.parent_id if thread else channel.id
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)
    chat_tasks_key = _chat_tasks_key(chat_id)

    task_id = await r.rpop(chat_tasks_key)
    if not task_id:
        await interaction.response.send_message("No queued tasks to cancel.", ephemeral=True)
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

    audit.info("task_cancelled", extra={"user_id": str(interaction.user.id), "task_id": task_id})
    await interaction.response.send_message(f"Cancelled task {task_id[:8]}.")


@tree.command(name="abort", description="Kill the currently running task")
async def cmd_abort(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    r = await _get_redis()
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    channel_id = thread.parent_id if thread else channel.id
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)
    chat_tasks_key = _chat_tasks_key(chat_id)

    task_id = await r.lindex(chat_tasks_key, -1)
    if not task_id:
        await interaction.response.send_message("No active task to abort.", ephemeral=True)
        return

    control_channel = f"{REDIS_CONTROL_PREFIX}{task_id}"
    await r.publish(control_channel, json.dumps({"action": "abort"}))
    audit.info("task_aborted", extra={"user_id": str(interaction.user.id), "task_id": task_id})
    await interaction.response.send_message(f"Aborted task {task_id[:8]}.", ephemeral=True)


@tree.command(name="status", description="Show task queue depth")
async def cmd_status(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    r = await _get_redis()
    depth = await r.llen(REDIS_TASKS_KEY)
    await interaction.response.send_message(f"Tasks in queue: {depth}", ephemeral=True)


@tree.command(name="stats", description="Today's usage stats")
async def cmd_stats(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    r = await _get_redis()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    stats = await r.hgetall(f"{STATS_KEY_PREFIX}{today}")

    if not stats:
        await interaction.response.send_message("No stats for today yet.", ephemeral=True)
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

    embed = discord.Embed(title=f"Stats for {today}", color=discord.Color.blue())
    embed.add_field(name="Tasks", value=f"{tasks} ({errors} errors, {error_pct:.0f}%)", inline=False)
    embed.add_field(name="Tokens", value=f"{in_tok:,} in / {out_tok:,} out / {cache_r:,} cache", inline=False)
    embed.add_field(name="Avg response", value=f"{avg_dur:.1f}s ({avg_turns:.1f} turns)", inline=False)
    embed.add_field(name="Gate", value=f"{gate_calls} checks, {gate_in + gate_out:,} tokens", inline=False)
    embed.add_field(name="Cost", value=f"${cost:.4f} main + ${gate_cost:.4f} gate = ${total_cost:.4f}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="model", description="Switch model")
@app_commands.describe(mode="Model mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="Fast (Haiku)", value="fast"),
    app_commands.Choice(name="Deep (Opus)", value="deep"),
])
async def cmd_model(interaction: discord.Interaction, mode: str | None = None):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    channel_id = thread.parent_id if thread else channel.id
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)

    if mode == "fast":
        _chat_model[chat_id] = "haiku"
        await interaction.response.send_message("\u26a1 Fast mode (Haiku)", ephemeral=True)
    elif mode == "deep":
        _chat_model[chat_id] = "opus"
        await interaction.response.send_message("\U0001f9e0 Deep mode (Opus)", ephemeral=True)
    else:
        current = _chat_model.get(chat_id, "opus")
        await interaction.response.send_message(
            f"Current model: **{current}**\nUsage: `/model fast` or `/model deep`",
            ephemeral=True,
        )


@tree.command(name="verbose", description="Toggle live activity stream")
async def cmd_verbose(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    channel_id = thread.parent_id if thread else channel.id
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)

    current = _verbose_mode.get(chat_id, True)
    _verbose_mode[chat_id] = not current
    state = "ON" if not current else "OFF"
    await interaction.response.send_message(f"Verbose mode: {state}", ephemeral=True)


@tree.command(name="teach", description="Start or end skill teaching")
@app_commands.describe(action="Use 'end' to finish teaching and generate skill draft")
@app_commands.choices(action=[
    app_commands.Choice(name="Start teaching", value="start"),
    app_commands.Choice(name="End teaching", value="end"),
])
async def cmd_teach(interaction: discord.Interaction, action: str | None = None):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    r = await _get_redis()
    channel = interaction.channel
    thread = channel if isinstance(channel, discord.Thread) else None
    channel_id = thread.parent_id if thread else channel.id
    thread_id = thread.id if thread else None
    chat_id = _thread_chat_id(channel_id, thread_id)
    user_id = str(interaction.user.id)

    if action == "end":
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        turns_key = f"{TEACH_PREFIX}{chat_id}:turns"
        raw_turns = await r.lrange(turns_key, 0, -1)
        await r.delete(teach_key, turns_key)

        if not raw_turns:
            await interaction.response.send_message("No teaching data collected.", ephemeral=True)
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

        await interaction.response.send_message("\U0001f4d6 Generating skill draft...")
        audit.info("teach_end", extra={
            "user_id": user_id, "chat_id": chat_id, "turns": len(raw_turns),
        })
        await _queue_task(channel, thread, user_id, prompt)
    else:
        teach_key = f"{TEACH_PREFIX}{chat_id}"
        await r.set(teach_key, "1", ex=TEACH_TTL)
        await interaction.response.send_message(
            "\U0001f4dd Teaching mode activated.\n"
            "Chat normally — all turns are being buffered.\n"
            "Use `/teach end` when done.",
        )
        audit.info("teach_start", extra={"user_id": user_id, "chat_id": chat_id})


@tree.command(name="skills", description="List loaded skills")
async def cmd_skills(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

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
                lines.append(f"\u2022 {name} \u2014 {keywords}{tag}")
            else:
                lines.append(f"\u2022 {name}{tag}")
            total += 1

    if not lines:
        await interaction.response.send_message("No skills loaded.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"\U0001f4da Skills ({total})",
        description="\n".join(lines),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="help", description="Show available commands")
async def cmd_help(interaction: discord.Interaction):
    if not authorized(interaction.user):
        await interaction.response.send_message("Not authorized.", ephemeral=True)
        return

    embed = discord.Embed(
        title="h-cli Commands",
        description=(
            "Send any message and I'll figure out the right tool.\n\n"
            "`/run <command>` \u2014 Execute a shell command directly\n"
            "`/new` \u2014 Clear context, start a fresh conversation\n"
            "`/cancel` \u2014 Cancel the last queued task\n"
            "`/abort` \u2014 Kill the currently running task\n"
            "`/status` \u2014 Show task queue depth\n"
            "`/stats` \u2014 Today's usage stats\n"
            "`/model fast|deep` \u2014 Switch model (Haiku/Opus)\n"
            "`/teach` / `/teach end` \u2014 Skill teaching\n"
            "`/verbose` \u2014 Toggle live activity stream\n"
            "`/skills` \u2014 List loaded skills\n"
            "`/help` \u2014 This message"
        ),
        color=discord.Color.blue(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ── Event handlers ───────────────────────────────────────────────────────
@client.event
async def on_ready():
    await _get_redis()
    # Sync slash commands to guilds (instant) or globally
    if GUILD_IDS:
        for gid in GUILD_IDS:
            guild = discord.Object(id=gid)
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %d", gid)
    else:
        await tree.sync()
        logger.info("Slash commands synced globally (may take up to 1h to propagate)")

    logger.info(
        "Bot ready as %s — allowed users: %s, allowed roles: %s, max tasks: %d, timeout: %ds",
        client.user,
        ALLOWED_USERS or "(none)",
        ALLOWED_ROLES or "(none)",
        MAX_CONCURRENT_TASKS,
        TASK_TIMEOUT,
    )


@client.event
async def on_message(message: discord.Message):
    # Ignore own messages
    if message.author == client.user:
        return
    # Ignore bot messages
    if message.author.bot:
        return

    # Check if bot is mentioned (channel) or if DM
    is_dm = isinstance(message.channel, discord.DMChannel)
    is_mentioned = client.user in message.mentions if not is_dm else False

    if not is_dm and not is_mentioned:
        return

    if not authorized(message.author):
        logger.warning("Unauthorized access attempt", extra={"user_id": str(message.author.id)})
        return

    text = message.content
    if is_mentioned:
        # Strip the bot mention
        text = re.sub(r"<@!?\d+>\s*", "", text).strip()
    if not text:
        return

    user_id = str(message.author.id)

    if is_dm:
        await _queue_task(message.channel, None, user_id, text)
    else:
        # Channel mention — create or use thread
        thread = None
        if isinstance(message.channel, discord.Thread):
            thread = message.channel
            channel = client.get_channel(thread.parent_id) or message.channel
        else:
            # Create a thread under the mention message
            try:
                thread = await message.create_thread(
                    name=f"h-cli: {text[:50]}",
                    auto_archive_duration=1440,
                )
            except discord.HTTPException:
                # Thread creation failed (maybe already in a thread, or no perms)
                thread = None
            channel = message.channel
        await _queue_task(channel, thread, user_id, text)


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
