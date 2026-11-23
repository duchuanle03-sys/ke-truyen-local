use crate::{
    ApiError, Library,
    reader::{ensure_chapter_cache, now, rows},
};
use rusqlite::{Connection, TransactionBehavior, params};
use serde_json::Value;

const ONLINE: &str = "('qq','full','hako','stv')";
const CHAPTER_BATCH: usize = 8;
const MIGRATION_BATCH: usize = 64;
const CHAPTER_BYTES: i64 = 256 * 1024;
const EVICTION_CANDIDATES: &str = "SELECT a.id,a.book_id,a.idx,a.bytes
    FROM chapter_cache a INDEXED BY chapter_cache_lru
    WHERE a.kind='cache' AND a.offline=0 AND a.id<=?1 AND a.last_used<=?2
    AND NOT EXISTS(SELECT 1 FROM chapter_readers r WHERE r.expires>?3 AND r.book_id=a.book_id AND r.idx=a.idx)
    ORDER BY a.last_used,a.id LIMIT ?4";
const FINISH_CLEAR: &str = "UPDATE chapter_cache_state SET clear_until=0 WHERE id=1 AND NOT EXISTS(
    SELECT 1 FROM chapter_cache INDEXED BY chapter_cache_lru
    WHERE kind='cache' AND offline=0 AND id<=? AND last_used<=?)";

