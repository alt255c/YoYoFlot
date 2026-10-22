"""Startup-only reweighting of verified, already evaluated marker results."""
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import tempfile
from types import MappingProxyType

import pyarrow as pa
import yaml

from .config import UniqueLoader, TITLES as BASELINE_TITLES

VERSION = 'runtime-scoring:1.2'
BASELINE = tuple(BASELINE_TITLES)
SOURCE_MARKERS = ('D01', 'P01', 'B01')
TITLES = dict(BASELINE_TITLES, D01='Конфликт паспортного документа',
    D03='Конфликт владельца билета', T01='Несовместимые подтверждённые перемещения',
    L01='Общая карта лояльности', P01='Общий PNR', M07='Повторные совместные перелёты',
    D02='Несколько документов у связанного профиля', N01='Отличие имени на одну букву',
    B01='Повторные неявки на посадку')


@dataclass(frozen=True)
class ScoringConfig:
    path: str
    sha256: str
    weights: object


def load(path):
    path = Path(path).expanduser().resolve()
    raw = path.read_bytes()
    try:
        data = yaml.load(raw, Loader=UniqueLoader)
    except (yaml.YAMLError, ValueError, TypeError) as exc:
        raise ValueError(f'Invalid scoring configuration {path}: {exc}') from exc
    if not isinstance(data, dict) or set(data) != {'version', 'weights'} or data['version'] != 'scoring-1.0':
        raise ValueError('Expected scoring-1.0 configuration with version and weights')
    weights = data['weights']
    if not isinstance(weights, dict) or set(weights) != set(TITLES):
        raise ValueError('Explicit weights for all 12 markers are required: ' + ', '.join(TITLES))
    for cid, value in weights.items():
        if type(value) not in (int, float) or not 0 <= value <= 1000 or not math.isfinite(value):
            raise ValueError(f'Weight {cid} must be a finite number between 0 and 1000')
    return ScoringConfig(str(path), hashlib.sha256(raw).hexdigest(),
                         MappingProxyType({cid: float(weights[cid]) for cid in TITLES}))


def default_path():
    local = Path.cwd() / 'configs/scoring.yaml'
    return local if local.is_file() else Path(__file__).resolve().parents[3] / 'configs/scoring.yaml'


def _path(path):
    return str(path).replace("'", "''")


