import { createReader } from './reader.js';
import { createCaptchaDialog } from './captcha.js';

const $ = (id) => document.getElementById(id);
let shutdownToken = '';
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null) continue;
    if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key in node && !key.startsWith('aria')) node[key] = value;
    else node.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child != null) node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

async function api(path, data, options = {}) {
  const { captcha: allowCaptcha = true, ...fetchOptions } = options;
  let response;
  try {
    response = await fetch(path, {
      ...fetchOptions,
      method: options.method || (data === undefined ? 'GET' : 'POST'),
      headers: { ...(data === undefined ? {} : { 'Content-Type': 'application/json' }), ...options.headers },
      body: data === undefined ? options.body : JSON.stringify(data),
    });
  } catch {
    const error = new Error('Không kết nối được ứng dụng cục bộ. Hãy kiểm tra cửa sổ máy chủ rồi thử lại.');
    error.code = 'network_error';
    throw error;
  }
  const type = response.headers.get('content-type') || '';
  if (response.headers.get('X-Shutdown-Token')) shutdownToken = response.headers.get('X-Shutdown-Token');
  let result;
  try { result = type.includes('json') ? await response.json() : await response.text(); }
  catch {
    const error = new Error('Phản hồi của máy chủ chưa có nội dung hợp lệ. Hãy thử lại.');
    error.code = 'parse_error'; error.http_status = response.status;
    throw error;
  }
  if (!response.ok) {
    const error = new Error(typeof result?.error === 'string' ? result.error : `Yêu cầu thất bại (${response.status}). ${typeof result === 'string' ? result.slice(0, 200) : ''}`);
    error.code = typeof result?.code === 'string' ? result.code : 'http_error';
    error.http_status = Number.isInteger(result?.http_status) ? result.http_status : response.status;
    if (Number.isInteger(result?.active_downloads)) error.active_downloads = result.active_downloads;
    if (result?.access && typeof result.access === 'object') {
      error.access = {
        vip: typeof result.access.vip === 'boolean' ? result.access.vip : null,
        state: typeof result.access.state === 'string' ? result.access.state : '',
        evidence: typeof result.access.evidence === 'string' ? result.access.evidence : '',
      };
    }
    if (allowCaptcha && error.code === 'captcha_required' && result?.captcha) {
      await verifyCaptcha(result.captcha);
      return api(path, data, options);
    }
    throw error;
  }
  return result;
}

const verifyCaptcha = createCaptchaDialog({ el, request: (path, data) => api(path, data, { captcha: false }) });

function notify(message, error = false) {
  clearTimeout(notify.timer);
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  notify.timer = setTimeout(() => { $('toast').hidden = true; }, error ? 10000 : 5000);
}

function showDialog(title, children = [], actions = []) {
  const content = el('div');
  content.append(...children);
  $('dialog-title').textContent = title;
  $('dialog-content').replaceChildren(content);
  const actionContent = el('div', { id: 'dialog-actions', class: 'dialog-actions' }, actions);
  $('dialog-actions').replaceWith(actionContent);
  if (!$('dialog').open) $('dialog').showModal();
  const token = Symbol();
  showDialog.token = token;
  return {
    body: content,
    actions: actionContent,
    close: () => { if (showDialog.token === token) $('dialog').close(); },
    title: (value) => { if (showDialog.token === token) $('dialog-title').textContent = value; },
  };
}
function requestDialogClose() {
  if ($('dialog').dispatchEvent(new Event('requestclose', { cancelable: true }))) $('dialog').close();
}
$('dialog-close').onclick = requestDialogClose;
$('dialog').addEventListener('click', (event) => {
  if (event.target !== $('dialog')) return;
  const rect = $('dialog').getBoundingClientRect();
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) requestDialogClose();
});

const state = { books: [], groups: [], sources: [], settings: {}, kind: 'all', view: 'grid', reverse: false, sourcePage: 1, activeView: 'library', offset: 0, limit: 60, total: 0, nextOffset: null };
const libraryCards = new Map();
let settingTimer;
function saveSettings(patch) {
  Object.assign(state.settings, patch);
  clearTimeout(settingTimer);
  settingTimer = setTimeout(async () => {
    try { await api('/api/settings', state.settings); }
    catch (error) { notify(`Chưa lưu được thiết lập: ${error.message}`, true); }
  }, 350);
}
const reader = createReader({ api, el, notify, showDialog, getSettings: () => state.settings.reader || {}, saveSettings: (value) => saveSettings({ reader: value }), openDictionary: (book) => openDictionary(book), onClose: () => (state.activeView === 'library' ? loadLibrary(state.offset) : navigate(state.activeView)) });

function safeMedia(value) {
  if (!value) return '';
  try {
    const url = new URL(value, location.href);
    return ['http:', 'https:'].includes(url.protocol) || /^data:image\/(png|jpeg|gif|webp);base64,/i.test(value) ? url.href : '';
  } catch { return ''; }
}
function cover(book) {
  const box = el('div', { class: 'book-cover' }, [el('span', { class: 'cover-symbol', 'aria-hidden': 'true', text: '▤' }), el('span', { class: 'cover-missing', text: 'Chưa có bìa' })]);
  const src = safeMedia(book.cover);
  if (src) {
    const label = box.querySelector('.cover-missing');
    label.textContent = 'Đang tải bìa…';
    box.setAttribute('aria-busy', 'true');
    const img = el('img', { src, alt: '', loading: 'lazy', referrerPolicy: 'no-referrer' });
    img.onload = () => { box.classList.add('has-cover'); box.setAttribute('aria-busy', 'false'); label.textContent = ''; };
    img.onerror = () => { img.remove(); box.setAttribute('aria-busy', 'false'); label.textContent = 'Không tải được bìa'; };
    box.append(img);
  }
  return box;
}
const percent = (book) => Math.round(Math.min(1, Math.max(0, Number(book.progress) || 0)) * 100);
const sourceName = (id) => state.sources.find((source) => source.id === id)?.name || (id === 'local' ? 'File cục bộ' : id || 'Chưa rõ nguồn');

function card(book, fromSource = false, previous = null) {
  const button = previous?.node || el('button', { class: 'book-card', 'data-book-id': book.id });
  if (!previous) {
    const info = el('div', { class: 'book-info' }, [el('h3'), el('p', { class: 'book-author' }), el('p', { class: 'book-meta' })]);
    if (!fromSource) info.append(el('div', { class: 'book-progress' }, [el('progress', { max: 100 }), el('span')]));
    button.append(cover(book), info);
  } else if (previous.cover !== book.cover && safeMedia(previous.cover) !== safeMedia(book.cover)) {
    button.firstElementChild.replaceWith(cover(book));
  }
  button.onclick = () => bookDetail(book, fromSource);
  const values = [['h3', book.name || 'Chưa có tên'], ['.book-author', book.author || 'Chưa rõ tác giả'],
    ['.book-meta', `${sourceName(book.source || $('source-select').value)}${book.chapter_count ? ` · ${Number(book.chapter_count).toLocaleString('vi-VN')} chương` : ''}`]];
  if (!fromSource) {
    const value = percent(book), progress = button.querySelector('progress'), label = `Đã đọc ${value}%`;
    if (progress.value !== value) progress.value = value;
    if (progress.getAttribute('aria-label') !== label) progress.setAttribute('aria-label', label);
    values.push(['.book-progress span', `${value}%`]);
  }
  for (const [selector, text] of values) {
    const node = button.querySelector(selector);
    if (node.textContent !== text) node.textContent = text;
  }
  return button;
}

function renderLibrary() {
  const books = state.books;
  const grid = $('book-grid'), focused = grid.contains(document.activeElement) ? document.activeElement : null;
  if (grid.className !== `book-grid ${state.view}`) grid.className = `book-grid ${state.view}`;
  const keep = new Set(books.map(book => book.id));
  for (const [id, entry] of libraryCards) if (!keep.has(id)) { entry.node.remove(); libraryCards.delete(id); }
  grid.querySelector('.empty-state')?.remove();
  books.forEach((book, i) => {
    const node = card(book, false, libraryCards.get(book.id));
    libraryCards.set(book.id, { node, cover: book.cover });
    const before = grid.children[i] || null;
    if (node !== before) {
      if (node.parentNode === grid && grid.moveBefore) grid.moveBefore(node, before);
      else grid.insertBefore(node, before);
    }
  });
  if (focused?.isConnected && document.activeElement !== focused) focused.focus({ preventScroll: true });
  const filtered = $('library-search').value || $('library-source').value || $('library-group').value || state.kind !== 'all';
  $('library-count').textContent = `${state.total ? `${state.offset + 1}–${state.offset + books.length}/` : ''}${state.total.toLocaleString('vi-VN')} truyện${filtered ? ' phù hợp' : ''}`;
  $('library-page').textContent = state.total ? `${Math.floor(state.offset / state.limit) + 1}/${Math.ceil(state.total / state.limit)}` : '0/0';
  $('library-previous').disabled = !state.offset; $('library-next').disabled = state.nextOffset === null;
  if (!books.length) $('book-grid').append(el('div', { class: 'empty-state' }, [el('span', { class: 'empty-icon', 'aria-hidden': 'true', text: '▥' }), el('h2', { text: filtered ? 'Không có truyện trong bộ lọc này' : 'Kệ sách đang chờ bạn' }), el('p', { text: filtered ? 'Thử đổi từ khóa hoặc bỏ bớt bộ lọc.' : 'Nhập thư viện điện thoại một lần, hoặc tìm truyện từ QQ, Full, Hako.' }), el('button', { class: 'button primary', text: 'Nhập thư viện hiện có', onclick: importBackup })]));
}

