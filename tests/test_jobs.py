"""Job store helpers — disk only, Azure never called."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app import jobs as job_service


def _write_excel(path: Path, po: str, erp: dict) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["Purchase Order Number", "ERP JSON"])
    ws.append([po, json.dumps(erp)])
    wb.save(path)


def _erp(po: str = "PO-1") -> dict:
    return {
        "purchaseordernumber": po,
        "vendorname": "Acme",
        "lines": [
            {"polinenumber": 1, "itemname": "Desk", "description": "Oak"},
        ],
    }


def test_create_job_from_excel(settings, tmp_path):
    path = tmp_path / "one.xlsx"
    _write_excel(path, "PO-9", _erp("PO-9"))
    summary = job_service.create_job_from_excel(path, "one.xlsx")
    assert summary["state"] == "ready"
    assert summary["total"] == 1
    assert summary["pending"] == 1
    assert summary["items"][0]["po_number"] == "PO-9"


def test_create_job_from_results_view_only(settings):
    payload = [
        {
            "purchase_order_number": "PO-1",
            "status": "completed",
            "message": "",
            "erp_json": _erp("PO-1"),
            "azure_output": {"purchaseordernumber": "PO-1", "lines": []},
        }
    ]
    summary = job_service.create_job_from_results(payload, "results.json")
    assert summary["state"] == "view_only"
    assert summary["mode"] == "view"
    assert summary["completed"] == 1


def test_create_job_from_results_rejects_raw_download(settings):
    with pytest.raises(ValueError, match="erp_json"):
        job_service.create_job_from_results(
            [{"purchase_order_number": "PO-1", "azure_output": {}}],
            "raw.json",
        )


def test_download_strips_synthetic_ids(settings, tmp_path):
    path = tmp_path / "one.xlsx"
    _write_excel(path, "PO-2", _erp("PO-2"))
    summary = job_service.create_job_from_excel(path, "one.xlsx")
    job_id = summary["job_id"]

    # Simulate a completed record with a synthetic purchaseorderid.
    records_path = settings.jobs_path / job_id / "records.json"
    records = json.loads(records_path.read_text(encoding="utf-8"))
    records[0]["status"] = "completed"
    records[0]["azure_response"] = {
        "purchaseorderid": 1,
        "purchaseordernumber": "PO-2",
        "lines": [],
    }
    records_path.write_text(json.dumps(records), encoding="utf-8")

    download = job_service.build_download_payload(job_id)
    assert "purchaseorderid" not in download[0]["azure_output"]
    assert download[0]["erp_json"] is not None


def test_delete_job(settings, tmp_path):
    path = tmp_path / "one.xlsx"
    _write_excel(path, "PO-3", _erp("PO-3"))
    summary = job_service.create_job_from_excel(path, "one.xlsx")
    job_id = summary["job_id"]
    job_service.delete_job(job_id)
    with pytest.raises(FileNotFoundError):
        job_service.summarize_job(job_id)


def test_recover_stuck_jobs(settings, tmp_path):
    path = tmp_path / "one.xlsx"
    _write_excel(path, "PO-4", _erp("PO-4"))
    summary = job_service.create_job_from_excel(path, "one.xlsx")
    job_id = summary["job_id"]

    meta_path = settings.jobs_path / job_id / "meta.json"
    records_path = settings.jobs_path / job_id / "records.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    records = json.loads(records_path.read_text(encoding="utf-8"))
    meta["state"] = "running"
    records[0]["status"] = "processing"
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    records_path.write_text(json.dumps(records), encoding="utf-8")

    fixed = job_service.recover_stuck_jobs()
    assert fixed == 1
    recovered = job_service.summarize_job(job_id)
    assert recovered["state"] != "running"
    assert recovered["failed"] == 1
