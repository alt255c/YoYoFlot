"""Stratified, deterministic human review; never fabricate reviewed decisions."""
from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime
from pathlib import Path

from ..normalize.values import file_hash

FIELDS = ['run_fingerprint','pair_id','author_decision','rule_id','left_observation_id','right_observation_id',
          'left_source_id','right_source_id','left_locator','right_locator','left_name','right_name',
          'left_birth_date','right_birth_date','name_similarity','features_json','evidence_json',
          'reviewer_decision','reviewer_name','reviewed_at','reviewer_notes']
EDITABLE = {'reviewer_decision','reviewer_name','reviewed_at','reviewer_notes'}


def sample_pairs(db, rules, path, fingerprint):
    available = dict(db.execute('SELECT status,count(*) FROM person_pairs GROUP BY status').fetchall())
    selected = {}
    with Path(path).open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        for status, quota in rules['review_sample'].items():
            cursor = db.execute('''SELECT p.pair_id,p.status author_decision,p.rule_id,
                p.left_observation_id,p.right_observation_id,a.source_id left_source_id,b.source_id right_source_id,
                a.locator left_locator,b.locator right_locator,a.name_raw left_name,b.name_raw right_name,
                a.birth_date left_birth_date,b.birth_date right_birth_date,p.name_similarity,p.features_json,p.evidence_json
                FROM person_pairs p JOIN nodes a ON p.left_observation_id=a.observation_id
                JOIN nodes b ON p.right_observation_id=b.observation_id
                WHERE p.status=? ORDER BY sha256(?||p.pair_id),p.pair_id LIMIT ?''',
                [status,rules['review_seed'],quota])
            names = [c[0] for c in cursor.description]
            rows = cursor.fetchall()
            selected[status] = len(rows)
            for row in rows:
                writer.writerow(dict(zip(names,row),run_fingerprint=fingerprint))
    return dict(target=dict(rules['review_sample']), available=available, selected=selected,
                shortages={s:max(0,q-selected[s]) for s,q in rules['review_sample'].items()},
                reviewed=0, acceptance='pending_independent_review',
                method='deterministic SHA-256 ordering within accepted/ambiguous/rejected strata',
                seed=rules['review_seed'], population_accuracy=None)


def reserve_review_copy(snapshot, directory, fingerprint):
    """Create a review copy exclusively; a reviewer-edited file is never replaced."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ('person_pairs.csv',f'person_pairs.{fingerprint[:12]}.csv',f'person_pairs.{fingerprint}.csv'):
        path = directory/name
        try:
            with path.open(encoding='utf-8-sig', newline='') as f:
                reader = csv.DictReader(f)
                first = next(reader, None)
                if first and first.get('run_fingerprint') == fingerprint:
                    return path, False
                if first is None and path.read_bytes() == Path(snapshot).read_bytes():
                    return path, False
            continue
        except FileNotFoundError:
            pass
        try:
            fd = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(Path(snapshot).read_bytes())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path, True
    raise ValueError('Existing review files do not match the current input fingerprint')


def audit_review(sample, snapshot, fingerprint):
    def read(path):
        with Path(path).open(encoding='utf-8-sig', newline='') as f:
            reader = csv.DictReader(f)
            if reader.fieldnames != FIELDS:
                raise ValueError('Unexpected review CSV header')
            rows = list(reader)
        if len(rows) > 10000 or len({r['pair_id'] for r in rows}) != len(rows):
            raise ValueError('Invalid or repeated review pair_id')
        return {r['pair_id']:r for r in rows}
    expected, actual = read(snapshot), read(sample)
    if expected.keys() != actual.keys():
        raise ValueError('Review sample rows differ from the published sample')
    confusion, reviewed, disagreements = Counter(), Counter(), []
    for pair_id, row in actual.items():
        if row['run_fingerprint'] != fingerprint:
            raise ValueError('Stale review sample: input/config fingerprint mismatch')
        if any(row[k] != expected[pair_id][k] for k in FIELDS if k not in EDITABLE):
            raise ValueError('Review evidence/author fields were edited: '+pair_id)
        decision = row['reviewer_decision'].strip()
        if not decision:
            continue
        if decision not in ('accepted','ambiguous','rejected'):
            raise ValueError('reviewer_decision must be accepted/ambiguous/rejected')
        if not row['reviewer_name'].strip() or not row['reviewed_at'].strip():
            raise ValueError('A reviewed decision requires reviewer_name and reviewed_at')
        datetime.fromisoformat(row['reviewed_at'].replace('Z','+00:00'))
        author = row['author_decision']
        confusion[(author,decision)] += 1
        reviewed[author] += 1
        if author != decision:
            disagreements.append(dict(pair_id=pair_id,author_decision=author,reviewer_decision=decision,
                                      reviewer_name=row['reviewer_name'],reviewer_notes=row['reviewer_notes']))
    accepted_known = confusion[('accepted','accepted')] + confusion[('accepted','rejected')]
    return dict(run_fingerprint=fingerprint,sampled=len(actual),reviewed=sum(reviewed.values()),
                reviewed_by_stratum=dict(reviewed),unreviewed=len(actual)-sum(reviewed.values()),
                confusion=[dict(author=a,reviewer=b,count=n) for (a,b),n in sorted(confusion.items())],
                disagreements=disagreements,
                accepted_sample_precision=confusion[('accepted','accepted')]/accepted_known if accepted_known else None,
                accepted_sample_denominator=accepted_known,population_accuracy=None,
                false_merge_sample_count=confusion[('accepted','rejected')],
                rejected_match_sample_count=confusion[('rejected','accepted')],
                disagreement_rate=len(disagreements)/sum(reviewed.values()) if sum(reviewed.values()) else None,
                scope='reviewed sample only; stratified sample is not a population accuracy estimate',
                independence='reviewer identity and independence require human verification')


def audit_to_file(sample, output, report_path):
    output = Path(output)
    manifest = json.loads((output/'resolution_manifest.json').read_text(encoding='utf-8'))
    for name in ('resolution_report.json','resolution_review_sample.csv'):
        if file_hash(output/name)!=manifest.get('files',{}).get(name):
            raise ValueError('Published T13 review evidence checksum mismatch: '+name)
    report = json.loads((output/'resolution_report.json').read_text(encoding='utf-8'))
    result = audit_review(sample, output/'resolution_review_sample.csv', report['run_fingerprint'])
    result['target'] = report['review']['target']
    result['shortages'] = report['review']['shortages']
    result['sample_fully_reviewed'] = bool(result['sampled'] and result['reviewed']==result['sampled'])
    result['acceptance'] = 'pending_human_signoff'
    destination = Path(report_path)
    protected={Path(sample).resolve(),(output/'resolution_manifest.json').resolve()}
    protected.update((output/name).resolve() for name in manifest.get('files',{}))
    protected.update(Path(name).resolve() for name in report.get('inputs',{}))
    if destination.resolve() in protected:
        raise ValueError('Review report must not overwrite source evidence or a published input/output')
    destination.parent.mkdir(parents=True, exist_ok=True)
    # This report is separate from the immutable resolution manifest and the edited CSV.
    temp = destination.with_name(destination.name+'.partial')
    temp.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temp,destination)
    return result
