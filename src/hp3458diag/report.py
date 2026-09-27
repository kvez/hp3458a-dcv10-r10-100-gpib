"""Diagnostic analysis and the self-contained Markdown report (WP-06). Files only.

Everything here is an *indicator* with its method stated next to it (DIAGNOSTIC_LOGIC.md):
no automatic good/bad verdict on the instrument, nothing is detrended or dropped silently,
the sample axis is the chronological sample index (AUTO sampling has no measured per-sample
time stamp; the block start/end are host times, the mean interval is an estimate).
"""

import base64
import math
import re
from dataclasses import dataclass, field
from statistics import fmean
from typing import Any
from .analysis import Statistics, nplc_ratios, subblocks, summarize
from .domain import canonical_dut_id, canonical_test_id, default_plan, dut_label

SQRT10 = math.sqrt(10)
# Documented heuristic bands (not limits, not a verdict): see `nplc_indicator`.
WHITE_BAND = (SQRT10 / 1.5, SQRT10 * 1.5)
FLOOR_RATIO = 1.5
SUBBLOCK = 10


# --- block extraction ---------------------------------------------------------------------
def _raw_text(raw: Any) -> str | None:
    if not isinstance(raw, dict) or "base64" not in raw:
        return None
    return base64.b64decode(raw["base64"]).decode("ascii", "replace").strip()


@dataclass
class BlockView:
    file: str
    test_id: str
    dut_id: str | None
    mode: str | None
    range_value: float | None
    nplc: int | None
    ocomp: bool | None
    delay_s: float | None
    nominal: float | None
    n_planned: int | None
    unit: str | None
    state: str | None
    validation: str | None
    values: list[float]
    raw_values: list[str]
    simulation: bool | None
    block_id: str | None = None
    started_utc: str | None = None
    ended_utc: str | None = None
    acquisition_s: float | None = None
    temp_start: str | None = None
    temp_end: str | None = None
    settle_s: float | None = None
    memory_read_status: str | None = None
    memory_read_attempts: int = 0
    read_kind: str | None = None
    stat_crosscheck: str | None = None
    stat_errors: list[str] = field(default_factory=list)
    dmm_statistics: dict[str, str] | None = None
    errors: list[str] = field(default_factory=list)
    retry_of: str | None = None
    recovered: bool = False
    stored_test_id: str | None = None  # as saved on disk (before D31: E-P321-...)

    @property
    def partial(self) -> bool:
        return self.n_planned is not None and len(self.values) < self.n_planned

    @property
    def usable(self) -> bool:
        """Complete and consistently read: the only blocks used in comparisons."""
        return self.validation == "VALIDATED" and not self.partial and len(self.values) >= 2


def block_view(name: str, block: dict[str, Any]) -> BlockView:
    point = block.get("test_point") or {}
    outcome = block.get("outcome") or {}
    result = (block.get("result") or outcome.get("result") or outcome.get("partial_result")
              or block.get("block_result") or {})
    raw_values = [str(v) for v in result.get("chronological_values") or ()]
    reads = result.get("memory_reads") or {}
    mode = point.get("mode")
    state = outcome.get("state") or ("RETRY_READ" if block.get("retry_of") else None)
    stored_id = point.get("test_id") or result.get("test_id") or "?"
    return BlockView(
        file=name, test_id=canonical_test_id(stored_id), stored_test_id=stored_id,
        dut_id=canonical_dut_id(point.get("dut_id")), mode=mode, range_value=point.get("range_value"),
        nplc=point.get("nplc"), ocomp=point.get("ocomp"), delay_s=point.get("delay_s"),
        nominal=point.get("nominal"), n_planned=point.get("n"),
        unit="V" if mode == "DCV" else "ohm" if mode else None, state=state,
        validation=result.get("status"), values=[float(v) for v in raw_values],
        raw_values=raw_values, simulation=block.get("simulation"),
        block_id=outcome.get("block_id") or block.get("block_uuid"),
        started_utc=outcome.get("started_utc"), ended_utc=outcome.get("ended_utc"),
        acquisition_s=outcome.get("acquisition_s"),
        temp_start=_raw_text(outcome.get("temp_start_raw")),
        temp_end=_raw_text(outcome.get("temp_end_raw")),
        settle_s=outcome.get("settle_actual_s"), memory_read_status=reads.get("status"),
        memory_read_attempts=len(reads.get("attempts") or ()),
        read_kind=result.get("read_kind"), stat_crosscheck=result.get("stat_crosscheck"),
        stat_errors=[str(e) for e in result.get("errors") or ()],
        dmm_statistics=result.get("dmm_statistics"),
        errors=[str(e) for e in outcome.get("errors") or ()],
        retry_of=block.get("retry_of"), recovered=bool(block.get("recovered")))


