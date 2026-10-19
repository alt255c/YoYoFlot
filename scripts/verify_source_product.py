"""Audit the supplied-data product without generating data or loading pickle."""
import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from yoyoflot.product.store import ProductStore


def check(product):
    store = ProductStore(product)
    top = store.top()
    expected = min(50, next((r['profiles'] for r in store.manifest['eligibility'] if r['eligibility_status'] == 'eligible'), 0))
    if len(top) != expected:
        raise ValueError('Top size differs from eligible count')
    if [r['rank'] for r in top] != list(range(1, len(top) + 1)):
        raise ValueError('Noncontiguous ranks')
    keys = [(-r['matched_criteria_count'], -r['rule_score'], r['person_id']) for r in top]
    if keys != sorted(keys):
        raise ValueError('Wrong ranking order')
    segments = evidence_count = 0
    for person in top:
        dossier = store.profile(person['person_id'])
        rows = dossier['segments']
        ids = {r['segment_id'] for r in rows}
        if len(ids) != len(rows) or any(r['person_id'] != person['person_id'] for r in rows):
            raise ValueError('Duplicate or foreign route')
        factors = dossier['factors']
        if sum(f['status'] == 'matched' for f in factors) != person['matched_criteria_count']:
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
        invalid = db.execute('''SELECT count(*) FROM explanations
            WHERE status IS NULL OR status NOT IN ('matched','not_matched','unavailable')
               OR contribution IS NULL OR NOT isfinite(contribution) OR contribution<0
               OR threshold IS NULL OR NOT isfinite(threshold) OR threshold<=0
               OR weight IS NULL OR NOT isfinite(weight) OR weight<=0
               OR (status<>'unavailable' AND (value IS NULL OR NOT isfinite(value)))''').fetchone()[0]
        if invalid:
            raise ValueError('Invalid population factor state or numeric value')
        invalid = db.execute('''WITH actual AS (SELECT person_id,sum(contribution) score,
            count(*) factor_count, count(DISTINCT criterion_id) distinct_factor_count,
            count(*) FILTER(WHERE status<>'unavailable') available_count,
            count(*) FILTER(WHERE status='matched') AS matched_count FROM explanations GROUP BY person_id)
            SELECT count(*) FROM rankings r FULL OUTER JOIN actual a USING(person_id)
            WHERE r.person_id IS NULL OR a.person_id IS NULL OR a.factor_count<>3
               OR a.distinct_factor_count<>3 OR r.rule_score IS NULL OR NOT isfinite(r.rule_score)
               OR abs(r.rule_score-a.score)>1e-10 OR r.matched_criteria_count IS DISTINCT FROM a.matched_count
               OR r.available_criteria_count IS DISTINCT FROM a.available_count
               OR r.eligibility_status IS DISTINCT FROM CASE WHEN a.available_count=0 THEN 'insufficient_data'
                    WHEN a.matched_count=0 THEN 'no_matches' ELSE 'eligible' END''').fetchone()[0]
        if invalid:
            raise ValueError('Population score/explanation mismatch')
        expected_ids = [row[0] for row in db.execute('''SELECT person_id FROM rankings
            WHERE eligibility_status='eligible'
            ORDER BY matched_criteria_count DESC,rule_score DESC,person_id LIMIT 50''').fetchall()]
        if expected_ids != [person['person_id'] for person in top]:
            raise ValueError('Top excludes a higher-ranked eligible profile')
        invalid = db.execute('''WITH ordered AS (SELECT person_id,
            row_number() OVER(ORDER BY matched_criteria_count DESC,rule_score DESC,person_id) expected_rank
            FROM rankings WHERE eligibility_status='eligible')
            SELECT count(*) FROM rankings r LEFT JOIN ordered o USING(person_id)
            WHERE r.rank IS DISTINCT FROM o.expected_rank''').fetchone()[0]
        if invalid:
            raise ValueError('Population ranks are not consistent with the declared ordering')
        if store.manifest['forest_status'] == 'complete':
            invalid = db.execute('''SELECT count(*) FROM if_scores WHERE if_score IS NULL OR NOT isfinite(if_score)
                OR if_percentile IS NULL OR rules_percentile IS NULL OR combined_score IS NULL
                OR if_percentile NOT BETWEEN 0 AND 1
                OR rules_percentile NOT BETWEEN 0 AND 1 OR abs(combined_score-(if_percentile+rules_percentile)*0.5)>1e-12''').fetchone()[0]
            if invalid:
                raise ValueError('Invalid model percentiles/combination')
            roles = db.execute('SELECT role,count(*) FROM if_scores GROUP BY role ORDER BY role').fetchall()
        else:
            roles = []
    return {'status':'passed', 'run_id':store.manifest['run_id'], 'counts':store.manifest['counts'],
            'profiles_independently_checked':len(top), 'segments_checked':segments,
            'evidence_links_checked':evidence_count, 'population_scores_consistent':True,
            'if_roles':dict(roles), 'input_sha256':{n:h for n,h in store.manifest['request']['inputs'].items() if n.endswith('.parquet')},
            'raw_reingestion_verified':store.manifest.get('source_provenance', {}).get('raw_reingestion_verified', False),
            'source_provenance':store.manifest.get('source_provenance', {}),
            'independent_human_review':'pending'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--product',type=Path,default=ROOT/'artifacts/source-product')
    parser.add_argument('--report',type=Path)
    args = parser.parse_args()
    result = check(args.product)
    content = json.dumps(result,ensure_ascii=False,indent=2,sort_keys=True,allow_nan=False)+'\n'
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(content,encoding='utf-8')
    print(content,end='')


if __name__ == '__main__':
    main()
