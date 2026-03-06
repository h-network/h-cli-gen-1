"""REST transport tests — mock-based, no live API needed."""
import json
import subprocess
import sys
import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytest

HSSH = os.path.join(os.path.dirname(__file__), "..", "h-ssh", "h-ssh.py")
CWD = os.path.join(os.path.dirname(__file__), "..", "h-ssh")


class MockAPIHandler(BaseHTTPRequestHandler):
    """Minimal mock REST API for testing."""

    def log_message(self, format, *args):
        pass  # Silence request logs

    def do_GET(self):
        auth = self.headers.get("Authorization", "")
        token = self.headers.get("X-Auth-Token", "")

        # Auth check
        if self.path.startswith("/api/auth-required/") and not auth and not token:
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail": "Authentication required"}')
            return

        if self.path == "/api/devices/":
            data = {
                "count": 2,
                "next": None,
                "results": [
                    {"id": 1, "name": "R1", "status": "active"},
                    {"id": 2, "name": "R2", "status": "active"},
                ]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/api/devices/1/":
            data = {"id": 1, "name": "R1", "status": "active", "site": "dc1"}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/api/paginated/?limit=2":
            data = {
                "count": 4,
                "next": f"http://localhost:{self.server.server_address[1]}/api/paginated/?limit=2&offset=2",
                "results": [{"id": 1}, {"id": 2}]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/api/paginated/?limit=2&offset=2":
            data = {
                "count": 4,
                "next": None,
                "results": [{"id": 3}, {"id": 4}]
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/api/simple-list/":
            data = [{"id": 1}, {"id": 2}, {"id": 3}]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        elif self.path == "/api/auth-required/data/":
            data = {"secret": "value", "auth": auth or token}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data).encode())

        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"detail": "Not found"}')

    def do_PATCH(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_length)) if content_length else {}
        if self.path == "/api/devices/1/":
            result = {"id": 1, "name": "R1", **body}
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b'{"detail": "Not found"}')


@pytest.fixture(scope="module")
def mock_server():
    """Start a mock HTTP server for the test module."""
    server = HTTPServer(("127.0.0.1", 0), MockAPIHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    server.shutdown()


def run_job_stdin(job: list[dict], extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    args = [sys.executable, HSSH, "--user", "hcli", "--job", "-", "--json", "--quiet"]
    if extra_args:
        args.extend(extra_args)
    return subprocess.run(args, capture_output=True, text=True, cwd=CWD,
                          input=json.dumps(job))


# ── Show (GET) tests ──────────────────────────────────────────


def test_rest_show_paginated(mock_server):
    """REST show with paginated results follows 'next' URLs."""
    job = [{"target": f"nb:{mock_server}:rest", "show": "/api/paginated/?limit=2"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert len(results) == 1
    assert results[0]["ok"]
    output = json.loads(results[0]["output"])
    assert len(output) == 4
    assert output == [{"id": 1}, {"id": 2}, {"id": 3}, {"id": 4}]


def test_rest_show_simple_list(mock_server):
    """REST show with a simple JSON array response."""
    job = [{"target": f"nb:{mock_server}:rest", "show": "/api/simple-list/"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    output = json.loads(results[0]["output"])
    assert len(output) == 3


def test_rest_show_single_object(mock_server):
    """REST show returning a single JSON object."""
    job = [{"target": f"nb:{mock_server}:rest", "show": "/api/devices/1/"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    output = json.loads(results[0]["output"])
    assert output["name"] == "R1"


def test_rest_show_404_error(mock_server):
    """REST show on non-existent path returns error."""
    job = [{"target": f"nb:{mock_server}:rest", "show": "/api/nonexistent/"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert not results[0]["ok"]
    assert "404" in results[0]["error"]


# ── Auth tests ────────────────────────────────────────────────


def test_rest_bearer_auth(mock_server):
    """Bearer token auth passed via job 'auth' field."""
    job = [{
        "target": f"nb:{mock_server}:rest",
        "show": "/api/auth-required/data/",
        "auth": {"scheme": "bearer", "token": "my-secret-token"}
    }]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    output = json.loads(results[0]["output"])
    assert output["auth"] == "Bearer my-secret-token"


def test_rest_x_auth_token(mock_server):
    """X-Auth-Token header auth (LibreNMS style)."""
    job = [{
        "target": f"nb:{mock_server}:rest",
        "show": "/api/auth-required/data/",
        "auth": {"scheme": "x-auth-token", "token": "librenms-key"}
    }]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    output = json.loads(results[0]["output"])
    assert output["auth"] == "librenms-key"


def test_rest_no_auth_401(mock_server):
    """REST without auth on protected endpoint returns 401 error."""
    job = [{"target": f"nb:{mock_server}:rest", "show": "/api/auth-required/data/"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert not results[0]["ok"]
    assert "401" in results[0]["error"]


# ── Edit (PATCH/POST/PUT) tests ───────────────────────────────


def test_rest_edit_dry_run(mock_server):
    """Edit dry-run GETs current state and diffs against proposed body."""
    edit_payload = json.dumps({
        "method": "PATCH",
        "path": "/api/devices/1/",
        "body": {"status": "planned", "site": "dc1"}
    })
    job = [{"target": f"nb:{mock_server}:rest", "edit": edit_payload}]
    r = run_job_stdin(job, extra_args=["--dry-run", "-y"])
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    # status changed from active to planned
    assert "active" in results[0]["output"]
    assert "planned" in results[0]["output"]
    # site unchanged — should show "(no changes)" or not appear
    assert "dc1" not in results[0]["output"] or "no changes" not in results[0]["output"]


def test_rest_edit_execute(mock_server):
    """Edit without dry-run sends the actual PATCH request."""
    edit_payload = json.dumps({
        "method": "PATCH",
        "path": "/api/devices/1/",
        "body": {"status": "planned"}
    })
    job = [{"target": f"nb:{mock_server}:rest", "edit": edit_payload}]
    r = run_job_stdin(job, extra_args=["-y"])
    results = json.loads(r.stdout)
    assert results[0]["ok"]
    diff = json.loads(results[0]["diff"])
    assert diff["status"] == "planned"


def test_rest_edit_invalid_payload(mock_server):
    """Edit with invalid JSON payload returns error."""
    job = [{"target": f"nb:{mock_server}:rest", "edit": "not json"}]
    r = run_job_stdin(job, extra_args=["-y"])
    results = json.loads(r.stdout)
    assert not results[0]["ok"]
    assert "Invalid JSON" in results[0]["error"]


# ── Mixed SSH+REST job ────────────────────────────────────────


def test_mixed_ssh_rest_job(mock_server):
    """Mixed job with SSH and REST targets in one run."""
    job = [
        {"target": "R1:198.51.100.1:junos", "show": "show system uptime"},
        {"target": f"nb:{mock_server}:rest", "show": "/api/devices/",
         "auth": {"scheme": "bearer", "token": "test"}},
    ]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert len(results) == 2
    # R1 is SSH — should succeed if router is reachable, or fail with connection error
    # nb is REST — should always succeed against mock
    nb_result = next(res for res in results if res["device"] == "nb")
    assert nb_result["ok"]
    output = json.loads(nb_result["output"])
    assert len(output) == 2  # 2 devices from mock


# ── Command field in output ───────────────────────────────────


def test_rest_output_includes_command(mock_server):
    """JSON output should include the command (API path) per result."""
    job = [{"target": f"nb:{mock_server}:rest", "show": "/api/devices/"}]
    r = run_job_stdin(job)
    results = json.loads(r.stdout)
    assert results[0]["command"] == "/api/devices/"
