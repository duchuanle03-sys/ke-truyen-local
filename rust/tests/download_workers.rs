mod common;

use common::Fixture;
use reader_local::{
    Library,
    downloads::{DownloadFailure, Downloads},
};
use rusqlite::{Connection, params};
use serde_json::{Value, json};
use std::{
    sync::{Arc, mpsc},
    time::{Duration, Instant},
};

const LIMIT: Duration = Duration::from_secs(5);

fn fixture(books: &[(&str, &str, i64)]) -> Fixture {
    let fixture = Fixture::new("empty");
    let marker = fixture.root.join("fixture/m1-fixture.json");
    let mut data: Value = serde_json::from_slice(&std::fs::read(&marker).unwrap()).unwrap();
    data["sealed"] = json!(false);
    std::fs::write(marker, serde_json::to_vec(&data).unwrap()).unwrap();
    let db = Connection::open(&fixture.db).unwrap();
    db.create_scalar_function(
        "shelf_fold",
        1,
        rusqlite::functions::FunctionFlags::SQLITE_DETERMINISTIC,
        |ctx| ctx.get::<String>(0),
    )
    .unwrap();
    for (id, source, count) in books {
        db.execute(
            "INSERT INTO books(id,name,source,chapter_count,added_at,updated_at) VALUES(?1,?1,?2,?3,0,0)",
            params![id, source, count],
        )
        .unwrap();
    }
    fixture
}

fn open(fixture: &Fixture) -> Arc<Library> {
    Arc::new(Library::open_writable_fixture(&fixture.db).unwrap())
}

fn saved(fixture: &Fixture, job: &Value) -> (String, bool, Value) {
    Connection::open(&fixture.db)
        .unwrap()
        .query_row(
            "SELECT status,cancelled,data FROM download_jobs WHERE id=?",
            [job["id"].as_str().unwrap()],
            |r| {
                Ok((
                    r.get(0)?,
                    r.get(1)?,
                    serde_json::from_str(&r.get::<_, String>(2)?).unwrap(),
                ))
            },
        )
        .unwrap()
}

fn until(check: impl Fn() -> bool) {
    let start = Instant::now();
    while !check() {
        assert!(
            start.elapsed() < LIMIT,
            "download did not reach its expected state"
        );
        std::thread::sleep(Duration::from_millis(5));
    }
}

#[test]
fn pause_resume_handoff_keeps_checkpoint_and_never_duplicates_started_chapter() {
    let fixture = fixture(&[("book", "full", 3)]);
    let (entered, calls) = mpsc::channel();
    let downloads = Downloads::open(open(&fixture), move |task, _| {
        let (done, release) = mpsc::channel();
        entered.send((task.index, done)).unwrap();
        release.recv_timeout(LIMIT).unwrap();
        Ok(())
    })
    .unwrap();
    let job = downloads.start("book", 0, -1).unwrap();
    assert_eq!(job["source"], "full");
    let id = job["id"].as_str().unwrap();
    let (index, release) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(index, 0);
    assert_eq!(downloads.action(id, "pause").unwrap()["status"], "paused");
    assert_eq!(saved(&fixture, &job).0, "paused");
    assert!(downloads.start("book", 0, -1).is_err());
    assert_eq!(downloads.action(id, "resume").unwrap()["status"], "queued");
    release.send(()).unwrap();
    let (index, release) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(index, 1);
    assert_eq!(saved(&fixture, &job).2["completed"], 1);
    downloads.action(id, "cancel").unwrap();
    assert!(saved(&fixture, &job).1);
    assert!(downloads.action(id, "delete").is_err());
    release.send(()).unwrap();
    until(|| saved(&fixture, &job).0 == "cancelled");
    let retry = downloads.action(id, "retry").unwrap();
    assert_ne!(job["id"], retry["id"]);
    assert_eq!(retry["start"], 2);
    let (index, release) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(index, 2);
    release.send(()).unwrap();
    until(|| saved(&fixture, &retry).0 == "completed");
    assert_eq!(
        downloads.action(id, "delete").unwrap()["status"],
        "cancelled"
    );
    let history = downloads.list(0).unwrap();
    assert_eq!(history["jobs"].as_array().unwrap().len(), 1);
    assert_eq!(history["jobs"][0]["id"], retry["id"]);
    assert_eq!(history["jobs"][0]["source"], "full");
    assert_eq!(history["next_offset"], Value::Null);
}

