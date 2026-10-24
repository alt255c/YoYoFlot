"""Rebuild the bundled CSV from pinned local OpenFlights bytes, without network."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ['airport_id','name','city','country','iata','icao','latitude','longitude','timezone']


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',type=Path,default=ROOT/'data/reference')
    args=parser.parse_args()
    directory=args.directory
    info=json.loads((directory/'reference_manifest.json').read_text(encoding='utf-8'))
    data=(directory/'openflights_airports.dat').read_bytes()
    if hashlib.sha256(data).hexdigest()!=info['files']['openflights_airports.dat']:
        raise ValueError('Upstream snapshot differs from the pinned manifest')
    target=directory/'airports.csv'
    temp=target.with_suffix('.csv.tmp')
    count=0
    try:
        with temp.open('w',newline='',encoding='utf-8') as f:
            writer=csv.DictWriter(f,fieldnames=FIELDS)
            writer.writeheader()
            for r in csv.reader(data.decode('utf-8').splitlines()):
                if len(r)!=14:
                    raise ValueError('Unexpected OpenFlights format')
                if r[12]!='airport':
                    continue
                writer.writerow(dict(airport_id='openflights:'+r[0],name=r[1],city=r[2],country=r[3],
                    iata='' if r[4]=='\\N' else r[4],icao='' if r[5]=='\\N' else r[5],
                    latitude=r[6],longitude=r[7],timezone='' if r[11]=='\\N' else r[11]))
                count+=1
        if count!=info['rows'] or hashlib.sha256(temp.read_bytes()).hexdigest()!=info['files']['airports.csv']:
            raise ValueError('Rebuilt reference differs from the pinned derived CSV')
        os.replace(temp,target)
    finally:
        temp.unlink(missing_ok=True)
    print(f'Airport reference verified and rebuilt: {count} rows')


if __name__=='__main__':
    main()
