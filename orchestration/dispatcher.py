"""h-cli dispatcher — thin BLPOP loop connecting bus and worker.

Entry point for the orchestration process. Blocks on Redis task queue,
delegates execution to worker, reports state via bus. Runs inside the
claude-code container (built from llm/claude-code/Dockerfile).
"""

import os
import signal
import threading
import time

import redis

from hcli_logging import get_logger
from bus import TaskBus
import worker

logger = get_logger(__name__, service="claude")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379")

_shutdown = False


def _handle_sigterm(signum, frame):
    global _shutdown
    logger.info("Received SIGTERM, finishing current task...")
    _shutdown = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_sigterm)

    bus = TaskBus(REDIS_URL)
    bus.connect()
    bus.recover_orphaned_tasks()

    logger.info("Waiting for tasks on hcli:tasks...")

    heartbeat_path = "/tmp/heartbeat"

    while not _shutdown:
        try:
            # Touch heartbeat so Docker healthcheck can verify liveness
            open(heartbeat_path, "w").close()

            task_json = bus.blpop_task(timeout=30)
            if task_json is None:
                worker.sweep_idle_sessions(bus.r)
                continue

            task = bus.validate_task(task_json)
            if task is None:
                continue

            task_id = task["task_id"]

            bus.set_state(task_id, "running")

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
            bus.unsubscribe_control()

        except redis.RedisError:
            logger.error("Redis error, reconnecting in 5s...")
            time.sleep(5)
            bus.reconnect()
        except KeyboardInterrupt:
            logger.info("Dispatcher shutting down")
            break
        except Exception:
            logger.exception("Unexpected error in dispatch loop")

    logger.info("Dispatcher stopped")


if __name__ == "__main__":
    main()
