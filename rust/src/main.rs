use std::{collections::HashMap, path::PathBuf};
fn main() {
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut args = std::env::args().skip(1);
    let mut options = HashMap::new();
    let mut writable = false;
    while let Some(key) = args.next() {
        if key == "--writable" && !writable {
            writable = true;
            continue;
        }
        if !["--fixture", "--web-root", "--port", "--import-qt"].contains(&key.as_str())
            || options.contains_key(&key)
        {
            return Err(format!("Unknown or duplicate argument: {key}").into());
        }
        let value = args.next().ok_or("Missing argument value")?;
        options.insert(key, value);
    }
    let get = |k: &str| {
        options
            .get(k)
            .ok_or_else(|| format!("Required argument: {k}"))
    };
    let fixture = PathBuf::from(get("--fixture")?);
    let web = PathBuf::from(get("--web-root")?);
    let port = get("--port")?.parse::<u16>()?;
    let pack = options.get("--import-qt").map(std::path::Path::new);
    reader_local::http::serve_fixture_with_pack(&fixture, &web, port, writable, pack)
}
