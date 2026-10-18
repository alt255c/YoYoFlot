import copy
import csv
import json
import math
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import joblib
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml
from scipy.stats import spearmanr

from yoyoflot.cli import main
from yoyoflot.evaluation import artifact
from yoyoflot.evaluation.audit import FIELDS, import_reviews
from yoyoflot.evaluation.compare import (METHODS, common_universe, key, metrics, midranks,
                                        overlap, selected, spearman, stability)
from yoyoflot.evaluation.plan import build_plan, load_config
from yoyoflot.evaluation.scenarios import annotations
from yoyoflot.evaluation.storage import validate_snapshot
from yoyoflot.evaluation.verify import audit_snapshot
from yoyoflot.features.graph import load_graph_config
from yoyoflot.features.graph_demo import _generate_observations
from yoyoflot.models.demo import (CENSORED_GENERATOR, END, HOLDOUT_GENERATOR, START, PROFILE_SCHEMA,
                                  create_demo, generate_rows, recipe, validate_demo, write_json)
from yoyoflot.models.isolation_forest import load_config as load_forest
from yoyoflot.models.rule_score import load_model
from yoyoflot.models.rule_score import _score_profiles
from yoyoflot.normalize.locking import OutputLock
from yoyoflot.provenance import fingerprint

ROOT = Path(__file__).resolve().parents[1]
EVAL, FOREST, RULES, GRAPH = [ROOT / 'configs' / name for name in ['evaluation.yaml', 'isolation_forest.yaml', 'model.yaml', 'graph.yaml']]


def read(path):return pq.ParquetFile(path).read().to_pylist()


def configs(path, graph=False, variants=False):
    path.mkdir(parents=True, exist_ok=True)
    cfg = load_config(EVAL)[0]
    cfg.update(profiles=36, top_k=[10, 25]); cfg['audit']['size']=20
    for name in ['if_seeds', 'trees', 'max_samples', 'threshold_factors', 'rules_weights', 'as_of', 'graph_seeds']:
        cfg['experiments'][name]=[]
    cfg['experiments'].update(include_graph=graph, group_ablations=variants, missingness_check=variants)
    forest=load_forest(FOREST)[0]
    forest['forest'].update(n_estimators=16, max_samples=32)
    forest['reference'].update(training_profiles=30, calibration_profiles=20, min_training_rows=8, min_calibration_rows=8)
    g=load_graph_config(GRAPH)[0]; g['null_model']['simulations']=19
    for name,value in [('evaluation.yaml',cfg),('forest.yaml',forest),('graph.yaml',g)]:
        (path/name).write_text(yaml.safe_dump(value,sort_keys=False),encoding='utf-8')
    return path/'evaluation.yaml',path/'forest.yaml',RULES,path/'graph.yaml'


def execute(path, paths, **kwargs):
    report,summary=artifact.run(*paths,artifacts_dir=path/'artifacts',work_dir=path/'work',**kwargs)
    return report, path/'artifacts'/report['run_id'],summary


@pytest.fixture(scope='module')
def snapshot(tmp_path_factory):
    path=tmp_path_factory.mktemp('evaluation')
    paths=configs(path/'configs',variants=True)
    report,target,_=execute(path,paths)
    return report,target


def model_rows(scores=(0.9,0.8,0.8,0.1), labels=(1,0,1,0)):
    methods={m:[] for m in METHODS}
    for method in METHODS:
        for i,value in enumerate(scores):
            methods[method].append(dict(profile_id=f'p{i}',method=method,score=value,matched_criteria_count=1,
                rule_score=value,available=value is not None,rank=i+1 if value is not None else None,status='scored'))
    cases=[dict(profile_id=f'p{i}',benchmark_label=label) for i,label in enumerate(labels)]
    return methods,cases


