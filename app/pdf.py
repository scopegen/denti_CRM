"""PDF documents: prescriptions now; invoices, estimates and the patient history later.

Built as HTML and converted with xhtml2pdf, which understands a simple subset of CSS (tables
and blocks, no flexbox). The letterhead repeats on every page through named page frames.
Everything printed comes from the clinic's own data, never from values typed into code.
"""
import base64
import io
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from xhtml2pdf import pisa

FONTS = Path(__file__).parent / "fonts"

ACCENT = "#1e5f8c"
ACCENT_DEEP = "#123f5c"
INK = "#101826"
INK_SOFT = "#57667a"
RULE = "#d9e1ea"

FREQUENCY_WORDS = {
    "OD": "OD (once a day)",
    "BD": "BD (twice a day)",
    "TDS": "TDS (three times a day)",
    "QID": "QID (four times a day)",
    "HS": "HS (at bedtime)",
    "SOS": "SOS (when needed)",
    "STAT": "STAT (at once)",
}


def _font(name: str) -> str:
    return (FONTS / name).resolve().as_uri()


CSS = f"""
@font-face {{ font-family: "Roboto"; src: url("{_font('Roboto-Regular.ttf')}"); }}
@font-face {{ font-family: "Roboto"; src: url("{_font('Roboto-Bold.ttf')}"); font-weight: bold; }}
@font-face {{ font-family: "Roboto"; src: url("{_font('Roboto-Italic.ttf')}"); font-style: italic; }}
@page {{
  size: A4;
  @frame header_frame {{ -pdf-frame-content: header_content; top: 0.9cm; left: 1.8cm; width: 17.4cm; height: 3cm; }}
  @frame content_frame {{ top: 4.1cm; left: 1.8cm; width: 17.4cm; height: 22.2cm; }}
  @frame footer_frame {{ -pdf-frame-content: footer_content; top: 26.8cm; left: 1.8cm; width: 17.4cm; height: 2.2cm; }}
}}
body {{ font-family: "Roboto", Helvetica, sans-serif; font-size: 11.5pt; color: {INK}; }}
#header_content table {{ width: 100%; }}
.clinic-name {{ font-size: 20pt; font-weight: bold; color: {ACCENT_DEEP}; margin: 0; }}
.clinic-line {{ font-size: 10pt; color: {INK_SOFT}; margin: 2px 0 0; }}
.doctor-cell {{ text-align: right; vertical-align: top; }}
.doctor-name {{ font-size: 15pt; font-weight: bold; color: {ACCENT_DEEP}; margin: 0; }}
.doctor-line {{ font-size: 10pt; color: {INK_SOFT}; margin: 2px 0 0; }}
.rule {{ color: {ACCENT}; background-color: {ACCENT}; height: 2px; border: none; margin: 6px 0 0; }}
.date-stamp {{ text-align: right; font-size: 10.5pt; color: {INK_SOFT}; margin-bottom: 10px; }}
table.fields {{ width: 100%; margin-bottom: 10px; }}
table.fields td {{ padding: 2px 0; font-size: 11pt; vertical-align: top; }}
.label {{ font-weight: bold; color: {INK_SOFT}; width: 22%; }}
.value {{ width: 28%; }}
.section-label {{ font-size: 10pt; font-weight: bold; color: {INK_SOFT}; margin: 10px 0 2px; }}
.section-text {{ margin: 0; font-size: 11.5pt; }}
.rx {{ font-size: 18pt; font-style: italic; font-weight: bold; color: {ACCENT}; margin: 14px 0 4px; }}
.rx-line {{ font-size: 11.5pt; padding: 3px 0 3px 12px; }}
.signature {{ text-align: right; margin-top: 30px; }}
.signature img {{ height: 44pt; }}
.signature-name {{ font-weight: bold; margin: 2px 0 0; }}
.signature-line {{ font-size: 10pt; color: {INK_SOFT}; margin: 0; }}
#footer_content {{ text-align: center; }}
.footer-note {{ font-size: 9.5pt; color: {INK_SOFT}; font-style: italic; margin: 0 0 4px; }}
.powered {{ font-size: 8.5pt; color: {INK_SOFT}; margin: 0; }}
.doc-title {{ font-size: 16pt; font-weight: bold; color: {ACCENT_DEEP}; margin: 0 0 8px; }}
.cancelled {{ font-size: 12pt; font-weight: bold; color: #a8433a; border: 1.5px solid #a8433a; padding: 4px 8px; margin: 0 0 10px; }}
table.items {{ width: 100%; margin-top: 10px; }}
table.items th {{ font-size: 9.5pt; color: {INK_SOFT}; text-align: left; border-bottom: 1px solid {RULE}; padding: 4px 6px; }}
table.items td {{ font-size: 11pt; border-bottom: 1px solid {RULE}; padding: 6px 6px; vertical-align: top; }}
table.items .num {{ text-align: right; }}
table.items tr.total td {{ font-weight: bold; border-bottom: none; padding-top: 8px; }}
.small-note {{ font-size: 10pt; color: {INK_SOFT}; margin: 12px 0 0; }}
"""


