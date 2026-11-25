//! Read local documents without extracting archives or modifying originals.
//!
//! Reference: Python `documents.py`. Output matches the reference for well-formed
//! input; recovery from malformed markup and rare codec edge cases may differ.
//! Resource limits apply before any archive member is read.

use crate::{
    ApiError,
    casefold::{casefold, python_whitespace},
    codepages::{Codec, lookup},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use regex::Regex;
use serde_json::{Map, Value, json};
use std::{cell::Cell, collections::HashMap, fs::File, io::Read, path::Path, sync::LazyLock};

pub(crate) const MAX_FILE_SIZE: u64 = 128 * 1024 * 1024;
pub(crate) const MAX_MEMBER_SIZE: usize = 64 * 1024 * 1024;
const MAX_ARCHIVE_SIZE: u64 = 256 * 1024 * 1024;
const MAX_MEMBERS: usize = 10_000;
const MAX_RATIO: u64 = 200;
const MAX_OUTPUT_SIZE: usize = 256 * 1024 * 1024;
const TEXT_EXTENSIONS: &[&str] = &[
    ".txt",
    ".html",
    ".htm",
    ".xhtml",
    ".md",
    ".markdown",
    ".fb2",
    ".fodt",
];
const OFFICE_EXTENSIONS: &[&str] = &[".docx", ".docm", ".dotx", ".dotm", ".odt", ".ott"];
const ARCHIVE_EXTENSIONS: &[&str] = &[".epub", ".zip", ".cbz", ".fbz"];
const IMAGE_EXTENSIONS: &[&str] = &[".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"];

macro_rules! re {
    ($pattern:literal) => {{
        static REGEX: LazyLock<Regex> =
            LazyLock::new(|| Regex::new($pattern).expect("constant regex"));
        &*REGEX
    }};
}
macro_rules! fancy {
    ($pattern:literal) => {{
        static REGEX: LazyLock<fancy_regex::Regex> =
            LazyLock::new(|| fancy_regex::Regex::new($pattern).expect("constant regex"));
        &*REGEX
    }};
}

pub(crate) fn chapter_heading(line: &str) -> bool {
    re!(r"(?i)^(?:chương\s+\S|chapter\s+\S|part\s+\S|quyển\s+\S|phần\s+\S|第[^\s]{1,24}[章节卷回部])")
        .is_match(line)
}

pub(crate) fn error(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message)
}

pub(crate) fn unreadable(error: impl std::fmt::Display) -> ApiError {
    self::error(format!("Không đọc được tài liệu: {error}"))
}

pub(crate) fn supported_extensions() -> Vec<&'static str> {
    let mut all = [TEXT_EXTENSIONS, OFFICE_EXTENSIONS, ARCHIVE_EXTENSIONS].concat();
    all.sort_unstable();
    all
}

/// Python `shutil.which` for the default PATH/PATHEXT search.
pub(crate) fn which(name: &str) -> Value {
    let path = std::env::var_os("PATH").unwrap_or_default();
    let extensions = std::env::var("PATHEXT").unwrap_or_else(|_| ".COM;.EXE;.BAT;.CMD".into());
    let extensions = extensions
        .split(';')
        .filter(|e| !e.is_empty())
        .collect::<Vec<_>>();
    let mut folders = vec![std::path::PathBuf::from(".")];
    folders.extend(std::env::split_paths(&path));
    for folder in folders {
        for extension in &extensions {
            let candidate = folder.join(format!("{name}{extension}"));
            if candidate.is_file() {
                return Value::String(if folder.as_os_str() == "." {
                    format!("{name}{extension}")
                } else {
                    candidate.to_string_lossy().into_owned()
                });
            }
        }
    }
    Value::Null
}

pub(crate) fn capabilities() -> Value {
    json!({
        "supported_extensions": supported_extensions(),
        "pdf": {"supported": false, "reason": "Chưa tích hợp engine đọc/render PDF.", "detected_engine": which("pdftotext")},
        "ocr": {"supported": false, "reason": "Chưa tích hợp OCR PDF scan hoặc dữ liệu ngôn ngữ.", "detected_engine": which("tesseract")},
        "legacy": {"supported": false, "reason": "MOBI/PRC/AZW/AZW3/UMD/CHM/DOC/DOT/WPS/RTF/HWP/HWPX/HWPML cần bộ chuyển đổi đã kiểm thử.", "detected_engine": match which("ebook-convert") { Value::Null => which("soffice"), found => found }},
        "limits": {"file_bytes": MAX_FILE_SIZE, "member_bytes": MAX_MEMBER_SIZE, "archive_uncompressed_bytes": MAX_ARCHIVE_SIZE, "archive_members": MAX_MEMBERS, "compression_ratio": MAX_RATIO, "generated_character_budget": MAX_OUTPUT_SIZE},
        "layout": "Giữ văn bản, tiêu đề, ảnh raster và SVG tĩnh; không bảo toàn toàn bộ bố cục, CSS hoặc macro.",
    })
}

// ---------------------------------------------------------------- output budget

thread_local! {
    // Mirrors the reference ContextVar: None means per-call checks without accumulation.
    static BUDGET: Cell<Option<usize>> = const { Cell::new(None) };
}

pub(crate) struct BudgetScope(Option<usize>);
impl BudgetScope {
    pub(crate) fn new() -> Self {
        Self(BUDGET.replace(Some(0)))
    }
    fn if_absent() -> Option<Self> {
        BUDGET.get().is_none().then(Self::new)
    }
}
impl Drop for BudgetScope {
    fn drop(&mut self) {
        BUDGET.set(self.0);
    }
}

pub(crate) fn reserve(size: usize) -> Result<(), ApiError> {
    let used = BUDGET.get();
    let total = size.saturating_add(used.unwrap_or(0));
    if total > MAX_OUTPUT_SIZE {
        return Err(error(
            "Tài liệu vượt giới hạn nội dung được tạo (HTML, văn bản và ảnh nhúng).",
        ));
    }
    if used.is_some() {
        BUDGET.set(Some(total));
    }
    Ok(())
}

fn chars(s: &str) -> usize {
    s.chars().count()
}

/// `html.escape(value, quote=True)` that reserves its output size first.
pub(crate) fn escape(value: &str) -> Result<String, ApiError> {
    let out = html_escape(value);
    reserve(chars(&out))?;
    Ok(out)
}

pub(crate) fn html_escape(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for c in value.chars() {
        match c {
            '&' => out.push_str("&amp;"),
            '<' => out.push_str("&lt;"),
            '>' => out.push_str("&gt;"),
            '"' => out.push_str("&quot;"),
            '\'' => out.push_str("&#x27;"),
            c => out.push(c),
        }
    }
    out
}

// ---------------------------------------------------------------- Python string helpers

pub(crate) fn strip(s: &str) -> &str {
    s.trim_matches(python_whitespace)
}

/// `str.splitlines()` boundaries.
pub(crate) fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((at, c)) = chars.next() {
        if matches!(
            c,
            '\n' | '\r'
                | '\x0b'
                | '\x0c'
                | '\x1c'
                | '\x1d'
                | '\x1e'
                | '\u{85}'
                | '\u{2028}'
                | '\u{2029}'
        ) {
            lines.push(&s[start..at]);
            let mut end = at + c.len_utf8();
            if c == '\r' && chars.peek().is_some_and(|(_, n)| *n == '\n') {
                chars.next();
                end += 1;
            }
            start = end;
        }
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// Final component of a POSIX-style relative name.
pub(crate) fn posix_name(name: &str) -> &str {
    name.trim_end_matches('/')
        .rsplit('/')
        .next()
        .unwrap_or_default()
}

/// Python 3.14 `PurePath.suffix`.
pub(crate) fn suffix(name: &str) -> &str {
    let name = posix_name(name);
    if name == "." || name == ".." {
        return "";
    }
    match name.rfind('.') {
        Some(at) if at > 0 => &name[at..],
        _ => "",
    }
}

pub(crate) fn stem(name: &str) -> &str {
    let base = posix_name(name);
    &base[..base.len() - suffix(base).len()]
}

fn parent(name: &str) -> &str {
    name.trim_end_matches('/')
        .rsplit_once('/')
        .map_or(".", |(head, _)| head)
}

fn natural(value: &str) -> Vec<(bool, String)> {
    // Alternating text/decimal runs; decimals compare by numeric value.
    let mut parts = Vec::new();
    let mut text = String::new();
    let mut digits = String::new();
    for c in value.chars() {
        if let Some(d) = crate::library::decimal(c) {
            if digits.is_empty() {
                parts.push((false, casefold(&text)));
                text.clear();
            }
            digits.push(char::from_digit(d, 10).expect("decimal digit"));
        } else {
            if !digits.is_empty() {
                let trimmed = digits.trim_start_matches('0');
                parts.push((true, trimmed.to_owned()));
                digits.clear();
            }
            text.push(c);
        }
    }
    if !digits.is_empty() {
        parts.push((true, digits.trim_start_matches('0').to_owned()));
        text.clear();
    }
    parts.push((false, casefold(&text)));
    parts
}

fn natural_cmp(a: &str, b: &str) -> std::cmp::Ordering {
    let (a, b) = (natural(a), natural(b));
    for (x, y) in a.iter().zip(&b) {
        let order = if x.0 {
            x.1.len().cmp(&y.1.len()).then_with(|| x.1.cmp(&y.1))
        } else {
            x.1.chars().cmp(y.1.chars())
        };
        if order.is_ne() {
            return order;
        }
    }
    a.len().cmp(&b.len())
}

// ---------------------------------------------------------------- text decoding

pub(crate) fn normalize_codec(name: &str) -> String {
    let mut out = String::new();
    let mut punct = false;
    for c in name.to_ascii_lowercase().chars() {
        if c.is_ascii_alphanumeric() || c == '.' {
            if punct && !out.is_empty() {
                out.push('_');
            }
            out.push(c);
            punct = false;
        } else {
            punct = true;
        }
    }
    out
}

pub(crate) fn decode_with(data: &[u8], codec: Codec) -> Option<String> {
    match codec {
        Codec::Utf8 => std::str::from_utf8(data).ok().map(str::to_owned),
        Codec::Utf8Sig => std::str::from_utf8(data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data))
            .ok()
            .map(str::to_owned),
        Codec::Ascii => data
            .is_ascii()
            .then(|| String::from_utf8_lossy(data).into_owned()),
        Codec::Single(table) => {
            let mut out = String::with_capacity(data.len());
            for &b in data {
                if b < 128 {
                    out.push(b as char);
                } else {
                    out.push(char::from_u32(match table[usize::from(b - 128)] {
                        0xffff => return None,
                        value => u32::from(value),
                    })?);
                }
            }
            Some(out)
        }
        Codec::Multi(encoding) => encoding
            .decode_without_bom_handling_and_without_replacement(data)
            .map(|s| s.into_owned()),
    }
}

