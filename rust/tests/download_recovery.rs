use std::{
    env, fs,
    io::{self, Write},
    path::PathBuf,
    process::Command,
    sync::Arc,
    thread,
    time::{Duration, Instant},
};

#[test]
fn durable_download_http_state_matches_python() {
    let output = Command::new("python")
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .args([
            "-B",
            "-X",
            "utf8",
            "rust/tests/support/m5_download.py",
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

#[test]
fn checkpoint_survives_a_process_kill_in_both_implementations() {
    let output = Command::new("python")
        .current_dir(concat!(env!("CARGO_MANIFEST_DIR"), "/.."))
        .args([
            "-B",
            "-X",
            "utf8",
            "rust/tests/support/m5_checkpoint.py",
            "--driver",
        ])
        .arg(env::current_exe().unwrap())
        .output()
        .expect("Python reference must be available");
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
}

#[test]
#[ignore = "test-only child for the process-kill oracle"]
fn download_checkpoint_driver() {
    use reader_local::{Library, downloads::Downloads};
    use rusqlite::{Connection, TransactionBehavior, params};

    let root = PathBuf::from(env::var_os("READER_M5_CHECKPOINT_FIXTURE").expect("synthetic root"))
        .canonicalize()
        .unwrap();
    assert!(root.starts_with(env::temp_dir().canonicalize().unwrap()));
    assert_eq!(
        fs::read_to_string(root.join("m5-checkpoint-fixture")).unwrap(),
        "reader-m5-checkpoint-1"
    );
    let phase = env::var("READER_M5_CHECKPOINT_PHASE").unwrap();
    assert!(matches!(phase.as_str(), "crash" | "recover"));
    let crashing = phase == "crash";
    let database = root.join("library.sqlite3");
    let library = Arc::new(Library::open_writable_fixture(&database).unwrap());
    let copy = root.clone();
    let downloads =
        Downloads::open(library, move |task, control| {
            assert_eq!(
                (task.book_id.as_str(), task.source.as_str()),
                ("book", "full")
            );
            let mut db = Connection::open(&database).unwrap();
            db.busy_timeout(Duration::from_secs(5)).unwrap();
            let cached: bool = db
                .query_row(
                    "SELECT downloaded FROM chapters WHERE book_id='book' AND idx=?",
                    [task.index],
                    |row| row.get(0),
                )
                .unwrap();
            if !cached {
                let mut log = fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(copy.join(if crashing {
                        "crash-fetches"
                    } else {
                        "recover-fetches"
                    }))
                    .unwrap();
                writeln!(log, "{}", task.index).unwrap();
                drop(log);
                if crashing && task.index == 17 {
                    fs::write(copy.join("checkpoint-gate"), "17").unwrap();
                    while !control.stopping() {
                        thread::sleep(Duration::from_millis(10));
                    }
                    return Err(reader_local::ApiError::new(503, "synthetic reader stopped").into());
                }
            }
            let tx = db
                .transaction_with_behavior(TransactionBehavior::Immediate)
                .unwrap();
            if !cached {
                let body = format!("Saved chapter {}", task.index);
                tx.execute(
                "UPDATE chapters SET downloaded=1,text=?1,html=?2 WHERE book_id='book' AND idx=?3",
                params![body, format!("<p>{body}</p>"), task.index],
            ).unwrap();
            }
            tx.execute(
            "UPDATE chapter_cache SET kind='protected',offline=1 WHERE book_id='book' AND idx=?",
            [task.index],
        ).unwrap();
            tx.commit().unwrap();
            Ok(())
        })
        .unwrap();
    if crashing {
        let job = downloads.start("book", 3, 30).unwrap();
        fs::write(root.join("checkpoint-job"), job["id"].as_str().unwrap()).unwrap();
        loop {
            thread::sleep(Duration::from_secs(1));
        }
    }
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let jobs = downloads.list(0).unwrap();
        if jobs["jobs"][0]["status"] == "completed" {
            assert_eq!(jobs["jobs"][0]["completed"], 30);
            break;
        }
        assert!(
            Instant::now() < deadline,
            "job did not finish after restart: {jobs}"
        );
        thread::sleep(Duration::from_millis(20));
    }
    downloads.join();
}

#[test]
#[ignore = "requires the frozen M5 mixed-workload orchestrator"]
fn download_scale_driver() {
    use reader_local::{ApiError, Library, downloads::Downloads};
    use rusqlite::{Connection, OpenFlags};
    use serde_json::json;
    use sha2::{Digest, Sha256};
    use std::sync::{Mutex, mpsc};

    let root = PathBuf::from(env::var_os("READER_M5_SCALE_FIXTURE").expect("synthetic root"))
        .canonicalize()
        .unwrap();
    assert!(root.starts_with(env::temp_dir().canonicalize().unwrap()));
    assert_eq!(
        fs::read_to_string(root.join("m5-scale-fixture")).unwrap(),
        "reader-m5-scale-1"
    );
    let path = root.join("library.sqlite3");
    let library = Arc::new(Library::open_writable_fixture(&path).unwrap());
    let (held, gate) = mpsc::channel();
    let (entered, observed) = mpsc::channel();
    let caller = Arc::new(Mutex::new(None::<Instant>));
    let queued = caller.clone();
    let downloads = Downloads::open(library.clone(), move |task, control| {
        if task.book_id == "m5-active-stv" {
            held.send(()).unwrap();
            while !control.stopping() {
                thread::sleep(Duration::from_millis(5));
            }
            return Err(ApiError::new(503, "synthetic hold cancelled").into());
        }
        assert_eq!(task.book_id, "m5-full");
        if task.index == 0 {
            entered
                .send(queued.lock().unwrap().unwrap().elapsed())
                .unwrap();
        }
        // Separates scheduler behavior from adapter parsing or I/O variance.
        thread::sleep(Duration::from_millis(4));
        Ok(())
    })
    .unwrap();
    gate.recv_timeout(Duration::from_secs(20)).unwrap();
    let observer = Connection::open_with_flags(&path, OpenFlags::SQLITE_OPEN_READ_ONLY).unwrap();
    observer.busy_timeout(Duration::from_secs(5)).unwrap();
    let count = |sql: &str| {
        observer
            .query_row(sql, [], |row| row.get::<_, i64>(0))
            .unwrap()
    };
    assert_eq!(
        count("SELECT count(*) FROM download_jobs WHERE status='queued'"),
        10_000
    );
    assert_eq!(
        count("SELECT count(*) FROM download_jobs WHERE status='running'"),
        1
    );
    let started = Instant::now();
    *caller.lock().unwrap() = Some(started);
    let job = downloads.start("m5-full", 0, 256).unwrap();
    let submit_ms = started.elapsed().as_secs_f64() * 1000.;
    let wait_ms = observed
        .recv_timeout(Duration::from_secs(20))
        .unwrap()
        .as_secs_f64()
        * 1000.;
    let mut measurements = Vec::new();
    let mut while_running = 0;
    let mut tail = String::new();
    let full = || {
        observer
            .query_row(
                "SELECT status FROM download_jobs WHERE book_id='m5-full'",
                [],
                |row| row.get::<_, String>(0),
            )
            .unwrap()
    };
    for _ in 0..30 {
        let active = full() == "running";
        let began = Instant::now();
        let chapter = library.chapter("m5-reader", 0, None, 0).unwrap();
        measurements.push(began.elapsed().as_secs_f64() * 1000.);
        assert!(chapter["downloaded"] == true);
        let digest = format!(
            "{:x}",
            Sha256::digest(chapter["text"].as_str().unwrap().as_bytes())
        );
        assert!(tail.is_empty() || tail == digest);
        tail = digest;
        if active && full() == "running" {
            while_running += 1;
        }
        thread::sleep(Duration::from_millis(20));
    }
    assert!(while_running > 0);
    let until = Instant::now() + Duration::from_secs(30);
    while full() != "completed" {
        assert!(Instant::now() < until);
        thread::sleep(Duration::from_millis(5));
    }
    assert_eq!(
        count("SELECT count(*) FROM download_jobs WHERE status='queued'"),
        10_000
    );
    assert_eq!(
        count("SELECT count(*) FROM download_jobs WHERE status='running'"),
        1
    );
    assert_eq!(
        count("SELECT count(*) FROM download_jobs WHERE status='completed'"),
        50_001
    );
    let visible = downloads.list(0).unwrap();
    let finished = &visible["jobs"][0];
    assert_eq!(
        (finished["id"].clone(), finished["completed"].clone()),
        (job["id"].clone(), json!(256))
    );
    println!(
        "M5_SCALE {}",
        json!({
            "queue_wait_ms":wait_ms, "submit_ms":submit_ms, "readers_ms":measurements,
            "reads_entirely_during_full_job":while_running, "output_sha256":tail,
            "completed":finished["completed"], "remaining_stv_queued":10_000,
        })
    );
    io::stdout().flush().unwrap();
    let mut ack = String::new();
    io::stdin().read_line(&mut ack).unwrap();
    assert_eq!(ack.trim_end(), "go");
    let _paused = downloads.pause_for_shutdown(true).unwrap();
    downloads.join();
}
