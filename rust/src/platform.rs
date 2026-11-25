//! Windows PDF/OCR and Desktop shortcut integration through the unchanged
//! PowerShell helpers in `windows/` (references: `pdfocr.py`, `shortcuts.py`).

use crate::{ApiError, documents::html_escape};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::{
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Mutex,
    time::{Duration, Instant},
};

const MAX_PDF_BYTES: u64 = 128 * 1024 * 1024;
const MAX_COVER_BYTES: usize = 16 * 1024 * 1024;
static CREATE_LOCK: Mutex<()> = Mutex::new(());

fn error(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message)
}

fn powershell() -> PathBuf {
    let root = std::env::var_os("SystemRoot").unwrap_or_else(|| r"C:\Windows".into());
    PathBuf::from(root).join(r"System32\WindowsPowerShell\v1.0\powershell.exe")
}

struct Completed {
    status: i32,
    stdout: Vec<u8>,
    stderr: Vec<u8>,
}

/// Run a helper without a console window and kill it after `timeout`.
fn run(mut command: Command, timeout: Duration) -> std::io::Result<Completed> {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(0x0800_0000);
    }
    let mut child = command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    let mut stdout = child.stdout.take().expect("piped stdout");
    let mut stderr = child.stderr.take().expect("piped stderr");
    let out = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stdout.read_to_end(&mut buffer);
        buffer
    });
    let err = std::thread::spawn(move || {
        let mut buffer = Vec::new();
        let _ = stderr.read_to_end(&mut buffer);
        buffer
    });
    let deadline = Instant::now() + timeout;
    let status = loop {
        if let Some(status) = child.try_wait()? {
            break status;
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                format!("timed out after {} seconds", timeout.as_secs()),
            ));
        }
        std::thread::sleep(Duration::from_millis(25));
    };
    Ok(Completed {
        status: status.code().unwrap_or(1),
        stdout: out.join().unwrap_or_default(),
        stderr: err.join().unwrap_or_default(),
    })
}

fn tail(bytes: &[u8]) -> String {
    let text = String::from_utf8_lossy(bytes);
    let count = text.chars().count();
    text.chars().skip(count.saturating_sub(1200)).collect()
}

fn helper_json(completed: &Completed) -> Option<Value> {
    let text = std::str::from_utf8(&completed.stdout).ok()?;
    serde_json::from_str(text.strip_prefix('\u{feff}').unwrap_or(text)).ok()
}

fn value_text(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => crate::http::py_value_repr(other),
    }
}

// ---------------------------------------------------------------- PDF / OCR

fn invoke_ocr(
    script: &Path,
    action: &str,
    path: Option<&Path>,
    language: Option<&str>,
) -> Result<Value, ApiError> {
    if !cfg!(windows) {
        return Err(error(
            "PDF/OCR WinRT chỉ có trên Windows; chưa có engine cho hệ điều hành này.",
        ));
    }
    let mut command = Command::new(powershell());
    command
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
        .arg(script)
        .args(["-Action", action]);
    if let Some(path) = path {
        command.arg("-Path").arg(path);
    }
    if let Some(language) = language.filter(|l| !l.is_empty()) {
        command.args(["-Language", language]);
    }
    let timeout = Duration::from_secs(if action == "Capabilities" { 30 } else { 600 });
    let completed = run(command, timeout)
        .map_err(|e| error(format!("Không chạy được engine Windows PDF/OCR: {e}")))?;
    let Some(result) = helper_json(&completed) else {
        return Err(error(format!(
            "Engine Windows PDF/OCR không trả kết quả hợp lệ. {}",
            tail(&completed.stderr)
        )));
    };
    let failure = match &result {
        Value::Object(map) => map.get("error").filter(|e| truthy(e)).map(value_text),
        _ => Some("invalid payload".into()),
    };
    if completed.status != 0 || failure.is_some() {
        let detail = failure.unwrap_or_else(|| {
            result
                .get("error")
                .map_or_else(|| "engine failed".into(), value_text)
        });
        return Err(error(format!("Windows PDF/OCR: {detail}")));
    }
    Ok(result)
}

