use crate::{
    ApiError, Library,
    casefold::{casefold, python_whitespace},
    http::json_bytes,
    library::{checked_qt_slot, parse_json},
    reader::rows,
};
use regex::{Captures, Regex};
use rusqlite::{
    Connection, OpenFlags, OptionalExtension, Transaction, TransactionBehavior,
    functions::FunctionFlags, params,
};
use serde::Deserialize;
use serde_json::{Value, json};
use std::{
    collections::{HashMap, VecDeque},
    fs::File,
    path::{Path, PathBuf},
    sync::{Arc, LazyLock, Mutex, MutexGuard, TryLockError},
    time::{Duration, SystemTime, UNIX_EPOCH},
};

const KINDS: [&str; 4] = ["name", "vietphrase", "pronouns", "hanviet"];
const HEADER: &str = "# qt-dictionary-v1 ";
const TEXT_ERROR: &str = "Từ và nghĩa phải là văn bản.";
const UPSERT: &str = "INSERT INTO entries(kind,word,meaning,scope) VALUES(?,?,?,?)
    ON CONFLICT(kind,scope,word) DO UPDATE SET meaning=excluded.meaning";
const SELECT_ENTRIES: &str = "SELECT kind,word,meaning,scope FROM entries WHERE kind=? AND scope=?";

pub struct QtDictionary {
    path: PathBuf,
    writer: Mutex<Connection>,
    mutation: Mutex<()>,
    cache: Mutex<Cache>,
    _owner: Arc<File>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct Entry {
    pub(crate) kind: String,
    pub(crate) word: String,
    pub(crate) meaning: String,
    pub(crate) scope: String,
}

impl Entry {
    fn value(&self) -> Value {
        json!({"kind":self.kind,"word":self.word,"meaning":self.meaning,"scope":self.scope})
    }
}

fn error(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message)
}

fn internal_error() -> ApiError {
    rusqlite::Error::InvalidQuery.into()
}

pub(crate) fn kind(value: &str) -> Result<&'static str, ApiError> {
    match value {
        "name" | "names" => Ok("name"),
        "vietphrase" | "vp" => Ok("vietphrase"),
        "pronouns" | "pronoun" => Ok("pronouns"),
        "hanviet" | "han-viet" => Ok("hanviet"),
        "rules" | "rule" => Ok("rules"),
        _ => Err(error(format!("Loại từ điển không hợp lệ: {value}"))),
    }
}

pub(crate) fn scope(value: &str) -> Result<&str, ApiError> {
    if trim(value).is_empty() || value.chars().count() > 512 || control(value) {
        return Err(error("Phạm vi từ điển phải là * hoặc mã truyện hợp lệ."));
    }
    Ok(value)
}

pub(crate) fn entry(
    kind_name: &str,
    word: &str,
    meaning: &str,
    scope_name: &str,
) -> Result<Entry, ApiError> {
    let kind = kind(kind_name)?;
    let scope = scope(scope_name)?;
    let (word, meaning) = (trim(word), trim(meaning));
    if word.is_empty() || meaning.is_empty() {
        return Err(error("Từ và nghĩa không được rỗng."));
    }
    if word.chars().count() > 256 || meaning.chars().count() > 8192 {
        return Err(error("Từ hoặc nghĩa quá dài."));
    }
    if word.contains('=') || word.starts_with('#') || control(word) || control(meaning) {
        return Err(error(
            "Từ không chứa =, ký tự điều khiển hoặc bắt đầu bằng #; nghĩa phải nằm trên một dòng.",
        ));
    }
    if kind == "rules" {
        Rule::compile(word, meaning, true)?;
    }
    Ok(Entry {
        kind: kind.to_owned(),
        word: word.to_owned(),
        meaning: meaning.to_owned(),
        scope: scope.to_owned(),
    })
}

fn trim(value: &str) -> &str {
    value.trim_matches(python_whitespace)
}

fn control(value: &str) -> bool {
    value.bytes().any(|byte| byte < 32)
}

