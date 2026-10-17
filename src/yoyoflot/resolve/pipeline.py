"""T13 pipeline over verified canonical/T11 snapshots; readers are never run."""
from __future__ import annotations

import json
import tempfile
import time
from array import array
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ..normalize.canonical import SCHEMAS as CANONICAL_SCHEMAS, publish
from ..normalize.geography import EXTRA_FIELDS
from ..normalize.locking import OutputLock
from ..normalize.values import file_hash
from . import VERSION
from .blocking import build_blocks, candidate_rows
from .common import AGGREGATION_GROUPS, Rules, Progress, Writer, SCHEMAS, arrow_query, conflict, dumps, stable_id
from .matching import Components, assess_pair
from .review import sample_pairs, reserve_review_copy
from .segments import prepare_trips, assign_trips, write_segments

SOURCES = {'boarding_csv','boardingpass_xlsx','forum_json','pointz_xml','sirena_fixed','skyteam_yaml'}


def verify_inputs(root, rules, progress):
    root = Path(root).resolve()
    files, fingerprints, reports = {}, {str(rules.path.resolve()):rules.sha256}, {}
    for manifest_name, names in (
        ('canonical_manifest.json',('observations.parquet','identifiers.parquet','travel_observations.parquet','canonical_report.json')),
        ('t11_manifest.json',('travel_geography.parquet','geography_report.json'))):
        path = root/manifest_name
        manifest = json.loads(path.read_text(encoding='utf-8'))
        fingerprints[str(path)] = file_hash(path)
        for name in names:
            progress.phase('проверка входа '+name)
            path = root/name
            actual = file_hash(path)
            if actual != manifest.get('files',{}).get(name):
                raise ValueError('Input does not match its published manifest: '+str(path))
            fingerprints[str(path)] = actual
            files[name] = path
            if name.endswith('.json'):
                reports[name] = json.loads(path.read_text(encoding='utf-8'))
    for table in ('observations','identifiers','travel_observations'):
        actual = pq.read_schema(files[table+'.parquet'])
        if actual.metadata is None or actual.metadata.get(b'canonical_contract_version') != b'1.0':
            raise ValueError('T13 requires canonical T12 input: '+table)
        for field in CANONICAL_SCHEMAS[table]:
            if field.name not in actual.names or actual.field(field.name).type != field.type:
                raise ValueError('Incompatible T12 field: '+table+'.'+field.name)
        if reports['canonical_report.json'].get('counts',{}).get(table) != pq.ParquetFile(files[table+'.parquet']).metadata.num_rows:
            raise ValueError('Canonical report row count mismatch: '+table)
    geo = pq.read_schema(files['travel_geography.parquet'])
    if (geo.metadata or {}).get(b'geography_contract_version') != b'1.0':
        raise ValueError('Unsupported T11 geography contract')
    for field in CANONICAL_SCHEMAS['travel_observations']:
        if field.name not in geo.names or geo.field(field.name).type != field.type:
            raise ValueError('T11 input must retain T12 columns: '+field.name)
    for name, typ in EXTRA_FIELDS.items():
        if name not in geo.names or geo.field(name).type != typ:
            raise ValueError('Missing/incompatible T11 field: '+name)
    canonical_hash = fingerprints[str(files['travel_observations.parquet'])]
    if not any(value==canonical_hash and name.replace('\\','/').endswith('/travel_observations.parquet')
               for name,value in reports['geography_report.json'].get('inputs',{}).items()):
        raise ValueError('T11 was not built from this canonical travel_observations snapshot; run T11 on T12 output')
    if reports['canonical_report.json'].get('status') not in ('complete','complete_with_errors'):
        raise ValueError('Canonical input is not a completed snapshot')
    if reports['geography_report.json'].get('status') != 'complete':
        raise ValueError('T11 input is not a completed snapshot')
    return files, fingerprints, reports


