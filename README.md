# PO Classification Batch UI — Backend

FastAPI service that accepts Excel/JSON uploads, batches calls to the deployed Azure `classify-po` API, and stores job state on disk.

## Prerequisites

- Python 3.11+
- Deployed Azure classify-po URL (and function key if required)

## Setup

```powershell
cd PO_classifcationUI\backend
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` (never commit this file):

1. Set `AZURE_CLASSIFY_URL` (and `AZURE_FUNCTION_KEY` if needed)
2. Set `SESSION_SECRET` (long random string)
3. Set `APP_USERS` after generating a bcrypt hash (see Auth below)
4. Leave local cookie flags as `COOKIE_SAMESITE=lax` and `COOKIE_SECURE=false`

Start:

```powershell
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

API base: http://localhost:8000  

Swagger `/docs` is disabled (API is private).

## Tests

Unit tests use pytest. Azure HTTP calls are mocked with `respx` — nothing hits the real classifier.

```powershell
cd PO_classifcationUI\backend
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
```

`pytest.ini` sets `asyncio_mode = auto` so async tests run without extra markers. Tests write job files under a temporary folder (not `jobs/`).

## Auth

Only `POST /api/login` and `POST /api/logout` are public. Every other route requires a valid session cookie.

### Create username + password hash

```powershell
.\.venv\Scripts\Activate.ps1
python -c "import bcrypt; print(bcrypt.hashpw(b'YOUR_PASSWORD_HERE', bcrypt.gensalt()).decode())"
```

Paste the full printed hash into `.env`:

```env
APP_USERS=yourusername:$2b$12$...full_hash...
SESSION_SECRET=...long random string...
SESSION_MAX_AGE_HOURS=8
COOKIE_SAMESITE=lax
COOKIE_SECURE=false
```

Generate a session secret:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Restart uvicorn after changing auth env vars.

### Cookie settings

| Environment | `COOKIE_SAMESITE` | `COOKIE_SECURE` |
|---|---|---|
| Local (HTTP) | `lax` | `false` |
| Production (Vercel proxies `/api` → Render) | `lax` | `true` |

Also set `CORS_ORIGINS` to your frontend origin(s), e.g. `http://localhost:5173`
locally or `https://poclassifier.vercel.app` in production (no trailing slash).

With the Vercel rewrite, the browser talks only to the Vercel origin, so cookies
are first-party (`lax` is enough). Use `none` + `true` only if the browser calls
Render directly (no proxy).

**Local tip:** use `localhost` for both the UI and `VITE_API_BASE_URL` (do not mix `localhost` and `127.0.0.1`), or session cookies will not be sent and uploads will return 401.

## Important env vars

| Variable | Purpose |
|---|---|
| `AZURE_CLASSIFY_URL` | Deployed classify-po endpoint |
| `AZURE_FUNCTION_KEY` | Optional function key |
| `BATCH_SIZE` | POs per Azure request (default 5) |
| `MAX_EXCEL_PO_ROWS` | Max PO data rows per upload |
| `JOBS_DIR` | Disk folder for job state |
| `CORS_ORIGINS` | Allowed frontend origins (comma-separated) |
| `APP_USERS` | `username:bcrypt_hash` (comma-separated if multiple) |
| `SESSION_SECRET` | Signs session cookies |
| `SESSION_COOKIE_NAME` | Cookie name (default `po_ui_session`) |
| `SESSION_MAX_AGE_HOURS` | Cookie lifetime (default 8) |
| `COOKIE_SAMESITE` / `COOKIE_SECURE` | Cookie flags (see table above) |

See `.env.example` for the full template.

## Deploy (Render)

1. Web service: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
2. Single worker (`--workers 1`)
3. Persistent disk mounted at `JOBS_DIR` for long runs
4. Set all secrets in the Render dashboard (`APP_USERS`, `SESSION_SECRET`, Azure URL/key, cookie flags, `CORS_ORIGINS`)

## Security

- Put secrets only in `.env` / host env — never in git
- Commit `.env.example` only (placeholders, no real passwords/hashes/keys)
- Password hashes in `APP_USERS`; plain password never stored
- Azure URL/key stay on the backend only
- Job folders under `jobs/` are gitignored (may contain PO data)
