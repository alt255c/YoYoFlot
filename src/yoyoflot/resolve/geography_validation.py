"""Validate the lossless T11 reference representation without expanding it."""
from __future__ import annotations

import duckdb
import pyarrow.parquet as pq

from ..contracts import SCHEMAS as BASE_SCHEMAS
from ..normalize.geography import (
    EXTRA_FIELDS, INVENTORY_SCHEMA, LINK_SCHEMA, META, PLACE_FIELDS,
    REFERENCE_GROUP_SCHEMA, REFERENCE_MEMBER_SCHEMA,
)

T11_TABLES = {
    'travel_geography': 'travels', 'schedule_geography': 'schedules',
    'schedule_links': 'schedule_links',
    'schedule_reference_groups': 'schedule_reference_groups',
    'schedule_reference_members': 'schedule_reference_members',
}
T11_FILES = {name + '.parquet' for name in T11_TABLES} | {
    'place_inventory.parquet', 'geography_report.json', 'geography_summary.txt',
}


def validate_reference_artifacts(files, report, db=None):
    """Check schemas, foreign keys and physical/logical counts in bounded SQL."""
    if report.get('geography_contract_version') != '2.0':
        raise ValueError('Unsupported T11 geography contract')
    schemas = {
        'schedule_links': LINK_SCHEMA,
        'schedule_reference_groups': REFERENCE_GROUP_SCHEMA,
        'schedule_reference_members': REFERENCE_MEMBER_SCHEMA,
        'place_inventory': INVENTORY_SCHEMA,
    }
    counts = report.get('counts', {})
    for table, schema in schemas.items():
        actual = pq.read_schema(files[table + '.parquet'])
        if not actual.equals(schema, check_metadata=True):
            raise ValueError('T11 reference schema mismatch: ' + table)
    for table, base, extras in (
        ('travel_geography', 'travel_observations', EXTRA_FIELDS),
        ('schedule_geography', 'schedules',
         {f'{side}_geo_{name}': typ for side in ('origin', 'destination') for name, typ in PLACE_FIELDS.items()}),
    ):
        actual = pq.read_schema(files[table + '.parquet'])
        if any((actual.metadata or {}).get(k) != v for k, v in META.items()):
            raise ValueError('T11 geography metadata mismatch: ' + table)
        fields = {f.name: f.type for f in BASE_SCHEMAS[base]}
        fields.update(extras)
        if any(name not in actual.names or actual.field(name).type != typ for name, typ in fields.items()):
            raise ValueError('T11 geography schema mismatch: ' + table)
    for table, count_key in T11_TABLES.items():
        count = counts.get(count_key)
        if type(count) is not int or count < 0 or pq.ParquetFile(files[table + '.parquet']).metadata.num_rows != count:
            raise ValueError('T11 report row count mismatch: ' + table)

    own_connection = db is None
    if own_connection:
        db = duckdb.connect()
        db.execute("SET memory_limit='1GB'")
        db.execute('SET threads=1')
    try:
        for table in T11_TABLES:
            db.read_parquet(str(files[table + '.parquet'])).create_view('gval_' + table, replace=True)

        def reject(query, message):
            if db.execute(query).fetchone()[0]:
                raise ValueError('T11 reference integrity: ' + message)

        for table, key in (('travel_geography', 'travel_id'), ('schedule_geography', 'schedule_id'),
                           ('schedule_reference_groups', 'group_id')):
            reject(f"SELECT count(*)-count(DISTINCT nullif({key},'')) FROM gval_{table}",
                   'missing or repeated ' + key)
        reject('''SELECT count(*) FROM gval_schedule_reference_groups
            WHERE member_count IS NULL OR member_count <= 0
            OR status IS DISTINCT FROM 'reference_only' OR temporal_coverage IS DISTINCT FROM 'outside_period'
            OR rule_id IS DISTINCT FROM 'airports:1.1:unique_flight_route_date_days'
            OR reason IS DISTINCT FROM 'outside_schedule_period' OR filled_fields_json IS DISTINCT FROM '{}' ''',
            'invalid outside-period group semantics')
        reject('''SELECT count(*) FROM (
            SELECT group_id,schedule_id,count(*) n FROM gval_schedule_reference_members GROUP BY ALL
            HAVING count(*)<>1 OR group_id IS NULL OR group_id='' OR schedule_id IS NULL OR schedule_id='')''',
            'missing or repeated group member')
        for child, parent, key in (
            ('schedule_reference_members', 'schedule_reference_groups', 'group_id'),
            ('schedule_reference_members', 'schedule_geography', 'schedule_id'),
            ('schedule_links', 'schedule_geography', 'schedule_id'),
            ('schedule_links', 'travel_geography', 'travel_id'),
        ):
            reject(f'SELECT count(*) FROM gval_{child} c ANTI JOIN gval_{parent} p USING({key})',
                   'orphan in ' + child + '.' + key)
        reject('''SELECT count(*) FROM gval_schedule_reference_groups g LEFT JOIN (
            SELECT group_id,count(*) n FROM gval_schedule_reference_members GROUP BY group_id) m USING(group_id)
            WHERE g.member_count IS DISTINCT FROM m.n''', 'group membership count mismatch')
        reject('''SELECT count(*) FROM gval_travel_geography t LEFT JOIN gval_schedule_reference_groups g
            ON t.schedule_reference_group_id=g.group_id
            WHERE t.schedule_reference_count IS NULL OR t.schedule_reference_count<0
            OR (t.schedule_reference_group_id IS NULL AND t.schedule_reference_count<>0)
            OR (t.schedule_reference_group_id IS NOT NULL AND
                (g.group_id IS NULL OR t.schedule_reference_count IS DISTINCT FROM g.member_count))''',
            'travel reference group/count mismatch')
        reject('''SELECT count(*) FROM gval_schedule_reference_groups g ANTI JOIN gval_travel_geography t
            ON t.schedule_reference_group_id=g.group_id''', 'unused reference group')
        reject('''SELECT count(*) FROM gval_schedule_links l JOIN gval_travel_geography t USING(travel_id)
            WHERE l.source_id IS DISTINCT FROM t.source_id
            OR l.source_record_id IS DISTINCT FROM t.source_record_id OR l.locator IS DISTINCT FROM t.locator
            OR (t.schedule_reference_group_id IS NOT NULL AND l.temporal_coverage='outside_period')''',
            'physical link provenance mismatch or duplicated compact reference')
        reject('''SELECT count(*) FROM gval_schedule_links l JOIN gval_schedule_geography s USING(schedule_id)
            WHERE l.schedule_source_id IS DISTINCT FROM s.source_id
            OR l.schedule_source_record_id IS DISTINCT FROM s.source_record_id
            OR l.schedule_locator IS DISTINCT FROM s.locator''', 'schedule link provenance mismatch')
        virtual = db.execute('SELECT coalesce(sum(schedule_reference_count),0) FROM gval_travel_geography').fetchone()[0]
        for key, expected in (('schedule_reference_associations', virtual),
                              ('logical_schedule_links', virtual + counts['schedule_links'])):
            if type(counts.get(key)) is not int or counts[key] != expected:
                raise ValueError('T11 reference integrity: ' + key + ' balance mismatch')
    finally:
        if own_connection:
            db.close()
