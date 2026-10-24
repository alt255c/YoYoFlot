"""Model Windows file locking: temporary DBs must close before directory cleanup."""

import sqlite3
import tempfile

import pytest

from yoyoflot.readers import forum_json, skyteam_yaml


@pytest.mark.parametrize(
    "reader,source,scenario",
    [
        (forum_json, '{"Forum Profiles":{"a":{"Real Name":"Иван"}}}', "normal"),
        (forum_json, '{"Forum Profiles":{"a":{},"a":{}}}', "duplicate"),
        (
            forum_json,
            '{"Forum Profiles":{"a":{"Real Name":"X","Real Name":"Y"}}}',
            "duplicate",
        ),
        (forum_json, '{"Forum Profiles":{"a":{"Real Name":"Иван"}}}', "close"),
        (skyteam_yaml, "2017-01-01:\n  AB1: {FF: {AA: '001'}}\n", "normal"),
        (skyteam_yaml, "2017-01-01: {}\n2017-01-01: {}\n", "duplicate"),
        (skyteam_yaml, "2017-01-01:\n  AB1: {FF: {AA: '001'}}\n", "close"),
    ],
)
def test_sqlite_closes_before_temp_cleanup(
    tmp_path, monkeypatch, reader, source, scenario
):
    original_connect = sqlite3.connect
    original_directory = tempfile.TemporaryDirectory
    connections = []
    cleanups = []

    class Connection(sqlite3.Connection):
        closed = False

        def close(self):
            super().close()
            self.closed = True

    def connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs, factory=Connection)
        connections.append(connection)
        return connection

    class Directory:
        def __init__(self):
            self.directory = original_directory()

        def __enter__(self):
            return self.directory.__enter__()

        def __exit__(self, *args):
            try:
                assert connections and all(c.closed for c in connections)
                cleanups.append(True)
            finally:
                self.directory.__exit__(*args)

    monkeypatch.setattr(sqlite3, "connect", connect)
    monkeypatch.setattr(tempfile, "TemporaryDirectory", Directory)
    path = tmp_path / ("input.json" if reader is forum_json else "input.yaml")
    path.write_text(source, encoding="utf-8")
    records = reader.read(path, batch_size=1)
    if scenario == "duplicate":
        with pytest.raises(ValueError, match="Duplicate"):
            list(records)
    elif scenario == "close":
        next(records)
        records.close()
    else:
        assert list(records)
    assert cleanups == [True]
    assert all(c.closed for c in connections)
