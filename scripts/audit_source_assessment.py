"""Audit the final published ranking and complete source associations."""
import argparse
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
sys.path.insert(0,str(ROOT))
from yoyoflot.product.store import ProductStore
from scripts.verify_source_product import check


def audit(product):
    store=ProductStore(product)
    if store.runtime_scores is None or store.assessment_manifest['version']!='source-assessment:4.0':
        raise ValueError('Audit requires the final complete scoring product; run build_and_run.sh')
    result=check(product)
    checks=dict(store.assessment_report['checks'])
    with store.connection() as db:
        checks['multiply_assigned_observation']=db.execute("""SELECT count(*) FROM (
            SELECT observation_id FROM assessment_source_links GROUP BY observation_id
            HAVING count(DISTINCT person_id)>1)""").fetchone()[0]
        checks['incomplete_profile_links']=db.execute('SELECT count(*) FROM assessment_profile_assessments WHERE NOT source_links_complete').fetchone()[0]
        checks['unknown_profile']=db.execute('SELECT count(*) FROM assessment_source_links ANTI JOIN persons USING(person_id)').fetchone()[0]
    if any(checks.values()): raise ValueError(checks)
    return dict(result,checks=checks,statistics=store.assessment_statistics(),
                assessment_run_id=store.assessment_manifest['run_id'])


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--product',type=Path,default=ROOT/'artifacts/source-product')
    p.add_argument('--output',type=Path)
    a=p.parse_args()
    result=audit(a.product)
    content=json.dumps(result,ensure_ascii=False,sort_keys=True,indent=2)+'\n'
    if a.output:
        a.output.parent.mkdir(parents=True,exist_ok=True)
        a.output.write_text(content,encoding='utf-8')
    print(content,end='')
