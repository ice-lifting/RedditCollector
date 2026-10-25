"""补齐搜索帖详情，复用首页快照的正文、媒体、预览与入库流程。"""

import random
import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

from .config import EXTRACT_SCRIPT
from .home_snapshot import save_capture


def extract_detail(tab, html, url, post_id):
    # 使用相同的提取器；HTML 只放入离线 template，不执行响应脚本。
    script = EXTRACT_SCRIPT.read_text(encoding='utf-8')
    wrapper = """
const template = document.createElement('template');
template.innerHTML = arguments[0];
const extract = function(document, location) {
    return (function() { SCRIPT }).call(null, '', 'detail_export');
};
return extract(template.content, {href: arguments[1]});
""".replace('SCRIPT', script)
    parsed = tab.run_js(wrapper, html, url, timeout=15)
    matches = [p for p in parsed['matches'] if p.get('id') == post_id]
    if len(matches) != 1:
        raise ValueError(f'详情响应未找到唯一的目标帖子 {post_id}')
    return matches[0]


def save_search_capture(posts, *, tab, fetch, folder, result, database, task_id=None,
                        task_name=None, workers=4, interval_min=1, interval_max=2.5):
    start = time.monotonic()
    details = Path(folder) / 'details'
    details.mkdir()
    detail_reports = []
    stop_fetching = False
    for index, post in enumerate(posts, 1):
        # 保持与 extract_posts.js 相同的数据结构；失败项不伪造正文或媒体。
        defaults = {'body': None, 'post_type': 'unknown', 'content_url': None,
                    'upvote_ratio': None, 'original_post_id': None, 'original_post_url': None,
                    'parse_status': 'unsupported_type', 'media': [],
                    'media_scope': 'currently_present_dom_only'}
        for key, value in defaults.items():
            post.setdefault(key, value)
        report = {'post_id': post['id'], 'url': post['url']}
        try:
            if stop_fetching:
                raise RuntimeError('前一条详情请求失败，未继续发送请求')
            url = urlsplit(post['url'])
            if (url.scheme != 'https' or url.netloc != 'www.reddit.com'
                    or '/comments/' not in url.path):
                raise ValueError('无效的 Reddit 帖子详情地址')
            time.sleep(random.uniform(interval_min, interval_max))
            logging.getLogger(__name__).info(f'补充详情 {index}/{len(posts)}：{post["id"]}')
            try:
                response = fetch(tab, post['url'], index)
            except Exception:
                stop_fetching = True
                raise
            html_path = details / f'{index:04d}.html'
            html_path.write_text(response['html'], encoding='utf-8')
            report['response_html'] = str(html_path)
            detail = extract_detail(tab, response['html'], post['url'], post['id'])
            post.update(detail)
            report['status'] = 'parsed'
        except Exception as error:
            report.update(status='failed', error=str(error))
            post['detail_error'] = str(error)
        detail_reports.append(report)
    result['details'] = detail_reports
    return save_capture(
        posts, page={'page_url': result['request_url']}, tab_id=getattr(tab, 'tab_id', None),
        action='fetch_only_no_navigation', start=start, output_root=folder, workers=workers,
        database=database, task_id=task_id, task_name=task_name,
        save_database=database is not None, acquisition_method='search',
        query={'q': result['query'], 'sort': result['sort'], 't': result['time_filter']},
        captured_at=result['captured_at'],
        capture_context={key: result[key] for key in (
            'request_url', 'pages', 'stop_reason', 'next_requests', 'details')})
