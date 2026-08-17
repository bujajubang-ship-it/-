# Owner authentication runbook

This phase protects the existing site without changing its AI provider or
database backend. Production remains closed unless all required owner-auth
settings are present and valid.

## Runtime behavior

- Local development defaults to `AUTH_MODE=disabled`.
- Render or `APP_ENV=production` requires `AUTH_MODE=owner`.
- Every page and API is private by default.
- `/healthz`, `/login`, and the login/logout actions are public.
- `POST /api/pipeline-remind` bypasses the browser cookie but still requires
  its existing `x-secret` machine credential.
- `/api/transcript-debug` is unavailable in production.
- The session is a stateless, full HMAC-SHA256 signed cookie. The browser gets
  `HttpOnly`, `SameSite=Strict`, and, in production, `Secure` attributes.

## Required production environment variable names

- `APP_ENV`
- `AUTH_MODE`
- `OWNER_USERNAME`
- `OWNER_PASSWORD_HASH`
- `AUTH_SIGNING_SECRET`
- `PIPELINE_REMIND_SECRET`
- `PIPELINE_REMIND_PHONE`
- `CNMAKER_BASE`
- `CNMAKER_SECRET`

Optional authentication tuning:

- `AUTH_ALLOWED_ORIGINS`
- `AUTH_COOKIE_NAME`
- `AUTH_SESSION_TTL_SECONDS`
- `AUTH_LOGIN_MAX_FAILURES`
- `AUTH_LOGIN_WINDOW_SECONDS`
- `ENABLE_TRANSCRIPT_DEBUG` (development only)

Do not place values in `render.yaml` or commit them to git.

## Generate the password hash locally

Run this command on the Mac from the repository directory:

```bash
python -m owner_auth hash-password
```

The command reads the password twice with terminal echo disabled. Copy only
the resulting `scrypt_v1$...` value into `OWNER_PASSWORD_HASH`. The plaintext
password is not written to a file or environment variable.

Generate `AUTH_SIGNING_SECRET` independently with a cryptographically secure
random generator. It must contain at least 32 bytes. Rotating it invalidates
all existing browser sessions.

## Safe production rollout

1. Confirm the current `/data/history.db` backup and integrity result again.
2. In Render, add every required environment variable and choose **Save only**.
3. Update the Lightsail caller and Render with a coordinated
   `PIPELINE_REMIND_SECRET`; never reuse the owner signing secret.
4. Confirm `CNMAKER_SECRET` is the value accepted by the existing Lightsail
   transcription/KV service.
5. Review the Render environment names without displaying their values.
6. Deploy only after explicit approval.
7. Verify `/healthz`, unauthenticated rejection, login, CRUD, an AI SSE request,
   multipart video upload, logout, and the Lightsail reminder call.

## Rollback

- Redeploy the previous known-good commit.
- Do not change, detach, or recreate the persistent disk.
- Keep the previous machine credential available until the Lightsail test has
  succeeded with the new credential.
- If the signing secret changes during rollback, existing cookies become
  invalid and the owner must log in again; no database data is affected.
