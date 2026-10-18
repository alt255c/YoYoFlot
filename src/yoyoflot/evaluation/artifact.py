"""Reproducible, immutable T20 experiments on generated fictional inputs."""
import csv
import json
import platform
import copy
import shutil
import tempfile
import time
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .. import __version__
from ..features.graph import load_graph_config
from ..models.demo import create_demo, digest, recipe, validate_demo, write_json
from ..models.if_artifact import run as train_run, _runtime as training_runtime
from ..models.isolation_forest import load_config as load_forest, make_matrix
from ..models.rule_score import load_model, _score_profiles
from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .audit import write_bundle
from .compare import METHODS, common_universe, keyed, method_agreement, metrics, selected, stability
from .plan import VERSION, build_plan, load_config
from .report import markdown
from .scenarios import annotations
from .storage import validate_snapshot

SCORE_SCHEMA = pa.schema([
    ("experiment", pa.string()), ("profile_id", pa.string()), ("method", pa.string()), ("score", pa.float64()),
    ("matched_criteria_count", pa.int32()), ("rule_score", pa.float64()), ("available", pa.bool_()), ("status", pa.string()),
    ("rank", pa.int32()), ("window_start", pa.date32()), ("window_end", pa.date32()), ("parent_run_id", pa.string()), ("run_id", pa.string()),
])
SCENARIO_SCHEMA = pa.schema([
    ("experiment", pa.string()), ("profile_id", pa.string()), ("case_id", pa.string()), ("title_ru", pa.string()),
    ("benchmark_label", pa.int32()), ("expected_insufficient", pa.bool_()), ("rationale_ru", pa.string()),
    ("label_scope", pa.string()), ("catalog_version", pa.string()), ("run_id", pa.string()),
])
DISAGREEMENT_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("case_id", pa.string()), ("benchmark_label", pa.int32()), ("kind", pa.string()),
    ("top_methods", pa.list_(pa.string())), ("rules_rank", pa.int32()), ("if_rank", pa.int32()), ("combined_rank", pa.int32()),
    ("if_score", pa.float64()), ("combined_score", pa.float64()), ("features_json", pa.string()),
    ("criteria_json", pa.string()), ("evidence_segment_ids", pa.list_(pa.string())), ("explanation_ru", pa.string()), ("run_id", pa.string()),
])


def read(path):
    return pq.ParquetFile(path).read().to_pylist()


def _runtime():
    package = Path(__file__).parents[1]
    files = [*package.glob("models/*.py"), *package.glob("features/graph*.py"), *Path(__file__).parent.glob("*.py"),
             package / "__init__.py", package / "provenance.py", package / "normalize/locking.py"]
    return dict(package_version=__version__, python=platform.python_version(), pyarrow=pa.__version__, training_runtime=training_runtime(),
                code_sha256=digest({p.relative_to(package).as_posix(): fingerprint(p) for p in sorted(files)}))


def collect(path, name, run_id):
    source, _, _ = validate_demo(path / "parents/evaluation/rules/input")
    primary = read(path / "rankings.parquet")
    methods = {m: [] for m in METHODS}
    for method, file, score_field in [("rules", "rankings.parquet", "rule_score"),
                                      ("isolation_forest", "if_rankings.parquet", "if_score"),
                                      ("combination", "combined_rankings.parquet", "combined_score")]:
        for row in read(path / file):
            status = row["eligibility_status"] if method == "rules" else row["status"]
            available = status in {"eligible", "no_matches"} if method == "rules" else status == "scored"
            methods[method].append(dict(experiment=name, profile_id=row["profile_id"], method=method,
                score=row[score_field], matched_criteria_count=row["matched_criteria_count"], rule_score=row["rule_score"],
                available=available, status=status, rank=row["rank"], window_start=row["window_start"], window_end=row["window_end"],
                parent_run_id=row["run_id"], run_id=run_id))
    common_universe(methods)
    return dict(path=path, methods=methods, annotations=annotations(source["recipe"]), source=source, primary=primary)


def _write_csv(path, rows, fallback):
    fields = list(rows[0]) if rows else fallback
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _cases(base, k):
    cases = keyed(base["annotations"])
    rows = []
    for method in METHODS:
        top = {r["profile_id"] for r in selected(base["methods"][method], k)}
        for cid in sorted({a["case_id"] for a in cases.values()}):
            group = [r for r in base["methods"][method] if cases[r["profile_id"]]["case_id"] == cid]
            rows.append(dict(case_id=cid, method=method, k=k, profiles=len(group), available=sum(r["available"] for r in group),
                selected=sum(r["profile_id"] in top for r in group), positive_labels=sum(cases[r["profile_id"]]["benchmark_label"]==1 for r in group),
                control_selected=sum(r["profile_id"] in top and cases[r["profile_id"]]["benchmark_label"]==0 for r in group)))
    return rows