impl Library {
    pub(crate) fn compact_database(&self, confirmed: bool) -> Result<Value, ApiError> {
        if !confirmed {
            return Err(ApiError::new(
                400,
                "Cần xác nhận trước khi thu gọn cơ sở dữ liệu.",
            ));
        }
        let busy = || {
            ApiError::new(
                400,
                "Hãy đóng trình đọc và chờ thao tác thư viện kết thúc rồi thử lại.",
            )
        };
        let _exclusive = self.database_gate.try_write().map_err(|_| busy())?;
        let db = self.write_connection()?;
        db.busy_timeout(std::time::Duration::from_secs(1))?;
        if !self.asset_leases.lock().is_empty() || !self.chapter_leases.lock().is_empty() || db.query_row(
            "SELECT EXISTS(SELECT 1 FROM chapter_readers WHERE expires>? AND book_id IS NOT NULL)",
            [now()], |r| r.get::<_,bool>(0),
        )? {
            return Err(busy());
        }
        if db.query_row(
            "SELECT EXISTS(SELECT 1 FROM download_jobs WHERE status IN ('queued','running','verification_required'))",
            [], |r| r.get::<_,bool>(0),
        )? {
            return Err(ApiError::new(400, "Hãy tạm dừng các tác vụ tải trước khi thu gọn."));
        }
        let pages = db.pragma_query_value(None, "page_count", |r| r.get::<_, i64>(0))? as u64;
        let page_size = db.pragma_query_value(None, "page_size", |r| r.get::<_, i64>(0))? as u64;
        let required = pages
            .saturating_mul(page_size)
            .saturating_mul(2)
            .saturating_add(128 * 1024 * 1024);
        if fs4::available_space(self.root())? < required {
            return Err(ApiError::new(
                400,
                "Không đủ dung lượng trống: cần khoảng hai lần dung lượng SQLite và thêm 128 MiB.",
            ));
        }
        let checkpoint_busy: bool =
            db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| r.get(0))?;
        if checkpoint_busy {
            return Err(ApiError::new(
                400,
                "SQLite đang được sử dụng; chưa thể thu gọn an toàn.",
            ));
        }
        db.execute(
            "INSERT INTO chapter_search_fts(chapter_search_fts) VALUES('optimize')",
            [],
        )?;
        db.execute_batch("VACUUM")?;
        db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()))?;
        self.chapter_cache_stats()
    }

    pub(crate) fn chapter_cache_stats(&self) -> Result<Value, ApiError> {
        let mut db = self.reader_connection()?;
        let tx = db.transaction()?;
        let mut state = rows(&tx, "SELECT * FROM chapter_cache_state WHERE id=1", [])?
            .pop()
            .ok_or(rusqlite::Error::QueryReturnedNoRows)?;
        for key in ["auto_vacuum", "page_count", "freelist_count", "page_size"] {
            let value: i64 = tx.pragma_query_value(None, key, |r| r.get(0))?;
            state[key] = value.into();
        }
        state["database_bytes"] = self.root().join("library.sqlite3").metadata()?.len().into();
        state["wal_bytes"] = match self.root().join("library.sqlite3-wal").metadata() {
            Ok(file) => file.len(),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => 0,
            Err(e) => return Err(e.into()),
        }
        .into();
        state["reusable_bytes"] = (state["freelist_count"].as_i64().unwrap_or(0)
            * state["page_size"].as_i64().unwrap_or(0))
        .into();
        Ok(state)
    }

    pub(crate) fn set_chapter_cache_limit(&self, value: &Value) -> Result<Value, ApiError> {
        let limit = value
            .as_i64()
            .filter(|value| (0..=100_000).contains(value))
            .ok_or_else(|| {
                ApiError::new(400, "Giới hạn phải là số nguyên từ 0 đến 100.000 chương.")
            })?;
        self.write_connection()?.execute(
            "UPDATE chapter_cache_state SET max_chapters=? WHERE id=1",
            [limit],
        )?;
        self.chapter_cache_stats()
    }

    pub(crate) fn clear_chapter_cache(&self) -> Result<Value, ApiError> {
        self.write_connection()?.execute(
            "UPDATE chapter_cache_state SET clear_until=coalesce((SELECT max(id) FROM chapter_cache),0),
             clear_before=? WHERE id=1", [now()],
        )?;
        self.chapter_cache_stats()
    }

    pub(crate) fn reclassify_legacy_chapters(&self, confirmed: bool) -> Result<Value, ApiError> {
        if !confirmed {
            return Err(ApiError::new(
                400,
                "Cần xác nhận: chương cũ có thể là bản ngoại tuyến duy nhất; sau phân loại lại có thể bị dọn và cần mạng/CAPTCHA/dịch để đọc lại.",
            ));
        }
        if self.write_connection()?.execute(
            "UPDATE chapter_cache_state SET legacy_until=coalesce((SELECT max(id) FROM chapter_cache WHERE kind='legacy'),0),
             legacy_cursor=0 WHERE id=1 AND complete=1", [],
        )? == 0 {
            return Err(ApiError::new(400, "Hãy chờ kiểm kê chương cũ hoàn tất trước khi phân loại lại."));
        }
        self.chapter_cache_stats()
    }

    pub(crate) fn maintain_chapter_cache(&self) -> Result<usize, ApiError> {
        let mut db = self.write_connection()?;
        let migrated = migrate_chapter_cache(&mut db)?;
        if !db.query_row(
            "SELECT legacy_until!=0 OR clear_until!=0 OR fts_merge!=0 OR cache_count>max_chapters
             FROM chapter_cache_state WHERE id=1",
            [],
            |r| r.get::<_, bool>(0),
        )? {
            return Ok(migrated);
        }
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let reclassified = reclassify_chapters(&tx)?;
        let evicted = self.evict_chapters(&tx)?;
        let merged = crate::search::reclaim(&tx)?;
        tx.commit()?;
        if evicted > 0 && db.pragma_query_value(None, "auto_vacuum", |r| r.get::<_, i64>(0))? == 2 {
            db.pragma_update(None, "incremental_vacuum", 16)?;
        }
        Ok(migrated + reclassified + evicted + usize::from(merged))
    }

    fn evict_chapters(&self, db: &Connection) -> Result<usize, ApiError> {
        let (mut excess, clear_until, clear_before) = db.query_row(
            "SELECT cache_count-max_chapters,clear_until,clear_before FROM chapter_cache_state WHERE id=1",
            [], |r| Ok((r.get::<_, i64>(0)?,r.get::<_,i64>(1)?,r.get::<_,f64>(2)?)),
        )?;
        if excess <= 0 && clear_until == 0 {
            return Ok(0);
        }
        let active = self.chapter_leases.lock();
        let clearing = clear_until > 0;
        let limit = if clearing {
            CHAPTER_BATCH
        } else {
            CHAPTER_BATCH.min(excess as usize)
        };
        let (last_id, last_used) = if clearing {
            (clear_until, clear_before)
        } else {
            (i64::MAX, f64::INFINITY)
        };
        let mut select = db.prepare(EVICTION_CANDIDATES)?;
        let candidates = select
            .query_map(
                params![last_id, last_used, now(), (limit + active.len()) as i64],
                |r| {
                    Ok((
                        r.get::<_, i64>(0)?,
                        (r.get::<_, String>(1)?, r.get::<_, i64>(2)?),
                        r.get::<_, i64>(3)?,
                    ))
                },
            )?
            .collect::<Result<Vec<_>, _>>()?;
        let mut removed = 0;
        let mut bytes = 0;
        for (id, (book, idx), size) in candidates
            .into_iter()
            .filter(|(_, key, _)| !active.contains_key(key))
            .take(limit)
        {
            // shortcut: one oversized chapter remains atomic, split body storage if measured latency warrants it.
            if removed > 0 && bytes + size > CHAPTER_BYTES {
                break;
            }
            let eligible: bool = db.query_row(
                &format!("SELECT EXISTS(SELECT 1 FROM chapter_cache a JOIN chapters c ON c.book_id=a.book_id AND c.idx=a.idx
                 JOIN books b ON b.id=c.book_id WHERE a.id=? AND a.kind='cache' AND a.offline=0
                 AND b.source IN {ONLINE} AND c.original_html IS NULL AND c.original_text IS NULL)"),
                [id], |r| r.get(0),
            )?;
            if eligible && (clearing || excess > 0) {
                db.execute(
                    "UPDATE chapters SET html='',text='',images='[]',original_html=NULL,original_text=NULL,downloaded=0,
                     raw=json_remove(raw,'$.html','$.text','$.images','$.original_html','$.original_text') WHERE book_id=? AND idx=?",
                    params![book,idx],
                )?;
                db.execute(
                    "DELETE FROM chapter_search_docs WHERE book_id=? AND idx=?",
                    params![book, idx],
                )?;
                db.execute(
                    "DELETE FROM chapter_search_pending WHERE book_id=? AND idx=?",
                    params![book, idx],
                )?;
                removed += 1;
                excess -= 1;
                bytes += size;
            }
        }
        if clearing {
            db.execute(FINISH_CLEAR, params![clear_until, clear_before])?;
        }
        if removed > 0 {
            db.execute(
                "UPDATE chapter_cache_state SET fts_merge=1 WHERE id=1 AND fts_merge=0",
                [],
            )?;
        }
        Ok(removed)
    }
}

