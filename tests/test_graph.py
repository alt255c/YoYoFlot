import copy
import json
import os
import random
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.cli import main
from yoyoflot.features.graph import OUTPUT_SCHEMAS, _build_graph, load_graph_config
from yoyoflot.features.graph_artifact import run
from yoyoflot.features.graph_demo import _generate_observations
from yoyoflot.models.demo import END, PROFILE_SCHEMA, START, create_demo, generate_rows, recipe, validate_demo, write_json
from yoyoflot.normalize.locking import OutputLock
from yoyoflot.provenance import fingerprint

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "configs/graph.yaml"


@pytest.fixture
def config():
    return load_graph_config(MODEL)[0]


def partition_fixture(partitions):
    """Explicit independent test data; never accepted as an external demo."""
    ids = sorted({pid for groups in partitions for members in groups for pid in members})
    profiles = [dict(profile_id=f"demo_profile_{pid}", display_name=f"Учебный {pid}", identity_status="certain",
                     source_coverage=1.0, window_start=START, window_end=END) for pid in ids]
    segments, observations = [], []
    for day, groups in enumerate(partitions):
        for flight, members in enumerate(groups):
            for pid in members:
                segment = dict(segment_id=f"demo_segment_{day}_{pid}", profile_id=f"demo_profile_{pid}",
                               departure_date=START + timedelta(days=day), departure_precision="day",
                               origin_iata="SVO", destination_iata="LED")
                segments.append(segment)
                observations.append(dict(observation_id=segment["segment_id"] + "_source", source_id="fictional_a",
                                         source_record_id=segment["segment_id"] + "_source", **{k: v for k, v in segment.items() if k != "departure_precision"},
                                         service_id=f"demo_service_{day}_{flight}", flight_number=f"DD{flight + 1:04d}",
                                         operating_service_id=None, operating_flight_number=None, codeshare_rule_id=None,
                                         identity_status="certain", resolution_status="resolved", participation_status="recorded",
                                         event_kind="fictional_boarding_record"))
    return profiles, segments, observations


def build(partitions, config):
    return _build_graph(*partition_fixture(partitions), [], config)


def read(path):
    return pq.ParquetFile(path).read().to_pylist()


def test_one_flight_and_duplicate_sources_never_become_repeat(config):
    profiles, segments, observations = partition_fixture([[(0, 1, 2)]])
    copies = [dict(r, observation_id=r["observation_id"] + "_copy", source_record_id=r["source_record_id"] + "_copy", source_id="fictional_b") for r in observations]
    tables, _ = _build_graph(profiles, segments, observations + copies + copies, [], config)
    assert len(tables["graph_events.parquet"]) == 1
    assert len(tables["graph_memberships.parquet"]) == 3
    assert len(tables["graph_links.parquet"]) == 6
    assert all(e["coflight_events"] == e["distinct_days"] == 1 and not e["repeated"] for e in tables["coflight_edges.parquet"])
    assert tables["graph_events.parquet"][0]["participant_count"] == 3
    assert all(len(m["observation_ids"]) == 2 for m in tables["graph_memberships.parquet"])


def test_exact_duplicate_conflicts_are_blockers(config):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    with pytest.raises(ValueError, match="Противоречивые дубли"):
        _build_graph(profiles, segments, observations + [dict(observations[0], source_id="different")], [], config)


def test_distinct_segment_ids_for_one_profile_event_do_not_add_slots(config):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    segment = dict(segments[0], segment_id="demo_segment_duplicate")
    witness = dict(observations[0], segment_id=segment["segment_id"], observation_id="demo_extra", source_record_id="demo_extra")
    tables, _ = _build_graph(profiles, segments + [segment], observations + [witness], [], config)
    assert tables["graph_events.parquet"][0]["participant_count"] == 2
    assert tables["coflight_edges.parquet"][0]["distinct_days"] == 1
    assert tables["coflight_edges.parquet"][0]["expected_days"] == 1
    member = next(r for r in tables["graph_memberships.parquet"] if r["profile_id"] == segment["profile_id"])
    assert len(member["segment_ids"]) == 2


def test_route_date_and_same_flight_number_are_not_service_identity(config):
    tables, _ = build([[(0, 1), (2, 3)]], config)
    assert len(tables["graph_events.parquet"]) == 2
    assert {(r["profile_a"], r["profile_b"]) for r in tables["coflight_edges.parquet"]} == {
        ("demo_profile_0", "demo_profile_1"), ("demo_profile_2", "demo_profile_3")}
    profiles, segments, observations = partition_fixture([[(0, 1), (2, 3)]])
    for r in observations:
        r["flight_number"] = "DD0001"
    same_number, _ = _build_graph(profiles, segments, observations, [], config)
    assert len(same_number["graph_events.parquet"]) == 2 and len(same_number["coflight_edges.parquet"]) == 2


