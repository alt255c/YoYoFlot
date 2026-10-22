"""Cosmetic map characters; name hints never populate sex or affect scoring."""
import re
import unicodedata

from ..normalize.values import RU


def name_tokens(value):
    text=unicodedata.normalize('NFKC',value or '').casefold()
    text=''.join(RU.get(char,char) for char in text)
    text=''.join(char for char in unicodedata.normalize('NFKD',text)
                 if not unicodedata.combining(char))
    return set(re.findall(r'[a-z]+',text))


def load_name_hints(db, observations):
    """Explicit given names with one observed sex and 3+ name/DOB combinations."""
    if observations is None:return {}
    rows=db.execute('''SELECT json_extract_string(raw_json,'$.PassengerFirstName') given_name,
        sex,count(DISTINCT (name_latin_comparison,birth_date)) people
        FROM read_parquet(?) WHERE sex IN ('Female','Male')
            AND json_extract_string(raw_json,'$.PassengerFirstName') IS NOT NULL
        GROUP BY 1,2''',[str(observations)]).fetchall()
    names={}
    for given_name,sex,people in rows:
        tokens=name_tokens(given_name)
        if len(tokens)!=1:continue
        token=next(iter(tokens))
        entry=names.setdefault(token,{'sexes':set(),'people':0})
        entry['sexes'].add(sex);entry['people']+=people
    return {name:next(iter(entry['sexes'])) for name,entry in names.items()
            if len(entry['sexes'])==1 and entry['people']>=3}


def choose_mascot(person,hints):
    if person.get('sex') in ('Female','Male'):
        return {'kind':'fly' if person['sex']=='Female' else 'mosquito','basis':'recorded_sex'}
    if person.get('sex_status')=='conflict':
        return {'kind':'mosquito','basis':'default_conflict'}
    found={hints[token] for token in name_tokens(person.get('display_name')) if token in hints}
    if len(found)==1:
        return {'kind':'fly' if found=={'Female'} else 'mosquito','basis':'given_name_hint'}
    return {'kind':'mosquito','basis':'default_unknown'}
