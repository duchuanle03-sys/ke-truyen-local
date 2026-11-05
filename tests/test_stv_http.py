"""Synthetic HTTP contracts; no captured text, real cookies or CAPTCHA answers."""
import base64
import json
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urljoin

from sources import SourceService
from source_errors import SourceError
from stv_source import StvSource

ORIGIN = 'https://sangtacviet.com'
BOOK = ORIGIN + '/truyen/qidian/1/123/'
CHAPTER = BOOK + '901/'
METADATA = '<h1 id="book_name2">Truyện thử tiếng Việt</h1><meta property="og:novel:author" content="Tác giả"><meta property="og:image" content="/cover.jpg">'
TOC = {'code': 1, 'data': '1-/-901-/-Chương một-//-1-/-902-/-Chương hai', 'oridata': '1-/-901-/-一-//-1-/-902-/-二'}
IMAGE = {'body_base64': base64.b64encode(b'\x89PNG\r\n\x1a\nfixture').decode(), 'content_type': 'image/png'}

def chapter(cid='901', **changes):
    return {'code': '0', 'bookid': '123', 'bookhost': 'qidian', 'origin': 'https://m.qidian.com/book/123/' + cid + '.html',
            'data': '<p><i t="中文" h="a">Tiếng Việt</i> rõ ràng.</p><p>Đoạn hai.</p>', **changes}

class HttpFixture:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
    def request(self, path, **options):
        self.calls.append((path, options))
        if not self.responses: raise AssertionError('Unexpected HTTP request: ' + path)
        value = self.responses.pop(0)
        if isinstance(value, Exception): raise value
        if isinstance(value, dict) and 'body_base64' in value: return value
        return {'text': json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value,
                'request_url': urljoin(ORIGIN, path), 'request_method': options.get('method', 'GET')}
    def close(self): pass

