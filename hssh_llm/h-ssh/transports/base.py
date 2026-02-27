from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EditResult:
    ok: bool
    diff: str | None = None
    error: str | None = None


class BaseTransport(ABC):
    @abstractmethod
    def connect(self, host: str, user: str, password: str | None = None, timeout: int = 30) -> None:
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
