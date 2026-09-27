"""H01 RMEM frame check: one small DCV block, recalled with LF and with EOI framing.

Operator-started only. Sequence (docs/COMMANDS.md): preflight, PRESET NORM + baseline,
DCV 10, NPLC 1, readback, MEM FIFO (only with MCOUNT? evidence of what is cleared),
count0, NRDGS N,AUTO, TARM SGL, serial-poll READY, MCOUNT?=N, TARM?=HOLD, MEM OFF,
RMEM 1,N by two-read consensus (LF framing), then one RMEM with EOI framing.
An EOI timeout is recovered only by a logged SDC; memory integrity is then re-verified.
DCV sources no current into the input. No OHMF, calibration or ACAL command is sent.
"""

import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from . import __version__
from .commands import BASELINE
from .config import LabConfig
from .identify import _jsonable
from .instrument import (IdentityError, ResponseFormatError, _line, preflight_identity,
                         read_error_register, software_environment)
from .persistence import _git_revision, _write_new
from .transport.base import TransportTimeout
from .transport.visa import VisaSettings, VisaTransport
from .validation import parse_readings, read_consistent

DCV10_BOUNDS = (-12.0, 12.0)  # DCV 10 full scale 12 V (pp. 183-186)
READY = 16                    # serial poll bit 4 (p. 306)
INBUF_SETTLE_S = 0.2          # let the instrument take TARM SGL from its input buffer
INBUF_POLL_S = 0.25           # serial-poll pacing while a buffered block runs
TARM_HOLD = "4"               # TARM? numeric equivalent of HOLD


class FrameCheckStop(RuntimeError):
    pass


def _integer(raw: bytes) -> int:
    text = _line(raw).strip()
    if not text.lstrip("+-").isdigit():
        raise ResponseFormatError("Integer reply expected", raw)
    return int(text)


def _last_statuses(transport: Any) -> list[str]:
    return [hex(s) for s in transport.trace[-1].read_statuses]


def acquire_block(transport: Any, n: int, ready_deadline_s: float,
                  steps: dict[str, Any]) -> None:
    before = _integer(transport.query_raw("MCOUNT?"))
    steps["mcount_before"] = before
    if before != 0:
        raise FrameCheckStop(f"Instrument memory holds {before} readings; not cleared")
    for command in BASELINE + ("DCV 10", "NPLC 1"):
        transport.write(command)
    steps["readback"] = {query: transport.query_raw(query) for query in
                         ("NPLC?", "AZERO?", "MFORMAT?", "OFORMAT?", "END?", "TARM?", "MEM?")}
    configured = read_error_register(transport)
    steps["errors_after_config"] = configured
    if configured.value != 0:
        raise FrameCheckStop(f"Configuration errors: {configured.bits or configured.error}")
    if _line(steps["readback"]["TARM?"]).strip() != TARM_HOLD:
        raise FrameCheckStop("TARM HOLD not confirmed before acquisition")
    acquire_configured(transport, n, ready_deadline_s, steps)


def acquire_configured(transport: Any, n: int, ready_deadline_s: float,
                       steps: dict[str, Any], inbuf: bool = False,
                       append_to: int | None = None) -> None:
    """MEM FIFO -> count0 -> NRDGS -> TARM SGL -> READY -> MCOUNT?=N, HOLD -> MEM OFF.

    Only after the previous memory content is saved and HOLD is confirmed.
    inbuf=False: TARM SGL holds the bus until the block ends (INBUF OFF, p. 75); on the
    NI GPIB-USB-HS + Keysight VISA this failed after ~15-150 s regardless of the VISA
    timeout (H02 evidence). inbuf=True: INBUF ON releases the bus at once and READY is
    polled (pp. 186-187); MCOUNT? = N then proves completion. INBUF OFF afterwards.
    append_to=K: MEM CONT keeps the K stored readings (pp. 197, 230); expect K+N.
    """
    start = start_block(transport, n, steps, inbuf, append_to)
    finish_block(transport, (append_to or 0) + n, ready_deadline_s, steps, inbuf, start)


def start_block(transport: Any, n: int, steps: dict[str, Any], inbuf: bool,
                append_to: int | None = None) -> float:
    if append_to is None:
        transport.write("MEM FIFO")
        before, key = 0, "mcount_after_fifo"
    else:
        transport.write("MEM CONT")
        before, key = append_to, "mcount_after_cont"
    count0 = _integer(transport.query_raw("MCOUNT?"))
    steps[key] = count0
    if count0 != before:
        raise FrameCheckStop(f"MCOUNT? before acquisition is {count0}, expected {before}")
    transport.write(f"NRDGS {n},AUTO")
    if inbuf:
        transport.write("INBUF ON")
        steps["inbuf_readback"] = state = transport.query_raw("INBUF?")
        if _line(state).strip() != "1":
            raise FrameCheckStop(f"INBUF ON not confirmed: {state!r}")
    start = time.monotonic()
    transport.write("TARM SGL")
    steps["tarm_sgl_write_s"] = round(time.monotonic() - start, 3)
    if inbuf:
        time.sleep(INBUF_SETTLE_S)
    return start


