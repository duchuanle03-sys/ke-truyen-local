"""Exercise the actual reader against a synthetic 10,000-chapter contents list."""
import json
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
const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
const open=()=>$('#reader button[aria-label="Mục lục"]').click();
const input=q=>{const e=$('#dialog input');e.focus();e.value=q;e.dispatchEvent(new Event('input',{bubbles:true}));};
const normalizer=String.prototype.toLocaleLowerCase;
const appErrors=[];window.addEventListener('error',e=>appErrors.push(e.error?.stack||e.message));
let normalizations=0,maxRows=0;
String.prototype.toLocaleLowerCase=function(...args){if(/^\d+ Chương/.test(String(this)))normalizations++;return normalizer.apply(this,args);};
const bounded=()=>{const count=document.querySelectorAll('.toc-item').length;maxRows=Math.max(maxRows,count);check(count<=40,'TOC rendered '+count+' rows instead of a viewport window');};
try{
 await wait(()=>$('#reader').dataset.state==='reading');
 const started=performance.now();open();await frame();bounded();
 const openMs=performance.now()-started;
 check($('.toc-item.active')?.dataset.chapterIndex==='5000','Current chapter was not rendered');
 const list=$('.toc-list');
 check(list.scrollTop>0,'Opening TOC did not center the current chapter');
 const long=$('.toc-item[data-chapter-index="5001"]');
 check(long.title.length>500 && long.getBoundingClientRect().height===64,'Long titles broke row geometry or lost their full text');
 const initialNodes=[...document.querySelectorAll('.toc-item')];
 list.scrollTop+=64;list.dispatchEvent(new Event('scroll'));await frame();bounded();
 check(initialNodes.filter(n=>n.isConnected).length>3,'Scrolling replaced overlapping rows instead of reusing them');
 list.scrollTop=list.scrollHeight;list.dispatchEvent(new Event('scroll'));await frame();bounded();
 check($('.toc-item[data-chapter-index="9999"]'),'Final chapter is unreachable by scrolling');
 check($('.toc-item[data-chapter-index="9998"] .toc-vip'),'Verified VIP label was lost');
 check($('.toc-item[data-chapter-index="9999"] .cached'),'Cached chapter label was lost');
 const before=normalizations, searchStarted=performance.now();let searchDispatchMs=0;
 for(const query of ['N','NE','NEEDLE','NEEDLE ALPHA']){const started=performance.now();input(query);searchDispatchMs+=performance.now()-started;await frame();bounded();}
 const searchMs=performance.now()-searchStarted;
 check(normalizations===before,'Typing re-normalized chapter titles');
 check($('.toc-item')?.dataset.chapterIndex==='4321','Filtered result lost its original chapter index');
 check(document.querySelectorAll('.toc-item').length===1,'Title search did not filter the complete TOC');
 $('.toc-item').click();await wait(()=>$('#reader').dataset.state==='reading' && $('.reading-sheet h1').textContent.includes('Needle Alpha'));
 open();input('No such chapter');await frame();
 check(!document.querySelector('.toc-item') && $('#dialog').textContent.includes('Không có chương phù hợp'),'Empty result was not handled');
 input('');await frame();bounded();
 let row=$('.toc-item');row.focus();
 row.dispatchEvent(new KeyboardEvent('keydown',{key:'End',bubbles:true}));await frame();bounded();
 check(document.activeElement.dataset.chapterIndex==='9999','End did not focus the final chapter');
 check(document.activeElement.closest('[role="listitem"]').getAttribute('aria-setsize')==='10000','Screen reader list size was lost');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Home',bubbles:true}));await frame();
 check(document.activeElement.dataset.chapterIndex==='0','Home did not focus the first chapter');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowDown',bubbles:true}));await frame();
 check(document.activeElement.dataset.chapterIndex==='1','Arrow navigation did not move between virtual rows');
 $('#dialog-close').click();await fetch('/qa/rename');open();
 input('NEEDLE ALPHA');await frame();$('.toc-item').click();
 await wait(()=>$('#reader').dataset.state==='reading' && $('.reading-sheet h1').textContent.includes('Needle Beta'));
 open();input('NEEDLE BETA');await frame();bounded();
 check($('.toc-item')?.dataset.chapterIndex==='4321','Changed chapter title was not reindexed');
 $('#dialog-close').click();open();input('5001');await frame();
 $('.toc-item[data-chapter-index="5000"]').click();await wait(()=>$('#reader').dataset.state==='reading' && $('.reading-sheet h1').textContent==='Chương 5001');
 const priorAudio={fetch:window.fetch,Utterance:window.SpeechSynthesisUtterance,getVoices:speechSynthesis.getVoices,speak:speechSynthesis.speak,cancel:speechSynthesis.cancel};
 try{
  window.fetch=(url,...args)=>String(url).endsWith('/chapters/5001')
   ? Promise.resolve(new Response(JSON.stringify({title:'CHANGED TITLE',html:'<p>Next chapter.</p>',text:'Next chapter.',images:[],downloaded:true}),{headers:{'Content-Type':'application/json'}}))
   : priorAudio.fetch(url,...args);
  window.SpeechSynthesisUtterance=class{constructor(text){this.text=text;}};
  speechSynthesis.getVoices=()=>[{voiceURI:'qa',lang:'en-US',name:'QA'}];
  speechSynthesis.speak=utterance=>{window.qaSpeech=utterance;};speechSynthesis.cancel=()=>{};
  $('#reader button[aria-label="Nghe truyện"]').click();$('#dialog-actions button.primary').click();await frame();
  check(window.qaSpeech,'Synthetic speech did not start');open();await frame();
  const existing=$('.toc-item[data-chapter-index="5001"]');
  await window.qaSpeech.onend();await wait(()=>$('#reader').dataset.state==='reading' && $('.reading-sheet h1').textContent==='CHANGED TITLE');
  check(existing.title==='CHANGED TITLE' && existing.querySelector('.cached') && existing.classList.contains('active'),'Open TOC did not refresh after background navigation');
  input('CHANGED TITLE');await frame();bounded();
  check($('.toc-item')===existing,'Updating background metadata replaced a reusable row');
  check(existing.title==='CHANGED TITLE' && existing.textContent.includes('CHANGED TITLE'),'Background title stayed stale');
  check(existing.querySelector('.cached'),'Background cached badge stayed stale');
  check(existing.classList.contains('active') && existing.getAttribute('aria-current')==='page','Background current marker stayed stale');
 }finally{window.fetch=priorAudio.fetch;window.SpeechSynthesisUtterance=priorAudio.Utterance;speechSynthesis.getVoices=priorAudio.getVoices;speechSynthesis.speak=priorAudio.speak;speechSynthesis.cancel=priorAudio.cancel;}
 $('#dialog-close').click();$('#reader .reader-back').click();await wait(()=>$('#reader').hidden);
 check(appErrors.length===0,'TOC lifecycle generated browser errors: '+JSON.stringify(appErrors));
 const result=document.createElement('pre');result.id='qa-result';
 result.textContent='PASS '+JSON.stringify({viewport:[innerWidth,innerHeight],chapters:10000,cached_bodies:3,open_ms:openMs,search_dispatch_ms:searchDispatchMs,search_four_inputs_ms:searchMs,max_rows:maxRows,title_normalizations_on_search:0});document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack+'\nApp errors: '+JSON.stringify(appErrors)+'\nDialog: '+$('#dialog')?.textContent.slice(0,400);document.body.append(result)}
