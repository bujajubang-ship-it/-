"""YouTube Reporting API Reach parsing and report retrieval.

The Reach report is the authoritative source for thumbnail impressions and CTR.
Job creation is idempotent: a Reach job is created only when none exists.
"""

from __future__ import annotations

import csv
import hashlib
import io
import math
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Sequence

import httpx

from analytics_service import (
    STATUS_AVAILABLE,
    STATUS_NOT_REPORTED,
    AnalyticsApiError,
    metric_value,
)


REPORTING_API_BASE = "https://youtubereporting.googleapis.com/v1"
REACH_REPORT_TYPE_ID = "channel_reach_basic_a1"
REACH_SOURCE = f"youtube_reporting_api:{REACH_REPORT_TYPE_ID}"
REACH_DIMENSIONS = ("date", "channel_id", "video_id")
REACH_METRICS = (
    "video_thumbnail_impressions",
    "video_thumbnail_impressions_ctr",
)
CTR_UNIT = "percent"


def _safe_google_error(response: httpx.Response) -> str:
    """Return only Google machine-readable error codes, never response messages."""

    codes: list[str] = []
    try:
        error = response.json().get("error") or {}
        if isinstance(error.get("status"), str):
            codes.append(error["status"])
        for detail in error.get("details") or []:
            if isinstance(detail, dict) and isinstance(detail.get("reason"), str):
                codes.append(detail["reason"])
    except (AttributeError, TypeError, ValueError):
        pass
    unique = list(dict.fromkeys(code for code in codes if code))
    return f" ({', '.join(unique[:3])})" if unique else ""


def _parse_nonnegative_int(raw: str, *, column: str) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer in {column}") from exc
    if value < 0:
        raise ValueError(f"Negative value in {column}")
    return value


def _parse_percent(raw: str, *, column: str) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid percentage in {column}") from exc
    if not math.isfinite(value) or not 0 <= value <= 100:
        raise ValueError(f"Out-of-range percentage in {column}")
    return value


def parse_reach_csv(
    csv_text: str,
    *,
    expected_video_ids: Sequence[str] | None = None,
    expected_date: str | None = None,
    report_id: str | None = None,
    report_generated_at: str | None = None,
    source_as_of: str | None = None,
) -> List[Dict[str, Any]]:
    """Parse channel_reach_basic_a1 CSV and make omitted rows explicit.

    Reporting API CTR is documented as a percentage, so values are preserved in
    percentage points. Missing report rows are `not_reported`, never numeric zero.
    """

    reader = csv.DictReader(io.StringIO(csv_text))
    headers = set(reader.fieldnames or [])
    required = set(REACH_DIMENSIONS + REACH_METRICS)
    missing_headers = sorted(required - headers)
    if missing_headers:
        raise ValueError("Reach CSV is missing columns: " + ", ".join(missing_headers))

    parsed: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in reader:
        metric_date = (row.get("date") or "").strip()
        video_id = (row.get("video_id") or "").strip()
        channel_id = (row.get("channel_id") or "").strip()
        if not metric_date or not video_id:
            raise ValueError("Reach CSV row is missing date or video_id")
        key = (video_id, metric_date)
        if key in seen:
            raise ValueError("Reach CSV contains a duplicate video/date row")
        seen.add(key)
        impressions = _parse_nonnegative_int(
            row.get("video_thumbnail_impressions", ""),
            column="video_thumbnail_impressions",
        )
        ctr_percent = _parse_percent(
            row.get("video_thumbnail_impressions_ctr", ""),
            column="video_thumbnail_impressions_ctr",
        )
        parsed.append(
            {
                "video_id": video_id,
                "channel_id": channel_id,
                "metric_date": metric_date,
                "thumbnail_impressions": metric_value(
                    impressions, STATUS_AVAILABLE, source=REACH_SOURCE, data_through=metric_date
                ),
                "thumbnail_ctr": metric_value(
                    ctr_percent, STATUS_AVAILABLE, source=REACH_SOURCE, data_through=metric_date
                ),
                "ctr_unit": CTR_UNIT,
                "source": REACH_SOURCE,
                "report_id": report_id,
                "report_generated_at": report_generated_at,
                "source_as_of": source_as_of or metric_date,
                "data_through": metric_date,
                "row_status": STATUS_AVAILABLE,
            }
        )

    if expected_video_ids and expected_date:
        for video_id in dict.fromkeys(expected_video_ids):
            if (video_id, expected_date) in seen:
                continue
            parsed.append(
                {
                    "video_id": video_id,
                    "channel_id": None,
                    "metric_date": expected_date,
                    "thumbnail_impressions": metric_value(
                        None,
                        STATUS_NOT_REPORTED,
                        source=REACH_SOURCE,
                        data_through=expected_date,
                    ),
                    "thumbnail_ctr": metric_value(
                        None,
                        STATUS_NOT_REPORTED,
                        source=REACH_SOURCE,
                        data_through=expected_date,
                    ),
                    "ctr_unit": CTR_UNIT,
                    "source": REACH_SOURCE,
                    "report_id": report_id,
                    "report_generated_at": report_generated_at,
                    "source_as_of": source_as_of or expected_date,
                    "data_through": expected_date,
                    "row_status": STATUS_NOT_REPORTED,
                }
            )
    return parsed


