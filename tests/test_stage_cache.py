"""Stage cache identity and migration, using only tiny text artifacts."""

from copy import deepcopy
import json
import os

import pytest
import pyarrow as pa
import pyarrow.parquet as pq

from yoyoflot import pipeline


COMMON_READER_FILES = {
    "__init__.py", "contracts.py", "io.py", "provenance.py",
    "readers/__init__.py", "readers/common.py",
}
CORE_STAGES = ("canonical", "geography", "resolution", "quality")
CORE_OWN_MODULES = {
    "canonical": "normalize/canonical.py",
    "geography": "normalize/geography.py",
    "resolution": "resolve/pipeline.py",
    "quality": "quality.py",
}
COMMON_CORE_FILES = {
    "__init__.py", "contracts.py", "io.py", "provenance.py",
    "normalize/__init__.py", "normalize/locking.py",
}
CANONICAL_CODE = COMMON_CORE_FILES | {"normalize/canonical.py", "normalize/values.py"}
GEOGRAPHY_CODE = COMMON_CORE_FILES | {
    "normalize/geography.py", "normalize/airports.py", "resolve/__init__.py", "resolve/geography_validation.py",
}
EXPECTED_CORE_CODE = {
    "canonical": CANONICAL_CODE,
    "geography": GEOGRAPHY_CODE,
    "resolution": CANONICAL_CODE | GEOGRAPHY_CODE | {
        "resolve/__init__.py", "resolve/pipeline.py", "resolve/common.py", "resolve/blocking.py",
        "resolve/matching.py", "resolve/review.py", "resolve/segments.py", "resolve/geography_validation.py",
    },
    "quality": CANONICAL_CODE | {"quality.py"},
}


class QuietLog:
    def event(self, *_args, **_kwargs):
        pass

    def message(self, *_args, **_kwargs):
        pass


@pytest.fixture
def environment():
    return pipeline._environment()


def changed_code(environment, filename):
    changed = deepcopy(environment)
    changed["source_files"][filename] = "changed:" + changed["source_files"][filename]
    changed["code_sha256"] = pipeline._signature(changed["source_files"])
    return changed


@pytest.fixture
def stage(tmp_path):
    raw = tmp_path / "raw.txt"
    raw.write_text("raw-a", encoding="utf-8")
    output = tmp_path / "output.txt"
    outputs = [output]
    calls = []

    def run(environment, *, name="ingest:boardingpass_xlsx", options=None, inputs=None):
        runner = pipeline._Stages(tmp_path, environment, QuietLog())

        def action():
            calls.append(name)
            for path in outputs:
                path.write_text("valid", encoding="utf-8")

        result = runner.run(
            name, pipeline._inventory([raw]) if inputs is None else inputs,
            {"chunk_size": 7} if options is None else options,
            action, lambda: outputs,
        )
        return runner, result

    run.raw, run.output, run.outputs, run.calls = raw, output, outputs, calls
    return run


@pytest.mark.parametrize("source", pipeline.SOURCES)
def test_reader_environment_has_only_its_static_code_dependencies(environment, source):
    scoped = pipeline._stage_environment("ingest:" + source, environment)
    expected = COMMON_READER_FILES | {f"readers/{source}.py"}
    if source == "boardingpass_xlsx":
        expected.add("readers/xlsx_stream.py")
    elif source == "timetable_pdf":
        expected.add("readers/pdf_reference.py")
    assert set(scoped["source_files"]) == expected
    assert scoped["source_files"] == {
        name: environment["source_files"][name] for name in expected
    }
    assert scoped["code_sha256"] == pipeline._signature(scoped["source_files"])
    assert set(scoped) == set(environment) | {"stage_cache_version", "reader"}
    for name, value in environment.items():
        if name not in {"source_files", "code_sha256"}:
            assert scoped[name] == value


@pytest.mark.parametrize("name", ["adopt_processed", "unknown"])
def test_other_stages_keep_full_environment(environment, name):
    assert pipeline._stage_environment(name, environment) == environment


