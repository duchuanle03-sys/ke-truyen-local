//! File and backup imports (references: `server.App.import_file`,
//! `library.Store._put_book` and `library.Store.import_backup`).
//!
//! Parsing and file I/O run outside SQLite transactions. Each book commits in
//! its own short write transaction; a backup import that stops part-way leaves
//! the finished books committed and is completed by running it again.

use crate::{
    ApiError, Library,
    documents::{self, suffix},
    http::{json_bytes, py_string},
};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::Read,
    path::{Path, PathBuf},
};

const MAX_JSON: u64 = 32 * 1024 * 1024;
const MAX_BACKUP: u64 = 512 * 1024 * 1024;
const MAX_BACKUP_ENTRIES: usize = 200_000;
const ONLINE: &[&str] = &["qq", "full", "hako", "stv"];
const NOTE_BATCH: usize = 500;
/// A parsed backup book, its chapter list and its preserved legacy JSON leaves.
type PendingBook = (Map<String, Value>, Vec<Value>, Vec<(String, Value)>);
// About 2,000 rows keeps one write transaction near tens of milliseconds.
const BOOK_BATCH_ROWS: usize = 2_000;

fn bad(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message)
}

fn stored(value: &Value) -> String {
    String::from_utf8(json_bytes(value)).expect("JSON is UTF-8")
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(b)) => *b,
        Some(Value::Number(n)) => n.as_f64().is_some_and(|v| v != 0.0),
        Some(Value::String(s)) => !s.is_empty(),
        Some(Value::Array(a)) => !a.is_empty(),
        Some(Value::Object(o)) => !o.is_empty(),
    }
}

/// `value or default`, as the reference spells most optional fields.
fn or<'a>(value: Option<&'a Value>, default: &'a Value) -> &'a Value {
    if truthy(value) {
        value.expect("truthy value")
    } else {
        default
    }
}

/// `dict.get(a, dict.get(b, default))`.
fn get2<'a>(map: &'a Map<String, Value>, a: &str, b: &str, default: &'a Value) -> &'a Value {
    map.get(a).or_else(|| map.get(b)).unwrap_or(default)
}

/// Python `float(value)` for JSON-decoded values.
fn py_float(value: &Value) -> Result<f64, ApiError> {
    match value {
        Value::Bool(b) => Ok(f64::from(u8::from(*b))),
        Value::Number(n) => n.as_f64().ok_or_else(|| bad("Giá trị số không hợp lệ.")),
        Value::String(s) => {
            let text = documents::strip(s);
            let digits_only = text.replace('_', "");
            let valid_underscores = !text.starts_with('_')
                && !text.ends_with('_')
                && !text.contains("__")
                && text.match_indices('_').all(|(at, _)| {
                    text[..at].ends_with(|c: char| c.is_ascii_digit())
                        && text[at + 1..].starts_with(|c: char| c.is_ascii_digit())
                });
            let candidate = if valid_underscores {
                digits_only
            } else {
                text.to_owned()
            };
            candidate.parse::<f64>().ok().ok_or_else(|| {
                bad(format!(
                    "could not convert string to float: {}",
                    crate::http::py_value_repr(value)
                ))
            })
        }
        other => Err(ApiError::new(
            500,
            format!(
                "float() argument must be a string or a real number, not '{}'",
                type_name(other)
            ),
        )),
    }
}

/// Python `int(value)` for JSON-decoded values.
fn py_int(value: &Value) -> Result<i64, ApiError> {
    match value {
        Value::Bool(b) => Ok(i64::from(*b)),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Ok(i),
            None => n
                .as_f64()
                .filter(|f| f.is_finite())
                .map(|f| f.trunc() as i64)
                .ok_or_else(|| ApiError::new(500, "cannot convert float to integer")),
        },
        Value::String(s) => crate::library::integer(s),
        other => Err(ApiError::new(
            500,
            format!(
                "int() argument must be a string, a bytes-like object or a real number, not '{}'",
                type_name(other)
            ),
        )),
    }
}

