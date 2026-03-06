"""h-cli task bus — Redis task lifecycle, state machine, HMAC, metrics.

Owns all Redis key constants and task state transitions. Single source of
truth for the Redis namespace. Other modules import key constants from here.

Thread-safety: TaskBus methods that operate on self.r (Redis) are safe for
concurrent calls — redis-py uses an internal connection pool. The control
channel tracking dict (_control_threads) is protected by a lock.
"""

import hashlib
import hmac
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone

import redis

from hcli_logging import get_logger

logger = get_logger(__name__, service="claude")

# ── Redis key constants (single source of truth) ────────────────────────

TASKS_KEY = "hcli:tasks"
RESULT_PREFIX = "hcli:results:"
SESSION_PREFIX = "hcli:session:"
SESSION_SIZE_PREFIX = "hcli:session_size:"
SESSION_HISTORY_PREFIX = "hcli:session_history:"
MEMORY_PREFIX = "hcli:memory:"
LAST_ACTIVITY_PREFIX = "hcli:last_activity:"
STATS_KEY_PREFIX = "hcli:stats:"

# ── TTLs ─────────────────────────────────────────────────────────────────

RESULT_TTL = 3600          # 1h (HLD spec)
STATE_TTL = 3600           # 1h (HLD spec)
STATS_TTL = 172800         # 48h (HLD spec)
SESSION_TTL = int(os.environ.get("SESSION_TTL", "28800"))    # 8h
HISTORY_TTL = int(os.environ.get("HISTORY_TTL", "86400"))    # 24h

# ── HMAC signing ─────────────────────────────────────────────────────────

RESULT_HMAC_KEY = os.environ.get("RESULT_HMAC_KEY", "")


def _state_key(task_id: str) -> str:
    return f"hcli:task:{task_id}:state"


def _notify_channel(task_id: str) -> str:
    return f"hcli:task:{task_id}:notify"


def _control_channel(task_id: str) -> str:
    return f"hcli:control:{task_id}"


def _sign_result(task_id: str, output: str, completed_at: str) -> str:
    """HMAC-SHA256 sign a result to prevent spoofing via Redis."""
    msg = f"{task_id}:{output}:{completed_at}"
    return hmac.new(
        RESULT_HMAC_KEY.encode(), msg.encode(), hashlib.sha256
    ).hexdigest()


# ── TimescaleDB (optional) ──────────────────────────────────────────────

TIMESCALE_URL = os.environ.get("TIMESCALE_URL", "")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3"))
_pg_pool = None
_pg_pool_lock = threading.Lock()


def _get_pg_pool():
    """Get or create the TimescaleDB connection pool. Thread-safe."""
    global _pg_pool
    if _pg_pool is not None:
        return _pg_pool
    if not TIMESCALE_URL:
        return None
    with _pg_pool_lock:
        if _pg_pool is not None:
            return _pg_pool
        try:
            import psycopg2
            import psycopg2.pool
            _pg_pool = psycopg2.pool.ThreadedConnectionPool(
                1, MAX_CONCURRENT_TASKS + 1, TIMESCALE_URL,
            )
            return _pg_pool
        except Exception as e:
            logger.warning("TimescaleDB connection failed, metrics disabled: %s", e)
            return None


# ── TaskBus ──────────────────────────────────────────────────────────────

