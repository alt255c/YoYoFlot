"""Immutable T18 artifacts, accepting only the replay-verifiable T17 generator."""
import json
import platform
import shutil
import tempfile
import time
from datetime import date
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from .. import __version__
from ..models.demo import TABLES, digest, generate_rows, validate_demo, write_json
from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .graph import OUTPUT_SCHEMAS, START, VERSION, _build_graph, load_graph_config
from .graph_demo import OBSERVATION_SCHEMA, RULE_SCHEMA, _generate_observations

INPUT_EXTRA = {"graph_observations.parquet": OBSERVATION_SCHEMA, "codeshare_rules.parquet": RULE_SCHEMA}


def _runtime():
    package = Path(__file__).parents[1]
    files = [package / "__init__.py", package / "models/demo.py", package / "models/rule_score.py",
             package / "normalize/locking.py", package / "provenance.py", *Path(__file__).parent.glob("graph*.py")]
    return dict(package_version=__version__, python=platform.python_version(), pyarrow=pa.__version__,
                pyyaml=yaml.__version__, code_sha256=digest({str(f.relative_to(package)): fingerprint(f) for f in sorted(files)}))


def _check_snapshot(path, request):
    file = path / "run_manifest.json"
    if not file.is_file() or file.stat().st_size > 65536:
        raise ValueError("Существующий выход не является завершённым снимком T18")
    manifest = json.loads(file.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "1.0" or manifest.get("request") != request or
            manifest.get("run_id") != "graph-" + digest(request)[:20]):
        raise ValueError("Снимок T18 относится к другой версии; выберите новый выход")
    expected = set(OUTPUT_SCHEMAS) | {"input/" + n for n in {**TABLES, **INPUT_EXTRA}} | {
        "input/demo_manifest.json", "input/graph_input_manifest.json", "config/graph.yaml",
        "model_metadata.json", "report.json", "summary.txt"}
    checksums = manifest.get("files", {})
    if not isinstance(checksums, dict) or set(checksums) != expected:
        raise ValueError("Неполный состав снимка T18")
    for name, checksum in checksums.items():
        file = path / name
        if not file.is_file() or file.is_symlink() or fingerprint(file) != checksum:
            raise ValueError(f"Изменён файл снимка T18: {name}")
    source, people, segments = validate_demo(path / "input")
    if source["logical_sha256"] != request["input_logical_sha256"]:
        raise ValueError("Неверный учебный вход снимка T18")
    observations, rules, _ = _generate_observations(people, segments, request["config"]["graph_seed"])
    generated = {"graph_observations.parquet": observations, "codeshare_rules.parquet": rules}
    for name, rows in generated.items():
        if pq.ParquetFile(path / "input" / name).read().to_pylist() != rows:
            raise ValueError(f"Учебные свидетельства не воспроизводятся: {name}")
    return json.loads((path / "report.json").read_text(encoding="utf-8"))


