"""Explicit, operator-started recovery of an interrupted session (WP-04).

Never measures: no MEM FIFO, TARM SGL, PRESET, configuration or MFORMAT is sent. The
decision is recorded durably and the session is closed as RECOVERED:<decision>:

  INSTRUMENT_BUSY            serial poll not READY: stop, nothing else is sent
  PROFILE_UNVERIFIED         reading profile not verified for this firmware
  NO_PENDING_BLOCK           every armed block was released: nothing to recover
  MEMORY_COUNT_DIFFERS       MCOUNT? != N: memory is not that block (no RMEM, D21)
  MEMORY_CONFIG_DIFFERS      NPLC?/OCOMP?/DELAY? do not match the block's request
  MEMORY_MATCHES_SAVED       saved block: RMEM bytes equal the saved consensus bytes
  MEMORY_DIFFERS_FROM_SAVED  saved block: RMEM bytes differ
  UNSAVED_BLOCK_IN_MEMORY    count and configuration consistent, never saved; with
                             read_memory=True it is validated as a RETRY read and saved
                             as a new recovered file (RECOVERED_<status>)
  INVALID_BLOCK_IN_MEMORY    saved INVALID block (no consensus, STOPPED_ON_INVALID); with
                             read_memory=True a RETRY read as above
"""

import base64
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any
from . import tolerances
from .domain import TestPoint
from .instrument import preflight_identity
from .memory_reader import validate_memory
from .replies import parse_reply
from .session_store import SessionStore, pending_from_events, scan_journal, sha256_bytes
from .validation import ReadingProfile, parse_scalar

READY = 16


def safe_end(transport: Any) -> dict[str, Any]:
    """Separate, explicit operator step after a recovery decision (H07 live finding):
    TARM HOLD + DCV 10 so that no ohms test current stays on the DUT. Refused while the
    instrument is busy; the reading memory is not touched (MCOUNT? before/after)."""
    if not transport.serial_poll() & READY:
        return {"done": False, "reason": "instrument busy"}
    before = parse_reply("MCOUNT?", transport.query_raw("MCOUNT?")).value
    transport.write("TARM HOLD")
    transport.write("DCV 10")
    tarm = parse_reply("TARM?", transport.query_raw("TARM?")).meaning
    after = parse_reply("MCOUNT?", transport.query_raw("MCOUNT?")).value
    return {"done": tarm == "HOLD", "tarm": tarm, "mcount_before": before,
            "mcount_after": after}


def _saved_consensus_raw(block_file: Path) -> bytes | None:
    payload = json.loads(block_file.read_text(encoding="utf-8"))
    # engine blocks keep the result in "outcome"; recovered and RETRY files at the top level
    result = (payload.get("outcome") or {}).get("result") or payload.get("result") or {}
    reads = result.get("memory_reads") or {}
    for attempt in reads.get("attempts", []):
        if attempt.get("number") in reads.get("selected_attempts", []) and \
                attempt.get("raw_base64"):
            return base64.b64decode(attempt["raw_base64"])
    return None


def recoverable(meta: dict[str, Any]) -> bool:
    """OPEN (interrupted), or closed by an earlier recovery (e.g. INSTRUMENT_BUSY first,
    then again after the block ends: H07 step 2). Never a normally closed session."""
    return meta.get("lifecycle") == "OPEN" or str(meta.get("status", "")).startswith(
        "RECOVERED:")


def recover_session(folder: Path, transport: Any, *, profile: ReadingProfile,
                    stat_crosscheck: str, accepted_identities: tuple[str, ...],
                    reason: str, read_memory: bool = False) -> dict[str, Any]:
    if not reason.strip():
        raise ValueError("Recovery requires an operator reason")
    folder = Path(folder)
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    if not recoverable(meta):  # a normally closed session keeps its final status
        raise ValueError(f"Not an interrupted session: {meta.get('lifecycle')} "
                         f"{meta.get('status')}")
    store = SessionStore.reopen(folder)
    pending, saved = pending_from_events(scan_journal(folder / "events.jsonl").events)
    report: dict[str, Any] = {"reason": reason, "pending_block": pending, "saved": saved,
                              "read_memory_requested": read_memory}
    store.journal.append("recovery_started", reason=reason,
                         pending_block=pending and pending["block_id"], saved=bool(saved))

    def finish(decision: str) -> dict[str, Any]:
        report["decision"] = decision
        store.save_record(f"recovery-{uuid.uuid4()}.json", report)
        store.journal.append("recovery_finished", decision=decision)
        store.close(status=f"RECOVERED:{decision}")
        return report

    status = transport.serial_poll()
    report["serial_poll"] = status
    if not status & READY:
        return finish("INSTRUMENT_BUSY")
    preflight = preflight_identity(transport, accepted_identities)
    report["preflight"] = preflight
    if not profile.verified_for(preflight.revision):
        return finish("PROFILE_UNVERIFIED")
    raw_count = transport.query_raw("MCOUNT?")
    count = parse_reply("MCOUNT?", raw_count).value
    readback = {q: transport.query_raw(q) for q in ("TARM?", "NPLC?", "OCOMP?", "DELAY?")}
    report.update(mcount=count, readback=readback)
    if pending is None:
        return finish("NO_PENDING_BLOCK")
    point = TestPoint(**pending["point"])
    if count != point.n:
        return finish("MEMORY_COUNT_DIFFERS")
    nplc = parse_scalar(readback["NPLC?"], profile)[0]
    delay = parse_scalar(readback["DELAY?"], profile)[0]
    ocomp = parse_reply("OCOMP?", readback["OCOMP?"]).value
    if not (tolerances.NPLC.accepts(Decimal(point.nplc), nplc)
            and tolerances.DELAY.accepts(Decimal(str(point.delay_s)), delay)
            and ocomp == int(point.ocomp)):
        return finish("MEMORY_CONFIG_DIFFERS")
    if parse_reply("MCOUNT?", transport.query_raw("MCOUNT?")).value != point.n:
        return finish("MEMORY_COUNT_DIFFERS")  # re-checked right before any RMEM (D21)
    expected = (_saved_consensus_raw(folder / "blocks" / saved["file"])
                if saved is not None else None)
    if expected is not None:
        raw = transport.query_raw(f"RMEM 1,{point.n}")
        report["rmem_sha256"] = sha256_bytes(raw)
        report["saved_sha256"] = sha256_bytes(expected)
        return finish("MEMORY_MATCHES_SAVED" if raw == expected else "MEMORY_DIFFERS_FROM_SAVED")
    # unsaved, or saved without a consensus (INVALID: STOPPED_ON_INVALID) -> RETRY read
    if not read_memory:
        return finish("UNSAVED_BLOCK_IN_MEMORY" if saved is None else "INVALID_BLOCK_IN_MEMORY")
    result = validate_memory(transport, point, absolute_tolerance=0.0, relative_tolerance=0.0,
                             profile=profile, stat_crosscheck=stat_crosscheck,
                             read_kind="RETRY")
    store.save_block({"simulation": store.metadata.get("simulation"), "recovered": True,
                      "recovery_reason": reason, "test_point": point, "result": result},
                     point.test_id, pending["block_id"])
    return finish(f"RECOVERED_{result.status}")
