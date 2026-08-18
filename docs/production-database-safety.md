# Production SQLite startup safety

The Render Starter service uses the existing `/data` Persistent Disk as its
source of truth. The confirmed runtime settings are `DB_BACKEND=sqlite` and
`DB_PATH=/data/history.db`.

The Render Dashboard remains the source of truth for the existing disk resource.
This release does not declare or change a disk name, mount, or size in
`render.yaml`.

When `RENDER` is true, startup accepts only `/data/history.db`. It verifies the
real directory and file, opens SQLite read-only, runs `PRAGMA quick_check`, and
requires all six established tables: `history`, `pipeline`, `optimize_videos`,
`worksheet_rows`, `chat_session`, and `knowledge`.

Normal application connections use SQLite `mode=rw`, so CRUD continues to work
but a missing DB cannot be replaced by a new empty file. Production table-ensure
helpers skip `CREATE TABLE`; local development keeps the existing fallback and
lazy table creation behavior.

This minimal release contains no PostgreSQL adapter or migration CLI.
`DATABASE_URL` is ignored and any `DB_BACKEND` other than `sqlite` fails closed.

The app no longer restores video-feedback rows from KV during startup. The
operator-only command below defaults to a no-write inspection:

```bash
python -m recovery_tools.video_feedback_kv
```

Actual recovery requires `--apply`, the exact confirmation phrase, valid CNMaker
environment variables, valid KV JSON/timestamps, and zero existing
`video_feedback` rows.