@dataclass
class Letterhead:
    clinic_name: str
    lines: list[str] = field(default_factory=list)  # address, phone, email: whatever the clinic has set
    show_powered_by: bool = True


@dataclass
class Doctor:
    name: str
    qualification: str | None = None
    specialty: str | None = None
    registration_no: str | None = None
    signature_png: bytes | None = None


@dataclass
class PrescriptionDoc:
    date_text: str
    patient_name: str
    patient_code: str
    age_gender: str
    phone: str
    doctor: Doctor
    diagnosis: str | None = None
    items: list[dict] = field(default_factory=list)
    advice: str | None = None
    chief_complaint: str | None = None
    oral_examination: str | None = None
    xray_taken: bool = False
    recommendations: list[str] = field(default_factory=list)
    version_note: str | None = None
    # For a prescription written at a treatment visit: "Root canal treatment, teeth 36".
    treatment: str | None = None


def _e(value: str | None) -> str:
    return escape(value or "")


def _lines(value: str | None) -> str:
    return "<br/>".join(escape(line) for line in (value or "").splitlines())


def _header(letterhead: Letterhead, doctor: Doctor | None) -> str:
    clinic_lines = "".join(f'<p class="clinic-line">{_e(line)}</p>' for line in letterhead.lines if line)
    doctor_html = ""
    if doctor:
        details = " · ".join(x for x in [doctor.qualification, doctor.specialty] if x)
        doctor_html = f'<p class="doctor-name">{_e(doctor.name)}</p>'
        if details:
            doctor_html += f'<p class="doctor-line">{_e(details)}</p>'
        if doctor.registration_no:
            doctor_html += f'<p class="doctor-line">Reg. No. {_e(doctor.registration_no)}</p>'
    return f"""
    <div id="header_content">
      <table><tr>
        <td width="58%" valign="top"><p class="clinic-name">{_e(letterhead.clinic_name)}</p>{clinic_lines}</td>
        <td width="42%" valign="top" class="doctor-cell">{doctor_html}</td>
      </tr></table>
      <hr class="rule"/>
    </div>"""


def _footer(letterhead: Letterhead, note: str | None) -> str:
    note_html = f'<p class="footer-note">{_e(note)}</p>' if note else ""
    powered = '<p class="powered">Powered by Denti</p>' if letterhead.show_powered_by else ""
    return f'<div id="footer_content">{note_html}{powered}</div>'


def _render(html: str) -> bytes:
    out = io.BytesIO()
    result = pisa.CreatePDF(html, dest=out, encoding="utf-8")
    if result.err:
        raise RuntimeError("The PDF could not be created.")
    return out.getvalue()


def _rx_line(number: int, item: dict) -> str:
    parts = [item.get("medicine"), item.get("dose"), FREQUENCY_WORDS.get(item.get("frequency") or "", item.get("frequency"))]
    if item.get("duration"):
        parts.append(f"for {item['duration']}")
    parts.append(item.get("instructions"))
    text = " · ".join(escape(p) for p in parts if p)
    return f'<div class="rx-line">{number}. {text}</div>'


