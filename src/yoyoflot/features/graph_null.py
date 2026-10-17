"""Conditional null preserving each daily route cohort and every flight size.

Participants occur once per stratum. Uniform permutations into fixed flight
slots give P(pair together) = sum_f n_f(n_f-1) / (N(N-1)). Strata are shuffled
independently. Exact day-count tails and a seeded Monte Carlo diagnostic agree
under these stated assumptions; this is not a fitted behavioural model.
"""
import math
import random
from collections import defaultdict
from itertools import combinations

from ..models.demo import digest


def _logadd(a, b):
    if a == -math.inf:
        return b
    if b == -math.inf:
        return a
    high, low = max(a, b), min(a, b)
    return high + math.log1p(math.exp(low - high))


def _log_tail(probabilities, observed):
    if observed <= 0:
        return 0.0
    distribution = [0.0]
    for probability in probabilities:
        if not math.isfinite(probability) or not 0 <= probability <= 1:
            raise ValueError("Недопустимая вероятность условного фона")
        yes = math.log(probability) if probability else -math.inf
        no = math.log1p(-probability) if probability < 1 else -math.inf
        updated = [-math.inf] * (len(distribution) + 1)
        for k, value in enumerate(distribution):
            updated[k] = _logadd(updated[k], value + no)
            updated[k + 1] = _logadd(updated[k + 1], value + yes)
        distribution = updated
    result = -math.inf
    for value in distribution[observed:]:
        result = _logadd(result, value)
    return min(0.0, result)


def _repeat_probability(probabilities, minimum):
    # Accumulate the upper tail directly, avoiding 1 - CDF cancellation.
    distribution, tail = [1.0] + [0.0] * (minimum - 1), 0.0
    for p in probabilities:
        tail += distribution[-1] * p
        distribution = [distribution[0] * (1 - p)] + [
            distribution[k] * (1 - p) + distribution[k - 1] * p for k in range(1, minimum)]
    return min(1.0, tail)


def _probability(log_probability):
    value = math.exp(log_probability)
    return value if value > 0 or log_probability == -math.inf else None


def _strata(events, limits, simulations):
    strata = defaultdict(list)
    for event in events:
        if event["participant_count"] != len(set(event["participant_ids"])) or not event["participant_ids"]:
            raise ValueError("Неверный состав участников учебного события")
        if event["participant_count"] > limits["max_event_participants"]:
            raise ValueError("Превышен max_event_participants; снимок не создан")
        key = event["departure_date"], event["origin_iata"], event["destination_iata"]
        strata[key].append(event)
    pair_visits, null_visits = 0, 0
    result = []
    for key, flights in sorted(strata.items()):
        flights.sort(key=lambda r: r["event_id"])
        participants = [pid for f in flights for pid in f["participant_ids"]]
        if len(set(participants)) != len(participants):
            raise ValueError("Профиль занимает несколько разных рейсов в одной дате/направлении; нужен другой условный фон")
        n = len(participants)
        pair_visits += n * (n - 1) // 2
        slot_pairs = sum(f["participant_count"] * (f["participant_count"] - 1) // 2 for f in flights)
        null_visits += slot_pairs * simulations
        result.append((key, sorted(participants), [f["participant_count"] for f in flights],
                       2 * slot_pairs / (n * (n - 1)) if n > 1 else 0.0))
    if pair_visits > limits["max_pair_visits"] or null_visits > limits["max_null_pair_visits"]:
        raise ValueError("Превышен бюджет пар условного фона; снимок не создан")
    return result


def _permuted_flights(stratum, seed, draw):
    key, participants, widths, _ = stratum
    shuffled = participants[:]
    random.Random(digest(["daily-route-partition-1.0", seed, draw, key])).shuffle(shuffled)
    offset = 0
    for width in widths:
        yield sorted(shuffled[offset:offset + width])
        offset += width


def background(events, observed_edges, eligible_profiles, config, progress=None):
    parameters = config["null_model"]
    simulations = parameters["simulations"]
    strata = _strata(events, config["limits"], simulations)
    pair_day_parts = defaultdict(lambda: defaultdict(list))
    for key, participants, _, probability in strata:
        for pair in combinations(participants, 2):
            pair_day_parts[pair][key[0]].append(probability)
    daily = {}
    for pair, days in sorted(pair_day_parts.items()):
        daily[pair] = [1.0 if 1 in parts else -math.expm1(math.fsum(math.log1p(-p) for p in parts))
                       for _, parts in sorted(days.items())]
    family = eligible_profiles * (eligible_profiles - 1) // 2
    node = defaultdict(lambda: dict(expected_partner_days_sum=0.0, expected_repeated_companions=0.0,
                                    max_positive_pair_excess_days=0.0))
    for pair, probabilities in daily.items():
        mean = math.fsum(probabilities)
        repeated = _repeat_probability(probabilities, config["min_repeat_days"])
        observed = observed_edges.get(pair, {}).get("distinct_days", 0)
        for pid in pair:
            node[pid]["expected_partner_days_sum"] += mean
            node[pid]["expected_repeated_companions"] += repeated
            node[pid]["max_positive_pair_excess_days"] = max(node[pid]["max_positive_pair_excess_days"], observed - mean)
    stats = {pair: dict(total=0, squares=0, tail=0) for pair in observed_edges}
    for draw in range(simulations):
        days = defaultdict(set)
        for stratum in strata:
            for flight in _permuted_flights(stratum, parameters["seed"], draw):
                for pair in combinations(flight, 2):
                    if pair in stats:
                        days[pair].add(stratum[0][0])
        for pair, value in stats.items():
            count = len(days.get(pair, ()))
            value["total"] += count
            value["squares"] += count * count
            value["tail"] += count >= observed_edges[pair]["distinct_days"]
        if progress and (draw + 1) % 100 == 0:
            progress(f"T18: условный фон — {draw + 1} / {simulations}")
    for pair, edge in observed_edges.items():
        probabilities = daily[pair]
        expected = math.fsum(probabilities)
        if expected <= 0 or family < 1:
            raise ValueError("Наблюдаемое ребро несовместимо с условным фоном")
        log_p = _log_tail(probabilities, edge["distinct_days"])
        adjusted_log = min(0.0, log_p + math.log(family))
        mc = stats[pair]
        mc_mean = mc["total"] / simulations
        variance = max(0.0, (mc["squares"] - mc["total"]**2 / simulations) / (simulations - 1))
        edge.update(expected_days=expected, coactive_days=len(probabilities),
                    excess_days=edge["distinct_days"] - expected, observed_expected_ratio=edge["distinct_days"] / expected,
                    p_exact=_probability(log_p), log_p_exact=log_p,
                    p_bonferroni=_probability(adjusted_log), log_p_bonferroni=adjusted_log,
                    family_size=family, mc_mean_days=mc_mean, mc_mean_standard_error=math.sqrt(variance / simulations),
                    mc_p_plus_one=(mc["tail"] + 1) / (simulations + 1),
                    simulations=simulations, null_seed=parameters["seed"], null_version=parameters["version"])
    return dict(node), dict(strata=len(strata), opportunity_pairs=len(daily), family_size=family,
                            simulations=simulations, seed=parameters["seed"], version=parameters["version"])
