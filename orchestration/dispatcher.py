"""h-cli dispatcher — concurrent BLPOP loop connecting bus and worker.

Entry point for the orchestration process. Blocks on Redis task queue,
delegates execution to a thread pool, reports state via bus. Runs inside the
claude-code container (built from llm/claude-code/Dockerfile).

Concurrency model: ThreadPoolExecutor with semaphore gating. Tasks from
the same chat_id are serialized to protect session history integrity.
MAX_CONCURRENT_TASKS=1 behaves identically to the original serial dispatcher.
"""

import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import redis

from hcli_logging import get_logger
from bus import TaskBus
import worker

logger = get_logger(__name__, service="claude")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")
MAX_CONCURRENT_TASKS = int(os.environ.get("MAX_CONCURRENT_TASKS", "3"))

_shutdown = False
_executor: ThreadPoolExecutor | None = None


def _handle_sigterm(signum, frame):
    global _shutdown
    logger.info("Received SIGTERM, finishing %d in-flight tasks...",
                _active_count())
    _shutdown = True


# ── Per-chat serialization ────────────────────────────────────────────────
# Tasks from the same chat_id must run sequentially to protect session
# history. We track in-flight chat_ids and hold waiting tasks locally.

_chat_locks_lock = threading.Lock()  # Protects _chat_locks dict
_chat_locks: dict[str, threading.Lock] = {}


def _get_chat_lock(chat_id) -> threading.Lock:
    """Get or create a per-chat lock. Thread-safe."""
    key = str(chat_id) if chat_id else "__no_chat__"
    with _chat_locks_lock:
        if key not in _chat_locks:
            _chat_locks[key] = threading.Lock()
        return _chat_locks[key]


def _cleanup_chat_lock(chat_id):
    """Remove a per-chat lock if no one else is waiting on it."""
    key = str(chat_id) if chat_id else "__no_chat__"
    with _chat_locks_lock:
        lock = _chat_locks.get(key)
        if lock is None:
            return
        # Only remove if the lock is free (no other task waiting)
        if lock.acquire(blocking=False):
            lock.release()
            del _chat_locks[key]


# ── Pool tracking ─────────────────────────────────────────────────────────

_semaphore: threading.Semaphore | None = None
_active_lock = threading.Lock()
_active_tasks: dict[str, str] = {}  # task_id -> chat_id, for observability


def _active_count() -> int:
    with _active_lock:
        return len(_active_tasks)


def _register_task(task_id: str, chat_id):
    with _active_lock:
        _active_tasks[task_id] = str(chat_id) if chat_id else ""


def _unregister_task(task_id: str):
    with _active_lock:
        _active_tasks.pop(task_id, None)


# ── Heartbeat ─────────────────────────────────────────────────────────────

HEARTBEAT_PATH = "/tmp/heartbeat"


def _touch_heartbeat():
    try:
        open(HEARTBEAT_PATH, "w").close()
    except OSError:
        pass


# ── Task handler (runs in worker thread) ──────────────────────────────────

