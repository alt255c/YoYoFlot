"""Semantic admission checks for the XLSX reader's published coverage ledger.

CSV hashes alone cannot prove that every sheet was written. A disk-backed key
index keeps the check bounded while joining the ledger to canonical locators.
This does not reopen historical raw paths or replace source-error acceptance.
"""
from __future__ import annotations

import csv
import re
import sqlite3
import tempfile
from collections import Counter
from contextlib import closing
from pathlib import Path

import pyarrow.parquet as pq

from ..provenance import fingerprint

VERSION = 'xlsx-coverage:1.0'
SOURCE = 'boardingpass_xlsx'
FIELDS = ('workbook', 'sheet', 'header_version', 'input_rows', 'accepted_rows',
          'errors', 'status', 'reason')
_SHA = re.compile(r'[0-9a-f]{64}')
_HEADER = re.compile(r'[0-9a-f]{12}')
_INTEGER = re.compile(r'0|[1-9][0-9]*')
_LOCATOR = re.compile(r'(.+)/row:([1-9][0-9]*)(/.*)?\Z')


def _fail(message):
    raise ValueError('XLSX coverage: ' + message)


def _count(value, label):
    if type(value) is not int or value < 0:
        _fail('invalid nonnegative count: ' + label)
    return value


def _row(row, line):
    if set(row) != set(FIELDS) or any(value is None for value in row.values()):
        _fail(f'malformed CSV row {line}')
    if not row['workbook'] or any('\x00' in value for value in row.values()):
        _fail(f'invalid workbook/sheet row {line}')
    for key in ('input_rows', 'accepted_rows', 'errors'):
        if not _INTEGER.fullmatch(row[key]):
            _fail(f'invalid {key} at CSV row {line}')
        row[key] = int(row[key])
    n, accepted, errors = (row[key] for key in ('input_rows', 'accepted_rows', 'errors'))
    sheet, version, status, reason = (row[key] for key in ('sheet', 'header_version', 'status', 'reason'))
    xlsx = row['workbook'].lower().endswith('.xlsx')
    if accepted > n:
        _fail(f'accepted rows exceed input at CSV row {line}')
    if not sheet:
        valid = not version and n == accepted == 0 and status == 'excluded' and (
            (not xlsx and reason == 'non_xlsx_member' and errors == 0)
            or (xlsx and reason == 'unreadable_workbook' and errors == 1))
    elif not xlsx:
        valid = False
    elif status == 'empty':
        valid = not version and not reason and n == accepted == errors == 0
    elif version == 'boarding_card_v1':
        valid = n == 1 and ((status == 'ok' and accepted == 1 and not reason)
                            or (status == 'excluded' and accepted == 0 and errors == 1
                                and reason == 'unsupported_boarding_card_layout'))
    elif status == 'excluded':
        valid = not version and accepted == 0 and errors == 1 and reason in {
            'duplicate_or_blank_headers', 'unsupported_header'}
    else:
        # A retained row can have several cell errors/warnings. They must not
        # be subtracted from accepted_rows. Rejected column-count rows each
        # contribute at least one error, however.
        valid = bool(_HEADER.fullmatch(version)) and status == 'ok' and not reason and errors >= n - accepted
    if not valid:
        _fail(f'inconsistent layout/status at CSV row {line}')
    return row