# --- per-block indicators -----------------------------------------------------------------
@dataclass(frozen=True)
class Quantization:
    unique_levels: int
    min_step: float | None      # smallest positive difference between distinct values
    modal_step: float | None    # most frequent positive difference between sorted levels
    sdev_in_steps: float | None


@dataclass(frozen=True)
class BlockAnalysis:
    stats: Statistics
    sub: list[Statistics] | None       # 10 x 10; None when N is not a multiple of 10
    sub_note: str | None
    sdev_of_sub_means: float | None
    mean_of_sub_sdevs: float | None
    slope_per_sample: float
    trend_change: float                # slope * (N - 1): least-squares change over the block
    lag1: float | None
    quant: Quantization
    indicators: list[str]


def quantization(values: list[float], sdev: float) -> Quantization:
    levels = sorted(set(values))
    steps = [b - a for a, b in zip(levels, levels[1:]) if b > a]
    if not steps:
        return Quantization(len(levels), None, None, None)
    rounded = [float(f"{s:.6g}") for s in steps]
    modal = max(set(rounded), key=lambda s: (rounded.count(s), -s))
    smallest = min(steps)
    return Quantization(len(levels), smallest, modal, sdev / smallest if smallest else None)


def _lag1(values: list[float]) -> float | None:
    mean = fmean(values)
    dev = [v - mean for v in values]
    denom = sum(d * d for d in dev)
    if denom == 0:
        return None
    return sum(a * b for a, b in zip(dev, dev[1:])) / denom


def _slope(values: list[float]) -> float:
    n = len(values)
    x_mean = (n - 1) / 2
    sxx = sum((i - x_mean) ** 2 for i in range(n))
    mean = fmean(values)
    return sum((i - x_mean) * (v - mean) for i, v in enumerate(values)) / sxx


def analyze_block(values: list[float], nominal: float | None = None) -> BlockAnalysis:
    stats = summarize(tuple(values), nominal)
    sub, note, sd_means, mean_sds = None, None, None, None
    if len(values) % SUBBLOCK == 0 and len(values) >= 2 * SUBBLOCK:
        sub = subblocks(tuple(values), SUBBLOCK)
        means = [s.mean for s in sub]
        sd_means = summarize(tuple(means)).sdev
        mean_sds = fmean(s.sdev for s in sub)
    else:
        note = (f"N={len(values)} nem osztható {SUBBLOCK}-zel vagy túl rövid: nincs alblokk-"
                "bontás (minta nem dobható el)")
    slope = _slope(values)
    lag1 = _lag1(values)
    quant = quantization(values, stats.sdev)
    indicators: list[str] = []
    if sd_means is not None and mean_sds:
        if stats.sdev > 1.2 * mean_sds and sd_means > 2 * mean_sds / math.sqrt(SUBBLOCK):
            indicators.append(
                "Hosszú távú drift-hozzájárulás indikátor (Long-term drift contribution): a teljes "
                "s nagyobb az alblokk-s-ek átlagánál, és az alblokk-átlagok szórása több mint "
                "kétszerese a független mintákra várható s/√10-nek")
    if lag1 is not None and abs(lag1) > 2 / math.sqrt(len(values)):
        kind = ("pozitív (lassú változás / drift)" if lag1 > 0 else
                "negatív (váltakozó, lehetséges periodikus komponens)")
        indicators.append(f"Szomszédos minták korrelációja {kind}: r1 = {lag1:.3f}, "
                          f"a független mintákra várható sáv ±{2 / math.sqrt(len(values)):.3f}")
    if quant.sdev_in_steps is not None and (quant.unique_levels <= 10 or
                                            quant.sdev_in_steps < 2):
        indicators.append(
            f"Kvantálási padló közelében: {quant.unique_levels} különböző érték, legkisebb "
            f"lépés {quant.min_step:.6g}, s = {quant.sdev_in_steps:.2f} lépés")
    return BlockAnalysis(stats, sub, note, sd_means, mean_sds, slope, slope * (len(values) - 1),
                         lag1, quant, indicators)