@pytest.mark.parametrize('generator',[HOLDOUT_GENERATOR,CENSORED_GENERATOR])
@pytest.mark.parametrize('count',[0,36,120,500])
def test_holdout_replay_bounds_and_annotations(tmp_path,generator,count):
    spec=recipe(303,count,generator)
    people,rows=generate_rows(spec)
    path=create_demo(tmp_path,303,count,generator)
    manifest,actual_people,actual_rows=validate_demo(path)
    assert actual_people==people and actual_rows==rows and manifest['recipe']==spec
    assert all(r['departure_date'] is None or START<=r['departure_date']<=END for r in rows)
    assert len(rows)<=60*count and len(people)==count
    cases=annotations(spec)
    assert len(cases)==count and all(r['label_scope']=='designed_scenario_not_person_trait' for r in cases)
    assert not {'benchmark_label','case_id'}.intersection(PROFILE_SCHEMA.names)


def test_holdout_catalog_and_explicit_planted_pair_are_independent():
    spec=recipe(303,120,HOLDOUT_GENERATOR)
    people,segments=generate_rows(spec)
    catalog=annotations(spec)
    assert Counter(a['benchmark_label'] for a in catalog)=={1:42,0:38,None:40}
    _,_,meta=_generate_observations(people,segments,29)
    assert meta['planted_pair']==[a['profile_id'] for a in catalog if a['case_id']=='coflight_stress']
    censored_people,censored_rows=generate_rows(recipe(303,120,CENSORED_GENERATOR))
    assert censored_people==people and [s['segment_id'] for s in censored_rows]==[s['segment_id'] for s in segments]
    assert sum(s['origin_iata'] is None for s in censored_rows)>sum(s['origin_iata'] is None for s in segments)
    _,other_segments=generate_rows(recipe(404,120,HOLDOUT_GENERATOR))
    assert Counter(s['profile_id'].rsplit('_',1)[1] for s in segments)!=Counter(s['profile_id'].rsplit('_',1)[1] for s in other_segments)


def test_external_fixture_contract_for_scenario_population_and_coverage():
    fixture=json.loads((ROOT/'tests/fixtures/scenario_data.json').read_text())
    people,segments=generate_rows(fixture['recipe']);catalog=annotations(fixture['recipe'])
    scores,_,_,_=_score_profiles(people,segments,load_model(RULES)[0])
    expected=fixture['expected']
    assert len(people)==expected['profiles']
    assert sum(r['eligibility_status']!='insufficient_data' for r in scores)==expected['comparable_profiles']
    assert sum(a['benchmark_label']==1 for a in catalog)==expected['stress_labels']
    assert sum(a['benchmark_label']==0 for a in catalog)==expected['control_labels']
    assert sum(a['benchmark_label'] is None for a in catalog)==expected['unknown_labels']
    _,_,meta=_generate_observations(people,segments,29)
    assert meta['planted_pair']==expected['coflight_pair']


def test_forged_holdout_rows_do_not_pass_replay(tmp_path):
    path=create_demo(tmp_path,303,36,HOLDOUT_GENERATOR)
    people=read(path/'profiles.parquet');people[0]['display_name']='Imported person'
    pq.write_table(pa.Table.from_pylist(people,schema=PROFILE_SCHEMA),path/'profiles.parquet')
    manifest=json.loads((path/'demo_manifest.json').read_text());manifest['files']['profiles.parquet']=fingerprint(path/'profiles.parquet')
    write_json(path/'demo_manifest.json',manifest)
    with pytest.raises(ValueError,match='не воспроизводятся'):validate_demo(path)


def test_tie_metrics_match_independent_counts():
    methods,cases=model_rows()
    result=metrics(methods,cases,[2,50],True)
    for row in result:
        assert row['average_precision']==pytest.approx(5/6)
        assert row['roc_auc']==pytest.approx(7/8)
        assert row['positive_prevalence']==0.5
        assert row['selected']==(2 if row['k']==2 else 4)
        assert row['precision_at_k']==0.5
        assert row['precision_tie_lower']==0.5 and row['precision_tie_upper']==(1.0 if row['k']==2 else 0.5)
    assert midranks([2,1,2,4])==[2.5,1,2.5,4]
    assert spearman([1,2,2,4],[4,1,1,2])==pytest.approx(spearmanr([1,2,2,4],[4,1,1,2]).statistic)