def _canonical(db, tables, report, source_report):
    reports = report.get('source_reports')
    if not isinstance(reports, dict):
        _fail('missing canonical source reports')
    matches = [(key, value) for key, value in reports.items()
               if isinstance(value, dict) and value.get('source_id') == SOURCE]
    if len(matches) != 1 or matches[0][1] != source_report:
        _fail('canonical source report mismatch')
    inputs = report.get('inputs', {})
    if not isinstance(inputs, dict) or not isinstance(inputs.get(matches[0][0]), str) or not _SHA.fullmatch(inputs[matches[0][0]]):
        _fail('missing canonical source report fingerprint')
    balances = report.get('balances')
    if not isinstance(balances, list) or any(not isinstance(item, dict) for item in balances):
        _fail('missing canonical source balances')
    archive = source_report['input_path'].replace('\\', '/').rsplit('/', 1)[-1] + '/'
    totals = {}
    severities = Counter()
    for table, count_key, column in (
            ('observations', 'person_observations', 'persons_seen'),
            ('travel_observations', 'travel_observations', 'travels_seen'),
            ('errors', 'errors', 'errors_seen')):
        path = Path(tables) / (table + '.parquet')
        if not path.is_file() or path.is_symlink():
            _fail('missing canonical evidence: ' + table)
        fields = ['source_id', 'locator'] + (['severity'] if table == 'errors' else [])
        parquet = pq.ParquetFile(path)
        if not set(fields) <= set(parquet.schema_arrow.names):
            _fail('invalid canonical evidence schema: ' + table)
        total = 0
        for batch in parquet.iter_batches(batch_size=8192, columns=fields):
            counts = Counter()
            for row in batch.to_pylist():
                if row['source_id'] != SOURCE:
                    continue
                locator = row['locator']
                if not isinstance(locator, str):
                    _fail('missing canonical locator: ' + table)
                match = _LOCATOR.fullmatch(locator)
                if not match:
                    if table != 'errors':
                        _fail('invalid canonical locator: ' + table)
                    key = (locator, '')  # unreadable workbook error
                else:
                    prefix, _, suffix = match.groups()
                    if (not prefix.startswith(archive) or '/' not in prefix[len(archive):]
                            or (table == 'observations' and suffix is not None)
                            or (table == 'travel_observations' and suffix != '/travel')):
                        _fail('invalid canonical locator: ' + table)
                    key = tuple(prefix[len(archive):].rsplit('/', 1))
                counts[key] += 1
                total += 1
                if table == 'errors':
                    severities[row['severity']] += 1
            cursor = db.executemany(f'UPDATE coverage SET {column}={column}+? WHERE workbook=? AND sheet=?',
                                    [(n, *key) for key, n in counts.items()])
            if cursor.rowcount != len(counts):
                _fail('canonical evidence references an unlisted workbook/sheet: ' + table)
        expected = source_report['counts'][count_key]
        if total != expected:
            _fail('canonical source count mismatch: ' + table)
        source_balances = [b for b in balances if b.get('source_id') == SOURCE and b.get('entity_table') == table]
        if not source_balances and total == 0:
            pass  # T12 emits balances only for sources with retained rows.
        elif len(source_balances) != 1:
            _fail('canonical source balance missing/duplicated: ' + table)
        else:
            balance = source_balances[0]
            incoming, outgoing, duplicates = (_count(balance.get(key), table + '.' + key)
                                              for key in ('input_rows', 'output_rows', 'duplicates_removed'))
            if outgoing != total or incoming != outgoing + duplicates:
                _fail('canonical source balance mismatch: ' + table)
        totals[table] = total
    if dict(severities) != {k: v for k, v in source_report['error_severities'].items() if v}:
        _fail('canonical error severity mismatch')
    mismatch = db.execute('SELECT count(*) FROM coverage WHERE persons_seen<>accepted_rows '
                          'OR travels_seen<>accepted_rows OR errors_seen<>errors').fetchone()[0]
    if mismatch:
        _fail('canonical evidence count differs for workbook/sheet')
    return dict(totals, per_sheet_counts_verified=True)


