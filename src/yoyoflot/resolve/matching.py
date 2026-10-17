"""Pair decisions and component-wide constraints; no unconstrained union-find."""
from __future__ import annotations

import json
from array import array
from datetime import datetime
from difflib import SequenceMatcher
from functools import lru_cache


@lru_cache(maxsize=50000)
def documents(text):
    values = {}
    for item in json.loads(text or '[]'):
        values.setdefault(item['namespace'], set()).add(item['value'])
    return tuple(sorted((ns, frozenset(v)) for ns, v in values.items()))


def document_conflict(a, b):
    return any(not a[ns] & b[ns] for ns in a.keys() & b.keys())


def name_similarity(a, b):
    if a.get('name_comparison') and a['name_comparison'] == b.get('name_comparison'):
        return 1.0
    left = a.get('name_latin_comparison') or a.get('name_comparison')
    right = b.get('name_latin_comparison') or b.get('name_comparison')
    if not left or not right:
        return None
    scores=[]
    for a,b in [(left,right),(' '.join(sorted(left.split())),' '.join(sorted(right.split())))]:
        a,b=sorted((a,b))
        scores.append(SequenceMatcher(None,a,b,autojunk=False).ratio())
    return max(scores)


def assess_pair(left, right, signals, rules):
    similarity = name_similarity(left, right)
    same_dob = bool(left.get('birth_date') and left['birth_date'] == right.get('birth_date'))
    features = dict(name_similarity=similarity, same_birth_date=same_dob,
                    document_match=bool(signals.get('document_match')),
                    card_match=bool(signals.get('card_match')),
                    source_document_match=bool(signals.get('source_document_match')),
                    name_dob_block=bool(signals.get('name_dob_match')),
                    contaminated_identifier=bool(signals.get('contaminated')))
    if left.get('birth_date') and right.get('birth_date') and not same_dob:
        return 'rejected', 'birth_date_conflict', features
    if document_conflict(dict(documents(left.get('documents_json'))), dict(documents(right.get('documents_json')))):
        return 'rejected', 'document_context_conflict', features
    if similarity is not None and similarity < rules['name_conflict_threshold']:
        return 'rejected', 'name_conflict', features
    strong = features['document_match'] or features['card_match']
    if strong:
        if similarity is not None and similarity < rules['name_similarity_threshold']:
            return 'ambiguous', 'identifier_with_uncertain_name', features
        return 'accepted', 'confirmed_document' if features['document_match'] else 'programme_card', features
    if features['source_document_match']:
        if same_dob and similarity is not None and similarity >= rules['name_similarity_threshold']:
            return 'accepted', 'source_document_name_birth_date', features
        return 'ambiguous', 'unconfirmed_document_context', features
    if features['contaminated_identifier']:
        return 'ambiguous', 'contaminated_identifier', features
    if similarity is not None and similarity < rules['name_conflict_threshold']:
        return 'rejected', 'weak_name_similarity', features
    return 'ambiguous', 'name_birth_date_only', features


class Components:
    """~13 bytes/node plus DOB array; document state only for touched roots."""
    def __init__(self, count, dobs, limit, max_name_variants=32, name_threshold=0.90, name_conflict_threshold=0.45):
        self.parent = array('I', range(count))
        self.size = array('I', [1]) * count
        self.dobs = dobs
        self.uncertain = bytearray(count)
        self.constraints = {}
        self.names = {}
        self.limit = limit
        self.max_name_variants = max_name_variants
        self.name_threshold, self.name_conflict_threshold = name_threshold, name_conflict_threshold

    def find(self, node):
        root = node
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[node] != node:
            next_node = self.parent[node]
            self.parent[node] = root
            node = next_node
        return root

    def mark(self, a, b):
        self.uncertain[self.find(a)] = self.uncertain[self.find(b)] = 1

    def merge(self, a, b, a_documents, b_documents, a_person=None, b_person=None):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return None
        da = self.constraints.get(ra, dict(documents(a_documents)))
        db = self.constraints.get(rb, dict(documents(b_documents)))
        if self.dobs[ra] and self.dobs[rb] and self.dobs[ra] != self.dobs[rb]:
            self.mark(a, b)
            return 'component_birth_date_conflict'
        if document_conflict(da, db):
            self.mark(a, b)
            return 'component_document_conflict'
        if self.size[ra] + self.size[rb] > self.limit:
            self.mark(a, b)
            return 'component_size_limit'
        def original_names(person):
            if not person or not (person.get('name_comparison') or person.get('name_latin_comparison')):
                return frozenset()
            return frozenset([(person.get('name_comparison'),person.get('name_latin_comparison'))])
        na = self.names.get(ra,original_names(a_person))
        nb = self.names.get(rb,original_names(b_person))
        if len(na|nb)>self.max_name_variants:
            self.mark(a,b)
            return 'component_name_variant_limit'
        for name_a in na:
            for name_b in nb:
                score = name_similarity(dict(zip(('name_comparison','name_latin_comparison'),name_a)),
                                        dict(zip(('name_comparison','name_latin_comparison'),name_b)))
                if score is not None and score<self.name_threshold:
                    self.mark(a,b)
                    return 'component_name_conflict' if score<self.name_conflict_threshold else 'component_name_uncertain'
        merged = {**da, **db}
        for ns in da.keys() & db.keys():
            merged[ns] = da[ns] & db[ns]
        root, other = sorted((ra, rb))
        self.parent[other] = root
        self.size[root] += self.size[other]
        self.dobs[root] = self.dobs[ra] or self.dobs[rb]
        self.uncertain[root] |= self.uncertain[other]
        self.constraints.pop(other, None)
        if merged:
            self.constraints[root] = merged
        self.names.pop(other,None)
        if na or nb:
            self.names[root] = na|nb
        return None


