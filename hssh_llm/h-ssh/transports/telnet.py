"""Telnet transport for Eve-NG console ports and legacy devices."""
import re
import socket
import time
from .base import BaseTransport, EditResult

# ANSI escape code stripper
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\].*?\x07|\x1b[()][0-9A-B]|\x0f|\x00')

# Per-vendor prompt patterns (regex)
PROMPT_PATTERNS = {
    "ios":    re.compile(r'[\w\-\.]+[>#]\s*$'),
    "junos":  re.compile(r'[\w\-\.]+@[\w\-\.]+[>%#]\s*$'),
    "arista": re.compile(r'[\w\-\.]+[>#]\s*$'),
    "nxos":   re.compile(r'[\w\-\.]+[>#]\s*$'),
}

# --More-- patterns to handle pagination
MORE_PATTERNS = [
    re.compile(r'---?\(more( \d+%)?\)---?', re.IGNORECASE),
    re.compile(r'--More--'),
    re.compile(r'\[yes,no\]'),
]

# Login prompts
LOGIN_RE = re.compile(r'(Username|Login|login|User Name)\s*:\s*$', re.IGNORECASE)
PASSWORD_RE = re.compile(r'(Password|password)\s*:\s*$', re.IGNORECASE)


def _strip_ansi(text: str) -> str:
    """Remove ANSI escape codes from output."""
    return _ANSI_RE.sub('', text)


