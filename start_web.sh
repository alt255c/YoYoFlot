#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
    echo "Environment is missing. Run: bash $ROOT/build_and_run.sh" >&2
    exit 1
fi
exec "$PYTHON" -m yoyoflot.product.web --product "$ROOT/artifacts/source-product" "$@"
