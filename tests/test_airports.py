"""Semantic regressions for T11: no invented airports, dates or unique routes."""
import csv
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.contracts import SCHEMAS
from yoyoflot.normalize.airports import (AirportReference, ScheduleIndex, departure_date,
                                        enrich_travel, flight_key, local_to_utc)
from yoyoflot.normalize.geography import discover_inputs, run


@pytest.fixture
def reference(tmp_path):
    path = tmp_path/'airports.csv'
    fields = ['airport_id','name','city','country','iata','icao','latitude','longitude','timezone']
    records = [
        ['a','Airport A','Metro','Country','AAA','AAAA',55,37,'Europe/Moscow'],
        ['b','Airport B','Coast','Country','BBB','BBBB',56,38,'Europe/Moscow'],
        ['c','Airport C','Metro','Country','CCC','CCCC',57,39,'Europe/Moscow'],
        ['d','Airport D','Coast','Other','DDD','DDDD',58,40,'Europe/London'],
        ['j','JFK Airport','New York','USA','JFK','KJFK',40,-73,'America/New_York'],
        ['l','Airport L','Village','Country','LLL','LLLL',59,41,''],
    ]
    with path.open('w',newline='',encoding='utf-8') as f:
        w=csv.writer(f);w.writerow(fields);w.writerows(records)
    return AirportReference(path)


def travel(**extra):
    return dict(source_id='csv',source_record_id='r',locator='line:2',travel_id='t',
                observation_id='p',origin_raw=None,destination_raw='BBB',flight_number='AB123',
                departure_local='2017-01-02',departure_precision='date',**extra)


def schedule(sid='s', **extra):
    return dict(dict(schedule_id=sid,source_id='pdf',source_record_id='sr'+sid,locator='page:1/'+sid,
                flight_number='AB123',origin='AAA',destination='BBB',valid_from='2017-01-01',
                valid_to='2017-12-31',days_of_week='1234567',departure_local='23:30',
                arrival_local='01:00',arrival_day_offset=1,parse_status='ok'),**extra)


def enrich(reference,tmp_path,row,schedules):
    idx=ScheduleIndex(tmp_path/'index.sqlite',reference)
    try:
        for s in schedules:idx.add(s)
        idx.commit()
        return enrich_travel(row,reference,idx)
    finally:
        idx.close()


@pytest.mark.parametrize('value,status,kind,code',[
    (' aaa ','exact','airport','AAA'),('AAAA','exact','airport','AAA'),
    ('Airport A','exact','airport','AAA'),('Metro','ambiguous','city',None),
    ('Coast','ambiguous','city',None),('Village','exact','city','LLL'),
    ('Country','unresolved','country',None),('XYZ','unresolved','unknown',None),
    (None,'unresolved','missing',None),('','unresolved','missing',None),
    ('{"Airport":"AAA"}','exact','airport','AAA'),
    ('{"City":"Coast","Country":"Other"}','exact','city','DDD'),
    ('{"IATA":"AAA","Country":"Other"}','unresolved','unknown',None),
    ('{"IATA":"AAA","ICAO":"CCCC"}','unresolved','unknown',None),
])
def test_places(reference,value,status,kind,code):
    p=reference.resolve(value)
    assert (p['status'],p['kind'],p['iata'])==(status,kind,code)


def test_overrides_require_auditable_reason(reference,tmp_path):
    path=tmp_path/'rules.yaml'
    rule=dict(source_id='csv',original='Аэропорт А',target_iata='AAA',reason='Verified airport',evidence='record:1')
    path.write_text(yaml.safe_dump(dict(version='1.0',airport_overrides=[rule])),encoding='utf-8')
    ref=AirportReference(reference.path,path)
    assert ref.resolve('Аэропорт А','csv')['status']=='corrected'
    assert ref.resolve('Аэропорт А','xml')['status']=='unresolved'
    assert json.loads(ref.resolve('Аэропорт А','csv')['rule_id'])['evidence']=='record:1'
    del rule['reason']
    path.write_text(yaml.safe_dump(dict(version='1.0',airport_overrides=[rule])))
    with pytest.raises(ValueError):AirportReference(reference.path,path)


