"""Statistical indicators, without automatic instrument pass/fail judgments."""

from dataclasses import dataclass
from decimal import Decimal
from math import isfinite, sqrt
from statistics import fmean, stdev


@dataclass(frozen=True)
class Statistics:
    n: int
    mean: float
    sdev: float
    minimum: float
    maximum: float
    peak_to_peak: float
    sem_iid: float
    relative_sdev_ppm: float | None
    mean_deviation_ppm: float | None


def summarize(values: tuple[Decimal, ...] | tuple[float, ...],
              nominal: float | None = None) -> Statistics:
    data = tuple(float(x) for x in values)
    if len(data) < 2 or not all(isfinite(x) for x in data):
        raise ValueError("At least two finite readings required")
    if nominal is not None and not isfinite(nominal):
        raise ValueError("Nominal must be finite")
    mean = fmean(data)
    sdev = stdev(data)  # ddof = 1; statistics uses a stable variance algorithm.
    ppm = sdev / abs(nominal) * 1e6 if nominal else None
    deviation = (mean - nominal) / abs(nominal) * 1e6 if nominal else None
    return Statistics(len(data), mean, sdev, min(data), max(data), max(data) - min(data),
                      sdev / sqrt(len(data)), ppm, deviation)


def subblocks(values: tuple[Decimal, ...], size: int = 10) -> list[Statistics]:
    if type(size) is not int or size < 2 or len(values) % size:
        raise ValueError("Subblocks must contain >=2 readings and divide the block exactly")
    return [summarize(values[i:i+size]) for i in range(0, len(values), size)]


def nplc_ratios(s1: float, s10: float, s100: float) -> dict[str, float | None]:
    if not all(isfinite(x) and x >= 0 for x in (s1, s10, s100)):
        raise ValueError("Finite nonnegative deviations required")
    return {"1_to_10": s1 / s10 if s10 else None,
            "10_to_100": s10 / s100 if s100 else None,
            "white_noise_reference": sqrt(10)}


def crosscheck(pc: Statistics, dmm: dict[str, Decimal],
               absolute_tolerance: float, relative_tolerance: float) -> list[str]:
    """Explicit tolerance, to be characterized in HIL; never hide a mismatch.

    No `isclose` default 1e-9 absolute tolerance: units vary greatly here.
    """
    if not all(isfinite(x) and x >= 0 for x in (absolute_tolerance, relative_tolerance)):
        raise ValueError("Finite nonnegative tolerances required")
    required = {"NSAMP", "MEAN", "SDEV", "LOWER", "UPPER"}
    if set(dmm) != required or not all(v.is_finite() for v in dmm.values()):
        return ["Missing, unexpected, or non-finite DMM statistic"]
    errors: list[str] = []
    if dmm["NSAMP"] != Decimal(pc.n):
        errors.append("NSAMP")
    if dmm["SDEV"] < 0 or dmm["LOWER"] > dmm["UPPER"]:
        errors.append("Invalid DMM statistic bounds")
    for key, value in (("MEAN", pc.mean), ("SDEV", pc.sdev),
                       ("LOWER", pc.minimum), ("UPPER", pc.maximum)):
        reference = float(dmm[key])
        tolerance = absolute_tolerance + relative_tolerance * max(abs(value), abs(reference))
        if abs(value - reference) > tolerance:
            errors.append(key)
    return errors
