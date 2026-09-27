"""Export a stored session to CSV/JSON/Markdown/SVG (WP-04, WP-06). Files only: never touches
the instrument. `raw_readings.csv` keeps every reading as the exact string read from the
instrument, in chronological order; `report.md` is the self-contained diagnostic report
(report.py) and `plots/` holds its SVG plots. Every file is complete or absent (atomic_create); `export_complete.json` is
written last, so a folder without it is an incomplete export. Old files are never
overwritten: each export gets a new folder.
"""

import csv
import io
import json
import uuid
from pathlib import Path
from typing import Any
from .identify import _jsonable
from .plots import histogram_plot, index_plot
from .report import block_view, build_report
from .session_store import atomic_create, load_session

FIELDS = ("block_file", "test_id", "state", "validation", "n", "unit", "mean", "sdev",
          "minimum", "maximum", "recovered", "legacy", "simulation")


def _row(name: str, block: dict[str, Any]) -> dict[str, Any]:
    point = block.get("test_point") or {}
    outcome = block.get("outcome") or {}
    result = block.get("result") or outcome.get("result") or block.get("block_result") or {}
    stats = result.get("pc_statistics") or {}
    unit = "V" if point.get("mode") == "DCV" else "ohm" if point.get("mode") else None
    return {"block_file": name, "test_id": point.get("test_id"),
            "state": outcome.get("state"), "validation": result.get("status"),
            "n": point.get("n"), "unit": unit, "mean": stats.get("mean"),
            "sdev": stats.get("sdev"), "minimum": stats.get("minimum"),
            "maximum": stats.get("maximum"), "recovered": bool(block.get("recovered")),
            "legacy": bool(block.get("legacy")), "simulation": block.get("simulation")}


def export_session(folder: Path, output: Path | None = None) -> Path:
    view = load_session(folder)
    target = (output or Path(folder) / "exports") / f"export-{uuid.uuid4()}"
    target.mkdir(parents=True, exist_ok=False)
    names = [p.name for p in sorted((Path(folder) / "blocks").glob("*.json"))] \
        if view.migrated_from is None else ["session.json"]
    rows = [_row(name, block) for name, block in zip(names, view.blocks)]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=FIELDS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    digests = {"summary.csv": atomic_create(target / "summary.csv", buffer.getvalue().encode())}
    digests["blocks.json"] = atomic_create(
        target / "blocks.json",
        json.dumps(_jsonable({"session": view.metadata, "blocks": view.blocks}),
                   ensure_ascii=False, allow_nan=False, indent=2).encode("utf-8"))
    views = [block_view(name, block) for name, block in zip(names, view.blocks)]
    simulated = any(b.simulation for b in views) or bool(view.metadata.get("simulation"))
    source = "SIMULATION — nem műszermérés" if simulated else "INSTRUMENT — valódi műszeradat"
    raw = io.StringIO()
    raw_writer = csv.writer(raw, lineterminator="\n")
    raw_writer.writerow(("block_file", "test_id", "block_id", "sample_index", "value_raw",
                         "validation", "simulation"))
    for b in views:
        for index, text in enumerate(b.raw_values):
            raw_writer.writerow((b.file, b.test_id, b.block_id, index, text, b.validation,
                                 b.simulation))
    digests["raw_readings.csv"] = atomic_create(target / "raw_readings.csv",
                                                raw.getvalue().encode())
    plot_names: dict[str, str] = {}
    (target / "plots").mkdir()
    for number, b in enumerate(views, 1):
        if len(b.values) < 2:
            continue
        prefix = f"plots/{number:03d}-{b.test_id}"
        title = f"{b.test_id} — {b.validation or b.state} — N={len(b.values)} ({b.file})"
        for suffix, render in (("index", index_plot), ("hist", histogram_plot)):
            name = f"{prefix}-{suffix}.svg"
            digests[name] = atomic_create(target / name, render(
                b.values, title, source, b.unit or "").encode("utf-8"))
        plot_names[b.file] = prefix
    metadata = dict(view.metadata)
    preflight = Path(folder) / "preflight.json"
    if preflight.exists() and not metadata.get("instrument_id"):
        pre = json.loads(preflight.read_text(encoding="utf-8")).get("preflight") or {}
        metadata["instrument_id"] = " ".join(
            [str(pre.get("identity") or "")] + [f"REV {','.join(pre['revision'])}"]
            if pre.get("revision") else [str(pre.get("identity") or "")]).strip()
    report = build_report(metadata, views, plot_names)
    if view.migrated_from:
        report += (f"\nSéma: {view.schema_version} (migrated from {view.migrated_from}, "
                   "read-only; a lemezen nem módosítva).\n")
    digests["report.md"] = atomic_create(target / "report.md", report.encode("utf-8"))
    atomic_create(target / "export_complete.json",
                  json.dumps({"files": digests, "source": str(folder)}, indent=2).encode())
    return target
