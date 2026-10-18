"""Run the independent fictional T17 demo, including from PyCharm's Run button."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    from yoyoflot.cli import main

    args = sys.argv[1:]
    if not any(arg in {"--demo", "--input"} or arg.startswith("--input=") for arg in args):
        args += ["--demo"]
    defaults = {"--config": ROOT / "configs/model.yaml", "--demo-dir": ROOT / "data/work/t17",
                "--artifacts-dir": ROOT / "artifacts/t17"}
    for flag, path in defaults.items():
        if not any(arg == flag or arg.startswith(flag + "=") for arg in args):
            args += [flag, str(path)]
    raise SystemExit(main(["train"] + args))
