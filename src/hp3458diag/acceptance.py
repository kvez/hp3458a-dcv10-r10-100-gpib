"""Operator-started hardware acceptance checks H03, H04 and H02 (abort) in one session.

H03: a known, asymmetric sequence in one memory (DCV 2 readings ~0 V, OHMF 3 readings
     ~DUT ohms, DCV 4 readings), appended with MEM CONT (pp. 197, 230); RMEM 1,9 must
     return it newest first: 4x ~0 V, 3x ~DUT, 2x ~0 V.
H04: every applied setting is read back and compared with what was requested; MSIZE?
     capacity; a deliberate RMEM beyond MCOUNT must be detected (error bit, invalid
     reply or timeout) and the stored memory must stay intact.
H02: an NPLC 100 block is aborted with a logged SDC (p. 304), then TARM HOLD. ABORTED
     only with TARM?=HOLD and a stable MCOUNT? below N; otherwise FAULT. Partial readings
     are saved as evidence, never as a result.
DCV sources no current; OHMF sources the range test current into the operator's DUT.
"""

import json
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable
from . import __version__
from . import frame_check
from .commands import BASELINE
from .config import LabConfig
from .frame_check import (READY, TARM_HOLD, FrameCheckStop, _integer, acquire_configured,
                          start_block)
from .identify import _jsonable
from .instrument import (IdentityError, ResponseFormatError, _line, preflight_identity,
                         read_error_register, software_environment)
from .persistence import _git_revision, _write_new
from .transport.base import TransportIOError, TransportTimeout
from .transport.visa import VisaSettings, VisaTransport
from .validation import DataValidationError, parse_readings, parse_scalar, read_consistent

BOUNDS = (-12.0, 12.0)  # DCV 10 and OHMF 10 full scale
H03_SEGMENTS = (("DCV", 10.0, 2), ("OHMF", 10.0, 3), ("DCV", 10.0, 4))
ABORT_STABILITY_S = 6.0  # > one NPLC 100 reading (~4 s with AZERO): a running block would count


def configure(transport: Any, mode: str, range_value: float, nplc: int,
              steps: dict[str, Any]) -> None:
    commands = ["TARM HOLD", "MMATH OFF", f"{mode} {range_value:g}", "AZERO ON"]
    if mode == "OHMF":
        commands.append("OCOMP ON")
    commands.append(f"NPLC {nplc}")
    for command in commands:
        transport.write(command)
    steps["errors_after_config"] = errors = read_error_register(transport)
    if errors.value != 0:
        raise FrameCheckStop(f"Configuration errors: {errors.bits or errors.error}")


def classify(value: Decimal, dut_ohm: float) -> str:
    if abs(value) < Decimal("0.001"):
        return "ZERO_V"
    if abs(value - Decimal(str(dut_ohm))) < Decimal(str(dut_ohm)) * Decimal("0.1"):
        return "DUT_OHM"
    return "OTHER"


def expected_h03() -> list[str]:
    labels = []
    for mode, _, n in reversed(H03_SEGMENTS):  # newest segment first
        labels += ["ZERO_V" if mode == "DCV" else "DUT_OHM"] * n
    return labels


def check_h03(transport: Any, profile: Any, dut_ohm: float, deadline_s: float,
              record: dict[str, Any]) -> int:
    steps: dict[str, Any] = {"segments": []}
    record["h03"] = steps
    total = 0
    for index, (mode, range_value, n) in enumerate(H03_SEGMENTS):
        segment: dict[str, Any] = {"mode": mode, "n": n}
        steps["segments"].append(segment)
        configure(transport, mode, range_value, 1, segment)
        acquire_configured(transport, n, deadline_s, segment, inbuf=True,
                           append_to=None if index == 0 else total)
        total += n
    reads = read_consistent(lambda: transport.query_raw(f"RMEM 1,{total}"),
                            lambda raw: parse_readings(raw, total, BOUNDS, profile))
    steps["rmem"] = reads
    if reads.values is None:
        steps["result"] = "INVALID_READ"
        return total
    steps["observed_newest_first"] = [classify(v, dut_ohm) for v in reads.values]
    steps["expected_newest_first"] = expected_h03()
    steps["result"] = ("PASS" if steps["observed_newest_first"] == steps["expected_newest_first"]
                       else "FAIL")
    return total


