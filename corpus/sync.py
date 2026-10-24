"""Private, explicit sync snapshots for the Murakami corpus.

Use: python -m corpus.sync export /private/location/library.zip
     python -m corpus.sync import /private/location/library.zip [--force]

Never put these archives on a public GitHub repository. Stop all web/desktop
instances before importing. Sync does not merge concurrent changes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import stat
import sys
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from corpus import db

MANIFEST = "manifest.json"
STATE = ".sync-state.json"
FORMAT = 1
MAX_UNPACKED = 10 * 1024**3
BACKUP_FOLDER = ".murakami-sync-backups"


class SyncError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _validate_regular(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise SyncError(f"Expected a regular file, not a symlink: {path}")


def _files(root: Path) -> list[tuple[str, Path]]:
    originals = root / "files"
    if originals.is_symlink():
        raise SyncError("data/files may not be a symlink")
    if not originals.exists():
        return []
    if not originals.is_dir():
        raise SyncError("data/files must be a directory")
    items: list[tuple[str, Path]] = []
    for path in originals.rglob("*"):
        if path.is_symlink():
            raise SyncError(f"Symlink in originals is not supported: {path}")
        if path.is_dir():
            continue
        _validate_regular(path)
        items.append((path.relative_to(root).as_posix(), path))
    return sorted(items)


def _snapshot_db(source: Path, destination: Path) -> None:
    """SQLite backup API includes uncheckpointed WAL data."""
    _validate_regular(source)
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _descriptors(root: Path) -> list[dict]:
    """Fingerprint an ONLINE SQLite snapshot, never raw .db without its WAL."""
    with tempfile.TemporaryDirectory(prefix="murakami-hash-") as directory:
        snapshot = Path(directory) / "snapshot.db"
        _snapshot_db(root / "corpus.db", snapshot)
        names = [("corpus.db", snapshot)] + _files(root)
        return [{"path": name, "size": path.stat().st_size,
                 "sha256": _sha256(path)} for name, path in names]


def _digest_piece(digest, value) -> None:
    """Length-prefix each encoded value to avoid ambiguous hash boundaries."""
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(",", ":")).encode("utf-8")
    digest.update(len(raw).to_bytes(8, "big"))
    digest.update(raw)


def _typed_value(value):
    """Portable, unambiguous representation of every SQLite scalar type."""
    if value is None:
        return ["null"]
    if isinstance(value, int):
        return ["integer", str(value)]
    if isinstance(value, float):
        return ["real", value.hex()]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    raise SyncError(f"Unexpected SQLite value type: {type(value).__name__}")


def _logical_db_digest(database: Path) -> str:
    """Hash SQL schema and logical rows, not SQLite page placement.

    FTS5 shadow tables encode internal index layout. Read the public virtual
    table instead; its searchable rows are the reproducible logical contents.
    """
    _validate_regular(database)
    con = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    digest = hashlib.sha256()
    try:
        objects = list(con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
        ))
        virtuals = [
            name for kind, name, _table, sql in objects
            if kind == "table" and sql and
            sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE")
        ]
        shadow_prefixes = tuple(v + "_" for v in virtuals)
        visible = [
            obj for obj in objects
            if not any(obj[1].startswith(prefix) for prefix in shadow_prefixes)
        ]
        _digest_piece(digest, ["sqlite-user-version",
                               con.execute("PRAGMA user_version").fetchone()[0]])
        _digest_piece(digest, ["sqlite-application-id",
                               con.execute("PRAGMA application_id").fetchone()[0]])
        _digest_piece(digest, visible)
        for kind, name, _table, sql in visible:
            if kind != "table":
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            fields = list(con.execute(f"PRAGMA table_info({quoted})"))
            keys = sorted(
                ((r[5], r[1]) for r in fields if r[5]), key=lambda x: x[0]
            )
            if sql and sql.lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                order = "rowid"
            elif keys:
                order = ", ".join('"' + k.replace('"', '""') + '"' for _, k in keys)
            else:
                # Tables without explicit PK have no stable physical rowid
                # guarantee after VACUUM. Sort by all logical columns.
                order = ", ".join(str(i) for i in range(1, len(fields) + 1))
                if not order:
                    raise SyncError(f"Cannot fingerprint table without columns: {name}")
            _digest_piece(digest, ["table", name])
            query = f"SELECT * FROM {quoted} ORDER BY {order}"
            for row in con.execute(query):
                _digest_piece(digest, [_typed_value(v) for v in row])
            _digest_piece(digest, ["end-table", name])
        return digest.hexdigest()
    finally:
        con.close()


def _logical_fingerprint_snapshot(snapshot: Path, descriptors: list[dict]) -> str:
    originals = sorted(
        (dict(path=d["path"], size=d["size"], sha256=d["sha256"])
         for d in descriptors if d["path"] != "corpus.db"),
        key=lambda d: d["path"]
    )
    payload = {"format": 1, "database_logical_sha256": _logical_db_digest(snapshot),
               "original_files": originals}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _logical_fingerprint(root: Path) -> str:
    """Consistent database backup plus direct hashes of original files."""
    with tempfile.TemporaryDirectory(prefix="murakami-logical-") as tmp:
        snapshot = Path(tmp) / "corpus.db"
        _snapshot_db(root / "corpus.db", snapshot)
        originals = [
            {"path": name, "size": path.stat().st_size, "sha256": _sha256(path)}
            for name, path in _files(root)
        ]
        return _logical_fingerprint_snapshot(snapshot, originals)


def _fingerprint(files: list[dict]) -> str:
    payload = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_state(root: Path) -> dict | None:
    path = root / STATE
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if (state.get("format") != FORMAT or
                not isinstance(state.get("snapshot_id"), str) or
                not isinstance(state.get("fingerprint"), str)):
            raise ValueError("invalid state")
        logical = state.get("logical_fingerprint")
        if logical is not None and (
            not isinstance(logical, str) or len(logical) != 64
        ):
            raise ValueError("invalid logical_fingerprint")
        return state
    except (ValueError, OSError) as exc:
        raise SyncError(f"Cannot read sync state {path}: {exc}") from exc


def _write_state(root: Path, snapshot_id: str, fingerprint: str,
                 logical_fingerprint: str | None = None) -> None:
    state = {"format": FORMAT, "snapshot_id": snapshot_id,
             "fingerprint": fingerprint, "recorded_at": _utc_now()}
    if logical_fingerprint is not None:
        state["logical_fingerprint"] = logical_fingerprint
    temp = root / (STATE + ".tmp")
    temp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, root / STATE)


def _sqlite_ok(path: Path) -> None:
    _validate_regular(path)
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        result = con.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise SyncError(f"SQLite integrity check failed: {result}")
    finally:
        con.close()


def _app_likely_running() -> bool:
    # Desktop mode uses a random port, so close it manually as well.
    try:
        with socket.create_connection(("127.0.0.1", 5731), timeout=0.2):
            return True
    except OSError:
        return False


def export_snapshot(output: Path, root: Path | None = None) -> str:
    root = Path(root or db.DATA_DIR).expanduser().resolve()
    output = Path(output).expanduser().resolve()
    if _app_likely_running():
        raise SyncError("Local app on port 5731 is running. Stop it before exporting.")
    if output.suffix.lower() != ".zip":
        raise SyncError("Export filename must end in .zip")
    if not (root / "corpus.db").exists():
        raise SyncError(f"No library database at {root / 'corpus.db'}")
    if output.is_relative_to(root):
        raise SyncError("Export archive must be outside the data directory")
    output.parent.mkdir(parents=True, exist_ok=True)
    previous = _read_state(root)
    if previous and "logical_fingerprint" not in previous:
        raise SyncError("Legacy sync state needs adopt-baseline with the "
                        "matching historical ZIP before the next export.")
    snapshot_id = uuid.uuid4().hex
    tmp_archive: Path | None = None
    with tempfile.TemporaryDirectory(prefix="murakami-export-") as tmp:
        consistent = Path(tmp) / "corpus.db"
        _snapshot_db(root / "corpus.db", consistent)
        _sqlite_ok(consistent)
        archive_items = [("corpus.db", consistent)] + _files(root)
        fd, tmp_name = tempfile.mkstemp(prefix=".murakami-sync-", suffix=".zip",
                                        dir=output.parent)
        os.close(fd)
        tmp_archive = Path(tmp_name)
        try:
            descriptors = []
            with zipfile.ZipFile(tmp_archive, "w", compression=zipfile.ZIP_DEFLATED,
                                 compresslevel=6, allowZip64=True) as archive:
                for name, path in archive_items:
                    digest = hashlib.sha256()
                    byte_count = 0
                    with path.open("rb") as inp, archive.open(name, "w") as out:
                        for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                            digest.update(chunk)
                            byte_count += len(chunk)
                            out.write(chunk)
                    descriptors.append({"path": name, "size": byte_count,
                                        "sha256": digest.hexdigest()})
                manifest = {"format": FORMAT, "snapshot_id": snapshot_id,
                            "base_id": previous["snapshot_id"] if previous else None,
                            "exported_at": _utc_now(), "files": descriptors}
                manifest["fingerprint"] = _fingerprint(descriptors)
                manifest["logical_fingerprint"] = _logical_fingerprint_snapshot(
                    consistent, descriptors
                )
                archive.writestr(MANIFEST,
                                 json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            # Prevent marking a snapshot as current if the library changed while copying.
            if _logical_fingerprint(root) != manifest["logical_fingerprint"]:
                raise SyncError("Library changed during export; close the app and retry")
            if output.exists():
                raise SyncError(f"Archive already exists; choose a new name: {output}")
            os.replace(tmp_archive, output)
            tmp_archive = None
            _write_state(root, snapshot_id, manifest["fingerprint"],
                         manifest["logical_fingerprint"])
        finally:
            if tmp_archive is not None:
                tmp_archive.unlink(missing_ok=True)
    print(f"Exported: {output}\nSnapshot: {snapshot_id}")
    print("Private archive: do not upload to public GitHub or share publicly.")
    return snapshot_id


def _valid_member(name: str) -> bool:
    p = PurePosixPath(name)
    if (not name or name.startswith("/") or "\\" in name or
            any(x in {"", ".", ".."} for x in name.split("/"))):
        return False
    if name == "corpus.db":
        return True
    return len(p.parts) >= 2 and p.parts[0] == "files"


def _extract_verified(archive_path: Path, destination: Path) -> dict:
    with zipfile.ZipFile(archive_path, "r") as source:
        infos = source.infolist()
        if len(infos) != len({i.filename for i in infos}):
            raise SyncError("Archive has duplicate file names")
        if any(i.is_dir() or stat.S_IFMT(i.external_attr >> 16) == stat.S_IFLNK
               for i in infos):
            raise SyncError("Archive contains a directory/symlink entry")
        if sum(i.file_size for i in infos) > MAX_UNPACKED:
            raise SyncError("Archive exceeds 10 GiB extraction limit")
        if MANIFEST not in source.namelist():
            raise SyncError("Missing manifest.json")
        if source.getinfo(MANIFEST).file_size > 1024 * 1024:
            raise SyncError("Manifest is unexpectedly large")
        manifest = json.loads(source.read(MANIFEST))
        if (manifest.get("format") != FORMAT or
                not isinstance(manifest.get("snapshot_id"), str) or
                not isinstance(manifest.get("files"), list) or
                not isinstance(manifest.get("fingerprint"), str)):
            raise SyncError("Invalid snapshot manifest/version")
        descriptors = manifest["files"]
        expected = []
        for d in descriptors:
            if (not isinstance(d, dict) or not isinstance(d.get("path"), str)
                    or not _valid_member(d["path"]) or not isinstance(d.get("size"), int)
                    or d["size"] < 0 or not isinstance(d.get("sha256"), str)
                    or len(d["sha256"]) != 64):
                raise SyncError("Invalid file entry in manifest")
            expected.append(d["path"])
        if len(set(expected)) != len(expected) or "corpus.db" not in expected:
            raise SyncError("Duplicate or missing database in manifest")
        if set(source.namelist()) != set(expected) | {MANIFEST}:
            raise SyncError("Archive contains unexpected or missing entries")
        if _fingerprint(descriptors) != manifest["fingerprint"]:
            raise SyncError("Manifest fingerprint mismatch")
        for d in descriptors:
            entry = source.getinfo(d["path"])
            if entry.file_size != d["size"]:
                raise SyncError(f"Size mismatch: {d['path']}")
            target = destination.joinpath(*PurePosixPath(d["path"]).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with source.open(entry) as inp, target.open("xb") as out:
                for chunk in iter(lambda: inp.read(1024 * 1024), b""):
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != d["sha256"]:
                raise SyncError(f"Checksum mismatch: {d['path']}")
    _sqlite_ok(destination / "corpus.db")
    return manifest


def import_snapshot(archive: Path, *, root: Path | None = None, force: bool = False) -> str:
    root = Path(root or db.DATA_DIR).expanduser().resolve()
    archive = Path(archive).expanduser().resolve()
    if not archive.is_file():
        raise SyncError(f"Archive not found: {archive}")
    if _app_likely_running():
        raise SyncError("Local app on port 5731 is running. Stop it before importing.")
    if archive.is_relative_to(root):
        raise SyncError("Archive must live outside the active data directory")
    root.parent.mkdir(parents=True, exist_ok=True)
    # Keep temporary copies outside the Git worktree. Same filesystem as root.
    safe_parent = root.parent.parent
    with tempfile.TemporaryDirectory(prefix=".murakami-import-", dir=safe_parent) as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        manifest = _extract_verified(archive, stage)
        incoming_logical = _logical_fingerprint(stage)
        declared_logical = manifest.get("logical_fingerprint")
        if declared_logical is not None and declared_logical != incoming_logical:
            raise SyncError("Snapshot logical fingerprint mismatch")
        previous = _read_state(root) if root.exists() else None
        current_has_db = (root / "corpus.db").exists()
        if current_has_db:
            if not force and not previous:
                raise SyncError("Existing library has no sync history. First import needs "
                                "--force (a local backup is kept). Check which library is newer.")
            if not force and previous:
                baseline = previous.get("logical_fingerprint")
                if baseline is None:
                    raise SyncError("Legacy sync state needs adopt-baseline with the "
                                    "matching historical ZIP before importing.")
                unchanged = _logical_fingerprint(root) == baseline
                if manifest["snapshot_id"] == previous["snapshot_id"]:
                    if unchanged and incoming_logical == baseline:
                        print("Already synchronized; no changes made.")
                        return manifest["snapshot_id"]
                    raise SyncError("Local library changed after previous sync; import refused")
                if manifest.get("base_id") != previous["snapshot_id"]:
                    raise SyncError("Diverged sync history; import refused (manual resolution needed)")
                if not unchanged:
                    raise SyncError("Local library has unsynced changes; import refused")
        # Content integrity is checked against the archive manifest above.
        # A SQLite backup made on this machine may have different physical
        # page bytes than the sender's database, even when all rows match.
        # Record this machine's stable local snapshot fingerprint for guards.
        local_fingerprint = _fingerprint(_descriptors(stage))
        _write_state(stage, manifest["snapshot_id"], local_fingerprint,
                     incoming_logical)
        backup_target = None
        if root.exists():
            backups = safe_parent / BACKUP_FOLDER
            backups.mkdir(exist_ok=True)
            backup_target = backups / (_utc_now().replace(":", "-") + "-"
                                       + uuid.uuid4().hex[:8])
            root.rename(backup_target)
        try:
            stage.rename(root)
        except Exception:
            if backup_target is not None:
                backup_target.rename(root)
            raise
    print(f"Imported snapshot {manifest['snapshot_id']} into {root}")
    if backup_target is not None:
        print(f"Previous local library preserved: {backup_target}")
    return manifest["snapshot_id"]


def adopt_baseline(archive: Path, *, root: Path | None = None) -> str:
    """Upgrade existing v1 sync state without ever replacing the library.

    The historical archive must have the same snapshot id AND identical
    logical database rows/schema and original file hashes.
    """
    root = Path(root or db.DATA_DIR).expanduser().resolve()
    archive = Path(archive).expanduser().resolve()
    if _app_likely_running():
        raise SyncError("Stop the local app before adopting sync baseline")
    previous = _read_state(root)
    if not previous:
        raise SyncError("No existing sync state; refusing to create an unverified baseline")
    if previous.get("logical_fingerprint"):
        baseline = previous["logical_fingerprint"]
        if _logical_fingerprint(root) != baseline:
            raise SyncError("Local corpus has changed since its recorded baseline")
        print("Logical baseline already verified; no changes made.")
        return previous["snapshot_id"]
    with tempfile.TemporaryDirectory(prefix="murakami-adopt-") as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        manifest = _extract_verified(archive, stage)
        if manifest["snapshot_id"] != previous["snapshot_id"]:
            raise SyncError("Snapshot ID mismatch; refusing baseline upgrade")
        exported_logical = _logical_fingerprint(stage)
        local_logical = _logical_fingerprint(root)
        if exported_logical != local_logical:
            raise SyncError("Local corpus differs from historical archive; "
                            "baseline not adopted. Review changes manually.")
        _write_state(root, previous["snapshot_id"], previous["fingerprint"],
                     local_logical)
    print("Baseline verified and upgraded (database contents not replaced).")
    return previous["snapshot_id"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private single-writer library sync")
    sub = parser.add_subparsers(dest="operation", required=True)
    export = sub.add_parser("export", help="Create a consistent private ZIP snapshot")
    export.add_argument("archive", type=Path, help="New ZIP destination (never overwrites)")
    restore = sub.add_parser("import", help="Verify and back up before replacing local library")
    restore.add_argument("archive", type=Path, help="ZIP generated by the export command")
    restore.add_argument("--force", action="store_true",
                         help="Explicitly replace an untracked/diverged local library")
    adopt = sub.add_parser("adopt-baseline", help="Verify and upgrade old sync state, without replacing corpus")
    adopt.add_argument("archive", type=Path, help="ZIP matching the existing state snapshot ID")
    args = parser.parse_args(argv)
    try:
        if args.operation == "export":
            export_snapshot(args.archive)
        elif args.operation == "adopt-baseline":
            adopt_baseline(args.archive)
        else:
            import_snapshot(args.archive, force=args.force)
    except (SyncError, OSError, sqlite3.Error, zipfile.BadZipFile, ValueError) as exc:
        print(f"Sync aborted safely: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
