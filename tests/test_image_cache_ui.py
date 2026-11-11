"""Settings controls operate on a temporary cache, never the user's library."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest

from server import Handler, ROOT, make_server
from test_reader_access import BROWSER, NODE, READ_RESULT
from library import Store


CHECKS = r"""
const $=q=>document.querySelector(q),check=(v,m)=>{if(!v)throw Error(m)};
const wait=async test=>{for(let i=0;i<400;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timed out: '+test)};
try{
 await wait(()=>$('#book-grid').getAttribute('aria-busy')==='false');
 $('[data-nav="settings"]').click();await wait(()=>$('#image-cache-clear'));
 check($('#image-cache-total_bytes').textContent==='6 KiB','Total usage is wrong');
 check($('#image-cache-cache_bytes').textContent==='1 KiB','Transient cache was not separated');
 check($('#image-cache-protected_bytes').textContent==='5 KiB','Offline and legacy images are not protected');
 check($('#image-cache-legacy_bytes').textContent==='3 KiB','Legacy usage was not explained');
 $('#image-cache-limit').value='0';$('#image-cache-save').click();
 await wait(()=>!$('#image-cache-settings').inert);
 check((await fetch('/api/cache/images').then(r=>r.json())).max_bytes===0,'Limit was not saved');
 $('#image-cache-clear').click();await wait(()=>!$('#image-cache-settings').inert);
 const drained=await fetch('/qa/drain-cache').then(r=>r.json());check(drained.cache_bytes===0,'Drain: '+JSON.stringify(drained));$('#image-cache-refresh').click();
 await wait(()=>!$('#image-cache-settings').inert&&$('#image-cache-cache_bytes').textContent==='0 B');
 check($('#image-cache-total_bytes').textContent==='5 KiB'&&$('#image-cache-protected_bytes').textContent==='5 KiB','Clear deleted protected content');
 check($('#image-cache-settings [role=alert]').hidden,'Cache controls displayed an error');
 const result=document.createElement('pre');result.id='qa-result';result.textContent='PASS';document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack+'\nStats: '+JSON.stringify(await fetch('/api/cache/images').then(r=>r.json()))+'\nPanel: '+$('#image-cache-settings')?.textContent;document.body.append(result)}
"""


def fixture_server(directory):
    store = Store(directory)
    urls = [f'https://cdn.example/{i}.png' for i in range(3)]
    store.upsert_book({'id': 'cache-book', 'name': 'Cache fixture', 'source': 'full'},
                      [{'title': 'One', 'html': '<p>Cached body.</p>', 'images': urls}])
    server = make_server(directory, port=0)
    server.app._asset_stop.set()
    server.app._asset_worker.join(2)
    store = server.app.store
    while not store.asset_index_ready():
        store.index_assets()
    for i, url in enumerate(urls):
        path = server.app.image_dir / hashlib.sha256(url.encode()).hexdigest()
        path.write_bytes(b'\x89PNG\r\n\x1a\n' + bytes((i + 1) * 1024 - 8))
        store.touch_image(path, (i + 1) * 1024, legacy=i == 2)
        if i == 1:
            store.pin_image(path, 'cache-book', 0)
    while store.inventory_assets():
        pass
    class QaHandler(Handler):
        def _route(self):
            if self.path == '/' or self.path.startswith('/?'):
                markup = (ROOT / 'web/index.html').read_text(encoding='utf-8')
                if 'manual=1' not in self.path:
                    markup = markup.replace('</body>', '<script type="module" src="/qa.js"></script></body>')
                return self._reply(200, markup.encode('utf-8'), 'text/html; charset=utf-8')
            if self.path == '/qa.js':
                return self._reply(200, CHECKS.encode('utf-8'), 'text/javascript; charset=utf-8')
            if self.path == '/qa/drain-cache':
                for _ in range(10):
                    queued, cleaned = store.queue_cache_evictions(), store.cleanup_files()
                    if not queued and not cleaned:
                        break
                return self._reply(200, store.image_cache_stats() | {'ready': store.asset_index_ready(), 'queued': queued, 'cleaned': cleaned})
            return super()._route()
    server.RequestHandlerClass = QaHandler
    return server


@unittest.skipUnless(BROWSER and NODE, 'Installed browser and Node are required for local DOM checks')
class ImageCacheUiTests(unittest.TestCase):
    def test_settings_budget_usage_and_clear_preserve_offline_and_legacy_images(self):
        with tempfile.TemporaryDirectory(prefix='cache-ui-') as temporary:
            server = fixture_server(Path(temporary) / 'library')
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            browser = subprocess.Popen([BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
                '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync',
                '--user-data-dir=' + str(Path(temporary) / 'profile'), '--remote-debugging-port=0',
                '--remote-debugging-address=127.0.0.1', f'http://127.0.0.1:{server.server_port}/'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                port = None
                for _ in range(240):
                    try:
                        port = (Path(temporary) / 'profile/DevToolsActivePort').read_text().splitlines()[0]
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
                server.shutdown(); server.server_close(); thread.join(3)
