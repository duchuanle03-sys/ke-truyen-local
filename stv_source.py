"""Read STV's own rendered Vietnamese through an ordinary isolated browser session."""
import json
import re
from urllib.parse import urlencode, urlsplit

from htmltools import html_to_text, parse_html, safe_remote_url, sanitize_html
from source_errors import SourceError, SourceAccessError


ORIGIN = 'https://sangtacviet.vip'
_MAX_HTML = 8 * 1024 * 1024
_METADATA = 'h1#book_name2,meta[property="og:title"],meta[property="og:novel:book_name"],meta[property="og:novel:author"],meta[property="og:description"],meta[property="og:image"]'
_VIP = re.compile(r'\bvip\b', re.I)
_DENIED = re.compile(r'chưa\s+(?:mua|mở\s+khóa)|không\s+có\s+quyền|(?:cần|phải|vui\s+lòng|yêu\s+cầu)\s+(?:mua|mở\s+khóa)', re.I)
_LOGIN = re.compile(r'(?:vui\s+lòng|cần|hãy|phải)\s+đăng\s+nhập', re.I)
_PLACEHOLDER = re.compile(r'nhấp\s+vào\s+để\s+tải\s+chương|đang\s+tải\s+(?:nội\s+dung|chương)', re.I)


def _parts(url, chapter=False):
    parsed = urlsplit(url)
    match = re.fullmatch(r'/truyen/([a-zA-Z0-9_-]+)/1/([0-9]+)/(?:([0-9]+)/)?', parsed.path)
    if (parsed.scheme != 'https' or parsed.netloc != 'sangtacviet.vip' or parsed.query or parsed.fragment
            or not match or bool(match[3]) != chapter):
        raise SourceError('URL truyện hoặc chương Sáng Tác Việt không hợp lệ')
    return match.groups()


def _metadata_expression(toc_url=None):
    selected = 'Array.from(document.querySelectorAll(' + json.dumps(_METADATA) + ')).map(n=>n.outerHTML).join("")'
    if not toc_url:
        return '(()=>{if(document.readyState==="loading"||!document.querySelector("#book_name2"))return null;return {url:location.href,html:' + selected + '};})()'
    return '''(()=>{
      if(document.readyState==='loading'||!document.querySelector('#book_name2'))return null;
      const key=location.href, old=window.__localReaderStvToc;
      if(!old||old.key!==key){
        const state=window.__localReaderStvToc={key};
        fetch(ENDPOINT,{credentials:'same-origin'}).then(async r=>{
          if(!r.ok)return {error:{code:'http_error',http_status:r.status}};
          try{return {toc:await r.json()};}catch(_){return {error:{code:'parse_error'}};}
        }).then(value=>state.value=value).catch(()=>state.value={error:{code:'network_error'}});
        return null;
      }
      if(!old.value)return null;
      return {url:location.href,html:SELECTED,...old.value};
    })()'''.replace('ENDPOINT', json.dumps(toc_url)).replace('SELECTED', selected)


def _search_expression(page):
    return '''(()=>{
      if(document.readyState==='loading'||window.loading===true)return null;
      const container=document.getElementById('search-containter');
      if(!container||(!container.querySelector('.booksearch')&&!container.innerText.includes('Không tìm thấy truyện?')))return null;
      const shown=document.getElementById('spag');
      if(shown&&parseInt(shown.textContent,10)!==PAGE)return null;
      const pagination_html=Array.from(document.querySelectorAll('a.page-link[onclick]')).map(a=>{
        const clone=a.cloneNode(true);
        if(a.closest('.disabled,[aria-disabled="true"]')||!a.getClientRects().length)clone.setAttribute('aria-disabled','true');
        return clone.outerHTML;
      }).join('');
      const html=container.outerHTML, key=location.href, signature=html+pagination_html;
      const old=window.__localReaderStvSearch;
      if(!old||old.key!==key||old.signature!==signature){window.__localReaderStvSearch={key,signature,at:Date.now()};return null;}
      if(Date.now()-old.at<500)return null;
      return {url:location.href,html,pagination_html};
    })()'''.replace('PAGE', str(page))


