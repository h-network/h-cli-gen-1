"""Job file (--job) per-device command tests."""
import json
import subprocess
import sys
import os
import tempfile
import pytest

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")

R1 = "R1:198.51.100.1:junos"
R2 = "R2:198.51.100.2:junos"


def run_job_stdin(job: list[dict], extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, HSSH, "--user", "hcli", "--job", "-", "--json", "--quiet"]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, capture_output=True, text=True, cwd=CWD,
                          input=json.dumps(job))


def run_job_file(job: list[dict], extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
        json.dump(job, f)
        f.flush()
        args = [sys.executable, HSSH, "--user", "hcli", "--job", f.name, "--json", "--quiet"]
        if extra_args:
            args.extend(extra_args)
        r = subprocess.run(args, capture_output=True, text=True, cwd=CWD, input="")
    os.unlink(f.name)
    return r


# ── Per-device show commands (live router tests) ──────────────


def test_different_show_commands_parallel():
    """Core use case: different show commands to different devices in parallel."""
    job = [
        {"target": R1, "show": "show system uptime"},
        {"target": R2, "show": "show version"},
    ]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert len(results) == 2
    assert all(res["ok"] for res in results)

    r1 = next(res for res in results if res["device"] == "R1")
    r2 = next(res for res in results if res["device"] == "R2")

    # R1 ran uptime, R2 ran version — outputs should differ
    assert r1["command"] == "show system uptime"
    assert r2["command"] == "show version"
    assert "boot" in r1["output"].lower() or "uptime" in r1["output"].lower()
    assert "junos" in r2["output"].lower() or "vmx" in r2["output"].lower()


def test_job_from_file():
    """Job loaded from a file on disk."""
    job = [
        {"target": R1, "show": "show system uptime"},
        {"target": R2, "show": "show route summary"},
    ]
    r = run_job_file(job)
    results = json.loads(r.stdout)
    assert len(results) == 2
    assert all(res["ok"] for res in results)
    assert results[1]["command"] == "show route summary"


def test_json_output_includes_command_field():
    """Each result should include the per-device command in JSON output."""
    job = [
        {"target": R1, "show": "show system uptime"},
        {"target": R2, "show": "show interfaces terse"},
    ]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    for res in results:
        assert "command" in res
        assert isinstance(res["command"], str)
        assert len(res["command"]) > 0


def test_parallel_execution_timing():
    """Both devices should execute in parallel (not sequential)."""
    job = [
        {"target": R1, "show": "show system uptime"},
        {"target": R2, "show": "show system uptime"},
    ]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    times = [res["duration_ms"] for res in results]
    assert all(t < 5000 for t in times)
    assert max(times) < 2 * min(times) + 200


# ── Per-device edit commands (dry-run) ─────────────────────────


def test_mixed_edit_dry_run():
    """Edit dry-run via job file on both routers with different payloads."""
    job = [
        {"target": R1, "edit": "set system host-name R1-job-test"},
        {"target": R2, "edit": "set system host-name R2-job-test"},
    ]
    r = run_job_stdin(job, extra_args=["--dry-run", "-y"])
    results = json.loads(r.stdout)
    assert len(results) == 2
    assert all(res["ok"] for res in results)
    assert "R1-job-test" in results[0]["output"]
    assert "R2-job-test" in results[1]["output"]


# ── Validation / error handling ────────────────────────────────


def test_job_combined_with_sC_errors():
    """--job cannot be combined with -sC."""
    r = subprocess.run(
        [sys.executable, HSSH, "--job", "-", "-sC", "test"],
        capture_output=True, text=True, cwd=CWD, input="[]",
    )
    assert r.returncode == 2
    assert "cannot be combined" in r.stderr


def test_empty_job_array_errors():
    """Empty job array should be rejected."""
    r = run_job_stdin([])
    assert r.returncode == 2
    assert "non-empty" in r.stderr


def test_missing_operation_errors():
    """Job entry without show/edit should be rejected."""
    r = run_job_stdin([{"target": "R1:10.0.0.1"}])
    assert r.returncode == 2
    assert "show" in r.stderr and "edit" in r.stderr


def test_invalid_target_format_errors():
    """Job entry with bad target format should be rejected."""
    r = run_job_stdin([{"target": "badformat", "show": "test"}])
    assert r.returncode == 2
