"""Read-only YouTube Analytics API client with explicit missing-data semantics."""

from __future__ import annotations

import asyncio
import math
import os
import re
from datetime import date
from typing import Any, Dict, Iterable, List, Sequence

import httpx


TOKEN_URL = "https://oauth2.googleapis.com/token"
ANALYTICS_URL = "https://youtubeanalytics.googleapis.com/v2/reports"
YOUTUBE_CHANNELS_URL = "https://www.googleapis.com/youtube/v3/channels"
YOUTUBE_PLAYLIST_ITEMS_URL = "https://www.googleapis.com/youtube/v3/playlistItems"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"

STATUS_AVAILABLE = "available"
STATUS_PENDING = "pending"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_REPORTED = "not_reported"
STATUS_ERROR = "error"
VALID_STATUSES = frozenset(
    {
        STATUS_AVAILABLE,
        STATUS_PENDING,
        STATUS_UNAVAILABLE,
        STATUS_NOT_REPORTED,
        STATUS_ERROR,
    }
)

ANALYTICS_SOURCE = "youtube_analytics_api_v2"
# The API intermittently fails on paginated day,video queries. Staying below
# one 200-row page avoids the backend failure while preserving all requested
# rows through more, smaller cohorts.
DAILY_QUERY_ROW_BUDGET = 180
ANALYTICS_METRICS = (
    "views",
    "likes",
    "comments",
    "shares",
    "subscribersGained",
    "subscribersLost",
    "estimatedMinutesWatched",
    "averageViewDuration",
    "averageViewPercentage",
)
COUNT_METRICS = frozenset(
    {
        "views",
        "likes",
        "comments",
        "shares",
        "subscribersGained",
        "subscribersLost",
    }
)
FLOAT_METRICS = frozenset(
    {
        "estimatedMinutesWatched",
        "averageViewDuration",
        "averageViewPercentage",
    }
)
FLAT_NAMES = {
    "views": "views",
    "likes": "likes",
    "comments": "comments",
    "shares": "shares",
    "subscribersGained": "subscribers_gained",
    "subscribersLost": "subscribers_lost",
    "estimatedMinutesWatched": "watch_minutes",
    "averageViewDuration": "avg_view_duration_sec",
    "averageViewPercentage": "avg_view_percentage",
}


class AnalyticsApiError(RuntimeError):
    """Sanitized Analytics failure that never includes tokens or response bodies."""

    def __init__(self, message: str, *, code: str = "api_error") -> None:
        super().__init__(message)
        self.code = code


def _env_first(*names: str) -> str:
    for name in names:
        value = os.getenv(name, "").strip()
        if value:
            return value
    return ""


def _duration_seconds(value: str) -> int | None:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?",
        str(value or ""),
    )
    if not match:
        return None
    parts = {key: int(raw or 0) for key, raw in match.groupdict().items()}
    return (
        parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )


def metric_value(
    value: int | float | None,
    status: str,
    *,
    source: str = ANALYTICS_SOURCE,
    data_through: str | None = None,
) -> Dict[str, Any]:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid metric status: {status}")
    if status == STATUS_AVAILABLE and value is None:
        raise ValueError("An available metric must contain a value")
    if status != STATUS_AVAILABLE:
        value = None
    return {
        "value": value,
        "status": status,
        "source": source,
        "data_through": data_through,
    }


def _coerce_metric(name: str, raw: Any) -> int | float | None:
    if raw is None or raw == "":
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if name in COUNT_METRICS:
        return int(number)
    if name in FLOAT_METRICS:
        return float(number)
    return number


def _headers(payload: Dict[str, Any]) -> List[str]:
    return [str(item.get("name", "")) for item in payload.get("columnHeaders", [])]


