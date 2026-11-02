"""Start Kệ Truyện on loopback, optionally import a backup and open an app window."""
import argparse
import logging
import os
import subprocess
import sys
import webbrowser
from pathlib import Path
from server import make_server

def main():
    parser = argparse.ArgumentParser(description="Kệ Truyện — thư viện cá nhân trên máy.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", type=Path, default=Path(__file__).resolve().parent / "user-data")
    parser.add_argument("--import-backup", type=Path)
    parser.add_argument("--import-qt", type=Path, help="Nhập mục từ còn thiếu từ gói QT ZIP vào dữ liệu riêng.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=str(args.data_dir.parent / "reader.log"), level=logging.WARNING,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    try:
        server = make_server(args.data_dir, port=args.port)
    except OSError as error:
        print("Không mở được cổng cục bộ. Có thể app đã chạy:", error)
        return 1
    if args.import_backup:
        result = server.app.store.import_backup(args.import_backup)
        print(f"Khôi phục {result['books']} truyện, {result['chapters']} mục chương.")
    if args.import_qt:
        import json
        from qt_pack import install_pack
        report = install_pack(server.app.qt, args.import_qt)
        (args.data_dir / 'qt-install-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Đã thêm', sum(item['inserted'] for item in report['files'].values()), 'mục QT; nhóm còn thiếu:', ', '.join(report['pending']))
    url = f"http://127.0.0.1:{server.server_address[1]}"
    print("Kệ Truyện đang chạy tại", url)
    print("Dữ liệu riêng:", args.data_dir.resolve())
    print("Giữ cửa sổ này mở. Nhấn Ctrl+C để dừng.")
    if not args.no_browser:
        candidates = [
            Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
        ]
        browser = next((p for p in candidates if p.is_file()), None)
        if browser:
            subprocess.Popen([str(browser), "--app=" + url, "--new-window"], close_fds=True)
        else:
            webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0

if __name__ == "__main__":
    sys.exit(main())
