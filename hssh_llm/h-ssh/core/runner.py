import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from transports.base import BaseTransport
from transports.junos import JunosTransport
from transports.generic import GenericSSHTransport
from transports.arista import AristaTransport
from transports.rest import RestTransport
from transports.telnet import TelnetTransport


@dataclass
class DeviceResult:
    device: str
    host: str
    vendor: str
    ok: bool
    output: str = ""
    error: str | None = None
    duration_ms: int = 0
    diff: str | None = None
    command: str | None = None


@dataclass
class Device:
    name: str
    host: str
    vendor: str = "junos"
    port: int | None = None


TRANSPORT_MAP: dict[str, type[BaseTransport]] = {
    "junos": JunosTransport,
    "arista": AristaTransport,
    "generic": GenericSSHTransport,
    "ssh": GenericSSHTransport,
    "rest": RestTransport,
    "telnet-ios": TelnetTransport,
    "telnet-junos": TelnetTransport,
    "telnet-arista": TelnetTransport,
    "telnet-nxos": TelnetTransport,
    "telnet": TelnetTransport,
}


def _run_device(device: Device, user: str | None, password: str | None, command: str,
                mode: str, session_timeout: int, command_timeout: int,
                edit_payload: str | None = None, dry_run: bool = False,
                confirmed_minutes: int = 0,
                auth: dict | None = None) -> DeviceResult:
    start = time.monotonic()
    transport_cls = TRANSPORT_MAP.get(device.vendor, GenericSSHTransport)
    transport = transport_cls()
    try:
        # Per-device auth override (for REST targets in --job mode)
        dev_user = user
        dev_password = password
        if auth:
            dev_user = auth.get("scheme", user)
            dev_password = auth.get("token", password)
        # Set vendor for telnet prompt detection
        if hasattr(transport, 'set_vendor'):
            actual_vendor = device.vendor.replace("telnet-", "") if device.vendor.startswith("telnet-") else "ios"
            transport.set_vendor(actual_vendor)
        transport.connect(device.host, dev_user, dev_password,
                          timeout=session_timeout, port=device.port)
        if mode == "show":
            output = transport.show(command, timeout=command_timeout)
            elapsed = int((time.monotonic() - start) * 1000)
            return DeviceResult(device=device.name, host=device.host, vendor=device.vendor,
                                ok=True, output=output.rstrip(), duration_ms=elapsed,
                                command=command)
        elif mode in ("edit-command", "edit-broadcast"):
            result = transport.edit(edit_payload or command, dry_run=dry_run,
                                    confirmed_minutes=confirmed_minutes)
            elapsed = int((time.monotonic() - start) * 1000)
            return DeviceResult(device=device.name, host=device.host, vendor=device.vendor,
                                ok=result.ok, output=result.diff or "", error=result.error,
                                duration_ms=elapsed, diff=result.diff, command=command)
        elif mode == "edit-directory":
            result = transport.edit(edit_payload or "", dry_run=dry_run,
                                    confirmed_minutes=confirmed_minutes)
            elapsed = int((time.monotonic() - start) * 1000)
            return DeviceResult(device=device.name, host=device.host, vendor=device.vendor,
                                ok=result.ok, output=result.diff or "", error=result.error,
                                duration_ms=elapsed, diff=result.diff, command=command)
        else:
            elapsed = int((time.monotonic() - start) * 1000)
            return DeviceResult(device=device.name, host=device.host, vendor=device.vendor,
                                ok=False, error=f"Unknown mode: {mode}", duration_ms=elapsed,
                                command=command)
    except Exception as e:
        elapsed = int((time.monotonic() - start) * 1000)
        return DeviceResult(device=device.name, host=device.host, vendor=device.vendor,
                            ok=False, error=str(e), duration_ms=elapsed, command=command)
    finally:
        transport.close()


def run_parallel(devices: list[Device], user: str | None, password: str | None,
                 command: str, mode: str, workers: int = 8,
                 session_timeout: int = 30, command_timeout: int = 120,
                 edit_payloads: dict[str, str] | None = None,
                 per_device_commands: dict[str, str] | None = None,
                 per_device_modes: dict[str, str] | None = None,
                 per_device_auth: dict[str, dict] | None = None,
                 dry_run: bool = False, confirmed_minutes: int = 0,
                 on_start=None, on_complete=None) -> list[DeviceResult]:
    results: list[DeviceResult] = []
    with ThreadPoolExecutor(max_workers=min(workers, len(devices))) as executor:
        futures = {}
        for dev in devices:
            # Per-device command override (for --job mode)
            dev_command = command
            if per_device_commands and dev.name in per_device_commands:
                dev_command = per_device_commands[dev.name]
            # Per-device mode override (for --job mode with mixed operations)
            dev_mode = mode
            if per_device_modes and dev.name in per_device_modes:
                dev_mode = per_device_modes[dev.name]
            payload = None
            if edit_payloads and dev.name in edit_payloads:
                payload = edit_payloads[dev.name]
            elif dev_mode in ("edit-command", "edit-broadcast"):
                payload = dev_command
            dev_auth = None
            if per_device_auth and dev.name in per_device_auth:
                dev_auth = per_device_auth[dev.name]
            if on_start:
                on_start(dev)
            future = executor.submit(_run_device, dev, user, password, dev_command,
                                     dev_mode, session_timeout, command_timeout,
                                     payload, dry_run, confirmed_minutes, dev_auth)
            futures[future] = dev

        for future in as_completed(futures):
            result = future.result()
            if on_complete:
                on_complete(result)
            results.append(result)

    # Sort results to match input device order
    device_order = {d.name: i for i, d in enumerate(devices)}
    results.sort(key=lambda r: device_order.get(r.device, 999))
    return results
