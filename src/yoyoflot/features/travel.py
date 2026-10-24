"""Explainable travel features built from accepted T13 segments."""
from __future__ import annotations

import json
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import yaml

FEATURES = (
    ("trip_count", "distinct observed T13 segments", "count_segments"),
    ("active_months", "distinct months with a known departure date", "count_months"),
    ("trips_per_active_month", "trip_count / active_months", "rate"),
    ("unique_routes", "distinct known origin and destination pairs", "routes"),
    ("unique_airports", "distinct known airports used as origin or destination", "airports"),
    ("repeated_route_share", "1 - unique_routes / trip_count", "route_repeat"),
    ("median_gap_days", "median gap between dated trips", "gap"),
    ("return_trip_count", "immediate A to B to A sequences", "return"),
    ("short_stay_count", "immediate returns within two days", "short_stay"),
)

FEATURE_SCHEMA = pa.schema([
    pa.field("person_id", pa.string(), nullable=False),
    pa.field("trip_count", pa.int64()),
    pa.field("active_months", pa.int64()),
    pa.field("trips_per_active_month", pa.float64()),
    pa.field("unique_routes", pa.int64()),
    pa.field("unique_airports", pa.int64()),
    pa.field("repeated_route_share", pa.float64()),
    pa.field("median_gap_days", pa.float64()),
    pa.field("return_trip_count", pa.int64()),
    pa.field("short_stay_count", pa.int64()),
    pa.field("observed_days", pa.int64()),
    pa.field("feature_version", pa.string(), nullable=False),
])

COVERAGE_SCHEMA = pa.schema([
    pa.field("person_id", pa.string(), nullable=False),
    pa.field("feature_name", pa.string(), nullable=False),
    pa.field("available", pa.bool_(), nullable=False),
    pa.field("used_observations", pa.int64(), nullable=False),
    pa.field("reason", pa.string(), nullable=False),
    pa.field("feature_version", pa.string(), nullable=False),
])


def _config(path):
    if path is None:
        return {"version": "1.0", "features": [name for name, _, _ in FEATURES]}
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    enabled = data.get("features", [name for name, _, _ in FEATURES])
    known = {name for name, _, _ in FEATURES}
    unknown = set(enabled) - known
    if unknown:
        raise ValueError("Unknown feature(s): " + ", ".join(sorted(unknown)))
    return {"version": str(data.get("version", "1.0")), "features": list(enabled)}


def _paths(input_dir):
    root = Path(input_dir)
    persons, segments = root / "persons.parquet", root / "segments.parquet"
    if not persons.is_file() or not segments.is_file():
        raise FileNotFoundError("T15 requires persons.parquet and segments.parquet from T13")
    return root, persons, segments


