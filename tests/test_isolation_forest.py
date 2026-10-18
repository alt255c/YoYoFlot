import copy
import json
import math
import os
import random
import subprocess
import sys
from datetime import date
from pathlib import Path

import joblib
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.cli import main
from yoyoflot.models.demo import (END, START, PROFILE_SCHEMA, VARIED_GENERATOR, create_demo,
                                  generate_rows, recipe, validate_demo, write_json)
from yoyoflot.models.if_artifact import TABLE_SCHEMAS, _rank, run
from yoyoflot.models.if_inputs import check_alignment, prepare_dataset
from yoyoflot.models.isolation_forest import (columns, experimental_rows, fit, load_config, make_matrix, score)
from yoyoflot.models.ranking import midrank_percentiles
from yoyoflot.models.rule_score import _score_profiles, load_model
from yoyoflot.normalize.locking import OutputLock
from yoyoflot.provenance import fingerprint

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs/isolation_forest.yaml"
RULES, GRAPH = ROOT / "configs/model.yaml", ROOT / "configs/graph.yaml"


@pytest.fixture
def config():
    value, _ = load_config(CONFIG)
    value["forest"].update(n_estimators=16, max_samples=32)
    value["reference"].update(training_profiles=30, calibration_profiles=20, min_training_rows=8, min_calibration_rows=8)
    return value


def config_file(tmp_path, config, name="if.yaml"):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def source(tmp_path, seed=17, profiles=20):
    return create_demo(tmp_path / "sources", seed, profiles, VARIED_GENERATOR)


def execute(tmp_path, config, input_path=None, output=None, **kwargs):
    return run(input_path or source(tmp_path), config_file(tmp_path, config), RULES, GRAPH,
               output=output, artifacts_dir=tmp_path / "artifacts", work_dir=tmp_path / "work", **kwargs)


def read(path):
    return pq.ParquetFile(path).read().to_pylist()


def core_rows(role, count=40, seed=1):
    rng = random.Random(seed)
    return [dict(role=role, dataset_sha256=role, profile_id=f"{role}_{i:04d}",
                 feature_names=list(load_config(CONFIG)[0]["features"]), values=[6 + rng.random(), 0.1 + rng.random() * 0.2, 0.4 + rng.random() * 0.2],
                 feature_available=[True] * 3, eligible=True, exclusion_reason=None, window_start=START, window_end=END)
            for i in range(count)]


def rules_for(rows):
    return [dict(profile_id=r["profile_id"], display_name=r["profile_id"], matched_criteria_count=i % 4,
                 rule_score=float(i % 4), eligibility_status="eligible" if i % 4 else "no_matches") for i, r in enumerate(rows)]


def raw_fixture(count=20, seed=17):
    people, segments = generate_rows(recipe(seed, count, VARIED_GENERATOR))
    rules, reasons, features, coverage = _score_profiles(people, segments, load_model(RULES)[0])
    return people, segments, rules, features, coverage


@pytest.mark.parametrize("seed,count", [(17, 0), (17, 1), (42, 120), (101, 500)])
def test_varied_generator_is_bounded_replayable_and_independent(tmp_path, seed, count):
    path = source(tmp_path, seed, count)
    manifest, people, segments = validate_demo(path)
    assert manifest["recipe"]["generator"] == VARIED_GENERATOR
    assert len(people) == count and len(segments) <= 60 * count
    assert all(r["departure_date"] is None or START <= r["departure_date"] <= END for r in segments)
    assert len({r["segment_id"] for r in segments}) == len(segments)
    assert source(tmp_path, seed, count) == path
    other, _ = generate_rows(recipe(seed + 1, count, VARIED_GENERATOR))
    assert not {p["profile_id"] for p in people}.intersection(p["profile_id"] for p in other)


def test_varied_reference_has_real_numeric_diversity(config):
    _, _, rules, features, coverage = raw_fixture(300, 101)
    matrix = make_matrix("training", "fictional", features, coverage, rules, config)
    vectors = {tuple(r["values"]) for r in matrix if r["eligible"]}
    assert len(vectors) > 100
    assert len(matrix) == 300 and sum(r["eligible"] for r in matrix) == 180


