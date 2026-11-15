mod common;
use reader_local::{Library, ShelfQuery};
#[test]
fn bounded_large_shelf() {
    if let Ok(path) = std::env::var("M1_SCALE_CHILD") {
        let library = Library::open_fixture(std::path::Path::new(&path)).unwrap();
        let start = std::time::Instant::now();
        let page = library
            .list_books(&ShelfQuery::from_uri("/api/books").unwrap())
            .unwrap();
        let mut value = serde_json::to_value(page).unwrap();
        reader_local::http::public_content(&mut value).unwrap();
        let bytes = reader_local::http::json_bytes(&value);
        println!(
            "M1_SAMPLE {}",
            serde_json::json!({"elapsed_ms":start.elapsed().as_secs_f64()*1000.0,"json_bytes":bytes.len()})
        );
        return;
    }
    let fixture = common::Fixture::new("scale");
    let library = Library::open_fixture(&fixture.db).unwrap();
    let q = ShelfQuery::from_uri("/api/books").unwrap();
    let mut timings = Vec::new();
    let mut json_bytes = 0;
    for _ in 0..31 {
        let start = std::time::Instant::now();
        let page = library.list_books(&q).unwrap();
        assert_eq!(page.items.len(), 60);
        assert_eq!(page.total, 50000);
        let mut value = serde_json::to_value(page).unwrap();
        reader_local::http::public_content(&mut value).unwrap();
        json_bytes = reader_local::http::json_bytes(&value).len();
        timings.push(start.elapsed().as_secs_f64() * 1000.0);
    }
    timings.remove(0);
    let raw = timings.clone();
    timings.sort_by(f64::total_cmp);
    println!(
        "books=50000 cached_bodies=3 page=60 samples=30 direct_query_publicJSON_ms p50={} p95={} max={} json_bytes={} raw={:?}",
        (timings[14] + timings[15]) / 2.0,
        timings[28],
        timings[29],
        json_bytes,
        raw
    );
    for _ in 0..3 {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args(["--exact", "bounded_large_shelf", "--nocapture"])
            .env("M1_SCALE_CHILD", &fixture.db)
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
        for line in String::from_utf8_lossy(&output.stdout)
            .lines()
            .filter(|s| s.starts_with("M1_SAMPLE "))
        {
            println!("process_cold_direct_query_publicJSON {line}");
        }
    }
    let db = rusqlite::Connection::open_with_flags(
        &fixture.db,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
    )
    .unwrap();
    db.create_scalar_function(
        "shelf_fold",
        1,
        rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| Ok(reader_local::casefold::casefold(&ctx.get::<String>(0)?)),
    )
    .unwrap();
    let plan:Vec<String>=db.prepare("EXPLAIN QUERY PLAN SELECT * FROM books WHERE (trashed_at>0)=0 AND archived=0 ORDER BY read_at DESC,name DESC,id DESC LIMIT 60 OFFSET 0").unwrap().query_map([],|r|r.get(3)).unwrap().map(Result::unwrap).collect();
    assert!(
        plan.iter().any(|s| s.contains("books_shelf_recent")),
        "{plan:?}"
    );
    assert!(!plan.iter().any(|s| s.contains("TEMP B-TREE")), "{plan:?}");
    println!("plan={plan:?}");
}
