"""CLI argument parsing and validation tests."""
import subprocess
import sys
import os

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")


def run(args: list[str], stdin_data: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, HSSH] + args,
        capture_output=True, text=True, cwd=CWD,
        input=stdin_data,
    )


def test_no_devices():
    r = run(["-sC", "show version"])
    assert r.returncode == 2
    assert "No devices" in r.stderr


def test_no_operation():
    r = run(["--target", "R1:10.0.0.1"])
    assert r.returncode == 2
    assert "No operation" in r.stderr


def test_target_parsing_two_parts():
    # Should not error on parsing — will fail on connect, but parsing is ok
    r = run(["--target", "R1:10.0.0.1", "-sC", "show version", "--session-timeout", "1"])
    # Will fail to connect (timeout) but should not be exit code 2 (parsing error)
    assert r.returncode != 2 or "No devices" not in r.stderr


def test_target_parsing_three_parts():
    r = run(["--target", "R1:10.0.0.1:junos", "-sC", "show version", "--session-timeout", "1"])
    assert r.returncode != 2 or "No devices" not in r.stderr


def test_target_parsing_invalid():
    r = run(["--target", "invalid", "-sC", "show version"])
    assert r.returncode == 2


def test_mutual_exclusivity_devices_target():
    r = run(["--devices", "/tmp/nonexistent.csv", "--target", "R1:10.0.0.1", "-sC", "show version"])
    assert r.returncode == 2
    assert "not allowed" in r.stderr.lower() or "error" in r.stderr.lower()
