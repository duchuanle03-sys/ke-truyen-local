use crate::{ApiError, Library, casefold, library, reader::rows};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::atomic::{AtomicBool, Ordering};

const SEARCH_PAGE: usize = 100;
const SEED_BATCH: usize = 256;
const INDEX_BATCH: usize = 8;
const INDEX_CHARACTERS: usize = 256 * 1024;

pub(crate) fn reclaim(db: &rusqlite::Connection) -> Result<bool, ApiError> {
    let merge: i64 = db.query_row(
        "SELECT fts_merge FROM chapter_cache_state WHERE id=1",
        [],
        |r| r.get(0),
    )?;
    if merge == 0 {
        return Ok(false);
    }
    let previous = db.total_changes();
    db.execute(
        "INSERT INTO chapter_search_fts(chapter_search_fts,rank) VALUES('merge',?)",
        [if merge == 1 { -8 } else { 8 }],
    )?;
    let next = if db.total_changes() - previous < 2 {
        0
    } else {
        2
    };
    db.execute(
        "UPDATE chapter_cache_state SET fts_merge=? WHERE id=1",
        [next],
    )?;
    Ok(true)
}
const CLEAN: &str =
    "NOT EXISTS(SELECT 1 FROM chapter_search_pending p WHERE p.book_id=d.book_id AND p.idx=d.idx)";

