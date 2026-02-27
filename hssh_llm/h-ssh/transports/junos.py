"""Hybrid Junos transport — paramiko SSH for show, PyEZ NETCONF for config."""
import paramiko
from jnpr.junos import Device as JunosDevice
from jnpr.junos.utils.config import Config
from jnpr.junos.exception import (
    ConnectError, LockError, UnlockError,
    ConfigLoadError, CommitError, RpcTimeoutError,
)
from .base import BaseTransport, EditResult


class JunosTransport(BaseTransport):
    """Hybrid Junos transport.

    Show path:  paramiko exec_command with optional '| display xml' pipe.
    Config path: PyEZ NETCONF (lock → load → diff → commit/rollback → unlock).
    """

    def __init__(self):
        self._ssh: paramiko.SSHClient | None = None
        self._host: str = ""
        self._user: str = ""
        self._password: str | None = None
        self._timeout: int = 30

    # ── connection ──────────────────────────────────────────────

    def connect(self, host: str, user: str, password: str | None = None, timeout: int = 30) -> None:
        self._host = host
        self._user = user
        self._password = password
        self._timeout = timeout

        # Paramiko SSH — used for show commands
        self._ssh = paramiko.SSHClient()
        self._ssh.load_system_host_keys()
        self._ssh.set_missing_host_key_policy(paramiko.WarningPolicy())
        kwargs = {"hostname": host, "username": user, "timeout": timeout,
                  "allow_agent": True, "look_for_keys": True}
        if password:
            kwargs["password"] = password
        self._ssh.connect(**kwargs)

    # ── show path (paramiko SSH) ────────────────────────────────

    def _exec(self, command: str, timeout: int = 120) -> str:
        if not self._ssh:
            raise RuntimeError("Not connected")
        _, stdout, stderr = self._ssh.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if err and "warning:" not in err.lower():
            return out + "\n" + err if out else err
        return out

    def show(self, command: str, timeout: int = 120) -> str:
        if "| no-more" not in command:
            command = command + " | no-more"
        return self._exec(command, timeout)

    # ── config path (PyEZ NETCONF) ──────────────────────────────

    def _pyez_device(self) -> JunosDevice:
        """Create a PyEZ device connection over SSH port 22."""
        kwargs = {
            "host": self._host,
            "user": self._user,
            "port": 22,
            "gather_facts": False,
            "timeout": self._timeout,
        }
        if self._password:
            kwargs["passwd"] = self._password
        else:
            kwargs["ssh_private_key_file"] = None  # use agent/default keys
        dev = JunosDevice(**kwargs)
        dev.open()
        return dev

    def edit(self, payload: str, dry_run: bool = False, confirmed_minutes: int = 0) -> EditResult:
        dev = None
        try:
            dev = self._pyez_device()
            cu = Config(dev)
            cu.lock()

            try:
                # Determine load format from payload content
                fmt = "set" if payload.strip().startswith("set ") or payload.strip().startswith("delete ") else "text"
                cu.load(payload, format=fmt)

                diff = cu.diff()
                if diff is None:
                    diff = "(no changes)"

                if dry_run:
                    cu.rollback()
                    cu.unlock()
                    return EditResult(ok=True, diff=diff)

                # Commit (with optional confirmed timeout)
                if confirmed_minutes > 0:
                    cu.commit(confirm=confirmed_minutes)
                else:
                    cu.commit()

                cu.unlock()
                return EditResult(ok=True, diff=diff)

            except (ConfigLoadError, CommitError) as e:
                cu.rollback()
                cu.unlock()
                return EditResult(ok=False, diff=diff if 'diff' in dir() else None,
                                  error=str(e))
            except Exception as e:
                try:
                    cu.rollback()
                    cu.unlock()
                except Exception:
                    pass
                return EditResult(ok=False, error=str(e))

        except LockError as e:
            return EditResult(ok=False, error=f"Failed to lock config: {e}")
        except ConnectError as e:
            return EditResult(ok=False, error=f"NETCONF connect failed: {e}")
        except Exception as e:
            return EditResult(ok=False, error=str(e))
        finally:
            if dev:
                try:
                    dev.close()
                except Exception:
                    pass

    # ── teardown ────────────────────────────────────────────────

    def close(self) -> None:
        if self._ssh:
            self._ssh.close()
            self._ssh = None
