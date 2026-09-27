"""Offline integration audit (WP-07): document <-> code <-> test, forbidden writes, data
preservation and operator gates, checked on command logs, journals and files. Pure checks
that return findings; `scripts/audit.py` runs them on a full simulated plan. A clean audit
is offline evidence only: it closes no HIL gate.
"""

import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
# Never part of the diagnosis (CLAUDE.md): calibration, memory poking, erase, security.
FORBIDDEN = ("CAL", "ACAL", "CALSTR", "POKE", "PEEK", "SCRATCH", "DEFEAT", "SECURE",
             "SSTATE", "RSTATE", "PURGE", "STORE", "TEST")
TRIGGERING = ("TARM SGL", "MEM FIFO", "NRDGS", "MFORMAT", "PRESET", "ACAL")
# WP-08: the only exception, and only after a journaled operator confirmation.
ACAL_ALLOWED = ("ACAL DCV", "ACAL OHMS")


def keyword(command: str) -> str:
    """'RMEM 1,100' -> 'RMEM'; 'MCOUNT?' -> 'MCOUNT'; 'TARM HOLD' -> 'TARM'."""
    return re.split(r"[ ?]", command.strip(), maxsplit=1)[0].upper()


def forbidden_commands(commands: Iterable[str], acal_confirmed: bool = False) -> list[str]:
    return [c for c in commands if keyword(c) in FORBIDDEN
            and not (acal_confirmed and c in ACAL_ALLOWED)]


def evidence_gaps(commands: Iterable[str], commands_md: str, evidence: list[dict]) -> list[str]:
    """Keywords used on the bus without a row in COMMANDS.md or command_evidence.json."""
    documented = set(re.findall(r"`([A-Z]+)[ ?`]", commands_md))
    in_json = {keyword(part) for e in evidence for part in re.split(r"[/,]", e["syntax"])}
    gaps = []
    for kw in sorted({keyword(c) for c in commands}):
        if kw not in documented:
            gaps.append(f"{kw}: nincs a COMMANDS.md táblájában")
        if kw not in in_json:
            gaps.append(f"{kw}: nincs a references/command_evidence.json-ban")
    return gaps


def sequence_violations(commands: list[str]) -> list[str]:
    """D21: RMEM only right after MCOUNT? (no trigger/erase in between). C05: MFORMAT and
    PRESET only before the first block (baseline), never while a block may own memory."""
    out, last_mcount, first_fifo = [], None, None
    for i, c in enumerate(commands):
        if c == "MCOUNT?":
            last_mcount = i
        elif c.startswith("RMEM"):
            between = commands[last_mcount + 1:i] if last_mcount is not None else None
            if between is None or any(b.startswith(TRIGGERING) for b in between):
                out.append(f"#{i} {c}: nem közvetlenül igazolt MCOUNT? után")
        elif c.startswith(TRIGGERING):
            last_mcount = None
        if c == "MEM FIFO" and first_fifo is None:
            first_fifo = i
        if c.startswith(("MFORMAT", "PRESET")) and first_fifo is not None:
            out.append(f"#{i} {c}: memóriatörlő/alaphelyzet parancs az első blokk után")
    return out


