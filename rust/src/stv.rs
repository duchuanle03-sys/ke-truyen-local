mod session;
mod transport;
pub(crate) use transport::{
    Cancellation, Fetcher, Transport, WireError, WireRequest, WireResponse,
};

use crate::{
    ApiError,
    casefold::python_whitespace,
    http::{html_to_text, quote, relative_remote},
};
use base64::{Engine, engine::general_purpose::STANDARD};
use hyper::{
    HeaderMap,
    header::{HeaderName, HeaderValue},
};
use regex::Regex;
use scraper::{ElementRef, Html, Selector};
use serde::Deserialize;
use serde_json::{Value, json};
use session::{Options, READ_INTERVAL, Reply, Session};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    sync::{Arc, Condvar, LazyLock, Mutex, MutexGuard},
    time::{Duration, Instant},
};
use url::Url;

const ORIGIN: &str = "https://sangtacviet.com";
const MAX_BODY: usize = 8 * 1024 * 1024;
const MAX_WAITERS: usize = 64;

impl From<WireError> for ApiError {
    fn from(error: WireError) -> Self {
        let status = match error.code {
            "network_error" | "source_timeout" => 503,
            "captcha_required" => 409,
            "vip_locked" | "auth_required" => 403,
            _ => 400,
        };
        let mut details = error.details.map_or_else(|| json!({}), |value| *value);
        if let Some(status) = error.http_status {
            details["http_status"] = json!(status);
        }
        let api = ApiError::new(status, error.message)
            .with_code(error.code)
            .with_details(details);
        if let Some(verification) = error.verification {
            api.with_verification(verification)
        } else {
            api
        }
    }
}

fn parse_error(message: &str) -> WireError {
    WireError::new("parse_error", message)
}

fn closed() -> WireError {
    WireError::new("network_error", "Nguồn Sáng Tác Việt đã đóng.")
}

fn locked<T>(mutex: &Mutex<T>) -> Result<MutexGuard<'_, T>, WireError> {
    mutex.lock().map_err(|_| closed())
}

fn trim(s: &str) -> &str {
    s.trim_matches(python_whitespace)
}

fn random_id() -> Result<String, WireError> {
    let mut bytes = [0; 16];
    getrandom::fill(&mut bytes)
        .map_err(|_| WireError::new("source_error", "Không tạo được yêu cầu CAPTCHA."))?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct FixtureStep {
    #[serde(default)]
    method: Option<String>,
    url: String,
    request_body: Option<String>,
    status: Option<u16>,
    #[serde(default)]
    headers: Vec<(String, String)>,
    text: Option<String>,
    body_base64: Option<String>,
    #[serde(default)]
    repeat: bool,
}

pub(crate) fn fixture_fetcher(value: Option<&Value>) -> Result<Arc<Fetcher>, ApiError> {
    let invalid = || ApiError::new(400, "Invalid synthetic STV transport");
    let mut steps = Vec::new();
    for step in value
        .map(|value| serde_json::from_value::<Vec<FixtureStep>>(value.clone()))
        .transpose()
        .map_err(|_| invalid())?
        .unwrap_or_default()
    {
        if step.text.is_some() && step.body_base64.is_some()
            || !["GET", "POST"].contains(&step.method.as_deref().unwrap_or("GET"))
        {
            return Err(invalid());
        }
        let (url, _) = session::checked(&step.url, None).map_err(|_| invalid())?;
        let mut headers = HeaderMap::new();
        for (name, value) in step.headers {
            headers.append(
                HeaderName::from_bytes(name.as_bytes()).map_err(|_| invalid())?,
                HeaderValue::from_str(&value).map_err(|_| invalid())?,
            );
        }
        let response = WireResponse {
            status: step.status.unwrap_or(200),
            headers,
            body: match step.body_base64 {
                Some(data) => STANDARD.decode(data).map_err(|_| invalid())?,
                None => step.text.unwrap_or_default().into_bytes(),
            },
        };
        if !(100..600).contains(&response.status) || response.body.len() > MAX_BODY {
            return Err(invalid());
        }
        steps.push((
            step.method.unwrap_or_else(|| "GET".into()),
            url,
            step.request_body,
            response,
            step.repeat,
        ));
    }
    let steps = Mutex::new(steps);
    Ok(Arc::new(move |request, cancel| {
        if cancel.is_cancelled() {
            return Err(closed());
        }
        let mut steps = locked(&steps)?;
        let index = steps
            .iter()
            .position(|(method, url, body, ..)| {
                method == request.method.as_str()
                    && (url == &request.url
                        || (url == "https://sangtacviet.com/generate_captcha.php"
                            && request
                                .url
                                .strip_prefix(
                                    "https://sangtacviet.com/generate_captcha.php?random=",
                                )
                                .is_some_and(|suffix| {
                                    suffix.strip_prefix("0.").is_some_and(|digits| {
                                        !digits.is_empty()
                                            && digits.bytes().all(|b| b.is_ascii_digit())
                                    })
                                })))
                    && body
                        .as_ref()
                        .is_none_or(|body| body.as_bytes() == request.body)
            })
            .ok_or_else(|| {
                WireError::new(
                    "network_error",
                    "Nguồn không khả dụng trong chế độ ngoại tuyến.",
                )
            })?;
        let (_, _, _, response, repeat) = &steps[index];
        if *repeat {
            Ok(WireResponse {
                status: response.status,
                headers: response.headers.clone(),
                body: response.body.clone(),
            })
        } else {
            Ok(steps.remove(index).3)
        }
    }))
}

pub(crate) struct Stv {
    fetch: Arc<Fetcher>,
    metadata: Mutex<Session>,
    reading: Mutex<Session>,
    state: Mutex<State>,
    wake: Condvar,
    stop: Cancellation,
    virtual_time: bool,
}

#[derive(Default)]
struct State {
    active: bool,
    waiters: [VecDeque<u64>; 2],
    sequence: u64,
    pending: Option<Pending>,
    tocs: HashMap<(String, String), HashSet<String>>,
}

#[derive(Clone)]
struct Pending {
    id: String,
    key: String,
    previous: Option<String>,
    image: Option<Arc<str>>,
    url: String,
    outcome: Arc<std::sync::OnceLock<bool>>,
}

pub(crate) struct CaptchaAction {
    pub(crate) body: Value,
    pub(crate) key: String,
}

struct Turn<'a>(&'a Stv);

