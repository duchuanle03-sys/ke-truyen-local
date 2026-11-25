//! Production launcher (reference: Python `run.py`): open the user's library on loopback,
//! reuse an instance already serving the same library, and open an app window.

use crate::{ApiError, Library};
use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    time::Duration,
};

struct Options {
    data_dir: Option<PathBuf>,
    port: u16,
    no_browser: bool,
    book: Option<String>,
    import_backup: Option<PathBuf>,
    import_qt: Option<PathBuf>,
}

fn parse(args: impl Iterator<Item = String>) -> Result<Options, String> {
    let mut options = Options {
        data_dir: None,
        port: 8765,
        no_browser: false,
        book: None,
        import_backup: None,
        import_qt: None,
    };
    let mut args = args.peekable();
    while let Some(arg) = args.next() {
        let (key, inline) = match arg.split_once('=') {
            Some((key, value)) if key.starts_with("--") => (key.to_owned(), Some(value.to_owned())),
            _ => (arg.clone(), None),
        };
        if key == "--no-browser" {
            options.no_browser = true;
            continue;
        }
        let value = match inline.or_else(|| args.next()) {
            Some(value) => value,
            None => return Err(format!("Thiếu giá trị cho {key}.")),
        };
        match key.as_str() {
            "--data-dir" => options.data_dir = Some(PathBuf::from(value)),
            "--port" => {
                options.port = value
                    .parse()
                    .map_err(|_| format!("Cổng không hợp lệ: {value}"))?
            }
            "--book" => options.book = Some(value),
            "--import-backup" => options.import_backup = Some(PathBuf::from(value)),
            "--import-qt" => options.import_qt = Some(PathBuf::from(value)),
            _ => return Err(format!("Tham số không hỗ trợ: {key}")),
        }
    }
    if let Some(book) = &options.book
        && (book.is_empty() || book.chars().count() > 256 || book.chars().any(|c| (c as u32) < 32))
    {
        return Err("Mã truyện không hợp lệ.".into());
    }
    Ok(options)
}

/// The first ancestor of the executable that holds the unchanged frontend.
fn project_dir() -> Result<PathBuf, ApiError> {
    let exe = std::env::current_exe()?;
    exe.ancestors()
        .skip(1)
        .find(|dir| dir.join("web").join("index.html").is_file())
        .map(Path::to_owned)
        .ok_or_else(|| ApiError::new(500, "Không tìm thấy thư mục web cạnh ứng dụng."))
}

