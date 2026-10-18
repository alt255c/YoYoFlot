"""Auditable coflight evidence for the replay-verified fictional T17 dataset."""
from collections import defaultdict
from datetime import date
from itertools import combinations
from pathlib import Path
import re

import pyarrow as pa
import yaml

from ..models.demo import END, START, digest
from ..models.rule_score import UniqueLoader, _number
from .graph_null import _strata, background

VERSION = "graph-demo-1.0"
META = [("run_id", pa.string()), ("data_mode", pa.string()), ("graph_version", pa.string()),
        ("window_start", pa.date32()), ("window_end", pa.date32())]
OUTPUT_SCHEMAS = {
    "graph_events.parquet": pa.schema([
        ("event_id", pa.string()), ("service_id", pa.string()), ("flight_number", pa.string()),
        ("departure_date", pa.date32()), ("origin_iata", pa.string()), ("destination_iata", pa.string()),
        ("participant_count", pa.int32()), ("participant_ids", pa.list_(pa.string())),
        ("segment_ids", pa.list_(pa.string())), ("observation_ids", pa.list_(pa.string())), *META]),
    "graph_memberships.parquet": pa.schema([
        ("event_id", pa.string()), ("profile_id", pa.string()), ("segment_ids", pa.list_(pa.string())),
        ("observation_ids", pa.list_(pa.string())), *META]),
    "graph_links.parquet": pa.schema([
        ("observation_id", pa.string()), ("profile_id", pa.string()), ("segment_id", pa.string()),
        ("canonical_service_id", pa.string()), ("event_id", pa.string()), ("graph_eligible", pa.bool_()),
        ("eligibility_reason", pa.string()), *META]),
    "coflight_edges.parquet": pa.schema([
        ("edge_id", pa.string()), ("profile_a", pa.string()), ("profile_b", pa.string()),
        ("coflight_events", pa.int32()), ("distinct_days", pa.int32()), ("repeated", pa.bool_()),
        ("event_ids", pa.list_(pa.string())), ("dates", pa.list_(pa.date32())),
        ("expected_days", pa.float64()), ("coactive_days", pa.int32()), ("excess_days", pa.float64()),
        ("observed_expected_ratio", pa.float64()), ("p_exact", pa.float64()), ("log_p_exact", pa.float64()),
        ("p_bonferroni", pa.float64()), ("log_p_bonferroni", pa.float64()), ("family_size", pa.int64()),
        ("mc_mean_days", pa.float64()), ("mc_mean_standard_error", pa.float64()), ("mc_p_plus_one", pa.float64()),
        ("simulations", pa.int32()), ("null_seed", pa.int64()), ("null_version", pa.string()), *META]),
    "graph_features.parquet": pa.schema([
        ("profile_id", pa.string()), ("available", pa.bool_()), ("availability_reason", pa.string()),
        ("considered_segments", pa.int32()), ("candidate_segments", pa.int32()),
        ("eligible_segments", pa.int32()), ("eligible_events", pa.int32()),
        ("graph_coverage", pa.float64()), ("source_coverage", pa.float64()), ("covered_days", pa.int32()),
        ("companion_count", pa.int32()), ("repeated_companion_count", pa.int32()),
        ("observed_partner_days_sum", pa.float64()), ("expected_partner_days_sum", pa.float64()),
        ("expected_repeated_companions", pa.float64()), ("excess_repeated_companions", pa.float64()),
        ("max_positive_pair_excess_days", pa.float64()), ("m07_status", pa.string()), ("m07_reason", pa.string()), *META]),
}


