from __future__ import annotations

import threading

from hccast_wired.session import HCCASTSession
from hccast_wired.transport import Transport, TransportError


class BlockingReadTransport(Transport):
    """Transport that keeps the session reader inside one in-flight read."""

    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.release_read = threading.Event()
        self.closed = threading.Event()

    def write(self, data: bytes) -> None:
        del data

    def read(self, *, timeout_ms: int = 500) -> bytes:
        del timeout_ms
        self.read_started.set()
        self.release_read.wait(timeout=2.0)
        return b""

    def close(self) -> None:
        self.closed.set()


class SlowTimeoutTransport(Transport):
    """Transport whose bounded read returns just after the old close grace period."""

    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.read_returned = threading.Event()
        self.closed_before_read_returned = False

    def write(self, data: bytes) -> None:
        del data

    def read(self, *, timeout_ms: int = 500) -> bytes:
        del timeout_ms
        self.read_started.set()
        threading.Event().wait(timeout=0.7)
        self.read_returned.set()
        return b""

    def close(self) -> None:
        self.closed_before_read_returned = not self.read_returned.is_set()


class CloseInterruptsReadTransport(Transport):
    """Transport whose in-flight read reports closure as an endpoint error."""

    def __init__(self) -> None:
        self.read_started = threading.Event()
        self.closed = threading.Event()

    def write(self, data: bytes) -> None:
        del data

    def read(self, *, timeout_ms: int = 500) -> bytes:
        del timeout_ms
        self.read_started.set()
        self.closed.wait(timeout=2.0)
        raise TransportError("endpoint shut down by close")

    def close(self) -> None:
        self.closed.set()


def test_close_waits_for_reader_before_closing_transport() -> None:
    transport = BlockingReadTransport()
    session = HCCASTSession(transport)
    session.start_reader()
    assert transport.read_started.wait(timeout=1.0)

    close_finished = threading.Event()

    def close_session() -> None:
        session.close()
        close_finished.set()

    closer = threading.Thread(target=close_session)
    closer.start()
    try:
        assert session._reader_stop.wait(timeout=1.0)
        assert not transport.closed.wait(timeout=0.1)

        transport.release_read.set()
        assert close_finished.wait(timeout=1.0)
        assert transport.closed.is_set()
        assert not session._reader.is_alive()
        session._raise_reader_error()
    finally:
        transport.release_read.set()
        closer.join(timeout=2.0)


def test_close_allows_bounded_reader_timeout_before_transport_close() -> None:
    transport = SlowTimeoutTransport()
    session = HCCASTSession(transport)
    session.start_reader()
    assert transport.read_started.wait(timeout=1.0)

    session.close()

    assert transport.read_returned.is_set()
    assert not transport.closed_before_read_returned
    assert not session._reader.is_alive()
    session._raise_reader_error()


def test_close_does_not_report_transport_error_caused_by_shutdown() -> None:
    transport = CloseInterruptsReadTransport()
    session = HCCASTSession(transport)
    session.start_reader()
    assert transport.read_started.wait(timeout=1.0)

    session.close()

    assert not session._reader.is_alive()
    session._raise_reader_error()
