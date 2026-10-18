"""Tie-aware metrics with explicit universes and no unavailable-as-zero rows."""
import math
from itertools import combinations

METHODS = ("rules", "isolation_forest", "combination")


def keyed(rows):
    result = {r["profile_id"]: r for r in rows}
    if len(result) != len(rows):
        raise ValueError("Повторный profile_id сравнения")
    return result


def key(row):
    value = (row["matched_criteria_count"], row["rule_score"]) if row["method"] == "rules" else row["score"]
    numbers = value if isinstance(value, tuple) else (value,)
    if any(type(v) not in {int, float} or not math.isfinite(v) for v in numbers):
        raise ValueError("Недоступный/нечисловой балл сравнения")
    return value


def midranks(values):
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    result, i = [0.0] * len(values), 0
    while i < len(ordered):
        j = i + 1
        while j < len(ordered) and ordered[j][1] == ordered[i][1]:
            j += 1
        for index, _ in ordered[i:j]:
            result[index] = (i + 1 + j) / 2
        i = j
    return result


def spearman(left, right):
    if len(left) != len(right):
        raise ValueError("Несопоставимые длины рангов")
    if len(left) < 2:
        return None
    a, b = midranks(left), midranks(right)
    mean = (len(a) + 1) / 2
    cross = math.fsum((x - mean) * (y - mean) for x, y in zip(a, b))
    variance_a = math.fsum((x - mean)**2 for x in a)
    variance_b = math.fsum((x - mean)**2 for x in b)
    return max(-1.0, min(1.0, cross / math.sqrt(variance_a * variance_b))) if variance_a and variance_b else None


def common_universe(method_rows):
    sets = []
    population = None
    for method in METHODS:
        rows = method_rows[method]
        by_id = keyed(rows)
        if population is not None and set(by_id) != population:
            raise ValueError("Методы описывают разные входные профили")
        population = set(by_id)
        for row in rows:
            if row["available"]:
                key(row)
        sets.append({r["profile_id"] for r in rows if r["available"]})
    return set.intersection(*sets)


def selected(rows, k, universe=None):
    ids = set(r["profile_id"] for r in rows) if universe is None else universe
    candidates = [r for r in rows if r["available"] and r["rank"] is not None and r["profile_id"] in ids]
    ranks = [r["rank"] for r in candidates]
    if len(set(ranks)) != len(ranks) or any(type(rank) is not int or rank < 1 for rank in ranks):
        raise ValueError("Неверные/повторные ранги")
    return sorted(candidates, key=lambda r: r["rank"])[:k]


