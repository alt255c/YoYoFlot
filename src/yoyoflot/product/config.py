"""Strict versioned configuration; no data generation or fabricated labels."""
import math
from pathlib import Path

import yaml

class UniqueLoader(yaml.SafeLoader):
    pass


def unique_mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in result:
            raise ValueError(f'Duplicate configuration key: {key}')
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, unique_mapping)

TITLES = {'M01': 'Частота перелётов', 'M02': 'Быстрые возвраты', 'M03': 'Повторяемость маршрутов'}


def load(path):
    data = yaml.load(Path(path).read_bytes(), Loader=UniqueLoader)
    if not isinstance(data, dict) or set(data) != {'version', 'criteria', 'coverage', 'forest', 'calibration'} or data['version'] != 'product-1.1':
        raise ValueError('Expected product-1.1 configuration')
    if not isinstance(data['criteria'], dict) or set(data['criteria']) != set(TITLES):
        raise ValueError('Explicit M01, M02 and M03 settings are required')
    def number(value, low, high):
        return type(value) in (int, float) and math.isfinite(value) and low <= value <= high
    for cid, rule in data['criteria'].items():
        if not isinstance(rule, dict) or set(rule) != {'threshold', 'weight', 'min_observations'}:
            raise ValueError(f'Invalid criterion {cid}')
        if not number(rule['threshold'], 0.000001, 1000000 if cid == 'M01' else 1):
            raise ValueError(f'Invalid threshold {cid}')
        if not number(rule['weight'], 0.000001, 100) or type(rule['min_observations']) is not int or not 1 <= rule['min_observations'] <= 10000:
            raise ValueError(f'Invalid weight/support {cid}')
    calibration = data['calibration']
    if (not isinstance(calibration,dict) or set(calibration)!={'mode','quantile','min_reference_rows'}
            or calibration['mode'] not in {'fixed','empirical_quantile'}
            or not number(calibration['quantile'],.5,.9999)
            or type(calibration['min_reference_rows']) is not int or not 1 <= calibration['min_reference_rows'] <= 1000000):
        raise ValueError('Invalid calibration settings')
    c = data['coverage']
    if not isinstance(c, dict) or set(c) != {'min_known_fraction', 'min_window_days'} or not number(c['min_known_fraction'], .01, 1) or type(c['min_window_days']) is not int or not 1 <= c['min_window_days'] <= 36600:
        raise ValueError('Invalid coverage settings')
    f = data['forest']
    if not isinstance(f, dict) or set(f) != {'enabled', 'n_estimators', 'max_samples', 'max_training_rows', 'random_state'} or type(f['enabled']) is not bool:
        raise ValueError('Invalid forest settings')
    for key, low, high in [('n_estimators', 1, 1000), ('max_samples', 2, 10000), ('max_training_rows', 32, 1000000), ('random_state', 0, 2**32-1)]:
        if type(f[key]) is not int or not low <= f[key] <= high:
            raise ValueError(f'Invalid forest setting {key}')
    return data