impl Drop for Turn<'_> {
    fn drop(&mut self) {
        if let Ok(mut state) = self.0.state.lock() {
            state.active = false;
        }
        self.0.wake.notify_all();
    }
}

impl Stv {
    pub(crate) fn new(fetch: Arc<Fetcher>) -> Self {
        Self {
            fetch,
            metadata: Mutex::new(Session::new(false)),
            reading: Mutex::new(Session::new(true)),
            state: Mutex::default(),
            wake: Condvar::new(),
            stop: Cancellation::default(),
            virtual_time: false,
        }
    }

    pub(crate) fn fixture(fetch: Arc<Fetcher>) -> Self {
        Self {
            virtual_time: true,
            ..Self::new(fetch)
        }
    }

    pub(crate) fn close(&self) {
        self.stop.cancel();
        if let Ok(mut state) = self.state.lock() {
            state.pending = None;
        }
        self.wake.notify_all();
    }

    fn check_open(&self) -> Result<(), WireError> {
        if self.stop.is_cancelled() {
            Err(closed())
        } else {
            Ok(())
        }
    }

    fn turn(&self, background: bool, cancelled: impl Fn() -> bool) -> Result<Turn<'_>, WireError> {
        let mut state = locked(&self.state)?;
        self.check_open()?;
        if state.waiters.iter().map(VecDeque::len).sum::<usize>() >= MAX_WAITERS {
            return Err(WireError::new(
                "source_busy",
                "Hàng đợi Sáng Tác Việt đang đầy; hãy thử lại sau.",
            ));
        }
        let lane = usize::from(background);
        let id = state.sequence;
        state.sequence = state.sequence.wrapping_add(1);
        state.waiters[lane].push_back(id);
        loop {
            let failure = if self.stop.is_cancelled() {
                Some(closed())
            } else if cancelled() {
                Some(WireError::new(
                    "request_cancelled",
                    "Đã dừng yêu cầu đang chờ lượt nguồn.",
                ))
            } else {
                None
            };
            if let Some(failure) = failure {
                state.waiters[lane].retain(|ticket| *ticket != id);
                self.wake.notify_all();
                return Err(failure);
            }
            if !state.active
                && state.waiters[lane].front() == Some(&id)
                && (!background || state.waiters[0].is_empty())
            {
                state.waiters[lane].pop_front();
                state.active = true;
                return Ok(Turn(self));
            }
            state = self
                .wake
                .wait_timeout(state, Duration::from_millis(100))
                .map_err(|_| closed())?
                .0;
        }
    }

    fn pace(
        &self,
        last: Option<Instant>,
        cancelled: Option<&dyn Fn() -> bool>,
    ) -> Result<Instant, WireError> {
        let check = || {
            self.check_open()?;
            if cancelled.is_some_and(|check| check()) {
                return Err(WireError::new(
                    "request_cancelled",
                    "Đã dừng yêu cầu đang chờ lượt nguồn.",
                ));
            }
            Ok(())
        };
        check()?;
        let now = Instant::now();
        let next = last.map_or(now, |last| (last + READ_INTERVAL).max(now));
        if self.virtual_time {
            return Ok(next);
        }
        let mut state = locked(&self.state)?;
        loop {
            check()?;
            let now = Instant::now();
            let delay = next.saturating_duration_since(now);
            if delay.is_zero() {
                return Ok(now);
            }
            let delay = if cancelled.is_some() {
                delay.min(Duration::from_millis(100))
            } else {
                delay
            };
            state = self
                .wake
                .wait_timeout(state, delay)
                .map_err(|_| closed())?
                .0;
        }
    }

    fn request(&self, reading: bool, path: &str, options: Options<'_>) -> Result<Reply, WireError> {
        self.check_open()?;
        let target = session::checked(path, options.referer)?;
        let mut channel = locked(if reading {
            &self.reading
        } else {
            &self.metadata
        })?;
        self.check_open()?;
        if target
            .1
            .query_pairs()
            .find(|(key, _)| key == "sajax")
            .is_some_and(|(_, value)| value == "readchapter")
        {
            channel.last_read = Some(self.pace(channel.last_read, options.cancelled)?);
        }
        let reply = channel.request(target, options, self.fetch.as_ref(), &self.stop)?;
        self.check_open()?;
        Ok(reply)
    }

    fn text(&self, reading: bool, path: &str, options: Options<'_>) -> Result<String, WireError> {
        match self.request(reading, path, options)? {
            Reply::Text(text) => Ok(text),
            Reply::Image(_) => Err(parse_error("Phản hồi HTTP nguồn không hợp lệ.")),
        }
    }

    pub(crate) fn complete_verification(&self, key: &str) {
        if let Ok(mut state) = self.state.lock()
            && state
                .pending
                .as_ref()
                .is_some_and(|pending| pending.key == key && pending.outcome.get() == Some(&true))
        {
            state.pending = None;
        }
    }
}

static BOOK_PATH: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"^/truyen/([a-zA-Z0-9_-]+)/1/([0-9]+)/(?:([0-9]+)/)?$")
        .expect("fixed STV book path")
});
static PLACEHOLDER: LazyLock<Regex> = LazyLock::new(|| {
    Regex::new(r"(?i)nhấp\s+vào\s+để\s+tải\s+chương|đang\s+tải\s+(?:nội\s+dung|chương)")
        .expect("fixed placeholder pattern")
});

fn parts(url: &str, chapter: bool) -> Result<(&str, &str, Option<&str>), WireError> {
    let invalid = || {
        WireError::new(
            "source_error",
            "URL truyện hoặc chương Sáng Tác Việt không hợp lệ.",
        )
    };
    let fields = url
        .strip_prefix(ORIGIN)
        .and_then(|path| BOOK_PATH.captures(path))
        .ok_or_else(invalid)?;
    let cid = fields.get(3).map(|m| m.as_str());
    if cid.is_some() != chapter {
        return Err(invalid());
    }
    Ok((
        fields.get(1).ok_or_else(invalid)?.as_str(),
        fields.get(2).ok_or_else(invalid)?.as_str(),
        cid,
    ))
}

