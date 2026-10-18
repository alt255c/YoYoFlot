import copy
import json
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.cli import main
from yoyoflot.models.demo import (TABLES, create_demo, generate_rows, logical_hash,
                                  recipe, validate_demo, write_json)
from yoyoflot.models.ranking import OUTPUT_TABLES, rank_scores, run
from yoyoflot.models.rule_score import _score_profiles, group_totals, load_model, score_demo
from yoyoflot.normalize.locking import OutputLock
from yoyoflot.provenance import fingerprint

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / "configs/model.yaml"


@pytest.fixture
def config():
    return load_model(MODEL)[0]


def evaluate(count, config):
    people, segments = generate_rows(recipe(profiles=count))
    return _score_profiles(people, segments, config)


def read(path):
    return pq.ParquetFile(path).read().to_pylist()


def rewrite_table(directory, name, rows):
    pq.write_table(pa.Table.from_pylist(rows, schema=TABLES[name]), directory / name)
    manifest = json.loads((directory / "demo_manifest.json").read_text(encoding="utf-8"))
    manifest["files"][name] = fingerprint(directory / name)
    write_json(directory / "demo_manifest.json", manifest)


def test_independent_groups_dominate_weights(config):
    scores, explanations, _, _ = evaluate(6, config)
    assert [s["matched_criteria_count"] for s in scores] == [0, 1, 1, 1, 3, 2]
    config["criteria"][0]["weight"] = 100
    scores = evaluate(6, config)[0]
    # Three > two > one, even if a one-group profile has a larger numeric score.
    next(s for s in scores if s["profile_id"].endswith("000003"))["rule_score"] = 1000
    ranked, _ = rank_scores(scores, "test")
    assert [r["profile_id"][-6:] for r in ranked[:3]] == ["000005", "000006", "000003"]
    assert sum(r["group_contribution"] for r in explanations if r["profile_id"].endswith("000005")) == 3


def test_correlated_criteria_and_repeated_evidence_get_one_vote():
    rows = [dict(status="matched", criterion_group="activity", contribution=w) for w in [1, 1, 2, 2]]
    assert group_totals(rows) == (1, 2)
    assert group_totals(rows + [dict(status="unavailable", criterion_group="other", contribution=100)]) == (1, 2)


@pytest.mark.parametrize("profiles,eligible,top", [(0, 0, 0), (1, 0, 0), (2, 1, 1), (95, 49, 49), (96, 50, 50), (120, 60, 50)])
def test_top50_boundaries(profiles, eligible, top, config):
    scores = evaluate(profiles, config)[0]
    ranked, selected = rank_scores(scores, "test")
    assert len(selected) == top
    assert sum(r["rank"] is not None for r in ranked) == eligible
    assert [r["rank"] for r in selected] == list(range(1, top + 1))
    assert all(r["matched_criteria_count"] >= 1 and r["eligibility_status"] == "eligible" for r in selected)


def test_shuffle_and_ties_are_stable(config):
    people, segments = generate_rows(recipe(profiles=30))
    expected = _score_profiles(people, segments, config)
    random.Random(3).shuffle(people)
    random.Random(7).shuffle(segments)
    assert _score_profiles(people, segments, config) == expected
    shuffled = expected[0][:]
    random.Random(19).shuffle(shuffled)
    assert rank_scores(shuffled, "test") == rank_scores(expected[0], "test")
    ranked, _ = rank_scores(shuffled, "test")
    assert [r["profile_id"] for r in ranked[:3]] == [f"demo_profile_{i:06d}" for i in [5, 15, 25]]


def test_duplicate_witnesses_are_deduplicated(config):
    people, segments = generate_rows(recipe(profiles=6))
    assert _score_profiles(people, segments + segments, config) == _score_profiles(people, segments, config)
    conflicting = dict(segments[0], origin_iata="XXX")
    with pytest.raises(ValueError, match="Противоречивые дубли"):
        _score_profiles(people, segments + [conflicting], config)


