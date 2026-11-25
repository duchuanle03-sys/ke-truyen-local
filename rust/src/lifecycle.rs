use crate::ApiError;
use std::{
    collections::HashMap,
    fs::File,
    hash::Hash,
    path::Path,
    sync::{
        Arc, Mutex, MutexGuard,
        atomic::{AtomicBool, AtomicU8, Ordering},
    },
};

pub(crate) struct ShutdownAck {
    state: AtomicU8,
    stop_listener: Arc<tokio::sync::Notify>,
}

impl ShutdownAck {
    pub(crate) fn new(stop_listener: Arc<tokio::sync::Notify>) -> Self {
        Self {
            state: AtomicU8::new(0),
            stop_listener,
        }
    }

    pub(crate) fn accepted(&self) {
        self.mark(1, 2);
    }

    pub(crate) fn closed(&self) {
        self.mark(2, 1);
    }

    fn mark(&self, ready: u8, other: u8) {
        if self.state.fetch_or(ready, Ordering::AcqRel) == other {
            self.stop_listener.notify_one();
        }
    }
}

type Counts = Vec<(&'static str, usize)>;

#[derive(Default)]
pub(crate) struct CriticalOperations(Arc<Mutex<Counts>>);

impl CriticalOperations {
    pub(crate) fn lock(&self) -> MutexGuard<'_, Counts> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    pub(crate) fn enter(
        &self,
        name: &'static str,
        stopping: &AtomicBool,
    ) -> Result<CriticalOperation, ApiError> {
        let mut counts = self.lock();
        if stopping.load(Ordering::Acquire) {
            return Err(
                ApiError::new(503, "Máy chủ đang tắt; không nhận thao tác ghi mới.")
                    .with_code("shutting_down"),
            );
        }
        if let Some((_, count)) = counts.iter_mut().find(|(operation, _)| name == *operation) {
            *count += 1;
        } else {
            counts.push((name, 1));
        }
        Ok(CriticalOperation {
            operations: Self(self.0.clone()),
            name,
        })
    }
}

pub(crate) struct CriticalOperation {
    operations: CriticalOperations,
    name: &'static str,
}

impl Drop for CriticalOperation {
    fn drop(&mut self) {
        let mut counts = self.operations.lock();
        if let Some(index) = counts.iter().position(|(name, _)| *name == self.name) {
            counts[index].1 -= 1;
            if counts[index].1 == 0 {
                counts.remove(index);
            }
        }
    }
}

pub(crate) struct Leases<K>(Arc<Mutex<HashMap<K, usize>>>);

impl<K> Default for Leases<K> {
    fn default() -> Self {
        Self(Arc::new(Mutex::new(HashMap::new())))
    }
}

impl<K: Eq + Hash + Clone> Leases<K> {
    pub(crate) fn lock(&self) -> MutexGuard<'_, HashMap<K, usize>> {
        self.0
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    pub(crate) fn acquire(&self, key: K) -> Lease<K> {
        *self.lock().entry(key.clone()).or_default() += 1;
        Lease {
            leases: Self(self.0.clone()),
            key,
        }
    }
}

pub(crate) struct Lease<K: Eq + Hash + Clone> {
    leases: Leases<K>,
    key: K,
}

impl<K: Eq + Hash + Clone> Drop for Lease<K> {
    fn drop(&mut self) {
        let mut active = self.leases.lock();
        if let Some(count) = active.get_mut(&self.key) {
            *count -= 1;
            if *count == 0 {
                active.remove(&self.key);
            }
        }
    }
}

pub fn library_lock(root: &Path) -> Result<File, ApiError> {
    let path = root.join(".server.lock");
    if path.exists() {
        crate::library::checked_path(&path)?;
    }
    let file = File::options()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)?;
    file.try_lock().map_err(|_| {
        ApiError::new(
            400,
            "Thư viện đang được một máy chủ khác sử dụng hoặc đang hoàn tất tắt.",
        )
        .with_code("library_busy")
    })?;
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn shutdown_waits_for_commit_and_close_in_either_order() {
        let runtime = tokio::runtime::Builder::new_current_thread()
            .enable_time()
            .build()
            .unwrap();
        runtime.block_on(async {
            for (first, second) in [
                (
                    ShutdownAck::accepted as fn(&ShutdownAck),
                    ShutdownAck::closed as fn(&ShutdownAck),
                ),
                (ShutdownAck::closed, ShutdownAck::accepted),
            ] {
                let listener = Arc::new(tokio::sync::Notify::new());
                let ack = ShutdownAck::new(listener.clone());
                first(&ack);
                assert!(
                    tokio::time::timeout(std::time::Duration::ZERO, listener.notified())
                        .await
                        .is_err()
                );
                second(&ack);
                assert!(
                    tokio::time::timeout(std::time::Duration::ZERO, listener.notified())
                        .await
                        .is_ok()
                );
            }
        });
    }
    #[test]
    fn lock_excludes_another_owner_until_drop() {
        let root = std::env::temp_dir().join(format!("reader-m2-lock-{}", std::process::id()));
        std::fs::create_dir_all(&root).unwrap();
        let first = library_lock(&root).unwrap();
        assert!(library_lock(&root).is_err());
        drop(first);
        assert!(library_lock(&root).is_ok());
        std::fs::remove_dir_all(root).unwrap();
    }
}
