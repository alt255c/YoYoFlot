"""Reproducible IF experiment on complete, independently fictional profiles.

The forest is fit on training only. Both empirical CDFs are frozen using a
separate calibration cohort; evaluation never changes training or calibration.
"""
import math
from datetime import date
from pathlib import Path

import yaml

from .demo import END, START
from .ranking import midrank_percentiles
from .rule_score import UniqueLoader, _number

VERSION = "if-demo-1.0"
TRAVEL_FEATURES = {"trip_rate_30_days": (0, 1800), "quick_return_share": (0, 1), "repeated_route_share": (0, 1)}
GRAPH_FEATURES = {"excess_repeated_companions": (-500, 500), "max_positive_pair_excess_days": (0, 90)}


def load_config(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise ValueError("Слишком большая конфигурация T19")
    raw = path.read_bytes()
    config = yaml.load(raw, Loader=UniqueLoader)
    if (not isinstance(config, dict) or set(config) != {"version", "data_mode", "enabled", "as_of", "features",
            "include_graph", "graph_features", "missing_policy", "preprocessing", "reference", "forest", "combination"}
            or config["version"] != VERSION or config["data_mode"] != "synthetic_demo"):
        raise ValueError("Нужна конфигурация if-demo-1.0 только для synthetic_demo")
    for key in ["enabled", "include_graph"]:
        if type(config[key]) is not bool:
            raise ValueError(f"{key} должен быть boolean")
    try:
        cutoff = date.fromisoformat(config["as_of"])
    except (TypeError, ValueError) as exc:
        raise ValueError("as_of должен быть строкой YYYY-MM-DD") from exc
    if cutoff.isoformat() != config["as_of"] or not START <= cutoff <= END:
        raise ValueError("as_of вне известного учебного окна")
    for key, allowed in [("features", TRAVEL_FEATURES), ("graph_features", GRAPH_FEATURES)]:
        names = config[key]
        if (not isinstance(names, list) or not names or any(type(n) is not str or n not in allowed for n in names)
                or len(set(names)) != len(names)):
            raise ValueError(f"Неизвестные/повторные модельные признаки: {key}")
    if config["missing_policy"] != "exclude_incomplete" or config["preprocessing"] != "none_float32":
        raise ValueError("Поддерживаются только exclude_incomplete и none_float32")
    ref = config["reference"]
    if not isinstance(ref, dict) or set(ref) != {"training_seed", "training_profiles", "calibration_seed", "calibration_profiles",
                                               "min_training_rows", "min_calibration_rows", "min_distinct_rows"}:
        raise ValueError("Неизвестные параметры опорных наборов")
    for key in ["training_seed", "calibration_seed"]:
        _number(ref[key], 0, 2**32 - 1, key, integer=True)
    if ref["training_seed"] == ref["calibration_seed"]:
        raise ValueError("Обучение и калибровка должны иметь разные seed")
    for key in ["training_profiles", "calibration_profiles"]:
        _number(ref[key], 0, 500, key, integer=True)
    for key in ["min_training_rows", "min_calibration_rows"]:
        _number(ref[key], 4, 500, key, integer=True)
    _number(ref["min_distinct_rows"], 2, 500, "min_distinct_rows", integer=True)
    forest = config["forest"]
    if not isinstance(forest, dict) or set(forest) != {"n_estimators", "max_samples", "random_state", "n_jobs", "contamination"}:
        raise ValueError("Неизвестные параметры Isolation Forest")
    _number(forest["n_estimators"], 16, 512, "n_estimators", integer=True)
    _number(forest["max_samples"], 8, 512, "max_samples", integer=True)
    _number(forest["random_state"], 0, 2**32 - 1, "random_state", integer=True)
    if type(forest["n_jobs"]) is not int or forest["n_jobs"] != 1 or forest["contamination"] != "auto":
        raise ValueError("T19 фиксирует n_jobs=1 и contamination=auto; бинарные классы не выдаются")
    combo = config["combination"]
    if not isinstance(combo, dict) or set(combo) != {"rules_weight", "if_weight"}:
        raise ValueError("Неверная комбинация процентилей")
    for key in combo:
        _number(combo[key], 0, 1, key)
    if not math.isclose(sum(combo.values()), 1.0, abs_tol=1e-12, rel_tol=0):
        raise ValueError("Веса комбинации должны суммироваться в 1")
    return config, raw


def columns(config):
    return config["features"] + (config["graph_features"] if config["include_graph"] else [])


def make_matrix(role, dataset_hash, features, coverage, scores, config, graph_features=None):
    """Explicit allowlist/order. Availability is a gate, never a numeric feature."""
    def unique(rows, key):
        result = {r[key]: r for r in rows}
        if len(result) != len(rows):
            raise ValueError(f"Повторный ключ {key} входа матрицы")
        return result
    facts, rules = unique(features, "profile_id"), unique(scores, "profile_id")
    if set(facts) != set(rules):
        raise ValueError("Несовпадающие profile_id признаков и правил")
    cov = {(r["profile_id"], r["feature_name"]): r for r in coverage}
    if len(cov) != len(coverage):
        raise ValueError("Повторные строки покрытия")
    graph = unique(graph_features, "profile_id") if graph_features is not None else None
    if config["include_graph"] and (graph is None or set(graph) != set(facts)):
        raise ValueError("Несовпадающие profile_id графа и правил")
    selected = columns(config)
    expected_window = START, date.fromisoformat(config["as_of"])
    output = []
    for pid, row in sorted(facts.items()):
        score = rules[pid]
        if (row["window_start"], row["window_end"]) != expected_window or (score["window_start"], score["window_end"]) != expected_window:
            raise ValueError("Окно признаков/правил не соответствует T19")
        if config["include_graph"] and (graph[pid]["window_start"], graph[pid]["window_end"]) != expected_window:
            raise ValueError("Окно графа не соответствует T19")
        reasons, values, masks = [], [], []
        if score["eligibility_status"] not in {"eligible", "no_matches"}:
            reasons.append("Недостаточные данные для сопоставимых правил P0.")
        for name in selected:
            if name in TRAVEL_FEATURES:
                availability = cov.get((pid, name))
                if availability is None:
                    raise ValueError("Нет строки покрытия выбранного признака")
                available, value = availability["available"], row[name]
            else:
                available, value = graph[pid]["available"], graph[pid][name]
            if value is not None:
                low, high = {**TRAVEL_FEATURES, **GRAPH_FEATURES}[name]
                if type(value) not in {int, float} or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"Неверное числовое значение {name}: {pid}")
            available = bool(available and value is not None)
            values.append(float(value) if value is not None else None)
            masks.append(available)
            if not available:
                reasons.append(f"Недоступен признак {name}.")
        output.append(dict(role=role, dataset_sha256=dataset_hash, profile_id=pid, feature_names=selected,
                           values=values, feature_available=masks, eligible=not reasons,
                           exclusion_reason=" ".join(reasons) if reasons else None,
                           window_start=expected_window[0], window_end=expected_window[1]))
    return output


