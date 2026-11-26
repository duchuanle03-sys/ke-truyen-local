use crate::casefold::casefold;
use rusqlite::{Connection, OpenFlags, functions::FunctionFlags, params_from_iter};
use serde::Serialize;
use serde_json::Value;
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
    sync::{Arc, Mutex, OnceLock},
};
/// Idle connections kept for reuse. Each fresh connection reopens the just-committed WAL,
/// which Windows real-time antivirus rescans (~18 ms per request on a typical machine).
const POOL_SIZE: usize = 8;
type ConnectionPool = Arc<Mutex<Vec<(bool, Connection)>>>;

/// A library connection that returns to the pool when dropped, if it is clean.
pub(crate) struct Pooled {
    db: Option<Connection>,
    opened_writable: bool,
    pool: ConnectionPool,
}
impl std::ops::Deref for Pooled {
    type Target = Connection;
    fn deref(&self) -> &Connection {
        self.db.as_ref().expect("live pooled connection")
    }
}
impl std::ops::DerefMut for Pooled {
    fn deref_mut(&mut self) -> &mut Connection {
        self.db.as_mut().expect("live pooled connection")
    }
}
impl Drop for Pooled {
    fn drop(&mut self) {
        let Some(db) = self.db.take() else { return };
        // A connection left inside a transaction, or whose state cannot be reset, is closed instead.
        if !db.is_autocommit() || db.busy_timeout(std::time::Duration::from_secs(5)).is_err() {
            return;
        }
        if db
            .authorizer(
                None::<fn(rusqlite::hooks::AuthContext<'_>) -> rusqlite::hooks::Authorization>,
            )
            .is_err()
            || db.progress_handler(0, None::<fn() -> bool>).is_err()
        {
            return;
        }
        if let Ok(mut pool) = self.pool.lock()
            && pool.len() < POOL_SIZE
        {
            pool.push((self.opened_writable, db));
        }
    }
}

#[cfg(test)]
static SQL_FOLDS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
#[cfg(test)]
#[path = "../tests/common.rs"]
pub(crate) mod test_common;

#[derive(Debug)]
pub struct ApiError {
    pub status: u16,
    pub message: String,
    details: Option<Box<ErrorDetails>>,
}

#[derive(Debug, Default)]
struct ErrorDetails {
    code: Option<&'static str>,
    fields: serde_json::Map<String, Value>,
    verification: Option<(String, Arc<OnceLock<bool>>)>,
}
impl ApiError {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
            details: None,
        }
    }
    pub(crate) fn with_code(mut self, code: &'static str) -> Self {
        self.details.get_or_insert_with(Box::default).code = Some(code);
        self
    }
    pub fn code(&self) -> Option<&'static str> {
        self.details.as_ref()?.code
    }
    pub(crate) fn with_verification(mut self, verification: (String, Arc<OnceLock<bool>>)) -> Self {
        self.details.get_or_insert_with(Box::default).verification = Some(verification);
        self
    }
    pub(crate) fn verification(&self) -> Option<&(String, Arc<OnceLock<bool>>)> {
        self.details.as_ref()?.verification.as_ref()
    }
    pub(crate) fn with_details(mut self, fields: Value) -> Self {
        if let Value::Object(mut fields) = fields {
            fields.shift_remove("error");
            fields.shift_remove("code");
            self.details.get_or_insert_with(Box::default).fields = fields;
        }
        self
    }
    pub(crate) fn json(&self) -> Value {
        let mut value = serde_json::json!({"error":self.message});
        if let Some(code) = self.code() {
            value["code"] = code.into();
        }
        if let Some(details) = &self.details {
            value
                .as_object_mut()
                .expect("error object")
                .extend(details.fields.iter().map(|(k, v)| (k.clone(), v.clone())));
        }
        value
    }
}
impl std::fmt::Display for ApiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for ApiError {}
impl From<rusqlite::Error> for ApiError {
    fn from(_: rusqlite::Error) -> Self {
        Self::new(500, "Có lỗi khi xử lý. Dữ liệu gốc chưa bị thay đổi.")
    }
}
impl From<std::io::Error> for ApiError {
    fn from(e: std::io::Error) -> Self {
        Self::new(400, e.to_string())
    }
}

