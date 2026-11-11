from test_server import read_image
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from server import App

PNG = b'\x89PNG\r\n\x1a\n' + b'synthetic image'
URLS = [f'https://cdn.example/{name}.png' for name in ('a', 'b', 'c')]


class ImageCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.app = App(temporary.name)
        self.addCleanup(self.app.close)
        self.app._asset_stop.set()
        self.app._asset_worker.join(2)
        self.store = self.app.store
        self.store.upsert_book({'id': 'online', 'name': 'Online', 'source': 'full'},
                              [{'title': 'One', 'html': '<p>Cached body.</p>', 'images': URLS}])
        self.index()
        while self.store.inventory_assets():
            pass

    def index(self):
        while not self.store.asset_index_ready():
            if not self.store.index_assets():
                self.fail('Asset index could not finish')

    def path(self, url):
        return self.app.image_dir / hashlib.sha256(url.encode()).hexdigest()

    def cache(self, url, at, offline_owner=None):
        with patch('server.safe_fetch_bytes', return_value=PNG), patch('library.time.time', return_value=at):
            return read_image(self.app, url, offline_owner=offline_owner)

    def collect(self):
        self.index()
        for _ in range(100):
            queued = self.store.queue_cache_evictions()
            cleaned = self.store.cleanup_files()
            if not queued and not cleaned:
                break
        else:
            self.fail('Cache maintenance did not finish bounded work')

    def test_quota_skips_new_transient_writes_and_lru_reuses_recent_images(self):
        self.store.set_image_cache_limit(2 * len(PNG))
        self.cache(URLS[0], 100)
        self.cache(URLS[1], 200)
        self.cache(URLS[0], 300)
        self.assertEqual(self.cache(URLS[2], 400), (PNG, 'image/png'))
        self.assertFalse(self.path(URLS[2]).exists(), 'An over-budget response was stored anyway')
        self.collect()
        self.assertTrue(self.path(URLS[0]).exists())
        self.assertFalse(self.path(URLS[1]).exists(), 'The least recently used image was not evicted')
        self.cache(URLS[2], 500)
        stats = self.store.image_cache_stats()
        self.assertEqual((stats['total_bytes'], stats['cache_bytes'], stats['protected_bytes']), (2 * len(PNG), 2 * len(PNG), 0))

    def test_offline_shared_pins_survive_clear_trash_and_restart_until_last_owner_is_removed(self):
        for book_id in ('offline', 'other-offline'):
            self.store.upsert_book({'id': book_id, 'name': book_id, 'source': 'full'},
                                  [{'title': 'One', 'html': '<p>Body.</p>', 'images': [URLS[0]]}])
        self.index()
        self.cache(URLS[0], 100, ('offline', 0))
        self.cache(URLS[0], 200, ('other-offline', 0))
        self.cache(URLS[1], 300)
        self.store.trash_book('offline')
        self.store.set_image_cache_limit(0)
        self.store.clear_image_cache()
        self.collect()
        self.assertEqual((self.path(URLS[0]).exists(), self.path(URLS[1]).exists()), (True, False))
        self.store.purge_book('offline')
        self.collect()
        self.assertTrue(self.path(URLS[0]).exists())
        self.app.close()
        from library import Store
        self.store = Store(self.store.data_dir)
        self.assertEqual(self.store.image_cache_stats()['protected_bytes'], len(PNG))
        self.store.trash_book('other-offline')
        self.store.purge_book('other-offline')
        self.collect()
        self.assertFalse(self.path(URLS[0]).exists(), 'An ownerless offline pin left the shared cache permanently protected')

    def test_active_leases_and_late_hits_cancel_stale_automatic_evictions(self):
        self.cache(URLS[0], 100)
        self.cache(URLS[1], 200)
        self.store.set_image_cache_limit(len(PNG))
        self.store.queue_cache_evictions()
        with self.store.db() as db:
            stale = db.execute('SELECT * FROM asset_gc_candidates WHERE evict=1').fetchone()
        self.cache(URLS[0], 300)
        with self.store.db() as db:
            self.store._cleanup_candidate(stale, db)
        self.assertTrue(self.path(URLS[0]).exists())
        self.store.clear_image_cache()
        with self.store.use_asset(self.path(URLS[0])):
            self.collect()
            self.assertTrue(self.path(URLS[0]).exists())
        with self.store.db() as db:
            db.execute('UPDATE asset_gc_candidates SET retry_at=0')
        self.collect()
        self.assertFalse(self.path(URLS[0]).exists())

    def test_existing_unclassified_images_are_protected_and_inventory_is_not_repeated(self):
        old = self.path(URLS[0])
        old.write_bytes(PNG)
        with self.store.db() as db:
            db.execute('UPDATE asset_index_state SET inventory_images=0')
        while self.store.inventory_assets(limit=1):
            pass
        self.store.set_image_cache_limit(0)
        self.store.clear_image_cache()
        with patch('os.scandir', side_effect=AssertionError('Repeated directory inventory')), patch('library.parse_html', side_effect=AssertionError('Cache cleanup parsed chapter HTML')):
            self.collect()
            self.assertEqual(self.store.inventory_assets(), 0)
        self.assertTrue(old.exists())
        stats = self.store.image_cache_stats()
        self.assertEqual((stats['legacy_bytes'], stats['protected_bytes'], stats['inventory_complete']), (len(PNG), len(PNG), True))

    def test_limit_validation_and_zero_budget_do_not_disable_image_responses(self):
        for value in (-1, True, 1.5, '10', 65 * 1024 ** 3):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.set_image_cache_limit(value)
        self.store.set_image_cache_limit(0)
        self.assertEqual(self.cache(URLS[0], 100), (PNG, 'image/png'))
        self.assertFalse(self.path(URLS[0]).exists())

    def test_eviction_finishing_during_a_new_offline_fetch_keeps_its_pin(self):
        self.cache(URLS[0], 100)
        self.store.upsert_book({'id': 'offline', 'name': 'Offline', 'source': 'full'},
                              [{'title': 'One', 'html': '<p>Body.</p>', 'images': [URLS[0]]}])
        self.index()
        self.store.set_image_cache_limit(0)
        self.store.queue_cache_evictions()
        deleting, finish_delete, fetching, finish_fetch = (threading.Event() for _ in range(4))
        unlink = Path.unlink
        def blocked_unlink(path, *args, **kwargs):
            if '.gc-' in path.name:
                deleting.set()
                if not finish_delete.wait(4):
                    raise TimeoutError('GC was not released')
            return unlink(path, *args, **kwargs)
        def blocked_fetch(*args, **kwargs):
            fetching.set()
            if not finish_fetch.wait(4):
                raise TimeoutError('Image fetch was not released')
            return PNG
        errors = []
        def download():
            try:
                read_image(self.app, URLS[0], offline_owner=('offline', 0))
            except Exception as error:
                errors.append(error)
        with patch.object(Path, 'unlink', blocked_unlink), patch('server.safe_fetch_bytes', side_effect=blocked_fetch):
            collector = threading.Thread(target=self.store.cleanup_files)
            loader = threading.Thread(target=download)
            collector.start()
            try:
                self.assertTrue(deleting.wait(3))
                loader.start()
                self.assertTrue(fetching.wait(3))
                finish_delete.set()
                collector.join(3)
                with self.store.db() as db:
                    row = db.execute('SELECT protected FROM image_cache WHERE name=?', (self.path(URLS[0]).name,)).fetchone()
                self.assertIsNotNone(row, 'Deletion removed metadata owned by the new fetch')
                self.assertTrue(row['protected'])
            finally:
                finish_delete.set(); finish_fetch.set()
                collector.join(4)
                if loader.ident:
                    loader.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(self.path(URLS[0]).read_bytes(), PNG)
        self.assertEqual(self.store.image_cache_stats()['protected_bytes'], len(PNG))

    def test_download_pins_html_and_original_images_and_reads_them_after_offline_restart(self):
        self.store.upsert_book({'id': 'download', 'name': 'Download', 'source': 'full'},
                              [{'title': 'One', 'html': f'<p>Original<img src="{URLS[0]}"></p>', 'text': 'Original'}])
        self.store.edit_content('download', 0, {'html': f'<p>Edited<img src="{URLS[1]}"></p>', 'text': 'Edited'})
        self.store.set_image_cache_limit(0)
        with patch('server.safe_fetch_bytes', return_value=PNG) as fetch:
            job = self.app.download('download')
            for _ in range(300):
                saved = self.store.download_job(job['id'])
                if saved['status'] in ('completed', 'failed'):
                    break
                time.sleep(.01)
            self.assertEqual(saved['status'], 'completed', saved.get('error'))
            self.assertEqual({call.args[0] for call in fetch.call_args_list}, set(URLS[:2]))
        self.store.clear_image_cache(); self.collect()
        self.app.close()
        self.app = App(self.store.data_dir)
        self.addCleanup(self.app.close)
        with patch('server.safe_fetch_bytes', side_effect=AssertionError('Network used for an offline image')):
            self.assertEqual(self.app.chapter('download', 0)['text'], 'Edited')
            for url in URLS[:2]:
                self.assertEqual(read_image(self.app, url), (PNG, 'image/png'))
        self.assertEqual(self.app.store.image_cache_stats()['protected_bytes'], 2 * len(PNG))

    def test_imported_file_images_are_offline_even_when_transient_cache_is_disabled(self):
        self.store.upsert_book({'id': 'file', 'name': 'Imported', 'source': 'file'},
                              [{'title': 'One', 'html': '<p>File.</p>', 'images': [URLS[0]]}])
        self.index()
        self.store.set_image_cache_limit(0)
        self.cache(URLS[0], 100)
        self.store.clear_image_cache(); self.collect()
        self.assertTrue(self.path(URLS[0]).exists())
        self.assertEqual(self.store.image_cache_stats()['protected_bytes'], len(PNG))

    def test_clear_includes_images_already_selected_for_automatic_eviction(self):
        self.cache(URLS[0], 100); self.cache(URLS[1], 200)
        self.store.set_image_cache_limit(len(PNG)); self.store.queue_cache_evictions()
        self.store.clear_image_cache(); self.store.queue_cache_evictions()
        self.cache(URLS[0], 300)
        self.collect()
        self.assertEqual(self.store.image_cache_stats()['cache_bytes'], 0)

    def test_inventory_between_replace_and_accounting_cannot_protect_a_new_transient_image(self):
        with self.store.db() as db:
            db.execute('UPDATE asset_index_state SET inventory_images=0')
        replace = Path.replace
        def inventory_after_replace(path, target):
            result = replace(path, target)
            while self.store.inventory_assets():
                pass
            return result
        with patch.object(Path, 'replace', inventory_after_replace):
            self.cache(URLS[0], 100)
        self.assertEqual(self.store.image_cache_stats()['legacy_bytes'], 0)
        self.store.set_image_cache_limit(0); self.store.clear_image_cache(); self.collect()
        self.assertFalse(self.path(URLS[0]).exists())

    def test_late_import_pin_survives_completion_of_a_previously_claimed_eviction(self):
        self.cache(URLS[0], 100)
        self.store.set_image_cache_limit(0); self.store.queue_cache_evictions()
        deleting, finish = threading.Event(), threading.Event()
        unlink = Path.unlink
        def blocked_unlink(path, *args, **kwargs):
            if '.gc-' in path.name:
                deleting.set()
                if not finish.wait(4):
                    raise TimeoutError('Deletion was not released')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', blocked_unlink):
            collector = threading.Thread(target=self.store.cleanup_files)
            collector.start()
            try:
                self.assertTrue(deleting.wait(3))
                self.store.upsert_book({'id': 'import', 'name': 'Import', 'source': 'file', 'cover': URLS[0]})
            finally:
                finish.set(); collector.join(4)
        with self.store.db() as db:
            row = db.execute('SELECT protected,size FROM image_cache WHERE name=?', (self.path(URLS[0]).name,)).fetchone()
        self.assertIsNotNone(row, 'Eviction erased ownership acquired during its slow unlink')
        self.assertTrue(row['protected'])
        self.assertEqual(row['size'], 0, 'An already claimed/deleted file was counted as available offline')
        self.cache(URLS[0], 200)
        self.store.clear_image_cache(); self.collect()
        self.assertTrue(self.path(URLS[0]).exists(), 'Refetch did not retain the newly acquired protection')

    def test_parallel_fetches_do_not_over_admit_cache_bytes(self):
        self.store.set_image_cache_limit(len(PNG))
        barrier = threading.Barrier(2)
        def fetch(*args, **kwargs):
            barrier.wait(3)
            return PNG
        errors = []
        def load(url):
            try:
                read_image(self.app, url)
            except Exception as error:
                errors.append(error)
        with patch('server.safe_fetch_bytes', side_effect=fetch):
            threads = [threading.Thread(target=load, args=(url,)) for url in URLS[:2]]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(4)
        self.assertEqual(errors, [])
        self.assertEqual(self.store.image_cache_stats()['cache_bytes'], len(PNG))
        self.assertEqual(sum(self.path(url).exists() for url in URLS[:2]), 1)

    def test_failed_optional_cache_write_still_returns_image_but_offline_download_fails(self):
        write = Path.write_bytes
        def full_disk(path, value):
            if path.suffix == '.tmp':
                raise OSError('Disk full')
            return write(path, value)
        with patch.object(Path, 'write_bytes', full_disk), self.assertLogs(level='WARNING'):
            self.assertEqual(self.cache(URLS[0], 100), (PNG, 'image/png'))
        self.assertEqual(self.store.image_cache_stats()['total_bytes'], 0)
        with patch.object(Path, 'write_bytes', full_disk), self.assertRaisesRegex(OSError, 'Disk full'):
            self.cache(URLS[0], 200, ('online', 0))

    def test_crash_between_reservation_and_file_commit_recovers_without_phantom_usage(self):
        script = """
import os,sys
from pathlib import Path
from library import Store
store=Store(sys.argv[1]);path=Path(sys.argv[2])
reservation=store.cache_image_write(path,23);assert reservation.__enter__()
if sys.argv[3]=='written': path.write_bytes(b'\\x89PNG\\r\\n\\x1a\\n'+b'synthetic image')
os._exit(0)
"""
        for mode, url in zip(('missing', 'written'), URLS):
            with self.subTest(mode=mode):
                result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, str(self.store.data_dir), str(self.path(url)), mode], capture_output=True, encoding='utf-8', timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)
                from library import Store
                self.store = Store(self.store.data_dir)
                self.store.cleanup_files()
                self.assertEqual(self.store.image_cache_stats()['total_bytes'], 0 if mode == 'missing' else len(PNG))
                self.assertEqual(self.store.image_cache_stats()['legacy_bytes'], 0)


