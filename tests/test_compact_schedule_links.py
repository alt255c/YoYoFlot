"""Tiny authored controls for lossless T11 reference-link normalization.

No fixture is a production fallback. Both representations use the same source
rows; comparisons cover complete evidence JSON and retained travel decisions.
"""
import json
from collections import Counter
from datetime import date, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from yoyoflot.normalize.airports import ScheduleIndex, enrich_travel
from yoyoflot.normalize.geography import (
    LINK_SCHEMA, REFERENCE_GROUP_SCHEMA, REFERENCE_MEMBER_SCHEMA,
    iter_schedule_links, run,
)
from yoyoflot.provenance import fingerprint
from tests.test_airports import input_tables, reference, schedule, travel


def trip(tid, **changes):
    row = travel()
    row.update(travel_id=tid, source_record_id=tid, locator="row:" + tid, observation_id="p:" + tid)
    row.update(changes)
    return row


def expired(sid="old", **changes):
    row = schedule(sid, valid_from="2016-01-01", valid_to="2016-12-31")
    row.update(changes)
    return row


def read(directory, table):
    return pq.read_table(directory / (table + ".parquet")).to_pylist()


def relation(rows):
    return sorted(rows, key=lambda row: (row["travel_id"], row["schedule_id"]))


def decisions(rows):
    return {row["travel_id"]: {key: value for key, value in row.items()
                              if key not in {"schedule_reference_group_id", "schedule_reference_count"}}
            for row in rows}


def compare_modes(tmp_path, reference, trips, schedules, batch_size=2):
    source, dense, compact = tmp_path / "input", tmp_path / "dense", tmp_path / "compact"
    input_tables(source, trips, schedules)
    before = {path.name: fingerprint(path) for path in source.iterdir()}
    dense_report, _ = run([source], dense, reference.path, batch_size=batch_size, compact_reference_links=False)
    compact_report, _ = run([source], compact, reference.path, batch_size=batch_size)
    old_links = relation(read(dense, "schedule_links"))
    expanded = relation(iter_schedule_links(compact, batch_size=1))
    assert expanded == old_links  # Includes source locators and byte-exact evidence_json.
    assert decisions(read(compact, "travel_geography")) == decisions(read(dense, "travel_geography"))
    assert all(row["temporal_coverage"] != "outside_period" for row in read(compact, "schedule_links"))
    assert compact_report["counts"]["logical_schedule_links"] == len(old_links)
    assert compact_report["counts"]["schedule_links"] == len(read(compact, "schedule_links"))
    assert compact_report["by_source"] == dense_report["by_source"]
    assert before == {path.name: fingerprint(path) for path in source.iterdir()}
    return compact, compact_report, expanded


@pytest.mark.parametrize("case", ["all_outside", "mixed_periods", "observed_clocks", "aliases", "arrival_offsets", "unknown_dates"])
def test_expansion_matches_dense_evidence_and_decisions(tmp_path, reference, case):
    if case == "all_outside":
        trips = [trip("t0"), trip("t1", departure_local="2018-03-04")]
        schedules = [expired("old-a"), expired("old-b", origin="CCC")]
    elif case == "mixed_periods":
        trips = [trip("t0")]
        schedules = [expired(), schedule("active"), schedule("unknown", valid_from=None, valid_to=None)]
    elif case == "observed_clocks":
        trips = [trip("t0", departure_local="2017-01-02T23:30", departure_precision="minute"),
                 trip("t1", departure_local="2017-01-02T22:30", departure_precision="minute"),
                 trip("t2", departure_local="2017-01-02T10:00", departure_precision="minute")]
        schedules = [expired("late"), expired("early", departure_local="22:30")]
    elif case == "aliases":
        trips = [trip("t0", codeshare="XY123"), trip("t1", codeshare="*"), trip("t2")]
        schedules = [expired("primary"), expired("alias", flight_number="XY123")]
    elif case == "arrival_offsets":
        trips = [trip("t0", arrival_local="2017-01-03", arrival_precision="date"),
                 trip("t1", arrival_local="2017-01-02", arrival_precision="date"),
                 trip("t2", departure_local="2018-04-10", arrival_local="2018-04-11", arrival_precision="date")]
        schedules = [expired("next-day"), expired("same-day", arrival_day_offset=0)]
    else:
        trips = [trip("t0", departure_local=None, departure_precision=None), trip("t1")]
        schedules = [expired()]
    compact, _, links = compare_modes(tmp_path, reference, trips, schedules)
    derived = {row["travel_id"]: row for row in read(compact, "travel_geography")}
    if case == "mixed_periods":
        assert derived["t0"]["schedule_match_status"] == "ambiguous"
        assert derived["t0"]["origin_airport_id"] is None
        assert {row["temporal_coverage"] for row in links} == {"confirmed", "unknown", "outside_period"}
    elif case == "observed_clocks":
        assert {(row["travel_id"], row["schedule_id"]) for row in links} == {("t0", "late"), ("t1", "early")}
    elif case == "aliases":
        assert [derived[tid]["schedule_reference_count"] for tid in ("t0", "t1", "t2")] == [2, 1, 1]
    elif case == "arrival_offsets":
        assert {(row["travel_id"], row["schedule_id"]) for row in links} == {
            ("t0", "next-day"), ("t1", "same-day"), ("t2", "next-day")}
    elif case == "unknown_dates":
        assert derived["t0"]["schedule_reference_group_id"] is None
        assert derived["t0"]["schedule_reference_count"] == 0
        assert derived["t0"]["schedule_match_status"] == "temporal_unknown"


