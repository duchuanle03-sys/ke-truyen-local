//! Stable Rust does not expose Windows ChangeTime, required by Python's ETag.

use std::{fs::File, io, mem::size_of, os::windows::io::AsRawHandle};
use windows_sys::Win32::Storage::FileSystem::{
    FILE_BASIC_INFO, FILE_ID_INFO, FileBasicInfo, FileIdInfo, GetFileInformationByHandleEx,
};

pub(crate) fn identity(file: &File) -> io::Result<(u128, i128)> {
    let mut basic = FILE_BASIC_INFO::default();
    let mut id = FILE_ID_INFO::default();
    // SAFETY: file owns a live borrowed handle throughout both synchronous calls.
    // Each writable, aligned buffer matches its information class and exact size;
    // Windows retains no pointer. Failed calls return before reading their output.
    unsafe {
        if GetFileInformationByHandleEx(
            file.as_raw_handle(),
            FileBasicInfo,
            (&raw mut basic).cast(),
            size_of::<FILE_BASIC_INFO>() as u32,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
        if GetFileInformationByHandleEx(
            file.as_raw_handle(),
            FileIdInfo,
            (&raw mut id).cast(),
            size_of::<FILE_ID_INFO>() as u32,
        ) == 0
        {
            return Err(io::Error::last_os_error());
        }
    }
    Ok((
        u128::from_le_bytes(id.FileId.Identifier),
        (i128::from(basic.ChangeTime) - 116_444_736_000_000_000) * 100,
    ))
}