def _array(rows, feature_names):
    import numpy as np
    if any(r["feature_names"] != feature_names or not r["eligible"] or
           len(r["feature_available"]) != len(feature_names) or not all(r["feature_available"]) or
           len(r["values"]) != len(feature_names) for r in rows):
        raise ValueError("Порядок признаков или доступность не совпадают с моделью")
    result = np.asarray([r["values"] for r in rows], dtype=np.float32).reshape(len(rows), len(feature_names))
    if not np.isfinite(result).all():
        raise ValueError("Матрица содержит NaN/inf")
    return result


def fit(training, calibration, calibration_rules, config, progress=None):
    """No evaluation argument: evaluation cannot affect fit or either CDF."""
    names = columns(config)
    train = sorted((r for r in training if r["eligible"]), key=lambda r: r["profile_id"])
    calib = sorted((r for r in calibration if r["eligible"]), key=lambda r: r["profile_id"])
    if (any(r["role"] != "training" for r in training) or any(r["role"] != "calibration" for r in calibration) or
            len({r["profile_id"] for r in training}) != len(training) or len({r["profile_id"] for r in calibration}) != len(calibration) or
            {r["profile_id"] for r in training}.intersection(r["profile_id"] for r in calibration)):
        raise ValueError("Смешанные роли или повторные ID обучения/калибровки")
    if any((r["window_start"], r["window_end"]) != (START, date.fromisoformat(config["as_of"])) for r in [*training, *calibration]):
        raise ValueError("Окно обучения/калибровки не соответствует T19")
    by_id = {r["profile_id"]: r for r in calibration_rules}
    if (len(by_id) != len(calibration_rules) or set(by_id) != {r["profile_id"] for r in calibration} or
            any(type(r["matched_criteria_count"]) is not int or r["matched_criteria_count"] < 0 or
                type(r["rule_score"]) not in {int, float} or not math.isfinite(r["rule_score"])
                for r in calibration_rules)):
        raise ValueError("Несогласованные правила калибровочного набора")
    distinct = len({tuple(r["values"]) for r in train})
    metadata = dict(training_rows=len(train), calibration_rows=len(calib), distinct_training_rows=distinct,
                    feature_names=names, preprocessing="none_float32", missing_policy="exclude_incomplete")
    reason = None
    if not config["enabled"]:
        reason = "Эксперимент IF выключен в конфигурации."
    elif len(train) < config["reference"]["min_training_rows"]:
        reason = "Недостаточно полных обучающих профилей."
    elif len(calib) < config["reference"]["min_calibration_rows"]:
        reason = "Недостаточно полных калибровочных профилей."
    elif distinct < config["reference"]["min_distinct_rows"]:
        reason = "Недостаточно различных обучающих векторов."
    if reason:
        return None, dict(metadata, available=False, reason=reason)
    from sklearn.ensemble import IsolationForest
    x_train, x_calib = _array(train, names), _array(calib, names)
    import numpy as np
    metadata["distinct_training_rows"] = len(np.unique(x_train, axis=0))
    if metadata["distinct_training_rows"] < config["reference"]["min_distinct_rows"]:
        return None, dict(metadata, available=False, reason="Недостаточно различных float32-векторов обучения.")
    params = dict(config["forest"], max_samples=min(config["forest"]["max_samples"], len(train)),
                  max_features=1.0, bootstrap=False, warm_start=False)
    if progress:
        progress(f"T19: обучение Isolation Forest — {params['n_estimators']} деревьев, {len(train)} обучающих строк")
    forest = IsolationForest(**params).fit(x_train)
    if_reference = (-forest.score_samples(x_calib)).tolist()
    if not all(math.isfinite(v) for v in if_reference):
        raise ValueError("Isolation Forest вернул нечисловые оценки")
    rules_reference = [(by_id[r["profile_id"]]["matched_criteria_count"], by_id[r["profile_id"]]["rule_score"]) for r in calib]
    bundle = dict(format=VERSION, forest=forest, feature_names=names, as_of=config["as_of"],
                  if_reference=sorted(if_reference), rules_reference=sorted(rules_reference),
                  preprocessing="none_float32", score_direction="higher_is_more_unusual", params=params)
    return bundle, dict(metadata, available=True, reason=None, effective_forest=params,
                        constant_features=[name for i, name in enumerate(names) if np.unique(x_train[:, i]).size == 1])


