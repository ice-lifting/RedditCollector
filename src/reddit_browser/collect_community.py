"""社区帖子采集：原始 HTML、按页去重、正文/媒体/预览及 SQLite 入库。"""

from datetime import datetime, timezone
import math
from pathlib import Path
import random
import re
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .cdp_browser import connect_reddit
from .config import DEFAULT_ADDRESS, DEFAULT_DATABASE, DEFAULT_EXPORT_ROOT, EXTRACT_SCRIPT
from .home_snapshot import save_capture, write_json

SORTS = ('hot', 'new', 'top', 'rising', 'best')
TIMES = ('all', 'year', 'month', 'week', 'day', 'hour')
ORIGIN = 'https://www.reddit.com'

FETCH = r'''
return (async () => {
    if (location.origin !== 'https://www.reddit.com') throw Error('标签页已离开 Reddit');
    const response = await fetch(arguments[0], {credentials:'same-origin',redirect:'error',
        signal:AbortSignal.timeout(25000)});
    if (!response.ok) throw Error('HTTP '+response.status+'，停止，不重试');
    const contentType=response.headers.get('content-type') || '';
    if (!contentType.toLowerCase().includes('html')) throw Error('响应不是 HTML');
    const html=await response.text();
    if (html.length > 8*1024*1024) throw Error('响应超过 8 MiB 字符限制');
    return {html,status:response.status,response_url:response.url,content_type:contentType};
})();
'''


def normalize_community(value):
    if not isinstance(value, str):
        raise ValueError('社区名必须是字符串')
    value = value.strip()
    if value.startswith('https://'):
        url = urlsplit(value)
        if url.netloc != 'www.reddit.com' or url.query or url.fragment:
            raise ValueError('需要 Reddit 社区 URL')
        value = url.path
    value = re.sub(r'^/?r/', '', value).rstrip('/')
    if not re.fullmatch(r'[A-Za-z0-9_]{2,21}', value):
        raise ValueError('社区名应为 2-21 位字母、数字或下划线')
    return value


def community_url(name, sort, time_filter):
    return f'{ORIGIN}/r/{name}/{sort}/' + ('?' + urlencode({'t': time_filter}) if sort == 'top' else '')


def validate_next(url, name, sort):
    if not isinstance(url, str):
        raise ValueError('分页地址必须是字符串')
    parts = urlsplit(url)
    query = parse_qs(parts.query, keep_blank_values=True)
    if (parts.scheme != 'https' or parts.netloc != 'www.reddit.com' or parts.fragment
            or parts.path != f'/svc/shreddit/community-more-posts/{sort}/'
            or len(query.get('name', [])) != 1 or query['name'][0].lower() != name.lower()
            or len(query.get('after', [])) != 1 or not query['after'][0].strip()):
        raise ValueError('分页地址的社区、排序或 after 游标无效')
    return query['after'][0]


def next_request(candidates, name, sort):
    urls = set()
    for item in candidates:
        if item.get('method', 'GET').upper() != 'GET':
            raise ValueError('分页只支持 GET')
        validate_next(item['url'], name, sort)
        urls.add(item['url'])
    if len(urls) > 1:
        raise ValueError('下一页地址不唯一')
    return next(iter(urls), None)


def parse_page(tab, html, url, name):
    extractor = EXTRACT_SCRIPT.read_text(encoding='utf-8')
    script = '''
const template=globalThis.document.createElement('template');
template.innerHTML=arguments[0];
const root=template.content;
const extract=function(document,location) {return (function(){ EXTRACT }).call(null,'','detail_export');};
const feed=root.querySelector('main#main-content shreddit-feed, shreddit-feed');
const scope=root.querySelector('main#main-content') || feed || root;
for(const card of scope.querySelectorAll('shreddit-post[is-ad]:not([is-ad="false"]),shreddit-ad-post')) card.remove();
const result=extract(scope,{href:arguments[1]});
const posts=result.matches.filter(p=>p.subreddit?.toLowerCase() === ('r/'+arguments[2]).toLowerCase());
const next=[...scope.querySelectorAll('faceplate-partial[src]')].map(n=>({
    url:new URL(n.getAttribute('src'),arguments[1]).href,method:(n.getAttribute('method')||'GET').toUpperCase()
})).filter(n=>new URL(n.url).pathname.startsWith('/svc/shreddit/community-more-posts/'));
return {posts,next_requests:next,card_count:result.loaded_card_count};
'''.replace('EXTRACT', extractor)
    parsed = tab.run_js(script, html, url, name, timeout=20)
    if not isinstance(parsed, dict) or not isinstance(parsed.get('posts'), list):
        raise ValueError('社区解析结果格式无效')
    return parsed