#[derive(Debug, Clone)]
pub struct ShelfQuery {
    pub query: String,
    pub source: String,
    pub group: String,
    pub sort: String,
    pub archived: bool,
    pub trashed: bool,
    pub kind: String,
    pub reverse: bool,
    pub offset: i64,
    pub limit: i64,
    /// History: off-shelf books read at or after this Unix time.
    pub read_since: Option<f64>,
}
pub fn decode(input: &str, plus: bool) -> String {
    let bytes = input.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && let (Some(a), Some(b)) = (
                (bytes[i + 1] as char).to_digit(16),
                (bytes[i + 2] as char).to_digit(16),
            )
        {
            out.push((a * 16 + b) as u8);
            i += 3;
            continue;
        }
        out.push(if plus && bytes[i] == b'+' {
            b' '
        } else {
            bytes[i]
        });
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}
fn python_space(c: char) -> bool {
    crate::casefold::python_whitespace(c)
}
pub(crate) fn decimal(c: char) -> Option<u32> {
    // Unicode decimal blocks, including Unicode 16 additions, generated with the fold table.
    const STARTS: &[u32] = &[
        0x30, 0x660, 0x6f0, 0x7c0, 0x966, 0x9e6, 0xa66, 0xae6, 0xb66, 0xbe6, 0xc66, 0xce6, 0xd66,
        0xde6, 0xe50, 0xed0, 0xf20, 0x1040, 0x1090, 0x17e0, 0x1810, 0x1946, 0x19d0, 0x1a80, 0x1a90,
        0x1b50, 0x1bb0, 0x1c40, 0x1c50, 0xa620, 0xa8d0, 0xa900, 0xa9d0, 0xa9f0, 0xaa50, 0xabf0,
        0xff10, 0x104a0, 0x10d30, 0x10d40, 0x11066, 0x110f0, 0x11136, 0x111d0, 0x112f0, 0x11450,
        0x114d0, 0x11650, 0x116c0, 0x116d0, 0x116da, 0x11730, 0x118e0, 0x11950, 0x11bf0, 0x11c50,
        0x11d50, 0x11da0, 0x11f50, 0x16130, 0x16a60, 0x16ac0, 0x16b50, 0x16d70, 0x1ccf0, 0x1d7ce,
        0x1d7d8, 0x1d7e2, 0x1d7ec, 0x1d7f6, 0x1e140, 0x1e2f0, 0x1e4f0, 0x1e5f1, 0x1e950, 0x1fbf0,
    ];
    STARTS
        .iter()
        .find_map(|s| (c as u32).checked_sub(*s).filter(|n| *n < 10))
}
fn py_repr(s: &str) -> String {
    py_repr_full(s).chars().take(200).collect()
}
pub(crate) fn py_repr_full(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = quote.to_string();
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c)
            }
            c if !crate::casefold::python_printable(c) => out.push_str(&if (c as u32) < 256 {
                format!("\\x{:02x}", c as u32)
            } else if (c as u32) < 65536 {
                format!("\\u{:04x}", c as u32)
            } else {
                format!("\\U{:08x}", c as u32)
            }),
            _ => out.push(c),
        }
    }
    out.push(quote);
    out
}
pub(crate) fn integer(s: &str) -> Result<i64, ApiError> {
    parse_integer(s).map(|(value, _)| value)
}
pub(crate) fn sqlite_integer(s: &str) -> Result<i64, ApiError> {
    let (value, overflow) = parse_integer(s)?;
    if overflow {
        return Err(rusqlite::Error::InvalidQuery.into());
    }
    Ok(value)
}
pub(crate) fn parse_integer(s: &str) -> Result<(i64, bool), ApiError> {
    let raw = s.trim_matches(crate::casefold::integer_whitespace);
    let (negative, digits) = if let Some(v) = raw.strip_prefix('-') {
        (true, v)
    } else {
        (false, raw.strip_prefix('+').unwrap_or(raw))
    };
    let mut value = 0i64;
    let mut overflow = false;
    let mut count = 0;
    let mut previous = false;
    let mut valid = !digits.is_empty();
    for c in digits.chars() {
        if let Some(n) = decimal(c) {
            let next = value.checked_mul(10).and_then(|value| {
                if negative {
                    value.checked_sub(n.into())
                } else {
                    value.checked_add(n.into())
                }
            });
            overflow |= next.is_none();
            value = next.unwrap_or(if negative { i64::MIN } else { i64::MAX });
            count += 1;
            previous = true;
        } else if c == '_' && previous {
            previous = false;
        } else {
            valid = false;
            break;
        }
    }
    if !valid || !previous {
        return Err(ApiError::new(
            400,
            format!("invalid literal for int() with base 10: {}", py_repr(s)),
        ));
    }
    if count > 4300 {
        return Err(ApiError::new(
            400,
            format!(
                "Exceeds the limit (4300 digits) for integer string conversion: value has {count} digits; use sys.set_int_max_str_digits() to increase the limit"
            ),
        ));
    }
    Ok((value, overflow))
}
impl ShelfQuery {
    pub fn from_uri(uri: &str) -> Result<Self, ApiError> {
        let mut q = HashMap::new();
        for pair in uri
            .split_once('?')
            .map_or("", |p| p.1)
            .split('#')
            .next()
            .unwrap_or("")
            .split('&')
        {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            let v = decode(v, true);
            if !v.is_empty() {
                q.entry(decode(k, true)).or_insert(v);
            }
        }
        let get = |k: &str, d: &str| q.get(k).cloned().unwrap_or_else(|| d.to_owned());
        let offset = integer(&get("offset", "0"))?.max(0);
        let limit = integer(&get("limit", "60"))?.clamp(1, 200);
        let read_since = match get("read_since", "").as_str() {
            "" => None,
            value => Some(
                value
                    .parse::<f64>()
                    .ok()
                    .filter(|t| t.is_finite() && *t >= 0.0)
                    .ok_or_else(|| ApiError::new(400, "Mốc thời gian lịch sử không hợp lệ."))?,
            ),
        };
        let kind = get("kind", "all");
        if !["all", "text", "comic", "file"].contains(&kind.as_str()) {
            return Err(ApiError::new(400, "Loại truyện không hợp lệ."));
        }
        Ok(Self {
            query: get("q", ""),
            source: get("source", ""),
            group: get("group", ""),
            sort: get("sort", "recent"),
            archived: get("archived", "") == "1",
            trashed: get("trashed", "") == "1",
            kind,
            reverse: get("reverse", "") == "1",
            offset,
            limit,
            read_since,
        })
    }
}
#[derive(Debug, Serialize)]
pub struct ShelfPage {
    pub items: Vec<Value>,
    pub total: i64,
    pub shelf_total: i64,
    pub limit: i64,
    pub offset: i64,
    pub next_offset: Option<i64>,
}
pub struct Library {
    path: PathBuf,
    writable: bool,
    pub(crate) sources: crate::sources::Sources,
    // Keep WAL coordination live between requests. Close SQLite before releasing the library lock.
    _wal_connection: Option<std::sync::Mutex<Connection>>,
    pool: ConnectionPool,
    qt: Mutex<Option<Arc<crate::qt::QtDictionary>>>,
    _lock: Option<Arc<std::fs::File>>,
    pub(crate) chapter_leases: crate::lifecycle::Leases<(String, i64)>,
    pub(crate) prefetch: crate::prefetch::Prefetch,
    pub(crate) asset_leases: crate::lifecycle::Leases<PathBuf>,
    pub(crate) database_gate: std::sync::RwLock<()>,
    pub(crate) critical_operations: crate::lifecycle::CriticalOperations,
}
pub fn checked_path(path: &Path) -> Result<PathBuf, ApiError> {
    let absolute = std::path::absolute(path)?;
    let mut part = PathBuf::new();
    for component in absolute.components() {
        part.push(component);
        if matches!(component, std::path::Component::Prefix(_)) {
            continue;
        }
        let meta = std::fs::symlink_metadata(&part)?;
        #[cfg(windows)]
        {
            use std::os::windows::fs::MetadataExt;
            if meta.file_attributes() & 0x400 != 0 {
                return Err(ApiError::new(400, "Reparse paths are not supported"));
            }
        }
        if meta.file_type().is_symlink() {
            return Err(ApiError::new(400, "Symbolic paths are not supported"));
        }
    }
    Ok(absolute.canonicalize()?)
}

