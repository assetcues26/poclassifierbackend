"""Disk-backed job store and sequential batch worker."""

from __future__ import annotations

import json
import logging
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.azure_client import classify_batch
from app.config import Settings, get_settings
from app.excel_service import build_azure_payload, parse_excel

logger = logging.getLogger(__name__)

# In-process lock so one job isn't started twice
_job_locks: dict[str, threading.Lock] = {}
_running: set[str] = set()

_INTERRUPTED_MSG = "Interrupted — use Retry failed if needed"
_CANCELLED_MSG = "Cancelled"
_WORKER_STOPPED_MSG = "Worker stopped unexpectedly"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_dir(job_id: str, settings: Settings) -> Path:
    return settings.jobs_path / job_id


def _meta_path(job_id: str, settings: Settings) -> Path:
    return _job_dir(job_id, settings) / "meta.json"


def _records_path(job_id: str, settings: Settings) -> Path:
    return _job_dir(job_id, settings) / "records.json"


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _finalize_state(meta: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """Set ready/completed from remaining pending (keeps view_only)."""
    if meta.get("mode") == "view" or meta.get("state") == "view_only":
        meta["state"] = "view_only"
        return
    meta["cancel_requested"] = False
    if any(r["status"] == "pending" for r in records):
        meta["state"] = "ready"
    else:
        meta["state"] = "completed"


def recover_stuck_jobs() -> int:
    """On startup: fix jobs left running/processing after a crash or restart."""
    settings = get_settings()
    if not settings.jobs_path.exists():
        return 0

    fixed = 0
    for meta_file in settings.jobs_path.glob("*/meta.json"):
        try:
            meta = _read_json(meta_file)
            job_id = meta.get("job_id") or meta_file.parent.name
            if meta.get("mode") == "view" or meta.get("state") == "view_only":
                continue
            if meta.get("state") != "running":
                continue

            records_file = meta_file.parent / "records.json"
            records = _read_json(records_file)
            changed = False
            for r in records:
                if r["status"] == "processing":
                    r["status"] = "failed"
                    r["message"] = _INTERRUPTED_MSG
                    r["azure_response"] = None
                    changed = True
            if changed or meta.get("state") == "running":
                _refresh_counts(meta, records)
                _finalize_state(meta, records)
                _write_json(records_file, records)
                _write_json(meta_file, meta)
                fixed += 1
                logger.info("Recovered stuck job %s → %s", job_id, meta["state"])
        except Exception:  # noqa: BLE001
            logger.exception("Failed to recover job at %s", meta_file)
    return fixed


def create_job_from_excel(file_path: str | Path, original_name: str) -> dict[str, Any]:
    settings = get_settings()
    settings.jobs_path.mkdir(parents=True, exist_ok=True)

    records = parse_excel(file_path, settings)
    job_id = uuid.uuid4().hex
    job_path = _job_dir(job_id, settings)
    job_path.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "original_filename": original_name,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "mode": "process",
        "state": "ready",  # ready | running | completed
        "cancel_requested": False,
        "total": len(records),
        "pending": sum(1 for r in records if r["status"] == "pending"),
        "processing": 0,
        "completed": sum(1 for r in records if r["status"] == "completed"),
        "failed": sum(1 for r in records if r["status"] == "failed"),
        "skipped": sum(1 for r in records if r["status"] == "skipped"),
        "invalid": sum(1 for r in records if r["status"] == "invalid"),
    }
    _write_json(_meta_path(job_id, settings), meta)
    _write_json(_records_path(job_id, settings), records)
    _job_locks[job_id] = threading.Lock()
    return summarize_job(job_id)