def test_midrank_frozen_reference_ties_and_outside_range():
    reference = [1.0, 1.0, 2.0, 4.0]
    values = [-1, 1, 1.5, 2, 4, 5]
    assert midrank_percentiles(reference, values) == [0, 0.25, 0.5, 0.625, 0.875, 1]
    assert midrank_percentiles([2, 2, 2], [2, 2]) == [0.5, 0.5]
    assert midrank_percentiles(reference[::-1], [2]) == [0.625]
    assert midrank_percentiles([(2, 100), (3, 1), (3, 1)], [(2, 100), (3, 1)]) == [1 / 6, 2 / 3]


@pytest.mark.parametrize("reference,values", [([], [1]), ([float("nan")], [1]), ([1], [float("inf")]), ([(1, 2)], [1]), ([True], [1])])
def test_invalid_percentile_inputs_fail(reference, values):
    with pytest.raises(ValueError):
        midrank_percentiles(reference, values)


def test_incomplete_rows_are_excluded_without_zero_imputation(config):
    _, _, rules, features, coverage = raw_fixture(10)
    matrix = make_matrix("evaluation", "fictional", features, coverage, rules, config)
    assert [r["eligible"] for r in matrix] == [True] * 6 + [False] * 4
    assert all(r["exclusion_reason"] for r in matrix if not r["eligible"])
    assert all(any(v is None for v in r["values"]) for r in matrix if not r["eligible"])
    assert any(v == 0 for v in matrix[0]["values"])  # Known zero remains a legitimate value.
    coverage[0]["available"] = False
    changed = make_matrix("evaluation", "fictional", features, coverage, rules, config)
    assert not changed[0]["eligible"] and changed[0]["feature_available"][0] is False


@pytest.mark.parametrize("kind", ["nan", "inf", "duplicate", "missing_coverage", "wrong_window", "mismatched_ids"])
def test_invalid_matrix_inputs_block_learning(config, kind):
    _, _, rules, features, coverage = raw_fixture(10)
    if kind in {"nan", "inf"}:
        features[0][config["features"][0]] = float(kind)
    elif kind == "duplicate":
        features.append(copy.deepcopy(features[0]))
    elif kind == "missing_coverage":
        coverage.pop(0)
    elif kind == "wrong_window":
        features[0]["window_end"] = date(2026, 3, 1)
    else:
        rules[0]["profile_id"] = "other"
    with pytest.raises(ValueError):
        make_matrix("evaluation", "fictional", features, coverage, rules, config)


def test_identity_and_quality_fields_never_enter_model(config):
    _, _, rules, features, coverage = raw_fixture(10)
    names = columns(config)
    matrix = make_matrix("evaluation", "fictional", features, coverage, rules, config)
    assert all(r["feature_names"] == names and len(r["values"]) == 3 for r in matrix)
    assert not {"profile_id", "source_coverage", "covered_days", "total_segments", "display_name"}.intersection(names)


def test_seed_and_input_permutations_reproduce_forest_and_percentiles(config):
    training, calibration = core_rows("training", seed=1), core_rows("calibration", seed=2)
    rules = rules_for(calibration)
    first, _ = fit(training, calibration, rules, config)
    probe = core_rows("evaluation", seed=3)
    expected = score(first, probe)
    random.Random(3).shuffle(training); random.Random(4).shuffle(calibration)
    second, _ = fit(training, calibration, rules, config)
    assert score(second, probe) == expected
    assert first["if_reference"] == second["if_reference"]
    assert first["rules_reference"] == second["rules_reference"]
    assert all(np.array_equal(a.tree_.threshold, b.tree_.threshold) for a, b in zip(first["forest"].estimators_, second["forest"].estimators_))