fn truthy(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(b) => *b,
        Value::Number(n) => n.as_f64().is_some_and(|v| v != 0.0),
        Value::String(s) => !s.is_empty(),
        Value::Array(a) => !a.is_empty(),
        Value::Object(o) => !o.is_empty(),
    }
}

pub(crate) fn ocr_capabilities(script: &Path) -> Value {
    let mut status = match invoke_ocr(script, "Capabilities", None, None) {
        Ok(Value::Object(map)) => map,
        Ok(_) => unreachable!("helper results are objects"),
        Err(e) => {
            let mut map = Map::new();
            map.insert("pdf_rendering".into(), false.into());
            map.insert("ocr_available".into(), false.into());
            map.insert("languages".into(), json!([]));
            map.insert("error".into(), e.message.into());
            map
        }
    };
    let supported = status.get("pdf_rendering").is_some_and(truthy)
        && status.get("ocr_available").is_some_and(truthy);
    status.insert(
        "engine".into(),
        "Windows.Data.Pdf + Windows.Media.Ocr".into(),
    );
    status.insert("offline".into(), true.into());
    status.insert("supported".into(), supported.into());
    status.insert(
        "limits".into(),
        json!({"file_bytes": MAX_PDF_BYTES, "pages": 500, "render_dimension": 4000, "render_pixels": 16_000_000}),
    );
    status.insert(
        "quality".into(),
        "OCR phụ thuộc chất lượng bản scan và gói ngôn ngữ đã cài; chưa đo độ chính xác trên tài liệu người dùng.".into(),
    );
    Value::Object(status)
}

pub(crate) fn normalise_pages(path: &Path, payload: &Value) -> Result<Value, ApiError> {
    let pages = payload
        .get("pages")
        .and_then(Value::as_array)
        .filter(|p| !p.is_empty() && p.len() <= 500)
        .ok_or_else(|| error("Engine không trả danh sách trang PDF hợp lệ."))?;
    let language = payload
        .get("language")
        .cloned()
        .unwrap_or_else(|| "".into());
    let mut chapters = Vec::new();
    let mut empty = Vec::new();
    for (index, page) in pages.iter().enumerate() {
        let number = page.get("page").and_then(Value::as_f64);
        let Some(text) = page
            .get("text")
            .and_then(Value::as_str)
            .filter(|_| number == Some((index + 1) as f64))
        else {
            return Err(error("Trang OCR thiếu số trang/văn bản hoặc sai thứ tự."));
        };
        let text = crate::documents::strip(text);
        if text.is_empty() {
            empty.push((index + 1).to_string());
        }
        let html = crate::documents::splitlines(text)
            .into_iter()
            .map(|line| format!("<p>{}</p>", html_escape(line)))
            .collect::<String>();
        chapters.push(json!({
            "index": index, "title": format!("Trang {}", index + 1), "html": html, "text": text,
            "images": [], "downloaded": true, "source_page": index + 1,
            "ocr_status": if text.is_empty() { "empty" } else { "recognized" }, "ocr_language": language,
        }));
    }
    let mut warnings = Vec::new();
    if !empty.is_empty() {
        warnings.push(format!(
            "OCR không tìm thấy chữ ở trang: {}. Trang trắng hoặc ngôn ngữ/ảnh không nhận dạng được; mở PDF gốc để kiểm tra.",
            empty.join(", ")
        ));
    }
    let resolved = resolved(path);
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    Ok(json!({
        "book": {
            "name": crate::documents::stem(&name), "author": "", "kind": "text", "format": "pdf", "source": "local",
            "path": resolved, "description": "", "chapter_count": chapters.len(), "warnings": warnings,
            "ocr": {"engine": "Windows.Media.Ocr", "language": language, "pages": chapters.len(),
                    "recognized_pages": chapters.len() - empty.len(),
                    "status": if empty.is_empty() { "complete" } else { "empty_pages" }},
        },
        "chapters": chapters,
    }))
}

