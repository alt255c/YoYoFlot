import pyarrow as pa
import pyarrow.parquet as pq

from yoyoflot.features.travel import build


def test_features_are_person_level_and_date_conservative(tmp_path):
    pq.write_table(pa.table({"person_id": ["p1", "p2"]}), tmp_path / "persons.parquet")
    pq.write_table(pa.table({
        "segment_id": ["s1", "s2", "s3"],
        "person_id": ["p1", "p1", "p1"],
        "departure_date": ["2024-01-01", "2024-01-03", None],
        "origin_iata": ["AAA", "BBB", None],
        "destination_iata": ["BBB", "AAA", None],
    }), tmp_path / "segments.parquet")
    report = build(tmp_path, config=None)
    assert report["persons"] == 2
    rows = pq.read_table(tmp_path / "features.parquet").to_pylist()
    p1 = next(row for row in rows if row["person_id"] == "p1")
    p2 = next(row for row in rows if row["person_id"] == "p2")
    assert p1["trip_count"] == 3
    assert p1["active_months"] == 1
    assert p1["unique_routes"] == 2
    assert p1["return_trip_count"] == 1
    assert p1["short_stay_count"] == 1
    assert p1["median_gap_days"] == 2
    assert p2["trip_count"] == 0
    assert p2["trips_per_active_month"] is None


def test_disabled_feature_is_null_and_marked(tmp_path):
    pq.write_table(pa.table({"person_id": ["p1"]}), tmp_path / "persons.parquet")
    pq.write_table(pa.table({"segment_id": ["s1"], "person_id": ["p1"], "departure_date": [None], "origin_iata": [None], "destination_iata": [None]}), tmp_path / "segments.parquet")
    config = tmp_path / "features.yaml"
    config.write_text("version: 'test'\nfeatures: [trip_count]\n", encoding="utf-8")
    build(tmp_path, config=config)
    row = pq.read_table(tmp_path / "features.parquet").to_pylist()[0]
    coverage = pq.read_table(tmp_path / "feature_coverage.parquet").to_pylist()
    assert row["unique_routes"] is None
    assert next(x for x in coverage if x["feature_name"] == "unique_routes")["reason"] == "disabled_by_config"
