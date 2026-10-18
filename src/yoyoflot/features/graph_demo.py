"""Explicit fictional flight instances over replay-verified T17 journeys.

This generator adds teaching evidence only. Matching a date and a route does
not assert that two journeys are one flight; a generated service ID is required.
No imported real records or passenger identifiers are read here.
"""
import random
from collections import defaultdict

import pyarrow as pa

from ..models.demo import digest

GENERATOR = "independent-coflight-evidence-1.0"
OBSERVATION_SCHEMA = pa.schema([
    ("observation_id", pa.string()), ("source_id", pa.string()), ("source_record_id", pa.string()),
    ("segment_id", pa.string()), ("profile_id", pa.string()), ("departure_date", pa.date32()),
    ("origin_iata", pa.string()), ("destination_iata", pa.string()),
    ("service_id", pa.string()), ("flight_number", pa.string()),
    ("operating_service_id", pa.string()), ("operating_flight_number", pa.string()),
    ("codeshare_rule_id", pa.string()), ("identity_status", pa.string()),
    ("resolution_status", pa.string()), ("participation_status", pa.string()), ("event_kind", pa.string()),
])
RULE_SCHEMA = pa.schema([
    ("rule_id", pa.string()), ("marketing_service_id", pa.string()), ("operating_service_id", pa.string()),
    ("marketing_flight_number", pa.string()), ("operating_flight_number", pa.string()),
    ("departure_date", pa.date32()), ("origin_iata", pa.string()), ("destination_iata", pa.string()),
    ("evidence_ru", pa.string()),
])


def _generate_observations(profiles, segments, seed):
    cohorts = defaultdict(list)
    by_profile = defaultdict(list)
    for row in sorted(segments, key=lambda r: r["segment_id"]):
        by_profile[row["profile_id"]].append(row)
        if row["departure_date"] and row["origin_iata"] and row["destination_iata"]:
            cohorts[row["departure_date"], row["origin_iata"], row["destination_iata"]].append(row)
    identical = defaultdict(list)
    for profile in sorted(profiles, key=lambda p: p["profile_id"]):
        rows = by_profile[profile["profile_id"]]
        if profile["source_coverage"] == 1.0 and len(rows) == 18 and all(
                r["departure_date"] and r["origin_iata"] and r["destination_iata"] for r in rows):
            itinerary = tuple((r["departure_date"], r["origin_iata"], r["destination_iata"]) for r in rows)
            identical[itinerary].append(profile["profile_id"])
    candidates = sorted(identical.values(), key=lambda ids: (-len(ids), ids))
    planted = candidates[0][:2] if candidates and len(candidates[0]) >= 3 else []
    popular_key = next((key for key in sorted(cohorts) if len(cohorts[key]) >= 6), None)
    assignments, services = {}, {}
    for key, rows in sorted(cohorts.items()):
        ordered = sorted(rows, key=lambda r: r["profile_id"])
        random.Random(digest([GENERATOR, seed, key])).shuffle(ordered)
        selected = [r for r in ordered if r["profile_id"] in planted]
        if len(selected) == 2:
            ordered = sorted(selected, key=lambda r: r["profile_id"]) + [r for r in ordered if r not in selected]
        # One crowded teaching flight; other explicit flights have three seats.
        width = min(20, len(ordered)) if key == popular_key else 3
        batches = [ordered[:width]] + [ordered[i:i + 3] for i in range(width, len(ordered), 3)]
        for i, batch in enumerate(batches):
            service_id = "demo_service_" + digest([GENERATOR, seed, key, i])[:24]
            flight = f"DD{i + 1:04d}"
            services[service_id] = dict(departure_date=key[0], origin_iata=key[1], destination_iata=key[2],
                                        operating_flight_number=flight)
            for row in batch:
                assignments[row["segment_id"]] = service_id, flight
    observations, rules = [], {}
    for i, row in enumerate(sorted(segments, key=lambda r: r["segment_id"])):
        sid, flight = assignments.get(row["segment_id"], (None, None))
        oid = "demo_observation_" + digest([GENERATOR, seed, row, sid, flight, "boarding"])[:24]
        observation = dict(observation_id=oid, source_id="demo_boarding_a", source_record_id=oid,
                           segment_id=row["segment_id"], profile_id=row["profile_id"],
                           departure_date=row["departure_date"], origin_iata=row["origin_iata"],
                           destination_iata=row["destination_iata"], service_id=sid, flight_number=flight,
                           operating_service_id=None, operating_flight_number=None, codeshare_rule_id=None,
                           identity_status="certain", resolution_status="resolved", participation_status="recorded",
                           event_kind="fictional_boarding_record")
        observations.append(observation)
        if sid and i % 11 == 0:
            rule_id = "demo_codeshare_" + digest(sid)[:20]
            alias = "demo_marketing_" + digest(sid)[:24]
            rules[rule_id] = dict(rule_id=rule_id, marketing_service_id=alias, operating_service_id=sid,
                                  marketing_flight_number="DM" + flight[2:], evidence_ru="Явное правило вымышленного генератора.",
                                  **services[sid])
            aid = oid + "_codeshare"
            observations.append(dict(observation, observation_id=aid, source_record_id=aid, source_id="demo_boarding_b",
                                     service_id=alias, flight_number="DM" + flight[2:], operating_service_id=sid,
                                     operating_flight_number=flight, codeshare_rule_id=rule_id))
        for divisor, kind, identity in [(29, "fictional_reservation_record", "certain"),
                                        (31, "fictional_schedule_reference", "certain"),
                                        (37, "fictional_boarding_record", "ambiguous")]:
            if i % divisor == 0:
                extra = oid + f"_excluded_{divisor}"
                observations.append(dict(observation, observation_id=extra, source_record_id=extra,
                                         source_id="demo_reported", event_kind=kind, identity_status=identity,
                                         participation_status="reported" if kind != "fictional_boarding_record" else "recorded"))
    return (sorted(observations, key=lambda r: r["observation_id"]), sorted(rules.values(), key=lambda r: r["rule_id"]),
            dict(generator=GENERATOR, seed=seed, planted_pair=planted,
                 popular_stratum=[str(v) for v in popular_key] if popular_key else None))
