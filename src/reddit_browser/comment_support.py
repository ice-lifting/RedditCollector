"""评论 HTML 解析、浏览器内请求与附件下载。"""

from html.parser import HTMLParser
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import parse_qsl, urljoin, urlsplit
from urllib.request import ProxyHandler, build_opener

from DrissionPage import Chromium, ChromiumOptions
from reddit_browser.media import download_media

VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}


def largest_image(attrs):
    candidates=[]
    for source in ('srcset','data-lazy-srcset'):
        for part in re.split(r',\s*(?=https?://)',attrs.get(source,'')):
            pieces=part.strip().rsplit(None,1)
            if len(pieces)==2 and re.fullmatch(r'\d+(?:\.\d+)?[wx]',pieces[1]):
                candidates.append((float(pieces[1][:-1]),pieces[0]))
    return max(candidates,key=lambda c:c[0])[1] if candidates else attrs.get('src') or attrs.get('data-lazy-src')
FETCH = r'''
return (async()=>{
const request=JSON.parse(arguments[0]),expectedPath=arguments[1];
const url=new URL(request.url,location.href);
if(location.origin!=='https://www.reddit.com'||url.origin!==location.origin||url.pathname!==expectedPath)
    throw Error('Unexpected origin or comment endpoint');
const timeoutMs=Number(arguments[2])||25000;
const controller=new AbortController();const timer=setTimeout(()=>controller.abort(),timeoutMs);
try{
    const options={method:'POST',credentials:'same-origin',redirect:'error',signal:controller.signal};
    if(request.inputs.length) options.body=new URLSearchParams(request.inputs);
    const response=await fetch(url.href,options);
    const contentType=response.headers.get('content-type')||'';
    if(!response.ok||!contentType.includes('html'))
        return {status:response.status,content_type:contentType,html:null};
    const html=await response.text();
    if(html.length>8*1024*1024)throw Error('Response exceeds 8 MiB character limit');
    return {status:response.status,content_type:contentType,html};
}catch(error){
    return {status:0,content_type:'',html:null,
        error_kind:error.name==='AbortError'?'request_timeout':'network_error',
        error_message:String(error.message||error).slice(0,300)};
}finally{clearTimeout(timer);}
})();
'''


def post_identity(url):
    parts=urlsplit(url)
    match=re.fullmatch(r'/r/([A-Za-z0-9_]+)/comments/([A-Za-z0-9]+)(?:/[^/]+)?/?',parts.path)
    if parts.scheme!='https' or parts.netloc!='www.reddit.com' or not match:
        raise ValueError('需要 https://www.reddit.com/r/社区/comments/帖子ID/ 形式的链接')
    subreddit,ident=match.groups()
    return subreddit,'t3_'+ident,f'/svc/shreddit/more-comments/r/{subreddit}/t3_{ident}'


def request_key(request):
    parts=urlsplit(request['url'])
    # 位置编号只是渲染信息；避免相同 cursor 仅改变编号就反复请求。
    query=sorted((k,v) for k,v in parse_qsl(parts.query,keep_blank_values=True) if k!='commentPositionOffset')
    identity=[parts.path,query,sorted(request['inputs'])]
    return hashlib.sha256(json.dumps(identity,ensure_ascii=False).encode()).hexdigest()