fn open_connection(path: &Path, writable: bool) -> Result<Connection, ApiError> {
    let db = Connection::open_with_flags(
        path,
        (if writable {
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE
        } else {
            OpenFlags::SQLITE_OPEN_READ_ONLY
        }) | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    db.busy_timeout(Duration::from_secs(10))?;
    db.create_scalar_function(
        "casefold",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| Ok(casefold(&ctx.get::<String>(0)?)),
    )?;
    Ok(db)
}

fn locked<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>, ApiError> {
    mutex.lock().map_err(|_| internal_error())
}

fn data_version(db: &Connection) -> Result<i64, ApiError> {
    Ok(db
        .prepare_cached("PRAGMA data_version")?
        .query_row([], |r| r.get(0))?)
}

impl QtDictionary {
    pub(crate) fn open(library: &Library) -> Result<Self, ApiError> {
        let owner = library.writable_lock()?;
        let root = library.root();
        for name in [
            "qt.sqlite3",
            "qt.sqlite3-wal",
            "qt.sqlite3-shm",
            "qt.sqlite3-journal",
        ] {
            checked_qt_slot(root, name)?;
        }
        let path = root.join("qt.sqlite3");
        let writer = open_connection(&path, true)?;
        writer.pragma_update(None, "journal_mode", "WAL")?;
        writer.execute_batch(
            "CREATE TABLE IF NOT EXISTS entries (
                kind TEXT NOT NULL, word TEXT NOT NULL, meaning TEXT NOT NULL,
                scope TEXT NOT NULL, PRIMARY KEY(kind, scope, word));
             CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY, created TEXT NOT NULL,
                kind TEXT NOT NULL, scope TEXT NOT NULL, entries TEXT NOT NULL);",
        )?;
        let version = data_version(&writer)?;
        Ok(Self {
            path,
            writer: Mutex::new(writer),
            mutation: Mutex::default(),
            cache: Mutex::new(Cache {
                version,
                epoch: 0,
                scopes: VecDeque::new(),
            }),
            _owner: owner,
        })
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }

    fn reader(&self) -> Result<Connection, ApiError> {
        let db = open_connection(&self.path, false)?;
        db.execute_batch("BEGIN")?;
        Ok(db)
    }

    fn begin_write(&self) -> Result<MutexGuard<'_, ()>, ApiError> {
        let guard = locked(&self.mutation)?;
        let mut retired = Vec::new();
        drop(self.current(&mut retired)?);
        Ok(guard)
    }

    fn commit<T>(
        &self,
        change: impl FnOnce(&Transaction<'_>) -> Result<T, ApiError>,
    ) -> Result<T, ApiError> {
        let mut db = locked(&self.writer)?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let result = change(&tx)?;
        tx.commit()?;
        Ok(result)
    }

    /// Book ids with entries of their own. Each step seeks the next (kind, scope) pair on the
    /// primary key, so the cost follows the number of scopes, not the number of entries.
    pub(crate) fn book_scopes(&self) -> Result<Vec<String>, ApiError> {
        let db = self.reader()?;
        let mut next = db.prepare(
            "SELECT kind,scope FROM entries WHERE (kind,scope)>(?1,?2) ORDER BY kind,scope LIMIT 1",
        )?;
        let (mut kind, mut scope) = (String::new(), String::new());
        let mut books = std::collections::BTreeSet::new();
        while let Some(pair) = next
            .query_row(params![kind, scope], |r| Ok((r.get(0)?, r.get(1)?)))
            .optional()?
        {
            (kind, scope) = pair;
            if scope != "*" {
                books.insert(scope.clone());
            }
        }
        Ok(books.into_iter().collect())
    }

    pub fn status(&self) -> Result<Value, ApiError> {
        let db = self.reader()?;
        let counts = rows(
            &db,
            "SELECT kind,scope,count(*) AS count FROM entries GROUP BY kind,scope",
            [],
        )?;
        let backups: i64 = db.query_row("SELECT count(*) FROM backups", [], |r| r.get(0))?;
        Ok(json!({
            "installed":!counts.is_empty(),"counts":counts,"backups":backups,
            "precedence":"book > global; name > vietphrase > pronouns > hanviet; longest within each tier; rules first",
            "rule_grammar":"{n}, {n1}..{n8}: Arabic number; {word}, {word1}..{word8}: 1..32 Han characters; between fields require a delimiter outside the preceding field alphabet; replacement {lookup:word} looks up dictionary",
            "bundled_data":false,"traditional_conversion":false
        }))
    }

    pub fn list(&self, kind_name: &str, scope_name: &str, query: &str) -> Result<Value, ApiError> {
        let (kind, scope, needle) = listing(kind_name, scope_name, query)?;
        let db = self.reader()?;
        let items = if needle.is_empty() {
            rows(
                &db,
                &format!("{SELECT_ENTRIES} ORDER BY word"),
                params![kind, scope],
            )?
        } else {
            rows(
                &db,
                &format!(
                    "{SELECT_ENTRIES} AND (instr(casefold(word),?3)>0 OR instr(casefold(meaning),?3)>0) ORDER BY word"
                ),
                params![kind, scope, needle],
            )?
        };
        Ok(Value::Array(items))
    }

    pub fn page(
        &self,
        kind_name: &str,
        scope_name: &str,
        query: &str,
        offset: i64,
        limit: i64,
    ) -> Result<Value, ApiError> {
        self.page_input(kind_name, scope_name, query, Some(offset), limit)
    }

    pub(crate) fn page_input(
        &self,
        kind_name: &str,
        scope_name: &str,
        query: &str,
        offset: Option<i64>,
        limit: i64,
    ) -> Result<Value, ApiError> {
        let (kind, scope, needle) = listing(kind_name, scope_name, query)?;
        if offset.is_some_and(|value| value < 0) || !(1..=500).contains(&limit) {
            return Err(error(
                "Trang từ điển cần offset không âm, limit từ 1 đến 500.",
            ));
        }
        let offset = offset.ok_or_else(internal_error)?;
        let db = self.reader()?;
        let (total, items) = if needle.is_empty() {
            let total: i64 = db.query_row(
                "SELECT count(*) FROM entries WHERE kind=? AND scope=?",
                params![kind, scope],
                |r| r.get(0),
            )?;
            let items = rows(
                &db,
                &format!("{SELECT_ENTRIES} ORDER BY word LIMIT ? OFFSET ?"),
                params![kind, scope, limit, offset],
            )?;
            (total, items)
        } else {
            let predicate = "WHERE kind=? AND scope=? AND (instr(casefold(word),?3)>0 OR instr(casefold(meaning),?3)>0)";
            let total: i64 = db.query_row(
                &format!("SELECT count(*) FROM entries {predicate}"),
                params![kind, scope, needle],
                |r| r.get(0),
            )?;
            let items = rows(
                &db,
                &format!(
                    "SELECT kind,word,meaning,scope FROM entries {predicate} ORDER BY word LIMIT ?4 OFFSET ?5"
                ),
                params![kind, scope, needle, limit, offset],
            )?;
            (total, items)
        };
        Ok(json!({"items":items,"total":total,"offset":offset,"limit":limit}))
    }

    pub fn save(
        &self,
        kind: &str,
        word: &str,
        meaning: &str,
        scope: &str,
    ) -> Result<Value, ApiError> {
        let item = entry(kind, word, meaning, scope)?;
        let _write = self.begin_write()?;
        self.commit(|tx| {
            tx.execute(
                UPSERT,
                params![item.kind, item.word, item.meaning, item.scope],
            )?;
            Ok(())
        })?;
        self.changed(
            &item.scope,
            Some((&item.kind, &item.word, Some(&item.meaning))),
        )?;
        Ok(item.value())
    }

    pub fn delete(&self, kind_name: &str, word: &str, scope_name: &str) -> Result<Value, ApiError> {
        let kind = kind(kind_name)?;
        let scope = scope(scope_name)?;
        let _write = self.begin_write()?;
        let count = self.commit(|tx| {
            Ok(tx.execute(
                "DELETE FROM entries WHERE kind=? AND scope=? AND word=?",
                params![kind, scope, word],
            )?)
        })?;
        self.changed(scope, Some((kind, word, None)))?;
        Ok(json!({"deleted": count}))
    }

    pub fn export_text(&self, kind_name: &str, scope_name: &str) -> Result<String, ApiError> {
        let kind = kind(kind_name)?;
        let scope = scope(scope_name)?;
        let meta = json!({"kind":kind,"scope":scope});
        let mut out = String::from(HEADER);
        out.push_str(&String::from_utf8(json_bytes(&meta)).map_err(|_| internal_error())?);
        out.push('\n');
        let db = self.reader()?;
        let mut stmt =
            db.prepare("SELECT word,meaning FROM entries WHERE kind=? AND scope=? ORDER BY word")?;
        let mut entries = stmt.query(params![kind, scope])?;
        while let Some(row) = entries.next()? {
            let word = row.get_ref(0)?.as_str().map_err(|_| internal_error())?;
            let meaning = row.get_ref(1)?.as_str().map_err(|_| internal_error())?;
            out.push_str(word);
            out.push('=');
            out.push_str(meaning);
            out.push('\n');
        }
        Ok(out)
    }

    pub fn import_text(
        &self,
        kind_name: &str,
        text: &str,
        mode: &str,
        scope_name: &str,
    ) -> Result<Value, ApiError> {
        let kind = kind(kind_name)?;
        let requested_scope = scope(scope_name)?;
        if !["merge", "missing", "replace"].contains(&mode) {
            return Err(error("Chế độ nhập phải là merge, missing hoặc replace."));
        }
        if text.chars().count() > 32_000_000 {
            return Err(error("Tệp từ điển phải là văn bản, tối đa 32 triệu ký tự."));
        }
        let text = text.trim_start_matches('\u{feff}');
        let mut lines = PythonLines(text).peekable();
        let metadata = lines
            .peek()
            .and_then(|line| line.strip_prefix(HEADER))
            .map(|raw| parse_header(raw, kind))
            .transpose()?;
        let scope = if let Some(file_scope) = metadata.as_deref() {
            if requested_scope != "*" && requested_scope != file_scope {
                return Err(error("Phạm vi trong tệp khác phạm vi đã chọn."));
            }
            file_scope
        } else {
            requested_scope
        };
        let mut items = HashMap::new();
        let mut errors = Vec::new();
        let mut skipped = 0usize;
        for (i, line) in lines.enumerate() {
            let line = trim(line);
            if line.is_empty() || line.starts_with('#') {
                continue;
            }
            let item = line
                .split_once('=')
                .or_else(|| line.split_once('\t'))
                .ok_or_else(|| error("Thiếu dấu = hoặc tab ngăn từ và nghĩa."))
                .and_then(|(word, meaning)| entry(kind, word, meaning, scope));
            match item {
                Ok(item) => skipped += usize::from(items.insert(item.word, item.meaning).is_some()),
                Err(error) => errors.push(json!({"line":i+1,"error":error.message})),
            }
        }
        if mode == "replace" && (items.is_empty() || !errors.is_empty()) {
            if errors.is_empty() {
                errors.push(json!({"line":0,"error":"Không thay thế bằng tệp rỗng; dùng thao tác xóa rõ ràng."}));
            }
            return Ok(
                json!({"kind":kind,"scope":scope,"mode":mode,"inserted":0,"updated":0,"skipped":skipped,
                "errors":errors,"applied":false,"backup_id":null}),
            );
        }
        let _write = self.begin_write()?;
        let (inserted, updated, backup_id) = self.commit(|tx| {
            let backup_id = if mode == "replace" {
                let id = backup(tx, kind, scope)?;
                tx.execute(
                    "DELETE FROM entries WHERE kind=? AND scope=?",
                    params![kind, scope],
                )?;
                Some(id)
            } else {
                None
            };
            let mut present = tx.prepare(
                "SELECT EXISTS(SELECT 1 FROM entries WHERE kind=? AND scope=? AND word=?)",
            )?;
            let mut upsert = tx.prepare(UPSERT)?;
            let (mut inserted, mut updated) = (0usize, 0usize);
            for (word, meaning) in items {
                let exists = backup_id.is_none()
                    && present.query_row(params![kind, scope, word], |r| r.get::<_, bool>(0))?;
                if exists && mode == "missing" {
                    skipped += 1;
                } else {
                    upsert.execute(params![kind, word, meaning, scope])?;
                    updated += usize::from(exists);
                    inserted += usize::from(!exists);
                }
            }
            Ok((inserted, updated, backup_id))
        })?;
        self.changed(scope, None)?;
        Ok(
            json!({"kind":kind,"scope":scope,"mode":mode,"inserted":inserted,"updated":updated,"skipped":skipped,
            "errors":errors,"applied":true,"backup_id":backup_id}),
        )
    }

    pub fn restore_backup(&self, id: i64) -> Result<Value, ApiError> {
        let _write = self.begin_write()?;
        let (kind, scope, restored) = self.commit(|tx| {
            let (kind, scope, entries) = tx
                .query_row(
                    "SELECT kind,scope,entries FROM backups WHERE id=?",
                    [id],
                    |r| {
                        Ok((
                            r.get::<_, String>(0)?,
                            r.get::<_, String>(1)?,
                            r.get::<_, String>(2)?,
                        ))
                    },
                )
                .optional()?
                .ok_or_else(|| error("Không tìm thấy bản sao từ điển."))?;
            let parsed = serde_json::from_value::<Vec<Entry>>(parse_json(&entries)?)
                .map_err(|_| internal_error())?;
            let items = parsed
                .iter()
                .map(|item| entry(&item.kind, &item.word, &item.meaning, &item.scope))
                .collect::<Result<Vec<_>, _>>()?;
            backup(tx, &kind, &scope)?;
            tx.execute(
                "DELETE FROM entries WHERE kind=? AND scope=?",
                params![kind, scope],
            )?;
            let mut insert =
                tx.prepare("INSERT INTO entries(kind,word,meaning,scope) VALUES(?,?,?,?)")?;
            for item in &items {
                insert.execute(params![item.kind, item.word, item.meaning, item.scope])?;
            }
            Ok((kind, scope, items.len()))
        })?;
        self.changed(&scope, None)?;
        Ok(json!({"restored":restored,"kind":kind,"scope":scope}))
    }
}

