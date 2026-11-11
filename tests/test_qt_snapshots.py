import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from unittest.mock import patch

from qt import QtDictionary


class QtSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.qt = QtDictionary(self.temp.name)
        self.addCleanup(self.qt.close)
        self.qt.import_text('vp', '甲=old\n乙=keep')
        self.qt.translate('甲乙', 'book')

    def test_book_edit_preserves_global_and_copies_only_changed_trie_path(self):
        global_snapshot = self.qt._cache['*']
        old = self.qt._compiled('*')
        self.qt.save('vp', '甲', 'private', 'book')
        self.assertIs(self.qt._cache['*'], global_snapshot)
        self.assertEqual(self.qt.translate('甲乙', 'book'), 'private keep')
        self.qt.save('vp', '甲', 'new')
        current = self.qt._compiled('*')
        self.assertIs(current[0][0]['乙'], old[0][0]['乙'])
        self.assertEqual(old[0][0]['甲'][None], 'old')
        self.assertEqual(self.qt.translate('甲'), 'new')
        self.qt.delete('vp', '甲')
        self.assertEqual(current[0][0]['甲'][None], 'new')

    def test_warm_readers_continue_during_bulk_snapshot_build(self):
        entered, release = threading.Event(), threading.Event()
        original = self.qt._build_scope
        def build(*args):
            entered.set()
            if not release.wait(3): raise TimeoutError('Build not released')
            return original(*args)
        with ThreadPoolExecutor(max_workers=2) as pool, patch.object(self.qt, '_build_scope', side_effect=build):
            writer = pool.submit(self.qt.import_text, 'vp', '甲=new')
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(pool.submit(self.qt.translate, '甲乙', 'book').result(.5), 'old keep')
            finally: release.set()
            writer.result(3)
        self.assertEqual(self.qt.translate('甲乙', 'book'), 'new keep')

    def test_one_chapter_keeps_its_snapshot_during_save_delete_and_rules_update(self):
        entered, release = threading.Event(), threading.Event()
        original = self.qt._compiled
        def snapshot(book):
            result = original(book)
            if threading.current_thread().name.startswith('reader'):
                entered.set()
                if not release.wait(3): raise TimeoutError('Reader not released')
            return result
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix='reader') as pool, patch.object(self.qt, '_compiled', side_effect=snapshot):
            reader = pool.submit(self.qt.translate, '甲乙' * 1000)
            try:
                self.assertTrue(entered.wait(2))
                self.qt.save('vp', '甲', 'new')
                self.qt.delete('vp', '乙')
                self.qt.save('rules', '甲乙', 'rule')
            finally: release.set()
            self.assertEqual(reader.result(3), ' '.join(['old keep'] * 1000))
        self.assertEqual(self.qt.translate('甲乙'), 'rule')

    def test_failed_sql_write_keeps_published_snapshots_and_generations(self):
        snapshot = self.qt._cache['*']
        with self.qt._db_guard:
            self.qt._db.execute("CREATE TRIGGER reject_write BEFORE INSERT ON entries BEGIN SELECT RAISE(ABORT,'synthetic rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.qt.save('vp', '甲', 'new')
        self.assertIs(self.qt._cache['*'], snapshot)
        self.assertEqual(self.qt.translate('甲'), 'old')

    def test_external_commit_during_build_cannot_publish_stale_snapshot(self):
        original = self.qt._build_scope
        changed = []
        def build(*args):
            result = original(*args)
            if not changed:
                changed.append(True)
                with closing(sqlite3.connect(self.qt.path)) as db, db:
                    db.execute("UPDATE entries SET meaning='external' WHERE word='甲'")
            return result
        with patch.object(self.qt, '_build_scope', side_effect=build): self.qt.import_text('vp', '甲=new')
        self.assertEqual(self.qt.translate('甲'), 'external')

    def test_shared_connection_is_never_used_by_warm_reader_during_write(self):
        entered, release = threading.Event(), threading.Event()
        with ThreadPoolExecutor(max_workers=2) as pool:
            def hold():
                with self.qt._write_lock, self.qt._db_guard:
                    entered.set()
                    if not release.wait(3): raise TimeoutError('DB owner not released')
            writer = pool.submit(hold)
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(pool.submit(self.qt.translate, '甲乙', 'book').result(.5), 'old keep')
            finally: release.set()
            writer.result(3)

    def test_global_and_eight_private_scopes_are_retained(self):
        for i in range(20): self.qt.translate('甲', str(i))
        self.assertEqual(len(self.qt._cache), 9)
        self.assertIn('*', self.qt._cache)

    def test_continuous_readers_with_global_private_rules_and_bulk_mutations(self):
        stop, ready = threading.Event(), threading.Barrier(4)
        def read():
            ready.wait(3)
            count = 0
            while not stop.is_set():
                output = self.qt.translate('甲乙|' * 100, 'book')
                self.assertEqual(len(set(output.split('|')[:-1])), 1, output)
                count += 1
            return count
        with ThreadPoolExecutor(max_workers=3) as pool:
            readers = [pool.submit(read) for _ in range(3)]
            ready.wait(3)
            try:
                for scope in ('*', 'book'):
                    for i in range(12):
                        self.qt.save('vp', '甲', str(i), scope)
                        self.qt.save('rules', '甲乙', 'rule' + str(i), scope)
                        self.qt.delete('rules', '甲乙', scope)
                        self.qt.import_text('vp', '甲=bulk\n乙=pair', scope=scope)
                        self.qt.delete('vp', '甲', scope)
            finally: stop.set()
            self.assertTrue(all(f.result(5) > 0 for f in readers))

    def test_bulk_sql_rollback_preserves_scope_and_backups(self):
        snapshot = self.qt._cache['*']
        with self.qt._db_guard:
            self.qt._db.execute("CREATE TRIGGER reject_bulk BEFORE INSERT ON entries WHEN NEW.word='丙' BEGIN SELECT RAISE(ABORT,'rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError): self.qt.import_text('vp', '甲=new\n丙=bad', 'replace')
        self.assertIs(self.qt._cache['*'], snapshot)
        self.assertEqual(self.qt.translate('甲乙'), 'old keep')
        self.assertEqual(self.qt.status()['backups'], 0)

    def test_failed_rebuild_after_commit_does_not_leave_permanently_stale_cache(self):
        with patch.object(self.qt, '_build_scope', side_effect=RuntimeError('Build interrupted')):
            with self.assertRaises(RuntimeError): self.qt.import_text('vp', '甲=committed')
        self.assertEqual(self.qt.translate('甲'), 'committed')


if __name__ == '__main__': unittest.main()
