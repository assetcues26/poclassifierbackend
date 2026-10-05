"""Excel parse + Azure payload shape (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from app.excel_service import (
    apply_overrides,
    build_azure_payload,
    parse_excel,
    usable_customerid,
)


def _write_excel(path: Path, rows: list[tuple]) -> None:
    wb = Workbook()
    ws = wb.active
    ws.append(["Purchase Order Number", "ERP JSON"])
    for row in rows:
        ws.append(list(row))
    wb.save(path)


def _valid_erp(po: str = "PO-1") -> dict:
    return {
        "purchaseordernumber": po,
        "vendorname": "Acme",
        "lines": [
            {"polinenumber": 1, "itemname": "Laptop", "description": "14 inch"},
        ],
    }


def test_parse_excel_valid_row(settings, tmp_path):
    path = tmp_path / "pos.xlsx"
    _write_excel(path, [("PO-100", json.dumps(_valid_erp("PO-100")))])
    records = parse_excel(path, settings)
    assert len(records) == 1
    assert records[0]["status"] == "pending"
    assert records[0]["po_number"] == "PO-100"
    assert records[0]["erp"]["locationcode"] == settings.override_locationcode


def test_parse_excel_invalid_json(settings, tmp_path):
    path = tmp_path / "bad.xlsx"
    _write_excel(path, [("PO-1", "{not-json")])
    records = parse_excel(path, settings)
    assert records[0]["status"] == "invalid"
    assert "Invalid ERP JSON" in records[0]["message"]


def test_parse_excel_missing_po(settings, tmp_path):
    path = tmp_path / "nop.xlsx"
    _write_excel(path, [("", json.dumps(_valid_erp()))])
    records = parse_excel(path, settings)
    assert records[0]["status"] == "invalid"
    assert "No PO number" in records[0]["message"]


def test_parse_excel_rejects_over_max_rows(settings, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "max_excel_po_rows", 2)
    path = tmp_path / "many.xlsx"
    rows = [(f"PO-{i}", json.dumps(_valid_erp(f"PO-{i}"))) for i in range(3)]
    _write_excel(path, rows)
    with pytest.raises(ValueError, match="Maximum allowed"):
        parse_excel(path, settings)


def test_apply_overrides(settings):
    erp = apply_overrides({"locationcode": "old", "lines": []}, settings)
    assert erp["locationcode"] == settings.override_locationcode
    assert erp["companyname"] == settings.override_companyname
    assert erp["companycode"] == settings.override_companycode


def test_usable_customerid():
    assert usable_customerid(42) == 42
    assert usable_customerid("42") == 42
    assert usable_customerid(" 7 ") == 7
    assert usable_customerid(0) is None
    assert usable_customerid("0") is None
    assert usable_customerid(None) is None
    assert usable_customerid("") is None
    assert usable_customerid("ABC") is None
    assert usable_customerid(True) is None


def test_build_azure_payload_uses_erp_customerid(settings, monkeypatch):
    monkeypatch.setattr(settings, "override_customerid", 1063)
    erp = _valid_erp("PO-7")
    erp["customerid"] = 42
    records = [{"synthetic_id": 7, "po_number": "PO-7", "erp": erp}]
    payload = build_azure_payload(records, settings)
    assert payload["Data"][0]["purchaseorderid"] == 7
    assert payload["Data"][0]["purchaseordernumber"] == "PO-7"
    assert payload["Data"][0]["customerid"] == 42
    assert payload["Data"][0]["lines"][0]["polineid"] == 1


def test_build_azure_payload_falls_back_to_override(settings, monkeypatch):
    monkeypatch.setattr(settings, "override_customerid", 1063)
    records = [
        {"synthetic_id": 7, "po_number": "PO-7", "erp": _valid_erp("PO-7")},
    ]
    payload = build_azure_payload(records, settings)
    assert payload["Data"][0]["customerid"] == 1063


def test_build_azure_payload_omits_customerid_when_unusable(settings, monkeypatch):
    monkeypatch.setattr(settings, "override_customerid", None)
    erp = _valid_erp("PO-7")
    erp["customerid"] = "ABC"
    records = [{"synthetic_id": 7, "po_number": "PO-7", "erp": erp}]
    payload = build_azure_payload(records, settings)
    assert "customerid" not in payload["Data"][0]