fn listing<'a>(
    kind_name: &str,
    scope_name: &'a str,
    query: &str,
) -> Result<(&'static str, &'a str, String), ApiError> {
    let kind = kind(kind_name)?;
    let scope = scope(scope_name)?;
    if query.chars().count() > 8192 {
        return Err(error("Chuỗi tìm kiếm không hợp lệ."));
    }
    Ok((kind, scope, casefold(query)))
}

struct PythonLines<'a>(&'a str);

impl<'a> Iterator for PythonLines<'a> {
    type Item = &'a str;
    fn next(&mut self) -> Option<Self::Item> {
        if self.0.is_empty() {
            return None;
        }
        let Some((index, c)) = self.0.char_indices().find(|(_, c)| {
            [
                '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
                '\u{2029}',
            ]
            .contains(c)
        }) else {
            return Some(std::mem::take(&mut self.0));
        };
        let line = &self.0[..index];
        self.0 = &self.0[index + c.len_utf8()..];
        if c == '\r' {
            self.0 = self.0.strip_prefix('\n').unwrap_or(self.0);
        }
        Some(line)
    }
}

fn parse_header(raw: &str, expected_kind: &str) -> Result<String, ApiError> {
    let bad_header = || error("Header từ điển không hợp lệ.");
    let metadata = parse_json(raw).map_err(|_| bad_header())?;
    let raw_kind = metadata.get("kind").ok_or_else(bad_header)?;
    if raw_kind.is_object() || raw_kind.is_array() {
        return Err(bad_header());
    }
    if kind(&crate::http::py_string(raw_kind))? != expected_kind {
        return Err(error("Loại từ điển trong tệp khác loại đã chọn."));
    }
    let raw_scope = metadata.get("scope").ok_or_else(bad_header)?;
    Ok(scope(raw_scope.as_str().unwrap_or_default())?.to_owned())
}

