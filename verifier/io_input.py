"""Read CSV / Excel lists and detect the email column automatically."""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_EMAIL_HINT = re.compile(r"e[\s_-]?mail|^mail$|البريد|ايميل|إيميل|الايميل", re.I)
_EMAIL_LIKE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_SPLIT_MULTI = re.compile(r"[;,\s/|]+")


@dataclass
class InputList:
    columns: List[str]
    email_column: str
    rows: List[Tuple[int, str, Dict[str, str]]] = field(default_factory=list)  # (row_no, raw_email, extras)
    sha256: str = ""


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_csv(path: Path) -> List[List[str]]:
    raw = path.read_bytes()
    text = None
    for enc in ("utf-8-sig", "utf-16", "cp1256", "latin-1"):
        try:
            text = raw.decode(enc)
            if enc == "utf-16" and "\x00" in text:
                continue
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("utf-8", errors="replace")
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    return [row for row in csv.reader(io.StringIO(text), delimiter=delimiter)]


def _read_xlsx(path: Path) -> List[List[str]]:
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    out: List[List[str]] = []
    for row in ws.iter_rows(values_only=True):
        out.append(["" if v is None else str(v).strip() for v in row])
    wb.close()
    return out


def read_rows(path: Path) -> List[List[str]]:
    ext = path.suffix.lower()
    if ext in (".csv", ".txt", ".tsv"):
        return _read_csv(path)
    if ext in (".xlsx", ".xlsm"):
        return _read_xlsx(path)
    if ext == ".xls":
        raise ValueError("Old .xls format is not supported. Open it in Excel and save as .xlsx or .csv.")
    raise ValueError("Unsupported file type '%s'. Use .csv or .xlsx." % ext)


def _looks_like_header(row: List[str]) -> bool:
    return not any(_EMAIL_LIKE.search(c or "") for c in row)


def load_list(path: Path, email_column: Optional[str] = None) -> InputList:
    path = Path(path)
    rows = read_rows(path)
    # drop leading empty rows
    while rows and not any(c.strip() for c in rows[0]):
        rows.pop(0)
    if not rows:
        raise ValueError("The file is empty.")

    width = max(len(r) for r in rows)
    if _looks_like_header(rows[0]):
        header = [(c.strip() or "column_%d" % (i + 1)) for i, c in enumerate(rows[0])]
        header += ["column_%d" % (i + 1) for i in range(len(header), width)]
        body = rows[1:]
        first_data_row = 2
    else:
        header = ["column_%d" % (i + 1) for i in range(width)]
        body = rows
        first_data_row = 1

    # make header names unique
    seen: Dict[str, int] = {}
    for i, name in enumerate(header):
        if name in seen:
            seen[name] += 1
            header[i] = "%s_%d" % (name, seen[name])
        else:
            seen[name] = 1

    idx = _detect_email_column(header, body, email_column)
    col_name = header[idx]

    data: List[Tuple[int, str, Dict[str, str]]] = []
    for offset, row in enumerate(body):
        row_no = first_data_row + offset
        row = row + [""] * (width - len(row))
        raw = row[idx]
        if not any(c.strip() for c in row):
            continue
        extras = {header[i]: row[i] for i in range(width) if i != idx}
        # a cell holding several addresses becomes several rows
        if raw.count("@") > 1:
            parts = [p for p in _SPLIT_MULTI.split(raw) if "@" in p]
            for p in parts:
                data.append((row_no, p, extras))
        else:
            data.append((row_no, raw, extras))

    return InputList(columns=header, email_column=col_name, rows=data, sha256=file_sha256(path))


def _detect_email_column(header: List[str], body: List[List[str]], wanted: Optional[str]) -> int:
    if wanted:
        for i, h in enumerate(header):
            if h.strip().lower() == wanted.strip().lower():
                return i
        raise ValueError("Column '%s' not found. Columns: %s" % (wanted, ", ".join(header)))

    sample = body[:300]
    scores = []
    for i in range(len(header)):
        vals = [r[i] for r in sample if i < len(r) and r[i].strip()]
        ratio = (sum(1 for v in vals if "@" in v) / len(vals)) if vals else 0.0
        scores.append(ratio)
    hinted = [i for i, h in enumerate(header) if _EMAIL_HINT.search(h)]
    if hinted:
        best_hint = max(hinted, key=lambda i: scores[i])
        if scores[best_hint] > 0.2 or max(scores) == 0:
            return best_hint
    best = max(range(len(header)), key=lambda i: scores[i])
    if scores[best] == 0:
        raise ValueError("Could not find an email column. Columns: %s" % ", ".join(header))
    return best
