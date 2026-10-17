"""Quality and lineage audit for T12 canonical Parquet tables (T14)."""
from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

from .normalize.canonical import KEYS, SCHEMAS

TABLES = tuple(SCHEMAS)
CHILDREN = (
    ("identifiers", "observation_id", "observations", "observation_id"),
    ("travel_observations", "observation_id", "observations", "observation_id"),
    ("travel_observations", "loyalty_identifier_id", "identifiers", "identifier_id"),
)


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table(path: Path, name: str) -> Path:
    result = path / f"{name}.parquet"
    if not result.is_file():
        raise FileNotFoundError(result)
    return result


def _count(db, table: str) -> int:
    return int(db.execute(f"SELECT count(*) FROM {_q(table)}").fetchone()[0])


def _sample(db, output: Path, size: int) -> int:
    output.parent.mkdir(parents=True, exist_ok=True)
    query = """
        SELECT source_id, observation_id, locator, name_raw, birth_date,
               row_number() OVER (PARTITION BY source_id ORDER BY observation_id) AS rn
        FROM observations
        QUALIFY rn <= ?
        ORDER BY source_id, observation_id
    """
    rows = db.execute(query, [max(1, size)]).fetchall()
    fields = ["source_id", "observation_id", "locator", "name_raw", "birth_date", "review_status"]
    with output.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        writer.writerows([*row, "pending_independent_review"] for row in rows)
    return len(rows)


