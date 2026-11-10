from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from library import Store
from server import App, ROOT


CRASH_WORKER = r'''
from pathlib import Path
import sys, time
from server import App
root = Path(sys.argv[1])
class Source:
    def chapter(self, source, url):
        index = int(url.rsplit('/', 1)[1])
        if index == 17:
            (root/'blocked').write_text('ready')
            while True:
                time.sleep(.05)
        return {'html': '<p>Saved chapter</p>', 'text': 'Saved chapter', 'images': []}
app = App(root, Source())
job = app.download('book', start=3, count=30)
(root/'job-id').write_text(job['id'])
while True:
    time.sleep(.05)
'''


class GateSource:
    def __init__(self):
        self.release = threading.Event()
        self.calls = []

    def chapter(self, source, url):
        self.calls.append(url)
        if not self.release.wait(5):
            raise TimeoutError('Synthetic source was not released')
        return {'html': '<p>New chapter</p>', 'text': 'New chapter', 'images': []}

    def close(self):
        self.release.set()


class DownloadRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root)

    def book(self, book_id='book', count=2):
        self.store.upsert_book({'id': book_id, 'name': book_id, 'source': 'full'},
                              [{'url': f'https://example.com/{i}', 'title': str(i)} for i in range(count)])

    def job(self, book_id='book', status='queued', completed=0, cancelled=False):
        return {'id': 'job-' + book_id, 'book_id': book_id, 'name': book_id, 'start': 0, 'end': 2,
                'total': 2, 'completed': completed, 'status': status, 'cancelled': cancelled,
                'error': '', 'captcha': {'id': 'expired-session-challenge'}}

    def app(self, source=None):
        app = App(self.root, source or GateSource())
        self.addCleanup(app.close)
        return app

    def wait_for(self, condition):
        deadline = time.monotonic() + 8
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('Persistent download did not reach the expected state')
            time.sleep(.02)

    def test_forced_process_kill_resumes_same_range_and_reuses_saved_chapters(self):
        self.book(count=40)
        self.store.progress('book', 1, .65)
        process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', CRASH_WORKER, str(self.root)],
                                   cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        try:
            self.wait_for(lambda: (self.root / 'blocked').exists())
            job_id = (self.root / 'job-id').read_text()
            checkpoint = self.store.download_job(job_id)
            self.assertLessEqual(14 - checkpoint['completed'], 9)
            self.assertLess(checkpoint['completed'], 15)
            self.assertEqual(checkpoint['status'], 'running')
        finally:
            process.kill()
            process.wait(timeout=5)
        source = GateSource()
        source.release.set()
        app = self.app(source)
        self.wait_for(lambda: self.store.download_job(job_id)['status'] == 'completed')
        recovered = self.store.download_job(job_id)
        self.assertEqual((recovered['start'], recovered['end'], recovered['completed']), (3, 33, 30))
        self.assertEqual(source.calls, [f'https://example.com/{i}' for i in range(17, 33)])
        self.assertFalse(self.store.content('book', 2)['downloaded'])
        self.assertFalse(self.store.content('book', 33)['downloaded'])
        self.assertEqual(app.store.get_book('book')['position'], .65)

    def test_recovery_preserves_manual_pause_cancel_and_failure(self):
        for status in ('paused', 'cancelled', 'failed'):
            self.book(status)
            job = self.job(status, status, completed=1, cancelled=status == 'cancelled')
            self.store.create_download(job)
        self.book('cancel-requested')
        self.store.create_download(self.job('cancel-requested', 'running', cancelled=True))
        app = self.app()
        self.assertEqual(app.jobs, {})
        for status in ('paused', 'cancelled', 'failed'):
            self.assertEqual(self.store.download_job('job-' + status)['status'], status)
        self.assertEqual(self.store.download_job('job-cancel-requested')['status'], 'cancelled')
        self.assertEqual(app.sources.calls, [])
        app.job_action('job-failed', 'delete')
        with self.assertRaises(KeyError):
            self.store.download_job('job-failed')

    def test_stale_captcha_is_not_persisted_and_interrupted_job_restarts(self):
        self.book()
        self.store.create_download(self.job(status='verification_required'))
        with self.store.db() as db:
            payload = db.execute('SELECT data FROM download_jobs').fetchone()[0]
        self.assertNotIn('expired-session-challenge', payload)
        source = GateSource()
        app = self.app(source)
        self.wait_for(lambda: len(source.calls) == 1)
        self.assertIsNone(app.list_jobs()['jobs'][0]['captcha'])
        source.release.set()
        self.wait_for(lambda: self.store.download_job('job-book')['status'] == 'completed')

    def test_recovery_schedules_only_two_jobs_and_does_not_load_history(self):
        for i in range(20):
            book_id = 'book-' + str(i)
            self.book(book_id)
            self.store.create_download(self.job(book_id))
        for i in range(120):
            job = self.job(status='completed')
            job['id'] = 'history-' + str(i)
            job['book_id'] = 'book-0'
            self.store.create_download(job)
        source = GateSource()
        app = self.app(source)
        self.wait_for(lambda: len(source.calls) == 2)
        self.assertEqual(len(app._download_futures), 2)
        self.assertEqual(len(app.jobs), 2)
        self.assertEqual(len(self.store.queued_downloads(limit=30)), 18)
        first = app.list_jobs()
        second = app.list_jobs(first['next_offset'])
        self.assertEqual((len(first['jobs']), len(second['jobs'])), (50, 50))
        self.assertFalse({j['id'] for j in first['jobs']} & {j['id'] for j in second['jobs']})
        self.assertEqual(len(app.jobs), 2)

    def test_trashed_book_jobs_are_cancelled_before_startup_dispatch(self):
        self.book()
        self.store.create_download(self.job(status='running'))
        self.store.trash_book('book')
        app = self.app()
        self.assertEqual(self.store.download_job('job-book')['status'], 'cancelled')
        self.assertEqual(app.sources.calls, [])
        app.purge_book('book')
        with self.assertRaises(KeyError):
            self.store.download_job('job-book')

    def test_checkpoint_is_saved_before_shutdown_while_next_chapter_is_inflight(self):
        self.book()
        source = GateSource()
        source.release.set()
        original = source.chapter
        blocked, release = threading.Event(), threading.Event()
        def chapter(kind, url):
            if url.endswith('/1'):
                blocked.set()
                if not release.wait(5):
                    raise TimeoutError('Test chapter was not released')
            return original(kind, url)
        source.chapter = chapter
        app = self.app(source)
        job = app.download('book')
        try:
            self.assertTrue(blocked.wait(3))
            app.close()
            saved = self.store.download_job(job['id'])
            self.assertEqual(saved['completed'], 1)
            self.assertFalse(saved['cancelled'])
            self.assertEqual(saved['status'], 'running')
        finally:
            release.set()
            for future in app._download_futures.values():
                future.result(timeout=3)

    def test_old_shutdown_worker_cannot_overwrite_a_recovered_job(self):
        self.book()
        entered, release = threading.Event(), threading.Event()
        old_source = GateSource()
        def blocked(kind, url):
            entered.set()
            if not release.wait(5):
                raise TimeoutError('Test source was not released')
            raise OSError('Old request was closed')
        old_source.chapter = blocked
        old_app = self.app(old_source)
        job = old_app.download('book')
        try:
            self.assertTrue(entered.wait(2))
            old_app.close()
            new_source = GateSource()
            new_source.release.set()
            self.app(new_source)
            self.wait_for(lambda: self.store.download_job(job['id'])['status'] == 'completed')
        finally:
            release.set()
            for future in old_app._download_futures.values():
                future.result(timeout=3)
        saved = self.store.download_job(job['id'])
        self.assertEqual((saved['status'], saved['completed']), ('completed', 2))


if __name__ == '__main__':
    unittest.main()