def test_anomaly_direction_and_calibration_never_refit_forest(config):
    training, calibration = core_rows("training", seed=11), core_rows("calibration", seed=22)
    first, _ = fit(training, calibration, rules_for(calibration), config)
    probe = core_rows("evaluation", count=2)
    probe[0]["values"] = [6.5, 0.2, 0.5]
    probe[1]["values"] = [100, 1, 1]
    normal, extreme = score(first, probe)
    assert extreme > normal
    modified = copy.deepcopy(calibration)
    for row in modified:row["values"] = [100, 1, 1]
    second, _ = fit(training, modified, rules_for(modified), config)
    assert score(first, probe) == score(second, probe)
    assert first["if_reference"] != second["if_reference"]


@pytest.mark.parametrize("kind", ["order", "window", "unavailable", "availability_mask", "width", "nonfinite"])
def test_scoring_rejects_wrong_model_contract(config, kind):
    training, calibration = core_rows("training"), core_rows("calibration", seed=2)
    bundle, _ = fit(training, calibration, rules_for(calibration), config)
    probe = core_rows("evaluation", count=1)
    if kind == "order":probe[0]["feature_names"].reverse()
    elif kind == "window":probe[0]["window_end"] = date(2026, 3, 1)
    elif kind == "unavailable":probe[0]["eligible"] = False
    elif kind == "availability_mask":probe[0]["feature_available"][0] = False
    elif kind == "width":probe[0]["values"].pop()
    else:probe[0]["values"][0] = float("nan")
    with pytest.raises(ValueError):score(bundle, probe)


@pytest.mark.parametrize("kind", ["window", "duplicate_rules", "missing_rules", "nonfinite_rules"])
def test_bad_calibration_contract_is_rejected_before_fit(config, monkeypatch, kind):
    from sklearn.ensemble import IsolationForest
    train, calib = core_rows("training"), core_rows("calibration", seed=2)
    rules = rules_for(calib)
    if kind == "window":calib[0]["window_end"] = date(2026, 3, 1)
    elif kind == "duplicate_rules":rules.append(copy.deepcopy(rules[0]))
    elif kind == "missing_rules":rules.pop()
    else:rules[0]["rule_score"] = float("nan")
    def forbidden(*args, **kwargs):raise AssertionError("Invalid data reached fit")
    monkeypatch.setattr(IsolationForest, "fit", forbidden)
    with pytest.raises(ValueError):fit(train, calib, rules, config)


@pytest.mark.parametrize("kind", ["disabled", "training", "calibration", "constant", "float32_collapse"])
def test_unavailable_experiment_preserves_reason_and_has_no_model(config, kind):
    train, calib = core_rows("training"), core_rows("calibration", seed=2)
    if kind == "disabled":config["enabled"] = False
    elif kind == "training":train = train[:1]
    elif kind == "calibration":calib = calib[:1]
    elif kind == "constant":
        for r in train:r["values"] = [6.0, 0.2, 0.5]
    else:
        for i,r in enumerate(train):r["values"] = [6.0 + i * 1e-12, 0.2, 0.5]
    bundle, metadata = fit(train, calib, rules_for(calib), config)
    assert bundle is None and not metadata["available"] and metadata["reason"]


def test_graph_alignment_checks_source_window_version_and_status(config):
    config["include_graph"] = True
    rule = dict(input_logical_sha256="abc", data_mode="synthetic_demo", model_version="rules-demo-1.0", as_of=config["as_of"])
    graph = dict(input_logical_sha256="abc", data_mode="synthetic_demo", graph_version="graph-demo-1.0", config=dict(as_of=config["as_of"]), m07_status="disabled")
    check_alignment("abc", rule, graph, config)
    for change in [dict(input_logical_sha256="other"), dict(config=dict(as_of="2026-03-01")), dict(m07_status="matched"), dict(data_mode="real")]:
        with pytest.raises(ValueError):check_alignment("abc", rule, dict(graph, **change), config)


def test_score_percentiles_are_batch_independent_and_not_rule_explanations(config):
    train, calib = core_rows("training"), core_rows("calibration", seed=2)
    bundle, meta = fit(train, calib, rules_for(calib), config)
    evaluation = core_rows("evaluation", count=5, seed=3)
    rules = rules_for(evaluation)
    full = experimental_rows(evaluation, rules, bundle, meta, config, "run")
    single = experimental_rows(evaluation[:1], rules[:1], bundle, meta, config, "run")
    assert single[0] == full[0]
    assert full[0]["combined_score"] == 0.5 * full[0]["rule_percentile"] + 0.5 * full[0]["if_percentile"]
    assert "не вероятность" in full[0]["explanation_ru"] and "правил сохранены отдельно" in full[0]["explanation_ru"]


