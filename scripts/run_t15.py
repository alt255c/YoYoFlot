"""Run T15 features from an existing T13 output directory."""
from pathlib import Path
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from yoyoflot.features.travel import build

parser = argparse.ArgumentParser(description="T15 behavioral features")
parser.add_argument("--input", type=Path, default=ROOT / "data/processed")
parser.add_argument("--output", type=Path)
parser.add_argument("--config", type=Path, default=ROOT / "configs/features.yaml")
parser.add_argument("--memory-limit", default="512MB")
args = parser.parse_args()
print(json.dumps(build(args.input, args.output, args.config, args.memory_limit), ensure_ascii=False))