fn backup(db: &Connection, kind: &str, scope: &str) -> Result<i64, ApiError> {
    let entries = rows(
        db,
        &format!("{SELECT_ENTRIES} ORDER BY word"),
        params![kind, scope],
    )?;
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|_| internal_error())?;
    let seconds = i64::try_from(now.as_secs()).map_err(|_| internal_error())?;
    let mut created: String = db.query_row(
        "SELECT strftime('%Y-%m-%dT%H:%M:%S',?,'unixepoch')",
        [seconds],
        |r| r.get(0),
    )?;
    let micros = now.subsec_micros();
    if micros != 0 {
        created.push_str(&format!(".{micros:06}"));
    }
    created.push_str("+00:00");
    let json =
        String::from_utf8(json_bytes(&Value::Array(entries))).map_err(|_| internal_error())?;
    db.execute(
        "INSERT INTO backups(created,kind,scope,entries) VALUES(?,?,?,?)",
        params![created, kind, scope, json],
    )?;
    Ok(db.last_insert_rowid())
}

#[derive(Clone, Default)]
struct Trie {
    children: HashMap<char, Arc<Trie>>,
    meaning: Option<Arc<str>>,
}

impl Trie {
    fn is_empty(&self) -> bool {
        self.children.is_empty() && self.meaning.is_none()
    }

    fn insert(&mut self, word: &str, meaning: &str) {
        let mut node = self;
        for c in word.chars() {
            node = Arc::make_mut(node.children.entry(c).or_default());
        }
        node.meaning = Some(first_meaning(meaning));
    }

    fn edited(root: &Arc<Self>, word: &str, meaning: Option<&str>) -> Arc<Self> {
        let mut parents = Vec::new();
        let mut node = Some(root.as_ref());
        for c in word.chars() {
            parents.push((node, c));
            node = node.and_then(|n| n.children.get(&c)).map(Arc::as_ref);
        }
        if node.is_none() && meaning.is_none() {
            return root.clone();
        }
        let mut copy = node.cloned().unwrap_or_default();
        copy.meaning = meaning.map(first_meaning);
        for (parent, c) in parents.into_iter().rev() {
            let mut parent = parent.cloned().unwrap_or_default();
            if copy.is_empty() {
                parent.children.remove(&c);
            } else {
                parent.children.insert(c, Arc::new(copy));
            }
            copy = parent;
        }
        Arc::new(copy)
    }

    fn longest(&self, text: &str) -> Option<(usize, &str)> {
        let mut node = self;
        let mut best = None;
        for (offset, c) in text.char_indices() {
            let Some(next) = node.children.get(&c) else {
                break;
            };
            node = next;
            if let Some(meaning) = node.meaning.as_deref() {
                best = Some((offset + c.len_utf8(), meaning));
            }
        }
        best
    }
}

fn first_meaning(meaning: &str) -> Arc<str> {
    trim(meaning.split_once('¦').map_or(meaning, |(first, _)| first)).into()
}

type RuleIndex = HashMap<Option<char>, Vec<Rule>>;

struct ScopeSnapshot {
    tries: [Arc<Trie>; KINDS.len()],
    rules: Arc<RuleIndex>,
}

impl ScopeSnapshot {
    fn installed(&self) -> bool {
        !self.rules.is_empty() || self.tries.iter().any(|trie| !trie.is_empty())
    }
}

struct Cache {
    version: i64,
    epoch: u64,
    scopes: VecDeque<(String, Arc<ScopeSnapshot>)>,
}

impl Cache {
    fn key(&self) -> (i64, u64) {
        (self.version, self.epoch)
    }

    fn get(&self, scope: &str) -> Option<&Arc<ScopeSnapshot>> {
        self.scopes
            .iter()
            .find(|(key, _)| key == scope)
            .map(|(_, snapshot)| snapshot)
    }

    fn remove(&mut self, scope: &str) -> Option<(String, Arc<ScopeSnapshot>)> {
        self.scopes
            .iter()
            .position(|(key, _)| key == scope)
            .and_then(|index| self.scopes.remove(index))
    }
}

pub struct TranslationSnapshot {
    scopes: Vec<Arc<ScopeSnapshot>>,
}

