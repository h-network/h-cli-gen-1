"""h-cli Web Interface — FastAPI + WebSocket chat with Redis task queue."""

import asyncio
import base64
import hashlib
import hmac as hmac_mod
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

import httpx
import markdown as md
import redis.asyncio as aioredis
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from hcli_logging import get_logger, get_audit_logger

logger = get_logger(__name__, service="web")
audit = get_audit_logger("web")

# ── Config ───────────────────────────────────────────────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3"))
TASK_TIMEOUT = int(os.environ.get("TASK_TIMEOUT", "300"))
WEB_PORT = int(os.environ.get("WEB_PORT", "8080"))
WEB_SSL = os.environ.get("WEB_SSL", "true").lower() != "false"
SSL_CERTFILE = "/app/ssl/cert.pem"
SSL_KEYFILE = "/app/ssl/key.pem"

WEB_USERNAME = os.environ.get("WEB_USERNAME", "")
WEB_PASSWORD = os.environ.get("WEB_PASSWORD", "")

# Multi-user support: WEB_USERS takes priority over WEB_USERNAME/WEB_PASSWORD
WEB_USERS_RAW = os.environ.get("WEB_USERS", "")
_users: dict[str, str] = {}  # username -> password

if WEB_USERS_RAW:
    for _pair_u in WEB_USERS_RAW.split(","):
        _pair_u = _pair_u.strip()
        if ":" in _pair_u:
            _u, _p = _pair_u.split(":", 1)
            _users[_u.strip()] = _p.strip()

# Fallback to single-user config
if not _users and WEB_USERNAME and WEB_PASSWORD:
    _users[WEB_USERNAME] = WEB_PASSWORD

if not _users:
    logger.warning(
        "No users configured (WEB_USERS and WEB_USERNAME/WEB_PASSWORD are empty) — "
        "the web UI will reject all requests."
    )

RESULT_HMAC_KEY = os.environ.get("RESULT_HMAC_KEY", "")
if not RESULT_HMAC_KEY:
    raise RuntimeError("RESULT_HMAC_KEY not set — run install.sh to generate one")

# Redis keys — same contracts as telegram-bot and slack-bot
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

# Per-session state (ephemeral)
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

# Markdown renderer
_md = md.Markdown(extensions=["fenced_code", "tables", "nl2br"])

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


# ── FastAPI app ──────────────────────────────────────────────────────────
app = FastAPI(title="h-cli Web", docs_url=None, redoc_url=None)
security = HTTPBasic()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))


# ── Auth ─────────────────────────────────────────────────────────────────
def verify_credentials(credentials: HTTPBasicCredentials = Depends(security)):
    if not _users:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Auth not configured")
    expected_password = _users.get(credentials.username)
    if expected_password is None:
        # Timing-safe comparison against dummy to prevent username enumeration
        secrets.compare_digest(credentials.password.encode(), b"__invalid__")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not secrets.compare_digest(credentials.password.encode(), expected_password.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ── Helpers ──────────────────────────────────────────────────────────────
def _chat_dir_name(chat_id: str) -> str:
    return _CHAT_NAMES.get(str(chat_id), str(chat_id))


def _chat_tasks_key(chat_id: str) -> str:
    return f"hcli:chat:{chat_id}:tasks"


def _verify_result(task_id: str, result: dict) -> bool:
    expected = result.get("hmac", "")
    msg = f"{task_id}:{result.get('output', '')}:{result.get('completed_at', '')}"
    computed = hmac_mod.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256,
    ).hexdigest()
    return hmac_mod.compare_digest(expected, computed)


def markdown_to_html(text: str) -> str:
    """Convert markdown to HTML using Python markdown library."""
    _md.reset()
    return _md.convert(text)


