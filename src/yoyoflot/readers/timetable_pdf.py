"""
T03: Skyteam_Timetable.pdf -> schedules.csv/jsonl/parquet + page_coverage + errors + report.
Два подтверждённых класса таблиц: слитная строка и ячейки с новой строки.
Порции с чекпоинтом; carry-over секций FROM/TO между страницами и чанками.
"""

from __future__ import annotations

import csv
import json
import re
import time
from bisect import bisect_right
from functools import lru_cache
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

import pymupdf

PROJECT_ROOT = Path(__file__).resolve().parents[3]
PDF_PATH = PROJECT_ROOT / "data" / "Skyteam_Timetable.pdf"
OUT_DIR = PROJECT_ROOT / "data" / "interim" / "timetable_pdf"
CHUNK_PAGES = 2000

MONTHS = {
    "Jan": 1,
    "Feb": 2,
    "Mar": 3,
    "Apr": 4,
    "May": 5,
    "Jun": 6,
    "Jul": 7,
    "Aug": 8,
    "Sep": 9,
    "Oct": 10,
    "Nov": 11,
    "Dec": 12,
}

WS_RE = re.compile(r"\s+")
COVER_RE = re.compile(
    r"Covers period:\s*(\d{2}) ([A-Z][a-z]{2}) (\d{4})\s*through\s*(\d{2}) ([A-Z][a-z]{2}) (\d{4})"
)
SECTION_RE = re.compile(
    r"FROM:\s*(?P<ocity>[A-Za-z /().'-]+?)"
    r"(?:\s*,\s*(?P<ocountry>[A-Za-z ,/().'-]*?))?\s*(?P<oiata>[A-Z]{3})\s*"
    r"TO:\s*(?P<dcity>[A-Za-z /().'-]+?)"
    r"(?:\s*,\s*(?P<dcountry>[A-Za-z ,/().'-]*?))?\s*(?P<diata>[A-Z]{3})"
    r"(?=\s*(?:Validity\b|\d{2} [A-Z][a-z]{2}\s*-|Consult your travel agent|FROM:|$))"
)
ROW_RE = re.compile(
    r"(?P<valid>\d{2} [A-Z][a-z]{2}\s*-\s*\d{2} [A-Z][a-z]{2})\s*"
    r"(?P<days>[1-7](?: ?[1-7]){0,6})(?=\s?\d{2}:\d{2})\s*"
    r"(?P<dep>\d{2}:\d{2})\s*"
    r"(?P<arr>\d{2}:\d{2})(?P<offset>\+-?\d)?\s*"
    r"(?P<flight>[A-Z]{2} ?\d{1,4})(?P<star>\*?)\s*"
    r"(?P<aircraft>[A-Z0-9]{3})\s*"
    r"(?P<travel>\d+ ?H ?\d+ ?M)"
)
OPERATED_RE = re.compile(
    r"Operated by:\s*(?P<op>[A-Za-z .'/()!&-]+?)"
    r"(?=\s*(?:FROM:|TO:|Operated by:|\d{2} [A-Z][a-z]{2}\s*-|\d{2}:\d{2})|\s*$)"
)
OPERATOR_NOTES_RE = re.compile(r"(?:\s*Operated by:\s*[A-Za-z .'/()!&-]+)+\s*")

OK_TYPES = {
    "cover",
    "table",
    "continuation",
    "consult",
    "header_only",
    "blank",
    "notes",
}

FIELDS = [
    "schedule_id",
    "flight_number",
    "origin",
    "destination",
    "departure_local",
    "arrival_local",
    "arrival_day_offset",
    "days_of_week",
    "valid_from",
    "valid_to",
    "source_page",
    "parse_status",
    "section_source",
    "origin_city",
    "origin_country",
    "dest_city",
    "dest_country",
    "validity_raw",
    "days_raw",
    "aircraft",
    "travel_time",
    "operated_by",
    "codeshare_mark",
    "raw_line",
]


def normalize_ws(text: str) -> str:
    """Любые пробельные символы (включая переносы и nbsp) -> один пробел."""
    return WS_RE.sub(
        " ", text.replace("\xa0", " ").replace("\u2007", " ").replace("\u202f", " ")
    )


