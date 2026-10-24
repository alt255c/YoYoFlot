"""T13 regressions: conservative identity, transitivity, journeys and audit."""
import csv
import json
from array import array
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import yaml

from yoyoflot.contracts import SCHEMAS as READER_SCHEMAS, ID_COLUMNS
from yoyoflot.normalize.canonical import build
from yoyoflot.normalize.geography import run as geography
from yoyoflot.resolve.common import Rules
from yoyoflot.resolve.matching import Components, assess_pair, cluster_trips
from yoyoflot.resolve.pipeline import run
from yoyoflot.resolve.review import audit_to_file, FIELDS

ROOT=Path(__file__).resolve().parents[1]
CONFIG=ROOT/'configs/resolution.yaml'


def person(pid, source='boarding_csv', name='Ivan Ivanov', dob='1980-01-02'):
    return dict(observation_id=pid,source_id=source,source_record_id='record:'+pid,locator='person:'+pid,
                raw_json='{}',name_raw=name,name_normalized=name,birth_date_raw=dob,birth_date=dob)


def identifier(pid,value='000123',programme='FB',source='boarding_csv',kind='loyalty_card',iid=None):
    iid=iid or pid+':'+kind+':'+str(programme)+':'+value
    return dict(identifier_id=iid,observation_id=pid,source_id=source,source_record_id='record:'+iid,
                locator='id:'+iid,raw_json='{}',kind=kind,programme=programme,value=value)


def travel(tid,pid,source='boarding_csv',**changes):
    return dict(dict(travel_id=tid,observation_id=pid,source_id=source,source_record_id='record:'+tid,
                locator='travel:'+tid,raw_json='{}',event_kind='boarding_record',origin_raw='SVO',destination_raw='LED',
                flight_number='SU001',departure_raw='2017-01-02T10:30',departure_local='2017-01-02T10:30',
                departure_precision='minute'),**changes)


def input_snapshot(tmp_path,people,identifiers=(),travels=(),normalization=None):
    inp=tmp_path/'readers';inp.mkdir()
    data=dict(person_observations=people,identifiers=list(identifiers),travel_observations=list(travels),schedules=[],errors=[])
    data['provenance']=[{**{k:r[k] for k in ('source_id','source_record_id','locator')},'entity_table':t,
                        'entity_id':r[ID_COLUMNS[t]],'transformation':'reader:fixture'} for t,rs in data.items() for r in rs]
    for table,rows in data.items():pq.write_table(pa.Table.from_pylist(rows,schema=READER_SCHEMAS[table]),inp/(table+'.parquet'))
    norm=ROOT/'configs/normalization.yaml'
    if normalization:
        cfg=yaml.safe_load(norm.read_text(encoding='utf-8'));cfg.update(normalization)
        norm=tmp_path/'normalization.yaml';norm.write_text(yaml.safe_dump(cfg),encoding='utf-8')
    output=tmp_path/'canonical'
    report,_=build([inp],output,norm)
    assert report['normalization_errors']==0
    geography([output],output,ROOT/'data/reference/airports.csv',ROOT/'configs/airports_overrides.yaml')
    return output


def resolve(tmp_path,people,identifiers=(),travels=(),normalization=None,settings=None,**kwargs):
    inp=input_snapshot(tmp_path,people,identifiers,travels,normalization)
    cfg=CONFIG
    if settings:
        values=yaml.safe_load(CONFIG.read_text());values.update(settings)
        cfg=tmp_path/'resolution.yaml';cfg.write_text(yaml.safe_dump(values),encoding='utf-8')
    out=tmp_path/'resolved'
    report,summary=run(inp,out,cfg,review_output=tmp_path/'review',**kwargs)
    return report,out,inp


def read(path,table):return pq.read_table(path/(table+'.parquet')).to_pylist()