def run(input_dir, config_path, output=None, artifacts_dir=Path("artifacts/t18"), progress=None):
    started = time.monotonic()
    config, config_bytes = load_graph_config(config_path)
    if progress:
        progress("T18: проверка независимого происхождения учебного входа T17")
    manifest, _, _ = validate_demo(input_dir)
    request = dict(schema_version="1.0", data_mode="synthetic_demo", input_logical_sha256=manifest["logical_sha256"],
                   recipe=manifest["recipe"], config=config, runtime=_runtime())
    run_id = "graph-" + digest(request)[:20]
    target = Path(output).resolve() if output is not None else Path(artifacts_dir).resolve() / run_id
    source = Path(input_dir).resolve()
    if target == source or target in source.parents or source in target.parents:
        raise ValueError("Выход T18 не должен пересекаться с входом")
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ".t18-graph.lock", "T18"):
        if target.exists():
            return _check_snapshot(target, request), (target / "summary.txt").read_text(encoding="utf-8")
        temporary = Path(tempfile.mkdtemp(prefix=".t18-graph-", suffix=".partial", dir=target.parent))
        try:
            people, segments = generate_rows(manifest["recipe"])
            observations, codeshares, graph_spec = _generate_observations(people, segments, config["graph_seed"])
            if progress:
                progress(f"T18: проверка свидетельств — {len(observations)}")
            tables, null_report = _build_graph(people, segments, observations, codeshares, config, progress)
            common = dict(run_id=run_id, data_mode="synthetic_demo", graph_version=VERSION,
                          window_start=START, window_end=date.fromisoformat(config["as_of"]))
            for name, rows in tables.items():
                metadata = {b"schema_version": b"1.0", b"data_mode": b"synthetic_demo", b"run_id": run_id.encode("ascii")}
                pq.write_table(pa.Table.from_pylist([dict(r, **common) for r in rows],
                                                    schema=OUTPUT_SCHEMAS[name].with_metadata(metadata)), temporary / name)
            frozen = temporary / "input"
            frozen.mkdir()
            inputs = {"profiles.parquet": people, "segments.parquet": segments,
                      "graph_observations.parquet": observations, "codeshare_rules.parquet": codeshares}
            for name, rows in inputs.items():
                pq.write_table(pa.Table.from_pylist(rows, schema={**TABLES, **INPUT_EXTRA}[name]), frozen / name)
            write_json(frozen / "demo_manifest.json", dict(manifest, files={n: fingerprint(frozen / n) for n in TABLES}))
            write_json(frozen / "graph_input_manifest.json", dict(
                schema_version="1.0", data_mode="synthetic_demo", origin="independent_generator", **graph_spec,
                base_logical_sha256=manifest["logical_sha256"], logical_sha256=digest([observations, codeshares]),
                files={n: fingerprint(frozen / n) for n in INPUT_EXTRA}))
            (temporary / "config").mkdir()
            (temporary / "config/graph.yaml").write_bytes(config_bytes)
            write_json(temporary / "model_metadata.json", dict(
                **request, run_id=run_id, graph_version=VERSION, graph_generator=graph_spec,
                null_model=null_report, m07_status="disabled", training_performed=False, ui_ready=False,
                acceptance="pending_T20_review", scope="independent_fictional_coflight_graph",
                limitation_ru="Условный фон сохраняет наблюдаемые размеры рейсов и активные дни/направления. Он не объясняет причины встреч."))
            links, features = tables["graph_links.parquet"], tables["graph_features.parquet"]
            edges = tables["coflight_edges.parquet"]
            excluded = {}
            for row in links:
                if not row["graph_eligible"]:
                    reason = row["eligibility_reason"]
                    excluded[reason] = excluded.get(reason, 0) + 1
            report = dict(schema_version="1.0", run_id=run_id, status="complete_demo", data_mode="synthetic_demo",
                          profiles=len(people), segments=len(segments), observations=len(observations),
                          table_rows={n: len(rows) for n, rows in tables.items()},
                          available_profiles=sum(r["available"] for r in features),
                          repeated_edges=sum(r["repeated"] for r in edges), excluded_observations=excluded,
                          null_model=null_report, as_of=config["as_of"], m07_status="disabled", training_performed=False,
                          ui_ready=False, acceptance="pending_T20_review", elapsed_seconds=round(time.monotonic() - started, 3))
            summary = (f"Итог T18 (независимые вымышленные профили)\nПрофилей: {len(people)}; допустимы для графа: {report['available_profiles']}\n"
                       f"Событий: {len(tables['graph_events.parquet'])}; рёбер: {len(edges)}; повторных: {report['repeated_edges']}\n"
                       f"Окно: {START}–{config['as_of']}; перестановок фона: {null_report['simulations']}\n"
                       f"M07: выключен до T20; обучение не выполнялось\nВремя: {report['elapsed_seconds']} с\nРезультаты: {target}\n")
            write_json(temporary / "report.json", report)
            (temporary / "summary.txt").write_text(summary, encoding="utf-8")
            checksums = {str(p.relative_to(temporary)).replace("\\", "/"): fingerprint(p)
                         for p in sorted(temporary.rglob("*")) if p.is_file()}
            write_json(temporary / "run_manifest.json", dict(schema_version="1.0", request=request, run_id=run_id, files=checksums))
            _check_snapshot(temporary, request)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return report, summary
