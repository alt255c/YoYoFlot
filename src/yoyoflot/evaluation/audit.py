"""Prepare a blind fictional review; import decisions without inventing them."""
import csv
import hashlib
import io
import json
import random
import shutil
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from ..models.demo import digest, write_json
from ..normalize.locking import OutputLock
from ..provenance import fingerprint
from .compare import METHODS, selected
from .storage import validate_snapshot

FIELDS = ["review_id", "reviewer", "verdict", "reason_ru", "reviewed_at"]
VERDICTS = {"интересно_проверить", "обычная_история", "недостаточно_данных"}
PROFILE_SCHEMA = pa.schema([("review_id", pa.string()), ("source_coverage", pa.float64()), ("window_start", pa.date32()), ("window_end", pa.date32())])
SEGMENT_SCHEMA = pa.schema([("review_id", pa.string()), ("review_segment_id", pa.string()), ("departure_date", pa.date32()),
                            ("departure_precision", pa.string()), ("origin_iata", pa.string()), ("destination_iata", pa.string())])
CONTEXT_SCHEMA = pa.schema([("review_id", pa.string()), ("companion_alias", pa.string()), ("event_alias", pa.string()),
                            ("departure_date", pa.date32()), ("origin_iata", pa.string()), ("destination_iata", pa.string()), ("participant_count", pa.int32())])
KEY_SCHEMA = pa.schema([("review_id", pa.string()), ("profile_id", pa.string()), ("stratum", pa.string()),
                        ("case_id", pa.string()), ("benchmark_label", pa.int32()), ("top_methods", pa.list_(pa.string()))])


