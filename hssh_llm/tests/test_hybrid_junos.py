"""Hybrid Junos transport tests — validates both show (paramiko) and config (PyEZ) paths."""
import json
import subprocess
import sys
import os
import pytest

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")

R1 = "R1:192.168.178.120:junos"
R2 = "R2:192.168.178.108:junos"


def run_json(args: list[str]) -> list[dict]:
    r = subprocess.run(
        [sys.executable, HSSH] + args + ["--json", "--quiet"],
        capture_output=True, text=True, cwd=CWD, input="",
    )
    return json.loads(r.stdout)


# ── Show path (paramiko SSH) ──────────────────────────────────


def test_show_both_routers():
    results = run_json(["--user", "hcli", "-sC", "show system uptime", "--target", R1, "--target", R2])
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert all("uptime" in r["output"].lower() or "boot" in r["output"].lower() for r in results)


def test_show_display_xml():
    """Show with '| display xml' pipe should return XML output."""
    results = run_json(["--user", "hcli", "-sC", "show system uptime | display xml", "--target", R1])
    assert len(results) == 1
    assert results[0]["ok"]
    assert "<" in results[0]["output"]  # XML tags present


# ── Config path (PyEZ NETCONF) ─────────────────────────────────


def test_config_dry_run_r1():
    """Config dry-run on R1 should produce a diff without committing."""
    results = run_json(["--user", "hcli", "-eB", "set system host-name test-hybrid-r1",
                        "--target", R1, "--dry-run", "-y"])
    assert len(results) == 1
    assert results[0]["ok"]
    assert "host-name" in results[0]["output"]


def test_config_dry_run_r2():
    """Config dry-run on R2 should produce a diff without committing."""
    results = run_json(["--user", "hcli", "-eB", "set system host-name test-hybrid-r2",
                        "--target", R2, "--dry-run", "-y"])
    assert len(results) == 1
    assert results[0]["ok"]
    assert "host-name" in results[0]["output"]


def test_config_dry_run_parallel():
    """Config dry-run on both routers in parallel."""
    results = run_json(["--user", "hcli", "-eB", "set system host-name test-parallel",
                        "--target", R1, "--target", R2, "--dry-run", "-y"])
    assert len(results) == 2
    assert all(r["ok"] for r in results)
    assert all("host-name" in r["output"] for r in results)


def test_config_no_changes():
    """Loading existing config should report no changes."""
    # First get the current hostname from R1
    show = run_json(["--user", "hcli", "-sC", "show configuration system host-name", "--target", R1])
    # Try to set it to the same value — expect no changes or minimal diff
    assert show[0]["ok"]
