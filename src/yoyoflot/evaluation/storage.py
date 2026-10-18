"""Read-only integrity verification. Existing model pickles are never loaded."""
import json
from pathlib import Path

from ..models.demo import digest
from ..features.graph_artifact import _check_snapshot as check_graph
from ..models.if_artifact import _check_snapshot as check_forest
from ..provenance import fingerprint

ROOT_FILES = {"evaluation.json", "evaluation.md", "summary.txt", "comparison.csv", "stability.csv", "method_agreement.csv",
              "case_summary.csv", "paired_comparison.csv", "scores.parquet", "scenarios.parquet", "disagreements.parquet", "report.json",
              "config/evaluation.yaml", "config/isolation_forest.yaml", "config/model.yaml", "config/graph.yaml",
              "review/ranking_audit.csv", "review/blind/profiles.parquet", "review/blind/segments.parquet",
              "review/blind/coflight_context.parquet", "review/blind/instructions.md", "private/audit_key.parquet"}


def validate_snapshot(path, expected_request=None):
    path = Path(path).resolve()
    file = path / "run_manifest.json"
    if not file.is_file() or file.is_symlink() or file.stat().st_size > 2 * 1024**2:
        raise ValueError("Нужен завершённый снимок T20")
    manifest = json.loads(file.read_text(encoding="utf-8"))
    request = manifest.get("request")
    if (manifest.get("schema_version") != "1.0" or not isinstance(request, dict) or
            request.get("version") != "evaluation-demo-1.0" or request.get("data_mode") != "synthetic_demo" or
            manifest.get("run_id") != "evaluation-" + digest(request)[:20] or
            expected_request is not None and request != expected_request):
        raise ValueError("Неверная/несовместимая версия снимка T20")
    files = manifest.get("files")
    if not isinstance(files, dict) or not ROOT_FILES.issubset(files) or len(files) > 4000:
        raise ValueError("Неполный/слишком большой состав T20")
    total_size = 0
    for name, checksum in files.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Недопустимый путь снимка T20")
        item = path / relative
        if not item.is_file() or item.is_symlink() or item.stat().st_size > 64 * 1024**2:
            raise ValueError(f"Недопустимый файл T20: {name}")
        total_size += item.stat().st_size
        if total_size > 512 * 1024**2 or fingerprint(item) != checksum:
            raise ValueError(f"Изменён/слишком большой снимок T20: {name}")
    actual = {p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file()}
    if actual != set(files) | {"run_manifest.json"}:
        raise ValueError("Незаявленные файлы T20")
    expected = set(ROOT_FILES)
    if not isinstance(request.get("plan"), list) or not 3 <= len(request["plan"]) <= 32:
        raise ValueError("Неверный размер плана T20")
    for plan in request["plan"]:
        name = plan["name"]
        if not isinstance(name, str) or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-p" for c in name):
            raise ValueError("Неверное имя эксперимента")
        for config in ["isolation_forest.yaml", "model.yaml", "graph.yaml"]:
            expected.add(f"config/experiments/{name}/{config}")
        parent = path / "parents" / name
        nested = json.loads((parent / "run_manifest.json").read_text(encoding="utf-8"))
        check_forest(parent, nested["request"])
        if nested['request']['runtime'] != request['runtime'].get('training_runtime'):
            raise ValueError("Версии библиотек/кода T19 не соответствуют запуску T20")
        if nested["request"]["config"] != plan["forest"] or nested["request"]["datasets"]["evaluation"]["recipe"] != request["recipes"][name]:
            raise ValueError("Смешанный план/родитель T19")
        rules = json.loads((parent / "parents/evaluation/rules/model_metadata.json").read_text(encoding="utf-8"))
        if rules["config_hash"] != digest(plan["rules"]):
            raise ValueError("Смешанные правила эксперимента T20")
        if plan["forest"]["include_graph"]:
            graph = json.loads((parent / "parents/evaluation/graph/model_metadata.json").read_text(encoding="utf-8"))
            if graph["config"] != plan["graph"] or graph["m07_status"] != "disabled":
                raise ValueError("Смешанный граф эксперимента T20")
            for role in ["training", "calibration", "evaluation"]:
                graph_parent = parent / "parents" / role / "graph"
                graph_request = json.loads((graph_parent / "run_manifest.json").read_text(encoding="utf-8"))["request"]
                check_graph(graph_parent, graph_request)
        prefix = f"parents/{name}/"
        expected.add(prefix + "run_manifest.json")
        expected.update(prefix + n for n in nested["files"])
    if set(files) != expected:
        raise ValueError("Несогласованный полный состав T20")
    return json.loads((path / "report.json").read_text(encoding="utf-8"))