def iso_date(day: str, mon: str, cover):
    """Derive a year only when exactly one date fits the explicit cover period."""
    if not cover:
        return None
    return _cached_iso_date(day, mon, tuple(map(tuple, cover)))


@lru_cache(maxsize=4096)
def _cached_iso_date(day, mon, cover):
    (sd, sm, sy), (ed, em, ey) = cover
    d, m = int(day), MONTHS[mon]
    from datetime import date

    lower, upper = date(sy, sm, sd), date(ey, em, ed)
    candidates = []
    for year in range(sy, ey + 1):
        try:
            candidate = date(year, m, d)
        except ValueError:
            continue
        if lower <= candidate <= upper:
            candidates.append(candidate.isoformat())
    return candidates[0] if len(candidates) == 1 else None


def parse_cover(text: str):
    m = COVER_RE.search(text)
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = m.groups()
    return (int(d1), MONTHS[m1], int(y1)), (int(d2), MONTHS[m2], int(y2))


def normalize_with_offsets(original):
    """Record only collapsed whitespace offsets; never compile a regex per row."""
    anchors, shifts = [], []
    removed = 0

    def collapse(match):
        nonlocal removed
        size = match.end() - match.start()
        if size > 1:
            anchor = match.start() - removed + 1
            removed += size - 1
            anchors.append(anchor)
            shifts.append(removed)
        return " "

    return WS_RE.sub(collapse, original), anchors, shifts


