"""Independent input census; retain aggregates, not personal sample payloads."""

from pathlib import Path
from collections import Counter
from zipfile import ZipFile
from lxml import etree
import json
import csv
import hashlib

ROOT = Path(__file__).resolve().parents[1]
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--data-dir", type=Path, default=ROOT / "data/raw")
INPUT = parser.parse_args().data_dir
OUT = ROOT / "docs/validation/input_census.json"
result = {"scope": "real_inputs", "files": {}}
for name in [
    "BoardingData.csv",
    "PointzAggregator-AirlinesData.xml",
    "Sirena-export-fixed.tab",
    "SkyTeam-Exchange.yaml",
    "YourBoardingPassDotAero.zip",
]:
    p = INPUT / name
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    result["files"][name] = {"bytes": p.stat().st_size, "sha256": h.hexdigest()}
with (INPUT / "BoardingData.csv").open(encoding="utf-8-sig", newline="") as f:
    r = csv.reader(f, delimiter=";", strict=True)
    header = next(r)
    counts = Counter()
    for row in r:
        counts["records"] += 1
        counts["blank"] += not any(row)
        counts["valid_width"] += len(row) == 14
    result["boarding_csv"] = {"header": header, **dict(counts)}
counts = Counter()
programmes = Counter()
for _, e in etree.iterparse(
    str(INPUT / "PointzAggregator-AirlinesData.xml"),
    events=("end",),
    resolve_entities=False,
    no_network=True,
):
    tag = etree.QName(e).localname
    if tag in ["user", "card", "activity"]:
        counts[tag] += 1
    if tag == "bonusprogramm":
        programmes[e.text] += 1
    if tag == "user":
        e.clear(keep_tail=True)
        while e.getprevious() is not None:
            del e.getparent()[0]
result["pointz_xml"] = {**dict(counts), "programmes": dict(programmes)}
with (INPUT / "Sirena-export-fixed.tab").open(encoding="utf-8") as f:
    header = f.readline().rstrip("\n")
    lengths = Counter(len(s.rstrip("\n")) for s in f)
result["sirena_fixed"] = {
    "header": header,
    "lengths": dict(lengths),
    "rows": sum(lengths.values()),
}
with ZipFile(INPUT / "YourBoardingPassDotAero.zip") as z:
    books = [i for i in z.infolist() if i.filename.lower().endswith(".xlsx")]
    result["boardingpass_xlsx"] = {
        "members": len(z.infolist()),
        "books": len(books),
        "largest_workbook": max(books, key=lambda i: i.file_size).filename,
        "largest_uncompressed_bytes": max(i.file_size for i in books),
    }
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(OUT)