def test_aggregation_ranges_preserve_decisions_links_and_complete_evidence(tmp_path,monkeypatch):
    import yoyoflot.resolve.common as common
    people=[person('a'),person('b',source='forum_json'),person('c'),
            person('d',dob='1981-02-03'),person('e'),person('f'),person('g',name=None,dob=None)]
    ids=[identifier('a'),identifier('b',source='forum_json'),identifier('c','000456'),identifier('d','000456'),
         identifier('a','D01',kind='document'),identifier('a','D01',kind='document',iid='extra-document'),
         identifier('a','D02',kind='document')]
    trips=[travel('a1','a'),travel('a2','a'),travel('b1','b',source='forum_json'),
           travel('c1','c'),travel('d1','d'),travel('g1','g')]
    _,out,inp=resolve(tmp_path,people,ids,trips)
    expected={table:sorted(read(out,table),key=lambda r:json.dumps(r,sort_keys=True)) for table in common.SCHEMAS}
    assert {r['status'] for r in expected['person_pairs']}=={'accepted','ambiguous','rejected'}
    monkeypatch.setattr(common,'AGGREGATION_GROUPS',1)
    run(inp,out,CONFIG,review_output=tmp_path/'review',batch_size=1,memory_limit='128MB')
    actual={table:sorted(read(out,table),key=lambda r:json.dumps(r,sort_keys=True)) for table in common.SCHEMAS}
    assert actual==expected
    for pair in actual['person_pairs']:
        for side in ('left','right'):
            witness=json.loads(pair['evidence_json'])[side]
            if witness['observation_id']=='a':
                assert len(witness['identifiers'])==4
    assert len(actual['person_links'])==len(people)
    assert len(actual['segment_links'])==len(trips)


def test_similar_name_and_dob_are_only_auxiliary(tmp_path):
    r,out,_=resolve(tmp_path,[person('a'),person('b',source='forum_json')])
    assert r['counts']['persons']==2
    assert {p['status'] for p in read(out,'person_pairs')}=={'ambiguous'}
    assert not any(p['graph_eligible'] for p in read(out,'person_links'))


@pytest.mark.parametrize('right_value,right_programme,expect_merge',[
    ('000123','fb',True),('123','FB',False),('000123','SU',False),('000123',None,False)])
def test_card_namespace_and_leading_zeroes(tmp_path,right_value,right_programme,expect_merge):
    people=[person('a'),person('b',source='forum_json')]
    ids=[identifier('a'),identifier('b',right_value,right_programme,source='forum_json')]
    r,out,_=resolve(tmp_path,people,ids)
    assert (r['counts']['persons']==1)==expect_merge


def test_reliable_card_does_not_require_a_name(tmp_path):
    people=[person('a',name=None,dob=None),person('b',source='pointz_xml',name=None,dob=None)]
    r,out,_=resolve(tmp_path,people,[identifier('a'),identifier('b',source='pointz_xml')])
    assert r['counts']['persons']==1
    assert all(p['graph_eligible'] for p in read(out,'person_links'))
    assert read(out,'persons')[0]['display_name'] is None


def test_corrupted_shared_card_is_quarantined_before_a_star_merge(tmp_path):
    people=[person('a'),person('b',dob=None),person('c',dob='1990-01-02')]
    r,out,_=resolve(tmp_path,people,[identifier(p['observation_id']) for p in people])
    assert r['counts']['persons']==3
    assert r['person_pair_statuses'].get('rejected',0)>0
    assert not any(p['graph_eligible'] for p in read(out,'person_links'))
    assert 'conflicting_birth_dates' in {x['reason'] for x in read(out,'blocking_issues')}


def test_transitive_dob_conflict_cannot_disappear(tmp_path):
    people=[person('a'),person('b',dob=None),person('c',dob='1990-01-02')]
    ids=[identifier('a','01'),identifier('b','01'),identifier('b','02'),identifier('c','02')]
    r,out,_=resolve(tmp_path,people,ids)
    assert r['counts']['persons']==2
    assert 'component_birth_date_conflict' in {c['reason'] for c in read(out,'conflicts')}
    assert not any(p['graph_eligible'] for p in read(out,'person_links'))


