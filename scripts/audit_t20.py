"""Import an independent review CSV; never fabricate reviewer decisions."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from yoyoflot.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    if not any(arg == "--artifacts-dir" or arg.startswith("--artifacts-dir=") for arg in args):args += ["--artifacts-dir", str(ROOT / "artifacts/t20-review")]
    raise SystemExit(main(["audit-ranking"] + args))
