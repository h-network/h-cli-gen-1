import paramiko
from .base import BaseTransport, EditResult, load_ssh_config


class GenericSSHTransport(BaseTransport):
    def __init__(self):
        self._client: paramiko.SSHClient | None = None

    def connect(self, host: str, user: str | None = None, password: str | None = None,
                timeout: int = 30, port: int | None = None) -> None:
        self._client = paramiko.SSHClient()
        self._client.load_system_host_keys()
        self._client.set_missing_host_key_policy(paramiko.WarningPolicy())

        # Load SSH config for this host
        ssh_cfg = load_ssh_config(host)

        kwargs = {
            "hostname": ssh_cfg.get("hostname", host),
            "timeout": timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }

        # Port: explicit > SSH config > paramiko default (22)
        if port:
            kwargs["port"] = port
        elif "port" in ssh_cfg:
            kwargs["port"] = int(ssh_cfg["port"])

        # User: explicit > SSH config > paramiko default (getpass.getuser())
        if user:
            kwargs["username"] = user
        elif "user" in ssh_cfg:
            kwargs["username"] = ssh_cfg["user"]

        # Identity file from SSH config
        if not password and "identityfile" in ssh_cfg:
            kwargs["key_filename"] = ssh_cfg["identityfile"]

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
