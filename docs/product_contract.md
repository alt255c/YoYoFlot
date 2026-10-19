# Контракт продукта 1.1 · source_dataset

Продукт собирается из неизменённых выходов T13: `persons.parquet` и
`segments.parquet`. Поддерживаются точные LFS-байты, поставленные в develop,
или полный завершённый T13 с проверенными SHA-256 всех опубликованных файлов,
схемами, балансом строк и отчётами в `resolution_manifest.json`.
`train --input <T16 snapshot>` использует подкаталог `tables` этого снимка.
Подписи/хеши доказывают целостность входа, а не достоверность каждого события.

`run_manifest.json`: schema_version=1.1, status=complete,
data_mode=source_dataset, run_id, request (входы/код/runtime), files (SHA-256),
window, counts, eligibility, limitations, sort_order, source_provenance и статусы модели/графа.
Частичный или изменённый снимок не открывается. Пути вне снимка запрещены.
Версия 1.1 меняет допуск в рейтинг на доступность отдельных факторов и фиксирует
популяции сравнений. Снимки/конфигурации 1.0 нужно пересобрать с новой конфигурацией
в отдельный output; автоматической переинтерпретации старых результатов нет.

| Файл | Ключ / содержание |
|---|---|
| tables/persons.parquet | Исходные T13 люди, ключ person_id |
| tables/segments.parquet | Исходные T13 сегменты, ключ segment_id, отсортированы для чтения карточки |
| tables/features.parquet | person_id, исходные счётчики и числовые признаки |
| tables/explanations.parquet | person_id + criterion_id, значение, threshold, weight, status, contribution, reference_size/value/method |
| tables/rankings.parquet | Все профили, counts, score, availability, eligibility_status, nullable rank |
| tables/top50.parquet | Только eligible с rank 1…50, до 50 строк |
| tables/graph_edges.parquet | Повторные пары; event_ids раскрываются через event_members |
| tables/event_members.parquet | Событие → person_id → segment_id |
| tables/segment_links.parquet | Опциональные оригинальные T13 ссылки на источник; копируются только с проверенным манифестом |
| if_scores.parquet | person_id, IF score/percentile, rules percentile, combined_score, роль |
| isolation_forest.joblib | Созданная текущим запуском модель; приложение не десериализует её |
| rules.json / product.yaml | Итоговые пороги/референсы и исходная конфигурация |
| evaluation.json / review.csv | Устойчивость и незаполненная выборка для ручного ревью |
| airports.csv / reference_manifest.json | Проверенный справочник координат той же версии |
| provenance/ | Проверенные отчёты и манифесты T13/T16 с происхождением и ограничениями |

`ProductStore` проверяет снимок один раз при открытии; после этого снимок
считается неизменяемым. Входы защищены от случайной подмены; хеши не являются
цифровой подписью против автора, намеренно переписавшего весь артефакт.

HTTP API только на чтение:

- GET /api/metadata — окно, количества, метод, ограничения и результаты проверки.
- GET /api/top — первые 50 допустимых людей в сохранённом порядке.
- GET /api/profile?id=<person_id> — профиль, факторы, evidence_segment_ids,
  все его сегменты с координатами, баланс, совместные события и отдельный IF.

Неизвестный ID — 404, неправильные параметры — 400. SQL параметризован.
В браузер передаются только топ и выбранная карточка. Баланс маршрутов:
total = plottable + unplottable; после фильтра visible + hidden = total.
Даты и все исходные статусы сохраняются; координаты присоединяются только
по точному airport_id. Неизвестный аэропорт не заменяется центром страны.
