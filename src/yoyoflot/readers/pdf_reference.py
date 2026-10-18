"""Retain the user's successful v1 output and compare actual data, not new IDs."""

import csv
import json
import shutil
from pathlib import Path


FIELDS = [
    "flight_number",
    "origin",
    "destination",
    "departure_local",
    "arrival_local",
    "arrival_day_offset",
    "days_of_week",
    "valid_from",
    "valid_to",
    "section_source",
    "origin_city",
    "origin_country",
    "dest_city",
    "dest_country",
    "validity_raw",
    "days_raw",
    "aircraft",
    "travel_time",
    "operated_by",
    "codeshare_mark",
]


def capture(out, input_sha256):
    out = Path(out)
    reference = out.with_name(out.name + ".baseline_v1")
    if (reference / "manifest.json").is_file():
        return reference
    old_report = out / "source_report.json"
    if not old_report.is_file() or not (out / "schedules.parquet").is_file():
        return None
    metadata = json.loads(old_report.read_text(encoding="utf-8"))
    if metadata.get("sha256") or metadata.get("contract_version"):
        return None  # A new-format run is not the user's original reference.
    reference.mkdir(exist_ok=True)
    for name in ["schedules.parquet", "source_report.json", "page_coverage.csv"]:
        source = out / name
        if source.is_file():
            temporary = reference / (name + ".tmp")
            shutil.copy2(source, temporary)
            temporary.replace(reference / name)
    (reference / "manifest.json").write_text(
        json.dumps(
            {
                "current_input_sha256_at_capture": input_sha256,
                "original_input_sha256_known": False,
                "note": "v1 did not store the input hash; compare only when this is the user's same PDF",
                "reference_rows": metadata.get("rows"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return reference


def compare(reference, output, input_sha256):
    import duckdb

    reference, output = Path(reference), Path(output)
    manifest = json.loads((reference / "manifest.json").read_text(encoding="utf-8"))
    if manifest["current_input_sha256_at_capture"] != input_sha256:
        return {
            "status": "not_applicable",
            "reason": "PDF changed since reference capture",
        }
    with duckdb.connect() as db:
        db.execute("SET memory_limit='512MB'")
        db.execute("SET threads=2")
        db.from_parquet(str(reference / "schedules.parquet")).create_view("old_raw")
        db.from_parquet(str(output / "schedules.parquet")).create_view("new_raw")

        def values():
            return ",".join(
                f"NULLIF(NULLIF(CAST({field} AS VARCHAR),'None'),'') AS {field}"
                for field in FIELDS
            )

        db.execute(
            "CREATE VIEW old_data AS SELECT CAST(source_page AS BIGINT) AS page, "
            "CAST(regexp_extract(schedule_id,'TT-[0-9]+-([0-9]+)',1) AS BIGINT)+1 AS ordinal, "
            + values()
            + " FROM old_raw"
        )
        db.execute(
            "CREATE VIEW new_data AS SELECT CAST(source_page AS BIGINT) AS page, "
            "CAST(regexp_extract(locator,'schedule:([0-9]+)$',1) AS BIGINT) AS ordinal, "
            + values()
            + " FROM new_raw"
        )
        for table in ["old_data", "new_data"]:
            invalid = db.sql(
                f"SELECT count(*) FROM (SELECT page,ordinal FROM {table} GROUP BY ALL HAVING count(*)<>1 OR page IS NULL OR ordinal IS NULL)"
            ).fetchone()[0]
            if invalid:
                raise ValueError(f"Invalid reference row keys: {table}")
        old_count = db.sql("SELECT count(*) FROM old_data").fetchone()[0]
        new_count = db.sql("SELECT count(*) FROM new_data").fetchone()[0]
        missing = db.sql(
            "SELECT count(*) FROM old_data o LEFT JOIN new_data n USING(page,ordinal) WHERE n.page IS NULL"
        ).fetchone()[0]
        added = db.sql(
            "SELECT count(*) FROM new_data n LEFT JOIN old_data o USING(page,ordinal) WHERE o.page IS NULL"
        ).fetchone()[0]
        expressions = [
            f"count(*) FILTER (WHERE o.{field} IS DISTINCT FROM n.{field})"
            for field in FIELDS
        ]
        differences = dict(
            zip(
                FIELDS,
                db.sql(
                    "SELECT "
                    + ",".join(expressions)
                    + " FROM old_data o JOIN new_data n USING(page,ordinal)"
                ).fetchone(),
            )
        )
        conditions = " OR ".join(
            f"o.{field} IS DISTINCT FROM n.{field}" for field in FIELDS
        )
        sample = db.sql(
            "SELECT o.page,o.ordinal,o.flight_number AS old_flight,n.flight_number AS new_flight FROM old_data o FULL JOIN new_data n USING(page,ordinal) WHERE o.page IS NULL OR n.page IS NULL OR "
            + conditions
            + " LIMIT 100"
        ).fetchall()
    with (output / "baseline_differences_sample.csv").open(
        "w", encoding="utf-8", newline=""
    ) as stream:
        writer = csv.writer(stream)
        writer.writerow(["page", "ordinal", "old_flight", "new_flight"])
        writer.writerows(sample)
    result = {
        "status": "matched"
        if missing == 0 and not any(differences.values())
        else "differences",
        "reference": str(reference),
        "old_rows": old_count,
        "new_rows": new_count,
        "missing_legacy_rows": missing,
        "added_rows": added,
        "field_differences": differences,
        "original_input_sha256_known": False,
        "excluded_from_comparison": [
            "new identifiers/provenance",
            "parse_status quality flags",
            "raw_line whitespace",
        ],
        "same_pdf_requires_user_confirmation": True,
    }
    (output / "baseline_comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result
