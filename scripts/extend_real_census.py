"""Independent JSON/YAML counters and ZIP workbook/sheet census."""

import csv
import hashlib
import json
import shutil
import tempfile
from collections import Counter
from pathlib import Path
from zipfile import ZipFile
import ijson
import yaml
from lxml import etree

ROOT = Path(__file__).resolve().parents[1]
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--data-dir", type=Path, default=ROOT / "data/raw")
SOURCE = parser.parse_args().data_dir
OUT = ROOT / "docs/validation"
report = json.loads((OUT / "input_census.json").read_text(encoding="utf-8"))
p = SOURCE / "FrequentFlyerForum-Profiles.json"
h = hashlib.sha256()
with p.open("rb") as f:
    for b in iter(lambda: f.read(1024 * 1024), b""):
        h.update(b)
report["files"][p.name] = {"bytes": p.stat().st_size, "sha256": h.hexdigest()}
c = Counter()
with p.open("rb") as f:
    for profile in ijson.items(f, "Forum Profiles.item"):
        c["profiles"] += 1
        c["flights"] += len(profile.get("Registered Flights") or [])
        c["cards"] += len(profile.get("Loyality Programm") or [])
        for container in profile.get("Travel Documents") or []:
            c["document_containers"] += 1
            c["empty_document_containers"] += container.get("Passports") is None
report["forum_json"] = dict(c)
c = Counter()
depth = 0
context = []
last_key = None
status_values = Counter()
card_codes = Counter()
with (SOURCE / "SkyTeam-Exchange.yaml").open(encoding="utf-8-sig") as f:
    for event in yaml.parse(f, Loader=yaml.CSafeLoader):
        if isinstance(event, yaml.events.MappingStartEvent):
            depth += 1
            context.append({"key": None, "key_next": True})
        elif isinstance(event, yaml.events.MappingEndEvent):
            context.pop()
            depth -= 1
            if context:
                context[-1]["key_next"] = True
        elif isinstance(event, yaml.events.ScalarEvent):
            frame = context[-1]
            if frame["key_next"]:
                frame["key"] = event.value
                frame["key_next"] = False
                if depth == 1:
                    c["dates"] += 1
                elif depth == 2:
                    c["flights"] += 1
                elif depth == 4 and context[-2]["key"] == "FF":
                    c["cards"] += 1
                    card_codes[event.value.split()[0]] += 1
            else:
                if depth == 3 and frame["key"] == "STATUS":
                    status_values[event.value] += 1
                frame["key_next"] = True
report["skyteam_yaml"] = {
    **dict(c),
    "status_values": dict(status_values),
    "card_programme_codes": dict(card_codes),
}
books = []
ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
with ZipFile(SOURCE / "YourBoardingPassDotAero.zip") as archive:
    for member in archive.infolist():
        if not member.filename.lower().endswith(".xlsx"):
            continue
        with archive.open(member) as f, tempfile.TemporaryFile() as t:
            shutil.copyfileobj(f, t, 1024 * 1024)
            t.seek(0)
            with ZipFile(t) as book:
                root = etree.fromstring(
                    book.read("xl/workbook.xml"),
                    parser=etree.XMLParser(resolve_entities=False, no_network=True),
                )
                sheets = [s.get("name") for s in root.findall("s:sheets/s:sheet", ns)]
                books.append(
                    {
                        "workbook": member.filename,
                        "uncompressed_bytes": member.file_size,
                        "sheets": len(sheets),
                        "sheet_names_sha256": hashlib.sha256(
                            json.dumps(sheets).encode()
                        ).hexdigest(),
                    }
                )
report["boardingpass_xlsx"]["sheets"] = sum(b["sheets"] for b in books)
with (OUT / "workbook_inventory.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.DictWriter(f, fieldnames=list(books[0]))
    w.writeheader()
    w.writerows(books)
(OUT / "input_census.json").write_text(
    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
)
print({k: report[k] for k in ["forum_json", "skyteam_yaml", "boardingpass_xlsx"]})
