import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from edit_project_store import EditProjectStore
from edit_storage import (
    EditStoragePolicy,
    EditStorageService,
    ObjectStorageBackend,
    object_storage_from_env,
)
from media_ingest import MediaIngestService, MediaValidationError


DiskUsage = namedtuple("DiskUsage", "total used free")


def database(path: Path):
    connection = sqlite3.connect(path)
    connection.execute(
        """CREATE TABLE history (
        id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
        keyword TEXT NOT NULL, report TEXT NOT NULL,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)"""
    )
    connection.commit()
    connection.close()
    return lambda: sqlite3.connect(path)


def project(uuid_value: str, *, status="completed", filename="sample.mp4", updated_at=None):
    return {
        "schema_version": 1,
        "project_uuid": uuid_value,
        "status": status,
        "source": {"filename": filename, "storage_name": "source.mp4", "size_bytes": 300 * 1024 * 1024},
        "outputs": {},
        "updated_at": updated_at or datetime.now(timezone.utc).isoformat(),
    }


class FakeS3:
    def __init__(self):
        self.calls = []

    def upload_file(self, *args): self.calls.append(("upload", args))
    def download_file(self, *args): self.calls.append(("download", args)); Path(args[-1]).write_bytes(b"x")
    def delete_object(self, **kwargs): self.calls.append(("delete", kwargs))
    def generate_presigned_url(self, *args, **kwargs): self.calls.append(("signed", args, kwargs)); return "https://object.invalid/signed"


class FakeBackgroundRenderer:
    def render_project(self, **_kwargs):
        return (
            {"full": {"storage_name": "edited-v1.mp4", "size_bytes": 10}},
            [{"order": 1, "output": "full"}],
        )

    def advisory_log(self, _plan):
        return []


class EditStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = EditProjectStore(database(self.root / "history.db"), storage_root=self.root / "media")
        self.policy = EditStoragePolicy(
            reserve_bytes=128 * 1024 * 1024,
            source_retention_hours=72,
            output_retention_hours=720,
            failed_retention_hours=24,
            orphan_retention_hours=24,
            temp_retention_hours=1,
            output_ratio=1.0,
            short_ratio=0.2,
        )
        self.service = EditStorageService(self.store, policy=self.policy)

    def tearDown(self):
        self.temp.cleanup()

    def create(self, *, status="completed", filename="sample.mp4", age_hours=0):
        uuid_value = os.urandom(16).hex()
        updated = datetime.now(timezone.utc) - timedelta(hours=age_hours)
        payload = project(uuid_value, status=status, filename=filename, updated_at=updated.isoformat())
        project_id = self.store.create(keyword=filename, project=payload)
        directory = self.store.project_dir(uuid_value, create=True)
        return project_id, payload, directory

    def test_300mb_preflight_enough_and_shortfall(self):
        size = 300 * 1024 * 1024
        with patch("edit_storage.shutil.disk_usage", return_value=DiskUsage(5 << 30, 1 << 30, 4 << 30)):
            enough = self.service.estimate_upload(size, target_format="short_reel")
        self.assertTrue(enough["enough"])
        self.assertGreater(enough["required_bytes"], size * 2)
        with patch("edit_storage.shutil.disk_usage", return_value=DiskUsage(1 << 30, 700 << 20, 324 << 20)):
            short = self.service.estimate_upload(size, target_format="short_reel")
        self.assertFalse(short["enough"])
        self.assertGreater(short["shortfall_bytes"], 0)

    def test_media_ingest_rejects_before_creating_project_directory(self):
        ingest = MediaIngestService(self.store)
        with patch("edit_storage.shutil.disk_usage", return_value=DiskUsage(1 << 30, 700 << 20, 324 << 20)):
            with self.assertRaisesRegex(MediaValidationError, "안전한 편집"):
                ingest.validate_capacity(300 * 1024 * 1024, target_format="short_reel")
        self.assertEqual(list(self.store.storage_root.iterdir()), [])

    def test_snapshot_breakdown_and_project_sizes(self):
        _, payload, directory = self.create()
        (directory / "source.mp4").write_bytes(b"s" * 100)
        (directory / "edited-v1.mp4").write_bytes(b"f" * 80)
        (directory / "short-v1.mp4").write_bytes(b"h" * 40)
        (directory / "edit-decision-v1.json").write_text("{}")
        (directory / ".edited-v2.part.mp4").write_bytes(b"t" * 20)
        snapshot = self.service.snapshot()
        self.assertEqual(snapshot["managed_bytes"], 242)
        self.assertEqual(snapshot["categories"]["sources"], 100)
        self.assertEqual(snapshot["categories"]["temporary"], 20)
        self.assertEqual(snapshot["projects"][0]["filename"], payload["source"]["filename"])

    def test_cleanup_protects_running_project_even_after_restart(self):
        project_id, _, directory = self.create(status="rendering", filename="이태원_배경음악 제거.mp4", age_hours=1000)
        source = directory / "source.mp4"
        source.write_bytes(b"protected")
        part = directory / ".edited-v1.part.mp4"
        part.write_bytes(b"active")
        old = datetime.now().timestamp() - 10 * 3600
        os.utime(part, (old, old))
        result = self.service.cleanup(now=datetime.now(timezone.utc))
        self.assertIn(project_id, result["skipped_active"])
        self.assertTrue(source.exists())
        self.assertTrue(part.exists())

    def test_completed_temp_is_removed_but_outputs_and_recent_source_remain(self):
        project_id, payload, directory = self.create(status="completed", age_hours=2)
        source = directory / "source.mp4"
        output = directory / "edited-v1.mp4"
        temp = directory / ".edited-v2.part.mp4"
        source.write_bytes(b"source")
        output.write_bytes(b"output")
        temp.write_bytes(b"temp")
        old = datetime.now().timestamp() - 2 * 3600
        os.utime(temp, (old, old))
        payload["outputs"] = {"full": {"storage_name": output.name}}
        self.store.save(project_id, payload)
        result = self.service.cleanup(now=datetime.now(timezone.utc))
        self.assertFalse(temp.exists())
        self.assertTrue(source.exists())
        self.assertTrue(output.exists())
        self.assertEqual(result["deleted_bytes"], 4)

    def test_retention_never_removes_owner_media_without_confirmation(self):
        project_id, payload, directory = self.create(status="completed", age_hours=800)
        source = directory / "source.mp4"
        output = directory / "edited-v1.mp4"
        decision = directory / "edit-decision-v1.json"
        source.write_bytes(b"source")
        output.write_bytes(b"output")
        decision.write_bytes(b"decision")
        payload["outputs"] = {
            "full": {"storage_name": output.name}, "decision": {"storage_name": decision.name}
        }
        payload["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=800)).isoformat()
        self.store.save(project_id, payload)
        # save() refreshes updated_at, so restore the fixture timestamp in its JSON.
        with self.store._connect() as connection:
            row = connection.execute("SELECT report FROM history WHERE id=?", (project_id,)).fetchone()
            report = json.loads(row[0])
            report["updated_at"] = (datetime.now(timezone.utc) - timedelta(hours=800)).isoformat()
            connection.execute("UPDATE history SET report=? WHERE id=?", (json.dumps(report), project_id))
            connection.commit()
        result = self.service.cleanup(now=datetime.now(timezone.utc))
        self.assertEqual(result["deleted_bytes"], 0)
        self.assertTrue(source.exists())
        self.assertTrue(output.exists())
        purged = self.service.delete_project_files(project_id, scope="media")
        self.assertGreater(purged["deleted_bytes"], 0)
        self.assertFalse(source.exists())
        self.assertFalse(output.exists())
        self.assertTrue(decision.exists())
        saved = self.store.get(project_id)
        self.assertIsNotNone(saved)
        self.assertIn("decision", saved["report"]["outputs"])

    def test_orphan_cleanup_is_age_bounded_and_idempotent(self):
        orphan = self.store.project_dir(os.urandom(16).hex(), create=True)
        (orphan / "source.mp4").write_bytes(b"orphan")
        old = datetime.now().timestamp() - 25 * 3600
        os.utime(orphan, (old, old))
        first = self.service.cleanup(now=datetime.now(timezone.utc))
        second = self.service.cleanup(now=datetime.now(timezone.utc))
        self.assertFalse(orphan.exists())
        self.assertEqual(first["deleted_bytes"], 6)
        self.assertEqual(second["deleted_bytes"], 0)

    def test_manual_delete_blocks_active_and_preserves_completed_audit_row(self):
        active_id, _, active_dir = self.create(status="proposed")
        (active_dir / "source.mp4").write_bytes(b"active")
        with self.assertRaisesRegex(RuntimeError, "작업 중"):
            self.service.delete_project_files(active_id)
        done_id, payload, done_dir = self.create(status="completed")
        (done_dir / "source.mp4").write_bytes(b"source")
        (done_dir / "edited-v1.mp4").write_bytes(b"output")
        payload["outputs"] = {"full": {"storage_name": "edited-v1.mp4"}}
        self.store.save(done_id, payload)
        result = self.service.delete_project_files(done_id, scope="all")
        self.assertEqual(result["deleted_bytes"], 12)
        self.assertIsNotNone(self.store.get(done_id))
        self.assertEqual(self.store.get(done_id)["report"]["outputs"], {})

    def test_object_storage_contract_without_real_credentials(self):
        client = FakeS3()
        backend = ObjectStorageBackend(bucket="videos", prefix="ed", client=client)
        source = self.root / "source.mp4"
        source.write_bytes(b"video")
        key = backend.upload(source, project_uuid="a" * 32, filename="source.mp4")
        self.assertEqual(key, "ed/" + "a" * 32 + "/source.mp4")
        destination = self.root / "download.mp4"
        backend.download(key, destination)
        backend.delete(key)
        self.assertIn("signed", backend.presigned_download(key))
        self.assertEqual([item[0] for item in client.calls], ["upload", "download", "delete", "signed"])
        with patch.dict(os.environ, {"EDIT_STORAGE_BACKEND": "object", "EDIT_OBJECT_BUCKET": "videos"}):
            self.assertIsInstance(object_storage_from_env(client=client), ObjectStorageBackend)

    def test_presigned_preview_download_requests_attachment_disposition(self):
        client = FakeS3()
        backend = ObjectStorageBackend(bucket="videos", prefix="ed", client=client)
        backend.presigned_download(
            "ed/project/preview-v2.mp4",
            download_filename="초음파세척기-preview.mp4",
        )
        _, _, kwargs = client.calls[-1]
        disposition = kwargs["Params"]["ResponseContentDisposition"]
        self.assertIn("attachment", disposition)
        self.assertIn("preview.mp4", disposition)
        self.assertNotIn("초음파세척기", disposition)


class EditStorageApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.store = EditProjectStore(database(root / "history.db"), storage_root=root / "media")
        self.service = EditStorageService(
            self.store,
            policy=EditStoragePolicy(
                reserve_bytes=32 * 1024 * 1024, source_retention_hours=72,
                output_retention_hours=720, failed_retention_hours=24,
                orphan_retention_hours=24, temp_retention_hours=1,
                output_ratio=1.0, short_ratio=0.2,
            ),
        )
        self.client = TestClient(main.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_status_preflight_cleanup_and_active_delete_contracts(self):
        factory = lambda *args, **kwargs: self.service
        with patch.object(main, "EditStorageService", factory):
            status = self.client.get("/api/edit-storage")
            self.assertEqual(status.status_code, 200)
            self.assertIn("free_bytes", status.json())
            preflight = self.client.post(
                "/api/edit-storage/preflight",
                json={"file_size": 300 * 1024 * 1024, "target_format": "short_reel"},
            )
            self.assertEqual(preflight.status_code, 200)
            self.assertIn("required_bytes", preflight.json())
            cleanup = self.client.post("/api/edit-storage/cleanup", json={"dry_run": True})
            self.assertEqual(cleanup.status_code, 200)
            self.assertTrue(cleanup.json()["dry_run"])

            uuid_value = os.urandom(16).hex()
            payload = project(uuid_value, status="rendering")
            project_id = self.store.create(keyword="active", project=payload)
            directory = self.store.project_dir(uuid_value, create=True)
            (directory / "source.mp4").write_bytes(b"active")
            blocked = self.client.delete(f"/api/edit-projects/{project_id}/files?scope=all")
            self.assertEqual(blocked.status_code, 409)
            self.assertTrue((directory / "source.mp4").exists())

    def test_background_render_job_finishes_without_an_sse_consumer(self):
        uuid_value = os.urandom(16).hex()
        payload = project(uuid_value, status="rendering")
        payload.update({"render_runs": [], "timings": {}, "storage_state": {}})
        project_id = self.store.create(keyword="background", project=payload)
        directory = self.store.project_dir(uuid_value, create=True)
        (directory / "source.mp4").write_bytes(b"source")
        main.EDIT_RENDERING.add(project_id)
        main.EDIT_RENDER_TASKS[project_id] = object()
        with patch.object(main, "EditProjectStore", lambda *args, **kwargs: self.store), patch.object(
            main, "EditRenderService", FakeBackgroundRenderer
        ):
            asyncio.run(
                main._run_edit_render_job(
                    project_id,
                    payload,
                    {"plan": {"render_timeline": []}},
                    1,
                )
            )
        saved = self.store.get(project_id)["report"]
        self.assertEqual(saved["status"], "completed")
        self.assertIn("full", saved["outputs"])
        self.assertNotIn(project_id, main.EDIT_RENDERING)
        self.assertNotIn(project_id, main.EDIT_RENDER_TASKS)

    def test_object_preview_download_uses_attachment_presign(self):
        uuid_value = os.urandom(16).hex()
        payload = project(uuid_value, status="final_queued")
        payload["outputs"] = {
            "preview": {
                "storage_backend": "object",
                "object_key": f"ed/{uuid_value}/preview-v2.mp4",
                "filename": "preview-v2.mp4",
            }
        }
        project_id = self.store.create(keyword="preview", project=payload)

        class Backend:
            def __init__(self):
                self.options = None

            def presigned_download(self, _key, **options):
                self.options = options
                return "https://object.invalid/signed"

        backend = Backend()
        with patch.object(main, "EditProjectStore", lambda *args, **kwargs: self.store), patch.object(
            main, "object_storage_from_env", return_value=backend
        ):
            response = self.client.get(
                f"/api/edit-projects/{project_id}/outputs/preview?download=1",
                follow_redirects=False,
            )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(backend.options["download_filename"], "preview-v2.mp4")


if __name__ == "__main__":
    unittest.main()
