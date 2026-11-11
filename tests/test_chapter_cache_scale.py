"""Synthetic storage/selection benchmark; never opens the personal library."""
import hashlib
import json
import tempfile
import time
import unittest

from chapter_cache import BODY_BYTES
from library import Store


def storage(store):
    with store.db() as db:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        metrics = {key: db.execute('PRAGMA ' + key).fetchone()[0]
                   for key in ('page_count', 'freelist_count', 'page_size', 'auto_vacuum')}
        # This Python SQLite build has no dbstat; measure FTS shadow payload separately from DB pages.
        metrics['fts_index_payload_bytes'] = db.execute('SELECT coalesce(sum(length(block)),0) FROM chapter_search_fts_data').fetchone()[0]
        metrics['normalized_text_bytes'] = db.execute('SELECT coalesce(sum(length(CAST(text AS BLOB))),0) FROM chapter_search_docs').fetchone()[0]
    metrics['physical_sqlite_bytes'] = store.path.stat().st_size
    metrics['freelist_bytes'] = metrics['freelist_count'] * metrics['page_size']
    return metrics


class ChapterCacheScaleTests(unittest.TestCase):
    def test_hundred_thousand_records_bounded_lru_fts_reuse_and_explicit_compaction(self):
        with tempfile.TemporaryDirectory(prefix='chapter-cache-scale-') as temporary:
            store = Store(temporary)
            with store.db() as db:
                db.executemany('INSERT INTO books(id,name,source,kind,added_at,updated_at,chapter_count) VALUES(?,?,?,?,0,0,50000)',
                               [('novel', 'Novel', 'stv', 'text'), ('manga', 'Manga', 'qq', 'comic')])
                db.executemany('INSERT INTO chapters(book_id,idx,title,url) VALUES(?,?,?,?)',
                    ((name, i, f'Chapter {i}', f'https://example.com/{name}/{i}') for name in ('novel', 'manga') for i in range(50000)))
                body = ' '.join('từ' + hashlib.sha256(str(i).encode()).hexdigest()[:16] for i in range(512))
                db.executemany('UPDATE chapters SET html=?,text=?,downloaded=1 WHERE book_id=? AND idx=?',
                    ((f'<p>{body}</p>', f'needle{i:05} {body}', name, i) for name in ('novel', 'manga') for i in range(256)))
                db.execute("UPDATE chapter_cache SET kind='cache',last_used=id")
            store.set_chapter_cache_limit(100000)
            while store.index_search(): pass
            before = storage(store)
            started = time.perf_counter()
            candidates = store.chapter_cache_candidates(8)
            selection_ms = (time.perf_counter() - started) * 1000
            self.assertEqual(len(candidates), 8)
            with store.db() as db:
                plan = ' '.join(r[3] for r in db.execute("EXPLAIN QUERY PLAN SELECT * FROM chapter_cache WHERE kind='cache' AND offline=0 ORDER BY last_used,id LIMIT 8"))
                self.assertIn('chapter_cache_lru', plan)
                self.assertNotIn('TEMP B-TREE', plan)
                logical = db.execute('SELECT sum(' + BODY_BYTES.format(p='c.') + ") FROM chapters c WHERE book_id='novel' AND idx<8").fetchone()[0]
            store.set_chapter_cache_limit(504)
            started = time.perf_counter()
            self.assertEqual(store.evict_chapters(candidates), 8)
            batch_ms = (time.perf_counter() - started) * 1000
            after_batch = storage(store)
            for _ in range(1000):
                if not store.maintain_chapter_cache(): break
            else: self.fail('Bounded FTS reclaim did not finish')
            after_reclaim = storage(store)
            self.assertEqual(store.search_content('novel', 'needle00000')['results'], [])
            self.assertEqual(store.chapter_cache_stats()['cache_count'], 504)
            self.assertLess(after_batch['normalized_text_bytes'], before['normalized_text_bytes'])
            self.assertGreater(logical, 100000)
            store.compact_database(True)
            after_compact = storage(store)
            self.assertEqual(after_compact['auto_vacuum'], before['auto_vacuum'])
            self.assertLess(after_compact['physical_sqlite_bytes'], before['physical_sqlite_bytes'])
            with store.db() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM chapters').fetchone()[0], 100000)
                self.assertEqual(db.execute('PRAGMA quick_check').fetchone()[0], 'ok')
            print(json.dumps({'chapter_cache_benchmark': {'chapter_records': 100000, 'cached_bodies': 512,
                'batch_size': 8, 'candidate_selection_ms': round(selection_ms, 3), 'single_batch_ms': round(batch_ms, 3),
                'logical_body_bytes_evicted': logical, 'normalized_bytes_removed': before['normalized_text_bytes'] - after_batch['normalized_text_bytes'],
                'fts_payload_bytes_released_by_merge': before['fts_index_payload_bytes'] - after_reclaim['fts_index_payload_bytes'],
                'before': before, 'after_eviction': after_batch, 'after_fts_merge': after_reclaim, 'after_explicit_compaction': after_compact}}))


if __name__ == '__main__': unittest.main()
