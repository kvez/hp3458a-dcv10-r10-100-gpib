"""Strict ASCII framing and whole-response repeatability.

Agreement is evidence of consistency, not a checksum or proof of correctness.
All responses, including rejected ones, are retained in the returned evidence.
"""

import base64
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable

READING = re.compile(r"[+-][0-9]\.[0-9]{8}E[+-][0-9]{2}")
SCALAR = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?")
_UNSIGNED = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[Ee][+-]?[0-9]+)?"
_ENGINEERING = re.compile(r"(?P<sign>[ -])(?P<int>[1-9][0-9]{0,2})\.(?P<frac>[0-9]+)"
                          r"E(?P<exp>[+-][0-9]{2})")
ENGINEERING_ZERO = " 0.0000000E+00"  # the instrument's zero: 7 decimals (WP-02 live)


def parse_engineering(text: str) -> Decimal:
    """REV 9,1 scalar grammar (every real NPLC?/DELAY?/LFREQ?/RMATH reply, WP-03 fixture):
    sign slot (space or '-'), 9 significant digits as d.dddddddd / dd.ddddddd /
    ddd.dddddd, exponent a multiple of 3; zero is exactly ' 0.0000000E+00'."""
    if text == ENGINEERING_ZERO:
        return Decimal(0)
    match = _ENGINEERING.fullmatch(text)
    if (match is None or len(match["int"]) + len(match["frac"]) != 9
            or int(match["exp"]) % 3):
        raise DataValidationError(f"not a 9-digit engineering reply: {text!r}")
    return Decimal(text.strip())


@dataclass(frozen=True)
class ReadingProfile:
    """One exact reply format. A block must match a single profile; no mixing.

    `verified_revisions` lists REV? (master, slave) pairs on which the profile was
    observed; empty means documentation only. Nothing is inferred beyond the evidence.
    """
    name: str
    reading: re.Pattern[str]
    scalar: re.Pattern[str]
    evidence: str
    verified_revisions: tuple[tuple[str, str], ...]
    # Strict scalar grammar replacing `scalar` when set (real replies, WP-03).
    scalar_parser: Callable[[str], Decimal] | None = None
    # DMM STAT definition verified on the instrument (H05): ddof=1, half-quantum rounding.
    stat_verified: bool = False

    def verified_for(self, revision: tuple[str, str]) -> bool:
        return revision in self.verified_revisions


MANUAL_P92 = ReadingProfile(
    "manual_p92", READING, SCALAR,
    "Ag_3458A_UserGuide_en.pdf p. 92: +d.ddddddddE+dd, 15 characters", ())
# C21: H01 on the real instrument (REV 9,1, OFORMAT ASCII, NDIG 8): a space replaces
# "+", nine decimals, 16 characters; verified on DCV 0.1/10 and OHMF 10/100 (H05 runs). Scalars (NPLC?, LFREQ?) may start with
# one space. Byte-exact evidence: tests/fixtures/h01_rmem_dcv10_n100.bin.
HP3458A_REV9_1 = ReadingProfile(
    "hp3458a_rev9_1", re.compile(r"[ -][0-9]\.[0-9]{9}E[+-][0-9]{2}"),
    re.compile(r"(?:[+-]| )?" + _UNSIGNED),
    "docs/validation/h01 run 7; tests/fixtures/h01_rmem_dcv10_n100.bin; "
    "tests/fixtures/real_query_replies.json; docs/validation/h05", (("9", "1"),),
    scalar_parser=parse_engineering, stat_verified=True)
PROFILES = {profile.name: profile for profile in (MANUAL_P92, HP3458A_REV9_1)}


def get_profile(name: str) -> ReadingProfile:
    try:
        return PROFILES[name]
    except (KeyError, TypeError):
        raise ValueError(f"Unknown reading profile {name!r}; known: {sorted(PROFILES)}")


class DataValidationError(ValueError):
    pass


class FramingError(DataValidationError):
    """A complete explicit response has not been established."""


def classify_frame(raw: object) -> str:
    """Evidence label for a reply frame; only COMPLETE may be parsed."""
    if not isinstance(raw, bytes):
        return "NOT_BYTES"
    if not raw:
        return "EMPTY"
    if not raw.endswith(b"\r\n"):
        return "LF_ONLY" if raw.endswith(b"\n") else "NO_TERMINATOR"
    body = raw[:-2]
    if b"\r" in body or b"\n" in body:
        return "EMBEDDED_TERMINATOR"
    try:
        body.decode("ascii")
    except UnicodeDecodeError:
        return "NON_ASCII"
    return "COMPLETE"


