import json
import tempfile
import threading
import hashlib
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
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

    def test_settings_roundtrip(self):
        self.assertEqual(self.request("/api/settings", {"fontSize": 24})[0], 200)
        result = json.loads(self.request("/api/settings")[1])
        self.assertEqual(result["fontSize"], 24)

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

    def test_reimport_failure_preserves_existing_managed_original(self):
        body = 'A real document to keep.'.encode()
        book = self.server.app.import_file('keep.txt', body)
        original = Path(book['path'])
        with patch('documents.import_document', side_effect=ValueError('transient converter error')):
            with self.assertRaises(ValueError):
                self.server.app.import_file('keep.txt', body)
        self.assertTrue(original.is_file())
        self.assertEqual(original.read_bytes(), body)

if __name__ == "__main__":
    unittest.main()
