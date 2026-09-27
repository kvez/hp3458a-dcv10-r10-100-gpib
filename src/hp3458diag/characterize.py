"""H05 / C21 characterization: reading format and DMM STAT vs PC on several blocks.

Operator-started only, with the DUT named by the operator. Per block: HOLD, function
and range, AZERO ON, OCOMP (OHMF only), NPLC, readback, ERR?, then the documented
acquisition (frame_check.acquire_configured, INBUF ON + serial-poll READY) and
validate_memory with the configured
profile. Existing memory is read and archived before the first MEM FIFO.

The STAT comparison tolerance is derived from each DMM reply's own resolution
(its last displayed digit), never from observed differences: `within_half_quantum`
means the PC value rounds to the DMM text. Both ddof=1 and ddof=0 PC deviations are
reported so the firmware SDEV definition can be read from N=3 blocks.
"""

import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from . import __version__
from .commands import BASELINE
from .config import LabConfig
from .domain import TestPoint
from .frame_check import FrameCheckStop, _integer, acquire_configured
from .identify import _jsonable
from .instrument import (IdentityError, ResponseFormatError, _line, preflight_identity,
                         read_error_register, software_environment)
from .memory_reader import validate_memory
from .persistence import _git_revision, _write_new
from .transport.visa import VisaSettings, VisaTransport
from .stat_check import resolution_table

# Simulation values only; the verdict here is the resolution-based table, not these.
SIM_ABS, SIM_REL = 1e-12, 1e-8


@dataclass(frozen=True)
class BlockSpec:
    label: str
    mode: str
    range_value: float
    nplc: int
    ocomp: bool
    n: int


DEFAULT_BLOCKS = (
    BlockSpec("DCV0.1-N3", "DCV", 0.1, 1, False, 3),
    BlockSpec("DCV0.1-N100", "DCV", 0.1, 1, False, 100),
    BlockSpec("OHMF10-N3", "OHMF", 10.0, 1, True, 3),
    BlockSpec("OHMF10-N100", "OHMF", 10.0, 1, True, 100),
    BlockSpec("OHMF100-N3", "OHMF", 100.0, 1, True, 3),
    BlockSpec("OHMF100-N100", "OHMF", 100.0, 1, True, 100),
    BlockSpec("DCV10-N10", "DCV", 10.0, 1, False, 10),  # last: leave no ohms source selected
)
# H05 at NPLC 10/100 (STAT arithmetic vs integration time); DCV last again.
NPLC_BLOCKS = (
    BlockSpec("OHMF10-NPLC10-N10", "OHMF", 10.0, 10, True, 10),
    BlockSpec("OHMF10-NPLC100-N3", "OHMF", 10.0, 100, True, 3),
    BlockSpec("DCV0.1-NPLC10-N10", "DCV", 0.1, 10, False, 10),
    BlockSpec("DCV10-NPLC100-N10", "DCV", 10.0, 100, False, 10),
)


def configure_block(transport: Any, spec: BlockSpec, steps: dict[str, Any]) -> None:
    # MMATH OFF: the previous block's MMATH STAT must not act on the new acquisition.
    commands = ["TARM HOLD", "MMATH OFF", f"{spec.mode} {spec.range_value:g}", "AZERO ON"]
    if spec.mode == "OHMF":
        commands.append("OCOMP ON" if spec.ocomp else "OCOMP OFF")
    commands.append(f"NPLC {spec.nplc}")
    for command in commands:
        transport.write(command)
    queries = ["NPLC?", "AZERO?", "TARM?", "DELAY?"] + (["OCOMP?"] if spec.mode == "OHMF" else [])
    steps["readback"] = {query: transport.query_raw(query) for query in queries}
    steps["errors_after_config"] = errors = read_error_register(transport)
    if errors.value != 0:
        raise FrameCheckStop(f"{spec.label}: configuration errors {errors.bits or errors.error}")
    if _line(steps["readback"]["TARM?"]).strip() != "4":
        raise FrameCheckStop(f"{spec.label}: TARM HOLD not confirmed")


