"""SQL shelf filtering, stable pages and bounded work on a large metadata corpus."""
import contextlib
from concurrent.futures import ThreadPoolExecutor
import json
import statistics
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from library import Store


class BookshelfTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(temporary.name)
        for i, name in enumerate(('Ánh Trăng', 'áNH trăng', 'Truyện 100%_!', 'Straße', 'Last')):
            self.store.upsert_book({'id': str(i), 'name': name, 'author': 'TÁC GIẢ',
                'source': 'hako' if i < 3 else 'file', 'kind': 'comic' if i == 2 else 'text',
                'groups': ['one', 'two'] if i == 0 else ['two']})
        with self.store.db() as db:
            db.execute('UPDATE books SET read_at=10,added_at=10,updated_at=10,progress=.5,chapter_count=20')

    def ids(self, **query):
        return [item['id'] for item in self.store.list_books(**query)['items']]

    def test_unicode_and_literal_substring_filters(self):
        self.assertEqual(self.ids(query='ÁNH TRĂNG', sort='name'), ['0', '1'])
        self.assertEqual(self.ids(query='STRASSE'), ['3'])
        self.assertEqual(self.ids(query='100%_!'), ['2'])
        self.assertEqual(self.ids(query="%' OR 1=1 --"), [])
        self.assertEqual(self.ids(query='tÁc GiẢ'), self.ids())
        self.assertEqual(self.ids(source='hako', group='one', kind='text'), ['0'])
        self.assertEqual(self.ids(source='hako', kind='comic'), ['2'])
        self.assertEqual(self.ids(kind='file', sort='name'), ['4', '3'])

    def test_sql_order_and_reverse_span_all_pages_with_stable_ties(self):
        for sort in ('recent', 'name', 'added', 'updated', 'progress', 'chapters', 'unknown'):
            with self.subTest(sort=sort):
                forward = self.ids(sort=sort)
                backward = self.ids(sort=sort, reverse=True)
                self.assertEqual(backward, forward[::-1])
                joined = []
                for offset in (0, 2, 4):
                    page = self.store.list_books(sort=sort, limit=2, offset=offset)
                    self.assertEqual((page['total'], page['shelf_total'], page['offset']), (5, 5, offset))
                    joined += [item['id'] for item in page['items']]
                    self.assertEqual(page['next_offset'], offset + 2 if offset < 4 else None)
                self.assertEqual(joined, forward)

    def test_each_sort_uses_its_own_key_and_breaks_identical_titles_by_id(self):
        for turn, (sort, column) in enumerate((('recent', 'read_at'), ('added', 'added_at'),
                ('updated', 'updated_at'), ('progress', 'progress'), ('chapters', 'chapter_count'))):
            values = {str(i): (i + turn) % 5 / 5 for i in range(5)}
            with self.store.db() as db:
                db.executemany(f'UPDATE books SET {column}=? WHERE id=?', ((value, key) for key, value in values.items()))
            expected = sorted(values, key=values.get, reverse=True)
            self.assertEqual(self.ids(sort=sort), expected)
            self.assertEqual(self.ids(sort=sort, reverse=True), expected[::-1])
        self.store.upsert_book({'id': '6', 'name': 'Identical'})
        self.store.upsert_book({'id': '5', 'name': 'Identical'})
        self.assertEqual(self.ids(query='Identical', sort='name'), ['5', '6'])
        self.assertEqual(self.ids(query='Identical', sort='name', reverse=True), ['6', '5'])

    def test_groups_and_text_indexes_follow_edits_and_deletion(self):
        self.store.update_book('0', {'name': 'Changed', 'author': 'New author', 'groups': ['new']})
        self.assertEqual(self.ids(query='ánh trăng'), ['1'])
        self.assertEqual(self.ids(query='CHANGED', group='new'), ['0'])
        self.assertEqual(self.ids(group='one'), [])
        self.store.trash_book('0')
        self.assertEqual(self.ids(group='new'), [])
        self.assertEqual(self.ids(group='new', trashed=True), ['0'])
        self.store.purge_book('0')
        with self.store.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM book_groups WHERE book_id='0'").fetchone()[0], 0)

    def test_archive_trash_and_page_validation(self):
        self.store.update_book('0', {'archived': True})
        self.store.trash_book('0')
        self.store.trash_book('1')
        self.assertEqual(set(self.ids(trashed=True)), {'0', '1'})
        self.assertEqual(self.ids(archived=True), [])
        self.store.restore_book('0')
        self.assertIn('0', self.ids())
        page = self.store.list_books(limit=2, offset=999999)
        self.assertEqual((page['offset'], len(page['items']), page['total'], page['shelf_total']), (2, 2, 4, 4))
        self.assertEqual(self.store.list_books(limit=99999)['limit'], 200)
        self.assertEqual(self.store.list_books(limit=-1, offset=-10)['limit'], 1)
        with self.assertRaises(ValueError):
            self.store.list_books(offset='invalid')
        with self.assertRaises(ValueError):
            self.store.list_books(kind='invalid')

    def test_page_count_and_items_share_a_snapshot_without_blocking_writes(self):
        counted, release = threading.Event(), threading.Event()
        original_db = self.store.db
        @contextlib.contextmanager
        def waiting_db():
            with original_db() as db:
                counts = 0
                def trace(sql):
                    nonlocal counts
                    if sql.startswith('SELECT count(*) FROM books'):
                        counts += 1
                        if counts == 2:
                            counted.set()
                            release.wait(5)
                db.set_trace_callback(trace)
                yield db
        with patch.object(self.store, 'db', waiting_db), ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(self.store.list_books)
            try:
                self.assertTrue(counted.wait(3), 'The first SQL count did not finish')
                self.store.update_book('4', {'archived': True})
            finally:
                release.set()
            page = pending.result(3)
        self.assertEqual((page['total'], page['shelf_total'], len(page['items'])), (5, 5, 5))
        self.assertEqual(self.store.list_books()['total'], 4)


