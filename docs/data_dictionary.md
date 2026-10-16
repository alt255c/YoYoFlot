# Словарь данных 1.0

Сгенерирован из `contracts.SCHEMAS`; смысл null и связи — `data_contract.md`.

## person_observations

| Поле | Arrow-тип | Nullable | Смысл |
|---|---|---|---|
| source_id | string | нет | Логическое имя источника; обязательная строка. |
| source_record_id | string | нет | Детерминированный ключ конкретного исходного locator и SHA-256 версии входа. |
| locator | string | нет | Путь к исходной записи (строка, профиль, XML-путь, книга/лист/строка, страница). |
| raw_json | string | да | Исходный payload/неиспользуемые поля; JSON в строке. Для PDF — ссылка на текст страницы и разобранные поля. |
| observation_id | string | нет | Локальное наблюдение о человеке; PK people/FK identifiers и travels. |
| name_raw | string | да | Имя, присутствующее в источнике; null если доказанного имени нет. |
| name_normalized | string | да | Схлопнутые пробелы и casefold; без транслитерации/объединения людей. |
| birth_date_raw | string | да | Исходная дата рождения; ошибки сохраняются. |
| birth_date | string | да | Нормализованная дата рождения; null при отсутствии/ошибке. |
| sex | string | да | Исходный пол без самостоятельного вывода. |
| nickname | string | да | Исходный NickName; не замена имени и не глобальный ключ. |
| external_id | string | да | Внешний строковый ID; уникален только в подтверждённом контексте источника. |

## identifiers

| Поле | Arrow-тип | Nullable | Смысл |
|---|---|---|---|
| source_id | string | нет | Логическое имя источника; обязательная строка. |
| source_record_id | string | нет | Детерминированный ключ конкретного исходного locator и SHA-256 версии входа. |
| locator | string | нет | Путь к исходной записи (строка, профиль, XML-путь, книга/лист/строка, страница). |
| raw_json | string | да | Исходный payload/неиспользуемые поля; JSON в строке. Для PDF — ссылка на текст страницы и разобранные поля. |
| identifier_id | string | нет | PK отдельного идентификатора, а не доказанный глобальный ключ человека. |
| observation_id | string | нет | Локальное наблюдение о человеке; PK people/FK identifiers и travels. |
| kind | string | нет | document, ticket или loyalty_card. |
| programme | string | да | Программа лояльности; null — неизвестна либо неприменима. Номер отдельно. |
| value | string | нет | Номер идентификатора строкой с ведущими нулями; внешние пробелы карты удалены, точное исходное значение в raw_json. |

## travel_observations

| Поле | Arrow-тип | Nullable | Смысл |
|---|---|---|---|
| source_id | string | нет | Логическое имя источника; обязательная строка. |
| source_record_id | string | нет | Детерминированный ключ конкретного исходного locator и SHA-256 версии входа. |
| locator | string | нет | Путь к исходной записи (строка, профиль, XML-путь, книга/лист/строка, страница). |
| raw_json | string | да | Исходный payload/неиспользуемые поля; JSON в строке. Для PDF — ссылка на текст страницы и разобранные поля. |
| travel_id | string | нет | PK отдельного наблюдения о поездке, включая повторные исходные активности. |
| observation_id | string | нет | Локальное наблюдение о человеке; PK people/FK identifiers и travels. |
| event_kind | string | да | Тип источника свидетельства; не утверждает фактическую посадку. |
| loyalty_identifier_id | string | да | Необязательная FK на карту, когда её связь с конкретным событием явна. |
| flight_number | string | да | Номер рейса как строка; codeshare автоматически не объединяется. |
| origin_raw | string | да | Исходное отправление, включая JSON вложенного объекта; null если отсутствует. |
| destination_raw | string | да | Исходное назначение, включая JSON вложенного объекта; аэропорт не угадывается. |
| departure_raw | string | да | Исходная дата или дата+время отправления; не источник аэропорта. |
| departure_local | string | да | ISO-строка локального времени с точностью источника, в schedules только HH:MM. |
| departure_timezone | string | да | Подтверждённый часовой пояс; текущие readers не выводят его из географии. |
| departure_precision | string | да | date/minute/second/invalid/null. Дата не превращается в полночь. |
| arrival_raw | string | да | Исходная дата или дата+время прибытия; не источник аэропорта. |
| arrival_local | string | да | ISO-строка локального времени с точностью источника, в schedules только HH:MM. |
| arrival_timezone | string | да | Подтверждённый часовой пояс; null когда неизвестен. |
| arrival_precision | string | да | date/minute/second/invalid/null. |
| ticket_number | string | да | Исходный билет строкой; повтор билета сохраняет несколько наблюдений. |
| booking_code | string | да | Код брони строкой. |
| codeshare | string | да | Исходное обозначение codeshare; не трактуется как готовая связь рейсов. |
| status_raw | string | да | Исходный STATUS; семантика пока не установлена. |
| fare_raw | string | да | Исходный Fare; не трактуется как подтверждённый класс/стоимость. |

