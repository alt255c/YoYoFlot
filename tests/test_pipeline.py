"""T16 integration: immutable snapshots, resume, lineage and failure boundaries."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from yoyoflot import pipeline
from yoyoflot.normalize.locking import OutputLock
from yoyoflot.provenance import fingerprint

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def args(tmp_path):
    raw = tmp_path / "raw"
    shutil.copytree(ROOT / "tests/fixtures", raw)
    config = yaml.safe_load((ROOT / "configs/sources_fixture.yaml").read_text())
    config["sources"]["sirena_fixed"]["columns"] = str(raw / "sirena_columns.yaml")
    sources = tmp_path / "sources.yaml"
    sources.write_text(yaml.safe_dump(config), encoding="utf-8")
    normalization = tmp_path / "normalization.yaml"
    shutil.copyfile(ROOT / "configs/normalization.yaml", normalization)
    return dict(config=sources, data_dir=raw, work_dir=tmp_path / "work", output=tmp_path / "snapshot",
                normalization=normalization, resolution=ROOT / "configs/resolution.yaml",
                reference=ROOT / "data/reference/airports.csv", overrides=ROOT / "configs/airports_overrides.yaml",
                require_all=True, memory_limit="128MB")


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def no_transform(*_args, **_kwargs):
    raise AssertionError("A completed upstream stage must not be run again")


def test_full_seven_sources_snapshot_and_exact_reuse(args, monkeypatch):
    report, summary = pipeline.run(**args)
    assert report["scope"] == "internal_data_core" and not report["ui_ready"] and not report["model_built"]
    assert report["counts"]["observations"] == 9
    assert report["counts"]["travel_observations"] == 9
    assert report["counts"]["persons"] == 6 and report["counts"]["segments"] == 9
    assert report["acceptance"] == "pending_real_data_and_independent_review"
    assert report["configured_sources"] == sorted(pipeline.SOURCES)
    assert "Итог T16" in summary
    assert pipeline._verify_snapshot(args["output"], report["request_sha256"]) == report
    assert load(args["output"] / "input_manifest.json")["raw_files_verified"]
    assert (args["output"] / "reference/airports.csv").read_bytes() == args["reference"].read_bytes()
    assert (args["output"] / "sources/timetable_pdf/page_text.zip").is_file()
    before = (args["output"] / "run_manifest.json").read_bytes()
    for name in ("ingest", "build", "geography", "resolve", "audit"):
        monkeypatch.setattr(pipeline, name, no_transform)
    second, summary = pipeline.run(**args)
    assert second == report and "использован снимок: да" in summary
    assert (args["output"] / "run_manifest.json").read_bytes() == before
    assert load(args["work_dir"] / "last_run.json")["reused_snapshot"] is True


def test_interrupt_resumes_completed_stages_and_releases_lock(args, monkeypatch):
    geography = pipeline.geography

    def interrupt(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(pipeline, "geography", interrupt)
    with pytest.raises(KeyboardInterrupt):
        pipeline.run(**args)
    assert not args["output"].exists()
    state = load(args["work_dir"] / "pipeline_state.json")["stages"]
    assert state["canonical"]["status"] == "complete"
    assert state["geography"]["status"] == "interrupted"
    assert load(args["work_dir"] / "last_failure.json")["status"] == "interrupted"
    monkeypatch.setattr(pipeline, "geography", geography)
    monkeypatch.setattr(pipeline, "ingest", no_transform)
    monkeypatch.setattr(pipeline, "build", no_transform)
    pipeline.run(**args)
    actions = load(args["output"] / "timings.json")
    assert actions["canonical"]["action"] == "reused"
    assert all(actions["ingest:" + name]["action"] == "reused" for name in pipeline.SOURCES)
    assert actions["geography"]["action"] == "executed"


def test_normalization_change_only_invalidates_downstream_and_preserves_old_snapshot(args, monkeypatch):
    first, _ = pipeline.run(**args)
    old_output = args["output"]
    old_manifest = (old_output / "run_manifest.json").read_bytes()
    old_config = (old_output / "configs/normalization.yaml").read_bytes()
    with args["normalization"].open("a", encoding="utf-8") as fh:
        fh.write("\n# new version, same formulas\n")
    args["output"] = old_output.with_name("snapshot_v2")
    monkeypatch.setattr(pipeline, "ingest", no_transform)
    second, _ = pipeline.run(**args)
    assert first["request_sha256"] != second["request_sha256"]
    assert first["counts"] == second["counts"]
    actions = load(args["output"] / "timings.json")
    assert all(actions["ingest:" + name]["action"] == "reused" for name in pipeline.SOURCES)
    assert all(actions[name]["action"] == "executed" for name in ("canonical", "resolution", "quality"))
    # A comment changes the normalization version hash, but not travel rows.
    assert actions["geography"]["action"] == "reused"
    assert (old_output / "run_manifest.json").read_bytes() == old_manifest
    assert (old_output / "configs/normalization.yaml").read_bytes() == old_config
    pipeline._verify_snapshot(old_output, first["request_sha256"])


def test_same_size_same_mtime_raw_edit_is_detected(args):
    first, _ = pipeline.run(**args)
    path = args["data_dir"] / "boarding.csv"
    before = path.stat()
    original = path.read_bytes()
    changed = original.replace(b"SYNTHETIC", b"SYNTHETIX", 1)
    assert changed != original and len(changed) == len(original)
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    args["output"] = args["output"].with_name("changed_raw")
    second, _ = pipeline.run(**args)
    assert first["request_sha256"] != second["request_sha256"]
    actions = load(args["output"] / "timings.json")
    assert actions["ingest:boarding_csv"]["action"] == "executed"
    assert actions["ingest:forum_json"]["action"] == "reused"


def test_corrupt_cached_output_reexecutes_only_affected_reader(args, monkeypatch):
    pipeline.run(**args)
    (args["work_dir"] / "interim/boarding_csv/source_report.json").write_text("broken JSON")
    args["output"] = args["output"].with_name("repaired_cache")
    ingest = pipeline.ingest
    called = []

    def tracked(*params, **kwargs):
        called.append(params[2])
        return ingest(*params, **kwargs)

    monkeypatch.setattr(pipeline, "ingest", tracked)
    pipeline.run(**args)
    assert called == ["boarding_csv"]
    assert load(args["output"] / "timings.json")["canonical"]["action"] == "executed"


def test_existing_snapshot_refuses_new_request_and_tampering(args, monkeypatch):
    pipeline.run(**args)
    original = (args["output"] / "run_manifest.json").read_bytes()
    with pytest.raises(FileExistsError, match="another request"):
        pipeline.run(**{**args, "sample_size": 31})
    assert (args["output"] / "run_manifest.json").read_bytes() == original
    for name in ("ingest", "build", "geography", "resolve", "audit"):
        monkeypatch.setattr(pipeline, name, no_transform)
    path = args["output"] / "quality.md"
    path.write_text(path.read_text() + "\nchanged\n", encoding="utf-8")
    changed = path.read_bytes()
    with pytest.raises(ValueError, match="Snapshot checksum mismatch"):
        pipeline.run(**args)
    assert path.read_bytes() == changed


def test_processed_adoption_skips_all_expensive_transformations_and_raw_files(args, monkeypatch):
    first, _ = pipeline.run(**args)
    shutil.rmtree(args["data_dir"])
    for name in ("ingest", "build", "geography", "resolve"):
        monkeypatch.setattr(pipeline, name, no_transform)
    adopted = {**args, "from_processed": args["work_dir"] / "processed",
               "work_dir": args["work_dir"].with_name("adopt_work"),
               "output": args["output"].with_name("adopted")}
    second, _ = pipeline.run(**adopted)
    assert second["counts"] == first["counts"]
    assert second["input_mode"] == "processed"
    inputs = load(adopted["output"] / "input_manifest.json")
    assert inputs["raw_files_verified"] is False
    assert "raw_files_not_revalidated:processed_input" in second["limitations"]
    assert set(load(adopted["output"] / "timings.json")) == {"preflight", "adopt_processed", "quality", "publish"}


def test_processed_adoption_rejects_self_consistent_but_mixed_lineage(args):
    pipeline.run(**args)
    processed = args["work_dir"] / "processed"
    path = processed / "resolution_report.json"
    report = load(path)
    key = next(k for k in report["inputs"] if k.endswith("canonical_manifest.json"))
    report["inputs"][key] = "0" * 64
    path.write_text(json.dumps(report), encoding="utf-8")
    manifest = load(processed / "resolution_manifest.json")
    manifest["files"][path.name] = fingerprint(path)
    (processed / "resolution_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    adopted = {**args, "from_processed": processed, "work_dir": args["work_dir"].with_name("mixed_work"),
               "output": args["output"].with_name("mixed")}
    with pytest.raises(ValueError, match="another upstream snapshot"):
        pipeline.run(**adopted)
    assert not adopted["output"].exists()


def test_interim_input_requires_explicit_source_error_opt_in(args, monkeypatch):
    pipeline.run(**args)
    interim = args["work_dir"] / "interim"
    path = interim / "timetable_pdf/source_report.json"
    report = load(path)
    report.update(status="incomplete", excluded_pages=1)
    path.write_text(json.dumps(report), encoding="utf-8")
    monkeypatch.setattr(pipeline, "ingest", no_transform)
    adopted = {**args, "from_interim": interim, "work_dir": args["work_dir"].with_name("interim_work"),
               "output": args["output"].with_name("interim_snapshot")}
    with pytest.raises(ValueError, match="allow-source-errors"):
        pipeline.run(**adopted)
    assert not adopted["output"].exists()
    result, _ = pipeline.run(**{**adopted, "allow_source_errors": True})
    assert result["status"] == "complete_with_limitations"
    assert result["input_mode"] == "interim"
    assert "schedule_coverage_unconfirmed" in result["limitations"]
    assert not load(adopted["output"] / "input_manifest.json")["raw_files_verified"]


def test_source_error_opt_in_never_bypasses_quality_blockers(args, monkeypatch):
    monkeypatch.setattr(pipeline, "audit", lambda *_args: {"status": "failed", "blockers": ["orphan_reference"]})
    with pytest.raises(ValueError, match="T14 structural blockers"):
        pipeline.run(**{**args, "allow_source_errors": True})
    assert not args["output"].exists()
    assert load(args["work_dir"] / "last_failure.json")["phase"] == "quality"


def test_input_changed_during_run_prevents_publication(args, monkeypatch):
    audit = pipeline.audit

    def mutate(*params):
        report = audit(*params)
        path = args["data_dir"] / "boarding.csv"
        path.write_bytes(path.read_bytes().replace(b"SYNTHETIC", b"SYNTHETIX", 1))
        return report

    monkeypatch.setattr(pipeline, "audit", mutate)
    with pytest.raises(RuntimeError, match="Input changed during T16"):
        pipeline.run(**args)
    assert not args["output"].exists()
    assert load(args["work_dir"] / "last_failure.json")["phase"] == "publish"


def test_raw_changed_for_ingestion_and_restored_is_not_a_valid_snapshot(args, monkeypatch):
    ingest = pipeline.ingest

    def temporarily_changed(reader, path, name, *params, **kwargs):
        original = path.read_bytes()
        if name == "boarding_csv":
            path.write_bytes(original.replace(b"SYNTHETIC", b"SYNTHETIX", 1))
        try:
            return ingest(reader, path, name, *params, **kwargs)
        finally:
            path.write_bytes(original)

    monkeypatch.setattr(pipeline, "ingest", temporarily_changed)
    with pytest.raises(RuntimeError, match="Raw input changed before ingestion"):
        pipeline.run(**args)
    assert not args["output"].exists()
    assert load(args["work_dir"] / "pipeline_state.json")["stages"]["ingest:boarding_csv"]["status"] == "failed"
    monkeypatch.setattr(pipeline, "ingest", ingest)
    report, _ = pipeline.run(**args)
    assert report["counts"]["observations"] == 9


def test_work_lock_and_overlapping_directories_are_rejected(args):
    args["work_dir"].mkdir()
    with OutputLock(args["work_dir"] / ".t16.lock"):
        with pytest.raises(RuntimeError, match="другим процессом"):
            pipeline.run(**args)
    for output in (args["data_dir"] / "snapshot", args["work_dir"] / "snapshot"):
        with pytest.raises(ValueError, match="overlap"):
            pipeline.run(**{**args, "output": output})
    assert not args["output"].exists()


def test_pycharm_entry_point_from_another_working_directory(args, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_t16.py"), "--config", str(args["config"]),
         "--data-dir", str(args["data_dir"]), "--work-dir", str(args["work_dir"]),
         "--output", str(args["output"]), "--normalization-config", str(args["normalization"]),
         "--require-all", "-q"], cwd=tmp_path, capture_output=True, text=True,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert load(args["output"] / "run_manifest.json")["counts"]["observations"] == 9


def test_default_artifact_root_is_not_inside_installed_package(args, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(pipeline, "ROOT", tmp_path / "installed_package")
    report, _ = pipeline.run(**{**args, "output": None})
    destination = Path(load(args["work_dir"] / "last_run.json")["snapshot"])
    assert destination.parent == tmp_path / "artifacts"
    assert report["run_id"] == destination.name
    assert not (tmp_path / "installed_package").exists()


def test_cli_until_product_joins_the_real_contracts(args, tmp_path):
    """Fixtures verify integration only; production readiness uses supplied LFS data."""
    from yoyoflot.cli import main
    from yoyoflot.product.store import ProductStore
    output = tmp_path / 'product'
    flags = ['run', '--until', 'product', '--require-all', '--output', str(output),
             '--config', str(args['config']), '--data-dir', str(args['data_dir']),
             '--work-dir', str(args['work_dir']), '--artifacts-dir', str(tmp_path / 'artifacts'),
             '--normalization-config', str(args['normalization']), '--resolution-config', str(args['resolution']),
             '--reference', str(args['reference']), '--overrides', str(args['overrides']),
             '--product-config', str(ROOT / 'configs/product.yaml'), '-q']
    assert main(flags) == 0
    store = ProductStore(output)
    assert store.manifest['counts']['persons'] == 6
    assert store.manifest['counts']['segments'] == 9
    assert store.manifest['request']['origin'] == 'verified_t13'
    assert 'tables/segment_links.parquet' in store.manifest['files']
    assert store.top() == []  # insufficient calibration, never a fabricated top
