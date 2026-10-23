"""Build a reproducible analytical product exclusively from supplied T13 data.

DuckDB performs bounded disk-backed joins/aggregation. No synthetic population,
source-data rewrite, inferred boarding, or guessed coordinates is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tempfile
import time
from pathlib import Path

import duckdb
import pyarrow as pa

from ..normalize.airports import AirportReference
from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .config import TITLES, load
from .input import verify

VERSION = '2.0'
FEATURES = ['trip_rate_30_days', 'quick_return_share', 'repeated_route_share']


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n', encoding='utf-8')


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _source_provenance(paths, origin):
    """Describe verified lineage without claiming an independent human audit."""
    result = {
        'origin': origin,
        'input_mode': 'supplied_t13' if origin == 'supplied_develop_t13' else 't13',
        'raw_reingestion_verified': False,
        'independent_human_review': 'pending',
    }
    core_path = paths.get('t16/run_manifest.json')
    if core_path is not None:
        core = json.loads(Path(core_path).read_text(encoding='utf-8'))
        result.update({
            'core_run_id': core['run_id'],
            'core_status': core['status'],
            'input_mode': core['input_mode'],
            'configured_sources': core.get('configured_sources', []),
            'raw_reingestion_verified': core['input_mode'] == 'raw'
                and core['status'] in {'complete', 'complete_with_limitations'}
                and core.get('options', {}).get('require_all') is True
                and core.get('options', {}).get('allow_source_errors') is False,
        })
        repair_path = paths.get('t16/repair_manifest.json')
        if repair_path is not None:
            repair = json.loads(Path(repair_path).read_text(encoding='utf-8'))
            result['auxiliary_repair'] = {
                'version': repair['version'],
                'status': repair['status'],
                'parent_core_run_id': repair['parent_run_id'],
                'parent_manifest_sha256': repair['parent_manifest_sha256'],
                'repair_manifest_sha256': fingerprint(repair_path),
                'scope': 'workbook_coverage.csv only; canonical and resolution tables unchanged',
            }
    return result


def _rows(db, sql, params=None):
    cur = db.execute(sql, params or [])
    return [dict(zip([c[0] for c in cur.description], row)) for row in cur.fetchall()]


def validate_tables(db):
    for table, key in [('persons', 'person_id'), ('segments', 'segment_id')]:
        if db.execute(f'SELECT count(*)-count(DISTINCT {key}) FROM {table}').fetchone()[0]:
            raise ValueError(f'Duplicate/null keys in {table}')
    if db.execute('SELECT count(*) FROM segments s ANTI JOIN persons p USING(person_id)').fetchone()[0]:
        raise ValueError('Segments without a person')
    # Invalid dates must not silently move outside the analytic window.
    if db.execute("SELECT count(*) FROM segments WHERE departure_date IS NOT NULL AND try_cast(departure_date AS DATE) IS NULL").fetchone()[0]:
        raise ValueError('Invalid non-null T13 departure dates')


def _features(db, cfg, window):
    db.execute('''CREATE TABLE dated AS SELECT *,try_cast(departure_date AS DATE) dep_date,
        count(*) OVER(PARTITION BY person_id,departure_date) day_count
        FROM segments''')
    # Unknown dates remain in denominators. Day-only ties never imply sequence.
    db.execute('''CREATE TABLE ordered AS SELECT *,
        lag(segment_id) OVER w prev_segment_id, lag(dep_date) OVER w prev_date,
        lag(day_count) OVER w prev_day_count, lag(origin_airport_id) OVER w prev_origin,
        lag(destination_airport_id) OVER w prev_destination,
        lag(resolution_status) OVER w prev_resolution, lag(identity_status) OVER w prev_identity
        FROM dated WHERE dep_date IS NOT NULL
        WINDOW w AS(PARTITION BY person_id ORDER BY dep_date,segment_id)''')
    db.execute('''CREATE TABLE pairs AS SELECT *,
        day_count=1 AND prev_day_count=1 AND origin_airport_id IS NOT NULL AND destination_airport_id IS NOT NULL
        AND prev_origin IS NOT NULL AND prev_destination IS NOT NULL
        AND identity_status='accepted' AND prev_identity='accepted'
        AND resolution_status='resolved' AND prev_resolution='resolved' comparable,
        origin_airport_id=prev_destination AND destination_airport_id=prev_origin
        AND date_diff('day',prev_date,dep_date) BETWEEN 1 AND 2 quick_return
        FROM ordered WHERE prev_segment_id IS NOT NULL''')
    db.execute('''CREATE TABLE aggregate AS SELECT person_id,count(*) total_segments,
        count(dep_date) dated_segments,
        count(*) FILTER(WHERE dep_date IS NOT NULL AND origin_airport_id IS NOT NULL AND destination_airport_id IS NOT NULL
            AND identity_status='accepted' AND resolution_status='resolved') route_segments,
        count(DISTINCT (origin_airport_id,destination_airport_id)) FILTER(WHERE dep_date IS NOT NULL
            AND origin_airport_id IS NOT NULL AND destination_airport_id IS NOT NULL
            AND identity_status='accepted' AND resolution_status='resolved') unique_routes,
        count(*) FILTER(WHERE identity_status<>'accepted' OR resolution_status<>'resolved'
            OR identity_status IS NULL OR resolution_status IS NULL) uncertain_segments
        FROM dated GROUP BY person_id''')
    db.execute('''CREATE TABLE pair_counts AS SELECT person_id,
        count(*) FILTER(WHERE comparable) comparable_pairs,
        count(*) FILTER(WHERE comparable AND quick_return) quick_return_pairs
        FROM pairs GROUP BY person_id''')
    days = window['days']
    db.execute('''CREATE TABLE features AS SELECT p.person_id,p.display_name,p.identity_status,
        coalesce(a.total_segments,0) total_segments,coalesce(a.dated_segments,0) dated_segments,
        coalesce(a.route_segments,0) route_segments,coalesce(a.unique_routes,0) unique_routes,
        coalesce(a.uncertain_segments,0) uncertain_segments,
        coalesce(c.comparable_pairs,0) comparable_pairs,coalesce(c.quick_return_pairs,0) quick_return_pairs,
        a.dated_segments*30.0/? trip_rate_30_days,
        c.quick_return_pairs::DOUBLE/nullif(c.comparable_pairs,0) quick_return_share,
        1.0-a.unique_routes::DOUBLE/nullif(a.route_segments,0) repeated_route_share
        FROM persons p LEFT JOIN aggregate a USING(person_id) LEFT JOIN pair_counts c USING(person_id)''', [days or None])
    known = cfg['coverage']['min_known_fraction']
    min_days = cfg['coverage']['min_window_days']
    settings = [dict(criterion_id=cid, title_ru=TITLES[cid], **cfg['criteria'][cid]) for cid in TITLES]
    db.register('settings_arrow', pa.Table.from_pylist(settings))
    db.execute('CREATE TABLE settings AS SELECT * FROM settings_arrow')
    db.execute('''CREATE TABLE explanations AS WITH facts AS (
        SELECT f.*,s.*,
        CASE s.criterion_id WHEN 'M01' THEN trip_rate_30_days WHEN 'M02' THEN quick_return_share ELSE repeated_route_share END AS value,
        CASE s.criterion_id WHEN 'M01' THEN dated_segments WHEN 'M02' THEN comparable_pairs ELSE route_segments END used_observations,
        CASE WHEN identity_status IS DISTINCT FROM 'accepted' THEN 'Неоднозначная идентичность профиля'
             WHEN ? < ? THEN 'Недостаточная длительность общего окна датасета'
             WHEN dated_segments::DOUBLE/nullif(total_segments,0) < ? OR dated_segments=0 THEN 'Недостаточно известных дат'
             WHEN criterion_id='M01' AND uncertain_segments>0 THEN 'Есть неоднозначные сегменты'
             WHEN criterion_id='M02' AND (dated_segments<total_segments OR comparable_pairs::DOUBLE/nullif(dated_segments-1,0) < ?)
                THEN 'Соседство поездок или маршруты неоднозначны'
             WHEN criterion_id='M03' AND route_segments::DOUBLE/nullif(dated_segments,0) < ? THEN 'Недостаточно известных маршрутов'
             ELSE NULL END base_reason
        FROM features f CROSS JOIN settings s), evaluated AS (
        SELECT *,coalesce(base_reason,CASE WHEN value IS NULL OR used_observations<min_observations
            THEN 'Недостаточно пригодных наблюдений' END) availability_reason FROM facts)
        SELECT person_id,criterion_id,title_ru,value,threshold,weight,used_observations,availability_reason,
            CASE WHEN availability_reason IS NOT NULL THEN 'unavailable' WHEN value>=threshold THEN 'matched' ELSE 'not_matched' END status,
            CASE WHEN availability_reason IS NULL AND value>=threshold
                THEN weight*least(3.0,value/threshold) ELSE 0.0 END contribution
        FROM evaluated''', [days, min_days, known, known, known])
    db.execute('ALTER TABLE explanations ADD COLUMN reference_size BIGINT')
    db.execute('ALTER TABLE explanations ADD COLUMN reference_value DOUBLE')
    db.execute('ALTER TABLE explanations ADD COLUMN threshold_method VARCHAR')
    db.execute('UPDATE explanations SET threshold_method=?',[cfg['calibration']['mode']])
    if cfg['calibration']['mode']=='empirical_quantile':
        # Deterministic calibration on supplied profiles; no generated reference
        # population and no test labels. Reference values are persisted per rule.
        db.execute('''CREATE TABLE rule_reference AS SELECT criterion_id,count(*) reference_size,
            quantile_cont(value,?) reference_value FROM explanations
            WHERE availability_reason IS NULL
              AND cast('0x'||substr(sha256(person_id),1,8) AS UBIGINT)%10 IN (6,7)
            GROUP BY criterion_id''',[cfg['calibration']['quantile']])
        db.execute('''UPDATE explanations e SET reference_size=r.reference_size,reference_value=r.reference_value,
            threshold=greatest(e.threshold,r.reference_value),
            availability_reason=CASE WHEN r.reference_size<? THEN 'Недостаточно профилей в калибровочной выборке' ELSE e.availability_reason END
            FROM rule_reference r WHERE e.criterion_id=r.criterion_id''',[cfg['calibration']['min_reference_rows']])
        db.execute("UPDATE explanations SET availability_reason='Нет калибровочной выборки' WHERE reference_size IS NULL")
        db.execute('''UPDATE explanations SET status=CASE WHEN availability_reason IS NOT NULL THEN 'unavailable'
            WHEN value>=threshold THEN 'matched' ELSE 'not_matched' END,
            contribution=CASE WHEN availability_reason IS NULL AND value>=threshold THEN weight*least(3.0,value/threshold) ELSE 0.0 END''')
    db.execute('''CREATE TABLE rule_scores AS SELECT f.*,
        count(*) FILTER(WHERE e.status='matched')::INTEGER matched_criteria_count,
        count(*) FILTER(WHERE e.status<>'unavailable')::INTEGER available_criteria_count,
        sum(e.contribution) rule_score,
        CASE WHEN count(*) FILTER(WHERE e.status<>'unavailable')=0 THEN 'insufficient_data'
             WHEN count(*) FILTER(WHERE e.status='matched')=0 THEN 'no_matches' ELSE 'eligible' END eligibility_status
        FROM features f JOIN explanations e USING(person_id) GROUP BY ALL''')
    db.execute('''CREATE TABLE rankings AS SELECT *,CASE WHEN eligibility_status='eligible' THEN
        row_number() OVER(PARTITION BY eligibility_status ORDER BY matched_criteria_count DESC,rule_score DESC,person_id) END rank
        FROM rule_scores''')
    db.execute('CREATE TABLE top50 AS SELECT * FROM rankings WHERE rank<=50 ORDER BY rank')


def _graph(db):
    # Only explicitly observed UTC departures qualify. A date + flight number
    # alone may describe multiple rotations and does not establish a coflight.
    db.execute('''CREATE TABLE event_members AS SELECT segment_id,person_id,departure_date,
        sha256(to_json([departure_utc,flight_number,origin_airport_id,destination_airport_id])) event_id
        FROM segments WHERE graph_eligible IS TRUE AND departure_utc IS NOT NULL
        AND flight_number IS NOT NULL AND origin_airport_id IS NOT NULL AND destination_airport_id IS NOT NULL''')
    db.execute('CREATE TABLE events AS SELECT event_id,min(departure_date) departure_date,count(DISTINCT person_id) passengers FROM event_members GROUP BY event_id')
    # Avoid quadratic expansion of unvalidated oversized event groups. Record
    # these as an explicit limitation, never as absence of relationships.
    excluded = db.execute('SELECT count(*) FROM events WHERE passengers>1000').fetchone()[0]
    estimate = db.execute('SELECT coalesce(sum(passengers*(passengers-1)/2),0) FROM events WHERE passengers<=1000').fetchone()[0]
    if estimate > 20000000:
        return {'status': 'unavailable', 'reason': 'Pair expansion exceeds 20 million; no truncated graph published', 'pair_candidates': int(estimate)}
    db.execute('''CREATE TABLE graph_edges AS WITH members AS (
        SELECT DISTINCT m.event_id,m.person_id,e.departure_date,e.passengers FROM event_members m
        JOIN events e USING(event_id) WHERE e.passengers BETWEEN 2 AND 1000)
        SELECT a.person_id person_a,b.person_id person_b,count(*) shared_events,
            count(DISTINCT a.departure_date) shared_days,
            sum(1.0/(a.passengers-1)) inverse_occupancy_support,
            list(a.event_id ORDER BY a.event_id) event_ids
        FROM members a JOIN members b ON a.event_id=b.event_id AND a.person_id<b.person_id
        GROUP BY a.person_id,b.person_id HAVING count(DISTINCT a.departure_date)>=2''')
    return {'status': 'complete_with_limitations' if excluded else 'complete',
            'events': db.execute('SELECT count(*) FROM events').fetchone()[0],
            'repeated_pairs': db.execute('SELECT count(*) FROM graph_edges').fetchone()[0],
            'excluded_oversized_events': excluded, 'ranking_contribution': False,
            'limitation': 'Repeated observed co-occurrence; not calibrated significance. M07 does not add a ranking vote.'}


def _evaluation(db, cfg):
    base = {r['person_id'] for r in _rows(db, 'SELECT person_id FROM top50')}
    population_size = db.execute("SELECT count(*) FROM rankings WHERE eligibility_status IN ('eligible','no_matches')").fetchone()[0]
    experiments = []
    for scale in [.8,1.,1.2]:
        for omit in [None, *TITLES]:
            rows = _rows(db, '''WITH scores AS (SELECT e.person_id,
                count(*) FILTER(WHERE e.status<>'unavailable' AND value>=threshold*?) AS matched_count,
                sum(CASE WHEN e.status<>'unavailable' AND value>=threshold*? THEN weight*least(3.0,value/(threshold*?)) ELSE 0 END) score
                FROM explanations e JOIN rankings r USING(person_id)
                WHERE r.eligibility_status IN ('eligible','no_matches')
                  AND criterion_id IS DISTINCT FROM ? GROUP BY e.person_id)
                SELECT person_id FROM scores WHERE matched_count>0
                ORDER BY matched_count DESC,score DESC,person_id LIMIT 50''', [scale,scale,scale,omit])
            ids = {r['person_id'] for r in rows}
            experiments.append({'threshold_scale':scale,'omitted_criterion':omit, 'top_count':len(ids),
                                'jaccard_to_primary':len(ids&base)/len(ids|base) if ids|base else None})
    return {'status':'computed_without_labels', 'experiments':experiments,
            'population_fixed':True, 'population_size':population_size,
            'quality_metrics':None,'reason':'No supplied ground-truth labels; precision/recall and reviewer decisions are not fabricated.',
            'independent_review':'pending'}


def run(input_dir, output, config='configs/product.yaml', reference='data/reference/airports.csv', memory_limit='1GB', progress=None):
    started = time.monotonic()
    root, paths, hashes, origin, limitations = verify(input_dir)
    cfg = load(config)
    reference = Path(reference).resolve()
    ref_manifest = reference.parent / 'reference_manifest.json'
    AirportReference(reference, manifest=ref_manifest)
    hashes.update({'reference':fingerprint(reference), 'reference_manifest':fingerprint(ref_manifest), 'config':fingerprint(config)})
    code_hash = _digest({str(p.relative_to(Path(__file__).parents[1])):fingerprint(p)
                         for p in sorted(Path(__file__).parents[1].rglob('*.py'))})
    request = {'version':VERSION,'inputs':hashes,'origin':origin, 'code':code_hash,
               'runtime':{'python':platform.python_version(),'duckdb':duckdb.__version__, 'pyarrow':pa.__version__, 'scoring':'rules-only'}}
    run_id = 'product-' + _digest(request)[:20]
    target = Path(output).resolve()
    if target==root or root in target.parents or target in root.parents:
        raise ValueError('Product output must not overlap its input')
    target.parent.mkdir(parents=True, exist_ok=True)
    with OutputLock(target.parent / ('.'+target.name+'.lock'), 'Product'):
        if target.exists():
            from .store import ProductStore
            store = ProductStore(target)
            if store.manifest['request'] != request:
                changed=', '.join(key for key,value in request.items()
                                  if store.manifest['request'].get(key)!=value)
                raise ValueError('Output belongs to another input/config/code version; '
                    f'changed: {changed}. Existing snapshot was not overwritten. '
                    'To start the web UI, use: python -m yoyoflot.product.web --product "'
                    +str(target)+'". To rebuild, choose a NEW --output directory.')
            return store.manifest
        with tempfile.TemporaryDirectory(prefix='.'+target.name+'.', suffix='.partial', dir=target.parent) as temp:
            stage = Path(temp)
            def log(message):
                if progress: progress(message)
            log('Проверка исходных таблиц и расчёт признаков')
            with duckdb.connect(str(stage / 'work.duckdb')) as db:
                db.execute('SET memory_limit=?', [memory_limit])
                db.execute('SET threads=2')
                db.read_parquet(str(paths['persons.parquet'])).create_view('persons')
                db.read_parquet(str(paths['segments.parquet'])).create_view('segments')
                validate_tables(db)
                bounds = db.execute('SELECT min(try_cast(departure_date AS DATE)),max(try_cast(departure_date AS DATE)) FROM segments').fetchone()
                window = {'start':str(bounds[0]) if bounds[0] else None, 'end':str(bounds[1]) if bounds[1] else None,
                          'days':(bounds[1]-bounds[0]).days+1 if bounds[0] else 0,
                          'basis':'global observed dataset dates; source completeness is unknown'}
                _features(db,cfg,window)
                log('Граф наблюдаемых совместных событий')
                graph = _graph(db)
                evaluation = _evaluation(db,cfg)
                write_json(stage/'rules.json',_rows(db,'''SELECT criterion_id,min(threshold) threshold,min(weight) weight,
                    min(reference_size) reference_size,min(reference_value) reference_value,min(threshold_method) threshold_method
                    FROM explanations GROUP BY criterion_id ORDER BY criterion_id'''))
                counts = {table:db.execute(f'SELECT count(*) FROM {table}').fetchone()[0] for table in ['persons','segments','top50']}
                statuses = _rows(db,'SELECT eligibility_status,count(*) profiles FROM rankings GROUP BY eligibility_status ORDER BY eligibility_status')
                (stage/'tables').mkdir()
                exports = {'rankings':'person_id', 'top50':'rank','explanations':'person_id,criterion_id',
                           'features':'person_id', 'segments':'person_id,segment_id', 'persons':'person_id'}
                if graph['status'] in {'complete','complete_with_limitations'}:
                    exports.update({'graph_edges':'person_a,person_b','event_members':'person_id,event_id'})
                if 'person_links.parquet' in paths:
                    db.read_parquet(str(paths['person_links.parquet'])).create_view('person_links')
                    exports['person_links']='person_id,observation_id'
                if 'segment_links.parquet' in paths:
                    db.read_parquet(str(paths['segment_links.parquet'])).create_view('segment_links')
                    exports['segment_links']='person_id,segment_id,travel_id'
                log('Сохранение таблиц продукта')
                for table,order in exports.items():
                    db.execute(f'COPY (SELECT * FROM {table} ORDER BY {order}) TO ? (FORMAT PARQUET,COMPRESSION ZSTD,ROW_GROUP_SIZE 16384)', [str(stage/'tables'/f'{table}.parquet')])
                # Reviewers fill decisions themselves; original IDs make source tracing possible.
                audit = _rows(db, 'SELECT person_id,rank,matched_criteria_count,rule_score FROM top50 ORDER BY rank')
                import csv
                with (stage/'review.csv').open('w',newline='',encoding='utf-8') as stream:
                    writer=csv.DictWriter(stream,fieldnames=['person_id','rank','matched_criteria_count','rule_score','reviewer','decision','reason'])
                    writer.writeheader()
                    for row in audit:writer.writerow(row)
            for p in stage.glob('work.duckdb*'):p.unlink()
            shutil.copyfile(reference,stage/'airports.csv')
            shutil.copyfile(ref_manifest,stage/'reference_manifest.json')
            shutil.copyfile(config,stage/'product.yaml')
            provenance_files = {
                'resolution_manifest.json', 'resolution_report.json',
                't16/run_manifest.json', 't16/input_manifest.json', 't16/repair_manifest.json',
            }
            provenance_files.update(name for name in paths if name.startswith('t16/repair/'))
            for name in provenance_files & paths.keys():
                destination = stage / 'provenance' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(paths[name], destination)
            write_json(stage/'evaluation.json',evaluation)
            write_json(stage/'graph.json',graph)
            if any(fingerprint(paths[n])!=h for n,h in hashes.items() if n in paths):
                raise RuntimeError('T13 input changed while building product')
            for path,key in [(reference,'reference'),(ref_manifest,'reference_manifest'),(Path(config),'config')]:
                if fingerprint(path)!=hashes[key]:raise RuntimeError('Configuration/reference changed while building')
            limitations += ['The source observation window is inferred from dataset dates; continuous source coverage is not asserted.',
                            'Rule thresholds and weights require domain review; scores are not class probabilities.']
            files = {p.relative_to(stage).as_posix():fingerprint(p) for p in sorted(stage.rglob('*')) if p.is_file()}
            manifest={'schema_version':VERSION,'data_mode':'source_dataset','status':'complete','run_id':run_id,
                      'request':request,'files':files,'window':window,'counts':counts,'eligibility':statuses,
                      'source_provenance':_source_provenance(paths, origin),
                      'limitations':limitations,'sort_order':['matched_criteria_count DESC','rule_score DESC','person_id ASC'],
                      'contribution_formula':'weight * min(3, value / threshold) for each matched independent group',
                      'graph_status':graph['status'], 'independent_review':'pending',
                      'elapsed_seconds':round(time.monotonic()-started,3)}
            write_json(stage/'run_manifest.json',manifest)
            os.rename(stage,target)
    return manifest
