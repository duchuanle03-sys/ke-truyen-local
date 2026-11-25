use crate::{
    ApiError,
    http::{quote, relative_remote},
    stv::{Cancellation, Transport, WireRequest},
};
use hyper::{
    HeaderMap, Method,
    header::{ACCEPT, ACCEPT_ENCODING, COOKIE, HeaderValue, LOCATION, REFERER, USER_AGENT},
};
use std::{
    collections::HashMap,
    sync::{Arc, LazyLock, Mutex},
    time::{Duration, Instant},
};

pub(crate) struct PublicFetch {
    pub(crate) transport: Arc<Transport>,
    stop: Cancellation,
    qq_cookie: Mutex<Option<HeaderValue>>,
}

fn source_error(message: &str) -> ApiError {
    ApiError::new(400, message).with_code("source_error")
}

fn ascii_url(url: &str) -> String {
    let mut result = String::with_capacity(url.len());
    for ch in url.chars() {
        if ch.is_ascii() {
            result.push(ch);
        } else {
            result.push_str(&quote(ch.encode_utf8(&mut [0; 4]), false));
        }
    }
    result
}

impl PublicFetch {
    pub(crate) fn new() -> Result<Self, ApiError> {
        Ok(Self {
            transport: Arc::new(Transport::new()?),
            stop: Cancellation::default(),
            qq_cookie: Mutex::default(),
        })
    }

    pub(crate) fn close(&self) {
        self.stop.cancel();
    }

    pub(crate) fn open_original(&self, url: &str) -> bool {
        if self.stop.is_cancelled() {
            return false;
        }
        #[cfg(windows)]
        {
            use std::ptr::null;
            use windows_sys::Win32::{
                System::Com::{
                    COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE, CoInitializeEx,
                    CoUninitialize,
                },
                UI::{Shell::ShellExecuteW, WindowsAndMessaging::SW_SHOWNORMAL},
            };
            let url = url
                .encode_utf16()
                .chain(std::iter::once(0))
                .collect::<Vec<_>>();
            // SAFETY: URL was canonicalized by the caller and is NUL-terminated. All other pointers are optional.
            unsafe {
                let initialized = CoInitializeEx(
                    null(),
                    (COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) as u32,
                ) >= 0;
                let opened = ShellExecuteW(
                    std::ptr::null_mut(),
                    null(),
                    url.as_ptr(),
                    null(),
                    null(),
                    SW_SHOWNORMAL,
                ) as isize
                    > 32;
                if initialized {
                    CoUninitialize();
                }
                opened
            }
        }
        #[cfg(not(windows))]
        {
            let _ = url;
            false
        }
    }

    fn get(
        &self,
        url: &str,
        referer: &str,
        limit: usize,
        accept: &'static str,
    ) -> Result<crate::stv::WireResponse, ApiError> {
        let invalid = || {
            source_error("Chỉ chấp nhận URL HTTP(S) công khai, không kèm tài khoản hoặc cổng riêng")
        };
        let mut url = relative_remote(url, "");
        let mut history = HashMap::new();
        let start = Instant::now();
        for _ in 0..=10 {
            if url.is_empty() {
                return Err(invalid());
            }
            let mut headers = HeaderMap::new();
            headers.insert(
                USER_AGENT,
                HeaderValue::from_static(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LocalReader/1.0",
                ),
            );
            headers.insert(ACCEPT, HeaderValue::from_static(accept));
            headers.insert(ACCEPT_ENCODING, HeaderValue::from_static("identity"));
            if !referer.is_empty() {
                let referer = relative_remote(referer, "");
                if referer.is_empty() {
                    return Err(invalid());
                }
                headers.insert(
                    REFERER,
                    HeaderValue::from_str(&ascii_url(&referer)).map_err(|_| invalid())?,
                );
            }
            if url.starts_with("https://truyenqqko.com/")
                && let Some(cookie) = self
                    .qq_cookie
                    .lock()
                    .map_err(|_| rusqlite::Error::InvalidQuery)?
                    .clone()
            {
                headers.insert(COOKIE, cookie);
            }
            let timeout = Duration::from_secs(20)
                .checked_sub(start.elapsed())
                .ok_or_else(|| {
                    ApiError::new(503, "Nguồn phản hồi quá lâu.").with_code("source_timeout")
                })?;
            let result = self.transport.fetch_blocking(
                WireRequest {
                    url: ascii_url(&url),
                    method: Method::GET,
                    headers,
                    body: Vec::new(),
                    limit,
                    fresh_ipv4: false,
                    timeout,
                },
                &self.stop,
            )?;
            if (200..300).contains(&result.status) {
                return Ok(result);
            }
            if [301, 302, 303, 307, 308].contains(&result.status)
                && let Some(location) = result.headers.get(LOCATION).and_then(|v| v.to_str().ok())
            {
                let next = checked_redirect(&url, location)?;
                let count = history.entry(next.clone()).or_insert(0);
                *count += 1;
                if *count > 4 {
                    break;
                }
                url = next;
                continue;
            }
            return Err(
                ApiError::new(400, format!("Nguồn trả HTTP {}", result.status))
                    .with_code("http_error")
                    .with_details(serde_json::json!({"http_status":result.status})),
            );
        }
        Err(source_error("Nguồn chuyển hướng quá nhiều lần"))
    }

