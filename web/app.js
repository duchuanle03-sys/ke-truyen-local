import { createReader } from './reader.js';
import { createCaptchaDialog } from './captcha.js';

const $ = (id) => document.getElementById(id);
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

const state = { books: [], groups: [], sources: [], settings: {}, kind: 'all', view: 'grid', reverse: false, sourcePage: 1, activeView: 'library' };
let settingTimer;
function saveSettings(patch) {
  Object.assign(state.settings, patch);
  clearTimeout(settingTimer);
  settingTimer = setTimeout(async () => {
    try { await api('/api/settings', state.settings); }
    catch (error) { notify(`Chưa lưu được thiết lập: ${error.message}`, true); }
  }, 350);
}
const reader = createReader({ api, el, notify, showDialog, getSettings: () => state.settings.reader || {}, saveSettings: (value) => saveSettings({ reader: value }), openDictionary: (book) => openDictionary(book), onClose: () => loadLibrary() });

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

function card(book, fromSource = false) {
  const button = el('button', { class: 'book-card', onclick: () => bookDetail(book, fromSource) });
  const info = el('div', { class: 'book-info' }, [el('h3', { text: book.name || 'Chưa có tên' }), el('p', { class: 'book-author', text: book.author || 'Chưa rõ tác giả' }), el('p', { class: 'book-meta', text: `${sourceName(book.source || $('source-select').value)}${book.chapter_count ? ` · ${Number(book.chapter_count).toLocaleString('vi-VN')} chương` : ''}` })]);
  if (!fromSource) info.append(el('div', { class: 'book-progress' }, [el('progress', { max: 100, value: percent(book), 'aria-label': `Đã đọc ${percent(book)}%` }), el('span', { text: `${percent(book)}%` })]));
  button.append(cover(book), info);
  return button;
}

function renderLibrary() {
  let books = state.books.filter((book) => state.kind === 'all' || (state.kind === 'file' ? ['local', 'file'].includes(book.source) : book.kind === state.kind));
  if (state.reverse) books = books.toReversed();
  $('book-grid').className = `book-grid ${state.view}`;
  $('book-grid').replaceChildren(...books.map((book) => card(book)));
  $('library-count').textContent = `${books.length.toLocaleString('vi-VN')} truyện${$('library-search').value ? ' phù hợp' : ''}`;
  if (!books.length) $('book-grid').append(el('div', { class: 'empty-state' }, [el('span', { class: 'empty-icon', 'aria-hidden': 'true', text: '▥' }), el('h2', { text: state.books.length ? 'Không có truyện trong bộ lọc này' : 'Kệ sách đang chờ bạn' }), el('p', { text: $('library-search').value || $('library-source').value || $('library-group').value ? 'Thử đổi từ khóa hoặc bỏ bớt bộ lọc.' : 'Nhập thư viện điện thoại một lần, hoặc tìm truyện từ QQ, Full, Hako.' }), el('button', { class: 'button primary', text: 'Nhập thư viện hiện có', onclick: importBackup })]));
}

