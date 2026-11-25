use std::{
    borrow::Cow,
    collections::{HashMap, HashSet},
    fs::File,
    io::Read,
    path::Path,
    sync::Arc,
};

use base64::{Engine, engine::general_purpose::STANDARD};
use rusqlite::{OptionalExtension, TransactionBehavior, params};
use scraper::{ElementRef, Html, Selector};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

use crate::{
    ApiError,
    casefold::{casefold, python_whitespace},
    http::{clean_html, escape, html_to_text, quote, relative_remote, validate_html_limits},
    library::checked_path,
    source_fetch::PublicFetch,
    stv::{self, Stv},
};

const MAX_PAGE: usize = 8 * 1024 * 1024;
const SOURCES: [(&str, &str, &str, &[&str]); 4] = [
    (
        "qq",
        "Truyện QQ",
        "https://truyenqqko.com",
        &[
            "truyenqqko.com",
            "truyenqqgo.com",
            "truyenqqviet.com",
            "truyenqqto.com",
            "truyenqqno.com",
            "truyenqq.com",
            "truyenqqvn.com",
            "truyenqqpro.com",
        ],
    ),
    (
        "full",
        "Truyện Full",
        "https://truyenfull.live",
        &[
            "truyenfull.live",
            "truyenfull.vision",
            "truyenfull.io",
            "truyenfull.vn",
            "truyenfull.com",
        ],
    ),
    (
        "hako",
        "Hako",
        "https://docln.sbs",
        &["docln.sbs", "docln.net", "ln.hako.vn"],
    ),
    (
        "stv",
        "Sáng Tác Việt",
        "https://sangtacviet.com",
        &["sangtacviet.com", "sangtacviet.vip"],
    ),
];

fn failure(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message).with_code("source_error")
}

fn parse_error(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message).with_code("parse_error")
}

pub(crate) fn unavailable() -> ApiError {
    ApiError::new(
        503,
        "Nguồn trực tuyến không khả dụng trong chế độ dữ liệu thử nghiệm.",
    )
    .with_code("network_error")
}

pub(crate) fn catalog() -> Value {
    Value::Array(
        SOURCES
            .iter()
            .map(|(id, name, _, _)| json!({"id":id,"name":name}))
            .collect(),
    )
}

fn source(
    id: &str,
) -> Result<
    &'static (
        &'static str,
        &'static str,
        &'static str,
        &'static [&'static str],
    ),
    ApiError,
> {
    SOURCES
        .iter()
        .find(|entry| entry.0 == id)
        .ok_or_else(|| failure(format!("Nguồn chưa được hỗ trợ: {id}")))
}

pub(crate) fn canonical(id: &str, link: &str, base: Option<&str>) -> Result<String, ApiError> {
    let (_, _, origin, hosts) = *source(id)?;
    let default = format!("{origin}/");
    let base = base.unwrap_or(&default);
    let normalized = if link.is_empty() {
        base.to_owned()
    } else {
        relative_remote(link, base)
    };
    let parsed = url::Url::parse(&normalized).map_err(|_| {
        failure("Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng")
    })?;
    let host = parsed.host_str().unwrap_or_default();
    if !hosts.contains(&host.strip_prefix("www.").unwrap_or(host)) {
        return Err(failure("URL không thuộc nguồn đã chọn"));
    }
    let (_, tail) = normalized.split_once("://").expect("validated URL");
    let tail = &tail[tail.find('/').unwrap_or(tail.len())..];
    let (path, query) = tail.split_once('?').unwrap_or((tail, ""));
    if id == "stv" {
        static STORY: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(r"\A/truyen/[a-zA-Z0-9_-]+/1/[0-9]+(?:/[0-9]+)?/?\z").unwrap()
        });
        if !query.is_empty() || !STORY.is_match(path) {
            return Err(failure(
                "URL Sáng Tác Việt phải là trang truyện hoặc chương gốc hợp lệ",
            ));
        }
        return Ok(format!("{origin}{}/", path.trim_end_matches('/')));
    }
    let path = if id == "qq" {
        path.strip_suffix(".html").unwrap_or(path)
    } else {
        path
    };
    let suffix = if query.is_empty() {
        String::new()
    } else {
        format!("?{query}")
    };
    Ok(format!("{origin}{path}{suffix}"))
}

#[derive(Default, Deserialize)]
pub(crate) struct Sources {
    format: String,
    #[serde(default)]
    pages: HashMap<String, String>,
    #[serde(default)]
    images: HashMap<String, String>,
    #[serde(skip)]
    image_writer: std::sync::Mutex<()>,
    #[serde(default, rename = "stv")]
    script: Option<Value>,
    #[serde(skip)]
    pub(crate) stv: Option<Arc<Stv>>,
    #[serde(skip)]
    native: Option<PublicFetch>,
}

impl Sources {
    pub(crate) fn open(root: &Path) -> Result<Self, ApiError> {
        let path = root.join("m5-sources.json");
        match std::fs::symlink_metadata(&path) {
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                return Ok(Self::default());
            }
            Err(error) => return Err(error.into()),
            Ok(meta) if !meta.is_file() => return Err(failure("Invalid source fixture")),
            Ok(_) => {}
        }
        let file = File::open(checked_path(&path)?)?;
        const MAX_FIXTURE: u64 = 32 * 1024 * 1024;
        let mut bytes = Vec::new();
        file.take(MAX_FIXTURE + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > MAX_FIXTURE {
            return Err(failure("Source fixture exceeds 32 MiB"));
        }
        let mut fixtures: Self =
            serde_json::from_slice(&bytes).map_err(|_| failure("Invalid source fixture"))?;
        if fixtures.format != "reader-local-m5-sources-1" {
            return Err(failure("Invalid source fixture format"));
        }
        if let Some(script) = fixtures.script.take() {
            fixtures.stv = Some(Arc::new(Stv::fixture(stv::fixture_fetcher(Some(&script))?)));
        }
        Ok(fixtures)
    }

