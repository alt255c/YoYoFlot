"""Read-only integrity and HTTP audit of the real local assessment artifacts."""
import argparse
import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import urlopen

from yoyoflot.product.store import ProductStore
from yoyoflot.product.assessment import verify
from yoyoflot.product.web import StoreManager, handler
from yoyoflot.provenance import fingerprint

ROOT = Path(__file__).resolve().parents[1]


def audit(product):
    store = ProductStore(product)
    if not store.assessment_root:
        raise ValueError('A verified assessment supplement is required')
    checks = dict(store.assessment_report['checks'])
    with store.connection() as db:
        checks['baseline_rows_changed'] = db.execute('''SELECT count(*) FROM (
            (SELECT person_id,criterion_id,status,value,threshold,weight,contribution
             FROM assessment_marker_coverage WHERE included_in_ranking
             EXCEPT SELECT person_id,criterion_id,status,value,threshold,weight,contribution FROM explanations)
            UNION ALL
            (SELECT person_id,criterion_id,status,value,threshold,weight,contribution FROM explanations
             EXCEPT SELECT person_id,criterion_id,status,value,threshold,weight,contribution
             FROM assessment_marker_coverage WHERE included_in_ranking))''').fetchone()[0]
        checks['identity_status_changed'] = db.execute('''SELECT count(*) FROM rankings r
            JOIN assessment_profile_assessments a USING(person_id)
            WHERE a.identity_accepted IS DISTINCT FROM (r.identity_status='accepted')''').fetchone()[0]
        checks['unknown_source_link_profile'] = db.execute('''SELECT count(*) FROM assessment_source_links l
            ANTI JOIN persons p USING(person_id)''').fetchone()[0]
        checks['multiply_assigned_observation'] = db.execute('''SELECT count(*) FROM (
            SELECT observation_id FROM assessment_source_links GROUP BY observation_id
            HAVING count(DISTINCT person_id)>1)''').fetchone()[0]
        checks['source_factor_changed_ranking'] = db.execute('''SELECT count(*) FROM assessment_marker_coverage
            WHERE NOT included_in_ranking AND contribution<>0''').fetchone()[0]
        checks['coverage_rows_missing_or_extra'] = abs(db.execute(
            'SELECT count(*) FROM assessment_marker_coverage').fetchone()[0] - 6 * store.manifest['counts']['persons'])
        ambiguous = db.execute('''SELECT a.person_id FROM assessment_profile_assessments a
            JOIN persons p USING(person_id) WHERE NOT a.identity_accepted AND a.evaluated_criteria_count>0
            ORDER BY a.person_id LIMIT 1''').fetchone()[0]
        conflict_ids = [r[0] for r in db.execute('''SELECT DISTINCT person_id FROM assessment_conflict_members
            WHERE person_id IS NOT NULL ORDER BY person_id''').fetchall()]
        conflicts = []
        for person_id in conflict_ids:
            data = store.source_details(person_id)
            if not data['documents'] or data.get('fallback_used'):
                raise AssertionError('Conflict passport is not connected by an exact link')
            conflicts.append({'person_id': person_id, 'documents_found': True, 'fuzzy_fallback_used': False})
    statistics = store.assessment_statistics()
    if any(checks.values()):
        raise AssertionError(checks)
    try:
        verify(store.assessment_root, '0' * 64)
    except ValueError:
        pass
    else:
        raise AssertionError('An assessment was accepted for another product hash')
    http = {}
    with ThreadingHTTPServer(('127.0.0.1', 0), handler(StoreManager(store))) as server:
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            for status in ('all', 'assessed', 'not_assessed', 'identity_accepted', 'eligible', 'no_matches'):
                with urlopen(base + '/api/users?limit=1&eligibility=' + status) as response:
                    http[status] = json.load(response)['pagination']['total']
            with urlopen(base + '/api/profile?id=' + ambiguous) as response:
                profile = json.load(response)
                assert profile['person']['identity_status'] == 'ambiguous'
                assert profile['person']['evaluated_criteria_count'] > 0
                assert len(profile['additional_factors']) == 3
            with urlopen(base + '/api/metadata') as response:
                assert json.load(response)['assessment'] == statistics
            for status, count_key in [('all', 'profiles'), ('assessed', 'assessed'),
                    ('not_assessed', 'unavailable'), ('identity_accepted', 'identity_accepted'),
                    ('eligible', 'with_signals'), ('no_matches', 'without_signals')]:
                assert http[status] == statistics[count_key]
        finally:
            server.shutdown()
            worker.join(timeout=3)
    return {'product_run_id': store.manifest['run_id'],
            'assessment_run_id': store.assessment_manifest['run_id'],
            'assessment_manifest_sha256': fingerprint(store.assessment_root / 'assessment_manifest.json'),
            'checks': checks, 'statistics': statistics, 'http_filter_totals': http,
            'ambiguous_profile_assessed_without_identity_upgrade': True,
            'different_product_hash_rejected': True,
            'conflict_profile_document_checks': conflicts,
            'source_data': 'Existing repository product and canonical data; no generated passenger records.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product', type=Path, default=ROOT / 'artifacts/source-product')
    parser.add_argument('--output', type=Path, default=ROOT / 'docs/validation/t32_source_assessment.json')
    args = parser.parse_args()
    report = audit(args.product)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report['statistics'], ensure_ascii=False))