def create_job_from_results(payload: Any, original_name: str) -> dict[str, Any]:
    """Build a view-only job from a combined Download results JSON file."""
    settings = get_settings()
    settings.jobs_path.mkdir(parents=True, exist_ok=True)

    if not isinstance(payload, list):
        raise ValueError("Results file must be a JSON array of PO results.")
    if not payload:
        raise ValueError("Results file is empty.")

    # Combined download has erp_json; raw Azure download does not — reject the latter.
    if not any(isinstance(row, dict) and "erp_json" in row for row in payload):
        raise ValueError(
            "Upload the combined Download results JSON (with erp_json), "
            "not the raw Azure download."
        )

    max_rows = settings.max_excel_po_rows
    if len(payload) > max_rows:
        raise ValueError(
            f"Results file has {len(payload)} purchase orders. "
            f"Maximum allowed is {max_rows}."
        )

    allowed_status = {
        "pending",
        "processing",
        "completed",
        "failed",
        "skipped",
        "invalid",
    }
    records: list[dict[str, Any]] = []
    for idx, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Row {idx}: must be a JSON object.")
        if "erp_json" not in row:
            raise ValueError(
                f"Row {idx}: missing erp_json. "
                "Upload the combined Download results JSON."
            )

        po_number = row.get("purchase_order_number")
        po_number = "" if po_number is None else str(po_number).strip()
        status = str(row.get("status") or "completed").strip().lower()
        if status not in allowed_status:
            status = "completed"
        message = row.get("message") or ""
        erp = row.get("erp_json")
        if erp is not None and not isinstance(erp, dict):
            raise ValueError(f"Row {idx}: erp_json must be an object or null.")
        azure = row.get("azure_output")
        if azure is not None and not isinstance(azure, dict):
            raise ValueError(f"Row {idx}: azure_output must be an object or null.")

        records.append(
            {
                "row_number": idx,
                "synthetic_id": idx,
                "po_number": po_number,
                "erp": erp,
                "status": status,
                "message": str(message),
                "azure_response": azure,
            }
        )

    job_id = uuid.uuid4().hex
    job_path = _job_dir(job_id, settings)
    job_path.mkdir(parents=True, exist_ok=True)

    meta = {
        "job_id": job_id,
        "original_filename": original_name,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "mode": "view",
        "state": "view_only",
        "cancel_requested": False,
        "total": len(records),
        "pending": 0,
        "processing": 0,
        "completed": 0,
        "failed": 0,
        "skipped": 0,
        "invalid": 0,
    }
    _refresh_counts(meta, records)
    meta["state"] = "view_only"
    _write_json(_meta_path(job_id, settings), meta)
    _write_json(_records_path(job_id, settings), records)
    _job_locks[job_id] = threading.Lock()
    return summarize_job(job_id)


def _refresh_counts(meta: dict[str, Any], records: list[dict[str, Any]]) -> None:
    meta["total"] = len(records)
    meta["pending"] = sum(1 for r in records if r["status"] == "pending")
    meta["processing"] = sum(1 for r in records if r["status"] == "processing")
    meta["completed"] = sum(1 for r in records if r["status"] == "completed")
    meta["failed"] = sum(1 for r in records if r["status"] == "failed")
    meta["skipped"] = sum(1 for r in records if r["status"] == "skipped")
    meta["invalid"] = sum(1 for r in records if r["status"] == "invalid")
    meta["updated_at"] = _utc_now()