def test_unknown_reader_is_rejected(environment):
    with pytest.raises(ValueError, match="Unknown ingestion stage"):
        pipeline._stage_environment("ingest:unknown", environment)


def test_scoping_does_not_mutate_full_environment(environment):
    original = deepcopy(environment)
    pipeline._stage_environment("ingest:boardingpass_xlsx", environment)
    assert environment == original


@pytest.mark.parametrize("filename", sorted(COMMON_READER_FILES | {
    "readers/boardingpass_xlsx.py", "readers/xlsx_stream.py",
}))
def test_incomplete_reader_code_inventory_fails_closed(environment, filename):
    del environment["source_files"][filename]
    environment["code_sha256"] = pipeline._signature(environment["source_files"])
    with pytest.raises(ValueError, match="Missing stage implementation evidence"):
        pipeline._stage_environment("ingest:boardingpass_xlsx", environment)


@pytest.mark.parametrize("source_files", [None, [], "invalid"])
def test_malformed_source_inventory_fails_closed(environment, source_files):
    environment["source_files"] = source_files
    with pytest.raises(ValueError, match="Invalid stage source environment"):
        pipeline._stage_environment("ingest:boardingpass_xlsx", environment)


def test_incorrect_source_inventory_hash_fails_closed(environment):
    environment["code_sha256"] = "invalid"
    with pytest.raises(ValueError, match="Invalid stage source environment"):
        pipeline._stage_environment("ingest:boardingpass_xlsx", environment)


def test_cache_version_changes_reader_identity(environment, monkeypatch):
    before = pipeline._stage_environment("ingest:boardingpass_xlsx", environment)
    monkeypatch.setattr(pipeline, "STAGE_CACHE_VERSION", pipeline.STAGE_CACHE_VERSION + ".next")
    after = pipeline._stage_environment("ingest:boardingpass_xlsx", environment)
    assert after != before


def test_completed_stage_persists_scoped_environment_and_reuses_verified_outputs(stage, environment):
    first, outputs = stage(environment)
    saved = json.loads(first.path.read_text(encoding="utf-8"))["stages"]["ingest:boardingpass_xlsx"]
    assert saved["environment"] == pipeline._stage_environment("ingest:boardingpass_xlsx", environment)
    second, reused = stage(environment)
    assert stage.calls == ["ingest:boardingpass_xlsx"]
    assert reused == outputs
    assert second.timings["ingest:boardingpass_xlsx"]["action"] == "reused"


@pytest.mark.parametrize("filename", ["readers/timetable_pdf.py", "readers/pdf_reference.py"])
def test_pdf_code_change_reuses_boardingpass_but_invalidates_pdf(stage, environment, filename):
    stage(environment)
    stage(environment, name="ingest:timetable_pdf")
    updated = changed_code(environment, filename)
    boarding, _ = stage(updated)
    timetable, _ = stage(updated, name="ingest:timetable_pdf")
    assert boarding.timings["ingest:boardingpass_xlsx"]["action"] == "reused"
    assert timetable.timings["ingest:timetable_pdf"]["action"] == "executed"
    assert stage.calls == ["ingest:boardingpass_xlsx", "ingest:timetable_pdf", "ingest:timetable_pdf"]


@pytest.mark.parametrize("filename", sorted(COMMON_READER_FILES | {
    "readers/boardingpass_xlsx.py", "readers/xlsx_stream.py",
}))
def test_own_or_shared_reader_code_change_invalidates_cache(stage, environment, filename):
    stage(environment)
    runner, _ = stage(changed_code(environment, filename))
    assert stage.calls == ["ingest:boardingpass_xlsx"] * 2
    assert runner.timings["ingest:boardingpass_xlsx"]["action"] == "executed"


