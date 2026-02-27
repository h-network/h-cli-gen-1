import paramiko
from .base import BaseTransport, EditResult


class GenericSSHTransport(BaseTransport):
    def __init__(self):
        self._client: paramiko.SSHClient | None = None

    def connect(self, host: str, user: str, password: str | None = None, timeout: int = 30) -> None:
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.WarningPolicy())
        kwargs = {"hostname": host, "username": user, "timeout": timeout, "allow_agent": True, "look_for_keys": True}
        if password:
            kwargs["password"] = password
        self._client.connect(**kwargs)

    def show(self, command: str, timeout: int = 120) -> str:
        if not self._client:
            raise RuntimeError("Not connected")
        _, stdout, stderr = self._client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out + err if err else out

    def edit(self, payload: str, dry_run: bool = False, confirmed_minutes: int = 0) -> EditResult:
        return EditResult(ok=False, error="Generic SSH transport does not support structured edit operations")

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None
