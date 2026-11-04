"""Exercise the actual app and reader in an isolated, installed headless browser."""
import json
import base64
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


WEB = Path(__file__).resolve().parents[1] / 'web'
BROWSER = next((str(path) for path in (
    shutil.which('google-chrome'), shutil.which('chromium'),
    Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
    Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Microsoft/Edge/Application/msedge.exe',
) if path and Path(path).is_file()), None)
NODE = shutil.which('node')

READ_RESULT = r"""
const pages = await fetch(process.argv[1] + '/json/list').then(r => r.json());
const ws = new WebSocket(pages.find(p => p.type === 'page').webSocketDebuggerUrl);
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject; });
let id = 0; const replies = new Map();
ws.onmessage = event => { const r = JSON.parse(event.data); if (r.id) { replies.get(r.id)?.(r); replies.delete(r.id); } };
const evaluate = () => new Promise(resolve => {
  const next = ++id; replies.set(next, resolve);
  ws.send(JSON.stringify({id: next, method: 'Runtime.evaluate', params: {expression: "document.querySelector('#qa-result')?.textContent", returnByValue: true}}));
});
for (let i = 0; i < 400; i++) {
  const result = await evaluate();
  if (result.result?.result?.value) { process.stdout.write(result.result.result.value); break; }
  await new Promise(r => setTimeout(r, 50));
}
ws.send(JSON.stringify({id: ++id, method: 'Browser.close'}));
await new Promise(r => setTimeout(r, 100)); ws.close();
"""

