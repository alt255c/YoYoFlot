import json
import os
import subprocess
import sys
from pathlib import Path

from yoyoflot.cli import main


def test_cli_help_and_unimplemented():
    result = subprocess.run(
        [sys.executable, "-m", "yoyoflot", "--help"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0 and "ingest" in result.stdout
    assert main(["train"]) == 2


def test_missing_source_nonzero(tmp_path):
    assert main(["ingest", "boarding_csv", "--path", str(tmp_path / "missing")]) == 1


def test_end_to_end_cli_export(tmp_path):
    fixture = Path(__file__).parent / "fixtures/boarding_data_test.csv"
    assert (
        main(
            [
                "ingest",
                "boarding_csv",
                "--path",
                str(fixture),
                "--output",
                str(tmp_path / "interim"),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "export",
                str(tmp_path / "interim/boarding_csv/identifiers.parquet"),
                "--output",
                str(tmp_path / "export"),
            ]
        )
        == 0
    )
    rows = [
        json.loads(x)
        for x in (tmp_path / "export/table.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert rows[0]["value"] == "0012 345678"