def score(bundle, rows):
    if bundle["format"] != VERSION:
        raise ValueError("Неподдерживаемый формат учебной модели")
    if not rows:
        return []
    if (bundle["forest"].n_features_in_ != len(bundle["feature_names"]) or
            any(r["window_start"] != START or r["window_end"].isoformat() != bundle["as_of"] for r in rows)):
        raise ValueError("Окно или число признаков не соответствует обученной модели")
    x = _array(rows, bundle["feature_names"])
    result = (-bundle["forest"].score_samples(x)).tolist()
    if not all(math.isfinite(v) for v in result):
        raise ValueError("Isolation Forest вернул NaN/inf")
    return result


def experimental_rows(matrix, rule_rows, bundle, fit_metadata, config, run_id):
    rules = {r["profile_id"]: r for r in rule_rows}
    eligible = [r for r in matrix if r["eligible"]]
    values = score(bundle, eligible) if bundle is not None else []
    scored = dict(zip((r["profile_id"] for r in eligible), values))
    if_percentiles = dict(zip(scored, midrank_percentiles(bundle["if_reference"], values))) if bundle else {}
    rule_keys = [(rules[r["profile_id"]]["matched_criteria_count"], rules[r["profile_id"]]["rule_score"]) for r in eligible]
    rule_percentiles = dict(zip((r["profile_id"] for r in eligible), midrank_percentiles(bundle["rules_reference"], rule_keys))) if bundle else {}
    rows = []
    for row in matrix:
        pid = row["profile_id"]
        status = "scored" if pid in scored else "insufficient_data" if not row["eligible"] else "disabled" if not config["enabled"] else "reference_unavailable"
        if_value, rp, ip = scored.get(pid), rule_percentiles.get(pid), if_percentiles.get(pid)
        combined = config["combination"]["rules_weight"] * rp + config["combination"]["if_weight"] * ip if if_value is not None else None
        explanation = (f"Учебный Isolation Forest: {if_value:.6f}; больший балл означает большую необычность относительно опорного набора. "
                       f"Процентиль по отдельной калибровке: {ip:.6f}. Это не вероятность; объяснения правил сохранены отдельно."
                       if if_value is not None else row["exclusion_reason"] or fit_metadata["reason"])
        rows.append(dict(profile_id=pid, display_name=rules[pid]["display_name"], if_score=if_value,
                         rule_percentile=rp, if_percentile=ip, combined_score=combined, status=status,
                         matched_criteria_count=rules[pid]["matched_criteria_count"], rule_score=rules[pid]["rule_score"],
                         primary_rule_status=rules[pid]["eligibility_status"], explanation_ru=explanation,
                         window_start=row["window_start"], window_end=row["window_end"], data_mode="synthetic_demo",
                         model_version=VERSION, run_id=run_id))
    return rows
