"""Compatible observed journeys, with precision and evidence kept explicit."""
from __future__ import annotations

from datetime import date
from itertools import groupby

import pyarrow as pa
import pyarrow.parquet as pq

from ..normalize.airports import flight_key
from .common import aggregate_ranges, arrow_query, conflict
from .matching import cluster_trips


def prepare_trips(db, geography, rules):
    db.read_parquet(str(geography)).create_view('geography')
    db.create_function('_t13_flight', lambda value: flight_key(value), ['VARCHAR'], 'VARCHAR', null_handling='special')
    db.execute('''CREATE TABLE ticket_contexts AS SELECT nid,value_normalized,min(namespace) namespace,
        count(DISTINCT namespace) n FROM ids WHERE kind='ticket' AND namespace IS NOT NULL
        GROUP BY nid,value_normalized''')
    db.execute('''CREATE TABLE trips AS SELECT g.travel_id,g.observation_id,g.source_id,g.source_record_id,g.locator,
        p.person_id,p.graph_eligible person_graph_eligible,g.event_kind,g.status_raw,
        CASE WHEN g.departure_precision IN ('date','minute','second') AND
          try_cast(left(g.departure_local,10) AS DATE) IS NOT NULL THEN left(g.departure_local,10) END departure_date,
        _t13_flight(g.flight_number_normalized) flight_number,
        CASE WHEN g.origin_status IN ('exact','corrected') THEN g.origin_airport_id END origin_id,
        CASE WHEN g.destination_status IN ('exact','corrected') THEN g.destination_airport_id END destination_id,
        CASE WHEN g.origin_status IN ('exact','corrected') THEN g.origin_iata END origin_iata,
        CASE WHEN g.destination_status IN ('exact','corrected') THEN g.destination_iata END destination_iata,
        g.departure_local,g.departure_precision,g.arrival_local,g.arrival_precision,
        CASE WHEN g.departure_time_origin='source' THEN g.departure_utc END departure_utc,
        CASE WHEN g.arrival_time_origin='source' THEN g.arrival_utc END arrival_utc,
        g.origin_status,g.destination_status,g.departure_utc_status,g.arrival_utc_status,
        g.departure_time_origin,g.arrival_time_origin,g.ticket_number_normalized ticket_value,
        CASE WHEN tc.n=1 THEN tc.namespace WHEN tc.n>1 THEN NULL
          WHEN g.ticket_number_normalized IS NOT NULL THEN to_json(['ticket','source',g.source_id]) END ticket_namespace,
        NULL::VARCHAR codeshare_rule_id,
        (g.departure_utc_status IN ('ambiguous_local_time','nonexistent_local_time','timezone_conflict','invalid_datetime')
         OR g.arrival_utc_status IN ('ambiguous_local_time','nonexistent_local_time','timezone_conflict','invalid_datetime')
         OR g.normalization_status<>'ok') unsafe_time
        FROM geography g JOIN person_links p USING(observation_id) JOIN obs o USING(observation_id)
        LEFT JOIN ticket_contexts tc ON tc.nid=o.nid AND tc.value_normalized=g.ticket_number_normalized''')
    codeshares = []
    seen = set()
    for rule in rules['codeshare_rules']:
        required = ('rule_id','source_id','marketing_flight','operating_flight','origin_iata','destination_iata',
                    'valid_from','valid_to','reason','evidence')
        if not isinstance(rule, dict) or not all(isinstance(rule.get(k),str) and rule[k].strip() for k in required):
            raise ValueError('Codeshare rule needs flight, source, exact route, period, reason and evidence')
        if rule['rule_id'] in seen:
            raise ValueError('Duplicate codeshare rule_id')
        seen.add(rule['rule_id'])
        start, end = date.fromisoformat(rule['valid_from']), date.fromisoformat(rule['valid_to'])
        if start>end or any(flight_key(rule[k]) != rule[k] for k in ('marketing_flight','operating_flight')):
            raise ValueError('Invalid codeshare period or noncanonical full flight number')
        if any(len(rule[k])!=3 or not rule[k].isalpha() or not rule[k].isupper() for k in ('origin_iata','destination_iata')):
            raise ValueError('Codeshare route needs explicit uppercase IATA codes')
        for old in codeshares:
            same_route = all(old[k]==rule[k] for k in ('marketing_flight','origin_iata','destination_iata'))
            same_source = old['source_id']==rule['source_id'] or '*' in (old['source_id'],rule['source_id'])
            if same_route and same_source and max(old['valid_from'],rule['valid_from'])<=min(old['valid_to'],rule['valid_to']):
                raise ValueError('Overlapping codeshare evidence rules')
        codeshares.append(rule)
    db.execute('''CREATE TABLE codeshare_rules(rule_id VARCHAR,source_id VARCHAR,marketing_flight VARCHAR,
        operating_flight VARCHAR,origin_iata VARCHAR,destination_iata VARCHAR,valid_from VARCHAR,valid_to VARCHAR,
        reason VARCHAR,evidence VARCHAR)''')
    if codeshares:
        db.executemany('INSERT INTO codeshare_rules VALUES (?,?,?,?,?,?,?,?,?,?)',
                       [[r[k] for k in ('rule_id','source_id','marketing_flight','operating_flight','origin_iata',
                         'destination_iata','valid_from','valid_to','reason','evidence')] for r in codeshares])
        # Evaluate against original flight numbers once; do not follow unverified alias chains.
        db.execute('''CREATE TABLE codeshare_matches AS SELECT t.travel_id,r.rule_id,r.operating_flight FROM trips t
            JOIN codeshare_rules r ON t.flight_number=r.marketing_flight AND t.origin_iata=r.origin_iata
            AND t.destination_iata=r.destination_iata AND t.departure_date BETWEEN r.valid_from AND r.valid_to
            AND (r.source_id='*' OR r.source_id=t.source_id)''')
        db.execute('''UPDATE trips SET flight_number=m.operating_flight,codeshare_rule_id=m.rule_id
            FROM codeshare_matches m WHERE trips.travel_id=m.travel_id''')
    db.execute('''ALTER TABLE trips ADD COLUMN group_key VARCHAR''')
    db.execute('''UPDATE trips SET group_key=CASE WHEN departure_date IS NOT NULL AND flight_number IS NOT NULL
        THEN sha256(to_json([person_id,departure_date,flight_number])) ELSE 'single:'||travel_id END''')
    db.execute('CREATE TABLE trip_groups AS SELECT group_key,count(*) n FROM trips GROUP BY group_key')


