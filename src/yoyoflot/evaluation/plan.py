"""Bounded, prespecified experiments; evaluation never chooses new defaults."""
import copy
from datetime import date
from pathlib import Path

import yaml

from ..models.demo import CENSORED_GENERATOR, END, HOLDOUT_GENERATOR, START, VARIED_GENERATOR
from ..models.rule_score import UniqueLoader, _number

VERSION = "evaluation-demo-1.0"
GROUPS = {"M01": "trip_rate_30_days", "M02": "quick_return_share", "M03": "repeated_route_share"}


def load_config(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("Слишком большая конфигурация T20")
    raw = path.read_bytes()
    cfg = yaml.load(raw, Loader=UniqueLoader)
    if (not isinstance(cfg, dict) or set(cfg) != {"version", "data_mode", "profiles", "seed", "replicate_seed", "in_domain_seed", "top_k", "audit", "experiments"}
            or cfg["version"] != VERSION or cfg["data_mode"] != "synthetic_demo"):
        raise ValueError("Нужна конфигурация evaluation-demo-1.0 только для synthetic_demo")
    _number(cfg["profiles"], 36, 240, "profiles", integer=True)
    for key in ["seed", "replicate_seed", "in_domain_seed"]:
        _number(cfg[key], 0, 2**32 - 1, key, integer=True)
    if len({cfg[k] for k in ["seed", "replicate_seed", "in_domain_seed"]}) != 3:
        raise ValueError("Нужны три разных seed оценочных наборов")
    def sequence(values, low, high, name, maximum=2, integer=True):
        if not isinstance(values, list) or len(values) > maximum:
            raise ValueError(f"Неверный ограниченный список {name}")
        for value in values:
            _number(value, low, high, name, integer=integer)
        if len(set(values)) != len(values):
            raise ValueError(f"Повторные значения {name}")
    sequence(cfg["top_k"], 1, 50, "top_k", 3)
    if not cfg["top_k"] or cfg["top_k"] != sorted(cfg["top_k"]):
        raise ValueError("top_k должен быть непустым возрастающим списком")
    audit = cfg["audit"]
    if not isinstance(audit, dict) or set(audit) != {"size", "seed"}:
        raise ValueError("Неверные параметры audit")
    _number(audit["size"], 1, 100, "audit.size", integer=True)
    _number(audit["seed"], 0, 2**32 - 1, "audit.seed", integer=True)
    exp = cfg["experiments"]
    if not isinstance(exp, dict) or set(exp) != {"if_seeds", "trees", "max_samples", "threshold_factors", "rules_weights", "as_of",
                                               "group_ablations", "missingness_check", "include_graph", "graph_seeds", "mc_seed"}:
        raise ValueError("Неверный план экспериментов T20")
    for key in ["group_ablations", "missingness_check", "include_graph"]:
        if type(exp[key]) is not bool:
            raise ValueError(f"{key} должен быть boolean")
    for key, low, high, integer in [("if_seeds", 0, 2**32-1, True), ("trees", 16, 512, True), ("max_samples", 8, 512, True),
                                    ("threshold_factors", 0.8, 1.2, False), ("rules_weights", 0.01, 0.99, False), ("graph_seeds", 0, 2**32-1, True)]:
        sequence(exp[key], low, high, key, integer=integer)
    _number(exp["mc_seed"], 0, 2**32-1, "mc_seed", integer=True)
    if not isinstance(exp["as_of"], list) or len(exp["as_of"]) > 2 or len(set(exp["as_of"])) != len(exp["as_of"]):
        raise ValueError("Неверный список окон")
    for value in exp["as_of"]:
        try:
            cutoff = date.fromisoformat(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("as_of должен быть YYYY-MM-DD") from exc
        if cutoff.isoformat() != value or not START <= cutoff < END:
            raise ValueError("Изменённое окно должно быть раньше конца базового")
    return cfg, raw


def build_plan(cfg, forest, rules, graph):
    if forest["as_of"] != END.isoformat() or forest["include_graph"] or set(forest["features"]) != set(GROUPS.values()):
        raise ValueError("Базовый T20 требует три travel-признака, конец 31.03.2026 и выключенный граф")
    if {c["id"] for c in rules["criteria"] if c["enabled"]} != set(GROUPS):
        raise ValueError("Базовые правила T20 должны включать M01–M03")
    seeds = {cfg[k] for k in ["seed", "replicate_seed", "in_domain_seed"]}
    if seeds.intersection(forest["reference"][k] for k in ["training_seed", "calibration_seed"]):
        raise ValueError("Seed оценки T20 пересекается с обучением/калибровкой")
    plan = []
    def add(name, change, dataset="holdout", seed=None, quality=True):
        f, r, g = copy.deepcopy(forest), copy.deepcopy(rules), copy.deepcopy(graph)
        change(f, r, g)
        generator = {"holdout": HOLDOUT_GENERATOR, "in_domain": VARIED_GENERATOR, "censored": CENSORED_GENERATOR}[dataset]
        plan.append(dict(name=name, dataset=dataset, seed=cfg["seed"] if seed is None else seed,
                         generator=generator, forest=f, rules=r, graph=g, quality_enabled=quality and dataset == "holdout"))
    add("baseline", lambda f,r,g: None)
    add("replicate", lambda f,r,g: None, seed=cfg["replicate_seed"])
    add("in_domain", lambda f,r,g: None, dataset="in_domain", seed=cfg["in_domain_seed"], quality=False)
    exp = cfg["experiments"]
    for key, values in [("random_state", exp["if_seeds"]), ("n_estimators", exp["trees"]), ("max_samples", exp["max_samples"])]:
        for value in values:
            add(f"if_{key}_{value}", lambda f,r,g,k=key,v=value: f["forest"].update({k:v}))
    for factor in exp["threshold_factors"]:
        def thresholds(f, r, g, factor=factor):
            for criterion in r["criteria"]:
                if criterion["enabled"]:
                    criterion["threshold"] *= factor
                    if criterion["id"] != "M01" and criterion["threshold"] > 1:
                        raise ValueError("Изменённый порог доли превысил 1")
        add("threshold_" + str(factor).replace(".", "p"), thresholds)
    for weight in exp["rules_weights"]:
        add("weight_" + str(weight).replace(".", "p"), lambda f,r,g,w=weight: f["combination"].update(rules_weight=w, if_weight=1-w))
    for cutoff in exp["as_of"]:
        add("window_" + cutoff, lambda f,r,g,v=cutoff: (f.update(as_of=v), g.update(as_of=v)), quality=False)
    if exp["group_ablations"]:
        for cid, feature in GROUPS.items():
            def remove(f, r, g, cid=cid, feature=feature):
                f["features"].remove(feature)
                r["criteria"] = [dict(id=cid, enabled=False) if c["id"] == cid else c for c in r["criteria"]]
                r["coverage"]["min_available_groups"] = 2
            add("without_" + cid, remove)
    if exp["missingness_check"]:
        add("missing_routes", lambda f,r,g: None, dataset="censored", quality=False)
    if exp["include_graph"]:
        if graph["as_of"] != forest["as_of"]:
            raise ValueError("Базовые окна T18 и T19 не совпадают")
        add("graph", lambda f,r,g: f.update(include_graph=True))
        for seed in exp["graph_seeds"]:
            add(f"graph_seed_{seed}", lambda f,r,g,v=seed: (f.update(include_graph=True), g.update(graph_seed=v)))
        add("graph_mc_seed", lambda f,r,g: (f.update(include_graph=True), g["null_model"].update(seed=exp["mc_seed"])))
    return plan
