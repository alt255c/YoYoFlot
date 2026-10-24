"""Reproduce T13 aggregate OOM and test its fix at real row counts, on synthetic data.

This tests input preparation and candidate construction, not human matching
quality or the full pipeline. Each measurement runs in a fresh child process.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))


def generate(output, observations, identifiers):
    import duckdb
    output.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect()
    try:
        db.execute("SET memory_limit='512MB'")
        db.execute('SET threads=1')
        db.execute('SET preserve_insertion_order=false')
        db.execute('SET temp_directory=?', [str(output/'generation_spill')])
        db.execute(f'''CREATE VIEW synthetic_people AS SELECT i,
            sha256('observation:'||i) observation_id,
            list_extract(['boarding_csv','boardingpass_xlsx','forum_json','pointz_xml','sirena_fixed','skyteam_yaml'],i%6+1) source_id
            FROM range({observations}) n(i)''')
        db.sql('''SELECT observation_id,source_id,sha256('record:'||i) source_record_id,
            'synthetic/person/'||i||'/'||sha256('locator:'||i) locator,
            'Bench Person'||i name_raw,'bench person'||i name_comparison,'bench person'||i name_latin_comparison,
            NULL::VARCHAR birth_date,'ok' normalization_status FROM synthetic_people''').write_parquet(str(output/'observations.parquet'))
        db.sql(f'''WITH rows AS (SELECT i,i%{observations} n FROM range({identifiers}) s(i)),
            values_ AS (SELECT r.i,p.observation_id,p.source_id,
                CASE WHEN n%4=0 THEN 'document' ELSE 'loyalty_card' END kind,
                '00'||(n//2)::VARCHAR value_normalized,
                CASE WHEN n%4=0 THEN 'source_scoped' ELSE 'programme_scoped' END namespace_status,
                to_json([CASE WHEN n%4=0 THEN 'document' ELSE 'loyalty_card' END,
                    CASE WHEN n%4=0 THEN 'source' ELSE 'programme' END,
                    CASE WHEN n%4=0 THEN p.source_id ELSE 'fb' END])::VARCHAR identifier_namespace
                FROM rows r JOIN synthetic_people p ON p.i=r.n)
            SELECT observation_id,sha256('identifier:'||i) identifier_id,source_id,
                sha256('identifier_record:'||i) source_record_id,
                'synthetic/identifier/'||i||'/'||sha256('identifier_locator:'||i) locator,
                kind,value_normalized,identifier_namespace,
                to_json([identifier_namespace,value_normalized])::VARCHAR identifier_match_key,
                namespace_status,'ok' normalization_status FROM values_''').write_parquet(str(output/'identifiers.parquet'))
    finally:
        db.close()


def worker(args):
    import duckdb
    import yoyoflot.resolve.blocking as blocking
    from yoyoflot.resolve.common import Progress, Rules
    from yoyoflot.resolve import VERSION
    stage = args.output/args.worker
    stage.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(str(stage/'work.duckdb'))
    report = dict(mode=args.worker,scope='synthetic_preparation_and_blocking_only',
                  resolution_version=VERSION,memory_limit=args.memory_limit,
                  threads=2 if args.worker=='unbounded_control' else 1)
    started = time.perf_counter()
    meter = Progress(lambda message: print(message,flush=True))
    try:
        db.execute('SET memory_limit=?', [args.memory_limit])
        db.execute('SET threads=?', [report['threads']])
        db.execute('SET preserve_insertion_order=false')
        db.execute('SET temp_directory=?', [str(stage/'spill')])
        if args.worker=='unbounded_control':
            # Same aggregate expressions, but across the entire snapshot as in
            # 0.6.0. Catch only the expected DuckDB OOM, not unrelated failures.
            def unbounded(db, table, query, group_count, params=(), progress=None):
                db.execute('CREATE TABLE '+table+' AS '+query, [*params, 0, 2**63-1])
            blocking.aggregate_ranges = unbounded
        with meter as progress:
            count,pairs,upper = blocking.build_blocks(db,args.output/'input/observations.parquet',
                args.output/'input/identifiers.parquet',Rules(ROOT/'configs/resolution.yaml'),stage,progress)
        evidence = db.execute('SELECT sum(json_array_length(identifiers_json)) FROM identifier_evidence').fetchone()[0]
        documents = db.execute('SELECT sum(json_array_length(documents_json)) FROM document_sets').fetchone()[0]
        nodes = db.execute('SELECT count(*) FROM nodes').fetchone()[0]
        assert count==nodes==args.observations
        assert evidence==args.identifiers
        assert documents==(args.observations+3)//4
        assert pairs==args.observations//4
        assert db.execute('SELECT count(*) FROM candidates').fetchone()[0]==pairs
        report.update(status='complete',observations=count,identifiers=evidence,
                      distinct_document_values=documents,candidate_pairs=pairs,candidate_upper_bound=upper,
                      balances_verified=True)
    except duckdb.OutOfMemoryException as exc:
        report.update(status='out_of_memory',exception_type=type(exc).__name__,message=str(exc))
        if args.worker!='unbounded_control':
            raise
    finally:
        db.close()
        report['last_phase']=meter.name
        report['seconds']=round(time.perf_counter()-started,3)
        report['peak_rss_mib']=None
        if sys.platform=='linux':
            import resource
            report['peak_rss_mib']=round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,2)
        (stage/'measurement.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--observations',type=int,default=2746977)
    parser.add_argument('--identifiers',type=int,default=3207435)
    parser.add_argument('--memory-limit',default='512MB')
    parser.add_argument('--output',type=Path,default=ROOT/'data/benchmark/t13_memory')
    parser.add_argument('--report',type=Path)
    parser.add_argument('--worker',choices=('unbounded_control','fixed'),help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.observations<4 or args.identifiers<args.observations:
        raise ValueError('Need at least four observations and one identifier per observation')
    if args.worker:
        worker(args)
        return
    if (args.output/'fixed/work.duckdb').exists() or (args.output/'unbounded_control/work.duckdb').exists():
        raise ValueError('Use a fresh benchmark output directory')
    print('Synthetic input generation',flush=True)
    generate(args.output/'input',args.observations,args.identifiers)
    measurements={}
    for mode in ('unbounded_control','fixed'):
        result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',mode,
            '--observations',str(args.observations),'--identifiers',str(args.identifiers),
            '--memory-limit',args.memory_limit,'--output',str(args.output)],
            env={**os.environ,'PYTHONIOENCODING':'utf-8'})
        if result.returncode:
            raise RuntimeError(f'{mode} child failed: {result.returncode}')
        measurements[mode]=json.loads((args.output/mode/'measurement.json').read_text(encoding='utf-8'))
    report=dict(scope='synthetic_preparation_and_blocking_only',observations=args.observations,
                identifiers=args.identifiers,measurements=measurements,
                limitations='Same row counts are not the same real payloads, candidate distribution, full run or identity accuracy.')
    target=args.report or args.output/'benchmark.json'
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