## schedules

| Поле | Arrow-тип | Nullable | Смысл |
|---|---|---|---|
| source_id | string | нет | Логическое имя источника; обязательная строка. |
| source_record_id | string | нет | Детерминированный ключ конкретного исходного locator и SHA-256 версии входа. |
| locator | string | нет | Путь к исходной записи (строка, профиль, XML-путь, книга/лист/строка, страница). |
| raw_json | string | да | Исходный payload/неиспользуемые поля; JSON в строке. Для PDF — ссылка на текст страницы и разобранные поля. |
| schedule_id | string | нет | PK отдельной строки расписания, зависит от версии PDF и locator. |
| flight_number | string | да | Номер рейса как строка; codeshare автоматически не объединяется. |
| origin | string | да | IATA-код из явно разобранной секции PDF; null при отсутствии секции. |
| destination | string | да | IATA-код из явно разобранной секции PDF; null при отсутствии секции. |
| departure_local | string | да | ISO-строка локального времени с точностью источника, в schedules только HH:MM. |
| arrival_local | string | да | ISO-строка локального времени с точностью источника, в schedules только HH:MM. |
| arrival_day_offset | int64 | да | Явный +N из расписания; без маркера null, не ноль. |
| days_of_week | string | да | Дни 1–7 из расписания, пробелы удалены. |
| valid_from | string | да | Начало действия YYYY-MM-DD, год только из однозначного периода обложки. |
| valid_to | string | да | Конец действия YYYY-MM-DD; null при неизвестном/неоднозначном периоде. |
| source_page | int64 | да | Физическая страница PDF, начиная с 1. |
| parse_status | string | да | ok/row_without_section/invalid_*; неудачи блокируют приёмку PDF. |
| section_source | string | да | page либо carried:N (страница исходного заголовка). |
| origin_city | string | да | Исходный город отправления PDF. |
| origin_country | string | да | Исходная страна отправления PDF. |
| dest_city | string | да | Исходный город назначения PDF. |
| dest_country | string | да | Исходная страна назначения PDF. |
| validity_raw | string | да | Исходный текст диапазона действия. |
| days_raw | string | да | Исходный текст дней недели. |
| aircraft | string | да | Исходный код самолёта. |
| travel_time | string | да | Длительность, указанная в расписании; не вычисляется из часов без timezone. |
| operated_by | string | да | Текст Operated by, не установленный по номеру рейса. |
| codeshare_mark | bool | да | Наличие звёздочки у номера рейса PDF. |
| raw_line | string | да | Исходный текст разобранного фрагмента; полный текст страницы доступен отдельно. |

## provenance

| Поле | Arrow-тип | Nullable | Смысл |
|---|---|---|---|
| source_id | string | нет | Логическое имя источника; обязательная строка. |
| source_record_id | string | нет | Детерминированный ключ конкретного исходного locator и SHA-256 версии входа. |
| locator | string | нет | Путь к исходной записи (строка, профиль, XML-путь, книга/лист/строка, страница). |
| raw_json | string | да | Исходный payload/неиспользуемые поля; JSON в строке. Для PDF — ссылка на текст страницы и разобранные поля. |
| entity_table | string | нет | Таблица преобразованного объекта, на который указывает provenance. |
| entity_id | string | нет | Первичный ключ преобразованного объекта. |
| transformation | string | нет | Версия преобразования reader. |

