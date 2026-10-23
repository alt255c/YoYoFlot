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
from .scoring import BASELINE, TITLES as SCORING_TITLES, RuntimeScores
from .mascots import load_name_hints,choose_mascot


def _json_file(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class ProductStore:
    def __init__(self, path, allow_demo=False, scoring=None):
        self.scoring = scoring
        self.runtime_scores = None
        self.path = Path(path).resolve()
        self.manifest = _json_file(self.path/'run_manifest.json')
        m = self.manifest
        self.data_mode = m.get('data_mode')
        valid_mode = self.data_mode == 'source_dataset' or (allow_demo and self.data_mode == 'demo')
        if (m.get('schema_version') not in {VERSION, '1.1'} or m.get('status') != 'complete' or not valid_mode
                or m.get('run_id') != 'product-'+_digest(m.get('request'))[:20]):
            raise ValueError('Not a completed supported product')
        files=m.get('files',{})
        required={'tables/top50.parquet','tables/rankings.parquet','tables/explanations.parquet',
                  'tables/segments.parquet','tables/persons.parquet','tables/features.parquet',
                  'airports.csv','reference_manifest.json','product.yaml','evaluation.json','graph.json','rules.json','review.csv'}
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
        self.profile_observations = self._find_profile_observations()
        with duckdb.connect() as mascot_db:
            mascot_db.execute("SET memory_limit='256MB'")
            mascot_db.execute('SET threads=1')
            self.mascot_name_hints=load_name_hints(mascot_db,self.profile_observations)
        self.has_marker_diagnostics=bool(self.assessment_manifest and 'marker_diagnostics.parquet' in self.assessment_manifest['files'])
        self.airports={}
        with (self.path/'airports.csv').open(encoding='utf-8',newline='') as stream:
            for row in csv.DictReader(stream):
                if row['airport_id'] in self.airports:raise ValueError('Duplicate airport reference key')
                for field in ['latitude','longitude']:
                    row[field]=float(row[field]) if row[field] else None
                    if row[field] is not None and (not math.isfinite(row[field]) or abs(row[field])>(90 if field=='latitude' else 180)):
                        raise ValueError('Invalid airport coordinate')
                self.airports[row['airport_id']]=row
        if 'scoring.json' in self.manifest['files']:
            from .scoring import PublishedScores, load
            self.scoring = load(self.path / 'scoring.yaml')
            if scoring is not None and scoring.sha256 != self.scoring.sha256:
                raise ValueError('Scoring weights changed. Run build_and_run.sh to publish a matching product.')
            self.runtime_scores = PublishedScores(self.path, self.scoring)
        elif scoring is not None:
            raise ValueError('This is an intermediate product. Run build_and_run.sh to prepare the final site and exports.')

    def close(self):
        if self.runtime_scores is not None:
            self.runtime_scores.close()

    def _find_assessment_root(self):
        configured = os.environ.get('YOYO_ASSESSMENT_ROOT')
        product_hash = self.manifest.get('base_product_manifest_sha256') or fingerprint(self.path / 'run_manifest.json')
        if 'assessment/assessment_manifest.json' in self.manifest['files']:
            configured = str(self.path / 'assessment')
        candidates = [Path(configured).expanduser().resolve()] if configured else [
            self.path.parent / 'source-assessment-v4',self.path.parent / 'source-assessment-v3',self.path.parent / 'source-assessment-v2',self.path.parent / 'source-assessment']
        for root in candidates:
            if not (root / 'assessment_manifest.json').is_file():
                if configured:
                    raise ValueError('Configured source assessment is missing')
                continue
            if not configured and _json_file(root / 'assessment_manifest.json').get('request', {}).get('product_manifest_sha256') != product_hash:
                continue
            self.assessment_manifest, self.assessment_report = verify_assessment(root, product_hash)
            return root
        return None

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
                'link_policy': 'complete_published_links_and_verified_source_versions',
                'observations_count': info['linked_observations'], 'travels_count': travels,
                'identifiers': identifiers, 'documents': [r for r in identifiers if r['kind']=='document']}

    def _find_profile_observations(self):
        """Use only the exact canonical input recorded by the verified supplement."""
        if self.assessment_manifest is None:
            return None
        configured=os.environ.get('YOYO_PROFILE_OBSERVATIONS')
        if 'source_observations.parquet' in self.manifest['files']:
            configured=str(self.path/'source_observations.parquet')
        inputs=self.assessment_manifest.get('request',{}).get('inputs',{})
        observations=[(path,checksum) for path,checksum in inputs.items()
                      if Path(path.replace('\\','/')).name=='observations.parquet']
        if len(observations)!=1:
            if configured:raise ValueError('Profile observations require a verified assessment input')
            return None
        original,expected=observations[0]
        path=Path(configured or original).expanduser().resolve()
        if not path.is_file():
            if configured:raise ValueError('Configured profile observations are missing')
            return None
        if path.is_symlink() or fingerprint(path)!=expected:
            raise ValueError('Profile observations checksum mismatch')
        return path

    def _profile_sex(self, db, person_id):
        if self.profile_observations is None:
            return {'sex':None,'sex_status':'unavailable'}
        rows=_rows(db,'''SELECT o.sex,count(*) records
            FROM read_parquet(?) o JOIN assessment_source_links l USING(observation_id)
            WHERE l.person_id=? AND o.sex IN ('Female','Male') GROUP BY o.sex ORDER BY o.sex''',
            [str(self.profile_observations),person_id])
        return {'sex':rows[0]['sex'] if len(rows)==1 else None,
                'sex_status':'recorded' if len(rows)==1 else 'conflict' if rows else 'unknown'}

    def assessment_statistics(self, ids=None):
        if self.runtime_scores is not None and ids is None:
            return dict(self.runtime_scores.statistics, source_assessment_available=self.assessment_root is not None,
                        ranked_with_signals=self.runtime_scores.statistics['with_signals'])
        if self.runtime_scores is None and self.assessment_root is not None and ids is None:
            return dict(self.assessment_report['statistics'], source_assessment_available=True,
                        ranked_with_signals=sum(row['profiles'] for row in self.manifest.get('eligibility', [])
                                                if row['eligibility_status']=='eligible'))
        with self.connection() as db:
            params = [ids] if ids is not None else []
            where = ' WHERE person_id IN (SELECT unnest(?::VARCHAR[]))' if ids is not None else ''
            if self.runtime_scores is not None:
                data = _rows(db, """SELECT count(*) profiles,
                    count(*) FILTER(WHERE available_criteria_count>0) assessed,
                    count(*) FILTER(WHERE eligibility_status='eligible') with_signals,
                    count(*) FILTER(WHERE eligibility_status='no_matches') without_signals,
                    count(*) FILTER(WHERE eligibility_status='insufficient_data') unavailable,
                    count(*) FILTER(WHERE identity_accepted) identity_accepted,
                    count(*) FILTER(WHERE source_links_complete) source_links_complete
                    FROM rankings""" + where, params)[0]
            elif self.assessment_root is not None:
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
            if self.assessment_root is not None:
                for name in ASSESSMENT_TABLES:
                    if name+'.parquet' in self.assessment_manifest['files']:
                        db.read_parquet(str(self.assessment_root / (name + '.parquet'))).create_view('assessment_' + name)
            if self.runtime_scores is not None:
                self.runtime_scores.attach(db)
            yield db

    def metadata(self):
        metadata = dict(self.manifest, data_mode=self.data_mode, evaluation=_json_file(self.path/'evaluation.json'),
                    graph=_json_file(self.path/'graph.json'),rules=_json_file(self.path/'rules.json'),
                    assessment=self.assessment_statistics(),
                    source_assessment=(dict(run_id=self.assessment_manifest['run_id'],
                        version=self.assessment_manifest['version'],path=str(self.assessment_root),
                        prepared_source_markers=self.assessment_report['prepared_source_markers'],
                        pending_source_markers=self.assessment_report['pending_source_markers'],
                        unavailable_reasons=self.assessment_report.get('field_audit',{}).get('unavailable_reasons',{})) if self.assessment_manifest else None))
        if self.runtime_scores is not None:
            statistics = self.runtime_scores.statistics
            metadata.update(scoring=self.runtime_scores.info, sort_order=self.runtime_scores.info['sort_order'],
                eligibility=[{'eligibility_status': status, 'profiles': statistics[key]} for status,key in (
                    ('eligible','with_signals'),('no_matches','without_signals'),('insufficient_data','unavailable'))])
            metadata['snapshot'] = {key: self.manifest.get(key) for key in ('run_id','sort_order','counts','eligibility')}
            metadata['counts'] = dict(self.manifest['counts'], top50=min(statistics['with_signals'],50))
            metadata['rules'] = [dict(row, weight=self.scoring.weights[row['criterion_id']]) for row in metadata['rules']]
            metadata['evaluation_scope'] = 'Published scoring snapshot; no supervised accuracy claims.'
        return metadata

    def _ranking_relation(self):
        return ('rankings LEFT JOIN assessment_profile_assessments USING(person_id)'
                if self.assessment_root and self.runtime_scores is None else 'rankings')

    def marker_statistics(self, ids):
        if self.runtime_scores is None:
            return []
        with self.connection() as db:
            rows=_rows(db, '''SELECT criterion_id,
                count(*) FILTER(WHERE status<>'unavailable') evaluated,
                count(*) FILTER(WHERE status='matched') AS "matched",
                count(*) FILTER(WHERE contribution>0) credited,
                count(*) FILTER(WHERE dependent_on_criterion_id IS NOT NULL) dependent,
                min(person_id) FILTER(WHERE status='matched') sample_person_id,
                count(*) FILTER(WHERE status='unavailable') unavailable
                FROM explanations WHERE person_id IN (SELECT unnest(?::VARCHAR[]))
                GROUP BY criterion_id''',[ids])
            candidates={r['criterion_id']:r for r in _rows(db,'''SELECT criterion_id,count(*) unconfirmed_candidates,
                min(person_id) sample_candidate_person_id FROM assessment_marker_diagnostics
                WHERE person_id IN (SELECT unnest(?::VARCHAR[]))
                  AND coalesce(try_cast(json_extract_string(details_json,'$.unconfirmed_pairs') AS BIGINT),0)>0
                GROUP BY criterion_id''',[ids])} if self.has_marker_diagnostics else {}
        by_id={row['criterion_id']:row for row in rows}
        return [dict(criterion_id=cid,title_ru=SCORING_TITLES[cid],weight=self.scoring.weights[cid],
                     sample_person_id=by_id.get(cid,{}).get('sample_person_id'),
                     unconfirmed_candidates=candidates.get(cid,{}).get('unconfirmed_candidates',0),
                     sample_candidate_person_id=candidates.get(cid,{}).get('sample_candidate_person_id'),
                     **{key:by_id.get(cid,{}).get(key,len(ids) if key=='unavailable' else 0)
                        for key in ('evaluated','matched','credited','dependent','unavailable')})
                for cid in sorted(self.scoring.weights)]

    def top(self, limit=50):
        if type(limit) is not int or not 1<=limit<=1000:raise ValueError('Limit must be 1–1000')
        with self.connection() as db:
            relation = self._ranking_relation()
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
        if self.runtime_scores is not None and sort in {'rank','score'}:
            order = 'rule_score DESC, person_id'
        clauses=[]
        params=[]
        if eligibility in {'assessed', 'not_assessed'}:
            field = 'evaluated_criteria_count' if self.assessment_root or self.runtime_scores is not None else 'available_criteria_count'
            clauses.append(field + ('>0' if eligibility=='assessed' else '=0'))
        elif eligibility in {'identity_accepted', 'identity_ambiguous'}:
            clauses.append('identity_status=?')
            params.append('accepted' if eligibility=='identity_accepted' else 'ambiguous')
        elif eligibility != 'all':
            field = 'assessment_status' if self.assessment_root or self.runtime_scores is not None else 'eligibility_status'
            clauses.append(field+'=?')
            params.append(eligibility)
        if query.strip():
            needle=f'%{query.strip().lower()}%'
            clauses.append("(lower(coalesce(display_name,'')) LIKE ? OR lower(person_id) LIKE ?)")
            params.extend([needle, needle])
        where=' WHERE '+ ' AND '.join(clauses) if clauses else ''
        with self.connection() as db:
            relation = self._ranking_relation()
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

    def _marker_relations(self, db, person_id, criterion_id='N01', offset=0, limit=10):
        if not self.has_marker_diagnostics:
            return {'items':[],'pagination':{'total':0,'offset':0,'limit':limit,'has_next':False}}
        condition="person_id=? AND criterion_id=? AND (other_person_id<>person_id OR name_key<other_name_key)"
        params=[person_id,criterion_id]
        total=db.execute('SELECT count(*) FROM assessment_marker_related_profiles WHERE '+condition,params).fetchone()[0]
        items=_rows(db,'''SELECT * FROM assessment_marker_related_profiles WHERE '''+condition+'''
            ORDER BY CASE link_status WHEN 'verified_identifier' THEN 0 WHEN 'unconfirmed' THEN 1 ELSE 2 END,
                other_name_key,other_person_id,pair_id LIMIT ? OFFSET ?''',params+[limit,offset])
        for item in items:
            item['adds_signal']=item['link_status']=='verified_identifier'
            item['reason_ru']={
                'verified_identifier':'Есть общий документ или карта одной программы; известного противоречия даты рождения нет.',
                'birth_date_conflict':'Есть общий идентификатор, но даты рождения противоречат друг другу. Связь личности не подтверждена.',
                'unconfirmed':'Однобуквенное отличие найдено. Общий документ или карта не установлены; это может быть другой человек. Баллы не начисляются.'}[item['link_status']]
        return {'items':items,'pagination':{'total':total,'offset':offset,'limit':limit,'has_next':offset+len(items)<total}}

    def related_profiles(self, person_id, criterion_id='N01', offset=0, limit=10):
        if not isinstance(person_id,str) or not person_id or len(person_id)>256:
            raise ValueError('Invalid person ID')
        if criterion_id!='N01' or type(offset) is not int or offset<0 or type(limit) is not int or not 1<=limit<=50:
            raise ValueError('Invalid marker relation query')
        with self.connection() as db:
            if not db.execute('SELECT 1 FROM rankings WHERE person_id=?',[person_id]).fetchone():
                raise KeyError('Profile not found')
            return self._marker_relations(db,person_id,criterion_id,offset,limit)

    def _annotate_marker(self, db, person, factor, diagnostics):
        cid=factor['criterion_id']
        diagnostic=diagnostics.get(cid)
        if diagnostic is None:
            return
        detail=json.loads(diagnostic['details_json'])
        factor['diagnostics']=detail
        suffix=f" Баллы при срабатывании: {factor['weight']:.4g}; прибавлено: {factor['contribution']:.4g}."
        if factor.get('dependent_on_criterion_id'):
            suffix+=f" Это основание уже учтено в {factor['dependent_on_criterion_id']}; повторные баллы исключены."
        if cid=='N01':
            factor['related_profiles']=self._marker_relations(db,person['person_id'])
            factor['comparison_names']=_rows(db,'''SELECT DISTINCT observation_id,source_id,locator,value name_raw
                FROM assessment_marker_evidence WHERE person_id=? AND criterion_id='N01'
                ORDER BY source_id,locator LIMIT 25''',[person['person_id']])
            factor['has_unconfirmed_candidates']=detail['unconfirmed_pairs']>0
            if factor['status']=='unavailable':
                factor['explanation_ru']=diagnostic['availability_reason']
            else:
                factor['explanation_ru']=(f"Поиск по всем профилям выполнен. Именных исходных записей: {detail['observations']}; "
                    f"нормализованных написаний: {detail['normalized_names']}. "
                    f"Других профилей с однобуквенным отличием: {detail['related_profiles']}. "
                    f"Пар с подтверждающим идентификатором: {detail['verified_pairs']}. "
                    + ('Есть похожие имена без подтверждённой связи. Они показаны ниже и сами по себе не добавляют баллы. '
                       if detail['unconfirmed_pairs'] else '')
                    + 'Перестановка имени и фамилии, инициалы и известные варианты транслитерации не считаются ошибкой в букве. '
                    + suffix)
        elif cid=='M07':
            counts=f"Пригодных посадочных событий: {detail['events']}, разных дат: {detail['observed_dates']}, направлений: {detail['routes']}. "
            if factor['status']=='unavailable':
                factor['explanation_ru']=counts+f"Для проверки нужны минимум {detail['required_dates']} даты и {detail['required_routes']} направления."
            else:
                factor['explanation_ru']=counts+('Повторный попутчик найден.' if factor['status']=='matched' else 'Повторный попутчик по условиям маркера не найден.')+suffix
        elif cid=='D02':
            counts=f"Документных исходных записей: {detail['observations']}; разных номеров документов: {detail['documents']}. "
            factor['explanation_ru']=counts+(diagnostic['availability_reason'] if factor['status']=='unavailable'
                else ('Несколько документов у связанного профиля найдены.' if factor['status']=='matched' else 'Несколько разных документов не найдены.')+suffix)
        elif cid=='L01':
            factor['explanation_ru']=(f"Именных исходных записей: {detail['observations']}; карт с известной программой: {detail['identifiers']}. "
                + ('Недостаточно пригодных сравнений владельцев этой карты.' if factor['status']=='unavailable' else
                   'Найдена общая карта у различающихся владельцев.' if factor['status']=='matched' else 'Различающиеся владельцы общей карты не найдены.')+suffix)
        if factor['weight']==0:
            factor['explanation_ru']+=' Фактор отключён весом 0.'

    def profile(self, person_id):
        if not isinstance(person_id,str) or not person_id or len(person_id)>256:raise ValueError('Invalid person ID')
        with self.connection() as db:
            people=_rows(db,'SELECT * FROM rankings WHERE person_id=?',[person_id])
            if not people:raise KeyError('Profile not found')
            person=people[0]
            person.update(self._profile_sex(db,person_id))
            person['map_mascot']=choose_mascot(person,self.mascot_name_hints)
            diagnostics={r['criterion_id']:r for r in _rows(db,
                'SELECT * FROM assessment_marker_diagnostics WHERE person_id=?',[person_id])} if self.has_marker_diagnostics else {}
            additional_factors=[]
            if self.assessment_root or self.runtime_scores is not None:
                if self.runtime_scores is None:
                    person.update(_rows(db, 'SELECT * FROM assessment_profile_assessments WHERE person_id=?', [person_id])[0])
                table = 'explanations' if self.runtime_scores is not None else 'assessment_marker_coverage'
                additional_factors=_rows(db, f'''SELECT * FROM {table}
                    WHERE person_id=? AND criterion_id NOT IN ('M01','M02','M03') ORDER BY criterion_id''',[person_id])
                for factor in additional_factors:
                    if factor['status']=='unavailable':
                        factor['explanation_ru']=factor['availability_reason']
                    elif self.runtime_scores is not None:
                        condition = {
                            'L01':'Общая карта одной программы у различающихся владельцев. ',
                            'M07':'Одна и та же пара встречается минимум в три даты и на двух направлениях. ',
                            'D02':'Разные документы у проверенно связанного профиля. ',
                            'N01':'Имена отличаются одной буквой при независимой связи по идентификатору; известные варианты транслитерации исключены. ',
                            'P01':'Разные пассажирские записи в одной брони и одном событии рейса. ',
                        }.get(factor['criterion_id'],'')
                        factor['explanation_ru']=(condition+f"Уникальных исходных наблюдений: {factor['used_observations']}. "
                            f"Результат: {'есть совпадение' if factor['status']=='matched' else 'совпадений не найдено'}. "
                            f"Баллы при срабатывании: {factor['weight']:.4g}; прибавлено: {factor['contribution']:.4g}."
                            + (f" Это же основание уже учтено в {factor['dependent_on_criterion_id']}; повторные баллы исключены." if factor.get('dependent_on_criterion_id') else '')
                            + (' Фактор отключён весом 0.' if not factor['included_in_ranking'] else ''))
                    else:
                        factor['explanation_ru']=(f"Проверены связанные исходные записи: {factor['used_observations']}. "
                            f"Результат: {'есть совпадение' if factor['status']=='matched' else 'совпадений не найдено'}. "
                            f"Вес: {factor['weight']}; вклад после подключения нового рейтинга: {factor['preview_contribution']}. "
                            "Текущий основной балл не изменён.")
                    factor['evidence_segment_ids']=[r['segment_id'] for r in _rows(db, '''SELECT DISTINCT segment_id
                        FROM assessment_marker_evidence WHERE person_id=? AND criterion_id=? AND segment_id IS NOT NULL AND is_matched
                        ORDER BY segment_id''',[person_id,factor['criterion_id']])] if self.assessment_root else []
                    factor['source_evidence']=_rows(db, '''SELECT * FROM assessment_marker_evidence
                        WHERE person_id=? AND criterion_id=? AND is_matched
                        ORDER BY source_id,locator,observation_id,evidence_group_id''',
                        [person_id,factor['criterion_id']]) if self.assessment_root else []
                    self._annotate_marker(db,person,factor,diagnostics)
            if self.runtime_scores is not None:
                for cid in self.runtime_scores.pending:
                    if cid in BASELINE:
                        continue
                    additional_factors.append(dict(criterion_id=cid,title_ru=SCORING_TITLES[cid],
                        status='unavailable',weight=self.scoring.weights[cid],value=None,threshold=1.0,
                        contribution=0.0,used_observations=0,included_in_ranking=self.scoring.weights[cid]>0,
                        availability_reason='Расчёт и проверенные свидетельства этого маркера ещё не подготовлены',
                        explanation_ru='Расчёт и проверенные свидетельства этого маркера ещё не подготовлены. Баллы не начислены.',
                        evidence_segment_ids=[]))
                additional_factors.sort(key=lambda factor: factor['criterion_id'])
                person['configured_criteria_count'] = len(self.scoring.weights)
            factors=_rows(db,"SELECT * FROM explanations WHERE person_id=? AND criterion_id IN ('M01','M02','M03') ORDER BY contribution DESC,criterion_id",[person_id])
            for factor in factors:
                if factor['status']=='unavailable':
                    factor['explanation_ru']=factor['title_ru']+': '+factor['availability_reason']+'.'
                    factor['value']=None
                else:
                    unit='перелётов / 30 дней' if factor['criterion_id']=='M01' else 'доля'
                    factor['explanation_ru']=(f"{factor['title_ru']}: {factor['value']:.4g} ({unit}), порог ≥ {factor['threshold']:.4g}. "
                        f"Наблюдений: {factor['used_observations']}. Базовые баллы: {factor['weight']:.4g}; прибавлено: {factor['contribution']:.4g}. "
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
        return {'run_id':self.manifest['run_id'],
                'scoring_run_id':self.runtime_scores.run_id if self.runtime_scores else None,
                'person':person,'factors':factors,'segments':segments,
                'additional_factors':additional_factors,
                'route_balance':{'total':len(segments),'plottable':sum(s['is_plottable'] for s in segments),
                                 'unplottable':sum(not s['is_plottable'] for s in segments)},
                'source_evidence':evidence,'companions':companions}
