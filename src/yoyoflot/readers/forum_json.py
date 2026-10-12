"""T06: one profile at a time; literal Loyality Programm is supported."""

from contextlib import closing
import sqlite3
import tempfile

import ijson

from .common import Emitter, pick, text


class StrictObject(dict):
    def __init__(self):
        super().__init__()
        self.duplicate_keys = []

    def __setitem__(self, key, value):
        if key in self:
            # Do not raise inside the C backend callback; validate before yielding.
            self.duplicate_keys.append(key)
        super().__setitem__(key, value)


def _reject_duplicates(obj):
    stack = [obj]
    while stack:
        current = stack.pop()
        if isinstance(current, StrictObject) and current.duplicate_keys:
            raise ValueError(f"Duplicate JSON key: {current.duplicate_keys[0]}")
        if isinstance(current, dict):
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)


def _profiles(path):
    with open(path, "rb") as f:
        parser = ijson.parse(f)
        prefix, event, _ = next(parser)
        if event == "start_array":
            shape, prefix = "array", ""
        elif event == "start_map":
            shape = None
            for p, ev, v in parser:
                if p == "" and ev == "map_key" and v == "Forum Profiles":
                    p, ev, _ = next(parser)
                    shape, prefix = (
                        (
                            "array"
                            if ev == "start_array"
                            else "map"
                            if ev == "start_map"
                            else None
                        ),
                        p,
                    )
                    break
            if shape is None:
                raise ValueError("Expected Forum Profiles mapping/array or root array")
        else:
            raise ValueError("Unsupported JSON root")
    with open(path, "rb") as f:
        if shape == "array":
            for i, obj in enumerate(
                ijson.items(
                    f, prefix + ".item" if prefix else "item", map_type=StrictObject
                )
            ):
                yield f"/{prefix}/{i}", obj
        else:
            with (
                tempfile.TemporaryDirectory() as tmp,
                closing(sqlite3.connect(tmp + "/keys.sqlite")) as db,
            ):
                db.execute("CREATE TABLE seen (key TEXT PRIMARY KEY)")
                for key, obj in ijson.kvitems(f, prefix, map_type=StrictObject):
                    try:
                        db.execute("INSERT INTO seen VALUES (?)", (key,))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(f"Duplicate JSON profile key: {key}") from exc
                    yield (
                        f"/{prefix}/{str(key).replace('~', '~0').replace('/', '~1')}",
                        obj,
                    )


def _entries(value):
    if value is None:
        return []
    if isinstance(value, list):
        return list(enumerate(value))
    if isinstance(value, dict):
        return list(value.items())
    raise ValueError("Expected nested mapping or list")


def read(path, source_id="forum_json", batch_size=1000, **kwargs):
    e = Emitter(path, source_id, batch_size)
    with closing(_profiles(path)) as profiles:
        for loc, obj in profiles:
            _reject_duplicates(obj)
            e.mark("profiles")
            if not isinstance(obj, dict):
                e.error(loc, obj, "profile_not_mapping")
                yield from e.flush()
                continue
            oid = e.person(loc, obj)

            def documents(value, path):
                if value is None:
                    return
                if isinstance(value, dict) and "Passports" in value:
                    e.mark("document_containers")
                    if value["Passports"] is None:
                        e.mark("empty_document_containers")
                    yield from documents(value["Passports"], path + "/Passports")
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, dict) and "Passports" in item:
                            yield from documents(item, path + f"/{index}")
                        else:
                            yield path + f"/{index}", item
                elif isinstance(value, dict) and not set(value).intersection(
                    {
                        "Number",
                        "number",
                        "Passport",
                        "Document",
                        "Passport Number",
                        "PassportNumber",
                    }
                ):
                    for key, item in value.items():
                        yield (
                            path + "/" + str(key).replace("~", "~0").replace("/", "~1"),
                            item,
                        )
                else:
                    yield path, value

            document_field = (
                "Travel Documents" if "Travel Documents" in obj else "Passports"
            )
            e.mark("documents", 0)
            for path_suffix, item in documents(obj.get(document_field), document_field):
                e.mark("documents")
                value = (
                    pick(
                        item,
                        "Number",
                        "number",
                        "Passport",
                        "Document",
                        "Passport Number",
                        "PassportNumber",
                    )
                    if isinstance(item, dict)
                    else item
                )
                iloc = loc + "/" + path_suffix
                if isinstance(value, (dict, list)) or not text(value):
                    e.error(iloc, item, "identifier_without_scalar_number")
                else:
                    e.identifier(iloc, item, oid, "document", value)
                yield from e.flush()
            loyalty_field = (
                "Loyality Programm"
                if "Loyality Programm" in obj
                else "Loyalty Programme"
            )
            loyalty = obj.get(loyalty_field)
            try:
                card_entries = _entries(loyalty)
            except ValueError:
                e.error(loc + "/" + loyalty_field, loyalty, "invalid_nested_structure")
                card_entries = []
            for key, item in card_entries:
                e.mark("cards")
                if isinstance(item, dict):
                    value = pick(item, "Number", "number", "CardNumber")
                    programme = pick(
                        item,
                        "Programme",
                        "Program",
                        "programme",
                        "programm",
                        "bonusprogramm",
                    )
                    if programme is None and isinstance(loyalty, dict):
                        programme = key
                else:
                    value = item
                    programme = key if isinstance(loyalty, dict) else None
                iloc = f"{loc}/{loyalty_field}/{key}"
                if isinstance(value, (dict, list)) or not text(value):
                    e.error(iloc, item, "identifier_without_scalar_number")
                else:
                    e.identifier(iloc, item, oid, "loyalty_card", value, programme)
                yield from e.flush()
            flights = obj.get("Registered Flights")
            try:
                entries = _entries(flights)
            except ValueError:
                e.error(
                    loc + "/Registered Flights", flights, "invalid_flights_structure"
                )
                entries = []
            for key, flight in entries:
                e.mark("flights")
                floc = f"{loc}/Registered Flights/{key}"
                if not isinstance(flight, dict):
                    e.error(floc, flight, "flight_not_mapping")
                else:
                    e.travel(floc, flight, oid, "registered_flight")
                yield from e.flush()
            yield from e.flush()
    yield from e.flush(True)
