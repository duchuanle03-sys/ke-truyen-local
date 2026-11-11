"""Exercise server-side shelf and trash pages through the real app."""
import json
import struct
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
import zlib

from library import Store
from server import Handler, ROOT, make_server
from test_reader_access import BROWSER, NODE, READ_RESULT


CHECKS = r"""
const $=q=>document.querySelector(q),cards=()=>[...document.querySelectorAll('#book-grid .book-card')];
const check=(ok,message)=>{if(!ok)throw Error(message)};
const wait=async test=>{for(let i=0;i<400;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timed out: '+test)};
const ready=()=>$('#book-grid').getAttribute('aria-busy')==='false';
const first=()=>cards()[0]?.querySelector('h3').textContent;
const change=(id,value)=>{const e=$('#'+id);e.value=value;e.dispatchEvent(new Event('change',{bubbles:true}));};
const search=value=>{const e=$('#library-search');e.value=value;e.dispatchEvent(new Event('input',{bubbles:true}));};
const errors=[];window.addEventListener('error',event=>errors.push(event.error?.stack||event.message));
try{
 await wait(()=>ready()&&cards().length===60);
 check($('#nav-count').textContent==='123','Navigation count came from only the first page');
 check(first()==='Book 0122','Initial sort was wrong');
 const initial=cards(),healthy=initial[0].querySelector('img');
 for(const item of initial.slice(0,2))item.querySelector('img').loading='eager';
 await wait(()=>healthy.complete&&healthy.naturalWidth>0&&healthy.parentElement.classList.contains('has-cover')&&!initial[1].querySelector('img'));
 let mutations=[];const observer=new MutationObserver(records=>mutations.push(...records));observer.observe($('#book-grid'),{subtree:true,childList:true,attributes:true,characterData:true});
 const takeMutations=()=>{const result=mutations.concat(observer.takeRecords());mutations=[];return result;};
 const unchanged=message=>{check(cards().every((node,i)=>node===initial[i]),message);check(initial[0].querySelector('img')===healthy,'An unchanged cover image was replaced');};
 for(const view of ['compact','list','grid']){$('[data-view="'+view+'"]').click();unchanged('Layout change recreated shelf cards');}
 takeMutations();$('#refresh-library').click();await wait(ready);unchanged('Refresh recreated unchanged cards');
 const refreshChanges=takeMutations();
 check(refreshChanges.filter(m=>m.target.closest?.('.book-card')&&m.target.closest('.book-card')!==initial[1]).length===0,'Refresh rewrote healthy unchanged cards');
 check(refreshChanges.some(m=>m.target.closest?.('.book-card')===initial[1]&&[...m.addedNodes].some(n=>n.matches?.('.book-cover'))),'Explicit refresh did not retry a failed cover');
 await wait(()=>!initial[1].querySelector('img'));takeMutations();
 await fetch('/qa/book-change?meta');$('[data-nav="library"]').click();await wait(()=>ready()&&first()==='Changed title');
 unchanged('Metadata change replaced a card');
 check(initial[0].querySelector('.book-author').textContent==='Changed author'&&initial[0].querySelector('progress').value===42,'Changed metadata was not patched');
 check(initial[0].querySelector('.book-meta').textContent.includes('444'),'Chapter count was stale');
 check(takeMutations().filter(m=>m.target.closest?.('.book-card')&&m.target.closest('.book-card')!==initial[0]).length===0,'One-book edit rewrote unrelated cards');
 initial[0].click();await wait(()=>$('#dialog .book-description'));
 check($('#dialog h2').textContent==='Changed title'&&$('#dialog-actions').textContent.includes('42%'),'Reused card opened stale book data');$('#dialog-close').click();
 await fetch('/qa/book-change?cover');$('#refresh-library').click();await wait(()=>ready()&&initial[0].querySelector('img')!==healthy);
 check(cards()[0]===initial[0]&&initial[0].querySelector('img'),'Changing a cover replaced the whole card or lost the image');
 const replacement=initial[0].querySelector('img');$('#refresh-library').click();await wait(ready);
 check(initial[0].querySelector('img')===replacement,'An unchanged replacement cover was recreated');
 check(takeMutations().filter(m=>[...m.addedNodes].some(n=>n.matches?.('.book-card'))).length===0,'Updates created new card elements');
 observer.disconnect();await fetch('/qa/book-change?restore');$('#refresh-library').click();await wait(()=>ready()&&first()==='Book 0122');
 $('#library-next').click();await wait(()=>ready()&&first()==='Book 0062');
 check(cards().length===60,'Page navigation accumulated cards');
 check(document.activeElement===cards()[0]&&cards()[0].getBoundingClientRect().top<innerHeight,'New page did not bring its first book into view and focus it');
 $('#library-next').click();await wait(()=>ready()&&cards().length===3);
 check(first()==='Book 0002'&&$('#library-next').disabled,'Last page was unreachable');
 $('#sort-direction').click();await wait(()=>ready()&&first()==='Book 0000');
 check(cards().length===60,'Changing direction did not reset the page');
 change('library-source','hako');await wait(()=>ready()&&first()==='Book 0001');
 check($('#library-count').textContent.includes('61'),'Source count was restricted to a loaded page');
 $('[data-kind="comic"]').click();await wait(()=>ready()&&first()==='Book 0003');
 check(cards().length===20,'Combined kind/source filtering was applied after pagination');
 $('[data-kind="all"]').click();change('library-source','');change('library-group','favorites');
 await wait(()=>ready()&&cards().length===25);check(first()==='Book 0000','Group filter failed');
 const groupNodes=cards(),focused=groupNodes[10],grid=$('#book-grid'),nativeMove=grid.moveBefore;
 for(const moving of [nativeMove,undefined]){
  grid.moveBefore=moving;focused.focus();$('#sort-direction').click();await wait(()=>ready()&&first()==='Book 0120');
  check(groupNodes.every(n=>n.isConnected)&&document.activeElement===focused,'Reordering lost cards or keyboard focus');
  $('#sort-direction').click();await wait(()=>ready()&&first()==='Book 0000');
 }
 grid.moveBefore=nativeMove;
 change('library-group','');change('library-sort','name');
 $('#sort-direction').click();await wait(()=>ready()&&first()==='Book 0000');
 search('Book 0122');await wait(()=>ready()&&cards().length===1&&first()==='Book 0122');
 search('absent');await wait(()=>ready()&&cards().length===0&&$('#library-count').textContent.includes('0'));
 search('Slow');await new Promise(r=>setTimeout(r,280));search('Book 0007');
 await wait(()=>ready()&&first()==='Book 0007');await new Promise(r=>setTimeout(r,500));
 check(first()==='Book 0007','An obsolete filter response replaced the newer page');
 search('');await wait(()=>ready()&&cards().length===60);
 $('#open-trash').click();await wait(()=>$('#dialog .note-item')&&$('#trash-next'));
 check(document.querySelectorAll('#dialog .note-item').length===60,'Trash was unbounded');
 $('#trash-next').click();await wait(()=>$('#dialog [role="status"]')?.textContent.includes('61–120'));
 $('#trash-next').click();await wait(()=>document.querySelectorAll('#dialog .note-item').length===1);
 const restore=[...document.querySelectorAll('#dialog .note-item button')].find(b=>b.textContent==='Khôi phục về kệ');restore.click();
 await wait(()=>$('#dialog [role="status"]')?.textContent.includes('61–120')&&document.querySelectorAll('#dialog .note-item').length===60);
 check($('#nav-count').textContent==='124','Restore did not refresh the complete shelf count');
 $('#dialog-close').click();
 check(!$('#library-error').textContent,'Shelf showed an API error');
 check(errors.length===0,'Browser errors: '+JSON.stringify(errors));
 const result=document.createElement('pre');result.id='qa-result';result.textContent='PASS';document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack+'\n'+JSON.stringify(errors)+'\n'+$('#dialog')?.textContent.slice(-300);document.body.append(result)}
"""


