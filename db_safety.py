"""Fail-closed SQLite selection for the Render production service."""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote


SQLITE_BACKEND = "sqlite"
PROJECT_ROOT = Path(__file__).resolve().parent
PRODUCTION_SQLITE_PATH = Path("/data/history.db")
PRODUCTION_REQUIRED_TABLES = frozenset(
    {
        "history",
        "pipeline",
        "optimize_videos",
        "worksheet_rows",
        "chat_session",
        "knowledge",
    }
)


class ProductionDatabaseError(RuntimeError):
    """Raised before an unsafe or incomplete production DB can be used."""


def _env_truthy(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    return value not in {"", "0", "false", "no", "off"}


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def validate_production_sqlite_path(raw_path: str | None) -> Path:
    """Accept only the confirmed persistent-disk file and create nothing."""

    if raw_path is None or not raw_path.strip():
        raise ProductionDatabaseError("Production SQLite DB_PATH is missing")

    candidate = Path(raw_path.strip()).expanduser()
    if not candidate.is_absolute():
        raise ProductionDatabaseError(
            f"Production SQLite DB_PATH must be absolute; received {raw_path!r}"
        )

    resolved_candidate = candidate.resolve(strict=False)
    if _is_within(resolved_candidate, PROJECT_ROOT.resolve(strict=False)):
        raise ProductionDatabaseError(
            f"Production SQLite DB_PATH must not be inside the repository; received {raw_path!r}"
        )
    if _is_within(resolved_candidate, Path("/tmp").resolve(strict=False)):
        raise ProductionDatabaseError(
            f"Production SQLite DB_PATH must not use /tmp; received {raw_path!r}"
        )
    if candidate != PRODUCTION_SQLITE_PATH:
        raise ProductionDatabaseError(
            f"Expected /data/history.db but received {raw_path!r}"
        )

    directory = PRODUCTION_SQLITE_PATH.parent
    if not directory.exists():
        raise ProductionDatabaseError("Production database directory /data does not exist")
    if not directory.is_dir():
        raise ProductionDatabaseError("Production database directory /data is not a directory")
    if directory.is_symlink():
        raise ProductionDatabaseError("Production database directory /data must not be a symlink")
    if not candidate.exists():
        raise ProductionDatabaseError("Production database file does not exist: /data/history.db")
    if not candidate.is_file():
        raise ProductionDatabaseError("Production database path is not a file: /data/history.db")
    if candidate.is_symlink():
        raise ProductionDatabaseError("Production database file must not be a symlink")
    try:
        if candidate.resolve(strict=True) != PRODUCTION_SQLITE_PATH:
            raise ProductionDatabaseError(
                "Production database resolved outside /data/history.db"
            )
    except OSError as exc:
        raise ProductionDatabaseError(
            f"Production database path cannot be resolved: {exc}"
        ) from exc
    return candidate


def validate_existing_sqlite_database(
    path: Path, *, required_tables: frozenset[str] = PRODUCTION_REQUIRED_TABLES
) -> None:
    """Open read-only, run quick_check, and verify the established six tables."""

    uri = f"file:{quote(str(path), safe='/')}?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            connection.execute("PRAGMA query_only = ON")
            check_messages = [
                str(row[0])
                for row in connection.execute("PRAGMA quick_check").fetchall()
            ]
            if check_messages != ["ok"]:
                detail = "; ".join(check_messages[:3]) or "unknown result"
                raise ProductionDatabaseError(
                    f"Production SQLite quick_check failed: {detail}"
                )
            existing_tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
    except ProductionDatabaseError:
        raise
    except sqlite3.Error as exc:
        raise ProductionDatabaseError(
            f"Production SQLite database cannot be opened safely: {exc}"
        ) from exc

    missing_tables = sorted(required_tables - existing_tables)
    if missing_tables:
        raise ProductionDatabaseError(
            "Production database is missing required tables: "
            + ", ".join(missing_tables)
        )


@dataclass(frozen=True)
class DatabaseRuntime:
    path: Path
    production: bool

    def connect(self) -> sqlite3.Connection:
        if self.production:
            # mode=rw permits normal CRUD but refuses to create a replacement DB.
            uri = f"file:{quote(str(self.path), safe='/')}?mode=rw"
            connection = sqlite3.connect(uri, uri=True, timeout=30)
        else:
            connection = sqlite3.connect(str(self.path), timeout=30)
        # Normal concurrent reads and short background writes should wait for a
        # bounded period instead of immediately failing with "database is locked".
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection


def load_database_runtime() -> DatabaseRuntime:
    backend = os.getenv("DB_BACKEND", SQLITE_BACKEND).strip().lower()
    if backend != SQLITE_BACKEND:
        raise ProductionDatabaseError(
            "This release supports only DB_BACKEND=sqlite; PostgreSQL is not enabled"
        )

    render_environment = _env_truthy("RENDER")
    raw_path = os.getenv("DB_PATH")
    if render_environment:
        path = validate_production_sqlite_path(raw_path)
        validate_existing_sqlite_database(path)
        return DatabaseRuntime(path=path, production=True)

    path = Path(raw_path) if raw_path else PROJECT_ROOT / "history.db"
    return DatabaseRuntime(path=path.expanduser().resolve(), production=False)
