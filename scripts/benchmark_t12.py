"""Reproducible synthetic T12 benchmark; never uses personal source data."""
import argparse
import hashlib
import json
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
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rows',type=int,default=20000)
    parser.add_argument('--output',type=Path,default=ROOT/'data/benchmark/t12')
    parser.add_argument('--report',type=Path)
    args=parser.parse_args()
    if args.rows<1:raise ValueError('rows must be positive')
    inp=args.output/'input';inp.mkdir(parents=True,exist_ok=True)
    def h(value):return hashlib.sha256(value.encode()).hexdigest()
    def base(i,kind):return dict(source_id='sirena_fixed',source_record_id=h(kind+str(i)),locator=kind+':'+str(i),raw_json=json.dumps({'benchmark_only':True,'original':'x'*512}))
    data={
        'person_observations':[{**base(i,'p'),'observation_id':h('p'+str(i)),'name_raw':'Иванов Иван '+str(i%100),'birth_date_raw':'03/02/1980','birth_date':'1980-02-03'} for i in range(args.rows)],
        'identifiers':[{**base(i,'i'),'identifier_id':h('i'+str(i)),'observation_id':h('p'+str(i)),'kind':'loyalty_card','programme':'FB','value':f'{i:08d}'} for i in range(args.rows)],
        'travel_observations':[{**base(i,'t'),'travel_id':h('t'+str(i)),'observation_id':h('p'+str(i%args.rows)),'loyalty_identifier_id':h('i'+str(i%args.rows)),'flight_number':'AB123','departure_raw':'2017-01-02','departure_local':'2017-01-02','departure_precision':'date'} for i in range(args.rows*2)],
        'schedules':[], 'errors':[]}
    provenance=[]
    for table,rows in data.items():
        for r in rows:provenance.append({**{k:r[k] for k in ('source_id','source_record_id','locator')},'entity_table':table,'entity_id':r[ID_COLUMNS[table]],'transformation':'reader:1.0'})
    data['provenance']=provenance
    for table,rows in data.items():pq.write_table(pa.Table.from_pylist(rows,schema=SCHEMAS[table]),inp/(table+'.parquet'))
    (inp/'source_report.json').write_text(json.dumps(dict(source_id='sirena_fixed',status='complete',counts={t:len(v) for t,v in data.items()})))
    del data,provenance
    report,_=build([inp],args.output/'canonical',ROOT/'configs/normalization.yaml')
    result=dict(scope='synthetic_only',people=args.rows,identifiers=args.rows,travels=args.rows*2,
                total_input_rows=args.rows*8,seconds=report['seconds'],normalization_errors=report['normalization_errors'],counts=report['counts'])
    assert result['normalization_errors']==0
    assert report['counts']['observations']==args.rows
    assert report['counts']['travel_observations']==args.rows*2
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
