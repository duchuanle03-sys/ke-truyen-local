import contextlib
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from library import Store


class ContentSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)

    def book(self, book_id='book', texts=()):
        self.store.upsert_book({'id': book_id, 'name': book_id},
                              [{'title': f'Chapter {i}', 'text': text} for i, text in enumerate(texts)])

    def index(self):
        while self.store.index_search():
            pass

    def hits(self, query, book_id='book', **kwargs):
        return self.store.search_content(book_id, query, **kwargs)

    def test_literal_substrings_unicode_and_fts_operators_keep_their_meaning(self):
        self.book(texts=('Một ngày CẦU MA bước vào phòng.', 'Cá nhân đứng trước phố.',
                         'STRASSE và Straße; a*b? "quote" OR NEAR(x) literal.', '林风走进房间。', ''))
        self.book('other', ('CẦU MA và cá nhân',))
        self.index()
        for query, expected in [('cầu ma', [0]), ('nhân', [1]), ('gày CẦ', [0]), ('STRASSE', [2]),
                                ('a*b?', [2]), ('"quote" OR NEAR(x)', [2]), ('风走进', [3]),
                                ('ma', [0]), ('林', [3]), ('cau ma', []), ('unknown', [])]:
            with self.subTest(query=query):
                result = self.hits(query)
                self.assertEqual([hit['index'] for hit in result['results']], expected)
                self.assertEqual((result['searched'], result['total']), (4, 5))
                self.assertFalse(result['indexing'])

    def test_edits_restores_downloads_and_deletes_update_the_index(self):
        self.book(texts=('Original phrase.', ''))
        self.index()
        self.store.edit_content('book', 0, {'title': 'Edited', 'text': 'Changed phrase.', 'html': '<p>Changed phrase.</p>'})
        pending = self.hits('Original')
        self.assertTrue(pending['indexing'])
        self.assertEqual(pending['results'], [])
        self.index()
        self.assertEqual(self.hits('Original')['results'], [])
        self.assertEqual(self.hits('Changed')['results'][0]['title'], 'Edited')
        self.store.restore_content('book', 0)
        self.store.save_content('book', 1, {'text': 'New downloaded phrase.'})
        self.index()
        self.assertEqual([h['index'] for h in self.hits('Original')['results']], [0])
        self.assertEqual([h['index'] for h in self.hits('downloaded')['results']], [1])
        self.store.trash_book('book')
        self.store.purge_book('book')
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chapter_search_docs').fetchone()[0], 0)

    def test_results_are_bounded_and_paginated_by_chapter(self):
        self.book(texts=('Shared phrase' for _ in range(205)))
        self.index()
        page = self.hits('Shared phrase')
        self.assertEqual(len(page['results']), 100)
        self.assertEqual((page['results'][0]['index'], page['next_index']), (0, 99))
        second = self.hits('Shared phrase', after=page['next_index'])
        self.assertEqual((second['results'][0]['index'], len(second['results']), second['next_index']), (100, 100, 199))
        last = self.hits('Shared phrase', after=second['next_index'])
        self.assertEqual((last['results'][0]['index'], len(last['results']), last['next_index']), (200, 5, None))

    def test_background_index_reports_partial_coverage_and_resumes_after_restart(self):
        self.book(texts=('Indexed phrase' for _ in range(30)))
        self.assertTrue(self.hits('Indexed')['indexing'])
        self.store.index_search(limit=2)
        partial = self.hits('Indexed')
        self.assertLessEqual(partial['searched'], 2)
        self.assertTrue(partial['indexing'])
        self.store = Store(self.temp.name)
        self.index()
        self.assertEqual(self.hits('Indexed')['searched'], 30)

    def test_embedded_nul_does_not_create_false_substring_matches(self):
        self.book(texts=('ab\x00c', 'abc', 'ghi\x00jkl'))
        self.index()
        self.assertEqual([h['index'] for h in self.hits('abc')['results']], [1])
        self.assertEqual(self.hits('ghijkl')['results'], [])
        self.assertEqual([h['index'] for h in self.hits('jkl')['results']], [2])

    def test_query_only_decodes_the_matching_result_bodies(self):
        base = 'Repeated paragraph. ' * 300
        self.book(texts=(base + ('needle phrase' if i == 170 else '') for i in range(205)))
        self.index()
        original, decoded = self.store.db, []
        @contextlib.contextmanager
        def observe():
            with original() as db:
                def decode(value):
                    if value.startswith(b'Repeated paragraph.'):
                        decoded.append(len(value))
                    return value.decode('utf-8')
                db.text_factory = decode
                yield db
        with patch.object(self.store, 'db', observe):
            self.assertEqual([h['index'] for h in self.hits('needle phrase')['results']], [170])
            self.assertEqual(len(decoded), 1)
            decoded.clear()
            self.assertEqual(len(self.hits('Repeated')['results']), 100)
            self.assertEqual(len(decoded), 101)

    def test_concurrent_edit_invalidates_a_stale_normalized_snapshot(self):
        self.book(texts=('Original needle',))
        self.index()
        self.store.save_content('book', 0, {'text': 'Old needle'})
        entered, release, errors = threading.Event(), threading.Event(), []
        original = self.store.db
        class SlowText(str):
            def casefold(value):
                if value == 'Old needle':
                    entered.set()
                    if not release.wait(3):
                        raise TimeoutError('Normalization was not released')
                return str.casefold(value)
        @contextlib.contextmanager
        def observe():
            with original() as db:
                db.text_factory = lambda value: SlowText(value.decode('utf-8'))
                yield db
        def run():
            try:
                self.store.index_search()
            except Exception as error:
                errors.append(error)
        with patch.object(self.store, 'db', observe):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                started = time.monotonic()
                self.store.save_content('book', 0, {'text': 'New needle'})
                self.assertLess(time.monotonic() - started, 1, 'Normalization held the SQLite write lock')
            finally:
                release.set()
                thread.join(4)
        self.assertEqual(errors, [])
        self.index()
        self.assertEqual(self.hits('Old needle')['results'], [])
        self.assertEqual([h['index'] for h in self.hits('New needle')['results']], [0])

    def test_legacy_backfill_uses_a_resumable_metadata_cursor(self):
        self.book(texts=('' for _ in range(500)))
        self.store.save_content('book', 499, {'text': 'Legacy searchable needle'})
        with self.store.db() as db:
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'search_%'").fetchall():
                db.execute('DROP TRIGGER ' + row[0])
            for table in ('chapter_search_fts', 'chapter_search_docs', 'chapter_search_pending', 'chapter_search_state'):
                db.execute('DROP TABLE ' + table)
        self.store = Store(self.temp.name)
        self.store.index_search(limit=2)
        with self.store.db() as db:
            state = db.execute('SELECT idx,complete FROM chapter_search_state').fetchone()
        self.assertEqual(tuple(state), (255, 0))
        self.assertTrue(self.hits('Legacy')['indexing'])
        self.store = Store(self.temp.name)
        self.index()
        self.assertEqual([h['index'] for h in self.hits('Legacy')['results']], [499])

    def test_failed_index_batch_rolls_back_fts_documents_and_pending_tokens(self):
        self.book(texts=('Needle one', 'Needle two'))
        with self.store.db() as db:
            db.execute("CREATE TRIGGER fail_search BEFORE INSERT ON chapter_search_docs WHEN NEW.idx=1 BEGIN SELECT RAISE(ABORT,'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.index_search()
        self.assertEqual(self.hits('Needle')['results'], [])
        with self.store.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM chapter_search_pending').fetchone()[0], 2)
            db.execute('DROP TRIGGER fail_search')
        self.index()
        self.assertEqual([h['index'] for h in self.hits('Needle')['results']], [0, 1])

    def test_vacuum_and_casefold_expansion_preserve_results_and_snippets(self):
        self.book(texts=('Delete this needle', 'ß' * 300 + 'needle at the end', 'Another needle'))
        self.index()
        with self.store.db() as db:
            db.execute('DELETE FROM chapters WHERE book_id=? AND idx=0', ('book',))
        with self.store.db() as db:
            db.execute('VACUUM')
            db.execute("INSERT INTO chapter_search_fts(chapter_search_fts,rank) VALUES('integrity-check',1)")
        result = self.hits('needle')
        self.assertEqual([h['index'] for h in result['results']], [1, 2])
        self.assertIn('needle at the end', result['results'][0]['snippet'])

    def test_finished_book_is_ready_while_other_books_backfill(self):
        self.book('first', ('Needle',))
        self.book('later', ('' for _ in range(300)))
        self.store.index_search(limit=1)
        self.assertFalse(self.hits('Needle', 'first')['indexing'])
        self.assertTrue(self.hits('Needle', 'later')['indexing'])


if __name__ == '__main__':
    unittest.main()
