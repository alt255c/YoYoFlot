"""Запускать кнопкой Run в PyCharm из корня проекта YoYoFlot.

Без аргументов командной строки. Не объединяет пассажиров. Не заменяет ручную
проверку 50 строк PDF. Первое выполнение больших файлов может занять много времени.
"""

from __future__ import annotations

import csv
import hashlib
import importlib
import itertools
import json
import os
import runpy
import random
import subprocess
import sys
import traceback
import uuid
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
from pathlib import Path
from zipfile import ZipFile


PROJECT_DIR = Path(__file__).resolve().parent
# НАСТРОЙКИ. None: поиск в data/raw, data и подпапках data по BoardingData.csv.
DATA_DIR = None  # Например: Path(r"C:\Users\reape\PycharmProjects\YoYoFlot\data")
OUTPUT_DIR = PROJECT_DIR / "data" / "interim" / "real"
RERUN_ALL = False  # True: заново обработать источники, даже если результат актуален.
CHECK_PDF = True  # Отсутствующий PDF будет явно отмечен как непроверенный.
PDF_PATH = None  # Например: Path(r"D:\dataset\Skyteam_Timetable.pdf")
BATCH_SIZE = 3000
CHUNK_PAGES = 100
PDF_OCR = False  # True при настроенном Tesseract/tessdata; смена режима сбрасывает PDF checkpoint.
VERBOSE = False  # Подробности всегда в verification.log; в консоли только итог.

FILES = {
    "boarding_csv": "BoardingData.csv",
    "forum_json": "FrequentFlyerForum-Profiles.json",
    "pointz_xml": "PointzAggregator-AirlinesData.xml",
    "sirena_fixed": "Sirena-export-fixed.tab",
    "skyteam_yaml": "SkyTeam-Exchange.yaml",
    "boardingpass_xlsx": "YourBoardingPassDotAero.zip",
}
TASKS = dict(zip(FILES, ["T05", "T06", "T07", "T08", "T09", "T10"]))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class Tee:
    def __init__(self, console, log):
        self.console, self.log = console, log

    def write(self, text):
        self.log.write(text)
        self.log.flush()
        try:
            self.console.write(text)
        except UnicodeEncodeError:
            encoding = getattr(self.console, "encoding", None) or "utf-8"
            self.console.write(text.encode(encoding, errors="replace").decode(encoding))
        self.console.flush()
        return len(text)

    def flush(self):
        self.log.flush()
        self.console.flush()


def choose_data_dir():
    if DATA_DIR is not None:
        path = Path(DATA_DIR).expanduser().resolve()
        require(path.is_dir(), f"Не найден DATA_DIR: {path}")
        return path
    candidates = [PROJECT_DIR / "data/raw", PROJECT_DIR / "data"]
    data = PROJECT_DIR / "data"
    if data.is_dir():
        candidates.extend(
            p.parent
            for p in data.rglob("BoardingData.csv")
            if "interim" not in p.relative_to(data).parts
        )
    candidates.append(PROJECT_DIR)
    candidates = list(dict.fromkeys(p.resolve() for p in candidates if p.is_dir()))

    def score(path):
        return sum((path / name).is_file() for name in FILES.values()) + int(
            (path / "FrequentFlyerForum-Profiles.zip").is_file()
            and not (path / FILES["forum_json"]).exists()
        )

    require(
        candidates and max(map(score, candidates)) > 0,
        "Не найдены исходники. Укажите DATA_DIR в начале check_all_data.py.",
    )
    return max(candidates, key=score)


