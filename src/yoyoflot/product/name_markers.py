"""Global name comparisons with explicit, non-scoring unconfirmed candidates."""


def prepare_names(db, progress):
    progress('N01: именные записи всех профилей, без требования предварительного accepted')
    db.execute('''CREATE TABLE name_rows AS SELECT p.person_id,p.observation_id,p.source_id,
        p.source_record_id,p.locator,p.name_key,p.birth_date,o.name_raw
        FROM marker_people p JOIN observations o USING(observation_id)
        WHERE p.name_key IS NOT NULL''')
    progress('N01: исходные именные записи подготовлены')
    db.execute('CREATE TABLE unique_names AS SELECT DISTINCT name_key FROM name_rows')
    # Keep the catalogue entirely in DuckDB: per-row Python callbacks here
    # serialize a large scan and can stall a parallel, spilling query.
    from .source_markers import transliteration_sql
    translit_expression=transliteration_sql('name_key')
    db.execute('''CREATE TABLE name_catalog AS SELECT name_key,
        len(string_split(name_key,' ')) token_count,
        array_to_string(list_sort(string_split('''+translit_expression+''',' ')),' ') translit_key
        FROM unique_names''')
    progress('N01: каталог полных имён подготовлен')
    # Index distinct words instead of repeating their deletion signatures for
    # hundreds of thousands of full names. Exact remaining tokens form a block.
    db.execute('''CREATE TABLE name_parts AS WITH n AS (
        SELECT *,string_split(name_key,' ') tokens FROM name_catalog)
        SELECT name_key,translit_key,tokens[p.pos] word,array_to_string(list_concat(
            list_slice(tokens,1,p.pos-1),list_slice(tokens,p.pos+1,len(tokens))),' ') context
        FROM n,range(1,len(tokens)+1) p(pos)''')
    progress('N01: части полных имён подготовлены')
    db.execute('CREATE TABLE name_words AS SELECT DISTINCT word FROM name_parts')
    db.execute('''CREATE TABLE word_deletions AS SELECT word,word signature FROM name_words
        UNION SELECT w.word,substr(w.word,1,p.pos-1)||substr(w.word,p.pos+1)
        FROM name_words w,range(1,length(w.word)+1) p(pos)''')
    progress('N01: поиск удалений, вставок и замен одной буквы по всему каталогу')
    db.execute('''CREATE TABLE word_neighbors AS SELECT DISTINCT a.word word_a,b.word word_b
        FROM word_deletions a JOIN word_deletions b USING(signature)
        WHERE a.word<b.word AND abs(length(a.word)-length(b.word))<=1 AND levenshtein(a.word,b.word)=1''')
    progress('N01: однобуквенные пары слов найдены')
    # Materialize the indexed candidates before applying distance/transliteration
    # predicates. Otherwise the optimizer can compare the entire name catalogue
    # with itself after changes to alias selectivity.
    db.execute('''CREATE TABLE indexed_name_changes AS SELECT a.name_key,a.translit_key,a.context,w.word_b
        FROM word_neighbors w JOIN name_parts a ON a.word=w.word_a''')
    db.execute('''CREATE TABLE candidate_name_pairs AS SELECT DISTINCT a.name_key name_a,b.name_key name_b,
        a.translit_key translit_a,b.translit_key translit_b
        FROM indexed_name_changes a JOIN name_parts b ON b.word=a.word_b AND a.context=b.context
        WHERE a.name_key<b.name_key''')
    progress('N01: индексированные пары полных имён подготовлены')
    db.execute('''CREATE TABLE name_neighbors AS SELECT name_a,name_b FROM candidate_name_pairs p
        WHERE p.translit_a<>p.translit_b
          AND levenshtein(p.name_a,p.name_b)=1''')
    progress('N01: варианты транслитерации исключены')
    db.execute('DROP TABLE word_deletions')
    db.execute('DROP TABLE name_parts')
    db.execute('''CREATE TABLE profile_name_reps AS WITH g AS (
        SELECT person_id,name_key,min(observation_id) observation_id,min(birth_date) first_birth,
            max(birth_date) last_birth FROM name_rows GROUP BY 1,2)
        SELECT r.*,g.first_birth,g.last_birth FROM g JOIN name_rows r USING(person_id,name_key,observation_id)''')
    db.execute('''CREATE TABLE name_profile_pairs AS SELECT a.person_id person_a,b.person_id person_b,
        a.name_key name_a,b.name_key name_b,a.observation_id observation_a,b.observation_id observation_b,
        coalesce(a.first_birth<>a.last_birth OR b.first_birth<>b.last_birth
            OR a.first_birth<>b.first_birth,false) known_birth_conflict,
        sha256(to_json([a.person_id,b.person_id,a.name_key,b.name_key])) pair_id
        FROM name_neighbors n JOIN profile_name_reps a ON a.name_key=n.name_a
        JOIN profile_name_reps b ON b.name_key=n.name_b''')
    progress('N01: проверка общих документов/карт и противоречий даты рождения')
    db.execute('''CREATE TABLE named_identifiers AS SELECT n.*,i.kind,i.identifier_namespace,i.value_normalized,
        'identity_'||sha256(to_json([i.identifier_namespace,i.value_normalized])) fact_key
        FROM name_rows n JOIN profile_identifiers i USING(person_id,observation_id)
        WHERE i.kind IN ('document','loyalty_card') AND i.value_normalized IS NOT NULL''')
    db.execute('''CREATE TABLE name_pair_links AS SELECT DISTINCT p.pair_id,a.kind,a.identifier_namespace,
        a.value_normalized,a.fact_key,a.observation_id observation_a,b.observation_id observation_b,
        p.known_birth_conflict OR (a.birth_date IS NOT NULL AND b.birth_date IS NOT NULL
            AND a.birth_date<>b.birth_date) birth_conflict
        FROM name_profile_pairs p JOIN named_identifiers a ON a.person_id=p.person_a AND a.name_key=p.name_a
        JOIN named_identifiers b ON b.person_id=p.person_b AND b.name_key=p.name_b
            AND a.identifier_namespace=b.identifier_namespace AND a.value_normalized=b.value_normalized
        WHERE a.observation_id<>b.observation_id''')
    db.execute('''CREATE TABLE name_pair_status AS SELECT p.pair_id,
        CASE WHEN count(l.pair_id)=0 THEN 'unconfirmed'
             WHEN bool_and(l.birth_conflict) THEN 'birth_date_conflict'
             ELSE 'verified_identifier' END link_status
        FROM name_profile_pairs p LEFT JOIN name_pair_links l USING(pair_id) GROUP BY p.pair_id''')
    # Store both directions, including the exact proof observations for confirmed
    # comparisons. Similarity alone never merges profiles or receives points.
    db.execute('''CREATE TABLE name_pair_display AS SELECT p.* EXCLUDE(observation_a,observation_b),s.link_status,
        coalesce(l.observation_a,p.observation_a) observation_a,
        coalesce(l.observation_b,p.observation_b) observation_b,l.kind identifier_kind,
        l.value_normalized identifier_value,l.fact_key evidence_group_id
        FROM name_profile_pairs p JOIN name_pair_status s USING(pair_id)
        LEFT JOIN name_pair_links l ON l.pair_id=p.pair_id AND NOT l.birth_conflict
        QUALIFY row_number() OVER(PARTITION BY p.pair_id ORDER BY l.kind,l.fact_key,l.observation_a,l.observation_b)=1''')
    db.execute('''CREATE TABLE marker_related_profiles AS WITH directed AS (
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
    db.execute('''INSERT INTO marker_evidence SELECT person_id,'N01',observation_id,NULL,NULL,
        source_id,source_record_id,locator,name_raw,'name_'||sha256(name_key),false FROM name_rows''')
    db.execute('''INSERT INTO marker_evidence WITH directed AS (
        SELECT p.person_a person_id,l.observation_a observation_id,l.fact_key
        FROM name_pair_links l JOIN name_profile_pairs p USING(pair_id) WHERE NOT l.birth_conflict
        UNION SELECT p.person_b,l.observation_b,l.fact_key
        FROM name_pair_links l JOIN name_profile_pairs p USING(pair_id) WHERE NOT l.birth_conflict)
        SELECT DISTINCT d.person_id,'N01',d.observation_id,NULL,NULL,n.source_id,n.source_record_id,
            n.locator,n.name_raw,d.fact_key,true FROM directed d JOIN name_rows n USING(person_id,observation_id)''')
    db.execute('''INSERT INTO marker_diagnostics WITH own AS (
        SELECT person_id,count(DISTINCT observation_id) observations,count(DISTINCT name_key) normalized_count
        FROM name_rows GROUP BY person_id), related AS (
        SELECT person_id,count(DISTINCT other_person_id) FILTER(WHERE other_person_id<>person_id) candidates,
            count(DISTINCT pair_id) FILTER(WHERE link_status='verified_identifier') verified_pairs,
            count(DISTINCT pair_id) FILTER(WHERE link_status<>'verified_identifier') unconfirmed_pairs
        FROM marker_related_profiles GROUP BY person_id)
        SELECT p.person_id,'N01',to_json(struct_pack(
            observations:=coalesce(o.observations,0),normalized_names:=coalesce(o.normalized_count,0),
            related_profiles:=coalesce(r.candidates,0),verified_pairs:=coalesce(r.verified_pairs,0),
            unconfirmed_pairs:=coalesce(r.unconfirmed_pairs,0))),
            CASE WHEN o.person_id IS NULL THEN 'Нет полного сопоставимого имени в связанных исходных записях'
                ELSE NULL END
        FROM persons p LEFT JOIN own o USING(person_id) LEFT JOIN related r USING(person_id)''')
    stats=db.execute('''SELECT (SELECT count(*) FROM name_catalog),
        (SELECT count(*) FROM name_neighbors),(SELECT count(*) FROM name_profile_pairs),
        (SELECT count(*) FROM name_pair_status WHERE link_status='verified_identifier')''').fetchone()
    progress(f'N01: имён {stats[0]}, однобуквенных пар имён {stats[1]}, пар профилей {stats[2]}, с общим идентификатором {stats[3]}')
    return dict(zip(('normalized_names','neighbor_name_pairs','profile_pairs','verified_profile_pairs'),stats))