finally{String.prototype.toLocaleLowerCase=normalizer;}
"""


def fixture_server(directory):
    store = Store(directory)
    store.upsert_book({'id': 'toc-ui', 'name': 'Synthetic large TOC', 'source': 'file', 'chapter_count': 10000})
    def chapters():
        for i in range(10000):
            title = f'Chương {i + 1}'
            if i == 4321:
                title += ': Needle Alpha'
            if i == 5001:
                title += ': ' + 'Một tên chương rất dài ' * 40
            cached = i in (4321, 5000, 9999)
            yield ('toc-ui', i, title, '<p>Cached text.</p>' if cached else '', 'Cached text.' if cached else '',
                   int(cached), '{"is_vip":true}' if i == 9998 else '{}')
    with store.db() as db:
        db.executemany('INSERT INTO chapters(book_id,idx,title,html,text,downloaded,raw) VALUES(?,?,?,?,?,?,?)', chapters())
    store.progress('toc-ui', 5000, .4)
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
            if self.path == '/qa/rename':
                server.app.store.edit_content('toc-ui', 4321, {'title': 'Chương 4322: Needle Beta', 'text': 'Updated text.', 'html': '<p>Updated text.</p>'})
                return self._reply(200, {'updated': True})
            return super()._route()
    server.RequestHandlerClass = QaHandler
    return server


@unittest.skipUnless(BROWSER and NODE, 'Installed browser and Node are required for local DOM checks')
class TocUiTests(unittest.TestCase):
    def test_large_toc_keeps_dom_bounded_and_preserves_search_navigation(self):
        for width, height in ((1280, 850), (390, 700)):
            with self.subTest(viewport=(width, height)), tempfile.TemporaryDirectory(prefix='toc-ui-') as temporary:
                server = fixture_server(Path(temporary) / 'library')
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                browser = subprocess.Popen([BROWSER, '--headless=new', '--disable-gpu', '--no-sandbox', '--no-first-run',
                    '--no-default-browser-check', '--disable-background-networking', '--disable-component-update', '--disable-sync',
                    '--user-data-dir=' + str(Path(temporary) / 'profile'), '--remote-debugging-port=0',
                    '--remote-debugging-address=127.0.0.1', f'--window-size={width},{height}',
                    f'http://127.0.0.1:{server.server_port}/?book=toc-ui'],
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
                    self.assertTrue(result.stdout.startswith('PASS '), result.stdout + result.stderr)
                    print(json.dumps({'viewport': [width, height], **json.loads(result.stdout[5:])}))
                finally:
                    if browser.poll() is None:
                        browser.terminate()
                    browser.wait(timeout=5)
                    server.shutdown()
                    server.server_close()
                    thread.join(3)