fn decode_utf16(data: &[u8], unit: usize) -> Option<String> {
    let little = data[..2] == [0xff, 0xfe];
    let body = &data[unit..];
    if !body.len().is_multiple_of(unit) {
        return None;
    }
    if unit == 2 {
        let units = body.as_chunks::<2>().0.iter().map(|c| {
            if little {
                u16::from_le_bytes([c[0], c[1]])
            } else {
                u16::from_be_bytes([c[0], c[1]])
            }
        });
        char::decode_utf16(units)
            .collect::<Result<String, _>>()
            .ok()
    } else {
        body.as_chunks::<4>()
            .0
            .iter()
            .map(|c| {
                let value = if little {
                    u32::from_le_bytes([c[0], c[1], c[2], c[3]])
                } else {
                    u32::from_be_bytes([c[0], c[1], c[2], c[3]])
                };
                char::from_u32(value)
            })
            .collect()
    }
}

/// Return (text, encoding); fallback encodings remain visible to callers.
pub(crate) fn decode_text(data: &[u8], repair_utf8: bool) -> Result<(String, String), ApiError> {
    if data.len() > MAX_MEMBER_SIZE {
        return Err(error("Văn bản vượt giới hạn 64 MiB."));
    }
    let mut repair = repair_utf8;
    let encodings: Vec<String> = if data.starts_with(b"\xff\xfe\x00\x00")
        || data.starts_with(b"\x00\x00\xfe\xff")
    {
        vec!["utf-32".into()]
    } else if data.starts_with(b"\xff\xfe") || data.starts_with(b"\xfe\xff") {
        vec!["utf-16".into()]
    } else if data.starts_with(b"\xef\xbb\xbf") {
        vec!["utf-8-sig".into()]
    } else if data.contains(&0) {
        return Err(error(
            "Tệp chứa dữ liệu nhị phân hoặc UTF-16 thiếu BOM; hãy lưu lại với encoding rõ ràng.",
        ));
    } else {
        static DECLARATION: LazyLock<regex::bytes::Regex> = LazyLock::new(|| {
            regex::bytes::Regex::new(
                r#"(?i-u)(?:encoding[ \t\n\r\x0b\x0c]*=[ \t\n\r\x0b\x0c]*["']|charset[ \t\n\r\x0b\x0c]*=[ \t\n\r\x0b\x0c]*["']?)([a-zA-Z0-9_-]+)"#,
            )
            .expect("constant regex")
        });
        let explicit = DECLARATION
            .captures(&data[..data.len().min(1024)])
            .map(|c| String::from_utf8_lossy(&c[1]).into_owned());
        repair = repair
            && explicit.as_deref().is_some_and(|e| {
                matches!(
                    e.to_lowercase().replace('_', "-").as_str(),
                    "utf-8" | "utf8"
                )
            });
        // shortcut: legacy encodings are ambiguous, add user encoding selection before promising exact decoding of every legacy text file.
        explicit
            .into_iter()
            .chain(["utf-8", "gb18030", "cp1258", "cp1252"].map(String::from))
            .collect()
    };
    for encoding in encodings {
        let mut label = encoding.clone();
        let decoded = match encoding.as_str() {
            "utf-32" => decode_utf16(data, 4),
            "utf-16" => decode_utf16(data, 2),
            _ => {
                let normalized = normalize_codec(&encoding);
                let Some((_, codec)) =
                    lookup(&normalized).or_else(|| lookup(&normalized.replace('.', "_")))
                else {
                    continue;
                };
                match decode_with(data, codec) {
                    Some(text) => Some(text),
                    None if repair && matches!(codec, Codec::Utf8 | Codec::Utf8Sig) => {
                        let body = if codec == Codec::Utf8Sig {
                            data.strip_prefix(b"\xef\xbb\xbf").unwrap_or(data)
                        } else {
                            data
                        };
                        let text = String::from_utf8_lossy(body).into_owned();
                        if text.matches('\u{fffd}').count() > (data.len() / 1000).max(1) {
                            continue;
                        }
                        label = "utf-8 (repaired)".into();
                        Some(text)
                    }
                    None => None,
                }
            }
        };
        let Some(text) = decoded else { continue };
        if text
            .chars()
            .any(|c| (c as u32) < 32 && !matches!(c, '\t' | '\n' | '\r' | '\x0c'))
        {
            continue;
        }
        let text = text.replace("\r\n", "\n").replace('\r', "\n");
        return Ok((text.trim_start_matches('\u{feff}').to_owned(), label));
    }
    Err(error(
        "Không giải mã được tệp văn bản; encoding lỗi hoặc nội dung nhị phân.",
    ))
}

// ---------------------------------------------------------------- XML (ElementTree subset)

#[derive(Default, Debug)]
pub(crate) struct Element {
    pub tag: String,
    pub attrib: Vec<(String, String)>,
    pub text: Option<String>,
    pub tail: Option<String>,
    pub children: Vec<Element>,
}

impl Element {
    pub(crate) fn local(&self) -> &str {
        self.tag.rsplit('}').next().unwrap_or_default()
    }
    pub(crate) fn get(&self, key: &str) -> Option<&str> {
        self.attrib
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }
    /// First attribute whose local name matches (`key.rsplit('}', 1)[-1] == name`).
    pub(crate) fn get_local(&self, name: &str) -> Option<&str> {
        self.attrib
            .iter()
            .find(|(k, _)| k.rsplit('}').next() == Some(name))
            .map(|(_, v)| v.as_str())
    }
    pub(crate) fn iter(&self) -> Vec<&Element> {
        let mut out = Vec::new();
        let mut pending = vec![self];
        while let Some(node) = pending.pop() {
            out.push(node);
            pending.extend(node.children.iter().rev());
        }
        out
    }
    pub(crate) fn nodes(&self, name: &str) -> Vec<&Element> {
        self.iter()
            .into_iter()
            .filter(|n| n.local() == name)
            .collect()
    }
    pub(crate) fn itertext(&self) -> String {
        let mut out = self.text.clone().unwrap_or_default();
        for child in &self.children {
            out.push_str(&child.itertext());
            out.push_str(child.tail.as_deref().unwrap_or_default());
        }
        out
    }
    pub(crate) fn node_text(&self) -> String {
        strip(&self.itertext()).to_owned()
    }
}

// Marker for nesting deeper than the reference's 100-level limit, detected while building.
const TOO_DEEP: &str = "\0depth";

fn xml_char(c: char) -> bool {
    matches!(c, '\t' | '\n' | '\r' | '\u{20}'..='\u{d7ff}' | '\u{e000}'..='\u{fffd}' | '\u{10000}'..)
}

fn xml_reference(name: &str) -> Result<char, String> {
    Ok(match name {
        "amp" => '&',
        "lt" => '<',
        "gt" => '>',
        "apos" => '\'',
        "quot" => '"',
        _ => {
            let Some(number) = name.strip_prefix('#') else {
                return Err(format!("undefined entity &{name};"));
            };
            let (digits, radix) = number
                .strip_prefix('x')
                .map_or((number, 10), |hex| (hex, 16));
            u32::from_str_radix(digits, radix)
                .ok()
                .and_then(char::from_u32)
                .filter(|c| xml_char(*c))
                .ok_or_else(|| format!("reference to invalid character number &{name};"))?
        }
    })
}

fn xml_attribute(raw: &str) -> Result<String, String> {
    let mut out = String::with_capacity(raw.len());
    let mut rest = raw;
    while let Some(at) = rest.find(['&', '\t', '\n', '\r', '<']) {
        out.push_str(&rest[..at]);
        let c = rest.as_bytes()[at];
        if c == b'<' {
            return Err("not well-formed (invalid token)".into());
        }
        if c != b'&' {
            out.push(' ');
            rest = &rest[at + 1..];
            continue;
        }
        let end = rest[at..]
            .find(';')
            .ok_or("not well-formed (invalid token)")?
            + at;
        out.push(xml_reference(&rest[at + 1..end])?);
        rest = &rest[end + 1..];
    }
    out.push_str(rest);
    Ok(out)
}

