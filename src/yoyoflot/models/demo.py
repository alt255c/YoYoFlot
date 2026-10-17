"""Bounded, reproducible fictional inputs; never imports passenger/source tables.

The manifest is not an authorization flag: validation replays the supported
generator and compares every logical row, in addition to checking file hashes.
"""
import hashlib
import json
import random
import shutil
import tempfile
from datetime import date, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..normalize.locking import OutputLock
from ..provenance import fingerprint

GENERATOR = "yoyoflot-independent-fiction-v1"
VARIED_GENERATOR = "yoyoflot-independent-fiction-v2"
START, END = date(2026, 1, 1), date(2026, 3, 31)
MAX_PROFILES = 500
PROFILE_SCHEMA = pa.schema([
    ("profile_id", pa.string()), ("display_name", pa.string()),
    ("identity_status", pa.string()), ("source_coverage", pa.float64()),
    ("window_start", pa.date32()), ("window_end", pa.date32()),
])
SEGMENT_SCHEMA = pa.schema([
    ("segment_id", pa.string()), ("profile_id", pa.string()),
    ("departure_date", pa.date32()), ("departure_precision", pa.string()),
    ("origin_iata", pa.string()), ("destination_iata", pa.string()),
])
TABLES = {"profiles.parquet": PROFILE_SCHEMA, "segments.parquet": SEGMENT_SCHEMA}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def recipe(seed=17, profiles=120, generator=GENERATOR):
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed должен быть целым числом от 0 до 2**32-1")
    if type(profiles) is not int or not 0 <= profiles <= MAX_PROFILES:
        raise ValueError(f"profiles должен быть от 0 до {MAX_PROFILES}")
    if generator not in {GENERATOR, VARIED_GENERATOR}:
        raise ValueError("Неизвестная версия независимого учебного генератора")
    return dict(generator=generator, seed=seed, profiles=profiles,
                window_start=START.isoformat(), window_end=END.isoformat())


def generate_rows(spec):
    """Ten explicit scenarios, including missing data and ordinary activity."""
    expected = recipe(spec.get("seed"), spec.get("profiles"), spec.get("generator"))
    if spec != expected:
        raise ValueError("Неизвестная версия или параметры учебного генератора")
    if spec["generator"] == VARIED_GENERATOR:
        from .demo_varied import generate
        return generate(spec)
    rng = random.Random(spec["seed"])
    airports = ["SVO", "LED", "JFK", "CDG", "HND", "LHR", "DXB", "SIN"]
    rng.shuffle(airports)
    profiles, segments = [], []
    for i in range(spec["profiles"]):
        pid = f"demo_profile_{i + 1:06d}"
        scenario = i % 10
        profiles.append(dict(profile_id=pid, display_name=f"Учебный профиль {i + 1:04d}",
                             identity_status="certain", source_coverage=0.5 if scenario == 6 else 1.0,
                             window_start=START, window_end=END))
        n = {0: 6, 1: 18, 2: 8, 3: 10, 4: 30, 5: 18, 6: 18, 7: 0, 8: 18, 9: 18}[scenario]
        # Consecutive points are entirely invented; airport codes are public labels.
        if scenario in {2, 3, 4}:
            points = [airports[j % 2] for j in range(n + 1)]
        elif scenario == 5:
            indices = [0, 1, 0, 2, 0, 3, 0, 4, 0, 5, 6, 7, 1, 2, 3, 4, 5, 6, 7]
            points = [airports[j] for j in indices]
        else:
            points = [airports[j % 6] for j in range(n + 1)]
        step = 10 if scenario == 0 else 8 if scenario == 3 else 1
        offset = rng.randrange(3)
        for j in range(n):
            missing_date, missing_place = scenario == 9, scenario == 8 and j % 2 == 0
            segments.append(dict(segment_id=f"demo_segment_{i + 1:06d}_{j + 1:03d}", profile_id=pid,
                                 departure_date=None if missing_date else START + timedelta(days=offset + j * step),
                                 departure_precision="unknown" if missing_date else "day",
                                 origin_iata=None if missing_place else points[j],
                                 destination_iata=None if missing_place else points[j + 1]))
    return profiles, segments