@pytest.mark.parametrize("count,top", [(0, 0), (1, 1), (49, 49), (50, 50), (61, 50)])
def test_experimental_top50_ties_and_boundaries(count, top):
    rows = [dict(profile_id=f"demo_{i:03d}", status="scored", if_score=1.0, combined_score=0.5) for i in reversed(range(count))]
    rows.append(dict(profile_id="unavailable", status="insufficient_data", if_score=None, combined_score=None))
    ranked, selected = _rank(rows, "isolation_forest")
    assert len(selected) == top and [r["profile_id"] for r in selected] == [f"demo_{i:03d}" for i in range(top)]
    assert ranked[-1]["rank"] is None


@pytest.mark.parametrize("kind", ["feature", "quality_feature", "repeat_feature", "enabled_type", "mode", "seed", "same_seeds", "trees", "threads", "contamination", "weights", "nan", "imputer", "date", "unknown"])
def test_bad_configs_never_reach_training(tmp_path, config, kind):
    if kind == "feature":config["features"] = ["passport"]
    elif kind == "quality_feature":config["features"] = ["source_coverage"]
    elif kind == "repeat_feature":config["features"].append(config["features"][0])
    elif kind == "enabled_type":config["enabled"] = "false"
    elif kind == "mode":config["data_mode"] = "real"
    elif kind == "seed":config["forest"]["random_state"] = True
    elif kind == "same_seeds":config["reference"]["calibration_seed"] = config["reference"]["training_seed"]
    elif kind == "trees":config["forest"]["n_estimators"] = 513
    elif kind == "threads":config["forest"]["n_jobs"] = -1
    elif kind == "contamination":config["forest"]["contamination"] = 0.1
    elif kind == "weights":config["combination"]["if_weight"] = 0.2
    elif kind == "nan":config["combination"]["if_weight"] = float("nan")
    elif kind == "imputer":config["missing_policy"] = "median"
    elif kind == "date":config["as_of"] = "2026-04-01"
    else:config["automatic_spy_labels"] = True
    with pytest.raises(ValueError):load_config(config_file(tmp_path, config))


def test_frozen_model_reload_primary_bytes_types_and_three_disjoint_roles(tmp_path, config):
    report, summary = execute(tmp_path, config)
    output = tmp_path / "artifacts" / report["run_id"]
    assert report["training_performed"] and report["reload_verified"] and not report["ui_ready"]
    assert report["fit"]["training_rows"] == 18 and report["fit"]["calibration_rows"] == 12
    for name in ["rankings.parquet", "top50.parquet"]:
        assert fingerprint(output / name) == fingerprint(output / "parents/evaluation/rules" / name)
    for name, schema in TABLE_SCHEMAS.items():
        file = pq.ParquetFile(output / name)
        assert file.schema_arrow.equals(schema, check_metadata=False)
        assert file.schema_arrow.metadata[b"data_mode"] == b"synthetic_demo"
    matrices = read(output / "matrices.parquet")
    roles = {role: {r["profile_id"] for r in matrices if r["role"] == role} for role in ["training", "calibration", "evaluation"]}
    assert len(matrices) == 70
    assert not roles["training"] & roles["calibration"] and not roles["training"] & roles["evaluation"]
    model = joblib.load(output / "model.joblib")  # Test-owned artifact, created immediately above.
    cases = [r for r in matrices if r["role"] == "evaluation" and r["eligible"]]
    expected = dict(zip((r["profile_id"] for r in cases), score(model, cases)))
    for row in read(output / "if_rankings.parquet"):
        assert row["if_score"] == expected.get(row["profile_id"])
        if row["status"] != "scored":assert row["rank"] is None
    assert all(r["if_score"] is None and r["combined_score"] is None for r in read(output / "rankings.parquet"))
    assert execute(tmp_path, config) == (report, summary)