def time_interval(value, precision):
    if not value or precision not in ('date', 'minute', 'second'):
        return None
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo:
        from datetime import timezone
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    start = dt.toordinal()*86400 + dt.hour*3600 + dt.minute*60 + dt.second + dt.microsecond/1000000
    return start, start + {'date':86400, 'minute':60, 'second':1}[precision]


def trip_constraints(row):
    result = dict(origin=row.get('origin_id'), destination=row.get('destination_id'),
                  tickets={row['ticket_namespace']: row['ticket_value']}
                  if row.get('ticket_namespace') and row.get('ticket_value') else {})
    for side in ('departure', 'arrival'):
        value, precision = row.get(side+'_local'), row.get(side+'_precision')
        # Local offsets describe a local clock here; UTC is checked independently.
        if value:
            dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
            value = dt.replace(tzinfo=None).isoformat() if precision != 'date' else value[:10]
        result[side] = time_interval(value, precision)
        result[side+'_utc'] = time_interval(row.get(side+'_utc'), precision)
    return result


def trip_compatible(a, b):
    for field in ('origin', 'destination'):
        if a[field] and b[field] and a[field] != b[field]:
            return 'route_incompatible'
    for field in ('departure', 'arrival', 'departure_utc', 'arrival_utc'):
        if a[field] and b[field] and max(a[field][0], b[field][0]) >= min(a[field][1], b[field][1]):
            return field+'_conflict'
    for ns in a['tickets'].keys() & b['tickets'].keys():
        if a['tickets'][ns] != b['tickets'][ns]:
            return 'ticket_context_conflict'
    full_route = all(a[k] and b[k] for k in ('origin', 'destination'))
    shared_ticket = any(a['tickets'][ns] == b['tickets'][ns] for ns in a['tickets'].keys() & b['tickets'].keys())
    if not full_route and not shared_ticket:
        return 'partial_route_without_shared_ticket'
    return None


def merge_trip_constraints(a, b):
    result = dict(origin=a['origin'] or b['origin'], destination=a['destination'] or b['destination'],
                  tickets={**a['tickets'], **b['tickets']})
    for field in ('departure', 'arrival', 'departure_utc', 'arrival_utc'):
        x, y = a[field], b[field]
        result[field] = (max(x[0],y[0]), min(x[1],y[1])) if x and y else x or y
    return result


def cluster_trips(records):
    """Most specific witnesses first; incomplete rows cannot bridge two events."""
    rank = {'second':2, 'minute':1, 'date':0}
    records = sorted(records, key=lambda r: (-int(bool(r['origin_id'] and r['destination_id'])),
                     -rank.get(r['departure_precision'], -1), -int(bool(r.get('ticket_value'))), r['travel_id']))
    clusters, conflicts = [], []
    for row in records:
        constraints = trip_constraints(row)
        compatible = []
        for cluster in clusters:
            reason = trip_compatible(cluster['constraints'], constraints)
            if reason is None:
                compatible.append(cluster)
            elif reason != 'partial_route_without_shared_ticket':
                conflicts.append((row['travel_id'], cluster['members'][0]['travel_id'], reason))
        if len(compatible) == 1:
            cluster = compatible[0]
            cluster['constraints'] = merge_trip_constraints(cluster['constraints'], constraints)
            cluster['members'].append(row)
        else:
            clusters.append(dict(constraints=constraints, members=[row],
                                 ambiguous=len(compatible)>1 or not all(constraints[k] for k in ('origin','destination')),
                                 reason='multiple_compatible_events' if len(compatible)>1 else 'incomplete_route'
                                 if not all(constraints[k] for k in ('origin','destination')) else 'observed_event'))
    assignments = []
    for cluster in clusters:
        anchor = min(r['travel_id'] for r in cluster['members'])
        ambiguous = cluster['ambiguous'] and not all(cluster['constraints'][k] for k in ('origin','destination'))
        # A row compatible with several events stays ambiguous even with its own full route.
        ambiguous |= cluster['reason'] == 'multiple_compatible_events'
        for row in cluster['members']:
            assignments.append(dict(travel_id=row['travel_id'], anchor_travel_id=anchor,
                                    status='ambiguous' if ambiguous else 'accepted',
                                    rule_id='segment.shared_ticket' if len(cluster['members'])>1 and
                                    any(not (r['origin_id'] and r['destination_id']) for r in cluster['members'])
                                    else 'segment.compatible_observed_event' if len(cluster['members'])>1 else 'segment.source_record',
                                    reason=cluster['reason']))
    return assignments, conflicts