def check_h04(transport: Any, profile: Any, stored: int, record: dict[str, Any]) -> None:
    steps: dict[str, Any] = {}
    record["h04"] = steps
    # State after H03: DCV 10, NPLC 1, AZERO ON, TARM HOLD, RMEM -> MEM OFF, INBUF OFF.
    expected = {"NPLC?": Decimal(1), "AZERO?": "1", "TARM?": TARM_HOLD, "MEM?": "0",
                "MFORMAT?": "1", "OFORMAT?": "1", "END?": "1", "INBUF?": "0"}
    readback = {}
    for query, wanted in expected.items():
        raw = transport.query_raw(query)
        got = (parse_scalar(raw, profile)[0] if isinstance(wanted, Decimal)
               else _line(raw).strip())
        readback[query] = {"raw": raw, "expected": wanted, "match": got == wanted}
    lfreq = parse_scalar(transport.query_raw("LFREQ?"), profile)[0]
    readback["LFREQ?"] = {"value": lfreq, "match": Decimal(49) < lfreq < Decimal(51)}
    steps["readback"] = readback
    msize = _line(transport.query_raw("MSIZE?")).split(",")
    capacity = int(msize[0].strip()) // 16  # ASCII: 16 bytes per stored reading (C04)
    steps["msize"] = {"fields": msize, "ascii_reading_capacity": capacity,
                      "match": capacity >= 100}
    count = _integer(transport.query_raw("MCOUNT?"))
    before = transport.query_raw(f"RMEM 1,{stored}")
    beyond: dict[str, Any] = {"requested": stored + 1, "mcount": count}
    steps["beyond_mcount"] = beyond
    try:
        raw = transport.query_raw(f"RMEM 1,{stored + 1}")
        beyond["raw"] = raw
        try:
            parse_readings(raw, stored + 1, BOUNDS, profile)
            beyond["reply_valid"] = True
        except DataValidationError as exc:
            beyond["reply_valid"], beyond["reply_error"] = False, str(exc)
    except (TransportTimeout, TransportIOError) as exc:
        beyond["transport"] = f"{type(exc).__name__}: {exc}"
        beyond["partial"] = exc.partial
        transport.clear_device("H04: recovery after deliberate RMEM beyond MCOUNT")
        transport.write("TARM HOLD")
    beyond["errors"] = errors = read_error_register(transport)
    detected = bool(errors.value) or beyond.get("reply_valid") is False or "transport" in beyond
    after_count = _integer(transport.query_raw("MCOUNT?"))
    after = transport.query_raw(f"RMEM 1,{stored}")
    beyond.update(detected=detected, mcount_after=after_count,
                  memory_intact=after_count == stored and after == before)
    matches = [item["match"] for item in readback.values()] + [steps["msize"]["match"]]
    steps["result"] = ("PASS" if all(matches) and count == stored and detected
                       and beyond["memory_intact"] else "FAIL")


def check_h02_abort(transport: Any, profile: Any, n: int, abort_after_s: float,
                    record: dict[str, Any]) -> None:
    steps: dict[str, Any] = {"n": n, "nplc": 100}
    record["h02_abort"] = steps
    configure(transport, "DCV", 10.0, 100, steps)
    start = start_block(transport, n, steps, inbuf=True)
    polls = []
    while True:
        status = transport.serial_poll()
        polls.append((round(time.monotonic() - start, 3), status))
        if status & READY:
            steps["serial_polls"] = polls
            steps["result"] = "NOT_TESTED_BLOCK_FINISHED_BEFORE_ABORT"
            transport.write("INBUF OFF")
            return
        if time.monotonic() - start >= abort_after_s:
            break
        time.sleep(frame_check.INBUF_POLL_S)
    steps["serial_polls"] = polls
    steps["abort_at_s"] = round(time.monotonic() - start, 3)
    transport.clear_device("H02: operator-approved abort during NPLC 100 block")
    transport.write("TARM HOLD")
    steps["tarm_after_abort"] = tarm = _line(transport.query_raw("TARM?")).strip()
    first = _integer(transport.query_raw("MCOUNT?"))
    time.sleep(ABORT_STABILITY_S)
    second = _integer(transport.query_raw("MCOUNT?"))
    steps.update(mcount_after_abort=first, mcount_after_stability=second,
                 stability_window_s=ABORT_STABILITY_S)
    steps["inbuf_after_sdc"] = transport.query_raw("INBUF?")
    transport.write("INBUF OFF")
    steps["errors"] = read_error_register(transport)
    stopped = tarm == TARM_HOLD and first == second and second < n
    steps["state"] = "ABORTED" if stopped else "FAULT"
    if 2 <= second <= 100:
        raw = transport.query_raw(f"RMEM 1,{second}")
        steps["partial_raw"] = raw
        try:
            steps["partial_valid_readings"] = len(parse_readings(raw, second, BOUNDS, profile))
        except DataValidationError as exc:
            steps["partial_error"] = str(exc)
    steps["result"] = "PASS" if stopped else "FAIL"