def audit(input_dir, output_dir=None, sample_size=30, memory_limit="512MB"):
    """Run all structural, balance, coverage, and provenance checks.

    ``input_dir`` is a completed T12 directory containing canonical Parquet
    tables. No rows are modified. The report marks the independent human
    review sample as pending until a reviewer supplies decisions.
    """
    source = Path(input_dir)
    destination = Path(output_dir) if output_dir else source
    destination.mkdir(parents=True, exist_ok=True)
    files = {name: _table(source, name) for name in TABLES}
    blockers = []
    warnings = []
    counts = {}
    duplicate_counts = {}
    schemas = {}
    with duckdb.connect() as db:
        db.execute("SET memory_limit=?", [memory_limit])
        db.execute("SET threads=2")
        for name, path in files.items():
            actual = pq.read_schema(path)
            expected = SCHEMAS[name]
            schemas[name] = actual.equals(expected)
            if not schemas[name]:
                blockers.append(f"schema_mismatch:{name}")
            db.from_parquet(str(path)).create_view(name)
            counts[name] = _count(db, name)
            key = KEYS[name][0]
            duplicate_counts[name] = int(db.execute(
                f"SELECT count(*) FROM (SELECT {_q(key)} FROM {_q(name)} GROUP BY {_q(key)} HAVING {_q(key)} IS NULL OR count(*) > 1)"
            ).fetchone()[0])
            if duplicate_counts[name]:
                blockers.append(f"duplicate_or_null_key:{name}")

        relations = {}
        for child, fk, parent, pk in CHILDREN:
            value = int(db.execute(
                f"SELECT count(*) FROM {_q(child)} c LEFT JOIN {_q(parent)} p ON c.{_q(fk)}=p.{_q(pk)} WHERE c.{_q(fk)} IS NOT NULL AND p.{_q(pk)} IS NULL"
            ).fetchone()[0])
            relations[f"{child}.{fk}->{parent}.{pk}"] = value
            if value:
                blockers.append(f"orphan_reference:{child}.{fk}")
        loyalty_mismatch = int(db.execute(
            """SELECT count(*) FROM travel_observations t JOIN identifiers i
               ON t.loyalty_identifier_id=i.identifier_id
               WHERE t.loyalty_identifier_id IS NOT NULL
                 AND (i.kind IS DISTINCT FROM 'loyalty_card'
                      OR t.observation_id <> i.observation_id)"""
        ).fetchone()[0])
        relations["loyalty_context_mismatch"] = loyalty_mismatch
        if loyalty_mismatch:
            blockers.append("loyalty_context_mismatch")

        provenance_duplicate = int(db.execute(
            """SELECT count(*) FROM (
                SELECT entity_table, entity_id, source_record_id
                FROM provenance GROUP BY ALL HAVING count(*) > 1
            )"""
        ).fetchone()[0])
        if provenance_duplicate:
            blockers.append("duplicate_provenance_link")
        provenance_missing = {}
        for name in TABLES:
            if name == "provenance":
                continue
            key = KEYS[name][0]
            provenance_missing[name] = int(db.execute(
                f"""SELECT count(*) FROM {_q(name)} x
                    LEFT JOIN provenance p ON p.entity_table=? AND p.entity_id=x.{_q(key)}
                    WHERE p.entity_id IS NULL""", [name]
            ).fetchone()[0])
            if provenance_missing[name]:
                blockers.append(f"missing_provenance:{name}")
        unknown_provenance = int(db.execute(
            "SELECT count(*) FROM provenance WHERE entity_table NOT IN (" + ",".join("?" for _ in TABLES) + ")",
            list(TABLES),
        ).fetchone()[0])
        if unknown_provenance:
            blockers.append("unknown_provenance_target")

        coverage = {}
        for source_id, in db.execute("SELECT DISTINCT source_id FROM observations ORDER BY source_id").fetchall():
            coverage[source_id] = {
                "observations": int(db.execute("SELECT count(*) FROM observations WHERE source_id=?", [source_id]).fetchone()[0]),
                "with_name": int(db.execute("SELECT count(*) FROM observations WHERE source_id=? AND name_raw IS NOT NULL", [source_id]).fetchone()[0]),
                "with_birth_date": int(db.execute("SELECT count(*) FROM observations WHERE source_id=? AND birth_date IS NOT NULL", [source_id]).fetchone()[0]),
                "identifiers": int(db.execute("SELECT count(*) FROM identifiers WHERE source_id=?", [source_id]).fetchone()[0]),
                "travels": int(db.execute("SELECT count(*) FROM travel_observations WHERE source_id=?", [source_id]).fetchone()[0]),
                "with_route": int(db.execute("SELECT count(*) FROM travel_observations WHERE source_id=? AND origin_raw IS NOT NULL AND destination_raw IS NOT NULL", [source_id]).fetchone()[0]),
                "with_departure": int(db.execute("SELECT count(*) FROM travel_observations WHERE source_id=? AND departure_local IS NOT NULL", [source_id]).fetchone()[0]),
            }
        errors = dict(db.execute("SELECT severity, count(*) FROM errors GROUP BY severity ORDER BY severity").fetchall())
        sample_path = destination / "quality_review_sample.csv"
        sample_count = _sample(db, sample_path, sample_size)

    canonical_report_path = source / "canonical_report.json"
    balance = {"status": "unavailable"}
    if canonical_report_path.is_file():
        report = json.loads(canonical_report_path.read_text(encoding="utf-8"))
        expected_counts = report.get("counts", {})
        mismatches = {k: {"report": expected_counts.get(k), "actual": v} for k, v in counts.items() if expected_counts.get(k) != v}
        balance = {"status": "passed" if not mismatches else "failed", "mismatches": mismatches,
                   "input_counts": report.get("input_counts", {}), "source_status": report.get("status")}
        if mismatches:
            blockers.append("canonical_balance_mismatch")
        if report.get("status") not in (None, "complete"):
            warnings.append(f"source_status:{report.get('status')}")
    else:
        warnings.append("canonical_report_missing")

    status = "failed" if blockers else "pass_with_limitations" if warnings or errors else "passed"
    result = {
        "status": status,
        "input_dir": str(source),
        "counts": counts,
        "schemas": schemas,
        "duplicate_or_null_keys": duplicate_counts,
        "relations": relations,
        "provenance": {"duplicate_links": provenance_duplicate, "missing_by_table": provenance_missing, "unknown_targets": unknown_provenance},
        "coverage_by_source": coverage,
        "errors_by_severity": errors,
        "balance": balance,
        "blockers": sorted(set(blockers)),
        "limitations": sorted(set(warnings + (["independent_review_pending"] if sample_count else []))),
        "review_sample": {"path": str(sample_path), "rows": sample_count, "status": "pending_independent_review"},
    }
    (destination / "quality.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = ["# T14 quality report", "", f"Status: **{status}**", "", "## Counts", ""]
    lines.extend(f"- `{name}`: {value}" for name, value in counts.items())
    lines.extend(["", "## Findings", "", f"- Blockers: {len(result['blockers'])}", f"- Errors in source table: {sum(errors.values())}", f"- Independent review sample: {sample_count} rows (pending)"])
    if result["blockers"]:
        lines.extend(["", "### Blockers", "", *[f"- `{item}`" for item in result["blockers"]]])
    if result["limitations"]:
        lines.extend(["", "### Limitations", "", *[f"- `{item}`" for item in result["limitations"]]])
    (destination / "quality.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return result