def validate_xlsx_coverage(coverage_path, source_report, *, canonical_report=None,
                           canonical_tables=None, raw_input=None):
    """Return audit evidence, or reject an incomplete/malformed coverage ledger.

    Reports are parsed dictionaries. ``raw_input`` is the XLSX entry in the
    T16 input manifest, not permission to reopen its historical path. Supplying
    canonical_report and canonical_tables additionally verifies every sheet's
    observation/travel/error counts and the embedded source report. Callers
    admitting a snapshot also verify the source-report *file* fingerprint.
    """
    path = Path(coverage_path)
    if not path.is_file() or path.is_symlink():
        _fail('missing coverage CSV')
    if (not isinstance(source_report, dict) or source_report.get('source_id') != SOURCE
            or source_report.get('contract_version') != '1.0'
            or source_report.get('status') not in {'complete', 'complete_with_errors'}):
        _fail('invalid source report')
    source_hash = source_report.get('sha256')
    source_path = source_report.get('input_path')
    if not isinstance(source_hash, str) or not _SHA.fullmatch(source_hash) or not isinstance(source_path, str) or not source_path:
        _fail('missing source fingerprint/path')
    counts, counters, severities = (source_report.get(key) for key in ('counts', 'input_counts', 'error_severities'))
    if not all(isinstance(value, dict) for value in (counts, counters, severities)):
        _fail('missing source counters')
    for label, values in (('counts', counts), ('input_counts', counters), ('error_severities', severities)):
        for key, value in values.items():
            if not isinstance(key, str):
                _fail('invalid counter name')
            _count(value, label + '.' + key)
    for key in ('person_observations', 'travel_observations', 'errors'):
        _count(counts.get(key), key)
    if (sum(severities.values()) != counts['errors'] or
            (source_report['status'] == 'complete') != (severities.get('error', 0) == 0)):
        _fail('source error status/count mismatch')
    if raw_input is not None:
        if (not isinstance(raw_input, dict) or raw_input.get('sha256') != source_hash
                or raw_input.get('path') != source_path):
            _fail('raw inventory source binding mismatch')
        _count(raw_input.get('bytes'), 'raw bytes')
    if (canonical_report is None) != (canonical_tables is None):
        _fail('canonical report and tables must be supplied together')
    before = fingerprint(path)
    with tempfile.TemporaryDirectory(prefix='yoyoflot-xlsx-coverage-') as temporary:
        with closing(sqlite3.connect(str(Path(temporary) / 'keys.sqlite'))) as db:
            db.execute('PRAGMA journal_mode=OFF')
            db.execute('PRAGMA synchronous=OFF')
            db.execute('PRAGMA cache_size=-8192')
            db.execute('CREATE TABLE coverage (workbook TEXT, sheet TEXT, input_rows INTEGER, '
                       'accepted_rows INTEGER, errors INTEGER, header_version TEXT, reason TEXT, '
                       'persons_seen INTEGER DEFAULT 0, travels_seen INTEGER DEFAULT 0, errors_seen INTEGER DEFAULT 0, '
                       'PRIMARY KEY(workbook,sheet)) WITHOUT ROWID')
            totals = Counter()
            try:
                with path.open(encoding='utf-8-sig', newline='') as stream:
                    reader = csv.DictReader(stream, strict=True)
                    if reader.fieldnames != list(FIELDS):
                        _fail('CSV schema mismatch')
                    for line, row in enumerate(reader, 2):
                        row = _row(row, line)
                        db.execute('INSERT INTO coverage(workbook,sheet,input_rows,accepted_rows,errors,header_version,reason) '
                                   'VALUES(?,?,?,?,?,?,?)', [row[key] for key in
                                    ('workbook', 'sheet', 'input_rows', 'accepted_rows', 'errors', 'header_version', 'reason')])
                        totals['coverage_rows'] += 1
                        totals['sheets'] += bool(row['sheet'])
                        totals['boarding_card_sheets'] += row['header_version'] == 'boarding_card_v1'
                        totals['excluded_rows'] += row['input_rows'] if row['reason'] in {'duplicate_or_blank_headers', 'unsupported_header'} else 0
                        for key in ('input_rows', 'accepted_rows', 'errors'):
                            totals[key] += row[key]
            except sqlite3.IntegrityError as exc:
                raise ValueError('XLSX coverage: duplicate workbook/sheet') from exc
            except (csv.Error, UnicodeError, OverflowError) as exc:
                raise ValueError('XLSX coverage: malformed CSV') from exc
            totals['workbooks'] = db.execute("SELECT count(DISTINCT workbook) FROM coverage WHERE lower(workbook) LIKE '%.xlsx'").fetchone()[0]
            if db.execute("SELECT count(*) FROM coverage a WHERE a.sheet='' AND EXISTS "
                          "(SELECT 1 FROM coverage b WHERE b.workbook=a.workbook AND b.sheet<>'')").fetchone()[0]:
                _fail('workbook exclusion conflicts with sheet entries')
            for key in ('workbooks', 'sheets', 'input_rows', 'boarding_card_sheets', 'excluded_rows'):
                if totals[key] != counters.get(key, 0):
                    _fail('source report total mismatch: ' + key)
            if (totals['accepted_rows'] != counts['person_observations']
                    or totals['accepted_rows'] != counts['travel_observations'] or totals['errors'] != counts['errors']):
                _fail('source report accepted/error total mismatch')
            evidence = _canonical(db, canonical_tables, canonical_report, source_report) if canonical_report is not None else None
    if fingerprint(path) != before:
        _fail('CSV changed during validation')
    return dict(validator_version=VERSION, status='passed', coverage_sha256=before,
                **{key: totals[key] for key in ('coverage_rows', 'workbooks', 'sheets', 'input_rows',
                                                'accepted_rows', 'errors', 'boarding_card_sheets', 'excluded_rows')},
                source_sha256=source_hash, canonical_evidence=evidence)
