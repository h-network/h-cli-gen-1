"""JSON output schema compliance tests — runs against live routers."""
import json
import subprocess
import sys
import os
import pytest

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")

R1 = "R1:198.51.100.1:junos"
R2 = "R2:198.51.100.2:junos"


def run_json(args: list[str]) -> list[dict]:
    r = subprocess.run(
        [sys.executable, HSSH] + args + ["--json", "--quiet"],
        capture_output=True, text=True, cwd=CWD,
    )
    return json.loads(r.stdout)


@pytest.fixture
def show_results():
    return run_json(["--user", "hcli", "-sC", "show system uptime", "--target", R1, "--target", R2])


def test_returns_list(show_results):
    assert isinstance(show_results, list)
    assert len(show_results) == 2


def test_device_fields(show_results):
    for r in show_results:
        assert "device" in r
        assert "host" in r
        assert "vendor" in r
        assert "ok" in r
        assert "duration_ms" in r
        assert isinstance(r["duration_ms"], int)


def test_success_has_output(show_results):
    for r in show_results:
        if r["ok"]:
            assert "output" in r
            assert isinstance(r["output"], str)
            assert len(r["output"]) > 0


def test_device_names(show_results):
    names = {r["device"] for r in show_results}
    assert names == {"R1", "R2"}


def test_vendor_preserved(show_results):
    for r in show_results:
        assert r["vendor"] == "junos"


def test_parallel_execution(show_results):
    """Both devices should complete in roughly the same time if parallel."""
    times = [r["duration_ms"] for r in show_results]
    # Both should be under 5 seconds and within 2x of each other
    assert all(t < 5000 for t in times)
    assert max(times) < 2 * min(times) + 200  # Allow 200ms jitter