fn type_name(value: &Value) -> &'static str {
    match value {
        Value::Null => "NoneType",
        Value::Array(_) => "list",
        Value::Object(_) => "dict",
        _ => "object",
    }
}

fn sql_value(value: &Value) -> Result<rusqlite::types::Value, ApiError> {
    use rusqlite::types::Value as Sql;
    Ok(match value {
        Value::Null => Sql::Null,
        Value::Bool(b) => Sql::Integer(i64::from(*b)),
        Value::Number(n) => match n.as_i64() {
            Some(i) => Sql::Integer(i),
            None => Sql::Real(n.as_f64().unwrap_or_default()),
        },
        Value::String(s) => Sql::Text(s.clone()),
        _ => {
            return Err(ApiError::new(
                500,
                "Error binding parameter: type is not supported",
            ));
        }
    })
}

fn source_key(value: &Value) -> String {
    let text = crate::casefold::casefold(&py_string(value));
    ["qq", "full", "hako"]
        .into_iter()
        .find(|k| text.contains(k))
        .map_or(text, str::to_owned)
}

fn groups(value: Option<&Value>) -> Value {
    let minus =
        |v: &Value| v.as_str() == Some("-1") || v.as_i64() == Some(-1) || v.as_f64() == Some(-1.0);
    match value {
        None | Some(Value::Null) => json!([]),
        Some(v) if minus(v) => json!([]),
        Some(Value::Array(items)) => items
            .iter()
            .map(py_string)
            .filter(|s| s != "-1")
            .collect::<Vec<_>>()
            .into(),
        Some(other) => json!([py_string(other)]),
    }
}

fn chapter_key(url: &str) -> &str {
    crate::sources::key(url)
}

