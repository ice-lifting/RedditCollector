"""图片预览与视频封面下载；同 URL 去重，不下载视频或默认原图。"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import logging
import threading
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)

def image_extension(data):
    if data.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if data.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if data.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if data.startswith(b'RIFF') and data[8:12] == b'WEBP':
        return '.webp'
    return None


def local_reference(path, root):
    return {'local_path': path.relative_to(root).as_posix(), 'local_url': path.as_uri()}


def download_media(posts, output, workers):
    jobs = {}
    for post in posts:
        for media in post['media']:
            url = media.get('poster_url') if media['media_type'] == 'video' else media.get('preview_url')
            media.update(download_url=url, local_path=None, local_url=None,
                         download_status='pending' if url else 'missing_url')
            if url:
                jobs.setdefault(url, []).append(media)
    stopped = threading.Event()

    def download(url):
        if stopped.is_set():
            return {'download_status': 'skipped_after_block'}
        try:
            parsed = urlparse(url)
            if parsed.scheme != 'https' or not parsed.hostname or not (
                parsed.hostname.endswith('.redd.it') or parsed.hostname.endswith('.redditmedia.com')
            ):
                raise ValueError('不支持的媒体域名或协议')
            request = Request(url, headers={'User-Agent': 'Mozilla/5.0',
                              'Accept': 'image/webp,image/*;q=0.9,*/*;q=0.1'})
            with urlopen(request, timeout=30) as response:
                content_type = response.headers.get('Content-Type', '')
                if not content_type.lower().startswith('image/'):
                    raise ValueError(f'响应不是图片：{content_type}')
                data = response.read(50 * 1024 * 1024 + 1)
                ext = image_extension(data)
                if len(data) > 50 * 1024 * 1024 or not ext:
                    raise ValueError('图片超过 50 MiB 或文件签名不支持')
                filename = hashlib.sha256(url.encode()).hexdigest()[:24] + ext
                path = output / 'media' / filename
                path.write_bytes(data)
                return {'download_status': 'downloaded', **local_reference(path, output),
                        'content_type': content_type, 'bytes': len(data),
                        'final_url': response.url}
        except HTTPError as error:
            if error.code in (401, 403, 429):
                stopped.set()
            return {'download_status': 'failed', 'http_status': error.code, 'error': str(error)}
        except Exception as error:
            return {'download_status': 'failed', 'error': str(error)}

    outcomes = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(download, url): url for url in jobs}
        for future in as_completed(futures):
            url = futures[future]
            result = future.result()
            for media in jobs[url]:
                media.update(result)
            outcomes.append({'url': url, **result})
            logger.info("媒体 %s/%s：%s", len(outcomes), len(jobs), result["download_status"])
    return outcomes