def country_alias_reference(reference, tmp_path, **changes):
    rule = dict(source_id='forum_json', original='Country Republic', target_country='Country',
                reason='Reviewed alternate country label', evidence='review:country-label')
    rule.update(changes)
    path = tmp_path/'country_aliases.yaml'
    path.write_text(yaml.safe_dump(dict(version='1.0', country_aliases=[rule])), encoding='utf-8')
    return AirportReference(reference.path, path)


@pytest.mark.parametrize('field', ['Airport', 'airport', 'IATA', 'ICAO', 'AirportCode'])
def test_country_alias_keeps_explicit_code_and_records_provenance(reference, tmp_path, field):
    ref = country_alias_reference(reference, tmp_path)
    raw = json.dumps({field: 'AAAA' if field == 'ICAO' else 'AAA',
                      'Country': 'Country Republic'})
    place = ref.resolve(raw, 'forum_json')
    assert (place['status'], place['iata'], place['country']) == ('corrected', 'AAA', 'Country')
    assert place['raw'] == raw and place['reason'] == 'verified_country_alias'
    rule = json.loads(place['rule_id'])['country_aliases'][0]
    assert rule['original'] == 'Country Republic' and rule['target_country'] == 'Country'
    assert rule['reason'] and rule['evidence']
    assert ref.resolve(raw, 'boarding_csv')['airport_id'] is None


def test_country_alias_does_not_guess_or_override_conflicts(reference, tmp_path):
    ref = country_alias_reference(reference, tmp_path)
    for obj in [dict(City='Metro', Country='Country Republic'),
                dict(City='Village', Country='Country Republic'),
                dict(Airport='DDD', Country='Country Republic'),
                dict(IATA='AAA', ICAO='CCCC', Country='Country Republic'),
                dict(IATA='AAA', City='Coast', Country='Country Republic'),
                dict(Airport='AAA', Country='Country Republic', country='Other')]:
        assert ref.resolve(json.dumps(obj), 'forum_json')['airport_id'] is None


@pytest.mark.parametrize('field', ['Airport', 'IATA'])
def test_blank_country_is_missing_context(reference, field):
    for value in ['', '  ', None]:
        place = reference.resolve(json.dumps({field: 'AAA', 'Country': value}))
        assert (place['status'], place['iata']) == ('exact', 'AAA')


@pytest.mark.parametrize('change', [dict(reason=''), dict(evidence=''),
                                  dict(target_country='Absent'), dict(original='Other')])
def test_country_aliases_reject_invalid_or_contradictory_rules(reference, tmp_path, change):
    with pytest.raises(ValueError):
        country_alias_reference(reference, tmp_path, **change)


def test_country_aliases_reject_duplicates(reference, tmp_path):
    ref = country_alias_reference(reference, tmp_path)
    path = tmp_path/'country_aliases.yaml'
    config = yaml.safe_load(path.read_text())
    config['country_aliases'] *= 2
    path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError, match='Duplicate country alias'):
        AirportReference(ref.path, path)


def test_reviewed_reference_country_variant_preserves_literal_match(reference, tmp_path):
    ref = country_alias_reference(reference, tmp_path, original='Other', reference_name_variant=True)
    literal = ref.resolve('{"Airport":"DDD","Country":"Other"}', 'forum_json')
    alternate = ref.resolve('{"Airport":"AAA","Country":"Other"}', 'forum_json')
    assert (literal['status'], literal['iata'], literal['rule_id']) == ('exact', 'DDD', None)
    assert (alternate['status'], alternate['iata']) == ('corrected', 'AAA')


def test_country_alias_cannot_choose_between_duplicate_reference_codes(reference, tmp_path):
    records = list(csv.DictReader(reference.path.open(encoding='utf-8')))
    records[3]['iata'] = 'AAA'
    path = tmp_path/'duplicate_codes.csv'
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0]))
        writer.writeheader(); writer.writerows(records)
    ref = country_alias_reference(AirportReference(path), tmp_path)
    place = ref.resolve('{"Airport":"AAA","Country":"Country Republic"}', 'forum_json')
    assert place['airport_id'] is None


