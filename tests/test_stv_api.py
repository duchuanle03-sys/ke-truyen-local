"""Exercise the real local API and Store against synthetic STV HTTP replies."""
import json
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from server import make_server
from stv_source import StvSource
from test_stv_http import HttpFixture, TOC, IMAGE, BOOK, CHAPTER, chapter


class StvApiTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(self.temp.name, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.store = self.server.app.store
        self.store.upsert_book({'id': 'http-stv', 'name': 'HTTP test', 'source': 'stv', 'path': BOOK},
            [{'title': 'One', 'url': CHAPTER, 'text': 'Đã lưu trước.'}, {'title': 'Two', 'url': BOOK + '902/'}])
        self.store.progress('http-stv', 0, .62)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(2); self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        request = Request(self.base + path, None if data is None else json.dumps(data).encode(),
                          {'Content-Type': 'application/json', **(headers or {})})
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def fixture(self, *responses):
        source = StvSource(http=HttpFixture(*responses))
        self.server.app.sources._stv_source = source
        return source

    def wait_for(self, predicate):
        for _ in range(100):
            if predicate(): return
            time.sleep(.03)
        self.fail('Operation did not reach the expected state')

    def test_required_verify_and_retry_preserve_progress_and_only_cache_success(self):
        source = self.fixture(TOC, '<div>page</div>', {'code': '21'}, IMAGE, 'success', '<div>page</div>', chapter('902'))
        before = self.store.get_book('http-stv')
        status, error = self.request('/api/books/http-stv/chapters/1')
        self.assertEqual(status, 409)
        self.assertEqual(error['code'], 'captcha_required')
        self.assertNotIn('access', error)
        token = error['captcha']['id']
        self.assertEqual(self.store.get_book('http-stv'), before)
        self.assertFalse(self.store.content('http-stv', 1)['downloaded'])
        status, image = self.request('/api/sources/stv/captcha?id=' + token)
        self.assertEqual(status, 200)
        self.assertTrue(image['image'].startswith('data:image/png;base64,'))
        self.assertEqual(self.request('/api/sources/stv/captcha/verify', {'id': token, 'answer': 'manual-answer'})[0], 200)
        self.assertEqual(self.request('/api/books/http-stv/chapters/1')[0], 200)
        self.assertTrue(self.store.content('http-stv', 1)['downloaded'])
        self.assertEqual(self.store.get_book('http-stv')['position'], before['position'])
        count = len(source.http.calls)
        self.assertEqual(self.request('/api/books/http-stv/chapters/1')[0], 200)
        self.assertEqual(len(source.http.calls), count)

    def test_captcha_api_rejects_cross_origin_and_unrequested_challenges(self):
        source = self.fixture()
        self.assertEqual(self.request('/api/sources/stv/captcha?id=fake')[0], 400)
        self.assertEqual(self.request('/api/sources/stv/captcha/verify', {'id': 'fake', 'answer': 'manual-answer'}, {'Origin': 'https://evil.test'})[0], 403)
        self.assertEqual(source.http.calls, [])

    def test_background_download_waits_then_resumes_same_chapter_after_verification(self):
        self.fixture(TOC, '<div>page</div>', {'code': '21'}, IMAGE, 'success', '<div>page</div>', chapter('902'))
        status, job = self.request('/api/books/http-stv/download', {'start': 0, 'count': 2})
        self.assertEqual(status, 200)
        actual = self.server.app.jobs[job['id']]
        self.wait_for(lambda: actual['status'] == 'verification_required')
        self.assertEqual(actual['completed'], 1)
        token = actual['captcha']['id']
        self.request('/api/sources/stv/captcha?id=' + token)
        self.request('/api/sources/stv/captcha/verify', {'id': token, 'answer': 'manual-answer'})
        self.wait_for(lambda: actual['status'] == 'completed')
        self.assertEqual(actual['completed'], 2)
        self.assertEqual(self.store.get_book('http-stv')['position'], .62)

    def test_cancel_verification_does_not_discard_downloaded_chapters_or_progress(self):
        self.fixture(TOC, '<div>page</div>', {'code': '21'})
        _, job = self.request('/api/books/http-stv/download', {'start': 0, 'count': 2})
        actual = self.server.app.jobs[job['id']]
        self.wait_for(lambda: actual['status'] == 'verification_required')
        token = actual['captcha']['id']
        self.assertEqual(self.request('/api/sources/stv/captcha/cancel', {'id': token})[0], 200)
        self.wait_for(lambda: actual['status'] == 'failed')
        self.assertEqual(actual['completed'], 1)
        self.assertTrue(self.store.content('http-stv', 0)['downloaded'])
        self.assertFalse(self.store.content('http-stv', 1)['downloaded'])
        self.assertEqual(self.store.get_book('http-stv')['position'], .62)

    def test_refresh_updates_the_challenge_on_waiting_downloads(self):
        self.fixture(TOC, '<div>page</div>', {'code': '21'}, IMAGE, IMAGE)
        _, job = self.request('/api/books/http-stv/download', {'start': 0, 'count': 2})
        actual = self.server.app.jobs[job['id']]
        self.wait_for(lambda: actual['status'] == 'verification_required')
        old = actual['captcha']['id']
        self.request('/api/sources/stv/captcha?id=' + old)
        _, renewed = self.request('/api/sources/stv/captcha/refresh', {'id': old})
        self.assertNotEqual(old, renewed['id'])
        self.assertEqual(actual['captcha']['id'], renewed['id'])

    def test_cancel_can_finish_an_inflight_refresh_using_previous_id(self):
        self.fixture(TOC, '<div>page</div>', {'code': '21'}, IMAGE, IMAGE)
        _, job = self.request('/api/books/http-stv/download', {'start': 0, 'count': 2})
        actual = self.server.app.jobs[job['id']]
        self.wait_for(lambda: actual['status'] == 'verification_required')
        old = actual['captcha']['id']
        self.request('/api/sources/stv/captcha?id=' + old)
        self.request('/api/sources/stv/captcha/refresh', {'id': old})
        self.assertEqual(self.request('/api/sources/stv/captcha/verify', {'id': old, 'answer': 'not-sent'})[0], 400)
        self.assertEqual(self.request('/api/sources/stv/captcha/cancel', {'id': old})[0], 200)
        self.wait_for(lambda: actual['status'] == 'failed')
        self.assertEqual(actual['error_code'], 'captcha_cancelled')
