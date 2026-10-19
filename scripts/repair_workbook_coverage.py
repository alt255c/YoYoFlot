#!/usr/bin/env python3
"""Publish an audited coverage-only core revision from an exact reader rerun."""
import argparse
import json
from pathlib import Path

from yoyoflot.product.repair import repair_workbook_coverage


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-core', type=Path, required=True)
    parser.add_argument('--fresh-reader', type=Path, required=True)
    parser.add_argument('--raw-zip', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='Optional exact derived repair run directory')
    args = parser.parse_args()
    result = repair_workbook_coverage(args.old_core, args.fresh_reader, args.raw_zip, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