fn encode<const N: usize>(fields: [(&str, &str); N]) -> String {
    fields
        .into_iter()
        .map(|(key, value)| format!("{key}={}", quote(value, true)))
        .collect::<Vec<_>>()
        .join("&")
}

fn source_json(text: &str) -> Result<Value, WireError> {
    let value: Value = crate::library::parse_json(text)
        .map_err(|_| parse_error("Sáng Tác Việt trả dữ liệu JSON không hợp lệ."))?;
    if !value.is_object() {
        return Err(parse_error("Dữ liệu nguồn không phải object."));
    }
    Ok(value)
}

fn source_code(payload: &Value) -> String {
    payload
        .get("code")
        .map(crate::http::py_string)
        .unwrap_or_default()
}

fn selector(css: &str) -> Selector {
    Selector::parse(css).expect("fixed STV selector")
}

fn node_text(element: ElementRef<'_>) -> String {
    let markup = element.inner_html();
    html_to_text(&markup)
        .unwrap_or_default()
        .split(python_whitespace)
        .filter(|s| !s.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

fn attr<'a>(node: ElementRef<'a>, name: &str) -> &'a str {
    node.attr(name).unwrap_or_default()
}

fn source_page(markup: &str) -> Result<Html, WireError> {
    if markup.len() > MAX_BODY {
        return Err(parse_error("Nội dung nguồn không hợp lệ hoặc quá lớn."));
    }
    crate::http::validate_html_limits(markup).map_err(|error| parse_error(&error.message))?;
    Ok(Html::parse_document(markup))
}

fn metadata(markup: &str, url: &str) -> Result<Value, WireError> {
    let doc = source_page(markup)?;
    let meta = |key: &str| {
        doc.select(&selector("meta[property]"))
            .find(|n| attr(*n, "property") == key)
            .map_or("", |node| attr(node, "content"))
    };
    let name = doc
        .select(&selector("#book_name2"))
        .next()
        .map(node_text)
        .unwrap_or_else(|| {
            let book = meta("og:novel:book_name");
            if book.is_empty() {
                meta("og:title").to_owned()
            } else {
                book.to_owned()
            }
        });
    if name.is_empty() {
        return Err(parse_error(
            "Không tìm thấy thông tin truyện Sáng Tác Việt.",
        ));
    }
    Ok(
        json!({"name":name,"author":meta("og:novel:author"),"description":meta("og:description"),
        "cover":relative_remote(meta("og:image"),url),"path":url,"source":"stv","kind":"text","format":"online"}),
    )
}

fn book_url(host: &str, book: &str) -> String {
    format!("{ORIGIN}/truyen/{host}/1/{book}/")
}

fn toc_entries(value: &Value) -> Result<Vec<(&str, &str)>, WireError> {
    let text = value
        .as_str()
        .map(trim)
        .filter(|text| !text.is_empty() && text.len() <= MAX_BODY)
        .ok_or_else(|| parse_error("Mục lục nguồn trống hoặc không hợp lệ."))?;
    text.split("-//-")
        .map(|line| {
            let mut fields = trim(line).splitn(3, "-/-");
            let _sequence = fields.next();
            let id = fields
                .next()
                .filter(|id| !id.is_empty() && id.bytes().all(|c| c.is_ascii_digit()));
            let title = fields.next().map(trim).filter(|s| !s.is_empty());
            id.zip(title)
                .ok_or_else(|| parse_error("Mục lục thiếu mã hoặc tên chương."))
        })
        .collect()
}

fn challenge_error(challenge: &Pending) -> WireError {
    let mut failure = WireError::new(
        "captcha_required",
        "Sáng Tác Việt yêu cầu xác minh CAPTCHA để tiếp tục.",
    );
    failure.details = Some(Box::new(
        json!({"captcha":{"source":"stv","id":challenge.id}}),
    ));
    failure.verification = Some((challenge.key.clone(), challenge.outcome.clone()));
    failure
}

impl Stv {
    pub(crate) fn search(&self, query: &str, page: i64) -> Result<Value, ApiError> {
        let params = encode([
            ("find", ""),
            ("findinname", query),
            ("minc", "0"),
            ("tag", ""),
            ("p", &page.to_string()),
        ]);
        let html = self.text(
            false,
            &format!("/io/searchtp/searchBooks?{params}"),
            Options::default(),
        )?;
        let doc = source_page(&html)?;
        let rows = doc.select(&selector("a.booksearch")).collect::<Vec<_>>();
        if rows.is_empty() {
            if trim(&html).is_empty()
                || node_text(doc.root_element()).contains("Không tìm thấy truyện?")
            {
                return Ok(json!({"items":[],"next":null}));
            }
            return Err(parse_error("Chưa nhận diện được kết quả tìm kiếm nguồn.").into());
        }
        let mut items = Vec::new();
        let mut seen = HashSet::new();
        for item in &rows {
            let field = |css| {
                item.select(&selector(css))
                    .next()
                    .map(node_text)
                    .unwrap_or_default()
            };
            let link = relative_remote(attr(*item, "href"), ORIGIN);
            let name = field(".searchbooktitle");
            if link.is_empty() || name.is_empty() {
                return Err(parse_error("Kết quả thiếu tên hoặc liên kết truyện.").into());
            }
            let link = Url::parse(&link)
                .map_err(|_| parse_error("Kết quả thiếu tên hoặc liên kết truyện."))?;
            if !link
                .host_str()
                .is_some_and(|host| ["sangtacviet.com", "sangtacviet.vip"].contains(&host))
            {
                return Err(parse_error("Liên kết tìm kiếm không thuộc Sáng Tác Việt.").into());
            }
            let url = format!("{ORIGIN}{}/", link.path().trim_end_matches('/'));
            parts(&url, false)?;
            if seen.insert(url.clone()) {
                let cover = item
                    .select(&selector("img"))
                    .next()
                    .map_or_else(String::new, |node| {
                        let image = ["data-original", "data-src", "src"]
                            .into_iter()
                            .map(|key| attr(node, key))
                            .find(|value| !value.is_empty())
                            .unwrap_or("");
                        relative_remote(image, ORIGIN)
                    });
                items.push(
                    json!({"name":name,"author":field(".searchbookauthor"),"path":url,
                    "cover":cover,"description":field(".searchtag")}),
                );
            }
        }
        let next = (rows.len() >= 48).then(|| page.saturating_add(1));
        Ok(json!({"items":items,"next":next}))
    }

