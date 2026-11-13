"""DSV instrument rules use broker PDFs as an independent reference."""
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QFileDialog

from tests.qt_lifecycle import dispose_widget

from online_trader.configuration.instrument_rules import parse_rule_pages, rule_display
from online_trader.desktop.instrument_rules import InstrumentRulesPanel, read_rule_pdf
from online_trader.errors import TraderError


PALM = '棕榈油 P★ 元/吨 10 2 8% 12% 15%'
PLASTIC = '塑料\nL 元/吨\n5 1 8% 11% 14%'


def _pdf_stream(data):
    return f'<< /Length {len(data)} >>\nstream\n'.encode() + data + b'\nendstream'


def text_rule_pdf(rule):
    # Use a ToUnicode map for a selectable CJK unit in a minimal PDF fixture.
    mapping = '\n'.join(f'<{ord(char):02X}> <{(0x5143 if char == "@" else ord(char)):04X}>'
                        for char in sorted(set(rule)))
    cmap = f'''/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Identity-H def
/CMapType 2 def
1 begincodespacerange
<00> <FF>
endcodespacerange
{len(set(rule))} beginbfchar
{mapping}
endbfchar
endcmap
CMapName currentdict /CMap defineresource pop
end
end'''.encode()
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 6 0 R >>',
        _pdf_stream(f'BT /F1 16 Tf 40 700 Td ({rule}) Tj ET'.encode()),
        _pdf_stream(cmap),
    ]
    pdf = bytearray(b'%PDF-1.4\n')
    offsets = []
    for index, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf.extend(f'{index} 0 obj\n'.encode() + obj + b'\nendobj\n')
    xref = len(pdf)
    pdf.extend(f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode())
    pdf.extend(''.join(f'{offset:010d} 00000 n \n' for offset in offsets).encode())
    pdf.extend(f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode())
    return pdf


class InstrumentRulesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root/'view.ini'), QSettings.IniFormat)

    def test_dsv_wrapped_rows_and_distinct_exchange_company_percent_columns(self):
        rows = parse_rule_pages([f'品种 合约单位\n{PALM}\n{PLASTIC}\n{PALM}'])
        self.assertEqual([row.code for row in rows], ['P','L'])
        self.assertEqual(rows[0].multiplier, Decimal('10'))
        self.assertEqual(rows[0].tick, Decimal('2'))
        self.assertEqual(rows[0].exchange_margin, Decimal('.12'))
        self.assertEqual(rows[0].company_margin, Decimal('.15'))
        self.assertEqual(rows[1].exchange_margin, Decimal('.11'))

    def test_live_margin_priority_markup_and_strict_difference(self):
        rule = parse_rule_pages([PALM])[0]
        quote = dict(fresh=True, last='1000', margin='1250', volume_multiple='10', commission='2.50')
        at_boundary = rule_display(quote, rule, Decimal('1.5'), Decimal('.25'))
        self.assertIsNone(at_boundary['difference'])
        self.assertEqual(at_boundary['company_margin'], Decimal('.140'))
        self.assertEqual(at_boundary['company_fee'], Decimal('2.75'))
        self.assertEqual(rule_display(quote | {'margin':'1260'}, rule, Decimal(0), Decimal(0))['difference'], Decimal('.006'))
        fallback = rule_display(quote | {'fresh':False}, rule, Decimal('1.5'), Decimal('.25'))
        self.assertEqual(fallback['company_margin'], Decimal('.15'))
        self.assertEqual(fallback['margin_source'], '规则表')
        self.assertIsNone(fallback['company_fee'])

    def test_qt_pdf_import_previews_then_persists_and_uses_validated_reference(self):
        path = self.root/'规则.pdf'
        path.write_bytes(text_rule_pdf('Palm P @ 10 2 8% 12% 15%'))
        metadata, rows = read_rule_pdf(path)
        self.assertEqual(rows[0].code, 'P')
        self.assertEqual(len(metadata['sha256']), 64)

        logs = []
        panel = InstrumentRulesPanel(self.settings, logs.append)
        self.addCleanup(dispose_widget, panel)
        panel.set_markets({'DCE.p2701':'棕榈油'}, {})
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), '')), \
                patch.object(panel, 'preview_import', return_value=False):
            panel.import_pdf()
        self.assertFalse(panel.rules)
        with patch.object(QFileDialog, 'getOpenFileName', return_value=(str(path), '')), \
                patch.object(panel, 'preview_import', return_value=True):
            panel.import_pdf()
        self.assertEqual(panel.grid.item(0, 2).text(), '12.00% 表')
        restored = InstrumentRulesPanel(self.settings, logs.append)
        self.addCleanup(dispose_widget, restored)
        self.assertEqual(restored.rules['P'].company_margin, Decimal('.15'))

    def test_malformed_file_and_rows_are_reported(self):
        path = self.root/'not-a-pdf.pdf'
        path.write_text('invalid', encoding='utf-8')
        with self.assertRaises(TraderError):
            read_rule_pdf(path)
        with self.assertRaises(TraderError):
            parse_rule_pages(['品种 合约单位\n棕榈油 P 10 2'])