def test_rules_key_preserves_count_before_weight_and_ties():
    methods,cases=model_rows()
    rows=methods['rules']
    rows[0].update(matched_criteria_count=1,rule_score=100)
    rows[1].update(matched_criteria_count=2,rule_score=1)
    assert key(rows[1])>key(rows[0])
    assert spearman([key(rows[0]),key(rows[1])],[100,1])==-1


@pytest.mark.parametrize('left,right,expected',[([],[],None),([1],[1],None),([1,1],[2,3],None),([1,2,3],[3,2,1],-1),([1,2],[1,2],1)])
def test_constant_empty_and_ordered_spearman(left,right,expected):
    assert spearman(left,right)==expected


def test_unavailable_values_not_zero_and_denominators_are_actual_selected():
    methods,cases=model_rows(scores=(.9,.8,None,.1))
    for method in METHODS:methods[method][3]['rank']=None
    result=metrics(methods,cases,[50],True)
    assert common_universe(methods)=={'p0','p1','p3'}
    for row in result:
        assert row['selected']==2 and row['precision_at_k']==.5
        assert row['available']==3 and row['common_profiles']==3
    methods['rules'][0]['rule_score']=float('nan')
    with pytest.raises(ValueError):common_universe(methods)


def test_unlabelled_and_changed_window_quality_are_null():
    methods,cases=model_rows()
    assert all(r['average_precision'] is r['precision_at_k'] is None for r in metrics(methods,cases,[10],False))
    for case in cases:case['benchmark_label']=None
    assert all(r['average_precision'] is r['precision_at_k'] is None for r in metrics(methods,cases,[10],True))
    with pytest.raises(ValueError):metrics(methods,cases,[10],True,{'unavailable'})


def test_different_people_not_reported_as_instability():
    left,_=model_rows();right=copy.deepcopy(left)
    for rows in right.values():
        for row in rows:row['profile_id']='other_'+row['profile_id']
    assert all(not r['comparable'] and r['end_to_end_jaccard'] is r['spearman_common'] is None for r in stability(left,right,[2,50]))
    assert overlap([],[])['jaccard'] is None


def test_coverage_changes_have_separate_end_to_end_and_common_top():
    left,_=model_rows();right=copy.deepcopy(left)
    for rows in right.values():rows[0].update(available=False,rank=None,score=None)
    result=stability(left,right,[2])
    assert all(r['excluded']==1 and r['shared_common']==3 for r in result)
    assert all(r['end_to_end_jaccard']==pytest.approx(1/3) and r['common_population_jaccard']==1 for r in result)


def test_bad_ids_ranks_labels_and_population_mismatch_fail():
    methods,cases=model_rows()
    methods['rules'].append(copy.deepcopy(methods['rules'][0]))
    with pytest.raises(ValueError):common_universe(methods)
    methods,cases=model_rows();methods['rules'][0]['rank']=2
    with pytest.raises(ValueError):selected(methods['rules'],2)
    methods,cases=model_rows();cases[0]['benchmark_label']=True
    with pytest.raises(ValueError):metrics(methods,cases,[2],True)
    methods,cases=model_rows();methods['rules'].pop()
    with pytest.raises(ValueError):common_universe(methods)


@pytest.mark.parametrize('kind',['unknown','mode','count','bool_seed','same_seed','k','k_order','audit','trees','weights','threshold','duplicate','date','flag'])
def test_invalid_configs_are_rejected(tmp_path,kind):
    cfg=load_config(EVAL)[0]
    if kind=='unknown':cfg['automatic_promotion']=True
    elif kind=='mode':cfg['data_mode']='real'
    elif kind=='count':cfg['profiles']=241
    elif kind=='bool_seed':cfg['seed']=True
    elif kind=='same_seed':cfg['replicate_seed']=cfg['seed']
    elif kind=='k':cfg['top_k']=[51]
    elif kind=='k_order':cfg['top_k']=[25,10]
    elif kind=='audit':cfg['audit']['size']=0
    elif kind=='trees':cfg['experiments']['trees']=[513]
    elif kind=='weights':cfg['experiments']['rules_weights']=[float('nan')]
    elif kind=='threshold':cfg['experiments']['threshold_factors']=[2]
    elif kind=='duplicate':cfg['experiments']['if_seeds']=[1,1]
    elif kind=='date':cfg['experiments']['as_of']=['2026-04-01']
    else:cfg['experiments']['include_graph']='false'
    path=tmp_path/'cfg.yaml';path.write_text(yaml.safe_dump(cfg),encoding='utf-8')
    with pytest.raises(ValueError):load_config(path)