def prescription_html(letterhead: Letterhead, doc: PrescriptionDoc) -> str:
    fields = f"""
    <table class="fields">
      <tr><td class="label">Patient</td><td class="value">{_e(doc.patient_name)}</td>
          <td class="label">Patient ID</td><td class="value">{_e(doc.patient_code)}</td></tr>
      <tr><td class="label">Age / Gender</td><td class="value">{_e(doc.age_gender)}</td>
          <td class="label">Phone</td><td class="value">{_e(doc.phone)}</td></tr>
    </table>"""

    sections = ""
    for label, value in [
        ("Treatment", doc.treatment), ("Chief complaint", doc.chief_complaint), ("Oral examination", doc.oral_examination),
    ]:
        if value:
            sections += f'<p class="section-label">{label}</p><p class="section-text">{_lines(value)}</p>'
    if doc.xray_taken:
        sections += '<p class="section-label">X-ray</p><p class="section-text">Taken</p>'
    if doc.diagnosis:
        sections += f'<p class="section-label">Diagnosis</p><p class="section-text">{_lines(doc.diagnosis)}</p>'

    rx = ""
    if doc.items:
        rx = '<p class="rx">Rx</p>' + "".join(_rx_line(i, item) for i, item in enumerate(doc.items, start=1))
    advice = f'<p class="section-label">Advice</p><p class="section-text">{_lines(doc.advice)}</p>' if doc.advice else ""
    recommended = ""
    if doc.recommendations:
        recommended = '<p class="section-label">Recommended treatment</p><p class="section-text">' + "<br/>".join(
            escape(r) for r in doc.recommendations
        ) + "</p>"

    signature_img = ""
    if doc.doctor.signature_png:
        encoded = base64.b64encode(doc.doctor.signature_png).decode("ascii")
        signature_img = f'<img src="data:image/png;base64,{encoded}" />'
    signature_lines = "".join(
        f'<p class="signature-line">{_e(x)}</p>' for x in [doc.doctor.qualification, f"Reg. No. {doc.doctor.registration_no}" if doc.doctor.registration_no else None] if x
    )
    signature = f"""
    <div class="signature">{signature_img}<p class="signature-name">{_e(doc.doctor.name)}</p>{signature_lines}</div>"""

    version = f'<p class="footer-note">{_e(doc.version_note)}</p>' if doc.version_note else ""
    note = "Valid with the doctor's signature. Please follow the dosage instructions carefully."
    html = f"""<html><head><meta charset="utf-8"/><style>{CSS}</style></head><body>
      {_header(letterhead, doc.doctor)}
      {_footer(letterhead, note)}
      <div class="date-stamp">Date: {_e(doc.date_text)}</div>
      {fields}{sections}{rx}{advice}{recommended}{signature}{version}
    </body></html>"""
    return html


def prescription_pdf(letterhead: Letterhead, doc: PrescriptionDoc) -> bytes:
    return _render(prescription_html(letterhead, doc))


@dataclass
class InvoiceLineDoc:
    description: str
    teeth: str
    amount: str


@dataclass
class InvoiceDoc:
    number_text: str
    date_text: str
    patient_name: str
    patient_code: str
    phone: str
    lines: list[InvoiceLineDoc]
    total: str
    # Set for a cancelled invoice: why, printed across the top.
    cancelled_note: str | None = None
    balance_note: str | None = None


def invoice_html(letterhead: Letterhead, doc: InvoiceDoc) -> str:
    fields = f"""
    <table class="fields">
      <tr><td class="label">Invoice</td><td class="value">{_e(doc.number_text)}</td>
          <td class="label">Date</td><td class="value">{_e(doc.date_text)}</td></tr>
      <tr><td class="label">Patient</td><td class="value">{_e(doc.patient_name)}</td>
          <td class="label">Patient ID</td><td class="value">{_e(doc.patient_code)}</td></tr>
      <tr><td class="label">Phone</td><td class="value">{_e(doc.phone)}</td><td></td><td></td></tr>
    </table>"""
    rows = "".join(
        f'<tr><td>{i}</td><td>{_e(line.description)}</td><td>{_e(line.teeth)}</td><td class="num">{_e(line.amount)}</td></tr>'
        for i, line in enumerate(doc.lines, start=1)
    )
    items = f"""
    <table class="items">
      <tr><th width="7%">No.</th><th width="55%">Item</th><th width="18%">Teeth</th><th width="20%" class="num">Amount</th></tr>
      {rows}
      <tr class="total"><td></td><td>Total</td><td></td><td class="num">{_e(doc.total)}</td></tr>
    </table>"""
    cancelled = f'<p class="cancelled">CANCELLED: {_e(doc.cancelled_note)}</p>' if doc.cancelled_note else ""
    balance = f'<p class="small-note">{_e(doc.balance_note)}</p>' if doc.balance_note else ""
    return f"""<html><head><meta charset="utf-8"/><style>{CSS}</style></head><body>
      {_header(letterhead, None)}
      {_footer(letterhead, "Thank you for choosing us for your care.")}
      <p class="doc-title">Invoice</p>
      {cancelled}{fields}{items}{balance}
    </body></html>"""