def parse_page(
    text: str, page_no: int, cover, carried: dict | None = None, keep_raw=False
):
    original = text
    if keep_raw:
        text, anchors, shifts = normalize_with_offsets(original)
    else:
        text = normalize_ws(text)
    rows, errors = [], []
    sections = list(SECTION_RE.finditer(text))
    secs = [
        (m.start(), {**m.groupdict(), "page": page_no})
        for m in sections
    ]
    # An unsupported explicit header is a boundary, never permission to reuse
    # the previous route. Otherwise a header typo silently changes row routes.
    for header in re.finditer(r"FROM:", text):
        if not any(m.start() == header.start() for m in sections):
            errors.append(
                dict(page=page_no, reason="unparsed_section", sample=text[header.start():header.start() + 200])
            )
            secs.append((header.start(), None))
    secs.sort(key=lambda item: item[0])
    matches = list(ROW_RE.finditer(text))

    for i, rm in enumerate(matches):
        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        nxt = min(nxt, next((p for p, _ in secs if p > rm.start()), len(text)))
        operated = OPERATED_RE.search(text, rm.end(), nxt)
        on_page = next(((p, d) for p, d in reversed(secs) if p < rm.start()), None)
        sec = on_page[1] if on_page is not None else carried
        if sec is None:
            src = None
            errors.append(
                {
                    "page": page_no,
                    "reason": "row_without_section",
                    "sample": rm.group(0)[:120],
                }
            )
        elif on_page is not None:
            src = "page"
        else:
            src = f"carried:{sec['page']}"
        parse_status = "ok" if sec else "row_without_section"
        if sec and src.startswith("carried") and sec.get("_unverified_since"):
            parse_status = "unverified_carry_after_unreadable_page"
            errors.append(dict(page=page_no, reason=parse_status, sample=rm.group(0)[:200]))
        vd, vto = re.split(r"\s*-\s*", rm["valid"])
        fd, fmon = vd.split()[0], vd.split()[1]
        td, tmon = vto.split()[0], vto.split()[1]
        rows.append(
            {
                "schedule_id": f"TT-{page_no:05d}-{i:04d}",
                "flight_number": rm["flight"].replace(" ", ""),
                "origin": sec["oiata"] if sec else None,
                "destination": sec["diata"] if sec else None,
                "departure_local": rm["dep"],
                "arrival_local": rm["arr"],
                "arrival_day_offset": int(rm["offset"][1:]) if rm["offset"] else None,
                "days_of_week": rm["days"].replace(" ", ""),
                "valid_from": iso_date(fd, fmon, cover),
                "valid_to": iso_date(td, tmon, cover),
                "source_page": page_no,
                "parse_status": parse_status,
                "section_source": src,
                "origin_city": sec["ocity"].strip() if sec else None,
                "origin_country": (sec["ocountry"] or "").strip() or None if sec else None,
                "dest_city": sec["dcity"].strip() if sec else None,
                "dest_country": (sec["dcountry"] or "").strip() or None if sec else None,
                "validity_raw": rm["valid"],
                "days_raw": rm["days"],
                "aircraft": rm["aircraft"],
                "travel_time": rm["travel"],
                "operated_by": operated["op"].strip() if operated else None,
                "codeshare_mark": bool(rm["star"]),
                "raw_line": original[
                    rm.start()
                    + (
                        shifts[bisect_right(anchors, rm.start()) - 1]
                        if bisect_right(anchors, rm.start())
                        else 0
                    ) : rm.end()
                    + (
                        shifts[bisect_right(anchors, rm.end() - 1) - 1]
                        if bisect_right(anchors, rm.end() - 1)
                        else 0
                    )
                ]
                if keep_raw
                else rm.group(0),
            }
        )

    carry_out = secs[-1][1] if secs else carried
    carry_used = sum(1 for r in rows if str(r["section_source"]).startswith("carried"))

    if COVER_RE.search(text):
        ptype = "cover"
    elif secs and matches:
        ptype = "table"
    elif matches:
        ptype = "continuation" if carried else "rows_without_header"
    elif secs:
        ptype = "consult" if "Consult your travel agent" in text else "header_only"
    elif "Contact To book" in text or "How to use" in text or "Aircraft Types" in text:
        ptype = "notes"
    elif OPERATOR_NOTES_RE.fullmatch(text):
        # These are real spillover notes; the complete page remains in page_text.zip.
        ptype = "notes"
    elif not text.strip():
        ptype = "blank"
    else:
        ptype = "unknown"
    if ptype in ("unknown", "rows_without_header"):
        errors.append({"page": page_no, "reason": ptype, "sample": text[:200]})

    candidates = list(
        re.finditer(r"\d{2} [A-Z][a-z]{2}\s*-\s*\d{2} [A-Z][a-z]{2}", text)
    )
    for candidate in candidates:
        if not any(m.start() <= candidate.start() < m.end() for m in matches):
            errors.append(
                {
                    "page": page_no,
                    "reason": "unparsed_schedule_candidate",
                    "sample": text[candidate.start() : candidate.start() + 200],
                }
            )
    coverage = {
        "page": page_no,
        "type": ptype,
        "sections": len(sections),
        "rows_found": max(len(matches), len(candidates)),
        "rows_parsed": sum(1 for r in rows if r["parse_status"] == "ok"),
        "carry_used": carry_used,
        "status": "ok" if ptype in OK_TYPES and not errors else "excluded",
        "reason": ";".join(sorted({e["reason"] for e in errors}))
        if errors
        else ""
        if ptype in OK_TYPES
        else ptype,
    }

    return rows, coverage, errors, carry_out


class PageExtractionError(ValueError):
    """A known per-page limitation that must be reported, not stop the full PDF."""

    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