def _disagreements(base, run_id, k):
    path = base["path"] / "parents/evaluation/rules"
    facts = keyed(read(path / "features.parquet"))
    details = {}
    for row in read(path / "model_explanations.parquet"):
        details.setdefault(row["profile_id"], []).append(row)
    primary = keyed(base["primary"])
    by_method = {m: keyed(base["methods"][m]) for m in METHODS}
    tops = {m: {r["profile_id"] for r in selected(base["methods"][m], k)} for m in METHODS}
    rows = []
    for annotation in base["annotations"]:
        pid = annotation["profile_id"]
        membership = [m for m in METHODS if pid in tops[m]]
        kind = ("insufficient_data" if not by_method["rules"][pid]["available"] else
                "control_selected" if annotation["benchmark_label"]==0 and membership else
                "stress_missed" if annotation["benchmark_label"]==1 and not membership else
                "top_membership_disagreement" if membership and len(membership)!=len(METHODS) else "agreement")
        feature_values = {k:v for k,v in facts[pid].items() if k in {"trip_rate_30_days", "quick_return_share", "repeated_route_share"}}
        rows.append(dict(profile_id=pid, case_id=annotation["case_id"], benchmark_label=annotation["benchmark_label"], kind=kind,
            top_methods=membership, rules_rank=by_method["rules"][pid]["rank"], if_rank=by_method["isolation_forest"][pid]["rank"],
            combined_rank=by_method["combination"][pid]["rank"], if_score=by_method["isolation_forest"][pid]["score"],
            combined_score=by_method["combination"][pid]["score"], features_json=json.dumps(feature_values, ensure_ascii=False, sort_keys=True),
            criteria_json=json.dumps(details[pid], ensure_ascii=False, sort_keys=True, default=str),
            evidence_segment_ids=sorted({sid for row in details[pid] for sid in row["evidence_segment_ids"]}),
            explanation_ru=primary[pid]["explanation_ru"], run_id=run_id))
    return rows


def _invariants(results, plan):
    base = results["baseline"]
    checks = []
    def test(name, success):
        if not success:raise ValueError(f"Нарушен инвариант T20: {name}")
        checks.append(dict(check=name, passed=True))
    def forest_values(data):
        return sorted((r["profile_id"], r["status"], r["score"]) for r in data["methods"]["isolation_forest"])
    for spec in plan:
        name = spec["name"]
        if name == "baseline" or spec["dataset"]!="holdout" or spec["seed"]!=plan[0]["seed"]:continue
        data = results[name]
        if spec["rules"] == plan[0]["rules"] and spec["forest"]["as_of"] == plan[0]["forest"]["as_of"]:
            test("primary_unchanged_" + name, all(fingerprint(base["path"] / file)==fingerprint(data["path"] / file) for file in ["rankings.parquet", "top50.parquet"]))
        if name.startswith(("threshold_", "weight_")):
            test("IF_unchanged_" + name, forest_values(base)==forest_values(data))
    if "graph_mc_seed" in results:
        first, other = results["graph"]["path"], results["graph_mc_seed"]["path"]
        if (first / "model.joblib").exists() and (other / "model.joblib").exists():
            test("diagnostic_MC_seed_does_not_change_model", fingerprint(first / "model.joblib")==fingerprint(other / "model.joblib"))
    if "graph" in results:
        graph = json.loads((results["graph"]["path"] / "parents/evaluation/graph/model_metadata.json").read_text(encoding="utf-8"))
        expected = sorted(a["profile_id"] for a in base["annotations"] if a["case_id"]=="coflight_stress")
        test("holdout_planted_pair_matches_explicit_events", graph["graph_generator"]["planted_pair"]==expected)
    return checks


