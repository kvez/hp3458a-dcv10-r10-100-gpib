"""Plot view for one block in the wizard (WP-06): readings vs chronological sample index with
the 10-sample block means, and a histogram. The data source label is part of every title.
Uses the analysis of report.py, so the GUI and the report show the same numbers."""

from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget
from ..analysis import subblocks
from ..report import SUBBLOCK, analyze_block, histogram

try:
    import pyqtgraph as pg
except ImportError:  # optional GUI dependency: the wizard still works without plots
    pg = None


class BlockPlotView(QWidget):
    def __init__(self, simulation: bool) -> None:
        super().__init__()
        self.source = "SZIMULÁCIÓ — nem műszermérés" if simulation else "ÉLŐ MŰSZER"
        layout = QVBoxLayout(self)
        self.caption = QLabel("Grafikon: válassz egy blokkot az eredménytáblában")
        self.caption.setWordWrap(True)
        layout.addWidget(self.caption)
        self.index_plot = self.hist_plot = None
        if pg is None:
            layout.addWidget(QLabel("A grafikonhoz a pyqtgraph csomag szükséges "
                                    "(pip install .[gui]); a jelentés SVG-grafikonjai ettől "
                                    "függetlenül elkészülnek."))
            return
        row = QHBoxLayout()
        self.index_plot = pg.PlotWidget(background="w")
        self.index_plot.setLabel("bottom", "mintasorszám (időrend)")
        self.hist_plot = pg.PlotWidget(background="w")
        self.hist_plot.setLabel("left", "darab")
        for plot in (self.index_plot, self.hist_plot):
            plot.setMinimumSize(120, 180)  # let the layout shrink them; never overlap
        row.addWidget(self.index_plot, 3)
        row.addWidget(self.hist_plot, 2)
        layout.addLayout(row)
        self.shown: str | None = None

    def show_block(self, test_id: str, state: str, values: list[float], unit: str) -> None:
        title = f"{test_id} — {state} — N={len(values)} — {self.source}"
        if len(values) < 2:
            self.caption.setText(f"{title}: kevesebb mint 2 minta, nincs grafikon")
            return
        analysis = analyze_block(values)
        self.caption.setText(
            f"{title}; x: mintasorszám, nincs mintánkénti időbélyeg; "
            f"s = {analysis.stats.sdev:.4g} {unit}; indikátorok: "
            + ("; ".join(analysis.indicators) or "nincs"))
        self.shown = test_id
        if pg is None:
            return
        self.index_plot.clear()
        self.index_plot.setTitle(test_id)  # full label with the source: caption
        self.index_plot.setLabel("left", unit)
        x = list(range(len(values)))
        self.index_plot.plot(x, values, pen=pg.mkPen("#1f5fbf"), symbol="o", symbolSize=4,
                             symbolBrush="#1f5fbf")
        if len(values) % SUBBLOCK == 0 and len(values) >= 2 * SUBBLOCK:
            for k, s in enumerate(subblocks(tuple(values), SUBBLOCK)):
                self.index_plot.plot([k * SUBBLOCK, k * SUBBLOCK + SUBBLOCK - 1], [s.mean] * 2,
                                     pen=pg.mkPen("#d07000", width=3))
        self.index_plot.autoRange()
        self.hist_plot.clear()
        self.hist_plot.setTitle("hisztogram")
        self.hist_plot.setLabel("bottom", unit)
        bins = histogram(values)
        centers = [(a + b) / 2 for a, b, _ in bins]
        widths = [(b - a) or (max(values) - min(values) or 1.0) / (len(bins) * 3)
                  for a, b, _ in bins]
        self.hist_plot.addItem(pg.BarGraphItem(x=centers, height=[c for _, _, c in bins],
                                               width=widths, brush="#5a8fd8"))
        self.hist_plot.autoRange()
