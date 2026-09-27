"""Hungarian lab wizard window (WP-05, docs/GUI_SPEC.md).

The window never touches VISA: it owns a WizardState (gui_state.py) and talks to the
SessionWorker in a QThread through queued signals. Buttons are enabled exactly by
`allowed_actions`. Closing during a block uses the same abort path and waits for the
worker to confirm before the thread is stopped.
"""

from dataclasses import dataclass, replace
from typing import Any, Callable
from PySide6.QtCore import QMetaObject, QThread, Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox,
                               QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
                               QPlainTextEdit, QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)
from .plot_view import BlockPlotView
from ..domain import dut_label
from ..gui_state import (IllegalAction, Phase, WizardState, allowed_actions, reduce,
                         series_end)

BUTTONS = (("new_session", "Új munkamenet"), ("start", "Indítás"),
           ("pause", "Szünet kérése"), ("abort", "Megszakítás"),
           ("retry", "Memória újraolvasása"), ("remeasure", "Újramérés"),
           ("skip_optional", "Opcionális pont kihagyása"), ("recover", "Helyreállítás"),
           ("export", "Export"), ("acal", "ACAL (külön művelet)…"))


@dataclass
class PlanRow:
    test_id: str
    dut_id: str
    text: str
    optional: bool
    mode: str = ""


