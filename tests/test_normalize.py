"""T12 semantics: source-specific dates, scoped IDs, exact rereads and lineage."""
import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.contracts import SCHEMAS as INPUT_SCHEMAS
from yoyoflot.normalize.canonical import build
from yoyoflot.normalize.values import Rules

CONFIG=Path(__file__).resolve().parents[1]/'configs/normalization.yaml'


@pytest.fixture
def rules():return Rules(CONFIG)


@pytest.mark.parametrize('raw,expected',[(None,None),('',None),(' N/A ',None),('Not presented',None),
    (' None ',None),('0','0'),('000123','000123'),('  Ａ１２３  ','A123')])
def test_empty_and_strings(rules,raw,expected):assert rules.clean(raw)==expected


@pytest.mark.parametrize('raw,expected,latin',[
    ('  ИВАН\u00a0Иванов  ','иван иванов','ivan ivanov'),
    ('Ёлкин Щукин','ёлкин щукин','yolkin shchukin'),
    ('José  García','josé garcía','jose garcia'),
    ('Е\u0308ЛКИН','ёлкин','yolkin'),(None,None,None)])
def test_names(rules,raw,expected,latin):
    assert rules.name(raw)==expected and rules.transliterate(expected)==latin


@pytest.mark.parametrize('source,raw,expected',[
    ('boarding_csv','02/03/1980','1980-02-03'),
    ('sirena_fixed','02/03/1980','1980-03-02'),
    ('boarding_csv','13/02/1980',None),
    ('boarding_csv','02/30/1980',None),
    ('pointz_xml','1980-02-03','1980-02-03'),
    ('pointz_xml','1980-02',None)])
def test_source_dates(rules,source,raw,expected):
    value,precision,error=rules.date_value(source,'birth_date',raw,None)
    assert value==expected
    assert bool(error)==(expected is None)


def test_date_only_stays_date(rules):
    assert rules.date_value('forum_json','departure','2017-01-02','2017-01-02','date')==('2017-01-02','date',None)
    assert rules.date_value('forum_json','departure','2017-01-02','2017-01-02T00:00','minute')[0] is None


def test_reader_conflict_is_visible(rules):
    value,precision,reason=rules.date_value('boarding_csv','birth_date','02/03/1980','1980-03-02')
    assert value is None and reason=='raw_reader_date_conflict'


def test_identifiers_keep_zero_letters_and_internal_spaces(rules):
    value,programme,namespace,match,status=rules.identifier('csv','document','  00AB 123  ',None)
    assert value=='00AB 123' and json.loads(namespace)==['document','source','csv']
    assert rules.identifier('xml','document','00AB 123',None)[3]!=match
    a=rules.identifier('xml','loyalty_card','000123','FB')
    b=rules.identifier('yaml','loyalty_card','000123','fb')
    c=rules.identifier('yaml','loyalty_card','000123','SU')
    assert a[3]==b[3] and a[3]!=c[3]
    unknown=rules.identifier('xml','loyalty_card','000123',None)
    assert unknown[0]=='000123' and unknown[3] is None and unknown[4]=='unknown_programme'


def base(source='boarding_csv',loc='line:1'):
    return dict(source_id=source,source_record_id='r:'+source+loc,locator=loc,raw_json='{"original":true}')


def person(pid='p1',source='boarding_csv',**fields):
    return {**base(source), 'observation_id':pid, 'name_raw':'  ИВАН Иванов ',
            'name_normalized':'иван иванов',**fields}


def travel(tid='t1',pid='p1',source='boarding_csv',**fields):
    return {**base(source,'travel:'+tid),'travel_id':tid,'observation_id':pid,
            'departure_raw':'2017-01-02','departure_local':'2017-01-02','departure_precision':'date',
            'destination_raw':'SVO','flight_number':'AB001',**fields}


def identifier(iid='i1',pid='p1',source='boarding_csv',**fields):
    return {**base(source,'id:'+iid),'identifier_id':iid,'observation_id':pid,
            'kind':'loyalty_card','programme':'FB','value':'000123',**fields}


