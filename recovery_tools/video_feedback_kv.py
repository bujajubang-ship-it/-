"""Inspect or explicitly restore the legacy video-feedback KV copy."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime
from typing import Any

import httpx

import database


CONFIRMATION_PHRASE = "RESTORE_VIDEO_FEEDBACK_FROM_KV"
KV_KEY = "yt_video_feedback"


class VideoFeedbackRecoveryError(RuntimeError):
    pass


def _validated_rows(payload: Any) -> list[dict[str, Any]]:
    rows = (
        ((payload or {}).get("data") or {}).get("rows")
        if isinstance(payload, dict)
        else None
    )
    if not isinstance(rows, list):
        raise VideoFeedbackRecoveryError("KV response does not contain a rows list.")
    validated: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            raise VideoFeedbackRecoveryError(f"KV row {index} is not an object.")
        report = row.get("report")
        if isinstance(report, str):
            try:
                decoded_report = json.loads(report)
            except json.JSONDecodeError as exc:
                raise VideoFeedbackRecoveryError(
                    f"KV row {index} has malformed report JSON."
                ) from exc
            if not isinstance(decoded_report, dict):
                raise VideoFeedbackRecoveryError(
                    f"KV row {index} report JSON must contain an object."
                )
        elif not isinstance(report, dict):
            raise VideoFeedbackRecoveryError(
                f"KV row {index} report must be JSON text or an object."
            )
        created_at = row.get("created_at")
        if created_at:
            try:
                datetime.fromisoformat(str(created_at).strip().replace("Z", "+00:00"))
            except ValueError as exc:
                raise VideoFeedbackRecoveryError(
                    f"KV row {index} has an invalid created_at value."
                ) from exc
        validated.append(row)
    return validated


def _fetch_rows(base: str, secret: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{base.rstrip('/')}/kv/{KV_KEY}",
        headers={"x-secret": secret},
        timeout=15,
    )
    if response.status_code != 200:
        raise VideoFeedbackRecoveryError(
            f"KV request failed with HTTP {response.status_code}."
        )
    return _validated_rows(response.json())


def run(*, apply: bool, confirmation: str = "") -> dict[str, Any]:
    base = os.getenv("CNMAKER_BASE", "").strip()
    secret = os.getenv("CNMAKER_SECRET", "")
    if not base or not secret:
        raise VideoFeedbackRecoveryError(
            "CNMAKER_BASE and CNMAKER_SECRET are required for manual recovery."
        )

    existing = database.list_history("video_feedback", limit=1)
    rows = _fetch_rows(base, secret)
    result = {
        "database": database.DB_PATH,
        "existing_video_feedback": len(existing),
        "kv_rows": len(rows),
        "applied": False,
    }
    if not apply:
        return result
    if confirmation != CONFIRMATION_PHRASE:
        raise VideoFeedbackRecoveryError(
            "Restore confirmation phrase is missing or incorrect."
        )
    if existing:
        raise VideoFeedbackRecoveryError(
            "Restore refused because video_feedback rows already exist."
        )
    result["restored_rows"] = database.restore_video_feedback_rows(rows)
    result["applied"] = True
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or manually restore the legacy video-feedback KV copy."
    )
    parser.add_argument(
        "--apply", action="store_true", help="Actually insert rows into the selected DB."
    )
    parser.add_argument(
        "--confirm", default="", help=f"Required with --apply: {CONFIRMATION_PHRASE}"
    )
    args = parser.parse_args()
    try:
        result = run(apply=args.apply, confirmation=args.confirm)
    except VideoFeedbackRecoveryError as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if not args.apply:
        print("DRY RUN: no database rows were changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
