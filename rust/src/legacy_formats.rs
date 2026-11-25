//! Bounded text imports for published RTF, HWPX and HWPML structures.
//!
//! Reference: Python `legacy_formats.py`. Format references: Microsoft RTF 1.6
//! specification, Hancom HWPX format notes and HWPML revision 1.2.

use crate::{
    ApiError,
    casefold::casefold,
    codepages::lookup,
    documents::{
        self, Archive, Element, MAX_FILE_SIZE, MAX_MEMBER_SIZE, error, html_escape, html_text,
        image_url, parse_xml, sections, text_chapters,
    },
};
use serde_json::{Map, Value, json};
use std::{
    collections::{BTreeSet, HashMap},
    path::Path,
};

pub(crate) const SUPPORTED_EXTENSIONS: &[&str] = &[".hwpml", ".hwpx", ".rtf"];

pub(crate) fn capabilities() -> Value {
    json!({
        "supported_extensions": SUPPORTED_EXTENSIONS,
        "rtf": {"supported": true, "content": "Văn bản Unicode/ANSI, nhóm, font charset, đoạn, bảng dạng chữ; không chạy đối tượng hoặc mã nhúng."},
        "hwpx": {"supported": true, "content": "Thứ tự spine, metadata, đoạn và bảng, ảnh raster nhúng; script/OLE không chạy."},
        "hwpml": {"supported": true, "content": "Metadata, section, đoạn, bảng XML; ảnh/đối tượng chưa chuyển đổi."},
        "unsupported_extensions": [".mobi", ".prc", ".azw", ".azw3", ".umd", ".chm", ".doc", ".dot", ".wps", ".hwp"],
        "limits": {"file_bytes": MAX_FILE_SIZE, "text_bytes": MAX_MEMBER_SIZE, "group_depth": 100},
        "layout": "Nhập để đọc văn bản, không bảo toàn chính xác bố cục, phông hoặc dàn trang gốc.",
    })
}

// ---------------------------------------------------------------- RTF

const DESTINATIONS: &[&str] = &[
    "fonttbl",
    "colortbl",
    "stylesheet",
    "info",
    "pict",
    "object",
    "objdata",
    "datastore",
    "filetbl",
    "fldinst",
    "generator",
    "listtable",
    "listoverridetable",
    "rsidtbl",
    "themedata",
    "colorschememapping",
    "latentstyles",
    "shppict",
    "nonshppict",
    "header",
    "headerl",
    "headerr",
    "headerf",
    "footer",
    "footerl",
    "footerr",
    "footerf",
    "annotation",
    "xmlnstbl",
    "xmlopen",
    "xmlclose",
    "xmlattrname",
    "xmlattrvalue",
];

fn control_text(word: &str) -> Option<&'static str> {
    Some(match word {
        "par" | "line" | "row" => "\n",
        "page" | "sect" => "\n\n",
        "tab" | "cell" => "\t",
        "emdash" => "—",
        "endash" => "–",
        "bullet" => "•",
        "lquote" => "‘",
        "rquote" => "’",
        "ldblquote" => "“",
        "rdblquote" => "”",
        "enspace" => "\u{2002}",
        "emspace" => "\u{2003}",
        "qmspace" => "\u{2005}",
        _ => return None,
    })
}

fn charset(number: i64) -> Option<Option<&'static str>> {
    Some(match number {
        0 | 1 => None,
        2 => Some("symbol"),
        77 => Some("mac_roman"),
        128 => Some("cp932"),
        129 => Some("cp949"),
        130 => Some("johab"),
        134 => Some("gbk"),
        136 => Some("big5"),
        161 => Some("cp1253"),
        162 => Some("cp1254"),
        163 => Some("cp1258"),
        177 => Some("cp1255"),
        178 => Some("cp1256"),
        186 => Some("cp1257"),
        204 => Some("cp1251"),
        222 => Some("cp874"),
        238 => Some("cp1250"),
        255 => Some("cp437"),
        _ => return None,
    })
}

fn codepage(number: i64) -> Result<String, ApiError> {
    let unsupported = || error(format!("RTF dùng codepage chưa được hỗ trợ: {number}"));
    // Python on Windows also resolves cpNNN through the OS code page tables.
    let alias = match number {
        65001 => "utf_8".to_owned(),
        950 => "big5".to_owned(),
        28591 => "latin_1".to_owned(),
        20127 => "ascii".to_owned(),
        _ => format!("cp{number}"),
    };
    lookup(&alias)
        .map(|(name, _)| name.to_owned())
        .ok_or_else(unsupported)
}