def build(input_dir, output_dir=None, config=None, memory_limit="512MB"):
    """Build ``features.parquet`` and ``feature_coverage.parquet``.

    Only T13 ``segments`` and ``persons`` are read. Identifiers, names,
    documents, and person IDs are keys/metadata and never model features.
    """
    root, persons, segments = _paths(input_dir)
    cfg = _config(config)
    output = Path(output_dir) if output_dir else root
    output.mkdir(parents=True, exist_ok=True)
    version = cfg["version"]
    with duckdb.connect() as db:
        db.execute("SET memory_limit=?", [memory_limit])
        db.execute("SET threads=2")
        db.read_parquet(str(persons)).create_view("persons")
        db.read_parquet(str(segments)).create_view("segments")
        db.execute("""
            CREATE TEMP VIEW dated AS
            SELECT *, try_cast(departure_date AS DATE) AS dep_date
            FROM segments
            WHERE person_id IS NOT NULL AND segment_id IS NOT NULL
        """)
        db.execute("""
            CREATE TEMP VIEW ordered AS
            SELECT *, lag(dep_date) OVER (PARTITION BY person_id ORDER BY dep_date, segment_id) prev_date,
                      lag(origin_iata) OVER (PARTITION BY person_id ORDER BY dep_date, segment_id) prev_origin,
                      lag(destination_iata) OVER (PARTITION BY person_id ORDER BY dep_date, segment_id) prev_destination
            FROM dated
        """)
        rows = db.execute("""
            WITH route_counts AS (
                SELECT person_id, count(DISTINCT CASE WHEN origin_iata IS NOT NULL AND destination_iata IS NOT NULL
                    THEN origin_iata || '>' || destination_iata END) unique_routes
                FROM dated GROUP BY person_id
            ), airport_counts AS (
                SELECT person_id, count(DISTINCT airport) unique_airports FROM (
                    SELECT person_id, origin_iata airport FROM dated WHERE origin_iata IS NOT NULL
                    UNION ALL SELECT person_id, destination_iata FROM dated WHERE destination_iata IS NOT NULL
                ) GROUP BY person_id
            ), aggregate AS (
                SELECT person_id, count(DISTINCT segment_id) trip_count,
                       count(DISTINCT strftime(dep_date, '%Y-%m')) FILTER (WHERE dep_date IS NOT NULL) active_months,
                       min(dep_date) first_date, max(dep_date) last_date,
                       median(date_diff('day', prev_date, dep_date)) FILTER (WHERE prev_date IS NOT NULL) median_gap_days,
                       count(*) FILTER (WHERE prev_origin IS NOT NULL AND destination_iata=prev_origin
                                           AND origin_iata=prev_destination) return_trip_count,
                       count(*) FILTER (WHERE prev_origin IS NOT NULL AND destination_iata=prev_origin
                                           AND origin_iata=prev_destination
                                           AND date_diff('day', prev_date, dep_date) BETWEEN 0 AND 2) short_stay_count
                FROM ordered GROUP BY person_id
            )
            SELECT p.person_id, coalesce(a.trip_count, 0)::BIGINT trip_count,
                   coalesce(a.active_months, 0)::BIGINT active_months,
                   CASE WHEN coalesce(a.active_months,0)>0 THEN a.trip_count::DOUBLE/a.active_months ELSE NULL END trips_per_active_month,
                   coalesce(r.unique_routes, 0)::BIGINT unique_routes,
                   coalesce(ac.unique_airports, 0)::BIGINT unique_airports,
                   CASE WHEN coalesce(a.trip_count,0)>0 THEN 1.0-r.unique_routes::DOUBLE/a.trip_count ELSE NULL END repeated_route_share,
                   a.median_gap_days::DOUBLE,
                   coalesce(a.return_trip_count, 0)::BIGINT return_trip_count,
                   coalesce(a.short_stay_count, 0)::BIGINT short_stay_count,
                   CASE WHEN a.first_date IS NOT NULL THEN date_diff('day', a.first_date, a.last_date)+1 ELSE NULL END::BIGINT observed_days
            FROM persons p LEFT JOIN aggregate a USING(person_id)
            LEFT JOIN route_counts r USING(person_id) LEFT JOIN airport_counts ac USING(person_id)
            ORDER BY p.person_id
        """).fetchall()
    feature_names = {name for name in cfg["features"]}
    columns = [field.name for field in FEATURE_SCHEMA]
    records = []
    coverage = []
    for row in rows:
        values = dict(zip(columns[:-1], row), feature_version=version)
        values["feature_version"] = version
        for name, _, _ in FEATURES:
            if name not in feature_names:
                values[name] = None
            used = values["trip_count"]
            available = name in feature_names and (used > 0 if name not in {"trip_count", "active_months"} else True)
            reason = "available" if available else "disabled_by_config" if name not in feature_names else "no_observed_segments"
            coverage.append(dict(person_id=values["person_id"], feature_name=name, available=available,
                                 used_observations=used, reason=reason, feature_version=version))
        records.append(values)
    pq.write_table(pa.Table.from_pylist(records, schema=FEATURE_SCHEMA), output / "features.parquet", compression="zstd")
    pq.write_table(pa.Table.from_pylist(coverage, schema=COVERAGE_SCHEMA), output / "feature_coverage.parquet", compression="zstd")
    report = {"status": "complete", "feature_version": version, "persons": len(records),
              "features": sorted(feature_names), "inputs": {"persons": str(persons), "segments": str(segments)}}
    (output / "features_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report
