"""Exact, small controls for streaming T13 candidates without sorting evidence.

The legacy SQL here is an independent equivalence oracle for unit fixtures;
it is never invoked by the production data pipeline.
"""
import copy
import json

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from yoyoflot.resolve.blocking import candidate_rows


NODE_FIELDS = (
    "observation_id", "source_id", "source_record_id", "locator", "name_raw", "name_comparison",
    "name_latin_comparison", "birth_date", "documents_json", "identifiers_json",
)
FLAG_FIELDS = (
    "document_match", "card_match", "source_document_match", "name_dob_match", "contaminated", "shared_identifier",
)
NODE_SCHEMA = pa.schema([("nid", pa.int64()), *[(name, pa.string()) for name in NODE_FIELDS]])
CANDIDATE_SCHEMA = pa.schema([
    ("left_n", pa.int64()), ("right_n", pa.int64()), *[(name, pa.bool_()) for name in FLAG_FIELDS],
    ("keys_json", pa.string()),
])


def legacy_rows(db, batch_size):
    columns = ",".join(f"{side}.{name} {prefix}_{name}"
                       for side, prefix in [("a", "left"), ("b", "right")] for name in NODE_FIELDS)
    cursor = db.execute("SELECT c.*," + columns + """ FROM candidates c JOIN nodes a ON a.nid=c.left_n
        JOIN nodes b ON b.nid=c.right_n ORDER BY CASE WHEN document_match THEN 0 WHEN card_match THEN 1
        WHEN source_document_match THEN 2 ELSE 3 END,c.left_n,c.right_n""")
    reader = cursor.to_arrow_reader(batch_size)
    for batch in reader:
        for row in batch.to_pylist():
            yield row, {key: row["left_" + key] for key in NODE_FIELDS}, {key: row["right_" + key] for key in NODE_FIELDS}


def node(nid):
    # Wide JSON/Unicode make accidental projection, parsing or truncation visible.
    evidence = [{"identifier_id": f"i:{nid}:{i}", "kind": "document", "namespace": "issuer:Тест",
                 "key_hash": "00" + str(i), "source_record_id": f"record:{nid}:{i}",
                 "locator": "строка/" + "ё" * 256 + str(i)} for i in range(24)]
    return dict(nid=nid, observation_id=f"observation:{nid}", source_id="fixture",
                source_record_id=f"record:{nid}", locator=f"строка:{nid}",
                name_raw=None if nid == 2 else ("Иванов, Сергей\n" + "Ж" * 4096 if nid == 4 else f"Person {nid}"),
                name_comparison=None if nid == 2 else f"person {nid}",
                name_latin_comparison=None if nid == 2 else f"person {nid}",
                birth_date=None if nid % 3 == 0 else "1980-02-29" if nid % 3 == 1 else "1980-03-01",
                documents_json=json.dumps([{"namespace": "issuer:Тест", "value": "0000123"}], ensure_ascii=False),
                identifiers_json=json.dumps(evidence, ensure_ascii=False))


def candidate(left, right, **flags):
    witness = {"kind": "card", "key_hash": "00000", "namespace": "programme:Тест", "member_count": 2,
               "contaminated": False}
    row = dict(left_n=left, right_n=right, **{name: False for name in FLAG_FIELDS},
               keys_json=json.dumps([witness, witness], ensure_ascii=False))
    row.update(flags)
    return row


def populate(db, rows, nodes=None):
    nodes = [node(i) for i in range(10)] if nodes is None else nodes
    db.register("fixture_nodes", pa.Table.from_pylist(nodes, schema=NODE_SCHEMA))
    db.register("fixture_candidates", pa.Table.from_pylist(rows, schema=CANDIDATE_SCHEMA))
    db.execute("CREATE TABLE nodes AS SELECT * FROM fixture_nodes")
    db.execute("CREATE TABLE candidates AS SELECT * FROM fixture_candidates")


def priority(row):
    return 0 if row["document_match"] else 1 if row["card_match"] else 2 if row["source_document_match"] else 3


@pytest.mark.parametrize("batch_size", [1, 2, 7, 4000])
def test_candidate_stream_exact_legacy_rows_priority_order_and_complete_evidence(batch_size):
    rows = [
        candidate(5, 6), candidate(8, 9, source_document_match=True),
        candidate(1, 7, card_match=True), candidate(0, 1, document_match=True),
        candidate(3, 6, name_dob_match=True, contaminated=True), candidate(0, 3, source_document_match=True),
        candidate(2, 5, document_match=True, card_match=True), candidate(0, 9, name_dob_match=True),
        candidate(4, 8, document_match=None), candidate(1, 4, card_match=True, source_document_match=True),
    ]
    # The stream is not the deduplication stage: identical candidate occurrences
    # and duplicate evidence keys must remain exactly as its input describes.
    rows.insert(3, copy.deepcopy(rows[5]))
    with duckdb.connect() as db:
        populate(db, rows, [node(i) for i in reversed(range(10))])
        expected = list(legacy_rows(db, batch_size=3))
        actual = list(candidate_rows(db, batch_size))
        assert actual == expected
        assert len(actual) == len(rows)
        assert [(priority(row), row["left_n"], row["right_n"]) for row, _, _ in actual] == sorted(
            (priority(row), row["left_n"], row["right_n"]) for row in rows)
        for row, left, right in actual:
            assert set(left) == set(right) == set(NODE_FIELDS)
            assert left == {name: node(row["left_n"])[name] for name in NODE_FIELDS}
            assert right == {name: node(row["right_n"])[name] for name in NODE_FIELDS}
            assert json.loads(row["keys_json"])[0] == json.loads(row["keys_json"])[1]
            assert len(json.loads(left["identifiers_json"])) == len(json.loads(right["identifiers_json"])) == 24
        # It must leave the evidence tables and connection available to the
        # remaining matching stages and support another pass with a new batch.
        assert db.execute("SELECT count(*) FROM nodes").fetchone()[0] == 10
        assert list(candidate_rows(db, 5)) == expected


