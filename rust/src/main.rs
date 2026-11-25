// A double-clicked app has no console window; inherited pipes still carry output.
#![windows_subsystem = "windows"]
use std::{collections::HashMap, path::PathBuf};
fn main() {
    // Fixture mode serves the regression harness; everything else is the production app.
    if !std::env::args().any(|arg| arg == "--fixture") {
        std::process::exit(reader_local::launcher::run(std::env::args().skip(1)));
    }
    if let Err(error) = run() {
        eprintln!("{error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let mut args = std::env::args().skip(1);
    let mut options = HashMap::new();
    while let Some(key) = args.next() {
        if !["--fixture", "--web-root", "--port"].contains(&key.as_str())
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
    reader_local::http::serve_fixture(
        &PathBuf::from(get("--fixture")?),
        &PathBuf::from(get("--web-root")?),
        get("--port")?.parse::<u16>()?,
    )
}