class RuntimeScores:
    """Per-process disposable cache; never modifies a source snapshot or IF."""

    def __init__(self, db, config, product_hash, assessment_hash=None):
        self.config = config
        self._temporary = tempfile.TemporaryDirectory(prefix='yoyoflot-scoring-', ignore_cleanup_errors=True)
        self.root = Path(self._temporary.name)
        try:
            self._build(db, product_hash, assessment_hash)
        except Exception:
            self.close()
            raise

    def _build(self, db, product_hash, assessment_hash):
        db.execute("SET memory_limit='512MB'")
        db.execute('SET temp_directory=?', [str(self.root / 'spill')])
        rows = [{'criterion_id': cid, 'active_weight': value, 'active_title':TITLES[cid]}
                for cid, value in self.config.weights.items()]
        db.register('runtime_weights', pa.Table.from_pylist(rows))
        source_sql = ''
        if assessment_hash:
            # Only consume evaluated rows from the verified supplement. A
            # configured weight never creates the underlying evidence.
            source_sql = '''UNION ALL SELECT person_id,criterion_id,title_ru,value,threshold,
                weight,used_observations,availability_reason,status,contribution,
                NULL::BIGINT reference_size,NULL::DOUBLE reference_value,NULL::VARCHAR threshold_method
                FROM assessment_marker_coverage WHERE criterion_id NOT IN ('M01','M02','M03')'''
        db.execute('''CREATE TEMP TABLE runtime_factors AS WITH frozen AS (
            SELECT person_id,criterion_id,title_ru,value,threshold,weight,used_observations,
                availability_reason,status,contribution,reference_size,reference_value,threshold_method
            FROM explanations WHERE criterion_id IN ('M01','M02','M03')
            ''' + source_sql + ''') SELECT f.* EXCLUDE(title_ru,weight,contribution),w.active_title title_ru,
                f.weight source_weight,f.contribution source_contribution,w.active_weight weight,
                w.active_weight>0 included_in_ranking,
                CASE WHEN f.status='matched' THEN w.active_weight *
                    CASE WHEN f.criterion_id IN ('M01','M02','M03')
                        THEN least(3.0,f.value/nullif(f.threshold,0)) ELSE 1.0 END ELSE 0.0 END contribution
            FROM frozen f JOIN runtime_weights w USING(criterion_id)''')
        invalid = db.execute('''SELECT count(*) FROM runtime_factors WHERE
            contribution IS NULL OR NOT isfinite(contribution) OR contribution<0 OR
            (status='matched' AND criterion_id IN ('M01','M02','M03')
             AND (value IS NULL OR NOT isfinite(value) OR threshold IS NULL
                  OR NOT isfinite(threshold) OR threshold<=0))''').fetchone()[0]
        if invalid:
            raise ValueError('Prepared factors contain invalid scoring inputs')
        db.execute('ALTER TABLE runtime_factors ADD COLUMN nominal_contribution DOUBLE')
        db.execute('ALTER TABLE runtime_factors ADD COLUMN scoring_group VARCHAR')
        db.execute('ALTER TABLE runtime_factors ADD COLUMN dependent_on_criterion_id VARCHAR')
        db.execute('UPDATE runtime_factors SET nominal_contribution=contribution,scoring_group=criterion_id')
        has_dependencies = db.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='assessment_marker_dependencies'").fetchone()[0]
        if has_dependencies:
            db.execute('''CREATE TEMP TABLE runtime_components AS WITH RECURSIVE edges AS (
                SELECT DISTINCT a.person_id,a.criterion_id left_id,b.criterion_id right_id
                FROM assessment_marker_dependencies a JOIN assessment_marker_dependencies b
                USING(person_id,dependency_key)
                JOIN runtime_factors x ON x.person_id=a.person_id AND x.criterion_id=a.criterion_id
                JOIN runtime_factors y ON y.person_id=b.person_id AND y.criterion_id=b.criterion_id
                WHERE x.status='matched' AND y.status='matched' AND x.included_in_ranking AND y.included_in_ranking),
                reachable(person_id,node,root) AS (
                    SELECT person_id,criterion_id,criterion_id FROM runtime_factors
                    WHERE status='matched' AND included_in_ranking AND criterion_id NOT IN ('M01','M02','M03')
                    UNION SELECT r.person_id,e.right_id,r.root FROM reachable r JOIN edges e
                    ON e.person_id=r.person_id AND e.left_id=r.node)
                SELECT person_id,node criterion_id,min(root) scoring_group FROM reachable GROUP BY person_id,node''')
            db.execute('''CREATE TEMP TABLE runtime_allocations AS SELECT c.*,
                first_value(c.criterion_id) OVER w winner,row_number() OVER w priority
                FROM runtime_components c JOIN runtime_factors f USING(person_id,criterion_id)
                WINDOW w AS(PARTITION BY c.person_id,c.scoring_group ORDER BY f.contribution DESC,c.criterion_id)''')
            db.execute('''UPDATE runtime_factors f SET scoring_group=a.scoring_group,
                contribution=CASE WHEN a.priority=1 THEN f.contribution ELSE 0.0 END,
                dependent_on_criterion_id=CASE WHEN a.priority>1 THEN a.winner END
                FROM runtime_allocations a WHERE a.person_id=f.person_id AND a.criterion_id=f.criterion_id''')
        self.prepared = [r[0] for r in db.execute('SELECT DISTINCT criterion_id FROM runtime_factors ORDER BY criterion_id').fetchall()]
        self.pending = sorted(set(TITLES) - set(self.prepared))
        db.execute('''CREATE TEMP TABLE runtime_totals AS SELECT person_id,sum(contribution) rule_score,
            count(*) FILTER(WHERE status='matched' AND included_in_ranking)::INTEGER matched_criteria_count,
            count(*) FILTER(WHERE status<>'unavailable')::INTEGER available_criteria_count,
            count(*) FILTER(WHERE contribution>0)::INTEGER matched_scoring_group_count,
            count(*) FILTER(WHERE status='matched' AND included_in_ranking
                             AND criterion_id NOT IN ('M01','M02','M03')) source_matched_count
            FROM runtime_factors GROUP BY person_id''')
        association_sql = '''LEFT JOIN assessment_profile_assessments a USING(person_id)''' if assessment_hash else ''
        association_fields = '''a.linked_observations,a.expected_observations,a.source_links_complete''' if assessment_hash else '''
            NULL::BIGINT linked_observations,NULL::BIGINT expected_observations,NULL::BOOLEAN source_links_complete'''
        db.execute('''CREATE TEMP TABLE runtime_rankings AS WITH scored AS (
            SELECT r.* EXCLUDE(rule_score,matched_criteria_count,available_criteria_count,eligibility_status,rank),
                r.rule_score snapshot_rule_score,r.rank snapshot_rank,t.* EXCLUDE(person_id),
                r.identity_status='accepted' identity_accepted,
                t.available_criteria_count evaluated_criteria_count,
                t.matched_criteria_count assessment_matched_count,
                ''' + association_fields + ''',
                CASE WHEN t.available_criteria_count=0 THEN 'insufficient_data'
                     WHEN t.rule_score>0 THEN 'eligible' ELSE 'no_matches' END eligibility_status
            FROM rankings r JOIN runtime_totals t USING(person_id) ''' + association_sql + ''')
            SELECT *,eligibility_status assessment_status,
                CASE WHEN eligibility_status='eligible' THEN row_number() OVER(
                    PARTITION BY eligibility_status ORDER BY rule_score DESC,person_id) END rank
            FROM scored''')
        for name in ('factors', 'rankings'):
            order = 'person_id,criterion_id' if name == 'factors' else 'person_id'
            db.execute(f"COPY (SELECT * FROM runtime_{name} ORDER BY {order}) TO '{_path(self.root / (name+'.parquet'))}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        result = db.execute('''SELECT count(*) profiles,
            count(*) FILTER(WHERE available_criteria_count>0) assessed,
            count(*) FILTER(WHERE eligibility_status='eligible') with_signals,
            count(*) FILTER(WHERE eligibility_status='no_matches') without_signals,
            count(*) FILTER(WHERE eligibility_status='insufficient_data') unavailable,
            count(*) FILTER(WHERE identity_accepted) identity_accepted,
            count(*) FILTER(WHERE source_links_complete) source_links_complete
            FROM runtime_rankings''')
        self.statistics = dict(zip([c[0] for c in result.description], result.fetchone()))
        result = db.execute('''SELECT criterion_id,
            count(*) FILTER(WHERE status<>'unavailable') evaluated,
            count(*) FILTER(WHERE status='matched') AS "matched",
            count(*) FILTER(WHERE contribution>0) credited,
            count(*) FILTER(WHERE dependent_on_criterion_id IS NOT NULL) dependent,
            min(person_id) FILTER(WHERE status='matched') sample_person_id,
            count(*) FILTER(WHERE status='unavailable') unavailable
            FROM runtime_factors GROUP BY criterion_id ORDER BY criterion_id''')
        marker_statistics = [dict(zip([c[0] for c in result.description],row)) for row in result.fetchall()]
        candidate_counts={}
        if db.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='assessment_marker_diagnostics'").fetchone()[0]:
            candidate_counts={r[0]:{'count':r[1],'sample':r[2]} for r in db.execute('''SELECT criterion_id,count(*),min(person_id) FROM assessment_marker_diagnostics
                WHERE coalesce(try_cast(json_extract_string(details_json,'$.unconfirmed_pairs') AS BIGINT),0)>0
                GROUP BY criterion_id''').fetchall()}
        for item in marker_statistics:
            item.update(title_ru=TITLES[item['criterion_id']],weight=self.config.weights[item['criterion_id']],
                unconfirmed_candidates=candidate_counts.get(item['criterion_id'],{}).get('count',0),
                sample_candidate_person_id=candidate_counts.get(item['criterion_id'],{}).get('sample'))
        for cid in self.pending:
            marker_statistics.append(dict(criterion_id=cid,title_ru=TITLES[cid],weight=self.config.weights[cid],
                evaluated=0,matched=0,credited=0,dependent=0,unavailable=self.statistics['profiles']))
        marker_statistics.sort(key=lambda row: row['criterion_id'])
        request = {'version': VERSION, 'config_sha256': self.config.sha256,
                   'product_manifest_sha256': product_hash, 'assessment_manifest_sha256': assessment_hash}
        self.run_id = 'scoring-' + hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:20]
        self.info = dict(request, run_id=self.run_id, config_path=self.config.path,
            weights=dict(self.config.weights), sort_order=['rule_score DESC', 'person_id ASC'],
            prepared_markers=self.prepared, pending_markers=self.pending,
            marker_count=len(TITLES), zero_weight_policy='no_points_or_ranking_signal; observed_status_retained',
            marker_statistics=marker_statistics,
            dependent_evidence_policy='maximum points per connected group of verified underlying facts',
            calculation_scope='Already prepared factor results only; pending markers are not evaluated.',
            cache='temporary_per_process; source_parquet_unchanged', forest_retrained=False)
        (self.root / 'scoring.json').write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding='utf-8')

    def attach(self, db):
        db.read_parquet(str(self.root / 'rankings.parquet')).create_view('rankings', replace=True)
        db.read_parquet(str(self.root / 'factors.parquet')).create_view('explanations', replace=True)

    def close(self):
        self._temporary.cleanup()
