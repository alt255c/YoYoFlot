#!/usr/bin/env bash
# Complete build and export on the original teacher dataset. Start the site with start_web.sh.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CALLER_DIR="$PWD"
DATA_DIR="$ROOT/data/raw"
OUTPUT="$ROOT/artifacts/source-product"
WORK_DIR="$ROOT/data/work/source-product"
REPORT_OUTPUT=""
SCORING="$ROOT/configs/scoring.yaml"
BUILD_ONLY=false
ARCHIVE=""
CORE_SNAPSHOT=""
MEMORY_LIMIT="1GB"
ASSESSMENT_MEMORY_LIMIT=""
PYTHON_BIN="${YOYOFLOT_PYTHON:-}"

usage() {
    cat <<'HELP'
Usage: ./build_and_run.sh [options]

Create/reuse .venv, install requirements, verify/download the original dataset,
run all seven sources through the complete backend, verify and export results.
Python 3.12 is preferred; Python 3.10+ is supported by the dependency markers.

  --build-only          Build and export without starting the website
  --archive PATH        Use an original local Airlines-All.zip (offline data)
  --core-snapshot PATH  Reuse a verified raw T16 core matching all current inputs
  --data-dir PATH       Raw source directory (default: <repo>/data/raw)
  --output PATH         Product snapshot (default: <repo>/artifacts/source-product)
  --work-dir PATH       Resumable ETL work (default: <repo>/data/work/source-product)
  --report-output PATH  CSV/JSON/Markdown reports (default: <output>-report)
  --scoring PATH        Scoring weights (default: configs/scoring.yaml)
  --memory-limit SIZE   DuckDB limit, not total process RAM (default: 1GB)
  --assessment-memory-limit SIZE  Override only source-factor calculation memory
  --python COMMAND     Interpreter for a new .venv (or YOYOFLOT_PYTHON)
  -h, --help            Show this help

Relative option paths are resolved against the caller's current directory.
Existing .venv is reused. Source files are always checked against pinned hashes.
Core reuse verifies every published artifact and all live raw/config/reference
hashes; it never falls back to processed tables or Git LFS extracts.
HELP
}

fail() { printf 'Error: %s\n' "$*" >&2; exit 1; }
absolute_path() {
    case "$1" in
        /*) printf '%s\n' "$1" ;;
        *) printf '%s/%s\n' "$CALLER_DIR" "$1" ;;
    esac
}

while (($#)); do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --build-only) BUILD_ONLY=true; shift ;;
        --archive|--core-snapshot|--data-dir|--output|--work-dir|--report-output|--scoring|--memory-limit|--assessment-memory-limit|--python)
            (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || fail "Missing value for $1"
            case "$1" in
                --archive) ARCHIVE="$(absolute_path "$2")" ;;
                --core-snapshot) CORE_SNAPSHOT="$(absolute_path "$2")" ;;
                --data-dir) DATA_DIR="$(absolute_path "$2")" ;;
                --output) OUTPUT="$(absolute_path "$2")" ;;
                --work-dir) WORK_DIR="$(absolute_path "$2")" ;;
                --report-output) REPORT_OUTPUT="$(absolute_path "$2")" ;;
                --scoring) SCORING="$(absolute_path "$2")" ;;
                --memory-limit) MEMORY_LIMIT="$2" ;;
                --assessment-memory-limit) ASSESSMENT_MEMORY_LIMIT="$2" ;;
                --python) PYTHON_BIN="$2" ;;
            esac
            shift 2 ;;
        *) fail "Unknown argument: $1 (see --help)" ;;
    esac
done

[[ -n "$REPORT_OUTPUT" ]] || REPORT_OUTPUT="${OUTPUT%/}-report"
if [[ -n "$PYTHON_BIN" && "$PYTHON_BIN" == */* ]]; then
    PYTHON_BIN="$(absolute_path "$PYTHON_BIN")"
fi

cd -- "$ROOT"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
    if [[ -z "$PYTHON_BIN" ]]; then
        for candidate in python3.12 python3 python; do
            if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
                PYTHON_BIN="$candidate"
                break
            fi
        done
    fi
    [[ -n "$PYTHON_BIN" ]] || fail "Python 3.10+ is required; install Python 3.12 or pass --python."
    "$PYTHON_BIN" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || fail "Python 3.10+ is required."
    printf 'Creating .venv with %s\n' "$PYTHON_BIN"
    "$PYTHON_BIN" -m venv "$ROOT/.venv"
fi
PYTHON="$ROOT/.venv/bin/python"
"$PYTHON" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' || fail "Existing .venv needs Python 3.10+; recreate it with Python 3.12."

printf '\nInstalling pinned dependencies and the backend...\n'
"$PYTHON" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
"$PYTHON" -m pip install --disable-pip-version-check --no-deps -e "$ROOT"
"$PYTHON" -m pip check

# Exercise the same DuckDB API used by resolution before any lengthy ETL.
"$PYTHON" - <<'PY'
import duckdb
with duckdb.connect() as db:
    db.create_function('_dependency_check', lambda value: value,
                       ['VARCHAR'], 'VARCHAR', null_handling='special')
    assert db.execute("SELECT _dependency_check('ready'), _dependency_check(NULL)").fetchone() == ('ready', None)
PY

args=(--data-dir "$DATA_DIR" --work-dir "$WORK_DIR" --output "$OUTPUT"
      --report-output "$REPORT_OUTPUT" --scoring "$SCORING" --memory-limit "$MEMORY_LIMIT")
[[ -z "$ARCHIVE" ]] || args+=(--archive "$ARCHIVE")
[[ -z "$CORE_SNAPSHOT" ]] || args+=(--core-snapshot "$CORE_SNAPSHOT")
[[ -z "$ASSESSMENT_MEMORY_LIMIT" ]] || args+=(--assessment-memory-limit "$ASSESSMENT_MEMORY_LIMIT")
"$PYTHON" "$ROOT/scripts/build_project.py" "${args[@]}"
printf '\nBuild complete. Start the site with: bash %s/start_web.sh\n' "$ROOT"
if [[ "$BUILD_ONLY" == false ]]; then
    exec bash "$ROOT/start_web.sh" --product "$OUTPUT" --scoring "$SCORING"
fi
