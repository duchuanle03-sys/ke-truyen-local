"""Source parser fixtures captured from public pages; no socket access required."""
import json
from pathlib import Path
import socket
from email.message import Message
import io
import urllib.request
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from htmltools import html_to_text, sanitize_html
from sources import SourceError, SourceService, validate_public_url, safe_fetch_bytes, _PublicFetcher, _PublicRedirect, _public_socket


FIXTURES = Path(__file__).parent / 'fixtures'


def fixture(name):
    return json.loads((FIXTURES / ('source_' + name + '.json')).read_text(encoding='utf-8'))


class SourceTests(unittest.TestCase):
    def test_cover_refresh_reads_only_metadata_without_loading_toc(self):
        for source, fixture_name in [('qq', 'qq_detail'), ('full', 'full_detail'), ('hako', 'hako_detail')]:
            with self.subTest(source=source):
                saved = fixture(fixture_name)
                calls = []
                def fetch(url):
                    calls.append(url)
                    self.assertEqual(url, saved['url'])
                    return saved['html']
                result = SourceService(fetch).cover(source, saved['url'])
                self.assertTrue(result['cover'].startswith('https://'))
                self.assertEqual(calls, [saved['url']])

    def test_other_sources_refresh_only_explicit_public_cover_metadata(self):
        service = SourceService(lambda url: '<meta property="og:image" content="https://cdn.example/cover.jpg">')
        self.assertEqual(service.cover('legacy', 'https://legacy.example/book/1')['cover'], 'https://cdn.example/cover.jpg')
        for markup in ['<img src="https://cdn.example/logo.jpg">', '<meta property="og:image" content="http://127.0.0.1/private">']:
            with self.assertRaises(SourceError):
                SourceService(lambda url: markup).cover('legacy', 'https://legacy.example/book/1')

    def recorded(self, *names):
        pages = {p['url']: p['html'] for p in map(fixture, names)}
        return SourceService(pages.__getitem__)

    def test_search_real_qq_full_hako_rows(self):
        service = self.recorded('qq_search', 'full_search', 'hako_search')
        cases = [('qq', 'xu so', 'Thiên Đường Song Song Tại Dị Giới'),
                 ('full', 'cau ma', 'Cầu Ma'), ('hako', 'mua xuan', 'Mùa xuân')]
        for source, query, first_name in cases:
            with self.subTest(source=source):
                result = service.search(source, query)
                self.assertEqual(result['items'][0]['name'], first_name)
                self.assertTrue(result['items'][0]['path'].startswith('https://'))
                self.assertTrue(result['items'][0]['cover'].startswith('https://'))
        self.assertEqual(service.search('full', 'cau ma')['items'][0]['author'], 'Nhĩ Căn')

    def test_search_query_is_encoded_and_next_page_is_numeric(self):
        page = '<div class="pagination"><li class="active">1</li><li><a href="?page=2">2</a></li></div>'
        def fetch(url):
            self.assertEqual(parse_qs(urlsplit(url).query)['keywords'], ['A & B'])
            return page
        self.assertEqual(SourceService(fetch).search('hako', 'A & B')['next'], 2)

    def test_disabled_search_next_link_is_not_a_page(self):
        page = '<ul class="pagination"><li class="active">2</li><li class="disabled"><a rel="next" href="#">Next</a></li></ul>'
        self.assertIsNone(SourceService(lambda url: page).search('full', 'book', 2)['next'])

    def test_empty_or_cookie_challenge_search_is_an_error(self):
        for page in ('', '<script>document.cookie="guard=abc";location.reload()</script>'):
            with self.subTest(page=page), self.assertRaises(SourceError):
                SourceService(lambda url: page).search('qq', 'book')

    def test_qq_detail_canonicalizes_old_domain_and_reverses_toc(self):
        saved = fixture('qq_detail')
        book = self.recorded('qq_detail').detail('qq', saved['url'].replace('truyenqqko.com', 'truyenqqgo.com') + '.html')
        self.assertEqual(book['name'], 'Xứ Sở Thần Tiên Của Người Chết')
        self.assertEqual(book['kind'], 'comic')
        self.assertEqual(len(book['chapters']), 62)
        self.assertEqual(book['chapters'][0]['title'], 'Chương 1')
        self.assertTrue(book['chapters'][0]['url'].endswith('-chap-1'))
        self.assertEqual([c['index'] for c in book['chapters']], list(range(62)))

    def test_full_fetches_every_recorded_toc_page_in_order(self):
        saved = fixture('full_detail')
        responses = {p['url']: p['body'] for p in fixture('full_toc')['pages']}
        responses[saved['url']] = saved['html']
        book = SourceService(responses.__getitem__).detail('full', 'https://truyenfull.vision/cau-ma/')
        self.assertEqual(book['name'], 'Cầu Ma')
        self.assertEqual(book['author'], 'Nhĩ Căn')
        self.assertEqual(len(book['chapters']), 1470)
        self.assertEqual(book['chapters'][0]['title'], 'Chương 1: Tô Minh')
        self.assertTrue(book['chapters'][-1]['url'].endswith('/chuong-1485/'))
        self.assertEqual(len({c['url'] for c in book['chapters']}), 1470)

    def test_full_failure_never_returns_partial_toc(self):
        saved = fixture('full_detail')
        def fetch(url):
            if url == saved['url']:
                return saved['html']
            if parse_qs(urlsplit(url).query)['page'] == ['2']:
                raise OSError('offline')
            return fixture('full_toc')['pages'][0]['body']
        with self.assertRaisesRegex(SourceError, 'offline'):
            SourceService(fetch).detail('full', saved['url'])

    def test_full_repeated_page_cannot_be_reported_as_complete_toc(self):
        saved = fixture('full_detail')
        service = SourceService(lambda url: saved['html'] if url == saved['url'] else fixture('full_toc')['pages'][0]['body'])
        with self.assertRaisesRegex(SourceError, 'lặp|chưa tải đủ'):
            service.detail('full', saved['url'])

    def test_hako_keeps_chapter_order_volume_and_deduplicates_mobile_links(self):
        saved = fixture('hako_detail')
        book = self.recorded('hako_detail', 'hako_volume').detail('hako', saved['url'].replace('docln.sbs', 'ln.hako.vn'))
        self.assertEqual(book['name'], 'Tớ sẽ đợi tại mùa xuân hôm qua')
        self.assertEqual(len(book['chapters']), 6)
        self.assertEqual(book['chapters'][0]['title'], '1/4 - 3 giờ chiều')
        self.assertEqual(book['chapters'][0]['volume'], 'Toàn tập')
        self.assertEqual(book['chapters'][-1]['title'], 'Giao đoạn 3')
        self.assertIn('i.hako.vip/', book['cover'])
        self.assertEqual(book['author'], 'Hachimoku Mei')

    def test_hako_volume_urls_keep_volume_and_author(self):
        book = self.recorded('hako_volume').detail('hako', fixture('hako_volume')['url'])
        self.assertEqual(book['author'], 'Hachimoku Mei')
        self.assertEqual(len(book['chapters']), 6)
        self.assertEqual(book['chapters'][0]['volume'], 'Toàn tập')

    def test_real_chapters_extract_text_and_qq_images(self):
        service = self.recorded('qq_chapter', 'full_chapter', 'hako_chapter')
        for source in ('qq', 'full', 'hako'):
            chapter = service.chapter(source, fixture(source + '_chapter')['url'])
            if source == 'qq':
                self.assertEqual(len(chapter['images']), 2)
                self.assertTrue(chapter['images'][0].startswith('https://'))
            elif source == 'full':
                self.assertIn('Núi! Thanh sơn!', chapter['text'])
            else:
                self.assertIn('Sau khi đã thấy quá đủ', chapter['text'])
                self.assertNotIn('data-c', chapter['html'])

    def test_remote_chapter_scripts_and_event_handlers_are_discarded(self):
        raw = '<div class="chapter-c"><p onclick="bad()">Một &amp; hai.</p><script>bad()</script><iframe src="http://127.0.0.1/"></iframe><img src="javascript:bad()" onerror="bad()"><p>Ba.</p><div class="ads-responsive">Quảng cáo</div></div>'
        result = SourceService(lambda url: raw).chapter('full', 'https://truyenfull.live/book/chuong-1/')
        self.assertEqual(result['text'], 'Một & hai.\nBa.')
        for value in ('script', 'iframe', 'onclick', 'onerror', 'javascript:', 'Quảng cáo'):
            self.assertNotIn(value, result['html'])

    def test_missing_content_fails_instead_of_returning_a_login_or_empty_page(self):
        for source in ('qq', 'full', 'hako'):
            with self.subTest(source=source), self.assertRaises(SourceError):
                SourceService(lambda url: '<h1>Verify you are human</h1>').chapter(source, {
                    'qq':'https://truyenqqko.com/chapter', 'full':'https://truyenfull.live/chapter',
                    'hako':'https://docln.sbs/chapter'}[source])

    def test_hako_unknown_encoding_fails_clearly(self):
        raw = '<div id="chapter-content"><div id="chapter-c-protected" data-s="unknown" data-c="[&quot;0000YWJj&quot;]"></div></div>'
        with self.assertRaisesRegex(SourceError, 'encoding|mã hóa'):
            SourceService(lambda url: raw).chapter('hako', 'https://docln.sbs/truyen/book/c1')

    def test_hako_encoded_segments_cannot_be_missing(self):
        raw = '<div id="chapter-content"><div id="chapter-c-protected" data-s="none" data-c="[&quot;0001PHA+VGVzdDwvcD4=&quot;]"></div></div>'
        with self.assertRaisesRegex(SourceError, 'mã hóa'):
            SourceService(lambda url: raw).chapter('hako', 'https://docln.sbs/truyen/book/c1')

    def test_source_rejects_external_links_and_invalid_input_before_fetch(self):
        def fetch(url):
            self.fail('Unsafe URL reached transport')
        service = SourceService(fetch)
        for url in ('http://127.0.0.1/', 'file:///C:/secret', 'https://example.com/', 'https://truyenqqko.com@127.0.0.1/', '//127.0.0.1/'):
            with self.subTest(url=url), self.assertRaises(SourceError):
                service.detail('qq', url)
        for page in (0, -1, True, '2', 100001):
            with self.subTest(page=page), self.assertRaises(SourceError):
                service.search('full', 'book', page)