@pytest.mark.parametrize("change", [
    "raw", "options", "corrupt_output", "missing_output", "output_inventory", "dependency", "python",
])
def test_changed_inputs_options_outputs_or_runtime_invalidate_cache(stage, environment, change):
    stage(environment)
    options = None
    if change in {"raw", "corrupt_output"}:
        path = stage.raw if change == "raw" else stage.output
        before = path.stat()
        path.write_text("raw-b" if change == "raw" else "wrong", encoding="utf-8")
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        assert path.stat().st_size == before.st_size
        assert path.stat().st_mtime_ns == before.st_mtime_ns
    elif change == "options":
        options = {"chunk_size": 8}
    elif change == "missing_output":
        stage.output.unlink()
    elif change == "output_inventory":
        stage.outputs.append(stage.output.with_name("extra.txt"))
    elif change == "dependency":
        environment["dependencies"]["openpyxl"] = "changed"
    elif change == "python":
        environment["python"] = "changed"
    runner, _ = stage(environment, options=options)
    assert stage.calls == ["ingest:boardingpass_xlsx"] * 2
    assert runner.timings["ingest:boardingpass_xlsx"]["action"] == "executed"
    assert stage.output.read_text(encoding="utf-8") == "valid"


@pytest.mark.parametrize("filename", ["readers/timetable_pdf.py", "normalize/geography.py"])
def test_unrelated_reader_or_geography_change_reuses_canonical_stage(stage, environment, filename):
    stage(environment, name="canonical")
    runner, _ = stage(changed_code(environment, filename), name="canonical")
    assert stage.calls == ["canonical"]
    assert runner.timings["canonical"]["action"] == "reused"


@pytest.fixture
def legacy(tmp_path, environment):
    old_environment = deepcopy(environment)
    old_environment["source_files"]["pipeline.py"] = pipeline.LEGACY_INGEST_PIPELINE_SHA256
    old_environment["code_sha256"] = pipeline._signature(old_environment["source_files"])
    requests, records, outputs = {}, {}, {}
    for source in pipeline.SOURCES:
        name = "ingest:" + source
        path = tmp_path / (source + ".txt")
        path.write_text("valid", encoding="utf-8")
        outputs[name] = path
        requests[name] = {"inputs": {"raw_sha256": "raw-content"}, "options": {"chunk_size": 7}}
        records[name] = {
            "status": "complete",
            "signature": pipeline._signature({"environment": old_environment, **requests[name]}),
            "outputs": pipeline._inventory([path]),
        }
    state = {"version": pipeline.VERSION, "stages": records}
    path = tmp_path / "pipeline_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    return old_environment, requests, outputs, path


def run_legacy_reader(runner, requests, outputs, *, options=None):
    name = "ingest:boardingpass_xlsx"
    calls = []

    def action():
        calls.append(name)
        outputs[name].write_text("valid", encoding="utf-8")

    result = runner.run(
        name, requests[name]["inputs"],
        requests[name]["options"] if options is None else options,
        action, lambda: [outputs[name]],
    )
    return calls, result


def test_verified_legacy_cache_migrates_after_unrelated_pdf_edit(legacy, environment):
    old_environment, requests, outputs, path = legacy
    current = changed_code(environment, "readers/timetable_pdf.py")
    runner = pipeline._Stages(path.parent, current, QuietLog())
    runner.attach_legacy_environment(old_environment, requests)
    calls, result = run_legacy_reader(runner, requests, outputs)
    assert calls == []
    assert result == pipeline._inventory([outputs["ingest:boardingpass_xlsx"]])
    assert runner.timings["ingest:boardingpass_xlsx"]["action"] == "reused"
    saved = json.loads(path.read_text(encoding="utf-8"))["stages"]["ingest:boardingpass_xlsx"]
    assert saved["environment"] == pipeline._stage_environment("ingest:boardingpass_xlsx", current)
    assert saved["signature"] == pipeline._signature({
        "environment": saved["environment"], **requests["ingest:boardingpass_xlsx"],
    })


