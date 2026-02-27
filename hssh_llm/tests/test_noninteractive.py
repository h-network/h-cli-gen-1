"""Non-interactive behavior tests (B1, B2 compliance)."""
import subprocess
import sys
import os

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")


def run_piped(args: list[str]) -> subprocess.CompletedProcess:
    """Run h-ssh with stdin piped (non-TTY)."""
    return subprocess.run(
        [sys.executable, HSSH] + args,
        capture_output=True, text=True, cwd=CWD,
        input="",  # Forces non-TTY stdin
    )


def test_b2_edit_no_yes_no_tty():
    """B2: Edit operation without -y in non-TTY must exit 2."""
    r = run_piped(["--target", "R1:192.168.178.120", "-eB", "set system ntp"])
    assert r.returncode == 2
    assert "stdin is not a TTY" in r.stderr


def test_b2_edit_with_dry_run_no_yes_no_tty():
    """B2: dry-run edit without -y in non-TTY proceeds (dry-run is non-destructive)."""
    r = run_piped(["--target", "R1:192.168.178.120", "--user", "hcli",
                    "-eB", "set system ntp", "--dry-run"])
    # Should NOT be blocked by B2 — dry-run is safe
    assert "stdin is not a TTY" not in r.stderr


def test_b2_edit_with_yes_flag():
    """B2: Edit with -y should proceed (will fail on connect if timeout is short)."""
    r = run_piped(["--target", "R1:192.168.178.120", "-eB", "set system ntp",
                    "-y", "--session-timeout", "1", "--dry-run"])
    # Should NOT be exit 2 for TTY reasons
    assert "stdin is not a TTY" not in r.stderr


def test_show_no_tty_works():
    """Show commands should work fine without TTY."""
    r = run_piped(["--target", "R1:192.168.178.120", "--user", "hcli",
                    "-sC", "show system uptime", "--json", "--quiet"])
    assert r.returncode == 0
    assert "R1" in r.stdout
