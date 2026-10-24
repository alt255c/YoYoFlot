"""Publish one self-contained scoring snapshot for the site and every export."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .build import VERSION, _digest
from .scoring import RuntimeScores, load
from .store import ProductStore


def run(product, output, scoring):
    source, target = Path(product).resolve(), Path(output).resolve()
    if target == source or target in source.parents or source in target.parents:
        raise ValueError('Final product must be separate from its inputs')
    config = load(scoring)
    store = ProductStore(source)
    if not store.assessment_root or store.assessment_manifest['version'] != 'source-assessment:4.0':
        raise ValueError('Final scoring requires the complete source-assessment:4.0 links')
    if store.runtime_scores is not None:
        raise ValueError('Compile from the baseline product, not an already scored product')
    base_hash = fingerprint(source / 'run_manifest.json')
    assessment_hash = fingerprint(store.assessment_root / 'assessment_manifest.json')
    request = dict(version='compiled-product:2.0', base_manifest_sha256=base_hash,
                   assessment_manifest_sha256=assessment_hash, scoring_sha256=config.sha256,
                   compiler_sha256=fingerprint(Path(__file__)),
                   scoring_code_sha256=fingerprint(Path(__file__).with_name('scoring.py')))
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ('.' + target.name + '.lock'), 'Final product'):
        if target.exists():
            existing = ProductStore(target, scoring=config)
            if existing.manifest['request'] != request:
                raise ValueError('Final output belongs to another build; choose a new directory')
            return existing.manifest
        with tempfile.TemporaryDirectory(prefix='.product-', dir=target.parent) as temp:
            stage = Path(temp) / 'product'
            stage.mkdir()
            for name in store.manifest['files']:
                destination = stage / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, destination)
            shutil.copytree(store.assessment_root, stage / 'assessment')
            if store.profile_observations is None:
                raise ValueError('Canonical observations are required for a self-contained product')
            shutil.copyfile(store.profile_observations, stage / 'source_observations.parquet')
            shutil.copyfile(source / 'run_manifest.json', stage / 'base_manifest.json')
            shutil.copyfile(source / 'tables/rankings.parquet', stage / 'baseline_rankings.parquet')
            shutil.copyfile(source / 'tables/explanations.parquet', stage / 'baseline_explanations.parquet')
            shutil.copyfile(config.path, stage / 'scoring.yaml')
            with store.connection() as db:
                scores = RuntimeScores(db, config, base_hash, assessment_hash)
                try:
                    shutil.copyfile(scores.root / 'rankings.parquet', stage / 'tables/rankings.parquet')
                    shutil.copyfile(scores.root / 'factors.parquet', stage / 'tables/explanations.parquet')
                    shutil.copyfile(scores.root / 'scoring.json', stage / 'scoring.json')
                    scores.attach(db)
                    (stage / 'tables/top50.parquet').unlink()
                    db.execute('COPY (SELECT * FROM rankings WHERE rank<=50 ORDER BY rank) TO ? '
                               '(FORMAT PARQUET, COMPRESSION ZSTD)', [str(stage / 'tables/top50.parquet')])
                    eligibility = [dict(eligibility_status=status, profiles=scores.statistics[key])
                        for status,key in [('eligible','with_signals'),('no_matches','without_signals'),
                                           ('insufficient_data','unavailable')]]
                    manifest = dict(store.manifest, schema_version=VERSION,
                        run_id='product-' + _digest(request)[:20], request=request,
                        base_product_manifest_sha256=base_hash, product_kind='published_scoring',
                        sort_order=scores.info['sort_order'], eligibility=eligibility,
                        contribution_formula='Weighted factors; maximum total without direct shared-evidence overlaps',
                        counts=dict(store.manifest['counts'], top50=min(50,scores.statistics['with_signals'])))
                finally:
                    scores.close()
            # Historical baseline experiments must not be presented as validation
            # of the final ranking, which includes additional source factors.
            shutil.move(stage / 'evaluation.json', stage / 'baseline_evaluation.json')
            evaluation = dict(status='computed_without_labels', quality_metrics=None,
                              scope='published_scoring', reason='No supplied ground-truth labels')
            (stage / 'evaluation.json').write_text(json.dumps(evaluation, indent=2) + '\n', encoding='utf-8')
            # Read and verify the same inputs again before publishing.
            ProductStore(source)
            if fingerprint(source / 'run_manifest.json') != base_hash:
                raise ValueError('Base product changed during compilation')
            if fingerprint(store.assessment_root / 'assessment_manifest.json') != assessment_hash:
                raise ValueError('Assessment changed during compilation')
            if fingerprint(Path(config.path)) != config.sha256:
                raise ValueError('Scoring configuration changed during compilation')
            manifest['files'] = {p.relative_to(stage).as_posix():fingerprint(p)
                                 for p in sorted(stage.rglob('*')) if p.is_file()}
            (stage / 'run_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            ProductStore(stage, scoring=config)
            os.rename(stage, target)
    return manifest
