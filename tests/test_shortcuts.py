import ctypes
import json
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
import zlib

import shortcuts


WORKSPACE = Path(__file__).resolve().parents[1]
QA_ROOT = WORKSPACE / '.qa'


def cover_png(width=2, height=4):
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    rows = b''.join(b'\0' + b'\xff\0\0\xff' * width for _ in range(height))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 6, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(rows)) + chunk(b'IEND', b''))


def native_link(path, target=None, arguments=None):
    """Inspect with native Unicode COM independently of the PowerShell writer."""
    ole = ctypes.WinDLL('ole32')
    ole.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole.CoInitializeEx.restype = ctypes.c_long
    initialized = ole.CoInitializeEx(None, 2)
    ole.CoCreateInstance.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    ole.CoCreateInstance.restype = ctypes.c_long
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    def guid(value):
        return (ctypes.c_ubyte * 16).from_buffer_copy(uuid.UUID(value).bytes_le)
    def call(pointer, slot, types, *values):
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[slot])
        status = method(pointer, *values)
        if status < 0:
            raise OSError(f'Native shortcut COM failed: 0x{status & 0xffffffff:08X}')
    try:
        clsid = guid('00021401-0000-0000-c000-000000000046')
        iid = guid('000214f9-0000-0000-c000-000000000046')
        status = ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(link))
        if status < 0:
            raise OSError(f'Native shortcut COM unavailable: 0x{status & 0xffffffff:08X}')
        iid_persist = guid('0000010b-0000-0000-c000-000000000046')
        call(link, 0, [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)], ctypes.byref(iid_persist), ctypes.byref(persist))
        call(persist, 5, [ctypes.c_wchar_p, ctypes.c_uint32], str(path), 0)
        if target is not None:
            call(link, 20, [ctypes.c_wchar_p], target)
            call(link, 11, [ctypes.c_wchar_p], arguments)
            call(persist, 6, [ctypes.c_wchar_p, ctypes.c_int], str(path), 1)
        result = {}
        for key, slot in (('target', 3), ('arguments', 10), ('working_directory', 8), ('icon', 16)):
            buffer = ctypes.create_unicode_buffer(32768)
            types = [ctypes.c_wchar_p, ctypes.c_int]
            values = [buffer, len(buffer)]
            icon_index = ctypes.c_int()
            if key == 'target':
                types += [ctypes.c_void_p, ctypes.c_uint32]
                values += [None, 4]
            elif key == 'icon':
                types += [ctypes.POINTER(ctypes.c_int)]
                values += [ctypes.byref(icon_index)]
            call(link, slot, types, *values)
            result[key] = buffer.value + (',' + str(icon_index.value) if key == 'icon' else '')
        return result
    finally:
        if persist:
            call(persist, 2, [])
        if link:
            call(link, 2, [])
        if initialized >= 0:
            ole.CoUninitialize()


NATIVE_INSPECTOR = r'''
param([string]$InputPath)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$data = [IO.File]::ReadAllText($InputPath, [Text.Encoding]::UTF8) | ConvertFrom-Json
$result = @{}
if ($data.icon) {
    Add-Type -AssemblyName System.Drawing
    $bytes = [IO.File]::ReadAllBytes($data.icon)
    $offset = [BitConverter]::ToUInt32($bytes, 18)
    $stream = [IO.MemoryStream]::new($bytes, $offset, $bytes.Length - $offset)
    $bitmap = [Drawing.Bitmap]::FromStream($stream)
    try {
        $result.width = $bitmap.Width
        $result.height = $bitmap.Height
        $result.corner_alpha = $bitmap.GetPixel(0, 0).A
        $result.center_red = $bitmap.GetPixel(128, 128).R
        $result.center_alpha = $bitmap.GetPixel(128, 128).A
    } finally { $bitmap.Dispose(); $stream.Dispose() }
}
$result | ConvertTo-Json -Compress
'''


class ShortcutTests(unittest.TestCase):
    def test_non_windows_reports_a_clear_error_without_launching(self):
        with patch('shortcuts.sys.platform', 'linux'), patch('shortcuts.subprocess.run', side_effect=AssertionError('must not launch')):
            with self.assertRaisesRegex(shortcuts.ShortcutError, 'Windows'):
                shortcuts.create_shortcut(book_id='book', title='Book', cover_bytes=None, data_dir=WORKSPACE, port=8765)
            with self.assertRaisesRegex(shortcuts.ShortcutError, 'Windows'):
                shortcuts.desktop_directory()

    def test_invalid_identity_and_port_are_rejected_before_creating_files(self):
        for fields in ({'book_id': ''}, {'book_id': 'x' * 257}, {'book_id': 'bad\0id'}, {'book_id': '\ud800'}, {'title': '\ud800'},
                       {'port': 0}, {'port': 65536}, {'port': True}, {'port': '8765'}):
            with self.subTest(fields=str(fields)), patch('shortcuts.subprocess.run', side_effect=AssertionError('must not launch')):
                values = dict(book_id='book', title='Book', cover_bytes=None, data_dir=WORKSPACE, port=8765)
                values.update(fields)
                with self.assertRaises(shortcuts.ShortcutError):
                    shortcuts.create_shortcut(**values)


