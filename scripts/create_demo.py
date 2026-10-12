"""Generate small, expressly SYNTHETIC examples for all T03–T10 readers."""

import csv
import json
from pathlib import Path
from zipfile import ZipFile

import yaml
from openpyxl import Workbook
from yoyoflot.readers.boarding_csv import EXPECTED_COLUMNS

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures"
FIX.mkdir(parents=True, exist_ok=True)
row = dict.fromkeys(EXPECTED_COLUMNS, "")
row.update(
    PassengerFirstName="SYNTHETIC",
    PassengerLastName="PERSON",
    PassengerDocument="000123",
    TicketNumber="000987",
    FlightDate="2017-01-01",
    FlightTime="23:30",
    FlightNumber="AB001",
    Destination="BBB",
)
with (FIX / "boarding.csv").open("w", encoding="utf-8", newline="") as f:
    w = csv.writer(f, delimiter=";")
    w.writerow(EXPECTED_COLUMNS)
    w.writerow([row[k] for k in EXPECTED_COLUMNS])
(FIX / "forum.json").write_text(
    json.dumps(
        {
            "Forum Profiles": {
                "synthetic/1": {
                    "Real Name": "SYNTHETIC PERSON",
                    "Travel Documents": {
                        "Passports": [{"Number": "000123"}, {"Number": "000456"}]
                    },
                    "Loyality Programm": {"AA": "0012", "BB": "0012"},
                    "Registered Flights": [
                        {
                            "Flight": "AB001",
                            "Date": "2017-01-01",
                            "Departure": {"Airport": "AAA"},
                            "Arrival": {"Airport": "BBB"},
                        }
                        for _ in range(3)
                    ],
                },
                "synthetic/2": {"NickName": "empty"},
            }
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
(FIX / "pointz.xml").write_text(
    '<PointzAggregatorUsers><user id="001"><name>SYNTHETIC PERSON</name><cards><card number="0012"><bonusprogramm>AA</bonusprogramm></card><card number="0012"><bonusprogramm>BB</bonusprogramm></card></cards><activities><activity><Code>AB001</Code><Date>2017-01-01</Date><Departure>AAA</Departure><Arrival>BBB</Arrival></activity></activities></user><user><name>EMPTY PROFILE</name><cards/></user></PointzAggregatorUsers>',
    encoding="utf-8",
)
(FIX / "skyteam.yaml").write_text(
    "2017-01-01:\n  AB001:\n    FROM: AAA\n    TO: BBB\n    STATUS: UNKNOWN\n    FF: {AA: 0012, BB: 0012}\n",
    encoding="utf-8",
)
fields = [
    ("PaxName", 24),
    ("PaxBirthDate", 10),
    ("DepartDate", 10),
    ("DepartTime", 5),
    ("Flight", 6),
    ("From", 3),
    ("Dest", 3),
    ("e-Ticket", 6),
    ("TravelDoc", 6),
]
columns = []
start = 0
for name, width in fields:
    columns.append(dict(name=name, start=start, end=start + width))
    start += width
(FIX / "sirena_columns.yaml").write_text(
    yaml.safe_dump(
        dict(
            confirmed=True,
            confirmation_scope="synthetic_fixture_only",
            encoding="utf-8",
            unit="characters",
            indexing="zero-based-half-open",
            header_lines=1,
            columns=columns,
        )
    ),
    encoding="utf-8",
)
values = [
    "SYNTHETIC PERSON",
    "1990-01-01",
    "2017-01-01",
    "23:30",
    "AB001",
    "AAA",
    "BBB",
    "000987",
    "000123",
]
(FIX / "sirena.tab").write_text(
    "".join(name.ljust(width) for name, width in fields)
    + "\n"
    + "".join(value.ljust(width) for value, (_, width) in zip(values, fields))
    + "\n",
    encoding="utf-8",
)
wb = Workbook()
ws = wb.active
ws.title = "Passengers"
ws.append(
    ["PassengerName", "FlightDate", "FlightNumber", "TicketNumber", "Destination"]
)
ws.append(["SYNTHETIC PERSON", "2017-01-01", "AB001", "000987", "BBB"])
wb.create_sheet("Empty")
wb.save(FIX / "boardingpass.xlsx")
wb.close()
with ZipFile(FIX / "boardingpass.zip", "w") as z:
    z.write(FIX / "boardingpass.xlsx", "synthetic_2018-01-01.xlsx")
(FIX / "expected.json").write_text(
    json.dumps(
        {
            "scope": "synthetic_only",
            "tables": {
                "boarding_csv": {
                    "person_observations": 1,
                    "identifiers": 2,
                    "travel_observations": 1,
                },
                "forum_json": {
                    "person_observations": 2,
                    "identifiers": 4,
                    "travel_observations": 3,
                },
                "pointz_xml": {
                    "person_observations": 2,
                    "identifiers": 2,
                    "travel_observations": 1,
                },
                "sirena_fixed": {
                    "person_observations": 1,
                    "identifiers": 2,
                    "travel_observations": 1,
                },
                "skyteam_yaml": {
                    "person_observations": 2,
                    "identifiers": 2,
                    "travel_observations": 2,
                },
                "boardingpass_xlsx": {
                    "person_observations": 1,
                    "identifiers": 1,
                    "travel_observations": 1,
                },
                "timetable_pdf": {"schedules": 5},
            },
        },
        indent=2,
    ),
    encoding="utf-8",
)
print("Synthetic fixtures generated:", FIX)
