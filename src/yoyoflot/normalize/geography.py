"""T11 file pipeline: bounded batches, disk indices, isolated derived outputs."""
from __future__ import annotations

import importlib.metadata
import json
import os
import shutil
import sqlite3
import tempfile
import time
import uuid
from collections import Counter, defaultdict
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..contracts import SCHEMAS
from .locking import OutputLock
from .airports import (AirportReference, RULE_VERSION, SCHEDULE_PAYLOAD_FIELDS, ScheduleIndex,
                       candidate_ids_json, digest, enrich_travel, json_text)

META = {b'geography_contract_version': b'2.0', b'rule_version': RULE_VERSION.encode()}
PLACE_FIELDS = {
    'airport_id': pa.string(), 'iata': pa.string(), 'city': pa.string(), 'country': pa.string(),
    'latitude': pa.float64(), 'longitude': pa.float64(), 'timezone': pa.string(),
    'status': pa.string(), 'kind': pa.string(), 'reason': pa.string(),
    'candidates_json': pa.string(), 'override_json': pa.string(),
}
EXTRA_FIELDS = {f'{side}_{name}': typ for side in ('origin', 'destination') for name, typ in PLACE_FIELDS.items()}
EXTRA_FIELDS.update({f'{label}_{name}': pa.string() for label in ('departure', 'arrival') for name in
                     ('effective_local','effective_precision','time_origin','utc','utc_status','iana_timezone')})
EXTRA_FIELDS.update({name:pa.string() for name in ('schedule_match_status','schedule_id_used',
                    'geography_rule_version','geography_reference_version','inferred_fields_json')})
EXTRA_FIELDS.update({name:pa.bool_() for name in ('geography_complete','geography_inferred','time_complete','time_inferred')})
EXTRA_FIELDS.update(schedule_reference_group_id=pa.string(), schedule_reference_count=pa.int64())
LINK_SCHEMA = pa.schema([(name, pa.string()) for name in ('travel_id','source_id','source_record_id','locator',
                    'schedule_id','schedule_source_id','schedule_source_record_id','schedule_locator','status',
                    'temporal_coverage','rule_id','reason','filled_fields_json','evidence_json')], metadata=META)
INVENTORY_SCHEMA = pa.schema([(n, pa.string()) for n in ('source_id','entity_table','field','raw','kind','status',
                    'airport_id','iata','city','country','reason','candidates_json')] + [('count',pa.int64())], metadata=META)
REFERENCE_GROUP_SCHEMA = pa.schema([('group_id', pa.string()), ('member_count', pa.int64())] +
    [(name, pa.string()) for name in ('status', 'temporal_coverage', 'rule_id', 'reason', 'filled_fields_json')], metadata=META)
REFERENCE_MEMBER_SCHEMA = pa.schema([('group_id', pa.string()), ('schedule_id', pa.string())], metadata=META)


