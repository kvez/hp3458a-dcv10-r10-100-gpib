"""Only explicit query responses are read; no implied measurement reads."""
from dataclasses import dataclass
from typing import Protocol


class TransportTimeout(TimeoutError):
    """Backend timeout. `partial` keeps every byte received before the timeout."""

    def __init__(self, message: str, partial: bytes = b"") -> None:
        super().__init__(message)
        self.partial = partial


class TransportIOError(OSError):
    """Any other backend I/O failure, normalized away from the VISA library."""

    def __init__(self, message: str, partial: bytes = b"") -> None:
        super().__init__(message)
        self.partial = partial


class FramingUnknownError(OSError):
    """A previous reply may still be pending; no new query before documented recovery."""


class TransportOwnershipError(RuntimeError):
    """The connection was used outside its single owner thread."""


class TransportStateError(RuntimeError):
    """Operation not allowed in the current open/closed state."""


@dataclass(frozen=True)
class TraceEntry:
    """One bus transaction. Raw bytes are kept even when the transaction failed."""
    sequence: int
    timestamp_utc: str
    monotonic_s: float
    operation: str
    command: str | None
    sent: bytes | None
    received: bytes | None
    error: str | None
    # VISA completion status of each read call, e.g. 0 = END (EOI), 0x3FFF0005 = termchar.
    read_statuses: tuple[int, ...] = ()


class Transport(Protocol):
    """One owner/thread per connection. Read must consume exactly one explicit reply.

    The VISA adapter keeps raw bytes, normalizes backend timeouts to TransportTimeout
    (a TimeoutError), and marks response framing unknown after timeout before any
    recovery attempt.
    """

    def write(self, command: str, *, acal: bool = False) -> None: ...

    def query_raw(self, command: str) -> bytes: ...

    def serial_poll(self) -> int: ...

    def clear_device(self, reason: str) -> None: ...

    def close(self) -> None: ...