    pub(crate) fn enable_native(&mut self) -> Result<(), ApiError> {
        if self.enabled() {
            return Err(failure(
                "Cannot combine scripted and native source transport",
            ));
        }
        let native = PublicFetch::new()?;
        let transport = native.transport.clone();
        self.stv = Some(Arc::new(Stv::new(Arc::new(move |request, stop| {
            transport.fetch_blocking(request, stop)
        }))));
        self.native = Some(native);
        Ok(())
    }

    pub(crate) fn close(&self) {
        if let Some(native) = &self.native {
            native.close();
        }
        if let Some(stv) = &self.stv {
            stv.close();
        }
    }

    pub(crate) fn enabled(&self) -> bool {
        !self.format.is_empty() || self.native.is_some()
    }

    pub(crate) fn open_original(&self, id: &str, link: &str) -> Result<Value, ApiError> {
        let url = canonical(id, link, None)?;
        let opened = self
            .native
            .as_ref()
            .is_some_and(|native| native.open_original(&url));
        Ok(json!({"opened":opened,"url":url}))
    }

    fn load(&self, url: &str) -> Result<Cow<'_, str>, ApiError> {
        let text = if let Some(page) = self.pages.get(url) {
            Cow::Borrowed(page.as_str())
        } else {
            Cow::Owned(
                self.native
                    .as_ref()
                    .ok_or_else(unavailable)?
                    .page(url, MAX_PAGE)?,
            )
        };
        static CHALLENGE: std::sync::LazyLock<regex::Regex> = std::sync::LazyLock::new(|| {
            regex::Regex::new(
                r"(?i)document\.cookie\s*=|<title>\s*(?:Just a moment|Attention Required)",
            )
            .unwrap()
        });
        if text.chars().take(MAX_PAGE + 1).count() > MAX_PAGE {
            return Err(failure("Nguồn trả dữ liệu không hợp lệ hoặc quá lớn"));
        }
        if text.trim_matches(python_whitespace).is_empty()
            || (text.chars().take(5000).count() < 5000 && CHALLENGE.is_match(&text))
        {
            return Err(failure(
                "Nguồn trả trang trống hoặc yêu cầu kiểm tra trình duyệt",
            ));
        }
        Ok(text)
    }

    pub(crate) fn image(&self, url: &str, referer: &str) -> Result<Vec<u8>, ApiError> {
        let url = relative_remote(url, "");
        if url.is_empty() {
            return Err(failure(
                "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
            ));
        }
        let Some(data) = self.images.get(&url) else {
            return self
                .native
                .as_ref()
                .ok_or_else(unavailable)?
                .image(&url, referer);
        };
        if data.len() > 20 * 1024 * 1024 * 4 / 3 + 4 {
            return Err(failure("Nguồn trả dữ liệu không hợp lệ hoặc quá lớn"));
        }
        STANDARD
            .decode(data)
            .map_err(|_| failure("Ảnh nguồn không hợp lệ."))
    }

    pub(crate) fn search(&self, id: &str, query: &str, page: i64) -> Result<Value, ApiError> {
        let (_, _, origin, _) = *source(id)?;
        let query_length = query.chars().take(501).count();
        let query = query.trim_matches(python_whitespace);
        if !(1..=10_000).contains(&page) || query.is_empty() || query_length > 500 {
            return Err(failure("Từ khóa hoặc số trang không hợp lệ"));
        }
        let (url, rows, title) = match id {
            "qq" => (
                format!(
                    "{origin}/tim-kiem/trang-{page}?q={}",
                    quote(query, false).replace("%2F", "/")
                ),
                "#main_homepage .list_grid li",
                ".book_name a",
            ),
            "full" => (
                format!(
                    "{origin}/tim-kiem/?tukhoa={}&page={page}",
                    quote(query, true)
                ),
                ".list-truyen div[itemscope]",
                ".truyen-title a",
            ),
            "hako" => {
                let suffix = if page > 1 {
                    format!("&page={page}")
                } else {
                    String::new()
                };
                (
                    format!("{origin}/tim-kiem?keywords={}{suffix}", quote(query, true)),
                    ".thumb-item-flow",
                    ".series-title a",
                )
            }
            _ => {
                return self
                    .stv
                    .as_ref()
                    .ok_or_else(unavailable)?
                    .search(query, page);
            }
        };
        let doc = source_document(&self.load(&url)?)?;
        let root = doc.root_element();
        let mut items = Vec::new();
        for row in root.select(&selector(rows)) {
            let Some(link) = find(row, title) else {
                continue;
            };
            let name = text(link);
            let href = attr(link, "href");
            if name.is_empty() || href.is_empty() {
                continue;
            }
            items.push(json!({"name":name,"author":value(row,".author",None),"path":canonical(id,href,Some(&url))?,"cover":cover(row,&url),"description":value(row,".last_chapter a, .chapter-title, .author",None)}));
        }
        Ok(json!({"items":items,"next":next_page(root,page)}))
    }

    pub(crate) fn detail(&self, id: &str, link: &str) -> Result<Value, ApiError> {
        let url = canonical(id, link, None)?;
        let (title, author, image, description) = match id {
            "qq" => (
                "h1[itemprop=name]",
                ".book_info .author a",
                ".book_avatar",
                ".story-detail-info",
            ),
            "full" => ("h3.title", "a[itemprop=author]", "div.book", ".desc-text"),
            "hako" => (".series-name", "", ".series-cover", ".summary-content"),
            _ => return self.stv.as_ref().ok_or_else(unavailable)?.detail(&url),
        };
        let doc = source_document(&self.load(&url)?)?;
        let root = doc.root_element();
        let name = value(root, title, None);
        if name.is_empty() {
            return Err(failure(
                "Không tìm thấy thông tin truyện; nguồn có thể đổi giao diện hoặc yêu cầu kiểm tra trình duyệt",
            ));
        }
        let mut author = if id == "hako" {
            hako_author(root)
        } else {
            value(root, author, None)
        };
        let mut chapters = Vec::new();
        let mut seen = HashSet::new();
        let mut append = |links: Vec<ElementRef<'_>>, volume: &str| -> Result<usize, ApiError> {
            let initial = chapters.len();
            for link in links {
                let href = attr(link, "href");
                let title = text(link);
                if href.is_empty() || title.is_empty() {
                    continue;
                }
                let path = canonical(id, href, Some(&url))?;
                if seen.insert(path.clone()) {
                    let mut row = json!({"index":chapters.len(),"title":title,"url":path});
                    if !volume.is_empty() {
                        row["volume"] = json!(volume);
                    }
                    chapters.push(row);
                }
            }
            Ok(chapters.len() - initial)
        };
        match id {
            "qq" => {
                let mut links: Vec<_> = root.select(&selector(".works-chapter-list a")).collect();
                links.reverse();
                append(links, "")?;
            }
            "full" => {
                let pages = value(root, "#total-page", Some("value"));
                let pages = if pages.is_empty() {
                    1
                } else {
                    crate::library::integer(&pages)
                        .ok()
                        .filter(|x| (1..=10_000).contains(x))
                        .ok_or_else(|| failure("Nguồn trả số trang mục lục không hợp lệ"))?
                };
                let story = value(root, "#truyen-id", Some("value"));
                let slug = value(root, "#truyen-ascii", Some("value"));
                if !story.is_empty() && !slug.is_empty() {
                    for page in 1..=pages {
                        let endpoint = format!(
                            "https://truyenfull.live/ajax.php?type=list_chapter&tid={}&tascii={}&page={page}&totalp={pages}",
                            quote(&story, true),
                            quote(&slug, true)
                        );
                        let error = |reason: &str| {
                            failure(format!("Không đọc được trang mục lục {page}: {reason}"))
                        };
                        let data: Value = crate::library::parse_json(&self.load(&endpoint)?)
                            .map_err(|e| error(&e.to_string()))?;
                        let html = data
                            .get("chap_list")
                            .ok_or_else(|| error("'chap_list'"))?
                            .as_str()
                            .ok_or_else(|| error("chap_list không phải văn bản"))?;
                        validate_html_limits(html).map_err(|err| error(&err.message))?;
                        let part = Html::parse_fragment(html);
                        let links: Vec<_> = part.select(&selector(".list-chapter li a")).collect();
                        if links.is_empty() {
                            return Err(failure(format!(
                                "Trang mục lục {page} trống; chưa tải đủ chương"
                            )));
                        }
                        if append(links, "")? == 0 {
                            return Err(failure(format!(
                                "Trang mục lục {page} lặp lại; chưa tải đủ chương"
                            )));
                        }
                    }
                } else if pages > 1 {
                    return Err(failure(
                        "Thiếu thông tin phân trang mục lục; chưa tải đủ chương",
                    ));
                } else {
                    append(root.select(&selector(".list-chapter li a")).collect(), "")?;
                }
            }
            _ => {
                let volumes = selector("section.volume-list");
                let links = selector(".list-chapters a");
                let mut any = false;
                for volume in root.select(&volumes) {
                    any = true;
                    let title = value(volume, ".sect-title", None);
                    let local: Vec<_> = volume.select(&links).collect();
                    let mut added = false;
                    if let Some(link) = find(volume, ".volume-cover a")
                        .filter(|_| local.is_empty() || author.is_empty())
                    {
                        let volume_url = canonical(id, attr(link, "href"), Some(&url))?;
                        let page = source_document(&self.load(&volume_url)?)?;
                        if author.is_empty() {
                            author = hako_author(page.root_element());
                        }
                        if local.is_empty() {
                            let remote: Vec<_> = page.select(&links).collect();
                            if remote.is_empty() {
                                return Err(failure(
                                    "Tập truyện thiếu mục lục; chưa tải đủ chương",
                                ));
                            }
                            append(remote, &title)?;
                            added = true;
                        }
                    }
                    if !added {
                        if local.is_empty() {
                            return Err(failure("Tập truyện thiếu mục lục; chưa tải đủ chương"));
                        }
                        append(local, &title)?;
                    }
                }
                if !any {
                    append(
                        root.select(&links).collect(),
                        &value(root, ".volume-name", None),
                    )?;
                }
            }
        }
        if chapters.is_empty() {
            return Err(failure("Không tìm thấy mục lục truyện"));
        }
        Ok(
            json!({"name":name,"author":author,"path":url,"cover":cover(find(root,image).unwrap_or(root),&url),"description":value(root,description,None),"source":id,"kind":if id == "qq" {"comic"} else {"text"},"format":"online","chapters":chapters}),
        )
    }

    pub(crate) fn chapter(
        &self,
        id: &str,
        link: &str,
        background: bool,
        cancelled: impl Fn() -> bool,
    ) -> Result<Value, ApiError> {
        let url = canonical(id, link, None)?;
        if id == "stv" {
            return self
                .stv
                .as_ref()
                .ok_or_else(unavailable)?
                .chapter(&url, background, cancelled);
        }
        let raw = self.load(&url)?;
        let doc = source_document(&raw)?;
        let root = doc.root_element();
        if id == "qq" {
            let mut images = Vec::new();
            for node in root.select(&selector(".chapter_content .page-chapter img")) {
                let image = relative_remote(first_attr(node, &["data-original", "src"]), &url);
                if image.is_empty() {
                    return Err(failure("Ảnh chương có URL không hợp lệ"));
                }
                images.push(image);
            }
            if images.is_empty() {
                return Err(failure(
                    "Không tìm thấy ảnh chương; nguồn có thể yêu cầu kiểm tra trình duyệt",
                ));
            }
            let html = images
                .iter()
                .enumerate()
                .map(|(i, image)| {
                    format!(
                        "<img src=\"{}\" alt=\"Trang {}\" loading=\"lazy\">",
                        escape(image),
                        i + 1
                    )
                })
                .collect::<String>();
            return Ok(json!({"html":html,"text":"","images":images}));
        }
        let content = find(root,if id == "full" {".chapter-c"} else {"#chapter-content"}).ok_or_else(|| parse_error("Không tìm thấy nội dung chương; nguồn có thể yêu cầu đăng nhập hoặc kiểm tra trình duyệt"))?;
        let markup = if id == "hako" {
            decode_hako(content)?
        } else {
            content.inner_html()
        };
        static GUEST: std::sync::LazyLock<regex::Regex> =
            std::sync::LazyLock::new(|| regex::Regex::new(r"\bisLoggedIn\s*=\s*false\b").unwrap());
        let html = clean_html(&markup, Some(&url), id != "hako" || !GUEST.is_match(&raw))?;
        let text = html_to_text(&html)?;
        let clean = Html::parse_fragment(&html);
        let images: Vec<_> = clean
            .select(&selector("img"))
            .map(|n| attr(n, "src"))
            .collect();
        if text.is_empty() && images.is_empty() {
            return Err(parse_error("Nội dung chương trống; chưa tải được nội dung"));
        }
        Ok(json!({"html":html,"text":text,"images":images}))
    }

    pub(crate) fn cover(&self, id: &str, link: &str) -> Result<Value, ApiError> {
        let url = if source(id).is_ok() {
            canonical(id, link, None)?
        } else {
            relative_remote(link, "")
        };
        if url.is_empty() {
            return Err(failure(
                "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
            ));
        }
        if id == "stv" {
            return self.stv.as_ref().ok_or_else(unavailable)?.cover(&url);
        }
        let page = source_document(&self.load(&url)?)?;
        let root = page.root_element();
        let container = match id {
            "qq" => ".book_avatar",
            "full" => "div.book",
            "hako" => ".series-cover",
            _ => "",
        };
        let mut picture = if container.is_empty() {
            String::new()
        } else {
            find(root, container).map_or_else(String::new, |node| cover(node, &url))
        };
        if picture.is_empty() {
            picture = relative_remote(
                &value(
                    root,
                    "meta[property=\"og:image\"], meta[name=\"og:image\"]",
                    Some("content"),
                ),
                &url,
            );
        }
        if picture.is_empty() {
            return Err(failure(
                "Trang truyện không cung cấp URL bìa công khai hợp lệ.",
            ));
        }
        Ok(json!({"cover":picture,"path":url}))
    }
}

