"""Small controlled cases test arithmetic; the shipped app never uses these inputs."""
import copy
import json
import threading
from datetime import date, timedelta
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen
from http.server import ThreadingHTTPServer

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.cli import main
from yoyoflot.product.build import _features, _graph, run
from yoyoflot.product.config import load
from yoyoflot.product.input import verify
from yoyoflot.product.store import ProductStore
from yoyoflot.product.web import handler
from yoyoflot.provenance import fingerprint
from yoyoflot.resolve.common import SCHEMAS

ROOT=Path(__file__).resolve().parents[1]


def tables(tmp_path, people=3):
    root=tmp_path/'input'
    root.mkdir()
    persons=[]
    segments=[]
    for i in range(people):
        pid=f'person_{i:03d}'
        persons.append(dict(person_id=pid,display_name=f'Контроль {i}',identity_status='accepted'))
        for day in range(8):
            a,b=('openflights:2965','openflights:2990') if day%2 else ('openflights:2990','openflights:2965')
            segments.append(dict(person_id=pid,segment_id=f'{pid}_{day}',anchor_travel_id=f'travel_{i}_{day}',
                departure_date=(date(2017,1,1)+timedelta(days=day)).isoformat(),
                origin_airport_id=a,destination_airport_id=b,origin_iata='SVO' if day%2 else 'LED',destination_iata='LED' if day%2 else 'SVO',
                departure_precision='date',identity_status='accepted',resolution_status='resolved',
                participation_status='reported_only',graph_eligible=False))
    for table,rows in [('persons',persons),('segments',segments)]:
        pq.write_table(pa.Table.from_pylist(rows,schema=SCHEMAS[table]),root/f'{table}.parquet')
    manifest={'version':'resolution:1.0','files':{f'{n}.parquet':fingerprint(root/f'{n}.parquet') for n in ['persons','segments']}}
    (root/'resolution_manifest.json').write_text(json.dumps(manifest))
    return root


def config(tmp_path):
    data=load(ROOT/'configs/product.yaml')
    data['calibration']['mode']='fixed'
    data['criteria']['M01']['threshold']=6.0
    data['criteria']['M02']['threshold']=.2
    data['coverage']['min_window_days']=1
    data['forest']['enabled']=False
    data['criteria']['M03']['threshold']=.5
    p=tmp_path/'config.yaml'
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def product(tmp_path):
    source=tables(tmp_path)
    output=tmp_path/'product'
    cfg=config(tmp_path)
    before={p.name:fingerprint(p) for p in source.iterdir()}
    report=run(source,output,cfg,ROOT/'data/reference/airports.csv')
    assert before=={p.name:fingerprint(p) for p in source.iterdir()}
    return output,report,source,cfg


def test_source_build_evidence_weight_and_repeatability(product):
    output,report,source,cfg=product
    assert report['data_mode']=='source_dataset'
    assert report['counts']=={'persons':3,'segments':24,'top50':3}
    store=ProductStore(output)
    top=store.top()
    assert [r['person_id'] for r in top]==['person_000','person_001','person_002']
    assert [r['rank'] for r in top]==[1,2,3]
    p=store.profile('person_000')
    assert p['person']['matched_criteria_count']==3
    # M01: 30 trips/month, /6=5 capped to3; M02 1/.2 capped3 *1.5;
    # M03 .75/.5=1.5. Three distinct groups, sum9.
    assert p['person']['rule_score']==9
    assert p['person']['rule_score']==sum(f['contribution'] for f in p['factors'])
    assert p['route_balance']['total']==8
    all_ids={s['segment_id'] for s in p['segments']}
    assert all(set(f['evidence_segment_ids'])<=all_ids for f in p['factors'])
    assert all(len(f['evidence_segment_ids'])==8 for f in p['factors'])
    assert run(source,output,cfg,ROOT/'data/reference/airports.csv')['run_id']==report['run_id']
    assert report['forest_status']=='disabled'
    assert json.loads((output/'evaluation.json').read_text())['quality_metrics'] is None
    with pytest.raises(KeyError):store.profile('unknown')


def test_reject_synthetic_bare_and_tampered_input(tmp_path):
    source=tables(tmp_path)
    (source/'resolution_manifest.json').unlink()
    with pytest.raises(ValueError,match='completed T13'):verify(source)
    (source/'persons.parquet').write_text('version https://git-lfs.github.com/spec/v1\n')
    with pytest.raises(ValueError,match='git lfs pull'):verify(source)
    with pytest.raises(SystemExit) as e:main(['train','--demo'])
    assert e.value.code==2


