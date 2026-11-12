"""Start Kệ Truyện on loopback, optionally import a backup and open an app window."""
import argparse
import http.client
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import sys
import time
import threading
import webbrowser
from pathlib import Path
from urllib.parse import urlencode
from server import make_server

def running_instance(data_dir, port):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=.5)
    try:
        connection.request('GET', '/api/instance')
        response = connection.getresponse()
        if response.status != 200:
            return False
        identity = json.loads(response.read(16_385))
        return (identity.get('app') == 'ke-truyen-local'
                and os.path.normcase(os.path.normpath(identity['data_directory'])) == os.path.normcase(str(Path(data_dir).resolve())))
    except (OSError, http.client.HTTPException, ValueError, TypeError, KeyError, AttributeError):
        return False
    finally:
        connection.close()


def open_window(url):
    candidates = [
        Path(os.environ.get('ProgramFiles', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
        Path(os.environ.get('ProgramFiles(x86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
    ]
    browser = next((path for path in candidates if path.is_file()), None)
    if browser:
        subprocess.Popen([str(browser), '--app=' + url, '--new-window'], close_fds=True)
    else:
        webbrowser.open(url)


def report_error(message):
    logging.error(message)
    if sys.stdout is not None:
        print(message)
    elif sys.platform == 'win32':
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, 'Kệ Truyện', 0x10)


def configure_logging(data_dir):
    logging.basicConfig(handlers=[RotatingFileHandler(str(Path(data_dir).parent / 'reader.log'),
        maxBytes=2 * 1024 * 1024, backupCount=2, encoding='utf-8', delay=True)],
        level=logging.WARNING, format='%(asctime)s %(levelname)s %(message)s')


def console_close_handler(server):
    if sys.platform != 'win32': return lambda: None
    import ctypes
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    @callback_type
    def control(event):
        if event not in (2, 5, 6): return False
        try: server.app.begin_shutdown(True, force=True, timeout=.5)
        except Exception: logging.exception('Console closed before pause could be persisted; startup recovery remains necessary.')
        threading.Thread(target=server.shutdown, name='console-shutdown', daemon=True).start()
        return True
    if not ctypes.windll.kernel32.SetConsoleCtrlHandler(control, True): return lambda: None
    return lambda: ctypes.windll.kernel32.SetConsoleCtrlHandler(control, False)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Kệ Truyện — thư viện cá nhân trên máy.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "user-data")
    parser.add_argument("--import-backup", type=Path)
    parser.add_argument("--import-qt", type=Path, help="Nhập mục từ còn thiếu từ gói QT ZIP vào dữ liệu riêng.")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument('--book', help='Mở trực tiếp truyện từ lối tắt.')
    args = parser.parse_args(argv)
    if args.book is not None and (not args.book or len(args.book) > 256 or any(ord(char) < 32 for char in args.book)):
        parser.error('Mã truyện không hợp lệ.')
    query = '/?' + urlencode({'book': args.book}) if args.book is not None else ''
    reuse = not (args.import_backup or args.import_qt)
    if reuse and running_instance(args.data_dir, args.port):
        if not args.no_browser:
            open_window(f'http://127.0.0.1:{args.port}' + query)
        return 0
    args.data_dir.mkdir(parents=True, exist_ok=True)
    configure_logging(args.data_dir)
    for attempt in range(81):
        try:
            server = make_server(args.data_dir, port=args.port)
            break
        except OSError as error:
            if reuse:
                if running_instance(args.data_dir, args.port):
                    if not args.no_browser:
                        open_window(f'http://127.0.0.1:{args.port}' + query)
                    return 0
            if not reuse or attempt == 80:
                report_error('Không mở được Kệ Truyện. Cổng/thư viện đang được dùng hoặc máy chủ cũ chưa tắt xong: ' + str(error))
                return 1
            time.sleep(.25)
    unregister = console_close_handler(server)
    try:
        if args.import_backup:
            with server.app.critical_operation('nhập thư viện khởi động'):
                result = server.app.store.import_backup(args.import_backup)
            print(f"Khôi phục {result['books']} truyện, {result['chapters']} mục chương.")
        if args.import_qt:
            from qt_pack import install_pack
            with server.app.critical_operation('nhập gói QT khởi động'):
                report = install_pack(server.app.qt, args.import_qt)
            (args.data_dir / 'qt-install-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print('Đã thêm', sum(item['inserted'] for item in report['files'].values()), 'mục QT; nhóm còn thiếu:', ', '.join(report['pending']))
        url = f"http://127.0.0.1:{server.server_address[1]}" + query
        print("Kệ Truyện đang chạy tại", url)
        print("Dữ liệu riêng:", args.data_dir.resolve())
        print("Giữ cửa sổ này mở. Nhấn Ctrl+C hoặc chọn Tắt máy chủ trong Thiết lập để dừng.")
        if not args.no_browser: open_window(url)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        closed = server.server_close()
        unregister()
    if not closed:
        report_error('Chưa tắt hoàn toàn: còn thao tác đang kết thúc hoặc lỗi dọn tài nguyên. Xem reader.log; không chạy thêm máy chủ cho thư viện này.')
    return 0 if closed else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:
        logging.exception('Cannot launch reader')
        report_error('Không mở được Kệ Truyện: ' + str(error))
        sys.exit(1)
