"""Pure-Python quote PDF, drawn with ReportLab.

The fallback for hosts where WeasyPrint's native libraries are missing. It
lays out the same document as ``templates/main/pdf/quote.html``: centred
letterhead, Prepared for / From, the Description · Size · Area · Rate · Total
table, totals, the special-rate note and the terms, with a "Page x of y"
footer. Keep the two in step when the quote document changes.
"""

import io
import pathlib
from xml.sax.saxutils import escape

from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas as pdf_canvas
from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

INK = colors.HexColor('#18181b')
MUTED = colors.HexColor('#71717a')
FAINT = colors.HexColor('#a1a1aa')
BRIEF = colors.HexColor('#52525b')
RULE = colors.HexColor('#ededf0')
TERMS_RULE = colors.HexColor('#e4e4e7')
PANEL = colors.HexColor('#fafafa')
YELLOW = colors.HexColor('#FFD200')
GREEN = colors.HexColor('#16a34a')

MARGIN_X = 14 * mm
FOOTER = 'InkPro · Think Ink, Think Pro · Page {page} of {pages}'

#: Helvetica-like TrueType faces, tried in order, so customer text with
#: characters outside Latin-1 (macrons, for instance) still prints. Without
#: any of them the built-in Helvetica is used.
FONT_CANDIDATES = [
    ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
    ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
     '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
    ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
     '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
]


def _fonts():
    """(regular, bold) font names, registering a TrueType pair if one exists."""
    if 'InkSans' in pdfmetrics.getRegisteredFontNames():
        return 'InkSans', 'InkSans-Bold'
    for regular, bold in FONT_CANDIDATES:
        if pathlib.Path(regular).is_file() and pathlib.Path(bold).is_file():
            try:
                pdfmetrics.registerFont(TTFont('InkSans', regular))
                pdfmetrics.registerFont(TTFont('InkSans-Bold', bold))
            except Exception:  # pragma: no cover - unreadable font file
                continue
            return 'InkSans', 'InkSans-Bold'
    return 'Helvetica', 'Helvetica-Bold'


def _money(value):
    return f'${value:,.2f}'


def _text(value):
    """Escape user text for a Paragraph, keeping its line breaks."""
    return escape(str(value or '')).replace('\n', '<br/>')


class _NumberedCanvas(pdf_canvas.Canvas):
    """Defers each page so the footer can say "Page x of y"."""

    def __init__(self, *args, footer_font='Helvetica', **kwargs):
        super().__init__(*args, **kwargs)
        self._pages = []
        self._footer_font = footer_font

    def showPage(self):
        self._pages.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._pages)
        for state in self._pages:
            self.__dict__.update(state)
            self.setFont(self._footer_font, 8)
            self.setFillColor(FAINT)
            self.drawCentredString(
                A4[0] / 2, 10 * mm, FOOTER.format(page=self._pageNumber, pages=total)
            )
            super().showPage()
        super().save()