#[derive(Clone)]
struct State {
    skip: bool,
    hidden: bool,
    encoding: String,
    uc: i64,
    font: Option<i64>,
    fonttbl: bool,
    upr_pending: u8,
    unicode_branch: bool,
    parent_skip: bool,
}

struct Rtf {
    state: Option<State>,
    units: Vec<u16>,
    pending: Vec<u8>,
    fonts: HashMap<Option<i64>, Option<String>>,
    fallback: i64,
    default_font: i64,
}

impl Rtf {
    fn state(&mut self) -> &mut State {
        self.state.as_mut().expect("inside the root group")
    }

    fn visible(&self) -> bool {
        self.state.as_ref().is_some_and(|s| !s.skip && !s.hidden)
    }

    fn flush(&mut self) -> Result<(), ApiError> {
        if self.pending.is_empty() {
            return Ok(());
        }
        let state = self.state.as_ref().expect("inside the root group");
        let font = state.font.unwrap_or(self.default_font);
        let encoding = self
            .fonts
            .get(&Some(font))
            .cloned()
            .flatten()
            .unwrap_or_else(|| state.encoding.clone());
        if encoding == "symbol" {
            return Err(error("RTF dùng font Symbol chưa có ánh xạ Unicode."));
        }
        let text = lookup(&documents::normalize_codec(&encoding))
            .and_then(|(_, codec)| documents::decode_with(&self.pending, codec))
            .ok_or_else(|| {
                error(format!(
                    "RTF có dữ liệu không hợp lệ cho encoding {encoding}"
                ))
            })?;
        self.units.extend(text.encode_utf16());
        self.pending.clear();
        Ok(())
    }

    fn emit_bytes(&mut self, value: &[u8]) {
        let skip = (self.fallback.max(0) as usize).min(value.len());
        self.fallback -= skip as i64;
        if self.visible() {
            self.pending.extend_from_slice(&value[skip..]);
        }
    }

    fn emit(&mut self, value: &str) -> Result<(), ApiError> {
        self.flush()?;
        if self.fallback > 0 {
            self.fallback -= 1;
        } else if self.visible() {
            self.units.extend(value.encode_utf16());
        }
        Ok(())
    }
}

