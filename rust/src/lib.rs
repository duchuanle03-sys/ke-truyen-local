pub mod casefold;
pub mod http;
pub mod library;
pub use library::{ApiError, Library, ShelfPage, ShelfQuery};

mod entities;
mod lifecycle;
mod reader;
#[cfg(windows)]
mod windows_metadata;