def parse_pdf_page(page, text, page_no, cover, carried=None):
    """Keep the two physical timetable columns' route continuations separate.

    The source's left and right columns contain opposite directions. A single
    last-header carry assigns the left continuation to the right-hand route.
    The archived text and left-to-right row order stay unchanged.
    """
    midpoint = (page.rect.x0 + page.rect.x1) / 2
    column_carry = carried.get("_columns") if carried else None
    if column_carry is None:
        headers = [block for block in page.get_text("blocks") if str(block[4]).startswith("FROM:")]
        if not (
            any(block[0] < midpoint and block[2] <= midpoint for block in headers)
            and any(block[0] >= midpoint for block in headers)
        ):
            return parse_page(text, page_no, cover, carried, keep_raw=True)
        column_carry = {"left": None, "right": None}
    left, right = pymupdf.Rect(page.rect), pymupdf.Rect(page.rect)
    left.x1 = right.x0 = midpoint
    texts = [page.get_text("text", clip=rect) for rect in (left, right)]
    if normalize_ws("".join(texts)).strip() != normalize_ws(text).strip():
        raise PageExtractionError(
            "unsupported_column_layout",
            f"Page {page_no}: column text does not preserve the complete original extraction order",
        )
    rows, errors, coverages, next_carry = [], [], [], {}
    for name, column_text in zip(("left", "right"), texts):
        parsed, coverage, problems, next_carry[name] = parse_page(
            column_text, page_no, cover, column_carry[name], keep_raw=True
        )
        for row in parsed:
            row["schedule_id"] = f"TT-{page_no:05d}-{len(rows):04d}"
            if row["section_source"]:
                row["section_source"] += f"/column:{name}"
            rows.append(row)
        errors.extend(problems)
        coverages.append(coverage)
    sections = sum(c["sections"] for c in coverages)
    if rows:
        ptype = "table" if sections else "continuation"
    elif sections:
        ptype = "consult" if "Consult your travel agent" in text else "header_only"
    elif not text.strip():
        ptype = "blank"
    elif all(c["type"] in ("notes", "blank") for c in coverages):
        ptype = "notes"
    else:
        ptype = "unknown"
    coverage = dict(
        page=page_no,
        type=ptype,
        sections=sections,
        rows_found=sum(c["rows_found"] for c in coverages),
        rows_parsed=sum(c["rows_parsed"] for c in coverages),
        carry_used=sum(c["carry_used"] for c in coverages),
        status="excluded" if errors else "ok",
        reason=";".join(sorted({e["reason"] for e in errors})),
    )
    return rows, coverage, errors, {"_columns": next_carry}


def _unverified_carry(carry, page_no):
    if not carry:
        return None
    if "_columns" in carry:
        return {"_columns": {key: _unverified_carry(value, page_no) for key, value in carry["_columns"].items()}}
    return dict(carry, _unverified_since=page_no)


def _write_part(path, payload):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    temporary.replace(path)


class CheckpointTexts:
    def __init__(self, state, chunk_pages):
        self.state, self.chunk_pages, self.start, self.texts = (
            state,
            chunk_pages,
            None,
            {},
        )

    def read(self, page):
        name = f"page_{page:06d}.txt"
        old = self.state / name
        if old.is_file():
            return old.read_text(encoding="utf-8")
        start = (page - 1) // self.chunk_pages * self.chunk_pages
        if start != self.start:
            self.texts = json.loads(
                (self.state / f"part_{start:06d}.json").read_text(encoding="utf-8")
            ).get("texts", {})
            self.start = start
        return self.texts[name]


def write_summary(out, report, elapsed, reused=False):
    baseline = report.get("baseline_comparison") or {}
    lines = [
        "Итог PDF",
        f"Страницы: {report['pages']} / {report['total_pages']}",
        f"Строки расписания: {report['rows']}; со статусом ok: {report['rows_ok']}",
        f"Страницы, требующие проверки: {report.get('excluded_pages', 0)}",
        f"Результат: {report['status']}; использована готовая выгрузка: {'да' if reused else 'нет'}",
        f"Текущий запуск: {elapsed:.2f} с",
    ]
    if baseline:
        lines.append(
            f"Сравнение с первым парсером: {baseline.get('status')}; потеряно строк: {baseline.get('missing_legacy_rows', 'не определено')}"
        )
    lines.append(f"Файлы сохранены в: {Path(out).resolve()}")
    text = "\n".join(lines)
    (Path(out) / "summary.txt").write_text(text + "\n", encoding="utf-8")
    return text


def _verified_header_only(page):
    """Recognize a repeated timetable masthead, never an arbitrary image page.

    Some source pages contain only the same logo/banner as populated tables.
    Require matching image content and placement from a text-bearing table,
    no other painted objects, and an entirely white rendered page body.
    """
    images = page.get_image_info(hashes=True)
    header_bottom = page.rect.y0 + page.rect.height * 0.1
    if not images or any(image["bbox"][3] > header_bottom for image in images):
        return False
    if any(
        drawing["type"] != "f"
        or drawing.get("fill") != (1.0, 1.0, 1.0)
        or drawing["rect"].y1 > header_bottom
        for drawing in page.get_drawings()
    ) or next(page.annots(), None) is not None:
        return False

    def image_identity(image):
        return image["digest"], image["bbox"], image["transform"]

    expected = sorted(image_identity(image) for image in images)
    for index in range(min(10, page.parent.page_count)):
        reference = page.parent[index]
        if reference.rect != page.rect or not SECTION_RE.search(
            normalize_ws(reference.get_text("text"))
        ):
            continue
        reference_images = reference.get_image_info(hashes=True)
        header_images = [
            image for image in reference_images if image["bbox"][3] <= header_bottom
        ]
        if sorted(image_identity(image) for image in header_images) == expected:
            body = pymupdf.Rect(page.rect)
            body.y0 = header_bottom
            raster = page.get_pixmap(clip=body, colorspace=pymupdf.csRGB, alpha=False)
            return all(value == 255 for value in raster.samples)
    return False