impl QtDictionary {
    fn current(
        &self,
        retired: &mut Vec<Arc<ScopeSnapshot>>,
    ) -> Result<MutexGuard<'_, Cache>, ApiError> {
        let mut cache = locked(&self.cache)?;
        match self.writer.try_lock() {
            Ok(writer) => {
                let version = data_version(&writer)?;
                if version != cache.version {
                    retired.extend(cache.scopes.drain(..).map(|(_, snapshot)| snapshot));
                    cache.epoch = cache.epoch.wrapping_add(1);
                    cache.version = version;
                }
            }
            Err(TryLockError::WouldBlock) => (),
            Err(TryLockError::Poisoned(_)) => return Err(internal_error()),
        }
        Ok(cache)
    }

    fn build_rules(db: &Connection, scope: &str) -> Result<Arc<RuleIndex>, ApiError> {
        let mut rules = RuleIndex::new();
        let mut stmt = db.prepare(
            "SELECT word,meaning FROM entries WHERE kind='rules' AND scope=? ORDER BY word",
        )?;
        let mut rows = stmt.query([scope])?;
        while let Some(row) = rows.next()? {
            let word = row.get_ref(0)?.as_str().map_err(|_| internal_error())?;
            let meaning = row.get_ref(1)?.as_str().map_err(|_| internal_error())?;
            let first = word.chars().next().filter(|c| *c != '{');
            rules
                .entry(first)
                .or_default()
                .push(Rule::compile(word, meaning, true)?);
        }
        Ok(Arc::new(rules))
    }

    fn build_scope(&self, scope: &str) -> Result<ScopeSnapshot, ApiError> {
        let db = self.reader()?;
        let rules = Self::build_rules(&db, scope)?;
        let mut tries = [const { None }; KINDS.len()];
        let mut stmt = db.prepare("SELECT word,meaning FROM entries WHERE kind=? AND scope=?")?;
        for (index, kind) in KINDS.into_iter().enumerate() {
            let mut trie = Trie::default();
            let mut rows = stmt.query(params![kind, scope])?;
            while let Some(row) = rows.next()? {
                let word = row.get_ref(0)?.as_str().map_err(|_| internal_error())?;
                let meaning = row.get_ref(1)?.as_str().map_err(|_| internal_error())?;
                if word.chars().take(257).count() > 256 {
                    return Err(error("Từ hoặc nghĩa quá dài."));
                }
                trie.insert(word, meaning);
            }
            tries[index] = Some(Arc::new(trie));
        }
        Ok(ScopeSnapshot {
            tries: tries.map(Option::unwrap_or_default),
            rules,
        })
    }

    fn publish(
        &self,
        scope: &str,
        snapshot: ScopeSnapshot,
        expected: (i64, u64),
    ) -> Result<bool, ApiError> {
        let mut retired = Vec::new();
        let mut cache = self.current(&mut retired)?;
        if cache.key() != expected {
            return Ok(false);
        }
        if let Some((_, previous)) = cache.remove(scope) {
            retired.push(previous);
        }
        cache
            .scopes
            .push_back((scope.to_owned(), Arc::new(snapshot)));
        let mut private = cache
            .scopes
            .iter()
            .enumerate()
            .filter(|(_, (key, _))| key != "*");
        let oldest_private = private.next().map(|(index, _)| index);
        if private.count() >= 8
            && let Some((_, snapshot)) = oldest_private.and_then(|index| cache.scopes.remove(index))
        {
            retired.push(snapshot);
        }
        Ok(true)
    }

    fn changed(
        &self,
        scope: &str,
        edit: Option<(&str, &str, Option<&str>)>,
    ) -> Result<(), ApiError> {
        let result = (|| {
            let (old, expected) = {
                let mut retired = Vec::new();
                let mut cache = self.current(&mut retired)?;
                cache.epoch = cache.epoch.wrapping_add(1);
                (cache.get(scope).cloned(), cache.key())
            };
            let snapshot = match (edit, old) {
                (None, _) => self.build_scope(scope)?,
                (Some(_), None) => return Ok(()),
                (Some(("rules", _, _)), Some(previous)) => {
                    let db = self.reader()?;
                    ScopeSnapshot {
                        tries: previous.tries.clone(),
                        rules: Self::build_rules(&db, scope)?,
                    }
                }
                (Some((kind, word, meaning)), Some(previous)) => {
                    let index = KINDS
                        .iter()
                        .position(|tier| *tier == kind)
                        .ok_or_else(internal_error)?;
                    let mut tries = previous.tries.clone();
                    tries[index] = Trie::edited(&tries[index], word, meaning);
                    ScopeSnapshot {
                        tries,
                        rules: previous.rules.clone(),
                    }
                }
            };
            self.publish(scope, snapshot, expected)?;
            Ok(())
        })();
        if result.is_err() {
            // Committed database contents beat an older snapshot after a failed rebuild.
            let retired = locked(&self.cache)?.remove(scope);
            drop(retired);
        }
        result
    }

    pub fn snapshot(&self, book_id: &str) -> Result<TranslationSnapshot, ApiError> {
        if book_id.chars().count() > 512 {
            return Err(error("Mã truyện không hợp lệ."));
        }
        self.compiled(book_id)
    }

    fn compiled(&self, book_id: &str) -> Result<TranslationSnapshot, ApiError> {
        let scopes = [book_id, "*"];
        let scopes = &scopes[usize::from(book_id.is_empty() || book_id == "*")..];
        for _ in 0..4 {
            {
                let mut retired = Vec::new();
                let mut cache = self.current(&mut retired)?;
                if scopes.iter().all(|scope| cache.get(scope).is_some()) {
                    let mut snapshots = Vec::with_capacity(scopes.len());
                    for scope in scopes {
                        if let Some(item) = cache.remove(scope) {
                            snapshots.push(item.1.clone());
                            cache.scopes.push_back(item);
                        }
                    }
                    return Ok(TranslationSnapshot { scopes: snapshots });
                }
            }
            let _mutation = locked(&self.mutation)?;
            for scope in scopes {
                let expected = {
                    let mut retired = Vec::new();
                    let cache = self.current(&mut retired)?;
                    if cache.get(scope).is_some() {
                        continue;
                    }
                    cache.key()
                };
                let snapshot = self.build_scope(scope)?;
                if !self.publish(scope, snapshot, expected)? {
                    break;
                }
            }
        }
        Err(error(
            "Từ điển đang được thay đổi liên tục; hãy thử dịch lại.",
        ))
    }
}

static FIELDS: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"\{(n[1-8]?|word[1-8]?)\}").expect("constant QT grammar"));
static TEMPLATE_FIELDS: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"\{(lookup:)?([a-zA-Z0-9]+)\}").expect("constant QT template grammar")
});
static ALNUM: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"^[\p{L}\p{N}]$").expect("Unicode 16 general categories"));

struct Rule {
    regex: Regex,
    template: Vec<Template>,
}

enum Template {
    Literal(String),
    Field { index: usize, lookup: bool },
}

