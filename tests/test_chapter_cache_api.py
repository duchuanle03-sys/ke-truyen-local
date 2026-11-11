import json
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

from server import make_server


class ChapterCacheApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.entered, self.release = threading.Event(), threading.Event()
        class Source:
            def chapter(source_self, source, url):
                self.entered.set()
                if not self.release.wait(3): raise TimeoutError('Test fetch not released')
                return {'html': '<p>Body</p>', 'text': 'Body', 'images': []}
        self.server = make_server(self.tmp.name, port=0, source_service=Source())
        self.server.app._asset_stop.set(); self.server.app._asset_worker.join(3)
        self.store = self.server.app.store
        self.store.upsert_book({'id': 'book', 'name': 'Book', 'source': 'full'}, [{'url': 'https://example.com/0'}])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def tearDown(self):
        self.release.set()
        self.server.shutdown(); self.server.server_close(); self.thread.join(3)
        self.tmp.cleanup()

    def request(self, path, body=None, origin=None):
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', **({'Origin': origin} if origin else {})})
        try: response = urlopen(request, timeout=5)
        except HTTPError as error: response = error
        with response: return response.status, json.load(response)

    def test_controls_validate_confirmations_and_preserve_independent_image_budget(self):
        self.assertEqual(self.request('/api/cache/chapters')[1]['max_chapters'], 100)
        self.assertEqual(self.request('/api/cache/chapters', {'max_chapters': 30})[1]['max_chapters'], 30)
        self.assertEqual(self.request('/api/cache/images')[1]['max_bytes'], 1024 ** 3)
        self.assertEqual(self.request('/api/cache/chapters', {'max_chapters': True})[0], 400)
        self.assertEqual(self.request('/api/cache/chapters/legacy', {'confirmed': False})[0], 400)
        self.assertEqual(self.request('/api/cache/chapters/compact', {'confirmed': False})[0], 400)
        self.assertEqual(self.request('/api/cache/chapters/clear', {}, 'https://evil.example')[0], 403)

    def test_bad_reader_identifiers_return_http_400(self):
        for value in (None, {}, [], -1, True, '1', 2 ** 63):
            with self.subTest(index=value):
                self.assertEqual(self.request('/api/readers', {'id': 'tab', 'book_id': 'book', 'index': value})[0], 400)
        self.assertEqual(self.request('/api/readers', {'id': [], 'book_id': None})[0], 400)
        self.assertEqual(self.request('/api/readers', {'id': 'tab', 'book_id': None})[0], 200)

    def test_paused_worker_finishing_current_chapter_blocks_compaction(self):
        app = self.server.app
        job = app.download('book')
        self.assertTrue(self.entered.wait(2))
        app.job_action(job['id'], 'pause')
        with patch.object(self.store, 'compact_database', side_effect=AssertionError('Compaction interrupted a worker')):
            self.assertEqual(self.request('/api/cache/chapters/compact', {'confirmed': True})[0], 400)
        self.release.set()
        for _ in range(200):
            if all(f.done() for f in app._download_futures.values()): break
            time.sleep(.01)
        self.assertEqual(self.store.download_job(job['id'])['status'], 'paused')


if __name__ == '__main__': unittest.main()
