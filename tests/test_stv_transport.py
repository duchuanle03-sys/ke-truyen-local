"""Native Node transport contracts, with synthetic HTTP responses and no external network."""
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from source_errors import SourceError
from stv_http import StvHttp


@unittest.skipUnless(shutil.which('node'), 'Node is required for the STV HTTP transport')
class StvTransportTests(unittest.TestCase):
    def test_cookie_order_headers_and_fresh_manual_verification_transport(self):
        script = r'''
import assert from 'node:assert/strict';
import {Session} from './stv_http_worker.mjs';
const calls=[], fresh=[];
const replies=[
 new Response('<script>document.cookie="_ac=literal; path=/";document.cookie="lang=zh; path=/";</script>', {headers:{'Set-Cookie':'_ac=server; Path=/'}}),
 new Response('{"code":7}',{headers:{'Set-Cookie':'_ac=rotated; Path=/'}}),
 new Response('success'),
 new Response(new Uint8Array([137,80,78,71,13,10,26,10]),{headers:{'Content-Type':'image/png','Set-Cookie':'PHPSESSID=new; Path=/'}})
];
const session=new Session(true,async(url,options)=>{calls.push({url:String(url),options});return replies.shift()},async(url,options)=>{fresh.push({url:String(url),options});return replies.shift()});
const referer='https://sangtacviet.com/truyen/qidian/1/123/901/';
await session.request(referer,{navigation:true});
assert.match(calls[0].options.headers.Cookie,/lang=vi/);
assert.match(calls[0].options.headers.Cookie,/transmode=name/);
assert.equal(session.jar.get('_ac;/').value,'server');
assert.equal(session.jar.get('lang;/').value,'vi');
await session.request('/index.php?bookid=123&h=qidian&c=901&ngmar=readc&sajax=readchapter&sty=1&exts=',{method:'POST',seed:true,xhr:true,referer});
assert.equal(calls[1].options.body,'server');
assert.equal(calls[1].options.headers['X-Requested-With'],'XmlHttpRequest');
assert.equal(calls[1].options.headers.Referer,referer);
assert.equal(calls[1].options.headers.Origin,'https://sangtacviet.com');
assert.equal(calls[1].options.redirect,'manual');
assert.equal(session.jar.get('_ac;/').value,'rotated');
await session.request('/index.php?ngmar=verifyca',{method:'POST',body:'ajax=verifycaptcha&token=synthetic&purpose=read&provider=sangtacviet',fresh_connection:true,referer});
assert.equal(fresh.length,1);assert.match(fresh[0].options.headers.Cookie,/_ac=rotated/);
assert.equal(fresh[0].options.headers.Connection,'close');
assert.equal(Number(fresh[0].options.headers['Content-Length']),Buffer.byteLength(fresh[0].options.body));
const image=await session.request('/generate_captcha.php?random=0.5',{image:true,referer});
assert.equal(image.content_type,'image/png');assert.equal(image.body_base64,'iVBORw0KGgo=');
assert.deepEqual(Object.keys(calls[2].options.headers).sort(),['Cookie','Referer','User-Agent']);
assert.match(session.cookieHeader('/index.php'),/PHPSESSID=new/);
session.put('_ac=deleted; Path=/; Max-Age=0');assert(!session.jar.has('_ac;/'));
session.put('wrong=ignored; Domain=evil.test; Path=/');assert(!session.jar.has('wrong;/'));
session.put('scoped=yes; Path=/index');assert(!session.cookieHeader('/index.php').includes('scoped='));
await assert.rejects(()=>session.request('https://evil.test/index.php'),{code:'source_error'});
await assert.rejects(()=>session.request('/redirect'),{code:'source_error'});
'''
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, encoding='utf-8', timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)

    def test_real_worker_process_is_reused_and_does_not_start_a_browser(self):
        transport = StvHttp()
        self.addCleanup(transport.close)
        for _ in range(2):
            with self.assertRaises(SourceError) as caught:
                transport.request('/not-a-source-route')
            self.assertEqual(caught.exception.code, 'source_error')
        self.assertEqual(transport.generation, 1)

    def test_missing_node_and_lost_verification_session_fail_without_sending_answer(self):
        transport = StvHttp()
        with patch('stv_http.shutil.which', return_value=None), self.assertRaises(SourceError) as caught:
            transport.request('/index.php')
        self.assertEqual(caught.exception.code, 'transport_unavailable')
        with self.assertRaises(SourceError) as caught:
            transport.request('/index.php?ngmar=verifyca', require_session=True, method='POST', body='not-sent')
        self.assertEqual(caught.exception.code, 'captcha_expired')
        self.assertIsNone(transport.process)

    def test_invalid_utf8_and_http_failures_remain_parse_and_transport_errors(self):
        script = r'''
import assert from 'node:assert/strict';
import {Session} from './stv_http_worker.mjs';
const broken=new Session(false,async()=>new Response(new Uint8Array([255])));
await assert.rejects(()=>broken.request('/index.php'),{code:'parse_error'});
for(const status of [302,403,404]){
 const source=new Session(false,async()=>new Response('failure',{status}));
 await assert.rejects(()=>source.request('/index.php'),{code:'http_error',http_status:status});
}
'''
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
