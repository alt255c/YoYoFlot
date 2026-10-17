"""T16: resumable, fingerprinted data builds and immutable internal snapshots.

This module does not train a model, rank people, or publish a web product.
Existing readers and T11--T14 retain ownership of their transformation rules.
"""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

import pyarrow.parquet as pq
import yaml

from . import __version__
from .contracts import CONTRACT_VERSION, SCHEMAS as READER_SCHEMAS
from .io import atomic_json, ingest
from .normalize.airports import AirportReference
from .normalize.canonical import SCHEMAS as CANONICAL_SCHEMAS, build
from .normalize.geography import run as geography
from .normalize.locking import OutputLock
from .provenance import fingerprint
from .quality import audit
from .resolve import VERSION as RESOLUTION_VERSION
from .resolve.common import Rules
from .resolve.common import SCHEMAS as RESOLUTION_SCHEMAS
from .resolve.pipeline import run as resolve
from .resolve.pipeline import verify_inputs

VERSION = "1.0"
ROOT = Path(__file__).resolve().parents[2]
SOURCES = (
    "boarding_csv", "forum_json", "pointz_xml", "sirena_fixed",
    "skyteam_yaml", "boardingpass_xlsx", "timetable_pdf",
)
CANONICAL_FILES = tuple(f"{name}.parquet" for name in CANONICAL_SCHEMAS) + (
    "normalization_errors.parquet", "transformation_log.parquet", "duplicate_reads.parquet",
    "canonical_report.json", "canonical_summary.txt",
)
GEOGRAPHY_FILES = (
    "travel_geography.parquet", "schedule_geography.parquet", "schedule_links.parquet",
    "place_inventory.parquet", "geography_report.json", "geography_summary.txt",
)
RESOLUTION_FILES = tuple(f"{name}.parquet" for name in RESOLUTION_SCHEMAS) + (
    "resolution_review_sample.csv", "resolution_report.json", "resolution_summary.txt",
)
PUBLISHED = {
    "canonical_manifest.json": CANONICAL_FILES,
    "t11_manifest.json": GEOGRAPHY_FILES,
    "resolution_manifest.json": RESOLUTION_FILES,
}


def _json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _signature(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")).hexdigest()


def _describe(path):
    path = Path(path)
    result = {"sha256": fingerprint(path), "bytes": path.stat().st_size}
    if path.suffix == ".parquet":
        pf = pq.ParquetFile(path)
        result.update(rows=pf.metadata.num_rows,
                      schema_sha256=sha256(pf.schema_arrow.serialize().to_pybytes()).hexdigest())
    return result


def _inventory(paths):
    return {str(Path(p).resolve()): _describe(p) for p in sorted(set(map(Path, paths)))}


def _unchanged(files):
    return all(Path(p).is_file() and _describe(p) == info for p, info in files.items())


def _environment():
    versions = {}
    for name in ("duckdb", "pyarrow", "ijson", "lxml", "openpyxl", "pymupdf",
                 "pyyaml", "scikit-learn", "tzdata"):
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    package = Path(__file__).parent
    code = {str(p.relative_to(package)): fingerprint(p) for p in sorted(package.rglob("*.py"))}
    return {"pipeline_version": VERSION, "package_version": __version__,
            "python": platform.python_version(), "platform": platform.system(),
            "dependencies": versions, "source_files": code, "code_sha256": _signature(code)}


class _Log:
    def __init__(self, path, progress):
        self.path, self.progress = path, progress
        self.lock = threading.Lock()

    def event(self, event, **details):
        with self.lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                 "event": event, **details}, ensure_ascii=False) + "\n")

    def message(self, text):
        self.event("progress", message=text)
        if self.progress is not None:
            self.progress(text)