let libraryRequest = 0;
async function loadLibrary(offset = 0, focusPage = false) {
  const request = ++libraryRequest;
  $('book-grid').setAttribute('aria-busy', 'true');
  $('library-error').hidden = true;
  $('library-previous').disabled = $('library-next').disabled = true;
  const params = new URLSearchParams({ q: $('library-search').value, source: $('library-source').value, group: $('library-group').value, sort: $('library-sort').value, kind: state.kind, reverse: state.reverse ? '1' : '0', offset, limit: state.limit });
  try {
    const page = await api(`/api/books?${params}`);
    if (request !== libraryRequest) return;
    state.books = page.items; state.offset = page.offset; state.total = page.total; state.limit = page.limit; state.nextOffset = page.next_offset;
    $('nav-count').textContent = page.shelf_total || '';
    renderLibrary();
    if (focusPage) {
      $('book-grid').scrollIntoView({ block: 'start' });
      $('book-grid').querySelector('.book-card')?.focus({ preventScroll: true });
    }
  } catch (error) {
    if (request !== libraryRequest) return;
    $('library-error').textContent = `${error.message} Dùng nút “Làm mới” để thử lại.`;
    $('library-error').hidden = false;
    $('library-count').textContent = 'Chưa tải được thư viện';
  } finally { if (request === libraryRequest) $('book-grid').setAttribute('aria-busy', 'false'); }
}

async function loadGroups() {
  try {
    state.groups = await api('/api/groups');
    const value = $('library-group').value;
    $('library-group').replaceChildren(el('option', { value: '', text: 'Tất cả' }), ...state.groups.map((group) => el('option', { value: group.id, text: group.name })));
    $('library-group').value = value;
  } catch (error) { notify(`Không lấy được nhóm sách: ${error.message}`, true); }
}

function manageGroups() {
  const name = el('input', { placeholder: 'Tên nhóm mới', maxLength: 100, 'aria-label': 'Tên nhóm mới' });
  const list = el('div', { class: 'dictionary-list' });
  const error = el('p', { class: 'inline-error', hidden: true });
  const run = async (work) => {
    error.hidden = true;
    try { await work(); await loadGroups(); render(); loadLibrary(); return true; }
    catch (problem) { error.textContent = problem.message; error.hidden = false; return false; }
  };
  const render = () => list.replaceChildren(...(state.groups.length ? state.groups.map((group) => {
    const input = el('input', { value: group.name, maxLength: 100, class: 'grow', 'aria-label': `Tên nhóm ${group.name}` });
    return el('div', { class: 'dictionary-entry' }, [input,
      el('button', { class: 'button', text: 'Đổi tên', onclick: () => run(() => api(`/api/groups/${encodeURIComponent(group.id)}/rename`, { name: input.value })) }),
      el('button', { class: 'button danger', text: 'Xóa', onclick: () => confirm(`Xóa nhóm “${group.name}”? Truyện trong nhóm vẫn được giữ, chỉ bỏ khỏi nhóm này.`) && run(() => api(`/api/groups/${encodeURIComponent(group.id)}`, undefined, { method: 'DELETE' })) })]);
  }) : [el('p', { class: 'muted', text: 'Chưa có nhóm nào.' })]));
  const create = el('form', { class: 'dictionary-form', onsubmit: async (event) => {
    event.preventDefault();
    if (await run(() => api('/api/groups', { name: name.value }))) name.value = '';
  } }, [name, el('button', { class: 'button primary', type: 'submit', text: 'Tạo nhóm' })]);
  showDialog('Quản lý nhóm', [el('p', { class: 'muted', text: 'Thêm truyện vào nhóm trong mục Sửa thông tin của từng truyện. Đổi tên giữ nguyên các truyện trong nhóm.' }), create, error, list]);
  render();
}
$('manage-groups').onclick = () => loadGroups().then(manageGroups);

async function navigate(view) {
  state.activeView = view;
  if (view !== 'downloads') stopPolling();
  for (const section of document.querySelectorAll('.view')) section.hidden = section.id !== `${view}-view`;
  for (const button of document.querySelectorAll('[data-nav]')) {
    button.classList.toggle('active', button.dataset.nav === view);
    if (button.dataset.nav === view) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  }
  if (view === 'library') await loadLibrary();
  if (view === 'history') renderHistory();
  if (view === 'downloads') renderDownloads();
  if (view === 'dictionary') renderDictionary($('dictionary-page'));
  if (view === 'settings') renderSettings();
}
document.querySelectorAll('[data-nav]').forEach((button) => { button.onclick = () => navigate(button.dataset.nav); });
document.querySelector('.brand').onclick = (event) => { event.preventDefault(); navigate('library'); };
let searchTimer;
$('library-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadLibrary, 220); });
for (const id of ['library-source', 'library-group', 'library-sort']) $(id).onchange = () => { saveSettings({ library: { ...state.settings.library, sort: $('library-sort').value } }); loadLibrary(); };
$('refresh-library').onclick = () => {
  for (const entry of libraryCards.values()) if (entry.cover && !entry.node.querySelector('img')) entry.cover = null;
  loadLibrary(state.offset);
};
$('library-previous').onclick = () => loadLibrary(Math.max(0, state.offset - state.limit), true);
$('library-next').onclick = () => { if (state.nextOffset !== null) loadLibrary(state.nextOffset, true); };
$('sort-direction').onclick = () => { state.reverse = !state.reverse; $('sort-direction').setAttribute('aria-pressed', String(state.reverse)); loadLibrary(); };
document.querySelectorAll('[data-kind]').forEach((button) => {
  button.onclick = () => {
    state.kind = button.dataset.kind;
    for (const other of document.querySelectorAll('[data-kind]')) { other.classList.toggle('active', other === button); other.setAttribute('aria-pressed', String(other === button)); }
    loadLibrary();
  };
});
document.querySelectorAll('[data-view]').forEach((button) => {
  button.onclick = () => {
    state.view = button.dataset.view;
    document.querySelectorAll('[data-view]').forEach((other) => { other.classList.toggle('active', other === button); other.setAttribute('aria-pressed', String(other === button)); });
    saveSettings({ library: { ...state.settings.library, view: state.view } });
    renderLibrary();
  };
});

async function importBackup() {
  const message = el('p', { class: 'muted', text: 'Đang đọc bộ xuất hiện có. Dữ liệu gốc được giữ nguyên; lần nhập lại giữ tiến độ đã có.' });
  const dialog = showDialog('Nhập thư viện điện thoại', [message]);
  try {
    const report = await api('/api/import/backup', {});
    message.textContent = 'Đã hoàn tất yêu cầu nhập. Kết quả từ máy chủ:';
    dialog.body.append(reportNode(report));
    dialog.actions.replaceChildren(el('button', { class: 'button primary', text: 'Về kệ sách', onclick: () => { dialog.close(); navigate('library'); } }));
    await Promise.all([loadGroups(), loadLibrary()]);
  } catch (error) {
    message.className = 'inline-error';
    message.textContent = error.message;
    dialog.body.append(el('p', { class: 'muted', text: 'Nếu bộ xuất nằm nơi khác, nhập đường dẫn thư mục chứa books/, data/, prefs/.' }));
    const path = el('input', { type: 'text', placeholder: 'C:\\DuLieu\\backup', 'aria-label': 'Đường dẫn thư mục backup' });
    dialog.body.append(path);
    dialog.actions.replaceChildren(el('button', { class: 'button primary', text: 'Nhập từ đường dẫn', onclick: async (event) => {
      if (!path.value.trim()) { path.focus(); return; }
      event.currentTarget.disabled = true;
      try { const report = await api('/api/import/backup', { path: path.value.trim() }); dialog.body.replaceChildren(reportNode(report)); dialog.actions.replaceChildren(el('button', { class: 'button primary', text: 'Về kệ sách', onclick: () => { dialog.close(); navigate('library'); } })); await Promise.all([loadLibrary(), loadGroups()]); }
      catch (error) { message.textContent = error.message; }
      finally { event.target.disabled = false; }
    } }));
  }
}
$('import-backup').onclick = importBackup;
$('import-file').onclick = () => $('file-input').click();
$('file-input').onchange = async () => {
  const files = [...$('file-input').files];
  if (!files.length) return;
  const list = el('div', { class: 'stack' });
  showDialog('Nhập file vào kệ', [list]);
  for (const file of files) {
    const line = el('p', { text: `${file.name}: đang nhập…` }); list.append(line);
    try {
      const result = await api('/api/import/file', undefined, { method: 'POST', body: file, headers: { 'Content-Type': 'application/octet-stream', 'X-Filename': encodeURIComponent(file.name) } });
      line.textContent = `${file.name}: đã nhập ${result.book?.chapter_count || result.chapter_count || ''} chương.`;
      if (result.warnings?.length) list.append(el('details', {}, [
        el('summary', { text: `Lưu ý về tệp gốc (${result.warnings.length})` }),
        el('ul', {}, result.warnings.map(text => el('li', { text }))),
      ]));
    } catch (error) { line.className = 'inline-error'; line.textContent = `${file.name}: ${error.message}`; }
  }
  $('file-input').value = '';
  await loadLibrary();
};

function reportNode(report) {
  const labels = { books: 'Truyện', chapters: 'Chương', groups: 'Nhóm', notes: 'Ghi chú', imported: 'Đã nhập', skipped: 'Đã giữ / bỏ qua', duplicates: 'Mục trùng', orphan_notes: 'Ghi chú chưa ghép sách', warnings: 'Lưu ý', errors: 'Lỗi', added: 'Đã thêm', updated: 'Đã cập nhật', invalid: 'Dòng không hợp lệ', backup: 'Bản sao trước thay đổi' };
  const box = el('dl', { class: 'report' });
  for (const [key, value] of Object.entries(report || {})) box.append(el('dt', { text: labels[key] || key }), el('dd', { text: typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value) }));
  return box;
}

