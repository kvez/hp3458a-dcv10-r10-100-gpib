"""Complete fake session through the real engine and the durable store (WP-02/WP-04).

SimulatedInstrument + VirtualClock; the same SessionStore and run_points path as a live
session. Output is labelled simulation everywhere and is not hardware evidence.
"""

from pathlib import Path
from .domain import TestPoint
from .engine import AcquisitionEngine
from .session_runner import run_points
from .session_store import SessionStore
from .simulator import SimulatedInstrument, VirtualClock
from .validation import HP3458A_REV9_1


def run_simulated_session(points: list[TestPoint], output: Path, fault: str = "none",
                          nominal_ohm: float = 10.0) -> tuple[str, Path, list]:
    clock = VirtualClock()
    sim = SimulatedInstrument(clock, fault, nominal_ohm)
    store = SessionStore.create(output, {"kind": "engine_simulated_session",
                                         "simulation": True, "fault": fault,
                                         "hardware_validation": "NOT_PERFORMED",
                                         "test_ids": [p.test_id for p in points]},
                                monotonic=clock.monotonic)
    engine = AcquisitionEngine(sim, profile=HP3458A_REV9_1,
                               stat_crosscheck="dmm_half_quantum",
                               accepted_identities=("HP3458A",), clock=clock.monotonic,
                               sleep=clock.sleep, event_sink=store.event_sink)
    engine.preflight()
    if engine.foreign_memory["mcount"]:
        engine.discard_foreign_memory("simulation: pre-existing simulated memory")
    store.save_record("baseline.json", engine.baseline())
    status, summary = run_points(store, engine, points)
    store.save_record("simulated-commands.json", {"command_log": sim.commands,
                                                  "sdc_reasons": sim.clears})
    if engine.pending is not None:  # unreleased block: leave OPEN for explicit recovery
        store.journal.append("session_left_open", status=status,
                             pending_block=engine.pending.block_id)
    else:
        store.close(status)
    return status, store.folder, summary
