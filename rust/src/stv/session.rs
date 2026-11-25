use super::{Cancellation, Fetcher, MAX_BODY, ORIGIN, WireError, WireRequest};
use base64::{Engine, engine::general_purpose::STANDARD};
use hyper::{HeaderMap, Method, header::HeaderValue};
use regex::Regex;
use std::{
    sync::LazyLock,
    time::{Duration, Instant, UNIX_EPOCH},
};
use url::Url;

const UA: &str = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36";
pub(super) const READ_INTERVAL: Duration = Duration::from_secs(10);

#[derive(Default)]
pub(super) struct Options<'a> {
    pub(super) referer: Option<&'a str>,
    pub(super) post: Option<&'a str>,
    pub(super) cancelled: Option<&'a dyn Fn() -> bool>,
    pub(super) navigation: bool,
    pub(super) xhr: bool,
    pub(super) seed: bool,
    pub(super) image: bool,
    pub(super) fresh: bool,
}

pub(super) struct Session {
    reading: bool,
    jar: Vec<Cookie>,
    pub(super) last_read: Option<Instant>,
}

struct Cookie {
    name: String,
    value: String,
    path: String,
    expires: Option<f64>,
}

fn headers_error() -> WireError {
    WireError::new("parse_error", "HTTP headers không hợp lệ")
}

pub(super) fn checked(path: &str, referer: Option<&str>) -> Result<(String, Url), WireError> {
    static CHAPTER: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"^/truyen/[a-zA-Z0-9_-]+/1/[0-9]+/(?:[0-9]+/)?$").expect("fixed STV path")
    });
    let invalid = || {
        WireError::new(
            "source_error",
            "Yêu cầu ngoài nguồn Sáng Tác Việt bị từ chối.",
        )
    };
    let url = if path.starts_with('/') && !path.starts_with("//") {
        format!("{ORIGIN}{path}")
    } else {
        path.to_owned()
    };
    let target = Url::parse(&url).map_err(|_| invalid())?;
    let valid = |url: &Url| {
        url.origin().ascii_serialization() == ORIGIN
            && url.username().is_empty()
            && url.password().is_none()
            && url.fragment().is_none()
    };
    if !valid(&target)
        || ![
            "/index.php",
            "/search/",
            "/io/searchtp/searchBooks",
            "/generate_captcha.php",
        ]
        .contains(&target.path())
            && !CHAPTER.is_match(target.path())
        || referer.is_some_and(|s| Url::parse(s).ok().is_none_or(|url| !valid(&url)))
    {
        return Err(invalid());
    }
    Ok((url, target))
}

pub(super) enum Reply {
    Text(String),
    Image(String),
}

impl Session {
    pub(super) fn new(reading: bool) -> Self {
        let mut session = Self {
            reading,
            jar: vec![],
            last_read: None,
        };
        if reading {
            session.preferences();
        }
        session
    }

    fn preferences(&mut self) {
        for (name, value) in [("lang", "vi"), ("transmode", "name"), ("foreignlang", "vi")] {
            self.put(
                &format!("{name}={value}; Path=/"),
                false,
                crate::reader::now(),
            );
        }
    }

