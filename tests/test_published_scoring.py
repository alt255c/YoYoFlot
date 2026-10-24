"""Publication regressions using the existing T13 unit fixture, never app input."""
import itertools
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from tests.test_source_product import tables, config, ROOT
from yoyoflot.product import assessment
from yoyoflot.product.build import run as build
from yoyoflot.product.compile import run as compile_product
from yoyoflot.product.scoring import load, select_independent, RuntimeScores
from yoyoflot.product.store import ProductStore
from yoyoflot.product.report import run as export
from yoyoflot.product.web import handler, StoreManager, _extract_product
from yoyoflot.provenance import fingerprint
from scripts.verify_source_product import check


def test_indirect_overlap_cannot_collapse_independent_contributions():
    names=['D01','N01','L01']
    weights=[2.,.7,.8]
    assert select_independent(names,weights,['D01:N01','L01:N01'])==['D01','L01']
    assert select_independent(names,[2.,3.,.8],['D01:N01','L01:N01'])==['N01']
    for permutation in itertools.permutations(zip(names,weights)):
        ids,values=zip(*permutation)
        assert select_independent(ids,values,['D01:N01','L01:N01'])==['D01','L01']


@pytest.fixture
def published(tmp_path,monkeypatch):
    source=tables(tmp_path)
    base=tmp_path/'source-product'
    build(source,base,config(tmp_path),ROOT/'data/reference/airports.csv')
    canonical=tmp_path/'upstream'
    # Only archive provenance is mocked at the boundary. All source association,
    # factor, publication, hashing and reader code executes unchanged on the
    # project's pre-existing unit fixture.
    def fixture_inputs(product,canonical,archive,raw,repo):
        pm=json.loads((product/'run_manifest.json').read_text())
        cr=json.loads((canonical/'canonical_report.json').read_text())
        files=[canonical/name for name in ['observations.parquet','identifiers.parquet','travel_observations.parquet']]
        return dict(version=assessment.VERSION,product_manifest_sha256=fingerprint(product/'run_manifest.json'),
                    inputs={str(p):fingerprint(p) for p in files},source_version_bridges=[]),pm,cr
    monkeypatch.setattr(assessment,'_inputs',fixture_inputs)
    supplement=tmp_path/'source-assessment-v4'
    result=assessment.build(base,canonical,tmp_path/'unused.zip',tmp_path/'unused-raw',supplement,progress=lambda _:None)
    assert result['checks']['incomplete_profile_links']==0
    final=tmp_path/'published'
    compile_product(base,final,ROOT/'configs/scoring.yaml')
    return final


def test_final_score_export_and_api_use_identical_snapshot(published,tmp_path):
    store=ProductStore(published)
    assert store.runtime_scores.pending==[]
    assert store.runtime_scores.prepared==sorted(load(ROOT/'configs/scoring.yaml').weights)
    assert check(published)['population_scores_consistent']
    report=export(published,tmp_path/'export')
    app=handler(StoreManager(store))
    client=app.test_client()
    assert client.get('/api/top').json==store.top()
    assert report['run_id']==store.manifest['run_id']
    assert [(r['person_id'],r['rank'],r['rule_score']) for r in report['top50']]==[
        (r['person_id'],r['rank'],r['rule_score']) for r in store.top()]
    exported=json.loads((tmp_path/'export/profiles.json').read_text())['profiles']
    for profile in exported:
        assert client.get('/api/profile',query_string={'id':profile['person']['person_id']}).json==profile
        assert len(profile['factors'])+len(profile['additional_factors'])==12
    assert not any('forest' in p.name or p.suffix=='.joblib' for p in published.rglob('*'))


def test_browser_demo_isolation_and_nested_manifest_upload(published,tmp_path):
    manager=StoreManager(ProductStore(published))
    app=handler(manager)
    first,second=app.test_client(),app.test_client()
    assert first.post('/api/demo').status_code==200
    assert first.get('/api/metadata').json['data_mode']=='demo_subset'
    assert second.get('/api/metadata').json['data_mode']=='source_dataset'
    assert first.post('/api/reset').json['mode']=='source_dataset'
    archive=tmp_path/'product.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for p in published.rglob('*'):
            if p.is_file(): z.write(p,p.relative_to(published))
        z.writestr('provenance/t16/run_manifest.json','{"scope":"internal_data_core"}')
    with archive.open('rb') as stream:
        root,temporary=_extract_product(stream)
    try:
        assert ProductStore(root).manifest['run_id']==manager.current().manifest['run_id']
    finally:
        shutil.rmtree(temporary)
    manager.close()