def test_evaluation_changes_never_change_learned_model_or_calibration(tmp_path, config):
    first, _ = execute(tmp_path, config)
    other_input = source(tmp_path, seed=99)
    second, _ = execute(tmp_path, config, input_path=other_input)
    first_path, second_path = [tmp_path / "artifacts" / r["run_id"] for r in [first, second]]
    assert first["run_id"] != second["run_id"]
    assert fingerprint(first_path / "model.joblib") == fingerprint(second_path / "model.joblib")


@pytest.mark.parametrize("enabled", [False, True])
def test_disabled_or_insufficient_reference_keeps_primary_and_null_experiment(tmp_path, config, enabled):
    config["enabled"] = enabled
    if enabled:config["reference"]["training_profiles"] = 0
    report, _ = execute(tmp_path, config)
    output = tmp_path / "artifacts" / report["run_id"]
    assert not report["training_performed"] and not (output / "model.joblib").exists()
    assert (output / "rankings.parquet").exists()
    assert all(r["if_score"] is r["combined_score"] is r["rank"] is None for r in read(output / "if_rankings.parquet"))
    assert not read(output / "if_top50.parquet")


def test_empty_evaluation_is_typed_and_does_not_prevent_training(tmp_path, config):
    report, _ = execute(tmp_path, config, input_path=source(tmp_path, profiles=0))
    output = tmp_path / "artifacts" / report["run_id"]
    assert report["training_performed"] and report["profiles"] == report["if_scored"] == 0
    assert not read(output / "if_rankings.parquet") and not read(output / "rankings.parquet")


def test_graph_experiment_is_explicit_and_m07_does_not_gain_a_vote(tmp_path, config):
    config["include_graph"] = True
    report, _ = execute(tmp_path, config)
    output = tmp_path / "artifacts" / report["run_id"]
    assert report["graph_status"] == "experimental_features" and report["m07_status"] == "disabled"
    assert len(report["fit"]["feature_names"]) == 5
    assert (output / "parents/evaluation/graph/graph_features.parquet").exists()
    primary = {r["profile_id"]: r for r in read(output / "rankings.parquet")}
    assert all(r["matched_criteria_count"] == primary[r["profile_id"]]["matched_criteria_count"] for r in read(output / "if_rankings.parquet"))


def test_graph_disabled_does_not_require_graph_config_or_stage(tmp_path, config):
    evaluation = source(tmp_path)
    report, _ = run(evaluation, config_file(tmp_path, config), RULES, tmp_path / "missing.yaml",
                    artifacts_dir=tmp_path / "artifacts", work_dir=tmp_path / "work")
    assert report["graph_status"] == "disabled_by_config" and report["training_performed"]


def test_graph_window_mismatch_is_rejected_before_fit(tmp_path, config):
    config.update(include_graph=True, as_of="2026-03-01")
    with pytest.raises(ValueError, match="as_of"):
        execute(tmp_path, config)
    assert not (tmp_path / "artifacts").exists()


def test_t17_v1_evaluation_still_works_with_v2_reference(tmp_path, config):
    evaluation = create_demo(tmp_path / "old-demo", seed=17, profiles=20)
    report, _ = execute(tmp_path, config, input_path=evaluation)
    assert report["training_performed"] and report["profiles"] == 20


def test_overlapping_seed_and_relabelled_real_input_are_blocked(tmp_path, config):
    evaluation = source(tmp_path)
    with pytest.raises(ValueError, match="разные seed"):
        execute(tmp_path, config, input_path=evaluation, reference=evaluation)
    people = read(evaluation / "profiles.parquet")
    people[0]["display_name"] = "Copied person"
    pq.write_table(pa.Table.from_pylist(people, schema=PROFILE_SCHEMA), evaluation / "profiles.parquet")
    manifest = json.loads((evaluation / "demo_manifest.json").read_text(encoding="utf-8"))
    manifest["files"]["profiles.parquet"] = fingerprint(evaluation / "profiles.parquet")
    write_json(evaluation / "demo_manifest.json", manifest)
    with pytest.raises(ValueError, match="не воспроизводятся"):
        execute(tmp_path, config, input_path=evaluation)


