"""Build T17–T20 on the supplied dataset. Never generate replacement inputs."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
if __name__ == '__main__':
    from yoyoflot.cli import main
    args = sys.argv[1:]
    for flag, path in {'--input':ROOT/'data/processed','--config':ROOT/'configs/product.yaml',
                      '--reference':ROOT/'data/reference/airports.csv','--output':ROOT/'artifacts/source-product'}.items():
        if not any(a==flag or a.startswith(flag+'=') for a in args):args += [flag,str(path)]
    raise SystemExit(main(['train-if']+args))
