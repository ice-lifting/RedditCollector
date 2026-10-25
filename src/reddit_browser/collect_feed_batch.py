"""开发闭环：获取一批信息流帖子，保存文件、下载预览图，再关联任务入库。"""

import json
import logging
import math
import random
from datetime import datetime, timezone
from pathlib import Path
import shutil
import sqlite3
import time

from .fetch_next_posts import fetch_next_posts, request_cursor
from reddit_browser.config import DEFAULT_ADDRESS, DEFAULT_DATABASE, DEFAULT_EXPORT_ROOT
from reddit_browser.home_snapshot import save_capture


def validate_task(database, task_id):
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id < 1:
        raise ValueError('需要有效的已有任务 ID')
    database = Path(database).resolve()
    if not database.is_file():
        raise ValueError('任务数据库不存在，未发送请求')
    conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    try:
        if not conn.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
            raise ValueError(f'任务 {task_id} 不存在，未发送请求')
    finally:
        conn.close()
    return database


def continuation_state(resume_json):
    previous_json = str(Path(resume_json).resolve())
    previous = json.loads(Path(previous_json).read_text(encoding='utf-8'))
    if previous.get('scope') != 'fetched_home_feed_fragment':
        raise ValueError('只能从信息流批次 JSON 接续，不能使用首页快照或单帖 JSON')
    context = previous.get('capture_context') or previous
    cursor = request_cursor(context['request_url'])
    visited = list(dict.fromkeys([*context.get('visited_cursors', []), cursor]))
    candidates = context.get('next_requests', [])
    if not isinstance(candidates, list):
        raise ValueError('next_requests 必须是列表')
    urls = set()
    for candidate in candidates:
        if candidate.get('method', '').upper() != 'GET':
            raise ValueError('分页只支持 GET 请求')
        url = candidate.get('url')
        request_cursor(url)
        urls.add(url)
    reason = ('no_next_url' if not urls else 'ambiguous_next_url' if len(urls) != 1 else
              'repeated_cursor' if request_cursor(next(iter(urls))) in visited else None)
    if reason:
        return {'status':'stopped','reason':reason,'database_written':False,
                'request_sent':False,'previous_json':previous_json}
    return {'request_url':urls.pop(),'visited_cursors':visited,'previous_json':previous_json}


