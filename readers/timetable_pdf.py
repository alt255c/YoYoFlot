"""
T03: Skyteam_Timetable.pdf -> schedules.csv/jsonl/parquet + page_coverage + errors + report.
Два подтверждённых класса таблиц: слитная строка и ячейки с новой строки.
Порции с чекпоинтом; carry-over секций FROM/TO между страницами и чанками.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pymupdf

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = PROJECT_ROOT / "data" / "Skyteam_Timetable.pdf"
OUT_DIR = PROJECT_ROOT / "data" / "interim" / "timetable_pdf"
CHUNK_PAGES = 2000

MONTHS = {"Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
          "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12}

WS_RE = re.compile(r"\s+")
COVER_RE = re.compile(
    r"Covers period:\s*(\d{2}) ([A-Z][a-z]{2}) (\d{4})\s*through\s*(\d{2}) ([A-Z][a-z]{2}) (\d{4})")
SECTION_RE = re.compile(
    r"FROM:\s*(?P<ocity>[A-Za-z .'-]+?)\s*,\s*(?P<ocountry>[A-Za-z .'-]+?)(?P<oiata>[A-Z]{3})\s*"
    r"TO:\s*(?P<dcity>[A-Za-z .'-]+?)\s*,\s*(?P<dcountry>[A-Za-z .'-]+?)(?P<diata>[A-Z]{3})")
ROW_RE = re.compile(
    r"(?P<valid>\d{2} [A-Z][a-z]{2}\s*-\s*\d{2} [A-Z][a-z]{2})\s*"
    r"(?P<days>[1-7](?: ?[1-7]){0,6})(?=\s?\d{2}:\d{2})\s*"
    r"(?P<dep>\d{2}:\d{2})\s*"
    r"(?P<arr>\d{2}:\d{2})(?P<offset>\+\d)?\s*"
    r"(?P<flight>[A-Z]{2} ?\d{1,4})(?P<star>\*?)\s*"
    r"(?P<aircraft>[A-Z0-9]{3})\s*"
    r"(?P<travel>\d+ ?H ?\d+ ?M)")
OPERATED_RE = re.compile(
    r"Operated by:\s*(?P<op>[A-Za-z .'-]+?)"
    r"(?=\s*(?:FROM:|TO:|Operated by:|\d{2} [A-Z][a-z]{2}\s*-|\d{2}:\d{2})|\s*$)"
)

OK_TYPES = {"cover", "table", "continuation", "consult", "header_only", "blank", "notes"}

FIELDS = ["schedule_id", "flight_number", "origin", "destination", "departure_local",
          "arrival_local", "arrival_day_offset", "days_of_week", "valid_from", "valid_to",
          "source_page", "parse_status", "section_source", "origin_city", "origin_country",
          "dest_city", "dest_country", "validity_raw", "days_raw", "aircraft", "travel_time",
          "operated_by", "codeshare_mark", "raw_line"]


def normalize_ws(text: str) -> str:
    """Любые пробельные символы (включая переносы и nbsp) -> один пробел."""
    return WS_RE.sub(" ", text.replace("\xa0", " ").replace("\u2007", " ").replace("\u202f", " "))


def iso_date(day: str, mon: str, cover):
    """Год из периода расписания: месяц >= начала -> год начала, иначе год конца."""
    if not cover:
        return None
    (_, sm, sy), (_, em, ey) = cover
    d, m = int(day), MONTHS[mon]
    if m >= sm:
        y = sy
    elif m <= em:
        y = ey
    else:
        return None
    return f"{y:04d}-{m:02d}-{d:02d}"


def parse_cover(text: str):
    m = COVER_RE.search(text)
    if not m:
        return None
    d1, m1, y1, d2, m2, y2 = m.groups()
    return (int(d1), MONTHS[m1], int(y1)), (int(d2), MONTHS[m2], int(y2))


def parse_page(text: str, page_no: int, cover, carried: dict | None = None):
    text = normalize_ws(text)
    rows, errors = [], []
    secs = [(m.start(), {**m.groupdict(), "page": page_no}) for m in SECTION_RE.finditer(text)]
    matches = list(ROW_RE.finditer(text))

    for i, rm in enumerate(matches):
        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        operated = OPERATED_RE.search(text, rm.end(), nxt)
        on_page = next((d for p, d in reversed(secs) if p < rm.start()), None)
        sec = on_page or carried
        if sec is None:
            src = None
            errors.append({"page": page_no, "reason": "row_without_section",
                           "sample": rm.group(0)[:120]})
        elif on_page is not None:
            src = "page"
        else:
            src = f"carried:{sec['page']}"
        vd, vto = rm["valid"].split(" - ")
        fd, fmon = vd.split()[0], vd.split()[1]
        td, tmon = vto.split()[0], vto.split()[1]
        rows.append({
            "schedule_id": f"TT-{page_no:05d}-{i:04d}",
            "flight_number": rm["flight"].replace(" ", ""),
            "origin": sec["oiata"] if sec else None,
            "destination": sec["diata"] if sec else None,
            "departure_local": rm["dep"],
            "arrival_local": rm["arr"],
            "arrival_day_offset": int(rm["offset"]) if rm["offset"] else None,
            "days_of_week": rm["days"].replace(" ", ""),
            "valid_from": iso_date(fd, fmon, cover),
            "valid_to": iso_date(td, tmon, cover),
            "source_page": page_no,
            "parse_status": "ok" if sec else "row_without_section",
            "section_source": src,
            "origin_city": sec["ocity"].strip() if sec else None,
            "origin_country": sec["ocountry"].strip() if sec else None,
            "dest_city": sec["dcity"].strip() if sec else None,
            "dest_country": sec["dcountry"].strip() if sec else None,
            "validity_raw": rm["valid"],
            "days_raw": rm["days"],
            "aircraft": rm["aircraft"],
            "travel_time": rm["travel"],
            "operated_by": operated["op"].strip() if operated else None,
            "codeshare_mark": bool(rm["star"]),
            "raw_line": rm.group(0),
        })

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
    elif not text.strip():
        ptype = "blank"
    else:
        ptype = "unknown"
    if ptype in ("unknown", "rows_without_header"):
        errors.append({"page": page_no, "reason": ptype, "sample": text[:200]})

    coverage = {"page": page_no, "type": ptype, "sections": len(secs),
                "rows_found": len(matches),
                "rows_parsed": sum(1 for r in rows if r["parse_status"] == "ok"),
                "carry_used": carry_used,
                "status": "ok" if ptype in OK_TYPES else "excluded",
                "reason": "" if ptype in OK_TYPES else ptype}

    return rows, coverage, errors, carry_out


def run(limit_pages: int | None = None, suffix: str = ""):
    out = OUT_DIR if not suffix else PROJECT_ROOT / f"data/interim/timetable_pdf{suffix}"
    parts = out / "parts"
    parts.mkdir(parents=True, exist_ok=True)
    ckpt_path = out / "checkpoint.json"
    ckpt = json.loads(ckpt_path.read_text(encoding="utf-8")) if ckpt_path.exists() else {
        "done_chunks": [], "cover": None, "carry": None}

    doc = pymupdf.open(PDF_PATH)
    total = min(doc.page_count, limit_pages or doc.page_count)
    if ckpt.get("total_pages") not in (None, total):
        ckpt = {"done_chunks": [], "cover": None, "carry": None}
    cover_raw = None
    if not ckpt["cover"]:
        first_text = normalize_ws(doc[0].get_text("text"))
        ckpt["cover"] = parse_cover(first_text)
        m = COVER_RE.search(first_text)
        cover_raw = m.group(0) if m else None
        ckpt["cover_raw"] = cover_raw
    cover = ckpt["cover"]
    cover_raw = ckpt.get("cover_raw")
    carry = ckpt.get("carry")

    t0 = time.perf_counter()
    for ci, start in enumerate(range(0, total, CHUNK_PAGES)):
        if ci in ckpt["done_chunks"]:
            continue
        rows_buf, cov_buf, err_buf = [], [], []
        for p in range(start, min(start + CHUNK_PAGES, total)):
            r, c, e, carry = parse_page(doc[p].get_text("text"), p + 1, cover, carry)
            rows_buf += r
            cov_buf.append(c)
            err_buf += e
        _write_parts(parts, ci, rows_buf, cov_buf, err_buf)
        ckpt["done_chunks"].append(ci)
        ckpt["total_pages"] = total
        ckpt["carry"] = carry
        ckpt_path.write_text(json.dumps(ckpt), encoding="utf-8")
    dt = time.perf_counter() - t0
    doc.close()
    assemble(out, dt, cover, cover_raw, total)


def _write_parts(parts: Path, ci: int, rows, cov, errs):
    tmp = parts / f".part_{ci:04d}.csv"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(parts / f"part_{ci:04d}.csv")
    (parts / f"cov_{ci:04d}.json").write_text(json.dumps(cov), encoding="utf-8")
    (parts / f"err_{ci:04d}.json").write_text(json.dumps(errs), encoding="utf-8")


def assemble(out: Path, dt: float, cover, cover_raw, total):
    rows, cov, errs = [], [], []
    for pf in sorted((out / "parts").glob("part_*.csv")):
        with open(pf, newline="", encoding="utf-8") as f:
            rows += list(csv.DictReader(f))
    for cf in sorted((out / "parts").glob("cov_*.json")):
        cov += json.loads(cf.read_text(encoding="utf-8"))
    for ef in sorted((out / "parts").glob("err_*.json")):
        errs += json.loads(ef.read_text(encoding="utf-8"))

    for r in rows:
        r["arrival_day_offset"] = None if r["arrival_day_offset"] in ("", "None") else int(r["arrival_day_offset"])
        r["source_page"] = int(r["source_page"])
        r["codeshare_mark"] = r["codeshare_mark"] == "True"

    with open(out / "schedules.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    with open(out / "schedules.jsonl", "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    table = pa.Table.from_pylist(rows, schema=pa.schema([
        (n, pa.int64() if n in ("source_page", "arrival_day_offset")
         else pa.bool_() if n == "codeshare_mark" else pa.string())
        for n in FIELDS]))
    pq.write_table(table, out / "schedules.parquet")

    with open(out / "page_coverage.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["page", "type", "sections", "rows_found",
                                          "rows_parsed", "carry_used", "status", "reason"])
        w.writeheader()
        w.writerows(cov)
    err_table = pa.Table.from_pylist(
        errs, schema=pa.schema([("page", pa.int64()), ("reason", pa.string()), ("sample", pa.string())]))
    pq.write_table(err_table, out / "errors.parquet")

    report = {
        "pages": total, "rows": len(rows),
        "rows_ok": sum(1 for r in rows if r["parse_status"] == "ok"),
        "pages_by_type": {t: sum(1 for c in cov if c["type"] == t) for t in {c["type"] for c in cov}},
        "errors": len(errs),
        "cover_period_raw": cover_raw,
        "cover_period": cover,
        "seconds": round(dt, 1),
        "pages_per_min": round(total / dt * 60, 1) if dt else None,
        "limitations": [
            "arrival_day_offset берётся только из маркера +N в шаблоне; без маркера null",
            ("период 01 Nov 2018 - 31 Jan 2019 не покрывает наблюдения пассажиров 2017 года: "
             "расписание не доказанный исторический маршрут для T11"),
            "codeshare: рейс со звёздочкой маркетинговый, фактический оператор в operated_by",
            "части таблиц без заголовка используют секцию предыдущей страницы (section_source=carried:N)",
        ],
    }
    (out / "source_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def audit(n: int = 50):
    """Лист ручного аудита: n строк равномерно по всему файлу."""
    with open(OUT_DIR / "schedules.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    step = max(1, len(rows) // n)
    sheet = OUT_DIR / "audit_sheet.csv"
    with open(sheet, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS + ["manual_match"])
        w.writeheader()
        for r in rows[::step][:n]:
            r["manual_match"] = ""
            w.writerow(r)
    print(f"audit sheet: {sheet} ({min(n, len(rows))} строк)")


def debug_page(n: int):
    doc = pymupdf.open(PDF_PATH)
    text = normalize_ws(doc[n - 1].get_text("text"))
    print("repr head:", repr(text[:400]))
    print("sections:", len(SECTION_RE.findall(text)))
    print("rows:", len(ROW_RE.findall(text)))
    doc.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit-pages", type=int, default=None)
    ap.add_argument("--suffix", default="")
    ap.add_argument("--audit", type=int, default=None)
    ap.add_argument("--debug-page", type=int, default=None)
    args = ap.parse_args()
    if args.audit:
        audit(args.audit)
    elif args.debug_page:
        debug_page(args.debug_page)
    else:
        run(args.limit_pages, args.suffix)
        out = OUT_DIR if not args.suffix else PROJECT_ROOT / f"data/interim/timetable_pdf{args.suffix}"
        report = json.loads((out / "source_report.json").read_text(encoding="utf-8"))
        print(json.dumps(report, ensure_ascii=False, indent=2))