class _Heartbeat:
    def __init__(self, log, phase):
        self.log, self.phase = log, phase
        self.stop = threading.Event()

    def __enter__(self):
        started = time.perf_counter()

        def tick():
            while not self.stop.wait(30):
                self.log.message(f"T16: {self.phase} — {int(time.perf_counter() - started)} с")

        self.thread = threading.Thread(target=tick, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()


class _Stages:
    def __init__(self, work, environment, log):
        self.path, self.environment, self.log = work / "pipeline_state.json", environment, log
        self.timings = {}
        self.current = "preflight"
        try:
            self.state = _json(self.path) if self.path.is_file() else {}
        except (ValueError, OSError):
            self.state = {}
            log.event("cache_state_invalid", action="recompute")
        if self.state.get("version") != VERSION or not isinstance(self.state.get("stages"), dict):
            self.state = {"version": VERSION, "stages": {}}

    def run(self, name, inputs, options, action, outputs):
        self.current = name
        started = time.perf_counter()
        key = _signature({"environment": self.environment, "inputs": inputs, "options": options})
        previous = self.state["stages"].get(name, {})
        if not isinstance(previous, dict):
            previous = {}
        self.log.message(f"T16: {name} — проверка/запуск")
        with _Heartbeat(self.log, name):
            if previous.get("status") == "complete" and previous.get("signature") == key:
                try:
                    cached = previous.get("outputs", {})
                    expected = {str(p.resolve()) for p in outputs()}
                    valid = bool(cached) and set(cached) == expected and _unchanged(cached)
                except (OSError, ValueError, TypeError, AttributeError):
                    valid = False
                if valid:
                    self.timings[name] = {"action": "reused", "seconds": round(time.perf_counter() - started, 3)}
                    self.log.message(f"T16: {name} — готовый этап подтверждён по SHA-256")
                    return cached
            self.state["stages"][name] = {"status": "running", "signature": key}
            atomic_json(self.path, self.state)
            try:
                action()
                files = _inventory(outputs())
                if not files:
                    raise RuntimeError(f"Stage produced no files: {name}")
                self.state["stages"][name] = {"status": "complete", "signature": key, "outputs": files}
                atomic_json(self.path, self.state)
            except BaseException as exc:
                self.state["stages"][name].update(
                    status="interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                    exception_type=type(exc).__name__, message=str(exc))
                atomic_json(self.path, self.state)
                raise
        self.timings[name] = {"action": "executed", "seconds": round(time.perf_counter() - started, 3)}
        self.log.event("stage_complete", stage=name, **self.timings[name])
        return files


def _reader_files(root):
    paths = [root / (name + ".parquet") for name in READER_SCHEMAS]
    paths.append(root / "source_report.json")
    paths.extend(root / name for name in (
        "workbook_coverage.csv", "page_coverage.csv", "page_text.zip", "schedules.csv",
        "schedules.jsonl", "parser_comparison.json",
    ) if (root / name).is_file())
    if (root / "problem_pages").is_dir():
        paths.extend(p for p in (root / "problem_pages").rglob("*") if p.is_file())
    return paths


def _check_reader(root, name):
    report = _json(root / "source_report.json")
    if report.get("source_id") != name:
        raise ValueError(f"Wrong source_report source_id: {name}")
    if report.get("contract_version") != CONTRACT_VERSION:
        raise ValueError(f"Unsupported reader contract: {name}")
    if report.get("status") not in ("complete", "complete_with_errors", "incomplete"):
        raise ValueError(f"Reader is not finished: {name}")
    for table, schema in READER_SCHEMAS.items():
        pf = pq.ParquetFile(root / (table + ".parquet"))
        if not pf.schema_arrow.equals(schema) or pf.metadata.num_rows != report.get("counts", {}).get(table):
            raise ValueError(f"Reader schema/count mismatch: {name}/{table}")
    return report


def _source_limitations(report, label, allow_errors):
    errors = report.get("error_severities", {}).get("error", 0)
    limited = report.get("status") != "complete" or bool(errors)
    if limited and not allow_errors:
        raise ValueError(f"{label}: source errors/incomplete coverage; inspect its report. "
                         "Use --allow-source-errors only to retain these explicit limitations.")
    return [f"{label}:{report.get('status')}:{errors}_errors"] if limited else []


def _published(root, manifest_name, verify=False, known=None):
    manifest = _json(root / manifest_name)
    expected = set(PUBLISHED[manifest_name])
    if set(manifest.get("files", {})) != expected:
        raise ValueError(f"Unexpected file set: {manifest_name}")
    if verify:
        for name, digest in manifest["files"].items():
            actual = (known or {}).get(str((root / name).resolve()), {}).get("sha256")
            if (actual if actual is not None else fingerprint(root / name)) != digest:
                raise ValueError(f"Published checksum mismatch: {root / name}")
    return [root / name for name in sorted(expected)] + [root / manifest_name]


def _matches_input(report, name, digest):
    matches = [v for k, v in report.get("inputs", {}).items()
               if k.replace("\\", "/").rsplit("/", 1)[-1] == name]
    return bool(matches) and all(v == digest for v in matches)


def _check_processed(root, normalization, resolution, reference, overrides, require_all, known=None, expected_sources=None):
    for manifest in PUBLISHED:
        _published(root, manifest, verify=True, known=known)
    canonical = _json(root / "canonical_report.json")
    geo = _json(root / "geography_report.json")
    resolved = _json(root / "resolution_report.json")
    if canonical.get("status") not in ("complete", "complete_with_errors") or geo.get("status") != "complete" or resolved.get("status") != "complete":
        raise ValueError("Processed inputs are not completed T11/T12/T13 outputs")
    if canonical.get("normalization_errors") != 0:
        raise ValueError("T12 normalization errors must be resolved before T16 publication")
    for report, path in ((canonical, normalization), (resolved, resolution)):
        if report.get("config_sha256") != fingerprint(path):
            raise ValueError(f"Configuration differs from processed snapshot: {path}")
    if geo.get("reference_sha256") != fingerprint(reference) or geo.get("overrides_sha256") != fingerprint(overrides):
        raise ValueError("Airport reference/overrides differ from processed snapshot")
    if resolved.get("version") != RESOLUTION_VERSION:
        raise ValueError("Unsupported resolution version")
    # T13's own input validator checks canonical schemas and the T11/T12 hash link.
    verify_inputs(root, Rules(resolution), type("Quiet", (), {"phase": lambda *_: None})())
    for name in ("canonical_manifest.json", "t11_manifest.json", "observations.parquet",
                 "identifiers.parquet", "travel_observations.parquet", "canonical_report.json",
                 "travel_geography.parquet", "geography_report.json"):
        if not _matches_input(resolved, name, fingerprint(root / name)):
            raise ValueError(f"T13 was built from another upstream snapshot: {name}")
    for report, schemas in ((canonical, CANONICAL_SCHEMAS), (resolved, RESOLUTION_SCHEMAS)):
        for name, schema in schemas.items():
            pf = pq.ParquetFile(root / (name + ".parquet"))
            if not pf.schema_arrow.equals(schema) or pf.metadata.num_rows != report.get("counts", {}).get(name):
                raise ValueError(f"Processed schema/count mismatch: {name}")
    present = {r.get("source_id") for r in canonical.get("source_reports", {}).values()}
    if expected_sources is not None and present != set(expected_sources):
        raise ValueError("Configured sources differ from processed source reports")
    if require_all:
        if set(SOURCES) - present:
            raise ValueError("Missing source reports in processed snapshot: " + ", ".join(sorted(set(SOURCES) - present)))
        if set(SOURCES[:-1]) - set(resolved.get("personal_sources_present", [])):
            raise ValueError("Missing personal sources in T13 snapshot")
    return canonical, geo, resolved


def _columns(config, source_config):
    value = Path(config)
    if value.is_absolute():
        return value
    for relative in (source_config.parent / value, Path.cwd() / value, ROOT / value):
        if relative.is_file():
            return relative.resolve()
    raise FileNotFoundError(f"Column configuration missing: {value}")


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


def _verify_snapshot(output, request):
    manifest = _json(output / "run_manifest.json")
    if manifest.get("pipeline_version") != VERSION or manifest.get("status") not in ("complete", "complete_with_limitations"):
        raise ValueError(f"Not a completed T16 snapshot: {output}")
    if manifest.get("request_sha256") != request:
        raise FileExistsError(f"Immutable output belongs to another request: {output}. Choose another --output.")
    expected = set(manifest.get("files", {}))
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    if not expected or actual != expected | {"run_manifest.json"}:
        raise ValueError(f"Snapshot inventory mismatch: {output}")
    for name, info in manifest["files"].items():
        path = (output / name).resolve()
        if output not in path.parents or _describe(path) != info:
            raise ValueError(f"Snapshot checksum mismatch: {name}")
    return manifest


def _summary(report, output, elapsed, reused=False):
    counts = report.get("counts", {})
    return (f"Итог T16\nСтатус: {report['status']}\nrun_id: {report['run_id']}\n"
            f"Наблюдения людей: {counts.get('observations', 0)}\n"
            f"Наблюдения поездок: {counts.get('travel_observations', 0)}\n"
            f"Люди: {counts.get('persons', 0)}; сегменты: {counts.get('segments', 0)}\n"
            f"Снимок: {output}\nПовторно использован снимок: {'да' if reused else 'нет'}\n"
            f"Время: {elapsed:.3f} с\nМодель/интерфейс: не входят в T16; независимая приёмка ожидается\n")


def run(config="configs/sources.yaml", data_dir="data/raw", work_dir="data/work/t16",
        output=None, from_interim=None, from_processed=None, artifacts_dir="artifacts",
        normalization="configs/normalization.yaml", resolution="configs/resolution.yaml",
        reference="data/reference/airports.csv", overrides="configs/airports_overrides.yaml",
        reference_manifest=None, batch_size=4000, memory_limit="1GB", chunk_pages=100,
        sample_size=30, require_all=False, allow_source_errors=False, progress=None):
    """Build T12 -> T11 -> T13 -> T14, or verify and adopt completed inputs.

    A return value denotes technical completion, never independent acceptance.
    Source errors require explicit consent; structural quality blockers never do.
    No existing data/processed directory or completed snapshot is overwritten.
    """
    started = time.perf_counter()
    if min(batch_size, chunk_pages, sample_size) < 1:
        raise ValueError("batch_size, chunk_pages and sample_size must be positive")
    if from_interim is not None and from_processed is not None:
        raise ValueError("Choose only one of --from-interim / --from-processed")
    import duckdb
    with duckdb.connect() as db:
        db.execute("SET memory_limit=?", [memory_limit])
    work = Path(work_dir).resolve()
    input_root = Path(from_processed or from_interim or data_dir).resolve()
    if _overlap(work, input_root):
        raise ValueError("Work directory must not overlap the input directory")
    if output is not None and (_overlap(Path(output).resolve(), work) or _overlap(Path(output).resolve(), input_root)):
        raise ValueError("Output must not overlap input or work directories")
    config, normalization, resolution, reference, overrides = map(
        lambda p: Path(p).resolve(), (config, normalization, resolution, reference, overrides))
    reference_manifest = Path(reference_manifest).resolve() if reference_manifest else reference.parent / "reference_manifest.json"
    if any(work == p or work in p.parents for p in (config, normalization, resolution, reference, overrides, reference_manifest)):
        raise ValueError("Configuration/reference files must be outside the work directory")
    work.mkdir(parents=True, exist_ok=True)
    with OutputLock(work / ".t16.lock", label="T16"):
        logs = work / "logs"
        logs.mkdir(exist_ok=True)
        log = _Log(logs / (uuid.uuid4().hex + ".jsonl"), progress)
        environment = _environment()
        stages = _Stages(work, environment, log)
        log.event("run_started", scope="internal_data_core")
        destination = None
        try:
            with _Heartbeat(log, "проверка входов"):
                cfg = yaml.safe_load(config.read_text(encoding="utf-8"))
                sources = cfg.get("sources") if isinstance(cfg, dict) else None
                if not isinstance(sources, dict) or not sources or set(sources) - set(SOURCES):
                    raise ValueError("sources must contain known, explicitly configured readers")
                if require_all and set(sources) != set(SOURCES):
                    raise ValueError("--require-all requires all seven configured sources")
                AirportReference(reference, overrides, reference_manifest)
                configs = {"sources.yaml": config, "normalization.yaml": normalization,
                           "resolution.yaml": resolution, "airports_overrides.yaml": overrides}
                inputs = {"config:" + k: v for k, v in configs.items()}
                ref_files = {"airports.csv": reference, "reference_manifest.json": reference_manifest}
                for name, digest in _json(reference_manifest).get("files", {}).items():
                    candidate = reference.parent / name
                    if candidate.name != name or not candidate.is_file() or fingerprint(candidate) != digest:
                        raise ValueError(f"Reference manifest file mismatch: {name}")
                    ref_files[name] = candidate
                inputs.update({"reference:" + k: v for k, v in ref_files.items()})
                mode = "processed" if from_processed else "interim" if from_interim else "raw"
                reader_roots, reader_options = {}, {}
                if mode == "processed":
                    for manifest in PUBLISHED:
                        for path in _published(input_root, manifest):
                            inputs["processed:" + path.name] = path
                else:
                    for name in SOURCES:
                        if name not in sources:
                            continue
                        source = sources[name]
                        if not isinstance(source, dict) or source.get("reader", name) != name:
                            raise ValueError(f"Invalid source configuration: {name}")
                        options = {"batch_size": batch_size}
                        if name == "boarding_csv":
                            options["encoding"] = source.get("encoding", "utf-8-sig")
                        if name == "sirena_fixed":
                            columns = _columns(source.get("columns", "configs/sirena_columns.yaml"), config)
                            inputs["config:sirena_columns.yaml"] = columns
                            configs["sirena_columns.yaml"] = columns
                            options["columns"] = str(columns)
                        reader_options[name] = options
                        if mode == "raw":
                            path = input_root / source["file"]
                            inputs["raw:" + name] = path
                            reader_roots[name] = work / "interim" / name
                        else:
                            directory = input_root / name
                            _check_reader(directory, name)
                            reader_roots[name] = directory
                            for path in _reader_files(directory):
                                inputs[f"interim:{name}/{path.relative_to(directory).as_posix()}"] = path
                initial = {label: {"path": str(path.resolve()), **_describe(path)} for label, path in sorted(inputs.items())}
                options = {"batch_size": batch_size, "memory_limit": memory_limit,
                           "chunk_pages": chunk_pages, "sample_size": sample_size,
                           "require_all": require_all, "allow_source_errors": allow_source_errors}
                request = _signature({"environment": environment, "mode": mode, "options": options,
                                      "inputs": {k: v["sha256"] for k, v in initial.items()}})
                destination = Path(output).resolve() if output else (Path(artifacts_dir) / ("data-" + request[:20])).resolve()
                if _overlap(destination, work) or _overlap(destination, input_root):
                    raise ValueError("Output must not overlap input or work directories")
            stages.timings["preflight"] = {"action": "executed", "seconds": round(time.perf_counter() - started, 3)}
            if destination.exists():
                with _Heartbeat(log, "проверка готового снимка"):
                    report = _verify_snapshot(destination, request)
                    if not _unchanged({v["path"]: {k: x for k, x in v.items() if k != "path"} for v in initial.values()}):
                        raise RuntimeError("Input changed during T16 snapshot verification")
                log.message("T16: завершённый снимок подтверждён по SHA-256; этапы не повторяются")
                atomic_json(work / "last_run.json", {"status": report["status"], "snapshot": str(destination),
                            "request_sha256": request, "reused_snapshot": True, "seconds": round(time.perf_counter() - started, 3)})
                return report, _summary(report, destination, time.perf_counter() - started, reused=True)
            limitations = []
            if set(sources) != set(SOURCES):
                limitations.append("partial_source_configuration")
            if mode == "processed":
                known = {v["path"]: {k: x for k, x in v.items() if k != "path"} for v in initial.values()}
                stages.run("adopt_processed", initial, {},
                           lambda: _check_processed(input_root, normalization, resolution, reference, overrides, require_all, known, sources),
                           lambda: [p for m in PUBLISHED for p in _published(input_root, m)])
                processed = input_root
                limitations.append("raw_files_not_revalidated:processed_input")
            else:
                for name, directory in reader_roots.items():
                    if mode == "raw":
                        source_path = inputs["raw:" + name]
                        reader = importlib.import_module("yoyoflot.readers." + name)
                        params = reader_options[name]
                        signature_inputs = {"raw": initial["raw:" + name]}
                        if name == "sirena_fixed":
                            signature_inputs["columns"] = initial["config:sirena_columns.yaml"]

                        def ingest_source(name=name, directory=directory, reader=reader,
                                          source_path=source_path, params=params):
                            if name == "timetable_pdf":
                                reader.process(source_path, directory, chunk_pages=chunk_pages, force=True)
                            else:
                                ingest(reader.read, source_path, name, directory, **params)
                            report = _check_reader(directory, name)
                            if report.get("sha256") != initial["raw:" + name]["sha256"]:
                                raise RuntimeError("Raw input changed before ingestion: " + name)
                            if name == "sirena_fixed" and report.get("configuration_sha256") != initial["config:sirena_columns.yaml"]["sha256"]:
                                raise RuntimeError("Column configuration changed before ingestion")

                        stages.run("ingest:" + name, signature_inputs,
                                   {**params, "chunk_pages": chunk_pages if name == "timetable_pdf" else None},
                                   ingest_source, lambda directory=directory: _reader_files(directory))
                    report = _check_reader(directory, name)
                    if mode == "raw" and report.get("sha256") != initial["raw:" + name]["sha256"]:
                        raise RuntimeError("Raw input changed before ingestion: " + name)
                    if name == "sirena_fixed" and report.get("configuration_sha256") != fingerprint(reader_options[name]["columns"]):
                        raise ValueError("Sirena column configuration differs from reader report")
                    limitations.extend(_source_limitations(report, name, allow_source_errors))
                if mode == "interim":
                    limitations.append("raw_files_not_revalidated:interim_input")
                processed = work / "processed"
                reader_files = _inventory([p for directory in reader_roots.values() for p in _reader_files(directory)])
                stages.run("canonical", reader_files, {"config": fingerprint(normalization), "batch_size": batch_size,
                           "memory_limit": memory_limit, "require_all": require_all},
                           lambda: build(list(reader_roots.values()), processed, normalization, batch_size, memory_limit,
                                         require_all, progress=log.message),
                           lambda: _published(processed, "canonical_manifest.json"))
                canonical = _json(processed / "canonical_report.json")
                if canonical.get("normalization_errors") != 0:
                    raise ValueError("T12 normalization errors must be resolved before T16 publication")
                schedules = [reader_roots["timetable_pdf"] / "schedules.parquet"] if "timetable_pdf" in reader_roots else []
                geo_inputs = _inventory([processed / "travel_observations.parquet", reference, reference_manifest, overrides]
                                        + schedules + [p.parent / "source_report.json" for p in schedules])
                stages.run("geography", geo_inputs, {"batch_size": batch_size},
                           lambda: geography([processed], processed, reference, overrides, reference_manifest,
                                             schedules, batch_size, progress=log.message),
                           lambda: _published(processed, "t11_manifest.json"))
                upstream = _inventory([p for m in ("canonical_manifest.json", "t11_manifest.json") for p in _published(processed, m)])
                stages.run("resolution", upstream, {"config": fingerprint(resolution), "batch_size": batch_size,
                           "memory_limit": memory_limit, "require_all": require_all},
                           lambda: resolve(processed, processed, resolution, work / "review", batch_size,
                                           memory_limit, require_all, progress=log.message),
                           lambda: _published(processed, "resolution_manifest.json"))
            known = {p: info for stage in stages.state["stages"].values()
                     if stage.get("status") == "complete" for p, info in stage.get("outputs", {}).items()}
            canonical, geo, resolved = _check_processed(processed, normalization, resolution, reference, overrides, require_all, known, sources)
            limitations.extend("unobserved_personal_source:" + name for name in resolved.get("personal_sources_missing", []))
            for name, report in canonical.get("source_reports", {}).items():
                limitations.extend(_source_limitations(report, report.get("source_id", name), allow_source_errors))
            if canonical.get("source_error_severities", {}).get("error"):
                if not allow_source_errors:
                    raise ValueError("Upstream reader errors require --allow-source-errors")
                limitations.append("upstream_reader_errors_preserved")
            if any(s.get("coverage") != "confirmed" for s in geo.get("schedule_sources", [])):
                if not allow_source_errors:
                    raise ValueError("Incomplete schedule coverage requires --allow-source-errors")
                limitations.append("schedule_coverage_unconfirmed")
            quality_dir = work / "quality"

            def quality():
                report = audit(processed, quality_dir, sample_size, memory_limit)
                if report["status"] == "failed":
                    raise ValueError("T14 structural blockers: " + ", ".join(report["blockers"]))

            published_files = [p for m in PUBLISHED for p in _published(processed, m)]
            published_inventory = _inventory(published_files)
            quality_inventory = stages.run("quality", published_inventory, {"sample_size": sample_size, "memory_limit": memory_limit},
                       quality, lambda: [quality_dir / name for name in ("quality.json", "quality.md", "quality_review_sample.csv")])
            quality_report = _json(quality_dir / "quality.json")
            limitations.extend(quality_report["limitations"])
            stages.current = "publish"
            publish_started = time.perf_counter()
            destination.parent.mkdir(parents=True, exist_ok=True)
            with OutputLock(destination.parent / ("." + destination.name + ".publish.lock"), label="T16 publication"):
                if destination.exists():
                    report = _verify_snapshot(destination, request)
                    return report, _summary(report, destination, time.perf_counter() - started, reused=True)
                with _Heartbeat(log, "сохранение снимка"), tempfile.TemporaryDirectory(
                        prefix="." + destination.name + ".", suffix=".partial", dir=destination.parent) as temp:
                    staging = Path(temp) / "snapshot"
                    staging.mkdir()
                    files = {}

                    expected_copies = {v["path"]: v for v in initial.values()}
                    expected_copies.update(published_inventory)
                    expected_copies.update(quality_inventory)
                    for stage in stages.state["stages"].values():
                        if stage.get("status") == "complete":
                            expected_copies.update(stage.get("outputs", {}))

                    def copy(source, relative):
                        target = staging / relative
                        target.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(source, target)
                        description = _describe(target)
                        expected = expected_copies.get(str(source.resolve()), {}).get("sha256")
                        if (expected if expected is not None else fingerprint(source)) != description["sha256"]:
                            raise RuntimeError(f"File changed during snapshot copy: {source}")
                        files[relative] = description

                    for path in published_files:
                        copy(path, "tables/" + path.name)
                    for name, path in configs.items():
                        copy(path, "configs/" + name)
                    for name, path in ref_files.items():
                        copy(path, "reference/" + name)
                    for name in ("quality.json", "quality.md"):
                        copy(quality_dir / name, name)
                    copy(quality_dir / "quality_review_sample.csv", "review/quality_review_sample.csv")
                    for name, directory in reader_roots.items():
                        for path in _reader_files(directory):
                            if path.suffix != ".parquet":
                                copy(path, f"sources/{name}/" + path.relative_to(directory).as_posix())
                    # Re-hash initial files after all processing/copies, not just their mtimes.
                    for info in initial.values():
                        if _describe(info["path"]) != {k: v for k, v in info.items() if k != "path"}:
                            raise RuntimeError("Input changed during T16: " + info["path"])
                    if not _unchanged(published_inventory):
                        raise RuntimeError("Processed files changed during publication")
                    input_manifest = {"mode": mode, "raw_files_verified": mode == "raw", "files": initial}
                    atomic_json(staging / "input_manifest.json", input_manifest)
                    atomic_json(staging / "environment.json", environment)
                    stages.timings["publish"] = {"action": "executed", "seconds": round(time.perf_counter() - publish_started, 3)}
                    atomic_json(staging / "timings.json", stages.timings)
                    log.event("snapshot_prepared", run_id=destination.name)
                    with log.lock:
                        copy(log.path, "logs/run.jsonl")
                    for name in ("input_manifest.json", "environment.json", "timings.json"):
                        files[name] = _describe(staging / name)
                    report = {"pipeline_version": VERSION, "run_id": destination.name, "request_sha256": request,
                              "status": "complete_with_limitations" if limitations else "complete",
                              "scope": "internal_data_core", "ui_ready": False, "model_built": False,
                              "acceptance": "pending_real_data_and_independent_review", "input_mode": mode,
                              "configured_sources": sorted(sources),
                              "personal_sources_present": resolved.get("personal_sources_present", []),
                              "personal_sources_missing": resolved.get("personal_sources_missing", []),
                              "created_at": datetime.now(timezone.utc).isoformat(), "options": options,
                              "seconds": round(time.perf_counter() - started, 3),
                              "counts": {**canonical["counts"], **resolved["counts"]}, "quality_status": quality_report["status"],
                              "limitations": sorted(set(limitations)), "files": files}
                    atomic_json(staging / "run_manifest.json", report)
                    os.rename(staging, destination)
            log.event("snapshot_published", run_id=destination.name)
            atomic_json(work / "last_run.json", {"status": report["status"], "snapshot": str(destination),
                        "request_sha256": request, "reused_snapshot": False, "stages": stages.timings,
                        "seconds": round(time.perf_counter() - started, 3)})
            return report, _summary(report, destination, time.perf_counter() - started)
        except BaseException as exc:
            diagnostic = {"status": "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed",
                          "phase": stages.current, "exception_type": type(exc).__name__, "message": str(exc),
                          "snapshot": str(destination) if destination else None,
                          "seconds": round(time.perf_counter() - started, 3)}
            atomic_json(work / "last_failure.json", diagnostic)
            atomic_json(work / "last_run.json", diagnostic)
            log.event("run_failed", **diagnostic)
            raise