pub(crate) fn checked_qt_slot(root: &Path, name: &str) -> Result<(), ApiError> {
    let path = root.join(name);
    let invalid = || ApiError::new(400, "Đường dẫn từ điển QT không hợp lệ.");
    match std::fs::symlink_metadata(&path) {
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error.into()),
        Ok(metadata) if !metadata.is_file() => return Err(invalid()),
        Ok(_) => {}
    }
    if checked_path(&path)? != path {
        return Err(invalid());
    }
    #[cfg(windows)]
    let links = crate::windows_metadata::link_count(&std::fs::File::open(path)?)?;
    #[cfg(unix)]
    let links = std::os::unix::fs::MetadataExt::nlink(&std::fs::metadata(path)?);
    if links != 1 {
        return Err(invalid());
    }
    Ok(())
}

impl Library {
    pub fn open_fixture(path: &Path) -> Result<Self, ApiError> {
        Self::open_fixture_mode(path, false)
    }
    pub fn open_writable_fixture(path: &Path) -> Result<Self, ApiError> {
        Self::open_fixture_mode(path, true)
    }
    fn open_fixture_mode(path: &Path, writable: bool) -> Result<Self, ApiError> {
        let path = checked_path(path)?;
        let root = if path.is_dir() {
            path
        } else {
            if path.file_name().and_then(|n| n.to_str()) != Some("library.sqlite3") {
                return Err(ApiError::new(400, "Expected library.sqlite3"));
            }
            path.parent()
                .ok_or_else(|| ApiError::new(400, "Missing fixture directory"))?
                .to_owned()
        };
        let marker = checked_path(&root.join("m1-fixture.json"))?;
        let marker = std::fs::read(marker)?;
        let marker = std::str::from_utf8(&marker)
            .ok()
            .and_then(|text| parse_json(text).ok())
            .ok_or_else(|| ApiError::new(400, "Invalid fixture marker"))?;
        if marker["format"] != "reader-local-rust-m1"
            || marker["version"] != 1
            || marker["database"] != "library.sqlite3"
            || marker["synthetic"] != true
            || !marker["sealed"].is_boolean()
            || marker["unicode"] != "16.0.0"
        {
            return Err(ApiError::new(400, "Unsupported fixture marker"));
        }
        if writable && marker["sealed"] != false {
            return Err(ApiError::new(
                400,
                "Writable mode requires an unsealed synthetic fixture",
            ));
        }
        Self::open_root(root, writable)
    }
    /// Open a real data directory (no fixture marker) for serving.
    pub fn open_production(root: &Path) -> Result<Self, ApiError> {
        std::fs::create_dir_all(root)?;
        let root = checked_path(root)?;
        if !root.is_dir() {
            return Err(ApiError::new(400, "Thư mục dữ liệu không hợp lệ."));
        }
        let database = root.join("library.sqlite3");
        {
            let _owner = crate::lifecycle::library_lock(&root)?;
            // First run, or a first run that was interrupted before its catalog committed.
            let empty = !database.exists() || {
                let db = Connection::open(&database)?;
                db.query_row("SELECT count(*) FROM sqlite_master", [], |r| {
                    r.get::<_, i64>(0)
                })? == 0
            };
            if empty {
                let mut db = Connection::open(&database)?;
                register_shelf_fold(&db)?;
                db.pragma_update(None, "journal_mode", "WAL")?;
                crate::schema::create(&mut db)?;
            }
        }
        Self::open_root(root, true)
    }
    fn open_root(root: PathBuf, writable: bool) -> Result<Self, ApiError> {
        let path = checked_path(&root.join("library.sqlite3"))?;
        if path.parent() != Some(root.as_path()) {
            return Err(ApiError::new(400, "Fixture database escapes root"));
        }
        let lock = if writable {
            Some(Arc::new(crate::lifecycle::library_lock(&root)?))
        } else {
            None
        };
        let mut library = Self {
            path,
            writable: false,
            sources: if writable {
                crate::sources::Sources::open(&root)?
            } else {
                Default::default()
            },
            _wal_connection: None,
            pool: Default::default(),
            qt: Mutex::new(None),
            _lock: lock,
            chapter_leases: Default::default(),
            prefetch: Default::default(),
            asset_leases: Default::default(),
            database_gate: Default::default(),
            critical_operations: Default::default(),
        };
        if writable {
            crate::schema::upgrade(&mut library.base_connection(true)?)?;
        }
        let db = library.connection()?;
        db.prepare("SELECT id,name,author,source,path,cover,kind,format,description,progress,chapter_index,position,groups_json,added_at,updated_at,read_at,chapter_count,raw,archived,trashed_at FROM books LIMIT 0")?;
        db.prepare("SELECT book_id,group_id FROM book_groups LIMIT 0")?;
        db.prepare("SELECT id FROM books INDEXED BY books_shelf_search LIMIT 0")?;
        for index in [
            "books_shelf_recent",
            "books_shelf_added",
            "books_shelf_updated",
            "books_shelf_progress",
            "books_shelf_chapters",
            "books_shelf_name",
            "books_shelf_source",
            "books_shelf_kind",
        ] {
            db.prepare(&format!("SELECT id FROM books INDEXED BY {index} LIMIT 0"))?;
        }
        db.prepare("SELECT book_id FROM book_groups INDEXED BY book_group_lookup LIMIT 0")?;
        for (name, expression) in [
            ("recent", "read_at"),
            ("added", "added_at"),
            ("updated", "updated_at"),
            ("progress", "progress"),
            ("chapters", "chapter_count"),
            ("name", "shelf_fold(name)"),
        ] {
            let direction = if name == "name" { "ASC" } else { "DESC" };
            check_index(
                &db,
                &format!("books_shelf_{name}"),
                &format!(
                    "CREATE INDEX books_shelf_{name} ON books((trashed_at>0),archived,{expression} {direction},name {direction},id {direction})"
                ),
            )?;
        }
        for column in ["source", "kind"] {
            check_index(
                &db,
                &format!("books_shelf_{column}"),
                &format!(
                    "CREATE INDEX books_shelf_{column} ON books((trashed_at>0),archived,{column},read_at DESC,name DESC,id DESC)"
                ),
            )?;
        }
        check_index(
            &db,
            "books_shelf_search",
            "CREATE INDEX books_shelf_search ON books((trashed_at>0),archived,shelf_fold(name || ' ' || author),id)",
        )?;
        check_index(
            &db,
            "book_group_lookup",
            "CREATE INDEX book_group_lookup ON book_groups(group_id,book_id)",
        )?;
        drop(db);
        if writable {
            library.writable = true;
            // Read-only handles opened during validation must not serve the writable library.
            library
                .pool
                .lock()
                .map_err(|_| rusqlite::Error::InvalidQuery)?
                .clear();
            let db = library.base_connection(true)?;
            db.pragma_update(None, "journal_mode", "WAL")?;
            crate::chapters::recover_trashed_jobs(&db)?;
            db.query_row("SELECT 1 FROM sqlite_schema LIMIT 1", [], |_| Ok(()))?;
            library._wal_connection = Some(std::sync::Mutex::new(db));
        }
        Ok(library)
    }
    pub fn root(&self) -> &Path {
        self.path.parent().expect("validated database parent")
    }
    pub fn writable(&self) -> bool {
        self.writable
    }
    pub(crate) fn writable_lock(&self) -> Result<Arc<std::fs::File>, ApiError> {
        self._lock
            .clone()
            .ok_or_else(|| ApiError::new(400, "QT requires a writable synthetic fixture"))
    }
    pub fn qt(&self) -> Result<Arc<crate::qt::QtDictionary>, ApiError> {
        let mut qt = self.qt.lock().map_err(|_| rusqlite::Error::InvalidQuery)?;
        if let Some(qt) = qt.as_ref() {
            return Ok(qt.clone());
        }
        let dictionary = Arc::new(crate::qt::QtDictionary::open(self)?);
        *qt = Some(dictionary.clone());
        Ok(dictionary)
    }
    fn base_connection(&self, writable: bool) -> Result<Connection, ApiError> {
        let db = Connection::open_with_flags(
            &self.path,
            (if writable {
                OpenFlags::SQLITE_OPEN_READ_WRITE
            } else {
                OpenFlags::SQLITE_OPEN_READ_ONLY
            }) | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        register_shelf_fold(&db)?;
        db.pragma_update(None, "foreign_keys", true)?;
        db.pragma_update(None, "query_only", !writable)?;
        if writable {
            // Bound the WAL file whenever SQLite restarts it after a checkpoint.
            db.pragma_update(None, "journal_size_limit", 64 * 1024 * 1024)?;
        }
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        Ok(db)
    }
    fn pooled(&self, write: bool) -> Result<Pooled, ApiError> {
        // Reads use read-write handles with query_only in a writable library, read-only handles otherwise.
        let opened_writable = self.writable;
        let reused = self.pool.lock().ok().and_then(|mut pool| {
            let at = pool
                .iter()
                .rposition(|(writable, _)| *writable == opened_writable)?;
            Some(pool.swap_remove(at).1)
        });
        let db = match reused {
            Some(db) => db,
            None => self.base_connection(opened_writable)?,
        };
        db.pragma_update(None, "query_only", !write)?;
        Ok(Pooled {
            db: Some(db),
            opened_writable,
            pool: self.pool.clone(),
        })
    }
    pub(crate) fn reader_connection(&self) -> Result<Pooled, ApiError> {
        self.pooled(false)
    }
    pub(crate) fn write_connection(&self) -> Result<Pooled, ApiError> {
        if !self.writable {
            return Err(ApiError::new(403, "Writable mode is required"));
        }
        self.pooled(true)
    }
    fn connection(&self) -> Result<Pooled, ApiError> {
        let db = self.reader_connection()?;
        install_authorizer(&db)?;
        Ok(db)
    }
    pub fn list_books(&self, q: &ShelfQuery) -> Result<ShelfPage, ApiError> {
        let mut db = self.connection()?;
        let tx = db.transaction()?;
        let trashed = q.trashed && q.read_since.is_none();
        let mut predicate = String::from("(trashed_at>0)=?");
        let mut values: Vec<rusqlite::types::Value> = vec![(trashed as i64).into()];
        if !trashed {
            predicate.push_str(" AND archived=?");
            values.push((q.archived as i64).into());
        }
        if let Some(since) = q.read_since {
            predicate.push_str(" AND read_at>0 AND read_at>=?");
            values.push(since.into());
        }
        let term = casefold(&q.query).trim_matches(python_space).to_owned();
        if !term.is_empty() {
            predicate.push_str(&format!(" AND id IN (SELECT id FROM books INDEXED BY books_shelf_search WHERE {predicate} AND instr(shelf_fold(name || ' ' || author),?)>0)"));
            values.extend(values.clone());
            values.push(term.into());
        }
        for (column, value) in [
            ("source", q.source.as_str()),
            (
                "kind",
                if ["text", "comic"].contains(&q.kind.as_str()) {
                    q.kind.as_str()
                } else {
                    ""
                },
            ),
        ] {
            if !value.is_empty() {
                predicate.push_str(&format!(" AND {column}=?"));
                values.push(value.to_owned().into());
            }
        }
        if q.kind == "file" {
            predicate.push_str(" AND source IN ('local','file')");
        }
        if !q.group.is_empty() {
            predicate.push_str(" AND id IN (SELECT book_id FROM book_groups WHERE group_id=?)");
            values.push(q.group.clone().into());
        }
        let key = match q.sort.as_str() {
            "added" => "added_at",
            "updated" => "updated_at",
            "progress" => "progress",
            "chapters" => "chapter_count",
            "name" => "shelf_fold(name)",
            _ => "read_at",
        };
        let direction = if (key == "shelf_fold(name)") != q.reverse {
            "ASC"
        } else {
            "DESC"
        };
        let total: i64 = tx.query_row(
            &format!("SELECT count(*) FROM books WHERE {predicate}"),
            params_from_iter(&values),
            |r| r.get(0),
        )?;
        let shelf_total = tx.query_row(
            "SELECT count(*) FROM books WHERE (trashed_at>0)=0 AND archived=0",
            [],
            |r| r.get(0),
        )?;
        let offset = q.offset.min(((total - 1).max(0) / q.limit) * q.limit);
        values.push(q.limit.into());
        values.push(offset.into());
        let items = crate::reader::rows(&tx, &format!("SELECT * FROM books WHERE {predicate} ORDER BY {key} {direction},name {direction},id {direction} LIMIT ? OFFSET ?"), params_from_iter(&values))?.into_iter().map(crate::reader::book_value).collect::<Result<Vec<_>,_>>()?;
        Ok(ShelfPage {
            items,
            total,
            shelf_total,
            limit: q.limit,
            offset,
            next_offset: if offset + q.limit < total {
                Some(offset + q.limit)
            } else {
                None
            },
        })
    }
}
fn check_index(db: &Connection, name: &str, expected: &str) -> Result<(), ApiError> {
    let actual: String = db.query_row(
        "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
        [name],
        |r| r.get(0),
    )?;
    let normalize = |s: &str| {
        let mut quoted = false;
        let mut normalized = String::new();
        for c in s.chars() {
            if c == '\'' {
                quoted = !quoted;
                normalized.push(c)
            } else if quoted {
                normalized.push(c)
            } else if !c.is_ascii_whitespace() {
                normalized.push(c.to_ascii_lowercase());
            }
        }
        normalized
    };
    if normalize(&actual) != normalize(expected) {
        return Err(ApiError::new(
            400,
            format!("Unsupported fixture index: {name}"),
        ));
    }
    Ok(())
}
pub(crate) fn parse_json(s: &str) -> Result<Value, ApiError> {
    let raw: &serde_json::value::RawValue =
        serde_json::from_str(s).map_err(|error| json_error(s, error))?;
    parse_raw_json(raw.get())
}
fn parse_raw_json(s: &str) -> Result<Value, ApiError> {
    use serde_json::value::RawValue;
    enum Work<'a> {
        Visit(&'a str, usize),
        Array(usize),
        Object(Vec<String>),
    }
    struct ObjectVisitor;
    impl<'de> serde::de::Visitor<'de> for ObjectVisitor {
        type Value = Vec<(String, &'de RawValue)>;
        fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
            f.write_str("a JSON object")
        }
        fn visit_map<M: serde::de::MapAccess<'de>>(
            self,
            mut map: M,
        ) -> Result<Self::Value, M::Error> {
            let mut entries = Vec::new();
            while let Some(entry) = map.next_entry()? {
                entries.push(entry);
            }
            Ok(entries)
        }
    }
    let mut work = vec![Work::Visit(s, 0)];
    let mut values = Vec::new();
    while let Some(task) = work.pop() {
        match task {
            Work::Array(count) => {
                let children = values.split_off(values.len() - count);
                values.push(Value::Array(children));
            }
            Work::Object(keys) => {
                let children = values.split_off(values.len() - keys.len());
                let mut object = serde_json::Map::new();
                for (key, value) in keys.into_iter().zip(children) {
                    object.insert(key, value);
                }
                values.push(Value::Object(object));
            }
            Work::Visit(s, depth) => match s.as_bytes().first().copied() {
                Some(b'{' | b'[') if depth >= 984 => {
                    return Err(ApiError::new(400, "Dữ liệu JSON lồng quá sâu."));
                }
                Some(b'{') => {
                    let mut deserializer = serde_json::Deserializer::from_str(s);
                    let entries =
                        serde::Deserializer::deserialize_map(&mut deserializer, ObjectVisitor)
                            .map_err(|error| json_error(s, error))?;
                    let (keys, children): (Vec<_>, Vec<_>) = entries.into_iter().unzip();
                    work.push(Work::Object(keys));
                    for raw in children.into_iter().rev() {
                        work.push(Work::Visit(raw.get(), depth + 1));
                    }
                }
                Some(b'[') => {
                    let entries: Vec<&RawValue> =
                        serde_json::from_str(s).map_err(|error| json_error(s, error))?;
                    work.push(Work::Array(entries.len()));
                    for raw in entries.into_iter().rev() {
                        work.push(Work::Visit(raw.get(), depth + 1));
                    }
                }
                Some(b'"') => values.push(
                    serde_json::from_str::<String>(s)
                        .map(Value::String)
                        .map_err(|error| json_error(s, error))?,
                ),
                Some(b't' | b'f') => values.push(
                    serde_json::from_str::<bool>(s)
                        .map(Value::Bool)
                        .map_err(|error| json_error(s, error))?,
                ),
                Some(b'n') => values.push(Value::Null),
                _ => {
                    if !s.contains(['.', 'e', 'E']) {
                        let digits = s.strip_prefix('-').unwrap_or(s).len();
                        if digits > 4300 {
                            return Err(ApiError::new(
                                400,
                                format!(
                                    "Exceeds the limit (4300 digits) for integer string conversion: value has {digits} digits; use sys.set_int_max_str_digits() to increase the limit"
                                ),
                            ));
                        }
                    }
                    values.push(
                        serde_json::from_str::<serde_json::Number>(s)
                            .map(Value::Number)
                            .map_err(|error| json_error(s, error))?,
                    );
                }
            },
        }
    }
    Ok(values.pop().expect("validated JSON value"))
}
fn json_error(s: &str, error: serde_json::Error) -> ApiError {
    let text = error.to_string();
    let line_start = s
        .split_inclusive('\n')
        .take(error.line().saturating_sub(1))
        .map(str::len)
        .sum::<usize>();
    let byte = (line_start + error.column()).min(s.len());
    let mut byte = byte;
    while !s.is_char_boundary(byte) {
        byte -= 1
    }
    let mut pos = s[..byte].chars().count().saturating_sub(1);
    let chars: Vec<char> = s.chars().collect();
    if error.is_eof() {
        pos = chars.len()
    }
    let previous = chars[..pos.min(chars.len())]
        .iter()
        .rposition(|c| ![' ', '\t', '\r', '\n'].contains(c));
    let trailing = previous
        .filter(|&p| chars[p] == ',')
        .filter(|_| chars.get(pos).is_some_and(|c| *c == ']' || *c == '}'));
    let unicode_escape = (0..chars.len())
        .rev()
        .find(|&i| {
            i <= pos
                && chars[i] == '\\'
                && chars.get(i + 1) == Some(&'u')
                && chars[..i].iter().rev().take_while(|&&c| c == '\\').count() % 2 == 0
                && (i + 2..i + 6).any(|n| !chars.get(n).is_some_and(char::is_ascii_hexdigit))
        })
        .filter(|&i| pos <= i + 6);
    let message = if let Some(escape) = unicode_escape.filter(|_| {
        text.starts_with("invalid escape") || text.starts_with("EOF while parsing a string")
    }) {
        pos = escape + 1;
        "Invalid \\uXXXX escape"
    } else if let Some(comma) = trailing {
        let end = chars[pos];
        pos = comma;
        if end == ']' {
            "Illegal trailing comma before end of array"
        } else {
            "Illegal trailing comma before end of object"
        }
    } else if text.starts_with("expected `:`") {
        "Expecting ':' delimiter"
    } else if text.starts_with("expected `,`") {
        "Expecting ',' delimiter"
    } else if text.starts_with("key must be a string")
        || text.starts_with("EOF while parsing an object")
            && previous.is_some_and(|p| chars[p] == '{' || chars[p] == ',')
    {
        "Expecting property name enclosed in double quotes"
    } else if text.starts_with("EOF while parsing an object")
        || text.starts_with("EOF while parsing a list")
            && !previous.is_some_and(|p| chars[p] == '[' || chars[p] == ',')
    {
        "Expecting ',' delimiter"
    } else if text.starts_with("trailing characters") {
        "Extra data"
    } else if text.starts_with("EOF while parsing a string") {
        let mut open = None;
        let mut escaped = false;
        for (i, &c) in chars.iter().enumerate() {
            if escaped {
                escaped = false;
                continue;
            }
            if c == '\\' {
                escaped = true
            } else if c == '"' {
                open = if open.is_some() { None } else { Some(i) }
            }
        }
        pos = open.unwrap_or(pos);
        "Unterminated string starting at"
    } else if text.starts_with("invalid escape") {
        pos = pos.saturating_sub(1);
        "Invalid \\escape"
    } else if text.starts_with("control character") {
        "Invalid control character at"
    } else if text.starts_with("expected ident") || text.starts_with("EOF while parsing a value") {
        while pos > 0 && chars[pos - 1].is_ascii_alphabetic() {
            pos -= 1;
        }
        "Expecting value"
    } else {
        "Expecting value"
    };
    let before = &chars[..pos.min(chars.len())];
    let line = before.iter().filter(|&&c| c == '\n').count() + 1;
    let column = before
        .iter()
        .rposition(|&c| c == '\n')
        .map_or(pos + 1, |n| pos - n);
    ApiError::new(
        400,
        format!("{message}: line {line} column {column} (char {pos})"),
    )
}