def run_characterization(config: LabConfig, output: Path, dut_id: str, dut_nominal_ohm: float,
                         blocks: tuple[BlockSpec, ...] = DEFAULT_BLOCKS,
                         transport_factory: Callable[[VisaSettings], Any] = VisaTransport,
                         ready_deadline_s: float = 600.0) -> tuple[str, Path]:
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    profile = config.reading_profile
    points = [TestPoint(spec.label, dut_id if spec.mode == "OHMF" else "INPUT", spec.mode,
                        spec.range_value, spec.nplc,
                        dut_nominal_ohm if spec.mode == "OHMF" else 0.0, n=spec.n,
                        ocomp=spec.ocomp) for spec in blocks]  # validates before any I/O
    settings = replace(config.visa, timeout_ms=60_000, read_termination="lf")
    transport = transport_factory(settings)
    record: dict[str, Any] = {"profile": profile.name, "blocks": []}
    status, detail = "TRANSPORT_ERROR", None
    try:
        transport.open()
        preflight = preflight_identity(transport, config.accepted_identities)
        record["preflight"] = preflight
        if not profile.verified_for(preflight.revision):
            raise FrameCheckStop(f"Profile {profile.name} not verified for REV "
                                 f"{','.join(preflight.revision)}")
        existing = _integer(transport.query_raw("MCOUNT?"))
        record["mcount_before"] = existing
        if existing:
            if not 2 <= existing <= 100:
                raise FrameCheckStop(f"{existing} readings in memory cannot be archived here")
            record["archived_memory_raw"] = transport.query_raw(f"RMEM 1,{existing}")
        for command in BASELINE:
            transport.write(command)
        for spec, point in zip(blocks, points):
            steps: dict[str, Any] = {"spec": spec}
            record["blocks"].append(steps)
            configure_block(transport, spec, steps)
            steps["temp_start"] = transport.query_raw("TEMP?")
            acquire_configured(transport, spec.n, ready_deadline_s, steps, inbuf=True)
            steps["temp_end"] = transport.query_raw("TEMP?")
            result = validate_memory(transport, point, absolute_tolerance=SIM_ABS,
                                     relative_tolerance=SIM_REL, profile=profile,
                                     stat_crosscheck="dmm_half_quantum")
            steps["block_result"] = result
            if result.status == "TRANSPORT_ERROR":
                raise FrameCheckStop(f"{spec.label}: transport error; stopping")
            if result.chronological_values and result.statistic_reads:
                steps["resolution_table"] = resolution_table(
                    result.chronological_values, result.statistic_reads, profile)
        record["errors_final"] = read_error_register(transport)
        status = "COMPLETED"
    except IdentityError as exc:
        status, detail = "IDENTITY_REJECTED", str(exc)
    except FrameCheckStop as exc:
        status, detail = "STOPPED", str(exc)
    except ResponseFormatError as exc:
        status, detail = "RESPONSE_INVALID", str(exc)
    except (OSError, RuntimeError) as exc:
        detail = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            transport.close()
        except OSError as exc:
            detail = f"{detail or ''} close: {exc}".strip()
    session_id = str(uuid.uuid4())
    output.mkdir(parents=True, exist_ok=True)
    folder = output / f"characterize-{session_id}"
    folder.mkdir(exist_ok=False)
    payload = {
        "schema_version": 1, "kind": "h05_c21_characterization", "session_uuid": session_id,
        "created_utc": datetime.now(timezone.utc).isoformat(), "simulation": False,
        "status": status, "detail": detail, "dut_id": dut_id,
        "dut_nominal_ohm_not_calibrated": dut_nominal_ohm, "config_source": config.source,
        "visa_settings": settings, "backend_info": getattr(transport, "backend_info", {}),
        "software_version": __version__, "git_revision": _git_revision(),
        "software_environment": software_environment(), "record": record,
        "trace": list(getattr(transport, "trace", [])),
    }
    _write_new(folder / "characterize.json", json.dumps(_jsonable(payload), ensure_ascii=False,
                                                        allow_nan=False, indent=2))
    return status, folder