def metrics(method_rows, annotation_rows, top_k, quality_enabled, universe=None):
    annotations = keyed(annotation_rows)
    common = common_universe(method_rows)
    if universe is not None:
        if not universe.issubset(common):
            raise ValueError("Парное сравнение содержит недоступные профили")
        common = universe
    if set(annotations) != {r["profile_id"] for r in method_rows["rules"]}:
        raise ValueError("Разметка не соответствует входным профилям")
    output = []
    for method in METHODS:
        rows = method_rows[method]
        by_id = keyed(rows)
        known = sorted(pid for pid in common if annotations[pid]["benchmark_label"] is not None)
        labels = [annotations[pid]["benchmark_label"] for pid in known]
        if any(type(v) is not int or v not in {0, 1} for v in labels):
            raise ValueError("Метка сценария должна быть 0, 1 или null")
        positives, negatives = sum(labels), len(labels) - sum(labels)
        reason = ("Изменение окна/пропусков или неразмеченный набор: качество не измеряется." if not quality_enabled else
                  "Нет обоих классов в общей сопоставимой размеченной группе." if not positives or not negatives else None)
        ap = roc = None
        if reason is None:
            from sklearn.metrics import average_precision_score, roc_auc_score
            # Ordinal encoding preserves the rules' lexicographic priority and all ties.
            values = [key(by_id[pid]) for pid in known]
            levels = {value: i for i, value in enumerate(sorted(set(values)))}
            numeric = [levels[v] for v in values]
            ap, roc = float(average_precision_score(labels, numeric)), float(roc_auc_score(labels, numeric))
        for k in top_k:
            chosen = selected(rows, k, common)
            chosen_labels = [annotations[r["profile_id"]]["benchmark_label"] for r in chosen]
            hits = sum(v == 1 for v in chosen_labels)
            unknown = sum(v is None for v in chosen_labels)
            boundary_ties = 0
            lower = upper = None
            if chosen:
                boundary = key(chosen[-1])
                boundary_ties = sum(r["available"] and r["profile_id"] in common and key(r) == boundary for r in rows)
                candidates = [r for r in rows if r["available"] and r["rank"] is not None and r["profile_id"] in common]
                above = [r for r in candidates if key(r)>boundary]
                tied = [r for r in candidates if key(r)==boundary]
                group_labels = [annotations[r["profile_id"]]["benchmark_label"] for r in above+tied]
                if quality_enabled and all(v is not None for v in group_labels):
                    hits_above = sum(annotations[r["profile_id"]]["benchmark_label"]==1 for r in above)
                    tied_positives = sum(annotations[r["profile_id"]]["benchmark_label"]==1 for r in tied)
                    slots = len(chosen)-len(above)
                    lower = (hits_above+max(0, slots-(len(tied)-tied_positives)))/len(chosen)
                    upper = (hits_above+min(slots,tied_positives))/len(chosen)
            output.append(dict(method=method, k=k, population=len(rows), available=sum(r["available"] for r in rows),
                common_profiles=len(common), labelled_common=len(known), positives=positives, negatives=negatives,
                positive_prevalence=positives / len(known) if known else None,
                selected=len(chosen), selected_unlabelled=unknown, hits=hits if quality_enabled else None,
                precision_at_k=hits / len(chosen) if quality_enabled and chosen and not unknown else None,
                precision_tie_lower=lower, precision_tie_upper=upper,
                recall_at_k=hits / positives if quality_enabled and positives else None,
                average_precision=ap, roc_auc=roc, quality_reason=reason,
                boundary_tie_size=boundary_ties, scope="common_comparable_profiles; scenario_labels_only"))
    return output


def overlap(left, right):
    a, b = {r["profile_id"] for r in left}, {r["profile_id"] for r in right}
    return dict(left_size=len(a), right_size=len(b), intersection=len(a & b), union=len(a | b),
                jaccard=len(a & b) / len(a | b) if a or b else None,
                retention=len(a & b) / len(a) if a else None)


def stability(left_methods, right_methods, top_k):
    left_ids = {r["profile_id"] for r in left_methods["rules"]}
    right_ids = {r["profile_id"] for r in right_methods["rules"]}
    left_common, right_common = common_universe(left_methods), common_universe(right_methods)
    common = left_common & right_common
    output = []
    for method in METHODS:
        left, right = left_methods[method], right_methods[method]
        a, b = keyed(left), keyed(right)
        same_people = left_ids == right_ids
        correlation = spearman([key(a[p]) for p in sorted(common)], [key(b[p]) for p in sorted(common)]) if same_people else None
        for k in top_k:
            raw = overlap(selected(left, k), selected(right, k)) if same_people else {}
            matched = overlap(selected(left, k, common), selected(right, k, common)) if same_people else {}
            output.append(dict(method=method, k=k, comparable=same_people,
                reason=None if same_people else "Разные профили: Jaccard и корреляция не сопоставимы.",
                left_common=len(left_common), right_common=len(right_common), shared_common=len(common),
                entered=len(right_common-left_common), excluded=len(left_common-right_common),
                end_to_end_jaccard=raw.get("jaccard"), common_population_jaccard=matched.get("jaccard"),
                end_to_end_left_size=raw.get("left_size"), end_to_end_right_size=raw.get("right_size"),
                common_top_left_size=matched.get("left_size"), common_top_right_size=matched.get("right_size"),
                spearman_common=correlation))
    return output


def method_agreement(method_rows, top_k):
    common = common_universe(method_rows)
    output = []
    for left, right in combinations(METHODS, 2):
        a, b = keyed(method_rows[left]), keyed(method_rows[right])
        correlation = spearman([key(a[p]) for p in sorted(common)], [key(b[p]) for p in sorted(common)])
        for k in top_k:
            output.append(dict(left_method=left, right_method=right, k=k, common_profiles=len(common),
                               spearman_common=correlation, **overlap(selected(method_rows[left], k, common), selected(method_rows[right], k, common))))
    return output
