import base64
import io
import json
import tempfile
import threading
import hashlib
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from server import make_server
from sources import SourceService

class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(Path(self.tmp.name), port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_address[1])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, path, data=None, headers=None, method=None):
        content = json.dumps(data).encode() if data is not None else None
        req = urllib.request.Request(self.base + path, content,
               {"Content-Type": "application/json"} | (headers or {}), method=method)
        try:
            with urllib.request.urlopen(req, timeout=10) as res:
                return res.status, res.read(), res.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    def test_api_is_real_json_and_does_not_invent_books(self):
        status, body, headers = self.request("/api/books")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_second_server_on_same_port_fails_without_changing_running_library(self):
        self.server.app.store.upsert_book({'id': 'keep', 'name': 'Keep'},
                                         [{'title': 'One', 'text': 'Keep this chapter'}])
        self.server.app.store.progress('keep', 0, .6)
        before = json.loads(self.request('/api/books')[1])
        with tempfile.TemporaryDirectory() as temporary:
            other_data = Path(temporary) / 'unused'
            with self.assertRaises(OSError):
                second = make_server(other_data, port=self.server.server_address[1])
                second.server_close()
            self.assertFalse(other_data.exists())
        status, body, _ = self.request('/api/books')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), before)

    def test_settings_roundtrip(self):
        self.assertEqual(self.request("/api/settings", {"fontSize": 24})[0], 200)
        result = json.loads(self.request("/api/settings")[1])
        self.assertEqual(result["fontSize"], 24)

    def test_instance_identifies_the_local_app_and_library(self):
        status, body, _ = self.request('/api/instance')
        self.assertEqual(status, 200)
        instance = json.loads(body)
        self.assertEqual(instance['app'], 'ke-truyen-local')
        self.assertEqual(Path(instance['data_directory']), self.server.app.store.data_dir)
        self.assertIsInstance(instance['pid'], int)

    def test_shortcut_uses_saved_cover_and_actual_port_without_changing_reading_state(self):
        image = b'\x89PNG\r\n\x1a\n' + b'cover'
        store = self.server.app.store
        book = store.upsert_book({'id': 'shortcut-book', 'name': 'Book',
            'cover': 'data:image/png;base64,' + base64.b64encode(image).decode()},
            [{'title': 'One', 'text': 'Keep this chapter'}])
        store.progress(book['id'], 0, .6)
        store.save_note({'id': 'shortcut-note', 'book_id': book['id'], 'note': 'Keep this'})
        before = store.get_book(book['id'])
        def create(**kwargs):
            self.assertEqual(kwargs['book_id'], book['id'])
            self.assertEqual(kwargs['cover_bytes'], image)
            self.assertEqual(kwargs['port'], self.server.server_address[1])
            self.assertEqual(kwargs['data_dir'], store.data_dir)
            return {'path': 'temporary-desktop/book.lnk', 'used_cover': True}
        with patch('shortcuts.create_shortcut', side_effect=create):
            status, body, _ = self.request('/api/books/shortcut-book/shortcut', {})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)['used_cover'])
        self.assertEqual(store.get_book(book['id']), before)
        self.assertEqual(store.notes(book['id'])[0]['note'], 'Keep this')

    def test_shortcut_cannot_be_created_by_get_cross_origin_or_for_trashed_book(self):
        store = self.server.app.store
        store.upsert_book({'id': 'shortcut-book', 'name': 'Book'})
        with patch('shortcuts.create_shortcut', side_effect=AssertionError('Must not write a shortcut')):
            self.assertEqual(self.request('/api/books/shortcut-book/shortcut')[0], 404)
            self.assertEqual(self.request('/api/books/shortcut-book/shortcut', {}, {'Origin': 'https://other.example'})[0], 403)
            store.trash_book('shortcut-book')
            self.assertEqual(self.request('/api/books/shortcut-book/shortcut', {})[0], 400)

    def test_shortcut_can_be_created_when_remote_cover_is_unavailable(self):
        self.server.app.store.upsert_book({'id': 'no-cover', 'name': 'Book', 'source': 'hako',
            'cover': 'https://images.example/dead.jpg'})
        def create(**kwargs):
            self.assertEqual(kwargs['cover_bytes'], b'')
            return {'path': 'temporary-desktop/book.lnk', 'used_cover': False}
        with patch.object(self.server.app, 'cover', side_effect=ValueError('404')), patch('shortcuts.create_shortcut', side_effect=create):
            status, body, _ = self.request('/api/books/no-cover/shortcut', {})
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)['warning'])

    def test_dictionary_pagination_is_bounded_at_http_boundary(self):
        for word in ['one', 'two', 'three']:
            self.server.app.qt.save('vp', word, word)
        status, body, _ = self.request('/api/dictionaries?kind=vp&offset=0&limit=2')
        self.assertEqual(status, 200)
        page = json.loads(body)
        self.assertEqual(page['total'], 3)
        self.assertEqual(len(page['items']), 2)
        self.assertEqual(self.request('/api/dictionaries?limit=999999')[0], 400)

    def test_cross_origin_write_is_rejected(self):
        status, body, _ = self.request("/api/settings", {"fontSize": 99},
                                      {"Origin": "https://attacker.example"})
        self.assertEqual(status, 403)
        self.assertNotIn("fontSize", json.loads(self.request("/api/settings")[1]))

    def test_rebinding_host_is_rejected(self):
        status, _, _ = self.request("/api/books", headers={"Host": "attacker.example"})
        self.assertEqual(status, 403)

    def test_static_file_cannot_escape_web_root(self):
        status, _, _ = self.request("/..%2Flibrary.py")
        self.assertEqual(status, 403)

    def test_missing_book_returns_clear_error(self):
        status, body, _ = self.request("/api/books/missing")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))

    def test_source_search_route_passes_integer_page_to_real_adapter(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures' / 'source_hako_search.json').read_text(encoding='utf-8'))
        self.server.app.sources = SourceService(fetcher=lambda url: fixture['html'])
        status, body, _ = self.request('/api/sources/hako/search?q=spring')
        self.assertEqual(status, 200, body)
        self.assertGreater(len(json.loads(body)['items']), 0)

    def test_cached_image_remains_readable_when_dns_is_offline(self):
        url = 'https://cdn.example/page.png'
        body = b'\x89PNG\r\n\x1a\n' + b'image bytes'
        cached = self.server.app.image_dir / hashlib.sha256(url.encode()).hexdigest()
        cached.write_bytes(body)
        with patch('sources._public_addresses', side_effect=OSError('offline')):
            actual, kind = self.server.app.image(url)
        self.assertEqual(actual, body)
        self.assertEqual(kind, 'image/png')

    def test_stv_image_403_retries_https_without_referrer_and_caches_real_image(self):
        from source_errors import SourceError
        url = 'http://cdn.example/stv-cover.jpg'
        referer = 'https://sangtacviet.com/truyen/ciweimao/1/123/'
        body = b'\x89PNG\r\n\x1a\n' + b'public cover'
        with patch('server.safe_fetch_bytes', side_effect=[SourceError('Forbidden', code='http_error', http_status=403), body]) as fetch:
            actual, kind = self.server.app.image(url, referer)
            self.assertEqual((actual, kind), (body, 'image/png'))
            self.assertEqual(fetch.call_args_list[0].args[0], url)
            self.assertEqual(fetch.call_args_list[0].kwargs['headers']['Referer'], referer)
            self.assertEqual(fetch.call_args_list[1].args[0], url.replace('http:', 'https:'))
            self.assertNotIn('Referer', fetch.call_args_list[1].kwargs['headers'])
            self.assertEqual(self.server.app.image(url, referer), (body, 'image/png'))
            self.assertEqual(fetch.call_count, 2)

    def test_stv_image_retry_keeps_http_failures_and_other_sources_distinct(self):
        from source_errors import SourceError
        for referer, status in [('https://sangtacviet.com/truyen/qidian/1/123/', 404),
                                ('https://docln.sbs/truyen/123/', 403), ('', 403)]:
            with self.subTest(referer=referer, status=status), patch('server.safe_fetch_bytes', side_effect=SourceError('Failure', code='http_error', http_status=status)) as fetch:
                with self.assertRaises(SourceError): self.server.app.image('https://cdn.example/missing.jpg', referer)
                self.assertEqual(fetch.call_count, 1)

    def test_failed_cover_is_refreshed_without_changing_library_or_progress(self):
        book = self.server.app.store.upsert_book({'id': 'keep', 'name': 'Keep', 'source': 'hako',
            'path': 'https://ln.hako.vn/truyen/1-keep', 'cover': 'https://old.example/dead.jpg'},
            [{'title': 'One', 'url': 'https://docln.sbs/c1'}])
        self.server.app.store.progress('keep', 0, .6)
        self.server.app.store.save_note({'id': 'n', 'book_id': 'keep', 'note': 'Keep this'})
        body = b'\x89PNG\r\n\x1a\n' + b'new cover'
        with patch.object(self.server.app, 'image', side_effect=[ValueError('404'), (body, 'image/png')]), \
             patch.object(self.server.app.sources, 'cover', return_value={'cover': 'https://new.example/good.jpg', 'path': 'https://docln.sbs/truyen/1-keep'}) as refresh:
            status, actual, _ = self.request('/api/books/keep/cover')
        self.assertEqual((status, actual), (200, body))
        self.assertEqual(refresh.call_count, 1)
        kept = self.server.app.store.get_book('keep')
        self.assertEqual(kept['cover'], 'https://new.example/good.jpg')
        self.assertEqual((kept['position'], kept['chapter_count']), (.6, 1))
        self.assertEqual(self.server.app.store.notes('keep')[0]['note'], 'Keep this')

    def test_failed_cover_replacement_keeps_existing_metadata(self):
        self.server.app.store.upsert_book({'id': 'keep', 'name': 'Keep', 'source': 'hako',
            'path': 'https://docln.sbs/truyen/1-keep', 'cover': 'https://old.example/dead.jpg'})
        with patch.object(self.server.app, 'image', side_effect=ValueError('image unavailable')), \
             patch.object(self.server.app.sources, 'cover', return_value={'cover': 'https://new.example/good.jpg', 'path': 'https://docln.sbs/truyen/1-keep'}):
            self.assertEqual(self.request('/api/books/keep/cover')[0], 400)
        self.assertEqual(self.server.app.store.get_book('keep')['cover'], 'https://old.example/dead.jpg')

    def test_valid_cached_book_cover_does_not_refresh_source(self):
        url = 'https://cdn.example/keep.png'
        self.server.app.store.upsert_book({'id': 'keep', 'name': 'Keep', 'source': 'hako', 'cover': url})
        body = b'\x89PNG\r\n\x1a\n' + b'cached cover'
        (self.server.app.image_dir / hashlib.sha256(url.encode()).hexdigest()).write_bytes(body)
        with patch.object(self.server.app.sources, 'cover') as refresh:
            status, actual, _ = self.request('/api/books/keep/cover')
        self.assertEqual((status, actual), (200, body))
        refresh.assert_not_called()

    def test_epub_cover_is_saved_and_served_without_resetting_reading_state(self):
        image = b'\x89PNG\r\n\x1a\n' + b'test cover bytes'
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            archive.writestr('book.opf', '<package><metadata><meta name="cover" content="jacket"/></metadata>'
                '<manifest><item id="jacket" href="jacket.png" media-type="image/png"/>'
                '<item id="chapter" href="chapter.xhtml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
            archive.writestr('jacket.png', image)
            archive.writestr('chapter.xhtml', '<h1>One</h1><p>Readable text.</p>')
        body = content.getvalue()
        book = self.server.app.import_file('book.epub', body)
        store = self.server.app.store
        store.progress(book['id'], 0, .6)
        store.save_note({'id': 'keep-note', 'book_id': book['id'], 'note': 'Keep this'})
        store.update_book(book['id'], {'cover': ''})
        saved = self.server.app.import_file('book.epub', body)
        self.assertEqual(saved['id'], book['id'])
        self.assertEqual((saved['position'], saved['progress']), (.6, .6))
        self.assertEqual(store.notes(book['id'])[0]['note'], 'Keep this')
        status, data, _ = self.request('/api/books/' + book['id'])
        self.assertEqual(status, 200)
        cover = json.loads(data)['cover']
        self.assertTrue(cover.startswith('data:image/png;base64,'))
        self.assertEqual(base64.b64decode(cover.partition(',')[2]), image)
        self.assertEqual(Path(saved['path']).read_bytes(), body)

    def test_reimport_failure_preserves_existing_managed_original(self):
        body = 'A real document to keep.'.encode()
        book = self.server.app.import_file('keep.txt', body)
        original = Path(book['path'])
        with patch('documents.import_document', side_effect=ValueError('transient converter error')):
            with self.assertRaises(ValueError):
                self.server.app.import_file('keep.txt', body)
        self.assertTrue(original.is_file())
        self.assertEqual(original.read_bytes(), body)

    def test_file_import_returns_repair_warnings_and_serves_static_svg(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('META-INF/container.xml', '<container><rootfiles><rootfile full-path="book.opf"/></rootfiles></container>')
            archive.writestr('book.opf', '<package><manifest><item id="chapter" href="chapter.xhtml"/></manifest><spine><itemref idref="chapter"/></spine></package>')
            archive.writestr('chapter.xhtml', '<p>Keep this text.</p><img src="missing.png"><img src="diagram.svg">')
            archive.writestr('diagram.svg', '<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0L20 20"/></svg>')
        request = urllib.request.Request(self.base + '/api/import/file', content.getvalue(),
            {'Content-Type': 'application/octet-stream', 'X-Filename': 'vector.epub'})
        with urllib.request.urlopen(request, timeout=10) as response:
            book = json.load(response)
        self.assertTrue(any('missing.png' in w for w in book['warnings']))
        status, body, _ = self.request('/api/books/' + book['id'] + '/chapters/0')
        self.assertEqual(status, 200, body)
        self.assertIn('data:image/svg+xml;base64,', json.loads(body)['html'])

if __name__ == "__main__":
    unittest.main()
