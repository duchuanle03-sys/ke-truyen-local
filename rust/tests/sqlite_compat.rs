use rusqlite::{Connection, functions::FunctionFlags};
mod common;
#[test]
fn exact_current_schema_and_external_fts_triggers() {
    let fixture = common::Fixture::new("small");
    let marker: serde_json::Value = serde_json::from_slice(
        &std::fs::read(fixture.root.join("fixture/m1-fixture.json")).unwrap(),
    )
    .unwrap();
    let db = Connection::open_in_memory().unwrap();
    db.create_scalar_function(
        "shelf_fold",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| Ok(reader_local::casefold::casefold(&ctx.get::<String>(0)?)),
    )
    .unwrap();
    let schema = marker["schema"].as_array().unwrap();
    let mut executed = 0;
    for kind in ["table", "index", "trigger"] {
        for object in schema {
            if object[0] != kind {
                continue;
            }
            let name = object[1].as_str().unwrap();
            let Some(sql) = object[3].as_str() else {
                continue;
            };
            if name == "sqlite_sequence" || name.starts_with("chapter_search_fts_") {
                continue;
            }
            db.execute_batch(sql)
                .unwrap_or_else(|e| panic!("{name}: {e}; {sql}"));
            executed += 1;
        }
    }
    assert!(executed > 40, "full current schema required");
    db.execute("INSERT INTO books(id,name,added_at,updated_at,groups_json) VALUES('b','Straße',1,1,'[\"one\"]')",[]).unwrap();
    assert_eq!(
        db.query_row(
            "SELECT group_id FROM book_groups WHERE book_id='b'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "one"
    );
    db.execute("UPDATE books SET groups_json='[\"two\"]' WHERE id='b'", [])
        .unwrap();
    assert_eq!(
        db.query_row(
            "SELECT group_id FROM book_groups WHERE book_id='b'",
            [],
            |r| r.get::<_, String>(0)
        )
        .unwrap(),
        "two"
    );
    db.execute(
        "INSERT INTO chapters(book_id,idx,title) VALUES('b',0,'Chapter')",
        [],
    )
    .unwrap();
    db.execute("INSERT INTO chapter_search_docs(book_id,idx,scope,literal_check,text) VALUES('b',0,'original',0,'AbcDef')",[]).unwrap();
    let count = |term: &str| {
        db.query_row(
            "SELECT count(*) FROM chapter_search_fts WHERE chapter_search_fts MATCH ?",
            [term],
            |r| r.get::<_, i64>(0),
        )
        .unwrap()
    };
    assert_eq!(count("Abc"), 1);
    assert_eq!(count("abc"), 0);
    db.execute("UPDATE chapter_search_docs SET text='ChangedText'", [])
        .unwrap();
    assert_eq!(count("Abc"), 0);
    assert_eq!(count("Changed"), 1);
    db.execute("DELETE FROM chapter_search_docs", []).unwrap();
    assert_eq!(count("Changed"), 0);
}
#[test]
fn sqlite_features_match_persisted_contract() {
    let db = Connection::open_in_memory().unwrap();
    db.create_scalar_function(
        "shelf_fold",
        1,
        FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| Ok(reader_local::casefold::casefold(&ctx.get::<String>(0)?)),
    )
    .unwrap();
    db.execute_batch("CREATE TABLE b(id INTEGER PRIMARY KEY,name TEXT,raw TEXT, folded TEXT GENERATED ALWAYS AS(shelf_fold(name)) STORED); CREATE INDEX fold ON b(shelf_fold(name)); CREATE TABLE g(value TEXT); CREATE TRIGGER groups AFTER INSERT ON b BEGIN INSERT INTO g SELECT value FROM json_each(NEW.raw) WHERE type='text'; END; CREATE VIRTUAL TABLE f USING fts5(value,tokenize='trigram case_sensitive 1'); INSERT INTO b(name,raw) VALUES('Straße','[\"one\"]'); INSERT INTO f VALUES('AbcDef');").unwrap();
    assert_eq!(
        db.query_row("SELECT folded FROM b", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "strasse"
    );
    assert_eq!(
        db.query_row("SELECT value FROM g", [], |r| r.get::<_, String>(0))
            .unwrap(),
        "one"
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM f WHERE f MATCH 'Abc'", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        1
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM f WHERE f MATCH 'abc'", [], |r| r
            .get::<_, i64>(0))
            .unwrap(),
        0
    );
    println!("SQLite {}", rusqlite::version());
}