async function bookDetail(initial, fromSource = false) {
  const dialog = showDialog(initial.name || 'Chi tiết truyện', [el('p', { text: 'Đang lấy thông tin…' })]);
  let book;
  const selectedSource = initial.source || $('source-select').value;
  try { book = fromSource ? await api(`/api/sources/${encodeURIComponent(selectedSource)}/detail?${new URLSearchParams({ url: initial.path || initial.url })}`) : await api(`/api/books/${encodeURIComponent(initial.id)}`); }
  catch (error) { dialog.body.replaceChildren(el('p', { class: 'inline-error', text: error.message })); return; }
  dialog.title(book.name || 'Chi tiết truyện');
  const description = el('p', { class: 'book-description', text: book.description || 'Nguồn chưa cung cấp giới thiệu.' });
  const metadata = el('div', { class: 'detail-metadata' }, [el('p', { text: book.author || 'Chưa rõ tác giả' }), el('p', { class: 'muted', text: `${sourceName(book.source || selectedSource)} · ${book.kind === 'comic' ? 'Truyện tranh' : 'Truyện chữ'} · ${book.chapter_count || book.chapters?.length || 0} chương` }), description]);
  const link = safeMedia(book.path || book.url);
  if (link && /^https?:/.test(link)) metadata.append(el('a', { href: link, target: '_blank', rel: 'noopener noreferrer', class: 'text-link', text: 'Mở trang nguồn ↗' }));
  dialog.body.replaceChildren(el('div', { class: 'book-detail' }, [cover(book), metadata]));
  const status = el('p', { class: 'inline-error', hidden: true, role: 'alert' }); dialog.body.append(status);
  if (fromSource) {
    // Reading and downloading from Khám phá keep the novel off the shelf; adding it is a separate choice.
    const url = book.path || book.url || initial.path || initial.url;
    const library = book.library || null;
    const chapters = Array.isArray(book.chapters) ? book.chapters : [];
    const add = async (shelf) => {
      const request = () => api(`/api/sources/${encodeURIComponent(selectedSource)}/add`, { url, shelf });
      let added;
      try { added = await request(); }
      catch (error) {
        // A concurrent change to the record: reading the source once more settles it.
        if (error.http_status !== 400 || !/đã thay đổi/.test(error.message)) throw error;
        added = await request();
      }
      return added.book || added;
    };
    const showError = (error) => { status.className = 'inline-error'; status.textContent = error.message; status.hidden = false; };
    const facts = [];
    if (library?.trashed) facts.push('Truyện đang ở Thùng rác — khôi phục để đọc');
    else if (library && (library.chapter_index || library.position)) facts.push(`Đọc tiếp chương ${library.chapter_index + 1}`);
    if (library?.downloaded) facts.push(`Đã tải ${library.downloaded} chương`);
    if (facts.length) metadata.append(el('p', { class: 'detail-library', text: facts.join(' · ') }));
    const detailNodes = [...dialog.body.childNodes];
    const showDetail = () => dialog.body.replaceChildren(...detailNodes);
    const showToc = () => {
      const list = el('ol', { class: 'toc-list' });
      const openChapter = async (index) => {
        list.inert = true; status.hidden = true;
        try {
          const record = await add(false);
          dialog.close();
          reader.open(record, index, index === library?.chapter_index ? library.position : 0);
        } catch (error) { list.inert = false; showError(error); }
      };
      chapters.forEach((chapter, index) => list.append(el('li', {}, [el('button', { class: `toc-chapter${index === library?.chapter_index ? ' current' : ''}`, text: chapter.title || `Chương ${index + 1}`, onclick: () => openChapter(index) })])));
      const jump = el('input', { type: 'number', min: 1, max: chapters.length, placeholder: 'Đến chương…', 'aria-label': 'Đến chương' });
      jump.onchange = () => { const target = list.children[Number(jump.value) - 1]; if (target) { target.scrollIntoView({ block: 'center' }); target.querySelector('button').focus(); } };
      dialog.body.replaceChildren(el('div', { class: 'toc-head' }, [el('button', { class: 'text-button', text: '← Giới thiệu', onclick: showDetail }), el('strong', { text: `${chapters.length} chương` }), jump]), list, status);
      list.children[library?.chapter_index ?? -1]?.scrollIntoView({ block: 'center' });
    };
    const shelfButton = el('button', { class: 'button primary', text: library?.on_shelf ? 'Đã có trên kệ' : 'Thêm vào kệ', disabled: Boolean(library?.on_shelf), onclick: async () => {
      shelfButton.disabled = true; status.hidden = true;
      try { await add(true); shelfButton.textContent = 'Đã có trên kệ'; notify('Đã thêm vào kệ.'); refreshShelfCount(); }
      catch (error) { showError(error); shelfButton.disabled = false; }
    } });
    const buttons = [
      el('button', { class: 'button', text: 'Tải xuống', onclick: () => downloadDialog({ name: book.name, chapter_count: chapters.length }, () => add(false)) }),
      el('button', { class: 'button', text: 'Mục lục', onclick: showToc }),
      shelfButton,
    ];
    if (library?.trashed) for (const button of buttons) button.disabled = true;
    dialog.actions.replaceChildren(...buttons);
  } else {
    dialog.body.append(el('div', { class: 'download-row' }, [el('button', { class: 'button', text: 'Tải xuống', onclick: () => downloadDialog(book, async () => book) })]));
    const shortcut = el('button', { class: 'button', text: 'Thêm lối tắt', onclick: async (event) => {
      const button = event.currentTarget;
      button.disabled = true; button.textContent = 'Đang tạo…'; status.hidden = true;
      try {
        const result = await api(`/api/books/${encodeURIComponent(book.id)}/shortcut`, {});
        status.className = 'inline-info';
        status.textContent = `Đã tạo lối tắt: ${result.path}${result.warning ? '\n' + result.warning : ''}`;
        status.hidden = false;
        notify('Đã tạo lối tắt trên Desktop.');
      } catch (error) { status.className = 'inline-error'; status.textContent = error.message; status.hidden = false; }
      finally { button.disabled = false; button.textContent = 'Thêm lối tắt'; }
    } });
    dialog.actions.replaceChildren(el('button', { class: 'button danger', text: 'Xóa khỏi kệ', onclick: () => confirmTrash(book) }), el('button', { class: 'button', text: 'Sửa thông tin', onclick: () => editBook(book) }), shortcut, el('button', { class: 'button primary', text: percent(book) ? `Đọc tiếp · ${percent(book)}%` : 'Bắt đầu đọc', onclick: () => { dialog.close(); reader.open(book); } }));
  }
}

// ---- Tải xuống: job progress polled every 2 s only while it can change and is visible ----
const jobStatuses = { queued: 'Đang chờ', running: 'Đang tải', paused: 'Tạm dừng', verification_required: 'Chờ xác minh CAPTCHA', completed: 'Hoàn tất', failed: 'Lỗi', cancelled: 'Đã hủy' };
const poller = { timer: 0, generation: 0, inFlight: false, pending: false, offset: 0, active: new Set() };
const pollable = () => state.activeView === 'downloads' && $('reader').hidden && document.visibilityState === 'visible';
function schedulePoll() {
  clearTimeout(poller.timer);
  if (pollable() && !poller.inFlight && poller.active.size) poller.timer = setTimeout(pollJobs, 2000);
}
function stopPolling() { clearTimeout(poller.timer); poller.generation++; poller.pending = false; }
// An immediate refresh that never overlaps one already in flight.
function refreshJobs() {
  clearTimeout(poller.timer);
  if (!pollable()) return;
  if (poller.inFlight) poller.pending = true; else pollJobs();
}
async function pollJobs() {
  poller.inFlight = true;
  const generation = poller.generation;
  try {
    const page = await api(`/api/jobs?offset=${poller.offset}`);
    if (generation === poller.generation) {
      const jobs = Array.isArray(page) ? page : page.jobs || [];
      const active = new Set(jobs.filter((job) => ['queued', 'running'].includes(job.status)).map((job) => job.id));
      const ended = [...poller.active].some((id) => !active.has(id));
      poller.active = active;
      renderJobs(jobs, page.next_offset);
      if (ended) renderDownloaded();
    }
  } catch (error) {
    if (generation === poller.generation) { poller.active = new Set(); $('download-jobs').replaceChildren(el('p', { class: 'inline-error', text: error.message })); }
  } finally {
    poller.inFlight = false;
    if (poller.pending && pollable()) { poller.pending = false; pollJobs(); } else { poller.pending = false; schedulePoll(); }
  }
}
function downloadsChanged() { if (state.activeView === 'downloads') { refreshJobs(); renderDownloaded(); } }
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') refreshJobs(); else stopPolling(); });

