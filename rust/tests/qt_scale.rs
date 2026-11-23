use reader_local::Library;
use rusqlite::{Connection, OpenFlags};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    env, fs,
    io::{self, Write},
    path::PathBuf,
    sync::{
        Barrier,
        atomic::{AtomicBool, Ordering},
    },
    thread,
    time::{Duration, Instant},
};

const SCOPE: &str = "m4-book";
const SENTINEL: &str = "__m4_sentinel__";

fn timed<T>(work: impl FnOnce() -> T) -> (f64, T) {
    let now = Instant::now();
    let result = work();
    (now.elapsed().as_secs_f64() * 1000., result)
}

fn sha(text: &str) -> String {
    format!("{:x}", Sha256::digest(text.as_bytes()))
}

fn emit(phase: &str, values: Value) {
    println!("QT_SCALE {}", json!({"phase":phase,"values":values}));
    io::stdout().flush().unwrap();
    let mut ack = String::new();
    io::stdin().read_line(&mut ack).unwrap();
    assert_eq!(ack.trim_end(), "go");
}

#[test]
#[ignore = "requires the frozen reference benchmark orchestrator"]
fn qt_scale_driver() {
    let root =
        PathBuf::from(env::var_os("READER_M4_SCALE_FIXTURE").expect("synthetic fixture argument"))
            .canonicalize()
            .unwrap();
    assert!(root.starts_with(env::temp_dir().canonicalize().unwrap()));
    let input: Value =
        serde_json::from_slice(&fs::read(root.join("m4-synthetic.json")).unwrap()).unwrap();
    assert_eq!(input["format"], "reader-m4-qt-synthetic");
    let db_path = root.join("qt.sqlite3");
    let db = Connection::open_with_flags(&db_path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    assert_eq!(
        db.pragma_query_value(None, "application_id", |row| row.get::<_, i64>(0))
            .unwrap(),
        0x51544231
    );
    assert_eq!(
        db.query_row("SELECT count(*) FROM entries", [], |row| row
            .get::<_, i64>(0))
            .unwrap(),
        1_014_374
    );
    drop(db);
    let library = Library::open_writable_fixture(&root.join("library.sqlite3")).unwrap();
    let (open_ms, qt) = timed(|| library.qt().unwrap());
    emit("opened", json!({"samples":[open_ms]}));
    let (cold_ms, cold) = timed(|| qt.snapshot(SCOPE).unwrap());
    emit("cold", json!({"samples":[cold_ms]}));
    let warm = (0..100)
        .map(|_| timed(|| qt.snapshot(SCOPE).unwrap()).0)
        .collect::<Vec<_>>();
    emit("warm", json!({"samples":warm}));
    let repeats = input["samples"].as_u64().unwrap();
    for (label, chapter) in input["chapters"].as_object().unwrap() {
        let chapter = chapter.as_str().unwrap();
        let result = cold.translate(chapter).unwrap();
        let times = (0..repeats)
            .map(|_| {
                let (ms, value) = timed(|| cold.translate(chapter).unwrap());
                assert_eq!(value, result);
                ms
            })
            .collect::<Vec<_>>();
        emit(
            label,
            json!({"samples":times,"output_sha256":sha(&result),"characters":chapter.chars().count()}),
        );
    }
    drop(cold);
    qt.save("name", "私", "riêng", SCOPE).unwrap();
    for scope in ["*", SCOPE] {
        for action in ["add", "update", "delete"] {
            let times = (0..9)
                .map(|index| {
                    let word = format!("__m4_edit_{index}__");
                    if action != "add" {
                        qt.save("name", &word, "before", scope).unwrap();
                    }
                    timed(|| {
                        if action == "delete" {
                            assert_eq!(
                                qt.delete("name", &word, scope).unwrap(),
                                json!({"deleted":1})
                            );
                        } else {
                            qt.save("name", &word, "after", scope).unwrap();
                        }
                    })
                    .0
                })
                .collect::<Vec<_>>();
            emit(&format!("{scope}:{action}"), json!({"samples":times}));
        }
    }
    qt.save("name", SENTINEL, "before", "*").unwrap();
    let old = qt.snapshot(SCOPE).unwrap();
    let probe = input["probe"].as_str().unwrap();
    let before = old.translate(probe).unwrap();
    let after = before.replace("before", "after");
    let ready = Barrier::new(2);
    let done = AtomicBool::new(false);
    let (bulk, contender, readers) = thread::scope(|scope| {
        let reader = scope.spawn(|| {
            ready.wait();
            let mut times = Vec::new();
            while !done.load(Ordering::Acquire) {
                let (ms, value) = timed(|| qt.translate(probe, SCOPE).unwrap());
                assert!(value == before || value == after);
                times.push(ms);
                thread::sleep(Duration::from_millis(2));
            }
            times
        });
        ready.wait();
        let writer = scope.spawn(|| {
            timed(|| {
                qt.import_text("name", input["bulk"].as_str().unwrap(), "merge", "*")
                    .unwrap()
            })
        });
        let contender = (|| {
            let observer = Connection::open_with_flags(&db_path, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
            observer.busy_timeout(Duration::from_secs(5))?;
            let deadline = Instant::now() + Duration::from_secs(30);
            loop {
                let value: String = observer.query_row(
                    "SELECT meaning FROM entries WHERE kind='name' AND scope='*' AND word=?",
                    [SENTINEL],
                    |row| row.get(0),
                )?;
                if value == "after" {
                    break;
                }
                if writer.is_finished() || Instant::now() >= deadline {
                    return Err(rusqlite::Error::InvalidQuery);
                }
                thread::sleep(Duration::from_millis(2));
            }
            Ok(timed(|| {
                qt.save("name", "__m4_second_writer__", "done", "*")
                    .unwrap()
            })
            .0)
        })();
        let bulk = writer.join();
        done.store(true, Ordering::Release);
        (bulk.unwrap(), contender.unwrap(), reader.join().unwrap())
    });
    assert!(!readers.is_empty());
    assert_eq!(qt.translate(probe, SCOPE).unwrap(), after);
    assert_eq!(old.translate(probe).unwrap(), before);
    emit(
        "publish",
        json!({"samples":[bulk.0],"readers_ms":readers,"second_writer_ms":contender,
          "old_sha256":sha(&before),"new_sha256":sha(&after),"report":bulk.1}),
    );
    drop(old);
    emit("reclaimed", json!({}));
    let expected_count = {
        let db = Connection::open(&db_path).unwrap();
        db.query_row(
            "SELECT count(*) FROM entries WHERE kind='name' AND scope='*'",
            [],
            |row| row.get::<_, i64>(0),
        )
        .unwrap()
    };
    let (replace_ms, replacement) = timed(|| {
        qt.import_text("name", "__m4_replacement__=temporary", "replace", "*")
            .unwrap()
    });
    emit(
        "replace",
        json!({"samples":[replace_ms],"report":replacement}),
    );
    let (restore_ms, restored) = timed(|| {
        qt.restore_backup(replacement["backup_id"].as_i64().unwrap())
            .unwrap()
    });
    assert_eq!(restored["restored"], expected_count);
    assert_eq!(qt.translate(SENTINEL, SCOPE).unwrap(), "after");
    emit("restore", json!({"samples":[restore_ms],"report":restored}));
    {
        let external = Connection::open(&db_path).unwrap();
        external
            .execute(
                "UPDATE entries SET meaning='external' WHERE kind='name' AND scope='*' AND word=?",
                [SENTINEL],
            )
            .unwrap();
    }
    let (external_ms, value) = timed(|| qt.translate(SENTINEL, SCOPE).unwrap());
    assert_eq!(value, "external");
    emit(
        "external_commit",
        json!({"samples":[external_ms],"output_sha256":sha(&value)}),
    );
}