def test_transitive_name_conflict_cannot_hide_behind_a_nameless_bridge(tmp_path):
    people=[person('a',name='Ivan Ivanov',dob=None),person('b',name=None,dob=None),
            person('c',name='Charlotte Wexford',dob=None)]
    ids=[identifier('a','01'),identifier('b','01'),identifier('b','02'),identifier('c','02')]
    r,out,_=resolve(tmp_path,people,ids)
    assert r['counts']['persons']==2
    assert 'component_name_conflict' in {c['reason'] for c in read(out,'conflicts')}
    assert not any(p['graph_eligible'] for p in read(out,'person_links'))


def test_name_variant_limit_does_not_discard_variants_silently(tmp_path):
    people=[person(str(i),name=f'Ivan Ivanov{i}',dob=None) for i in range(3)]
    r,out,_=resolve(tmp_path,people,[identifier(p['observation_id']) for p in people],settings=dict(max_name_variants=2))
    assert r['counts']['persons']==2
    assert 'component_name_variant_limit' in {c['reason'] for c in read(out,'conflicts')}


@pytest.mark.parametrize('name,status', [('Iwan Ivanov','accepted'),('Charlotte Wexford','rejected')])
def test_fuzzy_name_needs_independent_card_and_respects_conflict(tmp_path,name,status):
    r,out,_=resolve(tmp_path,[person('a'),person('b',name=name)],[identifier('a'),identifier('b')])
    assert read(out,'person_pairs')[0]['status']==status


def shared_document_namespaces(sources):
    return dict(namespace_overrides=[dict(source_id=s,kind='document',namespace='passport:fixture:issuer',
                reason='Synthetic verified type and issuer',evidence='test fixture') for s in sources])


def test_transitive_document_intersection_is_not_a_union(tmp_path):
    people=[person('a'),person('b',source='forum_json'),person('c',source='pointz_xml')]
    ids=[identifier('a','D1',kind='document'),identifier('b','D1',source='forum_json',kind='document'),
         identifier('b','D2',source='forum_json',kind='document'),identifier('c','D2',source='pointz_xml',kind='document')]
    r,out,_=resolve(tmp_path,people,ids,normalization=shared_document_namespaces([p['source_id'] for p in people]))
    assert r['counts']['persons']==2
    assert 'component_document_conflict' in {c['reason'] for c in read(out,'conflicts')}


def test_same_name_with_incompatible_documents_stays_separate(tmp_path):
    people=[person('a'),person('b')]
    ids=[identifier('a','D1',kind='document'),identifier('b','D2',kind='document')]
    r,out,_=resolve(tmp_path,people,ids)
    assert r['counts']['persons']==2
    assert read(out,'person_pairs')[0]['reason']=='document_context_conflict'


def test_source_scoped_documents_cannot_bridge_sources(tmp_path):
    people=[person('a'),person('b',source='forum_json')]
    ids=[identifier('a','D1',kind='document'),identifier('b','D1',source='forum_json',kind='document')]
    r,_,_=resolve(tmp_path,people,ids)
    assert r['counts']['persons']==2


def test_source_document_needs_name_and_dob(tmp_path):
    people=[person('a',dob=None),person('b',dob=None)]
    r,out,_=resolve(tmp_path,people,[identifier('a','D1',kind='document'),identifier('b','D1',kind='document')])
    assert r['counts']['persons']==2
    assert read(out,'person_pairs')[0]['reason']=='unconfirmed_document_context'


def test_repeated_ticket_in_three_sources_is_one_segment(tmp_path):
    people=[person('a'),person('b',source='forum_json'),person('c',source='pointz_xml')]
    ids=[identifier(p['observation_id'],source=p['source_id']) for p in people]
    trips=[travel('t'+p['observation_id'],p['observation_id'],source=p['source_id'],ticket_number='000789') for p in people]
    r,out,_=resolve(tmp_path,people,ids,trips)
    assert r['counts']['persons']==1 and r['counts']['segments']==1
    assert r['counts']['segment_links']==3 and r['graph_eligible_segments']==1
    assert read(out,'segments')[0]['observation_count']==3


@pytest.mark.parametrize('changes',[
    dict(departure_raw='2017-01-03T10:30',departure_local='2017-01-03T10:30'),
    dict(destination_raw='LHR'),dict(flight_number='SU002'),
    dict(departure_raw='2017-01-02T11:30',departure_local='2017-01-02T11:30'),
    dict(ticket_number='different')])
