"""Author check of T13 sample evidence; never creates independent review labels."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from difflib import SequenceMatcher
from pathlib import Path

RU = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
              ('a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya')))
NULLS = {'', 'none', 'not presented', 'n/a'}
COMPONENT_RULES = {'component_support', 'component_name_uncertain', 'component_document_conflict'}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def normalize_name(value):
    text = unicodedata.normalize('NFKC', value).strip()
    if text.casefold() in NULLS:
        return None, None
    text = ' '.join(text.split()).casefold()
    latin = ''.join(RU.get(c, c) for c in text)
    folded = []
    for char in latin:
        if 'LATIN' in unicodedata.name(char, ''):
            folded.extend(c for c in unicodedata.normalize('NFKD', char) if not unicodedata.combining(c))
        else:
            folded.append(char)
    return text, ''.join(folded)


def similarity(left, right):
    a, x = normalize_name(left)
    b, y = normalize_name(right)
    if a and a == b:
        return 1.0
    if not x or not y:
        return None
    scores = []
    for p, q in ((x, y), (' '.join(sorted(x.split())), ' '.join(sorted(y.split())))):
        p, q = sorted((p, q))
        scores.append(SequenceMatcher(None, p, q, autojunk=False).ratio())
    return max(scores)


def identifier_keys(evidence, kind):
    result = defaultdict(set)
    for item in evidence['identifiers']:
        if item['kind'] == kind:
            result[item['namespace']].add(item['key_hash'])
    return result


def has_initial(value):
    _, text = normalize_name(value)
    return bool(text and any(re.fullmatch(r'[a-z]\.?', token) for token in text.split()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', type=Path, default=Path('data/review/person_pairs.csv'))
    parser.add_argument('--report', type=Path, default=Path('data/processed/resolution_report.json'))
    parser.add_argument('--config', type=Path, default=Path('configs/resolution.yaml'))
    parser.add_argument('--output', type=Path, default=Path('data/review/t13_author_audit'))
    args = parser.parse_args()
    originals = {str(p.resolve()): digest(p) for p in (args.sample, args.report, args.config)}
    report = json.loads(args.report.read_text(encoding='utf-8-sig'))
    with args.sample.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.DictReader(stream))
    # These values are pinned by the supplied resolution configuration hash.
    if digest(args.config) != report['config_sha256']:
        raise ValueError('Config SHA-256 differs from the supplied run report')
    if report['config_sha256'] != '726ff9022878e77ee8828b41a2cbbd201fe0edc6f75b9618d281c6fa26eb6575':
        raise ValueError('This audit implements only the supplied resolution:1.0 configuration')
    if len({r['pair_id'] for r in rows}) != len(rows):
        raise ValueError('Duplicate pair_id')
    strata = Counter(row['author_decision'] for row in rows)
    if dict(strata) != report['review']['selected']:
        raise ValueError('CSV strata differ from the reported review sample')
    if any(r['run_fingerprint'] != report['run_fingerprint'] for r in rows):
        raise ValueError('CSV fingerprint differs from the supplied run report')
    findings = []
    for number, row in enumerate(rows, 1):
        features, evidence = json.loads(row['features_json']), json.loads(row['evidence_json'])
        errors = []
        def check(condition, code):
            if not condition:
                errors.append(code)
        for side in ('left', 'right'):
            for field in ('observation_id', 'source_id', 'locator'):
                check(evidence[side][field] == row[side+'_'+field], side+'_'+field+'_provenance')
            check(bool(evidence[side]['source_record_id']), side+'_source_record')
            for identifier in evidence[side]['identifiers']:
                check(all(identifier.get(k) for k in ('identifier_id', 'kind', 'namespace', 'key_hash', 'source_record_id', 'locator')), side+'_identifier_provenance')
                namespace = json.loads(identifier['namespace'])
                check(len(namespace) == 3 and namespace[0] == identifier['kind'] and bool(namespace[2]), side+'_identifier_namespace')
                if namespace[1] == 'source':
                    check(namespace[2] == row[side+'_source_id'], side+'_identifier_source_namespace')
                if identifier['kind'] == 'loyalty_card':
                    check(namespace[1] == 'programme', side+'_card_programme_namespace')
                check(bool(re.fullmatch('[0-9a-f]{64}', identifier['key_hash'])), side+'_identifier_key_hash')
        expected_pair = 'pair_'+hashlib.sha256(dumps(['pair','1.0',row['left_observation_id'],row['right_observation_id']]).encode()).hexdigest()
        check(row['pair_id'] == expected_pair, 'pair_id')
        left_dob, right_dob = row['left_birth_date'], row['right_birth_date']
        for value in (left_dob, right_dob):
            if value:
                check(date.fromisoformat(value).isoformat() == value, 'birth_date_iso')
        same_dob = bool(left_dob and left_dob == right_dob)
        different_dob = bool(left_dob and right_dob and left_dob != right_dob)
        score = similarity(row['left_name'], row['right_name'])
        expected_score = float(row['name_similarity']) if row['name_similarity'] else None
        check(score is expected_score if score is None or expected_score is None else math.isclose(score,expected_score,abs_tol=1e-12), 'name_similarity_from_raw')
        check(features['name_similarity'] == expected_score, 'name_similarity_column')
        check(features['same_birth_date'] == same_dob, 'same_birth_date_feature')
        left_documents, right_documents = (identifier_keys(evidence[s], 'document') for s in ('left', 'right'))
        common_doc_namespaces = left_documents.keys() & right_documents.keys()
        conflicting_doc_namespaces = sum(not (left_documents[ns] & right_documents[ns]) for ns in common_doc_namespaces)
        common_documents = sum(len(left_documents[ns] & right_documents[ns]) for ns in common_doc_namespaces)
        left_cards, right_cards = (identifier_keys(evidence[s], 'loyalty_card') for s in ('left', 'right'))
        common_cards = sum(len(left_cards[ns] & right_cards[ns]) for ns in left_cards.keys() & right_cards.keys())
        blocks = features['blocking_keys']
        for block in blocks:
            check(block['kind'] in ('card','source_document','document','name_dob'), 'block_kind')
            check(block['member_count'] >= 2, 'block_size')
            if block['kind'] in ('card', 'source_document', 'document'):
                kind = 'loyalty_card' if block['kind'] == 'card' else 'document'
                a, b = (identifier_keys(evidence[s], kind) for s in ('left', 'right'))
                check(block['key_hash'] in a.get(block['namespace'], set()) & b.get(block['namespace'], set()), 'block_identifier_evidence')
                namespace = json.loads(block['namespace'])
                check(namespace[0] == kind and namespace[1] == {'card':'programme','source_document':'source','document':'confirmed'}[block['kind']], 'block_namespace_context')
            elif block['kind'] == 'name_dob':
                a, b = (normalize_name(row[s+'_name'])[1] for s in ('left', 'right'))
                prefixes = {v[:3] for v in (a or '').split() if len(v) >= 3} & {v[:3] for v in (b or '').split() if len(v) >= 3}
                possible = {hashlib.sha256(dumps([left_dob,v]).encode()).hexdigest() for v in prefixes}
                check(same_dob and len((a or '').split()) >= 2 and len((b or '').split()) >= 2 and block['key_hash'] in possible, 'name_dob_block_from_raw')
        signals = {feature:any(b['kind']==kind and not b['contaminated'] for b in blocks)
                   for feature,kind in (('card_match','card'),('document_match','document'),('source_document_match','source_document'))}
        for key, value in signals.items():
            check(features[key] == value, key+'_feature')
        check(features['name_dob_block'] == any(b['kind']=='name_dob' for b in blocks), 'name_dob_feature')
        check(features['contaminated_identifier'] == any(b['contaminated'] for b in blocks), 'contamination_feature')
        if different_dob:
            direct_status,direct_rule = 'rejected','birth_date_conflict'
        elif conflicting_doc_namespaces:
            direct_status,direct_rule = 'rejected','document_context_conflict'
        elif score is not None and score < .45:
            direct_status,direct_rule = 'rejected','name_conflict'
        elif signals['document_match'] or signals['card_match']:
            direct_status,direct_rule = ('ambiguous','identifier_with_uncertain_name') if score is not None and score < .90 else ('accepted','confirmed_document' if signals['document_match'] else 'programme_card')
        elif signals['source_document_match']:
            direct_status,direct_rule = ('accepted','source_document_name_birth_date') if same_dob and score is not None and score >= .90 else ('ambiguous','unconfirmed_document_context')
        elif features['contaminated_identifier']:
            direct_status,direct_rule = 'ambiguous','contaminated_identifier'
        else:
            direct_status,direct_rule = 'ambiguous','name_birth_date_only'
        check((features['direct_status'],features['direct_rule']) == (direct_status,direct_rule), 'direct_rule_from_evidence')
        rule = row['rule_id'].split(':')[-1]
        category, note = 'direct_rule_supported', 'Доступные признаки согласуются с прямым правилом. Личность независимо не подтверждена.'
        direct_notes = {
            'programme_card':'Совпадает ключ карты в одном программном пространстве; в прямых признаках нет конфликта DOB, документов или доступных имён. Это основание правила, а не независимое подтверждение личности.',
            'source_document_name_birth_date':'Общий документ в одном source-scoped пространстве, одинаковая полная DOB и сходство имён не ниже 0.90. Прямое правило принятия соблюдено.',
            'name_birth_date_only':'Есть совпадение DOB и блока имени, общего сильного ключа нет. Неоднозначность соответствует консервативному правилу даже при точной транслитерации ФИО.',
            'unconfirmed_document_context':'Общий source-scoped документ есть; для принятия не хватает одинаковой полной DOB либо сходства имени 0.90. Решение ambiguous согласуется с правилом.',
            'identifier_with_uncertain_name':'Общий сильный ключ есть, сходство имён между 0.45 и 0.90. Автоматическое принятие отложено согласно порогу.',
            'document_context_conflict':'Хеши документов в общем пространстве имён не пересекаются. Прямое правило отклонения соблюдено; типы и история документов в CSV отсутствуют.',
            'name_conflict':'Сходство доступных имён ниже 0.45; совпадение DOB/слабого блока не даёт основания объединять пару.',
        }
        note = direct_notes.get(rule, note)
        if rule in COMPONENT_RULES:
            category = 'component_context_required'
            if rule == 'component_support':
                check(row['author_decision']=='accepted' and direct_status=='ambiguous', 'component_support_transition')
                note = 'Принятие опирается на транзитивный путь; в CSV нет полного состава компонента и пути сильных связей.'
            else:
                check(features.get('component_constraints') is not None and direct_status=='accepted', 'component_constraints_missing')
                check(row['author_decision']==('ambiguous' if rule=='component_name_uncertain' else 'rejected'), 'component_restriction_transition')
                note = 'Прямая пара имеет общий ключ, решение ограничено другими участниками компонента. Нужны их имена/документы и история связей.'
        else:
            check((row['author_decision'],rule) == (direct_status,direct_rule), 'final_direct_decision')
        same_names = score == 1.0
        if row['author_decision']=='rejected' and same_dob and same_names and conflicting_doc_namespaces:
            category = 'document_history_required'
            note = 'Совпадают ФИО и DOB, но документы в source-scoped пространстве различаются. Проверить тип, замену и ошибки документа; тёзки также возможны. Автоматически объединять нельзя.'
        abbreviation_candidate = (row['author_decision']=='ambiguous' and bool(common_documents or common_cards)
                                  and bool(has_initial(row['left_name']) != has_initial(row['right_name'])))
        if abbreviation_candidate:
            note += ' Есть инициалы вместо полного имени/отчества: возможна консервативно пропущенная связь; требуется проверка, а не глобальное снижение порога.'
        if errors:
            category = 'inconsistency'
        findings.append(dict(sample_row=number,csv_line=number+1,pair_id=row['pair_id'],author_decision=row['author_decision'],rule_id=row['rule_id'],left_source_id=row['left_source_id'],right_source_id=row['right_source_id'],audit_category=category,
            initial_direct_decision=direct_status,initial_direct_rule=direct_rule,
            left_component_anchor_node_index=features.get('component_constraints',{}).get('left_anchor_node_index',''),
            right_component_anchor_node_index=features.get('component_constraints',{}).get('right_anchor_node_index',''),
            name_similarity='' if score is None else score,name_presence='both' if row['left_name'] and row['right_name'] else 'one' if row['left_name'] or row['right_name'] else 'neither',
            birth_date_relation='conflict' if different_dob else 'same' if same_dob else 'one' if left_dob or right_dob else 'missing',
            shared_card_keys=common_cards,shared_document_keys=common_documents,conflicting_document_namespaces=conflicting_doc_namespaces,
            abbreviation_candidate=abbreviation_candidate,check_errors=';'.join(errors),audit_notes=note))
    args.output.mkdir(parents=True, exist_ok=True)
    output_csv = args.output/'pair_findings.csv'
    with output_csv.open('w',encoding='utf-8-sig',newline='') as stream:
        writer = csv.DictWriter(stream,fieldnames=list(findings[0]));writer.writeheader();writer.writerows(findings)
    summary = dict(scope='author_evidence_and_rule_consistency_check_only',run_fingerprint=report['run_fingerprint'],inputs_sha256=originals,
        sample_rows=len(rows),strata=dict(strata),rule_counts=dict(Counter(r['rule_id'].split(':')[-1] for r in rows)),
        categories=dict(Counter(f['audit_category'] for f in findings)),inconsistencies=[dict(sample_row=f['sample_row'],pair_id=f['pair_id'],errors=f['check_errors']) for f in findings if f['check_errors']],
        direct_computation_matches=len(findings)-sum(bool(f['check_errors']) for f in findings),
        accepted_name_presence=dict(Counter(f['name_presence'] for f in findings if f['author_decision']=='accepted')),
        component_context_required_rows=[f['sample_row'] for f in findings if f['audit_category']=='component_context_required'],
        document_history_required_rows=[f['sample_row'] for f in findings if f['audit_category']=='document_history_required'],
        abbreviation_candidate_rows=[f['sample_row'] for f in findings if f['abbreviation_candidate']],
        independently_reviewed_in_uploaded_csv=sum(bool(r['reviewer_decision']) for r in rows),independent_acceptance='pending',
        identity_precision=None,population_accuracy=None,
        limitations=['Agreement with implemented rules is not identity ground truth.',
          'Shared identifier hashes validate equality in the supplied evidence; original identifier values were not supplied for most sources.',
          'Full components and the historical merge path are absent from the sample.',
          'Names were compared under the supplied canonical:1.0 NFKC/casefold/ru_ascii_v1 rules; original documents and raw source files were not read.',
          'Membership in the exact published deterministic sample cannot be independently verified without the published snapshot or all person_pairs.',
          'The 50/30/20 stratified sample does not establish population accuracy.',
          'This author check leaves every independent reviewer field and all source files unchanged.'])
    for path, before in originals.items():
        if digest(path) != before:
            raise RuntimeError('Input was modified: '+path)
    summary['inputs_unchanged'] = True
    (args.output/'audit_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 1 if summary['inconsistencies'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
