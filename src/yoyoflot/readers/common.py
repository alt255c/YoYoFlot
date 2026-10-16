"""Shared mapping helpers: conservative aliases, raw payload and explicit errors."""

import json
import re
from datetime import date, datetime

from ..contracts import ID_COLUMNS, TableBatch
from ..provenance import fingerprint, stable_id


def raw(value):
    return json.dumps(value, ensure_ascii=False, default=str)


def text(value):
    if value is None or value in ("", "None", "Not presented", "N/A"):
        return None
    if isinstance(value, (dict, list)):
        return raw(value)
    return str(value)


def pick(obj, *keys):
    for key in keys:
        if key in obj:
            return obj[key]
    return None


def split_card(value):
    """Split an explicitly present programme code, never infer it from an airline."""
    value = text(value)
    match = re.fullmatch(r"([A-Z]{2})\s+([0-9]+)", value or "")
    return match.groups() if match else (None, value)


def local_time(value):
    """No inferred timezone, no midnight for date-only values."""
    if isinstance(value, datetime):
        return value.isoformat(), "second" if value.second else "minute"
    if isinstance(value, date):
        return value.isoformat(), "date"
    s = text(value)
    if not s:
        return None, None
    for fmt, precision in [
        ("%Y-%m-%d", "date"),
        ("%d.%m.%Y", "date"),
        ("%d/%m/%Y", "date"),
        ("%Y/%m/%d", "date"),
        ("%Y-%m-%d %H:%M", "minute"),
        ("%d.%m.%Y %H:%M", "minute"),
        ("%Y-%m-%dT%H:%M", "minute"),
        ("%Y-%m-%dT%H:%M:%S", "second"),
        ("%Y-%m-%d %H:%M:%S", "second"),
    ]:
        try:
            d = datetime.strptime(s.strip(), fmt)
            return (
                d.date().isoformat()
                if precision == "date"
                else d.isoformat(
                    timespec="seconds" if precision == "second" else "minutes"
                )
            ), precision
        except ValueError:
            pass
    return None, "invalid"