def test_repeated_full_popular_flight_has_no_excess(config):
    tables, _ = build([[tuple(range(8))]] * 3, config)
    assert len(tables["coflight_edges.parquet"]) == 28
    assert all(e["repeated"] and e["distinct_days"] == e["expected_days"] == 3 and e["p_exact"] == 1
               and e["excess_days"] == 0 for e in tables["coflight_edges.parquet"])
    assert all(f["repeated_companion_count"] == f["expected_repeated_companions"] == 7
               and f["excess_repeated_companions"] == 0 and f["max_positive_pair_excess_days"] == 0 and f["m07_status"] == "disabled"
               for f in tables["graph_features.parquet"])


def test_rotating_independent_companions_keep_negative_excess(config):
    tables, _ = build([[(0, 1), (2, 3)], [(0, 2), (1, 3)], [(0, 3), (1, 2)]], config)
    assert not any(e["repeated"] for e in tables["coflight_edges.parquet"])
    for row in tables["graph_features.parquet"]:
        assert row["observed_partner_days_sum"] == row["expected_partner_days_sum"] == 3
        assert row["repeated_companion_count"] == 0
        assert row["expected_repeated_companions"] == pytest.approx(7 / 9)
        assert row["excess_repeated_companions"] == pytest.approx(-7 / 9)


def test_two_events_same_day_are_one_repeat_day(config):
    profiles, segments, observations = partition_fixture([[(0, 1), (2, 3)]])
    reverse_segments = [dict(s, segment_id=s["segment_id"] + "_back", origin_iata="LED", destination_iata="SVO") for s in segments]
    reverse_observations = [dict(o, segment_id=o["segment_id"] + "_back", observation_id=o["observation_id"] + "_back",
                                 source_record_id=o["source_record_id"] + "_back", service_id=o["service_id"] + "_back",
                                 origin_iata="LED", destination_iata="SVO") for o in observations]
    tables, _ = _build_graph(profiles, segments + reverse_segments, observations + reverse_observations, [], config)
    assert all(e["coflight_events"] == 2 and e["distinct_days"] == 1 and not e["repeated"] for e in tables["coflight_edges.parquet"])
    assert all(e["expected_days"] == pytest.approx(5 / 9) for e in tables["coflight_edges.parquet"])


def test_verified_codeshare_is_one_event_and_unverified_alias_is_excluded(config):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    row = observations[0]
    alias = dict(row, observation_id="demo_alias", source_record_id="demo_alias", service_id="demo_marketing",
                 flight_number="DM0001", operating_service_id=row["service_id"], operating_flight_number=row["flight_number"],
                 codeshare_rule_id="demo_rule")
    rule = dict(rule_id="demo_rule", marketing_service_id="demo_marketing", operating_service_id=row["service_id"],
                marketing_flight_number="DM0001", operating_flight_number=row["flight_number"],
                departure_date=row["departure_date"], origin_iata=row["origin_iata"], destination_iata=row["destination_iata"],
                evidence_ru="Явное учебное свидетельство")
    valid, _ = _build_graph(profiles, segments, observations + [alias], [rule], config)
    assert len(valid["graph_events.parquet"]) == len(valid["coflight_edges.parquet"]) == 1
    assert all(r["graph_eligible"] for r in valid["graph_links.parquet"])
    invalid, _ = _build_graph(profiles, segments, observations + [alias], [], config)
    assert invalid["coflight_edges.parquet"] == valid["coflight_edges.parquet"]
    assert next(r for r in invalid["graph_links.parquet"] if r["observation_id"] == "demo_alias")["eligibility_reason"] == "unverified_codeshare"


@pytest.mark.parametrize("kind", ["chain", "duplicate_alias", "missing_flight"])
def test_invalid_codeshare_rules_are_blockers(config, kind):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    row = observations[0]
    first = dict(rule_id="r1", marketing_service_id="demo_alias", operating_service_id=row["service_id"],
                 marketing_flight_number="DM0001", operating_flight_number=row["flight_number"],
                 departure_date=row["departure_date"], origin_iata=row["origin_iata"], destination_iata=row["destination_iata"],
                 evidence_ru="Учебное правило")
    if kind == "chain":
        rules = [first, dict(first, rule_id="r2", marketing_service_id=row["service_id"], operating_service_id="terminal")]
    elif kind == "duplicate_alias":
        rules = [first, dict(first, rule_id="r2")]
    else:
        rules = [dict(first, marketing_flight_number=None)]
    with pytest.raises(ValueError, match="codeshare"):
        _build_graph(profiles, segments, observations, rules, config)


