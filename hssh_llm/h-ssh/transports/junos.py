"""Hybrid Junos transport — paramiko SSH for show, PyEZ NETCONF for config."""
import paramiko
from jnpr.junos import Device as JunosDevice
from jnpr.junos.utils.config import Config
from jnpr.junos.exception import (
    ConnectError, LockError, UnlockError,
    ConfigLoadError, CommitError, RpcTimeoutError,
)
from .base import BaseTransport, EditResult, load_ssh_config


class JunosTransport(BaseTransport):
    """Hybrid Junos transport.

    Show path:  paramiko exec_command with optional '| display xml' pipe.
    Config path: PyEZ NETCONF (lock → load → diff → commit/rollback → unlock).
    """

    def __init__(self):
        self._ssh: paramiko.SSHClient | None = None
        self._host: str = ""
        self._user: str | None = None
        self._password: str | None = None
        self._port: int | None = None
        self._timeout: int = 30
        self._ssh_cfg: dict = {}

    # ── connection ──────────────────────────────────────────────

    def connect(self, host: str, user: str | None = None, password: str | None = None,
                timeout: int = 30, port: int | None = None) -> None:
        self._host = host
        self._password = password
        self._timeout = timeout

        # Load SSH config for this host
        self._ssh_cfg = load_ssh_config(host)

        # Resolve effective user and port
        self._user = user or self._ssh_cfg.get("user")
        self._port = port or (int(self._ssh_cfg["port"]) if "port" in self._ssh_cfg else None)

        # Paramiko SSH — used for show commands
        self._ssh = paramiko.SSHClient()
        self._ssh.load_system_host_keys()
        self._ssh.set_missing_host_key_policy(paramiko.WarningPolicy())

        kwargs = {
            "hostname": self._ssh_cfg.get("hostname", host),
            "timeout": timeout,
            "allow_agent": True,
            "look_for_keys": True,
        }

        if self._port:
            kwargs["port"] = self._port
        if self._user:
            kwargs["username"] = self._user
        if not password and "identityfile" in self._ssh_cfg:
            kwargs["key_filename"] = self._ssh_cfg["identityfile"]
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
        """Create a PyEZ device connection, respecting SSH config and explicit overrides."""
        kwargs = {
            "host": self._ssh_cfg.get("hostname", self._host),
            "port": self._port or 22,
            "gather_facts": False,
            "timeout": self._timeout,
        }

        if self._user:
            kwargs["user"] = self._user

        if self._password:
            kwargs["passwd"] = self._password
        elif "identityfile" in self._ssh_cfg:
            # Use first identity file from SSH config
            key_files = self._ssh_cfg["identityfile"]
            if isinstance(key_files, list) and key_files:
                kwargs["ssh_private_key_file"] = key_files[0]
            elif isinstance(key_files, str):
                kwargs["ssh_private_key_file"] = key_files
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
                return EditResult(ok=False, diff=diff if 'diff' in locals() else None,
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