    pub(crate) fn detail(&self, url: &str) -> Result<Value, ApiError> {
        let (host, book, _) = parts(url, false)?;
        let html = self.text(
            false,
            url,
            Options {
                navigation: true,
                ..Options::default()
            },
        )?;
        let mut result = metadata(&html, url)?;
        result["chapters"] = self.toc(host, book)?;
        Ok(result)
    }

    pub(crate) fn cover(&self, url: &str) -> Result<Value, ApiError> {
        parts(url, false)?;
        let html = self.text(
            false,
            url,
            Options {
                navigation: true,
                ..Options::default()
            },
        )?;
        let info = metadata(&html, url)?;
        if info["cover"] == "" {
            return Err(parse_error("Trang truyện không cung cấp bìa hợp lệ.").into());
        }
        Ok(json!({"cover":info["cover"],"path":url}))
    }

    fn toc(&self, host: &str, book: &str) -> Result<Value, WireError> {
        let url = book_url(host, book);
        let params = encode([
            ("ngmar", "chapterlist"),
            ("h", host),
            ("bookid", book),
            ("sajax", "getchapterlist"),
        ]);
        let text = self.text(
            false,
            &format!("/index.php?{params}"),
            Options {
                referer: Some(&url),
                ..Options::default()
            },
        )?;
        let payload = source_json(&text)?;
        if source_code(&payload) != "1" {
            return Err(self.handle_error(&payload, None));
        }
        let translated = toc_entries(&payload["data"])?;
        if crate::http::truthy(&payload["oridata"]) {
            let original = toc_entries(&payload["oridata"])?;
            if translated
                .iter()
                .map(|(id, _)| id)
                .ne(original.iter().map(|(id, _)| id))
            {
                return Err(parse_error("Nguồn chưa trả đủ mục lục tiếng Việt."));
            }
        }
        let mut ids = HashSet::new();
        let mut chapters = Vec::new();
        for (id, title) in translated {
            if ids.insert(id.to_owned()) {
                chapters.push(
                    json!({"index":chapters.len(),"title":title,"url":format!("{url}{id}/")}),
                );
            }
        }
        let mut state = locked(&self.state)?;
        self.check_open()?;
        state.tocs.insert((host.to_owned(), book.to_owned()), ids);
        Ok(Value::Array(chapters))
    }