def validate_relations(db, files):
    for table in ('observations','identifiers','travel_observations','travel_geography'):
        db.read_parquet(str(files[table+'.parquet'])).create_view('input_'+table)
        pk = 'observation_id' if table=='observations' else 'identifier_id' if table=='identifiers' else 'travel_id'
        missing = db.execute(f'''SELECT count(*) FROM input_{table} WHERE {pk} IS NULL OR {pk}=''
            OR source_id IS NULL OR source_id='' OR source_record_id IS NULL OR source_record_id=''
            OR locator IS NULL OR locator='' ''').fetchone()[0]
        duplicate = db.execute(f'SELECT count(*)-count(DISTINCT {pk}) FROM input_{table}').fetchone()[0]
        if missing or duplicate:
            raise ValueError('Invalid/repeated identity or provenance in '+table)
    for child in ('identifiers','travel_observations'):
        orphan = db.execute(f'''SELECT count(*) FROM input_{child} c ANTI JOIN input_observations p
            ON c.observation_id=p.observation_id AND c.source_id=p.source_id''').fetchone()[0]
        if orphan:
            raise ValueError('Invalid observation reference in '+child)
    columns = ('observation_id','source_id','source_record_id','locator','origin_raw','destination_raw',
               'flight_number_normalized','departure_local','departure_precision','arrival_local','arrival_precision')
    condition = ' OR '.join(f'g.{c} IS DISTINCT FROM t.{c}' for c in columns)
    mismatch = db.execute('''SELECT count(*) FROM input_travel_geography g FULL JOIN input_travel_observations t
        USING(travel_id) WHERE g.travel_id IS NULL OR t.travel_id IS NULL OR '''+condition).fetchone()[0]
    if mismatch:
        raise ValueError('T11 rows differ from canonical travel observations')
    invalid_dob = db.execute('''SELECT count(*) FROM input_observations
        WHERE birth_date IS NOT NULL AND (try_cast(birth_date AS DATE) IS NULL OR
          NOT regexp_full_match(birth_date,'[0-9]{4}-[0-9]{2}-[0-9]{2}'))''').fetchone()[0]
    if invalid_dob:
        raise ValueError('Noncanonical birth_date in T12 input')
    invalid_key = db.execute('''SELECT count(*) FROM input_identifiers WHERE identifier_match_key IS NOT NULL AND
        (NOT json_valid(identifier_match_key) OR identifier_namespace IS NULL OR value_normalized IS NULL OR value_normalized='' OR
         json_extract_string(identifier_match_key,'$[0]') IS DISTINCT FROM identifier_namespace OR
         json_extract_string(identifier_match_key,'$[1]') IS DISTINCT FROM value_normalized)''').fetchone()[0]
    if invalid_key:
        raise ValueError('Identifier match key disagrees with namespace/value')
    invalid_namespace=db.execute('''SELECT count(*) FROM input_identifiers WHERE identifier_match_key IS NOT NULL AND
        (NOT json_valid(identifier_namespace) OR json_array_length(identifier_namespace)<>3 OR namespace_status IS NULL OR
         json_extract_string(identifier_namespace,'$[2]') IS NULL OR json_extract_string(identifier_namespace,'$[2]')='' OR
         json_extract_string(identifier_namespace,'$[0]') IS DISTINCT FROM kind OR
         (namespace_status='programme_scoped' AND (kind<>'loyalty_card' OR programme_normalized IS NULL OR programme_normalized='' OR
           json_extract_string(identifier_namespace,'$[1]') IS DISTINCT FROM 'programme' OR
           json_extract_string(identifier_namespace,'$[2]') IS DISTINCT FROM programme_normalized)) OR
         (namespace_status='source_scoped' AND (kind NOT IN ('document','ticket') OR
           json_extract_string(identifier_namespace,'$[1]') IS DISTINCT FROM 'source' OR
           json_extract_string(identifier_namespace,'$[2]') IS DISTINCT FROM source_id)) OR
         (namespace_status='confirmed_override' AND (kind NOT IN ('document','ticket') OR
           json_extract_string(identifier_namespace,'$[1]') IS DISTINCT FROM 'confirmed')) OR
         namespace_status NOT IN ('programme_scoped','source_scoped','confirmed_override'))''').fetchone()[0]
    if invalid_namespace:raise ValueError('Invalid identifier namespace/programme context')


