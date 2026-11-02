from pathlib import Path
import base64
import os
import subprocess
import sys
import tempfile
import unittest

from pdfocr import PdfOcrError, capabilities, extract_pdf, normalise_pages


def make_pdf(objects):
    data = b'%PDF-1.4\n'
    offsets = [0]
    for index, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += str(index).encode() + b' 0 obj\n' + obj + b'\nendobj\n'
    xref = len(data)
    data += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    for offset in offsets[1:]:
        data += f'{offset:010d} 00000 n \n'.encode()
    return data + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()


def sample_pdf():
    content = b'BT /F1 36 Tf 50 700 Td (HELLO LOCAL READER) Tj ET'
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
    ]
    return make_pdf(objects)


def scan_pdf():
    # A real image-only PDF: the expected words never occur as PDF text.
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = r'''
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object Drawing.Bitmap(1000, 180)
$graphics = [Drawing.Graphics]::FromImage($bitmap)
$graphics.Clear([Drawing.Color]::White)
$font = New-Object Drawing.Font('Arial', 48)
$graphics.DrawString('SCAN TEST HELLO READER', $font, [Drawing.Brushes]::Black, 20, 40)
$stream = New-Object IO.MemoryStream
$bitmap.Save($stream, [Drawing.Imaging.ImageFormat]::Jpeg)
[Convert]::ToBase64String($stream.ToArray())
$font.Dispose()
$graphics.Dispose()
$bitmap.Dispose()
$stream.Dispose()
'''
    jpeg = base64.b64decode(subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-Command', command], capture_output=True, timeout=20, check=True, creationflags=subprocess.CREATE_NO_WINDOW).stdout)
    content = b'q 600 0 0 108 6 600 cm /Scan Do Q'
    return make_pdf([
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Scan 4 0 R >> >> /Contents 5 0 R >>',
        b'<< /Type /XObject /Subtype /Image /Width 1000 /Height 180 /ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length ' + str(len(jpeg)).encode() + b' >>\nstream\n' + jpeg + b'\nendstream',
        b'<< /Length ' + str(len(content)).encode() + b' >>\nstream\n' + content + b'\nendstream',
    ])


class PdfOcrTests(unittest.TestCase):
    def test_pages_preserve_source_page_numbers_escape_and_empty_status(self):
        result = normalise_pages('book.pdf', {'language': 'en-US', 'pages': [{'page': 1, 'text': '<script>English & 中文</script>'}, {'page': 2, 'text': ''}]})
        self.assertEqual(result['book']['format'], 'pdf')
        self.assertEqual(result['chapters'][0]['source_page'], 1)
        self.assertEqual(result['chapters'][1]['index'], 1)
        self.assertEqual(result['chapters'][1]['ocr_status'], 'empty')
        self.assertIn('&lt;script&gt;', result['chapters'][0]['html'])
        self.assertIn('2', result['book']['warnings'][0])

    def test_malformed_engine_payload_fails_instead_of_fake_success(self):
        for payload in ({}, {'pages': []}, {'pages': [{'page': 0, 'text': 'a'}]}, {'pages': [{'page': 2, 'text': 'a'}]}, {'pages': [{'page': 1, 'text': 7}]}):
            with self.subTest(payload=payload):
                with self.assertRaises(PdfOcrError):
                    normalise_pages('file.pdf', payload)

    def test_rejects_non_pdf_before_starting_an_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.pdf'
            path.write_bytes(b'not a PDF')
            with self.assertRaises(PdfOcrError):
                extract_pdf(path)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows WinRT integration')
    def test_actual_windows_pdf_render_and_ocr(self):
        status = capabilities()
        self.assertTrue(status['pdf_rendering'], status)
        if not status['ocr_available']:
            self.skipTest('Windows OCR language data is not installed')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.pdf'
            path.write_bytes(sample_pdf())
            result = extract_pdf(path, language=status['languages'][0]['tag'])
            self.assertEqual(len(result['chapters']), 1)
            self.assertIn('HELLO LOCAL READER', result['chapters'][0]['text'].upper())
            self.assertEqual(path.read_bytes(), sample_pdf())

    @unittest.skipUnless(sys.platform == 'win32', 'Windows WinRT integration')
    def test_actual_image_only_scan_ocr_and_missing_language_error(self):
        status = capabilities()
        if not status['ocr_available']:
            self.skipTest('Windows OCR language data is not installed')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'scan.pdf'
            original = scan_pdf()
            self.assertNotIn(b'SCAN TEST', original)
            path.write_bytes(original)
            result = extract_pdf(path, language=status['languages'][0]['tag'])
            self.assertIn('SCAN TEST HELLO READER', result['chapters'][0]['text'].upper())
            self.assertEqual(result['book']['ocr']['recognized_pages'], 1)
            self.assertEqual(path.read_bytes(), original)
            with self.assertRaises(PdfOcrError):
                extract_pdf(path, language='zz-ZZ')


if __name__ == '__main__':
    unittest.main()