def assign_trips(db, rules, staging, conflict_writer, progress, batch_size):
    limit = rules['max_trip_group_size']
    db.execute('''CREATE TABLE segment_assignments AS SELECT t.travel_id,t.travel_id anchor_travel_id,
        CASE WHEN g.n>? OR t.departure_date IS NULL OR t.flight_number IS NULL OR
             t.origin_id IS NULL OR t.destination_id IS NULL THEN 'ambiguous' ELSE 'accepted' END status,
        'segment.source_record' rule_id,
        CASE WHEN g.n>? THEN 'trip_group_limit' WHEN t.departure_date IS NULL OR t.flight_number IS NULL
             THEN 'incomplete_event_key' WHEN t.origin_id IS NULL OR t.destination_id IS NULL
             THEN 'incomplete_route' ELSE 'observed_event' END reason
        FROM trips t JOIN trip_groups g USING(group_key) WHERE g.n=1 OR g.n>?''', [limit,limit,limit])
    for key,n in db.execute('SELECT group_key,n FROM trip_groups WHERE n>? ORDER BY group_key',[limit]).fetchall():
        conflict(conflict_writer,'segment_block',key,None,'trip_group_limit',dict(member_count=n,limit=limit))
    total = db.execute('SELECT coalesce(sum(n),0) FROM trip_groups WHERE n BETWEEN 2 AND ?',[limit]).fetchone()[0]
    progress.phase('объединение совместимых свидетельств поездок',total)
    cursor = db.execute('''SELECT t.* FROM trips t JOIN trip_groups g USING(group_key)
        WHERE g.n BETWEEN 2 AND ? ORDER BY t.group_key,t.travel_id''',[limit])
    reader = getattr(cursor,'to_arrow_reader',cursor.fetch_record_batch)(batch_size)
    assignment_schema = pa.schema([(n,pa.string()) for n in ('travel_id','anchor_travel_id','status','rule_id','reason')])
    path = staging/'assignments.parquet'
    processed = 0
    with pq.ParquetWriter(path,assignment_schema,compression='zstd') as writer:
        buffer=[]
        def records():
            for batch in reader:
                yield from batch.to_pylist()
        for _, group in groupby(records(),key=lambda r:r['group_key']):
            rows = list(group)
            assignments, conflicts = cluster_trips(rows)
            buffer.extend(assignments)
            if len(buffer)>=batch_size:
                writer.write_table(pa.Table.from_pylist(buffer,schema=assignment_schema))
                buffer.clear()
            for left,right,reason in conflicts:
                conflict(conflict_writer,'segment',left,right,reason,severity='info')
            processed += len(rows)
            progress.tick(processed)
        if buffer:writer.write_table(pa.Table.from_pylist(buffer,schema=assignment_schema))
    db.execute('INSERT INTO segment_assignments SELECT * FROM read_parquet(?)',[str(path)])


