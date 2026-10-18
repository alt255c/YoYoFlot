"""Independent persisted-table oracle: grouped AP, pairwise AUC and tie bounds.

Does not import the production metrics helper or load any model pickle.
"""
import csv
import json
import math
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from ..provenance import fingerprint
from .storage import validate_snapshot


def audit_snapshot(path):
    path = Path(path)
    report = validate_snapshot(path)
    evaluation = json.loads((path/'evaluation.json').read_text(encoding='utf-8'))
    assert report==evaluation['report']
    for filename,field in [('comparison.csv','comparison'),('stability.csv','stability'),('method_agreement.csv','method_agreement'),
                            ('paired_comparison.csv','paired_comparison'),('case_summary.csv','case_summary')]:
        with (path/filename).open(encoding='utf-8',newline='') as stream:csv_rows=list(csv.DictReader(stream))
        assert csv_rows==[{k:'' if v is None else str(v) for k,v in r.items()} for r in evaluation[field]]
    scores = pq.ParquetFile(path/'scores.parquet').read().to_pylist()
    annotations = pq.ParquetFile(path/'scenarios.parquet').read().to_pylist()
    grouped, truth = defaultdict(dict), defaultdict(dict)
    for row in scores:
        identity=(row['experiment'],row['method'])
        assert row['profile_id'] not in grouped[identity]
        grouped[identity][row['profile_id']]=row
    for row in annotations:
        assert row['profile_id'] not in truth[row['experiment']]
        truth[row['experiment']][row['profile_id']]=row['benchmark_label']
    methods=['rules','isolation_forest','combination']
    available={}
    for name, labels in truth.items():
        assert all(set(grouped[name,method])==set(labels) for method in methods)
        available[name]=set.intersection(*[{pid for pid,row in grouped[name,method].items() if row['available']} for method in methods])
    def value(row):
        return (row['matched_criteria_count'],row['rule_score']) if row['method']=='rules' else row['score']
    def close(actual,expected):
        assert actual is None if expected is None else actual is not None and math.isclose(actual,expected,rel_tol=1e-12,abs_tol=1e-12),(actual,expected)
    quality={p['name']:p['quality_enabled'] for p in evaluation['request']['plan']}
    count=0
    def verify_metric(metric,name,universe,enabled):
        nonlocal count
        rows=grouped[name,metric['method']]; labels=truth[name]
        assert metric['common_profiles']==len(universe)
        known=[pid for pid in universe if labels[pid] is not None]
        positives=[pid for pid in known if labels[pid]==1]
        negatives=[pid for pid in known if labels[pid]==0]
        ap=auc=None
        if enabled and positives and negatives:
            bins=defaultdict(list)
            for pid in known:bins[value(rows[pid])].append(labels[pid])
            seen=tp=0;pieces=[]
            for score in sorted(bins,reverse=True):
                seen+=len(bins[score]);increment=sum(bins[score]);tp+=increment
                pieces.append(increment/len(positives)*(tp/seen))
            ap=math.fsum(pieces)
            wins=math.fsum(1 if value(rows[p])>value(rows[n]) else .5 if value(rows[p])==value(rows[n]) else 0 for p in positives for n in negatives)
            auc=wins/(len(positives)*len(negatives))
        close(metric['average_precision'],ap);close(metric['roc_auc'],auc)
        candidates=sorted([r for pid,r in rows.items() if pid in universe and r['available'] and r['rank'] is not None],key=lambda r:r['rank'])
        chosen=candidates[:metric['k']]
        assert metric['selected']==len(chosen)
        labels_top=[labels[r['profile_id']] for r in chosen]
        precision=sum(v==1 for v in labels_top)/len(chosen) if enabled and chosen and None not in labels_top else None
        close(metric['precision_at_k'],precision)
        low=high=None
        if enabled and chosen:
            boundary=value(chosen[-1]);above=[r for r in candidates if value(r)>boundary];tied=[r for r in candidates if value(r)==boundary]
            if all(labels[r['profile_id']] is not None for r in above+tied):
                fixed=sum(labels[r['profile_id']]==1 for r in above)
                zero=sum(labels[r['profile_id']]==0 for r in tied);one=len(tied)-zero;slots=len(chosen)-len(above)
                low=(fixed+max(0,slots-zero))/len(chosen);high=(fixed+min(slots,one))/len(chosen)
        close(metric['precision_tie_lower'],low);close(metric['precision_tie_upper'],high)
        count+=1
    for metric in evaluation['comparison']:
        name=metric['experiment'];verify_metric(metric,name,available[name],quality[name])
    for metric in evaluation['paired_comparison']:
        variant=metric['experiment'];name='baseline' if metric['side']=='baseline' else variant
        verify_metric(metric,name,available['baseline']&available[variant],quality[variant])
    for item in evaluation['report']['parents']:
        parent=path/'parents'/item['experiment']
        assert fingerprint(parent/'run_manifest.json')==item['manifest_sha256']
        for name in ['rankings.parquet','top50.parquet']:
            assert fingerprint(parent/name)==fingerprint(parent/'parents/evaluation/rules'/name)
        matrices=pq.ParquetFile(parent/'matrices.parquet').read().to_pylist()
        assert all(not {'benchmark_label','case_id','profile_id','source_coverage'}.intersection(r['feature_names']) for r in matrices)
        for method,file,score_field in [('rules','rankings.parquet','rule_score'),('isolation_forest','if_rankings.parquet','if_score'),('combination','combined_rankings.parquet','combined_score')]:
            for row in pq.ParquetFile(parent/file).read().to_pylist():
                projected=grouped[item['experiment'],method][row['profile_id']]
                assert all(projected[k]==row[k] for k in ['rank','rule_score','matched_criteria_count'])
                assert projected['score']==row[score_field] and projected['parent_run_id']==row['run_id']
    with (path/'review/ranking_audit.csv').open(encoding='utf-8',newline='') as stream:
        audit=list(csv.DictReader(stream))
    assert len(audit)==report['audit']['selected'] and all(not r[field] for r in audit for field in ['reviewer','verdict','reason_ru','reviewed_at'])
    assert all(r['passed'] for r in report['invariants']) and not report['ui_ready']
    return dict(result='passed',metric_rows_checked=count,checks=['grouped_AP_oracle','pairwise_tied_ROC_oracle',
        'actual_selected_precision_and_tie_bounds','paired_population_intersections','parent_primary_byte_identity',
        'label_and_quality_exclusion_from_matrices','blank_independent_review','snapshot_inventory_and_generator_replay','CSV_JSON_and_parent_projection_consistency'],
        existing_models_deserialized=False)
