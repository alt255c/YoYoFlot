"""Verify published source artifacts and retain their upstream qualifications.

Manifest hashes establish integrity and a reproducible chain, not a digital
signature or independent proof that the producer used authentic raw sources.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

import pyarrow.parquet as pq

from ..provenance import fingerprint
from ..resolve import VERSION
from ..resolve.common import SCHEMAS, stable_id
from ..resolve.geography_validation import T11_FILES, validate_reference_artifacts
from .coverage import validate_xlsx_coverage

# These are the original LFS object IDs committed in develop, not new data.
SUPPLIED = {
    'persons.parquet': 'ec5948ba0a6941164c94bd8d8ee05c5a07125b3964aba1a8e91419ee12252a53',
    'segments.parquet': 'f7c2f83f1a99d5fc555f56eb622ed43da74f5d8b0b963dd3513eedfadfe40354',
}
T13_FILES = {name + '.parquet' for name in SCHEMAS} | {
    'resolution_report.json', 'resolution_review_sample.csv', 'resolution_summary.txt',
}
UPSTREAM_FILES = {
    'canonical_manifest.json', 't11_manifest.json', 'observations.parquet',
    'identifiers.parquet', 'travel_observations.parquet', 'canonical_report.json',
    'travel_geography.parquet', 'geography_report.json',
} | T11_FILES
SHA256 = re.compile(r'[0-9a-f]{64}')
PERSONAL_SOURCES = {'boarding_csv', 'boardingpass_xlsx', 'forum_json', 'pointz_xml', 'sirena_fixed', 'skyteam_yaml'}


def _json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key in {path.name}: {key}')
            result[key] = value
        return result

    value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=unique)
    if not isinstance(value, dict):
        raise ValueError(f'Expected a JSON object: {path.name}')
    return value


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def _sha256(value):
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _count(value):
    return type(value) is int and value >= 0


def _strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(f'Invalid {label}')
    return value


def _safe_file(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError(f'Unsafe artifact path: {name!r}')
    relative = PurePosixPath(name)
    if relative.is_absolute() or relative.as_posix() != name or '..' in relative.parts:
        raise ValueError(f'Unsafe artifact path: {name!r}')
    path = root
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symlink is not a published source artifact: {name}')
    if not path.is_file():
        raise ValueError(f'Missing published source artifact: {name}')
    return path


class _VerifiedFiles:
    def __init__(self):
        self.paths = {}
        self.hashes = {}
        self.cache = {}

    def add(self, root, name, expected=None, prefix=''):
        path = _safe_file(root, name)
        digest = self.cache.get(path)
        if digest is None:
            digest = fingerprint(path)
            self.cache[path] = digest
        if expected is not None and (not _sha256(expected) or digest != expected):
            raise ValueError(f'Published source checksum mismatch: {name}')
        key = prefix + name
        # A T16 inventory also names the already verified tables. Keep one
        # entry per physical path so the product's final recheck hashes it once.
        if path not in self.paths.values():
            self.paths[key], self.hashes[key] = path, digest
        return path


def _manifest(root, name, verified, expected=None):
    manifest = _json(verified.add(root, name))
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError(f'Invalid published file set: {name}')
    if expected is not None and set(files) != expected:
        raise ValueError(f'Unexpected published file set: {name}')
    for filename, digest in files.items():
        if not _sha256(digest):
            raise ValueError(f'Invalid published checksum: {filename}')
        verified.add(root, filename, digest)
    return manifest


def _report_limitations(report, label):
    limitations = [f'{label}: {item}' for item in _strings(report.get('limitations'), label + ' limitations')]
    acceptance = report.get('acceptance')
    if not isinstance(acceptance, str) or not acceptance:
        raise ValueError(f'Missing {label} acceptance status')
    limitations.append(f'{label} acceptance: {acceptance}.')
    return limitations


def _upstream_limitations(report):
    upstream = report.get('upstream')
    if not isinstance(upstream, dict):
        raise ValueError('Missing T13 upstream status')
    status = upstream.get('canonical_status')
    errors = upstream.get('canonical_normalization_errors')
    severities = upstream.get('reader_error_severities')
    schedules = upstream.get('schedule_sources')
    if status not in {'complete', 'complete_with_errors'} or not _count(errors):
        raise ValueError('Invalid T13 upstream canonical status/errors')
    if not isinstance(severities, dict) or any(not isinstance(k, str) or not _count(v) for k, v in severities.items()):
        raise ValueError('Invalid T13 upstream reader error severities')
    if not isinstance(schedules, list) or any(not isinstance(item, dict) for item in schedules):
        raise ValueError('Invalid T13 upstream schedule sources')
    if (errors or severities.get('error', 0)) and status != 'complete_with_errors':
        raise ValueError('T13 upstream errors disagree with canonical status')
    limitations = []
    if status != 'complete' or errors:
        limitations.append(f'T12 upstream status: {status}; normalization errors: {errors}.')
    if any(severities.values()):
        limitations.append('Upstream reader error severities: ' + json.dumps(severities, sort_keys=True) + '.')
    for source in schedules:
        if source.get('coverage') != 'confirmed':
            limitations.append('Upstream schedule coverage unconfirmed: ' + json.dumps(source, sort_keys=True) + '.')
    missing = _strings(report.get('personal_sources_missing'), 'T13 missing personal sources')
    present = _strings(report.get('personal_sources_present'), 'T13 present personal sources')
    if (set(present) & set(missing) or set(present) | set(missing) != PERSONAL_SOURCES
            or len(set(present)) != len(present) or len(set(missing)) != len(missing)):
        raise ValueError('Inconsistent T13 personal source coverage')
    limitations.extend('Unobserved personal source: ' + name + '.' for name in missing)
    return limitations


def _verify_t13(root, verified):
    manifest = _manifest(root, 'resolution_manifest.json', verified, T13_FILES)
    report = _json(verified.paths['resolution_report.json'])
    if manifest.get('version') != VERSION or report.get('version') != VERSION:
        raise ValueError('T13 manifest/report version mismatch')
    if report.get('status') != 'complete':
        raise ValueError('T13 report is not a completed resolution')
    inputs = report.get('inputs')
    if not isinstance(inputs, dict) or not inputs or any(
            not isinstance(name, str) or not name or not _sha256(value) for name, value in inputs.items()):
        raise ValueError('Invalid T13 report input fingerprints')
    basenames = {name.replace('\\', '/').rsplit('/', 1)[-1] for name in inputs}
    if not UPSTREAM_FILES <= basenames:
        raise ValueError('Incomplete T13 upstream fingerprints')
    for name in UPSTREAM_FILES:
        expected = {digest for original, digest in inputs.items()
                    if original.replace('\\', '/').rsplit('/', 1)[-1] == name}
        if len(expected) != 1:
            raise ValueError('Conflicting T13 upstream fingerprint: ' + name)
    config_hash = report.get('config_sha256')
    if not _sha256(config_hash) or config_hash not in inputs.values():
        raise ValueError('T13 configuration fingerprint missing from inputs')
    run = stable_id('run', sorted(inputs.values()))
    if report.get('run_fingerprint') != run or manifest.get('run_fingerprint') != run:
        raise ValueError('T13 run_fingerprint mismatch')
    counts = report.get('counts')
    if not isinstance(counts, dict) or set(counts) != set(SCHEMAS):
        raise ValueError('Incomplete T13 report counts')
    for name, schema in SCHEMAS.items():
        parquet = pq.ParquetFile(verified.paths[name + '.parquet'])
        if not parquet.schema_arrow.equals(schema, check_metadata=True):
            raise ValueError('T13 schema/version mismatch: ' + name)
        if not _count(counts[name]) or parquet.metadata.num_rows != counts[name]:
            raise ValueError('T13 report row count mismatch: ' + name)
    limitations = _report_limitations(report, 'T13') + _upstream_limitations(report)
    limitations.extend(_verify_available_upstream(root, report, verified))
    limitations.append('Published manifest hashes verify artifact integrity and provenance consistency; they are not a digital signature or independent raw-source authentication.')
    return report, limitations


def _verify_available_upstream(root, report, verified):
    """Verify transferred upstream evidence, without reopening historical paths."""
    missing = []
    for name in sorted(UPSTREAM_FILES):
        path = root / name
        if not path.exists() and not path.is_symlink():
            missing.append(name)
            continue
        expected = [digest for original, digest in report['inputs'].items()
                    if original.replace('\\', '/').rsplit('/', 1)[-1] == name]
        if len(set(expected)) != 1:
            raise ValueError('Conflicting T13 upstream fingerprint: ' + name)
        verified.add(root, name, expected[0])
    from ..normalize.canonical import SCHEMAS as canonical_schemas

    canonical_files = {name + '.parquet' for name in canonical_schemas} | {
        'normalization_errors.parquet', 'transformation_log.parquet', 'duplicate_reads.parquet',
        'canonical_report.json', 'canonical_summary.txt',
    }
    geography_files = T11_FILES
    for name, version_key, version, expected in (
            ('canonical_manifest.json', 'version', 'canonical:1.0', canonical_files),
            ('t11_manifest.json', 'rule_version', 'airports:1.1', geography_files)):
        if name in verified.paths:
            manifest = _manifest(root, name, verified, expected)
            if manifest.get(version_key) != version:
                raise ValueError('Unsupported upstream manifest version: ' + name)
    limitations = []
    if missing:
        limitations.append('Upstream files are not included in this T13 transfer; their recorded fingerprints and status are retained, without re-reading original raw sources: ' + ', '.join(missing) + '.')
    for name, label, statuses in (
            ('canonical_report.json', 'T12', {'complete', 'complete_with_errors'}),
            ('geography_report.json', 'T11', {'complete'})):
        if name not in verified.paths:
            continue
        upstream = _json(verified.paths[name])
        if upstream.get('status') not in statuses:
            raise ValueError('Upstream report is not complete: ' + name)
        limitations.extend(_report_limitations(upstream, label))
        if label == 'T12':
            if upstream.get('version') != 'canonical:1.0':
                raise ValueError('Unsupported upstream canonical report version')
            for table, schema in canonical_schemas.items():
                filename = table + '.parquet'
                if filename in verified.paths:
                    parquet = pq.ParquetFile(verified.paths[filename])
                    if not parquet.schema_arrow.equals(schema, check_metadata=True):
                        raise ValueError('Upstream canonical schema mismatch: ' + table)
                    if parquet.metadata.num_rows != upstream.get('counts', {}).get(table):
                        raise ValueError('Upstream canonical count mismatch: ' + table)
            for report_key, upstream_key in (('status', 'canonical_status'),
                                           ('normalization_errors', 'canonical_normalization_errors'),
                                           ('source_error_severities', 'reader_error_severities')):
                if upstream.get(report_key) != report['upstream'].get(upstream_key):
                    raise ValueError('T13 upstream report mismatch: ' + report_key)
            source_reports = upstream.get('source_reports', {})
            if not isinstance(source_reports, dict):
                raise ValueError('Invalid T12 source reports')
            for source, detail in source_reports.items():
                if not isinstance(detail, dict):
                    raise ValueError('Invalid upstream reader report: ' + source)
                severities = detail.get('error_severities', {})
                if not isinstance(severities, dict) or any(not _count(count) for count in severities.values()):
                    raise ValueError('Invalid upstream reader error severities: ' + source)
                if detail.get('status') != 'complete' or any(severities.values()):
                    limitations.append('Upstream reader report: ' + json.dumps(detail, sort_keys=True) + '.')
        else:
            if T11_FILES <= verified.paths.keys():
                validate_reference_artifacts(verified.paths, upstream)
            if upstream.get('rule_version') != 'airports:1.1':
                raise ValueError('Unsupported upstream geography report version')
            if upstream.get('schedule_sources') != report['upstream'].get('schedule_sources'):
                raise ValueError('T13 upstream schedule sources mismatch')
            if 'travel_observations.parquet' in verified.hashes:
                inputs = upstream.get('inputs', {})
                digests = [value for name, value in inputs.items()
                           if name.replace('\\', '/').rsplit('/', 1)[-1] == 'travel_observations.parquet']
                if not digests or any(value != verified.hashes['travel_observations.parquet'] for value in digests):
                    raise ValueError('T11/T12 travel observation fingerprint mismatch')
    return limitations


def _verify_t16(root, report, verified):
    """Check a surrounding immutable snapshot and preserve its declared scope."""
    parent = root.parent
    manifest = _json(verified.add(parent, 'run_manifest.json', prefix='t16/'))
    if manifest.get('pipeline_version') != '1.0' or manifest.get('status') not in {'complete', 'complete_with_limitations'}:
        raise ValueError('Not a completed T16 snapshot')
    files = manifest.get('files')
    if not isinstance(files, dict) or not files:
        raise ValueError('Missing T16 file inventory')
    actual = {path.relative_to(parent).as_posix() for path in parent.rglob('*') if path.is_file() or path.is_symlink()}
    if actual != set(files) | {'run_manifest.json'}:
        raise ValueError('T16 snapshot inventory mismatch')
    for name, info in files.items():
        if not isinstance(info, dict) or not _sha256(info.get('sha256')) or not _count(info.get('bytes')):
            raise ValueError('Invalid T16 artifact description: ' + name)
        path = verified.add(parent, name, info['sha256'], prefix='t16/')
        if path.stat().st_size != info['bytes']:
            raise ValueError('T16 artifact size mismatch: ' + name)
        if path.suffix == '.parquet':
            parquet = pq.ParquetFile(path)
            schema_hash = hashlib.sha256(parquet.schema_arrow.serialize().to_pybytes()).hexdigest()
            if parquet.metadata.num_rows != info.get('rows') or schema_hash != info.get('schema_sha256'):
                raise ValueError('T16 artifact schema/count mismatch: ' + name)
    for name in ('input_manifest.json', 'environment.json', 'tables/canonical_manifest.json', 'tables/t11_manifest.json'):
        if name not in files:
            raise ValueError('Missing T16 provenance artifact: ' + name)
    inventory = _json(verified.paths['t16/input_manifest.json'])
    environment = _json(verified.paths['t16/environment.json'])
    mode = manifest.get('input_mode')
    if mode not in {'raw', 'interim', 'processed'} or inventory.get('mode') != mode:
        raise ValueError('T16 input mode mismatch')
    if inventory.get('raw_files_verified') is not (mode == 'raw'):
        raise ValueError('T16 raw-source verification status mismatch')
    inputs = inventory.get('files')
    if not isinstance(inputs, dict) or not inputs or any(
            not isinstance(info, dict) or not _sha256(info.get('sha256')) or
            not _count(info.get('bytes')) or not isinstance(info.get('path'), str)
            for info in inputs.values()):
        raise ValueError('Invalid T16 source inventory')
    options = manifest.get('options')
    if not isinstance(options, dict) or any(type(options.get(key)) is not bool for key in ('require_all', 'allow_source_errors')):
        raise ValueError('Invalid T16 source acceptance options')
    request = _digest({'environment': environment, 'mode': mode, 'options': options,
                       'inputs': {key: info['sha256'] for key, info in inputs.items()}})
    if request != manifest.get('request_sha256'):
        raise ValueError('T16 request fingerprint mismatch')
    if any(manifest.get('counts', {}).get(name) != count for name, count in report['counts'].items()):
        raise ValueError('T16/T13 report count mismatch')
    for key in ('personal_sources_present', 'personal_sources_missing'):
        if manifest.get(key) != report.get(key):
            raise ValueError('T16/T13 source coverage mismatch: ' + key)
    limitations = _report_limitations(manifest, 'T16')
    sources = _strings(manifest.get('configured_sources'), 'T16 configured sources')
    if not sources or not set(sources) <= PERSONAL_SOURCES | {'timetable_pdf'}:
        raise ValueError('Invalid T16 configured source inventory')
    if options['require_all'] and (set(sources) != PERSONAL_SOURCES | {'timetable_pdf'} or report['personal_sources_missing']):
        raise ValueError('T16 require_all disagrees with source coverage')
    if mode == 'raw' and {key for key in inputs if key.startswith('raw:')} != {'raw:' + name for name in sources}:
        raise ValueError('T16 raw source inventory disagrees with configured sources')
    upstream = report['upstream']
    if upstream['canonical_normalization_errors']:
        raise ValueError('T16 snapshot contains unresolved normalization errors')
    source_errors = upstream['reader_error_severities'].get('error', 0)
    incomplete_schedules = any(source.get('coverage') != 'confirmed' for source in upstream['schedule_sources'])
    if (source_errors or incomplete_schedules) and not options['allow_source_errors']:
        raise ValueError('T16 source errors lack declared acceptance option')
    if ('boardingpass_xlsx' in sources
            or 'boardingpass_xlsx' in manifest.get('personal_sources_present', [])):
        source_name = 'sources/boardingpass_xlsx/source_report.json'
        coverage_name = 'sources/boardingpass_xlsx/workbook_coverage.csv'
        if not {source_name, coverage_name} <= files.keys():
            raise ValueError('Missing T16 XLSX coverage/source report')
        source_path = _safe_file(parent, source_name)
        source_report = _json(source_path)
        canonical = _json(_safe_file(root, 'canonical_report.json'))
        embedded = canonical.get('source_reports', {})
        canonical_inputs = canonical.get('inputs')
        matches = [name for name, value in embedded.items()
                   if isinstance(value, dict) and value.get('source_id') == 'boardingpass_xlsx'] if isinstance(embedded, dict) else []
        if (len(matches) != 1 or not isinstance(canonical_inputs, dict)
                or canonical_inputs.get(matches[0]) != verified.cache[source_path]):
            raise ValueError('T16 XLSX source report fingerprint differs from canonical input')
        if mode == 'raw' and 'raw:boardingpass_xlsx' not in inputs:
            raise ValueError('T16 XLSX source is missing from raw inventory')
        coverage = validate_xlsx_coverage(
            _safe_file(parent, coverage_name), source_report,
            canonical_report=canonical, canonical_tables=root,
            raw_input=inputs.get('raw:boardingpass_xlsx') if mode == 'raw' else None)
        limitations.append('T16 XLSX coverage verified: ' + json.dumps(coverage, sort_keys=True) + '.')
    repair_signal = ('repair' in manifest or 'repair_manifest.json' in files
                     or any(name.startswith('repair/') for name in files)
                     or '-coverage-' in str(manifest.get('run_id', '')))
    if repair_signal:
        if 'repair_manifest.json' not in files:
            raise ValueError('T16 repair evidence requires repair_manifest.json')
        # The repair verifier is independent of product admission. The complete
        # inventory has already been checked above, including this manifest.
        from .repair import verify_repair

        verify_repair(parent, verify_inventory=False)
    limitations.append('T16 source inventory: mode=' + mode + '; configured sources=' + ', '.join(sources) +
                       '; raw_files_verified=' + str(inventory['raw_files_verified']).lower() +
                       '; original source paths are recorded evidence and are not reopened by the product build.')
    limitations.append('T16 source acceptance options: ' + json.dumps(options, sort_keys=True) + '.')
    return limitations


def verify(input_dir):
    root = Path(input_dir).resolve()
    verified = _VerifiedFiles()
    for name in SUPPLIED:
        path = root / name
        if not path.is_file():
            raise FileNotFoundError(f'Missing T13 input: {path}')
        path = verified.add(root, name)
        with path.open('rb') as stream:
            if stream.read(4) != b'PAR1':
                raise ValueError(f'{path.name}: not Parquet; run git lfs pull (see docs/usage.md)')
    manifest_path = root / 'resolution_manifest.json'
    parent_manifest = root.parent / 'run_manifest.json'
    has_t16 = root.name == 'tables' and (parent_manifest.exists() or parent_manifest.is_symlink())
    if manifest_path.exists() or manifest_path.is_symlink():
        report, limitations = _verify_t13(root, verified)
        origin = 'verified_t13'
        if has_t16:
            limitations.extend(_verify_t16(root, report, verified))
    elif has_t16:
        raise ValueError('T16 snapshot lacks a completed T13 resolution_manifest.json')
    elif verified.hashes == SUPPLIED:
        origin = 'supplied_develop_t13'
        limitations = ['Original raw files and complete T13 manifests are absent from this checkout; supplied T13 bytes verified against committed LFS SHA-256.']
        if (root / 'segment_links.parquet').exists() or (root / 'segment_links.parquet').is_symlink():
            raise ValueError('segment_links.parquet lacks a matching T13 manifest checksum')
        limitations.append('Source locators unavailable: evidence links resolve to original T13 segment_id and anchor_travel_id only.')
    else:
        raise ValueError('Input must match the supplied dataset or a completed T13 resolution_manifest.json')
    return root, verified.paths, verified.hashes, origin, list(dict.fromkeys(limitations))
