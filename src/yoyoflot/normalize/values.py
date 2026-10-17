"""T12 value rules: retain source values, normalize representations, never identity."""
from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections import defaultdict
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path

import yaml

VERSION = 'canonical:1.0'
DATE_RE = re.compile(r'\d{4}-\d{2}-\d{2}')
DATETIME_RE = re.compile(r'\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?')
RU = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
              ('a','b','v','g','d','e','yo','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','kh','ts','ch','sh','shch','','y','','e','yu','ya')))


def dumps(value):
    def fallback(v):
        if isinstance(v, bytes):
            return {'bytes_hex':v.hex()}
        if isinstance(v, (date, datetime)):
            return {'iso':v.isoformat(),'python_type':type(v).__name__}
        return {'repr':repr(v),'python_type':type(v).__name__}
    try:
        return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=fallback,allow_nan=False)
    except ValueError:
        def finite(v):
            if isinstance(v,float) and not math.isfinite(v):return {'nonfinite_float':repr(v)}
            if isinstance(v,dict):return {k:finite(x) for k,x in v.items()}
            if isinstance(v,(list,tuple)):return [finite(x) for x in v]
            return v
        return json.dumps(finite(value),ensure_ascii=False,sort_keys=True,separators=(',',':'),default=fallback,allow_nan=False)


