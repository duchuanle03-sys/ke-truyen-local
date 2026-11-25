#[cfg(test)]
mod tests {
    use crate::reader::tests::writable;
    use std::{fs, sync::atomic::AtomicBool};

    #[test]
    fn m3_gc_checks_shared_ownership_leases_and_recovers_a_stage() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        let name = format!("{}.epub", "a".repeat(64));
        let directory = library.root().join("files");
        fs::create_dir_all(&directory).unwrap();
        let path = directory.join(&name);
        fs::write(&path, "managed").unwrap();
        db.execute(
            "INSERT INTO asset_refs VALUES('book',-1,'files',?)",
            [&name],
        )
        .unwrap();
        db.execute("UPDATE asset_index_state SET complete=1", [])
            .unwrap();
        db.execute("DELETE FROM asset_pending", []).unwrap();
        db.execute(
            "INSERT INTO asset_gc_candidates(kind,name) VALUES('files',?)",
            [&name],
        )
        .unwrap();
        let stop = AtomicBool::new(false);
        assert_eq!(library.cleanup_assets(&stop).unwrap(), 1);
        assert_eq!(fs::read_to_string(&path).unwrap(), "managed");
        db.execute("DELETE FROM asset_refs WHERE name=?", [&name])
            .unwrap();
        let id: i64 = db
            .query_row(
                "SELECT id FROM asset_gc_candidates WHERE kind='files' AND name=?",
                [&name],
                |r| r.get(0),
            )
            .unwrap();
        let lease = library.asset_leases.acquire(path.clone());
        assert_eq!(library.cleanup_assets(&stop).unwrap(), 1);
        assert!(path.exists());
        drop(lease);
        let staged = directory.join(format!("{name}.gc-{id}"));
        fs::rename(&path, &staged).unwrap();
        db.execute("UPDATE asset_gc_candidates SET retry_at=0 WHERE id=?", [id])
            .unwrap();
        assert_eq!(library.cleanup_assets(&stop).unwrap(), 1);
        assert!(!staged.exists());
        assert!(!path.exists());
    }
}
use crate::{
    ApiError, Library, http,
    library::parse_json,
    reader::{now, rows},
};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::{
    collections::BTreeSet,
    fs::{self, Metadata},
    io,
    path::Path,
    sync::atomic::{AtomicBool, Ordering},
};