def _chapter_expression(book, chapter):
    target = json.dumps('cld-' + book + '-' + chapter)
    return '''(()=>{
      const language=document.querySelector('.seloption[value="vi"]');
      const languageKey='__localReaderStvLanguageChange';
      try{
        const stored=sessionStorage.getItem(languageKey);
        if(stored){
          const state=JSON.parse(stored);
          if(!state||state.url!==location.href||typeof state.timeOrigin!=='string')sessionStorage.removeItem(languageKey);
          else{
            if(state.timeOrigin===String(performance.timeOrigin)||document.readyState!=='complete')return null;
            sessionStorage.removeItem(languageKey);
            return {url:location.href,language_changed:true};
          }
        }
        if(language&&language.getClientRects().length){
          sessionStorage.setItem(languageKey,JSON.stringify({url:location.href,timeOrigin:String(performance.timeOrigin)}));
          language.click();
          return null;
        }
      }catch(_){return {url:location.href,error:{code:'browser_error',evidence:'Không lưu được trạng thái chọn tiếng Việt trong phiên trình duyệt.'}};}
      const node=document.getElementById(TARGET);
      const text=node?.innerText?.trim()||'';
      if(!node||!node.classList.contains('contentbox')||!node.closest('#content-container')||!text
          ||/nhấp\\s+vào\\s+để\\s+tải\\s+chương|đang\\s+tải\\s+(?:nội\\s+dung|chương)/i.test(text)
          ||(text.match(/[\\u3400-\\u9fff]/g)||[]).length>(text.match(/[A-Za-z\\u00c0-\\u024f\\u1e00-\\u1eff]/g)||[]).length){
        const requests=performance.getEntriesByType('resource');
        for(let i=requests.length-1;i>=0;i--){
          let request;
          try{request=new URL(requests[i].name);}catch(_){continue;}
          if(request.origin!==location.origin||request.pathname!=='/index.php'
              ||request.searchParams.get('sajax')!=='readchapter'
              ||request.searchParams.get('bookid')!==BOOK||request.searchParams.get('c')!==CHAPTER
              ||request.searchParams.get('h')!==location.pathname.split('/')[2])continue;
          const status=requests[i].responseStatus;
          if(status===403||status===404)return {url:location.href,error:{code:'http_error',http_status:status}};
          break;
        }
        const pending=document.getElementById('maincontent');
        const evidence=(pending?.innerText||'').trim().slice(0,600);
        if(/\\bvip\\b/i.test(evidence)&&/chưa\\s+(?:mua|mở\\s+khóa)|không\\s+có\\s+quyền|(?:cần|phải|vui\\s+lòng|yêu\\s+cầu)\\s+(?:mua|mở\\s+khóa)/i.test(evidence))return {url:location.href,error:{code:'vip_locked',evidence}};
        if(/(?:vui\\s+lòng|cần|hãy|phải)\\s+đăng\\s+nhập/i.test(evidence))return {url:location.href,error:{code:'auth_required',evidence}};
        return null;
      }
      const html=node.outerHTML, key=location.href;
      const old=window.__localReaderStvChapter;
      if(!old||old.key!==key||old.html!==html){window.__localReaderStvChapter={key,html,at:Date.now()};return null;}
      if(Date.now()-old.at<1500)return null;
      return {url:location.href,html};
    })()'''.replace('TARGET', target).replace('BOOK', json.dumps(book)).replace('CHAPTER', json.dumps(chapter))