def write_input(path,people=None,identifiers=None,travels=None,schedules=None,extra_schema=None):
    path.mkdir(parents=True,exist_ok=True)
    data={'person_observations':people or [],'identifiers':identifiers or [],
          'travel_observations':travels or [],'schedules':schedules or [],'errors':[]}
    provenance=[]
    for table,rows in data.items():
        pk={'person_observations':'observation_id','identifiers':'identifier_id','travel_observations':'travel_id','schedules':'schedule_id','errors':'error_id'}[table]
        for row in rows:
            provenance.append({**{k:row[k] for k in ('source_id','source_record_id','locator')},'entity_table':table,'entity_id':row[pk],'transformation':'reader:1.0'})
    data['provenance']=provenance
    for table,rows in data.items():
        schema=extra_schema.get(table,INPUT_SCHEMAS[table]) if extra_schema else INPUT_SCHEMAS[table]
        pq.write_table(pa.Table.from_pylist(rows,schema=schema),path/(table+'.parquet'))
    (path/'source_report.json').write_text(json.dumps(dict(source_id=(people or [{}])[0].get('source_id','boarding_csv'),status='complete',counts={t:len(r) for t,r in data.items()})))


def read(out,name):return pq.read_table(out/(name+'.parquet')).to_pylist()


def test_build_preserves_sources_and_new_comparison_fields(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person(birth_date_raw='02/03/1980',birth_date='1980-02-03')],[identifier()],[travel()])
    report,summary=build([inp],out,CONFIG)
    p=read(out,'observations')[0]
    assert p['name_raw']=='  ИВАН Иванов ' and p['name_latin_comparison']=='ivan ivanov'
    assert p['birth_date']=='1980-02-03' and p['raw_json']=='{"original":true}'
    t=read(out,'travel_observations')[0]
    assert t['departure_local']=='2017-01-02' and t['departure_precision']=='date'
    assert 'departure_utc' not in t
    assert read(out,'identifiers')[0]['value_normalized']=='000123'
    assert report['counts']['observations']==1 and not report['all_personal_sources_present']
    assert report['status']=='complete' and 'Итог T12' in summary
    provenance=read(out,'provenance')
    assert any(r['entity_table']=='observations' for r in provenance)


def test_exact_reread_is_removed_but_distinct_trips_retained(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person()],[identifier()],[travel('t1'),travel('t2')])
    report,_=build([inp,inp],out,CONFIG,batch_size=1)
    assert report['counts']['travel_observations']==2
    assert {r['travel_id'] for r in read(out,'travel_observations')}=={'t1','t2'}
    assert sum(b['duplicates_removed'] for b in report['balances'])>0
    assert all(b['input_rows']==b['output_rows']+b['duplicates_removed'] for b in report['balances'])
    assert read(out,'duplicate_reads')


def test_different_sources_not_merged(tmp_path):
    a,b,out=tmp_path/'a',tmp_path/'b',tmp_path/'output'
    write_input(a,[person('pa','boarding_csv')],[],[travel('ta','pa','boarding_csv')])
    write_input(b,[person('pb','forum_json')],[],[travel('tb','pb','forum_json')])
    report,_=build([a,b],out,CONFIG)
    assert report['counts']['observations']==2 and report['counts']['travel_observations']==2


def test_conflicting_same_key_keeps_previous_result(tmp_path):
    a,b,out=tmp_path/'a',tmp_path/'b',tmp_path/'output'
    write_input(a,[person()]);build([a],out,CONFIG)
    before=(out/'canonical_manifest.json').read_bytes()
    write_input(b,[person(name_raw='Другой человек')])
    with pytest.raises(ValueError,match='conflicting copies'):build([a,b],out,CONFIG)
    assert (out/'canonical_manifest.json').read_bytes()==before