def summarize_job(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    meta = _read_json(_meta_path(job_id, settings))
    records = _read_json(_records_path(job_id, settings))
    # Lightweight list for UI (no full ERP / azure blobs)
    items = [
        {
            "synthetic_id": r["synthetic_id"],
            "row_number": r["row_number"],
            "po_number": r["po_number"],
            "status": r["status"],
            "message": r.get("message") or "",
            "line_count": len((r.get("erp") or {}).get("lines") or []),
        }
        for r in records
    ]
    return {**meta, "items": items}


def get_record(job_id: str, synthetic_id: int) -> dict[str, Any]:
    settings = get_settings()
    records = _read_json(_records_path(job_id, settings))
    for r in records:
        if int(r["synthetic_id"]) == int(synthetic_id):
            return {
                "synthetic_id": r["synthetic_id"],
                "row_number": r["row_number"],
                "po_number": r["po_number"],
                "status": r["status"],
                "message": r.get("message") or "",
                "erp": r.get("erp"),
                "azure_response": r.get("azure_response"),
            }
    raise KeyError(f"PO synthetic_id={synthetic_id} not found")


def start_job(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    meta = _read_json(_meta_path(job_id, settings))
    if meta.get("mode") == "view" or meta.get("state") == "view_only":
        raise RuntimeError("View-only jobs cannot be processed.")
    if meta.get("state") == "running" or job_id in _running:
        raise RuntimeError("Job is already running.")

    records = _read_json(_records_path(job_id, settings))
    if not any(r["status"] == "pending" for r in records):
        raise RuntimeError("No pending POs to process.")

    meta["state"] = "running"
    meta["cancel_requested"] = False
    meta["updated_at"] = _utc_now()
    _write_json(_meta_path(job_id, settings), meta)

    thread = threading.Thread(target=_worker_sync, args=(job_id,), daemon=True)
    thread.start()
    return summarize_job(job_id)


def retry_failed(job_id: str) -> dict[str, Any]:
    settings = get_settings()
    meta = _read_json(_meta_path(job_id, settings))
    if meta.get("mode") == "view" or meta.get("state") == "view_only":
        raise RuntimeError("View-only jobs cannot be retried.")
    if meta.get("state") == "running" or job_id in _running:
        raise RuntimeError("Job is already running.")

    records = _read_json(_records_path(job_id, settings))
    changed = False
    for r in records:
        if r["status"] == "failed":
            r["status"] = "pending"
            r["message"] = ""
            r["azure_response"] = None
            changed = True
    if not changed:
        raise RuntimeError("No failed POs to retry.")

    _refresh_counts(meta, records)
    meta["state"] = "ready"
    meta["cancel_requested"] = False
    _write_json(_records_path(job_id, settings), records)
    _write_json(_meta_path(job_id, settings), meta)
    return start_job(job_id)


def cancel_job(job_id: str) -> dict[str, Any]:
    """Request cancel; worker stops after the current batch."""
    settings = get_settings()
    meta = _read_json(_meta_path(job_id, settings))
    if meta.get("mode") == "view" or meta.get("state") == "view_only":
        raise RuntimeError("View-only jobs cannot be cancelled.")
    if meta.get("state") != "running" and job_id not in _running:
        raise RuntimeError("Job is not running.")

    meta["cancel_requested"] = True
    meta["updated_at"] = _utc_now()
    _write_json(_meta_path(job_id, settings), meta)
    return summarize_job(job_id)


def delete_job(job_id: str) -> None:
    """Remove job files from disk. Refuses if still running."""
    settings = get_settings()
    meta_path = _meta_path(job_id, settings)
    if not meta_path.exists():
        raise FileNotFoundError(job_id)

    meta = _read_json(meta_path)
    if meta.get("state") == "running" or job_id in _running:
        raise RuntimeError("Cannot delete a running job. Cancel it first.")

    shutil.rmtree(_job_dir(job_id, settings), ignore_errors=True)
    _job_locks.pop(job_id, None)


def build_download_payload(job_id: str) -> list[dict[str, Any]]:
    """User-facing download: no synthetic purchaseorderid."""
    settings = get_settings()
    records = _read_json(_records_path(job_id, settings))
    out: list[dict[str, Any]] = []
    for r in records:
        azure = r.get("azure_response")
        cleaned_azure = _strip_synthetic_ids(azure) if azure else None
        out.append(
            {
                "purchase_order_number": r["po_number"],
                "status": r["status"],
                "message": r.get("message") or "",
                "erp_json": r.get("erp"),
                "azure_output": cleaned_azure,
            }
        )
    return out


def build_raw_download_payload(job_id: str) -> list[dict[str, Any]]:
    """Azure responses only (as returned), keyed by real PO number."""
    settings = get_settings()
    records = _read_json(_records_path(job_id, settings))
    return [
        {
            "purchase_order_number": r["po_number"],
            "azure_output": r.get("azure_response"),
        }
        for r in records
    ]


def _strip_synthetic_ids(azure_obj: Any) -> Any:
    if isinstance(azure_obj, dict):
        cleaned = {}
        for k, v in azure_obj.items():
            if k == "purchaseorderid":
                continue
            cleaned[k] = _strip_synthetic_ids(v)
        return cleaned
    if isinstance(azure_obj, list):
        return [_strip_synthetic_ids(x) for x in azure_obj]
    return azure_obj


def _mark_remaining_cancelled(records: list[dict[str, Any]]) -> None:
    for r in records:
        if r["status"] in ("pending", "processing"):
            r["status"] = "failed"
            r["message"] = _CANCELLED_MSG
            r["azure_response"] = None


def _cleanup_worker_exit(job_id: str) -> None:
    """Ensure disk state is not left as running/processing after worker ends."""
    settings = get_settings()
    try:
        meta = _read_json(_meta_path(job_id, settings))
        records = _read_json(_records_path(job_id, settings))
    except FileNotFoundError:
        return

    if meta.get("mode") == "view" or meta.get("state") == "view_only":
        return

    dirty = False
    for r in records:
        if r["status"] == "processing":
            r["status"] = "failed"
            r["message"] = _WORKER_STOPPED_MSG
            r["azure_response"] = None
            dirty = True

    if meta.get("state") == "running" or dirty:
        _refresh_counts(meta, records)
        _finalize_state(meta, records)
        _write_json(_records_path(job_id, settings), records)
        _write_json(_meta_path(job_id, settings), meta)


def _worker_sync(job_id: str) -> None:
    """Bridge: run async worker inside a dedicated thread."""
    import asyncio

    _running.add(job_id)
    try:
        asyncio.run(_process_job(job_id))
    except Exception:  # noqa: BLE001
        logger.exception("Worker crashed for job %s", job_id)
    finally:
        _cleanup_worker_exit(job_id)
        _running.discard(job_id)


async def _process_job(job_id: str) -> None:
    settings = get_settings()
    lock = _job_locks.setdefault(job_id, threading.Lock())

    while True:
        with lock:
            meta = _read_json(_meta_path(job_id, settings))
            records = _read_json(_records_path(job_id, settings))

            if meta.get("cancel_requested"):
                _mark_remaining_cancelled(records)
                _refresh_counts(meta, records)
                _finalize_state(meta, records)
                _write_json(_records_path(job_id, settings), records)
                _write_json(_meta_path(job_id, settings), meta)
                return

            pending = [r for r in records if r["status"] == "pending"]
            if not pending:
                meta["state"] = "completed"
                meta["cancel_requested"] = False
                _refresh_counts(meta, records)
                _write_json(_meta_path(job_id, settings), meta)
                _write_json(_records_path(job_id, settings), records)
                return

            batch = pending[: max(1, settings.batch_size)]
            batch_ids = {r["synthetic_id"] for r in batch}
            for r in records:
                if r["synthetic_id"] in batch_ids:
                    r["status"] = "processing"
                    r["message"] = ""
            _refresh_counts(meta, records)
            meta["state"] = "running"
            _write_json(_records_path(job_id, settings), records)
            _write_json(_meta_path(job_id, settings), meta)

        payload = build_azure_payload(batch, settings)
        try:
            response = await classify_batch(payload, settings)
            by_id = _index_azure_pos(response)
            with lock:
                records = _read_json(_records_path(job_id, settings))
                meta = _read_json(_meta_path(job_id, settings))
                for r in records:
                    if r["synthetic_id"] not in batch_ids:
                        continue
                    matched = by_id.get(int(r["synthetic_id"]))
                    if matched is None:
                        r["status"] = "failed"
                        r["message"] = "PO missing from Azure response"
                        r["azure_response"] = None
                    else:
                        r["status"] = "completed"
                        r["message"] = ""
                        r["azure_response"] = matched
                _refresh_counts(meta, records)
                _write_json(_records_path(job_id, settings), records)
                _write_json(_meta_path(job_id, settings), meta)
        except Exception as exc:  # noqa: BLE001 — isolate batch failures
            # RuntimeError from azure_client is already short; keep others short too.
            msg = str(exc) if isinstance(exc, RuntimeError) else "Azure classification failed"
            logger.exception("Batch failed for job %s: %s", job_id, exc)
            with lock:
                records = _read_json(_records_path(job_id, settings))
                meta = _read_json(_meta_path(job_id, settings))
                for r in records:
                    if r["synthetic_id"] in batch_ids:
                        r["status"] = "failed"
                        r["message"] = msg
                        r["azure_response"] = None
                _refresh_counts(meta, records)
                _write_json(_records_path(job_id, settings), records)
                _write_json(_meta_path(job_id, settings), meta)


def _index_azure_pos(response: dict[str, Any]) -> dict[int, Any]:
    data = response.get("Data")
    if data is None and isinstance(response.get("data"), list):
        data = response["data"]
    if not isinstance(data, list):
        # Single-PO shape fallback
        if "purchaseorderid" in response:
            data = [response]
        else:
            data = []
    indexed: dict[int, Any] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        pid = item.get("purchaseorderid")
        try:
            indexed[int(pid)] = item
        except (TypeError, ValueError):
            continue
    return indexed
