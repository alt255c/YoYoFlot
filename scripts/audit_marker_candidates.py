"""Explore marker hypotheses on supplied records without changing runtime scores.

All thresholds below are declared screening hypotheses, not calibrated labels.
Run with the repository virtualenv; results go to a separate audit directory.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import tempfile

import duckdb


ROOT = Path(__file__).resolve().parents[1]
POLICY = {
    "C01": {"title": "Series of short international return intervals",
        "max_departure_gap_days": 3, "window_days": 90,
        "min_returns": 3, "min_destination_countries": 3,
        "same_origin_airport": True, "shared_flight_legs_allowed": False,
        "return_must_finish_inside_window": True},
    "C02": {"title": "Repeated directed cycles", "cycle_lengths": [3, 4],
        "max_cycle_days": 30, "min_nonoverlapping_occurrences": 2,
        "min_countries": 2},
    "C03": {"title": "Change in recorded travel pattern",
        "baseline_calendar_months": 3, "current_calendar_months": 1,
        "min_baseline_events": 6, "min_current_events": 6,
        "min_daily_rate_ratio": 3, "min_new_destination_countries": 3,
        "min_share_events_in_new_countries": 0.5,
        "require_common_source_in_each_month": True},
    "C04": {"title": "Candidate incompatible departures",
        "max_gap_hours": 6, "min_origin_distance_km": 500,
        "screening_speed_kmh": 1500,
        "simultaneous_min_origin_distance_km": 100,
        "utc_policy": "airport-derived; no explicit source timezones",
        "speed_is_physical_proof": False},
}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def records(db, sql, params=None):
    result = db.execute(sql, params or [])
    names = [x[0] for x in result.description]
    return [dict(zip(names, row)) for row in result.fetchall()]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
        default=ROOT / "artifacts/marker-candidates-20261021-v4")
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("Use a new output directory to preserve earlier audits")
    product = ROOT / "artifacts/source-product"
    assessment = ROOT / "artifacts/source-assessment-v2"
    canonical = ROOT / "data/processed/t14_canonical_20261017"
    paths = {"persons": product / "tables/persons.parquet",
        "segments": product / "tables/segments.parquet",
        "rankings": product / "tables/rankings.parquet",
        "coverage": assessment / "marker_coverage.parquet",
        "source_links": assessment / "source_links.parquet",
        "segment_links": assessment / "segment_source_links.parquet",
        "profile_identifiers": assessment / "profile_identifiers.parquet",
        "observations": canonical / "observations.parquet",
        "travel": canonical / "travel_observations.parquet",
        "airports": product / "airports.csv",
        "product_manifest": product / "run_manifest.json",
        "assessment_manifest": assessment / "assessment_manifest.json",
        "canonical_manifest": canonical / "canonical_manifest.json",
        "script": Path(__file__).resolve()}
    for source_root in (product, assessment, canonical):
        if output == source_root or source_root in output.parents or output in source_root.parents:
            raise ValueError("Output must be separate from inputs")
    before = {str(p.relative_to(ROOT)): digest(p) for p in paths.values()}
    # Match the frozen manifests before using their source/profile associations.
    pm = json.loads(paths["product_manifest"].read_text(encoding="utf-8"))
    am = json.loads(paths["assessment_manifest"].read_text(encoding="utf-8"))
    cm = json.loads(paths["canonical_manifest"].read_text(encoding="utf-8"))
    if am["request"]["product_manifest_sha256"] != digest(paths["product_manifest"]):
        raise ValueError("Assessment belongs to another product")
    for key in ("persons", "segments", "rankings", "airports"):
        if digest(paths[key]) != pm["files"][str(paths[key].relative_to(product)).replace('\\', '/')]:
            raise ValueError("Product checksum mismatch: " + key)
    for key in ("source_links", "segment_links", "profile_identifiers", "coverage"):
        if digest(paths[key]) != am["files"][paths[key].name]:
            raise ValueError("Assessment checksum mismatch: " + key)
    for key in ("observations", "travel"):
        if digest(paths[key]) != cm["files"][paths[key].name]:
            raise ValueError("Canonical checksum mismatch: " + key)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="candidate-audit-", dir=output) as temp, duckdb.connect() as db:
        db.execute("SET threads=2")
        db.execute("SET memory_limit='768MB'")
        db.execute("SET preserve_insertion_order=false")
        db.execute("SET temp_directory=?", [temp])
        for key, path in paths.items():
            if path.suffix == '.parquet':
                db.read_parquet(str(path)).create_view(key)
        db.read_csv(str(paths["airports"]), header=True).create_view("airports")
        report = {"version": "candidate-audit:1.0", "created_at": datetime.now(timezone.utc).isoformat(),
            "product_run_id": pm["run_id"], "assessment_run_id": am["run_id"],
            "input_sha256": before, "policy": POLICY,
            "limitations": [
                "No labels of espionage; no detection accuracy or probability can be calculated.",
                "Boarding-record evidence_scope denotes source records, not independently verified physical boarding.",
                "Reported evidence_scope excludes reservations, but reports and loyalty entries do not prove travel.",
                "Identity accepted is a frozen resolver status, not an external identity verification.",
                "Unknown same-day ordering and unusable records break sequences; unknown dates disable profile sequences.",
                "Truly absent events can still conceal intervening travel.",
                "Short returns use departure-to-departure intervals, not actual time spent in a country.",
                "All UTC departure times are airport-derived; clock/date/geography errors may create conflicts.",
                "Recorded monthly activity does not establish continuous source coverage.",
                "No weights, model, backend, frontend or source Parquet are changed."]}
        def checkpoint(stage):
            report['last_completed_stage']=stage
            (output/'progress.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
        print("Auditing identity fields and baseline population", flush=True)
        report["population"] = records(db, "SELECT identity_status,count(*) profiles FROM persons GROUP BY 1")
        db.execute("CREATE TABLE positive AS SELECT DISTINCT person_id FROM coverage WHERE status='matched'")
        report["current_signal_profiles"] = db.execute("SELECT count(*) FROM positive").fetchone()[0]
        report["field_audit"] = {}
        field_queries = {
            "sex_by_source": "SELECT source_id,count(*) observations,count(sex) filled FROM observations GROUP BY 1 ORDER BY 1",
            "linked_sex_contradictions": "SELECT count(*) profiles FROM (SELECT l.person_id FROM source_links l JOIN observations o USING(observation_id) WHERE sex IS NOT NULL GROUP BY l.person_id HAVING count(distinct sex)>1)t",
            "linked_birth_date_contradictions": "SELECT count(*) profiles FROM (SELECT l.person_id FROM source_links l JOIN observations o USING(observation_id) WHERE birth_date IS NOT NULL GROUP BY l.person_id HAVING count(distinct birth_date)>1)t",
            "document_sex_contradictions": "SELECT count(*) conflict_groups FROM (SELECT i.identifier_namespace,i.value_normalized FROM profile_identifiers i JOIN observations o USING(observation_id) WHERE kind='document' AND sex IS NOT NULL GROUP BY 1,2 HAVING count(distinct sex)>1)t",
            "document_birth_date_contradictions": "SELECT count(*) conflict_groups FROM (SELECT i.identifier_namespace,i.value_normalized FROM profile_identifiers i JOIN observations o USING(observation_id) WHERE kind='document' AND birth_date IS NOT NULL GROUP BY 1,2 HAVING count(distinct birth_date)>1)t",
            "source_time_fields": "SELECT source_id,event_kind,count(*) events,count(departure_timezone) explicit_timezone,count(arrival_local) arrival_time,count(*) FILTER(WHERE upper(coalesce(status_raw,''))='NO_SHOW') explicit_no_show FROM travel GROUP BY 1,2 ORDER BY 1",
            "observation_multi_profile_links": "SELECT count(*) observations FROM (SELECT observation_id FROM source_links GROUP BY 1 HAVING count(distinct person_id)>1)t",
            "segment_profile_mismatch": "SELECT count(*) links FROM segment_links l JOIN segments s USING(segment_id) WHERE l.person_id<>s.person_id",
            "baseline_unavailability": "SELECT criterion_id,availability_reason,count(*) profiles FROM coverage WHERE criterion_id IN ('M01','M02','M03') AND status='unavailable' GROUP BY 1,2 ORDER BY 1,3 DESC"}
        for key, query in field_queries.items():
            report["field_audit"][key] = records(db, query)
        checkpoint('identity_fields')
        print("Preparing de-duplicated events and safe ordering", flush=True)
        db.execute("""CREATE TABLE raw_events AS SELECT s.person_id,s.identity_status,s.segment_id,
            t.travel_id,t.observation_id,t.source_id,t.source_record_id,t.locator,t.event_kind,
            s.origin_airport_id origin_id,s.destination_airport_id destination_id,
            o.iata origin_iata,z.iata destination_iata,o.country origin_country,z.country destination_country,
            o.latitude origin_lat,o.longitude origin_lon,
            try_cast(s.departure_utc AS TIMESTAMP) dep_utc,
            coalesce(cast(try_cast(s.departure_utc AS TIMESTAMP) AS DATE),try_cast(s.departure_date AS DATE)) event_date,
            t.departure_precision IN ('minute','second') AND s.departure_utc IS NOT NULL precise,
            t.event_kind IN ('boarding_record','boarding_pass_record') boarding,
            coalesce(s.resolution_status='resolved' AND s.origin_airport_id<>s.destination_airport_id
              AND nullif(o.country,'') IS NOT NULL AND nullif(z.country,'') IS NOT NULL
              AND upper(trim(coalesce(t.origin_raw,''))) NOT IN ('','REA')
              AND upper(trim(coalesce(t.destination_raw,''))) NOT IN ('','REA'),false) route_usable,
            CASE t.source_id WHEN 'boarding_csv' THEN 1 WHEN 'boardingpass_xlsx' THEN 2
                WHEN 'forum_json' THEN 4 WHEN 'pointz_xml' THEN 8 WHEN 'skyteam_yaml' THEN 16 ELSE 32 END source_mask
            FROM segment_links l JOIN segments s USING(segment_id) JOIN travel t USING(travel_id)
            LEFT JOIN airports o ON s.origin_airport_id=o.airport_id
            LEFT JOIN airports z ON s.destination_airport_id=z.airport_id
            WHERE t.event_kind<>'reservation_record'""")
        db.execute("""CREATE TABLE events AS SELECT person_id,any_value(identity_status) identity_status,origin_id,destination_id,
            any_value(origin_iata) origin_iata,any_value(destination_iata) destination_iata,
            any_value(origin_country) origin_country,any_value(destination_country) destination_country,
            any_value(origin_lat) origin_lat,any_value(origin_lon) origin_lon,
            event_date,dep_utc,min(segment_id) segment_id,bool_and(route_usable) route_usable,
            bool_or(precise) precise,bool_or(boarding) boarding,bit_or(source_mask) source_mask,
            count(*) raw_event_count,
            sha256(to_json([person_id,origin_id,destination_id,event_date::VARCHAR,dep_utc::VARCHAR])) event_id
            FROM raw_events GROUP BY person_id,origin_id,destination_id,event_date,dep_utc""")
        db.execute("""CREATE TABLE scoped AS SELECT 'reported' evidence_scope,* FROM events
            UNION ALL SELECT 'boarding' evidence_scope,* FROM events WHERE boarding""")
        db.execute("""CREATE TABLE event_days AS SELECT evidence_scope,person_id,event_date,
            count(*)=1 OR (bool_and(precise) AND count(distinct dep_utc)=count(*)) safe_order
            FROM scoped GROUP BY 1,2,3""")
        db.execute("""CREATE TABLE sequence AS WITH flags AS (
            SELECT evidence_scope,person_id,bool_and(event_date IS NOT NULL) person_dates_known FROM scoped GROUP BY 1,2)
            SELECT s.*,coalesce(d.safe_order,false) AND route_usable AND f.person_dates_known safe_order,
            f.person_dates_known,
            row_number() OVER(PARTITION BY evidence_scope,person_id ORDER BY event_date,dep_utc NULLS LAST,event_id) seq
            FROM scoped s LEFT JOIN event_days d USING(evidence_scope,person_id,event_date)
            JOIN flags f USING(evidence_scope,person_id)""")
        report["event_population"] = records(db, """SELECT evidence_scope,identity_status,count(*) events,
            count(distinct person_id) profiles,count(*) FILTER(WHERE NOT safe_order) unsafe_sequence_events,
            count(*) FILTER(WHERE route_usable AND event_date IS NOT NULL) usable_route_events,
            count(distinct person_id) FILTER(WHERE route_usable AND event_date IS NOT NULL) usable_route_profiles
            FROM sequence GROUP BY 1,2 ORDER BY 1,2""")
        report["history_lengths"] = records(db, """WITH g AS (SELECT evidence_scope,person_id,identity_status,
            count(*) FILTER(WHERE route_usable AND event_date IS NOT NULL) n,
            count(distinct event_date) FILTER(WHERE route_usable) dates,
            date_diff('day',min(event_date),max(event_date)) span
            FROM sequence GROUP BY 1,2,3) SELECT evidence_scope,identity_status,count(*) profiles,
            count(*) FILTER(WHERE n=1) single_event,count(*) FILTER(WHERE n>=2) two_events,
            count(*) FILTER(WHERE n>=6 AND dates>=3) six_events_three_dates,
            count(*) FILTER(WHERE n>=6 AND span>=90) six_events_90_day_span
            FROM g GROUP BY 1,2 ORDER BY 1,2""")
        db.execute('DROP TABLE scoped')
        db.execute('DROP TABLE event_days')
        checkpoint('event_population')
        db.execute("""CREATE TABLE hits(evidence_scope VARCHAR,criterion_id VARCHAR,person_id VARCHAR,
            identity_status VARCHAR,detail JSON)""")
        db.execute("""CREATE TABLE evidence(evidence_scope VARCHAR,criterion_id VARCHAR,person_id VARCHAR,
            event_id VARCHAR,instance_id VARCHAR)""")
        report["candidate_details"] = {}

        print("C01: short international return series", flush=True)
        db.execute("""CREATE TABLE return_pairs AS WITH n AS (SELECT *,
            lead(origin_id) OVER w next_origin,lead(destination_id) OVER w next_destination,
            lead(event_date) OVER w next_date,lead(dep_utc) OVER w next_utc,
            lead(safe_order) OVER w next_safe,lead(event_id) OVER w next_event
            FROM sequence WINDOW w AS(PARTITION BY evidence_scope,person_id ORDER BY seq))
            SELECT *,sha256(to_json([event_id,next_event])) pair_id FROM n
            WHERE safe_order AND next_safe AND origin_id=next_destination AND destination_id=next_origin
              AND origin_country<>destination_country AND date_diff('day',event_date,next_date) BETWEEN 0 AND 3
              AND (next_date>event_date OR (dep_utc IS NOT NULL AND next_utc>dep_utc))""")
        db.execute("""CREATE TABLE return_windows(evidence_scope VARCHAR,person_id VARCHAR,
            identity_status VARCHAR,origin_id VARCHAR,start_date DATE,return_count INTEGER,
            destination_countries INTEGER,selected_pairs VARCHAR[],countries VARCHAR[])""")
        return_groups=defaultdict(list)
        for pair in records(db,'SELECT * FROM return_pairs'):
            return_groups[(pair['evidence_scope'],pair['person_id'],pair['identity_status'],pair['origin_id'])].append(pair)
        return_windows=[]
        for key, pairs in sorted(return_groups.items()):
            pairs.sort(key=lambda r:r['seq'])
            for start in sorted({r['event_date'] for r in pairs}):
                stop=start+timedelta(days=89)
                selected=[]; last_end=-1
                for pair in pairs:
                    if pair['event_date']>=start and pair['next_date']<=stop and pair['seq']>last_end:
                        selected.append(pair);last_end=pair['seq']+1
                countries=sorted({r['destination_country'] for r in selected})
                return_windows.append((*key,start,len(selected),len(countries),[r['pair_id'] for r in selected],countries))
        if return_windows:
            db.executemany('INSERT INTO return_windows VALUES (?,?,?,?,?,?,?,?,?)',return_windows)
        report["candidate_details"]["C01"] = records(db, """SELECT evidence_scope,identity_status,
            count(*) profiles_with_short_return,
            count(*) FILTER(WHERE max_returns>=3) with_three_returns,
            count(*) FILTER(WHERE qualifies) with_three_returns_three_countries
            FROM (SELECT evidence_scope,person_id,identity_status,max(return_count) max_returns,
                bool_or(return_count>=3 AND destination_countries>=3) qualifies
                FROM return_windows GROUP BY 1,2,3) g GROUP BY 1,2 ORDER BY 1,2""")
        db.execute("""CREATE TABLE selected_return_windows AS SELECT * FROM return_windows
            WHERE return_count>=3 AND destination_countries>=3
            QUALIFY row_number() OVER(PARTITION BY evidence_scope,person_id
                ORDER BY destination_countries DESC,return_count DESC,start_date,origin_id)=1""")
        db.execute("""INSERT INTO hits SELECT evidence_scope,'C01',person_id,identity_status,
            to_json(struct_pack(window_start:=start_date,returns:=return_count,country_count:=destination_countries,
                countries:=countries,base_airport_id:=w.origin_id,base_iata:=a.iata))
            FROM selected_return_windows w LEFT JOIN airports a ON w.origin_id=a.airport_id""")
        db.execute("""INSERT INTO evidence SELECT DISTINCT w.evidence_scope,'C01',w.person_id,e.event_id,p.pair_id
            FROM selected_return_windows w JOIN return_pairs p ON p.evidence_scope=w.evidence_scope AND p.person_id=w.person_id
                AND list_contains(w.selected_pairs,p.pair_id)
            JOIN events e ON e.event_id IN (p.event_id,p.next_event)""")
        db.execute('DROP TABLE return_windows')
        db.execute('DROP TABLE return_pairs')
        checkpoint('C01')

        print("C02: repeated non-overlapping cycles", flush=True)
        lead_columns = ','.join(f'lead({col},{k}) OVER w {col}_{k}'
            for k in range(1,4) for col in ('origin_id','destination_id','event_date','safe_order','event_id','origin_country','destination_country'))
        db.execute("CREATE TABLE cycle_ordered AS SELECT *,"+lead_columns+
            " FROM sequence WINDOW w AS(PARTITION BY evidence_scope,person_id ORDER BY seq)")
        cycle_groups = defaultdict(list)
        for length in (3,4):
            links = " AND ".join(f"destination_id{'_'+str(i) if i else ''}=origin_id_{i+1}" for i in range(length-1))
            safe = " AND ".join('safe_order'+('_'+str(i) if i else '') for i in range(length))
            origin_cols = ['origin_id']+['origin_id_'+str(i) for i in range(1,length)]
            country_cols = ['origin_country']+['origin_country_'+str(i) for i in range(1,length)]
            event_cols = ['event_id']+['event_id_'+str(i) for i in range(1,length)]
            cols = lambda names: ','.join(names)
            query = f"""SELECT evidence_scope,person_id,identity_status,seq,event_date start_date,event_date_{length-1} end_date,
                [{cols(origin_cols)}] airports,[{cols(country_cols)}] countries,[{cols(event_cols)}] event_ids
                FROM cycle_ordered WHERE {links} AND {safe} AND destination_id_{length-1}=origin_id
                  AND len(list_distinct([{cols(origin_cols)}]))={length}
                  AND len(list_distinct([{cols(country_cols)}]))>=2
                  AND date_diff('day',event_date,event_date_{length-1}) BETWEEN 1 AND 30"""
            for row in records(db,query):
                route = tuple(row['airports'])
                key = min(route[i:]+route[:i] for i in range(length))
                cycle_groups[(row['evidence_scope'],row['person_id'],row['identity_status'],key)].append(row)
        cycle_hits=[]; cycle_evidence=[]; cycle_stats=defaultdict(lambda: {'profiles':set(),'two':set(),'three':set(),'sequences':0})
        for (evidence_scope,person,status,key), group in sorted(cycle_groups.items()):
            chosen=[]; last_end=-1
            for row in sorted(group,key=lambda r:r['seq']):
                if row['seq']>last_end:
                    chosen.append(row);last_end=row['seq']+len(key)-1
            st=cycle_stats[(evidence_scope,status)];st['profiles'].add(person);st['sequences']+=len(chosen)
            if len(chosen)>=2:
                st['two'].add(person)
                if len(chosen)>=3:st['three'].add(person)
                detail={'airports':key+(key[0],),'occurrences':len(chosen),
                    'starts':[str(r['start_date']) for r in chosen],
                    'ends':[str(r['end_date']) for r in chosen]}
                cycle_hits.append((evidence_scope,'C02',person,status,json.dumps(detail)))
                for r in chosen:
                    for event_id in r['event_ids']:
                        cycle_evidence.append((evidence_scope,'C02',person,event_id,str(r['seq'])))
        if cycle_hits:
            db.executemany("INSERT INTO hits VALUES (?,?,?,?,?)",cycle_hits)
            db.executemany("INSERT INTO evidence VALUES (?,?,?,?,?)",cycle_evidence)
        report['candidate_details']['C02']=[{'evidence_scope':sc,'identity_status':st,
            'profiles_with_cycle':len(v['profiles']),'with_two_nonoverlapping_cycles':len(v['two']),
            'with_three_nonoverlapping_cycles':len(v['three']),'nonoverlapping_cycles':v['sequences']}
            for (sc,st),v in sorted(cycle_stats.items())]
        db.execute("DROP TABLE cycle_ordered")
        checkpoint('C02')

        print("C03: monthly rate and geography changes",flush=True)
        db.execute("""CREATE TABLE monthly AS SELECT evidence_scope,person_id,identity_status,
            date_trunc('month',event_date)::DATE AS month,count(*) FILTER(WHERE route_usable) n,
            bit_or(source_mask) FILTER(WHERE route_usable) source_mask
            FROM sequence WHERE event_date IS NOT NULL AND person_dates_known GROUP BY 1,2,3,4
            HAVING count(*) FILTER(WHERE route_usable)::DOUBLE/count(*)>=.8""")
        db.execute("""CREATE TABLE comparable_months AS WITH previous AS (
            SELECT m.evidence_scope,m.person_id,m.identity_status,m.month,m.n current_events,
                sum(p.n) prior_events,count(*) prior_active_months,
                bit_and(p.source_mask)&m.source_mask common_sources
            FROM monthly m JOIN monthly p ON m.evidence_scope=p.evidence_scope AND m.person_id=p.person_id
                AND p.month BETWEEN m.month-INTERVAL 3 MONTH AND m.month-INTERVAL 1 MONTH
            WHERE m.month+INTERVAL 1 MONTH <= (SELECT max(event_date)+INTERVAL 1 DAY FROM events)
            GROUP BY m.evidence_scope,m.person_id,m.identity_status,m.month,m.n,m.source_mask),
            measured AS (SELECT *,
                (current_events::DOUBLE/date_diff('day',month,month+INTERVAL 1 MONTH)) /
                  (prior_events::DOUBLE/date_diff('day',month-INTERVAL 3 MONTH,month)) rate_ratio
                FROM previous WHERE prior_active_months=3 AND prior_events>=6 AND common_sources<>0)
            SELECT * FROM measured""")
        db.execute("""CREATE TABLE activity_increases AS SELECT * FROM comparable_months
            WHERE current_events>=6 AND rate_ratio>=3""")
        db.execute("""CREATE TABLE change_countries AS SELECT m.evidence_scope,m.person_id,m.month,
            s.destination_country,count(*) events,
            NOT EXISTS(SELECT 1 FROM sequence p WHERE p.evidence_scope=m.evidence_scope AND p.person_id=m.person_id
                AND p.route_usable AND p.destination_country=s.destination_country
                AND p.event_date>=m.month-INTERVAL 3 MONTH AND p.event_date<m.month) is_new
            FROM activity_increases m JOIN sequence s ON s.evidence_scope=m.evidence_scope AND s.person_id=m.person_id
                AND s.route_usable AND s.event_date>=m.month AND s.event_date<m.month+INTERVAL 1 MONTH
            GROUP BY m.evidence_scope,m.person_id,m.month,s.destination_country""")
        db.execute("""CREATE TABLE change_windows AS WITH g AS (
            SELECT evidence_scope,person_id,month,list_sort(list(destination_country) FILTER(WHERE is_new)) new_countries,
                sum(events) FILTER(WHERE is_new)::DOUBLE/sum(events) new_country_share
            FROM change_countries GROUP BY 1,2,3)
            SELECT m.*,coalesce(g.new_countries,[]::VARCHAR[]) new_countries,
                coalesce(g.new_country_share,0) new_country_share
            FROM activity_increases m JOIN g USING(evidence_scope,person_id,month)""")
        report['candidate_details']['C03']=records(db,"""SELECT evidence_scope,identity_status,
            count(distinct person_id) comparable_profiles,
            count(distinct person_id) FILTER(WHERE current_events>=6 AND rate_ratio>=3) activity_increase_profiles,
            count(distinct person_id) FILTER(WHERE person_id IN(SELECT person_id FROM change_windows w
                WHERE w.evidence_scope=c.evidence_scope AND len(w.new_countries)>=3 AND w.new_country_share>=.5)) combined_profiles
            FROM comparable_months c GROUP BY 1,2 ORDER BY 1,2""")
        db.execute("""INSERT INTO hits SELECT evidence_scope,'C03',person_id,identity_status,
            to_json(struct_pack(month:=month,current_events:=current_events,prior_events:=prior_events,
                daily_rate_ratio:=rate_ratio,new_countries:=new_countries,new_country_share:=new_country_share,
                common_source_mask:=common_sources))
            FROM change_windows WHERE current_events>=6 AND rate_ratio>=3 AND len(new_countries)>=3 AND new_country_share>=.5
            QUALIFY row_number() OVER(PARTITION BY evidence_scope,person_id ORDER BY rate_ratio DESC,month)=1""")
        db.execute("""INSERT INTO evidence SELECT s.evidence_scope,'C03',s.person_id,s.event_id,'four_month_window'
            FROM sequence s JOIN hits h ON s.evidence_scope=h.evidence_scope AND s.person_id=h.person_id AND h.criterion_id='C03'
            WHERE s.event_date>=cast(json_extract_string(h.detail,'$.month') AS DATE)-INTERVAL 3 MONTH
              AND s.event_date<cast(json_extract_string(h.detail,'$.month') AS DATE)+INTERVAL 1 MONTH""")
        checkpoint('C03')

        print("C04: departure conflicts with derived UTC",flush=True)
        # All pairs within six hours, not just adjacent events: inserting a duplicate
        # or an intermediate record must not hide a contradictory pair.
        db.execute("""CREATE TABLE departure_pairs AS WITH pairs AS (
            SELECT a.evidence_scope,a.person_id,a.identity_status,a.event_id event_a,b.event_id event_b,
                a.dep_utc start_utc,b.dep_utc end_utc,a.origin_iata airport_a,b.origin_iata airport_b,
                date_diff('second',a.dep_utc,b.dep_utc)/3600.0 gap_hours,
                2*6371*asin(sqrt(least(1.0,greatest(0.0,
                    pow(sin(radians(b.origin_lat-a.origin_lat)/2),2)+
                    cos(radians(a.origin_lat))*cos(radians(b.origin_lat))*
                    pow(sin(radians(b.origin_lon-a.origin_lon)/2),2))))) distance_km
            FROM sequence a JOIN sequence b ON a.evidence_scope=b.evidence_scope AND a.person_id=b.person_id
                AND (a.dep_utc<b.dep_utc OR (a.dep_utc=b.dep_utc AND a.event_id<b.event_id))
                AND a.origin_id<>b.origin_id AND a.precise AND b.precise AND a.route_usable AND b.route_usable
                AND date_diff('second',a.dep_utc,b.dep_utc)<=21600
            WHERE a.origin_lat BETWEEN -90 AND 90 AND b.origin_lat BETWEEN -90 AND 90
              AND a.origin_lon BETWEEN -180 AND 180 AND b.origin_lon BETWEEN -180 AND 180)
            SELECT * EXCLUDE(gap_hours),abs(gap_hours) gap_hours,
                distance_km/nullif(abs(gap_hours),0) screening_speed FROM pairs""")
        report['candidate_details']['C04']=records(db,"""SELECT evidence_scope,identity_status,
            count(*) close_time_different_origin_pairs,
            count(*) FILTER(WHERE gap_hours=0 AND distance_km>=100) simultaneous_conflicts,
            count(*) FILTER(WHERE gap_hours>0 AND distance_km>=500 AND screening_speed>1500) fast_transfer_pairs,
            count(distinct person_id) FILTER(WHERE (gap_hours=0 AND distance_km>=100)
                OR (gap_hours>0 AND distance_km>=500 AND screening_speed>1500)) candidate_profiles
            FROM departure_pairs GROUP BY 1,2 ORDER BY 1,2""")
        db.execute("""INSERT INTO hits SELECT evidence_scope,'C04',person_id,identity_status,
            to_json(struct_pack(airport_a:=airport_a,airport_b:=airport_b,start_utc:=start_utc,end_utc:=end_utc,
                gap_hours:=gap_hours,distance_km:=distance_km,screening_speed_kmh:=screening_speed,
                event_a:=event_a,event_b:=event_b))
            FROM departure_pairs WHERE (gap_hours=0 AND distance_km>=100)
                OR (gap_hours>0 AND distance_km>=500 AND screening_speed>1500)
            QUALIFY row_number() OVER(PARTITION BY evidence_scope,person_id ORDER BY gap_hours=0 DESC,screening_speed DESC,event_a,event_b)=1""")
        db.execute("""INSERT INTO evidence SELECT h.evidence_scope,'C04',h.person_id,e.event_id,'departure_pair'
            FROM hits h JOIN events e ON e.event_id IN (json_extract_string(h.detail,'$.event_a'),json_extract_string(h.detail,'$.event_b'))
            WHERE h.criterion_id='C04'""")
        checkpoint('C04')

        print("Summarizing overlap and preserving source locators",flush=True)
        db.execute("CREATE TABLE unique_hits AS SELECT DISTINCT evidence_scope,criterion_id,person_id,identity_status FROM hits")
        report['summary']=records(db,"""SELECT h.evidence_scope,h.criterion_id,h.identity_status,count(*) candidate_profiles,
            count(*) FILTER(WHERE p.person_id IS NOT NULL) already_have_signals,
            count(*) FILTER(WHERE p.person_id IS NULL) new_profiles
            FROM unique_hits h LEFT JOIN positive p USING(person_id) GROUP BY 1,2,3 ORDER BY 1,2,3""")
        report['overlap_current_markers']=records(db,"""SELECT h.evidence_scope,h.criterion_id,h.identity_status,c.criterion_id current_marker,
            count(*) profiles FROM unique_hits h JOIN coverage c USING(person_id)
            WHERE c.status='matched' GROUP BY 1,2,3,4 ORDER BY 1,2,3,4""")
        report['union_by_scope']=records(db,"""WITH u AS(SELECT DISTINCT evidence_scope,person_id,identity_status FROM unique_hits)
            SELECT u.evidence_scope,u.identity_status,count(*) candidate_profiles,
                count(*) FILTER(WHERE p.person_id IS NULL) new_profiles FROM u LEFT JOIN positive p USING(person_id)
                GROUP BY 1,2 ORDER BY 1,2""")
        report['pairwise_overlap']=records(db,"""SELECT a.evidence_scope,a.identity_status,a.criterion_id marker_a,b.criterion_id marker_b,
            count(*) profiles FROM unique_hits a JOIN unique_hits b USING(evidence_scope,person_id,identity_status)
            WHERE a.criterion_id<b.criterion_id GROUP BY 1,2,3,4 ORDER BY 1,2,3,4""")
        report['examples']=records(db,"""SELECT h.*,p.display_name,
            p.person_id IN (SELECT person_id FROM positive) already_has_signals
            FROM hits h JOIN persons p USING(person_id)
            QUALIFY row_number() OVER(PARTITION BY evidence_scope,criterion_id,h.identity_status ORDER BY h.person_id,h.detail::VARCHAR)<=3
            ORDER BY evidence_scope,criterion_id,h.identity_status,h.person_id""")
        for row in report['examples']:
            row['detail']=json.loads(row['detail'])
            row['evidence']=records(db,"""SELECT DISTINCT e.instance_id,s.event_date,s.dep_utc,s.origin_iata,s.destination_iata,
                s.origin_country,s.destination_country,r.segment_id,r.travel_id,r.observation_id,r.source_id,
                r.source_record_id,r.locator,r.event_kind FROM evidence e JOIN events s USING(event_id)
                JOIN raw_events r ON r.person_id=s.person_id AND r.origin_id=s.origin_id AND r.destination_id=s.destination_id
                    AND r.event_date=s.event_date AND r.dep_utc IS NOT DISTINCT FROM s.dep_utc
                WHERE e.evidence_scope=? AND e.criterion_id=? AND e.person_id=?
                ORDER BY s.event_date,s.dep_utc,s.origin_iata,r.source_id LIMIT 120""",
                [row['evidence_scope'],row['criterion_id'],row['person_id']])
        db.execute("COPY (SELECT * FROM hits ORDER BY evidence_scope,criterion_id,person_id) TO ? (FORMAT PARQUET,COMPRESSION ZSTD)", [str(output/'candidates.parquet')])
        db.execute("""COPY (SELECT DISTINCT e.*,r.segment_id,r.travel_id,r.observation_id,r.source_id,r.source_record_id,r.locator,
            r.event_kind,r.event_date,r.dep_utc,r.origin_iata,r.destination_iata
            FROM evidence e JOIN events s USING(event_id) JOIN raw_events r
            ON r.person_id=s.person_id AND r.origin_id=s.origin_id AND r.destination_id=s.destination_id
              AND r.event_date=s.event_date AND r.dep_utc IS NOT DISTINCT FROM s.dep_utc)
            TO ? (FORMAT PARQUET,COMPRESSION ZSTD)""",[str(output/'evidence.parquet')])
        report['audit_counts']={'candidate_rows':db.execute('SELECT count(*) FROM hits').fetchone()[0],
            'evidence_rows':db.execute('SELECT count(*) FROM evidence').fetchone()[0]}
    after={str(p.relative_to(ROOT)):digest(p) for p in paths.values()}
    report['all_inputs_unchanged']=before==after
    if before!=after:raise RuntimeError('Input changed during audit')
    report['output_sha256']={name:digest(output/name) for name in ('candidates.parquet','evidence.parquet')}
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
    print(json.dumps({'output':str(output),'summary':report['summary'],
        'candidate_details':report['candidate_details'],'all_inputs_unchanged':report['all_inputs_unchanged']},ensure_ascii=False,indent=2,default=str),flush=True)


if __name__=='__main__':
    main()
