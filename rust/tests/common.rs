#![allow(dead_code)]
use std::{
    path::PathBuf,
    process::Command,
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
pub struct Fixture {
    pub root: PathBuf,
    pub db: PathBuf,
}
impl Fixture {
    pub fn new(profile: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "reader-m1-rust-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        std::fs::create_dir(&root).unwrap();
        let script = "import sys,pathlib;sys.path.insert(0,str(pathlib.Path('rust/tests/support').resolve()));import capture_reference as h;h.fixture(pathlib.Path(sys.argv[1])/'fixture',sys.argv[2])";
        let output = Command::new("python")
            .current_dir(PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap())
            .args(["-B", "-X", "utf8", "-c", script])
            .arg(&root)
            .arg(profile)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        let db = root.join("fixture/library.sqlite3");
        Self { root, db }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
pub fn python(script: &str, args: &[&str]) -> String {
    let output = Command::new("python")
        .current_dir(PathBuf::from(env!("CARGO_MANIFEST_DIR")).parent().unwrap())
        .args(["-B", "-X", "utf8", "-c", script])
        .args(args)
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap()
}