    fn put(&mut self, raw: &str, literal: bool, now: f64) {
        let mut parts = raw.split(';').map(str::trim);
        let Some((name, value)) = parts.next().and_then(|pair| pair.split_once('=')) else {
            return;
        };
        let ordinary = |c: u8| c.is_ascii_alphanumeric() || matches!(c, b'_' | b'-');
        if name.is_empty() || !name.bytes().all(ordinary) || value.contains(['\r', '\n']) {
            return;
        }
        if literal
            && (!["_ac", "_acx", "_gac"].contains(&name)
                || value.is_empty()
                || !value
                    .bytes()
                    .all(|byte| ordinary(byte) || b"+/=".contains(&byte)))
        {
            return;
        }
        let mut domain = None;
        let mut path = None;
        let mut max_age = None;
        let mut expires = None;
        for (attr, val) in parts.filter_map(|part| part.split_once('=')) {
            for (key, slot) in [
                ("domain", &mut domain),
                ("path", &mut path),
                ("max-age", &mut max_age),
                ("expires", &mut expires),
            ] {
                if key.eq_ignore_ascii_case(attr) && slot.is_none() {
                    *slot = Some(val);
                }
            }
        }
        if let Some(domain) = domain.filter(|val| !val.is_empty())
            && !domain
                .strip_prefix('.')
                .unwrap_or(domain)
                .eq_ignore_ascii_case("sangtacviet.com")
        {
            return;
        }
        let path = path.filter(|val| !val.is_empty()).unwrap_or("/");
        if !path.starts_with('/') {
            return;
        }
        let expires = if let Some(seconds) = max_age {
            let seconds = seconds.trim();
            let seconds = if seconds.is_empty() {
                Some(0.0)
            } else {
                seconds.parse::<f64>().ok()
            };
            seconds.filter(|x| x.is_finite()).map(|x| now + x)
        } else {
            expires
                .and_then(|raw| httpdate::parse_http_date(raw).ok())
                .map(|date| {
                    date.duration_since(UNIX_EPOCH)
                        .map_or_else(|e| -e.duration().as_secs_f64(), |d| d.as_secs_f64())
                })
        };
        let old = self
            .jar
            .iter()
            .position(|cookie| cookie.name == name && cookie.path == path);
        if expires.is_some_and(|time| time <= now) {
            if let Some(index) = old {
                self.jar.remove(index);
            }
        } else {
            let cookie = Cookie {
                name: name.into(),
                value: value.into(),
                path: path.into(),
                expires,
            };
            if let Some(index) = old {
                self.jar[index] = cookie;
            } else {
                self.jar.push(cookie);
            }
        }
    }

    fn cookie_header(&self, path: &str, now: f64) -> String {
        let mut found = self
            .jar
            .iter()
            .filter(|cookie| {
                cookie.expires.is_none_or(|expires| expires > now)
                    && (path == cookie.path
                        || path.strip_prefix(&cookie.path).is_some_and(|rest| {
                            cookie.path.ends_with('/') || rest.starts_with('/')
                        }))
            })
            .collect::<Vec<_>>();
        found.sort_by_key(|cookie| std::cmp::Reverse(cookie.path.len()));
        found
            .iter()
            .map(|c| format!("{}={}", c.name, c.value))
            .collect::<Vec<_>>()
            .join("; ")
    }

