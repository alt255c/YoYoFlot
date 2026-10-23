"""Regression cases for missing-factor eligibility and fixed-cohort ablation."""

import duckdb
import pyarrow.parquet as pq

from tests.test_source_product import config, tables
from yoyoflot.product.build import _evaluation, _features
from yoyoflot.product.config import load


def test_unknown_date_disables_sequence_not_other_supported_factors(tmp_path):
    source = tables(tmp_path)
    settings = load(config(tmp_path))
    with duckdb.connect() as db:
        db.read_parquet(str(source / 'persons.parquet')).create_view('persons')
        db.register('segment_input', pq.read_table(source / 'segments.parquet'))
        db.execute('CREATE TABLE segments AS SELECT * FROM segment_input')
        db.execute("UPDATE segments SET departure_date=NULL WHERE segment_id='person_000_3'")
        _features(db, settings, {'days': 8})
        actual = db.execute("""SELECT eligibility_status, available_criteria_count,
            matched_criteria_count, rank FROM rankings WHERE person_id='person_000'""").fetchone()
        assert actual == ('eligible', 2, 2, 3)
        assert db.execute("""SELECT status, contribution FROM explanations
            WHERE person_id='person_000' AND criterion_id='M02'""").fetchone() == ('unavailable', 0)
        # Complete profiles with three supported matches stay above this one.
        assert db.execute('SELECT person_id FROM top50 ORDER BY rank').fetchall() == [
            ('person_001',), ('person_002',), ('person_000',),
        ]


def test_ablation_cannot_admit_profiles_outside_original_population():
    with duckdb.connect() as db:
        db.execute("""CREATE TABLE rankings AS SELECT * FROM (VALUES
            ('included','eligible'), ('excluded','insufficient_data'))
            t(person_id, eligibility_status)""")
        db.execute("CREATE TABLE top50 AS SELECT 'included' person_id")
        db.execute("""CREATE TABLE explanations (
            person_id VARCHAR, criterion_id VARCHAR, status VARCHAR,
            value DOUBLE, threshold DOUBLE, weight DOUBLE)""")
        db.executemany('INSERT INTO explanations VALUES (?,?,?,?,?,?)', [
            ('included', 'M01', 'matched', 2, 1, 1),
            ('included', 'M02', 'matched', 2, 1, 1),
            ('included', 'M03', 'matched', 2, 1, 1),
            ('excluded', 'M01', 'matched', 3, 1, 1),
            ('excluded', 'M02', 'unavailable', None, 1, 1),
            ('excluded', 'M03', 'matched', 3, 1, 1),
        ])
        result = _evaluation(db, {})
        assert result['population_fixed'] is True
        assert result['population_size'] == 1
        for experiment in result['experiments']:
            assert experiment['top_count'] == 1
            assert experiment['jaccard_to_primary'] == 1
