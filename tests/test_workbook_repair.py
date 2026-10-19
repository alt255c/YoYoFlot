"""Immutable coverage repair, proved against authored tiny XLSX reader bytes."""

from copy import deepcopy
import csv
import json
from pathlib import Path
import shutil
from zipfile import ZipFile

from openpyxl import Workbook
import pytest

from yoyoflot import pipeline
from yoyoflot.contracts import SCHEMAS
from yoyoflot.io import ingest
from yoyoflot.product import repair
from yoyoflot.provenance import fingerprint
from yoyoflot.readers import boardingpass_xlsx


SOURCE = "boardingpass_xlsx"
COVERAGE = "sources/boardingpass_xlsx/workbook_coverage.csv"
SOURCE_REPORT = "sources/boardingpass_xlsx/source_report.json"


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def bytes_by_name(root):
    return {path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*") if path.is_file()}


def refresh_core(root):
    manifest = load(root / "run_manifest.json")
    inventory = load(root / "input_manifest.json")
    manifest["request_sha256"] = pipeline._signature({
        "environment": load(root / "environment.json"), "mode": manifest["input_mode"],
        "options": manifest["options"],
        "inputs": {name: info["sha256"] for name, info in inventory["files"].items()},
    })
    manifest["files"] = {
        path.relative_to(root).as_posix(): pipeline._describe(path)
        for path in root.rglob("*") if path.is_file() and path != root / "run_manifest.json"
    }
    write(root / "run_manifest.json", manifest)


@pytest.fixture
def fixture(tmp_path):
    workbook = tmp_path / "authored.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "Passengers"
    sheet.append(["PassengerName", "FlightDate", "FlightNumber", "TicketNumber", "Destination"])
    sheet.append(["Authored Example", "2017-01-01", "AB001", "000123", "BBB"])
    book.create_sheet("Empty")
    book.save(workbook)
    book.close()
    raw = tmp_path / "boardingpass.zip"
    with ZipFile(raw, "w") as archive:
        archive.write(workbook, "authored.xlsx")
        archive.writestr("notes.txt", "Authored unit-test workbook, not production data.")
    fresh = tmp_path / "fresh" / SOURCE
    ingest(boardingpass_xlsx.read, raw, SOURCE, fresh, batch_size=10)

    core = tmp_path / "old_core"
    (core / "tables").mkdir(parents=True)
    (core / SOURCE_REPORT).parent.mkdir(parents=True)
    report = load(fresh / "source_report.json")
    assert report["status"] == "complete"
    assert report["error_severities"] == {}
    report.update(seconds=123.5, process_peak_rss_mib=7.0)
    write(core / SOURCE_REPORT, report)
    # The old core published only the CSV header; its data outputs are intact.
    (core / COVERAGE).write_text(
        (fresh / "workbook_coverage.csv").read_text(encoding="utf-8").splitlines()[0] + "\n",
        encoding="utf-8",
    )
    historical_reader = tmp_path / "historical_work" / "interim" / SOURCE
    report_key = str(historical_reader / "source_report.json")
    canonical_inputs = {
        str(historical_reader / (name + ".parquet")): fingerprint(fresh / (name + ".parquet"))
        for name in SCHEMAS
    }
    canonical_inputs[report_key] = fingerprint(core / SOURCE_REPORT)
    write(core / "tables/canonical_report.json", {
        "status": "complete", "version": "canonical:1.0", "inputs": canonical_inputs,
        "source_reports": {report_key: report}, "counts": report["counts"],
        "normalization_errors": 0, "source_error_severities": report["error_severities"],
    })
    shutil.copyfile(fresh / "person_observations.parquet", core / "tables/retained.parquet")
    write(core / "environment.json", pipeline._environment())
    write(core / "input_manifest.json", {
        "mode": "raw", "raw_files_verified": True,
        "files": {"raw:" + SOURCE: {"path": str(raw.resolve()), **pipeline._describe(raw)}},
    })
    write(core / "run_manifest.json", {
        "pipeline_version": pipeline.VERSION, "status": "complete", "run_id": core.name,
        "input_mode": "raw", "options": {"require_all": False, "allow_source_errors": False},
        "configured_sources": [SOURCE], "scope": "internal_data_core",
        "acceptance": "pending_real_data_and_independent_review", "limitations": [],
    })
    refresh_core(core)
    return {"core": core, "fresh": fresh, "raw": raw, "report_key": report_key,
            "historical_reader": historical_reader}


def repair_fixture(fixture, **kwargs):
    return repair.repair_workbook_coverage(fixture["core"], fixture["fresh"], fixture["raw"], **kwargs)


def test_repair_is_immutable_hardlinked_verified_and_idempotent(fixture):
    core, fresh = fixture["core"], fixture["fresh"]
    before = bytes_by_name(core)
    result = repair_fixture(fixture)
    output = Path(result["snapshot"])
    assert output != core and output.parent == core.parent
    assert result["run_id"] == output.name
    assert result["reused"] is False
    assert result["repair_sha256"] == load(output / "repair_manifest.json")["repair_sha256"]
    assert load(output / "run_manifest.json")["files"]["repair_manifest.json"]["sha256"] == fingerprint(output / "repair_manifest.json")
    assert (output / COVERAGE).read_bytes() == (fresh / "workbook_coverage.csv").read_bytes()
    assert bytes_by_name(core) == before
    for name in before.keys() - {COVERAGE, "run_manifest.json"}:
        assert (output / name).read_bytes() == before[name]
        assert (output / name).samefile(core / name)
    assert not (output / COVERAGE).samefile(core / COVERAGE)
    record = repair.verify_repair(output)
    assert record == load(output / "repair_manifest.json")
    published = bytes_by_name(output)
    second = repair_fixture(fixture)
    assert second == {**result, "reused": True}
    assert bytes_by_name(output) == published
    assert bytes_by_name(core) == before


@pytest.mark.parametrize("change", ["contents", "missing", "extra", "schema_description", "request"])
def test_old_snapshot_inventory_and_request_must_verify_before_repair(fixture, change):
    core = fixture["core"]
    if change == "contents":
        (core / "tables/canonical_report.json").write_text("{}", encoding="utf-8")
    elif change == "missing":
        (core / SOURCE_REPORT).unlink()
    elif change == "extra":
        (core / "unrecorded.txt").write_text("unexpected", encoding="utf-8")
    else:
        manifest = load(core / "run_manifest.json")
        if change == "schema_description":
            manifest["files"]["tables/retained.parquet"]["schema_sha256"] = "0" * 64
        else:
            manifest["request_sha256"] = "0" * 64
        write(core / "run_manifest.json", manifest)
    before = bytes_by_name(core)
    with pytest.raises((ValueError, FileNotFoundError)):
        repair_fixture(fixture)
    assert bytes_by_name(core) == before


@pytest.mark.parametrize("change", ["missing", "wrong_hash", "extra_sibling", "ambiguous_report"])
def test_canonical_report_must_bind_exact_six_reader_outputs(fixture, change):
    core = fixture["core"]
    path = core / "tables/canonical_report.json"
    canonical = load(path)
    key = str(fixture["historical_reader"] / "person_observations.parquet")
    if change == "missing":
        del canonical["inputs"][key]
    elif change == "wrong_hash":
        canonical["inputs"][key] = "0" * 64
    elif change == "extra_sibling":
        canonical["inputs"][str(fixture["historical_reader"] / "unexpected.parquet")] = "0" * 64
    else:
        other_key = str(core.parent / "another_work" / SOURCE / "source_report.json")
        canonical["inputs"][other_key] = canonical["inputs"][fixture["report_key"]]
        canonical["source_reports"][other_key] = deepcopy(canonical["source_reports"][fixture["report_key"]])
    write(path, canonical)
    refresh_core(core)
    with pytest.raises(ValueError):
        repair_fixture(fixture)


@pytest.mark.parametrize("field,value", [
    ("source_id", "different"), ("sha256", "0" * 64), ("contract_version", "unsupported"),
    ("reader_version", "unsupported"), ("options", {"batch_size": 99}), ("status", "failed"),
    ("counts", {}), ("input_counts", {}), ("error_severities", {"error": 1}), ("input_path", "/another.zip"),
])
def test_fresh_report_must_match_every_stable_old_report_field(fixture, field, value):
    path = fixture["fresh"] / "source_report.json"
    report = load(path)
    assert report.get(field) != value
    report[field] = value
    write(path, report)
    with pytest.raises(ValueError):
        repair_fixture(fixture)


@pytest.mark.parametrize("change", ["zip", "input_manifest", "original_report", "reader_environment"])
def test_raw_source_report_and_reader_environment_evidence_cannot_be_substituted(fixture, change):
    core = fixture["core"]
    if change == "zip":
        with ZipFile(fixture["raw"], "a") as archive:
            archive.writestr("new.txt", "changed raw archive")
    elif change == "input_manifest":
        path = core / "input_manifest.json"
        inventory = load(path)
        inventory["files"]["raw:" + SOURCE]["sha256"] = "0" * 64
        write(path, inventory)
        refresh_core(core)
    elif change == "original_report":
        path = core / SOURCE_REPORT
        report = load(path)
        report["reader_version"] = "changed"
        write(path, report)
        refresh_core(core)
    else:
        path = core / "environment.json"
        environment = load(path)
        environment["source_files"]["readers/boardingpass_xlsx.py"] = "0" * 64
        environment["code_sha256"] = pipeline._signature(environment["source_files"])
        write(path, environment)
        refresh_core(core)
    with pytest.raises(ValueError):
        repair_fixture(fixture)


def test_corrupt_fresh_reader_parquet_is_rejected(fixture):
    with (fixture["fresh"] / "person_observations.parquet").open("ab") as stream:
        stream.write(b"corruption")
    with pytest.raises(ValueError):
        repair_fixture(fixture)


@pytest.mark.parametrize("change", ["missing_sheet", "wrong_sheet"])
def test_fresh_coverage_must_account_for_actual_workbook_sheets(fixture, change):
    path = fixture["fresh"] / "workbook_coverage.csv"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    if change == "missing_sheet":
        lines = [line for line in lines if ",Empty," not in line]
    else:
        lines = [line.replace(",Empty,", ",Incorrect,") for line in lines]
    path.write_text("".join(lines), encoding="utf-8")
    with pytest.raises(ValueError):
        repair_fixture(fixture)


def test_explicit_output_requires_derived_identity_and_preserves_existing_destination(fixture):
    arbitrary = fixture["core"].parent / "arbitrary_name"
    arbitrary.mkdir()
    sentinel = arbitrary / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises((ValueError, FileExistsError)):
        repair_fixture(fixture, output=arbitrary)
    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_repaired_snapshot_rejects_inventory_corruption_on_verify_and_idempotent_reuse(fixture):
    result = repair_fixture(fixture)
    output = Path(result["snapshot"])
    # Replace the repaired CSV; do not mutate a hardlinked original artifact.
    path = output / COVERAGE
    path.write_text("corrupt\n", encoding="utf-8")
    with pytest.raises(ValueError):
        repair.verify_repair(output)
    with pytest.raises(ValueError):
        repair_fixture(fixture)


@pytest.mark.parametrize("target", ["original_report", "fresh_coverage"])
def test_symlinked_evidence_is_rejected(fixture, target):
    path = fixture["core"] / SOURCE_REPORT if target == "original_report" else fixture["fresh"] / "workbook_coverage.csv"
    outside = fixture["core"].parent / (target + ".outside")
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(ValueError):
        repair_fixture(fixture)


def test_repair_evidence_verifies_without_original_core_raw_or_fresh_reader(fixture):
    result = repair_fixture(fixture)
    output = Path(result["snapshot"])
    expected = repair.verify_repair(output)
    shutil.rmtree(fixture["core"])
    shutil.rmtree(fixture["fresh"])
    fixture["raw"].unlink()
    assert repair.verify_repair(output) == expected


def test_publication_failure_cleans_staging_and_preserves_original_bytes(fixture, monkeypatch):
    core = fixture["core"]
    before = bytes_by_name(core)
    link = repair.os.link
    calls = []

    def fail_second_link(source, target):
        calls.append(str(source))
        if len(calls) == 2:
            raise OSError("Authored publication failure")
        return link(source, target)

    monkeypatch.setattr(repair.os, "link", fail_second_link)
    with pytest.raises(OSError, match="Authored publication failure"):
        repair_fixture(fixture)
    assert len(calls) == 2
    assert bytes_by_name(core) == before
    assert list(core.parent.glob(".coverage-repair-*")) == []
    assert list(core.parent.glob("data-*-coverage-*")) == []


def rebind_repair_identity(output, record):
    """Refresh all enclosing identities so semantic tests cannot stop at SHA checks."""
    record["repair_sha256"] = pipeline._signature({
        key: value for key, value in record.items() if key not in {"repair_sha256", "run_id"}
    })
    record["run_id"] = (
        "data-" + record["original_request_sha256"][:20] + "-coverage-" + record["repair_sha256"][:20]
    )
    write(output / "repair_manifest.json", record)
    manifest = load(output / "run_manifest.json")
    manifest["run_id"] = record["run_id"]
    manifest["repair"]["repair_sha256"] = record["repair_sha256"]
    manifest["files"] = {
        path.relative_to(output).as_posix(): pipeline._describe(path)
        for path in output.rglob("*") if path.is_file() and path != output / "run_manifest.json"
    }
    write(output / "run_manifest.json", manifest)
    rebound = output.with_name(record["run_id"])
    if rebound != output:
        output.rename(rebound)
    return rebound


def test_repair_reader_hash_proof_is_checked_after_all_outer_identities_are_refreshed(fixture):
    result = repair_fixture(fixture)
    output = Path(result["snapshot"])
    record = load(output / "repair_manifest.json")
    record["fresh_reader_files"]["person_observations.parquet"]["sha256"] = "0" * 64
    output = rebind_repair_identity(output, record)
    with pytest.raises(ValueError, match="reader Parquet proof mismatch"):
        repair.verify_repair(output)


def test_repair_cannot_change_retained_bytes_with_self_consistent_outer_inventory(fixture):
    result = repair_fixture(fixture)
    output = Path(result["snapshot"])
    path = output / "tables/canonical_report.json"
    canonical = load(path)
    canonical["counts"]["person_observations"] = 999
    path.unlink()  # Break the hard link before changing the repaired copy.
    write(path, canonical)
    output = rebind_repair_identity(output, load(output / "repair_manifest.json"))
    with pytest.raises(ValueError, match="changed a retained artifact"):
        repair.verify_repair(output)
    assert load(fixture["core"] / "tables/canonical_report.json")["counts"]["person_observations"] == 1


@pytest.mark.parametrize("location", ["inside_old_core", "ancestor_old_core", "inside_fresh", "inside_raw_zip"])
def test_output_overlaps_are_rejected_before_mkdir_or_staging(fixture, monkeypatch, location):
    core, fresh, raw = fixture["core"], fixture["fresh"], fixture["raw"]
    outputs = {
        "inside_old_core": core / "new" / "snapshot",
        "ancestor_old_core": core.parent,
        "inside_fresh": fresh / "new" / "snapshot",
        "inside_raw_zip": raw / "new" / "snapshot",
    }
    before_bytes = bytes_by_name(core.parent)
    before_inventory = {path.relative_to(core.parent).as_posix() for path in core.parent.rglob("*")}

    def forbidden_creation(*_args, **_kwargs):
        raise AssertionError("Overlapping output must be rejected before filesystem creation")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", forbidden_creation)
        patch.setattr(repair.tempfile, "mkdtemp", forbidden_creation)
        with pytest.raises(ValueError, match="must not overlap"):
            repair_fixture(fixture, output=outputs[location])
    assert bytes_by_name(core.parent) == before_bytes
    assert {path.relative_to(core.parent).as_posix() for path in core.parent.rglob("*")} == before_inventory


def test_complete_old_coverage_with_different_csv_serialization_cannot_be_repaired(fixture):
    core, fresh = fixture["core"], fixture["fresh"]
    with (fresh / "workbook_coverage.csv").open(encoding="utf-8", newline="") as stream:
        reader = csv.DictReader(stream)
        fieldnames, rows = reader.fieldnames, list(reader)
    with (core / COVERAGE).open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames, quoting=csv.QUOTE_ALL, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    assert (core / COVERAGE).read_bytes() != (fresh / "workbook_coverage.csv").read_bytes()
    refresh_core(core)
    before_core, before_fresh = bytes_by_name(core), bytes_by_name(fresh)
    before_inventory = {path.relative_to(core.parent).as_posix() for path in core.parent.rglob("*")}
    with pytest.raises(ValueError, match="Original coverage is complete"):
        repair_fixture(fixture)
    assert bytes_by_name(core) == before_core
    assert bytes_by_name(fresh) == before_fresh
    assert {path.relative_to(core.parent).as_posix() for path in core.parent.rglob("*")} == before_inventory
