use crate::{ApiError, Library, library::checked_path, qt::entry};
use rusqlite::{Connection, OpenFlags, TransactionBehavior, params};
use serde::Serialize;
use serde_json::json;
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    fs::{self, File},
    io::{Cursor, Read, Write},
    path::Path,
    time::Duration,
};
use zip::{
    CompressionMethod, ZipArchive, ZipReadOptions, read::Config, unstable::read::ZipIterable,
};

const MIB: u64 = 1024 * 1024;
const MEMBERS: [(&str, &str); 4] = [
    ("VP.dic", "vietphrase"),
    ("NE.dic", "name"),
    ("HV.dic", "hanviet"),
    ("Pronouns.dic", "pronouns"),
];
const KNOWN_SHA: &str = "8132f987fd01db53d0f6cef199307d05a83da6458d05d02b940c2d4bd74e0c40";
const SOURCE: &str =
    "https://raw.githubusercontent.com/Darkrai9x/vbook-settings/main/translate2.zip";

fn invalid(message: impl Into<String>) -> ApiError {
    ApiError::new(400, message)
}

fn count(data: &mut &[u8], max: usize) -> Result<usize, ApiError> {
    let (header, rest) = data
        .split_first_chunk::<4>()
        .ok_or_else(|| invalid("Từ điển bị thiếu header."))?;
    *data = rest;
    let count = u32::from_be_bytes(*header) as usize;
    if !(1..=max).contains(&count) {
        return Err(invalid("Số mục hoặc kích thước trie không hợp lệ."));
    }
    Ok(count)
}

fn columns<'a>(filename: &str, mut data: &'a [u8]) -> Result<(usize, &'a str), ApiError> {
    if filename != "HV.dic" {
        let mut sizes = [0; 2];
        for size in &mut sizes {
            *size = count(&mut data, 4_000_000)?;
            data = data
                .get(*size * 4..)
                .ok_or_else(|| invalid("Từ điển bị thiếu mảng trie."))?;
        }
        if sizes[0] != sizes[1] {
            return Err(invalid("Hai mảng trie có độ dài khác nhau."));
        }
    }
    let size = count(&mut data, 1_000_000)?;
    let text = std::str::from_utf8(data)
        .map_err(|_| invalid("Nội dung từ điển không phải UTF-8 hợp lệ."))?;
    let lines = text.split_terminator('\n').count();
    if lines != size * 2 {
        return Err(invalid(format!(
            "Từ điển hỏng: cần {} dòng nghĩa/từ, nhận {lines}; không đoán ghép các cặp.",
            size * 2
        )));
    }
    Ok((size, text))
}

fn unzip(archive: &mut ZipArchive<Cursor<&[u8]>>, filename: &str) -> Result<Vec<u8>, ApiError> {
    let index = archive
        .index_for_name(filename)
        .ok_or_else(|| invalid("Gói chưa có từ điển này."))?;
    let zip_error = |error: zip::result::ZipError| invalid(error.to_string());
    let mut raw = archive.by_index_raw(index).map_err(zip_error)?;
    if raw.compression() == CompressionMethod::Lzma {
        let mut header = [0; 9];
        raw.read_exact(&mut header)?;
        if u32::from_le_bytes(header[5..].try_into().expect("four bytes")) as u64 > 64 * MIB {
            return Err(invalid("Từ điển vượt giới hạn 64 MiB."));
        }
    }
    drop(raw);
    let member = archive
        .by_index_with_options(
            index,
            ZipReadOptions::new().enforce_header_consistency(true),
        )
        .map_err(zip_error)?;
    let size = member.size() as usize;
    let mut bytes = Vec::with_capacity(size);
    member
        .take(64 * MIB + 1)
        .read_to_end(&mut bytes)
        .map_err(|error| {
            if error.to_string().contains("CRC") {
                invalid(format!("Bad CRC-32 for file '{filename}'"))
            } else {
                error.into()
            }
        })?;
    if bytes.len() != size {
        return Err(invalid("Kích thước từ điển trong ZIP không khớp."));
    }
    Ok(bytes)
}

#[derive(Default, Serialize)]
struct Summary {
    kind: &'static str,
    declared: usize,
    inserted: usize,
    skipped_existing: usize,
    rejected: usize,
    warnings: Vec<String>,
}

