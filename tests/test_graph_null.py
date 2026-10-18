import copy
import math
from collections import Counter
from datetime import date
from fractions import Fraction
from itertools import combinations, permutations, product
from pathlib import Path

import pytest

from yoyoflot.features.graph import load_graph_config
from yoyoflot.features.graph_null import (_log_tail, _permuted_flights, _probability,
                                         _repeat_probability, _strata, background)

MODEL = Path(__file__).resolve().parents[1] / "configs/graph.yaml"


def event(key, members, day=1, route=("SVO", "LED")):
    return dict(event_id=key, participant_ids=sorted(members), participant_count=len(members),
                departure_date=date(2026, 1, day), origin_iata=route[0], destination_iata=route[1])


def observed(pair, days):
    return {tuple(sorted(pair)): dict(distinct_days=days)}


@pytest.mark.parametrize("probabilities", [[Fraction(1, 3)] * 3,
                                          [Fraction(1, 10), Fraction(1, 2), Fraction(9, 10)],
                                          [Fraction(0), Fraction(1), Fraction(1, 7)], []])
def test_log_tail_matches_independent_exhaustive_bernoulli_enumeration(probabilities):
    # Oracle enumerates elementary outcomes with rational arithmetic, no DP.
    for minimum in range(len(probabilities) + 2):
        exact = Fraction(0)
        for outcomes in product([0, 1], repeat=len(probabilities)):
            if sum(outcomes) >= minimum:
                value = Fraction(1)
                for p, yes in zip(probabilities, outcomes):
                    value *= p if yes else 1 - p
                exact += value
        actual = _probability(_log_tail([float(p) for p in probabilities], minimum))
        assert actual == pytest.approx(float(exact), abs=1e-15)
        if minimum >= 1:
            assert _repeat_probability([float(p) for p in probabilities], minimum) == pytest.approx(float(exact), abs=1e-15)


def test_rare_tail_keeps_log_and_never_reports_rounded_zero_as_exact():
    log_p = _log_tail([1e-5] * 90, 90)
    assert math.isfinite(log_p) and log_p == pytest.approx(90 * math.log(1e-5))
    assert _probability(log_p) is None
    assert _repeat_probability([1e-9] * 3, 2) == pytest.approx(3e-18 - 2e-27, rel=1e-12)


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_null_probabilities_fail(bad):
    with pytest.raises(ValueError):
        _log_tail([bad], 1)


def test_flight_probability_matches_exhaustive_seat_permutations():
    config, _ = load_graph_config(MODEL)
    flights = [event("a", ["a", "b"]), event("b", ["c", "d"])]
    stratum = _strata(flights, config["limits"], 19)[0]
    numerator = 0
    for arrangement in permutations(["a", "b", "c", "d"]):
        together = any({"a", "b"} <= set(arrangement[i:i + 2]) for i in [0, 2])
        numerator += together
    assert Fraction(numerator, math.factorial(4)) == Fraction(1, 3)
    assert stratum[3] == pytest.approx(1 / 3)


def test_permutations_preserve_each_cohort_activity_and_every_event_size():
    config, _ = load_graph_config(MODEL)
    strata = _strata([event("a", ["a", "b", "c"]), event("b", ["d"]), event("c", ["e", "f"])], config["limits"], 19)
    for draw in range(40):
        flights = list(_permuted_flights(strata[0], 17, draw))
        assert list(map(len, flights)) == [3, 1, 2]
        assert Counter(pid for f in flights for pid in f) == Counter("abcdef")
        assert flights == list(_permuted_flights(strata[0], 17, draw))
    assert list(_permuted_flights(strata[0], 17, 0)) != list(_permuted_flights(strata[0], 18, 0))


