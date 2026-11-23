use std::process::Command;

#[test]
fn chapter_and_image_cache_contract_matches_python() {
    let result = Command::new("python")
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .args([
            "-B",
            "-X",
            "utf8",
            "rust/tests/support/m3_cache.py",
            "--binary",
        ])
        .arg(env!("CARGO_BIN_EXE_reader_local"))
        .output()
        .expect("Python reference must be available");
    assert!(
        result.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}