class Emitter:
    def __init__(self, path, source_id, batch_size=1000):
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self.source_id, self.version = source_id, fingerprint(path)
        self.batch_size = batch_size
        self.buffers = {}
        self.buffer_bytes = {}
        self.counts = {}
        self.metrics = {}

    def base(self, locator, payload):
        return dict(
            source_id=self.source_id,
            source_record_id=stable_id(self.source_id, self.version, locator, "record"),
            locator=locator,
            raw_json=raw(payload),
        )

    def add(self, table, locator, payload, **fields):
        row = {**self.base(locator, payload), **fields}
        if table in ID_COLUMNS:
            row[ID_COLUMNS[table]] = stable_id(
                self.source_id, self.version, locator, table
            )
        self.buffers.setdefault(table, []).append(row)
        self.buffer_bytes[table] = self.buffer_bytes.get(table, 0) + len(
            row["raw_json"].encode("utf-8")
        )
        self.counts[table] = self.counts.get(table, 0) + 1
        if table in ID_COLUMNS:
            self.buffers.setdefault("provenance", []).append(
                {
                    **self.base(locator, None),
                    "entity_table": table,
                    "entity_id": row[ID_COLUMNS[table]],
                    "transformation": "reader:1.0",
                }
            )
        return row.get(ID_COLUMNS.get(table, ""))

    def error(self, locator, payload, reason, severity="error"):
        self.add("errors", locator, payload, reason=reason, severity=severity)

    def person(self, locator, obj, birth_date_format=None):
        name_value = pick(obj, "Real Name", "name", "Name", "PaxName", "PassengerName")
        if isinstance(name_value, dict):
            name_keys = (
                "first",
                "middle",
                "last",
                "First Name",
                "Middle Name",
                "Last Name",
            )
            name = (
                " ".join(str(name_value[k]) for k in name_keys if name_value.get(k))
                or None
            )
            if not set(name_keys).intersection(name_value):
                self.error(locator + "/name", name_value, "unsupported_name_structure")
        else:
            name = text(name_value)
        if not name:
            name = (
                " ".join(
                    str(obj[k])
                    for k in (
                        "PassengerLastName",
                        "PassengerFirstName",
                        "PassengerSecondName",
                    )
                    if obj.get(k)
                )
                or None
            )
        dob = text(
            pick(
                obj,
                "PassengerBirthDate",
                "PaxBirthDate",
                "BirthDate",
                "Date of Birth",
                "birth_date",
            )
        )
        parsed, precision = local_time(dob)
        if dob and birth_date_format and "/" in dob:
            try:
                parsed, precision = (
                    datetime.strptime(dob, birth_date_format).date().isoformat(),
                    "date",
                )
            except ValueError:
                parsed, precision = None, "invalid"
        if precision == "invalid":
            self.error(locator + "/birth_date", dob, "invalid_birth_date")
        return self.add(
            "person_observations",
            locator,
            obj,
            name_raw=name,
            name_normalized=" ".join(name.split()).casefold() if name else None,
            birth_date_raw=dob,
            birth_date=parsed,
            sex=text(pick(obj, "PassengerSex", "Sex", "sex")),
            nickname=text(pick(obj, "NickName", "nickname")),
            external_id=text(pick(obj, "id", "ID", "uid", "external_id")),
        )

    def identifier(self, locator, obj, observation_id, kind, value, programme=None):
        value = text(value)
        if value and kind == "loyalty_card":
            value = value.strip()
        if value:
            return self.add(
                "identifiers",
                locator,
                obj,
                observation_id=observation_id,
                kind=kind,
                value=value,
                programme=text(programme),
            )

    def travel(self, locator, obj, observation_id, event_kind, **extra_fields):
        depdate = pick(
            obj, "FlightDate", "DepartDate", "Date", "date", "departure_date"
        )
        deptime = pick(obj, "FlightTime", "DepartTime", "departure_time")
        arrdate = pick(obj, "ArrivalDate", "arrival_date")
        arrtime = pick(obj, "ArrivalTime", "arrival_time")
        dep = text(depdate)
        arr = text(arrdate)
        if dep and deptime:
            dep = dep + " " + str(deptime)
        if arr and arrtime:
            arr = arr + " " + str(arrtime)
        dl, dp = local_time(dep)
        al, ap = local_time(arr)
        for label, value, precision in [("departure", dep, dp), ("arrival", arr, ap)]:
            if precision == "invalid":
                self.error(locator + "/" + label, value, "invalid_" + label + "_date")
        return self.add(
            "travel_observations",
            locator,
            obj,
            observation_id=observation_id,
            event_kind=event_kind,
            flight_number=text(pick(obj, "FlightNumber", "Flight", "Code", "flight")),
            origin_raw=text(pick(obj, "From", "FROM", "Departure", "origin")),
            destination_raw=text(
                pick(obj, "Destination", "Dest", "TO", "Arrival", "destination")
            ),
            departure_raw=dep,
            departure_local=dl,
            departure_precision=dp,
            arrival_raw=arr,
            arrival_local=al,
            arrival_precision=ap,
            ticket_number=text(pick(obj, "TicketNumber", "e-Ticket", "Ticket")),
            booking_code=text(pick(obj, "BookingCode", "Booking")),
            codeshare=text(pick(obj, "CodeShare", "Codeshare", "CodeSh", "codeshare")),
            status_raw=text(pick(obj, "STATUS", "Status")),
            fare_raw=text(pick(obj, "Fare", "FARE", "fare")),
            **extra_fields,
        )

    def mark(self, label, count=1):
        self.metrics[label] = self.metrics.get(label, 0) + count

    def flush(self, force=False):
        for table, rows in self.buffers.items():
            if rows and (
                force
                or len(rows) >= self.batch_size
                or self.buffer_bytes.get(table, 0) >= 4 * 1024 * 1024
            ):
                while rows:
                    part = rows[: self.batch_size]
                    del rows[: self.batch_size]
                    yield TableBatch(table, part)
                self.buffer_bytes[table] = 0

        if force and self.metrics:
            yield TableBatch("provenance", [], counters=dict(self.metrics))
            self.metrics.clear()