def write_bundle(path, profiles, segments, methods, annotations, graph_events, config, input_hash):
    path = Path(path)
    people, cases = {p["profile_id"]: p for p in profiles}, {a["profile_id"]: a for a in annotations}
    top = {method: {r["profile_id"] for r in selected(methods[method], 50)} for method in METHODS}
    union = set.union(*top.values())
    insufficient = {r["profile_id"] for r in methods["rules"] if not r["available"]}
    buckets = {"top_union": sorted(union), "outside_top": sorted(set(people) - union - insufficient), "insufficient": sorted(insufficient)}
    strata = {pid: name for name, ids in buckets.items() for pid in ids}
    if set(strata) != set(people):
        raise ValueError("Неоднозначные/неполные страты аудита")
    rng = random.Random(config["seed"])
    size = min(config["size"], len(people))
    quotas = [size // 2, size * 3 // 10, size - size // 2 - size * 3 // 10]
    chosen = []
    for (name, ids), quota in zip(buckets.items(), quotas):
        chosen.extend(rng.sample(ids, min(quota, len(ids))))
    chosen.extend(rng.sample(sorted(set(people) - set(chosen)), size - len(chosen)))
    aliases = {pid: "review_" + digest([config["seed"], input_hash, pid])[:24] for pid in people}
    chosen.sort(key=lambda pid: aliases[pid])
    keys = [dict(review_id=aliases[pid], profile_id=pid, stratum=strata[pid], case_id=cases[pid]["case_id"],
                 benchmark_label=cases[pid]["benchmark_label"], top_methods=[m for m in METHODS if pid in top[m]]) for pid in chosen]
    selected_ids = set(chosen)
    blind_profiles = [dict(review_id=aliases[pid], **{k: people[pid][k] for k in ["source_coverage", "window_start", "window_end"]}) for pid in chosen]
    blind_segments = [dict(review_id=aliases[s["profile_id"]], review_segment_id="segment_" + digest([input_hash, s["segment_id"]])[:24],
                           **{k: s[k] for k in ["departure_date", "departure_precision", "origin_iata", "destination_iata"]})
                      for s in segments if s["profile_id"] in selected_ids]
    context = []
    for event in graph_events:
        for pid in sorted(selected_ids.intersection(event["participant_ids"])):
            for companion in sorted(set(event["participant_ids"]) - {pid}):
                context.append(dict(review_id=aliases[pid], companion_alias=aliases[companion],
                    event_alias="event_" + digest([input_hash, event["event_id"]])[:24],
                    **{k: event[k] for k in ["departure_date", "origin_iata", "destination_iata", "participant_count"]}))
    (path / "review/blind").mkdir(parents=True)
    (path / "private").mkdir()
    for name, rows, schema in [("review/blind/profiles.parquet", blind_profiles, PROFILE_SCHEMA),
                               ("review/blind/segments.parquet", blind_segments, SEGMENT_SCHEMA),
                               ("review/blind/coflight_context.parquet", context, CONTEXT_SCHEMA),
                               ("private/audit_key.parquet", keys, KEY_SCHEMA)]:
        pq.write_table(pa.Table.from_pylist(rows, schema=schema.with_metadata({b"data_mode":b"synthetic_demo", b"schema_version":b"1.0"})), path / name)
    with (path / "review/ranking_audit.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(dict.fromkeys(FIELDS, "") | {"review_id": aliases[pid]} for pid in chosen)
    (path / "review/blind/instructions.md").write_text(
        "# Независимое ревью вымышленных историй\n\n"
        "Передайте проверяющему только review/blind и копию ranking_audit.csv. Не передавайте private, метрики, ранги или каталог сценариев.\n\n"
        "Посмотрите полную историю, качество данных и доступный контекст совместных событий. Заполните reviewer, verdict, reason_ru и reviewed_at с часовым поясом.\n"
        "verdict: интересно_проверить / обычная_история / недостаточно_данных. Содержательное обоснование обязательно.\n\n"
        "Выборка стратифицирована и не случайна по всей совокупности. Её доля положительных решений не является точностью модели. "
        "Метки методов и заложенные сценарии скрыты, но знание исходного генератора может нарушить слепоту. "
        "Не рассматривайте каталог сценариев до завершения ревью. Все истории вымышлены; решения не описывают реальных людей.\n",
        encoding="utf-8")
    return dict(requested=config["size"], selected=size, strata=dict(Counter(k["stratum"] for k in keys)),
                completed=0, status="pending_independent_review", identities_authenticated=False)


def import_reviews(snapshot, reviews, output=None, artifacts_dir=Path("artifacts/t20-review")):
    snapshot, reviews = Path(snapshot).resolve(), Path(reviews).resolve()
    base = validate_snapshot(snapshot)
    if not reviews.is_file() or reviews.stat().st_size > 1024**2:
        raise ValueError("Нужен CSV ревью размером до 1 MiB")
    keys = {r["review_id"]: r for r in pq.ParquetFile(snapshot / "private/audit_key.parquet").read().to_pylist()}
    review_bytes = reviews.read_bytes()
    with io.StringIO(review_bytes.decode("utf-8-sig"), newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise ValueError("Неверные столбцы CSV ревью T20")
        rows = list(reader)
    if (len(rows) != len(keys) or len({r["review_id"] for r in rows}) != len(rows) or
            {r["review_id"] for r in rows} != set(keys)):
        raise ValueError("Повторные, неизвестные или отсутствующие review_id")
    completed = []
    for row in rows:
        if any(v is None or len(v) > 4000 for v in row.values()) or None in row:
            raise ValueError("Повреждённая/слишком длинная строка ревью")
        values = {k: v.strip() for k, v in row.items()}
        fields = [values[k] for k in FIELDS[1:]]
        if not any(fields):
            continue
        if not all(fields) or values["verdict"] not in VERDICTS:
            raise ValueError("Частично заполненное или неизвестное решение ревью")
        try:
            stamp = datetime.fromisoformat(values["reviewed_at"])
        except ValueError as exc:
            raise ValueError("reviewed_at должен быть ISO datetime с часовым поясом") from exc
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("reviewed_at должен иметь часовой пояс")
        completed.append(dict(values, stratum=keys[values["review_id"]]["stratum"]))
    request = dict(version="review-demo-1.0", base_run_id=base["run_id"],
                   base_manifest_sha256=fingerprint(snapshot / "run_manifest.json"), reviews_sha256=hashlib.sha256(review_bytes).hexdigest())
    run_id = "review-" + digest(request)[:20]
    target = Path(output).resolve() if output else Path(artifacts_dir).resolve() / run_id
    if target == snapshot or target in snapshot.parents or snapshot in target.parents or target == reviews or target in reviews.parents:
        raise ValueError("Выход ревью не должен пересекаться с входами")
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = dict(schema_version="1.0", run_id=run_id, request=request, selected=len(rows), completed=len(completed),
        pending=len(rows)-len(completed), status="review_complete_unverified_identity" if len(completed)==len(rows) else "incomplete_review",
        verdict_counts=dict(Counter(r["verdict"] for r in completed)),
        by_stratum={stratum: dict(Counter(r["verdict"] for r in completed if r["stratum"]==stratum)) for stratum in sorted({k["stratum"] for k in keys.values()})},
        reviewers=sorted({r["reviewer"] for r in completed}), independence_verified=False, ui_ready=False,
        acceptance="pending_T21_independent_signoff", population_precision=None,
        limitation_ru="Стратифицированная выборка не даёт точность по всей совокупности; личность и независимость проверяющего программно не удостоверены.")
    with OutputLock(target.parent / ".t20-review.lock", "T20 review"):
        if target.exists():
            file = target / "review_report.json"
            if not file.is_file() or file.read_text(encoding="utf-8") != json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2) + "\n" or (target / "reviews.csv").read_bytes()!=review_bytes:
                raise ValueError("Существующий отчёт ревью повреждён/относится к другой версии")
            manifest_file = target / "run_manifest.json"
            expected_files = {name:fingerprint(target/name) for name in ['review_report.json','reviews.csv']}
            if (not manifest_file.is_file() or json.loads(manifest_file.read_text(encoding='utf-8')) !=
                    dict(schema_version='1.0',run_id=run_id,request=request,files=expected_files) or
                    {p.name for p in target.iterdir()} != {'review_report.json','reviews.csv','run_manifest.json'}):
                raise ValueError("Неверный состав/манифест отчёта ревью")
            return summary, target
        temporary = Path(tempfile.mkdtemp(prefix=".t20-review-", suffix=".partial", dir=target.parent))
        try:
            (temporary / "reviews.csv").write_bytes(review_bytes)
            write_json(temporary / "review_report.json", summary)
            write_json(temporary / 'run_manifest.json', dict(schema_version='1.0',run_id=run_id,request=request,
                files={name:fingerprint(temporary/name) for name in ['review_report.json','reviews.csv']}))
            temporary.rename(target)
        finally:
            if temporary.exists():shutil.rmtree(temporary)
    return summary, target
