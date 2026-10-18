"""T16 entry point, also usable with PyCharm Run from another directory."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if __name__ == "__main__":
    from yoyoflot.cli import main

    args = sys.argv[1:]
    defaults = {
        "--config": ROOT / "configs/sources.yaml",
        "--product-config": ROOT / "configs/product.yaml",
        "--data-dir": ROOT / "data/raw",
        "--work-dir": ROOT / "data/work/t16",
        "--artifacts-dir": ROOT / "artifacts",
        "--normalization-config": ROOT / "configs/normalization.yaml",
        "--resolution-config": ROOT / "configs/resolution.yaml",
        "--reference": ROOT / "data/reference/airports.csv",
        "--overrides": ROOT / "configs/airports_overrides.yaml",
    }
    for flag, path in defaults.items():
        if not any(arg == flag or arg.startswith(flag + "=") for arg in args):
            args += [flag, str(path)]
    raise SystemExit(main(["run"] + args))