def _text(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise FramingError("Transport must return original bytes")
    if not payload.endswith(b"\r\n"):
        raise FramingError("Missing CRLF terminator; framing unverified")
    try:
        return payload[:-2].decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise DataValidationError("Non-ASCII byte") from exc


def parse_readings(payload: bytes, expected: int, bounds: tuple[float, float],
                   profile: ReadingProfile = MANUAL_P92) -> tuple[Decimal, ...]:
    if type(expected) is not int or expected < 1:
        raise DataValidationError("Invalid expected count")
    low, high = (Decimal(str(v)) for v in bounds)
    if not low.is_finite() or not high.is_finite() or low > high:
        raise DataValidationError("Invalid physical bounds")
    fields = _text(payload).split(",")
    if len(fields) != expected:
        raise DataValidationError(f"Expected {expected} readings, received {len(fields)}")
    values: list[Decimal] = []
    for field in fields:
        if not profile.reading.fullmatch(field):
            raise DataValidationError(f"Reading does not match profile {profile.name}")
        value = Decimal(field.lstrip(" "))
        if abs(value) >= Decimal("1e37"):
            raise DataValidationError("Overload sentinel")
        if not low <= value <= high:
            raise DataValidationError("Outside broad physical range; preserve as invalid")
        values.append(value)
    return tuple(values)


def parse_scalar(payload: bytes, profile: ReadingProfile = MANUAL_P92
                 ) -> tuple[Decimal, ...]:
    text = _text(payload)
    if profile.scalar_parser is not None:
        value = profile.scalar_parser(text)
    elif not profile.scalar.fullmatch(text):
        raise DataValidationError("Invalid numeric query response")
    else:
        value = Decimal(text.lstrip(" "))
    if not value.is_finite() or abs(value) >= Decimal("1e37"):
        raise DataValidationError("Non-finite value or overload")
    return (value,)


@dataclass(frozen=True)
class Attempt:
    number: int
    timestamp_utc: str
    raw_base64: str | None
    values: tuple[str, ...] | None
    error: str | None
    frame: str | None = None          # classify_frame() of the reply (evidence label)
    monotonic_s: float | None = None  # host monotonic time of the attempt


@dataclass(frozen=True)
class Consensus:
    status: str
    values: tuple[Decimal, ...] | None
    attempts: tuple[Attempt, ...]
    selected_attempts: tuple[int, ...]
    read_kind: str = "COLD"  # COLD: first read of this memory; RETRY: re-read, no trigger


def read_consistent(query: Callable[[], bytes],
                    parser: Callable[[bytes], tuple[Decimal, ...]],
                    read_kind: str = "COLD") -> Consensus:
    if read_kind not in ("COLD", "RETRY"):
        raise ValueError("read_kind must be COLD or RETRY")
    attempts: list[Attempt] = []
    candidates: dict[bytes, list[tuple[int, tuple[Decimal, ...]]]] = {}

    def done(status: str, values=None, selected=()) -> Consensus:
        return Consensus(status, values, tuple(attempts), tuple(selected), read_kind)

    for number in range(1, 4):
        timestamp = datetime.now(timezone.utc).isoformat()
        moment = time.monotonic()
        try:
            raw = query()
        except (TimeoutError, OSError) as exc:
            # A timeout may leave a partial reply pending. No blind automatic resend.
            attempts.append(Attempt(number, timestamp, None, None,
                                    f"{type(exc).__name__}: {exc}", "TRANSPORT_ERROR",
                                    moment))
            return done("TRANSPORT_ERROR")
        frame = classify_frame(raw)
        encoded = base64.b64encode(raw).decode() if isinstance(raw, bytes) else None
        try:
            values = parser(raw)
        except FramingError as exc:
            attempts.append(Attempt(number, timestamp, encoded, None, str(exc), frame, moment))
            return done("TRANSPORT_ERROR")
        except ValueError as exc:
            attempts.append(Attempt(number, timestamp, encoded, None, str(exc), frame, moment))
            continue
        attempts.append(Attempt(number, timestamp, encoded, tuple(str(v) for v in values),
                                None, frame, moment))
        matches = candidates.setdefault(raw, [])
        matches.append((number, values))
        if len(matches) == 2:
            return done("EXACT" if number == 2 else "MAJORITY", values,
                        tuple(i for i, _ in matches))
    return done("INVALID")
