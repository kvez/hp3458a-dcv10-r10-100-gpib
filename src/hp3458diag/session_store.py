"""Durable session store (WP-04): schema-versioned, append-only, never overwriting.

Layout of one session folder (`session-<uuid>/`):
  session.json     metadata; the only file ever replaced (atomically), lifecycle OPEN/CLOSED
  events.jsonl     append-only journal, one JSON line per event, fsynced, hash-chained
  bus.jsonl        raw bus bytes written by VisaTransport(journal=...) right after each I/O
  blocks/          one file per block outcome, created once (never overwritten)
  checksums.sha256 written at close for every data file
Unknown/partial state is preserved as evidence: a truncated journal tail is kept, a
recovery appends after it. Storage failures raise StorageError; callers must stop.
"""

import hashlib
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from . import __version__
from .identify import _jsonable

SCHEMA_VERSION = 2


class StorageError(OSError):
    """Evidence could not be made durable (disk full, permission, I/O error)."""


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fsync_dir(path: Path) -> None:
    try:  # POSIX durability of the directory entry; not supported on Windows
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _publish_no_overwrite(tmp: Path, path: Path) -> None:
    """Make the complete temp file visible under its final name, never replacing a file.

    os.link fails with FileExistsError on an existing target. FAT volumes have no hard
    links (WinError 1, seen on a FAT test disk in H07); on Windows os.rename gives the
    same guarantee there (FileExistsError, WinError 183). POSIX rename would overwrite,
    so there the link error is raised unchanged.
    """
    try:
        os.link(tmp, path)
    except FileExistsError:
        raise
    except OSError:
        if os.name != "nt":
            raise
        os.rename(tmp, path)