def logical_hash(profiles, segments):
    return digest(dict(profiles=sorted(profiles, key=lambda r: r["profile_id"]),
                       segments=sorted(segments, key=lambda r: r["segment_id"])))


def validate_demo(directory):
    directory = Path(directory).resolve()
    path = directory / "demo_manifest.json"
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("T17 принимает только независимый учебный набор генератора; нужен demo_manifest.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(manifest, dict) or
            set(manifest) != {"schema_version", "data_mode", "origin", "recipe", "logical_sha256", "files"} or
            manifest["schema_version"] != "1.0" or manifest["data_mode"] != "synthetic_demo" or
            manifest["origin"] != "independent_generator" or not isinstance(manifest["recipe"], dict)):
        raise ValueError("Реальные, обезличенные и неподтверждённые персональные входы T17 не принимает")
    expected_profiles, expected_segments = generate_rows(manifest["recipe"])
    if manifest["logical_sha256"] != logical_hash(expected_profiles, expected_segments):
        raise ValueError("Логический хеш не соответствует независимому генератору")
    if not isinstance(manifest["files"], dict) or set(manifest["files"]) != set(TABLES):
        raise ValueError("Неверный состав учебного набора")
    rows = {}
    for name, schema in TABLES.items():
        table_path = directory / name
        if not table_path.is_file() or table_path.is_symlink() or table_path.stat().st_size > 32 * 1024**2:
            raise ValueError(f"Недопустимый учебный файл: {name}")
        if fingerprint(table_path) != manifest["files"][name]:
            raise ValueError(f"Изменён учебный файл: {name}")
        file = pq.ParquetFile(table_path)
        per_profile = 60 if manifest["recipe"]["generator"] == VARIED_GENERATOR else 30
        limit = MAX_PROFILES if name == "profiles.parquet" else MAX_PROFILES * per_profile
        expanded_size = sum(file.metadata.row_group(i).column(j).total_uncompressed_size
                            for i in range(file.metadata.num_row_groups)
                            for j in range(file.metadata.num_columns))
        if (file.metadata.num_rows > limit or expanded_size > 64 * 1024**2 or
                not file.schema_arrow.equals(schema, check_metadata=False)):
            raise ValueError(f"Неверная схема или размер учебной таблицы: {name}")
        rows[name] = file.read().to_pylist()
    profiles, segments = rows["profiles.parquet"], rows["segments.parquet"]
    # A copied synthetic flag and freshly recomputed checksums cannot admit real rows.
    if (sorted(profiles, key=lambda r: r["profile_id"]) != expected_profiles or
            sorted(segments, key=lambda r: r["segment_id"]) != expected_segments):
        raise ValueError("Строки не воспроизводятся независимым учебным генератором")
    return manifest, profiles, segments


def create_demo(root, seed=17, profiles=120, generator=GENERATOR):
    spec = recipe(seed, profiles, generator)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    target = root / ("demo-" + digest(spec)[:20])
    with OutputLock(root / ".t17-demo.lock", "T17 demo"):
        if target.exists():
            validate_demo(target)
            return target
        temporary = Path(tempfile.mkdtemp(prefix=".t17-demo-", suffix=".partial", dir=root))
        try:
            people, journeys = generate_rows(spec)
            for name, data in [("profiles.parquet", people), ("segments.parquet", journeys)]:
                pq.write_table(pa.Table.from_pylist(data, schema=TABLES[name]), temporary / name)
            write_json(temporary / "demo_manifest.json", dict(
                schema_version="1.0", data_mode="synthetic_demo", origin="independent_generator", recipe=spec,
                logical_sha256=logical_hash(people, journeys),
                files={name: fingerprint(temporary / name) for name in TABLES}))
            validate_demo(temporary)
            temporary.rename(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return target