def test_invalid_date_nulls_one_field_without_losing_person(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person(birth_date_raw='13/02/1980',birth_date=None)])
    report,_=build([inp],out,CONFIG)
    p=read(out,'observations')[0]
    assert p['birth_date'] is None and p['birth_date_raw']=='13/02/1980'
    assert p['observation_id']=='p1' and p['normalization_status']=='invalid_fields'
    assert report['normalization_errors']==1
    assert read(out,'normalization_errors')[0]['field']=='birth_date'


def test_incompatible_numeric_identifier_stays_unresolved(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    fields=[pa.field(f.name,pa.int64() if f.name=='value' else f.type,nullable=f.nullable) for f in INPUT_SCHEMAS['identifiers']]
    write_input(inp,[person()],[identifier(value=123)],extra_schema={'identifiers':pa.schema(fields)})
    report,_=build([inp],out,CONFIG)
    i=read(out,'identifiers')[0]
    assert i['value'] is None and i['value_normalized'] is None
    assert json.loads(i['source_values_json'])['value']==123
    assert report['counts']['observations']==1
    assert any(r['reason']=='incompatible_type' for r in read(out,'normalization_errors'))


def test_namespace_override_requires_evidence(tmp_path):
    cfg=yaml.safe_load(CONFIG.read_text())
    cfg['namespace_overrides']=[dict(source_id='boarding_csv',kind='document',namespace='passport')]
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='evidence'):Rules(path)


def test_manual_correction_is_configured_and_auditable(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person()],[identifier()])
    cfg=yaml.safe_load(CONFIG.read_text())
    cfg['manual_corrections']=[dict(rule_id='verified_card',source_id='boarding_csv',entity_table='identifiers',entity_id='i1',field='value_normalized',original='000123',replacement='000124',reason='Verified typo',evidence='sheet:1 row:4')]
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    build([inp],out,path)
    i=read(out,'identifiers')[0]
    assert i['value']=='000123' and i['value_normalized']=='000124'
    assert json.loads(i['normalization_rules_json'])['value_normalized']=='verified_card'
    log=[r for r in read(out,'transformation_log') if r['rule_id']=='verified_card'][0]
    assert json.loads(log['examples_json'])[0]['after']=='000124'


def test_unmatched_manual_rule_fails(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person()])
    cfg=yaml.safe_load(CONFIG.read_text())
    cfg['manual_corrections']=[dict(rule_id='bad_id',source_id='boarding_csv',entity_table='observations',entity_id='absent',field='name_comparison',original='x',replacement='y',reason='Reviewed',evidence='record:1')]
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='did not match'):build([inp],out,path)


def test_t11_outputs_survive_and_t11_reads_canonical(tmp_path):
    from yoyoflot.normalize.geography import run
    root=Path(__file__).resolve().parents[1]
    inp,out=tmp_path/'input',tmp_path/'processed'
    write_input(inp,[person()],[],[travel()]);out.mkdir()
    (out/'geography_summary.txt').write_text('existing T11')
    build([inp],out,CONFIG)
    assert (out/'geography_summary.txt').read_text()=='existing T11'
    report,_=run([out],out,root/'data/reference/airports.csv',root/'configs/airports_overrides.yaml')
    assert report['counts']['travels']==1
    assert (out/'observations.parquet').exists()


def test_require_all_sources_and_cli_quiet(tmp_path,capsys):
    from yoyoflot.cli import main
    inp,out=tmp_path/'input',tmp_path/'output';write_input(inp,[person()])
    args=['build','--input',str(inp),'--output',str(out),'--config',str(CONFIG)]
    assert main(args+['--require-all'])==1
    capsys.readouterr()
    assert main(args+['-q'])==0
    assert capsys.readouterr().out==''
    assert 'Итог T12' in (out/'canonical_summary.txt').read_text(encoding='utf-8')


def test_orphan_is_visible_without_silently_dropping_trip(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person()],[],[travel(pid='missing')])
    report,_=build([inp],out,CONFIG)
    assert report['counts']['travel_observations']==1
    assert any(r['reason']=='orphan_reference' for r in read(out,'normalization_errors'))
    assert report['status']=='complete_with_errors'


