const defaults = { theme: 'sepia', mode: 'scroll', font: 'Times', size: 21, lineHeight: 1.85, paragraph: 1.15, letter: 0, word: 0, width: 760, align: 'left', indent: false, qt: 'original', rate: 1, pitch: 1, voice: '', sleep: 0, autoSpeed: 28, zoom: 100 };
const clamp = (value, min, max) => Math.min(max, Math.max(min, Number(value) || 0));
const fonts = { Times: '"Times New Roman",serif', Segoe: '"Segoe UI",Arial,sans-serif', Arial: 'Arial,sans-serif' };

export function createReader({ api, el, notify, showDialog, getSettings, saveSettings, openDictionary, onClose }) {
  const root = document.getElementById('reader');
  const app = document.getElementById('app');
  let book = null, chapters = [], chapter = null, index = 0, prefs = { ...defaults }, generation = 0;
  let tocEntries = [], disposeToc = () => {}, refreshToc = () => {};
  let scroller, sheet, content, status, progressBar, chapterTitle, previousButton, nextButton, selectionTip, speechBar;
  let saveTimer, resizeTimer, progressChain = Promise.resolve(), loading = false, dirty = false, lastSelection = '';
  let imagePage = 0, autoFrame = 0, autoLast = 0, lastReadingPosition = 0;
  let lastReadable = null, savedReading = null;
  let voices = [], speechChunks = [], speechIndex = 0, speaking = false, paused = false, speechGeneration = 0, sleepTimer, sleepDeadline = 0, voiceSelect = null, speechStatus = null;
  let wakeLock = null;
  const synth = window.speechSynthesis;
  const button = (text, action, primary = false) => el('button', { class: `button${primary ? ' primary' : ''}`, text, onclick: action });
  const icon = (text, title, action, extra = '') => el('button', { class: `icon-button ${extra}`, text, title, 'aria-label': title, onclick: action });
  const path = (suffix = '') => `/api/books/${encodeURIComponent(book.id)}${suffix}`;
  const chapterPath = () => path(`/chapters/${index}`);

  function tocEntry(item, fallback) {
    const at = Number.isInteger(item.index) ? item.index : fallback;
    return { item, index: at, search: `${at + 1} ${item.title || `Chương ${at + 1}`}`.toLocaleLowerCase('vi') };
  }

  function position() {
    if (!scroller || !chapter) return 0;
    if (book.kind === 'comic') {
      const pages = [...content.querySelectorAll('.comic-page')];
      if (!pages.length) return 0;
      if (prefs.mode === 'paged') return imagePage / pages.length;
      if (scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop < 2 && scroller.scrollTop > 0) return 1;
      const top = scroller.getBoundingClientRect().top;
      const visible = pages.findIndex((page) => page.getBoundingClientRect().bottom > top);
      const page = pages[Math.max(0, visible)].getBoundingClientRect();
      return (Math.max(0, visible) + clamp((top - page.top) / Math.max(1, page.height), 0, 1)) / pages.length;
    }
    if (prefs.mode === 'paged') { const pages = pageCount(); return pages > 1 ? clamp(Math.round(scroller.scrollLeft / pageStep()) / (pages - 1), 0, 1) : 0; }
    return scroller.scrollHeight > scroller.clientHeight ? clamp(scroller.scrollTop / (scroller.scrollHeight - scroller.clientHeight), 0, 1) : 0;
  }

  function restorePosition(value) {
    if (!scroller || !chapter) return;
    value = clamp(value, 0, 1);
    if (book.kind === 'comic') {
      const pages = [...content.querySelectorAll('.comic-page')];
      if (!pages.length) return;
      imagePage = Math.min(pages.length - 1, Math.floor(value * pages.length));
      showImagePage();
      if (prefs.mode !== 'paged') {
        const page = pages[imagePage], area = scroller.getBoundingClientRect(), rect = page.getBoundingClientRect();
        scroller.scrollTop += rect.top - area.top + (value * pages.length - imagePage) * rect.height;
      }
    }
    else if (prefs.mode === 'paged') { measurePages(); scroller.scrollTop = 0; scroller.scrollLeft = Math.round(value * (pageCount() - 1)) * pageStep(); }
    else { scroller.scrollLeft = 0; scroller.scrollTop = (scroller.scrollHeight - scroller.clientHeight) * value; }
    updateStatus();
  }

  function record() { return book && chapter && !loading ? { id: book.id, index, position: position() } : null; }
  function saveProgress(quiet = false) {
    clearTimeout(saveTimer);
    const entry = record();
    if (!entry || !dirty) return progressChain;
    dirty = false;
    progressChain = progressChain.catch(() => {}).then(() => api(`/api/books/${encodeURIComponent(entry.id)}/progress`, { index: entry.index, position: entry.position })).catch((error) => {
      if (book?.id === entry.id && chapter && index === entry.index && !loading) dirty = true;
      if (!quiet) notify(`Chưa lưu được vị trí đọc: ${error.message}`, true);
    });
    return progressChain;
  }

  function scheduleSave() {
    if (loading || !chapter) return;
    dirty = true;
    updateStatus();
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveProgress(), 1100);
  }

  function updateStatus() {
    if (!status || !chapter) return;
    const pos = position();
    lastReadingPosition = pos;
    const total = Math.max(chapters.length, book.chapter_count || 0, 1);
    let label = `Chương ${index + 1}/${total} · ${Math.round(pos * 100)}% chương`;
    if (book.kind === 'comic' && prefs.mode === 'paged') label = `Ảnh ${imagePage + 1}/${content.children.length} · Chương ${index + 1}/${total}`;
    else if (prefs.mode === 'paged') { const pages = pageCount(); label = `Trang ${Math.min(pages, Math.round(scroller.scrollLeft / pageStep()) + 1)}/${pages} · Chương ${index + 1}/${total}`; }
    status.textContent = label;
    progressBar.style.width = `${clamp(((index + pos) / total) * 100, 0, 100)}%`;
    previousButton.disabled = index <= 0;
    nextButton.disabled = index >= chapters.length - 1;
  }

  function applyPrefs(preserve = true) {
    const pos = preserve ? position() : 0;
    if (!Object.hasOwn(fonts, prefs.font)) prefs.font = defaults.font;
    root.dataset.theme = ['sepia', 'night', 'day', 'system'].includes(prefs.theme) ? prefs.theme : 'sepia';
    root.dataset.mode = prefs.mode === 'paged' ? 'paged' : 'scroll';
    root.style.setProperty('--reading-font', fonts[prefs.font]);
    root.style.setProperty('--font-size', `${clamp(prefs.size, 12, 40)}px`);
    root.style.setProperty('--line-height', clamp(prefs.lineHeight, 1.2, 3));
    root.style.setProperty('--paragraph-space', `${clamp(prefs.paragraph, 0, 3)}em`);
    root.style.setProperty('--letter-space', `${clamp(prefs.letter, -1, 5)}px`);
    root.style.setProperty('--word-space', `${clamp(prefs.word, 0, 10)}px`);
    root.style.setProperty('--reading-width', `${clamp(prefs.width, 400, 1400)}px`);
    root.style.setProperty('--comic-zoom', clamp(prefs.zoom, 50, 200) / 100);
    root.style.setProperty('--text-align', ['left', 'center', 'right', 'justify'].includes(prefs.align) ? prefs.align : 'left');
    root.style.setProperty('--indent', prefs.indent ? '1.7em' : '0');
    if (preserve) requestAnimationFrame(() => restorePosition(pos));
  }
  function changePref(key, value) {
    const pos = position();
    prefs[key] = value; applyPrefs(false);
    requestAnimationFrame(() => restorePosition(pos));
    saveSettings({ ...prefs });
  }

  function buildShell() {
    chapterTitle = el('h1'); content = el('div', { class: 'chapter-content' });
    sheet = el('article', { class: 'reading-sheet', 'aria-label': 'Nội dung chương' }, [chapterTitle, content]);
    scroller = el('div', { class: 'reader-scroll', tabindex: '0' }, [sheet]);
    scroller.addEventListener('scroll', () => {
      if (chapter && !loading && prefs.mode === 'paged' && book.kind !== 'comic') {
        const aligned = Math.round(scroller.scrollLeft / pageStep()) * pageStep();
        if (Math.abs(scroller.scrollLeft - aligned) > 1) scroller.scrollLeft = aligned;
      }
      scheduleSave();
    }, { passive: true });
    scroller.addEventListener('wheel', (event) => {
      if (!chapter || loading || prefs.mode !== 'paged' || event.ctrlKey || event.shiftKey || event.altKey || document.querySelector('dialog[open]')) return;
      if (window.getSelection()?.toString()) return;
      event.preventDefault();
      const delta = Math.abs(event.deltaY) >= Math.abs(event.deltaX) ? event.deltaY : event.deltaX;
      if (Math.abs(delta) > 2 && !scroller.wheelLock) {
        step(delta > 0 ? 1 : -1);
        scroller.wheelLock = true; setTimeout(() => { scroller.wheelLock = false; }, 220);
      }
    }, { passive: false });
    let touchStart;
    scroller.addEventListener('touchstart', (event) => { if (event.touches.length === 1) touchStart = { x: event.touches[0].clientX, y: event.touches[0].clientY }; }, { passive: true });
    scroller.addEventListener('touchend', (event) => {
      if (!touchStart || prefs.mode !== 'paged' || window.getSelection()?.toString()) return;
      const dx = event.changedTouches[0].clientX - touchStart.x, dy = event.changedTouches[0].clientY - touchStart.y;
      touchStart = null;
      if (Math.abs(dx) > 70 && Math.abs(dx) > Math.abs(dy) * 1.5) step(dx < 0 ? 1 : -1);
    }, { passive: true });
    scroller.addEventListener('click', (event) => {
      if (prefs.mode !== 'paged' || event.target.closest('a,button') || window.getSelection()?.toString()) return;
      const x = event.clientX / innerWidth;
      if (x < .12) step(-1); else if (x > .88) step(1);
    });
    scroller.addEventListener('dblclick', (event) => {
      if (book.kind === 'comic' && prefs.mode === 'scroll' && event.target.tagName === 'IMG') changePref('zoom', prefs.zoom === 100 ? 150 : 100);
    });
    status = el('span', { class: 'reader-status', role: 'status' });
    progressBar = el('span');
    previousButton = button('‹ Chương trước', () => {
      const target = chapter ? { index: index - 1, position: 0 } : previousReading();
      if (target) loadChapter(target.index, target.position);
    });
    nextButton = button('Chương sau ›', () => loadChapter(index + 1), true);
    selectionTip = el('div', { class: 'reader-selection-tip', hidden: true }, [button('Ghi chú', () => editNote()), button('Nghe đoạn chọn', () => { startSpeech(lastSelection); selectionTip.hidden = true; }), button('Từ điển', () => { selectionTip.hidden = true; openDictionary(book); })]);
    speechBar = el('div', { class: 'tts-transport', hidden: true }, [icon('Ⅱ', 'Tạm dừng hoặc tiếp tục nghe', togglePause), el('span', { class: 'speech-bar-label', text: 'Đang đọc…' }), icon('■', 'Dừng đọc', stopSpeech), icon('⚙', 'Thiết lập giọng đọc', voiceDialog)]);
    root.replaceChildren(el('header', { class: 'reader-header' }, [el('button', { class: 'reader-back', onclick: close }, [el('span', { text: '‹', 'aria-hidden': 'true' }), 'Về kệ']), el('p', { class: 'reader-book-title', text: book.name }), el('div', { class: 'reader-tools' }, [icon('☰', 'Mục lục', tocDialog), icon('⌕', 'Tìm trong nội dung', searchDialog), icon('☆', 'Ghi chú và dấu trang', notesDialog), icon('♫', 'Nghe truyện', voiceDialog), icon('QT', 'QT và từ điển', qtDialog), icon('Aa', 'Cách đọc và giao diện', settingsDialog), icon('⋯', 'Công cụ đọc', toolsDialog, 'reader-optional')])]), scroller, el('div', { class: 'reader-progress-track' }, [progressBar]), el('footer', { class: 'reader-footer' }, [previousButton, status, nextButton]), selectionTip, speechBar);
    applyPrefs(false);
  }

  async function open(selected, atIndex, atPosition) {
    const token = ++generation;
    const saved = book ? saveProgress() : progressChain;
    stopSpeech(); stopAutoScroll();
    loading = true; chapter = null; dirty = false;
    await saved;
    if (token !== generation) return;
    book = selected;
    lastReadable = null;
    const savedIndex = Number(book.chapter_index);
    savedReading = Number.isInteger(savedIndex) ? { index: savedIndex, position: clamp(book.position, 0, 1) } : null;
    prefs = { ...defaults, ...getSettings() };
    disposeToc(); tocEntries = []; chapters = []; chapter = null; index = Number(atIndex ?? book.chapter_index) || 0;
    const initialPosition = Number(atPosition ?? book.position) || 0;
    root.hidden = false; app.inert = true; document.body.style.overflow = 'hidden';
    root.dataset.kind = book.kind === 'comic' ? 'comic' : 'text';
    buildShell();
    root.dataset.state = 'loading';
    content.append(el('p', { class: 'reader-loading', text: 'Đang mở mục lục…' }));
    try {
      const result = await api(path('/chapters'));
      if (token !== generation) return;
      chapters = Array.isArray(result) ? result : result.chapters || [];
      tocEntries = chapters.map(tocEntry);
      if (!chapters.length) throw new Error('Truyện chưa có mục lục. Hãy kiểm tra trang nguồn; sách vẫn được giữ trên kệ.');
      await loadChapter(clamp(index, 0, chapters.length - 1), initialPosition, false);
    } catch (error) { if (token === generation) showReadError(error, () => open(selected, index, initialPosition)); }
    scroller.focus({ preventScroll: true });
  }

  async function close() {
    generation++;
    await saveProgress();
    stopSpeech(); stopAutoScroll();
    if (wakeLock) { wakeLock.release().catch(() => {}); wakeLock = null; }
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    root.hidden = true; root.classList.remove('is-focused'); app.inert = false; document.body.style.overflow = '';
    disposeToc(); tocEntries = []; chapters = [];
    book = null; chapter = null; dirty = false; lastReadable = savedReading = null;
    document.getElementById('main').focus({ preventScroll: true });
    onClose();
  }

  async function loadChapter(next, pos = 0, save = true, continueSpeech = false) {
    if (next < 0 || next >= chapters.length) return;
    const token = ++generation;
    stopAutoScroll(); stopSpeech();
    const previous = record(), previousChapter = chapter, previousIndex = index, previousImagePage = imagePage;
    if (previous) lastReadable = previous;
    const saved = save ? saveProgress() : progressChain;
    loading = true; dirty = false;
    selectionTip.hidden = true; lastSelection = '';
    index = next; chapter = null; imagePage = 0;
    root.dataset.state = 'loading';
    if (!previousChapter) {
      chapterTitle.textContent = chapters[index].title || `Chương ${index + 1}`;
      content.replaceChildren(el('p', { class: 'reader-loading', text: 'Đang lấy nội dung chương… Nội dung tải được sẽ lưu trên máy.' }));
      scroller.scrollTop = scroller.scrollLeft = 0;
    }
    status.textContent = `Đang mở chương ${index + 1}/${chapters.length}…`;
    previousButton.disabled = nextButton.disabled = true;
    await saved;
    if (token !== generation) return;
    try {
      const result = await api(path(`/chapters/${index}`));
      if (token !== generation) return;
      chapter = result; lastReadingPosition = clamp(pos, 0, 1);
      Object.assign(chapters[index], { title: result.title || chapters[index].title, downloaded: true });
      tocEntries[index] = tocEntry(chapters[index], index);
      refreshToc();
      if (prefs.qt !== 'original' && book.kind !== 'comic' && !sourceVietnamese()) {
        try { await renderChapter(pos); }
        catch (error) { if (token !== generation) return; renderOriginal(); notify(`QT chưa dùng được: ${error.message} Đang hiển thị nguyên văn.`, true); }
      } else renderOriginal();
      if (token !== generation) return;
      await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      if (token !== generation) return;
      restorePosition(pos);
      loading = false; dirty = true; root.dataset.state = 'reading';
      lastReadable = record();
      await saveProgress();
      if (token === generation && continueSpeech) startSpeech();
    } catch (error) {
      if (token !== generation) return;
      if (error.code === 'captcha_cancelled' && previousChapter && previous) {
        chapter = previousChapter; index = previousIndex; imagePage = previousImagePage;
        loading = false; dirty = false; root.dataset.state = 'reading';
        restorePosition(previous.position); notify(error.message); return;
      }
      showReadError(error, () => loadChapter(next, pos, false));
    }
  }

  function previousReading() {
    if (lastReadable) return lastReadable;
    if (savedReading && savedReading.index !== index && savedReading.index >= 0 && savedReading.index < chapters.length) return savedReading;
    return index > 0 && index < chapters.length ? { index: index - 1, position: 0 } : null;
  }

  function showReadError(error, retry) {
    chapterTitle.textContent = chapters[index]?.title || `Chương ${index + 1}`;
    stopSpeech(); stopAutoScroll(); clearTimeout(saveTimer);
    loading = false; chapter = null; dirty = false; root.dataset.state = 'error';
    selectionTip.hidden = true; lastSelection = '';
    const vip = error.code === 'vip_locked' && error.access?.vip === true && typeof error.access.evidence === 'string' && error.access.evidence.trim();
    if (vip && chapters[index]) chapters[index].is_vip = true;
    refreshToc();
    const auth = error.code === 'auth_required';
    const unverified = !vip && ['vip_locked', 'access_unverified'].includes(error.code);
    const titles = { network_error: 'Chưa kết nối được nguồn', source_timeout: 'Nguồn phản hồi quá lâu', parse_error: 'Chưa nhận diện được nội dung chương', http_error: `Nguồn trả về lỗi HTTP${error.http_status ? ` ${error.http_status}` : ''}` };
    const title = vip ? 'Chương VIP chưa thể đọc' : auth ? 'Cần đăng nhập tại website gốc' : unverified ? 'Chưa xác minh quyền truy cập' : titles[error.code] || 'Chưa mở được nội dung';
    const message = vip ? 'Website gốc xác nhận chương VIP và phiên truy cập hiện tại chưa có quyền đọc. Hãy mở đúng chương trên website gốc để kiểm tra quyền truy cập, rồi thử lại.' : auth ? 'Website gốc yêu cầu đăng nhập. Hãy đăng nhập tại website gốc trong cửa sổ được mở, rồi quay lại thử đọc.' : unverified ? 'Chưa xác minh được quyền đọc chương này. Hãy kiểm tra nội dung và phiên đăng nhập trên website gốc, rồi thử lại.' : error.message;
    const target = previousReading();
    const previous = button('Quay về chương trước', () => { if (target) loadChapter(target.index, target.position, false); });
    previous.disabled = !target;
    const actions = [previous, button('Thử lại', retry, true), button('Mở mục lục', tocDialog)];
    if (chapters[index]) {
      const originalPath = path(`/chapters/${index}/original`), token = generation;
      actions.unshift(button('Mở trên website gốc', async (event) => {
        const control = event.currentTarget;
        control.disabled = true;
        try { await api(originalPath, {}); if (token === generation) notify('Đã mở chương trên website gốc. Kiểm tra quyền đọc tại đó rồi thử lại.'); }
        catch (problem) { if (token === generation) notify(problem.message, true); }
        finally { control.disabled = false; }
      }));
    }
    content.replaceChildren(el('div', { class: 'reader-loading reader-access-error', role: 'alert' }, [vip ? el('span', { class: 'vip-label', text: 'VIP' }) : null, el('h2', { text: title }), el('p', { class: 'inline-error', text: message }), el('p', { class: 'muted', text: 'Sách và vị trí đã lưu vẫn ở trên kệ. Bạn có thể thử lại hoặc chọn chương khác.' }), el('div', { class: 'reader-access-actions' }, actions)]));
    status.textContent = 'Chưa có nội dung'; previousButton.disabled = !target; nextButton.disabled = index >= chapters.length - 1;
  }

  function sourceVietnamese() { return chapter?.language === 'vi' && chapter?.translation === 'source'; }

  function safeUrl(value, allowSvg = false) {
    if (typeof value !== 'string' || !value.trim()) return '';
    try {
      const url = new URL(value, location.href);
      return ['http:', 'https:'].includes(url.protocol) || /^data:image\/(png|jpeg|gif|webp);base64,/i.test(value) || (allowSvg && /^data:image\/svg\+xml;base64,/i.test(value)) ? url.href : '';
    } catch { return ''; }
  }
  function sanitizedContent(html) {
    const fragment = document.createDocumentFragment();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const allowed = new Set(['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'BR', 'IMG', 'HR', 'EM', 'B', 'I', 'STRONG', 'U', 'BLOCKQUOTE', 'UL', 'OL', 'LI', 'A', 'PRE', 'CODE', 'SPAN', 'FIGURE', 'FIGCAPTION']);
    const unsafe = new Set(['SCRIPT', 'STYLE', 'IFRAME', 'OBJECT', 'EMBED', 'SVG', 'MATH', 'TEMPLATE', 'FORM', 'INPUT', 'BUTTON', 'LINK', 'META']);
    const block = new Set(['P', 'DIV', 'H1', 'H2', 'H3', 'H4', 'BR', 'HR', 'BLOCKQUOTE', 'UL', 'OL', 'LI', 'PRE', 'FIGURE', 'FIGCAPTION']);
    function copy(node, target) {
      if (node.nodeType === Node.TEXT_NODE) {
        const text = node.textContent.replace(/\r\n?/g, '\n');
        // STV uses literal newlines between inline translation tokens; ordinary HTML collapses them.
        if (book.source === 'stv' && text.includes('\n') && !node.parentElement?.closest('pre')) {
          const before = node.previousSibling, after = node.nextSibling;
          if (!text.trim() && (!before || !after || block.has(before.nodeName) || block.has(after.nodeName))) return;
          text.split('\n').forEach((line, i) => {
            if (i) target.append(document.createElement('br'));
            target.append(document.createTextNode(line));
          });
        } else target.append(document.createTextNode(text));
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || unsafe.has(node.tagName)) return;
      if (!allowed.has(node.tagName)) { for (const child of node.childNodes) copy(child, target); return; }
      const clean = document.createElement(node.tagName.toLowerCase());
      if (node.tagName === 'A') {
        const href = safeUrl(node.getAttribute('href'));
        if (href) { clean.href = href; clean.target = '_blank'; clean.rel = 'noopener noreferrer'; }
      }
      if (node.tagName === 'IMG') {
        const src = safeUrl(node.getAttribute('src'), true);
        if (!src) return;
        clean.src = src; clean.alt = node.getAttribute('alt') || 'Ảnh trong chương'; clean.loading = 'lazy'; clean.referrerPolicy = 'no-referrer';
      }
      for (const child of node.childNodes) copy(child, clean);
      target.append(clean);
    }
    for (const child of parsed.body.childNodes) copy(child, fragment);
    return fragment;
  }

  function renderOriginal() {
    chapterTitle.textContent = chapter.title || chapters[index].title || `Chương ${index + 1}`;
    content.replaceChildren();
    if (book.kind === 'comic') {
      let images = chapter.images || [];
      if (!images.length && chapter.html) images = [...new DOMParser().parseFromString(chapter.html, 'text/html').images].map((image) => image.getAttribute('src'));
      const validImages = images.map((image) => typeof image === 'string' ? image : image.url || image.src).filter((src) => safeUrl(src));
      if (!validImages.length) throw new Error('Chương này chưa trả về ảnh hợp lệ. Hãy thử lại hoặc kiểm tra nguồn.');
      validImages.forEach((src, i) => {
        const img = el('img', { src: safeUrl(src), alt: `Trang ảnh ${i + 1}`, loading: i < 2 ? 'eager' : 'lazy', referrerPolicy: 'no-referrer' });
        const figure = el('figure', { class: 'comic-page' }, [img, el('figcaption', { text: `${i + 1} / ${validImages.length}` })]);
        const token = generation;
        img.onload = () => { if (token === generation && !loading) restorePosition(lastReadingPosition); };
        img.onerror = () => {
          img.hidden = true;
          const error = el('div', { class: 'image-error' }, [el('p', { text: `Chưa tải được ảnh ${i + 1}.` }), button('Tải lại ảnh', () => { error.remove(); img.hidden = false; img.src = safeUrl(src); })]);
          figure.prepend(error);
        };
        content.append(figure);
      });
      if (prefs.mode === 'paged') showImagePage();
    } else if (chapter.html) content.append(sanitizedContent(chapter.html));
    else appendParagraphs(chapter.text || '', content);
    if (!content.textContent.trim() && !content.querySelector('img')) throw new Error('Nguồn trả về nội dung rỗng. Không có nội dung để đọc hoặc nghe.');
    const firstHeading = content.querySelector('h1,h2,h3,h4');
    if (firstHeading?.textContent.trim() === chapterTitle.textContent.trim()) firstHeading.remove();
  }

  function appendParagraphs(text, target) {
    for (const paragraph of text.split(/\n\s*\n|\n/).filter((line) => line.trim())) target.append(el('p', { class: 'plain-paragraph', text: paragraph }));
  }

  async function renderChapter(pos = position()) {
    if (!chapter) return;
    stopSpeech();
    const token = generation;
    renderOriginal();
    if (prefs.qt === 'original' || book.kind === 'comic' || sourceVietnamese()) { requestAnimationFrame(() => { if (token === generation) restorePosition(pos); }); return; }
    const original = content.innerText || chapter.text || '';
    if (!original.trim()) return;
    const result = await api('/api/qt', { text: original, book_id: book.id });
    if (token !== generation || !book) return;
    if (!result.text?.trim()) throw new Error('QT trả về nội dung rỗng. Chọn “Nguyên văn” để tiếp tục đọc.');
    const images = [...content.querySelectorAll('img')].map((image) => image.cloneNode());
    content.replaceChildren();
    if (prefs.qt === 'bilingual') {
      const source = original.split(/\n+/).filter((line) => line.trim());
      const translated = result.text.split(/\n+/).filter((line) => line.trim());
      if (source.length === translated.length) source.forEach((line, i) => { content.append(el('p', { class: 'qt-source plain-paragraph', text: line }), el('p', { class: 'qt-text plain-paragraph', text: translated[i] })); });
      else { const originalBox = el('section', { class: 'qt-source' }); appendParagraphs(original, originalBox); content.append(originalBox); appendParagraphs(result.text, content); }
    } else appendParagraphs(result.text, content);
    content.append(...images);
    requestAnimationFrame(() => { if (token === generation) restorePosition(pos); });
    if (result.warning) notify(result.warning);
    else if (result.text === original) notify('QT chưa thay đổi văn bản. Kiểm tra từ điển đã nhập và ngôn ngữ nội dung.');
  }

  function showImagePage() {
    [...content.children].forEach((image, i) => { image.hidden = prefs.mode === 'paged' && i !== imagePage; });
  }
  function pageStep() { return Math.max(1, scroller.getBoundingClientRect().width); }
  function pageCount() {
    const endGutter = parseFloat(getComputedStyle(sheet).paddingRight) || 0;
    return Math.max(1, Math.round((sheet.scrollWidth + endGutter) / pageStep()));
  }
  function measurePages() {
    scroller.style.setProperty('--page-end', `${Math.ceil(pageCount() * pageStep()) - 1}px`);
  }
  async function step(direction) {
    if (loading || !chapter) return;
    const pos = position();
    if (book.kind === 'comic' && prefs.mode === 'paged') {
      if (imagePage + direction >= 0 && imagePage + direction < content.children.length) { imagePage += direction; showImagePage(); scheduleSave(); return; }
    } else if (prefs.mode === 'paged') {
      const page = Math.round(scroller.scrollLeft / pageStep()) + direction;
      if (page >= 0 && page < pageCount()) { scroller.scrollTo({ left: page * pageStep(), behavior: 'instant' }); scheduleSave(); return; }
    } else if ((direction > 0 && pos < .999) || (direction < 0 && pos > .001)) { scroller.scrollBy({ top: direction * scroller.clientHeight * .88, behavior: 'instant' }); return; }
    if (index + direction >= 0 && index + direction < chapters.length) await loadChapter(index + direction, direction < 0 ? 1 : 0);
  }

  function tocDialog() {
    disposeToc();
    const query = el('input', { type: 'search', placeholder: 'Tìm tên hoặc số chương…', 'aria-label': 'Tìm mục lục' });
    const count = el('p', { class: 'muted small', role: 'status' });
    const help = el('p', { id: 'toc-help', class: 'sr-only', text: 'Dùng phím mũi tên, Home, End, Page Up và Page Down để chọn chương; Enter để mở.' });
    const spacer = el('div', { class: 'toc-window', role: 'presentation' });
    const list = el('div', { class: 'toc-list', role: 'list', 'aria-label': 'Mục lục', 'aria-describedby': 'toc-help' }, [spacer]);
    const dialog = showDialog(`Mục lục · ${chapters.length} chương`, [query, count, help, list]);
    const nativeDialog = dialog.body.closest('dialog'), rowHeight = 64, overscan = 6, rows = new Map();
    let items = tocEntries, focused = Math.max(0, items.findIndex(item => item.index === index)), frame = 0;
    list.style.setProperty('--toc-row-height', `${rowHeight}px`);
    function size() {
      list.style.setProperty('--toc-height', `${items.length * rowHeight}px`);
      list.hidden = !items.length;
      count.textContent = items.length ? `${items.length.toLocaleString('vi-VN')}/${tocEntries.length.toLocaleString('vi-VN')} chương` : 'Không có chương phù hợp.';
    }
    function render() {
      if (!dialog.body.isConnected || !nativeDialog.open) { dispose(); return; }
      const visible = Math.ceil((list.clientHeight || rowHeight * 8) / rowHeight);
      const first = Math.max(0, Math.floor(list.scrollTop / rowHeight) - overscan);
      const last = Math.min(items.length, Math.ceil(list.scrollTop / rowHeight) + visible + overscan);
      const positions = Array.from({ length: Math.max(0, last - first) }, (_, i) => first + i);
      if (list.contains(document.activeElement)) {
        if (items[focused]?.index === Number(document.activeElement.dataset.chapterIndex) && !positions.includes(focused)) positions.push(focused);
      } else if (focused < first || focused >= last) focused = Math.min(items.length - 1, Math.floor(list.scrollTop / rowHeight));
      positions.sort((a, b) => a - b);
      const keep = new Set(positions.map(pos => items[pos].index));
      for (const [key, row] of rows) if (!keep.has(key)) { row.remove(); rows.delete(key); }
      let before = null;
      for (const pos of positions.reverse()) {
        const entry = items[pos], item = entry.item, title = item.title || `Chương ${entry.index + 1}`;
        let row = rows.get(entry.index);
        if (!row) {
          const control = el('button', { class: 'toc-item',
            'data-chapter-index': entry.index,
            onclick: () => { dialog.close(); loadChapter(entry.index); } }, [
              el('span', { class: 'toc-title' }, [el('span', { class: 'toc-title-text' })])]);
          row = el('div', { class: 'toc-row', role: 'listitem' }, [control]);
          rows.set(entry.index, row);
        }
        const control = row.firstChild, label = control.querySelector('.toc-title');
        if (control.title !== title) { control.title = title; label.firstChild.textContent = title; }
        control.classList.toggle('active', entry.index === index);
        if (entry.index === index) control.setAttribute('aria-current', 'page');
        else control.removeAttribute('aria-current');
        const vip = label.querySelector('.toc-vip'), cached = control.querySelector('.cached');
        if (item.is_vip === true && !vip) label.append(el('span', { class: 'toc-vip', text: '🔒', role: 'img', 'aria-label': 'Chương VIP theo mục lục nguồn; quyền đọc chưa được kiểm tra', title: 'Chương VIP theo mục lục nguồn; quyền đọc chưa được kiểm tra' }));
        else if (item.is_vip !== true) vip?.remove();
        if (item.downloaded && !cached) control.append(el('span', { class: 'cached', text: 'Đã lưu' }));
        else if (!item.downloaded) cached?.remove();
        row.style.top = `${pos * rowHeight}px`;
        row.setAttribute('aria-posinset', pos + 1); row.setAttribute('aria-setsize', items.length);
        control.dataset.tocPosition = pos; control.tabIndex = pos === focused ? 0 : -1;
        if (row.parentNode !== spacer || row.nextSibling !== before) spacer.insertBefore(row, before);
        before = row;
      }
    }
    function schedule() { if (!frame) frame = requestAnimationFrame(() => { frame = 0; render(); }); }
    function focus(pos) {
      focused = Math.max(0, Math.min(items.length - 1, pos));
      const top = focused * rowHeight;
      if (top < list.scrollTop) list.scrollTop = top;
      else if (top + rowHeight > list.scrollTop + list.clientHeight) list.scrollTop = top + rowHeight - list.clientHeight;
      render();
      rows.get(items[focused]?.index)?.firstChild.focus({ preventScroll: true });
    }
    list.onkeydown = event => {
      const page = Math.max(1, Math.floor(list.clientHeight / rowHeight));
      const moves = { ArrowDown: focused + 1, ArrowUp: focused - 1, PageDown: focused + page, PageUp: focused - page, Home: 0, End: items.length - 1 };
      if (event.key in moves && items.length) { event.preventDefault(); event.stopPropagation(); focus(moves[event.key]); }
    };
    list.onfocusin = event => { if (event.target.matches('.toc-item')) focused = Number(event.target.dataset.tocPosition); };
    list.onscroll = schedule;
    function filter(reset) {
      const focusedKey = items[focused]?.index, hadFocus = list.contains(document.activeElement);
      const term = query.value.trim().toLocaleLowerCase('vi');
      items = term ? tocEntries.filter(item => item.search.includes(term)) : tocEntries;
      focused = reset ? 0 : Math.max(0, items.findIndex(item => item.index === focusedKey));
      size(); if (reset) list.scrollTop = 0; render();
      if (hadFocus && !reset) {
        if (items[focused]?.index === focusedKey) focus(focused);
        else query.focus();
      }
    }
    query.oninput = () => filter(true);
    const observer = new ResizeObserver(() => {
      if (list.contains(document.activeElement)) focus(focused);
      schedule();
    });
    function dispose() {
      observer.disconnect(); cancelAnimationFrame(frame);
      nativeDialog.removeEventListener('close', dispose);
      query.oninput = list.onscroll = list.onkeydown = list.onfocusin = null;
      if (disposeToc === dispose) { disposeToc = () => {}; refreshToc = () => {}; }
    }
    disposeToc = dispose;
    refreshToc = () => filter(false);
    nativeDialog.addEventListener('close', dispose, { once: true });
    size(); list.scrollTop = Math.max(0, focused * rowHeight - (list.clientHeight - rowHeight) / 2); render();
    observer.observe(list);
  }

  function rangeControl(label, key, min, max, step, suffix = '') {
    const output = el('output', { text: `${prefs[key]}${suffix}` });
    const input = el('input', { type: 'range', min, max, step, value: prefs[key], oninput: () => { changePref(key, Number(input.value)); output.textContent = `${input.value}${suffix}`; } });
    return el('label', {}, [el('span', { text: label }), input, output]);
  }
  function selectControl(label, key, options) {
    const select = el('select', {}, options.map(([value, text]) => el('option', { value, text })));
    select.value = prefs[key];
    select.onchange = () => { changePref(key, select.value); if (key === 'mode' && book.kind === 'comic') showImagePage(); };
    return el('label', {}, [el('span', { text: label }), select]);
  }
  function settingsDialog() {
    const indent = el('input', { type: 'checkbox', checked: prefs.indent, onchange: () => changePref('indent', indent.checked) });
    const grid = el('div', { class: 'reader-setting-grid' }, [selectControl('Nền đọc', 'theme', [['sepia', 'Giấy ấm'], ['day', 'Sáng'], ['night', 'Tối'], ['system', 'Theo hệ thống']]), selectControl('Cách đọc', 'mode', [['scroll', 'Cuộn dọc'], ['paged', book.kind === 'comic' ? 'Từng ảnh' : 'Lật trang ngang']]), selectControl('Phông chữ', 'font', [['Times', 'Times New Roman'], ['Segoe', 'Segoe UI'], ['Arial', 'Arial']]), selectControl('Căn chữ', 'align', [['left', 'Căn trái'], ['justify', 'Căn đều'], ['center', 'Căn giữa'], ['right', 'Căn phải']]), rangeControl('Cỡ chữ', 'size', 12, 40, 1, ' px'), rangeControl('Giãn dòng', 'lineHeight', 1.2, 3, .05), rangeControl('Khoảng cách đoạn', 'paragraph', 0, 3, .1, ' em'), rangeControl('Độ rộng nội dung', 'width', 400, 1400, 20, ' px'), rangeControl('Giãn ký tự', 'letter', 0, 5, .25, ' px'), rangeControl('Giãn từ', 'word', 0, 10, .5, ' px'), el('label', { class: 'wide' }, [el('span', { text: 'Thụt đầu dòng' }), indent])]);
    if (book.kind === 'comic') grid.prepend(rangeControl('Thu phóng ảnh khi cuộn · nháy đúp để phóng/thu', 'zoom', 50, 200, 10, '%'));
    showDialog('Cách đọc và giao diện', [grid, el('p', { class: 'muted small', text: 'Thiết lập được lưu cho các lần đọc sau. Mũi tên, Space và PageUp/PageDown chuyển trang; không áp dụng khi gõ hoặc bôi chọn.' })], [button('Đặt lại giao diện đọc', () => { const pos = position(); prefs = { ...defaults, voice: prefs.voice, rate: prefs.rate, pitch: prefs.pitch }; applyPrefs(false); restorePosition(pos); saveSettings({ ...prefs }); settingsDialog(); })]);
  }

  function qtDialog() {
    if (!chapter) { notify('Mở nội dung chương trước khi chọn chế độ QT.'); return; }
    const translatedAtSource = sourceVietnamese();
    const choose = el('select', { 'aria-label': 'Chế độ QT' }, [['original', 'Nguyên văn'], ['qt', 'Bản QT'], ['bilingual', 'Song ngữ xen đoạn']].map(([value, text]) => el('option', { value, text })));
    choose.value = translatedAtSource ? 'original' : prefs.qt;
    choose.disabled = translatedAtSource;
    const error = el('p', { class: 'inline-error', hidden: true });
    choose.onchange = async () => {
      const pos = position(); prefs.qt = choose.value; saveSettings({ ...prefs }); error.hidden = true; choose.disabled = true;
      try { await renderChapter(pos); }
      catch (problem) { renderOriginal(); restorePosition(pos); error.textContent = `${problem.message} Đang hiển thị nguyên văn.`; error.hidden = false; }
      finally { choose.disabled = false; }
    };
    showDialog('QT và từ điển', [el('p', { class: 'muted', text: translatedAtSource ? 'Đang hiển thị bản tiếng Việt do website gốc cung cấp. QT cục bộ không áp dụng cho chương này.' : 'QT sử dụng từ điển đã nhập trên máy. Chưa có bộ VietPhrase/Names thì nội dung có thể không đổi. Đây là dịch từ điển, không phải dịch AI.' }), el('label', { class: 'field', text: 'Nội dung hiển thị' }, [choose]), error], [button('Chỉnh từ điển truyện này', () => openDictionary(book)), translatedAtSource ? null : button('Dịch lại chương', async () => { try { await renderChapter(); notify('Đã cập nhật nội dung theo từ điển hiện tại.'); } catch (problem) { error.textContent = problem.message; error.hidden = false; } }, true)].filter(Boolean));
  }

  function searchDialog() {
    const query = el('input', { type: 'search', placeholder: 'Nhập cụm từ cần tìm…', 'aria-label': 'Tìm trong nội dung' });
    const scope = el('select', { 'aria-label': 'Phạm vi tìm' }, [el('option', { value: 'chapter', text: 'Chương đang đọc' }), el('option', { value: 'book', text: 'Nội dung đã tải của truyện' })]);
    const result = el('div');
    const controls = el('div', { class: 'search-reader-bar' });
    const form = el('form', {}, [el('div', { class: 'search-reader-bar' }, [query, button('Tìm', () => {})]), scope]);
    form.querySelector('button').type = 'submit';
    const dialog = showDialog('Tìm trong nội dung', [form, el('p', { class: 'muted small', text: 'Tìm toàn truyện chỉ xét chương đã có nội dung. Kết quả luôn cho biết số chương đã tìm.' }), result, controls]);
    let hits = [], selected = -1, searchGeneration = 0;
    form.onsubmit = async (event) => {
      event.preventDefault();
      const generation = ++searchGeneration, term = query.value.trim();
      clearSearch(); result.replaceChildren(); controls.replaceChildren();
      if (!term) return;
      if (scope.value === 'book') {
        const info = el('p', { class: 'inline-info', text: 'Đang tìm trong nội dung đã tải…' });
        const list = el('div');
        result.append(info, list);
        let after = -1, loading = false;
        async function loadMore() {
          if (loading) return;
          loading = true;
          controls.replaceChildren(el('p', { text: 'Đang tìm…' }));
          try {
            const found = await api(path(`/search?${new URLSearchParams({ q: term, after })}`));
            if (generation !== searchGeneration || !dialog.body.closest('dialog')?.open) return;
            info.textContent = found.indexing
              ? `Đang chuẩn bị tìm kiếm nền: ${found.searched}/${found.total} chương sẵn sàng. Hãy tìm lại sau để cập nhật kết quả.`
              : `Đã tìm ${found.searched}/${found.total} chương. ${found.searched < found.total ? 'Các chương chưa tải chưa được tìm kiếm.' : 'Đã xét toàn bộ nội dung hiện có.'}`;
            for (const hit of found.results || []) list.append(el('button', { class: 'whole-book-result', onclick: async () => { dialog.close(); await loadChapter(hit.index); highlightSearch(term); } }, [el('strong', { text: hit.title || `Chương ${hit.index + 1}` }), el('p', { text: hit.snippet || '' })]));
            if (!list.children.length) list.append(el('p', { text: 'Không có kết quả trong phạm vi đã tìm.' }));
            controls.replaceChildren();
            if (found.next_index != null) {
              after = found.next_index;
              controls.append(button('Xem thêm kết quả', loadMore));
            }
          } catch (error) {
            if (generation === searchGeneration && dialog.body.closest('dialog')?.open) controls.replaceChildren(el('p', { class: 'inline-error', text: error.message }), button('Thử lại', loadMore));
          } finally { loading = false; }
        }
        await loadMore();
      } else {
        hits = highlightSearch(term); selected = -1;
        const count = el('p', { class: 'muted', text: `${hits.length} kết quả trong chương đang đọc.` }); result.append(count);
        function jump(direction) {
          if (!hits.length) return;
          hits[selected]?.classList.remove('current'); selected = (selected + direction + hits.length) % hits.length; hits[selected].classList.add('current'); hits[selected].scrollIntoView({ block: 'center', inline: 'center' }); count.textContent = `${selected + 1} / ${hits.length} kết quả`; scheduleSave();
        }
        controls.append(button('Kết quả trước', () => jump(-1)), button('Kết quả sau', () => jump(1)), button('Về nội dung', () => { if (selected < 0) jump(1); dialog.close(); }, true));
      }
    };
  }

  function clearSearch() {
    for (const mark of content.querySelectorAll('mark.search-hit')) mark.replaceWith(document.createTextNode(mark.textContent));
    content.normalize();
  }
  function highlightSearch(query) {
    clearSearch();
    if (!query) return [];
    const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const lower = query.toLocaleLowerCase('vi');
    for (const node of nodes) {
      const text = node.textContent, normalized = text.toLocaleLowerCase('vi');
      let offset = 0, found = normalized.indexOf(lower), fragment = document.createDocumentFragment();
      if (found < 0) continue;
      while (found >= 0) { fragment.append(document.createTextNode(text.slice(offset, found)), el('mark', { class: 'search-hit', text: text.slice(found, found + query.length) })); offset = found + query.length; found = normalized.indexOf(lower, offset); }
      fragment.append(document.createTextNode(text.slice(offset))); node.replaceWith(fragment);
    }
    return [...content.querySelectorAll('mark.search-hit')];
  }

  function editNote(existing = null, bookmark = false) {
    if (!chapter) return;
    const quote = existing?.quote ?? (bookmark ? '' : lastSelection || window.getSelection()?.toString() || '');
    const text = el('textarea', { value: existing?.note || '', placeholder: bookmark ? 'Ghi chú cho dấu trang (không bắt buộc)…' : 'Ghi lại suy nghĩ của bạn…', 'aria-label': 'Nội dung ghi chú' });
    const color = el('select', { 'aria-label': 'Màu đánh dấu' }, [['yellow', 'Vàng'], ['green', 'Xanh lá'], ['purple', 'Tím'], ['pink', 'Hồng']].map(([value, title]) => el('option', { value, text: title })));
    color.value = existing?.color || 'yellow';
    const error = el('p', { class: 'inline-error', hidden: true });
    const dialog = showDialog(bookmark ? 'Thêm dấu trang' : existing ? 'Sửa ghi chú' : 'Ghi chú đoạn chọn', [el('div', { class: 'note-form' }, [quote ? el('blockquote', { class: 'note-quote', text: quote }) : el('p', { class: 'muted small', text: `Chương ${index + 1} · ${Math.round(position() * 100)}%` }), el('label', { class: 'field', text: 'Ghi chú' }, [text]), el('label', { class: 'field', text: 'Màu' }, [color]), error])], [button('Lưu', async () => {
      if (!bookmark && !quote.trim() && !text.value.trim()) { error.textContent = 'Chọn một đoạn hoặc viết ghi chú trước khi lưu.'; error.hidden = false; return; }
      try { await api('/api/notes', { ...(existing?.id ? { id: existing.id } : {}), book_id: book.id, chapter_index: existing?.chapter_index ?? index, position: existing?.position ?? position(), quote, note: text.value, color: color.value, type: existing?.type || (bookmark ? 'bookmark' : 'note') }); selectionTip.hidden = true; dialog.close(); notify('Đã lưu vào ghi chú của truyện.'); }
      catch (problem) { error.textContent = problem.message; error.hidden = false; }
    }, true)]);
  }

  async function notesDialog() {
    const query = el('input', { type: 'search', placeholder: 'Tìm ghi chú, đoạn trích…', 'aria-label': 'Tìm ghi chú' });
    const list = el('div', {}, [el('p', { text: 'Đang đọc ghi chú…' })]);
    const dialog = showDialog('Ghi chú và dấu trang', [query, list], [button('Ghi chú mới', () => editNote()), button('Đánh dấu vị trí này', () => editNote(null, true), true)]);
    try {
      const result = await api(`/api/notes?${new URLSearchParams({ book_id: book.id })}`);
      const notes = Array.isArray(result) ? result : result.items || [];
      function render() {
        const found = notes.filter((note) => `${note.quote} ${note.note}`.toLocaleLowerCase('vi').includes(query.value.toLocaleLowerCase('vi')));
        list.replaceChildren(...found.map((note) => el('article', { class: 'note-item' }, [el('div', { class: 'note-location', text: `${note.type === 'bookmark' ? 'Dấu trang' : 'Ghi chú'} · Chương ${Number(note.chapter_index) + 1} · ${Math.round((Number(note.position) || 0) * 100)}%` }), note.quote ? el('blockquote', { class: 'note-quote', text: note.quote }) : null, el('p', { class: 'note-content', text: note.note || '' }), el('div', { class: 'note-actions' }, [el('button', { class: 'text-button', text: 'Đến vị trí', onclick: () => { dialog.close(); loadChapter(Number(note.chapter_index) || 0, Number(note.position) || 0); } }), el('button', { class: 'text-button', text: 'Sửa', onclick: () => editNote(note) }), el('button', { class: 'text-button danger', text: 'Xóa', onclick: async () => {
          if (!confirm('Xóa ghi chú này?')) return;
          try { await api(`/api/notes/${encodeURIComponent(note.id)}`, undefined, { method: 'DELETE' }); notes.splice(notes.indexOf(note), 1); render(); }
          catch (error) { notify(error.message, true); }
        } })])] )));
        if (!found.length) list.append(el('p', { class: 'muted', text: 'Chưa có ghi chú phù hợp. Bôi chọn văn bản hoặc thêm dấu trang tại vị trí đang đọc.' }));
      }
      query.oninput = render; render();
    } catch (error) { list.replaceChildren(el('p', { class: 'inline-error', text: error.message })); }
  }

  function toolsDialog() {
    const actions = el('div', { class: 'stack' }, [button(document.fullscreenElement ? 'Thoát toàn màn hình' : 'Toàn màn hình', async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await root.requestFullscreen(); } catch (error) { notify(`Không mở được toàn màn hình: ${error.message}`, true); } }), button('Ẩn thanh công cụ · Esc để hiện', () => { document.getElementById('dialog').close(); root.classList.add('is-focused'); }), button(autoFrame ? 'Dừng tự cuộn' : 'Tự cuộn', () => { document.getElementById('dialog').close(); if (autoFrame) stopAutoScroll(); else startAutoScroll(); }), button('Giữ màn hình thức', async () => {
      if (!navigator.wakeLock) { notify('Trình duyệt này chưa cung cấp tính năng giữ màn hình thức.', true); return; }
      try { if (wakeLock) { await wakeLock.release(); wakeLock = null; notify('Đã tắt giữ màn hình thức.'); } else { wakeLock = await navigator.wakeLock.request('screen'); notify('Đang giữ màn hình thức khi ứng dụng hiển thị.'); } }
      catch (error) { notify(`Không giữ được màn hình thức: ${error.message}`, true); }
    }), button('Sửa văn bản chương', editChapterDialog), button('Cách đọc và giao diện', settingsDialog), button('Thêm dấu trang', () => editNote(null, true))]);
    if (String(book.format).toLowerCase() === 'pdf') actions.prepend(el('a', { class: 'button', href: path('/file'), target: '_blank', rel: 'noopener noreferrer', text: 'Mở PDF gốc ↗' }));
    showDialog('Công cụ đọc', [actions, rangeControl('Tốc độ tự cuộn', 'autoSpeed', 10, 100, 5, ' px/giây'), el('p', { class: 'muted small', text: 'Tự cuộn dừng khi hết chương. Giọng đọc dừng khi về kệ; khi thu nhỏ cửa sổ, hành vi âm thanh phụ thuộc trình duyệt và Windows.' })]);
  }

  function startAutoScroll() {
    if (!chapter || loading) { notify('Chưa có nội dung chương để tự cuộn.'); return; }
    if (prefs.mode !== 'scroll') { notify('Chuyển sang cuộn dọc để dùng tự cuộn.'); return; }
    stopAutoScroll();
    autoLast = performance.now();
    function tick(now) {
      scroller.scrollTop += Math.min(now - autoLast, 100) * prefs.autoSpeed / 1000; autoLast = now;
      if (position() >= .999) { stopAutoScroll(); notify('Đã đến cuối chương.'); return; }
      autoFrame = requestAnimationFrame(tick);
    }
    autoFrame = requestAnimationFrame(tick); notify('Đang tự cuộn. Mở Công cụ hoặc nhấn Esc để dừng.');
  }
  function stopAutoScroll() { if (autoFrame) cancelAnimationFrame(autoFrame); autoFrame = 0; }

  function editChapterDialog() {
    if (!chapter || book.kind === 'comic') { notify('Chỉ sửa văn bản của chương đã mở.', true); return; }
    const title = el('input', { value: chapter.title || '', 'aria-label': 'Tiêu đề chương' });
    const text = el('textarea', { class: 'chapter-edit-text', value: chapter.text || content.innerText, 'aria-label': 'Nội dung chương' });
    const problem = el('p', { class: 'inline-error', hidden: true });
    let unsaved = false;
    text.oninput = title.oninput = () => { unsaved = true; };
    const dialog = showDialog('Sửa văn bản chương', [el('p', { class: 'muted small', text: 'Lưu bản văn bản đã sửa trong thư viện mới. Bản chỉnh sửa bỏ định dạng HTML; bản gốc được giữ để khôi phục.' }), el('label', { class: 'field', text: 'Tiêu đề' }, [title]), text, problem]);
    const nativeDialog = document.getElementById('dialog');
    function cancel(event) { if (unsaved && !confirm('Bỏ thay đổi chưa lưu?')) event.preventDefault(); }
    nativeDialog.addEventListener('cancel', cancel);
    nativeDialog.addEventListener('requestclose', cancel);
    const closeButton = document.getElementById('dialog-close');
    const priorClose = closeButton.onclick;
    closeButton.onclick = () => { if (!unsaved || confirm('Bỏ thay đổi chưa lưu?')) dialog.close(); };
    nativeDialog.addEventListener('close', () => { nativeDialog.removeEventListener('cancel', cancel); nativeDialog.removeEventListener('requestclose', cancel); closeButton.onclick = priorClose; }, { once: true });
    async function save() {
      const pos = position();
      try { stopSpeech(); await api(`${chapterPath()}/edit`, { title: title.value, text: text.value, html: '' }); unsaved = false; dialog.close(); await loadChapter(index, pos); notify('Đã lưu bản chỉnh sửa.'); }
      catch (error) { problem.textContent = error.message; problem.hidden = false; }
    }
    text.addEventListener('keydown', (event) => { if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); } });
    dialog.actions.append(button('Khôi phục bản gốc', async () => {
      if (!confirm('Bỏ bản chỉnh sửa chương này và trở về nội dung gốc đã lưu?')) return;
      const pos = position();
      try { await api(`${chapterPath()}/restore`, {}); unsaved = false; dialog.close(); await loadChapter(index, pos); notify('Đã khôi phục nội dung gốc.'); }
      catch (error) { problem.textContent = error.message; problem.hidden = false; }
    }), button('Lưu văn bản', save, true));
  }

  function refreshVoices() {
    voices = synth?.getVoices() || [];
    if (voiceSelect?.isConnected) renderVoices();
  }
  function renderVoices() {
    const local = voices.filter((voice) => voice.localService), online = voices.filter((voice) => !voice.localService);
    voiceSelect.replaceChildren();
    if (!voices.length) voiceSelect.append(el('option', { value: '', text: 'Chưa có giọng từ trình duyệt' }));
    for (const [name, items] of [['Offline · giọng cục bộ', local], ['Online · cần kết nối mạng', online]]) {
      if (!items.length) continue;
      const group = el('optgroup', { label: name }, items.map((voice) => el('option', { value: voice.voiceURI, text: `${voice.name} · ${voice.lang}` })));
      voiceSelect.append(group);
    }
    if (voices.some((voice) => voice.voiceURI === prefs.voice)) voiceSelect.value = prefs.voice;
    else { const preferred = voices.find((voice) => voice.lang.toLowerCase().startsWith('vi')) || voices.find((voice) => voice.default) || voices[0]; if (preferred) { voiceSelect.value = preferred.voiceURI; prefs.voice = preferred.voiceURI; } }
    speechStatus.textContent = `${local.length} giọng offline, ${online.length} giọng online được trình duyệt cung cấp. ${!local.length ? 'Chưa phát hiện giọng cục bộ. ' : ''}${!online.length ? 'Chưa phát hiện giọng online. ' : ''}Tên giọng và phân loại do trình duyệt báo; hãy nghe thử trước khi dùng.`;
  }

  function voiceDialog() {
    voiceSelect = el('select', { 'aria-label': 'Giọng đọc', onchange: () => { prefs.voice = voiceSelect.value; saveSettings({ ...prefs }); } });
    speechStatus = el('p', { class: 'voice-state', role: 'status' });
    const timer = el('select', { 'aria-label': 'Hẹn giờ dừng' }, [[0, 'Không hẹn giờ'], [5, 'Dừng sau 5 phút'], [15, 'Dừng sau 15 phút'], [30, 'Dừng sau 30 phút'], [60, 'Dừng sau 60 phút']].map(([value, text]) => el('option', { value, text })));
    timer.value = prefs.sleep; timer.onchange = () => { prefs.sleep = Number(timer.value); saveSettings({ ...prefs }); if (speaking) setSleepTimer(); };
    const dialog = showDialog('Nghe truyện', [el('label', { class: 'field', text: 'Giọng có trên máy / trình duyệt' }, [voiceSelect]), speechStatus, el('div', { class: 'reader-setting-grid' }, [rangeControl('Tốc độ', 'rate', .5, 2, .1, '×'), rangeControl('Cao độ', 'pitch', .5, 2, .1)]), el('div', { class: 'tts-controls' }, [button('Nghe thử giọng', () => startSpeech('Đây là giọng đọc truyện của bạn.', true)), button('Lùi đoạn', () => skipSpeech(-1)), button('Tiến đoạn', () => skipSpeech(1)), button('Tạm dừng / tiếp tục', togglePause), button('Dừng', stopSpeech)]), el('label', { class: 'field', text: 'Hẹn giờ' }, [timer]), el('p', { class: 'muted small', text: 'Bôi chọn để nghe từ đoạn chọn. Nếu không chọn, bắt đầu gần vị trí đang đọc; tự tiếp tục chương sau. Tốc độ và cao độ thực tế tùy engine. Web Speech không xuất file audio.' })], [button('Kiểm tra lại giọng', refreshVoices), button('Nghe từ vị trí đang đọc', () => { dialog.close(); startSpeech(); }, true)]);
    refreshVoices();
    if (!synth) speechStatus.textContent = 'Trình duyệt này chưa hỗ trợ Web Speech. Hãy mở ứng dụng bằng Chrome hoặc Edge có giọng đọc được cài.';
  }

  function setSleepTimer() {
    clearTimeout(sleepTimer);
    sleepDeadline = prefs.sleep > 0 ? Date.now() + prefs.sleep * 60000 : 0;
    if (sleepDeadline) sleepTimer = setTimeout(() => { stopSpeech(); notify('Đã dừng nghe theo hẹn giờ.'); }, prefs.sleep * 60000);
  }
  function paragraphsForSpeech() {
    let blocks = [...content.querySelectorAll('p,blockquote,li,pre')].filter((node) => node.textContent.trim() && !node.classList.contains('qt-source') && !node.querySelector('p,blockquote,li,pre'));
    if (!blocks.length && content.textContent.trim()) blocks = [content];
    return blocks;
  }
  function splitSpeech(text) {
    const chunks = [];
    for (const part of text.match(/[^.!?。！？\n]+[.!?。！？\n]*|[.!?。！？\n]+/gu) || [text]) {
      let remaining = part.trim();
      while (remaining.length > 450) { let cut = remaining.lastIndexOf(' ', 450); if (cut < 200) cut = 450; chunks.push(remaining.slice(0, cut)); remaining = remaining.slice(cut).trim(); }
      if (remaining) chunks.push(remaining);
    }
    return chunks;
  }
  function startSpeech(selection = '', preview = false) {
    if (!synth || typeof SpeechSynthesisUtterance === 'undefined') { notify('Trình duyệt chưa cung cấp công cụ đọc. Mở bằng Chrome hoặc Edge và kiểm tra giọng trong hệ thống.', true); return; }
    refreshVoices();
    if (!voices.length) { notify('Chưa có giọng đọc khả dụng. Mở thiết lập giọng và kiểm tra lại sau khi Windows tải xong danh sách.', true); return; }
    if (!chapter && !preview) { notify('Chưa có nội dung chương để nghe.', true); return; }
    stopSpeech();
    speechChunks = [];
    if (selection?.trim()) selection.split(/\n+/).filter((line) => line.trim()).forEach((paragraph, block) => { for (const text of splitSpeech(paragraph)) speechChunks.push({ text, node: null, block }); });
    else {
      const blocks = paragraphsForSpeech();
      const area = scroller.getBoundingClientRect();
      let start = blocks.findIndex((node) => { const rect = node.getBoundingClientRect(); return prefs.mode === 'paged' ? rect.right > area.left + 25 && rect.left < area.right - 25 : rect.bottom > area.top + 30; });
      if (start < 0) start = 0;
      blocks.slice(start).forEach((node, block) => { for (const text of splitSpeech(node.textContent)) speechChunks.push({ text, node, block }); });
    }
    if (!speechChunks.length) { notify(book?.kind === 'comic' ? 'Chương ảnh chưa có văn bản để nghe. OCR ảnh truyện tranh chưa được bật.' : 'Không có văn bản để nghe.', true); return; }
    speechIndex = 0; speaking = true; paused = false;
    speechBar.hidden = false; speechBar.querySelector('.speech-bar-label').textContent = preview ? 'Đang nghe thử giọng' : 'Đang đọc…';
    speechBar.dataset.preview = String(preview);
    if (!preview) setSleepTimer();
    speakCurrent();
    if ('mediaSession' in navigator) {
      try {
        navigator.mediaSession.metadata = new MediaMetadata({ title: chapter?.title || book?.name || 'Nghe thử', artist: book?.name || 'Kệ Truyện' });
        navigator.mediaSession.setActionHandler('play', () => { if (paused) togglePause(); });
        navigator.mediaSession.setActionHandler('pause', () => { if (!paused) togglePause(); });
        navigator.mediaSession.setActionHandler('stop', stopSpeech);
        navigator.mediaSession.setActionHandler('previoustrack', () => loadChapter(index - 1, 0, true, true));
        navigator.mediaSession.setActionHandler('nexttrack', () => loadChapter(index + 1, 0, true, true));
      } catch { /* Media keys vary between browser versions. */ }
    }
  }

  function speakCurrent() {
    if (!speaking || !speechChunks[speechIndex]) return;
    const current = speechChunks[speechIndex], token = ++speechGeneration;
    content.querySelectorAll('.speaking').forEach((node) => node.classList.remove('speaking'));
    if (current.node) { current.node.classList.add('speaking'); current.node.scrollIntoView({ block: 'nearest', inline: 'nearest' }); scheduleSave(); }
    const utterance = new SpeechSynthesisUtterance(current.text);
    const voice = voices.find((item) => item.voiceURI === prefs.voice) || voices.find((item) => item.lang.startsWith('vi')) || voices[0];
    utterance.voice = voice; utterance.lang = voice.lang; utterance.rate = clamp(prefs.rate, .5, 2); utterance.pitch = clamp(prefs.pitch, .5, 2);
    utterance.onend = async () => {
      if (token !== speechGeneration || !speaking) return;
      speechIndex++;
      if (speechIndex < speechChunks.length) speakCurrent();
      else if (speechBar.dataset.preview !== 'true' && index < chapters.length - 1) {
        const deadline = sleepDeadline;
        await loadChapter(index + 1, 0, true, true);
        if (speaking && deadline) {
          clearTimeout(sleepTimer); sleepDeadline = deadline;
          sleepTimer = setTimeout(() => { stopSpeech(); notify('Đã dừng nghe theo hẹn giờ.'); }, Math.max(0, deadline - Date.now()));
        }
      }
      else stopSpeech();
    };
    utterance.onerror = (event) => { if (token === speechGeneration && !['canceled', 'interrupted'].includes(event.error)) { stopSpeech(); notify(`Giọng đọc báo lỗi: ${event.error}. Kiểm tra kết nối hoặc chọn giọng khác; ứng dụng không tự đổi giọng.`, true); } };
    synth.speak(utterance);
  }
  function stopSpeech() {
    speaking = false; paused = false; speechGeneration++; clearTimeout(sleepTimer); sleepDeadline = 0;
    synth?.cancel();
    content?.querySelectorAll('.speaking').forEach((node) => node.classList.remove('speaking'));
    if (speechBar) { speechBar.hidden = true; speechBar.querySelector('button').textContent = 'Ⅱ'; }
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = 'none';
  }
  function togglePause() {
    if (!synth || !speaking) { notify('Chưa có đoạn đang nghe. Nhấn “Nghe từ vị trí đang đọc”.'); return; }
    paused = !paused;
    if (paused) synth.pause(); else synth.resume();
    speechBar.querySelector('button').textContent = paused ? '▶' : 'Ⅱ';
    speechBar.querySelector('.speech-bar-label').textContent = paused ? 'Đã tạm dừng' : 'Đang đọc…';
    if ('mediaSession' in navigator) navigator.mediaSession.playbackState = paused ? 'paused' : 'playing';
  }
  function skipSpeech(direction) {
    if (!speaking || !speechChunks.length) { notify('Bắt đầu nghe trước khi chuyển đoạn.'); return; }
    const block = speechChunks[speechIndex].block;
    let next = speechIndex + direction;
    while (next >= 0 && next < speechChunks.length && speechChunks[next].block === block) next += direction;
    if (direction < 0 && next >= 0) { const previous = speechChunks[next].block; while (next > 0 && speechChunks[next - 1].block === previous) next--; }
    speechGeneration++; synth.cancel(); paused = false; speechIndex = clamp(next, 0, speechChunks.length - 1); speakCurrent();
  }
  synth?.addEventListener('voiceschanged', refreshVoices);

  document.addEventListener('selectionchange', () => {
    if (root.hidden || !content || !chapter || loading) return;
    const selection = window.getSelection();
    const valid = selection && selection.toString().trim() && content.contains(selection.anchorNode) && content.contains(selection.focusNode);
    if (valid) lastSelection = selection.toString();
    selectionTip.hidden = !valid;
  });
  document.addEventListener('keydown', (event) => {
    if (root.hidden || document.querySelector('dialog[open]') || event.ctrlKey || event.metaKey || event.altKey || event.target.closest?.('input,textarea,select,button,[contenteditable=true]')) return;
    if (event.key === 'Escape') { root.classList.remove('is-focused'); stopAutoScroll(); selectionTip.hidden = true; return; }
    if (window.getSelection()?.toString()) return;
    if (['ArrowRight', 'ArrowDown', 'PageDown', ' '].includes(event.key)) { event.preventDefault(); step(event.shiftKey ? -1 : 1); }
    else if (['ArrowLeft', 'ArrowUp', 'PageUp'].includes(event.key)) { event.preventDefault(); step(-1); }
    else if (event.key === 'Home') { event.preventDefault(); restorePosition(0); scheduleSave(); }
    else if (event.key === 'End') { event.preventDefault(); restorePosition(1); scheduleSave(); }
  });
  window.addEventListener('resize', () => {
    if (!book || !chapter) return;
    const pos = lastReadingPosition; clearTimeout(resizeTimer); resizeTimer = setTimeout(() => restorePosition(pos), 100);
  });
  window.addEventListener('dictionarychange', async (event) => {
    if (!book || !chapter || loading || sourceVietnamese() || prefs.qt === 'original' || !['*', book.id].includes(event.detail)) return;
    const pos = position();
    try { await renderChapter(pos); }
    catch (error) { notify(`Đã lưu từ điển nhưng chưa cập nhật được chương: ${error.message}`, true); }
  });
  window.addEventListener('pagehide', () => {
    const entry = record();
    if (entry) fetch(`/api/books/${encodeURIComponent(entry.id)}/progress`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ index: entry.index, position: entry.position }), keepalive: true }).catch(() => {});
    stopSpeech();
  });
  document.addEventListener('visibilitychange', () => { if (document.hidden && book) saveProgress(true); });
  return { open, close };
}
