"""Verified source associations and per-marker availability for a frozen product.

The supplement never rewrites people, journeys, the baseline rules or scores.
An exact observation association is not a claim of resolved physical identity.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from zipfile import ZipFile

import duckdb
import yaml

from ..provenance import fingerprint

VERSION = 'source-assessment:1.0'
TABLES = ('source_links', 'segment_source_links', 'profile_identifiers',
          'document_conflicts', 'conflict_members', 'marker_evidence',
          'marker_coverage', 'profile_assessments')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def query_rows(db, sql, params=None):
    cursor = db.execute(sql, params or [])
    fields = [c[0] for c in cursor.description]
    return [dict(zip(fields, row)) for row in cursor.fetchall()]


def verify(root, product_manifest_sha256):
    root = Path(root).resolve()
    manifest = read_json(root / 'assessment_manifest.json')
    if (manifest.get('version') != VERSION or manifest.get('status') != 'complete_with_limitations'
            or manifest.get('run_id') != 'assessment-' + digest(manifest.get('request'))[:20]
            or manifest['request']['product_manifest_sha256'] != product_manifest_sha256):
        raise ValueError('Source assessment does not belong to this product')
    if set(manifest.get('files', {})) != {n + '.parquet' for n in TABLES} | {'assessment_report.json'}:
        raise ValueError('Incomplete source assessment')
    for name, expected in manifest['files'].items():
        path = root / name
        if path.is_symlink() or not path.is_file() or fingerprint(path) != expected:
            raise ValueError('Source assessment checksum mismatch: ' + name)
    return manifest, read_json(root / 'assessment_report.json')


def _inputs(product, canonical, archive, raw_dir, repo):
    """Allow one specific ID bridge only after whole-file equivalence is proved."""
    catalog = read_json(repo / 'scripts/dataset_manifest.json')
    if fingerprint(archive) != catalog['archive']['sha256']:
        raise ValueError('Original dataset ZIP does not match the committed catalog')
    cm = read_json(canonical / 'canonical_manifest.json')
    cr = read_json(canonical / 'canonical_report.json')
    pm = read_json(product / 'run_manifest.json')
    files = [product / 'run_manifest.json', canonical / 'canonical_manifest.json',
             canonical / 'canonical_report.json', archive, repo / 'configs/markers_v2.yaml',
             repo / 'scripts/dataset_manifest.json', Path(__file__).resolve()]
    for name in ('observations', 'identifiers', 'travel_observations'):
        path = canonical / (name + '.parquet')
        if fingerprint(path) != cm['files'][path.name]:
            raise ValueError('Canonical checksum mismatch: ' + path.name)
        files.append(path)
    for name in ('persons', 'segments', 'rankings', 'explanations'):
        path = product / 'tables' / (name + '.parquet')
        if fingerprint(path) != pm['files']['tables/' + path.name]:
            raise ValueError('Product checksum mismatch: ' + path.name)
        files.append(path)
    reports = {r['source_id']: r for r in cr['source_reports'].values()}
    source_files = {'boarding_csv': 'BoardingData.csv', 'forum_json': 'FrequentFlyerForum-Profiles.json',
                    'pointz_xml': 'PointzAggregator-AirlinesData.xml', 'sirena_fixed': 'Sirena-export-fixed.tab',
                    'skyteam_yaml': 'SkyTeam-Exchange.yaml', 'boardingpass_xlsx': 'YourBoardingPassDotAero.zip'}
    bridges = []
    for source, filename in source_files.items():
        report_hash = reports[source]['sha256']
        original_hash = catalog['files'][filename]['sha256']
        if report_hash == original_hash:
            continue
        if source != 'sirena_fixed':
            raise ValueError('Unverified source version difference: ' + source)
        local = raw_dir / filename
        if fingerprint(local) != report_hash:
            raise ValueError('Local Sirena differs from the canonical source version')
        lf_hash = hashlib.sha256()
        with local.open('rb') as stream:
            for line in stream:
                lf_hash.update(line.replace(b'\r\n', b'\n'))
        if lf_hash.hexdigest() != original_hash:
            raise ValueError('Sirena versions differ in content, not just line endings')
        with ZipFile(archive) as z:
            matches = [n for n in z.namelist() if Path(n).name == filename]
            if len(matches) != 1:
                raise ValueError('Ambiguous Sirena ZIP member')
            h = hashlib.sha256()
            with z.open(matches[0]) as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    h.update(chunk)
            if h.hexdigest() != original_hash:
                raise ValueError('Sirena original ZIP member hash mismatch')
        files.append(local)
        bridges.append({'source_id': source, 'canonical_sha256': report_hash,
                        'original_sha256': original_hash, 'proof': 'whole_file_CRLF_to_LF_equality'})
    hashes = {str(p): fingerprint(p) for p in files}
    request = {'version': VERSION, 'product_manifest_sha256': hashes[str(product / 'run_manifest.json')],
               'inputs': hashes, 'source_version_bridges': bridges}
    return request, pm, cr


def build(product, canonical, archive, raw_dir, output, memory_limit='1GB', progress=print):
    product, canonical, archive, raw_dir, output = [Path(p).resolve()
        for p in (product, canonical, archive, raw_dir, output)]
    repo = Path(__file__).resolve().parents[3]
    if any(output == p or p in output.parents or output in p.parents
           for p in (product, canonical, raw_dir)):
        raise ValueError('Assessment output must be separate from its input directories')
    progress('Проверка входных отпечатков и версий Sirena')
    request, pm, cr = _inputs(product, canonical, archive, raw_dir, repo)
    if output.exists():
        manifest, report = verify(output, request['product_manifest_sha256'])
        if manifest['request'] != request:
            raise ValueError('Output is immutable; use a new output directory')
        return report
    cfg = yaml.safe_load((repo / 'configs/markers_v2.yaml').read_text(encoding='utf-8'))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='assessment-', suffix='.partial', dir=output.parent) as tmp:
        stage = Path(tmp) / 'snapshot'
        stage.mkdir()
        with duckdb.connect() as db:
            db.execute('SET threads=2')
            db.execute('SET memory_limit=?', [memory_limit])
            db.execute('SET temp_directory=?', [str(Path(tmp) / 'spill')])
            for name in ('observations', 'identifiers', 'travel_observations'):
                db.read_parquet(str(canonical / (name + '.parquet'))).create_view(name)
            for name in ('persons', 'segments', 'rankings', 'explanations'):
                db.read_parquet(str(product / 'tables' / (name + '.parquet'))).create_view(name)
            progress('Восстановление точных якорных связей')
            db.execute('''CREATE TABLE observation_aliases AS SELECT observation_id,
                observation_id published_id,'exact_id' alias_method FROM observations''')
            db.execute('''CREATE TABLE travel_aliases AS SELECT travel_id,
                travel_id published_id,'exact_id' alias_method FROM travel_observations''')
            for bridge in request['source_version_bridges']:
                for table, key, kind, alias_table in (
                    ('observations', 'observation_id', 'person_observations', 'observation_aliases'),
                    ('travel_observations', 'travel_id', 'travel_observations', 'travel_aliases')):
                    source = bridge['source_id']
                    # Stable IDs use json.dumps with spaces; Sirena locators are
                    # line numbers, so no string escaping is guessed here.
                    prefix = json.dumps([source, bridge['original_sha256']])[:-1] + ', "'
                    suffix = '", "' + kind + '"]'
                    current_prefix = json.dumps([source, bridge['canonical_sha256']])[:-1] + ', "'
                    bad = db.execute(f'''SELECT count(*) FROM {table} WHERE source_id=? AND
                        (NOT regexp_full_match(locator,'line:[0-9]+(/travel)?')
                         OR {key}<>sha256(?||locator||?))''', [source, current_prefix, suffix]).fetchone()[0]
                    if bad:
                        raise ValueError('Canonical Sirena ID scheme/locator mismatch')
                    db.execute(f'''INSERT INTO {alias_table} SELECT {key},sha256(?||locator||?),
                        'verified_source_version' FROM {table} WHERE source_id=?''', [prefix, suffix, source])
            db.execute('''CREATE TABLE link_seeds AS
                SELECT p.person_id,a.observation_id,'person_anchor:'||a.alias_method alias_method
                FROM persons p JOIN observation_aliases a ON a.published_id=p.anchor_observation_id
                UNION ALL
                SELECT s.person_id,t.observation_id,'segment_anchor:'||a.alias_method alias_method
                FROM segments s JOIN travel_aliases a ON a.published_id=s.anchor_travel_id
                JOIN travel_observations t USING(travel_id)''')
            if db.execute('''SELECT count(*) FROM (SELECT observation_id FROM link_seeds
                GROUP BY observation_id HAVING count(DISTINCT person_id)>1)''').fetchone()[0]:
                raise ValueError('Exact anchors disagree about observation ownership')
            db.execute('''CREATE TABLE source_links AS SELECT l.person_id,l.observation_id,
                o.source_id,o.source_record_id,o.locator,p.identity_status,
                string_agg(DISTINCT l.alias_method,'|' ORDER BY l.alias_method) link_method
                FROM link_seeds l JOIN observations o USING(observation_id) JOIN persons p USING(person_id)
                GROUP BY ALL''')
            db.execute('''CREATE TABLE segment_source_links AS SELECT s.person_id,s.segment_id,
                s.anchor_travel_id published_travel_id,t.travel_id,t.observation_id,t.source_id,
                t.source_record_id,t.locator,a.alias_method link_method
                FROM segments s JOIN travel_aliases a ON a.published_id=s.anchor_travel_id
                JOIN travel_observations t USING(travel_id)''')
            db.execute('''CREATE TABLE profile_identifiers AS SELECT l.person_id,i.source_id,
                i.source_record_id,i.locator,i.identifier_id,i.observation_id,i.kind,i.programme,i.value,
                i.identifier_namespace,i.value_normalized,l.link_method
                FROM identifiers i JOIN source_links l USING(observation_id)''')
            db.execute('''CREATE TABLE link_counts AS SELECT person_id,count(*) linked_observations,
                count(DISTINCT source_id) linked_sources FROM source_links GROUP BY person_id''')
            progress('Документные конфликты по исходным пространствам имён')
            db.execute('''CREATE TABLE doc_rows AS SELECT i.*,o.name_raw,o.name_latin_comparison,o.birth_date
                FROM identifiers i JOIN observations o USING(observation_id)
                WHERE i.kind='document' AND i.value_normalized IS NOT NULL''')
            db.execute('''CREATE TABLE doc_candidates AS SELECT identifier_namespace,value_normalized
                FROM doc_rows GROUP BY ALL HAVING count(DISTINCT birth_date)>1''')
            db.execute('''CREATE TABLE document_conflicts AS SELECT DISTINCT
                'conflict_'||sha256(to_json([a.identifier_namespace,a.value_normalized])) conflict_id,
                a.identifier_namespace,a.value_normalized,
                'document_bundle_'||sha256(a.value_normalized) display_bundle_id,
                'D01' criterion_id,'different_valid_birth_dates_and_incompatible_names' reason
                FROM doc_rows a JOIN doc_candidates k USING(identifier_namespace,value_normalized)
                JOIN doc_rows b USING(identifier_namespace,value_normalized)
                WHERE a.birth_date<>b.birth_date
                  AND length(split_part(a.name_latin_comparison,' ',1))>1
                  AND length(split_part(a.name_latin_comparison,' ',2))>1
                  AND length(split_part(b.name_latin_comparison,' ',1))>1
                  AND length(split_part(b.name_latin_comparison,' ',2))>1
                  AND split_part(a.name_latin_comparison,' ',1)<>split_part(b.name_latin_comparison,' ',1)
                  AND split_part(a.name_latin_comparison,' ',2)<>split_part(b.name_latin_comparison,' ',2)''')
            db.execute('''CREATE TABLE conflict_members AS SELECT c.conflict_id,c.display_bundle_id,
                l.person_id,d.observation_id,d.identifier_id,d.source_id,d.source_record_id,d.locator,
                d.value,d.name_raw,d.birth_date,l.link_method,
                CASE WHEN l.person_id IS NULL THEN 'unlinked' ELSE 'exact_observation_association' END association_status
                FROM document_conflicts c JOIN doc_rows d USING(identifier_namespace,value_normalized)
                LEFT JOIN source_links l USING(observation_id)''')
            progress('PNR: проверка наблюдаемых записей без глобального запрета ambiguous')
            db.execute('''CREATE TABLE pnr_records AS SELECT DISTINCT l.person_id,l.segment_id,
                t.observation_id,t.source_id,t.source_record_id,t.locator,t.travel_id,
                t.booking_code_normalized pnr,o.name_latin_comparison passenger_name,
                sha256(to_json([t.source_id,t.flight_number_normalized,t.departure_local,
                    s.origin_airport_id,s.destination_airport_id,t.booking_code_normalized])) booking_event_id
                FROM segment_source_links l JOIN travel_observations t USING(travel_id)
                JOIN segments s USING(segment_id) JOIN observations o ON o.observation_id=t.observation_id
                WHERE t.booking_code_normalized IS NOT NULL AND t.booking_code_normalized<>''
                  AND o.name_latin_comparison IS NOT NULL AND o.name_latin_comparison<>''
                  AND t.flight_number_normalized IS NOT NULL
                  AND t.departure_precision IN ('minute','second')
                  AND s.resolution_status='resolved'
                  AND s.origin_airport_id IS NOT NULL AND s.destination_airport_id IS NOT NULL
                  AND upper(trim(coalesce(t.origin_raw,''))) NOT IN ('','REA')
                  AND upper(trim(coalesce(t.destination_raw,''))) NOT IN ('','REA')''')
            db.execute('''CREATE TABLE pnr_groups AS SELECT booking_event_id,
                count(DISTINCT person_id) profiles,count(DISTINCT passenger_name) passenger_names
                FROM pnr_records GROUP BY booking_event_id''')
            db.execute('''CREATE TABLE marker_evidence AS
                SELECT l.person_id,'D01' criterion_id,d.observation_id,NULL::VARCHAR travel_id,
                    NULL::VARCHAR segment_id,d.source_id,d.source_record_id,d.locator,d.value,
                    c.conflict_id evidence_group_id,c.conflict_id IS NOT NULL AS is_matched
                FROM doc_rows d JOIN source_links l USING(observation_id)
                LEFT JOIN document_conflicts c USING(identifier_namespace,value_normalized)
                WHERE d.birth_date IS NOT NULL AND d.name_latin_comparison IS NOT NULL
                UNION ALL
                SELECT r.person_id,'P01',r.observation_id,r.travel_id,r.segment_id,r.source_id,
                    r.source_record_id,r.locator,r.pnr,r.booking_event_id,
                    g.profiles>1 AND g.passenger_names>1
                FROM pnr_records r JOIN pnr_groups g USING(booking_event_id)''')
            db.execute('''CREATE TABLE source_marker_results AS SELECT person_id,criterion_id,
                count(*) used_observations,bool_or(is_matched) AS is_matched
                FROM marker_evidence GROUP BY person_id,criterion_id''')
            new_settings = [{'criterion_id': cid, 'title_ru': cfg['criteria'][cid]['title_ru'],
                             'weight': cfg['criteria'][cid]['weight']} for cid in ('D01','P01','B01')]
            import pyarrow as pa
            db.register('new_settings', pa.Table.from_pylist(new_settings))
            db.execute('''CREATE TABLE marker_coverage AS
                SELECT person_id,criterion_id,title_ru,status,value,threshold,weight,used_observations,
                    availability_reason,contribution,contribution preview_contribution,
                    'baseline' evaluation_scope,true included_in_ranking
                FROM explanations
                UNION ALL
                SELECT p.person_id,k.criterion_id,k.title_ru,
                    CASE WHEN r.person_id IS NULL THEN 'unavailable' WHEN r.is_matched THEN 'matched' ELSE 'not_matched' END,
                    CASE WHEN r.person_id IS NOT NULL THEN r.is_matched::INTEGER::DOUBLE END,
                    1.0,k.weight,coalesce(r.used_observations,0),
                    CASE WHEN k.criterion_id='B01' THEN 'Нет явного пассажирского статуса неявки'
                         WHEN r.person_id IS NULL THEN 'Нет пригодных связанных записей для этого маркера' END,
                    0.0,CASE WHEN r.is_matched THEN k.weight ELSE 0.0 END,
                    'linked_source_records',false
                FROM persons p CROSS JOIN new_settings k LEFT JOIN source_marker_results r
                    ON r.person_id=p.person_id AND r.criterion_id=k.criterion_id''')
            db.execute('''CREATE TABLE profile_assessments AS SELECT p.person_id,
                p.identity_status='accepted' identity_accepted,
                count(*) FILTER(WHERE m.status<>'unavailable') evaluated_criteria_count,
                count(*) FILTER(WHERE m.status='matched') assessment_matched_count,
                count(*) FILTER(WHERE m.status='matched' AND NOT m.included_in_ranking) source_matched_count,
                CASE WHEN count(*) FILTER(WHERE m.status<>'unavailable')=0 THEN 'insufficient_data'
                     WHEN count(*) FILTER(WHERE m.status='matched')>0 THEN 'eligible' ELSE 'no_matches' END assessment_status,
                coalesce(l.linked_observations,0) linked_observations,p.observation_count expected_observations,
                coalesce(l.linked_observations,0)=p.observation_count AND coalesce(l.linked_sources,0)=p.source_count source_links_complete
                FROM persons p JOIN marker_coverage m USING(person_id) LEFT JOIN link_counts l USING(person_id)
                GROUP BY ALL''')
            progress('Проверка связности, сохранности исходных факторов и запись Parquet')
            checks = {}
            checks['person_anchors_unresolved'] = db.execute('''SELECT count(*) FROM persons p ANTI JOIN observation_aliases a
                ON a.published_id=p.anchor_observation_id''').fetchone()[0]
            checks['segment_anchors_unresolved'] = db.execute('SELECT (SELECT count(*) FROM segments)-(SELECT count(*) FROM segment_source_links)').fetchone()[0]
            checks['associations_exceed_frozen_observation_count'] = db.execute('''SELECT count(*) FROM profile_assessments
                WHERE linked_observations>expected_observations''').fetchone()[0]
            checks['duplicate_profile_assessments'] = db.execute('SELECT count(*)-count(DISTINCT person_id) FROM profile_assessments').fetchone()[0]
            checks['baseline_rows_changed'] = db.execute('''SELECT count(*) FROM (
                (SELECT person_id,criterion_id,status,value,threshold,weight,contribution FROM marker_coverage WHERE included_in_ranking
                 EXCEPT SELECT person_id,criterion_id,status,value,threshold,weight,contribution FROM explanations)
                UNION ALL
                (SELECT person_id,criterion_id,status,value,threshold,weight,contribution FROM explanations
                 EXCEPT SELECT person_id,criterion_id,status,value,threshold,weight,contribution FROM marker_coverage WHERE included_in_ranking))''').fetchone()[0]
            checks['inferred_no_shows'] = db.execute("SELECT count(*) FROM marker_coverage WHERE criterion_id='B01' AND (status<>'unavailable' OR value IS NOT NULL OR contribution<>0)").fetchone()[0]
            if any(checks.values()):
                raise ValueError('Source assessment integrity checks failed: ' + str(checks))
            counts = {}
            for table in TABLES:
                counts[table] = db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                path = str(stage / (table + '.parquet')).replace("'", "''")
                order = 'person_id,criterion_id' if table == 'marker_coverage' else 'person_id' if table in {
                    'source_links','segment_source_links','profile_identifiers','marker_evidence','profile_assessments'} else 'conflict_id'
                db.execute(f"COPY (SELECT * FROM {table} ORDER BY {order}) TO '{path}' (FORMAT PARQUET, COMPRESSION ZSTD)")
            statistics = query_rows(db, '''SELECT count(*) profiles,
                count(*) FILTER(WHERE evaluated_criteria_count>0) assessed,
                count(*) FILTER(WHERE assessment_status='eligible') with_signals,
                count(*) FILTER(WHERE assessment_status='no_matches') without_signals,
                count(*) FILTER(WHERE assessment_status='insufficient_data') unavailable,
                count(*) FILTER(WHERE identity_accepted) identity_accepted,
                count(*) FILTER(WHERE source_links_complete) source_links_complete
                FROM profile_assessments''')[0]
            restored = query_rows(db, '''SELECT
                (SELECT count(*) FROM persons p JOIN observation_aliases a ON a.published_id=p.anchor_observation_id
                 WHERE a.alias_method='verified_source_version') person_anchors,
                (SELECT count(*) FROM segment_source_links WHERE link_method='verified_source_version') segment_anchors''')[0]
            report = {'version': VERSION, 'statistics': statistics, 'restored_version_anchors': restored,
                      'counts': counts, 'checks': checks,
                      'marker_states': query_rows(db, '''SELECT criterion_id,status,count(*) profiles FROM marker_coverage
                          GROUP BY criterion_id,status ORDER BY criterion_id,status'''),
                      'source_version_bridges': request['source_version_bridges'],
                      'canonical_status': cr['status'], 'product_run_id': pm['run_id'],
                      'prepared_source_markers': ['D01','P01','B01'],
                      'pending_source_markers': ['L01','M07','D02','N01'],
                      'limitations': [
                          'Exact anchors recover source associations, not all T13 component members; completeness is measured per profile.',
                          'Ambiguous identity remains ambiguous. Source checks apply to linked records, not a proven unique physical person.',
                          'Not matched means no match in the observed linked records; completeness of external passenger lists is unknown.',
                          'PNR is scoped to source, local departure, flight and resolved route; unrelated code reuse does not create a shared booking.',
                          'New source markers are evaluated separately and do not change baseline ranks, weights or scores in this step.',
                          'No passenger no-show status exists; B01 is unavailable.',
                          'The canonical PDF report is incomplete; this supplement does not consume or repair historical timetable data.',
                      ]}
        if any(fingerprint(Path(path)) != expected for path, expected in request['inputs'].items()):
            raise ValueError('Inputs changed during source assessment build')
        report['all_inputs_unchanged'] = True
        (stage / 'assessment_report.json').write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        manifest = {'version': VERSION, 'status': 'complete_with_limitations',
                    'run_id': 'assessment-' + digest(request)[:20], 'request': request,
                    'files': {p.name: fingerprint(p) for p in sorted(stage.iterdir())}}
        (stage / 'assessment_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')
        verify(stage, request['product_manifest_sha256'])
        os.replace(stage, output)
    return report
