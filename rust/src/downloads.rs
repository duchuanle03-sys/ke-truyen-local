use crate::{ApiError, Library, reader::now};
use rusqlite::{Connection, OptionalExtension, Row, TransactionBehavior, params};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::{
    collections::{HashMap, HashSet, VecDeque},
    sync::{
        Arc, Condvar, Mutex, MutexGuard, OnceLock,
        atomic::{AtomicBool, Ordering},
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const ACTIVE: &str = "status IN ('queued','running','paused','verification_required')";
const SHUTDOWN_ACTIVE: &str = "status IN ('queued','running','verification_required')";
const RETRY: Duration = Duration::from_millis(250);
const PAGE: i64 = 50;
const WORKERS: usize = 2;
const SOURCE_HISTORY: usize = 100;

pub struct ChapterTask {
    pub job_id: String,
    pub book_id: String,
    pub source: String,
    pub index: i64,
}

pub struct DownloadControl {
    interrupted: Arc<AtomicBool>,
    stopping: Arc<AtomicBool>,
}

impl DownloadControl {
    pub fn cancelled(&self) -> bool {
        self.stopping() || self.interrupted.load(Ordering::Acquire)
    }

    pub fn stopping(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }
}

pub enum DownloadFailure {
    Error(ApiError),
    Access {
        message: String,
        code: &'static str,
        access: Value,
    },
    Verification {
        key: String,
        outcome: Arc<OnceLock<bool>>,
        captcha: Box<Value>,
        message: String,
    },
}

impl From<ApiError> for DownloadFailure {
    fn from(error: ApiError) -> Self {
        Self::Error(error)
    }
}

type Process = dyn Fn(&ChapterTask, &DownloadControl) -> Result<(), DownloadFailure> + Send + Sync;
type Verified = dyn Fn(&str) + Send + Sync;

#[derive(Default, Deserialize, Serialize)]
#[serde(default)]
struct Checkpoint {
    name: String,
    start: i64,
    end: i64,
    total: i64,
    completed: i64,
    error: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    error_code: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    access: Option<Value>,
}

impl Checkpoint {
    fn clear_error(&mut self) {
        self.error.clear();
        self.error_code = None;
        self.access = None;
    }

    fn bounds(&self) -> Option<std::ops::Range<i64>> {
        (self.start >= 0
            && self.end >= self.start
            && self.total == self.end - self.start
            && (0..=self.total).contains(&self.completed))
        .then(|| self.start + self.completed..self.end)
    }
}

struct Job {
    id: String,
    book_id: String,
    status: String,
    cancelled: bool,
    data: Checkpoint,
}

impl Job {
    fn read(row: &Row<'_>) -> rusqlite::Result<Self> {
        let encoded: String = row.get(4)?;
        let mut data: Checkpoint = serde_json::from_str(&encoded).map_err(|error| {
            rusqlite::Error::FromSqlConversionFailure(
                4,
                rusqlite::types::Type::Text,
                Box::new(error),
            )
        })?;
        let status: String = row.get(2)?;
        if status == "queued" {
            data.clear_error();
        }
        Ok(Self {
            id: row.get(0)?,
            book_id: row.get(1)?,
            status,
            cancelled: row.get(3)?,
            data,
        })
    }

    fn find(db: &Connection, id: &str) -> Result<Option<Self>, ApiError> {
        Ok(db
            .query_row(
                "SELECT id,book_id,status,cancelled,data FROM download_jobs WHERE id=?",
                [id],
                Self::read,
            )
            .optional()?)
    }

    fn value(&self, state: Option<&State>) -> Value {
        let mut value = json!(self.data);
        value["id"] = json!(self.id);
        value["book_id"] = json!(self.book_id);
        value["status"] = json!(self.status);
        value["cancelled"] = json!(self.cancelled);
        value["captcha"] = Value::Null;
        if let Some(state) = state {
            if let Some(active) = state.active.get(&self.id) {
                value["completed"] = json!(active.completed);
            }
            if let Some((_, source)) = state.sources.iter().find(|(id, _)| id == &self.id) {
                value["source"] = json!(source);
            }
            if let Some(waiting) = state.verifications.get(&self.id) {
                value["captcha"] = waiting.captcha.clone();
            }
        }
        value
    }

    fn encoded(&self) -> Result<String, ApiError> {
        serde_json::to_string(&self.data).map_err(|_| internal())
    }

    fn save(&self, db: &Connection) -> Result<(), ApiError> {
        db.execute(
            "UPDATE download_jobs SET status=?,cancelled=?,data=?,updated_at=? WHERE id=?",
            params![self.status, self.cancelled, self.encoded()?, now(), self.id],
        )?;
        Ok(())
    }
}

struct Active {
    book_id: String,
    source: String,
    completed: i64,
    interrupted: Arc<AtomicBool>,
}

struct Verification {
    book_id: String,
    key: String,
    captcha: Value,
    resolution: Option<bool>,
}

#[derive(Default)]
struct State {
    active: HashMap<String, Active>,
    ready: VecDeque<(ChapterTask, i64, DownloadControl)>,
    sources: VecDeque<(String, String)>,
    verifications: HashMap<String, Verification>,
}

struct Scheduler {
    library: Arc<Library>,
    process: Option<Box<Process>>,
    verified: OnceLock<Box<Verified>>,
    state: Mutex<State>,
    wake: Condvar,
    stopping: Arc<AtomicBool>,
}

pub struct Downloads {
    scheduler: Arc<Scheduler>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}

impl Downloads {
    fn new(library: Arc<Library>, process: Option<Box<Process>>) -> Self {
        Self {
            scheduler: Arc::new(Scheduler {
                library,
                process,
                verified: OnceLock::new(),
                state: Mutex::default(),
                wake: Condvar::new(),
                stopping: Arc::default(),
            }),
            workers: Mutex::default(),
        }
    }

    pub fn offline(library: Arc<Library>) -> Self {
        Self::new(library, None)
    }

    pub fn open(
        library: Arc<Library>,
        process: impl Fn(&ChapterTask, &DownloadControl) -> Result<(), DownloadFailure>
        + Send
        + Sync
        + 'static,
    ) -> Result<Self, ApiError> {
        let downloads = Self::new(library, Some(Box::new(process)));
        let mut db = downloads.scheduler.database()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        tx.execute(
            &format!("UPDATE download_jobs SET status='cancelled',cancelled=1 WHERE {ACTIVE}
                AND (cancelled=1 OR EXISTS(SELECT 1 FROM books b WHERE b.id=download_jobs.book_id AND b.trashed_at>0))"),
            [],
        )?;
        tx.execute(
            "UPDATE download_jobs SET status='queued' WHERE status IN ('running','verification_required')",
            [],
        )?;
        tx.commit()?;
        for index in 0..WORKERS {
            let scheduler = downloads.scheduler.clone();
            let worker = thread::Builder::new()
                .name(format!("chapter-download-{index}"))
                .spawn(move || scheduler.worker())?;
            downloads
                .workers
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .push(worker);
        }
        Ok(downloads)
    }

    pub fn list_saved(library: &Library, offset: i64) -> Result<Value, ApiError> {
        list(library, offset, None)
    }

    pub fn list(&self, offset: i64) -> Result<Value, ApiError> {
        let state = self.scheduler.lock();
        list(&self.scheduler.library, offset, Some(&state))
    }

    pub fn start(&self, book_id: &str, start: i64, count: i64) -> Result<Value, ApiError> {
        let scheduler = &self.scheduler;
        let mut state = scheduler.lock();
        if scheduler.stopped() {
            return Err(ApiError::new(
                400,
                "Máy chủ đang dừng; tác vụ đã lưu sẽ tiếp tục khi mở lại.",
            ));
        }
        let mut db = scheduler.database()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let job = create(&tx, book_id, start, count)?;
        tx.commit()?;
        scheduler.changed(&mut state);
        Ok(job.value(Some(&state)))
    }

    pub fn action(&self, id: &str, action: &str) -> Result<Value, ApiError> {
        let scheduler = &self.scheduler;
        let mut state = scheduler.lock();
        if scheduler.stopped() {
            return Err(ApiError::new(
                400,
                "Máy chủ đang dừng; hãy mở lại ứng dụng để quản lý tác vụ.",
            ));
        }
        let mut db = scheduler.database()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let mut job =
            Job::find(&tx, id)?.ok_or_else(|| ApiError::new(404, "Không tìm thấy tác vụ."))?;
        if let Some(active) = state.active.get(id) {
            job.data.completed = active.completed;
        }
        match (action, job.status.as_str()) {
            ("pause", "running") => job.status = "paused".into(),
            ("resume", "paused") if !job.cancelled => {
                job.status = if state.verifications.contains_key(id) {
                    "verification_required"
                } else {
                    "queued"
                }
                .into();
                if job.status == "queued" {
                    job.data.clear_error();
                }
            }
            ("cancel", _) => {
                job.cancelled = true;
                if !state.active.contains_key(id) {
                    job.status = "cancelled".into();
                }
            }
            ("retry", "failed" | "cancelled") if !state.active.contains_key(id) => {
                let range = job.data.bounds().ok_or_else(invalid_action)?;
                if !range.is_empty() {
                    let retry = create(&tx, &job.book_id, range.start, range.end - range.start)?;
                    tx.commit()?;
                    scheduler.changed(&mut state);
                    return Ok(retry.value(Some(&state)));
                }
                job.status = "completed".into();
                job.cancelled = false;
            }
            ("delete", status)
                if !matches!(
                    status,
                    "queued" | "running" | "paused" | "verification_required"
                ) =>
            {
                tx.execute("DELETE FROM download_jobs WHERE id=?", [id])?;
                tx.commit()?;
                state.verifications.remove(id);
                let value = job.value(Some(&state));
                state.sources.retain(|(source_id, _)| source_id != id);
                return Ok(value);
            }
            _ => return Err(invalid_action()),
        }
        job.save(&tx)?;
        tx.commit()?;
        if matches!(action, "pause" | "cancel")
            && let Some(active) = state.active.get(id)
        {
            active.interrupted.store(true, Ordering::Release);
        }
        if action == "cancel" {
            state.verifications.remove(id);
        }
        scheduler.changed(&mut state);
        Ok(job.value(Some(&state)))
    }

    pub fn update_captcha(&self, key: &str, captcha: Value) {
        for waiting in self.scheduler.lock().verifications.values_mut() {
            if waiting.key == key {
                waiting.captcha = captcha.clone();
            }
        }
    }

    pub fn resolve_verification(&self, key: &str, verified: bool) -> Result<(), ApiError> {
        let scheduler = &self.scheduler;
        let mut state = scheduler.lock();
        for waiting in state.verifications.values_mut() {
            if waiting.key == key && waiting.resolution.is_none() {
                waiting.resolution = Some(verified);
            }
        }
        let result = scheduler.apply_verification(&mut state, key);
        scheduler.wake.notify_all();
        result
    }

    pub fn on_verification_committed(
        &self,
        callback: impl Fn(&str) + Send + Sync + 'static,
    ) -> bool {
        self.scheduler.verified.set(Box::new(callback)).is_ok()
    }

    pub fn trash_book(&self, book_id: &str) -> Result<Value, ApiError> {
        let scheduler = &self.scheduler;
        let mut state = scheduler.lock();
        let worker = state
            .active
            .iter()
            .find(|(_, active)| active.book_id == book_id);
        let book = scheduler
            .library
            .trash_with_active_download(book_id, worker.map(|(id, _)| id.as_str()))?;
        if let Some((_, active)) = worker {
            active.interrupted.store(true, Ordering::Release);
        }
        state
            .verifications
            .retain(|_, waiting| waiting.book_id != book_id);
        scheduler.wake.notify_all();
        Ok(book)
    }

    pub fn purge_book(&self, book_id: &str) -> Result<Value, ApiError> {
        let state = self.scheduler.lock();
        if state
            .active
            .values()
            .any(|active| active.book_id == book_id)
        {
            return Err(ApiError::new(
                400,
                "Đang dừng tác vụ tải của truyện. Hãy thử lại sau khi tác vụ dừng.",
            ));
        }
        self.scheduler.library.purge_book(book_id)
    }

    pub fn pause_for_shutdown(&self, pause: bool) -> Result<Value, ApiError> {
        let scheduler = &self.scheduler;
        let mut state = scheduler.lock();
        if scheduler.stopped() {
            return Ok(json!({"accepted":true}));
        }
        let mut db = scheduler.library.write_connection()?;
        db.busy_timeout(Duration::from_secs(1))?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let active: i64 = tx.query_row(
            &format!("SELECT count(*) FROM download_jobs WHERE {SHUTDOWN_ACTIVE}"),
            [],
            |row| row.get(0),
        )?;
        if active > 0 && !pause {
            return Ok(
                json!({"error":"Tạm dừng các tác vụ tải đang chạy và tắt máy chủ?","code":"active_downloads","active_downloads":active}),
            );
        }
        for (id, active) in &state.active {
            if let Some(mut job) = Job::find(&tx, id)? {
                job.data.completed = active.completed;
                job.save(&tx)?;
            }
        }
        tx.execute(&format!("UPDATE download_jobs SET status=CASE WHEN cancelled=1 THEN 'cancelled' ELSE 'paused' END,updated_at=? WHERE {SHUTDOWN_ACTIVE} OR (status='paused' AND cancelled=1)"), [now()])?;
        tx.commit()?;
        state.verifications.clear();
        scheduler.stopping.store(true, Ordering::Release);
        scheduler.wake.notify_all();
        Ok(json!({"accepted":true}))
    }

    pub fn join(&self) {
        let scheduler = &self.scheduler;
        {
            let _state = scheduler.lock();
            scheduler.stopping.store(true, Ordering::Release);
            scheduler.wake.notify_all();
        }
        for worker in self
            .workers
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .drain(..)
        {
            if worker.join().is_err() {
                eprintln!("Download worker exited unexpectedly.");
            }
        }
    }
}

impl Drop for Downloads {
    fn drop(&mut self) {
        self.join();
    }
}

impl Scheduler {
    fn stopped(&self) -> bool {
        self.stopping.load(Ordering::Acquire)
    }

    fn lock(&self) -> MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(|e| e.into_inner())
    }

    fn database(&self) -> Result<Connection, ApiError> {
        let db = self.library.write_connection()?;
        db.busy_timeout(RETRY)?;
        Ok(db)
    }

    fn retry<'a>(&self, state: MutexGuard<'a, State>) -> MutexGuard<'a, State> {
        self.wake
            .wait_timeout(state, RETRY)
            .unwrap_or_else(|e| e.into_inner())
            .0
    }

    fn changed(&self, state: &mut State) {
        if self.process.is_some()
            && let Err(error) = self.dispatch(state)
        {
            eprintln!("Download scheduling will retry: {error}");
        }
        self.wake.notify_all();
    }

    fn dispatch(&self, state: &mut State) -> Result<(), ApiError> {
        while state.active.len() < WORKERS {
            let Some(ready) = self.claim(state)? else {
                break;
            };
            state.ready.push_back(ready);
        }
        Ok(())
    }

    fn worker(&self) {
        let Some(process) = self.process.as_deref() else {
            return;
        };
        let mut state = self.lock();
        let mut failed = false;
        while !self.stopped() {
            let resolved: HashSet<_> = state
                .verifications
                .values()
                .filter(|waiting| waiting.resolution.is_some())
                .map(|waiting| waiting.key.clone())
                .collect();
            let mut retry_verification = false;
            for key in resolved {
                retry_verification |= self.apply_verification(&mut state, &key).is_err();
            }
            let dispatched = self.dispatch(&mut state);
            if let Some((task, end, control)) = state.ready.pop_front() {
                failed = false;
                let id = task.job_id.clone();
                self.wake.notify_all();
                drop(state);
                state = self.run(task, end, &control, process);
                state.active.remove(&id);
                self.wake.notify_all();
            } else {
                if dispatched.is_ok() && !retry_verification {
                    failed = false;
                    state = self.wake.wait(state).unwrap_or_else(|e| e.into_inner());
                } else {
                    if !failed && let Err(error) = dispatched {
                        eprintln!("Download scheduling will retry: {error}");
                    }
                    failed = true;
                    state = self.retry(state);
                }
            }
        }
    }

    fn claim(
        &self,
        state: &mut State,
    ) -> Result<Option<(ChapterTask, i64, DownloadControl)>, ApiError> {
        let _gate = self
            .library
            .database_gate
            .try_read()
            .map_err(|_| internal())?;
        let db = self.database()?;
        let mut books = state.active.values().map(|active| active.book_id.as_str());
        let exclude_stv = state.active.values().any(|active| active.source == "stv");
        let selected = db
            .query_row(
                "SELECT j.id,j.book_id,j.status,j.cancelled,j.data,b.source
             FROM download_jobs j JOIN books b ON b.id=j.book_id
             WHERE j.status='queued' AND j.cancelled=0 AND b.trashed_at=0
             AND j.book_id NOT IN (?1,?2) AND (?3=0 OR b.source!='stv')
             AND CASE WHEN json_valid(j.data) THEN json_type(j.data,'$.start')='integer' ELSE 0 END
             ORDER BY j.created_at,j.id LIMIT 1",
                params![
                    books.next().unwrap_or(""),
                    books.next().unwrap_or(""),
                    exclude_stv
                ],
                |row| Ok((Job::read(row)?, row.get::<_, String>(5)?)),
            )
            .optional()?;
        let Some((mut job, source)) = selected else {
            return Ok(None);
        };
        let Some(range) = job.data.bounds() else {
            job.status = "failed".into();
            job.data.error = "Khoảng chương không hợp lệ.".into();
            job.save(&db)?;
            self.wake.notify_all();
            return Err(invalid_action());
        };
        job.data.clear_error();
        if db.execute(
            "UPDATE download_jobs SET status='running',data=?,updated_at=? WHERE id=? AND status='queued' AND cancelled=0",
            params![job.encoded()?, now(), job.id],
        )? == 0 {
            self.wake.notify_all();
            return Err(internal());
        }
        let interrupted = Arc::<AtomicBool>::default();
        state.active.insert(
            job.id.clone(),
            Active {
                book_id: job.book_id.clone(),
                source: source.clone(),
                completed: job.data.completed,
                interrupted: interrupted.clone(),
            },
        );
        state.sources.retain(|(id, _)| id != &job.id);
        if state.sources.len() == SOURCE_HISTORY {
            state.sources.pop_front();
        }
        state.sources.push_back((job.id.clone(), source.clone()));
        Ok(Some((
            ChapterTask {
                job_id: job.id,
                book_id: job.book_id,
                source,
                index: range.start,
            },
            range.end,
            DownloadControl {
                interrupted,
                stopping: self.stopping.clone(),
            },
        )))
    }

    fn run(
        &self,
        mut task: ChapterTask,
        end: i64,
        control: &DownloadControl,
        process: &Process,
    ) -> MutexGuard<'_, State> {
        let mut unflushed = 0;
        let mut checkpoint = Instant::now();
        loop {
            let outcome = if task.index == end || control.cancelled() {
                None
            } else {
                Some(
                    std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        process(&task, control)
                    }))
                    .unwrap_or_else(|_| Err(internal().into())),
                )
            };
            let mut state = self.lock();
            if self.stopped() {
                return state;
            }
            let Some(active) = state.active.get_mut(&task.job_id) else {
                return state;
            };
            if matches!(outcome, Some(Ok(()))) {
                active.completed += 1;
                unflushed += 1;
                task.index += 1;
            }
            let done = task.index == end;
            let finished = done || control.cancelled() || !matches!(outcome, Some(Ok(())));
            if finished || unflushed >= 10 || checkpoint.elapsed() >= Duration::from_secs(2) {
                let failure = outcome.and_then(Result::err);
                let mut first = true;
                loop {
                    let result = self.settle(&mut state, &task.job_id, done, failure.as_ref());
                    match result {
                        Ok(true) => break,
                        Ok(false) => return state,
                        Err(error) => {
                            if first {
                                eprintln!("Download checkpoint will retry: {error}");
                                first = false;
                            }
                            state = self.retry(state);
                            if self.stopped() {
                                return state;
                            }
                        }
                    }
                }
                unflushed = 0;
                checkpoint = Instant::now();
            }
            if finished {
                return state;
            }
        }
    }

    fn settle(
        &self,
        state: &mut State,
        id: &str,
        done: bool,
        failure: Option<&DownloadFailure>,
    ) -> Result<bool, ApiError> {
        let mut db = self.database()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let Some(mut job) = Job::find(&tx, id)? else {
            return Ok(false);
        };
        let active = &state.active[id];
        job.data.completed = active.completed;
        let mut verification = None;
        let mut acknowledge = None;
        if job.cancelled || job.status == "cancelled" {
            job.status = "cancelled".into();
        } else if matches!(job.status.as_str(), "running" | "paused" | "queued") {
            match failure {
                Some(DownloadFailure::Verification {
                    key,
                    outcome,
                    captcha,
                    message,
                }) => {
                    if let Some(&verified) = outcome.get()
                        && !state
                            .verifications
                            .values()
                            .any(|waiter| &waiter.key == key)
                    {
                        resolve_job(&mut job, verified);
                        acknowledge = Some(key);
                    } else {
                        if job.status != "paused" {
                            job.status = "verification_required".into();
                        }
                        job.data.error.clone_from(message);
                        verification = Some(Verification {
                            book_id: job.book_id.clone(),
                            key: key.clone(),
                            captcha: captcha.as_ref().clone(),
                            resolution: outcome.get().copied(),
                        });
                    }
                }
                Some(DownloadFailure::Error(error))
                    if error.code() == Some("request_cancelled")
                        && active.interrupted.load(Ordering::Acquire) => {}
                Some(DownloadFailure::Error(error)) => {
                    job.status = "failed".into();
                    job.data.error.clone_from(&error.message);
                    job.data.error_code = error.code().map(str::to_owned);
                }
                Some(DownloadFailure::Access {
                    message,
                    code,
                    access,
                }) => {
                    job.status = "failed".into();
                    job.data.error.clone_from(message);
                    job.data.error_code = Some((*code).into());
                    job.data.access = Some(access.clone());
                }
                None if done && job.status == "running" => job.status = "completed".into(),
                None => {}
            }
        } else {
            return Ok(false);
        }
        job.save(&tx)?;
        tx.commit()?;
        if let Some(key) = acknowledge
            && let Some(verified) = self.verified.get()
        {
            verified(key);
        }
        if let Some(waiting) = verification {
            if let Some(verified) = waiting.resolution {
                for peer in state
                    .verifications
                    .values_mut()
                    .filter(|peer| peer.key == waiting.key)
                {
                    peer.resolution = Some(verified);
                }
            }
            state.verifications.insert(job.id, waiting);
        }
        Ok(job.status == "running")
    }

    fn apply_verification(&self, state: &mut State, key: &str) -> Result<(), ApiError> {
        let jobs: Vec<_> = state
            .verifications
            .iter()
            .filter_map(|(id, waiting)| {
                (waiting.key == key)
                    .then_some(waiting.resolution)
                    .flatten()
                    .map(|verified| (id.clone(), verified))
            })
            .collect();
        if jobs.is_empty() {
            if let Some(verified) = self.verified.get() {
                verified(key);
            }
            return Ok(());
        }
        let mut db = self.database()?;
        let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
        for (id, verified) in &jobs {
            let Some(mut job) = Job::find(&tx, id)? else {
                continue;
            };
            if job.cancelled || !matches!(job.status.as_str(), "paused" | "verification_required") {
                continue;
            }
            resolve_job(&mut job, *verified);
            job.save(&tx)?;
        }
        tx.commit()?;
        for (id, _) in jobs {
            state.verifications.remove(&id);
        }
        if let Some(verified) = self.verified.get() {
            verified(key);
        }
        Ok(())
    }
}

