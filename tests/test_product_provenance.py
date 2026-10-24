"""Publication integrity, upstream qualifications and portable source provenance."""
import hashlib
import json
import shutil

import pyarrow.parquet as pq
import pytest

from yoyoflot.product.build import run
from yoyoflot.product.input import T13_FILES, verify
from yoyoflot.provenance import fingerprint
from tests.test_source_product import ROOT, config, tables
from tests.test_pipeline import args


def load(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')


def refresh_t13(root, change=None):
    report = load(root / 'resolution_report.json')
    if change:
        change(report)
    write(root / 'resolution_report.json', report)
    manifest = load(root / 'resolution_manifest.json')
    manifest['files'] = {name: fingerprint(root / name) for name in manifest['files']}
    write(root / 'resolution_manifest.json', manifest)


def describe(path):
    result = {'sha256': fingerprint(path), 'bytes': path.stat().st_size}
    if path.suffix == '.parquet':
        parquet = pq.ParquetFile(path)
        result.update(rows=parquet.metadata.num_rows,
                      schema_sha256=hashlib.sha256(parquet.schema_arrow.serialize().to_pybytes()).hexdigest())
    return result


def refresh_t16(parent):
    manifest = load(parent / 'run_manifest.json')
    manifest['files'] = {path.relative_to(parent).as_posix(): describe(path)
                         for path in parent.rglob('*') if path.is_file() and path != parent / 'run_manifest.json'}
    inventory = load(parent / 'input_manifest.json')
    request = {'environment': load(parent / 'environment.json'), 'mode': manifest['input_mode'],
               'options': manifest['options'],
               'inputs': {name: info['sha256'] for name, info in inventory['files'].items()}}
    manifest['request_sha256'] = hashlib.sha256(json.dumps(request, ensure_ascii=False, sort_keys=True,
                                                        separators=(',', ':')).encode()).hexdigest()
    write(parent / 'run_manifest.json', manifest)


def snapshot(tmp_path, mode='raw'):
    source = tables(tmp_path)
    parent = tmp_path / 'snapshot'
    parent.mkdir()
    source.rename(parent / 'tables')
    root = parent / 'tables'
    for path in (tmp_path / 'upstream').iterdir():
        shutil.copyfile(path, root / path.name)
    raw = tmp_path / 'boarding.csv'
    raw.write_text('Authored unit-test source bytes; not production app data.\n')
    inputs = {('raw:boarding_csv' if mode == 'raw' else mode + ':source'):
              {'path': str(raw), **describe(raw)}}
    write(parent / 'input_manifest.json', {'mode': mode, 'raw_files_verified': mode == 'raw', 'files': inputs})
    write(parent / 'environment.json', {'pipeline_version': '1.0', 'fixture': 'authored unit-test snapshot'})
    report = load(root / 'resolution_report.json')
    manifest = {'pipeline_version': '1.0', 'status': 'complete_with_limitations', 'run_id': 'control-snapshot',
                'input_mode': mode, 'acceptance': 'pending_real_data_and_independent_review',
                'configured_sources': ['boarding_csv'], 'personal_sources_present': report['personal_sources_present'],
                'personal_sources_missing': report['personal_sources_missing'], 'counts': report['counts'],
                'limitations': ['Independent review remains pending.'],
                'options': {'require_all': False, 'allow_source_errors': False, 'batch_size': 4000}}
    write(parent / 'run_manifest.json', manifest)
    refresh_t16(parent)
    return root


def test_complete_t13_verifies_every_published_artifact(tmp_path):
    root = tables(tmp_path)
    _, paths, hashes, origin, limitations = verify(root)
    assert origin == 'verified_t13'
    assert set(paths) == T13_FILES | {'resolution_manifest.json'}
    assert set(paths) == set(hashes)
    assert all(fingerprint(path) == hashes[name] for name, path in paths.items())
    assert any('not a digital signature' in item for item in limitations)
    assert any('not included in this T13 transfer' in item for item in limitations)


@pytest.mark.parametrize('name', ['resolution_report.json', 'resolution_review_sample.csv',
                                  'resolution_summary.txt', 'person_pairs.parquet', 'segment_links.parquet'])
def test_any_published_artifact_corruption_is_rejected(tmp_path, name):
    root = tables(tmp_path)
    with (root / name).open('ab') as stream:
        stream.write(b'corruption')
    with pytest.raises(ValueError, match='checksum'):
        verify(root)


@pytest.mark.parametrize('remove_from_manifest', [False, True])
def test_missing_report_cannot_claim_verified_t13(tmp_path, remove_from_manifest):
    root = tables(tmp_path)
    (root / 'resolution_report.json').unlink()
    if remove_from_manifest:
        manifest = load(root / 'resolution_manifest.json')
        del manifest['files']['resolution_report.json']
        write(root / 'resolution_manifest.json', manifest)
    with pytest.raises(ValueError, match='Missing published|file set'):
        verify(root)


@pytest.mark.parametrize(('field', 'value', 'message'), [
    ('status', 'failed', 'completed resolution'),
    ('version', 'resolution:9.0', 'version mismatch'),
    ('run_fingerprint', 'run_' + '0' * 64, 'run_fingerprint'),
    ('upstream', {}, 'canonical status'),
])
def test_report_contract_is_checked_even_with_updated_checksum(tmp_path, field, value, message):
    root = tables(tmp_path)
    refresh_t13(root, lambda report: report.update({field: value}))
    with pytest.raises(ValueError, match=message):
        verify(root)


def test_report_counts_and_schema_are_checked(tmp_path):
    root = tables(tmp_path)
    refresh_t13(root, lambda report: report['counts'].update(persons=99))
    with pytest.raises(ValueError, match='row count mismatch'):
        verify(root)
    refresh_t13(root, lambda report: report['counts'].update(persons=3))
    table = pq.read_table(root / 'person_links.parquet').replace_schema_metadata({b'resolution_contract_version': b'9.0'})
    pq.write_table(table, root / 'person_links.parquet')
    refresh_t13(root)
    with pytest.raises(ValueError, match='schema/version'):
        verify(root)


@pytest.mark.parametrize('bad_name', ['../outside.json', '/tmp/outside.json', 'folder/../outside.json', 'folder\\outside.json'])
def test_unsafe_manifest_path_is_rejected(tmp_path, bad_name):
    root = tables(tmp_path)
    manifest = load(root / 'resolution_manifest.json')
    manifest['files'][bad_name] = '0' * 64
    write(root / 'resolution_manifest.json', manifest)
    with pytest.raises(ValueError, match='file set|Unsafe'):
        verify(root)


def test_manifest_symlink_and_null_checksum_are_rejected(tmp_path):
    root = tables(tmp_path)
    original = root / 'resolution_report.json'
    outside = tmp_path / original.name
    original.rename(outside)
    original.symlink_to(outside)
    with pytest.raises(ValueError, match='Symlink'):
        verify(root)
    original.unlink()
    outside.rename(original)
    manifest = load(root / 'resolution_manifest.json')
    manifest['files']['resolution_report.json'] = None
    write(root / 'resolution_manifest.json', manifest)
    with pytest.raises(ValueError, match='Invalid published checksum'):
        verify(root)


def test_upstream_errors_and_limitations_survive_product_publication(tmp_path):
    root = tables(tmp_path)

    def qualify(report):
        report['limitations'].append('Coverage is partial and requires review.')
        report['upstream'].update(canonical_status='complete_with_errors', canonical_normalization_errors=2,
                                  reader_error_severities={'error': 3, 'warning': 4},
                                  schedule_sources=[{'path': 'original/schedules.parquet', 'coverage': 'unknown'}])

    refresh_t13(root, qualify)
    result = run(root, tmp_path / 'product', config(tmp_path), ROOT / 'data/reference/airports.csv')
    limitations = '\n'.join(result['limitations'])
    for value in ('Coverage is partial', 'normalization errors: 2', '"error": 3',
                  '"warning": 4', 'schedule coverage unconfirmed', 'Unobserved personal source: forum_json'):
        assert value in limitations
    assert fingerprint(tmp_path / 'product/provenance/resolution_report.json') == fingerprint(root / 'resolution_report.json')


def test_available_upstream_artifact_is_bound_to_t13_report(tmp_path):
    root = tables(tmp_path)
    source = tmp_path / 'upstream/canonical_report.json'
    shutil.copyfile(source, root / source.name)
    _, _, _, _, limitations = verify(root)
    assert any('Authored canonical control' in item for item in limitations)
    report = load(root / source.name)
    report['normalization_errors'] = 9
    write(root / source.name, report)
    with pytest.raises(ValueError, match='checksum'):
        verify(root)


@pytest.mark.parametrize('mode', ['raw', 'interim', 'processed'])
def test_t16_snapshot_retains_source_mode_without_requiring_original_raw(tmp_path, mode):
    root = snapshot(tmp_path, mode)
    (tmp_path / 'boarding.csv').unlink()
    shutil.rmtree(tmp_path / 'upstream')
    _, paths, _, origin, limitations = verify(root)
    assert origin == 'verified_t13'
    assert paths['t16/run_manifest.json'] == root.parent / 'run_manifest.json'
    assert paths['t16/input_manifest.json'] == root.parent / 'input_manifest.json'
    assert len(paths.values()) == len(set(paths.values()))
    assert any('mode=' + mode in item for item in limitations)
    assert any('raw_files_verified=' + str(mode == 'raw').lower() in item for item in limitations)
    assert any('Independent review remains pending.' in item for item in limitations)


def test_t16_parent_inventory_and_request_cannot_be_ignored(tmp_path):
    root = snapshot(tmp_path)
    parent = root.parent
    with (parent / 'input_manifest.json').open('ab') as stream:
        stream.write(b'\n')
    with pytest.raises(ValueError, match='checksum'):
        verify(root)
    refresh_t16(parent)
    manifest = load(parent / 'run_manifest.json')
    manifest['options']['require_all'] = True
    write(parent / 'run_manifest.json', manifest)
    with pytest.raises(ValueError, match='request fingerprint'):
        verify(root)
    refresh_t16(parent)
    with pytest.raises(ValueError, match='require_all'):
        verify(root)


def test_t16_cannot_upgrade_processed_input_to_raw_verification(tmp_path):
    root = snapshot(tmp_path, 'processed')
    path = root.parent / 'input_manifest.json'
    inventory = load(path)
    inventory['raw_files_verified'] = True
    write(path, inventory)
    refresh_t16(root.parent)
    with pytest.raises(ValueError, match='raw-source verification'):
        verify(root)


def test_t16_requires_its_published_upstream_manifests(tmp_path):
    root = snapshot(tmp_path)
    (root / 't11_manifest.json').unlink()
    refresh_t16(root.parent)
    with pytest.raises(ValueError, match='Missing T16 provenance artifact'):
        verify(root)


def test_raw_reingestion_is_separate_from_pending_human_review(args):
    from yoyoflot.pipeline import run as build_core
    from yoyoflot.product.build import _source_provenance

    core, _ = build_core(**args)
    assert core['status'] == 'complete_with_limitations'
    assert 'independent_review_pending' in core['limitations']
    _, paths, _, origin, _ = verify(args['output'] / 'tables')
    provenance = _source_provenance(paths, origin)
    assert provenance['raw_reingestion_verified'] is True
    assert provenance['independent_human_review'] == 'pending'