    fn handle_error(&self, payload: &Value, url: Option<&str>) -> WireError {
        let code = source_code(payload);
        if code == "21"
            && let Some(url) = url
        {
            let challenge = || {
                let mut state = locked(&self.state)?;
                self.check_open()?;
                if state.pending.is_none() {
                    let id = random_id()?;
                    state.pending = Some(Pending {
                        key: id.clone(),
                        id,
                        previous: None,
                        image: None,
                        url: url.to_owned(),
                        outcome: Arc::default(),
                    });
                }
                state
                    .pending
                    .as_ref()
                    .map(challenge_error)
                    .ok_or_else(closed)
            };
            return match challenge() {
                Ok(error) | Err(error) => error,
            };
        }
        let evidence = payload
            .get("err")
            .map(crate::http::py_string)
            .unwrap_or_default();
        let evidence = html_to_text(&evidence)
            .unwrap_or_default()
            .chars()
            .take(400)
            .collect::<String>();
        static VIP: LazyLock<Regex> =
            LazyLock::new(|| Regex::new(r"(?i)\bvip\b").expect("fixed evidence pattern"));
        static RESTRICTION: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)chưa\s+(?:mua|mở\s+khóa)|không\s+có\s+quyền|(?:cần|phải|vui\s+lòng|yêu\s+cầu)\s+(?:mua|mở\s+khóa)").expect("fixed evidence pattern")
        });
        static AUTH: LazyLock<Regex> = LazyLock::new(|| {
            Regex::new(r"(?i)(?:vui\s+lòng|cần|hãy|phải)\s+đăng\s+nhập")
                .expect("fixed evidence pattern")
        });
        let mut failure = if VIP.is_match(&evidence) && RESTRICTION.is_match(&evidence) {
            WireError::new(
                "vip_locked",
                "Chương VIP yêu cầu quyền đọc tại website gốc.",
            )
        } else if AUTH.is_match(&evidence) {
            WireError::new("auth_required", "Website gốc yêu cầu đăng nhập.")
        } else {
            let code = if code.is_empty() { "không rõ" } else { &code };
            let detail = if evidence.is_empty() {
                ".".to_owned()
            } else {
                format!(": {evidence}")
            };
            return WireError::new(
                "source_response_error",
                format!("Sáng Tác Việt trả mã {code}{detail}"),
            );
        };
        let vip = if failure.code == "vip_locked" {
            Some(true)
        } else {
            None
        };
        failure.details = Some(Box::new(
            json!({"access":{"state":failure.code,"vip":vip,"evidence":evidence}}),
        ));
        failure
    }

    pub(crate) fn chapter(
        &self,
        url: &str,
        background: bool,
        cancelled: impl Fn() -> bool,
    ) -> Result<Value, ApiError> {
        let _turn = self.turn(background, &cancelled)?;
        let (host, book, cid) = parts(url, true)?;
        let cid = cid.ok_or_else(|| parse_error("Chương không có trong mục lục nguồn."))?;
        let tuple = (host.to_owned(), book.to_owned());
        let known = {
            let state = locked(&self.state)?;
            if let Some(pending) = state.pending.as_ref() {
                return Err(challenge_error(pending).into());
            }
            state
                .tocs
                .get(&tuple)
                .is_some_and(|chapters| chapters.contains(cid))
        };
        if !known {
            self.toc(host, book)?;
            if !locked(&self.state)?
                .tocs
                .get(&tuple)
                .is_some_and(|chapters| chapters.contains(cid))
            {
                return Err(parse_error("Chương không có trong mục lục nguồn.").into());
            }
        }
        let parent = book_url(host, book);
        let document = self.text(
            true,
            url,
            Options {
                referer: Some(&parent),
                navigation: true,
                ..Options::default()
            },
        )?;
        let params = encode([
            ("bookid", book),
            ("h", host),
            ("c", cid),
            ("ngmar", "readc"),
            ("sajax", "readchapter"),
            ("sty", "1"),
            ("exts", ""),
        ]);
        let api = format!("/index.php?{params}");
        if document.contains("/stv.readinit.js") {
            let bootstrap = self.text(
                true,
                &api,
                Options {
                    referer: Some(url),
                    post: Some(""),
                    cancelled: Some(&cancelled),
                    seed: true,
                    xhr: true,
                    ..Options::default()
                },
            )?;
            let bootstrap = source_json(&bootstrap)?;
            if !["7", "0"].contains(&source_code(&bootstrap).as_str()) {
                return Err(self.handle_error(&bootstrap, Some(url)).into());
            }
        }
        let payload = source_json(&self.text(
            true,
            &api,
            Options {
                referer: Some(url),
                post: Some(""),
                cancelled: Some(&cancelled),
                ..Options::default()
            },
        )?)?;
        if source_code(&payload) != "0" {
            return Err(self.handle_error(&payload, Some(url)).into());
        }
        identity(&payload, host, book, cid)?;
        let raw = payload["data"]
            .as_str()
            .ok_or_else(|| parse_error("Nội dung nguồn không hợp lệ hoặc quá lớn."))?;
        let (mut html, mut text) = clean_chapter(raw, url, host == "ciweimao")?;
        if chinese(&text) {
            let form = encode([("ajax", "trans"), ("content", &text)]);
            let translated = self.text(
                false,
                "/index.php?ngmar=trans&langhint=chinese",
                Options {
                    referer: Some("https://sangtacviet.com/trans/"),
                    post: Some(&form),
                    ..Options::default()
                },
            )?;
            if let Ok(envelope) = crate::library::parse_json(&translated)
                && (envelope.is_object() || envelope.is_array())
            {
                let confirmation = envelope.is_object() && source_code(&envelope) == "21";
                return Err(WireError::new(
                    if confirmation {
                        "translation_verification_required"
                    } else {
                        "translation_error"
                    },
                    if confirmation {
                        "Trans yêu cầu xác minh riêng; chưa có bản dịch hợp lệ."
                    } else {
                        "Trans trả phản hồi lỗi thay vì nội dung dịch."
                    },
                )
                .into());
            }
            (html, text) = clean_chapter(&translated, url, host == "ciweimao")?;
            if chinese(&text) {
                return Err(parse_error("Trans chưa trả nội dung tiếng Việt hợp lệ.").into());
            }
        }
        let clean = Html::parse_fragment(&html);
        let images = clean
            .select(&selector("img"))
            .map(|node| attr(node, "src"))
            .collect::<Vec<_>>();
        Ok(json!({"html":html,"text":text,"images":images,"language":"vi","translation":"source"}))
    }
}

fn comments(node: ElementRef<'_>) -> Option<Vec<String>> {
    let raw = attr(node, "href");
    let link = Url::parse(&relative_remote(raw, "")).ok()?;
    if link.scheme() != "https"
        || link.host_str() != Some("wap.ciweimao.com")
        || link.path() != "/chapter/get_par_tsu_list"
        || raw.contains('#')
    {
        return None;
    }
    Some(
        link.query_pairs()
            .filter(|(key, _)| key == "chapter_id")
            .map(|(_, value)| value.into_owned())
            .collect(),
    )
}

fn identity(payload: &Value, host: &str, book: &str, cid: &str) -> Result<(), WireError> {
    let origin = &payload["origin"];
    let expected = match host {
        "qidian" => Some(format!("https://m.qidian.com/book/{book}/{cid}.html")),
        "fanqie" => Some(format!("https://fanqienovel.com/reader/{cid}")),
        _ => None,
    };
    let mut valid = if let Some(expected) = expected {
        origin == &expected
    } else if origin.is_null() || origin == "" {
        true
    } else if let Some(raw) = origin.as_str()
        && let Ok(parsed) = Url::parse(&relative_remote(raw, ""))
    {
        let last = parsed
            .path()
            .trim_end_matches('/')
            .rsplit('/')
            .next()
            .unwrap_or("");
        let host_name = parsed.host_str().unwrap_or("");
        let correct_id = last == cid
            || last.strip_suffix(".html") == Some(cid)
            || last.strip_suffix(".htm") == Some(cid);
        !raw.contains(['?', '#'])
            && correct_id
            && (!["sangtacviet.com", "sangtacviet.vip"].contains(&host_name)
                || parsed.path() == format!("/truyen/{host}/1/{book}/{cid}/"))
            && (host != "ciweimao"
                || [
                    "ciweimao.com",
                    "www.ciweimao.com",
                    "wap.ciweimao.com",
                    "sangtacviet.com",
                    "sangtacviet.vip",
                ]
                .contains(&host_name))
    } else {
        false
    };
    if host == "ciweimao" {
        let markup = payload["data"]
            .as_str()
            .ok_or_else(|| parse_error("Nội dung nguồn không hợp lệ hoặc quá lớn."))?;
        let doc = source_page(markup)?;
        valid &= doc
            .select(&selector("a.num-link"))
            .all(|node| comments(node).is_some_and(|ids| ids == [cid]));
    }
    if !valid || crate::http::py_string(&payload["bookid"]) != book || payload["bookhost"] != host {
        return Err(parse_error(
            "Chưa xác minh được đúng nguồn/truyện/chương từ phản hồi Sáng Tác Việt; không lưu nội dung không chắc chắn.",
        ));
    }
    Ok(())
}