pub(crate) fn install(library: &Library, path: &Path) -> Result<(), ApiError> {
    let file_error = || invalid("Gói từ điển phải là tệp ZIP tối đa 32 MiB.");
    let path = checked_path(path).map_err(|_| file_error())?;
    let file = File::open(path).map_err(|_| file_error())?;
    let metadata = file.metadata()?;
    if !metadata.is_file() || metadata.len() > 32 * MIB {
        return Err(file_error());
    }
    let mut bytes = Vec::new();
    file.take(32 * MIB + 1).read_to_end(&mut bytes)?;
    if bytes.len() as u64 > 32 * MIB {
        return Err(file_error());
    }
    let zip_error = |_| invalid("Không mở được ZIP từ điển.");
    let mut directory = ZipIterable::try_new(Cursor::new(bytes.as_slice()), Config::default())
        .map_err(zip_error)?;
    let mut names = HashSet::new();
    let mut total = 0u64;
    for file in directory.files().map_err(zip_error)? {
        let file = file.map_err(zip_error)?;
        let name = file.name().map_err(zip_error)?;
        if !names.insert(name.into_owned()) {
            return Err(invalid("ZIP thiếu từ điển chính hoặc chứa tên tệp trùng."));
        }
        total = total.saturating_add(file.size());
        if total > 80 * MIB || file.size() > 64 * MIB || file.encrypted() {
            return Err(invalid("ZIP từ điển quá lớn hoặc bị mã hóa."));
        }
    }
    if MEMBERS[..3].iter().any(|(name, _)| !names.contains(*name)) {
        return Err(invalid("ZIP thiếu từ điển chính hoặc chứa tên tệp trùng."));
    }
    let sha256 = format!("{:x}", Sha256::digest(&bytes));
    let mut archive = ZipArchive::new(Cursor::new(bytes.as_slice())).map_err(zip_error)?;
    let mut inputs = Vec::with_capacity(MEMBERS.len());
    for (name, _) in MEMBERS {
        let parsed = unzip(&mut archive, name).and_then(|data| {
            let (size, text) = columns(name, &data)?;
            let start = data.len() - text.len();
            Ok((size, data, start))
        });
        if name != "Pronouns.dic"
            && let Err(error) = &parsed
        {
            return Err(invalid(format!("{name}: {error}")));
        }
        inputs.push(parsed);
    }

    let dictionary = library.qt()?;
    let mut db = Connection::open_with_flags(dictionary.path(), OpenFlags::SQLITE_OPEN_READ_WRITE)?;
    db.busy_timeout(Duration::from_secs(30))?;
    let tx = db.transaction_with_behavior(TransactionBehavior::Immediate)?;
    let mut files = serde_json::Map::new();
    let mut pending = Vec::new();
    {
        let mut insert = tx.prepare(
            "INSERT OR IGNORE INTO entries(kind,word,meaning,scope) VALUES(?1,?2,?3,?4)",
        )?;
        for ((name, kind), input) in MEMBERS.into_iter().zip(inputs) {
            let mut summary = Summary {
                kind,
                ..Default::default()
            };
            match input {
                Ok((size, bytes, start)) => {
                    summary.declared = size;
                    let lines = std::str::from_utf8(&bytes[start..])
                        .expect("validated columns")
                        .split_terminator('\n');
                    for (index, (meaning, word)) in lines.clone().zip(lines.skip(size)).enumerate()
                    {
                        let meaning = meaning.strip_suffix('\r').unwrap_or(meaning);
                        let word = word.strip_suffix('\r').unwrap_or(word);
                        match entry(kind, word, meaning, "*") {
                            Ok(row) => {
                                let added = insert.execute(params![
                                    row.kind,
                                    row.word,
                                    row.meaning,
                                    row.scope
                                ])?;
                                summary.inserted += added;
                                summary.skipped_existing += 1 - added;
                            }
                            Err(error) => {
                                summary.rejected += 1;
                                if summary.warnings.len() < 10 {
                                    summary.warnings.push(format!("Mục {}: {error}", index + 1));
                                }
                            }
                        }
                    }
                }
                Err(error) => {
                    summary.warnings.push(error.message);
                    pending.push(kind);
                }
            }
            files.insert(name.into(), json!(summary));
        }
    }
    tx.commit()?;
    files.insert(
        "Rule.dic".into(),
        json!(Summary {
            kind: "rules",
            warnings: vec![
                "Giữ Rule.dic trong ZIP; cú pháp luật vBook chưa tương thích nên chưa nhập.".into()
            ],
            ..Default::default()
        }),
    );
    pending.push("rules");
    let report = json!({"source_url":(sha256 == KNOWN_SHA).then_some(SOURCE), "sha256":sha256, "files":files,"pending":pending});
    write_report(library.root(), &report)
}

fn write_report(root: &Path, report: &serde_json::Value) -> Result<(), ApiError> {
    let mut nonce = [0; 16];
    getrandom::fill(&mut nonce).map_err(|error| invalid(error.to_string()))?;
    let temporary = root.join(format!(
        ".qt-install-{:032x}.tmp",
        u128::from_le_bytes(nonce)
    ));
    let mut file = File::create_new(&temporary)?;
    let result = (|| {
        file.write_all(
            serde_json::to_string_pretty(report)
                .expect("serializable report")
                .as_bytes(),
        )?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, root.join("qt-install-report.json"))
    })();
    if result.is_err() {
        let _ = fs::remove_file(temporary);
    }
    result.map_err(Into::into)
}
