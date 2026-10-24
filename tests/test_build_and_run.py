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
    shutil.copy2(ROOT / "start_web.sh", project / "start_web.sh")
    interpreter = project / ".venv/bin/python"
    interpreter.parent.mkdir(parents=True)
    interpreter.write_text("""#!/usr/bin/env python3
import json, os, sys
with open(os.environ['RUNNER_LOG'], 'a') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'cwd': os.getcwd()}) + '\\n')
if os.environ.get('FAIL_RUN') and sys.argv[1].endswith('build_project.py'):
    raise SystemExit(23)
if os.environ.get('FAIL_DEPENDENCY_CHECK') and sys.argv[1] == '-':
    raise SystemExit(17)
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
    build = next(command for command in commands if command[0].endswith('build_project.py'))
    for key,value in [('--archive','teacher original.zip'),('--data-dir','raw data'),
                      ('--output','result'),('--work-dir','work data'),('--report-output','result-report')]:
        assert build[build.index(key)+1] == str(caller/value)
    assert build[build.index('--memory-limit')+1]=='2GB'
    assert commands[-1][:3]==['-m','yoyoflot.product.web','--product']
    assert str(caller/'result') in commands[-1]


def test_runner_does_not_publish_reports_after_pipeline_failure(runner):
    project, caller, log, env = runner
    result = subprocess.run(["bash", str(project / "build_and_run.sh")], cwd=caller, env={**env, "FAIL_RUN": "1"}, capture_output=True, text=True)
    assert result.returncode == 23
    calls = [json.loads(line)["argv"] for line in log.read_text().splitlines()]
    assert calls[-1][0].endswith('build_project.py')
    assert not any(call[:2] == ["-m", "yoyoflot.product.web"] for call in calls)


def test_dependency_check_failure_stops_before_source_processing(runner):
    project,caller,log,env=runner
    result=subprocess.run(['bash',str(project/'build_and_run.sh')],cwd=caller,
                          env={**env,'FAIL_DEPENDENCY_CHECK':'1'},capture_output=True,text=True)
    assert result.returncode==17
    calls=[json.loads(line)['argv'] for line in log.read_text().splitlines()]
    assert calls[-1]==['-']
    assert not any(call[0].endswith('build_project.py') for call in calls)
    assert not any(call[:2]==['-m','yoyoflot.product.web'] for call in calls)


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
    command=next(call for call in calls if call[0].endswith('build_project.py'))
    assert ["-m", "pip", "check"] in calls
    assert command[command.index('--core-snapshot')+1]==str(caller/'audited core')
    assert not {"--from-processed", "--from-interim", "--allow-source-errors"} & set(command)


def test_build_only_never_starts_web(runner):
    project,caller,log,env=runner
    result=subprocess.run(['bash',str(project/'build_and_run.sh'),'--build-only'],cwd=caller,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    calls=[json.loads(line)['argv'] for line in log.read_text().splitlines()]
    assert calls[-1][0].endswith('build_project.py')
    assert not any(call[:2]==['-m','yoyoflot.product.web'] for call in calls)


def test_assessment_memory_override_keeps_core_memory_unchanged(runner):
    project,caller,log,env=runner
    result=subprocess.run(['bash',str(project/'build_and_run.sh'),'--build-only',
                           '--assessment-memory-limit','4GB'],cwd=caller,env=env,capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    calls=[json.loads(line)['argv'] for line in log.read_text().splitlines()]
    build=next(call for call in calls if call[0].endswith('build_project.py'))
    assert build[build.index('--memory-limit')+1]=='1GB'
    assert build[build.index('--assessment-memory-limit')+1]=='4GB'