/// Python `str.casefold`, required by the persisted shelf expression indexes.
fn register_shelf_fold(db: &Connection) -> rusqlite::Result<()> {
    db.create_scalar_function(
        "shelf_fold",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| {
            #[cfg(test)]
            SQL_FOLDS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            Ok(casefold(&ctx.get::<String>(0)?))
        },
    )
}
fn install_authorizer(db: &Connection) -> rusqlite::Result<()> {
    use rusqlite::hooks::{AuthAction, AuthContext, Authorization};
    db.authorizer(Some(|ctx: AuthContext<'_>| match ctx.action {
        AuthAction::Read {
            table_name: "books" | "book_groups" | "sqlite_master",
            ..
        }
        | AuthAction::Select
        | AuthAction::Function { .. }
        | AuthAction::Transaction { .. } => Authorization::Allow,
        _ => Authorization::Deny,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn production_opens_python_library_without_marker() {
        let fixture = test_common::Fixture::new("empty");
        let root = fixture.db.parent().unwrap().to_owned();
        std::fs::remove_file(root.join("m1-fixture.json")).unwrap();
        let library = Library::open_production(&root).unwrap();
        assert!(library.writable());
        let db = library.write_connection().unwrap();
        db.execute(
            "INSERT INTO books(id,name,added_at,updated_at) VALUES('real','Truyện',1,1)",
            [],
        )
        .unwrap();
        drop(db);
        assert_eq!(library.get_book("real").unwrap()["name"], "Truyện");
        // The cross-language owner lock keeps a second server off the same library.
        assert!(Library::open_production(&root).is_err());
    }

    fn fresh_dirs(name: &str) -> PathBuf {
        let base = std::env::temp_dir().join(format!("reader-m7-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        base
    }

    #[test]
    fn interrupted_first_run_leaves_a_recoverable_empty_database() {
        let base = fresh_dirs("empty-db");
        let root = base.join("user-data");
        std::fs::create_dir_all(&root).unwrap();
        // An interrupted first run can leave a database file with no catalog.
        drop(Connection::open(root.join("library.sqlite3")).unwrap());
        let library = Library::open_production(&root).expect("empty database must be initialised");
        assert_eq!(
            library
                .list_books(&ShelfQuery::from_uri("/api/books").unwrap())
                .unwrap()
                .total,
            0
        );
        drop(library);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn production_refuses_unknown_schema_without_writing() {
        let fixture = test_common::Fixture::new("empty");
        let root = fixture.db.parent().unwrap().to_owned();
        std::fs::remove_file(root.join("m1-fixture.json")).unwrap();
        Connection::open(&fixture.db)
            .unwrap()
            .execute_batch("CREATE TABLE unknown_shape(v TEXT)")
            .unwrap();
        let before = std::fs::read(&fixture.db).unwrap();
        let error = Library::open_production(&root)
            .err()
            .expect("unknown schema must be refused");
        assert_eq!(error.status, 400);
        assert_eq!(std::fs::read(&fixture.db).unwrap(), before);
    }
    #[test]
    fn pooled_connections_are_reset_before_reuse() {
        let (_fixture, library) = crate::reader::tests::writable();
        let pooled = |library: &Library| library.pool.lock().unwrap().len();
        drop(library.write_connection().unwrap());
        let idle = pooled(&library);
        assert!(idle >= 1);
        // A reused handle still refuses writes when checked out for reading.
        let reader = library.reader_connection().unwrap();
        assert!(
            reader
                .execute("UPDATE books SET name='x' WHERE id='book'", [])
                .is_err()
        );
        drop(reader);
        // The shelf authorizer is removed before the handle is reused for writes.
        drop(library.connection().unwrap());
        let writer = library.write_connection().unwrap();
        writer
            .execute("UPDATE books SET name='Tên mới' WHERE id='book'", [])
            .unwrap();
        drop(writer);
        // A handle dropped inside a transaction is closed, never pooled.
        let before = pooled(&library);
        let open = library.write_connection().unwrap();
        open.execute_batch("BEGIN IMMEDIATE").unwrap();
        drop(open);
        assert_eq!(pooled(&library), before - 1);
        // Read-only fixtures never receive writable handles.
        let read_only = Library::open_fixture(&library.path).unwrap();
        assert!(read_only.write_connection().is_err());
        assert!(
            read_only
                .reader_connection()
                .unwrap()
                .execute("DELETE FROM notes", [])
                .is_err()
        );
    }
    #[test]
    fn m2_json_boundary_preserves_literal_keys_and_integer_limit() {
        for value in [
            serde_json::json!({"nested":{"$serde_json::private::Number":"123"}}),
            serde_json::json!({"nested":{"$serde_json::private::Number":"123","other":true}}),
            serde_json::json!({"nested":[{"$serde_json::private::RawValue":"[]"}]}),
        ] {
            assert_eq!(
                parse_json(&String::from_utf8(crate::http::json_bytes(&value)).unwrap()).unwrap(),
                value
            );
        }
        let error = parse_json(&format!("{{\"x\":{}}}", "1".repeat(4301))).unwrap_err();
        assert_eq!(error.status, 400);
        assert_eq!(
            error.message,
            "Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use sys.set_int_max_str_digits() to increase the limit"
        );
        assert!(parse_json(&"1".repeat(4300)).is_ok());
        assert!(parse_json(&format!("\"{}\"", "1".repeat(4301))).is_ok());
        assert_eq!(
            String::from_utf8(crate::http::json_bytes(
                &parse_json("{\"a\":1,\"b\":2,\"a\":3}").unwrap()
            ))
            .unwrap(),
            "{\"a\": 3, \"b\": 2}"
        );
    }
    #[test]
    fn m2_json_depth_preserves_reference_valid_settings() {
        for depth in [127, 128, 256, 512, 983] {
            let text = format!("{{\"depth\":{}0{}}}", "[".repeat(depth), "]".repeat(depth));
            let value = parse_json(&text).unwrap();
            assert_eq!(
                crate::http::json_bytes(&value),
                text.replace(':', ": ").as_bytes()
            );
        }
        let over = format!("{{\"depth\":{}0{}}}", "[".repeat(984), "]".repeat(984));
        let error = parse_json(&over).unwrap_err();
        assert_eq!(error.status, 400);
        assert_eq!(error.message, "Dữ liệu JSON lồng quá sâu.");
    }
    #[test]
    fn parser() {
        assert_eq!(
            ShelfQuery::from_uri("/api/books?limit=&limit=1_0&offset=999999999999999999999")
                .unwrap()
                .limit,
            10
        );
        assert_eq!(ShelfQuery::from_uri("/?limit=١٠").unwrap().limit, 10);
        assert!(ShelfQuery::from_uri("/?limit=²").is_err());
        assert_eq!(crate::casefold::casefold("Straße Σς ﬃ"), "strasse σσ ffi");
    }
}
#[cfg(test)]
mod path_tests {
    #[test]
    fn qt_rejects_existing_database_hardlink_before_opening_it() {
        let (fixture, library) = crate::reader::tests::writable();
        let outside = fixture.root.join("separate-test-data.sqlite3");
        std::fs::write(&outside, b"synthetic sentinel").unwrap();
        std::fs::hard_link(&outside, library.root().join("qt.sqlite3")).unwrap();
        assert_eq!(library.qt().err().unwrap().status, 400);
        assert_eq!(std::fs::read(outside).unwrap(), b"synthetic sentinel");
        assert!(!library.root().join("qt.sqlite3-wal").exists());
    }

    #[test]
    fn m3_writable_owner_keeps_wal_open_between_requests() {
        let (_fixture, library) = crate::reader::tests::writable();
        let wal = library.root().join("library.sqlite3-wal");
        let db = library.write_connection().unwrap();
        db.execute(
            "UPDATE chapter_cache_state SET max_chapters=39 WHERE id=1",
            [],
        )
        .unwrap();
        drop(db);
        assert!(
            wal.metadata().is_ok_and(|m| m.len() > 0),
            "WAL disappeared between requests"
        );
        assert_eq!(library.chapter_cache_stats().unwrap()["max_chapters"], 39);
        let root = library.root().to_path_buf();
        drop(library);
        assert!(
            !wal.exists(),
            "idle connection must close with the writable owner"
        );
        assert!(crate::lifecycle::library_lock(&root).is_ok());
    }
    #[test]
    fn rooted_workspace_is_valid() {
        assert!(
            super::checked_path(
                &std::path::Path::new(env!("CARGO_MANIFEST_DIR"))
                    .canonicalize()
                    .unwrap()
            )
            .is_ok()
        );
    }
    #[test]
    fn authorizer_forbids_chapters_and_mutation() {
        let db = rusqlite::Connection::open_in_memory().unwrap();
        db.execute_batch(
            "CREATE TABLE books(id);CREATE TABLE chapters(text);CREATE TABLE book_groups(book_id);",
        )
        .unwrap();
        super::install_authorizer(&db).unwrap();
        assert!(db.prepare("SELECT id FROM books").is_ok());
        assert!(db.prepare("SELECT text FROM chapters").is_err());
        assert!(db.execute("UPDATE books SET id=1", []).is_err());
    }
    #[test]
    fn shelf_normal_page_does_not_fold_rows() {
        let fixture = super::test_common::Fixture::new("scale");
        let library = super::Library::open_fixture(&fixture.db).unwrap();
        super::SQL_FOLDS.store(0, std::sync::atomic::Ordering::Relaxed);
        let page = library
            .list_books(&super::ShelfQuery::from_uri("/api/books").unwrap())
            .unwrap();
        assert_eq!((page.total, page.items.len()), (50_000, 60));
        assert_eq!(
            super::SQL_FOLDS.load(std::sync::atomic::Ordering::Relaxed),
            0
        );
    }
}
