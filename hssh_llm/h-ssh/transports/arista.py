from .base import BaseTransport, EditResult


class AristaTransport(BaseTransport):
    """Arista eAPI transport — stub. Requires pyeapi (not installed in dev)."""

    def connect(self, host: str, user: str | None = None, password: str | None = None,
                timeout: int = 30, port: int | None = None) -> None:
        raise NotImplementedError("Arista eAPI transport requires pyeapi. Install with: pip install pyeapi")

    def show(self, command: str, timeout: int = 120) -> str:
        raise NotImplementedError("Not connected")

    def edit(self, payload: str, dry_run: bool = False, confirmed_minutes: int = 0) -> EditResult:
        return EditResult(ok=False, error="Not connected")

    def close(self) -> None:
        pass
