"""Transport interfaces shared by host-mode USB and FunctionFS gadget mode."""

from __future__ import annotations

from abc import ABC, abstractmethod


class TransportError(RuntimeError):
    pass


class Transport(ABC):
    @abstractmethod
    def write(self, data: bytes) -> None:
        """Write one logical HCCAST frame to the byte stream."""

    @abstractmethod
    def read(self, *, timeout_ms: int = 500) -> bytes:
        """Read available bytes; return ``b''`` on timeout."""

    @abstractmethod
    def close(self) -> None:
        """Release transport resources."""

    def __enter__(self) -> "Transport":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()
