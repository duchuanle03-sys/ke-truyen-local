use crate::{
    ApiError, Library, http,
    reader::{book_value, chapter_value, now, rows, track},
};
use rusqlite::{Connection, OptionalExtension, TransactionBehavior, params};
use serde_json::{Value, json};

const TRASH_RETENTION: f64 = 30.0 * 24.0 * 60.0 * 60.0;
const ACTIVE_JOB: &str = "status IN ('queued','running','paused','verification_required')";

impl Library {
    pub(crate) fn edit_chapter(
        &self,
        book: &str,
        idx: i64,
        body: &Value,
    ) -> Result<Value, ApiError> {
        // The Python handler evaluates the HTML fallback before selecting the text field.
        let fallback = http::html_to_text(match body.get("html") {
            None => "",
            Some(Value::String(text)) => text,
            Some(_) => return Err(ApiError::from(rusqlite::Error::InvalidQuery)),
        })?;
        let text = body.get("text").map_or(fallback, http::py_string);
        let title = body.get("title").map(http::py_string);
        let html = format!("<p>{}</p>", http::escape(&text).replace('\n', "</p><p>"));
        self.change_chapter(book, idx, Some((&html, &text, title.as_deref())))
    }

    pub(crate) fn restore_chapter(&self, book: &str, idx: i64) -> Result<Value, ApiError> {
        self.change_chapter(book, idx, None)
    }

    fn change_chapter(
        &self,
        book: &str,
        idx: i64,
        edited: Option<(&str, &str, Option<&str>)>,
    ) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if edited.is_some() {
            let kind: String = tx
                .query_row("SELECT kind FROM books WHERE id=?", [book], |r| r.get(0))
                .optional()?
                .ok_or_else(book_missing)?;
            if kind != "text" {
                return Err(ApiError::new(
                    400,
                    "Chương ảnh không dùng trình sửa văn bản.",
                ));
            }
        }
        let loaded: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM chapters WHERE book_id=? AND idx=? AND downloaded!=0)",
            params![book, idx],
            |r| r.get(0),
        )?;
        if !loaded {
            return Err(ApiError::new(
                400,
                if edited.is_some() {
                    "Hãy mở lại nội dung chương trước khi chỉnh sửa."
                } else {
                    "Hãy mở lại chương trước khi khôi phục."
                },
            ));
        }
        track(&tx, book, idx)?;
        tx.execute(
            "UPDATE chapter_cache SET kind='protected' WHERE book_id=? AND idx=?",
            params![book, idx],
        )?;
        if let Some((html, text, title)) = edited {
            tx.execute(
                "UPDATE chapters SET original_html=coalesce(original_html,html),
                 original_text=coalesce(original_text,text),html=?,text=?,title=coalesce(?,title)
                 WHERE book_id=? AND idx=?",
                params![html, text, title, book, idx],
            )?;
        } else {
            tx.execute(
                "UPDATE chapters SET html=coalesce(original_html,html),text=coalesce(original_text,text),
                 original_html=NULL,original_text=NULL WHERE book_id=? AND idx=?",
                params![book,idx],
            )?;
        }
        tx.commit()?;
        chapter_value(
            rows(
                &db,
                "SELECT * FROM chapters WHERE book_id=? AND idx=?",
                params![book, idx],
            )?
            .pop()
            .ok_or_else(|| ApiError::new(404, "Không tìm thấy chương."))?,
        )
    }

    pub(crate) fn trash_book(&self, book: &str) -> Result<Value, ApiError> {
        self.trash_with_active_download(book, None)
    }

    pub(crate) fn trash_with_active_download(
        &self,
        book: &str,
        active: Option<&str>,
    ) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let time = now();
        if tx.execute(
            "UPDATE books SET trashed_at=CASE WHEN trashed_at=0 THEN ? ELSE trashed_at END WHERE id=?",
            params![time,book],
        )? == 0 {
            return Err(book_missing());
        }
        tx.execute(
            &format!(
                "UPDATE download_jobs SET status=CASE WHEN id=? THEN status ELSE 'cancelled' END,cancelled=1,updated_at=?
             WHERE book_id=? AND {ACTIVE_JOB}"
            ),
            params![active,time, book],
        )?;
        tx.commit()?;
        self.get_book(book)
    }

    pub(crate) fn restore_book(&self, book: &str) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let trashed = trashed_at(&tx, book)?;
        if trashed != 0.0 && trashed + TRASH_RETENTION <= now() {
            return Err(ApiError::new(
                400,
                "Truyện đã hết thời hạn 30 ngày trong thùng rác, không thể khôi phục.",
            ));
        }
        let item = rows(
            &tx,
            "UPDATE books SET trashed_at=0,archived=0 WHERE id=? RETURNING *",
            [book],
        )?
        .pop()
        .ok_or_else(book_missing)?;
        tx.commit()?;
        book_value(item)
    }

    pub(crate) fn purge_book(&self, book: &str) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let active: bool = tx.query_row(
            &format!("SELECT EXISTS(SELECT 1 FROM download_jobs WHERE book_id=? AND {ACTIVE_JOB})"),
            [book],
            |r| r.get(0),
        )?;
        if active {
            return Err(ApiError::new(
                400,
                "Đang dừng tác vụ tải của truyện. Hãy thử lại sau khi tác vụ dừng.",
            ));
        }
        if trashed_at(&tx, book)? == 0.0 {
            return Err(ApiError::new(
                400,
                "Chỉ xóa vĩnh viễn truyện đã nằm trong thùng rác.",
            ));
        }
        purge(&tx, book)?;
        tx.commit()?;
        Ok(json!({"id":book,"deleted":true}))
    }

    pub(crate) fn purge_expired_book(&self) -> Result<bool, ApiError> {
        let mut db = self.write_connection()?;
        let cutoff = now() - TRASH_RETENTION;
        let candidate = format!(
            "SELECT id FROM books b WHERE (trashed_at>0)=1 AND trashed_at<=?
             AND NOT EXISTS(SELECT 1 FROM download_jobs WHERE book_id=b.id AND {ACTIVE_JOB}) LIMIT 1"
        );
        let Some(book) = db
            .query_row(&candidate, [cutoff], |r| r.get::<_, String>(0))
            .optional()?
        else {
            return Ok(false);
        };
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let expired: bool = tx.query_row(
            &format!(
                "SELECT EXISTS(SELECT 1 FROM books b WHERE id=? AND trashed_at>0 AND trashed_at<=?
              AND NOT EXISTS(SELECT 1 FROM download_jobs WHERE book_id=b.id AND {ACTIVE_JOB}))"
            ),
            params![book, cutoff],
            |r| r.get(0),
        )?;
        if expired {
            purge(&tx, &book)?;
        }
        tx.commit()?;
        Ok(expired)
    }
}

