import argparse
import importlib
import json
import sys
from pathlib import Path

from .config import load_sources, resolve_path
from .io import ingest
from .provenance import fingerprint
from . import __version__

READERS = [
    "boarding_csv",
    "forum_json",
    "pointz_xml",
    "sirena_fixed",
    "skyteam_yaml",
    "boardingpass_xlsx",
    "timetable_pdf",
]


def parser():
    p = argparse.ArgumentParser(prog="yoyoflot", description="YoYoFlot data pipeline and independent fictional rules demo")
    p.add_argument("--version", action="version", version="yoyoflot " + __version__)
    sub = p.add_subparsers(dest="command", required=True)
    inv = sub.add_parser(
        "inventory", help="File metadata and hashes (not full T01 acceptance)"
    )
    inv.add_argument("--data-dir", default="data/raw")
    ing = sub.add_parser("ingest", help="Ingest one source or all configured sources")
    ing.add_argument("source", choices=READERS + ["all"])
    ing.add_argument("--path", type=Path)
    ing.add_argument("--data-dir", default="data/raw")
    ing.add_argument("--config", default="configs/sources.yaml")
    ing.add_argument("--output", type=Path, default=Path("data/interim"))
    ing.add_argument("--batch-size", type=int, default=1000)
    ing.add_argument("--columns", type=Path)
    ing.add_argument("--encoding")
    ing.add_argument("--chunk-pages", type=int, default=100)
    ing.add_argument("--limit-pages", type=int)
    ing.add_argument("--ocr", action="store_true")
    exp = sub.add_parser("export", help="Stream Parquet to CSV and JSONL")
    exp.add_argument("path", type=Path)
    exp.add_argument("--output", type=Path, required=True)
    audit = sub.add_parser("audit", help="Make a blank PDF review sample")
    audit.add_argument(
        "--output", type=Path, default=Path("data/interim/timetable_pdf")
    )
    audit.add_argument("--rows", type=int, default=50)
    geo = sub.add_parser("airports", help="T11: normalize geography and link compatible schedules")
    geo.add_argument("--input", type=Path, nargs="+", required=True)
    geo.add_argument("--output", type=Path, default=Path("data/processed"))
    geo.add_argument("--reference", type=Path, default=Path("data/reference/airports.csv"))
    geo.add_argument("--reference-manifest", type=Path)
    geo.add_argument("--overrides", type=Path, default=Path("configs/airports_overrides.yaml"))
    geo.add_argument("--schedules", type=Path, nargs="+")
    geo.add_argument("--batch-size", type=int, default=2000)
    geo.add_argument("-q", "--quiet", action="store_true")
    canonical = sub.add_parser("build", help="T12: normalize and assemble canonical reader tables")
    canonical.add_argument("--input", type=Path, nargs="+", default=[Path("data/interim/real")])
    canonical.add_argument("--output", type=Path, default=Path("data/processed"))
    canonical.add_argument("--config", type=Path, default=Path("configs/normalization.yaml"))
    canonical.add_argument("--batch-size", type=int, default=4000)
    canonical.add_argument("--memory-limit", default="512MB")
    canonical.add_argument("--require-all", action="store_true")
    canonical.add_argument("-q", "--quiet", action="store_true")
    resolution = sub.add_parser("resolve", help="T13: resolve passenger identities and duplicate journey evidence")
    resolution.add_argument("--input", type=Path, default=Path("data/processed"))
    resolution.add_argument("--output", type=Path, default=Path("data/processed"))
    resolution.add_argument("--config", type=Path, default=Path("configs/resolution.yaml"))
    resolution.add_argument("--review-output", type=Path, default=Path("data/review"))
    resolution.add_argument("--batch-size", type=int, default=4000)
    resolution.add_argument("--memory-limit", default="1GB", help="DuckDB memory limit (not total Python RAM)")
    resolution.add_argument("--require-all", action="store_true")
    resolution.add_argument("-q", "--quiet", action="store_true")
    review = sub.add_parser("resolution-review", help="T13: audit an independently reviewed CSV without rerunning matching")
    review.add_argument("--sample", type=Path, default=Path("data/review/person_pairs.csv"))
    review.add_argument("--input", type=Path, default=Path("data/processed"))
    review.add_argument("--report", type=Path, default=Path("data/review/person_pairs_review_report.json"))
    review.add_argument("-q", "--quiet", action="store_true")
    quality = sub.add_parser("quality", help="T14: audit canonical data quality and provenance")
    quality.add_argument("--input", type=Path, default=Path("data/processed"))
    quality.add_argument("--output", type=Path)
    quality.add_argument("--sample-size", type=int, default=30)
    quality.add_argument("--memory-limit", default="512MB")
    pipeline = sub.add_parser("run", help="T16: reproducible data core, without a model or web UI")
    pipeline.add_argument("--config", type=Path, default=Path("configs/sources.yaml"))
    pipeline.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    pipeline.add_argument("--work-dir", type=Path, default=Path("data/work/t16"))
    pipeline.add_argument("--artifacts-dir", type=Path, default=Path("artifacts"), help="Parent for automatic snapshot names")
    pipeline.add_argument("--output", type=Path, help="New immutable snapshot directory; default: artifacts/data-<fingerprint>")
    start = pipeline.add_mutually_exclusive_group()
    start.add_argument("--from-interim", type=Path, help="Use completed reader outputs; original raw files are not revalidated")
    start.add_argument("--from-processed", type=Path, help="Verify/adopt existing T11/T12/T13; do not repeat their calculations")
    pipeline.add_argument("--until", choices=["build"], default="build", help="build = entire data core through T14, not only T12")
    pipeline.add_argument("--normalization-config", type=Path, default=Path("configs/normalization.yaml"))
    pipeline.add_argument("--resolution-config", type=Path, default=Path("configs/resolution.yaml"))
    pipeline.add_argument("--reference", type=Path, default=Path("data/reference/airports.csv"))
    pipeline.add_argument("--reference-manifest", type=Path)
    pipeline.add_argument("--overrides", type=Path, default=Path("configs/airports_overrides.yaml"))
    pipeline.add_argument("--batch-size", type=int, default=4000)
    pipeline.add_argument("--chunk-pages", type=int, default=100)
    pipeline.add_argument("--sample-size", type=int, default=30)
    pipeline.add_argument("--memory-limit", default="1GB", help="DuckDB limit, not a total Python RAM limit")
    pipeline.add_argument("--require-all", action="store_true", help="Require all seven logical sources")
    pipeline.add_argument("--allow-source-errors", action="store_true", help="Keep documented parser/PDF limitations; never bypass structural blockers")
    pipeline.add_argument("-q", "--quiet", action="store_true")
    train = sub.add_parser("train", help="T17: rules for independently generated fictional profiles only")
    training_input = train.add_mutually_exclusive_group()
    training_input.add_argument("--demo", action="store_true", help="Generate independent fictional profiles")
    training_input.add_argument("--input", type=Path, help="Previously generated demo, verified by generator replay")
    train.add_argument("--seed", type=int, default=17)
    train.add_argument("--profiles", type=int, default=120, help="Fictional profiles, 0–500")
    train.add_argument("--demo-dir", type=Path, default=Path("data/work/t17"))
    train.add_argument("--config", type=Path, default=Path("configs/model.yaml"))
    train.add_argument("--output", type=Path, help="New immutable output directory")
    train.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/t17"))
    train.add_argument("-q", "--quiet", action="store_true")
    graph = sub.add_parser("graph", help="T18: auditable coflight graph of independent fictional profiles")
    graph_input = graph.add_mutually_exclusive_group()
    graph_input.add_argument("--demo", action="store_true")
    graph_input.add_argument("--input", type=Path, help="Replay-verifiable T17 demo, or its snapshot/input directory")
    graph.add_argument("--seed", type=int, default=17)
    graph.add_argument("--profiles", type=int, default=120)
    graph.add_argument("--demo-dir", type=Path, default=Path("data/work/t18"))
    graph.add_argument("--config", type=Path, default=Path("configs/graph.yaml"))
    graph.add_argument("--output", type=Path)
    graph.add_argument("--artifacts-dir", type=Path, default=Path("artifacts/t18"))
    graph.add_argument("-q", "--quiet", action="store_true")
    for name in ["features", "evaluate"]:
        sub.add_parser(name, help="Not implemented by this CLI; see task-specific scripts")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "graph":
            if not args.demo and args.input is None:
                print("T18: укажите --demo или --input с независимым учебным набором T17", file=sys.stderr)
                return 2
            from .models.demo import create_demo
            from .features.graph_artifact import run
            directory = create_demo(args.demo_dir, args.seed, args.profiles) if args.demo else args.input
            _, summary = run(directory, args.config, args.output, args.artifacts_dir,
                             progress=None if args.quiet else lambda message: print(message, flush=True))
            if not args.quiet:
                print(summary, end="")
            return 0
        if args.command == "train":
            if not args.demo and args.input is None:
                print("T17: укажите --demo или --input с независимым учебным набором", file=sys.stderr)
                return 2
            from .models.demo import create_demo
            from .models.ranking import run
            directory = create_demo(args.demo_dir, args.seed, args.profiles) if args.demo else args.input
            _, summary = run(directory, args.config, args.output, args.artifacts_dir,
                             progress=None if args.quiet else lambda message: print(message, flush=True))
            if not args.quiet:
                print(summary, end="")
            return 0
        if args.command == "run":
            from .pipeline import run
            _, summary = run(config=args.config, data_dir=args.data_dir, work_dir=args.work_dir,
                             output=args.output, from_interim=args.from_interim, from_processed=args.from_processed,
                             artifacts_dir=args.artifacts_dir,
                             normalization=args.normalization_config, resolution=args.resolution_config,
                             reference=args.reference, overrides=args.overrides, reference_manifest=args.reference_manifest,
                             batch_size=args.batch_size, memory_limit=args.memory_limit, chunk_pages=args.chunk_pages,
                             sample_size=args.sample_size, require_all=args.require_all, allow_source_errors=args.allow_source_errors,
                             progress=None if args.quiet else lambda message: print(message, flush=True))
            if not args.quiet:
                print(summary, end="")
            return 0
        if args.command == "resolve":
            from .resolve.pipeline import run
            _, summary = run(args.input,args.output,args.config,args.review_output,args.batch_size,args.memory_limit,
                             args.require_all,progress=None if args.quiet else lambda message: print(message,flush=True))
            if not args.quiet:
                print(summary,end="")
            return 0
        if args.command == "resolution-review":
            from .resolve.review import audit_to_file
            report = audit_to_file(args.sample,args.input,args.report)
            if not args.quiet:
                print(f"T13 review: проверено {report['reviewed']} / {report['sampled']}; расхождений {len(report['disagreements'])}")
                print(f"Отчёт: {args.report}")
            return 0
        if args.command == "quality":
            from .quality import audit
            report = audit(args.input, args.output, args.sample_size, args.memory_limit)
            print(json.dumps(report, ensure_ascii=False))
            return 0 if report["status"] != "failed" else 1
        if args.command == "build":
            from .normalize.canonical import build
            report, summary = build(args.input, args.output, args.config, args.batch_size,
                                    args.memory_limit, args.require_all,
                                    progress=None if args.quiet else lambda message: print(message, flush=True))
            if not args.quiet:
                print(summary, end="")
            return 0 if report["status"] == "complete" else 1
        if args.command == "airports":
            from .normalize.geography import run
            _, summary = run(args.input, args.output, args.reference, args.overrides,
                             args.reference_manifest, args.schedules, args.batch_size,
                             progress=None if args.quiet else lambda message: print(message, flush=True))
            if not args.quiet:
                print(summary, end="")
            return 0
        if args.command == "inventory":
            root = Path(args.data_dir)
            if not root.is_dir():
                raise FileNotFoundError(root)
            from zipfile import ZipFile, is_zipfile

            for path in sorted(root.rglob("*")):
                if path.is_file():
                    entry = dict(
                        path=str(path.relative_to(root)),
                        bytes=path.stat().st_size,
                        sha256=fingerprint(path),
                    )
                    if is_zipfile(path):
                        with ZipFile(path) as z:
                            entry["zip_members"] = [i.filename for i in z.infolist()]
                    print(json.dumps(entry, ensure_ascii=False))
            return 0
        if args.command == "audit":
            from .readers.timetable_pdf import audit

            audit(args.rows, args.output)
            return 0
        if args.command == "export":
            export(args.path, args.output)
            return 0
        if args.command != "ingest":
            print(f"{args.command}: not implemented by this CLI", file=sys.stderr)
            return 2
        if args.source == "all" and args.path:
            raise ValueError("--path only applies to one source")
        config = load_sources(args.config) if args.path is None else {}
        chosen = READERS if args.source == "all" else [args.source]
        failed = False
        for name in chosen:
            try:
                cfg = config.get(name, {})
                path = args.path or resolve_path(cfg, args.data_dir)
                module = importlib.import_module("yoyoflot.readers." + name)
                if name == "timetable_pdf":
                    report = module.process(
                        path,
                        args.output / name,
                        chunk_pages=args.chunk_pages,
                        limit_pages=args.limit_pages,
                        ocr=args.ocr,
                    )
                else:
                    options = dict(batch_size=args.batch_size)
                    if name == "boarding_csv":
                        options["encoding"] = args.encoding or cfg.get(
                            "encoding", "utf-8-sig"
                        )
                    if name == "sirena_fixed":
                        options["columns"] = str(
                            args.columns
                            or cfg.get("columns", "configs/sirena_columns.yaml")
                        )
                    report = ingest(
                        module.read, path, name, args.output / name, **options
                    )
                print(json.dumps(report, ensure_ascii=False, default=str))
                failed = failed or report["status"] != "complete"
            except Exception as exc:
                print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
                failed = True
        return 1 if failed else 0
    except KeyboardInterrupt:
        if args.command == "run":
            print("T16 прерван. Блокировка освобождена; повторная команда проверит и использует завершённые этапы.", file=sys.stderr)
        else:
            print("Выполнение прервано. Блокировка освобождена; повторный запуск начнёт обработку заново.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def export(path, out):
    import csv
    import uuid

    import pyarrow.parquet as pq

    from .io import publish_directory

    out.parent.mkdir(parents=True, exist_ok=True)
    staging = out.with_name(out.name + "." + uuid.uuid4().hex + ".partial")
    staging.mkdir()
    pf = pq.ParquetFile(path)
    with (
        open(staging / "table.csv", "w", newline="", encoding="utf-8") as cf,
        open(staging / "table.jsonl", "w", encoding="utf-8") as jf,
    ):
        writer = csv.DictWriter(cf, fieldnames=pf.schema_arrow.names)
        writer.writeheader()
        for batch in pf.iter_batches(batch_size=1000):
            for row in batch.to_pylist():
                writer.writerow({k: "\\N" if v is None else v for k, v in row.items()})
                jf.write(json.dumps(row, ensure_ascii=False) + "\n")
    publish_directory(staging, out)
