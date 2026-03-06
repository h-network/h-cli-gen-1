"""REST API transport using httpx — thread-safe synchronous Client."""
import base64
import json
import httpx
from .base import BaseTransport, EditResult


def _resolve_auth(scheme: str, credential: str) -> dict[str, str]:
    """Map auth scheme + credential to HTTP headers."""
    scheme_lower = scheme.lower()
    if scheme_lower == "bearer":
        return {"Authorization": f"Bearer {credential}"}
    elif scheme_lower == "basic":
        # credential is "user:password"
        b64 = base64.b64encode(credential.encode()).decode()
        return {"Authorization": f"Basic {b64}"}
    elif scheme_lower == "x-auth-token":
        return {"X-Auth-Token": credential}
    elif scheme_lower == "token":
        return {"Authorization": f"Token {credential}"}
    else:
        # Generic: treat scheme as the header name
        return {scheme: credential}


class RestTransport(BaseTransport):
    """Generic REST API transport.

    - host carries the base URL (e.g., https://netbox.example.com)
    - Auth comes from per-job "auth" field, passed via user/password params
      where user=scheme and password=token
    - show() = GET request, follows pagination
    - edit() = PATCH/POST/PUT with dry-run diff support
    """

    def __init__(self):
        self._client: httpx.Client | None = None
        self._base_url: str = ""

    def connect(self, host: str, user: str | None = None, password: str | None = None,
                timeout: int = 30, port: int | None = None) -> None:
        self._base_url = host.rstrip("/")
        headers = {}
        if password:
            headers = _resolve_auth(user or "bearer", password)
        self._client = httpx.Client(
            base_url=self._base_url,
            headers=headers,
            timeout=httpx.Timeout(120.0, connect=float(timeout)),
            follow_redirects=True,
        )

    def show(self, command: str, timeout: int = 120) -> str:
        """GET request to API path. Handles pagination via 'next' field."""
        if not self._client:
            raise RuntimeError("Not connected")
        all_results = []
        url = command
        params = None
        while url:
            resp = self._client.get(url, params=params,
                                    timeout=httpx.Timeout(float(timeout), connect=5.0))
            resp.raise_for_status()
            data = resp.json()
            if isinstance(data, dict) and "results" in data:
                all_results.extend(data["results"])
                next_url = data.get("next")
                if next_url:
                    # next URL is absolute — use it directly
                    url = next_url
                    params = None
                else:
                    url = None
            elif isinstance(data, list):
                all_results.extend(data)
                url = None
            else:
                # Single object response
                return json.dumps(data, indent=2)
        return json.dumps(all_results, indent=2)

    def edit(self, payload: str, dry_run: bool = False,
             confirmed_minutes: int = 0) -> EditResult:
        """Execute a REST write operation.

        Payload is a JSON string: {"method": "PATCH", "path": "/api/...", "body": {...}}
        For dry_run, GET current state and diff against proposed body.
        """
        if not self._client:
            raise RuntimeError("Not connected")
        try:
            spec = json.loads(payload)
        except json.JSONDecodeError as e:
            return EditResult(ok=False, error=f"Invalid JSON payload: {e}")

        method = spec.get("method", "PATCH").upper()
        path = spec.get("path", "")
        body = spec.get("body", {})

        if not path:
            return EditResult(ok=False, error="Missing 'path' in edit payload")

        if dry_run:
            try:
                current_resp = self._client.get(path)
                current_resp.raise_for_status()
                current = current_resp.json()
                # Compute diff: show fields that would change
                diff_lines = []
                for key, new_val in body.items():
                    old_val = current.get(key, "<absent>")
                    if old_val != new_val:
                        diff_lines.append(f"- {key}: {json.dumps(old_val)}")
                        diff_lines.append(f"+ {key}: {json.dumps(new_val)}")
                diff = "\n".join(diff_lines) if diff_lines else "(no changes)"
                return EditResult(ok=True, diff=diff)
            except httpx.HTTPStatusError as e:
                return EditResult(ok=False, error=f"GET for dry-run failed: {e.response.status_code}")
            except Exception as e:
                return EditResult(ok=False, error=f"Dry-run failed: {e}")

        try:
            resp = self._client.request(method, path, json=body)
            resp.raise_for_status()
            result_text = json.dumps(resp.json(), indent=2)
            return EditResult(ok=True, diff=result_text)
        except httpx.HTTPStatusError as e:
            error_body = ""
            try:
                error_body = e.response.text[:500]
            except Exception:
                pass
            return EditResult(ok=False, error=f"HTTP {e.response.status_code}: {error_body}")
        except Exception as e:
            return EditResult(ok=False, error=str(e))

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