def atomic_create(path: Path, data: bytes) -> str:
    """Write a new file completely or not at all; an existing file is never replaced.
    Returns the SHA-256 of the content."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        _publish_no_overwrite(tmp, path)
    except FileExistsError:
        raise
    except OSError as exc:
        raise StorageError(f"Cannot write {path}: {exc}") from exc
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    _fsync_dir(path.parent)
    return sha256_bytes(data)


def atomic_replace(path: Path, data: bytes) -> None:
    """Metadata only (session.json): complete old or complete new content, never partial."""
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise StorageError(f"Cannot update {path}: {exc}") from exc
    _fsync_dir(path.parent)


def _dumps(value: Any) -> bytes:
    return json.dumps(_jsonable(value), ensure_ascii=False, allow_nan=False,
                      indent=2).encode("utf-8")


@dataclass
class JournalScan:
    events: list[dict[str, Any]]
    chain_ok: bool
    truncated_tail: bytes | None
    first_bad_line: int | None
    last_hash: str | None
    partial_lines: list[int] = field(default_factory=list)  # kept earlier truncated tails


def scan_journal(path: Path) -> JournalScan:
    data = path.read_bytes() if path.exists() else b""
    lines = data.split(b"\n")
    tail = lines.pop()  # b"" when the file ends with a newline
    events, prev, bad, partial = [], None, None, []
    for number, line in enumerate(lines, 1):
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:  # JSONDecodeError, or UnicodeDecodeError when cut in a character
            # An earlier crash left a partial line; a reopen started a new line after it.
            # It stays as evidence and the chain continues from the last complete line.
            partial.append(number)
            continue
        if event.get("prev") != prev and bad is None:
            bad = number
        prev = sha256_bytes(line)
        events.append(event)
    return JournalScan(events, bad is None, tail or None, bad, prev, partial)


class Journal:
    """Append-only, fsynced, hash-chained event log (`prev` = SHA-256 of the prior line)."""

    def __init__(self, path: Path, monotonic: Callable[[], float]) -> None:
        self.path, self.monotonic = path, monotonic
        scan = scan_journal(path)
        self.seq = max((e.get("seq", 0) for e in scan.events), default=0)
        self.last_hash = scan.last_hash
        self.truncated_tail = scan.truncated_tail

    def _open(self):
        return open(self.path, "ab")

    def append(self, kind: str, **data: Any) -> dict[str, Any]:
        self.seq += 1
        event = {"seq": self.seq, "utc": _utc(), "monotonic_s": self.monotonic(),
                 "type": kind, "data": _jsonable(data), "prev": self.last_hash}
        line = json.dumps(event, ensure_ascii=False, allow_nan=False,
                          sort_keys=True).encode("utf-8")
        try:
            with self._open() as stream:
                if self.truncated_tail is not None:
                    stream.write(b"\n")  # keep the partial line as evidence, start fresh
                stream.write(line + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            # Bytes may have reached the file (partial write, fsync error after the write):
            # continue the chain from what the file really holds.
            try:
                scan = scan_journal(self.path)
                self.seq = max((e.get("seq", 0) for e in scan.events), default=0)
                self.last_hash, self.truncated_tail = scan.last_hash, scan.truncated_tail
            except OSError:
                self.seq -= 1
            raise StorageError(f"Journal write failed ({kind}): {exc}") from exc
        self.truncated_tail = None
        self.last_hash = sha256_bytes(line)
        return event


class SessionStore:
    def __init__(self, folder: Path, metadata: dict[str, Any],
                 monotonic: Callable[[], float]) -> None:
        self.folder, self.metadata = folder, metadata
        self.session_id = metadata["session_uuid"]
        self.journal = Journal(folder / "events.jsonl", monotonic)
        self.block_seq = len(list((folder / "blocks").glob("*.json")))

    @property
    def bus_journal_path(self) -> Path:
        return self.folder / "bus.jsonl"

    @classmethod
    def create(cls, root: Path, metadata: dict[str, Any],
               uuid_factory: Callable[[], uuid.UUID] = uuid.uuid4,
               monotonic: Callable[[], float] | None = None) -> "SessionStore":
        import time
        session_id = str(uuid_factory())
        folder = Path(root) / f"session-{session_id}"
        try:
            Path(root).mkdir(parents=True, exist_ok=True)
            folder.mkdir(exist_ok=False)  # a UUID collision never touches existing data
            (folder / "blocks").mkdir()
        except FileExistsError:
            raise
        except OSError as exc:
            raise StorageError(f"Cannot create session folder: {exc}") from exc
        meta = {"schema_version": SCHEMA_VERSION, "session_uuid": session_id,
                "lifecycle": "OPEN", "status": "RUNNING", "created_utc": _utc(),
                "software_version": __version__, **metadata}
        atomic_create(folder / "session.json", _dumps(meta))
        store = cls(folder, meta, monotonic or time.monotonic)
        store.journal.append("session_opened", schema_version=SCHEMA_VERSION)
        return store

    @classmethod
    def reopen(cls, folder: Path, monotonic: Callable[[], float] | None = None
               ) -> "SessionStore":
        import time
        meta = json.loads((Path(folder) / "session.json").read_text(encoding="utf-8"))
        if meta.get("schema_version") != SCHEMA_VERSION:
            raise StorageError(f"Session schema {meta.get('schema_version')} cannot be "
                               "reopened for writing (read it with load_session)")
        store = cls(Path(folder), meta, monotonic or time.monotonic)
        store.journal.append("session_reopened",
                             truncated_tail_sha256=(sha256_bytes(store.journal.truncated_tail)
                                                    if store.journal.truncated_tail else None))
        return store

    def event_sink(self, event: dict[str, Any]) -> None:
        self.journal.append("engine", **event)

    def save_block(self, payload: dict[str, Any], test_id: str, block_id: str) -> Path:
        """One new file per block; the journal records its SHA-256 (checksum)."""
        self.block_seq += 1
        path = self.folder / "blocks" / f"{self.block_seq:03d}-{test_id}-{block_id}.json"
        data = _dumps({"schema_version": SCHEMA_VERSION, "session_uuid": self.session_id,
                       "block_uuid": block_id, **payload})
        digest = atomic_create(path, data)
        self.journal.append("block_saved", block_id=block_id, test_id=test_id,
                            file=path.name, sha256=digest, bytes=len(data))
        return path

    def save_record(self, name: str, payload: dict[str, Any]) -> Path:
        path = self.folder / name
        digest = atomic_create(path, _dumps(payload))
        self.journal.append("record_saved", file=name, sha256=digest)
        return path

    def close(self, status: str) -> None:
        self.journal.append("session_closed", status=status)
        lines = []
        for path in sorted(self.folder.rglob("*")):
            if (path.is_file() and path.name != "session.json"
                    and not path.name.startswith("checksums")
                    and not path.name.endswith(".tmp")
                    and "exports" not in path.relative_to(self.folder).parts):
                data = path.read_bytes()
                # "sha256  size  name": journals may only grow later, so the first
                # `size` bytes must still hash the same (append-only proof).
                lines.append(f"{sha256_bytes(data)}  {len(data)}  "
                             f"{path.relative_to(self.folder).as_posix()}")
        # Each close (first run, later recoveries) gets its own checksum file; none replaced.
        name = ("checksums.sha256" if not (self.folder / "checksums.sha256").exists()
                else f"checksums-{self.journal.seq:06d}.sha256")
        atomic_create(self.folder / name, ("\n".join(lines) + "\n").encode())
        self.metadata.update(lifecycle="CLOSED", status=status, closed_utc=_utc())
        atomic_replace(self.folder / "session.json", _dumps(self.metadata))


@dataclass
class Interrupted:
    folder: Path
    session_uuid: str
    last_event: str | None
    pending_block: dict[str, Any] | None  # engine "armed" data of an unreleased block
    pending_saved: dict[str, Any] | None  # its block_saved record, if the save happened
    truncated_tail: bool
    chain_ok: bool


def pending_from_events(events: list[dict[str, Any]]) -> tuple[dict | None, dict | None]:
    armed: dict[str, dict] = {}
    saved: dict[str, dict] = {}
    released: set[str] = set()
    for event in events:
        data = event.get("data", {})
        if event.get("type") == "engine" and data.get("event") in ("block_opened", "armed"):
            armed.setdefault(data["block_id"], {}).update(data)  # owned from MEM FIFO on
        elif event.get("type") == "engine" and data.get("event") == "released":
            released.add(data["block_id"])
        elif event.get("type") == "block_saved":
            saved[data["block_id"]] = data
    open_blocks = [b for b in armed if b not in released]
    if not open_blocks:
        return None, None
    last = open_blocks[-1]
    return armed[last], saved.get(last)


def find_interrupted(root: Path) -> list[Interrupted]:
    found = []
    for meta_path in sorted(Path(root).glob("session-*/session.json")):
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if meta.get("schema_version") != SCHEMA_VERSION or meta.get("lifecycle") != "OPEN":
            continue
        scan = scan_journal(meta_path.parent / "events.jsonl")
        pending, saved = pending_from_events(scan.events)
        found.append(Interrupted(meta_path.parent, meta["session_uuid"],
                                 scan.events[-1]["type"] if scan.events else None,
                                 pending, saved, scan.truncated_tail is not None,
                                 scan.chain_ok))
    return found


@dataclass
class Verification:
    chain_ok: bool
    truncated_tail: bool
    block_mismatches: list[str] = field(default_factory=list)
    checksum_mismatches: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.chain_ok and not self.block_mismatches and not self.checksum_mismatches


def verify_session(folder: Path) -> Verification:
    folder = Path(folder)
    scan = scan_journal(folder / "events.jsonl")
    result = Verification(scan.chain_ok, scan.truncated_tail is not None)
    for event in scan.events:
        if event.get("type") in ("block_saved", "record_saved"):
            data = event["data"]
            sub = "blocks" if event["type"] == "block_saved" else ""
            path = folder / sub / data["file"]
            if not path.exists() or sha256_bytes(path.read_bytes()) != data["sha256"]:
                result.block_mismatches.append(data["file"])
    for checksums in sorted(folder.glob("checksums*.sha256")):
        for line in checksums.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            digest, size, name = line.split("  ", 2)
            path = folder / name
            data = path.read_bytes() if path.exists() else b""
            appendable = name.endswith(".jsonl")
            prefix = data[:int(size)] if appendable else data
            if not path.exists() or sha256_bytes(prefix) != digest:
                result.checksum_mismatches.append(f"{checksums.name}: {name}")
    return result


@dataclass
class SessionView:
    folder: Path
    schema_version: int
    migrated_from: int | None
    metadata: dict[str, Any]
    blocks: list[dict[str, Any]]


def load_session(folder: Path) -> SessionView:
    """Read any known schema without rewriting it. v1 = single-block simulation export
    (persistence.save_simulation, 0.1.x); it is mapped in memory, never migrated on disk."""
    folder = Path(folder)
    meta = json.loads((folder / "session.json").read_text(encoding="utf-8"))
    version = meta.get("schema_version")
    if version == 1:
        block = {"test_point": meta.get("test_point"), "block_result": meta.get("block_result"),
                 "simulation": meta.get("simulation"), "legacy": True}
        view_meta = {k: v for k, v in meta.items() if k not in ("test_point", "block_result")}
        view_meta.update(lifecycle="LEGACY_EXPORT", status=(meta.get("block_result") or {})
                         .get("status"))
        return SessionView(folder, SCHEMA_VERSION, 1, view_meta, [block])
    if version == SCHEMA_VERSION:
        blocks = [json.loads(p.read_text(encoding="utf-8"))
                  for p in sorted((folder / "blocks").glob("*.json"))]
        return SessionView(folder, SCHEMA_VERSION, None, meta, blocks)
    raise StorageError(f"Unknown session schema_version {version!r}")
