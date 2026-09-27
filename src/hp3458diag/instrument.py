"""Audited 3458A identification and error-register reads. No measurement, no ACAL.

Sources: Ag_3458A_UserGuide_en.pdf ID? p. 186, REV? p. 228, ERR? p. 177,
ERRSTR? p. 178. ERR?/ERRSTR? clear what they return: each is read exactly once,
never repeated for consensus, and every raw reply is kept.
"""

import importlib.metadata
import platform
import re
import sys
from dataclasses import dataclass
from .transport.base import Transport

DOCUMENTED_IDENTITY = "HP 3458A"
MAX_ERROR_STRINGS = 32

_REVISION = re.compile(r"\s*([+-]?[0-9]+(?:\.[0-9]*)?)\s*,\s*([+-]?[0-9]+(?:\.[0-9]*)?)\s*")
_ERRSTR = re.compile(r"\s*([+-]?[0-9]+)\s*,\s*\"([^\"]*)\"\s*")
_INTEGER = re.compile(r"\s*[+-]?[0-9]+(?:\.0*)?(?:[Ee][+]?[0-9]+)?\s*")

ERROR_BITS = (
    "Hardware error (see AUXERR?)", "Calibration error", "Trigger too fast error",
    "Syntax error", "Command not allowed from remote", "Undefined parameter received",
    "Parameter out of range", "Memory error", "Destructive overload detected",
    "Out of calibration", "Calibration required", "Settings conflict",
    "Math error", "Subprogram error", "System error",
)


class ResponseFormatError(ValueError):
    def __init__(self, message: str, raw: bytes) -> None:
        super().__init__(f"{message}: {raw!r}")
        self.raw = raw


class IdentityError(ResponseFormatError):
    """Not an accepted 3458A identity. No further command is sent to that device."""


def _line(raw: bytes) -> str:
    if not isinstance(raw, bytes):
        raise ResponseFormatError("Transport must return bytes", b"")
    if not raw.endswith(b"\r\n"):
        raise ResponseFormatError("Missing CRLF terminator; framing unverified", raw)
    try:
        return raw[:-2].decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise ResponseFormatError("Non-ASCII byte", raw) from exc


def identify(transport: Transport, accepted: tuple[str, ...] = (DOCUMENTED_IDENTITY,)
             ) -> tuple[str, bytes]:
    """Exact identity comparison; variants must be added to the config from H01 evidence."""
    if not accepted or not all(isinstance(text, str) and text for text in accepted):
        raise ValueError("At least one accepted identity string is required")
    raw = transport.query_raw("ID?")
    text = _line(raw).strip()
    if text not in accepted:
        raise IdentityError(f"Unexpected identity, accepted {list(accepted)}", raw)
    return text, raw


def read_revision(transport: Transport) -> tuple[tuple[str, str], bytes]:
    """Master and slave firmware revision as the original number text."""
    raw = transport.query_raw("REV?")
    match = _REVISION.fullmatch(_line(raw))
    if match is None:
        raise ResponseFormatError("REV? must return two comma-separated numbers", raw)
    return (match[1], match[2]), raw


@dataclass(frozen=True)
class ErrorRegister:
    value: int | None
    bits: tuple[str, ...]
    raw: bytes
    error: str | None


def decode_error_bits(value: int) -> tuple[str, ...]:
    if value < 0 or value >= 1 << len(ERROR_BITS):
        raise ValueError(f"Error register value outside documented bits: {value}")
    return tuple(name for bit, name in enumerate(ERROR_BITS) if value & (1 << bit))


def read_error_register(transport: Transport) -> ErrorRegister:
    """Single ERR? read. It clears the register, so a parse failure is not retried."""
    raw = transport.query_raw("ERR?")
    try:
        text = _line(raw)
        if not _INTEGER.fullmatch(text):
            raise ResponseFormatError("ERR? must return an integer", raw)
        value = int(float(text))
        return ErrorRegister(value, decode_error_bits(value), raw, None)
    except ValueError as exc:
        return ErrorRegister(None, (), raw, str(exc))


@dataclass(frozen=True)
class ErrorLog:
    entries: tuple[tuple[int, str], ...]
    raw: tuple[bytes, ...]
    complete: bool
    error: str | None


def read_error_strings(transport: Transport, limit: int = MAX_ERROR_STRINGS) -> ErrorLog:
    """ERRSTR? until 0,"NO ERROR"; each call clears one bit (p. 178).

    Stops without a further query on a malformed reply or after `limit` entries;
    then `complete` is False and the remaining register state is unknown.
    """
    entries: list[tuple[int, str]] = []
    raws: list[bytes] = []
    for _ in range(limit):
        raw = transport.query_raw("ERRSTR?")
        raws.append(raw)
        try:
            match = _ERRSTR.fullmatch(_line(raw))
            if match is None:
                raise ResponseFormatError('ERRSTR? must return number,"message"', raw)
        except ResponseFormatError as exc:
            return ErrorLog(tuple(entries), tuple(raws), False, str(exc))
        code, message = int(match[1]), match[2]
        if code == 0:
            return ErrorLog(tuple(entries), tuple(raws), True, None)
        entries.append((code, message))
    return ErrorLog(tuple(entries), tuple(raws), False,
                    f"Stopped after {limit} ERRSTR? replies without NO ERROR")


@dataclass(frozen=True)
class Preflight:
    identity: str
    identity_raw: bytes
    revision: tuple[str, str]
    revision_raw: bytes
    preexisting_errors: ErrorLog
    error_register_after: ErrorRegister | None


def preflight_identity(transport: Transport,
                       accepted: tuple[str, ...] = (DOCUMENTED_IDENTITY,)) -> Preflight:
    """ID? first; only a verified 3458A receives REV?, ERRSTR? and ERR?.

    Pre-existing errors are archived, not interpreted as a verdict. No configuration,
    trigger, memory, calibration or measurement command is sent.
    """
    identity, identity_raw = identify(transport, accepted)
    revision, revision_raw = read_revision(transport)
    errors = read_error_strings(transport)
    register = read_error_register(transport) if errors.complete else None
    return Preflight(identity, identity_raw, revision, revision_raw, errors, register)


def software_environment() -> dict[str, str]:
    versions = {"python": sys.version.split()[0], "platform": platform.platform()}
    for package in ("pyvisa", "pyvisa-py", "PySide6"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not installed"
    return versions
