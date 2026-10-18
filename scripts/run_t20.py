"""Inspect a completed source-dataset product; no synthetic fallback."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
if __name__ == '__main__':
    from yoyoflot.cli import main
    args = sys.argv[1:]
    if not any(a=='--product' or a.startswith('--product=') for a in args):args += ['--product',str(ROOT/'artifacts/source-product')]
    raise SystemExit(main(['evaluate']+args))
