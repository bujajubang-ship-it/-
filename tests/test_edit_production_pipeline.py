import asyncio
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
from edit_job_queue import EditJobQueue, EditJobWorker
from edit_pipeline import EditPipeline
from edit_project_store import EditProjectStore, migrate_project, transition_project
from edit_quality_service import EditQualityError, EditQualityService
from edit_render_contract import build_final_render_payload, render_profile, requires_external_final
from edit_render_service import EditRenderService
from edit_storage import EditStorageService, ObjectStorageBackend
from media_ingest import MediaIngestService


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
        self.downloads = []
        self.fail_copy_key = None

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

    def download_file(self, bucket, key, destination):
        self.downloads.append(key)
        Path(destination).write_bytes(self.objects[key])

    def upload_file(self, source, bucket, key, ExtraArgs=None):
        self.objects[key] = Path(source).read_bytes()

    def copy_object(self, **kwargs):
        if kwargs["Key"] == self.fail_copy_key:
            raise RuntimeError("temporary copy failure")
        self.objects[kwargs["Key"]] = self.objects[kwargs["CopySource"]["Key"]]
        return {"CopyObjectResult": {"ETag": '"copied"'}}


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

    def test_external_final_payload_is_self_contained_and_not_claimed_by_embedded_worker(self):
        self.assertEqual(
            {render_profile(name).name for name in ("preview_720p", "preview_1080p", "final_original")},
            {"preview_720p", "preview_1080p", "final_original"},
        )
        backend = ObjectStorageBackend(bucket="videos", prefix="ed", client=MultipartS3())
        project = {
            "project_uuid": "a" * 32,
            "source": {
                "storage_backend": "object", "object_key": "ed/" + "a" * 32 + "/source.mp4",
                "filename": "source.mp4", "size_bytes": 292 * 1024 * 1024,
                "media": {"width": 3840, "height": 2160, "duration": 300, "has_audio": True},
            },
        }
        plan = {
            "render_timeline": [{"source_start": 0, "source_end": 20}],
            "short_timeline": [], "create_short_highlight": False,
            "estimated_output_duration": 20, "enhancements": [],
        }
        self.assertTrue(requires_external_final(project))
        payload = build_final_render_payload(project, approved_version=2, plan=plan, backend=backend)
        job = self.queue.enqueue(9, "final_rendering", payload=payload, idempotency_key="final:9:v2")
        self.assertEqual(job["payload"]["job_id"], job["job_id"])
        self.assertEqual(job["payload"]["source"]["object_key"], project["source"]["object_key"])
        self.assertEqual(job["payload"]["edl"]["render_timeline"], plan["render_timeline"])
        self.assertEqual(job["payload"]["render_profile"]["name"], "final_original")
        self.assertIn("staging_key", job["payload"]["output_target"]["objects"]["full"])
        self.assertIsNone(self.queue.claim("api-worker", allowed_types={"analysis", "rendering", "preview_rendering"}))

    def test_atomic_object_publish_cleans_partial_final_on_failure(self):
        s3 = MultipartS3()
        backend = ObjectStorageBackend(bucket="videos", prefix="ed", client=s3)
        targets = {
            "full": {"staging_key": "ed/p/.stage-full", "final_key": "ed/p/full.mp4", "content_type": "video/mp4"},
            "decision": {"staging_key": "ed/p/.stage-edl", "final_key": "ed/p/edl.json", "content_type": "application/json"},
        }
        s3.objects["ed/p/.stage-full"] = b"video"
        s3.objects["ed/p/.stage-edl"] = b"{}"
        s3.fail_copy_key = "ed/p/edl.json"
        with self.assertRaisesRegex(RuntimeError, "copy failure"):
            backend.publish_staged(targets)
        self.assertNotIn("ed/p/full.mp4", s3.objects)
        self.assertNotIn("ed/p/edl.json", s3.objects)
        self.assertIn("ed/p/.stage-full", s3.objects)
        s3.fail_copy_key = None
        published = backend.publish_staged(targets)
        self.assertEqual(set(published), {"full", "decision"})
        self.assertNotIn("ed/p/.stage-full", s3.objects)


class CheckpointedTranscriptTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_transcript_chunk_is_reused_and_progress_is_reported(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"EDIT_TRANSCRIPT_CHUNK_SECONDS": "600"}
        ):
            root = Path(td)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ingest = MediaIngestService()
            extract_starts = []
            progress = []

            def extract(_source, output, _duration, *, start=0.0):
                extract_starts.append(start)
                output.write_bytes(b"audio")
                return output

            async def transcribe(_audio):
                return {
                    "text": "두 번째 구간",
                    "segments": [{"start": 1.0, "end": 4.0, "text": "두 번째 구간"}],
                    "provider": "openai",
                }

            existing = [{
                "chunk_index": 0, "start": 0.0, "end": 600.0,
                "status": "completed",
                "transcript": {
                    "text": "첫 번째 구간",
                    "segments": [{"start": 1.0, "end": 4.0, "text": "첫 번째 구간"}],
                    "provider": "openai",
                },
            }]
            with patch.object(ingest, "extract_audio", side_effect=extract), patch.object(
                ingest, "transcribe", side_effect=transcribe
            ), patch.object(ingest, "detect_silences_chunked", return_value=[]), patch.object(
                ingest, "detect_scenes", return_value=[]
            ):
                transcript, silences, scenes = await ingest.inspect_and_transcribe(
                    source,
                    {"duration": 1200.0, "has_audio": True},
                    work_dir=root,
                    existing_chunks=existing,
                    on_progress=lambda row: _capture_progress(progress, row),
                )

            self.assertEqual(extract_starts, [600.0])
            self.assertIn("첫 번째 구간", transcript["text"])
            self.assertIn("두 번째 구간", transcript["text"])
            self.assertEqual((silences, scenes), ([], []))
            completed = [row for row in progress if row.get("checkpoint") == "TRANSCRIPT_CHUNK_COMPLETED"]
            self.assertEqual(completed[-1]["completed_chunks"], 2)
            self.assertEqual(completed[-1]["total_chunks"], 2)

    async def test_resume_keeps_persisted_chunk_boundaries_after_config_change(self):
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"EDIT_TRANSCRIPT_CHUNK_SECONDS": "300"}
        ):
            root = Path(td)
            source = root / "source.mp4"
            source.write_bytes(b"source")
            ingest = MediaIngestService()
            starts = []
            progress = []

            def extract(_source, output, _duration, *, start=0.0):
                starts.append(start)
                output.write_bytes(b"audio")
                return output

            existing = [{
                "chunk_index": 0, "start": 0.0, "end": 1200.0,
                "status": "completed",
                "transcript": {
                    "text": "기존 20분 구간",
                    "segments": [{"start": 1.0, "end": 4.0, "text": "기존 20분 구간"}],
                    "provider": "openai",
                },
            }]
            with patch.object(ingest, "extract_audio", side_effect=extract), patch.object(
                ingest, "transcribe", return_value={
                    "text": "남은 구간", "segments": [{"start": 1, "end": 3, "text": "남은 구간"}],
                    "provider": "openai",
                }
            ), patch.object(ingest, "detect_silences_chunked", return_value=[]), patch.object(
                ingest, "detect_scenes", return_value=[]
            ):
                await ingest.inspect_and_transcribe(
                    source, {"duration": 1726.0, "has_audio": True}, work_dir=root,
                    existing_chunks=existing,
                    on_progress=lambda row: _capture_progress(progress, row),
                )
            self.assertEqual(starts, [1200.0])
            self.assertEqual(progress[0]["total_chunks"], 2)


async def _capture_progress(target, row):
    target.append(dict(row))


class DurableWorkerHeartbeatTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_heartbeat_failure_does_not_kill_liveness(self):
        class FlakyQueue:
            calls = 0

            def heartbeat(self, _job_id):
                self.calls += 1
                if self.calls == 1:
                    raise sqlite3.OperationalError("database is locked")

        queue = FlakyQueue()
        worker = EditJobWorker(queue, {}, heartbeat_seconds=0.01)
        task = asyncio.create_task(worker._heartbeat(7))
        await asyncio.sleep(0.08)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertGreaterEqual(queue.calls, 2)