@pytest.mark.parametrize("change", ["own_reader", "output", "options", "raw"])
def test_legacy_migration_revalidates_reader_inputs_options_and_outputs(legacy, environment, change):
    old_environment, requests, outputs, path = legacy
    current = changed_code(environment, "readers/boardingpass_xlsx.py") if change == "own_reader" else environment
    runner = pipeline._Stages(path.parent, current, QuietLog())
    runner.attach_legacy_environment(old_environment, requests)
    if change == "output":
        outputs["ingest:boardingpass_xlsx"].write_text("wrong", encoding="utf-8")
    if change == "raw":
        requests = deepcopy(requests)
        requests["ingest:boardingpass_xlsx"]["inputs"]["raw_sha256"] = "changed"
    options = {"chunk_size": 8} if change == "options" else None
    calls, _ = run_legacy_reader(runner, requests, outputs, options=options)
    assert calls == ["ingest:boardingpass_xlsx"]
    assert runner.timings["ingest:boardingpass_xlsx"]["action"] == "executed"


def test_legacy_cache_without_environment_evidence_is_recomputed(legacy, environment):
    _old_environment, requests, outputs, path = legacy
    runner = pipeline._Stages(path.parent, environment, QuietLog())
    calls, _ = run_legacy_reader(runner, requests, outputs)
    assert calls == ["ingest:boardingpass_xlsx"]


@pytest.mark.parametrize("change", ["signature", "missing_request", "code_hash", "unknown_pipeline"])
def test_invalid_legacy_evidence_is_rejected_without_partial_state_changes(legacy, environment, change):
    old_environment, requests, _outputs, path = legacy
    runner = pipeline._Stages(path.parent, environment, QuietLog())
    if change == "signature":
        runner.state["stages"]["ingest:timetable_pdf"]["signature"] = "invalid"
    elif change == "missing_request":
        del requests["ingest:timetable_pdf"]
    elif change == "code_hash":
        old_environment["code_sha256"] = "invalid"
    elif change == "unknown_pipeline":
        old_environment = changed_code(old_environment, "pipeline.py")
        for name in requests:
            runner.state["stages"][name]["signature"] = pipeline._signature({
                "environment": old_environment, **requests[name],
            })
    before_state, before_disk = deepcopy(runner.state), path.read_bytes()
    with pytest.raises(ValueError):
        runner.attach_legacy_environment(old_environment, requests)
    assert runner.state == before_state
    assert path.read_bytes() == before_disk


@pytest.mark.parametrize("name", CORE_STAGES)
def test_core_environment_has_its_static_transitive_code_dependencies(environment, name):
    original = deepcopy(environment)
    scoped = pipeline._stage_environment(name, environment)
    assert set(scoped["source_files"]) == EXPECTED_CORE_CODE[name]
    assert scoped["source_files"] == {
        filename: environment["source_files"][filename] for filename in EXPECTED_CORE_CODE[name]
    }
    assert scoped["code_sha256"] == pipeline._signature(scoped["source_files"])
    assert scoped["stage_cache_version"] == pipeline.CORE_STAGE_CACHE_VERSION
    assert scoped["stage"] == name
    assert set(scoped) == set(environment) | {"stage_cache_version", "stage"}
    for key, value in environment.items():
        if key not in {"source_files", "code_sha256"}:
            assert scoped[key] == value
    assert environment == original


@pytest.mark.parametrize("name", CORE_STAGES)
@pytest.mark.parametrize("which", ["own", "shared"])
def test_missing_current_core_code_evidence_fails_closed(environment, name, which):
    filename = CORE_OWN_MODULES[name] if which == "own" else "contracts.py"
    del environment["source_files"][filename]
    environment["code_sha256"] = pipeline._signature(environment["source_files"])
    with pytest.raises(ValueError):
        pipeline._stage_environment(name, environment)


@pytest.mark.parametrize("name", CORE_STAGES)
def test_core_cache_version_changes_identity_without_changing_ingestion(environment, monkeypatch, name):
    before = pipeline._stage_environment(name, environment)
    reader_before = pipeline._stage_environment("ingest:boardingpass_xlsx", environment)
    monkeypatch.setattr(pipeline, "CORE_STAGE_CACHE_VERSION", pipeline.CORE_STAGE_CACHE_VERSION + ".next")
    assert pipeline._stage_environment(name, environment) != before
    assert pipeline._stage_environment("ingest:boardingpass_xlsx", environment) == reader_before


