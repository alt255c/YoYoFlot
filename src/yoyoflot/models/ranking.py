"""Stable criteria-count ranking and immutable, self-contained demo artifacts."""
import json
import platform
import shutil
import tempfile
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .. import __version__
from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .demo import (PROFILE_SCHEMA, SEGMENT_SCHEMA, TABLES, digest, generate_rows,
                   validate_demo, write_json)
from .rule_score import _score_profiles, load_model

SCORE_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("display_name", pa.string()), ("rank", pa.int32()),
    ("matched_criteria_count", pa.int32()), ("available_criteria_count", pa.int32()),
    ("enabled_criteria_count", pa.int32()), ("rule_score", pa.float64()),
    ("if_score", pa.float64()), ("combined_score", pa.float64()),
    ("eligibility_status", pa.string()), ("explanation_ru", pa.string()),
    ("window_start", pa.date32()), ("window_end", pa.date32()), ("source_coverage", pa.float64()),
    ("model_version", pa.string()), ("data_mode", pa.string()), ("ranking_mode", pa.string()),
    ("run_id", pa.string()),
])
EXPLANATION_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("criterion_id", pa.string()), ("criterion_group", pa.string()),
    ("criterion_version", pa.string()), ("title_ru", pa.string()), ("feature_name", pa.string()),
    ("status", pa.string()), ("value", pa.float64()), ("unit", pa.string()), ("operator", pa.string()),
    ("threshold", pa.float64()), ("min_observations", pa.int32()), ("used_observations", pa.int32()),
    ("availability_reason", pa.string()), ("contribution", pa.float64()), ("group_contribution", pa.float64()),
    ("explanation_ru", pa.string()), ("evidence_segment_ids", pa.list_(pa.string())),
    ("config_hash", pa.string()), ("window_start", pa.date32()), ("window_end", pa.date32()),
    ("run_id", pa.string()),
])
FEATURE_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("window_start", pa.date32()), ("window_end", pa.date32()),
    ("covered_days", pa.int32()), ("source_coverage", pa.float64()), ("total_segments", pa.int32()),
    ("dated_segments", pa.int32()), ("known_route_segments", pa.int32()), ("comparable_pairs", pa.int32()),
    ("quick_return_pairs", pa.int32()), ("unique_known_routes", pa.int32()),
    ("trip_rate_30_days", pa.float64()), ("quick_return_share", pa.float64()),
    ("repeated_route_share", pa.float64()), ("feature_version", pa.string()), ("run_id", pa.string()),
])
COVERAGE_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("feature_name", pa.string()), ("available", pa.bool_()),
    ("used_observations", pa.int32()), ("reason", pa.string()), ("feature_version", pa.string()),
    ("run_id", pa.string()),
])
OUTPUT_TABLES = {
    "rule_scores.parquet": SCORE_SCHEMA, "rankings.parquet": SCORE_SCHEMA, "top50.parquet": SCORE_SCHEMA,
    "model_explanations.parquet": EXPLANATION_SCHEMA,
    "features.parquet": FEATURE_SCHEMA, "feature_coverage.parquet": COVERAGE_SCHEMA,
}


def rank_scores(scores, run_id):
    eligible = sorted((r for r in scores if r["eligibility_status"] == "eligible"),
                      key=lambda r: (-r["matched_criteria_count"], -r["rule_score"], r["profile_id"]))
    ranks = {r["profile_id"]: i + 1 for i, r in enumerate(eligible)}
    rows = [dict(r, rank=ranks.get(r["profile_id"]), if_score=None, combined_score=None,
                 data_mode="synthetic_demo", ranking_mode="criteria_count", run_id=run_id) for r in scores]
    rows.sort(key=lambda r: (r["rank"] is None, r["rank"] or 0, r["profile_id"]))
    return rows, [r for r in rows if r["rank"] is not None and r["rank"] <= 50]


def _runtime():
    package = Path(__file__).parent
    return dict(package_version=__version__, python=platform.python_version(), pyarrow=pa.__version__,
                pyyaml=yaml.__version__, code_sha256=digest({
                    path.name: fingerprint(path) for path in sorted(package.glob("*.py"))}))


def _check_snapshot(path, request):
    manifest_path = path / "run_manifest.json"
    if not manifest_path.is_file() or manifest_path.stat().st_size > 65536:
        raise ValueError("Существующий выход не является завершённым снимком T17; выберите новый путь")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "1.0" or manifest.get("request") != request or
            manifest.get("run_id") != "rules-" + digest(request)[:20]):
        raise ValueError("Снимок относится к другой конфигурации/версии; выберите новый выход")
    expected = set(OUTPUT_TABLES) | {"input/" + n for n in TABLES} | {
        "input/demo_manifest.json", "config/model.yaml", "model_metadata.json", "report.json", "summary.txt"}
    files = manifest.get("files", {})
    if not isinstance(files, dict) or set(files) != expected:
        raise ValueError("Неполный состав снимка T17")
    for name, checksum in files.items():
        file = path / name
        if file.is_symlink() or not file.is_file() or fingerprint(file) != checksum:
            raise ValueError(f"Изменён файл снимка T17: {name}")
    validate_demo(path / "input")
    return json.loads((path / "report.json").read_text(encoding="utf-8"))


