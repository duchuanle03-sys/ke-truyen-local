use crate::ApiError;
use std::{fs::File, path::Path};

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
    })?;
    Ok(file)
}

#[cfg(test)]
mod tests {
    use super::*;
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
