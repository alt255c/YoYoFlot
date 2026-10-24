from pathlib import Path

import yaml


def load_sources(path="configs/sources.yaml"):
    with open(path, encoding="utf-8") as f:
        config = yaml.safe_load(f)
    sources = config.get("sources")
    if not isinstance(sources, dict):
        raise ValueError("sources must be a mapping")
    return sources


def resolve_path(source, root):
    root = Path(root)
    path = root / source["file"]
    if not path.is_file():
        raise FileNotFoundError(f"Source missing: {path}")
    return path
