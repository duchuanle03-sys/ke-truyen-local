//! Progress-triggered next-chapter prefetch coordination: memory only, one active and one
//! pending prefetch for the whole app.
//!
//! A prefetch runs on the HTTP request thread that asked for it, so the server's existing
//! request permits and shutdown drain bound and finish it. Locks are held only briefly and
//! never across network or SQLite work.
use std::collections::HashMap;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant};

/// Minimum gap between prefetches to the same QQ, Truyện Full or Hako source. Reader requests
/// are never delayed by it; Sáng Tác Việt keeps its own 10 s pacing instead.
pub(crate) const SPACING: Duration = if cfg!(test) {
    Duration::from_millis(20)
} else {
    Duration::from_secs(2)
};
/// How many of the next chapter's images (manga pages or illustrations) are prefetched.
pub(crate) const IMAGES: usize = 10;
const POLL: Duration = Duration::from_millis(100);

pub(crate) type Key = (String, i64);

#[derive(Clone, Copy, PartialEq)]
enum Phase {
    /// Waiting for the single prefetch slot.
    Pending,
    /// Holding the slot, waiting out the per-source spacing; nothing has been sent.
    Spacing,
    /// Fetching from the source.
    Fetching,
    /// The chapter is saved; fetching its first images. Readers never wait for this phase.
    Images,
}

struct Slot {
    key: Key,
    ticket: u64,
    phase: Phase,
    cancelled: Arc<AtomicBool>,
    promoted: Arc<AtomicBool>,
}

#[derive(Default)]
struct State {
    pending: Option<Slot>,
    active: Option<Slot>,
    sent: HashMap<String, Instant>,
    next: u64,
    /// The chapter the reader last opened; only the one after it is worth prefetching.
    opened: Option<Key>,
}

#[derive(Default)]
pub(crate) struct Prefetch {
    state: Mutex<State>,
    changed: Condvar,
}

/// The running prefetch; dropping it frees the slot, on success, failure or cancellation.
pub(crate) struct Fetching<'a> {
    owner: &'a Prefetch,
    ticket: u64,
    cancelled: Arc<AtomicBool>,
    promoted: Arc<AtomicBool>,
}

impl Fetching<'_> {
    /// False once a reader opened this chapter: the request then uses the foreground lane.
    pub(crate) fn background(&self) -> bool {
        !self.promoted.load(Ordering::Acquire)
    }
    /// True once the reader moved to an unrelated chapter or book.
    pub(crate) fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::Acquire)
    }
}

impl Fetching<'_> {
    /// Marks the chapter as saved: a reader opening it no longer waits, and stops the images.
    pub(crate) fn start_images(&self) {
        let mut state = self.owner.lock();
        if let Some(slot) = state
            .active
            .as_mut()
            .filter(|slot| slot.ticket == self.ticket)
        {
            slot.phase = Phase::Images;
        }
        self.owner.changed.notify_all();
    }

    /// Waits out the per-source gap before another background request and records it.
    /// Returns false, without waiting further, once cancelled or stopping.
    pub(crate) fn pace(
        &self,
        source: &str,
        spacing: Duration,
        stopping: &dyn Fn() -> bool,
    ) -> bool {
        let mut state = self.owner.lock();
        loop {
            if stopping() || self.cancelled() {
                return false;
            }
            let now = Instant::now();
            match state.sent.get(source).map(|sent| *sent + spacing) {
                Some(due) if due > now => state = self.owner.wait(state, (due - now).min(POLL)),
                _ => {
                    state.sent.insert(source.to_owned(), now);
                    return true;
                }
            }
        }
    }
}

impl Drop for Fetching<'_> {
    fn drop(&mut self) {
        let mut state = self.owner.lock();
        if state
            .active
            .as_ref()
            .is_some_and(|slot| slot.ticket == self.ticket)
        {
            state.active = None;
        }
        self.owner.changed.notify_all();
    }
}

