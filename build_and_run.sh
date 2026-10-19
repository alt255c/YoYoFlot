#!/usr/bin/env bash
# Reproducible backend build on the original teacher dataset; no UI is started.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CALLER_DIR="$PWD"
DATA_DIR="$ROOT/data/raw"
OUTPUT="$ROOT/artifacts/source-product"
WORK_DIR="$ROOT/data/work/source-product"
REPORT_OUTPUT=""
ARCHIVE=""
CORE_SNAPSHOT=""
MEMORY_LIMIT="1GB"
PYTHON_BIN="${YOYOFLOT_PYTHON:-}"

usage() {
    cat <<'HELP'
Usage: ./build_and_run.sh [options]

Create/reuse .venv, install requirements, verify/download the original dataset,
run all seven sources through the complete backend, verify and export results.
Python 3.12 is preferred; Python 3.10+ is supported by the dependency markers.

  --archive PATH        Use an original local Airlines-All.zip (offline data)
  --core-snapshot PATH  Reuse a verified raw T16 core matching all current inputs
  --data-dir PATH       Raw source directory (default: <repo>/data/raw)
  --output PATH         Product snapshot (default: <repo>/artifacts/source-product)
  --work-dir PATH       Resumable ETL work (default: <repo>/data/work/source-product)
  --report-output PATH  CSV/JSON/Markdown reports (default: <output>-report)
  --memory-limit SIZE   DuckDB limit, not total process RAM (default: 1GB)
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
        --archive|--core-snapshot|--data-dir|--output|--work-dir|--report-output|--memory-limit|--python)
            (($# >= 2)) && [[ -n "$2" && "$2" != --* ]] || fail "Missing value for $1"
            case "$1" in
                --archive) ARCHIVE="$(absolute_path "$2")" ;;
                --core-snapshot) CORE_SNAPSHOT="$(absolute_path "$2")" ;;
                --data-dir) DATA_DIR="$(absolute_path "$2")" ;;
                --output) OUTPUT="$(absolute_path "$2")" ;;
                --work-dir) WORK_DIR="$(absolute_path "$2")" ;;
                --report-output) REPORT_OUTPUT="$(absolute_path "$2")" ;;
                --memory-limit) MEMORY_LIMIT="$2" ;;
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

download_args=(--data-dir "$DATA_DIR")
[[ -z "$ARCHIVE" ]] || download_args+=(--archive "$ARCHIVE")
"$PYTHON" "$ROOT/scripts/download_dataset.py" "${download_args[@]}"

core_args=()
if [[ -n "$CORE_SNAPSHOT" ]]; then
    core_args+=(--core-snapshot "$CORE_SNAPSHOT")
    printf '\nVerifying the raw core against all seven original sources, then building the product...\n'
else
    printf '\nRunning the complete backend on all seven original sources...\n'
fi
"$PYTHON" -m yoyoflot run \
    --data-dir "$DATA_DIR" \
    --work-dir "$WORK_DIR" \
    --artifacts-dir "$ROOT/artifacts" \
    --output "$OUTPUT" \
    --memory-limit "$MEMORY_LIMIT" \
    "${core_args[@]}" \
    --require-all --until product

printf '\nVerifying scores, factors and routes, then exporting reports...\n'
"$PYTHON" "$ROOT/scripts/verify_source_product.py" \
    --product "$OUTPUT" --report "${OUTPUT%/}-verification.json"
"$PYTHON" -m yoyoflot report --product "$OUTPUT" --output "$REPORT_OUTPUT"
printf '\nComplete. Product: %s\nReports: %s\nVerification: %s\n' \
    "$OUTPUT" "$REPORT_OUTPUT" "${OUTPUT%/}-verification.json"
