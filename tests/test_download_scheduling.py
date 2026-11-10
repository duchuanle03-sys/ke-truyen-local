import tempfile
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from server import App
from source_errors import SourceCaptchaError, SourceError


class SchedulingSource:
    def __init__(self):
        self.captcha_enabled = False
        self.calls = []
        self.entered = {name: threading.Event() for name in ('a', 'b')}
        self.release = threading.Event()
        self.verifications = {}
        for name in ('a', 'b'):
            self.verifications[name] = {
                'captcha': {'source': 'stv', 'id': name}, 'event': threading.Event(), 'verified': False}

    def chapter(self, kind, url):
        name, index = url.rstrip('/').split('/')[-2:]
        self.calls.append((name, index))
        if name in self.entered and index == '0':
            self.entered[name].set()
            if self.captcha_enabled:
                verification = self.verifications[name]
                if not verification['verified']:
                    raise SourceCaptchaError(verification['captcha']['id'], verification)
            elif not self.release.wait(5):
                raise TimeoutError('Test did not release a chapter')
        return {'html': '<p>Saved text</p>', 'text': 'Saved text', 'images': []}

    def captcha(self, kind, action, challenge_id, answer=None):
        verification = next(v for v in self.verifications.values() if v['captcha']['id'] == challenge_id)
        if action == 'verify':
            if answer != 'manual-answer':
                raise SourceError('Synthetic verification failed', code='captcha_failed')
            verification['verified'] = True
            verification['event'].set()
            return {'verified': True}
        if action == 'cancel':
            verification['event'].set()
            return {'cancelled': True}
        raise AssertionError('Unexpected synthetic CAPTCHA action')

    def close(self):
        self.release.set()


class DownloadSchedulingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.source = SchedulingSource()
        self.app = App(self.temp.name, self.source)
        self.addCleanup(self.app.close)
        for name in ('a', 'b', 'c'):
            self.app.store.upsert_book({'id': name, 'name': name, 'source': 'full'},
                                      [{'title': str(i), 'url': f'https://example.com/{name}/{i}'} for i in range(2)])

    def wait_for(self, condition):
        deadline = time.monotonic() + 2
        while not condition():
            if time.monotonic() >= deadline:
                self.fail('Independent queued work did not advance')
            time.sleep(.01)

    def completed(self, job):
        return self.app.store.download_job(job['id'])['status'] == 'completed'

    def test_two_paused_jobs_release_workers_for_another_book(self):
        first, second = self.app.download('a'), self.app.download('b')
        self.assertTrue(all(event.wait(2) for event in self.source.entered.values()))
        for job in (first, second):
            self.app.job_action(job['id'], 'pause')
        third = self.app.download('c')
        self.source.release.set()
        self.wait_for(lambda: all(self.app.store.download_job(job['id'])['completed'] == 1 for job in (first, second)))
        self.wait_for(lambda: self.completed(third))
        for job in (first, second):
            saved = self.app.store.download_job(job['id'])
            self.assertEqual((saved['status'], saved['completed']), ('paused', 1))
        resumed = self.app.job_action(first['id'], 'resume')
        self.assertEqual(resumed['id'], first['id'])
        self.wait_for(lambda: self.completed(first))
        self.assertEqual(self.source.calls.count(('a', '0')), 1)
        self.assertEqual(self.source.calls.count(('a', '1')), 1)

    def test_shared_captcha_releases_both_workers_until_manual_verification(self):
        self.source.captcha_enabled = True
        self.source.verifications['b'] = self.source.verifications['a']
        first, second = self.app.download('a'), self.app.download('b')
        self.wait_for(lambda: all(self.app.jobs[job['id']]['status'] == 'verification_required' for job in (first, second)))
        third = self.app.download('c')
        self.wait_for(lambda: self.completed(third))
        with self.assertRaises(SourceError):
            self.app.captcha('stv', 'verify', 'a', 'wrong-answer')
        for job in (first, second):
            self.assertEqual(self.app.store.download_job(job['id'])['status'], 'verification_required')
        self.assertEqual(self.source.calls.count(('a', '0')), 1)
        self.assertEqual(self.source.calls.count(('b', '0')), 1)
        self.app.captcha('stv', 'verify', 'a', 'manual-answer')
        self.wait_for(lambda: self.completed(first) and self.completed(second))
        self.assertEqual(self.source.calls.count(('a', '0')), 2)
        self.assertEqual(self.source.calls.count(('b', '0')), 2)

    def test_verifying_one_session_does_not_release_another_session(self):
        self.source.captcha_enabled = True
        first, second = self.app.download('a'), self.app.download('b')
        self.wait_for(lambda: all(self.app.jobs[job['id']]['status'] == 'verification_required' for job in (first, second)))
        third = self.app.download('c')
        self.wait_for(lambda: self.completed(third))
        self.app.captcha('stv', 'verify', 'a', 'manual-answer')
        self.wait_for(lambda: self.completed(first))
        self.assertEqual(self.app.store.download_job(second['id'])['status'], 'verification_required')
        self.assertFalse(self.source.verifications['b']['verified'])
        self.app.job_action(second['id'], 'cancel')
        self.wait_for(lambda: self.app.store.download_job(second['id'])['status'] == 'cancelled')

    def test_resume_during_worker_handoff_does_not_lose_or_duplicate_job(self):
        returned, release = threading.Event(), threading.Event()
        original = self.app._download
        def handoff(job):
            original(job)
            if job['status'] == 'paused':
                returned.set()
                if not release.wait(4):
                    raise TimeoutError('Test did not release worker handoff')
        with patch.object(self.app, '_download', side_effect=handoff):
            job = self.app.download('a')
            self.assertTrue(self.source.entered['a'].wait(2))
            self.app.job_action(job['id'], 'pause')
            self.source.release.set()
            try:
                self.assertTrue(returned.wait(2), 'Pause kept the worker waiting instead of returning')
                self.app.job_action(job['id'], 'resume')
            finally:
                release.set()
            self.wait_for(lambda: self.completed(job))
        self.assertEqual(self.source.calls, [('a', '0'), ('a', '1')])

    def test_database_failure_keeps_the_verified_session_until_requeue_commits(self):
        self.source.captcha_enabled = True
        self.source.verifications['b'] = self.source.verifications['a']
        first, second = self.app.download('a'), self.app.download('b')
        self.wait_for(lambda: all(self.app.store.download_job(job['id'])['status'] == 'verification_required' for job in (first, second)))
        for future in tuple(self.app._download_futures.values()):
            future.result(timeout=2)
        with self.app.lock:
            self.source.captcha('stv', 'verify', 'a', 'manual-answer')
            with self.app.store.db() as db:
                db.execute("CREATE TRIGGER fail_requeue BEFORE UPDATE ON download_jobs WHEN NEW.status='queued' AND NEW.book_id='b' BEGIN SELECT RAISE(ABORT,'injected'); END")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    self.app._schedule_downloads()
                for job in (first, second):
                    self.assertEqual(self.app.store.download_job(job['id'])['status'], 'verification_required')
                    self.assertEqual(self.app.jobs[job['id']]['status'], 'verification_required')
            finally:
                with self.app.store.db() as db:
                    db.execute('DROP TRIGGER fail_requeue')
            self.app._schedule_downloads()
        self.wait_for(lambda: self.completed(first) and self.completed(second))

    def test_verification_rollback_does_not_expose_an_active_job_as_deletable(self):
        self.source.captcha_enabled = True
        job = self.app.download('a')
        self.wait_for(lambda: self.app.store.download_job(job['id'])['status'] == 'verification_required')
        future = self.app._download_futures.get(job['id'])
        if future:
            future.result(timeout=2)
        with self.app.lock:
            self.source.captcha('stv', 'cancel', 'a')
            with self.app.store.db() as db:
                db.execute("CREATE TRIGGER fail_verification BEFORE UPDATE ON download_jobs WHEN NEW.status='failed' BEGIN SELECT RAISE(ABORT,'injected'); END")
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    self.app._schedule_downloads()
                self.assertEqual(self.app.jobs[job['id']]['status'], 'verification_required')
                with self.assertRaises(ValueError):
                    self.app.job_action(job['id'], 'delete')
            finally:
                with self.app.store.db() as db:
                    db.execute('DROP TRIGGER fail_verification')
            self.app._schedule_downloads()
        self.assertEqual(self.app.store.download_job(job['id'])['status'], 'failed')
        self.app.job_action(job['id'], 'delete')
        third = self.app.download('c')
        self.wait_for(lambda: self.completed(third))

    def test_cancelling_one_waiting_job_preserves_the_shared_challenge(self):
        self.source.captcha_enabled = True
        self.source.verifications['b'] = self.source.verifications['a']
        first, second = self.app.download('a'), self.app.download('b')
        self.wait_for(lambda: all(self.app.store.download_job(job['id'])['status'] == 'verification_required' for job in (first, second)))
        self.app.job_action(first['id'], 'cancel')
        self.assertFalse(self.source.verifications['b']['event'].is_set())
        self.app.captcha('stv', 'verify', 'a', 'manual-answer')
        self.wait_for(lambda: self.completed(second))
        self.assertEqual(self.app.store.download_job(first['id'])['status'], 'cancelled')
        self.assertFalse(self.app.store.content('a', 0)['downloaded'])

    def test_pause_during_a_challenge_preserves_pause_after_verification(self):
        self.source.captcha_enabled = True
        original = self.source.chapter
        def delayed(kind, url):
            self.source.entered['a'].set()
            if not self.source.release.wait(4):
                raise TimeoutError('Test did not release CAPTCHA request')
            return original(kind, url)
        self.source.chapter = delayed
        job = self.app.download('a')
        self.assertTrue(self.source.entered['a'].wait(2))
        self.app.job_action(job['id'], 'pause')
        self.source.release.set()
        self.wait_for(lambda: self.app.list_jobs()['jobs'][0]['captcha'] is not None)
        self.assertEqual(self.app.store.download_job(job['id'])['status'], 'paused')
        self.app.captcha('stv', 'verify', 'a', 'manual-answer')
        self.wait_for(lambda: self.app.list_jobs()['jobs'][0]['captcha'] is None)
        self.assertEqual(self.app.store.download_job(job['id'])['status'], 'paused')
        self.assertEqual(self.app.job_action(job['id'], 'resume')['id'], job['id'])
        self.wait_for(lambda: self.completed(job))


if __name__ == '__main__':
    unittest.main()