def test_nulls_and_source_coverage_never_create_rank(config):
    scores, explanations, features, coverage = evaluate(10, config)
    assert [scores[i]["eligibility_status"] for i in [6, 7, 8, 9]] == ["insufficient_data"] * 4
    assert features[7]["trip_rate_30_days"] is None
    assert features[9]["trip_rate_30_days"] is None
    assert features[8]["repeated_route_share"] is None
    assert features[8]["trip_rate_30_days"] == 6
    assert all(r["value"] is None and r["contribution"] == 0 for r in explanations
               if r["status"] in {"unavailable", "disabled"})
    assert all(c["reason"] for c in coverage if not c["available"])
    ranked, _ = rank_scores(scores, "test")
    assert all(r["rank"] is None for r in ranked if r["eligibility_status"] != "eligible")


def test_route_denominator_and_window_are_explicit(config):
    people, segments = generate_rows(recipe(profiles=4))
    p = people[3]
    rows = [r for r in segments if r["profile_id"] == p["profile_id"]]
    rows[0] = dict(rows[0], origin_iata=None, destination_iata=None)
    scores, explanations, features, _ = _score_profiles([p], rows, config)
    assert features[0]["known_route_segments"] == 9
    assert features[0]["repeated_route_share"] == round(1 - 2 / 9, 12)
    assert next(r for r in explanations if r["criterion_id"] == "M03")["used_observations"] == 9
    outside = dict(rows[0], segment_id="outside", departure_date=date(2025, 12, 31))
    with_outside = _score_profiles([p], rows + [outside], config)
    assert with_outside[0] == scores and with_outside[1] == explanations
    assert with_outside[2][0]["total_segments"] == 11


def test_day_ties_and_missing_dates_do_not_invent_order(config):
    people, segments = generate_rows(recipe(profiles=5))
    p = people[4]
    rows = [dict(r, departure_date=date(2026, 1, 1)) for r in segments if r["profile_id"] == p["profile_id"]]
    scores, explanations, features, _ = _score_profiles([p], rows, config)
    assert features[0]["comparable_pairs"] == 0 and features[0]["quick_return_share"] is None
    assert scores[0]["eligibility_status"] == "insufficient_data"
    assert next(r for r in explanations if r["criterion_id"] == "M02")["status"] == "unavailable"
    rows = [r for r in segments if r["profile_id"] == p["profile_id"]]
    rows[2] = dict(rows[2], departure_date=None, departure_precision="unknown")
    reasons = _score_profiles([p], rows, config)[1]
    assert "соседство событий не установлено" in next(r for r in reasons if r["criterion_id"] == "M02")["explanation_ru"]


@pytest.mark.parametrize("change", ["identity", "window"])
def test_identity_and_short_window_are_availability_failures(config, change):
    people, segments = generate_rows(recipe(profiles=5))
    p = people[4]
    if change == "identity":
        p["identity_status"] = "ambiguous"
    else:
        p["window_end"] = date(2026, 1, 30)
    scores, reasons, _, _ = _score_profiles([p], [r for r in segments if r["profile_id"] == p["profile_id"]], config)
    assert scores[0]["eligibility_status"] == "insufficient_data"
    assert all(r["status"] in {"unavailable", "disabled"} for r in reasons)


def test_explanations_have_values_thresholds_window_and_own_evidence(config):
    people, segments = generate_rows(recipe(profiles=6))
    _, reasons, _, _ = _score_profiles(people, segments, config)
    owners = {s["segment_id"]: s["profile_id"] for s in segments}
    for row in reasons:
        assert row["window_start"].isoformat() in row["explanation_ru"]
        assert row["window_end"].isoformat() in row["explanation_ru"]
        assert all(owners[key] == row["profile_id"] for key in row["evidence_segment_ids"])
        if row["status"] == "matched":
            assert row["value"] >= row["threshold"]
            assert row["evidence_segment_ids"] and "условие ≥" in row["explanation_ru"]
            assert "Возможные обычные объяснения" in row["explanation_ru"]
    m1 = next(r for r in reasons if r["profile_id"].endswith("000002") and r["criterion_id"] == "M01")
    assert "6 перелётов / 30 дней; условие ≥ 6" in m1["explanation_ru"]