@unittest.skipUnless(sys.platform == 'win32', 'Windows COM shortcut integration')
class NativeShortcutTests(unittest.TestCase):
    def setUp(self):
        QA_ROOT.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='shortcuts-', dir=QA_ROOT)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.desktop = self.root / 'Desktop tiếng Việt'
        self.desktop.mkdir()
        self.data_dir = self.root / "Dữ liệu & $(never-execute) 'quoted'"
        self.patcher = patch('shortcuts.desktop_directory', return_value=self.desktop)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.inspector = self.root / 'inspect.ps1'
        self.inspector.write_text(NATIVE_INSPECTOR, encoding='utf-8')

    def create(self, **updates):
        values = dict(book_id='book-đọc "quoted" & $(never-execute)', title='CON: Cổ tích / "đẹp"? * | < >',
                      cover_bytes=cover_png(), data_dir=self.data_dir, port=18765)
        values.update(updates)
        return shortcuts.create_shortcut(**values)

    def inspect(self, path, **updates):
        info = native_link(path, updates.get('target'), updates.get('arguments'))
        if not updates.get('icon'):
            return info
        input_path = self.root / 'inspect.json'
        values = dict(path=str(path), action='read')
        values.update(updates)
        input_path.write_text(json.dumps(values, ensure_ascii=False), encoding='utf-8')
        executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        process = subprocess.run([str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(self.inspector),
                                  '-InputPath', str(input_path)], capture_output=True, timeout=30,
                                 creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        self.assertEqual(process.returncode, 0, process.stderr.decode('utf-8', errors='replace'))
        return {**info, **json.loads(process.stdout.decode('utf-8-sig'))}

    def test_unicode_unsafe_title_and_quoted_arguments_create_a_square_transparent_cover_icon(self):
        result = self.create()
        link_path = Path(result['path'])
        self.assertTrue(result['used_cover'])
        self.assertEqual(link_path.parent, self.desktop)
        self.assertTrue(link_path.is_file())
        self.assertFalse(any(char in link_path.name for char in '<>:"/\\|?*'))
        self.assertIn('Cổ tích', link_path.name)
        info = self.inspect(link_path)
        self.assertEqual(Path(info['target']), Path(sys.executable).with_name('pythonw.exe'))
        self.assertEqual(info['arguments'], subprocess.list2cmdline([
            str(WORKSPACE / 'run.py'), '--book=book-đọc "quoted" & $(never-execute)',
            '--data-dir=' + str(self.data_dir.resolve()), '--port=18765']))
        self.assertEqual(Path(info['working_directory']), WORKSPACE)
        icon_path = Path(info['icon'].rsplit(',', 1)[0])
        self.assertEqual(icon_path.parent, self.data_dir / 'shortcuts')
        self.assertEqual(icon_path.read_bytes()[:6], b'\0\0\1\0\1\0')
        user = ctypes.WinDLL('user32', use_last_error=True)
        user.LoadImageW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        user.LoadImageW.restype = ctypes.c_void_p
        user.DestroyIcon.argtypes = [ctypes.c_void_p]
        handle = user.LoadImageW(None, str(icon_path), 1, 256, 256, 0x10)
        self.assertTrue(handle, f'Windows could not load ICO: {ctypes.get_last_error()}')
        user.DestroyIcon(handle)
        image = self.inspect(link_path, icon=str(icon_path))
        self.assertEqual((image['width'], image['height']), (256, 256))
        self.assertEqual(image['corner_alpha'], 0)
        self.assertEqual(image['center_red'], 255)
        self.assertEqual(image['center_alpha'], 255)

    def test_repeated_clicks_reuse_one_link_and_reserved_title_is_safe(self):
        first = self.create(title='CON')
        link = Path(first['path'])
        original = link.read_bytes()
        modified = link.stat().st_mtime_ns
        second = self.create(title='CON')
        self.assertEqual(first, second)
        self.assertEqual(list(self.desktop.glob('*.lnk')), [Path(first['path'])])
        self.assertEqual(link.read_bytes(), original)
        self.assertEqual(link.stat().st_mtime_ns, modified)
        self.assertNotEqual(Path(first['path']).stem.split('.')[0].upper(), 'CON')

    def test_long_emoji_title_stays_within_windows_filename_limits(self):
        result = self.create(title='📚' * 80, cover_bytes=None)
        self.assertLessEqual(len(Path(result['path']).name.encode('utf-16-le')) // 2, 120)

    def test_failed_update_preserves_the_existing_link(self):
        result = self.create()
        link = Path(result['path'])
        original = link.read_bytes()
        link.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(shortcuts.ShortcutError):
                self.create(cover_bytes=None)
            self.assertEqual(link.read_bytes(), original)
            self.assertEqual(list(self.desktop.iterdir()), [link])
        finally:
            link.chmod(stat.S_IWRITE)

    def test_collision_keeps_an_unrelated_shortcut_unchanged(self):
        first = self.create()
        path = Path(first['path'])
        self.inspect(path, action='create', target=str(Path(sys.executable)), arguments='--unrelated')
        original = path.read_bytes()
        second = self.create()
        self.assertNotEqual(second['path'], first['path'])
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.create(), second)
        self.assertEqual(len(list(self.desktop.glob('*.lnk'))), 2)

    def test_invalid_missing_and_oversized_cover_use_default_icon(self):
        for index, data in enumerate((None, b'not an image', b'x' * (16 * 1024 * 1024 + 1), cover_png(8193, 1))):
            with self.subTest(index=index):
                result = self.create(book_id=f'fallback-{index}', cover_bytes=data)
                self.assertFalse(result['used_cover'])
                info = self.inspect(result['path'])
                self.assertFalse(info['icon'].lower().split(',')[0].endswith('.ico'))
                self.assertTrue(Path(result['path']).is_file())

    def test_filesystem_errors_are_reported_without_false_success(self):
        blocker = self.root / 'not-a-directory'
        blocker.write_bytes(b'keep')
        with self.assertRaises(shortcuts.ShortcutError):
            self.create(data_dir=blocker)
        self.assertEqual(blocker.read_bytes(), b'keep')
        self.assertEqual(list(self.desktop.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
