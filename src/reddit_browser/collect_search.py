"""全站帖子搜索实验：浏览器内 fetch 获取 HTML，按 cursor 逐页翻页，调用 parse_search 解析并汇总。"""

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
import logging
from pathlib import Path
import random
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from .search_parser import parse_search

from .config import DEFAULT_DATABASE, DEFAULT_EXPORT_ROOT
from .search_capture import save_search_capture
from .cdp_browser import connect_reddit

logger = logging.getLogger(__name__)

SORT_OPTIONS = {
    'relevance': '相关性', 'hot': '热门', 'top': '最受欢迎',
    'new': '最新', 'comments': '评论数量',
}
TIME_OPTIONS = {
    'all': '全部时间', 'year': '过去一年', 'month': '过去一个月',
    'week': '过去一周', 'day': '过去一天', 'hour': '过去一小时',
}
STOP_REASONS = {
    'max_pages': '达到页数上限', 'no_next': '没有下一页入口',
    'repeated_url': '分页地址重复', 'invalid_next': '分页地址不合法',
    'empty_page': '本页没有结果卡片', 'fetch_failed': '请求失败',
    'parse_failed': '解析失败', 'repeated_cursor': '分页游标重复',
}

SEARCH_ORIGIN = 'https://www.reddit.com'
SEARCH_PATH = '/search/'
FRAGMENT_PATH = '/svc/shreddit/search/'


FETCH_SEARCH = r'''
return (async () => {
    if (location.origin !== 'https://www.reddit.com') throw Error('需要 Reddit 标签页');
    const response = await fetch(arguments[0], {
        method: 'GET', credentials: 'same-origin', redirect: 'error',
        signal: AbortSignal.timeout(25000),
    });
    const contentType = response.headers.get('content-type') || '';
    if (!response.ok) throw Error('搜索请求失败 HTTP ' + response.status + '，停止，不重试');
    if (!contentType.includes('html')) throw Error('响应不是 HTML：' + contentType);
    const html = await response.text();
    if (html.length > 8 * 1024 * 1024) throw Error('响应超过 8 MiB 字符限制');
    return {status: response.status, request_url: arguments[0], response_url: response.url,
        content_type: contentType, html};
})();
'''


def search_page_url(query, sort, time_filter):
    """固定 /search/ 确保全站范围；不沿用当前社区的路径。"""
    return SEARCH_ORIGIN + SEARCH_PATH + '?' + urlencode({
        'q': query, 'type': 'posts', 'sort': sort, 't': time_filter,
    })


def validate_fragment_url(url, expected):
    """校验服务端分页地址；允许 Reddit 对关键词作空格等改写。"""
    if not isinstance(url, str):
        raise ValueError('分页地址必须是字符串')
    parts = urlsplit(url)
    if (parts.scheme != 'https' or parts.netloc != 'www.reddit.com' or parts.fragment
            or parts.path != FRAGMENT_PATH):
        raise ValueError('无效的分页地址，需要 Reddit 搜索片段的同源 HTTPS 地址')
    query = parse_qs(parts.query, keep_blank_values=True)
    cursor = query.get('cursor', [])
    if len(cursor) != 1 or not cursor[0].strip():
        raise ValueError('分页地址缺少唯一且非空的 cursor')
    keywords = query.get('q', [])
    if len(keywords) != 1 or not keywords[0].strip():
        raise ValueError('分页地址缺少唯一且非空的关键词 q')
    for key in ('type', 'sort', 't'):
        if key in query and query[key] != [expected[key]]:
            raise ValueError(f'分页地址的 {key} 与本次搜索不一致，停止翻页')
    return cursor[0]


def next_fragment_url(parsed, expected):
    """从当前页解析出的分页入口里挑出唯一的下一个地址；没有则返回 None。"""
    urls = {}
    for candidate in parsed.get('next_requests') or []:
        if not isinstance(candidate, dict):
            raise ValueError('next_requests 的每一项都必须是对象')
        if (candidate.get('method') or 'GET').upper() != 'GET':
            raise ValueError('分页只支持 GET 请求')
        url = candidate.get('url')
        validate_fragment_url(url, expected)
        urls[url] = None
    if not urls:
        return None
    if len(urls) > 1:
        raise ValueError(f'分页地址不唯一（{len(urls)} 个），停止翻页')
    return next(iter(urls))


def fetch_page(tab, url, page_number):
    response = tab.run_js(FETCH_SEARCH, url, timeout=35)
    if not isinstance(response, dict) or not response.get('html'):
        raise RuntimeError(f'第 {page_number} 页没有取得搜索 HTML，停止，不重试')
    return response


