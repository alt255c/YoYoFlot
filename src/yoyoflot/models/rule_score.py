"""Three independent teaching criteria with explicit availability and evidence."""
import math
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import yaml

from .demo import digest, validate_demo

CRITERIA = {
    "M01": ("activity", "Частота перелётов", "trip_rate_30_days", "перелётов / 30 дней"),
    "M02": ("quick_return", "Быстрые возвраты", "quick_return_share", "доля соседних пар"),
    "M03": ("repetition", "Повторяемость маршрутов", "repeated_route_share", "доля повторов"),
    "M04": ("activity", "Всплески активности", None, None),
    "M05": ("sequence", "Повторяемые цепочки", None, None),
    "M06": ("rarity", "Редкость маршрутов", None, None),
    "M07": ("coflight", "Повторные совместные перелёты", None, None),
}
DISABLED_REASON = {
    "M04": "Нужны сопоставимые прошлые окна и проверка T20; группа общая с M01.",
    "M05": "Нужна отдельная проверка полноты цепочек и T20.",
    "M06": "Не определены эталонная совокупность, сглаживание и минимальная поддержка.",
    "M07": "Нужны T18, поправка на популярность рейсов и проверка T20.",
}


class UniqueLoader(yaml.SafeLoader):
    pass


def _unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f"Повторный ключ конфигурации: {key}")
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


def _number(value, low, high, label, integer=False):
    if (type(value) not in ({int} if integer else {int, float}) or
            not math.isfinite(value) or not low <= value <= high):
        raise ValueError(f"Недопустимый параметр {label}")


