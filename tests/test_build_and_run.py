"""Exercise shell argument/path handling and failure propagation without ETL."""
import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def runner(tmp_path):
    project = tmp_path / "project with spaces"
    project.mkdir()
    shutil.copy2(ROOT / "build_and_run.sh", project / "build_and_run.sh")
    interpreter = project / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['RUNNER_LOG'], 'a') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
if os.environ.get('FAIL_RUN') and sys.argv[1:4] == ['-m', 'yoyoflot', 'run']:
    raise SystemExit(23)
""", encoding="utf-8")
    interpreter.chmod(0o755)
    caller = tmp_path / "another cwd"
    caller.mkdir()
    log = tmp_path / "calls.jsonl"
    env = {**os.environ, "RUNNER_LOG": str(log)}
    return project, caller, log, env


def test_runner_preserves_paths_and_runs_strict_pipeline_before_reports(runner):
    project, caller, log, env = runner
    result = subprocess.run(["bash", str(project / "build_and_run.sh"), "--archive", "teacher original.zip", "--data-dir", "raw data", "--output", "result", "--work-dir", "work data", "--memory-limit", "2GB"], cwd=caller, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert all(call["cwd"] == str(project) for call in calls)
    commands = [call["argv"] for call in calls]
    assert ["-m", "pip", "install", "--disable-pip-version-check", "-r", str(project / "requirements.txt")] in commands
    assert ["-m", "pip", "install", "--disable-pip-version-check", "--no-deps", "-e", str(project)] in commands
    assert ["-m", "pip", "check"] in commands
    dataset = next(command for command in commands if command[0].endswith("download_dataset.py"))
    assert dataset == [str(project / "scripts/download_dataset.py"), "--data-dir", str(caller / "raw data"), "--archive", str(caller / "teacher original.zip")]
    pipeline = next(command for command in commands if command[:3] == ["-m", "yoyoflot", "run"])
    assert pipeline[-3:] == ["--require-all", "--until", "product"]
    assert "--allow-source-errors" not in pipeline
    assert pipeline[pipeline.index("--memory-limit") + 1] == "2GB"
    assert pipeline[pipeline.index("--work-dir") + 1] == str(caller / "work data")
    assert commands[-2] == [str(project / "scripts/verify_source_product.py"), "--product", str(caller / "result"), "--report", str(caller / "result-verification.json")]
    assert commands[-1] == ["-m", "yoyoflot", "report", "--product", str(caller / "result"), "--output", str(caller / "result-report")]


def test_runner_does_not_publish_reports_after_pipeline_failure(runner):
    project, caller, log, env = runner
    result = subprocess.run(["bash", str(project / "build_and_run.sh")], cwd=caller, env={**env, "FAIL_RUN": "1"}, capture_output=True, text=True)
    assert result.returncode == 23
    calls = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert calls[-1][:3] == ["-m", "yoyoflot", "run"]
    assert not any("verify_source_product.py" in " ".join(call) or call[:3] == ["-m", "yoyoflot", "report"] for call in calls)


@pytest.mark.parametrize("args", [["--archive"], ["--core-snapshot"], ["--output", "--work-dir"], ["--allow-source-errors"]])
def test_runner_rejects_invalid_arguments_before_bootstrap(runner, args):
    project, caller, log, env = runner
    result = subprocess.run(["bash", str(project / "build_and_run.sh"), *args], cwd=caller, env=env, capture_output=True, text=True)
    assert result.returncode != 0
    assert not log.exists()


def test_runner_core_reuse_keeps_dependency_and_raw_verification(runner):
    project, caller, log, env = runner
    result = subprocess.run(["bash", str(project / "build_and_run.sh"), "--core-snapshot", "audited core"],
                            cwd=caller, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    download_index = next(i for i, command in enumerate(calls) if command[0].endswith("download_dataset.py"))
    pipeline_index = next(i for i, command in enumerate(calls) if command[:3] == ["-m", "yoyoflot", "run"])
    assert ["-m", "pip", "check"] in calls[:download_index]
    assert download_index < pipeline_index
    command = calls[pipeline_index]
    assert command[command.index("--core-snapshot") + 1] == str(caller / "audited core")
    assert command[-3:] == ["--require-all", "--until", "product"]
    assert not {"--from-processed", "--from-interim", "--allow-source-errors"} & set(command)
    assert calls[-1][:3] == ["-m", "yoyoflot", "report"]
