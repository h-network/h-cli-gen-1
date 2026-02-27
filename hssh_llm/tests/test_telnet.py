"""Telnet transport tests — mock socket server, no live devices needed."""
import json
import socket
import subprocess
import sys
import os
import threading
import time
import pytest

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")


class MockTelnetServer:
    """Minimal mock telnet server for testing."""

    def __init__(self, vendor="ios"):
        self.vendor = vendor
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        self.server.listen(5)
        self._running = True
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while self._running:
            try:
                self.server.settimeout(1.0)
                conn, _ = self.server.accept()
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
            except socket.timeout:
                continue
            except OSError:
                break

    def _handle(self, conn: socket.socket):
        try:
            conn.settimeout(10.0)
            # Send prompt
            if self.vendor == "ios":
                prompt = "Router#"
            elif self.vendor == "junos":
                prompt = "user@Router>"
            elif self.vendor == "arista":
                prompt = "Switch#"
            else:
                prompt = "Router#"

            conn.sendall(f"\r\n{prompt} ".encode())

            while True:
                try:
                    data = conn.recv(4096).decode("utf-8", errors="replace").strip()
                    if not data:
                        continue

                    if data == "show version":
                        response = f"Mock {self.vendor} device\nVersion: 1.0.0\nUptime: 1 day\r\n{prompt} "
                    elif data == "show ip route":
                        response = f"Gateway of last resort is 10.0.0.1\n10.0.0.0/24 is directly connected\n192.168.1.0/24 via 10.0.0.1\r\n{prompt} "
                    elif data == "show interfaces":
                        response = f"GigabitEthernet0/0 is up, line protocol is up\n  Internet address is 10.0.0.1/24\r\n{prompt} "
                    elif data.startswith("configure") or data == "conf t":
                        response = f"Entering configuration mode\r\n{prompt.replace('#', '(config)#').replace('>', '#')} "
                    elif data == "end":
                        response = f"\r\n{prompt} "
                    elif data == "exit":
                        response = f"\r\n{prompt} "
                    elif data.startswith("set ") or data.startswith("interface ") or data.startswith("ip "):
                        response = f"\r\n{prompt.replace('#', '(config)#').replace('>', '#')} "
                    elif data == "show | compare":
                        response = f"[edit system]\n+  host-name test;\r\n{prompt.replace('>', '#')} "
                    elif data.startswith("rollback"):
                        response = f"Rolled back\r\n{prompt.replace('>', '#')} "
                    elif data == "commit":
                        response = f"commit complete\r\n{prompt.replace('>', '#')} "
                    else:
                        response = f"% Unknown command: {data}\r\n{prompt} "

                    conn.sendall(response.encode())
                except socket.timeout:
                    break
                except (ConnectionError, OSError):
                    break
        finally:
            conn.close()

    def stop(self):
        self._running = False
        self.server.close()


@pytest.fixture(scope="module")
def ios_server():
    server = MockTelnetServer(vendor="ios")
    yield server
    server.stop()


@pytest.fixture(scope="module")
def junos_server():
    server = MockTelnetServer(vendor="junos")
    yield server
    server.stop()


@pytest.fixture(scope="module")
def arista_server():
    server = MockTelnetServer(vendor="arista")
    yield server
    server.stop()


def run_job_stdin(job: list[dict], extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, HSSH, "--user", "admin", "--job", "-", "--json", "--quiet"]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, capture_output=True, text=True, cwd=CWD,
                          input=json.dumps(job))


# ── Unit tests for ANSI stripping ─────────────────────────────


def test_ansi_strip():
    """ANSI escape codes are stripped from output."""
    import importlib.util
    import types

    transports_dir = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "transports")

    # Create the 'transports' package in sys.modules so relative imports work
    transports_pkg = types.ModuleType("transports")
    transports_pkg.__path__ = [transports_dir]
    transports_pkg.__package__ = "transports"
    sys.modules["transports"] = transports_pkg

    # Load and register transports.base
    base_spec = importlib.util.spec_from_file_location(
        "transports.base",
        os.path.join(transports_dir, "base.py"),
        submodule_search_locations=[],
    )
    base_mod = importlib.util.module_from_spec(base_spec)
    sys.modules["transports.base"] = base_mod
    base_spec.loader.exec_module(base_mod)

    # Load transports.telnet
    spec = importlib.util.spec_from_file_location(
        "transports.telnet",
        os.path.join(transports_dir, "telnet.py"),
        submodule_search_locations=[],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "transports"
    spec.loader.exec_module(mod)

    assert mod._strip_ansi("\x1b[32mgreen\x1b[0m") == "green"
    assert mod._strip_ansi("no escapes here") == "no escapes here"
    assert mod._strip_ansi("\x1b[1;31mred bold\x1b[0m text") == "red bold text"


# ── IOS show via mock telnet ──────────────────────────────────


def test_ios_show_version(ios_server):
    """Show command via IOS telnet returns device output."""
    job = [{"target": f"SW1:127.0.0.1:{ios_server.port}:telnet-ios", "show": "show version"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert len(results) == 1
    assert results[0]["ok"]
    assert "Mock ios device" in results[0]["output"]
    assert "Version: 1.0.0" in results[0]["output"]


def test_ios_show_ip_route(ios_server):
    """Show ip route via IOS telnet."""
    job = [{"target": f"SW1:127.0.0.1:{ios_server.port}:telnet-ios", "show": "show ip route"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    assert "10.0.0" in results[0]["output"]


# ── Arista show via mock telnet ───────────────────────────────


def test_arista_show_interfaces(arista_server):
    """Show interfaces via Arista telnet."""
    job = [{"target": f"AR1:127.0.0.1:{arista_server.port}:telnet-arista", "show": "show interfaces"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    assert "GigabitEthernet" in results[0]["output"]


# ── Parallel execution ────────────────────────────────────────


def test_parallel_telnet(ios_server, arista_server):
    """Multiple telnet targets in parallel."""
    job = [
        {"target": f"SW1:127.0.0.1:{ios_server.port}:telnet-ios", "show": "show version"},
        {"target": f"AR1:127.0.0.1:{arista_server.port}:telnet-arista", "show": "show interfaces"},
    ]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert len(results) == 2
    assert all(res["ok"] for res in results)


# ── JSON output schema ────────────────────────────────────────


def test_json_output_schema(ios_server):
    """Telnet output matches the same JSON schema as SSH."""
    job = [{"target": f"SW1:127.0.0.1:{ios_server.port}:telnet-ios", "show": "show version"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    res = results[0]
    assert "device" in res
    assert "host" in res
    assert "vendor" in res
    assert "ok" in res
    assert "duration_ms" in res
    assert isinstance(res["duration_ms"], int)
    assert "command" in res


# ── Target format with port ───────────────────────────────────


def test_target_4_part_format(ios_server):
    """4-part target format NAME:HOST:PORT:VENDOR works."""
    job = [{"target": f"SW1:127.0.0.1:{ios_server.port}:telnet-ios", "show": "show version"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    assert results[0]["device"] == "SW1"


# ── Mixed SSH+Telnet job ─────────────────────────────────────


def test_mixed_ssh_telnet_job(ios_server):
    """Mixed job with SSH and telnet targets."""
    job = [
        {"target": "R1:192.168.178.120:junos", "show": "show system uptime"},
        {"target": f"SW1:127.0.0.1:{ios_server.port}:telnet-ios", "show": "show version"},
    ]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert len(results) == 2
    # Telnet mock should always succeed
    sw1 = next(res for res in results if res["device"] == "SW1")
    assert sw1["ok"]
    assert "Mock ios device" in sw1["output"]