def render(context, logo=None) -> bytes:
    """Draw the quote document described by *context* and return PDF bytes.

    *context* is ``pdf.quote_document_context(quote)``; *logo* is the path to
    the light-background logo PNG, or None to print the wordmark instead.
    """
    regular, bold = _fonts()
    quote = context['quote']
    items = context['items']
    all_sized = context['all_sized']
    special = context['special']
    gst_pct = context['gst_pct']

    def style(name, **kw):
        base = {'fontName': regular, 'fontSize': 10, 'leading': 15, 'textColor': INK}
        base.update(kw)
        return ParagraphStyle(name, **base)

    body = style('body', fontSize=9, leading=14)
    label = style('label', fontName=bold, fontSize=7, leading=10, textColor=FAINT)
    th = style('th', fontName=bold, fontSize=7.5, leading=10, textColor=MUTED)
    cell = style('cell', fontSize=10, leading=14)
    cell_r = style('cell_r', fontSize=10, leading=14, alignment=TA_RIGHT)
    cell_b = style('cell_b', fontName=bold, fontSize=10, leading=14)
    cell_br = style('cell_br', fontName=bold, fontSize=10, leading=14, alignment=TA_RIGHT)
    sub = style('sub', fontSize=8, leading=11, textColor=FAINT)
    sub_r = style('sub_r', fontSize=8, leading=11, textColor=FAINT, alignment=TA_RIGHT)
    brief = style('brief', fontSize=8, leading=11, textColor=BRIEF)
    centred = style('centred', alignment=TA_CENTER)

    width = A4[0] - 2 * MARGIN_X
    story = []

    # -- letterhead ---------------------------------------------------------
    if logo:
        reader = ImageReader(str(logo))
        img_w, img_h = reader.getSize()
        height = 62 * 0.75  # the HTML template's 62px
        story.append(Image(str(logo), width=height * img_w / img_h, height=height))
    else:
        story.append(Paragraph('InkPro', style('brand', fontName=bold, fontSize=26, leading=30, alignment=TA_CENTER)))
        story.append(Paragraph('THINK INK, THINK PRO', style('tag', fontSize=7, textColor=FAINT, alignment=TA_CENTER)))
    story.append(Spacer(1, 8))

    title = f'Quote {_text(quote.quote_number)}'
    if quote.revision:
        title += f' <font name="{regular}" size="10" color="#71717a">Revision {quote.revision}</font>'
    story.append(Paragraph(title, style('h1', fontName=bold, fontSize=17, leading=21, alignment=TA_CENTER)))

    if quote.status == quote.ACCEPTED:
        accepted = 'ACCEPTED'
        if quote.responded_at:
            accepted += ' ' + _date(quote.responded_at).upper()
        stamp = Table(
            [[Paragraph(f'{accepted} · FINAL QUOTE', style('stamp', fontName=bold, fontSize=7.5, leading=10, textColor=GREEN))]],
            style=TableStyle([
                ('BOX', (0, 0), (-1, -1), 1.1, GREEN),
                ('ROUNDEDCORNERS', [3, 3, 3, 3]),
                ('TOPPADDING', (0, 0), (-1, -1), 3), ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
            ]),
        )
        stamp.hAlign = 'CENTER'
        story += [Spacer(1, 6), stamp]

    issued = quote.sent_at or quote.created_at
    issued_line = f'Issued {_date(issued)}'
    if quote.is_urgent:
        issued_line += ' · <font name="%s" color="#000000" backColor="#FFD200"> RUSH JOB </font>' % bold
    story.append(Spacer(1, 3))
    story.append(Paragraph(issued_line, style('issued', fontSize=9, leading=13, textColor=MUTED, alignment=TA_CENTER)))
    story.append(Spacer(1, 30))

    # -- prepared for / from ------------------------------------------------
    prepared = [f'<font name="{bold}">{_text(quote.contact_name or "—")}</font>']
    company = quote.customer.company_name if quote.customer else ''
    if company and company != quote.contact_name:
        prepared.append(_text(company))
    for line in (quote.project_title, quote.contact_email, quote.contact_phone):
        if line:
            prepared.append(_text(line))
    sender = [f'<font name="{bold}">InkPro</font>', _text(context['CONTACT_EMAIL']),
              _text(context['SITE_URL'] or 'inkpro.example')]
    meta = Table(
        [[Paragraph('P R E P A R E D&nbsp;&nbsp;F O R', label), Paragraph('F R O M', label)],
         [Paragraph('<br/>'.join(prepared), body), Paragraph('<br/>'.join(sender), body)]],
        colWidths=[width / 2, width / 2],
        style=TableStyle([
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, 0), 4),
        ]),
    )
    story += [meta, Spacer(1, 26)]

    # -- line items ---------------------------------------------------------
    # No rate column: customers see sizes and totals, never the per-m² or unit rate.
    col_widths = [width * f for f in (0.46, 0.22, 0.14, 0.18)]
    quantity_heading = 'AREA' if all_sized else ('QTY / AREA' if context['any_sized'] else 'QTY')
    headings = ['DESCRIPTION', 'SIZE', quantity_heading, 'TOTAL']
    rows = [[Paragraph(h, th) for h in headings]]
    for item in items:
        description = [Paragraph(_text(item.description), cell_b)]
        if item.pricing_rule_id and item.pricing_rule.notes:
            description.append(Paragraph(_text(item.pricing_rule.notes), sub))
        if item.customer_brief:
            description.append(Spacer(1, 4))
            description.append(Table(
                [[Paragraph(_text(item.customer_brief), brief)]],
                colWidths=[col_widths[0] - 16],
                style=TableStyle([
                    ('LINEBEFORE', (0, 0), (0, -1), 2, YELLOW),
                    ('LEFTPADDING', (0, 0), (-1, -1), 7), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                    ('TOPPADDING', (0, 0), (-1, -1), 0), ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
                ]),
            ))
            description[-1].hAlign = 'LEFT'
        if item.area_label:
            area = [Paragraph(f'{item.area_label} m²', cell_r)]
            if item.quantity != 1:
                area.append(Paragraph(f'× {item.quantity}', sub_r))
        else:
            area = [Paragraph(str(item.quantity), cell_r)]
        rows.append([
            description,
            Paragraph(_text(item.size or '—'), cell),
            area,
            Paragraph(_money(item.line_total), cell_br),
        ])
    if not items:
        rows.append([Paragraph('No line items.', style('none', textColor=MUTED)), '', '', ''])

    table_style = [
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 6), ('BOTTOMPADDING', (0, 0), (-1, 0), 6),
        ('TOPPADDING', (0, 1), (-1, -1), 9), ('BOTTOMPADDING', (0, 1), (-1, -1), 10),
        ('LINEBELOW', (0, 0), (-1, 0), 1.2, INK),
        ('LINEBELOW', (0, 1), (-1, -1), 0.75, RULE),
    ]
    if not items:
        table_style.append(('SPAN', (0, 1), (-1, 1)))
    story.append(Table(rows, colWidths=col_widths, repeatRows=1, style=TableStyle(table_style)))

    # -- totals -------------------------------------------------------------
    total_label = style('tl', fontSize=9.5, leading=13, textColor=MUTED)
    total_value = style('tv', fontSize=9.5, leading=13, alignment=TA_RIGHT)
    grand = style('grand', fontName=bold, fontSize=13, leading=16)
    grand_r = style('grand_r', fontName=bold, fontSize=13, leading=16, alignment=TA_RIGHT)
    totals = [[Paragraph('Subtotal (special rate)' if special else 'Subtotal', total_label),
               Paragraph(_money(quote.subtotal), total_value)]]
    if quote.discount:
        totals.append([Paragraph('Discount', total_label), Paragraph('–' + _money(quote.discount), total_value)])
    if quote.urgent_fee:
        totals.append([Paragraph('Urgency fee', total_label), Paragraph(_money(quote.urgent_fee), total_value)])
    totals.append([Paragraph(f'GST ({gst_pct}%)', total_label), Paragraph(_money(quote.gst_amount), total_value)])
    totals.append([Paragraph('Total', grand), Paragraph(_money(quote.total), grand_r)])
    totals_table = Table(
        totals,
        colWidths=[width * 0.30, width * 0.18],
        style=TableStyle([
            ('LEFTPADDING', (0, 0), (-1, -1), 8), ('RIGHTPADDING', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 4), ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
            ('LINEABOVE', (0, -1), (-1, -1), 1.5, INK),
            ('TOPPADDING', (0, -1), (-1, -1), 9),
        ]),
    )
    totals_table.hAlign = 'RIGHT'
    story += [Spacer(1, 14), totals_table]

    # -- notes --------------------------------------------------------------
    def panel(content):
        return Table(
            [[content]],
            colWidths=[width],
            style=TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), PANEL),
                ('LINEBEFORE', (0, 0), (0, -1), 3, YELLOW),
                ('LEFTPADDING', (0, 0), (-1, -1), 14), ('RIGHTPADDING', (0, 0), (-1, -1), 14),
                ('TOPPADDING', (0, 0), (-1, -1), 10), ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ]),
        )

    if special:
        if special['rate'] is not None:
            priced = (f"{_money(special['rate'])} per m² + GST "
                      f"({_money(special['rate_inc_gst'])} incl. GST).")
        else:
            priced = 'below our normal per m² rate.'
        story += [Spacer(1, 24), panel(Paragraph(
            f'<font name="{bold}">Special rate applied.</font> This quote is priced at the special rate of '
            f"{priced} At the normal rate of {_money(special['normal_rate'])} per m² + GST, the same work "
            f"would total {_money(special['normal_total'])} — a saving of {_money(special['saving'])}.",
            body,
        ))]

    if quote.customer_notes:
        story += [Spacer(1, 24), panel([
            Paragraph('Y O U R&nbsp;&nbsp;N O T E S', label),
            Paragraph(_text(quote.customer_notes), body),
        ])]

    # -- terms --------------------------------------------------------------
    terms = (
        f'<font name="{bold}">Terms.</font> All amounts are in Samoan Tala (WST). GST at {gst_pct}% is '
        'shown separately and included in the total. This quote is valid for '
        f"{context['valid_days']} days from the date of issue and is subject to final artwork approval. "
        'Production begins once the quote is accepted and artwork is confirmed.'
    )
    if quote.urgent_fee:
        terms += ' Rush and same-day work carries the urgency fee shown above.'
    story += [Spacer(1, 26), Table(
        [[Paragraph(terms, style('terms', fontSize=7.5, leading=12, textColor=FAINT))]],
        colWidths=[width],
        style=TableStyle([
            ('LINEABOVE', (0, 0), (-1, 0), 0.75, TERMS_RULE),
            ('LEFTPADDING', (0, 0), (-1, -1), 0), ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 12),
        ]),
    )]

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=MARGIN_X, rightMargin=MARGIN_X, topMargin=16 * mm, bottomMargin=20 * mm,
        title=f'Quote {quote.quote_number}', author='InkPro',
    )
    doc.build(story, canvasmaker=lambda *a, **kw: _NumberedCanvas(*a, footer_font=regular, **kw))
    return buffer.getvalue()


def _date(value):
    """"5 October 2026", matching the template's ``date:"j F Y"``."""
    if value is None:
        return ''
    local = timezone.localtime(value) if hasattr(value, 'tzinfo') and value.tzinfo else value
    return f'{local.day} {local:%B %Y}'