#[test]
fn trash_keeps_started_download_visible_and_blocks_purge_until_it_finishes() {
    let fixture = fixture(&[("book", "full", 1)]);
    let (entered, calls) = mpsc::channel();
    let downloads = Downloads::open(open(&fixture), move |_, _| {
        let (done, release) = mpsc::channel();
        entered.send(done).unwrap();
        release.recv_timeout(LIMIT).unwrap();
        Ok(())
    })
    .unwrap();
    let job = downloads.start("book", 0, -1).unwrap();
    let release = calls.recv_timeout(LIMIT).unwrap();
    downloads.trash_book("book").unwrap();
    let (status, cancelled, _) = saved(&fixture, &job);
    assert_eq!(status, "running");
    assert!(cancelled);
    assert_eq!(downloads.purge_book("book").unwrap_err().status, 400);
    release.send(()).unwrap();
    until(|| saved(&fixture, &job).0 == "cancelled");
    assert_eq!(downloads.purge_book("book").unwrap()["deleted"], true);
}

#[test]
fn two_workers_admit_one_stv_and_allow_later_independent_book() {
    let fixture = fixture(&[
        ("a", "stv", 1),
        ("b", "stv", 1),
        ("c", "full", 1),
        ("d", "full", 1),
    ]);
    let (entered, calls) = mpsc::channel();
    let downloads = Downloads::open(open(&fixture), move |task, _| {
        let (done, release) = mpsc::channel();
        entered.send((task.book_id.clone(), done)).unwrap();
        release.recv_timeout(LIMIT).unwrap();
        Ok(())
    })
    .unwrap();
    downloads.start("a", 0, -1).unwrap();
    let (book, release_a) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(book, "a");
    let b = downloads.start("b", 0, -1).unwrap();
    downloads.start("c", 0, -1).unwrap();
    let (book, release_c) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(book, "c");
    let d = downloads.start("d", 0, -1).unwrap();
    assert_eq!(saved(&fixture, &b).0, "queued");
    assert_eq!(saved(&fixture, &d).0, "queued");
    release_c.send(()).unwrap();
    let (book, release_d) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(book, "d");
    release_d.send(()).unwrap();
    release_a.send(()).unwrap();
    let (book, release_b) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(book, "b");
    release_b.send(()).unwrap();
    until(|| saved(&fixture, &b).0 == "completed");
}