class WizardWindow(QMainWindow):
    # GUI -> worker (queued across threads)
    sig_open = Signal()
    sig_prepare = Signal(int)
    sig_run = Signal(int, str, float, str)
    sig_continue = Signal(int)
    sig_retry = Signal(int)
    sig_release = Signal()
    sig_recover = Signal(str)
    sig_export = Signal()
    sig_acal = Signal(str)

    def __init__(self, worker_factory: Callable[[], Any], plan: list[PlanRow],
                 simulation: bool, confirm: Callable[[str, str], bool] | None = None) -> None:
        super().__init__()
        self.worker_factory, self.plan, self.simulation = worker_factory, plan, simulation
        self.confirm = confirm or self._ask
        self.state = WizardState()
        self.worker: Any = None
        self.thread: QThread | None = None
        self.gate_info: dict | None = None
        self.closing = False
        self.discard_reason: str | None = None  # read by the worker factory (app.py)
        self.discard_count: int | None = None   # the reading count the operator confirmed
        self._shown_log = 0
        self.setWindowTitle("HP 3458A zajdiagnosztika")
        self._build()
        self._refresh()

    # --- layout ------------------------------------------------------------------------
    def _build(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)
        self.mode_label = QLabel("SZIMULÁCIÓ — nem műszermérés" if self.simulation
                                 else "ÉLŐ MŰSZER")
        font = QFont()
        font.setPointSize(16)
        font.setBold(True)
        self.mode_label.setFont(font)
        self.mode_label.setAlignment(Qt.AlignCenter)
        self.mode_label.setStyleSheet("background:#ffb000;color:black;padding:6px" if
                                      self.simulation else
                                      "background:#c00000;color:white;padding:6px")
        layout.addWidget(self.mode_label)

        top = QHBoxLayout()
        session = QGroupBox("Munkamenet")
        form = QFormLayout(session)
        self.warmup = QCheckBox("Legalább 4 órája folyamatosan bekapcsolva")
        self.last_acal = QLineEdit()
        self.last_acal.setPlaceholderText("utolsó ACAL ideje (kézzel)")
        self.note = QLineEdit()
        self.identity = QLabel("—")
        self.folder = QLabel("—")
        form.addRow(self.warmup)
        form.addRow("Utolsó ACAL", self.last_acal)
        form.addRow("Megjegyzés", self.note)
        form.addRow("Műszer", self.identity)
        form.addRow("Mappa", self.folder)
        top.addWidget(session)

        self.plan_table = QTableWidget(len(self.plan), 3)
        self.plan_table.setHorizontalHeaderLabels(["Pont", "Beállítás", "Állapot"])
        for row, item in enumerate(self.plan):
            label = item.test_id + (" (opcionális)" if item.optional else "")
            for col, text in enumerate((label, item.text, "—")):
                self.plan_table.setItem(row, col, QTableWidgetItem(text))
        top.addWidget(self.plan_table)
        layout.addLayout(top)

        gate = QGroupBox("Bekötés és aktuális lépés")
        gate_layout = QVBoxLayout(gate)
        self.gate_text = QLabel("—")
        self.gate_text.setWordWrap(True)
        gate_font = QFont()
        gate_font.setPointSize(12)
        self.gate_text.setFont(gate_font)
        self.gate_text.setMinimumHeight(110)  # series/ACAL lines must never be clipped
        # The button is the confirmation; this is an optional free-text note (operator's
        # choice, WP-05 manual review), journaled as written, empty when left empty.
        self.wiring_note = QLineEdit()
        self.wiring_note.setPlaceholderText("Megjegyzés a bekötéshez (opcionális)")
        settle_row = QHBoxLayout()
        self.settle = QDoubleSpinBox()
        self.settle.setRange(0, 86400)
        self.settle.setSuffix(" s")
        self.settle_reason = QLineEdit()
        self.settle_reason.setPlaceholderText("a stabilizálási idő módosításának oka")
        settle_row.addWidget(QLabel("Stabilizálás"))
        settle_row.addWidget(self.settle)
        settle_row.addWidget(self.settle_reason)
        self.confirm_button = QPushButton("Bekötve, folytatás")
        self.confirm_button.clicked.connect(self.on_confirm_wiring)
        gate_layout.addWidget(self.gate_text)
        gate_layout.addWidget(self.wiring_note)
        gate_layout.addLayout(settle_row)
        gate_layout.addWidget(self.confirm_button)
        layout.addWidget(gate)

        self.phase_label = QLabel("Állapot: IDLE")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.phase_label)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        self.buttons: dict[str, QPushButton] = {}
        handlers = {"new_session": self.on_new_session, "start": self.on_start,
                    "pause": self.on_pause, "abort": self.on_abort, "retry": self.on_retry,
                    "remeasure": self.on_remeasure, "skip_optional": self.on_skip,
                    "recover": self.on_recover, "export": self.on_export,
                    "acal": self.on_acal}
        for action, text in BUTTONS:
            button = QPushButton(text)
            button.clicked.connect(handlers[action])
            buttons.addWidget(button)
            self.buttons[action] = button
        layout.addLayout(buttons)

        self.results = QTableWidget(0, 7)
        self.results.setHorizontalHeaderLabels(["Pont", "Blokk", "Validálás", "N", "Átlag",
                                                "Szórás", "Egység"])
        self.results.cellClicked.connect(lambda row, _col: self._show_plot(row))
        self.block_data: list[dict] = []  # per results row, for the plot view
        self.plot_view = BlockPlotView(self.simulation)
        data_row = QHBoxLayout()
        data_row.addWidget(self.results, 2)
        data_row.addWidget(self.plot_view, 3)
        layout.addLayout(data_row)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        layout.addWidget(self.log_view)
        self.setCentralWidget(root)

    # --- state ---------------------------------------------------------------------------
    def dispatch(self, event: str, **data) -> bool:
        try:
            self.state = reduce(self.state, event, **data)
        except IllegalAction as exc:
            self._log(f"Nem engedélyezett: {exc}")
            return False
        self._refresh()
        return True

    def _refresh(self) -> None:
        allowed = allowed_actions(self.state)
        for action, button in self.buttons.items():
            button.setEnabled(action in allowed and not self.closing)
        # after a pause the same button continues the plan; say so on it
        self.buttons["start"].setText(
            "Folytatás" if self.state.phase == Phase.PAUSED else "Indítás")
        self.confirm_button.setEnabled("confirm_wiring" in allowed and not self.closing)
        self.phase_label.setText(f"Állapot: {self.state.phase.value} — {self.state.message}")
        if self.state.log:
            self._sync_log()

    def _sync_log(self) -> None:
        for line in self.state.log[self._shown_log:]:
            self.log_view.appendPlainText(line)
        self._shown_log = len(self.state.log)

    def _log(self, text: str) -> None:
        self.state = replace(self.state, log=self.state.log + (text,))
        self._sync_log()

    @staticmethod
    def _ask(title: str, text: str) -> bool:
        return QMessageBox.question(None, title, text) == QMessageBox.Yes

    # --- worker lifecycle -----------------------------------------------------------------
    def _start_worker(self) -> None:
        self.worker = self.worker_factory()
        self.thread = QThread(self)
        self.worker.moveToThread(self.thread)
        self.sig_open.connect(self.worker.open_session)
        self.sig_prepare.connect(self.worker.prepare)
        self.sig_run.connect(self.worker.run_point)
        self.sig_continue.connect(self.worker.continue_series)
        self.sig_retry.connect(self.worker.retry)
        self.sig_release.connect(self.worker.release_for_remeasure)
        self.sig_recover.connect(self.worker.recover)
        self.sig_export.connect(self.worker.export)
        self.sig_acal.connect(self.worker.run_acal)
        self.worker.acal_done.connect(self.on_acal_done)
        self.worker.session_ready.connect(self.on_session_ready)
        self.worker.session_failed.connect(self.on_session_failed)
        self.worker.gate.connect(self.on_gate)
        self.worker.tick.connect(self.on_tick)
        self.worker.block_done.connect(self.on_block_done)
        self.worker.recovered.connect(self.on_recovered)
        self.worker.storage_fault.connect(self.on_storage_fault)
        self.worker.fault.connect(self.on_fault)
        # a bound method, not a lambda: a lambda slot runs in the worker thread and
        # touched the widgets from there (Qt6Gui access violation on export, L4)
        self.worker.exported.connect(self.on_exported)
        self.worker.log.connect(self._log)
        self.worker.shut_down.connect(self.on_worker_shut_down)
        self.thread.start()

    # --- operator actions -----------------------------------------------------------------
    def on_new_session(self) -> None:
        # a discard decision belongs to exactly this start; dropped on every other path
        decision = (self.discard_reason, self.discard_count)
        self.discard_reason = self.discard_count = None
        if not self.warmup.isChecked() and not self.confirm(
                "Bemelegedés", "A 4 órás bemelegedés nincs megerősítve. Dokumentált "
                "exploratív mérésként folytatod (nem gyári specifikáció-vizsgálat)?"):
            return
        if self.worker is not None:
            self._shutdown_worker()  # previous worker ends; a new one owns the new session
        if self.dispatch("new_session"):
            self.discard_reason, self.discard_count = decision
            self._start_worker()
            self.discard_reason = self.discard_count = None
            self.sig_open.emit()

    def on_start(self) -> None:
        index = self.state.point_index
        if self.dispatch("start", optional=self.plan[index].optional,
                         series_end=series_end(self.plan, index)):
            if self.state.phase == Phase.SETTLING:  # paused series: same wiring, no gate
                self._continue_series()
            else:
                self.sig_prepare.emit(index)

    def _continue_series(self) -> None:
        index = self.state.point_index
        self._plan_status(index, "stabilizálás (sorozat)")
        self.progress.setValue(0)
        self.sig_continue.emit(index)

    def on_confirm_wiring(self) -> None:
        if self.gate_info is None:
            return
        default = self.gate_default
        reason = self.settle_reason.text().strip()
        if self.settle.value() != default and not reason:
            self._log("A stabilizálási idő módosításához ok megadása kötelező")
            return
        if self.dispatch("confirm_wiring"):
            self._plan_status(self.gate_info["index"], "stabilizálás")
            self.sig_run.emit(self.gate_info["index"], self.wiring_note.text().strip(),
                              float(self.settle.value()), reason)

    def on_pause(self) -> None:
        if self.dispatch("pause"):
            self.worker.request_pause()

    def on_abort(self) -> None:
        was_gate = self.state.phase == Phase.WAIT_WIRING
        if self.dispatch("abort") and not was_gate:
            self.worker.request_abort()

    def on_retry(self) -> None:
        if self.dispatch("retry"):
            self.sig_retry.emit(self.state.point_index)

    def on_remeasure(self) -> None:
        if self.dispatch("remeasure"):
            self.sig_release.emit()
            self.sig_prepare.emit(self.state.point_index)

    def on_skip(self) -> None:
        index = self.state.point_index
        if self.dispatch("skip_optional"):
            for row in range(index, self.state.point_index):  # the whole optional series
                self.plan_table.item(row, 2).setText("kihagyva")

    def on_recover(self) -> None:
        if self.dispatch("recover"):
            self.sig_recover.emit("GUI: operator-confirmed recovery after FAULT")

    def on_acal(self) -> None:
        from ..acal import CONDITIONS
        text = ("Run ACAL DCV + ACAL OHMS?\n\nEz megváltoztatja a műszer belső autokalibrációs "
                "állapotát. Feltételek:\n- " + "\n- ".join(CONDITIONS) +
                "\n\nUtána a következő mérés alap-stabilizálása legalább 30 perc lesz.")
        if "acal" in allowed_actions(self.state) and self.confirm("ACAL — külön művelet", text) \
                and self.dispatch("acal"):
            self.sig_acal.emit(self.note.text().strip())

    def on_acal_done(self, info: dict) -> None:
        if info.get("status") == "REFUSED":
            self._log(f"ACAL elutasítva: {info.get('note')}")
        self.progress.setValue(1000 if info.get("status") == "COMPLETE" else 0)
        steps = ", ".join(f"{k} {s} ({d:.0f} s)" for k, s, d in info.get("steps") or ())
        self._log(f"ACAL: {info.get('status')}; {steps}; TEMP? {info.get('temp_before')} → "
                  f"{info.get('temp_after')}; stabilizálás {info.get('settle_s', 0):.0f} s")
        if self.state.phase == Phase.ACAL:
            self.dispatch("acal_done", status=info.get("status"), note=info.get("note", ""))

    def on_export(self) -> None:
        if "export" in allowed_actions(self.state):
            self.sig_export.emit()

    def on_exported(self, path: str) -> None:
        self._log(f"Export: {path}")

    # --- worker facts ---------------------------------------------------------------------
    def on_session_ready(self, info: dict) -> None:
        self.identity.setText(f"{info['identity']} REV {info['revision']}")
        self.folder.setText(info["folder"])
        self.dispatch("session_ready", point_count=info["point_count"])

    def on_session_failed(self, info: dict) -> None:
        self.dispatch("session_failed", reason=info["reason"], fault=info.get("fault"))
        count = info.get("foreign_count")
        if count and self.confirm(
                "Műszermemória", f"{info['reason']}\n\nTörlöd a {count} minta memóriát, és "
                "új munkamenetet indítasz? (A döntés a naplóba kerül.)"):
            self.discard_reason = f"GUI: a kezelő törölte ({count} minta, nem archiválható)"
            self.discard_count = count
            self.on_new_session()

    def on_gate(self, info: dict) -> None:
        self.gate_info = info
        p = info["point"]
        control = "\nDIAGNOSZTIKAI KONTROLL (OCOMP OFF)" if info["diagnostic_control"] else ""
        self.gate_text.setText(
            f"Előző eszköz: {dut_label(info['previous_dut'])}  →  Következő eszköz: "
            f"{dut_label(info['next_dut'])}\n{p['test_id']}: {p['mode']} {p['range_value']:g}, "
            f"NPLC {p['nplc']}, N {p['n']}, AZERO ON, OCOMP {'ON' if p['ocomp'] else 'OFF'}, "
            f"DELAY {p['delay_s']:g} s, névleges {p['nominal']:g}"
            f"{control}{self._series_text(info['index'])}{self._acal_text(info)}")
        # after an ACAL the project settling (C17) raises the default; no reason needed
        self.gate_default = max(float(p["settling_s"]),
                                float(info.get("acal_settle_remaining_s") or 0.0))
        self.settle.setValue(self.gate_default)
        self.settle_reason.clear()
        self.wiring_note.clear()  # a note belongs to one block only
        self.progress.setValue(0)
        self.plan_table.item(info["index"], 2).setText("bekötésre vár")

    @staticmethod
    def _acal_text(info: dict) -> str:
        remaining = float(info.get("acal_settle_remaining_s") or 0.0)
        return (f"\nACAL utáni stabilizálás: még {remaining / 60:.0f} perc (projekt-policy, "
                "C17)" if remaining > 0 else "")

    def _series_text(self, index: int) -> str:
        end = min(self.state.series_end, len(self.plan))
        if end - index < 2:
            return ""
        ids = ", ".join(row.test_id for row in self.plan[index:end])
        return (f"\nSorozat ezzel a bekötéssel ({end - index} blokk, köztük nincs új kapu): "
                f"{ids}")

    def _plan_status(self, index: int, text: str) -> None:
        """The plan table row follows the running block (operator report: it stayed on
        'bekötésre vár' while the block was being measured)."""
        item = self.plan_table.item(index, 2) if 0 <= index < len(self.plan) else None
        if item is not None and item.text() != text:
            item.setText(text)

    def on_tick(self, phase: str, details: dict) -> None:
        if phase == "acal":
            self._acal_progress(details)
            return
        index = self.gate_info["index"] if self.state.phase in (
            Phase.SETTLING, Phase.ACQUIRING, Phase.READING, Phase.PAUSE_REQUESTED,
            Phase.ABORTING) and self.gate_info else None
        if index is not None and self.state.point_index != index:
            index = self.state.point_index  # a series continuation: no new gate
        if index is not None:
            self._plan_status(index, "stabilizálás" if phase == "settling"
                              else "mérés folyamatban")
        if phase == "settling":
            total = max(details["total_s"], 1e-9)
            self.progress.setValue(int(1000 * (1 - details["remaining_s"] / total)))
            self.phase_label.setText(f"Stabilizálás: még {details['remaining_s']:.0f} s")
        elif phase == "acquiring":
            if self.state.phase == Phase.SETTLING:
                self.dispatch("acquiring")
            estimate = max(details["estimate_s"], 1e-9)
            self.progress.setValue(min(1000, int(1000 * details["elapsed_s"] / estimate)))
            self.phase_label.setText(f"Mérés: {details['elapsed_s']:.0f} s / becslés "
                                     f"{estimate:.0f} s (a mintaidő becsült, nem mért)")

    def _acal_progress(self, info: dict) -> None:
        """Operator report (L4): no progress during ACAL. The bar follows the durations
        measured on this instrument (an estimate); READY by serial poll decides the end."""
        from ..acal import TYPICAL_SECONDS
        types = info["types"]
        done_s = sum(TYPICAL_SECONDS[k] for k in types[:info["number"] - 1])
        total = sum(TYPICAL_SECONDS[k] for k in types)
        current = min(info["elapsed_s"], info["typical_s"])
        self.progress.setValue(min(1000, int(1000 * (done_s + current) / max(total, 1e-9))))
        over = info["elapsed_s"] > info["typical_s"]
        self.phase_label.setText(
            f"ACAL {info['routine']} ({info['number']}/{info['count']}): "
            f"{info['elapsed_s']:.0f} s / várható ~{info['typical_s']:.0f} s"
            + (" (a szokásosnál tovább tart)" if over else "")
            + f"; gyári {info['factory_s']:.0f} s, határidő {info['deadline_s']:.0f} s")

    def on_block_done(self, info: dict) -> None:
        row = self.results.rowCount()
        self.results.insertRow(row)
        def text(value: Any) -> str:
            if value is None:
                return "—"
            return f"{value:.9g}" if isinstance(value, float) else str(value)
        for col, value in enumerate((info["test_id"], info["state"], info["validation"],
                                     info["n"], info["mean"], info["sdev"], info["unit"])):
            self.results.setItem(row, col, QTableWidgetItem(text(value)))
        self.plan_table.item(info["index"], 2).setText(info["state"])
        if info["state"] != "VALIDATED" and info.get("errors"):
            self._log(f"{info['test_id']}: {info['state']} — {'; '.join(info['errors'])}")
        self.block_data.append(info)
        self._show_plot(row)
        # the bar follows an estimate; at the end show the real outcome, not e.g. 99 %
        self.progress.setValue(1000 if info["state"] in ("VALIDATED", "INVALID") else 0)
        self.dispatch("block_done", state=info["state"])
        if self.state.phase == Phase.SETTLING and not self.closing:
            self._continue_series()  # same wiring series: next block without a gate
        self._maybe_finish_close()

    def _show_plot(self, row: int) -> None:
        if 0 <= row < len(self.block_data):
            info = self.block_data[row]
            self.plot_view.show_block(info["test_id"], info["state"], info.get("values") or [],
                                      info["unit"])

    def on_recovered(self, info: dict) -> None:
        self.dispatch("recovered", memory_intact=info["memory_intact"])

    def on_storage_fault(self, reason: str) -> None:
        self.dispatch("storage_fault", reason=reason)
        self._maybe_finish_close()

    def on_fault(self, reason: str) -> None:
        self.dispatch("fault", reason=reason)
        self._maybe_finish_close()

    # --- closing ----------------------------------------------------------------------------
    def closeEvent(self, event) -> None:
        if self.worker is None:
            event.accept()
            return
        if self.state.busy:
            if not self.closing:
                self.closing = True
                if self.state.phase not in (Phase.ABORTING, Phase.CONNECTING):
                    if "abort" in allowed_actions(self.state):
                        self.dispatch("abort")
                    self.worker.request_abort()
                self._log("Bezárás: a futó blokk szabályos megszakítása folyamatban")
                self._refresh()
            event.ignore()
            return
        self._shutdown_worker()
        event.accept()

    def _maybe_finish_close(self) -> None:
        if self.closing and not self.state.busy:
            self._shutdown_worker()
            self.closing = False
            self.close()

    def on_worker_shut_down(self) -> None:
        self._log("Worker leállt")

    def _shutdown_worker(self) -> None:
        """Run the worker's shutdown in its own thread and wait for it (safe end, session
        close or left OPEN, transport close) before the thread is stopped."""
        if self.worker is not None and self.thread is not None:
            QMetaObject.invokeMethod(self.worker, "shutdown",
                                     Qt.ConnectionType.BlockingQueuedConnection)
        self._stop_thread()

    def _stop_thread(self) -> None:
        if self.thread is not None:
            self.thread.quit()
            self.thread.wait(30_000)
            self.thread = None
            self.worker = None