def test_duplicate_journal_counts_accepted_records_only(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person(),person()],[identifier(),identifier()],[travel(),travel()])
    report,_=build([inp],out,CONFIG)
    assert report['counts']['observations']==1
    logs=read(out,'transformation_log')
    assert all(r['affected_records']==1 for r in logs)
    assert any(r['field']=='identifier_match_key' for r in logs)


def test_pdf_is_schedule_not_a_passenger(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    schedule={**base('timetable_pdf','page:1'),'schedule_id':'s1','flight_number':'AB123',
              'origin':'LHR','destination':'SVO','valid_from':'2018-01-01','valid_to':'2018-12-31','parse_status':'ok'}
    write_input(inp,[],[],[],[schedule])
    report,_=build([inp],out,CONFIG)
    assert report['counts']['observations']==0 and report['counts']['travel_observations']==0
    assert report['counts']['schedules']==1


def test_reversed_schedule_period_stays_unknown(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    schedule={**base('timetable_pdf','page:1'),'schedule_id':'s1','valid_from':'2018-12-31','valid_to':'2018-01-01'}
    write_input(inp,[],[],[],[schedule])
    build([inp],out,CONFIG)
    row=read(out,'schedules')[0]
    assert row['valid_from'] is None and row['valid_to'] is None
    assert json.loads(row['source_values_json'])['valid_from']=='2018-12-31'


def test_loyalty_identifier_cannot_be_assigned_to_another_profile(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output'
    write_input(inp,[person('p1'),person('p2')],[identifier(pid='p1')],[travel(pid='p2',loyalty_identifier_id='i1')])
    report,_=build([inp],out,CONFIG)
    assert any(e['reason']=='loyalty_identifier_context_mismatch' for e in read(out,'normalization_errors'))
    assert report['counts']['observations']==2


def test_unknown_input_column_is_recoverable(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output';write_input(inp,[person()])
    path=inp/'person_observations.parquet';table=pq.read_table(path)
    table=table.append_column('extra_note',pa.array([{'city':'Москва','number':'0001'}]))
    pq.write_table(table,path)
    build([inp],out,CONFIG)
    row=read(out,'observations')[0]
    assert json.loads(row['source_values_json'])['extra:extra_note']=={'city':'Москва','number':'0001'}


def test_repeated_run_and_batch_size_keep_canonical_values(tmp_path):
    inp,out=tmp_path/'input',tmp_path/'output';write_input(inp,[person()],[identifier()],[travel()])
    build([inp],out,CONFIG,batch_size=1)
    before={n:pq.read_table(out/(n+'.parquet')) for n in ('observations','identifiers','travel_observations','schedules','transformation_log')}
    build([inp],out,CONFIG,batch_size=20)
    assert all(t.equals(pq.read_table(out/(n+'.parquet'))) for n,t in before.items())


def test_no_incomplete_config_date_gets_default_year(tmp_path):
    cfg=yaml.safe_load(CONFIG.read_text());cfg['date_formats']['boarding_csv']['birth_date']=['%m/%d']
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='full year'):Rules(path)


def test_configured_namespace_can_be_shared_without_changing_value(tmp_path):
    cfg=yaml.safe_load(CONFIG.read_text())
    cfg['namespace_overrides']=[dict(source_id=s,kind='document',namespace='verified_passport_type_X',reason='Confirmed source column',evidence='schema review') for s in ('boarding_csv','sirena_fixed')]
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    r=Rules(path)
    assert r.identifier('boarding_csv','document','000123',None)[3]==r.identifier('sirena_fixed','document','000123',None)[3]


def test_manual_birth_date_precision_is_validated(tmp_path):
    cfg=yaml.safe_load(CONFIG.read_text())
    cfg['manual_corrections']=[dict(rule_id='fix',source_id='boarding_csv',entity_table='observations',entity_id='p1',field='birth_date',original=None,replacement='1980-01-02T00:00',reason='review',evidence='record')]
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    inp,out=tmp_path/'input',tmp_path/'output';write_input(inp,[person()])
    with pytest.raises(ValueError,match='canonical ISO'):build([inp],out,path)


def test_ctrl_c_preserves_previous_canonical_and_releases_lock(tmp_path,monkeypatch):
    from yoyoflot.normalize.canonical import Normalizer
    from yoyoflot.normalize.locking import OutputLock
    inp,out=tmp_path/'input',tmp_path/'output';write_input(inp,[person()])
    build([inp],out,CONFIG);before=(out/'canonical_manifest.json').read_bytes()
    def stop(*args,**kwargs):raise KeyboardInterrupt()
    monkeypatch.setattr(Normalizer,'row',stop)
    with pytest.raises(KeyboardInterrupt):build([inp],out,CONFIG)
    assert (out/'canonical_manifest.json').read_bytes()==before
    with OutputLock(out/'.canonical.lock',label='T12'):pass


def test_t11_uses_canonical_flight_number_without_changing_source(tmp_path):
    from yoyoflot.normalize.geography import run
    root=Path(__file__).resolve().parents[1]
    inp,out=tmp_path/'input',tmp_path/'output'
    schedule={**base('timetable_pdf','page:1'),'schedule_id':'s1','flight_number':'AB123','origin':'LHR','destination':'SVO',
              'valid_from':'2017-01-01','valid_to':'2017-12-31','days_of_week':'1234567','parse_status':'ok'}
    write_input(inp,[person()],[],[travel(flight_number=' ＡＢ１２３ ')],[schedule])
    build([inp],out,CONFIG)
    run([out],out,root/'data/reference/airports.csv',root/'configs/airports_overrides.yaml',schedules=[inp/'schedules.parquet'])
    row=read(out,'travel_geography')[0]
    assert row['origin_iata']=='LHR' and row['flight_number']==' ＡＢ１２３ '
    assert row['flight_number_normalized']=='AB123'


def test_t11_does_not_treat_invalid_location_as_missing(tmp_path):
    from yoyoflot.normalize.geography import run
    root=Path(__file__).resolve().parents[1]
    inp,out=tmp_path/'input',tmp_path/'output'
    schedule={**base('timetable_pdf','page:1'),'schedule_id':'s1','flight_number':'AB001','origin':'LHR','destination':'SVO',
              'valid_from':'2017-01-01','valid_to':'2017-12-31','days_of_week':'1234567','parse_status':'ok'}
    fields=[pa.field(f.name,pa.int64() if f.name=='origin_raw' else f.type,nullable=f.nullable) for f in INPUT_SCHEMAS['travel_observations']]
    write_input(inp,[person()],[],[travel(origin_raw=123)],[schedule],extra_schema={'travel_observations':pa.schema(fields)})
    build([inp],out,CONFIG)
    run([out],out,root/'data/reference/airports.csv',root/'configs/airports_overrides.yaml',schedules=[inp/'schedules.parquet'])
    row=read(out,'travel_geography')[0]
    assert row['origin_airport_id'] is None and row['origin_reason']=='incompatible_source_location'


def test_same_key_with_different_input_types_is_not_an_exact_reread(tmp_path):
    a,b,out=tmp_path/'a',tmp_path/'b',tmp_path/'output'
    def schema(typ):return pa.schema([pa.field(f.name,typ if f.name=='value' else f.type,nullable=f.nullable) for f in INPUT_SCHEMAS['identifiers']])
    write_input(a,[person()],[identifier(value=b'123')],extra_schema={'identifiers':schema(pa.binary())})
    write_input(b,[person()],[identifier(value={'bytes_hex':'313233'})],extra_schema={'identifiers':schema(pa.struct([('bytes_hex',pa.string())]))})
    with pytest.raises(ValueError,match='conflicting copies'):build([a,b],out,CONFIG)


def test_namespace_override_cannot_erase_card_programme(tmp_path):
    cfg=yaml.safe_load(CONFIG.read_text())
    cfg['namespace_overrides']=[dict(source_id='boarding_csv',kind='loyalty_card',namespace='all_cards',reason='test',evidence='record')]
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(cfg))
    with pytest.raises(ValueError,match='programme context'):Rules(path)
