"""Synthetic STV contracts; fixtures contain no copied chapter text or credentials."""
import io
import json
import shutil
import subprocess
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlsplit

from sources import SourceError, SourceAccessError, SourceService, _PublicFetcher
from browser_source import BrowserError
from stv_source import _chapter_expression


BOOK = 'https://sangtacviet.vip/truyen/fanqie/1/123/'
CHAPTER = BOOK + '901/'
METADATA = '''<h1 id="book_name2">Truyện thử tiếng Việt</h1>
<meta property="og:novel:author" content="Tác giả thử">
<meta property="og:description" content="Mô tả thử nghiệm.">
<meta property="og:image" content="https://images.example/cover.jpg">'''


def toc(data):
    return {'code': 1, 'data': data, 'oridata': data, 'val': 42}


class BrowserFixture:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def read(self, url, expression, timeout=60):
        self.calls.append((url, expression))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return dict(url=url, **result)

    def open(self, url):
        self.calls.append((url, 'open'))
        return {'opened': True, 'url': url}


class StvSourceTests(unittest.TestCase):
    def test_source_is_listed_without_starting_browser(self):
        service = SourceService(browser=BrowserFixture())
        self.assertIn({'id': 'stv', 'name': 'Sáng Tác Việt'}, service.sources())

    def test_search_uses_real_row_links_and_explicit_pagination(self):
        row = '''<a class="booksearch" href="/truyen/fanqie/1/123/">
        <img src="https://images.example/cover.jpg"><div><b class="searchbooktitle">Tên tiếng Việt</b>
        <span class="searchbookauthor">Tác giả</span><span class="searchtag">Fanqie · Đang ra</span></div></a>'''
        browser = BrowserFixture({'html': row + row, 'pagination_html': '<a class="page-link" onclick="gopagen()">&gt;</a>'})
        result = SourceService(browser=browser).search('stv', 'Tên & thử', 2)
        self.assertEqual(result['items'], [{'name': 'Tên tiếng Việt', 'author': 'Tác giả',
            'path': BOOK, 'cover': 'https://images.example/cover.jpg', 'description': 'Fanqie · Đang ra'}])
        self.assertEqual(result['next'], 3)
        query = parse_qs(urlsplit(browser.calls[0][0]).query)
        self.assertEqual(query['findinname'], ['Tên & thử'])
        self.assertEqual(query['p'], ['2'])
        for pagination in ('', '<li class="disabled"><a class="page-link" onclick="gopagen()">&gt;</a></li>',
                           '<a aria-disabled="true" class="page-link" onclick="gopagen()">&gt;</a>'):
            with self.subTest(pagination=pagination):
                result = SourceService(browser=BrowserFixture({'html': row, 'pagination_html': pagination})).search('stv', 'Tên')
                self.assertIsNone(result['next'])

    def test_search_never_reports_a_pending_or_unrecognized_page_as_empty(self):
        with self.assertRaises(SourceError) as caught:
            SourceService(browser=BrowserFixture({'html': '<div id="search-containter"><div id="endless"></div></div>'})).search('stv', 'thử')
        self.assertEqual(caught.exception.code, 'parse_error')

    def test_search_reports_empty_only_from_the_sources_explicit_message(self):
        markup = '<div id="search-containter"><div class="row"><center class="w-100 t-20"><br>Không tìm thấy truyện? Kiểm tra chính tả hoặc vào <a href="/#cboxdiv">Chat Box</a> nhờ tìm nào.</center></div><div id="endless"></div></div>'
        result = SourceService(browser=BrowserFixture({'html': markup})).search('stv', 'Không tồn tại')
        self.assertEqual(result, {'items': [], 'next': None})

    def test_original_browser_navigation_rejects_other_hosts(self):
        browser = BrowserFixture()
        service = SourceService(browser=browser)
        for url in ('https://evil.example/truyen/dich/1/123/1/',
                    'https://sangtacviet.vip@evil.example/',
                    'http://127.0.0.1/', 'https://sangtacviet.vip:444/',
                    'https://sangtacviet.vip/redirect?url=https://evil.example/'):
            with self.subTest(url=url), self.assertRaises(SourceError):
                service.open_original('stv', url)
        self.assertEqual(browser.calls, [])
        url = 'https://sangtacviet.vip/truyen/dich/1/123/9/'
        self.assertEqual(service.open_original('stv', url), {'opened': True, 'url': url})

    def test_metadata_and_complete_toc_preserve_source_order_and_real_ids(self):
        raw = '1-/-901-/-Chương một-//-1-/-77-/-Chương hai-//-1-/-901-/-Chương một-//-1-/-8-/-Chương ba\n'
        browser = BrowserFixture({'html': METADATA, 'toc': toc(raw)})
        book = SourceService(browser=browser).detail('stv', BOOK)
        self.assertEqual(book['name'], 'Truyện thử tiếng Việt')
        self.assertEqual(book['author'], 'Tác giả thử')
        self.assertEqual(book['description'], 'Mô tả thử nghiệm.')
        self.assertEqual(book['cover'], 'https://images.example/cover.jpg')
        self.assertEqual(book['kind'], 'text')
        self.assertEqual([item['url'] for item in book['chapters']], [BOOK + key + '/' for key in ('901', '77', '8')])
        self.assertEqual([item['index'] for item in book['chapters']], [0, 1, 2])
        self.assertTrue(all(item.get('is_vip') is None for item in book['chapters']))

    def test_toc_never_returns_partial_or_invents_placeholder_ids(self):
        bad = [
            {'code': 5, 'data': ''},
            toc(''),
            toc('1-/-about:blank-/-Một'),
            toc('1-/-901-/-Một-//-unparsed entry'),
            {'code': 1, 'data': '1-/-901-/-Một', 'oridata': '1-/-901-/-一-//-1-/-902-/-二'},
        ]
        for response in bad:
            with self.subTest(response=response):
                service = SourceService(browser=BrowserFixture({'html': METADATA, 'toc': response}))
                with self.assertRaises(SourceError) as caught:
                    service.detail('stv', BOOK)
                self.assertEqual(caught.exception.code, 'parse_error')
                self.assertFalse(hasattr(caught.exception, 'access'))

    def test_cover_refresh_does_not_need_toc(self):
        browser = BrowserFixture({'html': METADATA})
        self.assertEqual(SourceService(browser=browser).cover('stv', BOOK),
                         {'cover': 'https://images.example/cover.jpg', 'path': BOOK})
        self.assertNotIn('getchapterlist', browser.calls[0][1])

    def test_chapter_keeps_original_vietnamese_and_removes_unsafe_markup(self):
        markup = '<div class="contentbox" id="cld-123-901"><p><i h="đây" t="这" v="đây" p="m">Đây</i> là văn bản tự viết.</p><p>Giữ nguyên nghĩa và dấu Việt.</p><script>alert(1)</script></div>'
        browser = BrowserFixture({'html': markup})
        chapter = SourceService(browser=browser).chapter('stv', CHAPTER)
        self.assertEqual(chapter['text'], 'Đây là văn bản tự viết.\nGiữ nguyên nghĩa và dấu Việt.')
        self.assertNotIn('script', chapter['html'])
        self.assertNotIn('<i>', chapter['html'])
        self.assertEqual(chapter['images'], [])
        self.assertEqual((chapter['language'], chapter['translation']), ('vi', 'source'))
        self.assertEqual(len(browser.calls), 1)

    def test_chapter_reopens_once_after_normal_website_language_selection(self):
        markup = '<div class="contentbox" id="cld-123-901"><p>Văn bản tiếng Việt tự viết.</p></div>'
        browser = BrowserFixture({'language_changed': True}, {'html': markup})
        chapter = SourceService(browser=browser).chapter('stv', CHAPTER)
        self.assertEqual(chapter['text'], 'Văn bản tiếng Việt tự viết.')
        self.assertEqual([call[0] for call in browser.calls], [CHAPTER, CHAPTER])

    def test_repeated_language_selection_fails_without_loop_or_access_claim(self):
        browser = BrowserFixture({'language_changed': True}, {'language_changed': True})
        with self.assertRaises(SourceError) as caught:
            SourceService(browser=browser).chapter('stv', CHAPTER)
        self.assertEqual(caught.exception.code, 'parse_error')
        self.assertIn('tiếng Việt', str(caught.exception))
        self.assertFalse(hasattr(caught.exception, 'access'))
        self.assertEqual(len(browser.calls), 2)

    def test_wrong_chapter_and_placeholders_are_not_content(self):
        for markup in ('<div class="contentbox" id="cld-123-902">Nội dung cũ.</div>',
                       '<div id="maincontent">Nhấp vào để tải chương...</div>',
                       '<div class="contentbox" id="cld-123-901">Nhấp vào để tải chương...</div>',
                       '<div class="contentbox" id="cld-123-901">這是一個合成的中文測試不是實際小說</div>',
                       '<div class="contentbox" id="cld-123-901"></div>'):
            with self.subTest(markup=markup):
                with self.assertRaises(SourceError) as caught:
                    SourceService(browser=BrowserFixture({'html': markup})).chapter('stv', CHAPTER)
                self.assertNotEqual(caught.exception.code, 'vip_locked')

    def test_denial_evidence_is_required_and_readable_unlocked_content_wins(self):
        evidence = 'Chương này là chương VIP. Bạn chưa mua chương này.'
        browser = BrowserFixture({'error': {'code': 'vip_locked', 'evidence': evidence}})
        with self.assertRaises(SourceAccessError) as caught:
            SourceService(browser=browser).chapter('stv', CHAPTER)
        self.assertEqual(caught.exception.access, {'state': 'vip_locked', 'vip': True, 'evidence': evidence})
        for failure in ({'code': 'vip_locked', 'evidence': 'Tải thất bại'},
                        {'code': 'access_unverified', 'evidence': 'code 7'},
                        {'code': 'access_unverified', 'evidence': 'code 5'}):
            with self.subTest(failure=failure), self.assertRaises(SourceAccessError) as caught:
                SourceService(browser=BrowserFixture({'error': failure})).chapter('stv', CHAPTER)
            self.assertEqual(caught.exception.code, 'access_unverified')
            self.assertIsNone(caught.exception.access['vip'])
        markup = '<div class="contentbox" id="cld-123-901"><p>Đã mua và đọc được bình thường.</p></div>'
        chapter = SourceService(browser=BrowserFixture({'html': markup, 'error': {'code': 'vip_locked', 'evidence': evidence}})).chapter('stv', CHAPTER)
        self.assertIn('Đã mua', chapter['text'])

    def test_login_http_parse_and_unknown_access_have_separate_types(self):
        for error, expected, access in (
                ({'code': 'auth_required', 'evidence': 'Vui lòng đăng nhập để đọc chương này.'}, 'auth_required', True),
                ({'code': 'http_error', 'http_status': 403}, 'http_error', False),
                ({'code': 'http_error', 'http_status': 404}, 'http_error', False),
                ({'code': 'parse_error', 'evidence': 'unexpected structure'}, 'parse_error', False),
                ({'code': 'browser_error', 'evidence': 'private storage unavailable'}, 'browser_error', False),
                ({'code': 'access_unverified', 'evidence': 'not loaded'}, 'access_unverified', True)):
            with self.subTest(error=error), self.assertRaises(SourceError) as caught:
                SourceService(browser=BrowserFixture({'error': error})).chapter('stv', CHAPTER)
            self.assertEqual(caught.exception.code, expected)
            self.assertEqual(isinstance(caught.exception, SourceAccessError), access)

    def test_browser_transport_failures_never_claim_uncertain_entitlement(self):
        failures = [(BrowserError('render timed out', code='browser_timeout'), 'source_timeout'),
                    (BrowserError('browser is missing', code='browser_unavailable'), 'browser_unavailable'),
                    (BrowserError('protocol error', code='browser_error'), 'browser_error'),
                    (OSError('offline'), 'network_error')]
        for failure, expected in failures:
            with self.subTest(expected=expected):
                with self.assertRaises(SourceError) as caught:
                    SourceService(browser=BrowserFixture(failure)).chapter('stv', CHAPTER)
                self.assertEqual(caught.exception.code, expected)
                self.assertFalse(isinstance(caught.exception, SourceAccessError))
                self.assertFalse(hasattr(caught.exception, 'access'))

    def test_existing_text_sources_report_real_parser_failures(self):
        pages = [('full', '<h1>Missing content</h1>'),
                 ('full', '<div class="chapter-c"><script>ignored()</script></div>'),
                 ('hako', '<h1>Missing content</h1>'),
                 ('hako', '<div id="chapter-content"></div>'),
                 ('hako', '<div id="chapter-content"><div id="chapter-c-protected" data-s="unsupported"></div></div>'),
                 ('hako', '<div id="chapter-content"><div id="chapter-c-protected" data-s="base64_reverse" data-c=\'["0000!!!"]\'></div></div>')]
        for source, markup in pages:
            with self.subTest(source=source, markup=markup):
                url = 'https://truyenfull.live/book/chapter/' if source == 'full' else 'https://docln.sbs/truyen/book/chapter/'
                with self.assertRaises(SourceError) as caught:
                    SourceService(fetcher=lambda _: markup).chapter(source, url)
                self.assertEqual(caught.exception.code, 'parse_error')
                self.assertFalse(hasattr(caught.exception, 'access'))

    @unittest.skipUnless(shutil.which('node'), 'Node is required to check the browser extraction expression')
    def test_ajax_status_belongs_to_the_exact_current_source_chapter(self):
        script = 'const expression=' + json.dumps(_chapter_expression('123', '901')) + ';' + r'''
          const assert=require('node:assert/strict');
          globalThis.location=new URL('https://sangtacviet.vip/truyen/fanqie/1/123/901/');
          globalThis.window={};
          globalThis.document={querySelector:()=>null,getElementById:()=>null};
          const storage=new Map();
          globalThis.sessionStorage={getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)};
          let entries=[];
          globalThis.performance={timeOrigin:100,getEntriesByType:()=>entries};
          const name='https://sangtacviet.vip/index.php?sajax=readchapter&bookid=123&h=fanqie&c=901';
          for(const status of [403,404]){
            entries=[{name,responseStatus:status}];
            assert.deepEqual(eval(expression),{url:location.href,error:{code:'http_error',http_status:status}});
          }
          for(const entry of [
            {name,responseStatus:0},{name},{name,responseStatus:'403'},
            {name:name.replace('bookid=123','bookid=124'),responseStatus:403},
            {name:name.replace('h=fanqie','h=qidian'),responseStatus:403},
            {name:name.replace('c=901','c=902'),responseStatus:403},
            {name:name.replace('sajax=readchapter','sajax=getchapterlist'),responseStatus:403},
            {name:name.replace('sangtacviet.vip','other.example'),responseStatus:403},
            {name:name.replace('/index.php','/another.php'),responseStatus:403}
          ]){entries=[entry];assert.equal(eval(expression),null);}
          entries=[{name,responseStatus:403},{name,responseStatus:200}];
          assert.equal(eval(expression),null);
          const node={classList:{contains:()=>true},closest:()=>true,innerText:'Readable text.',outerHTML:'<div>Readable text.</div>'};
          document.getElementById=()=>node;
          entries=[{name,responseStatus:403}];
          assert.equal(eval(expression),null);
          window.__localReaderStvChapter.at-=2000;
          assert.equal(eval(expression).html,node.outerHTML);
          let selected=0;
          document.querySelector=()=>({getClientRects:()=>[{}],click:()=>selected++});
          assert.equal(eval(expression),null);
          assert.equal(selected,1);
          assert.equal(eval(expression),null);
          assert.equal(selected,1);
          performance.timeOrigin=200;
          globalThis.window={};
          document.readyState='loading';
          assert.equal(eval(expression),null);
          assert.equal(selected,1);
          document.readyState='complete';
          assert.deepEqual(eval(expression),{url:location.href,language_changed:true});
          assert.equal(storage.size,0);
          document.querySelector=()=>null;
          assert.equal(eval(expression),null);
          window.__localReaderStvChapter.at-=2000;
          assert.equal(eval(expression).html,node.outerHTML);
          sessionStorage.getItem=()=>{throw Error('storage blocked');};
          assert.equal(eval(expression).error.code,'browser_error');
        '''
        result = subprocess.run([shutil.which('node'), '-e', script], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_http_status_and_network_failures_remain_distinct(self):
        for failure, code, status in (
                (HTTPError('https://sangtacviet.vip/', 403, 'Forbidden', {}, io.BytesIO()), 'http_error', 403),
                (HTTPError('https://sangtacviet.vip/', 404, 'Missing', {}, io.BytesIO()), 'http_error', 404),
                (URLError('offline'), 'network_error', None)):
            with self.subTest(code=code, status=status):
                fetcher = _PublicFetcher()
                with patch('sources.validate_public_url', side_effect=lambda value: value), \
                        patch.object(fetcher.opener, 'open', side_effect=failure), \
                        self.assertRaises(SourceError) as caught:
                    fetcher('https://sangtacviet.vip/')
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.http_status, status)
                self.assertFalse(hasattr(caught.exception, 'access'))


if __name__ == '__main__':
    unittest.main()