def test_cached_output_never_unpickles_and_corruption_is_not_overwritten(tmp_path, config, monkeypatch):
    report, _ = execute(tmp_path, config)
    output = tmp_path / "artifacts" / report["run_id"]
    def forbidden(*args, **kwargs):raise AssertionError("Unpickled cached model")
    monkeypatch.setattr(joblib, "load", forbidden)
    assert execute(tmp_path, config)[0] == report
    (output / "model.joblib").write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="Изменён файл"):
        execute(tmp_path, config)
    assert (output / "model.joblib").read_bytes() == b"corrupted"


def test_unlisted_file_is_rejected_and_unrelated_output_is_preserved(tmp_path, config):
    report, _ = execute(tmp_path, config)
    output = tmp_path / "artifacts" / report["run_id"]
    (output / "unlisted.txt").write_text("extra", encoding="utf-8")
    with pytest.raises(ValueError, match="незаявленные"):
        execute(tmp_path, config)
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="завершённым снимком"):
        execute(tmp_path, config, output=unrelated)
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_interruption_and_concurrent_writer_never_publish_partial_output(tmp_path, config, monkeypatch):
    from yoyoflot.models import if_artifact
    evaluation = source(tmp_path)
    out = tmp_path / "artifacts" / "new"
    out.parent.mkdir()
    with OutputLock(out.parent / ".t19-training.lock", "test"):
        with pytest.raises(RuntimeError, match="другим процессом"):
            execute(tmp_path, config, input_path=evaluation, output=out)
    def interrupted(*args, **kwargs):raise KeyboardInterrupt
    monkeypatch.setattr(if_artifact, "fit", interrupted)
    with pytest.raises(KeyboardInterrupt):execute(tmp_path, config, input_path=evaluation, output=out)
    assert not out.exists() and not list(out.parent.glob("*.partial"))


def test_as_of_features_do_not_use_future_counts(config):
    people, segments = generate_rows(recipe(101, 40, VARIED_GENERATOR))
    cutoff = date(2026, 3, 1)
    people = [dict(p, window_end=cutoff) for p in people]
    parameters = load_model(RULES)[0]
    all_values = _score_profiles(people, segments, parameters)
    prefix = _score_profiles(people, [s for s in segments if s["departure_date"] is None or s["departure_date"] <= cutoff], parameters)
    config["as_of"] = cutoff.isoformat()
    first = make_matrix("training", "same", all_values[2], all_values[3], all_values[0], config)
    second = make_matrix("training", "same", prefix[2], prefix[3], prefix[0], config)
    assert first == second


def test_cli_script_help_and_actual_training_from_other_cwd(tmp_path, config, capsys):
    assert main(["train-if"]) == 2
    capsys.readouterr()
    cfg = config_file(tmp_path, config)
    script = ROOT / "scripts/run_t19.py"
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    result = subprocess.run([sys.executable, str(script), "--profiles", "10", "--config", str(cfg),
                             "--demo-dir", str(tmp_path / "evaluation"), "--work-dir", str(tmp_path / "work"),
                             "--artifacts-dir", str(tmp_path / "artifacts")], cwd=tmp_path,
                            env=env, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    assert "Обучение IF: выполнено" in result.stdout
    help_run = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path, env=env, capture_output=True, text=True, encoding="utf-8")
    assert help_run.returncode == 0 and "--calibration" in help_run.stdout


def test_independent_persisted_audit_script_creates_its_own_model(tmp_path):
    report = tmp_path / "audit.json"
    result = subprocess.run([sys.executable, str(ROOT / "scripts/verify_t19_demo.py"), "--profiles", "20", "--report", str(report)],
                            cwd=tmp_path, env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                            capture_output=True, text=True, encoding="utf-8")
    assert result.returncode == 0, result.stderr
    audit = json.loads(report.read_text(encoding="utf-8"))
    assert audit["audit"]["result"] == "passed" and audit["report"]["reload_verified"]
    assert "calibration_only_CDF_counting_oracle" in audit["audit"]["checks"]