def test_prespecified_plan_and_group_ablations_do_not_mutate_baselines():
    cfg=load_config(EVAL)[0];forest=load_forest(FOREST)[0];rules=load_model(RULES)[0];graph=load_graph_config(GRAPH)[0]
    before=copy.deepcopy((forest,rules,graph))
    plans=build_plan(cfg,forest,rules,graph)
    assert len(plans)==21 and (forest,rules,graph)==before
    assert len({p['name'] for p in plans})==len(plans)
    for plan in plans:
        if plan['name'].startswith('without_'):
            assert len(plan['forest']['features'])==2 and plan['rules']['coverage']['min_available_groups']==2
        assert not next(c for c in plan['rules']['criteria'] if c['id']=='M07')['enabled']
    cfg['seed']=forest['reference']['training_seed']
    with pytest.raises(ValueError,match='пересекается'):build_plan(cfg,forest,rules,graph)


def test_full_snapshot_schemas_metrics_parents_and_unfilled_blind_review(snapshot):
    report,path=snapshot
    assert validate_snapshot(path)==report and report['experiments']==7 and not report['ui_ready']
    assert report['audit']['completed']==0 and report['audit']['selected']==20
    for filename,schema in [('scores.parquet',artifact.SCORE_SCHEMA),('scenarios.parquet',artifact.SCENARIO_SCHEMA),('disagreements.parquet',artifact.DISAGREEMENT_SCHEMA)]:
        file=pq.ParquetFile(path/filename)
        assert file.schema_arrow.equals(schema,check_metadata=False)
        assert file.schema_arrow.metadata[b'run_id']==report['run_id'].encode('ascii')
    key_rows=read(path/'private/audit_key.parquet')
    with (path/'review/ranking_audit.csv').open(encoding='utf-8',newline='') as stream:reviews=list(csv.DictReader(stream))
    assert len(reviews)==len(key_rows)==20
    assert all(not row[field] for row in reviews for field in FIELDS[1:])
    for filename in ['profiles.parquet','segments.parquet','coflight_context.parquet']:
        rows=read(path/'review/blind'/filename)
        assert not {'profile_id','case_id','benchmark_label','score','rank'}.intersection(pq.ParquetFile(path/'review/blind'/filename).schema_arrow.names)
        assert all(row['review_id'].startswith('review_') for row in rows)
    values=json.loads((path/'evaluation.json').read_text())
    assert all(r['average_precision'] is None for r in values['comparison'] if r['experiment'] in {'in_domain','missing_routes'})
    assert all(r['common_profiles']==24 for r in values['comparison'] if r['experiment']=='baseline')
    assert all(r['common_profiles']<=24 for r in values['paired_comparison'])
    assert all('benchmark_label' not in r['feature_names'] for r in read(path/'parents/baseline/matrices.parquet'))


def test_independent_oracle_does_not_call_production_metrics_or_unpickle(snapshot,monkeypatch):
    from yoyoflot.evaluation import compare
    def forbidden(*args,**kwargs):raise AssertionError('Not an independent persisted-table audit')
    monkeypatch.setattr(compare,'metrics',forbidden);monkeypatch.setattr(joblib,'load',forbidden)
    result=audit_snapshot(snapshot[1])
    assert result['result']=='passed' and result['metric_rows_checked']>0 and not result['existing_models_deserialized']


def test_independent_oracle_detects_wrong_metric_even_with_recomputed_hash(snapshot,tmp_path):
    target=tmp_path/'copy';shutil.copytree(snapshot[1],target)
    values=json.loads((target/'evaluation.json').read_text());values['comparison'][0]['roc_auc']=0.123
    write_json(target/'evaluation.json',values)
    manifest=json.loads((target/'run_manifest.json').read_text());manifest['files']['evaluation.json']=fingerprint(target/'evaluation.json')
    write_json(target/'run_manifest.json',manifest)
    with pytest.raises(AssertionError):audit_snapshot(target)


