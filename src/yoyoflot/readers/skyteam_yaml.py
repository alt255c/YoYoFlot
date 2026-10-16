"""T09: YAML event parser, bounded flight objects and duplicate key rejection."""

from contextlib import closing
import sqlite3
import tempfile

import yaml
from yaml import events as Y

from .common import Emitter, pick, text, split_card


class Events:
    def __init__(self, stream, max_nodes=100000):
        self.it = iter(
            yaml.parse(stream, Loader=getattr(yaml, "CSafeLoader", yaml.SafeLoader))
        )
        self.max_nodes = max_nodes
        self.nodes = 0

    def next(self):
        return next(self.it)

    def expect(self, kind):
        event = self.next()
        if not isinstance(event, kind):
            raise ValueError(f"Expected {kind.__name__}, got {type(event).__name__}")
        return event

    def node(self, event=None, depth=0):
        event = event or self.next()
        self.nodes += 1
        if self.nodes > self.max_nodes or depth > 50:
            raise ValueError("YAML flight exceeds configured node/depth budget")
        if isinstance(event, Y.AliasEvent) or getattr(event, "anchor", None):
            raise ValueError("YAML anchors/aliases are unsupported; expand explicitly")
        if isinstance(event, Y.ScalarEvent):
            # All scalars remain strings, including date keys and zero-prefixed IDs.
            if len(event.value) > 2_000_000:
                raise ValueError("YAML scalar exceeds 2 MB character budget")
            return event.value
        if isinstance(event, Y.SequenceStartEvent):
            out = []
            while True:
                current = self.next()
                if isinstance(current, Y.SequenceEndEvent):
                    return out
                out.append(self.node(current, depth + 1))
        if isinstance(event, Y.MappingStartEvent):
            out = {}
            while True:
                current = self.next()
                if isinstance(current, Y.MappingEndEvent):
                    return out
                key = self.node(current, depth + 1)
                if not isinstance(key, str) or key in out:
                    raise ValueError(f"Duplicate/non-scalar YAML key: {key!r}")
                out[key] = self.node(depth=depth + 1)
        raise ValueError(f"Unsupported YAML event {type(event).__name__}")


def _flights(path, max_nodes):
    # Disk index bounds memory for date/flight duplicate tracking over the full source.
    with (
        tempfile.TemporaryDirectory() as tmp,
        closing(sqlite3.connect(tmp + "/keys.sqlite")) as db,
        open(path, encoding="utf-8-sig") as stream,
    ):
        db.execute("CREATE TABLE seen (scope TEXT, key TEXT, PRIMARY KEY(scope,key))")
        p = Events(stream, max_nodes)
        p.expect(Y.StreamStartEvent)
        doc = 0
        while True:
            event = p.next()
            if isinstance(event, Y.StreamEndEvent):
                return
            if not isinstance(event, Y.DocumentStartEvent):
                raise ValueError("Expected YAML document")
            doc += 1
            p.expect(Y.MappingStartEvent)
            while True:
                date_event = p.next()
                if isinstance(date_event, Y.MappingEndEvent):
                    break
                if not isinstance(date_event, Y.ScalarEvent):
                    raise ValueError("Date key must be scalar")
                day = date_event.value
                try:
                    db.execute("INSERT INTO seen VALUES (?, ?)", (f"doc:{doc}", day))
                except sqlite3.IntegrityError as exc:
                    raise ValueError(f"Duplicate YAML date key {day}") from exc
                p.expect(Y.MappingStartEvent)
                while True:
                    flight_event = p.next()
                    if isinstance(flight_event, Y.MappingEndEvent):
                        break
                    if not isinstance(flight_event, Y.ScalarEvent):
                        raise ValueError("Flight key must be scalar")
                    flight = flight_event.value
                    try:
                        db.execute(
                            "INSERT INTO seen VALUES (?, ?)", (f"{doc}/{day}", flight)
                        )
                    except sqlite3.IntegrityError as exc:
                        raise ValueError(f"Duplicate YAML flight key {flight}") from exc
                    p.nodes = 0
                    obj = p.node()
                    yield f"document:{doc}/{day}/{flight}", day, flight, obj
            p.expect(Y.DocumentEndEvent)


def _cards(ff):
    if ff in (None, "", "null", "~"):
        return
    if isinstance(ff, dict):
        for programme, cards in ff.items():
            code, card_number = split_card(programme)
            if code is not None:
                yield (
                    programme,
                    code,
                    card_number,
                    {**cards, "_card_raw": programme}
                    if isinstance(cards, dict)
                    else {"_card_raw": programme, "_payload": cards},
                )
                continue
            for index, card in enumerate(cards if isinstance(cards, list) else [cards]):
                if isinstance(card, dict):
                    number = pick(card, "Number", "number", "CardNumber")
                    if number is None and len(card) == 1:
                        number, nested = next(iter(card.items()))
                        card = nested if isinstance(nested, dict) else card
                    yield f"{programme}/{index}", programme, number, card
                else:
                    yield f"{programme}/{index}", programme, card, {}
    elif isinstance(ff, list):
        for i, card in enumerate(ff):
            if not isinstance(card, dict):
                yield str(i), None, None, {"_raw": card}
            elif pick(card, "Programme", "Program", "programme") is not None:
                yield (
                    str(i),
                    pick(card, "Programme", "Program", "programme"),
                    pick(card, "Number", "number", "CardNumber"),
                    card,
                )
            elif len(card) == 1:
                programme, number = next(iter(card.items()))
                yield str(i), programme, number, card
            else:
                yield str(i), None, None, card
    else:
        raise ValueError("FF must be mapping/list/null")


def read(path, source_id="skyteam_yaml", batch_size=1000, max_nodes=100000, **kwargs):
    e = Emitter(path, source_id, batch_size)
    with closing(_flights(path, max_nodes)) as flights:
        for loc, day, flight, obj in flights:
            e.mark("flights")
            if not isinstance(obj, dict):
                e.error(loc, obj, "flight_not_mapping")
                yield from e.flush()
                continue
            if "FF" not in obj:
                e.error(loc, obj, "missing_FF")
            try:
                cards = list(_cards(obj.get("FF")))
            except ValueError:
                cards = []
                e.error(loc, obj, "invalid_FF")
            if not cards:
                e.error(loc, obj, "empty_FF", "info")
            for key, programme, number, payload in cards:
                e.mark("cards")
                cloc = f"{loc}/FF/{key}"
                if (
                    not text(programme)
                    or not text(number)
                    or isinstance(number, (dict, list))
                ):
                    e.error(cloc, payload, "card_without_programme_or_number")
                else:
                    oid = e.person(cloc, payload)
                    card_id = e.identifier(
                        cloc + "/card", payload, oid, "loyalty_card", number, programme
                    )
                    flight_fields = {k: v for k, v in obj.items() if k != "FF"}
                    e.travel(
                        cloc + "/travel",
                        {
                            **flight_fields,
                            **payload,
                            "FlightDate": day,
                            "FlightNumber": flight,
                        },
                        oid,
                        "exchange_record",
                        loyalty_identifier_id=card_id,
                    )
                yield from e.flush()
            yield from e.flush()
    yield from e.flush(True)
