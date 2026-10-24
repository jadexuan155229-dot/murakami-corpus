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
        return state
    except (ValueError, OSError) as exc:
        raise SyncError(f"Cannot read sync state {path}: {exc}") from exc


def _write_state(root: Path, snapshot_id: str, fingerprint: str) -> None:
    state = {"format": FORMAT, "snapshot_id": snapshot_id,
             "fingerprint": fingerprint, "recorded_at": _utc_now()}
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
                archive.writestr(MANIFEST,
                                 json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            # Prevent marking a snapshot as current if the library changed while copying.
            if _descriptors(root) != descriptors:
                raise SyncError("Library changed during export; close the app and retry")
            if output.exists():
                raise SyncError(f"Archive already exists; choose a new name: {output}")
            os.replace(tmp_archive, output)
            tmp_archive = None
            _write_state(root, snapshot_id, manifest["fingerprint"])
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
    with tempfile.TemporaryDirectory(prefix=".murakami-import-", dir=root.parent) as tmp:
        stage = Path(tmp) / "data"
        stage.mkdir()
        manifest = _extract_verified(archive, stage)
        previous = _read_state(root) if root.exists() else None
        current_has_db = (root / "corpus.db").exists()
        if current_has_db:
            if not force and not previous:
                raise SyncError("Existing library has no sync history. First import needs "
                                "--force (a local backup is kept). Check which library is newer.")
            if not force and previous:
                if manifest["snapshot_id"] == previous["snapshot_id"]:
                    if _fingerprint(_descriptors(root)) == previous["fingerprint"]:
                        print("Already synchronized; no changes made.")
                        return manifest["snapshot_id"]
                    raise SyncError("Local library changed after previous sync; import refused")
                if manifest.get("base_id") != previous["snapshot_id"]:
                    raise SyncError("Diverged sync history; import refused (manual resolution needed)")
                if _fingerprint(_descriptors(root)) != previous["fingerprint"]:
                    raise SyncError("Local library has unsynced changes; import refused")
        _write_state(stage, manifest["snapshot_id"], manifest["fingerprint"])
        backup_target = None
        if root.exists():
            backups = root.parent / BACKUP_FOLDER
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Private single-writer library sync")
    sub = parser.add_subparsers(dest="operation", required=True)
    export = sub.add_parser("export", help="Create a consistent private ZIP snapshot")
    export.add_argument("archive", type=Path, help="New ZIP destination (never overwrites)")
    restore = sub.add_parser("import", help="Verify and back up before replacing local library")
    restore.add_argument("archive", type=Path, help="ZIP generated by the export command")
    restore.add_argument("--force", action="store_true",
                         help="Explicitly replace an untracked/diverged local library")
    args = parser.parse_args(argv)
    try:
        if args.operation == "export":
            export_snapshot(args.archive)
        else:
            import_snapshot(args.archive, force=args.force)
    except (SyncError, OSError, sqlite3.Error, zipfile.BadZipFile, ValueError) as exc:
        print(f"Sync aborted safely: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