impl Rule {
    fn compile(pattern: &str, replacement: &str, anchored: bool) -> Result<Self, ApiError> {
        if pattern.chars().count() > 256 || replacement.chars().count() > 2048 {
            return Err(error(
                "Luật quá dài (pattern tối đa 256, bản dịch tối đa 2048 ký tự).",
            ));
        }
        let mut source = if anchored {
            "^".to_owned()
        } else {
            String::new()
        };
        let mut fields = Vec::<&str>::new();
        let mut end = 0;
        let mut bad_braces = false;
        for field in FIELDS.captures_iter(pattern) {
            let matched = field.get(0).ok_or_else(internal_error)?;
            let name = field.get(1).ok_or_else(internal_error)?.as_str();
            if fields.contains(&name) {
                return Err(error(format!(
                    "Placeholder trong pattern không được lặp: {name}"
                )));
            }
            let separator = &pattern[end..matched.start()];
            bad_braces |= separator.contains(['{', '}']);
            if let Some(previous) = fields.last() {
                let unique = if previous.starts_with("word") {
                    separator.chars().any(|c| !han_word(c))
                } else {
                    separator
                        .chars()
                        .any(|c| !(c.is_ascii_digit() || c == '.' || c == ','))
                };
                if !unique {
                    return Err(error(
                        "Giữa các placeholder cần dấu phân cách mà placeholder trước không thể chứa (word: ngoài chữ Hán; n: ngoài 0-9, dấu chấm/phẩy).",
                    ));
                }
            }
            source.push_str(&regex::escape(separator));
            source.push_str(if name.starts_with('n') {
                r"([0-9]{1,12}(?:[.,][0-9]{1,6})?)"
            } else {
                r"([\u{3400}-\u{9fff}\u{f900}-\u{faff}]{1,32})"
            });
            fields.push(name);
            end = matched.end();
        }
        let ending = &pattern[end..];
        if bad_braces || ending.contains(['{', '}']) {
            return Err(error(
                "Placeholder pattern chỉ hỗ trợ {n}, {n1}…{n8}, {word}, {word1}…{word8}.",
            ));
        }
        if pattern.is_empty() {
            return Err(error("Pattern không được rỗng."));
        }
        source.push_str(&regex::escape(ending));

        let mut template = Vec::new();
        end = 0;
        bad_braces = false;
        for field in TEMPLATE_FIELDS.captures_iter(replacement) {
            let matched = field.get(0).ok_or_else(internal_error)?;
            let name = field.get(2).ok_or_else(internal_error)?.as_str();
            let index = fields
                .iter()
                .position(|candidate| *candidate == name)
                .ok_or_else(|| {
                    error(format!(
                        "Bản dịch tham chiếu placeholder chưa khai báo: {name}"
                    ))
                })?;
            let lookup = field.get(1).is_some();
            if lookup && !name.starts_with("word") {
                return Err(error("lookup chỉ dùng với placeholder word."));
            }
            let literal = &replacement[end..matched.start()];
            bad_braces |= literal.contains(['{', '}']);
            if !literal.is_empty() {
                template.push(Template::Literal(literal.to_owned()));
            }
            template.push(Template::Field {
                index: index + 1,
                lookup,
            });
            end = matched.end();
        }
        let ending = &replacement[end..];
        if bad_braces || ending.contains(['{', '}']) {
            return Err(error("Placeholder bản dịch không hợp lệ."));
        }
        if !ending.is_empty() {
            template.push(Template::Literal(ending.to_owned()));
        }
        let regex = Regex::new(&source).map_err(|_| internal_error())?;
        Ok(Self { regex, template })
    }

    fn replacement(&self, matched: &Captures<'_>, snapshot: &TranslationSnapshot) -> String {
        let mut result = String::new();
        for piece in &self.template {
            match piece {
                Template::Literal(literal) => result.push_str(literal),
                Template::Field { index, lookup } => {
                    let value = matched.get(*index).map_or("", |m| m.as_str());
                    result.push_str(if *lookup {
                        snapshot.lookup(value)
                    } else {
                        value
                    });
                }
            }
        }
        result
    }
}

fn han_word(c: char) -> bool {
    matches!(c, '\u{3400}'..='\u{9fff}' | '\u{f900}'..='\u{faff}')
}

fn isalnum(c: char) -> bool {
    if c.is_ascii() {
        c.is_ascii_alphanumeric()
    } else {
        ALNUM.is_match(c.encode_utf8(&mut [0; 4]))
    }
}

fn text_limit(text: &str) -> Result<(), ApiError> {
    if text.chars().take(2_000_001).count() > 2_000_000 {
        return Err(error("Nội dung QT phải là văn bản, tối đa 2 triệu ký tự."));
    }
    Ok(())
}

fn append(
    out: &mut String,
    previous_translated: &mut bool,
    part: &str,
    translated: bool,
) -> Result<(), ApiError> {
    let Some(first) = part.chars().next() else {
        return Ok(());
    };
    let space = (translated || *previous_translated)
        && out.chars().next_back().is_some_and(isalnum)
        && isalnum(first);
    out.try_reserve(part.len() + usize::from(space))
        .map_err(|_| internal_error())?;
    if space {
        out.push(' ');
    }
    out.push_str(part);
    *previous_translated = translated;
    Ok(())
}

impl TranslationSnapshot {
    fn lookup<'a>(&'a self, text: &'a str) -> &'a str {
        self.scopes
            .iter()
            .flat_map(|scope| &scope.tries)
            .filter_map(|trie| trie.longest(text))
            .find(|(end, _)| *end == text.len())
            .map_or(text, |(_, part)| part)
    }

    pub fn translate(&self, text: &str) -> Result<String, ApiError> {
        text_limit(text)?;
        self.translate_checked(text)
    }

    fn translate_checked(&self, text: &str) -> Result<String, ApiError> {
        if !self.scopes.iter().any(|scope| scope.installed())
            && text
                .chars()
                .any(|c| han_word(c) || matches!(c, '\u{20000}'..='\u{323af}'))
        {
            return Err(error(
                "Chưa có dữ liệu QT cho truyện này. Hãy nhập Names/VietPhrase/Hán Việt trước.",
            ));
        }
        let mut out = String::with_capacity(text.len());
        let mut previous_translated = false;
        let mut remaining = text;
        while let Some(first) = remaining.chars().next() {
            let mut chosen = None;
            for scope in &self.scopes {
                let mut longest = 0;
                for key in [Some(first), None] {
                    if let Some(rules) = scope.rules.get(&key) {
                        for rule in rules {
                            if let Some(matched) = rule.regex.captures(remaining)
                                && let Some(whole) = matched.get(0)
                                && whole.end() > longest
                            {
                                longest = whole.end();
                                chosen = Some((longest, rule, matched));
                            }
                        }
                    }
                }
                if chosen.is_some() {
                    break;
                }
            }
            if let Some((end, rule, matched)) = chosen {
                append(
                    &mut out,
                    &mut previous_translated,
                    &rule.replacement(&matched, self),
                    true,
                )?;
                remaining = &remaining[end..];
            } else if let Some((end, part)) = self
                .scopes
                .iter()
                .flat_map(|scope| &scope.tries)
                .find_map(|trie| trie.longest(remaining))
            {
                append(&mut out, &mut previous_translated, part, true)?;
                remaining = &remaining[end..];
            } else {
                let (part, rest) = remaining.split_at(first.len_utf8());
                append(&mut out, &mut previous_translated, part, false)?;
                remaining = rest;
            }
        }
        Ok(out)
    }
}

