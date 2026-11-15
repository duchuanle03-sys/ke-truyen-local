use crate::{
    ApiError, Library, ShelfQuery,
    library::{checked_path, decode},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use http_body_util::Full;
use hyper::{Response, body::Bytes};
use regex::Regex;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::sync::Arc;
use std::{io::Write, path::Path};

macro_rules! cached_regex {
    ($pattern:literal) => {{
        static REGEX: std::sync::LazyLock<Regex> =
            std::sync::LazyLock::new(|| Regex::new($pattern).expect("constant regex"));
        &*REGEX
    }};
}

pub fn quote(s: &str, plus: bool) -> String {
    let mut out = String::new();
    for b in s.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            out.push(b as char)
        } else if b == b' ' && plus {
            out.push('+')
        } else {
            out.push_str(&format!("%{b:02X}"))
        }
    }
    out
}
fn escape(s: &str) -> String {
    s.replace('&', "&amp;")
        .replace('<', "&lt;")
        .replace('>', "&gt;")
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}
fn raster(s: &str) -> String {
    if s.len() > 28 * 1024 * 1024 {
        return String::new();
    };
    let Some((kind, data)) = s
        .strip_prefix("data:image/")
        .and_then(|s| s.split_once(";base64,"))
    else {
        return String::new();
    };
    let Ok(b) = STANDARD.decode(data) else {
        return String::new();
    };
    let valid = match kind {
        "png" => b.starts_with(b"\x89PNG\r\n\x1a\n"),
        "jpeg" => b.starts_with(b"\xff\xd8\xff"),
        "gif" => b.starts_with(b"GIF87a") || b.starts_with(b"GIF89a"),
        "webp" => b.starts_with(b"RIFF") && b.get(8..12) == Some(b"WEBP"),
        "bmp" => b.starts_with(b"BM"),
        _ => false,
    };
    if valid { s.into() } else { String::new() }
}
pub fn public_content(value: &mut Value) -> Result<(), ApiError> {
    match value {
        Value::Array(items) => {
            for v in items {
                public_content(v)?
            }
        }
        Value::Object(map) => {
            for v in map.values_mut() {
                public_content(v)?
            }
            if let Some(Value::String(cover)) = map.get("cover") {
                let result = if cover.starts_with("https://") || cover.starts_with("http://") {
                    let id = map.get("id");
                    if id.is_some_and(truthy) && map.contains_key("source") {
                        format!(
                            "/api/books/{}/cover?v={:x}",
                            quote(&py_string(id.unwrap()), false),
                            Sha256::digest(cover.as_bytes())
                        )
                    } else {
                        format!(
                            "/api/image?url={}&referer={}",
                            quote(cover, true),
                            quote(&map.get("path").map(py_string).unwrap_or_default(), true)
                        )
                    }
                } else {
                    raster(cover)
                };
                map.insert("cover".into(), result.into());
            }
            if let Some(Value::String(html)) = map.get("html") {
                let html = sanitize_html(html)?;
                map.insert("html".into(), html.into());
            }
        }
        _ => {}
    }
    Ok(())
}
fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
fn py_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        _ => v.to_string(),
    }
}
pub fn json_bytes(v: &Value) -> Vec<u8> {
    fn emit(v: &Value, out: &mut String) {
        match v {
            Value::Array(a) => {
                out.push('[');
                for (i, v) in a.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ")
                    }
                    emit(v, out)
                }
                out.push(']')
            }
            Value::Object(o) => {
                out.push('{');
                for (i, (k, v)) in o.iter().enumerate() {
                    if i > 0 {
                        out.push_str(", ")
                    }
                    out.push_str(&serde_json::to_string(k).expect("string serialization"));
                    out.push_str(": ");
                    emit(v, out)
                }
                out.push('}')
            }
            Value::Number(n) if n.is_f64() => {
                let value = n.as_f64().expect("float number");
                let text = if value != 0.0 && value.abs() < 0.0001 {
                    format!("{value:e}")
                } else {
                    format!("{value:?}")
                };
                if let Some((mantissa, exponent)) = text.split_once('e') {
                    let exponent: i32 = exponent.parse().expect("formatted exponent");
                    out.push_str(&format!(
                        "{mantissa}e{}{exponent:02}",
                        if exponent < 0 { "-" } else { "+" },
                        exponent = exponent.abs()
                    ));
                } else {
                    out.push_str(&text);
                }
            }
            _ => out.push_str(&v.to_string()),
        }
    }
    let mut out = String::new();
    emit(v, &mut out);
    out.into_bytes()
}

