"""首页快照完整业务：提取、保存 JSON/正文/媒体、预览及入库。"""

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import time
from urllib.parse import urlparse

from .config import DEFAULT_ADDRESS, DEFAULT_DATABASE, DEFAULT_EXPORT_ROOT, EXTRACT_SCRIPT
from .media import download_media, local_reference
from .preview import write_preview
from .storage.importer import import_export

logger = logging.getLogger(__name__)

def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def export_home(address=DEFAULT_ADDRESS, output_root=None, workers=4,
                database=DEFAULT_DATABASE, task_id=None, task_name=None, save_database=True):
    """首页快照业务入口，供开发脚本调用。"""
    from .browser import get_home_tab, is_reddit
    if not 1 <= workers <= 8:
        raise ValueError('workers 必须在 1 到 8 之间')
    if task_id is not None and task_name is not None:
        raise ValueError('task_id 和 task_name 只能选择一个')
    start = time.monotonic()
    tab, action = get_home_tab(address)
    if not tab.wait.eles_loaded('css:main#main-content shreddit-feed shreddit-post', timeout=15):
        raise RuntimeError(f'没有首页卡片，请检查登录或页面验证状态：{tab.url}')
    script = EXTRACT_SCRIPT.read_text(encoding='utf-8')
    page = tab.run_js(script, '', 'export')
    if not is_reddit(page['page_url']) or urlparse(page['page_url']).path not in ('', '/'):
        raise RuntimeError('读取时页面已离开首页，请重新运行')
    posts = list({p['id']: p for p in page['matches'] if p['id']}.values())
    if not posts:
        raise RuntimeError('未读取到有效帖子')
    return save_capture(posts, page=page, tab_id=tab.tab_id, action=action, start=start,
                        output_root=output_root, workers=workers, database=database,
                        task_id=task_id, task_name=task_name, save_database=save_database)


def save_capture(posts, *, page, tab_id, action, start, output_root=None, workers=4,
                 database=DEFAULT_DATABASE, task_id=None, task_name=None, save_database=True,
                 acquisition_method='home_snapshot', query=None, captured_at=None, capture_context=None):
    """首页快照与单帖采集共用的文件、媒体及入库流程。"""
    root = Path(output_root or DEFAULT_EXPORT_ROOT).resolve()
    formats = {
        'home_snapshot': ('home', 'home.json', 'currently_loaded_home_feed_cards'),
        'single_post': ('post', 'post.json', 'single_loaded_home_feed_card'),
        'feed_batch': ('feed', 'feed.json', 'fetched_home_feed_fragment'),
        'search': ('search', 'search.json', 'global_search'),
        'community': ('community', 'community.json', 'community_feed'),
    }
    if acquisition_method not in formats:
        raise ValueError('不支持的采集方式')
    prefix, json_name, scope = formats[acquisition_method]
    output = root / (prefix + datetime.now().strftime('_%Y%m%d_%H%M%S_%f'))
    (output / 'media').mkdir(parents=True)
    (output / 'text').mkdir()
    for post in posts:
        post.update(body_local_path=None, body_local_url=None)
        if post['body']:
            filename = hashlib.sha256(post['id'].encode()).hexdigest()[:20] + '.txt'
            path = output / 'text' / filename
            path.write_text(post['body'], encoding='utf-8')
            ref = local_reference(path, output)
            post.update(body_local_path=ref['local_path'], body_local_url=ref['local_url'])
    data = {'captured_at': captured_at or datetime.now(timezone.utc).isoformat(),
            'page_url': page['page_url'], 'tab_id': tab_id, 'browser_action': action,
            'scope': scope, 'acquisition_method': acquisition_method,
            'query': query, 'json_name': json_name,
            'capture_context': capture_context,
            'download_policy': 'largest_available_preview_and_video_poster_only',
            'post_count': len(posts), 'posts': posts}
    # 先落盘采集数据，图片网络错误也不会丢掉文字和帖子信息。
    write_json(output / json_name, data)
    logger.info("已读取 %s 条帖子，开始下载预览图和封面。", len(posts))
    downloads = download_media(posts, output, workers)
    data['summary'] = {'post_types': dict(Counter(p['post_type'] for p in posts)),
                       'bodies_saved': sum(bool(p['body']) for p in posts),
                       'media_items': sum(len(p['media']) for p in posts),
                       'download_urls': len(downloads),
                       'download_status': dict(Counter(d['download_status'] for d in downloads)),
                       'missing_media_urls': sum(m['download_status'] == 'missing_url'
                                                 for p in posts for m in p['media']),
                       'total_download_bytes': sum(d.get('bytes', 0) for d in downloads),
                       'seconds': round(time.monotonic() - start, 3)}
    write_json(output / 'download_results.json', downloads)
    write_json(output / json_name, data)
    write_preview(output, data)
    database_result = {'status': 'not_requested'}
    if save_database:
        try:
            database_result = {'status': 'saved', **import_export(
                output / json_name, database, task_id, task_name, acquisition_method)}
        except Exception as error:
            database_result = {'status': 'failed', 'error': str(error)}
        # 入库失败也保留完整导出，并在 JSON 与命令行明确报告。
        data['database'] = database_result
        write_json(output / 'database_import.json', database_result)
        write_json(output / json_name, data)
    return {'output_directory': str(output), 'json_path': str(output / json_name),
            'preview_path': str(output / 'preview.html'), 'database': database_result,
            'post_count': len(posts), 'scope': scope,
            **data['summary']}