fn reclassify_chapters(db: &Connection) -> Result<usize, ApiError> {
    let (until, cursor): (i64, i64) = db.query_row(
        "SELECT legacy_until,legacy_cursor FROM chapter_cache_state WHERE id=1",
        [],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )?;
    if until == 0 {
        return Ok(0);
    }
    let ids = db.prepare("SELECT id FROM chapter_cache WHERE kind='legacy' AND id>? AND id<=? ORDER BY id LIMIT ?")?
        .query_map(params![cursor,until,CHAPTER_BATCH as i64], |r| r.get::<_, i64>(0))?
        .collect::<Result<Vec<_>,_>>()?;
    let sql = format!(
        "UPDATE chapter_cache SET kind='cache',version=version+1 WHERE id=? AND offline=0 AND EXISTS(
        SELECT 1 FROM chapters c JOIN books b ON b.id=c.book_id WHERE c.book_id=chapter_cache.book_id
        AND c.idx=chapter_cache.idx AND b.source IN {ONLINE} AND c.original_html IS NULL AND c.original_text IS NULL)",
    );
    for id in &ids {
        db.execute(&sql, [id])?;
    }
    db.execute(
        "UPDATE chapter_cache_state SET legacy_cursor=?,legacy_until=? WHERE id=1",
        params![
            ids.last().copied().unwrap_or(cursor),
            if ids.len() == CHAPTER_BATCH { until } else { 0 }
        ],
    )?;
    Ok(ids.len())
}

