"""Build and verify window-compatible T17/T18 parents before learning."""
import json
from pathlib import Path

import pyarrow.parquet as pq

from ..features.graph import OUTPUT_SCHEMAS, load_graph_config
from ..features.graph_artifact import run as graph_run
from .demo import validate_demo
from .isolation_forest import make_matrix
from .ranking import OUTPUT_TABLES, run as rules_run


def _read_table(path, name, schema, run_id, profiles):
    file = pq.ParquetFile(path / name)
    limit = profiles * 3 if name == "feature_coverage.parquet" else profiles
    if not file.schema_arrow.equals(schema, check_metadata=False) or file.metadata.num_rows != limit:
        raise ValueError(f"Неверная схема/число строк родительского {name}")
    if file.schema_arrow.metadata.get(b"run_id") != run_id.encode("ascii") or file.schema_arrow.metadata.get(b"data_mode") != b"synthetic_demo":
        raise ValueError(f"Неверная версия таблицы {name}")
    rows = file.read().to_pylist()
    if any(r["run_id"] != run_id for r in rows):
        raise ValueError(f"Смешанные run_id таблицы {name}")
    return rows


def check_alignment(input_hash, rule_meta, graph_meta, config):
    if (rule_meta.get("input_logical_sha256") != input_hash or rule_meta.get("data_mode") != "synthetic_demo" or
            rule_meta.get("model_version") != "rules-demo-1.0" or rule_meta.get("as_of") != config["as_of"]):
        raise ValueError("T17 не соответствует входу/окну/версии T19")
    if config["include_graph"] and (not graph_meta or graph_meta.get("input_logical_sha256") != input_hash or
            graph_meta.get("data_mode") != "synthetic_demo" or graph_meta.get("graph_version") != "graph-demo-1.0" or
            graph_meta.get("config", {}).get("as_of") != config["as_of"] or graph_meta.get("m07_status") != "disabled"):
        raise ValueError("T18 не соответствует T17/T19 или неожиданно включает M07")


def prepare_dataset(source, role, rules_config, graph_config, config, work_dir, progress=None):
    manifest, profiles, _ = validate_demo(source)
    if config["include_graph"]:
        graph_parameters, _ = load_graph_config(graph_config)
        if graph_parameters["as_of"] != config["as_of"]:
            raise ValueError("as_of конфигураций T18 и T19 различается")
    if progress:
        progress(f"T19: подготовка {role}; профилей — {len(profiles)}")
    parent = Path(work_dir) / "parents" / role
    report, _ = rules_run(source, rules_config, artifacts_dir=parent / "rules", as_of=config["as_of"])
    rule_path = (parent / "rules" / report["run_id"]).resolve()
    rule_meta = json.loads((rule_path / "model_metadata.json").read_text(encoding="utf-8"))
    graph_meta, graph_path, graph_features = None, None, None
    if config["include_graph"]:
        graph_report, _ = graph_run(source, graph_config, artifacts_dir=parent / "graph", progress=progress)
        graph_path = (parent / "graph" / graph_report["run_id"]).resolve()
        graph_meta = json.loads((graph_path / "model_metadata.json").read_text(encoding="utf-8"))
        graph_features = _read_table(graph_path, "graph_features.parquet", OUTPUT_SCHEMAS["graph_features.parquet"], graph_report["run_id"], len(profiles))
    check_alignment(manifest["logical_sha256"], rule_meta, graph_meta, config)
    features, coverage, scores = [_read_table(rule_path, name, OUTPUT_TABLES[name], report["run_id"], len(profiles))
                                  for name in ["features.parquet", "feature_coverage.parquet", "rule_scores.parquet"]]
    matrix = make_matrix(role, manifest["logical_sha256"], features, coverage, scores, config, graph_features)
    return dict(role=role, manifest=manifest, matrix=matrix, rules=scores, rule_path=rule_path, graph_path=graph_path,
                parents=dict(rules_run_id=report["run_id"], graph_run_id=graph_meta["run_id"] if graph_meta else None,
                             input_logical_sha256=manifest["logical_sha256"], recipe=manifest["recipe"],
                             graph_status="experimental_features" if graph_meta else "disabled_by_config"))
