"""HTTP route tests via TestClient (Azure never called)."""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import Workbook

from tests.conftest import TEST_PASSWORD, TEST_USERNAME


def test_health_requires_auth(client):
    assert client.get("/api/health").status_code == 401


def test_login_and_me(client):
    bad = client.post(
        "/api/login",
        json={"username": TEST_USERNAME, "password": "wrong"},
    )
    assert bad.status_code == 401

    ok = client.post(
        "/api/login",
        json={"username": TEST_USERNAME, "password": TEST_PASSWORD},
    )
    assert ok.status_code == 200
    assert ok.json()["username"] == TEST_USERNAME
    assert "po_ui_session" in ok.cookies

    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["username"] == TEST_USERNAME


def test_logout_clears_session(auth_client):
    assert auth_client.get("/api/me").status_code == 200
    out = auth_client.post("/api/logout")
    assert out.status_code == 200
    # Cookie cleared — next protected call should fail.
    auth_client.cookies.clear()
    assert auth_client.get("/api/me").status_code == 401


def test_upload_excel_and_get_job(auth_client, tmp_path):
    path = tmp_path / "upload.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Purchase Order Number", "ERP JSON"])
    erp = {
        "purchaseordernumber": "PO-55",
        "vendorname": "Acme",
        "lines": [{"polinenumber": 1, "itemname": "Chair", "description": ""}],
    }
    ws.append(["PO-55", json.dumps(erp)])
    wb.save(path)

    with path.open("rb") as f:
        res = auth_client.post(
            "/api/jobs",
            files={
                "file": (
                    "upload.xlsx",
                    f,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["total"] == 1
    assert body["pending"] == 1

    job_id = body["job_id"]
    got = auth_client.get(f"/api/jobs/{job_id}")
    assert got.status_code == 200
    assert got.json()["items"][0]["po_number"] == "PO-55"


def test_reject_non_excel(auth_client):
    res = auth_client.post(
        "/api/jobs",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert res.status_code == 400