## errors

| Поле | Arrow-тип | Nullable | Смысл |
|---|---|---|---|
| source_id | string | нет | Логическое имя источника; обязательная строка. |
| source_record_id | string | нет | Детерминированный ключ конкретного исходного locator и SHA-256 версии входа. |
| locator | string | нет | Путь к исходной записи (строка, профиль, XML-путь, книга/лист/строка, страница). |
| raw_json | string | да | Исходный payload/неиспользуемые поля; JSON в строке. Для PDF — ссылка на текст страницы и разобранные поля. |
| error_id | string | нет | PK записи журнала; не итоговое число исключённых исходных объектов. |
| reason | string | нет | Машинно-читаемая причина ошибки, исключения или ограничения. |
| severity | string | нет | info (пустые/служебные записи), warning (ограничение), error (потеря/ошибка). |

## T11: дополнительные таблицы geography 1.0

Базовые схемы readers выше не меняются. Метаданные/типы дополнительных полей
заданы в `src/yoyoflot/normalize/geography.py`; все дополнительные поля nullable,
булевы quality flags в travel_geography заполняются для каждой строки.

### travel_geography

Содержит все исходные travel_observations и совместимые дополнительные поля T12.

| Дополнительные поля | Тип | Смысл |
|---|---|---|
| origin_airport_id, destination_airport_id | string | Ключ аэропорта в снимке справочника, null при нерешённости |
| origin_iata, destination_iata | string | Подтверждённый справочный код; null допустим даже при известном airport_id |
| origin_city/country, destination_city/country | string | Справочный город/страна; страна также может быть известна без аэропорта |
| origin_latitude/longitude, destination_latitude/longitude | float64 | Градусы из снимка справочника, без оценки исторической точности |
| origin_timezone, destination_timezone | string | IANA timezone известного аэропорта |
| origin_status, destination_status | string | exact / corrected / inferred_from_schedule / ambiguous / unresolved |
| origin_kind, destination_kind | string | airport / city / country / missing / unknown — вид исходного обозначения |
| origin_reason, destination_reason | string | Причина выбора или отказа |
| origin_candidates_json, destination_candidates_json | string | JSON-массив возможных airport_id; [] при отсутствии |
| origin_override_json, destination_override_json | string | Полное правило YAML с evidence/reason либо null; для стран объект country_aliases (массив правил), при совместном исправлении кода также airport_override |
| departure_effective_local, arrival_effective_local | string | Исходная ISO-строка либо явно выведенная полная локальная дата/время |
| departure_effective_precision, arrival_effective_precision | string | Точность effective-значения; исходная точность хранится отдельно |
| departure_time_origin, arrival_time_origin | string | source / inferred_from_schedule / missing |
| departure_utc, arrival_utc | string | Разрешённый ISO timestamp UTC с Z, иначе null |
| departure_utc_status, arrival_utc_status | string | converted / missing_time / incomplete_datetime / unknown_airport / unknown_timezone / invalid_datetime / ambiguous_local_time / nonexistent_local_time / timezone_conflict |
| departure_iana_timezone, arrival_iana_timezone | string | Пояс аэропорта, использованный/проверенный при UTC-конверсии |
| schedule_match_status | string | matched / ambiguous / temporal_unknown / reference_only / unresolved |
| schedule_id_used | string | Единственная разрешённая строка расписания, иначе null |
| geography_rule_version, geography_reference_version | string | Версия алгоритма и снимка справочника |
| inferred_fields_json | string | JSON-объект всех полей, заполненных из расписания; {} если таких нет |
| geography_complete, geography_inferred | bool | Известны обе точки; есть точка, выведенная по расписанию |
| time_complete, time_inferred | bool | Разрешены оба UTC; есть время, выведенное по расписанию |

### schedule_geography