def histogram(values: list[float], max_bins: int = 20) -> list[tuple[float, float, int]]:
    """Bins (low, high, count). Few distinct levels (quantized data): one bin per level."""
    levels = sorted(set(values))
    if len(levels) <= max_bins:
        return [(v, v, values.count(v)) for v in levels]
    low, high = levels[0], levels[-1]
    width = (high - low) / max_bins
    counts = [0] * max_bins
    for v in values:
        counts[min(int((v - low) / width), max_bins - 1)] += 1
    return [(low + i * width, low + (i + 1) * width, c) for i, c in enumerate(counts)]


# --- cross-block indicators ---------------------------------------------------------------
def series_key(test_id: str) -> str:
    return re.sub(r"-NPLC\d+$", "", test_id)


def nplc_indicator(s1: float, s10: float, s100: float) -> tuple[dict, str]:
    """Heuristic label from the original task (section 8); band x/÷1.5 around √10."""
    ratios = nplc_ratios(s1, s10, s100)
    r1, r2 = ratios["1_to_10"], ratios["10_to_100"]
    if r1 is None or r2 is None:
        return ratios, "nem értelmezhető (nulla szórás)"
    labels = []
    if WHITE_BAND[0] <= r1 <= WHITE_BAND[1] and WHITE_BAND[0] <= r2 <= WHITE_BAND[1]:
        labels.append("approximately white-noise-like (közel fehérzaj-szerű)")
    if min(r1, r2) < WHITE_BAND[0]:
        labels.append("noise reduction weaker than sqrt(N) (a zajcsökkenés gyengébb √10-nél)")
    if r2 < FLOOR_RATIO:
        labels.append("possible flicker/drift floor (lehetséges 1/f- vagy driftpadló)")
    if max(r1, r2) > WHITE_BAND[1]:
        labels.append("possible periodic/systematic component (lehetséges periodikus/"
                      "szisztematikus komponens)")
    return ratios, "; ".join(labels)


@dataclass(frozen=True)
class Comparison:
    title: str
    rows: list[tuple[str, BlockView]]
    suggests: str
    not_implied: str
    next_control: str


COMPARISONS = (
    ("C közvetlen Kelvin-short vs D kábelvégi short", ("C", "D"),
     "Kábel, árnyékolás, kontaktus, EMI vagy termikus különbség",
     "Nem biztos, hogy csak a kábel az ok (közben idő és hőmérséklet is változott)",
     "C–D–C sorrend; kábelmozgatás külön jegyzett teszt"),
    ("OCOMP ON vs OFF (F)", ("F-ON", "F-OFF", "F-RETURN-ON"),
     "Más mérési algoritmus/időzítés; offsethatás",
     "Az OFF nem feltétlen pontosabb; OFF diagnosztikai kontroll, nem végső érték",
     "ON–OFF–ON és az átlag driftje"),
    ("OCOMP × DELAY a 100 Ω-on (F, D34)", ("F-R100-ON-D1", "F-R100-ON-D0", "F-R100-OFF-D1",
                                           "F-R100-OFF-D0", "F-R100-RETURN-ON"),
     "ON−OFF: soros offset (termofeszültség); ON D1−D0: beállás az áramkapcsolás után "
     "(OCOMP ON mellett a DELAY minden kapcsolás után hat, C26)",
     "Az OFF diagnosztikai kontroll, nem végső érték; OFF mellett a DELAY csak az első "
     "mintát érinti",
     "ON–OFF–ON és az átlag driftje; hosszabb DELAY, ha D1 és D0 eltér"),
    ("DCV rövidzár: 100 mV vs 10 V méréshatár (A, G)", ("A", "G-DCV10-SHORT"),
     "A DMM saját zaja/offsete a két méréshatáron; a G a B-sor DMM-része (D35)",
     "A rövidzár nem választja külön az ADC-t és a front-endet",
     "B-sor zaja − G zaja (független zajnál négyzetesen)"),
)