@pytest.mark.parametrize('obj,expected', [
    (dict(City='Moscow', Airport='SVO', Country='Russian Federation'), 'SVO'),
    (dict(City='Saint Petersburg', Airport='LED', Country='Russian'), 'LED'),
    (dict(City='Atlanta GA', Airport='ATL', Country='United States of'), 'ATL'),
    (dict(City='Boise ID', Airport='BOI', Country='United States of America'), 'BOI'),
    (dict(City='Madrid', Airport='MAD', Country='Spain and Canary Islands'), 'MAD'),
    (dict(City='Barcelona', Airport='BCN', Country='Spain and Canary'), 'BCN'),
    (dict(City='Palma De Mallorca', Airport='PMI', Country='Spain and'), 'PMI'),
    (dict(City='Mandalay', Airport='MDL', Country='Myanmar'), 'MDL'),
    (dict(City='Vientiane', Airport='VTE', Country="Lao People's"), 'VTE'),
    (dict(City='Providenciales', Airport='PLS', Country='Turks and Caicos'), 'PLS'),
    (dict(City='Samarkand International', Airport='SKD', Country=''), 'SKD'),
    (dict(City='Seoul', Airport='rea', Country='Ko'), None),
    (dict(City='Chisinau', Airport='ova', Country='Mold'), None),
    (dict(City='Kinshasa', Airport='ngo', Country='Co'), None),
    (dict(City='Qinhuangdao', Airport='BPE', Country='China'), None),
    (dict(City='Debre Tabor', Airport='DTB', Country='Ethiopia'), None),
    (dict(City='Asheville/Hendersonv. NC', Airport='AVL', Country='United'), None),
    (dict(City='Moscow', Airport='SVO', Country='United States of'), None),
])
def test_reviewed_real_country_context(obj, expected):
    root = Path(__file__).resolve().parents[1]
    ref = AirportReference(root/'data/reference/airports.csv', root/'configs/airports_overrides.yaml')
    place = ref.resolve(json.dumps(obj), 'forum_json')
    assert place['iata'] == expected
    if expected and obj['Country']:
        assert place['status'] == 'corrected' and place['rule_id']


@pytest.mark.parametrize('raw,expected', [('AB 00123','AB123'),('AB123*','AB123'),('XY123','XY123'),
                                         ('123',None),('1',None),('*',None),('AB12A','AB12A')])
def test_flight_numbers(raw,expected):
    assert flight_key(raw)==expected


def test_unique_schedule_and_midnight_provenance(reference,tmp_path):
    row=travel()
    out,links=enrich(reference,tmp_path,row,[schedule()])
    assert out['origin_iata']=='AAA' and out['origin_status']=='inferred_from_schedule'
    assert out['arrival_effective_local']=='2017-01-03T01:00'
    assert out['departure_utc']=='2017-01-02T20:30:00Z'
    assert out['arrival_utc']=='2017-01-02T22:00:00Z'
    assert out['origin_raw'] is None and out['departure_local']=='2017-01-02'
    assert links[0]['schedule_locator']=='page:1/s'
    assert links[0]['schedule_source_record_id']=='srs'
    assert json.loads(links[0]['filled_fields_json'])['origin_airport_id']=='a'


def test_multiple_routes_remain_ambiguous(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(),[schedule('a'),schedule('c',origin='CCC')])
    assert out['origin_airport_id'] is None and out['schedule_match_status']=='ambiguous'
    assert len(links)==2 and {l['status'] for l in links}=={'ambiguous'}
    assert all(json.loads(l['filled_fields_json'])=={} for l in links)


def test_multi_leg_same_number(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(),[schedule('first',destination='CCC'),schedule('second',origin='CCC')])
    assert out['origin_iata']=='CCC' and len(links)==1