def _extract(page, ocr=False):
    # Preserve the original working reader's extraction order exactly.
    content = page.get_text("text")
    if not content.strip() and page.get_images():
        if _verified_header_only(page):
            return content
        if not ocr:
            raise PageExtractionError(
                "image_without_text",
                f"Page {page.number + 1} has images without text; OCR or manual inspection required",
            )
        try:
            tp = page.get_textpage_ocr(full=True)
            content = page.get_text("text", textpage=tp)
        except Exception as exc:
            if isinstance(exc, MemoryError):
                raise
            raise PageExtractionError(
                "ocr_failed", f"Page {page.number + 1}: {type(exc).__name__}: {exc}"
            ) from exc
        if not content.strip():
            raise PageExtractionError(
                "ocr_empty", f"OCR produced no text on page {page.number + 1}"
            )
    return content


def process(
    path,
    out,
    source_id="timetable_pdf",
    chunk_pages=100,
    limit_pages=None,
    ocr=False,
    progress=False,
    force=False,
):
    """Checkpoint identity includes input, parser, contract and processing options."""
    import shutil
    import uuid
    from collections import Counter

    from ..contracts import SCHEMAS, TableBatch
    from ..io import atomic_json, publish_directory, write_batches
    from ..provenance import fingerprint, stable_id
    from .common import Emitter
    from .pdf_reference import capture, compare
    import pyarrow.parquet as pq

    run_started = time.perf_counter()

    if chunk_pages < 1 or (limit_pages is not None and limit_pages < 1):
        raise ValueError("Page limits/chunk sizes must be positive")
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    state = out.with_name(out.name + ".checkpoint")
    state.mkdir(exist_ok=True)
    e = Emitter(path, source_id)
    reference = capture(out, e.version)
    if reference is None and Path(OUT_DIR) != out:
        reference = capture(OUT_DIR, e.version)
    if progress and reference:
        print(f"Эталон первого парсера сохранён: {reference}", flush=True)
    signature = dict(
        sha256=e.version,
        parser_version="2.1",
        contract_version="1.0",
        source_id=source_id,
        chunk_pages=chunk_pages,
        limit_pages=limit_pages,
        ocr=ocr,
        extraction_mode="column_carry_original_text",
    )
    checkpoint_file = state / "checkpoint.json"
    ck = (
        json.loads(checkpoint_file.read_text(encoding="utf-8"))
        if checkpoint_file.exists()
        else None
    )
    if isinstance(ck, dict) and ck.get("signature") == {
        k: v for k, v in signature.items() if k != "extraction_mode"
    }:
        # Reuse older partial work only after verifying every stored text page
        # agrees with the original extraction order. No sampling/guessing here.
        compatible = True
        if progress:
            print(
                f"Проверяю совместимость {ck['next_page']} сохранённых страниц с первым парсером...",
                flush=True,
            )
        saved_texts = CheckpointTexts(state, chunk_pages)
        with pymupdf.open(path) as previous_doc:
            for p in range(ck["next_page"]):
                try:
                    saved = saved_texts.read(p + 1)
                except (FileNotFoundError, KeyError):
                    compatible = False
                    break
                if normalize_ws(saved) != normalize_ws(
                    previous_doc[p].get_text("text")
                ):
                    compatible = False
                    break
        if compatible:
            ck["signature"] = signature
            atomic_json(checkpoint_file, ck)
    if ck and (not isinstance(ck, dict) or ck.get("signature") != signature):
        # Old chunks belong to another input/configuration; never mix them into a new result.
        state.rename(state.with_name(state.name + ".previous_" + uuid.uuid4().hex[:8]))
        state.mkdir()
        ck = None
    ck = ck or dict(
        signature=signature,
        next_page=0,
        carry=None,
        cover=None,
        cover_raw=None,
        seconds=0.0,
    )
    t0 = time.perf_counter()
    with pymupdf.open(path) as doc:
        total = min(doc.page_count, limit_pages or doc.page_count)
        existing_report = out / "source_report.json"
        if (
            not force
            and ck["next_page"] == total
            and ck["signature"] == signature
            and existing_report.is_file()
        ):
            previous = json.loads(existing_report.read_text(encoding="utf-8"))
            required_files = [out / f"{name}.parquet" for name in SCHEMAS] + [
                out / name
                for name in ("schedules.csv", "schedules.jsonl", "page_coverage.csv")
            ]
            if previous.get("processing_signature"):
                required_files.append(out / "page_text.zip")
            if (
                previous.get("sha256") == e.version
                and previous.get("pages") == total
                and all(
                    file.is_file()
                    and file.stat().st_mtime_ns <= existing_report.stat().st_mtime_ns
                    for file in required_files
                )
                and all(
                    pq.ParquetFile(out / f"{name}.parquet").metadata.num_rows
                    == previous["counts"][name]
                    for name in SCHEMAS
                )
            ):
                write_summary(
                    out, previous, time.perf_counter() - run_started, reused=True
                )
                return previous
        if progress:
            print(
                f"Страниц: {doc.page_count}. Обработка: {total}. В checkpoint: {ck['next_page']}.",
                flush=True,
            )
        if ck["cover"] is None:
            try:
                cover_text = normalize_ws(_extract(doc[0], ocr))
            except PageExtractionError:
                # The normal page loop records the cover extraction problem.
                cover_text = ""
            ck["cover"] = parse_cover(cover_text)
            match = COVER_RE.search(cover_text)
            ck["cover_raw"] = match.group(0) if match else None
        for start in range(ck["next_page"], total, chunk_pages):
            end = min(start + chunk_pages, total)
            rows, coverage, errors, texts = [], [], [], {}
            carry = ck["carry"]
            for p in range(start, end):
                page = doc[p]
                original = ""
                try:
                    original = _extract(page, ocr)
                    r, c, err, carry = parse_pdf_page(page, original, p + 1, ck["cover"], carry)
                    c["extraction_method"] = (
                        "ocr"
                        if ocr
                        and not page.get_text("text").strip()
                        and page.get_images()
                        else "text"
                    )
                    if not original.strip() and page.get_images():
                        c["type"] = "header_only"
                        c["extraction_method"] = "verified_header_only"
                except PageExtractionError as exc:
                    r = []
                    # Retain the old parser's route candidates, but explicitly
                    # flag their uncertainty after an unreadable page.
                    carry = _unverified_carry(carry, p + 1)
                    c = dict(
                        page=p + 1,
                        type=exc.reason,
                        sections=0,
                        rows_found=0,
                        rows_parsed=0,
                        carry_used=0,
                        status="excluded",
                        reason=exc.reason,
                        extraction_method="failed",
                    )
                    err = [dict(page=p + 1, reason=exc.reason, sample=str(exc))]
                    # Preserve the original page as a small PDF for inspection/upload.
                    diagnostics = state / "problem_pages"
                    diagnostics.mkdir(exist_ok=True)
                    diagnostic = diagnostics / f"page_{p + 1:06d}.pdf"
                    with pymupdf.open() as single_page:
                        single_page.insert_pdf(doc, from_page=p, to_page=p)
                        single_page.save(diagnostic)
                    if progress:
                        print(
                            f"Страница {p + 1}: {exc.reason}. Сохранена для проверки: {diagnostic}. Продолжаю.",
                            flush=True,
                        )
                page_text = state / f"page_{p + 1:06d}.txt"
                texts[page_text.name] = original
                for i, row in enumerate(r):
                    locator = f"page:{p + 1}/schedule:{i + 1}"
                    row.update(
                        e.base(
                            locator,
                            {
                                "page_text": page_text.name,
                                "page_text_archive": "page_text.zip",
                                "parsed": dict(row),
                            },
                        )
                    )
                    row["schedule_id"] = stable_id(
                        source_id, e.version, locator, "schedules"
                    )
                    invalid = []
                    for key in ("departure_local", "arrival_local"):
                        value = row[key]
                        if not (
                            value.isascii()
                            and len(value) == 5
                            and value[2] == ":"
                            and value[:2].isdigit()
                            and value[3:].isdigit()
                            and int(value[:2]) < 24
                            and int(value[3:]) < 60
                        ):
                            invalid.append(key)
                    if ck["cover"] and (
                        row["valid_from"] is None or row["valid_to"] is None
                    ):
                        invalid.append("validity")
                    if invalid:
                        row["parse_status"] = "invalid_" + "_".join(invalid)
                        err.append(
                            dict(
                                page=p + 1,
                                reason=row["parse_status"],
                                sample=row["raw_line"],
                            )
                        )
                        c["status"], c["reason"] = "excluded", row["parse_status"]
                c["rows_parsed"] = sum(row["parse_status"] == "ok" for row in r)
                rows.extend(r)
                coverage.append(c)
                errors.extend(err)
            part = state / f"part_{start:06d}.json"
            _write_part(
                part, dict(rows=rows, coverage=coverage, errors=errors, texts=texts)
            )
            ck.update(
                next_page=end,
                carry=carry,
                seconds=ck["seconds"] + time.perf_counter() - t0,
            )
            atomic_json(checkpoint_file, ck)
            if progress:
                print(
                    f"Разобраны страницы {start + 1}–{end} / {total}; строк в порции: {len(rows)}. Прогресс сохранён.",
                    flush=True,
                )
            t0 = time.perf_counter()
        full_pages = doc.page_count
    staging = out.with_name(out.name + "." + uuid.uuid4().hex + ".partial")
    staging.mkdir()
    if progress:
        print(
            "Сборка CSV, JSONL, Parquet и отчётов из сохранённых порций...", flush=True
        )
    stats, types = Counter(), Counter()
    with (
        open(staging / "page_coverage.csv", "w", newline="", encoding="utf-8") as cf,
        open(staging / "schedules.csv", "w", newline="", encoding="utf-8") as csvf,
        open(staging / "schedules.jsonl", "w", encoding="utf-8") as jf,
        ZipFile(
            staging / "page_text.zip", "w", compression=ZIP_DEFLATED, compresslevel=1
        ) as text_archive,
    ):
        cw = csv.DictWriter(
            cf,
            fieldnames=[
                "page",
                "type",
                "sections",
                "rows_found",
                "rows_parsed",
                "carry_used",
                "status",
                "reason",
                "extraction_method",
            ],
        )
        cw.writeheader()
        rw = csv.DictWriter(csvf, fieldnames=SCHEMAS["schedules"].names)
        rw.writeheader()

        def batches():
            for start in range(0, total, chunk_pages):
                part = json.loads(
                    (state / f"part_{start:06d}.json").read_text(encoding="utf-8")
                )
                for coverage in part["coverage"]:
                    name = f"page_{coverage['page']:06d}.txt"
                    original = part.get("texts", {}).get(name)
                    if original is None:
                        original = (state / name).read_text(encoding="utf-8")
                    text_archive.writestr(name, original.encode("utf-8"))
                    coverage.setdefault("extraction_method", "text")
                    cw.writerow(coverage)
                    types[coverage["type"]] += 1
                    stats["excluded_pages"] += coverage["status"] != "ok"
                    stats["ocr_pages"] += coverage["extraction_method"] == "ocr"
                    stats["extraction_errors"] += (
                        coverage["extraction_method"] == "failed"
                    )
                for row in part["rows"]:
                    old_prefix = f'{{"page_text": "page_{row["source_page"]:06d}.txt", "parsed": '
                    if row["raw_json"].startswith(old_prefix):
                        row["raw_json"] = row["raw_json"].replace(
                            old_prefix,
                            old_prefix.replace(
                                '"parsed": ',
                                '"page_text_archive": "page_text.zip", "parsed": ',
                            ),
                            1,
                        )
                    rw.writerow(
                        {
                            k: "\\N" if row.get(k) is None else row.get(k)
                            for k in SCHEMAS["schedules"].names
                        }
                    )
                    jf.write(json.dumps(row, ensure_ascii=False) + "\n")
                    e.buffers.setdefault("provenance", []).append(
                        {
                            "source_id": row["source_id"],
                            "source_record_id": row["source_record_id"],
                            "locator": row["locator"],
                            "raw_json": "null",
                            "entity_table": "schedules",
                            "entity_id": row["schedule_id"],
                            "transformation": "reader:2.1",
                        }
                    )
                    stats["rows_ok"] += row["parse_status"] == "ok"
                for index, error in enumerate(part["errors"]):
                    e.error(
                        f"page:{error['page']}/error:{start}:{index}",
                        error["sample"],
                        error["reason"],
                    )
                yield TableBatch("schedules", part["rows"])
                yield from e.flush(True)
                if progress:
                    print(
                        f"Выгружены страницы до {min(start + chunk_pages, total)} / {total}.",
                        flush=True,
                    )

        counts, severities, _counters = write_batches(batches(), staging)
    # Raw extracted pages are needed to resolve the source lines independently of the parser.
    if (state / "problem_pages").is_dir():
        shutil.copytree(state / "problem_pages", staging / "problem_pages")
    baseline = None
    if reference:
        if progress:
            print("Сравнение с сохранённой выгрузкой первого парсера...", flush=True)
        try:
            baseline = compare(reference, staging, e.version)
        except Exception as exc:
            if isinstance(exc, MemoryError):
                raise
            baseline = {
                "status": "comparison_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
    complete = (
        total == full_pages
        and not stats["excluded_pages"]
        and not severities.get("error")
        and (baseline is None or baseline["status"] in ("matched", "not_applicable"))
    )
    report = dict(
        status="complete" if complete else "incomplete",
        source_id=source_id,
        output_path=str(out.resolve()),
        sha256=e.version,
        counts=counts,
        pages=total,
        total_pages=full_pages,
        rows=counts["schedules"],
        rows_ok=stats["rows_ok"],
        pages_by_type=dict(types),
        errors=counts["errors"],
        excluded_pages=stats["excluded_pages"],
        extraction_errors=stats["extraction_errors"],
        ocr_enabled=ocr,
        ocr_pages=stats["ocr_pages"],
        baseline_comparison=baseline,
        seconds=ck["seconds"],
        cover_period=ck["cover"],
        cover_period_raw=ck["cover_raw"],
        contract_version="1.0",
        limitations=[
            "Schedule validity must be compared to passenger dates in T11; never extrapolate historic routes.",
            "Missing +N marker leaves arrival_day_offset null.",
            "OCR requires separately installed Tesseract; unknown page templates block acceptance.",
        ],
    )
    report["pages_per_min"] = total / ck["seconds"] * 60 if ck["seconds"] else None
    report["processing_signature"] = signature
    try:
        import resource
        import sys

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report["process_peak_rss_mib"] = rss / (
            1024 * 1024 if sys.platform == "darwin" else 1024
        )
    except ImportError:
        report["process_peak_rss_mib"] = None
    if fingerprint(path) != e.version:
        raise RuntimeError("PDF changed during processing; result was not published")
    atomic_json(staging / "source_report.json", report)
    publish_directory(staging, out)
    write_summary(out, report, time.perf_counter() - run_started)
    return report


def run(limit_pages=None, suffix=""):
    return process(
        PDF_PATH,
        OUT_DIR if not suffix else PROJECT_ROOT / f"data/interim/timetable_pdf{suffix}",
        chunk_pages=CHUNK_PAGES,
        limit_pages=limit_pages,
    )


def audit(n=50, out=None):
    """Bounded reservoir sample; blank manual_match is not a verified review."""
    import random

    out = Path(out or OUT_DIR)
    sample = []
    rng = random.Random(42)
    if n < 1:
        raise ValueError("n must be positive")
    with open(out / "schedules.csv", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for i, row in enumerate(reader):
            if len(sample) < n:
                sample.append(row)
            else:
                k = rng.randrange(i + 1)
                if k < n:
                    sample[k] = row
    with open(out / "audit_sheet.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f, fieldnames=fieldnames + ["manual_match", "reviewer", "notes"]
        )
        w.writeheader()
        w.writerows(sorted(sample, key=lambda x: (int(x["source_page"]), x["locator"])))
