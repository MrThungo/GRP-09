"""Shared PDF report builder for all roles.

Uses ReportLab Platypus so reports look polished and paginate cleanly.
The single `build_report_pdf()` helper renders a branded MediLab Connect document
with title, optional subtitle, summary lines, and one or more data tables.
"""
from __future__ import annotations
import html
from io import BytesIO
from datetime import datetime, date

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak,
)


BRAND = colors.HexColor("#0ea5e9")
DARK = colors.HexColor("#0f172a")
MUTED = colors.HexColor("#475569")
ZEBRA = colors.HexColor("#f1f5f9")


def _styles():
    s = getSampleStyleSheet()
    s.add(ParagraphStyle(name="HeroTitle", parent=s["Title"], fontSize=20,
                         textColor=DARK, spaceAfter=4))
    s.add(ParagraphStyle(name="HeroSub", parent=s["Normal"], fontSize=10,
                         textColor=MUTED, spaceAfter=14))
    s.add(ParagraphStyle(name="Section", parent=s["Heading2"], fontSize=13,
                         textColor=BRAND, spaceBefore=10, spaceAfter=6))
    s.add(ParagraphStyle(name="Meta", parent=s["Normal"], fontSize=9,
                         textColor=MUTED))
    s.add(ParagraphStyle(name="TableHeader", parent=s["Normal"], fontSize=8,
                         leading=10, textColor=colors.white,
                         fontName="Helvetica-Bold"))
    s.add(ParagraphStyle(name="TableCell", parent=s["Normal"], fontSize=8,
                         leading=10, textColor=DARK))
    return s


def _clean_text(value):
    text = "-" if value is None else str(value)
    replacements = {
        "\u2192": "to",
        "\u2013": "-",
        "\u2014": "-",
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u00a0": " ",
    }
    for src, dest in replacements.items():
        text = text.replace(src, dest)
    return text


def _safe_markup(value, allow_basic_markup=False):
    text = html.escape(_clean_text(value)).replace("\n", "<br/>")
    if allow_basic_markup:
        for tag in ("b", "i", "u"):
            text = text.replace(f"&lt;{tag}&gt;", f"<{tag}>")
            text = text.replace(f"&lt;/{tag}&gt;", f"</{tag}>")
    return text


def _paragraph(value, style, allow_basic_markup=False):
    return Paragraph(_safe_markup(value, allow_basic_markup), style)


def _format_decimal(value):
    if value is None:
        return "-"
    text = str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def format_result_value(item):
    if item.result_value is not None:
        return _format_decimal(item.result_value)
    return item.result_text or "-"


def format_reference(test):
    low = getattr(test, "reference_low", None)
    high = getattr(test, "reference_high", None)
    if low is not None and high is not None:
        return f"{_format_decimal(low)} - {_format_decimal(high)}"
    if low is not None:
        return f">= {_format_decimal(low)}"
    if high is not None:
        return f"<= {_format_decimal(high)}"
    return getattr(test, "reference_text", None) or "-"


def _default_col_widths(headers):
    count = max(1, len(headers))
    return [(A4[0] - 30 * mm) / count] * count


def _table(headers, rows, col_widths=None):
    s = _styles()
    data = [
        [_paragraph(header, s["TableHeader"]) for header in headers]
    ]
    for row in rows or [["-"] * len(headers)]:
        padded = list(row)[:len(headers)]
        padded.extend([""] * (len(headers) - len(padded)))
        data.append([_paragraph(cell, s["TableCell"]) for cell in padded])
    t = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), BRAND),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ALIGN", (0, 0), (-1, 0), "LEFT"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#cbd5e1")),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), ZEBRA))
    t.setStyle(TableStyle(style))
    return t


