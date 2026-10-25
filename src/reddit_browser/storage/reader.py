"""SQLite 只读搜索与阅读，不创建数据库、不访问浏览器、不执行用户 SQL。"""

from contextlib import closing
from pathlib import Path
import re
import sqlite3

from ..config import DEFAULT_DATABASE


def connect_readonly(database):
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError('数据库不存在，请先采集或导入数据')
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only = ON')
    return conn


def validate_page(limit, offset):
    if (isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100
            or isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
        raise ValueError('limit 必须为 1-100，offset 必须为非负整数')


def search_posts(query='', *, database=DEFAULT_DATABASE, task_id=None, subreddit=None,
                 author=None, post_type=None, limit=20, offset=0):
    """在已保存标题和正文中搜索字面关键词，支持任务/社区/作者/类型过滤。"""
    validate_page(limit, offset)
    if not isinstance(query, str):
        raise ValueError('搜索词必须是字符串')
    clauses, args = [], []
    if query:
        # LIKE 的通配符按字面字符处理；SQL 内容始终参数绑定。
        escaped = query.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        clauses.append("(p.title LIKE ? ESCAPE '\\' OR p.body LIKE ? ESCAPE '\\')")
        args.extend(['%' + escaped + '%'] * 2)
    if task_id is not None:
        clauses.append('EXISTS (SELECT 1 FROM task_posts t WHERE t.post_id=p.id AND t.task_id=?)')
        args.append(task_id)
    for column, value in [('subreddit_name', subreddit), ('author_name', author), ('post_type', post_type)]:
        if value is not None:
            if column == 'subreddit_name':
                value = 'r/' + value.removeprefix('r/').strip('/')
            clauses.append(f'p.{column} = ? COLLATE NOCASE')
            args.append(value)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    with closing(connect_readonly(database)) as conn:
        total = conn.execute('SELECT count(*) FROM posts p' + where, args).fetchone()[0]
        rows = conn.execute('''SELECT p.id,p.title,p.url,p.post_type,p.subreddit_name,p.author_name,
            p.score,p.comment_count,p.created_at,p.last_seen_at,substr(p.body,1,240) body_excerpt
            FROM posts p''' + where + ' ORDER BY p.last_seen_at DESC,p.id LIMIT ? OFFSET ?',
                            [*args, limit, offset]).fetchall()
    return {'query': query, 'total': total, 'limit': limit, 'offset': offset,
            'has_more': offset + len(rows) < total, 'posts': [dict(row) for row in rows]}


def read_post(post_id, *, database=DEFAULT_DATABASE, task_id=None, comment_limit=20, comment_offset=0):
    """读取已保存的完整正文、媒体文件记录、所属任务和已采集评论；不补发网络请求。"""
    validate_page(comment_limit, comment_offset)
    if not isinstance(post_id, str) or not re.fullmatch(r't3_[a-z0-9]+', post_id):
        raise ValueError('需要 t3_ 开头的帖子 ID')
    with closing(connect_readonly(database)) as conn:
        condition = 'p.id=?'
        args = [post_id]
        if task_id is not None:
            condition += ' AND EXISTS (SELECT 1 FROM task_posts t WHERE t.post_id=p.id AND t.task_id=?)'
            args.append(task_id)
        row = conn.execute('SELECT p.* FROM posts p WHERE ' + condition, args).fetchone()
        if row is None:
            return {'found': False, 'post_id': post_id}
        post = dict(row)
        post['media'] = [dict(r) for r in conn.execute(
            'SELECT * FROM post_media WHERE post_id=? ORDER BY position', (post_id,))]
        post['tasks'] = [dict(r) for r in conn.execute('''SELECT t.id,t.name,tp.acquisition_method,tp.source_url
            FROM tasks t JOIN task_posts tp ON t.id=tp.task_id WHERE tp.post_id=? ORDER BY t.id''', (post_id,))]
        total = conn.execute('SELECT count(*) FROM comments WHERE post_id=?', (post_id,)).fetchone()[0]
        comments = [dict(r) for r in conn.execute('''SELECT * FROM comments WHERE post_id=?
            ORDER BY created_at,id LIMIT ? OFFSET ?''', (post_id, comment_limit, comment_offset))]
    return {'found': True, 'post': post, 'comments': comments, 'comments_total': total,
            'comments_offset': comment_offset, 'comments_has_more': comment_offset + len(comments) < total}


def list_tasks(*, database=DEFAULT_DATABASE, limit=20, offset=0):
    validate_page(limit, offset)
    with closing(connect_readonly(database)) as conn:
        total = conn.execute('SELECT count(*) FROM tasks').fetchone()[0]
        rows = [dict(r) for r in conn.execute('''SELECT t.*,
            (SELECT count(*) FROM task_posts p WHERE p.task_id=t.id) post_count FROM tasks t
            ORDER BY t.id DESC LIMIT ? OFFSET ?''', (limit, offset))]
    return {'tasks': rows, 'total': total, 'has_more': offset + len(rows) < total}