@pytest.mark.parametrize("mutation", ["nan", "weight", "feature", "unknown", "duplicate", "graph", "coverage", "mode", "bool"])
def test_bad_configuration_is_rejected(tmp_path, mutation):
    config = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    if mutation == "nan":
        config["criteria"][0]["threshold"] = float("nan")
    elif mutation == "weight":
        config["criteria"][0]["weight"] = -1
    elif mutation == "feature":
        config["criteria"][0]["feature"] = "nationality"
    elif mutation == "unknown":
        config["criteria"][0]["id"] = "M99"
    elif mutation == "duplicate":
        config["criteria"].append(copy.deepcopy(config["criteria"][0]))
    elif mutation == "graph":
        config["criteria"][-1]["enabled"] = True
    elif mutation == "coverage":
        config["coverage"]["min_available_groups"] = 2
    elif mutation == "mode":
        config["data_mode"] = "real"
    elif mutation == "bool":
        config["criteria"][0]["threshold"] = True
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError):
        load_model(path)


def test_duplicate_yaml_keys_rejected(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(MODEL.read_text(encoding="utf-8") + "\nversion: rules-demo-1.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Повторный ключ"):
        load_model(path)


def test_disabled_group_changes_comparison_set_explicitly(tmp_path):
    config = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    config["criteria"][2] = dict(id="M03", enabled=False)
    config["coverage"]["min_available_groups"] = 2
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    config, _ = load_model(path)
    scores, reasons, _, _ = evaluate(6, config)
    assert scores[4]["enabled_criteria_count"] == 2 and scores[4]["matched_criteria_count"] == 2
    assert all(r["status"] == "disabled" and r["value"] is None for r in reasons if r["criterion_id"] == "M03")


@pytest.mark.parametrize("seed,count", [(-1, 10), (2**32, 10), (True, 10), (1, 501), (1, -1)])
def test_generator_bounds(seed, count):
    with pytest.raises(ValueError):
        recipe(seed, count)


def test_generator_replay_rejects_relabelled_real_rows(tmp_path, config):
    directory = create_demo(tmp_path, profiles=6)
    people = read(directory / "profiles.parquet")
    people[0]["display_name"] = "Copied passenger"
    rewrite_table(directory, "profiles.parquet", people)
    # Both synthetic mode and fresh physical checksums are present: neither is sufficient.
    with pytest.raises(ValueError, match="не воспроизводятся"):
        score_demo(directory, config)
    with pytest.raises(ValueError):
        create_demo(tmp_path, profiles=6)


@pytest.mark.parametrize("kind", ["processed", "label_only", "forged_recipe", "forged_logical_hash", "unhashed_change", "oversize"])
def test_non_demo_inputs_rejected(tmp_path, kind):
    if kind == "processed":
        (tmp_path / "features.parquet").write_text("real T15 output", encoding="utf-8")
        directory = tmp_path
    elif kind == "label_only":
        directory = tmp_path
        write_json(directory / "demo_manifest.json", dict(data_mode="synthetic_demo"))
    else:
        directory = create_demo(tmp_path, profiles=2)
        path = directory / "demo_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if kind == "forged_recipe":
            manifest["recipe"]["generator"] = "real_data_wrapper"
        elif kind == "forged_logical_hash":
            manifest["logical_sha256"] = "0" * 64
        elif kind == "oversize":
            manifest["recipe"]["profiles"] = 1000000000
        else:
            with (directory / "segments.parquet").open("ab") as stream:
                stream.write(b"changed")
        write_json(path, manifest)
    with pytest.raises(ValueError):
        validate_demo(directory)


def test_physical_shuffle_keeps_logical_input_and_outputs(tmp_path):
    directory = create_demo(tmp_path / "demo", profiles=20)
    manifest, people, segments = validate_demo(directory)
    report, _ = run(directory, MODEL, artifacts_dir=tmp_path / "out")
    for name in TABLES:
        rows = read(directory / name)
        random.Random(3).shuffle(rows)
        rewrite_table(directory, name, rows)
    assert validate_demo(directory)[0]["logical_sha256"] == logical_hash(people, segments) == manifest["logical_sha256"]
    again, _ = run(directory, MODEL, artifacts_dir=tmp_path / "out")
    assert again == report


@pytest.mark.parametrize("profiles", [0, 120])
def test_snapshot_is_self_contained_typed_and_reusable(tmp_path, profiles):
    directory = create_demo(tmp_path / "demo", profiles=profiles)
    assert directory == create_demo(tmp_path / "demo", profiles=profiles)
    report, summary = run(directory, MODEL, artifacts_dir=tmp_path / "out")
    output = tmp_path / "out" / report["run_id"]
    manifest = json.loads((output / "run_manifest.json").read_text(encoding="utf-8"))
    checksums = {name: fingerprint(output / name) for name in manifest["files"]}
    assert manifest["files"] == checksums
    for name, schema in OUTPUT_TABLES.items():
        file = pq.ParquetFile(output / name)
        assert file.schema_arrow.equals(schema, check_metadata=False)
        assert file.schema_arrow.metadata[b"data_mode"] == b"synthetic_demo"
    assert len(read(output / "top50.parquet")) == min(50, profiles // 2)
    assert report["criteria_rows"] == profiles * 7
    assert not report["ui_ready"] and report["acceptance"] == "pending_T20_review"
    assert (output / "input/profiles.parquet").stat().st_ino != (directory / "profiles.parquet").stat().st_ino
    assert run(directory, MODEL, output=output) == (report, summary)
    assert {name: fingerprint(output / name) for name in checksums} == checksums
    assert len(list((tmp_path / "out").glob("*.partial"))) == 0


def test_output_corruption_is_not_silently_rebuilt(tmp_path):
    directory = create_demo(tmp_path / "demo", profiles=6)
    output = tmp_path / "result"
    run(directory, MODEL, output=output)
    (output / "top50.parquet").write_bytes(b"broken")
    with pytest.raises(ValueError, match="Изменён файл снимка"):
        run(directory, MODEL, output=output)
    assert (output / "top50.parquet").read_bytes() == b"broken"


def test_config_change_creates_another_version_and_preserves_old(tmp_path):
    directory = create_demo(tmp_path / "demo", profiles=6)
    before, _ = run(directory, MODEL, artifacts_dir=tmp_path / "out")
    config = yaml.safe_load(MODEL.read_text(encoding="utf-8"))
    config["criteria"][0]["threshold"] = 8
    path = tmp_path / "model.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    after, _ = run(directory, path, artifacts_dir=tmp_path / "out")
    assert after["run_id"] != before["run_id"]
    assert after["eligibility"]["eligible"] == 4
    with pytest.raises(ValueError, match="другой конфигурации"):
        run(directory, path, output=tmp_path / "out" / before["run_id"])


def test_existing_and_overlapping_outputs_are_not_overwritten(tmp_path):
    directory = create_demo(tmp_path / "demo", profiles=2)
    with pytest.raises(ValueError, match="пересекаться"):
        run(directory, MODEL, output=directory / "out")
    output = tmp_path / "existing"
    output.mkdir()
    (output / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="завершённым снимком"):
        run(directory, MODEL, output=output)
    assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_concurrent_writer_and_interruption_leave_no_partial_snapshot(tmp_path, monkeypatch):
    directory = create_demo(tmp_path / "demo", profiles=2)
    output = tmp_path / "out" / "result"
    output.parent.mkdir()
    with OutputLock(output.parent / ".t17-ranking.lock", "test"):
        with pytest.raises(RuntimeError, match="другим процессом"):
            run(directory, MODEL, output=output)
    from yoyoflot.models import ranking
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(ranking, "_score_profiles", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(directory, MODEL, output=output)
    assert not output.exists() and not list(output.parent.glob("*.partial"))


def test_cli_rejects_removed_generation_options(tmp_path, capsys):
    with pytest.raises(SystemExit) as error:
        main(["train", "--demo", "--profiles", "6"])
    assert error.value.code == 2
    assert not list(tmp_path.iterdir())
    assert main(["train", "--input", str(tmp_path), "-q"]) == 1


def test_script_help_and_other_working_directory(tmp_path):
    script = ROOT / "scripts/run_t17.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    help_run = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path,
                              env=env, capture_output=True, text=True, encoding="utf-8")
    assert help_run.returncode == 0 and "--input" in help_run.stdout and "--demo" not in help_run.stdout
    result = subprocess.run([sys.executable, str(script), "--input", str(tmp_path / "missing")], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 1 and not list(tmp_path.iterdir())