function renderJobs(jobs, nextOffset) {
  const rows = jobs.map((job) => {
    const total = Number(job.total) || 0, done = Number(job.completed) || 0, pct = total ? Math.floor(done * 100 / total) : 0;
    const row = el('article', { class: 'download-job' }, [el('h3', { text: job.name || 'Tải chương' }), el('p', { class: 'muted small', text: `${pct}% · ${done}/${total} chương · ${jobStatuses[job.status] || job.status}` }), el('progress', { value: done, max: total || 1, 'aria-label': `Tiến độ tải ${pct}%` })]);
    if (Number(job.image_total) > 0) row.append(el('p', { class: 'job-images', text: `Chương ${Number(job.start) + done + 1}: ảnh ${job.image_done}/${job.image_total}` }));
    if (job.error) row.append(el('p', { class: 'inline-error', text: job.error }));
    const controls = el('div', { class: 'header-actions' });
    if (job.status === 'verification_required' && job.captcha) controls.append(el('button', { class: 'button primary', text: 'Xác minh CAPTCHA', onclick: async () => {
      try { await verifyCaptcha(job.captcha); } catch (error) { notify(error.message, true); }
      refreshJobs();
    } }));
    for (const [action, text, enabled] of [['pause', 'Tạm dừng', job.status === 'running'], ['resume', 'Tiếp tục', job.status === 'paused'], ['retry', 'Thử lại phần còn lại', ['failed', 'cancelled'].includes(job.status)], ['cancel', 'Hủy tải', ['queued', 'running', 'paused', 'verification_required'].includes(job.status)], ['delete', 'Xóa khỏi danh sách', ['completed', 'failed', 'cancelled'].includes(job.status)]]) {
      if (enabled) controls.append(el('button', { class: 'button', text, onclick: async (event) => {
        event.currentTarget.disabled = true;
        try { await api(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, {}); }
        catch (error) { notify(error.message, true); }
        refreshJobs();
      } }));
    }
    row.append(controls);
    return row;
  });
  if (!rows.length) rows.push(el('p', { class: 'muted', text: 'Chưa có yêu cầu tải. Chọn Tải xuống ở một truyện trong Khám phá hoặc trên kệ.' }));
  const pages = el('div', { class: 'header-actions' });
  if (poller.offset) pages.append(el('button', { class: 'button', text: 'Tác vụ trước', onclick: () => { poller.offset = Math.max(0, poller.offset - 50); refreshJobs(); } }));
  if (nextOffset != null) pages.append(el('button', { class: 'button', text: 'Tác vụ tiếp', onclick: () => { poller.offset = nextOffset; refreshJobs(); } }));
  $('download-jobs').replaceChildren(...rows, pages);
}

async function clearDownloads(book, refresh) {
  if (!confirm('Xóa nội dung đã tải của truyện này để giải phóng dung lượng? Chương đã sửa được giữ. Có thể tải lại từ nguồn khi cần.')) return;
  const send = (confirmed) => api(`/api/books/${encodeURIComponent(book.id)}/downloads/clear`, { confirmed });
  let result;
  try { result = await send(false); }
  catch (error) {
    if (error.code !== 'legacy_confirmation') { notify(error.message, true); return; }
    if (!confirm(error.message)) return;
    try { result = await send(true); } catch (problem) { notify(problem.message, true); return; }
  }
  // Nothing is freed on disk yet: say what is waiting for deletion and what SQLite reuses.
  const parts = [`Đã xóa ${result.cleared} chương.`];
  if (result.pending_file_bytes) parts.push(`Khoảng ${formatBytes(result.pending_file_bytes)} ảnh đang chờ xóa khỏi ổ đĩa trong nền.`);
  if (result.sqlite_reusable_bytes) parts.push(`${formatBytes(result.sqlite_reusable_bytes)} trong tệp thư viện được để SQLite dùng lại; tệp chỉ nhỏ lại khi bấm Thu gọn trong Thiết lập.`);
  notify(parts.join(' '));
  refresh();
}

async function renderDownloaded(offset = 0) {
  const target = $('downloaded-list');
  let page;
  try { page = await api(`/api/downloads?offset=${offset}`); }
  catch (error) { target.replaceChildren(el('p', { class: 'inline-error', text: error.message })); return; }
  const refresh = () => renderDownloaded(offset);
  const rows = page.items.map((book) => {
    const info = el('div', { class: 'saved-info' }, [el('h3', {}, [book.name || 'Chưa có tên', el('span', { class: `saved-badge${book.archived ? ' off' : ''}`, text: book.archived ? 'Ngoài kệ' : 'Trên kệ' })]), el('p', { class: 'muted', text: `${sourceName(book.source)} · Đã tải ${book.downloaded_count}/${book.chapter_count || '?'} chương` })]);
    const actions = [el('button', { class: 'button danger', text: 'Xóa nội dung đã tải', onclick: () => clearDownloads(book, refresh) })];
    if (book.archived) actions.push(el('button', { class: 'button', text: 'Thêm vào kệ', onclick: () => putOnShelf(book, refresh) }));
    actions.push(el('button', { class: 'button primary', text: 'Đọc', onclick: () => { stopPolling(); openSaved(book, refresh); } }));
    return el('article', { class: 'saved-row' }, [cover(book), info, el('div', { class: 'header-actions' }, actions)]);
  });
  if (!rows.length) rows.push(el('p', { class: 'muted', text: 'Chưa có truyện nào được tải để đọc offline.' }));
  const pages = el('div', { class: 'header-actions' });
  if (offset) pages.append(el('button', { class: 'button', text: 'Trang trước', onclick: () => renderDownloaded(Math.max(0, offset - 60)) }));
  if (page.next_offset != null) pages.append(el('button', { class: 'button', text: 'Trang sau', onclick: () => renderDownloaded(page.next_offset) }));
  target.replaceChildren(...rows, pages);
}

function renderDownloads() {
  poller.offset = 0;
  $('download-jobs').replaceChildren(el('p', { class: 'muted', text: 'Đang lấy tiến độ…' }));
  refreshJobs();
  renderDownloaded();
}

async function refreshShelfCount() {
  try { $('nav-count').textContent = (await api('/api/books?limit=1')).shelf_total || ''; } catch { /* the next shelf load shows it */ }
}

// resolve() returns the library record to download, creating an off-shelf one only on confirm.
function downloadDialog(book, resolve) {
  const total = Number(book.chapter_count) || 0;
  const all = el('input', { type: 'radio', name: 'download-scope', checked: true });
  const range = el('input', { type: 'radio', name: 'download-scope' });
  const from = el('input', { type: 'number', min: 1, max: total, value: 1, 'aria-label': 'Từ chương' });
  const to = el('input', { type: 'number', min: 1, max: total, value: total, 'aria-label': 'Đến chương' });
  const status = el('p', { class: 'inline-error', role: 'alert', hidden: true });
  const start = el('button', { class: 'button primary', text: 'Bắt đầu tải' });
  const chosen = () => {
    if (all.checked) return total > 0 ? { start: 0, count: -1 } : null;
    const a = Number(from.value), b = Number(to.value);
    return from.value !== '' && to.value !== '' && Number.isInteger(a) && Number.isInteger(b) && a >= 1 && a <= b && b <= total ? { start: a - 1, count: b - a + 1 } : null;
  };
  const update = () => { start.disabled = !chosen(); };
  for (const input of [from, to]) input.addEventListener('focus', () => { range.checked = true; update(); });
  for (const input of [all, range, from, to]) input.addEventListener('input', update);
  const dialog = showDialog(`Tải xuống · ${book.name || 'truyện'}`, [
    el('label', { class: 'download-choice' }, [all, el('span', { text: `Toàn bộ (${total} chương)` })]),
    el('label', { class: 'download-choice' }, [range, el('span', { text: 'Từ chương' }), from, el('span', { text: 'đến' }), to]),
    status,
  ], [el('button', { class: 'button', text: 'Hủy', onclick: () => dialog.close() }), start]);
  start.onclick = async () => {
    const body = chosen();
    if (!body) return;
    start.disabled = true; status.hidden = true;
    try {
      const record = await resolve();
      await api(`/api/books/${encodeURIComponent(record.id)}/download`, body);
      dialog.body.replaceChildren(el('p', { class: 'inline-info', text: 'Đã tạo yêu cầu tải. Xem tiến độ trong Tải xuống.' }));
      dialog.actions.replaceChildren(el('button', { class: 'button primary', text: 'Đóng', onclick: () => dialog.close() }));
      downloadsChanged();
    } catch (error) { status.textContent = error.message; status.hidden = false; update(); }
  };
  update();
}

// Opens a saved record; a History row may outlive its record (cleaned after 31 days).
async function openSaved(book, refresh) {
  let fresh;
  try { fresh = await api(`/api/books/${encodeURIComponent(book.id)}`); }
  catch (error) {
    if (error.http_status !== 404) { notify(error.message, true); return; }
    const status = el('p', { class: 'inline-error', role: 'alert', hidden: true });
    const reopen = el('button', { class: 'button primary', text: 'Mở lại từ nguồn', onclick: async () => {
      reopen.disabled = true; status.hidden = true;
      try {
        const added = await api(`/api/sources/${encodeURIComponent(book.source)}/add`, { url: book.path, shelf: false });
        dialog.close();
        reader.open(added.book || added, book.chapter_index || 0, book.position || 0);
      } catch (problem) { status.textContent = problem.message; status.hidden = false; reopen.disabled = false; }
    } });
    const dialog = showDialog('Mục lịch sử đã hết hạn', [el('p', { text: 'Mục lịch sử này đã hết hạn và dữ liệu tạm đã được dọn.' }), status], [el('button', { class: 'button', text: 'Đóng', onclick: () => dialog.close() }), reopen]);
    refresh?.();
    return;
  }
  reader.open(fresh);
}

async function putOnShelf(book, refresh) {
  try { await api(`/api/books/${encodeURIComponent(book.id)}/update`, { archived: false }); notify('Đã thêm vào kệ.'); refreshShelfCount(); }
  catch (error) { notify(error.message, true); }
  refresh();
}