def iter_schedule_links(directory, batch_size=2000):
    """Expand the normalized relation to the exact legacy link dictionaries.

    Dense links are yielded first; relation order is not a part of the contract.
    Joins stream in bounded batches and never create an expanded artifact.
    """
    import duckdb
    directory = Path(directory)
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    version = (pq.read_schema(directory / 'travel_geography.parquet').metadata or {}).get(b'geography_contract_version')
    if version not in (b'1.0', b'2.0'):
        raise ValueError('Unsupported geography contract version')
    if version == b'2.0':
        for name in ('schedule_reference_groups.parquet', 'schedule_reference_members.parquet'):
            if not (directory / name).is_file():
                raise ValueError('Missing compact schedule reference artifact: ' + name)
        from ..resolve.geography_validation import T11_FILES, validate_reference_artifacts
        report = json.loads((directory / 'geography_report.json').read_text(encoding='utf-8'))
        validate_reference_artifacts({name: directory / name for name in T11_FILES}, report)
    for batch in rows(directory / 'schedule_links.parquet', batch_size):
        yield from batch
    group_path = directory / 'schedule_reference_groups.parquet'
    if version == b'1.0':
        return  # Original dense T11 artifact, before contract 2.0.
    with duckdb.connect() as db:
        db.execute("SET memory_limit='256MB'")
        db.execute('SET threads=2')
        for view, name in (('travels', 'travel_geography'), ('groups', 'schedule_reference_groups'),
                           ('members', 'schedule_reference_members'), ('schedules', 'schedule_geography')):
            db.read_parquet(str(directory / (name + '.parquet'))).create_view(view)
        schedule_columns = ','.join(f's."{name}" AS "__schedule_{name}"' for name in SCHEDULE_PAYLOAD_FIELDS)
        query = f'''SELECT t.travel_id,t.source_id,t.source_record_id,t.locator,
            g.status,g.temporal_coverage,g.rule_id,g.reason,g.filled_fields_json,
            {schedule_columns},s.origin_geo_airport_id,s.destination_geo_airport_id
            FROM travels t JOIN groups g ON t.schedule_reference_group_id=g.group_id
            JOIN members m USING(group_id) JOIN schedules s ON m.schedule_id=s.schedule_id'''
        reader = db.execute(query).to_arrow_reader(batch_size)
        for batch in reader:
            for row in batch.to_pylist():
                schedule = {name: row.pop('__schedule_' + name) for name in SCHEDULE_PAYLOAD_FIELDS}
                schedule['origin_airport_id'] = row.pop('origin_geo_airport_id')
                schedule['destination_airport_id'] = row.pop('destination_geo_airport_id')
                yield dict(row, schedule_id=schedule['schedule_id'], schedule_source_id=schedule['source_id'],
                           schedule_source_record_id=schedule['source_record_id'], schedule_locator=schedule['locator'],
                           evidence_json=json_text(schedule))


def rows(path, batch_size=2000):
    pf = pq.ParquetFile(path)
    for batch in pf.iter_batches(batch_size=batch_size):
        yield batch.to_pylist()


TRAVEL_WORK_FIELDS = ('travel_id','source_id','source_record_id','locator','flight_number','flight_number_normalized','normalization_rules_json','carrier',
    'codeshare','origin_raw','destination_raw','departure_local','departure_precision','departure_timezone',
    'arrival_local','arrival_precision','arrival_timezone')
SCHEDULE_WORK_FIELDS = ('schedule_id','source_id','source_record_id','locator','flight_number','origin','destination',
    'valid_from','valid_to','days_of_week','departure_local','arrival_local','arrival_day_offset','parse_status',
    'codeshare_mark','operated_by','origin_city','origin_country','dest_city','dest_country')


def work_batches(path, batch_size, fields):
    for batch in pq.ParquetFile(path).iter_batches(batch_size=batch_size):
        projected = batch.select([n for n in fields if n in batch.schema.names])
        yield batch, projected.to_pylist()


def write_derived(writer, original, enriched, schema):
    # Keep original Arrow columns as-is, including raw_json and future T12 fields.
    # Only newly computed fields need conversion back from Python objects.
    extras = pa.schema([f for f in schema if f.name not in original.schema.names])
    derived = pa.Table.from_pylist(enriched, schema=extras)
    columns = [original.column(f.name) if f.name in original.schema.names else derived.column(f.name) for f in schema]
    writer.write_table(pa.Table.from_arrays(columns, schema=schema))


def discover_inputs(roots):
    travels, schedules = [], []
    for root in map(Path, roots):
        if not root.is_dir():
            raise FileNotFoundError(f'Input directory not found: {root}')
        # A flat T12 output or immediate per-reader directories, never checkpoints.
        dirs = [root] if (root/'travel_observations.parquet').exists() else [root] + sorted(
            p for p in root.iterdir() if p.is_dir() and not p.name.endswith(('.partial','.checkpoint','.backup')))
        for directory in dirs:
            for name, result in [('travel_observations',travels),('schedules',schedules)]:
                p = directory/(name+'.parquet')
                if p.is_file():
                    result.append(p.resolve())
    if len(travels) != len(set(travels)) or len(schedules) != len(set(schedules)):
        raise ValueError('Overlapping input directories')
    if not travels:
        raise ValueError('No travel_observations.parquet found; run readers or T12 first')
    return travels, schedules


