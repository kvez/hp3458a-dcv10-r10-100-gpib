"""Query-type reply parsers with semantic checks (WP-03).

Grammar source: every distinct reply of the real HP3458A REV 9,1 recorded in
docs/validation (tests/fixtures/real_query_replies.json); meanings from the numeric
query equivalents of Ag_3458A_UserGuide_en.pdf. A reply that does not match its
query's grammar is rejected and kept as evidence; nothing is repaired or guessed.
"""

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from .validation import DataValidationError, classify_frame, parse_engineering

# Numeric query equivalents (value -> meaning) with their pages.
ENUMERATIONS: dict[str, tuple[dict[str, str], str]] = {
    "TARM?": ({"1": "AUTO", "2": "EXT", "3": "SGL", "4": "HOLD", "5": "SYN"}, "p. 251"),
    "MEM?": ({"0": "OFF", "1": "LIFO", "2": "FIFO", "3": "CONT"}, "pp. 196-197"),
    "MFORMAT?": ({"1": "ASCII", "2": "SINT", "3": "DINT", "4": "SREAL", "5": "DREAL"},
                 "pp. 198-199"),
    "OFORMAT?": ({"1": "ASCII", "2": "SINT", "3": "DINT", "4": "SREAL", "5": "DREAL"},
                 "p. 210"),
    "END?": ({"0": "OFF", "1": "ON", "2": "ALWAYS"}, "p. 176"),
    "AZERO?": ({"0": "OFF", "1": "ON", "2": "ONCE"}, "p. 162"),
    "OCOMP?": ({"0": "OFF", "1": "ON"}, "p. 209"),
    "INBUF?": ({"0": "OFF", "1": "ON"}, "p. 187"),
    "TERM?": ({"0": "OPEN", "1": "FRONT", "2": "REAR"}, "p. 254"),
}
ENGINEERING_QUERIES = {"NPLC?", "DELAY?", "LFREQ?", "RMATH MEAN", "RMATH SDEV",
                       "RMATH LOWER", "RMATH UPPER", "RMATH NSAMP"}
ERROR_REGISTER_MAX = (1 << 15) - 1  # bits 0..14, p. 177

_UNSIGNED = re.compile(r"[0-9]+")
_TEMP = re.compile(r"[0-9]{1,3}\.[0-9]")
_MSIZE = re.compile(r"([0-9]+),([0-9]+)")


class ReplyError(DataValidationError):
    def __init__(self, message: str, query: str, raw: bytes) -> None:
        super().__init__(f"{query}: {message}: {raw!r}")
        self.query, self.raw = query, raw


def _body(query: str, raw: bytes) -> str:
    frame = classify_frame(raw)
    if frame != "COMPLETE":
        raise ReplyError(f"frame {frame}", query, raw)
    return raw[:-2].decode("ascii")


@dataclass(frozen=True)
class Reply:
    query: str
    raw: bytes
    value: Any
    meaning: str | None = None


def parse_reply(query: str, raw: bytes) -> Reply:
    """Parse one complete reply by query type; raises ReplyError (raw kept) otherwise."""
    text = _body(query, raw)
    try:
        if query in ENUMERATIONS:
            table, _ = ENUMERATIONS[query]
            if text not in table:
                raise DataValidationError(f"value outside {sorted(table)}")
            return Reply(query, raw, int(text), table[text])
        if query in ENGINEERING_QUERIES:
            value = parse_engineering(text)
            if query == "RMATH NSAMP" and value != value.to_integral_value():
                raise DataValidationError("NSAMP is not an integer")
            if query in ("RMATH SDEV", "DELAY?", "NPLC?", "LFREQ?") and value < 0:
                raise DataValidationError("negative value")
            return Reply(query, raw, value)
        if query in ("MCOUNT?", "ERR?"):
            if not _UNSIGNED.fullmatch(text):
                raise DataValidationError("unsigned integer expected")
            value = int(text)
            if query == "ERR?" and value > ERROR_REGISTER_MAX:
                raise DataValidationError("bits beyond the documented register")
            return Reply(query, raw, value)
        if query == "MSIZE?":
            match = _MSIZE.fullmatch(text)
            if match is None:
                raise DataValidationError("'reading_bytes,subprogram_bytes' expected")
            return Reply(query, raw, (int(match[1]), int(match[2])))
        if query == "TEMP?":
            # Syntax only: no invented plausibility limit (HARDWARE_ACCEPTANCE.md).
            if not _TEMP.fullmatch(text):
                raise DataValidationError("'dd.d' degrees C expected")
            return Reply(query, raw, Decimal(text), "internal temperature, deg C")
    except DataValidationError as exc:
        raise ReplyError(str(exc), query, raw) from exc
    raise ReplyError("no parser for this query type", query, raw)