fn clean_chapter(markup: &str, url: &str, ciweimao: bool) -> Result<(String, String), WireError> {
    crate::http::validate_html_limits(markup).map_err(|error| parse_error(&error.message))?;
    if markup.len() > MAX_BODY {
        return Err(parse_error("Nội dung nguồn không hợp lệ hoặc quá lớn."));
    }
    let mut doc = Html::parse_fragment(markup);
    if ciweimao {
        let removed = doc
            .select(&selector("a.num-link[href], span"))
            .filter(|node| {
                (node.value().name() == "span" && node.value().attrs().next().is_none())
                    || comments(*node).is_some()
            })
            .map(|node| node.id())
            .collect::<Vec<_>>();
        for id in removed {
            if let Some(mut node) = doc.tree.get_mut(id) {
                node.detach();
            }
        }
    }
    let translations = doc
        .select(&selector("i[t][h], i[t][v]"))
        .map(|node| node.id())
        .collect::<Vec<_>>();
    if !translations.is_empty() {
        let sample = Html::parse_fragment("<span></span>");
        if let Some(span) = sample.select(&selector("span")).next() {
            let tag = span.value().name.clone();
            for id in translations {
                if let Some(mut node) = doc.tree.get_mut(id)
                    && let scraper::Node::Element(element) = node.value()
                {
                    element.name = tag.clone();
                }
            }
        }
    }
    let raw = doc.root_element().inner_html();
    let error = || parse_error("Không đọc được cấu trúc nội dung nguồn.");
    let mut clean = crate::http::clean_html(&raw, Some(url), true).map_err(|_| error())?;
    let text = html_to_text(&clean).map_err(|_| error())?;
    if text.is_empty() || text.contains('\u{fffd}') || PLACEHOLDER.is_match(&text) {
        return Err(parse_error(
            "Nguồn trả chương trống, chưa tải xong hoặc lỗi mã hóa.",
        ));
    }
    static BLOCK: LazyLock<Regex> =
        LazyLock::new(|| Regex::new(r"(?i)<(?:p|div|br|h[1-6])\b").expect("fixed block pattern"));
    if !BLOCK.is_match(&clean) {
        clean = text
            .lines()
            .filter(|line| !trim(line).is_empty())
            .map(|line| format!("<p>{}</p>", crate::http::escape(line)))
            .collect();
    }
    Ok((clean, text))
}

fn chinese(text: &str) -> bool {
    let mut han = 0usize;
    let mut latin = 0usize;
    for char in text.chars() {
        if matches!(char, '\u{3400}'..='\u{9fff}') {
            han += 1;
        }
        if matches!(char,'A'..='Z'|'a'..='z'|'\u{c0}'..='\u{24f}'|'\u{1e00}'..='\u{1eff}') {
            latin += 1;
        }
    }
    han > latin
}

impl Stv {
    pub(crate) fn captcha(
        &self,
        action: &str,
        id: &str,
        answer: Option<&str>,
    ) -> Result<CaptchaAction, ApiError> {
        let _turn = self.turn(false, || false)?;
        let expired = || {
            WireError::new(
                "captcha_expired",
                "Yêu cầu CAPTCHA đã hết hiệu lực. Hãy mở lại chương.",
            )
        };
        let challenge = {
            let mut state = locked(&self.state)?;
            let pending = state.pending.as_ref().ok_or_else(expired)?;
            if action == "cancel"
                && pending.outcome.get().is_none()
                && (pending.id == id || pending.previous.as_deref() == Some(id))
            {
                let key = pending.key.clone();
                let _ = pending.outcome.set(false);
                state.pending = None;
                return Ok(CaptchaAction {
                    body: json!({"cancelled":true}),
                    key,
                });
            }
            if pending.id != id {
                return Err(WireError::new(
                    "captcha_expired",
                    "Ảnh CAPTCHA đã thay đổi. Hãy dùng ảnh mới.",
                )
                .into());
            }
            if pending.outcome.get() == Some(&true) {
                return if action == "verify" {
                    Ok(CaptchaAction {
                        body: json!({"verified":true}),
                        key: pending.key.clone(),
                    })
                } else {
                    Err(expired().into())
                };
            }
            pending.clone()
        };
        if ["image", "refresh"].contains(&action) {
            if let Some(image) = challenge.image.as_deref()
                && action == "image"
            {
                return Ok(CaptchaAction {
                    body: json!({"id":challenge.id,"image":image}),
                    key: challenge.key,
                });
            }
            let mut digits = [0; 8];
            getrandom::fill(&mut digits)
                .map_err(|_| WireError::new("source_error", "Không tạo được yêu cầu CAPTCHA."))?;
            let path = format!(
                "/generate_captcha.php?random=0.{}",
                u64::from_le_bytes(digits)
            );
            let reply = self.request(
                true,
                &path,
                Options {
                    referer: Some(&challenge.url),
                    image: true,
                    ..Options::default()
                },
            )?;
            let Reply::Image(image) = reply else {
                return Err(parse_error("Website trả ảnh CAPTCHA không hợp lệ.").into());
            };
            if !valid_captcha_image(&image) {
                return Err(parse_error("Website trả ảnh CAPTCHA không hợp lệ.").into());
            }
            let mut state = locked(&self.state)?;
            self.check_open()?;
            let pending = state.pending.as_mut().ok_or_else(expired)?;
            if action == "refresh" {
                let next = random_id()?;
                pending.previous = Some(std::mem::replace(&mut pending.id, next));
            }
            let body = json!({"id":pending.id,"image":image});
            pending.image = Some(image.into());
            return Ok(CaptchaAction {
                body,
                key: pending.key.clone(),
            });
        }
        if action == "verify" {
            let answer = answer
                .filter(|answer| {
                    challenge.image.is_some()
                        && (4..=128).contains(&trim(answer).chars().count())
                        && !answer.bytes().any(|c| c < 32 || c == 127)
                })
                .ok_or_else(|| {
                    WireError::new(
                        "captcha_failed",
                        "Hãy tải ảnh và nhập mã CAPTCHA từ 4 ký tự.",
                    )
                })?;
            let form = encode([
                ("ajax", "verifycaptcha"),
                ("token", trim(answer)),
                ("purpose", "read"),
                ("provider", "sangtacviet"),
            ]);
            let reply = self.text(
                true,
                "/index.php?ngmar=verifyca",
                Options {
                    referer: Some(&challenge.url),
                    post: Some(&form),
                    fresh: true,
                    ..Options::default()
                },
            )?;
            if trim(&reply) != "success" {
                return Err(WireError::new(
                    "captcha_failed",
                    "Xác minh CAPTCHA thất bại. Kiểm tra mã hoặc làm mới ảnh rồi thử lại.",
                )
                .into());
            }
            let mut state = locked(&self.state)?;
            self.check_open()?;
            let pending = state.pending.as_mut().ok_or_else(expired)?;
            // Keep a successful verification until the coordinator confirms its durable transition.
            let _ = pending.outcome.set(true);
            return Ok(CaptchaAction {
                body: json!({"verified":true}),
                key: pending.key.clone(),
            });
        }
        Err(WireError::new("source_error", "Thao tác CAPTCHA không hợp lệ.").into())
    }
}