def journal_violations(events: list[dict], gated: bool,
                       expected_blocks: int | None = None) -> list[str]:
    """Every block_opened is preceded (since the previous block) by an operator gate
    (GUI: operator_wiring_confirmed or series_continuation); every opened block is saved
    before release and released before the next block opens."""
    out, gate_seen, open_block, saved, opened = [], False, None, set(), 0
    for i, e in enumerate(events):
        name = e.get("type")  # journal record kind (session_store); engine facts in data
        data = e.get("data") or {}
        sub = data.get("event") if name == "engine" else None
        if name in ("operator_wiring_confirmed", "series_continuation"):
            gate_seen = True
        elif name == "block_saved":
            saved.add(data.get("block_id"))
        elif sub == "block_opened":
            if open_block is not None:
                out.append(f"#{i}: új blokk nyílt, mielőtt {open_block} felszabadult")
            if gated and not gate_seen:
                out.append(f"#{i}: {data.get('test_id')} blokk kezelői kapu/sorozat nélkül")
            open_block, gate_seen, opened = data.get("block_id"), False, opened + 1
        elif sub == "acal_started" and open_block is not None:
            out.append(f"#{i}: ACAL indult, miközben {open_block} birtokolja a memóriát")
        elif sub == "released":
            block = data.get("block_id")
            if data.get("how") == "saved" and block not in saved:
                out.append(f"#{i}: {block} felszabadítva mentés előtt")
            if block == open_block:
                open_block = None
    if expected_blocks is not None and opened != expected_blocks:
        # guards the check itself: a wrong record key would otherwise pass silently
        out.append(f"{opened} block_opened esemény, {expected_blocks} várt")
    return out


def tree_digest(folder: Path, exclude: str = "exports") -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(Path(folder).rglob("*")) if p.is_file()
            and exclude not in p.relative_to(folder).parts}


def _touched_memory(block: dict) -> bool:
    """False for a block stopped before MEM FIFO (abort during settling): no read expected.
    TEMP? start is read right before MEM FIFO, so it marks every block that got that far."""
    outcome = block.get("outcome") or {}
    return bool(outcome.get("temp_start_raw") or outcome.get("mcount") is not None
                or outcome.get("result")
                or outcome.get("partial_result") or block.get("result"))


def _read_expected(block: dict) -> bool:
    """True when the block reached an RMEM (a result exists, or a readable count was
    stored); COUNT_MISMATCH/FAULT after MEM FIFO and aborts before arming have no read."""
    outcome = block.get("outcome") or {}
    if outcome.get("result") or outcome.get("partial_result") or block.get("result"):
        return True
    return (outcome.get("state") in ("ACQUIRED", "VALIDATED", "INVALID", "PARTIAL", "ABORTED")
            and (outcome.get("mcount") or 0) >= 2)


def raw_preservation_gaps(folder: Path) -> list[str]:
    """Every block keeps the raw bytes of every memory read and statistic read."""
    out, checked, touched = [], 0, 0
    for path in sorted((Path(folder) / "blocks").glob("*.json")):
        block = json.loads(path.read_text(encoding="utf-8"))
        touched += _read_expected(block)
        outcome = block.get("outcome") or {}
        result = (outcome.get("result") or outcome.get("partial_result") or
                  block.get("result") or {})
        reads = [result.get("memory_reads") or {}] + list(
            (result.get("statistic_reads") or {}).values())
        for read in reads:
            for attempt in read.get("attempts") or ():
                checked += 1
                if not attempt.get("raw_base64") and not attempt.get("error"):
                    out.append(f"{path.name}: olvasás nyers bájt nélkül")
    if not checked and touched:
        out.append("egyetlen olvasási kísérlet sem található: az ellenőrzés nem futott")
    return out


def traceability_gaps(root: Path = ROOT) -> list[str]:
    requirements = set(re.findall(r"^\| (R\d\d) \|", (root / "docs/REQUIREMENTS.md")
                                  .read_text(encoding="utf-8"), re.M))
    with (root / "docs/TRACEABILITY.csv").open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    rows = rows[1:]  # header row
    traced = {r[0] for r in rows}
    out = [f"{r}: nincs a TRACEABILITY.csv-ben" for r in sorted(requirements - traced)]
    for row in rows:
        for ref in re.split(r"[;\s]+", row[3]):
            if ref.startswith(("tests/", "docs/")) and not (root / ref.rstrip(",")).exists():
                out.append(f"{row[0]}: hivatkozott bizonyíték nem létezik: {ref}")
    return out


