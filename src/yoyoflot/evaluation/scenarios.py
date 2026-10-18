"""Prespecified benchmark annotations, never passed into fit or calibration."""
from ..models.demo import CENSORED_GENERATOR, HOLDOUT_GENERATOR, generate_rows

VERSION = "holdout-catalog-1.0"
CASES = [
    ("sparse_control", "Редкие регулярные поездки", 0, "Обычный разреженный контроль."),
    ("business_control", "Еженедельные рабочие возвраты", 0, "Обычная высокая активность и регулярность; возможное ложное срабатывание."),
    ("holiday_control", "Несколько коротких поездок с возвратом", 0, "Обычные короткие поездки; сам быстрый возврат не объясняет мотив."),
    ("activity_stress", "Плотная серия поездок", 1, "Заранее заложенный тест активности."),
    ("reversal_stress", "Ежедневные обратные направления", 1, "Заранее заложенный тест частых быстрых возвратов."),
    ("cycle_stress", "Повтор трёхзвенного цикла", 1, "Заранее заложенный повторяемый цикл."),
    ("slow_repeat_stress", "Медленные повторные возвраты", 1, "Повтор без быстрых возвратов: проверка разных групп."),
    ("unknown_dates", "Неизвестные даты", None, "Ожидается недостаток данных, не отрицательная метка."),
    ("unknown_routes", "Неполные маршруты", None, "Ожидается недостаток данных, не отрицательная метка."),
    ("low_coverage", "Неполный источник", None, "Ожидается недостаток данных, не отрицательная метка."),
    ("empty_history", "Пустая история", None, "Ожидается недостаток данных, не отрицательная метка."),
    ("shared_schedule", "Общее учебное расписание", 0, "Контроль популярного расписания; первые два участника имеют специально заложенную совместную серию."),
]


def annotations(spec):
    people, _ = generate_rows(spec)  # Recipe is validated even for annotation-only callers.
    holdout = spec["generator"] in {HOLDOUT_GENERATOR, CENSORED_GENERATOR}
    rows = []
    for i, person in enumerate(people):
        case, title, label, reason = CASES[i % 12] if holdout else ("in_domain_unlabelled", "Учебный набор семейства training", None, "Целевой разметки нет.")
        if holdout and i % 12 == 11 and i // 12 < 2 and spec["profiles"] >= 36:
            case, title, label, reason = "coflight_stress", "Заложенная совместная серия", 1, "Два вымышленных участника назначаются на одни явные рейсы в разные дни."
        rows.append(dict(profile_id=person["profile_id"], case_id=case, title_ru=title, benchmark_label=label,
                         expected_insufficient=holdout and i % 12 in {7, 8, 9, 10}, rationale_ru=reason,
                         label_scope="designed_scenario_not_person_trait", catalog_version=VERSION))
    return rows
