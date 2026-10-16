"""DuckDB structural checks on all Parquet tables; no in-memory passenger merge."""

import argparse
import json
from pathlib import Path

import duckdb
import pyarrow.parquet as pq
from yoyoflot.contracts import ID_COLUMNS, SCHEMAS


def validate(path):
    results = {}
    with duckdb.connect() as db:
        db.execute("SET memory_limit='512MB'")
        db.execute("SET threads=2")
        for name, schema in SCHEMAS.items():
            file = path / (name + ".parquet")
            assert pq.read_schema(file) == schema, f"Schema mismatch: {name}"
            db.from_parquet(str(file)).create_view(name)
            results[name] = db.sql(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            if name in ID_COLUMNS:
                key = ID_COLUMNS[name]
                duplicates = db.sql(
                    f'SELECT count(*) FROM (SELECT "{key}" FROM "{name}" GROUP BY "{key}" HAVING count(*)>1 OR "{key}" IS NULL)'
                ).fetchone()[0]
                assert duplicates == 0, f"Duplicate/null primary keys: {name}"
        for name in ["identifiers", "travel_observations"]:
            unmatched = db.sql(
                f'SELECT count(*) FROM "{name}" x LEFT JOIN person_observations p USING(observation_id) WHERE p.observation_id IS NULL'
            ).fetchone()[0]
            assert unmatched == 0, f"Unmatched people: {name}"
        unmatched = db.sql(
            "SELECT count(*) FROM travel_observations t LEFT JOIN identifiers i ON t.loyalty_identifier_id=i.identifier_id WHERE t.loyalty_identifier_id IS NOT NULL AND i.identifier_id IS NULL"
        ).fetchone()[0]
        assert unmatched == 0, "Unmatched loyalty identifiers"
        for name, key in ID_COLUMNS.items():
            unmatched = db.sql(
                f"SELECT count(*) FROM provenance p LEFT JOIN {name} x ON p.entity_id=x.{key} WHERE p.entity_table='{name}' AND x.{key} IS NULL"
            ).fetchone()[0]
            assert unmatched == 0, f"Unmatched provenance: {name}"
            missing = db.sql(
                f"SELECT count(*) FROM {name} x LEFT JOIN provenance p ON p.entity_table='{name}' AND p.entity_id=x.{key} WHERE p.entity_id IS NULL"
            ).fetchone()[0]
            assert missing == 0, f"Missing provenance: {name}"
        duplicates = db.sql(
            "SELECT count(*) FROM (SELECT entity_table,entity_id,source_record_id FROM provenance GROUP BY ALL HAVING count(*)>1)"
        ).fetchone()[0]
        assert duplicates == 0, "Duplicate provenance links"
        report = json.loads((path / "source_report.json").read_text(encoding="utf-8"))
        for name, value in results.items():
            assert report["counts"][name] == value, f"Report balance mismatch: {name}"
    return dict(
        path=str(path),
        structural_status="passed",
        source_status=report["status"],
        counts=results,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("directory", type=Path)
    args = p.parse_args()
    print(json.dumps(validate(args.directory), ensure_ascii=False))