def fixture_server(directory):
    store = Store(directory)
    with store.db() as db:
        db.executemany('INSERT INTO books(id,name,source,kind,groups_json,added_at,updated_at,read_at,trashed_at) '
            'VALUES(?,?,?,?,?,?,?,?,?)',
            ((f'book-{i}', f'Book {i:04}' if i < 123 else f'Trash {i:04}',
              'file' if i % 2 == 0 else 'hako', 'comic' if i % 3 == 0 else 'text',
              '["favorites"]' if i % 5 == 0 else '[]', i, i, i, 0 if i < 123 else time.time())
             for i in range(244)))
        db.execute("INSERT INTO groups_list VALUES('favorites','Favorites')")
        db.execute("UPDATE books SET cover='https://cdn.example/original.png' WHERE id IN ('book-121','book-122')")
    server = make_server(directory, port=0)
    server.app._asset_stop.set()
    server.app._asset_worker.join(2)
    def chunk(kind, body):
        return struct.pack('!I', len(body)) + kind + body + struct.pack('!I', zlib.crc32(kind + body))
    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('!2I5B', 1, 1, 8, 6, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(b'\0\xff\0\0\xff')) + chunk(b'IEND', b'')
    class QaHandler(Handler):
        def _route(self):
            if self.path == '/' or self.path.startswith('/?'):
                markup = (ROOT / 'web/index.html').read_text(encoding='utf-8')
                if 'manual=1' not in self.path:
                    markup = markup.replace('</body>', '<script type="module" src="/qa.js"></script></body>')
                return self._reply(200, markup.encode('utf-8'), 'text/html; charset=utf-8')
            if self.path == '/qa.js':
                return self._reply(200, CHECKS.encode('utf-8'), 'text/javascript; charset=utf-8')
            if self.path.startswith('/api/books/book-122/cover'):
                return self._reply(200, png, 'image/png')
            if self.path.startswith('/api/books/book-121/cover'):
                return self._reply(404, {'error': 'Synthetic unavailable cover'})
            if self.path.startswith('/qa/book-change?'):
                with server.app.store.db() as db:
                    if self.path.endswith('?meta'):
                        db.execute("UPDATE books SET name='Changed title',author='Changed author',progress=.42,chapter_count=444 WHERE id='book-122'")
                    elif self.path.endswith('?cover'):
                        db.execute("UPDATE books SET cover='https://cdn.example/replacement.png' WHERE id='book-122'")
                    else:
                        db.execute("UPDATE books SET name='Book 0122',author='',progress=0,chapter_count=0,cover='https://cdn.example/original.png' WHERE id='book-122'")
                return self._reply(200, {'updated': True})
            if '/api/books?' in self.path and 'q=Slow' in self.path:
                time.sleep(.6)
            return super()._route()
    server.RequestHandlerClass = QaHandler
    return server


@unittest.skipUnless(BROWSER and NODE, 'Installed browser and Node are required for local DOM checks')
class BookshelfUiTests(unittest.TestCase):
    def test_shelf_filters_and_trash_pages_do_not_accumulate_cards(self):
        for width in (1280, 504):
            with self.subTest(width=width), tempfile.TemporaryDirectory(prefix='shelf-ui-') as temporary:
                server = fixture_server(Path(temporary) / 'library')
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                browser = subprocess.Popen([BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
                    '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync',
                    '--user-data-dir=' + str(Path(temporary) / 'profile'), '--remote-debugging-port=0',
                    '--remote-debugging-address=127.0.0.1', f'--window-size={width},850',
                    f'http://127.0.0.1:{server.server_port}/'],
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
                                            capture_output=True, encoding='utf-8', timeout=35)
                    self.assertEqual(result.stdout, 'PASS', result.stdout + result.stderr)
                finally:
                    if browser.poll() is None:
                        browser.terminate()
                    browser.wait(timeout=5)
                    server.shutdown()
                    server.server_close()
                    thread.join(3)
