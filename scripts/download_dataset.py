#!/usr/bin/env python3
"""Fetch and verify the teacher's original dataset using only the standard library.

Google Drive's ordinary large-file confirmation is supported. Authentication,
cookies, generated substitutes and automatic changes to pinned hashes are not.
"""
from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = Path(__file__).with_name("dataset_manifest.json")
ALLOWED_HOSTS = {"drive.google.com", "drive.usercontent.google.com"}
CHUNK_SIZE = 1024 * 1024
MAX_CONFIRM_BYTES = 1024 * 1024


class DatasetError(ValueError):
    """A source could not be obtained or failed an integrity check."""


def load_manifest():
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def digest(path):
    checksum = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(CHUNK_SIZE), b""):
            checksum.update(block)
    return checksum.hexdigest()


def verify_file(path, expected):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise DatasetError(f"Expected a regular source file: {path}")
    actual_size = path.stat().st_size
    if actual_size != expected["bytes"]:
        raise DatasetError(f"Size mismatch for {path.name}: expected {expected['bytes']}, got {actual_size}")
    actual_hash = digest(path)
    if actual_hash != expected["sha256"]:
        raise DatasetError(f"SHA-256 mismatch for {path.name}: expected {expected['sha256']}, got {actual_hash}")


def verify_dataset(directory, manifest):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise DatasetError(f"Expected a regular dataset directory: {directory}")
    for name, expected in manifest["files"].items():
        verify_file(directory / name, expected)


def checked_url(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        valid_port = parsed.port in (None, 443)
    except ValueError:
        valid_port = False
    if (parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS
            or parsed.username or parsed.password or not valid_port):
        raise DatasetError("Download redirect or confirmation points outside the allowed Google Drive HTTPS hosts")
    return url


class DriveRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, newurl):
        checked_url(newurl)
        return super().redirect_request(request, response, code, message, headers, newurl)


class ConfirmationParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.forms = []
        self.current = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form":
            self.current = {"action": attrs.get("action", ""), "method": attrs.get("method", "get").lower(), "inputs": {}}
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None and attrs.get("name"):
            self.current["inputs"][attrs["name"]] = attrs.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form":
            self.current = None


def confirmation_url(html, response_url, source_url):
    parser = ConfirmationParser()
    parser.feed(html)
    source_id = urllib.parse.parse_qs(urllib.parse.urlsplit(source_url).query)["id"][0]
    for form in parser.forms:
        fields = form["inputs"]
        if form["method"] != "get" or fields.get("id") != source_id or fields.get("export") != "download" or not fields.get("confirm"):
            continue
        action = checked_url(urllib.parse.urljoin(response_url, form["action"]))
        parsed = urllib.parse.urlsplit(action)
        if parsed.path not in ("/download", "/uc"):
            continue
        # Only the public download form fields are submitted; no cookie/session
        # state or arbitrary hidden form values are forwarded.
        fields = {name: fields[name] for name in ("id", "export", "confirm", "uuid") if name in fields}
        return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(fields), ""))
    raise DatasetError("Google Drive returned HTML without a public download confirmation. Check public access/quota, or provide the original ZIP with --archive.")


