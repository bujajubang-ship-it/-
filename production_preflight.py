"""Fail-closed production SQLite inspection and online backup.

The command never modifies the source database and refuses to overwrite a
backup. It prints metadata only; application rows are never emitted.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import quote

from db_safety import PRODUCTION_REQUIRED_TABLES


@dataclass(frozen=True)
class DatabaseEvidence:
    path: str
    size_bytes: int
    integrity: str
    row_counts: dict[str, int]
    schema_sha256: str
    file_sha256: str | None = None


def _read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=30)
    connection.execute("PRAGMA query_only = ON")
    return connection


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inspect_connection(
    connection: sqlite3.Connection,
    path: Path,
    *,
    include_file_hash: bool,
) -> DatabaseEvidence:
    check = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    if check != ["ok"]:
        raise RuntimeError("SQLite integrity_check failed: " + "; ".join(check[:3]))
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    missing = sorted(PRODUCTION_REQUIRED_TABLES - tables)
    if missing:
        raise RuntimeError("Required production tables are missing: " + ", ".join(missing))
    counts = {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in sorted(PRODUCTION_REQUIRED_TABLES)
    }
    schema_rows = connection.execute(
        """
        SELECT type, name, tbl_name, COALESCE(sql, '')
        FROM sqlite_master
        WHERE name NOT LIKE 'sqlite_%'
        ORDER BY type, name
        """
    ).fetchall()
    schema_text = json.dumps(schema_rows, ensure_ascii=False, separators=(",", ":"))
    return DatabaseEvidence(
        path=str(path),
        size_bytes=path.stat().st_size,
        integrity="ok",
        row_counts=counts,
        schema_sha256=hashlib.sha256(schema_text.encode("utf-8")).hexdigest(),
        file_sha256=_sha256(path) if include_file_hash else None,
    )


def inspect_database(path: Path, *, include_file_hash: bool = False) -> DatabaseEvidence:
    path = path.resolve()
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"SQLite source must be an existing regular file: {path}")
    with closing(_read_only(path)) as connection:
        return _inspect_connection(
            connection, path, include_file_hash=include_file_hash
        )


def create_verified_backup(source: Path, destination: Path) -> tuple[DatabaseEvidence, DatabaseEvidence]:
    source = source.resolve()
    if not destination.is_absolute():
        raise RuntimeError("Backup destination must be absolute")
    destination = destination.resolve()
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"SQLite source must be an existing regular file: {source}")
    if destination == source:
        raise RuntimeError("Backup destination must differ from the source database")
    if destination.exists():
        raise RuntimeError(f"Backup destination already exists; refusing overwrite: {destination}")
    if not destination.parent.is_dir() or destination.parent.is_symlink():
        raise RuntimeError("Backup destination parent must be an existing regular directory")

    created = False
    try:
        with closing(_read_only(source)) as source_db:
            source_db.execute("BEGIN")
            source_evidence = _inspect_connection(
                source_db, source, include_file_hash=False
            )
            with closing(sqlite3.connect(str(destination), timeout=30)) as backup_db:
                created = True
                source_db.backup(backup_db)
            source_db.execute("COMMIT")
        os.chmod(destination, 0o600)
        backup_evidence = inspect_database(destination, include_file_hash=True)
        if source_evidence.row_counts != backup_evidence.row_counts:
            raise RuntimeError("Backup row counts do not match the consistent source snapshot")
        if source_evidence.schema_sha256 != backup_evidence.schema_sha256:
            raise RuntimeError("Backup schema hash does not match the source snapshot")
        return source_evidence, backup_evidence
    except Exception:
        if created and destination.exists():
            destination.unlink()
        raise


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect production SQLite and optionally create a verified online backup."
    )
    parser.add_argument(
        "--source",
        default=os.getenv("DB_PATH", "/data/history.db"),
        help="Existing SQLite source (default: DB_PATH or /data/history.db)",
    )
    parser.add_argument(
        "--backup",
        help="Absolute new backup file. Existing files are never overwritten.",
    )
    args = parser.parse_args()

    source = Path(args.source)
    if args.backup:
        original, backup = create_verified_backup(source, Path(args.backup))
        payload = {
            "ok": True,
            "mode": "backup",
            "source": asdict(original),
            "backup": asdict(backup),
        }
    else:
        payload = {
            "ok": True,
            "mode": "read_only",
            "source": asdict(inspect_database(source)),
        }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