def write_segments(db, rules, staging, batch_size, progress=None):
    db.execute('''CREATE TABLE assigned AS SELECT t.*,a.anchor_travel_id,a.status link_status,a.rule_id link_rule_id,a.reason,
        'segment_'||sha256(to_json(['segment','1.0',a.anchor_travel_id])) segment_id
        FROM trips t JOIN segment_assignments a USING(travel_id)''')
    rank = "CASE {s}_precision WHEN 'second' THEN 2 WHEN 'minute' THEN 1 WHEN 'date' THEN 0 ELSE -1 END DESC,travel_id"
    times = []
    for side in ('departure','arrival'):
        order = rank.format(s=side)
        for field in ('local','precision','utc'):
            column = side+'_'+field
            times.append(f'first({column} ORDER BY {order}) FILTER(WHERE {column} IS NOT NULL) {column}')
    # list(DISTINCT ...) cannot spill. Sort groups once, then aggregate bounded
    # ranges, keeping every witness of a segment within the same range.
    columns = ('segment_id,person_id,anchor_travel_id,departure_date,flight_number,origin_id,destination_id,'
               'origin_iata,destination_iata,departure_local,departure_precision,departure_utc,'
               'arrival_local,arrival_precision,arrival_utc,travel_id,person_graph_eligible,'
               'link_status,unsafe_time,event_kind,source_id')
    db.execute('''CREATE TABLE segment_aggregation_input AS SELECT '''+columns+''',
        dense_rank() OVER(ORDER BY segment_id)-1 aggregation_group FROM assigned ORDER BY segment_id''')
    group_count = db.execute('SELECT coalesce(max(aggregation_group)+1,0) FROM segment_aggregation_input').fetchone()[0]
    if progress:
        progress.phase('сборка сегментов по порциям', group_count)
    aggregate_ranges(db, 'segment_results', '''SELECT segment_id,min(person_id) person_id,
        min(anchor_travel_id) anchor_travel_id,min(departure_date) departure_date,min(flight_number) flight_number,
        min(origin_id) origin_airport_id,min(destination_id) destination_airport_id,
        min(origin_iata) origin_iata,min(destination_iata) destination_iata,'''+','.join(times)+''',
        CASE WHEN bool_and(person_graph_eligible) THEN 'accepted' ELSE 'ambiguous' END identity_status,
        CASE WHEN bool_or(link_status='ambiguous' OR coalesce(unsafe_time,false)) THEN 'ambiguous' ELSE 'resolved' END resolution_status,
        CASE WHEN bool_or(list_contains(?::VARCHAR[],event_kind)) THEN 'boarding_record_present' ELSE 'reported_only' END participation_status,
        to_json(list_sort(list(DISTINCT event_kind) FILTER(WHERE event_kind IS NOT NULL))) event_kinds_json,
        to_json(list_sort(list(DISTINCT source_id))) sources_json,
        CASE WHEN count(*)>1 THEN 'segment.compatible_observed_evidence' ELSE 'segment.source_record' END rule_id,
        count(*) observation_count,count(DISTINCT source_id) source_count
        FROM segment_aggregation_input WHERE aggregation_group>=? AND aggregation_group<? GROUP BY segment_id''',
        group_count, params=[rules['graph_event_kinds']], progress=progress)
    db.execute('DROP TABLE segment_aggregation_input')
    db.execute('''ALTER TABLE segment_results ADD COLUMN graph_eligible BOOLEAN''')
    db.execute('''UPDATE segment_results SET graph_eligible=identity_status='accepted' AND resolution_status='resolved'
        AND participation_status='boarding_record_present' AND departure_date IS NOT NULL AND flight_number IS NOT NULL
        AND origin_airport_id IS NOT NULL AND destination_airport_id IS NOT NULL''')
    arrow_query(db,'''SELECT segment_id,person_id,anchor_travel_id,departure_date,flight_number,
        origin_airport_id,destination_airport_id,origin_iata,destination_iata,departure_local,departure_precision,
        arrival_local,arrival_precision,departure_utc,arrival_utc,identity_status,resolution_status,participation_status,
        event_kinds_json,sources_json,rule_id,observation_count,source_count,graph_eligible,
        origin_airport_id IS NOT NULL AND destination_airport_id IS NOT NULL geography_complete,
        departure_utc IS NOT NULL AND arrival_utc IS NOT NULL time_complete FROM segment_results ORDER BY segment_id''',
        staging/'segments.parquet','segments',batch_size)
    arrow_query(db,'''SELECT a.travel_id,a.segment_id,a.person_id,a.observation_id,a.source_id,a.source_record_id,a.locator,
        a.event_kind,a.status_raw,a.link_status status,a.link_rule_id rule_id,a.reason,a.departure_precision,a.arrival_precision,
        a.origin_status,a.destination_status,a.departure_utc_status,a.arrival_utc_status,a.departure_time_origin,a.arrival_time_origin,
        a.codeshare_rule_id,s.graph_eligible FROM assigned a JOIN segment_results s USING(segment_id) ORDER BY a.travel_id''',
        staging/'segment_links.parquet','segment_links',batch_size)