class LargeBookshelfTests(unittest.TestCase):
    def test_fifty_thousand_books_convert_only_one_page_and_use_sql_indexes(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = Store(temporary)
            with store.db() as db:
                db.executemany('INSERT INTO books(id,name,author,source,kind,groups_json,added_at,updated_at,read_at) '
                    'VALUES(?,?,?,?,?,?,?,?,?)',
                    ((f'{i:05}', f'TRUYỆN {i:05}', 'Tác giả', 'hako' if i % 2 else 'file',
                      'text', json.dumps(['group-' + str(i % 100)]), i, i, i) for i in range(50000)))
                for i in range(3):
                    db.execute('INSERT INTO chapters(book_id,idx,text,html,downloaded) VALUES(?,0,?,?,1)',
                               (f'{i:05}', 'Cached text.', '<p>Cached text.</p>'))
            statements, folds = [], []
            original_db = store.db
            @contextlib.contextmanager
            def measured_db():
                with original_db() as db:
                    db.set_trace_callback(statements.append)
                    db.create_function('shelf_fold', 1, lambda value: folds.append(value) or value.casefold(), deterministic=True)
                    yield db
            times = []
            with patch.object(store, 'db', measured_db), patch.object(store, '_book', wraps=store._book) as convert:
                for _ in range(5):
                    started = time.perf_counter()
                    page = store.list_books()
                    times.append((time.perf_counter() - started) * 1000)
                    self.assertEqual((len(page['items']), page['total'], page['next_offset']), (60, 50000, 60))
                self.assertEqual(convert.call_count, 300)
                self.assertEqual(page['items'][0]['id'], '49999')
                self.assertEqual(store.list_books(query='truyện 123')['total'], 100)
                self.assertEqual(store.list_books(group='group-3', source='hako')['total'], 500)
            self.assertEqual(folds, [], 'Reads repeated Python normalization instead of reading the expression index')
            self.assertFalse(any('FROM chapters' in sql for sql in statements))
            with store.db() as db:
                for sql in (s for s in statements if s.startswith('SELECT * FROM books') and 'instr' not in s and 'book_groups' not in s):
                    plan = ' '.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN ' + sql))
                    self.assertIn('INDEX books_shelf_recent', plan)
                    self.assertNotIn('TEMP B-TREE', plan)
            print(json.dumps({'books': 50000, 'cached_chapter_bodies': 3,
                              'page_size': 60, 'median_page_ms': statistics.median(times),
                              'page_json_bytes': len(json.dumps(page).encode()), 'read_normalizations': len(folds)}))
            for label, query in (('name', {'sort': 'name'}), ('substring', {'query': 'tác giả'}),
                                 ('group', {'group': 'group-3'}), ('last_page', {'offset': 49980})):
                started = time.perf_counter()
                result = store.list_books(**query)
                print(json.dumps({'workload': label, 'elapsed_ms': (time.perf_counter() - started) * 1000,
                                  'total': result['total'], 'returned': len(result['items'])}))
            with store.db() as db:
                for row in db.execute("SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'books_shelf_%'").fetchall():
                    db.execute('DROP INDEX ' + row[0])
                db.execute('DROP TRIGGER shelf_group_insert')
                db.execute('DROP TRIGGER shelf_group_update')
                db.execute('DROP TABLE book_groups')
            started = time.perf_counter()
            upgraded = Store(temporary)
            upgrade_ms = (time.perf_counter() - started) * 1000
            self.assertEqual(upgraded.list_books(group='group-3')['total'], 500)
            self.assertEqual(upgraded.get_book('00000')['read_at'], 0)
            self.assertEqual(upgraded.content('00000', 0)['text'], 'Cached text.')
            print(json.dumps({'books': 50000, 'cached_chapter_bodies': 3, 'one_time_shelf_index_upgrade_ms': upgrade_ms}))


if __name__ == '__main__':
    unittest.main()
