"""Dependency-free SVG plots for the report (WP-06). Deterministic text output.

The x axis of a reading plot is the chronological sample index (AUTO sampling: no per-sample
time stamp). Every plot carries the data source label (SIMULATION / INSTRUMENT) in its title,
so a plot copied out of the report cannot lose it.
"""

from html import escape
from .analysis import subblocks
from .report import SUBBLOCK, histogram

W, H = 760, 360
L, R, T, B = 90, 45, 46, 50


def _axis(lo: float, hi: float) -> tuple[float, float]:
    if hi == lo:
        pad = abs(lo) * 1e-6 or 1e-9
        return lo - pad, hi + pad
    pad = (hi - lo) * 0.05
    return lo - pad, hi + pad


def _frame(title: str, source: str, xlabel: str, ylabel: str, body: list[str],
           xticks: list[tuple[float, str]], yticks: list[tuple[float, str]]) -> str:
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" '
           f'viewBox="0 0 {W} {H}" font-family="sans-serif" font-size="11">',
           f'<rect width="{W}" height="{H}" fill="white"/>',
           f'<text x="{W / 2}" y="18" text-anchor="middle" font-size="13">{escape(title)}</text>',
           f'<text x="{W / 2}" y="34" text-anchor="middle" font-size="11" '
           f'fill="{"#b00000" if source.startswith("SIM") else "#333"}">{escape(source)}</text>',
           f'<rect x="{L}" y="{T}" width="{W - L - R}" height="{H - T - B}" fill="none" '
           'stroke="#444"/>']
    for x, text in xticks:
        out.append(f'<line x1="{x:.1f}" y1="{H - B}" x2="{x:.1f}" y2="{H - B + 4}" stroke="#444"/>'
                   f'<text x="{x:.1f}" y="{H - B + 16}" text-anchor="middle">{escape(text)}</text>')
    for y, text in yticks:
        out.append(f'<line x1="{L - 4}" y1="{y:.1f}" x2="{L}" y2="{y:.1f}" stroke="#444"/>'
                   f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" stroke="#eee"/>'
                   f'<text x="{L - 6}" y="{y + 4:.1f}" text-anchor="end">{escape(text)}</text>')
    out.append(f'<text x="{(L + W - R) / 2}" y="{H - 12}" text-anchor="middle">'
               f'{escape(xlabel)}</text>')
    out.append(f'<text x="14" y="{(T + H - B) / 2}" text-anchor="middle" '
               f'transform="rotate(-90 14 {(T + H - B) / 2})">{escape(ylabel)}</text>')
    return "\n".join(out + body + ["</svg>"]) + "\n"


def _scales(xlo, xhi, ylo, yhi):
    def sx(x):
        return L + (x - xlo) / (xhi - xlo) * (W - L - R)

    def sy(y):
        return H - B - (y - ylo) / (yhi - ylo) * (H - T - B)
    return sx, sy


def _ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    return [lo + (hi - lo) * i / (count - 1) for i in range(count)]


def _int_ticks(lo: float, hi: float, count: int = 5) -> list[int]:
    """Whole-number ticks (sample index, counts): the label sits at its own value."""
    return sorted({round(v) for v in _ticks(lo, hi, count) if lo <= round(v) <= hi})


def index_plot(values: list[float], title: str, source: str, unit: str) -> str:
    """Readings vs chronological sample index, with the 10-sample block means as steps."""
    n = len(values)
    ylo, yhi = _axis(min(values), max(values))
    sx, sy = _scales(0, max(n - 1, 1), ylo, yhi)
    points = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(values))
    body = [f'<polyline points="{points}" fill="none" stroke="#1f5fbf" stroke-width="1"/>']
    body += [f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="1.8" fill="#1f5fbf"/>'
             for i, v in enumerate(values)]
    if n % SUBBLOCK == 0 and n >= 2 * SUBBLOCK:
        for k, s in enumerate(subblocks(tuple(values), SUBBLOCK)):
            x0, x1 = sx(k * SUBBLOCK), sx(k * SUBBLOCK + SUBBLOCK - 1)
            body.append(f'<line x1="{x0:.1f}" y1="{sy(s.mean):.1f}" x2="{x1:.1f}" '
                        f'y2="{sy(s.mean):.1f}" stroke="#d07000" stroke-width="2.5"/>')
        body.append(f'<text x="{W - R - 4}" y="{T + 14}" text-anchor="end" fill="#d07000">'
                    '— 10 mintás alblokk-átlag</text>')
    xt = [(sx(x), f"{x}") for x in _int_ticks(0, max(n - 1, 1))]
    yt = [(sy(y), f"{y:.9g}") for y in _ticks(ylo, yhi)]
    return _frame(title, source, "mintasorszám (időrend; nincs mintánkénti időbélyeg)",
                  unit, body, xt, yt)


def histogram_plot(values: list[float], title: str, source: str, unit: str) -> str:
    bins = histogram(values)
    lo, hi = _axis(bins[0][0], bins[-1][1])
    top = max(c for _, _, c in bins)
    sx, sy = _scales(lo, hi, 0, top * 1.1)
    body = []
    for a, b, c in bins:
        if a == b:  # one bin per distinct level (quantized data): a narrow bar at the level
            width = max((W - L - R) / (len(bins) * 3), 2)
            x = sx(a) - width / 2
        else:
            x, width = sx(a), max(sx(b) - sx(a) - 1, 1)
        body.append(f'<rect x="{x:.1f}" y="{sy(c):.1f}" width="{width:.1f}" '
                    f'height="{sy(0) - sy(c):.1f}" fill="#5a8fd8" stroke="#1f5fbf"/>')
    xt = [(sx(x), f"{x:.9g}") for x in _ticks(lo, hi, 4)]
    yt = [(sy(y), f"{y}") for y in _int_ticks(0, top * 1.1)]
    return _frame(title, source, unit, "darab", body, xt, yt)