Все исходные schedules. Для каждого префикса `origin_geo_`/`destination_geo_`
добавлены поля airport_id, iata, city, country, timezone, status, kind, reason,
candidates_json, override_json (string), latitude/longitude (float64). Исходные
origin/destination и origin_city/origin_country остаются без изменений.

### schedule_links

Все поля string: travel_id, source_id/source_record_id/locator исходной поездки;
schedule_id, schedule_source_id/schedule_source_record_id/schedule_locator строки
расписания; status (matched/ambiguous/temporal_unknown/reference_only),
temporal_coverage (confirmed/unknown/outside_period), rule_id, reason,
filled_fields_json и evidence_json. Свидетельство содержит маршрут, номер,
период, дни, локальные часы, +N и locator кандидата. filled_fields_json непустой
только у единственного разрешённого кандидата, который действительно что-то
дополнил. У ambiguous кандидатов он всегда {}.

### place_inventory

source_id, entity_table, field, raw, kind, status, airport_id, iata, city, country,
reason, candidates_json — string; count — int64. Одна строка на уникальное
исходное значение поля в источнике, включая null. Это инвентарь мест, а не людей.
Исходные города/страны расписания учитываются отдельными полями и не повышают
знаменатель airport coverage (он равен двум точкам на строку расписания).

## T12: canonical 1.0

Структура базовых полей наследует readers, а nullable для неключевых значений
расширен: несовместимое значение может стать null без удаления профиля.
Схемы — normalize/canonical.py. `observations` соответствует person_observations;
остальные имена базовых таблиц сохранены. PK и все исходные локальные ссылки
не перенумеровываются.

| Дополнительные поля | Тип | Значение |
|---|---|---|
| input_schema_sha256 | string | SHA-256 исходной Arrow-схемы; её описание сохранено в canonical_report.input_schemas |
| normalization_version | string | Версия правил canonical:1.0, во всех таблицах |
| source_values_json | string | Оригиналы изменённых базовых полей и extra:<имя> для новых входных полей |
| normalization_rules_json | string | Поле → rule_id последнего применённого изменения; полный рецепт задаёт конфигурация |
| normalization_status | string | ok / invalid_fields; межтабличные ошибки читаются отдельно |
| observations.name_comparison | string | NFKC + casefold + нормализация пробелов, без замены name_raw |
| observations.name_latin_comparison | string | Сравнительная ru_ascii_v1, не доказательство личности |
| identifiers.value_normalized | string | Номер по правилу kind с сохранёнными значимыми нулями/буквами |
| identifiers.programme_normalized | string | Отдельная программа, NFKC/casefold/пробелы |
| identifiers.identifier_namespace | string | JSON пространства имён: source или подтверждённый контекст либо programme |
| identifiers.identifier_match_key | string | JSON namespace + value; null, если контекст неизвестен |
| identifiers.namespace_status | string | source_scoped / programme_scoped / confirmed_override / unknown_programme / unsupported_identifier_kind / missing_identifier_value |
| travel_observations.flight_number_normalized | string | Отдельное очищенное представление номера; не объединяет codeshare |
| travel_observations.ticket_number_normalized | string | Строковый билет без значимых преобразований цифр |
| travel_observations.booking_code_normalized | string | Отдельное представление кода брони, регистр/внутренние символы сохраняются |

В normalization_errors все поля string: normalization_error_id, source_id,
entity_table, entity_id, source_record_id, locator, field, reason, value_json,
rule_id, severity. Идентификатор ошибки детерминирован по объекту/полю/причине.
value_json позволяет восстановить несовместимый вход, severity=error требует
проверки. Это не количество удалённых записей.

В transformation_log поля source_id/entity_table/field/rule_id/version и
examples_json строковые; affected_records и exceptions — int64. Примеры содержат
entity_id, locator, before, after. Числа относятся к принятым строкам после
удаления повторного чтения, а не к числу физических копий входных файлов.

В duplicate_reads строковые source_id/entity_table/entity_id/source_record_id/
locator, целочисленные input_copies и duplicates_removed. Одна строка описывает
точные копии одного ключа; содержательно похожие события с другими ключами не
сворачиваются здесь.