def download_archive(destination, expected, timeout=60, opener=None):
    """Download to a temporary file; publish the cache only after verification."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_symlink():
        raise DatasetError(f"Archive cache must not be a symbolic link: {destination}")
    if not 0 < timeout <= 300:
        raise DatasetError("Request timeout must be greater than zero and at most 300 seconds")
    opener = opener or urllib.request.build_opener(DriveRedirectHandler())
    url = checked_url(expected["url"])
    temporary = None
    try:
        for _ in range(4):
            request = urllib.request.Request(url, headers={"User-Agent": "YoYoFlot-source-downloader/1.0", "Accept": "application/octet-stream,text/html;q=0.5"})
            with opener.open(request, timeout=timeout) as response:
                response_url = checked_url(response.geturl())
                first = response.read(65536)
                is_html = "text/html" in response.headers.get("Content-Type", "").lower() or first.lstrip().lower().startswith((b"<!doctype html", b"<html"))
                if is_html:
                    body = first + response.read(MAX_CONFIRM_BYTES + 1 - len(first))
                    if len(body) > MAX_CONFIRM_BYTES:
                        raise DatasetError("Google Drive confirmation page exceeds the allowed size")
                    url = confirmation_url(body.decode("utf-8", errors="replace"), response_url, expected["url"])
                    continue
                checksum = hashlib.sha256()
                total = 0
                with tempfile.NamedTemporaryFile(mode="wb", prefix=".Airlines-All-", suffix=".partial", dir=destination.parent, delete=False) as output:
                    temporary = Path(output.name)
                    block = first
                    while block:
                        total += len(block)
                        if total > expected["bytes"]:
                            raise DatasetError("Downloaded archive exceeds its pinned size")
                        checksum.update(block)
                        output.write(block)
                        block = response.read(CHUNK_SIZE)
                    output.flush()
                    os.fsync(output.fileno())
                if total != expected["bytes"] or checksum.hexdigest() != expected["sha256"]:
                    raise DatasetError("Downloaded archive does not match the pinned size/SHA-256. No source files were installed.")
                os.replace(temporary, destination)
                temporary = None
                return destination
        raise DatasetError("Google Drive repeated its download confirmation too many times; use --archive with the original ZIP")
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise DatasetError(f"Download failed ({type(error).__name__}). Check network access or provide the original ZIP with --archive.") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def extract_archive(archive, directory, manifest):
    """Install all seven exact members as one directory, never partial files."""
    directory = Path(directory)
    if directory.is_symlink() or (directory.exists() and (not directory.is_dir() or any(directory.iterdir()))):
        raise DatasetError(f"Refusing to replace an existing nonempty dataset directory: {directory}")
    directory.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{directory.name}-", dir=directory.parent))
    try:
        expected_members = {"Airlines/" + name: (name, metadata) for name, metadata in manifest["files"].items()}
        with zipfile.ZipFile(archive) as source:
            seen = set()
            for member in source.infolist():
                path = PurePosixPath(member.filename)
                mode = member.external_attr >> 16
                if (path.is_absolute() or ".." in path.parts or "\\" in member.filename
                        or stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR))):
                    raise DatasetError(f"Unsafe ZIP member: {member.filename!r}")
                if member.is_dir() and member.filename == "Airlines/":
                    continue
                if member.filename not in expected_members or member.filename in seen:
                    raise DatasetError(f"Unexpected or duplicate ZIP member: {member.filename!r}")
                seen.add(member.filename)
                name, expected = expected_members[member.filename]
                if member.file_size != expected["bytes"] or member.flag_bits & 1:
                    raise DatasetError(f"Unexpected size or encryption for ZIP member: {member.filename!r}")
                checksum = hashlib.sha256()
                total = 0
                with source.open(member) as input_stream, (stage / name).open("xb") as output:
                    for block in iter(lambda: input_stream.read(CHUNK_SIZE), b""):
                        total += len(block)
                        if total > expected["bytes"]:
                            raise DatasetError(f"ZIP member exceeds its pinned size: {member.filename!r}")
                        checksum.update(block)
                        output.write(block)
                if total != expected["bytes"] or checksum.hexdigest() != expected["sha256"]:
                    raise DatasetError(f"SHA-256 mismatch for ZIP member: {member.filename!r}")
            if seen != set(expected_members):
                raise DatasetError("The archive is missing required original source files")
        (stage / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(stage, directory)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
        raise DatasetError(f"Cannot extract the source archive: {error}") from error
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def prepare_dataset(directory, *, archive=None, cache=None, timeout=60, manifest=None):
    manifest = manifest if manifest is not None else load_manifest()
    directory = Path(directory).absolute()
    if directory.exists() or directory.is_symlink():
        if directory.is_symlink() or not directory.is_dir() or any(directory.iterdir()):
            try:
                verify_dataset(directory, manifest)
            except DatasetError as error:
                raise DatasetError(f"Existing dataset failed verification: {error}. Move it aside or select an empty --data-dir; existing files were preserved.") from error
            print(f"Verified all {len(manifest['files'])} original source files: {directory}", flush=True)
            return directory
    if archive is not None:
        source = Path(archive)
        verify_file(source, manifest["archive"])
    else:
        source = Path(cache) if cache is not None else ROOT / "data/downloads/Airlines-All.zip"
        try:
            verify_file(source, manifest["archive"])
            print(f"Reusing verified source archive: {source}", flush=True)
        except DatasetError:
            print("Downloading the original teacher dataset (477,710,849 bytes)...", flush=True)
            download_archive(source, manifest["archive"], timeout=timeout)
    extract_archive(source, directory, manifest)
    print(f"Installed and verified all {len(manifest['files'])} original source files: {directory}", flush=True)
    return directory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, help="Original Airlines-All.zip for an offline data install (size and SHA-256 are checked)")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/raw")
    parser.add_argument("--cache", type=Path, default=ROOT / "data/downloads/Airlines-All.zip")
    parser.add_argument("--timeout", type=float, default=60, help="Per-request socket timeout in seconds (0 < timeout <= 300)")
    args = parser.parse_args(argv)
    try:
        prepare_dataset(args.data_dir, archive=args.archive, cache=args.cache, timeout=args.timeout)
    except (DatasetError, OSError) as error:
        print(f"Dataset error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