class PublicURLTests(unittest.TestCase):
    def test_blocks_non_public_addresses_and_credentials(self):
        for url in ('http://127.0.0.1/', 'http://10.0.0.1/', 'http://169.254.169.254/',
                    'http://[::1]/', 'http://[::ffff:127.0.0.1]/', 'http://localhost/',
                    'http://224.0.0.1/', 'http://[ff02::1]/', 'http://[fec0::1]/', 'http://[2002:7f00:1::]/',
                    'file:///etc/passwd', 'https://user:pass@example.com/',
                    'https://example.com:8080/', 'https://example.com/\r\nX-Test:yes',
                    'http://2130706433/', 'http://0x7f000001/', 'http://127.1/'):
            with self.subTest(url=url), self.assertRaises(SourceError):
                validate_public_url(url, resolve=False)

    def test_blocks_dns_private_and_mixed_answers(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('192.168.1.1', 443))
        for answers in ([private], [public, private]):
            with patch('sources.socket.getaddrinfo', return_value=answers), self.assertRaises(SourceError):
                validate_public_url('https://example.com')

    def test_public_url_normalizes_without_losing_query_or_unicode_path(self):
        self.assertEqual(validate_public_url('https://EXAMPLE.com/a?q=one#anchor', resolve=False), 'https://example.com/a?q=one')

    def test_redirects_cannot_reach_private_dns_or_downgrade_tls(self):
        request = urllib.request.Request('https://truyenfull.live/book/')
        private = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))
        with patch('sources.socket.getaddrinfo', return_value=[private]), self.assertRaises(SourceError):
            _PublicRedirect().redirect_request(request, None, 302, 'Found', {}, 'https://internal.example.com/')
        with patch('sources._public_addresses', return_value=[]), self.assertRaises(SourceError):
            _PublicRedirect().redirect_request(request, None, 302, 'Found', {}, 'http://truyenfull.live/book/')

    def test_connection_pins_the_validated_address(self):
        public = (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))
        connections = []
        class Sock:
            def settimeout(self, timeout):
                pass
            def connect(self, address):
                connections.append(address)
        with patch('sources.socket.getaddrinfo', return_value=[public]), patch('sources.socket.socket', return_value=Sock()):
            _public_socket(('public.example.com', 443), timeout=1)
        self.assertEqual(connections, [('93.184.216.34', 443)])

    def test_qq_cookie_bootstrap_is_replayed_without_executing_script(self):
        transport = _PublicFetcher()
        url = 'https://truyenqqko.com/book'
        def response(body):
            value = io.BytesIO(body.encode())
            value.url = url
            value.headers = Message()
            value.headers['Content-Type'] = 'text/html; charset=utf-8'
            return value
        with patch('sources._public_addresses', return_value=[]), patch.object(transport.opener, 'open', side_effect=[
                response('<script>document.cookie = "guard=abc123;path=/";location.reload()</script>'), response('<h1>Book</h1>')]):
            self.assertEqual(transport(url), '<h1>Book</h1>')
        request = urllib.request.Request(url)
        transport.cookies.add_cookie_header(request)
        self.assertEqual(request.get_header('Cookie'), 'guard=abc123')
        other = urllib.request.Request('https://other.example.com/')
        transport.cookies.add_cookie_header(other)
        self.assertIsNone(other.get_header('Cookie'))

    def test_mixed_hex_ipv4_cannot_be_embedded_in_html(self):
        self.assertNotIn('src=', sanitize_html('<img src="http://0x7f.0.0.1/secret">'))

    def test_binary_transport_returns_original_bytes_and_enforces_limit(self):
        def response(body):
            value = io.BytesIO(body)
            value.url = 'https://images.example.com/page.png'
            value.headers = Message()
            value.headers['Content-Type'] = 'image/png'
            return value
        with patch('sources._public_addresses', return_value=[]), patch('urllib.request.OpenerDirector.open', side_effect=[
                response(b'\x89PNG\r\n\x1a\n'), response(b'123456')]):
            self.assertEqual(safe_fetch_bytes('https://images.example.com/page.png', max_bytes=8), b'\x89PNG\r\n\x1a\n')
            with self.assertRaisesRegex(SourceError, 'limit|giới hạn'):
                safe_fetch_bytes('https://images.example.com/page.png', max_bytes=5)


class HTMLSafetyTests(unittest.TestCase):
    def test_sanitizer_preserves_inline_words_breaks_and_safe_images(self):
        raw = '<p>Một <em>hai</em> ba &amp; bốn<br>Năm.</p><img src="/p.png" onerror="x"><svg><script>x</script></svg>'
        clean = sanitize_html(raw, 'https://example.com/chapter')
        self.assertEqual(html_to_text(clean), 'Một hai ba & bốn\nNăm.')
        self.assertIn('https://example.com/p.png', clean)
        self.assertNotIn('onerror', clean)
        self.assertNotIn('script', clean)

    def test_sanitizer_rejects_private_image_urls_and_css(self):
        clean = sanitize_html('<p style="background:url(http://localhost)">ok</p><img src="http://192.168.0.1/x"><img src="data:image/svg+xml,bad"><a href="javascript:bad()">link</a>')
        self.assertEqual(html_to_text(clean), 'ok\nlink')
        self.assertNotIn('src=', clean)
        self.assertNotIn('style=', clean)


if __name__ == '__main__':
    unittest.main()
