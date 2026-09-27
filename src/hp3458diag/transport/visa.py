"""PyVISA adapter for the 3458A command language. Not a generic SCPI driver.

Importing this module or constructing `VisaTransport` performs no I/O and does not
import PyVISA. Hardware is touched only by an explicit `open()` call.

Framing (Ag_3458A_UserGuide_en.pdf): commands end with CR LF and EOI on the last byte;
ASCII output ends with CR LF (p. 176). `END` controls EOI for readings only: H01 on
the real instrument (docs/validation/h01) showed ID?/REV?/ERR?/END? replies carry no
EOI even with END ON. The default "lf" framing therefore stops at LF (or END); the
parser still requires the full CR LF and the original bytes are returned unchanged.
"""

import base64
import json
import os
import re
import threading
from pathlib import Path
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from ..commands import check_command
from .base import (FramingUnknownError, TraceEntry, TransportIOError,
                   TransportOwnershipError, TransportStateError, TransportTimeout)

# VISA specification completion/error codes (values fixed by the VISA standard).
VI_ERROR_TMO = -1073807339          # 0xBFFF0015
VI_SUCCESS_MAX_CNT = 0x3FFF0006     # read stopped at requested count, not at END
VI_SUCCESS_TERM_CHAR = 0x3FFF0005   # read stopped at the termination character
READ_TERMINATIONS = ("eoi", "lf")

GPIB_RESOURCE = re.compile(r"GPIB(?P<board>[0-9]{0,2})::(?P<primary>[0-9]{1,2})"
                           r"(?:::(?P<secondary>[0-9]{1,3}))?::INSTR")


