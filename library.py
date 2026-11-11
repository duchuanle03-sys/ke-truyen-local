"""Private local library. Imports never write to the source backup."""
import contextlib
from collections import Counter
import hashlib
import json
import logging
import math
import os
import re
import sqlite3
import stat
import threading
import time
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, urlsplit

from htmltools import parse_html, safe_remote_url

MAX_JSON = 32 * 1024 * 1024
MAX_BACKUP = 512 * 1024 * 1024
TRASH_RETENTION = 30 * 24 * 60 * 60
_ACTIVE_DOWNLOAD_SQL = "status IN ('queued','running','paused','verification_required')"
_SHELF_SORTS = {'recent': 'read_at', 'added': 'added_at', 'updated': 'updated_at',
                'progress': 'progress', 'chapters': 'chapter_count', 'name': 'shelf_fold(name)'}
_ASSET_NAMES = {'files': r'[0-9a-f]{64}\.[a-z0-9]+',
                'images': r'[0-9a-f]{64}(?:\.[0-9a-f]{32}\.tmp)?'}

def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)

def _source(value):
    text = str(value or "").casefold()
    return next((key for key in ("qq", "full", "hako") if key in text), text)

def _groups(value):
    if value is None or value == -1 or value == "-1":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value if str(v) != "-1"]
    return [str(value)]

def _chapter_key(url):
    return urlsplit(url or "").path.rstrip("/").removesuffix(".html")


def image_urls(values, markup, base):
    if not isinstance(values, (list, tuple)):
        raise ValueError('Invalid chapter images')
    values = list(values)
    for text in markup:
        if text:
            values.extend(node.get('data-original') or node.get('data-src') or node.get('src')
                          for node in parse_html(text).select('img'))
    urls = set()
    for value in values:
        if not isinstance(value, str):
            raise ValueError('Invalid image reference')
        if not value or value.startswith('data:'):
            continue
        parsed = urlsplit(value)
        candidates = (parse_qs(parsed.query).get('url', []) if parsed.path == '/api/image' and
                (not parsed.netloc or parsed.hostname in ('127.0.0.1', 'localhost', '::1')) else [value])
        for url in candidates:
            checked = safe_remote_url(url, base)
            if checked:
                urls.add(checked)
    return sorted(urls)


def _asset_references(row):
    references = set()
    if row['idx'] == -1:
        name = row['path'].replace('\\', '/').rsplit('/', 1)[-1].lower()
        if re.fullmatch(r'[0-9a-f]{64}\.[a-z0-9]+', name):
            references.add(('files', name))
        urls = image_urls([row['cover']], (), row['path'])
    else:
        urls = image_urls(json.loads(row['images']), (row['html'], row['original_html']), row['path'])
    references.update(('images', hashlib.sha256(url.encode()).hexdigest()) for url in urls)
    return references

