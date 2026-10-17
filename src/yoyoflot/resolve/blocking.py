"""Disk-backed exact blocks. Large blocks use O(n) witness edges, not n²."""
from .common import aggregate_ranges, arrow_query


def build_blocks(db, observations, identifiers, rules, staging, progress):
    db.read_parquet(str(observations)).create_view('canonical_observations')
    db.read_parquet(str(identifiers)).create_view('canonical_identifiers')
    progress.phase('упорядочивание наблюдений людей')
    db.execute('''CREATE TABLE obs AS SELECT row_number() OVER (ORDER BY observation_id)-1 nid,
        observation_id,source_id,source_record_id,locator,name_raw,name_comparison,
        name_latin_comparison,birth_date,normalization_status FROM canonical_observations''')
    count = db.execute('SELECT COUNT(*) FROM obs').fetchone()[0]
    if count >= 2**32:
        raise ValueError('T13 currently supports fewer than 2^32 observations per run')
    progress.phase('привязка идентификаторов к наблюдениям')
    db.execute('''CREATE TABLE ids_unordered AS SELECT o.nid,i.identifier_id,i.source_id,i.source_record_id,i.locator,
        i.kind,i.value_normalized,i.identifier_namespace AS namespace,i.identifier_match_key AS match_key,
        i.namespace_status,i.normalization_status,sha256(i.identifier_match_key) key_hash
        FROM canonical_identifiers i JOIN obs o USING(observation_id)''')
    progress.phase('упорядочивание идентификаторов для порционной обработки')
    db.execute('CREATE TABLE ids AS SELECT * FROM ids_unordered ORDER BY nid')
    db.execute('DROP TABLE ids_unordered')
    progress.phase('сборка документов по порциям', count)
    aggregate_ranges(db, 'document_sets', '''SELECT nid,
        to_json(list_sort(list(DISTINCT struct_pack(namespace := namespace, value := match_key)))) documents_json
        FROM ids WHERE kind='document' AND match_key IS NOT NULL AND normalization_status='ok'
        AND nid>=? AND nid<? GROUP BY nid''', count, progress=progress)
    progress.phase('сборка происхождения идентификаторов по порциям', count)
    aggregate_ranges(db, 'identifier_evidence', '''SELECT nid,
        to_json(list_sort(list(DISTINCT struct_pack(identifier_id := identifier_id, kind := kind,
        namespace := namespace, key_hash := key_hash, source_record_id := source_record_id, locator := locator)))) identifiers_json
        FROM ids WHERE match_key IS NOT NULL AND nid>=? AND nid<? GROUP BY nid''', count, progress=progress)
    progress.phase('сборка свидетельств для сопоставления')
    db.execute('''CREATE TABLE nodes AS SELECT o.*,coalesce(d.documents_json,'[]') documents_json,
        coalesce(i.identifiers_json,'[]') identifiers_json FROM obs o
        LEFT JOIN document_sets d USING(nid) LEFT JOIN identifier_evidence i USING(nid)''')
    progress.phase('построение точных блоков кандидатов')
    db.execute('''CREATE TABLE block_members AS
        SELECT DISTINCT CASE WHEN kind='loyalty_card' THEN 'card'
          WHEN namespace_status='confirmed_override' THEN 'document' ELSE 'source_document' END kind,
          key_hash,nid,namespace
        FROM ids WHERE match_key IS NOT NULL AND normalization_status='ok' AND
          ((kind='loyalty_card' AND namespace_status='programme_scoped') OR
           (kind='document' AND namespace_status IN ('source_scoped','confirmed_override')))
        UNION
        SELECT DISTINCT 'name_dob',sha256(to_json([birth_date,left(token,3)])),nid,'dob_name_token'
        FROM obs,unnest(string_split(name_latin_comparison,' ')) words(token)
        WHERE birth_date IS NOT NULL AND length(token)>=3
          AND array_length(string_split(name_latin_comparison,' '))>=2''')
    db.execute('''CREATE TABLE blocks AS SELECT m.kind,m.key_hash,min(m.namespace) namespace,
        count(*) member_count,min(m.nid) anchor,count(DISTINCT o.birth_date)>1 contaminated
        FROM block_members m JOIN obs o USING(nid) GROUP BY m.kind,m.key_hash HAVING count(*)>1''')
    name_limit, small = rules['max_name_block_size'], rules['pairwise_block_size']
    arrow_query(db, '''SELECT 'block_'||sha256(kind||':'||key_hash) block_id,kind,
        CASE WHEN contaminated THEN 'conflicting_birth_dates'
             WHEN kind='name_dob' AND member_count>? THEN 'name_block_limit' ELSE 'star_block' END reason,
        key_hash,member_count FROM blocks WHERE contaminated OR member_count>? OR (kind='name_dob' AND member_count>?)
        ORDER BY kind,key_hash''', staging/'blocking_issues.parquet', 'blocking_issues',
        params=[name_limit,small,name_limit])
    db.execute('''CREATE TABLE poisoned_nodes AS SELECT DISTINCT m.nid FROM block_members m
        JOIN blocks b USING(kind,key_hash) WHERE b.contaminated
        UNION SELECT nid FROM obs WHERE normalization_status<>'ok'
        UNION SELECT nid FROM ids WHERE normalization_status<>'ok' ''')
    upper = db.execute('''SELECT coalesce(sum(CASE WHEN member_count<=? THEN member_count*(member_count-1)//2
        ELSE member_count-1 END + CASE WHEN contaminated THEN 1 ELSE 0 END),0) FROM blocks''', [small]).fetchone()[0]
    if upper > rules['max_candidates']:
        raise ValueError(f'Candidate upper bound {upper} exceeds max_candidates={rules["max_candidates"]}; '
                         'no comparisons silently discarded; review blocking limits')
    db.execute('''CREATE TABLE small_members AS SELECT m.* FROM block_members m JOIN blocks g USING(kind,key_hash)
        WHERE g.member_count<=?''',[small])
    db.execute('''CREATE TABLE raw_pairs AS
        SELECT a.nid left_n,b.nid right_n,a.kind,a.key_hash FROM small_members a
        JOIN small_members b ON a.kind=b.kind AND a.key_hash=b.key_hash AND a.nid<b.nid
        UNION ALL
        SELECT g.anchor,a.nid,g.kind,g.key_hash FROM blocks g JOIN block_members a USING(kind,key_hash)
        WHERE g.member_count>? AND a.nid<>g.anchor''', [small])
    # If the anchor has no DOB, still expose an explicit contradictory witness pair.
    db.execute('''INSERT INTO raw_pairs SELECT min(nid),min(nid) FILTER(WHERE witness_position=2),kind,key_hash FROM
        (SELECT *,row_number() OVER(PARTITION BY kind,key_hash ORDER BY nid) witness_position FROM
          (SELECT m.kind,m.key_hash,min(m.nid) nid FROM block_members m
           JOIN blocks b USING(kind,key_hash) JOIN obs o ON o.nid=m.nid
           WHERE b.contaminated AND o.birth_date IS NOT NULL GROUP BY m.kind,m.key_hash,o.birth_date))
         WHERE witness_position<=2 GROUP BY kind,key_hash HAVING count(*)>1''')
    progress.phase('упорядочивание свидетельств пар')
    db.execute('''CREATE TABLE candidate_keys_unordered AS SELECT r.left_n,r.right_n,
        b.kind,b.key_hash,b.namespace,b.member_count,b.contaminated
        FROM raw_pairs r JOIN blocks b USING(kind,key_hash)''')
    db.execute('''CREATE TABLE candidate_keys AS SELECT *,
        dense_rank() OVER(ORDER BY r.left_n,r.right_n)-1 aggregation_group
        FROM candidate_keys_unordered r ORDER BY r.left_n,r.right_n''')
    db.execute('DROP TABLE candidate_keys_unordered')
    pairs = db.execute('SELECT coalesce(max(aggregation_group)+1,0) FROM candidate_keys').fetchone()[0]
    progress.phase('сборка свидетельств пар по порциям', pairs)
    aggregate_ranges(db, 'candidates', '''SELECT left_n,right_n,
        bool_or(kind='document' AND NOT contaminated) document_match,
        bool_or(kind='card' AND NOT contaminated) card_match,
        bool_or(kind='source_document' AND NOT contaminated) source_document_match,
        bool_or(kind='name_dob') name_dob_match,bool_or(contaminated) contaminated,
        bool_or(kind<>'name_dob') shared_identifier,
        to_json(list_sort(list(DISTINCT struct_pack(kind := kind,key_hash := key_hash,
            namespace := namespace,member_count := member_count,contaminated := contaminated)))) keys_json
        FROM candidate_keys WHERE aggregation_group>=? AND aggregation_group<? GROUP BY left_n,right_n''',
        pairs, progress=progress)
    db.execute('DROP TABLE raw_pairs')
    db.execute('DROP TABLE small_members')
    db.execute('DROP TABLE candidate_keys')
    return count, pairs, int(upper)


def candidate_rows(db, batch_size):
    names = ('observation_id','source_id','source_record_id','locator','name_raw','name_comparison',
             'name_latin_comparison','birth_date','documents_json','identifiers_json')
    columns = ','.join(f'{side}.{name} {prefix}_{name}' for side,prefix in [('a','left'),('b','right')] for name in names)
    cursor = db.execute('SELECT c.*,'+columns+''' FROM candidates c JOIN nodes a ON a.nid=c.left_n
        JOIN nodes b ON b.nid=c.right_n ORDER BY CASE WHEN document_match THEN 0 WHEN card_match THEN 1
        WHEN source_document_match THEN 2 ELSE 3 END,c.left_n,c.right_n''')
    reader = getattr(cursor, 'to_arrow_reader', cursor.fetch_record_batch)(batch_size)
    for batch in reader:
        for row in batch.to_pylist():
            yield row,{k:row['left_'+k] for k in names},{k:row['right_'+k] for k in names}