let libraryRequest = 0;
async function loadLibrary() {
  const request = ++libraryRequest;
  $('book-grid').setAttribute('aria-busy', 'true');
  $('library-error').hidden = true;
  const params = new URLSearchParams({ q: $('library-search').value, source: $('library-source').value, group: $('library-group').value, sort: $('library-sort').value });
  try {
    const books = await api(`/api/books?${params}`);
    if (request !== libraryRequest) return;
    state.books = Array.isArray(books) ? books : books.items || [];
    renderLibrary();
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

async function navigate(view) {
  state.activeView = view;
  for (const section of document.querySelectorAll('.view')) section.hidden = section.id !== `${view}-view`;
  for (const button of document.querySelectorAll('[data-nav]')) {
    button.classList.toggle('active', button.dataset.nav === view);
    if (button.dataset.nav === view) button.setAttribute('aria-current', 'page'); else button.removeAttribute('aria-current');
  }
  if (view === 'library') await loadLibrary();
  if (view === 'dictionary') renderDictionary($('dictionary-page'));
  if (view === 'settings') renderSettings();
}
document.querySelectorAll('[data-nav]').forEach((button) => { button.onclick = () => navigate(button.dataset.nav); });
document.querySelector('.brand').onclick = (event) => { event.preventDefault(); navigate('library'); };
let searchTimer;
$('library-search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadLibrary, 220); });
for (const id of ['library-source', 'library-group', 'library-sort']) $(id).onchange = () => { saveSettings({ library: { ...state.settings.library, sort: $('library-sort').value } }); loadLibrary(); };
$('refresh-library').onclick = loadLibrary;
$('sort-direction').onclick = () => { state.reverse = !state.reverse; $('sort-direction').setAttribute('aria-pressed', String(state.reverse)); renderLibrary(); };
document.querySelectorAll('[data-kind]').forEach((button) => {
  button.onclick = () => {
    state.kind = button.dataset.kind;
    for (const other of document.querySelectorAll('[data-kind]')) { other.classList.toggle('active', other === button); other.setAttribute('aria-pressed', String(other === button)); }
    renderLibrary();
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
    $('nav-count').textContent = state.books.length;
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
    dialog.actions.replaceChildren(el('button', { class: 'button primary', text: 'Thêm vào kệ và đọc', onclick: async (event) => {
      event.currentTarget.disabled = true; status.hidden = true;
      try { const added = await api(`/api/sources/${encodeURIComponent(selectedSource)}/add`, { url: book.path || book.url || initial.path || initial.url }); dialog.close(); await loadLibrary(); reader.open(added.book || added); }
      catch (error) { status.textContent = error.message; status.hidden = false; event.target.disabled = false; }
    } }));
  } else {
    const downloadCount = el('input', { type: 'number', value: Math.min(20, book.chapter_count || 20), min: 1, max: Math.max(1, book.chapter_count || 100), 'aria-label': 'Số chương tải từ chương đang đọc' });
    const download = el('div', { class: 'download-row' }, [el('label', { text: 'Tải từ chương đang đọc' }, [downloadCount]), el('button', { class: 'button', text: 'Tải để đọc offline', onclick: async (event) => {
      if (!downloadCount.checkValidity()) { downloadCount.reportValidity(); return; }
      event.currentTarget.disabled = true;
      try { const job = await api(`/api/books/${encodeURIComponent(book.id)}/download`, { start: book.chapter_index || 0, count: Number(downloadCount.value) }); status.className = 'inline-info'; status.textContent = `Đã tạo yêu cầu tải ${job.count || downloadCount.value} chương. Xem kết quả trong Thiết lập → Tải nội dung.`; status.hidden = false; }
      catch (error) { status.className = 'inline-error'; status.textContent = error.message; status.hidden = false; }
      finally { event.target.disabled = false; }
    } })]);
    dialog.body.append(download);
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

async function refreshShelf() {
  await loadLibrary();
  try { $('nav-count').textContent = (await api('/api/books')).length || ''; }
  catch { $('nav-count').textContent = ''; }
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

async function openTrash() {
  const list = el('div', {}, [el('p', { text: 'Đang mở thùng rác…' })]);
  const dialog = showDialog('Thùng rác · giữ 30 ngày', [el('p', { class: 'muted', text: 'Có thể khôi phục trước ngày hết hạn hoặc xóa vĩnh viễn ngay. Truyện quá 30 ngày tự được dọn khi ứng dụng đang chạy hoặc vào lần mở tiếp theo.' }), list]);
  const token = showDialog.token;
  dialog.actions.append(el('button', { class: 'button', text: 'Làm mới', onclick: openTrash }), el('button', { class: 'button', text: 'Đóng', onclick: dialog.close }));
  try {
    const books = await api('/api/books?trashed=1');
    if (!$('dialog').open || showDialog.token !== token) return;
    list.replaceChildren();
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
          await refreshShelf(); if ($('dialog').open && showDialog.token === token) await openTrash(); notify('Đã khôi phục truyện cùng tiến độ và ghi chú.');
        } catch (problem) { row.inert = false; error.textContent = problem.message; error.hidden = false; }
      } }), el('button', { class: 'button danger', text: 'Xóa vĩnh viễn', onclick: () => confirmPurge(book) })]), error);
      list.append(row);
    }
  } catch (error) { if (showDialog.token === token) list.replaceChildren(el('p', { class: 'inline-error', role: 'alert', text: error.message })); }
}

function confirmPurge(book) {
  const error = el('p', { class: 'inline-error', role: 'alert', hidden: true });
  const dialog = showDialog('Xóa vĩnh viễn truyện?', [el('h3', { text: book.name }), el('p', { text: 'Truyện, tiến độ đọc, ghi chú/dấu trang và nội dung chương đã lưu trong app sẽ bị xóa. Sau đó không thể khôi phục từ Thùng rác.' }), el('p', { class: 'muted small', text: 'Bộ xuất điện thoại, tệp nhập gốc, từ điển và cache ảnh dùng chung được giữ.' }), error]);
  const token = showDialog.token;
  dialog.actions.append(el('button', { class: 'button', text: 'Hủy', onclick: openTrash }), el('button', { class: 'button danger', text: 'Xác nhận xóa vĩnh viễn', onclick: async (event) => {
    event.currentTarget.disabled = true; error.hidden = true;
    try {
      await api(`/api/books/${encodeURIComponent(book.id)}`, undefined, { method: 'DELETE' });
      if ($('dialog').open && showDialog.token === token) await openTrash(); notify('Đã xóa vĩnh viễn truyện khỏi thư viện ứng dụng.');
    } catch (problem) { error.textContent = problem.message; error.hidden = false; event.target.disabled = false; }
  } }));
}
$('open-trash').onclick = openTrash;

