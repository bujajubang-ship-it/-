"""Capacity planning and safe lifecycle management for edit media.

The SQLite project record is the audit trail. Media deletion never removes that
record and active project states are always protected, including after restart.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from edit_project_store import EditProjectStore, utc_now


MIB = 1024 * 1024
ACTIVE_STATUSES = {
    "uploaded", "transcribing", "retrieving_context", "diagnosing",
    "proposed", "revised", "approved", "rendering",
}
TEMP_NAMES = {"analysis_audio.mp3"}
TEMP_SUFFIXES = {".part", ".tmp", ".partial"}


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default))))
    except (TypeError, ValueError):
        return max(minimum, default)


def _parse_time(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _tree_bytes(directory: Path) -> int:
    total = 0
    if not directory.is_dir():
        return total
    for path in directory.rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            continue
    return total


class StorageBackend(Protocol):
    name: str

    def stats(self) -> dict[str, int]: ...
    def delete_file(self, path: Path) -> int: ...


class LocalStorageBackend:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def stats(self) -> dict[str, int]:
        usage = shutil.disk_usage(self.root)
        return {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free}

    def delete_file(self, path: Path) -> int:
        resolved = path.resolve()
        if resolved.parent != self.root and self.root not in resolved.parents:
            raise ValueError("storage path escaped edit root")
        try:
            size = resolved.stat().st_size if resolved.is_file() else 0
            resolved.unlink(missing_ok=True)
            return size
        except FileNotFoundError:
            return 0


class ObjectStorageBackend:
    """S3-compatible backend used once credentials are configured.

    ffmpeg still renders to a bounded local working directory; completed files
    can then be uploaded and removed locally by a future deployment setting.
    The client is injectable so the contract is fully testable without secrets.
    """

    name = "object"

    def __init__(
        self, *, bucket: str, prefix: str = "edit-projects", endpoint_url: str = "",
        region: str = "auto", client: Any = None,
    ) -> None:
        if not bucket.strip():
            raise ValueError("EDIT_OBJECT_BUCKET is required")
        self.bucket = bucket.strip()
        self.prefix = prefix.strip("/")
        if client is None:
            try:
                import boto3  # type: ignore
            except ImportError as exc:
                raise RuntimeError("Object storage requires the optional boto3 package") from exc
            client = boto3.client(
                "s3", endpoint_url=endpoint_url or None, region_name=region,
                aws_access_key_id=os.getenv("EDIT_OBJECT_ACCESS_KEY_ID") or None,
                aws_secret_access_key=os.getenv("EDIT_OBJECT_SECRET_ACCESS_KEY") or None,
            )
        self.client = client

    def key(self, project_uuid: str, filename: str) -> str:
        safe_name = Path(filename).name
        if safe_name != filename:
            raise ValueError("invalid object filename")
        return "/".join(part for part in (self.prefix, project_uuid, safe_name) if part)

    def upload(self, path: Path, *, project_uuid: str, filename: str) -> str:
        key = self.key(project_uuid, filename)
        self.client.upload_file(str(path), self.bucket, key)
        return key

    def download(self, key: str, destination: Path) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.client.download_file(self.bucket, key, str(destination))
        return destination

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def presigned_download(self, key: str, *, expires_seconds: int = 3600) -> str:
        return self.client.generate_presigned_url(
            "get_object", Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=max(60, min(int(expires_seconds), 86400)),
        )


def object_storage_from_env(*, client: Any = None) -> ObjectStorageBackend | None:
    if os.getenv("EDIT_STORAGE_BACKEND", "local").strip().lower() != "object":
        return None
    return ObjectStorageBackend(
        bucket=os.getenv("EDIT_OBJECT_BUCKET", ""),
        prefix=os.getenv("EDIT_OBJECT_PREFIX", "edit-projects"),
        endpoint_url=os.getenv("EDIT_OBJECT_ENDPOINT", ""),
        region=os.getenv("EDIT_OBJECT_REGION", "auto"),
        client=client,
    )


@dataclass(frozen=True)
class EditStoragePolicy:
    reserve_bytes: int = 128 * MIB
    source_retention_hours: int = 72
    output_retention_hours: int = 24 * 30
    failed_retention_hours: int = 72
    orphan_retention_hours: int = 24
    temp_retention_hours: int = 1
    output_ratio: float = 1.0
    short_ratio: float = 0.20

    @classmethod
    def from_env(cls) -> "EditStoragePolicy":
        return cls(
            reserve_bytes=_env_int("EDIT_DISK_RESERVE_MB", 128, minimum=32) * MIB,
            source_retention_hours=_env_int("EDIT_SOURCE_RETENTION_HOURS", 72, minimum=1),
            output_retention_hours=_env_int("EDIT_OUTPUT_RETENTION_HOURS", 720, minimum=24),
            failed_retention_hours=_env_int("EDIT_FAILED_RETENTION_HOURS", 72, minimum=1),
            orphan_retention_hours=_env_int("EDIT_ORPHAN_RETENTION_HOURS", 24, minimum=1),
            temp_retention_hours=_env_int("EDIT_TEMP_RETENTION_HOURS", 1, minimum=1),
            output_ratio=_env_float("EDIT_OUTPUT_ESTIMATE_RATIO", 1.0, minimum=0.25),
            short_ratio=_env_float("EDIT_SHORT_ESTIMATE_RATIO", 0.20, minimum=0.05),
        )


class EditStorageService:
    def __init__(
        self, store: EditProjectStore | None = None, *, policy: EditStoragePolicy | None = None,
        backend: LocalStorageBackend | None = None,
    ) -> None:
        self.store = store or EditProjectStore()
        self.store.ensure_storage()
        self.policy = policy or EditStoragePolicy.from_env()
        self.backend = backend or LocalStorageBackend(self.store.storage_root)

    def estimate_upload(self, file_size: int, *, target_format: str = "mid_form") -> dict[str, Any]:
        size = max(0, int(file_size))
        full = int(size * self.policy.output_ratio)
        # AI may offer a highlight for every format; short_reel always requires it.
        short_ratio = max(self.policy.short_ratio, 0.25 if target_format == "short_reel" else 0.0)
        short = int(size * short_ratio)
        required = size + full + short + self.policy.reserve_bytes
        stats = self.backend.stats()
        return {
            **stats,
            "file_bytes": size,
            "estimated_full_bytes": full,
            "estimated_short_bytes": short,
            "reserve_bytes": self.policy.reserve_bytes,
            "required_bytes": required,
            "enough": stats["free_bytes"] >= required,
            "shortfall_bytes": max(0, required - stats["free_bytes"]),
        }

    def estimate_render(self, project: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
        source_size = int((project.get("source") or {}).get("size_bytes") or 0)
        full = int(source_size * self.policy.output_ratio)
        short = int(source_size * self.policy.short_ratio) if plan.get("create_short_highlight") else 0
        required = full + short + self.policy.reserve_bytes
        stats = self.backend.stats()
        return {
            **stats, "estimated_full_bytes": full, "estimated_short_bytes": short,
            "reserve_bytes": self.policy.reserve_bytes, "required_bytes": required,
            "enough": stats["free_bytes"] >= required,
            "shortfall_bytes": max(0, required - stats["free_bytes"]),
        }

    @staticmethod
    def _category(path: Path) -> str:
        name = path.name.lower()
        if name.startswith("source."):
            return "sources"
        if name.startswith("edited-") and name.endswith(".mp4"):
            return "full_outputs"
        if name.startswith("short-") and name.endswith(".mp4"):
            return "short_outputs"
        if name.startswith("edit-decision-") and name.endswith(".json"):
            return "decision_files"
        if name in TEMP_NAMES or any(name.endswith(suffix) for suffix in TEMP_SUFFIXES) or (name.startswith(".") and ".part" in name):
            return "temporary"
        if "thumb" in name:
            return "thumbnails"
        if "transcript" in name:
            return "transcripts"
        return "other"

    def snapshot(self) -> dict[str, Any]:
        rows = self.store.all_rows(limit=2000)
        by_uuid = {str(row["report"].get("project_uuid") or ""): row for row in rows}
        categories = {
            key: 0 for key in (
                "sources", "full_outputs", "short_outputs", "decision_files",
                "temporary", "thumbnails", "transcripts", "other",
            )
        }
        projects = []
        seen = set()
        for directory in self.store.storage_root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            seen.add(directory.name)
            usage = 0
            breakdown = {key: 0 for key in categories}
            for path in directory.rglob("*"):
                try:
                    if not path.is_file() or path.is_symlink():
                        continue
                    size = path.stat().st_size
                except FileNotFoundError:
                    continue
                category = self._category(path)
                breakdown[category] += size
                categories[category] += size
                usage += size
            row = by_uuid.get(directory.name)
            report = row["report"] if row else {}
            projects.append({
                "id": row.get("id") if row else None,
                "filename": (report.get("source") or {}).get("filename") or "orphan",
                "status": report.get("status") or "orphan",
                "bytes": usage,
                "breakdown": breakdown,
                "protected": bool(report.get("status") in ACTIVE_STATUSES),
                "updated_at": report.get("updated_at") or row.get("created_at") if row else None,
            })
        for project_uuid, row in by_uuid.items():
            if project_uuid and project_uuid not in seen:
                projects.append({
                    "id": row.get("id"), "filename": (row["report"].get("source") or {}).get("filename") or "-",
                    "status": row["report"].get("status"), "bytes": 0,
                    "breakdown": {key: 0 for key in categories},
                    "protected": bool(row["report"].get("status") in ACTIVE_STATUSES),
                    "updated_at": row["report"].get("updated_at") or row.get("created_at"),
                })
        projects.sort(key=lambda item: item["bytes"], reverse=True)
        root = self.backend.stats()
        temp_usage = shutil.disk_usage("/tmp") if Path("/tmp").exists() else None
        return {
            "backend": self.backend.name,
            "configured_backend": os.getenv("EDIT_STORAGE_BACKEND", "local").strip().lower(),
            "object_storage_ready": bool(
                os.getenv("EDIT_OBJECT_BUCKET", "").strip()
                and os.getenv("EDIT_OBJECT_ENDPOINT", "").strip()
                and os.getenv("EDIT_OBJECT_ACCESS_KEY_ID", "").strip()
                and os.getenv("EDIT_OBJECT_SECRET_ACCESS_KEY", "").strip()
            ),
            "root": str(self.store.storage_root),
            **root,
            "managed_bytes": sum(categories.values()),
            "categories": categories,
            "projects": projects,
            "tmp_filesystem": (
                {"total_bytes": temp_usage.total, "used_bytes": temp_usage.used, "free_bytes": temp_usage.free}
                if temp_usage else None
            ),
            "policy": {
                "reserve_bytes": self.policy.reserve_bytes,
                "source_retention_hours": self.policy.source_retention_hours,
                "output_retention_hours": self.policy.output_retention_hours,
                "failed_retention_hours": self.policy.failed_retention_hours,
                "orphan_retention_hours": self.policy.orphan_retention_hours,
            },
            "collected_at": utc_now(),
        }

    def _delete(self, path: Path, *, dry_run: bool) -> int:
        try:
            size = path.stat().st_size if path.is_file() else 0
        except FileNotFoundError:
            return 0
        if not dry_run:
            self.backend.delete_file(path)
        return size

    def cleanup(
        self, *, now: datetime | None = None, dry_run: bool = False,
        in_memory_active: set[int] | None = None,
    ) -> dict[str, Any]:
        current = now or datetime.now(timezone.utc)
        active_ids = set(in_memory_active or set())
        rows = self.store.all_rows(limit=2000)
        known = {str(row["report"].get("project_uuid") or ""): row for row in rows}
        deleted_bytes = 0
        deleted_files = 0
        skipped_active = []
        updated_projects = []

        for project_uuid, row in known.items():
            if not project_uuid:
                continue
            project_id = int(row["id"])
            project = row["report"]
            status = str(project.get("status") or "")
            directory = self.store.project_dir(project_uuid)
            if status in ACTIVE_STATUSES or project_id in active_ids:
                skipped_active.append(project_id)
                continue
            changed = False
            lifecycle_time = (project.get("storage_state") or {}).get("render_completed_at")
            updated = _parse_time(lifecycle_time or project.get("updated_at") or row.get("created_at")) or current
            age = current - updated
            referenced = {
                str((project.get("source") or {}).get("storage_name") or ""),
                *[
                    str(value.get("storage_name") or "")
                    for value in (project.get("outputs") or {}).values()
                    if isinstance(value, dict)
                ],
            }
            if directory.is_dir():
                for path in list(directory.iterdir()):
                    if not path.is_file() or path.is_symlink():
                        continue
                    name = path.name
                    category = self._category(path)
                    file_age = current - datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
                    should_delete = category == "temporary" and file_age >= timedelta(hours=self.policy.temp_retention_hours)
                    should_delete = should_delete or (
                        name not in referenced
                        and category in {"full_outputs", "short_outputs", "decision_files"}
                        and file_age >= timedelta(hours=self.policy.orphan_retention_hours)
                    )
                    if should_delete:
                        deleted_bytes += self._delete(path, dry_run=dry_run)
                        deleted_files += 1

                source_name = str((project.get("source") or {}).get("storage_name") or "")
                source_limit = self.policy.failed_retention_hours if "failed" in status else self.policy.source_retention_hours
                if source_name and age >= timedelta(hours=source_limit):
                    source_path = directory / source_name
                    if source_path.is_file():
                        deleted_bytes += self._delete(source_path, dry_run=dry_run)
                        deleted_files += 1
                        if not dry_run:
                            project.setdefault("storage_state", {})["source_deleted_at"] = utc_now()
                            changed = True

                if age >= timedelta(hours=self.policy.output_retention_hours):
                    for kind, value in list((project.get("outputs") or {}).items()):
                        name = str(value.get("storage_name") or "") if isinstance(value, dict) else ""
                        path = directory / name if name else None
                        if path and path.is_file():
                            deleted_bytes += self._delete(path, dry_run=dry_run)
                            deleted_files += 1
                        if not dry_run:
                            project.get("outputs", {}).pop(kind, None)
                            changed = True
                    if changed and not dry_run:
                        project.setdefault("storage_state", {})["outputs_deleted_at"] = utc_now()

            if changed and not dry_run:
                self.store.save(project_id, project)
                updated_projects.append(project_id)

        for directory in list(self.store.storage_root.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or directory.name in known:
                continue
            modified = datetime.fromtimestamp(directory.stat().st_mtime, tz=timezone.utc)
            if current - modified < timedelta(hours=self.policy.orphan_retention_hours):
                continue
            size = _tree_bytes(directory)
            if not dry_run:
                shutil.rmtree(directory)
            deleted_bytes += size
            deleted_files += 1

        return {
            "dry_run": dry_run, "deleted_bytes": deleted_bytes,
            "deleted_files": deleted_files, "updated_projects": updated_projects,
            "skipped_active": sorted(set(skipped_active)), "completed_at": utc_now(),
        }

    def delete_project_files(
        self, project_id: int, *, scope: str = "all", in_memory_active: set[int] | None = None,
    ) -> dict[str, Any]:
        if scope not in {"source", "outputs", "all"}:
            raise ValueError("invalid cleanup scope")
        row = self.store.get(project_id)
        if not row:
            raise KeyError("편집 프로젝트를 찾지 못했습니다.")
        project = row["report"]
        if project.get("status") in ACTIVE_STATUSES or project_id in set(in_memory_active or set()):
            raise RuntimeError("작업 중인 프로젝트 파일은 삭제할 수 없습니다.")
        directory = self.store.project_dir(str(project.get("project_uuid") or ""))
        deleted = 0
        if scope in {"source", "all"}:
            name = str((project.get("source") or {}).get("storage_name") or "")
            if name:
                deleted += self._delete(directory / name, dry_run=False)
                project.setdefault("storage_state", {})["source_deleted_at"] = utc_now()
        if scope in {"outputs", "all"}:
            for value in (project.get("outputs") or {}).values():
                name = str(value.get("storage_name") or "") if isinstance(value, dict) else ""
                if name:
                    deleted += self._delete(directory / name, dry_run=False)
            project["outputs"] = {}
            project.setdefault("storage_state", {})["outputs_deleted_at"] = utc_now()
        project.setdefault("storage_state", {})["last_manual_cleanup_at"] = utc_now()
        self.store.save(project_id, project)
        return {"ok": True, "project_id": project_id, "scope": scope, "deleted_bytes": deleted}
