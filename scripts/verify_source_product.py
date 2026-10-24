"""Audit the supplied-data product without generating data or loading pickle."""
import argparse
import json
import math
import os
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yoyoflot.product.store import ProductStore


def _configure_audit(db, memory_limit):
    # ProductStore defaults are for interactive profile queries, not joins and
    # aggregations over every profile and every factor in the dataset.
    db.execute('SET memory_limit=?', [memory_limit])
    db.execute('SET threads=1')
    db.execute('SET preserve_insertion_order=false')


def check(product, memory_limit=None):
    memory_limit = memory_limit or os.environ.get('YOYO_VERIFY_MEMORY_LIMIT', '8GB')
    store = ProductStore(product)
    top = store.top()
    published = store.runtime_scores is not None
    criterion_count = len(store.runtime_scores.prepared) if published else 3
    ordering = 'rule_score DESC,person_id' if published else 'matched_criteria_count DESC,rule_score DESC,person_id'
    expected = min(50, next((r['profiles'] for r in store.manifest['eligibility'] if r['eligibility_status'] == 'eligible'), 0))
    if len(top) != expected:
        raise ValueError('Top size differs from eligible count')
    if [r['rank'] for r in top] != list(range(1, len(top) + 1)):
        raise ValueError('Noncontiguous ranks')
    keys = [((-r['rule_score'], r['person_id']) if published else (-r['matched_criteria_count'], -r['rule_score'], r['person_id'])) for r in top]
    if keys != sorted(keys):
        raise ValueError('Wrong ranking order')
    segments = evidence_count = 0
    for person in top:
        dossier = store.profile(person['person_id'])
        rows = dossier['segments']
        ids = {r['segment_id'] for r in rows}
        if len(ids) != len(rows) or any(r['person_id'] != person['person_id'] for r in rows):
            raise ValueError('Duplicate or foreign route')
        factors = dossier['factors'] + dossier.get('additional_factors', [])
        if sum(f['status'] == 'matched' and f.get('included_in_ranking',True) for f in factors) != person['matched_criteria_count']:
            raise ValueError('Wrong factor count')
        if not math.isclose(sum(f['contribution'] for f in factors), person['rule_score'], abs_tol=1e-12):
            raise ValueError('Wrong sum of contributions')
        dated = [r for r in rows if r['departure_date']]
        date_counts = Counter(r['departure_date'] for r in dated)
        pairs = [(a,b) for a,b in zip(dated, dated[1:]) if date_counts[a['departure_date']] == date_counts[b['departure_date']] == 1
                 and all(r['identity_status'] == 'accepted' and r['resolution_status'] == 'resolved'
                         and r['origin_airport_id'] and r['destination_airport_id'] for r in (a,b))]
        from datetime import date
        returns = [(a,b) for a,b in pairs if a['origin_airport_id'] == b['destination_airport_id']
                   and a['destination_airport_id'] == b['origin_airport_id']
                   and 1 <= (date.fromisoformat(b['departure_date']) - date.fromisoformat(a['departure_date'])).days <= 2]
        routes = [r for r in dated if r['identity_status'] == 'accepted' and r['resolution_status'] == 'resolved'
                  and r['origin_airport_id'] and r['destination_airport_id']]
        values = {'M01': len(dated) * 30 / store.manifest['window']['days'],
                  'M02': len(returns) / len(pairs) if pairs else None,
                  'M03': 1 - len({(r['origin_airport_id'],r['destination_airport_id']) for r in routes}) / len(routes) if routes else None}
        for factor in factors:
            if not set(factor['evidence_segment_ids']) <= ids:
                raise ValueError('Foreign factor evidence')
            if factor['criterion_id'] not in values:
                if factor['status']=='matched' and store.assessment_root:
                    with store.connection() as evidence_db:
                        n = evidence_db.execute('SELECT count(*) FROM assessment_marker_evidence WHERE person_id=? AND criterion_id=? AND is_matched', [person['person_id'],factor['criterion_id']]).fetchone()[0]
                        if not n: raise ValueError('Matched factor without source evidence')
                continue
            if factor['status'] != 'unavailable':
                if values[factor['criterion_id']] is None or not math.isclose(factor['value'], values[factor['criterion_id']], abs_tol=1e-12):
                    raise ValueError('Feature differs from independently recomputed value')
                matched = factor['value'] >= factor['threshold']
                contribution = factor['weight'] * min(3, factor['value'] / factor['threshold']) if matched else 0
                if matched != (factor['status'] == 'matched') or not math.isclose(contribution, factor['contribution'], abs_tol=1e-12):
                    raise ValueError('Wrong threshold/contribution')
            evidence_count += len(factor['evidence_segment_ids'])
        balance = dossier['route_balance']
        if balance['total'] != len(rows) or balance['plottable'] + balance['unplottable'] != len(rows):
            raise ValueError('Route imbalance')
        segments += len(rows)
    with store.connection() as db:
        _configure_audit(db, memory_limit)
        invalid = db.execute('''SELECT count(*) FROM explanations
            WHERE status IS NULL OR status NOT IN ('matched','not_matched','unavailable')
               OR contribution IS NULL OR NOT isfinite(contribution) OR contribution<0
               OR threshold IS NULL OR NOT isfinite(threshold) OR threshold<=0
               OR weight IS NULL OR NOT isfinite(weight) OR weight<0
               OR (status<>'unavailable' AND (value IS NULL OR NOT isfinite(value)))''').fetchone()[0]
        if invalid:
            raise ValueError('Invalid population factor state or numeric value')
        invalid = db.execute(f'''WITH actual AS (SELECT person_id,sum(contribution) score,
            count(*) factor_count, count(DISTINCT criterion_id) distinct_factor_count,
            count(*) FILTER(WHERE status<>'unavailable') available_count,
            count(*) FILTER(WHERE status='matched' AND weight>0) AS matched_count FROM explanations GROUP BY person_id)
            SELECT count(*) FROM rankings r FULL OUTER JOIN actual a USING(person_id)
            WHERE r.person_id IS NULL OR a.person_id IS NULL OR a.factor_count<>{criterion_count}
               OR a.distinct_factor_count<>{criterion_count} OR r.rule_score IS NULL OR NOT isfinite(r.rule_score)
               OR abs(r.rule_score-a.score)>1e-10 OR r.matched_criteria_count IS DISTINCT FROM a.matched_count
               OR r.available_criteria_count IS DISTINCT FROM a.available_count
               OR r.eligibility_status IS DISTINCT FROM CASE WHEN a.available_count=0 THEN 'insufficient_data'
                    WHEN a.matched_count=0 THEN 'no_matches' ELSE 'eligible' END''').fetchone()[0]
        if invalid:
            raise ValueError('Population score/explanation mismatch')
        expected_ids = [row[0] for row in db.execute(f'''SELECT person_id FROM rankings
            WHERE eligibility_status='eligible'
            ORDER BY {ordering} LIMIT 50''').fetchall()]
        if expected_ids != [person['person_id'] for person in top]:
            raise ValueError('Top excludes a higher-ranked eligible profile')
        invalid = db.execute(f'''WITH ordered AS (SELECT person_id,
            row_number() OVER(ORDER BY {ordering}) expected_rank
            FROM rankings WHERE eligibility_status='eligible')
            SELECT count(*) FROM rankings r LEFT JOIN ordered o USING(person_id)
            WHERE r.rank IS DISTINCT FROM o.expected_rank''').fetchone()[0]
        if invalid:
            raise ValueError('Population ranks are not consistent with the declared ordering')
        if published:
            duplicate_points = db.execute("""SELECT count(*) FROM assessment_marker_dependencies a
                JOIN assessment_marker_dependencies b USING(person_id,dependency_key)
                JOIN explanations x ON x.person_id=a.person_id AND x.criterion_id=a.criterion_id
                JOIN explanations y ON y.person_id=b.person_id AND y.criterion_id=b.criterion_id
                WHERE a.criterion_id<b.criterion_id AND x.contribution>0 AND y.contribution>0""").fetchone()[0]
            if duplicate_points: raise ValueError('Shared evidence credited more than once')
    if published:
        # Population queries may already have spilled to disk. RuntimeScores
        # configures its own spill directory, which DuckDB cannot switch once
        # the current directory has been used. Close the audit connection first
        # and recalculate on a fresh one, as the product compiler does.
        with store.connection() as db:
            _configure_audit(db, memory_limit)
            from yoyoflot.product.scoring import RuntimeScores
            baseline = store.path / 'base_manifest.json'
            from yoyoflot.provenance import fingerprint
            # Recompute final weights/dependencies from the preserved baseline
            # explanations and original evaluated source factors.
            db.read_parquet(str(store.path/'baseline_explanations.parquet')).create_view('explanations', replace=True)
            db.read_parquet(str(store.path/'baseline_rankings.parquet')).create_view('rankings', replace=True)
            recomputed = RuntimeScores(db, store.scoring, fingerprint(baseline),
                                       fingerprint(store.assessment_root/'assessment_manifest.json'))
            try:
                # RuntimeScores sets its own 512MB calculation budget. Restore
                # the audit budget before comparing both complete factor sets.
                _configure_audit(db, memory_limit)
                diff = db.execute("""SELECT count(*) FROM (
                    (SELECT person_id,criterion_id,contribution FROM read_parquet(?)
                     EXCEPT SELECT person_id,criterion_id,contribution FROM read_parquet(?))
                    UNION ALL
                    (SELECT person_id,criterion_id,contribution FROM read_parquet(?)
                     EXCEPT SELECT person_id,criterion_id,contribution FROM read_parquet(?)))""",
                     [str(store.path/'tables/explanations.parquet'), str(recomputed.root/'factors.parquet'),
                      str(recomputed.root/'factors.parquet'), str(store.path/'tables/explanations.parquet')]).fetchone()[0]
                if diff: raise ValueError('Published contributions differ from recalculation')
            finally:
                recomputed.close()
    return {'status':'passed', 'run_id':store.manifest['run_id'], 'counts':store.manifest['counts'],
            'profiles_independently_checked':len(top), 'segments_checked':segments,
            'evidence_links_checked':evidence_count, 'population_scores_consistent':True,
            'raw_reingestion_verified':store.manifest.get('source_provenance', {}).get('raw_reingestion_verified', False),
            'source_provenance':store.manifest.get('source_provenance', {}),
            'independent_human_review':'pending'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product',type=Path,default=ROOT/'artifacts/source-product')
    parser.add_argument('--report',type=Path)
    parser.add_argument('--memory-limit',help='Audit memory limit; defaults to YOYO_VERIFY_MEMORY_LIMIT or 8GB')
    args = parser.parse_args()
    result = check(args.product, memory_limit=args.memory_limit)
    content = json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n'
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(content,encoding='utf-8')
    print(content,end='')


if __name__ == '__main__':
    main()