    pub(crate) fn page(&self, url: &str, limit: usize) -> Result<String, ApiError> {
        static QQ_COOKIE: LazyLock<regex::Regex> = LazyLock::new(|| {
            regex::Regex::new(r#"document\.cookie\s*=\s*["']([A-Za-z0-9_-]+=[A-Za-z0-9._~%+/-]*)(?:;[^\r\n]*?)?["']"#).unwrap()
        });
        let mut text = decode(self.get(
            url,
            "",
            limit,
            "text/html,application/json;q=0.9,image/*;q=0.8",
        )?)?;
        if url.starts_with("https://truyenqqko.com/")
            && text.chars().take(5_000).count() < 5_000
            && let Some(cookie) = QQ_COOKIE
                .captures(&text)
                .and_then(|c| c.get(1))
                .and_then(|c| HeaderValue::from_str(c.as_str()).ok())
        {
            *self
                .qq_cookie
                .lock()
                .map_err(|_| rusqlite::Error::InvalidQuery)? = Some(cookie);
            text = decode(self.get(
                url,
                "",
                limit,
                "text/html,application/json;q=0.9,image/*;q=0.8",
            )?)?;
        }
        Ok(text)
    }

    pub(crate) fn image(&self, url: &str, referer: &str) -> Result<Vec<u8>, ApiError> {
        let read = |url, referer| {
            self.get(url, referer, 20 * 1024 * 1024, "image/*")
                .map(|reply| reply.body)
        };
        match read(url, referer) {
            Err(error)
                if error.json()["http_status"] == 403
                    && url::Url::parse(referer)
                        .ok()
                        .and_then(|value| {
                            value
                                .host_str()
                                .map(|host| host.trim_start_matches("www.").to_string())
                        })
                        .is_some_and(|host| {
                            ["sangtacviet.com", "sangtacviet.vip"].contains(&host.as_str())
                        }) =>
            {
                let upgraded = url
                    .strip_prefix("http:")
                    .map(|rest| format!("https:{rest}"));
                read(upgraded.as_deref().unwrap_or(url), "")
            }
            result => result,
        }
    }
}

fn checked_redirect(base: &str, location: &str) -> Result<String, ApiError> {
    let url = relative_remote(location, base);
    if url.is_empty() || (base.starts_with("https:") && !url.starts_with("https:")) {
        return Err(source_error("Chuyển hướng nguồn không an toàn"));
    }
    Ok(url)
}

fn decode(response: crate::stv::WireResponse) -> Result<String, ApiError> {
    let charset = response
        .headers
        .get(hyper::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .and_then(|header| {
            header
                .split(';')
                .skip(1)
                .filter_map(|p| p.trim().split_once('='))
                .find_map(|(k, v)| {
                    k.trim()
                        .eq_ignore_ascii_case("charset")
                        .then_some(v.trim().trim_matches(['\"', '\'']))
                })
        })
        .unwrap_or("utf-8");
    if ["utf-8", "utf8"]
        .iter()
        .any(|s| s.eq_ignore_ascii_case(charset))
    {
        Ok(String::from_utf8_lossy(&response.body).into_owned())
    } else if ["latin-1", "iso-8859-1"]
        .iter()
        .any(|s| s.eq_ignore_ascii_case(charset))
    {
        Ok(response.body.into_iter().map(char::from).collect())
    } else {
        Err(source_error("Nguồn trả bảng mã không hợp lệ"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redirects_preserve_signed_spelling_and_reject_unsafe_targets() {
        let origin = "https://files.example.org/page/1";
        assert_eq!(
            checked_redirect(origin, "../ảnh?a=%2f&a=A%20B")
                .map(|s| ascii_url(&s))
                .unwrap(),
            "https://files.example.org/%E1%BA%A3nh?a=%2f&a=A%20B"
        );
        for target in [
            "http://files.example.org/",
            "https://127.0.0.1/private",
            "//user@files.example.org/",
            "https://files.example.org:81/",
        ] {
            assert!(checked_redirect(origin, target).is_err(), "{target}");
        }
    }
}