fn parse_xml_tree(text: &str) -> Result<Element, String> {
    use quick_xml::{
        NsReader,
        events::Event,
        name::{Namespace, ResolveResult},
    };
    if let Some(c) = text.chars().find(|c| !xml_char(*c)) {
        return Err(format!(
            "not well-formed (invalid token) U+{:04X}",
            c as u32
        ));
    }
    let mut reader = NsReader::from_str(text);
    reader.config_mut().check_end_names = true;
    let mut stack: Vec<Element> = Vec::new();
    let mut root: Option<Element> = None;
    let push_text =
        |stack: &mut Vec<Element>, root: &Option<Element>, value: &str| -> Result<(), String> {
            match stack.last_mut() {
                Some(top) => {
                    let slot = match top.children.last_mut() {
                        Some(child) => &mut child.tail,
                        None => &mut top.text,
                    };
                    slot.get_or_insert_with(String::new).push_str(value);
                    Ok(())
                }
                None if strip(value).is_empty()
                    || value.chars().all(|c| matches!(c, ' ' | '\t' | '\n' | '\r')) =>
                {
                    Ok(())
                }
                None if root.is_some() => Err("junk after document element".into()),
                None => Err("syntax error".into()),
            }
        };
    loop {
        let (namespace, event) = reader.read_resolved_event().map_err(|e| e.to_string())?;
        match event {
            Event::Start(ref e) | Event::Empty(ref e) => {
                if root.is_some() && stack.is_empty() {
                    return Err("junk after document element".into());
                }
                if stack.len() > 100 {
                    return Err(TOO_DEEP.into());
                }
                let local = String::from_utf8_lossy(e.local_name().as_ref()).into_owned();
                let tag = match namespace {
                    ResolveResult::Bound(Namespace(ns)) => {
                        format!("{{{}}}{local}", String::from_utf8_lossy(ns))
                    }
                    ResolveResult::Unbound => local,
                    ResolveResult::Unknown(_) => return Err("unbound prefix".into()),
                };
                let mut attrib = Vec::new();
                for attr in e.attributes() {
                    let attr = attr.map_err(|e| e.to_string())?;
                    let key = attr.key.as_ref();
                    if key == b"xmlns" || key.starts_with(b"xmlns:") {
                        continue;
                    }
                    let (resolved, name) = reader.resolver().resolve_attribute(attr.key);
                    let name = String::from_utf8_lossy(name.as_ref()).into_owned();
                    let key = match resolved {
                        ResolveResult::Bound(Namespace(ns)) => {
                            format!("{{{}}}{name}", String::from_utf8_lossy(ns))
                        }
                        ResolveResult::Unbound => name,
                        ResolveResult::Unknown(_) => return Err("unbound prefix".into()),
                    };
                    let raw = std::str::from_utf8(&attr.value).map_err(|e| e.to_string())?;
                    attrib.push((key, xml_attribute(raw)?));
                }
                let element = Element {
                    tag,
                    attrib,
                    ..Element::default()
                };
                if matches!(event, Event::Empty(_)) {
                    match stack.last_mut() {
                        Some(top) => top.children.push(element),
                        None => root = Some(element),
                    }
                } else {
                    stack.push(element);
                }
            }
            Event::End(_) => {
                let element = stack.pop().ok_or("mismatched tag")?;
                match stack.last_mut() {
                    Some(top) => top.children.push(element),
                    None => root = Some(element),
                }
            }
            Event::Text(e) => {
                let value = e.decode().map_err(|e| e.to_string())?;
                push_text(&mut stack, &root, &value)?;
            }
            Event::CData(e) => {
                let value = e.decode().map_err(|e| e.to_string())?;
                if stack.is_empty() {
                    return Err("syntax error".into());
                }
                push_text(&mut stack, &root, &value)?;
            }
            Event::GeneralRef(e) => {
                let name = e.decode().map_err(|e| e.to_string())?;
                if stack.is_empty() {
                    return Err("syntax error".into());
                }
                push_text(&mut stack, &root, &xml_reference(&name)?.to_string())?;
            }
            Event::DocType(_) => return Err("DTD".into()),
            Event::Eof => break,
            Event::Decl(_) | Event::PI(_) | Event::Comment(_) => {}
        }
    }
    if !stack.is_empty() {
        return Err("no element found".into());
    }
    root.ok_or_else(|| "no element found: line 1, column 0".into())
}

pub(crate) fn parse_xml(data: &[u8]) -> Result<Element, ApiError> {
    let (text, _) = decode_text(data, false)?;
    parse_xml_text(&text)
}

pub(crate) fn parse_xml_text(text: &str) -> Result<Element, ApiError> {
    if re!(r"(?i)<!\s*(?:DOCTYPE|ENTITY)\b").is_match(text) {
        return Err(error("Không cho phép DTD/entity trong tài liệu XML."));
    }
    let root = parse_xml_tree(text).map_err(|e| {
        if e == TOO_DEEP {
            error("XML lồng quá sâu.")
        } else {
            error(format!("Tài liệu XML không hợp lệ: {e}"))
        }
    })?;
    let mut pending = vec![(&root, 0)];
    while let Some((node, depth)) = pending.pop() {
        if depth > 100 {
            return Err(error("XML lồng quá sâu."));
        }
        pending.extend(node.children.iter().map(|c| (c, depth + 1)));
    }
    Ok(root)
}

// ---------------------------------------------------------------- images and archives

pub(crate) fn image_url(data: &[u8]) -> Result<String, ApiError> {
    let mime = if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png"
    } else if data.starts_with(b"\xff\xd8\xff") {
        "image/jpeg"
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        "image/gif"
    } else if data.starts_with(b"RIFF") && data.get(8..12) == Some(b"WEBP") {
        "image/webp"
    } else if data.starts_with(b"BM") {
        "image/bmp"
    } else {
        return Err(error("Ảnh không phải PNG/JPEG/GIF/WebP/BMP hợp lệ."));
    };
    let prefix = format!("data:{mime};base64,");
    reserve(prefix.len() + 4 * data.len().div_ceil(3))?;
    Ok(prefix + &STANDARD.encode(data))
}

/// Python `urlsplit` scheme/netloc detection plus its path component.
fn split_reference(reference: &str) -> (bool, String) {
    let cleaned: String = reference
        .trim_start_matches(|c: char| c <= ' ')
        .chars()
        .filter(|c| !matches!(c, '\t' | '\r' | '\n'))
        .collect();
    let mut rest = cleaned.as_str();
    let mut external = false;
    if let Some(colon) = rest.find(':') {
        let scheme = &rest[..colon];
        if scheme
            .chars()
            .next()
            .is_some_and(|c| c.is_ascii_alphabetic())
            && scheme
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || "+-.".contains(c))
        {
            external = true;
            rest = &rest[colon + 1..];
        }
    }
    if rest.starts_with("//") {
        let host_end = rest[2..]
            .find(['/', '?', '#'])
            .map_or(rest.len(), |at| at + 2);
        external |= host_end > 2;
        rest = &rest[host_end..];
    }
    let path = rest.split(['?', '#']).next().unwrap_or_default();
    (external, path.to_owned())
}