impl Library {
    pub(crate) fn search_content(
        &self,
        book: &str,
        query: &str,
        after: &str,
    ) -> Result<Value, ApiError> {
        let (after, overflow) = library::parse_integer(after)?;
        if query.trim_matches(casefold::python_whitespace).is_empty() {
            return Err(ApiError::new(400, "Nhập cụm từ cần tìm."));
        }
        if query.chars().count() > 500 || query.contains('\0') {
            return Err(ApiError::new(
                400,
                "Cụm từ tối đa 500 ký tự và không chứa ký tự NUL.",
            ));
        }
        if overflow || after < -1 {
            return Err(ApiError::new(400, "Vị trí phân trang không hợp lệ."));
        }
        let term = casefold::casefold(query);
        let term_count = term.chars().count();
        let mut db = self.reader_connection()?;
        let tx = db.transaction()?;
        let total: i64 = tx
            .query_row("SELECT chapter_count FROM books WHERE id=?", [book], |r| {
                r.get(0)
            })
            .optional()?
            .ok_or_else(|| ApiError::new(404, "Không tìm thấy truyện."))?;
        let indexing: bool = tx.query_row(
            "SELECT (NOT complete AND (book_id < ?1 OR (book_id = ?1 AND EXISTS(
                SELECT 1 FROM chapters WHERE book_id=?1 AND idx>s.idx)))) OR
                EXISTS(SELECT 1 FROM chapter_search_pending WHERE book_id=?1)
                FROM chapter_search_state s WHERE id=1",
            [book],
            |r| r.get(0),
        )?;
        let searched: i64 = tx.query_row(
            &format!("SELECT count(*) FROM chapter_search_docs d WHERE d.book_id=? AND {CLEAN}"),
            [book],
            |r| r.get(0),
        )?;
        let (source, filter) = if term_count >= 3 {
            (
                "chapter_search_fts CROSS JOIN chapter_search_docs d ON d.id=chapter_search_fts.rowid",
                "chapter_search_fts MATCH ?5 AND (d.literal_check=0 OR instr(d.text,?1)>0)",
            )
        } else {
            // shortcut: short queries scan the selected book's normalized documents, add short grams if profiling warrants it.
            ("chapter_search_docs d", "instr(d.text,?1)>0")
        };
        let sql = format!(
            "WITH hits AS MATERIALIZED (
                SELECT d.book_id,d.idx FROM {source} WHERE {filter}
                AND d.book_id=?2 AND d.idx>?3 AND {CLEAN} ORDER BY d.idx LIMIT ?4
            ) SELECT c.idx,c.title,c.text FROM hits JOIN chapters c
            ON c.book_id=hits.book_id AND c.idx=hits.idx WHERE c.downloaded=1 ORDER BY c.idx",
        );
        let page = SEARCH_PAGE as i64 + 1;
        let mut hits = if term_count >= 3 {
            let scope = hex_scope(book);
            let pattern = format!(
                "scope:\"{scope}\" AND text:\"{}\"",
                term.replace('"', "\"\"")
            );
            rows(&tx, &sql, params![term, book, after, page, pattern])?
        } else {
            rows(&tx, &sql, params![term, book, after, page])?
        };
        tx.commit()?;
        let next = if hits.len() > SEARCH_PAGE {
            hits.truncate(SEARCH_PAGE);
            hits[SEARCH_PAGE - 1]["idx"].clone()
        } else {
            Value::Null
        };
        let results = hits.into_iter().map(|row| {
            let text = row["text"].as_str().unwrap_or_default();
            json!({"index":row["idx"],"title":row["title"],"snippet":snippet(text,&term,term_count)})
        }).collect::<Vec<_>>();
        Ok(
            json!({"results":results,"searched":searched,"total":total,"indexing":indexing,"next_index":next}),
        )
    }

    pub(crate) fn index_search(&self, stop: &AtomicBool) -> Result<usize, ApiError> {
        if stop.load(Ordering::Acquire) {
            return Ok(0);
        }
        let mut db = self.write_connection()?;
        let seeded = seed_search(&mut db)?;
        let owners = db
            .prepare("SELECT id,book_id,idx FROM chapter_search_pending ORDER BY id LIMIT ?")?
            .query_map([INDEX_BATCH as i64], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, i64>(2)?,
                ))
            })?
            .collect::<Result<Vec<_>, _>>()?;
        let count = owners.len();
        let mut indexed = Vec::with_capacity(count);
        let mut characters = 0;
        {
            let mut load = db.prepare(
                "SELECT CASE WHEN c.downloaded!=0 THEN c.text END FROM chapter_search_pending p
                JOIN chapters c ON c.book_id=p.book_id AND c.idx=p.idx WHERE p.id=?",
            )?;
            for (token, book, idx) in owners {
                if stop.load(Ordering::Acquire) {
                    return Ok(0);
                }
                if let Some(text) = load
                    .query_row([token], |r| r.get::<_, Option<String>>(0))
                    .optional()?
                {
                    let normalized = text.map(|s| casefold::casefold(&s));
                    characters += normalized.as_deref().map_or(0, |s| s.chars().count());
                    let scope = normalized.as_ref().map(|_| hex_scope(&book));
                    indexed.push((token, book, idx, normalized, scope));
                    // shortcut: one oversized chapter is indexed atomically, partition it if measured write latency warrants it.
                    if characters >= INDEX_CHARACTERS {
                        break;
                    }
                }
            }
        }
        if stop.load(Ordering::Acquire) {
            return Ok(0);
        }
        if !indexed.is_empty() {
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            for (token, book, idx, normalized, scope) in indexed {
                if tx.execute("DELETE FROM chapter_search_pending WHERE id=?", [token])? == 0 {
                    continue;
                }
                if let Some(text) = normalized {
                    tx.execute("INSERT INTO chapter_search_docs(book_id,idx,scope,literal_check,text) VALUES(?,?,?,?,?)
                        ON CONFLICT(book_id,idx) DO UPDATE SET literal_check=excluded.literal_check,text=excluded.text",
                        params![book, idx, scope, text.contains('\0'), text])?;
                } else {
                    tx.execute(
                        "DELETE FROM chapter_search_docs WHERE book_id=? AND idx=?",
                        params![book, idx],
                    )?;
                }
            }
            tx.commit()?;
        }
        Ok(seeded + count)
    }
}

