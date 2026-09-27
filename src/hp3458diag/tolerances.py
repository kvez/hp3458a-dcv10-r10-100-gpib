"""Configuration readback tolerances with units and sources (WP-03).

Each rule states why it is what it is; none was widened to hide an observed difference.
Live evidence: NPLC?/DELAY? read back exactly (H04, WP-02); LFREQ? 49.966-49.983 Hz.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class Tolerance:
    quantity: str
    unit: str
    lower_offset: Decimal  # accepted: requested + lower_offset <= readback
    upper_offset: Decimal  # accepted: readback <= requested + upper_offset
    source: str

    def accepts(self, requested: Decimal, readback: Decimal) -> bool:
        return requested + self.lower_offset <= readback <= requested + self.upper_offset


NPLC = Tolerance("integration time", "PLC", Decimal(0), Decimal(0),
                 "NPLC 1/10/100 are exact settings (pp. 204-206); read back exactly (H04)")
DELAY = Tolerance("trigger delay", "s", Decimal("-2E-7"), Decimal("2E-7"),
                  "100 ns resolution (p. 170): two steps of rounding; WP-02 live: exact")


def line_frequency(nominal_hz: int) -> Tolerance:
    """LFREQ LINE measures the mains (pp. 191-192). Accepted band = EN 50160 limits for
    interconnected systems over 100 % of the time: +4 % / -6 % of the nominal frequency."""
    if nominal_hz not in (50, 60):
        raise ValueError("Nominal line frequency must be 50 or 60 Hz")
    nominal = Decimal(nominal_hz)
    return Tolerance("line frequency", "Hz", nominal * Decimal("-0.06"),
                     nominal * Decimal("0.04"), "EN 50160: f_nom +4 %/-6 % (100 % of time)")