fn unquote(path: &str) -> String {
    let bytes = path.as_bytes();
    if !path.contains('%') {
        return path.to_owned();
    }
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%'
            && i + 2 < bytes.len()
            && bytes[i + 1].is_ascii_hexdigit()
            && bytes[i + 2].is_ascii_hexdigit()
        {
            out.push(u8::from_str_radix(&path[i + 1..i + 3], 16).expect("hex digits"));
            i += 3;
        } else {
            out.push(bytes[i]);
            i += 1;
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

fn normpath(path: &str) -> String {
    let mut parts: Vec<&str> = Vec::new();
    for part in path.split('/') {
        match part {
            "" | "." => {}
            ".." if parts.last().is_some_and(|p| *p != "..") => {
                parts.pop();
            }
            part => parts.push(part),
        }
    }
    if parts.is_empty() {
        ".".into()
    } else {
        parts.join("/")
    }
}

pub(crate) struct Archive {
    zip: zip::ZipArchive<File>,
    pub members: Vec<String>,
    index: HashMap<String, usize>,
    folded: HashMap<String, Vec<String>>,
    pub warnings: Vec<(String, String)>,
    _budget: Option<BudgetScope>,
}

impl Archive {
    pub(crate) fn open(path: &Path) -> Result<Self, ApiError> {
        let file = File::open(path).map_err(unreadable)?;
        let mut zip = zip::ZipArchive::new(file).map_err(|e| match e {
            // Python zipfile reports any missing/corrupt central directory this way.
            zip::result::ZipError::InvalidArchive(_) => unreadable("File is not a zip file"),
            other => unreadable(other),
        })?;
        if zip.len() > MAX_MEMBERS {
            return Err(error("Archive chứa quá nhiều mục (tối đa 10.000)."));
        }
        let mut members = Vec::new();
        let mut index = HashMap::new();
        let mut folded: HashMap<String, Vec<String>> = HashMap::new();
        let mut total = 0u64;
        for i in 0..zip.len() {
            let entry = zip.by_index_raw(i).map_err(unreadable)?;
            let raw = entry.name().map_err(unreadable)?;
            let name = raw
                .split('\0')
                .next()
                .unwrap_or_default()
                .replace('\\', "/");
            let parts = name.trim_end_matches('/').split('/');
            if name.starts_with('/')
                || name.is_empty()
                || parts.into_iter().any(|p| {
                    matches!(p, "" | "." | "..")
                        || p.contains(':')
                        || p.chars().any(|c| (c as u32) < 32)
                })
            {
                return Err(error(format!("Đường dẫn archive không an toàn: {name}")));
            }
            if (entry.external_attributes() >> 16) & 0o170000 == 0o120000 {
                return Err(error("Không cho phép symlink trong archive."));
            }
            if entry.encrypted() {
                return Err(error("Archive có mật khẩu chưa được hỗ trợ."));
            }
            if name.ends_with('/') {
                continue;
            }
            if index.contains_key(&name) {
                return Err(error(format!("Archive có tên mục trùng: {name}")));
            }
            let size = entry.size();
            total += size;
            if size > MAX_MEMBER_SIZE as u64 || total > MAX_ARCHIVE_SIZE {
                return Err(error("Archive vượt giới hạn kích thước giải nén."));
            }
            if size > 1_048_576 && size > MAX_RATIO * entry.compressed_size().max(1) {
                return Err(error("Tỷ lệ nén archive vượt giới hạn an toàn."));
            }
            drop(entry);
            folded
                .entry(casefold(&name))
                .or_default()
                .push(name.clone());
            index.insert(name.clone(), i);
            members.push(name);
        }
        if zip.has_duplicate_names() {
            return Err(error("Archive có tên mục trùng."));
        }
        Ok(Self {
            zip,
            members,
            index,
            folded,
            warnings: Vec::new(),
            _budget: BudgetScope::if_absent(),
        })
    }

    pub(crate) fn contains(&self, name: &str) -> bool {
        self.index.contains_key(name)
    }

    pub(crate) fn member_name(&self, name: &str) -> Result<Option<String>, ApiError> {
        if self.index.contains_key(name) {
            return Ok(Some(name.to_owned()));
        }
        match self.folded.get(&casefold(name)).map(Vec::as_slice) {
            Some([one]) => Ok(Some(one.clone())),
            Some([_, _, ..]) => Err(error(format!(
                "Tham chiếu archive mơ hồ do khác chữ hoa/thường: {name}"
            ))),
            _ => Ok(None),
        }
    }

    pub(crate) fn read(&mut self, name: &str) -> Result<Vec<u8>, ApiError> {
        let actual = self
            .member_name(name)?
            .ok_or_else(|| error(format!("Archive thiếu mục bắt buộc: {name}")))?;
        let entry = self.zip.by_index(self.index[&actual]).map_err(unreadable)?;
        let mut data = Vec::new();
        entry
            .take(MAX_MEMBER_SIZE as u64 + 1)
            .read_to_end(&mut data)
            .map_err(unreadable)?;
        if data.len() > MAX_MEMBER_SIZE {
            return Err(error("Mục archive vượt giới hạn kích thước."));
        }
        Ok(data)
    }

    pub(crate) fn resolve(base: &str, reference: &str) -> Result<String, ApiError> {
        let (external, path) = split_reference(reference);
        if external {
            return Err(error(
                "Tài liệu archive không được đọc tài nguyên bên ngoài.",
            ));
        }
        let path = unquote(&path);
        if path.contains('\\') || path.starts_with('/') || path.contains(':') || path.contains('\0')
        {
            return Err(error("Tham chiếu archive không an toàn."));
        }
        let directory = base.rsplit_once('/').map_or("", |(head, _)| head);
        let joined = if directory.is_empty() {
            path
        } else {
            format!("{directory}/{path}")
        };
        let resolved = normpath(&joined);
        if resolved == ".." || resolved.starts_with("../") {
            return Err(error("Tham chiếu thoát khỏi archive."));
        }
        Ok(resolved)
    }

    pub(crate) fn image(
        &mut self,
        base: &str,
        reference: &str,
        allow_svg: bool,
    ) -> Result<String, ApiError> {
        let name = Self::resolve(base, reference)?;
        let extension = suffix(&name).to_lowercase();
        if !(IMAGE_EXTENSIONS.contains(&extension.as_str()) || allow_svg && extension == ".svg") {
            return Err(error(format!(
                "Định dạng ảnh trong tài liệu chưa hỗ trợ: {extension}"
            )));
        }
        if self.member_name(&name)?.is_none() {
            if !self.warnings.iter().any(|(k, _)| *k == name) {
                let message = format!("Ảnh không có trong tệp, đã giữ phần chữ: {name}");
                self.warnings.push((name, message));
            }
            return Ok(String::new());
        }
        let data = self.read(&name)?;
        if extension == ".svg" {
            let url = crate::http::svg_data(&format!(
                "data:image/svg+xml;base64,{}",
                STANDARD.encode(&data)
            ));
            if url.is_empty() {
                return Err(error(format!(
                    "Ảnh SVG không hợp lệ hoặc vượt giới hạn an toàn: {name}"
                )));
            }
            reserve(url.len())?;
            return Ok(url);
        }
        image_url(&data)
    }
}

// ---------------------------------------------------------------- HTML (html.parser subset)

pub(crate) enum Token {
    Start(String, Vec<(String, Option<String>)>, bool),
    End(String),
    Data(String),
}

const CDATA_ELEMENTS: &[&str] = &["script", "style", "xmp", "iframe", "noembed", "noframes"];
const RCDATA_ELEMENTS: &[&str] = &["textarea", "title"];

fn unescape(text: &str) -> Result<String, ApiError> {
    crate::entities::unescape(text)
}

fn unescape_attribute(value: &str) -> Result<String, ApiError> {
    let pattern = re!(r"&(#[0-9]+|#[xX][0-9a-fA-F]+|[a-zA-Z][a-zA-Z0-9]*)[;=]?");
    let mut out = String::new();
    let mut at = 0;
    for m in pattern.find_iter(value) {
        out.push_str(&value[at..m.start()]);
        let reference = m.as_str();
        if reference.starts_with("&#")
            || !reference.ends_with('=') && crate::entities::named(&reference[1..]).is_some()
        {
            out.push_str(&unescape(reference)?);
        } else {
            out.push_str(reference);
        }
        at = m.end();
    }
    out.push_str(&value[at..]);
    Ok(out)
}

/// Tokenize like `HTMLParser(convert_charrefs=True)` fed once and closed.
pub(crate) fn tokenize(raw: &str) -> Result<Vec<Token>, ApiError> {
    let tagfind = fancy!(r"\G([a-zA-Z][^\t\n\r\f />]*)(?:[\t\n\r\f ]|/(?!>))*");
    let attrfind = fancy!(
        r#"\G((?<=['"\t\n\r\f /])[^\t\n\r\f />][^\t\n\r\f /=>]*)([\t\n\r\f ]*=[\t\n\r\f ]*('[^']*'|"[^"]*"|(?!['"])[^>\t\n\r\f ]*))?(?:[\t\n\r\f ]|/(?!>))*"#
    );
    let locatetagend = fancy!(
        r#"\G[a-zA-Z][^\t\n\r\f />]*[\t\n\r\f /]*(?:(?<=['"\t\n\r\f /])[^\t\n\r\f />][^\t\n\r\f /=>]*(?:[\t\n\r\f ]*=[\t\n\r\f ]*(?:'[^']*'|"[^"]*"|(?!['"])[^>\t\n\r\f ]*))?[\t\n\r\f /]*)*>?"#
    );
    let fancy_err = |e: fancy_regex::Error| error(format!("HTML không hợp lệ: {e}"));
    let mut tokens = Vec::new();
    let mut cdata: Option<(String, bool)> = None;
    let mut end_tags: HashMap<String, Regex> = HashMap::new();
    let n = raw.len();
    let mut i = 0;
    let data = |tokens: &mut Vec<Token>, text: &str, escapable: bool| -> Result<(), ApiError> {
        tokens.push(Token::Data(if escapable {
            unescape(text)?
        } else {
            text.to_owned()
        }));
        Ok(())
    };
    while i < n {
        let j = match &cdata {
            None => raw[i..].find('<').map_or(n, |at| at + i),
            Some((elem, _)) if elem == "plaintext" => n,
            Some((elem, _)) => {
                let pattern = end_tags.entry(elem.clone()).or_insert_with(|| {
                    Regex::new(&format!("(?i-u)</{}", regex::escape(elem))).expect("tag regex")
                });
                let mut found = None;
                let mut from = i;
                while let Some(m) = pattern.find_at(raw, from) {
                    if raw[m.end()..].starts_with(['\t', '\n', '\r', '\x0c', ' ', '/', '>']) {
                        found = Some(m.start());
                        break;
                    }
                    from = m.end();
                }
                match found {
                    Some(at) => at,
                    None => break,
                }
            }
        };
        if i < j {
            data(
                &mut tokens,
                &raw[i..j],
                cdata.as_ref().is_none_or(|(_, e)| *e),
            )?;
        }
        i = j;
        if i == n {
            break;
        }
        let rest = &raw[i..];
        let starttag = rest.len() > 1 && rest.as_bytes()[1].is_ascii_alphabetic();
        let k: Option<usize> = if starttag {
            // parse_starttag
            let end = locatetagend
                .find_from_pos(raw, i + 1)
                .map_err(fancy_err)?
                .expect("tag name");
            if !raw[..end.end()].ends_with('>') {
                None
            } else {
                let endpos = end.end();
                let name = tagfind
                    .captures_from_pos(raw, i + 1)
                    .map_err(fancy_err)?
                    .expect("tag name");
                let tag = name[1].to_lowercase();
                let mut k = name.get(0).expect("match").end();
                let mut attrs = Vec::new();
                while k < endpos {
                    let Some(m) = attrfind
                        .captures_from_pos(&raw[..endpos], k)
                        .map_err(fancy_err)?
                    else {
                        break;
                    };
                    let name = m[1].to_lowercase();
                    let value = match m.get(3) {
                        None if m.get(2).is_none() => None,
                        value => {
                            let mut v = value.map_or("", |v| v.as_str());
                            if v.len() >= 2
                                && (v.starts_with('\'') && v.ends_with('\'')
                                    || v.starts_with('"') && v.ends_with('"'))
                            {
                                v = &v[1..v.len() - 1];
                            } else if v == "'" || v == "\"" {
                                v = "";
                            }
                            Some(if v.is_empty() {
                                String::new()
                            } else {
                                unescape_attribute(v)?
                            })
                        }
                    };
                    attrs.push((name, value));
                    k = m.get(0).expect("match").end();
                }
                let end_text = strip(&raw[k..endpos]);
                if end_text != ">" && end_text != "/>" {
                    tokens.push(Token::Data(raw[i..endpos].to_owned()));
                } else if end_text.ends_with("/>") {
                    tokens.push(Token::Start(tag, attrs, true));
                } else {
                    if CDATA_ELEMENTS.contains(&tag.as_str()) || tag == "plaintext" {
                        cdata = Some((tag.clone(), false));
                    } else if RCDATA_ELEMENTS.contains(&tag.as_str()) {
                        cdata = Some((tag.clone(), true));
                    }
                    tokens.push(Token::Start(tag, attrs, false));
                }
                Some(endpos)
            }
        } else if let Some(after) = rest.strip_prefix("</") {
            // parse_endtag
            if !after.contains('>') {
                None
            } else if !after
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphabetic)
            {
                if after.starts_with('>') {
                    Some(i + 3)
                } else {
                    Some(i + 2 + after.find('>').expect("checked") + 1)
                }
            } else {
                let end = locatetagend
                    .find_from_pos(raw, i + 2)
                    .map_err(fancy_err)?
                    .expect("tag name");
                if !raw[..end.end()].ends_with('>') {
                    None
                } else {
                    let name = tagfind
                        .captures_from_pos(raw, i + 2)
                        .map_err(fancy_err)?
                        .expect("tag name");
                    tokens.push(Token::End(name[1].to_lowercase()));
                    cdata = None;
                    Some(end.end())
                }
            }
        } else if let Some(body) = rest.strip_prefix("<!--") {
            if let Some(m) = re!(r"^-?>").find(body) {
                Some(i + 4 + m.end())
            } else {
                re!(r"--!?>").find(body).map(|m| i + 4 + m.end())
            }
        } else if let Some(after) = rest.strip_prefix("<?") {
            after.find('>').map(|at| i + 2 + at + 1)
        } else if let Some(after) = rest.strip_prefix("<![CDATA[") {
            after.find("]]>").map(|at| i + 9 + at + 3)
        } else if let Some(after) = rest.strip_prefix("<!") {
            if rest
                .get(..9)
                .is_some_and(|head| head.eq_ignore_ascii_case("<!doctype"))
            {
                rest[9..].find('>').map(|at| i + 9 + at + 1)
            } else {
                after.find('>').map(|at| i + 2 + at + 1)
            }
        } else {
            tokens.push(Token::Data("<".into()));
            Some(i + 1)
        };
        match k {
            Some(k) => i = k,
            None => {
                // At EOF an unterminated construct swallows the rest; only "</" remains data.
                if rest == "</" {
                    tokens.push(Token::Data("</".into()));
                }
                i = n;
            }
        }
    }
    if i < n {
        data(
            &mut tokens,
            &raw[i..],
            cdata.as_ref().is_none_or(|(_, e)| *e),
        )?;
    }
    Ok(tokens)
}

const BLOCKS: &[&str] = &[
    "p",
    "div",
    "section",
    "article",
    "main",
    "br",
    "hr",
    "li",
    "blockquote",
    "pre",
    "tr",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
];
const INLINE: &[&str] = &[
    "b", "strong", "em", "i", "u", "s", "ul", "ol", "code", "table", "td", "th", "thead", "tbody",
    "sup", "sub", "span", "a", "img",
];
const SKIP: &[&str] = &[
    "script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript", "form",
];
const WRAPPERS: &[&str] = &["root", "html", "body", "div", "section", "article", "main"];
const VOID: &[&str] = &[
    "img", "br", "hr", "meta", "link", "input", "source", "wbr", "base", "area", "param", "col",
];

fn allowed(tag: &str) -> bool {
    BLOCKS.contains(&tag) || INLINE.contains(&tag)
}

pub(crate) enum HNode {
    Text(String),
    Element(HElement),
}

pub(crate) struct HElement {
    tag: String,
    attrs: Vec<(String, Option<String>)>,
    children: Vec<HNode>,
}

impl HElement {
    fn attr(&self, name: &str) -> Option<&str> {
        self.attrs
            .iter()
            .rev()
            .find(|(k, _)| k == name)
            .and_then(|(_, v)| v.as_deref())
    }
}

pub(crate) type ImageLoader<'a> = dyn FnMut(&str) -> Result<String, ApiError> + 'a;

pub(crate) struct Html<'a, 'b> {
    root: HElement,
    image: Option<&'a mut ImageLoader<'b>>,
    pub title: String,
}

impl<'a, 'b> Html<'a, 'b> {
    pub(crate) fn parse(
        source: &str,
        image: Option<&'a mut ImageLoader<'b>>,
    ) -> Result<Self, ApiError> {
        let mut parser = Html {
            root: HElement {
                tag: "root".into(),
                attrs: Vec::new(),
                children: Vec::new(),
            },
            image,
            title: String::new(),
        };
        let mut stack: Vec<HElement> = Vec::new();
        let mut skipped: Vec<String> = Vec::new();
        fn attach(parser: &mut Html<'_, '_>, stack: &mut [HElement], node: HNode) {
            match stack.last_mut() {
                Some(top) => top.children.push(node),
                None => parser.root.children.push(node),
            }
        }
        for token in tokenize(source)? {
            let (start, end) = match token {
                Token::Data(text) => {
                    if skipped.is_empty() {
                        attach(&mut parser, &mut stack, HNode::Text(text));
                    }
                    continue;
                }
                Token::Start(tag, attrs, closed) => {
                    (Some((tag.clone(), attrs)), closed.then_some(tag))
                }
                Token::End(tag) => (None, Some(tag)),
            };
            if let Some((tag, attrs)) = start {
                if stack.len() + 1 > 100 {
                    return Err(error("HTML lồng quá sâu."));
                }
                if !skipped.is_empty() || SKIP.contains(&tag.as_str()) {
                    if SKIP.contains(&tag.as_str()) {
                        skipped.push(tag);
                    }
                } else {
                    let node = HElement {
                        tag: tag.clone(),
                        attrs,
                        children: Vec::new(),
                    };
                    if VOID.contains(&tag.as_str()) {
                        attach(&mut parser, &mut stack, HNode::Element(node));
                    } else {
                        stack.push(node);
                    }
                }
            }
            if let Some(tag) = end {
                if !skipped.is_empty() {
                    if skipped.last() == Some(&tag) {
                        skipped.pop();
                    }
                    continue;
                }
                if let Some(index) = stack.iter().rposition(|n| n.tag == tag) {
                    while stack.len() > index + 1 {
                        let node = stack.pop().expect("open element");
                        attach(&mut parser, &mut stack, HNode::Element(node));
                    }
                    let node = stack.pop().expect("open element");
                    if tag == "title" {
                        let rendered = parser.render_children(&node)?;
                        parser.title = html_text(&rendered)?;
                    }
                    attach(&mut parser, &mut stack, HNode::Element(node));
                }
            }
        }
        while let Some(node) = stack.pop() {
            attach(&mut parser, &mut stack, HNode::Element(node));
        }
        Ok(parser)
    }

    fn render_children(&mut self, node: &HElement) -> Result<String, ApiError> {
        let mut out = String::new();
        for child in &node.children {
            out.push_str(&self.render(child)?);
        }
        Ok(out)
    }

    fn render(&mut self, node: &HNode) -> Result<String, ApiError> {
        let node = match node {
            HNode::Text(text) => return escape(text),
            HNode::Element(node) => node,
        };
        if matches!(node.tag.as_str(), "title" | "head" | "meta" | "link") {
            return Ok(String::new());
        }
        if node.tag == "img" {
            let Some(reference) = node.attr("src").filter(|s| !s.is_empty()) else {
                return Ok(String::new());
            };
            let Some(image) = self.image.as_mut() else {
                return Ok(String::new());
            };
            if split_reference(reference).0 {
                return Ok(String::new());
            }
            let url = image(reference)?;
            if url.is_empty() {
                return Ok(String::new());
            }
            return Ok(format!(
                "<img src=\"{}\" alt=\"{}\">",
                escape(&url)?,
                escape(node.attr("alt").unwrap_or_default())?
            ));
        }
        let inner = self.render_children(node)?;
        if !allowed(&node.tag) {
            return Ok(inner);
        }
        let void = matches!(node.tag.as_str(), "br" | "hr");
        reserve(chars(&node.tag) + 2 + if void { 0 } else { chars(&node.tag) + 3 })?;
        Ok(if void {
            format!("<{}>{inner}", node.tag)
        } else {
            format!("<{0}>{inner}</{0}>", node.tag)
        })
    }

    pub(crate) fn chapters(&mut self, default_title: &str) -> Result<Vec<Value>, ApiError> {
        let mut events = Vec::new();
        let root = std::mem::replace(
            &mut self.root,
            HElement {
                tag: "root".into(),
                attrs: Vec::new(),
                children: Vec::new(),
            },
        );
        let root = HNode::Element(root);
        self.walk(&root, &mut events)?;
        sections(events, default_title)
    }

    fn walk(
        &mut self,
        node: &HNode,
        events: &mut Vec<(Option<String>, String)>,
    ) -> Result<(), ApiError> {
        match node {
            HNode::Element(element) if WRAPPERS.contains(&element.tag.as_str()) => {
                for child in &element.children {
                    self.walk(child, events)?;
                }
            }
            HNode::Element(element) if matches!(element.tag.as_str(), "h1" | "h2") => {
                let rendered = self.render(node)?;
                events.push((Some(html_text(&rendered)?), rendered));
            }
            node => events.push((None, self.render(node)?)),
        }
        Ok(())
    }
}

pub(crate) fn html_text(source: &str) -> Result<String, ApiError> {
    let mut text = String::new();
    for token in tokenize(source)? {
        match token {
            Token::Start(tag, _, closed) => {
                if BLOCKS.contains(&tag.as_str()) {
                    text.push('\n');
                    if closed {
                        text.push('\n');
                    }
                }
            }
            Token::End(tag) => {
                if BLOCKS.contains(&tag.as_str()) {
                    text.push('\n');
                }
            }
            Token::Data(data) => text.push_str(&data),
        }
    }
    let joined = splitlines(&text)
        .into_iter()
        .map(strip)
        .collect::<Vec<_>>()
        .join("\n");
    Ok(strip(&re!(r"\n{3,}").replace_all(&joined, "\n\n")).to_owned())
}

pub(crate) fn chapter(title: &str, html: &str) -> Result<Value, ApiError> {
    reserve(chars(html))?;
    let text = html_text(html)?;
    let images = re!(r#"<img src="(data:image/[^"]+)""#)
        .captures_iter(html)
        .map(|c| c[1].to_owned())
        .collect::<Vec<_>>();
    reserve(chars(&text) + images.iter().map(|i| chars(i)).sum::<usize>() + chars(title))?;
    Ok(
        json!({"index": 0, "title": strip(title), "html": strip(html), "text": text, "images": images, "downloaded": true}),
    )
}

pub(crate) fn sections(
    events: Vec<(Option<String>, String)>,
    default_title: &str,
) -> Result<Vec<Value>, ApiError> {
    let mut chapters = Vec::new();
    let mut title = default_title.to_owned();
    let mut parts = String::new();
    let flush = |chapters: &mut Vec<Value>, title: &str, html: &str| -> Result<(), ApiError> {
        if !html_text(html)?.is_empty() || html.contains("<img ") {
            chapters.push(chapter(title, html)?);
        }
        Ok(())
    };
    for (heading, html) in events {
        if let Some(heading) = heading.filter(|h| !h.is_empty()) {
            flush(&mut chapters, &title, &parts)?;
            title = heading;
            parts.clear();
        }
        parts.push_str(&html);
    }
    flush(&mut chapters, &title, &parts)?;
    Ok(chapters)
}

fn markdown_inline(rendered: &str) -> String {
    let strong = re!(r"\*\*(.+?)\*\*").replace_all(rendered, "<strong>${1}</strong>");
    let code = re!(r"`([^`]+)`").replace_all(&strong, "<code>${1}</code>");
    let em = fancy!(r"(?<!\*)\*([^*]+)\*(?!\*)").replace_all(&code, "<em>${1}</em>");
    re!(r"^#{3,6}\s+(.+)$")
        .replace_all(&em, "<strong>${1}</strong>")
        .into_owned()
}

pub(crate) fn text_chapters(
    text: &str,
    title: &str,
    markdown: bool,
) -> Result<Vec<Value>, ApiError> {
    let mut events = Vec::new();
    let mut fenced = false;
    for line in splitlines(text) {
        if markdown
            && line
                .trim_start_matches(python_whitespace)
                .starts_with("```")
        {
            fenced = !fenced;
            events.push((
                None,
                if fenced {
                    "<pre><code>"
                } else {
                    "</code></pre>"
                }
                .to_owned(),
            ));
            continue;
        }
        let heading = if markdown && !fenced {
            re!(r"^#{1,2}\s+(.+?)\s*#*$")
                .captures(line)
                .map(|c| c[1].to_owned())
        } else {
            None
        };
        let trimmed = strip(line);
        if let Some(name) = heading {
            let rendered = format!("<h2>{}</h2>", escape(&name)?);
            events.push((Some(name), rendered));
        } else if !fenced && chars(trimmed) <= 180 && chapter_heading(trimmed) {
            events.push((
                Some(trimmed.to_owned()),
                format!("<h2>{}</h2>", escape(trimmed)?),
            ));
        } else if fenced {
            events.push((None, escape(line)? + "\n"));
        } else if trimmed.is_empty() {
            events.push((None, "\n".into()));
        } else {
            let mut rendered = escape(line)?;
            if markdown {
                rendered = markdown_inline(&rendered);
            }
            events.push((None, format!("<p>{rendered}</p>")));
        }
    }
    if fenced {
        events.push((None, "</code></pre>".into()));
    }
    sections(events, title)
}

// ---------------------------------------------------------------- FB2

fn b64_lenient(text: &str) -> Option<Vec<u8>> {
    let clean: String = text
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || *c == '+' || *c == '/')
        .collect();
    if clean.len() % 4 == 1 {
        return None;
    }
    base64::engine::general_purpose::STANDARD_NO_PAD
        .decode(clean)
        .ok()
}

fn fb2(data: &[u8], default_title: &str) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    let root = parse_xml(data)?;
    if root.local() != "FictionBook" {
        return Err(error("Tệp không có cấu trúc FictionBook FB2."));
    }
    let titles = root.nodes("book-title");
    let name = titles
        .first()
        .map_or_else(|| default_title.to_owned(), |t| t.node_text());
    let author = root
        .nodes("author")
        .iter()
        .map(|node| {
            node.children
                .iter()
                .filter(|c| {
                    matches!(
                        c.local(),
                        "first-name" | "middle-name" | "last-name" | "nickname"
                    )
                })
                .map(Element::node_text)
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect::<Vec<_>>()
        .join(", ");
    let mut binaries: HashMap<Option<String>, Vec<u8>> = HashMap::new();
    for binary in root.nodes("binary") {
        let text = strip(&binary.itertext()).to_owned();
        let decoded = text
            .is_ascii()
            .then(|| b64_lenient(&text))
            .flatten()
            .ok_or_else(|| error("Ảnh nhúng FB2 không hợp lệ."))?;
        binaries.insert(binary.get("id").map(str::to_owned), decoded);
    }
    fn render(
        node: &Element,
        binaries: &HashMap<Option<String>, Vec<u8>>,
    ) -> Result<String, ApiError> {
        let tag = node.local();
        if tag == "image" {
            let reference = node.get_local("href").unwrap_or_default();
            let data = reference
                .strip_prefix('#')
                .and_then(|id| binaries.get(&Some(id.to_owned())))
                .ok_or_else(|| error("FB2 thiếu ảnh nhúng được tham chiếu."))?;
            return Ok(format!("<img src=\"{}\" alt=\"\">", image_url(data)?));
        }
        let mut inner = escape(node.text.as_deref().unwrap_or_default())?;
        for child in &node.children {
            inner.push_str(&render(child, binaries)?);
            inner.push_str(&escape(child.tail.as_deref().unwrap_or_default())?);
        }
        let target = match tag {
            "p" | "v" => "p",
            "subtitle" => "h3",
            "strong" => "strong",
            "emphasis" => "em",
            "strikethrough" => "s",
            "code" => "code",
            "epigraph" | "cite" => "blockquote",
            "title" => "h2",
            "empty-line" => "br",
            _ => return Ok(inner),
        };
        Ok(if target == "br" {
            format!("<br>{inner}")
        } else {
            format!("<{target}>{inner}</{target}>")
        })
    }
    fn section(
        node: &Element,
        fallback: &str,
        binaries: &HashMap<Option<String>, Vec<u8>>,
        chapters: &mut Vec<Value>,
    ) -> Result<(), ApiError> {
        let title = node
            .children
            .iter()
            .find(|c| c.local() == "title")
            .map_or_else(|| fallback.to_owned(), |t| t.node_text());
        let mut contents = String::new();
        let mut any = false;
        for child in &node.children {
            if child.local() == "section" {
                if any {
                    chapters.push(chapter(&title, &contents)?);
                    contents.clear();
                    any = false;
                }
                section(child, &title, binaries, chapters)?;
            } else {
                contents.push_str(&render(child, binaries)?);
                any = true;
            }
        }
        if any {
            let item = chapter(&title, &contents)?;
            if !item["text"].as_str().unwrap_or_default().is_empty()
                || !item["images"].as_array().is_none_or(Vec::is_empty)
            {
                chapters.push(item);
            }
        }
        Ok(())
    }
    let mut chapters = Vec::new();
    for body in root.children.iter().filter(|c| c.local() == "body") {
        section(body, &name, &binaries, &mut chapters)?;
    }
    let mut book = Map::new();
    book.insert("name".into(), name.into());
    book.insert("author".into(), author.into());
    Ok((book, chapters))
}

// ---------------------------------------------------------------- EPUB and Office

fn epub(archive: &mut Archive, title: &str) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    let container = parse_xml(&archive.read("META-INF/container.xml")?)?;
    let opf_path = container
        .nodes("rootfile")
        .first()
        .and_then(|r| r.get("full-path"))
        .filter(|p| !p.is_empty())
        .map(str::to_owned)
        .ok_or_else(|| error("EPUB thiếu đường dẫn package."))?;
    let opf_path = Archive::resolve("", &opf_path)?;
    let package = parse_xml(&archive.read(&opf_path)?)?;
    if package.local() != "package" {
        return Err(error("EPUB package không hợp lệ."));
    }
    let mut book = Map::new();
    book.insert(
        "name".into(),
        package
            .nodes("title")
            .first()
            .map_or_else(|| title.to_owned(), |n| n.node_text())
            .into(),
    );
    book.insert(
        "author".into(),
        package
            .nodes("creator")
            .iter()
            .map(|n| n.node_text())
            .collect::<Vec<_>>()
            .join(", ")
            .into(),
    );
    book.insert(
        "description".into(),
        package
            .nodes("description")
            .first()
            .map_or_else(String::new, |n| n.node_text())
            .into(),
    );
    let items = package.nodes("item");
    let mut manifest: HashMap<Option<&str>, &Element> = HashMap::new();
    let mut order = Vec::new();
    for item in &items {
        let key = item.get("id");
        if manifest.insert(key, item).is_none() {
            order.push(key);
        }
    }
    let cover_id = package
        .nodes("meta")
        .into_iter()
        .find(|n| casefold(n.get("name").unwrap_or_default()) == "cover")
        .map(|n| n.get("content"));
    let cover = order
        .iter()
        .map(|k| manifest[k])
        .find(|item| {
            item.get("properties")
                .unwrap_or_default()
                .split(python_whitespace)
                .any(|p| p == "cover-image")
        })
        .or_else(|| cover_id.and_then(|id| manifest.get(&id).copied()));
    if let Some(href) = cover.and_then(|c| c.get("href")).filter(|h| !h.is_empty()) {
        let url = archive.image(&opf_path, href, false)?;
        book.insert("cover".into(), url.into());
    }
    let spine = package.nodes("itemref");
    if spine.is_empty() {
        return Err(error("EPUB không có spine/mục lục đọc."));
    }
    let mut chapters = Vec::new();
    let mut seen = std::collections::HashSet::new();
    for reference in spine {
        let item = manifest
            .get(&reference.get("idref"))
            .filter(|item| item.get("href").is_some_and(|h| !h.is_empty()))
            .ok_or_else(|| error("EPUB spine tham chiếu mục manifest không tồn tại."))?;
        let media = item.get("media-type");
        if !matches!(media, None | Some("application/xhtml+xml" | "text/html")) {
            return Err(error(format!(
                "EPUB spine chứa định dạng chưa hỗ trợ: {}",
                media.unwrap_or("None")
            )));
        }
        let member = Archive::resolve(&opf_path, item.get("href").unwrap_or_default())?;
        let member = archive.member_name(&member)?.unwrap_or(member);
        if !seen.insert(member.clone()) {
            return Err(error(
                "EPUB spine lặp lại cùng tài liệu; từ chối khuếch đại nội dung.",
            ));
        }
        let (text, encoding) = decode_text(&archive.read(&member)?, true)?;
        if encoding == "utf-8 (repaired)" {
            let message =
                format!("UTF-8 có byte hỏng; ký tự không khôi phục được hiển thị bằng �: {member}");
            match archive.warnings.iter_mut().find(|(k, _)| *k == member) {
                Some(entry) => entry.1 = message,
                None => archive.warnings.push((member.clone(), message)),
            }
        }
        let base = member.clone();
        let mut loader = |reference: &str| archive.image(&base, reference, true);
        let mut parser = Html::parse(&text, Some(&mut loader))?;
        let fallback = if parser.title.is_empty() {
            stem(&member).to_owned()
        } else {
            parser.title.clone()
        };
        chapters.extend(parser.chapters(&fallback)?);
    }
    book.insert(
        "warnings".into(),
        archive
            .warnings
            .iter()
            .map(|(_, v)| Value::from(v.as_str()))
            .collect::<Vec<_>>()
            .into(),
    );
    Ok((book, chapters))
}

fn office(
    mut archive: Option<&mut Archive>,
    extension: &str,
    title: &str,
    flat: Option<&[u8]>,
) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    let word = matches!(extension, ".docx" | ".docm" | ".dotx" | ".dotm");
    let member = if word {
        "word/document.xml"
    } else {
        "content.xml"
    };
    let root = match (flat, archive.as_mut()) {
        (Some(data), _) => parse_xml(data)?,
        (None, Some(archive)) => parse_xml(&archive.read(member)?)?,
        (None, None) => return Err(error("Cấu trúc tài liệu Office không hợp lệ.")),
    };
    let valid = if word {
        root.local() == "document"
    } else {
        matches!(root.local(), "document" | "document-content")
    };
    if !valid {
        return Err(error("Cấu trúc tài liệu Office không hợp lệ."));
    }
    let mut book = Map::new();
    book.insert("name".into(), title.into());
    book.insert("author".into(), "".into());
    let mut relations: HashMap<Option<String>, String> = HashMap::new();
    if let Some(archive) = archive.as_mut() {
        let meta = if word {
            "docProps/core.xml"
        } else {
            "meta.xml"
        };
        if archive.contains(meta) {
            let metadata = parse_xml(&archive.read(meta)?)?;
            if let Some(name) = metadata
                .nodes("title")
                .first()
                .map(|n| n.node_text())
                .filter(|t| !t.is_empty())
            {
                book.insert("name".into(), name.into());
            }
            book.insert(
                "author".into(),
                metadata
                    .nodes("creator")
                    .iter()
                    .map(|n| n.node_text())
                    .collect::<Vec<_>>()
                    .join(", ")
                    .into(),
            );
        }
        if word && archive.contains("word/_rels/document.xml.rels") {
            let rels = parse_xml(&archive.read("word/_rels/document.xml.rels")?)?;
            for relation in rels.nodes("Relationship") {
                if relation.get("TargetMode") != Some("External") {
                    relations.insert(
                        relation.get("Id").map(str::to_owned),
                        relation.get("Target").unwrap_or_default().to_owned(),
                    );
                }
            }
        }
    }
    fn odt_text(node: &Element) -> Result<String, ApiError> {
        let mut out = node.text.clone().unwrap_or_default();
        for child in &node.children {
            match child.local() {
                "line-break" => out.push('\n'),
                "tab" => out.push('\t'),
                "s" => {
                    let count = crate::library::integer(child.get_local("c").unwrap_or("1"))?;
                    out.push_str(&" ".repeat(count.clamp(0, 1000) as usize));
                }
                _ => out.push_str(&odt_text(child)?),
            }
            out.push_str(child.tail.as_deref().unwrap_or_default());
        }
        Ok(out)
    }
    let mut events = Vec::new();
    for node in root.iter() {
        let tag = node.local();
        if !(tag == "p" || !word && tag == "h") {
            continue;
        }
        let text = if word {
            node.iter()
                .into_iter()
                .map(|c| match c.local() {
                    "t" => c.text.clone().unwrap_or_default(),
                    "br" | "cr" => "\n".into(),
                    "tab" => "\t".into(),
                    _ => String::new(),
                })
                .collect::<String>()
        } else {
            odt_text(node)?
        };
        let heading = if word {
            let style = node
                .nodes("pStyle")
                .first()
                .and_then(|s| s.attrib.first())
                .map_or("", |(_, v)| v.as_str());
            re!(r"(?i)^heading\s*[12]\n?$").is_match(style)
        } else {
            tag == "h"
        };
        let mut html = if heading {
            format!("<h2>{}</h2>", escape(&text)?)
        } else {
            format!("<p>{}</p>", escape(&text)?.replace('\n', "<br>"))
        };
        for image in node.iter() {
            if word && image.local() == "blip" {
                let id = image.get_local("embed").unwrap_or_default();
                let target = relations
                    .get(&Some(id.to_owned()))
                    .ok_or_else(|| error("DOCX thiếu liên kết ảnh nhúng."))?
                    .clone();
                let archive = archive
                    .as_mut()
                    .ok_or_else(|| error("DOCX thiếu liên kết ảnh nhúng."))?;
                html.push_str(&format!(
                    "<img src=\"{}\" alt=\"\">",
                    archive.image(member, &target, true)?
                ));
            } else if !word && image.local() == "image" {
                let href = image.get_local("href").unwrap_or_default();
                if let Some(archive) = archive.as_mut().filter(|_| !href.is_empty()) {
                    html.push_str(&format!(
                        "<img src=\"{}\" alt=\"\">",
                        archive.image(member, href, true)?
                    ));
                }
            }
        }
        events.push(((heading && !text.is_empty()).then_some(text), html));
    }
    let name = book["name"].as_str().unwrap_or_default().to_owned();
    Ok((book, sections(events, &name)?))
}