CHECKS = r"""
const $ = (query) => document.querySelector(query);
const check = (condition, message) => { if (!condition) throw Error(message); };
const wait = async (test) => {
  for (let i = 0; i < 300; i++) { if (await test()) return; await new Promise(r => setTimeout(r, 20)); }
  throw Error('Timed out: ' + test);
};
const state = () => fetch('/qa/state').then(r => r.json());
const ready = () => wait(() => $('#reader').dataset.state === 'reading');
const denied = () => wait(() => $('#reader').dataset.state === 'error');
const button = (text) => [...document.querySelectorAll('#reader button')].find(n => n.textContent === text);
const toc = (index) => {
  $('#reader button[aria-label="Mục lục"]').click();
  document.querySelectorAll('.toc-item')[index].click();
};
const ratio = () => { const s = $('.reader-scroll'); return s.scrollTop / (s.scrollHeight - s.clientHeight); };
try {
  if (location.search.includes('direct')) {
    await denied();
    check((await state()).progress.length === 0, 'Direct denial saved unread chapter');
    const area = $('.reader-scroll').getBoundingClientRect();
    check(getComputedStyle($('.reader-scroll')).overflowY === 'auto', 'Paged error cannot scroll vertically');
    for (const control of document.querySelectorAll('.reader-access-actions button')) {
      control.scrollIntoView({block: 'nearest'});
      const rect = control.getBoundingClientRect();
      check(rect.left >= area.left && rect.right <= area.right && rect.bottom <= area.bottom, 'Paged error actions are clipped');
    }
    button('Quay về chương trước').click(); await ready();
    check($('.reader-scroll').scrollLeft === 0 && (await state()).progress.at(-1).position === 0, 'Adjacent fallback invented a saved position');
  } else {
    await ready();
    const image = $('.chapter-content img');
    check(image?.src.startsWith('data:image/svg+xml;base64,'), 'Reader removed the sanitized SVG document image');
    image.loading = 'eager';
    await wait(() => image.complete);
    check(image.naturalWidth === 20 && image.naturalHeight === 20, 'SVG document image did not render');
    check(!$('.chapter-content svg'), 'SVG became inline active markup');
    check($('.chapter-content').textContent.includes('Tiếng Việt từ website gốc'), 'Source Vietnamese was changed');
    check((await state()).qt === 0, 'Saved QT preference translated source Vietnamese');
    window.dispatchEvent(new CustomEvent('dictionarychange', {detail: '*'}));
    $('#reader button[aria-label="QT và từ điển"]').click();
    check($('#dialog select').disabled, 'Source translation allows local QT'); $('#dialog-close').click();
    const s = $('.reader-scroll'); s.scrollTop = (s.scrollHeight - s.clientHeight) * .61;
    s.dispatchEvent(new Event('scroll')); const savedPosition = ratio();
    $('#reader button[aria-label="Mục lục"]').click();
    check(document.querySelectorAll('.toc-vip').length === 1, 'TOC invented VIP locks');
    check(!document.querySelectorAll('.toc-item')[1].querySelector('.toc-vip'), 'Unknown chapter was marked VIP before verification');
    check($('.toc-vip').getAttribute('aria-label').includes('quyền đọc chưa được kiểm tra'), 'TOC lock asserts entitlement');
    document.querySelectorAll('.toc-item')[1].click(); await denied();
    check($('.vip-label')?.textContent === 'VIP', 'Verified VIP label missing');
    check($('.reading-sheet h1').textContent === 'Chương 2', 'Denied chapter title missing');
    $('#reader button[aria-label="Mục lục"]').click();
    check(document.querySelectorAll('.toc-item')[1].querySelector('.toc-vip'), 'Verified denial did not update the open TOC metadata');
    check(document.querySelectorAll('.toc-vip').length === 2, 'Verified denial changed unrelated TOC metadata');
    $('#dialog-close').click();
    button('Mở trên website gốc').click(); await wait(async () => (await state()).original.length === 1);
    check((await state()).original[0] === '/api/books/book/chapters/1/original', 'Original action used wrong chapter');
    document.dispatchEvent(new KeyboardEvent('keydown', {key: 'End', bubbles: true}));
    s.dispatchEvent(new Event('scroll'));
    await new Promise(r => setTimeout(r, 1200));
    let log = await state();
    check(log.progress.every(p => p.index === 0), 'Denial advanced progress');
    check(Math.abs(log.progress.at(-1).position - savedPosition) < .025, 'Previous position was not saved');
    button('Quay về chương trước').click(); await ready();
    check(Math.abs(ratio() - savedPosition) < .025, 'Previous chapter lost saved position');
    for (const [index, title] of [[2, 'HTTP 403'], [3, 'HTTP 404'], [4, 'Chưa xác minh quyền truy cập'], [5, 'Cần đăng nhập tại website gốc'], [6, 'Chưa xác minh quyền truy cập'], [9, 'Chưa kết nối được nguồn'], [10, 'Chưa nhận diện được nội dung chương'], [11, 'Chưa xác minh quyền truy cập'], [12, 'Chưa nhận diện được nội dung chương'], [13, 'Nguồn phản hồi quá lâu'], [14, 'Trình duyệt nguồn chưa sẵn sàng'], [15, 'Chưa kết nối được trình duyệt nguồn']]) {
      toc(index); await denied();
      check(!$('.vip-label'), 'Unverified or ordinary error became VIP: ' + index);
      check($('.reader-access-error h2').textContent.includes(title), 'Wrong error screen: ' + index);
      $('#reader button[aria-label="Mục lục"]').click();
      check(document.querySelectorAll('.toc-vip').length === 2 && !document.querySelectorAll('.toc-item')[index].querySelector('.toc-vip'), 'An ordinary error marked the TOC as VIP: ' + index);
      $('#dialog-close').click();
    }
    toc(7); await wait(async () => (await state()).slow);
    toc(2); await denied();
    await new Promise(r => setTimeout(r, 700));
    check(!$('.vip-label') && $('.reading-sheet h1').textContent === 'Chương 3', 'Stale VIP request replaced active error');
    check((await state()).progress.every(p => p.index === 0), 'Stale or failed chapter saved progress');
    toc(8); await ready();
    check($('.chapter-content').textContent.includes('Bản QT thử nghiệm'), 'Existing source QT stopped working');
    check((await state()).qt === 1, 'Unexpected QT requests');
  }
  const result = document.createElement('pre'); result.id = 'qa-result'; result.textContent = 'PASS'; document.body.append(result);
} catch (error) {
  const result = document.createElement('pre'); result.id = 'qa-result'; result.textContent = error.stack + '\nReader: ' + $('#reader').dataset.state + ' ' + $('.reader-status')?.textContent + ' ' + $('.chapter-content')?.textContent.slice(0, 200); document.body.append(result);
}
"""