def match_people(db, count, total_pairs, rules, staging, conflicts, progress, batch_size):
    dobs = array('i')
    cursor = db.execute("SELECT cast(coalesce(date_diff('day',DATE '0001-01-01',try_cast(birth_date AS DATE))+1,0) AS INTEGER) dob FROM obs ORDER BY nid")
    reader = getattr(cursor,'to_arrow_reader',cursor.fetch_record_batch)(batch_size)
    for batch in reader:
        dobs.extend(batch.column(0).to_pylist())
    components = Components(count,dobs,rules['max_component_size'],rules['max_name_variants'],
                            rules['name_similarity_threshold'],rules['name_conflict_threshold'])
    progress.phase('сопоставление пассажиров и проверка компонентов',total_pairs)
    with Writer(staging/'pair_decisions.parquet','person_pairs',batch_size) as writer:
        for n,(row,left,right) in enumerate(candidate_rows(db,batch_size),1):
            status, reason, features = assess_pair(left,right,row,rules)
            features['blocking_keys'] = json.loads(row['keys_json'])
            features['direct_status'],features['direct_rule'] = status,reason
            if status=='accepted':
                ra,rb = components.find(row['left_n']),components.find(row['right_n'])
                component_reason = components.merge(row['left_n'],row['right_n'],left['documents_json'],right['documents_json'],left,right)
                if component_reason:
                    features['component_constraints'] = dict(left_anchor_node_index=ra,right_anchor_node_index=rb,
                        left_birth_date_ordinal=int(components.dobs[ra]),right_birth_date_ordinal=int(components.dobs[rb]))
                    status = 'ambiguous' if component_reason.endswith('_limit') or component_reason.endswith('_uncertain') else 'rejected'
                    reason = component_reason
            if status=='rejected' and row['shared_identifier']:
                components.mark(row['left_n'],row['right_n'])
            if reason.startswith('component_') or (status=='rejected' and row['shared_identifier']):
                conflict(conflicts,'person',left['observation_id'],right['observation_id'],reason,features)
            evidence = {side:{k:obj[k] for k in ('observation_id','source_id','source_record_id','locator')}
                        for side,obj in [('left',left),('right',right)]}
            for side,obj in [('left',left),('right',right)]:
                evidence[side]['identifiers'] = json.loads(obj['identifiers_json'])
            writer.add(dict(pair_id=stable_id('pair',left['observation_id'],right['observation_id']),
                left_observation_id=left['observation_id'],right_observation_id=right['observation_id'],
                status=status,rule_id=VERSION+':'+reason,reason=reason,name_similarity=features['name_similarity'],
                features_json=dumps(features),evidence_json=dumps(evidence)))
            progress.tick(n)
    progress.phase('сборка стабильных person_id и статусов неопределённости')
    map_schema = pa.schema([('nid',pa.int64()),('root',pa.int64()),('uncertain',pa.bool_())])
    with pq.ParquetWriter(staging/'components.parquet',map_schema,compression='zstd') as writer:
        for start in range(0,count,batch_size):
            records=[]
            for node in range(start,min(count,start+batch_size)):
                root=components.find(node)
                records.append(dict(nid=node,root=root,uncertain=bool(components.uncertain[root])))
            writer.write_table(pa.Table.from_pylist(records,schema=map_schema))
    del components
    db.execute('CREATE TABLE membership AS SELECT * FROM read_parquet(?)',[str(staging/'components.parquet')])
    db.execute('''CREATE TABLE pairs_final AS SELECT p.* EXCLUDE(status,rule_id,reason),
        CASE WHEN p.status='ambiguous' AND ma.root=mb.root THEN 'accepted' ELSE p.status END status,
        CASE WHEN p.status='ambiguous' AND ma.root=mb.root THEN 'resolution:1.0:component_support' ELSE p.rule_id END rule_id,
        CASE WHEN p.status='ambiguous' AND ma.root=mb.root THEN 'component_support' ELSE p.reason END reason,
        ma.root left_root,mb.root right_root
        FROM read_parquet(?) p JOIN obs a ON p.left_observation_id=a.observation_id
        JOIN obs b ON p.right_observation_id=b.observation_id JOIN membership ma ON ma.nid=a.nid
        JOIN membership mb ON mb.nid=b.nid''',[str(staging/'pair_decisions.parquet')])
    arrow_query(db,'''SELECT pair_id,left_observation_id,right_observation_id,status,rule_id,reason,
        features_json,evidence_json,name_similarity FROM pairs_final ORDER BY pair_id''',
        staging/'person_pairs.parquet','person_pairs',batch_size)
    db.read_parquet(str(staging/'person_pairs.parquet')).create_view('person_pairs')
    db.execute('''CREATE TABLE limited_name_keys AS SELECT b.key_hash FROM blocks b
        JOIN block_members bm USING(kind,key_hash) JOIN membership m USING(nid)
        WHERE b.kind='name_dob' AND b.member_count>? GROUP BY b.key_hash HAVING count(DISTINCT m.root)>1''',
        [rules['max_name_block_size']])
    db.execute('''CREATE TABLE uncertain_roots AS SELECT DISTINCT root FROM membership WHERE uncertain
        UNION SELECT root FROM membership JOIN poisoned_nodes USING(nid)
        UNION SELECT root FROM membership JOIN block_members USING(nid)
          JOIN limited_name_keys USING(key_hash) WHERE kind='name_dob'
        UNION SELECT left_root FROM pairs_final WHERE status='ambiguous'
        UNION SELECT right_root FROM pairs_final WHERE status='ambiguous'
        UNION SELECT DISTINCT m.root FROM membership m ANTI JOIN
          (SELECT DISTINCT m2.root FROM ids i JOIN membership m2 USING(nid) JOIN obs o ON o.nid=i.nid
           WHERE i.match_key IS NOT NULL AND i.normalization_status='ok' AND
             ((i.kind='loyalty_card' AND i.namespace_status='programme_scoped') OR
              (i.kind='document' AND (i.namespace_status='confirmed_override' OR
               (i.namespace_status='source_scoped' AND o.name_comparison IS NOT NULL AND o.birth_date IS NOT NULL))))) strong
          ON m.root=strong.root''')
    db.execute('''CREATE TABLE identities AS SELECT o.*,m.root,
        'person_'||sha256(to_json(['person','1.0',anchor.observation_id])) person_id,
        anchor.observation_id anchor_observation_id,u.root IS NULL graph_eligible
        FROM obs o JOIN membership m USING(nid) JOIN obs anchor ON anchor.nid=m.root
        LEFT JOIN uncertain_roots u ON u.root=m.root''')
    arrow_query(db,'''SELECT person_id,min(anchor_observation_id) anchor_observation_id,
        first(name_raw ORDER BY observation_id) FILTER(WHERE name_raw IS NOT NULL AND name_raw<>'') display_name,
        min(birth_date) birth_date,CASE WHEN bool_and(graph_eligible) THEN 'accepted' ELSE 'ambiguous' END identity_status,
        CASE WHEN count(*)>1 THEN 'identity.component_constraints' ELSE 'identity.source_observation' END rule_id,
        count(*) observation_count,count(DISTINCT source_id) source_count,bool_and(graph_eligible) graph_eligible
        FROM identities GROUP BY person_id ORDER BY person_id''',staging/'persons.parquet','persons',batch_size)
    arrow_query(db,'''SELECT i.observation_id,i.person_id,i.source_id,i.source_record_id,i.locator,
        CASE WHEN i.graph_eligible THEN 'accepted' ELSE 'ambiguous' END status,
        CASE WHEN NOT i.graph_eligible THEN 'unresolved' WHEN p.observation_count>1 THEN 'strong_identifier' ELSE 'source_only' END confidence,
        p.rule_id,i.nid node_index,i.graph_eligible FROM identities i JOIN read_parquet(?) p USING(person_id)
        ORDER BY i.observation_id''',staging/'person_links.parquet','person_links',batch_size,[str(staging/'persons.parquet')])
    db.read_parquet(str(staging/'person_links.parquet')).create_view('person_links')


