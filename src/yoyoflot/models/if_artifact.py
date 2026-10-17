"""Self-contained fictional training artifacts with an unchanged P0 ranking."""
import importlib.metadata
import json
import platform
import shutil
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from .. import __version__
from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .demo import VARIED_GENERATOR, create_demo, digest, validate_demo, write_json
from .if_inputs import prepare_dataset
from .isolation_forest import VERSION, columns, experimental_rows, fit, load_config, score

MATRIX_SCHEMA = pa.schema([
    ("role", pa.string()), ("dataset_sha256", pa.string()), ("profile_id", pa.string()),
    ("feature_names", pa.list_(pa.string())), ("values", pa.list_(pa.float32())),
    ("feature_available", pa.list_(pa.bool_())), ("eligible", pa.bool_()), ("exclusion_reason", pa.string()),
    ("window_start", pa.date32()), ("window_end", pa.date32()), ("run_id", pa.string()),
])
EXPERIMENT_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("display_name", pa.string()), ("rank", pa.int32()), ("ranking_mode", pa.string()),
    ("if_score", pa.float64()), ("rule_percentile", pa.float64()), ("if_percentile", pa.float64()),
    ("combined_score", pa.float64()), ("status", pa.string()), ("matched_criteria_count", pa.int32()),
    ("rule_score", pa.float64()), ("primary_rule_status", pa.string()), ("explanation_ru", pa.string()),
    ("window_start", pa.date32()), ("window_end", pa.date32()), ("data_mode", pa.string()),
    ("model_version", pa.string()), ("run_id", pa.string()),
])
REFERENCE_SCHEMA = pa.schema([
    ("role", pa.string()), ("dataset_sha256", pa.string()),
    *[(field.name, field.type) for field in EXPERIMENT_SCHEMA if field.name not in {"rank", "ranking_mode"}],
])
TABLE_SCHEMAS = {"matrices.parquet": MATRIX_SCHEMA, "reference_scores.parquet": REFERENCE_SCHEMA,
                 **{name: EXPERIMENT_SCHEMA for name in ["if_rankings.parquet", "if_top50.parquet", "combined_rankings.parquet", "combined_top50.parquet"]}}


def _runtime():
    package = Path(__file__).parents[1]
    paths = [*Path(__file__).parent.glob("*.py"), * (package / "features").glob("graph*.py"),
             package / "__init__.py", package / "normalize/locking.py", package / "provenance.py"]
    versions = {}
    for name in ["scikit-learn", "numpy", "scipy", "joblib", "pyarrow", "pyyaml"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return dict(package_version=__version__, python=platform.python_version(), libraries=versions,
                code_sha256=digest({str(p.relative_to(package)): fingerprint(p) for p in sorted(paths)}))


def _rank(rows, mode):
    key = "if_score" if mode == "isolation_forest" else "combined_score"
    ordered = sorted((r for r in rows if r["status"] == "scored"), key=lambda r: (-r[key], r["profile_id"]))
    ranks = {r["profile_id"]: i + 1 for i, r in enumerate(ordered)}
    result = [dict(r, rank=ranks.get(r["profile_id"]), ranking_mode=mode) for r in rows]
    result.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["profile_id"]))
    return result, [r for r in result if r["rank"] is not None and r["rank"] <= 50]


def _check_snapshot(path, request):
    file = path / "run_manifest.json"
    if not file.is_file() or file.stat().st_size > 262144:
        raise ValueError("Выход не является завершённым снимком T19")
    manifest = json.loads(file.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "1.0" or manifest.get("request") != request or
            manifest.get("run_id") != "if-" + digest(request)[:20]):
        raise ValueError("Снимок T19 относится к другой версии; выберите новый выход")
    files = manifest.get("files")
    if not isinstance(files, dict) or not set(TABLE_SCHEMAS).issubset(files):
        raise ValueError("Неполный состав снимка T19")
    for name, checksum in files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Неверный путь файла снимка T19")
        file = path / relative
        if not file.is_file() or file.is_symlink() or fingerprint(file) != checksum:
            raise ValueError(f"Изменён файл снимка T19: {name}")
    report = json.loads((path / "report.json").read_text(encoding="utf-8"))
    mandatory = set(TABLE_SCHEMAS) | {"rankings.parquet", "top50.parquet", "config/isolation_forest.yaml",
                                    "model_metadata.json", "report.json", "summary.txt"}
    if report["training_performed"]:
        mandatory.add("model.joblib")
    if not mandatory.issubset(files) or ("model.joblib" in files) != report["training_performed"]:
        raise ValueError("Несогласованный состав обученной модели T19")
    expected_files = set(mandatory)
    for role in ["training", "calibration", "evaluation"]:
        parent = path / "parents" / role / "rules"
        source, _, _ = validate_demo(parent / "input")
        if source["logical_sha256"] != request["datasets"][role]["input_logical_sha256"]:
            raise ValueError("Смешанные учебные наборы снимка T19")
        if fingerprint(parent / "run_manifest.json") != request["datasets"][role]["rules_manifest_sha256"]:
            raise ValueError("Смешанная версия T17 в снимке T19")
        for stage in ["rules"] + (["graph"] if request["config"]["include_graph"] else []):
            prefix = f"parents/{role}/{stage}/"
            parent_manifest = path / prefix / "run_manifest.json"
            identity = request["datasets"][role][stage + "_manifest_sha256"]
            if not parent_manifest.is_file() or fingerprint(parent_manifest) != identity:
                raise ValueError("Смешанная версия родительского снимка")
            expected_files.add(prefix + "run_manifest.json")
            nested = json.loads(parent_manifest.read_text(encoding="utf-8"))["files"]
            for name, checksum in nested.items():
                if files.get(prefix + name) != checksum:
                    raise ValueError("Неполный/изменённый родительский снимок T19")
                expected_files.add(prefix + name)
    if set(files) != expected_files:
        raise ValueError("Неверный полный состав снимка T19")
    actual_files = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
    if actual_files != set(files) | {"run_manifest.json"}:
        raise ValueError("Снимок T19 содержит незаявленные файлы")
    # Never unpickle a pre-existing user-selected output path, including cache hits.
    return report