async def _handle_graph_action(
    ws: WebSocket, payload: str,
) -> None:
    """Fetch Grafana render PNG and send as base64 over WebSocket."""
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
        return

    if resp.status_code == 200 and resp.headers.get("content-type", "").startswith("image/"):
        b64 = base64.b64encode(resp.content).decode()
        await ws.send_json({
            "type": "image",
            "content": f"data:image/png;base64,{b64}",
        })


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
def _format_activity(task_id: str, commands: list[dict], done: bool) -> dict:
    """Format activity as JSON for WebSocket."""
    now = time.monotonic()
    formatted_cmds = []
    for e in commands[-ACTIVITY_MAX_COMMANDS:]:
        entry = {
            "cmd": e["cmd"][:ACTIVITY_CMD_MAX_LEN],
            "done": e["done"],
            "duration": e.get("duration"),
        }
        if not e["done"] and e.get("started_at"):
            entry["elapsed"] = int(now - e["started_at"])
        formatted_cmds.append(entry)
    return {
        "type": "activity",
        "task_id": task_id[:8],
        "done": done,
        "commands": formatted_cmds,
    }


async def _stream_activity(
    task_id: str, commands: list[dict], ws: WebSocket,
    r: aioredis.Redis | None = None,
) -> None:
    """Subscribe to audit channel and stream activity over WebSocket."""
    audit_channel = f"hcli:audit:{task_id}"
    state_key = f"hcli:task:{task_id}:state"
    last_event_time = time.monotonic()
    last_edit_time = 0.0
    last_state_check = 0.0
    pending_update = False
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
                    pending_update = True

            # Long-running command updates — show elapsed every 10s after 30s
            has_long_running = any(
                not e["done"] and e.get("started_at") and (now - e["started_at"]) >= LONG_RUNNING_THRESHOLD
                for e in commands
            )
            if has_long_running and now - last_edit_time >= LONG_RUNNING_UPDATE_INTERVAL:
                pending_update = True

            idle_time = now - last_event_time
            if r and idle_time > STATE_CHECK_INTERVAL and now - last_state_check > STATE_CHECK_INTERVAL:
                last_state_check = now
                try:
                    state = await r.get(state_key)
                    if state in ("completed", "failed", "aborted", "timed_out", "cancelled"):
                        try:
                            await ws.send_json(_format_activity(task_id, commands, done=True))
                        except Exception:
                            pass
                        return
                except aioredis.RedisError:
                    pass

            has_active = any(not e["done"] for e in commands)
            if idle_time > ACTIVITY_IDLE_TIMEOUT and not has_active:
                try:
                    await ws.send_json(_format_activity(task_id, commands, done=True))
                except Exception:
                    pass
                return

            if pending_update and now - last_edit_time >= ACTIVITY_EDIT_INTERVAL:
                try:
                    await ws.send_json(_format_activity(task_id, commands, done=False))
                except Exception:
                    pass
                last_edit_time = now
                pending_update = False

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
    ws: WebSocket, chat_id: str, user_id: str, message: str,
) -> None:
    r = await _get_redis()

    depth = await r.llen(REDIS_TASKS_KEY)
    if depth >= MAX_CONCURRENT_TASKS:
        await ws.send_json({
            "type": "error",
            "content": f"Queue full ({depth}/{MAX_CONCURRENT_TASKS}). Try again later.",
        })
        return

    task_id = str(uuid.uuid4())
    task_payload = json.dumps({
        "task_id": task_id,
        "message": message,
        "user_id": user_id,
        "chat_id": chat_id,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "model": _chat_model.get(chat_id, "opus"),
        "source": "web",
    })

    await r.rpush(REDIS_TASKS_KEY, task_payload)
    chat_tasks_key = _chat_tasks_key(chat_id)
    await r.rpush(chat_tasks_key, task_id)
    await r.expire(chat_tasks_key, TASK_TIMEOUT * 2)
    audit.info(
        "task_queued",
        extra={"user_id": user_id, "task_id": task_id, "user_message": message},
    )
    logger.info("Task queued: %s (user=%s, id=%s, model=%s)", message[:100], user_id, task_id, _chat_model.get(chat_id, "opus"))

    await ws.send_json({"type": "task_queued", "task_id": task_id})

    poll_task = asyncio.create_task(
        _poll_result(ws, chat_id, r, task_id, user_id, user_message=message),
    )
    _background_tasks.add(poll_task)
    poll_task.add_done_callback(_background_tasks.discard)