def run(input_dir, config_path, output=None, artifacts_dir=Path("artifacts/t17"), progress=None):
    started = time.monotonic()
    config, config_bytes = load_model(config_path)
    if progress:
        progress("T17: проверка происхождения и воспроизведение независимого учебного набора")
    manifest, _, _ = validate_demo(input_dir)
    request = dict(schema_version="1.0", data_mode="synthetic_demo", input_logical_sha256=manifest["logical_sha256"],
                   recipe=manifest["recipe"], config_hash=digest(config), runtime=_runtime())
    run_id = "rules-" + digest(request)[:20]
    target = Path(output).resolve() if output is not None else Path(artifacts_dir).resolve() / run_id
    source = Path(input_dir).resolve()
    if target == source or target in source.parents or source in target.parents:
        raise ValueError("Выход T17 не должен пересекаться с входным набором")
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ".t17-ranking.lock", "T17"):
        if target.exists():
            report = _check_snapshot(target, request)
            return report, (target / "summary.txt").read_text(encoding="utf-8")
        temporary = Path(tempfile.mkdtemp(prefix=".t17-ranking-", suffix=".partial", dir=target.parent))
        try:
            # Freeze canonical regenerated rows, independent of physical input order.
            people, segments = generate_rows(manifest["recipe"])
            if progress:
                progress(f"T17: расчёт трёх групп критериев, профилей — {len(people)}")
            scores, explanations, features, coverage = _score_profiles(people, segments, config)
            ranked, top = rank_scores(scores, run_id)
            for rows in [explanations, features, coverage]:
                for row in rows:
                    row["run_id"] = run_id
            for row in features:
                row["feature_version"] = config["version"]
            outputs = {"rule_scores.parquet": sorted(ranked, key=lambda r: r["profile_id"]),
                       "rankings.parquet": ranked, "top50.parquet": top,
                       "model_explanations.parquet": explanations, "features.parquet": features,
                       "feature_coverage.parquet": coverage}
            schema_metadata = {b"schema_version": b"1.0", b"data_mode": b"synthetic_demo",
                               b"run_id": run_id.encode("ascii")}
            for name, rows in outputs.items():
                table = pa.Table.from_pylist(rows, schema=OUTPUT_TABLES[name].with_metadata(schema_metadata))
                pq.write_table(table, temporary / name)
            frozen_input = temporary / "input"
            frozen_input.mkdir()
            for name, rows, schema in [("profiles.parquet", people, PROFILE_SCHEMA),
                                       ("segments.parquet", segments, SEGMENT_SCHEMA)]:
                pq.write_table(pa.Table.from_pylist(rows, schema=schema), frozen_input / name)
            write_json(frozen_input / "demo_manifest.json", dict(manifest, files={
                name: fingerprint(frozen_input / name) for name in TABLES}))
            (temporary / "config").mkdir()
            (temporary / "config/model.yaml").write_bytes(config_bytes)
            write_json(temporary / "model_metadata.json", dict(
                **request, run_id=run_id, model_version=config["version"], ranking_mode="criteria_count",
                sort_order=["matched_criteria_count DESC", "rule_score DESC", "profile_id ASC"],
                threshold_status="illustrative_unvalidated", if_built=False, graph_built=False,
                ui_ready=False, acceptance="pending_T20_review", group_contribution="max_matched_weight"))
            statuses = {status: sum(r["eligibility_status"] == status for r in ranked)
                        for status in ["eligible", "no_matches", "insufficient_data"]}
            report = dict(schema_version="1.0", run_id=run_id, data_mode="synthetic_demo", status="complete_demo",
                          profiles=len(people), segments=len(segments), top50=len(top), eligibility=statuses,
                          matched_groups={str(n): sum(r["matched_criteria_count"] == n for r in ranked) for n in range(4)},
                          criteria_rows=len(explanations), elapsed_seconds=round(time.monotonic() - started, 3),
                          threshold_status="illustrative_unvalidated", acceptance="pending_T20_review", ui_ready=False,
                          limitation_ru="Независимый вымышленный учебный набор. Баллы не являются вероятностью или выводом о человеке.")
            summary = (f"Итог T17 (вымышленные учебные профили)\nПрофилей: {len(people)}; сегментов: {len(segments)}\n"
                       f"Допустимых с срабатываниями: {statuses['eligible']}; в топе: {len(top)} / 50\n"
                       f"Без срабатываний: {statuses['no_matches']}; недостаточно данных: {statuses['insufficient_data']}\n"
                       f"Пороги: учебные, не прошли проверку T20\nВремя: {report['elapsed_seconds']} с\n"
                       f"Результаты: {target}\n")
            write_json(temporary / "report.json", report)
            (temporary / "summary.txt").write_text(summary, encoding="utf-8")
            files = {str(path.relative_to(temporary)).replace("\\", "/"): fingerprint(path)
                     for path in sorted(temporary.rglob("*")) if path.is_file()}
            write_json(temporary / "run_manifest.json", dict(schema_version="1.0", run_id=run_id,
                                                            request=request, files=files))
            _check_snapshot(temporary, request)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return report, summary
