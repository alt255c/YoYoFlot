"""Compatible import alias AND direct PyCharm Run entry point for the PDF reader."""

import argparse
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

from yoyoflot.readers import timetable_pdf as _implementation

# Настройки для запуска кнопкой Run (аргументы командной строки не обязательны).
PDF_PATH = None  # Например: Path(r"D:\dataset\Skyteam_Timetable.pdf")
OUTPUT_DIR = PROJECT_DIR / "data/interim/timetable_pdf"
CHUNK_PAGES = 100
USE_OCR = False  # True только при настроенном Tesseract/tessdata.
VERBOSE = False


def main(argv=None):
    parser = argparse.ArgumentParser(description="Разбор PDF-расписания")
    parser.add_argument("--path", type=Path, default=PDF_PATH)
    parser.add_argument("--output", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--chunk-pages", type=int, default=CHUNK_PAGES)
    parser.add_argument("--limit-pages", type=int)
    parser.add_argument("--ocr", action="store_true", default=USE_OCR)
    parser.add_argument("--verbose", action="store_true", default=VERBOSE)
    parser.add_argument(
        "-q", "--quiet", action="store_true",
        help="Не выводить прогресс и итог в консоль; итог сохраняется в summary.txt",
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    if args.path is None:
        candidates = [
            PROJECT_DIR / "data/Skyteam_Timetable.pdf",
            PROJECT_DIR / "data/raw/Skyteam_Timetable.pdf",
        ]
        args.path = next((p for p in candidates if p.is_file()), candidates[0])
    if not args.path.is_file():
        raise FileNotFoundError(
            f"Не найден PDF: {args.path}. Укажите PDF_PATH в начале этого файла."
        )
    report = _implementation.process(
        args.path,
        args.output,
        chunk_pages=args.chunk_pages,
        limit_pages=args.limit_pages,
        ocr=args.ocr,
        progress=args.verbose and not args.quiet,
        force=args.force,
    )
    if not args.quiet:
        print((args.output / "summary.txt").read_text(encoding="utf-8"), end="", flush=True)
    return (
        0 if report.get("status") == "complete" else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
else:
    # Preserve monkeypatching and legacy imports used by existing PDF tests.
    sys.modules[__name__] = _implementation
