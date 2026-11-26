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
        // One statement: a new note needs its book to exist; updating an existing (even orphan) note does not.
        let written = db.execute("INSERT INTO notes SELECT ?1,?2,?3,?4,?5,?6,?7,?8,?9,?10 WHERE EXISTS(SELECT 1 FROM notes WHERE id=?1) OR EXISTS(SELECT 1 FROM books WHERE id=?2) ON CONFLICT(id) DO UPDATE SET quote=excluded.quote,note=excluded.note,color=excluded.color,position=excluded.position",params![id,book,index,get("quote",""),get("note",""),get("color",""),position,get("type","note"),now(),json_store(body)?])?;
        if written == 0 {
            return Err(ApiError::new(404, "Không tìm thấy truyện."));
        }
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
        let was_archived: bool =
            tx.query_row("SELECT archived FROM books WHERE id=?", [id], |r| r.get(0))?;
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
        if was_archived {
            // Putting an off-shelf novel on the shelf makes "Mới thêm" mean the moment it was added.
            tx.execute(
                "UPDATE books SET added_at=? WHERE id=? AND archived=0",
                params![now(), id],
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
    /// The last step of a shutdown: copy the whole WAL into the database and empty it.
    /// Bounded by attempts *and* by total time (each attempt may also wait on busy_timeout), so
    /// readers that never let go only cost a few seconds. `false` is not data loss: committed
    /// transactions stay in the WAL and SQLite applies them at the next open.
    pub fn final_checkpoint(&self) -> bool {
        const ATTEMPTS: u32 = 5;
        const BUDGET: std::time::Duration = std::time::Duration::from_secs(8);
        let deadline = std::time::Instant::now() + BUDGET;
        let remaining = || deadline.saturating_duration_since(std::time::Instant::now());
        for attempt in 1..=ATTEMPTS {
            let wait = remaining().min(std::time::Duration::from_secs(1));
            let result = self.write_connection().and_then(|db| {
                db.busy_timeout(wait)?;
                Ok(db.query_row("PRAGMA wal_checkpoint(TRUNCATE)", [], |r| {
                    Ok((
                        r.get::<_, i64>(0)?,
                        r.get::<_, i64>(1)?,
                        r.get::<_, i64>(2)?,
                    ))
                })?)
            });
            let reason = match result {
                Ok((0, log, done)) if log == done => return true,
                Ok((busy, log, done)) => format!("busy={busy}, log={log}, checkpointed={done}"),
                Err(error) => error.message,
            };
            eprintln!("final checkpoint attempt {attempt}: {reason}");
            if attempt == ATTEMPTS || remaining().is_zero() {
                break;
            }
            std::thread::sleep(remaining().min(std::time::Duration::from_secs(1)));
        }
        false
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
    pub(crate) fn online() -> (common::Fixture, Library) {
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
    fn downloads(library: &Library) -> usize {
        library
            .sources
            .image_fetches
            .load(std::sync::atomic::Ordering::Acquire)
    }
    fn slow_images(library: &Library, ms: u64) {
        *library.sources.image_delay.lock().unwrap() = std::time::Duration::from_millis(ms);
    }
    #[test]
    fn overlapping_requests_for_one_image_download_it_once() {
        let (_fixture, library) = online();
        slow_images(&library, 300);
        let url = illustration(0);
        let (first, second) = std::thread::scope(|scope| {
            let first = scope.spawn(|| library.save_source_image(&url, ONLINE_BOOK, None));
            std::thread::sleep(std::time::Duration::from_millis(50));
            let second = scope.spawn(|| library.save_source_image(&url, ONLINE_BOOK, None));
            (first.join().unwrap(), second.join().unwrap())
        });
        assert!(first.is_ok() && second.is_ok());
        assert_eq!(
            downloads(&library),
            1,
            "the second request reused the first download"
        );
        assert!(cached_image(&library, &url));
    }
    #[test]
    fn a_reader_request_during_a_prefetch_image_download_reuses_it() {
        let (_fixture, library) = online();
        library.prefetch.claim("web", 1);
        slow_images(&library, 150);
        std::thread::scope(|scope| {
            let prefetch = scope.spawn(|| library.prefetch_chapter("web", 2, &|| false).unwrap());
            // The prefetch is downloading the chapter's first image; the reader asks for it too.
            while downloads(&library) == 0 {
                std::thread::yield_now();
            }
            library
                .save_source_image(&illustration(0), ONLINE_BOOK, None)
                .unwrap();
            assert!(cached_image(&library, &illustration(0)));
            prefetch.join().unwrap();
        });
        assert_eq!(
            downloads(&library),
            10,
            "ten prefetched images, none downloaded twice"
        );
    }
    #[test]
    fn with_the_image_cache_full_waiters_get_the_shared_bytes() {
        let (_fixture, library) = online();
        library
            .write_connection()
            .unwrap()
            .execute("UPDATE image_cache_state SET max_bytes=1 WHERE id=1", [])
            .unwrap();
        slow_images(&library, 300);
        let url = illustration(0);
        let (first, second) = std::thread::scope(|scope| {
            let first = scope.spawn(|| library.save_source_image(&url, ONLINE_BOOK, None));
            std::thread::sleep(std::time::Duration::from_millis(50));
            let second = scope.spawn(|| library.save_source_image(&url, ONLINE_BOOK, None));
            (
                first.join().unwrap().unwrap(),
                second.join().unwrap().unwrap(),
            )
        });
        assert!(
            first.is_some() && first == second,
            "both get the image bytes"
        );
        assert!(!cached_image(&library, &url), "not kept: over the budget");
        assert_eq!(downloads(&library), 1);
    }
    #[test]
    fn a_failed_shared_download_is_retried_by_the_waiting_request() {
        let (_fixture, library) = online();
        slow_images(&library, 200);
        let missing = "https://i.hako.vip/illusts/missing.png";
        let (first, second) = std::thread::scope(|scope| {
            let first = scope.spawn(|| library.save_source_image(missing, ONLINE_BOOK, None));
            std::thread::sleep(std::time::Duration::from_millis(50));
            let second = scope.spawn(|| library.save_source_image(missing, ONLINE_BOOK, None));
            (first.join().unwrap(), second.join().unwrap())
        });
        assert!(first.is_err() && second.is_err());
        assert_eq!(
            downloads(&library),
            2,
            "the waiter tried again instead of inheriting the failure"
        );
    }
    #[test]
    fn after_a_failed_shared_download_waiters_retry_in_parallel() {
        let (_fixture, library) = online();
        slow_images(&library, 300);
        let missing = "https://i.hako.vip/illusts/missing.png";
        let started = std::time::Instant::now();
        std::thread::scope(|scope| {
            let leader = scope.spawn(|| library.save_source_image(missing, ONLINE_BOOK, None));
            std::thread::sleep(std::time::Duration::from_millis(50));
            let waiters: Vec<_> = (0..2)
                .map(|_| scope.spawn(|| library.save_source_image(missing, ONLINE_BOOK, None)))
                .collect();
            assert!(leader.join().unwrap().is_err());
            for waiter in waiters {
                assert!(waiter.join().unwrap().is_err());
            }
        });
        assert_eq!(downloads(&library), 3, "one retry per waiter");
        // One failed download, then both retries at once: about 600 ms, not 900 ms in turn.
        assert!(
            started.elapsed() < std::time::Duration::from_millis(800),
            "{:?}",
            started.elapsed()
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
    const SOURCE_BOOK: &str = "https://truyenfull.live/truyen-b/";
    fn source_detail(path: &str) -> Value {
        json!({"name":"Truyện nguồn","author":"Tác giả","source":"full","path":path,"cover":"",
            "kind":"text","format":"online","description":"","chapters":[
                {"title":"Chương 1","url":format!("{SOURCE_BOOK}chuong-1/")},
                {"title":"Chương 2","url":format!("{SOURCE_BOOK}chuong-2/")}]})
    }
    fn book_count(library: &Library) -> i64 {
        library
            .reader_connection()
            .unwrap()
            .query_row("SELECT count(*) FROM books", [], |r| r.get(0))
            .unwrap()
    }
    fn set_read_at(library: &Library, id: &str, at: f64) {
        library
            .write_connection()
            .unwrap()
            .execute("UPDATE books SET read_at=? WHERE id=?", params![at, id])
            .unwrap();
    }
    #[test]
    fn off_shelf_read_then_add_keeps_progress_and_sorts_first() {
        let (_fixture, library) = writable();
        let book = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap();
        assert_eq!(book["archived"], true);
        let id = book["id"].as_str().unwrap().to_owned();
        library
            .progress(&id, &json!({"index":1,"position":0.4}))
            .unwrap();
        set_read_at(&library, "book", 1.0);
        let before = library.get_book(&id).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        let added = library
            .add_source_book(source_detail(SOURCE_BOOK), true)
            .unwrap();
        assert_eq!(added["id"], id.as_str());
        assert_eq!(added["archived"], false);
        assert_eq!(added["chapter_index"], 1);
        assert_eq!(added["position"], 0.4);
        assert_eq!(added["read_at"], before["read_at"]);
        assert!(added["added_at"].as_f64().unwrap() > before["added_at"].as_f64().unwrap());
        let page = library
            .list_books(&crate::library::ShelfQuery::from_uri("/api/books?sort=recent").unwrap())
            .unwrap();
        assert_eq!(page.items[0]["id"], id.as_str());
    }
    #[test]
    fn local_add_to_shelf_keeps_progress_and_sets_added_at() {
        let (_fixture, library) = writable();
        let id = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap()["id"]
            .as_str()
            .unwrap()
            .to_owned();
        library
            .progress(&id, &json!({"index":1,"position":0.4}))
            .unwrap();
        let before = library.get_book(&id).unwrap();
        std::thread::sleep(std::time::Duration::from_millis(20));
        let added = library
            .update_book(&id, &json!({"archived":false}))
            .unwrap();
        assert_eq!(added["archived"], false);
        assert_eq!(added["chapter_index"], 1);
        assert_eq!(added["position"], 0.4);
        assert_eq!(added["read_at"], before["read_at"]);
        assert!(added["added_at"].as_f64().unwrap() > before["added_at"].as_f64().unwrap());
        let again = library
            .update_book(&id, &json!({"archived":false}))
            .unwrap();
        assert_eq!(again["added_at"], added["added_at"]);
    }
    #[test]
    fn opening_a_shelf_book_from_discover_keeps_it_on_the_shelf() {
        let (_fixture, library) = writable();
        library
            .add_source_book(source_detail(SOURCE_BOOK), true)
            .unwrap();
        let again = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap();
        assert_eq!(again["archived"], false);
    }
    #[test]
    fn discover_finds_the_same_record_under_every_url_spelling() {
        let (_fixture, library) = writable();
        let id = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap()["id"]
            .clone();
        let count = book_count(&library);
        for path in [
            "https://truyenfull.vision/truyen-b/",
            "https://truyenfull.live/truyen-b.html",
            "https://truyenfull.live/truyen-b",
            "https://truyenfull.live/truyen-b/?ref=1",
        ] {
            let again = library.add_source_book(source_detail(path), false).unwrap();
            assert_eq!(again["id"], id, "{path}");
            assert_eq!(book_count(&library), count, "{path}");
        }
    }
    #[test]
    fn adding_from_discover_never_untrashes() {
        let (_fixture, library) = writable();
        let id = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap()["id"]
            .as_str()
            .unwrap()
            .to_owned();
        library.trash_book(&id).unwrap();
        let again = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap();
        assert!(again["trashed_at"].as_f64().unwrap() > 0.0);
        assert_eq!(again["archived"], true);
    }
    #[test]
    fn source_book_state_reports_shelf_notes_and_downloads() {
        let (_fixture, library) = writable();
        assert!(
            library
                .source_book_state("full", SOURCE_BOOK)
                .unwrap()
                .is_null()
        );
        let id = library
            .add_source_book(source_detail(SOURCE_BOOK), false)
            .unwrap()["id"]
            .as_str()
            .unwrap()
            .to_owned();
        let db = library.write_connection().unwrap();
        db.execute(
            "INSERT INTO notes(id,book_id,created_at) VALUES('n',?,1)",
            [&id],
        )
        .unwrap();
        db.execute(
            "UPDATE chapters SET text='Đã tải',downloaded=1 WHERE book_id=? AND idx=0",
            [&id],
        )
        .unwrap();
        db.execute(
            "UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id=? AND idx=0",
            [&id],
        )
        .unwrap();
        drop(db);
        let state = library
            .source_book_state("full", "https://truyenfull.vision/truyen-b")
            .unwrap();
        assert_eq!(state["id"], id.as_str());
        assert_eq!(state["on_shelf"], false);
        assert_eq!(state["trashed"], false);
        assert_eq!(state["has_notes"], true);
        assert_eq!(state["downloaded"], 1);
    }
    fn history(library: &Library, since: &str) -> Result<Vec<String>, ApiError> {
        let query = crate::library::ShelfQuery::from_uri(&format!(
            "/api/books?archived=1&sort=recent&read_since={since}"
        ))?;
        Ok(library
            .list_books(&query)?
            .items
            .iter()
            .map(|b| b["id"].as_str().unwrap().to_owned())
            .collect())
    }
    #[test]
    fn history_lists_recent_off_shelf_reads_only() {
        let (_fixture, library) = writable();
        let day = 86400.0;
        let t = now();
        let db = library.write_connection().unwrap();
        for (id, archived, read_at) in [
            ("recent", 1, t - 5.0 * day),
            ("old", 1, t - 30.5 * day),
            ("unread", 1, 0.0),
            ("shelf", 0, t - day),
        ] {
            db.execute(
                "INSERT INTO books(id,name,archived,read_at,added_at,updated_at) VALUES(?,?,?,?,1,1)",
                params![id, id, archived, read_at],
            )
            .unwrap();
        }
        drop(db);
        assert_eq!(
            history(&library, &format!("{}", t - 30.0 * day)).unwrap(),
            ["recent"]
        );
    }
    #[test]
    fn history_rejects_a_bad_read_since() {
        let (_fixture, library) = writable();
        for bad in ["abc", "-1", "inf", "NaN"] {
            assert_eq!(history(&library, bad).unwrap_err().status, 400, "{bad}");
        }
    }
    #[test]
    fn a_new_note_needs_its_book() {
        let (_fixture, library) = writable();
        let error = library
            .save_note(&json!({"id":"n1","book_id":"missing"}))
            .unwrap_err();
        assert_eq!(error.status, 404);
        assert!(library.notes(Some("missing")).unwrap()[0].is_null());
    }
    // ---- Downloads section: the downloaded list and deleting a book's downloads ----
    /// `web` gets chapter 1 offline (images 0 and 1, both pinned), chapter 2 legacy and an edited,
    /// downloaded chapter 3; shelf book `keep` has an offline chapter that also pins image 1.
    fn downloaded_fixture() -> (common::Fixture, Library) {
        let (fixture, library) = online();
        for k in 0..2 {
            library
                .save_source_image(&illustration(k), ONLINE_BOOK, None)
                .unwrap();
        }
        let (zero, one) = (illustration(0), illustration(1));
        let (n0, n1) = (image_name(&zero), image_name(&one));
        exec(&library, &format!(
            "UPDATE chapters SET downloaded=0 WHERE book_id='web' AND idx=0;
             UPDATE chapters SET html='<p>1</p>',images='[\"{zero}\",\"{one}\"]',downloaded=1 WHERE book_id='web' AND idx=1;
             UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id='web' AND idx=1;
             UPDATE chapters SET text='2',downloaded=1 WHERE book_id='web' AND idx=2;
             UPDATE chapters SET html='<p>Đã sửa</p>',original_html='<p>gốc</p>',downloaded=1 WHERE book_id='web' AND idx=3;
             INSERT INTO books(id,name,source,path,chapter_count,added_at,updated_at) VALUES('keep','Giữ','full','https://truyenfull.live/giu/',1,1,1);
             INSERT INTO chapters(book_id,idx,title,url,html,images,downloaded) VALUES('keep',0,'C','https://truyenfull.live/giu/c/','','[\"{one}\"]',1);
             UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id='keep';"
        ));
        drain(|s| library.index_assets(s));
        exec(
            &library,
            &format!(
                "INSERT OR IGNORE INTO image_pins VALUES('web',1,'{n0}'),('web',1,'{n1}'),('keep',0,'{n1}')"
            ),
        );
        (fixture, library)
    }
    #[test]
    fn downloaded_books_lists_shelf_and_off_shelf_downloads_with_counts() {
        let (_fixture, library) = downloaded_fixture();
        exec(&library, "UPDATE books SET archived=1 WHERE id='web';
            INSERT INTO books(id,name,source,path,chapter_count,added_at,updated_at) VALUES('tmp','Tạm','full','https://truyenfull.live/tam/',1,1,1);
            INSERT INTO chapters(book_id,idx,title,url,text,downloaded) VALUES('tmp',0,'C','https://truyenfull.live/tam/c/','x',1);
            UPDATE chapter_cache SET kind='cache' WHERE book_id='tmp';
            INSERT INTO books(id,name,source,path,chapter_count,trashed_at,added_at,updated_at) VALUES('bin','Rác','full','https://truyenfull.live/rac/',1,5,1,1);
            INSERT INTO chapters(book_id,idx,title,url,text,downloaded) VALUES('bin',0,'C','https://truyenfull.live/rac/c/','x',1);");
        let page = library.downloaded_books(0).unwrap();
        let mut found: Vec<(String, i64)> = page["items"]
            .as_array()
            .unwrap()
            .iter()
            .map(|b| {
                (
                    b["id"].as_str().unwrap().to_owned(),
                    b["downloaded_count"].as_i64().unwrap(),
                )
            })
            .collect();
        found.sort();
        assert_eq!(
            found,
            [("keep".to_owned(), 1), ("web".to_owned(), 2)],
            "{page}"
        );
    }
    #[test]
    fn clearing_downloads_counts_old_app_images_the_gc_will_delete() {
        let (_fixture, library) = downloaded_fixture();
        let n0 = image_name(&illustration(0));
        exec(
            &library,
            &format!("UPDATE image_cache SET legacy=1 WHERE name='{n0}'"),
        );
        let private = count(
            &library,
            &format!("SELECT size FROM image_cache WHERE name='{n0}'"),
        );
        assert!(private > 0);
        let result = library.clear_book_downloads("web", true).unwrap();
        assert_eq!(result["pending_file_bytes"], private, "{result}");
        drain(|s| library.index_assets(s));
        drain(|s| library.cleanup_assets(s));
        assert!(
            !cached_image(&library, &illustration(0)),
            "the GC deletes it, so the reply must count it"
        );
    }
    #[test]
    fn clearing_a_books_downloads_frees_their_content_and_image_files() {
        let (_fixture, library) = downloaded_fixture();
        let size = |name: &str| {
            count(
                &library,
                &format!("SELECT coalesce(sum(size),0) FROM image_cache WHERE name='{name}'"),
            )
        };
        let (n0, n1) = (image_name(&illustration(0)), image_name(&illustration(1)));
        let private = size(&n0);
        assert!(private > 0);
        let refused = library.clear_book_downloads("web", false).unwrap_err();
        assert_eq!(refused.status, 400);
        assert_eq!(
            count(
                &library,
                "SELECT count(*) FROM chapters WHERE book_id='web' AND downloaded=1"
            ),
            3
        );
        let result = library.clear_book_downloads("web", true).unwrap();
        assert_eq!(result["cleared"], 2, "{result}");
        assert_eq!(
            result["kept"], 0,
            "chapter 3 was edited, not downloaded: {result}"
        );
        // A chapter downloaded and then edited stays downloaded, and is reported as kept.
        exec(
            &library,
            "UPDATE chapter_cache SET offline=1 WHERE book_id='web' AND idx=3",
        );
        let again = library.clear_book_downloads("web", true).unwrap();
        assert_eq!(
            (&again["cleared"], &again["kept"]),
            (&json!(0), &json!(1)),
            "{again}"
        );
        assert_eq!(
            result["freed_bytes"], 0,
            "files are deleted later, in the background"
        );
        assert_eq!(
            result["pending_file_bytes"], private,
            "only the image no other book uses"
        );
        assert!(result["sqlite_reusable_bytes"].as_i64().unwrap() > 0);
        for idx in [1, 2] {
            let row = chapter_value(library.raw_chapter("web", idx).unwrap()).unwrap();
            assert_eq!(
                (
                    row["html"].as_str(),
                    row["text"].as_str(),
                    row["downloaded"].as_bool()
                ),
                (Some(""), Some(""), Some(false)),
                "{idx}"
            );
            assert_eq!(
                count(
                    &library,
                    &format!(
                        "SELECT count(*) FROM chapter_cache WHERE book_id='web' AND idx={idx}"
                    )
                ),
                0
            );
        }
        assert_eq!(cache_kind(&library, 3).unwrap().0, "protected");
        drain(|s| library.index_assets(s));
        drain(|s| library.cleanup_assets(s));
        assert!(
            !cached_image(&library, &illustration(0)),
            "the private image file is gone"
        );
        assert_eq!(
            count(
                &library,
                &format!("SELECT count(*) FROM image_cache WHERE name='{n0}'")
            ),
            0
        );
        assert!(
            cached_image(&library, &illustration(1)),
            "the shared image stays"
        );
        assert!(
            count(
                &library,
                &format!("SELECT pin_count FROM image_cache WHERE name='{n1}'")
            ) > 0
        );
        assert_eq!(
            count(
                &library,
                "SELECT count(*) FROM chapter_cache WHERE book_id='keep' AND offline=1"
            ),
            1
        );
        assert_eq!(
            count(&library, "SELECT count(*) FROM pragma_foreign_key_check"),
            0
        );
    }
    #[test]
    fn clearing_downloads_is_refused_while_in_use() {
        let (_fixture, library) = downloaded_fixture();
        let downloaded = "SELECT count(*) FROM chapters WHERE book_id='web' AND downloaded=1";
        let lease = library.chapter_leases.acquire(("web".into(), 1));
        assert_eq!(
            library
                .clear_book_downloads("web", true)
                .unwrap_err()
                .status,
            400
        );
        drop(lease);
        exec(
            &library,
            &format!(
                "INSERT INTO chapter_readers(id,book_id,idx,sequence,expires) VALUES('r','web',0,1,{})",
                now() + 60.0
            ),
        );
        assert_eq!(
            library
                .clear_book_downloads("web", true)
                .unwrap_err()
                .status,
            400
        );
        exec(
            &library,
            "DELETE FROM chapter_readers; INSERT INTO download_jobs(id,book_id,status,data,created_at,updated_at) VALUES('j','web','running','{}',1,1)",
        );
        assert_eq!(
            library
                .clear_book_downloads("web", true)
                .unwrap_err()
                .status,
            400
        );
        assert_eq!(count(&library, downloaded), 3);
        assert_eq!(
            library
                .clear_book_downloads("missing", true)
                .unwrap_err()
                .status,
            404
        );
    }
    // ---- Off-shelf clean-up (History GC) ----
    use crate::chapters::{Rule, candidate_sql, next_candidate};
    use std::sync::atomic::{AtomicBool, Ordering};
    const DAY: f64 = 86400.0;
    fn exec(library: &Library, sql: &str) {
        library
            .write_connection()
            .unwrap()
            .execute_batch(sql)
            .unwrap();
    }
    fn drain(step: impl Fn(&AtomicBool) -> Result<usize, ApiError>) {
        let stop = AtomicBool::new(false);
        while step(&stop).unwrap() > 0 {}
    }
    /// Makes `web` an off-shelf book untouched for 32 days, with nothing saved.
    fn stale_off_shelf(library: &Library, id: &str) {
        let old = now() - 32.0 * DAY;
        let db = library.write_connection().unwrap();
        db.execute("UPDATE chapters SET downloaded=0 WHERE book_id=?", [id])
            .unwrap();
        db.execute(
            "UPDATE books SET archived=1,read_at=?,updated_at=? WHERE id=?",
            params![old, old, id],
        )
        .unwrap();
        drop(db);
        drain(|s| library.index_assets(s));
    }
    fn present(library: &Library, id: &str) -> bool {
        library.get_book(id).is_ok()
    }
    fn count(library: &Library, sql: &str) -> i64 {
        library
            .reader_connection()
            .unwrap()
            .query_row(sql, [], |r| r.get(0))
            .unwrap()
    }
    fn image_name(url: &str) -> String {
        use sha2::Digest;
        format!("{:x}", sha2::Sha256::digest(url.as_bytes()))
    }
    fn web_detail(path: &str) -> Value {
        json!({"name":"Truyện mạng","author":"","source":"full","path":path,"cover":"","kind":"text",
            "format":"online","description":"","chapters":(0..4).map(|n| json!({"title":format!("Chương {n}"),
            "url":format!("{ONLINE_BOOK}chuong-{n}/")})).collect::<Vec<_>>()})
    }
    /// An off-shelf record with nothing saved: a cache-only body and its search entry for chapter 1,
    /// progress in chapter 2.
    fn reorderable(library: &Library) {
        stale_off_shelf(library, "web");
        exec(
            library,
            "UPDATE books SET chapter_index=2,position=0.4,progress=0.6 WHERE id='web';
             UPDATE chapters SET text='nội dung cũ',downloaded=1 WHERE book_id='web' AND idx=1;
             UPDATE chapter_cache SET kind='cache',offline=0 WHERE book_id='web' AND idx=1;
             INSERT INTO chapter_search_docs(book_id,idx,scope,literal_check,text) VALUES('web',1,'s',0,'nội dung cũ');",
        );
    }
    fn source_order(order: &[i64]) -> Value {
        let mut detail = web_detail(ONLINE_BOOK);
        detail["chapters"] = order
            .iter()
            .map(|n| json!({"title":format!("Chương {n}"),"url":format!("{ONLINE_BOOK}chuong-{n}/")}))
            .collect();
        detail
    }
    fn urls(library: &Library) -> Vec<String> {
        let db = library.reader_connection().unwrap();
        let mut stmt = db
            .prepare("SELECT url FROM chapters WHERE book_id='web' ORDER BY idx")
            .unwrap();
        stmt.query_map([], |r| r.get(0))
            .unwrap()
            .collect::<Result<_, _>>()
            .unwrap()
    }
    #[test]
    fn an_off_shelf_book_with_nothing_saved_takes_a_reordered_source_list() {
        let (_fixture, library) = online();
        reorderable(&library);
        let book = library
            .add_source_book(source_order(&[3, 2, 1]), false)
            .unwrap();
        let url = |n: i64| format!("{ONLINE_BOOK}chuong-{n}/");
        assert_eq!(urls(&library), [url(3), url(2), url(1)]);
        assert_eq!(
            (
                &book["chapter_index"],
                &book["position"],
                &book["chapter_count"],
                &book["archived"]
            ),
            (&json!(1), &json!(0.4), &json!(3), &json!(true)),
            "progress follows chapter 2 to its new place: {book}"
        );
        assert!(
            (book["progress"].as_f64().unwrap() - 1.4 / 3.0).abs() < 1e-9,
            "{book}"
        );
        for (sql, what) in [
            (
                "SELECT count(*) FROM chapters WHERE book_id='web' AND (text!='' OR html!='')",
                "stale bodies",
            ),
            (
                "SELECT count(*) FROM chapter_search_docs WHERE book_id='web'",
                "search documents",
            ),
            (
                "SELECT count(*) FROM chapter_search_fts WHERE chapter_search_fts MATCH 'dung'",
                "FTS entries",
            ),
        ] {
            assert_eq!(count(&library, sql), 0, "{what}");
        }
    }
    #[test]
    fn a_reorder_that_drops_the_current_chapter_starts_from_the_beginning() {
        let (_fixture, library) = online();
        reorderable(&library);
        let book = library
            .add_source_book(source_order(&[3, 1, 0]), false)
            .unwrap();
        assert_eq!(
            (&book["chapter_index"], &book["position"], &book["progress"]),
            (&json!(0), &json!(0.0), &json!(0.0)),
            "{book}"
        );
    }
    #[test]
    fn a_reordered_source_list_is_refused_while_the_book_holds_saved_data() {
        let (_fixture, library) = online();
        reorderable(&library);
        let before = urls(&library);
        exec(
            &library,
            "INSERT INTO notes(id,book_id,created_at) VALUES('n','web',1)",
        );
        let error = library
            .add_source_book(source_order(&[3, 2, 1]), false)
            .unwrap_err();
        assert_eq!(error.status, 400);
        assert_eq!(urls(&library), before);
    }
    #[test]
    fn an_empty_source_list_never_replaces_the_chapters() {
        let (_fixture, library) = online();
        reorderable(&library);
        let before = urls(&library);
        let error = library
            .add_source_book(source_order(&[]), false)
            .unwrap_err();
        assert_eq!(error.status, 400);
        assert_eq!(urls(&library), before);
    }
    #[test]
    fn a_reordered_source_list_is_refused_while_a_chapter_is_leased() {
        let (_fixture, library) = online();
        reorderable(&library);
        let before = urls(&library);
        let lease = library.chapter_leases.acquire(("web".into(), 1));
        let error = library
            .add_source_book(source_order(&[3, 2, 1]), false)
            .unwrap_err();
        assert_eq!(error.status, 400);
        assert_eq!(urls(&library), before);
        drop(lease);
        assert!(
            library
                .add_source_book(source_order(&[3, 2, 1]), false)
                .is_ok()
        );
    }
    #[test]
    fn off_shelf_gc_uses_the_updated_index() {
        let (_fixture, library) = online();
        let db = library.reader_connection().unwrap();
        let plan = db
            .prepare(&format!(
                "EXPLAIN QUERY PLAN {}",
                candidate_sql(Rule::OffShelf)
            ))
            .unwrap()
            .query_map(params![now(), now(), "[]"], |r| r.get::<_, String>(3))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert!(
            plan.iter()
                .any(|l| l.starts_with("SEARCH b") && l.contains("books_shelf_updated")),
            "{plan:?}"
        );
        assert!(!plan.iter().any(|l| l.starts_with("SCAN b")), "{plan:?}");
    }
    #[test]
    fn off_shelf_gc_purges_a_cache_only_book_and_queues_its_assets() {
        let (_fixture, library) = online();
        library.prefetch_chapter("web", 2, &|| false).unwrap();
        stale_off_shelf(&library, "web");
        let refs = "SELECT count(*) FROM asset_refs WHERE book_id='web'";
        assert!(count(&library, refs) > 0);
        let queued = count(&library, "SELECT count(*) FROM asset_gc_candidates");
        assert!(library.purge_expired_book().unwrap());
        assert!(!present(&library, "web"));
        assert_eq!(count(&library, refs), 0);
        assert!(count(&library, "SELECT count(*) FROM asset_gc_candidates") > queued);
        assert_eq!(
            count(&library, "SELECT count(*) FROM pragma_foreign_key_check"),
            0
        );
    }
    /// Each keep condition blocks the purge, and removing it lets the purge happen.
    fn keeps(setup: &str, undo: &str) {
        let (_fixture, library) = online();
        stale_off_shelf(&library, "web");
        exec(&library, setup);
        assert!(!library.purge_expired_book().unwrap(), "{setup}");
        assert!(present(&library, "web"), "{setup}");
        exec(&library, undo);
        assert!(library.purge_expired_book().unwrap(), "{undo}");
        assert!(!present(&library, "web"), "{undo}");
    }
    #[test]
    fn off_shelf_gc_keeps_a_book_with_its_own_dictionary_and_moves_past_it() {
        let (_fixture, library) = online();
        stale_off_shelf(&library, "web");
        let qt = library.qt().unwrap();
        qt.save("rules", "a", "b", "web").unwrap();
        exec(
            &library,
            "INSERT INTO books(id,name,source,path,chapter_count,archived,added_at,updated_at,read_at)
             VALUES('bare','Trống','full','https://truyenfull.live/trong/',0,1,1,1,1)",
        );
        assert!(library.purge_expired_book().unwrap());
        assert!(
            !present(&library, "bare"),
            "the record behind it is still cleaned"
        );
        assert!(!library.purge_expired_book().unwrap());
        assert!(present(&library, "web"), "its dictionary entries keep it");
        qt.delete("rules", "a", "web").unwrap();
        assert!(library.purge_expired_book().unwrap());
        assert!(!present(&library, "web"));
    }
    const UNDOWNLOAD: &str = "UPDATE chapters SET downloaded=0 WHERE book_id='web' AND idx=1";
    #[test]
    fn off_shelf_gc_keeps_an_offline_chapter() {
        keeps(
            "UPDATE chapters SET text='x',downloaded=1 WHERE book_id='web' AND idx=1;
             UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id='web' AND idx=1",
            UNDOWNLOAD,
        );
    }
    #[test]
    fn off_shelf_gc_keeps_a_legacy_chapter() {
        keeps(
            "UPDATE chapters SET text='x',downloaded=1 WHERE book_id='web' AND idx=1",
            UNDOWNLOAD,
        );
    }
    #[test]
    fn off_shelf_gc_keeps_a_download_whose_cache_inventory_is_not_built_yet() {
        keeps(
            "UPDATE chapters SET text='x',downloaded=1 WHERE book_id='web' AND idx=1;
             DELETE FROM chapter_cache WHERE book_id='web' AND idx=1",
            UNDOWNLOAD,
        );
    }
    #[test]
    fn off_shelf_gc_keeps_an_edited_chapter() {
        keeps(
            "UPDATE chapters SET original_html='<p>gốc</p>' WHERE book_id='web' AND idx=2",
            "UPDATE chapters SET original_html=NULL WHERE book_id='web' AND idx=2",
        );
    }
    #[test]
    fn off_shelf_gc_keeps_a_note() {
        keeps(
            "INSERT INTO notes(id,book_id,created_at) VALUES('n','web',1)",
            "DELETE FROM notes",
        );
    }
    #[test]
    fn off_shelf_gc_keeps_every_active_download_job() {
        for status in ["queued", "running", "paused", "verification_required"] {
            keeps(
                &format!(
                    "INSERT INTO download_jobs(id,book_id,status,data,created_at,updated_at) VALUES('j','web','{status}','{{}}',1,1)"
                ),
                "DELETE FROM download_jobs",
            );
        }
    }
    #[test]
    fn off_shelf_gc_keeps_an_open_reader() {
        keeps(
            &format!(
                "INSERT INTO chapter_readers(id,book_id,idx,sequence,expires) VALUES('r','web',0,1,{})",
                now() + 60.0
            ),
            "DELETE FROM chapter_readers",
        );
    }
    #[test]
    fn off_shelf_gc_keeps_a_book_hidden_from_history_but_inside_the_grace_day() {
        keeps(
            &format!(
                "UPDATE books SET read_at={} WHERE id='web'",
                now() - 30.5 * DAY
            ),
            &format!(
                "UPDATE books SET read_at={} WHERE id='web'",
                now() - 32.0 * DAY
            ),
        );
    }
    #[test]
    fn off_shelf_gc_keeps_a_recently_updated_book() {
        keeps(
            &format!("UPDATE books SET updated_at={} WHERE id='web'", now()),
            &format!(
                "UPDATE books SET updated_at={} WHERE id='web'",
                now() - 32.0 * DAY
            ),
        );
    }
    #[test]
    fn off_shelf_gc_keeps_shelf_books_other_sources_and_grouped_books() {
        keeps(
            "UPDATE books SET archived=0 WHERE id='web'",
            "UPDATE books SET archived=1 WHERE id='web'",
        );
        keeps(
            "UPDATE books SET source='blog' WHERE id='web'",
            "UPDATE books SET source='full' WHERE id='web'",
        );
        keeps(
            r#"UPDATE books SET groups_json='["g"]' WHERE id='web'"#,
            "UPDATE books SET groups_json='[]' WHERE id='web'",
        );
    }
    #[test]
    fn off_shelf_gc_keeps_a_book_with_a_held_chapter_lease() {
        let (_fixture, library) = online();
        stale_off_shelf(&library, "web");
        let lease = library.chapter_leases.acquire(("web".into(), 1));
        assert!(!library.purge_expired_book().unwrap());
        assert!(present(&library, "web"));
        drop(lease);
        assert!(library.purge_expired_book().unwrap());
        assert!(!present(&library, "web"));
    }
    #[test]
    fn a_change_between_selection_and_purge_keeps_the_book() {
        let changes: [&dyn Fn(&Library); 5] = [
            &|l| {
                l.save_note(&json!({"id":"n","book_id":"web"})).unwrap();
            },
            &|l| {
                l.progress("web", &json!({"index":1,"position":0.2}))
                    .unwrap();
            },
            &|l| {
                exec(
                    l,
                    "INSERT INTO download_jobs(id,book_id,status,data,created_at,updated_at) VALUES('j','web','queued','{}',1,1)",
                )
            },
            &|l| {
                l.add_source_book(web_detail(ONLINE_BOOK), true).unwrap();
            },
            &|l| {
                exec(
                    l,
                    "UPDATE chapters SET original_html='<p>gốc</p>' WHERE book_id='web' AND idx=3",
                )
            },
        ];
        for (n, change) in changes.iter().enumerate() {
            let (_fixture, library) = online();
            stale_off_shelf(&library, "web");
            let candidate =
                next_candidate(&library.reader_connection().unwrap(), now(), "[]").unwrap();
            assert!(
                matches!(candidate, Some((ref id, Rule::OffShelf)) if id == "web"),
                "{n}"
            );
            change(&library);
            assert!(
                !library.purge_if_eligible("web", Rule::OffShelf).unwrap(),
                "change {n}"
            );
            assert!(present(&library, "web"), "change {n}");
        }
    }
    #[test]
    fn a_lease_taken_during_the_purge_waits_and_then_sees_the_book_gone() {
        let (_fixture, library) = online();
        stale_off_shelf(&library, "web");
        let (signal, wait) = std::sync::mpsc::channel::<()>();
        let acquired = std::sync::Arc::new(AtomicBool::new(false));
        let seen = acquired.clone();
        *library.purge_hook.lock().unwrap() = Some(Box::new(move || {
            signal.send(()).unwrap();
            std::thread::sleep(std::time::Duration::from_millis(100));
            assert!(
                !seen.load(Ordering::Acquire),
                "the lease waited for the purge"
            );
        }));
        let (shared, flag) = (&library, &acquired);
        std::thread::scope(|scope| {
            let helper = scope.spawn(move || {
                wait.recv().unwrap();
                let _lease = shared.chapter_leases.acquire(("web".into(), 1));
                flag.store(true, Ordering::Release);
                shared.get_book("web").is_err()
            });
            assert!(library.purge_if_eligible("web", Rule::OffShelf).unwrap());
            assert!(helper.join().unwrap(), "the late lease found the book gone");
        });
        assert_eq!(library.chapter("web", 1, None, 0).unwrap_err().status, 404);
    }
    #[test]
    fn a_download_blocked_mid_fetch_blocks_gc_and_then_protects_the_book() {
        let (_fixture, library) = online();
        let library = std::sync::Arc::new(library);
        stale_off_shelf(&library, "web");
        let before = library.get_book("web").unwrap();
        slow_images(&library, 150);
        let owner = library.clone();
        let jobs = crate::downloads::Downloads::open(library.clone(), move |job, control| {
            owner.download_source_chapter(job, control)
        })
        .unwrap();
        jobs.start("web", 2, 1).unwrap();
        let status = || {
            jobs.list(0).unwrap()["jobs"][0]["status"]
                .as_str()
                .unwrap()
                .to_owned()
        };
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        while downloads(&library) == 0 {
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        assert_eq!(status(), "running");
        assert!(!library.purge_expired_book().unwrap(), "mid-fetch");
        while status() != "completed" {
            assert!(std::time::Instant::now() < deadline, "{}", status());
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        assert!(
            !library.purge_expired_book().unwrap(),
            "downloaded content is protected"
        );
        let after = library.get_book("web").unwrap();
        assert_eq!(after["read_at"], before["read_at"]);
        assert_eq!(after["updated_at"], before["updated_at"]);
        jobs.join();
    }
    #[test]
    fn background_work_never_touches_read_at_or_updated_at() {
        let (_fixture, library) = online();
        let library = std::sync::Arc::new(library);
        let before = library.get_book("web").unwrap();
        library.prefetch_chapter("web", 2, &|| false).unwrap();
        let owner = library.clone();
        let jobs = crate::downloads::Downloads::open(library.clone(), move |job, control| {
            owner.download_source_chapter(job, control)
        })
        .unwrap();
        jobs.start("web", 1, 1).unwrap();
        let deadline = std::time::Instant::now() + std::time::Duration::from_secs(20);
        while jobs.list(0).unwrap()["jobs"][0]["status"] != "completed" {
            assert!(std::time::Instant::now() < deadline);
            std::thread::sleep(std::time::Duration::from_millis(20));
        }
        jobs.join();
        let after = library.get_book("web").unwrap();
        assert_eq!(after["read_at"], before["read_at"]);
        assert_eq!(after["updated_at"], before["updated_at"]);
    }
    #[test]
    fn prefetch_image_phase_in_flight_during_purge_leaves_ordinary_cache() {
        let (_fixture, library) = online();
        slow_images(&library, 300);
        std::thread::scope(|scope| {
            let prefetch = scope.spawn(|| library.prefetch_chapter("web", 2, &|| false));
            while downloads(&library) == 0 {
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            stale_off_shelf(&library, "web");
            assert!(library.purge_expired_book().unwrap());
            assert!(prefetch.join().unwrap().is_ok());
        });
        for table in [
            "chapters",
            "chapter_cache",
            "asset_refs",
            "asset_pending",
            "image_pins",
            "chapter_search_docs",
            "notes",
        ] {
            assert_eq!(
                count(
                    &library,
                    &format!("SELECT count(*) FROM {table} WHERE book_id='web'")
                ),
                0,
                "{table}"
            );
        }
        assert_eq!(
            count(&library, "SELECT count(*) FROM pragma_foreign_key_check"),
            0
        );
        let saved = count(&library, "SELECT count(*) FROM image_cache");
        assert!(saved > 0);
        assert_eq!(
            count(
                &library,
                "SELECT count(*) FROM image_cache WHERE pin_count>0 OR protected"
            ),
            0
        );
    }
    /// A shelf book `keep` with a downloaded chapter that pins `illustration(0)`.
    fn keep_pinning_first_illustration(library: &Library) {
        library
            .save_source_image(&illustration(0), ONLINE_BOOK, None)
            .unwrap();
        let name = image_name(&illustration(0));
        exec(library, &format!(
            "INSERT INTO books(id,name,source,path,chapter_count,added_at,updated_at) VALUES('keep','Giữ','full','https://truyenfull.live/giu/',1,1,1);
             INSERT INTO chapters(book_id,idx,title,url,html,images,downloaded) VALUES('keep',0,'C','https://truyenfull.live/giu/c/','','[\"{}\"]',1);
             UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id='keep';
             INSERT INTO image_pins VALUES('keep',0,'{name}')",
            illustration(0)
        ));
        drain(|s| library.index_assets(s));
    }
    #[test]
    fn a_shared_protected_image_survives_the_purge() {
        let (_fixture, library) = online();
        keep_pinning_first_illustration(&library);
        library.prefetch_chapter("web", 2, &|| false).unwrap();
        stale_off_shelf(&library, "web");
        assert!(library.purge_expired_book().unwrap());
        drain(|s| library.cleanup_assets(s));
        assert!(cached_image(&library, &illustration(0)));
        let name = image_name(&illustration(0));
        assert!(
            count(
                &library,
                &format!("SELECT pin_count FROM image_cache WHERE name='{name}'")
            ) > 0
        );
    }
    #[test]
    fn leftover_prefetch_images_are_evicted_by_the_existing_size_limit() {
        let (_fixture, library) = online();
        keep_pinning_first_illustration(&library);
        slow_images(&library, 100);
        std::thread::scope(|scope| {
            let prefetch = scope.spawn(|| library.prefetch_chapter("web", 2, &|| false));
            while downloads(&library) < 2 {
                std::thread::sleep(std::time::Duration::from_millis(5));
            }
            stale_off_shelf(&library, "web");
            assert!(library.purge_expired_book().unwrap());
            prefetch.join().unwrap().unwrap();
        });
        let leftovers = "SELECT count(*) FROM image_cache WHERE pin_count=0";
        assert!(
            count(&library, leftovers) > 0,
            "in-flight images land as unpinned cache"
        );
        library.set_image_cache_limit(&json!(1)).unwrap();
        for _ in 0..3 {
            drain(|s| library.queue_image_evictions(s));
            drain(|s| library.cleanup_assets(s));
        }
        assert_eq!(count(&library, leftovers), 0);
        assert!((1..10).all(|k| !cached_image(&library, &illustration(k))));
        assert!(
            cached_image(&library, &illustration(0)),
            "the pinned image stays"
        );
    }
    #[test]
    fn a_leased_or_shared_cover_survives_the_purge() {
        for shared in [true, false] {
            let (_fixture, library) = online();
            let cover = illustration(11);
            library
                .save_source_image(&cover, ONLINE_BOOK, None)
                .unwrap();
            exec(
                &library,
                &format!("UPDATE books SET cover='{cover}' WHERE id='web'"),
            );
            if shared {
                exec(
                    &library,
                    &format!(
                        "INSERT INTO books(id,name,source,path,cover,added_at,updated_at) VALUES('keep','Giữ','full','https://truyenfull.live/giu/','{cover}',1,1)"
                    ),
                );
            }
            stale_off_shelf(&library, "web");
            let path = library.root().join("images").join(image_name(&cover));
            let lease = library.asset_leases.acquire(path.clone());
            assert!(library.purge_expired_book().unwrap());
            drain(|s| library.cleanup_assets(s));
            assert!(path.exists(), "leased cover kept (shared={shared})");
            drop(lease);
            // A leased file is retried after 1 s; let that delay pass.
            exec(&library, "UPDATE asset_gc_candidates SET retry_at=0");
            drain(|s| library.cleanup_assets(s));
            assert_eq!(path.exists(), shared, "shared={shared}");
        }
    }
    #[test]
    fn preserved_off_shelf_book_is_found_again_with_notes_and_edits() {
        let (_fixture, library) = online();
        stale_off_shelf(&library, "web");
        let old = now() - 32.0 * DAY;
        exec(&library, &format!(
            "INSERT INTO notes(id,book_id,created_at) VALUES('n','web',1);
             UPDATE chapters SET html='<p>Đã sửa</p>',original_html='<p>gốc</p>',downloaded=1 WHERE book_id='web' AND idx=1;
             INSERT INTO books(id,name,source,path,archived,read_at,added_at,updated_at) VALUES('gone','Tạm','full','https://truyenfull.live/tam/',1,{old},1,{old})"
        ));
        while library.purge_expired_book().unwrap() {}
        assert!(!present(&library, "gone"));
        let books = book_count(&library);
        for path in [
            ONLINE_BOOK,
            "https://truyenfull.vision/truyen-a/",
            "https://truyenfull.live/truyen-a.html",
            "https://truyenfull.live/truyen-a",
            "https://truyenfull.live/truyen-a/?ref=1",
        ] {
            let again = library.add_source_book(web_detail(path), false).unwrap();
            assert_eq!(again["id"], "web", "{path}");
            assert_eq!(book_count(&library), books, "{path}");
        }
        assert_eq!(library.notes(Some("web")).unwrap()[0]["id"], "n");
        let chapter = library.chapter("web", 1, None, 0).unwrap();
        assert!(
            chapter["html"].as_str().unwrap().contains("Đã sửa"),
            "{chapter}"
        );
    }
    #[test]
    fn progress_on_a_missing_book_is_not_found() {
        let (_fixture, library) = writable();
        let error = library
            .progress("gone", &json!({"index":0,"position":0.1}))
            .unwrap_err();
        assert_eq!(error.status, 404);
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
        // A vBook import keeps notes whose book is missing; it writes them directly.
        library
            .write_connection()
            .unwrap()
            .execute("INSERT INTO notes(id,book_id,position,quote,created_at) VALUES('orphan','missing',0.3,'Việt',1)", [])
            .unwrap();
        let note = library.notes(Some("missing")).unwrap()[0].clone();
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