def run_acceptance(config: LabConfig, output: Path, dut_id: str, dut_nominal_ohm: float,
                   transport_factory: Callable[[VisaSettings], Any] = VisaTransport,
                   ready_deadline_s: float = 600.0, abort_after_s: float = 12.0,
                   abort_n: int = 30) -> tuple[str, Path]:
    if config.visa is None:
        raise ValueError("No [instrument].resource in the config; nothing is opened")
    if not 0 < dut_nominal_ohm <= 12:
        raise ValueError("H03 uses OHMF 10: DUT nominal must be within 0..12 ohm")
    profile = config.reading_profile
    session_id = str(uuid.uuid4())
    output.mkdir(parents=True, exist_ok=True)
    folder = output / f"acceptance-{session_id}"
    folder.mkdir(exist_ok=False)
    # 10 s: on NI GPIB-USB-HS + Keysight VISA a timeout lasts ~1.48x the NI-quantized
    # value (5 s -> 14.8 s, 60 s -> 147.6 s, H05). Long waits use INBUF + serial poll.
    transport = transport_factory(replace(config.visa, timeout_ms=10_000,
                                          read_termination="lf"),
                                  journal=folder / "journal.jsonl")
    record: dict[str, Any] = {"profile": profile.name}
    status, detail = "TRANSPORT_ERROR", None
    try:
        transport.open()
        record["preflight"] = preflight = preflight_identity(transport,
                                                             config.accepted_identities)
        if not profile.verified_for(preflight.revision):
            raise FrameCheckStop(f"Profile {profile.name} not verified for this REV")
        existing = _integer(transport.query_raw("MCOUNT?"))
        record["mcount_before"] = existing
        if existing:
            if not 2 <= existing <= 100:
                raise FrameCheckStop(f"{existing} readings in memory cannot be archived here")
            record["archived_memory_raw"] = transport.query_raw(f"RMEM 1,{existing}")
        for command in BASELINE:
            transport.write(command)
        stored = check_h03(transport, profile, dut_nominal_ohm, ready_deadline_s, record)
        check_h04(transport, profile, stored, record)
        check_h02_abort(transport, profile, abort_n, abort_after_s, record)
        record["errors_final"] = read_error_register(transport)
        results = [record[key]["result"] for key in ("h03", "h04", "h02_abort")]
        status = "PASS" if all(result == "PASS" for result in results) else "CHECK_RESULTS"
    except IdentityError as exc:
        status, detail = "IDENTITY_REJECTED", str(exc)
    except FrameCheckStop as exc:
        status, detail = "STOPPED", str(exc)
    except ResponseFormatError as exc:
        status, detail = "RESPONSE_INVALID", str(exc)
    except (OSError, RuntimeError) as exc:
        detail = f"{type(exc).__name__}: {exc}"

    def write_result(name: str) -> None:
        payload = {
            "schema_version": 1, "kind": "h02_h03_h04_acceptance",
            "session_uuid": session_id,
            "created_utc": datetime.now(timezone.utc).isoformat(), "simulation": False,
            "status": status, "detail": detail, "dut_id": dut_id,
            "dut_nominal_ohm_not_calibrated": dut_nominal_ohm, "config_source": config.source,
            "backend_info": getattr(transport, "backend_info", {}),
            "backend_poisoned": getattr(transport, "backend_poisoned", False),
            "software_version": __version__, "git_revision": _git_revision(),
            "software_environment": software_environment(), "record": record,
            "trace": list(getattr(transport, "trace", [])),
        }
        _write_new(folder / name, json.dumps(_jsonable(payload), ensure_ascii=False,
                                             allow_nan=False, indent=2))

    # Result first: a hanging native close must never cost the evidence.
    write_result("acceptance.json")
    try:
        transport.close()
    except OSError as exc:
        detail = f"{detail or ''} close: {exc}".strip()
    return status, folder
