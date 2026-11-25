use crate::{
    ApiError, Library,
    http::{json_bytes, py_string, truthy},
    library::{integer, parse_json},
};
use rusqlite::{Connection, OptionalExtension, params, types::ValueRef};
use serde_json::{Map, Value, json};
#[cfg(test)]
use sha2::{Digest, Sha256};

pub(crate) fn now() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}
fn fail(message: &str) -> ApiError {
    ApiError::new(400, message)
}
fn json_store(value: &Value) -> Result<String, ApiError> {
    crate::http::validate_finite_json(value)?;
    Ok(String::from_utf8(json_bytes(value)).expect("JSON UTF8"))
}
pub(crate) fn int(v: &Value) -> Result<i64, ApiError> {
    match v {
        Value::String(s) => integer(s),
        Value::Bool(v) => Ok(i64::from(*v)),
        Value::Number(n) => {
            let raw = n.to_string();
            let value = n.as_f64().or_else(|| raw.parse::<f64>().ok());
            if raw.contains(['.', 'e', 'E']) && value.is_some_and(|v| !v.is_finite()) {
                return Err(rusqlite::Error::InvalidQuery.into());
            }
            n.as_i64()
                .or_else(|| value.map(|v| v as i64))
                .ok_or_else(|| fail("Invalid integer"))
        }
        _ => Err(rusqlite::Error::InvalidQuery.into()),
    }
}
fn sqlite_int(v: &Value) -> Result<i64, ApiError> {
    if let Value::String(s) = v {
        return crate::library::sqlite_integer(s);
    }
    if let Value::Number(n) = v {
        let raw = n.to_string();
        if !raw.contains(['.', 'e', 'E']) {
            return crate::library::sqlite_integer(&raw);
        }
        let value = raw
            .parse::<f64>()
            .map_err(|_| ApiError::from(rusqlite::Error::InvalidQuery))?;
        if !value.is_finite() || !(-9223372036854775808.0..9223372036854775808.0).contains(&value) {
            return Err(rusqlite::Error::InvalidQuery.into());
        }
    }
    int(v)
}
fn float(v: &Value) -> Result<f64, ApiError> {
    match v {
        Value::Number(n) => {
            let raw = n.to_string();
            let value = raw
                .parse::<f64>()
                .map_err(|_| ApiError::from(rusqlite::Error::InvalidQuery))?;
            if !raw.contains(['.', 'e', 'E']) && !value.is_finite() {
                return Err(rusqlite::Error::InvalidQuery.into());
            }
            Ok(value)
        }
        Value::Bool(b) => Ok(if *b { 1.0 } else { 0.0 }),
        Value::String(s) => {
            let invalid = || {
                fail(&format!(
                    "could not convert string to float: {}",
                    crate::library::py_repr_full(s)
                ))
            };
            let mapped = s
                .trim_matches(crate::casefold::integer_whitespace)
                .chars()
                .map(|c| {
                    crate::library::decimal(c)
                        .and_then(|n| char::from_digit(n, 10))
                        .unwrap_or(c)
                })
                .collect::<String>();
            let bytes = mapped.as_bytes();
            for (index, c) in bytes.iter().enumerate() {
                if *c == b'_'
                    && (index == 0
                        || !bytes[index - 1].is_ascii_digit()
                        || !bytes.get(index + 1).is_some_and(u8::is_ascii_digit))
                {
                    return Err(invalid());
                }
            }
            mapped.replace('_', "").parse().map_err(|_| invalid())
        }
        _ => Err(rusqlite::Error::InvalidQuery.into()),
    }
}
fn sql_value(v: &Value) -> Result<rusqlite::types::Value, ApiError> {
    Ok(match v {
        Value::Null => rusqlite::types::Value::Null,
        Value::Bool(b) => (*b as i64).into(),
        Value::Number(n) => {
            let raw = n.to_string();
            if raw.contains(['.', 'e', 'E']) {
                raw.parse::<f64>()
                    .map_err(|_| ApiError::from(rusqlite::Error::InvalidQuery))?
                    .into()
            } else {
                crate::library::sqlite_integer(&raw)?.into()
            }
        }
        Value::String(s) => s.clone().into(),
        _ => {
            return Err(ApiError::new(
                500,
                "Có lỗi khi xử lý. Dữ liệu gốc chưa bị thay đổi.",
            ));
        }
    })
}
pub(crate) fn rows(
    db: &Connection,
    sql: &str,
    values: impl rusqlite::Params,
) -> Result<Vec<Value>, ApiError> {
    let mut stmt = db.prepare(sql)?;
    let names = stmt
        .column_names()
        .iter()
        .map(|s| s.to_string())
        .collect::<Vec<_>>();
    let mut result = Vec::new();
    let mut query = stmt.query(values)?;
    while let Some(row) = query.next()? {
        let mut item = Map::new();
        for (i, name) in names.iter().enumerate() {
            item.insert(
                name.clone(),
                match row.get_ref(i)? {
                    ValueRef::Null => Value::Null,
                    ValueRef::Integer(i) => i.into(),
                    ValueRef::Real(f) => json!(f),
                    ValueRef::Text(s) => String::from_utf8_lossy(s).into_owned().into(),
                    _ => return Err(fail("Invalid SQLite value")),
                },
            );
        }
        result.push(Value::Object(item));
    }
    Ok(result)
}
pub(crate) fn book_value(mut item: Value) -> Result<Value, ApiError> {
    let map = item.as_object_mut().expect("row object");
    let groups = map.remove("groups_json").unwrap_or(Value::Null);
    map.insert("groups".into(), parse_json(groups.as_str().unwrap_or(""))?);
    let archived = map.get("archived").is_some_and(truthy);
    map.insert("archived".into(), archived.into());
    let trashed = map.get("trashed_at").and_then(Value::as_f64).unwrap_or(0.0);
    map.insert(
        "trash_expires_at".into(),
        if trashed == 0.0 {
            Value::Null
        } else {
            json!(trashed + 2592000.0)
        },
    );
    let raw = map.remove("raw").unwrap_or_else(|| json!("{}"));
    let metadata = parse_json(raw.as_str().unwrap_or(""))?;
    if metadata.is_null()
        || metadata.is_number()
        || metadata.is_boolean()
        || metadata
            .as_array()
            .is_some_and(|a| a.iter().any(|v| v == "warnings" || v == "ocr"))
        || metadata
            .as_str()
            .is_some_and(|s| s.contains("warnings") || s.contains("ocr"))
    {
        return Err(ApiError::from(rusqlite::Error::InvalidQuery));
    }
    for key in ["warnings", "ocr"] {
        if let Some(v) = metadata.get(key) {
            map.insert(key.into(), v.clone());
        }
    }
    Ok(item)
}
fn metadata(item: &mut Value) -> Result<Value, ApiError> {
    let raw = item
        .as_object_mut()
        .expect("row")
        .shift_remove("raw")
        .unwrap_or_else(|| json!("{}"));
    parse_json(raw.as_str().unwrap_or(""))
}
pub(crate) fn ensure_chapter_cache(db: &Connection, id: &str, index: i64) -> Result<(), ApiError> {
    db.execute("INSERT INTO chapter_cache(book_id,idx,kind,bytes) SELECT c.book_id,c.idx,CASE WHEN b.source NOT IN ('qq','full','hako','stv') OR c.original_html IS NOT NULL OR c.original_text IS NOT NULL THEN 'protected' WHEN c.downloaded=1 THEN 'legacy' ELSE 'cache' END,length(CAST(c.html AS BLOB))+length(CAST(c.text AS BLOB))+length(CAST(c.images AS BLOB))+COALESCE(length(CAST(c.original_html AS BLOB)),0)+COALESCE(length(CAST(c.original_text AS BLOB)),0)+length(CAST(c.raw AS BLOB)) FROM chapters c JOIN books b ON b.id=c.book_id WHERE c.book_id=? AND c.idx=? AND NOT EXISTS(SELECT 1 FROM chapter_cache WHERE book_id=c.book_id AND idx=c.idx)",params![id,index])?;
    Ok(())
}

