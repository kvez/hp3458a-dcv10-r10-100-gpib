"""Run plan points through the engine with durable evidence (WP-04).

A block is released only after its outcome file is durable. If saving fails (disk full,
permission), the session stops with STORAGE_FAULT and the instrument memory stays owned
by that block, so a later explicit recovery can still read it.
"""

from typing import Any
from .domain import TestPoint
from .engine import AcquisitionEngine, BlockState, EngineError
from .session_store import SessionStore, StorageError


def validation_label(outcome: Any) -> str | None:
    if outcome.result is not None:
        return outcome.result.status
    if outcome.partial_result is not None:
        return f"partial {outcome.partial_result.status}"
    return None


def run_points(store: SessionStore, engine: AcquisitionEngine, points: list[TestPoint],
               settle_s: float | None = None) -> tuple[str, list[tuple[str, str, Any]]]:
    summary: list[tuple[str, str, Any]] = []
    for point in points:
        try:
            outcome, block = engine.run_block(point, settle_s)
        except EngineError as exc:
            store.journal.append("session_stopped", reason=str(exc), test_id=point.test_id)
            return "ENGINE_STOPPED", summary
        summary.append((point.test_id, outcome.state.value, validation_label(outcome)))
        try:
            store.save_block({"simulation": store.metadata.get("simulation"),
                              "test_point": point, "outcome": outcome},
                             point.test_id, outcome.block_id)
        except (StorageError, FileExistsError):
            return "STORAGE_FAULT", summary  # block NOT released: memory stays owned
        if outcome.state == BlockState.FAULT:
            return "STOPPED_ON_FAULT", summary  # operator recovery required, never automatic
        if outcome.state == BlockState.INVALID:
            return "STOPPED_ON_INVALID", summary  # not released: RETRY reads the same memory
        if block is not None:
            try:
                engine.release(block, saved=True)
            except StorageError:
                return "STORAGE_FAULT", summary
        if outcome.state == BlockState.ABORTED:
            return "ABORTED", summary  # saved and released; no further block
        if outcome.paused_after:
            return "PAUSED", summary
    return "COMPLETED", summary