fn parse_bytes(
    data: &[u8],
    extension: &str,
    title: &str,
    image: Option<&mut ImageLoader<'_>>,
) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    if extension == ".fb2" {
        return fb2(data, title);
    }
    if extension == ".fodt" {
        return office(None, extension, title, Some(data));
    }
    let (text, encoding) = decode_text(data, false)?;
    let mut book = Map::new();
    let chapters = if matches!(extension, ".html" | ".htm" | ".xhtml") {
        let mut parser = Html::parse(&text, image)?;
        let name = if parser.title.is_empty() {
            title.to_owned()
        } else {
            parser.title.clone()
        };
        book.insert("name".into(), name.clone().into());
        parser.chapters(&name)?
    } else {
        book.insert("name".into(), title.into());
        text_chapters(&text, title, matches!(extension, ".md" | ".markdown"))?
    };
    book.insert("encoding".into(), encoding.into());
    Ok((book, chapters))
}

fn parse_archive(
    path: &Path,
    extension: &str,
    title: &str,
) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    let mut archive = Archive::open(path)?;
    if extension == ".epub" {
        return epub(&mut archive, title);
    }
    if OFFICE_EXTENSIONS.contains(&extension) {
        return office(Some(&mut archive), extension, title, None);
    }
    let mut names = archive.members.clone();
    names.sort_by(|a, b| natural_cmp(a, b));
    let (mut images, mut texts, mut ignored) = (Vec::new(), Vec::new(), Vec::new());
    for name in names {
        let extension_of = suffix(&name).to_lowercase();
        if name.starts_with("__MACOSX/") || posix_name(&name).starts_with('.') {
            continue;
        }
        if IMAGE_EXTENSIONS.contains(&extension_of.as_str()) {
            images.push(name);
        } else if TEXT_EXTENSIONS.contains(&extension_of.as_str()) && extension != ".cbz" {
            texts.push(name);
        } else {
            ignored.push(name);
        }
    }
    if extension == ".fbz" && (texts.len() != 1 || suffix(&texts[0]).to_lowercase() != ".fb2") {
        return Err(error("FBZ phải chứa đúng một tài liệu FB2."));
    }
    let mut book = Map::new();
    book.insert("name".into(), title.into());
    let mut chapters = Vec::new();
    let single = texts.len() == 1;
    for member in &texts {
        let data = archive.read(member)?;
        let base = member.clone();
        let mut loader = |reference: &str| archive.image(&base, reference, true);
        let (metadata, member_chapters) = parse_bytes(
            &data,
            &suffix(member).to_lowercase(),
            stem(member),
            Some(&mut loader),
        )?;
        if single {
            book.extend(metadata);
        }
        chapters.extend(member_chapters);
    }
    if !images.is_empty() && texts.is_empty() {
        let mut groups: Vec<(String, Vec<String>)> = Vec::new();
        for member in images {
            let folder = parent(&member).to_owned();
            match groups.iter_mut().find(|(f, _)| *f == folder) {
                Some((_, members)) => members.push(member),
                None => groups.push((folder, vec![member])),
            }
        }
        for (folder, members) in groups {
            let mut html = String::new();
            for member in members {
                let url = image_url(&archive.read(&member)?)?;
                html.push_str(&format!(
                    "<img src=\"{url}\" alt=\"{}\">",
                    escape(posix_name(&member))?
                ));
            }
            chapters.push(chapter(if folder == "." { title } else { &folder }, &html)?);
        }
        book.insert("kind".into(), "comic".into());
    }
    if !ignored.is_empty() {
        book.insert(
            "warnings".into(),
            json!([format!(
                "Đã bỏ qua các mục không phải nội dung đọc: {}",
                ignored
                    .iter()
                    .take(20)
                    .cloned()
                    .collect::<Vec<_>>()
                    .join(", ")
            )]),
        );
    }
    Ok((book, chapters))
}

