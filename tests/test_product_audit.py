"""Audit must reject broken population values, including people outside top50."""
import json

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from scripts.verify_source_product import check
from tests.test_source_product import ROOT, config, tables
from yoyoflot.product.build import run
from yoyoflot.provenance import fingerprint


def test_audit_file_is_identical_for_different_group_iteration_orders(tmp_path, monkeypatch, capsys):
    from scripts import verify_source_product as audit

    output = tmp_path / 'verification.json'
    monkeypatch.setattr('sys.argv', ['verify_source_product', '--report', str(output)])
    records = iter([
        {'status': 'passed', 'if_roles': {'train': 14, 'holdout': 4, 'calibration': 5}},
        {'if_roles': {'calibration': 5, 'train': 14, 'holdout': 4}, 'status': 'passed'},
    ])
    monkeypatch.setattr(audit, 'check', lambda _: next(records))
    audit.main()
    first = output.read_bytes()
    audit.main()
    assert output.read_bytes() == first
    assert json.loads(first)['if_roles'] == {'train': 14, 'holdout': 4, 'calibration': 5}
    capsys.readouterr()


@pytest.mark.parametrize('field', ['matched_criteria_count', 'available_criteria_count', 'eligibility_status'])
def test_null_population_fields_outside_top_are_not_ignored(tmp_path, field):
    source = tables(tmp_path, people=51)
    output = tmp_path / 'product'
    run(source, output, config(tmp_path), ROOT / 'data/reference/airports.csv')
    path = output / 'tables/rankings.parquet'
    table = pq.read_table(path)
    rows = table.to_pylist()
    assert rows[-1]['rank'] == 51
    rows[-1][field] = None
    pq.write_table(pa.Table.from_pylist(rows, schema=table.schema), path)
    manifest_path = output / 'run_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files']['tables/rankings.parquet'] = fingerprint(path)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='Population score/explanation mismatch'):
        check(output)