def bus_commands(folder: Path) -> list[str]:
    """Commands actually written/queried, from the raw bus journal of a live session."""
    out = []
    for line in (Path(folder) / "bus.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:  # a partial line kept after a crash: not a sent command
            continue
        # device_clear logs its reason in "command"; only writes and queries are commands
        if entry.get("operation") in ("write", "query") and entry.get("command"):
            out.append(entry["command"])
    return out


def audit_folder(folder: Path, commands: list[str], root: Path = ROOT) -> dict[str, list[str]]:
    """All file-level checks of one session folder (simulated or live). GUI sessions must
    show a gate or series continuation before every block; a CLI live session is one wiring
    series under one confirmation (checked from its plan); the export must not modify it."""
    from .domain import canonical_test_id, default_plan, series_end
    from .export import export_session
    from .session_store import scan_journal, verify_session
    folder = Path(folder)
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    events = scan_journal(folder / "events.jsonl").events  # skips kept partial lines
    evidence = json.loads((root / "references/command_evidence.json").read_text(
        encoding="utf-8"))
    blocks = sorted((folder / "blocks").glob("*.json"))
    opened = sum(1 for e in events if e.get("type") == "engine" and
                 (e.get("data") or {}).get("event") == "block_opened")
    acal_confirmed = sum(1 for e in events if e.get("type") == "engine" and
                         (e.get("data") or {}).get("event") == "acal_confirmed")
    acal_sent = sum(1 for c in commands if keyword(c) == "ACAL")
    findings = {
        "Tiltott parancs": forbidden_commands(commands, acal_confirmed=acal_confirmed > 0),
        "Parancsbizonyíték": evidence_gaps(
            commands, (root / "docs/COMMANDS.md").read_text(encoding="utf-8"), evidence),
        "Sorrend (D21, C05)": sequence_violations(commands),
        "Napló (kapu, mentés→felszabadítás)": journal_violations(
            events, gated=meta.get("kind") == "gui_session", expected_blocks=opened),
        "Nyers bájtok": raw_preservation_gaps(folder),
    }
    if not commands:
        findings["Tiltott parancs"].append("nincs parancsnapló: az ellenőrzés nem futott")
    if acal_sent and not acal_confirmed:
        findings["Tiltott parancs"].append("ACAL kezelői megerősítés (acal_confirmed) nélkül")
    if acal_sent > 2 * acal_confirmed:
        findings["Tiltott parancs"].append("több ACAL, mint amennyit a kezelő megerősített")
    if meta.get("kind") == "live_session":
        plan = {p.test_id: p for p in default_plan(True)}
        ids = [canonical_test_id(t) for t in meta.get("test_ids") or ()]
        points = [plan[t] for t in ids if t in plan]
        findings["CLI: egy megerősítés = egy sorozat"] = (
            [] if points and series_end(points, 0) == len(points)
            else ["a CLI-munkamenet több bekötési sorozatot fed le egy megerősítéssel"])
        if len(points) != len(ids):
            findings["CLI: egy megerősítés = egy sorozat"].append(
                "ismeretlen pontazonosító a munkamenetben")
    verification = verify_session(folder)
    findings["Hash-lánc"] = [] if verification.ok else [str(verification)]
    before = tree_digest(folder)
    with __import__("tempfile").TemporaryDirectory() as tmp:
        export = export_session(folder, Path(tmp))
        findings["Export"] = ([] if (export / "export_complete.json").exists() else
                              ["nincs export_complete.json"])
    if tree_digest(folder) != before:
        findings["Export"].append("a session-mappa megváltozott")
    findings["Blokkfájlok"] = ([] if len(blocks) >= opened else
                               [f"{opened} megnyitott blokk, {len(blocks)} blokkfájl"])
    if not opened and any(_touched_memory(json.loads(b.read_text(encoding="utf-8")))
                          for b in blocks):  # guards against a wrong record key
        findings["Blokkfájlok"].append("blokkfájl van, de egy block_opened esemény sincs")
    return findings

