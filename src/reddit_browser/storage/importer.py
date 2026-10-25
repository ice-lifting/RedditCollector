"""将已保存的首页快照写入五表模型，不重新浏览或下载。"""

from datetime import datetime, timezone
import json
from pathlib import Path

from . import DEFAULT_DATABASE, connect_database, initialize_database


from ..config import PROJECT_ROOT


def utc_timestamp(value):
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None:
        raise ValueError('采集时间必须带时区')
    return stamp.astimezone(timezone.utc).isoformat(timespec='microseconds')


def storage_path(value, export_directory):
    if not value:
        return None
    path = Path(value)
    path = (export_directory / path).resolve() if not path.is_absolute() else path.resolve()
    try:
        return path.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return str(path)


def upsert(conn, table, values, keys):
    # table、keys 和列名均由本模块固定定义，采集内容只通过参数绑定传入。
    columns = list(values)
    updates = ','.join(f'{c}=excluded.{c}' for c in columns if c not in keys)
    sql = (f'INSERT INTO {table} ({",".join(columns)}) VALUES '
           f'({",".join("?" for _ in columns)}) ON CONFLICT ({",".join(keys)}) '
           f'DO UPDATE SET {updates}')
    conn.execute(sql, [values[c] for c in columns])


def import_export(json_path, database=DEFAULT_DATABASE, task_id=None, task_name=None,
                  acquisition_method=None):
    """一次事务保存任务、帖子、关联及媒体；同任务重复导入按 ID 去重。"""
    if task_id is not None and task_name is not None:
        raise ValueError('task_id 和 task_name 只能选择一个')
    source = Path(json_path).resolve()
    data = json.loads(source.read_text(encoding='utf-8'))
    acquisition_method = acquisition_method or data.get('acquisition_method', 'home_snapshot')
    if acquisition_method not in ('home_snapshot', 'single_post', 'feed_batch', 'search', 'community'):
        raise ValueError('不支持的采集方式')
    captured = utc_timestamp(data['captured_at'])
    if not isinstance(data.get('posts'), list) or not data['posts']:
        raise ValueError('JSON 中没有帖子列表')
    source_url = data.get('page_url') or data['request_url']
    path = initialize_database(database)
    conn = connect_database(path)
    counts = {'posts_inserted': 0, 'posts_updated': 0, 'older_posts_skipped': 0,
              'media_processed': 0}
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if task_id is None:
                prefix = {'single_post': '单帖采集', 'home_snapshot': '首页快照',
                          'feed_batch': '信息流批次', 'search': '全站搜索', 'community': '社区采集'}[acquisition_method]
                name = task_name if task_name is not None else f'{prefix} {captured}'
                cursor = conn.execute('INSERT INTO tasks(name,description,created_at) VALUES (?,?,?)',
                                      (name, f'首次导入来源：{source}',
                                       datetime.now(timezone.utc).isoformat()))
                task_id = cursor.lastrowid
            elif not conn.execute('SELECT id FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise ValueError(f'任务 {task_id} 不存在')
            for post in data['posts']:
                post_id = post['id']
                if not isinstance(post_id, str) or not post_id.startswith('t3_'):
                    raise ValueError(f'无效帖子 ID：{post_id!r}')
                old_row = conn.execute('SELECT * FROM posts WHERE id=?', (post_id,)).fetchone()
                old = dict(old_row) if old_row else None
                newer = not old or captured >= old['last_seen_at']
                values = {
                    'id': post_id, 'title': post['title'],
                    'post_type': (old['post_type'] if old else 'unknown')
                        if acquisition_method == 'search' and post.get('post_type') in (None, 'unknown')
                        else post['post_type'],
                    'url': post['url'], 'body': post.get('body'),
                    'content_url': post.get('content_url'),
                    'thumbnail_url': post.get('thumbnail_url'),
                    'subreddit_id': post.get('subreddit_id'), 'subreddit_name': post.get('subreddit'),
                    'author_id': post.get('author_id'), 'author_name': post.get('author'),
                    'created_at': post.get('created_at'), 'score': post.get('score'),
                    'comment_count': post.get('comment_count'), 'upvote_ratio': post.get('upvote_ratio'),
                    'original_post_id': post.get('original_post_id'),
                    'original_post_url': post.get('original_post_url'),
                    'parse_status': (old['parse_status'] if old else post.get('parse_status', 'parsed'))
                        if acquisition_method == 'search' and post.get('post_type') in (None, 'unknown')
                        else post.get('parse_status', 'parsed'),
                    'body_local_path': storage_path(post.get('body_local_path'), source.parent),
                    'first_seen_at': min(captured, old['first_seen_at']) if old else captured,
                    'last_seen_at': captured,
                }
                if old and newer:
                    # 新快照未读到的字段不清掉已保存的正文或原帖关联等信息。
                    values = {key: (old.get(key) if value is None else value)
                              for key, value in values.items()}
                    if post.get('body') is not None and post['body'] != old['body']:
                        values['body_local_path'] = storage_path(post.get('body_local_path'), source.parent)
                if newer:
                    upsert(conn, 'posts', values, ('id',))
                    counts['posts_updated' if old else 'posts_inserted'] += 1
                else:
                    conn.execute('UPDATE posts SET first_seen_at=min(first_seen_at,?) WHERE id=?',
                                 (captured, post_id))
                    counts['older_posts_skipped'] += 1
                conn.execute('''INSERT INTO task_posts
                    (task_id,post_id,source_url,acquisition_method,first_seen_at,last_seen_at)
                    VALUES (?,?,?,?,?,?) ON CONFLICT(task_id,post_id) DO UPDATE SET
                    source_url=CASE WHEN excluded.last_seen_at>=task_posts.last_seen_at
                        THEN excluded.source_url ELSE task_posts.source_url END,
                    acquisition_method=CASE WHEN excluded.last_seen_at>=task_posts.last_seen_at
                        THEN excluded.acquisition_method ELSE task_posts.acquisition_method END,
                    first_seen_at=min(task_posts.first_seen_at,excluded.first_seen_at),
                    last_seen_at=max(task_posts.last_seen_at,excluded.last_seen_at)''',
                             (task_id, post_id, source_url, acquisition_method, captured, captured))
                for media in post.get('media', []):
                    position = media['position']
                    existing = conn.execute('SELECT * FROM post_media WHERE post_id=? AND position=?',
                                            (post_id, position)).fetchone()
                    previous = dict(existing) if existing else None
                    if previous and captured < previous['last_seen_at']:
                        conn.execute('UPDATE post_media SET first_seen_at=min(first_seen_at,?) WHERE id=?',
                                     (captured, previous['id']))
                        continue
                    item = {key: media.get(key) for key in (
                        'original_url', 'preview_url', 'alt_text', 'stream_url', 'poster_url',
                        'preview_video_url', 'caption_url', 'download_url')}
                    item.update(post_id=post_id, position=position, media_type=media['media_type'],
                                local_path=storage_path(media.get('local_path'), source.parent),
                                download_status=media.get('download_status', 'not_requested'),
                                download_error=media.get('error'),
                                downloaded_at=captured if media.get('download_status') == 'downloaded' else None,
                                first_seen_at=min(captured, previous['first_seen_at']) if previous else captured,
                                last_seen_at=captured)
                    identity = ('media_type', 'original_url', 'preview_url', 'poster_url')
                    if (previous and previous['download_status'] == 'downloaded'
                            and item['download_status'] != 'downloaded'
                            and all(item[k] == previous[k] for k in identity)):
                        for key in ('local_path', 'download_status', 'download_error', 'downloaded_at', 'download_url'):
                            item[key] = previous[key]
                    upsert(conn, 'post_media', item, ('post_id', 'position'))
                    counts['media_processed'] += 1
            task = dict(conn.execute('SELECT id,name FROM tasks WHERE id=?', (task_id,)).fetchone())
        return {'database': str(path), 'task_id': task['id'], 'task_name': task['name'],
                'source_json': str(source), **counts}
    finally:
        conn.close()
