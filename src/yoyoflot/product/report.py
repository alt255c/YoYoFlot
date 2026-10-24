"""Deterministic, read-only exports of a verified source-dataset product.

The report consumes ProductStore's frozen ranking and explanations. It never
recomputes a score, fills a missing observation, or generates source records.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .store import ProductStore


VERSION = "product-report-2.0"
PAYLOAD_FILES = {
    "top50.csv", "factors.csv", "flights.csv", "profiles.json", "summary.json", "report.md",
}
TOP_FIELDS = [
    "run_id", "rank", "person_id", "display_name", "identity_status", "eligibility_status",
    "matched_criteria_count", "available_criteria_count", "total_criteria_count", "rule_score",
    "total_segments", "dated_segments", "route_segments", "plottable_segments", "unplottable_segments",
    "matched_factors", "unavailable_factors", "source_evidence_count",
]
FACTOR_FIELDS = [
    "run_id", "rank", "person_id", "display_name", "criterion_id", "title_ru", "status", "value",
    "threshold", "weight", "contribution", "used_observations", "threshold_method", "reference_size",
    "reference_value", "availability_reason", "explanation_ru", "evidence_segment_count",
    "evidence_segment_ids_json", "source_evidence_count", "source_evidence_json",
]
SEGMENT_FIELDS = [
    "segment_id", "anchor_travel_id", "departure_date", "departure_label_ru", "flight_number",
    "origin_airport_id", "destination_airport_id", "origin_iata", "destination_iata",
    "departure_local", "departure_precision", "arrival_local", "arrival_precision",
    "departure_utc", "arrival_utc", "identity_status", "resolution_status", "participation_status",
    "event_kinds_json", "sources_json", "rule_id", "observation_count", "source_count",
    "graph_eligible", "geography_complete", "time_complete", "is_plottable", "unplottable_reason_ru",
]
AIRPORT_FIELDS = ["name", "city", "country", "latitude", "longitude", "timezone"]
FLIGHT_FIELDS = (
    ["run_id", "rank", "person_id", "display_name"] + SEGMENT_FIELDS
    + [f"{side}_{field}" for side in ("origin", "destination") for field in AIRPORT_FIELDS]
    + ["related_criteria_json", "source_evidence_count", "source_evidence_json"]
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _excel_cell(value):
    """Escape spreadsheet formula-like text only in the presentation CSV.

    JSON remains lossless. Numeric Python values, including negatives, stay
    numeric. Quoting CSV alone does not disable formulas in spreadsheet apps.
    """
    if isinstance(value, str):
        probe = value.lstrip(" \t\r\n\ufeff")
        if (probe.startswith(("=", "+", "-", "@", "＝", "＋", "－", "＠"))
                or value.startswith(("\t", "\r", "\n"))):
            return "'" + value
    return value


def _write_csv(path, fields, rows):
    # UTF-8 BOM makes Cyrillic readable when opened directly in Excel.
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="raise", quoting=csv.QUOTE_ALL)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _excel_cell(row.get(key)) for key in fields})


def _export_rows(profiles, run_id):
    top, factors, flights = [], [], []
    for profile in profiles:
        person = profile["person"]
        common = {"run_id": run_id, "rank": person["rank"], "person_id": person["person_id"],
                  "display_name": person.get("display_name")}
        links = defaultdict(list)
        for link in profile["source_evidence"]:
            links[link["segment_id"]].append(link)
        criteria = defaultdict(list)
        all_factors = profile["factors"] + profile.get("additional_factors", [])
        for factor in all_factors:
            for segment_id in factor["evidence_segment_ids"]:
                criteria[segment_id].append(factor["criterion_id"])
            source_links = factor.get('source_evidence') or [link for segment_id in factor["evidence_segment_ids"] for link in links[segment_id]]
            row = {key: factor.get(key) for key in FACTOR_FIELDS if key not in common}
            row.update(common, evidence_segment_count=len(factor["evidence_segment_ids"]),
                       evidence_segment_ids_json=_json(factor["evidence_segment_ids"]),
                       source_evidence_count=len(source_links), source_evidence_json=_json(source_links))
            factors.append(row)
        row = {key: person.get(key) for key in TOP_FIELDS if key not in common}
        row.update(common, total_criteria_count=len(all_factors),
                   plottable_segments=profile["route_balance"]["plottable"],
                   unplottable_segments=profile["route_balance"]["unplottable"],
                   matched_factors="; ".join(f["criterion_id"] + " " + f["title_ru"] for f in all_factors if f["status"] == "matched"),
                   unavailable_factors="; ".join(f["criterion_id"] + ": " + (f["availability_reason"] or "") for f in all_factors if f["status"] == "unavailable"),
                   source_evidence_count=len(profile["source_evidence"]))
        top.append(row)
        for segment in profile["segments"]:
            row = {key: segment.get(key) for key in SEGMENT_FIELDS}
            row.update(common)
            for side in ("origin", "destination"):
                airport = segment[side] or {}
                row.update({f"{side}_{key}": airport.get(key) for key in AIRPORT_FIELDS})
            row.update(related_criteria_json=_json(sorted(criteria[segment["segment_id"]])),
                       source_evidence_count=len(links[segment["segment_id"]]),
                       source_evidence_json=_json(links[segment["segment_id"]]))
            flights.append(row)
    return top, factors, flights


def _population_coverage(store):
    # Aggregate persisted availability only; no alternative factor formulas.
    with store.connection() as db:
        cur = db.execute("""SELECT criterion_id, title_ru, status, availability_reason, count(*) profiles
            FROM explanations GROUP BY ALL ORDER BY criterion_id, status, availability_reason NULLS LAST""")
        fields = [column[0] for column in cur.description]
        return [dict(zip(fields, row)) for row in cur.fetchall()]


def _summary(store, metadata, profiles, top, factors, flights, request, report_id):
    factor_statuses = Counter(row["status"] for row in factors)
    source_linked = sum(row["source_evidence_count"] > 0 for row in flights)
    plottable = sum(bool(row["is_plottable"]) for row in flights)
    dated = sum(bool(row["departure_date"]) for row in flights)
    population = metadata["counts"]
    return {
        "schema_version": VERSION, "status": "complete", "report_id": report_id, "run_id": metadata["run_id"],
        "data_mode": metadata["data_mode"], "source_manifest_sha256": request["source_manifest_sha256"],
        "source_provenance": metadata["source_provenance"],
        "counts": {"persons": population["persons"], "segments": population["segments"], "top50": len(top),
                   "exported_factors": len(factors), "exported_segments": len(flights),
                   "source_evidence_rows": sum(len(profile["source_evidence"]) for profile in profiles)},
        "window": metadata["window"], "eligibility": metadata["eligibility"],
        "coverage": {
            "population_factor_statuses": _population_coverage(store),
            "top50": {"available_factors": len(factors) - factor_statuses["unavailable"],
                      "total_factors": len(factors), "factor_statuses": dict(sorted(factor_statuses.items())),
                      "profiles_with_all_factors": sum(row["available_criteria_count"] == row["total_criteria_count"] for row in top),
                      "dated_segments": dated, "undated_segments": len(flights) - dated,
                      "plottable_segments": plottable, "unplottable_segments": len(flights) - plottable,
                      "source_linked_segments": source_linked,
                      "segments_without_source_links": len(flights) - source_linked},
        },
        "methodology": {"sort_order": metadata["sort_order"], "contribution_formula": metadata["contribution_formula"],
                        "rules": metadata["rules"], "graph": metadata["graph"],
                        "ranking_source": "Frozen tables/top50.parquet via ProductStore; scores are not recalculated"},
        "evaluation": metadata["evaluation"], "limitations": metadata["limitations"],
        "independent_review": metadata["independent_review"],
        "csv": {"encoding": "utf-8-sig", "delimiter": ",", "null": "empty cell",
                "formula_protection": "Formula-like strings have a leading apostrophe in CSV only; profiles.json retains original strings"},
        "paths": {name: name for name in sorted(PAYLOAD_FILES | {"report_manifest.json"})},
        "top50": top,
    }


def _plain(value):
    """Keep source names from breaking terminal lines and Markdown cells."""
    text = "" if value is None else str(value)
    return " ".join("".join(c if c.isprintable() else " " for c in text).split())


def _md(value):
    return _plain(value).replace("\\", "\\\\").replace("|", "\\|").replace("<", "&lt;").replace(">", "&gt;")


def _human_report(summary):
    counts, coverage = summary["counts"], summary["coverage"]["top50"]
    window = summary["window"]
    rows = [
        "# YoYoFlot — аналитический результат", "",
        f"Снимок: `{summary['run_id']}`. SHA-256 манифеста: `{summary['source_manifest_sha256']}`.", "",
        f"В исходном снимке {counts['persons']:,} профилей и {counts['segments']:,} сегментов. "
        f"Экспортировано {counts['top50']} профилей из зафиксированного топ-50; "
        "если подходящих профилей меньше 50, результат не дополняется.",
        f"Общее окно наблюдаемых дат: {window['start'] or 'нет'} — {window['end'] or 'нет'} "
        f"({window['days']} дней). Непрерывное покрытие источников этим окном не подтверждено.", "",
        "## Методика", "",
        "Рейтинг, факторы и их свидетельства прочитаны из проверенного снимка через ProductStore. "
        "Исходные таблицы не изменяются; новые наблюдения, координаты и даты не создаются. "
        "Порядок: число сработавших критериев по убыванию, сумма вкладов по убыванию, стабильный person_id по возрастанию.",
        "Для доступного сработавшего критерия вклад = вес × min(3, факт / порог); "
        "для несработавшего или недоступного — 0. Недоступность не означает отсутствие признака. "
        "Доступные/все критерии показаны для каждого профиля; причины недоступности сохранены отдельно.",
        "M01 — число датированных перелётов на 30 дней общего окна; M02 — доля быстрых обратных "
        "поездок через 1–2 дня среди сопоставимых соседних поездок; M03 — повторяемость направленных маршрутов. "
        "Факт, действующий порог, вес, вклад, объём наблюдений и способ калибровки находятся в factors.csv.",
        "Isolation Forest и граф совместных наблюдений приведены как отдельные результаты снимка. "
        "Их значения не добавляются к основному баллу и не изменяют опубликованный порядок топ-50.", "",
        "## Покрытие и ограничения", "",
        f"В топе доступны {coverage['available_factors']} из {coverage['total_factors']} факторов; "
        f"все факторы доступны у {coverage['profiles_with_all_factors']} из {counts['top50']} профилей.",
        f"Все {counts['exported_segments']} сегментов выбранных профилей сохранены. "
        f"С датами: {coverage['dated_segments']}; без дат: {coverage['undated_segments']}. "
        f"С координатами обоих аэропортов: {coverage['plottable_segments']}; "
        f"без полных координат: {coverage['unplottable_segments']}.",
        f"Связь с исходными строками есть у {coverage['source_linked_segments']} сегментов; "
        f"без неё: {coverage['segments_without_source_links']}. "
        "Если исходные ссылки отсутствуют, сохранены segment_id и anchor_travel_id; "
        "это не заменяет проверку первичных файлов. Статусы участия и разрешения идентичности сохранены: "
        "запись поездки сама по себе не подтверждает посадку.",
        "Баллы показывают выраженность заданных признаков, не вероятность класса и не доказательство нарушения. "
        "Без подтверждённых меток нельзя заявлять precision/recall или доказанную точность выявления. "
        "Пороги и веса требуют предметной проверки; независимая разметка не подменяется расчётом.",
        f"Статус независимой проверки: {_md(summary['independent_review'])}. "
        "Скоринг по проверенным факторам; "
        f"граф: {_md(summary['methodology']['graph']['status'])}.",
        "Полные ограничения происхождения, доступность факторов по всей популяции и результаты "
        "анализа чувствительности сохранены в summary.json.", "",
        "## Топ профилей", "",
        "| Место | person_id | Имя | Сработало | Доступно/всего | Балл | Перелёты | Без координат |",
        "| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in summary["top50"]:
        rows.append(f"| {row['rank']} | {_md(row['person_id'])} | {_md(row['display_name'])} | "
                    f"{row['matched_criteria_count']} | {row['available_criteria_count']}/{row['total_criteria_count']} | "
                    f"{row['rule_score']:.6g} | {row['total_segments']} | {row['unplottable_segments']} |")
    if not summary["top50"]:
        rows.extend(["", "Подходящих профилей нет. Причины недоступности факторов отражены в summary.json."])
    rows.extend(["", "## Файлы", "",
                 "- top50.csv — рейтинг и покрытие каждого профиля.",
                 "- factors.csv — факты, пороги, веса, вклады и связанные segment_id/исходные строки.",
                 "- flights.csv — все сегменты топа, даты и их точность, координаты, статусы и исходные ссылки.",
                 "- profiles.json — полные профили ProductStore без преобразования исходных строк для будущего интерфейса.",
                 "- summary.json — машинный итог, покрытие, методика и ограничения.",
                 "- report_manifest.json — run_id, происхождение снимка и SHA-256 всех файлов отчёта.", "",
                 "CSV: UTF-8 с BOM, разделитель — запятая; пустая ячейка означает неизвестное значение. "
                 "Строки, которые таблицы могут принять за формулу, получают начальный апостроф только в CSV. "
                 "Исходные значения сохраняются в JSON.", ""])
    return "\n".join(rows)


def format_console(summary, output):
    counts, coverage = summary["counts"], summary["coverage"]["top50"]
    lines = [f"Отчёт: {Path(output).resolve()}", f"Снимок: {summary['run_id']}",
             f"Топ: {counts['top50']}; перелётов: {counts['exported_segments']}; "
             f"без полных координат: {coverage['unplottable_segments']}",
             "Место  Сработало  Доступно  Балл       Профиль"]
    for row in summary["top50"][:10]:
        availability = f"{row['available_criteria_count']}/{row['total_criteria_count']}"
        name = _plain(row["display_name"])[:48]
        lines.append(f"{row['rank']:>5}  {row['matched_criteria_count']:>9}  {availability:>8}  "
                     f"{row['rule_score']:>9.5g}  {_plain(row['person_id'])} {name}")
    if counts["top50"] > 10:
        lines.append("Первые 10 показаны выше; весь топ — в top50.csv и report.md.")
    lines.append("Факторы: factors.csv; география: flights.csv; полные профили: profiles.json.")
    return "\n".join(lines) + "\n"


def _existing(target, request, report_id):
    """Accept only an intact report of this exact snapshot and exporter."""
    if not target.is_dir() or target.is_symlink():
        raise ValueError("Report output already exists and is not a report directory; choose a new path")
    manifest_path = target / "report_manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("Report output already exists without a report manifest; choose a new path")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("Existing report manifest is invalid; choose a new path") from exc
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != VERSION or manifest.get("status") != "complete"
            or manifest.get("request") != request or manifest.get("report_id") != report_id
            or manifest.get("run_id") != request["run_id"]):
        raise ValueError("Report output belongs to another snapshot/export version; choose a new path")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != PAYLOAD_FILES:
        raise ValueError("Existing report has an incomplete file manifest; choose a new path")
    if {entry.name for entry in target.iterdir()} != PAYLOAD_FILES | {"report_manifest.json"}:
        raise ValueError("Existing report contains unexpected files; choose a new path")
    for name, checksum in files.items():
        path = target / name
        if not path.is_file() or path.is_symlink() or fingerprint(path) != checksum:
            raise ValueError(f"Report checksum/path mismatch: {name}; choose a new path")
    return json.loads((target / "summary.json").read_text(encoding="utf-8"))


def run(product, output):
    """Export to a new directory, or verify and reuse an identical report.

    Both directions of product/output overlap are rejected. No --force path
    exists: another snapshot, damaged export, and foreign files are preserved.
    """
    source = Path(product).resolve()
    destination = Path(output)
    if destination.is_symlink():
        raise ValueError("Report output must not be a symbolic link")
    target = destination.resolve()
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("Report output must not overlap its product snapshot")
    store = ProductStore(source)
    manifest_sha256 = fingerprint(source / "run_manifest.json")
    request = {"schema_version": VERSION, "run_id": store.manifest["run_id"],
               "source_manifest_sha256": manifest_sha256, "source_files": store.manifest["files"],
               "source_request": store.manifest["request"], "exporter_sha256": fingerprint(Path(__file__)),
               "store_sha256": fingerprint(Path(__file__).with_name("store.py"))}
    report_id = "report-" + hashlib.sha256(_json(request).encode("utf-8")).hexdigest()[:20]
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ("." + target.name + ".lock"), "Report"):
        if target.exists():
            return _existing(target, request, report_id)
        metadata = store.metadata()
        profiles = [store.profile(row["person_id"]) for row in store.top()]
        top, factors, flights = _export_rows(profiles, store.manifest["run_id"])
        summary = _summary(store, metadata, profiles, top, factors, flights, request, report_id)
        with tempfile.TemporaryDirectory(prefix="." + target.name + ".", suffix=".partial", dir=target.parent) as temp:
            stage = Path(temp)
            _write_csv(stage / "top50.csv", TOP_FIELDS, top)
            _write_csv(stage / "factors.csv", FACTOR_FIELDS, factors)
            _write_csv(stage / "flights.csv", FLIGHT_FIELDS, flights)
            _write_json(stage / "profiles.json", {"schema_version": VERSION, "run_id": store.manifest["run_id"],
                        "source_manifest_sha256": manifest_sha256, "profiles": profiles})
            _write_json(stage / "summary.json", summary)
            (stage / "report.md").write_text(_human_report(summary), encoding="utf-8")
            # Reverify the source before publication, including a concurrently
            # replaced manifest. A partial export never appears as completed.
            ProductStore(source)
            if fingerprint(source / "run_manifest.json") != manifest_sha256:
                raise RuntimeError("Product snapshot changed while exporting report")
            _write_json(stage / "report_manifest.json", {
                "schema_version": VERSION, "status": "complete", "run_id": store.manifest["run_id"],
                "report_id": report_id, "request": request,
                "files": {name: fingerprint(stage / name) for name in sorted(PAYLOAD_FILES)},
            })
            # OutputLock prevents cooperating writers; this explicit check also
            # preserves a directory created while we were reading the source.
            if target.exists() or target.is_symlink():
                raise ValueError("Report output appeared during export; choose a new path")
            os.rename(stage, target)
    return summary