class StvHttpTests(unittest.TestCase):
    def source(self, *responses):
        http = HttpFixture(*responses)
        return StvSource(http=http), http

    def test_service_reuses_http_source_and_canonicalizes_old_domain(self):
        service = SourceService()
        self.addCleanup(lambda: getattr(service, 'close', lambda: None)())
        self.assertEqual(service._url('stv', CHAPTER.replace('.com', '.vip')), CHAPTER)
        self.assertIs(service._stv(), service._stv())
        with patch('webbrowser.open', return_value=True) as opened:
            self.assertTrue(service.open_original('stv', CHAPTER)['opened'])
        opened.assert_called_once_with(CHAPTER)

    def test_verified_bootstrap_empty_post_and_safe_vietnamese(self):
        source, http = self.source(TOC, '<script src="/stv.readinit.js?v=8"></script>', {'code': 7, 'time': 1000}, chapter())
        result = source.chapter(CHAPTER)
        self.assertEqual(result['text'], 'Tiếng Việt rõ ràng.\nĐoạn hai.')
        self.assertEqual((result['language'], result['translation']), ('vi', 'source'))
        self.assertNotIn('<i', result['html'])
        self.assertNotIn('中文', result['html'])
        self.assertEqual(len(http.calls), 4)
        bootstrap, actual = http.calls[-2:]
        self.assertEqual(bootstrap[1]['method'], 'POST')
        self.assertTrue(bootstrap[1]['seed'])
        self.assertTrue(bootstrap[1]['xhr'])
        self.assertEqual(actual[1]['body'], '')
        self.assertEqual(actual[1]['referer'], CHAPTER)
        self.assertFalse(any('ngmar=trans' in p for p, _ in http.calls))

    def test_initialized_pages_skip_bootstrap_and_keep_reading_session(self):
        source, http = self.source(TOC, '<div>page</div>', chapter(), '<div>page</div>', chapter('902'))
        source.chapter(CHAPTER); source.chapter(BOOK + '902/')
        self.assertEqual(len(http.calls), 5)
        self.assertTrue(all(o['session'] == 'reading' for _, o in http.calls[1:]))
        self.assertFalse(any(o.get('seed') for _, o in http.calls))

    def test_complete_toc_has_real_ids_and_no_invented_vip(self):
        source, _ = self.source(METADATA, TOC)
        detail = source.detail(BOOK)
        self.assertEqual(detail['name'], 'Truyện thử tiếng Việt')
        self.assertEqual([c['url'] for c in detail['chapters']], [CHAPTER, BOOK + '902/'])
        self.assertTrue(all(c.get('is_vip') is None for c in detail['chapters']))

    def test_wrong_chapter_is_rejected_even_with_code_zero(self):
        for changes in ({'origin': 'https://m.qidian.com/book/123/901999.html'}, {'origin': 'https://m.qidian.com.evil.test/book/123/901.html'},
                        {'origin': 'https://m.qidian.com/book/123/901.html?other=1'}, {'origin': ''}, {'bookid': '124'}, {'bookhost': 'fanqie'}, {'data': ''}):
            with self.subTest(changes=changes):
                source, _ = self.source(TOC, '<div>page</div>', chapter(**changes))
                with self.assertRaises(SourceError) as caught: source.chapter(CHAPTER)
                self.assertEqual(caught.exception.code, 'parse_error')
                self.assertFalse(hasattr(caught.exception, 'access'))

    def test_chapter_must_be_in_actual_toc(self):
        source, http = self.source(TOC)
        with self.assertRaises(SourceError): source.chapter(BOOK + '999/')
        self.assertEqual(len(http.calls), 1)

    def test_ciweimao_without_origin_uses_exact_paragraph_chapter_ids(self):
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        data = '<p>Tiếng Việt.<a class="num-link" href="https://wap.ciweimao.com/chapter/get_par_tsu_list?chapter_id=901&amp;data-pgid=1">4</a></p><p>Đoạn hai.<a class="num-link num-link2" href="https://wap.ciweimao.com/chapter/get_par_tsu_list?chapter_id=901&amp;data-pgid=2">0</a></p>'
        source, _ = self.source(TOC, '<div>page</div>', chapter(bookhost='ciweimao', origin='', data=data))
        result = source.chapter(url)
        self.assertEqual(result['text'], 'Tiếng Việt.\nĐoạn hai.')
        self.assertEqual(result['html'].count('<p>'), 2)
        self.assertNotIn('num-link', result['html'])

    def test_ciweimao_missing_origin_does_not_accept_wrong_ambiguous_or_forged_ids(self):
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        link = '<a class="num-link" href="https://wap.ciweimao.com/chapter/get_par_tsu_list?chapter_id=901&amp;data-pgid=1">0</a>'
        for data in (link.replace('chapter_id=901', 'chapter_id=902'), link.replace('chapter_id=901', 'chapter_id=901999'),
                     link.replace('wap.ciweimao.com/', 'wap.ciweimao.com.evil.test/'),
                     link.replace('/chapter/get_par_tsu_list?', '/other?'),
                     link.replace('chapter_id=901', 'chapter_id=901&amp;chapter_id=902'),
                     link + link.replace('chapter_id=901', 'chapter_id=902')):
            source, _ = self.source(TOC, '<div>page</div>', chapter(bookhost='ciweimao', origin='', data='<p>Nội dung thử.</p>'+data))
            with self.subTest(data=data), self.assertRaises(SourceError) as caught:
                source.chapter(url)
            self.assertEqual(caught.exception.code, 'parse_error')

    def test_ciweimao_without_optional_identifiers_uses_the_verified_post_resource(self):
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        # Source chapter caches can retain an older title than the current TOC.
        payload = chapter(bookhost='ciweimao', origin='', chaptername='Tên chương cũ', data='<p>Nội dung tiếng Việt.</p>')
        source, http = self.source(TOC, '<div>page</div>', payload)
        self.assertEqual(source.chapter(url)['text'], 'Nội dung tiếng Việt.')
        path, options = http.calls[-1]
        self.assertEqual(options['method'], 'POST')
        query = parse_qs(path.split('?', 1)[1])
        self.assertEqual((query['bookid'], query['h'], query['c']), (['123'], ['ciweimao'], ['901']))

    def test_ciweimao_no_identifier_fallback_still_rejects_wrong_book_or_source(self):
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        for fields in ({'bookid': '124'}, {'bookhost': 'qidian'}, {'bookid': None}, {'bookhost': None}):
            payload = chapter(bookhost='ciweimao', origin='', data='<p>Nội dung tiếng Việt.</p>'); payload.update(fields)
            source, _ = self.source(TOC, '<div>page</div>', payload)
            with self.subTest(fields=fields), self.assertRaises(SourceError): source.chapter(url)

    def test_response_must_belong_to_the_actual_post_book_and_chapter(self):
        payload = chapter(bookhost='ciweimao', origin='', data='<p>Nội dung tiếng Việt.</p>')
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        api = ORIGIN + '/index.php?bookid=123&h=ciweimao&c=901&ngmar=readc&sajax=readchapter&sty=1&exts='
        for changes in ({'request_method': 'GET'}, {'request_url': api.replace('c=901', 'c=902')},
                        {'request_url': api.replace('bookid=123', 'bookid=124')},
                        {'request_url': api.replace('sangtacviet.com', 'evil.test')},
                        {'request_url': api + '&c=902'}, {'request_url': ''}):
            class MismatchedHttp(HttpFixture):
                def request(self, path, **options):
                    response = super().request(path, **options)
                    if 'sajax=readchapter' in path: response.update(changes)
                    return response
            source = StvSource(http=MismatchedHttp(TOC, '<div>page</div>', payload))
            with self.subTest(changes=changes), self.assertRaises(SourceError) as caught: source.chapter(url)
            self.assertEqual(caught.exception.code, 'parse_error')

    def test_ciweimao_reference_does_not_override_mismatched_book_or_nonempty_origin(self):
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        data = '<p>Tiếng Việt.<a class="num-link" href="https://wap.ciweimao.com/chapter/get_par_tsu_list?chapter_id=901">0</a></p>'
        for changes in ({'bookid': '124'}, {'bookhost': 'qidian'}, {'origin': 'https://wap.ciweimao.com/chapter/902'},
                        {'origin': 'https://evil.test/chapter/901'}):
            payload = chapter(bookhost='ciweimao', origin='', data=data); payload.update(changes)
            source, _ = self.source(TOC, '<div>page</div>', payload)
            with self.subTest(changes=changes), self.assertRaises(SourceError):
                source.chapter(url)

    def test_ciweimao_explicit_chapter_reference_cannot_disagree_with_valid_origin(self):
        url = BOOK.replace('/qidian/', '/ciweimao/') + '901/'
        data = '<p>Nội dung.<a class="num-link" href="https://wap.ciweimao.com/chapter/get_par_tsu_list?chapter_id=902">0</a></p>'
        payload = chapter(bookhost='ciweimao', origin='https://wap.ciweimao.com/chapter/901', data=data)
        source, _ = self.source(TOC, '<div>page</div>', payload)
        with self.assertRaises(SourceError) as caught: source.chapter(url)
        self.assertEqual(caught.exception.code, 'parse_error')

    def test_only_chinese_calls_trans_and_paragraphs_are_not_duplicated(self):
        source, http = self.source(TOC, '<div>page</div>', chapter(data='<p>这是合成的中文测试。</p><p>第二段文字。</p>'), '<p>Đoạn thứ nhất.</p><p>Đoạn thứ hai.</p>')
        self.assertEqual(source.chapter(CHAPTER)['text'], 'Đoạn thứ nhất.\nĐoạn thứ hai.')
        path, options = http.calls[-1]
        self.assertIn('ngmar=trans&langhint=chinese', path)
        self.assertEqual(parse_qs(options['body'])['content'], ['这是合成的中文测试。\n第二段文字。'])
        self.assertEqual(options['referer'], ORIGIN + '/trans/')

    def test_html_safety_and_unicode(self):
        content = '<p onclick="bad()">Tiếng Việt &amp; dấu câu.</p><script>bad()</script><iframe>bad()</iframe><p>Đoạn hai<br>Xuống dòng.</p>'
        source, _ = self.source(TOC, '<div>page</div>', chapter(data=content))
        result = source.chapter(CHAPTER)
        self.assertEqual(result['text'], 'Tiếng Việt & dấu câu.\nĐoạn hai\nXuống dòng.')
        self.assertNotIn('bad', result['html'])

    def test_unknown_errors_are_neither_vip_nor_captcha(self):
        for code in ('5', '7', '403', '404'):
            source, _ = self.source(TOC, '<div>page</div>', {'code': code, 'err': '4002'})
            with self.subTest(code=code), self.assertRaises(SourceError) as caught: source.chapter(CHAPTER)
            self.assertNotEqual(caught.exception.code, 'captcha_required')
            self.assertFalse(hasattr(caught.exception, 'access'))

    def required(self, source):
        with self.assertRaises(SourceError) as caught: source.chapter(CHAPTER)
        self.assertEqual(caught.exception.code, 'captcha_required')
        self.assertFalse(hasattr(caught.exception, 'access'))
        return caught.exception.captcha['id']

    def test_image_refresh_failure_manual_success_and_resume(self):
        source, http = self.source(TOC, '<div>page</div>', {'code': '21'}, IMAGE, 'Xác thực bot thất bại', IMAGE, 'success', '<div>page</div>', chapter())
        token = self.required(source)
        self.assertTrue(source.captcha('image', token)['image'].startswith('data:image/png;base64,'))
        with self.assertRaises(SourceError) as caught: source.captcha('verify', token, 'manual-answer')
        self.assertEqual(caught.exception.code, 'captcha_failed')
        renewed = source.captcha('refresh', token)
        with self.assertRaises(SourceError): source.captcha('verify', token, 'old-answer')
        self.assertTrue(source.captcha('verify', renewed['id'], 'manual-answer')['verified'])
        self.assertEqual(source.chapter(CHAPTER)['language'], 'vi')
        verifies = [(p, o) for p, o in http.calls if 'ngmar=verifyca' in p]
        self.assertEqual(len(verifies), 2)
        for _, options in verifies:
            self.assertTrue(options['fresh_connection'])
            self.assertEqual(parse_qs(options['body']), {'ajax': ['verifycaptcha'], 'token': ['manual-answer'], 'purpose': ['read'], 'provider': ['sangtacviet']})

    def test_pending_challenge_does_not_repeat_requests_and_cancel_keeps_session(self):
        source, http = self.source(TOC, '<div>page</div>', {'code': '21'})
        token = self.required(source)
        self.assertEqual(self.required(source), token)
        self.assertEqual(len(http.calls), 3)
        self.assertTrue(source.captcha('cancel', token)['cancelled'])
        with self.assertRaises(SourceError): source.captcha('image', token)

    def test_new_explicit_challenge_can_appear_after_success(self):
        source, _ = self.source(TOC, '<div>page</div>', {'code': '21'}, IMAGE, 'success', '<div>page</div>', {'code': '21'})
        token = self.required(source); source.captcha('image', token)
        source.captcha('verify', token, 'manual-answer')
        self.assertNotEqual(self.required(source), token)

    def test_no_captcha_without_an_explicit_challenge(self):
        source, http = self.source()
        for action in ('image', 'refresh', 'verify', 'cancel'):
            with self.subTest(action=action), self.assertRaises(SourceError): source.captcha(action, 'invented', 'manual-answer')
        self.assertEqual(http.calls, [])

    def test_search_uses_the_verified_http_endpoint_and_pages(self):
        row = '<a class="booksearch" href="/truyen/qidian/1/123/"><span class="searchbooktitle">Tên truyện</span><span class="searchbookauthor">Tác giả</span><img src="/cover.jpg"></a>'
        source, http = self.source(row * 48, 'Không tìm thấy truyện?')
        first = source.search('tìm kiếm', 1)
        self.assertEqual(len(first['items']), 1)
        self.assertEqual(first['next'], 2)
        self.assertTrue(http.calls[0][0].startswith('/io/searchtp/searchBooks?'))
        self.assertIn('findinname', parse_qs(http.calls[0][0].split('?', 1)[1]))
        self.assertEqual(source.search('không có', 2), {'items': [], 'next': None})

    def test_explicit_vip_evidence_is_distinct_from_unknown_website_errors(self):
        source, _ = self.source(TOC, '<div>page</div>', {'code': '5', 'err': 'Chương VIP. Bạn chưa mua chương này.'})
        with self.assertRaises(SourceError) as caught: source.chapter(CHAPTER)
        self.assertEqual(caught.exception.code, 'vip_locked')
        self.assertTrue(caught.exception.access['vip'])

    def test_bad_image_and_worker_generation_changes_never_submit_an_answer(self):
        source, http = self.source(TOC, '<div>page</div>', {'code': '21'}, {'body_base64': 'PHN2Zy8+', 'content_type': 'image/png'})
        token = self.required(source)
        with self.assertRaises(SourceError): source.captcha('image', token)
        http.generation = 1
        with self.assertRaises(SourceError) as caught: source.captcha('verify', token, 'not-sent')
        self.assertEqual(caught.exception.code, 'captcha_expired')
        self.assertFalse(any('verifyca' in p for p, _ in http.calls))

    def test_other_providers_require_exact_chapter_identity_in_origin_not_a_substring(self):
        book_url = BOOK.replace('/qidian/', '/dich/')
        for origin, valid in ((book_url + '901/', True), (book_url + '901999/', False),
                              ('https://other.example/book/901.html', True), ('https://other.example/book/1901.html', False),
                              ('https://other.example/book/901.html?c=902', False)):
            source, _ = self.source(TOC, '<div>page</div>', chapter(bookhost='dich', origin=origin))
            if valid:
                self.assertEqual(source.chapter(book_url + '901/')['language'], 'vi')
            else:
                with self.assertRaises(SourceError): source.chapter(book_url + '901/')

    def test_trans_error_envelopes_are_never_accepted_as_vietnamese_chapters(self):
        for response in ('{"code":21,"err":"CAPTCHA required"}', '{"code":5,"err":"Failed"}', '{"error":"No translation"}'):
            source, _ = self.source(TOC, '<div>page</div>', chapter(data='<p>这是合成的中文测试。</p>'), response)
            with self.subTest(response=response):
                with self.assertRaises(SourceError) as caught:
                    source.chapter(CHAPTER)
                self.assertFalse(hasattr(caught.exception, 'access'))
                self.assertIn('Trans', str(caught.exception))
