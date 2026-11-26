use crate::{
    ApiError, Library, ShelfQuery,
    library::{checked_path, decode},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use http_body_util::BodyExt;
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
pub(crate) fn escape(s: &str) -> String {
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
        Value::Number(_) => {
            validate_finite_json(value)?;
        }
        _ => {}
    }
    Ok(())
}
pub(crate) fn truthy(v: &Value) -> bool {
    match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
        Value::Number(n) => n.as_f64() != Some(0.0),
    }
}
pub(crate) fn validate_finite_json(value: &Value) -> Result<(), ApiError> {
    match value {
        Value::Number(n) => {
            let raw = n.to_string();
            if raw.contains(['.', 'e', 'E']) && n.as_f64().is_none() {
                return Err(ApiError::new(
                    400,
                    format!(
                        "Out of range float values are not JSON compliant: {}inf",
                        if raw.starts_with('-') { "-" } else { "" }
                    ),
                ));
            }
        }
        Value::Array(items) => {
            for item in items {
                validate_finite_json(item)?;
            }
        }
        Value::Object(items) => {
            for item in items.values() {
                validate_finite_json(item)?;
            }
        }
        _ => {}
    }
    Ok(())
}
pub(crate) fn py_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => "None".into(),
        Value::Bool(b) => if *b { "True" } else { "False" }.into(),
        Value::Array(items) => format!(
            "[{}]",
            items
                .iter()
                .map(py_value_repr)
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Object(items) => format!(
            "{{{}}}",
            items
                .iter()
                .map(|(k, v)| format!("{}: {}", crate::library::py_repr_full(k), py_value_repr(v)))
                .collect::<Vec<_>>()
                .join(", ")
        ),
        Value::Number(_) => String::from_utf8(json_bytes(v)).expect("JSON UTF8"),
    }
}
pub(crate) fn py_value_repr(v: &Value) -> String {
    if let Value::String(s) = v {
        crate::library::py_repr_full(s)
    } else {
        py_string(v)
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
            Value::Number(n) if n.as_i64().is_some() => {
                out.push_str(&n.as_i64().expect("checked integer").to_string())
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
fn html_tokens() -> &'static Regex {
    cached_regex!(
        r#"(?s)<!--.*?-->|<![^>]*>|<\?.*?>|</\s*([A-Za-z][A-Za-z0-9:_.-]*)[^>]*>|<([A-Za-z][A-Za-z0-9:_.-]*)([\s/](?:[^<>"']|"[^"]*"|'[^']*')*)?\s*/?>"#
    )
}

fn html_limit(stack_len: usize, count: usize) -> Result<(), ApiError> {
    if stack_len > 256 || count > 100_000 {
        Err(ApiError::new(400, "HTML exceeds parsing limits"))
    } else {
        Ok(())
    }
}

pub(crate) fn validate_html_limits(markup: &str) -> Result<(), ApiError> {
    let mut tags: Vec<&str> = Vec::new();
    let mut count = 0;
    for tag in html_tokens().captures_iter(markup) {
        let end = tag.get(1).map(|name| name.as_str());
        if tags.last().is_some_and(|last| {
            last.eq_ignore_ascii_case("script") || last.eq_ignore_ascii_case("style")
        }) && end.is_none_or(|end| !end.eq_ignore_ascii_case(tags.last().unwrap()))
        {
            continue;
        }
        if let Some(end) = end {
            if let Some(index) = tags
                .iter()
                .rposition(|start| start.eq_ignore_ascii_case(end))
            {
                tags.truncate(index);
            }
        } else if let Some(start) = tag.get(2).map(|name| name.as_str()) {
            count += 1;
            html_limit(tags.len() + 1, count)?;
            if !tag[0].ends_with("/>") && !void(&start.to_ascii_lowercase()) {
                tags.push(start);
            }
        }
    }
    Ok(())
}

fn parse_html(s: &str) -> Result<Node, ApiError> {
    let attributes = cached_regex!(r#"([^\s=/'">]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]*)))?"#);
    let mut stack = vec![Node::default()];
    let mut pos = 0;
    let mut count = 0;
    for cap in html_tokens().captures_iter(s) {
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
            html_limit(stack.len(), count)?;
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
    remote_url(s)
}

fn remote_url(s: &str) -> String {
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
pub(crate) fn asset_image_urls(value: &str, base: &str) -> Result<Vec<String>, ApiError> {
    let parsed = asset_url_text(value);
    let (_, authority, path, query) = split_asset_url(&parsed);
    let authority = authority
        .filter(|s| !s.is_empty())
        .and_then(|s| s.rsplit('@').next());
    if let Some(authority) = authority {
        let host = authority.rsplit('@').next().unwrap_or("");
        if host.contains(['[', ']']) {
            let valid = host
                .strip_prefix('[')
                .and_then(|s| s.split_once(']'))
                .is_some_and(|(host, port)| {
                    (port.is_empty() || port.starts_with(':'))
                        && (host
                            .split('%')
                            .next()
                            .unwrap_or("")
                            .parse::<std::net::Ipv6Addr>()
                            .is_ok()
                            || cached_regex!(r"^[vV][0-9a-fA-F]+\..+$").is_match(host))
                });
            if !valid {
                return Err(ApiError::new(400, "Invalid IPv6 URL"));
            }
        }
    }
    let local = authority.is_none_or(|a| {
        let host = if let Some(bracketed) = a.strip_prefix('[') {
            bracketed.split(']').next().unwrap_or("")
        } else {
            a.rsplit('@')
                .next()
                .unwrap_or("")
                .split(':')
                .next()
                .unwrap_or("")
        };
        ["localhost", "127.0.0.1", "::1"].contains(&host.to_ascii_lowercase().as_str())
    });
    let candidates = if path == "/api/image" && local {
        query
            .unwrap_or("")
            .split('&')
            .filter_map(|pair| {
                let (key, value) = pair.split_once('=').unwrap_or((pair, ""));
                (decode(key, true) == "url" && !value.is_empty()).then(|| decode(value, true))
            })
            .collect::<Vec<_>>()
    } else {
        vec![value.to_owned()]
    };
    let mut urls = std::collections::BTreeSet::new();
    for candidate in candidates {
        let url = relative_remote(&candidate, base);
        if !url.is_empty() {
            urls.insert(url);
        }
    }
    Ok(urls.into_iter().collect())
}

pub(crate) fn asset_markup_urls(markup: &str, base: &str) -> Result<Vec<String>, ApiError> {
    fn collect(node: &Node, base: &str, urls: &mut Vec<String>) -> Result<(), ApiError> {
        if node.tag == "img"
            && let Some(value) = ["data-original", "data-src", "src"]
                .into_iter()
                .map(|attribute| attr(node, attribute))
                .find(|value| !value.is_empty())
        {
            urls.extend(asset_image_urls(value, base)?);
        }
        for child in &node.children {
            collect(child, base, urls)?;
        }
        Ok(())
    }
    let mut urls = Vec::new();
    collect(&parse_html(markup)?, base, &mut urls)?;
    Ok(urls)
}
pub(crate) fn relative_remote(value: &str, base: &str) -> String {
    if value.is_empty()
        || value
            .chars()
            .any(|c| c <= ' ' || c == '\u{7f}' || c == '\\')
    {
        return String::new();
    }
    let base = asset_url_text(base);
    let (base_scheme, base_authority, base_path, base_query) = split_asset_url(&base);
    let (value_scheme, authority, path, query) = split_asset_url(value);
    let Some(scheme) = value_scheme.or(base_scheme) else {
        return String::new();
    };
    if !base_scheme.is_some_and(|base_scheme| scheme.eq_ignore_ascii_case(base_scheme)) {
        return remote(value);
    }
    if let Some(authority) = authority.filter(|s| !s.is_empty()) {
        return normalized_asset_url(scheme, authority, path, query);
    }
    let Some(authority) = base_authority.filter(|s| !s.is_empty()) else {
        return String::new();
    };
    if path.is_empty() {
        return normalized_asset_url(scheme, authority, base_path, query.or(base_query));
    }
    let mut parts = Vec::new();
    if !path.starts_with('/') {
        parts.extend(base_path.split('/'));
        if parts.last().is_some_and(|s| !s.is_empty()) {
            parts.pop();
        }
    }
    parts.extend(path.split('/'));
    let mut segments = Vec::new();
    for (i, part) in parts.iter().enumerate() {
        match *part {
            "." => {}
            ".." => {
                segments.pop();
            }
            "" if !path.starts_with('/') && i > 0 && i + 1 < parts.len() => {}
            _ => segments.push(*part),
        }
    }
    if parts.last().is_some_and(|part| [".", ".."].contains(part)) {
        segments.push("");
    }
    normalized_asset_url(scheme, authority, &segments.join("/"), query)
}

fn normalized_asset_url(scheme: &str, authority: &str, path: &str, query: Option<&str>) -> String {
    let slash = if path.starts_with('/') { "" } else { "/" };
    let query = query.map_or_else(String::new, |q| format!("?{q}"));
    remote_url(&format!("{scheme}://{authority}{slash}{path}{query}"))
}

fn asset_url_text(value: &str) -> std::borrow::Cow<'_, str> {
    let text = value.trim_start_matches(|c| c <= ' ');
    if text.contains(['\t', '\n', '\r']) {
        text.replace(['\t', '\n', '\r'], "").into()
    } else {
        text.into()
    }
}

fn split_asset_url(value: &str) -> (Option<&str>, Option<&str>, &str, Option<&str>) {
    let scheme = value.split_once(':').filter(|(prefix, _)| {
        prefix
            .bytes()
            .next()
            .is_some_and(|b| b.is_ascii_alphabetic())
            && prefix
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"+-.".contains(&b))
    });
    let (scheme, rest) = scheme.map_or((None, value), |(prefix, rest)| (Some(prefix), rest));
    let rest = rest.split_once('#').map_or(rest, |(left, _)| left);
    let (path, query) = rest
        .split_once('?')
        .map_or((rest, None), |(path, query)| (path, Some(query)));
    let (authority, path) = path.strip_prefix("//").map_or((None, path), |rest| {
        let end = rest.find('/').unwrap_or(rest.len());
        (Some(&rest[..end]), &rest[end..])
    });
    (scheme, authority, path, query)
}
pub(crate) fn public_ip(ip: std::net::IpAddr) -> bool {
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
fn hidden_node(node: &Node) -> bool {
    let drop = [
        "script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript",
        "form", "button", "input", "select", "textarea",
    ];
    let hidden_style = cached_regex!(
        r"(?i)(?:display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*0(?:px|em|rem|pt)?(?:\s*[;!]|$))"
    );
    drop.contains(&node.tag.as_str())
        || node.attrs.iter().any(|(k, _)| k == "hidden")
        || attr(node, "aria-hidden") == "true"
        || hidden_style.is_match(attr(node, "style"))
        || attr(node, "class")
            .split(crate::casefold::python_whitespace)
            .any(|s| ["ads-responsive", "adsbygoogle"].contains(&s))
}

pub(crate) fn html_to_text(markup: &str) -> Result<String, ApiError> {
    fn append(node: &Node, out: &mut String) {
        let block = [
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
        ]
        .contains(&node.tag.as_str());
        if let Some(text) = &node.text {
            out.push_str(text);
        } else if !hidden_node(node) {
            if block || ["br", "hr"].contains(&node.tag.as_str()) {
                out.push('\n');
            }
            for child in &node.children {
                append(child, out);
            }
            if block {
                out.push('\n');
            }
        }
    }
    let mut value = String::new();
    append(&parse_html(markup)?, &mut value);
    Ok(value
        .split([
            '\n', '\r', '\u{b}', '\u{c}', '\u{1c}', '\u{1d}', '\u{1e}', '\u{85}', '\u{2028}',
            '\u{2029}',
        ])
        .map(|line| {
            line.split(crate::casefold::python_whitespace)
                .filter(|s| !s.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n"))
}

fn sanitize_html(s: &str) -> Result<String, ApiError> {
    clean_html(s, None, true)
}

pub(crate) fn clean_html(
    s: &str,
    source_base: Option<&str>,
    allow_images: bool,
) -> Result<String, ApiError> {
    fn clean(n: &Node, source_base: Option<&str>, allow_images: bool) -> String {
        if let Some(text) = &n.text {
            return escape(text);
        };
        if hidden_node(n) {
            return String::new();
        }
        if n.tag == "img" {
            if !allow_images {
                return String::new();
            }
            let raw = ["data-original", "data-src", "src"]
                .into_iter()
                .map(|k| attr(n, k))
                .find(|s| !s.is_empty())
                .unwrap_or("");
            let url = match source_base {
                Some(base) => relative_remote(raw, base),
                None => {
                    let url = remote(raw);
                    if !url.is_empty() {
                        format!("/api/image?url={}", quote(&url, true))
                    } else {
                        let url = raster(raw);
                        if url.is_empty() { svg_data(raw) } else { url }
                    }
                }
            };
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
        let body = n
            .children
            .iter()
            .map(|child| clean(child, source_base, allow_images))
            .collect::<String>();
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
    Ok(clean(&parse_html(s)?, source_base, allow_images))
}
pub(crate) fn svg_data(s: &str) -> String {
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
type ReadTask = tokio::task::JoinHandle<std::io::Result<Vec<u8>>>;
struct ServiceBody {
    bytes: Option<Bytes>,
    file: Option<Arc<std::sync::Mutex<std::fs::File>>>,
    pending: Option<ReadTask>,
    remaining: u64,
    library: Option<Arc<Library>>,
    permit: Option<Arc<tokio::sync::OwnedSemaphorePermit>>,
    asset: Option<Arc<crate::lifecycle::Lease<std::path::PathBuf>>>,
}
impl ServiceBody {
    fn bytes(bytes: Vec<u8>) -> Self {
        let remaining = bytes.len() as u64;
        Self {
            bytes: Some(bytes.into()),
            file: None,
            pending: None,
            remaining,
            library: None,
            permit: None,
            asset: None,
        }
    }
    fn file(
        file: std::fs::File,
        len: u64,
        asset: crate::lifecycle::Lease<std::path::PathBuf>,
    ) -> Self {
        Self {
            bytes: None,
            file: Some(Arc::new(std::sync::Mutex::new(file))),
            pending: None,
            remaining: len,
            library: None,
            permit: None,
            asset: Some(Arc::new(asset)),
        }
    }
}
impl hyper::body::Body for ServiceBody {
    type Data = Bytes;
    type Error = std::io::Error;
    fn poll_frame(
        mut self: std::pin::Pin<&mut Self>,
        cx: &mut std::task::Context<'_>,
    ) -> std::task::Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>> {
        use std::{future::Future, io::Read, task::Poll};
        if let Some(bytes) = self.bytes.take() {
            self.remaining = 0;
            return if bytes.is_empty() {
                Poll::Ready(None)
            } else {
                Poll::Ready(Some(Ok(hyper::body::Frame::data(bytes))))
            };
        }
        if self.remaining == 0 {
            return Poll::Ready(None);
        }
        if self.pending.is_none() {
            let file = self.file.as_ref().expect("stream file").clone();
            let library = self.library.clone();
            let permit = self.permit.clone();
            let asset = self.asset.clone();
            let len = self.remaining.min(65536) as usize;
            self.pending = Some(tokio::task::spawn_blocking(move || {
                let _library = library;
                let _permit = permit;
                let _asset = asset;
                let mut buf = vec![0; len];
                let n = file
                    .lock()
                    .map_err(|_| std::io::Error::other("file lock poisoned"))?
                    .read(&mut buf)?;
                if n == 0 {
                    return Err(std::io::Error::new(
                        std::io::ErrorKind::UnexpectedEof,
                        "stream shortened",
                    ));
                }
                buf.truncate(n);
                Ok(buf)
            }));
        }
        match std::pin::Pin::new(self.pending.as_mut().unwrap()).poll(cx) {
            Poll::Pending => Poll::Pending,
            Poll::Ready(result) => {
                self.pending = None;
                match result {
                    Ok(Ok(bytes)) => {
                        self.remaining -= bytes.len() as u64;
                        Poll::Ready(Some(Ok(hyper::body::Frame::data(bytes.into()))))
                    }
                    Ok(Err(e)) => {
                        self.remaining = 0;
                        Poll::Ready(Some(Err(e)))
                    }
                    Err(e) => {
                        self.remaining = 0;
                        Poll::Ready(Some(Err(std::io::Error::other(e))))
                    }
                }
            }
        }
    }
    fn is_end_stream(&self) -> bool {
        self.remaining == 0
    }
    fn size_hint(&self) -> hyper::body::SizeHint {
        hyper::body::SizeHint::with_exact(self.remaining)
    }
}
fn guard(parts: &hyper::http::request::Parts, port: u16) -> Result<(), ApiError> {
    let header = |name: &str| parts.headers.get(name).and_then(|v| v.to_str().ok());
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
    if !["GET", "HEAD"].contains(&parts.method.as_str())
        && header("Sec-Fetch-Site") == Some("cross-site")
    {
        return Err(ApiError::new(403, "Từ chối yêu cầu ngoài ứng dụng."));
    }
    Ok(())
}
fn session(parts: &hyper::http::request::Parts, token: &str) -> bool {
    parts
        .headers
        .get("Cookie")
        .and_then(|v| v.to_str().ok())
        .is_some_and(|s| {
            s.split(';')
                .filter_map(|p| p.trim().split_once('='))
                .any(|(k, v)| k == "ke_truyen_session" && v.trim_matches('"') == token)
        })
}
fn json_route(parts: &hyper::http::request::Parts) -> bool {
    if parts.method != "POST" {
        return false;
    }
    let path = decode(parts.uri.path(), false);
    let pieces = path
        .split('/')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>();
    [
        "/api/settings",
        "/api/readers",
        "/api/notes",
        "/api/groups",
        "/api/shutdown",
        "/api/cache/chapters",
        "/api/cache/chapters/clear",
        "/api/cache/chapters/legacy",
        "/api/cache/chapters/compact",
        "/api/cache/images",
        "/api/cache/images/clear",
        "/api/dictionaries",
        "/api/dictionaries/import",
        "/api/dictionaries/rules/preview",
        "/api/qt",
        "/api/import/backup",
    ]
    .contains(&path.as_str())
        || matches!(
            pieces.as_slice(),
            ["api", "sources", _, "add"]
                | [
                    "api",
                    "sources",
                    "stv",
                    "captcha",
                    "refresh" | "verify" | "cancel"
                ]
                | ["api", "jobs", _, _]
                | ["api", "groups", _, "rename"]
        )
        || (pieces.len() == 4
            && pieces[..2] == ["api", "books"]
            && [
                "progress", "update", "trash", "restore", "download", "shortcut",
            ]
            .contains(&pieces[3]))
        || (pieces.len() == 6
            && pieces[..2] == ["api", "books"]
            && pieces[3] == "chapters"
            && ["edit", "restore", "original", "prefetch"].contains(&pieces[5]))
}

fn critical_operation(parts: &hyper::http::request::Parts) -> Option<&'static str> {
    if !["POST", "DELETE"].contains(&parts.method.as_str()) {
        return None;
    }
    let path = decode(parts.uri.path(), false);
    let pieces = path
        .split('/')
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>();
    match (parts.method.as_str(), pieces.as_slice()) {
        ("POST", ["api", "sources", _, "add"]) => Some("thêm truyện từ nguồn"),
        ("POST", ["api", "import", _, ..]) => Some("nhập thư viện/tệp/OCR"),
        ("POST", ["api", "dictionaries"]) | ("POST", ["api", "dictionaries", "import"]) => {
            Some("ghi/nhập từ điển QT")
        }
        ("DELETE", ["api", "dictionaries"]) => Some("xóa mục từ QT"),
        ("POST", ["api", "cache", "chapters", "compact"]) => Some("thu gọn SQLite"),
        ("POST", ["api", "books", _, "chapters", _, "edit" | "restore"]) => {
            Some("sửa/khôi phục chương")
        }
        ("DELETE", ["api", "books", _]) => Some("xóa vĩnh viễn truyện"),
        _ => None,
    }
}
async fn request_json(
    parts: &hyper::http::request::Parts,
    body: &mut hyper::body::Incoming,
    token: &str,
    port: u16,
) -> Result<Option<Value>, ApiError> {
    if !json_route(parts) {
        return Ok(None);
    }
    guard(parts, port)?;
    if critical_operation(parts) == Some("sửa/khôi phục chương") {
        let path = decode(parts.uri.path(), false);
        if let Some(index) = path.split('/').filter(|s| !s.is_empty()).nth(4) {
            crate::library::integer(index)?;
        }
    }
    let content_type = parts
        .headers
        .get("Content-Type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .split(';')
        .next()
        .unwrap_or("")
        .trim()
        .to_ascii_lowercase();
    if decode(parts.uri.path(), false) == "/api/shutdown" {
        if content_type != "application/json" {
            return Err(ApiError::new(
                403,
                "Tắt máy chủ cần Content-Type: application/json.",
            ));
        }
        if !session(parts, token)
            || parts
                .headers
                .get("X-Shutdown-Token")
                .and_then(|v| v.to_str().ok())
                != Some(token)
        {
            return Err(ApiError::new(
                403,
                "Cần mở giao diện của phiên máy chủ này để tắt ứng dụng.",
            ));
        }
    }
    if content_type != "application/json" {
        return Err(ApiError::new(400, "Yêu cầu phải dùng application/json."));
    }
    if parts.headers.contains_key("Transfer-Encoding") {
        return Err(ApiError::new(400, "Không hỗ trợ kiểu truyền dữ liệu này."));
    }
    let length = crate::library::integer(
        parts
            .headers
            .get("Content-Length")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("0"),
    )?;
    if !(0..=128 * 1024 * 1024).contains(&length) {
        return Err(ApiError::new(400, "Tệp vượt giới hạn tải lên 128 MiB."));
    }
    if length > 2 * 1024 * 1024 {
        return Err(ApiError::new(400, "Dữ liệu JSON quá lớn."));
    }
    let mut raw = Vec::new();
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| ApiError::new(400, "Dữ liệu tải lên chưa đầy đủ."))?;
        if let Ok(data) = frame.into_data() {
            if raw.len() + data.len() > 2 * 1024 * 1024 {
                return Err(ApiError::new(400, "Dữ liệu JSON quá lớn."));
            }
            raw.extend_from_slice(&data);
        }
    }
    if raw.len() != length as usize {
        return Err(ApiError::new(400, "Dữ liệu tải lên chưa đầy đủ."));
    }
    Ok(Some(parse_request_bytes(&raw)?))
}
/// Raw `/api/import/file` body (reference `Handler._body`), bounded by the 128 MiB upload limit.
async fn request_upload(
    parts: &hyper::http::request::Parts,
    body: &mut hyper::body::Incoming,
    port: u16,
) -> Result<Option<Vec<u8>>, ApiError> {
    if parts.method != "POST" || decode(parts.uri.path(), false) != "/api/import/file" {
        return Ok(None);
    }
    guard(parts, port)?;
    if parts.headers.contains_key("Transfer-Encoding") {
        return Err(ApiError::new(400, "Không hỗ trợ kiểu truyền dữ liệu này."));
    }
    let length = crate::library::integer(
        parts
            .headers
            .get("Content-Length")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("0"),
    )?;
    if !(0..=128 * 1024 * 1024).contains(&length) {
        return Err(ApiError::new(400, "Tệp vượt giới hạn tải lên 128 MiB."));
    }
    // shortcut: buffers one upload in memory like the reference; stream to a managed temp file if concurrent large imports matter.
    let mut raw = Vec::with_capacity((length as usize).min(1024 * 1024));
    while let Some(frame) = body.frame().await {
        let frame = frame.map_err(|_| ApiError::new(400, "Dữ liệu tải lên chưa đầy đủ."))?;
        if let Ok(data) = frame.into_data() {
            if raw.len() + data.len() > length as usize {
                return Err(ApiError::new(400, "Dữ liệu tải lên chưa đầy đủ."));
            }
            raw.extend_from_slice(&data);
        }
    }
    if raw.len() != length as usize {
        return Err(ApiError::new(400, "Dữ liệu tải lên chưa đầy đủ."));
    }
    Ok(Some(raw))
}
fn permission_drain_length(parts: &hyper::http::request::Parts) -> Option<usize> {
    if parts.method == "OPTIONS" || parts.headers.contains_key("Transfer-Encoding") {
        return None;
    }
    let length = crate::library::integer(
        parts
            .headers
            .get("Content-Length")
            .and_then(|v| v.to_str().ok())
            .unwrap_or("0"),
    )
    .ok()?;
    (1..=65536).contains(&length).then_some(length as usize)
}
async fn drain_permission_body<B: hyper::body::Body<Data = Bytes> + Unpin>(
    body: &mut B,
    length: Option<usize>,
) {
    let Some(mut remaining) = length else { return };
    // Match Python's small permission-error drain so Windows can deliver the 403 before closing.
    let _ = tokio::time::timeout(std::time::Duration::from_millis(200), async {
        while remaining > 0 {
            match body.frame().await {
                Some(Ok(frame)) => {
                    if let Ok(data) = frame.into_data() {
                        remaining = remaining.saturating_sub(data.len());
                    }
                }
                _ => break,
            }
        }
    })
    .await;
}
fn parse_request_bytes(raw: &[u8]) -> Result<Value, ApiError> {
    if raw.is_empty() {
        return parse_request_json("{}");
    }
    let (width, little, bom) = if raw.starts_with(&[0xff, 0xfe, 0, 0]) {
        (4, true, 4)
    } else if raw.starts_with(&[0, 0, 0xfe, 0xff]) {
        (4, false, 4)
    } else if raw.starts_with(&[0xff, 0xfe]) {
        (2, true, 2)
    } else if raw.starts_with(&[0xfe, 0xff]) {
        (2, false, 2)
    } else if raw.starts_with(&[0xef, 0xbb, 0xbf]) {
        (1, false, 3)
    } else if raw.len() >= 4 && raw[0] == 0 {
        (if raw[1] == 0 { 4 } else { 2 }, false, 0)
    } else if raw.len() >= 4 && raw[1] == 0 {
        (if raw[2] == 0 && raw[3] == 0 { 4 } else { 2 }, true, 0)
    } else if raw.len() == 2 && raw[0] == 0 {
        (2, false, 0)
    } else if raw.len() == 2 && raw[1] == 0 {
        (2, true, 0)
    } else {
        (1, false, 0)
    };
    let data = &raw[bom..];
    if width == 1 {
        let text = std::str::from_utf8(data).map_err(|error| {
            let start = error.valid_up_to();
            let end = start + error.error_len().unwrap_or(data.len() - start);
            let reason = if error.error_len().is_none() {
                "unexpected end of data"
            } else if matches!(data[start], 0xc2..=0xf4) {
                "invalid continuation byte"
            } else {
                "invalid start byte"
            };
            unicode_error("utf-8", data, start, end, reason)
        })?;
        return parse_request_json(text);
    }
    let codec = match (width, little) {
        (2, true) => "utf-16-le",
        (2, false) => "utf-16-be",
        (4, true) => "utf-32-le",
        _ => "utf-32-be",
    };
    if !data.len().is_multiple_of(width) {
        let start = bom + data.len() / width * width;
        return Err(unicode_error(
            codec,
            raw,
            start,
            raw.len(),
            "truncated data",
        ));
    }
    let text = if width == 2 {
        let units = data
            .as_chunks::<2>()
            .0
            .iter()
            .map(|bytes| {
                if little {
                    u16::from_le_bytes([bytes[0], bytes[1]])
                } else {
                    u16::from_be_bytes([bytes[0], bytes[1]])
                }
            })
            .collect::<Vec<_>>();
        String::from_utf16(&units).map_err(|_| ApiError::from(rusqlite::Error::InvalidQuery))?
    } else {
        let mut text = String::new();
        for (i, bytes) in data.as_chunks::<4>().0.iter().enumerate() {
            let bytes = [bytes[0], bytes[1], bytes[2], bytes[3]];
            let value = if little {
                u32::from_le_bytes(bytes)
            } else {
                u32::from_be_bytes(bytes)
            };
            let Some(c) = char::from_u32(value) else {
                if value > 0x10ffff {
                    return Err(unicode_error(
                        codec,
                        raw,
                        bom + i * 4,
                        bom + i * 4 + 4,
                        "code point not in range(0x110000)",
                    ));
                }
                return Err(rusqlite::Error::InvalidQuery.into());
            };
            text.push(c);
        }
        text
    };
    parse_request_json(&text)
}
fn unicode_error(codec: &str, raw: &[u8], start: usize, end: usize, reason: &str) -> ApiError {
    let message = if end == start + 1 {
        format!(
            "'{codec}' codec can't decode byte 0x{:02x} in position {start}: {reason}",
            raw[start]
        )
    } else {
        format!(
            "'{codec}' codec can't decode bytes in position {start}-{}: {reason}",
            end - 1
        )
    };
    ApiError::new(400, message)
}
fn parse_request_json(text: &str) -> Result<Value, ApiError> {
    let value = crate::library::parse_json(text).map_err(|error| {
        let position = error
            .message
            .rsplit_once("(char ")
            .and_then(|(_, s)| s.strip_suffix(')'))
            .and_then(|s| s.parse::<usize>().ok());
        if let Some(position) = position.filter(|_| error.message.starts_with("Expecting value:")) {
            let tail = text.chars().skip(position).collect::<String>();
            for token in ["-Infinity", "Infinity", "NaN"] {
                if tail.starts_with(token) {
                    let token = if token == "Infinity"
                        && position > 0
                        && text.chars().nth(position - 1) == Some('-')
                    {
                        "-Infinity"
                    } else {
                        token
                    };
                    return ApiError::new(400, format!("Giá trị số không hợp lệ: {token}"));
                }
            }
        }
        error
    })?;
    if !value.is_object() {
        return Err(ApiError::new(400, "Dữ liệu JSON phải là object."));
    }
    Ok(value)
}
fn api_response(
    status: u16,
    value: Value,
    head: bool,
    extra: Vec<(String, String)>,
) -> Response<ServiceBody> {
    let result = (|| {
        let mut value = value;
        public_content(&mut value)?;
        Ok::<_, ApiError>(json_bytes(&value))
    })();
    let (status, bytes) = match result {
        Ok(bytes) => (status, bytes),
        Err(e) => (e.status, json_bytes(&e.json())),
    };
    let len = bytes.len();
    let mut response = Response::builder()
        .status(status)
        .header("Content-Type", "application/json; charset=utf-8")
        .header("Content-Length", len)
        .header("Cache-Control", "no-store")
        .header("X-Content-Type-Options", "nosniff")
        .header("Referrer-Policy", "no-referrer")
        .header("Cross-Origin-Resource-Policy", "same-origin")
        .header("Content-Security-Policy", CSP);
    if status >= 400 {
        response = response.header("Connection", "close");
    }
    for (k, v) in extra {
        response = response.header(k, v);
    }
    response
        .body(ServiceBody::bytes(if head { Vec::new() } else { bytes }))
        .expect("validated API headers")
}
fn error_response(error: ApiError) -> Response<ServiceBody> {
    api_response(error.status, error.json(), false, Vec::new())
}

fn qt_member<'a>(body: &'a Value, key: &str) -> Result<&'a Value, ApiError> {
    body.get(key).ok_or_else(|| ApiError::new(404, key))
}

fn qt_kind(kind: &Value) -> Result<&'static str, ApiError> {
    if kind.is_object() || kind.is_array() {
        return Err(rusqlite::Error::InvalidQuery.into());
    }
    match kind.as_str() {
        Some(kind) => crate::qt::kind(kind),
        None => crate::qt::kind(&py_string(kind)),
    }
}

fn qt_string<'a>(value: &'a Value, error: &str) -> Result<&'a str, ApiError> {
    value.as_str().ok_or_else(|| ApiError::new(400, error))
}

fn qt_scope(body: &Value) -> Result<&str, ApiError> {
    crate::qt::scope(body.get("scope").map_or("*", |v| v.as_str().unwrap_or("")))
}

fn qt_response(
    library: &Library,
    path: &str,
    method: &str,
    query: &std::collections::HashMap<String, String>,
    body: &Value,
) -> Result<Value, ApiError> {
    let qt = library.qt()?;
    let kind = query.get("kind").map_or("name", String::as_str);
    let scope = query.get("scope").map_or("*", String::as_str);
    let q = query.get("q").map_or("", String::as_str);
    let word_error = "Từ và nghĩa phải là văn bản.";
    match (path, method) {
        ("/api/dictionaries", "GET") => {
            if query.contains_key("offset") || query.contains_key("limit") {
                let (offset, overflow) =
                    crate::library::parse_integer(query.get("offset").map_or("0", String::as_str))?;
                let limit =
                    crate::library::integer(query.get("limit").map_or("100", String::as_str))?;
                let offset = (!(overflow && offset > 0)).then_some(offset);
                qt.page_input(kind, scope, q, offset, limit)
            } else {
                qt.list(kind, scope, q)
            }
        }
        ("/api/dictionaries", "POST") => {
            let kind = qt_member(body, "kind")?;
            let word = qt_member(body, "word")?;
            let meaning = qt_member(body, "meaning")?;
            let kind = qt_kind(kind)?;
            let scope = qt_scope(body)?;
            qt.save(
                kind,
                qt_string(word, word_error)?,
                qt_string(meaning, word_error)?,
                scope,
            )
        }
        ("/api/dictionaries", "DELETE") => {
            let kind = query
                .get("kind")
                .ok_or_else(|| ApiError::new(404, "kind"))?;
            let word = query
                .get("word")
                .ok_or_else(|| ApiError::new(404, "word"))?;
            qt.delete(kind, word, scope)
        }
        ("/api/dictionaries/import", "POST") => {
            let kind = qt_member(body, "kind")?;
            let text = qt_member(body, "text")?;
            let kind = qt_kind(kind)?;
            let scope = qt_scope(body)?;
            let mode = body
                .get("mode")
                .map_or("merge", |v| v.as_str().unwrap_or(""));
            if !["merge", "missing", "replace"].contains(&mode) {
                return Err(ApiError::new(
                    400,
                    "Chế độ nhập phải là merge, missing hoặc replace.",
                ));
            }
            let text = qt_string(text, "Tệp từ điển phải là văn bản, tối đa 32 triệu ký tự.")?;
            qt.import_text(kind, text, mode, scope)
        }
        ("/api/dictionaries/export", "GET") => Ok(json!({"text":qt.export_text(kind, scope)?})),
        ("/api/dictionaries/rules/preview", "POST") => {
            let pattern = qt_member(body, "pattern")?.as_str();
            let replacement = qt_member(body, "replacement")?.as_str();
            let sample = qt_string(
                qt_member(body, "sample")?,
                "Mẫu thử luật tối đa 100.000 ký tự.",
            )?;
            let book_id = body.get("book_id").unwrap_or(&Value::Null);
            qt.preview_rule_input(pattern, replacement, sample, book_id)
        }
        ("/api/qt", "POST") => {
            let text = qt_string(
                qt_member(body, "text")?,
                "Nội dung QT phải là văn bản, tối đa 2 triệu ký tự.",
            )?;
            if text.chars().count() > 2_000_000 {
                return Err(ApiError::new(
                    400,
                    "Nội dung QT phải là văn bản, tối đa 2 triệu ký tự.",
                ));
            }
            let book_id = body
                .get("book_id")
                .map(|v| qt_string(v, "Mã truyện không hợp lệ."))
                .transpose()?
                .unwrap_or("");
            Ok(json!({"text":qt.translate(text, book_id)?}))
        }
        _ => Err(ApiError::new(404, "Không tìm thấy API.")),
    }
}
fn capabilities(library: &Library, project: &Path) -> Result<Value, ApiError> {
    let mut formats = crate::documents::capabilities();
    formats["legacy"] = crate::legacy_formats::capabilities();
    let mut extensions = crate::documents::supported_extensions();
    extensions.extend(crate::legacy_formats::SUPPORTED_EXTENSIONS);
    let ocr = crate::platform::ocr_capabilities(&project.join("windows").join("pdfocr.ps1"));
    let supported = ocr["supported"] == true;
    formats["ocr"] = ocr.clone();
    formats["pdf"] = json!({"supported": supported, "details": ocr});
    if supported {
        extensions.push(".pdf");
    }
    extensions.sort_unstable();
    extensions.dedup();
    formats["supported_extensions"] = extensions.into();
    Ok(json!({
        "name":"Kệ Truyện", "documents":formats, "sources":crate::sources::catalog(), "qt":library.qt()?.status()?,
        "tts":{"browser":true,"offline":"Theo giọng localService của trình duyệt",
            "online":"Theo giọng online của trình duyệt","audio_export":false,"voice_models":false},
        "ocr":ocr, "data_directory":display_path(library.root()),
        "limitations":["Chưa nghiệm thu toàn bộ requirements. Giọng TTS phải có trên máy/trình duyệt.",
            "PDF/OCR và định dạng phụ thuộc converter được báo theo engine thực có."]
    }))
}

/// Cover bytes for a Desktop icon; acquisition failures fall back to the default icon.
fn shortcut_cover(library: &Library, book: &Value) -> Vec<u8> {
    let cover = book["cover"].as_str().unwrap_or_default();
    if cover.is_empty() {
        return Vec::new();
    }
    let embedded = raster(cover);
    if !embedded.is_empty() {
        return embedded
            .split_once(',')
            .and_then(|(_, data)| STANDARD.decode(data).ok())
            .unwrap_or_default();
    }
    let referer = book["path"].as_str().unwrap_or_default();
    let fetch = |url: &str, referer: &str| -> Option<Vec<u8>> {
        let url = remote(url);
        if url.is_empty() {
            return None;
        }
        let name = format!("{:x}", Sha256::digest(url.as_bytes()));
        let candidate = library.root().join("images").join(&name);
        let _asset = library.asset_leases.acquire(candidate.clone());
        if let Ok(bytes) = std::fs::read(&candidate) {
            return Some(bytes);
        }
        library
            .sources
            .enabled()
            .then(|| {
                // `Ok(None)` means the image is on disk (possibly saved by a concurrent download).
                library
                    .save_source_image(&url, referer, None)
                    .ok()
                    .and_then(|bytes| bytes.or_else(|| std::fs::read(&candidate).ok()))
            })
            .flatten()
    };
    if let Some(bytes) = fetch(cover, referer) {
        return bytes;
    }
    if !library.sources.enabled() {
        return Vec::new();
    }
    let source = book["source"].as_str().unwrap_or_default();
    let Ok(metadata) = library.sources.cover(source, referer) else {
        return Vec::new();
    };
    let image = metadata["cover"].as_str().unwrap_or_default().to_owned();
    let page = metadata["path"].as_str().unwrap_or(referer).to_owned();
    match fetch(&image, &page) {
        Some(bytes) => {
            let _ = library.update_book(
                book["id"].as_str().unwrap_or_default(),
                &json!({"cover":image}),
            );
            bytes
        }
        None => Vec::new(),
    }
}

fn pdf_response(
    library: &Library,
    book: &Value,
    head: bool,
) -> Result<Response<ServiceBody>, ApiError> {
    // Path.resolve(): canonical when the path exists, absolute otherwise.
    let resolve = |path: &Path| {
        std::fs::canonicalize(path)
            .map(|p| std::path::PathBuf::from(display_path(&p)))
            .unwrap_or_else(|_| std::path::absolute(path).unwrap_or_else(|_| path.to_owned()))
    };
    let uploads = resolve(&library.root().join("files"));
    let file = resolve(Path::new(book["path"].as_str().unwrap_or_default()));
    if !file.starts_with(&uploads) {
        return Err(ApiError::new(
            403,
            "Tệp không thuộc thư viện nhập của ứng dụng.",
        ));
    }
    let name = file
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    if crate::documents::suffix(&name).to_lowercase() != ".pdf" {
        return Err(ApiError::new(
            400,
            "Chức năng xem trang gốc hiện dành cho PDF.",
        ));
    }
    let asset = library
        .asset_leases
        .acquire(library.root().join("files").join(&name));
    let handle = std::fs::File::open(&file).map_err(|e| {
        if e.kind() == std::io::ErrorKind::NotFound {
            // Python: FileNotFoundError -> 404 with str(error).strip("'").
            let text = format!(
                "[Errno 2] No such file or directory: {}",
                crate::library::py_repr_full(&display_path(&file))
            );
            ApiError::new(404, text.trim_matches('\''))
        } else {
            ApiError::from(e)
        }
    })?;
    let length = handle.metadata()?.len();
    Ok(Response::builder()
        .status(200)
        .header("Content-Type", "application/pdf")
        .header("Content-Length", length)
        .header("X-Content-Type-Options", "nosniff")
        .header("Referrer-Policy", "no-referrer")
        .header("Cross-Origin-Resource-Policy", "same-origin")
        .header("Content-Security-Policy", CSP)
        .header("Cache-Control", "private, max-age=60")
        .header("Content-Disposition", "inline; filename=document.pdf")
        .body(if head {
            ServiceBody::bytes(Vec::new())
        } else {
            ServiceBody::file(handle, length, asset)
        })
        .expect("PDF headers"))
}

fn backup_source(body: &Value, project: &Path) -> Result<std::path::PathBuf, ApiError> {
    use std::path::PathBuf;
    // `body.get("path") or default`: every falsy JSON value selects the default.
    let given = body.get("path").filter(|v| match v {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|f| f != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    });
    let text = match given {
        None => return Ok(project.parent().unwrap_or(project).to_owned()),
        Some(Value::String(text)) => text.clone(),
        Some(_) => {
            return Err(ApiError::new(
                500,
                "expected str, bytes or os.PathLike object",
            ));
        }
    };
    // Path.expanduser: only a leading "~" naming the current user's profile.
    if let Some(rest) = text.strip_prefix('~')
        && (rest.is_empty() || rest.starts_with(['/', '\\']))
        && let Some(home) = std::env::var_os("USERPROFILE")
    {
        return Ok(PathBuf::from(home).join(rest.trim_start_matches(['/', '\\'])));
    }
    Ok(PathBuf::from(text))
}

#[allow(clippy::too_many_arguments)]
fn respond_writable(
    library: &Library,
    root: &Path,
    token: &str,
    port: u16,
    parts: hyper::http::request::Parts,
    body: Option<Value>,
    upload: Option<Vec<u8>>,
    shutdown_busy: &std::sync::Mutex<()>,
    accepted: &crate::lifecycle::ShutdownAck,
    stopping: &std::sync::atomic::AtomicBool,
    downloads: Option<&crate::downloads::Downloads>,
) -> Response<ServiceBody> {
    let head = parts.method == "HEAD";
    let request_method = parts.method.clone();
    let method = if head { "GET" } else { request_method.as_str() };
    let path = decode(parts.uri.path(), false);
    if !path.starts_with("/api/")
        || parts.method == "OPTIONS"
        || !["GET", "HEAD", "POST", "DELETE"].contains(&parts.method.as_str())
    {
        return respond(library, root, token, port, parts);
    }
    // PowerShell -File rejects verbatim (\?) paths, so hand helpers the display form.
    let project = std::path::PathBuf::from(display_path(root.parent().unwrap_or(root)));
    let project = project.as_path();
    let result = (|| {
        guard(&parts, port)?;
        let _database = if path == "/api/cache/chapters/compact" && method == "POST"
            || matches!(
                path.as_str(),
                "/api/instance" | "/api/sources" | "/api/shutdown"
            ) {
            None
        } else {
            Some(
                library.database_gate.try_read().map_err(|_| {
                    ApiError::new(400, "Đang thu gọn cơ sở dữ liệu; hãy thử lại sau.")
                })?,
            )
        };
        if path == "/api/books" {
            return Ok(respond(library, root, token, port, parts));
        }
        let body = body.as_ref().unwrap_or(&Value::Null);
        let jobs = || downloads.ok_or_else(|| ApiError::new(500, "Không mở được hàng đợi tải."));
        let mut extra = Vec::new();
        let mut status = 200;
        let pieces = path
            .split('/')
            .filter(|s| !s.is_empty())
            .collect::<Vec<_>>();
        let mut query = std::collections::HashMap::new();
        for pair in parts.uri.query().unwrap_or("").split('&') {
            let (k, v) = pair.split_once('=').unwrap_or((pair, ""));
            if !v.is_empty() {
                query
                    .entry(decode(k, true))
                    .or_insert_with(|| decode(v, true));
            }
        }
        let value = match (path.as_str(), method) {
            ("/api/capabilities", "GET") => capabilities(library, project)?,
            ("/api/import/file", "POST") => {
                let name = parts.headers.get("X-Filename").map_or_else(
                    || "book.txt".to_owned(),
                    |v| v.as_bytes().iter().map(|&b| char::from(b)).collect(),
                );
                library.import_file(
                    &decode(&name, false),
                    upload.as_deref().unwrap_or_default(),
                    project,
                )?
            }
            ("/api/import/backup", "POST") => {
                library.import_backup(&backup_source(body, project)?)?
            }
            ("/api/dictionaries", "GET" | "POST" | "DELETE")
            | (
                "/api/dictionaries/import" | "/api/dictionaries/rules/preview" | "/api/qt",
                "POST",
            )
            | ("/api/dictionaries/export", "GET") => {
                qt_response(library, &path, method, &query, body)?
            }
            ("/api/instance", "GET") => {
                json!({"app":"ke-truyen-local","data_directory":display_path(library.root()),"pid":std::process::id()})
            }
            ("/api/settings", _) => {
                if method == "GET" && session(&parts, token) {
                    extra.push(("X-Shutdown-Token".into(), token.into()));
                }
                library.settings(if method == "POST" { Some(body) } else { None })?
            }
            ("/api/groups", "GET") => library.groups()?,
            ("/api/groups", "POST") => library.create_group(body)?,
            ("/api/sources", "GET") => crate::sources::catalog(),
            ("/api/sources/stv/captcha", "GET") => {
                library
                    .sources
                    .stv
                    .as_ref()
                    .ok_or_else(captcha_expired)?
                    .captcha("image", query.get("id").map_or("", String::as_str), None)?
                    .body
            }
            ("/api/jobs", "GET") => {
                let (offset, overflow) =
                    crate::library::parse_integer(query.get("offset").map_or("0", String::as_str))?;
                if overflow && offset > 0 {
                    return Err(rusqlite::Error::InvalidQuery.into());
                }
                jobs()?.list(offset)?
            }
            ("/api/readers", "POST") => library.reader(body)?,
            ("/api/cache/chapters", "GET") => library.chapter_cache_stats()?,
            ("/api/cache/chapters", "POST") => {
                library.set_chapter_cache_limit(&body["max_chapters"])?
            }
            ("/api/cache/chapters/clear", "POST") => library.clear_chapter_cache()?,
            ("/api/cache/chapters/legacy", "POST") => {
                library.reclassify_legacy_chapters(body["confirmed"] == true)?
            }
            ("/api/cache/chapters/compact", "POST") => {
                library.compact_database(body["confirmed"] == true)?
            }
            ("/api/cache/images", "GET") => library.image_cache_stats()?,
            ("/api/cache/images", "POST") => library.set_image_cache_limit(&body["max_bytes"])?,
            ("/api/cache/images/clear", "POST") => library.clear_image_cache()?,
            ("/api/notes", "GET") => library.notes(query.get("book_id").map(String::as_str))?,
            ("/api/notes", "POST") => library.save_note(body)?,
            ("/api/shutdown", "POST") => {
                let _busy = match shutdown_busy.try_lock() {
                    Ok(lock) => lock,
                    Err(_) => {
                        return Ok(api_response(
                            409,
                            json!({"error":"Đang lưu trạng thái tắt máy chủ.","code":"busy_operation"}),
                            head,
                            Vec::new(),
                        ));
                    }
                };
                let operations = library.critical_operations.lock();
                let names = operations.iter().map(|(name, _)| *name).collect::<Vec<_>>();
                let database = library.database_gate.try_read();
                if !names.is_empty() || database.is_err() {
                    let joined = if names.is_empty() {
                        "thu gọn SQLite".to_owned()
                    } else {
                        names.join(", ")
                    };
                    return Ok(api_response(
                        409,
                        json!({
                            "error": format!("Hãy chờ thao tác kết thúc: {joined}"),
                            "code":"busy_operation","operations":names
                        }),
                        head,
                        Vec::new(),
                    ));
                }
                let _database = database;
                let value = match if let Some(downloads) = downloads {
                    downloads.pause_for_shutdown(body["pause_downloads"] == true)
                } else {
                    library.pause_downloads(body["pause_downloads"] == true)
                } {
                    Ok(v) => v,
                    Err(_) => {
                        json!({"error":"Thư viện đang ghi dữ liệu; hãy thử lại.","code":"busy_operation"})
                    }
                };
                if value["accepted"] == true {
                    extra.push(("Connection".into(), "close".into()));
                    stopping.store(true, std::sync::atomic::Ordering::Release);
                    accepted.accepted();
                } else {
                    status = 409;
                }
                value
            }
            ("/api/image", "GET") => {
                let url = query.get("url").ok_or_else(|| ApiError::new(404, "url"))?;
                return image_response(
                    library,
                    url,
                    query.get("referer").map_or("", String::as_str),
                    &parts,
                );
            }
            _ => {
                if let ["api", "sources", id, action] = pieces.as_slice() {
                    let url = query.get("url").map_or("", String::as_str);
                    match (*action, method) {
                        ("search", "GET") => library.sources.search(
                            id,
                            query.get("q").map_or("", String::as_str),
                            crate::library::integer(query.get("page").map_or("1", String::as_str))?,
                        )?,
                        ("detail", "GET") => {
                            let mut detail = library.sources.detail(id, url)?;
                            let path = detail["path"].as_str().unwrap_or_default().to_owned();
                            detail["library"] = library.source_book_state(id, &path)?;
                            detail
                        }
                        ("add", "POST") => {
                            let url = body.get("url").ok_or_else(|| ApiError::new(404, "url"))?;
                            let url = url.as_str().ok_or(rusqlite::Error::InvalidQuery)?;
                            let shelf = match body.get("shelf") {
                                None => true,
                                Some(Value::Bool(shelf)) => *shelf,
                                Some(_) => {
                                    return Err(ApiError::new(
                                        400,
                                        "shelf phải là true hoặc false.",
                                    ));
                                }
                            };
                            library.add_source_book(library.sources.detail(id, url)?, shelf)?
                        }
                        _ => return Err(ApiError::new(404, "Không tìm thấy API.")),
                    }
                } else if let ["api", "jobs", id, action] = pieces.as_slice()
                    && method == "POST"
                {
                    jobs()?.action(id, action)?
                } else if let ["api", "sources", "stv", "captcha", action] = pieces.as_slice()
                    && method == "POST"
                {
                    if !matches!(*action, "refresh" | "verify" | "cancel") {
                        return Err(ApiError::new(404, "Không có thao tác CAPTCHA này."));
                    }
                    let stv = library.sources.stv.as_ref().ok_or_else(captcha_expired)?;
                    let result = stv.captcha(
                        action,
                        body["id"].as_str().unwrap_or_default(),
                        body["answer"].as_str(),
                    )?;
                    match *action {
                        "refresh" => jobs()?.update_captcha(
                            &result.key,
                            json!({"source":"stv","id":result.body["id"]}),
                        ),
                        "verify" => {
                            jobs()?.resolve_verification(&result.key, true)?;
                        }
                        "cancel" => jobs()?.resolve_verification(&result.key, false)?,
                        _ => (),
                    }
                    result.body
                } else if pieces.len() == 3 && pieces[..2] == ["api", "notes"] && method == "DELETE"
                {
                    library.delete_note(pieces[2])?
                } else if let ["api", "groups", id] = pieces.as_slice()
                    && method == "DELETE"
                {
                    library.delete_group(id)?
                } else if let ["api", "groups", id, "rename"] = pieces.as_slice()
                    && method == "POST"
                {
                    library.rename_group(id, body)?
                } else if pieces.len() >= 3 && pieces[..2] == ["api", "books"] {
                    let id = pieces[2];
                    match (pieces.len(), method) {
                        (3, "GET") => library.get_book(id)?,
                        (3, "DELETE") => jobs()?.purge_book(id)?,
                        (4, "GET") if pieces[3] == "chapters" => library.chapters(id)?,
                        (4, "GET") if pieces[3] == "search" => library.search_content(
                            id,
                            query.get("q").map_or("", String::as_str),
                            query.get("after").map_or("-1", String::as_str),
                        )?,
                        (4, "GET") if pieces[3] == "cover" => {
                            let book = library.get_book(id)?;
                            let image = image_response(
                                library,
                                book["cover"].as_str().unwrap_or(""),
                                book["path"].as_str().unwrap_or(""),
                                &parts,
                            );
                            if let Ok(ref response) = image
                                && response.status().as_u16() != 503
                            {
                                return image;
                            }
                            if library.sources.enabled() {
                                let source = book["source"].as_str().unwrap_or_default();
                                let referer = book["path"].as_str().unwrap_or_default();
                                let cover =
                                    library.sources.cover(source, referer).and_then(|value| {
                                        let image = value["cover"].as_str().unwrap_or_default();
                                        let result =
                                            image_response(library, image, referer, &parts)?;
                                        library.update_book(id, &json!({"cover":image}))?;
                                        Ok(result)
                                    });
                                return cover.map_err(|err| {
                                    ApiError::new(
                                        400,
                                        format!(
                                            "Không tải được bìa cũ hoặc bìa trên trang nguồn: {err}"
                                        ),
                                    )
                                });
                            }
                            let message = if remote(book["path"].as_str().unwrap_or("")).is_empty()
                            {
                                "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng"
                            } else {
                                "Nguồn không khả dụng trong chế độ ngoại tuyến."
                            };
                            return Err(ApiError::new(
                                400,
                                format!(
                                    "Không tải được bìa cũ hoặc bìa trên trang nguồn: {message}"
                                ),
                            ));
                        }
                        (4, "POST") if pieces[3] == "progress" => library.progress(id, body)?,
                        (4, "POST") if pieces[3] == "update" => library.update_book(id, body)?,
                        (4, "POST") if pieces[3] == "trash" => {
                            if let Some(downloads) = downloads {
                                downloads.trash_book(id)?
                            } else {
                                library.trash_book(id)?
                            }
                        }
                        (4, "POST") if pieces[3] == "download" => {
                            library.get_book(id)?;
                            let n = |key, default| {
                                body.get(key)
                                    .map(crate::reader::int)
                                    .transpose()
                                    .map(|n| n.unwrap_or(default))
                            };
                            jobs()?.start(id, n("start", 0)?, n("count", -1)?)?
                        }
                        (4, "POST") if pieces[3] == "restore" => library.restore_book(id)?,
                        (4, "POST") if pieces[3] == "shortcut" => {
                            let book = library.get_book(id)?;
                            if book["trashed_at"].as_f64().unwrap_or_default() != 0.0 {
                                return Err(ApiError::new(
                                    400,
                                    "Hãy khôi phục truyện khỏi thùng rác trước khi tạo lối tắt.",
                                ));
                            }
                            let cover = shortcut_cover(library, &book);
                            let launcher = crate::platform::launcher(project)?;
                            let mut result = crate::platform::create_shortcut(
                                &project.join("windows").join("shortcut.ps1"),
                                &launcher,
                                book["id"].as_str().unwrap_or_default(),
                                book["name"].as_str().unwrap_or_default(),
                                &cover,
                                library.root(),
                                port,
                                None,
                            )?;
                            if result["used_cover"] != true {
                                result["warning"] =
                                    "Chưa lấy được bìa hợp lệ; lối tắt dùng biểu tượng mặc định."
                                        .into();
                            }
                            result
                        }
                        (4, "GET") if pieces[3] == "file" => {
                            return pdf_response(library, &library.get_book(id)?, head);
                        }
                        (6, "POST") if pieces[3] == "chapters" && pieces[5] == "original" => {
                            let index = crate::library::integer(pieces[4])?;
                            let book = library.get_book(id)?;
                            let chapter = library.raw_chapter(id, index)?;
                            library.sources.open_original(
                                book["source"].as_str().unwrap_or_default(),
                                chapter["url"].as_str().unwrap_or_default(),
                            )?
                        }
                        (6, "POST") if pieces[3] == "chapters" && pieces[5] == "prefetch" => {
                            library.prefetch_chapter(
                                id,
                                crate::library::integer(pieces[4])?,
                                &|| stopping.load(std::sync::atomic::Ordering::Acquire),
                            )?
                        }
                        (6, "POST") if pieces[3] == "chapters" && pieces[5] == "edit" => library
                            .edit_chapter(id, crate::library::sqlite_integer(pieces[4])?, body)?,
                        (6, "POST") if pieces[3] == "chapters" && pieces[5] == "restore" => library
                            .restore_chapter(id, crate::library::sqlite_integer(pieces[4])?)?,
                        (5, "GET") if pieces[3] == "chapters" => {
                            let index = crate::library::integer(pieces[4])?;
                            let reader = parts
                                .headers
                                .get("X-Reader-ID")
                                .and_then(|v| v.to_str().ok());
                            let sequence = crate::library::integer(
                                parts
                                    .headers
                                    .get("X-Reader-Seq")
                                    .and_then(|v| v.to_str().ok())
                                    .unwrap_or("0"),
                            )?;
                            library.chapter(id, index, reader, sequence)?
                        }
                        _ => return Err(ApiError::new(404, "Không tìm thấy API.")),
                    }
                } else {
                    return Err(ApiError::new(404, "Không tìm thấy API."));
                }
            }
        };
        Ok(api_response(status, value, head, extra))
    })();
    result.unwrap_or_else(|e| api_response(e.status, e.json(), head, Vec::new()))
}
fn captcha_expired() -> ApiError {
    ApiError::new(400, "Yêu cầu CAPTCHA đã hết hiệu lực. Hãy mở lại chương.")
        .with_code("captcha_expired")
}

fn display_path(path: &Path) -> String {
    let s = path.to_string_lossy();
    s.strip_prefix(r"\\?\").unwrap_or(&s).to_owned()
}
fn etag_matches(parts: &hyper::http::request::Parts, etag: &str) -> Option<bool> {
    parts
        .headers
        .get("If-None-Match")
        .and_then(|v| v.to_str().ok())
        .map(|matching| {
            matching.trim() == "*"
                || matching
                    .split(',')
                    .any(|v| v.trim().trim_start_matches("W/") == etag.trim_start_matches("W/"))
        })
}

fn uncached_image(bytes: Vec<u8>, parts: &hyper::http::request::Parts) -> Response<ServiceBody> {
    let etag = format!("\"{:x}\"", Sha256::digest(&bytes));
    let unchanged = etag_matches(parts, &etag).unwrap_or(false);
    let mut response = Response::builder()
        .status(if unchanged { 304 } else { 200 })
        .header("Content-Type", image_kind(&bytes))
        .header("ETag", etag)
        .header("Cache-Control", "private, max-age=60, must-revalidate")
        .header("X-Content-Type-Options", "nosniff")
        .header("Referrer-Policy", "no-referrer")
        .header("Cross-Origin-Resource-Policy", "same-origin")
        .header("Content-Security-Policy", CSP);
    if !unchanged {
        response = response.header("Content-Length", bytes.len());
    }
    response
        .body(ServiceBody::bytes(if unchanged || parts.method == "HEAD" {
            Vec::new()
        } else {
            bytes
        }))
        .expect("fixed image headers")
}

pub(crate) fn image_kind(bytes: &[u8]) -> &'static str {
    if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png"
    } else if bytes.starts_with(b"\xff\xd8\xff") {
        "image/jpeg"
    } else if bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a") {
        "image/gif"
    } else if bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP") {
        "image/webp"
    } else if bytes.starts_with(b"BM") {
        "image/bmp"
    } else {
        "application/octet-stream"
    }
}
fn image_response(
    library: &Library,
    url: &str,
    referer: &str,
    parts: &hyper::http::request::Parts,
) -> Result<Response<ServiceBody>, ApiError> {
    use std::io::{Read, Seek};
    let url = remote(url);
    if url.is_empty() {
        return Err(ApiError::new(
            400,
            "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
        )
        .with_code("source_error"));
    }
    let name = format!("{:x}", Sha256::digest(url.as_bytes()));
    let candidate = library.root().join("images").join(&name);
    let asset = library.asset_leases.acquire(candidate.clone());
    if !candidate.exists() {
        if !referer.is_empty() && remote(referer).is_empty() {
            return Err(ApiError::new(
                400,
                "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
            )
            .with_code("source_error"));
        }
        if library.sources.enabled() {
            if let Some(bytes) = library.save_source_image(&url, referer, None)? {
                return Ok(uncached_image(bytes, parts));
            }
        } else {
            return Ok(api_response(
                503,
                json!({"error":"Nguồn không khả dụng trong chế độ ngoại tuyến.","code":"network_error"}),
                parts.method == "HEAD",
                Vec::new(),
            ));
        }
    }
    let path = checked_path(&candidate)?;
    if !path.starts_with(library.root()) {
        return Err(ApiError::new(403, "Đường dẫn cache ảnh không hợp lệ."));
    }
    let mut options = std::fs::OpenOptions::new();
    options.read(true);
    #[cfg(windows)]
    {
        use std::os::windows::fs::OpenOptionsExt;
        options.share_mode(1);
    }
    // Deny replacement while the safe Windows identity helper opens its metadata handle.
    let mut file = options.open(&path)?;
    let info = file.metadata()?;
    let mut signature = [0; 12];
    let n = file.read(&mut signature)?;
    file.rewind()?;
    let kind = image_kind(&signature[..n]);
    library.touch_image(&name, info.len())?;
    let identity = file_identity(&path, &info, &file)?;
    let etag = format!("W/\"{:x}\"", Sha256::digest(identity.as_bytes()));
    let unchanged = etag_matches(parts, &etag).unwrap_or_else(|| {
        parts
            .headers
            .get("If-Modified-Since")
            .and_then(|v| v.to_str().ok())
            .and_then(|s| httpdate::parse_http_date(s).ok())
            .is_some_and(|date| {
                info.modified().ok().is_some_and(|mtime| {
                    mtime
                        .duration_since(std::time::UNIX_EPOCH)
                        .map(|t| t.as_secs())
                        .unwrap_or(0)
                        <= date
                            .duration_since(std::time::UNIX_EPOCH)
                            .map(|t| t.as_secs())
                            .unwrap_or(0)
                })
            })
    });
    let mut response = Response::builder()
        .status(if unchanged { 304 } else { 200 })
        .header("Content-Type", kind)
        .header("ETag", etag)
        .header("Last-Modified", httpdate::fmt_http_date(info.modified()?))
        .header("Cache-Control", "private, max-age=60, must-revalidate")
        .header("X-Content-Type-Options", "nosniff")
        .header("Referrer-Policy", "no-referrer")
        .header("Cross-Origin-Resource-Policy", "same-origin")
        .header("Content-Security-Policy", CSP);
    if !unchanged {
        response = response.header("Content-Length", info.len());
    }
    Ok(response
        .body(if unchanged || parts.method == "HEAD" {
            ServiceBody::bytes(Vec::new())
        } else {
            ServiceBody::file(file, info.len(), asset)
        })
        .expect("image headers"))
}
fn file_identity(
    path: &Path,
    info: &std::fs::Metadata,
    file: &std::fs::File,
) -> Result<String, ApiError> {
    let modified = info
        .modified()?
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    #[cfg(windows)]
    let (inode, changed_ns) = crate::windows_metadata::identity(file)?;
    #[cfg(unix)]
    let inode = {
        use std::os::unix::fs::MetadataExt;
        info.ino()
    };
    #[cfg(unix)]
    let changed_ns = {
        use std::os::unix::fs::MetadataExt;
        i128::from(info.ctime()) * 1000000000 + i128::from(info.ctime_nsec())
    };
    #[cfg(not(windows))]
    let _ = file;
    Ok(format!(
        "{}:{inode}:{}:{}:{}",
        display_path(path),
        info.len(),
        modified.as_nanos(),
        changed_ns
    ))
}
struct IdleIo {
    stream: tokio::net::TcpStream,
    read: Option<std::pin::Pin<Box<tokio::time::Sleep>>>,
    write: Option<std::pin::Pin<Box<tokio::time::Sleep>>>,
    // Requests still being handled; a slow import or OCR is not an idle client.
    busy: Arc<std::sync::atomic::AtomicUsize>,
}
struct Busy(Arc<std::sync::atomic::AtomicUsize>);
impl Busy {
    fn enter(counter: &Arc<std::sync::atomic::AtomicUsize>) -> Self {
        counter.fetch_add(1, std::sync::atomic::Ordering::AcqRel);
        Self(counter.clone())
    }
}
impl Drop for Busy {
    fn drop(&mut self) {
        self.0.fetch_sub(1, std::sync::atomic::Ordering::AcqRel);
    }
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
        if result.is_pending() && self.busy.load(std::sync::atomic::Ordering::Acquire) > 0 {
            self.read = None;
            return result;
        }
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
/// Read-only fixture server for the regression tests (`--fixture`).
pub fn serve_fixture(
    fixture: &Path,
    web_root: &Path,
    port: u16,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    checked_path(web_root)?;
    let listener = std::net::TcpListener::bind(("127.0.0.1", port))?;
    run_server(Library::open_fixture(fixture)?, web_root, listener)
}
/// Serve an already opened library on a bound loopback listener (production launcher).
pub(crate) fn serve_library(
    library: Library,
    web_root: &Path,
    listener: std::net::TcpListener,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    checked_path(web_root)?;
    run_server(library, web_root, listener)
}
fn run_server(
    library: Library,
    web_root: &Path,
    listener: std::net::TcpListener,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    listener.set_nonblocking(true)?;
    let root = Arc::new(checked_path(web_root)?);
    let library = Arc::new(library);
    let downloads = if library.writable() {
        let owner = library.clone();
        Some(Arc::new(if owner.sources.enabled() {
            crate::downloads::Downloads::open(library.clone(), move |job, control| {
                owner.download_source_chapter(job, control)
            })?
        } else {
            crate::downloads::Downloads::offline(library.clone())
        }))
    } else {
        None
    };
    if let (Some(downloads), Some(stv)) = (&downloads, &library.sources.stv) {
        let stv = stv.clone();
        assert!(downloads.on_verification_committed(move |key| stv.complete_verification(key)));
    }
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
        let listener = tokio::net::TcpListener::from_std(listener)?;
        let port = listener.local_addr()?.port();
        println!("{}", json!({"port":port}));
        std::io::stdout().flush()?;
        let permits = Arc::new(tokio::sync::Semaphore::new(64));
        let work_permits = Arc::new(tokio::sync::Semaphore::new(64));
        let shutdown = Arc::new(tokio::sync::Notify::new());
        let shutdown_busy = Arc::new(std::sync::Mutex::new(()));
        let stopping = Arc::new(std::sync::atomic::AtomicBool::new(false));
        let maintenance_stop = Arc::new(tokio::sync::Notify::new());
        let maintenance = library.writable().then(|| {
            let library = library.clone();
            let stopping = stopping.clone();
            let maintenance_stop = maintenance_stop.clone();
            tokio::spawn(async move {
                let mut failures = 0usize;
                let mut next_trash = std::time::Instant::now();
                let mut next_truncate = std::time::Instant::now();
                let mut inventory = crate::assets::Inventory::default();
                while !stopping.load(std::sync::atomic::Ordering::Acquire) {
                    let owner = library.clone();
                    let stop = stopping.clone();
                    let check_trash = std::time::Instant::now() >= next_trash;
                    let check_truncate = std::time::Instant::now() >= next_truncate;
                    let result = tokio::task::spawn_blocking(move || {
                        let result = (|| {
                            let Ok(_database) = owner.database_gate.try_read() else {
                                return Ok((false, 0));
                            };
                            if stop.load(std::sync::atomic::Ordering::Acquire) {
                                return Ok((false, 0));
                            }
                            let expired = check_trash && owner.purge_expired_book()?;
                            let chapters = owner.maintain_chapter_cache()?;
                            let assets = owner.index_assets(&stop)?;
                            let search = owner.index_search(&stop)?;
                            let scanned = owner.inventory_assets(&mut inventory, &stop)?;
                            let queued = owner.queue_image_evictions(&stop)?;
                            let cleaned = owner.cleanup_assets(&stop)?;
                            let work = chapters + assets + search + scanned + queued + cleaned;
                            // Long-lived connections keep the WAL; reset it on an idle tick so it cannot grow without bound.
                            if check_truncate && work == 0 && !expired {
                                owner.truncate_wal();
                            }
                            Ok::<_,ApiError>((expired, work))
                        })();
                        (result, inventory)
                    }).await;
                    let result = match result {
                        Ok((result,next)) => {
                            inventory = next;
                            result
                        }
                        Err(error) => {
                            inventory = crate::assets::Inventory::default();
                            Err(ApiError::new(500, error.to_string()))
                        }
                    };
                    let delay = match result {
                        Ok((expired, work)) => {
                            failures = 0;
                            if check_trash && !expired {
                                next_trash = std::time::Instant::now() + std::time::Duration::from_secs(60);
                            }
                            if check_truncate && work == 0 && !expired {
                                next_truncate = std::time::Instant::now() + std::time::Duration::from_secs(30);
                            }
                            if work == 0 && !expired { 250 } else { 10 }
                        }
                        error => {
                            if failures == 0 {
                                eprintln!("Library maintenance will retry: {error:?}");
                            }
                            let delay = [250, 1000, 5000, 30000][failures.min(3)];
                            failures = failures.saturating_add(1);
                            delay
                        }
                    };
                    tokio::select! {
                        _ = maintenance_stop.notified() => break,
                        _ = tokio::time::sleep(std::time::Duration::from_millis(delay)) => (),
                    }
                }
            })
        });
        let mut connections = tokio::task::JoinSet::new();
        loop {
            while connections.try_join_next().is_some() {}
            let permit = tokio::select! { _ = shutdown.notified() => break, p = permits.clone().acquire_owned() => p? };
            let (stream, _) = tokio::select! { _ = shutdown.notified() => break, r = listener.accept() => r? };
            let library = library.clone();
            let downloads = downloads.clone();
            let root = root.clone();
            let token = token.clone();
            let work_permits = work_permits.clone();
            let shutdown = shutdown.clone();
            let shutdown_busy = shutdown_busy.clone();
            let stopping = stopping.clone();
            connections.spawn(async move {
                let _permit = permit;
                let accepted = Arc::new(crate::lifecycle::ShutdownAck::new(shutdown));
                let accepted_service = accepted.clone();
                let busy = Arc::new(std::sync::atomic::AtomicUsize::new(0));
                let busy_service = busy.clone();
                let stopping_service = stopping.clone();
                let service = hyper::service::service_fn(
                    move |request: hyper::Request<hyper::body::Incoming>| {
                        let library = library.clone();
                        let downloads = downloads.clone();
                        let root = root.clone();
                        let token = token.clone();
                        let work_permits = work_permits.clone();
                        let accepted = accepted_service.clone();
                        let shutdown_busy = shutdown_busy.clone();
                        let stopping = stopping_service.clone();
                        let (parts, mut incoming) = request.into_parts();
                        let busy_service = busy_service.clone();
                        async move {
                            if stopping.load(std::sync::atomic::Ordering::Acquire) { return Ok::<_,std::convert::Infallible>(error_response(ApiError::new(503,"Máy chủ đang tắt; hãy mở lại ứng dụng sau khi hoàn tất."))); }
                            let work_permit = work_permits
                                .acquire_owned()
                                .await
                                .expect("server-owned admission semaphore");
                            let drain_length=permission_drain_length(&parts);
                            let critical = if library.writable() {
                                match critical_operation(&parts).map(|name| {
                                    guard(&parts, port)?;
                                    library.critical_operations.enter(name, &stopping)
                                }).transpose() {
                                    Ok(operation) => operation,
                                    Err(error) => {
                                        if error.status == 403 { drain_permission_body(&mut incoming, drain_length).await; }
                                        return Ok(error_response(error));
                                    }
                                }
                            } else { None };
                            let body = if library.writable() { match request_json(&parts, &mut incoming, &token, port).await { Ok(body) => body, Err(error) => {if error.status==403{drain_permission_body(&mut incoming,drain_length).await;}return Ok(error_response(error));} } } else { None };
                            let upload = if library.writable() { match request_upload(&parts, &mut incoming, port).await { Ok(upload) => upload, Err(error) => {if error.status==403{drain_permission_body(&mut incoming,drain_length).await;}return Ok(error_response(error));} } } else { None };
                            let _in_flight = Busy::enter(&busy_service);
                            let result = tokio::task::spawn_blocking(move || {
                                let _critical = critical;
                                // Keep admission until synchronous work ends, even if the client disconnects.
                                let mut response = if library.writable() { respond_writable(&library, &root, &token, port, parts, body, upload, &shutdown_busy, &accepted, &stopping, downloads.as_deref()) } else { respond(&library, &root, &token, port, parts) };
                                response.body_mut().library = Some(library);
                                response.body_mut().permit = Some(Arc::new(work_permit));
                                response
                            })
                            .await;
                            let response=result.unwrap_or_else(|_| {
                                Response::builder()
                                    .status(500)
                                    .header("Connection", "close")
                                    .body(ServiceBody::bytes(Vec::new()))
                                    .expect("fixed response")
                            });
                            if response.status()==403{drain_permission_body(&mut incoming,drain_length).await;}
                            Ok::<_, std::convert::Infallible>(response)
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
                            busy,
                        }),
                        service,
                    );
                let _ = connection.await;
                accepted.closed();
            });
        }
        drop(listener);
        stopping.store(true,std::sync::atomic::Ordering::Release);
        library.sources.close();
        if let Some(downloads) = downloads { tokio::task::spawn_blocking(move || downloads.join()).await?; }
        maintenance_stop.notify_one();
        if let Some(maintenance) = maintenance { maintenance.await?; }
        if tokio::time::timeout(std::time::Duration::from_secs(2), async { while connections.join_next().await.is_some() {} }).await.is_err() { connections.abort_all(); while connections.join_next().await.is_some() {} }
        // Blocking work retains its admission and library lock after a disconnected request is cancelled.
        while work_permits.available_permits()!=64 { tokio::time::sleep(std::time::Duration::from_millis(50)).await; }
        if library.writable() { loop {let owner=library.clone();if tokio::task::spawn_blocking(move||owner.checkpoint()).await?.is_ok(){break;}tokio::time::sleep(std::time::Duration::from_secs(1)).await;} }
        Ok::<(), Box<dyn std::error::Error + Send + Sync>>(())
    })
}
fn respond(
    library: &Library,
    root: &Path,
    token: &str,
    port: u16,
    parts: hyper::http::request::Parts,
) -> Response<ServiceBody> {
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
    let (status, body) = result.unwrap_or_else(|e| (e.status, json_bytes(&e.json())));
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
        .body(ServiceBody::bytes(if method == "HEAD" {
            Vec::new()
        } else {
            body
        }))
        .expect("validated response headers")
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn m3_asset_resolution_preserves_python_same_scheme_references() {
        let base = "https://cdn.example.org/books/original.html?a=4";
        for (input, expected) in [
            ("https:panel.png", "https://cdn.example.org/books/panel.png"),
            ("HTTPS:panel.png", "https://cdn.example.org/books/panel.png"),
            ("https:/panel.png", "https://cdn.example.org/panel.png"),
            ("https:///panel.png", "https://cdn.example.org/panel.png"),
            ("///panel.png", "https://cdn.example.org/panel.png"),
            ("https:", base),
            ("//", base),
            ("https:#part", base),
            (
                "https:?x=1",
                "https://cdn.example.org/books/original.html?x=1",
            ),
            (
                "//public.example.org/panel.png",
                "https://public.example.org/panel.png",
            ),
            (
                "https:/api/image?url=https%3A%2F%2Fpublic.example.org%2Fpanel.png",
                "https://public.example.org/panel.png",
            ),
            (
                "http://user@[::1]/api/image?url=https%3A%2F%2Fpublic.example.org%2Fpanel.png",
                "https://public.example.org/panel.png",
            ),
        ] {
            assert_eq!(
                asset_image_urls(input, base).unwrap(),
                [expected],
                "{input}"
            );
        }
        assert!(asset_image_urls("http://[broken", base).is_err());
        assert_eq!(
            asset_markup_urls(r#"<img/src="https:panel.png">"#, base).unwrap(),
            ["https://cdn.example.org/books/panel.png"],
        );
    }
    #[test]
    fn permission_body_drain_is_small_and_time_bounded() {
        fn parts(length: &str) -> hyper::http::request::Parts {
            hyper::Request::builder()
                .method("POST")
                .header("Content-Length", length)
                .body(())
                .unwrap()
                .into_parts()
                .0
        }
        assert_eq!(permission_drain_length(&parts("65536")), Some(65536));
        let mut options = parts("2");
        options.method = hyper::Method::OPTIONS;
        assert_eq!(permission_drain_length(&options), None);
        assert_eq!(
            permission_drain_length(&hyper::Request::new(()).into_parts().0),
            None
        );
        for length in ["0", "-1", "65537", "invalid"] {
            assert_eq!(permission_drain_length(&parts(length)), None);
        }
        let mut chunked = parts("2");
        chunked.headers.insert(
            "Transfer-Encoding",
            hyper::header::HeaderValue::from_static("chunked"),
        );
        assert_eq!(permission_drain_length(&chunked), None);
        struct Stalled;
        impl hyper::body::Body for Stalled {
            type Data = Bytes;
            type Error = std::convert::Infallible;
            fn poll_frame(
                self: std::pin::Pin<&mut Self>,
                _: &mut std::task::Context<'_>,
            ) -> std::task::Poll<Option<Result<hyper::body::Frame<Bytes>, Self::Error>>>
            {
                std::task::Poll::Pending
            }
        }
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .unwrap();
        runtime.block_on(async {
            let mut complete = http_body_util::Full::new(Bytes::from_static(b"{}"));
            drain_permission_body(&mut complete, Some(2)).await;
            assert!(hyper::body::Body::is_end_stream(&complete));
            let start = std::time::Instant::now();
            drain_permission_body(&mut Stalled, Some(2)).await;
            assert!(start.elapsed() < std::time::Duration::from_secs(1));
            let mut ignored = http_body_util::Full::new(Bytes::from_static(b"{}"));
            drain_permission_body(&mut ignored, None).await;
            assert!(!hyper::body::Body::is_end_stream(&ignored));
        });
    }
    #[test]
    fn m2_json_depth_walkers_fit_bounded_worker_stack() {
        std::thread::Builder::new()
            .stack_size(2 * 1024 * 1024)
            .spawn(|| {
                for (depth, object) in [(512, false), (984, false), (512, true), (984, true)] {
                    let mut value = json!(0);
                    for _ in 0..depth {
                        value = if object {
                            let mut map = serde_json::Map::new();
                            map.insert("a".into(), value);
                            Value::Object(map)
                        } else {
                            Value::Array(vec![value])
                        };
                    }
                    validate_finite_json(&value).unwrap();
                    public_content(&mut value).unwrap();
                    let bytes = json_bytes(&value);
                    assert_eq!(bytes.len(), depth * if object { 7 } else { 2 } + 1);
                    assert_eq!(py_string(&value).len(), bytes.len());
                    let mut book = serde_json::Map::new();
                    book.insert("warnings".into(), value);
                    let mut page = serde_json::Map::new();
                    page.insert("items".into(), Value::Array(vec![Value::Object(book)]));
                    let mut page = Value::Object(page);
                    validate_finite_json(&page).unwrap();
                    public_content(&mut page).unwrap();
                    assert!(!json_bytes(&page).is_empty());
                    assert!(!py_string(&page).is_empty());
                }
            })
            .unwrap()
            .join()
            .unwrap();
    }
    #[test]
    fn m2_json_boundary_distinguishes_empty_body_from_bom_only() {
        assert_eq!(parse_request_bytes(&[]).unwrap(), json!({}));
        for raw in [
            &[0xef, 0xbb, 0xbf][..],
            &[0xff, 0xfe][..],
            &[0xfe, 0xff][..],
            &[0xff, 0xfe, 0, 0][..],
            &[0, 0, 0xfe, 0xff][..],
        ] {
            assert_eq!(
                parse_request_bytes(raw).unwrap_err().message,
                "Expecting value: line 1 column 1 (char 0)"
            );
        }
    }
    #[test]
    fn m2_review_body_encodings_match_python() {
        assert_eq!(
            parse_request_bytes(&[0xff]).unwrap_err().message,
            "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
        );
        assert_eq!(
            parse_request_bytes(&[0xff, 0xfe, b'{', 0, 0xff])
                .unwrap_err()
                .message,
            "'utf-16-le' codec can't decode byte 0xff in position 4: truncated data"
        );
        assert_eq!(
            parse_request_bytes(&[0, 0, 0xfe, 0xff, 0, 0x11, 0, 0])
                .unwrap_err()
                .message,
            "'utf-32-be' codec can't decode bytes in position 4-7: code point not in range(0x110000)"
        );
        let value = "{\"name\":\"Việt\"}";
        let expected = json!({"name":"Việt"});
        let mut utf8 = vec![0xef, 0xbb, 0xbf];
        utf8.extend_from_slice(value.as_bytes());
        assert_eq!(parse_request_bytes(&utf8).unwrap(), expected);
        for little in [true, false] {
            for bom in [true, false] {
                let mut utf16 = if bom {
                    if little {
                        vec![0xff, 0xfe]
                    } else {
                        vec![0xfe, 0xff]
                    }
                } else {
                    Vec::new()
                };
                for unit in value.encode_utf16() {
                    utf16.extend_from_slice(&if little {
                        unit.to_le_bytes()
                    } else {
                        unit.to_be_bytes()
                    });
                }
                assert_eq!(parse_request_bytes(&utf16).unwrap(), expected);
                let mut utf32 = if bom {
                    if little {
                        vec![0xff, 0xfe, 0, 0]
                    } else {
                        vec![0, 0, 0xfe, 0xff]
                    }
                } else {
                    Vec::new()
                };
                for unit in value.chars().map(u32::from) {
                    utf32.extend_from_slice(&if little {
                        unit.to_le_bytes()
                    } else {
                        unit.to_be_bytes()
                    });
                }
                assert_eq!(parse_request_bytes(&utf32).unwrap(), expected);
            }
        }
    }
    #[test]
    fn m2_review_image_miss_validates_referer_and_source_error() {
        let (_fixture, library) = crate::reader::tests::writable();
        for uri in [
            "/api/image?url=https%3A%2F%2Fcdn.example.org%2Fabsent&referer=http%3A%2F%2F127.0.0.1%2F",
            "/api/image?url=http%3A%2F%2F127.0.0.1%2F",
        ] {
            let (parts, _) = hyper::Request::builder()
                .uri(uri)
                .header("Host", "127.0.0.1:1234")
                .body(())
                .unwrap()
                .into_parts();
            let response = respond_writable(
                &library,
                Path::new("."),
                "token",
                1234,
                parts,
                None,
                None,
                &std::sync::Mutex::new(()),
                &crate::lifecycle::ShutdownAck::new(Arc::new(tokio::sync::Notify::new())),
                &std::sync::atomic::AtomicBool::new(false),
                None,
            );
            assert_eq!(response.status(), 400);
            assert_eq!(
                serde_json::from_slice::<Value>(response.body().bytes.as_ref().unwrap()).unwrap()["code"],
                "source_error"
            );
        }
    }
    #[test]
    fn m2_review_python_string_coercion_keeps_container_repr() {
        assert_eq!(
            py_string(&json!(["Việt",true,null,{"key":false}])),
            "['Việt', True, None, {'key': False}]"
        );
        assert_eq!(json_bytes(&crate::library::parse_json("-0").unwrap()), b"0");
        assert_eq!(
            validate_finite_json(&crate::library::parse_json("1e999").unwrap())
                .unwrap_err()
                .status,
            400
        );
    }
    #[test]
    fn group_writes_take_json_bodies() {
        let parts = |method: &str, uri: &str| {
            hyper::Request::builder()
                .method(method)
                .uri(uri)
                .body(())
                .unwrap()
                .into_parts()
                .0
        };
        assert!(json_route(&parts("POST", "/api/groups")));
        assert!(json_route(&parts("POST", "/api/groups/abc/rename")));
        assert!(!json_route(&parts("POST", "/api/groups/abc/other")));
        assert!(json_route(&parts(
            "POST",
            "/api/books/b/chapters/3/prefetch"
        )));
    }
    #[test]
    fn request_json_rejects_nonfinite_constants_with_reference_error() {
        assert_eq!(
            parse_request_json("{\"x\" NaN}").unwrap_err().message,
            crate::library::parse_json("{\"x\" NaN}")
                .unwrap_err()
                .message
        );
        for token in ["NaN", "Infinity", "-Infinity"] {
            assert_eq!(
                parse_request_json(&format!("{{\"value\":{token}}}"))
                    .unwrap_err()
                    .message,
                format!("Giá trị số không hợp lệ: {token}")
            );
        }
        assert_eq!(
            parse_request_json("[]").unwrap_err().message,
            "Dữ liệu JSON phải là object."
        );
        assert_eq!(
            parse_request_json("{\"x\":\"NaN\"}").unwrap(),
            json!({"x":"NaN"})
        );
    }
    #[test]
    fn file_identity_tracks_inode_size_and_times() {
        let path = std::env::temp_dir().join(format!("reader-m2-identity-{}", std::process::id()));
        std::fs::write(&path, b"image").unwrap();
        let modified = std::time::UNIX_EPOCH + std::time::Duration::from_secs(1_700_000_000);
        std::fs::File::options()
            .write(true)
            .open(&path)
            .unwrap()
            .set_modified(modified)
            .unwrap();
        let path = checked_path(&path).unwrap();
        let file = std::fs::File::open(&path).unwrap();
        let actual = file_identity(&path, &file.metadata().unwrap(), &file).unwrap();
        // Same layout as the retired reference's fstat key: path:inode:size:mtime_ns:ctime_ns.
        let fields: Vec<&str> = actual.rsplitn(5, ':').collect();
        assert_eq!(fields[4], display_path(&path));
        assert!(fields[3].parse::<u64>().unwrap() > 0, "{actual}");
        assert_eq!((fields[2], fields[1]), ("5", "1700000000000000000"));
        assert!(fields[0].parse::<i128>().unwrap() > 0, "{actual}");
        drop(file);
        std::fs::remove_file(path).unwrap();
    }
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