function editBook(book) {
  const name = el('input', { value: book.name || '', required: true });
  const author = el('input', { value: book.author || '' });
  const groups = el('div', { class: 'checkbox-list' }, state.groups.map((group) => el('label', {}, [el('input', { type: 'checkbox', value: group.id, checked: (book.groups || []).some((item) => String(typeof item === 'object' ? item.id : item) === String(group.id)) }), group.name])));
  const error = el('p', { class: 'inline-error', hidden: true });
  const dialog = showDialog('Sửa thông tin truyện', [el('label', { class: 'field', text: 'Tên truyện' }, [name]), el('label', { class: 'field', text: 'Tác giả' }, [author]), el('h3', { text: 'Nhóm hiện có' }), groups, error]);
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

async function renderSettings() {
  const target = $('settings-page'); target.replaceChildren(el('p', { text: 'Đang kiểm tra công cụ…' }));
  const results = await Promise.allSettled([api('/api/capabilities'), api('/api/jobs')]);
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
  const jobs = el('section', { class: 'settings-section' }, [el('h2', { text: 'Tải nội dung' }), el('button', { class: 'button', text: 'Cập nhật trạng thái', onclick: renderSettings })]);
  if (results[1].status === 'fulfilled') {
    const list = Array.isArray(results[1].value) ? results[1].value : results[1].value.jobs || [];
    if (!list.length) jobs.append(el('p', { class: 'muted', text: 'Chưa có yêu cầu tải. Mở chi tiết sách trên kệ để tải một khoảng chương.' }));
    const jobStatuses = { queued: 'Đang chờ', running: 'Đang tải', paused: 'Tạm dừng', verification_required: 'Chờ xác minh CAPTCHA', completed: 'Hoàn tất', failed: 'Lỗi', cancelled: 'Đã hủy' };
    for (const job of list) {
      const row = el('article', { class: 'download-job' }, [el('h3', { text: job.name || 'Tải chương' }), el('p', { class: 'muted small', text: `${jobStatuses[job.status] || job.status} · ${job.completed}/${job.total} chương` }), el('progress', { value: job.completed, max: job.total, 'aria-label': 'Tiến độ tải chương' })]);
      if (job.error) row.append(el('p', { class: 'inline-error', text: job.error }));
      const controls = el('div', { class: 'header-actions' });
      if (job.status === 'verification_required' && job.captcha) controls.append(el('button', { class: 'button primary', text: 'Xác minh CAPTCHA', onclick: async () => {
        try { await verifyCaptcha(job.captcha); await renderSettings(); }
        catch (error) { notify(error.message, true); await renderSettings(); }
      } }));
      for (const [action, text, enabled] of [['pause', 'Tạm dừng', job.status === 'running'], ['resume', 'Tiếp tục', job.status === 'paused'], ['retry', 'Thử lại phần còn lại', ['failed', 'cancelled'].includes(job.status)], ['cancel', 'Hủy tải', ['queued', 'running', 'paused', 'verification_required'].includes(job.status)], ['delete', 'Xóa khỏi danh sách', ['completed', 'failed', 'cancelled'].includes(job.status)]]) {
        if (enabled) controls.append(el('button', { class: 'button', text, onclick: async (event) => {
          event.currentTarget.disabled = true;
          try { await api(`/api/jobs/${encodeURIComponent(job.id)}/${action}`, {}); await renderSettings(); }
          catch (error) { notify(error.message, true); event.target.disabled = false; }
        } }));
      }
      row.append(controls); jobs.append(row);
    }
  } else jobs.append(el('p', { class: 'inline-error', text: results[1].reason.message }));
  target.replaceChildren(capabilities, jobs, el('section', { class: 'settings-section' }, [el('h2', { text: 'Dữ liệu thư viện' }), el('p', { class: 'muted', text: 'Thư viện, nội dung và cài đặt mới nằm riêng trong reader-local/user-data. Nhập backup giữ nguyên bộ xuất gốc. Đóng ứng dụng trước khi sao chép thư mục dữ liệu để lưu dự phòng.' }), el('div', { class: 'header-actions' }, [el('button', { class: 'button', text: 'Nhập thư viện hiện có', onclick: importBackup }), el('button', { class: 'button', text: 'Tất cả ghi chú đã lưu', onclick: globalNotesDialog })])]));
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
  $('nav-count').textContent = state.books.length || '';
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