def load_model(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("Слишком большая конфигурация T17")
    raw = path.read_bytes()
    config = yaml.load(raw, Loader=UniqueLoader)
    if (not isinstance(config, dict) or
            set(config) != {"version", "data_mode", "threshold_status", "coverage", "criteria"} or
            config["version"] != "rules-demo-1.0" or config["data_mode"] != "synthetic_demo" or
            config["threshold_status"] != "illustrative_unvalidated"):
        raise ValueError("Нужна версия rules-demo-1.0 только для synthetic_demo")
    coverage = config["coverage"]
    if not isinstance(coverage, dict) or set(coverage) != {
            "min_source_coverage", "min_known_fraction", "min_covered_days", "min_available_groups"}:
        raise ValueError("Неизвестные параметры покрытия")
    for key in ["min_source_coverage", "min_known_fraction"]:
        _number(coverage[key], 0.01, 1, key)
    _number(coverage["min_covered_days"], 1, 366, "min_covered_days", integer=True)
    _number(coverage["min_available_groups"], 1, 3, "min_available_groups", integer=True)
    if not isinstance(config["criteria"], list):
        raise ValueError("criteria должен быть списком")
    seen, enabled_groups = set(), set()
    for criterion in config["criteria"]:
        if not isinstance(criterion, dict):
            raise ValueError("Неверная запись критерия")
        cid = criterion.get("id")
        if cid not in CRITERIA or cid in seen or type(criterion.get("enabled")) is not bool:
            raise ValueError("Неизвестный/повторный критерий или неверный enabled")
        seen.add(cid)
        if criterion["enabled"]:
            if cid not in {"M01", "M02", "M03"}:
                raise ValueError(f"{cid} ещё не реализован и не может быть включён")
            if set(criterion) != {"id", "enabled", "threshold", "weight", "min_observations"}:
                raise ValueError(f"Неизвестные параметры {cid}")
            _number(criterion["threshold"], 0.000001, 1000000 if cid == "M01" else 1, cid + ".threshold")
            _number(criterion["weight"], 0.000001, 100, cid + ".weight")
            _number(criterion["min_observations"], 1, 10000, cid + ".min_observations", integer=True)
            enabled_groups.add(CRITERIA[cid][0])
        elif set(criterion) != {"id", "enabled"}:
            raise ValueError(f"Для выключенного {cid} допустимы только id и enabled")
    if seen != set(CRITERIA):
        raise ValueError("Нужны явные статусы всех M01–M07")
    # Every eligible profile is compared using the same configured groups.
    if coverage["min_available_groups"] != len(enabled_groups):
        raise ValueError("min_available_groups должен равняться числу включённых независимых групп")
    config["criteria"].sort(key=lambda c: c["id"])
    return config, raw


def _unique_segments(rows):
    unique = {}
    for row in rows:
        key = row["segment_id"]
        if key in unique and unique[key] != row:
            raise ValueError(f"Противоречивые дубли сегмента {key}")
        unique[key] = row
    return list(unique.values())


def _facts(profile, rows, coverage):
    start, end = profile["window_start"], profile["window_end"]
    if not isinstance(start, date) or not isinstance(end, date) or end < start:
        raise ValueError("Некорректное окно наблюдения")
    days = (end - start).days + 1
    rows = _unique_segments(rows)
    undated = [r for r in rows if r["departure_date"] is None or r["departure_precision"] != "day"]
    dated = sorted([r for r in rows if r not in undated and start <= r["departure_date"] <= end],
                   key=lambda r: (r["departure_date"], r["segment_id"]))
    routes = [r for r in dated if r["origin_iata"] and r["destination_iata"]]
    date_counts = Counter(r["departure_date"] for r in dated)
    pairs = [(a, b) for a, b in zip(dated, dated[1:])
             if date_counts[a["departure_date"]] == date_counts[b["departure_date"]] == 1
             and a["origin_iata"] and a["destination_iata"] and b["origin_iata"] and b["destination_iata"]]
    returns = [(a, b) for a, b in pairs if a["origin_iata"] == b["destination_iata"]
               and a["destination_iata"] == b["origin_iata"]
               and 0 < (b["departure_date"] - a["departure_date"]).days <= 2]
    date_fraction = len(dated) / (len(dated) + len(undated)) if dated or undated else 0.0
    route_fraction = len(routes) / len(dated) if dated else 0.0
    pair_fraction = len(pairs) / max(len(dated) - 1, 1)
    unique_routes = len({(r["origin_iata"], r["destination_iata"]) for r in routes})
    base = None
    if profile["identity_status"] != "certain":
        base = "Неоднозначная связь учебных наблюдений с профилем."
    elif profile["source_coverage"] < coverage["min_source_coverage"]:
        base = "Покрытие источника ниже установленного минимума."
    elif days < coverage["min_covered_days"]:
        base = "Известное окно наблюдения короче установленного минимума."
    elif date_fraction < coverage["min_known_fraction"]:
        base = "Недостаточно наблюдений с известной датой в окне."
    values = {
        "M01": dict(value=len(dated) * 30 / days if dated else None, used=len(dated),
                    evidence=[r["segment_id"] for r in dated], reason=base),
        "M02": dict(value=len(returns) / len(pairs) if pairs else None, used=len(pairs),
                    evidence=sorted({r["segment_id"] for pair in pairs for r in pair}),
                    reason=base or ("Есть поездки без даты: соседство событий не установлено." if undated else
                                    "Недостаточно однозначно упорядоченных пар с известным маршрутом."
                                    if pair_fraction < coverage["min_known_fraction"] else None)),
        "M03": dict(value=1 - unique_routes / len(routes) if routes else None, used=len(routes),
                    evidence=[r["segment_id"] for r in routes],
                    reason=base or ("Недостаточно поездок с известными обоими аэропортами."
                                    if route_fraction < coverage["min_known_fraction"] else None)),
    }
    metadata = dict(window_start=start, window_end=end, covered_days=days, source_coverage=profile["source_coverage"],
                    total_segments=len(rows), dated_segments=len(dated), known_route_segments=len(routes),
                    comparable_pairs=len(pairs), quick_return_pairs=len(returns), unique_known_routes=unique_routes)
    return values, metadata


def group_totals(explanations):
    """One vote per group, and at most the largest matched weight per group."""
    contributions = {}
    for row in explanations:
        if row["status"] == "matched":
            group = row["criterion_group"]
            contributions[group] = max(contributions.get(group, 0.0), row["contribution"])
    return len(contributions), round(math.fsum(contributions.values()), 12)


def _ru(value):
    return f"{value:.6f}".rstrip("0").rstrip(".").replace(".", ",")


def _score_profiles(profiles, segments, config):
    """Internal arithmetic; external callers must use score_demo's replay gate."""
    by_profile = defaultdict(list)
    known = {p["profile_id"] for p in profiles}
    if len(known) != len(profiles) or any(s["profile_id"] not in known for s in segments):
        raise ValueError("Повторные профили или сегменты без профиля")
    for row in segments:
        by_profile[row["profile_id"]].append(row)
    scores, explanations, features, availability = [], [], [], []
    config_hash = digest(config)
    for profile in sorted(profiles, key=lambda p: p["profile_id"]):
        pid = profile["profile_id"]
        facts, metadata = _facts(profile, by_profile[pid], config["coverage"])
        current = []
        feature_row = dict(profile_id=pid, **metadata)
        for criterion in config["criteria"]:
            cid = criterion["id"]
            group, title, feature, unit = CRITERIA[cid]
            fact = facts.get(cid, {})
            value, reason = fact.get("value"), fact.get("reason")
            status = "disabled"
            if criterion["enabled"]:
                if not reason and (value is None or fact["used"] < criterion["min_observations"]):
                    reason = "Недостаточно пригодных наблюдений для этого критерия."
                status = "unavailable" if reason else "matched" if value >= criterion["threshold"] else "not_matched"
            else:
                reason = DISABLED_REASON.get(cid, "Критерий выключен в конфигурации.")
            available = status in {"matched", "not_matched"}
            value = round(value, 12) if available else None
            threshold = float(criterion["threshold"]) if criterion["enabled"] else None
            window = f"{metadata['window_start'].isoformat()}–{metadata['window_end'].isoformat()}"
            if available:
                text = (f"{title}: {_ru(value)} {unit}; условие ≥ {_ru(threshold)}; окно {window}. "
                        f"Пригодных наблюдений: {fact['used']}. "
                        + ("Учебный критерий сработал." if status == "matched" else "Учебный критерий не сработал.")
                        + " Возможные обычные объяснения: работа, регулярные поездки или сезонность.")
            else:
                text = f"{title}: {'недоступен' if criterion['enabled'] else 'выключен'}; окно {window}. {reason}"
            contribution = float(criterion["weight"]) if status == "matched" else 0.0
            row = dict(profile_id=pid, criterion_id=cid, criterion_group=group, criterion_version=config["version"],
                       title_ru=title, feature_name=feature, status=status, value=value, unit=unit,
                       operator="gte" if criterion["enabled"] else None, threshold=threshold,
                       min_observations=criterion.get("min_observations"), used_observations=fact.get("used", 0),
                       availability_reason=reason, contribution=contribution, group_contribution=contribution,
                       explanation_ru=text, evidence_segment_ids=sorted(fact.get("evidence", [])),
                       config_hash=config_hash, window_start=metadata["window_start"], window_end=metadata["window_end"])
            current.append(row)
            if feature:
                feature_row[feature] = value
                availability.append(dict(profile_id=pid, feature_name=feature, available=available,
                                         used_observations=fact["used"], reason=reason, feature_version=config["version"]))
        matched, score = group_totals(current)
        available_groups = len({r["criterion_group"] for r in current if r["status"] in {"matched", "not_matched"}})
        enabled_groups = len({CRITERIA[c["id"]][0] for c in config["criteria"] if c["enabled"]})
        sufficient = available_groups >= config["coverage"]["min_available_groups"]
        eligibility = "insufficient_data" if not sufficient else "eligible" if matched else "no_matches"
        summary = (f"Сработало независимых групп: {matched}; доступны {available_groups} из {enabled_groups}. "
                   + ("Данных недостаточно для сопоставимого рейтинга." if not sufficient else
                      "Срабатываний нет." if not matched else "Положение определяется числом групп, затем баллом правил."))
        scores.append(dict(profile_id=pid, display_name=profile["display_name"], matched_criteria_count=matched,
                           available_criteria_count=available_groups, enabled_criteria_count=enabled_groups,
                           rule_score=score, eligibility_status=eligibility, explanation_ru=summary,
                           window_start=metadata["window_start"], window_end=metadata["window_end"],
                           source_coverage=metadata["source_coverage"], model_version=config["version"]))
        # If future criteria share a group, only its largest contribution is recorded once.
        for group in {r["criterion_group"] for r in current}:
            group_rows = sorted((r for r in current if r["criterion_group"] == group),
                                key=lambda r: (-r["contribution"], r["criterion_id"]))
            for row in group_rows[1:]:
                row["group_contribution"] = 0.0
        explanations.extend(current)
        features.append(feature_row)
    return scores, explanations, features, availability


def score_demo(directory, config):
    manifest, profiles, segments = validate_demo(directory)
    return manifest, _score_profiles(profiles, segments, config)
