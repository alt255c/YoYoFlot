"""T11: exact reference resolution, conservative schedule links and historical UTC.

No network, fuzzy name matching, guessed IATA, implicit airline/codeshare mapping,
or arbitrary DST fold selection. Source fields are never overwritten.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
import sys
import unicodedata
from collections import OrderedDict, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache
from importlib import resources
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

RULE_VERSION = 'airports:1.1'
FLIGHT_RE = re.compile(r'([A-Z0-9]{2}|[A-Z]{3})\s*0*(\d{1,5})([A-Z]?)')
ISO_DATE = re.compile(r'\d{4}-\d{2}-\d{2}')
ISO_DATETIME = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?')
SCHEDULE_PAYLOAD_FIELDS = ('schedule_id', 'source_id', 'source_record_id', 'locator',
    'flight_number', 'origin', 'destination', 'valid_from', 'valid_to', 'days_of_week',
    'departure_local', 'arrival_local', 'arrival_day_offset', 'parse_status', 'codeshare_mark', 'operated_by')


class CompatibleSchedules(tuple):
    """One immutable compatibility result, partitioned once for repeated trips."""
    def __new__(cls, values):
        result = super().__new__(cls, values)
        result.outside = tuple(c for c in result if c['coverage'] == 'outside_period')
        result.contenders = tuple(c for c in result if c['coverage'] != 'outside_period')
        result.reference_group_id = None
        return result


def key(value):
    return ' '.join(unicodedata.normalize('NFKC', str(value or '')).split()).casefold()


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


@lru_cache(maxsize=50000)
def candidate_ids_json(ids):
    return json_text(ids)


class AirportReference:
    def __init__(self, path, overrides=None, manifest=None):
        self.path = Path(path)
        self.sha256 = digest(path)
        self.version = self.sha256
        if manifest is not None:
            info = json.loads(Path(manifest).read_text(encoding='utf-8'))
            if info['files']['airports.csv'] != self.sha256:
                raise ValueError('Airport reference checksum mismatch')
            self.version = info['reference_version']
        self.airports = {}
        self.codes, self.names, self.cities = (defaultdict(set) for _ in range(3))
        self.countries = set()
        with open(path, encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                aid = row['airport_id']
                if aid in self.airports:
                    raise ValueError(f'Duplicate airport_id: {aid}')
                for field in ('latitude', 'longitude'):
                    row[field] = float(row[field]) if row[field] else None
                if row['latitude'] is not None and not -90 <= row['latitude'] <= 90:
                    raise ValueError('Invalid latitude')
                if row['longitude'] is not None and not -180 <= row['longitude'] <= 180:
                    raise ValueError('Invalid longitude')
                row['timezone'] = row.get('timezone') or None
                row['iata'] = row.get('iata') or None
                row['icao'] = row.get('icao') or None
                self.airports[aid] = row
                for field in ('iata', 'icao'):
                    if row[field]:
                        self.codes[key(row[field])].add(aid)
                self.names[key(row['name'])].add(aid)
                self.cities[key(row['city'])].add(aid)
                self.countries.add(key(row['country']))
        self.overrides = {}
        self.country_aliases = {}
        self.overrides_sha256 = digest(overrides) if overrides else None
        if overrides:
            config = yaml.safe_load(Path(overrides).read_text(encoding='utf-8'))
            if not isinstance(config, dict) or config.get('version') != '1.0':
                raise ValueError('Unsupported airport overrides version')
            for rule in config.get('airport_overrides', []):
                if not all(isinstance(rule.get(k), str) and rule[k].strip() for k in
                           ('source_id', 'original', 'target_iata', 'reason', 'evidence')):
                    raise ValueError('Overrides require source, original, target, reason and evidence')
                ids = self.codes.get(key(rule['target_iata']), set())
                if len(ids) != 1 or not next(iter(ids)) in self.airports:
                    raise ValueError('Override target must be a unique reference airport')
                target = self.airports[next(iter(ids))]
                if target['iata'] != rule['target_iata']:
                    raise ValueError('Override target must be an exact IATA code')
                slot = (rule['source_id'], key(rule['original']))
                if slot in self.overrides:
                    raise ValueError('Duplicate airport override')
                self.overrides[slot] = dict(rule, airport_id=target['airport_id'])
            for rule in config.get('country_aliases', []):
                if not isinstance(rule, dict) or not all(
                        isinstance(rule.get(k), str) and rule[k].strip() for k in
                        ('source_id', 'original', 'target_country', 'reason', 'evidence')):
                    raise ValueError('Country aliases require source, original, target, reason and evidence')
                original, target = key(rule['original']), key(rule['target_country'])
                if target not in self.countries:
                    raise ValueError('Country alias target must exist in the airport reference')
                if ('reference_name_variant' in rule and
                        not isinstance(rule['reference_name_variant'], bool)):
                    raise ValueError('reference_name_variant must be boolean')
                if original == target or (original in self.countries and
                                           not rule.get('reference_name_variant', False)):
                    raise ValueError('Country alias must not replace an existing reference country')
                slot = (rule['source_id'], original)
                if slot in self.country_aliases:
                    raise ValueError('Duplicate country alias')
                self.country_aliases[slot] = dict(rule)

    def _country_context(self, ids, obj, source_id, explicit_code=False):
        """Check context; aliases can validate an explicit code, never choose one."""
        ids, applied = set(ids), []
        for field in ('Country', 'country'):
            value = obj.get(field)
            if not isinstance(value, str) or not value.strip():
                continue
            token = key(value)
            rule = (self.country_aliases.get((source_id, token)) or
                    self.country_aliases.get(('*', token))) if explicit_code else None
            exact = {aid for aid in ids if key(self.airports[aid]['country']) == token}
            aliased = {aid for aid in ids if key(self.airports[aid]['country']) ==
                       key(rule['target_country'])} if rule else set()
            ids = exact | aliased
            if aliased and rule not in applied:
                applied.append(rule)
        return ids, applied if ids else []

    @staticmethod
    def _record_country_aliases(result, applied):
        if applied:
            rules = {'country_aliases': applied}
            if result['rule_id']:
                rules['airport_override'] = json.loads(result['rule_id'])
            result.update(status='corrected', reason='verified_country_alias', rule_id=json_text(rules))
        return result

    def resolve(self, raw, source_id='*'):
        return self._resolve(raw, source_id).copy()

    @lru_cache(maxsize=50000)
    def _resolve(self, raw, source_id):
        result = dict(raw=raw, kind='unknown', status='unresolved', airport_id=None,
                      iata=None, city=None, country=None, latitude=None, longitude=None,
                      timezone=None, candidates=(), reason='unknown_place', rule_id=None)
        if raw is None or not str(raw).strip():
            result.update(kind='missing', reason='missing_place')
            return result
        value = str(raw).strip()
        # Structured location objects: use only named, recognized scalar fields.
        obj = None
        if value.startswith('{'):
            try:
                obj = json.loads(value)
            except ValueError:
                pass
        if isinstance(obj, dict):
            code_values = [obj[k] for k in ('IATA', 'iata', 'ICAO', 'icao', 'AirportCode', 'airport_code')
                           if isinstance(obj.get(k), str) and obj[k].strip()]
            if code_values:
                parts = [self.resolve(v, source_id) for v in code_values]
                ids = {r['airport_id'] for r in parts}
                if None in ids or len(ids) != 1:
                    result['reason'] = 'conflicting_location_codes'
                    return result
                resolved = parts[0]
                supplied_cities = [obj[k] for k in ('City', 'city')
                                   if isinstance(obj.get(k), str) and obj[k].strip()]
                if any(key(v) != key(resolved['city']) for v in supplied_cities):
                    result['reason'] = 'conflicting_location_context'
                    return result
                ids, applied = self._country_context(ids, obj, source_id,
                    explicit_code=all(len(self.codes.get(key(v), ())) == 1 for v in code_values))
                if not ids:
                    result['reason'] = 'conflicting_location_context'
                    return result
                return self._record_country_aliases(dict(resolved, raw=raw), applied)
            value = next((obj[k] for k in ('Airport', 'airport', 'City', 'city', 'Country', 'country')
                          if isinstance(obj.get(k), str) and obj[k].strip()), '')
            if not value:
                result['reason'] = 'unsupported_location_structure'
                return result
        token = key(value)
        rule = self.overrides.get((source_id, token)) or self.overrides.get(('*', token))
        if rule:
            ids, status, reason = {rule['airport_id']}, 'corrected', rule['reason']
            result['rule_id'] = json_text(rule)
        elif token in self.codes:
            ids, status, reason = self.codes[token], 'exact', 'reference_code'
        elif token in self.names:
            ids, status, reason = self.names[token], 'exact', 'reference_airport_name'
        elif token in self.cities:
            ids, status, reason = self.cities[token], 'exact', 'unique_reference_city'
            result['kind'] = 'city'
        elif token in self.countries:
            result.update(kind='country', country=value, reason='country_is_not_airport')
            return result
        else:
            return result
        applied = []
        if isinstance(obj, dict):
            ids, applied = self._country_context(ids, obj, source_id,
                                                explicit_code=len(self.codes.get(token, ())) == 1)
        result['candidates'] = tuple(sorted(ids))
        if len(ids) != 1:
            result.update(status='ambiguous' if ids else 'unresolved', reason='multiple_airports' if ids else 'context_conflict')
            return result
        airport = self.airports[next(iter(ids))]
        result.update({k: airport[k] for k in ('airport_id', 'iata', 'city', 'country', 'latitude', 'longitude', 'timezone')})
        result.update(status=status, kind=result['kind'] if result['kind']=='city' else 'airport', reason=reason)
        return self._record_country_aliases(result, applied)


def flight_key(value, carrier=None):
    token = str(value or '').strip().upper().rstrip('*').strip()
    if carrier and re.fullmatch(r'\d{1,5}[A-Z]?', token):
        token = str(carrier).strip().upper() + token
    m = FLIGHT_RE.fullmatch(token)
    if not m or not any(c.isalpha() for c in m[1]):
        return None
    return m[1] + str(int(m[2])) + m[3]


def departure_date(row):
    value = row.get('departure_local')
    if not isinstance(value, str) or not (ISO_DATE.fullmatch(value) or ISO_DATETIME.fullmatch(value)):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


@lru_cache(maxsize=1000)
def timezone_info(name):
    if not name or any(p in ('', '.', '..') for p in name.split('/')):
        raise ValueError('Invalid IANA timezone')
    try:
        root = resources.files('tzdata.zoneinfo')
    except ModuleNotFoundError:
        return ZoneInfo(name)
    try:
        with root.joinpath(*name.split('/')).open('rb') as f:
            return ZoneInfo.from_file(f, key=name)
    except (OSError, ValueError) as exc:
        raise ZoneInfoNotFoundError(name) from exc


def local_to_utc(value, precision, tz_name):
    if not value:
        return None, 'missing_time'
    if precision not in ('minute', 'second') or not ISO_DATETIME.fullmatch(value):
        return None, 'incomplete_datetime'
    if not tz_name:
        return None, 'unknown_timezone'
    try:
        zone = timezone_info(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None, 'unknown_timezone'
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None, 'invalid_datetime'
    naive = dt.replace(tzinfo=None)
    candidates = set()
    for fold in (0, 1):
        aware = naive.replace(tzinfo=zone, fold=fold)
        utc = aware.astimezone(timezone.utc)
        if utc.astimezone(zone).replace(tzinfo=None) == naive:
            if dt.tzinfo is None or dt.utcoffset() == aware.utcoffset():
                candidates.add(utc)
    if not candidates:
        return None, 'timezone_conflict' if dt.tzinfo else 'nonexistent_local_time'
    if len(candidates) > 1:
        return None, 'ambiguous_local_time'
    return candidates.pop().isoformat().replace('+00:00', 'Z'), 'converted'


class ScheduleIndex:
    """Disk index, bulk-built indices and a byte-bounded route candidate cache."""
    CACHE_BYTES = 24 * 1024 * 1024
    EVIDENCE_BYTES = 8 * 1024 * 1024
    COMPATIBILITY_CACHE_BYTES = 32 * 1024 * 1024
    REFERENCE_CACHE_BYTES = 32 * 1024 * 1024

    def __init__(self, path, reference):
        self.db = sqlite3.connect(path)
        # Only disposable work tables live here. A failed run discards the DB;
        # published outputs and their manifest are never stored in SQLite.
        self.db.execute('PRAGMA journal_mode=OFF')
        self.db.execute('PRAGMA synchronous=OFF')
        self.db.execute('PRAGMA cache_size=-65536')
        self.db.execute('PRAGMA temp_store=FILE')
        self.reference = reference
        self.corpus_coverage = "confirmed"
        self._indexed = False
        self._cache, self._evidence = OrderedDict(), OrderedDict()
        self._cache_bytes = self._evidence_bytes = 0
        self.cache_hits = self.cache_misses = self.decoded_rows = 0
        self._compatibility = OrderedDict()
        self._compatibility_bytes = 0
        self.compatibility_cache_hits = self.compatibility_cache_misses = 0
        # Compact outside-period results retain only an on-disk group reference,
        # not the decoded schedules. This working set can span many more routes.
        self._references = OrderedDict()
        self._reference_bytes = 0
        self.reference_cache_hits = self.reference_cache_misses = 0
        self.period_start = self.period_end = None
        self.periods_known = True
        self.min_arrival_offset = self.max_arrival_offset = 0
        self.db.execute('CREATE TABLE schedules (id TEXT NOT NULL, flight TEXT, '
                        'origin_id TEXT, destination_id TEXT, parse_status TEXT, payload TEXT)')
        self.db.execute('CREATE TABLE reference_groups (group_id TEXT PRIMARY KEY, member_count INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE reference_members (group_id TEXT NOT NULL, schedule_id TEXT NOT NULL, '
                        'PRIMARY KEY(group_id,schedule_id))')

    def close(self):
        self._cache.clear()
        self._evidence.clear()
        self._compatibility.clear()
        self._references.clear()
        self.db.close()

    def add(self, row):
        payload = {k: row.get(k) for k in SCHEDULE_PAYLOAD_FIELDS}
        for side in ('origin', 'destination'):
            p = self.reference.resolve(row.get(side), row['source_id'])
            payload[side + '_airport_id'] = p['airport_id']
        if self._cache:
            self._cache.clear()
            self._cache_bytes = 0
        self._compatibility.clear()
        self._compatibility_bytes = 0
        self._references.clear()
        self._reference_bytes = 0
        if row.get('parse_status') == 'ok' and all(payload[s + '_airport_id'] for s in ('origin', 'destination')):
            offset = row.get('arrival_day_offset')
            if offset is not None:
                self.min_arrival_offset = min(self.min_arrival_offset, offset)
                self.max_arrival_offset = max(self.max_arrival_offset, offset)
            try:
                start, end = date.fromisoformat(row['valid_from']), date.fromisoformat(row['valid_to'])
                if start > end:
                    raise ValueError('reversed period')
            except (KeyError, TypeError, ValueError):
                self.periods_known = False
            else:
                self.period_start = min(start, self.period_start) if self.period_start else start
                self.period_end = max(end, self.period_end) if self.period_end else end
        self.db.execute('INSERT INTO schedules VALUES (?,?,?,?,?,?)',
                        (row['schedule_id'], flight_key(row.get('flight_number')),
                         payload['origin_airport_id'], payload['destination_airport_id'],
                         row.get('parse_status'), json_text(payload)))

    def commit(self):
        if not self._indexed:
            # Sort once, rather than perform random B-tree writes for every row.
            self.db.execute('CREATE UNIQUE INDEX schedule_id ON schedules(id)')
            self.db.execute("CREATE INDEX schedule_route ON schedules(flight,destination_id,origin_id) "
                            "WHERE parse_status='ok'")
            self._indexed = True
        self.db.commit()

    def _remember_evidence(self, sid, text):
        if sid in self._evidence:
            self._evidence.move_to_end(sid)
            return
        size = len(text.encode('utf-8')) + len(sid) + 64
        if size > self.EVIDENCE_BYTES:
            return
        self._evidence[sid] = (text, size)
        self._evidence_bytes += size
        while self._evidence_bytes > self.EVIDENCE_BYTES:
            _, (_, weight) = self._evidence.popitem(last=False)
            self._evidence_bytes -= weight

    def evidence_json(self, schedule):
        sid = schedule['schedule_id']
        if sid in self._evidence:
            self._evidence.move_to_end(sid)
            return self._evidence[sid][0]
        text = json_text(schedule)
        self._remember_evidence(sid, text)
        return text

    def candidates(self, primary, codeshare, origins=(), destinations=()):
        keys = tuple(sorted({v for v in (primary, codeshare) if v}))
        if not keys:
            return ()
        cache_key = (keys, tuple(origins), tuple(destinations))
        if cache_key in self._cache:
            self.cache_hits += 1
            self._cache.move_to_end(cache_key)
            return self._cache[cache_key][0]
        self.cache_misses += 1
        if not self._indexed:
            self.commit()
        clauses = ["parse_status='ok'", 'origin_id IS NOT NULL', 'destination_id IS NOT NULL',
                   'flight IN (' + ','.join('?' for _ in keys) + ')']
        params = list(keys)
        for column, ids in (('origin_id', origins), ('destination_id', destinations)):
            if ids:
                clauses.append(column + ' IN (' + ','.join('?' for _ in ids) + ')')
                params.extend(ids)
        # All predicates are the same exact-place checks as the Python matcher;
        # periods/days/clocks and ambiguity remain checked below without changes.
        query = 'SELECT payload FROM schedules WHERE ' + ' AND '.join(clauses) + ' ORDER BY id'
        result, weight = [], len(repr(cache_key).encode('utf-8')) + 128
        for (text,) in self.db.execute(query, params):
            item = json.loads(text)
            self.decoded_rows += 1
            result.append(item)
            weight += len(text.encode('utf-8')) + 64
            self._remember_evidence(item['schedule_id'], text)
        result = tuple(result)
        if weight <= self.CACHE_BYTES:
            self._cache[cache_key] = (result, weight)
            self._cache_bytes += weight
            while self._cache_bytes > self.CACHE_BYTES:
                _, (_, size) = self._cache.popitem(last=False)
                self._cache_bytes -= size
        return result

    def outside_cache_key(self, row, places, primary, alias, day):
        # A single unknown/reversed relevant period disables this optimization.
        # Such a schedule remains a contender even when every known period is later.
        if (not self.periods_known or day is None or self.period_start is None
                or self.period_start <= day <= self.period_end):
            return None
        clocks = []
        for field in ('departure_local', 'arrival_local'):
            value = row.get(field)
            clock = None
            if value and ISO_DATETIME.fullmatch(value):
                try:
                    clock = datetime.fromisoformat(value.replace('Z', '+00:00')).time().replace(tzinfo=None).isoformat()
                except ValueError:
                    clock = 'invalid_clock'
            clocks.append(clock)
        arrival = row.get('arrival_local')
        offset = None
        if arrival and (ISO_DATE.fullmatch(arrival) or ISO_DATETIME.fullmatch(arrival)):
            try:
                day + timedelta(days=self.min_arrival_offset)
                day + timedelta(days=self.max_arrival_offset)
            except OverflowError:
                return None
            try:
                offset = (date.fromisoformat(arrival[:10]) - day).days
            except ValueError:
                offset = 'invalid_arrival_date'
        return (tuple(sorted({v for v in (primary, alias) if v})),
                tuple(places['origin']['candidates']), tuple(places['destination']['candidates']),
                tuple(clocks), offset)

    def remember_compatibility(self, key, result):
        # Include all retained schedules in the budget, even when the route cache
        # also refers to them. Evicting that cache must not make this one unbounded.
        size = len(repr(key).encode('utf-8')) + 256
        size += sum(len(json_text(c).encode('utf-8')) + 256 for c in result)
        if size > self.COMPATIBILITY_CACHE_BYTES:
            return
        previous = self._compatibility.pop(key, None)
        if previous is not None:
            self._compatibility_bytes -= previous[1]
        self._compatibility[key] = (result, size)
        self._compatibility_bytes += size
        while self._compatibility_bytes > self.COMPATIBILITY_CACHE_BYTES:
            _, (_, weight) = self._compatibility.popitem(last=False)
            self._compatibility_bytes -= weight

    def cached_reference(self, key):
        entry = self._references.get(key)
        if entry is None:
            self.reference_cache_misses += 1
            return None
        self.reference_cache_hits += 1
        self._references.move_to_end(key)
        return entry[0]

    def remember_reference(self, key, descriptor):
        # Count the retained tuple/string objects, including shared objects each
        # time, plus cache-entry overhead. No schedule payload is retained here.
        size, pending = 256, [key, descriptor]
        while pending:
            item = pending.pop()
            size += sys.getsizeof(item)
            if isinstance(item, tuple):
                pending.extend(item)
        if size > self.REFERENCE_CACHE_BYTES:
            return
        previous = self._references.pop(key, None)
        if previous is not None:
            self._reference_bytes -= previous[1]
        self._references[key] = (descriptor, size)
        self._reference_bytes += size
        while self._reference_bytes > self.REFERENCE_CACHE_BYTES:
            _, (_, weight) = self._references.popitem(last=False)
            self._reference_bytes -= weight

    def reference_group(self, candidates):
        if not candidates.outside:
            return None
        if candidates.reference_group_id is None:
            ids = [c['schedule']['schedule_id'] for c in candidates.outside]
            group_id = hashlib.sha256(json_text(ids).encode('utf-8')).hexdigest()
            inserted = self.db.execute('INSERT OR IGNORE INTO reference_groups VALUES (?,?)', (group_id, len(ids))).rowcount
            if inserted:
                self.db.executemany('INSERT INTO reference_members VALUES (?,?)', ((group_id, sid) for sid in ids))
            candidates.reference_group_id = group_id
        return candidates.reference_group_id


def compatible_candidates(row, places, index, *, cache=True):
    primary = flight_key(row.get('flight_number_normalized',row.get('flight_number')), row.get('carrier'))
    alias = flight_key(row.get('codeshare'))  # A boolean/star/number is not an alias.
    day = departure_date(row)
    found = []
    if any(p['kind'] != 'missing' and not p['candidates'] for p in places.values()):
        return CompatibleSchedules(())
    cache_key = index.outside_cache_key(row, places, primary, alias, day) if cache else None
    if cache_key is not None:
        if cache_key in index._compatibility:
            index.compatibility_cache_hits += 1
            index._compatibility.move_to_end(cache_key)
            return index._compatibility[cache_key][0]
        index.compatibility_cache_misses += 1
    for schedule in index.candidates(primary, alias, places['origin']['candidates'], places['destination']['candidates']):
        if schedule['parse_status'] != 'ok':
            continue
        if any(p['candidates'] and schedule[side + '_airport_id'] not in p['candidates']
               for side, p in places.items()):
            continue
        if not all(schedule[s + '_airport_id'] for s in ('origin', 'destination')):
            continue
        # Unknown raw places cannot be silently ignored in order to infer a route.
        if any(p['kind'] not in ('missing',) and not p['candidates'] for p in places.values()):
            continue
        coverage, reason = 'confirmed', 'date_days_route_flight'
        try:
            start = date.fromisoformat(schedule['valid_from'])
            end = date.fromisoformat(schedule['valid_to'])
            if start > end:
                raise ValueError('reversed period')
        except (TypeError, ValueError):
            coverage, reason = 'unknown', 'unknown_schedule_period'
        else:
            if day is None:
                coverage, reason = 'unknown', 'unknown_observation_date'
            elif not start <= day <= end:
                coverage, reason = 'outside_period', 'outside_schedule_period'
        days = str(schedule.get('days_of_week') or '')
        if coverage == 'confirmed':
            if not days or not re.fullmatch('[1-7]+', days):
                coverage, reason = 'unknown', 'unknown_days_of_week'
            # SkyTeam's original legend (PDF page 3) numbers Sunday as 1
            # through Saturday as 7. The reader preserves those source digits.
            elif str(day.isoweekday() % 7 + 1) not in days:
                continue
        # An observed clock value must agree. Do not infer a date change from clocks.
        conflict = False
        for field in ('departure_local', 'arrival_local'):
            value, expected = row.get(field), schedule.get(field)
            if value and ISO_DATETIME.fullmatch(value) and expected:
                try:
                    if datetime.fromisoformat(value.replace('Z', '+00:00')).time().replace(tzinfo=None) != time.fromisoformat(expected):
                        conflict = True
                except ValueError:
                    conflict = True
        if conflict:
            continue
        if row.get('arrival_local') and (ISO_DATE.fullmatch(row['arrival_local']) or ISO_DATETIME.fullmatch(row['arrival_local'])) and day and schedule.get('arrival_day_offset') is not None:
            if row['arrival_local'][:10] != (day + timedelta(days=schedule['arrival_day_offset'])).isoformat():
                continue
        if coverage == 'confirmed' and index.corpus_coverage != 'confirmed':
            coverage, reason = 'unknown', 'schedule_source_coverage_unknown'
        found.append(dict(schedule=schedule, coverage=coverage, reason=reason))
    result = CompatibleSchedules(found)
    if cache_key is not None:
        index.remember_compatibility(cache_key, result)
    return result


def enrich_travel(row, reference, index, compact=False):
    out = dict(row)
    places = {s: reference.resolve(row.get(s + '_raw'), row['source_id']) for s in ('origin', 'destination')}
    canonical_rules = json.loads(row.get('normalization_rules_json') or '{}')
    for side, place in places.items():
        if canonical_rules.get(side+'_raw') == 'type.incompatible':
            place.update(kind='unknown',status='unresolved',reason='incompatible_source_location',candidates=(),airport_id=None)
    reference_descriptor, reference_key = None, None
    if compact and not any(p['kind'] != 'missing' and not p['candidates'] for p in places.values()):
        primary = flight_key(row.get('flight_number_normalized', row.get('flight_number')), row.get('carrier'))
        alias = flight_key(row.get('codeshare'))
        reference_key = index.outside_cache_key(row, places, primary, alias, departure_date(row))
        if reference_key is not None:
            reference_descriptor = index.cached_reference(reference_key)
    if reference_descriptor is None:
        # A descriptor miss still uses the full conservative matcher. Persist its
        # exact memberships before caching; unknown periods never take this path.
        candidates = compatible_candidates(row, places, index, cache=reference_key is None)
        if compact:
            reference_descriptor = (index.reference_group(candidates), len(candidates.outside))
            if reference_key is not None:
                if candidates.contenders:
                    raise ValueError('Outside-period cache proof admitted a temporal contender')
                index.remember_reference(reference_key, reference_descriptor)
    else:
        candidates = CompatibleSchedules(())
    # Unknown-period candidates remain contenders: they can defeat uniqueness.
    contenders = candidates.contenders
    chosen = contenders[0] if len(contenders) == 1 and contenders[0]['coverage'] == 'confirmed' else None
    filled = {}
    day = departure_date(row)
    if chosen:
        schedule = chosen['schedule']
        for side, place in places.items():
            if place['kind'] == 'missing':
                aid = schedule[side + '_airport_id']
                airport = reference.airports[aid]
                places[side] = dict(place, **{k:airport[k] for k in ('airport_id','iata','city','country','latitude','longitude','timezone')},
                                    status='inferred_from_schedule', reason='unique_temporally_compatible_schedule')
                filled[side + '_airport_id'] = aid
    for label, side in (('departure', 'origin'), ('arrival', 'destination')):
        value, precision = row.get(label + '_local'), row.get(label + '_precision')
        effective, effective_precision, time_origin = value, precision, 'source' if value else 'missing'
        if chosen and day is not None and precision != 'invalid' and (not value or precision == 'date'):
            schedule = chosen['schedule']
            clock = schedule.get(label + '_local')
            offset = 0 if label == 'departure' else schedule.get('arrival_day_offset')
            # A supplied date must agree with the explicit schedule day offset.
            if clock and offset is not None:
                try:
                    inferred_day = day + timedelta(days=offset)
                    if value and value != inferred_day.isoformat():
                        raise ValueError('source date conflict')
                    parsed_clock = time.fromisoformat(clock)
                    if parsed_clock.tzinfo is not None:
                        raise ValueError('schedule time must be local')
                    effective = datetime.combine(inferred_day, parsed_clock).isoformat(timespec='minutes')
                    effective_precision, time_origin = 'minute', 'inferred_from_schedule'
                    filled[label + '_effective_local'] = effective
                except ValueError:
                    pass
        airport_tz = places[side]['timezone']
        source_tz = row.get(label + '_timezone')
        if source_tz and airport_tz and source_tz != airport_tz:
            utc, status = None, 'timezone_conflict'
        elif places[side]['airport_id'] is None:
            utc, status = None, 'unknown_airport'
        else:
            utc, status = local_to_utc(effective, effective_precision, airport_tz)
        out.update({label + '_effective_local': effective, label + '_effective_precision': effective_precision,
                    label + '_time_origin': time_origin, label + '_utc': utc,
                    label + '_utc_status': status, label + '_iana_timezone': airport_tz})
    for side, place in places.items():
        for field in ('airport_id', 'iata', 'city', 'country', 'latitude', 'longitude', 'timezone', 'status', 'kind', 'reason'):
            out[side + '_' + field] = place[field]
        out[side + '_candidates_json'] = candidate_ids_json(place['candidates'])
        out[side + '_override_json'] = place['rule_id']
    out['schedule_match_status'] = ('matched' if chosen else 'ambiguous' if len(contenders)>1 else
                                    'temporal_unknown' if contenders else 'reference_only' if candidates or
                                    (reference_descriptor is not None and reference_descriptor[1]) else 'unresolved')
    out['schedule_id_used'] = chosen['schedule']['schedule_id'] if chosen else None
    out['geography_rule_version'] = RULE_VERSION
    out['geography_reference_version'] = reference.version
    out['inferred_fields_json'] = json_text(filled)
    if compact:
        out['schedule_reference_group_id'], out['schedule_reference_count'] = reference_descriptor
    links = []
    for c in contenders if compact else candidates:
        s = c['schedule']
        status = ('reference_only' if c['coverage']=='outside_period' else 'ambiguous' if len(contenders)>1 else
                  'temporal_unknown' if c['coverage']=='unknown' else 'matched')
        links.append(dict(travel_id=row['travel_id'], source_id=row['source_id'], source_record_id=row['source_record_id'],
                          locator=row['locator'], schedule_id=s['schedule_id'], schedule_source_id=s['source_id'],
                          schedule_source_record_id=s['source_record_id'], schedule_locator=s['locator'],
                          status=status, temporal_coverage=c['coverage'], rule_id=RULE_VERSION + ':unique_flight_route_date_days',
                          reason=c['reason'], filled_fields_json=json_text(filled if chosen and c is chosen else {}),
                          evidence_json=index.evidence_json(s)))
    return out, links
