mod common;
use std::io::{BufRead, Read, Write};
struct ServerProcess(std::process::Child);
impl Drop for ServerProcess {
    fn drop(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }
}
#[cfg(windows)]
#[test]
fn static_junction_escape_is_forbidden() {
    let fixture = common::Fixture::new("empty");
    let web = fixture.root.join("web");
    let outside = fixture.root.join("outside");
    std::fs::create_dir(&web).unwrap();
    std::fs::create_dir(&outside).unwrap();
    std::fs::write(outside.join("secret.txt"), b"not web content").unwrap();
    let link = web.join("escape");
    let output = std::process::Command::new("cmd")
        .args(["/C", "mklink", "/J"])
        .arg(&link)
        .arg(&outside)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut child = ServerProcess(
        std::process::Command::new(env!("CARGO_BIN_EXE_reader_local"))
            .arg("--fixture")
            .arg(&fixture.db)
            .arg("--web-root")
            .arg(&web)
            .args(["--port", "0"])
            .stdout(std::process::Stdio::piped())
            .spawn()
            .unwrap(),
    );
    let mut line = String::new();
    std::io::BufReader::new(child.0.stdout.take().unwrap())
        .read_line(&mut line)
        .unwrap();
    let startup: serde_json::Value = serde_json::from_str(&line).unwrap();
    let port = startup["port"].as_u64().unwrap();
    let mut stream = std::net::TcpStream::connect(("127.0.0.1", port as u16)).unwrap();
    stream
        .set_read_timeout(Some(std::time::Duration::from_secs(5)))
        .unwrap();
    write!(
        stream,
        "GET /escape/secret.txt HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nConnection: close\r\n\r\n"
    )
    .unwrap();
    let mut response = String::new();
    stream.read_to_string(&mut response).unwrap();
    assert!(response.starts_with("HTTP/1.1 403"), "{response}");
    assert!(!response.contains("not web content"));
    drop(child);
    std::fs::remove_dir(&link).unwrap();
    assert!(outside.join("secret.txt").exists());
}
