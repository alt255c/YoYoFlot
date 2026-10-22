"""Source-marker calculations over exact profile links; no invented passengers."""
from functools import lru_cache
import re

from ..resolve.matching import name_similarity

TRANSLIT_REPLACEMENTS = (('shch','sch'),('kh','h'),('ts','c'),('tc','c'),('ya','ia'),
    ('yu','iu'),('ye','e'),('iy','i'),('yi','i'),('y','i'),('ii','i'),('x','ks'))
TRANSLIT_WORD_ALIASES = {'aliona':'alena','artiom':'artem','fiodor':'fedor',
    'piotr':'petr','semion':'semen','liova':'leva'}


def transliteration_sql(expression):
    """SQL equivalent of the exclusion key; never an identity assignment."""
    for left,right in TRANSLIT_REPLACEMENTS:
        expression=f"replace({expression},'{left}','{right}')"
    cases=' '.join(f"WHEN '{left}' THEN '{right}'" for left,right in TRANSLIT_WORD_ALIASES.items())
    return "array_to_string(list_sort(list_transform(string_split("+expression+",' '),p->CASE p "+cases+" ELSE p END)),' ')"


@lru_cache(maxsize=50000)
def clean_name(value):
    if not value:
        return None
    value = re.sub(r"['’`.-]", '', value.lower())
    parts = value.split()
    # Initials, unreadable encodings and unknown scripts are not letter errors.
    if len(parts) < 2 or any(len(p) < 2 or not re.fullmatch('[a-z]+', p) for p in parts):
        return None
    return ' '.join(sorted(parts))


@lru_cache(maxsize=50000)
def owner_name(value):
    if not value:
        return None
    parts = re.sub(r"['’`.-]", '', value.lower()).split()
    parts = [p for p in parts if len(p) > 1]
    if len(parts) < 2 or any(not re.fullmatch('[a-z]+', p) for p in parts):
        return None
    return transliteration_key(' '.join(sorted(parts)))


@lru_cache(maxsize=50000)
def transliteration_key(value):
    if not value:
        return None
    # Conservative exclusion of common Russian Latin spelling variants. This
    # key suppresses signals only; it NEVER assigns an observation to a person.
    for a, b in TRANSLIT_REPLACEMENTS:
        value = value.replace(a, b)
    return ' '.join(sorted(TRANSLIT_WORD_ALIASES.get(part,part) for part in value.split()))


def distinct_owners(left, right):
    # A shared given name or surname alone does not make two owners identical.
    # Full-name aliases and added patronymics remain uncertain.
    a,b=set(left.split()),set(right.split())
    if a<=b or b<=a or len(a&b)>=2:
        return False
    return (name_similarity({'name_latin_comparison':left},
                            {'name_latin_comparison':right}) or 0) < .90