async function renderHistory(offset = 0) {
  const target = $('history-list');
  target.replaceChildren(el('p', { class: 'muted', text: 'Đang mở lịch sử…' }));
  const since = Math.floor(Date.now() / 1000) - 30 * 86400;
  let page;
  try { page = await api(`/api/books?${new URLSearchParams({ archived: '1', read_since: since, sort: 'recent', offset, limit: 60 })}`); }
  catch (error) { target.replaceChildren(el('p', { class: 'inline-error', text: error.message })); return; }
  const refresh = () => renderHistory(offset);
  const rows = page.items.map((book) => el('article', { class: 'saved-row' }, [cover(book), el('div', { class: 'saved-info' }, [
    el('h3', { text: book.name || 'Chưa có tên' }),
    el('p', { class: 'muted', text: `${sourceName(book.source)} · Chương ${(Number(book.chapter_index) || 0) + 1}/${book.chapter_count || '?'} · Đọc lần cuối ${new Date(book.read_at * 1000).toLocaleDateString('vi-VN')}` }),
  ]), el('div', { class: 'header-actions' }, [
    el('button', { class: 'button', text: 'Thêm vào kệ', onclick: () => putOnShelf(book, refresh) }),
    el('button', { class: 'button primary', text: 'Đọc tiếp', onclick: () => openSaved(book, refresh) }),
  ])]));
  if (!rows.length) rows.push(el('p', { class: 'muted', text: 'Chưa có truyện nào được đọc ngoài kệ.' }));
  const pages = el('div', { class: 'header-actions' });
  if (offset) pages.append(el('button', { class: 'button', text: 'Trang trước', onclick: () => renderHistory(Math.max(0, offset - 60)) }));
  if (page.next_offset != null) pages.append(el('button', { class: 'button', text: 'Trang sau', onclick: () => renderHistory(page.next_offset) }));
  target.replaceChildren(...rows, pages);
}

async function refreshShelf() {
  await loadLibrary(state.offset);
}

function confirmTrash(book) {
  const error = el('p', { class: 'inline-error', role: 'alert', hidden: true });
  const dialog = showDialog('Xóa truyện khỏi kệ?', [el('h3', { text: book.name }), el('p', { text: 'Truyện được chuyển vào Thùng rác trong 30 ngày. Tiến độ, ghi chú và nội dung đã tải được giữ để khôi phục. Sau 30 ngày, truyện sẽ tự bị xóa vĩnh viễn khi ứng dụng đang chạy hoặc được mở lại.' }), error]);
  dialog.actions.append(el('button', { class: 'button', text: 'Hủy', onclick: dialog.close }), el('button', { class: 'button danger', text: 'Chuyển vào thùng rác', onclick: async (event) => {
    event.currentTarget.disabled = true; error.hidden = true;
    try {
      await api(`/api/books/${encodeURIComponent(book.id)}/trash`, {});
      dialog.close(); await refreshShelf(); notify('Đã chuyển truyện vào Thùng rác. Bạn có 30 ngày để khôi phục.');
    } catch (problem) { error.textContent = problem.message; error.hidden = false; event.target.disabled = false; }
  } }));
}

async function openTrash(offset = 0) {
  const list = el('div', {}, [el('p', { text: 'Đang mở thùng rác…' })]);
  const dialog = showDialog('Thùng rác · giữ 30 ngày', [el('p', { class: 'muted', text: 'Có thể khôi phục trước ngày hết hạn hoặc xóa vĩnh viễn ngay. Truyện quá 30 ngày tự được dọn khi ứng dụng đang chạy hoặc vào lần mở tiếp theo.' }), list]);
  const token = showDialog.token;
  dialog.actions.append(el('button', { class: 'button', text: 'Làm mới', onclick: () => openTrash(offset) }), el('button', { class: 'button', text: 'Đóng', onclick: dialog.close }));
  try {
    const page = await api(`/api/books?${new URLSearchParams({ trashed: '1', offset, limit: 60 })}`);
    if (!$('dialog').open || showDialog.token !== token) return;
    list.replaceChildren();
    const books = page.items; offset = page.offset;
    if (!books.length) list.append(el('p', { class: 'muted', text: 'Thùng rác đang trống.' }));
    for (const book of books) {
      const expiry = Number(book.trash_expires_at) * 1000;
      const days = Math.max(0, Math.ceil((expiry - Date.now()) / 86400000));
      const error = el('p', { class: 'inline-error', role: 'alert', hidden: true });
      const row = el('article', { class: 'note-item' }, [el('h3', { text: book.name }), el('p', { class: 'muted small', text: `Còn ${days} ngày · Xóa vĩnh viễn lúc ${new Date(expiry).toLocaleString('vi-VN')}` })]);
      row.append(el('div', { class: 'note-actions' }, [el('button', { class: 'button', text: 'Khôi phục về kệ', disabled: !days, onclick: async () => {
        row.inert = true; error.hidden = true;
        try {
          await api(`/api/books/${encodeURIComponent(book.id)}/restore`, {});
          await refreshShelf(); if ($('dialog').open && showDialog.token === token) await openTrash(offset); notify('Đã khôi phục truyện cùng tiến độ và ghi chú.');
        } catch (problem) { row.inert = false; error.textContent = problem.message; error.hidden = false; }
      } }), el('button', { class: 'button danger', text: 'Xóa vĩnh viễn', onclick: () => confirmPurge(book, offset) })]), error);
      list.append(row);
    }
    list.append(el('nav', { class: 'dictionary-pagination', 'aria-label': 'Trang thùng rác' }, [
      el('button', { id: 'trash-previous', class: 'button', text: 'Trang trước', disabled: !offset, onclick: () => openTrash(Math.max(0, offset - page.limit)) }),
      el('span', { role: 'status', text: page.total ? `${offset + 1}–${offset + books.length}/${page.total}` : '0/0' }),
      el('button', { id: 'trash-next', class: 'button', text: 'Trang sau', disabled: page.next_offset === null, onclick: () => openTrash(page.next_offset) })]));
  } catch (error) { if (showDialog.token === token) list.replaceChildren(el('p', { class: 'inline-error', role: 'alert', text: error.message })); }
}

function confirmPurge(book, offset = 0) {
  const error = el('p', { class: 'inline-error', role: 'alert', hidden: true });
  const dialog = showDialog('Xóa vĩnh viễn truyện?', [el('h3', { text: book.name }), el('p', { text: 'Truyện, tiến độ đọc, ghi chú/dấu trang và nội dung chương đã lưu trong app sẽ bị xóa. Bản sao tệp nhập và ảnh cache không còn truyện nào dùng cũng sẽ được dọn. Sau đó không thể khôi phục từ Thùng rác.' }), el('p', { class: 'muted small', text: 'Bộ xuất điện thoại, tệp nhập gốc bên ngoài app, từ điển và tệp/ảnh dùng chung được giữ.' }), error]);
  const token = showDialog.token;
  dialog.actions.append(el('button', { class: 'button', text: 'Hủy', onclick: () => openTrash(offset) }), el('button', { class: 'button danger', text: 'Xác nhận xóa vĩnh viễn', onclick: async (event) => {
    event.currentTarget.disabled = true; error.hidden = true;
    try {
      await api(`/api/books/${encodeURIComponent(book.id)}`, undefined, { method: 'DELETE' });
      if ($('dialog').open && showDialog.token === token) await openTrash(offset); notify('Đã xóa truyện. Tệp không còn dùng sẽ được dọn nền sau khi kiểm tra tham chiếu.');
    } catch (problem) { error.textContent = problem.message; error.hidden = false; event.target.disabled = false; }
  } }));
}
$('open-trash').onclick = () => openTrash();

function editBook(book) {
  const name = el('input', { value: book.name || '', required: true });
  const author = el('input', { value: book.author || '' });
  const groups = el('div', { class: 'checkbox-list' }, state.groups.map((group) => el('label', {}, [el('input', { type: 'checkbox', value: group.id, checked: (book.groups || []).some((item) => String(typeof item === 'object' ? item.id : item) === String(group.id)) }), group.name])));
  const error = el('p', { class: 'inline-error', hidden: true });
  const dialog = showDialog('Sửa thông tin truyện', [el('label', { class: 'field', text: 'Tên truyện' }, [name]), el('label', { class: 'field', text: 'Tác giả' }, [author]), el('h3', { text: 'Nhóm hiện có' }), state.groups.length ? groups : el('p', { class: 'muted', text: 'Chưa có nhóm. Tạo nhóm bằng nút Quản lý nhóm trên kệ.' }), error]);
  dialog.actions.append(el('button', { class: 'button primary', text: 'Lưu thông tin', onclick: async () => {
    if (!name.reportValidity()) return;
    try { await api(`/api/books/${encodeURIComponent(book.id)}/update`, { name: name.value.trim(), author: author.value.trim(), groups: [...groups.querySelectorAll('input:checked')].map((input) => input.value) }); dialog.close(); loadLibrary(); }
    catch (problem) { error.textContent = problem.message; error.hidden = false; }
  } }));
}

$('source-search-form').onsubmit = (event) => { event.preventDefault(); state.sourcePage = 1; searchSource(); };
$('source-next').onclick = () => { state.sourcePage += 1; searchSource(true); };
let sourceRequest = 0;
async function searchSource(append = false) {
  const query = $('source-query').value.trim();
  if (!query || !$('source-select').value) return;
  const request = ++sourceRequest;
  const button = $('source-search-form').querySelector('button'); button.disabled = true;
  $('source-status').className = 'results-line'; $('source-status').textContent = `Đang tìm trên ${sourceName($('source-select').value)}…`;
  $('source-next').hidden = true;
  if (!append) $('source-results').replaceChildren();
  try {
    const source = $('source-select').value;
    const result = await api(`/api/sources/${encodeURIComponent(source)}/search?${new URLSearchParams({ q: query, page: state.sourcePage })}`);
    if (request !== sourceRequest) return;
    for (const book of result.items || []) $('source-results').append(card({ ...book, source }, true));
    $('source-status').textContent = `${(result.items || []).length} kết quả trên trang ${state.sourcePage}. Chọn truyện để xem chi tiết.`;
    if (!result.items?.length) $('source-status').textContent = 'Nguồn không trả về kết quả. Thử từ khóa ngắn hơn hoặc đổi nguồn.';
    $('source-next').hidden = !result.next;
  } catch (error) { if (request === sourceRequest) { $('source-status').className = 'inline-error'; $('source-status').textContent = error.message; } }
  finally { if (request === sourceRequest) button.disabled = false; }
}

