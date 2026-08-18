import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from edit_job_queue import EditJobQueue
from edit_project_store import EditProjectStore, migrate_project, transition_project
from edit_quality_service import EditQualityError, EditQualityService
from edit_storage import EditStorageService, ObjectStorageBackend


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
    return lambda: sqlite3.connect(path, timeout=10)


class MultipartS3:
    def __init__(self):
        self.objects = {}
        self.parts = {}
        self.aborted = []

    def create_multipart_upload(self, **kwargs):
        self.parts[(kwargs["Key"], "upload-1")] = []
        return {"UploadId": "upload-1"}

    def generate_presigned_url(self, operation, Params, ExpiresIn):
        return f"https://objects.invalid/{operation}/{Params.get('PartNumber', 0)}"

    def list_parts(self, **kwargs):
        return {"Parts": self.parts.get((kwargs["Key"], kwargs["UploadId"]), []), "IsTruncated": False}

    def complete_multipart_upload(self, **kwargs):
        rows = kwargs["MultipartUpload"]["Parts"]
        self.objects[kwargs["Key"]] = b"x" * sum(8 for _ in rows)

    def abort_multipart_upload(self, **kwargs):
        self.aborted.append(kwargs["Key"])

    def head_object(self, **kwargs):
        value = self.objects[kwargs["Key"]]
        return {"ContentLength": len(value), "ETag": '"etag"', "ContentType": "video/mp4"}

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = bytes(kwargs["Body"])

    def delete_object(self, **kwargs):
        self.objects.pop(kwargs["Key"], None)

    def list_objects_v2(self, **kwargs):
        return {"Contents": [{"Key": key, "Size": len(value)} for key, value in self.objects.items()], "IsTruncated": False}


class DurableQueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connect = database(self.root / "history.db")
        self.queue = EditJobQueue(self.connect)

    def tearDown(self):
        self.temp.cleanup()

    def test_idempotent_queue_priority_retry_and_restart_recovery(self):
        first = self.queue.enqueue(1, "analysis", idempotency_key="analysis:1", priority=50)
        duplicate = self.queue.enqueue(1, "analysis", idempotency_key="analysis:1", priority=50)
        render = self.queue.enqueue(2, "rendering", idempotency_key="render:2:v1", priority=10)
        self.assertEqual(first["job_id"], duplicate["job_id"])
        claimed = self.queue.claim("worker-a")
        self.assertEqual(claimed["job_id"], render["job_id"])
        with self.connect() as connection:
            report = json.loads(connection.execute("SELECT report FROM history WHERE id=?", (render["job_id"],)).fetchone()[0])
            report["heartbeat_at"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
            connection.execute("UPDATE history SET report=? WHERE id=?", (json.dumps(report), render["job_id"]))
            connection.commit()
        self.assertEqual(self.queue.recover_stale(stale_seconds=60), [render["job_id"]])
        reclaimed = self.queue.claim("worker-b")
        self.assertEqual(reclaimed["job_id"], render["job_id"])
        failed = self.queue.fail(reclaimed["job_id"], TimeoutError("temporary"), retryable=False)
        self.assertEqual(failed["status"], "failed")
        retried = self.queue.retry(reclaimed["job_id"])
        self.assertEqual(retried["status"], "queued")
        self.assertEqual(len(self.queue.list()), 2)

    def test_five_projects_and_sixty_minute_metadata_remain_serial(self):
        for project_id in range(1, 6):
            self.queue.enqueue(
                project_id, "rendering", idempotency_key=f"render:{project_id}:v1",
                payload={"duration_seconds": 3600}, priority=60,
            )
        running = self.queue.claim("one-heavy-worker", allowed_types={"rendering"})
        snapshot = self.queue.snapshot()
        self.assertEqual(snapshot["counts"]["running"], 1)
        self.assertEqual(snapshot["counts"]["queued"], 4)
        self.assertEqual([row["queue_position"] for row in snapshot["active"] if row["status"] == "queued"], [1, 2, 3, 4])
        self.queue.finish(running["job_id"])


class LifecycleAndQualityTests(unittest.TestCase):
    def test_additive_lifecycle_migration_and_state_audit(self):
        legacy = migrate_project({"project_uuid": "a" * 32, "status": "completed", "custom": {"keep": True}})
        self.assertEqual(legacy["schema_version"], 2)
        self.assertEqual(legacy["lifecycle_status"], "COMPLETED")
        changed = transition_project(legacy, "published_or_downloaded", reason="owner link")
        self.assertTrue(changed["custom"]["keep"])
        self.assertEqual(changed["state_history"][-1]["to"], "published_or_downloaded")

    @unittest.skipUnless(Path("/usr/local/bin/ffmpeg").exists() or __import__("shutil").which("ffmpeg"), "ffmpeg required")
    def test_post_render_qa_accepts_valid_and_rejects_duration_mismatch(self):
        with tempfile.TemporaryDirectory() as td:
            output = Path(td) / "qa.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=20",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2", "-c:v", "libx264", "-c:a", "aac", str(output), "-y",
            ], check=True)
            plan = {"render_timeline": [{"source_start": 0, "source_end": 2}], "target_length_seconds": 2}
            report = EditQualityService().validate(source=str(output), plan=plan, output_kind="full", expected_duration=2, require_audio=True)
            self.assertIn(report["status"], {"passed", "warning"})
            with self.assertRaises(EditQualityError):
                EditQualityService().validate(source=str(output), plan=plan, output_kind="full", expected_duration=20, require_audio=True)


class MultipartAndPurgeApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.connect = database(self.root / "history.db")
        self.store = EditProjectStore(self.connect, storage_root=self.root / "media")
        self.queue = EditJobQueue(self.connect)
        self.s3 = MultipartS3()
        self.backend = ObjectStorageBackend(bucket="videos", prefix="ed", client=self.s3)
        self.client = TestClient(main.app)

    def tearDown(self):
        self.temp.cleanup()

    def test_multipart_contract_is_idempotent_and_analysis_is_queued(self):
        payload = {
            "client_upload_id": "stable-upload-123", "filename": "long.mp4", "file_size": 16,
            "content_type": "video/mp4", "video_type": "raw_footage", "target_format": "long_form",
            "target_length_seconds": 0, "purpose": "현장기록형", "topic": "long", "strategy_id": None,
        }
        patches = (
            patch.object(main, "EditProjectStore", lambda: self.store),
            patch.object(main, "EDIT_JOB_QUEUE", self.queue),
            patch.object(main, "object_storage_configured", return_value=True),
            patch.object(main, "object_storage_from_env", return_value=self.backend),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            started = self.client.post("/api/edit-uploads/multipart/start", json=payload)
            self.assertEqual(started.status_code, 200)
            again = self.client.post("/api/edit-uploads/multipart/start", json=payload)
            self.assertEqual(again.json()["project_id"], started.json()["project_id"])
            project_id = started.json()["project_id"]
            key = self.store.get(project_id)["report"]["source"]["object_key"]
            self.s3.objects[key] = b"x" * 16
            done = self.client.post(
                f"/api/edit-uploads/{project_id}/complete",
                json={"parts": [{"part_number": 1, "etag": "one"}, {"part_number": 2, "etag": "two"}]},
            )
            self.assertEqual(done.status_code, 200)
            self.assertEqual(done.json()["project"]["lifecycle_status"], "UPLOADED")
            self.assertEqual(self.queue.list(project_id=project_id)[0]["type"], "analysis")

    def test_media_purge_requires_confirmation_and_preserves_decision_audit(self):
        project_uuid = os.urandom(16).hex()
        directory = self.store.project_dir(project_uuid, create=True)
        for name, data in (("source.mp4", b"source"), ("edited-v1.mp4", b"full"), ("short-v1.mp4", b"short"), ("edit-decision-v1.json", b"{}")):
            (directory / name).write_bytes(data)
        project_id = self.store.create(keyword="done", project={
            "project_uuid": project_uuid, "status": "completed",
            "source": {"filename": "source.mp4", "storage_name": "source.mp4"},
            "outputs": {
                "full": {"storage_name": "edited-v1.mp4"}, "short": {"storage_name": "short-v1.mp4"},
                "decision": {"storage_name": "edit-decision-v1.json"},
            },
            "plan_versions": [{"version": 1, "plan": {"segments": []}}], "conversation": [{"role": "user", "content": "keep"}],
        })
        with patch.object(main, "EditProjectStore", lambda: self.store), patch.object(
            main, "EditStorageService", lambda *a, **k: EditStorageService(self.store)
        ), patch.object(main, "EDIT_JOB_QUEUE", self.queue):
            denied = self.client.post(f"/api/edit-projects/{project_id}/media-purge", json={"confirmed": False})
            self.assertEqual(denied.status_code, 409)
            done = self.client.post(f"/api/edit-projects/{project_id}/media-purge", json={"confirmed": True})
            self.assertEqual(done.status_code, 200)
        saved = self.store.get(project_id)["report"]
        self.assertEqual(saved["lifecycle_status"], "MEDIA_PURGED")
        self.assertEqual(saved["conversation"][0]["content"], "keep")
        self.assertIn("decision", saved["outputs"])
        self.assertTrue((directory / "edit-decision-v1.json").exists())
        self.assertFalse((directory / "source.mp4").exists())


if __name__ == "__main__":
    unittest.main()