def comparisons(blocks: list[BlockView]) -> list[Comparison]:
    latest = latest_usable(blocks)
    out = []
    for title, keys, suggests, not_implied, nxt in COMPARISONS:
        rows = [(tid, b) for tid, b in latest.items() if series_key(tid) in keys or tid in keys]
        if len({series_key(t) for t, _ in rows}) >= 2:
            # time order: the elapsed time runs from the first to the last measured block
            rows.sort(key=lambda row: (row[1].started_utc or "", row[0]))
            out.append(Comparison(title, rows, suggests, not_implied, nxt))
    return out


def latest_usable(blocks: list[BlockView]) -> dict[str, BlockView]:
    """The last complete VALIDATED block per test ID (block files are in save order)."""
    latest: dict[str, BlockView] = {}
    for b in blocks:
        if b.usable:
            latest[b.test_id] = b
    return latest


def coverage(blocks: list[BlockView], planned: list[str]) -> list[tuple[str, str]]:
    status = []
    by_id: dict[str, list[BlockView]] = {}
    for b in blocks:
        by_id.setdefault(b.test_id, []).append(b)
    for tid in planned + [t for t in by_id if t not in planned]:
        items = by_id.get(tid)
        if not items:
            status.append((tid, "NINCS MÉRVE"))
            continue
        parts = []
        for b in items:
            text = b.validation or b.state or "?"
            if b.partial:
                text += f" (részleges {len(b.values)}/{b.n_planned})"
            if b.state and b.state != b.validation:
                text = f"{b.state}/{text}"
            parts.append(text)
        status.append((tid, ", ".join(parts)))
    return status


# --- Markdown -----------------------------------------------------------------------------
def _g(value: Any, digits: int = 9) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def _minutes_between(a: str | None, b: str | None) -> str:
    from datetime import datetime
    if not a or not b:
        return "—"
    delta = datetime.fromisoformat(b) - datetime.fromisoformat(a)
    return f"{delta.total_seconds() / 60:.1f} perc"