@pytest.mark.parametrize('changes,status',[
    ({'valid_from':'2018-01-01','valid_to':'2018-12-31'},'reference_only'),
    ({'valid_from':None},'temporal_unknown'),({'valid_to':'2016-01-01'},'temporal_unknown'),
    ({'days_of_week':None},'temporal_unknown'),
])
def test_temporal_coverage(reference,tmp_path,changes,status):
    out,links=enrich(reference,tmp_path,travel(),[schedule(**changes)])
    assert out['origin_airport_id'] is None
    assert out['schedule_match_status']==status and links[0]['status']==status
    assert out['departure_effective_local']=='2017-01-02'


def test_unknown_period_candidate_defeats_uniqueness(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(),[schedule('one'),schedule('two',origin='CCC',valid_from=None)])
    assert out['origin_airport_id'] is None and {l['status'] for l in links}=={'ambiguous'}


def test_wrong_weekday(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(),[schedule(days_of_week='2')])
    assert not links and out['origin_airport_id'] is None


def test_different_carrier_not_same_flight(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(),[schedule(flight_number='XY123')])
    assert not links and out['origin_airport_id'] is None


def test_explicit_codeshare_retains_both_candidates(reference,tmp_path):
    row=travel(codeshare='XY123')
    out,links=enrich(reference,tmp_path,row,[schedule('market'),schedule('operating',flight_number='XY123',origin='CCC')])
    assert out['origin_airport_id'] is None and len(links)==2


def test_codeshare_star_is_not_operator(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(codeshare='1'),[schedule(flight_number='XY123')])
    assert not links


def test_unknown_place_not_ignored(reference,tmp_path):
    row=travel();row['destination_raw']='UNKNOWN'
    out,links=enrich(reference,tmp_path,row,[schedule()])
    assert not links and out['origin_airport_id'] is None


def test_unknown_observation_date(reference,tmp_path):
    row=travel();row['departure_local']='2017-01';row['departure_precision']=None
    out,links=enrich(reference,tmp_path,row,[schedule()])
    assert out['origin_airport_id'] is None and links[0]['status']=='temporal_unknown'


def test_unknown_day_offset_does_not_guess_arrival_date(reference,tmp_path):
    out,_=enrich(reference,tmp_path,travel(),[schedule(arrival_day_offset=None)])
    assert out['arrival_effective_local'] is None and out['arrival_utc'] is None


def test_conflicting_source_arrival_date(reference,tmp_path):
    row=travel(arrival_local='2017-01-02',arrival_precision='date')
    out,links=enrich(reference,tmp_path,row,[schedule()])
    assert not links and out['origin_airport_id'] is None


def test_clock_conflict(reference,tmp_path):
    row=travel();row.update(departure_local='2017-01-02T10:00',departure_precision='minute')
    out,links=enrich(reference,tmp_path,row,[schedule()])
    assert not links


def test_bad_schedule_row_not_used(reference,tmp_path):
    out,links=enrich(reference,tmp_path,travel(),[schedule(parse_status='row_without_section')])
    assert not links


@pytest.mark.parametrize('value,precision,zone,expected,status',[
    ('2021-11-07T01:30','minute','America/New_York',None,'ambiguous_local_time'),
    ('2021-03-14T02:30','minute','America/New_York',None,'nonexistent_local_time'),
    ('2021-11-07T01:30-04:00','minute','America/New_York','2021-11-07T05:30:00Z','converted'),
    ('2010-01-02T12:00','minute','Europe/Moscow','2010-01-02T09:00:00Z','converted'),
    ('2012-01-02T12:00','minute','Europe/Moscow','2012-01-02T08:00:00Z','converted'),
    ('2021-01-02','date','Europe/Moscow',None,'incomplete_datetime'),
    ('2021-01','date','Europe/Moscow',None,'incomplete_datetime'),
    ('2021-01-02T12:00','minute',None,None,'unknown_timezone'),
    ('2021-01-02T12:00','minute','No/Such_Zone',None,'unknown_timezone'),
])
def test_utc(value,precision,zone,expected,status):
    assert local_to_utc(value,precision,zone)==(expected,status)