def final_checks(db, files, staging):
    counts = {name:pq.ParquetFile(staging/(name+'.parquet')).metadata.num_rows for name in SCHEMAS}
    nobs=pq.ParquetFile(files['observations.parquet']).metadata.num_rows
    ntrips=pq.ParquetFile(files['travel_observations.parquet']).metadata.num_rows
    if counts['person_links']!=nobs or counts['segment_links']!=ntrips:
        raise RuntimeError('T13 input/link row balance failed')
    for table,key in [('persons','person_id'),('person_links','observation_id'),('person_pairs','pair_id'),
                      ('segments','segment_id'),('segment_links','travel_id'),('conflicts','conflict_id')]:
        count,unique=db.execute(f'SELECT count(*),count(DISTINCT {key}) FROM read_parquet(?)',[str(staging/(table+'.parquet'))]).fetchone()
        if count!=unique:raise RuntimeError('T13 output primary key not unique: '+table)
    for child,fk,parent,pk in [('person_links','person_id','persons','person_id'),
                              ('segments','person_id','persons','person_id'),('segment_links','segment_id','segments','segment_id')]:
        orphan=db.execute(f'''SELECT count(*) FROM read_parquet(?) c ANTI JOIN read_parquet(?) p
            ON c.{fk}=p.{pk}''',[str(staging/(child+'.parquet')),str(staging/(parent+'.parquet'))]).fetchone()[0]
        if orphan:raise RuntimeError('T13 output foreign key failed: '+child)
    bad_component=db.execute('''SELECT count(*) FROM (SELECT root,count(DISTINCT birth_date) n
        FROM identities GROUP BY root HAVING count(DISTINCT birth_date)>1)''').fetchone()[0]
    if bad_component:raise RuntimeError('Incompatible DOB values in a resolved component')
    link_mismatch=db.execute('''SELECT count(*) FROM read_parquet(?) l JOIN input_travel_observations t USING(travel_id)
        JOIN person_links p ON p.observation_id=t.observation_id JOIN segment_results s ON s.segment_id=l.segment_id
        WHERE l.person_id<>p.person_id OR l.person_id<>s.person_id OR l.observation_id<>t.observation_id
          OR l.source_id<>t.source_id OR l.source_record_id<>t.source_record_id OR l.locator<>t.locator''',
        [str(staging/'segment_links.parquet')]).fetchone()[0]
    if link_mismatch:raise RuntimeError('T13 segment/person provenance mismatch')
    return counts,dict(input_observations=nobs,person_links=counts['person_links'],persons=counts['persons'],
                       observations_combined=nobs-counts['persons'],input_travels=ntrips,
                       segment_links=counts['segment_links'],segments=counts['segments'],
                       duplicate_travel_witnesses_combined=ntrips-counts['segments'])