@pytest.mark.parametrize("name", CORE_STAGES)
def test_core_cache_persists_inputs_options_and_scoped_environment(stage, environment, name):
    runner, outputs = stage(environment, name=name)
    saved = json.loads(runner.path.read_text(encoding="utf-8"))["stages"][name]
    assert saved["inputs"] == pipeline._inventory([stage.raw])
    assert saved["options"] == {"chunk_size": 7}
    assert saved["environment"] == pipeline._stage_environment(name, environment)
    assert saved["outputs"] == outputs
    second, reused = stage(environment, name=name)
    assert stage.calls == [name]
    assert second.timings[name]["action"] == "reused"
    assert reused == outputs


@pytest.mark.parametrize("name", CORE_STAGES)
@pytest.mark.parametrize("change", ["own_code", "shared_code", "input", "config", "options", "output", "inventory"])
def test_core_stage_invalidates_changed_dependencies(stage, environment, name, change):
    inputs = {"upstream": pipeline._inventory([stage.raw]), "config_sha256": "config-a"}
    options = {"chunk_size": 7}
    stage(environment, name=name, inputs=inputs, options=options)
    if change in {"own_code", "shared_code"}:
        filename = CORE_OWN_MODULES[name] if change == "own_code" else "contracts.py"
        environment = changed_code(environment, filename)
    elif change == "input":
        stage.raw.write_text("raw-b", encoding="utf-8")
        inputs["upstream"] = pipeline._inventory([stage.raw])
    elif change == "config":
        inputs["config_sha256"] = "config-b"
    elif change == "options":
        options["chunk_size"] = 8
    elif change == "output":
        stage.output.write_text("wrong", encoding="utf-8")
    elif change == "inventory":
        stage.outputs.append(stage.output.with_name("added.txt"))
    runner, _ = stage(environment, name=name, inputs=inputs, options=options)
    assert stage.calls == [name, name]
    assert runner.timings[name]["action"] == "executed"


@pytest.mark.parametrize("name", CORE_STAGES)
@pytest.mark.parametrize("field,value", [("schema_sha256", "invalid"), ("rows", 999)])
def test_core_stage_revalidates_cached_parquet_schema_and_row_counts(tmp_path, environment, name, field, value):
    output = tmp_path / "output.parquet"
    calls = []

    def action():
        calls.append(name)
        pq.write_table(pa.table({"value": [1]}), output)

    runner = pipeline._Stages(tmp_path, environment, QuietLog())
    runner.run(name, {}, {}, action, lambda: [output])
    state = json.loads(runner.path.read_text(encoding="utf-8"))
    state["stages"][name]["outputs"][str(output.resolve())][field] = value
    runner.path.write_text(json.dumps(state), encoding="utf-8")
    resumed = pipeline._Stages(tmp_path, environment, QuietLog())
    resumed.run(name, {}, {}, action, lambda: [output])
    assert calls == [name, name]
    assert resumed.timings[name]["action"] == "executed"


@pytest.fixture
def legacy_core(tmp_path, environment):
    def seed(name="canonical", *, legacy_environment=None):
        old_environment = deepcopy(environment if legacy_environment is None else legacy_environment)
        old_environment["source_files"]["pipeline.py"] = pipeline.LEGACY_CORE_PIPELINE_SHA256
        old_environment["code_sha256"] = pipeline._signature(old_environment["source_files"])
        output = tmp_path / (name + ".txt")
        output.write_text("valid", encoding="utf-8")
        request = {"inputs": {"upstream_sha256": "input-a", "config_sha256": "config-a"}, "options": {"chunk_size": 7}}
        record = {
            "status": "complete", "environment": old_environment,
            "signature": pipeline._signature({"environment": old_environment, **request}),
            "outputs": pipeline._inventory([output]),
        }
        path = tmp_path / "pipeline_state.json"
        path.write_text(json.dumps({"version": pipeline.VERSION, "stages": {name: record}}), encoding="utf-8")
        return old_environment, request, output, path

    return seed