fn valid_captcha_image(image: &str) -> bool {
    let Some((kind, data)) = image
        .strip_prefix("data:")
        .and_then(|value| value.split_once(";base64,"))
    else {
        return false;
    };
    let Ok(bytes) = STANDARD.decode(data) else {
        return false;
    };
    match kind {
        "image/png" => bytes.starts_with(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg" => bytes.starts_with(b"\xff\xd8\xff"),
        "image/gif" => bytes.starts_with(b"GIF87a") || bytes.starts_with(b"GIF89a"),
        "image/webp" => bytes.starts_with(b"RIFF") && bytes.get(8..12) == Some(b"WEBP"),
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::{
            Barrier,
            atomic::{AtomicBool, Ordering},
            mpsc,
        },
        thread,
    };

    const BOOK: &str = "https://sangtacviet.com/truyen/qidian/1/123/";
    const CHAPTER: &str = "https://sangtacviet.com/truyen/qidian/1/123/901/";
    const TOC: &str = "https://sangtacviet.com/index.php?ngmar=chapterlist&h=qidian&bookid=123&sajax=getchapterlist";
    const READ: &str = "https://sangtacviet.com/index.php?bookid=123&h=qidian&c=901&ngmar=readc&sajax=readchapter&sty=1&exts=";
    const TRANS: &str = "https://sangtacviet.com/index.php?ngmar=trans&langhint=chinese";
    const IMAGE: &str = "https://sangtacviet.com/generate_captcha.php";
    const VERIFY: &str = "https://sangtacviet.com/index.php?ngmar=verifyca";

    #[test]
    fn m5_stv_bootstrap_cookie_lanes_captcha_and_source_trans_are_ordered() {
        let script=fixture_fetcher(Some(&json!([
            {"url":BOOK,"text":"<meta property='og:title' content='Truyện thử'><meta property='og:image' content='https://cdn.example.org/cover.png'>"},
            {"url":TOC,"text":"{\"code\":1,\"data\":\"1-/-901-/-Một chương\"}","headers":[["Set-Cookie","meta=separate; Path=/"]]},
            {"url":CHAPTER,"headers":[["Set-Cookie","_ac=server; Path=/"],["Set-Cookie","lang=zh; Path=/"]],
                "text":"<script src='/stv.readinit.js'></script><script>document.cookie=\"_ac=literal; Path=/\";</script>"},
            {"url":READ,"method":"POST","request_body":"server","headers":[["Set-Cookie","_ac=rotated; Path=/"]],
              "text":"{\"code\":7}"},
            {"url":READ,"method":"POST","request_body":"","text":"{\"code\":21}"},
            {"url":IMAGE,"headers":[["Content-Type","image/png"]],"body_base64":"iVBORw0KGgo=","repeat":true},
            {"url":VERIFY,"method":"POST","request_body":"ajax=verifycaptcha&token=answ&purpose=read&provider=sangtacviet",
                "text":"success"},
            {"url":CHAPTER,"text":"<p>Khởi tạo xong</p>"},
            {"url":READ,"method":"POST","request_body":"",
                "text":json!({"code":0,"bookid":123,"bookhost":"qidian","origin":"https://m.qidian.com/book/123/901.html",
                    "data":"<p>中文甲。</p>"}).to_string()},
            {"url":TRANS,"method":"POST","request_body":encode([("ajax","trans"),("content","中文甲。")]),
                "text":"<p>Tiếng Việt.</p>"},
        ]))).unwrap();
        let observations = Arc::new(Mutex::new(Vec::new()));
        let seen = observations.clone();
        let transport: Arc<Fetcher> = Arc::new(move |req, stop| {
            seen.lock().unwrap().push((
                req.url.clone(),
                req.headers.clone(),
                req.fresh_ipv4,
                req.body.clone(),
            ));
            script(req, stop)
        });
        let stv = Stv::fixture(transport);
        let detail = stv.detail(BOOK).unwrap();
        assert_eq!(detail["name"], "Truyện thử");
        assert_eq!(detail["chapters"][0]["url"], CHAPTER);
        let error = stv.chapter(CHAPTER, false, || false).unwrap_err();
        assert_eq!(error.status, 409);
        let key = error.verification().unwrap().0.clone();
        let details = error.json();
        let first = details["captcha"]["id"].as_str().unwrap();
        assert_eq!(
            stv.captcha("verify", first, Some("answ"))
                .err()
                .unwrap()
                .code(),
            Some("captcha_failed")
        );
        let last_read = stv.reading.lock().unwrap().last_read.unwrap();
        assert_eq!(
            stv.captcha("image", first, None).unwrap().body["image"],
            "data:image/png;base64,iVBORw0KGgo="
        );
        let rotated = stv.captcha("refresh", first, None).unwrap();
        assert_eq!(rotated.key, key);
        let second = rotated.body["id"].as_str().unwrap();
        assert_ne!(first, second);
        assert_eq!(
            stv.captcha("verify", first, Some("answ"))
                .err()
                .unwrap()
                .code(),
            Some("captcha_expired")
        );
        assert_eq!(
            stv.captcha("verify", second, Some(" answ ")).unwrap().key,
            key
        );
        assert_eq!(
            stv.captcha("verify", second, Some("answ")).unwrap().body["verified"],
            true
        );
        assert_eq!(
            stv.chapter(CHAPTER, false, || false).unwrap_err().status,
            409
        );
        stv.complete_verification(&key);
        assert!(stv.state.lock().unwrap().pending.is_none());
        assert_eq!(
            stv.chapter(CHAPTER, false, || false).unwrap(),
            json!({
                "html":"<p>Tiếng Việt.</p>","text":"Tiếng Việt.","images":[],"language":"vi","translation":"source",
            })
        );
        assert_eq!(
            stv.reading
                .lock()
                .unwrap()
                .last_read
                .unwrap()
                .duration_since(last_read),
            READ_INTERVAL
        );
        let observed = observations.lock().unwrap();
        let mut reads = observed.iter().filter(|(url, ..)| url == READ);
        let (_, seed, _, body) = reads.next().unwrap();
        assert_eq!(body, b"server");
        assert_eq!(seed["x-requested-with"], "XmlHttpRequest");
        assert!(seed["cookie"].to_str().unwrap().contains("lang=vi"));
        assert!(seed["cookie"].to_str().unwrap().contains("_ac=server"));
        assert!(!seed["cookie"].to_str().unwrap().contains("meta=separate"));
        assert!(
            reads.next().unwrap().1["cookie"]
                .to_str()
                .unwrap()
                .contains("_ac=rotated")
        );
        let mut verify = observed.iter().filter(|(url, ..)| url == VERIFY);
        let (_, headers, fresh, body) = verify.next().unwrap();
        assert!(*fresh);
        assert_eq!(headers["connection"], "close");
        assert_eq!(
            headers["content-length"].to_str().unwrap(),
            body.len().to_string()
        );
        assert!(verify.next().is_none());
        let (_, headers, _, _) = observed.iter().find(|(url, ..)| url == TRANS).unwrap();
        assert_eq!(headers["cookie"], "meta=separate");
    }

    fn wait_count(stv: &Stv, count: usize) {
        let end = Instant::now() + Duration::from_secs(2);
        while stv
            .state
            .lock()
            .unwrap()
            .waiters
            .iter()
            .map(VecDeque::len)
            .sum::<usize>()
            != count
        {
            assert!(Instant::now() < end);
            thread::sleep(Duration::from_millis(2));
        }
    }

    #[test]
    fn m5_stv_queue_prefers_interactive_preserves_fifo_and_cancels_waiters() {
        let stv = Stv::fixture(fixture_fetcher(None).unwrap());
        let active = stv.turn(false, || false).unwrap();
        let (send, rx) = mpsc::channel();
        let queued = AtomicBool::new(false);
        thread::scope(|scope| {
            for (ticket, background) in [(0, true), (1, true), (2, false), (3, false), (4, true)] {
                let queue = &stv;
                let sender = send.clone();
                let cancel = &queued;
                scope.spawn(move || {
                    let admission =
                        queue.turn(background, || ticket == 4 && cancel.load(Ordering::Acquire));
                    if let Err(ref error) = admission {
                        assert_eq!(error.code, "request_cancelled");
                    }
                    sender.send((ticket, admission.is_ok())).unwrap();
                });
                wait_count(&stv, ticket + 1);
            }
            queued.store(true, Ordering::Release);
            assert_eq!(rx.recv_timeout(Duration::from_secs(2)).unwrap(), (4, false));
            drop(active);
            for ticket in [2, 3, 0, 1] {
                assert_eq!(
                    rx.recv_timeout(Duration::from_secs(2)).unwrap(),
                    (ticket, true)
                );
            }
        });
        {
            let mut state = stv.state.lock().unwrap();
            state.waiters[1].extend(0..MAX_WAITERS as u64);
        }
        assert_eq!(stv.turn(false, || false).err().unwrap().code, "source_busy");
        stv.close();
        assert_eq!(
            stv.turn(false, || false).err().unwrap().code,
            "network_error"
        );
    }

    #[test]
    fn m5_real_pacing_is_woken_by_engine_close() {
        let stv = Stv::new(fixture_fetcher(None).unwrap());
        let entered = Barrier::new(2);
        thread::scope(|scope| {
            let source = &stv;
            let barrier = &entered;
            let (send, rx) = mpsc::channel();
            scope.spawn(move || {
                barrier.wait();
                send.send(source.pace(Some(Instant::now()), None).unwrap_err().code)
                    .unwrap();
            });
            entered.wait();
            stv.close();
            assert_eq!(
                rx.recv_timeout(Duration::from_secs(1)).unwrap(),
                "network_error"
            );
        });
    }

    #[test]
    fn m5_stv_cancelled_throttled_read_does_not_advance_virtual_clock_or_send_request() {
        let sends = Arc::new(std::sync::atomic::AtomicUsize::new(0));
        let sent = sends.clone();
        let wire: Arc<Fetcher> = Arc::new(move |_, _| {
            sent.fetch_add(1, Ordering::Relaxed);
            Ok(WireResponse {
                status: 200,
                headers: HeaderMap::new(),
                body: br#"{"code":7}"#.to_vec(),
            })
        });
        let stv = Stv::fixture(wire);
        stv.text(
            true,
            READ,
            Options {
                post: Some(""),
                ..Options::default()
            },
        )
        .unwrap();
        let before = stv.reading.lock().unwrap().last_read.unwrap();
        assert_eq!(
            stv.text(
                true,
                READ,
                Options {
                    post: Some(""),
                    cancelled: Some(&|| true),
                    ..Options::default()
                }
            )
            .unwrap_err()
            .code,
            "request_cancelled"
        );
        assert_eq!(stv.reading.lock().unwrap().last_read, Some(before));
        assert_eq!(sends.load(Ordering::Relaxed), 1);
        stv.text(
            true,
            READ,
            Options {
                post: Some(""),
                ..Options::default()
            },
        )
        .unwrap();
        let after = stv.reading.lock().unwrap().last_read.unwrap();
        assert_eq!(after.duration_since(before), READ_INTERVAL);
    }
}
