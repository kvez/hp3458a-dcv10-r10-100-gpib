"""Pure command planning. No I/O, no implicit hardware authorization.

Authority: references/vendor/Ag_3458A_UserGuide_en.pdf; evidence IDs in
docs/COMMANDS.md. The live driver must still verify configuration and completion.
"""

import re
from .domain import TestPoint

BASELINE = (
    "PRESET NORM", "TARM HOLD", "MATH OFF", "MMATH OFF", "OFORMAT ASCII",
    "MFORMAT ASCII", "END ON", "AZERO ON", "LFREQ LINE", "NDIG 8", "TRIG AUTO",
)

_ALLOWED = tuple(re.compile(pattern) for pattern in (
    r"PRESET NORM", r"TARM (?:HOLD|SGL)", r"(?:MATH|MMATH) OFF", r"MMATH STAT",
    r"(?:OFORMAT|MFORMAT) ASCII", r"END ON", r"AZERO ON", r"LFREQ LINE",
    r"NDIG 8", r"TRIG AUTO", r"DCV (?:0\.1|10)", r"OHMF (?:10|100)",
    r"NPLC (?:1|10|100)", r"OCOMP (?:ON|OFF)", r"DELAY (?:0|1)",
    r"MEM (?:FIFO|OFF|CONT)",  # CONT keeps stored readings (pp. 197, 230)
    r"NRDGS (?:[2-9]|[1-9][0-9]|100),AUTO",
    r"RMEM 1,(?:[2-9]|[1-9][0-9]|100)",
    r"RMATH (?:MEAN|SDEV|LOWER|UPPER|NSAMP)",
    r"(?:ID|TEMP|MCOUNT|MSIZE|NPLC|LFREQ|AZERO|OCOMP|MEM|MFORMAT|OFORMAT|TARM)\?",
    # Identity/firmware (pp. 186, 228). ERR?/ERRSTR? clear on read (pp. 177-178):
    # never part of a two-read consensus; use instrument.read_error_* only.
    r"REV\?", r"ERR\?", r"ERRSTR\?",
    r"END\?",  # present EOI mode, p. 176
    r"DELAY\?",  # present trigger delay, p. 170
    # Input buffer (pp. 75, 186-187): ON releases the bus at once; completion is then
    # observed by serial-poll READY. Needed for long blocks (H02 evidence).
    r"INBUF (?:ON|OFF)", r"INBUF\?",
    r"TERM\?",  # FRONT/REAR terminal switch readback, p. 254 (metadata only)
))


# Autocal is NOT part of the diagnostic allowlist (CLAUDE.md, R04). It exists only for the
# separate, operator-confirmed WP-08 tool (acal.py), which passes acal=True. DCV and OHMS
# only, never a security code: a secured autocal is reported, not unlocked (p. 157).
_ACAL_ALLOWED = re.compile(r"ACAL (?:DCV|OHMS)")


def check_command(command: str, *, acal: bool = False) -> str:
    if acal:
        if not _ACAL_ALLOWED.fullmatch(command):
            raise ValueError(f"Not an allowed autocal command: {command!r}")
        return command
    if not any(pattern.fullmatch(command) for pattern in _ALLOWED):
        raise ValueError(f"Command outside diagnostic allowlist: {command!r}")
    return command


def configuration(point: TestPoint) -> tuple[str, ...]:
    if point.delay_s not in (0.0, 1.0):
        raise ValueError("Foundation command profile supports DELAY 0/1 only")
    commands = ["TARM HOLD", f"{point.mode} {point.range_value:g}",
                "AZERO ON", f"OCOMP {'ON' if point.ocomp else 'OFF'}",
                f"NPLC {point.nplc}", f"DELAY {point.delay_s:g}"]
    return tuple(check_command(command) for command in commands)


def new_block(point: TestPoint) -> tuple[str, ...]:
    # Only after prior block evidence is saved and pending-memory ownership is released.
    return tuple(check_command(command) for command in (
        "MEM FIFO", f"NRDGS {point.n},AUTO", "TARM SGL"))


def reread(point: TestPoint) -> str:
    return check_command(f"RMEM 1,{point.n}")