class LifecycleAndQualityTests(unittest.TestCase):
    def test_additive_lifecycle_migration_and_state_audit(self):
        legacy = migrate_project({"project_uuid": "a" * 32, "status": "completed", "custom": {"keep": True}})
        self.assertEqual(legacy["schema_version"], 3)
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

    @unittest.skipUnless(__import__("shutil").which("ffmpeg"), "ffmpeg required")
    def test_analysis_proxy_is_fast_1080p_bounded_and_atomic(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "source.mp4"
            proxy = Path(td) / "proxy.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=20",
                "-f", "lavfi", "-i", "sine=frequency=440", "-t", "2",
                "-c:v", "libx264", "-c:a", "aac", str(source), "-y",
            ], check=True)
            MediaIngestService.create_analysis_proxy(source, proxy, 2)
            metadata = MediaIngestService.probe(proxy)
            self.assertLessEqual(metadata["width"], 1920)
            self.assertLessEqual(metadata["height"], 1080)
            self.assertFalse(list(Path(td).glob(".*.part.mp4")))


class LargeAnalysisSourceTests(unittest.TestCase):
    def test_large_analysis_source_streams_from_r2_without_tmp_copy(self):
        class Backend:
            def __init__(self):
                self.signed = []

            def presigned_download(self, key, *, expires_seconds):
                self.signed.append((key, expires_seconds))
                return "https://objects.invalid/source.mp4?signed=redacted"

        backend = Backend()
        pipeline = EditPipeline()
        project = {
            "project_uuid": "f" * 32,
            "source": {
                "object_key": "edit/f/source.mp4",
                "size_bytes": 2_205_946_289,
            },
        }
        with patch.dict(os.environ, {"EDIT_ANALYSIS_STREAM_THRESHOLD_MB": "1024"}), patch.object(
            pipeline, "_working_source", side_effect=AssertionError("must not copy large source to /tmp")
        ):
            temporary, source, _seconds, streamed = asyncio.run(
                pipeline._analysis_source_access(project, backend)
            )
        self.assertIsNone(temporary)
        self.assertTrue(streamed)
        self.assertTrue(source.startswith("https://objects.invalid/"))
        self.assertEqual(backend.signed, [("edit/f/source.mp4", 43200)])