def collect_community(subreddit, *, sort='new', time_filter='all', max_pages=3,
                      interval_min=3.0, interval_max=6.0, workers=4,
                      address=DEFAULT_ADDRESS, target_id=None, database=DEFAULT_DATABASE,
                      task_id=None, task_name=None, output_root=None):
    """按响应中的真实地址翻页；每页先落盘，再下载媒体并在一个任务下入库。"""
    name = normalize_community(subreddit)
    if sort not in SORTS or time_filter not in TIMES:
        raise ValueError('不支持的排序或时间范围')
    if sort != 'top' and time_filter != 'all':
        raise ValueError('社区时间筛选仅支持 top 排序')
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 20:
        raise ValueError('max_pages 必须是 1-20 的整数')
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 8:
        raise ValueError('workers 必须是 1-8 的整数')
    if (any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
            for v in (interval_min, interval_max)) or not 0 <= interval_min <= interval_max <= 30):
        raise ValueError('请求间隔必须满足 0 <= min <= max <= 30')
    if task_id is not None and task_name is not None:
        raise ValueError('task_id 和 task_name 只能选择一个')
    if task_id is not None and (isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1):
        raise ValueError('task_id 必须是正整数')
    if task_name is not None and (not isinstance(task_name, str) or not task_name.strip()):
        raise ValueError('task_name 不能为空')
    if task_id is not None and database is not None:
        # 在浏览器请求之前检查已有任务，避免采集完才发现任务无效。
        from .storage.database import connect_database
        db_path = Path(database).resolve()
        if not db_path.is_file():
            raise ValueError('任务数据库不存在')
        conn = connect_database(db_path)
        try:
            if not conn.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise ValueError('任务不存在')
        finally:
            conn.close()
    tab = connect_reddit(address, target_id)
    stamp = datetime.now(timezone.utc).isoformat()
    folder = Path(output_root or DEFAULT_EXPORT_ROOT / 'communities').resolve() / datetime.now().strftime('community_%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True)
    url = community_url(name, sort, time_filter)
    report = {'subreddit': 'r/' + name, 'sort': sort, 'time_filter': time_filter,
              'captured_at': stamp, 'page_url': url, 'output_directory': str(folder),
              'task_id': task_id, 'pages': [], 'post_count': 0, 'duplicate_count': 0,
              'status': 'running', 'stop_reason': None, 'next_page_url': None}
    seen, visited = set(), set()
    current_task = task_id
    try:
        for number in range(1, max_pages + 1):
            if number > 1:
                time.sleep(random.uniform(interval_min, interval_max))
            page_dir = folder / f'page_{number:02d}'
            page_dir.mkdir()
            page = {'page': number, 'request_url': url, 'status': 'fetching'}
            report['pages'].append(page)
            write_json(folder / 'report.json', report)
            response = tab.run_js(FETCH, url, timeout=35)
            html = response.pop('html')
            (page_dir / 'response.html').write_text(html, encoding='utf-8')
            write_json(page_dir / 'request.json', {'request_url': url, **response})
            page['status'] = 'parsing'
            parsed = parse_page(tab, html, url, name)
            write_json(page_dir / 'posts.json', parsed)
            posts = []
            for post in parsed['posts']:
                if not re.fullmatch(r't3_[a-z0-9]+', post.get('id', '')):
                    raise ValueError('解析到无效帖子 ID')
                if post['id'] not in seen:
                    seen.add(post['id'])
                    posts.append(post)
            report['duplicate_count'] += len(parsed['posts']) - len(posts)
            page.update(post_count=len(parsed['posts']), new_count=len(posts))
            if posts:
                page['status'] = 'saving'
                capture = save_capture(
                    posts, page={'page_url': report['page_url']}, tab_id=tab.tab_id,
                    action='fetch_only_no_navigation', start=time.monotonic(),
                    output_root=page_dir, workers=workers, database=database,
                    task_id=current_task, task_name=(task_name or f'社区采集 r/{name} {stamp}') if current_task is None else None,
                    save_database=database is not None, acquisition_method='community',
                    query={'subreddit': name, 'sort': sort, 't': time_filter},
                    capture_context={'request_url': url, 'page': number,
                                     'next_requests': parsed['next_requests']})
                page['capture'] = capture
                if capture['database']['status'] == 'failed':
                    raise RuntimeError(capture['database']['error'])
                if capture['database']['status'] == 'saved':
                    current_task = capture['database']['task_id']
                    report['task_id'] = current_task
                report['post_count'] += len(posts)
            page['status'] = 'saved'
            # 即使后续请求失败，前面的正文、媒体和任务关联也已保存。
            write_json(folder / 'report.json', report)
            next_url = next_request(parsed['next_requests'], name, sort)
            report['next_page_url'] = next_url
            if not parsed['posts']:
                report['stop_reason'] = 'empty_page'
                break
            if not next_url:
                report['stop_reason'] = 'no_next'
                break
            cursor = validate_next(next_url, name, sort)
            if cursor in visited:
                report['stop_reason'] = 'repeated_cursor'
                report['next_page_url'] = None
                break
            if number == max_pages:
                report['stop_reason'] = 'max_pages'
                break
            visited.add(cursor)
            url = next_url
        media_incomplete = any(
            count for p in report['pages'] for status, count in
            p.get('capture', {}).get('download_status', {}).items() if status != 'downloaded')
        media_incomplete = media_incomplete or any(
            p.get('capture', {}).get('missing_media_urls', 0) for p in report['pages'])
        report['status'] = ('partial' if media_incomplete else 'success') if report['post_count'] else 'empty'
    except KeyboardInterrupt:
        report.update(status='interrupted', stop_reason='interrupted')
    except Exception as error:
        report.update(status='failed', stop_reason='error', error=str(error))
        if report['pages']:
            report['pages'][-1].update(status='failed', error=str(error))
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        write_json(folder / 'report.json', report)
    return report