def test_shared_groups_publish_complete_versioned_relation(tmp_path, reference):
    trips = [trip(f"t{i}", departure_local=(date(2017, 1, 2) + timedelta(days=i)).isoformat()) for i in range(7)]
    schedules = [expired("one"), expired("two", origin="CCC"), expired("three", departure_local="22:30")]
    compact, report, links = compare_modes(tmp_path, reference, trips, schedules, batch_size=1)
    groups, members = read(compact, "schedule_reference_groups"), read(compact, "schedule_reference_members")
    assert len(groups) == 1 and len(members) == 3 and len(links) == 21
    group = groups[0]
    assert group["member_count"] == 3 and group["status"] == "reference_only"
    assert group["temporal_coverage"] == "outside_period" and group["reason"] == "outside_schedule_period"
    assert group["filled_fields_json"] == "{}"
    assert {row["group_id"] for row in members} == {group["group_id"]}
    assert len({(row["group_id"], row["schedule_id"]) for row in members}) == 3
    assert {row["schedule_reference_group_id"] for row in read(compact, "travel_geography")} == {group["group_id"]}
    counts = report["counts"]
    assert counts["schedule_links"] == 0 and counts["logical_schedule_links"] == 21
    assert counts["schedule_reference_associations"] == 21
    assert counts["schedule_reference_groups"] == 1 and counts["schedule_reference_members"] == 3
    manifest = json.loads((compact / "t11_manifest.json").read_text())
    for name, schema in [("schedule_links", LINK_SCHEMA), ("schedule_reference_groups", REFERENCE_GROUP_SCHEMA),
                         ("schedule_reference_members", REFERENCE_MEMBER_SCHEMA)]:
        path = compact / (name + ".parquet")
        assert manifest["files"][path.name] == fingerprint(path)
        assert pq.read_schema(path).equals(schema, check_metadata=True)
        assert schema.metadata[b"geography_contract_version"] == b"2.0"
    expanded_counts = Counter(row["travel_id"] for row in links)
    assert set(expanded_counts.values()) == {3}


def test_batch_size_and_source_order_do_not_change_groups(tmp_path, reference):
    trips = [trip("t0"), trip("t1", departure_local="2018-01-02"), trip("t2", codeshare="XY123")]
    schedules = [expired("a"), expired("b", flight_number="XY123")]
    results = []
    for index, (rows, schedule_rows, batch_size) in enumerate([(trips, schedules, 1), (trips[::-1], schedules[::-1], 9)]):
        source, output = tmp_path / f"input-{index}", tmp_path / f"output-{index}"
        input_tables(source, rows, schedule_rows)
        run([source], output, reference.path, batch_size=batch_size)
        results.append((relation(iter_schedule_links(output)),
                        {row["travel_id"]: (row["schedule_reference_group_id"], row["schedule_reference_count"])
                         for row in read(output, "travel_geography")},
                        read(output, "schedule_reference_groups"), read(output, "schedule_reference_members")))
    assert results[0] == results[1]