def _handle_task(bus: TaskBus, task: dict) -> None:
    """Execute a single task end-to-end. Runs in a pool thread.

    Acquires per-chat lock before execution to serialize same-chat tasks.
    Always releases the semaphore and unregisters, even on crash.
    """
    task_id = task["task_id"]
    chat_id = task.get("chat_id")
    chat_lock = _get_chat_lock(chat_id)

    try:
        # Per-chat serialization: wait for previous task from same chat
        if not chat_lock.acquire(blocking=False):
            logger.info("Task %s waiting — chat %s has in-flight task",
                        task_id, chat_id)
            chat_lock.acquire()  # Block until previous finishes
            logger.info("Task %s proceeding — chat %s lock acquired",
                        task_id, chat_id)

        _register_task(task_id, chat_id)
        _touch_heartbeat()

        bus.set_state(task_id, "running")
        logger.info("Task %s started (pool: %d/%d)",
                     task_id, _active_count(), MAX_CONCURRENT_TASKS)

        # Set up abort control channel
        abort_event = threading.Event()
        proc_ref = [None]
        bus.subscribe_control(task_id, abort_event, proc_ref)

        # Execute task
        output, metrics = worker.run_task(
            bus.r, task, abort_event, proc_ref,
        )

        # Determine final state
        if metrics.get("aborted"):
            bus.set_state(task_id, "aborted")
        elif metrics.get("timed_out"):
            bus.set_state(task_id, "timed_out")
        elif metrics.get("is_error"):
            bus.set_state(task_id, "failed")
        else:
            bus.set_state(task_id, "completed")

        # Store HMAC-signed result
        bus.store_result(task_id, output, metrics, task.get("model", "opus"))

        # Write metrics
        try:
            bus.write_metrics(
                task_id=task_id,
                chat_id=task.get("chat_id"),
                model=metrics.get("model", task.get("model", "opus")),
                input_tokens=metrics.get("input_tokens", 0),
                output_tokens=metrics.get("output_tokens", 0),
                cache_read=metrics.get("cache_read", 0),
                cache_create=metrics.get("cache_create", 0),
                cost_usd=metrics.get("cost_usd", 0),
                duration_ms=metrics.get("duration_ms", 0),
                num_turns=metrics.get("num_turns", 1),
                is_error=metrics.get("is_error", False),
            )
        except Exception:
            logger.exception("Failed to write metrics for task %s", task_id)

        # Store raw conversation memory
        bus.store_memory(
            task_id, task.get("chat_id"), "user", task.get("message", ""),
        )
        bus.store_memory(task_id, task.get("chat_id"), "asst", output)

        # Clean up control channel
        bus.unsubscribe_control(task_id)

        logger.info("Task %s done (pool: %d/%d)",
                     task_id, _active_count() - 1, MAX_CONCURRENT_TASKS)

    except Exception:
        logger.exception("Worker thread crashed for task %s", task_id)
        try:
            bus.set_state(task_id, "failed")
            bus.store_result(task_id, "Error: worker thread crashed")
            bus.unsubscribe_control(task_id)
        except Exception:
            logger.exception("Failed to clean up after crash for task %s",
                             task_id)
    finally:
        _unregister_task(task_id)
        chat_lock.release()
        _cleanup_chat_lock(chat_id)
        _semaphore.release()
        _touch_heartbeat()


# ── Main loop ─────────────────────────────────────────────────────────────

def main() -> None:
    global _shutdown, _executor, _semaphore

    signal.signal(signal.SIGTERM, _handle_sigterm)

    bus = TaskBus(REDIS_URL)
    bus.connect()
    bus.recover_orphaned_tasks()

    _semaphore = threading.Semaphore(MAX_CONCURRENT_TASKS)
    _executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TASKS)

    logger.info("Dispatcher ready — max_concurrent=%d, waiting on hcli:tasks",
                MAX_CONCURRENT_TASKS)

    while not _shutdown:
        try:
            _touch_heartbeat()

            # Gate: wait for a free slot before popping a task
            if not _semaphore.acquire(timeout=5):
                continue  # Check _shutdown flag, touch heartbeat, retry

            # We have a slot — try to get a task
            try:
                task_json = bus.blpop_task(timeout=30)
            except redis.RedisError:
                _semaphore.release()
                raise  # Handled by outer except

            if task_json is None:
                _semaphore.release()
                worker.sweep_idle_sessions(bus.r)
                continue

            task = bus.validate_task(task_json)
            if task is None:
                _semaphore.release()
                continue

            # Submit to pool — semaphore is released in _handle_task
            _executor.submit(_handle_task, bus, task)

        except redis.RedisError:
            logger.error("Redis error, reconnecting in 5s...")
            time.sleep(5)
            bus.reconnect()
        except KeyboardInterrupt:
            logger.info("Dispatcher shutting down (KeyboardInterrupt)")
            _shutdown = True
        except Exception:
            logger.exception("Unexpected error in dispatch loop")

    # Graceful shutdown: wait for all in-flight tasks
    logger.info("Shutting down — waiting for %d in-flight tasks (timeout=%ds)",
                _active_count(), worker.TASK_TIMEOUT)
    _executor.shutdown(wait=True)
    logger.info("Dispatcher stopped")


if __name__ == "__main__":
    main()
