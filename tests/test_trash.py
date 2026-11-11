import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from library import Store
from server import make_server

DAY = 86400


class TrashStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        for book_id in ('first', 'other'):
            self.store.upsert_book({'id': book_id, 'name': book_id, 'source': 'file'},
                [{'title': 'One', 'url': '', 'text': 'Saved text', 'html': '<p>Saved text</p>'}])
        self.store.progress('first', 0, .65)
        self.store.save_note({'id': 'keep', 'book_id': 'first', 'note': 'Personal note'})
        self.store.save_note({'id': 'orphan', 'book_id': 'missing', 'note': 'Unrelated note'})

    def test_remove_restore_preserves_content_progress_and_notes(self):
        with patch('library.time.time', return_value=2_000_000_000):
            book = self.store.trash_book('first')
        self.assertEqual(book['trash_expires_at'], 2_000_000_000 + 30 * DAY)
        self.assertEqual([b['id'] for b in self.store.list_books()['items']], ['other'])
        self.assertEqual([b['id'] for b in self.store.list_books(trashed=True)['items']], ['first'])
        self.assertEqual(self.store.content('first', 0)['text'], 'Saved text')
        self.assertEqual(self.store.notes('first')[0]['note'], 'Personal note')
        with patch('library.time.time', return_value=2_000_000_000 + DAY):
            self.assertEqual(self.store.trash_book('first')['trashed_at'], 2_000_000_000)
            restored = self.store.restore_book('first')
        self.assertEqual(restored['trashed_at'], 0)
        self.assertEqual(restored['position'], .65)
        self.assertEqual(len(self.store.list_books()['items']), 2)

    def test_permanent_delete_requires_trash_and_only_removes_matching_data(self):
        with self.assertRaises(ValueError):
            self.store.purge_book('first')
        self.store.trash_book('first')
        self.store.purge_book('first')
        with self.assertRaises(KeyError):
            self.store.get_book('first')
        self.assertEqual(self.store.get_book('other')['name'], 'other')
        self.assertEqual(self.store.notes('first'), [])
        self.assertEqual(self.store.notes()[0]['id'], 'orphan')
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chapters WHERE book_id='first'").fetchone()[0], 0)

    def test_expiry_boundary_and_active_download_exclusion(self):
        with patch('library.time.time', return_value=2_000_000_000):
            self.store.trash_book('first')
        self.assertEqual(self.store.purge_expired(now=2_000_000_000 + 30 * DAY - 1), [])
        with patch('library.time.time', return_value=2_000_000_000 + 30 * DAY):
            with self.assertRaises(ValueError):
                self.store.restore_book('first')
        self.assertEqual(self.store.purge_expired(now=2_000_000_000 + 30 * DAY, skip_ids={'first'}), [])
        self.assertEqual(self.store.purge_expired(now=2_000_000_000 + 30 * DAY), ['first'])
        self.assertEqual([b['id'] for b in self.store.list_books()['items']], ['other'])

    def test_upgrade_keeps_existing_books_and_reading_state(self):
        with self.store.db() as db:
            if 'trashed_at' in {r[1] for r in db.execute('PRAGMA table_info(books)')}:
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'books_shelf_%'").fetchall():
                    db.execute('DROP INDEX ' + row[0])
                db.execute('ALTER TABLE books DROP COLUMN trashed_at')
        upgraded = Store(self.tmp.name)
        self.assertEqual(upgraded.get_book('first')['trashed_at'], 0)
        self.assertEqual(upgraded.get_book('first')['position'], .65)
        self.assertEqual(len(upgraded.list_books()['items']), 2)


class TrashHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.server = make_server(self.tmp.name, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.server.app.store.upsert_book({'id': 'test-only', 'name': 'Temporary test'},
                                         [{'title': 'One', 'url': ''}])

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.tmp.cleanup()

    def request(self, path, method='GET', origin=None):
        req = Request(self.base + path, data=b'{}' if method == 'POST' else None,
                      headers={'Content-Type': 'application/json'} | ({'Origin': origin} if origin else {}), method=method)
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            return response.status, json.load(response)

    def test_remove_restore_and_permanent_delete_through_api(self):
        self.assertEqual(self.request('/api/books/test-only', 'DELETE')[0], 400)
        self.assertEqual(self.request('/api/books/test-only/trash', 'POST')[0], 200)
        self.assertEqual(self.request('/api/books')[1]['items'], [])
        self.assertEqual(len(self.request('/api/books?trashed=1')[1]['items']), 1)
        self.assertEqual(self.request('/api/books/test-only/restore', 'POST')[0], 200)
        self.assertEqual(len(self.request('/api/books')[1]['items']), 1)
        self.request('/api/books/test-only/trash', 'POST')
        self.assertEqual(self.request('/api/books/test-only', 'DELETE')[0], 200)
        self.assertEqual(self.request('/api/books?trashed=1')[1]['items'], [])
        self.assertEqual(self.request('/api/books/test-only')[0], 404)

    def test_cross_origin_cannot_remove_or_permanently_delete(self):
        self.assertEqual(self.request('/api/books/test-only/trash', 'POST', 'https://evil.example')[0], 403)
        self.assertEqual(self.request('/api/books/test-only')[1]['trashed_at'], 0)
        self.request('/api/books/test-only/trash', 'POST')
        self.assertEqual(self.request('/api/books/test-only', 'DELETE', 'https://evil.example')[0], 403)
        self.assertEqual(len(self.request('/api/books?trashed=1')[1]['items']), 1)

    def test_removal_cancels_only_matching_jobs_and_blocks_new_downloads(self):
        app = self.server.app
        app.store.upsert_book({'id': 'other', 'name': 'Other'}, [{'text': 'Already saved'}])
        entered = {key: threading.Event() for key in ('test-only', 'other')}
        release = threading.Event()
        original = app._chapter_content
        def block(book, index):
            entered[book['id']].set()
            if not release.wait(3):
                raise TimeoutError('Test did not release a download')
            return original(book, index)
        with patch.object(app, '_chapter_content', side_effect=block):
            one, two = app.download('test-only'), app.download('other')
            try:
                self.assertTrue(all(event.wait(2) for event in entered.values()))
                self.request('/api/books/test-only/trash', 'POST')
                self.assertTrue(app.jobs[one['id']]['cancelled'])
                self.assertFalse(app.jobs[two['id']]['cancelled'])
                self.assertEqual(self.request('/api/books/test-only', 'DELETE')[0], 400)
                with self.assertRaises(ValueError):
                    app.download('test-only')
            finally:
                release.set()
                for future in tuple(app._download_futures.values()):
                    future.result(timeout=3)
        self.assertEqual(self.request('/api/books/test-only', 'DELETE')[0], 200)

    def test_maintenance_removes_expired_trash_while_app_runs(self):
        with patch('library.time.time', return_value=time.time() - 31 * DAY):
            self.server.app.store.trash_book('test-only')
        self.server.app._next_trash_cleanup = 0
        self.server.service_actions()
        self.assertEqual(self.request('/api/books/test-only')[0], 404)

    def test_expired_trash_is_cleaned_on_next_start(self):
        from server import App
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.upsert_book({'id': 'expired', 'name': 'Temporary expired test'})
            with patch('library.time.time', return_value=time.time() - 31 * DAY):
                store.trash_book('expired')
            app = App(directory)
            try:
                self.assertEqual(app.store.list_books(trashed=True)['items'], [])
                with self.assertRaises(KeyError):
                    app.store.get_book('expired')
            finally:
                app.close()

    def test_explicit_file_reimport_returns_book_to_shelf(self):
        app = self.server.app
        book = app.import_file('only-test.txt', b'Saved test document')
        app.store.progress(book['id'], 0, .8)
        app.store.trash_book(book['id'])
        restored = app.import_file('only-test.txt', b'Saved test document')
        self.assertEqual(restored['trashed_at'], 0)
        self.assertEqual(restored['position'], .8)

    def test_explicit_source_add_restores_existing_book_without_resetting_progress(self):
        app = self.server.app
        url = 'https://docln.sbs/truyen/1-test'
        chapters = [{'title': 'One', 'url': 'https://docln.sbs/c1'}]
        app.store.upsert_book({'id': 'source-test', 'source': 'hako', 'path': url, 'name': 'Test'}, chapters)
        app.store.progress('source-test', 0, .4)
        app.store.trash_book('source-test')
        with patch.object(app.sources, 'detail', return_value={'source': 'hako', 'path': url, 'name': 'Test', 'chapters': chapters}):
            restored = app.add_source('hako', url)
        self.assertEqual(restored['id'], 'source-test')
        self.assertEqual(restored['trashed_at'], 0)
        self.assertEqual(restored['position'], .4)
