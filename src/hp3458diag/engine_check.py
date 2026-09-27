"""Operator-started live check of the WP-02 engine (closes its simulator-only points).

1. engine.preflight() including the raw TERM? reply; existing memory archived (evidence).
2. Probes: DCV 10 + OCOMP OFF -> ERR?/OCOMP?; DELAY 0 / DELAY 1 -> DELAY? and ERR?.
3. engine.run_block on a DCV 10 block and an OHMF 10 block (OCOMP ON, DELAY 1), settling
   0 s (a sequence check, not a diagnostic measurement); evidence saved before release.
4. TARM HOLD and DCV 10 at the end so no ohms current source stays selected.
OHMF sends the 10 mA test current into the operator's DUT. No calibration, no ACAL.
"""

import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from . import __version__
from .config import LabConfig
from .domain import TestPoint
from .engine import AcquisitionEngine, EngineError
from .identify import _jsonable
from .instrument import ResponseFormatError, read_error_register, software_environment
from .persistence import _git_revision, _write_new
from .transport.visa import VisaTransport

LIVE_BLOCKS = (
    TestPoint("LIVE-DCV10-NPLC1-N10", "INPUT", "DCV", 10.0, 1, 0.0, n=10, settling_s=0),
    TestPoint("LIVE-OHMF10-NPLC1-N5", "DUT", "OHMF", 10.0, 1, 10.0, n=5, ocomp=True,
              delay_s=1.0, settling_s=0),
)


def probes(transport: Any) -> dict[str, Any]:
    record: dict[str, Any] = {}
    transport.write("TARM HOLD")
    transport.write("DCV 10")
    transport.write("OCOMP OFF")
    record["ocomp_off_in_dcv"] = {"errors": read_error_register(transport),
                                  "OCOMP?": transport.query_raw("OCOMP?")}
    for value in ("0", "1"):
        transport.write(f"DELAY {value}")
        record[f"delay_{value}"] = {"DELAY?": transport.query_raw("DELAY?"),
                                    "errors": read_error_register(transport)}
    return record


def run_engine_check(config: LabConfig, output: Path, dut_id: str, dut_nominal_ohm: float,
                     transport_factory: Callable[..., Any] = VisaTransport,
                     clock: Callable[[], float] | None = None,
                     sleep: Callable[[float], None] | None = None) -> tuple[str, Path]:
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    blocks = (LIVE_BLOCKS[0],
              replace(LIVE_BLOCKS[1], dut_id=dut_id, nominal=dut_nominal_ohm))  # validates
    folder = output / f"engine-check-{uuid.uuid4()}"
    folder.mkdir(parents=True, exist_ok=False)
    transport = transport_factory(replace(config.visa, timeout_ms=10_000,
                                          read_termination="lf"),
                                  journal=folder / "journal.jsonl")
    timing = {k: v for k, v in (("clock", clock), ("sleep", sleep)) if v is not None}
    engine = AcquisitionEngine(transport, profile=config.reading_profile,
                               stat_crosscheck=config.stat_crosscheck,
                               accepted_identities=config.accepted_identities, **timing)
    record: dict[str, Any] = {"blocks": []}
    status, detail = "TRANSPORT_ERROR", None
    try:
        transport.open()
        record["preflight"] = engine.preflight()
        record["foreign_memory_before"] = dict(engine.foreign_memory)
        if engine.foreign_memory["mcount"]:
            if 2 <= engine.foreign_memory["mcount"] <= 100:
                record["archived_memory_raw"] = engine.archive_foreign_memory()
            else:
                engine.discard_foreign_memory("engine check: single/oversized old block")
        record["baseline"] = engine.baseline()
        record["probes"] = probes(transport)
        for point in blocks:
            outcome, block = engine.run_block(point)
            record["blocks"].append(outcome)
            _write_new(folder / f"block-{point.test_id}.json",
                       json.dumps(_jsonable({"simulation": False, "test_point": point,
                                             "outcome": outcome}), indent=2))
            if block is None or engine.fault is not None:
                break
            engine.release(block, saved=True)
        states = [outcome.state.value for outcome in record["blocks"]]
        status = "PASS" if states == ["VALIDATED", "VALIDATED"] else "CHECK_RESULTS"
    except EngineError as exc:
        status, detail = "ENGINE_STOPPED", str(exc)
    except ResponseFormatError as exc:
        status, detail = "RESPONSE_INVALID", str(exc)
    except (OSError, RuntimeError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    # Safe end also after an engine stop: leave no ohms current source selected, unless
    # the memory is still owned or the bus state is unknown (then nothing is sent).
    if (engine.pending is None and getattr(transport, "is_open", True)
            and not getattr(transport, "framing_unknown", False)):
        try:
            transport.write("TARM HOLD")
            transport.write("DCV 10")
            record["safe_end"] = "TARM HOLD, DCV 10"
        except (OSError, RuntimeError) as exc:
            record["safe_end"] = f"failed: {type(exc).__name__}: {exc}"
    record["events"] = engine.events
    _write_new(folder / "engine-check.json", json.dumps(_jsonable({
        "schema_version": 1, "kind": "wp02_engine_live_check", "simulation": False,
        "status": status, "detail": detail, "dut_id": dut_id,
        "dut_nominal_ohm_not_calibrated": dut_nominal_ohm,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "backend_poisoned": getattr(transport, "backend_poisoned", False),
        "software_version": __version__, "git_revision": _git_revision(),
        "software_environment": software_environment(), "record": record,
        "trace": list(getattr(transport, "trace", [])),
    }), ensure_ascii=False, allow_nan=False, indent=2))  # before a possibly hanging close
    try:
        transport.close()
    except OSError as exc:
        detail = f"{detail or ''} close: {exc}".strip()
    return status, folder
