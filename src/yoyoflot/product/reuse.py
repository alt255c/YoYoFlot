"""Reuse an immutable raw T16 core only after artifact and live-input checks."""
from __future__ import annotations

import json
from pathlib import Path

from ..config import load_sources, resolve_path
from ..normalize.airports import AirportReference
from ..pipeline import SOURCES, _columns
from ..provenance import fingerprint
from .input import verify


def verify_raw_core(snapshot, *, data_dir, config, normalization, resolution,
                    reference, overrides, reference_manifest=None):
    """Return verified tables and manifest without repeating core calculations.

    Historical absolute paths are evidence, not file locations to reopen. All
    seven live raw sources and the requested ETL configuration/reference must
    match the published input hashes, even if the snapshot has been relocated.
    Product-specific configuration is subsequently bound by the product build.
    """
    snapshot = Path(snapshot).resolve()
    if not (snapshot / 'run_manifest.json').is_file() or not (snapshot / 'tables').is_dir():
        raise ValueError('--core-snapshot requires a published T16 snapshot directory')
    root, paths, hashes, origin, _ = verify(snapshot / 'tables')
    required = {'t16/run_manifest.json', 't16/input_manifest.json'}
    if origin != 'verified_t13' or not required <= paths.keys():
        raise ValueError('--core-snapshot requires fully verified T16 provenance')
    manifest = json.loads(paths['t16/run_manifest.json'].read_text(encoding='utf-8'))
    inventory = json.loads(paths['t16/input_manifest.json'].read_text(encoding='utf-8'))
    for name in required:
        if fingerprint(paths[name]) != hashes[name]:
            raise RuntimeError('T16 provenance changed during reuse verification')
    options = manifest.get('options', {})
    if (manifest.get('input_mode') != 'raw' or inventory.get('mode') != 'raw'
            or inventory.get('raw_files_verified') is not True
            or options.get('require_all') is not True
            or options.get('allow_source_errors') is not False
            or set(manifest.get('configured_sources', [])) != set(SOURCES)):
        raise ValueError('--core-snapshot requires raw mode, all seven sources, require_all=true and allow_source_errors=false')
    run_id = manifest.get('run_id')
    if not isinstance(run_id, str) or run_id in {'', '.', '..'} or Path(run_id).name != run_id or '\\' in run_id:
        raise ValueError('Invalid T16 run_id for product output')

    config, normalization, resolution, reference, overrides = (
        Path(path).resolve() for path in (config, normalization, resolution, reference, overrides))
    ref_manifest = (Path(reference_manifest).resolve() if reference_manifest
                    else reference.parent / 'reference_manifest.json')
    # The product builder consumes the manifest beside airports.csv. A custom
    # core-manifest path must describe the exact same reference publication.
    if reference_manifest and fingerprint(ref_manifest) != fingerprint(reference.parent / 'reference_manifest.json'):
        raise ValueError('The selected reference manifest differs from the product reference manifest')
    sources = load_sources(config)
    if set(sources) != set(SOURCES):
        raise ValueError('--core-snapshot requires all seven configured sources')
    expected = {'config:sources.yaml': config, 'config:normalization.yaml': normalization,
                'config:resolution.yaml': resolution, 'config:airports_overrides.yaml': overrides,
                'reference:airports.csv': reference, 'reference:reference_manifest.json': ref_manifest}
    data_dir = Path(data_dir).resolve()
    for name in SOURCES:
        source = sources[name]
        if not isinstance(source, dict) or source.get('reader', name) != name:
            raise ValueError('Invalid source configuration: ' + name)
        raw = resolve_path(source, data_dir).resolve()
        if not raw.is_relative_to(data_dir):
            raise ValueError('Raw source must be inside --data-dir: ' + name)
        expected['raw:' + name] = raw
    expected['config:sirena_columns.yaml'] = _columns(
        sources['sirena_fixed'].get('columns', 'configs/sirena_columns.yaml'), config)
    AirportReference(reference, overrides, ref_manifest)
    reference_files = json.loads(ref_manifest.read_text(encoding='utf-8')).get('files', {})
    for name, digest in reference_files.items():
        candidate = reference.parent / name
        if (Path(name).name != name or '\\' in name or not candidate.is_file()
                or fingerprint(candidate) != digest):
            raise ValueError('Reference manifest file mismatch: ' + name)
        expected['reference:' + name] = candidate
    recorded = inventory['files']
    if set(recorded) != set(expected):
        raise ValueError('T16 input inventory differs from the requested seven-source raw configuration')
    for label, path in sorted(expected.items()):
        info = recorded[label]
        if not path.is_file() or path.stat().st_size != info['bytes'] or fingerprint(path) != info['sha256']:
            raise ValueError('T16 input does not match the current file: ' + label)
    return root, manifest
