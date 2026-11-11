"""Synthetic million-entry QT timings, never the private dictionary."""
import json
import statistics
import tempfile
import time
import unittest

from qt import QtDictionary


class QtScaleTests(unittest.TestCase):
    def test_million_entries_measure_mutation_acquisition_and_translation_separately(self):
        with tempfile.TemporaryDirectory(prefix='qt-scale-') as temporary:
            qt = QtDictionary(temporary)
            try:
                count = 1014374
                def word(i): return chr(0x4e00 + i % 8192) + chr(0x4e00 + i // 8192)
                with qt._write_lock, qt._db_guard, qt._db:
                    qt._db.executemany("INSERT INTO entries(kind,scope,word,meaning) VALUES('vietphrase','*',?,?)",
                                       ((word(i), 'v' + str(i)) for i in range(count)))
                qt.save('name', '私', 'riêng', 'book')
                start = time.perf_counter(); qt._compiled('book')
                cold_ms = (time.perf_counter() - start) * 1000
                timings = {}
                for scope in ('*', 'book'):
                    for action in ('save', 'delete'):
                        measured = []
                        for _ in range(7):
                            if action == 'delete': qt.save('vp', word(51), 'updated', scope)
                            start = time.perf_counter()
                            if action == 'save': qt.save('vp', word(51), 'updated', scope)
                            else: qt.delete('vp', word(51), scope)
                            measured.append((time.perf_counter() - start) * 1000)
                        timings[scope + '_' + action + '_median_ms'] = round(statistics.median(measured), 3)
                acquisitions = []
                for _ in range(100):
                    start = time.perf_counter(); qt._compiled('book')
                    acquisitions.append((time.perf_counter() - start) * 1000)
                snapshot = qt._compiled('book')
                chapter = ''.join(word(i) for i in range(5000))
                self.assertEqual(len(chapter), 10000)
                # Fix the already-acquired snapshot to isolate traversal from acquisition.
                from unittest.mock import patch
                translations = []
                with patch.object(qt, '_compiled', return_value=snapshot):
                    for _ in range(5):
                        start = time.perf_counter(); result = qt.translate(chapter, 'book')
                        translations.append((time.perf_counter() - start) * 1000)
                self.assertIn('v4999', result)
                self.assertEqual(len(qt._cache), 2)
                print(json.dumps({'qt_benchmark': {'synthetic_global_entries': count, 'chapter_characters': len(chapter),
                    'cold_scope_compilation_ms': round(cold_ms, 3), **timings,
                    'composite_acquisition_median_ms': round(statistics.median(acquisitions), 4),
                    'pure_translation_median_ms': round(statistics.median(translations), 3)}}))
            finally: qt.close()
