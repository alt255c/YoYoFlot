"""Versioned output schemas, bounded writers and progress for T13."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import yaml

from . import VERSION
from ..normalize.values import file_hash

META = {b'resolution_contract_version': b'1.0', b'resolution_version': VERSION.encode()}


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def stable_id(kind, *values):
    return kind + '_' + hashlib.sha256(dumps([kind, '1.0', *values]).encode()).hexdigest()


def schema(strings, integers=(), booleans=(), floats=()):
    return pa.schema([(n, pa.string()) for n in strings.split()] +
                     [(n, pa.int64()) for n in integers] +
                     [(n, pa.bool_()) for n in booleans] +
                     [(n, pa.float64()) for n in floats], metadata=META)


SCHEMAS = {
    'persons': schema('person_id anchor_observation_id display_name birth_date identity_status rule_id',
                      ('observation_count', 'source_count'), ('graph_eligible',)),
    'person_links': schema('observation_id person_id source_id source_record_id locator status confidence rule_id',
                           integers=('node_index',), booleans=('graph_eligible',)),
    'person_pairs': schema('pair_id left_observation_id right_observation_id status rule_id reason '
                          'features_json evidence_json', floats=('name_similarity',)),
    'segments': schema('segment_id person_id anchor_travel_id departure_date flight_number '
                      'origin_airport_id destination_airport_id origin_iata destination_iata '
                      'departure_local departure_precision arrival_local arrival_precision '
                      'departure_utc arrival_utc identity_status resolution_status participation_status '
                      'event_kinds_json sources_json rule_id', ('observation_count', 'source_count'),
                      ('graph_eligible', 'geography_complete', 'time_complete')),
    'segment_links': schema('travel_id segment_id person_id observation_id source_id source_record_id locator '
                           'event_kind status_raw status rule_id reason departure_precision arrival_precision '
                           'origin_status destination_status departure_utc_status arrival_utc_status '
                           'departure_time_origin arrival_time_origin codeshare_rule_id', booleans=('graph_eligible',)),
    'conflicts': schema('conflict_id entity_kind left_id right_id rule_id reason severity details_json'),
    'blocking_issues': schema('block_id kind reason key_hash', ('member_count',)),
}


class Rules:
    def __init__(self, path):
        self.path = Path(path)
        self.sha256 = file_hash(path)
        cfg = yaml.safe_load(self.path.read_text(encoding='utf-8'))
        if not isinstance(cfg, dict) or cfg.get('version') != '1.0':
            raise ValueError('Unsupported resolution config version')
        expected = {'version', 'name_similarity_threshold', 'name_conflict_threshold',
                    'pairwise_block_size', 'max_name_block_size', 'max_candidates',
                    'max_component_size', 'max_name_variants', 'max_trip_group_size', 'source_document_requires_name_and_dob',
                    'graph_event_kinds', 'review_sample', 'review_seed', 'codeshare_rules'}
        if set(cfg) - expected:
            raise ValueError('Unknown resolution settings: ' + ', '.join(sorted(set(cfg) - expected)))
        for key in ('pairwise_block_size', 'max_name_block_size', 'max_candidates',
                    'max_component_size', 'max_name_variants', 'max_trip_group_size'):
            if type(cfg.get(key)) is not int or cfg[key] < 2:
                raise ValueError('Resolution limit must be an integer >= 2: ' + key)
        for key in ('name_similarity_threshold', 'name_conflict_threshold'):
            if type(cfg.get(key)) not in (float, int) or not 0 <= cfg[key] <= 1:
                raise ValueError('Invalid similarity threshold: ' + key)
        if cfg['name_conflict_threshold'] >= cfg['name_similarity_threshold']:
            raise ValueError('Name conflict threshold must be below acceptance threshold')
        if cfg.get('source_document_requires_name_and_dob') is not True:
            raise ValueError('Unconfirmed source documents must require independent name and DOB support')
        if (not isinstance(cfg.get('graph_event_kinds'), list) or
                not all(isinstance(v, str) and v for v in cfg['graph_event_kinds'])):
            raise ValueError('graph_event_kinds must be a list of source event kinds')
        if not isinstance(cfg.get('review_seed'), str) or not cfg['review_seed']:
            raise ValueError('review_seed is required')
        if (set(cfg.get('review_sample', {})) != {'accepted', 'ambiguous', 'rejected'} or
                any(type(v) is not int or v < 0 for v in cfg['review_sample'].values())):
            raise ValueError('Invalid review sample quotas')
        if not isinstance(cfg.get('codeshare_rules'), list):
            raise ValueError('codeshare_rules must be a list')
        self.config = cfg

    def __getitem__(self, key):
        return self.config[key]


class Writer:
    def __init__(self, path, table, batch_size=4000):
        self.schema = SCHEMAS[table]
        self.writer = pq.ParquetWriter(path, self.schema, compression='zstd')
        self.buffer = []
        self.batch_size = batch_size
        self.count = 0

    def add(self, row):
        self.buffer.append(row)
        self.count += 1
        if len(self.buffer) >= self.batch_size:
            self.flush()

    def flush(self):
        if self.buffer:
            self.writer.write_table(pa.Table.from_pylist(self.buffer, schema=self.schema))
            self.buffer.clear()

    def close(self):
        self.flush()
        self.writer.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class Progress:
    """A heartbeat keeps long native SQL operations distinguishable from a hang."""
    def __init__(self, callback=None, interval=30):
        self.callback, self.interval = callback, interval
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.name, self.current, self.total, self.started = '', 0, 0, time.monotonic()
        self.thread = None

    def phase(self, name, total=0):
        with self.lock:
            self.name, self.current, self.total, self.started = name, 0, total, time.monotonic()
        if self.callback:
            self.callback('T13: ' + name)

    def tick(self, count):
        with self.lock:
            self.current = count

    def _heartbeat(self):
        while not self.stop.wait(self.interval):
            with self.lock:
                name, n, total, seconds = self.name, self.current, self.total, time.monotonic()-self.started
            message = f'T13: {name} — {seconds:.0f} с'
            if total:
                message += f', {n} / {total}'
                if n:
                    message += f', осталось на этом этапе ≈ {(total-n)*seconds/n:.0f} с'
            self.callback(message)

    def __enter__(self):
        if self.callback:
            self.thread = threading.Thread(target=self._heartbeat, daemon=True)
            self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)


def arrow_query(db, query, path, table, batch_size=4000, params=None):
    with pq.ParquetWriter(path, SCHEMAS[table], compression='zstd') as writer:
        cursor = db.execute(query, params or [])
        reader = getattr(cursor, 'to_arrow_reader', cursor.fetch_record_batch)(batch_size)
        for batch in reader:
            writer.write_table(pa.Table.from_batches([batch]).cast(SCHEMAS[table]))


AGGREGATION_GROUPS = 16384


def aggregate_ranges(db, table, query, group_count, params=(), progress=None):
    """Bound non-spillable aggregate states by a sorted numeric group range.

    The caller materializes input ordered by the numeric key used by the last
    two query parameters. A group must belong to exactly one range; splitting
    a group would change DISTINCT, counts and the evidence being aggregated.
    """
    db.execute('CREATE TABLE '+table+' AS '+query, [*params, 0, 0])
    for start in range(0, group_count, AGGREGATION_GROUPS):
        end = min(group_count, start+AGGREGATION_GROUPS)
        db.execute('INSERT INTO '+table+' '+query, [*params, start, end])
        if progress:
            progress.tick(end)


def conflict(writer, kind, left, right, reason, details=None, severity='warning'):
    writer.add(dict(conflict_id=stable_id('conflict', kind, left, right, reason), entity_kind=kind,
                    left_id=left, right_id=right, rule_id=VERSION+':'+reason,
                    reason=reason, severity=severity, details_json=dumps(details or {})))
