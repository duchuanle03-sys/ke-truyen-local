import hashlib
from pathlib import Path
import stat
import tempfile
import threading
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
        for folder in ('files', 'images'):
            (self.root / folder).mkdir()

    def upload(self, data=b'uploaded book'):
        path = self.root / 'files' / (hashlib.sha256(data).hexdigest() + '.epub')
        path.write_bytes(data)
        return path

    def image(self, url):
        path = self.root / 'images' / hashlib.sha256(url.encode()).hexdigest()
        path.write_bytes(b'cached image')
        return path

    def book(self, book_id, path='', cover='', chapters=None):
        return self.store.upsert_book({'id': book_id, 'name': book_id, 'source': 'file',
                                      'path': str(path), 'cover': cover}, chapters)

    def purge(self, book_id):
        self.store.trash_book(book_id)
        self.store.purge_book(book_id)

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
        self.assertEqual(path.read_bytes(), b'uploaded book')
        self.assertEqual(cover.read_bytes(), b'cached image')
        self.store.purge_book('second')
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

    def test_busy_asset_does_not_block_other_cleanup_and_can_be_retried(self):
        busy, free = self.upload(b'busy'), self.upload(b'free')
        self.book('victim')
        unlink = Path.unlink
        def remove(path, *args, **kwargs):
            if path == busy:
                raise PermissionError('in use')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', remove), self.assertLogs('library', level='WARNING'):
            self.purge('victim')
        self.assertTrue(busy.exists())
        self.assertFalse(free.exists())
        self.store.cleanup_files()
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
        temporary.write_bytes(b'in progress')
        with self.store.use_asset(path), self.store.use_asset(cache):
            with self.store.use_asset(path):
                pass
            self.store.cleanup_files()
            self.assertTrue(path.exists())
            self.assertTrue(cache.exists())
            self.assertTrue(temporary.exists())
        self.store.cleanup_files()
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
        app.store.cleanup_files()
        self.assertEqual(cache.read_bytes(), png)
