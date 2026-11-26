# Notes for a future discussion

Written 2026-11-26, after the Rust rewrite (M1–M7) went live. Nothing here is decided or scheduled.
It records what was investigated, what I recommend and what is still open, so the discussion can
pick up later without redoing the research. Facts about other projects (Tauri, wry, Slint) are as
of this date; re-check them before acting.

## 1. Where the app stands

- One Rust exe (`Kệ Truyện.exe`, about 12 MB) runs a Hyper server on `127.0.0.1`. The existing
  web UI (`web/`) opens in Chrome/Edge app mode and talks to it over HTTP: 94 routes, a session
  cookie, and a shutdown token.
- SQLite (WAL, FTS5 trigram, 8-connection pool) holds the library. The online-chapter cache is
  evictable, offline chapters are protected, and images have an LRU byte budget with pins.
- Next-chapter prefetch at 80% (text and the first 10 images) and image single-flight are in
  place.
- **The two real weaknesses are the window and the platform, not the backend.**
  - The server outlives the window: closing the window doesn't stop the server, so you need the
    "Tắt máy chủ" button.
  - The app is Windows only.

## 2. What I recommend, and in what order

1. **Cheapest step: tie the server's lifetime to the window.**
   - The launcher starts Chrome/Edge in app mode with a private profile folder, so the window is a
     process it owns and can watch.
   - When that process exits, the launcher runs the existing controlled shutdown.
   - Only `launcher.rs` changes; the frontend stays as it is.
   - Decision needed: when the window closes during a download, stop the download, or ask whether
     to keep downloading in the background.
2. **If a real app window is wanted** (own title bar, single window, a path to Android): use a
   **Tauri 2 shell around the existing loopback server.**
   - The Rust server keeps running in-process, and the WebView loads `http://127.0.0.1:<port>`.
   - All 94 routes, Range requests, streaming, uploads and cookies keep working exactly as today,
     because it is still a real HTTP server.
   - This avoids every custom-protocol problem in section 3.
3. **Don't do a native UI rewrite now** (Slint, Compose or similar); see section 4.

## 3. Why not Tauri's custom protocol (the "in-app channel" design)

This was my first diagram, `truyện/Architecture you want.svg`. It still shows the in-app channel
and should be redrawn if the Tauri-shell plan goes ahead.

- **Buffered responses.**
  - Custom-protocol responses are built from a whole byte buffer
    (`respond<T: Into<Cow<'static, [u8]>>>`).
  - Range requests can serve slices, but that is not streaming.
  - Tauri's official streaming example (`examples/streaming/main.rs`) reads a Range chunk into
    memory per request.
  - Fine for small JSON, poor for large images, PDFs and EPUBs.
- **Android request bodies.**
  - wry issue #1448: custom-protocol POST/PUT bodies arrive empty on Android.
  - PR #1623 (April 2026) addresses it but was **still open and unreleased** when checked; the
    latest wry was 0.58.1.
  - The tauri-cli 2.12.6 release doesn't prove the fix shipped: check the wry version Tauri
    depends on and whether #1623 is in it.
- **Revisit when** #1623 is merged and released. Even then, run the spike in section 6 before
  moving any route.

## 4. Slint ("native-first, WebView only when necessary")

Checked against Slint 1.19.1 (2026-11-24):

| Need | Slint 1.19 |
|---|---|
| Bold, italic, colour, links | yes, `StyledText` (Markdown subset, since 1.16) |
| Illustrations inside the text flow | no inline images; only blocks between paragraphs |
| Mixed font sizes in a paragraph | no |
| Line height, letter spacing | yes |
| Justify, first-line indent, user font files | not documented |
| Text selection | since 1.19.0, within one text element only |
| Highlights, anchored notes, search marks, TTS sentence highlight | no documented character hit-testing or geometry |
| Page mode, exact position restore after a font change | nothing built in; needs text measuring |
| Long lists and comic pages | yes, virtualized `ListView`, images from memory |
| Android | yes, Skia renderer, API 26+ |
| **Embedding a WebView** | **no**, research prototype only |
| Licence | GPLv3, or royalty-free with attribution |

