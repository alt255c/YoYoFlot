from dataclasses import dataclass, field
from typing import Any

import pyarrow as pa

CONTRACT_VERSION = "1.0"
BASE = {
    "source_id": pa.string(),
    "source_record_id": pa.string(),
    "locator": pa.string(),
    "raw_json": pa.string(),
}


def schema(fields):
    required = {
        "source_id",
        "source_record_id",
        "locator",
        "observation_id",
        "identifier_id",
        "travel_id",
        "schedule_id",
        "error_id",
        "entity_table",
        "entity_id",
        "transformation",
    }
    if "kind" in fields:
        required.update({"kind", "value"})
    if "reason" in fields:
        required.update({"reason", "severity"})
    return pa.schema(
        [
            pa.field(k, t, nullable=k not in required)
            for k, t in {**BASE, **fields}.items()
        ],
        metadata={b"contract_version": CONTRACT_VERSION.encode()},
    )


SCHEMAS = {
    "person_observations": schema(
        {
            "observation_id": pa.string(),
            "name_raw": pa.string(),
            "name_normalized": pa.string(),
            "birth_date_raw": pa.string(),
            "birth_date": pa.string(),
            "sex": pa.string(),
            "nickname": pa.string(),
            "external_id": pa.string(),
        }
    ),
    "identifiers": schema(
        {
            "identifier_id": pa.string(),
            "observation_id": pa.string(),
            "kind": pa.string(),
            "programme": pa.string(),
            "value": pa.string(),
        }
    ),
    "travel_observations": schema(
        {
            "travel_id": pa.string(),
            "observation_id": pa.string(),
            "event_kind": pa.string(),
            "loyalty_identifier_id": pa.string(),
            "flight_number": pa.string(),
            "origin_raw": pa.string(),
            "destination_raw": pa.string(),
            "departure_raw": pa.string(),
            "departure_local": pa.string(),
            "departure_timezone": pa.string(),
            "departure_precision": pa.string(),
            "arrival_raw": pa.string(),
            "arrival_local": pa.string(),
            "arrival_timezone": pa.string(),
            "arrival_precision": pa.string(),
            "ticket_number": pa.string(),
            "booking_code": pa.string(),
            "codeshare": pa.string(),
            "status_raw": pa.string(),
            "fare_raw": pa.string(),
        }
    ),
    "schedules": schema(
        {
            "schedule_id": pa.string(),
            "flight_number": pa.string(),
            "origin": pa.string(),
            "destination": pa.string(),
            "departure_local": pa.string(),
            "arrival_local": pa.string(),
            "arrival_day_offset": pa.int64(),
            "days_of_week": pa.string(),
            "valid_from": pa.string(),
            "valid_to": pa.string(),
            "source_page": pa.int64(),
            "parse_status": pa.string(),
            "section_source": pa.string(),
            "origin_city": pa.string(),
            "origin_country": pa.string(),
            "dest_city": pa.string(),
            "dest_country": pa.string(),
            "validity_raw": pa.string(),
            "days_raw": pa.string(),
            "aircraft": pa.string(),
            "travel_time": pa.string(),
            "operated_by": pa.string(),
            "codeshare_mark": pa.bool_(),
            "raw_line": pa.string(),
        }
    ),
    "provenance": schema(
        {
            "entity_table": pa.string(),
            "entity_id": pa.string(),
            "transformation": pa.string(),
        }
    ),
    "errors": schema(
        {"error_id": pa.string(), "reason": pa.string(), "severity": pa.string()}
    ),
}
ID_COLUMNS = {
    "person_observations": "observation_id",
    "identifiers": "identifier_id",
    "travel_observations": "travel_id",
    "schedules": "schedule_id",
    "errors": "error_id",
}


@dataclass
class TableBatch:
    table_name: str
    records: list[dict[str, Any]]
    counters: dict[str, int] = field(default_factory=dict)

    def to_arrow(self):
        s = SCHEMAS[self.table_name]
        known_fields = set(s.names)
        required_fields = tuple(f.name for f in s if not f.nullable)
        string_fields = tuple(f.name for f in s if pa.types.is_string(f.type))
        for row in self.records:
            unknown = row.keys() - known_fields
            if unknown:
                raise ValueError(f"Unknown fields in {self.table_name}: {unknown}")
            for k in ("source_id", "source_record_id", "locator"):
                if not isinstance(row.get(k), str) or not row[k]:
                    raise ValueError(f"Required string {k}")
            for name in required_fields:
                if row.get(name) is None:
                    raise ValueError(f"Required field {name}")
            if self.table_name in ID_COLUMNS and not row.get(
                ID_COLUMNS[self.table_name]
            ):
                raise ValueError(f"Missing primary key for {self.table_name}")
            if self.table_name in {
                "identifiers",
                "travel_observations",
            } and not row.get("observation_id"):
                raise ValueError("Missing observation_id")
            if (
                self.table_name == "provenance"
                and row["entity_table"] not in ID_COLUMNS
            ):
                raise ValueError("Unknown provenance target table")
            if self.table_name == "errors" and row["severity"] not in {
                "info",
                "warning",
                "error",
            }:
                raise ValueError("Unknown error severity")
            for name in string_fields:
                if row.get(name) is not None and not isinstance(row[name], str):
                    raise TypeError(f"{name} must remain a string")
        return pa.Table.from_pylist(self.records, schema=s)
