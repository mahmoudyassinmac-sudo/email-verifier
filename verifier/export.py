"""Excel (Summary / Valid / Risky / Invalid) and Valid-only CSV exports."""
from __future__ import annotations

import csv
import json
import time
from pathlib import Path
from typing import Any, Dict, List

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.workbook.properties import CalcProperties

from .models import DUPLICATE, INVALID, PENDING, RISKY, STATUS_LABEL, VALID
from .storage import Storage

FONT = "Arial"
BASE_COLUMNS = ["email", "original_email", "status", "reason", "domain", "mx_host",
                "is_role", "is_disposable", "typo_fixed", "smtp_response"]
SHEETS = [("Valid", VALID), ("Risky", RISKY), ("Invalid", INVALID)]

F_TITLE = Font(name=FONT, size=14, bold=True)
F_BOLD = Font(name=FONT, bold=True)
F_HEAD = Font(name=FONT, bold=True, color="FFFFFF")
F_NORM = Font(name=FONT)
F_NOTE = Font(name=FONT, italic=True, color="666666", size=9)
FILL_HEAD = PatternFill("solid", fgColor="1F2937")
FILL = {
    VALID: PatternFill("solid", fgColor="D1FAE5"),
    RISKY: PatternFill("solid", fgColor="FEF3C7"),
    INVALID: PatternFill("solid", fgColor="FEE2E2"),
}


def _extra_columns(job: Dict[str, Any]) -> List[str]:
    cols = json.loads(job.get("columns_json") or "[]")
    return [c for c in cols if c != job.get("email_column")]


def _cell(ws, value, font=F_NORM, fill=None, fmt=None, align=None, comment=None):
    c = WriteOnlyCell(ws, value=value)
    c.font = font
    if fill is not None:
        c.fill = fill
    if fmt:
        c.number_format = fmt
    if align:
        c.alignment = align
    if comment:
        c.comment = Comment(comment, "Verifier")
    return c


def _row_values(e: Dict[str, Any], extras: List[str]) -> List[Any]:
    smtp = ""
    if e.get("smtp_code") or e.get("smtp_message"):
        smtp = ("%s %s" % (e.get("smtp_code") or "", e.get("smtp_message") or "")).strip()
    vals: List[Any] = [
        e.get("email") or "",
        e.get("original_email") or "",
        STATUS_LABEL.get(e["status"], e["status"]),
        e.get("reason") or "",
        e.get("domain") or "",
        e.get("mx_host") or "",
        "Yes" if e.get("is_role") else "No",
        "Yes" if e.get("is_disposable") else "No",
        e.get("typo_fixed") or "",
        smtp,
    ]
    ex = json.loads(e.get("extra_json") or "{}")
    vals.extend(ex.get(c, "") for c in extras)
    return vals


