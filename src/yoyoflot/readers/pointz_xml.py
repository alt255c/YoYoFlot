"""T07: iterparse with namespace-neutral tags and safe preceding-sibling cleanup."""

from lxml import etree

from .common import Emitter, pick, split_card


def _tag(element):
    return etree.QName(element).localname


def _object(element):
    result = dict(element.attrib)
    for child in element:
        key = _tag(child)
        value = _object(child) if len(child) or child.attrib else child.text
        if key in result:
            current = result[key]
            result[key] = (
                current + [value] if isinstance(current, list) else [current, value]
            )
        else:
            result[key] = value
    return result


def read(path, source_id="pointz_xml", batch_size=1000, **kwargs):
    e = Emitter(path, source_id, batch_size)
    number = 0
    with open(path, "rb") as stream:
        for event, user in etree.iterparse(
            stream,
            events=("end",),
            resolve_entities=False,
            no_network=True,
            load_dtd=False,
            huge_tree=False,
        ):
            if _tag(user) != "user":
                continue
            e.mark("users")
            number += 1
            loc = f"/PointzAggregatorUsers/user[{number}]"
            obj = _object(user)
            oid = e.person(loc, obj)
            card_ids = {}
            # Select cards and activities in document order, preserving actual nesting.
            for i, card in enumerate(x for x in user.iter() if _tag(x) == "card"):
                e.mark("cards")
                cobj = _object(card)
                value = pick(cobj, "number", "Number", "id", "CardNumber")
                if value is None and card.text and card.text.strip():
                    value = card.text.strip()
                if value is None:
                    e.error(f"{loc}/cards/card[{i + 1}]", cobj, "card_without_number")
                else:
                    code, number_value = split_card(value)
                    card_ids[card] = e.identifier(
                        f"{loc}/cards/card[{i + 1}]",
                        cobj,
                        oid,
                        "loyalty_card",
                        number_value,
                        code or pick(cobj, "bonusprogramm", "programme"),
                    )
                yield from e.flush()
            # Activity locator includes a relative structural XPath, including card ancestry.
            for activity in (x for x in user.iter() if _tag(x) == "activity"):
                e.mark("activities")
                chain, cur = [], activity
                while cur is not user:
                    parent = cur.getparent()
                    peers = [x for x in parent if _tag(x) == _tag(cur)]
                    chain.append(f"{_tag(cur)}[{peers.index(cur) + 1}]")
                    cur = parent
                aloc = loc + "/" + "/".join(reversed(chain))
                aobj = _object(activity)
                ancestor = activity.getparent()
                while (
                    ancestor is not None
                    and ancestor is not user
                    and _tag(ancestor) != "card"
                ):
                    ancestor = ancestor.getparent()
                e.travel(
                    aloc,
                    aobj,
                    oid,
                    "loyalty_activity",
                    loyalty_identifier_id=card_ids.get(ancestor),
                )
                yield from e.flush()
            # Keep the current node attached: libxml2 may still touch it after
            # the end event. Only completed preceding siblings may be removed.
            user.clear(keep_tail=True)
            while user.getprevious() is not None:
                del user.getparent()[0]
            yield from e.flush()
    if number == 0:
        raise ValueError("No user elements found; confirm XML schema")
    yield from e.flush(True)