fn seed_search(db: &mut rusqlite::Connection) -> Result<usize, ApiError> {
    if db.query_row(
        "SELECT complete FROM chapter_search_state WHERE id=1",
        [],
        |r| r.get::<_, bool>(0),
    )? {
        return Ok(0);
    }
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let state = tx.query_row(
        "SELECT book_id,idx,complete FROM chapter_search_state WHERE id=1",
        [],
        |r| {
            Ok((
                r.get::<_, String>(0)?,
                r.get::<_, i64>(1)?,
                r.get::<_, bool>(2)?,
            ))
        },
    )?;
    if state.2 {
        return Ok(0);
    }
    let owners = tx.prepare("SELECT book_id,idx,downloaded FROM chapters WHERE (book_id,idx)>(?,?) ORDER BY book_id,idx LIMIT ?")?
        .query_map(params![state.0,state.1,SEED_BATCH as i64], |r| Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?, r.get::<_, bool>(2)?)))?
        .collect::<Result<Vec<_>,_>>()?;
    for (book, idx, downloaded) in &owners {
        if *downloaded {
            tx.execute(
                "INSERT OR IGNORE INTO chapter_search_pending(book_id,idx) SELECT ?1,?2
                WHERE NOT EXISTS(SELECT 1 FROM chapter_search_docs WHERE book_id=?1 AND idx=?2)",
                params![book, idx],
            )?;
        }
    }
    if let Some((book, idx, _)) = owners.last() {
        tx.execute(
            "UPDATE chapter_search_state SET book_id=?,idx=? WHERE id=1",
            params![book, idx],
        )?;
    }
    if owners.len() < SEED_BATCH {
        tx.execute("UPDATE chapter_search_state SET complete=1 WHERE id=1", [])?;
    }
    tx.commit()?;
    Ok(owners.len())
}

fn hex_scope(book: &str) -> String {
    format!("{:x}", Sha256::digest(book.as_bytes()))
}

fn snippet(text: &str, term: &str, term_count: usize) -> String {
    let folded = casefold::casefold(text);
    let at = folded.find(term).map(|byte| folded[..byte].chars().count());
    let position = if folded.chars().count() == text.chars().count() {
        at.map_or(-1, |i| i as isize)
    } else {
        let mut end = 0;
        text.chars()
            .position(|character| {
                let mut encoded = [0; 4];
                end += casefold::casefold(character.encode_utf8(&mut encoded))
                    .chars()
                    .count();
                at.is_none_or(|start| end > start)
            })
            .map_or(-1, |i| i as isize)
    };
    let start = (position - 70).max(0) as usize;
    let finish = (position + term_count as isize + 120).max(0) as usize;
    text.chars().skip(start).take(finish - start).collect()
}

#[cfg(test)]
mod tests {
    use crate::reader::tests::writable;
    use serde_json::json;
    use std::sync::atomic::AtomicBool;

    #[test]
    fn m3_search_index_excludes_pending_and_obeys_literal_matching() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        db.execute(
            "UPDATE chapters SET text=? WHERE book_id='book' AND idx=0",
            [format!("{}needle at the end", "ß".repeat(300))],
        )
        .unwrap();
        db.execute(
            "UPDATE chapters SET title='Second',text=?,downloaded=1 WHERE book_id='book' AND idx=1",
            ["a*b? \"quote\" OR NEAR(x);ab\0c"],
        )
        .unwrap();
        drop(db);
        assert_eq!(
            library.search_content("book", "needle", "-1").unwrap(),
            json!({
                "results": [], "searched": 0, "total": 2, "indexing": true, "next_index": null,
            })
        );
        assert_eq!(library.index_search(&AtomicBool::new(false)).unwrap(), 4);
        assert_eq!(
            library.search_content("book", "needle", "-1").unwrap(),
            json!({
                "results": [{"index": 0, "title": "", "snippet": format!("{}needle at the end", "ß".repeat(70))}],
                "searched": 2, "total": 2, "indexing": false, "next_index": null,
            })
        );
        for term in ["a*b?", "\"quote\" OR NEAR(x)", "OR"] {
            assert_eq!(
                library.search_content("book", term, "-1").unwrap()["results"][0]["index"],
                1
            );
        }
        assert_eq!(
            library.search_content("book", "abc", "-1").unwrap()["results"],
            json!([])
        );
        let db = library.write_connection().unwrap();
        db.execute(
            "UPDATE chapters SET text='new term' WHERE book_id='book' AND idx=0",
            [],
        )
        .unwrap();
        assert_eq!(
            library.search_content("book", "needle", "-1").unwrap(),
            json!({
                "results": [], "searched": 1, "total": 2, "indexing": true, "next_index": null,
            })
        );
        db.execute(
            "INSERT INTO chapter_search_fts(chapter_search_fts,rank) VALUES('integrity-check',1)",
            [],
        )
        .unwrap();
    }
}
