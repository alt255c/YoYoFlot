"""Fit the independent fictional IF experiment, including PyCharm Run."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    from yoyoflot.cli import main

    args = sys.argv[1:]
    if not any(arg in {"--demo", "--input"} or arg.startswith("--input=") for arg in args):
        args += ["--demo"]
    defaults = {"--config": ROOT / "configs/isolation_forest.yaml", "--rules-config": ROOT / "configs/model.yaml",
                "--graph-config": ROOT / "configs/graph.yaml", "--demo-dir": ROOT / "data/work/t19/evaluation",
                "--work-dir": ROOT / "data/work/t19", "--artifacts-dir": ROOT / "artifacts/t19"}
    for flag, path in defaults.items():
        if not any(arg == flag or arg.startswith(flag + "=") for arg in args):
            args += [flag, str(path)]
    raise SystemExit(main(["train-if"] + args))