def test_existing_timezone_conflict(reference,tmp_path):
    row=travel(departure_timezone='Europe/London');row.update(origin_raw='AAA',departure_local='2017-01-02T23:30',departure_precision='minute')
    out,_=enrich(reference,tmp_path,row,[])
    assert out['departure_utc'] is None and out['departure_utc_status']=='timezone_conflict'


def input_tables(path, travels, schedules):
    path.mkdir(parents=True,exist_ok=True)
    pq.write_table(pa.Table.from_pylist(travels,schema=SCHEMAS['travel_observations']),path/'travel_observations.parquet')
    pq.write_table(pa.Table.from_pylist(schedules,schema=SCHEMAS['schedules']),path/'schedules.parquet')
    (path/'source_report.json').write_text(json.dumps(dict(status='complete',counts={'schedules':len(schedules)})))


def test_country_alias_pipeline_keeps_raw_values_provenance_and_quality(reference, tmp_path):
    ref = country_alias_reference(reference, tmp_path)
    inp, out = tmp_path/'input', tmp_path/'output'
    row = travel()
    raw = '{"Airport":"AAA","Country":"Country Republic"}'
    row.update(source_id='forum_json', origin_raw=raw, destination_raw='BBB')
    input_tables(inp, [row], [])
    report, _ = run([inp], out, ref.path, overrides=tmp_path/'country_aliases.yaml')
    derived = pq.read_table(out/'travel_geography.parquet').to_pylist()[0]
    assert derived['origin_raw'] == raw and derived['origin_iata'] == 'AAA'
    assert derived['origin_status'] == 'corrected' and derived['geography_complete']
    assert derived['departure_utc'] is None and derived['departure_utc_status'] == 'incomplete_datetime'
    assert json.loads(derived['origin_override_json'])['country_aliases'][0]['evidence']
    counts = report['by_source']['forum_json']['counts']
    assert counts['places_exact'] == counts['places_corrected'] == 1
    assert counts['places_total'] == 2
    inventory = pq.read_table(out/'place_inventory.parquet').to_pylist()
    assert next(p for p in inventory if p['field'] == 'origin_raw')['status'] == 'corrected'


def test_pipeline_balances_lineage_and_preserves_t12(reference,tmp_path):
    inp,out=tmp_path/'input',tmp_path/'processed'
    input_tables(inp,[travel()],[schedule()])
    out.mkdir();(out/'identifiers.parquet').write_bytes(b'T12 untouched')
    report,_=run([inp],out,reference.path,batch_size=1)
    assert report['counts']['travels']==1 and report['counts']['schedule_links']==1
    assert sum(report['by_source']['csv']['fractions'].values())==1
    row=pq.read_table(out/'travel_geography.parquet').to_pylist()[0]
    assert row['geography_complete'] and row['time_complete'] and row['time_inferred']
    assert (out/'identifiers.parquet').read_bytes()==b'T12 untouched'
    links=pq.read_table(out/'schedule_links.parquet').to_pylist()
    assert links[0]['schedule_id']==row['schedule_id_used']
    assert len(pq.read_table(out/'place_inventory.parquet'))==8
    snapshot=pq.read_table(out/'travel_geography.parquet')
    run([inp],out,reference.path,batch_size=2)
    assert snapshot.equals(pq.read_table(out/'travel_geography.parquet'))
    from yoyoflot.normalize.locking import OutputLock
    with OutputLock(out/'.t11.lock'):
        pass