def run_legacy_core(runner, name, request, output, calls):
    def action():
        calls.append(name)
        output.write_text("valid", encoding="utf-8")

    return runner.run(name, request["inputs"], request["options"], action, lambda: [output])


@pytest.mark.parametrize("name", CORE_STAGES)
def test_core_legacy_cache_migrates_from_recorded_environment_and_persists_request(legacy_core, environment, name):
    _old_environment, request, output, path = legacy_core(name)
    current = changed_code(environment, "readers/timetable_pdf.py")
    runner = pipeline._Stages(path.parent, current, QuietLog())
    calls = []
    result = run_legacy_core(runner, name, request, output, calls)
    assert calls == []
    assert result == pipeline._inventory([output])
    assert runner.timings[name]["action"] == "reused"
    saved = json.loads(path.read_text(encoding="utf-8"))["stages"][name]
    assert saved["environment"] == pipeline._stage_environment(name, current)
    assert saved["inputs"] == request["inputs"]
    assert saved["options"] == request["options"]
    assert saved["signature"] == pipeline._signature({"environment": saved["environment"], **request})
    resumed = pipeline._Stages(path.parent, current, QuietLog())
    run_legacy_core(resumed, name, request, output, calls)
    assert calls == []


@pytest.mark.parametrize("status", ["complete", "running"])
def test_new_dependency_recomputes_legacy_geography_without_blocking_resume(legacy_core, environment, status):
    old_environment = deepcopy(environment)
    del old_environment["source_files"]["resolve/geography_validation.py"]
    _old_environment, request, output, path = legacy_core("geography", legacy_environment=old_environment)
    state = json.loads(path.read_text(encoding="utf-8"))
    state["stages"]["geography"]["status"] = status
    path.write_text(json.dumps(state), encoding="utf-8")
    runner = pipeline._Stages(path.parent, environment, QuietLog())
    calls = []
    run_legacy_core(runner, "geography", request, output, calls)
    assert calls == ["geography"]
    assert runner.timings["geography"]["action"] == "executed"


@pytest.mark.parametrize("change", [
    "signature", "environment_hash", "unknown_pipeline", "missing_code", "own_code", "shared_code",
    "input", "config", "options", "output", "inventory", "incomplete",
])
def test_core_legacy_migration_rejects_changed_or_unverifiable_evidence(legacy_core, environment, change):
    old_environment, request, output, path = legacy_core()
    state = json.loads(path.read_text(encoding="utf-8"))
    record = state["stages"]["canonical"]
    if change == "signature":
        record["signature"] = "invalid"
    elif change == "environment_hash":
        record["environment"]["code_sha256"] = "invalid"
    elif change in {"unknown_pipeline", "missing_code"}:
        if change == "unknown_pipeline":
            old_environment["source_files"]["pipeline.py"] = "unapproved"
        else:
            del old_environment["source_files"]["normalize/canonical.py"]
        old_environment["code_sha256"] = pipeline._signature(old_environment["source_files"])
        record["environment"] = old_environment
        record["signature"] = pipeline._signature({"environment": old_environment, **request})
    elif change in {"own_code", "shared_code"}:
        environment = changed_code(environment, "normalize/canonical.py" if change == "own_code" else "contracts.py")
    elif change in {"input", "config"}:
        request["inputs"]["upstream_sha256" if change == "input" else "config_sha256"] = "changed"
    elif change == "options":
        request["options"]["chunk_size"] = 8
    elif change == "output":
        output.write_text("wrong", encoding="utf-8")
    elif change == "inventory":
        record["outputs"][str(output.with_name("extra.txt").resolve())] = pipeline._describe(output)
    elif change == "incomplete":
        record["status"] = "interrupted"
    path.write_text(json.dumps(state), encoding="utf-8")
    runner = pipeline._Stages(path.parent, environment, QuietLog())
    calls = []
    try:
        run_legacy_core(runner, "canonical", request, output, calls)
    except ValueError:
        assert change == "missing_code"
        assert calls == []
    else:
        assert calls == ["canonical"]
        assert runner.timings["canonical"]["action"] == "executed"
