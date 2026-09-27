"""Unique-session export for the offline foundation.

WP-04 adds an acquisition-time durable journal and crash recovery. The current
export occurs after memory validation and must not be advertised as crash-safe I/O.
"""

import csv
import json
import os
import subprocess
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from . import __version__
from .domain import TestPoint
from .memory_reader import BlockResult


def json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def _write_new(path: Path, content: str) -> None:
    if path.exists():
        raise FileExistsError(path)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def _git_revision() -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, timeout=3, cwd=Path(__file__).resolve().parents[2])
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def save_simulation(root: Path, point: TestPoint, result: BlockResult,
                    command_log: list[str], scenario: str) -> Path:
    session_id = str(uuid.uuid4())
    created = datetime.now(timezone.utc).isoformat()
    root.mkdir(parents=True, exist_ok=True)
    folder = root / session_id
    folder.mkdir(exist_ok=False)
    payload = {
        "schema_version": 1, "session_uuid": session_id, "created_utc": created,
        "simulation": True, "instrument_id": "SIMULATED HP 3458A",
        "visa_resource": None, "software_version": __version__,
        "git_revision": _git_revision(), "scenario": scenario,
        "hardware_validation": "NOT_PERFORMED", "warm_up_confirmed": None,
        "last_acal": None, "temp_start_c": None, "temp_end_c": None,
        "time_basis": "sample_index_only", "test_point": point,
        "block_result": result, "command_log": command_log,
    }
    _write_new(folder / "session.json", json.dumps(payload, default=json_default,
                                                  ensure_ascii=False, allow_nan=False, indent=2))
    with (folder / "raw_readings.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["test_id", "chronological_index", "rmem_index", "value", "unit",
                         "block_status", "simulation"])
        for i, value in enumerate(result.chronological_values, 1):
            writer.writerow([point.test_id, i, point.n-i+1, str(value), point.unit,
                             result.status, True])
    with (folder / "summary.csv").open("x", encoding="utf-8", newline="") as stream:
        fields = ["test_id", "status", "unit", "n", "mean", "sdev", "minimum", "maximum",
                  "peak_to_peak", "sem_iid", "relative_sdev_ppm", "mean_deviation_ppm"]
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"test_id": point.test_id, "status": result.status, "unit": point.unit,
                         **(asdict(result.pc_statistics) if result.pc_statistics else {})})
    stat_text = json.dumps(asdict(result.pc_statistics), indent=2) if result.pc_statistics else "N/A"
    agreement = "YES" if result.status == "VALIDATED" else "NO / NOT ESTABLISHED"
    report = f"""# HP3458A DIAGNOSTIC SUMMARY

**SIMULATION ONLY — nem műszermérés.**

Instrument ID: SIMULATED HP 3458A
Date UTC: {created}
Session: {session_id}
Test: {point.test_id} / {point.dut_id}
Mode: {point.mode}; range: {point.range_value:g} {point.unit}; NPLC: {point.nplc}
AZERO: ON; OCOMP: {point.ocomp}; DELAY: {point.delay_s:g} s; requested N: {point.n}
Warm-up / Last ACAL / TEMP start / TEMP end: N/A (simulation)

## TEST RESULTS

```json
{stat_text}
```

## VALIDATION

Block status: {result.status}
Memory read consensus: {result.memory_reads.status}
RMEM attempts: {len(result.memory_reads.attempts)}
All initial memory rereads identical: {"YES" if result.memory_reads.status == "EXACT" else "NO"}
PC vs simulated DMM STAT agreement: {agreement}
Cross-check absolute tolerance: {result.absolute_tolerance:g} {point.unit}
Cross-check relative tolerance: {result.relative_tolerance:g}
Errors: {', '.join(result.errors) or 'none'}

## OBSERVATIONS

A minták időrendbe fordítva szerepelnek a CSV-ben; az RMEM-válaszok eredeti
bájtsorai base64 formában a session.json fájlban maradnak, a hibás olvasásokkal együtt.
A sem_iid csak független, stacionárius minták mellett értelmezhető standard hiba.
A névleges értéktől való eltérés nem kalibrációs hibaigazolás.
Az adatkonzisztencia nem bizonyítja a műszer hibátlanságát.
A szimulátor nem igazolja a valódi firmware statisztikai pontosságát vagy időzítését.
"""
    _write_new(folder / "report.md", report)
    return folder
