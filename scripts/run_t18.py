"""Reproducible fictional coflight graph, including PyCharm Run support."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    from yoyoflot.cli import main

    args = sys.argv[1:]
    if not any(arg in {"--demo", "--input"} or arg.startswith("--input=") for arg in args):
        args += ["--demo"]
    for flag, path in {"--config": ROOT / "configs/graph.yaml", "--demo-dir": ROOT / "data/work/t18",
                       "--artifacts-dir": ROOT / "artifacts/t18"}.items():
        if not any(arg == flag or arg.startswith(flag + "=") for arg in args):
            args += [flag, str(path)]
    raise SystemExit(main(["graph"] + args))
