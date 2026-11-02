import collections
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from server import App


PNG = b'\x89PNG\r\n\x1a\n' + b'cached image bytes'


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.events = []
        self.futures = []
        self.payloads = {
            f'https://example.com/chapter-{index}': {
                'html': f'<p>Chapter {index} text</p>',
                'text': f'Chapter {index} text',
                'images': [f'https://cdn.example/page-{index}.png'],
            } for index in range(2)
        }
        self.source = SimpleNamespace(chapter=Mock(side_effect=lambda source, url: self.payloads[url]))
        self.app = App(self.tmp.name, self.source)
        self.app.store.upsert_book(
            {'id': 'book', 'name': 'Download test', 'source': 'full',
             'path': 'https://example.com/book', 'kind': 'text'},
            [{'title': f'Chapter {index}', 'url': url}
             for index, url in enumerate(self.payloads)])
        self.fetch_patch = patch('server.safe_fetch_bytes', return_value=PNG)
        self.fetch = self.fetch_patch.start()
        self.dns_patch = patch('sources._public_addresses', return_value=[])
        self.dns_patch.start()
        submit = self.app.executor.submit

        def record_future(*args, **kwargs):
            future = submit(*args, **kwargs)
            self.futures.append(future)
            return future

        self.submit_patch = patch.object(self.app.executor, 'submit', side_effect=record_future)
        self.submit_patch.start()

    def tearDown(self):
        try:
            for job in self.app.jobs.values():
                job['cancelled'] = True
            for event in self.events:
                event.set()
            for future in self.futures:
                future.result(timeout=4)
        finally:
            self.app.close()
            self.submit_patch.stop()
            self.fetch_patch.stop()
            self.dns_patch.stop()
            self.tmp.cleanup()

    def event(self):
        event = threading.Event()
        self.events.append(event)
        return event

    def wait_for(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Download did not reach the expected state within three seconds.')
            time.sleep(.01)

    def block_first_chapter(self):
        entered, release = self.event(), self.event()

        def load(source, url):
            if url == 'https://example.com/chapter-0':
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('Test did not release the in-flight chapter.')
            return self.payloads[url]

        self.source.chapter.side_effect = load
        return entered, release

    def test_image_failure_fails_job_and_retry_reuses_saved_text_and_images(self):
        first = self.payloads['https://example.com/chapter-0']
        first['images'] = ['https://cdn.example/first.png', 'https://cdn.example/failed.png']
        attempts = collections.Counter()

        def fetch(url, **kwargs):
            attempts[url] += 1
            if url.endswith('/failed.png') and attempts[url] == 1:
                raise OSError('Image connection failed.')
            return PNG

        self.fetch.side_effect = fetch
        initial = self.app.download('book')
        self.futures[-1].result(timeout=3)
        failed = self.app.jobs[initial['id']]
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['completed'], 0)
        self.assertIn('Image connection failed', failed['error'])
        self.assertEqual(self.app.store.content('book', 0)['text'], first['text'])
        self.assertTrue(self.app.store.content('book', 0)['downloaded'])
        self.assertFalse(self.app.store.content('book', 1)['downloaded'])

        retry = self.app.job_action(initial['id'], 'retry')
        self.futures[-1].result(timeout=3)
        self.assertNotEqual(retry['id'], initial['id'])
        self.assertEqual(self.app.jobs[retry['id']]['status'], 'completed')
        self.assertEqual(self.app.jobs[retry['id']]['completed'], 2)
        self.assertEqual(self.source.chapter.call_args_list,
                         [call('full', url) for url in self.payloads])
        self.assertEqual(attempts['https://cdn.example/first.png'], 1)
        self.assertEqual(attempts['https://cdn.example/failed.png'], 2)
        self.assertEqual(attempts['https://cdn.example/page-1.png'], 1)

    def test_completed_text_and_images_remain_available_after_offline_restart(self):
        job = self.app.download('book')
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'completed')
        self.assertEqual(self.app.jobs[job['id']]['completed'], 2)
        self.app.close()
        offline_source = SimpleNamespace(chapter=Mock(side_effect=AssertionError('Network source used offline.')))
        self.app = App(self.tmp.name, offline_source)
        self.fetch.side_effect = AssertionError('Image transport used offline.')
        with patch('sources._public_addresses', side_effect=AssertionError('DNS used offline.')):
            for index, payload in enumerate(self.payloads.values()):
                chapter = self.app.chapter('book', index)
                self.assertTrue(chapter['downloaded'])
                self.assertEqual(chapter['text'], payload['text'])
                self.assertEqual(chapter['html'], payload['html'])
                for url in payload['images']:
                    self.assertEqual(self.app.image(url), (PNG, 'image/png'))
        offline_source.chapter.assert_not_called()

    def test_pause_finishes_inflight_chapter_then_resume_downloads_the_rest(self):
        entered, release = self.block_first_chapter()
        job = self.app.download('book')
        self.assertTrue(entered.wait(2))
        self.assertEqual(self.app.job_action(job['id'], 'pause')['status'], 'paused')
        release.set()
        self.wait_for(lambda: self.app.jobs[job['id']]['completed'] == 1)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'paused')
        self.source.chapter.assert_called_once()
        self.assertFalse(self.app.store.content('book', 1)['downloaded'])
        with self.assertRaises(ValueError):
            self.app.download('book')
        self.app.job_action(job['id'], 'resume')
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'completed')
        self.assertEqual(self.app.jobs[job['id']]['completed'], 2)

    def test_cancel_preserves_inflight_chapter_and_retry_fetches_only_the_rest(self):
        entered, release = self.block_first_chapter()
        job = self.app.download('book')
        self.assertTrue(entered.wait(2))
        self.app.job_action(job['id'], 'cancel')
        release.set()
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'cancelled')
        self.source.chapter.assert_called_once()
        self.assertTrue(self.app.store.content('book', 0)['downloaded'])
        self.assertFalse(self.app.store.content('book', 1)['downloaded'])
        retry = self.app.job_action(job['id'], 'retry')
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[retry['id']]['status'], 'completed')
        self.assertEqual(self.source.chapter.call_args_list,
                         [call('full', url) for url in self.payloads])
        self.assertEqual(self.fetch.call_count, 2)

    def test_cancel_paused_job_does_not_require_resume(self):
        entered, release = self.block_first_chapter()
        job = self.app.download('book')
        self.assertTrue(entered.wait(2))
        self.app.job_action(job['id'], 'pause')
        release.set()
        self.wait_for(lambda: self.app.jobs[job['id']]['completed'] == 1)
        self.app.job_action(job['id'], 'cancel')
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'cancelled')
        self.source.chapter.assert_called_once()

    def test_cancel_during_final_inflight_chapter_is_not_reported_complete(self):
        entered, release = self.block_first_chapter()
        job = self.app.download('book', count=1)
        self.assertTrue(entered.wait(2))
        self.app.job_action(job['id'], 'cancel')
        release.set()
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'cancelled')

    def test_cancel_queued_job_never_fetches_a_chapter(self):
        release = self.event()
        entered = [self.event(), self.event()]

        def occupy_worker(started):
            started.set()
            if not release.wait(3):
                raise TimeoutError('Test did not release a busy worker.')

        for event in entered:
            self.app.executor.submit(occupy_worker, event)
        self.assertTrue(all(event.wait(2) for event in entered))
        job = self.app.download('book')
        self.assertEqual(self.app.jobs[job['id']]['status'], 'queued')
        self.app.job_action(job['id'], 'cancel')
        release.set()
        self.futures[-1].result(timeout=3)
        self.assertEqual(self.app.jobs[job['id']]['status'], 'cancelled')
        self.assertEqual(self.app.jobs[job['id']]['completed'], 0)
        self.source.chapter.assert_not_called()


if __name__ == '__main__':
    unittest.main()
