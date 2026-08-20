"""Persistent, history-backed storage for collaborative edit projects.

The production database is deliberately not migrated for this feature.  Each
project is an additive ``history`` row and media files live below one isolated
directory on the existing persistent disk.  Existing history, pipeline,
worksheet, knowledge, and chat rows are never rewritten.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from database import get_db


PROJECT_TYPE = "edit_project"
CURRENT_PROJECT_SCHEMA = 3
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")

LIFECYCLE_BY_STATUS = {
    "uploading": "UPLOADING", "upload_failed": "FAILED_UPLOAD",
    "uploaded": "UPLOADED", "transcribing": "ANALYZING",
    "retrieving_context": "ANALYZING", "diagnosing": "ANALYZING",
    "analysis_failed": "FAILED_ANALYSIS", "proposed": "AWAITING_REVIEW",
    "revised": "AWAITING_REVIEW", "approved": "APPROVED",
    "queued": "QUEUED", "rendering": "RENDERING",
    "final_queued": "QUEUED", "final_rendering": "RENDERING",
    "render_failed": "FAILED_RENDER", "completed": "COMPLETED",
    "published_or_downloaded": "PUBLISHED_OR_DOWNLOADED",
    "media_purged": "MEDIA_PURGED",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def default_storage_root() -> Path:
    configured = os.getenv("EDIT_PROJECT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if os.getenv("RENDER", "").strip().lower() not in {"", "0", "false", "no", "off"}:
        return Path("/data/edit_projects")
    return (Path(__file__).resolve().parent / "storage" / "edit_projects").resolve()


class EditProjectStore:
    def __init__(
        self,
        connect: Callable[[], sqlite3.Connection] | None = None,
        *,
        storage_root: str | Path | None = None,
    ) -> None:
        self._connect = connect or get_db
        self.storage_root = Path(storage_root or default_storage_root()).resolve()

    @staticmethod
    def _decode(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        item = dict(row)
        report = item.get("report")
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except json.JSONDecodeError:
                report = {}
        item["report"] = migrate_project(report if isinstance(report, dict) else {})
        return item

    def ensure_storage(self) -> None:
        self.storage_root.mkdir(parents=True, exist_ok=True)
        if not self.storage_root.is_dir():
            raise RuntimeError("편집 프로젝트 저장소를 준비하지 못했습니다.")

    def project_dir(self, project_uuid: str, *, create: bool = False) -> Path:
        if not _UUID_RE.fullmatch(project_uuid):
            raise ValueError("invalid edit project identifier")
        self.ensure_storage()
        directory = (self.storage_root / project_uuid).resolve()
        if directory.parent != self.storage_root:
            raise ValueError("edit project path escaped storage root")
        if create:
            directory.mkdir(parents=False, exist_ok=True)
        return directory

    def create(self, *, keyword: str, project: dict[str, Any]) -> int:
        now = utc_now()
        payload = migrate_project(dict(project))
        payload.setdefault("created_at", now)
        payload["updated_at"] = now
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO history (type, keyword, report) VALUES (?,?,?)",
                (
                    PROJECT_TYPE,
                    (keyword or "AI 편집 프로젝트").strip()[:300],
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def get(self, project_id: int) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM history WHERE id=? AND type=?",
                (project_id, PROJECT_TYPE),
            ).fetchone()
        return self._decode(row) if row else None

    def save(self, project_id: int, project: dict[str, Any]) -> bool:
        payload = migrate_project(dict(project))
        payload["updated_at"] = utc_now()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE history SET report=? WHERE id=? AND type=?",
                (
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    project_id,
                    PROJECT_TYPE,
                ),
            )
            connection.commit()
            return cursor.rowcount == 1

    def list(self, *, limit: int = 30) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 100))
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT id,type,keyword,report,created_at
                FROM history WHERE type=? ORDER BY id DESC LIMIT ?
                """,
                (PROJECT_TYPE, safe_limit),
            ).fetchall()
        projects = []
        for row in rows:
            item = self._decode(row)
            report = item["report"]
            source = report.get("source") or {}
            projects.append(
                {
                    "id": item["id"],
                    "keyword": item.get("keyword"),
                    "created_at": item.get("created_at"),
                    "updated_at": report.get("updated_at"),
                    "status": report.get("status"),
                    "filename": source.get("filename"),
                    "video_type": (report.get("settings") or {}).get("video_type"),
                    "target_format": (report.get("settings") or {}).get("target_format"),
                    "duration": (source.get("media") or {}).get("duration"),
                    "version": len(report.get("plan_versions") or []),
                    "source_count": len(report.get("sources") or []) or (1 if source else 0),
                }
            )
        return projects

    def linked_projects(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return linked projects for the periodic measured-feedback pass."""

        safe_limit = max(1, min(int(limit), 500))
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT * FROM history WHERE type=? ORDER BY id DESC LIMIT ?
                """,
                (PROJECT_TYPE, safe_limit),
            ).fetchall()
        output = []
        for row in rows:
            item = self._decode(row)
            if str((item["report"].get("upload_feedback") or {}).get("video_id") or "").strip():
                output.append(item)
        return output

    def all_rows(self, *, limit: int = 500) -> list[dict[str, Any]]:
        """Return full edit-project rows for storage accounting and lifecycle work."""

        safe_limit = max(1, min(int(limit), 2000))
        with closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                "SELECT * FROM history WHERE type=? ORDER BY id DESC LIMIT ?",
                (PROJECT_TYPE, safe_limit),
            ).fetchall()
        return [self._decode(row) for row in rows]

    def find_by_client_upload_id(self, client_upload_id: str) -> dict[str, Any] | None:
        marker = str(client_upload_id or "").strip()
        if not marker:
            return None
        for row in self.all_rows(limit=2000):
            if str((row["report"].get("upload") or {}).get("client_upload_id") or "") == marker:
                return row
        return None

    def resolve_media_path(self, project: dict[str, Any], key: str) -> Path:
        project_uuid = str(project.get("project_uuid") or "")
        directory = self.project_dir(project_uuid)
        if key == "source":
            raw = ((project.get("source") or {}).get("storage_name") or "")
        else:
            raw = (((project.get("outputs") or {}).get(key) or {}).get("storage_name") or "")
        if not raw or Path(raw).name != raw:
            raise FileNotFoundError("편집 파일을 찾지 못했습니다.")
        path = (directory / raw).resolve()
        if path.parent != directory or not path.is_file():
            raise FileNotFoundError("편집 파일을 찾지 못했습니다.")
        return path


