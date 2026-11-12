"""Real local UI cookie/token flow, with disposable data and no upstream requests."""
import unittest
from unittest.mock import patch

import test_image_cache_ui as image_qa


CHECKS = r"""
const $=q=>document.querySelector(q),check=(v,m)=>{if(!v)throw Error(m)};
const wait=async test=>{for(let i=0;i<400;i++){if(test())return;await new Promise(r=>setTimeout(r,20));}throw Error('Timed out: '+test)};
try{
 await wait(()=>$('#book-grid').getAttribute('aria-busy')==='false');
 $('[data-nav="settings"]').click();await wait(()=>$('#server-shutdown'));
 check(!document.cookie.includes('ke_truyen_session'),'Shutdown cookie is readable by scripts');
 let count=0;window.confirm=()=>{count++;return false};
 $('#server-shutdown').click();await wait(()=>!$('#server-shutdown').disabled);
 check(count===1,'Active download confirmation was not shown');
 check((await fetch('/api/instance')).status===200,'Cancel closed server');
 window.confirm=()=>true;$('#server-shutdown').click();
 await wait(()=>$('#server-settings').textContent.includes('Đã nhận lệnh tắt'));
 check($('#server-shutdown').disabled,'Shutdown can be submitted repeatedly');
 const result=document.createElement('pre');result.id='qa-result';result.textContent='PASS';document.body.append(result);
}catch(error){const result=document.createElement('pre');result.id='qa-result';result.textContent=error.stack;document.body.append(result)}
"""


ORIGINAL_FIXTURE = image_qa.fixture_server


def fixture_server(directory):
    server = ORIGINAL_FIXTURE(directory)
    app = server.app
    app._download_stop.set(); app._download_wake.set(); app._download_worker.join(2)
    app.store.create_download({'id': 'queued', 'book_id': 'cache-book', 'name': 'Synthetic download',
        'status': 'queued', 'cancelled': False, 'start': 0, 'end': 1, 'total': 1, 'completed': 0})
    return server


@unittest.skipUnless(image_qa.BROWSER and image_qa.NODE, 'Installed browser and Node required')
class ShutdownUiTests(unittest.TestCase):
    def test_cookie_token_active_job_confirmation_cancel_and_shutdown_acceptance(self):
        with patch.object(image_qa, 'fixture_server', fixture_server), patch.object(image_qa, 'CHECKS', CHECKS):
            image_qa.ImageCacheUiTests.test_settings_budget_usage_and_clear_preserve_offline_and_legacy_images(self)