impl Library {
    /// Port of `Store._put_book` in its own short write transaction.
    pub(crate) fn put_book(
        &self,
        book: &Map<String, Value>,
        chapters: Option<&[Value]>,
    ) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let id = write_book(&tx, book, chapters)?;
        tx.commit()?;
        self.index_book_assets(&id)?;
        self.get_book(&id)
    }

    fn queue_file(&self, name: &str) -> Result<(), ApiError> {
        self.write_connection()?.execute(
            "INSERT INTO asset_gc_candidates(kind,name) VALUES('files',?)
            ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0,evict=0",
            [name],
        )?;
        Ok(())
    }

    /// Port of `App.import_file`; `project` holds the unchanged `windows/` helpers.
    pub(crate) fn import_file(
        &self,
        name: &str,
        body: &[u8],
        project: &Path,
    ) -> Result<Value, ApiError> {
        let clean = windows_name(&name.replace('\\', "/"));
        let extension = suffix(&clean).to_lowercase();
        if clean.chars().count() > 240 || extension.is_empty() {
            return Err(bad("Tên tệp không hợp lệ."));
        }
        let supported = extension == ".pdf"
            || crate::legacy_formats::SUPPORTED_EXTENSIONS.contains(&extension.as_str())
            || documents::supported_extensions().contains(&extension.as_str());
        if !supported {
            // Reject before writing: unsupported names never become managed files.
            return Err(documents::unsupported(&extension));
        }
        let key = format!("{:x}", Sha256::digest(body));
        let file_name = format!("{key}{extension}");
        let folder = self.root().join("files");
        std::fs::create_dir_all(&folder)?;
        let path = folder.join(&file_name);
        let _lease = self.asset_leases.acquire(path.clone());
        let created = !path.exists();
        let result = (|| {
            if created {
                self.queue_file(&file_name)?;
                std::fs::write(&path, body)?;
            }
            let result = if extension == ".pdf" {
                crate::platform::extract_pdf(&project.join(r"windows\pdfocr.ps1"), &path, None)?
            } else if crate::legacy_formats::SUPPORTED_EXTENSIONS.contains(&extension.as_str()) {
                crate::legacy_formats::import_legacy(&path)?
            } else {
                documents::import_document(&path)?
            };
            let Value::Object(mut book) = result["book"].clone() else {
                return Err(ApiError::new(500, "import result"));
            };
            book.insert("source".into(), "file".into());
            book.insert("path".into(), display(&path).into());
            let current = book.get("name").and_then(Value::as_str).unwrap_or_default();
            if current.is_empty() || current == key || current == documents::stem(&file_name) {
                book.insert("name".into(), documents::stem(&clean).into());
            }
            let chapters = result["chapters"].as_array().cloned().unwrap_or_default();
            let mut saved = self.put_book(&book, Some(&chapters))?;
            if truthy(saved.get("trashed_at")) {
                saved = self.restore_book(saved["id"].as_str().unwrap_or_default())?;
            }
            if truthy(result.get("warnings")) {
                saved["warnings"] = result["warnings"].clone();
            }
            Ok(saved)
        })();
        if result.is_err() && created {
            // Another import may already be using or have saved the same content-addressed file.
            drop(_lease);
            let _ = self.cleanup_file(&file_name);
        }
        result
    }

    /// Port of `Store.import_backup` with per-book commits.
    pub(crate) fn import_backup(&self, source: &Path) -> Result<Value, ApiError> {
        let mut backup = Backup::open(source)?;
        let mut report = json!({"books": 0, "chapters": 0, "annotations": 0, "orphan_annotations": 0, "warnings": []});
        let legacy = |entries: Vec<(String, Value)>| -> Result<(), ApiError> {
            if entries.is_empty() {
                return Ok(());
            }
            let mut db = self.write_connection()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            for (path, value) in entries {
                tx.execute(
                    "INSERT OR IGNORE INTO legacy VALUES(?,?)",
                    params![path, stored(&value)],
                )?;
            }
            tx.commit()?;
            Ok(())
        };
        // Parse a bounded batch outside the write lock, then commit it in one short transaction.
        let mut batch: Vec<PendingBook> = Vec::new();
        let mut rows = 0;
        let flush = |batch: &mut Vec<PendingBook>| -> Result<(), ApiError> {
            if batch.is_empty() {
                return Ok(());
            }
            let mut db = self.write_connection()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            for (book, chapters, leaves) in batch.iter() {
                write_book(&tx, book, Some(chapters))?;
                for (path, value) in leaves {
                    tx.prepare_cached("INSERT OR IGNORE INTO legacy VALUES(?,?)")?
                        .execute(params![path, stored(value)])?;
                }
            }
            // Asset references stay pending for the background indexer, which blocks cleanup until published.
            tx.commit()?;
            batch.clear();
            Ok(())
        };
        for relative in backup.book_paths.clone() {
            let book = backup.read(&relative)?;
            let Some(Value::Object(book)) = book.filter(|b| truthy(b.get("book_id"))) else {
                return Err(bad(format!("Dữ liệu truyện không hợp lệ: {relative}")));
            };
            let folder = relative
                .rsplit_once('/')
                .map_or("", |(head, _)| head)
                .to_owned();
            let chapters = backup
                .read(&format!("{folder}/chapters.json"))?
                .unwrap_or_else(|| json!([]));
            let Value::Array(chapters) = chapters else {
                return Err(bad(format!("Mục lục không hợp lệ: {folder}")));
            };
            let novel = backup
                .read(&format!("{folder}/novel.json"))?
                .unwrap_or_else(|| json!({}));
            let Value::Object(novel) = novel else {
                return Err(ApiError::new(500, "'list' object has no attribute 'get'"));
            };
            let mut merged = book.clone();
            merged.insert(
                "description".into(),
                novel
                    .get("description")
                    .cloned()
                    .unwrap_or_else(|| "".into()),
            );
            let zero = json!(0);
            let timestamp = py_float(or(book.get("timestamp"), &zero))? / 1000.0;
            merged.insert("read_at".into(), json!(timestamp));
            let mut leaves = Vec::new();
            for leaf in [
                "book.json",
                "chapters.json",
                "novel.json",
                "names.json",
                "pages.json",
            ] {
                let path = format!("{folder}/{leaf}");
                if let Some(value) = backup.read(&path)? {
                    leaves.push((path, value));
                }
            }
            report["books"] = (report["books"].as_i64().unwrap_or_default() + 1).into();
            report["chapters"] =
                (report["chapters"].as_i64().unwrap_or_default() + chapters.len() as i64).into();
            let declared = book.get("chapter_count");
            if truthy(declared) && declared.and_then(Value::as_f64) != Some(chapters.len() as f64) {
                let label = book.get("name").unwrap_or(&book["book_id"]);
                report["warnings"]
                    .as_array_mut()
                    .expect("warnings list")
                    .push(format!("Số chương khai báo khác mục lục: {}", py_string(label)).into());
            }
            rows += chapters.len() + leaves.len() + 1;
            batch.push((merged, chapters, leaves));
            if rows >= BOOK_BATCH_ROWS {
                flush(&mut batch)?;
                rows = 0;
            }
        }
        flush(&mut batch)?;
        let prefs = backup
            .read("prefs/setting.json")?
            .unwrap_or_else(|| json!({}));
        let mut group_list = prefs
            .get("shelf.group.list")
            .cloned()
            .unwrap_or_else(|| "{}".into());
        if let Value::String(text) = &group_list {
            group_list = crate::library::parse_json(text).map_err(|e| bad(e.message))?;
        }
        let group_list = group_list.as_object().cloned().unwrap_or_default();
        {
            let mut db = self.write_connection()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            for (key, name) in &group_list {
                tx.execute(
                    "INSERT OR IGNORE INTO groups_list VALUES(?,?)",
                    params![key, py_string(name)],
                )?;
            }
            tx.commit()?;
        }
        for leaf in ["notes.json", "bookmarks.json"] {
            let items = backup
                .read(&format!("data/{leaf}"))?
                .unwrap_or_else(|| json!([]));
            let items = match items {
                Value::Array(items) => items,
                Value::Object(map) => map.into_iter().map(|(k, _)| Value::String(k)).collect(),
                _ => Vec::new(),
            };
            for batch in items.chunks(NOTE_BATCH) {
                let mut rows = Vec::new();
                for item in batch {
                    let fields = item
                        .as_object()
                        .ok_or_else(|| ApiError::new(500, "'str' object has no attribute 'get'"))?;
                    let empty = json!("");
                    let book_id = py_string(get2(fields, "book_id", "name", &empty));
                    let id = match fields.get("id").or_else(|| fields.get("a")) {
                        Some(v) => py_string(v),
                        None => {
                            let mut token = [0u8; 16];
                            getrandom::fill(&mut token)
                                .map_err(|e| ApiError::new(500, e.to_string()))?;
                            token.iter().map(|b| format!("{b:02x}")).collect()
                        }
                    };
                    let zero = json!(0);
                    rows.push((
                        format!("legacy-{leaf}-{id}"),
                        book_id,
                        py_int(fields.get("chap_index").unwrap_or(&zero))?,
                        py_string(fields.get("content").unwrap_or(&empty)),
                        py_string(fields.get("title").unwrap_or(&empty)),
                        py_float(or(fields.get("position"), &zero))?,
                        if leaf == "bookmarks.json" {
                            "bookmark"
                        } else {
                            "note"
                        },
                        py_float(or(fields.get("timestamp"), &zero))? / 1000.0,
                        stored(item),
                    ));
                }
                let mut db = self.write_connection()?;
                let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
                for row in rows {
                    tx.execute(
                        "INSERT OR IGNORE INTO notes VALUES(?,?,?,?,?,?,?,?,?,?)",
                        params![
                            row.0, row.1, row.2, row.3, row.4, "", row.5, row.6, row.7, row.8
                        ],
                    )?;
                    report["annotations"] =
                        (report["annotations"].as_i64().unwrap_or_default() + 1).into();
                    let known: bool = tx.query_row(
                        "SELECT EXISTS(SELECT 1 FROM books WHERE id=?)",
                        [&row.1],
                        |r| r.get(0),
                    )?;
                    if !known {
                        report["orphan_annotations"] =
                            (report["orphan_annotations"].as_i64().unwrap_or_default() + 1).into();
                    }
                }
                tx.commit()?;
            }
        }
        for (folder, leaves) in [
            (
                "prefs",
                &[
                    "setting.json",
                    "book.json",
                    "novel.json",
                    "comic.json",
                    "tts.json",
                ][..],
            ),
            (
                "data",
                &[
                    "notes.json",
                    "bookmarks.json",
                    "names.json",
                    "downloads.json",
                    "repositories.json",
                    "chinese_names.json",
                    "chinese_vietphrases.json",
                    "trashes.json",
                ][..],
            ),
        ] {
            for leaf in leaves {
                let path = format!("{folder}/{leaf}");
                if let Some(value) = backup.read(&path)? {
                    legacy(vec![(path, value)])?;
                }
            }
        }
        report["groups"] = group_list.len().into();
        Ok(report)
    }
}

