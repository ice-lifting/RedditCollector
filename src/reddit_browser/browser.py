"""接管现有 Chrome 并选择 Reddit 标签页；不创建新的浏览器进程。"""

import json
from urllib.parse import urlparse
from urllib.request import ProxyHandler, build_opener
from DrissionPage import Chromium, ChromiumOptions

def is_reddit(url):
    parsed = urlparse(url)
    host = (parsed.hostname or '').lower()
    return parsed.scheme in ('http', 'https') and (
        host == 'reddit.com' or host.endswith('.reddit.com')
    )


def get_home_tab(address, *, navigate=True):
    # 先探测现有浏览器；连接失败时不启动任何 Chrome 进程。
    opener = build_opener(ProxyHandler({}))
    with opener.open(f'http://{address}/json/list', timeout=5) as response:
        targets = json.load(response)
    pages = [target for target in targets if target.get('type') == 'page']
    reddit_pages = [target for target in pages if is_reddit(target.get('url', ''))]
    home = next((target for target in reddit_pages
                 if urlparse(target['url']).path in ('', '/')), None)
    if home is None and not navigate:
        raise RuntimeError('没有已打开的 Reddit 首页；请先打开首页，单帖采集不会自动导航。')
    options = ChromiumOptions().set_address(address).existing_only()
    browser = Chromium(options)
    if home:
        return browser.get_tab(home['id']), 'reused_home'
    if reddit_pages:
        tab = browser.get_tab(reddit_pages[0]['id'])
    else:
        blank = next((target for target in pages
                      if target.get('url') in ('about:blank', 'chrome://newtab/')), None)
        tab = browser.get_tab(blank['id']) if blank else browser.new_tab()
    tab.get('https://www.reddit.com/')
    return tab, 'navigated_home'
