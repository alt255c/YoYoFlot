import hashlib
import json
from pathlib import Path


def fingerprint(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def stable_id(source_id, version, locator, kind):
    return hashlib.sha256(
        json.dumps([source_id, version, locator, kind], ensure_ascii=False).encode()
    ).hexdigest()
