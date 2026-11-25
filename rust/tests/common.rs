#![allow(dead_code)]
use std::{
    path::{Path, PathBuf},
    sync::atomic::{AtomicU64, Ordering},
};
static NEXT: AtomicU64 = AtomicU64::new(0);
pub struct Fixture {
    pub root: PathBuf,
    pub db: PathBuf,
}
impl Fixture {
    /// A private temp copy of a frozen fixture (`small`, `empty`), or `scale`: 50,000 books.
    pub fn new(profile: &str) -> Self {
        let root = std::env::temp_dir().join(format!(
            "reader-m1-rust-{}-{}",
            std::process::id(),
            NEXT.fetch_add(1, Ordering::Relaxed)
        ));
        let frozen = Path::new(env!("CARGO_MANIFEST_DIR"))
            .join("tests/fixtures")
            .join(if profile == "scale" { "empty" } else { profile });
        let fixture = root.join("fixture");
        std::fs::create_dir_all(&fixture).unwrap();
        for name in ["library.sqlite3", "m1-fixture.json"] {
            std::fs::copy(frozen.join(name), fixture.join(name)).unwrap();
        }
        let db = fixture.join("library.sqlite3");
        if profile == "scale" {
            let mut connection = rusqlite::Connection::open(&db).unwrap();
            // The fixture's names are ASCII/Vietnamese, where casefold equals lowercase.
            connection
                .create_scalar_function(
                    "shelf_fold",
                    1,
                    rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
                    |ctx| Ok(ctx.get::<String>(0)?.to_lowercase()),
                )
                .unwrap();
            let tx = connection.transaction().unwrap();
            {
                let mut book = tx
                    .prepare(
                        "INSERT INTO books(id,name,author,source,kind,groups_json,added_at,updated_at,read_at) \
                         VALUES(?1,?2,'Tác giả',?3,'text',?4,?5,?5,?5)",
                    )
                    .unwrap();
                for i in 0..50_000 {
                    let source = if i % 2 == 1 { "hako" } else { "file" };
                    book.execute(rusqlite::params![
                        format!("{i:05}"),
                        format!("TRUYỆN {i:05}"),
                        source,
                        format!("[\"group-{}\"]", i % 100),
                        i
                    ])
                    .unwrap();
                }
                for i in 0..3 {
                    tx.execute(
                        "INSERT INTO chapters(book_id,idx,text,html,downloaded) VALUES(?1,0,'Cached text.','<p>Cached text.</p>',1)",
                        [format!("{i:05}")],
                    )
                    .unwrap();
                }
            }
            tx.commit().unwrap();
        }
        Self { root, db }
    }
}
impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}
/// Answers the retired Python reference gave for the inputs these tests assert.
pub fn reference() -> &'static serde_json::Value {
    static VALUE: std::sync::OnceLock<serde_json::Value> = std::sync::OnceLock::new();
    VALUE.get_or_init(|| {
        serde_json::from_str(include_str!("fixtures/python_reference.json")).unwrap()
    })
}
