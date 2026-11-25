//! Production launcher contract (replaces Python `run.py`): first run, instance reuse,
//! lock and port refusal, `--book` validation and Unicode data paths.
use std::{
    io::{BufRead, BufReader, Read, Write},
    net::{TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Output, Stdio},
    sync::atomic::{AtomicU64, Ordering},
    time::{Duration, Instant},
};

static NEXT: AtomicU64 = AtomicU64::new(0);

/// A scratch directory removed when the test ends (declared before servers, so dropped after them).
struct Temp(PathBuf);
impl Temp {
    fn join(&self, part: &str) -> PathBuf {
        self.0.join(part)
    }
}
impl Drop for Temp {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.0);
    }
}

fn temp(name: &str) -> Temp {
    let dir = std::env::temp_dir().join(format!(
        "reader-m7-launcher-{name}-{}-{}",
        std::process::id(),
        NEXT.fetch_add(1, Ordering::Relaxed)
    ));
    let _ = std::fs::remove_dir_all(&dir);
    Temp(dir)
}

fn free_port() -> u16 {
    TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

fn launcher(args: &[&str]) -> Command {
    let mut command = Command::new(env!("CARGO_BIN_EXE_reader_local"));
    command.arg("--no-browser").args(args).stdin(Stdio::null());
    command
}

struct Server(Child, u16);
impl Drop for Server {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}

fn start(data: &Path, port: u16) -> Server {
    let mut child = launcher(&[
        "--data-dir",
        data.to_str().unwrap(),
        "--port",
        &port.to_string(),
    ])
    .stdout(Stdio::piped())
    .stderr(Stdio::null())
    .spawn()
    .unwrap();
    let mut line = String::new();
    BufReader::new(child.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let hello: serde_json::Value = serde_json::from_str(&line).expect("startup JSON line");
    assert_eq!(hello["port"], port);
    Server(child, port)
}

fn get(port: u16, path: &str) -> serde_json::Value {
    let mut stream = TcpStream::connect(("127.0.0.1", port)).unwrap();
    write!(
        stream,
        "GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    )
    .unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    let body = response.split_once("\r\n\r\n").expect("HTTP response").1;
    serde_json::from_str(body).expect("JSON body")
}

fn finish(command: &mut Command, limit: Duration) -> (Output, Duration) {
    let started = Instant::now();
    let mut child = command
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    while child.try_wait().unwrap().is_none() {
        assert!(
            started.elapsed() < limit,
            "launcher did not exit within {limit:?}"
        );
        std::thread::sleep(Duration::from_millis(50));
    }
    (child.wait_with_output().unwrap(), started.elapsed())
}

#[test]
fn first_run_creates_library_and_serves_shelf() {
    let scratch = temp("first");
    let data = scratch.join("user-data");
    let server = start(&data, free_port());
    assert_eq!(get(server.1, "/api/books")["total"], 0);
    assert!(data.join("library.sqlite3").is_file());
}

#[test]
fn second_launch_reuses_running_instance() {
    let scratch = temp("reuse");
    let data = scratch.join("user-data");
    let mut server = start(&data, free_port());
    let (output, elapsed) = finish(
        &mut launcher(&[
            "--data-dir",
            data.to_str().unwrap(),
            "--port",
            &server.1.to_string(),
        ]),
        Duration::from_secs(10),
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(elapsed < Duration::from_secs(5), "reuse took {elapsed:?}");
    assert!(
        server.0.try_wait().unwrap().is_none(),
        "first instance must keep running"
    );
}

#[test]
fn unicode_data_path_is_recognised_for_reuse() {
    let scratch = temp("unicode");
    let data = scratch.join("truyện thử").join("user-data");
    let server = start(&data, free_port());
    let (output, _) = finish(
        &mut launcher(&[
            "--data-dir",
            data.to_str().unwrap(),
            "--port",
            &server.1.to_string(),
        ]),
        Duration::from_secs(10),
    );
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let identity = get(server.1, "/api/instance");
    assert!(
        identity["data_directory"]
            .as_str()
            .unwrap()
            .contains("truyện thử")
    );
}

#[test]
fn library_held_by_another_server_is_refused() {
    let scratch = temp("held");
    let data = scratch.join("user-data");
    let _owner = start(&data, free_port());
    let (output, _) = finish(
        &mut launcher(&[
            "--data-dir",
            data.to_str().unwrap(),
            "--port",
            &free_port().to_string(),
        ]),
        Duration::from_secs(60),
    );
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("Không mở được Kệ Truyện"));
}

#[test]
fn foreign_port_is_not_reused() {
    let foreign = TcpListener::bind("127.0.0.1:0").unwrap();
    let port = foreign.local_addr().unwrap().port();
    let scratch = temp("foreign");
    let data = scratch.join("user-data");
    let (output, _) = finish(
        &mut launcher(&[
            "--data-dir",
            data.to_str().unwrap(),
            "--port",
            &port.to_string(),
        ]),
        Duration::from_secs(60),
    );
    assert_eq!(output.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&output.stderr).contains("Không mở được Kệ Truyện"));
}

#[test]
fn book_argument_is_validated() {
    let scratch = temp("book");
    let data = scratch.join("user-data");
    let long = "x".repeat(257);
    for book in ["", long.as_str(), "a\u{1}b"] {
        let (output, _) = finish(
            &mut launcher(&["--data-dir", data.to_str().unwrap(), "--book", book]),
            Duration::from_secs(10),
        );
        assert_eq!(output.status.code(), Some(2), "{book:?}");
        assert!(String::from_utf8_lossy(&output.stderr).contains("Mã truyện không hợp lệ."));
    }
    assert!(
        !data.exists(),
        "argument errors must not create the data directory"
    );
}