pub(crate) fn rtf_text(data: &[u8]) -> Result<String, ApiError> {
    let signature = data
        .iter()
        .position(|b| !b" \t\n\r\x0b\x0c".contains(b))
        .is_some_and(|at| {
            data[at..].starts_with(b"{\\rtf1")
                && !data
                    .get(at + 6)
                    .is_some_and(|b| b.is_ascii_alphanumeric() || *b == b'_')
        });
    if data.len() > MAX_MEMBER_SIZE || !signature {
        return Err(error("RTF thiếu chữ ký {\\rtf1 hoặc vượt giới hạn 64 MiB."));
    }
    let mut stack: Vec<Option<State>> = Vec::new();
    let mut rtf = Rtf {
        state: None,
        units: Vec::new(),
        pending: Vec::new(),
        fonts: HashMap::new(),
        fallback: 0,
        default_font: 0,
    };
    let mut index = 0;
    let mut root_closed = false;
    while index < data.len() {
        let c = data[index];
        if rtf.state.is_none() && !b" \r\n\t{".contains(&c) {
            return Err(error("RTF có nội dung ngoài nhóm gốc."));
        }
        if c == b'{' {
            if root_closed {
                return Err(error("RTF có nhiều nhóm gốc."));
            }
            if stack.len() >= 100 {
                return Err(error("RTF lồng nhóm quá sâu."));
            }
            if rtf.state.is_some() {
                rtf.flush()?;
            }
            match rtf.state.take() {
                None => {
                    stack.push(None);
                    rtf.state = Some(State {
                        skip: false,
                        hidden: false,
                        encoding: "cp1252".into(),
                        uc: 1,
                        font: None,
                        fonttbl: false,
                        upr_pending: 0,
                        unicode_branch: false,
                        parent_skip: false,
                    });
                }
                Some(mut parent) => {
                    let mut child = parent.clone();
                    child.parent_skip = parent.skip;
                    child.unicode_branch = parent.upr_pending == 1;
                    if parent.upr_pending == 2 {
                        child.skip = true;
                    }
                    parent.upr_pending = parent.upr_pending.saturating_sub(1);
                    child.upr_pending = 0;
                    stack.push(Some(parent));
                    rtf.state = Some(child);
                }
            }
            rtf.fallback = 0;
            index += 1;
            continue;
        }
        if c == b'}' {
            let Some(previous) = stack.pop() else {
                return Err(error("RTF đóng nhóm không có nhóm mở."));
            };
            rtf.flush()?;
            rtf.state = previous;
            root_closed = rtf.state.is_none();
            rtf.fallback = 0;
            index += 1;
            continue;
        }
        if rtf.state.is_none() || c == b'\r' || c == b'\n' {
            index += 1;
            continue;
        }
        if c != b'\\' {
            let end = data[index + 1..]
                .iter()
                .position(|b| b"\\{}\r\n".contains(b))
                .map_or(data.len(), |at| index + 1 + at);
            rtf.emit_bytes(&data[index..end]);
            index = end;
            continue;
        }
        let Some(&symbol) = data.get(index + 1) else {
            return Err(error("RTF kết thúc giữa escape."));
        };
        if symbol == b'\'' {
            let hex = data
                .get(index + 2..index + 4)
                .filter(|h| h.iter().all(u8::is_ascii_hexdigit));
            let Some(hex) = hex else {
                return Err(error("RTF có hex escape không hợp lệ."));
            };
            let value = u8::from_str_radix(std::str::from_utf8(hex).expect("hex digits"), 16)
                .expect("hex digits");
            rtf.emit_bytes(&[value]);
            index += 4;
            continue;
        }
        if !symbol.is_ascii_alphabetic() {
            index += 2;
            match symbol {
                b'*' => {
                    rtf.flush()?;
                    rtf.state().skip = true;
                }
                b'\\' | b'{' | b'}' => rtf.emit_bytes(&[symbol]),
                b'~' => rtf.emit("\u{a0}")?,
                b'_' => rtf.emit("\u{2011}")?,
                b'-' => rtf.emit("\u{ad}")?,
                b'\n' | b'\r' => rtf.emit("\n")?,
                _ => {}
            }
            continue;
        }
        // CONTROL_WORD: \\([A-Za-z]+)(-?\d+)? ?
        let start = index + 1;
        let mut at = start;
        while data.get(at).is_some_and(u8::is_ascii_alphabetic) {
            at += 1;
        }
        let word = std::str::from_utf8(&data[start..at])
            .expect("ASCII letters")
            .to_owned();
        let digits_start = at;
        let mut probe = at;
        if data.get(probe) == Some(&b'-') {
            probe += 1;
        }
        let mut digits_end = probe;
        while data.get(digits_end).is_some_and(u8::is_ascii_digit) {
            digits_end += 1;
        }
        let parameter = (digits_end > probe).then(|| &data[digits_start..digits_end]);
        at = if parameter.is_some() { digits_end } else { at };
        if data.get(at) == Some(&b' ') {
            at += 1;
        }
        if word.len() > 32 || parameter.is_some_and(|p| p.len() > 11) {
            return Err(error("RTF có control word hoặc tham số quá dài."));
        }
        let number = parameter.map(|p| {
            std::str::from_utf8(p)
                .expect("ASCII digits")
                .parse::<i64>()
                .expect("11 digits")
        });
        index = at;
        let word = word.as_str();
        if word == "bin" {
            rtf.flush()?;
            match number {
                Some(n) if n >= 0 && n as usize <= data.len() - index => index += n as usize,
                _ => return Err(error("RTF có dữ liệu nhị phân bị cắt cụt.")),
            }
        } else if DESTINATIONS.contains(&word) {
            rtf.flush()?;
            rtf.state().skip = true;
            if word == "fonttbl" {
                rtf.state().fonttbl = true;
            }
        } else if word == "ud" && rtf.state().unicode_branch {
            let skip = rtf.state().parent_skip;
            rtf.state().skip = skip;
        } else if word == "upr" {
            rtf.state().upr_pending = 2;
        } else if word == "uc" {
            match number {
                Some(n @ 0..=16) => rtf.state().uc = n,
                _ => return Err(error("RTF có giá trị Unicode fallback không hợp lệ.")),
            }
        } else if word == "u" {
            rtf.flush()?;
            let unit = match number {
                Some(n)
                    if (-32768..=65535).contains(&n)
                        && !matches!(n.rem_euclid(65536), 65534 | 65535) =>
                {
                    n.rem_euclid(65536) as u16
                }
                _ => return Err(error("RTF có UTF-16 code unit không hợp lệ.")),
            };
            if rtf.visible() {
                rtf.units.push(unit);
            }
            rtf.fallback = rtf.state().uc;
        } else if matches!(word, "ansi" | "mac" | "pc" | "pca" | "ansicpg") {
            rtf.flush()?;
            rtf.state().encoding = match (word, number) {
                ("ansicpg", Some(n)) => codepage(n)?,
                ("mac", _) => "mac_roman".into(),
                ("pc", _) => "cp437".into(),
                ("pca", _) => "cp850".into(),
                _ => "cp1252".into(),
            };
        } else if word == "deff" && number.is_some() {
            rtf.default_font = number.expect("checked");
        } else if word == "f" && number.is_some() {
            rtf.flush()?;
            rtf.state().font = number;
        } else if matches!(word, "fcharset" | "cpg") && rtf.state().fonttbl {
            let Some(n) = number else {
                return Err(error("RTF font thiếu charset/codepage."));
            };
            let encoding = if word == "cpg" {
                Some(codepage(n)?)
            } else {
                charset(n)
                    .ok_or_else(|| error(format!("RTF có font charset chưa hỗ trợ: {n}")))?
                    .map(str::to_owned)
            };
            let font = rtf.state().font;
            rtf.fonts.insert(font, encoding);
        } else if word == "plain" {
            rtf.flush()?;
            rtf.state().hidden = false;
            rtf.state().font = None;
        } else if word == "v" {
            rtf.flush()?;
            rtf.state().hidden = number != Some(0);
        } else if let Some(text) = control_text(word) {
            rtf.emit(text)?;
        }
    }
    if !stack.is_empty() || !root_closed {
        return Err(error("RTF thiếu dấu đóng nhóm."));
    }
    String::from_utf16(&rtf.units).map_err(|_| error("RTF có cặp UTF-16 surrogate không hợp lệ."))
}