def test_cached_snapshot_never_unpickles_or_retrains(tmp_path,monkeypatch):
    paths=configs(tmp_path/'config')
    report,path,summary=execute(tmp_path,paths)
    def forbidden(*args,**kwargs):raise AssertionError('Loaded/retrained cached model')
    monkeypatch.setattr(joblib,'load',forbidden);monkeypatch.setattr(artifact,'train_run',forbidden)
    assert execute(tmp_path,paths)==(report,path,summary)


def test_changed_training_library_version_invalidates_T20_cache(tmp_path,monkeypatch):
    paths=configs(tmp_path/'config')
    _,path,_=execute(tmp_path,paths)
    before=fingerprint(path/'run_manifest.json')
    runtime=artifact._runtime();runtime['training_runtime']['libraries']['scikit-learn']='changed-version-for-test'
    monkeypatch.setattr(artifact,'_runtime',lambda:runtime)
    def fresh_run_required(*args,**kwargs):raise RuntimeError('fresh parent required after library change')
    monkeypatch.setattr(artifact,'train_run',fresh_run_required)
    with pytest.raises(RuntimeError,match='fresh parent required'):execute(tmp_path,paths)
    assert fingerprint(path/'run_manifest.json')==before


def test_no_IF_model_keeps_primary_and_reports_empty_common_group(tmp_path):
    paths=configs(tmp_path/'config')
    forest=yaml.safe_load(paths[1].read_text());forest['enabled']=False
    paths[1].write_text(yaml.safe_dump(forest),encoding='utf-8')
    report,path,_=execute(tmp_path,paths)
    assert all(not r['training_performed'] for r in report['parents'])
    assert (path/'parents/baseline/top50.parquet').is_file()
    assert not (path/'parents/baseline/model.joblib').exists()
    values=json.loads((path/'evaluation.json').read_text())
    assert all(r['common_profiles']==0 and r['average_precision'] is r['precision_at_k'] is None for r in values['comparison'])


@pytest.mark.parametrize('kind',['hash','extra','missing'])
def test_corrupted_outputs_fail_without_overwrite(snapshot,tmp_path,kind):
    _,source=snapshot;target=tmp_path/'copy';shutil.copytree(source,target)
    if kind=='hash':(target/'comparison.csv').write_text('changed',encoding='utf-8')
    elif kind=='extra':(target/'unknown.txt').write_text('extra',encoding='utf-8')
    else:(target/'scores.parquet').unlink()
    with pytest.raises(ValueError):validate_snapshot(target)
    if kind=='hash':assert (target/'comparison.csv').read_text()=='changed'


def review_copy(snapshot,tmp_path,fill=False):
    _,path=snapshot
    with (path/'review/ranking_audit.csv').open(encoding='utf-8',newline='') as stream:rows=list(csv.DictReader(stream))
    if fill:
        for row in rows:row.update(reviewer='Тестовый проверяющий (fixture)',verdict='обычная_история',reason_ru='Искусственное решение только для теста импортера.',reviewed_at='2026-10-18T00:00:00+03:00')
    output=tmp_path/'reviews.csv'
    with output.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    return path,output,rows


@pytest.mark.parametrize('fill',[False,True])
def test_review_import_preserves_original_and_does_not_authenticate_people(snapshot,tmp_path,fill):
    path,reviews,_=review_copy(snapshot,tmp_path,fill)
    before=fingerprint(path/'run_manifest.json')
    report,target=import_reviews(path,reviews,artifacts_dir=tmp_path/'reviews')
    assert report['completed']==(20 if fill else 0) and not report['independence_verified'] and not report['ui_ready']
    assert report['population_precision'] is None and fingerprint(path/'run_manifest.json')==before
    assert (target/'reviews.csv').read_bytes()==reviews.read_bytes()
    assert import_reviews(path,reviews,artifacts_dir=tmp_path/'reviews')==(report,target)


