import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

import paramiko


@dataclass
class EditResult:
    ok: bool
    diff: str | None = None
    error: str | None = None


def load_ssh_config(host: str) -> dict:
    """Load SSH config for a host. Returns dict with hostname, user, port, identityfile etc."""
    config_path = os.path.expanduser("~/.ssh/config")
    if not os.path.exists(config_path):
        return {}
    try:
        ssh_config = paramiko.SSHConfig()
        with open(config_path) as f:
            ssh_config.parse(f)
        return ssh_config.lookup(host)
    except Exception:
        return {}


class BaseTransport(ABC):
    @abstractmethod
    def connect(self, host: str, user: str | None = None, password: str | None = None,
                timeout: int = 30, port: int | None = None) -> None:
        ...

    @abstractmethod
    def show(self, command: str, timeout: int = 120) -> str:
        ...

    @abstractmethod
    def edit(self, payload: str, dry_run: bool = False, confirmed_minutes: int = 0) -> EditResult:
        ...

    @abstractmethod
    def close(self) -> None:
        ...