pub(crate) fn track(db: &Connection, id: &str, index: i64) -> Result<(), ApiError> {
    ensure_chapter_cache(db, id, index)?;
    db.execute(
        "UPDATE chapter_cache SET last_used=?,version=version+1 WHERE book_id=? AND idx=?",
        params![now(), id, index],
    )?;
    Ok(())
}

pub(crate) fn chapter_value(mut item: Value) -> Result<Value, ApiError> {
    let meta = metadata(&mut item)?;
    item["index"] = item
        .as_object_mut()
        .expect("row")
        .shift_remove("idx")
        .unwrap();
    let images = parse_json(item["images"].as_str().unwrap_or(""))?;
    item["images"] = images;
    item["downloaded"] = truthy(&item["downloaded"]).into();
    if meta.get("is_vip").is_some_and(Value::is_boolean) {
        item["is_vip"] = meta["is_vip"].clone();
    }
    if meta["translation"] == "source" && meta["language"] == "vi" {
        item["translation"] = json!("source");
        item["language"] = json!("vi");
    }
    Ok(item)
}
impl Library {
    pub fn get_book(&self, id: &str) -> Result<Value, ApiError> {
        let db = self.reader_connection()?;
        let item = rows(&db, "SELECT * FROM books WHERE id=?", [id])?
            .pop()
            .ok_or_else(|| ApiError::new(404, "Không tìm thấy truyện."))?;
        book_value(item)
    }
    pub fn chapters(&self, id: &str) -> Result<Value, ApiError> {
        self.get_book(id)?;
        let db = self.reader_connection()?;
        let mut items = rows(
            &db,
            "SELECT idx,title,url,downloaded,raw FROM chapters WHERE book_id=? ORDER BY idx",
            [id],
        )?;
        for item in &mut items {
            let meta = metadata(item)?;
            if meta.get("is_vip").is_some_and(Value::is_boolean) {
                item["is_vip"] = meta["is_vip"].clone();
            }
            item["index"] = item["idx"].clone();
            item["downloaded"] = truthy(&item["downloaded"]).into();
        }
        Ok(items.into())
    }
    /// Fetches one online chapter in the background into the ordinary reader cache (an
    /// evictable entry, never an offline copy). Runs on the calling request thread; see
    /// `prefetch.rs` for the scheduling rules. Failures are reported quietly, never retried.
    pub fn prefetch_chapter(
        &self,
        id: &str,
        index: i64,
        stopping: &dyn Fn() -> bool,
    ) -> Result<Value, ApiError> {
        let skipped = || Ok(json!({"prefetch":"skipped"}));
        let book = self.get_book(id)?;
        let source = book["source"].as_str().unwrap_or_default().to_owned();
        if !self.sources.enabled() || !["qq", "full", "hako", "stv"].contains(&source.as_str()) {
            return skipped();
        }
        if chapter_value(self.raw_chapter(id, index)?)?["downloaded"] == true {
            return skipped();
        }
        let spacing = if source == "stv" {
            std::time::Duration::ZERO
        } else {
            crate::prefetch::SPACING
        };
        let Some(turn) = self
            .prefetch
            .begin((id.to_owned(), index), &source, spacing, stopping)
        else {
            return skipped();
        };
        // A reader request may have fetched it while this prefetch waited, or a download or
        // reader request may be fetching it right now (leases count requests using a chapter).
        let item = chapter_value(self.raw_chapter(id, index)?)?;
        if item["downloaded"] == true
            || self
                .chapter_leases
                .lock()
                .contains_key(&(id.to_owned(), index))
        {
            return skipped();
        }
        let lease = self.chapter_leases.acquire((id.to_owned(), index));
        let Ok(content) = self.fetch_source_chapter(
            id,
            index,
            &source,
            item["url"].as_str().unwrap_or_default(),
            None,
            &|| turn.background(),
            || turn.cancelled() || stopping(),
        ) else {
            return Ok(json!({"prefetch":"failed"}));
        };
        // Like downloads, do not start new database work once shutdown has begun.
        if stopping() {
            return Ok(json!({"prefetch":"failed"}));
        }
        self.save_source_chapter(
            id,
            index,
            item["revision"]
                .as_i64()
                .ok_or(rusqlite::Error::InvalidQuery)?,
            Some(&content),
            None,
        )?;
        // Then the first images of the saved chapter, whatever the book's kind: manga pages and
        // illustrations embedded in text, collected exactly as downloads collect them.
        // The chapter is saved: release it for eviction and compaction during the image phase.
        drop(lease);
        turn.start_images();
        // From here on nothing can fail the saved chapter; an unreadable image list is skipped.
        let Ok(stored) = self.raw_chapter(id, index).and_then(chapter_value) else {
            return Ok(json!({"prefetch":"done"}));
        };
        let referer = book["path"].as_str().unwrap_or_default();
        let mut urls = Vec::new();
        for image in stored["images"].as_array().into_iter().flatten() {
            urls.extend(
                crate::http::asset_image_urls(image.as_str().unwrap_or_default(), referer)
                    .unwrap_or_default(),
            );
        }
        for field in ["html", "original_html"] {
            if let Some(markup) = stored[field].as_str() {
                urls.extend(crate::http::asset_markup_urls(markup, referer).unwrap_or_default());
            }
        }
        let mut seen = std::collections::HashSet::new();
        urls.retain(|url| seen.insert(url.clone()));
        for url in urls.into_iter().take(crate::prefetch::IMAGES) {
            use sha2::Digest;
            let name = format!(
                "{:x}",
                sha2::Sha256::digest(crate::http::relative_remote(&url, "").as_bytes())
            );
            let path = self.root().join("images").join(name);
            // Already cached, or being fetched or served by another request right now.
            if path.exists() || self.asset_leases.lock().contains_key(&path) {
                continue;
            }
            if !turn.pace(&source, crate::prefetch::SPACING, stopping) {
                break;
            }
            // An evictable cache entry within the image budget; a failure is skipped, never retried.
            let _ = self.save_source_image(&url, referer, None);
        }
        Ok(json!({"prefetch":"done"}))
    }
    pub fn chapter(
        &self,
        id: &str,
        index: i64,
        reader: Option<&str>,
        sequence: i64,
    ) -> Result<Value, ApiError> {
        // Take over or reuse a prefetch of this chapter; cancel ones that are no longer relevant.
        self.prefetch.claim(id, index);
        let _lease = self.chapter_leases.acquire((id.to_owned(), index));
        let book = self.get_book(id)?;
        let mut item = chapter_value(self.raw_chapter(id, index)?)?;
        if item["downloaded"] != true
            && self.sources.enabled()
            && ["qq", "full", "hako", "stv"].contains(&book["source"].as_str().unwrap_or_default())
        {
            let content = self.fetch_source_chapter(
                id,
                index,
                book["source"].as_str().unwrap_or_default(),
                item["url"].as_str().unwrap_or_default(),
                None,
                &|| false,
                || false,
            )?;
            self.save_source_chapter(
                id,
                index,
                item["revision"]
                    .as_i64()
                    .ok_or(rusqlite::Error::InvalidQuery)?,
                Some(&content),
                None,
            )?;
            item = chapter_value(self.raw_chapter(id, index)?)?;
        }
        if item["downloaded"] != true {
            return Err(
                if ["qq", "full", "hako", "stv"].contains(&book["source"].as_str().unwrap_or("")) {
                    ApiError::new(503, "Nguồn không khả dụng trong chế độ ngoại tuyến.")
                        .with_code("network_error")
                } else {
                    fail("Chương chưa có nội dung. Hãy nhập tệp hoặc chọn lại nguồn tương ứng.")
                },
            );
        }
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        track(&tx, id, index)?;
        tx.commit()?;
        if let Some(reader) = reader {
            self.reader(&json!({"id":reader,"book_id":id,"index":index,"sequence":sequence}))?;
        }
        if let Some(images) = item["images"].as_array_mut() {
            for url in images {
                if let Some(s) = url.as_str()
                    && (s.starts_with("https://") || s.starts_with("http://"))
                {
                    *url = json!(format!(
                        "/api/image?url={}&referer={}",
                        crate::http::quote(s, true),
                        crate::http::quote(book["path"].as_str().unwrap_or(""), true)
                    ));
                }
            }
        }
        Ok(item)
    }
    pub fn reader(&self, body: &Value) -> Result<Value, ApiError> {
        let token = body["id"]
            .as_str()
            .filter(|s| (1..=128).contains(&s.chars().count()))
            .ok_or_else(|| fail("Mã phiên đọc không hợp lệ."))?;
        let seq = body
            .get("sequence")
            .unwrap_or(&Value::from(0))
            .as_u64()
            .filter(|i| *i <= 1 << 53)
            .ok_or_else(|| fail("Thứ tự phiên đọc không hợp lệ."))?;
        let id = body.get("book_id").unwrap_or(&Value::Null);
        let active = !id.is_null();
        let (book, index) = if active {
            let book = id
                .as_str()
                .filter(|s| !s.is_empty() && s.chars().count() <= 256)
                .ok_or_else(|| fail("Truyện hoặc chỉ số chương đọc không hợp lệ."))?;
            let index = body["index"]
                .as_u64()
                .filter(|i| *i <= 1 << 53)
                .ok_or_else(|| fail("Truyện hoặc chỉ số chương đọc không hợp lệ."))?;
            (Some(book), Some(index as i64))
        } else {
            (None, None)
        };
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let time = now();
        let old: Option<(Option<String>, i64)> = tx
            .query_row(
                "SELECT book_id,sequence FROM chapter_readers WHERE id=? AND expires>?",
                params![token, time],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?;
        if let Some((ref old_id, old_seq)) = old
            && old_seq > seq as i64
        {
            return Ok(json!({"active":old_id.is_some()}));
        }
        if old.is_none()
            && tx.query_row(
                "SELECT count(*) FROM chapter_readers WHERE expires>?",
                [time],
                |r| r.get::<_, i64>(0),
            )? >= 128
        {
            return Err(fail("Quá nhiều phiên đọc đang mở."));
        }
        if let (Some(book), Some(index)) = (book, index) {
            if !tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM chapters WHERE book_id=? AND idx=? AND downloaded=1)",
                params![book, index],
                |r| r.get::<_, bool>(0),
            )? {
                return Err(fail("Chương đọc không còn được lưu; hãy mở lại chương."));
            }
            track(&tx, book, index)?;
        }
        tx.execute("DELETE FROM chapter_readers WHERE expires<=?", [time])?;
        tx.execute("INSERT INTO chapter_readers(id,book_id,idx,sequence,expires) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET book_id=excluded.book_id,idx=excluded.idx,sequence=excluded.sequence,expires=excluded.expires",params![token,book,index,seq as i64,time+300.0])?;
        tx.commit()?;
        Ok(json!({"active":active}))
    }
    pub fn settings(&self, body: Option<&Value>) -> Result<Value, ApiError> {
        if let Some(body) = body {
            let mut db = self.write_connection()?;
            let tx = db.transaction()?;
            for (key, value) in body.as_object().expect("validated JSON object") {
                tx.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",params![key,json_store(value)?])?;
            }
            tx.commit()?;
        }
        let db = self.reader_connection()?;
        let mut result = Map::new();
        for row in rows(&db, "SELECT * FROM settings", [])? {
            result.insert(
                row["key"].as_str().unwrap().into(),
                parse_json(row["value"].as_str().unwrap())?,
            );
        }
        Ok(result.into())
    }
    pub fn groups(&self) -> Result<Value, ApiError> {
        Ok(rows(
            &*self.reader_connection()?,
            "SELECT id,name FROM groups_list ORDER BY name",
            [],
        )?
        .into())
    }
    /// New groups get a random id, so the id never depends on the name.
    pub fn create_group(&self, body: &Value) -> Result<Value, ApiError> {
        let name = group_name(body)?;
        let mut bytes = [0u8; 16];
        getrandom::fill(&mut bytes).map_err(|e| ApiError::new(500, e.to_string()))?;
        let id: String = bytes.iter().map(|b| format!("{b:02x}")).collect();
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        unique_group_name(&tx, &name, None)?;
        tx.execute("INSERT INTO groups_list VALUES(?,?)", params![id, name])?;
        tx.commit()?;
        Ok(json!({"id":id,"name":name}))
    }
    pub fn rename_group(&self, id: &str, body: &Value) -> Result<Value, ApiError> {
        let name = group_name(body)?;
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        unique_group_name(&tx, &name, Some(id))?;
        if tx.execute(
            "UPDATE groups_list SET name=? WHERE id=?",
            params![name, id],
        )? == 0
        {
            return Err(ApiError::new(404, "Không tìm thấy nhóm."));
        }
        tx.commit()?;
        Ok(json!({"id":id,"name":name}))
    }
    /// Removes the group and drops its id from the books that had it; nothing else changes.
    pub fn delete_group(&self, id: &str) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        if tx.execute("DELETE FROM groups_list WHERE id=?", [id])? == 0 {
            return Err(ApiError::new(404, "Không tìm thấy nhóm."));
        }
        let members: Vec<(String, String)> = tx
            .prepare(
                "SELECT id,groups_json FROM books WHERE id IN \
                 (SELECT book_id FROM book_groups INDEXED BY book_group_lookup WHERE group_id=?)",
            )?
            .query_map([id], |r| Ok((r.get(0)?, r.get(1)?)))?
            .collect::<Result<_, _>>()?;
        for (book, groups) in &members {
            let mut groups = crate::library::parse_json(groups)?;
            if let Some(items) = groups.as_array_mut() {
                items.retain(|item| item.as_str() != Some(id));
            }
            tx.execute(
                "UPDATE books SET groups_json=? WHERE id=?",
                params![String::from_utf8(json_bytes(&groups)).unwrap(), book],
            )?;
        }
        tx.commit()?;
        Ok(json!({"deleted":true,"books":members.len()}))
    }
    pub fn progress(&self, id: &str, body: &Value) -> Result<Value, ApiError> {
        let index = int(body
            .get("index")
            .ok_or_else(|| ApiError::new(404, "index"))?)?;
        let position = float(
            body.get("position")
                .ok_or_else(|| ApiError::new(404, "position"))?,
        )?;
        let book = self.get_book(id)?;
        let count = book["chapter_count"].as_i64().unwrap_or(0);
        if !position.is_finite() || !(0.0..=1.0).contains(&position) || index < 0 || index >= count
        {
            return Err(fail("Vị trí đọc không hợp lệ."));
        }
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        if let Some(row) = rows(
            &tx,
            "SELECT downloaded,raw FROM chapters WHERE book_id=? AND idx=?",
            params![id, index],
        )?
        .pop()
            && !truthy(&row["downloaded"])
            && parse_json(row["raw"].as_str().unwrap_or(""))?["access"]["state"] == "vip_locked"
        {
            return Err(fail(
                "Chương VIP chưa đọc được; vị trí đọc trước đó được giữ nguyên.",
            ));
        }
        tx.execute(
            "UPDATE books SET chapter_index=?,position=?,progress=?,read_at=? WHERE id=?",
            params![
                index,
                position,
                (index as f64 + position) / count as f64,
                now(),
                id
            ],
        )?;
        tx.commit()?;
        self.get_book(id)
    }
    pub fn notes(&self, id: Option<&str>) -> Result<Value, ApiError> {
        let db = self.reader_connection()?;
        Ok(if let Some(id) = id.filter(|s| !s.is_empty()) {
            rows(
                &db,
                "SELECT * FROM notes WHERE book_id=? ORDER BY created_at DESC",
                [id],
            )?
        } else {
            rows(&db, "SELECT * FROM notes ORDER BY created_at DESC", [])?
        }
        .into())
    }
    pub fn save_note(&self, body: &Value) -> Result<Value, ApiError> {
        let book = body
            .get("book_id")
            .filter(|v| truthy(v))
            .map(py_string)
            .unwrap_or_default();
        if book.is_empty() {
            return Err(fail("Thiếu truyện của ghi chú."));
        }
        let id = if let Some(id) = body.get("id").filter(|v| truthy(v)) {
            py_string(id)
        } else {
            let mut bytes = [0u8; 16];
            getrandom::fill(&mut bytes).map_err(|e| ApiError::new(500, e.to_string()))?;
            bytes[6] = (bytes[6] & 15) | 64;
            bytes[8] = (bytes[8] & 63) | 128;
            bytes.iter().map(|b| format!("{b:02x}")).collect()
        };
        let position = float(
            body.get("position")
                .filter(|v| truthy(v))
                .unwrap_or(&json!(0)),
        )?;
        if !position.is_finite() {
            return Err(fail("Vị trí ghi chú không hợp lệ."));
        }
        let index = sqlite_int(body.get("chapter_index").unwrap_or(&json!(0)))?;
        let get = |key: &str, default: &str| {
            body.get(key)
                .map(py_string)
                .unwrap_or_else(|| default.into())
        };
        let db = self.write_connection()?;
        db.execute("INSERT INTO notes VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET quote=excluded.quote,note=excluded.note,color=excluded.color,position=excluded.position",params![id,book,index,get("quote",""),get("note",""),get("color",""),position,get("type","note"),now(),json_store(body)?])?;
        Ok(rows(&db, "SELECT * FROM notes WHERE id=?", [id])?
            .pop()
            .expect("inserted note"))
    }
    pub fn delete_note(&self, id: &str) -> Result<Value, ApiError> {
        self.write_connection()?
            .execute("DELETE FROM notes WHERE id=?", [id])?;
        Ok(json!({"deleted":true}))
    }
    pub fn update_book(&self, id: &str, body: &Value) -> Result<Value, ApiError> {
        self.get_book(id)?;
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        for key in ["name", "author", "archived", "cover"] {
            if let Some(v) = body.get(key) {
                tx.execute(
                    &format!("UPDATE books SET {key}=? WHERE id=?"),
                    params![sql_value(v)?, id],
                )?;
            }
        }
        if let Some(groups) = body.get("groups") {
            let groups = if groups.is_null() || groups.as_f64() == Some(-1.0) || groups == "-1" {
                Vec::new()
            } else if let Some(a) = groups.as_array() {
                a.iter().map(py_string).filter(|v| v != "-1").collect()
            } else {
                vec![py_string(groups)]
            };
            tx.execute(
                "UPDATE books SET groups_json=? WHERE id=?",
                params![String::from_utf8(json_bytes(&json!(groups))).unwrap(), id],
            )?;
        }
        tx.commit()?;
        if body.get("cover").is_some() {
            self.index_book_assets(id)?;
        }
        self.get_book(id)
    }
    pub fn touch_image(&self, name: &str, size: u64) -> Result<(), ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        tx.execute("INSERT INTO image_cache(name,size,last_used,legacy) VALUES(?,?,?,1) ON CONFLICT(name) DO UPDATE SET size=excluded.size,last_used=excluded.last_used",params![name,size as i64,now()])?;
        tx.execute("INSERT OR IGNORE INTO image_pins(book_id,idx,name) SELECT r.book_id,r.idx,r.name FROM asset_refs r JOIN books b ON b.id=r.book_id WHERE r.kind='images' AND r.name=? AND b.source IN ('file','local')",[name])?;
        tx.execute("UPDATE asset_gc_candidates SET evict=0,generation=generation+1,retry_at=0 WHERE kind='images' AND name=? AND evict=1",[name])?;
        tx.commit()?;
        Ok(())
    }
    pub fn pause_downloads(&self, pause: bool) -> Result<Value, ApiError> {
        let mut db = self.write_connection()?;
        db.busy_timeout(std::time::Duration::from_secs(1))?;
        let tx = db.transaction_with_behavior(rusqlite::TransactionBehavior::Immediate)?;
        let active=tx.query_row("SELECT count(*) FROM download_jobs WHERE status IN ('queued','running','verification_required')",[],|r|r.get::<_,i64>(0))?;
        if active > 0 && !pause {
            return Ok(
                json!({"error":"Tạm dừng các tác vụ tải đang chạy và tắt máy chủ?","code":"active_downloads","active_downloads":active}),
            );
        }
        tx.execute("UPDATE download_jobs SET status=CASE WHEN cancelled=1 THEN 'cancelled' ELSE 'paused' END,updated_at=? WHERE status IN ('queued','running','verification_required')",[now()])?;
        tx.commit()?;
        Ok(json!({"accepted":true}))
    }
    /// Return the WAL to zero length when no reader holds an old snapshot; skips quickly when busy.
    pub(crate) fn truncate_wal(&self) {
        if let Ok(db) = self.write_connection()
            && db
                .busy_timeout(std::time::Duration::from_millis(50))
                .is_ok()
        {
            let _ = db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |_| Ok(()));
        }
    }
    pub fn checkpoint(&self) -> Result<(), ApiError> {
        let db = self.write_connection()?;
        db.busy_timeout(std::time::Duration::from_secs(1))?;
        db.query_row("PRAGMA wal_checkpoint(PASSIVE)", [], |_| Ok(()))?;
        Ok(())
    }
}