def _block_section(b: BlockView, plot_prefix: str | None) -> list[str]:
    renamed = (f" (a fájlban: {b.stored_test_id})" if b.stored_test_id and
               b.stored_test_id != b.test_id else "")
    lines = [f"### {b.test_id}{renamed} — {b.file}", ""]
    config = (f"{b.mode} {_g(b.range_value)}, NPLC {b.nplc}, N {b.n_planned}, AZERO ON, "
              f"OCOMP {'ON' if b.ocomp else 'OFF'}, DELAY {_g(b.delay_s)} s, DUT {dut_label(b.dut_id)}, "
              f"névleges {_g(b.nominal)} {b.unit}")
    lines += [f"- Beállítás: {config}",
              f"- Állapot: {b.state or '—'}; validálás: {b.validation or '—'}; "
              f"minták: {len(b.values)}" + (" (RÉSZLEGES)" if b.partial else ""),
              f"- Idő (host, mért): blokk kezdete {b.started_utc or '—'}, vége "
              f"{b.ended_utc or '—'}; mérés {_g(b.acquisition_s, 6)} s; stabilizálás "
              f"{_g(b.settle_s, 6)} s",
              "- Időtengely: mintasorszám (AUTO mintavétel, nincs mintánkénti időbélyeg)"
              + (f"; becsült átlagos mintaköz {b.acquisition_s / len(b.values):.3f} s "
                 "(BECSLÉS: mérési idő / N)" if b.acquisition_s and b.values else ""),
              f"- TEMP? kezdet/vég: {b.temp_start or '—'} / {b.temp_end or '—'} °C (belső)",
              f"- Memóriaolvasás: {b.memory_read_status or '—'} ({b.memory_read_attempts} "
              f"olvasás, {b.read_kind or '—'}); DMM–PC STAT: {b.stat_crosscheck or '—'}"
              + (f"; eltérés: {', '.join(b.stat_errors)}" if b.stat_errors else "")]
    if b.errors:
        lines.append(f"- Hibák/megjegyzések: {'; '.join(b.errors)}")
    if b.retry_of:
        lines.append(f"- RETRY-olvasás (ugyanaz a memória) — eredeti blokk: {b.retry_of}")
    if len(b.values) < 2:
        return lines + ["", "Kevesebb mint 2 minta: nincs statisztika.", ""]
    a = analyze_block(b.values, b.nominal or None)
    s = a.stats
    lines += ["", "| N | átlag | s (ddof=1) | min | max | P-P | s/átlag relatív (ppm) | "
              "eltérés a névlegestől (ppm) | SEM (csak IID) |", "|---|---|---|---|---|---|---|---|---|",
              f"| {s.n} | {_g(s.mean)} | {_g(s.sdev, 4)} | {_g(s.minimum)} | {_g(s.maximum)} | "
              f"{_g(s.peak_to_peak, 4)} | {_g(s.relative_sdev_ppm, 4)} | "
              f"{_g(s.mean_deviation_ppm, 4)} | {_g(s.sem_iid, 3)} |", ""]
    if a.sub is not None:
        lines += ["10×10 alblokk (időrendben):", "",
                  "| # | átlag | s |", "|---|---|---|"]
        lines += [f"| {i + 1} | {_g(x.mean)} | {_g(x.sdev, 4)} |" for i, x in enumerate(a.sub)]
        lines += ["", f"Alblokk-átlagok szórása: {_g(a.sdev_of_sub_means, 4)}; alblokk-s átlaga: "
                  f"{_g(a.mean_of_sub_sdevs, 4)}; teljes s: {_g(s.sdev, 4)}."]
    else:
        lines.append(a.sub_note)
    lines += [f"Lineáris trend (legkisebb négyzetek, mintasorszám szerint): "
              f"{_g(a.slope_per_sample, 4)} {b.unit}/minta, a blokk alatt {_g(a.trend_change, 4)} "
              f"{b.unit} ({_g(a.trend_change / s.sdev if s.sdev else None, 3)} × szórás). "
              "A statisztika "
              "NINCS trendmentesítve.",
              f"Kvantálás: {a.quant.unique_levels} különböző érték; legkisebb lépés "
              f"{_g(a.quant.min_step, 4)}; leggyakoribb lépés {_g(a.quant.modal_step, 4)}.", ""]
    lines += ["Indikátorok (nem ítélet):"] + ([f"- {i}" for i in a.indicators] or ["- nincs"])
    if plot_prefix:
        lines += ["", f"Grafikonok: `{plot_prefix}-index.svg` (mérés a mintasorszám szerint, "
                  f"10-es alblokk-átlagokkal), `{plot_prefix}-hist.svg` (hisztogram)"]
    return lines + [""]


def _summary_line(b: BlockView | None) -> list[str]:
    if b is None:
        return ["nincs érvényes teljes blokk"]
    s = summarize(tuple(b.values))
    return [f"mean={_g(s.mean)}", f"sdev={_g(s.sdev, 6)}", f"min={_g(s.minimum)}",
            f"max={_g(s.maximum)}"]


PLAN_NPLC = {p.test_id: p.nplc for p in default_plan(True)}
SUMMARY_GROUPS = (("A1 DCV 100mV SHORT", "A"), ("C OHMF10 DIRECT SHORT", "C"),
                  ("D OHMF10 CABLE-END SHORT", "D"), ("E 0.01 OHM (P310)", "E-R001"),
                  ("E 0.1 OHM (P321)", "E-R01"), ("E 1 OHM (P321)", "E-R1"), ("E 10 OHM (P321)", "E-R10"),
                  ("E 100 OHM (P331)", "E-R100"),
                  ("F OCOMP ON", "F-ON"), ("F OCOMP OFF", "F-OFF"),
                  ("F R100 OCOMP ON DELAY 1", "F-R100-ON-D1"),
                  ("F R100 OCOMP ON DELAY 0", "F-R100-ON-D0"),
                  ("F R100 OCOMP OFF DELAY 1", "F-R100-OFF-D1"),
                  ("F R100 OCOMP OFF DELAY 0", "F-R100-OFF-D0"),
                  ("A2 DCV 10V SHORT", "G-DCV10-SHORT"), ("DCV 5V", "B-5V"),
                  ("DCV 7.05V", "B-7V05"), ("DCV 10V", "B-10V"))


