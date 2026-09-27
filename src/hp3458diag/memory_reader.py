"""Validate already-completed reading memory. Acquisition belongs to WP-02."""

from dataclasses import dataclass
from decimal import Decimal
from .analysis import Statistics, crosscheck, summarize
from .domain import TestPoint
from .stat_check import resolution_errors, resolution_table
from .transport.base import Transport
from .validation import (MANUAL_P92, Consensus, ReadingProfile, parse_readings, parse_scalar,
                         read_consistent)


STAT_CROSSCHECKS = ("absolute_relative", "dmm_half_quantum")


@dataclass(frozen=True)
class BlockResult:
    test_id: str
    status: str
    chronological_values: tuple[Decimal, ...]
    pc_statistics: Statistics | None
    dmm_statistics: dict[str, Decimal]
    memory_reads: Consensus
    statistic_reads: dict[str, Consensus]
    errors: tuple[str, ...]
    absolute_tolerance: float
    relative_tolerance: float
    reading_profile: str = MANUAL_P92.name
    stat_crosscheck: str = "absolute_relative"
    resolution_rows: tuple[dict, ...] = ()
    read_kind: str = "COLD"


OVERLOAD_MESSAGE = ("TÚLTERHELÉS: a memóriában ±1E38 (OVLD) minta van — a bemeneti jel a "
                    "méréshatár fölött volt; ellenőrizd a bekötést és a méréshatárt")


def validate_memory(transport: Transport, point: TestPoint, *,
                    absolute_tolerance: float,
                    relative_tolerance: float,
                    profile: ReadingProfile = MANUAL_P92,
                    stat_crosscheck: str = "absolute_relative",
                    read_kind: str = "COLD") -> BlockResult:
    """stat_crosscheck: "absolute_relative" (explicit tolerances, simulator) or
    "dmm_half_quantum" (H05: PC exact value must round to each DMM reply)."""
    if stat_crosscheck not in STAT_CROSSCHECKS:
        raise ValueError(f"stat_crosscheck must be one of {STAT_CROSSCHECKS}")
    if stat_crosscheck == "dmm_half_quantum" and not profile.stat_verified:
        raise ValueError(f"dmm_half_quantum needs a STAT-verified profile, not {profile.name}")
    reads = read_consistent(lambda: transport.query_raw(f"RMEM 1,{point.n}"),
                            lambda raw: parse_readings(raw, point.n, point.physical_bounds,
                                                       profile), read_kind)
    if reads.values is None:
        errors = ("Stored memory has no validated consensus",)
        if reads.attempts and all(a.error == "Overload sentinel" for a in reads.attempts):
            errors += (OVERLOAD_MESSAGE,)  # operator report (L4): say why, in Hungarian
        return BlockResult(point.test_id, reads.status, (), None, {}, reads, {}, errors,
                           absolute_tolerance, relative_tolerance, profile.name,
                           read_kind=read_kind)
    chronological = tuple(reversed(reads.values))
    pc = summarize(chronological, point.nominal)
    stat_reads: dict[str, Consensus] = {}
    dmm: dict[str, Decimal] = {}
    errors: list[str] = []
    try:
        transport.write("MMATH STAT")
    except (TimeoutError, OSError) as exc:
        return BlockResult(point.test_id, "TRANSPORT_ERROR", chronological, pc, {}, reads, {},
                           (str(exc),), absolute_tolerance, relative_tolerance,
                           profile.name, read_kind=read_kind)
    for key in ("MEAN", "SDEV", "LOWER", "UPPER", "NSAMP"):
        check = read_consistent(lambda key=key: transport.query_raw(f"RMATH {key}"),
                                lambda raw: parse_scalar(raw, profile), read_kind)
        stat_reads[key] = check
        if check.values is None:
            errors.append(f"Unvalidated DMM register {key}: {check.status}")
            # Do not send a new query after an incompletely framed reply.
            if check.status == "TRANSPORT_ERROR":
                return BlockResult(point.test_id, "TRANSPORT_ERROR", chronological, pc,
                                   dmm, reads, stat_reads, tuple(errors),
                                   absolute_tolerance, relative_tolerance, profile.name,
                           read_kind=read_kind)
        else:
            dmm[key] = check.values[0]
    rows: list[dict] = []
    if stat_crosscheck == "dmm_half_quantum":
        if not errors:
            rows = resolution_table(chronological, stat_reads, profile)
            errors.extend(resolution_errors(rows))
    else:
        errors.extend(crosscheck(pc, dmm, absolute_tolerance, relative_tolerance))
    return BlockResult(point.test_id, "INVALID" if errors else "VALIDATED", chronological,
                       pc, dmm, reads, stat_reads, tuple(errors),
                       absolute_tolerance, relative_tolerance, profile.name,
                       stat_crosscheck, tuple(rows), read_kind)
