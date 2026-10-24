"""Strict published-core reuse; authored controls never enter app data."""
import json
from pathlib import Path
import shutil

import pytest

from yoyoflot import pipeline
from yoyoflot.cli import main, parser
from yoyoflot.product.reuse import verify_raw_core
from yoyoflot.product.store import ProductStore
from tests.test_pipeline import args, no_transform


def reuse_options(args):
    return {key: args[key] for key in ('data_dir', 'config', 'normalization',
                                      'resolution', 'reference', 'overrides')}


@pytest.mark.parametrize('extra', [[], ['--until', 'product'],
                                  ['--until', 'product', '--require-all', '--allow-source-errors']])
def test_reuse_requires_strict_product_flags(tmp_path, monkeypatch, extra):
    monkeypatch.setattr('yoyoflot.product.reuse.verify_raw_core', no_transform)
    assert main(['run', '--core-snapshot', str(tmp_path / 'core'), *extra, '-q']) == 1


@pytest.mark.parametrize('other', ['--from-interim', '--from-processed'])
def test_reuse_is_exclusive_with_adopted_inputs(tmp_path, other):
    with pytest.raises(SystemExit) as error:
        parser().parse_args(['run', '--core-snapshot', str(tmp_path), other, str(tmp_path)])
    assert error.value.code == 2


def test_reuse_rejects_plain_processed_directory(tmp_path):
    with pytest.raises(ValueError, match='published T16'):
        verify_raw_core(tmp_path, data_dir=tmp_path, config='unused', normalization='unused',
                        resolution='unused', reference='unused', overrides='unused')


def test_published_raw_core_reuse_revalidates_sources_and_artifacts(args, tmp_path, monkeypatch):
    core, _ = pipeline.run(**args)
    snapshot = tmp_path / 'relocated-core'
    args['output'].rename(snapshot)
    # Live sources may move; historical manifest paths must never be reopened.
    live_raw = tmp_path / 'relocated-raw'
    shutil.copytree(args['data_dir'], live_raw)
    sources = json.loads((snapshot / 'input_manifest.json').read_text())['files']
    for label, info in sources.items():
        if label.startswith('raw:'):
            Path(info['path']).unlink()
    args['data_dir'] = live_raw
    options = reuse_options(args)
    tables, verified_core = verify_raw_core(snapshot, **options)
    assert tables == snapshot / 'tables' and verified_core == core
    original_manifest = (snapshot / 'run_manifest.json').read_bytes()
    monkeypatch.setattr(pipeline, 'run', no_transform)
    artifacts = tmp_path / 'products'
    flags = ['run', '--core-snapshot', str(snapshot), '--until', 'product', '--require-all',
             '--config', str(args['config']), '--data-dir', str(live_raw),
             '--artifacts-dir', str(artifacts), '--normalization-config', str(args['normalization']),
             '--resolution-config', str(args['resolution']), '--reference', str(args['reference']),
             '--overrides', str(args['overrides']), '--memory-limit', '128MB', '-q']
    assert main(flags) == 0
    output = artifacts / ('product-' + core['run_id'])
    store = ProductStore(output)
    assert store.manifest['counts']['persons'] == 6
    assert store.manifest['counts']['segments'] == 9
    assert store.manifest['request']['origin'] == 'verified_t13'
    assert any('mode=raw' in text for text in store.manifest['limitations'])
    assert main(flags) == 0  # Immutable matching product is verified and reused.
    assert (snapshot / 'run_manifest.json').read_bytes() == original_manifest

    raw = live_raw / Path(sources['raw:boarding_csv']['path']).name
    original = raw.read_bytes()
    raw.write_bytes(original + b'changed')
    with pytest.raises(ValueError, match='raw:boarding_csv'):
        verify_raw_core(snapshot, **options)
    assert main(flags) == 1  # Even an existing model cannot bypass live raw checks.
    raw.write_bytes(original)

    original = args['normalization'].read_bytes()
    args['normalization'].write_bytes(original + b'\n# changed\n')
    with pytest.raises(ValueError, match='config:normalization.yaml'):
        verify_raw_core(snapshot, **options)
    args['normalization'].write_bytes(original)

    # Full verification also covers auxiliary reports, not just persons/segments.
    auxiliary = snapshot / 'tables/resolution_review_sample.csv'
    original = auxiliary.read_bytes()
    auxiliary.write_bytes(original + b'broken')
    with pytest.raises(ValueError, match='checksum'):
        verify_raw_core(snapshot, **options)
    auxiliary.write_bytes(original)


def test_reuse_rejects_nonraw_or_relaxed_manifest(tmp_path, monkeypatch):
    """Exercise policy gates after a successful strict artifact verifier."""
    from yoyoflot.product import reuse
    from yoyoflot.provenance import fingerprint

    snapshot = tmp_path / 'core'
    tables = snapshot / 'tables'
    tables.mkdir(parents=True)
    manifest_path = snapshot / 'run_manifest.json'
    inventory_path = snapshot / 'input_manifest.json'
    paths = {'t16/run_manifest.json': manifest_path, 't16/input_manifest.json': inventory_path}
    manifest = {'input_mode': 'raw', 'configured_sources': list(pipeline.SOURCES),
                'options': {'require_all': True, 'allow_source_errors': False}, 'run_id': 'control'}
    inventory = {'mode': 'raw', 'raw_files_verified': True}
    for change in ('processed', 'interim', 'partial', 'errors'):
        candidate = {**manifest, 'options': dict(manifest['options'])}
        if change in {'processed', 'interim'}:
            candidate['input_mode'] = change
        elif change == 'partial':
            candidate['options']['require_all'] = False
        else:
            candidate['options']['allow_source_errors'] = True
        manifest_path.write_text(json.dumps(candidate))
        inventory_path.write_text(json.dumps(inventory))
        hashes = {name: fingerprint(path) for name, path in paths.items()}
        monkeypatch.setattr(reuse, 'verify', lambda _: (tables, paths, hashes, 'verified_t13', []))
        with pytest.raises(ValueError, match='raw mode, all seven sources'):
            verify_raw_core(snapshot, data_dir=tmp_path, config='unused', normalization='unused',
                            resolution='unused', reference='unused', overrides='unused')