function openDictionary(book) {
  const dialog = showDialog('Từ điển QT');
  renderDictionary(dialog.body, book);
}

function renderDictionary(target, book = null) {
  const kind = el('select', {}, [['vietphrase', 'VietPhrase'], ['name', 'Names'], ['hanviet', 'Hán Việt'], ['pronouns', 'Đại từ'], ['rules', 'Luật']].map(([value, text]) => el('option', { value, text })));
  const scope = el('select', {}, [el('option', { value: '*', text: 'Chung cho mọi truyện' }), ...(book ? [el('option', { value: book.id, text: `Riêng: ${book.name}` })] : [])]);
  const query = el('input', { type: 'search', placeholder: 'Tìm từ hoặc nghĩa…', 'aria-label': 'Tìm trong từ điển' });
  const status = el('div', { class: 'muted', role: 'status' });
  const list = el('div', { class: 'dictionary-list' });
  const pagination = el('div', { class: 'dictionary-pagination' });
  const word = el('input', { placeholder: 'Từ gốc', required: true, 'aria-label': 'Từ gốc' });
  const meaning = el('input', { placeholder: 'Nghĩa tiếng Việt', required: true, 'aria-label': 'Nghĩa tiếng Việt' });
  const form = el('form', { class: 'dictionary-form' }, [word, meaning, el('button', { type: 'submit', class: 'button primary', text: 'Lưu mục từ' })]);
  let listRequest = 0, offset = 0;
  async function load(reset = true) {
    if (reset) offset = 0;
    const request = ++listRequest;
    status.textContent = 'Đang đọc từ điển…';
    list.inert = true;
    pagination.inert = true;
    try {
      const result = await api(`/api/dictionaries?${new URLSearchParams({ kind: kind.value, scope: scope.value, q: query.value, offset, limit: 100 })}`);
      if (request !== listRequest) return;
      if (Array.isArray(result)) throw new Error('Hãy khởi động lại máy chủ ứng dụng để dùng danh sách từ điển phân trang.');
      const entries = result.items || [], total = Number(result.total) || 0, limit = Number(result.limit) || 100;
      if (offset >= total && total > 0) { offset = Math.floor((total - 1) / limit) * limit; return load(false); }
      offset = Number(result.offset) || 0;
      const emptyHint = query.value.trim() ? ' Thử từ khóa khác.' : scope.value === '*' ? ' Chưa có mục trong loại từ điển này; chọn loại khác hoặc nhập file.' : ' Chưa có nghĩa riêng; QT vẫn dùng nghĩa chung nếu đã có.';
      status.textContent = `${total.toLocaleString('vi-VN')} mục từ trong phạm vi và từ khóa đang chọn.${total ? '' : emptyHint}`;
      status.className = 'muted';
      list.replaceChildren(...entries.map((entry) => el('div', { class: 'dictionary-entry' }, [el('button', { class: 'dictionary-word', onclick: () => { word.value = entry.word; meaning.value = entry.meaning; word.focus(); } }, [el('strong', { text: entry.word }), el('span', { text: entry.meaning })]), el('button', { class: 'icon-button danger', text: '×', 'aria-label': `Xóa mục ${entry.word}`, onclick: async () => {
        if (!confirm(`Xóa nghĩa “${entry.word}” trong phạm vi đang chọn?`)) return;
        try { await api(`/api/dictionaries?${new URLSearchParams({ kind: kind.value, scope: scope.value, word: entry.word })}`, undefined, { method: 'DELETE' }); window.dispatchEvent(new CustomEvent('dictionarychange', { detail: scope.value })); await load(false); }
        catch (error) { status.textContent = error.message; }
      } })])));
      pagination.replaceChildren();
      if (total > limit) {
        const previous = el('button', { class: 'button', text: 'Trang trước', disabled: offset === 0, onclick: () => { offset = Math.max(0, offset - limit); load(false); } });
        const next = el('button', { class: 'button', text: 'Trang sau', disabled: offset + limit >= total, onclick: () => { offset += limit; load(false); } });
        pagination.append(previous, el('span', { text: `${Math.floor(offset / limit) + 1}/${Math.ceil(total / limit)}` }), next);
      }
      list.scrollTop = 0;
    } catch (error) { if (request === listRequest) { status.className = 'inline-error'; status.textContent = error.message; list.replaceChildren(el('button', { class: 'button', text: 'Thử lại', onclick: () => load(false) })); pagination.replaceChildren(); } }
    finally { if (request === listRequest) { list.inert = false; pagination.inert = false; } }
  }
  form.onsubmit = async (event) => {
    event.preventDefault();
    try { await api('/api/dictionaries', { kind: kind.value, scope: scope.value, word: word.value.trim(), meaning: meaning.value.trim() }); word.value = ''; meaning.value = ''; window.dispatchEvent(new CustomEvent('dictionarychange', { detail: scope.value })); await load(false); notify('Đã lưu nghĩa và yêu cầu cập nhật bản QT đang đọc.'); }
    catch (error) { status.textContent = error.message; }
  };
  const sample = el('textarea', { placeholder: 'Đoạn mẫu để thử luật, ví dụ: 第12章', 'aria-label': 'Đoạn mẫu để thử luật QT', rows: 3 });
  const preview = el('div');
  const ruleHelp = el('details', { hidden: kind.value !== 'rules' }, [el('summary', { text: 'Cú pháp và thử luật QT' }), el('p', { class: 'muted small', text: 'Mẫu dùng {n}, {n1}…{n8} cho số và {word}, {word1}…{word8} cho 1–32 chữ Hán. Bản dịch dùng lại placeholder hoặc {lookup:word} để tra từ điển. Ví dụ: 第{n}章 = Chương {n}. Đây là cú pháp của ứng dụng này.' }), sample, el('button', { class: 'button', text: 'Thử mẫu và nghĩa ở trên', onclick: async () => {
    try { preview.replaceChildren(reportNode(await api('/api/dictionaries/rules/preview', { pattern: word.value, replacement: meaning.value, sample: sample.value, book_id: book?.id || '' }))); }
    catch (error) { preview.replaceChildren(el('p', { class: 'inline-error', text: error.message })); }
  } }), preview]);
  kind.onchange = () => { ruleHelp.hidden = kind.value !== 'rules'; load(); }; scope.onchange = load;
  let timer; query.oninput = () => { clearTimeout(timer); timer = setTimeout(load, 220); };
  const file = el('input', { type: 'file', accept: '.txt,.ini,.json', hidden: true });
  const mode = el('select', { 'aria-label': 'Cách nhập từ điển' }, [el('option', { value: 'merge', text: 'Gộp, ghi đè mục trùng' }), el('option', { value: 'missing', text: 'Chỉ thêm mục thiếu' }), el('option', { value: 'replace', text: 'Thay thế phạm vi đã chọn' })]);
  file.onchange = async () => {
    if (!file.files[0]) return;
    if (mode.value === 'replace' && !confirm('Thay toàn bộ mục từ của loại và phạm vi đang chọn bằng nội dung file? Máy chủ sẽ giữ bản sao dữ liệu trước thay đổi.')) return;
    try { const report = await api('/api/dictionaries/import', { kind: kind.value, scope: scope.value, mode: mode.value, text: await file.files[0].text() }); window.dispatchEvent(new CustomEvent('dictionarychange', { detail: scope.value })); await load(); status.append(reportNode(report)); }
    catch (error) { status.textContent = error.message; }
    file.value = '';
  };
  target.replaceChildren(el('div', { class: 'dictionary-toolbar' }, [el('label', { text: 'Loại từ điển' }, [kind]), el('label', { text: 'Phạm vi' }, [scope]), query]), el('p', { class: 'muted small', text: 'Trong mỗi phạm vi, Names ưu tiên hơn VietPhrase; nghĩa riêng truyện ưu tiên nghĩa chung. Mỗi dòng nhập dùng dạng từ=nghĩa. Các nghĩa ngăn bằng ¦ được giữ nguyên; bản QT dùng nghĩa đầu tiên.' }), form, ruleHelp, status, list, pagination, el('div', { class: 'dictionary-import' }, [mode, el('button', { class: 'button', text: 'Nhập file từ điển', onclick: () => file.click() }), el('button', { class: 'button', text: 'Xuất phạm vi này', onclick: async () => {
    try { const result = await api(`/api/dictionaries/export?${new URLSearchParams({ kind: kind.value, scope: scope.value })}`); const url = URL.createObjectURL(new Blob([result.text], { type: 'text/plain;charset=utf-8' })); const link = el('a', { href: url, download: `${kind.value}-${scope.value}.txt` }); link.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); }
    catch (error) { status.textContent = error.message; }
  } }), file]));
  load();
}

const formatBytes = value => value < 1024 ? `${value} B` : `${(value / 1024 ** (value < 1024 ** 2 ? 1 : 2)).toLocaleString('vi-VN', { maximumFractionDigits: 2 })} ${value < 1024 ** 2 ? 'KiB' : 'MiB'}`;