def test_outside_cache_reuses_semantics_across_dates_but_respects_budget(tmp_path, reference):
    index = ScheduleIndex(tmp_path / "cache.sqlite", reference)
    try:
        index.add(expired())
        index.commit()
        first, _ = enrich_travel(trip("first"), reference, index, compact=True)
        assert index.reference_cache_misses == 1
        second, _ = enrich_travel(trip("second", departure_local="2019-04-10"), reference, index, compact=True)
        assert index.reference_cache_hits == 1
        assert first["schedule_reference_group_id"] == second["schedule_reference_group_id"]
        assert 0 < index._reference_bytes <= index.REFERENCE_CACHE_BYTES
    finally:
        index.close()
    bounded = ScheduleIndex(tmp_path / "bounded.sqlite", reference)
    bounded.REFERENCE_CACHE_BYTES = 128
    try:
        bounded.add(expired())
        bounded.commit()
        for i in range(12):
            row, _ = enrich_travel(trip(str(i), departure_local=f"2017-01-{i + 1:02}"), reference, bounded, compact=True)
            assert row["schedule_reference_count"] == 1
            assert bounded._reference_bytes <= bounded.REFERENCE_CACHE_BYTES
    finally:
        bounded.close()


def test_unknown_period_disables_outside_fast_cache_without_dropping_references(tmp_path, reference):
    index = ScheduleIndex(tmp_path / "unknown.sqlite", reference)
    try:
        index.add(expired())
        # Even an unrelated unknown period defeats the whole-corpus proof.
        index.add(schedule("unknown", flight_number="ZZ999", valid_from=None))
        index.commit()
        for tid in ("first", "second"):
            row, links = enrich_travel(trip(tid), reference, index, compact=True)
            assert row["schedule_reference_count"] == 1 and links == []
        assert index.compatibility_cache_hits == index.compatibility_cache_misses == 0
        assert index._compatibility_bytes == 0
        assert index.reference_cache_hits == index.reference_cache_misses == 0
        assert index._reference_bytes == 0
    finally:
        index.close()


def test_reused_compact_cache_does_not_truncate_later_dense_evidence(tmp_path, reference):
    cached = ScheduleIndex(tmp_path / "cached.sqlite", reference)
    baseline = ScheduleIndex(tmp_path / "baseline.sqlite", reference)
    schedules = [expired("one"), expired("two", origin="CCC")]
    try:
        for index in (cached, baseline):
            for row in schedules:
                index.add(row)
            index.commit()
        first, _ = enrich_travel(trip("first"), reference, cached, compact=True)
        repeated = trip("repeated", departure_local="2019-04-10")
        reused, compact_links = enrich_travel(repeated, reference, cached, compact=True)
        expected, expected_links = enrich_travel(repeated, reference, baseline, compact=False)
        assert decisions([reused]) == decisions([expected])
        assert compact_links == [] and reused["schedule_reference_count"] == 2
        assert reused["schedule_reference_group_id"] == first["schedule_reference_group_id"]
        member_ids = {row[0] for row in cached.db.execute(
            "SELECT schedule_id FROM reference_members WHERE group_id=?", [reused["schedule_reference_group_id"]])}
        assert member_ids == {row["schedule_id"] for row in expected_links}
        # A descriptor has no schedule payloads. Dense mode must recover all
        # evidence, even when the same semantic key was warmed in compact mode.
        dense, dense_links = enrich_travel(repeated, reference, cached, compact=False)
        assert dense == expected and dense_links == expected_links
        assert {row["schedule_id"] for row in dense_links} == {"one", "two"}
        reused_again, links_again = enrich_travel(repeated, reference, cached, compact=True)
        assert reused_again == reused and links_again == []
    finally:
        cached.close()
        baseline.close()


def test_reused_empty_compact_result_stays_unresolved(tmp_path, reference):
    index = ScheduleIndex(tmp_path / "empty-match.sqlite", reference)
    try:
        index.add(expired())
        index.commit()
        for tid, day in [("first", "2017-01-02"), ("second", "2019-04-10"), ("third", "2020-02-20")]:
            row = trip(tid, flight_number="ZZ404", departure_local=day)
            compact, links = enrich_travel(row, reference, index, compact=True)
            assert links == [] and compact["schedule_reference_count"] == 0
            assert compact["schedule_reference_group_id"] is None
            assert compact["schedule_match_status"] == "unresolved"
            assert compact["schedule_id_used"] is None and compact["inferred_fields_json"] == "{}"
            dense, dense_links = enrich_travel(row, reference, index, compact=False)
            assert decisions([compact]) == decisions([dense]) and dense_links == []
    finally:
        index.close()