def test_corruption_detected_and_existing_output_preserved(product):
    output,report,source,cfg=product
    before=fingerprint(output/'tables/top50.parquet')
    with (output/'tables/top50.parquet').open('ab') as stream:stream.write(b'corrupt')
    with pytest.raises(ValueError,match='checksum'):ProductStore(output)
    with pytest.raises(ValueError):run(source,output,cfg,ROOT/'data/reference/airports.csv')
    assert fingerprint(output/'tables/top50.parquet')!=before
    assert (output/'run_manifest.json').is_file()


def test_null_dates_routes_and_ambiguous_identity_never_inflate(tmp_path):
    root=tables(tmp_path)
    segments=pq.read_table(root/'segments.parquet').to_pylist()
    persons=pq.read_table(root/'persons.parquet').to_pylist()
    persons[2]['identity_status']='ambiguous'
    for s in segments:
        if s['person_id']=='person_001':s['origin_airport_id']=None
        if s['person_id']=='person_000' and s['departure_date']=='2017-01-04':s['departure_date']=None
    db=duckdb.connect()
    db.register('persons',pa.Table.from_pylist(persons,schema=SCHEMAS['persons']))
    db.register('segments',pa.Table.from_pylist(segments,schema=SCHEMAS['segments']))
    cfg=load(config(tmp_path))
    _features(db,cfg,{'days':8})
    assert db.execute("SELECT status FROM explanations WHERE person_id='person_000' AND criterion_id='M02'").fetchone()[0]=='unavailable'
    assert db.execute("SELECT status FROM explanations WHERE person_id='person_001' AND criterion_id='M03'").fetchone()[0]=='unavailable'
    assert db.execute("SELECT sum(contribution) FROM explanations WHERE person_id='person_002'").fetchone()[0]==0
    assert db.execute('SELECT count(*) FROM top50').fetchone()[0]==0
    db.close()


def test_graph_requires_observed_time_and_keeps_all_map_rows(tmp_path):
    root=tables(tmp_path)
    rows=pq.read_table(root/'segments.parquet').to_pylist()
    for row in rows:
        row['graph_eligible']=True
        row['flight_number']='SU100'
    db=duckdb.connect()
    db.register('segments',pa.Table.from_pylist(rows,schema=SCHEMAS['segments']))
    result=_graph(db)
    assert result['events']==0 and result['repeated_pairs']==0
    assert db.execute('SELECT count(*) FROM segments').fetchone()[0]==24
    db.close()
    for row in rows:row['departure_utc']=row['departure_date']+'T12:00:00+00:00'
    # Duplicate participation for one profile must not count as another person.
    db=duckdb.connect()
    db.register('segments',pa.Table.from_pylist(rows+[rows[0]],schema=SCHEMAS['segments']))
    result=_graph(db)
    assert result['events']==8 and result['repeated_pairs']==3
    assert db.execute('SELECT DISTINCT shared_days FROM graph_edges').fetchall()==[(8,)]
    db.close()


def test_top50_boundaries_and_determinism(tmp_path):
    source=tables(tmp_path,people=53)
    cfg=config(tmp_path)
    output=tmp_path/'product'
    run(source,output,cfg,ROOT/'data/reference/airports.csv')
    store=ProductStore(output)
    assert len(store.top())==50 and store.top()[-1]['rank']==50
    assert [r['person_id'] for r in store.top()]==[f'person_{i:03d}' for i in range(50)]
    for row in store.top():assert store.profile(row['person_id'])['person']['person_id']==row['person_id']
    with pytest.raises(ValueError):store.top(51)


def test_actual_http_api_static_and_isolated_profile(product):
    store=ProductStore(product[0])
    with ThreadingHTTPServer(('127.0.0.1',0),handler(store)) as server:
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            base=f'http://127.0.0.1:{server.server_port}'
            with urlopen(base+'/') as response:
                assert response.status==200 and 'script-src' in response.headers['Content-Security-Policy']
                assert 'YoYoFlot' in response.read().decode()
            with urlopen(base+'/api/profile?id=person_001') as response:
                p=json.load(response)
                assert all(s['person_id']=='person_001' for s in p['segments'])
            for route,status in [('/api/profile?id=bad',404),('/api/profile',400),('/../secret',404)]:
                with pytest.raises(HTTPError) as e:urlopen(base+route)
                assert e.value.code==status
            with urlopen(base+'/world.geojson') as response:assert len(json.load(response)['features'])>100
        finally:
            server.shutdown()
            thread.join(timeout=3)


