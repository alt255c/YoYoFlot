import argparse
import importlib
import json
import sys
from pathlib import Path

from .config import load_sources, resolve_path
from .io import ingest
from .provenance import fingerprint

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
    p = argparse.ArgumentParser(prog="yoyoflot", description="YoYoFlot readers T03–T10")
    p.add_argument("--version", action="version", version="yoyoflot 0.3.5")
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
    for name in ["build", "features", "train", "evaluate", "run"]:
        sub.add_parser(name, help="Not implemented: tasks T11 onward")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    try:
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
            print(f"{args.command}: not implemented (outside T03–T10)", file=sys.stderr)
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