@pytest.mark.parametrize("kind", ["reported", "schedule", "identity", "resolution", "provenance", "service", "date", "route"])
def test_uncertain_or_incomplete_witness_never_creates_edge(config, kind):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    if kind == "reported":
        observations[0]["participation_status"] = "reported"
    elif kind == "schedule":
        observations[0]["event_kind"] = "fictional_schedule_reference"
    elif kind == "identity":
        observations[0]["identity_status"] = "ambiguous"
    elif kind == "resolution":
        observations[0]["resolution_status"] = "ambiguous"
    elif kind == "provenance":
        observations[0]["source_record_id"] = None
    elif kind == "service":
        observations[0]["service_id"] = None
    else:
        key = "departure_date" if kind == "date" else "origin_iata"
        segments[0][key] = observations[0][key] = None
    tables, _ = _build_graph(profiles, segments, observations, [], config)
    assert not tables["coflight_edges.parquet"]
    assert all(r["event_id"] is None and r["eligibility_reason"] for r in tables["graph_links.parquet"] if r["profile_id"] == "demo_profile_0")
    feature = next(f for f in tables["graph_features.parquet"] if f["profile_id"] == "demo_profile_0")
    assert not feature["available"] and feature["repeated_companion_count"] is None


def test_conflicting_service_metadata_is_rejected_for_all_witnesses(config):
    profiles, segments, observations = partition_fixture([[(0, 1)], [(0, 1)]])
    observations[2]["service_id"] = observations[0]["service_id"]
    tables, _ = _build_graph(profiles, segments, observations, [], config)
    assert not tables["coflight_edges.parquet"]
    conflicted = {observations[i]["observation_id"] for i in [0, 1, 2]}
    assert all(r["eligibility_reason"] == "conflicting_event_evidence" and not r["graph_eligible"]
               for r in tables["graph_links.parquet"] if r["observation_id"] in conflicted)


def test_conflicting_segment_has_no_first_wins_behaviour(config):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    alternative = dict(observations[0], observation_id="alternative", source_record_id="alternative", service_id="other_service")
    tables, _ = _build_graph(profiles, segments, observations + [alternative], [], config)
    assert not tables["coflight_edges.parquet"]
    assert sum(r["eligibility_reason"] == "conflicting_event_evidence" for r in tables["graph_links.parquet"]) == 2


def test_missing_or_wrong_foreign_keys_fail(config):
    profiles, segments, observations = partition_fixture([[(0, 1)]])
    observations[0]["profile_id"] = "other"
    with pytest.raises(ValueError, match="Неверная связь"):
        _build_graph(profiles, segments, observations, [], config)


def test_shuffle_is_stable_and_graph_generation_replays(config):
    profiles, segments = generate_rows(recipe(profiles=30))
    observations, rules, spec = _generate_observations(profiles, segments, config["graph_seed"])
    expected = _build_graph(profiles, segments, observations, rules, config)
    for rows in [profiles, segments, observations, rules]:
        random.Random(2).shuffle(rows)
    assert _build_graph(profiles, segments, observations, rules, config) == expected
    assert _generate_observations(profiles, segments, config["graph_seed"])[2] == spec
    assert sorted(_generate_observations(profiles, segments, config["graph_seed"])[0], key=lambda r: r["observation_id"]) == sorted(observations, key=lambda r: r["observation_id"])


def test_witness_ids_follow_generated_input_and_event_versions(config):
    people, segments = generate_rows(recipe(profiles=20))
    first = {r["observation_id"]: r for r in _generate_observations(people, segments, config["graph_seed"])[0]}
    other_seed = _generate_observations(people, segments, config["graph_seed"] + 1)[0]
    assert not set(first).intersection(r["observation_id"] for r in other_seed)
    for spec in [recipe(profiles=30), recipe(seed=42, profiles=20)]:
        people, segments = generate_rows(spec)
        second = {r["observation_id"]: r for r in _generate_observations(people, segments, config["graph_seed"])[0]}
        assert all(first[oid] == second[oid] for oid in set(first).intersection(second))


