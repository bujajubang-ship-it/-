import importlib
import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from db_safety import (
    DatabaseRuntime,
    PRODUCTION_REQUIRED_TABLES,
    PRODUCTION_SQLITE_PATH,
    ProductionDatabaseError,
    load_database_runtime,
    validate_existing_sqlite_database,
    validate_production_sqlite_path,
)


def create_sqlite(path: Path, tables=PRODUCTION_REQUIRED_TABLES) -> None:
    with closing(sqlite3.connect(path)) as connection:
        for table in sorted(tables):
            connection.execute(
                f'CREATE TABLE "{table}" (id INTEGER PRIMARY KEY AUTOINCREMENT)'
            )


class ProductionPathPolicyTests(unittest.TestCase):
    def load_render(self, db_path_marker=...):
        environment = {"RENDER": "true", "DB_BACKEND": "sqlite"}
        if db_path_marker is not ...:
            environment["DB_PATH"] = db_path_marker
        with patch.dict(os.environ, environment, clear=True):
            return load_database_runtime()

    def test_missing_and_empty_db_path_fail_closed(self):
        for marker in (..., "   "):
            with self.subTest(marker=marker):
                with self.assertRaisesRegex(
                    ProductionDatabaseError, "Production SQLite DB_PATH is missing"
                ):
                    self.load_render(marker)

    def test_relative_repository_tmp_and_other_paths_fail_closed(self):
        cases = (
            ("history.db", "must be absolute"),
            (str(Path(__file__).resolve().parents[1] / "history.db"), "repository"),
            ("/tmp/history.db", "/tmp"),
            ("/var/lib/history.db", "Expected /data/history.db"),
        )
        for path, message in cases:
            with self.subTest(path=path):
                with self.assertRaisesRegex(ProductionDatabaseError, message):
                    self.load_render(path)

    def test_missing_production_file_fails_without_creating_it(self):
        candidate = PRODUCTION_SQLITE_PATH

        def fake_exists(path):
            return path == candidate.parent

        with (
            patch.object(Path, "exists", fake_exists),
            patch.object(Path, "is_dir", lambda path: path == candidate.parent),
            patch.object(Path, "is_symlink", lambda path: False),
        ):
            with self.assertRaisesRegex(
                ProductionDatabaseError, "Production database file does not exist"
            ):
                validate_production_sqlite_path(str(candidate))

    def test_normal_render_configuration_runs_read_only_preflight(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            database_path = Path(temp_dir) / "existing.db"
            create_sqlite(database_path)
            environment = {
                "RENDER": "true",
                "DB_BACKEND": "sqlite",
                "DB_PATH": "/data/history.db",
            }
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "db_safety.validate_production_sqlite_path",
                    return_value=database_path,
                ),
            ):
                runtime = load_database_runtime()
            self.assertTrue(runtime.production)
            self.assertEqual(runtime.path, database_path)


class ProductionDatabasePreflightTests(unittest.TestCase):
    def test_invalid_sqlite_file_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "invalid.db"
            path.write_bytes(b"this is not a SQLite database")
            with self.assertRaisesRegex(
                ProductionDatabaseError, "cannot be opened safely"
            ):
                validate_existing_sqlite_database(path)

    def test_missing_required_table_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "missing.db"
            create_sqlite(path, PRODUCTION_REQUIRED_TABLES - {"knowledge"})
            with self.assertRaisesRegex(
                ProductionDatabaseError, "missing required tables: knowledge"
            ):
                validate_existing_sqlite_database(path)

    def test_established_six_table_database_passes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "complete.db"
            create_sqlite(path)
            validate_existing_sqlite_database(path)

    def test_production_runtime_never_creates_a_missing_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            missing = Path(temp_dir) / "must-not-be-created.db"
            runtime = DatabaseRuntime(path=missing, production=True)
            with self.assertRaises(sqlite3.OperationalError):
                runtime.connect()
            self.assertFalse(missing.exists())

    def test_existing_production_schema_is_not_recreated_or_changed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "established.db"
            development_environment = {
                "RENDER": "false",
                "DB_BACKEND": "sqlite",
                "DB_PATH": str(path),
            }
            with patch.dict(os.environ, development_environment, clear=True):
                sys.modules.pop("database", None)
                development = importlib.import_module("database")
            development.init_db()
            development.init_pipeline()
            development.list_optimize()
            development.list_worksheet()
            development.list_chat_sessions()
            development.list_knowledge()
            sys.modules.pop("database", None)

            with closing(sqlite3.connect(path)) as connection:
                before = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()

            with patch(
                "db_safety.load_database_runtime",
                return_value=DatabaseRuntime(path=path, production=True),
            ):
                production = importlib.import_module("database")
            try:
                production.init_db()
                production.init_pipeline()
                production.list_optimize()
                production.list_worksheet()
                production.list_chat_sessions()
                production.list_knowledge()
            finally:
                sys.modules.pop("database", None)

            with closing(sqlite3.connect(path)) as connection:
                after = connection.execute(
                    "SELECT name, sql FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            self.assertEqual(after, before)


class DevelopmentCompatibilityTests(unittest.TestCase):
    def test_development_without_db_path_keeps_fallback_and_crud(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "development.db"
            environment = {
                "RENDER": "false",
                "DB_BACKEND": "sqlite",
                "DB_PATH": str(path),
            }
            with patch.dict(os.environ, environment, clear=True):
                sys.modules.pop("database", None)
                database = importlib.import_module("database")
            try:
                database.init_db()
                database.init_pipeline()
                database.list_optimize()
                database.list_worksheet()
                database.list_chat_sessions()
                database.list_knowledge()
                database.save_history("planning", "local", {"ok": True})
                self.assertEqual(database.list_history()[0]["keyword"], "local")
                with closing(sqlite3.connect(path)) as connection:
                    actual_tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                self.assertEqual(
                    actual_tables & PRODUCTION_REQUIRED_TABLES,
                    PRODUCTION_REQUIRED_TABLES,
                )
            finally:
                sys.modules.pop("database", None)

    def test_database_url_alone_does_not_enable_postgresql(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            environment = {
                "RENDER": "false",
                "DB_PATH": str(Path(temp_dir) / "sqlite.db"),
                "DATABASE_URL": "postgresql://must-not-connect/production",
            }
            with patch.dict(os.environ, environment, clear=True):
                runtime = load_database_runtime()
            self.assertFalse(runtime.production)

    def test_explicit_postgresql_backend_is_not_in_this_release(self):
        environment = {
            "RENDER": "false",
            "DB_BACKEND": "postgresql",
            "DATABASE_URL": "postgresql://must-not-connect/production",
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(
                ProductionDatabaseError, "only DB_BACKEND=sqlite"
            ):
                load_database_runtime()


if __name__ == "__main__":
    unittest.main()
