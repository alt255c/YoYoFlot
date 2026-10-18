"""Read-only, parameterized queries against one verified product snapshot."""
import csv
import json
import math
from contextlib import contextmanager
from pathlib import Path

import duckdb

from ..provenance import fingerprint
from .build import VERSION, _digest, _rows


def _json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class ProductStore:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.manifest = _json_file(self.path/'run_manifest.json')
        m = self.manifest
        if (m.get('schema_version') != VERSION or m.get('status') != 'complete' or m.get('data_mode') != 'source_dataset'
                or m.get('run_id') != 'product-'+_digest(m.get('request'))[:20]):
            raise ValueError('Not a completed source-dataset product')
        files=m.get('files',{})
        required={'tables/top50.parquet','tables/rankings.parquet','tables/explanations.parquet',
                  'tables/segments.parquet','tables/persons.parquet','tables/features.parquet',
                  'airports.csv','reference_manifest.json','product.yaml','evaluation.json','forest.json','graph.json','rules.json','review.csv'}
        if not isinstance(files,dict) or not required<=files.keys():
            raise ValueError('Incomplete product snapshot')
        for name,checksum in files.items():
            file=self.path/name
            if Path(name).is_absolute() or '..' in Path(name).parts or not file.is_file() or file.is_symlink() or self.path not in file.resolve().parents or fingerprint(file)!=checksum:
                raise ValueError(f'Product checksum/path mismatch: {name}')
        self.airports={}
        with (self.path/'airports.csv').open(encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                if row['airport_id'] in self.airports:raise ValueError('Duplicate airport reference key')
                for field in ['latitude','longitude']:
                    row[field]=float(row[field]) if row[field] else None
                    if row[field] is not None and (not math.isfinite(row[field]) or abs(row[field])>(90 if field=='latitude' else 180)):
                        raise ValueError('Invalid airport coordinate')
                self.airports[row['airport_id']]=row

    @contextmanager
    def connection(self):
        with duckdb.connect() as db:
            db.execute("SET threads=2")
            db.execute("SET memory_limit='256MB'")
            for p in (self.path/'tables').glob('*.parquet'):
                if p.name in self.manifest['files'] or 'tables/'+p.name in self.manifest['files']:
                    db.read_parquet(str(p)).create_view(p.stem)
            if 'if_scores.parquet' in self.manifest['files']:
                db.read_parquet(str(self.path/'if_scores.parquet')).create_view('if_scores')
            yield db

    def metadata(self):
        return dict(self.manifest,evaluation=_json_file(self.path/'evaluation.json'),
                    forest=_json_file(self.path/'forest.json'),graph=_json_file(self.path/'graph.json'),rules=_json_file(self.path/'rules.json'))

    def top(self, limit=50):
        if type(limit) is not int or not 1<=limit<=50:raise ValueError('Limit must be 1–50')
        with self.connection() as db:
            return _rows(db,'SELECT * FROM top50 ORDER BY rank LIMIT ?',[limit])

    def profile(self, person_id):
        if not isinstance(person_id,str) or not person_id or len(person_id)>256:raise ValueError('Invalid person ID')
        with self.connection() as db:
            people=_rows(db,'SELECT * FROM rankings WHERE person_id=?',[person_id])
            if not people:raise KeyError('Profile not found')
            person=people[0]
            factors=_rows(db,'SELECT * FROM explanations WHERE person_id=? ORDER BY contribution DESC,criterion_id',[person_id])
            for factor in factors:
                if factor['status']=='unavailable':
                    factor['explanation_ru']=factor['title_ru']+': '+factor['availability_reason']+'.'
                    factor['value']=None
                else:
                    unit='перелётов / 30 дней' if factor['criterion_id']=='M01' else 'доля'
                    factor['explanation_ru']=(f"{factor['title_ru']}: {factor['value']:.4g} ({unit}), порог ≥ {factor['threshold']:.4g}. "
                        f"Наблюдений: {factor['used_observations']}. Вес: {factor['weight']:.4g}; вклад: {factor['contribution']:.4g}. "
                        + (f"Калибровочная выборка: {factor['reference_size']} профилей." if factor['reference_size'] else "Фиксированный порог."))
            segments=_rows(db,'SELECT * FROM segments WHERE person_id=? ORDER BY departure_date NULLS LAST,segment_id',[person_id])
            for s in segments:
                s['origin']=self.airports.get(s['origin_airport_id'])
                s['destination']=self.airports.get(s['destination_airport_id'])
                s['is_plottable']=all(s[side] and s[side]['latitude'] is not None and s[side]['longitude'] is not None for side in ['origin','destination'])
                s['unplottable_reason_ru']=None if s['is_plottable'] else 'Неизвестны координаты одного или обоих аэропортов'
                s['departure_label_ru']=s['departure_date'] or 'Дата неизвестна'
            # Per-criterion evidence is derived from the very same frozen rows
            # and formulas, never by a text-generation model.
            for factor in factors:
                cid=factor['criterion_id']
                if cid=='M01':ids=[s['segment_id'] for s in segments if s['departure_date']]
                elif cid=='M03':ids=[s['segment_id'] for s in segments if s['departure_date'] and s['origin_airport_id'] and s['destination_airport_id'] and s['identity_status']=='accepted' and s['resolution_status']=='resolved']
                else:
                    ids=[r['segment_id'] for r in _rows(db,'''WITH dated AS (
                        SELECT *,count(*) OVER(PARTITION BY departure_date) day_count FROM segments WHERE person_id=?), ordered AS (
                        SELECT *,lag(segment_id) OVER w prev_id,lag(departure_date) OVER w prev_date,
                        lag(day_count) OVER w prev_count,lag(origin_airport_id) OVER w prev_origin,
                        lag(destination_airport_id) OVER w prev_destination,
                        lag(identity_status) OVER w prev_identity,lag(resolution_status) OVER w prev_resolution
                        FROM dated WHERE departure_date IS NOT NULL WINDOW w AS(ORDER BY departure_date,segment_id)), pairs AS (
                        SELECT * FROM ordered WHERE day_count=1 AND prev_count=1 AND origin_airport_id=prev_destination
                        AND destination_airport_id=prev_origin AND identity_status='accepted' AND prev_identity='accepted'
                        AND resolution_status='resolved' AND prev_resolution='resolved'
                        AND date_diff('day',cast(prev_date AS DATE),cast(departure_date AS DATE)) BETWEEN 1 AND 2)
                        SELECT segment_id FROM pairs UNION SELECT prev_id segment_id FROM pairs''',[person_id])]
                factor['evidence_segment_ids']=sorted(ids) if factor['status']!='unavailable' else []
            evidence=[]
            if 'tables/segment_links.parquet' in self.manifest['files']:
                evidence=_rows(db,'SELECT * FROM segment_links WHERE person_id=? ORDER BY segment_id,travel_id',[person_id])
            companions=[]
            if 'tables/graph_edges.parquet' in self.manifest['files']:
                companions=_rows(db,'''SELECT CASE WHEN person_a=? THEN person_b ELSE person_a END person_id,
                    shared_events,shared_days,inverse_occupancy_support,event_ids
                    FROM graph_edges WHERE person_a=? OR person_b=? ORDER BY shared_days DESC,person_id''',[person_id]*3)
            forest=None
            if 'if_scores.parquet' in self.manifest['files']:
                scores=_rows(db,'SELECT * FROM if_scores WHERE person_id=?',[person_id])
                forest=scores[0] if scores else None
        return {'run_id':self.manifest['run_id'],'person':person,'factors':factors,'segments':segments,
                'route_balance':{'total':len(segments),'plottable':sum(s['is_plottable'] for s in segments),
                                 'unplottable':sum(not s['is_plottable'] for s in segments)},
                'source_evidence':evidence,'companions':companions,'forest':forest}
