use std::process::Command;

#[test]
fn qt_wire_translation_grammar_and_storage_match_python() {
    let output = Command::new("python")
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .args([
            "-B",
            "-X",
            "utf8",
            "rust/tests/support/m4_contract.py",
            "--binary",
        ])
        .arg(env!("CARGO_BIN_EXE_reader_local"))
        .output()
        .expect("Python reference must be available");
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}
