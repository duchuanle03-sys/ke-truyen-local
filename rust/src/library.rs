use crate::casefold::casefold;
use rusqlite::{
    Connection, OpenFlags, functions::FunctionFlags, params_from_iter, types::ValueRef,
};
use serde::Serialize;
use serde_json::{Map, Value};
use std::{
    collections::HashMap,
    path::{Path, PathBuf},
};
#[cfg(test)]
static SQL_FOLDS: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(0);
#[cfg(test)]
#[path = "../tests/common.rs"]
mod test_common;

#[derive(Debug)]
pub struct ApiError {
    pub status: u16,
    pub message: String,
}
impl ApiError {
    pub fn new(status: u16, message: impl Into<String>) -> Self {
        Self {
            status,
            message: message.into(),
        }
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
fn decimal(c: char) -> Option<u32> {
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
    out.chars().take(200).collect()
}
fn integer(s: &str) -> Result<i64, ApiError> {
    let raw = s.trim_matches(crate::casefold::integer_whitespace);
    let (negative, digits) = if let Some(v) = raw.strip_prefix('-') {
        (true, v)
    } else {
        (false, raw.strip_prefix('+').unwrap_or(raw))
    };
    let mut value = 0i64;
    let mut count = 0;
    let mut previous = false;
    let mut valid = !digits.is_empty();
    for c in digits.chars() {
        if let Some(n) = decimal(c) {
            value = value.saturating_mul(10).saturating_add(n.into());
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
    Ok(if negative { -value } else { value })
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
impl Library {
    pub fn open_fixture(path: &Path) -> Result<Self, ApiError> {
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
        let marker: Value = serde_json::from_slice(&std::fs::read(marker)?)
            .map_err(|_| ApiError::new(400, "Invalid fixture marker"))?;
        if marker["format"] != "reader-local-rust-m1"
            || marker["version"] != 1
            || marker["database"] != "library.sqlite3"
            || marker["synthetic"] != true
            || !marker["sealed"].is_boolean()
            || marker["unicode"] != "16.0.0"
        {
            return Err(ApiError::new(400, "Unsupported fixture marker"));
        }
        let path = checked_path(&root.join("library.sqlite3"))?;
        if path.parent() != Some(root.as_path()) {
            return Err(ApiError::new(400, "Fixture database escapes root"));
        }
        let library = Self { path };
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
        Ok(library)
    }
    fn connection(&self) -> Result<Connection, ApiError> {
        let db = Connection::open_with_flags(
            &self.path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        db.create_scalar_function(
            "shelf_fold",
            1,
            FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| {
                #[cfg(test)]
                SQL_FOLDS.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                Ok(casefold(&ctx.get::<String>(0)?))
            },
        )?;
        db.pragma_update(None, "query_only", true)?;
        db.busy_timeout(std::time::Duration::from_secs(5))?;
        install_authorizer(&db)?;
        Ok(db)
    }
    pub fn list_books(&self, q: &ShelfQuery) -> Result<ShelfPage, ApiError> {
        let mut db = self.connection()?;
        let tx = db.transaction()?;
        let mut predicate = String::from("(trashed_at>0)=?");
        let mut values: Vec<rusqlite::types::Value> = vec![(q.trashed as i64).into()];
        if !q.trashed {
            predicate.push_str(" AND archived=?");
            values.push((q.archived as i64).into());
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
        let mut stmt=tx.prepare(&format!("SELECT * FROM books WHERE {predicate} ORDER BY {key} {direction},name {direction},id {direction} LIMIT ? OFFSET ?"))?;
        let names: Vec<String> = stmt.column_names().iter().map(|s| s.to_string()).collect();
        let mut rows = stmt.query(params_from_iter(&values))?;
        let mut items = Vec::new();
        while let Some(row) = rows.next()? {
            let mut item = Map::new();
            for (i, name) in names.iter().enumerate() {
                let value = match row.get_ref(i)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(v) => v.into(),
                    ValueRef::Real(v) => {
                        serde_json::Number::from_f64(v).map_or(Value::Null, Value::Number)
                    }
                    ValueRef::Text(v) => String::from_utf8_lossy(v).to_string().into(),
                    ValueRef::Blob(_) => return Err(ApiError::new(500, "Invalid book value")),
                };
                item.insert(name.clone(), value);
            }
            let groups = item.remove("groups_json").unwrap_or(Value::Null);
            item.insert("groups".into(), parse_json(groups.as_str().unwrap_or(""))?);
            let archived = item.get("archived").and_then(Value::as_i64).unwrap_or(0) != 0;
            item.insert("archived".into(), archived.into());
            let trash = item
                .get("trashed_at")
                .and_then(Value::as_f64)
                .unwrap_or(0.0);
            item.insert(
                "trash_expires_at".into(),
                if trash == 0.0 {
                    Value::Null
                } else {
                    (trash + 2592000.0).into()
                },
            );
            let raw = item.remove("raw").unwrap_or_else(|| "{}".into());
            let metadata = parse_json(raw.as_str().unwrap_or(""))?;
            if metadata.is_null() {
                return Err(ApiError::new(
                    500,
                    "Có lỗi khi xử lý. Dữ liệu gốc chưa bị thay đổi.",
                ));
            }
            if metadata.is_number()
                || metadata.is_boolean()
                || metadata
                    .as_array()
                    .is_some_and(|values| values.iter().any(|v| v == "warnings" || v == "ocr"))
                || metadata
                    .as_str()
                    .is_some_and(|s| s.contains("warnings") || s.contains("ocr"))
            {
                return Err(rusqlite::Error::InvalidQuery.into());
            }
            for key in ["warnings", "ocr"] {
                if let Some(value) = metadata.get(key) {
                    item.insert(key.into(), value.clone());
                }
            }
            items.push(Value::Object(item));
        }
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
fn parse_json(s: &str) -> Result<Value, ApiError> {
    serde_json::from_str(s).map_err(|error| {
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
        } else if text.starts_with("expected ident")
            || text.starts_with("EOF while parsing a value")
        {
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
    })
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
