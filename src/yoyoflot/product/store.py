"""Read-only, parameterized queries against one verified product snapshot."""
import csv
import json
import math
import os
from contextlib import contextmanager
from pathlib import Path

import duckdb

from ..provenance import fingerprint
from .build import VERSION, _digest, _rows
from .assessment import TABLES as ASSESSMENT_TABLES, verify as verify_assessment


def _json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class ProductStore:
    def __init__(self, path, allow_demo=False):
        self.path = Path(path).resolve()
        self.manifest = _json_file(self.path/'run_manifest.json')
        m = self.manifest
        self.data_mode = m.get('data_mode')
        valid_mode = self.data_mode == 'source_dataset' or (allow_demo and self.data_mode == 'demo')
        if (m.get('schema_version') != VERSION or m.get('status') != 'complete' or not valid_mode
                or m.get('run_id') != 'product-'+_digest(m.get('request'))[:20]):
            raise ValueError('Not a completed supported product')
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
        # The verified supplement is separate from the frozen ranking. Detail
        # rows are queried only when the selected profile is opened.
        self.assessment_manifest = None
        self.assessment_report = None
        self.assessment_root = self._find_assessment_root()
        self.airports={}
        with (self.path/'airports.csv').open(encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                if row['airport_id'] in self.airports:raise ValueError('Duplicate airport reference key')
                for field in ['latitude','longitude']:
                    row[field]=float(row[field]) if row[field] else None
                    if row[field] is not None and (not math.isfinite(row[field]) or abs(row[field])>(90 if field=='latitude' else 180)):
                        raise ValueError('Invalid airport coordinate')
                self.airports[row['airport_id']]=row

    def _find_assessment_root(self):
        configured = os.environ.get('YOYO_ASSESSMENT_ROOT')
        root = Path(configured).expanduser().resolve() if configured else self.path.parent / 'source-assessment'
        if not (root / 'assessment_manifest.json').is_file():
            if configured:
                raise ValueError('Configured source assessment is missing')
            return None
        product_hash = fingerprint(self.path / 'run_manifest.json')
        if not configured and _json_file(root / 'assessment_manifest.json').get('request', {}).get('product_manifest_sha256') != product_hash:
            # A neighbour belonging to another uploaded product must not be
            # attached or prevent opening a valid baseline-only snapshot.
            return None
        self.assessment_manifest, self.assessment_report = verify_assessment(root, product_hash)
        return root

    def _source_details(self, db, person):
        if self.assessment_root is None:
            return {'available': False, 'reason': 'Набор проверенных связей с источниками ещё не подготовлен',
                    'observations_count': 0, 'travels_count': 0, 'identifiers': [], 'documents': []}
        person_id = person['person_id']
        identifiers = _rows(db, """SELECT * FROM assessment_profile_identifiers
            WHERE person_id=? ORDER BY source_id,locator,identifier_id""", [person_id])
        info = _rows(db, 'SELECT * FROM assessment_profile_assessments WHERE person_id=?', [person_id])[0]
        travels = db.execute('SELECT count(*) FROM assessment_segment_source_links WHERE person_id=?', [person_id]).fetchone()[0]
        return {'available': True, 'complete': info['source_links_complete'],
                'expected_observations': info['expected_observations'], 'fallback_used': False,
                'link_policy': 'exact_anchors_and_verified_source_version_aliases',
                'observations_count': info['linked_observations'], 'travels_count': travels,
                'identifiers': identifiers, 'documents': [r for r in identifiers if r['kind']=='document']}

    def assessment_statistics(self, ids=None):
        if self.assessment_root is not None and ids is None:
            return dict(self.assessment_report['statistics'], source_assessment_available=True,
                        ranked_with_signals=sum(row['profiles'] for row in self.manifest.get('eligibility', [])
                                                if row['eligibility_status']=='eligible'))
        with self.connection() as db:
            params = [ids] if ids is not None else []
            where = ' WHERE person_id IN (SELECT unnest(?::VARCHAR[]))' if ids is not None else ''
            if self.assessment_root is not None:
                data = _rows(db, """SELECT count(*) profiles,
                    count(*) FILTER(WHERE evaluated_criteria_count>0) assessed,
                    count(*) FILTER(WHERE assessment_status='eligible') with_signals,
                    count(*) FILTER(WHERE assessment_status='no_matches') without_signals,
                    count(*) FILTER(WHERE assessment_status='insufficient_data') unavailable,
                    count(*) FILTER(WHERE identity_accepted) identity_accepted,
                    count(*) FILTER(WHERE source_links_complete) source_links_complete
                    FROM assessment_profile_assessments""" + where, params)[0]
            else:
                data = _rows(db, """SELECT count(*) profiles,
                    count(*) FILTER(WHERE available_criteria_count>0) assessed,
                    count(*) FILTER(WHERE eligibility_status='eligible') with_signals,
                    count(*) FILTER(WHERE eligibility_status='no_matches') without_signals,
                    count(*) FILTER(WHERE available_criteria_count=0) unavailable,
                    count(*) FILTER(WHERE identity_status='accepted') identity_accepted
                    FROM rankings""" + where, params)[0]
            data['source_assessment_available'] = self.assessment_root is not None
            return data

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
            if self.assessment_root is not None:
                for name in ASSESSMENT_TABLES:
                    db.read_parquet(str(self.assessment_root / (name + '.parquet'))).create_view('assessment_' + name)
            yield db

    def metadata(self):
        return dict(self.manifest, data_mode=self.data_mode, evaluation=_json_file(self.path/'evaluation.json'),
                    forest=_json_file(self.path/'forest.json'),graph=_json_file(self.path/'graph.json'),rules=_json_file(self.path/'rules.json'),
                    assessment=self.assessment_statistics(),
                    source_assessment=(dict(run_id=self.assessment_manifest['run_id'],
                        prepared_source_markers=self.assessment_report['prepared_source_markers'],
                        pending_source_markers=self.assessment_report['pending_source_markers']) if self.assessment_manifest else None))

    def top(self, limit=50):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('Limit must be 1–1000')
        with self.connection() as db:
            relation = 'rankings LEFT JOIN assessment_profile_assessments USING(person_id)' if self.assessment_root else 'rankings'
            return _rows(db,f'''SELECT * FROM {relation}
                WHERE eligibility_status='eligible' AND rank IS NOT NULL
                ORDER BY rank LIMIT ?''',[limit])

    def users(self, limit=50, offset=0, query='', eligibility='all', sort='rank'):
        """Return a bounded, searchable page of every profile in the model."""
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Limit must be 1–1000')
        if type(offset) is not int or not 0 <= offset <= 10_000_000:
            raise ValueError('Offset must be between 0 and 10000000')
        if not isinstance(query, str) or len(query) > 120:
            raise ValueError('Search query is too long')
        if eligibility not in {'all', 'eligible', 'no_matches', 'insufficient_data',
                               'assessed', 'not_assessed', 'identity_accepted', 'identity_ambiguous'}:
            raise ValueError('Unknown eligibility filter')
        if sort not in {'rank', 'score', 'name'}:
            raise ValueError('Unknown sort order')
        order = {
            'rank': "CASE WHEN eligibility_status='eligible' THEN 0 ELSE 1 END, rank NULLS LAST, matched_criteria_count DESC, rule_score DESC, person_id",
            'score': 'rule_score DESC NULLS LAST, matched_criteria_count DESC, person_id',
            'name': 'lower(coalesce(display_name, \'\')), person_id',
        }[sort]
        clauses=[]
        params=[]
        if eligibility in {'assessed', 'not_assessed'}:
            field = 'evaluated_criteria_count' if self.assessment_root else 'available_criteria_count'
            clauses.append(field + ('>0' if eligibility=='assessed' else '=0'))
        elif eligibility in {'identity_accepted', 'identity_ambiguous'}:
            clauses.append('identity_status=?')
            params.append('accepted' if eligibility=='identity_accepted' else 'ambiguous')
        elif eligibility != 'all':
            field = 'assessment_status' if self.assessment_root else 'eligibility_status'
            clauses.append(field+'=?')
            params.append(eligibility)
        if query.strip():
            needle=f'%{query.strip().lower()}%'
            clauses.append("(lower(coalesce(display_name,'')) LIKE ? OR lower(person_id) LIKE ?)")
            params.extend([needle, needle])
        where=' WHERE '+ ' AND '.join(clauses) if clauses else ''
        with self.connection() as db:
            relation = 'rankings LEFT JOIN assessment_profile_assessments USING(person_id)' if self.assessment_root else 'rankings'
            total=db.execute(f'SELECT count(*) FROM {relation}{where}', params).fetchone()[0]
            rows=_rows(db, f'SELECT * FROM {relation}{where} ORDER BY {order} LIMIT ? OFFSET ?', params+[limit,offset])
        return {'items':rows, 'pagination':{'offset':offset,'limit':limit,'total':total,'has_next':offset+len(rows)<total}}

    def source_details(self, person_id):
        """Read source parquet details for one profile only when requested."""
        if not isinstance(person_id,str) or not person_id or len(person_id)>256:
            raise ValueError('Invalid person ID')
        with self.connection() as db:
            people=_rows(db,'SELECT * FROM rankings WHERE person_id=?',[person_id])
            if not people:
                raise KeyError('Profile not found')
            person=people[0]
            return self._source_details(db, person)

    def profile(self, person_id):
        if not isinstance(person_id,str) or not person_id or len(person_id)>256:raise ValueError('Invalid person ID')
        with self.connection() as db:
            people=_rows(db,'SELECT * FROM rankings WHERE person_id=?',[person_id])
            if not people:raise KeyError('Profile not found')
            person=people[0]
            additional_factors=[]
            if self.assessment_root:
                person.update(_rows(db, 'SELECT * FROM assessment_profile_assessments WHERE person_id=?', [person_id])[0])
                additional_factors=_rows(db, '''SELECT * FROM assessment_marker_coverage
                    WHERE person_id=? AND NOT included_in_ranking ORDER BY criterion_id''',[person_id])
                for factor in additional_factors:
                    if factor['status']=='unavailable':
                        factor['explanation_ru']=factor['availability_reason']
                    else:
                        factor['explanation_ru']=(f"Проверены связанные исходные записи: {factor['used_observations']}. "
                            f"Результат: {'есть совпадение' if factor['status']=='matched' else 'совпадений не найдено'}. "
                            f"Вес: {factor['weight']}; вклад после подключения нового рейтинга: {factor['preview_contribution']}. "
                            "Текущий основной балл не изменён.")
                    factor['evidence_segment_ids']=[r['segment_id'] for r in _rows(db, '''SELECT DISTINCT segment_id
                        FROM assessment_marker_evidence WHERE person_id=? AND criterion_id=? AND segment_id IS NOT NULL
                        ORDER BY segment_id''',[person_id,factor['criterion_id']])]
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
                'additional_factors':additional_factors,
                'route_balance':{'total':len(segments),'plottable':sum(s['is_plottable'] for s in segments),
                                 'unplottable':sum(not s['is_plottable'] for s in segments)},
                'source_evidence':evidence,'companions':companions,'forest':forest}