def _scenario_invariants(people, segments, rules, forest):
    original = _score_profiles(people, segments, rules)
    if _score_profiles(people, segments + segments[:20], rules) != original:
        raise ValueError("Повторное свидетельство изменило правила T20")
    if _score_profiles(list(reversed(people)), list(reversed(segments)), rules) != original:
        raise ValueError("Порядок строк изменил правила T20")
    from datetime import date
    cutoff = date(2026, 3, 1)
    bounded = [dict(p, window_end=cutoff) for p in people]
    before = _score_profiles(bounded, segments, rules)
    prefix = _score_profiles(bounded, [s for s in segments if s['departure_date'] is None or s['departure_date']<=cutoff], rules)
    cfg = copy.deepcopy(forest); cfg['as_of'] = cutoff.isoformat()
    matrices = [make_matrix('evaluation', 'same_verified_holdout', data[2], data[3], data[0], cfg) for data in [before, prefix]]
    if matrices[0] != matrices[1]:
        raise ValueError("Будущие поездки изменили признаки префикса T20")
    return [dict(check=name, passed=True) for name in ['duplicate_evidence_invariance', 'input_order_invariance', 'holdout_as_of_prefix_invariance']]


def run(config_path, forest_path, rules_path, graph_path, output=None, artifacts_dir=Path("artifacts/t20"), work_dir=Path("data/work/t20"), progress=None):
    started = time.monotonic()
    cfg, raw = load_config(config_path)
    forest, forest_raw = load_forest(forest_path)
    rules, rules_raw = load_model(rules_path)
    graph, graph_raw = load_graph_config(graph_path)
    plan = build_plan(cfg, forest, rules, graph)
    request = dict(version=VERSION, data_mode="synthetic_demo", config=cfg, plan=plan,
        recipes={p["name"]:recipe(p["seed"], cfg["profiles"], p["generator"]) for p in plan}, runtime=_runtime(),
        config_sha256={"evaluation":fingerprint(config_path), "forest":fingerprint(forest_path), "rules":fingerprint(rules_path), "graph":fingerprint(graph_path)})
    run_id = "evaluation-" + digest(request)[:20]
    target = Path(output).resolve() if output else Path(artifacts_dir).resolve() / run_id
    work_dir = Path(work_dir).resolve()
    if target == work_dir or target in work_dir.parents or work_dir in target.parents:
        raise ValueError("Выход T20 не должен пересекаться с рабочим каталогом")
    for source in [config_path, forest_path, rules_path, graph_path]:
        if target == Path(source).resolve() or target in Path(source).resolve().parents:
            raise ValueError("Выход T20 пересекается с конфигурацией")
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ".t20-evaluation.lock", "T20"):
        if target.exists():
            if progress:progress("T20: проверка готового снимка без загрузки моделей")
            report = validate_snapshot(target, request)
            return report, (target / "summary.txt").read_text(encoding="utf-8")
        temporary = Path(tempfile.mkdtemp(prefix=".t20-evaluation-", suffix=".partial", dir=target.parent))
        try:
            (temporary / "config").mkdir()
            for name, content in [("evaluation.yaml", raw), ("isolation_forest.yaml", forest_raw), ("model.yaml", rules_raw), ("graph.yaml", graph_raw)]:
                (temporary / "config" / name).write_bytes(content)
            results, comparison, all_stability, agreement, paired, parents = {}, [], [], [], [], []
            for index, spec in enumerate(plan):
                name = spec["name"]
                if progress:progress(f"T20: опыт {index+1}/{len(plan)} — {name}")
                folder = work_dir / "configs" / name
                folder.mkdir(parents=True, exist_ok=True)
                paths = []
                for filename, content in [("isolation_forest.yaml", spec["forest"]), ("model.yaml", spec["rules"]), ("graph.yaml", spec["graph"])]:
                    file = folder / filename
                    file.write_text(yaml.safe_dump(content, allow_unicode=True, sort_keys=False), encoding="utf-8")
                    paths.append(file)
                source = create_demo(work_dir / "sources", spec["seed"], cfg["profiles"], spec["generator"])
                report, _ = train_run(source, *paths, artifacts_dir=work_dir / "experiments", work_dir=work_dir / "t19", progress=progress)
                parent = work_dir / "experiments" / report["run_id"]
                data = collect(parent, name, run_id)
                results[name] = data
                comparison.extend(dict(row, experiment=name) for row in metrics(data["methods"], data["annotations"], cfg["top_k"], spec["quality_enabled"]))
                agreement.extend(dict(row, experiment=name) for row in method_agreement(data["methods"], cfg["top_k"]))
                if name != "baseline":
                    base = results["baseline"]
                    stable = stability(base["methods"], data["methods"], cfg["top_k"])
                    all_stability.extend(dict(row, experiment=name) for row in stable)
                    if stable[0]["comparable"]:
                        common = common_universe(base["methods"]) & common_universe(data["methods"])
                        for side, item in [("baseline", base), ("variant", data)]:
                            paired.extend(dict(row, experiment=name, side=side) for row in metrics(item["methods"], item["annotations"], cfg["top_k"], spec["quality_enabled"], common))
                dest = temporary / "parents" / name
                dest.parent.mkdir(exist_ok=True)
                shutil.copytree(parent, dest)
                frozen = temporary / "config/experiments" / name
                frozen.mkdir(parents=True)
                for file in paths:shutil.copyfile(file, frozen / file.name)
                parents.append(dict(experiment=name, parent_run_id=report["run_id"], manifest_sha256=fingerprint(parent / "run_manifest.json"),
                    model_sha256=fingerprint(parent / "model.joblib") if (parent / "model.joblib").exists() else None,
                    training_performed=report["training_performed"], fit=report["fit"], if_scored=report["if_scored"], elapsed_seconds=report["elapsed_seconds"]))
            invariants = _invariants(results, plan)
            base = results["baseline"]
            _, people, segments = validate_demo(base["path"] / "parents/evaluation/rules/input")
            invariants.extend(_scenario_invariants(people, segments, rules, forest))
            graph_events = read(results["graph"]["path"] / "parents/evaluation/graph/graph_events.parquet") if "graph" in results else []
            audit = write_bundle(temporary, people, segments, base["methods"], base["annotations"], graph_events, cfg["audit"], base["source"]["logical_sha256"])
            cases = _cases(base, max(cfg["top_k"]))
            tables = [("scores.parquet", [r for data in results.values() for rows in data["methods"].values() for r in rows], SCORE_SCHEMA),
                      ("scenarios.parquet", [dict(r, experiment=name, run_id=run_id) for name,data in results.items() for r in data["annotations"]], SCENARIO_SCHEMA),
                      ("disagreements.parquet", _disagreements(base, run_id, max(cfg["top_k"])), DISAGREEMENT_SCHEMA)]
            for name, rows, schema in tables:
                pq.write_table(pa.Table.from_pylist(rows, schema=schema.with_metadata({b"schema_version":b"1.0", b"data_mode":b"synthetic_demo", b"run_id":run_id.encode("ascii")})), temporary / name)
            for name, rows, fallback in [("comparison.csv", comparison, []), ("stability.csv", all_stability, ["experiment", "method", "k"]),
                    ("method_agreement.csv", agreement, []), ("paired_comparison.csv", paired, ["experiment", "side", "method", "k"]), ("case_summary.csv", cases, [])]:
                _write_csv(temporary / name, rows, fallback)
            report = dict(schema_version="1.0", run_id=run_id, status="complete_demo_pending_review", data_mode="synthetic_demo",
                profiles=cfg["profiles"], experiments=len(plan), top_k=cfg["top_k"], parents=parents, audit=audit, invariants=invariants,
                disagreement_counts=dict(Counter(r["kind"] for r in tables[2][1])),
                primary_decision="retain_criteria_count_rules; experiments_not_promoted", thresholds_tuned=False,
                m07_status="disabled", ui_ready=False, acceptance="pending_independent_review_and_T21",
                elapsed_seconds=round(time.monotonic()-started, 3))
            write_json(temporary / "report.json", report)
            write_json(temporary / "evaluation.json", dict(report=report, request=request, comparison=comparison, stability=all_stability,
                paired_comparison=paired, method_agreement=agreement, case_summary=cases,
                label_scope="designed_scenarios_only; not_person_traits", quality_claim="no_real_world_efficacy_claim"))
            (temporary / "evaluation.md").write_text(markdown(report, comparison, all_stability, paired, cases), encoding="utf-8")
            summary = (f"Итог T20 (только вымышленные истории)\nОпытов: {len(plan)}; профилей в каждом: {cfg['profiles']}\n"
                f"Сравнение правил / IF / комбинации и чувствительность: выполнено\nПроверенных инвариантов: {len(invariants)}\n"
                f"Независимый аудит: 0 / {audit['selected']}; ожидается\nОсновной режим: число критериев; M07 выключен\n"
                f"T21 и интерфейс: ожидаются\nВремя: {report['elapsed_seconds']} с\nРезультаты: {target}\n")
            (temporary / "summary.txt").write_text(summary, encoding="utf-8")
            files = {p.relative_to(temporary).as_posix():fingerprint(p) for p in sorted(temporary.rglob("*")) if p.is_file()}
            write_json(temporary / "run_manifest.json", dict(schema_version="1.0", run_id=run_id, request=request, files=files))
            validate_snapshot(temporary, request)
            temporary.rename(target)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
    return report, summary
