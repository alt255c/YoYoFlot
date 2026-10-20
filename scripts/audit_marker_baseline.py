"""Audit document conflicts against canonical parquet and original ZIP members.

This script records evidence for the marker redesign. It does not rebuild or
rescore the running product, infer document ownership, or generate passengers.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
from contextlib import ExitStack
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path
from zipfile import ZipFile

import duckdb
import yaml


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    with Path(path).open("rb") as stream:
        return stream_hash(stream)


def stream_hash(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def rows(db, query, params=None):
    cursor = db.execute(query, params or [])
    columns = [field[0] for field in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical", type=Path, default=ROOT / "data/processed/t14_canonical_20261017")
    parser.add_argument("--product", type=Path, default=ROOT / "artifacts/source-product")
    parser.add_argument("--archive", type=Path, default=ROOT / "data/Airlines-All.zip")
    # Preserve the original 0.01-policy audit as history.
    parser.add_argument("--output", type=Path, default=ROOT / "docs/validation/t32_marker_policy_20261020.json")
    args = parser.parse_args()
    canonical = args.canonical.resolve()
    product = args.product.resolve()
    output = args.output.resolve()
    spec = ROOT / "configs/markers_v2.yaml"
    inventory = ROOT / "scripts/dataset_manifest.json"
    canonical_manifest_path = canonical / "canonical_manifest.json"
    canonical_report_path = canonical / "canonical_report.json"
    product_manifest_path = product / "run_manifest.json"
    inputs = [args.archive.resolve(), inventory, spec, canonical_manifest_path,
              canonical_report_path, product_manifest_path, ROOT / "configs/sirena_columns.yaml",
              ROOT / "configs/product.yaml", product / "product.yaml", product / "rules.json",
              Path(__file__).resolve()]
    inputs += [canonical / (name + ".parquet") for name in ("observations", "identifiers", "travel_observations")]
    inputs += [product / "tables" / (name + ".parquet") for name in ("persons", "segments", "rankings")]
    if output in inputs or canonical in output.parents or product in output.parents:
        raise ValueError("Audit output must be separate from its source/product inputs")
    hashes_before = {str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path): sha256(path)
                     for path in inputs}
    canonical_manifest = read_json(canonical_manifest_path)
    product_manifest = read_json(product_manifest_path)
    canonical_report = read_json(canonical_report_path)
    dataset_manifest = read_json(inventory)
    settings = yaml.safe_load(spec.read_text(encoding="utf-8"))
    criteria = settings["criteria"]
    strong = criteria["D01"]
    strong_ids = {"D01", "D03", "T01"}
    baseline = {key: criteria[key] for key in ("M01", "M02", "M03")}
    weak_ids = {"L01", "P01", "M07", "D02", "N01", "B01"}
    weak = {key: criteria[key] for key in weak_ids}
    baseline_maximum = sum(rule["contribution_cap"] for rule in baseline.values())
    weak_maximum = sum(rule["contribution_cap"] for rule in weak.values())
    maximum_main = max(criteria[key]["contribution_cap"] for key in set(baseline) | strong_ids)
    weak_without_no_show = weak_maximum - weak["B01"]["contribution_cap"]
    existing_config = yaml.safe_load((ROOT / "configs/product.yaml").read_text(encoding="utf-8"))
    existing_rules = {rule["criterion_id"]: rule for rule in read_json(product / "rules.json")}
    weight_checks = {
        "strong_minimum_contribution": strong["weight"],
        "baseline_maximum_contribution": baseline_maximum,
        "weak_maximum_contribution": weak_maximum,
        "weak_sum_exceeds_any_single_main": weak_maximum > maximum_main,
        "weak_without_no_show_exceeds_any_single_main": weak_without_no_show > maximum_main,
        "maximum_single_main_contribution": maximum_main,
        "strong_cap_consistent": abs(strong["weight"] - strong["contribution_cap"]) < 1e-12
            and abs(sum(criteria[key]["contribution_cap"] for key in strong_ids)
                    - settings["strong_total_contribution_cap"]) < 1e-12,
        "baseline_cap_consistent": abs(baseline_maximum - settings["baseline_total_contribution_cap"]) < 1e-12,
        "weak_cap_consistent": abs(weak_maximum - settings["weak_total_contribution_cap"]) < 1e-12,
        "baseline_formula_caps_consistent": all(abs(rule["weight"] * 3 - rule["contribution_cap"]) < 1e-12
                                                 for rule in baseline.values()),
        "baseline_weights_unchanged": all(rule["weight"] == existing_config["criteria"][key]["weight"]
                                          == existing_rules[key]["weight"] for key, rule in baseline.items()),
        "all_requested_weak_markers_enabled_with_positive_weights": all(rule["enabled"] is True
            and rule["weight"] > 0 for rule in weak.values()),
        "approved_secondary_points": {key: rule["weight"] for key, rule in weak.items()} == {
            "L01": 0.8, "P01": 0.8, "M07": 1.8, "D02": 0.9, "N01": 0.7, "B01": 1.2},
        "exactly_twelve_unique_requested_criteria": set(criteria) == set(baseline) | strong_ids | weak_ids,
        "duplicate_behavioral_markers_not_added": settings["duplicate_list_items"] == {
            "frequent_flights": "M01", "quick_returns": "M02", "repeated_route": "M03"},
        "score_sorted_first_with_stable_tie_break": settings["ranking_order"] == ["rule_score DESC", "person_id ASC"],
        "repeat_coflight_thresholds": criteria["M07"]["min_distinct_dates"] == 3
            and criteria["M07"]["min_distinct_directed_routes"] == 2,
        "repeat_no_show_threshold": criteria["B01"]["min_distinct_events"] == 3,
        "dependent_conflicts_not_double_counted": settings["evidence_deduplication"]["document_and_ticket_same_conflict_cap"] == 2.0,
        "no_show_unavailable_instead_of_inferred": criteria["B01"]["status"] == "unavailable_on_current_dataset"
            and criteria["B01"]["missing_boarding_record_is_no_show"] is False,
        "forest_kept_separate": settings["forest"]["enabled"] is True
            and settings["forest"]["role"] == "experimental_only"
            and settings["forest"]["adds_ranking_vote"] is False,
    }
    if not all(value for key, value in weight_checks.items() if type(value) is bool):
        raise ValueError("Baseline/strong/weak weight specification is inconsistent")
    for path in inputs:
        expected = None
        if path.parent == canonical and path.suffix == ".parquet":
            expected = canonical_manifest["files"][path.name]
        elif path.parent == product / "tables":
            expected = product_manifest["files"]["tables/" + path.name]
        elif path.parent == product and path.name in {"product.yaml", "rules.json"}:
            expected = product_manifest["files"][path.name]
        if expected is not None and sha256(path) != expected:
            raise ValueError("Manifest checksum mismatch: " + str(path))
    if sha256(args.archive) != dataset_manifest["archive"]["sha256"]:
        raise ValueError("Original dataset ZIP checksum mismatch")

    with duckdb.connect() as db:
        db.execute("SET threads=2")
        db.execute("SET memory_limit='512MB'")
        for name in ("observations", "identifiers", "travel_observations"):
            db.read_parquet(str(canonical / (name + ".parquet"))).create_view(name)
        for name in ("persons", "segments", "rankings"):
            db.read_parquet(str(product / "tables" / (name + ".parquet"))).create_view(name)
        db.execute("""CREATE TEMP TABLE document_rows AS
            SELECT i.source_id,i.identifier_namespace,i.namespace_status,i.value_normalized,
                i.value,i.identifier_id,i.observation_id,i.source_record_id AS identifier_source_record_id,
                i.locator AS identifier_locator,o.source_record_id,o.locator,
                o.name_raw,o.name_normalized,o.name_latin_comparison,o.birth_date_raw,o.birth_date
            FROM identifiers i JOIN observations o USING(observation_id)
            WHERE i.kind='document' AND i.value_normalized IS NOT NULL""")
        counts = rows(db, """SELECT source_id,count(*) document_rows,
                count(DISTINCT value_normalized) document_values
            FROM document_rows GROUP BY source_id ORDER BY source_id""")
        scoped = rows(db, """SELECT source_id,identifier_namespace,value_normalized,
                count(*) evidence_rows,count(DISTINCT birth_date) birth_dates,
                count(DISTINCT name_normalized) name_variants
            FROM document_rows WHERE identifier_namespace IS NOT NULL
            GROUP BY source_id,identifier_namespace,value_normalized
            HAVING count(DISTINCT birth_date)>1 ORDER BY source_id,value_normalized""")
        keys = sorted({row["value_normalized"] for row in scoped})
        # Cross-source bundles are display/evidence groups, not assertions of
        # shared passport issuer/type or independently collected evidence.
        evidence = rows(db, """SELECT * FROM document_rows
            WHERE value_normalized IN (SELECT unnest(?::VARCHAR[]))
            ORDER BY source_id,locator,identifier_id""", [keys])
        for group in scoped:
            members = [row for row in evidence
                       if row["identifier_namespace"] == group["identifier_namespace"]
                       and row["value_normalized"] == group["value_normalized"]]
            incompatible_pairs = []
            for left, right in combinations(members, 2):
                left_name = (left["name_latin_comparison"] or "").split()
                right_name = (right["name_latin_comparison"] or "").split()
                if (left["birth_date"] and right["birth_date"]
                        and left["birth_date"] != right["birth_date"]
                        and len(left_name) >= 2 and len(right_name) >= 2
                        and min(len(left_name[0]), len(left_name[1]),
                                len(right_name[0]), len(right_name[1])) > 1
                        and left_name[0] != right_name[0] and left_name[1] != right_name[1]):
                    incompatible_pairs.append([left["observation_id"], right["observation_id"]])
            group["incompatible_name_and_birth_date_pairs"] = incompatible_pairs
            group["strict_candidate"] = bool(incompatible_pairs)
        observation_ids = sorted({row["observation_id"] for row in evidence})
        travels = rows(db, """SELECT source_id,observation_id,travel_id,locator,
                departure_local,flight_number_normalized,ticket_number_normalized,
                origin_raw,destination_raw
            FROM travel_observations
            WHERE observation_id IN (SELECT unnest(?::VARCHAR[]))
            ORDER BY source_id,locator,travel_id""", [observation_ids])
        profiles = rows(db, """SELECT p.*,r.eligibility_status,r.rank
            FROM persons p JOIN rankings r USING(person_id)
            WHERE p.anchor_observation_id IN (SELECT unnest(?::VARCHAR[]))
            ORDER BY person_id""", [observation_ids])
        segment_links = rows(db, """SELECT s.person_id,s.segment_id,s.anchor_travel_id,
                t.observation_id,t.source_id,t.locator
            FROM segments s JOIN travel_observations t ON t.travel_id=s.anchor_travel_id
            WHERE t.observation_id IN (SELECT unnest(?::VARCHAR[]))
            ORDER BY s.person_id,s.segment_id""", [observation_ids])
        historical = rows(db, """SELECT eligibility_status,count(*) profiles
            FROM rankings GROUP BY eligibility_status ORDER BY eligibility_status""")

    targets = {(row["source_id"], row["locator"]): row for row in evidence}
    source_checks = []
    source_counts = {}
    source_fingerprints = []

    def compare(source_id, locator, name, birth, document, birth_format, departure, flight, ticket):
        item = targets.get((source_id, locator))
        if item is None:
            return
        actual = {"name_raw": name, "birth_date": datetime.strptime(birth, birth_format).date().isoformat(),
                  "value": document}
        matches = {field: value == item[field] for field, value in actual.items()}
        linked_travels = [travel for travel in travels if travel["observation_id"] == item["observation_id"]]
        raw_travel = {
            "departure_local": datetime.strptime(departure, "%Y-%m-%d %H:%M").isoformat(timespec="minutes"),
            "flight_number_normalized": flight,
            "ticket_number_normalized": None if ticket.strip() in {"", "Not presented", "None", "N/A"} else ticket.strip(),
        }
        travel_matches = [{field: value == travel[field] for field, value in raw_travel.items()}
                          for travel in linked_travels]
        passed = all(matches.values()) and bool(travel_matches) and all(all(match.values()) for match in travel_matches)
        source_checks.append({"source_id": source_id, "locator": locator,
                              "fields_match": matches, "travel_fields_match": travel_matches,
                              "passed": passed})

    with ZipFile(args.archive) as archive:
        member_map = {}
        for source_id, filename in (("boarding_csv", "BoardingData.csv"), ("sirena_fixed", "Sirena-export-fixed.tab")):
            candidates = [item for item in archive.infolist() if Path(item.filename).name == filename and not item.is_dir()]
            if len(candidates) != 1:
                raise ValueError("Expected exactly one original member: " + filename)
            member = candidates[0]
            with archive.open(member) as binary:
                digest = stream_hash(binary)
            expected = dataset_manifest["files"][filename]
            if digest != expected["sha256"] or member.file_size != expected["bytes"]:
                raise ValueError("Original ZIP member checksum/size mismatch: " + filename)
            member_map[source_id] = member
            source_fingerprints.append({"source_id": source_id, "member": member.filename,
                                        "bytes": member.file_size, "sha256": digest,
                                        "matches_committed_dataset_manifest": True})
        with ExitStack() as stack:
            stream = stack.enter_context(io.TextIOWrapper(archive.open(member_map["boarding_csv"]), encoding="utf-8-sig", newline=""))
            reader = csv.reader(stream, delimiter=";")
            header = next(reader)
            valid = invalid = 0
            while True:
                first = reader.line_num + 1
                try:
                    values = next(reader)
                except StopIteration:
                    break
                if len(values) != len(header):
                    invalid += 1
                    continue
                valid += 1
                row = dict(zip(header, values))
                locator = f"line:{first}-{reader.line_num}"
                name = " ".join(row[key] for key in ("PassengerLastName", "PassengerFirstName", "PassengerSecondName") if row[key])
                compare("boarding_csv", locator, name, row["PassengerBirthDate"], row["PassengerDocument"], "%m/%d/%Y",
                        row["FlightDate"] + " " + row["FlightTime"], row["FlightNumber"], row["TicketNumber"])
            source_counts["boarding_csv"] = {"valid_width_rows": valid, "invalid_width_rows": invalid}
        columns = yaml.safe_load((ROOT / "configs/sirena_columns.yaml").read_text(encoding="utf-8"))
        boundaries = {item["name"]: (item["start"], item["end"]) for item in columns["columns"]}
        with io.TextIOWrapper(archive.open(member_map["sirena_fixed"]), encoding=columns["encoding"]) as stream:
            count = 0
            for number, line in enumerate(stream, 1):
                if number <= columns["header_lines"]:
                    continue
                count += 1
                def field(key):
                    start, end = boundaries[key]
                    return line[start:end].strip()
                compare("sirena_fixed", f"line:{number}", field("PaxName"), field("PaxBirthDate"), field("TravelDoc"), "%Y-%m-%d",
                        field("DepartDate") + " " + field("DepartTime"), field("Flight"), field("e-Ticket"))
            source_counts["sirena_fixed"] = {"data_rows": count}
    if len(source_checks) != len(evidence) or not all(item["passed"] for item in source_checks):
        raise ValueError("Not all canonical conflict rows matched original source records")

    event_keys = {}
    for row in travels:
        ticket = row["ticket_number_normalized"]
        if ticket:
            key = (ticket, row["flight_number_normalized"], row["departure_local"])
            event_keys.setdefault(key, []).append({"source_id": row["source_id"], "travel_id": row["travel_id"]})
    repeated = [{"ticket": key[0], "flight": key[1], "departure_local": key[2], "rows": value}
                for key, value in sorted(event_keys.items()) if len({row["source_id"] for row in value}) > 1]
    input_unchanged = all(sha256(path) == hashes_before[str(path.relative_to(ROOT) if path.is_relative_to(ROOT) else path)] for path in inputs)
    if not input_unchanged:
        raise ValueError("An audit input changed while it was being read")
    result = {
        "schema_version": "marker-baseline:1.0", "status": "complete_with_limitations",
        "scope": "T32 step 1: original-record audit and marker/weight specification; runtime migration pending",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(), "input_sha256": hashes_before,
        "all_inputs_unchanged": input_unchanged, "runtime_product_run_id": product_manifest["run_id"],
        "canonical_status": canonical_report["status"], "canonical_acceptance": canonical_report["acceptance"],
        "document_counts_by_source": counts, "source_fingerprints": source_fingerprints,
        "raw_source_counts": source_counts, "within_source_dob_conflict_groups": scoped,
        "strict_within_source_candidates": sum(group["strict_candidate"] for group in scoped),
        "cross_source_display_bundles": len(keys), "document_values": keys,
        "evidence": evidence, "travel_evidence": travels, "raw_record_checks": source_checks,
        "repeated_ticket_flight_departure_across_sources": repeated,
        "exact_anchor_profiles": profiles, "exact_segment_anchor_links": segment_links,
        "evidence_without_exact_product_anchor": sorted(set(observation_ids) -
            {p["anchor_observation_id"] for p in profiles} - {s["observation_id"] for s in segment_links}),
        "current_ranking_population": historical,
        "target_weights": {key: value["weight"] for key, value in settings["criteria"].items()},
        "weight_policy_checks": weight_checks, "target_ranking_order": settings["ranking_order"],
        "preserved_baseline_rules": existing_rules,
        "limitations": [
            "Conflicting document records establish a data inconsistency, not fraud, culpability or espionage.",
            "Document type and issuer are absent; source-scoped namespaces stay separate.",
            "Two files do not establish independent acquisition; matching ticket/flight/departure rows are repeated evidence.",
            "Names alone do not link evidence to frozen product people; only exact anchors are reported here.",
            "The local canonical run is complete_with_errors with human acceptance pending; full raw-to-product lineage is not established by this audit.",
            "No ground truth labels exist; weights are explicit review policy and no detection accuracy is claimed.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "scoped_conflicts": len(scoped), "display_bundles": len(keys),
                      "evidence_rows": len(evidence), "raw_checks_passed": len(source_checks),
                      "exact_profiles": len(profiles), "unchanged_inputs": input_unchanged}, ensure_ascii=False))


if __name__ == "__main__":
    main()
