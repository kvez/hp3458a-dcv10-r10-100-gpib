"""DMM STAT registers vs exact PC statistics, judged at the DMM reply's own resolution.

H05 (docs/validation/h05): on REV 9,1 every RMATH MEAN/SDEV/LOWER/UPPER/NSAMP reply of
eight real blocks (DCV 0.1/10, OHMF 10/100, N 3/10/100) equals the exact PC value
rounded to the reply's last displayed digit; SDEV is the N-1 (ddof=1) deviation.
The tolerance is therefore half a unit of that last digit (a tie counts as rounding),
derived from the reply format, never from observed differences.

D32 (L4, 2026-09-25): SDEV is the one exception. On a 5 V source (mean/sdev ~ 8e6) the
SDEV reply missed the correctly rounded value by 0.58 and 1.69 units of its 9th digit:
the DMM's own finite-precision arithmetic, not the data (double RMEM and the other four
registers agreed). SDEV therefore passes within max(half a unit, SDEV_RELATIVE * |SDEV|);
a changed sample still shifts SDEV by far more (~1/N relative for a 1-sigma change).
"""

import base64
from decimal import Decimal, localcontext
from typing import Any
from .validation import Consensus, ReadingProfile, parse_scalar

REGISTERS = ("MEAN", "SDEV", "LOWER", "UPPER", "NSAMP")
SDEV_RELATIVE = Decimal("1E-7")  # D32: bound of the DMM's internal SDEV arithmetic


def exact_statistics(values: tuple[Decimal, ...]) -> dict[str, Decimal]:
    if len(values) < 2:
        raise ValueError("At least two readings required")
    with localcontext() as context:
        context.prec = 60
        n = len(values)
        mean = sum(values) / n
        squares = sum((x - mean) ** 2 for x in values)
        return {"MEAN": +mean, "SDEV": (squares / (n - 1)).sqrt(),
                "SDEV_DDOF0": (squares / n).sqrt(), "LOWER": min(values),
                "UPPER": max(values), "NSAMP": Decimal(n)}


def consensus_raw(check: Consensus) -> bytes | None:
    for attempt in check.attempts:
        if attempt.number in check.selected_attempts and attempt.raw_base64:
            return base64.b64decode(attempt.raw_base64)
    return None


def reply_quantum(raw: bytes) -> Decimal:
    """One unit of the last displayed digit, e.g. b' 650.716370E-09\\r\\n' -> 1E-15."""
    text = raw[:-2].decode("ascii").strip()
    return Decimal(1).scaleb(Decimal(text).as_tuple().exponent)


def resolution_table(values: tuple[Decimal, ...], stat_reads: dict[str, Consensus],
                     profile: ReadingProfile) -> list[dict[str, Any]]:
    pc = exact_statistics(values)
    rows: list[dict[str, Any]] = []
    for key in REGISTERS:
        raw = consensus_raw(stat_reads[key]) if key in stat_reads else None
        if raw is None:
            rows.append({"register": key, "dmm_raw": None, "status": "NO_CONSENSUS",
                         "within_half_quantum": False, "within_tolerance": False})
            continue
        dmm = parse_scalar(raw, profile)[0]
        quantum = reply_quantum(raw)
        diff = pc[key] - dmm
        row = {"register": key, "dmm_raw": raw, "dmm": dmm, "pc_exact": pc[key],
               "quantum": quantum, "diff": diff, "diff_in_quanta": diff / quantum,
               "within_half_quantum": abs(diff) <= quantum / 2,
               "within_one_quantum": abs(diff) <= quantum}
        tolerance = quantum / 2
        if key == "SDEV":
            tolerance = max(tolerance, abs(dmm) * SDEV_RELATIVE)
            pop = pc["SDEV_DDOF0"] - dmm
            row.update({"pc_ddof0": pc["SDEV_DDOF0"], "diff_ddof0_in_quanta": pop / quantum,
                        "ddof0_within_half_quantum": abs(pop) <= quantum / 2})
        row.update({"tolerance": tolerance, "within_tolerance": abs(diff) <= tolerance})
        rows.append(row)
    return rows


def resolution_errors(rows: list[dict[str, Any]]) -> list[str]:
    return [row["register"] for row in rows if not row["within_tolerance"]]