def extended_schema(inputs, base, extras):
    schemas = [pq.read_schema(p) for p in inputs] or [base]
    for s in schemas:
        for f in base:
            if f.name not in s.names or s.field(f.name).type != f.type:
                raise ValueError(f'Input must follow reader contract: {f.name} ({f.type})')
        if set(s.names) & set(extras):
            raise ValueError('Input already contains T11 fields; use upstream tables, not previous T11 output')
    combined = pa.unify_schemas(schemas)
    return pa.schema(list(combined) + [pa.field(k,v) for k,v in extras.items()], metadata=META)


def write(writer, records, schema):
    if records:
        writer.write_table(pa.Table.from_pylist(records, schema=schema))


def publish_files(staging, output, names):
    """Replace only T11-owned files; preserve T12 tables and roll back on exception."""
    backup = staging/'backup'
    backup.mkdir()
    changed = []
    try:
        for name in names:
            dest = output/name
            if dest.exists():
                os.replace(dest, backup/name)
            changed.append(name)
            os.replace(staging/name, dest)
    except BaseException:
        for name in reversed(changed):
            dest = output/name
            if dest.exists():
                dest.unlink()
            if (backup/name).exists():
                os.replace(backup/name, dest)
        raise


def run(inputs, output, reference, overrides=None, manifest=None, schedules=None, batch_size=2000, progress=None,
        compact_reference_links=True):
    started = time.perf_counter()
    last_progress = started
    def announce(message):
        nonlocal last_progress
        now = time.perf_counter()
        if progress is not None and now - last_progress >= 30:
            progress(message)
            last_progress = now
    if batch_size < 1:
        raise ValueError('batch_size must be positive')
    travels, schedule_paths = discover_inputs(inputs)
    if schedules is not None:
        schedule_paths = [Path(p).resolve() for p in schedules]
        if len(schedule_paths) != len(set(schedule_paths)):
            raise ValueError('Duplicate schedule inputs')
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if manifest is None and (Path(reference).parent/'reference_manifest.json').is_file():
        manifest = Path(reference).parent/'reference_manifest.json'
    ref = AirportReference(reference, overrides, manifest)
    ts = extended_schema(travels, SCHEMAS['travel_observations'], EXTRA_FIELDS)
    ss = extended_schema(schedule_paths, SCHEMAS['schedules'],
                         {f'{side}_geo_{name}': typ for side in ('origin','destination') for name,typ in PLACE_FIELDS.items()})
    fingerprints = {}
    for p in travels + schedule_paths:
        fingerprints[str(p)] = digest(p)
        announce('T11: проверка входных файлов')
    if manifest is not None:
        fingerprints[str(Path(manifest).resolve())] = digest(manifest)
    schedule_sources = []
    for path in schedule_paths:
        if pq.ParquetFile(path).metadata.num_rows == 0:
            continue
        report_path = path.parent/'source_report.json'
        info = json.loads(report_path.read_text(encoding='utf-8')) if report_path.exists() else {}
        if report_path.exists():
            fingerprints[str(report_path)] = digest(report_path)
        complete = (info.get('status') == 'complete' and
                    info.get('excluded_pages',0) == 0 and info.get('extraction_errors',0) == 0 and
                    info.get('pages') == info.get('total_pages') and
                    info.get('counts',{}).get('schedules') == pq.ParquetFile(path).metadata.num_rows)
        schedule_sources.append(dict(path=str(path),coverage='confirmed' if complete else 'unknown',
                                     source_status=info.get('status'),source_report=str(report_path) if info else None))
    total_travels = sum(pq.ParquetFile(p).metadata.num_rows for p in travels)
    total_schedules = sum(pq.ParquetFile(p).metadata.num_rows for p in schedule_paths)
    counts, quality = Counter(), defaultdict(Counter)
    schedule_quality = defaultdict(Counter)
    link_counts, match_counts = defaultdict(Counter), defaultdict(Counter)
    with OutputLock(output/'.t11.lock'):
        with tempfile.TemporaryDirectory(prefix='.t11-', dir=output) as temp:
            staging = Path(temp)
            index = ScheduleIndex(staging/'index.sqlite', ref)
            try:
                if any(s['coverage'] != 'confirmed' for s in schedule_sources):
                    index.corpus_coverage = 'unknown'
                db = index.db
                db.execute('CREATE TABLE seen_travels (id TEXT NOT NULL)')
                db.execute('CREATE TABLE places (source TEXT, entity TEXT, field TEXT, raw_json TEXT, n INTEGER, '
                           'PRIMARY KEY(source,entity,field,raw_json))')
                def inventory(batch, entity, fields):
                    values = Counter((r['source_id'], entity, f, r.get(f)) for r in batch for f in fields)
                    db.executemany('INSERT INTO places VALUES (?,?,?,?,?) ON CONFLICT(source,entity,field,raw_json) '
                                   'DO UPDATE SET n=n+excluded.n', [(source, table, field, json_text(raw), n)
                                       for (source, table, field, raw), n in values.items()])
                with pq.ParquetWriter(staging/'schedule_geography.parquet', ss, compression='zstd') as writer:
                    for path in schedule_paths:
                        for original, batch in work_batches(path, batch_size, SCHEDULE_WORK_FIELDS):
                            inventory(batch,'schedules',('origin','destination','origin_city','origin_country','dest_city','dest_country'))
                            enriched = []
                            for row in batch:
                                index.add(row)
                                item = dict(row)
                                for side in ('origin','destination'):
                                    p = ref.resolve(row.get(side),row['source_id'])
                                    status = p['status']
                                    group = status if status in ('exact','corrected') else 'unresolved'
                                    schedule_quality[row['source_id']]['places_'+group] += 1
                                    schedule_quality[row['source_id']]['places_total'] += 1
                                    for k in PLACE_FIELDS:
                                        value = candidate_ids_json(p['candidates']) if k=='candidates_json' else p['rule_id'] if k=='override_json' else p[k]
                                        item[side+'_geo_'+k] = value
                                enriched.append(item)
                            write_derived(writer,original,enriched,ss)
                            counts['schedules'] += len(batch)
                            announce(f"T11: обработано строк расписания — {counts['schedules']} / {total_schedules}")
                    announce('T11: сборка индекса расписания')
                    index.commit()
                with (pq.ParquetWriter(staging/'travel_geography.parquet', ts, compression='zstd') as tw,
                      pq.ParquetWriter(staging/'schedule_links.parquet', LINK_SCHEMA, compression='zstd') as lw):
                    for path in travels:
                        for original, batch in work_batches(path, batch_size, TRAVEL_WORK_FIELDS):
                            inventory(batch,'travel_observations',('origin_raw','destination_raw'))
                            for row in batch:
                                if any(not isinstance(row.get(k),str) or not row[k] for k in ('travel_id','source_id','source_record_id','locator')):
                                    raise ValueError('Missing travel primary key or provenance')
                            db.executemany('INSERT INTO seen_travels VALUES (?)', ((row['travel_id'],) for row in batch))
                            enriched, links_buffer = [], []
                            for row in batch:
                                item, links = enrich_travel(row,ref,index,compact_reference_links)
                                item.setdefault('schedule_reference_group_id', None)
                                item.setdefault('schedule_reference_count', 0)
                                item['geography_complete'] = bool(item['origin_airport_id'] and item['destination_airport_id'])
                                item['geography_inferred'] = any(item[s+'_status']=='inferred_from_schedule' for s in ('origin','destination'))
                                item['time_complete'] = bool(item['departure_utc'] and item['arrival_utc'])
                                item['time_inferred'] = any(item[s+'_time_origin']=='inferred_from_schedule' for s in ('departure','arrival'))
                                enriched.append(item)
                                source = row['source_id']
                                for s in ('origin','destination'):
                                    status = item[s+'_status']
                                    group = 'inferred' if status=='inferred_from_schedule' else status if status in ('exact','corrected') else 'unresolved'
                                    quality[source]['places_'+group] += 1
                                    quality[source]['places_total'] += 1
                                    quality[source][s+'_'+status] += 1
                                for s in ('departure','arrival'):
                                    quality[source][s+'_utc_'+item[s+'_utc_status']] += 1
                                quality[source]['travels'] += 1
                                match_counts[source][item['schedule_match_status']] += 1
                                if item['schedule_reference_count']:
                                    link_counts[source]['reference_only'] += item['schedule_reference_count']
                                counts['logical_schedule_links'] += len(links) + item['schedule_reference_count']
                                counts['schedule_reference_associations'] += item['schedule_reference_count']
                                for link in links:
                                    links_buffer.append(link)
                                    link_counts[source][link['status']] += 1
                                    # Bound writer buffers even for flights with many matching legs.
                                    if len(links_buffer) >= batch_size:
                                        write(lw,links_buffer,LINK_SCHEMA)
                                        counts['schedule_links'] += len(links_buffer)
                                        links_buffer.clear()
                            write_derived(tw,original,enriched,ts)
                            write(lw,links_buffer,LINK_SCHEMA)
                            counts['schedule_links'] += len(links_buffer)
                            counts['travels'] += len(batch)
                            announce(f"T11: обработано поездок — {counts['travels']} / {total_travels} ({path.parent.name})")
                            db.commit()
                with (pq.ParquetWriter(staging/'schedule_reference_groups.parquet', REFERENCE_GROUP_SCHEMA, compression='zstd') as gw,
                      pq.ParquetWriter(staging/'schedule_reference_members.parquet', REFERENCE_MEMBER_SCHEMA, compression='zstd') as mw):
                    cursor = db.execute('SELECT group_id,member_count FROM reference_groups ORDER BY group_id')
                    while batch := cursor.fetchmany(batch_size):
                        write(gw, [dict(group_id=gid,member_count=count,status='reference_only',temporal_coverage='outside_period',
                            rule_id=RULE_VERSION + ':unique_flight_route_date_days',reason='outside_schedule_period',filled_fields_json='{}')
                            for gid,count in batch], REFERENCE_GROUP_SCHEMA)
                        counts['schedule_reference_groups'] += len(batch)
                    cursor = db.execute('SELECT group_id,schedule_id FROM reference_members ORDER BY group_id,schedule_id')
                    while batch := cursor.fetchmany(batch_size):
                        write(mw, [dict(group_id=gid,schedule_id=sid) for gid,sid in batch], REFERENCE_MEMBER_SCHEMA)
                        counts['schedule_reference_members'] += len(batch)
                for count_name in ('travels', 'schedules', 'schedule_links', 'logical_schedule_links', 'schedule_reference_associations',
                                   'schedule_reference_groups', 'schedule_reference_members'):
                    counts.setdefault(count_name, 0)
                compatibility_cache = dict(hits=index.compatibility_cache_hits, misses=index.compatibility_cache_misses,
                    bytes=index._compatibility_bytes, limit_bytes=index.COMPATIBILITY_CACHE_BYTES,
                    known_schedule_periods=index.periods_known)
                reference_cache = dict(hits=index.reference_cache_hits, misses=index.reference_cache_misses,
                    bytes=index._reference_bytes, limit_bytes=index.REFERENCE_CACHE_BYTES,
                    entries=len(index._references), known_schedule_periods=index.periods_known)
                announce('T11: проверка уникальности поездок')
                db.execute('CREATE UNIQUE INDEX seen_travel_id ON seen_travels(id)')
                db.commit()
                with pq.ParquetWriter(staging/'place_inventory.parquet', INVENTORY_SCHEMA, compression='zstd') as writer:
                    cursor = db.execute('SELECT source,entity,field,raw_json,n FROM places ORDER BY source,entity,field,raw_json')
                    while True:
                        batch = cursor.fetchmany(batch_size)
                        if not batch:
                            break
                        inventory_rows = []
                        for source,entity,field,raw,n in batch:
                            p = ref.resolve(json.loads(raw),source)
                            inventory_rows.append(dict(source_id=source,entity_table=entity,field=field,raw=p['raw'],count=n,
                                **{k:p[k] for k in ('kind','status','airport_id','iata','city','country','reason')},
                                candidates_json=json_text(p['candidates'])))
                        write(writer,inventory_rows,INVENTORY_SCHEMA)
                        counts['unique_places_by_source_field'] += len(inventory_rows)
                        announce('T11: сборка инвентаря мест')
            finally:
                index.close()
            for path, before in fingerprints.items():
                announce('T11: заключительная проверка входных файлов')
                if digest(path) != before:
                    raise RuntimeError('Input changed during T11: '+path)
            for path, before in ((reference,ref.sha256),(overrides,ref.overrides_sha256)):
                if path and digest(path) != before:
                    raise RuntimeError('Reference or overrides changed during T11')
            by_source = {}
            for source,c in sorted(quality.items()):
                by_source[source] = dict(counts=dict(c), fractions={g:c['places_'+g]/c['places_total']
                    for g in ('exact','corrected','inferred','unresolved')},
                    schedule_links=dict(link_counts[source]),schedule_matches=dict(match_counts[source]))
            try:
                tz_version = importlib.metadata.version('tzdata')
            except importlib.metadata.PackageNotFoundError:
                tz_version = None
            schedule_by_source = {source:dict(counts=dict(c),fractions={
                g:c['places_'+g]/c['places_total'] for g in ('exact','corrected','inferred','unresolved')})
                for source,c in sorted(schedule_quality.items())}
            report = dict(status='complete' , acceptance='pending_real_data_and_T12_review', rule_version=RULE_VERSION,
                          contract_version='2.0',
                          geography_contract_version='2.0',
                          schedule_link_representation='outside_period_groups' if compact_reference_links else 'dense',
                          compatibility_cache=compatibility_cache,
                          reference_cache=reference_cache,
                          schedule_sources=schedule_sources, schedule_by_source=schedule_by_source,
                          reference_version=ref.version, reference_sha256=ref.sha256, overrides_sha256=ref.overrides_sha256,
                          tzdata_package_version=tz_version, timezone_rules_source='tzdata' if tz_version else 'system', counts=dict(counts),by_source=by_source,
                          inputs=fingerprints,seconds=round(time.perf_counter()-started,3),
                          limitations=['Reference is not a certified historical airport-code register.',
                                       'Unresolved geography/time and out-of-period links are not parser errors.'])
            summary = ('Итог T11\nНаблюдения поездок: '+str(counts['travels'])+'\nСтроки расписания: '+str(counts['schedules'])+
                       '\nЛогические связи с расписанием: '+str(counts['logical_schedule_links'])+
                       '\nРазвёрнутые связи: '+str(counts['schedule_links'])+
                       '\nОбщие группы ссылок вне периода: '+str(counts['schedule_reference_groups'])+'\n')
            for source, stats in by_source.items():
                c=stats['counts']
                summary += (f"{source}: места — точные {c.get('places_exact',0)}, исправленные {c.get('places_corrected',0)}, "
                            f"выведенные {c.get('places_inferred',0)}, нерешённые {c.get('places_unresolved',0)}\n")
            summary += f"Время: {report['seconds']} с\nРезультаты: {output}\nПриёмка на полных реальных данных и T12: ожидается\n"
            (staging/'geography_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
            (staging/'geography_summary.txt').write_text(summary,encoding='utf-8')
            files = ['travel_geography.parquet','schedule_geography.parquet','schedule_links.parquet',
                     'schedule_reference_groups.parquet','schedule_reference_members.parquet',
                     'place_inventory.parquet','geography_report.json','geography_summary.txt']
            from ..resolve.geography_validation import validate_reference_artifacts
            validate_reference_artifacts({name: staging / name for name in files}, report)
            manifest_payload = dict(contract_version='2.0',rule_version=RULE_VERSION,files={n:digest(staging/n) for n in files})
            (staging/'t11_manifest.json').write_text(json.dumps(manifest_payload,indent=2)+'\n',encoding='utf-8')
            publish_files(staging,output,files+['t11_manifest.json'])
            return report, summary