/// Write one book and its chapters inside the caller's transaction; returns the book id.
fn write_book(
    tx: &rusqlite::Transaction<'_>,
    book: &Map<String, Value>,
    chapters: Option<&[Value]>,
) -> Result<String, ApiError> {
    let empty = Value::String(String::new());
    let source = source_key(or(
        Some(get2(book, "source", "book_source", &empty)),
        &empty,
    ));
    let path = py_string(get2(book, "path", "url", &empty));
    let explicit = book.contains_key("id") || book.contains_key("book_id");
    let mut book_id = if truthy(book.get("id")) {
        py_string(&book["id"])
    } else if truthy(book.get("book_id")) {
        py_string(&book["book_id"])
    } else {
        format!(
            "{:x}",
            Sha256::digest(format!("{source}:{path}").as_bytes())
        )[..32]
            .to_owned()
    };
    let kind = if truthy(book.get("kind")) {
        py_string(&book["kind"])
    } else if book.get("book_type").and_then(Value::as_f64) == Some(2.0) {
        "comic".into()
    } else {
        "text".into()
    };
    let zero = json!(0);
    let count = match chapters {
        Some(items) => items.len() as i64,
        None => py_int(or(book.get("chapter_count"), &zero))?,
    };
    let mut position = py_float(or(
        Some(get2(book, "position", "last_position", &zero)),
        &zero,
    ))?;
    let mut progress = py_float(or(Some(get2(book, "progress", "percent", &zero)), &zero))?;
    if !position.is_finite() || position < 0.0 {
        position = 0.0;
    }
    if !progress.is_finite() {
        progress = 0.0;
    }
    let chapter_index = py_int(or(
        Some(get2(book, "chapter_index", "last_index", &zero)),
        &zero,
    ))?
    .max(0);
    let read_at = py_float(or(book.get("read_at"), &zero))?;
    let groups_json = stored(&groups(
        book.get("groups").or_else(|| book.get("book_folder")),
    ));
    let raw = stored(&Value::Object(book.clone()));
    let text = |key: &str, default: &str| -> String {
        if truthy(book.get(key)) {
            py_string(&book[key])
        } else {
            default.to_owned()
        }
    };
    let (name, author, cover, format, description) = (
        text("name", "Chưa đặt tên"),
        text("author", ""),
        text("cover", ""),
        text("format", "online"),
        text("description", ""),
    );
    let mut existing: Option<String> = if path.is_empty() {
        None
    } else {
        tx.query_row(
            "SELECT id FROM books WHERE source=? AND path=?",
            params![source, path],
            |r| r.get(0),
        )
        .optional()?
    };
    if existing.is_none() && ONLINE.contains(&source.as_str()) && !path.is_empty() {
        let mut stmt = tx.prepare("SELECT id,path FROM books WHERE source=?")?;
        let mut rows = stmt.query([&source])?;
        while let Some(row) = rows.next()? {
            if chapter_key(&row.get::<_, String>(1)?) == chapter_key(&path) {
                existing = Some(row.get(0)?);
                break;
            }
        }
    }
    if let Some(id) = existing.filter(|_| !explicit) {
        book_id = id;
    }
    let chapter_url = |chapter: &Value| -> String {
        let fields = chapter.as_object();
        fields.map_or_else(String::new, |c| {
            py_string(or(Some(get2(c, "url", "path", &empty)), &empty))
        })
    };
    if let Some(items) = chapters.filter(|_| ONLINE.contains(&source.as_str())) {
        let mut stmt = tx.prepare("SELECT url FROM chapters WHERE book_id=? ORDER BY idx")?;
        let old = stmt
            .query_map([&book_id], |r| r.get::<_, String>(0))?
            .collect::<Result<Vec<_>, _>>()?;
        let new = items.iter().map(chapter_url).collect::<Vec<_>>();
        if !old.is_empty()
            && (new.len() < old.len()
                || old
                    .iter()
                    .zip(&new)
                    .any(|(a, b)| chapter_key(a) != chapter_key(b)))
        {
            return Err(bad(
                "Mục lục nguồn đã đổi thứ tự hoặc thiếu chương. Thư viện, ghi chú và tiến độ cũ được giữ nguyên; cần đối chiếu chương trước khi cập nhật.",
            ));
        }
    }
    let now = crate::reader::now();
    tx.execute(
            "INSERT INTO books(id,name,author,source,path,cover,kind,format,description,
                progress,chapter_index,position,groups_json,added_at,updated_at,read_at,chapter_count,raw)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET name=excluded.name,author=excluded.author,
                source=excluded.source,path=excluded.path,cover=excluded.cover,kind=excluded.kind,
                format=excluded.format,description=excluded.description,
                chapter_count=excluded.chapter_count,updated_at=excluded.updated_at,
                read_at=CASE WHEN books.read_at=0 THEN excluded.read_at ELSE books.read_at END,raw=excluded.raw",
            params![
                book_id, name, author, source, path, cover, kind, format, description,
                progress.clamp(0.0, 1.0), chapter_index, position.min(1.0), groups_json, now, now, read_at, count, raw
            ],
        )?;
    for (index, chapter) in chapters.unwrap_or_default().iter().enumerate() {
        let fields = chapter.as_object().ok_or_else(|| {
            ApiError::new(
                500,
                format!("'{}' object has no attribute 'get'", type_name(chapter)),
            )
        })?;
        let title_value = get2(fields, "title", "name", &empty);
        let title = if truthy(Some(title_value)) {
            py_string(title_value)
        } else {
            format!("Chương {}", index + 1)
        };
        let url = chapter_url(chapter);
        let old: Option<(String, String)> = tx
            .prepare_cached("SELECT url,raw FROM chapters WHERE book_id=? AND idx=?")?
            .query_row(params![book_id, index as i64], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })
            .optional()?;
        let same_url = old
            .as_ref()
            .is_some_and(|(u, _)| chapter_key(u) == chapter_key(&url));
        let mut metadata = match &old {
            Some((_, raw)) if same_url => crate::library::parse_json(raw)?,
            _ => json!({}),
        };
        let meta = metadata
            .as_object_mut()
            .ok_or(rusqlite::Error::InvalidQuery)?;
        for (key, value) in fields {
            if !["html", "text", "images", "original_html", "original_text"].contains(&key.as_str())
                && (!value.is_null()
                    || !["language", "translation", "is_vip", "access"].contains(&key.as_str()))
            {
                meta.insert(key.clone(), value.clone());
            }
        }
        if fields.get("is_vip") == Some(&Value::Bool(false)) {
            meta.shift_remove("access");
        }
        if old.is_some() && !same_url {
            tx.prepare_cached(
                    "UPDATE chapters SET html='',text='',images='[]',downloaded=0,original_html=NULL,original_text=NULL WHERE book_id=? AND idx=?",
                )?
                .execute(params![book_id, index as i64])?;
        }
        let has_body = ["html", "text", "images"]
            .iter()
            .any(|k| truthy(fields.get(*k)));
        tx.prepare_cached(
                "INSERT INTO chapters(book_id,idx,title,url,html,text,images,downloaded,raw) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(book_id,idx) DO UPDATE SET title=excluded.title,url=excluded.url,raw=excluded.raw",
            )?
            .execute(params![
                    book_id,
                    index as i64,
                    title,
                    url,
                    sql_value(fields.get("html").unwrap_or(&empty))?,
                    sql_value(fields.get("text").unwrap_or(&empty))?,
                    stored(fields.get("images").unwrap_or(&json!([]))),
                    i64::from(has_body),
                    stored(&metadata),
            ])?;
    }
    Ok(book_id)
}