class TaskBus:
    """Redis task lifecycle manager."""

    def __init__(self, redis_url: str):
        if not RESULT_HMAC_KEY:
            raise RuntimeError("RESULT_HMAC_KEY not set — run install.sh to generate one")
        self.redis_url = redis_url
        self.r: redis.Redis | None = None
        self._sub_r: redis.Redis | None = None
        # Per-task control channel tracking: {task_id: (thread, stop_event)}
        self._control_threads_lock = threading.Lock()
        self._control_threads: dict[str, tuple[threading.Thread, threading.Event]] = {}

    def connect(self):
        """Connect to Redis (main + separate pub/sub connection)."""
        self.r = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self.r.ping()
        self._sub_r = redis.Redis.from_url(self.redis_url, decode_responses=True)
        logger.info("Redis connected at %s", self.redis_url.split("@")[-1])

    def reconnect(self):
        """Reconnect after a Redis error."""
        self.r = redis.Redis.from_url(self.redis_url, decode_responses=True)
        self._sub_r = redis.Redis.from_url(self.redis_url, decode_responses=True)

    # ── Task queue ───────────────────────────────────────────────────

    def blpop_task(self, timeout: int = 30) -> str | None:
        """Block-pop a task from the queue. Returns JSON string or None."""
        result = self.r.blpop(TASKS_KEY, timeout=timeout)
        if result is None:
            return None
        return result[1]

    # ── Task validation ──────────────────────────────────────────────

    def validate_task(self, task_json: str) -> dict | None:
        """Validate task JSON schema per HLD section 9.

        Returns parsed dict or None. Sets state=failed for tasks with a
        valid task_id but missing required fields.
        """
        try:
            task = json.loads(task_json)
        except json.JSONDecodeError:
            logger.error("Malformed task JSON, skipping: %s", task_json[:200])
            return None

        task_id = task.get("task_id")
        if not task_id or not isinstance(task_id, str) or len(task_id) > 100:
            logger.error("Invalid or missing task_id, skipping: %s", task_json[:200])
            return None

        # Required: message (accept legacy "command" fallback)
        if not task.get("message") and not task.get("command"):
            logger.error("Missing 'message' (task_id=%s)", task_id)
            self.set_state(task_id, "failed")
            self.store_result(task_id, "Error: missing 'message' field")
            return None

        # Normalize legacy "command" field
        if "message" not in task and "command" in task:
            task["message"] = task["command"]

        # Warn on missing optional fields
        if task.get("chat_id") is None:
            logger.warning("Task %s missing 'chat_id'", task_id)
        if task.get("user_id") is None:
            logger.warning("Task %s missing 'user_id'", task_id)

        # Validate model enum
        if task.get("model") and task["model"] not in {"opus", "sonnet", "haiku"}:
            task["model"] = "opus"

        return task

    # ── State machine ────────────────────────────────────────────────

    def set_state(self, task_id: str, state: str):
        """SET hcli:task:{id}:state + PUBLISH hcli:task:{id}:notify."""
        self.r.set(_state_key(task_id), state, ex=STATE_TTL)
        self.r.publish(_notify_channel(task_id), state)
        logger.debug("Task %s → %s", task_id, state)

    # ── Result storage ───────────────────────────────────────────────

    def store_result(self, task_id: str, output: str,
                     metrics: dict | None = None, model: str = "opus"):
        """HMAC-sign and store task result."""
        completed_at = datetime.now(timezone.utc).isoformat()
        result_data = {
            "output": output,
            "completed_at": completed_at,
        }
        if metrics and not metrics.get("is_error"):
            result_data["usage"] = {
                "model": metrics.get("model", model),
                "input_tokens": metrics.get("input_tokens", 0),
                "output_tokens": metrics.get("output_tokens", 0),
                "cost_usd": metrics.get("cost_usd", 0),
                "duration_ms": metrics.get("duration_ms", 0),
            }
        result_data["hmac"] = _sign_result(task_id, output, completed_at)
        self.r.set(f"{RESULT_PREFIX}{task_id}", json.dumps(result_data), ex=RESULT_TTL)

    # ── Control channel (abort) ──────────────────────────────────────

    def subscribe_control(self, task_id: str, abort_event: threading.Event,
                          proc_ref: list):
        """Subscribe to hcli:control:{task_id} in a daemon thread.

        On abort message: sets abort_event, kills process via proc_ref[0].
        Call unsubscribe_control(task_id) when the task is done.
        Thread-safe: each task gets its own listener tracked by task_id.
        """
        channel = _control_channel(task_id)
        stop = threading.Event()

        def _listener():
            pubsub = self._sub_r.pubsub()
            try:
                pubsub.subscribe(channel)
                while not stop.is_set():
                    msg = pubsub.get_message(timeout=1.0)
                    if msg is None:
                        continue
                    if msg["type"] != "message":
                        continue
                    try:
                        data = json.loads(msg["data"])
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if data.get("action") == "abort":
                        logger.info("Abort received for task %s", task_id)
                        abort_event.set()
                        proc = proc_ref[0]
                        if proc and proc.poll() is None:
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        break
            finally:
                try:
                    pubsub.unsubscribe(channel)
                    pubsub.close()
                except Exception:
                    pass

        t = threading.Thread(target=_listener, daemon=True)
        t.start()
        with self._control_threads_lock:
            self._control_threads[task_id] = (t, stop)

    def unsubscribe_control(self, task_id: str):
        """Stop control channel listener for a specific task. Thread-safe."""
        with self._control_threads_lock:
            entry = self._control_threads.pop(task_id, None)
        if entry is None:
            return
        thread, stop_event = entry
        stop_event.set()
        thread.join(timeout=3)

    # ── Metrics ──────────────────────────────────────────────────────

    def write_metrics(self, task_id: str, chat_id, model: str,
                      input_tokens: int, output_tokens: int,
                      cache_read: int, cache_create: int,
                      cost_usd: float, duration_ms: int,
                      num_turns: int, is_error: bool):
        """Write task metrics to Redis counters and TimescaleDB."""
        now = datetime.now(timezone.utc)

        # Redis counters (always, for /stats)
        date_key = f"{STATS_KEY_PREFIX}{now.strftime('%Y-%m-%d')}"
        pipe = self.r.pipeline()
        pipe.hincrby(date_key, "tasks", 1)
        pipe.hincrby(date_key, "input_tokens", input_tokens)
        pipe.hincrby(date_key, "output_tokens", output_tokens)
        pipe.hincrby(date_key, "cache_read", cache_read)
        pipe.hincrby(date_key, "cache_create", cache_create)
        pipe.hincrbyfloat(date_key, "cost_usd", cost_usd)
        pipe.hincrby(date_key, "duration_ms", duration_ms)
        pipe.hincrby(date_key, "num_turns", num_turns)
        if is_error:
            pipe.hincrby(date_key, "errors", 1)
        pipe.expire(date_key, STATS_TTL)
        pipe.execute()

        # TimescaleDB (optional)
        pool = _get_pg_pool()
        if pool is None:
            return
        conn = None
        try:
            conn = pool.getconn()
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO task_metrics
                       (time, task_id, chat_id, model, input_tokens, output_tokens,
                        cache_read, cache_create, cost_usd, duration_ms, num_turns, is_error)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (now, task_id, str(chat_id) if chat_id else None, model,
                     input_tokens, output_tokens, cache_read, cache_create,
                     cost_usd, duration_ms, num_turns, is_error),
                )
            conn.commit()
        except Exception as e:
            logger.warning("TimescaleDB write failed (non-fatal): %s", e)
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
        finally:
            if conn:
                try:
                    pool.putconn(conn)
                except Exception:
                    pass

    # ── Memory storage ───────────────────────────────────────────────

    def store_memory(self, task_id: str, chat_id, role: str, content: str):
        """Store a conversation turn as raw JSON for future processing."""
        if not chat_id:
            return
        key = f"{MEMORY_PREFIX}{task_id}:{role}"
        doc = json.dumps({
            "chat_id": str(chat_id),
            "role": role,
            "content": content,
            "timestamp": time.time(),
            "task_id": task_id,
        })
        self.r.set(key, doc, ex=SESSION_TTL)

    # ── Crash recovery ───────────────────────────────────────────────

    def recover_orphaned_tasks(self):
        """On startup, scan for tasks stuck in 'running' and mark failed."""
        cursor = 0
        recovered = 0
        while True:
            cursor, keys = self.r.scan(
                cursor, match="hcli:task:*:state", count=100,
            )
            for key in keys:
                state = self.r.get(key)
                if state == "running":
                    task_id = key[len("hcli:task:"):-len(":state")]
                    self.set_state(task_id, "failed")
                    self.store_result(
                        task_id,
                        "Error: task was running when dispatcher restarted",
                    )
                    recovered += 1
                    logger.warning(
                        "Recovered orphaned task %s: running → failed", task_id,
                    )
            if cursor == 0:
                break
        if recovered:
            logger.info("Recovered %d orphaned tasks on startup", recovered)