class Store:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.data_dir / "library.sqlite3"
        self._asset_lock = threading.RLock()
        self._cache_write_lock = threading.RLock()
        # shortcut: in-flight leases cover one server; share leases before supporting multiple servers per library.
        self._active_assets = Counter()
        self._asset_inventory = None
        self._inventory_retry_at = 0
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS books (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, author TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '', path TEXT NOT NULL DEFAULT '',
                    cover TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT 'text',
                    format TEXT NOT NULL DEFAULT 'online', description TEXT NOT NULL DEFAULT '',
                    progress REAL NOT NULL DEFAULT 0, chapter_index INTEGER NOT NULL DEFAULT 0,
                    position REAL NOT NULL DEFAULT 0, groups_json TEXT NOT NULL DEFAULT '[]',
                    archived INTEGER NOT NULL DEFAULT 0, added_at REAL NOT NULL,
                    trashed_at REAL NOT NULL DEFAULT 0,
                    updated_at REAL NOT NULL, read_at REAL NOT NULL DEFAULT 0,
                    chapter_count INTEGER NOT NULL DEFAULT 0, raw TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS chapters (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                    html TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
                    images TEXT NOT NULL DEFAULT '[]', downloaded INTEGER NOT NULL DEFAULT 0,
                    original_html TEXT, original_text TEXT, raw TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY (book_id, idx)
                );
                CREATE TABLE IF NOT EXISTS notes (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL, chapter_index INTEGER NOT NULL DEFAULT 0,
                    quote TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
                    position REAL NOT NULL DEFAULT 0, type TEXT NOT NULL DEFAULT 'note',
                    created_at REAL NOT NULL, raw TEXT NOT NULL DEFAULT '{}'
                );
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS groups_list (id TEXT PRIMARY KEY, name TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS legacy (path TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS download_jobs (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS download_active_book ON download_jobs(book_id)
                    WHERE status IN ('queued','running','paused','verification_required');
                CREATE INDEX IF NOT EXISTS download_queue ON download_jobs(status,created_at,id);
                CREATE INDEX IF NOT EXISTS download_history ON download_jobs(created_at DESC,id DESC);
                CREATE INDEX IF NOT EXISTS download_book ON download_jobs(book_id);
                CREATE INDEX IF NOT EXISTS chapter_book ON chapters(book_id);
                CREATE INDEX IF NOT EXISTS note_book ON notes(book_id);
            """)
            if 'trashed_at' not in {row[1] for row in db.execute('PRAGMA table_info(books)')}:
                db.execute('ALTER TABLE books ADD COLUMN trashed_at REAL NOT NULL DEFAULT 0')
        self._init_asset_index()
        self._init_image_cache()
        self._init_search_index()
        self._init_shelf_index()

    def _init_image_cache(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            first = not db.execute("SELECT 1 FROM sqlite_master WHERE name='image_cache'").fetchone()
            if 'evict' not in {row[1] for row in db.execute('PRAGMA table_info(asset_gc_candidates)')}:
                db.execute('ALTER TABLE asset_gc_candidates ADD COLUMN evict INTEGER NOT NULL DEFAULT 0')
            db.execute('CREATE INDEX IF NOT EXISTS asset_gc_cache ON asset_gc_candidates(evict,retry_at,id)')
            db.execute('CREATE TABLE IF NOT EXISTS image_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, pin_count INTEGER NOT NULL DEFAULT 0, protected INTEGER GENERATED ALWAYS AS (legacy OR pin_count>0) VIRTUAL, evicting INTEGER NOT NULL DEFAULT 0)')
            db.execute('CREATE INDEX IF NOT EXISTS image_cache_lru ON image_cache(protected,evicting,last_used,id)')
            db.execute('CREATE INDEX IF NOT EXISTS image_cache_clear ON image_cache(protected,id)')
            db.execute('CREATE TABLE IF NOT EXISTS image_pins (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, name TEXT NOT NULL REFERENCES image_cache(name) ON DELETE CASCADE, PRIMARY KEY(book_id,idx,name))')
            db.execute('CREATE INDEX IF NOT EXISTS image_pin_lookup ON image_pins(name)')
            db.execute('CREATE TABLE IF NOT EXISTS image_cache_state (id INTEGER PRIMARY KEY CHECK(id=1), max_bytes INTEGER NOT NULL DEFAULT 1073741824, total_bytes INTEGER NOT NULL DEFAULT 0, protected_bytes INTEGER NOT NULL DEFAULT 0, legacy_bytes INTEGER NOT NULL DEFAULT 0, entries INTEGER NOT NULL DEFAULT 0, protected_entries INTEGER NOT NULL DEFAULT 0, pending_bytes INTEGER NOT NULL DEFAULT 0, trim_to INTEGER NOT NULL DEFAULT -1, clear_until INTEGER NOT NULL DEFAULT 0, clear_cursor INTEGER NOT NULL DEFAULT 0)')
            db.execute('INSERT OR IGNORE INTO image_cache_state(id) VALUES(1)')
            for action, event, delta in (
                ('insert', 'INSERT', 'total_bytes=total_bytes+NEW.size,protected_bytes=protected_bytes+NEW.size*NEW.protected,legacy_bytes=legacy_bytes+NEW.size*NEW.legacy,entries=entries+(NEW.size>0),protected_entries=protected_entries+(NEW.size>0)*NEW.protected,pending_bytes=pending_bytes+NEW.size*(1-NEW.protected)*NEW.evicting'),
                ('delete', 'DELETE', 'total_bytes=total_bytes-OLD.size,protected_bytes=protected_bytes-OLD.size*OLD.protected,legacy_bytes=legacy_bytes-OLD.size*OLD.legacy,entries=entries-(OLD.size>0),protected_entries=protected_entries-(OLD.size>0)*OLD.protected,pending_bytes=pending_bytes-OLD.size*(1-OLD.protected)*OLD.evicting'),
                ('update', 'UPDATE OF size,legacy,pin_count,evicting', 'total_bytes=total_bytes+NEW.size-OLD.size,protected_bytes=protected_bytes+NEW.size*NEW.protected-OLD.size*OLD.protected,legacy_bytes=legacy_bytes+NEW.size*NEW.legacy-OLD.size*OLD.legacy,entries=entries+(NEW.size>0)-(OLD.size>0),protected_entries=protected_entries+(NEW.size>0)*NEW.protected-(OLD.size>0)*OLD.protected,pending_bytes=pending_bytes+NEW.size*(1-NEW.protected)*NEW.evicting-OLD.size*(1-OLD.protected)*OLD.evicting')):
                db.execute(f'CREATE TRIGGER IF NOT EXISTS cache_count_{action} AFTER {event} ON image_cache BEGIN UPDATE image_cache_state SET {delta} WHERE id=1; END')
            for sql in (
                'CREATE TRIGGER IF NOT EXISTS image_pin_insert AFTER INSERT ON image_pins BEGIN UPDATE image_cache SET pin_count=pin_count+1 WHERE name=NEW.name; END',
                'CREATE TRIGGER IF NOT EXISTS image_pin_delete AFTER DELETE ON image_pins BEGIN UPDATE image_cache SET pin_count=pin_count-1 WHERE name=OLD.name; END',
                'CREATE TRIGGER IF NOT EXISTS image_pin_chapter_delete AFTER DELETE ON chapters BEGIN DELETE FROM image_pins WHERE book_id=OLD.book_id AND idx=OLD.idx; END',
                "CREATE TRIGGER IF NOT EXISTS image_pin_ref_delete AFTER DELETE ON asset_refs WHEN OLD.kind='images' BEGIN DELETE FROM image_pins WHERE book_id=OLD.book_id AND idx=OLD.idx AND name=OLD.name; END",
                "CREATE TRIGGER IF NOT EXISTS image_pin_file_ref AFTER INSERT ON asset_refs WHEN NEW.kind='images' AND EXISTS(SELECT 1 FROM books WHERE id=NEW.book_id AND source IN ('file','local')) BEGIN INSERT OR IGNORE INTO image_pins SELECT NEW.book_id,NEW.idx,NEW.name WHERE EXISTS(SELECT 1 FROM image_cache WHERE name=NEW.name); END",
                "CREATE TRIGGER IF NOT EXISTS cache_evict_insert AFTER INSERT ON asset_gc_candidates WHEN NEW.kind='images' AND NEW.evict>0 BEGIN UPDATE image_cache SET evicting=1 WHERE name=NEW.name; END",
                "CREATE TRIGGER IF NOT EXISTS cache_evict_update AFTER UPDATE OF evict ON asset_gc_candidates WHEN NEW.kind='images' BEGIN UPDATE image_cache SET evicting=(NEW.evict>0) WHERE name=NEW.name; END",
                "CREATE TRIGGER IF NOT EXISTS cache_evict_delete AFTER DELETE ON asset_gc_candidates WHEN OLD.kind='images' BEGIN UPDATE image_cache SET evicting=0 WHERE name=OLD.name; END",
            ):
                db.execute(sql)
            if first:
                db.execute('UPDATE asset_index_state SET inventory_images=0 WHERE id=1')

    def _init_shelf_index(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            first = not db.execute("SELECT 1 FROM sqlite_master WHERE name='book_groups'").fetchone()
            for sort, key in _SHELF_SORTS.items():
                direction = 'ASC' if sort == 'name' else 'DESC'
                db.execute(f'CREATE INDEX IF NOT EXISTS books_shelf_{sort} ON books('
                           f'(trashed_at>0),archived,{key} {direction},name {direction},id {direction})')
            for column in ('source', 'kind'):
                db.execute(f'CREATE INDEX IF NOT EXISTS books_shelf_{column} ON books('
                           f'(trashed_at>0),archived,{column},read_at DESC,name DESC,id DESC)')
            # Persist casefolded metadata in SQLite; substring queries scan these keys without calling Python per book.
            db.execute("CREATE INDEX IF NOT EXISTS books_shelf_search ON books((trashed_at>0),archived,shelf_fold(name || ' ' || author),id)")
            db.execute('CREATE TABLE IF NOT EXISTS book_groups (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, group_id TEXT NOT NULL, PRIMARY KEY(book_id,group_id))')
            db.execute('CREATE INDEX IF NOT EXISTS book_group_lookup ON book_groups(group_id,book_id)')
            db.execute("CREATE TRIGGER IF NOT EXISTS shelf_group_insert AFTER INSERT ON books BEGIN INSERT OR IGNORE INTO book_groups SELECT NEW.id,value FROM json_each(NEW.groups_json) WHERE type='text'; END")
            db.execute("CREATE TRIGGER IF NOT EXISTS shelf_group_update AFTER UPDATE OF groups_json ON books WHEN NEW.groups_json IS NOT OLD.groups_json BEGIN DELETE FROM book_groups WHERE book_id=NEW.id; INSERT OR IGNORE INTO book_groups SELECT NEW.id,value FROM json_each(NEW.groups_json) WHERE type='text'; END")
            if first:
                db.execute("INSERT OR IGNORE INTO book_groups SELECT b.id,g.value FROM books b,json_each(b.groups_json) g WHERE g.type='text'")

    def _init_search_index(self):
        if sqlite3.sqlite_version_info < (3, 35, 0):
            raise ValueError('Tìm kiếm cần SQLite 3.35 trở lên có FTS5/trigram. Hãy cập nhật Python.')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS chapter_search_docs (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, scope TEXT NOT NULL, literal_check INTEGER NOT NULL, text TEXT NOT NULL, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE)')
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS chapter_search_fts USING fts5(scope,text,content='chapter_search_docs',content_rowid='id',tokenize='trigram case_sensitive 1')")
            db.execute('CREATE TABLE IF NOT EXISTS chapter_search_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE)')
            db.execute("CREATE TABLE IF NOT EXISTS chapter_search_state (id INTEGER PRIMARY KEY CHECK(id=1), book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0)")
            db.execute('INSERT OR IGNORE INTO chapter_search_state(id) VALUES(1)')
            for sql in (
                "CREATE TRIGGER IF NOT EXISTS search_doc_insert AFTER INSERT ON chapter_search_docs BEGIN INSERT INTO chapter_search_fts(rowid,scope,text) VALUES(NEW.id,NEW.scope,NEW.text); END",
                "CREATE TRIGGER IF NOT EXISTS search_doc_delete AFTER DELETE ON chapter_search_docs BEGIN INSERT INTO chapter_search_fts(chapter_search_fts,rowid,scope,text) VALUES('delete',OLD.id,OLD.scope,OLD.text); END",
                "CREATE TRIGGER IF NOT EXISTS search_doc_update AFTER UPDATE OF text ON chapter_search_docs WHEN NEW.text IS NOT OLD.text BEGIN INSERT INTO chapter_search_fts(chapter_search_fts,rowid,scope,text) VALUES('delete',OLD.id,OLD.scope,OLD.text); INSERT INTO chapter_search_fts(rowid,scope,text) VALUES(NEW.id,NEW.scope,NEW.text); END",
                "CREATE TRIGGER IF NOT EXISTS search_chapter_insert AFTER INSERT ON chapters WHEN NEW.downloaded=1 BEGIN INSERT INTO chapter_search_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id; END",
                "CREATE TRIGGER IF NOT EXISTS search_chapter_update AFTER UPDATE OF text,downloaded ON chapters WHEN NEW.text IS NOT OLD.text OR NEW.downloaded IS NOT OLD.downloaded BEGIN INSERT INTO chapter_search_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id; END",
            ):
                db.execute(sql)

    def index_search(self, limit=8, stop=None):
        if stop is not None and stop.is_set():
            return 0
        seeded = 0
        with self.db() as db:
            state = db.execute('SELECT * FROM chapter_search_state WHERE id=1').fetchone()
            if not state['complete']:
                db.execute('BEGIN IMMEDIATE')
                state = db.execute('SELECT * FROM chapter_search_state WHERE id=1').fetchone()
                if not state['complete']:
                    owners = db.execute('SELECT book_id,idx,downloaded FROM chapters WHERE (book_id,idx)>(?,?) ORDER BY book_id,idx LIMIT 256',
                                        (state['book_id'], state['idx'])).fetchall()
                    db.executemany('INSERT OR IGNORE INTO chapter_search_pending(book_id,idx) SELECT ?,? '
                        'WHERE NOT EXISTS(SELECT 1 FROM chapter_search_docs WHERE book_id=? AND idx=?)',
                        ((row['book_id'], row['idx'], row['book_id'], row['idx']) for row in owners if row['downloaded']))
                    if owners:
                        db.execute('UPDATE chapter_search_state SET book_id=?,idx=? WHERE id=1',
                                   (owners[-1]['book_id'], owners[-1]['idx']))
                    if len(owners) < 256:
                        db.execute('UPDATE chapter_search_state SET complete=1 WHERE id=1')
                    seeded = len(owners)
        with self.db() as db:
            owners = db.execute('SELECT * FROM chapter_search_pending ORDER BY id LIMIT ?', (limit,)).fetchall()
        indexed, characters = [], 0
        with self.db() as db:
            for owner in owners:
                if stop is not None and stop.is_set():
                    return 0
                cursor = db.execute('SELECT c.text,c.downloaded FROM chapter_search_pending p JOIN chapters c '
                                    'ON c.book_id=p.book_id AND c.idx=p.idx WHERE p.id=?', (owner['id'],))
                row = cursor.fetchone()
                cursor.close()
                if row is not None:
                    # shortcut: one oversized chapter is indexed atomically; partition it if write-latency measurements require it.
                    normalized = row['text'].casefold() if row['downloaded'] else None
                    indexed.append((owner, normalized))
                    characters += len(normalized or '')
                    if characters >= 256 * 1024:
                        break
        if stop is not None and stop.is_set():
            return 0
        if indexed:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                for owner, normalized in indexed:
                    # A replaced pending ID rejects content changed while normalization was outside the transaction.
                    if not db.execute('DELETE FROM chapter_search_pending WHERE id=?', (owner['id'],)).rowcount:
                        continue
                    if normalized is None:
                        db.execute('DELETE FROM chapter_search_docs WHERE book_id=? AND idx=?', (owner['book_id'], owner['idx']))
                    else:
                        db.execute('INSERT INTO chapter_search_docs(book_id,idx,scope,literal_check,text) VALUES(?,?,?,?,?) '
                            'ON CONFLICT(book_id,idx) DO UPDATE SET literal_check=excluded.literal_check,text=excluded.text',
                            (owner['book_id'], owner['idx'], hashlib.sha256(owner['book_id'].encode()).hexdigest(), int('\x00' in normalized), normalized))
        return seeded + len(owners)

    def _init_asset_index(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            first = not db.execute("SELECT 1 FROM sqlite_master WHERE name='asset_refs'").fetchone()
            for name in ('asset_book_insert', 'asset_book_update', 'asset_book_path',
                         'asset_chapter_insert', 'asset_chapter_update'):
                trigger = db.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?", (name,)).fetchone()
                if trigger and 'INSERT OR REPLACE INTO asset_pending' in trigger[0]:
                    db.execute('DROP TRIGGER ' + name)
            for sql in (
                'CREATE TABLE IF NOT EXISTS asset_refs (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(book_id,idx,kind,name))',
                'CREATE INDEX IF NOT EXISTS asset_lookup ON asset_refs(kind,name)',
                'CREATE TABLE IF NOT EXISTS asset_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0, UNIQUE(book_id,idx))',
                'CREATE INDEX IF NOT EXISTS asset_pending_work ON asset_pending(blocked,id)',
                "CREATE TABLE IF NOT EXISTS asset_index_state (id INTEGER PRIMARY KEY CHECK(id=1), book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0)",
                'CREATE TABLE IF NOT EXISTS asset_gc_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1, retry_at REAL NOT NULL DEFAULT 0, UNIQUE(kind,name))',
                'CREATE INDEX IF NOT EXISTS asset_gc_due ON asset_gc_candidates(retry_at,id)',
                "CREATE TRIGGER IF NOT EXISTS asset_book_insert AFTER INSERT ON books BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.id,-1) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END",
                "CREATE TRIGGER IF NOT EXISTS asset_book_update AFTER UPDATE OF path,cover ON books WHEN NEW.path IS NOT OLD.path OR NEW.cover IS NOT OLD.cover BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.id,-1) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END",
                'CREATE TRIGGER IF NOT EXISTS asset_ref_gc BEFORE DELETE ON asset_refs BEGIN INSERT INTO asset_gc_candidates(kind,name) VALUES(OLD.kind,OLD.name) ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0; END',
                "CREATE TRIGGER IF NOT EXISTS asset_book_path AFTER UPDATE OF path ON books WHEN NEW.path IS NOT OLD.path BEGIN INSERT INTO asset_pending(book_id,idx) SELECT book_id,idx FROM chapters WHERE book_id=NEW.id ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END",
                "CREATE TRIGGER IF NOT EXISTS asset_chapter_insert AFTER INSERT ON chapters WHEN NEW.html!='' OR NEW.original_html IS NOT NULL OR NEW.images!='[]' BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END",
                "CREATE TRIGGER IF NOT EXISTS asset_chapter_update AFTER UPDATE OF html,original_html,images ON chapters WHEN NEW.html IS NOT OLD.html OR NEW.original_html IS NOT OLD.original_html OR NEW.images IS NOT OLD.images BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END",
                'CREATE TRIGGER IF NOT EXISTS asset_chapter_delete AFTER DELETE ON chapters BEGIN DELETE FROM asset_refs WHERE book_id=OLD.book_id AND idx=OLD.idx; DELETE FROM asset_pending WHERE book_id=OLD.book_id AND idx=OLD.idx; END',
            ):
                db.execute(sql)
            db.execute('DROP TRIGGER IF EXISTS asset_book_delete')
            columns = {row[1] for row in db.execute('PRAGMA table_info(asset_index_state)')}
            for column in ('cleanup_requested', 'cleanup_completed'):
                if column in columns:
                    db.execute('ALTER TABLE asset_index_state DROP COLUMN ' + column)
            for column in ('inventory_files', 'inventory_images'):
                if column not in columns:
                    db.execute('ALTER TABLE asset_index_state ADD COLUMN ' + column + ' INTEGER NOT NULL DEFAULT 0')
            if first:
                db.execute('INSERT OR IGNORE INTO asset_pending(book_id,idx) SELECT id,-1 FROM books')
                db.execute('INSERT OR REPLACE INTO asset_index_state(id) VALUES(1)')

    @staticmethod
    def _save_asset_refs(db, book_id, index, references):
        old = {(row['kind'], row['name']) for row in db.execute(
            'SELECT kind,name FROM asset_refs WHERE book_id=? AND idx=?', (book_id, index))}
        db.executemany('DELETE FROM asset_refs WHERE book_id=? AND idx=? AND kind=? AND name=?',
                       ((book_id, index, kind, name) for kind, name in old - references))
        db.executemany('INSERT INTO asset_refs(book_id,idx,kind,name) VALUES(?,?,?,?)',
                       ((book_id, index, kind, name) for kind, name in references - old))

    def _index_book_assets(self, db, book_id):
        row = db.execute('SELECT path,cover,-1 AS idx FROM books WHERE id=?', (book_id,)).fetchone()
        try:
            references = _asset_references(row)
        except (ValueError, TypeError, RecursionError):
            db.execute('UPDATE asset_pending SET blocked=1 WHERE book_id=? AND idx=-1', (book_id,))
            logging.getLogger(__name__).warning('Cannot index book asset references for %s', book_id, exc_info=True)
            return
        self._save_asset_refs(db, book_id, -1, references)
        db.execute('DELETE FROM asset_pending WHERE book_id=? AND idx=-1', (book_id,))

    def index_assets(self, limit=32, stop=None):
        if stop is not None and stop.is_set():
            return 0
        seeded = 0
        with self.db() as db:
            state = db.execute('SELECT * FROM asset_index_state WHERE id=1').fetchone()
        if not state['complete']:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                state = db.execute('SELECT * FROM asset_index_state WHERE id=1').fetchone()
                if not state['complete']:
                    # The primary-key cursor visits legacy owners in bounded batches without reading HTML.
                    owners = db.execute('SELECT book_id,idx FROM chapters WHERE (book_id,idx)>(?,?) ORDER BY book_id,idx LIMIT ?',
                                        (state['book_id'], state['idx'], limit)).fetchall()
                    db.executemany('INSERT OR IGNORE INTO asset_pending(book_id,idx) VALUES(?,?)',
                                   ((row['book_id'], row['idx']) for row in owners))
                    if owners:
                        db.execute('UPDATE asset_index_state SET book_id=?,idx=? WHERE id=1',
                                   (owners[-1]['book_id'], owners[-1]['idx']))
                    if len(owners) < limit:
                        db.execute('UPDATE asset_index_state SET complete=1 WHERE id=1')
                    seeded = len(owners)
        with self.db() as db:
            owners = db.execute('SELECT id AS token,book_id,idx FROM asset_pending '
                                'WHERE blocked=0 ORDER BY id LIMIT ?', (limit,)).fetchall()
        indexed = []
        with self.db() as read_db:
            for owner in owners:
                if stop is not None and stop.is_set():
                    break
                # Autocommit reads; close the cursor before parsing, retaining just one body.
                cursor = read_db.execute("SELECT p.idx,b.path,CASE WHEN p.idx=-1 THEN b.cover ELSE '' END AS cover,c.html,c.original_html,c.images "
                    'FROM asset_pending p JOIN books b ON b.id=p.book_id LEFT JOIN chapters c ON c.book_id=p.book_id AND c.idx=p.idx '
                    'WHERE p.id=?', (owner['token'],))
                row = cursor.fetchone()
                cursor.close()
                if row is None:
                    continue
                try:
                    references = _asset_references(row)
                except (ValueError, TypeError, RecursionError):
                    references = None
                    logging.getLogger(__name__).warning('Cannot index asset references for book %s chapter %s', owner['book_id'], owner['idx'], exc_info=True)
                indexed.append((owner, references))
        if stop is not None and stop.is_set():
            return 0
        if indexed:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                for row, references in indexed:
                    if references is None:
                        db.execute('UPDATE asset_pending SET blocked=1 WHERE id=?', (row['token'],))
                    # A replaced token means the content changed while it was being parsed.
                    elif db.execute('DELETE FROM asset_pending WHERE id=?', (row['token'],)).rowcount:
                        self._save_asset_refs(db, row['book_id'], row['idx'], references)
        return len(owners) + seeded

    def asset_index_ready(self):
        with self.db() as db:
            return (bool(db.execute('SELECT complete FROM asset_index_state WHERE id=1').fetchone()[0])
                    and not db.execute('SELECT 1 FROM asset_pending LIMIT 1').fetchone())

    @contextlib.contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function('shelf_fold', 1, str.casefold, deterministic=True)
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _download_data(job):
        # CAPTCHA IDs belong to an in-memory HTTP session; never restore stale challenges or credentials.
        return _json({key: job[key] for key in ('name', 'start', 'end', 'total', 'completed',
                                               'error', 'error_code', 'access') if key in job})

    @staticmethod
    def _download_row(row):
        job = json.loads(row['data'])
        job.update(id=row['id'], book_id=row['book_id'], status=row['status'],
                   cancelled=bool(row['cancelled']), captcha=None)
        if job['status'] == 'queued':
            job.update(error='')
            job.pop('error_code', None)
            job.pop('access', None)
        return job

    def create_download(self, job):
        now = time.time()
        with self.db() as db:
            db.execute('INSERT INTO download_jobs(id,book_id,status,cancelled,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                       (job['id'], job['book_id'], job['status'], job['cancelled'], self._download_data(job), now, now))

    def save_download(self, job):
        self.save_downloads((job,))

    def save_downloads(self, jobs):
        now = time.time()
        values = [(job['status'], job['cancelled'], self._download_data(job), now, job['id']) for job in jobs]
        with self.db() as db:
            # UPDATE cannot resurrect a job deleted while its worker was finishing.
            db.executemany('UPDATE download_jobs SET status=?,cancelled=?,data=?,updated_at=? WHERE id=?', values)

    def download_job(self, job_id):
        jobs = self.download_jobs_by_ids((job_id,))
        if job_id not in jobs:
            raise KeyError('Không tìm thấy tác vụ.')
        return jobs[job_id]

    def download_jobs_by_ids(self, job_ids):
        if not job_ids:
            return {}
        with self.db() as db:
            rows = db.execute('SELECT * FROM download_jobs WHERE id IN (' + ','.join('?' for _ in job_ids) + ')', job_ids).fetchall()
        return {row['id']: self._download_row(row) for row in rows}

    def download_jobs(self, offset=0, limit=50):
        offset, limit = max(0, int(offset)), min(100, max(1, int(limit)))
        with self.db() as db:
            rows = db.execute('SELECT * FROM download_jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?',
                              (limit + 1, offset)).fetchall()
        return {'jobs': [self._download_row(row) for row in rows[:limit]],
                'next_offset': offset + limit if len(rows) > limit else None}

    def active_download(self, book_id):
        with self.db() as db:
            row = db.execute('SELECT * FROM download_jobs WHERE book_id=? AND ' + _ACTIVE_DOWNLOAD_SQL, (book_id,)).fetchone()
        return self._download_row(row) if row else None

    def active_download_books(self):
        with self.db() as db:
            return {row[0] for row in db.execute('SELECT book_id FROM download_jobs WHERE ' + _ACTIVE_DOWNLOAD_SQL)}

    def queued_downloads(self, exclude=(), limit=2):
        with self.db() as db:
            rows = db.execute("SELECT * FROM download_jobs WHERE status='queued' AND id NOT IN (" +
                              ','.join('?' for _ in exclude) + ') ORDER BY created_at,id LIMIT ?', (*exclude, limit)).fetchall()
        return [self._download_row(row) for row in rows]

    def recover_downloads(self):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE download_jobs SET status='cancelled',cancelled=1 WHERE " + _ACTIVE_DOWNLOAD_SQL +
                       ' AND (cancelled=1 OR EXISTS(SELECT 1 FROM books b WHERE b.id=book_id AND b.trashed_at>0))')
            db.execute("UPDATE download_jobs SET status='queued' WHERE status IN ('running','verification_required')")

    def delete_download(self, job_id):
        with self.db() as db:
            db.execute('DELETE FROM download_jobs WHERE id=?', (job_id,))

    def _book(self, row):
        if row is None:
            raise KeyError("Không tìm thấy truyện.")
        item = dict(row)
        item["groups"] = json.loads(item.pop("groups_json"))
        item["archived"] = bool(item["archived"])
        item['trash_expires_at'] = item['trashed_at'] + TRASH_RETENTION if item['trashed_at'] else None
        metadata = json.loads(item.pop("raw", '{}'))
        for key in ('warnings', 'ocr'):
            if key in metadata:
                item[key] = metadata[key]
        return item

    def list_books(self, query="", source="", group="", sort="recent", archived=False, trashed=False,
                   kind="all", reverse=False, offset=0, limit=60):
        offset, limit = max(0, int(offset)), min(200, max(1, int(limit)))
        if kind not in ('all', 'text', 'comic', 'file'):
            raise ValueError('Loại truyện không hợp lệ.')
        where, values = ['(trashed_at>0)=?'], [int(bool(trashed))]
        if not trashed:
            where.append('archived=?')
            values.append(int(bool(archived)))
        term = query.casefold().strip()
        if term:
            where.append("id IN (SELECT id FROM books INDEXED BY books_shelf_search WHERE " +
                         ' AND '.join(where) + " AND instr(shelf_fold(name || ' ' || author),?)>0)")
            values += [*values, term]
        for column, value in (('source', source), ('kind', kind if kind in ('text', 'comic') else '')):
            if value:
                where.append(column + '=?')
                values.append(value)
        if kind == 'file':
            where.append("source IN ('local','file')")
        if group:
            where.append('id IN (SELECT book_id FROM book_groups WHERE group_id=?)')
            values.append(str(group))
        key = _SHELF_SORTS.get(sort, 'read_at')
        direction = 'ASC' if (key == _SHELF_SORTS['name']) != bool(reverse) else 'DESC'
        predicate = ' AND '.join(where)
        with self.db() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT count(*) FROM books WHERE ' + predicate, values).fetchone()[0]
            shelf_total = db.execute('SELECT count(*) FROM books WHERE (trashed_at>0)=0 AND archived=0').fetchone()[0]
            offset = min(offset, max(0, (total - 1) // limit * limit))
            rows = db.execute(f'SELECT * FROM books WHERE {predicate} ORDER BY {key} {direction},'
                              f'name {direction},id {direction} LIMIT ? OFFSET ?', (*values, limit, offset)).fetchall()
        return {'items': [self._book(row) for row in rows], 'total': total, 'shelf_total': shelf_total,
                'limit': limit, 'offset': offset, 'next_offset': offset + limit if offset + limit < total else None}

    def get_book(self, book_id):
        with self.db() as db:
            return self._book(db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())

    def upsert_book(self, book, chapters=None):
        with self.db() as db:
            return self._put_book(db, book, chapters)

    def _put_book(self, db, book, chapters=None):
        source = _source(book.get("source", book.get("book_source", "")))
        path = str(book.get("path", book.get("url", "")))
        book_id = str(book.get("id") or book.get("book_id") or
                      hashlib.sha256((source + ":" + path).encode()).hexdigest()[:32])
        # A source refresh must reuse imported shelf IDs rather than split reading history.
        existing = db.execute("SELECT id FROM books WHERE source=? AND path=?", (source, path)).fetchone() if path else None
        if not existing and source in ("qq", "full", "hako", "stv") and path:
            existing = next((row for row in db.execute("SELECT id,path FROM books WHERE source=?", (source,))
                             if _chapter_key(row["path"]) == _chapter_key(path)), None)
        if existing and "id" not in book and "book_id" not in book:
            book_id = existing["id"]
        if chapters is not None and source in ('qq', 'full', 'hako', 'stv'):
            old_urls = [_chapter_key(row['url']) for row in db.execute(
                'SELECT url FROM chapters WHERE book_id=? ORDER BY idx', (book_id,))]
            new_urls = [_chapter_key(chapter.get('url', chapter.get('path', ''))) for chapter in chapters]
            if old_urls and (len(new_urls) < len(old_urls) or old_urls != new_urls[:len(old_urls)]):
                raise ValueError('Mục lục nguồn đã đổi thứ tự hoặc thiếu chương. Thư viện, ghi chú và tiến độ cũ được giữ nguyên; cần đối chiếu chương trước khi cập nhật.')
        kind = book.get("kind") or ("comic" if book.get("book_type") == 2 else "text")
        now = time.time()
        count = len(chapters) if chapters is not None else int(book.get("chapter_count", 0) or 0)
        position = float(book.get("position", book.get("last_position", 0)) or 0)
        progress = float(book.get("progress", book.get("percent", 0)) or 0)
        if not math.isfinite(position) or position < 0:
            position = 0
        if not math.isfinite(progress):
            progress = 0
        db.execute("""
            INSERT INTO books(id,name,author,source,path,cover,kind,format,description,
                progress,chapter_index,position,groups_json,added_at,updated_at,read_at,chapter_count,raw)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,author=excluded.author,
                source=excluded.source,path=excluded.path,cover=excluded.cover,kind=excluded.kind,
                format=excluded.format,description=excluded.description,
                chapter_count=excluded.chapter_count,updated_at=excluded.updated_at,
                read_at=CASE WHEN books.read_at=0 THEN excluded.read_at ELSE books.read_at END,raw=excluded.raw
        """, (book_id, str(book.get("name") or "Chưa đặt tên"), str(book.get("author") or ""), source, path,
              str(book.get("cover") or ""), kind, str(book.get("format") or "online"),
              str(book.get("description") or ""), min(1, max(0, progress)),
              max(0, int(book.get("chapter_index", book.get("last_index", 0)) or 0)),
              min(1, position), _json(_groups(book.get("groups", book.get("book_folder")))),
              now, now, float(book.get('read_at', 0) or 0), count, _json(book)))
        self._index_book_assets(db, book_id)
        if chapters is not None:
            for index, chapter in enumerate(chapters):
                title = str(chapter.get("title", chapter.get("name", "")) or "Chương " + str(index + 1))
                url = str(chapter.get("url", chapter.get("path", "")) or "")
                old = db.execute("SELECT url,raw FROM chapters WHERE book_id=? AND idx=?", (book_id, index)).fetchone()
                same_url = old and _chapter_key(old['url']) == _chapter_key(url)
                metadata = json.loads(old['raw']) if same_url else {}
                metadata.update({key: value for key, value in chapter.items()
                                 if value is not None or key not in ('language', 'translation', 'is_vip', 'access')})
                if chapter.get('is_vip') is False:
                    metadata.pop('access', None)
                if old and not same_url:
                    db.execute("UPDATE chapters SET html='',text='',images='[]',downloaded=0,"
                               "original_html=NULL,original_text=NULL WHERE book_id=? AND idx=?", (book_id, index))
                has_body = bool(chapter.get("html") or chapter.get("text") or chapter.get("images"))
                db.execute("""
                    INSERT INTO chapters(book_id,idx,title,url,html,text,images,downloaded,raw)
                    VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(book_id,idx) DO UPDATE SET
                    title=excluded.title,url=excluded.url,raw=excluded.raw
                """, (book_id, index, title, url, chapter.get("html", ""), chapter.get("text", ""),
                      _json(chapter.get("images", [])), int(has_body), _json(metadata)))
        return self._book(db.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone())

    def chapters(self, book_id):
        self.get_book(book_id)
        with self.db() as db:
            result = []
            for row in db.execute('SELECT idx,title,url,downloaded,raw FROM chapters WHERE book_id=? ORDER BY idx', (book_id,)):
                item = dict(row)
                metadata = json.loads(item.pop('raw'))
                if type(metadata.get('is_vip')) is bool:
                    item['is_vip'] = metadata['is_vip']
                item.update(index=item['idx'], downloaded=bool(item['downloaded']))
                result.append(item)
            return result

    def content(self, book_id, index):
        with self.db() as db:
            row = db.execute("SELECT * FROM chapters WHERE book_id=? AND idx=?", (book_id, int(index))).fetchone()
        if row is None:
            raise KeyError("Không tìm thấy chương.")
        item = dict(row)
        item["index"] = item.pop("idx")
        item["images"] = json.loads(item["images"])
        item["downloaded"] = bool(item["downloaded"])
        metadata = json.loads(item.pop('raw'))
        if type(metadata.get('is_vip')) is bool:
            item['is_vip'] = metadata['is_vip']
        if metadata.get('translation') == 'source' and metadata.get('language') == 'vi':
            item.update(translation='source', language='vi')
        return item

    def save_content(self, book_id, index, content):
        self.content(book_id, index)
        with self.db() as db:
            metadata = json.loads(db.execute('SELECT raw FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone()['raw'])
            if content.get('translation') == 'source' and content.get('language') == 'vi':
                metadata.update(translation='source', language='vi')
            metadata.pop('access', None)
            db.execute("UPDATE chapters SET html=?,text=?,images=?,downloaded=1,raw=? WHERE book_id=? AND idx=?",
                       (content.get("html", ""), content.get("text", ""), _json(content.get("images", [])),
                        _json(metadata), book_id, int(index)))
        return self.content(book_id, index)

    def chapter_access(self, book_id, index, access):
        if (access.get('state') != 'vip_locked' or access.get('vip') is not True
                or not isinstance(access.get('evidence'), str) or not access['evidence'].strip()):
            return
        self.content(book_id, index)
        with self.db() as db:
            metadata = json.loads(db.execute('SELECT raw FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone()['raw'])
            metadata.update(is_vip=True, access=access)
            db.execute('UPDATE chapters SET raw=? WHERE book_id=? AND idx=?', (_json(metadata), book_id, index))

    def edit_content(self, book_id, index, content):
        old = self.content(book_id, index)
        if self.get_book(book_id)["kind"] != "text":
            raise ValueError("Chương ảnh không dùng trình sửa văn bản.")
        with self.db() as db:
            db.execute("UPDATE chapters SET original_html=COALESCE(original_html,html),"
                       "original_text=COALESCE(original_text,text),html=?,text=?,title=? "
                       "WHERE book_id=? AND idx=?", (content["html"], content["text"],
                       content.get("title", old["title"]), book_id, int(index)))
        return self.content(book_id, index)

    def restore_content(self, book_id, index):
        self.content(book_id, index)
        with self.db() as db:
            db.execute("UPDATE chapters SET html=COALESCE(original_html,html),"
                       "text=COALESCE(original_text,text),original_html=NULL,original_text=NULL "
                       "WHERE book_id=? AND idx=?", (book_id, int(index)))
        return self.content(book_id, index)

    def progress(self, book_id, index, position):
        index, position = int(index), float(position)
        book = self.get_book(book_id)
        if not math.isfinite(position) or not 0 <= position <= 1 or not 0 <= index < book["chapter_count"]:
            raise ValueError("Vị trí đọc không hợp lệ.")
        with self.db() as db:
            chapter = db.execute('SELECT downloaded,raw FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone()
            access = json.loads(chapter['raw']).get('access') if chapter else None
            if chapter and not chapter['downloaded'] and isinstance(access, dict) and access.get('state') == 'vip_locked':
                raise ValueError('Chương VIP chưa đọc được; vị trí đọc trước đó được giữ nguyên.')
            db.execute("UPDATE books SET chapter_index=?,position=?,progress=?,read_at=? WHERE id=?",
                       (index, position, (index + position) / book["chapter_count"], time.time(), book_id))
        return self.get_book(book_id)

    def update_book(self, book_id, changes):
        self.get_book(book_id)
        allowed = {k: changes[k] for k in ("name", "author", "archived", "cover") if k in changes}
        if "groups" in changes:
            allowed["groups_json"] = _json(_groups(changes["groups"]))
        if allowed:
            with self.db() as db:
                db.execute("UPDATE books SET " + ",".join(k + "=?" for k in allowed) + " WHERE id=?",
                           (*allowed.values(), book_id))
                if 'cover' in allowed:
                    self._index_book_assets(db, book_id)
        return self.get_book(book_id)

    def groups(self):
        with self.db() as db:
            return [dict(row) for row in db.execute("SELECT id,name FROM groups_list ORDER BY name")]

    def trash_book(self, book_id):
        with self.db() as db:
            changed = db.execute('UPDATE books SET trashed_at=CASE WHEN trashed_at=0 THEN ? ELSE trashed_at END WHERE id=?',
                                 (time.time(), book_id)).rowcount
            if not changed:
                raise KeyError('Không tìm thấy truyện.')
        return self.get_book(book_id)

    def restore_book(self, book_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT trashed_at FROM books WHERE id=?', (book_id,)).fetchone()
            if row is None:
                raise KeyError('Không tìm thấy truyện.')
            if row['trashed_at'] and row['trashed_at'] + TRASH_RETENTION <= time.time():
                raise ValueError('Truyện đã hết thời hạn 30 ngày trong thùng rác, không thể khôi phục.')
            db.execute('UPDATE books SET trashed_at=0,archived=0 WHERE id=?', (book_id,))
        return self.get_book(book_id)

    @staticmethod
    def _purge(db, book_id):
        db.execute('DELETE FROM notes WHERE book_id=?', (book_id,))
        db.execute('DELETE FROM books WHERE id=?', (book_id,))

    def purge_book(self, book_id):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT trashed_at FROM books WHERE id=?', (book_id,)).fetchone()
            if row is None:
                raise KeyError('Không tìm thấy truyện.')
            if not row['trashed_at']:
                raise ValueError('Chỉ xóa vĩnh viễn truyện đã nằm trong thùng rác.')
            self._purge(db, book_id)

    def purge_expired(self, now=None, skip_ids=()):
        cutoff = (time.time() if now is None else now) - TRASH_RETENTION
        skipped = set(skip_ids)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            ids = [row['id'] for row in db.execute('SELECT id FROM books WHERE trashed_at>0 AND trashed_at<=?', (cutoff,))
                   if row['id'] not in skipped]
            for book_id in ids:
                self._purge(db, book_id)
        return ids

    @contextlib.contextmanager
    def use_asset(self, path):
        path = Path(path).resolve()
        with self._asset_lock:
            self._active_assets[path] += 1
        try:
            yield
        finally:
            with self._asset_lock:
                self._active_assets[path] -= 1
                if not self._active_assets[path]:
                    del self._active_assets[path]

    def image_cache_stats(self):
        with self.db() as db:
            row = dict(db.execute('SELECT * FROM image_cache_state WHERE id=1').fetchone())
            inventory = bool(db.execute('SELECT inventory_images FROM asset_index_state WHERE id=1').fetchone()[0])
            clearing = bool(row['clear_until'] or db.execute('SELECT 1 FROM asset_gc_candidates WHERE evict=2 LIMIT 1').fetchone())
        return {key: row[key] for key in ('max_bytes', 'total_bytes', 'protected_bytes', 'legacy_bytes', 'entries', 'protected_entries', 'pending_bytes')} | {
            'cache_bytes': row['total_bytes'] - row['protected_bytes'], 'inventory_complete': inventory, 'clearing': clearing}

    def set_image_cache_limit(self, value):
        if type(value) is not int or not 0 <= value <= 64 * 1024 ** 3:
            raise ValueError('Giới hạn cache ảnh phải là số byte nguyên từ 0 đến 64 GiB.')
        with self.db() as db:
            db.execute('UPDATE image_cache_state SET max_bytes=? WHERE id=1', (value,))
        return self.image_cache_stats()

    def clear_image_cache(self):
        with self.db() as db:
            db.execute('UPDATE image_cache_state SET clear_until=COALESCE((SELECT max(id) FROM image_cache),0),clear_cursor=0 WHERE id=1')
        return self.image_cache_stats()

    def _image_cache_name(self, path):
        item = self._asset_path(path)
        if item is None or item[1] != 'images' or not re.fullmatch('[0-9a-f]{64}', item[2]) or item[3]:
            raise ValueError('Đường dẫn cache ảnh không hợp lệ.')
        return item[2]

    def pin_image(self, path, book_id, index):
        name = self._image_cache_name(path)
        with self.db() as db:
            if not db.execute('SELECT 1 FROM chapters WHERE book_id=? AND idx=?', (book_id, index)).fetchone():
                raise KeyError('Không tìm thấy chương để giữ ảnh ngoại tuyến.')
            db.execute('INSERT OR IGNORE INTO image_cache(name,last_used) VALUES(?,?)', (name, time.time()))
            db.execute('INSERT OR IGNORE INTO image_pins(book_id,idx,name) VALUES(?,?,?)', (book_id, index, name))

    def touch_image(self, path, size, legacy=True):
        name = self._image_cache_name(path)
        with self.db() as db:
            db.execute('INSERT INTO image_cache(name,size,last_used,legacy) VALUES(?,?,?,?) '
                       'ON CONFLICT(name) DO UPDATE SET size=excluded.size,last_used=excluded.last_used',
                       (name, size, time.time(), int(legacy)))
            self._pin_file_images(db, name)
            db.execute("UPDATE asset_gc_candidates SET evict=0,generation=generation+1,retry_at=0 WHERE kind='images' AND name=? AND evict=1", (name,))

    @staticmethod
    def _pin_file_images(db, name):
        db.execute("INSERT OR IGNORE INTO image_pins(book_id,idx,name) SELECT r.book_id,r.idx,r.name "
                   "FROM asset_refs r JOIN books b ON b.id=r.book_id WHERE r.kind='images' AND r.name=? AND b.source IN ('file','local')", (name,))

    @contextlib.contextmanager
    def cache_image_write(self, path, size):
        name = self._image_cache_name(path)
        # Only admission/writes serialize; network, cache hits, asset leases and SQLite readers do not wait on this lock.
        with self._cache_write_lock:
            if path.exists():
                yield False
                return
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("INSERT OR IGNORE INTO image_cache(name,last_used) SELECT ?,? WHERE EXISTS("
                           "SELECT 1 FROM asset_refs r JOIN books b ON b.id=r.book_id WHERE r.kind='images' AND r.name=? AND b.source IN ('file','local'))", (name, time.time(), name))
                self._pin_file_images(db, name)
                entry = db.execute('SELECT protected FROM image_cache WHERE name=?', (name,)).fetchone()
                state = db.execute('SELECT * FROM image_cache_state WHERE id=1').fetchone()
                fits = bool(entry and entry['protected']) or state['total_bytes'] - state['protected_bytes'] + size <= state['max_bytes']
                if not fits and size <= state['max_bytes']:
                    target = state['max_bytes'] - size
                    db.execute('UPDATE image_cache_state SET trim_to=CASE WHEN trim_to<0 THEN ? ELSE min(trim_to,?) END WHERE id=1', (target, target))
                if fits:
                    # Reserve before exposing the file, so crash recovery and upgrade inventory know its size and intent.
                    db.execute('INSERT INTO image_cache(name,size,last_used) VALUES(?,?,?) '
                               'ON CONFLICT(name) DO UPDATE SET size=excluded.size,last_used=excluded.last_used', (name, size, time.time()))
                    db.execute("INSERT INTO asset_gc_candidates(kind,name) VALUES('images',?) ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0,evict=0", (name,))
            if not fits:
                yield False
                return
            try:
                yield not path.exists()
            finally:
                if path.exists() and self._regular_asset(path):
                    self.touch_image(path, path.lstat().st_size, legacy=False)
                elif not path.exists():
                    with self.db() as db:
                        db.execute('UPDATE image_cache SET size=0 WHERE name=?', (name,))
                        db.execute('DELETE FROM image_cache WHERE name=? AND protected=0', (name,))

    def queue_cache_evictions(self, limit=32, stop=None):
        if (stop is not None and stop.is_set()) or not self.asset_index_ready():
            return 0
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            state = db.execute('SELECT * FROM image_cache_state WHERE id=1').fetchone()
            if state['clear_until']:
                rows = db.execute('SELECT * FROM image_cache WHERE protected=0 AND id>? AND id<=? '
                                  'ORDER BY id LIMIT ?', (state['clear_cursor'], state['clear_until'], limit)).fetchall()
                db.execute('UPDATE image_cache_state SET clear_cursor=?,clear_until=? WHERE id=1',
                           (rows[-1]['id'] if rows else 0, state['clear_until'] if len(rows) == limit else 0))
                mode, needed = 2, None
            else:
                target = min(state['max_bytes'], state['trim_to']) if state['trim_to'] >= 0 else state['max_bytes']
                remaining = state['total_bytes'] - state['protected_bytes']
                needed = remaining - state['pending_bytes'] - target
                if needed <= 0:
                    if remaining <= target and state['trim_to'] >= 0:
                        db.execute('UPDATE image_cache_state SET trim_to=-1 WHERE id=1')
                    return 0
                rows = db.execute('SELECT * FROM image_cache WHERE protected=0 AND evicting=0 AND size>0 '
                                  'ORDER BY last_used,id LIMIT ?', (limit,)).fetchall()
                mode = 1
            queued = 0
            for row in rows:
                db.execute("INSERT INTO asset_gc_candidates(kind,name,evict) VALUES('images',?,?) "
                           'ON CONFLICT(kind,name) DO UPDATE SET evict=max(evict,excluded.evict),generation=generation+1,retry_at=0', (row['name'], mode))
                queued += 1
                if needed is not None:
                    needed -= row['size']
                    if needed <= 0:
                        break
            return queued

    def _asset_path(self, path):
        path = Path(path)
        kind = path.parent.name
        if kind not in _ASSET_NAMES or path.parent != self.data_dir / kind:
            return None
        match = re.fullmatch('(' + _ASSET_NAMES[kind] + r')(\.[0-9a-f]{32}\.gc)?', path.name)
        return (path, kind, match[1], bool(match[2])) if match else None

    def queue_assets(self, paths):
        keys = {(item[1], item[0].name) for path in paths if (item := self._asset_path(path))}
        if keys:
            with self.db() as db:
                db.executemany('INSERT INTO asset_gc_candidates(kind,name) VALUES(?,?) '
                    'ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0,evict=0', keys)

    def close_inventory(self):
        if self._asset_inventory is not None:
            self._asset_inventory[1].close()
            self._asset_inventory = None

    def inventory_assets(self, limit=256, stop=None):
        # Upgrade recovery only. Folder completion is durable; an interrupted folder can be replayed safely.
        if (stop is not None and stop.is_set()) or time.monotonic() < self._inventory_retry_at:
            return 0
        with self.db() as db:
            state = db.execute('SELECT inventory_files,inventory_images FROM asset_index_state WHERE id=1').fetchone()
        kind = next((name for name in ('files', 'images') if not state['inventory_' + name]), None)
        if kind is None:
            self.close_inventory()
            return 0
        paths, cached, exhausted = [], [], False
        try:
            if self._asset_inventory is None or self._asset_inventory[0] != kind:
                self.close_inventory()
                directory = self.data_dir / kind
                info = directory.lstat()
                if (not stat.S_ISDIR(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400
                        or directory.resolve() != directory):
                    exhausted = True
                else:
                    self._asset_inventory = (kind, os.scandir(directory))
            if not exhausted:
                for _ in range(limit):
                    if stop is not None and stop.is_set():
                        self.close_inventory()
                        return 0
                    entry = next(self._asset_inventory[1], None)
                    if entry is None:
                        exhausted = True
                        break
                    item = self._asset_path(Path(entry.path))
                    if item:
                        paths.append((kind, item[0].name))
                        if kind == 'images' and re.fullmatch('[0-9a-f]{64}', entry.name) and self._regular_asset(item[0]):
                            info = entry.stat(follow_symlinks=False)
                            cached.append((entry.name, info.st_size, info.st_mtime))
        except FileNotFoundError:
            exhausted = True
        except OSError:
            self.close_inventory()
            self._inventory_retry_at = time.monotonic() + 30
            logging.getLogger(__name__).warning('Cannot inventory asset directory %s', kind, exc_info=True)
            return 0
        try:
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                db.executemany('INSERT OR IGNORE INTO asset_gc_candidates(kind,name) '
                    'SELECT ?,? WHERE NOT EXISTS(SELECT 1 FROM asset_refs WHERE kind=? AND name=?)',
                    ((kind, name, kind, name) for kind, name in paths))
                db.executemany('INSERT OR IGNORE INTO image_cache(name,size,last_used,legacy) VALUES(?,?,?,1)', cached)
                if exhausted:
                    db.execute('UPDATE asset_index_state SET inventory_' + kind + '=1 WHERE id=1')
        except sqlite3.Error:
            # Rewind the folder if enqueueing failed after advancing its filesystem iterator.
            self.close_inventory()
            raise
        if exhausted:
            self.close_inventory()
        return max(1, len(paths))

    @staticmethod
    def _regular_asset(path):
        directory = path.parent
        folder, info = directory.lstat(), path.lstat()
        return (stat.S_ISDIR(folder.st_mode) and not getattr(folder, 'st_file_attributes', 0) & 0x400
                and directory.resolve() == directory and stat.S_ISREG(info.st_mode)
                and not getattr(info, 'st_file_attributes', 0) & 0x400 and path.resolve().parent == directory)

    @staticmethod
    def _retry_asset(db, row, seconds):
        db.execute('UPDATE asset_gc_candidates SET retry_at=? WHERE id=? AND generation=?',
                   (time.time() + seconds, row['id'], row['generation']))

    def _cleanup_candidate(self, row, db):
        path = self.data_dir / row['kind'] / row['name']
        item = self._asset_path(path)
        if item is None:
            with db:
                db.execute('DELETE FROM asset_gc_candidates WHERE id=? AND generation=?', (row['id'], row['generation']))
            return True
        _, kind, original_name, legacy_stage = item
        original = path.parent / original_name
        asset = path.parent / (original_name[:64] if kind == 'images' else original_name)
        # A stable candidate ID recovers a rename even if the process exits before SQLite commits.
        staged = path if legacy_stage else path.with_name(path.name + '.gc-' + str(row['id']))
        try:
            with db:
                db.execute('BEGIN IMMEDIATE')
                with self._asset_lock:
                    current = db.execute('SELECT 1 FROM asset_gc_candidates WHERE id=? AND generation=?',
                                         (row['id'], row['generation'])).fetchone()
                    if not current:
                        return True
                    if (not db.execute('SELECT complete FROM asset_index_state WHERE id=1').fetchone()[0]
                            or db.execute('SELECT 1 FROM asset_pending LIMIT 1').fetchone()):
                        return False
                    if asset in self._active_assets:
                        self._retry_asset(db, row, 1)
                        return True
                    referenced = db.execute('SELECT 1 FROM asset_refs WHERE kind=? AND name=? LIMIT 1',
                                            (kind, original_name)).fetchone()
                    cache = db.execute('SELECT protected FROM image_cache WHERE name=?', (original_name,)).fetchone() if kind == 'images' else None
                    evict = row['evict'] and cache is not None and not cache['protected']
                    if (referenced and not evict) or (row['evict'] and not evict):
                        if staged.exists() and not original.exists() and self._regular_asset(staged):
                            staged.rename(original)
                        if not staged.exists():
                            if kind == 'images' and not original.exists():
                                db.execute('UPDATE image_cache SET size=0 WHERE name=?', (original_name,))
                            db.execute('DELETE FROM asset_gc_candidates WHERE id=? AND generation=?',
                                       (row['id'], row['generation']))
                            return True
                    elif not staged.exists():
                        if not path.exists():
                            if kind == 'images':
                                db.execute('DELETE FROM image_cache WHERE name=?', (original_name,))
                            db.execute('DELETE FROM asset_gc_candidates WHERE id=? AND generation=?',
                                       (row['id'], row['generation']))
                            return True
                        if not self._regular_asset(path):
                            self._retry_asset(db, row, 30)
                            return True
                        path.rename(staged)
                    if not self._regular_asset(staged):
                        self._retry_asset(db, row, 30)
                        return True
            # Slow deletion never holds a database transaction or the asset-registration lock.
            staged.unlink()
            with db:
                db.execute('BEGIN IMMEDIATE')
                with self._asset_lock:
                    if not db.execute('SELECT 1 FROM asset_gc_candidates WHERE id=? AND generation=?',
                                      (row['id'], row['generation'])).fetchone():
                        return True
                    if asset in self._active_assets:
                        self._retry_asset(db, row, 1)
                    elif original != staged and original.exists():
                        db.execute('UPDATE asset_gc_candidates SET retry_at=0 WHERE id=? AND generation=?',
                                   (row['id'], row['generation']))
                    else:
                        if kind == 'images':
                            db.execute('UPDATE image_cache SET size=0 WHERE name=? AND protected=1', (original_name,))
                            db.execute('DELETE FROM image_cache WHERE name=? AND protected=0', (original_name,))
                        db.execute('DELETE FROM asset_gc_candidates WHERE id=? AND generation=?',
                                   (row['id'], row['generation']))
        except FileNotFoundError:
            with db:
                self._retry_asset(db, row, 1)
        except (OSError, RuntimeError):
            with db:
                self._retry_asset(db, row, 30)
            logging.getLogger(__name__).warning('Cannot remove unused asset %s', path, exc_info=True)
        return True

    def cleanup_file(self, path):
        self.queue_assets((path,))
        item = self._asset_path(path)
        if item is None:
            return True
        with self.db() as db:
            row = db.execute('SELECT * FROM asset_gc_candidates WHERE kind=? AND name=?',
                             (item[1], item[0].name)).fetchone()
            return self._cleanup_candidate(row, db) if row else True

    def cleanup_files(self, stop=None, limit=32):
        if stop is not None and stop.is_set():
            return 0
        if not self.asset_index_ready():
            logging.getLogger(__name__).warning('Skipping file cleanup: asset references are not indexed yet')
            return 0
        processed = 0
        with self.db() as db:
            rows = db.execute('SELECT * FROM asset_gc_candidates WHERE retry_at<=? '
                              'ORDER BY retry_at,id LIMIT ?', (time.time(), limit)).fetchall()
            for row in rows:
                if stop is not None and stop.is_set():
                    break
                if not self._cleanup_candidate(row, db):
                    break
                processed += 1
        return processed

    def settings(self, values=None):
        with self.db() as db:
            if values is not None:
                for key, value in values.items():
                    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                               (str(key), _json(value)))
            return {r["key"]: json.loads(r["value"]) for r in db.execute("SELECT * FROM settings")}

    def notes(self, book_id=None):
        with self.db() as db:
            rows = db.execute("SELECT * FROM notes" + (" WHERE book_id=?" if book_id else "") +
                              " ORDER BY created_at DESC", (book_id,) if book_id else ()).fetchall()
        return [dict(r) for r in rows]

    def save_note(self, note):
        book_id = str(note.get("book_id") or "")
        if not book_id:
            raise ValueError("Thiếu truyện của ghi chú.")
        note_id = str(note.get("id") or uuid.uuid4().hex)
        position = float(note.get("position", 0) or 0)
        if not math.isfinite(position):
            raise ValueError("Vị trí ghi chú không hợp lệ.")
        with self.db() as db:
            db.execute("""
                INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET quote=excluded.quote,note=excluded.note,
                    color=excluded.color,position=excluded.position
            """, (note_id, book_id, int(note.get("chapter_index", 0)), str(note.get("quote", "")),
                  str(note.get("note", "")), str(note.get("color", "")), position,
                  str(note.get("type", "note")), time.time(), _json(note)))
        return next(r for r in self.notes() if r["id"] == note_id)

    def delete_note(self, note_id):
        with self.db() as db:
            db.execute("DELETE FROM notes WHERE id=?", (note_id,))

    def search_content(self, book_id, query, after=-1, limit=100):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("Nhập cụm từ cần tìm.")
        if len(query) > 500 or '\x00' in query:
            raise ValueError('Cụm từ tối đa 500 ký tự và không chứa ký tự NUL.')
        after, limit = int(after), min(100, max(1, int(limit)))
        if not -1 <= after < 2**63:
            raise ValueError('Vị trí phân trang không hợp lệ.')
        term = query.casefold()
        with self.db() as db:
            db.execute('BEGIN')
            book = db.execute('SELECT chapter_count FROM books WHERE id=?', (book_id,)).fetchone()
            if book is None:
                raise KeyError('Không tìm thấy truyện.')
            state = db.execute('SELECT * FROM chapter_search_state WHERE id=1').fetchone()
            seeded = (state['complete'] or state['book_id'] > book_id or
                      (state['book_id'] == book_id and not db.execute(
                          'SELECT 1 FROM chapters WHERE book_id=? AND idx>? LIMIT 1', (book_id, state['idx'])).fetchone()))
            indexing = (not seeded or bool(db.execute(
                'SELECT 1 FROM chapter_search_pending WHERE book_id=? LIMIT 1', (book_id,)).fetchone()))
            clean = 'NOT EXISTS(SELECT 1 FROM chapter_search_pending p WHERE p.book_id=d.book_id AND p.idx=d.idx)'
            searched = db.execute('SELECT count(*) FROM chapter_search_docs d WHERE d.book_id=? AND ' + clean, (book_id,)).fetchone()[0]
            if len(term) >= 3:
                scope = hashlib.sha256(book_id.encode()).hexdigest()
                match = 'scope:"' + scope + '" AND text:"' + term.replace('"', '""') + '"'
                # The trigram tokenizer skips NULs; only affected documents need a literal residual check.
                source, condition, parameters = ('chapter_search_fts CROSS JOIN chapter_search_docs d ON d.id=chapter_search_fts.rowid',
                    'chapter_search_fts MATCH ? AND (d.literal_check=0 OR instr(d.text,?)>0) AND ', (match, term))
            else:
                # shortcut: one/two-character queries scan only this book's normalized index; index short grams if measured demand justifies their disk cost.
                source, condition, parameters = 'chapter_search_docs d', 'instr(d.text,?)>0 AND ', (term,)
            rows = db.execute('WITH hits AS MATERIALIZED (SELECT d.book_id,d.idx FROM ' + source + ' WHERE ' + condition +
                'd.book_id=? AND d.idx>? AND ' + clean + ' ORDER BY d.idx LIMIT ?) '
                'SELECT c.idx,c.title,c.text FROM hits JOIN chapters c ON c.book_id=hits.book_id AND c.idx=hits.idx '
                'WHERE c.downloaded=1 ORDER BY c.idx', (*parameters, book_id, after, limit + 1)).fetchall()
        results = []
        for row in rows[:limit]:
            text = row['text']
            folded = text.casefold()
            at = folded.find(term)
            if len(folded) != len(text):
                position = 0
                for i, character in enumerate(text):
                    position += len(character.casefold())
                    if position > at:
                        at = i
                        break
            results.append({'index': row['idx'], 'title': row['title'],
                            'snippet': text[max(0, at - 70):at + len(term) + 120]})
        return {'results': results, 'searched': searched, 'total': book['chapter_count'],
                'indexing': indexing, 'next_index': rows[limit - 1]['idx'] if len(rows) > limit else None}

    def import_backup(self, source):
        source = Path(source).expanduser().resolve()
        archive = None
        if source.is_file():
            if not zipfile.is_zipfile(source):
                raise ValueError("Bản sao lưu phải là thư mục hoặc ZIP.")
            archive = zipfile.ZipFile(source)
            infos = archive.infolist()
            if len(infos) > 200000 or sum(i.file_size for i in infos) > MAX_BACKUP:
                archive.close()
                raise ValueError("Bản sao lưu vượt giới hạn xử lý an toàn.")
            for info in infos:
                p = PurePosixPath(info.filename.replace("\\", "/"))
                if p.is_absolute() or ".." in p.parts or ":" in info.filename or stat.S_ISLNK(info.external_attr >> 16):
                    archive.close()
                    raise ValueError("Đường dẫn trong ZIP không an toàn.")
            names = {i.filename: i for i in infos}
            entries = sorted(n for n in names if re_book_path(n))
            if not entries:
                archive.close()
                raise ValueError("Không tìm thấy books/*/book.json trong bản sao lưu.")
            roots = {PurePosixPath(name).parents[2] for name in entries}
            if len(roots) != 1:
                archive.close()
                raise ValueError("ZIP chứa nhiều bản sao lưu. Hãy chọn riêng một bản.")
            root = roots.pop().as_posix()
            prefix = '' if root == '.' else root + '/'
            def read(relative, default=None):
                name = prefix + relative
                if name not in names:
                    return default
                if names[name].file_size > MAX_JSON:
                    raise ValueError("Tệp JSON quá lớn: " + relative)
                return json.loads(archive.read(name).decode("utf-8-sig"))
            book_paths = [n[len(prefix):] for n in entries]
        elif source.is_dir():
            if not (source / "books").is_dir():
                raise ValueError("Chọn thư mục chứa books, data và prefs.")
            def read(relative, default=None):
                path = (source / relative).resolve()
                if not path.is_relative_to(source):
                    raise ValueError("Liên kết trong backup đi ra ngoài thư mục.")
                if not path.is_file():
                    return default
                if path.stat().st_size > MAX_JSON:
                    raise ValueError("Tệp JSON quá lớn: " + relative)
                return json.loads(path.read_text(encoding="utf-8-sig"))
            book_paths = sorted(str(p.relative_to(source)).replace("\\", "/")
                                for p in (source / "books").glob("*/book.json"))
        else:
            raise ValueError("Không tìm thấy đường dẫn bản sao lưu.")
        report = {"books": 0, "chapters": 0, "annotations": 0, "orphan_annotations": 0, "warnings": []}
        try:
            with self.db() as db:
                for relative in book_paths:
                    book = read(relative)
                    if not isinstance(book, dict) or not book.get("book_id"):
                        raise ValueError("Dữ liệu truyện không hợp lệ: " + relative)
                    folder = relative.rsplit("/", 1)[0]
                    chapters = read(folder + "/chapters.json", [])
                    if not isinstance(chapters, list):
                        raise ValueError("Mục lục không hợp lệ: " + folder)
                    novel = read(folder + "/novel.json", {})
                    self._put_book(db, dict(book, description=novel.get("description", ""),
                                           read_at=float(book.get('timestamp', 0) or 0) / 1000), chapters)
                    for leaf in ("book.json", "chapters.json", "novel.json", "names.json", "pages.json"):
                        value = read(folder + "/" + leaf)
                        if value is not None:
                            db.execute("INSERT OR IGNORE INTO legacy VALUES(?,?)", (folder + "/" + leaf, _json(value)))
                    report["books"] += 1
                    report["chapters"] += len(chapters)
                    if book.get("chapter_count") and book["chapter_count"] != len(chapters):
                        report["warnings"].append("Số chương khai báo khác mục lục: " + str(book.get("name", book["book_id"])))
                prefs = read("prefs/setting.json", {})
                groups = prefs.get("shelf.group.list", "{}")
                if isinstance(groups, str):
                    groups = json.loads(groups)
                for key, name in groups.items():
                    db.execute("INSERT OR IGNORE INTO groups_list VALUES(?,?)", (str(key), str(name)))
                for leaf in ("notes.json", "bookmarks.json"):
                    for item in read("data/" + leaf, []):
                        book_id = str(item.get("book_id", item.get("name", "")))
                        note_id = "legacy-" + leaf + "-" + str(item.get("id", item.get("a", uuid.uuid4().hex)))
                        db.execute("INSERT OR IGNORE INTO notes VALUES(?,?,?,?,?,?,?,?,?,?)", (
                            note_id, book_id, int(item.get("chap_index", 0)), str(item.get("content", "")),
                            str(item.get("title", "")), "", float(item.get("position", 0) or 0),
                            "bookmark" if leaf == "bookmarks.json" else "note",
                            float(item.get("timestamp", 0) or 0) / 1000, _json(item)))
                        report["annotations"] += 1
                        if not db.execute("SELECT 1 FROM books WHERE id=?", (book_id,)).fetchone():
                            report["orphan_annotations"] += 1
                for folder, leaves in {"prefs": ["setting.json", "book.json", "novel.json", "comic.json", "tts.json"],
                                       "data": ["notes.json", "bookmarks.json", "names.json", "downloads.json",
                                                "repositories.json", "chinese_names.json", "chinese_vietphrases.json",
                                                "trashes.json"]}.items():
                    for leaf in leaves:
                        value = read(folder + "/" + leaf)
                        if value is not None:
                            db.execute("INSERT OR IGNORE INTO legacy VALUES(?,?)", (folder + "/" + leaf, _json(value)))
                report["groups"] = len(groups)
        finally:
            if archive:
                archive.close()
        return report

def re_book_path(name):
    parts = PurePosixPath(name).parts
    return len(parts) >= 3 and parts[-3] == "books" and parts[-1] == "book.json"