class ImageCacheScaleTests(unittest.TestCase):
    def test_hundred_thousand_metadata_entries_use_bounded_lru_and_constant_time_stats(self):
        from library import Store
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            with store.db() as db:
                db.execute('UPDATE asset_index_state SET complete=1,inventory_files=1,inventory_images=1')
                db.executemany('INSERT INTO image_cache(name,size,last_used,legacy) VALUES(?,16384,?,?)',
                               ((hashlib.sha256(str(i).encode()).hexdigest(), i, int(i % 2 == 0)) for i in range(100000)))
            store.set_image_cache_limit(64 * 1024 ** 2)
            steps = []
            original_db = store.db
            import contextlib
            @contextlib.contextmanager
            def measured_db():
                with original_db() as db:
                    db.set_progress_handler(lambda: steps.append(1) or 0, 1000)
                    yield db
            started = time.perf_counter()
            with patch.object(store, 'db', measured_db), patch('os.scandir', side_effect=AssertionError('Repeated cache scan')), patch('library.parse_html', side_effect=AssertionError('Cache parsed chapter bodies')):
                stats = store.image_cache_stats()
                self.assertEqual(store.queue_cache_evictions(), 32)
                elapsed = (time.perf_counter() - started) * 1000
            self.assertEqual((stats['total_bytes'], stats['protected_bytes']), (100000 * 16384, 50000 * 16384))
            self.assertLess(len(steps), 30, 'Cache selection or statistics scanned the whole metadata table')
            with store.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM asset_gc_candidates WHERE evict=1').fetchone()[0], 32)
                plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN SELECT * FROM image_cache WHERE protected=0 AND evicting=0 AND size>0 ORDER BY last_used,id LIMIT 32'))
                self.assertIn('image_cache_lru', plan)
                self.assertNotIn('TEMP B-TREE', plan)
            print(json.dumps({'cache_metadata_entries': 100000, 'physical_image_files': 0, 'cached_chapter_bodies': 0,
                              'stats_and_select_32_ms': elapsed, 'sqlite_vm_steps_upper_bound': (len(steps) + 1) * 1000}))


if __name__ == '__main__':
    unittest.main()
