// HTTP transport from the verified .com probe. No browser, profile or persistent cookies.
import https from 'node:https';
import zlib from 'node:zlib';
import readline from 'node:readline';
import {fileURLToPath} from 'node:url';

const ORIGIN = 'https://sangtacviet.com', LIMIT = 8 * 1024 * 1024;
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36';
function failure(code, message, http_status) { return Object.assign(new Error(message), {code, http_status}); }
async function limited(response, limit) {
  const chunks = []; let size = 0;
  for await (const chunk of response.body) {
    size += chunk.length;
    if (size > limit) throw failure('parse_error', 'Nội dung nguồn vượt giới hạn an toàn.');
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}
function freshRequest(url, options) {
  return new Promise((resolve, reject) => {
    const req = https.request(url, {method: options.method, headers: {...options.headers, Connection: 'close'},
      agent: false, family: 4}, res => {
      const chunks = []; let size = 0;
      res.on('data', chunk => { size += chunk.length; if (size > LIMIT) req.destroy(failure('parse_error', 'Phản hồi xác minh quá lớn.')); else chunks.push(chunk); });
      res.on('error', reject);
      res.on('end', () => {
        try {
          let body = Buffer.concat(chunks);
          const encoding = res.headers['content-encoding'], settings = {maxOutputLength: LIMIT};
          if (encoding === 'gzip') body = zlib.gunzipSync(body, settings);
          else if (encoding === 'deflate') body = zlib.inflateSync(body, settings);
          else if (encoding === 'br') body = zlib.brotliDecompressSync(body, settings);
          else if (encoding && encoding !== 'identity') throw failure('parse_error', 'Kiểu nén xác minh chưa hỗ trợ.');
          const headers = new Headers();
          for (const [key, value] of Object.entries(res.headers))
            for (const entry of Array.isArray(value) ? value : [value]) if (entry !== undefined) headers.append(key, entry);
          resolve(new Response(body, {status: res.statusCode, headers}));
        } catch (error) { reject(error); }
      });
    });
    const timer = setTimeout(() => req.destroy(failure('source_timeout', 'Xác minh phản hồi quá lâu; chưa gửi lại đáp án.')), 25000);
    req.on('close', () => clearTimeout(timer));
    req.on('error', reject);
    if (options.body) req.write(options.body);
    req.end();
  });
}

export class Session {
  constructor(reading = false, send = fetch, fresh = freshRequest) {
    this.jar = new Map(); this.lastRead = 0; this.reading = reading; this.send = send; this.fresh = fresh;
    if (reading) this.preferences();
  }
  preferences() {
    for (const [name, value] of [['lang', 'vi'], ['transmode', 'name'], ['foreignlang', 'vi']])
      this.jar.set(name + ';/', {name, value, path: '/'});
  }
  put(cookie, literal = false) {
    const [pair, ...attrs] = cookie.split(';').map(s => s.trim()), eq = pair.indexOf('=');
    if (eq <= 0) return;
    const name = pair.slice(0, eq), value = pair.slice(eq + 1);
    if (!/^[A-Za-z0-9_-]+$/.test(name) || /[\r\n;]/.test(value)) return;
    if (literal && (!['_ac', '_acx', '_gac'].includes(name) || !/^[A-Za-z0-9+/=_-]+$/.test(value))) return;
    const domain = attrs.find(a => /^domain=/i.test(a))?.slice(7).replace(/^\./, '').toLowerCase();
    if (domain && domain !== 'sangtacviet.com') return;
    const path = attrs.find(a => /^path=/i.test(a))?.slice(5) || '/';
    if (!path.startsWith('/')) return;
    const maxAge = attrs.find(a => /^max-age=/i.test(a))?.slice(8);
    const expires = maxAge !== undefined ? Date.now() + Number(maxAge) * 1000 :
      Date.parse(attrs.find(a => /^expires=/i.test(a))?.slice(8) || '');
    if (Number.isFinite(expires) && expires <= Date.now()) this.jar.delete(name + ';' + path);
    else this.jar.set(name + ';' + path, {name, value, path, expires});
  }
  cookieHeader(path) {
    return [...this.jar.values()].filter(c => (!Number.isFinite(c.expires) || c.expires > Date.now()) &&
      (path === c.path || (path.startsWith(c.path) && (c.path.endsWith('/') || path[c.path.length] === '/'))))
      .sort((a, b) => b.path.length - a.path.length).map(c => c.name + '=' + c.value).join('; ');
  }
  async request(path, {method = 'GET', body, referer = ORIGIN + '/', navigation = false, xhr = false,
    seed = false, image = false, fresh_connection = false} = {}) {
    const url = new URL(path, ORIGIN), ref = new URL(referer);
    if (url.origin !== ORIGIN || url.username || url.password || url.hash || ref.origin !== ORIGIN ||
        !['GET', 'POST'].includes(method) ||
        !(/^\/truyen\/[a-zA-Z0-9_-]+\/1\/[0-9]+\/(?:[0-9]+\/)?$/.test(url.pathname) ||
          ['/index.php', '/search/', '/io/searchtp/searchBooks', '/generate_captcha.php'].includes(url.pathname)))
      throw failure('source_error', 'Yêu cầu ngoài nguồn Sáng Tác Việt bị từ chối.');
    if (url.searchParams.get('sajax') === 'readchapter') {
      // Keep the measured probe's spacing; reduce only after a separate rate-limit check.
      await new Promise(resolve => setTimeout(resolve, Math.max(0, 10000 - (Date.now() - this.lastRead))));
      this.lastRead = Date.now();
    }
    const headers = image ? {'User-Agent': UA, Referer: referer} : {
      'User-Agent': UA, 'Accept-Language': 'en-US,en;q=0.9', 'Accept-Encoding': 'gzip, deflate, br', Referer: referer,
      Accept: navigation ? 'text/html,application/xhtml+xml,application/xml;q=0.9,image/jxl,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7' : '*/*',
      'Sec-Fetch-Dest': navigation ? 'document' : 'empty', 'Sec-Fetch-Mode': navigation ? 'navigate' : 'cors',
      'Sec-Fetch-Site': navigation ? 'none' : 'same-origin',
      'sec-ch-ua': '"Not:A-Brand";v="8", "Chromium";v="156", "Google Chrome";v="156"',
      'sec-ch-ua-mobile': '?0', 'sec-ch-ua-platform': '"Windows"'
    };
    if (navigation) { headers['Sec-Fetch-User'] = '?1'; headers['Upgrade-Insecure-Requests'] = '1'; }
    if (xhr) headers['X-Requested-With'] = 'XmlHttpRequest';
    const cookies = this.cookieHeader(url.pathname);
    if (cookies) headers.Cookie = cookies;
    if (method === 'POST') {
      headers['Content-Type'] = 'application/x-www-form-urlencoded'; headers.Origin = ORIGIN;
      if (seed) {
        body = this.jar.get('_ac;/')?.value;
        if (!body) throw failure('parse_error', 'Trang chương chưa cấp cookie khởi tạo _ac.');
      }
      body ??= '';
      if (fresh_connection) { headers.Connection = 'close'; headers['Content-Length'] = String(Buffer.byteLength(body)); }
    }
    const options = {method, headers, ...(method === 'POST' ? {body} : {}), redirect: 'manual', signal: AbortSignal.timeout(18000)};
    const response = await (fresh_connection ? this.fresh(url, options) : this.send(url, options));
    if (!response.ok) {
      await response.body?.cancel();
      throw failure('http_error', 'Sáng Tác Việt trả HTTP ' + response.status + (response.status >= 300 && response.status < 400 ? '; không tự chuyển hướng.' : '.'), response.status);
    }
    const bytes = await limited(response, image ? 1024 * 1024 : LIMIT);
    let text = '';
    try { if (!image) text = new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
    catch { throw failure('parse_error', 'Nội dung Sáng Tác Việt không phải UTF-8 hợp lệ.'); }
    // Match the measured ordering: literal assignments first, HTTP Set-Cookie last.
    if (!image) for (const match of text.matchAll(/document\.cookie\s*=\s*(["'])([^\r\n]*?)\1/g)) this.put(match[2], true);
    for (const cookie of response.headers.getSetCookie()) this.put(cookie);
    if (this.reading && navigation) this.preferences();
    if (image) {
      const content_type = (response.headers.get('content-type') || '').split(';')[0].toLowerCase();
      if (!/^image\/(?:png|jpeg|gif|webp)$/.test(content_type)) throw failure('parse_error', 'Website chưa trả ảnh CAPTCHA hợp lệ.');
      return {body_base64: bytes.toString('base64'), content_type};
    }
    return {text, request_url: url.href, request_method: method};
  }
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const sessions = {metadata: new Session(), reading: new Session(true)};
  for await (const line of readline.createInterface({input: process.stdin, crlfDelay: Infinity})) {
    let result;
    try {
      if (Buffer.byteLength(line) > 2 * 1024 * 1024) throw failure('parse_error', 'Yêu cầu HTTP quá lớn.');
      const {path, session = 'metadata', ...options} = JSON.parse(line);
      if (!Object.hasOwn(sessions, session)) throw failure('source_error', 'Phiên HTTP không hợp lệ.');
      result = await sessions[session].request(path, options);
    } catch (error) {
      const timeout = ['TimeoutError', 'AbortError'].includes(error.name);
      result = {error: error.code && !String(error.code).startsWith('UND_') ? error.message :
        timeout ? 'Sáng Tác Việt phản hồi quá lâu.' : 'Không kết nối được Sáng Tác Việt. Hãy kiểm tra mạng rồi thử lại.',
        code: error.code && ['source_error', 'parse_error', 'http_error', 'source_timeout'].includes(error.code) ? error.code : timeout ? 'source_timeout' : 'network_error',
        http_status: error.http_status};
    }
    process.stdout.write(JSON.stringify(result) + '\n');
  }
}