async def _poll_result(
    ws: WebSocket, chat_id: str,
    r: aioredis.Redis, task_id: str, user_id: str,
    user_message: str = "",
) -> None:
    verbose = _verbose_mode.get(chat_id, True)
    activity_task = None
    activity_commands: list[dict] = []

    if verbose:
        try:
            await ws.send_json(_format_activity(task_id, [], done=False))
        except Exception:
            pass
        activity_task = asyncio.create_task(
            _stream_activity(task_id, activity_commands, ws, r=r),
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

        if activity_task and not activity_task.done():
            activity_task.cancel()

        if raw is None:
            if verbose:
                try:
                    await ws.send_json(_format_activity(task_id, activity_commands, done=True))
                except Exception:
                    pass
            try:
                await ws.send_json({
                    "type": "error",
                    "content": f"Task {task_id[:8]} timed out after {TASK_TIMEOUT}s.",
                })
            except Exception:
                pass
            audit.info(
                "task_timeout",
                extra={"user_id": user_id, "task_id": task_id, "timeout": TASK_TIMEOUT},
            )
            return

        # Got result
        if verbose:
            try:
                await ws.send_json(_format_activity(task_id, activity_commands, done=True))
            except Exception:
                pass

        await r.delete(result_key)
        await r.lrem(chat_tasks_key, 1, task_id)

        output, stats_data = _process_result(task_id, raw)

        # Buffer teach turns
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

        # Convert to HTML and send
        html_content = markdown_to_html(output)
        try:
            await ws.send_json({
                "type": "result",
                "content": html_content,
                "raw": output,
                "stats": stats_data,
                "task_id": task_id,
            })
        except Exception:
            pass

        audit.info(
            "task_completed",
            extra={"user_id": user_id, "task_id": task_id},
        )

    except asyncio.CancelledError:
        return
    except aioredis.RedisError as e:
        logger.error("Redis error waiting for task %s: %s", task_id[:8], e)
        try:
            await ws.send_json({
                "type": "error",
                "content": f"Lost connection to backend while waiting for task {task_id[:8]}.",
            })
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
        # Strip action markers
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

        # Strip stats HTML comment if present
        if "<!-- stats:" in output:
            output = output.split("<!-- stats:", 1)[0].rstrip()

        return output, stats_data
    except json.JSONDecodeError:
        return "(error: malformed result)", None


# ── Command handlers ─────────────────────────────────────────────────────
async def _handle_command(ws: WebSocket, chat_id: str, user_id: str, text: str) -> bool:
    """Handle /commands. Returns True if it was a command."""
    if not text.startswith("/"):
        return False

    parts = text.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""
    r = await _get_redis()

    if cmd == "/new":
        chunk_path = await _dump_session_chunk(r, chat_id)
        await r.delete(f"hcli:session:{chat_id}")
        msg = "Context cleared. Next message starts fresh."
        if chunk_path:
            msg = f"Session saved to {os.path.basename(chunk_path)}. " + msg
        await ws.send_json({"type": "system", "content": msg})
        return True

    elif cmd == "/status":
        depth = await r.llen(REDIS_TASKS_KEY)
        await ws.send_json({"type": "system", "content": f"Tasks in queue: {depth}"})
        return True

    elif cmd == "/stats":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        stats = await r.hgetall(f"{STATS_KEY_PREFIX}{today}")
        if not stats:
            await ws.send_json({"type": "system", "content": "No stats for today yet."})
            return True
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
            f"<b>Stats for {today}</b>",
            f"Tasks: {tasks} ({errors} errors, {error_pct:.0f}%)",
            f"Tokens: {in_tok:,} in / {out_tok:,} out / {cache_r:,} cache",
            f"Avg response: {avg_dur:.1f}s ({avg_turns:.1f} turns)",
            f"Gate: {gate_calls} checks, {gate_in + gate_out:,} tokens",
            f"Cost: ${cost:.4f} main + ${gate_cost:.4f} gate = ${total_cost:.4f}",
        ]
        await ws.send_json({"type": "system", "content": "<br>".join(lines)})
        return True

    elif cmd == "/cancel":
        chat_tasks_key = _chat_tasks_key(chat_id)
        task_id = await r.rpop(chat_tasks_key)
        if not task_id:
            await ws.send_json({"type": "system", "content": "No queued tasks to cancel."})
            return True
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
        sig = hmac_mod.new(RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
        result = json.dumps({"output": output, "completed_at": completed_at, "hmac": sig})
        await r.set(f"{REDIS_RESULT_PREFIX}{task_id}", result, ex=TASK_TIMEOUT)
        audit.info("task_cancelled", extra={"user_id": user_id, "task_id": task_id})
        await ws.send_json({"type": "system", "content": f"Cancelled task {task_id[:8]}."})
        return True

    elif cmd == "/abort":
        chat_tasks_key = _chat_tasks_key(chat_id)
        task_id = await r.lindex(chat_tasks_key, -1)
        if not task_id:
            await ws.send_json({"type": "system", "content": "No active task to abort."})
            return True
        control_channel = f"{REDIS_CONTROL_PREFIX}{task_id}"
        await r.publish(control_channel, json.dumps({"action": "abort"}))
        audit.info("task_aborted", extra={"user_id": user_id, "task_id": task_id})
        await ws.send_json({"type": "system", "content": f"Aborted task {task_id[:8]}."})
        return True

    elif cmd == "/model":
        arg = args.strip().lower()
        if arg == "fast":
            _chat_model[chat_id] = "haiku"
            await ws.send_json({"type": "system", "content": "\u26a1 Fast mode (Haiku)"})
        elif arg == "deep":
            _chat_model[chat_id] = "opus"
            await ws.send_json({"type": "system", "content": "\U0001f9e0 Deep mode (Opus)"})
        else:
            current = _chat_model.get(chat_id, "opus")
            await ws.send_json({"type": "system", "content": f"Current model: {current}. Use /model fast or /model deep"})
        return True

    elif cmd == "/verbose":
        current = _verbose_mode.get(chat_id, True)
        _verbose_mode[chat_id] = not current
        state = "ON" if not current else "OFF"
        await ws.send_json({"type": "system", "content": f"Verbose mode: {state}"})
        return True

    elif cmd == "/teach":
        arg = args.strip().lower()
        if arg == "end":
            teach_key = f"{TEACH_PREFIX}{chat_id}"
            turns_key = f"{TEACH_PREFIX}{chat_id}:turns"
            raw_turns = await r.lrange(turns_key, 0, -1)
            await r.delete(teach_key, turns_key)
            if not raw_turns:
                await ws.send_json({"type": "system", "content": "No teaching data collected."})
                return True
            session_lines = []
            for raw in raw_turns:
                turn = json.loads(raw)
                role = turn["role"].upper()
                content = turn["content"]
                session_lines.append(f"{role}: {content}")
            session_text = "\n\n".join(session_lines)
            MAX_TEACH_BYTES = 100_000
            if len(session_text) > MAX_TEACH_BYTES:
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
            await ws.send_json({"type": "system", "content": "\U0001f4d6 Generating skill draft..."})
            audit.info("teach_end", extra={"user_id": user_id, "chat_id": chat_id, "turns": len(raw_turns)})
            await _queue_task(ws, chat_id, user_id, prompt)
            return True
        else:
            teach_key = f"{TEACH_PREFIX}{chat_id}"
            await r.set(teach_key, "1", ex=TEACH_TTL)
            await ws.send_json({
                "type": "system",
                "content": "\U0001f4dd Teaching mode activated. Chat normally. Use /teach end when done.",
            })
            audit.info("teach_start", extra={"user_id": user_id, "chat_id": chat_id})
            return True

    elif cmd == "/skills":
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
                    lines.append(f"  \u2022 {name} \u2014 {keywords}{tag}")
                else:
                    lines.append(f"  \u2022 {name}{tag}")
                total += 1
        if not lines:
            await ws.send_json({"type": "system", "content": "No skills loaded."})
        else:
            header = f"\U0001f4da <b>Skills ({total})</b>"
            await ws.send_json({"type": "system", "content": header + "<br>" + "<br>".join(lines)})
        return True

    elif cmd == "/run":
        if not args.strip():
            await ws.send_json({"type": "system", "content": "Usage: /run &lt;command&gt;"})
            return True
        await _queue_task(ws, chat_id, user_id, args.strip())
        return True

    elif cmd == "/help":
        help_text = (
            "<b>Commands</b><br>"
            "<code>/run &lt;command&gt;</code> \u2014 Execute a shell command<br>"
            "<code>/new</code> \u2014 Clear context, start fresh<br>"
            "<code>/cancel</code> \u2014 Cancel last queued task<br>"
            "<code>/abort</code> \u2014 Kill running task<br>"
            "<code>/status</code> \u2014 Queue depth<br>"
            "<code>/stats</code> \u2014 Today's usage stats<br>"
            "<code>/model fast|deep</code> \u2014 Switch model<br>"
            "<code>/teach</code> / <code>/teach end</code> \u2014 Skill teaching<br>"
            "<code>/verbose</code> \u2014 Toggle activity stream<br>"
            "<code>/skills</code> \u2014 List loaded skills<br>"
            "<code>/help</code> \u2014 This message"
        )
        await ws.send_json({"type": "system", "content": help_text})
        return True

    # Not a recognized command — treat as message
    return False


# ── Routes ───────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index(request: Request, username: str = Depends(verify_credentials)):
    session_id = request.cookies.get("hcli_session")
    if not session_id:
        session_id = str(uuid.uuid4())
    response = templates.TemplateResponse("index.html", {
        "request": request,
        "session_id": session_id,
        "username": username,
    })
    response.set_cookie(
        "hcli_session", session_id,
        httponly=True, samesite="strict", max_age=86400,
        secure=WEB_SSL,
    )
    response.set_cookie(
        "hcli_user", username,
        httponly=True, samesite="strict", max_age=86400,
        secure=WEB_SSL,
    )
    return response


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Auth check from cookie or header
    session_id = ws.cookies.get("hcli_session")
    if not session_id:
        await ws.close(code=4001, reason="No session")
        return

    # WebSocket doesn't support HTTP Basic natively in browser.
    # Auth is enforced on the page load (GET /). The session cookie
    # proves the user passed Basic Auth to get the page.

    username = ws.cookies.get("hcli_user", "unknown")
    await ws.accept()
    chat_id = f"web:{username}:{session_id}"
    user_id = f"web:{username}"
    logger.info("WebSocket connected: user=%s, session=%s", username, session_id[:8])

    # Restore conversation history on connect
    try:
        r = await _get_redis()
        history_key = f"{SESSION_HISTORY_PREFIX}{chat_id}"
        raw_turns = await r.lrange(history_key, -50, -1)  # last 50 turns
        if raw_turns:
            turns = []
            for raw in raw_turns:
                turn = json.loads(raw)
                role = turn.get("role", "unknown")
                content = turn.get("content", "")
                if role == "assistant":
                    role = "asst"
                    content = markdown_to_html(content)
                turns.append({"role": role, "content": content})
            await ws.send_json({"type": "history", "turns": turns})
    except Exception:
        logger.warning("Failed to restore history for %s", chat_id, exc_info=True)

    try:
        while True:
            data = await ws.receive_json()
            msg_type = data.get("type", "")

            if msg_type == "message":
                text = data.get("content", "").strip()
                if not text:
                    continue

                # Try command first
                handled = await _handle_command(ws, chat_id, user_id, text)
                if not handled:
                    await _queue_task(ws, chat_id, user_id, text)

            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: session=%s", session_id[:8])
    except Exception:
        logger.exception("WebSocket error: session=%s", session_id[:8])


# ── Lifecycle ────────────────────────────────────────────────────────────
@app.on_event("startup")
async def startup():
    await _get_redis()
    logger.info(
        "Web UI starting on port %d — auth: %s (%d user(s))",
        WEB_PORT,
        "configured" if _users else "NOT CONFIGURED (all requests rejected)",
        len(_users),
    )


@app.on_event("shutdown")
async def shutdown():
    global _redis_pool, _redis_client
    if _redis_pool:
        await _redis_pool.aclose()
        _redis_client = None
        _redis_pool = None
        logger.info("Redis connection pool closed")


# ── Main ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    ssl_kwargs = {}
    if WEB_SSL:
        if os.path.exists(SSL_CERTFILE) and os.path.exists(SSL_KEYFILE):
            ssl_kwargs = {"ssl_certfile": SSL_CERTFILE, "ssl_keyfile": SSL_KEYFILE}
            logger.info("SSL enabled — serving HTTPS")
        else:
            logger.warning("WEB_SSL=true but certs not found at %s — falling back to HTTP", SSL_CERTFILE)

    uvicorn.run(
        "bot:app",
        host="0.0.0.0",
        port=WEB_PORT,
        log_level="info",
        access_log=False,
        **ssl_kwargs,
    )
