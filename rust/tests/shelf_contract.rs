mod common;
use reader_local::{Library, ShelfQuery};
use serde_json::Value;
#[test]
fn float_json_matches_python() {
    let expected = common::reference()["float_json"].as_str().unwrap();
    let value: Value = serde_json::from_str(expected).unwrap();
    assert_eq!(
        String::from_utf8(reader_local::http::json_bytes(&value)).unwrap(),
        expected
    );
}
#[test]
fn html_tokenizer_matches_python_quoted_attributes() {
    for markup in [
        "<p title=\"1 > 0\">Text <b data-value='x<y'>bold</b></p>",
        "<p><script></p>secret</script></p>",
        "<p>A&amp;B &#65; &#x42;</p>",
    ] {
        let mut actual = serde_json::json!({"html":markup});
        reader_local::http::public_content(&mut actual).unwrap();
        assert_eq!(actual, common::reference()["tokenizer"][markup]);
    }
}
#[test]
fn svg_namespaces_entities_and_whitespace_match_python() {
    use base64::Engine;
    for svg in [
        "<s:svg xmlns:s=\"http://www.w3.org/2000/svg\"><s:rect width=\"10\"/></s:svg>",
        "\n<svg><rect width=\"10\"/></svg>\n",
        "<svg><text>A&amp;B &#65; &#x42;</text></svg>",
    ] {
        let markup = format!(
            "<img src='data:image/svg+xml;base64,{}'>",
            base64::engine::general_purpose::STANDARD.encode(svg)
        );
        let mut actual = serde_json::json!({"html":markup});
        reader_local::http::public_content(&mut actual).unwrap();
        assert_eq!(actual, common::reference()["svg"][svg], "{svg}");
    }
}
#[test]
fn unsupported_metadata_types_match_python() {
    for (raw, status) in [
        ("0", 500),
        ("false", 500),
        ("[\"warnings\"]", 500),
        ("[]", 200),
        ("\"hello\"", 200),
    ] {
        let fixture = common::Fixture::new("small");
        let db = rusqlite::Connection::open(&fixture.db).unwrap();
        db.create_scalar_function(
            "shelf_fold",
            1,
            rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| Ok(reader_local::casefold::casefold(&ctx.get::<String>(0)?)),
        )
        .unwrap();
        db.execute("UPDATE books SET raw=?", [raw]).unwrap();
        drop(db);
        let library = Library::open_fixture(&fixture.db).unwrap();
        let result = library.list_books(&ShelfQuery::from_uri("/api/books").unwrap());
        assert_eq!(result.map_or_else(|e| e.status, |_| 200), status, "{raw}");
    }
}
#[test]
fn wrong_unicode_or_index_definition_fails_closed() {
    let fixture = common::Fixture::new("small");
    let marker_path = fixture.root.join("fixture/m1-fixture.json");
    let mut marker: Value = serde_json::from_slice(&std::fs::read(&marker_path).unwrap()).unwrap();
    marker["unicode"] = "15.0.0".into();
    std::fs::write(&marker_path, serde_json::to_vec(&marker).unwrap()).unwrap();
    assert!(Library::open_fixture(&fixture.db).is_err());
    marker["unicode"] = "16.0.0".into();
    std::fs::write(&marker_path, serde_json::to_vec(&marker).unwrap()).unwrap();
    let db = rusqlite::Connection::open(&fixture.db).unwrap();
    db.execute_batch("DROP INDEX books_shelf_recent;CREATE INDEX books_shelf_recent ON books(id)")
        .unwrap();
    drop(db);
    assert!(Library::open_fixture(&fixture.db).is_err());
    let fixture = common::Fixture::new("small");
    let db = rusqlite::Connection::open(&fixture.db).unwrap();
    db.create_scalar_function(
        "shelf_fold",
        1,
        rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| Ok(reader_local::casefold::casefold(&ctx.get::<String>(0)?)),
    )
    .unwrap();
    db.execute_batch("DROP INDEX books_shelf_search;CREATE INDEX books_shelf_search ON books((trashed_at>0),archived,shelf_fold(name || '' || author),id)").unwrap();
    drop(db);
    assert!(Library::open_fixture(&fixture.db).is_err());
}
#[test]
fn public_html_urls_match_python_trust_boundary() {
    for url in [
        "https://example.internal/a",
        "https://example.lan/a",
        "https://example.home/a",
        "https://example.test/a",
        "https://example.invalid/a",
        "https://example.com:80/a",
        "https://[example.com]/a",
        "https://[8.8.8.8]/a",
        "https://example.com:+443/a",
        "https://[2001:4860:4860::8888]:+443/a",
        "https://[v1.example.com]/a",
        "https://[V1.example.com]/a",
        "http://example.com:443/a",
        "https://[::ffff:127.0.0.1]/a",
        "https://[fec0::1]/a",
        "https://[2001:db8::1]/a",
        "http://192.0.0.8/a",
        "http://192.0.0.9/a",
        "http://198.18.0.1/a",
        "https://example.com/a/../b?x=%FF",
        "https://[2001:4860:4860::8888]/a",
    ] {
        let markup = format!("<img src='{url}'>");
        let mut actual = serde_json::json!({"html":markup});
        reader_local::http::public_content(&mut actual).unwrap();
        assert_eq!(actual, common::reference()["trust_boundary"][url], "{url}");
    }
}
#[test]
fn malformed_metadata_errors_match_python() {
    for raw in [
        "{\"x\" 1}",
        "[1,]",
        "{\n\"x\":",
        "{",
        "[",
        "\"unterminated",
        "true false",
        "{\"x\":1,}",
        r#"{"x":"\u12x4"}"#,
        r#"{"雪":"\u12x4"}"#,
        r#"{"x":"\u123"}"#,
        r#"{"x":"\q"}"#,
        r#"{"x":tru}"#,
        r#"{"x":tru"#,
        r#"{"x":fals"#,
        r#"{"x":nul"#,
    ] {
        let fixture = common::Fixture::new("small");
        let db = rusqlite::Connection::open(&fixture.db).unwrap();
        db.create_scalar_function(
            "shelf_fold",
            1,
            rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
            |ctx| Ok(reader_local::casefold::casefold(&ctx.get::<String>(0)?)),
        )
        .unwrap();
        db.execute("UPDATE books SET raw=?", [raw]).unwrap();
        drop(db);
        let expected = &common::reference()["malformed_metadata"][raw];
        let actual = Library::open_fixture(&fixture.db)
            .unwrap()
            .list_books(&ShelfQuery::from_uri("/api/books").unwrap())
            .unwrap_err();
        assert_eq!(actual.status, 400);
        assert_eq!(actual.message, expected.as_str().unwrap(), "raw={raw:?}");
    }
}
#[test]
fn html_named_and_numeric_entities_match_python() {
    let cases = &common::reference()["entities"];
    for pair in cases.as_array().unwrap() {
        let markup = pair[0].as_str().unwrap();
        let mut actual = serde_json::json!({"html":markup});
        reader_local::http::public_content(&mut actual).unwrap();
        assert_eq!(actual, pair[1], "{markup}");
    }
}
#[test]
fn raw_script_style_skip_entity_decoding() {
    let entity = format!("&#{};", "9".repeat(4301));
    for tag in ["script", "style"] {
        for closing in [true, false] {
            let markup = format!(
                "<{tag}>{entity}{}",
                if closing {
                    format!("</{tag}>")
                } else {
                    String::new()
                }
            );
            let expected = &common::reference()["raw_text"][&markup];
            let mut actual = serde_json::json!({"html":markup});
            reader_local::http::public_content(&mut actual).unwrap();
            assert_eq!(&actual, expected);
        }
    }
    let mut ordinary = serde_json::json!({"html":format!("<p>{entity}</p>")});
    let error = reader_local::http::public_content(&mut ordinary).unwrap_err();
    assert_eq!(error.status, 400);
    let expected = &common::reference()["visible_entity_error"];
    assert_eq!(error.message, expected.as_str().unwrap());
}
#[test]
fn shelf_matches_python_pages() {
    let fixture = common::Fixture::new("small");
    let library = Library::open_fixture(&fixture.db).unwrap();
    let expected = &common::reference()["shelf_page_limit_2"];
    let actual = serde_json::to_value(
        library
            .list_books(&ShelfQuery::from_uri("/api/books?limit=2").unwrap())
            .unwrap(),
    )
    .unwrap();
    assert_eq!(&actual, expected);
}
#[test]
fn unicode_matches_python() {
    let folds = &common::reference()["casefold"];
    let mut count = 0;
    for i in 0..=0x10ffff {
        if let Some(c) = char::from_u32(i) {
            let original = c.to_string();
            let expected = folds
                .get(i.to_string())
                .and_then(Value::as_str)
                .unwrap_or(&original);
            assert_eq!(
                reader_local::casefold::casefold(&original),
                expected,
                "scalar {i:x}"
            );
            count += 1
        }
    }
    assert_eq!(count, 1_112_064);
}
#[test]
fn parser_matches_python() {
    for (uri, limit, offset) in [
        ("/?limit=&limit=1_0", 10, 0),
        ("/?limit=١٠&offset=-2", 10, 0),
        ("/?limit=999999999999999999999999999999999999", 200, 0),
        ("/?limit=%E2%80%8310%E2%80%83", 10, 0),
    ] {
        let q = ShelfQuery::from_uri(uri).unwrap();
        assert_eq!((q.limit, q.offset), (limit, offset));
    }
    for uri in ["/?limit=%1C10", "/?limit=²", "/?limit=1__0", "/?kind=no"] {
        assert!(ShelfQuery::from_uri(uri).is_err(), "{uri}");
    }
}
#[test]
fn shelf_applies_public_output() {
    let mut v = serde_json::json!({"warnings":[{"id":"id /?#雪","source":null,"cover":"http://127.0.0.1/a","html":"<p onclick='bad()'>Text<script>x</script><img src='https://cdn.example/x.png'></p>"}],"cover":"data:image/svg+xml;base64,PHN2Zz4="});
    reader_local::http::public_content(&mut v).unwrap();
    assert_eq!(v["cover"], "");
    assert!(
        v["warnings"][0]["cover"]
            .as_str()
            .unwrap()
            .starts_with("/api/books/id%20%2F%3F%23%E9%9B%AA/cover?v=")
    );
    assert_eq!(
        v["warnings"][0]["html"],
        "<p>Text<img src=\"/api/image?url=https%3A%2F%2Fcdn.example%2Fx.png\" alt=\"\" loading=\"lazy\"></p>"
    );
}
#[test]
fn fixture_remains_unchanged() {
    let fixture = common::Fixture::new("small");
    let before = std::fs::read(&fixture.db).unwrap();
    let library = Library::open_fixture(&fixture.db).unwrap();
    for _ in 0..3 {
        library
            .list_books(&ShelfQuery::from_uri("/api/books").unwrap())
            .unwrap();
    }
    assert_eq!(before, std::fs::read(&fixture.db).unwrap());
    assert!(!fixture.db.with_extension("sqlite3-wal").exists());
    assert!(!fixture.db.with_extension("sqlite3-shm").exists());
    assert!(Library::open_fixture(&fixture.root.join("missing")).is_err());
}
