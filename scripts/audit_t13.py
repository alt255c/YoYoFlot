"""Validate the human-reviewed person-pair CSV; does not rebuild T13 tables."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))

if __name__ == '__main__':
    from yoyoflot.cli import main
    args = sys.argv[1:]
    for flag,path in [('--input',ROOT/'data/processed'),('--sample',ROOT/'data/review/person_pairs.csv'),
                      ('--report',ROOT/'data/review/person_pairs_review_report.json')]:
        if not any(a==flag or a.startswith(flag+'=') for a in args):
            args += [flag,str(path)]
    raise SystemExit(main(['resolution-review']+args))