def _header_footer(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(BRAND)
    canvas.rect(0, A4[1] - 14 * mm, A4[0], 14 * mm, fill=1, stroke=0)
    canvas.setFillColor(colors.white)
    canvas.setFont("Helvetica-Bold", 11)
    canvas.drawString(15 * mm, A4[1] - 9 * mm, "MediLab Connect")
    canvas.setFont("Helvetica", 8)
    canvas.drawRightString(A4[0] - 15 * mm, A4[1] - 9 * mm,
                           "Nelson Mandela Bay Haematology Diagnostic Laboratories")
    canvas.setFillColor(MUTED)
    canvas.setFont("Helvetica", 8)
    canvas.drawString(15 * mm, 10 * mm,
                      f"Generated {datetime.now():%Y-%m-%d %H:%M}")
    canvas.drawRightString(A4[0] - 15 * mm, 10 * mm, f"Page {doc.page}")
    canvas.restoreState()


def build_report_pdf(title, subtitle=None, summary=None, sections=None):
    """Build a PDF.

    sections: list of dicts: {"heading": str, "headers": [...], "rows": [...],
                              "col_widths": [...] or None}
    Returns a BytesIO seeked to start.
    """
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=22 * mm, bottomMargin=18 * mm,
        title=title,
    )
    s = _styles()
    flow = [_paragraph(title, s["HeroTitle"])]
    if subtitle:
        flow.append(_paragraph(subtitle, s["HeroSub"]))
    if summary:
        for line in summary:
            flow.append(_paragraph(line, s["Meta"], allow_basic_markup=True))
        flow.append(Spacer(1, 8))
    for sec in sections or []:
        if sec.get("page_break") and len(flow) > 1:
            flow.append(PageBreak())
        flow.append(_paragraph(sec.get("heading", ""), s["Section"]))
        flow.append(_table(sec["headers"], sec.get("rows") or [],
                           col_widths=sec.get("col_widths") or _default_col_widths(sec["headers"])))
        flow.append(Spacer(1, 6))
    doc.build(flow, onFirstPage=_header_footer, onLaterPages=_header_footer)
    buf.seek(0)
    return buf


def build_request_results_pdf(req, title=None, items=None):
    rows = []
    result_items = list(items) if items is not None else req.items
    for item in result_items:
        rows.append([
            f"{item.test.code} {item.test.name}",
            format_result_value(item),
            item.test.units or "-",
            format_reference(item.test),
            (item.abnormal_flag or "").upper() or "-",
            item.status.replace("_", " ").title(),
        ])

    summary = [
        f"Request number: <b>{req.request_number}</b>",
        f"Patient: <b>{req.patient.full_name if req.patient else '-'}</b>",
        f"MRN: <b>{req.patient.mrn if req.patient else '-'}</b>",
        f"Status: <b>{req.status.replace('_', ' ').title()}</b>",
        f"Priority: <b>{req.priority.upper()}</b>",
        f"Created: <b>{req.created_at:%Y-%m-%d %H:%M}</b>",
    ]
    if req.released_at:
        summary.append(f"Released: <b>{req.released_at:%Y-%m-%d %H:%M}</b>")
    if req.doctor:
        summary.append(f"Requesting doctor: <b>{req.doctor.full_name or req.doctor.email}</b>")
    if req.release_note:
        summary.append(f"Doctor note: {req.release_note}")

    return build_report_pdf(
        title or f"Lab Results - {req.request_number}",
        subtitle="Nelson Mandela Bay Haematology Diagnostic Laboratories",
        summary=summary,
        sections=[{
            "heading": "Test results",
            "headers": ["Test", "Result", "Units", "Reference", "Flag", "Status"],
            "rows": rows or [["No tests", "", "", "", ""]],
            "col_widths": [45 * mm, 28 * mm, 20 * mm, 38 * mm, 18 * mm, 31 * mm],
        }],
    )


def parse_range(args):
    """Parse ?from=YYYY-MM-DD&to=YYYY-MM-DD; default = last 30 days."""
    today = date.today()
    def _p(key, default):
        v = (args.get(key) or "").strip()
        if not v:
            return default
        try:
            return date.fromisoformat(v)
        except ValueError:
            return default
    frm = _p("from", today.replace(day=1) if today.day > 1 else today)
    to = _p("to", today)
    if frm > to:
        frm, to = to, frm
    start = datetime.combine(frm, datetime.min.time())
    end = datetime.combine(to, datetime.max.time())
    return frm, to, start, end