function chapterCachePanel(initial, problem = '') {
  const summary = el('dl', { class: 'report' });
  const status = el('p', { class: 'muted small', role: 'status' });
  const error = el('p', { class: 'inline-error', role: 'alert', hidden: !problem, text: problem });
  const limit = el('input', { id: 'chapter-cache-limit', type: 'number', min: 0, max: 100000, step: 1, required: true, value: 100, 'aria-label': 'Số chương giữ trong cache toàn thư viện' });
  const panel = el('section', { class: 'settings-section', id: 'chapter-cache-settings' }, [el('h2', { text: 'Bộ nhớ đệm nội dung chương' }),
    el('p', { class: 'muted small', text: 'Một giới hạn chung cho toàn bộ truyện chữ và truyện tranh: mặc định giữ 100 chương có thể tải lại được truy cập gần nhất. Nội dung tải ngoại tuyến chủ động, tệp nhập, bản chỉnh sửa và bản gốc để khôi phục được giữ riêng. Chương đang đọc không bị dọn; phiên đọc mất kết nối quá 5 phút có thể hết bảo vệ. Mở lại chương đã dọn cần mạng và có thể cần CAPTCHA hoặc dịch lại trên STV. Giới hạn cache ảnh được tính riêng.' }),
    summary, el('label', { class: 'field', text: 'Số chương cache toàn thư viện · 0 = chỉ giữ chương đang dùng' }, [limit]),
    el('div', { class: 'header-actions' }, [30, 50, 100].map(value => el('button', { class: 'button', text: `${value} chương`, onclick: () => { limit.value = value; } }))), status, error]);
  function draw(data) {
    limit.value = data.max_chapters;
    summary.replaceChildren();
    for (const [key, label] of [['cache', 'Có thể tải lại'], ['protected', 'Được giữ ngoại tuyến / nhập / chỉnh sửa'], ['legacy', 'Nội dung cũ chưa xác định kiểu tải']]) {
      summary.append(el('dt', { text: label }), el('dd', { id: 'chapter-cache-' + key, text: `${data[key + '_count'].toLocaleString('vi-VN')} chương · khoảng ${formatBytes(data[key + '_bytes'])}` }));
    }
    summary.append(el('dt', { text: 'Tệp SQLite / WAL' }), el('dd', { text: `${formatBytes(data.database_bytes)} / ${formatBytes(data.wal_bytes)}` }),
      el('dt', { text: 'Trang trống SQLite có thể tái sử dụng' }), el('dd', { text: `${formatBytes(data.reusable_bytes)} · ${data.freelist_count.toLocaleString('vi-VN')} trang` }));
    status.textContent = `${!data.complete ? 'Đang kiểm kê nội dung cũ theo lô; thống kê chưa đầy đủ. Nội dung chưa kiểm kê vẫn an toàn. ' : ''}${data.clear_until || data.legacy_until || data.cache_count > data.max_chapters ? 'Đang dọn/phân loại nền; chương đang dùng sẽ được kiểm tra lại sau. ' : ''}Dung lượng trên là ước tính nội dung, chưa cộng chỉ mục FTS. Xóa nội dung giải phóng chỗ cho SQLite tái sử dụng; tệp thường không nhỏ đi. Auto-vacuum: ${['NONE', 'FULL', 'INCREMENTAL'][data.auto_vacuum]}.`;
    panel.querySelector('#chapter-cache-legacy-action').disabled = !data.complete || !data.legacy_count;
  }
  async function action(path, body) {
    panel.inert = true; error.hidden = true;
    try { draw(await api(path, body)); }
    catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { panel.inert = false; }
  }
  panel.append(el('div', { class: 'note-actions' }, [
    el('button', { class: 'button', id: 'chapter-cache-save', text: 'Lưu giới hạn chương', onclick: () => { if (limit.reportValidity()) action('/api/cache/chapters', { max_chapters: Number(limit.value) }); } }),
    el('button', { class: 'button', id: 'chapter-cache-refresh', text: 'Cập nhật cache chương', onclick: () => action('/api/cache/chapters') }),
    el('button', { class: 'button', id: 'chapter-cache-clear', text: 'Dọn nội dung có thể tải lại', onclick: () => {
      if (confirm('Dọn cache chương có thể tải lại? Giữ nguyên tên, mục lục, tiến độ và ghi chú. Nội dung ngoại tuyến, tệp nhập, bản sửa và nội dung cũ chưa phân loại được giữ.')) action('/api/cache/chapters/clear', {});
    } }),
    el('button', { class: 'button', id: 'chapter-cache-legacy-action', text: 'Cho phép dọn nội dung cũ chưa phân loại…', onclick: () => {
      if (confirm('CẢNH BÁO: không biết các chương cũ này đã được tải ngoại tuyến hay chỉ đọc thông thường. Đây có thể là bản ngoại tuyến duy nhất; website có thể đã mất nội dung. Phân loại lại sẽ cho phép LRU xóa chúng, và mở lại cần mạng/CAPTCHA/dịch. Tệp nhập, bản sửa và nội dung đã xác nhận ngoại tuyến vẫn an toàn. Bạn có chấp nhận rủi ro này?')) action('/api/cache/chapters/legacy', { confirmed: true });
    } }),
    el('button', { class: 'button', id: 'chapter-cache-compact', text: 'Thu gọn tệp SQLite…', onclick: () => {
      if (confirm('Đóng mọi trình đọc và tạm dừng các tác vụ tải trước. Thu gọn có thể mất nhiều thời gian, tạm khóa thao tác thư viện và cần khoảng hai lần dung lượng SQLite cộng 128 MiB trống. Giữ máy chủ chạy đến khi hoàn tất. Chỉ thực hiện khi cần thu nhỏ tệp; không bắt buộc để SQLite tái sử dụng chỗ trống. Tiếp tục?')) action('/api/cache/chapters/compact', { confirmed: true });
    } })]));
  if (initial) draw(initial);
  return panel;
}

function imageCachePanel(initial, problem = '') {
  const summary = el('dl', { class: 'report' });
  const status = el('p', { class: 'muted small', role: 'status' });
  const error = el('p', { class: 'inline-error', role: 'alert', hidden: !problem, text: problem });
  const limit = el('input', { id: 'image-cache-limit', type: 'number', min: 0, max: 65536, step: 1, required: true, 'aria-label': 'Giới hạn cache ảnh (MiB)' });
  const panel = el('section', { class: 'settings-section', id: 'image-cache-settings' }, [el('h2', { text: 'Bộ nhớ đệm ảnh' }),
    el('p', { class: 'muted small', text: 'Giới hạn áp dụng cho ảnh có thể tải lại; ảnh tải ngoại tuyến chủ động và ảnh trong file nhập được giữ riêng. Ảnh cũ chưa xác định được kiểu tải cũng được giữ an toàn. Dọn cache có thể làm ảnh đã xem cần mạng để tải lại.' }), summary,
    el('label', { class: 'field', text: 'Giới hạn cache ảnh (MiB) · 0 = không lưu ảnh tạm mới' }, [limit]), status, error]);
  function draw(data) {
    limit.value = data.max_bytes / 1024 ** 2;
    summary.replaceChildren();
    for (const [key, label] of [['total_bytes', 'Tổng ảnh đã theo dõi'], ['cache_bytes', 'Cache có thể dọn'], ['protected_bytes', 'Ảnh được giữ an toàn'], ['legacy_bytes', 'Trong đó: ảnh cũ chưa phân loại']]) {
      summary.append(el('dt', { text: label }), el('dd', { id: 'image-cache-' + key, text: formatBytes(data[key]) }));
    }
    status.textContent = `${data.entries.toLocaleString('vi-VN')} ảnh đã theo dõi. ${!data.inventory_complete ? 'Đang kiểm kê ảnh cũ; thống kê chưa đầy đủ. ' : ''}${data.clearing || data.pending_bytes || data.cache_bytes > data.max_bytes ? 'Đang dọn nền; ảnh đang dùng hoặc tệp chưa xóa được sẽ thử lại. Bấm cập nhật để xem kết quả.' : ''}`;
  }
  async function action(path, body) {
    panel.inert = true; error.hidden = true;
    try { draw(await api(path, body)); }
    catch (problem) { error.textContent = problem.message; error.hidden = false; }
    finally { panel.inert = false; }
  }
  panel.append(el('div', { class: 'note-actions' }, [
    el('button', { class: 'button', id: 'image-cache-save', text: 'Lưu giới hạn', onclick: () => { if (limit.reportValidity()) action('/api/cache/images', { max_bytes: Number(limit.value) * 1024 ** 2 }); } }),
    el('button', { class: 'button', id: 'image-cache-refresh', text: 'Cập nhật dung lượng', onclick: () => action('/api/cache/images') }),
    el('button', { class: 'button', id: 'image-cache-clear', text: 'Dọn cache có thể tải lại', onclick: () => action('/api/cache/images/clear', {}) })]));
  if (initial) draw(initial);
  return panel;
}

