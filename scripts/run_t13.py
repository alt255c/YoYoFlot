"""T13 over completed T12/T11 output; works with PyCharm Run and -q."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

if __name__ == '__main__':
    from yoyoflot.cli import main
    args = sys.argv[1:]
    for flag,path in [('--input',ROOT/'data/processed'),('--output',ROOT/'data/processed'),
                      ('--config',ROOT/'configs/resolution.yaml'),('--review-output',ROOT/'data/review')]:
        if not any(a==flag or a.startswith(flag+'=') for a in args):
            args += [flag,str(path)]
    raise SystemExit(main(['resolve']+args))