def invoice_pdf(letterhead: Letterhead, doc: InvoiceDoc) -> bytes:
    return _render(invoice_html(letterhead, doc))


@dataclass
class EstimateRowDoc:
    description: str
    teeth: str
    sittings: str
    amount: str


@dataclass
class EstimateOptionDoc:
    label: str
    rows: list[EstimateRowDoc]
    subtotal: str
    discount: str | None
    total: str


@dataclass
class EstimateDoc:
    number_text: str
    date_text: str
    valid_until_text: str
    patient_name: str
    patient_code: str
    phone: str
    doctor: Doctor
    options: list[EstimateOptionDoc]
    notes: str | None = None
    status_note: str | None = None


def _option_table(option: EstimateOptionDoc, show_label: bool) -> str:
    rows = "".join(
        f'<tr><td>{i}</td><td>{_e(r.description)}</td><td>{_e(r.teeth)}</td><td>{_e(r.sittings)}</td><td class="num">{_e(r.amount)}</td></tr>'
        for i, r in enumerate(option.rows, start=1)
    )
    summary = ""
    if option.discount:
        summary += f'<tr><td></td><td>Subtotal</td><td></td><td></td><td class="num">{_e(option.subtotal)}</td></tr>'
        summary += f'<tr><td></td><td>Discount</td><td></td><td></td><td class="num">{_e(option.discount)}</td></tr>'
    summary += f'<tr class="total"><td></td><td>Total</td><td></td><td></td><td class="num">{_e(option.total)}</td></tr>'
    label = f'<p class="section-label">{_e(option.label)}</p>' if show_label else ""
    return f"""{label}
    <table class="items">
      <tr><th width="6%">No.</th><th width="46%">Treatment</th><th width="16%">Teeth</th><th width="12%">Sittings</th><th width="20%" class="num">Amount</th></tr>
      {rows}{summary}
    </table>"""


def estimate_html(letterhead: Letterhead, doc: EstimateDoc) -> str:
    fields = f"""
    <table class="fields">
      <tr><td class="label">Estimate</td><td class="value">{_e(doc.number_text)}</td>
          <td class="label">Date</td><td class="value">{_e(doc.date_text)}</td></tr>
      <tr><td class="label">Patient</td><td class="value">{_e(doc.patient_name)}</td>
          <td class="label">Valid until</td><td class="value">{_e(doc.valid_until_text)}</td></tr>
      <tr><td class="label">Patient ID</td><td class="value">{_e(doc.patient_code)}</td>
          <td class="label">Phone</td><td class="value">{_e(doc.phone)}</td></tr>
    </table>"""
    several = len(doc.options) > 1
    options = "".join(_option_table(o, several) for o in doc.options)
    comparison = ""
    if several:
        cells = "".join(f'<td class="num">{_e(o.total)}</td>' for o in doc.options)
        heads = "".join(f'<th class="num">{_e(o.label)}</th>' for o in doc.options)
        comparison = f"""<p class="section-label">Your options at a glance</p>
        <table class="items"><tr><th></th>{heads}</tr><tr class="total"><td>Total</td>{cells}</tr></table>"""
    notes = f'<p class="section-label">Notes</p><p class="section-text">{_lines(doc.notes)}</p>' if doc.notes else ""
    status = f'<p class="cancelled">{_e(doc.status_note)}</p>' if doc.status_note else ""
    doctor_line = ", ".join(x for x in [doc.doctor.qualification, f"Reg. No. {doc.doctor.registration_no}" if doc.doctor.registration_no else None] if x)
    sign = f"""
    <table width="100%" style="margin-top: 36px;"><tr>
      <td width="50%" valign="bottom"><p class="signature-name">{_e(doc.doctor.name)}</p><p class="signature-line">{_e(doctor_line)}</p></td>
      <td width="50%" valign="bottom" style="text-align: right;"><p class="signature-line">Patient's signature</p>
        <p class="signature-line">______________________________</p></td>
    </tr></table>"""
    note = "This is an estimate, not a bill. Prices may change if the treatment plan changes."
    return f"""<html><head><meta charset="utf-8"/><style>{CSS}</style></head><body>
      {_header(letterhead, None)}
      {_footer(letterhead, note)}
      <p class="doc-title">Treatment estimate</p>
      {status}{fields}{comparison}{options}{notes}{sign}
    </body></html>"""


def estimate_pdf(letterhead: Letterhead, doc: EstimateDoc) -> bytes:
    return _render(estimate_html(letterhead, doc))