fn display(path: &Path) -> String {
    let text = path.to_string_lossy();
    text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
}

/// `PureWindowsPath(name).name` for an upload name whose separators are already `/`.
fn windows_name(name: &str) -> String {
    let bytes = name.as_bytes();
    let name = if bytes.len() >= 2 && bytes[0].is_ascii_alphabetic() && bytes[1] == b':' {
        &name[2..]
    } else {
        name
    };
    name.split('/')
        .rfind(|p| !p.is_empty() && *p != ".")
        .unwrap_or_default()
        .to_owned()
}

enum BackupSource {
    Zip {
        archive: zip::ZipArchive<std::fs::File>,
        prefix: String,
    },
    Directory(PathBuf),
}

struct Backup {
    source: BackupSource,
    book_paths: Vec<String>,
}

fn book_entry(name: &str) -> bool {
    let parts = name
        .split('/')
        .filter(|p| !p.is_empty() && *p != ".")
        .collect::<Vec<_>>();
    parts.len() >= 3 && parts[parts.len() - 3] == "books" && parts[parts.len() - 1] == "book.json"
}

fn parse_backup_json(bytes: &[u8]) -> Result<Value, ApiError> {
    let text = std::str::from_utf8(bytes)
        .map_err(|e| bad(format!("'utf-8' codec can't decode bytes: {e}")))?;
    serde_json::from_str(text.strip_prefix('\u{feff}').unwrap_or(text))
        .map_err(|e| bad(e.to_string()))
}

