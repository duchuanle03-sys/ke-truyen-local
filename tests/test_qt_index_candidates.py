"""Differential checks for benchmark candidates; production remains unchanged."""
from contextlib import ExitStack, closing
import importlib.metadata
import importlib.util
from pathlib import Path
import random
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from qt import QtDictionary
from qt_index_benchmark import candidate_engine, generate, profile_database, synthetic_meaning


class QtIndexCandidateTests(unittest.TestCase):
    def test_synthetic_generator_preserves_counts_lengths_and_is_reproducible(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = {'tiers': [{'kind': 'vietphrase', 'entries': 1200, 'mean_first_meaning_bytes': 14.5}],
                       'word_lengths': [['vietphrase', 1, 20], ['vietphrase', 2, 300],
                                        ['vietphrase', 4, 600], ['vietphrase', 9, 280]],
                       'adjacent_prefix_lengths': {'vietphrase': {'0': 30, '1': 400, '2': 770}},
                       'meaning_shapes': {'vietphrase': [[5, 5, 1, 600], [12, 24, 2, 600]]}}
            paths = [Path(directory) / name / 'qt.sqlite3' for name in ('a', 'b')]
            for path in paths: generate(profile, path)
            actual = profile_database(paths[0])
            self.assertEqual(actual['word_lengths'], profile['word_lengths'])
            self.assertEqual({int(k): v for k, v in actual['adjacent_prefix_lengths']['vietphrase'].items()},
                             {int(k): v for k, v in profile['adjacent_prefix_lengths']['vietphrase'].items()})
            self.assertEqual(sum(t['entries'] for t in actual['tiers']), 1200)
            self.assertEqual(actual['meaning_shapes'], profile['meaning_shapes'])
            with closing(sqlite3.connect(paths[0])) as a, closing(sqlite3.connect(paths[1])) as b:
                self.assertEqual(a.execute('SELECT * FROM entries ORDER BY kind,scope,word').fetchall(),
                                 b.execute('SELECT * FROM entries ORDER BY kind,scope,word').fetchall())
            self.assertGreater(sum(v for k, v in actual['adjacent_prefix_lengths']['vietphrase'].items() if int(k)), 0)

    def test_candidates_match_original_engine_through_edits_and_snapshot_replacement(self):
        backends = ['flat', 'bucketed']
        if importlib.util.find_spec('marisa_trie'): backends.append('marisa')
        for backend in backends:
            with self.subTest(backend=backend), ExitStack() as stack:
                original = stack.enter_context(tempfile.TemporaryDirectory())
                prototype = stack.enter_context(tempfile.TemporaryDirectory())
                oracle = QtDictionary(original)
                stack.callback(oracle.close)
                # Build expectations before installing benchmark-only dispatch.
                actions = [('save', ('name', '甲', 'short¦alternative')),
                           ('save', ('vp', '甲乙丙', 'long')),
                           ('save', ('vp', '乙', 'base')),
                           ('save', ('vp', '乙丁', 'pair')),
                           ('save', ('vp', '乙丁戊', 'triple')),
                           ('save', ('name', '己', 'global-name')),
                           ('save', ('vp', '己庚', 'global-long')),
                           ('save', ('vp', '空', '¦hidden')),
                           ('save', ('hanviet', '𠀀', 'ngoài')),
                           ('save', ('pronouns', '他', 'hắn')),
                           ('save', ('hanviet', '他', 'lower-priority')),
                           ('save', ('vp', '乙', 'private', 'book')),
                           ('save', ('vp', '己', 'private-over-name', 'book')),
                           ('save', ('rules', '第{n}章', 'chương {n}')),
                           ('save', ('rules', '《{word}》', '{lookup:word}')),
                           ('save', ('vp', '乙', 'changed')),
                           ('delete', ('vp', '乙丁戊')),
                           ('delete', ('vp', '乙丁')),
                           ('delete', ('vp', '乙', 'book')),
                           ('delete', ('vp', '己', 'book')),
                           ('save', ('name', '甲', 'new')),
                           ('import_text', ('vp', '乙=bulk\n丙=third', 'replace')),
                           ('restore_backup', (1,))]
                rng = random.Random(6)
                texts = ['甲乙丙 空𠀀 他 第12章 《乙丁》 《乙》 己庚', '乙丁戊', '', 'English text',
                         ''.join(rng.choices('甲乙丙丁戊空他𠀀 x\n', k=2000))]
                expected = []
                for method, args in actions:
                    getattr(oracle, method)(*args)
                    expected.append([oracle.translate(text, scope) for scope in ('', 'book') for text in texts])
                with candidate_engine(backend, prototype) as candidate:
                    for (method, args), outputs in zip(actions, expected):
                        old = candidate._compiled('book')
                        previous = candidate.translate(texts[0], 'book') if old[2] else None
                        getattr(candidate, method)(*args)
                        self.assertEqual([candidate.translate(text, scope) for scope in ('', 'book') for text in texts], outputs)
                        # An acquired snapshot remains usable after a mutation or rebuild.
                        from unittest.mock import patch
                        if old[2]:
                            with patch.object(candidate, '_compiled', return_value=old):
                                self.assertEqual(candidate.translate(texts[0], 'book'), previous)
                    before = candidate.translate('乙丁戊')
                    with candidate._db_guard:
                        candidate._db.execute("CREATE TRIGGER reject_insert BEFORE INSERT ON entries BEGIN SELECT RAISE(ABORT,'rollback'); END")
                    with self.assertRaises(sqlite3.IntegrityError): candidate.save('vp', '乙', 'rejected')
                    self.assertEqual(candidate.translate('乙丁戊'), before)
                    for book in range(12): candidate.translate('乙', str(book))
                    self.assertEqual(len(candidate._cache), 9)

    def test_native_candidate_is_checked_when_wheel_is_installed(self):
        if not importlib.util.find_spec('marisa_trie'): self.skipTest('Run with the isolated MARISA benchmark environment')
        self.assertEqual(importlib.metadata.version('marisa-trie'), '1.4.1')

    def test_readers_keep_old_snapshot_during_rebuild_and_external_writes_invalidate_it(self):
        backends = ['flat', 'bucketed'] + (['marisa'] if importlib.util.find_spec('marisa_trie') else [])
        for backend in backends:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                with candidate_engine(backend, directory) as engine:
                    engine.import_text('vp', '甲=old\n甲乙=long')
                    self.assertEqual(engine.translate('甲乙'), 'long')
                    entered, release = threading.Event(), threading.Event()
                    build = engine._build_scope
                    def paused_build(*args):
                        entered.set()
                        if not release.wait(3): raise TimeoutError('Rebuild stalled')
                        return build(*args)
                    with patch.object(engine, '_build_scope', side_effect=paused_build), ThreadPoolExecutor(2) as pool:
                        writer = pool.submit(engine.import_text, 'vp', '甲乙=new')
                        try:
                            self.assertTrue(entered.wait(2))
                            self.assertEqual(pool.submit(engine.translate, '甲乙').result(.5), 'long')
                        finally: release.set()
                        writer.result(3)
                    self.assertEqual(engine.translate('甲乙'), 'new')
                    with closing(sqlite3.connect(engine.path)) as db, db:
                        db.execute("UPDATE entries SET meaning='external' WHERE word='甲乙'")
                    self.assertEqual(engine.translate('甲乙'), 'external')
                    with patch.object(engine, '_build_scope', side_effect=RuntimeError('Build failed')):
                        with self.assertRaises(RuntimeError): engine.import_text('vp', '甲乙=committed')
                    self.assertEqual(engine.translate('甲乙'), 'committed')

    def test_synthetic_meanings_match_unicode_width_and_byte_shape(self):
        for shape in ((0, 0, 1), (7, 7, 1), (7, 11, 1), (7, 8, 2), (7, 18, 2), (7, 12, 4), (3, 12, 4)):
            meaning = synthetic_meaning(*shape, 314)
            self.assertEqual((len(meaning), len(meaning.encode('utf-8'))), shape[:2])
            maximum = max(map(ord, meaning), default=0)
            self.assertEqual(1 if maximum <= 255 else 2 if maximum <= 65535 else 4, shape[2])

    def test_stale_build_is_rejected_and_failed_bulk_transaction_keeps_snapshot(self):
        backends = ['flat', 'bucketed'] + (['marisa'] if importlib.util.find_spec('marisa_trie') else [])
        for backend in backends:
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as directory:
                with candidate_engine(backend, directory) as engine:
                    engine.save('vp', '甲', 'old')
                    engine.translate('甲')
                    build, external = engine._build_scope, []
                    def changed_build(*args):
                        snapshot = build(*args)
                        if not external:
                            external.append(True)
                            with closing(sqlite3.connect(engine.path)) as db, db:
                                db.execute("UPDATE entries SET meaning='external' WHERE word='甲'")
                        return snapshot
                    with patch.object(engine, '_build_scope', side_effect=changed_build):
                        engine.import_text('vp', '甲=internal')
                    self.assertEqual(engine.translate('甲'), 'external')
                    snapshot = engine._cache['*']
                    with engine._db_guard:
                        engine._db.execute("CREATE TRIGGER reject_bulk BEFORE INSERT ON entries WHEN NEW.word='丙' BEGIN SELECT RAISE(ABORT,'rollback'); END")
                    with self.assertRaises(sqlite3.IntegrityError): engine.import_text('vp', '甲=new\n丙=bad', 'replace')
                    self.assertIs(engine._cache['*'], snapshot)
                    self.assertEqual(engine.status()['backups'], 0)
                    self.assertEqual(engine.translate('甲'), 'external')


if __name__ == '__main__': unittest.main()
