"""Validated measurement intent; no Qt, VISA, or I/O dependencies."""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class TestPoint:
    test_id: str
    dut_id: str
    mode: str
    range_value: float
    nplc: int
    nominal: float
    n: int = 100
    ocomp: bool = False
    delay_s: float = 0.0
    settling_s: int = 300
    optional: bool = False

    def __post_init__(self) -> None:
        if not self.test_id or not self.dut_id:
            raise ValueError("test_id and dut_id are required")
        ranges = {"DCV": (0.1, 10.0), "OHMF": (10.0, 100.0)}
        if self.mode not in ranges or self.range_value not in ranges[self.mode]:
            raise ValueError("Function/range is outside this diagnostic plan")
        if type(self.n) is not int or not 2 <= self.n <= 100:
            raise ValueError("Foundation supports 2..100 readings per block")
        if type(self.nplc) is not int or self.nplc not in (1, 10, 100):
            raise ValueError("NPLC must be 1, 10, or 100")
        if type(self.ocomp) is not bool or (self.mode == "DCV" and self.ocomp):
            raise ValueError("OCOMP applies to resistance measurements")
        if not isfinite(self.nominal) or abs(self.nominal) > self.range_value * 1.2:
            raise ValueError("Nominal value outside selected range")
        if not isfinite(self.delay_s) or not 0 <= self.delay_s <= 6000:
            raise ValueError("Invalid trigger delay")
        if type(self.settling_s) is not int or not 0 <= self.settling_s <= 86400:
            raise ValueError("Invalid host settling time")

    @property
    def unit(self) -> str:
        return "V" if self.mode == "DCV" else "ohm"

    @property
    def physical_bounds(self) -> tuple[float, float]:
        # Broad full-scale gate, NOT an accuracy or noise pass/fail limit.
        return (-1.2 * self.range_value, 1.2 * self.range_value)


# Operator-given resistor types (2026-09-24); the label disambiguates the three P321 types.
# D31: the 10 ohm DUT is R10 (was P321). Old IDs stay readable: stored sessions keep them on
# disk and `canonical_test_id` / `canonical_dut_id` map them when reading (never rewritten).
DUT_LABELS = {
    "R001": "P310 (0,01 Ω)", "R01": "P321 (0,1 Ω)", "R1": "P321 (1 Ω)",
    "R10": "P321 (10 Ω)", "R100": "P331 (100 Ω)",
    "direct_dcv_short": "DCV rövidzár (INPUT HI–LO)",
    "direct_kelvin_short": "4W Kelvin-rövidzár (közvetlen)",
    "cable_end_kelvin_short": "Kábelvégi 4W rövidzár",
    "source_5V": "5 V forrás", "source_7V05": "7,05 V forrás", "source_10V": "10 V forrás",
}


LEGACY_DUT_IDS = {"P321": "R10"}
_LEGACY_TEST_PREFIXES = (("E-P321-", "E-R10-"), ("G-P321-R100-", "G-R10-R100-"))


def canonical_dut_id(dut_id: str | None) -> str | None:
    return LEGACY_DUT_IDS.get(dut_id, dut_id) if dut_id else dut_id


def canonical_test_id(test_id: str) -> str:
    """'E-P321-NPLC10' (before D31) -> 'E-R10-NPLC10'; other IDs unchanged."""
    for old, new in _LEGACY_TEST_PREFIXES:
        if test_id.startswith(old):
            return new + test_id[len(old):]
    return test_id


def dut_label(dut_id: str | None) -> str:
    if not dut_id:
        return "—"
    canonical = canonical_dut_id(dut_id)
    label = DUT_LABELS.get(canonical)
    if not label:
        return dut_id
    return f"{label} [{canonical}" + (f", korábban {dut_id}]" if canonical != dut_id else "]")


def series_end(points, index: int) -> int:
    """Exclusive end of the wiring series starting at `index` (decision D27): the following
    points on the same DUT with the same function (same wiring) and the same optional flag
    need no new wiring gate. A DUT, wiring or function change always starts a new series."""
    def key(p):
        return (p.dut_id, getattr(p, "mode", ""), p.optional)
    end = index + 1
    while end < len(points) and key(points[end]) == key(points[index]):
        end += 1
    return end


def continuation_settle(previous: TestPoint, point: TestPoint) -> tuple[float, str]:
    """Host settling for a block that continues a wiring series (no rewiring, no DUT
    change; decision D27). Same function and range: no relay switching and the DUT has
    been under the same excitation, so 0 s. A range change switches range relays: the
    point's default settling applies (factory offset tests allow 5 min for the range
    relays, 03458-90017 p. 14, 39)."""
    if (previous.mode, previous.range_value) == (point.mode, point.range_value):
        return 0.0, "series: same DUT, wiring, function and range; no rewiring"
    return float(point.settling_s), "series: range change; default settling for relays"


# D36: default host settling after a wiring gate (operator, 2026-09-26)
SETTLING_S = {"DCV": 300, "OHMF": 900}

# D34: OCOMP x DELAY on the 100 ohm DUT (one more relative digit than the 10 ohm range);
# with OCOMP ON the delay acts after every current switch (C26), with OFF once per trigger
F_BLOCKS = (("ON-D1", True, 1.0), ("ON-D0", True, 0.0), ("OFF-D1", False, 1.0),
            ("OFF-D0", False, 0.0))


def default_plan(include_optional: bool = False) -> tuple[TestPoint, ...]:
    points: list[TestPoint] = []

    def add(prefix: str, dut: str, mode: str, rng: float, nominal: float,
            optional: bool = False) -> None:
        for nplc in (1, 10, 100):
            points.append(TestPoint(
                f"{prefix}-NPLC{nplc}", dut, mode, rng, nplc, nominal,
                ocomp=mode == "OHMF", delay_s=1.0 if mode == "OHMF" else 0.0,
                settling_s=SETTLING_S[mode], optional=optional,
            ))

    add("A", "direct_dcv_short", "DCV", 0.1, 0.0)
    # D35: DMM 10 V range on the same input short, continues the A series (no new gate)
    add("G-DCV10-SHORT", "direct_dcv_short", "DCV", 10.0, 0.0)
    for label, volts in (("5V", 5.0), ("7V05", 7.05), ("10V", 10.0)):
        add(f"B-{label}", f"source_{label}", "DCV", 10.0, volts)
    add("C", "direct_kelvin_short", "OHMF", 10.0, 0.0)
    add("D", "cable_end_kelvin_short", "OHMF", 10.0, 0.0)
    for label, ohms in (("R001", .01), ("R01", .1), ("R1", 1.0),
                        ("R10", 10.0), ("R100", 100.0)):  # one 10 ohm DUT (D30, D31)
        add(f"E-{label}", label, "OHMF", 100.0 if ohms == 100 else 10.0, ohms)
    for label, ocomp, delay in F_BLOCKS:  # continues the E-R100 series (D34)
        points.append(TestPoint(f"F-R100-{label}", "R100", "OHMF", 100.0, 100, 100.0,
                                ocomp=ocomp, delay_s=delay, settling_s=SETTLING_S["OHMF"]))
    if include_optional:
        points.append(TestPoint("F-R100-RETURN-ON", "R100", "OHMF", 100.0, 100, 100.0,
                                ocomp=True, delay_s=1.0, settling_s=SETTLING_S["OHMF"],
                                optional=True))
        add("G-R10-R100", "R10", "OHMF", 100.0, 10.0, True)
    return tuple(points)
