"""Check whether C03 candidates repeat within a single supplied source.

This inspects the selected four-month windows saved by audit_marker_candidates.
It neither changes runtime factors nor searches other windows for new matches.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path

import duckdb


ROOT = Path(__file__).resolve().parents[1]


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def shift_month(day, amount):
    value=day.year*12+day.month-1+amount
    return date(value//12,value%12+1,1)


def scan_all_sources(db, output):
    """Search every complete four-month window without pooling source counts."""
    print('Scanning C03 separately within every source',flush=True)
    db.execute("SET memory_limit='768MB'")
    db.execute('SET preserve_insertion_order=false')
    db.read_parquet(str(ROOT/'artifacts/source-assessment-v2/segment_source_links.parquet')).create_view('segment_links')
    db.read_parquet(str(ROOT/'data/processed/t14_canonical_20261017/travel_observations.parquet')).create_view('travel')
    db.execute("""CREATE TABLE source_events AS WITH raw AS (
        SELECT s.person_id,s.identity_status,t.source_id,s.segment_id,s.origin_airport_id,s.destination_airport_id,
            try_cast(s.departure_utc AS TIMESTAMP) dep_utc,
            coalesce(cast(try_cast(s.departure_utc AS TIMESTAMP) AS DATE),try_cast(s.departure_date AS DATE)) event_date,
            z.country destination_country,
            coalesce(s.resolution_status='resolved' AND s.origin_airport_id<>s.destination_airport_id
                AND nullif(o.country,'') IS NOT NULL AND nullif(z.country,'') IS NOT NULL
                AND upper(trim(coalesce(t.origin_raw,''))) NOT IN ('','REA')
                AND upper(trim(coalesce(t.destination_raw,''))) NOT IN ('','REA'),false) usable
        FROM segment_links l JOIN segments s USING(segment_id) JOIN travel t USING(travel_id)
        LEFT JOIN airports o ON s.origin_airport_id=o.airport_id
        LEFT JOIN airports z ON s.destination_airport_id=z.airport_id
        WHERE t.event_kind<>'reservation_record')
        SELECT person_id,any_value(identity_status) identity_status,source_id,origin_airport_id,destination_airport_id,
            dep_utc,event_date,any_value(destination_country) destination_country,bool_and(usable) usable,
            min(segment_id) segment_id
        FROM raw GROUP BY person_id,source_id,origin_airport_id,destination_airport_id,dep_utc,event_date""")
    db.execute("""CREATE TABLE source_flags AS SELECT person_id,source_id,bool_and(event_date IS NOT NULL) dates_known
        FROM source_events GROUP BY 1,2""")
    db.execute("""CREATE TABLE source_months AS SELECT e.person_id,e.identity_status,e.source_id,
        date_trunc('month',event_date)::DATE AS month,count(*) FILTER(WHERE usable) n
        FROM source_events e JOIN source_flags f USING(person_id,source_id)
        WHERE f.dates_known AND event_date IS NOT NULL GROUP BY 1,2,3,4
        HAVING count(*) FILTER(WHERE usable)::DOUBLE/count(*)>=.8""")
    db.execute("""CREATE TABLE source_windows AS WITH g AS (
        SELECT m.person_id,m.identity_status,m.source_id,m.month,m.n current_events,
            sum(p.n) prior_events,count(*) prior_active_months
        FROM source_months m JOIN source_months p ON m.person_id=p.person_id AND m.source_id=p.source_id
            AND p.month BETWEEN m.month-INTERVAL 3 MONTH AND m.month-INTERVAL 1 MONTH
        WHERE m.month+INTERVAL 1 MONTH<=(SELECT max(event_date)+INTERVAL 1 DAY FROM source_events)
        GROUP BY m.person_id,m.identity_status,m.source_id,m.month,m.n)
        SELECT *, (current_events::DOUBLE/date_diff('day',month,month+INTERVAL 1 MONTH)) /
            (prior_events::DOUBLE/date_diff('day',month-INTERVAL 3 MONTH,month)) rate_ratio
        FROM g WHERE prior_active_months=3 AND prior_events>=6""")
    db.execute("""CREATE TABLE source_activity_increases AS SELECT * FROM source_windows
        WHERE current_events>=6 AND rate_ratio>=3""")
    db.execute("""CREATE TABLE source_country_changes AS SELECT w.person_id,w.source_id,w.month,e.destination_country,
        count(*) n,NOT EXISTS(SELECT 1 FROM source_events p WHERE p.person_id=w.person_id AND p.source_id=w.source_id
            AND p.usable AND p.destination_country=e.destination_country
            AND p.event_date>=w.month-INTERVAL 3 MONTH AND p.event_date<w.month) is_new
        FROM source_activity_increases w JOIN source_events e ON e.person_id=w.person_id AND e.source_id=w.source_id
            AND e.usable AND e.event_date>=w.month AND e.event_date<w.month+INTERVAL 1 MONTH
        GROUP BY w.person_id,w.source_id,w.month,e.destination_country""")
    db.execute("""CREATE TABLE source_candidates AS WITH g AS (
        SELECT person_id,source_id,month,list_sort(list(destination_country) FILTER(WHERE is_new)) new_countries,
            sum(n) FILTER(WHERE is_new)::DOUBLE/sum(n) new_country_share
        FROM source_country_changes GROUP BY 1,2,3)
        SELECT w.*,g.new_countries,g.new_country_share FROM source_activity_increases w
        JOIN g USING(person_id,source_id,month) WHERE len(g.new_countries)>=3 AND g.new_country_share>=.5""")
    def rows(sql):
        cur=db.execute(sql);names=[f[0] for f in cur.description]
        return [dict(zip(names,r)) for r in cur.fetchall()]
    summary=rows("""WITH p AS(SELECT DISTINCT person_id FROM coverage WHERE status='matched'),
        u AS(SELECT DISTINCT person_id,identity_status FROM source_candidates)
        SELECT u.identity_status,count(*) profiles,count(*) FILTER(WHERE p.person_id IS NOT NULL) already_have_signals,
            count(*) FILTER(WHERE p.person_id IS NULL) new_profiles
        FROM u LEFT JOIN p USING(person_id) GROUP BY 1 ORDER BY 1""")
    details=rows("""SELECT w.source_id,w.identity_status,count(distinct w.person_id) comparable_profiles,
        count(distinct w.person_id) FILTER(WHERE current_events>=6 AND rate_ratio>=3) activity_increases,
        count(distinct w.person_id) FILTER(WHERE w.person_id IN(SELECT c.person_id FROM source_candidates c
            WHERE c.source_id=w.source_id)) combined_candidates
        FROM source_windows w GROUP BY 1,2 ORDER BY 1,2""")
    overlap=rows("""WITH c AS(SELECT DISTINCT person_id FROM source_candidates),
        original AS(SELECT person_id FROM candidates WHERE criterion_id='C03')
        SELECT s.identity_status,count(distinct s.person_id) profiles,
            count(distinct s.person_id) FILTER(WHERE o.person_id IS NOT NULL) also_in_pooled_C03,
            count(distinct s.person_id) FILTER(WHERE o.person_id IS NULL) absent_from_pooled_C03
        FROM source_candidates s LEFT JOIN original o USING(person_id) GROUP BY 1 ORDER BY 1""")
    db.execute("COPY (SELECT * FROM source_candidates ORDER BY person_id,source_id,month) TO ? (FORMAT PARQUET,COMPRESSION ZSTD)",
        [str(output/'c03_single_source_candidates.parquet')])
    db.execute("""COPY (SELECT c.person_id,c.identity_status,c.source_id,c.month,e.event_date,e.segment_id,
        s.origin_iata,s.destination_iata,t.travel_id,t.observation_id,t.source_record_id,t.locator,t.event_kind
        FROM source_candidates c JOIN source_events e ON c.person_id=e.person_id AND c.source_id=e.source_id
            AND e.usable AND e.event_date>=c.month-INTERVAL 3 MONTH AND e.event_date<c.month+INTERVAL 1 MONTH
        JOIN segments s USING(segment_id) JOIN segment_links l USING(segment_id) JOIN travel t USING(travel_id))
        TO ? (FORMAT PARQUET,COMPRESSION ZSTD)""",[str(output/'c03_single_source_evidence.parquet')])
    return {'summary':summary,'by_source':details,'overlap_pooled_C03':overlap,
        'scope':'All source/profile windows; sources are evaluated separately, without pooling.',
        'outputs':{name:sha256(output/name) for name in ('c03_single_source_candidates.parquet','c03_single_source_evidence.parquet')}}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--audit',type=Path,default=ROOT/'artifacts/marker-candidates-20261021-v4')
    parser.add_argument('--scan-all',action='store_true')
    args=parser.parse_args()
    audit=args.audit.resolve()
    output=audit/('source_robustness_all.json' if args.scan_all else 'source_robustness.json')
    if output.exists():raise ValueError('Source robustness output already exists')
    report=json.loads((audit/'report.json').read_text(encoding='utf-8'))
    candidates=audit/'candidates.parquet';evidence=audit/'evidence.parquet'
    segments=ROOT/'artifacts/source-product/tables/segments.parquet'
    airports=ROOT/'artifacts/source-product/airports.csv'
    for path in (candidates,evidence):
        if sha256(path)!=report['output_sha256'][path.name]:
            raise ValueError('Audit output checksum mismatch: '+path.name)
    for path in (segments,airports):
        if sha256(path)!=report['input_sha256'][str(path.relative_to(ROOT))]:
            raise ValueError('Product input checksum mismatch: '+path.name)
    coverage=ROOT/'artifacts/source-assessment-v2/marker_coverage.parquet'
    inputs=[audit/'report.json',candidates,evidence,segments,airports,coverage,Path(__file__).resolve()]
    if args.scan_all:
        inputs.extend((ROOT/'artifacts/source-assessment-v2/segment_source_links.parquet',
            ROOT/'data/processed/t14_canonical_20261017/travel_observations.parquet'))
        for path in inputs[-2:]:
            if sha256(path)!=report['input_sha256'][str(path.relative_to(ROOT))]:
                raise ValueError('Scan input checksum mismatch: '+path.name)
    before={str(p):sha256(p) for p in inputs}
    with duckdb.connect() as db:
        db.execute('SET threads=1')
        db.execute("SET memory_limit='256MB'")
        for name,path in (('candidates',candidates),('evidence',evidence),('segments',segments)):
            db.read_parquet(str(path)).create_view(name)
        db.read_csv(str(airports),header=True).create_view('airports')
        hits=db.execute("SELECT person_id,identity_status,detail FROM candidates WHERE criterion_id='C03'").fetchall()
        query="""SELECT DISTINCT e.person_id,e.event_id,e.source_id,e.event_date,a.country
            FROM evidence e JOIN segments s USING(segment_id)
            JOIN airports a ON s.destination_airport_id=a.airport_id
            WHERE e.criterion_id='C03' AND s.resolution_status='resolved'
              AND e.origin_iata NOT IN ('','REA') AND e.destination_iata NOT IN ('','REA')"""
        groups=defaultdict(lambda:defaultdict(list))
        for person,event,source,day,country in db.execute(query).fetchall():
            groups[person][source].append((event,day,country))
        results=[]
        for person,status,detail in hits:
            detail=json.loads(detail);month=date.fromisoformat(detail['month'])
            start=shift_month(month,-3);stop=shift_month(month,1)
            row={'person_id':person,'identity_status':status,'month':str(month),'source_checks':[]}
            for source,events in sorted(groups[person].items()):
                previous=[r for r in events if start<=r[1]<month]
                current=[r for r in events if month<=r[1]<stop]
                prior_months={r[1].replace(day=1) for r in previous}
                prior_countries={r[2] for r in previous}
                new_countries={r[2] for r in current}-prior_countries
                share=sum(r[2] in new_countries for r in current)/len(current) if current else None
                ratio=(len(current)/(stop-month).days)/(len(previous)/(month-start).days) if previous else None
                available=len(prior_months)==3 and len(previous)>=6 and len(current)>=6
                matched=ratio>=3 and len(new_countries)>=3 and share>=.5 if available else None
                row['source_checks'].append({'source_id':source,'previous_events':len(previous),
                    'current_events':len(current),'prior_active_months':len(prior_months),
                    'daily_rate_ratio':ratio,'new_countries':sorted(new_countries),
                    'new_country_share':share,'enough_records':available,'replicates':matched})
            available=any(r['enough_records'] for r in row['source_checks'])
            replicated=any(r['replicates'] is True for r in row['source_checks'])
            row['source_validation']='replicated' if replicated else 'comparable_not_matched' if available else 'insufficient_source_history'
            results.append(row)
        summary=[]
        for status in ('accepted','ambiguous'):
            group=[r for r in results if r['identity_status']==status]
            summary.append({'identity_status':status,'candidates':len(group),
                **{key:sum(r['source_validation']==key for r in group)
                    for key in ('replicated','comparable_not_matched','insufficient_source_history')}})
        support=db.execute("""WITH e AS (SELECT person_id,criterion_id,count(distinct event_id) events,
            count(distinct event_id) FILTER(WHERE event_kind IN ('boarding_record','boarding_pass_record')) boarding_events
            FROM evidence GROUP BY 1,2), h AS (SELECT DISTINCT person_id,criterion_id,identity_status FROM candidates)
            SELECT h.criterion_id,h.identity_status,count(*) profiles,
                count(*) FILTER(WHERE e.boarding_events>0) any_boarding_record,
                count(*) FILTER(WHERE e.boarding_events=e.events) all_events_have_boarding_records
            FROM h JOIN e USING(person_id,criterion_id) GROUP BY 1,2 ORDER BY 1,2""")
        support_columns=[c[0] for c in support.description]
        support=[dict(zip(support_columns,row)) for row in support.fetchall()]
        db.read_parquet(str(coverage)).create_view('coverage')
        positive={row[0] for row in db.execute("SELECT DISTINCT person_id FROM coverage WHERE status='matched'").fetchall()}
        for row in results:row['already_has_signals']=row['person_id'] in positive
        scan=scan_all_sources(db,audit) if args.scan_all else None
    unchanged=before=={str(p):sha256(p) for p in inputs}
    if not unchanged:raise RuntimeError('Input changed during source check')
    payload={'version':'candidate-source-audit:1.0','created_at':datetime.now(timezone.utc).isoformat(),
        'audit_report_sha256':sha256(audit/'report.json'),'input_sha256':before,'policy':report['policy']['C03'],
        'scope':'Selected C03 windows only; enough records is not proof of continuous source coverage.',
        'source_query':query,'summary':summary,'boarding_support':support,'results':results,
        'all_inputs_unchanged':unchanged}
    if scan is not None:payload['whole_population_scan']=scan
    output.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'summary':summary,'boarding_support':support,
        'whole_population_scan':scan},ensure_ascii=False,indent=2,default=str))


if __name__=='__main__':main()