def test_future_events_do_not_change_past_features_or_null_reference(config):
    profiles, segments, observations = partition_fixture([[(0, 1), (2, 3)]] * 4)
    config.update(as_of="2026-01-03", min_covered_days=1)
    before, _ = _build_graph(profiles, segments[:-4], observations[:-4], [], config)
    after, _ = _build_graph(profiles, segments, observations, [], config)
    for name in ["graph_events.parquet", "graph_memberships.parquet", "coflight_edges.parquet", "graph_features.parquet"]:
        assert before[name] == after[name]
    assert sum(r["eligibility_reason"] == "outside_window" for r in after["graph_links.parquet"]) == 4


def test_inadequate_profiles_are_null_and_cannot_populate_graph(config):
    profiles, segments = generate_rows(recipe(profiles=10))
    observations, rules, _ = _generate_observations(profiles, segments, config["graph_seed"])
    tables, _ = _build_graph(profiles, segments, observations, rules, config)
    bad = {profiles[i]["profile_id"] for i in [6, 7, 8, 9]}
    for row in tables["graph_features.parquet"]:
        assert row["available"] == (row["profile_id"] not in bad)
        if row["profile_id"] in bad:
            assert row["eligible_segments"] == row["eligible_events"] == 0
            assert row["companion_count"] is row["expected_repeated_companions"] is None
    assert all(not bad.intersection(e["participant_ids"]) for e in tables["graph_events.parquet"])


@pytest.mark.parametrize("kind", ["mode", "seed", "nan", "repeat", "date", "simulations", "null_version", "budget", "unknown"])
def test_invalid_configs_fail(tmp_path, kind):
    config = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    if kind == "mode":
        config["data_mode"] = "real"
    elif kind == "seed":
        config["graph_seed"] = True
    elif kind == "nan":
        config["min_source_coverage"] = float("nan")
    elif kind == "repeat":
        config["min_repeat_days"] = 1
    elif kind == "date":
        config["as_of"] = "2026-04-01"
    elif kind == "simulations":
        config["null_model"]["simulations"] = 1
    elif kind == "null_version":
        config["null_model"]["version"] = "global_shuffle"
    elif kind == "budget":
        config["limits"]["max_pair_visits"] = 1000000000000
    else:
        config["enable_m07"] = True
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError):
        load_graph_config(path)


@pytest.mark.parametrize("profiles", [0, 10])
def test_snapshot_types_provenance_full_history_and_replay(tmp_path, profiles):
    source = create_demo(tmp_path / "demo", profiles=profiles)
    checksums = {n: fingerprint(source / n) for n in ["profiles.parquet", "segments.parquet", "demo_manifest.json"]}
    report, summary = run(source, MODEL, artifacts_dir=tmp_path / "out")
    output = tmp_path / "out" / report["run_id"]
    assert report["m07_status"] == "disabled" and not report["training_performed"] and not report["ui_ready"]
    assert validate_demo(output / "input")[0]["logical_sha256"] == validate_demo(source)[0]["logical_sha256"]
    assert len(read(output / "input/segments.parquet")) == report["segments"]  # Graph eligibility never filters the map history.
    for name, schema in OUTPUT_SCHEMAS.items():
        file = pq.ParquetFile(output / name)
        assert file.schema_arrow.equals(schema, check_metadata=False)
        assert file.schema_arrow.metadata[b"data_mode"] == b"synthetic_demo"
    links = {r["observation_id"]: r for r in read(output / "graph_links.parquet")}
    events = {r["event_id"]: r for r in read(output / "graph_events.parquet")}
    for edge in read(output / "coflight_edges.parquet"):
        evidence = [events[eid] for eid in edge["event_ids"]]
        assert len(evidence) == edge["coflight_events"]
        assert len({e["departure_date"] for e in evidence}) == edge["distinct_days"]
        assert all({edge["profile_a"], edge["profile_b"]} <= set(e["participant_ids"]) for e in evidence)
    for membership in read(output / "graph_memberships.parquet"):
        assert all(links[oid]["graph_eligible"] and links[oid]["event_id"] == membership["event_id"]
                   and links[oid]["profile_id"] == membership["profile_id"] for oid in membership["observation_ids"])
    assert run(source, MODEL, output=output) == (report, summary)
    assert {n: fingerprint(source / n) for n in checksums} == checksums


