"""Read-only, parameterized queries against one verified product snapshot."""
import csv
import json
import math
import os
import threading
from contextlib import contextmanager
from pathlib import Path

import duckdb

from ..provenance import fingerprint
from .build import VERSION, _digest, _rows


def _json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def _name_key(value):
    return ' '.join(str(value or '').casefold().split())


def _name_token_key(value):
    return ' '.join(sorted(_name_key(value).split()))


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
        # Source details are loaded lazily when a profile is opened.  They are
        # deliberately kept outside the frozen product snapshot: the ranking
        # remains reproducible while the UI can inspect the canonical parquet
        # rows belonging to one selected person without rebuilding the model.
        self.canonical_root = self._find_canonical_root()
        self._document_index = None
        self._document_index_lock = threading.Lock()
        self.airports={}
        with (self.path/'airports.csv').open(encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                if row['airport_id'] in self.airports:raise ValueError('Duplicate airport reference key')
                for field in ['latitude','longitude']:
                    row[field]=float(row[field]) if row[field] else None
                    if row[field] is not None and (not math.isfinite(row[field]) or abs(row[field])>(90 if field=='latitude' else 180)):
                        raise ValueError('Invalid airport coordinate')
                self.airports[row['airport_id']]=row

    def _find_canonical_root(self):
        configured = os.environ.get('YOYO_CANONICAL_ROOT')
        candidates = []
        if configured:
            candidates.append(Path(configured).expanduser())
        # The development checkout keeps T14 canonical parquet files next to
        # the product.  Pick the newest complete directory when no override is
        # supplied, so a later canonical run is used automatically.
        repo_root = self.path.parent.parent
        candidates.extend(sorted((repo_root / 'data' / 'processed').glob('t14_canonical_*/'), reverse=True))
        for root in candidates:
            root = root.resolve()
            required = ('observations.parquet', 'travel_observations.parquet', 'identifiers.parquet')
            if root.is_dir() and all((root / name).is_file() for name in required):
                return root
        return None

    def _source_details(self, db, person, segments):
        """Read canonical source rows for one profile on demand.

        The source parquet is not part of the product manifest and is never
        read while building the ranking.  We use the frozen anchor observation
        and the segment anchor travel IDs to recover the exact canonical
        observations represented by this profile, then expose their original
        identifiers and raw payloads for inspection.
        """
        if self.canonical_root is None:
            return {'available': False, 'reason': 'Канонический source parquet не подключён',
                    'observations_count': 0, 'travels_count': 0, 'identifiers': [], 'documents': []}
        anchor = None
        # rankings intentionally contains only model-facing fields; the
        # anchor is read from the frozen persons table using the same person_id.
        anchor_row = _rows(db, 'SELECT anchor_observation_id,observation_count,birth_date FROM persons WHERE person_id=?', [person['person_id']])
        if anchor_row:
            anchor = anchor_row[0].get('anchor_observation_id')
        expected_observations = anchor_row[0].get('observation_count') if anchor_row else None
        birth_date = anchor_row[0].get('birth_date') if anchor_row else None
        travel_ids = sorted({s.get('anchor_travel_id') for s in segments if s.get('anchor_travel_id')})
        root = self.canonical_root
        observations_path = str(root / 'observations.parquet')
        travels_path = str(root / 'travel_observations.parquet')
        identifiers_path = str(root / 'identifiers.parquet')
        # Resolve the full observation component through the canonical
        # identifier match keys.  This recovers all source observations for a
        # person (including rows that are not the segment's anchor row) while
        # retaining a conservative fallback for a conflicting identifier.
        observation_ids = {anchor} if anchor else set()
        anchor_keys = _rows(db, '''SELECT DISTINCT identifier_match_key
            FROM read_parquet(?) WHERE observation_id=? AND identifier_match_key IS NOT NULL''',
            [identifiers_path, anchor]) if anchor else []
        keys = sorted({row['identifier_match_key'] for row in anchor_keys if row.get('identifier_match_key')})
        if keys:
            linked = _rows(db, '''SELECT DISTINCT observation_id FROM read_parquet(?)
                WHERE identifier_match_key IN (SELECT unnest(?::VARCHAR[]))''', [identifiers_path, keys])
            linked_ids = {row['observation_id'] for row in linked if row.get('observation_id')}
            if expected_observations is None or len(linked_ids) <= int(expected_observations):
                observation_ids.update(linked_ids)
        if travel_ids:
            travel_observations = _rows(db, '''SELECT DISTINCT observation_id FROM read_parquet(?)
                WHERE travel_id IN (SELECT unnest(?::VARCHAR[]))''', [travels_path, travel_ids])
            observation_ids.update(row['observation_id'] for row in travel_observations if row.get('observation_id'))
        observation_ids = sorted(observation_ids)
        # Only counts are needed for the profile card.  Full flight and
        # observation payloads stay in their existing map/timeline views.
        identifiers_query = '''SELECT source_id,source_record_id,locator,identifier_id,
                observation_id,kind,programme,value
            FROM read_parquet(?)
            WHERE observation_id IN (SELECT unnest(?::VARCHAR[]))
            ORDER BY source_id,locator,identifier_id'''
        identifiers = _rows(db, identifiers_query, [identifiers_path, observation_ids]) if observation_ids else []
        documents = [row for row in identifiers if row.get('kind') == 'document']
        # Some source observations carry a passport/document but no shared
        # identifier with the selected anchor. Use the canonical name (and
        # DOB when available) as a narrow read-only fallback so those records
        # are visible without rebuilding the product snapshot. The document
        # index is built once per running web process; otherwise every click
        # would rescan hundreds of megabytes of canonical parquet.
        fallback_used = False
        display_name = person.get('display_name')
        if display_name:
            candidate_ids = self._document_observation_ids(db, display_name, birth_date)
            if candidate_ids:
                observation_ids = sorted(set(observation_ids) | candidate_ids)
                identifiers = _rows(db, identifiers_query, [identifiers_path, observation_ids])
                documents = [row for row in identifiers if row.get('kind') == 'document']
                fallback_used = True
        observations_count = db.execute('''SELECT count(*) FROM read_parquet(?)
            WHERE observation_id IN (SELECT unnest(?::VARCHAR[]))''', [observations_path, observation_ids]).fetchone()[0] if observation_ids else 0
        travels_count = db.execute('''SELECT count(*) FROM read_parquet(?)
            WHERE observation_id IN (SELECT unnest(?::VARCHAR[]))
               OR travel_id IN (SELECT unnest(?::VARCHAR[]))''', [travels_path, observation_ids, travel_ids]).fetchone()[0] if observation_ids or travel_ids else 0
        complete = expected_observations is None or len(observation_ids) >= int(expected_observations)
        return {'available': True, 'complete': complete, 'expected_observations': expected_observations,
                'canonical_root': str(root), 'fallback_used': fallback_used,
                'observations_count': observations_count, 'travels_count': travels_count,
                'identifiers': identifiers, 'documents': documents}

    def _document_observation_ids(self, db, display_name, birth_date):
        if self._document_index is None:
            with self._document_index_lock:
                if self._document_index is None:
                    root = self.canonical_root
                    observations_path = str(root / 'observations.parquet')
                    identifiers_path = str(root / 'identifiers.parquet')
                    rows = _rows(db, '''SELECT DISTINCT o.name_normalized,o.name_raw,
                            o.name_latin_comparison,o.birth_date,i.observation_id
                        FROM read_parquet(?) i
                        JOIN read_parquet(?) o USING(observation_id)
                        WHERE lower(i.kind)='document' ''',
                        [identifiers_path, observations_path])
                    by_name = {}
                    by_name_birth = {}
                    for row in rows:
                        observation_id = row.get('observation_id')
                        if not observation_id:
                            continue
                        keys = {_name_key(row.get('name_normalized')), _name_key(row.get('name_raw')),
                                _name_key(row.get('name_latin_comparison'))}
                        keys.discard('')
                        for key in keys:
                            by_name.setdefault(key, set()).add(observation_id)
                            if row.get('birth_date'):
                                by_name_birth.setdefault((key, row['birth_date']), set()).add(observation_id)
                            token_key = _name_token_key(key)
                            if token_key:
                                by_name.setdefault(token_key, set()).add(observation_id)
                                if row.get('birth_date'):
                                    by_name_birth.setdefault((token_key, row['birth_date']), set()).add(observation_id)
                    self._document_index = (by_name, by_name_birth)
        by_name, by_name_birth = self._document_index
        keys = {_name_key(display_name), _name_token_key(display_name)}
        keys.discard('')
        if birth_date:
            result = set()
            for key in keys:
                result.update(by_name_birth.get((key, birth_date), ()))
            return result
        result = set()
        for key in keys:
            result.update(by_name.get(key, ()))
        return result

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
        return dict(self.manifest, data_mode=self.data_mode, evaluation=_json_file(self.path/'evaluation.json'),
                    forest=_json_file(self.path/'forest.json'),graph=_json_file(self.path/'graph.json'),rules=_json_file(self.path/'rules.json'))

    def top(self, limit=50):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('Limit must be 1–1000')
        with self.connection() as db:
            return _rows(db,'''SELECT * FROM rankings
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
        if eligibility not in {'all', 'eligible', 'no_matches', 'insufficient_data'}:
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
        if eligibility != 'all':
            clauses.append('eligibility_status=?')
            params.append(eligibility)
        if query.strip():
            needle=f'%{query.strip().lower()}%'
            clauses.append("(lower(coalesce(display_name,'')) LIKE ? OR lower(person_id) LIKE ?)")
            params.extend([needle, needle])
        where=' WHERE '+ ' AND '.join(clauses) if clauses else ''
        with self.connection() as db:
            total=db.execute(f'SELECT count(*) FROM rankings{where}', params).fetchone()[0]
            rows=_rows(db, f'SELECT * FROM rankings{where} ORDER BY {order} LIMIT ? OFFSET ?', params+[limit,offset])
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
            segments=_rows(db,'SELECT anchor_travel_id FROM segments WHERE person_id=?',[person_id])
            return self._source_details(db, person, segments)

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