fn resolved(path: &Path) -> String {
    let path = std::fs::canonicalize(path)
        .unwrap_or_else(|_| std::path::absolute(path).unwrap_or_else(|_| path.to_owned()));
    let text = path.to_string_lossy();
    text.strip_prefix(r"\\?\").unwrap_or(&text).to_owned()
}

pub(crate) fn extract_pdf(
    script: &Path,
    path: &Path,
    language: Option<&str>,
) -> Result<Value, ApiError> {
    let missing = || error("Không tìm thấy PDF hoặc tệp vượt giới hạn 128 MiB.");
    let path = PathBuf::from(resolved(path));
    if !path.is_file() || path.metadata().map_err(|_| missing())?.len() > MAX_PDF_BYTES {
        return Err(missing());
    }
    let mut head = Vec::new();
    std::fs::File::open(&path)
        .and_then(|f| f.take(1024).read_to_end(&mut head))
        .map_err(|_| missing())?;
    if !head.windows(5).any(|w| w == b"%PDF-") {
        return Err(error("Tệp không có chữ ký PDF hợp lệ."));
    }
    if let Some(language) = language
        && (language.is_empty()
            || language.chars().count() > 64
            || !language
                .chars()
                .all(|c| c.is_ascii_alphanumeric() || c == '-'))
    {
        return Err(error("Mã ngôn ngữ OCR không hợp lệ."));
    }
    normalise_pages(
        &path,
        &invoke_ocr(script, "Extract", Some(&path), language)?,
    )
}

// ---------------------------------------------------------------- Desktop shortcuts

#[cfg(windows)]
pub(crate) fn desktop_directory() -> Result<PathBuf, ApiError> {
    use windows_sys::Win32::{
        System::Com::CoTaskMemFree,
        UI::Shell::{FOLDERID_Desktop, SHGetKnownFolderPath},
    };
    let mut result: windows_sys::core::PWSTR = std::ptr::null_mut();
    // SAFETY: FOLDERID_Desktop is a static GUID; `result` receives a CoTaskMem
    // string that is read once and freed exactly once below, even on failure.
    let status =
        unsafe { SHGetKnownFolderPath(&FOLDERID_Desktop, 0, std::ptr::null_mut(), &mut result) };
    let path = (status >= 0 && !result.is_null()).then(|| {
        // SAFETY: on success `result` is a NUL-terminated UTF-16 string owned by us.
        let length = (0..)
            .take_while(|&i| unsafe { *result.add(i) } != 0)
            .count();
        // SAFETY: `length` units were just read from the live allocation.
        let units = unsafe { std::slice::from_raw_parts(result, length) };
        PathBuf::from(String::from_utf16_lossy(units))
    });
    if !result.is_null() {
        // SAFETY: allocated by SHGetKnownFolderPath and not used afterwards.
        unsafe { CoTaskMemFree(result.cast()) };
    }
    path.filter(|p| !p.as_os_str().is_empty()).ok_or_else(|| {
        error(format!(
            "Không tìm được Desktop Windows (0x{:08X}).",
            status as u32
        ))
    })
}

#[cfg(not(windows))]
pub(crate) fn desktop_directory() -> Result<PathBuf, ApiError> {
    Err(error("Lối tắt Desktop chỉ có trên Windows."))
}

/// `subprocess.list2cmdline`.
pub(crate) fn list2cmdline(args: &[String]) -> String {
    let mut result = String::new();
    for arg in args {
        let mut backslashes = 0;
        if !result.is_empty() {
            result.push(' ');
        }
        let quote = arg.contains(' ') || arg.contains('\t') || arg.is_empty();
        if quote {
            result.push('"');
        }
        for c in arg.chars() {
            match c {
                '\\' => backslashes += 1,
                '"' => {
                    result.push_str(&"\\".repeat(backslashes * 2));
                    backslashes = 0;
                    result.push_str("\\\"");
                }
                c => {
                    result.push_str(&"\\".repeat(backslashes));
                    backslashes = 0;
                    result.push(c);
                }
            }
        }
        result.push_str(&"\\".repeat(backslashes));
        if quote {
            result.push_str(&"\\".repeat(backslashes));
            result.push('"');
        }
    }
    result
}

pub(crate) fn safe_title(title: &str) -> String {
    let replaced: String = title
        .chars()
        .map(|c| {
            if "<>:\"/\\|?*".contains(c) || (c as u32) < 32 {
                '_'
            } else {
                c
            }
        })
        .collect();
    let trimmed: String = replaced.trim_matches([' ', '.']).chars().take(80).collect();
    let mut safe = trimmed.trim_end_matches([' ', '.']).to_owned();
    if safe.is_empty() {
        safe = "Truyện".into();
    }
    let units: Vec<u16> = safe.encode_utf16().take(80).collect();
    let mut decoded = String::new();
    for c in char::decode_utf16(units).flatten() {
        decoded.push(c);
    }
    let mut safe = decoded.trim_end_matches([' ', '.']).to_owned();
    if safe.is_empty() {
        safe = "Truyện".into();
    }
    let base = safe
        .split('.')
        .next()
        .unwrap_or_default()
        .to_ascii_uppercase();
    let reserved = matches!(base.as_str(), "CON" | "PRN" | "AUX" | "NUL")
        || ["COM", "LPT"].iter().any(|p| {
            base.strip_prefix(p).is_some_and(|rest| {
                let mut chars = rest.chars();
                matches!(
                    (chars.next(), chars.next()),
                    (Some('1'..='9' | '¹' | '²' | '³'), None)
                )
            })
        });
    if reserved { format!("_{safe}") } else { safe }
}

pub(crate) struct Launcher {
    pub target: PathBuf,
    pub prefix: Vec<String>,
    pub working_directory: PathBuf,
}

/// Book shortcuts start this app directly; it reuses a running instance of the same library.
pub(crate) fn launcher(project: &Path) -> Result<Launcher, ApiError> {
    let exe = std::env::current_exe()
        .map_err(|e| error(format!("Không xác định được đường dẫn ứng dụng: {e}")))?;
    Ok(Launcher {
        target: PathBuf::from(resolved(&exe)),
        prefix: Vec::new(),
        working_directory: project.to_owned(),
    })
}

#[allow(clippy::too_many_arguments)]
pub(crate) fn create_shortcut(
    script: &Path,
    launcher: &Launcher,
    book_id: &str,
    title: &str,
    cover: &[u8],
    data_dir: &Path,
    port: u16,
    desktop: Option<&Path>,
) -> Result<Value, ApiError> {
    if !cfg!(windows) {
        return Err(error("Lối tắt Desktop chỉ có trên Windows."));
    }
    if book_id.is_empty()
        || book_id.chars().count() > 256
        || book_id.chars().any(|c| (c as u32) < 32)
        || title.chars().count() > 2048
        || port == 0
    {
        return Err(error("Thông tin truyện hoặc cổng lối tắt không hợp lệ."));
    }
    let library = PathBuf::from(resolved(data_dir));
    let library_text = library.to_string_lossy().into_owned();
    if library_text.contains('\0') {
        return Err(error("Tên truyện hoặc đường dẫn dữ liệu không hợp lệ."));
    }
    let normcase = library_text.replace('/', "\\").to_lowercase();
    let identity = format!(
        "{:x}",
        Sha256::digest(format!("{normcase}\0{book_id}").as_bytes())
    )[..16]
        .to_owned();
    let mut args = launcher.prefix.clone();
    args.extend([
        format!("--book={book_id}"),
        format!("--data-dir={library_text}"),
        format!("--port={port}"),
    ]);
    let arguments = list2cmdline(&args);
    let assets = library.join("shortcuts");
    let os_error = |e: std::io::Error| error(format!("Không ghi được lối tắt Windows: {e}"));
    let _guard = CREATE_LOCK.lock().unwrap_or_else(|p| p.into_inner());
    std::fs::create_dir_all(&assets).map_err(os_error)?;
    let desktop = match desktop {
        Some(folder) => folder.to_owned(),
        None => desktop_directory()?,
    };
    let mut token = [0u8; 8];
    getrandom::fill(&mut token).map_err(|e| error(e.to_string()))?;
    let folder = assets.join(format!(
        ".create-{}",
        token.iter().map(|b| format!("{b:02x}")).collect::<String>()
    ));
    std::fs::create_dir(&folder).map_err(os_error)?;
    struct Cleanup(PathBuf);
    impl Drop for Cleanup {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    let _cleanup = Cleanup(folder.clone());
    let cover_path = if !cover.is_empty() && cover.len() <= MAX_COVER_BYTES {
        let path = folder.join("cover");
        std::fs::write(&path, cover).map_err(os_error)?;
        path.to_string_lossy().into_owned()
    } else {
        String::new()
    };
    let input = folder.join("input.json");
    let payload = json!({
        "desktop": desktop.to_string_lossy(), "filename": format!("{} - {identity}", safe_title(title)),
        "identity": identity, "target": launcher.target.to_string_lossy(), "arguments": arguments,
        "working_directory": launcher.working_directory.to_string_lossy(), "cover_path": cover_path,
        "icon_path": assets.join(format!("{identity}.ico")).to_string_lossy(), "temporary": folder.to_string_lossy(),
    });
    std::fs::write(&input, serde_json::to_vec(&payload).expect("JSON")).map_err(os_error)?;
    let mut command = Command::new(powershell());
    command
        .args(["-NoLogo", "-NoProfile", "-NonInteractive", "-File"])
        .arg(script)
        .arg("-InputPath")
        .arg(&input);
    let completed = run(command, Duration::from_secs(30)).map_err(os_error)?;
    let Some(result) = helper_json(&completed) else {
        return Err(error(format!(
            "Windows không trả kết quả lối tắt hợp lệ: {}",
            tail(&completed.stderr)
        )));
    };
    let failure = match &result {
        Value::Object(map) => map.get("error").filter(|e| truthy(e)).map(value_text),
        _ => Some("invalid payload".into()),
    };
    if completed.status != 0 || failure.is_some() {
        let mut detail = failure.unwrap_or_else(|| "engine failed".into());
        if let Some(code) = result.get("hresult").filter(|h| truthy(h)) {
            detail.push_str(&format!(" ({})", value_text(code)));
        }
        return Err(error(format!("Không tạo được lối tắt Windows: {detail}")));
    }
    match (
        result.get("path").and_then(Value::as_str),
        result.get("used_cover").and_then(Value::as_bool),
    ) {
        (Some(path), Some(used)) if Path::new(path).is_file() => {
            Ok(json!({"path": path, "used_cover": used}))
        }
        _ => Err(error("Windows chưa tạo tệp lối tắt hợp lệ.")),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn list2cmdline_matches_subprocess() {
        let args = [
            r"C:\a b\run.py",
            "--book=x\"y",
            r"--data-dir=C:\data dir\",
            "",
            r#"a\"b"#,
        ]
        .map(String::from);
        assert_eq!(
            list2cmdline(&args),
            r#""C:\a b\run.py" --book=x\"y "--data-dir=C:\data dir\\" "" a\\\"b"#
        );
    }

    #[test]
    fn safe_title_matches_reference_rules() {
        assert_eq!(safe_title(" a<b>.. "), "a_b_");
        assert_eq!(safe_title("..."), "Truyện");
        assert_eq!(safe_title("con.txt"), "_con.txt");
        assert_eq!(safe_title("COM²"), "_COM²");
        assert_eq!(safe_title("COM10"), "COM10");
        assert_eq!(safe_title(&"x".repeat(100)).len(), 80);
    }

    /// Real COM shortcut and System.Drawing icon through the unchanged helper,
    /// compared with the Python reference; both write into private temp desktops.
    #[cfg(windows)]
    #[test]
    fn shortcut_targets_rust_launcher() {
        use base64::Engine;
        let png = base64::engine::general_purpose::STANDARD
            .decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
            .unwrap();
        let project = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_owned();
        let base = std::env::temp_dir().join(format!("reader-m6-shortcut-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&base);
        let (data, cover) = (base.join("data"), base.join("cover.png"));
        std::fs::create_dir_all(&data).unwrap();
        std::fs::write(&cover, &png).unwrap();
        let title = "Truyện: thử/1 <b>";
        let script = "import json,pathlib,sys;sys.path.insert(0,sys.argv[1]);import shortcuts;\
            shortcuts.desktop_directory=lambda: pathlib.Path(sys.argv[2]);\
            print(json.dumps(shortcuts.create_shortcut(book_id='book-1',title=sys.argv[3],\
            cover_bytes=pathlib.Path(sys.argv[4]).read_bytes(),data_dir=sys.argv[5],port=8765)))";
        let output = Command::new("python")
            .args(["-B", "-X", "utf8", "-c", script])
            .arg(&project)
            .arg(base.join("desktop-python"))
            .arg(title)
            .arg(&cover)
            .arg(&data)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let python: Value = serde_json::from_slice(&output.stdout).unwrap();
        let icon = std::fs::read(
            data.join("shortcuts")
                .read_dir()
                .unwrap()
                .next()
                .unwrap()
                .unwrap()
                .path(),
        )
        .unwrap();
        let script_path = project.join("windows").join("shortcut.ps1");
        let launcher = launcher(&project).unwrap();
        let desktop = base.join("desktop-rust");
        let rust = create_shortcut(
            &script_path,
            &launcher,
            "book-1",
            title,
            &png,
            &data,
            8765,
            Some(&desktop),
        )
        .unwrap();
        let name = |v: &Value| {
            Path::new(v["path"].as_str().unwrap())
                .file_name()
                .unwrap()
                .to_owned()
        };
        assert_eq!(name(&rust), name(&python));
        assert_eq!(
            (rust["used_cover"].clone(), python["used_cover"].clone()),
            (json!(true), json!(true))
        );
        // The Rust shortcut launches the running app directly (no pythonw/run.py).
        let link = std::fs::read(rust["path"].as_str().unwrap()).unwrap();
        let text = String::from_utf16_lossy(
            &link
                .as_chunks::<2>()
                .0
                .iter()
                .map(|c| u16::from_le_bytes([c[0], c[1]]))
                .collect::<Vec<_>>(),
        );
        let exe = std::env::current_exe().unwrap();
        assert!(
            text.contains(&*exe.to_string_lossy()),
            "target must be the running app"
        );
        assert!(text.contains("--book=book-1") && text.contains("--port=8765"));
        assert!(!text.contains("pythonw") && !text.contains("run.py"));
        let icons = data
            .join("shortcuts")
            .read_dir()
            .unwrap()
            .filter_map(Result::ok)
            .collect::<Vec<_>>();
        assert_eq!(icons.len(), 1, "temporary folders must be removed");
        assert_eq!(std::fs::read(icons[0].path()).unwrap(), icon);
        let again = create_shortcut(
            &script_path,
            &launcher,
            "book-1",
            title,
            &png,
            &data,
            8765,
            Some(&desktop),
        )
        .unwrap();
        let same = |a: &Value, b: &Value| {
            std::fs::canonicalize(a["path"].as_str().unwrap()).unwrap()
                == std::fs::canonicalize(b["path"].as_str().unwrap()).unwrap()
        };
        assert!(same(&again, &rust));
        let fallback = create_shortcut(
            &script_path,
            &launcher,
            "book-1",
            title,
            b"not an image",
            &data,
            8765,
            Some(&desktop),
        )
        .unwrap();
        assert!(same(&fallback, &rust) && fallback["used_cover"] == json!(false));
        assert_eq!(std::fs::read_dir(&desktop).unwrap().count(), 1);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[test]
    fn normalise_pages_reports_empty_pages() {
        let result = normalise_pages(
            Path::new("scan.pdf"),
            &json!({"language": "vi", "pages": [{"page": 1, "text": " a\nb "}, {"page": 2, "text": ""}]}),
        )
        .unwrap();
        assert_eq!(result["chapters"][0]["html"], "<p>a</p><p>b</p>");
        assert_eq!(result["book"]["ocr"]["status"], "empty_pages");
        assert!(
            normalise_pages(
                Path::new("x.pdf"),
                &json!({"pages": [{"page": 2, "text": ""}]})
            )
            .is_err()
        );
    }
}