class StvSource:
    def __init__(self, browser):
        self.browser = browser

    def open(self, url):
        _parts(url, chapter=bool(re.fullmatch(r'/truyen/[^/]+/1/[0-9]+/[0-9]+/', urlsplit(url).path)))
        return self.browser.open(url)

    def _read(self, url, expression):
        try:
            data = self.browser.read(url, expression, timeout=60)
        except SourceError:
            raise
        except Exception as exc:
            code = {'browser_timeout': 'source_timeout', 'browser_unavailable': 'browser_unavailable',
                    'browser_error': 'browser_error'}.get(getattr(exc, 'code', ''), 'network_error')
            raise SourceError('Không mở được trình duyệt nguồn: ' + str(exc), code=code) from exc
        if not isinstance(data, dict) or data.get('url') != url:
            raise SourceError('Trình duyệt trả sai trang nguồn', code='parse_error')
        return data

    def _html(self, data):
        markup = data.get('html', '')
        if not isinstance(markup, str) or len(markup) > _MAX_HTML:
            raise SourceError('Trang nguồn trả nội dung không hợp lệ hoặc quá lớn', code='parse_error')
        try:
            return parse_html(markup)
        except ValueError as exc:
            raise SourceError('Không đọc được cấu trúc trang nguồn', code='parse_error') from exc

    def _error(self, data):
        error = data.get('error')
        if not isinstance(error, dict):
            return
        code = error.get('code')
        evidence = str(error.get('evidence', ''))[:600]
        if code == 'http_error' and type(error.get('http_status')) is int:
            status = error['http_status']
            raise SourceError('Nguồn trả HTTP ' + str(status), code=code, http_status=status)
        if code in ('network_error', 'parse_error', 'browser_error'):
            raise SourceError('Không tải được nội dung nguồn: ' + (evidence or code), code=code)
        if code == 'vip_locked' and _VIP.search(evidence) and _DENIED.search(evidence):
            raise SourceAccessError('Chương VIP chưa được mở trên trang gốc.', code='vip_locked', vip=True, evidence=evidence)
        if code == 'auth_required' and _LOGIN.search(evidence):
            raise SourceAccessError('Trang gốc yêu cầu đăng nhập.', code='auth_required', evidence=evidence)
        raise SourceAccessError('Chưa xác minh được quyền đọc trên trang gốc.', evidence=evidence)

    def _metadata(self, data, url):
        self._error(data)
        root = self._html(data)
        def meta(name):
            node = root.first('meta[property="' + name + '"]')
            return node.get('content') if node else ''
        name_node = root.first('#book_name2')
        name = name_node.text() if name_node else meta('og:novel:book_name') or meta('og:title')
        if not name:
            raise SourceError('Không tìm thấy thông tin truyện Sáng Tác Việt', code='parse_error')
        return {'name': name, 'author': meta('og:novel:author'), 'description': meta('og:description'),
                'cover': safe_remote_url(meta('og:image'), url), 'path': url, 'source': 'stv',
                'kind': 'text', 'format': 'online'}

    def search(self, query, page):
        fields = {'find': '', 'findinname': query, 'minc': 0, 'tag': ''}
        if page > 1:
            fields['p'] = page
        url = ORIGIN + '/search/?' + urlencode(fields)
        data = self._read(url, _search_expression(page))
        self._error(data)
        root = self._html(data)
        rows = root.select('a.booksearch')
        if not rows:
            message = root.first('#search-containter center')
            if message and message.text().startswith('Không tìm thấy truyện?'):
                return {'items': [], 'next': None}
            raise SourceError('Chưa xác nhận được kết quả tìm kiếm từ trang gốc', code='parse_error')
        items, seen = [], set()
        for row in rows:
            name, author, tag = row.first('.searchbooktitle'), row.first('.searchbookauthor'), row.first('.searchtag')
            path = safe_remote_url(row.get('href'), ORIGIN)
            if not name or not name.text() or not path:
                raise SourceError('Kết quả tìm kiếm thiếu tên hoặc liên kết truyện', code='parse_error')
            path = path.rstrip('/') + '/'
            _parts(path)
            if path in seen:
                continue
            seen.add(path)
            image = row.first('img')
            cover = safe_remote_url(image.get('data-original') or image.get('data-src') or image.get('src'), url) if image else ''
            items.append({'name': name.text(), 'author': author.text() if author else '', 'path': path,
                'cover': cover, 'description': tag.text() if tag else ''})
        pagination = data.get('pagination_html', '')
        if not isinstance(pagination, str) or len(pagination) > _MAX_HTML:
            raise SourceError('Phân trang tìm kiếm không hợp lệ', code='parse_error')
        pager = parse_html(pagination) if pagination else root
        next_page = None
        for link in pager.select('a.page-link'):
            if link.get('onclick').strip().rstrip(';') != 'gopagen()':
                continue
            ancestor, disabled = link, False
            while ancestor:
                disabled = disabled or 'disabled' in ancestor.get('class').split() or ancestor.get('aria-disabled') == 'true'
                ancestor = ancestor.parent
            if not disabled:
                next_page = page + 1
                break
        return {'items': items, 'next': next_page}

    def cover(self, url):
        _parts(url)
        book = self._metadata(self._read(url, _metadata_expression()), url)
        if not book['cover']:
            raise SourceError('Trang truyện không cung cấp bìa hợp lệ', code='parse_error')
        return {'cover': book['cover'], 'path': url}

    def detail(self, url):
        host, book_id, _ = _parts(url)
        endpoint = ORIGIN + '/index.php?' + urlencode({'ngmar': 'chapterlist', 'h': host,
            'bookid': book_id, 'sajax': 'getchapterlist'})
        data = self._read(url, _metadata_expression(endpoint))
        book = self._metadata(data, url)
        response = data.get('toc')
        if not isinstance(response, dict) or response.get('code') != 1:
            raise SourceError('Không tải được mục lục Sáng Tác Việt đầy đủ', code='parse_error')
        def entries(value):
            if not isinstance(value, str) or not value.strip() or len(value) > _MAX_HTML:
                raise SourceError('Mục lục Sáng Tác Việt trống hoặc không hợp lệ', code='parse_error')
            result = []
            for row in value.strip().split('-//-'):
                fields = row.strip().split('-/-', 2)
                if len(fields) != 3 or not re.fullmatch(r'[0-9]+', fields[1]) or not fields[2].strip():
                    raise SourceError('Mục lục Sáng Tác Việt thiếu mã hoặc tên chương', code='parse_error')
                result.append((fields[1], fields[2].strip()))
            return result
        translated = entries(response.get('data'))
        original = entries(response['oridata']) if response.get('oridata') else translated
        if [item[0] for item in translated] != [item[0] for item in original]:
            raise SourceError('Trang gốc chưa trả đủ mục lục tiếng Việt', code='parse_error')
        chapters, seen = [], set()
        for chapter_id, title in translated:
            if chapter_id in seen:
                continue
            seen.add(chapter_id)
            chapters.append({'index': len(chapters), 'title': title, 'url': url + chapter_id + '/'})
        book['chapters'] = chapters
        return book

    def chapter(self, url):
        _, book_id, chapter_id = _parts(url, chapter=True)
        expression = _chapter_expression(book_id, chapter_id)
        data = self._read(url, expression)
        if data.get('language_changed') is True:
            data = self._read(url, expression)
            if data.get('language_changed') is True:
                raise SourceError('Trang gốc chưa hoàn tất chọn tiếng Việt; hãy mở bản gốc rồi thử lại.', code='parse_error')
        root = self._html(data)
        content = root.first('#cld-' + book_id + '-' + chapter_id)
        if content and 'contentbox' in content.get('class').split():
            for token in content.select('i[t][h]'):
                token.tag = 'span'
            clean = sanitize_html(content.inner_html(), url)
            text = html_to_text(clean)
            chinese = len(re.findall(r'[\u3400-\u9fff]', text))
            latin = len(re.findall(r'[A-Za-z\u00c0-\u024f\u1e00-\u1eff]', text))
            if text and not _PLACEHOLDER.search(text) and chinese <= latin:
                return {'html': clean, 'text': text, 'images': [n.get('src') for n in parse_html(clean).select('img')],
                        'language': 'vi', 'translation': 'source'}
        self._error(data)
        raise SourceError('Trang gốc chưa trả đúng nội dung chương', code='parse_error')