def unpack_json_if_needed(raw):
    destination = raw / FILES["forum_json"]
    archive = raw / "FrequentFlyerForum-Profiles.zip"
    if destination.exists() or not archive.exists():
        return
    print("Распаковка JSON из FrequentFlyerForum-Profiles.zip...", flush=True)
    temporary = destination.with_suffix(".json.unpacking")
    try:
        with ZipFile(archive) as z:
            members = [
                m
                for m in z.infolist()
                if not m.is_dir() and Path(m.filename).name == destination.name
            ]
            require(
                len(members) == 1, "В ZIP должен быть ровно один файл профилей JSON."
            )
            with z.open(members[0]) as source, temporary.open("wb") as out:
                for chunk in iter(lambda: source.read(4 * 1024 * 1024), b""):
                    out.write(chunk)
        temporary.replace(destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def run_script(name, arguments):
    original = sys.argv
    try:
        sys.argv = [name, *map(str, arguments)]
        runpy.run_path(str(PROJECT_DIR / "scripts" / name), run_name="__main__")
    finally:
        sys.argv = original


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(path)


def run_child(operation, **parameters):
    """Native failures are confined to a worker; the supervisor keeps running."""
    folder = PROJECT_DIR / "data/verification/workers"
    folder.mkdir(parents=True, exist_ok=True)
    token = operation + "_" + uuid.uuid4().hex
    request, response = (
        folder / (token + ".request.json"),
        folder / (token + ".result.json"),
    )
    write_json(request, {"operation": operation, **parameters})
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONFAULTHANDLER": "1"}
    with subprocess.Popen(
        [
            sys.executable,
            "-X",
            "faulthandler",
            str(Path(__file__).resolve()),
            "--worker",
            str(request),
            str(response),
        ],
        cwd=PROJECT_DIR,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as process:
        for line in process.stdout:
            print(line, end="", flush=True)
        code = process.wait()
    payload = read_json(response) if response.exists() else {}
    if code != 0:
        detail = payload.get(
            "error",
            "Процесс завершился без Python-отчёта; возможна ошибка нативной библиотеки.",
        )
        raise RuntimeError(
            f"Worker {operation}: exit={code}, 0x{code & 0xFFFFFFFF:08X}. {detail}"
        )
    require(payload.get("ok") is True, f"Worker {operation} не создал успешный отчёт.")
    return payload.get("result")


def worker(request_path, response_path):
    """Internal entry point; launched automatically, no console commands needed."""
    sys.path.insert(0, str(PROJECT_DIR / "src"))
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    os.chdir(PROJECT_DIR)
    try:
        request = read_json(request_path)
        operation = request["operation"]
        if operation == "ingest":
            reader = importlib.import_module("yoyoflot.readers." + request["source"])
            path, output = Path(request["input"]), Path(request["output"])
            if request["source"] == "timetable_pdf":
                result = reader.process(
                    path,
                    output,
                    chunk_pages=request["chunk_pages"],
                    progress=False,
                    ocr=request.get("ocr", False),
                )
            else:
                from yoyoflot.io import ingest

                result = ingest(
                    reader.read, path, request["source"], output, **request["options"]
                )
        elif operation == "validate":
            from validate_interim import validate

            result = validate(Path(request["output"]))
        elif operation == "script":
            run_script(request["name"], request["arguments"])
            result = {"completed": request["name"]}
        elif operation == "pdf":
            output, path = Path(request["output"]), Path(request["input"])
            result = check_pdf(output, path, read_json(output / "source_report.json"))
            if not (output / "audit_sheet.csv").exists():
                from yoyoflot.readers.timetable_pdf import audit

                audit(n=50, out=output)
        else:
            raise ValueError(f"Unknown worker operation: {operation}")
        write_json(response_path, {"ok": True, "result": result})
        return 0
    except Exception as exc:
        traceback.print_exc()
        write_json(
            response_path, {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        )
        return 1


def check_balance(source, counts, census):
    c = census[source]
    expected = {
        "boarding_csv": {
            "person_observations": c.get("valid_width"),
            "travel_observations": c.get("valid_width"),
        },
        "forum_json": {
            "person_observations": c.get("profiles"),
            "identifiers": c.get("cards"),
            "travel_observations": c.get("flights"),
        },
        "pointz_xml": {
            "person_observations": c.get("user"),
            "identifiers": c.get("card"),
            "travel_observations": c.get("activity"),
        },
        "sirena_fixed": {
            "person_observations": c.get("rows"),
            "travel_observations": c.get("rows"),
        },
        "skyteam_yaml": {
            "person_observations": c.get("cards"),
            "identifiers": c.get("cards"),
            "travel_observations": c.get("cards"),
        },
        "boardingpass_xlsx": {
            "person_observations": c.get("sheets"),
            "travel_observations": c.get("sheets"),
        },
    }[source]
    # Этот независимый census рассчитан на структуру выданного датасета.
    if source == "forum_json":
        require(
            c.get("empty_document_containers", 0) == c.get("document_containers", 0),
            "В JSON появились непустые документы: нужен расширенный независимый подсчёт.",
        )
    for table, value in expected.items():
        require(value is not None, f"В census нет счётчика {source}/{table}.")
        require(
            counts[table] == value,
            f"Баланс {source}/{table}: источник={value}, результат={counts[table]}.",
        )
    return expected


def check_workbook_coverage(output, inventory):
    with Path(inventory).open(encoding="utf-8", newline="") as stream:
        books = {row["workbook"]: row for row in csv.DictReader(stream)}
    seen = set()
    current, sheets = None, []
    rows = 0

    def finish(book, names):
        if book is None:
            return
        require(book in books and book not in seen, f"Лишняя/повторная книга: {book}")
        require(len(names) == len(set(names)), f"Повтор листа: {book}")
        require(len(names) == int(books[book]["sheets"]), f"Не все листы книги: {book}")
        digest = hashlib.sha256(json.dumps(names).encode("utf-8")).hexdigest()
        require(
            digest == books[book]["sheet_names_sha256"],
            f"Имена/порядок листов изменены: {book}",
        )
        seen.add(book)

    with (Path(output) / "workbook_coverage.csv").open(
        encoding="utf-8", newline=""
    ) as stream:
        for row in csv.DictReader(stream):
            require(row["status"] == "ok", f"Исключён лист: {row}")
            require(
                row["header_version"] == "boarding_card_v1", "Неожиданный шаблон XLSX."
            )
            require(
                int(row["input_rows"]) == int(row["accepted_rows"]) == 1,
                f"Не принята посадочная карточка: {row}",
            )
            require(int(row["errors"]) == 0, f"Ошибка листа: {row}")
            if row["workbook"] != current:
                finish(current, sheets)
                current, sheets = row["workbook"], []
            sheets.append(row["sheet"])
            rows += 1
    finish(current, sheets)
    require(seen == set(books), "Не все книги попали в workbook_coverage.csv.")
    return {"workbooks": len(seen), "sheets": rows, "names_and_counts": "passed"}


def check_pdf(output, path, report):
    import pymupdf
    import pyarrow.parquet as pq

    with pymupdf.open(path) as pdf:
        total = len(pdf)
    require(
        report["pages"] == report["total_pages"] == total,
        f"PDF обработан частично: {report.get('pages')} из {total} страниц.",
    )
    found, parsed, excluded, types, reasons = 0, 0, [], Counter(), Counter()
    with (output / "page_coverage.csv").open(encoding="utf-8", newline="") as stream:
        number = 0
        for number, row in enumerate(csv.DictReader(stream), 1):
            require(int(row["page"]) == number, "Пропуск/повтор страницы PDF.")
            found += int(row["rows_found"])
            parsed += int(row["rows_parsed"])
            types[row["type"]] += 1
            if row["status"] != "ok":
                excluded.append(number)
                reasons[row.get("reason") or row["type"]] += 1
        require(number == total, "page_coverage.csv не охватывает весь PDF.")
    # Сохраняется полный список проблем, включая старые 615 unknown страниц.
    (output / "verification_pdf_pages.json").write_text(
        json.dumps(
            {
                "pages": total,
                "found": found,
                "parsed": parsed,
                "excluded_pages": excluded,
                "types": dict(types),
                "reasons": dict(reasons),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    def parquet_rows():
        for batch in pq.ParquetFile(output / "schedules.parquet").iter_batches(
            batch_size=2000
        ):
            yield from batch.to_pylist()

    marker = object()
    make_audit = not (output / "audit_sheet.csv").exists()
    sample, rng = [], random.Random(42)
    with (
        (output / "schedules.csv").open(encoding="utf-8", newline="") as cf,
        (output / "schedules.jsonl").open(encoding="utf-8") as jf,
    ):
        csv_rows = csv.DictReader(cf)
        require(
            csv_rows.fieldnames == pq.read_schema(output / "schedules.parquet").names,
            "Заголовок CSV отличается от схемы Parquet.",
        )
        count = 0
        for count, triple in enumerate(
            itertools.zip_longest(
                parquet_rows(),
                csv_rows,
                (json.loads(line) for line in jf),
                fillvalue=marker,
            ),
            1,
        ):
            require(
                all(row is not marker for row in triple),
                "Количество строк CSV/JSONL/Parquet различается.",
            )
            arrow, csv_row, json_row = triple
            require(arrow == json_row, f"JSONL отличается от Parquet, строка {count}.")
            expected = {
                key: "\\N" if value is None else str(value)
                for key, value in arrow.items()
            }
            require(csv_row == expected, f"CSV отличается от Parquet, строка {count}.")
            if make_audit:
                if len(sample) < 50:
                    sample.append(csv_row)
                else:
                    index = rng.randrange(count)
                    if index < 50:
                        sample[index] = csv_row
        require(
            count == report["rows"], "Неверное количество экспортированных строк PDF."
        )
    if make_audit:
        with (output / "audit_sheet.csv").open(
            "w", encoding="utf-8", newline=""
        ) as stream:
            writer = csv.DictWriter(
                stream,
                fieldnames=pq.read_schema(output / "schedules.parquet").names
                + ["manual_match", "reviewer", "notes"],
            )
            writer.writeheader()
            writer.writerows(
                sorted(
                    sample, key=lambda row: (int(row["source_page"]), row["locator"])
                )
            )
    baseline = report.get("baseline_comparison") or {}
    require(
        baseline.get("missing_legacy_rows", 0) == 0,
        "В сравнении с первым парсером потеряны строки PDF.",
    )
    needs_review = (
        bool(excluded)
        or found != parsed
        or parsed != report["rows_ok"]
        or report["rows_ok"] != report["rows"]
        or baseline.get("status") in {"differences", "comparison_failed"}
    )
    return {
        "pages": total,
        "rows": count,
        "exports": "equivalent",
        "coverage_status": "needs_review" if needs_review else "passed",
        "pages_requiring_review": len(excluded),
        "reasons": dict(reasons),
        "rows_found": found,
        "rows_parsed": parsed,
        "cover_period": report.get("cover_period"),
    }


def main():
    require(
        __debug__,
        "Запускайте без опции Python -O: структурный валидатор использует assert.",
    )
    require(
        (PROJECT_DIR / "src/yoyoflot").is_dir(),
        "Поместите check_all_data.py в корень YoYoFlot, рядом с pyproject.toml.",
    )
    sys.path.insert(0, str(PROJECT_DIR / "src"))
    sys.path.insert(0, str(PROJECT_DIR / "scripts"))
    previous_cwd = Path.cwd()
    os.chdir(PROJECT_DIR)
    destination = PROJECT_DIR / "data/verification"
    destination.mkdir(parents=True, exist_ok=True)
    summary = {
        "started": datetime.now().isoformat(),
        "python": sys.version,
        "checks": {},
        "sources": {},
        "manual_pdf_review": "required",
    }
    console, original_error = sys.stdout, sys.stderr
    with (destination / "verification.log").open("w", encoding="utf-8") as log:
        with (
            redirect_stdout(Tee(console, log) if VERBOSE else log),
            redirect_stderr(Tee(original_error, log) if VERBOSE else log),
        ):
            try:
                from yoyoflot.contracts import SCHEMAS, TableBatch

                for name, schema in SCHEMAS.items():
                    require(
                        TableBatch(name, []).to_arrow().schema == schema,
                        f"Контракт {name}.",
                    )
                summary["checks"]["T04_contract"] = {"status": "passed"}
                raw = choose_data_dir()
                print(f"Исходники: {raw}\nРезультаты: {OUTPUT_DIR}", flush=True)
                unpack_json_if_needed(raw)
                jobs = {source: raw / name for source, name in FILES.items()}
                if CHECK_PDF:
                    candidates = (
                        [Path(PDF_PATH)]
                        if PDF_PATH is not None
                        else [
                            raw / "Skyteam_Timetable.pdf",
                            PROJECT_DIR / "data/Skyteam_Timetable.pdf",
                            PROJECT_DIR / "data/raw/Skyteam_Timetable.pdf",
                        ]
                    )
                    jobs["timetable_pdf"] = next(
                        (p for p in candidates if p.is_file()), candidates[0]
                    )
                for source, path in jobs.items():
                    result = summary["sources"][source] = {
                        "task": TASKS.get(source, "T03"),
                        "input": str(path),
                    }
                    print(f"\n[{result['task']}] {source}", flush=True)
                    try:
                        require(path.is_file(), f"Нет исходного файла: {path}")
                        digest = sha256(path)
                        output = OUTPUT_DIR / source
                        manifest = output / "source_report.json"
                        old = read_json(manifest) if manifest.exists() else {}
                        reusable = old.get("sha256") == digest and all(
                            (output / (name + ".parquet")).exists() for name in SCHEMAS
                        )
                        options = {"batch_size": BATCH_SIZE}
                        if source == "sirena_fixed":
                            columns = PROJECT_DIR / "configs/sirena_columns.yaml"
                            options["columns"] = str(columns)
                            reusable = reusable and old.get(
                                "configuration_sha256"
                            ) == sha256(columns)
                        if source == "boarding_csv":
                            options["encoding"] = "utf-8-sig"
                        if RERUN_ALL or not reusable:
                            print(
                                "Полная обработка. Дождитесь окончания...", flush=True
                            )
                            run_child(
                                "ingest",
                                source=source,
                                input=str(path),
                                output=str(output),
                                options=options,
                                chunk_pages=CHUNK_PAGES,
                                ocr=PDF_OCR,
                            )
                        else:
                            print(
                                "SHA-256 совпадает: проверка существующего результата.",
                                flush=True,
                            )
                        report = read_json(manifest)
                        require(
                            report["sha256"] == sha256(path),
                            "Вход изменился во время проверки.",
                        )
                        result.update(
                            sha256=report["sha256"],
                            source_status=report["status"],
                            counts=report["counts"],
                            input_counts=report.get("input_counts"),
                        )
                        print(
                            "Проверка всех таблиц, ключей и provenance...", flush=True
                        )
                        result["structural"] = run_child("validate", output=str(output))
                        if source == "timetable_pdf":
                            result["pdf"] = run_child(
                                "pdf", output=str(output), input=str(path)
                            )
                            print(
                                "Для T03 остаётся ручная сверка audit_sheet.csv с PDF."
                            )
                        if (
                            source == "timetable_pdf"
                            and result["pdf"]["coverage_status"] == "needs_review"
                        ):
                            result["status"] = "review_required"
                        else:
                            require(
                                report["status"] == "complete",
                                f"Статус источника: {report['status']}",
                            )
                            result["status"] = "passed"
                        print("ТРЕБУЕТСЯ ПРОВЕРКА СТРАНИЦ" if result["status"] == "review_required" else "ПРОЙДЕНО", flush=True)
                    except Exception as exc:
                        result.update(
                            status="failed", error=f"{type(exc).__name__}: {exc}"
                        )
                        print("НЕ ПРОЙДЕНО:", result["error"], flush=True)
                        traceback.print_exc()
                    write_json(destination / "verification_report.json", summary)
                if not CHECK_PDF:
                    summary["sources"]["timetable_pdf"] = {
                        "task": "T03",
                        "status": "not_checked",
                        "error": "CHECK_PDF=False",
                    }
                for name in ["independent_balance", "source_value_audit"]:
                    summary["checks"][name] = {"status": "blocked"}
                if all(summary["sources"][s]["status"] == "passed" for s in FILES):
                    try:
                        print("\nНезависимый пересчёт исходников...", flush=True)
                        run_child(
                            "script",
                            name="inspect_real_sources.py",
                            arguments=["--data-dir", str(raw)],
                        )
                        run_child(
                            "script",
                            name="extend_real_census.py",
                            arguments=["--data-dir", str(raw)],
                        )
                        census = read_json(
                            PROJECT_DIR / "docs/validation/input_census.json"
                        )
                        balances = {}
                        reference_path = PROJECT_DIR / "docs/validation/real_runs.json"
                        reference = (
                            read_json(reference_path).get("reports", {})
                            if reference_path.exists()
                            else {}
                        )
                        reference_checks = {}
                        for source in FILES:
                            result = summary["sources"][source]
                            require(
                                census["files"][FILES[source]]["sha256"]
                                == result["sha256"],
                                "SHA-256 census не совпадает.",
                            )
                            balances[source] = check_balance(
                                source, result["counts"], census
                            )
                            baseline = reference.get(source, {})
                            if baseline.get("sha256") == result["sha256"]:
                                require(
                                    result["counts"] == baseline["counts"],
                                    f"Количество таблиц {source} отличается от проверенного эталона того же файла.",
                                )
                                reference_checks[source] = "passed"
                            else:
                                reference_checks[source] = (
                                    "not_applicable: another input version or no reference"
                                )
                        coverage = check_workbook_coverage(
                            OUTPUT_DIR / "boardingpass_xlsx",
                            PROJECT_DIR / "docs/validation/workbook_inventory.csv",
                        )
                        summary["checks"]["independent_balance"] = {
                            "status": "passed",
                            "balances": balances,
                            "xlsx": coverage,
                            "reference_counts_for_same_sha256": reference_checks,
                        }
                    except Exception as exc:
                        summary["checks"]["independent_balance"] = {
                            "status": "failed",
                            "error": str(exc),
                        }
                        traceback.print_exc()
                    if summary["checks"]["independent_balance"]["status"] == "passed":
                        try:
                            print("\nСверка выбранных исходных значений...", flush=True)
                            run_child(
                                "script",
                                name="audit_real_sources.py",
                                arguments=[
                                    "--data-dir",
                                    str(raw),
                                    "--interim",
                                    str(OUTPUT_DIR),
                                ],
                            )
                            audit_result = read_json(
                                PROJECT_DIR / "docs/validation/source_audit_checks.json"
                            )
                            require(
                                audit_result["failed"] == 0
                                and audit_result["count"] > 0,
                                "Расхождения исходных значений.",
                            )
                            summary["checks"]["source_value_audit"] = {
                                "status": "passed",
                                "comparisons": audit_result["count"],
                            }
                        except Exception as exc:
                            summary["checks"]["source_value_audit"] = {
                                "status": "failed",
                                "error": str(exc),
                            }
                            traceback.print_exc()
            except Exception as exc:
                summary["fatal_error"] = f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            finally:
                automatic_ok = (
                    bool(summary["sources"])
                    and not summary.get("fatal_error")
                    and all(
                        v.get("status") == "passed"
                        for v in [
                            *summary["sources"].values(),
                            *summary["checks"].values(),
                        ]
                    )
                )
                summary.update(
                    finished=datetime.now().isoformat(),
                    automatic_checks_passed=automatic_ok,
                    verdict="AUTOMATIC_CHECKS_PASSED_MANUAL_PDF_REVIEW_REQUIRED"
                    if automatic_ok
                    else "FAILED_OR_NOT_FULLY_CHECKED",
                )
                data_checked = (
                    bool(summary["sources"])
                    and not summary.get("fatal_error")
                    and all(
                        v.get("status") in {"passed", "review_required"}
                        for v in summary["sources"].values()
                    )
                    and all(
                        v.get("status") == "passed" for v in summary["checks"].values()
                    )
                )
                summary["data_integrity_checks_passed"] = data_checked
                if data_checked and not automatic_ok:
                    summary["verdict"] = "DATA_CHECKS_PASSED_REVIEW_REQUIRED"
                (destination / "verification_report.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                lines = [f"Итог: {summary['verdict']}"]
                pdf_result = summary["sources"].get("timetable_pdf", {}).get("pdf", {})
                if pdf_result:
                    lines.append(
                        f"PDF: целостность выгрузок — {pdf_result.get('exports')}; страниц для проверки — {pdf_result.get('pages_requiring_review', 0)}"
                    )
                    if pdf_result.get("reasons"):
                        lines.append(
                            "Причины: "
                            + "; ".join(
                                f"{reason}: {count}"
                                for reason, count in pdf_result["reasons"].items()
                            )
                        )
                lines += [
                    f"{v.get('task', name)} {name}: {v.get('status')} {v.get('error', '')}"
                    for name, v in summary["sources"].items()
                ]
                lines += [
                    f"{name}: {v.get('status')} {v.get('error', '')}"
                    for name, v in summary["checks"].items()
                ]
                lines += [
                    "Автоматическая сверка не заменяет ручные 50 строк PDF и подпись проверяющего.",
                    "Период расписания нужно сопоставить с датами пассажирских наблюдений.",
                ]
                lines.append(f"Отчёты сохранены: {destination}")
                (destination / "verification_summary.txt").write_text(
                    "\n".join(lines), encoding="utf-8"
                )
                console.write("\n".join(lines) + "\n")
                console.flush()
                os.chdir(previous_cwd)
    return 0 if summary.get("data_integrity_checks_passed") else 1


if __name__ == "__main__":
    try:
        if len(sys.argv) == 4 and sys.argv[1] == "--worker":
            raise SystemExit(worker(sys.argv[2], sys.argv[3]))
        raise SystemExit(main())
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
