"""Transactional, bounded Parquet writer. No final output is published on failure."""

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import pyarrow.parquet as pq

from .contracts import CONTRACT_VERSION, SCHEMAS, TableBatch


def atomic_json(path, payload):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.replace(tmp, path)


def publish_directory(staging, output):
    backup = output.with_name(output.name + ".backup")
    if backup.exists():
        raise RuntimeError(f"Backup requires recovery: {backup}")
    if output.exists():
        os.replace(output, backup)
    try:
        os.replace(staging, output)
    except BaseException:
        if backup.exists():
            os.replace(backup, output)
        raise
    if backup.exists():
        shutil.rmtree(backup)


def write_batches(batches, staging):
    writers, counts, errors, counters = {}, {}, {}, {}
    try:
        for batch in batches:
            for key, value in batch.counters.items():
                counters[key] = counters.get(key, 0) + value
            table = batch.to_arrow()
            if batch.table_name not in writers:
                writers[batch.table_name] = pq.ParquetWriter(
                    staging / (batch.table_name + ".parquet"),
                    SCHEMAS[batch.table_name],
                    compression="zstd",
                )
            writers[batch.table_name].write_table(table)
            counts[batch.table_name] = counts.get(batch.table_name, 0) + len(table)
            if batch.table_name == "errors":
                for record in batch.records:
                    k = record.get("severity", "error")
                    errors[k] = errors.get(k, 0) + 1
        for name in SCHEMAS:
            if name not in writers:
                pq.write_table(
                    TableBatch(name, []).to_arrow(), staging / (name + ".parquet")
                )
                counts[name] = 0
    finally:
        for writer in writers.values():
            writer.close()
    return counts, errors, counters


def ingest(reader, path, source_id, output, **options):
    from .provenance import fingerprint

    path, output = Path(path), Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.with_name(output.name + "." + uuid.uuid4().hex + ".partial")
    staging.mkdir()
    t0 = time.perf_counter()
    manifest = dict(
        source_id=source_id,
        input_path=str(path.resolve()),
        sha256=fingerprint(path),
        contract_version=CONTRACT_VERSION,
        reader_version="1.0",
        options=dict(options),
        status="running",
    )
    atomic_json(staging / "source_report.json", manifest)
    try:
        if options.get("columns"):
            manifest["configuration_sha256"] = fingerprint(options["columns"])
        if source_id == "boardingpass_xlsx":
            options["coverage_path"] = staging / "workbook_coverage.csv"
        counts, errors, counters = write_batches(
            reader(path, source_id=source_id, **options), staging
        )
        if fingerprint(path) != manifest["sha256"]:
            raise RuntimeError(
                "Input changed during ingestion; result was not published"
            )
        manifest.update(
            status="complete_with_errors" if errors.get("error") else "complete",
            counts=counts,
            input_counts=counters,
            error_severities=errors,
            seconds=time.perf_counter() - t0,
        )
        try:
            import resource

            value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            manifest["process_peak_rss_mib"] = value / (
                1024 * 1024 if sys.platform == "darwin" else 1024
            )
        except ImportError:
            manifest["process_peak_rss_mib"] = None
        atomic_json(staging / "source_report.json", manifest)
        publish_directory(staging, output)
        return manifest
    except BaseException as exc:
        manifest.update(
            status="failed", exception=str(exc), seconds=time.perf_counter() - t0
        )
        atomic_json(staging / "source_report.json", manifest)
        raise
