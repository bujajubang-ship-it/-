# Security + DB safety production release checklist

This release branch is based on `origin/main` commit `55cc94c`. It contains
owner authentication and production SQLite safety only. It deliberately excludes
the GPT Strategy Brain scaffold, OpenAI dependency, PostgreSQL adapter, migration
CLI/schema, retrieval code, and PostgreSQL test dependencies.

## Exact environment formats

Set values in the existing Render service Dashboard. Do not add secrets to git
or `render.yaml`, and do not recreate or edit the Persistent Disk resource.

| Name | Exact format accepted by this release |
| --- | --- |
| `APP_ENV` | Use the literal `production`. Matching is case-insensitive, but this exact lowercase value is recommended. |
| `AUTH_MODE` | Use the literal `owner`. Code accepts only `owner` or `disabled`, and production rejects `disabled`. |
| `OWNER_USERNAME` | Non-empty text chosen by the owner; surrounding whitespace is removed. |
| `OWNER_PASSWORD_HASH` | Output of `python -m owner_auth hash-password`; versioned `scrypt_v1$...` format only. |
| `AUTH_SIGNING_SECRET` | Independently generated secret containing at least 32 UTF-8 bytes. Do not reuse a password or machine secret. |
| `AUTH_ALLOWED_ORIGINS` | Comma-separated HTTP(S) origins only, for example `https://youtube-researcher.onrender.com`. No path, query, fragment, or embedded credentials. |
| `AUTH_COOKIE_NAME` | Optional. Omit for `yt_owner_session`, or use a non-empty cookie name without spaces, semicolon, comma, tab, CR, or LF. |
| `AUTH_SESSION_TTL_SECONDS` | Optional base-10 integer from `300` through `7776000`. Omit for `604800`; `43200` is the recommended 12-hour owner session. |
| `AUTH_LOGIN_MAX_FAILURES` | Optional integer from `3` through `20`; default `5`. |
| `AUTH_LOGIN_WINDOW_SECONDS` | Optional integer from `60` through `86400`; default `900`. |
| `DB_BACKEND` | Literal `sqlite`. Any other value fails startup; this release contains no PostgreSQL backend. |
| `DB_PATH` | Literal `/data/history.db` on Render. No other production path is accepted. |
| `PIPELINE_REMIND_SECRET` | Existing non-empty Lightsail machine credential; must match the caller's `x-secret`. Required at production startup. |
| `PIPELINE_REMIND_PHONE` | Existing destination value needed when a reminder is actually sent. |
| `CNMAKER_BASE` | Existing CNMaker service base URL. The code removes a trailing `/` but otherwise does not validate the URL. Do not invent or rotate it during this release. |
| `CNMAKER_SECRET` | Existing CNMaker credential used by transcription and the legacy KV copy. |

`RENDER` is supplied by Render and is not a value to invent locally.
`DATABASE_URL` is ignored by this release and should not be used for cutover.

Generate the password hash interactively on the Mac:

```bash
python -m owner_auth hash-password
```

Generate a separate signing secret locally, then copy only its output into the
Render secret field:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

## Pre-deploy order

1. Make a fresh SQLite online backup of `/data/history.db` outside Render.
2. Verify the backup SHA-256 and `integrity_check`.
3. Reconfirm the existing disk mount is `/data`; do not modify the disk resource.
4. Reconfirm all six required tables exist in `/data/history.db`.
5. Enter and review all environment values without displaying them in logs.
6. Confirm the Lightsail and Render `PIPELINE_REMIND_SECRET` values match.
7. Deploy only after explicit approval.
8. Verify `/healthz`, unauthenticated rejection, login/logout, CRUD, Claude SSE,
   multipart video upload, and the machine reminder call.
9. Compare database row counts before and after the smoke tests.

## Rollback

The code rollback target is the current production main commit
`55cc94cc23a4138739ef34e93313fd313a8e945d`. A rollback must not detach,
recreate, overwrite, or restore `/data/history.db`. The old commit re-exposes the
previous authentication and hardcoded-secret risks, so use it only as an
emergency code rollback while preserving the new environment values.
