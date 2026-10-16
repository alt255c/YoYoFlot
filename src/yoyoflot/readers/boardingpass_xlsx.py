"""T10: ZIP/OOXML read-only streams, real boarding-card and tabular layouts."""

import csv
import hashlib
import json
import re
import shutil
import tempfile
from pathlib import Path
from zipfile import ZipFile
from openpyxl.utils.cell import column_index_from_string
from .common import Emitter, split_card
from .xlsx_stream import WorkbookStream

NAME_FIELDS = {"PassengerName", "PaxName", "PassengerFirstName", "Real Name", "Name"}
TRAVEL_FIELDS = {
    "FlightNumber",
    "Flight",
    "FlightDate",
    "DepartDate",
    "Date",
    "TicketNumber",
    "e-Ticket",
}
IDENTIFIER_FIELDS = {
    "PassengerDocument",
    "TravelDoc",
    "TicketNumber",
    "e-Ticket",
    "BookingCode",
    "CardNumber",
}
CARD_LABELS = {
    "A1": "BOARDING PASS",
    "F1": "SEQUENCE:",
    "A7": "GATE",
    "F7": "->",
    "G11": "SEAT",
    "A13": "PNR:",
    "D13": "E-TICKET",
}
CARD_FIELDS = {
    "PassengerName": "B3",
    "FlightNumber": "A5",
    "FlightDate": "A9",
    "FlightTime": "C9",
    "From": "D7",
    "Destination": "H7",
    "BookingCode": "B13",
    "TicketNumber": "E13",
}


def _payload(cells):
    return {
        coord: {
            "value": cell.value,
            "type": cell.data_type,
            "formula": cell.formula,
            "xml_value": cell.xml_value,
        }
        for coord, cell in cells.items()
    }


def _values(cells):
    if not cells:
        return []
    width = max(
        column_index_from_string(re.match(r"[A-Z]+", coord)[0]) for coord in cells
    )
    result = [None] * width
    for coord, cell in cells.items():
        result[column_index_from_string(re.match(r"[A-Z]+", coord)[0]) - 1] = cell.value
    return result


def _emit_record(e, loc, obj, cells):
    # OOXML supplies cached results and formula text in a single read.
    for field, cell in cells.items():
        if cell.formula is not None and cell.value is None:
            e.error(loc + "/" + field, cell.formula, "formula_requires_cached_value")
        if cell.data_type == "e":
            e.error(loc + "/" + field, cell.xml_value, "excel_error_cell")
        if field in IDENTIFIER_FIELDS and isinstance(obj.get(field), (int, float)):
            e.error(
                loc + "/" + field,
                {"value": obj[field], "xml_value": cell.xml_value},
                "numeric_identifier_original_digits_unavailable",
                "warning",
            )
            obj[field] = (
                str(obj[field])
                if isinstance(obj[field], int)
                else format(obj[field], ".17g")
            )
    oid = e.person(loc, obj)
    e.identifier(
        loc + "/document",
        obj,
        oid,
        "document",
        obj.get("PassengerDocument", obj.get("TravelDoc")),
    )
    e.identifier(
        loc + "/ticket",
        obj,
        oid,
        "ticket",
        obj.get("TicketNumber", obj.get("e-Ticket")),
    )
    card_id = None
    if obj.get("_loyalty_card"):
        programme, number = split_card(obj["_loyalty_card"])
        if programme is None:
            e.error(
                loc + "/loyalty",
                obj["_loyalty_card"],
                "card_without_programme_or_number",
            )
        else:
            card_id = e.identifier(
                loc + "/loyalty",
                obj["_loyalty_card"],
                oid,
                "loyalty_card",
                number,
                programme,
            )
    e.travel(
        loc + "/travel", obj, oid, "boarding_pass_record", loyalty_identifier_id=card_id
    )