def build_report(metadata: dict[str, Any], blocks: list[BlockView],
                 plot_names: dict[str, str] | None = None) -> str:
    plot_names = plot_names or {}
    simulated = any(b.simulation for b in blocks) or bool(metadata.get("simulation"))
    label = ("**SZIMULÁCIÓ — nem műszermérés. Az adatok nem hardverbizonyítékok.**" if simulated
             else "Valódi műszeres adatok (a blokkfájlok nyers bájtjaival).")
    planned = [canonical_test_id(t) for t in
               metadata.get("test_ids") or [p.test_id for p in default_plan(False)]]
    latest = latest_usable(blocks)
    temps = [b for b in blocks if b.temp_start or b.temp_end]
    lines = [f"# HP 3458A diagnosztikai jelentés — session {metadata.get('session_uuid')}", "",
             label, "",
             "Nincs automatikus jó/hibás ítélet. VALIDATED = a megvalósított adatellenőrzések "
             "teljesültek (kétszeri egyező memóriaolvasás, formátum, DMM–PC STAT), nem a műszer "
             "minősítése. A névleges érték nem kalibrált referencia; nincs TCR-korrekció.", "",
             "## Munkamenet", "",
             f"- Létrehozva: {metadata.get('created_utc')}; lezárva: {metadata.get('closed_utc')}; "
             f"állapot: {metadata.get('lifecycle')} / {metadata.get('status')}",
             f"- Szoftver: {metadata.get('software_version')}; típus: {metadata.get('kind')}",
             f"- Bemelegedés (≥ 4 h, kezelő): {metadata.get('warm_up_confirmed')}; utolsó ACAL "
             f"(kezelő): {metadata.get('last_acal_operator_note') or '—'}",
             f"- Megjegyzés: {metadata.get('operator_note') or '—'}", "",
             "## Lefedettség (terv vs mért)", "", "| Pont | Blokk(ok) |", "|---|---|"]
    lines += [f"| {tid} | {text} |" for tid, text in coverage(blocks, planned)]
    lines += ["", "## Blokkok", ""]
    for b in blocks:
        lines += _block_section(b, plot_names.get(b.file))
    lines += ["## NPLC-arányok", "",
              "Fehér, független zajnál s1/s10 ≈ s10/s100 ≈ √10 = 3.162. A címkék heurisztikus "
              f"sávból (√10 ×/÷ 1.5; s10/s100 < {FLOOR_RATIO} → padló) jönnek, nem határértékek. "
              "Csak teljes VALIDATED blokkok; pontonként a legutolsó.", "",
              "| Sorozat | s1 | s10 | s100 | s1/s10 | s10/s100 | Indikátor |",
              "|---|---|---|---|---|---|---|"]
    groups: dict[str, dict[int, BlockView]] = {}
    for tid, b in latest.items():
        if b.nplc is not None and tid != series_key(tid):
            groups.setdefault(series_key(tid), {})[b.nplc] = b
    shown = False
    for key, by_nplc in sorted(groups.items()):
        if {1, 10, 100} <= set(by_nplc):
            sd = [summarize(tuple(by_nplc[n].values)).sdev for n in (1, 10, 100)]
            ratios, text = nplc_indicator(*sd)
            lines.append(f"| {key} | {_g(sd[0], 4)} | {_g(sd[1], 4)} | {_g(sd[2], 4)} | "
                         f"{_g(ratios['1_to_10'], 4)} | {_g(ratios['10_to_100'], 4)} | {text} |")
            shown = True
    if not shown:
        lines.append("| — | nincs teljes NPLC 1/10/100 hármas | | | | | |")
    lines += ["", "## Összehasonlítások (feltételekkel)", ""]
    comps = comparisons(blocks)
    for c in comps:
        lines += [f"### {c.title}", "", "| Pont | átlag | s | kezdet (UTC) | TEMP? kezdet |",
                  "|---|---|---|---|---|"]
        for tid, b in c.rows:
            s = summarize(tuple(b.values))
            lines.append(f"| {tid} | {_g(s.mean)} | {_g(s.sdev, 4)} | {b.started_utc or '—'} | "
                         f"{b.temp_start or '—'} |")
        first, last = c.rows[0][1], c.rows[-1][1]
        lines += ["", f"- Eltelt idő az első és utolsó blokk között: "
                  f"{_minutes_between(first.started_utc, last.started_utc)}",
                  f"- Mire utalhat: {c.suggests}", f"- Mi nem következik belőle: {c.not_implied}",
                  f"- Következő kontroll: {c.next_control}", ""]
    if not comps:
        lines += ["Nincs összehasonlítható pár (C/D, F ON/OFF).", ""]
    resistors = [b for tid, b in sorted(latest.items(), key=lambda x: x[1].nominal or 0)
                 if tid.startswith("E-") and b.nominal]
    if resistors:
        lines += ["### Ellenállás-sorozat (E): abszolút és relatív szórás", "",
                  "Közel állandó sR → additív feszültségzaj vagy felbontási padló lehet; közel "
                  "állandó sR/R → relatív (áramforrás/referencia/DUT) hatás lehet. Egyik sem "
                  "lokalizálja az okot; eltérő DUT, range és önmelegedés.", "",
                  "| Pont | R névleges | sR | sR/R (ppm) |", "|---|---|---|---|"]
        for b in resistors:
            s = summarize(tuple(b.values), b.nominal)
            lines.append(f"| {b.test_id} | {_g(b.nominal)} | {_g(s.sdev, 4)} | "
                         f"{_g(s.relative_sdev_ppm, 4)} |")
        lines.append("")
    lines += ["## Validálás", ""]
    retries = sum(1 for b in blocks if b.read_kind == "RETRY" or b.retry_of)
    thirds = sum(1 for b in blocks if b.memory_read_attempts > 2)
    invalid = sum(1 for b in blocks if b.validation != "VALIDATED")
    read = [b for b in blocks if b.memory_read_status]
    identical = ("N/A" if not read else
                 "YES" if all(b.memory_read_status == "EXACT" for b in read) else "NO")
    checked = [b for b in blocks if b.stat_crosscheck and b.validation]
    agree = ("N/A" if not checked else
             "YES" if all(not b.stat_errors and b.validation == "VALIDATED" for b in checked)
             else "NO")
    lines += [f"- RETRY-olvasások: {retries}; harmadik olvasás kellett: {thirds} blokknál",
              f"- Nem VALIDATED blokkok: {invalid}",
              f"- Minden memóriaolvasás első két olvasása egyezett: {identical}",
              f"- PC vs DMM STAT egyezés: {agree}", "",
              "A kétszeri olvasás nem CRC: azonos ismételt hiba vagy azonos statisztikájú "
              "permutáció nem zárható ki (C08).", "", "## HP3458A DIAGNOSTIC SUMMARY", "", "```",
              "HP3458A DIAGNOSTIC SUMMARY",
              "DATA SOURCE: " + ("SIMULATION (not instrument data)" if simulated else "INSTRUMENT"),
              "", f"Instrument ID: {metadata.get('instrument_id') or '—'}",
              f"Date: {metadata.get('created_utc')}",
              f"Warm-up: {metadata.get('warm_up_confirmed')}",
              f"Last ACAL: {metadata.get('last_acal_operator_note') or '—'}",
              f"Start TEMP: {temps[0].temp_start if temps else '—'}",
              f"End TEMP: {temps[-1].temp_end if temps else '—'}", "", "TEST RESULTS", ""]
    for title, key in SUMMARY_GROUPS:
        members = {tid: b for tid, b in latest.items() if series_key(tid) == key or tid == key}
        planned_here = [t for t in planned if series_key(t) == key or t == key]
        if not members and not planned_here:
            continue
        lines.append(title)
        for tid in sorted(set(planned_here) | set(members),
                          key=lambda t: (PLAN_NPLC.get(t, 0), t)):
            b = members.get(tid)
            head = f"NPLC{PLAN_NPLC[tid]}:" if tid != key and tid in PLAN_NPLC else f"{tid}:"
            lines += [head] + [f"  {x}" for x in _summary_line(b)]
        lines.append("")
    lines += ["VALIDATION:", f"bus retries={retries}", f"invalid blocks={invalid}",
              f"all memory rereads identical={identical}", f"PC vs DMM STAT agreement={agree}",
              "", "OBSERVATIONS:"]
    observations = []
    for b in blocks:
        if b.usable:
            for text in analyze_block(b.values).indicators:
                observations.append(f"{b.test_id}: {text}")
    lines += observations or ["no indicator raised"]
    lines += ["No automatic conclusion that the 3458A is good or faulty.", "```", ""]
    return "\n".join(lines) + "\n"
