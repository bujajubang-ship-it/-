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
_UUID_RE = re.compile(r"^[0-9a-f]{32}$")


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
        item["report"] = report if isinstance(report, dict) else {}
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
        payload = dict(project)
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
        payload = dict(project)
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
                }
            )
        return projects

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
    transcript = project.get("transcript") or {}
    text = str(transcript.pop("text", "") or "")
    transcript["preview"] = text[:4000]
    project.pop("evidence_snapshot", None)
    project.pop("strategy_snapshot", None)
    outputs = project.get("outputs") or {}
    for kind, output in outputs.items():
        if isinstance(output, dict):
            output.pop("storage_name", None)
            output["download_url"] = f"/api/edit-projects/{row.get('id')}/outputs/{kind}"
    return project