@unittest.skipUnless(BROWSER and NODE, 'Installed Chrome/Chromium/Edge and Node are required for real DOM checks')
class ReaderAccessTests(unittest.TestCase):
    def test_access_screens_progress_and_source_translation(self):
        for book_id in ('book', 'direct'):
            with self.subTest(entry=book_id), tempfile.TemporaryDirectory(prefix='reader-access-') as profile:
                calls = {'progress': [], 'qt': 0, 'original': [], 'slow': False}

                class Handler(BaseHTTPRequestHandler):
                    def log_message(self, *_):
                        pass

                    def reply(self, value, status=200, content_type='application/json'):
                        body = json.dumps(value, ensure_ascii=False).encode('utf-8') if content_type == 'application/json' else value.encode('utf-8')
                        self.send_response(status)
                        self.send_header('Content-Type', content_type + '; charset=utf-8')
                        self.send_header('Content-Length', str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def do_GET(self):
                        path = urlsplit(self.path).path
                        if path == '/':
                            return self.reply(WEB.joinpath('index.html').read_text(encoding='utf-8').replace('</body>', '<script type="module" src="/qa.js"></script></body>'), content_type='text/html')
                        if path in ('/app.js', '/reader.js', '/style.css'):
                            return self.reply(WEB.joinpath(path[1:]).read_text(encoding='utf-8'), content_type='text/css' if path.endswith('.css') else 'text/javascript')
                        if path == '/qa.js':
                            return self.reply(CHECKS, content_type='text/javascript')
                        if path == '/qa/state':
                            return self.reply(calls)
                        if path == '/api/settings':
                            return self.reply({'reader': {'qt': 'qt', 'mode': 'paged' if book_id == 'direct' else 'scroll'}})
                        if path in ('/api/groups', '/api/sources'):
                            return self.reply([])
                        book = {'id': book_id, 'name': 'Thử quyền đọc', 'source': 'file', 'chapter_index': 1 if book_id == 'direct' else 0, 'position': .37, 'chapter_count': 16}
                        if path == '/api/books':
                            return self.reply([book])
                        if path == f'/api/books/{book_id}':
                            return self.reply(book)
                        if path.endswith('/chapters'):
                            return self.reply([{'index': i, 'title': f'Chương {i + 1}', **({'is_vip': True} if i == 8 else {})} for i in range(16)])
                        if '/chapters/' in path:
                            index = int(path.rsplit('/', 1)[-1])
                            if index in (0, 8):
                                text = '\n'.join(f'Tiếng Việt từ website gốc, đoạn {i}.' for i in range(100))
                                from htmltools import sanitize_svg
                                svg = sanitize_svg(b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><rect width="20" height="20" fill="#000"/></svg>')
                                image = 'data:image/svg+xml;base64,' + base64.b64encode(svg).decode()
                                markup = '<img src="' + image + '">' + ''.join('<p>' + p + '</p>' for p in text.splitlines())
                                return self.reply({'title': f'Chương {index + 1}', 'text': text, 'html': markup, **({'language': 'vi', 'translation': 'source'} if index == 0 else {})})
                            if index == 7:
                                calls['slow'] = True
                                time.sleep(.4)
                            if index == 12:
                                return self.reply('{invalid-json', content_type='application/problem+json')
                            code = {1: 'vip_locked', 2: 'http_error', 3: 'http_error', 4: 'vip_locked', 5: 'auth_required', 6: 'access_unverified', 7: 'vip_locked', 9: 'network_error', 10: 'parse_error', 11: 'vip_locked', 13: 'source_timeout', 14: 'browser_unavailable', 15: 'browser_error'}[index]
                            status = 503 if index >= 13 else 404 if index == 3 else 403
                            error = {'error': 'Lỗi thử nghiệm', 'code': code, 'http_status': status}
                            if code != 'http_error':
                                error['access'] = {'vip': index in (1, 4, 7), 'state': code, 'evidence': '' if index == 4 else 'Explicit fixture access message'}
                            return self.reply(error, status)
                        return self.reply({'error': 'Unknown fixture route'}, 404)

                    def do_POST(self):
                        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
                        if self.path.endswith('/progress'):
                            calls['progress'].append(data)
                        elif self.path.endswith('/original'):
                            calls['original'].append(self.path)
                        elif self.path == '/api/qt':
                            calls['qt'] += 1
                            return self.reply({'text': 'Bản QT thử nghiệm'})
                        return self.reply({'ok': True})

                server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    # The fixture serves only local test assets; AppContainer cannot run in the agent sandbox.
                    browser = subprocess.Popen([BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
                                                '--disable-background-networking', '--disable-component-update', '--disable-sync',
                                                '--user-data-dir=' + profile, '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
                                                f'http://127.0.0.1:{server.server_port}/?book={book_id}'],
                                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                    try:
                        port_file = Path(profile) / 'DevToolsActivePort'
                        for _ in range(200):
                            if port_file.is_file():
                                break
                            time.sleep(.025)
                        self.assertTrue(port_file.is_file(), 'Headless browser did not start')
                        endpoint = 'http://127.0.0.1:' + port_file.read_text(encoding='utf-8').splitlines()[0]
                        result = subprocess.run([NODE, '--input-type=module', '-e', READ_RESULT, endpoint], capture_output=True,
                                                encoding='utf-8', errors='replace', timeout=30)
                        self.assertEqual(result.stdout, 'PASS', result.stdout + result.stderr)
                    finally:
                        if browser.poll() is None:
                            browser.terminate()
                        browser.wait(timeout=5)
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=3)


if __name__ == '__main__':
    unittest.main()