fn resolve_job(job: &mut Job, verified: bool) {
    if job.status != "paused" {
        job.status = if verified { "queued" } else { "failed" }.into();
    }
    job.data.clear_error();
    if !verified {
        job.data.error =
            "Đã hủy xác minh. Các chương đã tải vẫn được giữ; có thể thử lại phần còn lại.".into();
        job.data.error_code = Some("captcha_cancelled".into());
    }
}

fn create(db: &Connection, book: &str, start: i64, count: i64) -> Result<Job, ApiError> {
    let (name, chapter_count, trashed): (String, i64, f64) = db
        .query_row(
            "SELECT name,chapter_count,trashed_at FROM books WHERE id=?",
            [book],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?
        .ok_or_else(|| ApiError::new(404, "Không tìm thấy truyện."))?;
    if start < 0 || start >= chapter_count || count == 0 || count < -1 {
        return Err(ApiError::new(400, "Khoảng chương không hợp lệ."));
    }
    if trashed != 0.0 {
        return Err(ApiError::new(
            400,
            "Hãy khôi phục truyện khỏi thùng rác trước khi tải.",
        ));
    }
    if db.query_row(
        &format!("SELECT EXISTS(SELECT 1 FROM download_jobs WHERE book_id=? AND {ACTIVE})"),
        [book],
        |row| row.get::<_, bool>(0),
    )? {
        return Err(ApiError::new(400, "Truyện đang có trong hàng đợi tải."));
    }
    let end = if count == -1 {
        chapter_count
    } else {
        start.saturating_add(count).min(chapter_count)
    };
    let mut bytes = [0; 16];
    getrandom::fill(&mut bytes).map_err(|_| internal())?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    let id = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    let job = Job {
        id,
        book_id: book.into(),
        status: "queued".into(),
        cancelled: false,
        data: Checkpoint {
            name,
            start,
            end,
            total: end - start,
            ..Checkpoint::default()
        },
    };
    db.execute("INSERT INTO download_jobs(id,book_id,status,cancelled,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", params![job.id, job.book_id, job.status, job.cancelled, job.encoded()?, now(), now()])?;
    Ok(job)
}

fn list(library: &Library, offset: i64, state: Option<&State>) -> Result<Value, ApiError> {
    let db = library.reader_connection()?;
    let offset = offset.max(0);
    let mut query = db.prepare("SELECT id,book_id,status,cancelled,data FROM download_jobs ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?")?;
    let mut jobs = query
        .query_map(params![PAGE + 1, offset], Job::read)?
        .map(|row| row.map(|job| job.value(state)))
        .collect::<Result<Vec<_>, _>>()?;
    let next_offset = (jobs.len() > PAGE as usize).then(|| offset.saturating_add(PAGE));
    jobs.truncate(PAGE as usize);
    Ok(json!({"jobs": jobs,"next_offset":next_offset}))
}

fn invalid_action() -> ApiError {
    ApiError::new(400, "Không thể thực hiện thao tác với trạng thái hiện tại.")
}

fn internal() -> ApiError {
    ApiError::new(500, "Có lỗi khi xử lý. Dữ liệu gốc chưa bị thay đổi.")
}
