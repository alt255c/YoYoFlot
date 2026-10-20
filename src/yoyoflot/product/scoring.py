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

VERSION = 'runtime-scoring:1.0'
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
        rows = [{'criterion_id': cid, 'active_weight': value} for cid, value in self.config.weights.items()]
        db.register('runtime_weights', pa.Table.from_pylist(rows))
        source_sql = ''
        if assessment_hash:
            # These are the only source-marker semantics prepared by v1.0.
            # Do not turn graph companions or name matches into new evidence.
            source_sql = '''UNION ALL SELECT person_id,criterion_id,title_ru,value,threshold,
                weight,used_observations,availability_reason,status,contribution,
                NULL::BIGINT reference_size,NULL::DOUBLE reference_value,NULL::VARCHAR threshold_method
                FROM assessment_marker_coverage WHERE criterion_id IN ('D01','P01','B01')'''
        db.execute('''CREATE TEMP TABLE runtime_factors AS WITH frozen AS (
            SELECT person_id,criterion_id,title_ru,value,threshold,weight,used_observations,
                availability_reason,status,contribution,reference_size,reference_value,threshold_method
            FROM explanations WHERE criterion_id IN ('M01','M02','M03')
            ''' + source_sql + ''') SELECT f.* EXCLUDE(weight,contribution),
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
        self.prepared = [r[0] for r in db.execute('SELECT DISTINCT criterion_id FROM runtime_factors ORDER BY criterion_id').fetchall()]
        self.pending = sorted(set(TITLES) - set(self.prepared))
        db.execute('''CREATE TEMP TABLE runtime_totals AS SELECT person_id,sum(contribution) rule_score,
            count(*) FILTER(WHERE status='matched' AND included_in_ranking)::INTEGER matched_criteria_count,
            count(*) FILTER(WHERE status<>'unavailable')::INTEGER available_criteria_count,
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
        request = {'version': VERSION, 'config_sha256': self.config.sha256,
                   'product_manifest_sha256': product_hash, 'assessment_manifest_sha256': assessment_hash}
        self.run_id = 'scoring-' + hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()[:20]
        self.info = dict(request, run_id=self.run_id, config_path=self.config.path,
            weights=dict(self.config.weights), sort_order=['rule_score DESC', 'person_id ASC'],
            prepared_markers=self.prepared, pending_markers=self.pending,
            marker_count=len(TITLES), zero_weight_policy='no_points_or_ranking_signal; observed_status_retained',
            calculation_scope='Already prepared factor results only; pending markers are not evaluated.',
            cache='temporary_per_process; source_parquet_unchanged', forest_retrained=False)
        (self.root / 'scoring.json').write_text(json.dumps(self.info, ensure_ascii=False, indent=2), encoding='utf-8')

    def attach(self, db):
        db.read_parquet(str(self.root / 'rankings.parquet')).create_view('rankings', replace=True)
        db.read_parquet(str(self.root / 'factors.parquet')).create_view('explanations', replace=True)

    def close(self):
        self._temporary.cleanup()