def test_duplicate_inputs_fail_preserving_previous_result(reference,tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    input_tables(inp,[travel()],[])
    run([inp],out,reference.path)
    previous=(out/'t11_manifest.json').read_bytes()
    input_tables(inp,[travel(),travel()],[])
    with pytest.raises(Exception,match='UNIQUE'):
        run([inp],out,reference.path)
    assert (out/'t11_manifest.json').read_bytes()==previous
    from yoyoflot.normalize.locking import OutputLock
    with OutputLock(out/'.t11.lock'):
        pass


def test_missing_and_overlapping_inputs(tmp_path):
    with pytest.raises(ValueError,match='No travel'):discover_inputs([tmp_path])
    input_tables(tmp_path,[travel()],[])
    with pytest.raises(ValueError,match='Overlapping'):discover_inputs([tmp_path,tmp_path])


def test_cli_quiet_and_summary(reference,tmp_path,capsys):
    from yoyoflot.cli import main
    inp=tmp_path/'input';input_tables(inp,[travel()],[])
    rules=tmp_path/'overrides.yaml';rules.write_text("version: '1.0'\nairport_overrides: []\n")
    args=['airports','--input',str(inp),'--output',str(tmp_path/'output'),
          '--reference',str(reference.path),'--overrides',str(rules)]
    assert main(args+['-q'])==0 and capsys.readouterr().out==''
    assert 'Итог T11' in (tmp_path/'output/geography_summary.txt').read_text(encoding='utf-8')
    assert main(args)==0 and 'Итог T11' in capsys.readouterr().out


def test_reference_manifest_checksum(reference,tmp_path):
    manifest=tmp_path/'manifest.json'
    manifest.write_text(json.dumps(dict(files={'airports.csv':'wrong'},reference_version='v')))
    with pytest.raises(ValueError,match='checksum'):AirportReference(reference.path,manifest=manifest)


@pytest.mark.parametrize('status',['incomplete', 'failed', None])
def test_unverified_schedule_coverage_blocks_inference(reference,tmp_path,status):
    inp,out=tmp_path/'input',tmp_path/'output'
    input_tables(inp,[travel()],[schedule()])
    if status is None:
        (inp/'source_report.json').unlink()
    else:
        (inp/'source_report.json').write_text(json.dumps(dict(status=status,counts={'schedules':1})))
    run([inp],out,reference.path)
    row=pq.read_table(out/'travel_geography.parquet').to_pylist()[0]
    assert row['origin_airport_id'] is None
    assert row['schedule_match_status']=='temporal_unknown'
    links=pq.read_table(out/'schedule_links.parquet').to_pylist()
    assert links[0]['reason']=='schedule_source_coverage_unknown'


def test_legacy_empty_lock_does_not_block(reference,tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    input_tables(inp,[travel()],[])
    out.mkdir();(out/'.t11.lock').touch()
    run([inp],out,reference.path)
    run([inp],out,reference.path)


def test_keyboard_interrupt_releases_lock_and_preserves_output(reference,tmp_path,monkeypatch):
    import yoyoflot.normalize.geography as geo
    from yoyoflot.normalize.locking import OutputLock
    inp,out=tmp_path/'input',tmp_path/'output'
    input_tables(inp,[travel()],[])
    run([inp],out,reference.path)
    previous=(out/'t11_manifest.json').read_bytes()
    real=geo.enrich_travel
    def interrupt(*args):raise KeyboardInterrupt()
    monkeypatch.setattr(geo,'enrich_travel',interrupt)
    with pytest.raises(KeyboardInterrupt):run([inp],out,reference.path)
    with OutputLock(out/'.t11.lock'):pass
    assert (out/'t11_manifest.json').read_bytes()==previous
    monkeypatch.setattr(geo,'enrich_travel',real)
    run([inp],out,reference.path)


def test_os_lock_excludes_another_process_and_survives_kill(tmp_path):
    import os,subprocess,sys
    from yoyoflot.normalize.locking import OutputLock
    path=tmp_path/'.t11.lock'
    code='''from pathlib import Path
import sys
from yoyoflot.normalize.locking import OutputLock
with OutputLock(Path(sys.argv[1])):
 print("locked",flush=True)
 sys.stdin.read()
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'src')+os.pathsep+os.environ.get('PYTHONPATH','')}
    proc=subprocess.Popen([sys.executable,'-c',code,str(path)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=env)
    try:
        assert proc.stdout.readline().strip()=='locked'
        with pytest.raises(RuntimeError,match='другим процессом'):
            with OutputLock(path):pass
        proc.kill();proc.wait(timeout=10)
        with OutputLock(path):pass
    finally:
        if proc.poll() is None:proc.kill();proc.wait(timeout=10)
        for stream in (proc.stdin,proc.stdout,proc.stderr):stream.close()


def test_cli_handles_ctrl_c(reference,tmp_path,monkeypatch,capsys):
    import yoyoflot.normalize.geography as geo
    from yoyoflot.cli import main
    def interrupt(*args,**kwargs):raise KeyboardInterrupt()
    monkeypatch.setattr(geo,'run',interrupt)
    assert main(['airports','--input',str(tmp_path)])==130
    out=capsys.readouterr()
    assert 'прервано' in out.err and 'Traceback' not in out.err


def test_long_run_progress_callback(reference,tmp_path,monkeypatch):
    import itertools
    from types import SimpleNamespace
    import yoyoflot.normalize.geography as geo
    inp,out=tmp_path/'input',tmp_path/'output'
    input_tables(inp,[travel()],[])
    clock=itertools.count(0,31)
    monkeypatch.setattr(geo,'time',SimpleNamespace(perf_counter=lambda:next(clock)))
    messages=[]
    geo.run([inp],out,reference.path,progress=messages.append)
    assert any('поездок' in m for m in messages)


def test_bulk_schedule_index_rejects_duplicate_ids(reference,tmp_path):
    index=ScheduleIndex(tmp_path/'index.sqlite',reference)
    try:
        index.add(schedule());index.add(schedule())
        with pytest.raises(Exception,match='UNIQUE'):
            index.commit()
    finally:
        index.close()


def test_route_filter_retains_all_airports_and_codeshares(reference,tmp_path):
    index=ScheduleIndex(tmp_path/'index.sqlite',reference)
    try:
        for s in (schedule('a'),schedule('c',origin='CCC'),schedule('d',origin='DDD'),
                  schedule('alias',flight_number='XY123',origin='CCC')):
            index.add(s)
        index.commit()
        result=index.candidates('AB123','XY123',('a','c'),('b',))
        assert {r['schedule_id'] for r in result}=={'a','c','alias'}
        assert index.evidence_json(result[0])==json.dumps(result[0],ensure_ascii=False,sort_keys=True,separators=(',',':'))
        index.candidates('AB123','XY123',('a','c'),('b',))
        assert index.cache_hits==1
    finally:index.close()


def test_candidate_cache_budget_and_eviction(reference,tmp_path):
    index=ScheduleIndex(tmp_path/'index.sqlite',reference)
    index.CACHE_BYTES=1;index.EVIDENCE_BYTES=1
    try:
        index.add(schedule());index.commit()
        first=index.candidates('AB123',None)
        second=index.candidates('AB123',None)
        assert first==second and index._cache_bytes<=1 and index._evidence_bytes<=1
        assert json.loads(index.evidence_json(first[0]))==first[0]
    finally:index.close()


def test_arrow_passthrough_keeps_raw_and_t12_columns(reference,tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    input_tables(inp,[travel()],[])
    source=pq.read_table(inp/'travel_observations.parquet')
    raw='{"untouched":"' + 'ю'*10000 + '"}'
    source=source.set_column(source.schema.get_field_index('raw_json'),'raw_json',pa.array([raw]))
    source=source.append_column('t12_notes',pa.array([['0000123',None,'  untouched  ']]))
    pq.write_table(source,inp/'travel_observations.parquet')
    run([inp],out,reference.path)
    derived=pq.read_table(out/'travel_geography.parquet')
    for name in source.column_names:
        assert derived[name].equals(source[name]),name


def test_invalid_source_arrival_is_not_repaired_as_missing(reference,tmp_path):
    row=travel(arrival_local=None,arrival_raw='invalid date',arrival_precision='invalid')
    out,_=enrich(reference,tmp_path,row,[schedule()])
    assert out['arrival_effective_local'] is None and out['arrival_utc'] is None