def public_project(row: dict[str, Any]) -> dict[str, Any]:
    """Remove server paths while keeping the complete decision audit trail."""

    project = json.loads(json.dumps(row.get("report") or {}, ensure_ascii=False))
    project["id"] = row.get("id")
    source = project.get("source") or {}
    source.pop("storage_name", None)
    for item in project.get("sources") or []:
        if isinstance(item, dict):
            item.pop("storage_key", None)
            upload = item.get("upload") or {}
            upload.pop("multipart_upload_id", None)
            upload.pop("object_key", None)
            source_transcript = item.get("transcript") or {}
            source_text = str(source_transcript.pop("text", "") or "")
            source_transcript["preview"] = source_text[:1200]
            for chunk in item.get("transcript_chunks") or []:
                if not isinstance(chunk, dict):
                    continue
                chunk_transcript = chunk.get("transcript") or {}
                chunk_transcript.pop("text", None)
                chunk_transcript.pop("segments", None)
                chunk.pop("segments", None)
    transcript = project.get("transcript") or {}
    text = str(transcript.pop("text", "") or "")
    transcript["preview"] = text[:4000]
    project.pop("evidence_snapshot", None)
    project.pop("strategy_snapshot", None)
    upload = project.get("upload") or {}
    upload.pop("multipart_upload_id", None)
    upload.pop("object_key", None)
    outputs = project.get("outputs") or {}
    for kind, output in outputs.items():
        if isinstance(output, dict):
            output.pop("storage_name", None)
            output["download_url"] = f"/api/edit-projects/{row.get('id')}/outputs/{kind}"
    return project


def migrate_project(project: dict[str, Any]) -> dict[str, Any]:
    """Add lifecycle/audit fields without rewriting or deleting legacy data."""

    payload = dict(project or {})
    status = str(payload.get("status") or "uploaded")
    payload["schema_version"] = max(int(payload.get("schema_version") or 1), CURRENT_PROJECT_SCHEMA)
    payload.setdefault("lifecycle_status", LIFECYCLE_BY_STATUS.get(status, status.upper()))
    payload.setdefault("state_history", [])
    payload.setdefault("jobs", [])
    payload.setdefault("media_state", "available")
    payload.setdefault("quality_assurance", {})
    payload.setdefault("visual_analysis", {"status": "not_run", "fallback_used": False})
    payload.setdefault("preview_state", "not_requested")
    payload.setdefault("final_render_state", "not_requested")
    payload.setdefault("observability", {})
    # Multi-source rough-cut fields are purely additive.  Legacy single-source
    # projects keep their existing shape and are lazily exposed as one source
    # only when the new workflow is opened.
    payload.setdefault("sources", [])
    payload.setdefault("uploads_finalized", False)
    payload.setdefault("duplicate_groups", [])
    payload.setdefault("story_plan_state", "not_requested")
    return payload


def transition_project(
    project: dict[str, Any], status: str, *, lifecycle: str | None = None,
    reason: str = "", job_id: int | None = None,
) -> dict[str, Any]:
    payload = migrate_project(project)
    previous = str(payload.get("status") or "")
    next_lifecycle = lifecycle or LIFECYCLE_BY_STATUS.get(status, status.upper())
    payload["status"] = status
    payload["lifecycle_status"] = next_lifecycle
    event = {
        "from": previous, "to": status, "lifecycle": next_lifecycle,
        "at": utc_now(), "reason": str(reason or "")[:500], "job_id": job_id,
    }
    history = list(payload.get("state_history") or [])
    if not history or (history[-1].get("to"), history[-1].get("job_id")) != (status, job_id):
        history.append(event)
    payload["state_history"] = history[-200:]
    return payload
