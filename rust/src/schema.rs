use crate::ApiError;
use rusqlite::{Connection, TransactionBehavior};
use sha2::{Digest, Sha256};

const CURRENT: &str = "55fd8c47098a02d88d4cade8ebd04cf4fb40be8d75f0e501643b34b35d9580f0";
// SQLite appends trashed_at when upgrading the older books table in place.
const TRASH_UPGRADE: &str = "33f261c9c9334bbd887b55dac5af5b5864a68f9d1d1aff3a0fccd7942892b5ea";

fn signature(db: &Connection) -> Result<String, ApiError> {
    let mut stmt =
        db.prepare("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name")?;
    let sql = stmt
        .query_map([], |row| row.get::<_, String>(0))?
        .collect::<Result<Vec<_>, _>>()?
        .join("\n");
    Ok(format!("{:x}", Sha256::digest(sql.as_bytes())))
}

fn unsupported() -> ApiError {
    ApiError::new(400, "Unsupported writable fixture schema")
}

pub(crate) fn upgrade(db: &mut Connection) -> Result<(), ApiError> {
    let before = signature(db)?;
    let (script, expected) = match before.as_str() {
        CURRENT | TRASH_UPGRADE => return Ok(()),
        "07dcdf2f8988c05e1ca1577a057e943cb33128e14b0e92dfe55191ebd582bd1b" => {
            (TRASH, TRASH_UPGRADE)
        }
        "e583a7447114f0f07e28ccc2f1f646577f275d1a2bf63f169968f0247666c718" => (SEARCH, CURRENT),
        "58ecdff1871923abfea22b2bf6757772acbb248371510c9f44a24e2e6e56d45a" => (ASSETS, CURRENT),
        "9c46a9ba015a9962fbccf0de2d0964ea18475b8f72941e403fa62dffacb4dc51" => (INVENTORY, CURRENT),
        _ => return Err(unsupported()),
    };
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    if signature(&tx)? != before {
        return Err(unsupported());
    }
    tx.execute_batch(script)?;
    if signature(&tx)? != expected {
        return Err(unsupported());
    }
    tx.commit()?;
    Ok(())
}

/// Create the current catalog in an empty database (first run), in one transaction.
pub(crate) fn create(db: &mut Connection) -> Result<(), ApiError> {
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let objects: i64 = tx.query_row("SELECT count(*) FROM sqlite_master", [], |r| r.get(0))?;
    if objects != 0 {
        return Err(unsupported());
    }
    tx.execute_batch(CREATE)?;
    if signature(&tx)? != CURRENT {
        return Err(unsupported());
    }
    tx.commit()?;
    Ok(())
}