def collect_feed_batch(*, source_json=None, resume_json=None, address=DEFAULT_ADDRESS, database=DEFAULT_DATABASE,
                       output_root=None, workers=4, task_id=1):
    if not 1 <= workers <= 8:
        raise ValueError('workers 必须在 1 到 8 之间')
    if source_json is not None and resume_json is not None:
        raise ValueError('复用本批和接续下一批不能同时指定')
    database = validate_task(database, task_id)
    start = time.monotonic()
    reused_saved_batch = source_json is not None
    request_url = None
    visited_cursors = []
    previous_json = None
    if resume_json is not None:
        state = continuation_state(resume_json)
        if state.get('status') == 'stopped':
            return state
        request_url, visited_cursors, previous_json = (state[k] for k in
            ('request_url','visited_cursors','previous_json'))
    if source_json is None:
        fetch_options = {'address':address}
        if request_url is not None:
            fetch_options['request_url'] = request_url
        fetched = fetch_next_posts(**fetch_options)
        if fetched['status'] != 'success':
            return {'status':'fetch_failed','fetch_result':fetched,'database_written':False}
        source_json = fetched['posts_json']
    source = Path(source_json).resolve()
    data = json.loads(source.read_text(encoding='utf-8'))
    if data.get('scope') != 'fetched_home_feed_fragment' or not data.get('posts'):
        raise ValueError('输入必须是已成功提取的非空信息流批次 posts.json')
    if request_url is not None and data.get('request_url') != request_url:
        raise ValueError('返回的批次请求地址与接续地址不一致，停止保存和入库')
    posts = list({p['id']:p for p in data['posts']}.values())
    conn = sqlite3.connect(database.as_uri()+'?mode=ro', uri=True)
    try:
        placeholders = ','.join('?' for _ in posts)
        existing = {row[0] for row in conn.execute(
            f'SELECT post_id FROM task_posts WHERE task_id=? AND post_id IN ({placeholders})',
            [task_id, *(p['id'] for p in posts)])}
    finally:
        conn.close()
    request_path = source.parent/'request_result.json'
    metadata = json.loads(request_path.read_text(encoding='utf-8')) if request_path.is_file() else {}
    context = {'request_url':data['request_url'], 'next_requests':data.get('next_requests', []),
               'source_json':str(source), 'reused_saved_batch':reused_saved_batch,
               'previous_json':previous_json,
               'visited_cursors':list(dict.fromkeys([*visited_cursors,request_cursor(data['request_url'])]))}
    result = save_capture(posts, page={'page_url':data['page_url']},
        tab_id=metadata.get('tab_id'), action='fetch_only_no_navigation', start=start,
        output_root=output_root or DEFAULT_EXPORT_ROOT/'auto', workers=workers,
        database=database, task_id=task_id, save_database=True,
        acquisition_method='feed_batch', captured_at=data['captured_at'], capture_context=context)
    output = Path(result['output_directory'])
    shutil.copyfile(source, output/'source_posts.json')
    for name in ('response.html', 'request_result.json'):
        if (source.parent/name).is_file():
            shutil.copyfile(source.parent/name, output/name)
    failed_media = any(count for status,count in result['download_status'].items() if status != 'downloaded')
    status = ('database_failed' if result['database']['status'] != 'saved' else
              'partial' if failed_media or result['missing_media_urls'] else 'success')
    report = {**result,'status':status,'task_id':task_id,'source_json':str(source),
              'new_to_task_count':sum(p['id'] not in existing for p in posts) if status != 'database_failed' else 0,
              'next_request_count':len(data.get('next_requests', [])),
              'previous_json':previous_json,
              'continuous_pagination':False}
    (output/'collection_result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def collect_feed_auto(*, task_id, max_posts=100, max_batches=10, interval_min=3,
                      interval_max=6, max_empty_batches=2, resume_json=None,
                      address=DEFAULT_ADDRESS, database=DEFAULT_DATABASE, output_root=None, workers=4):
    """顺序采集有限批次；批次处理结束后随机等待，按当前任务新增数计数。"""
    for name,value in (('max_posts',max_posts),('max_batches',max_batches),
                       ('max_empty_batches',max_empty_batches)):
        if not isinstance(value,int) or isinstance(value,bool) or value < 1:
            raise ValueError(f'{name} 必须是正整数')
    if (not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v)
                for v in (interval_min,interval_max)) or not 0 <= interval_min <= interval_max):
        raise ValueError('间隔必须是有限非负秒数，且最短间隔不能大于最长间隔')
    if not isinstance(workers,int) or isinstance(workers,bool) or not 1 <= workers <= 8:
        raise ValueError('workers 必须在 1 到 8 之间')
    database = validate_task(database,task_id)
    folder = Path(output_root or DEFAULT_EXPORT_ROOT/'auto').resolve() / datetime.now().strftime('auto_%Y%m%d_%H%M%S_%f')
    folder.mkdir(parents=True)
    journal = folder/'run.json'
    report = {'status':'running','stop_reason':None,'task_id':task_id,
        'started_at':datetime.now(timezone.utc).isoformat(),'finished_at':None,
        'parameters':{'max_posts':max_posts,'max_batches':max_batches,
            'interval_min':interval_min,'interval_max':interval_max,'max_empty_batches':max_empty_batches,
            'address':address,'database':str(database),'workers':workers,
            'continue_from':str(Path(resume_json).resolve()) if resume_json else None},
        'run_json':str(journal),'completed_batches':0,'posts_seen':0,'new_to_task_count':0,
        'duplicate_count':0,'consecutive_empty_batches':0,'download_status':{},
        'last_completed_json':None,'resume_from':str(Path(resume_json).resolve()) if resume_json else None,
        'batches':[],'waits':[]}

    def checkpoint():
        temporary = journal.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        temporary.replace(journal)

    checkpoint()
    try:
        while True:
            # 先判断是否有下一批，再随机等待，避免结束时多等一次。
            if report['resume_from']:
                state = continuation_state(report['resume_from'])
                if state.get('status') == 'stopped':
                    report.update(status='stopped',stop_reason=state['reason'])
                    break
            if report['completed_batches']:
                seconds = random.uniform(interval_min,interval_max)
                wait = {'after_batch':report['completed_batches'],'seconds':seconds,'completed':False}
                report['waits'].append(wait)
                checkpoint()
                logging.info('本批已保存，下批前随机等待 %.2f 秒。',seconds)
                time.sleep(seconds)
                wait['completed'] = True
            result = collect_feed_batch(resume_json=report['resume_from'],address=address,
                database=database,output_root=folder,workers=workers,task_id=task_id)
            if result['status'] not in ('success','partial'):
                detail = result.get('fetch_result',result)
                report.update(status='failed' if result['status']=='database_failed' else 'stopped',
                              stop_reason=detail.get('reason',detail.get('status','batch_failed')),
                              last_attempt=result)
                break
            new_count = result['new_to_task_count']
            report['completed_batches'] += 1
            report['posts_seen'] += result['post_count']
            report['new_to_task_count'] += new_count
            report['duplicate_count'] += result['post_count']-new_count
            report['consecutive_empty_batches'] = 0 if new_count else report['consecutive_empty_batches']+1
            for status,count in result['download_status'].items():
                report['download_status'][status] = report['download_status'].get(status,0)+count
            report['last_completed_json'] = report['resume_from'] = result['json_path']
            report['batches'].append({'batch':report['completed_batches'],'json_path':result['json_path'],
                'status':result['status'],'post_count':result['post_count'],'new_to_task_count':new_count})
            checkpoint()
            reason = ('max_posts' if report['new_to_task_count'] >= max_posts else
                      'max_batches' if report['completed_batches'] >= max_batches else
                      'max_empty_batches' if report['consecutive_empty_batches'] >= max_empty_batches else None)
            if reason:
                report.update(status='completed',stop_reason=reason)
                break
    except KeyboardInterrupt:
        report.update(status='interrupted',stop_reason='keyboard_interrupt')
    except Exception as error:
        report.update(status='failed',stop_reason='exception',error=str(error))
    finally:
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        checkpoint()
    return report
