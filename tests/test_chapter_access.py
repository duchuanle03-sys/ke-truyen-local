import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

from server import make_server
from source_errors import SourceError, SourceAccessError


class ChapterAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = make_server(self.temp.name, port=0)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.server.server_close)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.app = self.server.app
        self.store = self.app.store
        self.store.upsert_book({'id': 'access', 'name': 'Access test', 'source': 'full',
            'path': 'https://truyenfull.live/access/'}, [
            {'title': 'One', 'url': 'https://truyenfull.live/access/one/', 'text': 'Already readable'},
            {'title': 'Two', 'url': 'https://truyenfull.live/access/two/'},
        ])
        self.store.progress('access', 0, .65)

    def request(self, path, data=None, headers=None):
        request = urllib.request.Request(self.base + path,
            None if data is None else json.dumps(data).encode(),
            {'Content-Type': 'application/json'} | (headers or {}))
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def test_only_verified_vip_denial_is_reported_and_updates_toc_without_progress(self):
        before = self.store.get_book('access')
        denied = SourceAccessError('Original website requires purchased access', code='vip_locked',
                                   vip=True, evidence='source response: vip=true, readable=false')
        with patch.object(self.app.sources, 'chapter', side_effect=denied):
            status, body = self.request('/api/books/access/chapters/1')
        self.assertEqual(status, 403)
        self.assertEqual(body['code'], 'vip_locked')
        self.assertIs(body['access']['vip'], True)
        self.assertTrue(body['access']['evidence'])
        self.assertEqual(self.store.get_book('access'), before)
        self.assertFalse(self.store.content('access', 1)['downloaded'])
        self.assertTrue(self.store.chapters('access')[1]['is_vip'])
        status, body = self.request('/api/books/access/progress', {'index': 1, 'position': 1})
        self.assertEqual(status, 400)
        self.assertEqual(self.store.get_book('access'), before)

    def test_blank_or_nonboolean_vip_evidence_is_unverified(self):
        for vip, evidence in [(True, ''), (True, '  '), (True, None), (1, 'flag'), ('true', 'flag')]:
            with self.subTest(vip=vip, evidence=evidence):
                error = SourceAccessError('Unknown', code='vip_locked', vip=vip, evidence=evidence)
                self.assertEqual(error.code, 'access_unverified')
                self.assertIsNone(error.access['vip'])

    def test_network_http_parser_and_unknown_access_do_not_become_vip(self):
        failures = [SourceError('Network unavailable', code='network_error'),
            SourceError('Forbidden', code='http_error', http_status=403),
            SourceError('Missing', code='http_error', http_status=404),
            SourceError('No chapter container', code='parse_error'),
            SourceAccessError('Cannot confirm rights', code='access_unverified'),
            SourceAccessError('Insufficient evidence', code='vip_locked', vip=True)]
        for failure in failures:
            with self.subTest(failure=failure.code), patch.object(self.app.sources, 'chapter', side_effect=failure):
                status, body = self.request('/api/books/access/chapters/1')
                self.assertEqual(body['code'], failure.code)
                self.assertNotEqual(body['code'], 'vip_locked')
                self.assertNotEqual(self.store.chapters('access')[1].get('is_vip'), True)
                self.assertFalse(self.store.content('access', 1)['downloaded'])
                self.assertEqual(self.store.get_book('access')['position'], .65)
                if failure.http_status:
                    self.assertEqual(body['http_status'], failure.http_status)

    def test_success_after_unlock_keeps_source_vietnamese_metadata_in_offline_cache(self):
        self.store.upsert_book({'id': 'stv-cache', 'name': 'STV', 'source': 'stv',
            'path': 'https://sangtacviet.vip/truyen/qidian/1/123/'},
            [{'title': 'One', 'url': 'https://sangtacviet.vip/truyen/qidian/1/123/456/', 'is_vip': True}])
        content = {'html': '<p>Tiếng Việt của nguồn.</p>', 'text': 'Tiếng Việt của nguồn.',
                   'images': [], 'translation': 'source', 'language': 'vi'}
        with patch.object(self.app.sources, 'chapter', return_value=content) as source:
            status, body = self.request('/api/books/stv-cache/chapters/0')
        self.assertEqual(status, 200, body)
        self.assertEqual(body['translation'], 'source')
        with patch.object(self.app.sources, 'chapter', side_effect=AssertionError('Cached chapter must work offline')):
            status, cached = self.request('/api/books/stv-cache/chapters/0')
        self.assertEqual(status, 200)
        self.assertEqual(cached['text'], content['text'])
        self.assertEqual(cached['language'], 'vi')
        self.assertTrue(self.store.chapters('stv-cache')[0]['is_vip'])
        self.store.upsert_book({'id': 'stv-cache', 'name': 'STV', 'source': 'stv',
            'path': 'https://sangtacviet.vip/truyen/qidian/1/123/'},
            [{'title': 'Updated title', 'url': 'https://sangtacviet.vip/truyen/qidian/1/123/456/', 'is_vip': None, 'translation': None, 'language': None}])
        refreshed = self.store.content('stv-cache', 0)
        self.assertEqual(refreshed.get('translation'), 'source')
        self.assertEqual(refreshed.get('language'), 'vi')
        self.assertEqual(refreshed['text'], content['text'])
        self.assertTrue(self.store.chapters('stv-cache')[0]['is_vip'])

    def test_verified_denial_survives_toc_refresh_but_legacy_access_values_are_ignored(self):
        self.store.chapter_access('access', 1, {'state': 'vip_locked', 'vip': True, 'evidence': 'source confirmed'})
        self.store.upsert_book({'id': 'access', 'name': 'Refreshed', 'source': 'full'}, [
            {'title': 'One', 'url': 'https://truyenfull.live/access/one/'},
            {'title': 'Two updated', 'url': 'https://truyenfull.live/access/two/'}])
        self.assertTrue(self.store.chapters('access')[1].get('is_vip'))
        with self.assertRaises(ValueError):
            self.store.progress('access', 1, 1)
        self.store.upsert_book({'id': 'legacy', 'name': 'Legacy'}, [{'title': 'One', 'access': 'legacy string'}])
        self.assertEqual(self.store.progress('legacy', 0, .2)['position'], .2)

    def test_original_action_is_guarded_and_uses_stored_chapter_url(self):
        opened = Mock(return_value={'opened': True, 'url': 'https://truyenfull.live/access/two/'})
        with patch.object(self.app.sources, 'open_original', opened, create=True):
            status, body = self.request('/api/books/access/chapters/1/original', {})
            self.assertEqual(status, 200, body)
            opened.assert_called_once_with('full', 'https://truyenfull.live/access/two/')
            opened.reset_mock()
            self.assertEqual(self.request('/api/books/access/chapters/1/original', {},
                {'Origin': 'https://foreign.invalid'})[0], 403)
            self.assertEqual(self.request('/api/books/access/chapters/1/original')[0], 404)
            opened.assert_not_called()

    def test_download_denial_stops_without_caching_or_advancing_failed_chapter(self):
        denied = SourceAccessError('Purchase required', code='vip_locked', vip=True,
                                   evidence='explicit locked VIP response')
        with patch.object(self.app.sources, 'chapter', side_effect=denied):
            job = self.app.download('access')
            deadline = time.monotonic() + 3
            while self.app.jobs[job['id']]['status'] not in ('failed', 'completed') and time.monotonic() < deadline:
                time.sleep(.01)
        saved = self.app.jobs[job['id']]
        self.assertEqual(saved['status'], 'failed')
        self.assertEqual(saved['completed'], 1)
        self.assertEqual(saved['error_code'], 'vip_locked')
        self.assertFalse(self.store.content('access', 1)['downloaded'])
        self.assertTrue(self.store.chapters('access')[1]['is_vip'])
        self.assertEqual(self.store.get_book('access')['position'], .65)


if __name__ == '__main__':
    unittest.main()
