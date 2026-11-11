"""Rendered Settings/reader regression on a disposable library and browser profile."""
import unittest
from unittest.mock import patch

import test_image_cache_ui as image_qa


CHECKS = r"""
const $=q=>document.querySelector(q),check=(v,m)=>{if(!v)throw Error(m)};
const wait=async test=>{for(let i=0;i<400;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timed out: '+test)};
const stats=()=>fetch('/api/cache/chapters').then(r=>r.json());
try{
 await wait(()=>$('#book-grid').getAttribute('aria-busy')==='false');
 $('[data-nav="settings"]').click();await wait(()=>$('#chapter-cache-clear'));
 check($('#chapter-cache-cache').textContent.startsWith('1 chương'),'Regenerable count');
 check($('#chapter-cache-protected').textContent.startsWith('2 chương'),'Protected count');
 check($('#chapter-cache-legacy').textContent.startsWith('2 chương'),'Legacy excluded from quota');
 [...$('#chapter-cache-settings').querySelectorAll('button')].find(b=>b.textContent==='30 chương').click();
 $('#chapter-cache-save').click();await wait(()=>!$('#chapter-cache-settings').inert);
 check((await stats()).max_chapters===30,'Lower preset not saved');
 window.confirm=()=>false;$('#chapter-cache-legacy-action').click();await new Promise(r=>setTimeout(r,50));
 check(!(await stats()).legacy_until,'Cancelled warning still reclassified legacy');
 window.confirm=()=>true;$('#chapter-cache-clear').click();await wait(()=>!$('#chapter-cache-settings').inert);
 await fetch('/qa/drain-chapters');$('#chapter-cache-refresh').click();await wait(()=>!$('#chapter-cache-settings').inert);
 check((await stats()).cache_count===0,'Clear did not remove regenerable content');
 check((await stats()).protected_count===2&&(await stats()).legacy_count===2,'Clear lost protected content');
 $('#chapter-cache-legacy-action').click();await wait(()=>!$('#chapter-cache-settings').inert);
 await fetch('/qa/drain-chapters');check((await stats()).legacy_count===0,'Opt-in legacy workflow failed');
 check($('#chapter-cache-compact')&&$('#chapter-cache-settings [role=alert]').hidden,'Missing compact/error state');
 await fetch('/qa/seed-chapter');
 $('[data-nav="library"]').click();await wait(()=>$('.book-card[data-book-id="chapter-fixture"]'));
 $('.book-card[data-book-id="chapter-fixture"]').click();await wait(()=>document.querySelector('dialog[open] .primary'));
 document.querySelector('dialog[open] button.primary').click();
 await wait(()=>$('#reader').dataset.state==='reading');
 await fetch('/api/cache/chapters',{method:'POST',headers:{'Content-Type':'application/json'},body:'{"max_chapters":0}'});
 await fetch('/qa/drain-chapters');
 check((await fetch('/qa/lease').then(r=>r.json())).active===1,'Reader did not register lease');
 window.dispatchEvent(new PageTransitionEvent('pagehide',{persisted:true}));
 window.dispatchEvent(new PageTransitionEvent('pageshow',{persisted:true}));
 await new Promise(r=>setTimeout(r,100));await fetch('/qa/drain-chapters');
 check((await fetch('/qa/lease').then(r=>r.json())).active===1,'History restoration lost lease');
 check($('#reader').textContent.includes('Cached needle'),'Rendered chapter disappeared');
 const result=document.createElement('pre');result.id='qa-result';result.textContent='PASS';document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack+'\nStats: '+JSON.stringify(await stats());document.body.append(result)}
"""


def fixture_server(directory):
    server = ORIGINAL_FIXTURE(directory)
    store = server.app.store
    store.upsert_book({'id': 'chapter-fixture', 'name': 'Chapter fixture', 'source': 'full'},
        [{'title': str(i), 'url': f'https://example.com/{i}'} for i in range(4)])
    for index in (0, 1, 2):
        store.save_content('chapter-fixture', index, {'html': '<p>Cached needle</p>', 'text': 'Cached needle'}, offline=index == 1)
    store.edit_content('chapter-fixture', 2, {'html': '<p>Edit</p>', 'text': 'Edit'})
    with store.db() as db:
        db.execute("UPDATE chapters SET html='<p>Legacy</p>',text='Legacy',downloaded=1 WHERE book_id='chapter-fixture' AND idx=3")
    class QaHandler(server.RequestHandlerClass):
        def _route(self):
            if self.path == '/qa/drain-chapters':
                for _ in range(100):
                    if not store.maintain_chapter_cache(): break
                return self._reply(200, store.chapter_cache_stats())
            if self.path == '/qa/lease':
                return self._reply(200, {'active': len(store._reader_keys())})
            if self.path == '/qa/seed-chapter':
                store.save_content('chapter-fixture', 0, {'html': '<p>Cached needle</p>', 'text': 'Cached needle'})
                return self._reply(200, {'ready': True})
            return super()._route()
    server.RequestHandlerClass = QaHandler
    return server


ORIGINAL_FIXTURE = image_qa.fixture_server


@unittest.skipUnless(image_qa.BROWSER and image_qa.NODE, 'Installed browser and Node required')
class ChapterCacheUiTests(unittest.TestCase):
    def test_settings_warning_cleanup_and_live_reader_history_lease(self):
        with patch.object(image_qa, 'fixture_server', fixture_server), patch.object(image_qa, 'CHECKS', CHECKS):
            image_qa.ImageCacheUiTests.test_settings_budget_usage_and_clear_preserve_offline_and_legacy_images(self)
