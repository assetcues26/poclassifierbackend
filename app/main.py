"""FastAPI entrypoint for PO Classification Batch UI."""

from __future__ import annotations

import json
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app import jobs as job_service
from app.auth import (
    CurrentUser,
    LoginBody,
    authenticate_user,
    clear_session_cookie,
    require_auth_configured,
    set_session_cookie,
)
from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    fixed = job_service.recover_stuck_jobs()
    if fixed:
        logger.info("Recovered %s stuck job(s) on startup", fixed)
    yield


app = FastAPI(
    title="PO Classification Batch UI API",
    version="1.0.0",
    lifespan=lifespan,
    # Keep API shape private; only login is public.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.post("/api/login")
def login(body: LoginBody, response: Response) -> dict:
    require_auth_configured(settings)
    username = authenticate_user(body.username.strip(), body.password, settings)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    set_session_cookie(response, username, settings)
    return {"ok": True, "username": username}


@app.post("/api/logout")
def logout(response: Response) -> dict:
    clear_session_cookie(response, settings)
    return {"ok": True}


@app.get("/api/me")
def me(user: CurrentUser) -> dict:
    return {"ok": True, "username": user}


@app.get("/api/health")
def health(_user: CurrentUser) -> dict:
    return {
        "ok": True,
        "batch_size": settings.batch_size,
        "azure_configured": bool(settings.azure_classify_url),
    }


@app.post("/api/jobs")
async def create_job(
    _user: CurrentUser,
    file: UploadFile = File(...),
) -> dict:
    if not file.filename or not file.filename.lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="Please upload an .xlsx Excel file.")

    suffix = Path(file.filename).suffix or ".xlsx"
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
            content = await file.read()
            tmp.write(content)
            tmp_path = tmp.name
        summary = job_service.create_job_from_excel(tmp_path, file.filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:  # noqa: BLE001
        logger.exception("Failed to parse Excel upload")
        raise HTTPException(status_code=500, detail="Failed to parse Excel.") from None
    finally:
        if tmp_path:
            try:
                Path(tmp_path).unlink(missing_ok=True)
            except Exception:
                pass
    return summary


@app.post("/api/jobs/from-results")
async def create_job_from_results(
    _user: CurrentUser,
    file: UploadFile = File(...),
) -> dict:
    if not file.filename or not file.filename.lower().endswith(".json"):
        raise HTTPException(
            status_code=400,
            detail="Please upload the combined Download results .json file.",
        )
    try:
        raw = await file.read()
        payload = json.loads(raw.decode("utf-8"))
        summary = job_service.create_job_from_results(payload, file.filename)
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="Results file must be UTF-8 JSON.") from exc
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON file.") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception:  # noqa: BLE001
        logger.exception("Failed to load results upload")
        raise HTTPException(status_code=500, detail="Failed to load results.") from None
    return summary


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, _user: CurrentUser) -> dict:
    try:
        return job_service.summarize_job(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None


@app.get("/api/jobs/{job_id}/items/{synthetic_id}")
def get_item(job_id: str, synthetic_id: int, _user: CurrentUser) -> dict:
    try:
        return job_service.get_record(job_id, synthetic_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except KeyError:
        raise HTTPException(status_code=404, detail="PO not found") from None


@app.post("/api/jobs/{job_id}/start")
def start_job(job_id: str, _user: CurrentUser) -> dict:
    try:
        return job_service.start_job(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/retry-failed")
def retry_failed(job_id: str, _user: CurrentUser) -> dict:
    try:
        return job_service.retry_failed(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str, _user: CurrentUser) -> dict:
    try:
        return job_service.cancel_job(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str, _user: CurrentUser) -> dict:
    try:
        job_service.delete_job(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "job_id": job_id}


@app.get("/api/jobs/{job_id}/download")
def download_results(job_id: str, _user: CurrentUser) -> StreamingResponse:
    try:
        payload = job_service.build_download_payload(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"po-results-{job_id[:8]}.json"
    return StreamingResponse(
        iter([body.encode("utf-8")]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/jobs/{job_id}/download-raw")
def download_raw_results(job_id: str, _user: CurrentUser) -> StreamingResponse:
    try:
        payload = job_service.build_raw_download_payload(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Job not found") from None

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    filename = f"po-azure-raw-{job_id[:8]}.json"
    return StreamingResponse(
        iter([body.encode("utf-8")]),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/")
def root(_user: CurrentUser) -> JSONResponse:
    return JSONResponse(
        {
            "service": "PO Classification Batch UI API",
            "authenticated_as": _user,
        }
    )
