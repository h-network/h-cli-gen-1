"""h-cli Core — MCP server exposing tools over SSE via FastMCP."""

import json
import os
import re
import subprocess
import threading
import time as _time
from datetime import datetime, timezone

import redis as _redis_lib

from mcp.server.fastmcp import FastMCP

from hcli_logging import get_logger, get_audit_logger

logger = get_logger(__name__, service="core")
audit = get_audit_logger("core")

mcp = FastMCP("h-cli-core", host="0.0.0.0", port=8083)

MAX_OUTPUT_BYTES = 500 * 1024  # 500KB
MAX_CONCURRENT = 5
_semaphore = threading.Semaphore(MAX_CONCURRENT)

# Output sanitization — redact credentials from command output
SANITIZE_OUTPUT = os.environ.get("SANITIZE_OUTPUT", "true").lower() != "false"

_SANITIZE_PATTERNS = [
    # PEM private keys (must be first — multiline, replace entire block)
    (re.compile(
        r"-----BEGIN\s+\w+\s+PRIVATE\s+KEY-----[\s\S]*?-----END\s+\w+\s+PRIVATE\s+KEY-----",
        re.IGNORECASE,
    ), "[REDACTED PRIVATE KEY]"),
    # AWS credentials
    (re.compile(r"(AWS[_A-Z]*KEY[_A-Z]*\s*[:=]\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    # Bearer tokens
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    # Generic key/secret/token/password patterns
    (re.compile(r"(password\s*[:=]\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(api[_-]?key\s*[:=]\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(token\s*[:=]\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(secret\s*[:=]\s*)\S+", re.IGNORECASE), r"\1[REDACTED]"),
]


# Detect bare env-listing commands (env, printenv, export with no args or only flags)
_ENV_CMD_RE = re.compile(
    r"^\s*(env|printenv|export)\s*(-[a-zA-Z0-9]*\s*)*$"
)


def _is_env_listing(command: str) -> bool:
    """Return True if command is a bare env/printenv/export (lists all env vars)."""
    return bool(_ENV_CMD_RE.match(command.strip()))


def _redact_env_output(text: str) -> str:
    """Redact all values in KEY=VALUE lines (for env/printenv/export output).

    Handles both KEY=value (env/printenv) and declare -x KEY="value" (export) formats.
    """
    text = re.sub(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", r"\1=[REDACTED]", text, flags=re.MULTILINE)
    text = re.sub(r"^(declare -x [A-Za-z_][A-Za-z0-9_]*)=(.*)$", r"\1=[REDACTED]", text, flags=re.MULTILINE)
    return text


def _sanitize_output(text: str) -> tuple[str, int]:
    """Redact sensitive patterns from command output. Returns (text, redaction_count)."""
    if not SANITIZE_OUTPUT:
        return text, 0
    count = 0
    for pattern, replacement in _SANITIZE_PATTERNS:
        text, n = pattern.subn(replacement, text)
        count += n
    return text, count

# Redis for audit event publishing (fire-and-forget)
REDIS_URL = os.environ.get("REDIS_URL", "")
_redis_client = None


def _get_redis():
    """Lazy Redis connection. Returns None if unavailable."""
    global _redis_client
    if not REDIS_URL:
        return None
    if _redis_client is None:
        try:
            _redis_client = _redis_lib.Redis.from_url(REDIS_URL, decode_responses=True)
            _redis_client.ping()
        except Exception as e:
            logger.warning("Redis unavailable for audit publish: %s", e)
            _redis_client = None
    return _redis_client


def _publish_audit(task_id: str, event: dict):
    """Publish audit event to Redis channel. Fire-and-forget."""
    global _redis_client
    if not task_id:
        return
    try:
        r = _get_redis()
        if r is None:
            return
        r.publish(f"hcli:audit:{task_id}", json.dumps(event))
    except _redis_lib.RedisError as e:
        logger.warning("Redis publish failed (non-fatal): %s", e)
        _redis_client = None
    except Exception as e:
        logger.warning("Audit publish error (non-fatal): %s", e)


@mcp.tool()
def run_command(command: str, task_id: str = "") -> str:
    """Execute a shell command in the core container.

    Available tools: nmap, dig, traceroute, mtr, tcpdump, whois,
    curl, wget, ssh, ping, iproute2, netcat, Playwright/Chromium.
    Returns combined stdout+stderr and exit code.
    """
    if not _semaphore.acquire(blocking=False):
        logger.warning("Concurrent limit reached, rejecting: %s", command)
        audit.info(
            "command_result",
            extra={"command": command, "exit_code": -1, "error": "busy"},
        )
        return "Error: too many concurrent commands (limit 5), try again shortly"

    logger.info("Executing command: %s", command)
    audit.info("command_exec", extra={"command": command})

    # Publish "running" audit event
    t0 = _time.monotonic()
    _publish_audit(task_id, {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "command": command,
        "status": "running",
    })

    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=240,
        )

        output = ""
        if proc.stdout:
            output += proc.stdout
        if proc.stderr:
            if output:
                output += "\n"
            output += proc.stderr

        if not output:
            output = "(no output)"

        # Env-specific redaction — blanket redact all values for env-listing commands
        if _is_env_listing(command):
            output = _redact_env_output(output)
            logger.info("Env output redacted for command: %s", command)

        # Sanitize before truncation — redact credentials from output
        output, redact_count = _sanitize_output(output)
        if redact_count > 0:
            logger.info("Output sanitized: %d patterns redacted for command: %s", redact_count, command)

        original_length = len(output)
        truncated = original_length > MAX_OUTPUT_BYTES
        if truncated:
            output = output[:MAX_OUTPUT_BYTES]

        duration_ms = int((_time.monotonic() - t0) * 1000)
        status = "completed" if proc.returncode == 0 else "failed"

        logger.info("Command finished (exit=%d): %s", proc.returncode, command)
        audit.info(
            "command_result",
            extra={
                "command": command,
                "exit_code": proc.returncode,
                "output_length": original_length,
                "truncated": truncated,
            },
        )

        # Publish "completed"/"failed" audit event
        _publish_audit(task_id, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "status": status,
            "exit_code": proc.returncode,
            "duration_ms": duration_ms,
            "output_length": original_length,
        })

        result = f"Exit code: {proc.returncode}\n\n{output}"
        if truncated:
            result += "\n\n[OUTPUT TRUNCATED at 500KB]"
        return result

    except subprocess.TimeoutExpired:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        logger.warning("Command timed out: %s", command)
        audit.info(
            "command_result",
            extra={
                "command": command,
                "exit_code": None,
                "timed_out": True,
            },
        )
        _publish_audit(task_id, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "status": "failed",
            "exit_code": -1,
            "duration_ms": duration_ms,
            "output_length": 0,
        })
        return "Error: command timed out after 240 seconds"

    except Exception as exc:
        duration_ms = int((_time.monotonic() - t0) * 1000)
        logger.exception("Command failed: %s", command)
        audit.info(
            "command_result",
            extra={
                "command": command,
                "exit_code": -1,
                "error": type(exc).__name__,
            },
        )
        _publish_audit(task_id, {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "command": command,
            "status": "failed",
            "exit_code": -1,
            "duration_ms": duration_ms,
            "output_length": 0,
        })
        return f"Error: command failed ({type(exc).__name__})"

    finally:
        _semaphore.release()


if __name__ == "__main__":
    if not SANITIZE_OUTPUT:
        logger.warning("Output sanitization is DISABLED (SANITIZE_OUTPUT=false)")
    logger.info("Starting MCP server on 0.0.0.0:8083")
    mcp.run(transport="sse")
