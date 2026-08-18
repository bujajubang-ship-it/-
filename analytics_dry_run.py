"""Explicit, read-only live validation for YouTube Analytics API.

This command never writes SQLite data and never creates Reporting API jobs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import date, timedelta
from typing import Any, Dict

from analytics_service import ANALYTICS_METRICS, AnalyticsService


CONFIRMATION = "READ_ONLY_YOUTUBE_ANALYTICS"


def mask_video_id(video_id: str) -> str:
    if len(video_id) <= 4:
        return "*" * len(video_id)
    return f"{video_id[:2]}{'*' * (len(video_id) - 4)}{video_id[-2:]}"


def presence_summary(row: Dict[str, Any]) -> Dict[str, Any]:
    metrics = row.get("metrics") or {}
    return {
        "video_id": mask_video_id(str(row.get("video_id") or "")),
        "data_through": row.get("data_through"),
        "sample_size": row.get("sample_size", 0),
        "metrics": {
            name: {
                "status": (metrics.get(name) or {}).get("status", "unavailable"),
                "value_present": (metrics.get(name) or {}).get("value") is not None,
            }
            for name in ANALYTICS_METRICS
        },
        "thumbnail_impressions": {
            "status": "unavailable",
            "reason": "requires_channel_reach_basic_a1_report",
        },
        "thumbnail_ctr": {
            "status": "unavailable",
            "reason": "requires_channel_reach_basic_a1_report",
        },
    }


async def run(args: argparse.Namespace) -> int:
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Pass --confirm {CONFIRMATION} to execute the read-only query")
    service = AnalyticsService()
    if not service.is_configured():
        raise SystemExit("OAuth environment is not configured")
    try:
        rows = await service.get_video_analytics(
            args.start_date,
            end_date=args.end_date,
            video_ids=args.video_id,
        )
        output: Dict[str, Any] = {
            "mode": "read_only",
            "database_writes": False,
            "reporting_job_changes": False,
            "row_count": len(rows),
            "videos": [presence_summary(row) for row in rows],
        }
        if args.retention:
            retention = []
            for video_id in args.video_id:
                result = await service.get_video_retention(
                    video_id, start_date=args.start_date, end_date=args.end_date
                )
                retention.append(
                    {
                        "video_id": mask_video_id(video_id),
                        "status": result["status"],
                        "row_count": len(result.get("points") or []),
                        "data_through": result.get("data_through"),
                    }
                )
            output["retention"] = retention
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    finally:
        await service.close()


def main() -> int:
    today = date.today()
    parser = argparse.ArgumentParser(
        description="Read-only YouTube Analytics validation; prints no tokens"
    )
    parser.add_argument("--video-id", action="append", required=True)
    parser.add_argument(
        "--start-date", default=(today - timedelta(days=30)).isoformat()
    )
    parser.add_argument("--end-date", default=today.isoformat())
    parser.add_argument("--retention", action="store_true")
    parser.add_argument("--confirm", required=True)
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