#[derive(Default)]
struct Node {
    tag: String,
    attrs: Vec<(String, String)>,
    children: Vec<Node>,
    text: Option<String>,
}
fn void(tag: &str) -> bool {
    [
        "area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param",
        "source", "track", "wbr",
    ]
    .contains(&tag)
}
fn attr<'a>(node: &'a Node, key: &str) -> &'a str {
    node.attrs
        .iter()
        .rev()
        .find(|(k, _)| k == key)
        .map_or("", |(_, v)| v)
}
fn parse_html(s: &str) -> Result<Node, ApiError> {
    let token = cached_regex!(
        r#"(?s)<!--.*?-->|<![^>]*>|<\?.*?>|</\s*([A-Za-z][A-Za-z0-9:-]*)[^>]*>|<([A-Za-z][A-Za-z0-9:-]*)(\s(?:[^<>"']|"[^"]*"|'[^']*')*)?\s*/?>"#
    );
    let attributes = cached_regex!(r#"([^\s=/'">]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]*)))?"#);
    let mut stack = vec![Node::default()];
    let mut pos = 0;
    let mut count = 0;
    for cap in token.captures_iter(s) {
        let raw_tag = &stack.last().expect("root").tag;
        let raw_text = ["script", "style"].contains(&raw_tag.as_str());
        if raw_text
            && !cap
                .get(1)
                .is_some_and(|m| m.as_str().eq_ignore_ascii_case(raw_tag))
        {
            continue;
        }
        let m = cap.get(0).expect("match");
        if m.start() > pos && !raw_text {
            stack.last_mut().expect("root").children.push(Node {
                text: Some(crate::entities::unescape(&s[pos..m.start()])?),
                ..Node::default()
            });
        }
        pos = m.end();
        if let Some(end) = cap.get(1) {
            let tag = end.as_str().to_ascii_lowercase();
            if let Some(index) = stack.iter().rposition(|n| n.tag == tag) {
                while stack.len() > index.max(1) {
                    let n = stack.pop().expect("node");
                    stack.last_mut().expect("root").children.push(n);
                }
            }
        } else if let Some(start) = cap.get(2) {
            count += 1;
            if stack.len() > 256 || count > 100000 {
                return Err(ApiError::new(400, "HTML exceeds parsing limits"));
            }
            let tag = start.as_str().to_ascii_lowercase();
            let attrs = attributes
                .captures_iter(cap.get(3).map_or("", |m| m.as_str()))
                .map(|a| {
                    Ok((
                        a[1].to_ascii_lowercase(),
                        crate::entities::unescape(
                            a.get(2)
                                .or(a.get(3))
                                .or(a.get(4))
                                .map_or("", |m| m.as_str()),
                        )?,
                    ))
                })
                .collect::<Result<Vec<_>, ApiError>>()?;
            let node = Node {
                tag: tag.clone(),
                attrs,
                ..Node::default()
            };
            if void(&tag) || m.as_str().ends_with("/>") {
                stack.last_mut().expect("root").children.push(node);
            } else {
                stack.push(node);
            }
        }
    }
    if pos < s.len() && !["script", "style"].contains(&stack.last().expect("root").tag.as_str()) {
        stack.last_mut().expect("root").children.push(Node {
            text: Some(crate::entities::unescape(&s[pos..])?),
            ..Node::default()
        });
    }
    while stack.len() > 1 {
        let n = stack.pop().expect("node");
        stack.last_mut().expect("root").children.push(n);
    }
    Ok(stack.pop().expect("root"))
}
fn remote(s: &str) -> String {
    if s.is_empty() || s.chars().any(|c| c <= ' ' || c == '\u{7f}' || c == '\\') {
        return String::new();
    }
    let Some((scheme, rest)) = s.split_once("://") else {
        return String::new();
    };
    let scheme = scheme.to_ascii_lowercase();
    if !["http", "https"].contains(&scheme.as_str()) {
        return String::new();
    }
    let authority_end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let authority = &rest[..authority_end];
    if authority.contains('@') {
        return String::new();
    }
    let (host, port) = if let Some(bracketed) = authority.strip_prefix('[') {
        let Some((host, tail)) = bracketed.split_once(']') else {
            return String::new();
        };
        if !tail.is_empty() && !tail.starts_with(':') {
            return String::new();
        };
        let address = if let Some((address, scope)) = host.split_once('%') {
            if scope.is_empty() || scope.contains('%') {
                return String::new();
            }
            address
        } else {
            host
        };
        if address.parse::<std::net::Ipv6Addr>().is_err()
            && !cached_regex!(r"^[vV][0-9a-fA-F]+\..+$").is_match(host)
        {
            return String::new();
        }
        (host, tail.strip_prefix(':'))
    } else {
        let mut parts = authority.split(':');
        let host = parts.next().unwrap_or("");
        let port = parts.next();
        if parts.next().is_some() {
            return String::new();
        };
        (host, port)
    };
    if let Some(port) = port.filter(|s| !s.is_empty()) {
        if port.len() > 4300 || !port.bytes().all(|c| c.is_ascii_digit()) {
            return String::new();
        }
        let Ok(port) = port.parse::<u16>() else {
            return String::new();
        };
        if port != if scheme == "http" { 80 } else { 443 } {
            return String::new();
        }
    }
    let host = host.to_ascii_lowercase().trim_end_matches('.').to_owned();
    if host.is_empty()
        || host == "localhost"
        || [
            ".localhost",
            ".local",
            ".internal",
            ".lan",
            ".home",
            ".test",
            ".invalid",
        ]
        .iter()
        .any(|s| host.ends_with(s))
    {
        return String::new();
    }
    let ip_text = host.split('%').next().unwrap_or("");
    if let Ok(ip) = ip_text.parse::<std::net::IpAddr>() {
        if !public_ip(ip) {
            return String::new();
        }
    } else if !cached_regex!(r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$").is_match(&host)
        || !host.contains('.')
        || cached_regex!(r"^(?:\d+|0x[0-9a-f]+)$").is_match(host.rsplit('.').next().unwrap_or(""))
    {
        return String::new();
    }
    let tail = &rest[authority_end..];
    let tail = tail.split('#').next().unwrap_or("");
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    let netloc = if host.contains(':') {
        format!("[{host}]")
    } else {
        host
    };
    format!(
        "{scheme}://{netloc}{}{}",
        if path.is_empty() { "/" } else { path },
        if query.is_empty() {
            String::new()
        } else {
            format!("?{query}")
        }
    )
}
fn public_ip(ip: std::net::IpAddr) -> bool {
    fn within(n: u128, networks: &[(u128, u8)], width: u8) -> bool {
        networks
            .iter()
            .any(|&(base, bits)| n >> (width - bits) == base >> (width - bits))
    }
    const V4_PRIVATE: &[(u128, u8)] = &[
        (0x0, 8),
        (0xa000000, 8),
        (0x7f000000, 8),
        (0xa9fe0000, 16),
        (0xac100000, 12),
        (0xc0000000, 24),
        (0xc00000aa, 31),
        (0xc0000200, 24),
        (0xc0a80000, 16),
        (0xc6120000, 15),
        (0xc6336400, 24),
        (0xcb007100, 24),
        (0xf0000000, 4),
        (0xffffffff, 32),
    ];
    const V4_EXCEPTIONS: &[(u128, u8)] = &[(0xc0000009, 32), (0xc000000a, 32)];
    const V4_RESERVED: &[(u128, u8)] = &[(0xf0000000, 4)];
    const V6_PRIVATE: &[(u128, u8)] = &[
        (0x1, 128),
        (0x0, 128),
        (0xffff00000000, 96),
        (0x64ff9b000100000000000000000000, 48),
        (0x1000000000000000000000000000000, 64),
        (0x20010000000000000000000000000000, 23),
        (0x20010db8000000000000000000000000, 32),
        (0x20020000000000000000000000000000, 16),
        (0x3fff0000000000000000000000000000, 20),
        (0xfc000000000000000000000000000000, 7),
        (0xfe800000000000000000000000000000, 10),
    ];
    const V6_EXCEPTIONS: &[(u128, u8)] = &[
        (0x20010001000000000000000000000001, 128),
        (0x20010001000000000000000000000002, 128),
        (0x20010003000000000000000000000000, 32),
        (0x20010004011200000000000000000000, 48),
        (0x20010020000000000000000000000000, 28),
        (0x20010030000000000000000000000000, 28),
    ];
    const V6_RESERVED: &[(u128, u8)] = &[
        (0x0, 8),
        (0x1000000000000000000000000000000, 8),
        (0x2000000000000000000000000000000, 7),
        (0x4000000000000000000000000000000, 6),
        (0x8000000000000000000000000000000, 5),
        (0x10000000000000000000000000000000, 4),
        (0x40000000000000000000000000000000, 3),
        (0x60000000000000000000000000000000, 3),
        (0x80000000000000000000000000000000, 3),
        (0xa0000000000000000000000000000000, 3),
        (0xc0000000000000000000000000000000, 3),
        (0xe0000000000000000000000000000000, 4),
        (0xf0000000000000000000000000000000, 5),
        (0xf8000000000000000000000000000000, 6),
        (0xfe000000000000000000000000000000, 9),
    ];
    match ip {
        std::net::IpAddr::V4(v) => {
            let n = u32::from(v) as u128;
            (!within(n, V4_PRIVATE, 32) || within(n, V4_EXCEPTIONS, 32))
                && !within(n, V4_RESERVED, 32)
                && !v.is_multicast()
                && !(v.octets()[0] == 100 && (64..128).contains(&v.octets()[1]))
        }
        std::net::IpAddr::V6(v) => {
            if let Some(mapped) = v.to_ipv4_mapped() {
                return public_ip(mapped.into());
            };
            let n = u128::from(v);
            (!within(n, V6_PRIVATE, 128) || within(n, V6_EXCEPTIONS, 128))
                && !within(n, V6_RESERVED, 128)
                && !v.is_multicast()
                && v.segments()[0] & 0xffc0 != 0xfec0
        }
    }
}
fn sanitize_html(s: &str) -> Result<String, ApiError> {
    fn clean(n: &Node) -> String {
        if let Some(text) = &n.text {
            return escape(text);
        };
        let drop = [
            "script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript",
            "form", "button", "input", "select", "textarea",
        ];
        let hidden = cached_regex!(
            r"(?i)(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|em|rem|pt)?(?:\s*[;!]|$))"
        );
        if drop.contains(&n.tag.as_str())
            || n.attrs.iter().any(|(k, _)| k == "hidden")
            || attr(n, "aria-hidden") == "true"
            || hidden.is_match(attr(n, "style"))
            || attr(n, "class")
                .split_whitespace()
                .any(|s| ["ads-responsive", "adsbygoogle"].contains(&s))
        {
            return String::new();
        }
        if n.tag == "img" {
            let raw = ["data-original", "data-src", "src"]
                .into_iter()
                .map(|k| attr(n, k))
                .find(|s| !s.is_empty())
                .unwrap_or("");
            let mut url = remote(raw);
            if !url.is_empty() {
                url = format!("/api/image?url={}", quote(&url, true));
            } else {
                url = raster(raw);
                if url.is_empty() {
                    url = svg_data(raw);
                }
            }
            return if url.is_empty() {
                String::new()
            } else {
                format!(
                    "<img src=\"{}\" alt=\"{}\" loading=\"lazy\">",
                    escape(&url),
                    escape(attr(n, "alt"))
                )
            };
        }
        let body = n.children.iter().map(clean).collect::<String>();
        let allowed = [
            "p",
            "div",
            "section",
            "article",
            "header",
            "footer",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
            "blockquote",
            "pre",
            "li",
            "ul",
            "ol",
            "tr",
            "br",
            "hr",
            "span",
            "em",
            "strong",
            "b",
            "i",
            "u",
            "s",
            "del",
            "sub",
            "sup",
            "ruby",
            "rt",
            "rp",
            "code",
            "table",
            "thead",
            "tbody",
            "td",
            "th",
        ];
        if allowed.contains(&n.tag.as_str()) {
            format!(
                "<{}>{}",
                n.tag,
                if void(&n.tag) {
                    String::new()
                } else {
                    format!("{body}</{}>", n.tag)
                }
            )
        } else {
            body
        }
    }
    Ok(clean(&parse_html(s)?))
}
fn svg_data(s: &str) -> String {
    use quick_xml::{NsReader, events::Event, name::ResolveResult};
    if s.len() > 3 * 1024 * 1024 {
        return String::new();
    };
    let Some(data) = s
        .strip_prefix("data:image/svg+xml;base64,")
        .and_then(|s| STANDARD.decode(s).ok())
    else {
        return String::new();
    };
    if data.len() > 2 * 1024 * 1024 {
        return String::new();
    };
    let Ok(text) = std::str::from_utf8(&data) else {
        return String::new();
    };
    if cached_regex!(r"(?i)<!\s*(?:DOCTYPE|ENTITY)\b").is_match(text) {
        return String::new();
    }
    let mut reader = NsReader::from_str(text.trim_start_matches('\u{feff}'));
    let mut stack = vec![Node::default()];
    let mut count = 0;
    loop {
        match reader.read_resolved_event() {
            Ok((namespace, event @ (Event::Start(_) | Event::Empty(_)))) => {
                let allowed_namespace = match namespace {
                    ResolveResult::Unbound => true,
                    ResolveResult::Bound(ns) => ns.as_ref() == b"http://www.w3.org/2000/svg",
                    ResolveResult::Unknown(_) => return String::new(),
                };
                let empty = matches!(event, Event::Empty(_));
                let e = match event {
                    Event::Start(e) | Event::Empty(e) => e,
                    _ => unreachable!(),
                };
                let name = if allowed_namespace {
                    String::from_utf8_lossy(e.local_name().as_ref()).into_owned()
                } else {
                    "foreign-namespace".into()
                };
                count += 1;
                if stack.len() > 101 || count > 10000 {
                    return String::new();
                };
                let mut attrs = Vec::new();
                for attr in e.attributes() {
                    let Ok(attr) = attr else { return String::new() };
                    let key = String::from_utf8_lossy(attr.key.as_ref()).into_owned();
                    let Ok(value) = attr.decode_and_unescape_value(reader.decoder()) else {
                        return String::new();
                    };
                    attrs.push((key, value.into_owned()));
                }
                let n = Node {
                    tag: name,
                    attrs,
                    ..Node::default()
                };
                if empty {
                    stack.last_mut().expect("root").children.push(n)
                } else {
                    stack.push(n)
                }
            }
            Ok((_, Event::End(_))) => {
                if stack.len() < 2 {
                    return String::new();
                };
                let n = stack.pop().expect("node");
                stack.last_mut().expect("root").children.push(n)
            }
            Ok((_, Event::Text(e))) => {
                let Ok(t) = e.decode() else {
                    return String::new();
                };
                let t = t.into_owned();
                stack.last_mut().expect("root").children.push(Node {
                    text: Some(t),
                    ..Node::default()
                })
            }
            Ok((_, Event::GeneralRef(e))) => {
                let Ok(name) = e.decode() else {
                    return String::new();
                };
                let decoded = match name.as_ref() {
                    "amp" => "&".into(),
                    "lt" => "<".into(),
                    "gt" => ">".into(),
                    "apos" => "'".into(),
                    "quot" => "\"".into(),
                    n if n.starts_with('#') => {
                        let (radix, digits) =
                            n.strip_prefix("#x").map_or((10, &n[1..]), |s| (16, s));
                        let Some(c) = u32::from_str_radix(digits, radix)
                            .ok()
                            .and_then(char::from_u32)
                        else {
                            return String::new();
                        };
                        c.to_string()
                    }
                    _ => return String::new(),
                };
                stack.last_mut().expect("root").children.push(Node {
                    text: Some(decoded),
                    ..Node::default()
                });
            }
            Ok((_, Event::CData(e))) => {
                let Ok(text) = e.decode() else {
                    return String::new();
                };
                stack.last_mut().expect("root").children.push(Node {
                    text: Some(text.into_owned()),
                    ..Node::default()
                });
            }
            Ok((_, Event::Eof)) => break,
            Err(_) => return String::new(),
            _ => {}
        }
    }
    if stack.len() != 1 {
        return String::new();
    };
    let root = &mut stack[0];
    root.children
        .retain(|n| n.text.as_ref().is_none_or(|s| !s.trim().is_empty()));
    if root.children.len() != 1 || root.children[0].tag != "svg" {
        return String::new();
    }
    fn safe(v: &str) -> bool {
        let v = cached_regex!(r"(?i)url\(\s*#[\w.-]+\s*\)").replace_all(v, "");
        !cached_regex!(r#"(?i)url\s*\(|expression\s*\(|[\\@<>"'{}]"#).is_match(&v)
    }
    const PAINT: &[&str] = &[
        "fill",
        "fill-rule",
        "fill-opacity",
        "stroke",
        "stroke-width",
        "stroke-opacity",
        "stroke-linecap",
        "stroke-linejoin",
        "stroke-miterlimit",
        "stroke-dasharray",
        "stroke-dashoffset",
        "opacity",
        "stop-color",
        "stop-opacity",
        "clip-path",
        "clip-rule",
        "font-family",
        "font-size",
        "font-weight",
        "font-style",
        "text-anchor",
    ];
    fn style(v: &str) -> String {
        v.split(';')
            .filter_map(|s| s.split_once(':'))
            .filter(|(k, v)| PAINT.contains(&k.trim().to_ascii_lowercase().as_str()) && safe(v))
            .map(|(k, v)| format!("{}:{}", k.trim().to_ascii_lowercase(), v.trim()))
            .collect::<Vec<_>>()
            .join(";")
    }
    fn xml_escape(v: &str, attrs: bool) -> String {
        let mut v = v
            .replace('&', "&amp;")
            .replace('<', "&lt;")
            .replace('>', "&gt;");
        if attrs {
            v = v
                .replace('"', "&quot;")
                .replace('\n', "&#10;")
                .replace('\r', "&#13;")
                .replace('\t', "&#09;");
        }
        v
    }
    fn clean(n: &Node, root: bool) -> String {
        let tags = [
            "svg",
            "g",
            "defs",
            "path",
            "rect",
            "circle",
            "ellipse",
            "line",
            "polyline",
            "polygon",
            "text",
            "tspan",
            "title",
            "desc",
            "linearGradient",
            "radialGradient",
            "stop",
            "clipPath",
        ];
        if n.tag == "style" {
            let text = n
                .children
                .iter()
                .filter_map(|n| n.text.as_deref())
                .collect::<String>();
            let rules = cached_regex!(r"([.#][\w-]+)\s*\{([^{}]*)\}")
                .captures_iter(&text)
                .filter_map(|c| {
                    let s = style(&c[2]);
                    if s.is_empty() {
                        None
                    } else {
                        Some(format!("{}{{{s}}}", &c[1]))
                    }
                })
                .collect::<String>();
            return if rules.is_empty() {
                String::new()
            } else {
                format!("<ns0:style>{}</ns0:style>", xml_escape(&rules, false))
            };
        }
        if !tags.contains(&n.tag.as_str()) {
            return String::new();
        };
        let mut out = format!("<ns0:{}", n.tag);
        if root {
            out.push_str(" xmlns:ns0=\"http://www.w3.org/2000/svg\"")
        };
        let attrs = [
            "id",
            "class",
            "viewBox",
            "preserveAspectRatio",
            "width",
            "height",
            "d",
            "points",
            "x",
            "y",
            "x1",
            "y1",
            "x2",
            "y2",
            "cx",
            "cy",
            "r",
            "rx",
            "ry",
            "dx",
            "dy",
            "transform",
            "offset",
            "gradientUnits",
            "gradientTransform",
            "spreadMethod",
        ];
        for (k, v) in &n.attrs {
            if (PAINT.contains(&k.as_str()) || attrs.contains(&k.as_str())) && safe(v) {
                out.push_str(&format!(" {k}=\"{}\"", xml_escape(v, true)))
            } else if k == "style" {
                out.push_str(&format!(" style=\"{}\"", xml_escape(&style(v), true)))
            }
        }
        let keep_text = ["text", "tspan", "title", "desc"].contains(&n.tag.as_str());
        let mut body = String::new();
        let mut element_seen = false;
        for child in &n.children {
            if let Some(t) = &child.text {
                if keep_text && (!element_seen || ["text", "tspan"].contains(&n.tag.as_str())) {
                    body.push_str(&xml_escape(t, false));
                }
            } else {
                element_seen = true;
                body.push_str(&clean(child, false));
            }
        }
        if body.is_empty() {
            out.push_str(" />")
        } else {
            out.push('>');
            out.push_str(&body);
            out.push_str(&format!("</ns0:{}>", n.tag));
        }
        out
    }
    let out = clean(&root.children[0], true);
    format!("data:image/svg+xml;base64,{}", STANDARD.encode(out))
}

const CSP: &str = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' https: http: data:; media-src 'self' blob: data:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'";
struct IdleIo {
    stream: tokio::net::TcpStream,
    read: Option<std::pin::Pin<Box<tokio::time::Sleep>>>,
    write: Option<std::pin::Pin<Box<tokio::time::Sleep>>>,
}
fn idle_poll<T>(
    timer: &mut Option<std::pin::Pin<Box<tokio::time::Sleep>>>,
    cx: &mut std::task::Context<'_>,
    result: std::task::Poll<std::io::Result<T>>,
) -> std::task::Poll<std::io::Result<T>> {
    use std::{future::Future, task::Poll};
    if result.is_ready() {
        *timer = None;
        return result;
    }
    let timer = timer
        .get_or_insert_with(|| Box::pin(tokio::time::sleep(std::time::Duration::from_secs(30))));
    if timer.as_mut().poll(cx).is_ready() {
        Poll::Ready(Err(std::io::Error::new(
            std::io::ErrorKind::TimedOut,
            "idle connection",
        )))
    } else {
        Poll::Pending
    }
}
impl tokio::io::AsyncRead for IdleIo {
    fn poll_read(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &mut tokio::io::ReadBuf<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let result = std::pin::Pin::new(&mut self.stream).poll_read(cx, buf);
        idle_poll(&mut self.read, cx, result)
    }
}
impl tokio::io::AsyncWrite for IdleIo {
    fn poll_write(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
        buf: &[u8],
    ) -> std::task::Poll<std::io::Result<usize>> {
        let result = std::pin::Pin::new(&mut self.stream).poll_write(cx, buf);
        idle_poll(&mut self.write, cx, result)
    }
    fn poll_flush(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        let result = std::pin::Pin::new(&mut self.stream).poll_flush(cx);
        idle_poll(&mut self.write, cx, result)
    }
    fn poll_shutdown(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<std::io::Result<()>> {
        std::pin::Pin::new(&mut self.stream).poll_shutdown(cx)
    }
}
pub fn serve(
    library: Library,
    web_root: &Path,
    port: u16,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let root = Arc::new(checked_path(web_root)?);
    let library = Arc::new(library);
    let mut entropy = [0u8; 32];
    getrandom::fill(&mut entropy).map_err(|e| std::io::Error::other(e.to_string()))?;
    let token = Arc::new(
        entropy
            .iter()
            .map(|b| format!("{b:02x}"))
            .collect::<String>(),
    );
    let runtime = tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .max_blocking_threads(64)
        .enable_all()
        .build()?;
    runtime.block_on(async move {
        let listener = tokio::net::TcpListener::bind(("127.0.0.1", port)).await?;
        let port = listener.local_addr()?.port();
        println!("{}", json!({"port":port}));
        std::io::stdout().flush()?;
        let permits = Arc::new(tokio::sync::Semaphore::new(64));
        let work_permits = Arc::new(tokio::sync::Semaphore::new(64));
        loop {
            let permit = permits.clone().acquire_owned().await?;
            let (stream, _) = listener.accept().await?;
            let library = library.clone();
            let root = root.clone();
            let token = token.clone();
            let work_permits = work_permits.clone();
            tokio::spawn(async move {
                let _permit = permit;
                let service = hyper::service::service_fn(
                    move |request: hyper::Request<hyper::body::Incoming>| {
                        let library = library.clone();
                        let root = root.clone();
                        let token = token.clone();
                        let work_permits = work_permits.clone();
                        let (parts, _body) = request.into_parts();
                        async move {
                            let work_permit = work_permits
                                .acquire_owned()
                                .await
                                .expect("server-owned admission semaphore");
                            let result = tokio::task::spawn_blocking(move || {
                                // Keep admission until synchronous work ends, even if the client disconnects.
                                let _work_permit = work_permit;
                                respond(&library, &root, &token, port, parts)
                            })
                            .await;
                            Ok::<_, std::convert::Infallible>(result.unwrap_or_else(|_| {
                                Response::builder()
                                    .status(500)
                                    .header("Connection", "close")
                                    .body(Full::new(Bytes::new()))
                                    .expect("fixed response")
                            }))
                        }
                    },
                );
                let connection = hyper::server::conn::http1::Builder::new()
                    .max_buf_size(65536)
                    .serve_connection(
                        hyper_util::rt::TokioIo::new(IdleIo {
                            stream,
                            read: None,
                            write: None,
                        }),
                        service,
                    );
                let _ = connection.await;
            });
        }
        #[allow(unreachable_code)]
        Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
    })
}
fn respond(
    library: &Library,
    root: &Path,
    token: &str,
    port: u16,
    parts: hyper::http::request::Parts,
) -> Response<Full<Bytes>> {
    let method = parts.method.as_str().to_owned();
    let uri = parts.uri.to_string();
    let header = |name: &str| parts.headers.get(name).and_then(|v| v.to_str().ok());
    let mut headers = Vec::<(String, String)>::new();
    let mut kind = "application/json; charset=utf-8".to_owned();
    let supported = ["GET", "HEAD", "POST", "DELETE", "OPTIONS"].contains(&method.as_str());
    let result: Result<(u16, Vec<u8>), ApiError> = (|| {
        if !supported {
            let body = format!(
                "<!DOCTYPE HTML>\n<html lang=\"en\">\n    <head>\n        <meta charset=\"utf-8\">\n        <style type=\"text/css\">\n            :root {{\n                color-scheme: light dark;\n            }}\n        </style>\n        <title>Error response</title>\n    </head>\n    <body>\n        <h1>Error response</h1>\n        <p>Error code: 501</p>\n        <p>Message: Unsupported method ('{}').</p>\n        <p>Error code explanation: 501 - Server does not support this operation.</p>\n    </body>\n</html>\n",
                escape(&method)
            );
            kind = "text/html;charset=utf-8".into();
            return Ok((501, body.into_bytes()));
        }
        if method == "OPTIONS" {
            return Err(ApiError::new(
                403,
                "Không cho phép truy cập API từ website khác.",
            ));
        }
        let allowed = [format!("127.0.0.1:{port}"), format!("localhost:{port}")];
        if !header("Host").is_some_and(|h| allowed.iter().any(|a| h == a)) {
            return Err(ApiError::new(403, "Địa chỉ máy chủ không hợp lệ."));
        }
        if header("Origin")
            .is_some_and(|o| !o.is_empty() && !allowed.iter().any(|a| o == format!("http://{a}")))
        {
            return Err(ApiError::new(
                403,
                "Ứng dụng chỉ nhận yêu cầu từ cửa sổ cục bộ của nó.",
            ));
        }
        if !["GET", "HEAD"].contains(&method.as_str())
            && header("Sec-Fetch-Site") == Some("cross-site")
        {
            return Err(ApiError::new(403, "Từ chối yêu cầu ngoài ứng dụng."));
        }
        let path = decode(uri.as_str().split(['?', '#']).next().unwrap_or(""), false);
        if path == "/api/books" && ["GET", "HEAD"].contains(&method.as_str()) {
            let page = library.list_books(&ShelfQuery::from_uri(uri.as_str())?)?;
            let mut value =
                serde_json::to_value(page).map_err(|e| ApiError::new(500, e.to_string()))?;
            public_content(&mut value)?;
            return Ok((200, json_bytes(&value)));
        }
        if path.starts_with("/api/") {
            return Err(ApiError::new(404, "Không tìm thấy API."));
        }
        if !["GET", "HEAD"].contains(&method.as_str()) {
            return Err(ApiError::new(404, "Không có đường dẫn này."));
        }
        let relative = path.trim_start_matches('/');
        let candidate = root.join(if relative.is_empty() {
            "index.html"
        } else {
            relative
        });
        let mut lexical = std::path::PathBuf::new();
        for component in candidate.components() {
            match component {
                std::path::Component::CurDir => {}
                std::path::Component::ParentDir => {
                    lexical.pop();
                }
                _ => lexical.push(component),
            }
        }
        if !lexical.starts_with(root) {
            return Err(ApiError::new(
                403,
                "Không truy cập tệp ngoài giao diện ứng dụng.",
            ));
        }
        let resolved = candidate
            .canonicalize()
            .map_err(|_| ApiError::new(404, "Không tìm thấy tệp giao diện."))?;
        if !resolved.starts_with(root) {
            return Err(ApiError::new(
                403,
                "Không truy cập tệp ngoài giao diện ứng dụng.",
            ));
        }
        let file = checked_path(&candidate)
            .map_err(|_| ApiError::new(403, "Không truy cập tệp ngoài giao diện ứng dụng."))?;
        if !file.is_file() {
            return Err(ApiError::new(404, "Không tìm thấy tệp giao diện."));
        }
        kind = match file.extension().and_then(|s| s.to_str()).unwrap_or("") {
            "html" => "text/html; charset=utf-8",
            "js" => "text/javascript; charset=utf-8",
            "css" => "text/css; charset=utf-8",
            "json" => "application/json; charset=utf-8",
            "svg" => "image/svg+xml",
            "png" => "image/png",
            "ico" => "image/vnd.microsoft.icon",
            _ => "application/octet-stream",
        }
        .into();
        let body = std::fs::read(file)?;
        if path == "/" || path == "/index.html" {
            headers.push((
                "Set-Cookie".into(),
                format!("ke_truyen_session={token}; HttpOnly; SameSite=Strict; Path=/api/"),
            ));
        }
        Ok((200, body))
    })();
    let (status, body) =
        result.unwrap_or_else(|e| (e.status, json_bytes(&json!({"error":e.message}))));
    if supported {
        headers.extend([
            ("X-Content-Type-Options".into(), "nosniff".into()),
            ("Referrer-Policy".into(), "no-referrer".into()),
            ("Cross-Origin-Resource-Policy".into(), "same-origin".into()),
            ("Content-Security-Policy".into(), CSP.into()),
            (
                "Cache-Control".into(),
                if kind.starts_with("application/json") {
                    "no-store"
                } else {
                    "private, max-age=60"
                }
                .into(),
            ),
        ]);
    }
    headers.push(("Content-Type".into(), kind));
    if status >= 400 {
        headers.push(("Connection".into(), "close".into()));
    }
    headers.push(("Content-Length".into(), body.len().to_string()));
    let mut response = Response::builder().status(status);
    for (k, v) in headers {
        response = response.header(k, v);
    }
    response
        .body(Full::new(if method == "HEAD" {
            Bytes::new()
        } else {
            Bytes::from(body)
        }))
        .expect("validated response headers")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn nested_output() {
        let mut v = json!({"warnings":[{"id":"a/b","source":"local","cover":"https://example.org/x","html":"<p>Hello<script>x</script><b>bold</b></p>"}]});
        public_content(&mut v).unwrap();
        assert!(
            v["warnings"][0]["cover"]
                .as_str()
                .unwrap()
                .starts_with("/api/books/a%2Fb/cover?v=")
        );
        assert_eq!(v["warnings"][0]["html"], "<p>Hello<b>bold</b></p>");
    }
}