fn group_name(body: &Value) -> Result<String, ApiError> {
    let name = body
        .get("name")
        .and_then(Value::as_str)
        .unwrap_or("")
        .trim();
    if name.is_empty() || name.chars().count() > 100 || name.chars().any(char::is_control) {
        return Err(fail("Tên nhóm phải có từ 1 đến 100 ký tự."));
    }
    Ok(name.to_owned())
}

/// Group names are unique ignoring case, so the shelf never shows two look-alike groups.
fn unique_group_name(
    db: &rusqlite::Connection,
    name: &str,
    except: Option<&str>,
) -> Result<(), ApiError> {
    let folded = crate::casefold::casefold(name);
    let mut statement = db.prepare("SELECT id,name FROM groups_list")?;
    let mut rows = statement.query([])?;
    while let Some(row) = rows.next()? {
        let (id, other): (String, String) = (row.get(0)?, row.get(1)?);
        if Some(id.as_str()) != except && crate::casefold::casefold(&other) == folded {
            return Err(ApiError::new(409, "Đã có nhóm tên này."));
        }
    }
    Ok(())
}

#[cfg(test)]
pub(crate) mod tests {
    use super::*;
    use crate::library::test_common as common;
    #[test]
    fn group_membership_changes_keep_the_group_identity() {
        let (_fixture, library) = writable();
        let group = library.create_group(&json!({"name":" Seinen "})).unwrap();
        assert_eq!(group["name"], "Seinen");
        let before = library.groups().unwrap();
        assert_eq!(before, json!([group.clone()]));
        library
            .update_book("book", &json!({"groups":[group["id"]]}))
            .unwrap();
        assert_eq!(library.groups().unwrap(), before, "adding a book");
        library.update_book("book", &json!({"groups":[]})).unwrap();
        assert_eq!(library.groups().unwrap(), before, "removing the last book");
    }
    const ONLINE_BOOK: &str = "https://truyenfull.live/truyen-a/";
    /// A Truyện Full book with three chapters; the source serves chapters 1 and 2 only.
    fn online() -> (common::Fixture, Library) {
        let fixture = common::Fixture::new("empty");
        let folder = fixture.db.parent().unwrap();
        let marker = folder.join("m1-fixture.json");
        let mut value: Value = serde_json::from_slice(&std::fs::read(&marker).unwrap()).unwrap();
        value["sealed"] = false.into();
        std::fs::write(&marker, serde_json::to_vec(&value).unwrap()).unwrap();
        let page = |n: i64| {
            (
                format!("{ONLINE_BOOK}chuong-{n}/"),
                format!(
                    "<html><body><div class=\"chapter-c\"><p>Chương {n} tải trước.</p>{}</div></body></html>",
                    if n == 2 {
                        (0..12)
                            .map(|k| format!("<img src=\"{}\">", illustration(k)))
                            .collect::<String>()
                    } else {
                        String::new()
                    }
                ),
            )
        };
        let pages: serde_json::Map<String, Value> = [page(1), page(2)]
            .into_iter()
            .map(|(k, v)| (k, v.into()))
            .collect();
        // Chapter 2 is an illustrated chapter: 12 embedded images, all served by the source.
        let images: serde_json::Map<String, Value> =
            (0..12).map(|k| (illustration(k), PNG.into())).collect();
        std::fs::write(
            folder.join("m5-sources.json"),
            serde_json::to_vec(
                &json!({"format":"reader-local-m5-sources-1","pages":pages,"images":images}),
            )
            .unwrap(),
        )
        .unwrap();
        let library = Library::open_writable_fixture(&fixture.db).unwrap();
        let db = library.write_connection().unwrap();
        db.execute("INSERT INTO books(id,name,source,path,chapter_count,added_at,updated_at) VALUES('web','Truyện mạng','full',?,4,1,1)", [ONLINE_BOOK]).unwrap();
        for n in 0..4 {
            db.execute(
                "INSERT INTO chapters(book_id,idx,title,url,text,downloaded) VALUES('web',?,?,?,?,?)",
                params![n, format!("Chương {n}"), format!("{ONLINE_BOOK}chuong-{n}/"), if n == 0 { "Đã có." } else { "" }, n == 0],
            )
            .unwrap();
        }
        drop(db);
        (fixture, library)
    }
    const PNG: &str = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";
    fn illustration(k: usize) -> String {
        format!("https://i.hako.vip/illusts/page-{k}.png")
    }
    fn cached_image(library: &Library, url: &str) -> bool {
        use sha2::Digest;
        let name = format!("{:x}", sha2::Sha256::digest(url.as_bytes()));
        library.root().join("images").join(name).exists()
    }
    #[test]
    fn prefetch_warms_only_the_first_ten_images_as_ordinary_cache() {
        let (_fixture, library) = online();
        assert_eq!(
            library.prefetch_chapter("web", 2, &|| false).unwrap(),
            json!({"prefetch":"done"})
        );
        let cached: Vec<bool> = (0..12)
            .map(|k| cached_image(&library, &illustration(k)))
            .collect();
        assert_eq!(
            cached,
            [vec![true; 10], vec![false; 2]].concat(),
            "first ten, in reading order"
        );
        let db = library.write_connection().unwrap();
        let (rows, protected): (i64, i64) = db
            .query_row(
                "SELECT count(*),coalesce(sum(protected),0) FROM image_cache",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .unwrap();
        let pins: i64 = db
            .query_row("SELECT count(*) FROM image_pins", [], |r| r.get(0))
            .unwrap();
        assert_eq!(
            (rows, protected, pins),
            (10, 0, 0),
            "evictable, never pinned offline"
        );
    }
    #[test]
    fn opening_the_chapter_stops_its_image_prefetch() {
        let (_fixture, library) = online();
        library.prefetch.claim("web", 1);
        std::thread::scope(|scope| {
            let prefetch = scope.spawn(|| library.prefetch_chapter("web", 2, &|| false).unwrap());
            // Open it once it is saved, while its images are still being fetched.
            while chapter_value(library.raw_chapter("web", 2).unwrap()).unwrap()["downloaded"]
                != true
            {}
            library.chapter("web", 2, None, 0).unwrap();
            prefetch.join().unwrap();
        });
        let cached = (0..12)
            .filter(|k| cached_image(&library, &illustration(*k)))
            .count();
        assert!(
            cached < 10,
            "the reader took over; {cached} images were prefetched"
        );
    }
    fn cache_kind(library: &Library, index: i64) -> Option<(String, i64)> {
        library
            .write_connection()
            .unwrap()
            .query_row(
                "SELECT kind,offline FROM chapter_cache WHERE book_id='web' AND idx=?",
                [index],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .ok()
    }
    #[test]
    fn prefetch_saves_the_next_chapter_as_an_ordinary_cache_entry() {
        let (_fixture, library) = online();
        assert_eq!(
            library.prefetch_chapter("web", 1, &|| false).unwrap(),
            json!({"prefetch":"done"})
        );
        assert_eq!(
            cache_kind(&library, 1),
            Some(("cache".into(), 0)),
            "evictable, not offline"
        );
        let opened = library.chapter("web", 1, None, 0).unwrap();
        assert!(
            opened["html"]
                .as_str()
                .unwrap()
                .contains("Chương 1 tải trước.")
        );
        assert_eq!(cache_kind(&library, 1), Some(("cache".into(), 0)));
    }
    #[test]
    fn prefetch_skips_saved_chapters_and_offline_books() {
        let (_fixture, library) = online();
        let skipped = json!({"prefetch":"skipped"});
        assert_eq!(
            library.prefetch_chapter("web", 0, &|| false).unwrap(),
            skipped
        );
        library
            .write_connection()
            .unwrap()
            .execute("UPDATE books SET source='file' WHERE id='web'", [])
            .unwrap();
        assert_eq!(
            library.prefetch_chapter("web", 1, &|| false).unwrap(),
            skipped
        );
        assert_eq!(cache_kind(&library, 1), None);
    }
    #[test]
    fn prefetch_skips_a_chapter_another_request_is_fetching() {
        let (_fixture, library) = online();
        let _download = library.chapter_leases.acquire(("web".into(), 1));
        assert_eq!(
            library.prefetch_chapter("web", 1, &|| false).unwrap(),
            json!({"prefetch":"skipped"})
        );
        assert_eq!(cache_kind(&library, 1), None);
    }
    #[test]
    fn a_failed_prefetch_is_quiet_frees_its_slot_and_the_reader_fetches_normally() {
        let (_fixture, library) = online();
        // Chapter 3 is not served by the source.
        assert_eq!(
            library.prefetch_chapter("web", 3, &|| false).unwrap(),
            json!({"prefetch":"failed"})
        );
        assert_eq!(cache_kind(&library, 3), None);
        assert_eq!(
            library.prefetch_chapter("web", 2, &|| false).unwrap(),
            json!({"prefetch":"done"})
        );
        assert!(
            library.chapter("web", 3, None, 0).is_err(),
            "foreground still tries the source"
        );
    }
    #[test]
    fn renaming_a_group_keeps_its_id_and_books() {
        let (_fixture, library) = writable();
        let group = library.create_group(&json!({"name":"Seinen"})).unwrap();
        let id = group["id"].as_str().unwrap();
        library
            .update_book("book", &json!({"groups":[id]}))
            .unwrap();
        let renamed = library
            .rename_group(id, &json!({"name":"Thanh niên"}))
            .unwrap();
        assert_eq!(renamed, json!({"id":id,"name":"Thanh niên"}));
        assert_eq!(library.groups().unwrap(), json!([renamed]));
        assert_eq!(library.get_book("book").unwrap()["groups"], json!([id]));
    }
    #[test]
    fn deleting_a_group_only_removes_it_from_its_books() {
        let (_fixture, library) = writable();
        let (a, b) = (
            library.create_group(&json!({"name":"A"})).unwrap(),
            library.create_group(&json!({"name":"B"})).unwrap(),
        );
        library
            .update_book("book", &json!({"groups":[a["id"], b["id"]]}))
            .unwrap();
        let before = library.get_book("book").unwrap();
        library.delete_group(a["id"].as_str().unwrap()).unwrap();
        assert_eq!(library.groups().unwrap(), json!([b]));
        let mut after = library.get_book("book").unwrap();
        assert_eq!(after["groups"], json!([b["id"]]));
        after["groups"] = before["groups"].clone();
        assert_eq!(after, before, "nothing else about the book changes");
    }
    #[test]
    fn group_names_are_required_and_unique() {
        let (_fixture, library) = writable();
        let group = library.create_group(&json!({"name":"Seinen"})).unwrap();
        let id = group["id"].as_str().unwrap();
        for bad in [
            json!({}),
            json!({"name":"  "}),
            json!({"name":"x".repeat(101)}),
        ] {
            assert_eq!(library.create_group(&bad).unwrap_err().status, 400, "{bad}");
        }
        assert_eq!(
            library
                .create_group(&json!({"name":"SEINEN"}))
                .unwrap_err()
                .status,
            409
        );
        let other = library.create_group(&json!({"name":"Shounen"})).unwrap();
        let other = other["id"].as_str().unwrap();
        assert_eq!(
            library
                .rename_group(other, &json!({"name":"seinen"}))
                .unwrap_err()
                .status,
            409
        );
        // Renaming a group to its own name (any case) is allowed.
        assert!(library.rename_group(id, &json!({"name":"SEINEN"})).is_ok());
        assert_eq!(
            library
                .rename_group("missing", &json!({"name":"x"}))
                .unwrap_err()
                .status,
            404
        );
        assert_eq!(library.delete_group("missing").unwrap_err().status, 404);
    }
    #[test]
    fn m2_json_boundary_survives_settings_and_book_metadata() {
        let (_fixture, library) = writable();
        let value = json!({"literal":{"$serde_json::private::Number":"123","other":true}});
        library.settings(Some(&value)).unwrap();
        assert_eq!(library.settings(None).unwrap()["literal"], value["literal"]);
        let db = library.write_connection().unwrap();
        db.execute(
            "UPDATE books SET raw=? WHERE id='book'",
            ["{\"warnings\":{\"$serde_json::private::Number\":\"123\"}} "],
        )
        .unwrap();
        drop(db);
        assert_eq!(
            library.get_book("book").unwrap()["warnings"],
            json!({"$serde_json::private::Number":"123"})
        );
    }

    pub(crate) fn writable() -> (common::Fixture, Library) {
        let fixture = common::Fixture::new("empty");
        let marker = fixture.db.parent().unwrap().join("m1-fixture.json");
        let mut value: Value = serde_json::from_slice(&std::fs::read(&marker).unwrap()).unwrap();
        value["sealed"] = false.into();
        std::fs::write(marker, serde_json::to_vec(&value).unwrap()).unwrap();
        let library = Library::open_writable_fixture(&fixture.db).unwrap();
        let db = library.write_connection().unwrap();
        db.execute("INSERT INTO books(id,name,chapter_count,added_at,updated_at) VALUES('book','Truyện',2,1,1)",[]).unwrap();
        db.execute("INSERT INTO chapters(book_id,idx,text,downloaded) VALUES('book',0,'Tiếng Việt',1),('book',1,'',0)",[]).unwrap();
        drop(db);
        (fixture, library)
    }
    #[test]
    fn m2_review_cover_normalization_preserves_existing_pins() {
        let (_fixture, library) = writable();
        let db = library.write_connection().unwrap();
        let name = format!(
            "{:x}",
            Sha256::digest(b"https://cdn.example.org/m2-page.png")
        );
        db.execute(
            "UPDATE books SET source='file',path='https://cdn.example.org/book' WHERE id='book'",
            [],
        )
        .unwrap();
        db.execute(
            "INSERT INTO image_cache(name,last_used) VALUES(?,1)",
            [&name],
        )
        .unwrap();
        db.execute(
            "INSERT INTO asset_refs VALUES('book',-1,'images',?)",
            [&name],
        )
        .unwrap();
        drop(db);
        for cover in [
            "/api/image?url=https%3A%2F%2Fcdn.example.org%2Fm2-page.png",
            "HTTPS://CDN.EXAMPLE.ORG:443/m2-page.png",
            "m2-page.png",
            "/m2-page.png",
        ] {
            library
                .update_book("book", &json!({"cover":cover}))
                .unwrap();
            let db = library.reader_connection().unwrap();
            assert_eq!(
                db.query_row(
                    "SELECT pin_count FROM image_cache WHERE name=?",
                    [&name],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
                1,
                "{cover}"
            );
            assert_eq!(
                db.query_row(
                    "SELECT count(*) FROM asset_refs WHERE book_id='book' AND idx=-1 AND name=?",
                    [&name],
                    |r| r.get::<_, i64>(0)
                )
                .unwrap(),
                1,
                "{cover}"
            );
        }
        let original = format!("{}.txt", "a".repeat(64));
        let db = library.write_connection().unwrap();
        db.execute("UPDATE books SET path=? WHERE id='book'", [&original])
            .unwrap();
        drop(db);
        library
            .update_book(
                "book",
                &json!({"cover":"https://cdn.example.org/m2-page.png"}),
            )
            .unwrap();
        library
            .update_book("book", &json!({"cover":"http://[broken"}))
            .unwrap();
        let db = library.reader_connection().unwrap();
        assert_eq!(
            db.query_row(
                "SELECT blocked FROM asset_pending WHERE book_id='book' AND idx=-1",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            1
        );
        assert_eq!(
            db.query_row(
                "SELECT count(*) FROM asset_refs WHERE book_id='book' AND idx=-1",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            2
        );
    }
    #[test]
    fn m2_review_note_integer_storage_is_exact() {
        let (_fixture, library) = writable();
        for value in [
            json!(9223372036854775808u64),
            json!("9223372036854775808"),
            json!("-9223372036854775809"),
            json!(1e30),
            parse_json("-9223372036854775809").unwrap(),
            parse_json("18446744073709551616").unwrap(),
        ] {
            assert_eq!(
                library
                    .save_note(&json!({"id":"bad","book_id":"book","chapter_index":value}))
                    .unwrap_err()
                    .status,
                500
            );
            assert!(library.notes(None).unwrap().as_array().unwrap().is_empty());
        }
        assert_eq!(
            library
                .save_note(
                    &json!({"id":"min","book_id":"book","chapter_index":"-9223372036854775808"})
                )
                .unwrap()["chapter_index"],
            i64::MIN
        );
    }
    #[test]
    fn m2_review_numeric_coercions_match_python() {
        for value in [json!("0_0.5"), json!("٠.٥")] {
            assert_eq!(float(&value).unwrap(), 0.5);
        }
        assert_eq!(float(&Value::Null).unwrap_err().status, 500);
        assert_eq!(int(&Value::Null).unwrap_err().status, 500);
        let (_fixture, library) = writable();
        assert_eq!(
            library
                .update_book("book", &json!({"groups":-1.0}))
                .unwrap()["groups"],
            json!([])
        );
    }
    #[test]
    fn reader_sequence_and_expiry_survive_reopen() {
        let (fixture, library) = writable();
        assert_eq!(
            library
                .reader(&json!({"id":"reader","book_id":"book","index":0,"sequence":3}))
                .unwrap(),
            json!({"active":true})
        );
        assert_eq!(
            library
                .reader(&json!({"id":"reader","sequence":2}))
                .unwrap(),
            json!({"active":true})
        );
        let db = library.reader_connection().unwrap();
        assert_eq!(
            db.query_row(
                "SELECT sequence FROM chapter_readers WHERE id='reader'",
                [],
                |r| r.get::<_, i64>(0)
            )
            .unwrap(),
            3
        );
        drop(db);
        drop(library);
        let library = Library::open_writable_fixture(&fixture.db).unwrap();
        assert_eq!(
            library
                .reader(&json!({"id":"reader","sequence":2}))
                .unwrap(),
            json!({"active":true})
        );
        assert_eq!(
            library
                .reader(&json!({"id":"reader","sequence":4}))
                .unwrap(),
            json!({"active":false})
        );
        assert!(
            library
                .reader(&json!({"id":"bad","book_id":"book","index":true}))
                .is_err()
        );
        assert!(
            library
                .reader(&json!({"id":"bad","sequence":true}))
                .is_err()
        );
    }
    #[test]
    fn scoped_writes_preserve_orphan_notes_and_protected_bodies() {
        let (_fixture, library) = writable();
        library
            .settings(Some(
                &json!({"appearance":{"size":24},"array":[true,1,null]}),
            ))
            .unwrap();
        assert_eq!(
            library.settings(None).unwrap()["array"],
            json!([true, 1, null])
        );
        let note = library
            .save_note(&json!({"id":"orphan","book_id":"missing","position":0.3,"quote":"Việt"}))
            .unwrap();
        library
            .save_note(&json!({"id":"orphan","book_id":"book","position":0.7,"note":"updated"}))
            .unwrap();
        let notes = library.notes(Some("missing")).unwrap();
        assert_eq!(notes[0]["created_at"], note["created_at"]);
        assert_eq!(notes[0]["position"], 0.7);
        assert_eq!(
            library.chapter("book", 0, None, 0).unwrap()["text"],
            "Tiếng Việt"
        );
        let db = library.reader_connection().unwrap();
        assert_eq!(
            db.query_row(
                "SELECT kind FROM chapter_cache WHERE book_id='book' AND idx=0",
                [],
                |r| r.get::<_, String>(0)
            )
            .unwrap(),
            "protected"
        );
        drop(db);
        library
            .update_book(
                "book",
                &json!({"groups":["one",-1,"two","one"],"archived":true}),
            )
            .unwrap();
        assert_eq!(
            library.get_book("book").unwrap()["groups"],
            json!(["one", "two", "one"])
        );
        assert!(
            library
                .progress("book", &json!({"index":0,"position":2}))
                .is_err()
        );
        assert_eq!(
            library
                .progress("book", &json!({"index":0,"position":0.5}))
                .unwrap()["progress"],
            0.25
        );
        assert_eq!(
            library
                .notes(Some("missing"))
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            1
        );
    }
}