def finish_block(transport: Any, expected: int, ready_deadline_s: float,
                 steps: dict[str, Any], inbuf: bool, start: float) -> None:
    polls = []
    while True:
        status = transport.serial_poll()
        polls.append((round(time.monotonic() - start, 3), status))
        if status & READY:
            break
        if time.monotonic() - start > ready_deadline_s:
            steps["serial_polls"] = polls
            raise FrameCheckStop("READY not seen before deadline")
        # poll pacing only; completion is proven by READY + MCOUNT?
        time.sleep(INBUF_POLL_S if inbuf else 0.05)
    steps["serial_polls"] = polls
    steps["ready_low_seen"] = any(not status & READY for _, status in polls)
    steps["block_duration_s"] = polls[-1][0]
    count = _integer(transport.query_raw("MCOUNT?"))
    steps["mcount_after_acquisition"] = count
    tarm = _line(transport.query_raw("TARM?")).strip()
    steps["tarm_after_acquisition"] = tarm
    if count != expected:
        raise FrameCheckStop(f"MCOUNT? is {count}, expected {expected}")
    if tarm != TARM_HOLD:
        raise FrameCheckStop(f"TARM? is {tarm} after TARM SGL, expected HOLD")
    transport.write("MEM OFF")  # keeps the stored readings (p. 196)
    if inbuf:
        transport.write("INBUF OFF")
    steps["errors_after_acquisition"] = read_error_register(transport)


def recall_and_compare(transport: Any, n: int, steps: dict[str, Any]) -> str:
    def recall() -> bytes:
        raw = transport.query_raw(f"RMEM 1,{n}")
        steps.setdefault("lf_read_statuses", []).append(_last_statuses(transport))
        return raw

    profile = steps["reading_profile"]
    lf = read_consistent(recall, lambda raw: parse_readings(raw, n, DCV10_BOUNDS, profile))
    steps["lf_consensus"] = lf
    lf_raw = next((transport.trace[i].received for i in range(len(transport.trace) - 1, -1, -1)
                   if transport.trace[i].command == f"RMEM 1,{n}"
                   and transport.trace[i].error is None), None)
    steps["lf_last_raw_length"] = len(lf_raw) if lf_raw is not None else None
    transport.set_read_termination("eoi")
    try:
        eoi_raw = transport.query_raw(f"RMEM 1,{n}")
    except TransportTimeout as exc:
        steps["eoi"] = {"result": "TIMEOUT", "partial": exc.partial}
        transport.clear_device("H01 frame check: RMEM timeout with EOI-only framing")
        transport.set_read_termination("lf")
        steps["mcount_after_sdc"] = _integer(transport.query_raw("MCOUNT?"))
        again = transport.query_raw(f"RMEM 1,{n}")
        steps["rmem_after_sdc_identical"] = again == lf_raw
        return "EOI_TIMEOUT"
    steps["eoi"] = {"result": "COMPLETED", "raw_length": len(eoi_raw),
                    "read_statuses": _last_statuses(transport),
                    "identical_to_lf": eoi_raw == lf_raw}
    transport.set_read_termination("lf")
    return "EOI_PRESENT" if eoi_raw == lf_raw else "EOI_REPLY_DIFFERS"


def run_frame_check(config: LabConfig, output: Path, n: int = 100,
                    transport_factory: Callable[[VisaSettings], Any] = VisaTransport,
                    io_timeout_ms: int = 30_000, chunk_bytes: int = 256,
                    ready_deadline_s: float = 60.0) -> tuple[str, Path]:
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    if type(n) is not int or not 2 <= n <= 100:
        raise ValueError("Sample count must be 2..100 (allowlist)")
    settings = replace(config.visa, timeout_ms=io_timeout_ms, chunk_bytes=chunk_bytes,
                       read_termination="lf")
    transport = transport_factory(settings)
    steps: dict[str, Any] = {"reading_profile": config.reading_profile}
    status, detail = "TRANSPORT_ERROR", None
    try:
        transport.open()
        steps["preflight"] = preflight_identity(transport, config.accepted_identities)
        acquire_block(transport, n, ready_deadline_s, steps)
        status = recall_and_compare(transport, n, steps)
        steps["errors_final"] = read_error_register(transport)
        steps["tarm_final"] = transport.query_raw("TARM?")
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
    folder = output / f"framecheck-{session_id}"
    folder.mkdir(exist_ok=False)
    payload = {
        "schema_version": 1, "kind": "h01_rmem_frame_check", "session_uuid": session_id,
        "created_utc": datetime.now(timezone.utc).isoformat(), "simulation": False,
        "status": status, "detail": detail, "samples": n, "config_source": config.source,
        "visa_settings": settings, "backend_info": getattr(transport, "backend_info", {}),
        "software_version": __version__, "git_revision": _git_revision(),
        "software_environment": software_environment(), "steps": steps,
        "trace": list(getattr(transport, "trace", [])),
    }
    _write_new(folder / "framecheck.json", json.dumps(_jsonable(payload), ensure_ascii=False,
                                                      allow_nan=False, indent=2))
    return status, folder