const ASSET_BATCH: i64 = 32;
const INDEX_BYTES: usize = 256 * 1024;
const INVENTORY_BATCH: usize = 256;
type References = BTreeSet<(&'static str, String)>;

struct Owner {
    token: i64,
    book: String,
    idx: i64,
}

struct Candidate {
    id: i64,
    kind: String,
    name: String,
    generation: i64,
    evict: i64,
}

#[derive(Default)]
pub(crate) struct Inventory(Option<(&'static str, fs::ReadDir)>);

impl Library {
    pub(crate) fn inventory_assets(
        &self,
        inventory: &mut Inventory,
        stop: &AtomicBool,
    ) -> Result<usize, ApiError> {
        if stop.load(Ordering::Acquire) {
            inventory.0 = None;
            return Ok(0);
        }
        let mut db = self.write_connection()?;
        let (files, images): (bool, bool) = db.query_row(
            "SELECT inventory_files,inventory_images FROM asset_index_state WHERE id=1",
            [],
            |r| Ok((r.get(0)?, r.get(1)?)),
        )?;
        let kind = if !files {
            "files"
        } else if !images {
            "images"
        } else {
            inventory.0 = None;
            return Ok(0);
        };
        let folder = self.root().join(kind);
        let mut scan = match inventory.0.take() {
            Some((active, scan)) if active == kind => Some(scan),
            _ => match safe_asset_directory(&folder)? {
                true => Some(fs::read_dir(&folder)?),
                false => None,
            },
        };
        let mut paths = Vec::new();
        let mut cached = Vec::new();
        for _ in 0..INVENTORY_BATCH {
            if stop.load(Ordering::Acquire) {
                return Ok(0);
            }
            let Some(entry) = scan.as_mut().and_then(Iterator::next).transpose()? else {
                scan = None;
                break;
            };
            let name = entry.file_name();
            let Some(name) = name
                .to_str()
                .filter(|name| managed_name(kind, name).is_some())
            else {
                continue;
            };
            paths.push(name.to_owned());
            if kind == "images"
                && hex(name, 64)
                && let Ok(Some(meta)) = regular_asset(&folder.join(name))
            {
                let modified = meta.modified()?;
                let mtime = match modified.duration_since(std::time::UNIX_EPOCH) {
                    Ok(value) => value.as_secs_f64(),
                    Err(error) => -error.duration().as_secs_f64(),
                };
                let size = i64::try_from(meta.len()).map_err(|_| rusqlite::Error::InvalidQuery)?;
                cached.push((name.to_owned(), size, mtime));
            }
        }
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        for name in &paths {
            tx.execute(
                "INSERT OR IGNORE INTO asset_gc_candidates(kind,name) SELECT ?1,?2
                WHERE NOT EXISTS(SELECT 1 FROM asset_refs WHERE kind=?1 AND name=?2)",
                params![kind, name],
            )?;
        }
        for (name, size, mtime) in cached {
            tx.execute(
                "INSERT OR IGNORE INTO image_cache(name,size,last_used,legacy) VALUES(?,?,?,1)",
                params![name, size, mtime],
            )?;
        }
        if scan.is_none() {
            let sql = if kind == "files" {
                "UPDATE asset_index_state SET inventory_files=1 WHERE id=1"
            } else {
                "UPDATE asset_index_state SET inventory_images=1 WHERE id=1"
            };
            tx.execute(sql, [])?;
        }
        tx.commit()?;
        // A restart or failed batch safely replays an unfinished folder.
        inventory.0 = scan.map(|scan| (kind, scan));
        Ok(paths.len().max(1))
    }

    pub(crate) fn index_book_assets(&self, book: &str) -> Result<(), ApiError> {
        let mut db = self.write_connection()?;
        let token = db
            .query_row(
                "SELECT id FROM asset_pending WHERE book_id=? AND idx=-1",
                [book],
                |r| r.get::<_, i64>(0),
            )
            .optional()?;
        if let Some(token) = token
            && let Some((_, refs)) = load_references(&db, token)?
        {
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            publish_refs(
                &tx,
                &Owner {
                    token,
                    book: book.to_owned(),
                    idx: -1,
                },
                refs,
            )?;
            tx.commit()?;
        }
        Ok(())
    }

    pub(crate) fn index_assets(&self, stop: &AtomicBool) -> Result<usize, ApiError> {
        if stop.load(Ordering::Acquire) {
            return Ok(0);
        }
        let mut db = self.write_connection()?;
        let seeded = seed_assets(&mut db)?;
        let owners = db
            .prepare(
                "SELECT id,book_id,idx FROM asset_pending WHERE blocked=0 ORDER BY id LIMIT ?",
            )?
            .query_map([ASSET_BATCH], |r| {
                Ok(Owner {
                    token: r.get(0)?,
                    book: r.get(1)?,
                    idx: r.get(2)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let count = owners.len();
        let mut pending = Vec::with_capacity(count);
        let mut bytes = 0usize;
        for owner in owners {
            if stop.load(Ordering::Acquire) {
                return Ok(0);
            }
            if let Some((size, refs)) = load_references(&db, owner.token)? {
                bytes += size;
                pending.push((owner, refs));
                // shortcut: one oversized owner remains atomic, split asset ownership if parsing large bodies becomes a bottleneck.
                if bytes >= INDEX_BYTES {
                    break;
                }
            }
        }
        if !pending.is_empty() && !stop.load(Ordering::Acquire) {
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            for (owner, refs) in pending {
                publish_refs(&tx, &owner, refs)?;
            }
            tx.commit()?;
        }
        Ok(seeded + count)
    }

    pub(crate) fn image_cache_stats(&self) -> Result<Value, ApiError> {
        let mut result = rows(&*self.reader_connection()?,
            "SELECT max_bytes,total_bytes,protected_bytes,legacy_bytes,entries,protected_entries,pending_bytes,
             total_bytes-protected_bytes AS cache_bytes,
             (SELECT inventory_images FROM asset_index_state WHERE id=1) AS inventory_complete,
             (clear_until>0 OR EXISTS(SELECT 1 FROM asset_gc_candidates WHERE evict=2)) AS clearing
             FROM image_cache_state WHERE id=1", [])?
            .pop().ok_or(rusqlite::Error::QueryReturnedNoRows)?;
        for key in ["inventory_complete", "clearing"] {
            result[key] = (result[key].as_i64() == Some(1)).into();
        }
        Ok(result)
    }

    pub(crate) fn set_image_cache_limit(&self, value: &Value) -> Result<Value, ApiError> {
        let limit = value
            .as_i64()
            .filter(|n| (0..=64 * 1024 * 1024 * 1024).contains(n))
            .ok_or_else(|| {
                ApiError::new(
                    400,
                    "Giới hạn cache ảnh phải là số byte nguyên từ 0 đến 64 GiB.",
                )
            })?;
        self.write_connection()?.execute(
            "UPDATE image_cache_state SET max_bytes=? WHERE id=1",
            [limit],
        )?;
        self.image_cache_stats()
    }

    pub(crate) fn clear_image_cache(&self) -> Result<Value, ApiError> {
        self.write_connection()?.execute(
            "UPDATE image_cache_state SET clear_until=coalesce((SELECT max(id) FROM image_cache),0),clear_cursor=0 WHERE id=1",
            [],
        )?;
        self.image_cache_stats()
    }

    pub(crate) fn queue_image_evictions(&self, stop: &AtomicBool) -> Result<usize, ApiError> {
        if stop.load(Ordering::Acquire) {
            return Ok(0);
        }
        let mut db = self.write_connection()?;
        if !asset_index_ready(&db)?
            || !db.query_row(
                "SELECT clear_until>0 OR trim_to>=0 OR
            total_bytes-protected_bytes-pending_bytes>max_bytes FROM image_cache_state WHERE id=1",
                [],
                |r| r.get::<_, bool>(0),
            )?
        {
            return Ok(0);
        }
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if !asset_index_ready(&tx)? {
            return Ok(0);
        }
        let (until,cursor,trim,target,remaining,pending): (i64,i64,i64,i64,i64,i64) = tx.query_row(
            "SELECT clear_until,clear_cursor,trim_to,CASE WHEN trim_to>=0 THEN min(max_bytes,trim_to) ELSE max_bytes END,
             total_bytes-protected_bytes,pending_bytes FROM image_cache_state WHERE id=1", [],
            |r| Ok((r.get(0)?,r.get(1)?,r.get(2)?,r.get(3)?,r.get(4)?,r.get(5)?)),
        )?;
        let mut needed = remaining - pending - target;
        let clear = until > 0;
        let candidates = if clear {
            let rows = tx.prepare(
                "SELECT id,name,size FROM image_cache WHERE protected=0 AND id>? AND id<=? ORDER BY id LIMIT ?",
            )?.query_map(params![cursor,until,ASSET_BATCH], image_candidate)?
                .collect::<Result<Vec<_>,_>>()?;
            tx.execute(
                "UPDATE image_cache_state SET clear_cursor=?,clear_until=? WHERE id=1",
                params![
                    rows.last().map_or(0, |row| row.0),
                    if rows.len() == ASSET_BATCH as usize {
                        until
                    } else {
                        0
                    }
                ],
            )?;
            rows
        } else if needed > 0 {
            tx.prepare(
                "SELECT id,name,size FROM image_cache WHERE protected=0 AND evicting=0 AND size>0
                ORDER BY last_used,id LIMIT ?",
            )?
            .query_map([ASSET_BATCH], image_candidate)?
            .collect::<Result<Vec<_>, _>>()?
        } else {
            if remaining <= target && trim >= 0 {
                tx.execute("UPDATE image_cache_state SET trim_to=-1 WHERE id=1", [])?;
            }
            Vec::new()
        };
        let mut queued = 0;
        for (_, name, size) in candidates {
            tx.execute("INSERT INTO asset_gc_candidates(kind,name,evict) VALUES('images',?,?)
                ON CONFLICT(kind,name) DO UPDATE SET evict=max(evict,excluded.evict),generation=generation+1,retry_at=0",
                params![name,if clear { 2 } else { 1 }])?;
            queued += 1;
            needed -= size;
            if !clear && needed <= 0 {
                break;
            }
        }
        tx.commit()?;
        Ok(queued)
    }

    pub(crate) fn cleanup_assets(&self, stop: &AtomicBool) -> Result<usize, ApiError> {
        if stop.load(Ordering::Acquire) {
            return Ok(0);
        }
        let mut db = self.write_connection()?;
        if !asset_index_ready(&db)? {
            return Ok(0);
        }
        let candidates = db
            .prepare(
                "SELECT id,kind,name,generation,evict FROM asset_gc_candidates
            WHERE retry_at<=? ORDER BY retry_at,id LIMIT ?",
            )?
            .query_map(params![now(), ASSET_BATCH], |r| {
                Ok(Candidate {
                    id: r.get(0)?,
                    kind: r.get(1)?,
                    name: r.get(2)?,
                    generation: r.get(3)?,
                    evict: r.get(4)?,
                })
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let mut processed = 0;
        for candidate in candidates {
            if stop.load(Ordering::Acquire) || !self.cleanup_candidate(&mut db, &candidate)? {
                break;
            }
            processed += 1;
        }
        Ok(processed)
    }

    /// Queue one managed upload and settle it now (reference `Store.cleanup_file`).
    pub(crate) fn cleanup_file(&self, name: &str) -> Result<bool, ApiError> {
        let mut db = self.write_connection()?;
        db.execute(
            "INSERT INTO asset_gc_candidates(kind,name) VALUES('files',?)
            ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0,evict=0",
            [name],
        )?;
        let row = db
            .query_row(
                "SELECT id,kind,name,generation,evict FROM asset_gc_candidates WHERE kind='files' AND name=?",
                [name],
                |r| {
                    Ok(Candidate {
                        id: r.get(0)?,
                        kind: r.get(1)?,
                        name: r.get(2)?,
                        generation: r.get(3)?,
                        evict: r.get(4)?,
                    })
                },
            )
            .optional()?;
        match row {
            Some(row) => self.cleanup_candidate(&mut db, &row),
            None => Ok(true),
        }
    }

    fn cleanup_candidate(&self, db: &mut Connection, row: &Candidate) -> Result<bool, ApiError> {
        let Some((name, old_stage)) = managed_name(&row.kind, &row.name) else {
            remove_candidate(db, row)?;
            return Ok(true);
        };
        let folder = self.root().join(&row.kind);
        let original = folder.join(name);
        let staged = folder.join(if old_stage {
            row.name.clone()
        } else {
            format!("{name}.gc-{}", row.id)
        });
        let image = row.kind == "images";
        let leased = if image {
            folder.join(&name[..64])
        } else {
            original.clone()
        };
        {
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            if !current_candidate(&tx, row)? {
                return Ok(true);
            }
            if !asset_index_ready(&tx)? {
                return Ok(false);
            }
            let active = self.asset_leases.lock();
            if active.contains_key(&leased) {
                retry_candidate(&tx, row, 1.0)?;
                tx.commit()?;
                return Ok(true);
            }
            let referenced: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM asset_refs WHERE kind=? AND name=?)",
                params![row.kind, name],
                |r| r.get(0),
            )?;
            let cache = if image {
                tx.query_row(
                    "SELECT protected,pin_count FROM image_cache WHERE name=?",
                    [name],
                    |r| Ok((r.get::<_, bool>(0)?, r.get::<_, i64>(1)?)),
                )
                .optional()?
            } else {
                None
            };
            let evict = row.evict > 0 && cache.is_some_and(|(protected, _)| !protected);
            let keep =
                !evict && (referenced || row.evict > 0 || cache.is_some_and(|(_, pins)| pins > 0));
            // Keep the stable candidate id until after unlink so an interrupted rename is discoverable.
            match stage_asset(&original, &staged, keep, old_stage) {
                Ok(Stage::Settle { missing }) => {
                    if missing && image {
                        if keep {
                            tx.execute("UPDATE image_cache SET size=0 WHERE name=?", [name])?;
                        } else {
                            settle_missing_image(&tx, name)?;
                        }
                    }
                    remove_candidate(&tx, row)?;
                    tx.commit()?;
                    return Ok(true);
                }
                Ok(Stage::Unlink) => tx.commit()?,
                Err(e) => {
                    retry_candidate(&tx, row, retry_delay(&e))?;
                    tx.commit()?;
                    return Ok(true);
                }
            }
        }
        // The slow unlink holds neither the database transaction nor the asset-registration lock.
        match regular_asset(&staged).and_then(|file| {
            if file.is_some() {
                fs::remove_file(&staged)
            } else {
                Ok(())
            }
        }) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::NotFound => (),
            Err(e) => {
                retry_candidate(db, row, retry_delay(&e))?;
                return Ok(true);
            }
        }
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if current_candidate(&tx, row)? {
            let active = self.asset_leases.lock();
            if active.contains_key(&leased) {
                retry_candidate(&tx, row, 1.0)?;
            } else {
                match regular_asset(&original) {
                    Ok(Some(_)) => retry_candidate(&tx, row, 0.0)?,
                    Ok(None) => {
                        if image {
                            settle_missing_image(&tx, name)?;
                        }
                        remove_candidate(&tx, row)?;
                    }
                    Err(e) => retry_candidate(&tx, row, retry_delay(&e))?,
                }
            }
            tx.commit()?;
        }
        Ok(true)
    }
}

fn image_candidate(row: &rusqlite::Row<'_>) -> rusqlite::Result<(i64, String, i64)> {
    Ok((row.get(0)?, row.get(1)?, row.get(2)?))
}

fn seed_assets(db: &mut Connection) -> Result<usize, ApiError> {
    if db.query_row(
        "SELECT complete FROM asset_index_state WHERE id=1",
        [],
        |r| r.get::<_, bool>(0),
    )? {
        return Ok(0);
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let (book, idx, complete) = tx.query_row(
        "SELECT book_id,idx,complete FROM asset_index_state WHERE id=1",
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
    let owners = tx.prepare("SELECT book_id,idx FROM chapters WHERE (book_id,idx)>(?,?) ORDER BY book_id,idx LIMIT ?")?
        .query_map(params![book,idx,ASSET_BATCH], |r| Ok((r.get::<_,String>(0)?,r.get::<_,i64>(1)?)))?
        .collect::<Result<Vec<_>,_>>()?;
    for (book, idx) in &owners {
        tx.execute(
            "INSERT OR IGNORE INTO asset_pending(book_id,idx) VALUES(?,?)",
            params![book, idx],
        )?;
    }
    if let Some((book, idx)) = owners.last() {
        tx.execute(
            "UPDATE asset_index_state SET book_id=?,idx=? WHERE id=1",
            params![book, idx],
        )?;
    }
    if owners.len() < ASSET_BATCH as usize {
        tx.execute("UPDATE asset_index_state SET complete=1 WHERE id=1", [])?;
    }
    tx.commit()?;
    Ok(owners.len())
}

fn load_references(
    db: &Connection,
    token: i64,
) -> Result<Option<(usize, Option<References>)>, ApiError> {
    let owner = db.query_row(
        "SELECT p.idx,b.path,CASE WHEN p.idx=-1 THEN b.cover ELSE '' END,c.html,c.original_html,c.images
         FROM asset_pending p JOIN books b ON b.id=p.book_id LEFT JOIN chapters c ON c.book_id=p.book_id AND c.idx=p.idx
         WHERE p.id=?",
        [token], |r| Ok((r.get::<_,i64>(0)?,r.get::<_,String>(1)?,r.get::<_,String>(2)?,
            r.get::<_,Option<String>>(3)?,r.get::<_,Option<String>>(4)?,r.get::<_,Option<String>>(5)?)),
    ).optional()?;
    let Some((idx, base, cover, html, original, images)) = owner else {
        return Ok(None);
    };
    let mut references = References::new();
    let size = base.len()
        + cover.len()
        + [&html, &original, &images]
            .into_iter()
            .map(|field| field.as_ref().map_or(0, String::len))
            .sum::<usize>();
    let collected = (|| {
        let mut urls = Vec::new();
        if idx == -1 {
            let file = base
                .rsplit(['/', '\\'])
                .next()
                .unwrap_or("")
                .to_ascii_lowercase();
            if managed_name("files", &file).is_some_and(|(_, staged)| !staged) {
                references.insert(("files", file));
            }
            urls.extend(http::asset_image_urls(&cover, &base)?);
        } else if let Some(images) = images {
            let images = parse_json(&images)?;
            for image in images
                .as_array()
                .ok_or_else(|| ApiError::new(400, "Invalid chapter images"))?
            {
                let url = image
                    .as_str()
                    .ok_or_else(|| ApiError::new(400, "Invalid image reference"))?;
                urls.extend(http::asset_image_urls(url, &base)?);
            }
            for markup in [html, original]
                .into_iter()
                .flatten()
                .filter(|s| !s.is_empty())
            {
                urls.extend(http::asset_markup_urls(&markup, &base)?);
            }
        }
        references.extend(
            urls.into_iter()
                .map(|url| ("images", format!("{:x}", Sha256::digest(url.as_bytes())))),
        );
        Ok::<_, ApiError>(references)
    })();
    Ok(Some((size, collected.ok())))
}

fn publish_refs(db: &Connection, owner: &Owner, refs: Option<References>) -> Result<(), ApiError> {
    let Some(refs) = refs else {
        db.execute(
            "UPDATE asset_pending SET blocked=1 WHERE id=?",
            [owner.token],
        )?;
        return Ok(());
    };
    if db.execute("DELETE FROM asset_pending WHERE id=?", [owner.token])? == 0 {
        return Ok(());
    }
    let previous = db
        .prepare("SELECT kind,name FROM asset_refs WHERE book_id=? AND idx=?")?
        .query_map(params![owner.book, owner.idx], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })?
        .collect::<Result<BTreeSet<_>, _>>()?;
    let refs = refs
        .into_iter()
        .map(|(kind, name)| (kind.to_owned(), name))
        .collect::<BTreeSet<_>>();
    for (kind, name) in previous.difference(&refs) {
        db.execute(
            "DELETE FROM asset_refs WHERE book_id=? AND idx=? AND kind=? AND name=?",
            params![owner.book, owner.idx, kind, name],
        )?;
    }
    for (kind, name) in refs.difference(&previous) {
        db.execute(
            "INSERT INTO asset_refs(book_id,idx,kind,name) VALUES(?,?,?,?)",
            params![owner.book, owner.idx, kind, name],
        )?;
    }
    Ok(())
}

fn asset_index_ready(db: &Connection) -> Result<bool, ApiError> {
    Ok(db.query_row(
        "SELECT complete AND NOT EXISTS(SELECT 1 FROM asset_pending) FROM asset_index_state WHERE id=1",
        [], |r| r.get(0),
    )?)
}

fn current_candidate(db: &Connection, row: &Candidate) -> rusqlite::Result<bool> {
    db.query_row(
        "SELECT EXISTS(SELECT 1 FROM asset_gc_candidates WHERE id=? AND generation=?)",
        params![row.id, row.generation],
        |r| r.get(0),
    )
}

fn remove_candidate(db: &Connection, row: &Candidate) -> rusqlite::Result<()> {
    db.execute(
        "DELETE FROM asset_gc_candidates WHERE id=? AND generation=?",
        params![row.id, row.generation],
    )?;
    Ok(())
}

fn retry_candidate(db: &Connection, row: &Candidate, seconds: f64) -> rusqlite::Result<()> {
    db.execute(
        "UPDATE asset_gc_candidates SET retry_at=? WHERE id=? AND generation=?",
        params![
            if seconds == 0.0 { 0.0 } else { now() + seconds },
            row.id,
            row.generation
        ],
    )?;
    Ok(())
}

fn retry_delay(error: &io::Error) -> f64 {
    if error.kind() == io::ErrorKind::NotFound {
        1.0
    } else {
        30.0
    }
}

fn settle_missing_image(db: &Connection, name: &str) -> rusqlite::Result<()> {
    db.execute(
        "UPDATE image_cache SET size=0 WHERE name=? AND protected=1",
        [name],
    )?;
    db.execute(
        "DELETE FROM image_cache WHERE name=? AND protected=0",
        [name],
    )?;
    Ok(())
}

enum Stage {
    Settle { missing: bool },
    Unlink,
}

fn stage_asset(original: &Path, staged: &Path, keep: bool, old_stage: bool) -> io::Result<Stage> {
    let staged_exists = regular_asset(staged)?.is_some();
    let original_exists = regular_asset(original)?.is_some();
    if keep {
        if staged_exists && !original_exists {
            fs::rename(staged, original)?;
            return Ok(Stage::Settle { missing: false });
        }
        if !staged_exists {
            return Ok(Stage::Settle {
                missing: !original_exists,
            });
        }
    } else if !staged_exists {
        if !original_exists {
            return Ok(Stage::Settle { missing: true });
        }
        if old_stage {
            return Ok(Stage::Settle { missing: false });
        }
        fs::rename(original, staged)?;
    }
    Ok(Stage::Unlink)
}

fn managed_name<'a>(kind: &str, name: &'a str) -> Option<(&'a str, bool)> {
    let (name, staged) = name
        .strip_suffix(".gc")
        .and_then(|prefix| prefix.rsplit_once('.'))
        .filter(|(_, token)| hex(token, 32))
        .map_or((name, false), |(base, _)| (base, true));
    let valid = match kind {
        "files" => name.split_once('.').is_some_and(|(base, ext)| {
            hex(base, 64)
                && !ext.is_empty()
                && ext
                    .bytes()
                    .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        }),
        "images" => {
            hex(name, 64)
                || name
                    .strip_suffix(".tmp")
                    .and_then(|s| s.split_once('.'))
                    .is_some_and(|(base, token)| hex(base, 64) && hex(token, 32))
        }
        _ => false,
    };
    valid.then_some((name, staged))
}

fn hex(value: &str, length: usize) -> bool {
    value.len() == length
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn safe_asset_type(meta: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        if meta.file_attributes() & 0x400 != 0 {
            return false;
        }
    }
    !meta.file_type().is_symlink()
}

fn regular_asset(path: &Path) -> io::Result<Option<Metadata>> {
    let folder = path
        .parent()
        .ok_or_else(|| io::Error::other("Missing asset directory"))?;
    if !safe_asset_directory(folder)? {
        return match fs::symlink_metadata(folder) {
            Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
            _ => Err(io::Error::new(
                io::ErrorKind::PermissionDenied,
                "Unsafe asset directory",
            )),
        };
    }
    match fs::symlink_metadata(path) {
        Ok(meta) if meta.is_file() && safe_asset_type(&meta) => Ok(Some(meta)),
        Ok(_) => Err(io::Error::new(
            io::ErrorKind::PermissionDenied,
            "Unsafe asset file",
        )),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(e) => Err(e),
    }
}

fn safe_asset_directory(folder: &Path) -> io::Result<bool> {
    match fs::symlink_metadata(folder) {
        Ok(meta) if meta.is_dir() && safe_asset_type(&meta) => {
            Ok(crate::library::checked_path(folder).map_err(io::Error::other)? == folder)
        }
        Ok(_) => Ok(false),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(e) => Err(e),
    }
}