// ---------------------------------------------------------------- Hancom XML

fn metadata(root: &Element, title_name: &str, author_name: &str) -> Map<String, Value> {
    let mut out = Map::new();
    out.insert(
        "name".into(),
        root.nodes(title_name)
            .first()
            .map_or_else(String::new, |n| n.node_text())
            .into(),
    );
    out.insert(
        "author".into(),
        root.nodes(author_name)
            .iter()
            .map(|n| n.node_text())
            .collect::<Vec<_>>()
            .join(", ")
            .into(),
    );
    out
}

type HwpxImage<'a> = dyn FnMut(&str) -> Result<String, ApiError> + 'a;

fn render_hancom(
    node: &Element,
    warnings: &mut BTreeSet<String>,
    image: &mut Option<&mut HwpxImage<'_>>,
    hwpx: bool,
) -> Result<String, ApiError> {
    let tag = node.local();
    if matches!(
        casefold(tag).as_str(),
        "ole"
            | "object"
            | "script"
            | "scripts"
            | "equation"
            | "unknownobject"
            | "button"
            | "radiobutton"
            | "checkbox"
            | "combobox"
            | "edit"
            | "listbox"
            | "scrollbar"
            | "hiddencomment"
    ) {
        warnings.insert(
            "Đã bỏ qua script/đối tượng nhúng hoặc công thức không hỗ trợ; chỉ giữ văn bản hiển thị.".into(),
        );
        return Ok(String::new());
    }
    if !hwpx && tag == "PICTURE" {
        warnings.insert("Ảnh nhúng HWPML chưa được chuyển đổi; văn bản/bảng vẫn được giữ.".into());
        return Ok(String::new());
    }
    if hwpx && tag == "img" {
        let reference = node.get("binaryItemIDRef").filter(|r| !r.is_empty());
        let (Some(reference), Some(image)) = (reference, image.as_mut()) else {
            return Err(error("HWPX thiếu liên kết ảnh nhúng."));
        };
        return Ok(format!("<img src=\"{}\" alt=\"\">", image(reference)?));
    }
    if tag == "t" || tag == "CHAR" {
        let mut text = html_escape(node.text.as_deref().unwrap_or_default());
        for child in &node.children {
            let inline = match child.local() {
                "tab" | "TAB" => Some("\t"),
                "lineBreak" | "LINEBREAK" => Some("\n"),
                "NBSPACE" | "nbSpace" => Some("\u{a0}"),
                "FWSPACE" | "fwSpace" => Some("\u{2007}"),
                "HYPEN" => Some("\u{ad}"),
                _ => None,
            };
            match inline {
                Some(value) => text.push_str(&html_escape(value)),
                None => text.push_str(&render_hancom(child, warnings, image, hwpx)?),
            }
            text.push_str(&html_escape(child.tail.as_deref().unwrap_or_default()));
        }
        return Ok(text.replace('\n', "<br>"));
    }
    let mut inner = String::new();
    for child in &node.children {
        inner.push_str(&render_hancom(child, warnings, image, hwpx)?);
    }
    let target = match tag {
        "p" | "P" => "div",
        "tbl" | "TABLE" => "table",
        "tr" | "ROW" => "tr",
        "tc" | "CELL" => "td",
        _ => return Ok(inner),
    };
    Ok(format!("<{target}>{inner}</{target}>"))
}