def test_many_descriptor_keys_reuse_without_decoding_schedule_payloads(tmp_path, reference, monkeypatch):
    index = ScheduleIndex(tmp_path / "many-descriptors.sqlite", reference)
    index.REFERENCE_CACHE_BYTES = 16 * 1024
    # The result must not depend on retaining full schedule objects elsewhere.
    index.CACHE_BYTES = index.EVIDENCE_BYTES = index.COMPATIBILITY_CACHE_BYTES = 128
    keys, members_per_key = 16, 20
    try:
        for key in range(keys):
            for member in range(members_per_key):
                index.add(expired(f"s-{key}-{member}", flight_number=f"AB{1000 + key}"))
        index.commit()
        expected = {}
        for key in range(keys):
            row, links = enrich_travel(trip(f"first-{key}", flight_number=f"AB{1000 + key}"), reference, index, compact=True)
            expected[key] = row["schedule_reference_group_id"]
            assert links == [] and row["schedule_reference_count"] == members_per_key
            assert index._reference_bytes <= index.REFERENCE_CACHE_BYTES
        assert index.reference_cache_misses == keys
        assert len(set(expected.values())) == keys
        assert index.db.execute("SELECT count(*) FROM reference_members").fetchone()[0] == keys * members_per_key
        decoded = index.decoded_rows

        def do_not_scan(*args, **kwargs):
            raise AssertionError("Warm descriptor reuse must not scan or decode schedule candidates")

        monkeypatch.setattr(index, "candidates", do_not_scan)
        for key in range(keys):
            row, links = enrich_travel(trip(f"second-{key}", flight_number=f"AB{1000 + key}",
                                           departure_local="2019-04-10"), reference, index, compact=True)
            assert row["schedule_reference_group_id"] == expected[key]
            assert row["schedule_reference_count"] == members_per_key and links == []
        assert index.reference_cache_hits == keys and index.reference_cache_misses == keys
        assert index.decoded_rows == decoded
    finally:
        index.close()


def test_descriptor_eviction_preserves_membership_when_old_key_is_revisited(tmp_path, reference):
    index = ScheduleIndex(tmp_path / "evicted-descriptors.sqlite", reference)
    index.REFERENCE_CACHE_BYTES = 1024
    keys = 12
    try:
        for key in range(keys):
            index.add(expired(f"s-{key}", flight_number=f"AB{1000 + key}"))
        index.commit()
        expected = {}
        for key in range(keys):
            row, _ = enrich_travel(trip(str(key), flight_number=f"AB{1000 + key}"), reference, index, compact=True)
            expected[key] = row["schedule_reference_group_id"]
            assert index._reference_bytes <= index.REFERENCE_CACHE_BYTES
        hits = index.reference_cache_hits
        row, _ = enrich_travel(trip("last-again", flight_number=f"AB{1000 + keys - 1}"), reference, index, compact=True)
        assert row["schedule_reference_group_id"] == expected[keys - 1]
        assert index.reference_cache_hits == hits + 1
        misses = index.reference_cache_misses
        row, links = enrich_travel(trip("first-again", flight_number="AB1000"), reference, index, compact=True)
        assert index.reference_cache_misses == misses + 1
        assert row["schedule_reference_group_id"] == expected[0] and row["schedule_reference_count"] == 1
        assert links == [] and index._reference_bytes <= index.REFERENCE_CACHE_BYTES
        assert index.db.execute("SELECT count(*) FROM reference_groups").fetchone()[0] == keys
        assert index.db.execute("SELECT count(*) FROM reference_members").fetchone()[0] == keys
    finally:
        index.close()


@pytest.mark.parametrize("period", [{"valid_from": None}, {"valid_to": None},
                                    {"valid_from": "2018-01-01", "valid_to": "2017-01-01"}])
def test_descriptor_cache_is_disabled_by_any_unknown_or_reversed_period(tmp_path, reference, period):
    index = ScheduleIndex(tmp_path / "invalid-period.sqlite", reference)
    try:
        index.add(expired())
        index.add(schedule("uncertain", flight_number="ZZ999", **period))
        index.commit()
        for day in ("2017-01-02", "2019-04-10"):
            row, links = enrich_travel(trip(day, departure_local=day), reference, index, compact=True)
            assert row["schedule_match_status"] == "reference_only"
            assert row["schedule_reference_count"] == 1 and links == []
        assert index.reference_cache_hits == index.reference_cache_misses == 0
        assert index._reference_bytes == 0
    finally:
        index.close()


