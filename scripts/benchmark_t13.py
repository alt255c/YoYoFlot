"""Synthetic T13 benchmark in a fresh process; never reads personal data."""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))


def main():
    import pyarrow as pa
    import pyarrow.parquet as pq
    from yoyoflot.contracts import SCHEMAS,ID_COLUMNS
    from yoyoflot.normalize.canonical import build
    from yoyoflot.normalize.geography import run as geography
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--people',type=int,default=20000)
    parser.add_argument('--output',type=Path,default=ROOT/'data/benchmark/t13')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.people<1:raise ValueError('people must be positive')
    raw=args.output/'readers';raw.mkdir(parents=True,exist_ok=True)
    def h(*values):return hashlib.sha256(json.dumps(values).encode()).hexdigest()
    def base(i,s,kind,k=0):
        return dict(source_id=s,source_record_id=h(i,s,kind,k),locator=f'{kind}:{i}:{k}',
                    raw_json=json.dumps({'synthetic_benchmark':True,'payload':'x'*256}))
    def generate(table):
        for i in range(args.people):
            for source in ('boarding_csv','forum_json'):
                pid=h(i,source,'person')
                if table=='person_observations':
                    yield dict(base(i,source,'person'),observation_id=pid,name_raw=f'Bench Person{i}',
                               name_normalized=f'bench person{i}')
                elif table=='identifiers':
                    yield dict(base(i,source,'card'),identifier_id=h(i,source,'card'),observation_id=pid,
                               kind='loyalty_card',programme='FB',value=f'00{i:08d}')
                elif table=='travel_observations':
                    for k in range(2 if source=='boarding_csv' else 1):
                        yield dict(base(i,source,'travel',k),travel_id=h(i,source,'travel',k),observation_id=pid,
                                   event_kind='boarding_record' if source=='boarding_csv' else 'registered_flight',
                                   origin_raw='SVO',destination_raw='LED',flight_number='SU001',
                                   departure_raw='2017-01-02T10:30',departure_local='2017-01-02T10:30',
                                   departure_precision='minute',ticket_number=f'009{i:08d}')
    counts={}
    for table in ('person_observations','identifiers','travel_observations','schedules','errors'):
        count=0;buffer=[]
        with pq.ParquetWriter(raw/(table+'.parquet'),SCHEMAS[table],compression='zstd') as writer:
            for row in generate(table):
                buffer.append(row);count+=1
                if len(buffer)>=4000:
                    writer.write_table(pa.Table.from_pylist(buffer,schema=SCHEMAS[table]));buffer.clear()
            if buffer:writer.write_table(pa.Table.from_pylist(buffer,schema=SCHEMAS[table]))
        counts[table]=count
    with pq.ParquetWriter(raw/'provenance.parquet',SCHEMAS['provenance'],compression='zstd') as writer:
        for table in counts:
            for batch in pq.ParquetFile(raw/(table+'.parquet')).iter_batches(batch_size=4000):
                records=[{**{k:r[k] for k in ('source_id','source_record_id','locator')},'entity_table':table,
                          'entity_id':r[ID_COLUMNS[table]],'transformation':'synthetic:benchmark'} for r in batch.to_pylist()]
                if records:writer.write_table(pa.Table.from_pylist(records,schema=SCHEMAS['provenance']))
    canonical=args.output/'canonical'
    build([raw],canonical,ROOT/'configs/normalization.yaml')
    geography([canonical],canonical,ROOT/'data/reference/airports.csv',ROOT/'configs/airports_overrides.yaml')
    started=time.perf_counter()
    result=subprocess.run([sys.executable,str(ROOT/'scripts/run_t13.py'),'--input',str(canonical),
        '--output',str(args.output/'resolved'),'--review-output',str(args.output/'review'),'-q'],
        capture_output=True,text=True,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    if result.returncode or result.stdout or result.stderr:
        raise RuntimeError(f'Benchmark child failed or quiet output was not empty: {result.returncode}\n{result.stdout}\n{result.stderr}')
    report=json.loads((args.output/'resolved/resolution_report.json').read_text(encoding='utf-8'))
    assert report['counts']['persons']==args.people
    assert report['counts']['segments']==args.people
    assert report['graph_eligible_segments']==args.people
    peak=None
    if sys.platform=='linux':
        import resource
        peak=round(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss/1024,2)
    measurement=dict(scope='synthetic_only',input_observations=args.people*2,input_identifiers=args.people*2,
        input_travels=args.people*3,person_pairs=report['counts']['person_pairs'],persons=report['counts']['persons'],
        segments=report['counts']['segments'],graph_eligible_segments=report['graph_eligible_segments'],
        resolution_seconds=report['seconds'],fresh_process_wall_seconds=round(time.perf_counter()-started,3),
        fresh_process_peak_rss_mib=peak,quiet_stdout_empty=True,quiet_stderr_empty=True,
        memory_limit=report['memory_limit'],limitations='Synthetic sizes/payloads do not predict full real run time or accuracy.')
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(measurement,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(measurement,ensure_ascii=False))


if __name__=='__main__':main()