@pytest.mark.parametrize("batch_size", [1, 4000])
def test_empty_candidate_stream_is_empty_and_keeps_connection_usable(batch_size):
    with duckdb.connect() as db:
        populate(db, [], [])
        assert list(candidate_rows(db, batch_size)) == []
        assert db.execute("SELECT 42").fetchone() == (42,)


def test_candidate_stream_repeated_nodes_across_batches_keep_each_source_witness():
    rows = [candidate(0, right, card_match=True) for right in range(1, 10)]
    with duckdb.connect() as db:
        populate(db, rows[::-1])
        expected = list(legacy_rows(db, 4))
        actual = list(candidate_rows(db, 2))
        assert actual == expected
        assert [right["observation_id"] for _, _, right in actual] == [f"observation:{i}" for i in range(1, 10)]
        assert {left["source_record_id"] for _, left, _ in actual} == {"record:0"}


def test_large_batch_crosses_node_lookup_chunks_and_preserves_inner_join_behavior():
    nodes = [dict(nid=i, **{name: f"{name}:{i}" for name in NODE_FIELDS}) for i in range(2054)]
    for item in nodes:
        item.update(documents_json="[]", identifiers_json="[]", birth_date=None)
    rows = [candidate(i, i + 1, card_match=True) for i in range(0, 2054, 2)]
    rows.extend([candidate(None, 0, document_match=True), candidate(0, 999999, document_match=True)])
    with duckdb.connect() as db:
        populate(db, rows[::-1], nodes[::-1])
        expected = list(legacy_rows(db, 4000))
        actual = list(candidate_rows(db, 4000))
        assert actual == expected
        assert len(actual) == 1027
        assert {left["observation_id"] for _, left, _ in actual} == {f"observation_id:{i}" for i in range(0, 2054, 2)}


def test_closing_candidate_generator_early_leaves_connection_and_next_pass_usable():
    rows = [candidate(0, 1, card_match=True), candidate(1, 2, document_match=True), candidate(2, 3)]
    with duckdb.connect() as db:
        populate(db, rows)
        expected = list(legacy_rows(db, 2))
        stream = candidate_rows(db, 1)
        assert next(stream) == expected[0]
        stream.close()
        assert db.execute("SELECT count(*) FROM candidates").fetchone()[0] == len(rows)
        assert list(candidate_rows(db, 2)) == expected


def test_streaming_and_legacy_resolution_preserve_duplicate_identifier_and_dob_decisions(tmp_path, monkeypatch):
    from tests.test_matching import CONFIG, identifier, input_snapshot, person, travel
    from yoyoflot.resolve import pipeline
    from yoyoflot.resolve.common import SCHEMAS

    people = [person("a", dob="1980-02-29"), person("b", source="forum_json", dob="1980-02-29"),
              person("c", dob="1980-03-01"), person("d", dob=None), person("e", name=None, dob=None)]
    identifiers = [identifier("a"), identifier("a", iid="duplicate-key-second-witness"),
                   identifier("b", source="forum_json"), identifier("c", value="DIFFERENT"),
                   identifier("d", value="SHARED"), identifier("e", value="SHARED"),
                   identifier("a", value="D001", kind="document"),
                   identifier("c", value="D001", kind="document")]
    trips = [travel("a-flight", "a"), travel("b-flight", "b", source="forum_json"),
             travel("c-flight", "c", departure_raw="2017-01-03", departure_local="2017-01-03", departure_precision="date")]
    source = input_snapshot(tmp_path, people, identifiers, trips)
    streamed, legacy = tmp_path / "streamed", tmp_path / "legacy"
    pipeline.run(source, streamed, CONFIG, review_output=tmp_path / "streamed-review", batch_size=1, memory_limit="128MB")
    monkeypatch.setattr(pipeline, "candidate_rows", legacy_rows)
    pipeline.run(source, legacy, CONFIG, review_output=tmp_path / "legacy-review", batch_size=3, memory_limit="128MB")
    for table in SCHEMAS:
        actual = pq.read_table(streamed / (table + ".parquet")).to_pylist()
        expected = pq.read_table(legacy / (table + ".parquet")).to_pylist()
        order = lambda row: json.dumps(row, sort_keys=True, ensure_ascii=False)
        assert sorted(actual, key=order) == sorted(expected, key=order), table
    pairs = pq.read_table(streamed / "person_pairs.parquet").to_pylist()
    assert pairs
    witnesses = [json.loads(pair["evidence_json"])[side] for pair in pairs for side in ("left", "right")]
    source_a = next(witness for witness in witnesses if witness["observation_id"] == "a")
    assert len(source_a["identifiers"]) == 3
