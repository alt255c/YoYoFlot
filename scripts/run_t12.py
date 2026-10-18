"""T12 canonical build, also runnable via PyCharm Run; readers are not repeated."""
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

if __name__=='__main__':
    from yoyoflot.cli import main
    args=sys.argv[1:]
    default=ROOT/'data/interim/real'
    if not default.is_dir():default=ROOT/'data/interim'
    if not any(a=='--input' or a.startswith('--input=') for a in args):args=['--input',str(default)]+args
    if not any(a=='--output' or a.startswith('--output=') for a in args):args+=['--output',str(ROOT/'data/processed')]
    if not any(a=='--config' or a.startswith('--config=') for a in args):args+=['--config',str(ROOT/'configs/normalization.yaml')]
    raise SystemExit(main(['build']+args))
