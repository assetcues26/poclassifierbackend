"""Excel parsing and ERP → Azure payload transform."""

from __future__ import annotations

import copy
import json
from typing import Any

from openpyxl import load_workbook

from app.config import Settings


def parse_excel(file_path: str | Any, settings: Settings) -> list[dict[str, Any]]:
    """
    Read Excel and return one record per row.

    Each record:
      row_number, po_number, erp (overridden), status, message, synthetic_id
    """
    wb = load_workbook(file_path, read_only=True, data_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    try:
        header = next(rows)
    except StopIteration:
        wb.close()
        raise ValueError("Excel sheet is empty.")

    header_map = {
        str(cell).strip(): idx for idx, cell in enumerate(header) if cell is not None
    }
    po_col = settings.excel_po_column
    json_col = settings.excel_json_column
    if po_col not in header_map or json_col not in header_map:
        wb.close()
        raise ValueError(
            f"Excel must contain columns '{po_col}' and '{json_col}'. "
            f"Found: {list(header_map.keys())}"
        )

    po_idx = header_map[po_col]
    json_idx = header_map[json_col]
    records: list[dict[str, Any]] = []
    synthetic_id = 0

    for excel_row_number, row in enumerate(rows, start=2):
        if row is None or all(cell is None or str(cell).strip() == "" for cell in row):
            continue

        po_raw = row[po_idx] if po_idx < len(row) else None
        json_raw = row[json_idx] if json_idx < len(row) else None
        po_number = "" if po_raw is None else str(po_raw).strip()

        synthetic_id += 1
        record: dict[str, Any] = {
            "row_number": excel_row_number,
            "synthetic_id": synthetic_id,
            "po_number": po_number,
            "erp": None,
            "status": "pending",
            "message": "",
            "azure_response": None,
        }

        if not po_number:
            record["status"] = "invalid"
            record["message"] = "No PO number present"
            records.append(record)
            continue

        if json_raw is None or str(json_raw).strip() == "":
            record["status"] = "invalid"
            record["message"] = "ERP JSON is empty"
            records.append(record)
            continue

        try:
            erp = json.loads(str(json_raw))
        except json.JSONDecodeError:
            record["status"] = "invalid"
            record["message"] = "Invalid ERP JSON"
            records.append(record)
            continue

        if not isinstance(erp, dict):
            record["status"] = "invalid"
            record["message"] = "ERP JSON must be an object"
            records.append(record)
            continue

        erp = apply_overrides(erp, settings)
        lines = erp.get("lines")
        if not isinstance(lines, list) or len(lines) == 0:
            record["erp"] = erp
            record["status"] = "skipped"
            record["message"] = "No line items to classify"
            records.append(record)
            continue

        # Validate lines have usable polinenumber / item fields for API
        bad_line = _first_bad_line(lines)
        if bad_line is not None:
            record["erp"] = erp
            record["status"] = "invalid"
            record["message"] = bad_line
            records.append(record)
            continue

        record["erp"] = erp
        record["status"] = "pending"
        records.append(record)

    wb.close()
    if not records:
        raise ValueError("No data rows found in Excel.")
    max_rows = settings.max_excel_po_rows
    if len(records) > max_rows:
        raise ValueError(
            f"Excel has {len(records)} purchase orders (header row excluded). "
            f"Maximum allowed is {max_rows}. Please upload a smaller file."
        )
    return records


def apply_overrides(erp: dict[str, Any], settings: Settings) -> dict[str, Any]:
    """Deep-copy ERP JSON and apply the three configured overrides."""
    data = copy.deepcopy(erp)
    data["locationcode"] = settings.override_locationcode
    data["companyname"] = settings.override_companyname
    data["companycode"] = settings.override_companycode
    return data


def usable_customerid(value: Any) -> int | None:
    """
    Return a usable int customerid for Azure/Pinecone, or None.

    Matches classifier rules: None / blank / 0 / non-numeric / bool → unusable.
    Numeric strings like \"42\" parse to 42.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value != 0 else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = int(text)
        except ValueError:
            return None
        return parsed if parsed != 0 else None
    return None


def resolve_customerid_for_po(erp: dict[str, Any], settings: Settings) -> int | None:
    """Prefer usable ERP customerid; else env OVERRIDE_CUSTOMERID; else None (omit)."""
    from_data = usable_customerid(erp.get("customerid"))
    if from_data is not None:
        return from_data
    return usable_customerid(settings.override_customerid)


def _first_bad_line(lines: list[Any]) -> str | None:
    for idx, line in enumerate(lines):
        if not isinstance(line, dict):
            return f"Line {idx + 1}: must be an object"
        line_id = line.get("polinenumber", line.get("polineid"))
        if line_id is None:
            return f"Line {idx + 1}: missing polinenumber"
        try:
            int(line_id)
        except (TypeError, ValueError):
            return f"Line {idx + 1}: polinenumber must be an integer"
    return None


def build_azure_payload(
    records: list[dict[str, Any]],
    settings: Settings,
) -> dict[str, Any]:
    """Build {Data: [...]} for a batch of valid pending/retry records."""
    data: list[dict[str, Any]] = []
    for rec in records:
        erp = rec["erp"] or {}
        lines_out = []
        for line in erp.get("lines") or []:
            line_id = line.get("polinenumber", line.get("polineid"))
            lines_out.append(
                {
                    "polineid": int(line_id),
                    "itemname": str(line.get("itemname") or ""),
                    "description": str(line.get("description") or ""),
                }
            )
        po_string = rec["po_number"] or erp.get("purchaseordernumber") or ""
        po_obj: dict[str, Any] = {
            "purchaseorderid": int(rec["synthetic_id"]),
            "purchaseordernumber": str(po_string),
            "vendorname": erp.get("vendorname"),
            "lines": lines_out,
        }
        customerid = resolve_customerid_for_po(erp, settings)
        if customerid is not None:
            po_obj["customerid"] = customerid
        data.append(po_obj)
    return {"Data": data}
