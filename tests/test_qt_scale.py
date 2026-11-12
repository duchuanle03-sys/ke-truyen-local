"""Synthetic million-entry QT timings, never the private dictionary."""
import json
from pathlib import Path
import statistics
import tempfile
import time
import unittest

from qt import QtDictionary
from qt_index_benchmark import generate


class QtScaleTests(unittest.TestCase):
    def test_million_entries_measure_mutation_acquisition_and_translation_separately(self):
        with tempfile.TemporaryDirectory(prefix='qt-scale-') as temporary:
            profile = json.loads((Path(__file__).parent / 'fixtures' / 'qt_index_profile.json').read_text(encoding='utf-8'))
            generate(profile, Path(temporary) / 'qt.sqlite3')
            qt = QtDictionary(temporary)
            try:
                count = sum(tier['entries'] for tier in profile['tiers'])
                with qt._reader() as db:
                    words = [row[0] for row in db.execute("SELECT word FROM entries WHERE kind='vietphrase' AND rowid%1000=0 LIMIT 512")]
                word = words[51]
                qt.save('name', '私', 'riêng', 'book')
                start = time.perf_counter(); qt._compiled('book')
                cold_ms = (time.perf_counter() - start) * 1000
                timings = {}
                for scope in ('*', 'book'):
                    for action in ('save', 'delete'):
                        measured = []
                        for _ in range(7):
                            if action == 'delete': qt.save('vp', word, 'updated', scope)
                            start = time.perf_counter()
                            if action == 'save': qt.save('vp', word, 'updated', scope)
                            else: qt.delete('vp', word, scope)
                            measured.append((time.perf_counter() - start) * 1000)
                        timings[scope + '_' + action + '_median_ms'] = round(statistics.median(measured), 3)
                acquisitions = []
                for _ in range(100):
                    start = time.perf_counter(); qt._compiled('book')
                    acquisitions.append((time.perf_counter() - start) * 1000)
                snapshot = qt._compiled('book')
                seed = '|'.join(words)
                chapter = (seed * (10000 // len(seed) + 1))[:10000]
                self.assertEqual(len(chapter), 10000)
                # Fix the already-acquired snapshot to isolate traversal from acquisition.
                from unittest.mock import patch
                translations = []
                with patch.object(qt, '_compiled', return_value=snapshot):
                    for _ in range(5):
                        start = time.perf_counter(); result = qt.translate(chapter, 'book')
                        translations.append((time.perf_counter() - start) * 1000)
                    long_translations = []
                    for _ in range(3):
                        start = time.perf_counter(); qt.translate(chapter * 10, 'book')
                        long_translations.append((time.perf_counter() - start) * 1000)
                self.assertTrue(result)
                self.assertEqual(len(qt._cache), 2)
                print(json.dumps({'qt_benchmark': {'synthetic_global_entries': count, 'chapter_characters': len(chapter),
                    'cold_scope_compilation_ms': round(cold_ms, 3), **timings,
                    'composite_acquisition_median_ms': round(statistics.median(acquisitions), 4),
                    'pure_translation_median_ms': round(statistics.median(translations), 3),
                    'pure_translation_100000_median_ms': round(statistics.median(long_translations), 3)}}))
            finally: qt.close()
