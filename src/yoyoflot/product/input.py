"""Verify supplied T13 tables, including the exact Git LFS dataset shipped by develop."""
import json
from pathlib import Path

from ..provenance import fingerprint

# These are the original LFS object IDs committed in develop, not new data.
SUPPLIED = {
    'persons.parquet': 'ec5948ba0a6941164c94bd8d8ee05c5a07125b3964aba1a8e91419ee12252a53',
    'segments.parquet': 'f7c2f83f1a99d5fc555f56eb622ed43da74f5d8b0b963dd3513eedfadfe40354',
}


def verify(input_dir):
    root = Path(input_dir).resolve()
    paths = {n: root / n for n in SUPPLIED}
    for path in paths.values():
        if not path.is_file():
            raise FileNotFoundError(f'Missing T13 input: {path}')
        with path.open('rb') as stream:
            if stream.read(4) != b'PAR1':
                raise ValueError(f'{path.name}: not Parquet; run git lfs pull (see docs/usage.md)')
    hashes = {n: fingerprint(p) for n, p in paths.items()}
    limitations = []
    if hashes == SUPPLIED:
        origin = 'supplied_develop_t13'
        limitations = ['Original raw files and complete T13 manifests are absent from this checkout; supplied T13 bytes verified against committed LFS SHA-256.']
    else:
        manifest_path = root / 'resolution_manifest.json'
        if not manifest_path.is_file():
            raise ValueError('Input must match the supplied dataset or a completed T13 resolution_manifest.json')
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('version') != 'resolution:1.0' or any(manifest.get('files', {}).get(n) != h for n, h in hashes.items()):
            raise ValueError('T13 manifest checksum/version mismatch')
        paths['resolution_manifest.json'] = manifest_path
        hashes['resolution_manifest.json'] = fingerprint(manifest_path)
        origin = 'verified_t13'
    links = root / 'segment_links.parquet'
    if links.is_file():
        manifest_file = root / 'resolution_manifest.json'
        manifest = json.loads(manifest_file.read_text(encoding='utf-8')) if manifest_file.is_file() else {}
        h = fingerprint(links)
        if manifest.get('files', {}).get(links.name) != h:
            raise ValueError('segment_links.parquet lacks a matching T13 manifest checksum')
        paths[links.name], hashes[links.name] = links, h
    else:
        limitations.append('Source locators unavailable: evidence links resolve to original T13 segment_id and anchor_travel_id only.')
    return root, paths, hashes, origin, limitations
