"""Bounded chapter bodies; titles, navigation and personal metadata stay in chapters."""
import contextlib
import shutil
import sqlite3
import time


ONLINE = "('qq','full','hako','stv')"
BODY_BYTES = ("length(CAST({p}html AS BLOB))+length(CAST({p}text AS BLOB))+length(CAST({p}images AS BLOB))"
              "+coalesce(length(CAST({p}original_html AS BLOB)),0)+coalesce(length(CAST({p}original_text AS BLOB)),0)"
              "+length(CAST({p}raw AS BLOB))")


class ChapterCache:
    def _init_chapter_cache(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if 'revision' not in {r[1] for r in db.execute('PRAGMA table_info(chapters)')}:
                db.execute('ALTER TABLE chapters ADD COLUMN revision INTEGER NOT NULL DEFAULT 0')
            db.execute("CREATE TABLE IF NOT EXISTS chapter_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('cache','protected','legacy')), offline INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE)")
            db.execute('CREATE INDEX IF NOT EXISTS chapter_cache_lru ON chapter_cache(kind,last_used,id)')
            db.execute('CREATE INDEX IF NOT EXISTS chapter_cache_class ON chapter_cache(kind,id)')
            db.execute("CREATE TRIGGER IF NOT EXISTS chapter_source_protect AFTER UPDATE OF source ON books WHEN NEW.source IS NOT OLD.source AND NEW.source NOT IN " + ONLINE + " BEGIN UPDATE chapter_cache SET kind='protected',version=version+1 WHERE book_id=NEW.id AND kind!='protected'; END")
            db.execute('CREATE TABLE IF NOT EXISTS chapter_readers (id TEXT PRIMARY KEY, book_id TEXT, idx INTEGER, sequence INTEGER NOT NULL, expires REAL NOT NULL, FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE)')
            db.execute('CREATE INDEX IF NOT EXISTS chapter_readers_expiry ON chapter_readers(expires)')
            now, monotonic = time.time(), time.monotonic()
            for row in db.execute('SELECT * FROM chapter_readers WHERE expires>? ORDER BY expires DESC LIMIT 128', (now,)):
                self._chapter_readers[row['id']] = (row['book_id'], row['idx'], monotonic + min(300, row['expires'] - now), row['sequence'])
            db.execute("CREATE TABLE IF NOT EXISTS chapter_cache_state (id INTEGER PRIMARY KEY CHECK(id=1), max_chapters INTEGER NOT NULL DEFAULT 100, cache_count INTEGER NOT NULL DEFAULT 0, cache_bytes INTEGER NOT NULL DEFAULT 0, protected_count INTEGER NOT NULL DEFAULT 0, protected_bytes INTEGER NOT NULL DEFAULT 0, legacy_count INTEGER NOT NULL DEFAULT 0, legacy_bytes INTEGER NOT NULL DEFAULT 0, book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0, clear_until INTEGER NOT NULL DEFAULT 0, clear_before REAL NOT NULL DEFAULT 0, legacy_until INTEGER NOT NULL DEFAULT 0, legacy_cursor INTEGER NOT NULL DEFAULT 0, fts_merge INTEGER NOT NULL DEFAULT 0)")
            db.execute('INSERT OR IGNORE INTO chapter_cache_state(id,complete) SELECT 1,NOT EXISTS(SELECT 1 FROM chapters LIMIT 1)')
            for event, old, new in (('INSERT', False, True), ('UPDATE', True, True), ('DELETE', True, False)):
                deltas = []
                for kind in ('cache', 'protected', 'legacy'):
                    for suffix, value in (('count', '1'), ('bytes', '{p}.bytes')):
                        change = ''.join(sign + f"({p}.kind='{kind}')*" + value.format(p=p)
                                         for p, sign, use in (('OLD', '-', old), ('NEW', '+', new)) if use)
                        deltas.append(f'{kind}_{suffix}={kind}_{suffix}' + change)
                db.execute(f"CREATE TRIGGER IF NOT EXISTS chapter_cache_count_{event.lower()} AFTER {event} ON chapter_cache " +
                           ('WHEN OLD.kind IS NOT NEW.kind OR OLD.bytes IS NOT NEW.bytes ' if event == 'UPDATE' else '') +
                           'BEGIN UPDATE chapter_cache_state SET ' + ','.join(deltas) + ' WHERE id=1; END')
            protected = f"NEW.original_html IS NOT NULL OR NEW.original_text IS NOT NULL OR (SELECT source FROM books WHERE id=NEW.book_id) NOT IN {ONLINE}"
            record = ("INSERT INTO chapter_cache(book_id,idx,kind,bytes) SELECT NEW.book_id,NEW.idx,CASE WHEN " + protected + " THEN 'protected' ELSE 'legacy' END," + BODY_BYTES.format(p='NEW.') + " WHERE NEW.downloaded=1 ON CONFLICT(book_id,idx) DO UPDATE SET bytes=excluded.bytes,version=chapter_cache.version+1,kind=CASE WHEN " + protected + " THEN 'protected' ELSE chapter_cache.kind END;")
            db.execute('CREATE TRIGGER IF NOT EXISTS chapter_body_insert AFTER INSERT ON chapters BEGIN ' + record + ' END')
            changed = ' OR '.join(f'NEW.{key} IS NOT OLD.{key}' for key in ('html', 'text', 'images', 'original_html', 'original_text', 'downloaded', 'url'))
            db.execute('CREATE TRIGGER IF NOT EXISTS chapter_body_update AFTER UPDATE OF html,text,images,original_html,original_text,downloaded,url ON chapters WHEN ' + changed + ' BEGIN UPDATE chapters SET revision=revision+1 WHERE book_id=NEW.book_id AND idx=NEW.idx; ' + record + ' DELETE FROM chapter_cache WHERE book_id=NEW.book_id AND idx=NEW.idx AND NEW.downloaded=0; END')

    @contextlib.contextmanager
    def use_chapter(self, book_id, index):
        key = (book_id, int(index))
        with self._chapter_lock:
            if self._compacting:
                raise ValueError('Đang thu gọn cơ sở dữ liệu; hãy thử lại sau.')
            self._active_chapters[key] += 1
        try:
            yield
        finally:
            with self._chapter_lock:
                self._active_chapters[key] -= 1
                if not self._active_chapters[key]: del self._active_chapters[key]

    def reader_chapter(self, token, book_id, index=None, sequence=0):
        if not isinstance(token, str) or not 1 <= len(token) <= 128:
            raise ValueError('Mã phiên đọc không hợp lệ.')
        if type(sequence) is not int or not 0 <= sequence <= 2 ** 53:
            raise ValueError('Thứ tự phiên đọc không hợp lệ.')
        if book_id is not None and (not isinstance(book_id, str) or not book_id or len(book_id) > 256 or type(index) is not int or not 0 <= index <= 2 ** 53):
            raise ValueError('Truyện hoặc chỉ số chương đọc không hợp lệ.')
        with self._chapter_lock:
            self._reader_keys()
            old = self._chapter_readers.get(token)
            if old and old[3] > sequence: return {'active': old[0] is not None}
            if token not in self._chapter_readers and len(self._chapter_readers) >= 128:
                raise ValueError('Quá nhiều phiên đọc đang mở.')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                if book_id is not None:
                    if not db.execute('SELECT 1 FROM chapters WHERE book_id=? AND idx=? AND downloaded=1', (book_id, int(index))).fetchone():
                        raise ValueError('Chương đọc không còn được lưu; hãy mở lại chương.')
                    self._track_body(db, book_id, index)
                db.execute('DELETE FROM chapter_readers WHERE expires<=?', (time.time(),))
                db.execute('INSERT INTO chapter_readers(id,book_id,idx,sequence,expires) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET book_id=excluded.book_id,idx=excluded.idx,sequence=excluded.sequence,expires=excluded.expires',
                           (token, book_id, index if book_id is not None else None, sequence, time.time() + 300))
            self._chapter_readers[token] = (book_id, int(index) if book_id is not None else None, time.monotonic() + 300, sequence)
        return {'active': book_id is not None}

    def _reader_keys(self):
        now = time.monotonic()
        self._chapter_readers = {k: v for k, v in self._chapter_readers.items() if v[2] > now}
        return set(self._active_chapters) | {(v[0], v[1]) for v in self._chapter_readers.values() if v[0] is not None}

    def _track_body(self, db, book_id, index, *, offline=False, edited=False):
        if not db.execute('SELECT 1 FROM chapter_cache WHERE book_id=? AND idx=?', (book_id, index)).fetchone():
            db.execute("INSERT INTO chapter_cache(book_id,idx,kind,bytes) SELECT c.book_id,c.idx,CASE WHEN b.source NOT IN " + ONLINE + " OR c.original_html IS NOT NULL OR c.original_text IS NOT NULL THEN 'protected' WHEN c.downloaded=1 THEN 'legacy' ELSE 'cache' END," + BODY_BYTES.format(p='c.') + " FROM chapters c JOIN books b ON b.id=c.book_id WHERE c.book_id=? AND c.idx=?", (book_id, index))
        db.execute("UPDATE chapter_cache SET kind=CASE WHEN ? OR ? THEN 'protected' ELSE kind END,offline=max(offline,?),last_used=?,version=version+1 WHERE book_id=? AND idx=?",
                   (offline, edited, offline, time.time(), book_id, index))

    def touch_chapter(self, book_id, index):
        with self._chapter_lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM chapters WHERE book_id=? AND idx=? AND downloaded=1', (book_id, int(index))).fetchone():
                self._track_body(db, book_id, int(index))

    def protect_chapter(self, book_id, index):
        with self._chapter_lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT downloaded FROM chapters WHERE book_id=? AND idx=?', (book_id, int(index))).fetchone()
            if not row or not row[0]: raise ValueError('Chương chưa có nội dung để giữ ngoại tuyến.')
            self._track_body(db, book_id, int(index), offline=True)

    def chapter_cache_stats(self):
        with self.db() as db:
            result = dict(db.execute('SELECT * FROM chapter_cache_state WHERE id=1').fetchone())
            result.update({key: db.execute('PRAGMA ' + key).fetchone()[0] for key in ('auto_vacuum', 'page_count', 'freelist_count', 'page_size')})
        result['database_bytes'] = self.path.stat().st_size
        wal = self.path.with_name(self.path.name + '-wal')
        try: result['wal_bytes'] = wal.stat().st_size
        except FileNotFoundError: result['wal_bytes'] = 0
        result['reusable_bytes'] = result['freelist_count'] * result['page_size']
        return result

    def set_chapter_cache_limit(self, value):
        if type(value) is not int or not 0 <= value <= 100000:
            raise ValueError('Giới hạn phải là số nguyên từ 0 đến 100.000 chương.')
        with self.db() as db: db.execute('UPDATE chapter_cache_state SET max_chapters=? WHERE id=1', (value,))
        return self.chapter_cache_stats()

    def clear_chapter_cache(self):
        with self.db() as db:
            db.execute('UPDATE chapter_cache_state SET clear_until=coalesce((SELECT max(id) FROM chapter_cache),0),clear_before=? WHERE id=1', (time.time(),))
        return self.chapter_cache_stats()

    def reclassify_legacy_chapters(self, confirmed):
        if confirmed is not True: raise ValueError('Cần xác nhận: chương cũ có thể là bản ngoại tuyến duy nhất; sau phân loại lại có thể bị dọn và cần mạng/CAPTCHA/dịch để đọc lại.')
        with self.db() as db:
            if not db.execute('SELECT complete FROM chapter_cache_state WHERE id=1').fetchone()[0]:
                raise ValueError('Hãy chờ kiểm kê chương cũ hoàn tất trước khi phân loại lại.')
            db.execute("UPDATE chapter_cache_state SET legacy_until=coalesce((SELECT max(id) FROM chapter_cache WHERE kind='legacy'),0),legacy_cursor=0 WHERE id=1")
        return self.chapter_cache_stats()

    def _migrate_chapter_cache(self):
        with self._chapter_lock, self.db() as db:
            state = db.execute('SELECT * FROM chapter_cache_state WHERE id=1').fetchone()
            if state['complete']: return 0
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT * FROM chapter_cache_state WHERE id=1').fetchone()
            if state['complete']: return 0
            rows = db.execute('SELECT book_id,idx,downloaded FROM chapters WHERE (book_id,idx)>(?,?) ORDER BY book_id,idx LIMIT 64', (state['book_id'], state['idx'])).fetchall()
            processed, size = [], 0
            for row in rows:
                if row['downloaded']:
                    # Unknown historical download intent stays protected as legacy.
                    db.execute("INSERT OR IGNORE INTO chapter_cache(book_id,idx,kind,bytes) SELECT c.book_id,c.idx,CASE WHEN b.source NOT IN " + ONLINE + " OR c.original_html IS NOT NULL OR c.original_text IS NOT NULL THEN 'protected' ELSE 'legacy' END," + BODY_BYTES.format(p='c.') + ' FROM chapters c JOIN books b ON b.id=c.book_id WHERE c.book_id=? AND c.idx=?', (row['book_id'], row['idx']))
                    size += db.execute('SELECT bytes FROM chapter_cache WHERE book_id=? AND idx=?', (row['book_id'], row['idx'])).fetchone()[0]
                processed.append(row)
                if size >= 256 * 1024: break
            if processed:
                db.execute('UPDATE chapter_cache_state SET book_id=?,idx=? WHERE id=1', (processed[-1]['book_id'], processed[-1]['idx']))
            if len(rows) < 64 and len(processed) == len(rows): db.execute('UPDATE chapter_cache_state SET complete=1 WHERE id=1')
            return len(processed)

    def chapter_cache_candidates(self, limit=8, clear=False):
        with self._chapter_lock:
            keys = self._reader_keys()
            exclude = ''.join(' AND NOT (book_id=? AND idx=?)' for _ in keys)
            with self.db() as db:
                state = db.execute('SELECT * FROM chapter_cache_state WHERE id=1').fetchone()
                return [dict(row) for row in db.execute("SELECT * FROM chapter_cache WHERE kind='cache' AND offline=0" +
                    (' AND id<=? AND last_used<=?' if clear else '') + exclude + ' ORDER BY last_used,id LIMIT ?',
                    (*((state['clear_until'], state['clear_before']) if clear else ()), *(v for key in keys for v in key), min(8, max(0, limit))))]

    def evict_chapters(self, candidates):
        removed = removed_bytes = 0
        with self._chapter_lock, self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            active = self._reader_keys()
            for row in candidates[:8]:
                key = row['book_id'], row['idx']
                if key in active: continue
                state = db.execute('SELECT max_chapters,cache_count,clear_until,clear_before FROM chapter_cache_state WHERE id=1').fetchone()
                clearing = row['id'] <= state['clear_until'] and row['last_used'] <= state['clear_before']
                if not clearing and state['cache_count'] <= state['max_chapters']: continue
                # shortcut: a single oversized body remains atomic; split storage if that latency becomes material.
                if removed and removed_bytes + row['bytes'] > 256 * 1024: break
                eligible = db.execute("SELECT 1 FROM chapter_cache a JOIN chapters c ON c.book_id=a.book_id AND c.idx=a.idx JOIN books b ON b.id=c.book_id WHERE a.id=? AND a.version=? AND a.kind='cache' AND a.offline=0 AND b.source IN " + ONLINE + ' AND c.original_html IS NULL AND c.original_text IS NULL', (row['id'], row['version'])).fetchone()
                if not eligible: continue
                db.execute("UPDATE chapters SET html='',text='',images='[]',original_html=NULL,original_text=NULL,downloaded=0,raw=json_remove(raw,'$.html','$.text','$.images','$.original_html','$.original_text') WHERE book_id=? AND idx=?", key)
                db.execute('DELETE FROM chapter_search_docs WHERE book_id=? AND idx=?', key)
                db.execute('DELETE FROM chapter_search_pending WHERE book_id=? AND idx=?', key)
                removed += 1
                removed_bytes += row['bytes']
            if removed: db.execute('UPDATE chapter_cache_state SET fts_merge=CASE WHEN fts_merge=0 THEN 1 ELSE fts_merge END WHERE id=1')
        return removed

    def maintain_chapter_cache(self):
        if self._compacting: return 0
        migrated = self._migrate_chapter_cache()
        with self._chapter_lock, self.db() as db:
            state = db.execute('SELECT * FROM chapter_cache_state WHERE id=1').fetchone()
            if not (state['legacy_until'] or state['clear_until'] or state['fts_merge'] or state['cache_count'] > state['max_chapters']):
                return migrated
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT * FROM chapter_cache_state WHERE id=1').fetchone()
            rows = db.execute("SELECT id FROM chapter_cache WHERE kind='legacy' AND id>? AND id<=? ORDER BY id LIMIT 8", (state['legacy_cursor'], state['legacy_until'])).fetchall()
            for row in rows:
                db.execute("UPDATE chapter_cache SET kind='cache',version=version+1 WHERE id=? AND offline=0 AND EXISTS(SELECT 1 FROM chapters c JOIN books b ON b.id=c.book_id WHERE c.book_id=chapter_cache.book_id AND c.idx=chapter_cache.idx AND b.source IN " + ONLINE + ' AND c.original_html IS NULL AND c.original_text IS NULL)', (row['id'],))
            if rows: db.execute('UPDATE chapter_cache_state SET legacy_cursor=? WHERE id=1', (rows[-1]['id'],))
            if len(rows) < 8: db.execute('UPDATE chapter_cache_state SET legacy_until=0 WHERE id=1')
        excess = max(0, state['cache_count'] - state['max_chapters'])
        candidates = self.chapter_cache_candidates(8 if state['clear_until'] else excess, clear=bool(state['clear_until']))
        removed = self.evict_chapters(candidates) if candidates else 0
        with self.db() as db:
            if state['clear_until'] and not candidates:
                # Active leases are reconsidered next pass; newer accesses are outside the clear request.
                if not db.execute("SELECT 1 FROM chapter_cache WHERE kind='cache' AND id<=? AND last_used<=? LIMIT 1", (state['clear_until'], state['clear_before'])).fetchone():
                    db.execute('UPDATE chapter_cache_state SET clear_until=0 WHERE id=1')
            merge = db.execute('SELECT fts_merge FROM chapter_cache_state WHERE id=1').fetchone()[0]
            if merge:
                before = db.total_changes
                db.execute("INSERT INTO chapter_search_fts(chapter_search_fts,rank) VALUES('merge',?)", (-8 if merge == 1 else 8,))
                db.execute('UPDATE chapter_cache_state SET fts_merge=? WHERE id=1', (0 if db.total_changes - before < 2 else 2,))
            if removed and db.execute('PRAGMA auto_vacuum').fetchone()[0] == 2:
                db.execute('PRAGMA incremental_vacuum(16)')
        return migrated + len(rows) + removed + int(bool(merge))

    def compact_database(self, confirmed=False):
        if confirmed is not True: raise ValueError('Cần xác nhận trước khi thu gọn cơ sở dữ liệu.')
        with self._chapter_lock, self._asset_lock, self._database_gate:
            if self._compacting or self._database_users or self._reader_keys() or self._active_assets:
                raise ValueError('Hãy đóng trình đọc và chờ thao tác thư viện kết thúc rồi thử lại.')
            self._compacting = True
        try:
            with contextlib.closing(sqlite3.connect(self.path, timeout=1)) as db:
                db.create_function('shelf_fold', 1, str.casefold, deterministic=True)
                if db.execute("SELECT 1 FROM download_jobs WHERE status IN ('queued','running','verification_required') LIMIT 1").fetchone():
                    raise ValueError('Hãy tạm dừng các tác vụ tải trước khi thu gọn.')
                pages = db.execute('PRAGMA page_count').fetchone()[0] * db.execute('PRAGMA page_size').fetchone()[0]
                if shutil.disk_usage(self.data_dir).free < pages * 2 + 128 * 1024 ** 2:
                    raise ValueError('Không đủ dung lượng trống: cần khoảng hai lần dung lượng SQLite và thêm 128 MiB.')
                if db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0]:
                    raise ValueError('SQLite đang được sử dụng; chưa thể thu gọn an toàn.')
                db.execute("INSERT INTO chapter_search_fts(chapter_search_fts) VALUES('optimize')")
                db.commit()
                db.execute('VACUUM')
                db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        finally:
            with self._database_gate: self._compacting = False
        return self.chapter_cache_stats()
