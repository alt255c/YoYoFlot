from pathlib import Path

import pytest

import check_all_data as checker
from yoyoflot.readers import pointz_xml


def test_xml_keeps_current_parser_node_attached(tmp_path, monkeypatch):
    original = pointz_xml.etree.iterparse
    completed = []

    def guarded(*args, **kwargs):
        for event, node in original(*args, **kwargs):
            yield event, node
            if node.tag == "user":
                assert node.getparent() is not None
                completed.append(node)

    monkeypatch.setattr(pointz_xml.etree, "iterparse", guarded)
    path = tmp_path / "users.xml"
    path.write_text(
        "<PointzAggregatorUsers><user><name>A</name></user>"
        "<user><name>B</name></user></PointzAggregatorUsers>",
        encoding="utf-8",
    )
    list(pointz_xml.read(path, batch_size=1))
    assert len(completed) == 2


def test_native_worker_exit_is_reported_without_killing_supervisor(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(checker, "PROJECT_DIR", tmp_path)

    class Crashed:
        stdout = ["worker started\n"]

        def __init__(self, *args, **kwargs):
            assert kwargs["env"]["PYTHONIOENCODING"] == "utf-8"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def wait(self):
            return -1073740940

    monkeypatch.setattr(checker.subprocess, "Popen", Crashed)
    with pytest.raises(RuntimeError, match="0xC0000374"):
        checker.run_child("validate", output=str(tmp_path))
    # The Python supervisor is still alive and can record/run the next task.
    assert list((tmp_path / "data/verification/workers").glob("*.request.json"))


def test_worker_ingestion_and_validation_roundtrip(tmp_path):
    fixture = Path(__file__).parent / "fixtures/boarding.csv"
    output = tmp_path / "boarding_csv"
    result = checker.run_child(
        "ingest",
        source="boarding_csv",
        input=str(fixture.resolve()),
        output=str(output),
        options={"batch_size": 2, "encoding": "utf-8"},
        chunk_pages=2,
    )
    assert result["status"] == "complete"
    checked = checker.run_child("validate", output=str(output))
    assert checked["structural_status"] == "passed"
    assert checked["counts"] == result["counts"]


def test_supervisor_records_xml_crash_and_checks_later_sources(tmp_path, monkeypatch, capsys):
    import sys
    from yoyoflot.contracts import SCHEMAS

    root = tmp_path / "project"
    raw, output = root / "data/raw", root / "data/interim/real"
    raw.mkdir(parents=True)
    (root / "src/yoyoflot").mkdir(parents=True)
    (root / "configs").mkdir()
    (root / "configs/sirena_columns.yaml").write_text("test", encoding="utf-8")
    for name in checker.FILES.values():
        (raw / name).write_text("test", encoding="utf-8")
    monkeypatch.setattr(checker, "PROJECT_DIR", root)
    monkeypatch.setattr(checker, "DATA_DIR", raw)
    monkeypatch.setattr(checker, "OUTPUT_DIR", output)
    monkeypatch.setattr(checker, "CHECK_PDF", False)
    monkeypatch.setattr(sys, "path", list(sys.path))

    def run(operation, **parameters):
        if operation == "ingest":
            if parameters["source"] == "pointz_xml":
                raise RuntimeError("native failure 0xC0000374")
            path = Path(parameters["output"])
            path.mkdir(parents=True)
            checker.write_json(
                path / "source_report.json",
                {
                    "sha256": checker.sha256(parameters["input"]),
                    "status": "complete",
                    "counts": dict.fromkeys(SCHEMAS, 0),
                },
            )
        elif operation == "validate":
            return {"structural_status": "passed"}
        else:
            raise AssertionError(
                "Independent checks must be blocked for a failed source"
            )

    monkeypatch.setattr(checker, "run_child", run)
    assert checker.main() == 1
    console = capsys.readouterr().out
    saved = (root / "data/verification/verification_summary.txt").read_text(encoding="utf-8")
    assert console.rstrip() == saved.rstrip()
    assert "Traceback" not in console
    assert "Полная обработка" not in console
    assert "Traceback" in (root / "data/verification/verification.log").read_text(encoding="utf-8")
    report = checker.read_json(root / "data/verification/verification_report.json")
    assert report["sources"]["pointz_xml"]["status"] == "failed"
    for source in ["sirena_fixed", "skyteam_yaml", "boardingpass_xlsx"]:
        assert report["sources"][source]["status"] == "passed"
    assert not report["automatic_checks_passed"]