function shutdownPanel() {
  const status = el('p', { class: 'muted small', role: 'status', text: 'Tắt chủ động khi không dùng ứng dụng. Đóng tab không tự tắt máy chủ; tác vụ nghe nền và tải vẫn được giữ.' });
  const control = el('button', { class: 'button', id: 'server-shutdown', text: 'Tắt máy chủ', onclick: async () => {
    control.disabled = true;
    const send = pause => api('/api/shutdown', { pause_downloads: pause }, { headers: { 'X-Shutdown-Token': shutdownToken }, captcha: false });
    // Opened before the shutdown request: the server answers it only once shutting down has finished.
    const waiting = new AbortController();
    let finished = null;
    try {
      if (!shutdownToken) { await api('/api/settings'); if (!shutdownToken) throw Error('Hãy tải lại giao diện để nhận phiên máy chủ hiện tại.'); }
      finished = fetch('/api/shutdown/wait', { headers: { 'X-Shutdown-Token': shutdownToken }, cache: 'no-store', signal: waiting.signal })
        .then((response) => (response.ok ? response.json() : null));
      finished.catch(() => {});
      try { await send(false); }
      catch (error) {
        if (error.code !== 'active_downloads') throw error;
        if (!confirm(`Có ${error.active_downloads} tác vụ tải. Tạm dừng các tác vụ tải đang chạy và tắt máy chủ?`)) { waiting.abort(); control.disabled = false; return; }
        await send(true);
      }
    } catch (error) { waiting.abort(); status.textContent = error.message; notify(error.message, true); control.disabled = false; return; }
    status.textContent = 'Đang lưu dữ liệu và tắt máy chủ…';
    const slow = setTimeout(() => { status.textContent = 'Máy chủ vẫn đang hoàn tất… Nếu quá lâu, xem reader.log.'; }, 60000);
    const result = await finished.catch(() => null);
    clearTimeout(slow);
    if (result?.finished && result.checkpoint === 'complete') {
      status.textContent = 'Máy chủ đã tắt an toàn. Đang đóng cửa sổ…';
      window.close();
      setTimeout(() => { status.textContent = 'Máy chủ đã tắt an toàn. Bạn có thể đóng cửa sổ này.'; }, 300);
    } else if (result?.finished) {
      status.textContent = 'Máy chủ đã tắt an toàn và mọi thay đổi đã được lưu. Riêng bước gộp nhật ký ghi của SQLite chưa xong; việc này không làm mất dữ liệu và sẽ tự hoàn tất ở lần mở sau.';
      control.replaceWith(el('button', { class: 'button', text: 'Đóng cửa sổ', onclick: () => window.close() }));
    } else {
      status.textContent = 'Máy chủ đã dừng nhưng chưa xác nhận đã tắt xong. Hãy xem reader.log trước khi mở lại.';
    }
  } });
  return el('section', { class: 'settings-section', id: 'server-settings' }, [el('h2', { text: 'Máy chủ cục bộ' }), status, control]);
}

async function renderSettings() {
  const target = $('settings-page'); target.replaceChildren(el('p', { text: 'Đang kiểm tra công cụ…' }));
  const results = await Promise.allSettled([api('/api/capabilities'), api('/api/cache/images'), api('/api/cache/chapters')]);
  const capabilities = el('section', { class: 'settings-section' }, [el('h2', { text: 'Khả năng trên máy này' }), el('p', { class: 'muted', text: 'Chỉ công cụ phát hiện được mới được ghi nhận sẵn sàng. Giọng đọc được kiểm tra trong trình đọc.' })]);
  if (results[0].status === 'fulfilled') {
    const data = results[0].value;
    const rows = [
      ['File có thể nhập', (data.documents?.supported_extensions || []).join(', ') || 'Chưa lấy được danh sách'],
      ['Bố cục tài liệu', data.documents?.layout || 'Tùy định dạng file'],
      ['PDF', data.documents?.pdf?.supported ? 'Đã có công cụ đọc PDF' : data.documents?.pdf?.reason || 'Xem trạng thái OCR bên dưới'],
      ['OCR PDF', data.ocr?.supported || data.ocr?.available ? `Đã phát hiện công cụ OCR${data.ocr.languages?.length ? ` · Ngôn ngữ: ${data.ocr.languages.join(', ')}` : ''}. Cần kiểm tra kết quả nhận dạng trước khi nghe.` : data.ocr?.message || data.ocr?.reason || data.documents?.ocr?.reason || 'Chưa phát hiện công cụ OCR'],
      ['QT / VietPhrase', data.qt?.installed ? `${(data.qt.counts || []).reduce((count, entry) => count + Number(entry.count || 0), 0).toLocaleString('vi-VN')} mục từ đã nhập` : 'Chưa có từ điển. Nhập VietPhrase, Names hoặc Hán Việt để dùng offline.'],
      ['Giọng đọc', 'Mở truyện → Nghe truyện để kiểm tra giọng offline và online đang có trong trình duyệt.'],
      ['Xuất audio', data.tts?.audio_export ? 'Có công cụ hỗ trợ' : 'Web Speech chưa hỗ trợ xuất audio thành file.'],
      ['Nơi lưu dữ liệu', data.data_directory || 'reader-local/user-data'],
    ];
    const summary = el('dl', { class: 'report' });
    for (const [label, value] of rows) summary.append(el('dt', { text: label }), el('dd', { text: value }));
    capabilities.append(summary, el('details', {}, [el('summary', { text: 'Chi tiết công cụ phát hiện được' }), reportNode(data)]));
  } else capabilities.append(el('p', { class: 'inline-error', text: results[0].reason.message }));
  target.replaceChildren(capabilities, chapterCachePanel(results[2].status === 'fulfilled' ? results[2].value : null, results[2].reason?.message), imageCachePanel(results[1].status === 'fulfilled' ? results[1].value : null, results[1].reason?.message), el('section', { class: 'settings-section' }, [el('h2', { text: 'Dữ liệu thư viện' }), el('p', { class: 'muted', text: 'Thư viện, nội dung và cài đặt mới nằm riêng trong reader-local/user-data. Nhập backup giữ nguyên bộ xuất gốc. Đóng ứng dụng trước khi sao chép thư mục dữ liệu để lưu dự phòng.' }), el('div', { class: 'header-actions' }, [el('button', { class: 'button', text: 'Nhập thư viện hiện có', onclick: importBackup }), el('button', { class: 'button', text: 'Tất cả ghi chú đã lưu', onclick: globalNotesDialog })])]));
  target.append(shutdownPanel());
}

async function globalNotesDialog() {
  const query = el('input', { type: 'search', placeholder: 'Tìm ghi chú, đoạn trích hoặc mã truyện…', 'aria-label': 'Tìm tất cả ghi chú' });
  const list = el('div', {}, [el('p', { text: 'Đang lấy ghi chú…' })]);
  const dialog = showDialog('Tất cả ghi chú và dấu trang', [el('p', { class: 'muted small', text: 'Bao gồm bản ghi chưa ghép được với sách khi nhập backup. Những ghi chú này vẫn được giữ nguyên và có thể xuất ra file.' }), query, list]);
  try {
    const notes = await api('/api/notes');
    function render() {
      const found = notes.filter((note) => `${note.quote} ${note.note} ${note.book_id}`.toLocaleLowerCase('vi').includes(query.value.toLocaleLowerCase('vi')));
      list.replaceChildren(...found.map((note) => el('article', { class: 'note-item' }, [el('p', { class: 'note-location', text: `${note.type === 'bookmark' ? 'Dấu trang' : 'Ghi chú'} · Mã truyện ${note.book_id} · Chương ${Number(note.chapter_index) + 1}` }), note.quote ? el('blockquote', { class: 'note-quote', text: note.quote }) : null, el('p', { class: 'note-content', text: note.note || '' }), el('button', { class: 'text-button', text: 'Mở vị trí trong truyện', onclick: async () => {
        try { const book = await api(`/api/books/${encodeURIComponent(note.book_id)}`); dialog.close(); reader.open(book, note.chapter_index, note.position); }
        catch (error) { notify(`Chưa mở được sách của ghi chú này: ${error.message} Bản ghi vẫn được giữ trong thư viện.`, true); }
      } })])));
      if (!found.length) list.append(el('p', { class: 'muted', text: 'Không có ghi chú phù hợp.' }));
    }
    query.oninput = render; render();
    dialog.actions.append(el('button', { class: 'button', text: `Xuất ${notes.length} ghi chú`, onclick: () => {
      const url = URL.createObjectURL(new Blob([JSON.stringify(notes, null, 2)], { type: 'application/json;charset=utf-8' }));
      el('a', { href: url, download: 'ke-truyen-ghi-chu.json' }).click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
    } }));
  } catch (error) { list.replaceChildren(el('p', { class: 'inline-error', text: error.message })); }
}

async function initialize() {
  const results = await Promise.allSettled([api('/api/settings'), api('/api/sources')]);
  if (results[0].status === 'fulfilled') state.settings = results[0].value || {};
  else notify(results[0].reason.message, true);
  if (results[1].status === 'fulfilled') {
    state.sources = Array.isArray(results[1].value) ? results[1].value : results[1].value.sources || [];
    $('library-source').append(...state.sources.map((source) => el('option', { value: source.id, text: source.name })));
    $('source-select').replaceChildren(...state.sources.map((source) => el('option', { value: source.id, text: source.name })));
  } else { $('source-status').textContent = results[1].reason.message; $('source-select').replaceChildren(el('option', { value: '', text: 'Chưa lấy được nguồn' })); }
  state.view = ['grid', 'compact', 'list'].includes(state.settings.library?.view) ? state.settings.library.view : 'grid';
  $('library-sort').value = state.settings.library?.sort || 'recent';
  document.querySelectorAll('[data-view]').forEach((button) => { button.classList.toggle('active', button.dataset.view === state.view); button.setAttribute('aria-pressed', String(button.dataset.view === state.view)); });
  await Promise.all([loadLibrary(), loadGroups()]);
  const locationUrl = new URL(location.href);
  const requestedBook = locationUrl.searchParams.get('book');
  if (requestedBook !== null) {
    locationUrl.searchParams.delete('book');
    history.replaceState(null, '', locationUrl.pathname + locationUrl.search + locationUrl.hash);
    try {
      if (!requestedBook || requestedBook.length > 256 || /[\x00-\x1f]/.test(requestedBook)) throw new Error('Mã truyện không hợp lệ.');
      const selected = await api(`/api/books/${encodeURIComponent(requestedBook)}`);
      if (selected.trashed_at) throw new Error('Truyện đang ở Thùng rác. Hãy khôi phục truyện để đọc tiếp.');
      await reader.open(selected);
    } catch (error) { notify(`Không mở được truyện từ lối tắt: ${error.message}`, true); }
  }
}
initialize();