class CommentParser(HTMLParser):
    """解析真实 HTML 标签，包括流式 template；不运行响应脚本。"""
    def __init__(self,post_id,endpoint,parent_id=None):
        super().__init__(convert_charrefs=True)
        self.post_id,self.endpoint,self.parent_id=post_id,endpoint,parent_id
        self.comments=[]
        self.tombstones=[]
        self.requests=[]
        self.frames=[]

    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        previous=self.frames[-1] if self.frames else {}
        frame={**previous,'tag':tag}
        if tag=='shreddit-comment':
            frame['body']=None
            frame['partial']=None
            if attrs.get('thingid')=='t1_deleted' and attrs.get('postid') in (None,self.post_id):
                self.tombstones.append({'reason':'deleted_placeholder','depth':attrs.get('depth'),
                    'position':attrs.get('comment-position'),'parent_comment_id':self.parent_id})
                frame['comment']=None
            elif attrs.get('postid')==self.post_id and attrs.get('thingid'):
                parent=previous.get('comment')
                depth=int(attrs.get('depth','0'))
                comment={'id':attrs['thingid'],'post_id':self.post_id,'author':attrs.get('author'),
                    'body':'','score':int(attrs['score']) if re.fullmatch(r'-?\d+',attrs.get('score','')) else None,
                    'created_at':attrs.get('created'),'depth':depth,
                    'parent_comment_id':parent['id'] if parent else self.parent_id if depth>0 else None,
                    'url':urljoin('https://www.reddit.com',attrs.get('permalink','')),
                    'content_type':attrs.get('content-type'),'media_urls':[],'media':[],'_text':[],'_assets':[]}
                self.comments.append(comment)
                frame['comment']=comment
                frame['asset']=None
            else:frame['comment']=None
        if tag=='faceplate-partial':
            frame['partial']=None
            url=urljoin('https://www.reddit.com',attrs.get('src',''))
            parts=urlsplit(url)
            if (parts.scheme=='https' and parts.netloc=='www.reddit.com' and parts.path==self.endpoint
                    and attrs.get('method','GET').upper()=='POST'):
                request={'url':url,'inputs':[],
                    'parent_comment_id':frame['comment']['id'] if frame.get('comment') else self.parent_id}
                self.requests.append(request)
                frame['partial']=request
        if attrs.get('slot')=='comment' and frame.get('comment'):
            frame['body']=frame['comment']
        if tag in ('script','style'):
            frame['ignored']=True
        if tag=='input' and frame.get('partial') is not None and attrs.get('name') and 'disabled' not in attrs:
            frame['partial']['inputs'].append([attrs['name'],attrs.get('value','')])
        if frame.get('body') is not None:
            owner=frame['body']
            if tag=='figure':
                frame['asset']={'original_url':None,'preview_url':None}
                owner['_assets'].append(frame['asset'])
            if tag in ('p','div','li','br','blockquote'):frame['body']['_text'].append('\n')
            if tag=='a':
                href=attrs.get('href','')
                parts=urlsplit(href)
                if parts.scheme=='https' and parts.path.lower().endswith(('.gif','.png','.jpg','.jpeg','.webp')):
                    if frame.get('asset') is None:
                        frame['asset']={'original_url':None,'preview_url':None}
                        owner['_assets'].append(frame['asset'])
                    frame['asset']['original_url']=href
            if tag=='img':
                src=largest_image(attrs)
                if src and urlsplit(src).scheme in ('http','https'):
                    asset=frame.get('asset')
                    if asset is None:
                        asset={'original_url':None,'preview_url':None}
                        owner['_assets'].append(asset)
                    asset['preview_url']=src
                    asset['alt_text']=attrs.get('alt')
            if tag=='shreddit-player' and 'gif' in attrs and attrs.get('src'):
                if frame.get('asset') is None:
                    frame['asset']={'original_url':None,'preview_url':None}
                    owner['_assets'].append(frame['asset'])
                frame['asset']['animation_url']=attrs['src']
        if tag not in VOID:self.frames.append(frame)

    def handle_endtag(self,tag):
        for index in range(len(self.frames)-1,-1,-1):
            if self.frames[index]['tag']==tag:
                if tag in ('p','div','li','blockquote') and self.frames[index].get('body') is not None:
                    self.frames[index]['body']['_text'].append('\n')
                del self.frames[index:]
                break

    def handle_startendtag(self,tag,attrs):
        self.handle_starttag(tag,attrs)
        if tag not in VOID:self.handle_endtag(tag)

    def handle_data(self,data):
        if self.frames:
            frame=self.frames[-1]
            if frame.get('body') is not None and not frame.get('ignored'):
                frame['body']['_text'].append(data)

    def finish(self,html):
        self.feed(html)
        self.close()
        for comment in self.comments:
            comment['body']=re.sub(r'\n[ \t]*\n+', '\n\n',''.join(comment.pop('_text'))).strip()
            assets={}
            for asset in comment.pop('_assets'):
                url=asset['preview_url'] or asset['original_url']
                if not url and not asset.get('animation_url'):continue
                key=url or asset['animation_url']
                if key in assets:continue
                assets[key]={'position':len(assets),'media_type':'image',**asset,'preview_url':url}
            comment['media']=list(assets.values())
            comment['media_urls']=[m['preview_url'] for m in comment['media'] if m['preview_url']]
        return self


def download_comment_media(comments,output,workers=4):
    """复用首页图片下载器；GIF 来源可下载为 GIF/WebP，MP4 地址仅保留。"""
    output=Path(output).resolve()
    pending=[]
    fields=('download_url','local_path','local_url','download_status','content_type','bytes','final_url')
    cache={}
    for comment in comments:
        if 'media' not in comment:
            comment['media']=[{'position':i,'media_type':'image','original_url':url,'preview_url':url}
                              for i,url in enumerate(comment.get('media_urls',[]))]
        for media in comment['media']:
            path=(output/media['local_path']).resolve() if media.get('local_path') else None
            if (media.get('download_status')=='downloaded' and media.get('preview_url') and path and path.is_relative_to(output) and path.is_file()):
                cache[media.get('preview_url')]={key:media[key] for key in fields if key in media}
    for comment in comments:
        items=[]
        for media in comment['media']:
            url=media.get('preview_url')
            if url in cache:media.update(cache[url])
            else:items.append(media)
        if items:pending.append({'id':comment['id'],'media':items})
    if pending:
        (output/'media').mkdir(exist_ok=True)
        download_media(pending,output,workers)
    counts={}
    for comment in comments:
        for media in comment['media']:
            status=media.get('download_status','missing_url')
            counts[status]=counts.get(status,0)+1
    return counts


def existing_tab(address,post_url):
    opener=build_opener(ProxyHandler({}))
    with opener.open(f'http://{address}/json/list',timeout=5) as response:
        targets=json.load(response)
    pages=[p for p in targets if p.get('type')=='page' and urlsplit(p.get('url','')).netloc=='www.reddit.com']
    if not pages:raise RuntimeError('需要现有 Chrome 中至少一个 www.reddit.com 标签页，不会新建浏览器或导航')
    target=next((p for p in pages if urlsplit(p['url']).path==urlsplit(post_url).path),pages[0])
    browser=Chromium(ChromiumOptions().set_address(address).existing_only())
    return browser.get_tab(target['id'])
