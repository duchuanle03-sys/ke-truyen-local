"""Local-only DOM regression: the CAPTCHA is a synthetic test image, never a live challenge."""
import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from server import Handler, ROOT, make_server
from stv_source import StvSource
from test_stv_http import HttpFixture, BOOK, CHAPTER, chapter
from test_reader_access import BROWSER, NODE, READ_RESULT

CHECKS = r"""
const $=q=>document.querySelector(q);
const check=(ok,message)=>{if(!ok)throw Error(message)};
const wait=async test=>{for(let i=0;i<250;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timed out: '+test)};
const ready=()=>wait(()=>$('#reader').dataset.state==='reading');
const popup=()=>wait(()=>$('#captcha-dialog')?.open && $('#captcha-image').naturalWidth>0 && !$('#captcha-answer').disabled);
const next=()=>[...document.querySelectorAll('#reader button')].find(b=>b.textContent==='Chương sau ›').click();
const ratio=()=>{const s=$('.reader-scroll');return s.scrollTop/(s.scrollHeight-s.clientHeight)};
const submit=()=>{$('#captcha-answer').value='manual-answer';$('#captcha-form').requestSubmit()};
try{
 await ready();
 let lines=$('.chapter-content').querySelectorAll('span');
 check(lines[1].getBoundingClientRect().top>lines[0].getBoundingClientRect().top,'Cached STV raw newlines collapsed');
 check($('.chapter-content').querySelectorAll('br').length===99,'Cached paragraph boundaries duplicated or lost');
 const s=$('.reader-scroll');s.scrollTop=(s.scrollHeight-s.clientHeight)*.61;s.dispatchEvent(new Event('scroll'));const saved=ratio();
 next();await popup();
 check($('.reading-sheet h1').textContent==='One','CAPTCHA replaced the readable chapter');
 check($('.chapter-content').textContent.includes('Nội dung đã lưu'),'CAPTCHA removed readable content');
 check(Math.abs(ratio()-saved)<.025,'CAPTCHA moved the reader');
 check((await fetch('/qa/state').then(r=>r.json())).progress.every(p=>p.index===0),'Challenge advanced reading progress');
 const width=$('#captcha-image').getBoundingClientRect().width;
 $('#captcha-zoom').value='300';$('#captcha-zoom').dispatchEvent(new Event('input'));
 check($('#captcha-image').getBoundingClientRect().width>width,'Image zoom did not enlarge the image');
 submit();await wait(()=>!$('#captcha-error').hidden && !$('#captcha-submit').disabled);
 check($('#captcha-error').textContent.includes('thất bại'),'Verification failure is not clear');
 check($('#captcha-dialog').open,'Failed verification closed the dialog');
 const old=$('#captcha-dialog').dataset.challengeId;$('#captcha-refresh').click();
 await wait(()=>$('#captcha-dialog').dataset.challengeId!==old);await popup();
 check($('#captcha-answer').value==='','Refresh retained the old answer');
 submit();await ready();check(!$('#captcha-dialog').open,'Successful verification left the dialog open');
 check($('.reading-sheet h1').textContent==='Two','Interrupted chapter was not retried');
 check($('.chapter-content').textContent==='Tiếng Việt rõ ràng.Đoạn hai.','Vietnamese paragraphs duplicated or changed');
 lines=$('.chapter-content').querySelectorAll('span');
 check(lines[1].getBoundingClientRect().top>lines[0].getBoundingClientRect().top,'New STV chapter raw newline collapsed');
 next();await popup();$('#captcha-cancel').click();await ready();
 check($('.reading-sheet h1').textContent==='Two','Cancel lost the readable chapter');
 check((await fetch('/qa/state').then(r=>r.json())).progress.every(p=>p.index!==2),'Cancelled challenge saved unread progress');
 next();await popup();submit();await ready();
 check($('.reading-sheet h1').textContent==='Three','Repeated website challenge did not resume');
 const result=document.createElement('pre');result.id='qa-result';result.textContent='PASS';document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack;document.body.append(result)}
"""


def fixture_server(directory):
    calls = {'progress': []}
    image = {'content_type': 'image/png', 'body_base64': 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j5XcAAAAASUVORK5CYII='}
    toc = {'code': 1, 'data': '1-/-901-/-One-//-1-/-902-/-Two-//-1-/-903-/-Three'}
    http = HttpFixture(toc, '<div>page</div>', {'code': '21'}, image, 'Xác thực bot thất bại', image, 'success', '<div>page</div>', chapter('902', data='<p><i t="中文" h="a">Tiếng Việt</i> rõ ràng.\n<span>Đoạn hai.</span></p>'),
                       '<div>page</div>', {'code': '21'}, image,
                       '<div>page</div>', {'code': '21'}, image, 'success', '<div>page</div>', chapter('903'))
    class QaHandler(Handler):
        def _route(self):
            if self.path.startswith('/?') or self.path == '/':
                markup = (ROOT/'web/index.html').read_text(encoding='utf-8').replace('</body>', '<script type="module" src="/qa.js"></script></body>')
                return self._reply(200, markup.encode('utf-8'), 'text/html; charset=utf-8')
            if self.path == '/qa.js':
                return self._reply(200, CHECKS.encode('utf-8'), 'text/javascript; charset=utf-8')
            if self.path == '/qa/state': return self._reply(200, calls)
            if self.path.endswith('/progress'):
                length = int(self.headers.get('Content-Length', 0))
                data = json.loads(self.rfile.read(length))
                calls['progress'].append(data)
                return self._reply(200, self.app.store.progress('captcha-book', data['index'], data['position']))
            super()._route()
    server = make_server(directory, port=0)
    server.RequestHandlerClass = QaHandler
    server.app.sources._stv_source = StvSource(http=http)
    server.app.store.upsert_book({'id': 'captcha-book', 'name': 'Kiểm tra CAPTCHA', 'source': 'stv', 'path': BOOK},
        [{'title': 'One', 'url': CHAPTER, 'html': '<p>Ghi chú nguồn</p>' + '\n'.join('<span>Nội dung đã lưu, đoạn %s.</span>'%i for i in range(100))},
         {'title': 'Two', 'url': BOOK+'902/'}, {'title': 'Three', 'url': BOOK+'903/'}])
    server.app.store.progress('captcha-book', 0, .37)
    return server, http, calls


@unittest.skipUnless(BROWSER and NODE, 'Installed browser and Node are required for local DOM checks')
class StvUiTests(unittest.TestCase):
    def test_in_app_manual_captcha_and_reader_resume(self):
        with tempfile.TemporaryDirectory(prefix='stv-ui-') as temporary:
            server, http, calls = fixture_server(Path(temporary)/'library')
            thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
            browser = subprocess.Popen([BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run', '--no-default-browser-check',
                '--disable-background-networking', '--disable-component-update', '--disable-sync', '--user-data-dir='+str(Path(temporary)/'profile'),
                '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', 'http://127.0.0.1:'+str(server.server_port)+'/?book=captcha-book'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                port_file = Path(temporary)/'profile/DevToolsActivePort'
                for _ in range(200):
                    if port_file.exists(): break
                    time.sleep(.025)
                self.assertTrue(port_file.exists(), 'Local test browser did not start')
                result = subprocess.run([NODE, '--input-type=module', '-e', READ_RESULT, 'http://127.0.0.1:'+port_file.read_text().splitlines()[0]],
                    capture_output=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.stdout, 'PASS', result.stdout+result.stderr)
                self.assertEqual(http.responses, [])
            finally:
                if browser.poll() is None: browser.terminate()
                browser.wait(timeout=5)
                server.shutdown();server.server_close();thread.join(3)