@unittest.skipUnless(__import__("shutil").which("ffmpeg") and __import__("shutil").which("ffprobe"), "ffmpeg required")
class ObjectWorkingCopyPipelineTests(unittest.TestCase):
    def test_object_render_downloads_once_validates_uploads_and_cleans_working_copy(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            connect = database(root / "history.db")
            store = EditProjectStore(connect, storage_root=root / "managed")
            source_file = root / "sample.mp4"
            subprocess.run([
                "ffmpeg", "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x120:rate=20",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
                "-t", "3", "-c:v", "libx264", "-c:a", "aac", str(source_file), "-y",
            ], check=True)
            s3 = MultipartS3()
            backend = ObjectStorageBackend(bucket="videos", prefix="ed", client=s3)
            project_uuid = os.urandom(16).hex()
            source_key = backend.key(project_uuid, "source.mp4")
            s3.objects[source_key] = source_file.read_bytes()
            plan = {
                "render_timeline": [{"source_start": 0.0, "source_end": 2.5, "action": "keep", "reason": "test"}],
                "short_timeline": [{"source_start": 0.0, "source_end": 1.5, "action": "short_highlight", "reason": "test"}],
                "estimated_output_duration": 2.5, "estimated_short_duration": 1.5,
                "create_short_highlight": True, "enhancements": [],
            }
            project_id = store.create(keyword="object staged render", project={
                "project_uuid": project_uuid, "status": "approved", "approved_version": 1,
                "source": {
                    "filename": "source.mp4", "size_bytes": source_file.stat().st_size,
                    "storage_backend": "object", "object_key": source_key,
                    "media": {"duration": 3.0, "has_audio": True},
                },
                "plan_versions": [{"version": 1, "status": "approved", "plan": plan}],
            })
            pipeline = EditPipeline(store)
            with patch("edit_pipeline.object_storage_from_env", return_value=backend), patch(
                "edit_pipeline.tempfile.gettempdir", return_value=str(root)
            ):
                result = asyncio.run(pipeline.rendering({"job_id": 999, "project_id": project_id}))
            saved = store.get(project_id)["report"]
            self.assertEqual(saved["status"], "completed")
            self.assertGreater(result["render_seconds"], 0)
            self.assertEqual(set(saved["outputs"]), {"full", "short", "decision"})
            self.assertTrue(all(value["storage_backend"] == "object" for value in saved["outputs"].values()))
            self.assertEqual(saved["quality_assurance"]["full"]["status"], "passed")
            self.assertIn("render_source_download_seconds", saved["timings"])
            self.assertIn("render_encode_seconds", saved["timings"])
            self.assertIn("render_storage_upload_seconds", saved["timings"])
            self.assertEqual(s3.downloads, [source_key])
            self.assertFalse(list(root.glob("edit-work-*")))

            with patch("edit_pipeline.object_storage_from_env", return_value=backend), patch(
                "edit_pipeline.tempfile.gettempdir", return_value=str(root)
            ):
                preview = asyncio.run(pipeline.preview_rendering({
                    "job_id": 1000, "project_id": project_id,
                    "payload": {"profile": "preview_1080p"},
                }))
            saved = store.get(project_id)["report"]
            self.assertGreater(preview["preview_render_seconds"], 0)
            self.assertEqual(saved["preview_state"], "succeeded")
            self.assertEqual(saved["outputs"]["preview"]["render_profile"], "preview_1080p")
            self.assertEqual(saved["status"], "completed")
            self.assertEqual(s3.downloads, [source_key, source_key])
            self.assertFalse(list(root.glob("edit-work-*")))


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

    def test_multipart_complete_queues_a_new_analysis_job(self):
        payload = {
            "client_upload_id": "stable-upload-123", "filename": "long.mp4", "file_size": 16,
            "force_new": True, "create_new_project": True, "reuse_existing": False,
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

    def test_same_filename_in_two_upload_sessions_creates_fresh_projects_and_jobs(self):
        base = {
            "filename": "same-name.mp4", "file_size": 16,
            "original_filename": "same-name.mp4",
            "source_hash": "same-source-hash", "file_hash": "same-file-hash",
            "content_type": "video/mp4", "video_type": "rough_cut",
            "target_format": "long_form", "target_length_seconds": 0,
            "purpose": "브랜드신뢰형", "topic": "새 러프컷", "strategy_id": None,
        }
        patches = (
            patch.object(main, "EditProjectStore", lambda: self.store),
            patch.object(main, "EDIT_JOB_QUEUE", self.queue),
            patch.object(main, "object_storage_configured", return_value=True),
            patch.object(main, "object_storage_from_env", return_value=self.backend),
        )
        project_ids, job_ids = [], []
        upload_ids = []
        with patches[0], patches[1], patches[2], patches[3]:
            for _attempt in range(2):
                started = self.client.post(
                    "/api/edit-uploads/multipart/start",
                    # New-production semantics are the server default, even if
                    # an older client omits both explicit flags.
                    json={**base, "client_upload_id": "same-browser-request-marker"},
                )
                self.assertEqual(started.status_code, 200)
                upload_ids.append(started.json()["upload_id"])
                project_id = int(started.json()["project_id"])
                key = self.store.get(project_id)["report"]["source"]["object_key"]
                self.s3.objects[key] = b"x" * 16
                completed = self.client.post(
                    f"/api/edit-uploads/{project_id}/complete",
                    json={"parts": [{"part_number": 1, "etag": "one"}, {"part_number": 2, "etag": "two"}]},
                )
                self.assertEqual(completed.status_code, 200)
                self.assertEqual(completed.json()["job"]["payload"]["upload_id"], started.json()["upload_id"])
                project_ids.append(project_id)
                job_ids.append(int(completed.json()["job"]["job_id"]))

            self.assertEqual(len(set(project_ids)), 2)
            self.assertEqual(len(set(upload_ids)), 2)
            self.assertEqual(len(set(job_ids)), 2)
            fresh = self.store.get(project_ids[1])["report"]
            self.assertEqual(fresh["source"]["filename"], "same-name.mp4")
            self.assertEqual(fresh["settings"]["rough_cut_mode"], "conservative_rough_cut")
            self.assertFalse(fresh["upload"]["reuse_existing"])
            self.assertEqual(fresh["transcript"], {})
            self.assertEqual(fresh["plan_versions"], [])
            self.assertEqual(fresh["outputs"], {})
            self.assertNotIn("rough_cut_script_editor", fresh)
            self.assertEqual(self.queue.get(job_ids[1])["status"], "queued")
            frontend = (Path(__file__).parents[1] / "static" / "app.js").read_text()
            forbidden = "같은 원본의 기존 프로젝트를 " + "열었습니다."
            self.assertNotIn(forbidden, frontend)

    def test_multisource_project_queues_each_source_independently(self):
        patches = (
            patch.object(main, "EditProjectStore", lambda: self.store),
            patch.object(main, "EDIT_JOB_QUEUE", self.queue),
            patch.object(main, "object_storage_configured", return_value=True),
            patch.object(main, "object_storage_from_env", return_value=self.backend),
        )
        with patches[0], patches[1], patches[2], patches[3]:
            created = self.client.post("/api/edit-projects/multisource", json={
                "topic": "초음파세척기", "purpose": "브랜드신뢰형",
                "target_length_seconds": 480, "strategy_id": None,
            })
            self.assertEqual(created.status_code, 200)
            project_id = created.json()["project"]["id"]
            source_ids = []
            for index in range(2):
                started = self.client.post(
                    f"/api/edit-projects/{project_id}/sources/multipart/start",
                    json={
                        "client_upload_id": f"source-upload-{index}",
                        "filename": f"source-{index}.mp4", "file_size": 16,
                        "content_type": "video/mp4", "speaker": f"speaker-{index}",
                    },
                )
                self.assertEqual(started.status_code, 200)
                source_id = started.json()["source_id"]
                source_ids.append(source_id)
                completed = self.client.post(
                    f"/api/edit-projects/{project_id}/sources/{source_id}/complete",
                    json={"parts": [{"part_number": 1, "etag": "one"}, {"part_number": 2, "etag": "two"}]},
                )
                self.assertEqual(completed.status_code, 200)
            finalized = self.client.post(f"/api/edit-projects/{project_id}/sources/finalize")
            self.assertEqual(finalized.status_code, 200)
            saved = self.store.get(project_id)["report"]
            self.assertEqual(len(saved["sources"]), 2)
            self.assertTrue(saved["uploads_finalized"])
            jobs = self.queue.list(project_id=project_id)
            self.assertEqual({job["payload"]["source_id"] for job in jobs}, set(source_ids))
            self.assertTrue(all(job["type"] == "source_analysis" for job in jobs))

    def test_4k_final_is_deferred_with_worker_payload_and_never_claimed_by_api(self):
        project_uuid = os.urandom(16).hex()
        source_key = self.backend.key(project_uuid, "source.mp4")
        self.s3.objects[source_key] = b"preserved-original"
        plan = {
            "render_timeline": [{"source_start": 0.0, "source_end": 60.0, "action": "keep"}],
            "short_timeline": [], "create_short_highlight": False,
            "estimated_output_duration": 60.0, "enhancements": [],
        }
        project_id = self.store.create(keyword="4k", project={
            "project_uuid": project_uuid, "status": "approved", "approved_version": 1,
            "source": {
                "filename": "source.mp4", "size_bytes": 291 * 1024 * 1024,
                "storage_backend": "object", "object_key": source_key,
                "media": {"width": 3840, "height": 2160, "duration": 300, "has_audio": True},
            },
            "plan_versions": [{"version": 1, "status": "approved", "plan": plan}],
            "outputs": {}, "render_runs": [],
        })
        with patch.object(main, "EditProjectStore", lambda: self.store), patch.object(
            main, "EDIT_JOB_QUEUE", self.queue
        ), patch.object(main, "object_storage_from_env", return_value=self.backend):
            response = self.client.post(f"/api/edit-projects/{project_id}/render")
        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertTrue(body["deferred"])
        self.assertEqual(body["job"]["type"], "final_rendering")
        self.assertEqual(body["job"]["payload"]["job_id"], body["job"]["job_id"])
        self.assertEqual(body["job"]["payload"]["source"]["object_key"], source_key)
        self.assertEqual(self.store.get(project_id)["report"]["final_render_state"], "queued")
        self.assertEqual(self.s3.objects[source_key], b"preserved-original")
        self.assertIsNone(self.queue.claim("api", allowed_types={"analysis", "rendering", "preview_rendering"}))

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