def weighted_ctr(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Aggregate official CTR percentages using impressions as weights."""

    numerator = 0.0
    denominator = 0
    samples = 0
    for row in rows:
        impressions_metric = row.get("thumbnail_impressions") or {}
        ctr_metric = row.get("thumbnail_ctr") or {}
        if (
            impressions_metric.get("status") != STATUS_AVAILABLE
            or ctr_metric.get("status") != STATUS_AVAILABLE
        ):
            continue
        impressions = int(impressions_metric["value"])
        ctr_percent = float(ctr_metric["value"])
        numerator += impressions * ctr_percent
        denominator += impressions
        samples += 1
    if denominator <= 0:
        return {
            "value": None,
            "status": STATUS_NOT_REPORTED,
            "unit": CTR_UNIT,
            "sample_size": samples,
            "total_impressions": denominator,
            "formula": "sum(impressions * ctr_percent) / sum(impressions)",
        }
    return {
        "value": numerator / denominator,
        "status": STATUS_AVAILABLE,
        "unit": CTR_UNIT,
        "sample_size": samples,
        "total_impressions": denominator,
        "formula": "sum(impressions * ctr_percent) / sum(impressions)",
    }


def aggregate_reach_by_video(rows: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("video_id") or "")].append(row)
    result: Dict[str, Dict[str, Any]] = {}
    for video_id, video_rows in grouped.items():
        available_impressions = [
            int(row["thumbnail_impressions"]["value"])
            for row in video_rows
            if row.get("thumbnail_impressions", {}).get("status") == STATUS_AVAILABLE
        ]
        result[video_id] = {
            "thumbnail_impressions": (
                sum(available_impressions) if available_impressions else None
            ),
            "thumbnail_impressions_status": (
                STATUS_AVAILABLE if available_impressions else STATUS_NOT_REPORTED
            ),
            "thumbnail_ctr": weighted_ctr(video_rows),
            "period_start": min(row["metric_date"] for row in video_rows),
            "period_end": max(row["metric_date"] for row in video_rows),
            "source_as_of": max(
                (
                    str(row.get("source_as_of") or row.get("data_through") or row["metric_date"])
                    for row in video_rows
                ),
                default=None,
            ),
            "report_generated_at": max(
                (str(row.get("report_generated_at")) for row in video_rows if row.get("report_generated_at")),
                default=None,
            ),
            "collected_at": max(
                (str(row.get("collected_at")) for row in video_rows if row.get("collected_at")),
                default=None,
            ),
            "source": REACH_SOURCE,
        }
    return result


class YouTubeReportingService:
    """Reporting API client. It never deletes jobs or reports."""

    def __init__(
        self,
        token_provider: Callable[[], Awaitable[str]],
        *,
        http: httpx.AsyncClient | None = None,
    ):
        self._token_provider = token_provider
        self.http = http or httpx.AsyncClient(timeout=60.0)
        self._owns_http = http is None

    async def _get_json(self, url: str, params: Dict[str, Any] | None = None) -> Dict[str, Any]:
        token = await self._token_provider()
        try:
            response = await self.http.get(
                url,
                params=params or {},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise AnalyticsApiError(
                "YouTube Reporting request failed with HTTP "
                f"{exc.response.status_code}{_safe_google_error(exc.response)}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AnalyticsApiError("YouTube Reporting request failed") from exc
        if not isinstance(payload, dict):
            raise AnalyticsApiError("YouTube Reporting returned an invalid response")
        return payload

    async def list_jobs(self) -> List[Dict[str, Any]]:
        jobs = await self._list_all(f"{REPORTING_API_BASE}/jobs", "jobs")
        return [
            job
            for job in jobs
            if job.get("reportTypeId") == REACH_REPORT_TYPE_ID
        ]

    async def ensure_reach_job(self, *, name: str = "bujajubang-reach") -> Dict[str, Any]:
        """Return the existing Reach job or create exactly one when absent."""

        jobs = await self.list_jobs()
        if jobs:
            return jobs[0]
        token = await self._token_provider()
        try:
            response = await self.http.post(
                f"{REPORTING_API_BASE}/jobs",
                json={"reportTypeId": REACH_REPORT_TYPE_ID, "name": name},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise AnalyticsApiError(
                "YouTube Reporting job creation failed with HTTP "
                f"{exc.response.status_code}{_safe_google_error(exc.response)}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise AnalyticsApiError("YouTube Reporting job creation failed") from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            raise AnalyticsApiError("YouTube Reporting job creation returned invalid data")
        return payload

    async def list_reports(
        self, job_id: str, *, created_after: str | None = None
    ) -> List[Dict[str, Any]]:
        params: Dict[str, Any] = {"createdAfter": created_after} if created_after else {}
        return await self._list_all(
            f"{REPORTING_API_BASE}/jobs/{job_id}/reports", "reports", params=params
        )

    async def _list_all(
        self,
        url: str,
        resource_key: str,
        *,
        params: Dict[str, Any] | None = None,
    ) -> List[Dict[str, Any]]:
        collected: List[Dict[str, Any]] = []
        page_token: str | None = None
        while True:
            page_params = dict(params or {})
            if page_token:
                page_params["pageToken"] = page_token
            payload = await self._get_json(url, params=page_params)
            collected.extend(payload.get(resource_key) or [])
            page_token = payload.get("nextPageToken")
            if not page_token:
                return collected

    async def download_report(self, download_url: str) -> tuple[str, str]:
        token = await self._token_provider()
        try:
            response = await self.http.get(
                download_url, headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise AnalyticsApiError(
                f"YouTube report download failed with HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise AnalyticsApiError("YouTube report download failed") from exc
        digest = hashlib.sha256(response.content).hexdigest()
        try:
            text = response.content.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise AnalyticsApiError("YouTube report is not valid UTF-8 CSV") from exc
        return text, digest

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()
