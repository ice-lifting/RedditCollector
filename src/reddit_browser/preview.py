"""基于采集结果生成本地 HTML 预览，不自动加载远程媒体。"""

import html
from urllib.parse import urlparse

def write_preview(output, data):
    esc = lambda value: html.escape(str(value if value is not None else ''), quote=True)
    cards = []
    for post in data['posts']:
        images = []
        for media in post['media']:
            if media.get('local_path'):
                label = '视频封面' if media['media_type'] == 'video' else f"图片 {media['position'] + 1}"
                images.append(f'<figure><a href="{esc(media["local_path"])}">'
                              f'<img loading="lazy" src="{esc(media["local_path"])}" alt="{esc(label)}">'
                              f'</a><figcaption>{esc(label)}</figcaption></figure>')
            else:
                images.append(f'<p class="muted">媒体：{esc(media["download_status"])}</p>')
        body = f'<pre>{esc(post["body"])}</pre>' if post.get('body') else ''
        original_url = post.get('original_post_url')
        original = f'<p>原帖：{esc(original_url)}</p>' if original_url else ''
        # 外部链接仅允许 HTTP(S)，页面不自动加载远程资源。
        url = post.get('url') or ''
        heading = esc(post['title'])
        if urlparse(url).scheme in ('http', 'https'):
            heading = f'<a href="{esc(url)}" target="_blank" rel="noreferrer">{heading}</a>'
        cards.append(f'<article><h2>{heading}</h2><p class="muted">'
                     f'{esc(post["subreddit"])} · {esc(post["author"])} · {esc(post["post_type"])}'
                     f' · 分数 {esc(post["score"])} · 评论 {esc(post["comment_count"])}</p>'
                     f'{original}{body}<div class="images">{"".join(images)}</div></article>')
    markup = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Reddit 首页导出预览</title><style>
body{max-width:1150px;margin:32px auto;padding:0 20px;font:16px/1.6 system-ui;background:#f3f4f6;color:#17212b}
article{background:white;border-radius:12px;padding:24px;margin:20px 0}h2{font-size:21px;margin-top:0}
a{color:#2459a6}.muted,figcaption{color:#64748b;font-size:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}
.images{display:flex;flex-wrap:wrap;gap:16px}figure{margin:0;flex:1 1 300px;max-width:100%}
img{display:block;max-width:100%;max-height:720px;object-fit:contain}p{overflow-wrap:anywhere}</style>
<h1>Reddit 首页导出预览</h1>'''
    if data.get('acquisition_method') == 'single_post':
        markup = markup.replace('Reddit 首页导出预览', 'Reddit 单帖采集预览')
    elif data.get('acquisition_method') == 'feed_batch':
        markup = markup.replace('Reddit 首页导出预览', 'Reddit 信息流批次预览')
    elif data.get('acquisition_method') == 'search':
        markup = markup.replace('Reddit 首页导出预览', 'Reddit 搜索采集预览')
    elif data.get('acquisition_method') == 'community':
        markup = markup.replace('Reddit 首页导出预览', 'Reddit 社区采集预览')
    markup += (f'<p>本次保存帖子 {len(data["posts"])} 条。只下载图片预览和视频封面；'
               f'正文来自页面现有 DOM。<a href="{esc(data.get("json_name", "home.json"))}">查看 JSON</a></p>')
    (output / 'preview.html').write_text(markup + ''.join(cards) + '</html>', encoding='utf-8')