def response_rows(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Map response rows by declared headers without inventing absent values."""

    headers = _headers(payload)
    if len(headers) != len(set(headers)):
        raise ValueError("Analytics response contains duplicate column headers")
    rows: List[Dict[str, Any]] = []
    for raw_row in payload.get("rows") or []:
        if len(raw_row) != len(headers):
            raise ValueError("Analytics response row length does not match headers")
        rows.append(dict(zip(headers, raw_row)))
    return rows


def _unique_rows(
    rows: Sequence[Dict[str, Any]], key_names: Sequence[str], *, label: str
) -> Dict[tuple[str, ...], Dict[str, Any]]:
    indexed: Dict[tuple[str, ...], Dict[str, Any]] = {}
    for row in rows:
        key = tuple(str(row.get(name)) for name in key_names)
        if key in indexed:
            raise ValueError(f"Duplicate {label} row in Analytics response")
        indexed[key] = row
    return indexed


def _metric_status_for_row(
    metric: str,
    row: Dict[str, Any] | None,
    response_headers: set[str],
    *,
    missing_row_status: str,
    data_through: str | None,
) -> Dict[str, Any]:
    if metric not in response_headers:
        return metric_value(None, STATUS_UNAVAILABLE, data_through=data_through)
    if row is None:
        return metric_value(None, missing_row_status, data_through=data_through)
    value = _coerce_metric(metric, row.get(metric))
    if value is None:
        return metric_value(None, STATUS_NOT_REPORTED, data_through=data_through)
    return metric_value(value, STATUS_AVAILABLE, data_through=data_through)


def parse_video_aggregate_response(
    payload: Dict[str, Any],
    requested_video_ids: Sequence[str],
    *,
    period_start: str,
    period_end: str,
    data_through: str | None,
) -> List[Dict[str, Any]]:
    headers = set(_headers(payload))
    indexed = _unique_rows(response_rows(payload), ["video"], label="video")
    by_video = {key[0]: row for key, row in indexed.items()}
    missing_status = STATUS_NOT_REPORTED if data_through else STATUS_PENDING
    results: List[Dict[str, Any]] = []

    for video_id in requested_video_ids:
        row = by_video.get(video_id)
        metrics = {
            metric: _metric_status_for_row(
                metric,
                row,
                headers,
                missing_row_status=missing_status,
                data_through=data_through,
            )
            for metric in ANALYTICS_METRICS
        }
        result: Dict[str, Any] = {
            "video_id": video_id,
            "metrics": metrics,
            "period_start": period_start,
            "period_end": period_end,
            "data_through": data_through,
            "source": ANALYTICS_SOURCE,
            "sample_size": 1 if row is not None else 0,
        }
        for api_name, flat_name in FLAT_NAMES.items():
            result[flat_name] = metrics[api_name]["value"]
            result[f"{flat_name}_status"] = metrics[api_name]["status"]
        results.append(result)
    return results


def parse_daily_response(
    payload: Dict[str, Any],
    requested_video_ids: Sequence[str],
    requested_dates: Sequence[str],
) -> List[Dict[str, Any]]:
    """Return an explicit video/date matrix with pending vs not-reported states."""

    headers = set(_headers(payload))
    rows = response_rows(payload)
    indexed = _unique_rows(rows, ["video", "day"], label="video/date")
    by_key = {(key[0], key[1]): row for key, row in indexed.items()}
    returned_dates = sorted({str(row.get("day")) for row in rows if row.get("day")})
    data_through = returned_dates[-1] if returned_dates else None
    parsed: List[Dict[str, Any]] = []

    for video_id in requested_video_ids:
        for metric_date in requested_dates:
            row = by_key.get((video_id, metric_date))
            missing_status = (
                STATUS_PENDING
                if data_through is None or metric_date > data_through
                else STATUS_NOT_REPORTED
            )
            metrics = {
                metric: _metric_status_for_row(
                    metric,
                    row,
                    headers,
                    missing_row_status=missing_status,
                    data_through=data_through,
                )
                for metric in ANALYTICS_METRICS
            }
            parsed.append(
                {
                    "video_id": video_id,
                    "metric_date": metric_date,
                    "metrics": metrics,
                    "row_status": STATUS_AVAILABLE if row is not None else missing_status,
                    "data_through": data_through,
                    "source": ANALYTICS_SOURCE,
                }
            )
    return parsed


def parse_retention_response(payload: Dict[str, Any]) -> List[Dict[str, float | None]]:
    headers = set(_headers(payload))
    required = {"elapsedVideoTimeRatio", "audienceWatchRatio"}
    if not required.issubset(headers):
        return []
    points: List[Dict[str, float | None]] = []
    for row in response_rows(payload):
        elapsed = _coerce_metric("elapsedVideoTimeRatio", row.get("elapsedVideoTimeRatio"))
        audience = _coerce_metric("audienceWatchRatio", row.get("audienceWatchRatio"))
        relative = _coerce_metric(
            "relativeRetentionPerformance", row.get("relativeRetentionPerformance")
        )
        if elapsed is None or audience is None:
            continue
        points.append(
            {
                "elapsed_video_time_ratio": float(elapsed),
                "audience_watch_ratio": float(audience),
                "relative_retention_performance": (
                    float(relative) if relative is not None else None
                ),
            }
        )
    points.sort(key=lambda item: float(item["elapsed_video_time_ratio"] or 0))
    return points


def retention_30s_estimate(
    points: Sequence[Dict[str, float | None]], duration_seconds: int | None
) -> tuple[float | None, Dict[str, Any]]:
    """Linearly interpolate the retention curve at 30 seconds.

    This is a derived estimate, not a YouTube Studio metric.
    """

    metadata: Dict[str, Any] = {
        "name": "retention_30s_estimate",
        "method": "linear_interpolation_audience_watch_ratio",
        "source_metric": "audienceWatchRatio",
        "source_dimension": "elapsedVideoTimeRatio",
        "target_seconds": 30,
    }
    if duration_seconds is None or duration_seconds <= 30:
        metadata["status"] = STATUS_UNAVAILABLE
        metadata["reason"] = "video_duration_not_over_30_seconds"
        return None, metadata
    if len(points) < 2:
        metadata["status"] = STATUS_UNAVAILABLE
        metadata["reason"] = "insufficient_curve_points"
        return None, metadata

    target = 30.0 / float(duration_seconds)
    metadata["target_elapsed_video_time_ratio"] = target
    normalized = [
        point
        for point in points
        if point.get("elapsed_video_time_ratio") is not None
        and point.get("audience_watch_ratio") is not None
    ]
    normalized.sort(key=lambda item: float(item["elapsed_video_time_ratio"] or 0))
    if len(normalized) < 2:
        metadata["status"] = STATUS_UNAVAILABLE
        metadata["reason"] = "insufficient_curve_points"
        return None, metadata

    for point in normalized:
        x = float(point["elapsed_video_time_ratio"] or 0)
        if math.isclose(x, target, rel_tol=0, abs_tol=1e-12):
            value = float(point["audience_watch_ratio"] or 0)
            metadata.update({"status": STATUS_AVAILABLE, "exact_point": True})
            return value, metadata

    for left, right in zip(normalized, normalized[1:]):
        left_x = float(left["elapsed_video_time_ratio"] or 0)
        right_x = float(right["elapsed_video_time_ratio"] or 0)
        if left_x < target < right_x and right_x > left_x:
            left_y = float(left["audience_watch_ratio"] or 0)
            right_y = float(right["audience_watch_ratio"] or 0)
            weight = (target - left_x) / (right_x - left_x)
            value = left_y + (right_y - left_y) * weight
            metadata.update(
                {
                    "status": STATUS_AVAILABLE,
                    "exact_point": False,
                    "left_ratio": left_x,
                    "right_ratio": right_x,
                }
            )
            return value, metadata

    metadata["status"] = STATUS_UNAVAILABLE
    metadata["reason"] = "target_outside_curve_range"
    return None, metadata


def chunked(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class AnalyticsService:
    def __init__(self, *, http: httpx.AsyncClient | None = None):
        # The channel-analysis flow historically used OAUTH_*. Render's
        # dedicated Analytics connector uses the clearer GOOGLE_*/YOUTUBE_*
        # names. Both are accepted without ever logging the resolved values.
        self.client_id = _env_first("GOOGLE_CLIENT_ID", "OAUTH_CLIENT_ID")
        self.client_secret = _env_first("GOOGLE_CLIENT_SECRET", "OAUTH_CLIENT_SECRET")
        self.refresh_token = _env_first(
            "YOUTUBE_ANALYTICS_REFRESH_TOKEN", "OAUTH_REFRESH_TOKEN"
        )
        self.scopes = {
            item.strip()
            for item in re.split(
                r"[\s,]+", os.getenv("YOUTUBE_ANALYTICS_SCOPES", "").strip()
            )
            if item.strip()
        }
        self._access_token = ""
        self.http = http or httpx.AsyncClient(timeout=30.0)
        self._owns_http = http is None

    def is_configured(self) -> bool:
        return bool(self.client_id and self.client_secret and self.refresh_token)

    def configuration_status(self) -> Dict[str, Any]:
        missing = []
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if not self.refresh_token:
            missing.append("refresh_token")
        return {
            "configured": not missing,
            "missing": missing,
            "analytics_scope_declared": any(
                "yt-analytics.readonly" in scope for scope in self.scopes
            ),
            "youtube_readonly_scope_declared": any(
                scope.endswith("/youtube.readonly") or scope == "youtube.readonly"
                for scope in self.scopes
            ),
        }

    async def _get_access_token(self) -> str:
        if self._access_token:
            return self._access_token
        try:
            response = await self.http.post(
                TOKEN_URL,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
            )
            if response.status_code >= 400:
                error_name = ""
                try:
                    error_name = str(response.json().get("error") or "")
                except (ValueError, AttributeError):
                    pass
                code = (
                    "invalid_refresh_token"
                    if error_name in {"invalid_grant", "invalid_token"}
                    or response.status_code == 401
                    else "api_error"
                )
                raise AnalyticsApiError(
                    "OAuth access token request failed", code=code
                )
            token = response.json().get("access_token")
        except AnalyticsApiError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise AnalyticsApiError("OAuth access token request failed") from exc
        if not token:
            raise AnalyticsApiError("OAuth response did not contain an access token")
        self._access_token = str(token)
        return self._access_token

    async def get_access_token(self) -> str:
        """Return a valid OAuth access token for sibling Google API clients."""

        return await self._get_access_token()

    async def _query(self, params: Dict[str, Any]) -> Dict[str, Any]:
        token = await self._get_access_token()
        payload: Any = None
        for attempt in range(3):
            try:
                response = await self.http.get(
                    ANALYTICS_URL,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                payload = response.json()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code in {429, 500, 502, 503, 504} and attempt < 2:
                    await asyncio.sleep(0.5 * (2**attempt))
                    continue
                raise AnalyticsApiError(
                    f"YouTube Analytics query failed with HTTP {status_code}",
                    code="insufficient_scope" if status_code in {401, 403} else "api_error",
                ) from exc
            except (httpx.HTTPError, ValueError) as exc:
                raise AnalyticsApiError("YouTube Analytics query failed") from exc
        if not isinstance(payload, dict):
            raise AnalyticsApiError("YouTube Analytics returned an invalid response")
        return payload

    async def get_authenticated_channel_id(self) -> str:
        """Resolve the OAuth owner's channel using the existing youtube.readonly scope."""

        token = await self._get_access_token()
        try:
            response = await self.http.get(
                YOUTUBE_CHANNELS_URL,
                params={"part": "id", "mine": "true", "maxResults": 1},
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            items = response.json().get("items") or []
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise AnalyticsApiError(
                f"YouTube owner channel lookup failed with HTTP {status}",
                code="insufficient_scope" if status in {401, 403} else "api_error",
            ) from exc
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise AnalyticsApiError("YouTube owner channel lookup failed") from exc
        if not items or not items[0].get("id"):
            raise AnalyticsApiError("OAuth account did not return an owned YouTube channel")
        return str(items[0]["id"])

    async def get_recent_upload_videos(self, *, limit: int = 30) -> List[Dict[str, Any]]:
        """Return the OAuth owner's newest uploads without requiring an API key."""

        limit = max(1, min(int(limit), 50))
        token = await self._get_access_token()
        headers = {"Authorization": f"Bearer {token}"}
        try:
            channel_response = await self.http.get(
                YOUTUBE_CHANNELS_URL,
                params={"part": "contentDetails", "mine": "true", "maxResults": 1},
                headers=headers,
            )
            channel_response.raise_for_status()
            channel_items = channel_response.json().get("items") or []
            uploads_id = (
                (((channel_items[0] if channel_items else {}).get("contentDetails") or {})
                 .get("relatedPlaylists") or {})
                .get("uploads")
            )
            if not uploads_id:
                return []
            playlist_response = await self.http.get(
                YOUTUBE_PLAYLIST_ITEMS_URL,
                params={
                    "part": "contentDetails,snippet",
                    "playlistId": uploads_id,
                    "maxResults": limit,
                },
                headers=headers,
            )
            playlist_response.raise_for_status()
            playlist_items = playlist_response.json().get("items") or []
            ordered_ids = [
                str((item.get("contentDetails") or {}).get("videoId") or "")
                for item in playlist_items
            ]
            ordered_ids = [video_id for video_id in ordered_ids if video_id]
            if not ordered_ids:
                return []
            videos_response = await self.http.get(
                YOUTUBE_VIDEOS_URL,
                params={
                    "part": "snippet,contentDetails",
                    "id": ",".join(ordered_ids),
                    "maxResults": len(ordered_ids),
                },
                headers=headers,
            )
            videos_response.raise_for_status()
            details = {
                str(item.get("id") or ""): item
                for item in (videos_response.json().get("items") or [])
            }
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            raise AnalyticsApiError(
                f"YouTube uploads lookup failed with HTTP {status}",
                code="insufficient_scope" if status in {401, 403} else "api_error",
            ) from exc
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise AnalyticsApiError("YouTube uploads lookup failed") from exc

        result = []
        for video_id in ordered_ids:
            item = details.get(video_id) or {}
            snippet = item.get("snippet") or {}
            result.append(
                {
                    "video_id": video_id,
                    "title": str(snippet.get("title") or ""),
                    "published_at": str(snippet.get("publishedAt") or "")[:10] or None,
                    "duration_seconds": _duration_seconds(
                        str((item.get("contentDetails") or {}).get("duration") or "")
                    ),
                    "source": "youtube_data_api_v3:oauth_uploads",
                }
            )
        return result

    async def _query_all_rows(
        self, params: Dict[str, Any], *, page_size: int = 200
    ) -> Dict[str, Any]:
        """Collect all rows using the API's 1-based startIndex pagination."""

        combined_rows: List[List[Any]] = []
        headers: List[Dict[str, Any]] | None = None
        start_index = 1
        while True:
            page_params = dict(params)
            page_params["startIndex"] = start_index
            page_params["maxResults"] = page_size
            payload = await self._query(page_params)
            page_headers = list(payload.get("columnHeaders") or [])
            if headers is None:
                headers = page_headers
            elif page_headers != headers:
                raise AnalyticsApiError("Analytics pagination returned inconsistent headers")
            page_rows = list(payload.get("rows") or [])
            combined_rows.extend(page_rows)
            if len(page_rows) < page_size:
                break
            start_index += len(page_rows)
            if start_index > 1_000_000:
                raise AnalyticsApiError("Analytics pagination exceeded the safety limit")
        return {"columnHeaders": headers or [], "rows": combined_rows}

    async def _data_through(
        self, video_ids: Sequence[str], start_date: str, end_date: str
    ) -> str | None:
        if not video_ids:
            return None
        # The Analytics filter has a practical URL/query-size limit. Full-channel
        # collection can exceed it (the production channel currently has 800+
        # videos), so derive freshness with the same bounded cohorts used by the
        # aggregate query instead of sending every ID in one HTTP request.
        days: list[str] = []
        unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
        for batch in chunked(unique_ids, 200):
            payload = await self._query_all_rows(
                {
                    "ids": "channel==MINE",
                    "startDate": start_date,
                    "endDate": end_date,
                    "metrics": ",".join(ANALYTICS_METRICS),
                    "dimensions": "day",
                    "filters": f"video=={','.join(batch)}",
                    "sort": "day",
                }
            )
            days.extend(
                str(row.get("day"))
                for row in response_rows(payload)
                if row.get("day")
            )
        return max(days) if days else None

    async def get_video_analytics(
        self,
        start_date: str = "2020-01-01",
        *,
        end_date: str | None = None,
        video_ids: Sequence[str] | None = None,
    ) -> List[Dict[str, Any]]:
        """Aggregate metrics for an explicit cohort, including low-view recent videos."""

        if not video_ids:
            raise ValueError("video_ids are required; implicit top-200 selection is disabled")
        unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
        requested_end = end_date or date.today().isoformat()
        data_through = await self._data_through(unique_ids, start_date, requested_end)
        results: List[Dict[str, Any]] = []
        for batch in chunked(unique_ids, 200):
            payload = await self._query_all_rows(
                {
                    "ids": "channel==MINE",
                    "startDate": start_date,
                    "endDate": requested_end,
                    "metrics": ",".join(ANALYTICS_METRICS),
                    "dimensions": "video",
                    "filters": f"video=={','.join(batch)}",
                    "sort": "-views",
                },
                page_size=min(200, len(batch)),
            )
            results.extend(
                parse_video_aggregate_response(
                    payload,
                    batch,
                    period_start=start_date,
                    period_end=requested_end,
                    data_through=data_through,
                )
            )
        return results

    async def get_daily_video_metrics(
        self,
        video_ids: Sequence[str],
        requested_dates: Sequence[str],
    ) -> List[Dict[str, Any]]:
        if not video_ids or not requested_dates:
            return []
        unique_ids = list(dict.fromkeys(video_id for video_id in video_ids if video_id))
        dates = sorted(set(requested_dates))
        results: List[Dict[str, Any]] = []
        # YouTube's backend consistently returns HTTP 500 while paging larger
        # day,video result sets. Keep each request inside one measured page.
        batch_size = max(1, min(200, DAILY_QUERY_ROW_BUDGET // len(dates)))
        for batch in chunked(unique_ids, batch_size):
            payload = await self._query_all_rows(
                {
                    "ids": "channel==MINE",
                    "startDate": dates[0],
                    "endDate": dates[-1],
                    "metrics": ",".join(ANALYTICS_METRICS),
                    "dimensions": "day,video",
                    "filters": f"video=={','.join(batch)}",
                    "sort": "day",
                },
                page_size=min(200, len(batch) * len(dates)),
            )
            results.extend(parse_daily_response(payload, batch, dates))
        return results

    async def get_video_retention(
        self,
        video_id: str,
        *,
        start_date: str,
        end_date: str | None = None,
    ) -> Dict[str, Any]:
        requested_end = end_date or date.today().isoformat()
        data_through = await self._data_through([video_id], start_date, requested_end)
        payload = await self._query(
            {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": requested_end,
                "metrics": "audienceWatchRatio,relativeRetentionPerformance",
                "dimensions": "elapsedVideoTimeRatio",
                "filters": f"video=={video_id}",
            }
        )
        points = parse_retention_response(payload)
        if points:
            status = STATUS_AVAILABLE
        elif data_through is None or data_through < requested_end:
            status = STATUS_PENDING
        else:
            status = STATUS_NOT_REPORTED
        return {
            "video_id": video_id,
            "period_start": start_date,
            "period_end": requested_end,
            "data_through": data_through,
            "status": status,
            "points": points,
            "source": ANALYTICS_SOURCE,
        }

    async def get_channel_overview(
        self, *, start_date: str = "2025-01-01", end_date: str | None = None
    ) -> Dict[str, Any]:
        """Valid channel metrics only; thumbnail reach is deliberately excluded."""

        return await self._query(
            {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date or date.today().isoformat(),
                "metrics": (
                    "views,estimatedMinutesWatched,averageViewDuration,"
                    "averageViewPercentage,likes,comments,shares,"
                    "subscribersGained,subscribersLost"
                ),
                "dimensions": "month",
                "sort": "month",
            }
        )

    async def get_channel_snapshot(
        self, *, start_date: str, end_date: str | None = None
    ) -> Dict[str, Any]:
        """One aggregate channel row for an arbitrary rolling date range."""

        return await self._query(
            {
                "ids": "channel==MINE",
                "startDate": start_date,
                "endDate": end_date or date.today().isoformat(),
                "metrics": (
                    "views,estimatedMinutesWatched,averageViewDuration,"
                    "averageViewPercentage,likes,comments,shares,"
                    "subscribersGained"
                ),
            }
        )

    async def close(self) -> None:
        if self._owns_http:
            await self.http.aclose()
