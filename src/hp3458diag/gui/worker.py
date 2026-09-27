"""The single owner of transport, engine and session store for the GUI (WP-05).

Lives in its own QThread: the transport is created and opened inside that thread
(single-owner rule of VisaTransport). The window talks to it only through queued
signals; pause/abort go straight to the engine's thread-safe request flags. Evidence
rules are those of run_points (WP-04): a block is released only after its file is
durable; an INVALID block stays owned for RETRY; storage failure keeps it owned.
"""

import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any
from PySide6.QtCore import QObject, Signal, Slot
from ..config import LabConfig
from ..domain import TestPoint, continuation_settle
from ..engine import AcquisitionEngine, BlockState, EngineError
from ..instrument import ResponseFormatError
from ..session_store import SessionStore, StorageError
from ..simulator import SimulatedInstrument, VirtualClock


@dataclass
class WorkerSettings:
    config: LabConfig
    output: Path
    points: list[TestPoint]
    simulation: bool = True
    sim_speed: float = 60.0          # simulated seconds per real second
    sim_fault: str = "none"
    session_meta: dict[str, Any] = field(default_factory=dict)
    discard_memory_reason: str | None = None  # operator's decision for unarchivable memory
    discard_memory_count: int | None = None   # ... valid only for this confirmed count