    pub(super) fn request(
        &mut self,
        target: (String, Url),
        options: Options<'_>,
        fetch: &Fetcher,
        cancel: &Cancellation,
    ) -> Result<Reply, WireError> {
        let (url, target) = target;
        let mut headers = HeaderMap::new();
        let mut insert = |key: &'static str, value: &str| -> Result<(), WireError> {
            headers.insert(
                key,
                HeaderValue::from_str(value).map_err(|_| headers_error())?,
            );
            Ok(())
        };
        let referer = options.referer.unwrap_or("https://sangtacviet.com/");
        insert("user-agent", UA)?;
        insert("referer", referer)?;
        if !options.image {
            insert("accept-language", "en-US,en;q=0.9")?;
            insert("accept-encoding", "gzip, deflate, br")?;
            insert(
                "accept",
                if options.navigation {
                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
                } else {
                    "*/*"
                },
            )?;
            insert(
                "sec-fetch-dest",
                if options.navigation {
                    "document"
                } else {
                    "empty"
                },
            )?;
            insert(
                "sec-fetch-mode",
                if options.navigation {
                    "navigate"
                } else {
                    "cors"
                },
            )?;
            insert(
                "sec-fetch-site",
                if options.navigation {
                    "none"
                } else {
                    "same-origin"
                },
            )?;
            insert(
                "sec-ch-ua",
                r#""Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156""#,
            )?;
            insert("sec-ch-ua-mobile", "?0")?;
            insert("sec-ch-ua-platform", r#""Windows""#)?;
        }
        if options.navigation {
            insert("sec-fetch-user", "?1")?;
            insert("upgrade-insecure-requests", "1")?;
        }
        if options.xhr {
            insert("x-requested-with", "XmlHttpRequest")?;
        }
        let now = crate::reader::now();
        let cookie = self.cookie_header(target.path(), now);
        if !cookie.is_empty() {
            insert("cookie", &cookie)?;
        }
        let (method, body) = if let Some(body) = options.post {
            insert("content-type", "application/x-www-form-urlencoded")?;
            insert("origin", ORIGIN)?;
            let body = if options.seed {
                self.jar
                    .iter()
                    .find(|c| {
                        c.name == "_ac"
                            && c.path == "/"
                            && c.expires.is_none_or(|expires| expires > now)
                    })
                    .map(|c| c.value.as_str())
                    .filter(|value| !value.is_empty())
                    .ok_or_else(|| {
                        WireError::new("parse_error", "Trang chương chưa cấp cookie khởi tạo _ac.")
                    })?
            } else {
                body
            };
            if options.fresh {
                insert("connection", "close")?;
                insert("content-length", &body.len().to_string())?;
            }
            (Method::POST, body.as_bytes().to_vec())
        } else {
            (Method::GET, Vec::new())
        };
        let limit = if options.image { 1024 * 1024 } else { MAX_BODY };
        let response = fetch(
            WireRequest {
                url,
                method,
                headers,
                body,
                limit,
                fresh_ipv4: options.fresh,
                timeout: Duration::from_secs(if options.fresh { 25 } else { 18 }),
            },
            cancel,
        )
        .map_err(|mut error| {
            if error.code == "source_timeout" && !options.fresh {
                error.message = "Sáng Tác Việt phản hồi quá lâu.".into();
            }
            error
        })?;
        if !(200..300).contains(&response.status) {
            return Err(http_error(response.status));
        }
        if response.body.len() > limit {
            return Err(WireError::new(
                "parse_error",
                "Nội dung nguồn vượt giới hạn an toàn.",
            ));
        }
        let (text, image) = if options.image {
            (None, Some(response.body))
        } else {
            let mut text = String::from_utf8(response.body).map_err(|_| {
                WireError::new(
                    "parse_error",
                    "Nội dung Sáng Tác Việt không phải UTF-8 hợp lệ.",
                )
            })?;
            if text.starts_with('\u{feff}') {
                text.drain(..'\u{feff}'.len_utf8());
            }
            static COOKIES: LazyLock<Regex> = LazyLock::new(|| {
                Regex::new(r#"document\.cookie\s*=\s*(?:"([^"\r\n]*)"|'([^'\r\n]*)')"#)
                    .expect("fixed cookie grammar")
            });
            for matched in COOKIES.captures_iter(&text) {
                if let Some(raw) = matched.get(1).or_else(|| matched.get(2)) {
                    self.put(raw.as_str(), true, crate::reader::now());
                }
            }
            (Some(text), None)
        };
        for cookie in response.headers.get_all("set-cookie") {
            if let Ok(cookie) = cookie.to_str() {
                self.put(cookie, false, crate::reader::now());
            }
        }
        if self.reading && options.navigation {
            self.preferences();
        }
        if let Some(text) = text {
            Ok(Reply::Text(text))
        } else {
            let content_type = response
                .headers
                .get("content-type")
                .and_then(|v| v.to_str().ok())
                .and_then(|v| v.split(';').next())
                .unwrap_or("")
                .to_ascii_lowercase();
            if !["image/png", "image/jpeg", "image/gif", "image/webp"]
                .contains(&content_type.as_str())
            {
                return Err(WireError::new(
                    "parse_error",
                    "Website chưa trả ảnh CAPTCHA hợp lệ.",
                ));
            }
            Ok(Reply::Image(format!(
                "data:{content_type};base64,{}",
                STANDARD.encode(image.unwrap_or_default())
            )))
        }
    }
}

fn http_error(status: u16) -> WireError {
    let tail = if (300..400).contains(&status) {
        "; không tự chuyển hướng."
    } else {
        "."
    };
    let mut error = WireError::new(
        "http_error",
        format!("Sáng Tác Việt trả HTTP {status}{tail}"),
    );
    error.http_status = Some(status);
    error
}