def read(
    path, source_id="boardingpass_xlsx", batch_size=1000, coverage_path=None, **kwargs
):
    e = Emitter(path, source_id, batch_size)
    coverage_file = (
        open(coverage_path, "w", encoding="utf-8", newline="")
        if coverage_path
        else None
    )
    writer = (
        csv.DictWriter(
            coverage_file,
            fieldnames=[
                "workbook",
                "sheet",
                "header_version",
                "input_rows",
                "accepted_rows",
                "errors",
                "status",
                "reason",
            ],
        )
        if coverage_file
        else None
    )
    if writer:
        writer.writeheader()

    def cover(**row):
        if writer:
            writer.writerow(row)
            coverage_file.flush()

    try:
        with ZipFile(path) as archive:
            seen = set()
            for member in archive.infolist():
                if member.is_dir():
                    continue
                if member.filename in seen:
                    raise ValueError(f"Duplicate ZIP member {member.filename}")
                seen.add(member.filename)
                if not member.filename.lower().endswith(".xlsx"):
                    cover(
                        workbook=member.filename,
                        sheet="",
                        header_version="",
                        input_rows=0,
                        accepted_rows=0,
                        errors=0,
                        status="excluded",
                        reason="non_xlsx_member",
                    )
                    continue
                e.mark("workbooks")
                with archive.open(member) as src, tempfile.TemporaryFile() as tmp:
                    shutil.copyfileobj(src, tmp, 1024 * 1024)
                    tmp.seek(0)
                    try:
                        book = WorkbookStream(tmp)
                    except Exception as exc:
                        e.error(member.filename, str(exc), "unreadable_workbook")
                        cover(
                            workbook=member.filename,
                            sheet="",
                            header_version="",
                            input_rows=0,
                            accepted_rows=0,
                            errors=1,
                            status="excluded",
                            reason="unreadable_workbook",
                        )
                        yield from e.flush()
                        continue
                    try:
                        for sheet, sheet_path in book.sheets:
                            e.mark("sheets")
                            start_errors = e.counts.get("errors", 0)
                            prefix = f"{Path(path).name}/{member.filename}/{sheet}"
                            rows = iter(book.rows(sheet_path))
                            first = None
                            for rn, cells in rows:
                                if any(
                                    c.value is not None or c.formula is not None
                                    for c in cells.values()
                                ):
                                    first = (rn, cells)
                                    break
                            if first is None:
                                cover(
                                    workbook=member.filename,
                                    sheet=sheet,
                                    header_version="",
                                    input_rows=0,
                                    accepted_rows=0,
                                    errors=0,
                                    status="empty",
                                    reason="",
                                )
                                continue
                            rn, cells = first
                            if (
                                cells.get("A1") is not None
                                and cells["A1"].value == "BOARDING PASS"
                            ):
                                all_cells = dict(cells)
                                for _, more in rows:
                                    all_cells.update(more)
                                if len(all_cells) > 10000:
                                    raise ValueError(
                                        "Boarding card sheet exceeds cell budget"
                                    )
                                missing = [
                                    coord
                                    for coord, value in CARD_LABELS.items()
                                    if coord not in all_cells
                                    or all_cells[coord].value != value
                                ]
                                extra_rows = [
                                    coord
                                    for coord in all_cells
                                    if int(re.search(r"\d+$", coord)[0]) > 13
                                    and all_cells[coord].value is not None
                                ]
                                accepted = 0
                                if missing or extra_rows:
                                    e.error(
                                        prefix + "/row:1",
                                        _payload(all_cells),
                                        "unsupported_boarding_card_layout",
                                    )
                                else:
                                    obj = {
                                        field: all_cells[coord].value
                                        if coord in all_cells
                                        else None
                                        for field, coord in CARD_FIELDS.items()
                                    }
                                    obj["_raw_cells"] = _payload(all_cells)
                                    obj["_loyalty_card"] = (
                                        all_cells["F3"].value
                                        if "F3" in all_cells
                                        else None
                                    )
                                    obj["_origin_city"] = (
                                        all_cells["D5"].value
                                        if "D5" in all_cells
                                        else None
                                    )
                                    obj["_destination_city"] = (
                                        all_cells["H5"].value
                                        if "H5" in all_cells
                                        else None
                                    )
                                    mapped = {
                                        field: all_cells[coord]
                                        for field, coord in CARD_FIELDS.items()
                                        if coord in all_cells
                                    }
                                    _emit_record(e, prefix + "/row:3", obj, mapped)
                                    accepted = 1
                                e.mark("input_rows")
                                e.mark("boarding_card_sheets")
                                errors = e.counts.get("errors", 0) - start_errors
                                cover(
                                    workbook=member.filename,
                                    sheet=sheet,
                                    header_version="boarding_card_v1",
                                    input_rows=1,
                                    accepted_rows=accepted,
                                    errors=errors,
                                    status="ok" if accepted else "excluded",
                                    reason=""
                                    if accepted
                                    else "unsupported_boarding_card_layout",
                                )
                                yield from e.flush()
                                continue
                            header = [
                                str(v).strip() if v is not None else ""
                                for v in _values(cells)
                            ]
                            reason = ""
                            if not all(header) or len(header) != len(set(header)):
                                reason = "duplicate_or_blank_headers"
                            elif not (
                                set(header) & NAME_FIELDS
                                and set(header) & TRAVEL_FIELDS
                            ):
                                reason = "unsupported_header"
                            if reason:
                                e.error(prefix + f"/row:{rn}", _payload(cells), reason)
                            version = (
                                hashlib.sha256(json.dumps(header).encode()).hexdigest()[
                                    :12
                                ]
                                if not reason
                                else ""
                            )
                            input_rows = accepted = 0
                            for row_number, cells in rows:
                                values = _values(cells)
                                if not any(
                                    c.value is not None or c.formula is not None
                                    for c in cells.values()
                                ):
                                    continue
                                if [
                                    str(v).strip() if v is not None else ""
                                    for v in values
                                ] == header:
                                    continue
                                input_rows += 1
                                e.mark("input_rows")
                                if reason:
                                    e.mark("excluded_rows")
                                    continue
                                # Missing trailing cells are null, not a changed header.
                                values += [None] * max(0, len(header) - len(values))
                                loc = prefix + f"/row:{row_number}"
                                if len(values) > len(header):
                                    e.error(loc, _payload(cells), "column_count")
                                    continue
                                obj = dict(zip(header, values))
                                obj["_raw_cells"] = _payload(cells)
                                mapped = {
                                    header[
                                        column_index_from_string(
                                            re.match(r"[A-Z]+", coord)[0]
                                        )
                                        - 1
                                    ]: cell
                                    for coord, cell in cells.items()
                                }
                                _emit_record(e, loc, obj, mapped)
                                accepted += 1
                                yield from e.flush()
                            cover(
                                workbook=member.filename,
                                sheet=sheet,
                                header_version=version,
                                input_rows=input_rows,
                                accepted_rows=accepted,
                                errors=e.counts.get("errors", 0) - start_errors,
                                status="excluded" if reason else "ok",
                                reason=reason,
                            )
                            yield from e.flush()
                    finally:
                        book.close()
    finally:
        if coverage_file:
            coverage_file.close()
    yield from e.flush(True)