pub(crate) fn number_chapters(chapters: &mut [Value]) {
    for (index, chapter) in chapters.iter_mut().enumerate() {
        chapter["index"] = index.into();
    }
}

pub(crate) fn base_book(path: &Path, extension: &str, count: usize) -> Map<String, Value> {
    let resolved = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_owned());
    let resolved = resolved.to_string_lossy();
    let resolved = resolved.strip_prefix(r"\\?\").unwrap_or(&resolved);
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let mut book = Map::new();
    book.insert("name".into(), stem(&name).into());
    book.insert("author".into(), "".into());
    book.insert("kind".into(), "text".into());
    book.insert("format".into(), extension[1..].into());
    book.insert("description".into(), "".into());
    book.insert("source".into(), "local".into());
    book.insert("path".into(), resolved.into());
    book.insert("chapter_count".into(), count.into());
    book
}

pub(crate) fn unsupported(extension: &str) -> ApiError {
    let label = if extension == ".pdf" {
        "PDF/OCR".to_owned()
    } else {
        match extension.to_uppercase().trim_start_matches('.') {
            "" => "không có phần mở rộng".to_owned(),
            label => label.to_owned(),
        }
    };
    error(format!(
        "Chưa hỗ trợ {label}: cần engine chuyển đổi thực đã tích hợp và kiểm thử. Xem capabilities()."
    ))
}

