"""Small archive fixtures exercise integrity and extraction boundaries, not ETL."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import stat
import urllib.parse
import zipfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("download_dataset", ROOT / "scripts/download_dataset.py")
download = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(download)


def metadata(value):
    return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}


def archive_fixture(tmp_path, *, entries=None, file_hash=None):
    path = tmp_path / "original.zip"
    files = {"one.csv": b"id,name\n1,Original\n", "two.json": b'{"original":true}\n'}
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, value in entries if entries is not None else [("Airlines/" + name, value) for name, value in files.items()]:
            archive.writestr(name, value)
    manifest = {
        "archive": {"url": "https://drive.google.com/uc?id=source-id&export=download", **metadata(path.read_bytes())},
        "files": {name: metadata(value) for name, value in files.items()},
    }
    if file_hash:
        manifest["files"]["one.csv"]["sha256"] = file_hash
    return path, manifest, files


def test_offline_install_preserves_original_bytes_and_verified_rerun(tmp_path, monkeypatch):
    archive, manifest, files = archive_fixture(tmp_path)
    target = tmp_path / "raw"
    monkeypatch.setattr(download, "download_archive", lambda *args, **kwargs: pytest.fail("offline install touched network"))
    download.prepare_dataset(target, archive=archive, manifest=manifest)
    before = {name: (target / name).stat().st_mtime_ns for name in files}
    archive.unlink()
    download.prepare_dataset(target, archive=archive, manifest=manifest)
    for name, value in files.items():
        assert (target / name).read_bytes() == value
        assert (target / name).stat().st_mtime_ns == before[name]
    assert json.loads((target / "source_manifest.json").read_text()) == manifest


def test_corrupt_existing_source_fails_without_overwriting(tmp_path):
    archive, manifest, _ = archive_fixture(tmp_path)
    target = tmp_path / "raw"
    download.prepare_dataset(target, archive=archive, manifest=manifest)
    source = target / "one.csv"
    original = source.read_bytes()
    source.write_bytes(b"X" + original[1:])
    with pytest.raises(download.DatasetError, match="Existing dataset failed verification.*SHA-256"):
        download.prepare_dataset(target, archive=archive, manifest=manifest)
    assert source.read_bytes() == b"X" + original[1:]


def test_corrupt_local_archive_is_rejected_before_extraction(tmp_path):
    archive, manifest, _ = archive_fixture(tmp_path)
    body = archive.read_bytes()
    archive.write_bytes(b"X" + body[1:])
    target = tmp_path / "raw"
    with pytest.raises(download.DatasetError, match="SHA-256 mismatch"):
        download.prepare_dataset(target, archive=archive, manifest=manifest)
    assert not target.exists()


@pytest.mark.parametrize("member", ["../escape.csv", "/tmp/escape.csv", "Airlines/../escape.csv", "Airlines\\escape.csv", "Airlines/unexpected.csv"])
def test_unsafe_and_unexpected_members_never_publish(tmp_path, member):
    archive, manifest, _ = archive_fixture(tmp_path, entries=[(member, b"bad")])
    with pytest.raises(download.DatasetError, match="Unsafe|Unexpected"):
        download.extract_archive(archive, tmp_path / "raw", manifest)
    assert not (tmp_path / "raw").exists()
    assert not (tmp_path / "escape.csv").exists()
    assert not list(tmp_path.glob(".raw-*"))


def test_symbolic_link_member_is_rejected(tmp_path):
    member = zipfile.ZipInfo("Airlines/one.csv")
    member.create_system = 3
    member.external_attr = (stat.S_IFLNK | 0o777) << 16
    archive, manifest, _ = archive_fixture(tmp_path, entries=[(member, b"/etc/passwd")])
    with pytest.raises(download.DatasetError, match="Unsafe ZIP member"):
        download.extract_archive(archive, tmp_path / "raw", manifest)
    assert not (tmp_path / "raw").exists()


def test_duplicate_members_missing_files_and_member_hashes_fail_atomically(tmp_path):
    original = b"id,name\n1,Original\n"
    with pytest.warns(UserWarning, match="Duplicate"):
        archive, manifest, _ = archive_fixture(tmp_path, entries=[("Airlines/one.csv", original)] * 2)
    with pytest.raises(download.DatasetError, match="duplicate"):
        download.extract_archive(archive, tmp_path / "raw", manifest)
    archive, manifest, _ = archive_fixture(tmp_path, entries=[("Airlines/one.csv", original)])
    with pytest.raises(download.DatasetError, match="missing required"):
        download.extract_archive(archive, tmp_path / "raw", manifest)
    archive, manifest, _ = archive_fixture(tmp_path, file_hash="0" * 64)
    with pytest.raises(download.DatasetError, match="SHA-256 mismatch for ZIP member"):
        download.extract_archive(archive, tmp_path / "raw", manifest)
    assert not (tmp_path / "raw").exists()
    assert not list(tmp_path.glob(".raw-*"))


def test_rejects_existing_dataset_symlinks(tmp_path):
    archive, manifest, _ = archive_fixture(tmp_path)
    raw = tmp_path / "actual"
    download.prepare_dataset(raw, archive=archive, manifest=manifest)
    target = tmp_path / "raw"
    target.symlink_to(raw, target_is_directory=True)
    with pytest.raises(download.DatasetError, match="regular dataset directory"):
        download.prepare_dataset(target, archive=archive, manifest=manifest)
    original = raw / "one.csv"
    moved = tmp_path / "external.csv"
    original.rename(moved)
    original.symlink_to(moved)
    with pytest.raises(download.DatasetError, match="regular source file"):
        download.prepare_dataset(raw, archive=archive, manifest=manifest)


class Response(io.BytesIO):
    def __init__(self, body, *, content_type="application/zip", url="https://drive.usercontent.google.com/download"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}
        self.url = url

    def geturl(self):
        return self.url


class Opener:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        return next(self.responses)


def confirmation(action="/download", source_id="source-id"):
    return (f'<html><form method="GET" action="{action}">'
            f'<input type="hidden" name="id" value="{source_id}">'
            '<input type="hidden" name="export" value="download">'
            '<input type="hidden" name="confirm" value="t">'
            '<input type="hidden" name="uuid" value="public-confirmation">'
            '<input type="hidden" name="unrelated" value="do-not-forward">'
            '</form></html>').encode()


def test_download_follows_public_confirmation_without_cookies(tmp_path):
    content = b"original zipped bytes"
    expected = {"url": "https://drive.google.com/uc?id=source-id&export=download", **metadata(content)}
    opener = Opener([Response(confirmation(), content_type="text/html"), Response(content)])
    cache = tmp_path / "downloads/source.zip"
    download.download_archive(cache, expected, timeout=17, opener=opener)
    assert cache.read_bytes() == content
    assert len(opener.requests) == 2
    request, timeout = opener.requests[1]
    assert timeout == 17
    assert urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query) == {
        "id": ["source-id"], "export": ["download"], "confirm": ["t"], "uuid": ["public-confirmation"],
    }
    assert not request.has_header("Cookie")


@pytest.mark.parametrize("action", ["http://drive.google.com/download", "https://attacker.example/download", "https://drive.google.com@attacker.example/download"])
def test_download_rejects_untrusted_confirmation_targets(tmp_path, action):
    expected = {"url": "https://drive.google.com/uc?id=source-id&export=download", **metadata(b"original")}
    opener = Opener([Response(confirmation(action), content_type="text/html")])
    with pytest.raises(download.DatasetError, match="outside the allowed"):
        download.download_archive(tmp_path / "cache.zip", expected, opener=opener)
    assert len(opener.requests) == 1
    assert not (tmp_path / "cache.zip").exists()


@pytest.mark.parametrize("content", [b"bad", b"0123456789-too-large"])
def test_failed_download_preserves_cache_and_removes_partial(tmp_path, content):
    cache = tmp_path / "cache.zip"
    cache.write_bytes(b"previous cached bytes")
    expected = {"url": "https://drive.google.com/uc?id=source-id&export=download", **metadata(b"original")}
    with pytest.raises(download.DatasetError, match="pinned size"):
        download.download_archive(cache, expected, opener=Opener([Response(content)]))
    assert cache.read_bytes() == b"previous cached bytes"
    assert not list(tmp_path.glob("*.partial"))


def test_cache_is_verified_before_reuse_or_download(tmp_path, monkeypatch):
    archive, manifest, _ = archive_fixture(tmp_path)
    original_archive = archive.read_bytes()
    monkeypatch.setattr(download, "download_archive", lambda *args, **kwargs: pytest.fail("valid cache was downloaded again"))
    download.prepare_dataset(tmp_path / "raw", cache=archive, manifest=manifest)
    archive.write_bytes(b"X" + original_archive[1:])
    calls = []

    def replacement(destination, expected, timeout):
        calls.append((destination, expected))
        destination.write_bytes(original_archive)

    monkeypatch.setattr(download, "download_archive", replacement)
    download.prepare_dataset(tmp_path / "second", cache=archive, manifest=manifest)
    assert len(calls) == 1
    download.verify_dataset(tmp_path / "second", manifest)


def test_confirmation_requires_original_file_and_bounded_retries(tmp_path):
    expected = {"url": "https://drive.google.com/uc?id=source-id&export=download", **metadata(b"original")}
    with pytest.raises(download.DatasetError, match="without a public download"):
        download.download_archive(tmp_path / "cache.zip", expected, opener=Opener([Response(confirmation(source_id="different"), content_type="text/html")]))
    opener = Opener([Response(confirmation(), content_type="text/html") for _ in range(4)])
    with pytest.raises(download.DatasetError, match="too many times"):
        download.download_archive(tmp_path / "cache.zip", expected, opener=opener)
    assert len(opener.requests) == 4
