"""连接已有本地 Chrome 标签页；不创建或关闭浏览器。"""

import ipaddress
from contextlib import closing
import json
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, build_opener

from .config import DEFAULT_ADDRESS


def list_tabs(address=DEFAULT_ADDRESS):
    endpoint = urlsplit('http://' + address)
    try:
        valid = ipaddress.ip_address(endpoint.hostname).is_loopback
    except ValueError:
        valid = False
    if (not valid or endpoint.username or endpoint.password or endpoint.path
            or endpoint.query or endpoint.fragment or not endpoint.port):
        raise ValueError('address 必须是本地回环 IP:端口，例如 127.0.0.1:9222')
    opener = build_opener(ProxyHandler({}))
    with opener.open(f'http://{address}/json/list', timeout=5) as response:
        targets = json.load(response)
    return [t for t in targets if t.get('type') == 'page'
            and urlsplit(t.get('url', '')).scheme == 'https'
            and urlsplit(t.get('url', '')).netloc == 'www.reddit.com']


class RedditTab:
    def __init__(self, target, address):
        self.tab_id = target['id']
        self.websocket_url = f'ws://{address}/devtools/page/{self.tab_id}'

    def run_js(self, script, *args, timeout=35):
        import websocket
        expression = '(function(){\n' + script + '\n}).apply(null,' + json.dumps(args) + ')'
        with closing(websocket.create_connection(self.websocket_url, timeout=timeout,
                                                  suppress_origin=True)) as connection:
            connection.send(json.dumps({'id': 1, 'method': 'Runtime.evaluate', 'params': {
                'expression': expression, 'awaitPromise': True, 'returnByValue': True}}))
            while True:
                message = json.loads(connection.recv())
                if message.get('id') != 1:
                    continue
                if 'error' in message:
                    raise RuntimeError(str(message['error']))
                result = message['result']
                if 'exceptionDetails' in result:
                    error = result['exceptionDetails']
                    raise RuntimeError(error.get('exception', {}).get('description') or error['text'])
                return result['result'].get('value')


def connect_reddit(address=DEFAULT_ADDRESS, target_id=None):
    targets = list_tabs(address)
    if target_id is not None:
        targets = [t for t in targets if t['id'] == target_id]
    if len(targets) != 1:
        raise ValueError('需要唯一的已有 Reddit 标签页；多个标签页时请指定 target_id')
    return RedditTab(targets[0], address)