def prepare(db, cfg, progress):
    db.create_function('marker_translit_key',transliteration_key,['VARCHAR'],'VARCHAR',null_handling='special')
    db.create_function('marker_distinct_owners',distinct_owners,
        ['VARCHAR','VARCHAR'],'BOOLEAN')
    db.execute('''CREATE TABLE marker_diagnostics(person_id VARCHAR,criterion_id VARCHAR,
        details_json VARCHAR,availability_reason VARCHAR)''')
    progress('L01: общие карты, разные сравнимые владельцы, без вариантов транслитерации')
    owner_expression = transliteration_sql("array_to_string(owner_parts,' ')")
    db.execute('''CREATE TABLE marker_people AS WITH cleaned AS (
        SELECT l.*,o.name_latin_comparison,o.birth_date,
            regexp_replace(lower(o.name_latin_comparison),?,'','g') cleaned_name
        FROM source_links l JOIN observations o USING(observation_id)), tokenized AS (
        SELECT *,list_filter(string_split(cleaned_name,' '),p->length(p)>1) owner_parts
        FROM cleaned)
        SELECT * EXCLUDE(cleaned_name,owner_parts),
            CASE WHEN len(owner_parts)>=2 AND regexp_full_match(array_to_string(owner_parts,' '),'[a-z]{2,}( [a-z]{2,})+')
                THEN array_to_string(list_sort(owner_parts),' ') END name_key,
            CASE WHEN len(owner_parts)>=2 AND regexp_full_match(array_to_string(owner_parts,' '),'[a-z]{2,}( [a-z]{2,})+')
                THEN array_to_string(list_sort(string_split('''+owner_expression+''',' ')),' ') END owner_key
        FROM tokenized''',[r"['’`.-]"])
    progress('L01: имена подготовлены, группировка карт')
    db.execute('''CREATE TABLE card_rows AS SELECT i.*,p.owner_key,
        'identity_'||sha256(to_json([i.identifier_namespace,i.value_normalized])) fact_key
        FROM profile_identifiers i JOIN marker_people p USING(person_id,observation_id)
        WHERE i.kind='loyalty_card' AND i.value_normalized IS NOT NULL
          AND i.programme IS NOT NULL AND p.owner_key IS NOT NULL''')
    db.execute('''CREATE TABLE card_owners AS SELECT DISTINCT person_id,identifier_namespace,
        value_normalized,owner_key FROM card_rows''')
    db.execute('''CREATE TABLE card_sizes AS SELECT identifier_namespace,value_normalized,
        count(*) n FROM card_owners GROUP BY ALL''')
    db.execute('''CREATE TABLE shared_cards AS SELECT DISTINCT a.identifier_namespace,a.value_normalized
        FROM card_owners a JOIN card_owners b USING(identifier_namespace,value_normalized)
        JOIN card_sizes k USING(identifier_namespace,value_normalized)
        WHERE k.n<=1000 AND a.person_id<b.person_id AND a.owner_key<>b.owner_key
          AND len(list_intersect(string_split(a.owner_key,' '),string_split(b.owner_key,' ')))<2
          AND jaro_winkler_similarity(a.owner_key,b.owner_key)<.95
          AND marker_distinct_owners(a.owner_key,b.owner_key)''')
    db.execute('''INSERT INTO marker_evidence SELECT r.person_id,'L01',r.observation_id,
        NULL,NULL,r.source_id,r.source_record_id,r.locator,r.value,r.fact_key,
        s.value_normalized IS NOT NULL FROM card_rows r JOIN card_sizes k
        USING(identifier_namespace,value_normalized) LEFT JOIN shared_cards s
        USING(identifier_namespace,value_normalized) WHERE k.n<=1000''')

    db.execute('''INSERT INTO marker_diagnostics SELECT person_id,'L01',
        to_json(struct_pack(observations:=count(DISTINCT observation_id),
            identifiers:=count(DISTINCT fact_key))),NULL FROM card_rows GROUP BY person_id''')
    progress('D02: документы и качество связи с профилем')
    db.execute('''CREATE TABLE document_owner_rows AS SELECT i.* FROM profile_identifiers i
        JOIN marker_people p USING(person_id,observation_id)
        WHERE i.kind='document' AND i.value_normalized IS NOT NULL
          AND p.identity_status='accepted' AND p.name_key IS NOT NULL''')
    db.execute('''CREATE TABLE multiple_documents AS SELECT person_id,count(DISTINCT value_normalized)>1 is_matched
        FROM document_owner_rows GROUP BY person_id''')
    db.execute('''INSERT INTO marker_evidence SELECT d.person_id,'D02',d.observation_id,NULL,NULL,
        d.source_id,d.source_record_id,d.locator,d.value,
        'identity_'||sha256(to_json([d.identifier_namespace,d.value_normalized])),m.is_matched
        FROM document_owner_rows d JOIN multiple_documents m USING(person_id)''')
    db.execute('''INSERT INTO marker_diagnostics SELECT p.person_id,'D02',
        to_json(struct_pack(observations:=count(DISTINCT i.observation_id),
            documents:=count(DISTINCT i.value_normalized),identity_status:=p.identity_status)),
        CASE WHEN p.identity_status<>'accepted' THEN
            'Документные записи есть, но связь записей профиля неоднозначна; разные документы пока нельзя приписать одному человеку'
            WHEN count(DISTINCT m.observation_id)=0 THEN 'В документных записях нет полного сопоставимого имени'
            ELSE NULL END
        FROM persons p JOIN profile_identifiers i USING(person_id)
        LEFT JOIN document_owner_rows m ON m.person_id=i.person_id AND m.observation_id=i.observation_id
        WHERE i.kind='document' AND i.value_normalized IS NOT NULL GROUP BY p.person_id,p.identity_status''')
    from .name_markers import prepare_names
    name_audit=prepare_names(db,progress)

    progress('M07: повторные совместные события, минимум три даты и два направления')
    db.execute('''CREATE TABLE coflight_rows AS SELECT DISTINCT l.person_id,l.segment_id,
        t.observation_id,t.travel_id,t.source_id,t.source_record_id,t.locator,t.flight_number_normalized,
        substr(t.departure_local,1,10) departure_date,
        sha256(to_json([s.origin_airport_id,s.destination_airport_id])) route_key,
        'event_'||sha256(to_json([t.source_id,t.flight_number_normalized,t.departure_local,
             s.origin_airport_id,s.destination_airport_id])) event_id,p.owner_key
        FROM segment_source_links l JOIN travel_observations t USING(travel_id)
        JOIN segments s USING(segment_id) JOIN marker_people p ON p.observation_id=t.observation_id
        WHERE t.event_kind IN ('boarding_record','boarding_pass_record')
          AND t.departure_precision IN ('minute','second') AND t.departure_local IS NOT NULL
          AND t.flight_number_normalized IS NOT NULL AND p.owner_key IS NOT NULL
          AND s.resolution_status='resolved' AND s.origin_airport_id IS NOT NULL
          AND s.destination_airport_id IS NOT NULL
          AND upper(trim(coalesce(t.origin_raw,''))) NOT IN ('','REA')
          AND upper(trim(coalesce(t.destination_raw,''))) NOT IN ('','REA')''')
    db.execute('''CREATE TABLE coflight_members AS SELECT DISTINCT person_id,event_id,departure_date,
        route_key,owner_key FROM coflight_rows''')
    db.execute('''CREATE TABLE coflight_sizes AS SELECT event_id,count(DISTINCT person_id) n
        FROM coflight_members GROUP BY event_id''')
    db.execute('''CREATE TABLE coflight_coverage AS SELECT r.person_id,count(DISTINCT r.event_id) events,
        count(DISTINCT r.departure_date) observed_dates,count(DISTINCT r.route_key) routes
        FROM coflight_members r JOIN coflight_sizes k USING(event_id) WHERE k.n<=1000 GROUP BY r.person_id''')
    db.execute('''INSERT INTO marker_diagnostics SELECT person_id,'M07',
        to_json(struct_pack(events:=events,observed_dates:=observed_dates,routes:=routes,
            required_dates:=?,required_routes:=?)),
        CASE WHEN observed_dates<? OR routes<? THEN
            'Для проверки повторных совместных перелётов нужно больше дат или направлений посадочных записей'
            ELSE NULL END FROM coflight_coverage''',
        [cfg['criteria']['M07']['min_distinct_dates'],cfg['criteria']['M07']['min_distinct_directed_routes']]*2)
    db.execute('''CREATE TABLE coflight_pairs AS SELECT DISTINCT a.person_id person_a,b.person_id person_b,
        a.event_id,a.departure_date,a.route_key FROM coflight_members a
        JOIN coflight_members b USING(event_id) JOIN coflight_sizes k USING(event_id)
        WHERE k.n BETWEEN 2 AND 1000 AND a.person_id<b.person_id
          AND a.owner_key<>b.owner_key
          AND len(list_intersect(string_split(a.owner_key,' '),string_split(b.owner_key,' ')))<2
          AND jaro_winkler_similarity(a.owner_key,b.owner_key)<.95''')
    db.execute('''CREATE TABLE repeated_companions AS SELECT person_a,person_b,
        count(DISTINCT departure_date) shared_dates,count(DISTINCT route_key) shared_routes
        FROM coflight_pairs GROUP BY person_a,person_b HAVING shared_dates>=? AND shared_routes>=?''',
        [cfg['criteria']['M07']['min_distinct_dates'],cfg['criteria']['M07']['min_distinct_directed_routes']])
    db.execute('''DELETE FROM repeated_companions r WHERE NOT EXISTS(
        SELECT 1 FROM coflight_members a JOIN coflight_members b USING(event_id)
        WHERE a.person_id=r.person_a AND b.person_id=r.person_b
          AND marker_distinct_owners(a.owner_key,b.owner_key))''')
    db.execute('''CREATE TABLE companion_events AS SELECT p.person_a person_id,p.event_id FROM coflight_pairs p
        JOIN repeated_companions r USING(person_a,person_b) UNION SELECT p.person_b,p.event_id
        FROM coflight_pairs p JOIN repeated_companions r USING(person_a,person_b)''')
    db.execute('''INSERT INTO marker_evidence SELECT r.person_id,'M07',r.observation_id,r.travel_id,r.segment_id,
        r.source_id,r.source_record_id,r.locator,r.flight_number_normalized,r.event_id,
        m.person_id IS NOT NULL FROM coflight_rows r JOIN coflight_sizes k USING(event_id)
        JOIN coflight_coverage c USING(person_id)
        LEFT JOIN companion_events m USING(person_id,event_id)
        WHERE k.n<=1000 AND c.observed_dates>=? AND c.routes>=?''',
        [cfg['criteria']['M07']['min_distinct_dates'],cfg['criteria']['M07']['min_distinct_directed_routes']])

    progress('Группы зависимости свидетельств и аудит недоступных проверок')
    db.execute('''CREATE TABLE marker_dependencies AS SELECT DISTINCT person_id,criterion_id,
        evidence_group_id dependency_key FROM marker_evidence
        WHERE is_matched AND criterion_id IN ('L01','N01','D02','M07')
        UNION SELECT DISTINCT e.person_id,e.criterion_id,
        'identity_'||sha256(to_json([d.identifier_namespace,d.value_normalized]))
        FROM marker_evidence e JOIN doc_rows d USING(observation_id)
        WHERE e.is_matched AND e.criterion_id='D01'
        UNION SELECT DISTINCT e.person_id,e.criterion_id,
        'event_'||sha256(to_json([t.source_id,t.flight_number_normalized,t.departure_local,
             s.origin_airport_id,s.destination_airport_id]))
        FROM marker_evidence e JOIN travel_observations t USING(travel_id)
        JOIN segments s USING(segment_id) WHERE e.is_matched AND e.criterion_id='P01' ''')
    field_rows = db.execute('''SELECT source_id,event_kind,count(*) records,
        count(arrival_local) arrival_times,count(*) FILTER(WHERE upper(coalesce(status_raw,''))='NO_SHOW') explicit_no_shows
        FROM travel_observations GROUP BY source_id,event_kind ORDER BY source_id''').fetchall()
    return {'travel_fields': [dict(zip(('source_id','event_kind','records','arrival_times','explicit_no_shows'),r)) for r in field_rows],
        'unavailable_reasons': {
            'B01':'В источниках нет явного пассажирского статуса неявки; отсутствие строки не считается неявкой',
            'T01':'Нет двух подтверждённых перемещений с точными временами вылета и прибытия: прибытия есть только у броней Sirena',
            'D03':'Нет подтверждённого пространства имён эмитента билета; один источник или номер рейса не устанавливает эмитента'},
        'repeated_companion_pairs':db.execute('SELECT count(*) FROM repeated_companions').fetchone()[0],
        'shared_loyalty_cards':db.execute('SELECT count(*) FROM shared_cards').fetchone()[0],
        'single_letter_pairs':name_audit['verified_profile_pairs'],
        'name_comparisons':name_audit,
        'oversized_coflight_events_excluded':db.execute('SELECT count(*) FROM coflight_sizes WHERE n>1000').fetchone()[0],
        'oversized_loyalty_blocks_excluded':db.execute('SELECT count(*) FROM card_sizes WHERE n>1000').fetchone()[0],
        'name_comparison_policy':'ASCII names after known canonical transliteration; token order and initials excluded; common Latin variants suppressed; no identity assignment by name'}
