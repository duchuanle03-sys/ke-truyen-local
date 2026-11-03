"""Create Windows book shortcuts without a console or third-party packages."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import uuid


class ShortcutError(ValueError):
    pass


SCRIPT = Path(__file__).resolve().parent / 'windows' / 'shortcut.ps1'
MAX_COVER_BYTES = 16 * 1024 * 1024
_CREATE_LOCK = threading.Lock()


def desktop_directory():
    if sys.platform != 'win32':
        raise ShortcutError('Lối tắt Desktop chỉ có trên Windows.')
    folder_id = (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID('b4bfcc3a-db2c-424c-b029-7fe99a87c641').bytes_le)
    result = ctypes.c_wchar_p()
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.SHGetKnownFolderPath.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_wchar_p)]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    status = shell.SHGetKnownFolderPath(ctypes.byref(folder_id), 0, None, ctypes.byref(result))
    try:
        if status < 0 or not result.value:
            raise ShortcutError(f'Không tìm được Desktop Windows (0x{status & 0xffffffff:08X}).')
        return Path(result.value)
    finally:
        if result:
            ole = ctypes.WinDLL('ole32')
            ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole.CoTaskMemFree(result)


def create_shortcut(*, book_id, title, cover_bytes, data_dir, port):
    if sys.platform != 'win32':
        raise ShortcutError('Lối tắt Desktop chỉ có trên Windows.')
    if (not isinstance(book_id, str) or not book_id or len(book_id) > 256
            or any(ord(char) < 32 for char in book_id) or not isinstance(title, str) or len(title) > 2048
            or not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535):
        raise ShortcutError('Thông tin truyện hoặc cổng lối tắt không hợp lệ.')
    try:
        (book_id + title).encode('utf-8')
        library = Path(data_dir).resolve()
        str(library).encode('utf-8')
        if '\x00' in str(library):
            raise ValueError('NUL in data directory')
    except (TypeError, ValueError, OSError, UnicodeError) as error:
        raise ShortcutError('Tên truyện hoặc đường dẫn dữ liệu không hợp lệ.') from error
    project = Path(__file__).resolve().parent
    pythonw = Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file():
        raise ShortcutError('Không tìm thấy pythonw.exe cạnh Python đang chạy.')
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', title).strip(' .')[:80].rstrip(' .') or 'Truyện'
    safe_title = safe_title.encode('utf-16-le')[:160].decode('utf-16-le', errors='ignore').rstrip(' .') or 'Truyện'
    if re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?', safe_title):
        safe_title = '_' + safe_title
    identity = hashlib.sha256((os.path.normcase(str(library)) + '\0' + book_id).encode('utf-8')).hexdigest()[:16]
    arguments = subprocess.list2cmdline([str(project / 'run.py'), '--book=' + book_id,
                                       '--data-dir=' + str(library), '--port=' + str(port)])
    assets = library / 'shortcuts'
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    try:
        with _CREATE_LOCK:
            assets.mkdir(parents=True, exist_ok=True)
            desktop = desktop_directory()
            with tempfile.TemporaryDirectory(prefix='.create-', dir=assets) as temporary:
                folder = Path(temporary)
                cover_path = None
                if isinstance(cover_bytes, (bytes, bytearray)) and 0 < len(cover_bytes) <= MAX_COVER_BYTES:
                    cover_path = folder / 'cover'
                    cover_path.write_bytes(cover_bytes)
                input_path = folder / 'input.json'
                payload = {'desktop': str(desktop), 'filename': safe_title + ' - ' + identity,
                           'identity': identity, 'target': str(pythonw), 'arguments': arguments,
                           'working_directory': str(project), 'cover_path': str(cover_path) if cover_path else '',
                           'icon_path': str(assets / (identity + '.ico')), 'temporary': str(folder)}
                input_path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                completed = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File',
                                            str(SCRIPT), '-InputPath', str(input_path)], capture_output=True, timeout=30,
                                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
                try:
                    result = json.loads(completed.stdout.decode('utf-8-sig'))
                except (UnicodeError, json.JSONDecodeError) as error:
                    detail = completed.stderr.decode('utf-8', errors='replace')[-1200:]
                    raise ShortcutError('Windows không trả kết quả lối tắt hợp lệ: ' + detail) from error
                if completed.returncode or not isinstance(result, dict) or result.get('error'):
                    detail = str(result.get('error', 'engine failed')) if isinstance(result, dict) else 'invalid payload'
                    if isinstance(result, dict) and result.get('hresult'):
                        detail += ' (' + str(result['hresult']) + ')'
                    raise ShortcutError('Không tạo được lối tắt Windows: ' + detail)
                if not isinstance(result.get('path'), str) or not isinstance(result.get('used_cover'), bool) or not Path(result['path']).is_file():
                    raise ShortcutError('Windows chưa tạo tệp lối tắt hợp lệ.')
                return {'path': result['path'], 'used_cover': result['used_cover']}
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ShortcutError('Không ghi được lối tắt Windows: ' + str(error)) from error