def test_exact_background_for_three_repeated_days_and_full_pair_family():
    config, _ = load_graph_config(MODEL)
    events = [event(str(day) + group, members, day) for day in [1, 2, 3]
              for group, members in [("a", ["a", "b"]), ("b", ["c", "d"])]]
    edges = observed(("a", "b"), 3)
    nodes, report = background(events, edges, 4, config)
    row = edges["a", "b"]
    assert row["expected_days"] == pytest.approx(1)
    assert row["p_exact"] == pytest.approx(1 / 27)
    assert row["p_bonferroni"] == pytest.approx(6 / 27)
    assert row["family_size"] == 6  # Not just the one observed edge supplied here.
    assert row["excess_days"] == pytest.approx(2)
    assert abs(row["mc_mean_days"] - 1) < 5 * row["mc_mean_standard_error"]
    assert 0 < row["mc_p_plus_one"] <= 1
    assert nodes["a"]["expected_partner_days_sum"] == pytest.approx(3)
    # Includes c,d, whose zero observed coflights are absent from `edges`.
    assert nodes["a"]["expected_repeated_companions"] == pytest.approx(3 * Fraction(7, 27))
    assert report["opportunity_pairs"] == 6


def test_two_shared_flights_on_one_day_use_union_probability_and_one_day_statistic():
    config, _ = load_graph_config(MODEL)
    events = [event(group + route[0], members, route=route) for route in [("SVO", "LED"), ("LED", "SVO")]
              for group, members in [("a", ["a", "b"]), ("b", ["c", "d"])]]
    edges = observed(("a", "b"), 1)
    background(events, edges, 4, config)
    row = edges["a", "b"]
    assert row["coactive_days"] == 1
    assert row["expected_days"] == pytest.approx(1 - (1 - 1 / 3)**2)
    assert row["p_exact"] == pytest.approx(5 / 9)


def test_popular_flights_and_sparse_activity_are_explained_by_conditioning():
    config, _ = load_graph_config(MODEL)
    events = [event(str(day), ["a", "b", "c", "d"], day) for day in [1, 2, 3]]
    events += [event("other", ["e"], 4)]
    edges = observed(("a", "b"), 3)
    nodes, _ = background(events, edges, 5, config)
    row = edges["a", "b"]
    assert row["expected_days"] == 3 and row["p_exact"] == row["p_bonferroni"] == 1
    assert row["excess_days"] == 0 and row["mc_mean_days"] == 3 and row["mc_mean_standard_error"] == 0
    assert nodes.get("e", {}).get("expected_partner_days_sum", 0) == 0


def test_singletons_remain_in_reference_population():
    config, _ = load_graph_config(MODEL)
    events = [event("a", ["a", "b"]), event("b", ["c"]), event("c", ["d"])]
    edges = observed(("a", "b"), 1)
    background(events, edges, 4, config)
    assert edges["a", "b"]["expected_days"] == pytest.approx(1 / 6)


def test_random_seed_changes_only_monte_carlo_diagnostic():
    config, _ = load_graph_config(MODEL)
    events = [event(str(day) + group, members, day) for day in [1, 2, 3]
              for group, members in [("a", ["a", "b"]), ("b", ["c", "d"])]]
    first = observed(("a", "b"), 3)
    second = copy.deepcopy(first)
    background(events, first, 4, config)
    config["null_model"]["seed"] += 1
    background(list(reversed(events)), second, 4, config)
    for key in ["expected_days", "p_exact", "log_p_exact", "p_bonferroni", "excess_days"]:
        assert first["a", "b"][key] == second["a", "b"][key]
    assert first["a", "b"]["mc_mean_days"] != second["a", "b"]["mc_mean_days"]


@pytest.mark.parametrize("kind", ["event_size", "pair_budget", "null_budget", "multiple_slots", "duplicate_member"])
def test_invalid_or_unsupported_null_structure_fails_before_simulation(kind):
    config, _ = load_graph_config(MODEL)
    events = [event("a", ["a", "b"]), event("b", ["c", "d"])]
    if kind == "event_size":
        config["limits"]["max_event_participants"] = 1
    elif kind == "pair_budget":
        config["limits"]["max_pair_visits"] = 1
    elif kind == "null_budget":
        config["limits"]["max_null_pair_visits"] = 1
    elif kind == "multiple_slots":
        events[1] = event("b", ["b", "c"])
    else:
        events[0]["participant_ids"].append("a")
        events[0]["participant_count"] = 3
    with pytest.raises(ValueError):
        _strata(events, config["limits"], config["null_model"]["simulations"])