def export_excel(store: Storage, job: Dict[str, Any], path: Path) -> Path:
    job_id = int(job["id"])
    extras = _extra_columns(job)
    headers = BASE_COLUMNS + extras
    counts = store.counts(job_id)
    reasons = [r for r in store.reason_breakdown(job_id) if r["status"] in (VALID, RISKY, INVALID)]
    domains = store.top_domains(job_id, 25)

    wb = Workbook(write_only=True)
    wb.calculation = CalcProperties(fullCalcOnLoad=True)

    # ------------------------------------------------------------ Summary
    ws = wb.create_sheet("Summary")
    n = [0]

    def add(cells=None):
        ws.append(cells or [])
        n[0] += 1
        return n[0]

    for col, width in zip("ABCDEFG", (44, 52, 14, 14, 14, 14, 12)):
        ws.column_dimensions[col].width = width
    pct = "0.0%"
    num = "#,##0"
    add([_cell(ws, "Email Verification Summary", F_TITLE)])
    add([_cell(ws, "Job", F_BOLD), _cell(ws, job["name"])])
    add([_cell(ws, "Exported", F_BOLD), _cell(ws, time.strftime("%Y-%m-%d %H:%M"))])
    add([_cell(ws, "Job status", F_BOLD), _cell(ws, job["status"].capitalize())])
    add([])
    # rows 6-10
    add([_cell(ws, h, F_HEAD, FILL_HEAD) for h in ("Category", "Count", "% of unique emails")])
    for i, (label, status) in enumerate(SHEETS):
        r = 7 + i
        add([
            _cell(ws, label, F_BOLD, FILL[status]),
            _cell(ws, "=COUNTA(%s!C:C)-1" % label, fmt=num),
            _cell(ws, "=IF($B$10=0,0,B%d/$B$10)" % r, fmt=pct),
        ])
    add([_cell(ws, "Total unique emails", F_BOLD), _cell(ws, "=SUM(B7:B9)", F_BOLD, fmt=num),
               _cell(ws, "=SUM(C7:C9)", F_BOLD, fmt=pct)])
    add([])
    # rows 12+
    add([_cell(ws, "Duplicates removed", F_BOLD),
               _cell(ws, counts.get(DUPLICATE, 0), fmt=num),
               _cell(ws, "Engine count (duplicates are not listed in the sheets)", F_NOTE)])
    add([_cell(ws, "Input rows", F_BOLD), _cell(ws, counts.get("total", 0), fmt=num),
               _cell(ws, "Engine count", F_NOTE)])
    if counts.get(PENDING, 0):
        add([_cell(ws, "Not processed yet (job unfinished)", F_BOLD),
                   _cell(ws, counts.get(PENDING, 0), fmt=num),
                   _cell(ws, "Engine count - export again when the job completes", F_NOTE)])
    add([_cell(ws, "Typos fixed", F_BOLD),
               _cell(ws, "=" + "+".join('COUNTIF(%s!I$2:I$1048576,"?*")' % s for s, _ in SHEETS), fmt=num)])
    add([_cell(ws, "Role addresses", F_BOLD),
               _cell(ws, "=" + "+".join('COUNTIF(%s!G$2:G$1048576,"Yes")' % s for s, _ in SHEETS), fmt=num)])
    add([])

    # reason breakdown
    add([_cell(ws, "Breakdown by reason", F_TITLE)])
    add([_cell(ws, h, F_HEAD, FILL_HEAD) for h in ("Category", "Reason", "Count", "% of category")])
    cat_row = {VALID: 7, RISKY: 8, INVALID: 9}
    for r in reasons:
        row = n[0] + 1
        sheet = STATUS_LABEL[r["status"]]
        reason = (r["reason"] or "").replace("*", "").replace("?", "").replace("~", "")
        add([
            _cell(ws, sheet, fill=FILL[r["status"]]),
            _cell(ws, reason),
            _cell(ws, '=COUNTIF(%s!D$2:D$1048576,B%d)' % (sheet, row), fmt=num),
            _cell(ws, "=IF($B$%d=0,0,C%d/$B$%d)" % (cat_row[r["status"]], row, cat_row[r["status"]]), fmt=pct),
        ])
    add([])

    # top domains
    add([_cell(ws, "Top domains", F_TITLE)])
    add([_cell(ws, h, F_HEAD, FILL_HEAD)
         for h in ("Domain", "Total", "Valid", "Risky", "Invalid", "Valid %")])
    for d in domains:
        row = n[0] + 1
        add([
            _cell(ws, d["domain"]),
            _cell(ws, "=SUM(C%d:E%d)" % (row, row), fmt=num),
            _cell(ws, '=COUNTIF(Valid!E$2:E$1048576,A%d)' % row, fmt=num),
            _cell(ws, '=COUNTIF(Risky!E$2:E$1048576,A%d)' % row, fmt=num),
            _cell(ws, '=COUNTIF(Invalid!E$2:E$1048576,A%d)' % row, fmt=num),
            _cell(ws, "=IF(B%d=0,0,C%d/B%d)" % (row, row, row), fmt=pct),
        ])

    # ------------------------------------------------------------ data sheets
    widths = [34, 34, 10, 52, 24, 34, 9, 13, 30, 50] + [18] * len(extras)
    for label, status in SHEETS:
        sh = wb.create_sheet(label)
        for i, w in enumerate(widths):
            sh.column_dimensions[_col(i)].width = w
        sh.freeze_panes = "A2"
        sh.auto_filter.ref = "A1:%s1" % _col(len(headers) - 1)
        sh.append([_cell(sh, h, F_HEAD, FILL_HEAD) for h in headers])
        for e in store.iter_emails(job_id, [status]):
            sh.append(_row_values(e, extras))

    wb.save(path)
    return path


def export_valid_csv(store: Storage, job: Dict[str, Any], path: Path) -> Path:
    extras = _extra_columns(job)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["email", "domain"] + extras)
        for e in store.iter_emails(int(job["id"]), [VALID]):
            ex = json.loads(e.get("extra_json") or "{}")
            w.writerow([e["email"], e["domain"]] + [ex.get(c, "") for c in extras])
    return path


def _col(i: int) -> str:
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s
