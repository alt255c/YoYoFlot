"""Create a fresh fictional experiment, then audit its persisted model/tables.

No argument accepts an existing pickle. Only this invocation's newly created
private snapshot is deserialized. Reports are diagnostic, not efficacy metrics.
"""
import argparse
import json
import math
import platform
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import joblib
import numpy as np
import pyarrow.parquet as pq
import yaml

from yoyoflot.models.demo import VARIED_GENERATOR, create_demo, write_json
from yoyoflot.models.if_artifact import TABLE_SCHEMAS, run
from yoyoflot.provenance import fingerprint


def read(path):
    return pq.ParquetFile(path).read().to_pylist()


def audit_fresh(path):
    manifest = json.loads((path / "run_manifest.json").read_text(encoding="utf-8"))
    assert all(fingerprint(path / name) == checksum for name, checksum in manifest["files"].items())
    assert {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()} == set(manifest["files"]) | {"run_manifest.json"}
    for name, schema in TABLE_SCHEMAS.items():
        file = pq.ParquetFile(path / name)
        assert file.schema_arrow.equals(schema, check_metadata=False)
        assert file.schema_arrow.metadata[b"run_id"] == manifest["run_id"].encode("ascii")
    matrices = read(path / "matrices.parquet")
    roles = {role: [r for r in matrices if r["role"] == role] for role in ["training", "calibration", "evaluation"]}
    ids = {role: {r["profile_id"] for r in rows} for role, rows in roles.items()}
    assert sum(map(len, ids.values())) == len(set.union(*ids.values()))
    bundle = joblib.load(path / "model.joblib")  # Fresh model created by this invocation, never a supplied path.
    raw = {}
    for role, rows in roles.items():
        cases = [r for r in rows if r["eligible"]]
        assert all(r["feature_names"] == bundle["feature_names"] and all(r["feature_available"]) for r in cases)
        x = np.asarray([r["values"] for r in cases], dtype=np.float32).reshape(len(cases), len(bundle["feature_names"]))
        values = -bundle["forest"].score_samples(x) if cases else []
        raw[role] = dict(zip((r["profile_id"] for r in cases), map(float, values)))
    calibration = list(raw["calibration"].values())
    rules = {r["profile_id"]: r for r in read(path / "parents/calibration/rules/rule_scores.parquet")}
    rule_reference = [(rules[pid]["matched_criteria_count"], rules[pid]["rule_score"]) for pid in raw["calibration"]]
    assert bundle["if_reference"] == sorted(calibration)
    assert bundle["rules_reference"] == sorted(rule_reference)
    def percentile(reference, value):
        # Independent counting oracle: do not call the production percentile helper.
        return (sum(v < value for v in reference) + 0.5 * sum(v == value for v in reference)) / len(reference)
    weights = manifest["request"]["config"]["combination"]
    for filename in ["if_rankings.parquet", "combined_rankings.parquet", "reference_scores.parquet"]:
        for row in read(path / filename):
            role = row.get("role", "evaluation")
            expected = raw[role].get(row["profile_id"])
            assert row["if_score"] == expected
            if expected is None:
                assert row["combined_score"] is row["if_percentile"] is row["rule_percentile"] is None
                if "rank" in row:assert row["rank"] is None
            else:
                ip = percentile(calibration, expected)
                rp = percentile(rule_reference, (row["matched_criteria_count"], row["rule_score"]))
                assert row["if_percentile"] == ip and row["rule_percentile"] == rp
                assert row["combined_score"] == weights["if_weight"] * ip + weights["rules_weight"] * rp
                assert math.isfinite(expected)
    for stem, key in [("if", "if_score"), ("combined", "combined_score")]:
        full, top = read(path / f"{stem}_rankings.parquet"), read(path / f"{stem}_top50.parquet")
        expected = sorted((r for r in full if r["status"] == "scored"), key=lambda r: (-r[key], r["profile_id"]))
        assert [r["rank"] for r in expected] == list(range(1, len(expected) + 1))
        assert top == expected[:50]
    for name in ["rankings.parquet", "top50.parquet"]:
        assert fingerprint(path / name) == fingerprint(path / "parents/evaluation/rules" / name)
    assert all(r["if_score"] is r["combined_score"] is None for r in read(path / "rankings.parquet"))
    return dict(result="passed", checks=["persisted_schemas_hashes_and_inventory", "disjoint_roles",
        "reloaded_sklearn_scores_from_float32_matrices", "calibration_only_CDF_counting_oracle",
        "combination_and_tie_order", "top50_projection", "unknown_values_remain_null", "primary_byte_identity"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profiles", type=int, default=120)
    parser.add_argument("--include-graph", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "data/verification/t19_demo_check.json")
    args = parser.parse_args()
    verification = ROOT / "data/verification"
    verification.mkdir(parents=True, exist_ok=True)
    fresh = Path(tempfile.mkdtemp(prefix="t19-", dir=verification))
    parameters = yaml.safe_load((ROOT / "configs/isolation_forest.yaml").read_text(encoding="utf-8"))
    parameters["include_graph"] = args.include_graph
    config = fresh / "isolation_forest.yaml"
    config.write_text(yaml.safe_dump(parameters, sort_keys=False), encoding="utf-8")
    started = time.monotonic()
    evaluation = create_demo(fresh / "evaluation", 17, args.profiles, VARIED_GENERATOR)
    report, _ = run(evaluation, config, ROOT / "configs/model.yaml", ROOT / "configs/graph.yaml",
                    work_dir=fresh / "work", artifacts_dir=fresh / "artifacts")
    wall = time.monotonic() - started
    snapshot = fresh / "artifacts" / report["run_id"]
    started = time.monotonic()
    audit = audit_fresh(snapshot)
    audit_seconds = time.monotonic() - started
    peak = None
    if sys.platform == "linux":
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result = dict(report=report, wall_seconds=round(wall, 3), audit=audit, audit_seconds=round(audit_seconds, 3),
                  peak_rss_kib=peak, peak_rss_scope="whole_fresh_process_including_audit; Linux only",
                  timing_scope="generation, parent stages, training and snapshot verification after imports; audit separate",
                  snapshot=snapshot.relative_to(ROOT).as_posix(), run_manifest_sha256=fingerprint(snapshot / "run_manifest.json"),
                  model_sha256=fingerprint(snapshot / "model.joblib"), platform=platform.platform())
    args.report.parent.mkdir(parents=True, exist_ok=True)
    write_json(args.report, result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
