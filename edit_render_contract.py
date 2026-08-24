"""Stable contract between the API service and a long-running render worker.

Only object keys and immutable edit data cross this boundary. Credentials and
presigned URLs are deliberately excluded so a queued job can survive expiry,
deploys, and worker restarts.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from typing import Any


MIB = 1024 * 1024


@dataclass(frozen=True)
class RenderProfile:
    name: str
    max_width: int | None
    max_height: int | None
    preset: str
    crf: int
    audio_bitrate: str
    timeout_seconds: int

    def public(self) -> dict[str, Any]:
        return asdict(self)


RENDER_PROFILES = {
    "preview_720p": RenderProfile(
        name="preview_720p", max_width=1280, max_height=720,
        preset="ultrafast", crf=30, audio_bitrate="96k", timeout_seconds=45 * 60,
    ),
    "preview_1080p": RenderProfile(
        name="preview_1080p", max_width=1920, max_height=1080,
        preset="veryfast", crf=26, audio_bitrate="128k", timeout_seconds=45 * 60,
    ),
    "final_original": RenderProfile(
        name="final_original", max_width=None, max_height=None,
        preset="veryfast", crf=22, audio_bitrate="160k", timeout_seconds=24 * 60 * 60,
    ),
}


def render_profile(name: str | None) -> RenderProfile:
    value = str(name or "final_original")
    if value not in RENDER_PROFILES:
        raise ValueError("unsupported render profile")
    return RENDER_PROFILES[value]


def requires_external_final(project: dict[str, Any]) -> bool:
    """Keep bounded HD clips compatible while moving heavy finals off the API."""

    source = project.get("source") or {}
    media = source.get("media") or {}
    width = int(media.get("width") or 0)
    height = int(media.get("height") or 0)
    duration = float(media.get("duration") or 0)
    size = int(source.get("size_bytes") or 0)
    max_pixels_width = int(os.getenv("EDIT_EMBEDDED_FINAL_MAX_WIDTH", "1920"))
    max_pixels_height = int(os.getenv("EDIT_EMBEDDED_FINAL_MAX_HEIGHT", "1080"))
    max_size = int(os.getenv("EDIT_EMBEDDED_FINAL_MAX_MIB", "200")) * MIB
    max_duration = int(os.getenv("EDIT_EMBEDDED_FINAL_MAX_SECONDS", "900"))
    return (
        width > max_pixels_width
        or height > max_pixels_height
        or size > max_size
        or duration > max_duration
    )


def plan_digest(plan: dict[str, Any]) -> str:
    encoded = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_final_render_payload(
    project: dict[str, Any], *, approved_version: int, plan: dict[str, Any], backend: Any,
) -> dict[str, Any]:
    source = project.get("source") or {}
    object_key = str(source.get("object_key") or "")
    if source.get("storage_backend") != "object" or not object_key:
        raise ValueError("external final render requires an object-storage source")
    project_uuid = str(project.get("project_uuid") or "")
    request_id = uuid.uuid4().hex
    names = {"full": f"edited-v{approved_version}.mp4", "decision": f"edit-decision-v{approved_version}.json"}
    if plan.get("create_short_highlight") and plan.get("short_timeline"):
        names["short"] = f"short-v{approved_version}.mp4"
    targets = {
        kind: {
            "filename": filename,
            "staging_key": backend.key(project_uuid, f".staging-{request_id}-{filename}"),
            "final_key": backend.key(project_uuid, filename),
            "content_type": "application/json" if kind == "decision" else "video/mp4",
        }
        for kind, filename in names.items()
    }
    return {
        "schema_version": 1,
        "execution_target": "external_render_worker",
        "render_kind": "final",
        "request_id": request_id,
        "project_uuid": project_uuid,
        "approved_version": int(approved_version),
        "approved_plan_sha256": plan_digest(plan),
        "source": {
            "storage_backend": "object",
            "object_key": object_key,
            "size_bytes": int(source.get("size_bytes") or 0),
            "etag": source.get("etag"),
            "filename": source.get("filename"),
            "media": source.get("media") or {},
        },
        "edl": {
            "render_timeline": plan.get("render_timeline") or [],
            "short_timeline": plan.get("short_timeline") or [],
            "create_short_highlight": bool(plan.get("create_short_highlight")),
            "estimated_output_duration": plan.get("estimated_output_duration"),
            "estimated_short_duration": plan.get("estimated_short_duration"),
            "enhancements": plan.get("enhancements") or [],
        },
        "render_profile": render_profile("final_original").public(),
        "output_target": {"storage_backend": "object", "objects": targets},
    }


def validate_final_payload(payload: dict[str, Any]) -> None:
    if int(payload.get("schema_version") or 0) != 1:
        raise ValueError("unsupported final render payload")
    if payload.get("execution_target") != "external_render_worker":
        raise ValueError("final render execution target is invalid")
    if not ((payload.get("source") or {}).get("object_key")):
        raise ValueError("final render source is missing")
    objects = ((payload.get("output_target") or {}).get("objects") or {})
    if not {"full", "decision"}.issubset(objects):
        raise ValueError("final render output targets are incomplete")
