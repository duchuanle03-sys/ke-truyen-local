"""Unchanged frontend smoke using the repository's installed Chrome/Node CDP pattern."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

import m2_capture as h
sys.path.insert(0, str(h.m1.ROOT / "tests"))
from test_reader_access import BROWSER, NODE

CHECKS = r"""
(async () => {
const $ = q => document.querySelector(q);
const check = (ok, why) => { if (!ok) throw Error(why); };
const wait = async test => { for(let i=0;i<400;i++){ if(await test()) return; await new Promise(r=>setTimeout(r,25)); } throw Error('Timed out: '+test); };
const clickText = (scope, text) => {const b=[...document.querySelectorAll(scope+' button')].find(b=>b.textContent.trim()===text);check(b,'Missing button '+text);b.click();};
const ready = () => wait(()=>$('#reader')?.dataset.state==='reading');
const get = path => fetch(path).then(r=>{check(r.ok,'HTTP '+r.status+' '+path);return r.json()});
await wait(()=>document.querySelectorAll('.book-card').length===4);
check(document.title.includes('Truyện') || document.title.includes('truyện'), 'Wrong page title '+document.title);
$('.book-card[data-book-id="0"]').click();
await wait(()=>$('#dialog[open] .dialog-actions button.primary'));
$('#dialog .dialog-actions button.primary').click();await ready();
check($('.chapter-content').textContent.includes('Tiếng Việt'), 'Vietnamese text missing');
check(!$('.chapter-content script'), 'Unsafe script in chapter');
clickText('#reader','Chương sau ›'); await wait(()=>$('.reading-sheet h1')?.textContent==='Chương 1'); await ready();
await wait(async()=>(await get('/api/books/0')).chapter_index===1);
$('#reader button[aria-label="Ghi chú và dấu trang"]').click();
await wait(()=>$('#dialog[open] button'));
clickText('#dialog','Đánh dấu vị trí này');
await wait(()=>$('#dialog textarea[aria-label="Nội dung ghi chú"]'));
$('#dialog textarea').value='M2 browser ghi chú'; clickText('#dialog','Lưu');
await wait(async()=>(await get('/api/notes?book_id=0')).some(n=>n.note==='M2 browser ghi chú'));
$('#reader button[aria-label="Ghi chú và dấu trang"]').click();
await wait(()=>$('#dialog .note-item'));
window.confirm=()=>true;clickText('#dialog','Xóa');
await wait(async()=>(await get('/api/notes?book_id=0')).length===0);
$('#dialog-close').click();
$('#reader button[aria-label="Cách đọc và giao diện"]').click();
await wait(()=>$('#dialog select'));
const theme=$('#dialog select');theme.value='night';theme.dispatchEvent(new Event('change',{bubbles:true}));
await wait(async()=>(await get('/api/settings')).reader?.theme==='night');
$('#dialog-close').click();check($('#reader').dataset.theme==='night','Theme did not render');
$('#reader .reader-back').click();await wait(()=>$('#reader').hidden);
$('.book-card[data-book-id="1"]').click();await wait(()=>$('#dialog[open] .dialog-actions button.primary'));
$('#dialog .dialog-actions button.primary').click();await ready();
await wait(()=>$('#reader').dataset.kind==='comic' && $('.chapter-content img'));
const image=$('.chapter-content img');check(image,'Comic image missing');image.loading='eager';
await wait(()=>image.complete);check(image.naturalWidth===1,'Cached comic image did not decode');
check($('#reader').dataset.theme==='night','Saved appearance did not persist across books');
return {status:'PASS',url:location.href,title:document.title,viewport:[innerWidth,innerHeight],textBookProgress:(await get('/api/books/0')).chapter_index,comicNaturalWidth:image.naturalWidth};
})()
"""

CDP = r"""
import fs from 'node:fs';
const [endpoint,expression,output]=process.argv.slice(1);
const pages=await fetch(endpoint+'/json/list').then(r=>r.json());
const ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
await new Promise((resolve,reject)=>{ws.onopen=resolve;ws.onerror=reject});
let id=0;const replies=new Map(),errors=[],network=[];
ws.onmessage=e=>{const r=JSON.parse(e.data);if(r.id){replies.get(r.id)?.(r);replies.delete(r.id)}
if(r.method==='Runtime.exceptionThrown')errors.push(r.params.exceptionDetails);
if(r.method==='Log.entryAdded'&&r.params.entry.level==='error')errors.push(r.params.entry);
if(r.method==='Network.requestWillBeSent')network.push({event:'request',id:r.params.requestId,url:r.params.request.url,method:r.params.request.method});
if(r.method==='Network.responseReceived')network.push({event:'response',id:r.params.requestId,url:r.params.response.url,status:r.params.response.status});
if(r.method==='Network.loadingFinished')network.push({event:'finished',id:r.params.requestId,bytes:r.params.encodedDataLength});
if(r.method==='Network.loadingFailed')network.push({event:'failed',id:r.params.requestId,error:r.params.errorText,canceled:r.params.canceled});};
const call=(method,params={})=>new Promise(resolve=>{const next=++id;replies.set(next,resolve);ws.send(JSON.stringify({id:next,method,params}))});
await call('Runtime.enable');await call('Log.enable');await call('Network.enable');
for(let i=0;i<200;i++){
 const ready=await call('Runtime.evaluate',{expression:"location.protocol==='http:' && document.readyState==='complete' && !!document.querySelector('.book-card')",returnByValue:true});
 if(ready.result?.result?.value)break;
 await new Promise(r=>setTimeout(r,25));
}
await call('Runtime.evaluate',{expression:"window.__m2Clicks=[];document.addEventListener('click',e=>{const b=e.target.closest('button');if(b)window.__m2Clicks.push({text:b.textContent.trim(),id:b.id,class:b.className,dialog:document.querySelector('#dialog-title')?.textContent,state:document.querySelector('#reader')?.dataset.state});},true)"});
const result=await call('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});
const state=await call('Runtime.evaluate',{expression:"({clicks:window.__m2Clicks,reader:{...document.querySelector('#reader')?.dataset},text:document.querySelector('.chapter-content')?.textContent?.slice(0,500),dialog:document.querySelector('#dialog-title')?.textContent})",returnByValue:true});
const screenshot=await call('Page.captureScreenshot',{format:'png'});
fs.writeFileSync(output,Buffer.from(screenshot.result.data,'base64'));
process.stdout.write(JSON.stringify({result,errors,network,state,screenshot:output}));
await call('Browser.close');ws.close();
"""


def debugger_endpoint(profile):
    try:
        lines = (Path(profile) / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()
        if len(lines) < 2 or not lines[1].startswith("/devtools/browser/"):
            return None
        port = int(lines[0])
        return f"http://127.0.0.1:{port}" if 0 < port < 65536 else None
    except (OSError, ValueError):
        return None


def run(args):
    assert BROWSER and NODE, "Installed Chrome/Edge and Node required; no dependencies installed"
    output = h.m1.fresh_output(args.output)
    canonical = output / "fixture"
    h.fixture(canonical)
    before = h.m1.source_hashes()
    assert args.binary or args.python_oracle
    context = h.oracle(canonical) if args.python_oracle else h.m1.child(h.command(Path(args.binary).resolve(), canonical), output / "rust.log")
    with context as (port, owner):
        with tempfile.TemporaryDirectory(prefix="reader-m2-browser-") as profile:
            browser = subprocess.Popen([BROWSER, "--headless=new", "--disable-gpu", "--no-sandbox", "--no-first-run",
                "--no-default-browser-check", "--disable-background-networking", "--disable-component-update", "--disable-sync",
                "--window-size=1280,900", "--user-data-dir=" + profile, "--remote-debugging-port=0",
                "--remote-debugging-address=127.0.0.1", f"http://127.0.0.1:{port}/"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            try:
                endpoint = None
                for _ in range(200):
                    endpoint = debugger_endpoint(profile)
                    if endpoint:
                        break
                    time.sleep(.025)
                assert endpoint, "Headless browser debugger metadata did not become readable"
                result = subprocess.run([NODE, "--input-type=module", "-e", CDP, endpoint, CHECKS,
                    str(output / "comic.png")], capture_output=True, encoding="utf-8", errors="replace", timeout=45)
                assert result.returncode == 0, result.stderr
                evidence = json.loads(result.stdout)
                h.m1.write_json(output / "browser.json", evidence)
                assert evidence["result"]["result"]["result"].get("value", {}).get("status") == "PASS", evidence
                relevant_errors = [e for e in evidence["errors"] if not e.get("url", "").endswith("/favicon.ico")]
                assert not relevant_errors, relevant_errors
            finally:
                if browser.poll() is None:
                    try:
                        browser.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        browser.terminate()
                        browser.wait(timeout=5)
        if not args.python_oracle:
            h.shutdown(port, owner, canonical)
    assert h.m1.source_hashes() == before
    print(json.dumps({"report": str(output / "browser.json"), "screenshot": str(output / "comic.png"),
                      "browser": BROWSER, "fallback": "Browser plugin unavailable; authorized existing repository CDP pattern"}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary")
    parser.add_argument("--python-oracle", action="store_true")
    parser.add_argument("--output")
    run(parser.parse_args())