#[test]
fn verified_requeue_rolls_back_as_a_group_and_retries_without_an_answer() {
    let fixture = fixture(&[("a", "full", 1), ("b", "full", 1)]);
    let verified = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let source = verified.clone();
    let downloads = Downloads::open(open(&fixture), move |_, _| {
        if source.load(std::sync::atomic::Ordering::Acquire) {
            return Ok(());
        }
        Err(DownloadFailure::Verification {
            key: "session-owner".into(),
            outcome: Arc::default(),
            captcha: Box::new(json!({"id":"challenge-secret","source":"stv"})),
            message: "Verify first".into(),
        })
    })
    .unwrap();
    let a = downloads.start("a", 0, -1).unwrap();
    let b = downloads.start("b", 0, -1).unwrap();
    until(|| {
        saved(&fixture, &a).0 == "verification_required"
            && saved(&fixture, &b).0 == "verification_required"
    });
    for job in [&a, &b] {
        let data = saved(&fixture, job).2.to_string();
        assert!(!data.contains("challenge-secret") && !data.contains("session-owner"));
    }
    let db = Connection::open(&fixture.db).unwrap();
    db.execute_batch("CREATE TRIGGER fail_requeue BEFORE UPDATE ON download_jobs WHEN NEW.status='queued' AND NEW.book_id='b' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    let committed = verified.clone();
    assert!(downloads.on_verification_committed(move |key| {
        assert_eq!(key, "session-owner");
        committed.store(true, std::sync::atomic::Ordering::Release);
    }));
    assert!(
        downloads
            .resolve_verification("session-owner", true)
            .is_err()
    );
    assert_eq!(saved(&fixture, &a).0, "verification_required");
    assert_eq!(saved(&fixture, &b).0, "verification_required");
    assert!(!verified.load(std::sync::atomic::Ordering::Acquire));
    db.execute_batch("DROP TRIGGER fail_requeue").unwrap();
    until(|| saved(&fixture, &a).0 == "completed" && saved(&fixture, &b).0 == "completed");
}

#[test]
fn verification_resolved_before_worker_registration_requeues_without_answer_replay() {
    let fixture = fixture(&[("book", "stv", 1)]);
    let (entered, calls) = mpsc::channel();
    let cleared = Arc::new(std::sync::atomic::AtomicBool::new(false));
    let outcome = Arc::new(std::sync::OnceLock::new());
    let worker_outcome = outcome.clone();
    let seen = cleared.clone();
    let downloads = Downloads::open(open(&fixture), move |_, _| {
        if seen.load(std::sync::atomic::Ordering::Acquire) {
            return Ok(());
        }
        let (done, release) = mpsc::channel();
        entered.send(done).unwrap();
        release.recv_timeout(LIMIT).unwrap();
        Err(DownloadFailure::Verification {
            key: "late-registered".into(),
            outcome: worker_outcome.clone(),
            captcha: Box::new(json!({"id":"fixture-challenge"})),
            message: "Verify first".into(),
        })
    })
    .unwrap();
    assert!(downloads.on_verification_committed(move |_| {
        cleared.store(true, std::sync::atomic::Ordering::Release)
    }));
    let job = downloads.start("book", 0, -1).unwrap();
    let release = calls.recv_timeout(LIMIT).unwrap();
    outcome.set(true).unwrap();
    release.send(()).unwrap();
    until(|| saved(&fixture, &job).0 == "completed");
    assert_eq!(saved(&fixture, &job).2["completed"], 1);
    assert!(calls.try_recv().is_err());
}

#[test]
fn a_late_job_waits_for_the_entire_verification_cohort_to_commit() {
    use std::sync::{
        OnceLock,
        atomic::{AtomicBool, Ordering},
    };
    let fixture = fixture(&[("first", "stv", 1), ("late", "stv", 1)]);
    let (entered, calls) = mpsc::channel();
    let acknowledged = Arc::new(AtomicBool::new(false));
    let flag = acknowledged.clone();
    let outcome = Arc::new(OnceLock::new());
    let worker_outcome = outcome.clone();
    let downloads = Downloads::open(open(&fixture), move |task, _| {
        if flag.load(Ordering::Acquire) {
            return Ok(());
        }
        if task.book_id == "late" {
            let (done, release) = mpsc::channel();
            entered.send(done).unwrap();
            release.recv_timeout(LIMIT).unwrap();
        }
        Err(DownloadFailure::Verification {
            key: "group".into(),
            outcome: worker_outcome.clone(),
            captcha: Box::new(json!({"id":"fixture-challenge"})),
            message: "Verify first".into(),
        })
    })
    .unwrap();
    let flag = acknowledged.clone();
    assert!(downloads.on_verification_committed(move |_| flag.store(true, Ordering::Release)));
    let first = downloads.start("first", 0, -1).unwrap();
    until(|| saved(&fixture, &first).0 == "verification_required");
    let late = downloads.start("late", 0, -1).unwrap();
    let release = calls.recv_timeout(LIMIT).unwrap();
    let db = Connection::open(&fixture.db).unwrap();
    db.execute_batch("CREATE TRIGGER fail_cohort BEFORE UPDATE ON download_jobs WHEN NEW.status='queued' AND NEW.book_id='first' BEGIN SELECT RAISE(ABORT,'injected'); END;").unwrap();
    outcome.set(true).unwrap();
    release.send(()).unwrap();
    until(|| saved(&fixture, &late).0 == "verification_required");
    assert!(!acknowledged.load(Ordering::Acquire));
    assert_eq!(saved(&fixture, &first).0, "verification_required");
    db.execute_batch("DROP TRIGGER fail_cohort").unwrap();
    until(|| saved(&fixture, &first).0 == "completed" && saved(&fixture, &late).0 == "completed");
}

#[test]
fn startup_recovery_then_shutdown_flush_the_last_completed_boundary() {
    let fixture = fixture(&[("book", "full", 30), ("stopped", "full", 2)]);
    let library = open(&fixture);
    let offline = Downloads::offline(library.clone());
    let resumed = offline.start("book", 2, 16).unwrap();
    let stopped = offline.start("stopped", 0, 2).unwrap();
    drop(offline);
    let db = Connection::open(&fixture.db).unwrap();
    db.execute(
        "UPDATE download_jobs SET status='verification_required',data=json_set(data,'$.completed',1) WHERE book_id='book'",
        [],
    )
    .unwrap();
    db.execute(
        "UPDATE download_jobs SET status='running',cancelled=1 WHERE book_id='stopped'",
        [],
    )
    .unwrap();

    let (entered, calls) = mpsc::channel();
    let (finished, results) = mpsc::channel();
    let downloads = Downloads::open(library, move |task, control| {
        assert_eq!(task.book_id, "book");
        let (done, release) = mpsc::channel();
        entered.send((task.index, done)).unwrap();
        release.recv_timeout(LIMIT).unwrap();
        if control.stopping() {
            finished.send(task.index).unwrap();
        }
        Ok(())
    })
    .unwrap();
    for expected in 3..=13 {
        let (index, release) = calls.recv_timeout(LIMIT).unwrap();
        assert_eq!(index, expected);
        if expected == 13 {
            assert_eq!(saved(&fixture, &resumed).2["completed"], 11);
        }
        release.send(()).unwrap();
    }
    let (index, release) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(index, 14);
    assert_eq!(saved(&fixture, &resumed).2["completed"], 11);
    assert_eq!(saved(&fixture, &stopped).0, "cancelled");
    assert_eq!(
        downloads.pause_for_shutdown(false).unwrap()["active_downloads"],
        1
    );
    assert_eq!(
        downloads.pause_for_shutdown(true).unwrap(),
        json!({"accepted":true})
    );
    assert_eq!(saved(&fixture, &resumed).0, "paused");
    assert_eq!(saved(&fixture, &resumed).2["completed"], 12);
    release.send(()).unwrap();
    downloads.join();
    assert_eq!(results.recv_timeout(LIMIT).unwrap(), 14);
    assert_eq!(saved(&fixture, &resumed).2["completed"], 12);
}

#[test]
fn late_chapter_completion_cannot_restore_deleted_job() {
    let fixture = fixture(&[("book", "full", 2), ("other", "full", 1)]);
    let (entered, calls) = mpsc::channel();
    let downloads = Downloads::open(open(&fixture), move |task, _| {
        if task.book_id == "book" {
            let (done, release) = mpsc::channel();
            entered.send((task.index, done)).unwrap();
            release.recv_timeout(LIMIT).unwrap();
        }
        Ok(())
    })
    .unwrap();
    let job = downloads.start("book", 0, -1).unwrap();
    let (index, release) = calls.recv_timeout(LIMIT).unwrap();
    assert_eq!(index, 0);
    let db = Connection::open(&fixture.db).unwrap();
    db.execute("DELETE FROM download_jobs WHERE book_id='book'", [])
        .unwrap();
    downloads.trash_book("book").unwrap();
    release.send(()).unwrap();
    let independent = downloads.start("other", 0, -1).unwrap();
    until(|| saved(&fixture, &independent).0 == "completed");
    downloads.join();
    let history = downloads.list(0).unwrap();
    assert_eq!(history["jobs"].as_array().unwrap().len(), 1);
    assert_ne!(history["jobs"][0]["id"], job["id"]);
    assert!(calls.try_recv().is_err());
}

#[test]
fn active_comic_job_reports_images_in_the_current_chapter() {
    let fixture = fixture(&[("book", "qq", 1)]);
    let (entered, calls) = mpsc::channel();
    let downloads = Downloads::open(open(&fixture), move |_, control| {
        control.images(3, 40);
        let (done, release) = mpsc::channel();
        entered.send(done).unwrap();
        release.recv_timeout(LIMIT).unwrap();
        Ok(())
    })
    .unwrap();
    let job = downloads.start("book", 0, -1).unwrap();
    let release = calls.recv_timeout(LIMIT).unwrap();
    let listed = downloads.list(0).unwrap()["jobs"][0].clone();
    assert_eq!(listed["image_done"], 3, "{listed}");
    assert_eq!(listed["image_total"], 40);
    release.send(()).unwrap();
    until(|| saved(&fixture, &job).0 == "completed");
    let done = downloads.list(0).unwrap()["jobs"][0].clone();
    assert!(done["image_total"].as_i64().unwrap_or(0) == 0, "{done}");
}

#[test]
fn a_full_novel_download_covers_every_chapter() {
    let fixture = fixture(&[("book", "full", 5)]);
    let downloads = Downloads::open(open(&fixture), |_, _| Ok(())).unwrap();
    assert_eq!(downloads.start("book", 0, -1).unwrap()["total"], 5);
}

#[test]
fn a_bad_range_is_refused() {
    let fixture = fixture(&[("book", "full", 5)]);
    let downloads = Downloads::offline(open(&fixture));
    for (start, count) in [(0, 0), (5, 1), (-1, 2)] {
        let error = downloads.start("book", start, count).unwrap_err();
        assert_eq!(error.status, 400, "{start},{count}");
        assert_eq!(error.message, "Khoảng chương không hợp lệ.");
    }
}