- **Verdict.**
  - Good fit for the shelf, settings, downloads, the dictionary editor and the comic reader.
  - Not yet able to carry the text reader at today's quality. Closing that gap means writing our
    own text layout and drawing engine (on `parley`), which is a typesetting project.
  - The "WebView when necessary" fallback doesn't exist, and the screen that would need it most
    is the reader.
- **If we try it:** a 2–3 day experiment, not a migration. Render one long Vietnamese chapter on
  Windows and on an Android phone, and check:
  1. font, size, line spacing, and justify or a fallback;
  2. an illustration between paragraphs;
  3. smooth scrolling;
  4. selecting a sentence, then a QT or dictionary lookup;
  5. a highlight that survives a font-size change;
  6. restoring the reading position;
  7. memory use.

  It fails if page mode or selection across paragraphs can't be done reasonably, or if highlights
  need our own text engine.
- **Revisit around late 2027.** Slint is moving fast: styled text arrived in April 2026 and
  selection in November 2026.

## 5. Lessons from vBook Desktop

Full study in `truyện/analysis/VBOOK-DESKTOP-ARCHITECTURE.md`.

- **What vBook is.** Native Compose screens; WebView2 only for real websites (login, CAPTCHA,
  dictionary). Its Ktor server is a developer-mode feature, and closing the window ends the
  process with `System.exit`.
- **Worth taking.**
  - Window close ends the app (step 1 in section 2), but keep our controlled shutdown rather than
    vBook's hard exit.
  - A simpler cache screen: sizes and Clear, with the advanced limits hidden.
- **Not worth taking.**
  - one SQLite file per book;
  - a byte cap for chapters (100 chapters is already small);
  - opening the server to the LAN without an opt-in and an access token.

## 6. Spike checklist before trusting any new shell

Do not assume that changing the base URL in `fetch()` makes all 94 routes work, especially on
Android. Test each of these:

- GET with query strings, including Vietnamese text;
- POST/PUT/DELETE with JSON bodies;
- file uploads (EPUB, PDF, backup ZIP, QT dictionaries);
- large images and comic pages;
- Range requests and seeking, for PDF and audio;
- ETag/304 and cache headers;
- cookies and the shutdown token;
- access control, so other apps on the device can't call the server;
- the CAPTCHA flow (`captcha.js`);
- opening an external link;
- shutdown while a download or a critical operation is running.

**Android specifics to verify, not assume:**

- plain-HTTP access to `127.0.0.1` from the WebView (cleartext policy);
- the server surviving the app going to the background;
- the storage location of the library.

## 7. Technical debt that is already known

From the post-cutover review and `docs/architecture.md`:

- `rust/src/http.rs` (about 3.4k lines) mixes routing, the HTML sanitizer, URL rewriting, image
  responses and the maintenance loop. Planned split: the sanitizer into its own module, and the
  maintenance loop into `lifecycle`.
- The Python quirks frozen in `tests/fixtures/python_reference.json` (`py_value_repr`,
  Arabic-Indic digits, Python-style error text): decide which are contract and which are
  accident.
- **Open:** after heavy bursts, an unidentified read snapshot blocks the idle WAL truncate, so
  the WAL stays at 20–90 MB until restart.
- Shelf text search is a full scan. That is fine at today's size; FTS exists if it ever matters.
- Live STV (CAPTCHA, TLS, cookies) is verified only on synthetic fixtures.
- The `m5-sources.json` test hook still exists in `Sources::open`.
- Launcher minors:
  - the first-run lock is released and re-taken;
  - the import success message uses the error icon;
  - non-Unicode arguments exit silently.
- Changing QT settings mid-chapter can trigger an early prefetch.
- Some tests rely on sleeps.

## 8. Sources change without notice

On 2026-11-26, Truyện QQ search started returning an empty page unless the request carries the
`visit-read` cookie (fixed in commit `360f69f`). Earlier, QQ had added a `document.cookie`
challenge.

- **Expect more of this.**
- **Cheap idea for later:** a "check sources" button that runs one search, one book page and one
  chapter per source, and reports which step broke. It would turn "Nguồn trả trang trống" into a
  clear "QQ search changed" message.

## 9. Questions only you can answer

- Is Android a real goal, and when? This decides between step 1 alone and the Tauri shell.
- When the window closes during a download, should the app stop or keep downloading in the
  background?
- Should Settings be simplified?
- Is a 2–3 day Slint experiment worth spending time on now, or should it wait until late 2027?
