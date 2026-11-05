// One shared manual challenge dialog; all interrupted API callers await its result.
export function createCaptchaDialog({el, request}) {
  let active = null;
  return function verifyCaptcha(challenge) {
    if (active) return active;
    if (challenge?.source !== 'stv' || typeof challenge.id !== 'string' || !/^[a-f0-9]{32}$/.test(challenge.id))
      return Promise.reject(new Error('Yêu cầu xác minh từ nguồn không hợp lệ.'));
    const dialog = document.getElementById('captcha-dialog'), priorFocus = document.activeElement;
    let id = challenge.id, busy = false, loaded = false, finished = false, resolve, reject;
    active = new Promise((yes, no) => { resolve = yes; reject = no; });
    const promise = active;
    const error = el('p', {id: 'captcha-error', class: 'inline-error', role: 'alert', hidden: true});
    const status = el('p', {class: 'muted small', role: 'status'});
    const image = el('img', {id: 'captcha-image', alt: 'Ảnh CAPTCHA từ Sáng Tác Việt'});
    const input = el('input', {id: 'captcha-answer', type: 'text', required: true, minLength: 4, maxLength: 128,
      autocomplete: 'off', autocapitalize: 'off', spellcheck: false, 'aria-describedby': 'captcha-help'});
    const submit = el('button', {id: 'captcha-submit', type: 'submit', class: 'button primary', text: 'Gửi'});
    const refresh = el('button', {id: 'captcha-refresh', type: 'button', class: 'button', text: 'Làm mới CAPTCHA', onclick: () => loadImage(true)});
    const cancelButton = el('button', {id: 'captcha-cancel', type: 'button', class: 'button', text: 'Hủy', onclick: cancel});
    const output = el('output', {text: '200%'});
    const zoom = el('input', {id: 'captcha-zoom', type: 'range', min: 100, max: 400, step: 50, value: 200,
      oninput: () => { output.textContent = zoom.value + '%'; image.style.width = Math.max(160, image.naturalWidth) * Number(zoom.value) / 100 + 'px'; }});
    function sync() {
      input.disabled = refresh.disabled = busy;
      submit.disabled = busy || !loaded;
      dialog.setAttribute('aria-busy', String(busy));
    }
    function showError(problem) { error.textContent = problem.message || 'Chưa xác minh được. Hãy thử lại.'; error.hidden = false; }
    function finish(verified) {
      if (finished) return;
      finished = true; dialog.close(); active = null; input.value = '';
      if (priorFocus?.isConnected) priorFocus.focus({preventScroll: true});
      if (verified) resolve();
      else reject(Object.assign(new Error('Đã hủy xác minh; vị trí đọc vẫn được giữ.'), {code: 'captcha_cancelled'}));
    }
    function cancel() {
      const cancelledId = id;
      finish(false);
      request('/api/sources/stv/captcha/cancel', {id: cancelledId}).catch(() => {});
    }
    async function loadImage(renew = false) {
      if (busy || finished) return;
      busy = true; loaded = false; error.hidden = true; input.value = ''; status.textContent = 'Đang tải ảnh xác minh…'; sync();
      try {
        const result = await request(renew ? '/api/sources/stv/captcha/refresh' : '/api/sources/stv/captcha?id=' + encodeURIComponent(id),
          renew ? {id} : undefined);
        if (finished) return;
        if (typeof result.image !== 'string' || !/^data:image\/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$/.test(result.image))
          throw new Error('Nguồn chưa trả ảnh CAPTCHA hợp lệ. Hãy làm mới ảnh.');
        id = result.id; dialog.dataset.challengeId = id; image.src = result.image;
        status.textContent = '';
      } catch (problem) { if (!finished) { showError(problem); status.textContent = ''; } }
      finally { busy = false; sync(); if (!finished) input.focus(); }
    }
    image.onload = () => {
      loaded = true; image.style.width = Math.max(160, image.naturalWidth) * Number(zoom.value) / 100 + 'px'; sync();
    };
    image.onerror = () => { loaded = false; showError(new Error('Không hiển thị được ảnh. Hãy làm mới CAPTCHA.')); sync(); };
    const form = el('form', {id: 'captcha-form', onsubmit: async event => {
      event.preventDefault();
      if (busy || !loaded || finished || !form.reportValidity()) return;
      busy = true; error.hidden = true; status.textContent = 'Đang xác minh…'; sync();
      try {
        const result = await request('/api/sources/stv/captcha/verify', {id, answer: input.value.trim()});
        if (finished) return;
        if (result.verified !== true) throw new Error('Website chưa xác nhận thành công. Hãy thử lại.');
        finish(true);
      } catch (problem) { if (!finished) { showError(problem); status.textContent = ''; } }
      finally { busy = false; sync(); if (!finished) { input.focus(); input.select(); } }
    }}, [
      el('div', {class: 'dialog-header'}, [el('h2', {id: 'captcha-title', text: 'Xác minh Sáng Tác Việt'}),
        el('button', {type: 'button', class: 'icon-button', text: '×', 'aria-label': 'Hủy xác minh', onclick: cancel})]),
      el('div', {class: 'dialog-content'}, [
        el('p', {id: 'captcha-help', text: 'Website yêu cầu xác minh để tiếp tục. Bạn nhập mã trong ảnh; vị trí đọc hiện tại vẫn được giữ.'}),
        el('div', {class: 'captcha-viewport'}, [image]),
        el('label', {class: 'captcha-zoom'}, [el('span', {text: 'Phóng to ảnh'}), zoom, output]),
        el('label', {class: 'field', htmlFor: 'captcha-answer'}, [el('span', {text: 'Mã xác minh'}), input]), error, status]),
      el('div', {class: 'dialog-actions'}, [cancelButton, refresh, submit])
    ]);
    dialog.replaceChildren(form); dialog.dataset.challengeId = id;
    dialog.oncancel = event => { event.preventDefault(); cancel(); };
    dialog.onclose = () => { if (!finished && !dialog.open) cancel(); };
    dialog.showModal(); loadImage();
    return promise;
  };
}