pub(crate) fn recover_trashed_jobs(db: &Connection) -> Result<(), ApiError> {
    db.execute(
        &format!("UPDATE download_jobs SET status='cancelled',cancelled=1 WHERE {ACTIVE_JOB}
            AND (cancelled=1 OR EXISTS(SELECT 1 FROM books b WHERE b.id=book_id AND b.trashed_at>0))"),
        [],
    )?;
    Ok(())
}

fn book_missing() -> ApiError {
    ApiError::new(404, "Không tìm thấy truyện.")
}

fn trashed_at(db: &Connection, book: &str) -> Result<f64, ApiError> {
    db.query_row("SELECT trashed_at FROM books WHERE id=?", [book], |r| {
        r.get(0)
    })
    .optional()?
    .ok_or_else(book_missing)
}

fn purge(db: &Connection, book: &str) -> Result<(), ApiError> {
    db.execute("DELETE FROM notes WHERE book_id=?", [book])?;
    // The reference cascades keep GC queue generation changes in the same transaction.
    db.execute("DELETE FROM books WHERE id=?", [book])?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use crate::reader::tests::writable;
    use serde_json::json;

    #[test]
    fn m3_cache_evicts_only_eligible_chapters_and_clears_search() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        db.execute("UPDATE books SET source='qq' WHERE id='book'", [])
            .unwrap();
        db.execute(
            "UPDATE chapter_cache SET kind='cache' WHERE book_id='book' AND idx=0",
            [],
        )
        .unwrap();
        db.execute("UPDATE chapters SET text='Ambiguous old body',downloaded=1 WHERE book_id='book' AND idx=1", []).unwrap();
        library
            .index_search(&std::sync::atomic::AtomicBool::new(false))
            .unwrap();
        library
            .reader(&json!({"id":"reader","book_id":"book","index":0}))
            .unwrap();
        library.set_chapter_cache_limit(&json!(0)).unwrap();
        library.clear_chapter_cache().unwrap();
        assert_eq!(library.maintain_chapter_cache().unwrap(), 0);
        assert_eq!(library.chapter_cache_stats().unwrap()["cache_count"], 1);
        library
            .reader(&json!({"id":"reader","sequence":1}))
            .unwrap();
        assert!(library.maintain_chapter_cache().unwrap() > 0);
        assert_eq!(library.chapter_cache_stats().unwrap()["cache_count"], 0);
        assert_eq!(library.chapter_cache_stats().unwrap()["legacy_count"], 1);
        assert_eq!(
            library.search_content("book", "Việt", "-1").unwrap()["results"],
            json!([])
        );
        db.execute(
            "INSERT INTO chapter_search_fts(chapter_search_fts,rank) VALUES('integrity-check',1)",
            [],
        )
        .unwrap();
        assert_eq!(
            library.chapter("book", 1, None, 0).unwrap()["text"],
            "Ambiguous old body"
        );
    }

    #[test]
    fn m3_edit_restore_retains_first_original_and_protection() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        db.execute("UPDATE books SET source='qq' WHERE id='book'", [])
            .unwrap();
        db.execute(
            "UPDATE chapter_cache SET kind='cache' WHERE book_id='book' AND idx=0",
            [],
        )
        .unwrap();
        let first = library
            .edit_chapter(
                "book",
                0,
                &json!({"text":"<script>x</script>\nThis & that", "title":9}),
            )
            .unwrap();
        assert_eq!(
            first["html"],
            "<p>&lt;script&gt;x&lt;/script&gt;</p><p>This &amp; that</p>"
        );
        assert_eq!(first["title"], "9");
        assert_eq!(first["original_text"], "Tiếng Việt");
        let second = library.edit_chapter("book", 0, &json!({"html":"<p>Hello &amp; world </p><div hidden>Skip</div>\r<p>next<br>line</p>"})).unwrap();
        assert_eq!(second["text"], "Hello & world\nnext\nline");
        assert_eq!(second["original_text"], first["original_text"]);
        let restored = library.restore_chapter("book", 0).unwrap();
        assert_eq!(restored["text"], first["original_text"]);
        assert_eq!(restored["original_text"], serde_json::Value::Null);
        assert_eq!(restored["title"], "9");
        assert_eq!(
            db.query_row(
                "SELECT kind FROM chapter_cache WHERE book_id='book' AND idx=0",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "protected"
        );
        assert_eq!(library.restore_chapter("book", 1).unwrap_err().status, 400);
    }

    #[test]
    fn m3_archive_trash_purge_keeps_unrelated_notes_and_queues_known_assets() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        db.execute(
            "INSERT INTO download_jobs VALUES('job','book','queued',0,'{}',1,1)",
            [],
        )
        .unwrap();
        db.execute(
            "INSERT INTO asset_refs VALUES('book',0,'images','image')",
            [],
        )
        .unwrap();
        library
            .save_note(&json!({"id":"theirs","book_id":"book"}))
            .unwrap();
        library
            .save_note(&json!({"id":"orphan","book_id":"unrelated"}))
            .unwrap();
        library
            .update_book("book", &json!({"archived":true}))
            .unwrap();
        assert_eq!(library.purge_book("book").unwrap_err().status, 400);
        let trashed = library.trash_book("book").unwrap();
        assert_eq!(
            library.trash_book("book").unwrap()["trashed_at"],
            trashed["trashed_at"]
        );
        assert_eq!(
            db.query_row("SELECT status FROM download_jobs WHERE id='job'", [], |r| r
                .get::<_, String>(0))
                .unwrap(),
            "cancelled"
        );
        let restored = library.restore_book("book").unwrap();
        assert_eq!(restored["trashed_at"], 0.0);
        assert_eq!(restored["archived"], false);
        assert!(library.chapter("book", 0, None, 0).unwrap()["downloaded"] == true);
        library.trash_book("book").unwrap();
        assert_eq!(
            library.purge_book("book").unwrap(),
            json!({"id":"book","deleted":true})
        );
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM asset_gc_candidates WHERE kind='images' AND name='image'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
        assert_eq!(library.notes(None).unwrap()[0]["id"], "orphan");
        assert_eq!(library.notes(None).unwrap().as_array().unwrap().len(), 1);
    }
}
