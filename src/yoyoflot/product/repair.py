"""Audited republication of an incomplete XLSX coverage sidecar.

This is deliberately not a general snapshot editor. All six reader tables must
reproduce the original canonical input hashes; the original report and every
other published byte are retained. Hashes establish integrity, not signatures.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from ..contracts import SCHEMAS
from ..normalize.locking import OutputLock
from ..pipeline import _describe, _environment, _signature, _stage_environment
from ..provenance import fingerprint

VERSION = 'workbook-coverage-repair:1'
SOURCE = 'boardingpass_xlsx'
COVERAGE = f'sources/{SOURCE}/workbook_coverage.csv'
REPORT = f'sources/{SOURCE}/source_report.json'
PARQUETS = {name + '.parquet' for name in SCHEMAS}
VOLATILE_REPORT_FIELDS = {'seconds', 'process_peak_rss_mib'}
EVIDENCE_FILES = {
    'repair/parent_run_manifest.json', 'repair/fresh_source_report.json',
    'repair/fresh_environment.json', 'repair/raw_inventory.json',
    'repair/coverage_validation.json', 'repair/audit_code/repair.py',
    'repair/audit_code/coverage.py', 'repair_manifest.json',
}


def _load(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result
    result = json.loads(Path(path).read_text(encoding='utf-8'), object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise ValueError(f'Expected JSON object: {path}')
    return result


def _write(path, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def _file(root, name):
    relative = PurePosixPath(name)
    if not name or '\\' in name or relative.is_absolute() or relative.as_posix() != name or '..' in relative.parts:
        raise ValueError(f'Unsafe repair artifact path: {name}')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symlink is not a repair artifact: {name}')
    if not path.is_file():
        raise ValueError(f'Missing repair artifact: {name}')
    return path


def _request(root, manifest):
    inventory = _load(_file(root, 'input_manifest.json'))
    environment = _load(_file(root, 'environment.json'))
    if manifest.get('input_mode') != 'raw' or inventory.get('mode') != 'raw' or inventory.get('raw_files_verified') is not True:
        raise ValueError('Coverage repair requires an original raw-mode snapshot')
    request = _signature({'environment': environment, 'mode': 'raw', 'options': manifest['options'],
                          'inputs': {key: info['sha256'] for key, info in inventory['files'].items()}})
    if request != manifest.get('request_sha256'):
        raise ValueError('Original compute request fingerprint mismatch')
    raw = inventory['files'].get('raw:' + SOURCE)
    if not isinstance(raw, dict):
        raise ValueError('Missing original XLSX raw identity')
    return environment, raw


def _snapshot(root):
    manifest = _load(_file(root, 'run_manifest.json'))
    if manifest.get('pipeline_version') != '1.0' or manifest.get('status') not in {'complete', 'complete_with_limitations'}:
        raise ValueError('Repair requires a completed T16 snapshot')
    files = manifest.get('files')
    actual = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if not isinstance(files, dict) or not files or actual != set(files) | {'run_manifest.json'}:
        raise ValueError('Repair snapshot inventory mismatch')
    for name, expected in files.items():
        if _describe(_file(root, name)) != expected:
            raise ValueError('Repair snapshot checksum mismatch: ' + name)
    _request(root, manifest)
    return manifest


def _stable_report(report):
    return {key: value for key, value in report.items() if key not in VOLATILE_REPORT_FIELDS}


def _reader_inputs(canonical, source_report_sha):
    inputs = canonical.get('inputs', {})
    matches = [PurePosixPath(name).parent for name, value in inputs.items()
               if PurePosixPath(name).name == 'source_report.json' and value == source_report_sha
               and PurePosixPath(name).parent.name == SOURCE]
    if len(matches) != 1:
        raise ValueError('Canonical input must bind exactly one original XLSX source report')
    siblings = {PurePosixPath(name).name for name in inputs if PurePosixPath(name).parent == matches[0]}
    if siblings != PARQUETS | {'source_report.json'}:
        raise ValueError('Canonical XLSX inputs must contain exactly six reader Parquets and their report')
    result = {}
    for name in sorted(PARQUETS):
        key = str(matches[0] / name)
        value = inputs.get(key)
        if not isinstance(value, str) or len(value) != 64:
            raise ValueError('Missing canonical XLSX reader hash: ' + name)
        result[name] = value
    return result


def _key_bytes(workbook, sheet):
    return (json.dumps([workbook, sheet], ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def _coverage_keys(path):
    with path.open(encoding='utf-8', newline='') as stream:
        for row in csv.DictReader(stream):
            yield row['workbook'], row['sheet']


def _raw_inventory(raw, coverage):
    """Compare every raw ZIP workbook/sheet key, in order, with fresh coverage."""
    namespace = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
    keys = iter(_coverage_keys(coverage))
    digest = hashlib.sha256()
    workbooks = sheets = rows = 0
    with ZipFile(raw) as archive:
        seen = set()
        for member in archive.infolist():
            if member.is_dir():
                continue
            if member.filename in seen:
                raise ValueError('Duplicate raw ZIP member')
            seen.add(member.filename)
            if member.filename.lower().endswith('.xlsx'):
                workbooks += 1
                with archive.open(member) as source, tempfile.TemporaryFile() as temporary:
                    shutil.copyfileobj(source, temporary)
                    temporary.seek(0)
                    with ZipFile(temporary) as book:
                        document = ET.fromstring(book.read('xl/workbook.xml'))
                        names = [sheet.attrib['name'] for sheet in document.findall('m:sheets/m:sheet', namespace)]
                if len(names) != len(set(names)):
                    raise ValueError('Duplicate raw workbook sheet name')
                sheets += len(names)
            else:
                names = ['']
            for sheet in names:
                expected = (member.filename, sheet)
                if next(keys, None) != expected:
                    raise ValueError('Fresh coverage does not match ordered raw workbook/sheet inventory')
                digest.update(_key_bytes(*expected))
                rows += 1
    if next(keys, None) is not None:
        raise ValueError('Fresh coverage contains keys absent from raw ZIP')
    return {'version': 1, 'status': 'passed', 'method': 'raw_zip_workbook_xml_ordered_keys',
            'ordered_keys_sha256': digest.hexdigest(), 'rows': rows,
            'workbooks': workbooks, 'sheets': sheets}


def _check_inventory_evidence(coverage, inventory):
    digest = hashlib.sha256()
    workbooks = set()
    rows = sheets = 0
    for workbook, sheet in _coverage_keys(coverage):
        digest.update(_key_bytes(workbook, sheet))
        rows += 1
        if workbook.lower().endswith('.xlsx'):
            workbooks.add(workbook)
            sheets += bool(sheet)
    expected = {'version': 1, 'status': 'passed', 'method': 'raw_zip_workbook_xml_ordered_keys',
                'ordered_keys_sha256': digest.hexdigest(), 'rows': rows,
                'workbooks': len(workbooks), 'sheets': sheets}
    if inventory != expected:
        raise ValueError('Repair raw inventory evidence mismatch')


def _identity(record):
    return _signature({key: value for key, value in record.items() if key not in {'repair_sha256', 'run_id'}})


def verify_repair(root, *, verify_inventory=True):
    """Validate portable repair evidence without reopening historical raw paths.

    ``verify_inventory=False`` is for product admission immediately after its
    own complete T16 inventory/hash check. It does not skip repair semantics.
    """
    from .coverage import validate_xlsx_coverage

    root = Path(root).resolve()
    manifest = _snapshot(root) if verify_inventory else _load(_file(root, 'run_manifest.json'))
    record = _load(_file(root, 'repair_manifest.json'))
    if record.get('version') != VERSION or record.get('status') != 'verified_auxiliary_repair':
        raise ValueError('Unsupported or incomplete coverage repair')
    if not isinstance(record.get('original_coverage_failure'), str) or not record['original_coverage_failure']:
        raise ValueError('Repair lacks the original coverage failure')
    if record.get('repair_sha256') != _identity(record):
        raise ValueError('Repair identity mismatch')
    expected_id = 'data-' + record['original_request_sha256'][:20] + '-coverage-' + record['repair_sha256'][:20]
    if record.get('run_id') != expected_id or manifest.get('run_id') != expected_id:
        raise ValueError('Repair run identity mismatch')
    parent_file = _file(root, 'repair/parent_run_manifest.json')
    parent = _load(parent_file)
    if fingerprint(parent_file) != record.get('parent_manifest_sha256'):
        raise ValueError('Repair parent manifest mismatch')
    if 'repair_manifest.json' in parent.get('files', {}):
        raise ValueError('Nested coverage repairs are not supported')
    if record.get('original_request_sha256') != parent.get('request_sha256'):
        raise ValueError('Repair original request mismatch')
    if record.get('parent_run_id') != parent.get('run_id'):
        raise ValueError('Repair parent run identity mismatch')
    changed = {'files', 'run_id', 'created_at', 'repair'}
    if {k: v for k, v in manifest.items() if k not in changed} != {k: v for k, v in parent.items() if k not in changed}:
        raise ValueError('Repair changed inherited core metadata')
    if manifest.get('repair') != {'version': VERSION, 'repair_sha256': record['repair_sha256'],
                                  'parent_manifest_sha256': record['parent_manifest_sha256']}:
        raise ValueError('Repair manifest linkage mismatch')
    if set(manifest['files']) != set(parent['files']) | EVIDENCE_FILES:
        raise ValueError('Repair may only replace coverage and add the defined evidence files')
    for name, description in parent['files'].items():
        if name != COVERAGE and manifest['files'].get(name) != description:
            raise ValueError('Repair changed a retained artifact: ' + name)
    if record.get('old_coverage') != parent['files'].get(COVERAGE) or record.get('new_coverage') != manifest['files'].get(COVERAGE):
        raise ValueError('Repair coverage identity mismatch')
    if record['old_coverage'] == record['new_coverage']:
        raise ValueError('Repair did not change incomplete coverage')
    environment, raw = _request(root, manifest)
    if record.get('raw_input') != raw:
        raise ValueError('Repair raw identity mismatch')
    stage = _stage_environment('ingest:' + SOURCE, environment)
    fresh_environment = _load(_file(root, 'repair/fresh_environment.json'))
    if _stage_environment('ingest:' + SOURCE, fresh_environment) != stage or record.get('reader_environment') != stage:
        raise ValueError('Repair reader/dependency identity mismatch')
    original_report_path = _file(root, REPORT)
    original_report = _load(original_report_path)
    fresh_report = _load(_file(root, 'repair/fresh_source_report.json'))
    if _stable_report(original_report) != _stable_report(fresh_report):
        raise ValueError('Repair source report semantics mismatch')
    if record.get('stable_source_report_sha256') != _signature(_stable_report(original_report)):
        raise ValueError('Repair stable source report identity mismatch')
    if original_report.get('source_id') != SOURCE or original_report.get('status') != 'complete' or original_report.get('sha256') != raw['sha256']:
        raise ValueError('Repair requires a complete, raw-bound XLSX report')
    canonical = _load(_file(root, 'tables/canonical_report.json'))
    expected = _reader_inputs(canonical, fingerprint(original_report_path))
    descriptions = record.get('fresh_reader_files')
    if not isinstance(descriptions, dict) or set(descriptions) != PARQUETS:
        raise ValueError('Repair requires all six reader Parquet identities')
    for name, description in descriptions.items():
        if description.get('sha256') != expected[name] or description.get('rows') != original_report['counts'].get(name[:-8]):
            raise ValueError('Repair reader Parquet proof mismatch: ' + name)
    coverage = _file(root, COVERAGE)
    evidence = validate_xlsx_coverage(coverage, original_report, raw_input=raw)
    if evidence != _load(_file(root, 'repair/coverage_validation.json')):
        raise ValueError('Repair coverage validation evidence mismatch')
    _check_inventory_evidence(coverage, _load(_file(root, 'repair/raw_inventory.json')))
    for name in ('repair.py', 'coverage.py'):
        if record.get('audit_code', {}).get(name) != fingerprint(_file(root, 'repair/audit_code/' + name)):
            raise ValueError('Repair audit implementation identity mismatch')
    evidence_hashes = {name: manifest['files'][name]['sha256'] for name in EVIDENCE_FILES - {'repair_manifest.json'}}
    if record.get('evidence_sha256') != evidence_hashes:
        raise ValueError('Repair evidence identity mismatch')
    return record


def repair_workbook_coverage(old_core, fresh_reader, raw_zip, output=None):
    """Publish a new repair revision; existing snapshots are never mutated."""
    from . import coverage as coverage_module

    old_core, fresh_reader, raw_zip = map(lambda p: Path(p).resolve(), (old_core, fresh_reader, raw_zip))
    if output is not None:
        candidate = Path(output).resolve()
        if any(candidate == path or candidate in path.parents or path in candidate.parents
               for path in (old_core, fresh_reader, raw_zip)):
            raise ValueError('Repair output must not overlap original or fresh input artifacts')
    parent = _snapshot(old_core)
    if 'repair_manifest.json' in parent['files'] or any(name.startswith('repair/') for name in parent['files']):
        raise ValueError('The original unrepaired snapshot is required')
    parent_sha = fingerprint(old_core / 'run_manifest.json')
    environment, raw = _request(old_core, parent)
    raw_description = _describe(raw_zip)
    if raw_description != {key: raw[key] for key in ('sha256', 'bytes')}:
        raise ValueError('Original raw ZIP identity mismatch')
    current_environment = _environment()
    stage = _stage_environment('ingest:' + SOURCE, environment)
    if stage != _stage_environment('ingest:' + SOURCE, current_environment):
        raise ValueError('Frozen XLSX reader/dependency identities changed')
    original_report_path = _file(old_core, REPORT)
    original_report = _load(original_report_path)
    fresh_report_path = _file(fresh_reader, 'source_report.json')
    fresh_report = _load(fresh_report_path)
    if _stable_report(original_report) != _stable_report(fresh_report):
        raise ValueError('Fresh source report semantics differ from original')
    if original_report.get('status') != 'complete' or original_report.get('sha256') != raw['sha256']:
        raise ValueError('Original source report is not complete and raw-bound')
    canonical = _load(_file(old_core, 'tables/canonical_report.json'))
    expected = _reader_inputs(canonical, fingerprint(original_report_path))
    fresh_files = {name: _describe(_file(fresh_reader, name)) for name in sorted(PARQUETS)}
    for name, description in fresh_files.items():
        if description['sha256'] != expected[name]:
            raise ValueError('Fresh reader Parquet differs from original canonical input: ' + name)
    fresh_coverage = _file(fresh_reader, 'workbook_coverage.csv')
    coverage_evidence = coverage_module.validate_xlsx_coverage(fresh_coverage, fresh_report, raw_input=raw)
    inventory_evidence = _raw_inventory(raw_zip, fresh_coverage)
    old_coverage = _file(old_core, COVERAGE)
    try:
        coverage_module.validate_xlsx_coverage(old_coverage, original_report, raw_input=raw)
        _raw_inventory(raw_zip, old_coverage)
    except ValueError as error:
        original_failure = str(error)
    else:
        raise ValueError('Original coverage is complete; no narrow repair is justified')
    new_coverage = _describe(fresh_coverage)
    if new_coverage == parent['files'].get(COVERAGE):
        raise ValueError('Coverage repair requires changed auxiliary bytes')
    parent_output = Path(output).resolve().parent if output is not None else old_core.parent
    if any(parent_output == path or path in parent_output.parents
           for path in (old_core, fresh_reader, raw_zip)):
        raise ValueError('Repair staging and lock must be outside all input artifacts')
    parent_output.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.coverage-repair-', dir=parent_output))
    try:
        # Hard links conserve disk without writing through either immutable tree.
        for name in parent['files']:
            target = staging / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if name == COVERAGE:
                shutil.copyfile(fresh_coverage, target)
            else:
                os.link(_file(old_core, name), target)
        shutil.copyfile(old_core / 'run_manifest.json', staging / 'repair-parent.tmp')
        (staging / 'repair').mkdir()
        (staging / 'repair-parent.tmp').rename(staging / 'repair/parent_run_manifest.json')
        shutil.copyfile(fresh_report_path, staging / 'repair/fresh_source_report.json')
        _write(staging / 'repair/fresh_environment.json', current_environment)
        _write(staging / 'repair/raw_inventory.json', inventory_evidence)
        _write(staging / 'repair/coverage_validation.json', coverage_evidence)
        audit_code = {'repair.py': Path(__file__), 'coverage.py': Path(coverage_module.__file__)}
        for name, source in audit_code.items():
            target = staging / 'repair/audit_code' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        evidence_hashes = {name: fingerprint(staging / name) for name in EVIDENCE_FILES - {'repair_manifest.json'}}
        record = {'version': VERSION, 'status': 'verified_auxiliary_repair',
                  'scope': 'Only workbook coverage is replaced; original reader report and all downstream data bytes retained.',
                  'parent_manifest_sha256': parent_sha, 'parent_run_id': parent['run_id'],
                  'original_request_sha256': parent['request_sha256'], 'raw_input': raw,
                  'original_coverage_failure': original_failure,
                  'old_coverage': parent['files'][COVERAGE], 'new_coverage': new_coverage,
                  'reader_environment': stage, 'fresh_reader_files': fresh_files,
                  'stable_source_report_sha256': _signature(_stable_report(original_report)),
                  'audit_code': {name: fingerprint(source) for name, source in audit_code.items()},
                  'evidence_sha256': evidence_hashes}
        record['repair_sha256'] = _identity(record)
        record['run_id'] = 'data-' + parent['request_sha256'][:20] + '-coverage-' + record['repair_sha256'][:20]
        destination = Path(output).resolve() if output is not None else parent_output / record['run_id']
        if any(destination == path or destination in path.parents or path in destination.parents
               for path in (old_core, fresh_reader, raw_zip)):
            raise ValueError('Repair output must not overlap original or fresh input artifacts')
        if destination.name != record['run_id']:
            raise ValueError('Repair output must use its derived run_id: ' + record['run_id'])
        _write(staging / 'repair_manifest.json', record)
        manifest = dict(parent, run_id=record['run_id'], created_at=datetime.now(timezone.utc).isoformat(),
                        repair={'version': VERSION, 'repair_sha256': record['repair_sha256'], 'parent_manifest_sha256': parent_sha})
        manifest['files'] = {name: (_describe(staging / name) if name == COVERAGE else info)
                             for name, info in parent['files'].items()}
        manifest['files'].update({name: _describe(staging / name) for name in EVIDENCE_FILES})
        _write(staging / 'run_manifest.json', manifest)
        # A complete recheck also covers every hard-linked downstream byte.
        verify_repair(staging)
        if _describe(raw_zip) != raw_description or fingerprint(old_core / 'run_manifest.json') != parent_sha:
            raise ValueError('Repair inputs changed during verification')
        if _describe(_file(old_core, COVERAGE)) != parent['files'][COVERAGE]:
            raise ValueError('Original coverage changed during repair')
        if any(_describe(_file(fresh_reader, name)) != description for name, description in fresh_files.items()):
            raise ValueError('Fresh reader output changed during repair')
        if _stage_environment('ingest:' + SOURCE, _environment()) != stage:
            raise ValueError('Reader implementation changed during repair')
        if any(fingerprint(source) != record['audit_code'][name] for name, source in audit_code.items()):
            raise ValueError('Audit implementation changed during repair')
        with OutputLock(parent_output / (record['run_id'] + '.repair.lock'), label='Coverage repair'):
            if destination.exists():
                existing = verify_repair(destination)
                if existing != record:
                    raise FileExistsError('Existing repair differs from verified inputs')
                return {'snapshot': str(destination), 'run_id': record['run_id'], 'repair_sha256': record['repair_sha256'], 'reused': True}
            os.rename(staging, destination)
            return {'snapshot': str(destination), 'run_id': record['run_id'], 'repair_sha256': record['repair_sha256'], 'reused': False}
    finally:
        if staging.exists():
            shutil.rmtree(staging)