def file_hash(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for b in iter(lambda:f.read(1024*1024),b''):h.update(b)
    return h.hexdigest()


class Rules:
    def __init__(self,path):
        self.path=Path(path);self.sha256=file_hash(path)
        cfg=yaml.safe_load(self.path.read_text(encoding='utf-8'))
        if not isinstance(cfg,dict) or cfg.get('version')!='1.0':
            raise ValueError('Unsupported normalization config version')
        if cfg.get('unicode_form')!='NFKC' or cfg.get('transliteration')!='ru_ascii_v1':
            raise ValueError('Unsupported Unicode/transliteration rules')
        for by_field in cfg.get('date_formats',{}).values():
            for formats in by_field.values():
                if not isinstance(formats,list) or any(not isinstance(f,str) or not all(x in f for x in ('%Y','%m','%d')) or (('%H' in f) != ('%M' in f)) for f in formats):
                    raise ValueError('Date formats must specify full year/month/day and complete clock precision')
        self.cfg=cfg
        self.nulls=frozenset(x.casefold() if cfg.get('null_tokens_casefold') else x for x in cfg['null_tokens'])
        self.namespace_overrides={}
        for rule in cfg.get('namespace_overrides',[]):
            if not all(isinstance(rule.get(k),str) and rule[k] for k in ('source_id','kind','namespace','reason','evidence')):
                raise ValueError('Namespace overrides need source/kind/namespace/reason/evidence')
            if rule['kind'] not in ('document','ticket'):
                raise ValueError('Namespace overrides cannot bypass loyalty programme context')
            key=rule['source_id'],rule['kind']
            if key in self.namespace_overrides:raise ValueError('Duplicate namespace override')
            self.namespace_overrides[key]=rule
        self.corrections=defaultdict(list)
        self.used_corrections=set()
        ids=set()
        for rule in cfg.get('manual_corrections',[]):
            required=('rule_id','source_id','entity_table','entity_id','field','reason','evidence')
            if not all(isinstance(rule.get(k),str) and rule[k].strip() for k in required) or not {'original','replacement'} <= rule.keys():
                raise ValueError('Manual corrections require a verified original, replacement, reason and evidence')
            if rule['rule_id'] in ids:raise ValueError('Duplicate manual rule_id')
            if any(rule[k] is not None and not isinstance(rule[k],str) for k in ('original','replacement')):
                raise ValueError('Manual values must be strings or null')
            ids.add(rule['rule_id'])
            self.corrections[(rule['source_id'],rule['entity_table'],rule['entity_id'])].append(rule)

    @lru_cache(maxsize=50000)
    def clean(self,value):
        if value is None:return None
        if not isinstance(value,str):return None
        text=unicodedata.normalize('NFKC',value).strip()
        token=text.casefold() if self.cfg.get('null_tokens_casefold') else text
        return None if token in self.nulls else text

    @lru_cache(maxsize=50000)
    def name(self,value):
        value=self.clean(value)
        return ' '.join(value.split()).casefold() if value else None

    @lru_cache(maxsize=50000)
    def transliterate(self,value):
        if value is None:return None
        text=''.join(RU.get(ch,ch) for ch in value)
        # Latin accent folding only. Other alphabets remain, without guessed transliteration.
        out=[]
        for ch in text:
            if 'LATIN' in unicodedata.name(ch,''):
                out.extend(c for c in unicodedata.normalize('NFKD',ch) if not unicodedata.combining(c))
            else:out.append(ch)
        return ''.join(out)

    def formats(self,source,kind):
        cfg=self.cfg['date_formats']
        return tuple(cfg.get(source,{}).get(kind,cfg.get('default',{}).get(kind,[])))

    @lru_cache(maxsize=50000)
    def parse(self,value,formats=()):
        text=self.clean(value)
        if text is None:return None,None,None
        try:
            if DATE_RE.fullmatch(text):return date.fromisoformat(text).isoformat(),'date',None
            if DATETIME_RE.fullmatch(text):
                dt=datetime.fromisoformat(text.replace('Z','+00:00'))
                clock=text[11:]
                precision='second' if re.match(r'\d\d:\d\d:\d\d',clock) else 'minute'
                return dt.isoformat(timespec='seconds' if precision=='second' else 'minutes'),precision,None
        except ValueError:return None,'invalid','invalid_date'
        for fmt in formats:
            try:
                parsed=datetime.strptime(text,fmt)
                precision='second' if '%S' in fmt else 'minute' if '%H' in fmt else 'date'
                return (parsed.date().isoformat() if precision=='date' else parsed.isoformat(timespec='seconds' if precision=='second' else 'minutes')),precision,None
            except ValueError:pass
        return None,'invalid','invalid_or_unsupported_date_format'

    def date_value(self,source,kind,raw,parsed,precision=None):
        raw_text=self.clean(raw);parsed_text=self.clean(parsed)
        a,ap,ae=self.parse(raw_text,self.formats(source,kind))
        b,bp,be=self.parse(parsed_text,())
        if raw_text is not None:
            if ae:return None,'invalid',ae
            if parsed_text is not None and (be or a!=b or ap!=bp):
                return None,'invalid','raw_reader_date_conflict'
            result,p=a,ap
        elif parsed_text is not None:
            if be:return None,'invalid',be
            result,p=b,bp
        else:return (None,'invalid','reader_invalid_date_without_value') if precision=='invalid' else (None,None,None)
        if kind=='birth_date' and p!='date':return None,'invalid','birth_date_contains_time'
        if precision not in (None,'invalid',p):return None,'invalid','date_precision_conflict'
        return result,p,None

    def identifier(self,source,kind,value,programme):
        rule=self.cfg['identifier_rules'].get(kind)
        if rule is None:return None,None,None,None,'unsupported_identifier_kind'
        original=value
        value=self.clean(value)
        if value is None:return None,None,None,None,'missing_identifier_value'
        if not rule.get('strip_outer',True):
            # Null detection still trims, actual identifier retains its original spacing.
            value=unicodedata.normalize('NFKC',original)
        case=rule.get('case','preserve')
        if case=='upper':value=value.upper()
        elif case=='casefold':value=value.casefold()
        elif case!='preserve':raise ValueError('Unsupported identifier case rule')
        programme=self.name(programme)
        override=self.namespace_overrides.get((source,kind))
        if override:
            namespace=dumps([kind,'confirmed',override['namespace']]);status='confirmed_override'
        elif rule.get('namespace')=='programme':
            if not programme:return value,programme,None,None,'unknown_programme'
            namespace=dumps([kind,'programme',programme]);status='programme_scoped'
        elif rule.get('namespace')=='source':
            namespace=dumps([kind,'source',source]);status='source_scoped'
        else:raise ValueError('Unsupported namespace mode')
        return value,programme,namespace,dumps([namespace,value]),status

    def manual(self,table,entity_id,source,out,allowed,record):
        changed=set()
        for rule in self.corrections.get((source,table,entity_id),[]):
            field=rule['field']
            if field not in allowed:raise ValueError('Manual correction targets a forbidden field: '+field)
            before=out.get(field)
            if before!=rule['original']:raise ValueError('Manual correction original does not match: '+rule['rule_id'])
            value=rule['replacement']
            if field in ('birth_date','departure_local','arrival_local') and value is not None:
                parsed,precision,error=self.parse(value,())
                if error or (field=='birth_date' and precision!='date') or parsed!=value:
                    raise ValueError('Manual date must be valid canonical ISO')
            out[field]=value
            record(field,rule['rule_id'],before,value,'manual: '+rule['reason'])
            self.used_corrections.add(rule['rule_id'])
            changed.add(field)
        return changed