def run(input_path, output, config, review_output=None, batch_size=4000, memory_limit='1GB', require_all=False, progress=None):
    started=time.perf_counter()
    if batch_size<1:raise ValueError('batch_size must be positive')
    input_path,output=Path(input_path).resolve(),Path(output).resolve()
    if not input_path.is_dir():raise FileNotFoundError(input_path)
    output.mkdir(parents=True,exist_ok=True)
    rules=Rules(config)
    review_output=Path(review_output) if review_output is not None else output.parent/'review'
    review_created=None
    published=False
    with Progress(progress) as meter, OutputLock(output/'.resolution.lock',label='T13'):
        files,fingerprints,reports=verify_inputs(input_path,rules,meter)
        fingerprint=stable_id('run',sorted(fingerprints.values()))
        with tempfile.TemporaryDirectory(prefix='.resolution-',dir=output) as temp:
            staging=Path(temp)
            db=duckdb.connect(str(staging/'work.duckdb'))
            try:
                db.execute('SET memory_limit=?',[memory_limit])
                db.execute('SET threads=1')
                db.execute('SET preserve_insertion_order=false')
                db.execute('SET temp_directory=?',[str(staging/'spill')])
                meter.phase('проверка ключей и соответствия T11/T12')
                validate_relations(db,files)
                present={r[0] for r in db.execute('SELECT DISTINCT source_id FROM input_observations').fetchall()}
                missing=sorted(SOURCES-present)
                if require_all and missing:raise ValueError('Missing personal sources: '+', '.join(missing))
                meter.phase('подготовка наблюдений и свидетельств')
                nobs,npairs,upper=build_blocks(db,files['observations.parquet'],files['identifiers.parquet'],rules,staging,meter)
                with Writer(staging/'conflicts.parquet','conflicts',batch_size) as conflicts:
                    match_people(db,nobs,npairs,rules,staging,conflicts,meter,batch_size)
                    meter.phase('подготовка наблюдений поездок и подтверждённых codeshare')
                    prepare_trips(db,files['travel_geography.parquet'],rules)
                    assign_trips(db,rules,staging,conflicts,meter,batch_size)
                    meter.phase('сборка segments и полных segment_links')
                    write_segments(db,rules,staging,batch_size,meter)
                meter.phase('выборка пар для независимого разбора')
                review=sample_pairs(db,rules,staging/'resolution_review_sample.csv',fingerprint)
                review_path,created=reserve_review_copy(staging/'resolution_review_sample.csv',review_output,fingerprint)
                if created:review_created=review_path
                review['editable_csv']=str(review_path.resolve())
                meter.phase('финальная проверка балансов и происхождения')
                counts,balances=final_checks(db,files,staging)
                pair_statuses=dict(db.execute('SELECT status,count(*) FROM person_pairs GROUP BY status').fetchall())
                identity_statuses=dict(db.execute("SELECT CASE WHEN graph_eligible THEN 'accepted' ELSE 'ambiguous' END,count(*) FROM identities GROUP BY 1").fetchall())
                segment_statuses=dict(db.execute('SELECT resolution_status,count(*) FROM segment_results GROUP BY resolution_status').fetchall())
                graph_segments=db.execute('SELECT count(*) FROM segment_results WHERE graph_eligible').fetchone()[0]
                source_coverage=[dict(source_id=s,observations=n,confident_observations=c) for s,n,c in
                    db.execute('SELECT source_id,count(*),count(*) FILTER(WHERE graph_eligible) FROM identities GROUP BY source_id ORDER BY source_id').fetchall()]
                meter.phase('заключительная проверка входных хешей')
                for path,before in fingerprints.items():
                    if file_hash(path)!=before:raise RuntimeError('Input changed during T13: '+path)
                report=dict(status='complete',acceptance='pending_independent_100_pair_review_and_signoff',
                    version=VERSION,run_fingerprint=fingerprint,config_sha256=rules.sha256,inputs=fingerprints,
                    counts=counts,balances=balances,person_pair_statuses=pair_statuses,person_link_statuses=identity_statuses,
                    segment_statuses=segment_statuses,graph_eligible_segments=graph_segments,source_coverage=source_coverage,
                    personal_sources_present=sorted(SOURCES&present),personal_sources_missing=missing,
                    candidate_upper_bound=upper,review=review,memory_limit=memory_limit,batch_size=batch_size,
                    duckdb_threads=1,aggregation_batch_groups=AGGREGATION_GROUPS,
                    upstream=dict(canonical_status=reports['canonical_report.json']['status'],
                        canonical_normalization_errors=reports['canonical_report.json'].get('normalization_errors'),
                        reader_error_severities=reports['canonical_report.json'].get('source_error_severities'),
                        schedule_sources=reports['geography_report.json'].get('schedule_sources',[])),
                    seconds=round(time.perf_counter()-started,3),limitations=[
                        'No independent human review has been fabricated; CSV reviewer fields start blank.',
                        'Similarity is a string feature, not a match probability.',
                        'Stable IDs mean same snapshot/rules, independent of input ordering and batch size; incremental merges may change anchors.',
                        'Uncertain identities and weak flight evidence are excluded from confident graph participation.',
                        'Large blocks use a stable spanning star; oversized name/trip groups stay explicitly unresolved.',
                        'Only observed geography/time is used for event deduplication; schedule inference is not observed travel.'])
                summary=(f"Итог T13\nНаблюдения людей: {nobs}\nЛюди: {counts['persons']}\n"
                         f"Кандидаты пар: {counts['person_pairs']}\nРешения пар: {dumps(pair_statuses)}\n"
                         f"Наблюдения поездок: {balances['input_travels']}\nСегменты: {counts['segments']}\n"
                         f"Объединено повторных свидетельств поездок: {balances['duplicate_travel_witnesses_combined']}\n"
                         f"Сегменты для уверенного графа: {graph_segments}\nКонфликты: {counts['conflicts']}\n"
                         f"Выборка для ревью: {sum(review['selected'].values())} / {sum(review['target'].values())}\n"
                         f"Ручная проверка: ожидается; {review_path.resolve()}\n"
                         f"Статус: complete; время: {report['seconds']} с\nРезультаты: {output}\n")
                (staging/'resolution_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                (staging/'resolution_summary.txt').write_text(summary,encoding='utf-8')
                names=[t+'.parquet' for t in SCHEMAS]+['resolution_review_sample.csv','resolution_report.json','resolution_summary.txt']
                manifest=dict(version=VERSION,run_fingerprint=fingerprint,files={n:file_hash(staging/n) for n in names})
                (staging/'resolution_manifest.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
                publish(staging,output,names+['resolution_manifest.json'])
                published=True
                return report,summary
            except BaseException as exc:
                diagnostic=dict(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',version=VERSION,
                    exception_type=type(exc).__name__,message=str(exc),phase=meter.name,
                    memory_limit=memory_limit,duckdb_threads=1,previous_outputs_preserved=not published)
                (output/'resolution_last_failure.json').write_text(json.dumps(diagnostic,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                raise
            finally:
                db.close()
                if review_created is not None and not published:review_created.unlink(missing_ok=True)
