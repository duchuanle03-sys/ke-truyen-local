use std::{path::PathBuf, process::Command};

fn python_gate(script: &str) {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let result = Command::new("python")
        .current_dir(root.parent().unwrap())
        .args(["-B", "-X", "utf8", script, "--binary"])
        .arg(env!("CARGO_BIN_EXE_reader_local"))
        .output()
        .expect("Python oracle must be available");
    assert!(
        result.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&result.stdout),
        String::from_utf8_lossy(&result.stderr)
    );
}

#[test]
fn writable_offline_http_state_and_lifecycle_match_python() {
    python_gate("rust/tests/support/m2_capture.py");
}

#[test]
fn json_depth_state_and_restart_match_python() {
    python_gate("rust/tests/support/m2_depth.py");
}

#[test]
fn rejected_small_bodies_flush_the_reference_response() {
    python_gate("rust/tests/support/m2_rejection.py");
}
