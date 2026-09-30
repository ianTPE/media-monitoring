"""Read the team's Excel search checklist without an extra Python dependency."""

from collections import defaultdict
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit
from xml.etree import ElementTree as ET
from zipfile import ZipFile

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
NS = {"m": MAIN}


def _sheet_path(z):
    workbook = ET.fromstring(z.read("xl/workbook.xml"))
    sheet = workbook.find("m:sheets/m:sheet", NS)
    if sheet is None:
        raise ValueError("Excel 沒有工作表")
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    targets = {r.attrib["Id"]: r.attrib["Target"] for r in rels}
    target = targets[sheet.attrib[f"{{{REL}}}id"]]
    return target.lstrip("/") if target.startswith("/") else "xl/" + target


def _cell_values(z, sheet_path):
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
        strings = ["".join(t.text or "" for t in item.findall(".//m:t", NS))
                   for item in root.findall("m:si", NS)]
    root = ET.fromstring(z.read(sheet_path))
    values = {}
    for cell in root.findall("m:sheetData/m:row/m:c", NS):
        value = cell.find("m:v", NS)
        inline = cell.find("m:is", NS)
        if value is not None and value.text:
            text = strings[int(value.text)] if cell.attrib.get("t") == "s" else value.text
        elif inline is not None:
            text = "".join(t.text or "" for t in inline.findall(".//m:t", NS))
        else:
            continue
        if text.strip():
            values[cell.attrib["r"]] = text.strip()
    return root, values


def _links(z, sheet_path, root):
    rel_path = str(Path(sheet_path).parent / "_rels" / (Path(sheet_path).name + ".rels"))
    if rel_path not in z.namelist():
        return {}
    rels = ET.fromstring(z.read(rel_path))
    targets = {r.attrib["Id"]: r.attrib.get("Target", "") for r in rels}
    return {h.attrib["ref"]: targets.get(h.attrib.get(f"{{{REL}}}id"), "")
            for h in root.findall("m:hyperlinks/m:hyperlink", NS)}


def _search(label, url):
    host = urlsplit(url).hostname or ""
    if host in ("www.google.com", "google.com"):
        return parse_qs(urlsplit(url).query).get("q", [label])[0].strip() or label
    if host in ("www.ctee.com.tw", "ctee.com.tw"):
        term = unquote(urlsplit(url).path).rstrip("/").split("/")[-1]
        return f"{term} site:ctee.com.tw" if term else None
    if host == "money.udn.com":
        parts = unquote(urlsplit(url).path).strip("/").split("/")
        term = parts[3] if len(parts) >= 4 and parts[:3] == ["search", "result", "1001"] else ""
        return f"{term} site:money.udn.com" if term else None
    return None


def read_searches(path):
    """Return {spreadsheet client name: [{query, label}, ...]}.

    Only linked C:H cells are searches. Repeated client blocks and repeated
    terms are combined; notes, formulas and paused rows are ignored.
    """
    groups = defaultdict(list)
    with ZipFile(path) as z:
        sheet_path = _sheet_path(z)
        root, cells = _cell_values(z, sheet_path)
        links = _links(z, sheet_path, root)
        client = None
        seen = defaultdict(set)
        for row in root.findall("m:sheetData/m:row", NS):
            number = row.attrib["r"]
            head = cells.get(f"B{number}")
            if head:
                client = head if not head.startswith("※") else None
            if not client:
                continue
            for column in "CDEFGH":
                ref = f"{column}{number}"
                label, url = cells.get(ref), links.get(ref)
                if not label or not url:
                    continue
                query = _search(label, url)
                if query and query not in seen[client]:
                    groups[client].append({"query": query, "label": label})
                    seen[client].add(query)
    return dict(groups)