impl QtDictionary {
    pub fn translate(&self, text: &str, book_id: &str) -> Result<String, ApiError> {
        text_limit(text)?;
        self.snapshot(book_id)?.translate_checked(text)
    }

    pub fn validate_rule(&self, pattern: &str, replacement: &str) -> Value {
        validate_rule(Some(pattern), Some(replacement))
    }

    pub fn preview_rule(
        &self,
        pattern: &str,
        replacement: &str,
        sample: &str,
        book_id: &str,
    ) -> Result<Value, ApiError> {
        self.preview_rule_input(Some(pattern), Some(replacement), sample, &json!(book_id))
    }

    pub(crate) fn preview_rule_input(
        &self,
        pattern: Option<&str>,
        replacement: Option<&str>,
        sample: &str,
        book_id: &Value,
    ) -> Result<Value, ApiError> {
        let mut result = validate_rule(pattern, replacement);
        if sample.chars().count() > 100_000 {
            return Err(error("Mẫu thử luật tối đa 100.000 ký tự."));
        }
        if result["valid"] != true {
            result["output"] = json!(sample);
            result["matches"] = json!([]);
            return Ok(result);
        }
        let numeric_scope: String;
        let scope = match book_id {
            Value::String(scope) => scope,
            value if !crate::http::truthy(value) => "",
            Value::Bool(true) => "1",
            Value::Number(number) => {
                let spelling = number.to_string();
                let bound = if spelling.contains(['.', 'e', 'E']) {
                    rusqlite::types::Value::Real(spelling.parse().map_err(|_| internal_error())?)
                } else {
                    rusqlite::types::Value::Integer(crate::library::sqlite_integer(&spelling)?)
                };
                numeric_scope =
                    self.reader()?
                        .query_row("SELECT CAST(? AS TEXT)", [bound], |r| r.get(0))?;
                &numeric_scope
            }
            _ => return Err(internal_error()),
        };
        let snapshot = self.compiled(scope)?;
        let rule = Rule::compile(
            pattern.ok_or_else(internal_error)?,
            replacement.ok_or_else(internal_error)?,
            false,
        )?;
        let mut output = String::with_capacity(sample.len());
        let mut matches = Vec::new();
        let (mut byte_end, mut char_end) = (0, 0);
        for capture in rule.regex.captures_iter(sample) {
            let found = capture.get(0).ok_or_else(internal_error)?;
            let prefix = &sample[byte_end..found.start()];
            let start = char_end + prefix.chars().count();
            char_end = start + found.as_str().chars().count();
            byte_end = found.end();
            let replacement = rule.replacement(&capture, &snapshot);
            output.push_str(prefix);
            output.push_str(&replacement);
            matches.push(json!({"start":start,"end":char_end,"source":found.as_str(),"translation":replacement}));
        }
        output.push_str(&sample[byte_end..]);
        result["output"] = json!(output);
        result["matches"] = json!(matches);
        Ok(result)
    }
}

