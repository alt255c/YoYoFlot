# T08 Sirena-export-fixed.tab

Реальный файл UTF-8: 336 символов каждой строки, включая заголовок. Диапазоны
из configs/sirena_columns.yaml подтверждены по всему входу и выборке 20 строк.
unit=characters, zero-based-half-open; нулевая позиция включительна, правая
граница исключительна. Сравнивается полный expected_header; символы кириллицы
не считаются отдельными байтами. Пустое поле не сдвигает соседние.

Все 20 колонок: PaxName, PaxBirthDate, DepartDate, DepartTime, ArrivalDate,
ArrivalTime, Flight, CodeSh, From, Dest, Code, e-Ticket, TravelDoc, Seat, Meal,
TrvCls, Fare, Baggage, PaxAdditionalInfo, AgentInfo. PaxName [0:60], даты/время
по 12 символов, дальнейшие точные позиции записаны в YAML. Code — booking_code,
CodeSh — codeshare; e-Ticket/TravelDoc строковые. N/A отсутствует, raw сохраняется.
Карты `FF#FB 00123` извлекаются из PaxAdditionalInfo с programme/value; если карта
одна, travel связан с ней. При нескольких картах произвольная связь не создаётся.

Полный баланс: 155757 строк, 363431 identifiers, 155757 поездок, errors=0.
20 распределённых по файлу строк сравнены по всем исходным колонкам и выходным
датам/рейсам/аэропортам. Журнал: validation/sirena_audit_20.csv. Иные раскладки
требуют собственного confirmed-конфига; короткая строка/хвост фиксируются в errors.