pub(crate) fn import_document(path: &Path) -> Result<Value, ApiError> {
    if !path.is_file() {
        return Err(error("Không tìm thấy tệp tài liệu."));
    }
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let extension = suffix(&name).to_lowercase();
    if !supported_extensions().contains(&extension.as_str()) {
        return Err(unsupported(&extension));
    }
    if path.metadata().map_err(unreadable)?.len() > MAX_FILE_SIZE {
        return Err(error("Tệp vượt giới hạn 128 MiB."));
    }
    let _budget = BudgetScope::new();
    let title = stem(&name).to_owned();
    let (metadata, mut chapters) = if TEXT_EXTENSIONS.contains(&extension.as_str()) {
        parse_bytes(
            &std::fs::read(path).map_err(unreadable)?,
            &extension,
            &title,
            None,
        )?
    } else {
        parse_archive(path, &extension, &title)?
    };
    if chapters.is_empty() {
        return Err(error("Tài liệu không có văn bản hoặc ảnh đọc được."));
    }
    number_chapters(&mut chapters);
    let mut book = base_book(path, &extension, chapters.len());
    book.extend(metadata);
    let warnings = book.shift_remove("warnings").unwrap_or_else(|| json!([]));
    Ok(json!({"book": book, "chapters": chapters, "warnings": warnings}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splitlines_and_suffix_follow_python() {
        assert_eq!(
            splitlines("a\x1cb\u{85}c\u{2028}d\re\r\nf\x0bg"),
            ["a", "b", "c", "d", "e", "f", "g"]
        );
        assert_eq!(splitlines("a\n\nb\n"), ["a", "", "b"]);
        assert_eq!((suffix("foo."), stem("foo.")), (".", "foo"));
        assert_eq!((suffix(".hidden"), suffix("..")), ("", ""));
        assert_eq!(parent("a/b/c.png"), "a/b");
        assert_eq!(parent("c.png"), ".");
    }

    #[test]
    fn natural_order_compares_numbers() {
        let mut names = vec!["p10.png", "p2.png", "P1.png", "p02.png"];
        names.sort_by(|a, b| natural_cmp(a, b));
        assert_eq!(names, ["P1.png", "p2.png", "p02.png", "p10.png"]);
    }

    #[test]
    fn decode_text_uses_declared_then_fallback_codecs() {
        assert_eq!(
            decode_text(b"abc", false).unwrap(),
            ("abc".into(), "utf-8".into())
        );
        assert_eq!(decode_text("Chương".as_bytes(), false).unwrap().1, "utf-8");
        let (_, label) = decode_text(b"<?xml encoding='windows-1258'?>\xe0", false).unwrap();
        assert_eq!(label, "windows-1258");
        assert!(decode_text(b"a\x00b", false).is_err());
        let (text, label) = decode_text(b"\xef\xbb\xbf\xef\xbb\xbfx\r\ny", false).unwrap();
        assert_eq!((text.as_str(), label.as_str()), ("x\ny", "utf-8-sig"));
        assert_eq!(decode_text(b"\xff\xfeh\x00i\x00", false).unwrap().0, "hi");
    }

    #[test]
    fn html_parser_handles_cdata_comments_and_entities() {
        let tokens =
            tokenize("<P CLASS=x>a &amp; b<!-- c --><script>if (a<b) x</script><br/>").unwrap();
        let summary = tokens
            .iter()
            .map(|t| match t {
                Token::Start(tag, attrs, closed) => format!("S{tag}{attrs:?}{closed}"),
                Token::End(tag) => format!("E{tag}"),
                Token::Data(d) => format!("D{d}"),
            })
            .collect::<Vec<_>>();
        assert_eq!(
            summary,
            [
                "Sp[(\"class\", Some(\"x\"))]false",
                "Da & b",
                "Sscript[]false",
                "Dif (a<b) x",
                "Escript",
                "Sbr[]true"
            ]
        );
    }

    #[test]
    fn html_declarations_with_multibyte_text_do_not_panic() {
        for source in [
            "<!ếtc>x",
            "<!ế",
            "</ế>y",
            "<?ế>z",
            "<!--ế-->w",
            "<a title='ế'",
        ] {
            tokenize(source).unwrap();
        }
    }

    #[test]
    fn text_chapters_split_headings_and_markdown() {
        let _budget = BudgetScope::new();
        let chapters = text_chapters("Intro\nChương 1 Mở đầu\nA *b* **c**\n", "T", true).unwrap();
        assert_eq!(chapters.len(), 2);
        assert_eq!(chapters[1]["title"], "Chương 1 Mở đầu");
        assert_eq!(
            chapters[1]["html"],
            "<h2>Chương 1 Mở đầu</h2><p>A <em>b</em> <strong>c</strong></p>"
        );
        assert_eq!(chapters[1]["text"], "Chương 1 Mở đầu\n\nA b c");
    }

    #[test]
    fn xml_tree_matches_elementtree_text_and_tail() {
        let root = parse_xml_text("<a xmlns:x='u'><b x:k='1\n2'>t&amp;</b>tail<c/></a>").unwrap();
        assert_eq!(root.children[0].tag, "b");
        assert_eq!(root.children[0].get("{u}k"), Some("1 2"));
        assert_eq!(root.children[0].text.as_deref(), Some("t&"));
        assert_eq!(root.children[0].tail.as_deref(), Some("tail"));
        assert_eq!(root.itertext(), "t&tail");
        assert!(parse_xml_text("<a><b></a>").is_err());
        assert!(parse_xml_text("<a>&nbsp;</a>").is_err());
        assert!(parse_xml_text("<a/><b/>").is_err());
        assert!(parse_xml_text("<x:a/>").is_err());
    }

    #[test]
    fn deep_xml_is_rejected_while_parsing() {
        let deep = format!("<r>{}x{}</r>", "<a>".repeat(50_000), "</a>".repeat(50_000));
        assert_eq!(
            parse_xml_text(&deep).unwrap_err().message,
            "XML lồng quá sâu."
        );
        let limit = format!("{}x{}", "<a>".repeat(101), "</a>".repeat(101));
        assert!(parse_xml_text(&limit).is_ok());
        let over = format!("{}x{}", "<a>".repeat(102), "</a>".repeat(102));
        assert!(parse_xml_text(&over).is_err());
    }

    #[test]
    fn raw_text_elements_tokenize_in_linear_time() {
        let many = "<title>a</title><SCRIPT>b</script >".repeat(100_000);
        let start = std::time::Instant::now();
        assert_eq!(tokenize(&many).unwrap().len(), 600_000);
        assert!(
            start.elapsed() < std::time::Duration::from_secs(20),
            "{:?}",
            start.elapsed()
        );
        let tokens = tokenize("<plaintext>a</plaintext><b>").unwrap();
        assert!(matches!(tokens.last(), Some(Token::Data(d)) if d == "a</plaintext><b>"));
    }

    #[test]
    fn budget_rejects_oversized_output() {
        let _budget = BudgetScope::new();
        assert!(reserve(MAX_OUTPUT_SIZE).is_ok());
        assert!(reserve(1).is_err());
    }
}
