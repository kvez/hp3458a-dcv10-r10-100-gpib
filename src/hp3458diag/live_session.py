"""Operator-started live session: plan points through the engine into a durable store.

The operator's wiring confirmation text is stored verbatim. One CLI confirmation covers
exactly one wiring series (D27: same DUT and function); a plan that changes DUT or wiring is
refused, because only the GUI wizard asks for a new gate at every change (R13, WP-07 audit). Pre-existing instrument memory is archived (2..100 readings) or
discarded only with an explicit reason. The session ends with TARM HOLD + DCV 10 when the
memory is released and the bus state is known. A storage failure leaves the session
OPEN (detected later by find_interrupted) and the block memory owned for recovery.
"""

from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from .config import LabConfig
from .domain import TestPoint, series_end
from .engine import AcquisitionEngine, EngineError
from .instrument import IdentityError, ResponseFormatError, software_environment
from .persistence import _git_revision
from .session_runner import run_points
from .session_store import SessionStore, StorageError
from .transport.visa import VisaTransport


def run_live_session(config: LabConfig, output: Path, points: list[TestPoint],
                     confirm_wiring: str, settle_s: float | None = None,
                     discard_memory_reason: str | None = None,
                     transport_factory: Callable[..., Any] = VisaTransport,
                     clock: Callable[[], float] | None = None,
                     sleep: Callable[[float], None] | None = None
                     ) -> tuple[str, Path, list]:
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    if not confirm_wiring.strip():
        raise ValueError("A live session needs the operator's wiring confirmation text")
    if not points or series_end(points, 0) != len(points):
        raise ValueError("One CLI confirmation covers one wiring series (same DUT and "
                         "function); use the GUI wizard for DUT or wiring changes")
    store = SessionStore.create(output, {
        "kind": "live_session", "simulation": False,
        "hardware_validation": "OPERATOR_STARTED_SESSION",
        "operator_wiring_confirmation": confirm_wiring, "settle_override_s": settle_s,
        "test_ids": [p.test_id for p in points], "config_source": config.source,
        "visa_resource": config.visa.resource, "git_revision": _git_revision(),
        "software_environment": software_environment()}, **({"monotonic": clock} if clock
                                                           else {}))
    transport = transport_factory(replace(config.visa, timeout_ms=10_000,
                                          read_termination="lf"),
                                  journal=store.bus_journal_path)
    timing = {k: v for k, v in (("clock", clock), ("sleep", sleep)) if v is not None}
    engine = AcquisitionEngine(transport, profile=config.reading_profile,
                               stat_crosscheck=config.stat_crosscheck,
                               accepted_identities=config.accepted_identities,
                               event_sink=store.event_sink, **timing)
    status, summary = "TRANSPORT_ERROR", []
    identified = False  # nothing is sent to an instrument whose ID? was rejected
    try:
        transport.open()
        preflight = engine.preflight()
        identified = True
        store.save_record("preflight.json", {"preflight": preflight,
                                             "foreign_memory": dict(engine.foreign_memory)})
        count = engine.foreign_memory["mcount"]
        if count and 2 <= count <= 100:
            store.save_record("foreign-memory.json",
                              {"mcount": count, "raw": engine.archive_foreign_memory()})
        elif count and discard_memory_reason:
            engine.discard_foreign_memory(discard_memory_reason)
        elif count:
            status = "FOREIGN_MEMORY_NEEDS_DECISION"
            raise EngineError(f"{count} readings in memory: archive impossible, no discard "
                              "reason given")
        store.save_record("baseline.json", engine.baseline())
        status, summary = run_points(store, engine, points, settle_s)
    except StorageError:
        status = "STORAGE_FAULT"
    except EngineError as exc:
        status = status if status != "TRANSPORT_ERROR" else "ENGINE_STOPPED"
        try:
            store.journal.append("session_stopped", reason=str(exc))
        except StorageError:
            status = "STORAGE_FAULT"
    except (ResponseFormatError, ValueError, OSError, RuntimeError) as exc:
        if isinstance(exc, IdentityError):
            status = "IDENTITY_REJECTED"
        try:
            store.journal.append("session_stopped", reason=f"{type(exc).__name__}: {exc}")
        except StorageError:
            status = "STORAGE_FAULT"
    if (identified and engine.pending is None and getattr(transport, "is_open", True)
            and not getattr(transport, "framing_unknown", False)):
        try:
            transport.write("TARM HOLD")
            transport.write("DCV 10")  # leave no ohms current source selected
        except (OSError, RuntimeError):
            pass
    try:
        if engine.pending is not None:
            # Memory still owned by an unreleased block: keep the session OPEN so that
            # find_interrupted reports it and an explicit recovery can read it.
            store.journal.append("session_left_open", status=status,
                                 pending_block=engine.pending.block_id)
        else:
            store.close(status)
    except StorageError:
        status = "STORAGE_FAULT"  # the session stays OPEN on disk: find_interrupted sees it
    try:
        transport.close()
    except OSError:
        pass
    return status, store.folder, summary