impl Prefetch {
    fn lock(&self) -> MutexGuard<'_, State> {
        self.state
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    fn wait<'a>(&self, state: MutexGuard<'a, State>, timeout: Duration) -> MutexGuard<'a, State> {
        self.changed
            .wait_timeout(state, timeout)
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .0
    }

    /// Queues a prefetch and waits for its turn. `None` means it should not run: the chapter
    /// is already queued or running, it was replaced or cancelled, or the app is stopping.
    pub(crate) fn begin(
        &self,
        key: Key,
        source: &str,
        spacing: Duration,
        stopping: &dyn Fn() -> bool,
    ) -> Option<Fetching<'_>> {
        let mut state = self.lock();
        let live = |slot: &Option<Slot>| {
            slot.as_ref()
                .is_some_and(|slot| slot.key == key && !slot.cancelled.load(Ordering::Acquire))
        };
        // A request that arrives after the reader moved on (or a duplicate of the chapter it just
        // opened) is stale.
        let stale = state
            .opened
            .as_ref()
            .is_some_and(|(book, index)| *book != key.0 || *index + 1 != key.1);
        if stale || live(&state.pending) || live(&state.active) {
            return None;
        }
        // Only the newest request is worth keeping: older queued or spacing ones are stale.
        if let Some(old) = state.pending.take() {
            old.cancelled.store(true, Ordering::Release);
        }
        if let Some(active) = state
            .active
            .as_ref()
            .filter(|slot| matches!(slot.phase, Phase::Spacing | Phase::Images))
        {
            active.cancelled.store(true, Ordering::Release);
        }
        let ticket = state.next;
        state.next += 1;
        let (cancelled, promoted) = (
            Arc::new(AtomicBool::new(false)),
            Arc::new(AtomicBool::new(false)),
        );
        state.pending = Some(Slot {
            key,
            ticket,
            phase: Phase::Pending,
            cancelled: cancelled.clone(),
            promoted: promoted.clone(),
        });
        self.changed.notify_all();
        let mine = |slot: &Option<Slot>| slot.as_ref().is_some_and(|slot| slot.ticket == ticket);
        loop {
            if stopping() || cancelled.load(Ordering::Acquire) {
                if mine(&state.pending) {
                    state.pending = None;
                }
                self.changed.notify_all();
                return None;
            }
            if state.active.is_none() && mine(&state.pending) {
                let mut slot = state.pending.take().expect("pending slot");
                slot.phase = Phase::Spacing;
                state.active = Some(slot);
                break;
            }
            state = self.wait(state, POLL);
        }
        let due = state.sent.get(source).map(|sent| *sent + spacing);
        loop {
            if stopping() || cancelled.load(Ordering::Acquire) {
                if mine(&state.active) {
                    state.active = None;
                }
                self.changed.notify_all();
                return None;
            }
            let now = Instant::now();
            match due {
                Some(due) if due > now => state = self.wait(state, (due - now).min(POLL)),
                _ => break,
            }
        }
        state.active.as_mut().expect("active slot").phase = Phase::Fetching;
        if !spacing.is_zero() {
            state.sent.insert(source.to_owned(), Instant::now());
        }
        Some(Fetching {
            owner: self,
            ticket,
            cancelled,
            promoted,
        })
    }

    /// Called before a reader request for `book`/`index`. A queued or spacing prefetch of that
    /// chapter is dropped (the reader fetches it now); a running one is promoted and awaited, so
    /// its result is reused. Prefetches for unrelated chapters are cancelled; one already sending
    /// to a source other than Sáng Tác Việt simply finishes.
    pub(crate) fn claim(&self, book: &str, index: i64) {
        let mut state = self.lock();
        state.opened = Some((book.to_owned(), index));
        let target = |slot: &Slot| slot.key.0 == book && slot.key.1 == index;
        // The chapter being read (the one before the target) keeps its prefetch.
        let related =
            |slot: &Slot| slot.key.0 == book && (slot.key.1 == index || slot.key.1 == index + 1);
        if state
            .pending
            .as_ref()
            .is_some_and(|slot| target(slot) || !related(slot))
        {
            let slot = state.pending.take().expect("pending slot");
            slot.cancelled.store(true, Ordering::Release);
        }
        let mut awaited = None;
        if let Some(active) = &state.active {
            if target(active) && active.phase == Phase::Fetching {
                active.cancelled.store(false, Ordering::Release);
                active.promoted.store(true, Ordering::Release);
                awaited = Some(active.ticket);
            } else if !related(active) || target(active) {
                active.cancelled.store(true, Ordering::Release);
            }
        }
        self.changed.notify_all();
        // Wait only until the chapter is saved; its image phase is never waited for.
        if let Some(ticket) = awaited {
            while let Some(slot) = state.active.as_ref().filter(|slot| slot.ticket == ticket) {
                if slot.phase == Phase::Images {
                    slot.cancelled.store(true, Ordering::Release);
                    break;
                }
                state = self.wait(state, POLL);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::mpsc;
    use std::thread;

    const FAR: Duration = Duration::from_secs(2);
    fn key(book: &str, index: i64) -> Key {
        (book.to_owned(), index)
    }
    fn never() -> bool {
        false
    }
    fn settle() {
        thread::sleep(Duration::from_millis(250));
    }

    #[test]
    fn a_late_request_for_anything_but_the_chapter_after_the_open_one_is_dropped() {
        let prefetch = Prefetch::default();
        prefetch.claim("b", 5);
        drop(
            prefetch
                .begin(key("b", 6), "stv", Duration::ZERO, &never)
                .unwrap(),
        );
        // The reader already moved on to 6 (or another book) before the request arrived.
        prefetch.claim("b", 6);
        assert!(
            prefetch
                .begin(key("b", 6), "stv", Duration::ZERO, &never)
                .is_none()
        );
        assert!(
            prefetch
                .begin(key("other", 1), "stv", Duration::ZERO, &never)
                .is_none()
        );
        assert!(
            prefetch
                .begin(key("b", 7), "stv", Duration::ZERO, &never)
                .is_some()
        );
    }

    #[test]
    fn opening_the_chapter_while_its_images_load_never_waits_and_stops_them() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 2), "qq", Duration::ZERO, &never)
            .unwrap();
        running.start_images();
        let started = Instant::now();
        prefetch.claim("b", 2);
        assert!(
            started.elapsed() < Duration::from_millis(50),
            "the chapter is already saved"
        );
        assert!(
            running.cancelled(),
            "the reader loads the remaining images itself"
        );
    }

    #[test]
    fn a_reader_waiting_for_the_chapter_is_released_when_it_is_saved() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 2), "qq", Duration::ZERO, &never)
            .unwrap();
        let (send, rx) = mpsc::channel();
        thread::scope(|scope| {
            scope.spawn(|| {
                prefetch.claim("b", 2);
                send.send(()).unwrap();
            });
            settle();
            assert!(rx.try_recv().is_err(), "waits while the chapter is fetched");
            running.start_images();
            rx.recv_timeout(Duration::from_millis(500))
                .expect("not held through the images");
            assert!(running.cancelled(), "the reader loads the images itself");
        });
    }

    #[test]
    fn image_requests_keep_the_per_source_gap_and_stop_when_cancelled() {
        let prefetch = Prefetch::default();
        let gap = Duration::from_millis(300);
        let running = prefetch.begin(key("b", 2), "qq", gap, &never).unwrap();
        running.start_images();
        let started = Instant::now();
        assert!(running.pace("qq", gap, &never));
        assert!(
            started.elapsed() >= Duration::from_millis(250),
            "gap after the chapter request"
        );
        let started = Instant::now();
        assert!(running.pace("qq", gap, &never));
        assert!(
            started.elapsed() >= Duration::from_millis(250),
            "gap between images"
        );
        prefetch.claim("other", 0);
        assert!(!running.pace("qq", gap, &never), "cancelled while waiting");
    }

    #[test]
    fn a_chapter_already_queued_or_running_is_not_prefetched_twice() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 2), "full", Duration::ZERO, &never)
            .unwrap();
        assert!(
            prefetch
                .begin(key("b", 2), "full", Duration::ZERO, &never)
                .is_none()
        );
        drop(running);
    }

    #[test]
    fn one_runs_at_a_time_and_a_newer_request_replaces_a_stale_pending_one() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 1), "full", Duration::ZERO, &never)
            .unwrap();
        let (send, rx) = mpsc::channel();
        thread::scope(|scope| {
            for index in [5, 7] {
                let (prefetch, send) = (&prefetch, send.clone());
                scope.spawn(move || {
                    let turn = prefetch.begin(key("b", index), "full", Duration::ZERO, &never);
                    send.send((index, turn.is_some())).unwrap();
                });
                settle();
            }
            assert_eq!(rx.recv_timeout(FAR).unwrap(), (5, false), "superseded");
            assert!(rx.try_recv().is_err(), "7 waits for the running prefetch");
            drop(running);
            assert_eq!(rx.recv_timeout(FAR).unwrap(), (7, true));
        });
    }

    #[test]
    fn opening_a_queued_chapter_takes_it_over_without_waiting() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 1), "full", Duration::ZERO, &never)
            .unwrap();
        thread::scope(|scope| {
            let queued = scope.spawn(|| {
                prefetch
                    .begin(key("b", 2), "full", Duration::ZERO, &never)
                    .is_some()
            });
            settle();
            prefetch.claim("b", 2);
            assert!(
                !queued.join().unwrap(),
                "the reader fetches it in the foreground"
            );
        });
        drop(running);
    }

    #[test]
    fn opening_a_running_chapter_promotes_it_and_waits_for_it() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 2), "stv", Duration::ZERO, &never)
            .unwrap();
        assert!(running.background());
        let (send, rx) = mpsc::channel();
        thread::scope(|scope| {
            scope.spawn(|| {
                prefetch.claim("b", 2);
                send.send(()).unwrap();
            });
            settle();
            assert!(!running.background(), "promoted to the foreground lane");
            assert!(rx.try_recv().is_err(), "reuses the running request");
            drop(running);
            rx.recv_timeout(FAR).unwrap();
        });
    }

    #[test]
    fn unrelated_navigation_cancels_but_rereading_the_current_chapter_does_not() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 2), "stv", Duration::ZERO, &never)
            .unwrap();
        prefetch.claim("b", 1);
        assert!(!running.cancelled());
        prefetch.claim("other", 2);
        assert!(running.cancelled());
        drop(running);
        // The slot is free again after a cancelled prefetch.
        assert!(
            prefetch
                .begin(key("other", 3), "stv", Duration::ZERO, &never)
                .is_some()
        );
    }

    #[test]
    fn prefetches_to_one_source_are_spaced_but_other_sources_are_not() {
        let prefetch = Prefetch::default();
        let spacing = Duration::from_millis(400);
        drop(
            prefetch
                .begin(key("b", 1), "full", spacing, &never)
                .unwrap(),
        );
        let started = Instant::now();
        drop(
            prefetch
                .begin(key("b", 2), "full", spacing, &never)
                .unwrap(),
        );
        assert!(
            started.elapsed() >= Duration::from_millis(350),
            "{:?}",
            started.elapsed()
        );
        let started = Instant::now();
        drop(
            prefetch
                .begin(key("c", 1), "hako", spacing, &never)
                .unwrap(),
        );
        assert!(started.elapsed() < Duration::from_millis(200));
    }

    #[test]
    fn opening_a_chapter_still_waiting_for_spacing_cancels_the_prefetch() {
        let prefetch = Prefetch::default();
        drop(prefetch.begin(key("b", 1), "full", FAR, &never).unwrap());
        let started = Instant::now();
        thread::scope(|scope| {
            let waiting =
                scope.spawn(|| prefetch.begin(key("b", 2), "full", FAR, &never).is_some());
            settle();
            prefetch.claim("b", 2);
            assert!(!waiting.join().unwrap());
        });
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn shutdown_cancels_queued_prefetches() {
        let prefetch = Prefetch::default();
        let running = prefetch
            .begin(key("b", 1), "full", Duration::ZERO, &never)
            .unwrap();
        let stopping = std::sync::atomic::AtomicBool::new(false);
        thread::scope(|scope| {
            let queued = scope.spawn(|| {
                let stop = || stopping.load(Ordering::Acquire);
                prefetch
                    .begin(key("b", 2), "full", Duration::ZERO, &stop)
                    .is_some()
            });
            settle();
            stopping.store(true, Ordering::Release);
            assert!(!queued.join().unwrap());
        });
        drop(running);
    }
}
