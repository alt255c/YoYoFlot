"""T08: externally confirmed zero-based half-open byte/character ranges."""

import yaml
import re

from .common import Emitter


def load_columns(path):
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    if not cfg.get("confirmed"):
        raise ValueError(
            "Sirena ranges are not confirmed on real data. Supply --columns with confirmed: true"
        )
    if cfg.get("indexing") != "zero-based-half-open" or cfg.get("unit") not in {
        "bytes",
        "characters",
    }:
        raise ValueError("Expected zero-based-half-open and unit bytes/characters")
    if not cfg.get("columns"):
        raise ValueError("Column ranges must not be empty")
    end = 0
    for col in cfg["columns"]:
        if (
            not isinstance(col["start"], int)
            or not isinstance(col["end"], int)
            or col["start"] < end
            or col["end"] <= col["start"]
        ):
            raise ValueError("Invalid or overlapping column range")
        end = col["end"]
    if len({c["name"] for c in cfg["columns"]}) != len(cfg["columns"]):
        raise ValueError("Duplicate column names")
    return cfg


def read(path, source_id="sirena_fixed", batch_size=1000, columns=None, **kwargs):
    if columns is None:
        raise ValueError("Sirena requires a confirmed columns configuration")
    cfg = load_columns(columns)
    e = Emitter(path, source_id, batch_size)
    end = cfg["columns"][-1]["end"]
    encoding = cfg["encoding"]
    with open(path, "rb") as f:
        for line_number, line in enumerate(f, 1):
            line = line.rstrip(b"\r\n")
            if line_number <= cfg.get("header_lines", 1):
                if (
                    line_number == 1
                    and cfg.get("expected_header") is not None
                    and line.decode(encoding) != cfg["expected_header"]
                ):
                    raise ValueError("Sirena header changed; reconfirm column ranges")
                continue
            e.mark("input_rows")
            value = line if cfg["unit"] == "bytes" else line.decode(encoding)
            loc = f"line:{line_number}"
            if not value:
                e.error(loc, "", "blank_row", "info")
            elif len(value) < end:
                e.error(loc, line.decode(encoding), "short_row")
            elif value[end:].strip():
                e.error(loc, line.decode(encoding), "unexpected_tail")
            else:
                obj = {}
                for col in cfg["columns"]:
                    cell = value[col["start"] : col["end"]]
                    obj[col["name"]] = (
                        cell.decode(encoding) if isinstance(cell, bytes) else cell
                    ).strip() or None
                obj["_raw_line"] = line.decode(encoding)
                obj["BookingCode"] = obj.get("Code")
                oid = e.person(loc, obj)
                e.identifier(
                    loc + "/document", obj, oid, "document", obj.get("TravelDoc")
                )
                e.identifier(loc + "/ticket", obj, oid, "ticket", obj.get("e-Ticket"))
                cards = list(
                    re.finditer(
                        r"FF#([A-Z]{2})\s+([0-9]+)", obj.get("PaxAdditionalInfo") or ""
                    )
                )
                card_id = None
                for ci, match in enumerate(cards):
                    card_id = e.identifier(
                        loc + f"/loyalty/{ci}",
                        match.group(0),
                        oid,
                        "loyalty_card",
                        match[2],
                        match[1],
                    )
                e.travel(
                    loc + "/travel",
                    obj,
                    oid,
                    "reservation_record",
                    loyalty_identifier_id=card_id if len(cards) == 1 else None,
                )
            yield from e.flush()
    yield from e.flush(True)