def load_graph_config(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("Слишком большая конфигурация T18")
    raw = path.read_bytes()
    config = yaml.load(raw, Loader=UniqueLoader)
    if (not isinstance(config, dict) or set(config) != {
            "version", "data_mode", "as_of", "graph_seed", "min_source_coverage", "min_graph_fraction",
            "min_covered_days", "min_repeat_days", "null_model", "limits"} or
            config["version"] != VERSION or config["data_mode"] != "synthetic_demo"):
        raise ValueError("Нужна конфигурация graph-demo-1.0 только для synthetic_demo")
    try:
        cutoff = date.fromisoformat(config["as_of"])
    except (ValueError, TypeError) as exc:
        raise ValueError("as_of должен быть строкой YYYY-MM-DD") from exc
    if cutoff.isoformat() != config["as_of"] or not START <= cutoff <= END:
        raise ValueError("as_of должен находиться в известном учебном окне")
    _number(config["graph_seed"], 0, 2**32 - 1, "graph_seed", integer=True)
    for key in ["min_source_coverage", "min_graph_fraction"]:
        _number(config[key], 0.01, 1, key)
    _number(config["min_covered_days"], 1, 90, "min_covered_days", integer=True)
    _number(config["min_repeat_days"], 2, 10, "min_repeat_days", integer=True)
    null = config["null_model"]
    if (not isinstance(null, dict) or set(null) != {"version", "seed", "simulations"} or
            null["version"] != "daily-route-partition-1.0"):
        raise ValueError("Неизвестная версия условного фона")
    _number(null["seed"], 0, 2**32 - 1, "null seed", integer=True)
    _number(null["simulations"], 19, 1999, "simulations", integer=True)
    limits = config["limits"]
    caps = {"max_event_participants": 128, "max_pair_visits": 1000000, "max_null_pair_visits": 20000000}
    if not isinstance(limits, dict) or set(limits) != set(caps):
        raise ValueError("Неизвестные ограничения ресурсов T18")
    for key, maximum in caps.items():
        _number(limits[key], 1, maximum, key, integer=True)
    return config, raw


def _unique(rows, key):
    result = {}
    for row in rows:
        identifier = row[key]
        if not isinstance(identifier, str) or not identifier:
            raise ValueError(f"Пустой ключ {key}")
        if identifier in result and row != result[identifier]:
            raise ValueError(f"Противоречивые дубли {key}: {identifier}")
        result[identifier] = row
    return result


def _codeshares(rows):
    rules = _unique(rows, "rule_id")
    marketing = set()
    for row in rules.values():
        if (any(not isinstance(row[k], str) or not row[k].strip()
                for k in ["marketing_service_id", "operating_service_id", "evidence_ru"]) or
                not isinstance(row["departure_date"], date) or
                any(not _flight_number(row[k]) for k in ["marketing_flight_number", "operating_flight_number"]) or
                any(not isinstance(row[k], str) or not re.fullmatch(r"[A-Z]{3}", row[k])
                    for k in ["origin_iata", "destination_iata"]) or
                row["origin_iata"] == row["destination_iata"] or
                row["marketing_service_id"] == row["operating_service_id"] or row["marketing_service_id"] in marketing):
            raise ValueError("Неверное или неоднозначное учебное правило codeshare")
        marketing.add(row["marketing_service_id"])
    if any(row["operating_service_id"] in marketing for row in rules.values()):
        raise ValueError("Цепочки и циклы codeshare не разрешены")
    return rules


def _flight_number(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Z0-9]{2,3}[0-9]{1,5}", value) is not None


def _canonical(row, rules):
    if row["operating_service_id"] or row["operating_flight_number"] or row["codeshare_rule_id"]:
        rule = rules.get(row["codeshare_rule_id"])
        expected = dict(marketing_service_id=row["service_id"], operating_service_id=row["operating_service_id"],
                        marketing_flight_number=row["flight_number"], operating_flight_number=row["operating_flight_number"],
                        departure_date=row["departure_date"], origin_iata=row["origin_iata"], destination_iata=row["destination_iata"])
        if not rule or any(rule[key] != value for key, value in expected.items()):
            return None, None, "unverified_codeshare"
        return rule["operating_service_id"], rule["operating_flight_number"], None
    return row["service_id"], row["flight_number"], None


def _prepare(profiles, segments, observations, rule_rows, config):
    people, journeys = _unique(profiles, "profile_id"), _unique(segments, "segment_id")
    rules, witnesses = _codeshares(rule_rows), _unique(observations, "observation_id")
    if any(r["profile_id"] not in people for r in journeys.values()):
        raise ValueError("Сегмент без учебного профиля")
    cutoff = date.fromisoformat(config["as_of"])
    links, candidates = {}, {}
    for oid, row in sorted(witnesses.items()):
        segment = journeys.get(row["segment_id"])
        if segment is None or row["profile_id"] != segment["profile_id"]:
            raise ValueError("Неверная связь observation → segment → profile")
        reason = None
        sid, flight, codeshare_reason = _canonical(row, rules)
        if row["identity_status"] != "certain" or row["resolution_status"] != "resolved":
            reason = "ambiguous_identity_or_resolution"
        elif row["event_kind"] != "fictional_boarding_record" or row["participation_status"] != "recorded":
            reason = "participation_not_confirmed"
        elif not row["source_id"] or not row["source_record_id"]:
            reason = "missing_provenance"
        elif any(row[k] != segment[k] for k in ["departure_date", "origin_iata", "destination_iata"]):
            reason = "segment_metadata_mismatch"
        elif not row["departure_date"] or not row["origin_iata"] or not row["destination_iata"]:
            reason = "incomplete_event_key"
        elif segment["departure_precision"] != "day":
            reason = "unknown_date_precision"
        elif not START <= row["departure_date"] <= cutoff:
            reason = "outside_window"
        elif codeshare_reason:
            reason = codeshare_reason
        elif (not isinstance(sid, str) or not sid or not _flight_number(flight) or
              any(not isinstance(row[k], str) or not re.fullmatch(r"[A-Z]{3}", row[k])
                  for k in ["origin_iata", "destination_iata"]) or row["origin_iata"] == row["destination_iata"]):
            reason = "incomplete_event_key"
        links[oid] = dict(observation_id=oid, profile_id=row["profile_id"], segment_id=row["segment_id"],
                          canonical_service_id=sid, event_id=None, graph_eligible=False, eligibility_reason=reason)
        if not reason:
            candidates[oid] = (sid, row["departure_date"], row["origin_iata"], row["destination_iata"], flight)
    service_keys, segment_keys = defaultdict(set), defaultdict(set)
    for oid, key in candidates.items():
        service_keys[key[0]].add(key)
        segment_keys[witnesses[oid]["segment_id"]].add(key)
    for oid, key in list(candidates.items()):
        if len(service_keys[key[0]]) > 1 or len(segment_keys[witnesses[oid]["segment_id"]]) > 1:
            links[oid]["eligibility_reason"] = "conflicting_event_evidence"
            del candidates[oid]
    usable = {witnesses[oid]["segment_id"] for oid in candidates}
    by_profile = defaultdict(list)
    for row in journeys.values():
        # Future rows never enter support/coverage denominators.
        if row["departure_date"] is None or row["departure_precision"] != "day" or START <= row["departure_date"] <= cutoff:
            by_profile[row["profile_id"]].append(row)
    coverage = {}
    for pid, profile in sorted(people.items()):
        rows = by_profile[pid]
        count = sum(row["segment_id"] in usable for row in rows)
        fraction = count / len(rows) if rows else 0.0
        days = (min(cutoff, profile["window_end"]) - profile["window_start"]).days + 1
        reason = None
        if profile["identity_status"] != "certain":
            reason = "Неоднозначная идентичность учебного профиля."
        elif profile["source_coverage"] < config["min_source_coverage"]:
            reason = "Недостаточное покрытие источника."
        elif days < config["min_covered_days"]:
            reason = "Известное окно короче установленного минимума."
        elif count == 0 or fraction < config["min_graph_fraction"]:
            reason = "Недостаточно допустимых учебных событий."
        coverage[pid] = dict(profile_id=pid, available=reason is None, availability_reason=reason,
                             considered_segments=len(rows), candidate_segments=count,
                             eligible_segments=count if reason is None else 0, graph_coverage=fraction,
                             source_coverage=profile["source_coverage"], covered_days=days)
    groups = defaultdict(list)
    for oid, key in sorted(candidates.items()):
        link = links[oid]
        if not coverage[link["profile_id"]]["available"]:
            link["eligibility_reason"] = "profile_not_comparable"
            continue
        event_id = "demo_event_" + digest([VERSION, key])[:24]
        link.update(event_id=event_id, graph_eligible=True, eligibility_reason=None)
        groups[key].append(oid)
    events, memberships = [], []
    for key, oids in sorted(groups.items()):
        sid, day, origin, destination, flight = key
        event_id = links[oids[0]]["event_id"]
        members = defaultdict(list)
        for oid in oids:
            members[witnesses[oid]["profile_id"]].append(oid)
        for pid, evidence in sorted(members.items()):
            memberships.append(dict(event_id=event_id, profile_id=pid, observation_ids=sorted(evidence),
                                    segment_ids=sorted({witnesses[o]["segment_id"] for o in evidence})))
        events.append(dict(event_id=event_id, service_id=sid, departure_date=day, origin_iata=origin,
                           destination_iata=destination, flight_number=flight, participant_ids=sorted(members),
                           participant_count=len(members), observation_ids=sorted(oids),
                           segment_ids=sorted({witnesses[o]["segment_id"] for o in oids})))
    return (sorted(events, key=lambda r: r["event_id"]), sorted(memberships, key=lambda r: (r["event_id"], r["profile_id"])),
            list(links.values()), coverage)


def _build_graph(profiles, segments, observations, codeshares, config, progress=None):
    """Internal arithmetic; graph_artifact.run enforces the external replay gate."""
    events, memberships, links, coverage = _prepare(profiles, segments, observations, codeshares, config)
    _strata(events, config["limits"], config["null_model"]["simulations"])
    edges = {}
    for event in events:
        for pair in combinations(event["participant_ids"], 2):
            if pair not in edges:
                edges[pair] = dict(profile_a=pair[0], profile_b=pair[1], event_ids=[], dates=set())
            edges[pair]["event_ids"].append(event["event_id"])
            edges[pair]["dates"].add(event["departure_date"])
    for pair, edge in edges.items():
        edge.update(edge_id="demo_edge_" + digest([VERSION, pair])[:24],
                    coflight_events=len(edge["event_ids"]), distinct_days=len(edge["dates"]),
                    repeated=len(edge["dates"]) >= config["min_repeat_days"])
        edge["event_ids"].sort()
        edge["dates"] = sorted(edge["dates"])
    if progress:
        progress(f"T18: событий — {len(events)}, пар — {len(edges)}; расчёт условного фона")
    node_background, null_report = background(events, edges, sum(c["available"] for c in coverage.values()), config, progress)
    incident = defaultdict(list)
    event_count = defaultdict(int)
    for row in memberships:
        event_count[row["profile_id"]] += 1
    for edge in edges.values():
        for pid in [edge["profile_a"], edge["profile_b"]]:
            incident[pid].append(edge)
    features = []
    for pid, row in coverage.items():
        current = incident[pid]
        repeated = sum(e["repeated"] for e in current)
        expected = node_background.get(pid, {})
        values = dict(companion_count=len(current), repeated_companion_count=repeated,
                      observed_partner_days_sum=float(sum(e["distinct_days"] for e in current)),
                      expected_partner_days_sum=expected.get("expected_partner_days_sum", 0.0),
                      expected_repeated_companions=expected.get("expected_repeated_companions", 0.0),
                      excess_repeated_companions=repeated - expected.get("expected_repeated_companions", 0.0),
                      max_positive_pair_excess_days=expected.get("max_positive_pair_excess_days", 0.0))
        features.append(dict(row, eligible_events=event_count[pid],
                             **{key: value if row["available"] else None for key, value in values.items()},
                             m07_status="disabled", m07_reason="Порог M07 и устойчивость графовой группы ожидают проверки T20."))
    return {"graph_events.parquet": events, "graph_memberships.parquet": memberships, "graph_links.parquet": links,
            "coflight_edges.parquet": [edges[p] for p in sorted(edges)], "graph_features.parquet": features}, null_report