impl Backup {
    fn open(source: &Path) -> Result<Self, ApiError> {
        let source = std::fs::canonicalize(source)
            .unwrap_or_else(|_| std::path::absolute(source).unwrap_or_else(|_| source.to_owned()));
        let source = PathBuf::from(display(&source));
        if source.is_file() {
            let not_zip = || bad("Bản sao lưu phải là thư mục hoặc ZIP.");
            let file = std::fs::File::open(&source).map_err(|_| not_zip())?;
            let mut archive = zip::ZipArchive::new(file).map_err(|_| not_zip())?;
            let mut total = 0u64;
            let mut names = Vec::new();
            for i in 0..archive.len() {
                let entry = archive.by_index_raw(i).map_err(|e| bad(e.to_string()))?;
                total += entry.size();
                let symlink = (entry.external_attributes() >> 16) & 0o170000 == 0o120000;
                names.push((
                    entry
                        .name()
                        .map_err(|e| bad(e.to_string()))?
                        .replace('\\', "/"),
                    symlink,
                ));
            }
            if archive.len() > MAX_BACKUP_ENTRIES || total > MAX_BACKUP {
                return Err(bad("Bản sao lưu vượt giới hạn xử lý an toàn."));
            }
            for (name, symlink) in &names {
                if name.starts_with('/')
                    || name.split('/').any(|p| p == "..")
                    || name.contains(':')
                    || *symlink
                {
                    return Err(bad("Đường dẫn trong ZIP không an toàn."));
                }
            }
            let names = names.into_iter().map(|(name, _)| name).collect::<Vec<_>>();
            let mut entries = names
                .iter()
                .filter(|n| book_entry(n))
                .cloned()
                .collect::<Vec<_>>();
            entries.sort();
            if entries.is_empty() {
                return Err(bad("Không tìm thấy books/*/book.json trong bản sao lưu."));
            }
            let root_of = |name: &str| {
                let parts = name
                    .split('/')
                    .filter(|p| !p.is_empty() && *p != ".")
                    .collect::<Vec<_>>();
                parts[..parts.len() - 3].join("/")
            };
            let root = root_of(&entries[0]);
            if entries.iter().any(|e| root_of(e) != root) {
                return Err(bad("ZIP chứa nhiều bản sao lưu. Hãy chọn riêng một bản."));
            }
            let prefix = if root.is_empty() {
                String::new()
            } else {
                format!("{root}/")
            };
            let book_paths = entries
                .iter()
                .map(|n| n.get(prefix.len()..).unwrap_or_default().to_owned())
                .collect();
            Ok(Self {
                source: BackupSource::Zip { archive, prefix },
                book_paths,
            })
        } else if source.is_dir() {
            let books = source.join("books");
            if !books.is_dir() {
                return Err(bad("Chọn thư mục chứa books, data và prefs."));
            }
            let mut book_paths = Vec::new();
            for entry in std::fs::read_dir(&books)? {
                let entry = entry?;
                if entry.path().join("book.json").is_file() {
                    book_paths.push(format!(
                        "books/{}/book.json",
                        entry.file_name().to_string_lossy()
                    ));
                }
            }
            book_paths.sort();
            Ok(Self {
                source: BackupSource::Directory(source),
                book_paths,
            })
        } else {
            Err(bad("Không tìm thấy đường dẫn bản sao lưu."))
        }
    }

