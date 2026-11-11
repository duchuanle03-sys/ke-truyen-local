import sqlite3
import subprocess
import sys
from pathlib import Path
import tempfile
import time
import threading
import unittest
from unittest.mock import patch

from library import Store
from server import App


class ChapterCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)

    def book(self, name, count=4, source='full', kind='text', bodies=False):
        self.store.upsert_book({'id': name, 'name': name, 'source': source, 'kind': kind},
            [{'title': f'Chapter {i}', 'url': f'https://example.com/{name}/{i}',
              **({'text': 'Imported body', 'html': '<p>Imported body</p>'} if bodies else {})} for i in range(count)])

    def save(self, name, index, offline=False):
        return self.store.save_content(name, index, {'text': f'needle {name} {index}', 'html': '<p>Body</p>', 'images': []}, offline=offline)

    def drain(self):
        for _ in range(100):
            if not self.store.maintain_chapter_cache(): break

    def test_global_lru_across_text_and_manga_preserves_metadata_and_search(self):
        self.book('novel'); self.book('manga', kind='comic')
        self.store.set_chapter_cache_limit(2)
        for i, key in enumerate((('novel', 0), ('manga', 0), ('novel', 1))):
            with patch('chapter_cache.time.time', return_value=100 + i): self.save(*key)
        with patch('chapter_cache.time.time', return_value=104): self.store.touch_chapter('novel', 0)
        self.store.progress('manga', 0, .4)
        self.store.save_note({'book_id': 'manga', 'chapter_index': 0, 'note': 'Keep me'})
        while self.store.index_search(): pass
        self.assertEqual(len(self.store.search_content('manga', 'needle')['results']), 1)
        self.drain()
        self.assertFalse(self.store.content('manga', 0)['downloaded'])
        self.assertEqual(self.store.content('manga', 0)['title'], 'Chapter 0')
        self.assertTrue(self.store.content('novel', 0)['downloaded'])
        self.assertEqual(self.store.search_content('manga', 'needle')['results'], [])
        self.assertEqual(self.store.get_book('manga')['position'], .4)
        self.assertEqual(len(self.store.notes('manga')), 1)
        with self.store.db() as db:
            self.assertFalse(db.execute("SELECT 1 FROM chapter_search_docs WHERE book_id='manga'").fetchone())
        self.assertEqual(self.store.chapter_cache_stats()['cache_count'], 2)

    def test_offline_import_edits_and_restorable_originals_survive_clear(self):
        self.book('online'); self.book('import', source='file', bodies=True)
        self.save('online', 0, offline=True); self.save('online', 1); self.save('online', 2)
        self.store.edit_content('online', 1, {'text': 'User edit', 'html': '<p>User edit</p>'})
        self.store.clear_chapter_cache(); self.drain()
        self.assertTrue(self.store.content('online', 0)['downloaded'])
        self.assertEqual(self.store.content('online', 1)['text'], 'User edit')
        self.assertFalse(self.store.content('online', 2)['downloaded'])
        self.assertTrue(self.store.content('import', 0)['downloaded'])
        self.store.restore_content('online', 1)
        self.store.clear_chapter_cache(); self.drain()
        self.assertEqual(self.store.content('online', 1)['text'], 'needle online 1')
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT offline FROM chapter_cache WHERE book_id='online' AND idx=0").fetchone()[0], 1)

    def test_inflight_and_active_reader_leases_protect_bodies(self):
        self.book('book'); self.save('book', 0); self.save('book', 1)
        self.store.set_chapter_cache_limit(0)
        with self.store.use_chapter('book', 0):
            self.store.reader_chapter('tab', 'book', 1)
            self.drain()
            self.assertTrue(self.store.content('book', 0)['downloaded'])
            self.assertTrue(self.store.content('book', 1)['downloaded'])
        self.store.reader_chapter('tab', None)
        self.drain()
        self.assertFalse(self.store.content('book', 0)['downloaded'])
        self.assertFalse(self.store.content('book', 1)['downloaded'])

    def test_candidate_revalidation_rejects_newer_protection_access_and_edits(self):
        self.book('book'); self.save('book', 0); self.save('book', 1); self.save('book', 2)
        self.store.set_chapter_cache_limit(0)
        candidates = self.store.chapter_cache_candidates(3)
        self.store.protect_chapter('book', 0)
        self.store.touch_chapter('book', 1)
        self.store.edit_content('book', 2, {'text': 'Changed', 'html': '<p>Changed</p>'})
        self.assertEqual(self.store.evict_chapters(candidates), 0)
        self.assertTrue(all(self.store.content('book', i)['downloaded'] for i in range(3)))

    def test_stale_fetch_cannot_overwrite_edit_or_newer_body(self):
        self.book('book'); first = self.save('book', 0)
        self.store.edit_content('book', 0, {'text': 'Edited', 'html': '<p>Edited</p>'})
        result = self.store.save_content('book', 0, {'text': 'Old response'}, expected_revision=first['revision'])
        self.assertEqual(result['text'], 'Edited')

    def test_eviction_limits_both_record_count_and_body_bytes_per_transaction(self):
        self.book('book')
        for i in range(3): self.store.save_content('book', i, {'text': 'x' * 150000})
        self.store.set_chapter_cache_limit(0)
        self.assertEqual(self.store.evict_chapters(self.store.chapter_cache_candidates(8)), 1)
        self.assertEqual(self.store.chapter_cache_stats()['cache_count'], 2)

    def test_raising_limit_after_candidate_selection_prevents_unnecessary_eviction(self):
        self.book('book'); self.save('book', 0); self.save('book', 1)
        self.store.set_chapter_cache_limit(0)
        candidates = self.store.chapter_cache_candidates(8)
        self.store.set_chapter_cache_limit(100)
        self.assertEqual(self.store.evict_chapters(candidates), 0)

    def test_import_reclassification_does_not_leave_protected_bodies_in_lru_queue(self):
        self.book('book'); self.save('book', 0)
        self.store.upsert_book({'id': 'book', 'name': 'Imported', 'source': 'file'})
        self.store.set_chapter_cache_limit(0)
        self.drain()
        self.assertTrue(self.store.content('book', 0)['downloaded'])
        self.assertEqual(self.store.chapter_cache_stats()['cache_count'], 0)
        self.assertEqual(self.store.chapter_cache_stats()['protected_count'], 1)

    def test_legacy_migration_and_opt_in_reclassification_resume_after_restart(self):
        self.book('book', bodies=True)
        # Simulate an old library with bodies but no reliable download-intent metadata.
        with self.store.db() as db:
            db.execute('DELETE FROM chapter_cache')
            db.execute("UPDATE chapter_cache_state SET complete=0,book_id='',idx=-1 WHERE id=1")
        self.store = Store(self.tmp.name)
        self.store.set_chapter_cache_limit(1); self.drain()
        stats = self.store.chapter_cache_stats()
        self.assertEqual((stats['legacy_count'], stats['cache_count']), (4, 0))
        self.assertTrue(all(self.store.content('book', i)['downloaded'] for i in range(4)))
        with self.assertRaises(ValueError): self.store.reclassify_legacy_chapters(False)
        self.store.reclassify_legacy_chapters(True)
        self.store = Store(self.tmp.name); self.drain()
        self.assertEqual(self.store.chapter_cache_stats()['legacy_count'], 0)
        self.assertEqual(self.store.chapter_cache_stats()['cache_count'], 1)

    def test_limits_validate_and_no_automatic_vacuum_mode_change(self):
        for value in (-1, 1.5, True, '50', 100001):
            with self.subTest(value=value), self.assertRaises(ValueError): self.store.set_chapter_cache_limit(value)
        self.assertEqual(self.store.chapter_cache_stats()['max_chapters'], 100)
        for value in (30, 50): self.assertEqual(self.store.set_chapter_cache_limit(value)['max_chapters'], value)
        with self.store.db() as db: self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 0)

    def test_delayed_reader_switch_and_close_cannot_replace_newer_lease(self):
        self.book('book'); self.save('book', 0); self.save('book', 1)
        self.store.reader_chapter('tab', 'book', 1, 3)
        self.store.reader_chapter('tab', 'book', 0, 2)
        self.assertEqual(self.store._reader_keys(), {('book', 1)})
        self.store.reader_chapter('tab', None, sequence=4)
        self.store.reader_chapter('tab', 'book', 1, 3)
        self.assertEqual(self.store._reader_keys(), set())

    def test_restart_restores_unexpired_reader_lease_without_scanning_bodies(self):
        self.book('book'); self.save('book', 0)
        self.store.reader_chapter('tab', 'book', 0, 3)
        self.store.set_chapter_cache_limit(0)
        self.store = Store(self.tmp.name)
        self.drain()
        self.assertTrue(self.store.content('book', 0)['downloaded'])
        self.store.reader_chapter('tab', None, sequence=4)
        self.store = Store(self.tmp.name)
        self.drain()
        self.assertFalse(self.store.content('book', 0)['downloaded'])

    def test_clear_and_reclassification_cannot_discard_newer_content(self):
        self.book('book'); self.save('book', 0)
        candidates = self.store.chapter_cache_candidates()
        self.store.clear_chapter_cache()
        self.save('book', 0)
        self.assertEqual(self.store.evict_chapters(candidates), 0)
        self.drain()
        self.assertTrue(self.store.content('book', 0)['downloaded'])

    def test_compaction_requires_confirmation_idle_and_free_space_and_keeps_mode(self):
        with self.assertRaises(ValueError): self.store.compact_database()
        self.book('book'); self.save('book', 0)
        self.store.reader_chapter('tab', 'book', 0)
        with self.assertRaises(ValueError): self.store.compact_database(True)
        self.store.reader_chapter('tab', None)
        with self.store.db():
            with self.assertRaises(ValueError): self.store.compact_database(True)
        with self.store.use_asset(self.store.data_dir / 'files' / ('a' * 64 + '.epub')):
            with self.assertRaises(ValueError): self.store.compact_database(True)
        with patch('chapter_cache.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            with self.assertRaises(ValueError): self.store.compact_database(True)
        self.assertEqual(self.store.compact_database(True)['auto_vacuum'], 0)
        self.assertTrue(self.store.content('book', 0)['downloaded'])

    def test_interrupted_eviction_rolls_back_and_persistent_clear_recovers(self):
        self.book('book'); self.save('book', 0); self.save('book', 1)
        while self.store.index_search(): pass
        self.store.clear_chapter_cache()
        script = '''
import contextlib, os, sys
from library import Store
store=Store(sys.argv[1])
rows=store.chapter_cache_candidates(8)
original=store.db
@contextlib.contextmanager
def interrupted():
    with original() as db:
        yield db
        os._exit(73)
store.db=interrupted
store.evict_chapters(rows)
'''
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script, self.tmp.name],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.assertEqual(result.returncode, 73, result.stderr)
        self.store = Store(self.tmp.name)
        self.assertTrue(self.store.content('book', 0)['downloaded'])
        self.assertEqual(len(self.store.search_content('book', 'needle')['results']), 2)
        self.drain()
        self.assertFalse(self.store.content('book', 0)['downloaded'])
        self.assertEqual(self.store.search_content('book', 'needle')['results'], [])

    def test_incremental_vacuum_only_runs_for_already_incremental_database(self):
        self.book('book'); self.save('book', 0)
        statements = []
        original = self.store.db
        from contextlib import contextmanager
        @contextmanager
        def trace():
            with original() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.store, 'db', trace):
            self.store.clear_chapter_cache(); self.drain()
        self.assertFalse(any(q.strip().upper().startswith(('VACUUM', 'PRAGMA INCREMENTAL_VACUUM')) for q in statements))
        with tempfile.TemporaryDirectory() as temporary:
            db = sqlite3.connect(Path(temporary) / 'library.sqlite3')
            db.execute('PRAGMA auto_vacuum=INCREMENTAL'); db.close()
            store = Store(temporary)
            with store.db() as db:
                self.assertEqual(db.execute('PRAGMA auto_vacuum').fetchone()[0], 2)
            store.upsert_book({'id': 'x', 'name': 'X', 'source': 'full'}, [{'url': 'https://example.com/0'}])
            store.save_content('x', 0, {'text': 'needle' * 3000})
            store.set_chapter_cache_limit(0)
            store.maintain_chapter_cache()
            self.assertFalse(store.content('x', 0)['downloaded'])


class ChapterCacheAppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.calls = []
        class Source:
            def chapter(source_self, source, url):
                self.calls.append(url)
                return {'html': '<p>Fetched needle</p>', 'text': 'Fetched needle', 'images': []}
        self.app = App(self.tmp.name, Source())
        self.addCleanup(self.app.close)
        self.app._asset_stop.set(); self.app._asset_worker.join(3)
        self.store = self.app.store
        self.store.upsert_book({'id': 'book', 'name': 'Book', 'source': 'full'},
            [{'title': str(i), 'url': f'https://example.com/{i}'} for i in range(3)])

    def drain(self):
        for _ in range(100):
            if not self.store.maintain_chapter_cache(): break

    def test_ordinary_read_refetches_after_eviction_but_live_tab_stays_safe(self):
        self.store.set_chapter_cache_limit(0)
        self.app.chapter('book', 0, 'tab', 1)
        self.drain()
        self.assertTrue(self.store.content('book', 0)['downloaded'])
        self.store.reader_chapter('tab', None, sequence=2)
        self.drain()
        self.assertFalse(self.store.content('book', 0)['downloaded'])
        self.app.chapter('book', 0)
        self.assertEqual(len(self.calls), 2)

    def test_offline_completion_and_restart_preserve_both_cached_and_new_bodies(self):
        self.app.chapter('book', 0)
        job = self.app.download('book', count=2)
        for _ in range(300):
            if self.store.download_job(job['id'])['status'] == 'completed': break
            time.sleep(.01)
        self.assertEqual(self.store.download_job(job['id'])['status'], 'completed')
        self.app.close()
        self.store = Store(self.tmp.name)
        self.store.set_chapter_cache_limit(0); self.drain()
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM chapter_cache WHERE offline=1 AND kind='protected'").fetchone()[0], 2)
        self.assertTrue(self.store.content('book', 0)['downloaded'])
        self.assertTrue(self.store.content('book', 1)['downloaded'])

    def test_inflight_fetch_and_edit_race_never_overwrites_newer_content(self):
        entered, release = threading.Event(), threading.Event()
        original = self.app.sources.chapter
        def blocked(*args):
            entered.set()
            if not release.wait(3): raise TimeoutError('Fetch was not released')
            return original(*args)
        result = []
        with patch.object(self.app.sources, 'chapter', side_effect=blocked):
            thread = threading.Thread(target=lambda: result.append(self.app.chapter('book', 0)))
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.store.save_content('book', 0, {'text': 'A newer fetch', 'html': '<p>A newer fetch</p>'})
                candidates = self.store.chapter_cache_candidates()
                self.assertEqual(candidates, [], 'In-flight fetch did not lease its chapter')
                self.store.edit_content('book', 0, {'text': 'User text', 'html': '<p>User text</p>'})
            finally:
                release.set(); thread.join(3)
        self.assertEqual(result[0]['text'], 'User text')
        self.assertEqual(self.store.content('book', 0)['original_text'], 'A newer fetch')


if __name__ == '__main__': unittest.main()