// Fresh-library DDL and singleton rows transcribed from a new Python `Store` (pinned catalog).
const CREATE: &str = r##"
CREATE TABLE books (
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
CREATE TABLE chapters (
                    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    idx INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '', url TEXT NOT NULL DEFAULT '',
                    html TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
                    images TEXT NOT NULL DEFAULT '[]', downloaded INTEGER NOT NULL DEFAULT 0,
                    original_html TEXT, original_text TEXT, raw TEXT NOT NULL DEFAULT '{}', revision INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (book_id, idx)
                );
CREATE TABLE notes (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL, chapter_index INTEGER NOT NULL DEFAULT 0,
                    quote TEXT NOT NULL DEFAULT '', note TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '',
                    position REAL NOT NULL DEFAULT 0, type TEXT NOT NULL DEFAULT 'note',
                    created_at REAL NOT NULL, raw TEXT NOT NULL DEFAULT '{}'
                );
CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE groups_list (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE legacy (path TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE download_jobs (
                    id TEXT PRIMARY KEY, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
                    status TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0, data TEXT NOT NULL,
                    created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
CREATE UNIQUE INDEX download_active_book ON download_jobs(book_id)
                    WHERE status IN ('queued','running','paused','verification_required');
CREATE INDEX download_queue ON download_jobs(status,created_at,id);
CREATE INDEX download_history ON download_jobs(created_at DESC,id DESC);
CREATE INDEX download_book ON download_jobs(book_id);
CREATE INDEX chapter_book ON chapters(book_id);
CREATE INDEX note_book ON notes(book_id);
CREATE TABLE asset_refs (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(book_id,idx,kind,name));
CREATE INDEX asset_lookup ON asset_refs(kind,name);
CREATE TABLE asset_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0, UNIQUE(book_id,idx));
CREATE INDEX asset_pending_work ON asset_pending(blocked,id);
CREATE TABLE asset_index_state (id INTEGER PRIMARY KEY CHECK(id=1), book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0, inventory_files INTEGER NOT NULL DEFAULT 0, inventory_images INTEGER NOT NULL DEFAULT 0);
CREATE TABLE asset_gc_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1, retry_at REAL NOT NULL DEFAULT 0, evict INTEGER NOT NULL DEFAULT 0, UNIQUE(kind,name));
CREATE INDEX asset_gc_due ON asset_gc_candidates(retry_at,id);
CREATE TRIGGER asset_book_insert AFTER INSERT ON books BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.id,-1) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_book_update AFTER UPDATE OF path,cover ON books WHEN NEW.path IS NOT OLD.path OR NEW.cover IS NOT OLD.cover BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.id,-1) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_ref_gc BEFORE DELETE ON asset_refs BEGIN INSERT INTO asset_gc_candidates(kind,name) VALUES(OLD.kind,OLD.name) ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0; END;
CREATE TRIGGER asset_book_path AFTER UPDATE OF path ON books WHEN NEW.path IS NOT OLD.path BEGIN INSERT INTO asset_pending(book_id,idx) SELECT book_id,idx FROM chapters WHERE book_id=NEW.id ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_chapter_insert AFTER INSERT ON chapters WHEN NEW.html!='' OR NEW.original_html IS NOT NULL OR NEW.images!='[]' BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_chapter_update AFTER UPDATE OF html,original_html,images ON chapters WHEN NEW.html IS NOT OLD.html OR NEW.original_html IS NOT OLD.original_html OR NEW.images IS NOT OLD.images BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_chapter_delete AFTER DELETE ON chapters BEGIN DELETE FROM asset_refs WHERE book_id=OLD.book_id AND idx=OLD.idx; DELETE FROM asset_pending WHERE book_id=OLD.book_id AND idx=OLD.idx; END;
CREATE INDEX asset_gc_cache ON asset_gc_candidates(evict,retry_at,id);
CREATE TABLE image_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE, size INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL, legacy INTEGER NOT NULL DEFAULT 0, pin_count INTEGER NOT NULL DEFAULT 0, protected INTEGER GENERATED ALWAYS AS (legacy OR pin_count>0) VIRTUAL, evicting INTEGER NOT NULL DEFAULT 0);
CREATE INDEX image_cache_lru ON image_cache(protected,evicting,last_used,id);
CREATE INDEX image_cache_clear ON image_cache(protected,id);
CREATE TABLE image_pins (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, name TEXT NOT NULL REFERENCES image_cache(name) ON DELETE CASCADE, PRIMARY KEY(book_id,idx,name));
CREATE INDEX image_pin_lookup ON image_pins(name);
CREATE TABLE image_cache_state (id INTEGER PRIMARY KEY CHECK(id=1), max_bytes INTEGER NOT NULL DEFAULT 1073741824, total_bytes INTEGER NOT NULL DEFAULT 0, protected_bytes INTEGER NOT NULL DEFAULT 0, legacy_bytes INTEGER NOT NULL DEFAULT 0, entries INTEGER NOT NULL DEFAULT 0, protected_entries INTEGER NOT NULL DEFAULT 0, pending_bytes INTEGER NOT NULL DEFAULT 0, trim_to INTEGER NOT NULL DEFAULT -1, clear_until INTEGER NOT NULL DEFAULT 0, clear_cursor INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER cache_count_insert AFTER INSERT ON image_cache BEGIN UPDATE image_cache_state SET total_bytes=total_bytes+NEW.size,protected_bytes=protected_bytes+NEW.size*NEW.protected,legacy_bytes=legacy_bytes+NEW.size*NEW.legacy,entries=entries+(NEW.size>0),protected_entries=protected_entries+(NEW.size>0)*NEW.protected,pending_bytes=pending_bytes+NEW.size*(1-NEW.protected)*NEW.evicting WHERE id=1; END;
CREATE TRIGGER cache_count_delete AFTER DELETE ON image_cache BEGIN UPDATE image_cache_state SET total_bytes=total_bytes-OLD.size,protected_bytes=protected_bytes-OLD.size*OLD.protected,legacy_bytes=legacy_bytes-OLD.size*OLD.legacy,entries=entries-(OLD.size>0),protected_entries=protected_entries-(OLD.size>0)*OLD.protected,pending_bytes=pending_bytes-OLD.size*(1-OLD.protected)*OLD.evicting WHERE id=1; END;
CREATE TRIGGER cache_count_update AFTER UPDATE OF size,legacy,pin_count,evicting ON image_cache BEGIN UPDATE image_cache_state SET total_bytes=total_bytes+NEW.size-OLD.size,protected_bytes=protected_bytes+NEW.size*NEW.protected-OLD.size*OLD.protected,legacy_bytes=legacy_bytes+NEW.size*NEW.legacy-OLD.size*OLD.legacy,entries=entries+(NEW.size>0)-(OLD.size>0),protected_entries=protected_entries+(NEW.size>0)*NEW.protected-(OLD.size>0)*OLD.protected,pending_bytes=pending_bytes+NEW.size*(1-NEW.protected)*NEW.evicting-OLD.size*(1-OLD.protected)*OLD.evicting WHERE id=1; END;
CREATE TRIGGER image_pin_insert AFTER INSERT ON image_pins BEGIN UPDATE image_cache SET pin_count=pin_count+1 WHERE name=NEW.name; END;
CREATE TRIGGER image_pin_delete AFTER DELETE ON image_pins BEGIN UPDATE image_cache SET pin_count=pin_count-1 WHERE name=OLD.name; END;
CREATE TRIGGER image_pin_chapter_delete AFTER DELETE ON chapters BEGIN DELETE FROM image_pins WHERE book_id=OLD.book_id AND idx=OLD.idx; END;
CREATE TRIGGER image_pin_ref_delete AFTER DELETE ON asset_refs WHEN OLD.kind='images' BEGIN DELETE FROM image_pins WHERE book_id=OLD.book_id AND idx=OLD.idx AND name=OLD.name; END;
CREATE TRIGGER image_pin_file_ref AFTER INSERT ON asset_refs WHEN NEW.kind='images' AND EXISTS(SELECT 1 FROM books WHERE id=NEW.book_id AND source IN ('file','local')) BEGIN INSERT OR IGNORE INTO image_pins SELECT NEW.book_id,NEW.idx,NEW.name WHERE EXISTS(SELECT 1 FROM image_cache WHERE name=NEW.name); END;
CREATE TRIGGER cache_evict_insert AFTER INSERT ON asset_gc_candidates WHEN NEW.kind='images' AND NEW.evict>0 BEGIN UPDATE image_cache SET evicting=1 WHERE name=NEW.name; END;
CREATE TRIGGER cache_evict_update AFTER UPDATE OF evict ON asset_gc_candidates WHEN NEW.kind='images' BEGIN UPDATE image_cache SET evicting=(NEW.evict>0) WHERE name=NEW.name; END;
CREATE TRIGGER cache_evict_delete AFTER DELETE ON asset_gc_candidates WHEN OLD.kind='images' BEGIN UPDATE image_cache SET evicting=0 WHERE name=OLD.name; END;
CREATE TABLE chapter_search_docs (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, scope TEXT NOT NULL, literal_check INTEGER NOT NULL, text TEXT NOT NULL, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE);
CREATE VIRTUAL TABLE chapter_search_fts USING fts5(scope,text,content='chapter_search_docs',content_rowid='id',tokenize='trigram case_sensitive 1');
CREATE TABLE chapter_search_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE);
CREATE TABLE chapter_search_state (id INTEGER PRIMARY KEY CHECK(id=1), book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER search_doc_insert AFTER INSERT ON chapter_search_docs BEGIN INSERT INTO chapter_search_fts(rowid,scope,text) VALUES(NEW.id,NEW.scope,NEW.text); END;
CREATE TRIGGER search_doc_delete AFTER DELETE ON chapter_search_docs BEGIN INSERT INTO chapter_search_fts(chapter_search_fts,rowid,scope,text) VALUES('delete',OLD.id,OLD.scope,OLD.text); END;
CREATE TRIGGER search_doc_update AFTER UPDATE OF text ON chapter_search_docs WHEN NEW.text IS NOT OLD.text BEGIN INSERT INTO chapter_search_fts(chapter_search_fts,rowid,scope,text) VALUES('delete',OLD.id,OLD.scope,OLD.text); INSERT INTO chapter_search_fts(rowid,scope,text) VALUES(NEW.id,NEW.scope,NEW.text); END;
CREATE TRIGGER search_chapter_insert AFTER INSERT ON chapters WHEN NEW.downloaded=1 BEGIN INSERT INTO chapter_search_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id; END;
CREATE TRIGGER search_chapter_update AFTER UPDATE OF text,downloaded ON chapters WHEN NEW.text IS NOT OLD.text OR NEW.downloaded IS NOT OLD.downloaded BEGIN INSERT INTO chapter_search_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id; END;
CREATE TABLE chapter_cache (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('cache','protected','legacy')), offline INTEGER NOT NULL DEFAULT 0, bytes INTEGER NOT NULL DEFAULT 0, last_used REAL NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE);
CREATE INDEX chapter_cache_lru ON chapter_cache(kind,last_used,id);
CREATE INDEX chapter_cache_class ON chapter_cache(kind,id);
CREATE TRIGGER chapter_source_protect AFTER UPDATE OF source ON books WHEN NEW.source IS NOT OLD.source AND NEW.source NOT IN ('qq','full','hako','stv') BEGIN UPDATE chapter_cache SET kind='protected',version=version+1 WHERE book_id=NEW.id AND kind!='protected'; END;
CREATE TABLE chapter_readers (id TEXT PRIMARY KEY, book_id TEXT, idx INTEGER, sequence INTEGER NOT NULL, expires REAL NOT NULL, FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE);
CREATE INDEX chapter_readers_expiry ON chapter_readers(expires);
CREATE TABLE chapter_cache_state (id INTEGER PRIMARY KEY CHECK(id=1), max_chapters INTEGER NOT NULL DEFAULT 100, cache_count INTEGER NOT NULL DEFAULT 0, cache_bytes INTEGER NOT NULL DEFAULT 0, protected_count INTEGER NOT NULL DEFAULT 0, protected_bytes INTEGER NOT NULL DEFAULT 0, legacy_count INTEGER NOT NULL DEFAULT 0, legacy_bytes INTEGER NOT NULL DEFAULT 0, book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0, clear_until INTEGER NOT NULL DEFAULT 0, clear_before REAL NOT NULL DEFAULT 0, legacy_until INTEGER NOT NULL DEFAULT 0, legacy_cursor INTEGER NOT NULL DEFAULT 0, fts_merge INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER chapter_cache_count_insert AFTER INSERT ON chapter_cache BEGIN UPDATE chapter_cache_state SET cache_count=cache_count+(NEW.kind='cache')*1,cache_bytes=cache_bytes+(NEW.kind='cache')*NEW.bytes,protected_count=protected_count+(NEW.kind='protected')*1,protected_bytes=protected_bytes+(NEW.kind='protected')*NEW.bytes,legacy_count=legacy_count+(NEW.kind='legacy')*1,legacy_bytes=legacy_bytes+(NEW.kind='legacy')*NEW.bytes WHERE id=1; END;
CREATE TRIGGER chapter_cache_count_update AFTER UPDATE ON chapter_cache WHEN OLD.kind IS NOT NEW.kind OR OLD.bytes IS NOT NEW.bytes BEGIN UPDATE chapter_cache_state SET cache_count=cache_count-(OLD.kind='cache')*1+(NEW.kind='cache')*1,cache_bytes=cache_bytes-(OLD.kind='cache')*OLD.bytes+(NEW.kind='cache')*NEW.bytes,protected_count=protected_count-(OLD.kind='protected')*1+(NEW.kind='protected')*1,protected_bytes=protected_bytes-(OLD.kind='protected')*OLD.bytes+(NEW.kind='protected')*NEW.bytes,legacy_count=legacy_count-(OLD.kind='legacy')*1+(NEW.kind='legacy')*1,legacy_bytes=legacy_bytes-(OLD.kind='legacy')*OLD.bytes+(NEW.kind='legacy')*NEW.bytes WHERE id=1; END;
CREATE TRIGGER chapter_cache_count_delete AFTER DELETE ON chapter_cache BEGIN UPDATE chapter_cache_state SET cache_count=cache_count-(OLD.kind='cache')*1,cache_bytes=cache_bytes-(OLD.kind='cache')*OLD.bytes,protected_count=protected_count-(OLD.kind='protected')*1,protected_bytes=protected_bytes-(OLD.kind='protected')*OLD.bytes,legacy_count=legacy_count-(OLD.kind='legacy')*1,legacy_bytes=legacy_bytes-(OLD.kind='legacy')*OLD.bytes WHERE id=1; END;
CREATE TRIGGER chapter_body_insert AFTER INSERT ON chapters BEGIN INSERT INTO chapter_cache(book_id,idx,kind,bytes) SELECT NEW.book_id,NEW.idx,CASE WHEN NEW.original_html IS NOT NULL OR NEW.original_text IS NOT NULL OR (SELECT source FROM books WHERE id=NEW.book_id) NOT IN ('qq','full','hako','stv') THEN 'protected' ELSE 'legacy' END,length(CAST(NEW.html AS BLOB))+length(CAST(NEW.text AS BLOB))+length(CAST(NEW.images AS BLOB))+coalesce(length(CAST(NEW.original_html AS BLOB)),0)+coalesce(length(CAST(NEW.original_text AS BLOB)),0)+length(CAST(NEW.raw AS BLOB)) WHERE NEW.downloaded=1 ON CONFLICT(book_id,idx) DO UPDATE SET bytes=excluded.bytes,version=chapter_cache.version+1,kind=CASE WHEN NEW.original_html IS NOT NULL OR NEW.original_text IS NOT NULL OR (SELECT source FROM books WHERE id=NEW.book_id) NOT IN ('qq','full','hako','stv') THEN 'protected' ELSE chapter_cache.kind END; END;
CREATE TRIGGER chapter_body_update AFTER UPDATE OF html,text,images,original_html,original_text,downloaded,url ON chapters WHEN NEW.html IS NOT OLD.html OR NEW.text IS NOT OLD.text OR NEW.images IS NOT OLD.images OR NEW.original_html IS NOT OLD.original_html OR NEW.original_text IS NOT OLD.original_text OR NEW.downloaded IS NOT OLD.downloaded OR NEW.url IS NOT OLD.url BEGIN UPDATE chapters SET revision=revision+1 WHERE book_id=NEW.book_id AND idx=NEW.idx; INSERT INTO chapter_cache(book_id,idx,kind,bytes) SELECT NEW.book_id,NEW.idx,CASE WHEN NEW.original_html IS NOT NULL OR NEW.original_text IS NOT NULL OR (SELECT source FROM books WHERE id=NEW.book_id) NOT IN ('qq','full','hako','stv') THEN 'protected' ELSE 'legacy' END,length(CAST(NEW.html AS BLOB))+length(CAST(NEW.text AS BLOB))+length(CAST(NEW.images AS BLOB))+coalesce(length(CAST(NEW.original_html AS BLOB)),0)+coalesce(length(CAST(NEW.original_text AS BLOB)),0)+length(CAST(NEW.raw AS BLOB)) WHERE NEW.downloaded=1 ON CONFLICT(book_id,idx) DO UPDATE SET bytes=excluded.bytes,version=chapter_cache.version+1,kind=CASE WHEN NEW.original_html IS NOT NULL OR NEW.original_text IS NOT NULL OR (SELECT source FROM books WHERE id=NEW.book_id) NOT IN ('qq','full','hako','stv') THEN 'protected' ELSE chapter_cache.kind END; DELETE FROM chapter_cache WHERE book_id=NEW.book_id AND idx=NEW.idx AND NEW.downloaded=0; END;
CREATE INDEX books_shelf_recent ON books((trashed_at>0),archived,read_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_added ON books((trashed_at>0),archived,added_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_updated ON books((trashed_at>0),archived,updated_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_progress ON books((trashed_at>0),archived,progress DESC,name DESC,id DESC);
CREATE INDEX books_shelf_chapters ON books((trashed_at>0),archived,chapter_count DESC,name DESC,id DESC);
CREATE INDEX books_shelf_name ON books((trashed_at>0),archived,shelf_fold(name) ASC,name ASC,id ASC);
CREATE INDEX books_shelf_source ON books((trashed_at>0),archived,source,read_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_kind ON books((trashed_at>0),archived,kind,read_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_search ON books((trashed_at>0),archived,shelf_fold(name || ' ' || author),id);
CREATE TABLE book_groups (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, group_id TEXT NOT NULL, PRIMARY KEY(book_id,group_id));
CREATE INDEX book_group_lookup ON book_groups(group_id,book_id);
CREATE TRIGGER shelf_group_insert AFTER INSERT ON books BEGIN INSERT OR IGNORE INTO book_groups SELECT NEW.id,value FROM json_each(NEW.groups_json) WHERE type='text'; END;
CREATE TRIGGER shelf_group_update AFTER UPDATE OF groups_json ON books WHEN NEW.groups_json IS NOT OLD.groups_json BEGIN DELETE FROM book_groups WHERE book_id=NEW.id; INSERT OR IGNORE INTO book_groups SELECT NEW.id,value FROM json_each(NEW.groups_json) WHERE type='text'; END;
INSERT INTO asset_index_state(id,book_id,idx,complete,inventory_files,inventory_images) VALUES(1,'',-1,0,0,0);
INSERT INTO chapter_cache_state(id,max_chapters,cache_count,cache_bytes,protected_count,protected_bytes,legacy_count,legacy_bytes,book_id,idx,complete,clear_until,clear_before,legacy_until,legacy_cursor,fts_merge) VALUES(1,100,0,0,0,0,0,0,'',-1,1,0,0.0,0,0,0);
INSERT INTO chapter_search_state(id,book_id,idx,complete) VALUES(1,'',-1,0);
INSERT INTO image_cache_state(id,max_bytes,total_bytes,protected_bytes,legacy_bytes,entries,protected_entries,pending_bytes,trim_to,clear_until,clear_cursor) VALUES(1,1073741824,0,0,0,0,0,0,-1,0,0);
"##;

// DDL transcribed from the pinned Python catalog. Never execute SQL supplied by a fixture.
const TRASH: &str = r#"
ALTER TABLE books ADD COLUMN trashed_at REAL NOT NULL DEFAULT 0;
CREATE INDEX books_shelf_added ON books((trashed_at>0),archived,added_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_chapters ON books((trashed_at>0),archived,chapter_count DESC,name DESC,id DESC);
CREATE INDEX books_shelf_kind ON books((trashed_at>0),archived,kind,read_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_name ON books((trashed_at>0),archived,shelf_fold(name) ASC,name ASC,id ASC);
CREATE INDEX books_shelf_progress ON books((trashed_at>0),archived,progress DESC,name DESC,id DESC);
CREATE INDEX books_shelf_recent ON books((trashed_at>0),archived,read_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_search ON books((trashed_at>0),archived,shelf_fold(name || ' ' || author),id);
CREATE INDEX books_shelf_source ON books((trashed_at>0),archived,source,read_at DESC,name DESC,id DESC);
CREATE INDEX books_shelf_updated ON books((trashed_at>0),archived,updated_at DESC,name DESC,id DESC);
"#;

const SEARCH: &str = r#"
CREATE TABLE chapter_search_docs (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, scope TEXT NOT NULL, literal_check INTEGER NOT NULL, text TEXT NOT NULL, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE);
CREATE VIRTUAL TABLE chapter_search_fts USING fts5(scope,text,content='chapter_search_docs',content_rowid='id',tokenize='trigram case_sensitive 1');
CREATE TABLE chapter_search_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL, idx INTEGER NOT NULL, UNIQUE(book_id,idx), FOREIGN KEY(book_id,idx) REFERENCES chapters(book_id,idx) ON DELETE CASCADE);
CREATE TABLE chapter_search_state (id INTEGER PRIMARY KEY CHECK(id=1), book_id TEXT NOT NULL DEFAULT '', idx INTEGER NOT NULL DEFAULT -1, complete INTEGER NOT NULL DEFAULT 0);
CREATE TRIGGER search_chapter_insert AFTER INSERT ON chapters WHEN NEW.downloaded=1 BEGIN INSERT INTO chapter_search_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id; END;
CREATE TRIGGER search_chapter_update AFTER UPDATE OF text,downloaded ON chapters WHEN NEW.text IS NOT OLD.text OR NEW.downloaded IS NOT OLD.downloaded BEGIN INSERT INTO chapter_search_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id; END;
CREATE TRIGGER search_doc_delete AFTER DELETE ON chapter_search_docs BEGIN INSERT INTO chapter_search_fts(chapter_search_fts,rowid,scope,text) VALUES('delete',OLD.id,OLD.scope,OLD.text); END;
CREATE TRIGGER search_doc_insert AFTER INSERT ON chapter_search_docs BEGIN INSERT INTO chapter_search_fts(rowid,scope,text) VALUES(NEW.id,NEW.scope,NEW.text); END;
CREATE TRIGGER search_doc_update AFTER UPDATE OF text ON chapter_search_docs WHEN NEW.text IS NOT OLD.text BEGIN INSERT INTO chapter_search_fts(chapter_search_fts,rowid,scope,text) VALUES('delete',OLD.id,OLD.scope,OLD.text); INSERT INTO chapter_search_fts(rowid,scope,text) VALUES(NEW.id,NEW.scope,NEW.text); END;
INSERT INTO chapter_search_state(id) VALUES(1);
"#;

const ASSETS: &str = r#"
CREATE TABLE asset_pending (id INTEGER PRIMARY KEY AUTOINCREMENT, book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, blocked INTEGER NOT NULL DEFAULT 0, UNIQUE(book_id,idx));
CREATE TABLE asset_refs (book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE, idx INTEGER NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL, PRIMARY KEY(book_id,idx,kind,name));
CREATE INDEX asset_lookup ON asset_refs(kind,name);
CREATE INDEX asset_pending_work ON asset_pending(blocked,id);
CREATE TRIGGER asset_book_insert AFTER INSERT ON books BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.id,-1) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_book_path AFTER UPDATE OF path ON books WHEN NEW.path IS NOT OLD.path BEGIN INSERT INTO asset_pending(book_id,idx) SELECT book_id,idx FROM chapters WHERE book_id=NEW.id ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_book_update AFTER UPDATE OF path,cover ON books WHEN NEW.path IS NOT OLD.path OR NEW.cover IS NOT OLD.cover BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.id,-1) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_chapter_delete AFTER DELETE ON chapters BEGIN DELETE FROM asset_refs WHERE book_id=OLD.book_id AND idx=OLD.idx; DELETE FROM asset_pending WHERE book_id=OLD.book_id AND idx=OLD.idx; END;
CREATE TRIGGER asset_chapter_insert AFTER INSERT ON chapters WHEN NEW.html!='' OR NEW.original_html IS NOT NULL OR NEW.images!='[]' BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_chapter_update AFTER UPDATE OF html,original_html,images ON chapters WHEN NEW.html IS NOT OLD.html OR NEW.original_html IS NOT OLD.original_html OR NEW.images IS NOT OLD.images BEGIN INSERT INTO asset_pending(book_id,idx) VALUES(NEW.book_id,NEW.idx) ON CONFLICT(book_id,idx) DO UPDATE SET id=excluded.id,blocked=0; END;
CREATE TRIGGER asset_ref_gc BEFORE DELETE ON asset_refs BEGIN INSERT INTO asset_gc_candidates(kind,name) VALUES(OLD.kind,OLD.name) ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0; END;
CREATE TRIGGER image_pin_file_ref AFTER INSERT ON asset_refs WHEN NEW.kind='images' AND EXISTS(SELECT 1 FROM books WHERE id=NEW.book_id AND source IN ('file','local')) BEGIN INSERT OR IGNORE INTO image_pins SELECT NEW.book_id,NEW.idx,NEW.name WHERE EXISTS(SELECT 1 FROM image_cache WHERE name=NEW.name); END;
CREATE TRIGGER image_pin_ref_delete AFTER DELETE ON asset_refs WHEN OLD.kind='images' BEGIN DELETE FROM image_pins WHERE book_id=OLD.book_id AND idx=OLD.idx AND name=OLD.name; END;
INSERT OR IGNORE INTO asset_pending(book_id,idx) SELECT id,-1 FROM books;
INSERT OR REPLACE INTO asset_index_state(id) VALUES(1);
"#;

const INVENTORY: &str = r#"
CREATE TABLE asset_gc_candidates (id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, name TEXT NOT NULL, generation INTEGER NOT NULL DEFAULT 1, retry_at REAL NOT NULL DEFAULT 0, evict INTEGER NOT NULL DEFAULT 0, UNIQUE(kind,name));
CREATE INDEX asset_gc_cache ON asset_gc_candidates(evict,retry_at,id);
CREATE INDEX asset_gc_due ON asset_gc_candidates(retry_at,id);
CREATE TRIGGER asset_ref_gc BEFORE DELETE ON asset_refs BEGIN INSERT INTO asset_gc_candidates(kind,name) VALUES(OLD.kind,OLD.name) ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0; END;
CREATE TRIGGER cache_evict_delete AFTER DELETE ON asset_gc_candidates WHEN OLD.kind='images' BEGIN UPDATE image_cache SET evicting=0 WHERE name=OLD.name; END;
CREATE TRIGGER cache_evict_insert AFTER INSERT ON asset_gc_candidates WHEN NEW.kind='images' AND NEW.evict>0 BEGIN UPDATE image_cache SET evicting=1 WHERE name=NEW.name; END;
CREATE TRIGGER cache_evict_update AFTER UPDATE OF evict ON asset_gc_candidates WHEN NEW.kind='images' BEGIN UPDATE image_cache SET evicting=(NEW.evict>0) WHERE name=NEW.name; END;
DROP TRIGGER asset_book_delete;
ALTER TABLE asset_index_state DROP COLUMN cleanup_requested;
ALTER TABLE asset_index_state DROP COLUMN cleanup_completed;
ALTER TABLE asset_index_state ADD COLUMN inventory_files INTEGER NOT NULL DEFAULT 0;
ALTER TABLE asset_index_state ADD COLUMN inventory_images INTEGER NOT NULL DEFAULT 0;
"#;

#[cfg(test)]
mod tests {
    use super::*;
    use crate::library::test_common::Fixture;
    use rusqlite::functions::FunctionFlags;

    const LEGACY: &[(&str, &[&str])] = &[
        (
            "trash",
            &[
                r#"DROP INDEX "books_shelf_recent""#,
                r#"DROP INDEX "books_shelf_added""#,
                r#"DROP INDEX "books_shelf_updated""#,
                r#"DROP INDEX "books_shelf_progress""#,
                r#"DROP INDEX "books_shelf_chapters""#,
                r#"DROP INDEX "books_shelf_name""#,
                r#"DROP INDEX "books_shelf_source""#,
                r#"DROP INDEX "books_shelf_kind""#,
                r#"DROP INDEX "books_shelf_search""#,
                r#"ALTER TABLE books DROP COLUMN trashed_at"#,
            ],
        ),
        (
            "search",
            &[
                r#"DROP TRIGGER "search_doc_insert""#,
                r#"DROP TRIGGER "search_doc_delete""#,
                r#"DROP TRIGGER "search_doc_update""#,
                r#"DROP TRIGGER "search_chapter_insert""#,
                r#"DROP TRIGGER "search_chapter_update""#,
                r#"DROP TABLE chapter_search_fts"#,
                r#"DROP TABLE chapter_search_docs"#,
                r#"DROP TABLE chapter_search_pending"#,
                r#"DROP TABLE chapter_search_state"#,
            ],
        ),
        (
            "assets",
            &[
                r#"DROP TRIGGER "asset_book_insert""#,
                r#"DROP TRIGGER "asset_book_update""#,
                r#"DROP TRIGGER "asset_ref_gc""#,
                r#"DROP TRIGGER "asset_book_path""#,
                r#"DROP TRIGGER "asset_chapter_insert""#,
                r#"DROP TRIGGER "asset_chapter_update""#,
                r#"DROP TRIGGER "asset_chapter_delete""#,
                r#"DROP TABLE asset_pending"#,
                r#"DROP TABLE asset_refs"#,
            ],
        ),
        (
            "inventory",
            &[
                r#"DROP TRIGGER asset_ref_gc"#,
                r#"DROP TABLE asset_gc_candidates"#,
                r#"ALTER TABLE asset_index_state DROP COLUMN inventory_files"#,
                r#"ALTER TABLE asset_index_state DROP COLUMN inventory_images"#,
                r#"ALTER TABLE asset_index_state ADD COLUMN cleanup_requested INTEGER NOT NULL DEFAULT 4"#,
                r#"ALTER TABLE asset_index_state ADD COLUMN cleanup_completed INTEGER NOT NULL DEFAULT 1"#,
                r#"CREATE TRIGGER asset_book_delete AFTER DELETE ON books BEGIN UPDATE asset_index_state SET cleanup_requested=cleanup_requested+1 WHERE id=1; END"#,
            ],
        ),
    ];

    fn fixture() -> (Fixture, Connection) {
        let fixture = Fixture::new("empty");
        let db = Connection::open(&fixture.db).unwrap();
        db.create_scalar_function(
            "shelf_fold",
            1,
            FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| Ok(crate::casefold::casefold(&ctx.get::<String>(0)?)),
        )
        .unwrap();
        db.execute("INSERT INTO books(id,name,added_at,updated_at,position) VALUES('keep','Straße',1,2,0.65)", []).unwrap();
        (fixture, db)
    }

    #[test]
    fn upgrades_reference_legacy_profiles_without_losing_progress() {
        for (profile, statements) in LEGACY {
            let (_fixture, mut db) = fixture();
            for stmt in *statements {
                db.execute_batch(stmt).unwrap();
            }
            upgrade(&mut db).unwrap_or_else(|e| panic!("{profile}: {e}"));
            upgrade(&mut db).unwrap_or_else(|e| panic!("{profile} reopen: {e}"));
            let row = db
                .query_row(
                    "SELECT position,trashed_at FROM books WHERE id='keep'",
                    [],
                    |r| Ok((r.get::<_, f64>(0)?, r.get::<_, f64>(1)?)),
                )
                .unwrap();
            assert_eq!(row, (0.65, 0.0), "{profile}");
            let integrity: String = db
                .query_row("PRAGMA integrity_check", [], |r| r.get(0))
                .unwrap();
            assert_eq!(integrity, "ok", "{profile}");
            match *profile {
                "search" => {
                    let state = db
                        .query_row(
                            "SELECT book_id,idx,complete FROM chapter_search_state WHERE id=1",
                            [],
                            |r| {
                                Ok((
                                    r.get::<_, String>(0)?,
                                    r.get::<_, i64>(1)?,
                                    r.get::<_, i64>(2)?,
                                ))
                            },
                        )
                        .unwrap();
                    assert_eq!(state, (String::new(), -1, 0));
                }
                "assets" => {
                    let pending: i64 = db
                        .query_row(
                            "SELECT count(*) FROM asset_pending WHERE book_id='keep' AND idx=-1",
                            [],
                            |r| r.get(0),
                        )
                        .unwrap();
                    assert_eq!(pending, 1);
                }
                _ => {}
            }
        }
    }

    #[test]
    fn unknown_schema_untouched() {
        let (_fixture, mut db) = fixture();
        db.execute_batch("CREATE TABLE unknown_shape(v TEXT)")
            .unwrap();
        let before: i64 = db
            .query_row("PRAGMA schema_version", [], |r| r.get(0))
            .unwrap();
        assert!(upgrade(&mut db).is_err());
        let after: i64 = db
            .query_row("PRAGMA schema_version", [], |r| r.get(0))
            .unwrap();
        assert_eq!(before, after);
    }

    #[test]
    fn failed_upgrade_rolls_back_schema_and_can_retry() {
        use rusqlite::hooks::{AuthAction, AuthContext, Authorization};

        let (_fixture, mut db) = fixture();
        for stmt in LEGACY.iter().find(|(name, _)| *name == "search").unwrap().1 {
            db.execute_batch(stmt).unwrap();
        }
        let before = signature(&db).unwrap();
        db.authorizer(Some(|ctx: AuthContext<'_>| match ctx.action {
            AuthAction::Insert {
                table_name: "chapter_search_state",
            } => Authorization::Deny,
            _ => Authorization::Allow,
        }))
        .unwrap();
        assert!(upgrade(&mut db).is_err());
        db.authorizer(None::<fn(AuthContext<'_>) -> Authorization>)
            .unwrap();
        assert_eq!(signature(&db).unwrap(), before);
        upgrade(&mut db).unwrap();
    }
}
