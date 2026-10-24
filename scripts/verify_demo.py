"""Run each synthetic source in a separate process and check the common contract."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pyarrow.parquet as pq
from yoyoflot.contracts import ID_COLUMNS, SCHEMAS

ROOT = Path(__file__).resolve().parents[1]
expected = json.loads(
    (ROOT / "tests/fixtures/expected.json").read_text(encoding="utf-8")
)
reports = {}
for source, counts in expected["tables"].items():
    command = [
        sys.executable,
        "-m",
        "yoyoflot",
        "ingest",
        source,
        "--config",
        "configs/sources_fixture.yaml",
        "--data-dir",
        "tests/fixtures",
        "--output",
        "data/interim/demo",
        "--batch-size",
        "2",
        "--chunk-pages",
        "2",
    ]
    result = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if result.returncode:
        raise RuntimeError(result.stderr + result.stdout)
    report = json.loads(result.stdout)
    for table, count in counts.items():
        assert report["counts"][table] == count, (source, table, report)
    path = ROOT / "data/interim/demo" / source
    tables = {name: pq.read_table(path / (name + ".parquet")) for name in SCHEMAS}
    for name, table in tables.items():
        assert table.schema == SCHEMAS[name]
        if name in ID_COLUMNS:
            values = table[ID_COLUMNS[name]].to_pylist()
            assert len(values) == len(set(values)), (source, name, "duplicate ids")
    people = set(tables["person_observations"]["observation_id"].to_pylist())
    for name in ["identifiers", "travel_observations"]:
        assert set(tables[name]["observation_id"].to_pylist()) <= people
    identifiers = set(tables["identifiers"]["identifier_id"].to_pylist())
    assert {
        v
        for v in tables["travel_observations"]["loyalty_identifier_id"].to_pylist()
        if v
    } <= identifiers
    for record in tables["provenance"].to_pylist():
        table = record["entity_table"]
        assert record["entity_id"] in tables[table][ID_COLUMNS[table]].to_pylist()
    if "input_path" in report:
        report["input_path"] = str(Path(report["input_path"]).relative_to(ROOT))
    report["scope"] = "synthetic_fixture_only"
    reports[source] = report
out = ROOT / "docs/validation/fixture_runs.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(
    json.dumps(
        dict(
            python=sys.version.split()[0],
            scope="synthetic_fixture_only",
            reports=reports,
        ),
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
print("Validated all 7 synthetic sources; report:", out)
