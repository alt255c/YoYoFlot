"""T05: streaming semicolon CSV with physical line locators."""

import csv

from .common import Emitter

EXPECTED_COLUMNS = [
    "PassengerFirstName",
    "PassengerSecondName",
    "PassengerLastName",
    "PassengerSex",
    "PassengerBirthDate",
    "PassengerDocument",
    "BookingCode",
    "TicketNumber",
    "Baggage",
    "FlightDate",
    "FlightTime",
    "FlightNumber",
    "CodeShare",
    "Destination",
]


def read(
    path, source_id="boarding_csv", batch_size=1000, encoding="utf-8-sig", **kwargs
):
    e = Emitter(path, source_id, batch_size)
    with open(path, encoding=encoding, newline="") as f:
        reader = csv.reader(f, delimiter=";", strict=True)
        header = next(reader, None)
        if header != EXPECTED_COLUMNS:
            raise ValueError(f"Unexpected CSV header: {header!r}")
        while True:
            start = reader.line_num + 1
            try:
                values = next(reader)
            except StopIteration:
                break
            except csv.Error as exc:
                # Quoting corruption cannot be recovered without a reliable record boundary.
                raise ValueError(f"CSV syntax error at line {start}: {exc}") from exc
            e.mark("input_rows")
            locator = f"line:{start}-{reader.line_num}"
            if not values or not any(v.strip() for v in values):
                e.error(locator, values, "blank_row", "info")
            elif len(values) != len(header):
                e.error(locator, values, f"column_count:{len(values)}")
            else:
                obj = dict(zip(header, values))
                oid = e.person(locator, obj, birth_date_format="%m/%d/%Y")
                e.identifier(
                    locator + "/document",
                    obj,
                    oid,
                    "document",
                    obj["PassengerDocument"],
                )
                e.identifier(
                    locator + "/ticket", obj, oid, "ticket", obj["TicketNumber"]
                )
                e.travel(locator + "/travel", obj, oid, "boarding_record")
            yield from e.flush()
    yield from e.flush(True)