fn validate_rule(pattern: Option<&str>, replacement: Option<&str>) -> Value {
    let validated = pattern
        .zip(replacement)
        .ok_or_else(|| error(TEXT_ERROR))
        .and_then(|(pattern, replacement)| entry("rules", pattern, replacement, "*"));
    match validated {
        Ok(_) => {
            json!({"valid":true,"errors":[],"warnings":["Chỉ hỗ trợ template giới hạn; không phải regex hoặc cú pháp luật của ứng dụng khác."]})
        }
        Err(error) => json!({"valid":false,"errors":[error.message],"warnings":[]}),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::reader::tests::writable;

    #[test]
    fn m4_preview_validates_before_binding_scalar_book_ids() {
        let (_fixture, library) = writable();
        let qt = QtDictionary::open(&library).unwrap();
        for (scope, meaning) in [
            ("*", "global"),
            ("1", "integer"),
            ("1.0e+20", "large float"),
            ("Inf", "infinite float"),
            ("-9223372036854775808", "minimum"),
        ] {
            qt.save("name", "甲", meaning, scope).unwrap();
        }
        let preview = |book_id: &Value| {
            qt.preview_rule_input(Some("{word}"), Some("{lookup:word}"), "甲", book_id)
        };
        for (book_id, output) in [
            (json!(1.0e20), "large float"),
            (parse_json("1e400").unwrap(), "infinite float"),
            (json!(true), "integer"),
            (json!(i64::MIN), "minimum"),
            (json!(false), "global"),
            (json!([]), "global"),
            (json!({}), "global"),
        ] {
            assert_eq!(preview(&book_id).unwrap()["output"], output, "{book_id}");
        }
        for invalid in [json!(9223372036854775808u64), json!([1]), json!({"x":1})] {
            assert_eq!(preview(&invalid).unwrap_err().status, 500);
            assert_eq!(
                qt.preview_rule_input(Some("{bad}"), Some("bad"), "甲", &invalid)
                    .unwrap()["valid"],
                false,
            );
        }
    }

    #[test]
    fn m4_scope_precedence_translation_rules_and_scalar_offsets() {
        let (_fixture, library) = writable();
        let qt = QtDictionary::open(&library).unwrap();
        assert_eq!(qt.translate("Chữ Việt", "").unwrap(), "Chữ Việt");
        assert!(qt.translate("𠀀", "").is_err());
        qt.save("name", "苹果", "Names", "*").unwrap();
        qt.save("vp", "苹果", "táo¦quả táo", "book").unwrap();
        qt.save("vp", "苹果树", "cây", "book").unwrap();
        qt.save("rule", "{n}个{word}", "{n} quả {lookup:word}", "*")
            .unwrap();
        assert_eq!(
            qt.translate("2个苹果。苹果树", "book").unwrap(),
            "2 quả táo。cây"
        );
        assert_eq!(qt.translate("苹果树", "other").unwrap(), "Names 树");
        let preview = qt
            .preview_rule("{n}个{word}", "{n} quả {lookup:word}", "😀2个苹果!", "book")
            .unwrap();
        assert_eq!(
            preview["matches"][0],
            json!({"start":1,"end":5,"source":"2个苹果","translation":"2 quả táo"})
        );
        assert_eq!(preview["output"], "😀2 quả táo!");
        assert_eq!(
            qt.preview_rule("{word}的{word1}", "x", "样本", "").unwrap()["valid"],
            false
        );
        assert_eq!(qt.translate("苹果\u{345}", "book").unwrap(), "táo\u{345}");
    }

    #[test]
    fn m4_import_and_backup_restore_validate_before_changing_data() {
        let (_fixture, library) = writable();
        let qt = QtDictionary::open(&library).unwrap();
        qt.save("vp", "一", "Straße = cũ", "book").unwrap();
        assert_eq!(
            qt.page("vp", "book", "STRASSE", 0, 1).unwrap()["items"][0]["word"],
            "一"
        );
        let export = qt.export_text("vp", "book").unwrap();
        assert_eq!(
            export,
            "# qt-dictionary-v1 {\"kind\": \"vietphrase\", \"scope\": \"book\"}\n一=Straße = cũ\n"
        );
        assert_eq!(
            qt.import_text("vp", "bad line", "replace", "book").unwrap()["applied"],
            false
        );
        let report = qt.import_text("vp", "二=mới", "replace", "book").unwrap();
        assert_eq!(report["inserted"], 1);
        assert_eq!(
            qt.restore_backup(report["backup_id"].as_i64().unwrap())
                .unwrap()["restored"],
            1
        );
        assert_eq!(qt.translate("一", "book").unwrap(), "Straße = cũ");
        assert_eq!(qt.status().unwrap()["backups"], 2);
        assert_eq!(
            qt.import_text("vp", &export, "missing", "*").unwrap()["skipped"],
            1
        );
        let invalid = qt
            .import_text("vp", "丙=one\r\nbad line\r\n丁=two", "replace", "book")
            .unwrap();
        assert_eq!(
            invalid["errors"],
            json!([{"line":2,"error":"Thiếu dấu = hoặc tab ngăn từ và nghĩa."}])
        );
        assert_eq!(
            qt.page_input("vp", "book", "", None, 1).unwrap_err().status,
            500
        );
        assert_eq!(
            qt.page_input("vp", "book", "", None, 0).unwrap_err().status,
            400
        );
    }

    #[test]
    fn m4_snapshots_share_unedited_paths_allow_warm_reads_and_retire() {
        let (_fixture, library) = writable();
        let qt = QtDictionary::open(&library).unwrap();
        qt.import_text("vp", "甲=old\n乙=keep", "merge", "*")
            .unwrap();
        let old = qt.snapshot("book").unwrap();
        let private = Arc::downgrade(&old.scopes[0]);
        let old_global = Arc::downgrade(&old.scopes[1]);
        let db = qt.writer.lock().unwrap();
        std::thread::scope(|threads| {
            let (send, receive) = std::sync::mpsc::channel();
            let dictionary = &qt;
            threads.spawn(move || send.send(dictionary.translate("甲乙", "book")).unwrap());
            let translated = receive.recv_timeout(Duration::from_secs(1));
            drop(db);
            assert_eq!(translated.unwrap().unwrap(), "old keep");
        });
        qt.save("vp", "甲", "new", "*").unwrap();
        let next = qt.snapshot("book").unwrap();
        assert!(Arc::ptr_eq(&next.scopes[0], &old.scopes[0]));
        assert!(Arc::ptr_eq(
            &next.scopes[1].tries[1].children[&'乙'],
            &old.scopes[1].tries[1].children[&'乙']
        ));
        assert_eq!(old.translate("甲乙").unwrap(), "old keep");
        assert_eq!(next.translate("甲乙").unwrap(), "new keep");
        drop(next);
        for i in 0..12 {
            qt.snapshot(&format!("other-{i}")).unwrap();
        }
        let cache = qt.cache.lock().unwrap();
        assert_eq!(cache.scopes.len(), 9);
        assert!(cache.get("*").is_some());
        assert!(cache.get("book").is_none());
        drop(cache);
        assert!(private.upgrade().is_some());
        assert!(old_global.upgrade().is_some());
        drop(old);
        assert!(private.upgrade().is_none());
        assert!(old_global.upgrade().is_none());
    }

    #[test]
    fn m4_external_commit_rejects_stale_publish_and_failed_write_keeps_cache() {
        let (_fixture, library) = writable();
        let qt = QtDictionary::open(&library).unwrap();
        qt.import_text("vp", "甲=old", "merge", "*").unwrap();
        let old = qt.snapshot("*").unwrap();
        qt.writer
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER reject_write BEFORE INSERT ON entries
             WHEN NEW.word='乙' BEGIN SELECT RAISE(ABORT,'synthetic'); END;",
            )
            .unwrap();
        assert!(qt.import_text("vp", "乙=bad", "replace", "*").is_err());
        assert!(Arc::ptr_eq(
            &old.scopes[0],
            &qt.snapshot("*").unwrap().scopes[0]
        ));
        assert_eq!(qt.status().unwrap()["backups"], 0);
        let expected = qt.cache.lock().unwrap().key();
        let stale = qt.build_scope("*").unwrap();
        let external = open_connection(qt.path(), true).unwrap();
        external
            .execute("UPDATE entries SET meaning='outside' WHERE word='甲'", [])
            .unwrap();
        assert!(!qt.publish("*", stale, expected).unwrap());
        assert_eq!(qt.translate("甲", "*").unwrap(), "outside");
        assert_eq!(old.translate("甲").unwrap(), "old");
    }

    #[test]
    fn m4_failed_rebuild_invalidates_scope_and_releases_the_old_snapshot() {
        let (_fixture, library) = writable();
        let qt = QtDictionary::open(&library).unwrap();
        qt.import_text("vp", "甲=old", "merge", "*").unwrap();
        let snapshot = qt.snapshot("*").unwrap();
        qt.writer
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER invalid_rule AFTER INSERT ON entries WHEN NEW.word='乙' BEGIN
                 INSERT INTO entries VALUES('rules','{bogus}','broken','*');
             END;",
            )
            .unwrap();
        assert!(qt.import_text("vp", "乙=new", "merge", "*").is_err());
        assert!(qt.cache.lock().unwrap().get("*").is_none());
        assert!(qt.translate("甲", "*").is_err());
        assert_eq!(snapshot.translate("甲").unwrap(), "old");
        let external = open_connection(qt.path(), true).unwrap();
        external
            .execute("DELETE FROM entries WHERE kind='rules'", [])
            .unwrap();
        assert_eq!(qt.translate("甲乙", "*").unwrap(), "old new");
    }
}