def search_global(query, address='127.0.0.1:9222', *, sort='relevance', time_filter='all',
                  max_pages=3, interval_min=1.0, interval_max=2.5,
                  database=DEFAULT_DATABASE, task_id=None, task_name=None, workers=4, output_root=None, target_id=None):
    if not isinstance(workers, int) or isinstance(workers, bool) or not 1 <= workers <= 8:
        raise ValueError('workers 必须在 1 到 8 之间')
    if task_id is not None and task_name is not None:
        raise ValueError('task_id 和 task_name 只能选择一个')
    if task_id is not None and (isinstance(task_id, bool) or not isinstance(task_id, int) or task_id < 1):
        raise ValueError('task_id 必须为正整数')
    if task_name is not None and (not isinstance(task_name, str) or not task_name.strip()):
        raise ValueError('task_name 不能为空')
    if not isinstance(query, str) or not query.strip():
        raise ValueError('搜索词不能为空')
    if sort not in SORT_OPTIONS or time_filter not in TIME_OPTIONS:
        raise ValueError('不支持的排序或时间范围')
    if not isinstance(max_pages, int) or isinstance(max_pages, bool) or not 1 <= max_pages <= 20:
        raise ValueError('max_pages 必须是 1 到 20 之间的整数')
    if (not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
                for v in (interval_min, interval_max)) or not 0 <= interval_min <= interval_max <= 30):
        raise ValueError('间隔必须是 0 到 30 秒之间的有限数值，且最短间隔不能大于最长间隔')

    expected = {'q': query, 'type': 'posts', 'sort': sort, 't': time_filter}
    if task_id is not None and database is not None:
        from contextlib import closing
        from .storage.reader import connect_readonly
        with closing(connect_readonly(database)) as conn:
            if not conn.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise ValueError('任务不存在')
    captured_at = datetime.now(timezone.utc).isoformat()
    tab = connect_reddit(address, target_id)
    folder = Path(output_root or DEFAULT_EXPORT_ROOT / 'searches').resolve() / datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True)
    first_url = search_page_url(query, sort, time_filter)
    logger.info(f'排序：{SORT_OPTIONS[sort]}；时间范围：{TIME_OPTIONS[time_filter]}；最多 {max_pages} 页')
    logger.info(f'全站搜索：{first_url}')

    url = first_url
    visited = set()
    visited_cursors = set()
    posts = []
    seen = set()
    pages = []
    stop_reason = None
    stop_detail = None
    duplicate_count = 0
    last_parsed = None

    for page_number in range(1, max_pages + 1):
        if page_number > 1:
            # 先确认有下一页再等待，避免结束时多等一次。
            seconds = random.uniform(interval_min, interval_max)
            logger.info(f'\n等待 {seconds:.2f} 秒后请求第 {page_number} 页…')
            time.sleep(seconds)
        visited.add(url)
        page_dir = folder / f'page_{page_number:02d}'
        page_dir.mkdir()
        try:
            response = fetch_page(tab, url, page_number)
        except Exception as error:
            stop_reason = 'fetch_failed'
            stop_detail = str(error)
            pages.append({'page': page_number, 'request_url': url, 'status': 'failed',
                          'error': str(error)})
            break
        html = response.pop('html')
        (page_dir / 'response.html').write_text(html, encoding='utf-8')
        response.update(page=page_number, fetched_at=datetime.now(timezone.utc).isoformat())
        (page_dir / 'request.json').write_text(
            json.dumps(response, ensure_ascii=False, indent=2), encoding='utf-8')

        try:
            parsed = parse_search(tab, html)
            if (not isinstance(parsed, dict)
                    or not isinstance(parsed.get('posts'), list)
                    or not isinstance(parsed.get('next_requests'), list)
                    or any(not isinstance(p, dict) or not p.get('id') for p in parsed['posts'])):
                raise ValueError('搜索解析结果格式无效')
        except Exception as error:
            stop_reason = 'parse_failed'
            stop_detail = str(error)
            pages.append({'page': page_number, 'request_url': url, 'status': 'parse_failed',
                          'status_code': response['status'], 'error': str(error),
                          'response_html': str(page_dir / 'response.html')})
            break
        last_parsed = parsed
        new_posts = []
        for post in parsed['posts']:
            if post['id'] not in seen:
                seen.add(post['id'])
                new_posts.append(post)
        posts.extend(new_posts)
        duplicate_count += len(parsed['posts']) - len(new_posts)
        pages.append({
            'page': page_number, 'status': 'fetched', 'request_url': url,
            'response_url': response['response_url'], 'status_code': response['status'],
            'post_count': len(parsed['posts']), 'new_count': len(new_posts),
            'next_requests': parsed['next_requests'], 'empty_message': parsed['empty_message'],
            'response_html': str(page_dir / 'response.html'),
        })
        logger.info(f'第 {page_number} 页：解析 {len(parsed["posts"])} 条，新增 {len(new_posts)} 条')
        if not parsed['posts']:
            stop_reason = 'empty_page'
            stop_detail = parsed['empty_message'] or '未识别到结果卡片，请检查本页 HTML'
            break
        if page_number == max_pages:
            stop_reason = 'max_pages'
            break
        try:
            next_url = next_fragment_url(parsed, expected)
        except ValueError as error:
            stop_reason = 'invalid_next'
            stop_detail = str(error)
            break
        if next_url is None:
            stop_reason = 'no_next'
            break
        if next_url in visited:
            stop_reason = 'repeated_url'
            stop_detail = f'下一个分页地址与第 {page_number} 页之前的请求相同，停止'
            break
        cursor = validate_fragment_url(next_url, expected)
        if cursor in visited_cursors:
            stop_reason = 'repeated_cursor'
            stop_detail = '下一页游标已经请求过，停止翻页'
            break
        visited_cursors.add(cursor)
        url = next_url

    next_url = None
    next_error = None
    if last_parsed is not None and stop_reason == 'max_pages':
        try:
            next_url = next_fragment_url(last_parsed, expected)
        except ValueError as error:
            next_error = str(error)

    result = {
        'status': ('failed' if stop_reason in ('fetch_failed', 'parse_failed') else
                   'partial' if stop_reason == 'invalid_next' else 'success' if posts else 'empty'),
        'captured_at': captured_at, 'acquisition_method': 'search',
        'query': query, 'scope': 'global', 'sort': sort, 'time_filter': time_filter,
        'request_url': first_url, 'max_pages': max_pages,
        'pages_fetched': sum(1 for page in pages if page['status'] == 'fetched'),
        'post_count': len(posts), 'duplicate_count': duplicate_count,
        'stop_reason': stop_reason, 'stop_detail': stop_detail,
        'next_page_url': next_url, 'next_page_error': next_error,
        'next_requests': (last_parsed or {}).get('next_requests', []),
        'posts': posts, 'pages': pages, 'output_directory': str(folder),
    }
    result_path = folder / 'results.json'
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')

    if posts:
        try:
            result['capture'] = save_search_capture(
                posts, tab=tab, fetch=fetch_page, folder=folder, result=result,
                database=database, task_id=task_id, workers=workers,
                interval_min=interval_min, interval_max=interval_max,
                task_name=task_name if task_id is not None or task_name is not None
                    else f'全站搜索：{query} {captured_at}')
            result['storage'] = result['capture']['database']
            if result['storage']['status'] == 'failed':
                raise RuntimeError(result['storage']['error'])
            if (any(item['status'] == 'failed' for item in result.get('details', []))
                    or result['capture'].get('missing_media_urls')
                    or any(count for status, count in result['capture'].get('download_status', {}).items()
                           if status != 'downloaded')):
                if result['status'] != 'failed':
                    result['status'] = 'partial'
            # 汇总 JSON 与完整导出目录不同，文件引用需相对汇总文件重定位。
            result['posts'] = deepcopy(posts)
            capture_folder = Path(result['capture']['output_directory'])
            for post in result['posts']:
                if post.get('body_local_path'):
                    post['body_local_path'] = (capture_folder / post['body_local_path']).relative_to(folder).as_posix()
                for media in post.get('media', []):
                    if media.get('local_path'):
                        media['local_path'] = (capture_folder / media['local_path']).relative_to(folder).as_posix()
        except Exception as error:
            result['status'] = 'failed'
            result['storage'] = {'status': 'failed', 'error': str(error)}
            result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            raise RuntimeError(f'搜索结果保存失败：{error}；JSON 已保存到 {result_path}') from error
        result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        logger.info(f'完整导出：{result["capture"]["output_directory"]}')
        if result['storage']['status'] == 'saved':
            logger.info(f'入库完成：任务 {result["storage"]["task_id"]}，'
                  f'新增 {result["storage"]["posts_inserted"]} 条，'
                  f'更新 {result["storage"]["posts_updated"]} 条')
        failed_details = sum(item['status'] == 'failed' for item in result['details'])
        if failed_details:
            logger.info(f'有 {failed_details} 条详情未补齐，错误已记录在 results.json 的 details 中')

    logger.info(f'\n共请求 {len(pages)} 页，解析到 {len(posts)} 条帖子，跨页重复 {duplicate_count} 条')
    logger.info(f'停止原因：{STOP_REASONS.get(stop_reason, stop_reason)}'
          + (f'（{stop_detail}）' if stop_detail else ''))
    logger.info(f'\n结果目录：{folder}')
    if stop_reason in ('fetch_failed', 'parse_failed'):
        raise RuntimeError(f'{stop_detail}；已保存前几页结果到 {folder}')
    return result
