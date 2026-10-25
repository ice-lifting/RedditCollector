"""按标题采集当前首页的一条已加载帖子，不导航、滚动或打开详情。"""

import re
import time
from urllib.parse import urlparse

from .browser import get_home_tab, is_reddit
from .config import DEFAULT_ADDRESS, DEFAULT_DATABASE, EXTRACT_SCRIPT
from .home_snapshot import save_capture


class PostSelectionError(ValueError):
    def __init__(self, code, message, candidates=None):
        super().__init__(message)
        self.code = code
        self.candidates = candidates or []


def collect_post(title, *, address=DEFAULT_ADDRESS, output_root=None, workers=4,
                 database=DEFAULT_DATABASE, task_id=None, task_name=None,
                 post_id=None, save_database=True):
    if not isinstance(title, str) or not title.strip():
        raise ValueError('标题不能为空')
    if post_id is not None and not re.fullmatch(r't3_[a-z0-9]+', post_id):
        raise ValueError('post_id 应为 t3_ 开头的帖子 ID')
    if not 1 <= workers <= 8:
        raise ValueError('workers 必须在 1 到 8 之间')
    if task_id is not None and task_name is not None:
        raise ValueError('task_id 和 task_name 只能选择一个')
    start = time.monotonic()
    tab, action = get_home_tab(address, navigate=False)
    script = EXTRACT_SCRIPT.read_text(encoding='utf-8')
    page = tab.run_js(script, title, 'single_export')
    if not is_reddit(page['page_url']) or urlparse(page['page_url']).path not in ('', '/'):
        raise PostSelectionError('page_changed', '读取时页面已离开首页，请在首页重新调用。')
    posts = list({p['id']: p for p in page['matches'] if p.get('id')}.values())
    if post_id:
        posts = [post for post in posts if post['id'] == post_id]
    if not posts:
        raise PostSelectionError('post_not_found', '当前首页已加载卡片中没有匹配的帖子；未搜索、滚动或打开详情。')
    if len(posts) > 1:
        raise PostSelectionError('ambiguous_post', '存在同标题的不同帖子，请补充 post_id 选择其中一条。',
                                 [{key: p.get(key) for key in ('id', 'title', 'url', 'subreddit')} for p in posts])
    result = save_capture(posts, page=page, tab_id=tab.tab_id, action=action, start=start,
                          output_root=output_root, workers=workers, database=database,
                          task_id=task_id, task_name=task_name, save_database=save_database,
                          acquisition_method='single_post', query={'title': title, 'post_id': post_id})
    return {**result, 'post_id': posts[0]['id'], 'matched_title': posts[0]['title']}