def test_strict_config_rejects_nan_and_duplicates(tmp_path):
    cfg=config(tmp_path)
    text=cfg.read_text()
    cfg.write_text(text+'\nversion: product-1.0\n')
    with pytest.raises(ValueError,match='Duplicate'):load(cfg)
    cfg.write_text(text.replace('threshold: 6.0','threshold: .nan'))
    with pytest.raises(ValueError,match='threshold'):load(cfg)


def test_empirical_calibration_uses_source_profiles_and_records_reference(tmp_path):
    root=tables(tmp_path,people=60)
    cfg_path=config(tmp_path)
    cfg=load(cfg_path)
    cfg['calibration']={'mode':'empirical_quantile','quantile':.95,'min_reference_rows':5}
    cfg_path.write_text(yaml.safe_dump(cfg))
    output=tmp_path/'product'
    run(root,output,cfg_path,ROOT/'data/reference/airports.csv')
    rules=json.loads((output/'rules.json').read_text())
    assert all(r['reference_size']>=5 and r['threshold_method']=='empirical_quantile' for r in rules)
    assert next(r for r in rules if r['criterion_id']=='M01')['threshold']==30.0
    assert next(r for r in rules if r['criterion_id']=='M02')['threshold']==1.0
    assert next(r for r in rules if r['criterion_id']=='M03')['threshold']==.75


def test_isolation_forest_roles_percentiles_combination_and_determinism(tmp_path):
    from yoyoflot.product.build import _forest
    # Authored numeric controls exercise the model; no test row enters the app.
    rows=[dict(person_id=f'p{i}',trip_rate_30_days=(i%17)/5,quick_return_share=(i%7)/7,
               repeated_route_share=(i%11)/11,matched_criteria_count=i%4,rule_score=float(i%23),
               available_criteria_count=3) for i in range(240)]
    cfg=load(ROOT/'configs/product.yaml')['forest']
    predictions=[]
    for index in range(2):
        folder=tmp_path/str(index);folder.mkdir()
        with duckdb.connect() as db:
            db.register('rankings',pa.Table.from_pylist(rows if index==0 else rows[::-1]))
            result=_forest(db,cfg,folder)
        assert result['status']=='complete' and result['training_rows']>=32
        table=pq.read_table(folder/'if_scores.parquet').to_pylist()
        assert len(table)==240
        for r in table:
            assert 0<=r['if_percentile']<=1 and 0<=r['rules_percentile']<=1
            assert r['combined_score']==.5*r['if_percentile']+.5*r['rules_percentile']
        assert {r['role'] for r in table}=={'train','calibration','holdout'}
        predictions.append(table)
    assert predictions[0]==predictions[1]


def test_interruption_and_concurrency_preserve_output(tmp_path,monkeypatch):
    from yoyoflot.product import build
    from yoyoflot.normalize.locking import OutputLock
    source=tables(tmp_path)
    cfg=config(tmp_path)
    target=tmp_path/'product'
    with OutputLock(tmp_path/'.product.lock'):
        with pytest.raises(RuntimeError):run(source,target,cfg,ROOT/'data/reference/airports.csv')
    def interrupt(*args,**kwargs):raise KeyboardInterrupt
    monkeypatch.setattr(build,'_features',interrupt)
    with pytest.raises(KeyboardInterrupt):run(source,target,cfg,ROOT/'data/reference/airports.csv')
    assert not target.exists() and not list(tmp_path.glob('*.partial'))


def test_empty_source_is_empty_top_without_fallback(tmp_path):
    root=tables(tmp_path,people=0)
    output=tmp_path/'product'
    result=run(root,output,config(tmp_path),ROOT/'data/reference/airports.csv')
    assert result['counts']=={'persons':0,'segments':0,'top50':0}
    assert ProductStore(output).top()==[]


def test_independent_verifier_runs_without_generation_or_model_loading(product, tmp_path):
    import subprocess
    import sys
    report=tmp_path/'audit.json'
    command=[sys.executable,str(ROOT/'scripts/verify_source_product.py'),'--product',str(product[0]),'--report',str(report)]
    result=subprocess.run(command,capture_output=True,text=True,encoding='utf-8')
    assert result.returncode==0,result.stderr
    audit=json.loads(report.read_text())
    assert audit['status']=='passed' and audit['profiles_independently_checked']==3
    assert audit['segments_checked']==24 and audit['population_scores_consistent']
