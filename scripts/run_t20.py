"""T20: prespecified experiments, scenario metrics and an unfilled blind audit."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from yoyoflot.cli import main

if __name__ == "__main__":
    args = sys.argv[1:]
    for flag, relative in [("--config", "configs/evaluation.yaml"), ("--if-config", "configs/isolation_forest.yaml"),
                            ("--rules-config", "configs/model.yaml"), ("--graph-config", "configs/graph.yaml"),
                            ("--artifacts-dir", "artifacts/t20"), ("--work-dir", "data/work/t20")]:
        if not any(arg == flag or arg.startswith(flag + "=") for arg in args):args += [flag, str(ROOT / relative)]
    if "--demo" not in args:args = ["--demo"] + args
    raise SystemExit(main(["evaluate"] + args))
