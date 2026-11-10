import hashlib
import contextlib
from pathlib import Path
import stat
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode

from library import Store, TRASH_RETENTION
from server import App
from source_errors import SourceError


class AssetCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = Store(self.root)
        self.store.index_assets()
        for folder in ('files', 'images'):
            (self.root / folder).mkdir()

    def upload(self, data=b'uploaded book'):
        path = self.root / 'files' / (hashlib.sha256(data).hexdigest() + '.epub')
        self.store.queue_assets((path,))
        path.write_bytes(data)
        return path

    def image(self, url):
        path = self.root / 'images' / hashlib.sha256(url.encode()).hexdigest()
        self.store.queue_assets((path,))
        path.write_bytes(b'cached image')
        return path

    def book(self, book_id, path='', cover='', chapters=None):
        return self.store.upsert_book({'id': book_id, 'name': book_id, 'source': 'file',
                                      'path': str(path), 'cover': cover}, chapters)

    def purge(self, book_id):
        self.store.trash_book(book_id)
        self.store.purge_book(book_id)
        self.collect()

    def collect(self, store=None):
        store = store or self.store
        while not store.asset_index_ready() and store.index_assets():
            pass
        if not store.asset_index_ready():
            return store.cleanup_files()
        while store.inventory_assets():
            pass
        while store.cleanup_files():
            pass
        return store.asset_index_ready()

    def test_soft_delete_keeps_assets_until_the_last_shared_book_is_permanently_deleted(self):
        path = self.upload()
        cover = self.image('https://cdn.example/shared.jpg')
        self.book('first', path, 'https://cdn.example/shared.jpg')
        self.book('second', path, 'https://cdn.example/shared.jpg')
        self.store.trash_book('second')
        self.store.trash_book('first')
        self.assertTrue(path.exists())
        self.assertTrue(cover.exists())
        self.store.purge_book('first')
        self.collect()
        self.assertEqual(path.read_bytes(), b'uploaded book')
        self.assertEqual(cover.read_bytes(), b'cached image')
        self.store.purge_book('second')
        self.collect()
        self.assertFalse(path.exists())
        self.assertFalse(cover.exists())

    def test_purge_collects_old_orphans_but_only_inside_app_owned_asset_names(self):
        upload = self.upload()
        image = self.image('https://cdn.example/old.jpg')
        original = self.root / 'original.epub'
        original.write_bytes(b'personal original')
        manual = self.root / 'files' / 'keep-my-file.epub'
        manual.write_bytes(b'personal file')
        folder = self.root / 'images' / ('a' * 64)
        folder.mkdir()
        (folder / 'keep.txt').write_text('keep', encoding='utf-8')
        self.book('victim', original)
        self.purge('victim')
        self.assertFalse(upload.exists())
        self.assertFalse(image.exists())
        self.assertTrue(original.exists())
        self.assertTrue(manual.exists())
        self.assertTrue((folder / 'keep.txt').exists())

    def test_covers_chapter_images_html_and_original_html_all_retain_cache(self):
        urls = ['https://cdn.example/' + name + '.jpg?a=1&b=2' for name in ('cover', 'page', 'html', 'original')]
        paths = [self.image(url) for url in urls]
        self.book('keep', cover=urls[0], chapters=[
            {'title': 'One', 'images': ['/api/image?' + urlencode({'url': urls[1]})],
             'html': '<p><img src="' + urls[2].replace('&', '&amp;') + '"></p>'},
            {'title': 'Two', 'html': '<p><img src="' + urls[3].replace('cdn.example', 'CDN.example').replace('&', '&amp;') + '#fragment"></p>'}])
        self.store.edit_content('keep', 1, {'html': '<p>Edited.</p>', 'text': 'Edited.'})
        self.book('victim')
        self.purge('victim')
        self.assertTrue(all(path.exists() for path in paths))
        self.store.restore_content('keep', 1)
        self.assertIn('original.jpg', self.store.content('keep', 1)['html'])

    def test_expiry_frees_assets_but_respects_active_download_exclusion(self):
        expired, active = self.upload(b'expired'), self.upload(b'active')
        self.book('expired', expired)
        self.book('active', active)
        with patch('library.time.time', return_value=2_000_000_000):
            self.store.trash_book('expired')
            self.store.trash_book('active')
        self.assertEqual(self.store.purge_expired(now=2_000_000_000 + TRASH_RETENTION, skip_ids={'active'}), ['expired'])
        self.collect()
        self.assertFalse(expired.exists())
        self.assertTrue(active.exists())

    def test_sqlite_rollback_never_removes_assets(self):
        path = self.upload()
        self.book('keep', path)
        self.store.trash_book('keep')
        purge = self.store._purge
        def fail(db, book_id):
            purge(db, book_id)
            raise RuntimeError('rollback')
        with patch.object(self.store, '_purge', side_effect=fail), self.assertRaises(RuntimeError):
            self.store.purge_book('keep')
        self.assertTrue(path.exists())
        self.assertEqual(self.store.get_book('keep')['path'], str(path))

    def test_unverifiable_references_abort_cleanup_without_undoing_book_deletion(self):
        orphan = self.upload()
        self.book('keep', chapters=[{'text': 'Saved.'}])
        with self.store.db() as db:
            db.execute("UPDATE chapters SET images='invalid JSON' WHERE book_id='keep'")
        self.book('victim')
        with self.assertLogs('library', level='WARNING'):
            self.purge('victim')
        self.assertTrue(orphan.exists())
        with self.assertRaises(KeyError):
            self.store.get_book('victim')
        with self.store.db() as db:
            db.execute("UPDATE chapters SET images='[]' WHERE book_id='keep'")
        self.collect()
        self.assertFalse(orphan.exists())

    def test_busy_asset_does_not_block_other_cleanup_and_can_be_retried(self):
        busy, free = self.upload(b'busy'), self.upload(b'free')
        self.book('victim')
        rename = Path.rename
        def remove(path, *args, **kwargs):
            if path == busy:
                raise PermissionError('in use')
            return rename(path, *args, **kwargs)
        with patch.object(Path, 'rename', remove), self.assertLogs('library', level='WARNING'):
            self.purge('victim')
        self.assertTrue(busy.exists())
        self.assertFalse(free.exists())
        self.store.cleanup_file(busy)
        self.assertFalse(busy.exists())

    def test_reparse_directory_and_link_entries_are_not_followed(self):
        path = self.upload()
        self.book('victim')
        lstat = Path.lstat
        def inspect(path, *args, **kwargs):
            if path == self.root / 'files':
                return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=0x400)
            return lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', inspect):
            self.purge('victim')
        self.assertTrue(path.exists())
        self.book('another')
        def link(path, *args, **kwargs):
            if path == self.root / 'files' / path.name and path.name.endswith('.epub'):
                return SimpleNamespace(st_mode=stat.S_IFLNK, st_file_attributes=0)
            return lstat(path, *args, **kwargs)
        with patch.object(Path, 'lstat', link):
            self.purge('another')
        self.assertTrue(path.exists())

    def test_nested_active_leases_and_image_temporary_files_are_protected(self):
        path = self.upload()
        cache = self.image('https://cdn.example/active.jpg')
        temporary = cache.with_suffix('.' + 'b' * 32 + '.tmp')
        self.store.queue_assets((temporary,))
        temporary.write_bytes(b'in progress')
        with self.store.use_asset(path), self.store.use_asset(cache):
            with self.store.use_asset(path):
                pass
            self.store.cleanup_files()
            self.assertTrue(path.exists())
            self.assertTrue(cache.exists())
            self.assertTrue(temporary.exists())
        with patch('library.time.time', return_value=time.time() + 2):
            while self.store.cleanup_files():
                pass
        self.assertFalse(path.exists())
        self.assertFalse(cache.exists())
        self.assertFalse(temporary.exists())

    def test_purge_during_import_does_not_delete_the_unregistered_upload(self):
        app = App(self.root)
        self.addCleanup(app.close)
        app.store.upsert_book({'id': 'victim', 'name': 'victim'})
        entered, release = threading.Event(), threading.Event()
        result, errors = [], []
        def parse(path):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test timed out')
            self.assertEqual(path.read_bytes(), b'in-flight book')
            return {'book': {'name': 'Imported'}, 'chapters': [{'text': 'Saved text.'}]}
        def run():
            try:
                result.append(app.import_file('book.epub', b'in-flight book'))
            except Exception as error:
                errors.append(error)
        with patch('server.documents.import_document', side_effect=parse):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                app.trash_book('victim')
                app.purge_book('victim')
                app.store.cleanup_files()
            finally:
                release.set()
                worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(Path(result[0]['path']).read_bytes(), b'in-flight book')

    def test_failed_import_does_not_remove_another_imports_shared_upload(self):
        app = App(self.root)
        self.addCleanup(app.close)
        first_entered, second_entered, first_done = threading.Event(), threading.Event(), threading.Event()
        calls = iter((1, 2))
        errors, results = [], []
        def parse(path):
            if next(calls) == 1:
                first_entered.set()
                if not second_entered.wait(5):
                    raise RuntimeError('second import timed out')
                raise ValueError('first import failed')
            second_entered.set()
            if not first_done.wait(5):
                raise RuntimeError('first import did not finish')
            self.assertEqual(path.read_bytes(), b'shared import')
            return {'book': {'name': 'Shared'}, 'chapters': [{'text': 'Saved.'}]}
        def run(first):
            try:
                results.append(app.import_file('book.epub', b'shared import'))
            except Exception as error:
                errors.append(str(error))
            finally:
                if first:
                    first_done.set()
        with patch('server.documents.import_document', side_effect=parse):
            a = threading.Thread(target=run, args=(True,))
            a.start()
            self.assertTrue(first_entered.wait(2))
            b = threading.Thread(target=run, args=(False,))
            b.start()
            a.join(6)
            b.join(6)
        self.assertFalse(a.is_alive() or b.is_alive())
        self.assertEqual(errors, ['first import failed'])
        self.assertEqual(Path(results[0]['path']).read_bytes(), b'shared import')

    def test_inflight_image_is_not_collected_while_network_is_pending(self):
        app = App(self.root)
        self.addCleanup(app.close)
        app.store.upsert_book({'id': 'victim', 'name': 'victim'})
        entered, release = threading.Event(), threading.Event()
        errors, results = [], []
        url = 'https://cdn.example/loading.jpg'
        png = b'\x89PNG\r\n\x1a\nsynthetic image'
        def fetch(*args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('network test timed out')
            return png
        def run():
            try:
                results.append(app.image(url))
            except Exception as error:
                errors.append(error)
        with patch('server.safe_fetch_bytes', side_effect=fetch):
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                cache = self.image(url)
                app.trash_book('victim')
                app.purge_book('victim')
                app.store.cleanup_files()
                self.assertTrue(cache.exists())
            finally:
                release.set()
                worker.join(6)
        self.assertEqual(errors, [])
        self.assertEqual(results[0], (png, 'image/png'))

    def test_refreshed_cover_is_protected_until_its_new_url_is_saved(self):
        app = App(self.root)
        self.addCleanup(app.close)
        old, new = 'https://cdn.example/dead.jpg', 'https://cdn.example/new.jpg'
        app.store.upsert_book({'id': 'keep', 'name': 'keep', 'source': 'hako',
                               'path': 'https://docln.sbs/truyen/1-book', 'cover': old})
        self.collect(app.store)
        cache = app.image_dir / hashlib.sha256(new.encode()).hexdigest()
        update = app.store.update_book
        def save(book_id, changes):
            app.store.cleanup_files()
            self.assertTrue(cache.exists())
            return update(book_id, changes)
        png = b'\x89PNG\r\n\x1a\nsynthetic image'
        with patch('server.safe_fetch_bytes', side_effect=[SourceError('old cover failed'), png]), \
             patch.object(app.sources, 'cover', return_value={'cover': new, 'path': 'https://docln.sbs/truyen/1-book'}), \
             patch.object(app.store, 'update_book', side_effect=save):
            self.assertEqual(app.cover('keep'), (png, 'image/png'))
        self.collect(app.store)
        self.assertEqual(cache.read_bytes(), png)

    def test_indexed_cleanup_does_not_read_or_parse_unrelated_chapter_bodies(self):
        self.book('keep', chapters=[{'html': '<p>Unrelated body.</p>'} for _ in range(2000)])
        while self.store.index_assets():
            pass
        self.store.progress('keep', 0, .5)
        self.assertTrue(self.store.asset_index_ready())
        orphan = self.upload()
        self.book('victim')
        db_context = self.store.db
        @contextlib.contextmanager
        def checked_db():
            with db_context() as db:
                def authorize(action, table, column, *_):
                    import sqlite3
                    if action == sqlite3.SQLITE_READ and table == 'chapters' and column in ('html', 'original_html', 'images'):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.set_authorizer(authorize)
                yield db
        with patch.object(self.store, 'db', checked_db), patch('library.parse_html', side_effect=AssertionError('HTML must not be parsed during indexed cleanup')):
            self.purge('victim')
        self.assertFalse(orphan.exists())

    def test_background_index_parsing_does_not_hold_sqlite_or_asset_locks(self):
        self.book('keep', chapters=[{'html': '<p>Saved.</p>', 'text': 'Saved.'}])
        entered, release, writable = threading.Event(), threading.Event(), threading.Event()
        from htmltools import parse_html
        def parse(markup):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('parser timed out')
            return parse_html(markup)
        def write():
            self.store.progress('keep', 0, .25)
            with self.store.use_asset(self.root / 'files' / ('c' * 64 + '.epub')):
                writable.set()
        with patch('library.parse_html', side_effect=parse):
            indexer = threading.Thread(target=self.store.index_assets)
            indexer.start()
            try:
                self.assertTrue(entered.wait(2))
                writer = threading.Thread(target=write)
                writer.start()
                self.assertTrue(writable.wait(1), 'Parsing blocked progress or asset registration')
            finally:
                release.set()
                indexer.join(6)
                if 'writer' in locals():
                    writer.join(6)

    def test_slow_file_unlink_does_not_block_writes_or_asset_registration(self):
        self.book('keep', chapters=[{'text': 'Saved.'}])
        orphan = self.upload()
        entered, release, writable = threading.Event(), threading.Event(), threading.Event()
        unlink = Path.unlink
        def slow(path, *args, **kwargs):
            if '.gc-' in path.name:
                entered.set()
                if not release.wait(5):
                    raise RuntimeError('unlink timed out')
            return unlink(path, *args, **kwargs)
        def write():
            self.store.progress('keep', 0, .3)
            with self.store.use_asset(self.root / 'files' / ('c' * 64 + '.epub')):
                writable.set()
        with patch.object(Path, 'unlink', slow):
            collector = threading.Thread(target=self.store.cleanup_files)
            collector.start()
            try:
                self.assertTrue(entered.wait(2))
                writer = threading.Thread(target=write)
                writer.start()
                self.assertTrue(writable.wait(1), 'File unlink held the database or asset lock')
            finally:
                release.set()
                collector.join(6)
                if 'writer' in locals():
                    writer.join(6)
        self.assertFalse(orphan.exists())
        self.assertEqual(list((self.root / 'files').iterdir()), [])

    def test_content_changed_during_indexing_cannot_install_stale_references(self):
        old = self.image('https://cdn.example/old.jpg')
        new = self.image('https://cdn.example/new.jpg')
        self.book('keep', chapters=[{'html': '<p><img src="https://cdn.example/old.jpg"></p>'}])
        from htmltools import parse_html
        changed = False
        def parse(markup):
            nonlocal changed
            if not changed:
                changed = True
                self.store.save_content('keep', 0, {'html': '<p><img src="https://cdn.example/new.jpg"></p>'})
            return parse_html(markup)
        with patch('library.parse_html', side_effect=parse):
            self.collect()
        self.assertFalse(old.exists())
        self.assertTrue(new.exists())

    def test_legacy_index_migration_is_incremental_safe_and_persistent(self):
        cache = self.image('https://cdn.example/keep.jpg')
        orphan = self.upload()
        self.book('keep', chapters=[{'html': '<p><img src="https://cdn.example/keep.jpg"></p>'}])
        with self.store.db() as db:
            names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'asset_%'")]
            for name in names:
                db.execute('DROP TRIGGER "' + name + '"')
            db.execute('DROP TABLE asset_pending')
            db.execute('DROP TABLE asset_refs')
        upgraded = Store(self.root)
        self.assertFalse(upgraded.asset_index_ready())
        with self.assertLogs('library', level='WARNING'):
            self.assertFalse(upgraded.cleanup_files())
        self.assertTrue(cache.exists() and orphan.exists())
        while upgraded.index_assets(limit=1):
            pass
        self.assertTrue(upgraded.asset_index_ready())
        with patch('library.parse_html', side_effect=AssertionError('Index must survive restart')):
            reopened = Store(self.root)
            while reopened.inventory_assets():
                pass
            while reopened.cleanup_files():
                pass
        self.assertTrue(cache.exists())
        self.assertFalse(orphan.exists())

    def test_app_delete_returns_while_legacy_index_is_building_then_collects_in_background(self):
        self.book('keep', chapters=[{'html': '<p>Saved.</p>', 'text': 'Saved.'}])
        self.book('victim')
        orphan = self.upload()
        entered, release = threading.Event(), threading.Event()
        from htmltools import parse_html
        def parse(markup):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('parser timed out')
            return parse_html(markup)
        with patch('library.parse_html', side_effect=parse):
            app = App(self.root)
            self.addCleanup(app.close)
            try:
                self.assertTrue(entered.wait(2))
                app.trash_book('victim')
                self.assertTrue(app.purge_book('victim')['deleted'])
                self.assertTrue(orphan.exists(), 'Collection started before migration was complete')
                app.store.progress('keep', 0, .2)
            finally:
                release.set()
            deadline = time.monotonic() + 4
            while orphan.exists() and time.monotonic() < deadline:
                time.sleep(.02)
        self.assertFalse(orphan.exists())

    def test_malformed_cover_does_not_rollback_readable_book_import(self):
        with self.assertLogs('library', level='WARNING'):
            self.book('keep', cover='http://[', chapters=[{'text': 'Readable text.'}])
        self.assertEqual(self.store.content('keep', 0)['text'], 'Readable text.')
        self.assertFalse(self.store.asset_index_ready())
        self.store.update_book('keep', {'cover': 'https://cdn.example/fixed.jpg'})
        self.collect()
        self.assertTrue(self.store.asset_index_ready())

    def test_shutdown_is_bounded_and_cancels_remaining_index_owners(self):
        self.book('keep', chapters=[{'html': '<p>Saved.</p>'} for _ in range(32)])
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        from htmltools import parse_html
        calls = []
        def parse(markup):
            calls.append(markup)
            entered.set()
            if not release.wait(5):
                raise RuntimeError('parser timed out')
            return parse_html(markup)
        with patch('library.parse_html', side_effect=parse):
            app = App(self.root)
            def close():
                app.close()
                closed.set()
            try:
                self.assertTrue(entered.wait(2))
                closer = threading.Thread(target=close)
                closer.start()
                self.assertTrue(closed.wait(1.5), 'Shutdown waited for a blocked maintenance operation')
            finally:
                release.set()
                app._asset_worker.join(6)
                if 'closer' in locals():
                    closer.join(6)
                else:
                    app.close()
        self.assertEqual(len(calls), 1)

    def test_restart_resumes_persistent_candidates_after_process_exit(self):
        orphan = self.upload()
        self.book('victim')
        self.store.trash_book('victim')
        self.store.purge_book('victim')
        self.assertTrue(orphan.exists())
        app = App(self.root)
        self.addCleanup(app.close)
        deadline = time.monotonic() + 3
        while orphan.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(orphan.exists())

    def test_finishing_one_candidate_does_not_erase_a_new_file_generation(self):
        path = self.upload()
        entered, release = threading.Event(), threading.Event()
        unlink = Path.unlink
        def slow(path, *args, **kwargs):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('unlink timed out')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', slow):
            worker = threading.Thread(target=self.store.cleanup_files)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.store.queue_assets((path,))
                path.write_bytes(b'new generation')
            finally:
                release.set()
                worker.join(6)
        self.assertEqual(path.read_bytes(), b'new generation')
        reopened = Store(self.root)
        self.assertGreater(reopened.cleanup_files(), 0)
        self.assertFalse(path.exists())

    def test_normal_delete_only_collects_affected_candidates_without_directory_scan(self):
        affected = self.image('https://cdn.example/affected.jpg')
        untouched = self.image('https://cdn.example/untouched.jpg')
        self.book('victim', cover='https://cdn.example/affected.jpg')
        self.book('keep', cover='https://cdn.example/untouched.jpg')
        self.collect()
        self.store.trash_book('victim')
        with patch('os.scandir', side_effect=AssertionError('Normal GC scanned the library')), \
             patch.object(Path, 'iterdir', side_effect=AssertionError('Normal GC enumerated assets')):
            self.store.purge_book('victim')
            while self.store.cleanup_files():
                pass
        self.assertFalse(affected.exists())
        self.assertTrue(untouched.exists())

    def test_unindexed_book_deletion_does_not_lose_created_image_candidates(self):
        app = App(self.root)
        self.addCleanup(app.close)
        app._asset_stop.set()
        app._asset_worker.join(2)
        self.collect(app.store)
        url = 'https://cdn.example/not-indexed.jpg'
        app.store.upsert_book({'id': 'victim', 'name': 'victim'},
                             [{'html': '<p><img src="' + url + '"></p>'}])
        png = b'\x89PNG\r\n\x1a\nsynthetic image'
        with patch('server.safe_fetch_bytes', return_value=png):
            app.image(url)
        path = app.image_dir / hashlib.sha256(url.encode()).hexdigest()
        with app.store.db() as db:
            self.assertTrue(db.execute('SELECT 1 FROM asset_gc_candidates WHERE kind=? AND name=?',
                                       ('images', path.name)).fetchone())
        app.trash_book('victim')
        app.purge_book('victim')
        self.collect(app.store)
        self.assertFalse(path.exists())

    def test_candidate_enqueue_rolls_back_with_book_deletion(self):
        path = self.upload()
        self.book('keep', path)
        self.collect()
        self.store.trash_book('keep')
        purge = self.store._purge
        def fail(db, book_id):
            purge(db, book_id)
            raise RuntimeError('rollback')
        with patch.object(self.store, '_purge', side_effect=fail), self.assertRaises(RuntimeError):
            self.store.purge_book('keep')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM asset_gc_candidates').fetchone()[0], 0)
        self.assertTrue(path.exists())

    def test_crash_after_rename_before_commit_recovers_without_directory_scan(self):
        path = self.upload()
        rename = Path.rename
        def crash(source, target):
            rename(source, target)
            raise sqlite3.OperationalError('simulated process interruption')
        with patch.object(Path, 'rename', crash), self.assertRaises(sqlite3.OperationalError):
            self.store.cleanup_files()
        self.assertFalse(path.exists())
        reopened = Store(self.root)
        with patch('os.scandir', side_effect=AssertionError('Recovery scanned directories')):
            self.assertGreater(reopened.cleanup_files(), 0)
        self.assertEqual(list((self.root / 'files').iterdir()), [])

    def test_locked_staged_file_remains_queued_and_is_retried_after_restart(self):
        path = self.upload()
        with patch.object(Path, 'unlink', side_effect=PermissionError('locked')), self.assertLogs('library', level='WARNING'):
            self.store.cleanup_files()
        self.assertFalse(path.exists())
        staged = list((self.root / 'files').glob('*.gc-*'))
        self.assertEqual(len(staged), 1)
        self.assertEqual(staged[0].read_bytes(), b'uploaded book')
        reopened = Store(self.root)
        with patch('library.time.time', return_value=time.time() + 31):
            self.assertGreater(reopened.cleanup_files(), 0)
        self.assertEqual(list((self.root / 'files').iterdir()), [])

    def test_inventory_replays_a_batch_when_its_database_transaction_fails(self):
        orphans = [self.root / 'images' / hashlib.sha256(str(i).encode()).hexdigest() for i in range(2)]
        for path in orphans:
            path.write_bytes(b'legacy orphan')
        self.store.inventory_assets(limit=1)  # Complete the empty files folder first.
        with self.store.db() as db:
            db.execute("CREATE TRIGGER fail_inventory BEFORE INSERT ON asset_gc_candidates BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.inventory_assets(limit=1)
        with self.store.db() as db:
            db.execute('DROP TRIGGER fail_inventory')
        self.collect()
        self.assertTrue(all(not path.exists() for path in orphans))

    def test_path_refresh_replaces_an_existing_pending_chapter_token(self):
        self.book('keep', 'https://cdn.example/old/', chapters=[{'html': '<p><img src="page.jpg"></p>'}])
        with self.store.db() as db:
            old = db.execute('SELECT id FROM asset_pending WHERE book_id=? AND idx=0', ('keep',)).fetchone()[0]
            db.execute('UPDATE asset_pending SET blocked=1 WHERE book_id=? AND idx=0', ('keep',))
            db.execute('DROP TRIGGER asset_book_path')
            db.execute('CREATE TRIGGER asset_book_path AFTER UPDATE OF path ON books WHEN NEW.path IS NOT OLD.path BEGIN INSERT OR REPLACE INTO asset_pending(book_id,idx) SELECT book_id,idx FROM chapters WHERE book_id=NEW.id; END')
        self.store = Store(self.root)  # Upgrade the trigger from an existing library.
        self.book('keep', 'https://cdn.example/new/')
        with self.store.db() as db:
            new = db.execute('SELECT id,blocked FROM asset_pending WHERE book_id=? AND idx=0', ('keep',)).fetchone()
        self.assertGreater(new['id'], old)
        self.assertEqual(new['blocked'], 0)
        self.collect()
        with self.store.db() as db:
            names = {row[0] for row in db.execute("SELECT name FROM asset_refs WHERE kind='images'")}
        self.assertEqual(names, {hashlib.sha256(b'https://cdn.example/new/page.jpg').hexdigest()})

    def test_upgrade_inventories_legacy_orphans_once_and_preserves_shared_assets(self):
        live = self.image('https://cdn.example/live.jpg')
        self.book('keep', cover='https://cdn.example/live.jpg')
        self.collect()
        orphan = self.root / 'files' / ('e' * 64 + '.epub')
        orphan.write_bytes(b'old orphan')
        with self.store.db() as db:
            db.execute('DROP TRIGGER asset_ref_gc')
            db.execute('DROP TABLE asset_gc_candidates')
            for column in ('inventory_files', 'inventory_images'):
                db.execute('ALTER TABLE asset_index_state DROP COLUMN ' + column)
            db.execute('ALTER TABLE asset_index_state ADD COLUMN cleanup_requested INTEGER NOT NULL DEFAULT 4')
            db.execute('ALTER TABLE asset_index_state ADD COLUMN cleanup_completed INTEGER NOT NULL DEFAULT 1')
            db.execute('CREATE TRIGGER asset_book_delete AFTER DELETE ON books BEGIN UPDATE asset_index_state SET cleanup_requested=cleanup_requested+1 WHERE id=1; END')
        upgraded = Store(self.root)
        self.collect(upgraded)
        self.assertFalse(orphan.exists())
        self.assertTrue(live.exists())
        with patch('os.scandir', side_effect=AssertionError('Repeated legacy inventory')):
            self.collect(Store(self.root))