def test_changed_weights_require_rebuild(published,tmp_path):
    path=tmp_path/'scoring.yaml'
    path.write_text((ROOT/'configs/scoring.yaml').read_text().replace('M01: 1.0','M01: 2.0'))
    with pytest.raises(ValueError,match='weights changed'):
        ProductStore(published,scoring=load(path))


def test_explanation_names_only_direct_shared_evidence(published):
    store=ProductStore(published)
    with store.connection() as db:
        db.execute('CREATE TEMP TABLE test_coverage AS SELECT * FROM assessment_marker_coverage')
        db.execute("UPDATE test_coverage SET status='matched',value=1 WHERE criterion_id IN ('D01','N01','L01')")
        db.execute('CREATE OR REPLACE TEMP VIEW assessment_marker_coverage AS SELECT * FROM test_coverage')
        db.execute('''CREATE OR REPLACE TEMP VIEW assessment_marker_dependencies AS
            SELECT person_id,criterion_id,'shared-document' dependency_key FROM test_coverage
            WHERE criterion_id IN ('D01','N01')''')
        scores=RuntimeScores(db,load(published/'scoring.yaml'),'unit-product','unit-assessment')
        try:
            scores.attach(db)
            rows=db.execute("SELECT contribution,dependent_on_criterion_id FROM explanations WHERE criterion_id='N01'").fetchall()
            assert rows and all(row==(0.0,'D01') for row in rows)
            assert db.execute("SELECT min(contribution) FROM explanations WHERE criterion_id='L01'").fetchone()[0]==.8
        finally:
            scores.close()


def test_all_disabled_weights_preserve_observed_factor_statuses(published,tmp_path):
    import yaml
    path=tmp_path/'disabled.yaml'
    data=yaml.safe_load((ROOT/'configs/scoring.yaml').read_text())
    data['weights']={key:0 for key in data['weights']}
    path.write_text(yaml.safe_dump(data))
    target=tmp_path/'disabled-product'
    compile_product(tmp_path/'source-product',target,path)
    store=ProductStore(target)
    assert store.top()==[]
    assert check(target)['population_scores_consistent']


@pytest.mark.parametrize('previous_kind',['symlink','directory','absent'])
def test_failed_second_publication_restores_both_outputs(tmp_path,monkeypatch,previous_kind):
    from scripts import build_project
    outputs=[tmp_path/'report',tmp_path/'product']
    destinations=[tmp_path/'new-report',tmp_path/'new-product']
    for output,destination in zip(outputs,destinations):
        destination.mkdir()
        if previous_kind=='symlink':
            old=tmp_path/('old-'+output.name)
            old.mkdir()
            (old/'run_manifest.json').write_text('{"version":"old"}')
            output.symlink_to(old.name,target_is_directory=True)
        elif previous_kind=='directory':
            output.mkdir()
            (output/'run_manifest.json').write_text('{"version":"old"}')
    replace=build_project.os.replace
    def fail_second(source,target):
        if Path(target)==outputs[1] and '.next-' in Path(source).name:
            raise OSError('injected publication failure')
        return replace(source,target)
    monkeypatch.setattr(build_project.os,'replace',fail_second)
    with pytest.raises(OSError,match='injected publication failure'):
        build_project.publish_links(zip(outputs,destinations))
    for output in outputs:
        if previous_kind=='absent':
            assert not output.exists() and not output.is_symlink()
        else:
            assert (output/'run_manifest.json').read_text()=='{"version":"old"}'
            assert output.is_symlink()==(previous_kind=='symlink')
    assert not list(tmp_path.glob('.*.next-*'))
    assert not list(tmp_path.glob('.*.restore-*'))


def test_publication_preflight_preserves_unrelated_directory(tmp_path):
    from scripts.build_project import publish_links
    destination=tmp_path/'new'
    destination.mkdir()
    unrelated=tmp_path/'unrelated'
    unrelated.mkdir()
    (unrelated/'keep.txt').write_text('keep')
    with pytest.raises(ValueError,match='unrelated directory'):
        publish_links([(tmp_path/'report',destination),(unrelated,destination)])
    assert not (tmp_path/'report').exists()
    assert (unrelated/'keep.txt').read_text()=='keep'
