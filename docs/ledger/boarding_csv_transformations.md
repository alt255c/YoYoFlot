# Transformation Ledger: BoardingData CSV

## Source Information
- **File**: `data/BoardingData.csv`
- **Format**: CSV with `;` delimiter
- **Size**: ~18.3 MB
- **Logical Source ID**: `boarding_data_csv_v1`
- **Rows**: ~155,148

## Reading Method
- **Library**: PyArrow CSV reader
- **Chunk Size**: 10,000 rows per chunk
- **Data Types**: All columns read as `string` to preserve leading zeros

## Transformation Rules

### 1. Chunked Reading
- Data is read in chunks using PyArrow for memory efficiency
- Each chunk is processed independently

### 2. Data Type Preservation
- All columns are explicitly cast to `string` type
- Leading zeros in `PassengerDocument` and `TicketNumber` are preserved
- Example: `0012 345678` remains as string, not converted to number

### 3. Empty Row Filtering
- Completely empty rows (all fields empty) are filtered out
- This handles malformed data without breaking the pipeline

### 4. Null Normalization (separate step)
- Values `"Not presented"`, `"None"`, `""` are converted to null
- Applied to columns: `PassengerDocument`, `TicketNumber`, `BookingCode`, `Baggage`, `CodeShare`
- Raw values are preserved in the initial read; normalization is explicit

### 5. Provenance/Locator Columns Added
- `source_file`: Original filename (e.g., `BoardingData.csv`)
- `row_number`: Sequential 1-based index of the processed row
- `origin`: Explicitly set to null (not provided in source)

## Business Logic Constraints

### No Travel Proof
A booking or boarding observation in this dataset is strictly an **observation**. It is **NOT** treated as proven actual travel.

### No Origin Inference
The `origin` field is intentionally left null. No assumptions are made based on flight numbers or destinations.

## Exceptions & Quarantine
- **Empty Rows**: Filtered out during reading, not counted in row numbers
- **Malformed Rows**: PyArrow will raise errors on structural issues; these should be logged and investigated

## Quality Summary (Initial Run)
- Total processed rows: ~155,148
- Rows with null `PassengerDocument`: ~38,900 (25%)
- Rows with null `TicketNumber`: ~38,900 (25%)
- Baggage values: `Registered`, `Transit`, `Delayed`, `None` (null after normalization)
- CodeShare values: `Own`, `Operated`

## Version History
- **v1**: Initial implementation with PyArrow chunked reading

## Quality Summary (фактический прогон, 04.10.2026)
- **total_processed_rows**: 155147
- **skipped blank lines**: 1 (физическая строка 2 исходного файла)
- **malformed rows skipped**: 0 (invalid_row_handler не срабатывал)
- **null PassengerDocument**: 0 (колонка всегда заполнена, паттерн `NNNN NNNNNN`)
- **null BookingCode**: 77761 (маркер `"Not presented"`, 50.12%)
- **null TicketNumber**: 77800 (маркер `"Not presented"`, 50.15%)
- **null Baggage**: 38932 (маркер `"None"`, 25.09%)
- **null CodeShare**: 0 (значения `"Own"` и `"Operated"` всегда присутствуют)
- **Наблюдение**: 39 строк имеют заполненный BookingCode, но null TicketNumber.
- **Независимая проверка**: csv-пересчёт через модуль `csv` даёт идентичные цифры.

## Locator semantics
- `row_number` — порядковый номер **обработанной** записи (после пропуска пустых строк),
  а не физическая строка файла; физическая строка = row_number + 1 (header) + количество
  пропущенных пустых строк выше. Полная line-level привязка уточняется в T13 (lineage).