def test_review_import_never_loads_model_or_changes_snapshot(snapshot,tmp_path,monkeypatch):
    path,reviews,_=review_copy(snapshot,tmp_path)
    def forbidden(*args,**kwargs):raise AssertionError('Unpickled reviewed model')
    monkeypatch.setattr(joblib,'load',forbidden)
    report,_=import_reviews(path,reviews,artifacts_dir=tmp_path/'reports')
    assert report['completed']==0
    with pytest.raises(ValueError,match='пересекаться'):import_reviews(path,reviews,output=path/'review/new')


@pytest.mark.parametrize('kind',['duplicate','unknown','missing','partial','verdict','date','timezone'])
def test_invalid_review_rows_are_blocked(snapshot,tmp_path,kind):
    path,reviews,rows=review_copy(snapshot,tmp_path,True)
    if kind=='duplicate':rows[1]=copy.deepcopy(rows[0])
    elif kind=='unknown':rows[0]['review_id']='unknown'
    elif kind=='missing':rows.pop()
    elif kind=='partial':rows[0]['reason_ru']=''
    elif kind=='verdict':rows[0]['verdict']='spy'
    elif kind=='date':rows[0]['reviewed_at']='yesterday'
    else:rows[0]['reviewed_at']='2026-10-18T00:00:00'
    with reviews.open('w',encoding='utf-8',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    with pytest.raises(ValueError):import_reviews(path,reviews,artifacts_dir=tmp_path/'reports')
    assert not (tmp_path/'reports').exists()


def test_lock_interruption_resume_and_unrelated_output_preserved(tmp_path,monkeypatch):
    paths=configs(tmp_path/'config');output=tmp_path/'artifacts/new';output.parent.mkdir()
    with OutputLock(output.parent/'.t20-evaluation.lock','test'):
        with pytest.raises(RuntimeError,match='другим процессом'):execute(tmp_path,paths,output=output)
    actual=artifact.train_run;calls=[]
    def interrupted(*args,**kwargs):
        calls.append(1)
        if len(calls)==2:raise KeyboardInterrupt
        return actual(*args,**kwargs)
    monkeypatch.setattr(artifact,'train_run',interrupted)
    with pytest.raises(KeyboardInterrupt):execute(tmp_path,paths,output=output)
    assert not output.exists() and not list(output.parent.glob('*.partial'))
    monkeypatch.setattr(artifact,'train_run',actual)
    assert execute(tmp_path,paths,output=output)[0]['experiments']==3
    unrelated=tmp_path/'unrelated';unrelated.mkdir();(unrelated/'keep.txt').write_text('keep')
    with pytest.raises(ValueError,match='завершённый'):execute(tmp_path,paths,output=unrelated)
    assert (unrelated/'keep.txt').read_text()=='keep'


def test_graph_null_MC_seed_does_not_change_actual_model(tmp_path):
    paths=configs(tmp_path/'config',graph=True)
    report,path,_=execute(tmp_path,paths)
    assert {'diagnostic_MC_seed_does_not_change_model','holdout_planted_pair_matches_explicit_events'}.issubset(r['check'] for r in report['invariants'])
    assert all(r['passed'] for r in report['invariants'])
    assert read(path/'review/blind/coflight_context.parquet')


def test_script_other_cwd_equals_flags_quiet_and_help(tmp_path,capsys):
    assert main(['evaluate'])==2;capsys.readouterr()
    paths=configs(tmp_path/'config')
    env=dict(os.environ,PYTHONIOENCODING='utf-8')
    args=[sys.executable,str(ROOT/'scripts/run_t20.py'),f'--config={paths[0]}',f'--if-config={paths[1]}',f'--graph-config={paths[3]}',
          '--work-dir',str(tmp_path/'work'),'--artifacts-dir',str(tmp_path/'artifacts'),'-q']
    result=subprocess.run(args,cwd=tmp_path,env=env,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
    assert result.stdout==''
    help_run=subprocess.run([sys.executable,str(ROOT/'scripts/audit_t20.py'),'--help'],cwd=tmp_path,env=env,capture_output=True,text=True,encoding='utf-8')
    assert help_run.returncode==0 and '--reviews' in help_run.stdout