fn section_chapters(
    root: &Element,
    title: &str,
    warnings: &mut BTreeSet<String>,
    mut image: Option<&mut HwpxImage<'_>>,
    hwpx: bool,
) -> Result<Vec<Value>, ApiError> {
    let mut events = Vec::new();
    for node in &root.children {
        let html = render_hancom(node, warnings, &mut image, hwpx)?;
        let text = html_text(&html)?;
        let heading = (text.chars().count() <= 180
            && !text.contains('\n')
            && documents::chapter_heading(&text))
        .then_some(text);
        events.push((heading, html));
    }
    sections(events, title)
}

fn hwpx(path: &Path) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    let mut archive = Archive::open(path)?;
    let mimetype = archive.read("mimetype")?;
    if mimetype.trim_ascii() != b"application/hwp+zip" {
        return Err(error("Tệp không có mimetype HWPX hợp lệ."));
    }
    let package = parse_xml(&archive.read("Contents/content.hpf")?)?;
    if package.local() != "package" {
        return Err(error("HWPX thiếu cấu trúc package OPF."));
    }
    let mut book = metadata(&package, "title", "creator");
    let mut manifest = HashMap::new();
    for item in package.nodes("item") {
        let (Some(id), Some(href)) = (
            item.get("id").filter(|i| !i.is_empty()),
            item.get("href").filter(|h| !h.is_empty()),
        ) else {
            return Err(error("HWPX có manifest ID/href không hợp lệ hoặc trùng."));
        };
        if manifest.contains_key(id) {
            return Err(error("HWPX có manifest ID/href không hợp lệ hoặc trùng."));
        }
        manifest.insert(id.to_owned(), Archive::resolve("", href)?);
    }
    let spine = package.nodes("itemref");
    if spine.is_empty() {
        return Err(error("HWPX thiếu spine/thứ tự đọc."));
    }
    let mut warnings = BTreeSet::new();
    if archive.members.iter().any(|m| m.starts_with("Scripts/")) {
        warnings.insert("Đã bỏ qua script trong tài liệu; không thực thi mã nhúng.".to_owned());
    }
    let mut chapters = Vec::new();
    let mut seen = std::collections::HashSet::new();
    let mut sections_seen = 0;
    for reference in spine {
        let member = reference
            .get("idref")
            .and_then(|id| manifest.get(id))
            .filter(|m| archive.contains(m))
            .ok_or_else(|| error("HWPX spine tham chiếu mục không tồn tại."))?
            .clone();
        if !seen.insert(member.clone()) {
            return Err(error("HWPX spine chứa mục lặp."));
        }
        let root = parse_xml(&archive.read(&member)?)?;
        if matches!(root.local(), "head" | "HWPApplicationSetting") {
            continue;
        }
        if root.local() != "sec" {
            return Err(error(format!(
                "HWPX spine chứa cấu trúc chưa hỗ trợ: {}",
                root.local()
            )));
        }
        sections_seen += 1;
        let mut image = |reference: &str| -> Result<String, ApiError> {
            let member = manifest
                .get(reference)
                .ok_or_else(|| error("HWPX ảnh tham chiếu ID manifest không tồn tại."))?;
            image_url(&archive.read(member)?)
        };
        chapters.extend(section_chapters(
            &root,
            &format!("Phần {sections_seen}"),
            &mut warnings,
            Some(&mut image),
            true,
        )?);
    }
    book.insert(
        "warnings".into(),
        warnings.into_iter().collect::<Vec<_>>().into(),
    );
    Ok((book, chapters))
}

