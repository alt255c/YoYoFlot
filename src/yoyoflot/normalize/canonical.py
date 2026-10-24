"""T12 canonical tables: stream value rules; native batch dedup and FK checks."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from ..contracts import ID_COLUMNS, SCHEMAS as READER_SCHEMAS
from .locking import OutputLock
from .values import Rules, VERSION, dumps, file_hash

TABLES={'person_observations':'observations','identifiers':'identifiers',
        'travel_observations':'travel_observations','schedules':'schedules',
        'provenance':'provenance','errors':'errors'}
PERSONAL_SOURCES={'boarding_csv','forum_json','pointz_xml','sirena_fixed','skyteam_yaml','boardingpass_xlsx'}
META={b'canonical_contract_version':b'1.0',b'normalization_version':VERSION.encode(),b'reader_contract_version':b'1.0'}
COMMON={'input_schema_sha256':pa.string(),'normalization_version':pa.string(),'source_values_json':pa.string(),
        'normalization_rules_json':pa.string(),'normalization_status':pa.string()}
EXTRAS={
    'observations':{'name_comparison':pa.string(),'name_latin_comparison':pa.string()},
    'identifiers':{k:pa.string() for k in ('value_normalized','programme_normalized','identifier_namespace','identifier_match_key','namespace_status')},
    'travel_observations':{k:pa.string() for k in ('flight_number_normalized','ticket_number_normalized','booking_code_normalized')},
    'schedules':{},'provenance':{},'errors':{},
}
KEYS={TABLES[t]:[v] for t,v in ID_COLUMNS.items()}
KEYS['provenance']=['entity_table','entity_id','source_record_id']
SCHEMAS={}
for source,name in TABLES.items():
    required={'source_id','source_record_id','locator',*KEYS[name]}
    if name in ('identifiers','travel_observations'):required.add('observation_id')
    SCHEMAS[name]=pa.schema([pa.field(f.name,f.type,nullable=f.name not in required) for f in READER_SCHEMAS[source]]+
        [pa.field(n,t) for n,t in {**COMMON,**EXTRAS[name]}.items()],metadata=META)
ERROR_SCHEMA=pa.schema([(n,pa.string()) for n in ('normalization_error_id','source_id','entity_table','entity_id',
    'source_record_id','locator','field','reason','value_json','rule_id','severity')],metadata=META)
LOG_SCHEMA=pa.schema([(n,pa.string()) for n in ('source_id','entity_table','field','rule_id','version','examples_json')]+
    [('affected_records',pa.int64()),('exceptions',pa.int64())],metadata=META)
DUP_SCHEMA=pa.schema([(n,pa.string()) for n in ('source_id','entity_table','entity_id','source_record_id','locator')]+
    [('input_copies',pa.int64()),('duplicates_removed',pa.int64())],metadata=META)


def _ident(row,table):
    return row.get(KEYS[table][0]) if table!='provenance' else dumps([row.get(k) for k in KEYS[table]])


class Normalizer:
    def __init__(self,rules):
        self.rules=rules

    def row(self,source_table,row,unknown=()):
        table=TABLES[source_table];out=dict(row);backups={};applied={};errors=[];events=[]
        source=row.get('source_id');eid=_ident(row,table)
        def record(field,rule,before,after,exception=None):
            if before==after and exception is None:return
            applied[field]=rule
            if field in READER_SCHEMAS[source_table].names:
                backups.setdefault(field,before)
            events.append((field,rule,before,after,exception))
        def error(field,reason,value,rule='type.incompatible'):
            errors.append(dict(normalization_error_id=hashlib.sha256(dumps([source,table,eid,field,reason]).encode()).hexdigest(),
                source_id=source,entity_table=table,entity_id=eid,source_record_id=row.get('source_record_id'),locator=row.get('locator'),
                field=field,reason=reason,value_json=dumps(value),rule_id=rule,severity='error'))
        required={f.name for f in SCHEMAS[table] if not f.nullable}
        for field in READER_SCHEMAS[source_table]:
            value=out.get(field.name)
            if field.name in required:
                if not isinstance(value,str) or not value:raise ValueError(f'{table}: missing/invalid identity {field.name}')
            elif value is not None:
                valid=isinstance(value,str) if pa.types.is_string(field.type) else isinstance(value,bool) if pa.types.is_boolean(field.type) else isinstance(value,int) and not isinstance(value,bool)
                if not valid:
                    record(field.name,'type.incompatible',value,None,'incompatible_type')
                    out[field.name]=None;error(field.name,'incompatible_type',value)
        for name in unknown:
            backups['extra:'+name]=row.get(name)
        if table=='observations':
            name=self.rules.name(out.get('name_raw'))
            out['name_comparison']=name
            out['name_latin_comparison']=self.rules.transliterate(name)
            record('name_comparison','name.nfkc_casefold_space',None,name)
            record('name_latin_comparison','name.ru_ascii_v1',None,out['name_latin_comparison'])
            before=out.get('birth_date')
            value,_,reason=self.rules.date_value(source,'birth_date',out.get('birth_date_raw'),before)
            out['birth_date']=value
            record('birth_date','date.source_format',before,value,reason)
            if reason:error('birth_date',reason,out.get('birth_date_raw'),'date.source_format')
            manual_fields=self.rules.manual(table,eid,source,out,{'name_comparison','name_latin_comparison','birth_date'},record)
            if 'name_comparison' in manual_fields and 'name_latin_comparison' not in manual_fields:
                before=out['name_latin_comparison'];out['name_latin_comparison']=self.rules.transliterate(out['name_comparison'])
                record('name_latin_comparison','name.ru_ascii_v1',before,out['name_latin_comparison'])
        elif table=='identifiers':
            value,programme,namespace,match,status=self.rules.identifier(source,out.get('kind'),out.get('value'),out.get('programme'))
            out.update(value_normalized=value,programme_normalized=programme,identifier_namespace=namespace,identifier_match_key=match,namespace_status=status)
            record('value_normalized','identifier.type_preserving',None,value)
            record('programme_normalized','identifier.programme_context',None,programme)
            record('identifier_namespace','identifier.namespace',None,namespace)
            record('identifier_match_key','identifier.namespace_value_key',None,match)
            record('namespace_status','identifier.namespace',None,status)
            self.rules.manual(table,eid,source,out,{'value_normalized','programme_normalized'},record)
            if out['value_normalized']!=value or out['programme_normalized']!=programme:
                value,programme,namespace,match,status=self.rules.identifier(source,out.get('kind'),out['value_normalized'],out['programme_normalized'])
                for field,new_value in [('value_normalized',value),('programme_normalized',programme),('identifier_namespace',namespace),('identifier_match_key',match),('namespace_status',status)]:
                    record(field,'identifier.namespace_value_key' if field=='identifier_match_key' else 'identifier.namespace' if field in ('identifier_namespace','namespace_status') else 'identifier.type_preserving',out[field],new_value)
                    out[field]=new_value
            if status in ('unsupported_identifier_kind','missing_identifier_value'):
                record('value_normalized','identifier.type_preserving',None,value,status)
                error('value_normalized',status,out.get('value'),'identifier.type_preserving')
        elif table=='travel_observations':
            for label in ('departure','arrival'):
                before=out.get(label+'_local');precision=out.get(label+'_precision')
                value,p,reason=self.rules.date_value(source,label,out.get(label+'_raw'),before,precision)
                out[label+'_local']=value;out[label+'_precision']=p
                record(label+'_local','date.source_format',before,value,reason)
                record(label+'_precision','date.precision',precision,p)
                if reason:error(label+'_local',reason,out.get(label+'_raw'),'date.source_format')
            for target,origin in [('flight_number_normalized','flight_number'),('ticket_number_normalized','ticket_number'),('booking_code_normalized','booking_code')]:
                out[target]=self.rules.clean(out.get(origin))
                record(target,'string.nfkc_outer_space',None,out[target])
            manual_fields=self.rules.manual(table,eid,source,out,{'departure_local','arrival_local','flight_number_normalized','ticket_number_normalized','booking_code_normalized'},record)
            for label in ('departure','arrival'):
                if label+'_local' in manual_fields:
                    _,p,_=self.rules.parse(out[label+'_local'],())
                    record(label+'_precision','date.manual_precision',out[label+'_precision'],p)
                    out[label+'_precision']=p
        elif table=='schedules':
            for name in ('valid_from','valid_to'):
                before=out.get(name);value,p,reason=self.rules.parse(before,())
                if p not in (None,'date'):value=None;reason='schedule_period_not_date'
                out[name]=value;record(name,'schedule.validate_period',before,value,reason)
                if reason:error(name,reason,before,'schedule.validate_period')
            if out.get('valid_from') and out.get('valid_to') and out['valid_from']>out['valid_to']:
                for name in ('valid_from','valid_to'):
                    before=out[name];out[name]=None;record(name,'schedule.validate_period',before,None,'reversed_period');error(name,'reversed_period',before,'schedule.validate_period')
        elif table=='provenance':
            before=out.get('entity_table')
            out['entity_table']=TABLES.get(before,before)
            record('entity_table','provenance.canonical_alias',before,out['entity_table'])
        out.update(normalization_version=VERSION,source_values_json=dumps(backups),normalization_rules_json=dumps(applied),
                   normalization_status='invalid_fields' if errors else 'ok')
        return out,errors,events


def discover(inputs):
    files={name:[] for name in TABLES};reports={}
    for root in map(Path,inputs):
        if not root.is_dir():raise FileNotFoundError(root)
        dirs=[root] if (root/'person_observations.parquet').is_file() else sorted(
            p for p in root.iterdir() if p.is_dir() and not p.name.startswith('.') and not p.name.endswith(('.checkpoint','.partial','.backup')))
        for directory in dirs:
            if not any((directory/(t+'.parquet')).is_file() for t in TABLES):continue
            for table in TABLES:
                path=directory/(table+'.parquet')
                if path.is_file():files[table].append(path.resolve())
            report=directory/'source_report.json'
            if report.is_file():reports[str(report.resolve())]=json.loads(report.read_text(encoding='utf-8'))
    if not files['person_observations']:raise ValueError('No reader person_observations.parquet found; use interim reader outputs')
    # Repeated paths are intentional input copies; native dedup below accounts for them.
    return files,reports


def publish(staging,output,names):
    backup=staging/'backup';backup.mkdir();changed=[]
    try:
        for name in names:
            dest=output/name
            if dest.exists():os.replace(dest,backup/name)
            changed.append(name);os.replace(staging/name,dest)
    except BaseException:
        for name in reversed(changed):
            dest=output/name
            if dest.exists():dest.unlink()
            if (backup/name).exists():os.replace(backup/name,dest)
        raise


def _sql_identifier(value):return '"'+value.replace('"','""')+'"'


def _write_query(db,query,params,path,schema,batch_size):
    with pq.ParquetWriter(path,schema,compression='zstd') as writer:
        cursor=db.execute(query,params)
        reader=getattr(cursor,'to_arrow_reader',cursor.fetch_record_batch)(batch_size)
        for batch in reader:
            writer.write_table(pa.Table.from_batches([batch]).cast(schema))


def build(inputs,output,config,batch_size=4000,memory_limit='512MB',require_all=False,progress=None):
    start=time.perf_counter();last=start
    def announce(message):
        nonlocal last
        now=time.perf_counter()
        if progress is not None and now-last>=30:progress(message);last=now
    if batch_size<1:raise ValueError('batch_size must be positive')
    files,reports=discover(inputs);rules=Rules(config);normalizer=Normalizer(rules)
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    fingerprints={str(p):file_hash(p) for paths in files.values() for p in paths}
    fingerprints.update({p:file_hash(p) for p in reports});fingerprints[str(Path(config).resolve())]=rules.sha256
    input_counts={TABLES[t]:sum(pq.ParquetFile(p).metadata.num_rows for p in paths) for t,paths in files.items()}
    balances=[];observed_sources=set();input_schemas={}
    for info in reports.values():
        if info.get('source_id') in PERSONAL_SOURCES:observed_sources.add(info['source_id'])
    with OutputLock(output/'.canonical.lock',label='T12'):
        with tempfile.TemporaryDirectory(prefix='.canonical-',dir=output) as temp:
            staging=Path(temp);db=duckdb.connect(str(staging/'work.duckdb'))
            db.execute('SET memory_limit=?',[memory_limit]);db.execute('SET threads=2')
            db.execute('SET preserve_insertion_order=false')
            db.execute('SET temp_directory=?',[str(staging/'spill')])
            errors_count=0;rule_stats={};publishing=False
            def collect(stats,source,table,eid,locator,events):
                seen=set()
                for field,rule,before,after,exception in reversed(events):
                    slot=(source,table,field,rule)
                    if slot in seen:continue
                    seen.add(slot)
                    item=stats.setdefault(slot,dict(n=0,samples=[]));item['n']+=1
                    if len(item['samples'])<3:
                        item['samples'].append(dict(entity_id=eid,locator=locator,before=before,after=after))
            try:
                with (pq.ParquetWriter(staging/'normalization_errors.input.parquet',ERROR_SCHEMA,compression='zstd') as ew,
                      pq.ParquetWriter(staging/'duplicate_reads.parquet',DUP_SCHEMA,compression='zstd') as dw):
                    for source_table,name in TABLES.items():
                        stage=staging/(name+'.input.parquet');schema=SCHEMAS[name];processed=0;table_stats={}
                        with pq.ParquetWriter(stage,schema,compression='zstd') as writer:
                            for path in files[source_table]:
                                pf=pq.ParquetFile(path);known=set(READER_SCHEMAS[source_table].names)
                                schema_hash=hashlib.sha256(pf.schema_arrow.serialize().to_pybytes()).hexdigest()
                                input_schemas[schema_hash]=str(pf.schema_arrow)
                                unknown=[n for n in pf.schema_arrow.names if n not in known]
                                needed=[n for n in pf.schema_arrow.names if n not in ('raw_json','raw_line') or pf.schema_arrow.field(n).type!=pa.string()]
                                # Source PK/provenance fields must remain textual identities.
                                for k in {f.name for f in schema if not f.nullable}:
                                    if k not in pf.schema_arrow.names or pf.schema_arrow.field(k).type!=pa.string():
                                        raise ValueError(f'{path}: identity column {k} must be string')
                                for batch in pf.iter_batches(batch_size=batch_size):
                                    work=batch.select(needed).to_pylist();result=[];exceptions=[]
                                    for row in work:
                                        out,errs,events=normalizer.row(source_table,row,unknown)
                                        out['input_schema_sha256']=schema_hash
                                        result.append(out);exceptions.extend(errs)
                                        collect(table_stats,row.get('source_id'),name,_ident(out,name),row.get('locator'),events)
                                    # Keep large untouched source columns directly in Arrow.
                                    columns=[]
                                    for field in schema:
                                        if field.name in ('raw_json','raw_line') and field.name in batch.schema.names and batch.schema.field(field.name).type==field.type:
                                            columns.append(batch.column(field.name))
                                        else:columns.append(pa.array([r.get(field.name) for r in result],type=field.type))
                                    writer.write_table(pa.Table.from_arrays(columns,schema=schema))
                                    if exceptions:ew.write_table(pa.Table.from_pylist(exceptions,schema=ERROR_SCHEMA))
                                    processed+=len(batch);announce(f'T12: {name} — обработано {processed}')
                        # Fast path: check only keys; do not hash/group all raw_json rows unless duplicates exist.
                        keys=KEYS[name];group=','.join(_sql_identifier(k) for k in keys)
                        duplicate_groups=db.execute(f'SELECT COUNT(*) FROM (SELECT {group} FROM read_parquet(?) GROUP BY {group} HAVING COUNT(*)>1)',[str(stage)]).fetchone()[0]
                        rows_in=pq.ParquetFile(stage).metadata.num_rows
                        if duplicate_groups:
                            db.read_parquet(str(stage)).distinct().create_view('canonical_unique',replace=True)
                            conflict=db.execute(f'SELECT {group} FROM canonical_unique GROUP BY {group} HAVING COUNT(*)>1 LIMIT 1').fetchone()
                            if conflict:raise ValueError(f'{name}: conflicting copies of the same source key: {conflict}')
                            _write_query(db,'SELECT * FROM canonical_unique ORDER BY '+group,[],staging/(name+'.parquet'),schema,batch_size)
                            db.execute('DROP VIEW canonical_unique')
                            # Reconstruct original small fields for accepted records only in
                            # this rare path. source_values_json restores all changed values.
                            table_stats={}
                            selected=[n for n in schema.names if n in known and n not in ('raw_json','raw_line')]+['source_values_json']
                            for part in pq.ParquetFile(staging/(name+'.parquet')).iter_batches(batch_size=batch_size,columns=selected):
                                for accepted in part.to_pylist():
                                    original={k:accepted.get(k) for k in known if k not in ('raw_json','raw_line')}
                                    backups=json.loads(accepted['source_values_json']);extra=[]
                                    for k,v in backups.items():
                                        if k.startswith('extra:'):
                                            original[k[6:]]=v;extra.append(k[6:])
                                        else:original[k]=v
                                    restored,_,events=normalizer.row(source_table,original,extra)
                                    collect(table_stats,original.get('source_id'),name,_ident(restored,name),original.get('locator'),events)
                        else:os.replace(stage,staging/(name+'.parquet'))
                        final=staging/(name+'.parquet')
                        # Per-source/entity balance, using the same projection for physical copies and accepted rows.
                        for source,n in db.execute('SELECT source_id,COUNT(*) FROM read_parquet(?) GROUP BY source_id',[str(final)]).fetchall():
                            if source in PERSONAL_SOURCES:observed_sources.add(source)
                            if rows_in==0:before=0
                            elif stage.exists():before=db.execute('SELECT COUNT(*) FROM read_parquet(?) WHERE source_id=?',[str(stage),source]).fetchone()[0]
                            else:before=n
                            balances.append(dict(source_id=source,entity_table=name,input_rows=before,output_rows=n,duplicates_removed=before-n))
                        if duplicate_groups:
                            select_keys='entity_table,entity_id,source_record_id' if name=='provenance' else _sql_identifier(KEYS[name][0])+',source_record_id'
                            identity='to_json(list_value(entity_table,entity_id,source_record_id))' if name=='provenance' else _sql_identifier(KEYS[name][0])
                            q=f'SELECT source_id,{identity} AS entity_id,source_record_id,locator,COUNT(*) n FROM read_parquet(?) GROUP BY source_id,{select_keys},locator HAVING COUNT(*)>1'
                            cursor=db.execute(q,[str(stage)])
                            while True:
                                part=cursor.fetchmany(batch_size)
                                if not part:break
                                records=[dict(source_id=source,entity_table=name,entity_id=eid,source_record_id=srid,locator=loc,input_copies=n,duplicates_removed=n-1)
                                         for source,eid,srid,loc,n in part]
                                dw.write_table(pa.Table.from_pylist(records,schema=DUP_SCHEMA))
                        rule_stats.update(table_stats)
                        announce(f'T12: {name} — проверка ключей завершена')
                missing=sorted(PERSONAL_SOURCES-observed_sources)
                if require_all and missing:raise ValueError('Missing personal sources: '+', '.join(missing))
                # Native relation checks; invalid child records remain visible with explicit errors.
                relation_path=staging/'relation_errors.parquet'
                with pq.ParquetWriter(relation_path,ERROR_SCHEMA,compression='zstd') as rw:
                    def relation_query(query,params,child,field,reason):
                        cursor=db.execute(query,params)
                        while True:
                            part=cursor.fetchmany(batch_size)
                            if not part:break
                            records=[dict(normalization_error_id=hashlib.sha256(dumps([source,child,eid,field,reason]).encode()).hexdigest(),source_id=source,
                                entity_table=child,entity_id=eid,source_record_id=srid,locator=loc,field=field,reason=reason,value_json=dumps(value),rule_id='relation.foreign_key',severity='error')
                                for source,eid,srid,loc,value in part]
                            rw.write_table(pa.Table.from_pylist(records,schema=ERROR_SCHEMA))
                    relations=[('identifiers','observation_id','observations','observation_id'),
                               ('travel_observations','observation_id','observations','observation_id'),
                               ('travel_observations','loyalty_identifier_id','identifiers','identifier_id')]
                    for child,fk,parent,pk in relations:
                        q=f'SELECT c.source_id,c.{_sql_identifier(KEYS[child][0])},c.source_record_id,c.locator,c.{_sql_identifier(fk)} FROM read_parquet(?) c ANTI JOIN read_parquet(?) p ON c.{_sql_identifier(fk)}=p.{_sql_identifier(pk)} WHERE c.{_sql_identifier(fk)} IS NOT NULL'
                        relation_query(q,[str(staging/(child+'.parquet')),str(staging/(parent+'.parquet'))],child,fk,'orphan_reference')
                    relation_query("SELECT t.source_id,t.travel_id,t.source_record_id,t.locator,t.loyalty_identifier_id FROM read_parquet(?) t JOIN read_parquet(?) i ON t.loyalty_identifier_id=i.identifier_id WHERE i.kind IS DISTINCT FROM 'loyalty_card' OR t.observation_id<>i.observation_id",
                        [str(staging/'travel_observations.parquet'),str(staging/'identifiers.parquet')],
                        'travel_observations','loyalty_identifier_id','loyalty_identifier_context_mismatch')
                    for target,keys in KEYS.items():
                        if target=='provenance':continue
                        pk=keys[0]
                        relation_query(f'SELECT p.source_id,p.entity_id,p.source_record_id,p.locator,p.entity_id FROM read_parquet(?) p ANTI JOIN read_parquet(?) t ON p.entity_id=t.{_sql_identifier(pk)} AND p.source_id=t.source_id AND p.source_record_id=t.source_record_id WHERE p.entity_table=?',
                            [str(staging/'provenance.parquet'),str(staging/(target+'.parquet')),target],
                            'provenance','entity_id','orphan_provenance')
                    allowed=list(TABLES.values())
                    q="SELECT source_id,entity_id,source_record_id,locator,entity_table FROM read_parquet(?) WHERE entity_table NOT IN ("+','.join('?' for _ in allowed)+')'
                    relation_query(q,[str(staging/'provenance.parquet'),*allowed],'provenance','entity_table','unknown_provenance_target')
                error_input=staging/'normalization_errors.input.parquet'
                _write_query(db,'SELECT DISTINCT * FROM read_parquet(?) ORDER BY normalization_error_id',[[str(error_input),str(relation_path)]],staging/'normalization_errors.parquet',ERROR_SCHEMA,batch_size)
                errors_count=pq.ParquetFile(staging/'normalization_errors.parquet').metadata.num_rows
                # Counters describe accepted rows, with samples of actual before/after.
                journal=[]
                error_counts={(s,t,f,r):n for s,t,f,r,n in db.execute(
                    'SELECT source_id,entity_table,field,rule_id,COUNT(*) FROM read_parquet(?) GROUP BY source_id,entity_table,field,rule_id',
                    [str(staging/'normalization_errors.parquet')]).fetchall()}
                for (source,table,field,rule),stats in sorted(rule_stats.items()):
                    exceptions=error_counts.get((source,table,field,rule),0)
                    journal.append(dict(source_id=source,entity_table=table,field=field,rule_id=rule,version=VERSION,affected_records=stats['n'],exceptions=exceptions,examples_json=dumps(stats['samples'])))
                pq.write_table(pa.Table.from_pylist(journal,schema=LOG_SCHEMA),staging/'transformation_log.parquet',compression='zstd')
                unused={r['rule_id'] for group in rules.corrections.values() for r in group}-rules.used_corrections
                if unused:raise ValueError('Manual corrections did not match input records: '+', '.join(sorted(unused)))
                for path,before in fingerprints.items():
                    announce('T12: заключительная проверка входов')
                    if file_hash(path)!=before:raise RuntimeError('Input changed during T12: '+path)
                counts={t:pq.ParquetFile(staging/(t+'.parquet')).metadata.num_rows for t in TABLES.values()}
                for table,n in counts.items():
                    entries=[b for b in balances if b['entity_table']==table]
                    if sum(b['input_rows'] for b in entries)!=input_counts[table] or sum(b['output_rows'] for b in entries)!=n:
                        raise RuntimeError('Canonical source/entity balance failed: '+table)
                source_error_severities=dict(db.execute('SELECT severity,COUNT(*) FROM read_parquet(?) GROUP BY severity',[str(staging/'errors.parquet')]).fetchall())
                report=dict(status='complete_with_errors' if errors_count or source_error_severities.get('error') else 'complete',acceptance='pending_real_data_review',
                    version=VERSION,config_sha256=rules.sha256,inputs=fingerprints,input_schemas=input_schemas,input_counts=input_counts,counts=counts,normalization_errors=errors_count,
                    balances=balances,source_error_severities=source_error_severities,personal_sources_present=sorted(observed_sources),personal_sources_missing=missing,
                    all_personal_sources_present=not missing,source_reports=reports,seconds=round(time.perf_counter()-start,3),memory_limit=memory_limit,batch_size=batch_size,
                    limitations=['No identity resolution or substantive trip deduplication; T13 owns those decisions.',
                                 'Source-scoped document/ticket keys require confirmed type/issuer before cross-source matching.'])
                summary='Итог T12\n'+'\n'.join(f'{name}: {n}' for name,n in counts.items())+'\n'
                summary+=f"Повторно прочитанные строки удалены: {sum(b['duplicates_removed'] for b in balances)}\nОшибки значений/связей: {errors_count}\n"
                summary+=f"Подключено персональных источников: {len(observed_sources)} / 6\n"
                if missing:summary+='Пока отсутствуют: '+', '.join(missing)+'\n'
                summary+=f"Статус: {report['status']}; время: {report['seconds']} с\nРезультаты: {output}\n"
                (staging/'canonical_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                (staging/'canonical_summary.txt').write_text(summary,encoding='utf-8')
                names=[t+'.parquet' for t in TABLES.values()]+['normalization_errors.parquet','transformation_log.parquet','duplicate_reads.parquet','canonical_report.json','canonical_summary.txt']
                (staging/'canonical_manifest.json').write_text(json.dumps(dict(version=VERSION,files={n:file_hash(staging/n) for n in names}),indent=2)+'\n')
                publishing=True
                publish(staging,output,names+['canonical_manifest.json'])
                return report,summary
            except BaseException as exc:
                failure=dict(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',
                    version=VERSION,exception_type=type(exc).__name__,message=str(exc),inputs=fingerprints,
                    seconds=round(time.perf_counter()-start,3),previous_outputs_preserved=None if publishing else True)
                try:
                    (output/'canonical_last_failure.json').write_text(json.dumps(failure,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                except OSError:
                    pass
                raise
            finally:db.close()
