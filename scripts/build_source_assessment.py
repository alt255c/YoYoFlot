"""Build the source-availability supplement without changing the product model."""
import argparse
import json
from pathlib import Path

from yoyoflot.product.assessment import build

ROOT = Path(__file__).resolve().parents[1]

if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--product', type=Path, default=ROOT / 'artifacts/source-product')
    p.add_argument('--canonical', type=Path, default=ROOT / 'data/processed/t14_canonical_20261017')
    p.add_argument('--archive', type=Path, default=ROOT / 'data/Airlines-All.zip')
    p.add_argument('--raw-dir', type=Path, default=ROOT / 'data/Airlines')
    p.add_argument('--output', type=Path, default=ROOT / 'artifacts/source-assessment-v3')
    p.add_argument('--memory-limit', default='1GB')
    a = p.parse_args()
    report = build(a.product, a.canonical, a.archive, a.raw_dir, a.output, a.memory_limit,
                   progress=lambda text: print(text, flush=True))
    print(json.dumps(report['statistics'], ensure_ascii=False))
