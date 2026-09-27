"""Acquisition engine and reading-memory lifecycle (WP-02). One worker thread owns it.

Sequence per block (docs/COMMANDS.md, D19, D21):
  configure (TARM HOLD first) -> readback + ERR? -> host settling on a monotonic clock
  -> TEMP? start -> MEM FIFO -> MCOUNT?=0 -> NRDGS N,AUTO -> INBUF ON -> TARM SGL
  -> serial-poll READY -> MCOUNT?=N and TARM?=HOLD -> MEM OFF -> INBUF OFF -> ERR?
  -> TEMP? end -> RMEM (only right after MCOUNT?=N) via memory_reader.

The instrument memory is owned by one PendingBlock until it is released as saved or
discarded with a logged reason: no configuration, baseline, MEM FIFO or new block
before that. RETRY re-reads the same memory and never triggers. Pause is honoured
after the current block; abort uses a logged SDC and becomes ABORTED only with a
confirmed HOLD and a stable MCOUNT?. Completion is never inferred from elapsed time:
READY is only a cue, MCOUNT? = N is the proof. No calibration, no ACAL.
"""

import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable
from .commands import BASELINE, configuration
from .domain import TestPoint
from .instrument import (Preflight, ResponseFormatError, _line, preflight_identity,
                         read_error_register)
from .memory_reader import BlockResult, validate_memory
from .replies import parse_reply
from .simulator import SECONDS_PER_READING
from . import tolerances
from .validation import DataValidationError, ReadingProfile, parse_scalar

READY = 16          # serial poll bit 4 (p. 306)
TARM_HOLD = "4"     # TARM? numeric HOLD
ASCII_BYTES_PER_READING = 16  # MFORMAT ASCII storage (C04)


class EngineError(RuntimeError):
    """A rule of the memory lifecycle or an instrument check was violated."""


class BlockState(str, Enum):
    ACQUIRED = "ACQUIRED"          # MCOUNT? = N, HOLD confirmed, not yet read
    PARTIAL = "PARTIAL"            # fewer than N readings (abort, stall, deadline)
    COUNT_MISMATCH = "COUNT_MISMATCH"  # more than N readings or count changed
    VALIDATED = "VALIDATED"
    INVALID = "INVALID"            # read done, no validated consensus: RETRY allowed
    ABORTED = "ABORTED"            # SDC abort with confirmed HOLD and stable count
    FAULT = "FAULT"                # transport/bus state unknown: operator recovery
    RELEASED = "RELEASED"


@dataclass
class PendingBlock:
    session_id: str
    block_id: str
    point: TestPoint
    fingerprint: tuple[str, ...]
    state: BlockState
    mcount: int | None = None
    reads: int = 0
    released_as: str | None = None


@dataclass
class BlockOutcome:
    block_id: str
    test_id: str
    state: BlockState
    requested: tuple[str, ...]
    readback: dict[str, bytes] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    settle_requested_s: float = 0.0
    settle_actual_s: float = 0.0
    temp_start_raw: bytes | None = None
    temp_end_raw: bytes | None = None
    started_utc: str | None = None
    ended_utc: str | None = None
    acquisition_s: float | None = None
    serial_polls: list[tuple[float, int]] = field(default_factory=list)
    mcount: int | None = None
    abort: dict[str, Any] | None = None
    result: BlockResult | None = None
    partial_result: BlockResult | None = None
    paused_after: bool = False


class _Aborted(Exception):
    pass


def seconds_per_reading(point: TestPoint) -> float:
    return SECONDS_PER_READING.get((point.mode, point.nplc), point.nplc / 50 * 4)