fn source_document(markup: &str) -> Result<Html, ApiError> {
    validate_html_limits(markup)?;
    Ok(Html::parse_document(markup))
}

fn selector(css: &str) -> Selector {
    Selector::parse(css).expect("static source selector")
}
fn attr<'a>(node: ElementRef<'a>, key: &str) -> &'a str {
    node.attr(key).unwrap_or_default()
}
fn first_attr<'a>(node: ElementRef<'a>, keys: &[&str]) -> &'a str {
    keys.iter()
        .map(|key| attr(node, key))
        .find(|value| !value.is_empty())
        .unwrap_or_default()
}
fn find<'a>(node: ElementRef<'a>, css: &str) -> Option<ElementRef<'a>> {
    node.select(&selector(css)).next()
}
fn text(node: ElementRef<'_>) -> String {
    html_to_text(&node.inner_html())
        .unwrap_or_default()
        .split(python_whitespace)
        .filter(|p| !p.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}
fn value(node: ElementRef<'_>, css: &str, key: Option<&str>) -> String {
    find(node, css).map_or_else(String::new, |node| {
        key.map_or_else(|| text(node), |key| attr(node, key).to_owned())
    })
}

fn hako_author(root: ElementRef<'_>) -> String {
    root.select(&selector(".info-item"))
        .find(|row| casefold(&value(*row, ".info-name", None)).contains("tác giả"))
        .map_or_else(String::new, |row| value(row, ".info-value", None))
}

fn cover(root: ElementRef<'_>, url: &str) -> String {
    if let Some(image) = find(root, "img") {
        return relative_remote(
            first_attr(image, &["data-original", "data-src", "src"]),
            url,
        );
    }
    if let Some(node) = find(root, "[data-bg], [data-image], [style]") {
        let source = first_attr(node, &["data-bg", "data-image"]);
        if !source.is_empty() {
            return relative_remote(source, url);
        }
        static STYLE: std::sync::LazyLock<regex::Regex> =
            std::sync::LazyLock::new(|| regex::Regex::new(r#"url\(["']?(.*?)["']?\)"#).unwrap());
        if let Some(caps) = STYLE.captures(attr(node, "style")) {
            return relative_remote(caps.get(1).unwrap().as_str(), url);
        }
    }
    String::new()
}

fn next_page(root: ElementRef<'_>, page: i64) -> Option<i64> {
    let has = |node: ElementRef<'_>, class: &str| {
        attr(node, "class")
            .split(python_whitespace)
            .any(|w| w == class)
    };
    for container in root.select(&selector(".pagination, .page_redirect")) {
        for link in container.select(&selector("a")) {
            if attr(link, "rel")
                .split(python_whitespace)
                .any(|r| r == "next")
                && !has(link, "disabled")
                && !link
                    .parent()
                    .and_then(ElementRef::wrap)
                    .is_some_and(|p| has(p, "disabled"))
            {
                return Some(page + 1);
            }
        }
        for node in container.select(&selector(".active")) {
            let active = node
                .parent()
                .and_then(ElementRef::wrap)
                .filter(|n| n.value().name() == "a")
                .unwrap_or(node);
            if let Some(next) = active.next_siblings().find_map(ElementRef::wrap) {
                let link = if next.value().name() == "a" {
                    Some(next)
                } else {
                    find(next, "a")
                };
                if !has(next, "disabled")
                    && link.is_some_and(|a| {
                        let link = attr(a, "href");
                        !link.is_empty() && !link.starts_with("javascript:")
                    })
                {
                    return Some(page + 1);
                }
            }
        }
    }
    None
}

fn decode_hako(root: ElementRef<'_>) -> Result<String, ApiError> {
    let Some(node) = find(root, "#chapter-c-protected") else {
        return Ok(root.inner_html());
    };
    let scheme = node.attr("data-s").unwrap_or("none");
    if !["none", "xor_shuffle", "base64_reverse"].contains(&scheme) {
        return Err(parse_error("Hako: unsupported chapter encoding"));
    }
    let fail = |reason: &str| parse_error(format!("Hako: không đọc được chương mã hóa: {reason}"));
    let mut chunks: Vec<String> =
        serde_json::from_str(attr(node, "data-c")).map_err(|_| fail("invalid encoded chunks"))?;
    let key = attr(node, "data-k").as_bytes();
    if chunks.is_empty()
        || chunks.len() > 20_000
        || !key.is_ascii()
        || (scheme == "xor_shuffle" && key.is_empty())
    {
        return Err(fail("invalid encoded chunks"));
    }
    if chunks.iter().any(|s| {
        !s.as_bytes()
            .get(..4)
            .is_some_and(|prefix| prefix.iter().all(u8::is_ascii_digit))
    }) {
        return Err(fail("invalid chunk sequence"));
    }
    chunks.sort_unstable_by(|a, b| a[..4].cmp(&b[..4]));
    let mut decoded = Vec::new();
    for (index, chunk) in chunks.iter().enumerate() {
        if chunk[..4].parse::<usize>() != Ok(index) {
            return Err(fail("missing or duplicate encoded chunk"));
        }
        let reversed;
        let body = if scheme == "base64_reverse" {
            reversed = chunk.as_bytes()[4..]
                .iter()
                .rev()
                .copied()
                .collect::<Vec<_>>();
            &reversed
        } else {
            &chunk.as_bytes()[4..]
        };
        let mut bytes = STANDARD
            .decode(body)
            .map_err(|_| fail("Only base64 data is allowed"))?;
        if scheme == "xor_shuffle" {
            for (position, byte) in bytes.iter_mut().enumerate() {
                *byte ^= key[position % key.len()];
            }
        }
        decoded.extend(bytes);
    }
    String::from_utf8(decoded).map_err(|error| fail(&error.to_string()))
}

fn stored_json(value: &Value) -> String {
    String::from_utf8(crate::http::json_bytes(value)).expect("JSON is UTF-8")
}

pub(crate) fn key(url: &str) -> &str {
    let url = url.split(['#', '?']).next().unwrap_or_default();
    let url = url
        .split_once("://")
        .map_or(url, |(_, tail)| tail.find('/').map_or("", |at| &tail[at..]));
    let path = url.trim_end_matches('/');
    path.strip_suffix(".html").unwrap_or(path)
}

impl crate::Library {
    pub(crate) fn fetch_source_chapter(
        &self,
        book: &str,
        index: i64,
        source: &str,
        url: &str,
        job: Option<&str>,
        cancelled: impl Fn() -> bool,
    ) -> Result<Value, ApiError> {
        let result = self.sources.chapter(source, url, job.is_some(), cancelled);
        if let Err(error) = &result
            && error.code() == Some("vip_locked")
        {
            let value = error.json();
            let access = &value["access"];
            if access["state"] == "vip_locked"
                && access["vip"] == true
                && access["evidence"]
                    .as_str()
                    .is_some_and(|text| !text.trim().is_empty())
            {
                let mut db = self.write_connection()?;
                let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
                if let Some(job) = job {
                    check_job(&tx, book, job)?;
                }
                tx.execute("UPDATE chapters SET raw=json_set(raw,'$.is_vip',json('true'),'$.access',json(?)) WHERE book_id=? AND idx=?",params![stored_json(access),book,index])?;
                tx.commit()?;
            }
        }
        result
    }

    pub(crate) fn download_source_chapter(
        &self,
        task: &crate::downloads::ChapterTask,
        control: &crate::downloads::DownloadControl,
    ) -> Result<(), crate::downloads::DownloadFailure> {
        use crate::downloads::DownloadFailure;
        let result = (|| {
            let book = &task.book_id;
            let index = task.index;
            let _lease = self.chapter_leases.acquire((book.clone(), index));
            let row = crate::reader::chapter_value(self.raw_chapter(book, index)?)?;
            let info = self.get_book(book)?;
            let content = if row["downloaded"] == true {
                None
            } else {
                Some(self.fetch_source_chapter(
                    book,
                    index,
                    &task.source,
                    row["url"].as_str().unwrap_or_default(),
                    Some(&task.job_id),
                    || control.cancelled(),
                )?)
            };
            if control.stopping() {
                return Err(
                    ApiError::new(400, "Yêu cầu tải đã dừng.").with_code("request_cancelled")
                );
            }
            self.save_source_chapter(
                book,
                index,
                row["revision"]
                    .as_i64()
                    .ok_or(rusqlite::Error::InvalidQuery)?,
                content.as_ref(),
                Some(&task.job_id),
            )?;
            let stored = crate::reader::chapter_value(self.raw_chapter(book, index)?)?;
            let referer = info["path"].as_str().unwrap_or_default();
            let mut assets = HashSet::new();
            for image in stored["images"]
                .as_array()
                .ok_or(rusqlite::Error::InvalidQuery)?
            {
                assets.extend(crate::http::asset_image_urls(
                    image.as_str().unwrap_or_default(),
                    referer,
                )?);
            }
            for field in ["html", "original_html"] {
                if let Some(markup) = stored[field].as_str() {
                    assets.extend(crate::http::asset_markup_urls(markup, referer)?);
                }
            }
            for image in assets {
                if control.stopping() {
                    return Err(
                        ApiError::new(400, "Yêu cầu tải đã dừng.").with_code("request_cancelled")
                    );
                }
                self.save_source_image(&image, referer, Some((book, index, &task.job_id)))?;
            }
            Ok::<_, ApiError>(())
        })();
        result.map_err(|error| {
            let value = error.json();
            if error.code() == Some("captcha_required") {
                let captcha = value["captcha"].clone();
                if let Some((key, outcome)) = error.verification().cloned() {
                    return DownloadFailure::Verification {
                        key,
                        outcome,
                        captcha: Box::new(captcha),
                        message: error.message,
                    };
                }
            } else if let Some(code) = error.code()
                && value["access"].is_object()
            {
                return DownloadFailure::Access {
                    message: error.message,
                    code,
                    access: value["access"].clone(),
                };
            }
            DownloadFailure::Error(error)
        })
    }

    pub(crate) fn add_source_book(&self, mut detail: Value) -> Result<Value, ApiError> {
        let chapters = detail
            .as_object_mut()
            .and_then(|book| book.shift_remove("chapters"))
            .and_then(|rows| match rows {
                Value::Array(rows) => Some(rows),
                _ => None,
            })
            .ok_or_else(|| parse_error("Không tìm thấy mục lục truyện"))?;
        let field = |name: &str| detail[name].as_str().unwrap_or_default();
        let path = field("path");
        let source = field("source");
        let default_id = format!(
            "{:x}",
            Sha256::digest(format!("{source}:{path}").as_bytes())
        );
        let mut read = self.reader_connection()?;
        let view = read.transaction()?;
        let current: Option<String> = view
            .query_row(
                "SELECT id FROM books WHERE source=? AND path=? ORDER BY rowid LIMIT 1",
                params![source, path],
                |r| r.get(0),
            )
            .optional()?;
        let id = if let Some(id) = current {
            id
        } else {
            let mut stmt =
                view.prepare("SELECT id,path FROM books WHERE source=? ORDER BY rowid")?;
            let mut rows = stmt.query([source])?;
            let mut found = None;
            while let Some(row) = rows.next()? {
                let existing: String = row.get(1)?;
                if key(&existing) == key(path) {
                    found = Some(row.get(0)?);
                    break;
                }
            }
            found.unwrap_or_else(|| default_id[..32].to_owned())
        };
        let timestamp = |db: &rusqlite::Connection| {
            db.query_row("SELECT updated_at FROM books WHERE id=?", [&id], |row| {
                row.get::<_, f64>(0)
            })
            .optional()
        };
        let previous = timestamp(&view)?;
        let mut metadata = Vec::new();
        {
            let mut stmt = view
                .prepare("SELECT idx,url,title,raw FROM chapters WHERE book_id=? ORDER BY idx")?;
            let mut rows = stmt.query([&id])?;
            while let Some(row) = rows.next()? {
                let idx = usize::try_from(row.get::<_, i64>(0)?)
                    .map_err(|_| rusqlite::Error::InvalidQuery)?;
                let url: String = row.get(1)?;
                if idx != metadata.len()
                    || chapters
                        .get(metadata.len())
                        .and_then(|chapter| chapter["url"].as_str())
                        .is_none_or(|url_new| key(url_new) != key(&url))
                {
                    return Err(ApiError::new(
                        400,
                        "Mục lục nguồn đã đổi thứ tự hoặc thiếu chương. Thư viện, ghi chú và tiến độ cũ được giữ nguyên; cần đối chiếu chương trước khi cập nhật.",
                    ));
                }
                metadata.push((url, row.get::<_, String>(2)?, row.get::<_, String>(3)?));
            }
        }
        view.commit()?;
        let mut changed = Vec::new();
        for (idx, chapter) in chapters.iter().enumerate() {
            let existing = metadata.get(idx);
            let mut prior = existing
                .map(|(_, _, raw)| crate::library::parse_json(raw))
                .transpose()?
                .unwrap_or_else(|| json!({}));
            let row = prior.as_object_mut().ok_or(rusqlite::Error::InvalidQuery)?;
            if let Some(chapter) = chapter.as_object() {
                for (name, value) in chapter {
                    if !["html", "text", "images", "original_html", "original_text"]
                        .contains(&name.as_str())
                        && (!value.is_null()
                            || !["language", "translation", "is_vip", "access"]
                                .contains(&name.as_str()))
                    {
                        row.insert(name.clone(), value.clone());
                    }
                }
                if chapter.get("is_vip") == Some(&Value::Bool(false)) {
                    row.shift_remove("access");
                }
            }
            let title = chapter["title"].as_str().unwrap_or_default();
            let url = chapter["url"].as_str().unwrap_or_default();
            let raw = stored_json(&prior);
            if existing.is_none_or(|(old_url, old_title, old_raw)| {
                url != old_url || title != old_title || &raw != old_raw
            }) {
                changed.push((idx, title, url, raw));
            }
        }
        let book_raw = stored_json(&detail);
        let concurrent = || {
            ApiError::new(
                400,
                "Truyện hoặc mục lục đã thay đổi. Hãy thử thêm truyện lại.",
            )
        };
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if timestamp(&tx)? != previous {
            return Err(concurrent());
        }
        let now = crate::reader::now();
        tx.execute("INSERT INTO books(id,name,author,source,path,cover,kind,format,description,progress,chapter_index,position,groups_json,added_at,updated_at,read_at,chapter_count,raw) VALUES(?,?,?,?,?,?,?,?,?,0,0,0,'[]',?,?,0,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,author=excluded.author,source=excluded.source,path=excluded.path,cover=excluded.cover,kind=excluded.kind,format=excluded.format,description=excluded.description,chapter_count=excluded.chapter_count,updated_at=excluded.updated_at,raw=excluded.raw", params![id,field("name"),field("author"),source,path,field("cover"),field("kind"),field("format"),field("description"),now,now,chapters.len() as i64,book_raw])?;
        {
            let mut update=tx.prepare("UPDATE chapters SET title=?,url=?,raw=? WHERE book_id=? AND idx=? AND url=? AND title=? AND raw=?")?;
            let mut insert=tx.prepare("INSERT OR IGNORE INTO chapters(book_id,idx,title,url,html,text,images,downloaded,raw) VALUES(?,?,?,?,'','','[]',0,?)")?;
            for (idx, title, url, raw) in changed {
                let affected = if let Some((old_url, old_title, old_raw)) = metadata.get(idx) {
                    update.execute(params![
                        title, url, raw, id, idx as i64, old_url, old_title, old_raw
                    ])?
                } else {
                    insert.execute(params![id, idx as i64, title, url, raw])?
                };
                if affected != 1 {
                    return Err(concurrent());
                }
            }
        }
        tx.commit()?;
        self.index_book_assets(&id)?;
        let book = self.get_book(&id)?;
        if book["trashed_at"].as_f64().unwrap_or_default() != 0.0 {
            self.restore_book(&id)
        } else {
            Ok(book)
        }
    }

    pub(crate) fn raw_chapter(&self, book: &str, index: i64) -> Result<Value, ApiError> {
        crate::reader::rows(
            &self.reader_connection()?,
            "SELECT * FROM chapters WHERE book_id=? AND idx=?",
            params![book, index],
        )?
        .pop()
        .ok_or_else(|| ApiError::new(404, "Không tìm thấy chương."))
    }

    pub(crate) fn save_source_chapter(
        &self,
        book: &str,
        index: i64,
        revision: i64,
        content: Option<&Value>,
        job: Option<&str>,
    ) -> Result<(), ApiError> {
        let mut db = self.write_connection()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        if let Some(job) = job {
            check_job(&tx, book, job)?;
        }
        let (raw, current, downloaded): (String, i64, bool) = tx
            .query_row(
                "SELECT raw,revision,downloaded FROM chapters WHERE book_id=? AND idx=?",
                params![book, index],
                |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
            )
            .optional()?
            .ok_or_else(|| ApiError::new(404, "Không tìm thấy chương."))?;
        if current != revision && !downloaded {
            return Err(ApiError::new(
                400,
                "Chương đã thay đổi trong lúc tải; hãy mở lại chương.",
            ));
        }
        crate::reader::track(&tx, book, index)?;
        if let Some(content) = content.filter(|_| current == revision && !downloaded) {
            let mut meta = crate::library::parse_json(&raw)?;
            let fields = meta.as_object_mut().ok_or(rusqlite::Error::InvalidQuery)?;
            fields.shift_remove("access");
            if content["translation"] == "source" && content["language"] == "vi" {
                fields.insert("translation".into(), json!("source"));
                fields.insert("language".into(), json!("vi"));
            }
            tx.execute("UPDATE chapters SET html=?,text=?,images=?,downloaded=1,raw=? WHERE book_id=? AND idx=?",params![content["html"].as_str().unwrap_or_default(),content["text"].as_str().unwrap_or_default(),stored_json(&content["images"]),stored_json(&meta),book,index])?;
        }
        if job.is_some() {
            tx.execute(
                "UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id=? AND idx=?",
                params![book, index],
            )?;
        }
        tx.commit()?;
        Ok(())
    }

    pub(crate) fn save_source_image(
        &self,
        url: &str,
        referer: &str,
        owner: Option<(&str, i64, &str)>,
    ) -> Result<Option<Vec<u8>>, ApiError> {
        use std::io::Write;
        let url = relative_remote(url, "");
        if url.is_empty() {
            return Err(failure(
                "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
            ));
        }
        let name = format!("{:x}", Sha256::digest(url.as_bytes()));
        let folder = self.root().join("images");
        let path = folder.join(&name);
        let _lease = self.asset_leases.acquire(path.clone());
        let existing = match std::fs::symlink_metadata(&path) {
            Ok(info) if info.is_file() => {
                checked_path(&path)?;
                Some(info.len())
            }
            Ok(_) => return Err(failure("Đường dẫn cache ảnh không hợp lệ.")),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => return Err(error.into()),
        };
        if let Some((book, index, job)) = owner {
            let mut db = self.write_connection()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            check_job(&tx, book, job)?;
            tx.execute(
                "INSERT OR IGNORE INTO image_cache(name,last_used) VALUES(?,?)",
                params![name, crate::reader::now()],
            )?;
            tx.execute(
                "INSERT OR IGNORE INTO image_pins(book_id,idx,name) VALUES(?,?,?)",
                params![book, index, name],
            )?;
            tx.commit()?;
        }
        if let Some(size) = existing {
            self.touch_image(&name, size)?;
            return Ok(None);
        }
        if !referer.is_empty() && relative_remote(referer, "").is_empty() {
            return Err(failure(
                "Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng",
            ));
        }
        let body = self.sources.image(&url, referer)?;
        if crate::http::image_kind(&body) == "application/octet-stream" {
            return Err(ApiError::new(
                400,
                "Nguồn trả về dữ liệu không phải ảnh được hỗ trợ.",
            ));
        }
        let _admission = self
            .sources
            .image_writer
            .lock()
            .map_err(|_| rusqlite::Error::InvalidQuery)?;
        if path.exists() {
            return Ok(Some(body));
        }
        let mut nonce = [0; 16];
        getrandom::fill(&mut nonce)
            .map_err(|_| ApiError::new(500, "Không tạo được tệp cache ảnh."))?;
        let temporary = format!("{name}.{:032x}.tmp", u128::from_le_bytes(nonce));
        let size = body.len() as i64;
        {
            let mut db = self.write_connection()?;
            let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
            if let Some((book, _, job)) = owner {
                check_job(&tx, book, job)?;
            }
            tx.execute("INSERT OR IGNORE INTO image_cache(name,last_used) SELECT ?,? WHERE EXISTS(SELECT 1 FROM asset_refs r JOIN books b ON b.id=r.book_id WHERE r.kind='images' AND r.name=? AND b.source IN ('file','local'))",params![name,crate::reader::now(),name])?;
            tx.execute("INSERT OR IGNORE INTO image_pins(book_id,idx,name) SELECT r.book_id,r.idx,r.name FROM asset_refs r JOIN books b ON b.id=r.book_id WHERE r.kind='images' AND r.name=? AND b.source IN ('file','local')",[&name])?;
            let (budget, used): (i64, i64) = tx.query_row(
                "SELECT max_bytes,total_bytes-protected_bytes FROM image_cache_state WHERE id=1",
                [],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )?;
            let protected = tx
                .query_row(
                    "SELECT protected FROM image_cache WHERE name=?",
                    [&name],
                    |r| r.get::<_, bool>(0),
                )
                .optional()?
                .unwrap_or(false);
            if !protected && used.saturating_add(size) > budget {
                if size <= budget {
                    let target = budget - size;
                    tx.execute("UPDATE image_cache_state SET trim_to=CASE WHEN trim_to<0 THEN ? ELSE min(trim_to,?) END WHERE id=1",[target,target])?;
                }
                tx.commit()?;
                return Ok(Some(body));
            }
            tx.execute("INSERT INTO image_cache(name,size,last_used) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET size=excluded.size,last_used=excluded.last_used",params![name,size,crate::reader::now()])?;
            for file in [&name, &temporary] {
                tx.execute("INSERT INTO asset_gc_candidates(kind,name) VALUES('images',?) ON CONFLICT(kind,name) DO UPDATE SET generation=generation+1,retry_at=0,evict=0",[file])?;
            }
            tx.commit()?;
        }
        let write = (|| -> Result<(), ApiError> {
            match std::fs::create_dir(&folder) {
                Ok(()) => (),
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => (),
                Err(error) => return Err(error.into()),
            }
            let checked = checked_path(&folder)?;
            let temp = checked.join(&temporary);
            let create = (|| {
                let mut file = File::create_new(&temp)?;
                file.write_all(&body)?;
                file.sync_all()?;
                drop(file);
                std::fs::rename(&temp, &path)
            })();
            if create.is_err() {
                let _ = std::fs::remove_file(&temp);
            }
            Ok(create?)
        })();
        if let Err(error) = write
            && owner.is_some()
        {
            return Err(error);
        }
        Ok(Some(body))
    }
}

fn check_job(db: &rusqlite::Connection, book: &str, job: &str) -> Result<(), ApiError> {
    let allowed: bool=db.query_row("SELECT EXISTS(SELECT 1 FROM download_jobs j JOIN books b ON b.id=j.book_id WHERE j.id=? AND j.book_id=? AND b.trashed_at=0 AND j.status IN ('running','queued','paused'))",params![job,book],|r|r.get(0))?;
    if !allowed {
        return Err(ApiError::new(400, "Yêu cầu tải đã dừng.").with_code("request_cancelled"));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn source_rejects_oversized_markup_and_query_before_processing() {
        assert!(source_document(&"<div>".repeat(256)).is_ok());
        for markup in ["<div>".repeat(257), "<br>".repeat(100_001)] {
            let error = source_document(&markup).unwrap_err();
            assert_eq!(
                (error.status, error.message.as_str()),
                (400, "HTML exceeds parsing limits")
            );
        }
        let oversized = format!("{}x", " ".repeat(500));
        assert_eq!(
            Sources::default()
                .search("qq", &oversized, 1)
                .unwrap_err()
                .status,
            400
        );
    }
}