fn migrate_chapter_cache(db: &mut Connection) -> Result<usize, ApiError> {
    if db.query_row(
        "SELECT complete FROM chapter_cache_state WHERE id=1",
        [],
        |r| r.get::<_, bool>(0),
    )? {
        return Ok(0);
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (book, idx, complete) = tx.query_row(
        "SELECT book_id,idx,complete FROM chapter_cache_state WHERE id=1",
        [],
        |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, bool>(2)?,
            ))
        },
    )?;
    if complete {
        return Ok(0);
    }
    let owners = tx.prepare("SELECT book_id,idx,downloaded FROM chapters WHERE (book_id,idx)>(?,?) ORDER BY book_id,idx LIMIT ?")?
        .query_map(params![book,idx,MIGRATION_BATCH as i64], |r|
            Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?,r.get::<_,bool>(2)?)))?
        .collect::<Result<Vec<_>,_>>()?;
    let mut processed = 0;
    let mut bytes = 0;
    for (book, idx, downloaded) in &owners {
        if *downloaded {
            ensure_chapter_cache(&tx, book, *idx)?;
            bytes += tx.query_row(
                "SELECT bytes FROM chapter_cache WHERE book_id=? AND idx=?",
                params![book, idx],
                |r| r.get::<_, i64>(0),
            )?;
        }
        processed += 1;
        if bytes >= CHAPTER_BYTES {
            break;
        }
    }
    if let Some((book, idx, _)) = owners[..processed].last() {
        tx.execute(
            "UPDATE chapter_cache_state SET book_id=?,idx=? WHERE id=1",
            params![book, idx],
        )?;
    }
    if owners.len() < MIGRATION_BATCH && owners.len() == processed {
        tx.execute("UPDATE chapter_cache_state SET complete=1 WHERE id=1", [])?;
    }
    tx.commit()?;
    Ok(processed)
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::tests::writable;
    use serde_json::json;
    use std::sync::{
        Arc,
        atomic::{AtomicUsize, Ordering},
    };

    #[test]
    fn m3_clear_does_not_scan_fresh_cache_behind_a_leased_body() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        db.execute_batch(
            "UPDATE books SET source='qq' WHERE id='book';
             UPDATE chapter_cache SET kind='cache' WHERE book_id='book' AND idx=0;
             UPDATE chapter_cache_state SET max_chapters=100000;",
        )
        .unwrap();
        library
            .reader(&json!({"id":"reader","book_id":"book","index":0}))
            .unwrap();
        library.clear_chapter_cache().unwrap();
        for (sql, parameters) in [
            (EVICTION_CANDIDATES, params![1, 0.0, 0.0, 8]),
            (FINISH_CLEAR, params![1, 0.0]),
        ] {
            let plan = db
                .prepare(&format!("EXPLAIN QUERY PLAN {sql}"))
                .unwrap()
                .query_map(parameters, |r| r.get::<_, String>(3))
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap();
            assert!(
                plan.iter()
                    .any(|line| line.contains("chapter_cache_lru") && line.contains("last_used<?")),
                "{plan:?}",
            );
        }

        let pass = || {
            let steps = Arc::new(AtomicUsize::new(0));
            let counter = steps.clone();
            db.progress_handler(
                1,
                Some(move || {
                    counter.fetch_add(1, Ordering::Relaxed);
                    false
                }),
            )
            .unwrap();
            let tx = db.unchecked_transaction().unwrap();
            assert_eq!(library.evict_chapters(&tx).unwrap(), 0);
            tx.commit().unwrap();
            db.progress_handler(0, None::<fn() -> bool>).unwrap();
            steps.load(Ordering::Relaxed)
        };
        let before = pass();
        db.execute_batch(
            "WITH RECURSIVE fresh(idx) AS (SELECT 2 UNION ALL SELECT idx+1 FROM fresh WHERE idx<10001)
             INSERT INTO chapters(book_id,idx,text,downloaded) SELECT 'book',idx,'fresh',1 FROM fresh;
             UPDATE chapter_cache SET kind='cache',
                 last_used=(SELECT clear_before+3600 FROM chapter_cache_state) WHERE idx>=2;",
        )
        .unwrap();
        let after = pass();
        assert!(
            after <= before + 100,
            "{before} VM steps before fresh rows, {after} after"
        );

        db.execute_batch(
            "UPDATE chapter_cache_state SET clear_until=(SELECT max(id) FROM chapter_cache);
             UPDATE chapter_cache SET last_used=(SELECT clear_before+3600 FROM chapter_cache_state)
                 WHERE book_id='book' AND idx=0;",
        )
        .unwrap();
        let completion = pass();
        assert!(
            completion <= before + 100,
            "{completion} VM steps for clear completion"
        );
        assert_eq!(library.chapter_cache_stats().unwrap()["clear_until"], 0);
        eprintln!(
            "Clear VM steps with 1/10,001 cached bodies: {before}/{after}, completion: {completion}"
        );
    }

    #[test]
    fn m3_compaction_requires_idle_and_preserves_search() {
        let (_fixture, library) = writable();
        assert_eq!(library.compact_database(false).unwrap_err().status, 400);
        library
            .reader(&json!({"id":"reader","book_id":"book","index":0}))
            .unwrap();
        assert_eq!(library.compact_database(true).unwrap_err().status, 400);
        library
            .reader(&json!({"id":"reader","sequence":1}))
            .unwrap();
        let db = library.write_connection().unwrap();
        db.execute(
            "INSERT INTO download_jobs VALUES('job','book','queued',0,'{}',1,1)",
            [],
        )
        .unwrap();
        assert_eq!(library.compact_database(true).unwrap_err().status, 400);
        db.execute("UPDATE download_jobs SET status='paused'", [])
            .unwrap();
        drop(db);
        let lease = library
            .asset_leases
            .acquire(library.root().join("images").join("test"));
        assert_eq!(library.compact_database(true).unwrap_err().status, 400);
        drop(lease);
        library
            .index_search(&std::sync::atomic::AtomicBool::new(false))
            .unwrap();
        assert_eq!(library.compact_database(true).unwrap()["auto_vacuum"], 0);
        assert_eq!(
            library.search_content("book", "Việt", "-1").unwrap()["results"][0]["index"],
            0
        );
    }
}
