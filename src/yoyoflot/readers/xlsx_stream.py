"""Small, read-only OOXML worksheet reader used by the T10 ingestion path.

It deliberately exposes cells as they occur in sheet XML instead of loading a
workbook through openpyxl.  This keeps memory bounded for the large ZIP source
while retaining formula text, cached values, and Excel date styles.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import PurePosixPath
import posixpath
from zipfile import ZipFile
import re
import xml.etree.ElementTree as ET

NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
      "pr": "http://schemas.openxmlformats.org/package/2006/relationships"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CELL_REF = re.compile(r"^[A-Z]+\d+$")

@dataclass
class Cell:
    value: object
    data_type: str | None = None
    formula: str | None = None
    xml_value: str | None = None

class WorkbookStream:
    def __init__(self, fileobj):
        self._zip = ZipFile(fileobj)
        self._closed = False
        self._date1904 = False
        self._shared = self._load_shared_strings()
        self._styles = self._load_styles()
        self._load_workbook()

    def _xml(self, name):
        return ET.fromstring(self._zip.read(name))

    def _load_shared_strings(self):
        try: root = self._xml("xl/sharedStrings.xml")
        except KeyError: return []
        result = []
        for si in root.findall("m:si", NS):
            result.append("".join(t.text or "" for t in si.iter("{" + NS["m"] + "}t")))
        return result

    def _load_styles(self):
        try: root = self._xml("xl/styles.xml")
        except KeyError: return set()
        custom = {}
        nums = root.find("m:numFmts", NS)
        if nums is not None:
            for n in nums:
                custom[int(n.attrib["numFmtId"])] = n.attrib.get("formatCode", "")
        builtin = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}
        date_styles = set()
        xfs = root.find("m:cellXfs", NS)
        if xfs is not None:
            for i, xf in enumerate(xfs):
                fmt = int(xf.attrib.get("numFmtId", 0))
                code = custom.get(fmt, "")
                if fmt in builtin or any(c in code.lower() for c in ("yy", "dd", "mm", "hh")):
                    date_styles.add(i)
        return date_styles

    def _load_workbook(self):
        root = self._xml("xl/workbook.xml")
        pr = root.find("m:workbookPr", NS)
        self._date1904 = pr is not None and pr.attrib.get("date1904", "0").lower() in ("1", "true")
        rels = {}
        try:
            rr = ET.fromstring(self._zip.read("xl/_rels/workbook.xml.rels"))
            for rel in rr.findall("pr:Relationship", NS): rels[rel.attrib["Id"]] = rel.attrib["Target"]
        except KeyError: pass
        self.sheets = []
        for sheet in root.findall("m:sheets/m:sheet", NS):
            target = rels[sheet.attrib["{" + REL_NS + "}id"]]
            path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join("xl", target))
            self.sheets.append((sheet.attrib["name"], path))

    def _value(self, typ, raw, style):
        if raw is None: return None
        if typ == "s":
            try: return self._shared[int(raw)]
            except (ValueError, IndexError): return raw
        if typ == "inlineStr": return raw
        if typ == "b": return raw == "1"
        if typ == "e": return raw
        if typ == "str": return raw
        try:
            number = float(raw)
            if number.is_integer(): number = int(number)
            if style in self._styles:
                epoch = datetime(1904, 1, 1) if self._date1904 else datetime(1899, 12, 30)
                dt = epoch + timedelta(days=float(raw))
                return dt.date() if dt.time() == datetime.min.time() else dt
            return number
        except ValueError: return raw

    def rows(self, sheet_path):
        with self._zip.open(sheet_path) as stream:
            for _, row in ET.iterparse(stream, events=("end",)):
                if row.tag != "{" + NS["m"] + "}row": continue
                cells = {}
                for node in row.findall("m:c", NS):
                    ref = node.attrib.get("r")
                    if not ref or not CELL_REF.match(ref): continue
                    f = node.find("m:f", NS)
                    v = node.find("m:v", NS)
                    inline = node.find("m:is", NS)
                    raw = ("".join(t.text or "" for t in inline.iter("{" + NS["m"] + "}t")) if inline is not None else (v.text if v is not None else None))
                    typ = node.attrib.get("t")
                    cells[ref] = Cell(self._value(typ, raw, int(node.attrib.get("s", 0))), typ, f.text if f is not None else None, raw)
                yield int(row.attrib.get("r", 0)), cells
                row.clear()

    def close(self):
        if not self._closed:
            self._zip.close(); self._closed = True
