"""Windows' installed PDF renderer and offline OCR, without third-party packages."""

from html import escape
import json
import os
from pathlib import Path
import subprocess
import sys


class PdfOcrError(ValueError):
    pass


SCRIPT = Path(__file__).parent / 'windows' / 'pdfocr.ps1'
MAX_PDF_BYTES = 128 * 1024 * 1024


def _invoke(action, path=None, language=None):
    if sys.platform != 'win32':
        raise PdfOcrError('PDF/OCR WinRT chỉ có trên Windows; chưa có engine cho hệ điều hành này.')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'
    command = [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT), '-Action', action]
    if path is not None:
        command.extend(['-Path', str(path)])
    if language:
        command.extend(['-Language', language])
    try:
        completed = subprocess.run(command, capture_output=True, timeout=30 if action == 'Capabilities' else 600,
                                   creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0,
                                   check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PdfOcrError('Không chạy được engine Windows PDF/OCR: ' + str(error)) from error
    try:
        result = json.loads(completed.stdout.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as error:
        detail = completed.stderr.decode('utf-8', errors='replace')[-1200:]
        raise PdfOcrError('Engine Windows PDF/OCR không trả kết quả hợp lệ. ' + detail) from error
    if completed.returncode or not isinstance(result, dict) or result.get('error'):
        raise PdfOcrError('Windows PDF/OCR: ' + str(result.get('error', 'engine failed') if isinstance(result, dict) else 'invalid payload'))
    return result


def capabilities():
    try:
        status = _invoke('Capabilities')
    except PdfOcrError as error:
        status = {'pdf_rendering': False, 'ocr_available': False, 'languages': [], 'error': str(error)}
    status['engine'] = 'Windows.Data.Pdf + Windows.Media.Ocr'
    status['offline'] = True
    status['supported'] = bool(status.get('pdf_rendering') and status.get('ocr_available'))
    status['limits'] = {'file_bytes': MAX_PDF_BYTES, 'pages': 500, 'render_dimension': 4000, 'render_pixels': 16_000_000}
    status['quality'] = 'OCR phụ thuộc chất lượng bản scan và gói ngôn ngữ đã cài; chưa đo độ chính xác trên tài liệu người dùng.'
    return status


def normalise_pages(path, payload):
    path = Path(path)
    pages = payload.get('pages') if isinstance(payload, dict) else None
    if not isinstance(pages, list) or not pages or len(pages) > 500:
        raise PdfOcrError('Engine không trả danh sách trang PDF hợp lệ.')
    chapters, empty = [], []
    for index, page in enumerate(pages):
        if not isinstance(page, dict) or page.get('page') != index + 1 or not isinstance(page.get('text'), str):
            raise PdfOcrError('Trang OCR thiếu số trang/văn bản hoặc sai thứ tự.')
        text = page['text'].strip()
        if not text:
            empty.append(index + 1)
        chapters.append({'index': index, 'title': f'Trang {index + 1}',
                         'html': ''.join('<p>' + escape(line) + '</p>' for line in text.splitlines()),
                         'text': text, 'images': [], 'downloaded': True, 'source_page': index + 1,
                         'ocr_status': 'recognized' if text else 'empty', 'ocr_language': payload.get('language', '')})
    warnings = []
    if empty:
        warnings.append('OCR không tìm thấy chữ ở trang: ' + ', '.join(map(str, empty)) + '. Trang trắng hoặc ngôn ngữ/ảnh không nhận dạng được; mở PDF gốc để kiểm tra.')
    return {'book': {'name': path.stem, 'author': '', 'kind': 'text', 'format': 'pdf', 'source': 'local',
                     'path': str(path.resolve()), 'description': '', 'chapter_count': len(chapters), 'warnings': warnings,
                     'ocr': {'engine': 'Windows.Media.Ocr', 'language': payload.get('language', ''), 'pages': len(chapters),
                             'recognized_pages': len(chapters) - len(empty), 'status': 'complete' if not empty else 'empty_pages'}},
            'chapters': chapters}


def extract_pdf(path, language=None):
    path = Path(path).resolve()
    if not path.is_file() or path.stat().st_size > MAX_PDF_BYTES:
        raise PdfOcrError('Không tìm thấy PDF hoặc tệp vượt giới hạn 128 MiB.')
    with path.open('rb') as handle:
        if b'%PDF-' not in handle.read(1024):
            raise PdfOcrError('Tệp không có chữ ký PDF hợp lệ.')
    if language is not None and (not isinstance(language, str) or len(language) > 64 or not language or any(not (char.isascii() and (char.isalnum() or char == '-')) for char in language)):
        raise PdfOcrError('Mã ngôn ngữ OCR không hợp lệ.')
    return normalise_pages(path, _invoke('Extract', path, language))
