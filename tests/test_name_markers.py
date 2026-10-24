"""Check N01 query equivalence; these records never enter the application."""
import duckdb
import pytest

from yoyoflot.product.name_markers import _related_profiles


@pytest.mark.parametrize('empty',[False,True])
def test_related_profiles_matches_previous_query_including_both_directions(tmp_path,empty):
    with duckdb.connect(str(tmp_path/'names.duckdb')) as db:
        db.execute("SET memory_limit='64MB'")
        db.execute('SET threads=1')
        db.execute('''CREATE TABLE name_rows AS SELECT * FROM (VALUES
            ('a','a1','James Smith','csv','row:1'),
            ('a','a2','James Smyth','xml','row:2'),
            ('b','b1','James Smyth','yaml','row:3'),
            ('c','c1','Jame Smith','csv','row:4'))
            t(person_id,observation_id,name_raw,source_id,locator)''')
        db.execute('''CREATE TABLE name_pair_display AS SELECT * FROM (VALUES
            ('a','b','james smith','james smyth','a1','b1','pair1',
             'verified_identifier','document','doc1','identity_doc1'),
            ('a','a','james smith','james smyth','a1','a2','pair2',
             'birth_date_conflict',NULL,NULL,NULL),
            ('a','c','james smith','jame smith','a1','c1','pair3',
             'unconfirmed',NULL,NULL,NULL))
            t(person_a,person_b,name_a,name_b,observation_a,observation_b,pair_id,
              link_status,identifier_kind,identifier_value,evidence_group_id)''')
        if empty:
            db.execute('DELETE FROM name_pair_display')
        db.execute('''CREATE TABLE previous_result AS WITH directed AS (
            SELECT person_a person_id,person_b other_person_id,name_a name_key,name_b other_name_key,
                observation_a observation_id,observation_b other_observation_id,pair_id,link_status,
                identifier_kind,identifier_value,evidence_group_id FROM name_pair_display
            UNION ALL
            SELECT person_b,person_a,name_b,name_a,observation_b,observation_a,pair_id,link_status,
                identifier_kind,identifier_value,evidence_group_id FROM name_pair_display)
            SELECT DISTINCT d.*,'N01' criterion_id,a.name_raw,b.name_raw other_name_raw,
                a.source_id,b.source_id other_source_id,a.locator,b.locator other_locator
            FROM directed d JOIN name_rows a ON a.person_id=d.person_id AND a.observation_id=d.observation_id
            JOIN name_rows b ON b.person_id=d.other_person_id AND b.observation_id=d.other_observation_id''')
        _related_profiles(db)
        expected=db.execute('SELECT * FROM previous_result ORDER BY pair_id,name_key').fetchall()
        expected_columns=[c[0] for c in db.description]
        actual=db.execute('SELECT * FROM marker_related_profiles ORDER BY pair_id,name_key').fetchall()
        assert [c[0] for c in db.description]==expected_columns
        assert actual==expected
        assert len(actual)==(0 if empty else 6)
        assert db.execute("SELECT count(*) FROM information_schema.tables WHERE table_name='name_pair_details'").fetchone()[0]==0
