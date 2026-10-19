#!/usr/bin/env bash
# Compatibility entry point for the same verified source-only downloader.
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ -x "$ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$ROOT/.venv/bin/python"
else
    PYTHON_BIN="${YOYOFLOT_PYTHON:-python3}"
fi
exec "$PYTHON_BIN" "$ROOT/scripts/download_dataset.py" --data-dir "$ROOT/data/raw" "$@"
