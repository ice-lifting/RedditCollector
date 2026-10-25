"""开发实验：页面内 fetch 一批新帖子，只保存 HTML/JSON，不入库或下载图片。"""

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from reddit_browser.browser import get_home_tab
from reddit_browser.config import DEFAULT_ADDRESS, DEFAULT_EXPORT_ROOT, EXTRACT_SCRIPT


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def request_cursor(url):
    """只允许 Reddit 首页信息流 GET 地址，使用真实游标，不构造分页参数。"""
    if not isinstance(url, str):
        raise ValueError('分页地址必须是字符串')
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme != 'https' or parts.netloc != 'www.reddit.com' or parts.fragment or
            parts.path != '/svc/shreddit/feeds/home-feed' or
            any(len(query.get(key, [])) != 1 or not query[key][0].strip() for key in ('cursor', 'after'))):
        raise ValueError('无效的 Reddit 首页分页地址，需要同源 HTTPS 地址及唯一的 after、cursor')
    return query['cursor'][0]


def parse_fragment(tab, html):
    # HTML 仅放进未挂载的 template，脚本不执行，图片不进入活动页面。
    # 运行的是本地受信任提取代码；响应内容只作为字符串参数传递。
    script = EXTRACT_SCRIPT.read_text(encoding='utf-8')
    parser = '''
const template = globalThis.document.createElement('template');
template.innerHTML = arguments[0];
const fragment = template.content;
const selector = 'main#main-content shreddit-feed shreddit-post';
const document = {querySelectorAll: css => fragment.querySelectorAll(css === selector ? 'shreddit-post' : css)};
const result = (function(){
''' + script + '''
}).apply(null, ['', 'export']);
const next = [...fragment.querySelectorAll('faceplate-partial[src]')].map(n=>({
    url:new URL(n.getAttribute('src'),location.href).href,
    method:(n.getAttribute('method')||'GET').toUpperCase()
})).filter(n=>{const url=new URL(n.url);return url.origin===location.origin &&
    url.pathname==='/svc/shreddit/feeds/home-feed' && url.searchParams.has('cursor') &&
    url.searchParams.has('after') && n.method==='GET';});
return {posts:result.matches,next_requests:[...new Map(next.map(n=>[n.url,n])).values()],
    fragment_card_count:result.loaded_card_count};
'''
    return tab.run_js(parser, html, timeout=15)


def fetch_next_posts(*, address=DEFAULT_ADDRESS, output_root=None, inspect_only=False, timeout=25,
                     request_url=None):
    if not 1 <= timeout <= 60:
        raise ValueError('timeout 必须在 1 到 60 秒之间')
    if request_url is not None:
        request_cursor(request_url)
    tab, _ = get_home_tab(address, navigate=False)
    script = Path(__file__).with_suffix('.js').read_text(encoding='utf-8')
    captured = tab.run_js(script, inspect_only, timeout * 1000, request_url or '', timeout=timeout+10)
    folder = Path(output_root or DEFAULT_EXPORT_ROOT/'feed_requests').resolve() / datetime.now().strftime('batch_%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True)
    html = captured.pop('html', None)
    captured['captured_at'] = datetime.now(timezone.utc).isoformat()
    captured['tab_id'] = tab.tab_id
    write_json(folder/'request_result.json', captured)
    if html is not None:
        (folder/'response.html').write_text(html, encoding='utf-8')
    if captured['status'] != 'fetched':
        return {**captured,'output_directory':str(folder)}
    try:
        if captured['before']['url'] != captured['after']['url']:
            raise RuntimeError('请求期间页面 URL 发生变化，停止解析，不重试')
        parsed = parse_fragment(tab, html)
        posts = list({p['id']:p for p in parsed['posts'] if p.get('id')}.values())
        before_ids = set(captured['before']['post_ids'])
        new_ids = [p['id'] for p in posts if p['id'] not in before_ids]
        data = {'captured_at':captured['captured_at'],'page_url':captured['before']['url'],
                'scope':'fetched_home_feed_fragment','request_url':captured['request']['url'],
                'post_count':len(posts),'new_to_current_dom_ids':new_ids,
                'next_requests':parsed['next_requests'],'posts':posts,
                'database_written':False,'images_downloaded':False}
        write_json(folder/'posts.json', data)
        summary = {'status':'success' if posts else 'empty_response',
            'status_code':captured['status_code'],'content_type':captured['content_type'],
            'post_count':len(posts),'new_to_current_dom_count':len(new_ids),
            'first_id':posts[0]['id'] if posts else None,'last_id':posts[-1]['id'] if posts else None,
            'next_request_count':len(parsed['next_requests']),
            'page_state_unchanged_during_fetch':captured['before']==captured['after'],
            'database_written':False,'images_downloaded':False,
            'output_directory':str(folder),'posts_json':str(folder/'posts.json'),
            'response_html':str(folder/'response.html')}
    except Exception as error:
        summary = {'status':'parse_failed','error':str(error),'output_directory':str(folder),
                   'response_saved':html is not None}
    write_json(folder/'summary.json', summary)
    return summary
