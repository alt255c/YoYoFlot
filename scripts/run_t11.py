"""Run in PyCharm or: python scripts/run_t11.py --input data/interim/real.

No arguments: prefer real reader outputs; otherwise look in data/interim.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

if __name__ == '__main__':
    from yoyoflot.cli import main
    default = ROOT/'data/interim/real'
    if not default.is_dir():
        default = ROOT/'data/interim'
    args = sys.argv[1:]
    if '--input' not in args:
        args = ['--input',str(default)] + args
    if '--reference' not in args:
        args += ['--reference',str(ROOT/'data/reference/airports.csv'),
                 '--reference-manifest',str(ROOT/'data/reference/reference_manifest.json')]
    if '--overrides' not in args:
        args += ['--overrides',str(ROOT/'configs/airports_overrides.yaml')]
    if '--output' not in args:
        args += ['--output',str(ROOT/'data/processed')]
    raise SystemExit(main(['airports'] + args))
