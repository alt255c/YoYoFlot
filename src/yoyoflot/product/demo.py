"""A fast demonstration view over the real active product snapshot.

The demo never invents people, journeys or labels. It simply selects a small
number of real ranked profiles from the active source-dataset product so a
presentation can start quickly and still show the exact production evidence.
"""
from copy import deepcopy


class DemoSubsetStore:
    data_mode = 'demo_subset'

    def __init__(self, source, size=5):
        if getattr(source, 'data_mode', None) != 'source_dataset':
            raise ValueError('Demo subset requires a source-dataset product')
        self.source = source
        self.ids = [row['person_id'] for row in source.top(size)]

    def metadata(self):
        metadata=deepcopy(self.source.metadata())
        metadata['data_mode']=self.data_mode
        metadata['run_id']=f"{metadata.get('run_id','source')}:demo"
        metadata['ui']={'product_name':'YoYoFlot Sentinel','mode_label':'ДЕМО-ПОДМНОЖЕСТВО'}
        metadata['demo']={
            'selection':'Первые профили реального рейтинга из активного source-dataset snapshot',
            'profiles':len(self.ids),
            'synthetic_records':False,
            'labels_invented':False,
        }
        rows=self.source.top(max(len(self.ids),1))
        metadata['counts']=dict(metadata.get('counts',{}), persons=len(self.ids),
                                segments=sum(row.get('total_segments',0) for row in rows),top50=len(self.ids))
        metadata['eligibility']=[{'eligibility_status':status,'profiles':sum(row['eligibility_status']==status for row in rows)}
                                 for status in ('eligible','no_matches','insufficient_data')]
        metadata['assessment']=self.source.assessment_statistics(self.ids)
        metadata['limitations']=list(metadata.get('limitations',[]))+[
            'Демо-режим показывает небольшое подмножество реального рейтинга; слово «кандидат» не означает доказанный статус шпиона.'
        ]
        return metadata

    def top(self, limit=50):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Limit must be 1–1000')
        return deepcopy([row for row in self.source.top(min(limit, max(len(self.ids), 1))) if row['person_id'] in self.ids])

    def users(self, limit=50, offset=0, query='', eligibility='all', sort='rank'):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('Limit must be 1–1000')
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid offset')
        rows=[row for row in self.source.top(1000) if row['person_id'] in self.ids]
        if eligibility in {'assessed','not_assessed'}:
            rows=[row for row in rows if (row.get('evaluated_criteria_count',row.get('available_criteria_count',0))>0)==(eligibility=='assessed')]
        elif eligibility in {'identity_accepted','identity_ambiguous'}:
            rows=[row for row in rows if row.get('identity_status')==('accepted' if eligibility=='identity_accepted' else 'ambiguous')]
        elif eligibility != 'all':
            rows=[row for row in rows if row.get('assessment_status',row.get('eligibility_status')) == eligibility]
        q=query.strip().lower()
        if q:
            rows=[row for row in rows if q in f"{row.get('display_name') or ''} {row['person_id']}".lower()]
        if sort == 'score':
            rows.sort(key=lambda row:(-float(row.get('rule_score') or 0),row['person_id']))
        elif sort == 'name':
            rows.sort(key=lambda row:(str(row.get('display_name') or '').lower(),row['person_id']))
        else:
            rows.sort(key=lambda row:(row.get('rank') is None,row.get('rank') or 10**9,row['person_id']))
        page=rows[offset:offset+limit]
        return {'items':deepcopy(page),'pagination':{'offset':offset,'limit':limit,'total':len(rows),'has_next':offset+len(page)<len(rows)}}

    def profile(self, person_id):
        if person_id not in self.ids:
            raise KeyError('Profile not found in demo subset')
        return self.source.profile(person_id)

    def source_details(self, person_id):
        if person_id not in self.ids:
            raise KeyError('Profile not found in demo subset')
        return self.source.source_details(person_id)