def test_immutable_versions_and_corruption_detection(tmp_path):
    source = create_demo(tmp_path / "demo", profiles=6)
    first, _ = run(source, MODEL, artifacts_dir=tmp_path / "out")
    config = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    config["null_model"]["seed"] += 1
    changed = tmp_path / "graph.yaml"
    changed.write_text(yaml.safe_dump(config), encoding="utf-8")
    second, _ = run(source, changed, artifacts_dir=tmp_path / "out")
    assert first["run_id"] != second["run_id"]
    output = tmp_path / "out" / first["run_id"]
    with pytest.raises(ValueError, match="другой версии"):
        run(source, changed, output=output)
    (output / "coflight_edges.parquet").write_bytes(b"broken")
    with pytest.raises(ValueError, match="Изменён файл"):
        run(source, MODEL, output=output)
    assert (output / "coflight_edges.parquet").read_bytes() == b"broken"


def test_real_and_falsely_relabelled_inputs_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="учебный набор"):
        run(tmp_path, MODEL, output=tmp_path / "out")
    source = create_demo(tmp_path / "demo", profiles=6)
    people = read(source / "profiles.parquet")
    people[0]["display_name"] = "Copied person"
    pq.write_table(pa.Table.from_pylist(people, schema=PROFILE_SCHEMA), source / "profiles.parquet")
    manifest = json.loads((source / "demo_manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["profiles.parquet"] = fingerprint(source / "profiles.parquet")
    write_json(source / "demo_manifest.json", manifest)
    with pytest.raises(ValueError, match="не воспроизводятся"):
        run(source, MODEL, output=tmp_path / "out")


def test_existing_overlapping_and_concurrent_outputs_are_preserved(tmp_path):
    source = create_demo(tmp_path / "demo", profiles=2)
    with pytest.raises(ValueError, match="пересекаться"):
        run(source, MODEL, output=source / "out")
    output = tmp_path / "out"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="завершённым снимком"):
        run(source, MODEL, output=output)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"
    with OutputLock(tmp_path / ".t18-graph.lock", "test"):
        with pytest.raises(RuntimeError, match="другим процессом"):
            run(source, MODEL, output=tmp_path / "new")


def test_interruption_or_resource_failure_never_publishes_snapshot(tmp_path, monkeypatch):
    from yoyoflot.features import graph_artifact
    source = create_demo(tmp_path / "demo", profiles=6)
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(graph_artifact, "_build_graph", interrupt)
    output = tmp_path / "new"
    with pytest.raises(KeyboardInterrupt):
        run(source, MODEL, output=output)
    assert not output.exists() and not list(tmp_path.glob("*.partial"))


def test_resource_budget_failure_does_not_publish_or_silently_truncate(tmp_path):
    source = create_demo(tmp_path / "demo", profiles=6)
    config = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    config["limits"]["max_event_participants"] = 1
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    output = tmp_path / "new"
    with pytest.raises(ValueError, match="max_event_participants"):
        run(source, path, output=output)
    assert not output.exists() and not list(tmp_path.glob("*.partial"))


def test_t17_snapshot_is_compatible_and_its_rankings_stay_unchanged(tmp_path):
    from yoyoflot.models.ranking import run as rules_run
    source = create_demo(tmp_path / "demo", profiles=10)
    rules_report, _ = rules_run(source, ROOT / "configs/model.yaml", artifacts_dir=tmp_path / "rules")
    rules_output = tmp_path / "rules" / rules_report["run_id"]
    before = {str(p.relative_to(rules_output)): fingerprint(p) for p in rules_output.rglob("*") if p.is_file()}
    graph_report, _ = run(rules_output / "input", MODEL, artifacts_dir=tmp_path / "graph")
    graph_output = tmp_path / "graph" / graph_report["run_id"]
    rules_meta = json.loads((rules_output / "model_metadata.json").read_text(encoding="utf-8"))
    graph_meta = json.loads((graph_output / "model_metadata.json").read_text(encoding="utf-8"))
    assert rules_meta["input_logical_sha256"] == graph_meta["input_logical_sha256"]
    assert graph_meta["m07_status"] == "disabled"
    assert {str(p.relative_to(rules_output)): fingerprint(p) for p in rules_output.rglob("*") if p.is_file()} == before


def test_cli_graph_requires_existing_product_and_never_generates(tmp_path, capsys):
    assert main(["graph"]) == 2
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    script = ROOT / "scripts/run_t18.py"
    result = subprocess.run([sys.executable, str(script), "--product", str(tmp_path / "missing")],
                            cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 1 and not list(tmp_path.iterdir())
    help_run = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert help_run.returncode == 0 and "--product" in help_run.stdout and "--demo" not in help_run.stdout