    fn read(&mut self, relative: &str) -> Result<Option<Value>, ApiError> {
        let too_large = || bad(format!("Tệp JSON quá lớn: {relative}"));
        match &mut self.source {
            BackupSource::Zip { archive, prefix } => {
                let name = format!("{prefix}{relative}");
                let Some(index) = archive.index_for_name(&name) else {
                    return Ok(None);
                };
                let entry = archive.by_index(index).map_err(|e| bad(e.to_string()))?;
                if entry.size() > MAX_JSON {
                    return Err(too_large());
                }
                let mut bytes = Vec::new();
                entry.take(MAX_JSON + 1).read_to_end(&mut bytes)?;
                if bytes.len() as u64 > MAX_JSON {
                    return Err(too_large());
                }
                parse_backup_json(&bytes).map(Some)
            }
            BackupSource::Directory(root) => {
                let path = root.join(relative);
                let resolved = std::fs::canonicalize(&path).map(|p| PathBuf::from(display(&p)));
                if let Ok(resolved) = &resolved
                    && !resolved.starts_with(&*root)
                {
                    return Err(bad("Liên kết trong backup đi ra ngoài thư mục."));
                }
                let Some(resolved) = resolved.ok().filter(|p| p.is_file()) else {
                    return Ok(None);
                };
                if resolved.metadata()?.len() > MAX_JSON {
                    return Err(too_large());
                }
                parse_backup_json(&std::fs::read(resolved)?).map(Some)
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn python_conversions_match_reference() {
        assert_eq!(py_float(&json!("1_0.5")).unwrap(), 10.5);
        assert_eq!(py_float(&json!(" 2 ")).unwrap(), 2.0);
        assert!(py_float(&json!("x")).is_err());
        assert_eq!(py_int(&json!(3.9)).unwrap(), 3);
        assert_eq!(py_int(&json!(true)).unwrap(), 1);
        assert_eq!(source_key(&json!("TruyenQQ plugin")), "qq");
        assert_eq!(groups(Some(&json!([1, "-1", "a"]))), json!(["1", "a"]));
        assert_eq!(groups(Some(&json!(-1))), json!([]));
        assert_eq!(windows_name("C:fakepath/x/Truyện.TXT"), "Truyện.TXT");
        assert_eq!(windows_name("a:b.txt"), "b.txt");
        assert!(book_entry("backup/books/1/book.json") && !book_entry("books/book.json"));
    }
}
