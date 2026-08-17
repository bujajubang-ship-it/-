# YouTube performance data accuracy

Phase D separates official source data from derived values. Numeric zero is
stored only when Google returns zero. Missing values remain SQL `NULL` with one
of `pending`, `unavailable`, `not_reported`, or `error` in status metadata.

## Official sources

The implementation was checked against Google's current official documents:

- Analytics metrics: https://developers.google.com/youtube/analytics/metrics
- Analytics channel report combinations: https://developers.google.com/youtube/analytics/channel_reports
- Reports query completeness: https://developers.google.com/youtube/analytics/reference/reports/query
- Reporting channel Reach report: https://developers.google.com/youtube/reporting/v1/reports/channel_reports
- Reporting metric definitions: https://developers.google.com/youtube/reporting/v1/reports/metrics
- Bulk report lifecycle: https://developers.google.com/youtube/reporting/v1/reports

| Data | Google source | Query/report | Delay and null policy | Recommended storage |
| --- | --- | --- | --- | --- |
| views, likes, comments, shares, subscribers gained/lost, watch minutes, average view duration/percentage | YouTube Analytics API v2 | `reports.query`, explicit video filter; aggregate snapshot or `day,video` | Response ends at the last date on which every requested metric is ready. Later requested dates are `pending`; omitted older rows are `not_reported`. | D1/D3/D7/D14/D30 plus daily sync for recent uploads |
| thumbnail impressions and CTR | YouTube Reporting API | `channel_reach_basic_a1`; `video_thumbnail_impressions`, `video_thumbnail_impressions_ctr` | Daily bulk files generally begin after a job exists and can take up to roughly 24–48 hours to appear. Omitted metric rows are `not_reported`. | Import each available daily Reach file once; keep report ID and hash |
| retention curve | YouTube Analytics API v2 | one video filter, `elapsedVideoTimeRatio`; `audienceWatchRatio,relativeRetentionPerformance` | Empty curve is `pending` unless an explicit terminal condition is known. No numeric zero is synthesized. | D1/D3/D7/D14/D30 and after meaningful edits |
| `retention_30s_estimate` | Derived locally | linear interpolation of `audienceWatchRatio` at `30 / duration_seconds` | Null when duration is at most 30 seconds, fewer than two usable points exist, or the target is outside the curve. | Stored beside the source retention snapshot and formula metadata |
| view growth per day | Derived locally | snapshot views divided by inclusive period days | Null when views are missing. | Stored with each aggregate snapshot |
| subscriber conversion | Derived locally | `subscribers_gained / views` | Null when either value is missing or views are zero. | Stored with each aggregate snapshot |

Reporting CTR is a percentage, not a ratio. Multi-day CTR is therefore:

`sum(impressions * ctr_percent) / sum(impressions)`

It is never calculated as views divided by impressions and is never averaged
without impression weighting.

## SQLite tables

The six established application tables are not altered. Phase D adds only
idempotent `CREATE TABLE IF NOT EXISTS` objects:

- `youtube_videos`
- `youtube_sync_runs`
- `video_metric_snapshots`
- `video_daily_metrics`
- `video_reach_metrics`
- `video_retention_snapshots`
- `video_retention_points`
- `youtube_reporting_jobs`
- `youtube_reporting_files`

Every source row or snapshot carries source, collection time, period, data
through date, sync run ID, and metric status metadata where applicable.

## Before production deployment

1. Take a fresh SQLite online backup of `/data/history.db` to off-site storage.
2. Verify its SHA-256 and `PRAGMA integrity_check`.
3. Record row counts for the existing six tables.
4. Deploy only after approval; startup will create the new tables without
   altering the established table schemas.
5. Re-run integrity check and compare the six original table schemas/counts.

## Google setup still requiring owner approval

The local code does not create jobs or change OAuth configuration. Before Reach
data can be collected in production, the owner must:

1. Confirm YouTube Reporting API is enabled for the existing Google project.
2. Confirm the existing refresh token grants the currently required read-only
   scopes. The repository already requests both `youtube.readonly` and
   `yt-analytics.readonly`; no automatic reauthorization occurs.
3. Explicitly approve creating one `channel_reach_basic_a1` reporting job.
4. Approve a scheduler only after the first report is manually verified.

No Render change is needed for fixture tests. Production collection continues
to use the existing `OAUTH_CLIENT_ID`, `OAUTH_CLIENT_SECRET`, and
`OAUTH_REFRESH_TOKEN`; none are printed by the dry-run command.
