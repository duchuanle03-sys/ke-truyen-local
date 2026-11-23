pub mod casefold;
pub mod http;
pub mod library;
pub use library::{ApiError, Library, ShelfPage, ShelfQuery};

mod assets;
mod chapter_cache;
mod chapters;
mod entities;
mod lifecycle;
mod reader;
mod schema;
mod search;
#[cfg(windows)]
mod windows_metadata;