@dataclass(frozen=True)
class VisaSettings:
    """Validated connection settings. The resource is always chosen by the operator."""
    resource: str
    backend: str = ""
    timeout_ms: int = 5000
    chunk_bytes: int = 4096
    max_response_bytes: int = 65536
    # "lf" (default, H01-verified): LF or END ends a reply. "eoi": only END ends it;
    # times out on query replies (H01); kept for RMEM characterization in WP-02.
    # Keysight VISA reports status 0 for both LF and END: the status is not EOI proof.
    read_termination: str = "lf"

    def __post_init__(self) -> None:
        match = GPIB_RESOURCE.fullmatch(self.resource) if isinstance(self.resource, str) else None
        if match is None:
            raise ValueError("Only an explicit GPIB INSTR resource is supported, "
                             f"got {self.resource!r}")
        if not 0 <= int(match["primary"]) <= 30:
            raise ValueError("GPIB primary address must be 0..30")
        if match["secondary"] is not None and not 96 <= int(match["secondary"]) <= 126:
            raise ValueError("GPIB secondary address must be 96..126")
        if not isinstance(self.backend, str):
            raise ValueError("VISA backend must be text; empty means PyVISA default")
        for name, low, high in (("timeout_ms", 100, 600_000), ("chunk_bytes", 1, 1 << 20),
                                ("max_response_bytes", 64, 1 << 24)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError(f"{name} must be an integer in {low}..{high}")
        if self.read_termination not in READ_TERMINATIONS:
            raise ValueError(f"read_termination must be one of {READ_TERMINATIONS}")
        if self.chunk_bytes > self.max_response_bytes:
            raise ValueError("chunk_bytes must not exceed max_response_bytes")


def _pyvisa_resource_manager(backend: str) -> Any:
    import pyvisa  # optional 'hardware' dependency, imported only by explicit open()
    return pyvisa.ResourceManager(backend) if backend else pyvisa.ResourceManager()


_PROCESS_POISONED = False
# Poisoned handles are kept referenced on purpose: dropping the last reference runs
# PyVISA's __del__, whose native viClose hangs (H04 evidence). os._exit then skips it.
_ABANDONED: list[Any] = []


def process_poisoned() -> bool:
    """True after a native VISA crash in this process: PyVISA's own exit cleanup would
    hang in viClose, so the CLI must leave with os._exit after flushing its output."""
    return _PROCESS_POISONED


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


class VisaTransport:
    """Single-owner connection. After a timeout or I/O failure framing is unknown.

    While framing is unknown, `write` and `query_raw` refuse to send anything.
    Only `serial_poll` (does not disturb the output buffer, p. 306), `clear_device`
    (documented SDC recovery, p. 304) and `close` remain available.
    """

    def __init__(self, settings: VisaSettings,
                 resource_manager_factory: Callable[[str], Any] | None = None,
                 journal: Path | None = None) -> None:
        """journal: append-only JSON lines, written and fsynced before and after every
        bus operation, so a hang or a killed process still leaves the evidence."""
        if not isinstance(settings, VisaSettings):
            raise TypeError("VisaTransport requires validated VisaSettings")
        self.settings = settings
        self._factory = resource_manager_factory or _pyvisa_resource_manager
        self._manager: Any = None
        self._resource: Any = None
        self._owner: int | None = None
        self.framing_unknown = False
        # A native crash inside the VISA library ("access violation" after an unanswered
        # RMEM, H04 evidence): SDC and further I/O still worked, but native close hung.
        self.backend_poisoned = False
        self.trace: list[TraceEntry] = []
        self.backend_info: dict[str, str] = {}
        self._journal = Path(journal) if journal is not None else None

    def _journal_line(self, item: dict[str, Any]) -> None:
        if self._journal is None:
            return
        encoded = {key: (base64.b64encode(value).decode() if isinstance(value, bytes)
                         else list(value) if isinstance(value, tuple) else value)
                   for key, value in item.items()}
        with self._journal.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(encoded, ensure_ascii=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def _pending(self, operation: str, command: str | None) -> None:
        self._journal_line({"pending": operation, "command": command,
                            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                            "monotonic_s": time.monotonic()})

    @property
    def is_open(self) -> bool:
        return self._resource is not None

    def _record(self, operation: str, command: str | None, sent: bytes | None,
                received: bytes | None, error: str | None,
                statuses: tuple[int, ...] = ()) -> None:
        entry = TraceEntry(len(self.trace) + 1, datetime.now(timezone.utc).isoformat(),
                           time.monotonic(), operation, command, sent, received, error,
                           statuses)
        self.trace.append(entry)
        self._journal_line(entry.__dict__)

    def _require_owner(self) -> None:
        if self._owner is not None and threading.get_ident() != self._owner:
            raise TransportOwnershipError("Connection used outside its owner thread")

    def _require_open(self) -> None:
        self._require_owner()
        if self._resource is None:
            raise TransportStateError("Transport is not open; call open() explicitly")

    def _require_framed(self) -> None:
        self._require_open()
        if self.framing_unknown:
            raise FramingUnknownError("Framing unknown after previous failure; "
                                      "documented recovery required before any query")

    def _failure(self, operation: str, command: str | None, sent: bytes | None,
                 partial: bytes, exc: BaseException,
                 statuses: tuple[int, ...] = ()) -> OSError:
        self.framing_unknown = True
        if isinstance(exc, OSError) and "access violation" in str(exc).lower():
            global _PROCESS_POISONED
            self.backend_poisoned = _PROCESS_POISONED = True
        self._record(operation, command, sent, partial, _describe(exc), statuses)
        if getattr(exc, "error_code", None) == VI_ERROR_TMO or isinstance(exc, TimeoutError):
            return TransportTimeout(f"{operation} timeout: {command!r}", partial)
        return TransportIOError(f"{operation} failed: {command!r}: {_describe(exc)}", partial)

    def open(self) -> None:
        if self._resource is not None:
            raise TransportStateError("Transport is already open")
        self._owner = threading.get_ident()
        try:
            manager = self._factory(self.settings.backend)
        except Exception as exc:
            self._owner = None
            self._record("open", None, None, None, _describe(exc))
            raise TransportIOError(f"VISA backend unavailable: {_describe(exc)}") from exc
        try:
            resource = manager.open_resource(self.settings.resource)
            resource.timeout = self.settings.timeout_ms
            # "lf": LF termchar (query replies have no EOI, H01). "eoi": END only.
            resource.read_termination = "\n" if self.settings.read_termination == "lf" else None
            resource.write_termination = None
            resource.send_end = True
        except Exception as exc:
            self._owner = None
            self._record("open", None, None, None, _describe(exc))
            try:
                manager.close()
            except Exception:
                pass
            raise TransportIOError(f"Cannot open {self.settings.resource}: "
                                   f"{_describe(exc)}") from exc
        self._manager, self._resource = manager, resource
        self.framing_unknown = False
        self.backend_info = {"resource": self.settings.resource,
                             "backend": self.settings.backend or "default",
                             "visa_library": str(getattr(manager, "visalib", "unknown")),
                             "timeout_ms": str(self.settings.timeout_ms),
                             "read_termination": self.settings.read_termination}
        self._record("open", None, None, None, None)

    def write(self, command: str, *, acal: bool = False) -> None:
        """acal=True only from acal.run_acal (operator-confirmed WP-08 autocal)."""
        self._require_framed()
        check_command(command, acal=acal)
        payload = (command + "\r\n").encode("ascii")
        self._pending("write", command)
        try:
            self._resource.write_raw(payload)
        except Exception as exc:
            raise self._failure("write", command, payload, b"", exc) from exc
        self._record("write", command, payload, None, None)

    def query_raw(self, command: str) -> bytes:
        """Send one allowlisted query and return exactly one END-delimited reply."""
        self._require_framed()
        check_command(command)
        payload = (command + "\r\n").encode("ascii")
        self._pending("query", command)
        try:
            self._resource.write_raw(payload)
        except Exception as exc:
            raise self._failure("query", command, payload, b"", exc) from exc
        chunks: list[bytes] = []
        statuses: list[int] = []
        received = 0
        library, session = self._resource.visalib, self._resource.session
        try:
            while True:
                data, status = library.read(session, self.settings.chunk_bytes)
                chunk = bytes(data)
                chunks.append(chunk)
                statuses.append(int(status))
                received += len(chunk)
                if received > self.settings.max_response_bytes:
                    raise TransportIOError(f"Reply exceeds {self.settings.max_response_bytes}"
                                           " bytes; remaining bytes may be pending")
                if int(status) != VI_SUCCESS_MAX_CNT:
                    break
                if not chunk:
                    raise TransportIOError("Backend returned an empty partial chunk")
        except Exception as exc:
            raise self._failure("query", command, payload, b"".join(chunks), exc,
                                tuple(statuses)) from exc
        reply = b"".join(chunks)
        self._record("query", command, payload, reply, None, tuple(statuses))
        return reply

    def set_timeout(self, timeout_ms: int) -> None:
        """Per-operation backend timeout, e.g. for a TARM SGL write that holds the bus
        until the block completes (INBUF OFF, p. 75). Logged; limits as VisaSettings."""
        self._require_framed()
        if type(timeout_ms) is not int or not 100 <= timeout_ms <= 600_000:
            raise ValueError("timeout_ms must be an integer in 100..600000")
        try:
            self._resource.timeout = timeout_ms
        except Exception as exc:
            raise self._failure("set_timeout", str(timeout_ms), None, b"", exc) from exc
        self.backend_info["timeout_ms"] = str(timeout_ms)
        self._record("set_timeout", str(timeout_ms), None, None, None)

    def set_read_termination(self, mode: str) -> None:
        """Switch reply framing inside an open, framed session (H01 comparison only)."""
        self._require_framed()
        if mode not in READ_TERMINATIONS:
            raise ValueError(f"read_termination must be one of {READ_TERMINATIONS}")
        try:
            self._resource.read_termination = "\n" if mode == "lf" else None
        except Exception as exc:
            raise self._failure("set_read_termination", mode, None, b"", exc) from exc
        self.backend_info["read_termination"] = mode
        self._record("set_read_termination", mode, None, None, None)

    def serial_poll(self) -> int:
        """GPIB serial poll; READY is bit 4 (16), error bit 5 (32), p. 306."""
        self._require_open()
        self._pending("serial_poll", None)
        try:
            status = int(self._resource.read_stb())
        except Exception as exc:
            self._record("serial_poll", None, None, None, _describe(exc))
            if getattr(exc, "error_code", None) == VI_ERROR_TMO:
                raise TransportTimeout("serial poll timeout") from exc
            raise TransportIOError(f"serial poll failed: {_describe(exc)}") from exc
        if not 0 <= status <= 255:
            self._record("serial_poll", None, None, None, f"status out of range: {status}")
            raise TransportIOError(f"Serial poll status out of range: {status}")
        self._record("serial_poll", None, None, bytes((status,)), None)
        return status

    def clear_device(self, reason: str) -> None:
        """Selected Device Clear: explicit, logged framing recovery (p. 304).

        Side effects: clears input/output buffers and the status register, and
        disables triggering. It is never sent automatically. The caller must verify
        instrument state (e.g. HOLD, MCOUNT?) again before relying on memory.
        """
        self._require_open()
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("Device clear requires a recorded reason")
        self._pending("device_clear", reason)
        try:
            self._resource.clear()
        except Exception as exc:
            self._record("device_clear", reason, None, None, _describe(exc))
            raise TransportIOError(f"Device clear failed: {_describe(exc)}") from exc
        self.framing_unknown = False
        self._record("device_clear", reason, None, None, None)

    def close(self) -> None:
        """Idempotent. Always releases the handles, even if the backend reports an error."""
        if self._resource is None and self._manager is None:
            return
        self._require_owner()
        resource, manager = self._resource, self._manager
        self._resource = self._manager = None
        self._owner = None
        if self.backend_poisoned:
            # Native close would hang (observed twice); the OS releases it at process exit.
            _ABANDONED.append((resource, manager))
            self._record("close_skipped", None, None, None,
                         "VISA library poisoned by a native crash; handles abandoned")
            return
        errors = []
        for handle in (resource, manager):
            if handle is None:
                continue
            try:
                handle.close()
            except Exception as exc:
                errors.append(_describe(exc))
        self._record("close", None, None, None, "; ".join(errors) or None)
        if errors:
            raise TransportIOError("Close reported: " + "; ".join(errors))
