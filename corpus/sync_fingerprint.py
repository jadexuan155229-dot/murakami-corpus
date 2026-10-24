"""Portable semantic SQLite fingerprint for private library synchronization.

Includes schema, user_version, application_id, row IDs, all user table data and
queryable FTS virtual-table rows. Excludes FTS shadow storage and optimizer
statistics, which can change without changing any stored literary content.

Requires SQLite >= 3.37 for reliable PRAGMA table_list shadow identification.
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
from pathlib import Path


def _feed(hasher, value):
    """Unambiguous, type-tagged and length-framed encoding of SQLite values."""
    if value is None:
        kind, data = b"N", b""
    elif isinstance(value, bytes):
        kind, data = b"B", value
    elif isinstance(value, str):
        kind, data = b"T", value.encode("utf-8", "surrogatepass")
    elif isinstance(value, int):
        kind, data = b"I", str(value).encode("ascii")
    elif isinstance(value, float):
        kind, data = b"F", struct.pack("!d", value)
    else:
        raise TypeError(f"Unsupported SQLite value: {type(value)!r}")
    hasher.update(kind)
    hasher.update(len(data).to_bytes(8, "big"))
    hasher.update(data)


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


def logical_sqlite_hash(path: Path) -> str:
    """Hash logical content, independently of SQLite page layout and VACUUM."""
    source = Path(path)
    con = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        table_list = list(con.execute("PRAGMA table_list"))
        if not table_list:
            raise RuntimeError("SQLite 3.37+ required for PRAGMA table_list")
        main_tables = {
            entry[1]: entry for entry in table_list
            if entry[0] == "main" and entry[2] in ("table", "virtual", "shadow")
        }
        skipped = {name for name, entry in main_tables.items() if entry[2] == "shadow"}
        # ANALYZE can rebuild statistics without changing user-facing content.
        skipped.update(name for name in main_tables if name.startswith("sqlite_stat"))
        skipped.update(("sqlite_schema", "sqlite_temp_schema"))
        schema = list(con.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
        ))
        h = hashlib.sha256()
        _feed(h, "murakami-sqlite-content-v1")
        for pragma in ("user_version", "application_id"):
            _feed(h, pragma)
            _feed(h, con.execute(f"PRAGMA {pragma}").fetchone()[0])
        for obj in schema:
            if obj[1] in skipped or obj[2] in skipped:
                continue
            for field in obj:
                _feed(h, field)
        for name in sorted(main_tables):
            if name in skipped:
                continue
            entry = main_tables[name]
            if entry[2] not in ("table", "virtual"):
                continue
            _feed(h, "TABLE")
            _feed(h, name)
            sql_name = _quote(name)
            if entry[4]:  # WITHOUT ROWID
                columns = [row[1] for row in con.execute(f"PRAGMA table_info({sql_name})")]
                order = ", ".join(_quote(col) + " COLLATE BINARY" for col in columns)
                query = f"SELECT * FROM {sql_name} ORDER BY {order}"
            else:
                query = f"SELECT rowid, * FROM {sql_name} ORDER BY rowid"
            for row in con.execute(query):
                _feed(h, "ROW")
                for value in row:
                    _feed(h, value)
            _feed(h, "END_TABLE")
        return h.hexdigest()
    finally:
        con.close()