fn hwpml(data: &[u8]) -> Result<(Map<String, Value>, Vec<Value>), ApiError> {
    let root = parse_xml(data)?;
    if root.local() != "HWPML" {
        return Err(error("Tệp không có cấu trúc HWPML hợp lệ."));
    }
    let summary = root
        .children
        .iter()
        .find(|c| c.local() == "HEAD")
        .and_then(|head| head.nodes("DOCSUMMARY").first().copied());
    let mut book = summary.map_or_else(Map::new, |s| metadata(s, "TITLE", "AUTHOR"));
    let mut warnings = BTreeSet::new();
    let mut chapters = Vec::new();
    let bodies = root.children.iter().filter(|c| c.local() == "BODY");
    let sections = bodies.flat_map(|b| b.children.iter().filter(|c| c.local() == "SECTION"));
    for (index, section) in sections.enumerate() {
        chapters.extend(section_chapters(
            section,
            &format!("Phần {}", index + 1),
            &mut warnings,
            None,
            false,
        )?);
    }
    book.insert(
        "warnings".into(),
        warnings.into_iter().collect::<Vec<_>>().into(),
    );
    Ok((book, chapters))
}

pub(crate) fn import_legacy(path: &Path) -> Result<Value, ApiError> {
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let extension = documents::suffix(&name).to_lowercase();
    if !SUPPORTED_EXTENSIONS.contains(&extension.as_str()) {
        return Err(error(format!(
            "Định dạng {extension} cần bộ chuyển đổi thực đã kiểm thử; không có parser trong bản này."
        )));
    }
    if !path.is_file() || path.metadata().map_err(documents::unreadable)?.len() > MAX_FILE_SIZE {
        return Err(error("Không tìm thấy tệp hoặc tệp vượt giới hạn 128 MiB."));
    }
    let title = documents::stem(&name).to_owned();
    let (metadata, mut chapters) = match extension.as_str() {
        ".rtf" => {
            let text = rtf_text(&std::fs::read(path).map_err(documents::unreadable)?)?;
            let mut metadata = Map::new();
            metadata.insert(
                "warnings".into(),
                json!([
                    "RTF giữ văn bản/đoạn; bỏ định dạng, ảnh, header/footer và đối tượng nhúng."
                ]),
            );
            (metadata, text_chapters(&text, &title, false)?)
        }
        ".hwpx" => hwpx(path)?,
        _ => hwpml(&std::fs::read(path).map_err(documents::unreadable)?)?,
    };
    if chapters.is_empty() {
        return Err(error("Tài liệu không có văn bản hoặc ảnh đọc được."));
    }
    documents::number_chapters(&mut chapters);
    let mut book = documents::base_book(path, &extension, chapters.len());
    for (key, value) in metadata {
        let truthy = match &value {
            Value::String(s) => !s.is_empty(),
            Value::Array(a) => !a.is_empty(),
            Value::Null => false,
            _ => true,
        };
        if truthy || key == "warnings" {
            book.insert(key, value);
        }
    }
    Ok(json!({"book": book, "chapters": chapters}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rtf_decodes_unicode_fallbacks_hex_and_hidden_groups() {
        let text = rtf_text(br"{\rtf1\ansi\deff0{\fonttbl{\f0\fcharset163 Times;}}\f0 Ch\'f2\u432?ng 1\par{\v hidden}{\*\generator x;}Xin \u8220?ch\'e0o\u8221?}").unwrap();
        assert_eq!(text, "Ch\u{323}ưng 1\nXin “chào”");
        assert!(rtf_text(b"{\\rtf1 a").is_err());
        assert!(rtf_text(b"{\\rtf1 a}}").is_err());
        assert!(rtf_text(b"plain").is_err());
        assert_eq!(
            rtf_text(br"{\rtf1 \u-10179?\u-8704?}").unwrap(),
            "\u{1f600}"
        );
        assert!(rtf_text(br"{\rtf1 \u-10179?}").is_err());
    }
}