def test_distinct_trips_are_not_deduplicated(tmp_path,changes):
    people=[person('a')]
    trips=[travel('ta','a',ticket_number='same'),travel('tb','a',**dict(dict(ticket_number='same'),**changes))]
    r,_,_=resolve(tmp_path,people,[identifier('a')],trips)
    assert r['counts']['segments']==2


def test_date_only_does_not_bridge_two_departure_times(tmp_path):
    trips=[travel('ta','a'),travel('tb','a',departure_raw='2017-01-02T18:30',departure_local='2017-01-02T18:30'),
           travel('tc','a',departure_raw='2017-01-02',departure_local='2017-01-02',departure_precision='date')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==3
    assert next(x for x in read(out,'segment_links') if x['travel_id']=='tc')['status']=='ambiguous'


def test_unknown_origin_needs_shared_scoped_ticket_to_attach(tmp_path):
    trips=[travel('ta','a',ticket_number='0001'),travel('tb','a',origin_raw=None,ticket_number='0001')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==1
    assert read(out,'segments')[0]['origin_iata']=='SVO'
    link=next(x for x in read(out,'segment_links') if x['travel_id']=='tb')
    assert link['origin_status']=='unresolved' and link['rule_id']=='segment.shared_ticket'


def test_missing_route_without_ticket_stays_separate(tmp_path):
    trips=[travel('ta','a'),travel('tb','a',origin_raw=None)]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==2
    assert next(x for x in read(out,'segment_links') if x['travel_id']=='tb')['status']=='ambiguous'


def test_same_booking_is_not_person_or_segment_evidence(tmp_path):
    people=[person('a'),person('b')]
    trips=[travel('ta','a',booking_code='PNR'),travel('tb','b',booking_code='PNR')]
    r,out,_=resolve(tmp_path,people,travels=trips)
    assert r['counts']['persons']==2 and r['counts']['segments']==2
    assert r['graph_eligible_segments']==0


def test_reservation_and_self_report_do_not_confirm_graph_participation(tmp_path):
    trips=[travel('ta','a',event_kind='reservation_record'),travel('tb','a',event_kind='registered_flight')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==1 and r['graph_eligible_segments']==0
    assert read(out,'segments')[0]['participation_status']=='reported_only'


def test_explicit_codeshare_field_is_not_a_verified_mapping(tmp_path):
    trips=[travel('ta','a',flight_number='SU001',codeshare='KL1'),travel('tb','a',flight_number='KL1')]
    r,_,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==2


def test_verified_codeshare_is_scoped_to_date_and_route(tmp_path):
    rule=dict(rule_id='fixture:codeshare',source_id='*',marketing_flight='KL1',operating_flight='SU1',
              origin_iata='SVO',destination_iata='LED',valid_from='2017-01-01',valid_to='2017-01-31',
              reason='Synthetic confirmed mapping',evidence='fixture only')
    trips=[travel('ta','a',flight_number='SU001'),travel('tb','a',flight_number='KL1')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips,settings=dict(codeshare_rules=[rule]))
    assert r['counts']['segments']==1
    assert next(x for x in read(out,'segment_links') if x['travel_id']=='tb')['codeshare_rule_id']=='fixture:codeshare'


def test_ids_and_links_are_stable_across_batches_and_reruns(tmp_path):
    people=[person('z'),person('a',source='forum_json')]
    r,out,inp=resolve(tmp_path,people,[identifier('z'),identifier('a',source='forum_json')],
                      [travel('tz','z'),travel('ta','a',source='forum_json')],batch_size=1)
    before={t:pq.read_table(out/(t+'.parquet')) for t in ('persons','person_links','person_pairs','segments','segment_links')}
    run(inp,out,CONFIG,review_output=tmp_path/'review',batch_size=7)
    for table,snapshot in before.items():assert snapshot.equals(pq.read_table(out/(table+'.parquet')))


def test_candidate_limit_fails_without_partial_success(tmp_path):
    people=[person(str(i)) for i in range(5)]
    inp=input_snapshot(tmp_path,people,[identifier(p['observation_id']) for p in people])
    config=yaml.safe_load(CONFIG.read_text());config['max_candidates']=2
    path=tmp_path/'rules.yaml';path.write_text(yaml.safe_dump(config))
    with pytest.raises(ValueError,match='upper bound'):run(inp,tmp_path/'out',path,review_output=tmp_path/'review')
    assert not (tmp_path/'out/resolution_manifest.json').exists()


def test_large_blocks_use_a_star_and_still_detect_dob_conflicts(tmp_path):
    people=[person(f'p{i:02}') for i in range(20)]
    r,out,_=resolve(tmp_path,people,[identifier(p['observation_id']) for p in people],
                    settings=dict(pairwise_block_size=2,max_name_block_size=100))
    assert r['counts']['persons']==1
    assert r['counts']['person_pairs']==19
    assert 'star_block' in {x['reason'] for x in read(out,'blocking_issues')}


def test_component_limit_excludes_uncertain_members_from_graph(tmp_path):
    people=[person(str(i)) for i in range(4)]
    r,out,_=resolve(tmp_path,people,[identifier(p['observation_id']) for p in people],settings=dict(max_component_size=2))
    assert r['counts']['persons']>=2
    assert not any(p['graph_eligible'] for p in read(out,'person_links'))


def test_upstream_hash_mismatch_fails_before_replacing_outputs(tmp_path):
    r,out,inp=resolve(tmp_path,[person('a')],[identifier('a')])
    previous=(out/'resolution_manifest.json').read_bytes()
    with (inp/'identifiers.parquet').open('ab') as f:f.write(b'changed')
    with pytest.raises(ValueError,match='manifest'):run(inp,out,CONFIG,review_output=tmp_path/'review')
    assert (out/'resolution_manifest.json').read_bytes()==previous


def test_review_is_blank_and_reports_shortages_not_fake_accuracy(tmp_path):
    r,out,_=resolve(tmp_path,[person('a'),person('b')],[identifier('a'),identifier('b')])
    sample=tmp_path/'review/person_pairs.csv'
    with sample.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    assert rows and all(not x['reviewer_decision'] for x in rows)
    report=audit_to_file(sample,out,tmp_path/'review/audit.json')
    assert report['reviewed']==0 and report['accepted_sample_precision'] is None
    assert r['review']['shortages']['accepted']>0


def test_review_metrics_are_sample_only_and_edits_are_preserved(tmp_path):
    r,out,inp=resolve(tmp_path,[person('a'),person('b')],[identifier('a'),identifier('b')])
    sample=tmp_path/'review/person_pairs.csv'
    with sample.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    rows[0].update(reviewer_decision='rejected',reviewer_name='Synthetic reviewer',reviewed_at='2026-10-17T00:00:00Z',reviewer_notes='test annotation only')
    with sample.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    snapshot=sample.read_bytes()
    review=audit_to_file(sample,out,tmp_path/'review/audit.json')
    assert review['accepted_sample_precision']==0 and review['population_accuracy'] is None
    assert len(review['disagreements'])==1
    run(inp,out,CONFIG,review_output=tmp_path/'review')
    assert sample.read_bytes()==snapshot


def test_cli_quiet_and_preserving_t11_t12(tmp_path,capsys):
    from yoyoflot.cli import main
    inp=input_snapshot(tmp_path,[person('a')],[identifier('a')],[travel('ta','a')])
    names=['canonical_manifest.json','t11_manifest.json','observations.parquet','travel_geography.parquet']
    previous={n:(inp/n).read_bytes() for n in names}
    assert main(['resolve','--input',str(inp),'--output',str(inp),'--config',str(CONFIG),
                 '--review-output',str(tmp_path/'review'),'-q'])==0
    captured=capsys.readouterr();assert captured.out==captured.err==''
    assert 'Итог T13' in (inp/'resolution_summary.txt').read_text(encoding='utf-8')
    for name,data in previous.items():assert (inp/name).read_bytes()==data


def test_review_contains_exactly_50_30_20_real_candidate_decisions(tmp_path):
    from datetime import date,timedelta
    people,ids=[],[]
    for group,count,year in [('accepted',50,1980),('ambiguous',30,1981),('rejected',20,1982)]:
        for i in range(count):
            dob=(date(year,1,1)+timedelta(days=i)).isoformat()
            for suffix in ('a','b'):
                pid=f'{group}:{i}:{suffix}'
                people.append(person(pid,name=f'{group} Person{i}',dob=dob))
                if group=='accepted':ids.append(identifier(pid,value=f'{group}:{i}'))
                elif group=='rejected':ids.append(identifier(pid,value=pid,kind='document'))
    r,out,_=resolve(tmp_path,people,ids)
    assert r['review']['selected']=={'accepted':50,'ambiguous':30,'rejected':20}
    assert sum(r['review']['shortages'].values())==0
    with (tmp_path/'review/person_pairs.csv').open(encoding='utf-8-sig',newline='') as f:
        sample=list(csv.DictReader(f))
    assert len(sample)==100 and all(not x['reviewer_decision'] for x in sample)


def test_input_order_does_not_change_person_or_segment_ids(tmp_path):
    people=[person('z'),person('a',source='forum_json')]
    ids=[identifier('z'),identifier('a',source='forum_json')]
    trips=[travel('tz','z'),travel('ta','a',source='forum_json')]
    first=tmp_path/'first';second=tmp_path/'second';first.mkdir();second.mkdir()
    _,out1,_=resolve(first,people,ids,trips)
    _,out2,_=resolve(second,list(reversed(people)),list(reversed(ids)),list(reversed(trips)))
    for table in ('persons','person_links','person_pairs','segments','segment_links'):
        assert pq.read_table(out1/(table+'.parquet')).equals(pq.read_table(out2/(table+'.parquet')))


def test_path_compression_preserves_all_roots():
    state=Components(5,array('i',[1,1,1,1,1]),100)
    for a,b in [(3,4),(2,3),(1,2),(0,1)]:assert state.merge(a,b,'[]','[]') is None
    assert all(state.find(i)==0 for i in range(5))


def test_missing_event_key_stays_unresolved(tmp_path):
    trips=[travel('ta','a',flight_number='123'),travel('tb','a',flight_number='123')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==2 and r['graph_eligible_segments']==0


def test_native_lock_excludes_another_t13_writer(tmp_path):
    from yoyoflot.normalize.locking import OutputLock
    out=tmp_path/'out';out.mkdir()
    with OutputLock(out/'.resolution.lock',label='T13'):
        with pytest.raises(RuntimeError,match='T13'):
            run(tmp_path,out,CONFIG,review_output=tmp_path/'review')


def test_require_all_is_explicit_and_does_not_run_readers(tmp_path):
    inp=input_snapshot(tmp_path,[person('a')],[identifier('a')])
    with pytest.raises(ValueError,match='Missing personal sources'):
        run(inp,tmp_path/'out',CONFIG,review_output=tmp_path/'review',require_all=True)


def test_review_rejects_stale_or_changed_author_evidence(tmp_path):
    r,out,_=resolve(tmp_path,[person('a'),person('b')],[identifier('a'),identifier('b')])
    sample=tmp_path/'review/person_pairs.csv'
    with sample.open(encoding='utf-8-sig',newline='') as f:rows=list(csv.DictReader(f))
    rows[0]['author_decision']='rejected'
    with sample.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=FIELDS);writer.writeheader();writer.writerows(rows)
    with pytest.raises(ValueError,match='edited'):
        audit_to_file(sample,out,tmp_path/'review/audit.json')


def test_minute_and_second_evidence_keep_the_observed_precision(tmp_path):
    trips=[travel('ta','a'),travel('tb','a',departure_raw='2017-01-02T10:30:45',
           departure_local='2017-01-02T10:30:45',departure_precision='second')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==1
    segment=read(out,'segments')[0]
    assert segment['departure_local']=='2017-01-02T10:30:45' and segment['departure_precision']=='second'


def test_different_seconds_are_distinct_observed_events(tmp_path):
    trips=[travel('ta','a',departure_raw='2017-01-02T10:30:00',departure_local='2017-01-02T10:30:00',departure_precision='second'),
           travel('tb','a',departure_raw='2017-01-02T10:30:30',departure_local='2017-01-02T10:30:30',departure_precision='second')]
    r,_,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    assert r['counts']['segments']==2


def test_date_only_never_becomes_midnight_or_utc(tmp_path):
    trips=[travel('ta','a',departure_raw='2017-01-02',departure_local='2017-01-02',departure_precision='date')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    segment=read(out,'segments')[0]
    assert segment['departure_local']=='2017-01-02' and segment['departure_precision']=='date'
    assert segment['departure_utc'] is None and not segment['time_complete']


def test_next_day_arrival_keeps_its_source_date(tmp_path):
    trips=[travel('ta','a',departure_raw='2017-01-02T23:30',departure_local='2017-01-02T23:30',
                  arrival_raw='2017-01-03T01:00',arrival_local='2017-01-03T01:00',arrival_precision='minute')]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips)
    segment=read(out,'segments')[0]
    assert segment['arrival_local']=='2017-01-03T01:00'
    assert segment['arrival_utc']=='2017-01-02T22:00:00Z' and segment['time_complete']


def test_inferred_geography_is_not_an_observed_event_key(tmp_path):
    from yoyoflot.normalize.values import file_hash
    inp=input_snapshot(tmp_path,[person('a')],[identifier('a')],[travel('ta','a',origin_raw=None)])
    path=inp/'travel_geography.parquet';table=pq.read_table(path);rows=table.to_pylist()
    rows[0].update(origin_airport_id='2985',origin_iata='SVO',origin_status='inferred_from_schedule',origin_kind='airport')
    pq.write_table(pa.Table.from_pylist(rows,schema=table.schema),path)
    manifest=json.loads((inp/'t11_manifest.json').read_text());manifest['files']['travel_geography.parquet']=file_hash(path)
    (inp/'t11_manifest.json').write_text(json.dumps(manifest))
    r,_=run(inp,tmp_path/'out',CONFIG,review_output=tmp_path/'review')
    segment=read(tmp_path/'out','segments')[0]
    assert segment['origin_airport_id'] is None and not segment['graph_eligible']


def test_oversized_trip_group_is_preserved_with_explicit_conflict(tmp_path):
    trips=[travel(f't{i}','a') for i in range(4)]
    r,out,_=resolve(tmp_path,[person('a')],[identifier('a')],trips,settings=dict(max_trip_group_size=2))
    assert r['counts']['segments']==4 and r['counts']['segment_links']==4
    assert r['graph_eligible_segments']==0
    assert 'trip_group_limit' in {c['reason'] for c in read(out,'conflicts')}


def test_audit_cannot_overwrite_reviewed_csv(tmp_path):
    r,out,_=resolve(tmp_path,[person('a'),person('b')],[identifier('a'),identifier('b')])
    sample=tmp_path/'review/person_pairs.csv';before=sample.read_bytes()
    with pytest.raises(ValueError,match='overwrite'):
        audit_to_file(sample,out,sample)
    assert sample.read_bytes()==before


def test_failed_publication_preserves_old_outputs_and_human_review(tmp_path,monkeypatch):
    import yoyoflot.resolve.pipeline as pipeline
    r,out,inp=resolve(tmp_path,[person('a'),person('b')],[identifier('a'),identifier('b')])
    previous=(out/'resolution_manifest.json').read_bytes()
    sample=tmp_path/'review/person_pairs.csv';review_before=sample.read_bytes()
    cfg=yaml.safe_load(CONFIG.read_text());cfg['review_seed']='a different run'
    path=tmp_path/'new_config.yaml';path.write_text(yaml.safe_dump(cfg))
    def fail(*args):raise RuntimeError('synthetic publication failure')
    monkeypatch.setattr(pipeline,'publish',fail)
    with pytest.raises(RuntimeError,match='publication'):
        run(inp,out,path,review_output=tmp_path/'review')
    assert (out/'resolution_manifest.json').read_bytes()==previous
    assert sample.read_bytes()==review_before
    assert len(list((tmp_path/'review').glob('*.csv')))==1