def _verify_fresh_reload(path, bundle, calibration, runtime):
    import joblib
    expected_sha = fingerprint(path)
    loaded = joblib.load(path)  # Only our just-written file in our private temporary directory.
    if fingerprint(path) != expected_sha or loaded["training_runtime"] != runtime:
        raise ValueError("Изменена собственная только что сохранённая модель")
    cases = [r for r in calibration if r["eligible"]]
    if score(loaded, cases) != score(bundle, cases):
        raise ValueError("Сохранённая модель не воспроизводит калибровочные оценки")


def run(input_dir, config_path, rules_config, graph_config, output=None, artifacts_dir=Path("artifacts/t19"),
        work_dir=Path("data/work/t19"), reference=None, calibration=None, progress=None):
    started = time.monotonic()
    config, config_bytes = load_config(config_path)
    evaluation_manifest, evaluation_people, _ = validate_demo(input_dir)
    work_dir = Path(work_dir).resolve()
    ref = config["reference"]
    sources = dict(evaluation=Path(input_dir).resolve(),
                   training=Path(reference).resolve() if reference is not None else create_demo(
                       work_dir / "sources", ref["training_seed"], ref["training_profiles"], VARIED_GENERATOR),
                   calibration=Path(calibration).resolve() if calibration is not None else create_demo(
                       work_dir / "sources", ref["calibration_seed"], ref["calibration_profiles"], VARIED_GENERATOR))
    seen_seeds, seen_ids = set(), set()
    for role, path in sources.items():
        manifest, people, _ = validate_demo(path)
        seed, ids = manifest["recipe"]["seed"], {p["profile_id"] for p in people}
        if seed in seen_seeds or ids.intersection(seen_ids):
            raise ValueError("Обучение, калибровка и оценка должны иметь разные seed и непересекающиеся profile_id; используйте генератор v2")
        seen_seeds.add(seed)
        seen_ids.update(ids)
    prospective = Path(output).resolve() if output is not None else Path(artifacts_dir).resolve()
    for directory in [work_dir, *sources.values()]:
        if prospective == directory or prospective in directory.parents or directory in prospective.parents:
            raise ValueError("Выход T19 не должен пересекаться с входами и рабочим каталогом")
    datasets = {role: prepare_dataset(sources[role], role, rules_config, graph_config, config, work_dir, progress)
                for role in ["training", "calibration", "evaluation"]}
    identities = {}
    for role, data in datasets.items():
        identities[role] = dict(data["parents"], rules_manifest_sha256=fingerprint(data["rule_path"] / "run_manifest.json"),
                                graph_manifest_sha256=fingerprint(data["graph_path"] / "run_manifest.json") if data["graph_path"] else None)
    runtime = _runtime()
    request = dict(schema_version="1.0", data_mode="synthetic_demo", config=config, datasets=identities, runtime=runtime)
    run_id = "if-" + digest(request)[:20]
    target = Path(output).resolve() if output is not None else Path(artifacts_dir).resolve() / run_id
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ".t19-training.lock", "T19"):
        if target.exists():
            return _check_snapshot(target, request), (target / "summary.txt").read_text(encoding="utf-8")
        temporary = Path(tempfile.mkdtemp(prefix=".t19-training-", suffix=".partial", dir=target.parent))
        try:
            training, calib, evaluation = [datasets[r] for r in ["training", "calibration", "evaluation"]]
            bundle, fitted = fit(training["matrix"], calib["matrix"], calib["rules"], config, progress)
            reloaded = False
            if bundle is not None:
                import joblib
                bundle["training_runtime"] = runtime
                joblib.dump(bundle, temporary / "model.joblib", compress=3)
                _verify_fresh_reload(temporary / "model.joblib", bundle, calib["matrix"], runtime)
                reloaded = True
            experiments = experimental_rows(evaluation["matrix"], evaluation["rules"], bundle, fitted, config, run_id)
            if_rows, if_top = _rank(experiments, "isolation_forest")
            combo_rows, combo_top = _rank(experiments, "experimental_combination")
            matrix_rows = [dict(row, run_id=run_id) for data in datasets.values() for row in data["matrix"]]
            reference_rows = []
            for role in ["training", "calibration"]:
                data = datasets[role]
                reference_rows.extend(dict(r, role=role, dataset_sha256=data["manifest"]["logical_sha256"])
                                      for r in experimental_rows(data["matrix"], data["rules"], bundle, fitted, config, run_id))
            tables = {"matrices.parquet": matrix_rows, "reference_scores.parquet": reference_rows,
                      "if_rankings.parquet": if_rows, "if_top50.parquet": if_top,
                      "combined_rankings.parquet": combo_rows, "combined_top50.parquet": combo_top}
            for name, rows in tables.items():
                schema = TABLE_SCHEMAS[name].with_metadata({b"schema_version": b"1.0", b"data_mode": b"synthetic_demo", b"run_id": run_id.encode("ascii")})
                pq.write_table(pa.Table.from_pylist(rows, schema=schema), temporary / name)
            for role, data in datasets.items():
                parent = temporary / "parents" / role
                parent.mkdir(parents=True)
                shutil.copytree(data["rule_path"], parent / "rules")
                if data["graph_path"]:
                    shutil.copytree(data["graph_path"], parent / "graph")
            for name in ["rankings.parquet", "top50.parquet"]:
                shutil.copyfile(evaluation["rule_path"] / name, temporary / name)
            (temporary / "config").mkdir()
            (temporary / "config/isolation_forest.yaml").write_bytes(config_bytes)
            primary_top = pq.ParquetFile(temporary / "top50.parquet").read().to_pylist()
            def overlap(a, b):
                left, right = {r["profile_id"] for r in a}, {r["profile_id"] for r in b}
                return dict(intersection=len(left & right), union=len(left | right), jaccard=len(left & right) / len(left | right) if left or right else None)
            report = dict(schema_version="1.0", run_id=run_id, status="complete_demo" if bundle else "complete_demo_experiment_unavailable",
                          data_mode="synthetic_demo", training_performed=bundle is not None, reload_verified=reloaded,
                          fit=fitted, profiles=len(evaluation_people), if_scored=sum(r["status"] == "scored" for r in experiments),
                          table_rows={name: len(rows) for name, rows in tables.items()},
                          top50_overlap=dict(rules_if=overlap(primary_top, if_top), rules_combined=overlap(primary_top, combo_top)),
                          primary_ranking="criteria_count_unchanged", graph_status="experimental_features" if config["include_graph"] else "disabled_by_config",
                          m07_status="disabled", ui_ready=False, acceptance="pending_T20_review",
                          elapsed_seconds=round(time.monotonic() - started, 3))
            write_json(temporary / "model_metadata.json", dict(**request, run_id=run_id, model_version=VERSION,
                feature_names=columns(config), fit=fitted, score_formula="-IsolationForest.score_samples(X)",
                score_direction="higher_is_more_unusual", percentile_reference="calibration_only_midrank_CDF",
                rule_percentile_key=["matched_criteria_count", "rule_score"], primary_parent=identities["evaluation"]["rules_run_id"],
                training_performed=bundle is not None, m07_status="disabled", ui_ready=False, acceptance="pending_T20_review",
                explanation_kind="method_description_not_internal_feature_attribution"))
            summary = (f"Итог T19 (независимые вымышленные профили)\nОбучение IF: {'выполнено' if bundle else 'не выполнялось'}\n"
                       f"Обучающих строк: {fitted['training_rows']}; калибровочных: {fitted['calibration_rows']}\n"
                       f"Профилей оценки: {len(evaluation_people)}; IF доступен: {report['if_scored']}\n"
                       f"Причина недоступности: {fitted['reason'] or 'нет'}\n"
                       f"Основной рейтинг: число критериев; IF и комбинация — отдельные эксперименты\n"
                       f"Граф: {report['graph_status']}; M07 выключен до T20\nВремя: {report['elapsed_seconds']} с\nРезультаты: {target}\n")
            write_json(temporary / "report.json", report)
            (temporary / "summary.txt").write_text(summary, encoding="utf-8")
            checksums = {str(p.relative_to(temporary)).replace("\\", "/"): fingerprint(p) for p in sorted(temporary.rglob("*")) if p.is_file()}
            write_json(temporary / "run_manifest.json", dict(schema_version="1.0", run_id=run_id, request=request, files=checksums))
            _check_snapshot(temporary, request)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return report, summary