class TelnetTransport(BaseTransport):
    """Raw-socket telnet transport for Eve-NG console ports and legacy devices.

    Uses raw sockets instead of telnetlib (deprecated in Python 3.11, removed in 3.13).
    Handles login sequences, --More-- pagination, and per-vendor prompt detection.
    """

    def __init__(self):
        self._sock: socket.socket | None = None
        self._vendor: str = "ios"
        self._prompt_re: re.Pattern = PROMPT_PATTERNS["ios"]
        self._buffer: str = ""

    def connect(self, host: str, user: str | None = None, password: str | None = None,
                timeout: int = 30, port: int | None = None) -> None:
        # Port: explicit param > parsed from host string > default 23
        if port:
            ip = host
        elif ":" in host:
            parts = host.rsplit(":", 1)
            ip = parts[0]
            port = int(parts[1])
        else:
            ip = host
            port = 23

        self._sock = socket.create_connection((ip, port), timeout=timeout)
        self._sock.settimeout(timeout)

        # Handle telnet negotiation bytes (IAC sequences)
        self._negotiate_telnet()

        # Wait for initial prompt or login sequence
        initial = self._read_until_prompt_or_login(timeout=timeout)

        # Handle login sequence if prompted
        if LOGIN_RE.search(initial):
            self._send((user or "") + "\n")
            resp = self._read_until_prompt_or_login(timeout=timeout)
            if PASSWORD_RE.search(resp):
                self._send((password or "") + "\n")
                self._read_until_prompt(timeout=timeout)
        elif PASSWORD_RE.search(initial):
            self._send((password or "") + "\n")
            self._read_until_prompt(timeout=timeout)

    def _negotiate_telnet(self) -> None:
        """Handle basic telnet IAC negotiation — refuse all options.

        Any non-IAC data received during negotiation is preserved in self._buffer.
        """
        try:
            self._sock.setblocking(False)
            time.sleep(0.3)
            try:
                data = self._sock.recv(4096)
            except (BlockingIOError, socket.error):
                data = b""
            self._sock.setblocking(True)

            # Respond to IAC negotiations: WILL->DONT, DO->WONT
            # Preserve non-IAC data in buffer
            i = 0
            responses = bytearray()
            non_iac = bytearray()
            while i < len(data):
                if data[i] == 0xFF and i + 2 < len(data):  # IAC
                    cmd = data[i + 1]
                    opt = data[i + 2]
                    if cmd == 0xFB:    # WILL -> DONT
                        responses.extend([0xFF, 0xFE, opt])
                    elif cmd == 0xFD:  # DO -> WONT
                        responses.extend([0xFF, 0xFC, opt])
                    i += 3
                else:
                    non_iac.append(data[i])
                    i += 1
            if responses:
                self._sock.sendall(bytes(responses))
            if non_iac:
                self._buffer = non_iac.decode("utf-8", errors="replace")
        except Exception:
            pass
        finally:
            if self._sock:
                self._sock.setblocking(True)

    def _send(self, data: str) -> None:
        """Send data over the socket."""
        if self._sock:
            self._sock.sendall(data.encode("utf-8", errors="replace"))

    def _read_until_prompt(self, timeout: int = 30) -> str:
        """Read from socket until a CLI prompt is detected."""
        buf = self._buffer
        self._buffer = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Check buffer before reading more
            clean = _strip_ansi(buf)
            if self._prompt_re.search(clean):
                return clean

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sock.settimeout(min(remaining, 2.0))
            try:
                chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                clean = _strip_ansi(buf)

                # Handle --More-- pagination
                for more_re in MORE_PATTERNS:
                    if more_re.search(clean):
                        self._send(" ")
                        buf = clean[:more_re.search(clean).start()]
                        break

                # Check for CLI prompt
                if self._prompt_re.search(clean):
                    return clean
            except socket.timeout:
                # Check if we already have a prompt in buffer
                clean = _strip_ansi(buf)
                if self._prompt_re.search(clean):
                    return clean
                continue
            except (OSError, ConnectionError):
                break
        return _strip_ansi(buf)

    def _read_until_prompt_or_login(self, timeout: int = 30) -> str:
        """Read until we see a CLI prompt OR a login prompt."""
        buf = self._buffer
        self._buffer = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Check buffer before reading more
            clean = _strip_ansi(buf)
            if LOGIN_RE.search(clean) or PASSWORD_RE.search(clean):
                return clean
            if self._prompt_re.search(clean):
                return clean

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            self._sock.settimeout(min(remaining, 2.0))
            try:
                chunk = self._sock.recv(4096).decode("utf-8", errors="replace")
                if not chunk:
                    break
                buf += chunk
                clean = _strip_ansi(buf)

                # Check for login/password prompts
                if LOGIN_RE.search(clean) or PASSWORD_RE.search(clean):
                    return clean
                # Check for CLI prompt (already logged in)
                if self._prompt_re.search(clean):
                    return clean
            except socket.timeout:
                clean = _strip_ansi(buf)
                if LOGIN_RE.search(clean) or PASSWORD_RE.search(clean):
                    return clean
                if self._prompt_re.search(clean):
                    return clean
                continue
            except (OSError, ConnectionError):
                break
        return _strip_ansi(buf)

    def set_vendor(self, vendor: str) -> None:
        """Set vendor for prompt detection."""
        self._vendor = vendor
        self._prompt_re = PROMPT_PATTERNS.get(vendor, PROMPT_PATTERNS["ios"])

    def show(self, command: str, timeout: int = 120) -> str:
        if not self._sock:
            raise RuntimeError("Not connected")
        self._send(command + "\n")
        output = self._read_until_prompt(timeout=timeout)

        # Strip the command echo from the beginning
        lines = output.splitlines()
        if lines and command.strip() in lines[0]:
            lines = lines[1:]
        # Strip the trailing prompt line
        if lines and self._prompt_re.search(lines[-1]):
            lines = lines[:-1]

        return "\n".join(lines)

    def edit(self, payload: str, dry_run: bool = False,
             confirmed_minutes: int = 0) -> EditResult:
        """Execute configuration commands via telnet CLI.

        For IOS-like: configure terminal → commands → end
        For Junos: configure → commands → show|compare → commit/rollback → exit
        """
        if not self._sock:
            raise RuntimeError("Not connected")

        try:
            if self._vendor == "junos":
                return self._edit_junos(payload, dry_run, confirmed_minutes)
            else:
                return self._edit_ios(payload, dry_run)
        except Exception as e:
            return EditResult(ok=False, error=str(e))

    def _edit_ios(self, payload: str, dry_run: bool) -> EditResult:
        """IOS/Arista/NXOS config mode: configure terminal → commands → end."""
        self._send("configure terminal\n")
        self._read_until_prompt(timeout=10)

        errors = []
        for line in payload.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("!") or line.startswith("#"):
                continue
            self._send(line + "\n")
            output = self._read_until_prompt(timeout=10)
            if "invalid" in output.lower() or "error" in output.lower() or "%" in output:
                errors.append(f"'{line}': {output.strip()}")

        self._send("end\n")
        self._read_until_prompt(timeout=10)

        if dry_run:
            # IOS doesn't have native dry-run — show running diff
            self._send("show archive config differences system:running-config\n")
            diff = self._read_until_prompt(timeout=30)
            return EditResult(ok=len(errors) == 0, diff=diff if diff.strip() else "(applied, no diff available)",
                              error="; ".join(errors) if errors else None)

        if errors:
            return EditResult(ok=False, error="; ".join(errors))
        return EditResult(ok=True, diff="(config applied)")

    def _edit_junos(self, payload: str, dry_run: bool, confirmed_minutes: int = 0) -> EditResult:
        """Junos config mode: configure → commands → show|compare → commit/rollback."""
        self._send("configure\n")
        self._read_until_prompt(timeout=10)

        errors = []
        for line in payload.strip().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            self._send(line + "\n")
            output = self._read_until_prompt(timeout=10)
            if "error" in output.lower() or "syntax error" in output.lower():
                errors.append(f"'{line}': {output.strip()}")

        # Get diff
        self._send("show | compare\n")
        diff = self._read_until_prompt(timeout=30)
        # Strip command echo and prompt
        diff_lines = diff.splitlines()
        if diff_lines and "show | compare" in diff_lines[0]:
            diff_lines = diff_lines[1:]
        if diff_lines and self._prompt_re.search(diff_lines[-1]):
            diff_lines = diff_lines[:-1]
        diff = "\n".join(diff_lines).strip()

        if dry_run or errors:
            self._send("rollback 0\n")
            self._read_until_prompt(timeout=10)
            self._send("exit\n")
            self._read_until_prompt(timeout=10)
            if errors:
                return EditResult(ok=False, diff=diff, error="; ".join(errors))
            return EditResult(ok=True, diff=diff if diff else "(no changes)")

        if confirmed_minutes > 0:
            self._send(f"commit confirmed {confirmed_minutes}\n")
        else:
            self._send("commit\n")
        commit_output = self._read_until_prompt(timeout=60)

        self._send("exit\n")
        self._read_until_prompt(timeout=10)

        if "error" in commit_output.lower() or "failed" in commit_output.lower():
            return EditResult(ok=False, diff=diff, error=f"Commit failed: {commit_output.strip()}")

        return EditResult(ok=True, diff=diff if diff else "(no changes)")

    def close(self) -> None:
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
