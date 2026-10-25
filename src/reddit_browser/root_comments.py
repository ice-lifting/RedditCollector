"""仅采集一级评论，下载附件，保存本地结果并入库。"""
from collections import deque
from datetime import datetime, timezone
from html import escape
import json
import math
import logging
from pathlib import Path
import random
import time
from urllib.parse import parse_qs, urlsplit
from .comment_support import CommentParser, FETCH, existing_tab, post_identity, request_key, download_comment_media
from reddit_browser.config import DEFAULT_ADDRESS, DEFAULT_EXPORT_ROOT, DEFAULT_DATABASE
from reddit_browser.storage.comment_importer import import_root_comments, validate_comment_target
logger = logging.getLogger(__name__)
DEFAULT_POST = None

class CollectionStopped(Exception):
    pass

def is_root_page(request):
    return parse_qs(urlsplit(request['url']).query).get('top-level') == ['1']

def collect_root_comments(post_url=DEFAULT_POST, *, top=10, max_requests=500, interval_min=1, interval_max=2, address=DEFAULT_ADDRESS, output_root=None, tab=None, resume_from=None, request_timeout=60, root_only=True, save_database=False, database=DEFAULT_DATABASE, task_id=1):
    if not root_only:
        raise ValueError('当前版本只支持一级评论')
    restored = None
    if resume_from is not None:
        if output_root is not None:
            raise ValueError('续采沿用原结果目录，不能同时指定 output_root')
        source = Path(resume_from).resolve()
        restored = json.loads(source.read_text(encoding='utf-8'))
        if bool(restored.get('parameters', {}).get('root_only', False)) != True:
            raise ValueError('续采范围不一致：旧两阶段结果与仅一级结果不能混用，旧文件保持不变')
        post_url = restored['post_url']
    if any((not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in (top, max_requests))):
        raise ValueError('top 和 max_requests 必须为正整数')
    if not all((math.isfinite(v) for v in (interval_min, interval_max))) or not 0 <= interval_min <= interval_max:
        raise ValueError('随机间隔必须有限、非负，且最短不大于最长')
    if not isinstance(request_timeout, (int, float)) or not math.isfinite(request_timeout) or (not 1 <= request_timeout <= 120):
        raise ValueError('request_timeout 必须为 1～120 秒')
    subreddit, post_id, endpoint = post_identity(post_url)
    if save_database:
        validate_comment_target(database, task_id, post_id)
    comments = {}
    roots = []
    selected = []
    initial = {'url': 'https://www.reddit.com' + endpoint + '?top-level=1&comments-remaining=100&commentPositionOffset=0', 'inputs': [], 'parent_comment_id': None}
    root_queue = deque([initial])
    root_scheduled = {request_key(initial)}
    deferred = {}
    thread_queues = {}
    executed = set()
    report = {'status': 'running', 'phase': 'roots', 'post_url': post_url, 'post_id': post_id, 'subreddit': subreddit, 'started_at': datetime.now(timezone.utc).isoformat(), 'parameters': {'top': top, 'max_requests': max_requests, 'interval_min': interval_min, 'interval_max': interval_max, 'request_timeout': request_timeout, 'root_only': True}, 'root_stage_complete': False, 'all_platform_comments_guaranteed': False, 'database_written': False, 'ranking_rule': 'score descending; unknown scores last; equal scores ordered by comment ID', 'selected_roots': [], 'threads': [], 'requests': [], 'tombstones': [], 'unresolved_reply_loaders': [], 'stop_reason': None, 'skipped_reply_loader_count': 0}
    if restored is not None:
        output = source.parent
        saved = json.loads((output / 'comments.json').read_text(encoding='utf-8'))
        root_snapshot = json.loads((output / 'root_comments.json').read_text(encoding='utf-8'))
        if any((item.get('post_id') != post_id for item in (restored, saved, root_snapshot))):
            raise ValueError('续采文件中的帖子 ID 不一致')
        comments = {c['id']: c for c in saved['comments']}
        if any((c.get('post_id') != post_id or c.get('depth') != 0 or c.get('parent_comment_id') for c in comments.values())):
            raise ValueError('续采数据必须为当前帖子的一级评论')
        report = restored
        roots = root_snapshot['comments'] if report['root_stage_complete'] else []
        selected = [item['id'] for item in report['selected_roots']]
        root_queue = deque(report['pending_root_requests'])
        deferred = {request_key(r): r for r in report['deferred_reply_requests']}
        thread_queues = {ident: deque(requests) for ident, requests in report['pending_thread_requests'].items()}
        pending = [*root_queue, *(r for q in thread_queues.values() for r in q), *deferred.values()]
        for request in pending:
            parts = urlsplit(request['url'])
            if parts.scheme != 'https' or parts.netloc != 'www.reddit.com' or parts.path != endpoint:
                raise ValueError('续采入口不属于当前帖子评论接口')
        if any((ident not in comments for ident in selected)) or any((ident not in selected for ident in thread_queues)):
            raise ValueError('续采的选中评论或队列不一致')
        pending_keys = {request_key(r) for r in root_queue} | {request_key(r) for q in thread_queues.values() for r in q}
        executed = {request_key(r['request']) for r in report['requests'] if 'returned_comments' in r} - pending_keys
        root_scheduled = {request_key(r) for r in root_queue} | executed
        session = len(report.get('resumptions', [])) + 1
        (output / f'before_resume_{session:02d}.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        report.setdefault('resumptions', []).append({'started_at': datetime.now(timezone.utc).isoformat(), 'previous_stop_reason': report['stop_reason'], 'requests_before': len(report['requests']), 'comments_before': len(comments), 'current_root_id': report.get('current_root_id')})
        report.update(status='running', stop_reason=None, finished_at=None)
        report.pop('error', None)
        report['parameters'].update(max_requests=max_requests, interval_min=interval_min, interval_max=interval_max, request_timeout=request_timeout)
        top = report['parameters']['top']
    else:
        output = Path(output_root or DEFAULT_EXPORT_ROOT / 'root_comments').resolve() / datetime.now().strftime(f'%Y%m%d_%H%M%S_%f_{post_id}')
        (output / 'responses').mkdir(parents=True)
    report['output_directory'] = str(output)
    tab = tab or existing_tab(address, post_url)

    def say(message):
        logger.info(message)

    def write(name, value):
        path = output / name
        temporary = path.with_suffix(path.suffix + '.tmp')
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)

    def save():
        report['request_count'] = len(report['requests'])
        report['root_count'] = sum((c['depth'] == 0 for c in comments.values()))
        report['total_saved_comments'] = len(comments)
        report['incidental_reply_count'] = 0
        report['pending_root_requests'] = list(root_queue)
        report['deferred_reply_requests'] = list(deferred.values())
        report['pending_thread_requests'] = {ident: list(queue) for ident, queue in thread_queues.items()}
        captured = report.get('root_captured_at') or report['started_at']
        write('comments.json', {'post_id': post_id, 'captured_at': captured, 'comments': list(comments.values())})
        write('root_comments.json', {'post_id': post_id, 'captured_at': captured, 'root_stage_complete': report['root_stage_complete'], 'comments': roots if report['root_stage_complete'] else [c for c in comments.values() if c['depth'] == 0]})
        write('run.json', report)

    def request_fragment(request, stage, root_id=None):
        if len(report['requests']) >= max_requests:
            raise CollectionStopped('max_requests')
        if report['requests']:
            wait = random.uniform(interval_min, interval_max)
            say(f'等待 {wait:.2f} 秒……')
            time.sleep(wait)
        index = len(report['requests']) + 1
        record = {'index': index, 'stage': stage, 'root_id': root_id, 'request': request}
        report['requests'].append(record)
        say(f"[{stage}] 请求 {index}/{max_requests}，父评论：{request['parent_comment_id'] or '帖子'}")
        result = tab.run_js(FETCH, json.dumps(request), endpoint, round(request_timeout * 1000), timeout=request_timeout + 10)
        html = result.pop('html', None)
        record.update(result)
        if result['status'] != 200 or html is None:
            reason = result.get('error_kind', 'http_error_or_unexpected_type')
            say(f"  请求停止：{reason}，HTTP={result['status']}；入口和已保存数据保留，不自动重试。")
            raise CollectionStopped(reason)
        name = f'responses/{index:04d}.html'
        (output / name).write_text(html, encoding='utf-8')
        record['response_file'] = name
        parsed = CommentParser(post_id, endpoint, request['parent_comment_id']).finish(html)
        record['returned_comments'] = len(parsed.comments)
        record['new_comments'] = len({c['id'] for c in parsed.comments} - comments.keys())
        record['tombstone_count'] = len(parsed.tombstones)
        report['tombstones'].extend(({'request_index': index, **item} for item in parsed.tombstones))
        if not parsed.comments and (not parsed.requests) and (not parsed.tombstones):
            raise CollectionStopped('empty_or_unrecognized_response')
        for comment in parsed.comments:
            if comment['depth'] != 0:
                continue
            old = comments.get(comment['id'])
            if old and report['root_stage_complete'] and (comment['depth'] == 0):
                continue
            if old and (not comment['parent_comment_id']):
                comment['parent_comment_id'] = old['parent_comment_id']
            comment['captured_at'] = datetime.now(timezone.utc).isoformat()
            comments[comment['id']] = comment
        executed.add(request_key(request))
        return parsed

    def render_preview():

        def attachments(c):
            parts = []
            if not c['body'] and c.get('content_type') in ('image', 'giphy'):
                parts.append('<p>图片 / GIF 评论</p>')
            for media in c.get('media', []):
                local = media.get('local_path')
                path = (output / local).resolve() if local else None
                if media.get('download_status') == 'downloaded' and path and path.is_relative_to(output) and path.is_file():
                    parts.append(f'''<p><a href="{escape(local, quote=True)}"><img src="{escape(local, quote=True)}" alt="{escape(media.get('alt_text') or '评论附件', quote=True)}" style="max-width:100%;max-height:500px"></a></p>''')
                else:
                    source = media.get('preview_url') or media.get('animation_url')
                    if source and urlsplit(source).scheme in ('http', 'https'):
                        parts.append(f'<p>附件未下载：<a href="{escape(source, quote=True)}">来源地址</a></p>')
            return ''.join(parts)
        parts = ['<!doctype html><meta charset="utf-8"><title>一级评论</title>', '<style>body{font:16px sans-serif;max-width:1100px;margin:30px auto}pre{white-space:pre-wrap;font:inherit}article{border-bottom:1px solid #ddd;padding:12px}small{color:#555}</style>', f"<h1>一级评论 {report['root_count']} 条</h1><p>状态：{escape(report['status'])}；停止原因：{escape(report['stop_reason'] or '无')}</p>"]
        parts.append('<details open>' + '<summary>全部一级评论</summary>')
        for c in roots or [c for c in comments.values() if c['depth'] == 0]:
            parts.append(f"<article><small>{escape(c['id'])} · score {c['score']}</small><pre>{escape(c['body'])}</pre>{attachments(c)}</article>")
        parts.append('</details>')
        (output / 'preview.html').write_text(''.join(parts), encoding='utf-8')
    say(f'目标帖子：{post_url}\n结果目录：{output}')
    if restored is not None:
        say(f"恢复进度：一级评论 {sum((c['depth'] == 0 for c in comments.values()))} 条，历史请求 {len(report['requests'])} 次；保留已保存数据。")
    if not report['root_stage_complete']:
        say('仅采一级分页；附带回复不保存、不展开。')
    save()
    try:
        while root_queue:
            request = root_queue[0]
            parsed = request_fragment(request, '一级评论')
            root_queue.popleft()
            for next_request in parsed.requests:
                key = request_key(next_request)
                if is_root_page(next_request):
                    if key in executed:
                        raise CollectionStopped('repeated_root_cursor')
                    if key not in root_scheduled:
                        root_scheduled.add(key)
                        root_queue.append(next_request)
                else:
                    report['skipped_reply_loader_count'] += 1
            save()
            reply_note = f"回复入口已忽略 {report['skipped_reply_loader_count']} 个"
            say(f"  一级评论累计 {report['root_count']} 条；待处理一级分页 {len(root_queue)} 个；{reply_note}。")
        roots = [dict(c) for c in comments.values() if c['depth'] == 0]
        report.setdefault('root_captured_at', datetime.now(timezone.utc).isoformat())
        report.update(root_stage_complete=True, status='completed', phase='finished', stop_reason='root_pages_exhausted')
        return report
    except CollectionStopped as error:
        report.update(status='partial', stop_reason=str(error))
    except KeyboardInterrupt:
        report.update(status='interrupted', stop_reason='keyboard_interrupt')
    except Exception as error:
        report.update(status='failed', stop_reason='exception', error=str(error))
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        report['completed_thread_count'] = sum((t['status'] == 'completed' for t in report['threads']))
        save()
        if report['status'] == 'completed' or report['stop_reason'] == 'max_requests':
            media_comments = [c for c in comments.values() if c['depth'] == 0]
            media_count = sum((len(c.get('media', c.get('media_urls', []))) for c in media_comments))
            if media_count:
                say(f'开始保存 {media_count} 个一级评论附件（图片 / GIF）。')
                try:
                    report['media_download_status'] = download_comment_media(media_comments, output)
                    report['media_file_count'] = len({m['local_path'] for c in media_comments for m in c.get('media', []) if m.get('download_status') == 'downloaded'})
                    for root in roots:
                        current = comments[root['id']]
                        root['media'] = current.get('media', [])
                        root['media_urls'] = current.get('media_urls', [])
                    say(f"附件保存结果：{report['media_download_status']}；本地文件 {report['media_file_count']} 个。")
                except KeyboardInterrupt:
                    report['media_download_error'] = 'interrupted'
                    say('附件下载已中断，已采集评论和已下载文件保留。')
                except Exception as error:
                    report['media_download_error'] = str(error)
                    say(f'附件保存异常，评论数据保留：{error}')
                save()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        save()
        if save_database and (report['root_count'] or report['root_stage_complete']):
            try:
                imported = import_root_comments(output / 'root_comments.json', database, task_id)
                report['database'] = {'status': 'saved', **imported}
                report['database_written'] = True
                say(f"一级评论入库成功：新增 {imported['comments_inserted']}，更新 {imported['comments_updated']}。")
            except Exception as error:
                report['database'] = {'status': 'failed', 'error': str(error)}
                report['database_written'] = False
                say(f'入库失败，本地文件保留：{error}')
            write('database_import.json', report['database'])
            save()
        render_preview()
        say(f"\n结束：{report['status']}，原因 {report['stop_reason']}；一级评论 {report['root_count']} 条；请求 {len(report['requests'])} 次；未展开回复。")
        say(f"查看结果：{output / 'preview.html'}")
    return report
