"""Real reader/search integration using only a synthetic local library."""
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from library import Store
from server import Handler, ROOT, make_server
from test_reader_access import BROWSER, NODE, READ_RESULT


CHECKS = r"""
const $=q=>document.querySelector(q);
const check=(ok,message)=>{if(!ok)throw Error(message)};
const wait=async test=>{for(let i=0;i<300;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timed out: '+test)};
const more=()=>[...document.querySelectorAll('#dialog button')].find(b=>b.textContent==='Xem thêm kết quả');
const openSearch=()=>$('#reader button[aria-label="Tìm trong nội dung"]').click();
const submit=()=>{
 $('#dialog input[aria-label="Tìm trong nội dung"]').value='SHARED phrase';
 $('#dialog select[aria-label="Phạm vi tìm"]').value='book';
 $('#dialog form').requestSubmit();
};
try{
 await wait(()=>$('#reader').dataset.state==='reading');
 openSearch();submit();
 await wait(()=>document.querySelectorAll('.whole-book-result').length===100);
 check($('#dialog').textContent.includes('205/205'),'Complete coverage was not reported');
 more().click();await wait(()=>document.querySelectorAll('.whole-book-result').length===200);
 more().click();await wait(()=>document.querySelectorAll('.whole-book-result').length===205 && !more());
 const labels=[...document.querySelectorAll('.whole-book-result strong')].map(e=>e.textContent);
 check(new Set(labels).size===205,'Pagination duplicated chapter results');
 document.querySelectorAll('.whole-book-result')[204].click();
 await wait(()=>!$('#dialog').open && $('.reading-sheet h1').textContent==='Chapter 204');
 await wait(()=>document.querySelectorAll('mark.search-hit').length>0);
 await fetch('/qa/change');openSearch();submit();
 await wait(()=>$('#dialog .inline-info')?.textContent.includes('204/205'));
 check($('#dialog .inline-info').textContent.includes('Đang chuẩn bị'),'Partial indexing was reported as a complete search');
 const result=document.createElement('pre');result.id='qa-result';result.textContent='PASS';document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack+'\nDialog: '+$('#dialog')?.textContent.slice(0,500);document.body.append(result)}
"""


def fixture_server(directory):
    store = Store(directory)
    store.upsert_book({'id': 'search-ui', 'name': 'Synthetic search QA', 'source': 'file'},
                      [{'title': f'Chapter {i}', 'text': f'Shared phrase in chapter {i}.',
                        'html': f'<p>Shared phrase in chapter {i}.</p>'} for i in range(205)])
    while store.index_search():
        pass
    server = make_server(directory, port=0)
    server.app._asset_stop.set()
    server.app._asset_worker.join(2)
    class QaHandler(Handler):
        def _route(self):
            if self.path == '/' or self.path.startswith('/?'):
                markup = (ROOT / 'web/index.html').read_text(encoding='utf-8')
                if 'manual=1' not in self.path:
                    markup = markup.replace('</body>', '<script type="module" src="/qa.js"></script></body>')
                return self._reply(200, markup.encode('utf-8'), 'text/html; charset=utf-8')
            if self.path == '/qa.js':
                return self._reply(200, CHECKS.encode('utf-8'), 'text/javascript; charset=utf-8')
            if self.path == '/qa/change':
                server.app.store.save_content('search-ui', 204, {'html': '<p>Updated phrase.</p>', 'text': 'Updated phrase.'})
                return self._reply(200, {'updated': True})
            return super()._route()
    server.RequestHandlerClass = QaHandler
    return server


@unittest.skipUnless(BROWSER and NODE, 'Installed browser and Node are required for local DOM checks')
class SearchUiTests(unittest.TestCase):
    def test_reader_pages_results_opens_hits_and_explains_partial_indexing(self):
        with tempfile.TemporaryDirectory(prefix='search-ui-') as temporary:
            server = fixture_server(Path(temporary) / 'library')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = subprocess.Popen([BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
                '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync',
                '--user-data-dir=' + str(Path(temporary) / 'profile'), '--remote-debugging-port=0',
                '--remote-debugging-address=127.0.0.1', f'http://127.0.0.1:{server.server_port}/?book=search-ui'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                port_file = Path(temporary) / 'profile/DevToolsActivePort'
                port = None
                for _ in range(240):
                    try:
                        port = port_file.read_text().splitlines()[0]
                        break
                    except (OSError, IndexError):
                        time.sleep(.025)
                self.assertIsNotNone(port, 'Local test browser did not start')
                result = subprocess.run([NODE, '--input-type=module', '-e', READ_RESULT, 'http://127.0.0.1:' + port],
                                        capture_output=True, encoding='utf-8', timeout=30)
                self.assertEqual(result.stdout, 'PASS', result.stdout + result.stderr)
            finally:
                if browser.poll() is None:
                    browser.terminate()
                browser.wait(timeout=5)
                server.shutdown()
                server.server_close()
                thread.join(3)