class SessionWorker(QObject):
    session_ready = Signal(dict)
    session_failed = Signal(dict)
    gate = Signal(dict)
    tick = Signal(str, dict)
    phase = Signal(str)
    block_done = Signal(dict)
    recovered = Signal(dict)
    storage_fault = Signal(str)
    fault = Signal(str)
    exported = Signal(str)
    acal_done = Signal(dict)
    log = Signal(str)
    shut_down = Signal()

    def __init__(self, settings: WorkerSettings) -> None:
        super().__init__()
        self.settings = settings
        self.store: SessionStore | None = None
        self.transport: Any = None
        self.engine: AcquisitionEngine | None = None
        self.block = None
        self.outcome = None
        self.last_dut: str | None = None
        self.clock: Any = time.monotonic
        self.sleep: Any = time.sleep
        self.acal_settle_until: float | None = None
        self.acal_fault = False  # ACAL READY missed: the routine may still run
        self.identified = False  # preflight accepted the ID? reply
        self.acal_count = 0
        self.last_mode: str | None = None  # function of the last configured block

    # --- thread-safe requests (called from the GUI thread) ----------------------------
    def request_pause(self) -> None:
        if self.engine is not None:
            self.engine.request_pause()

    def request_abort(self) -> None:
        if self.engine is not None:
            self.engine.request_abort()

    # --- slots (run in the worker thread) ---------------------------------------------
    @Slot()
    def open_session(self) -> None:
        s = self.settings
        try:
            if s.simulation:
                clock = VirtualClock()
                self.transport = SimulatedInstrument(clock, s.sim_fault)
                speed = s.sim_speed

                def sleep(seconds: float) -> None:
                    clock.sleep(seconds)
                    time.sleep(seconds / speed)
                timing = {"clock": clock.monotonic, "sleep": sleep}
                monotonic = clock.monotonic
                self.clock, self.sleep = clock.monotonic, sleep
            else:
                from ..transport.visa import VisaTransport
                timing, monotonic = {}, None
            meta = {"kind": "gui_session", "simulation": s.simulation,
                    "hardware_validation": "NOT_PERFORMED" if s.simulation
                    else "OPERATOR_STARTED_SESSION",
                    "test_ids": [p.test_id for p in s.points], **s.session_meta}
            self.store = SessionStore.create(s.output, meta,
                                             **({"monotonic": monotonic} if monotonic else {}))
            if not s.simulation:
                self.transport = VisaTransport(
                    replace(s.config.visa, timeout_ms=10_000, read_termination="lf"),
                    journal=self.store.bus_journal_path)
                self.transport.open()
            self.engine = AcquisitionEngine(
                self.transport, profile=s.config.reading_profile,
                stat_crosscheck=s.config.stat_crosscheck,
                accepted_identities=s.config.accepted_identities,
                event_sink=self.store.event_sink, on_tick=self._on_tick, **timing)
            preflight = self.engine.preflight()
            self.identified = True
            self.store.save_record("preflight.json", {"preflight": preflight})
            count = self.engine.foreign_memory["mcount"]
            if count and 2 <= count <= 100:
                self.store.save_record("foreign-memory.json",
                                       {"mcount": count,
                                        "raw": self.engine.archive_foreign_memory()})
            elif count and not (s.discard_memory_reason and s.discard_memory_count == count):
                reason = (f"{count} minta van a műszer memóriájában; nem archiválható "
                          "(csak 2–100 minta). Törlés csak kezelői döntéssel.")
                self.store.journal.append("session_stopped", status=(
                    "FOREIGN_MEMORY_NEEDS_DECISION"), reason=reason, mcount=count)
                self.session_failed.emit({  # stop: erasing needs the operator's decision
                    "reason": reason, "fault": False, "foreign_count": count})
                return
            elif count:
                self.engine.discard_foreign_memory(s.discard_memory_reason)
            self.store.save_record("baseline.json", self.engine.baseline())
            self.session_ready.emit({"identity": preflight.identity,
                                     "revision": ",".join(preflight.revision),
                                     "folder": str(self.store.folder),
                                     "point_count": len(s.points),
                                     "simulation": s.simulation})
        except (EngineError, StorageError, ValueError, OSError, RuntimeError) as exc:
            if self.store is not None:  # the failed start stays recorded in the session
                try:
                    self.store.journal.append("session_stopped",
                                              reason=f"{type(exc).__name__}: {exc}",
                                              raw=getattr(exc, "raw", None))
                except StorageError:
                    pass
            self.session_failed.emit({"reason": f"{type(exc).__name__}: {exc}",
                                      "fault": isinstance(exc, OSError)})

    def _on_tick(self, phase: str, details: dict) -> None:
        self.tick.emit(phase, dict(details))

    def acal_settle_remaining(self) -> float:
        if self.acal_settle_until is None:
            return 0.0
        return max(0.0, self.acal_settle_until - self.clock())

    @Slot(int)
    def prepare(self, index: int) -> None:
        point = self.settings.points[index]
        if not self._park_dcv():
            return
        self.gate.emit({"index": index, "point": asdict(point),
                        "acal_settle_remaining_s": self.acal_settle_remaining(),
                        "previous_dut": self.last_dut, "next_dut": point.dut_id,
                        "optional": point.optional,
                        "diagnostic_control": point.mode == "OHMF" and not point.ocomp})

    @Slot(int, str, float, str)
    def run_point(self, index: int, wiring: str, settle_s: float, reason: str) -> None:
        point = self.settings.points[index]
        try:
            self.store.journal.append("operator_wiring_confirmed", test_id=point.test_id,
                                      confirmed=True, note=wiring or None, settle_s=settle_s,
                                      settle_default_s=point.settling_s,
                                      settle_override_reason=reason or None,
                                      acal_settle_remaining_s=self.acal_settle_remaining())
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return
        self._run(index, point, settle_s)

    @Slot(int)
    def continue_series(self, index: int) -> None:
        """Next block of a wiring series: same DUT and wiring, so no operator gate; the
        settling follows `continuation_settle` and is journaled with its reason."""
        point, previous = self.settings.points[index], self.settings.points[index - 1]
        settle_s, reason = continuation_settle(previous, point)
        try:
            self.store.journal.append("series_continuation", test_id=point.test_id,
                                      previous_test_id=previous.test_id, settle_s=settle_s,
                                      settle_default_s=point.settling_s, settle_policy=reason)
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return
        self._run(index, point, settle_s)

    def _park_dcv(self) -> bool:
        """Before a wiring gate: TARM HOLD + DCV 10, so a source (5-10 V) is never connected
        while the 100 mV range of an A block (or PRESET NORM autorange) is selected. Only
        after DCV (D29: OHMF stays), with no pending block and a known bus."""
        if (self.last_mode not in (None, "DCV") or self.engine is None
                or self.engine.pending is not None or not self.identified or self.acal_fault
                or getattr(self.transport, "framing_unknown", False)):
            return True
        try:
            self.transport.write("TARM HOLD")
            self.transport.write("DCV 10")
            self.store.journal.append("safe_park", commands=["TARM HOLD", "DCV 10"],
                                      reason="wiring gate after DCV")
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return False
        except OSError as exc:  # no gate on an unknown bus state
            self.fault.emit(f"{type(exc).__name__}: {exc}")
            return False
        return True

    def _run(self, index: int, point: TestPoint, settle_s: float) -> None:
        try:
            if self.engine.paused:  # the operator pressed Start again after a pause
                self.engine.resume()
            self.phase.emit("settling")
            self.last_mode = point.mode
            outcome, block = self.engine.run_block(point, settle_s)
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return
        except EngineError as exc:
            self.fault.emit(f"EngineError: {exc}")
            return
        self.outcome, self.block = outcome, block
        self._finish_block(index, point, {"outcome": outcome}, outcome.state.value,
                           outcome.result or outcome.partial_result)

    def _finish_block(self, index: int, point: TestPoint, payload: dict, state: str,
                      result: Any) -> None:
        try:
            path = self.store.save_block({"simulation": self.settings.simulation,
                                          "test_point": point, **payload},
                                         point.test_id, self.outcome.block_id)
        except (StorageError, FileExistsError) as exc:
            self.storage_fault.emit(str(exc))  # block NOT released: memory stays owned
            return
        if state == "FAULT":
            self.fault.emit("A blokk FAULT állapotú: a buszállapot ismeretlen")
            self._emit_done(index, point, state, result, path)
            return
        if state != "INVALID" and self.block is not None:
            try:
                self.engine.release(self.block, saved=True)
            except StorageError as exc:
                self.storage_fault.emit(str(exc))
                return
            self.block = None
            self.last_dut = point.dut_id
        self._emit_done(index, point, state, result, path)

    def _emit_done(self, index, point, state, result, path) -> None:
        stats = getattr(result, "pc_statistics", None)
        self.block_done.emit({
            "index": index, "test_id": point.test_id, "state": state,
            "validation": getattr(result, "status", None),
            "n": len(getattr(result, "chronological_values", ()) or ()),
            "mean": getattr(stats, "mean", None), "sdev": getattr(stats, "sdev", None),
            "unit": point.unit, "file": str(path),
            "errors": [str(e) for e in getattr(result, "errors", ()) or ()],
            "values": [float(v) for v in getattr(result, "chronological_values", ()) or ()],
            "paused": bool(self.outcome and self.outcome.paused_after)})

    @Slot(int)
    def retry(self, index: int) -> None:
        point = self.settings.points[index]
        try:
            result = self.engine.read(self.block, self.outcome)  # same memory, no trigger
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return
        except (EngineError, ValueError, OSError) as exc:  # timeout: framing unknown
            self.fault.emit(f"{type(exc).__name__}: {exc}")
            return
        state = "VALIDATED" if result is not None and result.status == "VALIDATED" else (
            "FAULT" if self.engine.fault else "INVALID")
        self._finish_block(index, point, {"retry_of": self.outcome.block_id,
                                          "result": result}, state, result)

    @Slot()
    def release_for_remeasure(self) -> None:
        if self.block is not None:  # an INVALID block: its files are saved already
            try:
                self.engine.release(self.block, saved=True)
            except StorageError as exc:
                self.storage_fault.emit(str(exc))  # the block keeps owning the memory
                return
            self.block = None

    @Slot(str)
    def recover(self, reason: str) -> None:
        try:
            state = self.engine.recover(reason)
            self.acal_fault = False  # HOLD confirmed after SDC: the bus is known again
            intact = self.block is not None and self.block.state == BlockState.INVALID
            if self.block is not None and not intact:
                self.engine.release(self.block, saved=False,
                                    reason=f"GUI recovery: memory not intact ({state['mcount']})")
                self.block = None
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return
        except (EngineError, ValueError, OSError) as exc:
            self.fault.emit(f"Helyreállítás sikertelen: {exc}")
            return
        self.recovered.emit({"memory_intact": intact, "mcount": state["mcount"]})

    @Slot(str)
    def run_acal(self, note: str) -> None:
        """Separate autocal (WP-08): only when no block owns the memory; afterwards the
        measurement baseline is re-established with readback and the project settling
        (C17) is added to the next gate's default settling."""
        from ..acal import AcalRequest, run_acal
        if self.engine is None or self.engine.pending is not None:
            self.acal_done.emit({"status": "REFUSED", "note": "a blokk birtokolja a memóriát"})
            return
        request = AcalRequest(("DCV", "OHMS"), "GUI: a kezelő a feltételeket az ACAL "
                              "párbeszédablakban megerősítette" + (f"; {note}" if note else ""),
                              inputs_disconnected=True, warmed_up_2h=True)
        try:
            result = run_acal(self.transport, request,
                              accepted_identities=self.settings.config.accepted_identities,
                              clock=self.clock, sleep=self.sleep,
                              event_sink=self.store.event_sink,
                              progress=lambda info: self.tick.emit("acal", dict(info)))
            self.acal_count += 1
            self.acal_fault = self.acal_fault or result.status == "FAULT"
            self.store.save_record(f"acal-{self.acal_count:02d}.json", {"acal": result})
            if result.status in ("COMPLETE", "ERROR"):
                self.store.save_record(f"baseline-after-acal-{self.acal_count:02d}.json",
                                       self.engine.baseline())
                self.acal_settle_until = self.clock() + result.settle_s
        except StorageError as exc:
            self.storage_fault.emit(str(exc))
            return
        except (EngineError, ResponseFormatError, OSError, RuntimeError, ValueError) as exc:
            self.acal_fault = True
            self.acal_done.emit({"status": "FAULT", "note": f"{type(exc).__name__}: {exc}"})
            return
        self.acal_done.emit({"status": result.status, "note": result.note,
                             "settle_s": result.settle_s,
                             "temp_before": result.temp_before_raw,
                             "temp_after": result.temp_after_raw,
                             "steps": [(s.kind, s.state, s.duration_s) for s in result.steps]})

    @Slot()
    def export(self) -> None:
        from ..export import export_session
        try:
            self.exported.emit(str(export_session(self.store.folder)))
        except (StorageError, OSError) as exc:
            self.log.emit(f"Export sikertelen: {exc}")

    @Slot()
    def shutdown(self) -> None:
        """Safe end when the memory is released and the bus is known; the session is left
        OPEN (for recovery) while a block still owns the memory."""
        try:
            if self.engine is not None and self.engine.pending is None and \
                    self.identified and not self.acal_fault and \
                    not getattr(self.transport, "framing_unknown", False):
                self.transport.write("TARM HOLD")
                self.transport.write("DCV 10")
            if self.store is not None:
                if self.engine is not None and self.engine.pending is not None:
                    self.store.journal.append("session_left_open",
                                              pending_block=self.engine.pending.block_id)
                else:
                    self.store.close("CLOSED_BY_OPERATOR")
        except (OSError, RuntimeError) as exc:
            self.log.emit(f"Leállítási hiba: {exc}")
        finally:
            try:
                if self.transport is not None:
                    self.transport.close()
            except OSError:
                pass
            self.shut_down.emit()