/// `os.path.normcase(os.path.normpath(...))` for Windows identity comparison.
fn identity(path: &str) -> String {
    let path = path.strip_prefix(r"\\?\").unwrap_or(path);
    path.replace('/', "\\")
        .trim_end_matches('\\')
        .to_lowercase()
}

fn resolved(path: &Path) -> String {
    let full = std::fs::canonicalize(path)
        .unwrap_or_else(|_| std::path::absolute(path).unwrap_or_else(|_| path.to_owned()));
    full.to_string_lossy().into_owned()
}

/// True when a reader on `port` reports this same data directory.
fn running_instance(data_dir: &Path, port: u16) -> bool {
    let probe = || -> std::io::Result<Option<serde_json::Value>> {
        let mut stream =
            TcpStream::connect_timeout(&([127, 0, 0, 1], port).into(), Duration::from_millis(500))?;
        stream.set_read_timeout(Some(Duration::from_millis(500)))?;
        write!(
            stream,
            "GET /api/instance HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
        )?;
        let mut response = Vec::new();
        stream.take(16_385 + 4096).read_to_end(&mut response)?;
        let text = String::from_utf8_lossy(&response);
        let Some((head, body)) = text.split_once("\r\n\r\n") else {
            return Ok(None);
        };
        if !head.starts_with("HTTP/1.1 200") {
            return Ok(None);
        }
        Ok(serde_json::from_str(body).ok())
    };
    matches!(probe(), Ok(Some(value))
        if value["app"] == "ke-truyen-local"
            && value["data_directory"].as_str().is_some_and(|dir| identity(dir) == identity(&resolved(data_dir))))
}

fn open_window(url: &str) {
    let program_files =
        std::env::var("ProgramFiles").unwrap_or_else(|_| r"C:\Program Files".into());
    let program_files_x86 =
        std::env::var("ProgramFiles(x86)").unwrap_or_else(|_| r"C:\Program Files (x86)".into());
    let browsers = [
        PathBuf::from(program_files).join(r"Google\Chrome\Application\chrome.exe"),
        PathBuf::from(program_files_x86).join(r"Microsoft\Edge\Application\msedge.exe"),
    ];
    let result = match browsers.iter().find(|path| path.is_file()) {
        Some(browser) => std::process::Command::new(browser)
            .arg(format!("--app={url}"))
            .arg("--new-window")
            .spawn(),
        None => std::process::Command::new("explorer").arg(url).spawn(),
    };
    if let Err(error) = result {
        report(&format!(
            "Không mở được cửa sổ ứng dụng: {error}. Hãy mở {url} trong trình duyệt."
        ));
    }
}

/// Write to an attached stderr; a double-clicked GUI app has none, so show a message box.
pub fn report(message: &str) {
    if has_stderr() {
        eprintln!("{message}");
        return;
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::UI::WindowsAndMessaging::{MB_ICONERROR, MessageBoxW};
        let text: Vec<u16> = message.encode_utf16().chain([0]).collect();
        let title: Vec<u16> = "Kệ Truyện".encode_utf16().chain([0]).collect();
        // SAFETY: both buffers are NUL-terminated UTF-16 and outlive the synchronous call.
        unsafe {
            MessageBoxW(
                std::ptr::null_mut(),
                text.as_ptr(),
                title.as_ptr(),
                MB_ICONERROR,
            )
        };
    }
}

#[cfg(windows)]
fn has_stderr() -> bool {
    use windows_sys::Win32::System::Console::{GetStdHandle, STD_ERROR_HANDLE};
    // SAFETY: GetStdHandle only reads the process's standard-handle table.
    let handle = unsafe { GetStdHandle(STD_ERROR_HANDLE) };
    !handle.is_null() && handle != windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE
}

#[cfg(not(windows))]
fn has_stderr() -> bool {
    true
}

/// Entry point for production mode; returns the process exit code.
pub fn run(args: impl Iterator<Item = String>) -> i32 {
    let options = match parse(args) {
        Ok(options) => options,
        Err(message) => {
            report(&message);
            return 2;
        }
    };
    match launch(options) {
        Ok(code) => code,
        Err(error) => {
            report(&format!("Không mở được Kệ Truyện: {error}"));
            1
        }
    }
}

fn launch(options: Options) -> Result<i32, Box<dyn std::error::Error + Send + Sync>> {
    let project = project_dir()?;
    let data_dir = options
        .data_dir
        .clone()
        .unwrap_or_else(|| project.join("user-data"));
    let query = options.book.as_deref().map_or(String::new(), |book| {
        format!("/?book={}", crate::http::quote(book, true))
    });
    let reuse = options.import_backup.is_none() && options.import_qt.is_none();
    let show = |port: u16| {
        if !options.no_browser {
            open_window(&format!("http://127.0.0.1:{port}{query}"));
        }
    };
    if reuse && running_instance(&data_dir, options.port) {
        show(options.port);
        return Ok(0);
    }
    // Retry while a previous server for this library finishes shutting down, for ~20 s as run.py.
    let deadline = std::time::Instant::now() + Duration::from_secs(20);
    let (listener, mut library) = loop {
        let bound = TcpListener::bind(("127.0.0.1", options.port));
        let port_busy = bound.is_err();
        let opened = bound.map_err(ApiError::from).and_then(|listener| {
            Library::open_production(&data_dir).map(|library| (listener, library))
        });
        match opened {
            Ok(pair) => break pair,
            Err(error) => {
                // Only a busy port can belong to a running instance of this library.
                if reuse && port_busy && running_instance(&data_dir, options.port) {
                    show(options.port);
                    return Ok(0);
                }
                if !reuse || std::time::Instant::now() >= deadline {
                    report(&format!(
                        "Không mở được Kệ Truyện. Cổng/thư viện đang được dùng hoặc máy chủ cũ chưa tắt xong: {}",
                        error.message
                    ));
                    return Ok(1);
                }
                std::thread::sleep(Duration::from_millis(250));
            }
        }
    };
    library.sources.enable_native()?;
    if let Some(backup) = &options.import_backup {
        let result = library.import_backup(backup)?;
        report(&format!(
            "Khôi phục {} truyện, {} mục chương.",
            result["books"], result["chapters"]
        ));
    }
    if let Some(pack) = &options.import_qt {
        crate::qt_pack::install(&library, pack)?;
    }
    show(listener.local_addr()?.port());
    crate::http::serve_library(library, &project.join("web"), listener)?;
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identity_matches_python_normcase() {
        assert_eq!(
            identity(r"\\?\C:\Users\A\Truyện\user-data\"),
            identity("c:/users/a/truyện/user-data")
        );
        assert_ne!(identity(r"C:\a\user-data"), identity(r"C:\a\user-data2"));
    }
}