def test_adding_schedule_invalidates_warmed_descriptors(tmp_path, reference):
    index = ScheduleIndex(tmp_path / "added-schedule.sqlite", reference)
    try:
        index.add(expired("first"))
        index.commit()
        before, _ = enrich_travel(trip("before"), reference, index, compact=True)
        index.add(expired("second"))
        index.commit()
        after, links = enrich_travel(trip("after"), reference, index, compact=True)
        assert before["schedule_reference_count"] == 1 and after["schedule_reference_count"] == 2
        assert before["schedule_reference_group_id"] != after["schedule_reference_group_id"]
        assert links == []
        _, dense = enrich_travel(trip("after"), reference, index, compact=False)
        assert {row["schedule_id"] for row in dense} == {"first", "second"}
    finally:
        index.close()


def test_empty_schedule_corpus_publishes_empty_group_tables(tmp_path, reference):
    compact, report, links = compare_modes(tmp_path, reference, [trip("t0")], [])
    assert links == [] and read(compact, "schedule_reference_groups") == read(compact, "schedule_reference_members") == []
    assert report["counts"]["logical_schedule_links"] == report["counts"]["schedule_reference_associations"] == 0


def test_expansion_rejects_nonpositive_batch_size(tmp_path):
    with pytest.raises(ValueError, match="batch_size"):
        list(iter_schedule_links(tmp_path, batch_size=0))


@pytest.fixture
def compact_relation(tmp_path, reference):
    source, output = tmp_path / "source", tmp_path / "result"
    # Active links ensure validation happens before any dense row is yielded.
    input_tables(source, [trip("t0"), trip("t1")], [expired("old-a"), expired("old-b"), schedule("active")])
    run([source], output, reference.path)
    assert len(read(output, "schedule_links")) == 2
    return output


def rewrite(directory, table, rows):
    path = directory / (table + ".parquet")
    pq.write_table(pa.Table.from_pylist(rows, schema=pq.read_schema(path)), path)


def refresh_manifest(directory):
    path = directory / "t11_manifest.json"
    manifest = json.loads(path.read_text())
    manifest["files"] = {name: fingerprint(directory / name) for name in manifest["files"]}
    path.write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("damage", ["missing_member", "duplicate_member", "unknown_schedule", "unknown_group",
                                     "wrong_group_count", "wrong_travel_count", "wrong_logical_count", "wrong_status"])
def test_expansion_rejects_corrupt_compact_relation_before_yield(compact_relation, damage):
    root = compact_relation
    report_path = root / "geography_report.json"
    report = json.loads(report_path.read_text())
    if damage in {"missing_member", "duplicate_member", "unknown_schedule", "unknown_group"}:
        members = read(root, "schedule_reference_members")
        if damage == "missing_member":
            members.pop()
        elif damage == "duplicate_member":
            members.append(dict(members[0]))
        elif damage == "unknown_schedule":
            members[0]["schedule_id"] = "nonexistent-schedule"
        else:
            members[0]["group_id"] = "nonexistent-group"
        rewrite(root, "schedule_reference_members", members)
        # Retain consistent physical counts so relational checks must detect it.
        report["counts"]["schedule_reference_members"] = len(members)
    elif damage in {"wrong_group_count", "wrong_status"}:
        groups = read(root, "schedule_reference_groups")
        if damage == "wrong_group_count":
            groups[0]["member_count"] += 1
        else:
            groups[0]["status"] = "matched"
        rewrite(root, "schedule_reference_groups", groups)
    elif damage == "wrong_travel_count":
        trips = read(root, "travel_geography")
        trips[0]["schedule_reference_count"] += 1
        rewrite(root, "travel_geography", trips)
    else:
        report["counts"]["logical_schedule_links"] += 1
    report_path.write_text(json.dumps(report), encoding="utf-8")
    refresh_manifest(root)
    with pytest.raises(ValueError, match="T11 reference integrity"):
        next(iter_schedule_links(root))


@pytest.mark.parametrize("name", ["schedule_reference_groups.parquet", "schedule_reference_members.parquet"])
def test_compact_expansion_rejects_missing_artifact_before_yield(compact_relation, name):
    (compact_relation / name).unlink()
    with pytest.raises(ValueError, match="Missing compact schedule reference artifact"):
        next(iter_schedule_links(compact_relation))


def test_expansion_rejects_unknown_contract_before_yield(compact_relation):
    path = compact_relation / "travel_geography.parquet"
    table = pq.read_table(path)
    pq.write_table(table.replace_schema_metadata({**table.schema.metadata, b"geography_contract_version": b"99.0"}), path)
    with pytest.raises(ValueError, match="Unsupported geography contract"):
        next(iter_schedule_links(compact_relation))