class AcquisitionEngine:
    def __init__(self, transport: Any, *, profile: ReadingProfile, stat_crosscheck: str,
                 accepted_identities: tuple[str, ...],
                 absolute_tolerance: float = 0.0, relative_tolerance: float = 0.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_interval_s: float = 0.25, settle_step_s: float = 1.0,
                 line_frequency_hz: int = 50,
                 event_sink: Callable[[dict[str, Any]], None] | None = None,
                 on_tick: Callable[[str, dict[str, Any]], None] | None = None) -> None:
        # on_tick: progress for a GUI (settling countdown, acquisition elapsed); not
        # journaled, must be cheap and must not raise.
        self.on_tick = on_tick
        # event_sink: durable journal (WP-04). If it raises, the step fails: a block
        # whose events cannot be recorded becomes FAULT and keeps owning the memory.
        self.event_sink = event_sink
        self.line_frequency_hz = line_frequency_hz
        self.line_frequency = tolerances.line_frequency(line_frequency_hz)
        self.transport = transport
        self.profile = profile
        self.stat_crosscheck = stat_crosscheck
        self.accepted_identities = accepted_identities
        self.absolute_tolerance = absolute_tolerance
        self.relative_tolerance = relative_tolerance
        self.clock, self.sleep = clock, sleep
        self.poll_interval_s, self.settle_step_s = poll_interval_s, settle_step_s
        self.session_id = str(uuid.uuid4())
        self.preflight_result: Preflight | None = None
        self.foreign_memory: dict[str, Any] | None = None
        self.baseline_done = False
        self.pending: PendingBlock | None = None
        self.paused = False
        self.fault: str | None = None
        self.events: list[dict[str, Any]] = []
        self._pause = threading.Event()
        self._abort = threading.Event()

    # --- requests from other threads (GUI) -------------------------------------------
    def request_pause(self) -> None:
        self._pause.set()

    def request_abort(self) -> None:
        self._abort.set()

    def resume(self) -> None:
        self._pause.clear()
        self.paused = False
        self._event("resume")

    # --- helpers --------------------------------------------------------------------
    def _event(self, name: str, **details: Any) -> None:
        event = {"event": name, "utc": datetime.now(timezone.utc).isoformat(),
                 "monotonic_s": self.clock(), **details}
        self.events.append(event)
        if self.event_sink is not None:
            self.event_sink(event)

    def _tick(self, phase: str, **details: Any) -> None:
        if self.on_tick is not None:
            self.on_tick(phase, details)

    def _utc(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _int(self, query: str) -> int:
        raw = self.transport.query_raw(query)
        text = _line(raw).strip()
        if not text.lstrip("+-").isdigit():
            raise ResponseFormatError(f"{query} must return an integer", raw)
        return int(text)

    def _text(self, query: str) -> str:
        return _line(self.transport.query_raw(query)).strip()

    def _require_no_fault(self) -> None:
        if self.fault is not None:
            raise EngineError(f"Engine in FAULT ({self.fault}); explicit recover() required")

    def _require_memory_free(self, action: str) -> None:
        if self.pending is not None:
            raise EngineError(f"{action} refused: block {self.pending.block_id} "
                              f"({self.pending.state.value}) is not released")
        if self.foreign_memory is not None and not self.foreign_memory.get("released"):
            raise EngineError(f"{action} refused: pre-existing instrument memory not "
                              "archived or discarded")

    def _fail(self, reason: str) -> None:
        self.fault = reason
        try:
            self._event("fault", reason=reason)
        except OSError as exc:  # journal unavailable: the FAULT itself must still hold
            self.events.append({"event": "fault_not_journaled", "error": str(exc)})

    # --- session --------------------------------------------------------------------
    def preflight(self) -> Preflight:
        """ID?/REV?/errors; profile must be verified for this firmware; existing memory is
        recorded and must be archived or discarded before anything may clear it."""
        result = preflight_identity(self.transport, self.accepted_identities)
        if not self.profile.verified_for(result.revision):
            raise EngineError(f"Reading profile {self.profile.name} is not verified for "
                              f"REV {','.join(result.revision)}")
        self.preflight_result = result
        count = self._int("MCOUNT?")
        terminals = self.transport.query_raw("TERM?")
        self.foreign_memory = {"mcount": count, "released": count == 0}
        self._event("preflight", mcount=count, terminals=terminals)
        return result

    def archive_foreign_memory(self) -> bytes:
        """Single RMEM of pre-existing memory (evidence only), after MCOUNT? = count."""
        if not self.foreign_memory or self.foreign_memory["released"]:
            raise EngineError("No pre-existing memory to archive")
        count = self.foreign_memory["mcount"]
        if not 2 <= count <= 100 or self._int("MCOUNT?") != count:
            raise EngineError("Pre-existing memory cannot be archived; discard it explicitly")
        raw = self.transport.query_raw(f"RMEM 1,{count}")
        self.foreign_memory.update(archived_raw=raw, released=True)
        self._event("foreign_memory_archived", mcount=count)
        return raw

    def discard_foreign_memory(self, reason: str) -> None:
        if not reason.strip():
            raise EngineError("Discarding instrument memory requires an operator reason")
        if not self.foreign_memory:
            raise EngineError("Preflight first")
        self.foreign_memory.update(released=True, discard_reason=reason)
        self._event("foreign_memory_discarded", reason=reason)

    def baseline(self) -> dict[str, Any]:
        self._require_no_fault()
        if self.preflight_result is None:
            raise EngineError("Preflight first")
        self._require_memory_free("Baseline")
        for command in BASELINE:
            self.transport.write(command)
        expected = {"OFORMAT?": "1", "MFORMAT?": "1", "END?": "1", "TARM?": TARM_HOLD,
                    "INBUF?": "0", "MEM?": "0"}
        readback = {query: self._text(query) for query in expected}
        errors = read_error_register(self.transport)
        msize = self._text("MSIZE?").split(",")
        capacity = int(msize[0].strip()) // ASCII_BYTES_PER_READING
        mismatches = [q for q, v in expected.items() if readback[q] != v]
        lfreq_raw = self.transport.query_raw("LFREQ?")
        lfreq = parse_reply("LFREQ?", lfreq_raw).value
        if not self.line_frequency.accepts(Decimal(self.line_frequency_hz), lfreq):
            mismatches.append(f"LFREQ? {lfreq} Hz outside {self.line_frequency.source}")
        if mismatches or errors.value != 0 or capacity < 100:
            raise EngineError(f"Baseline not confirmed: mismatches={mismatches}, "
                              f"ERR?={errors.value}, ascii_capacity={capacity}")
        self.baseline_done = True
        record = {"readback": readback, "ascii_capacity": capacity, "msize": msize,
                  "lfreq_hz": lfreq}
        self._event("baseline", **record)
        return record

    # --- block ----------------------------------------------------------------------
    def configure(self, point: TestPoint, outcome: BlockOutcome) -> None:
        self._require_no_fault()
        if not self.baseline_done:
            raise EngineError("Baseline first")
        self._require_memory_free("Configuration change")
        for command in outcome.requested:
            self.transport.write(command)
        queries = ("NPLC?", "AZERO?", "OCOMP?", "DELAY?", "TARM?")
        outcome.readback = {query: self.transport.query_raw(query) for query in queries}
        problems = []
        nplc = parse_scalar(outcome.readback["NPLC?"], self.profile)[0]
        if not tolerances.NPLC.accepts(Decimal(point.nplc), nplc):
            problems.append(f"NPLC {nplc}")
        if _line(outcome.readback["AZERO?"]).strip() != "1":
            problems.append("AZERO")
        if _line(outcome.readback["OCOMP?"]).strip() != ("1" if point.ocomp else "0"):
            problems.append("OCOMP")
        delay = parse_scalar(outcome.readback["DELAY?"], self.profile)[0]
        if not tolerances.DELAY.accepts(Decimal(str(point.delay_s)), delay):
            problems.append(f"DELAY {delay} s")
        if _line(outcome.readback["TARM?"]).strip() != TARM_HOLD:
            problems.append("TARM")
        errors = read_error_register(self.transport)
        if errors.value != 0:
            problems.append(f"ERR?={errors.value} {list(errors.bits)}")
        if problems:
            raise EngineError(f"Configuration readback failed: {problems}")
        self._event("configured", test_id=point.test_id, fingerprint=outcome.requested)

    def settle(self, seconds: float, outcome: BlockOutcome) -> None:
        """Host settling on the monotonic clock; abort is checked, nothing is measured."""
        start = self.clock()
        outcome.settle_requested_s = seconds
        while (remaining := seconds - (self.clock() - start)) > 0:
            if self._abort.is_set():
                outcome.settle_actual_s = self.clock() - start
                raise _Aborted("abort during settling")
            self._tick("settling", remaining_s=remaining, total_s=seconds)
            self.sleep(min(self.settle_step_s, remaining))
        outcome.settle_actual_s = self.clock() - start
        if self._abort.is_set():  # also with 0 s settling: never arm after an abort request
            raise _Aborted("abort before arming")

    def _ready_deadline_s(self, point: TestPoint) -> float:
        # DELAY acts once per trigger (p. 170; WP-02 live check), not per reading.
        return 3 * (point.n * seconds_per_reading(point) + point.delay_s) + 60

    def acquire(self, point: TestPoint, outcome: BlockOutcome) -> PendingBlock:
        self._require_no_fault()
        self._require_memory_free("New block")
        outcome.temp_start_raw = self.transport.query_raw("TEMP?")
        self.transport.write("MEM FIFO")
        block = PendingBlock(self.session_id, outcome.block_id, point, outcome.requested,
                             BlockState.PARTIAL)
        self.pending = block  # from here on the instrument memory belongs to this block
        self._event("block_opened", block_id=block.block_id, n=point.n,
                    test_id=point.test_id, point=asdict(point), fingerprint=outcome.requested)
        count0 = self._int("MCOUNT?")
        if count0 != 0:
            block.state, block.mcount = BlockState.COUNT_MISMATCH, count0
            outcome.errors.append(f"MCOUNT? after MEM FIFO is {count0}: old data, no trigger")
            self.transport.write("MEM OFF")
            return block
        self.transport.write(f"NRDGS {point.n},AUTO")
        self.transport.write("INBUF ON")
        if self._text("INBUF?") != "1":
            raise EngineError("INBUF ON not confirmed")
        outcome.started_utc = self._utc()
        start = self.clock()
        self.transport.write("TARM SGL")
        self._event("armed", block_id=block.block_id, n=point.n, test_id=point.test_id,
                    point=asdict(point), fingerprint=outcome.requested)
        deadline = self._ready_deadline_s(point)
        while True:
            self.sleep(self.poll_interval_s)  # pacing only; READY + MCOUNT? prove completion
            status = self.transport.serial_poll()
            outcome.serial_polls.append((round(self.clock() - start, 3), status))
            self._tick("acquiring", elapsed_s=self.clock() - start, status=status,
                       estimate_s=point.n * seconds_per_reading(point) + point.delay_s)
            if status & READY:
                break
            if self._abort.is_set():
                self._abort_block(block, outcome, "operator abort")
                return block
            if self.clock() - start > deadline:
                self._abort_block(block, outcome, f"READY not seen within {deadline:.0f} s")
                return block
        outcome.acquisition_s = round(self.clock() - start, 3)
        count = self._int("MCOUNT?")
        tarm = self._text("TARM?")
        block.mcount = outcome.mcount = count
        self.transport.write("MEM OFF")
        self.transport.write("INBUF OFF")
        errors = read_error_register(self.transport)
        outcome.temp_end_raw = self.transport.query_raw("TEMP?")
        outcome.ended_utc = self._utc()
        if errors.value != 0:
            outcome.errors.append(f"ERR?={errors.value} {list(errors.bits)} after acquisition")
        if tarm != TARM_HOLD:
            block.state = BlockState.FAULT
            outcome.errors.append(f"TARM? {tarm} after block, HOLD not confirmed")
            self._fail("HOLD not confirmed after acquisition")
        elif count == point.n:  # an ERR? bit is recorded above; the readings are still read
            block.state = BlockState.ACQUIRED
        elif count < point.n:
            block.state = BlockState.PARTIAL
            outcome.errors.append(f"MCOUNT? {count} < N {point.n}")
        else:
            block.state = BlockState.COUNT_MISMATCH
            outcome.errors.append(f"MCOUNT? {count} != N {point.n}")
        self._event("acquired", block_id=block.block_id, state=block.state.value, mcount=count)
        return block

    def _abort_block(self, block: PendingBlock, outcome: BlockOutcome, reason: str) -> None:
        """Logged SDC, then TARM HOLD first (any other command would resume triggering,
        p. 304), HOLD readback and a count stable over more than one reading time."""
        self._event("aborting", block_id=block.block_id, reason=reason)
        record: dict[str, Any] = {"reason": reason}
        outcome.abort = record
        self.transport.clear_device(f"abort block {block.block_id}: {reason}")
        self.transport.write("TARM HOLD")
        record["tarm"] = tarm = self._text("TARM?")
        first = self._int("MCOUNT?")
        window = 1.5 * seconds_per_reading(block.point) + block.point.delay_s + 1.0
        self.sleep(window)
        second = self._int("MCOUNT?")
        record.update(mcount_first=first, mcount_second=second, stability_window_s=window)
        self.transport.write("INBUF OFF")
        record["errors"] = read_error_register(self.transport)
        block.mcount = outcome.mcount = second
        self._abort.clear()
        if tarm == TARM_HOLD and first == second and second <= block.point.n:
            block.state = BlockState.ABORTED if reason == "operator abort" else BlockState.PARTIAL
            outcome.errors.append(f"{reason}: stopped at {second}/{block.point.n}")
        else:
            block.state = BlockState.FAULT
            self._fail(f"abort not confirmed (TARM? {tarm}, MCOUNT? {first}->{second})")
        self._event("aborted", block_id=block.block_id, state=block.state.value)

    def read(self, block: PendingBlock, outcome: BlockOutcome) -> BlockResult | None:
        """Validate the owned memory. D21: RMEM only right after MCOUNT? = expected."""
        self._require_no_fault()
        if self.pending is not block:
            raise EngineError("Block is not the pending block")
        full = block.state in (BlockState.ACQUIRED, BlockState.INVALID)
        partial = block.state in (BlockState.PARTIAL, BlockState.ABORTED)
        if not (full or partial):
            raise EngineError(f"Block in state {block.state.value} cannot be read")
        expected = block.point.n if full else (block.mcount or 0)
        if expected < 2:
            outcome.errors.append("Fewer than 2 stored readings: nothing to validate")
            return None
        try:
            count = self._int("MCOUNT?")
        except (OSError, ValueError):
            block.state = BlockState.FAULT  # recover() then checks the count again
            self._fail("MCOUNT? before RMEM failed: bus state unknown")
            raise
        if count != expected:
            block.state = BlockState.COUNT_MISMATCH
            outcome.errors.append(f"MCOUNT? {count} before RMEM, expected {expected}")
            return None
        point = block.point if full else replace(block.point, n=expected)
        read_kind = "COLD" if block.reads == 0 else "RETRY"  # re-read of the same memory
        block.reads += 1
        result = validate_memory(self.transport, point,
                                 absolute_tolerance=self.absolute_tolerance,
                                 relative_tolerance=self.relative_tolerance,
                                 profile=self.profile, stat_crosscheck=self.stat_crosscheck,
                                 read_kind=read_kind)
        if result.status == "TRANSPORT_ERROR":
            block.state = BlockState.FAULT
            self._fail("transport error while reading memory")
        elif full:
            block.state = (BlockState.VALIDATED if result.status == "VALIDATED"
                           else BlockState.INVALID)
        self._event("read", block_id=block.block_id, status=result.status, full=full)
        return result

    def release(self, block: PendingBlock, *, saved: bool, reason: str = "") -> None:
        """Hand the memory back only after the caller saved the evidence, or with a reason."""
        if self.pending is not block:
            raise EngineError("Block is not the pending block")
        if not saved and not reason.strip():
            raise EngineError("Discarding an unsaved block requires an operator reason")
        if block.state == BlockState.FAULT and saved:
            raise EngineError("A FAULT block must be discarded with a reason after recovery")
        how = "saved" if saved else f"discarded: {reason}"
        self._event("released", block_id=block.block_id, how=how)  # journal first
        block.released_as = how
        block.state = BlockState.RELEASED
        self.pending = None

    def recover(self, reason: str) -> dict[str, Any]:
        """Explicit operator recovery after a FAULT: logged SDC, TARM HOLD, state readback.
        Never re-measures; the pending block stays owned until released."""
        if not reason.strip():
            raise EngineError("Recovery requires an operator reason")
        self.transport.clear_device(f"recover: {reason}")
        self.transport.write("TARM HOLD")
        state = {"tarm": self._text("TARM?"), "mcount": self._int("MCOUNT?"),
                 "errors": read_error_register(self.transport)}
        self.transport.write("INBUF OFF")
        if state["tarm"] != TARM_HOLD:
            raise EngineError(f"Recovery could not confirm HOLD: {state}")
        self.fault = None
        self._abort.clear()  # recovery measures nothing: an abort pressed meanwhile is void
        block = self.pending
        if (block is not None and block.state == BlockState.FAULT
                and block.mcount == block.point.n == state["mcount"]):
            # The full block is still in memory: a RETRY read of the same memory is allowed.
            block.state = BlockState.INVALID
        self._event("recovered", reason=reason, **{k: v for k, v in state.items()
                                                    if k != "errors"})
        return state

    def run_block(self, point: TestPoint, settle_s: float | None = None) -> tuple[
            BlockOutcome, PendingBlock | None]:
        """configure -> settle -> acquire -> read. The caller saves, then release()s."""
        if self.paused:
            raise EngineError("Engine paused; resume() first")
        outcome = BlockOutcome(str(uuid.uuid4()), point.test_id, BlockState.PARTIAL,
                               configuration(point))
        try:
            self.configure(point, outcome)
            self.settle(point.settling_s if settle_s is None else settle_s, outcome)
            block = self.acquire(point, outcome)
            if block.state == BlockState.ACQUIRED:
                outcome.result = self.read(block, outcome)
            elif block.state in (BlockState.PARTIAL, BlockState.ABORTED):
                outcome.partial_result = self.read(block, outcome)
            outcome.state = block.state
        except _Aborted as exc:
            outcome.state = BlockState.ABORTED
            outcome.errors.append(str(exc))
            self._abort.clear()
        except (OSError, ResponseFormatError, DataValidationError) as exc:
            outcome.state = BlockState.FAULT
            outcome.errors.append(f"{type(exc).__name__}: {exc}")
            if self.pending is not None:  # memory already owned: keep it, mark FAULT
                self.pending.state = BlockState.FAULT
            self._fail(f"{type(exc).__name__}: {exc}")
        self._abort.clear()  # e.g. pressed as the block completed: not for the next block
        if self._pause.is_set():
            self.paused = outcome.paused_after = True
            self._event("paused_after_block", block_id=outcome.block_id)
        return outcome, self.pending
