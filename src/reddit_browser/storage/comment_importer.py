"""一级评论 JSON 事务入库，附件元数据保存在 comments.media_json，仍为五张表。"""

import json
from pathlib import Path
import re
import sqlite3

from .database import DEFAULT_DATABASE,connect_database,initialize_database
from .importer import upsert,storage_path,utc_timestamp


def validate_comment_target(database,task_id,post_id):
    if not isinstance(task_id,int) or isinstance(task_id,bool) or task_id<1:
        raise ValueError('需要已有任务的有效 task_id')
    path=Path(database).resolve()
    if not path.is_file():raise ValueError('数据库不存在，请先采集帖子入库')
    conn=sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)
    try:
        if not conn.execute('SELECT 1 FROM tasks WHERE id=?',(task_id,)).fetchone():
            raise ValueError(f'任务 {task_id} 不存在')
        if not conn.execute('SELECT 1 FROM task_posts WHERE task_id=? AND post_id=?',(task_id,post_id)).fetchone():
            raise ValueError('帖子尚未关联该任务，请先采集帖子入库')
    finally:conn.close()
    return path


def normalized_media(comment,source,previous):
    items=comment.get('media')
    if items is None:
        items=[{'position':i,'media_type':'image','preview_url':url,'original_url':url}
               for i,url in enumerate(comment.get('media_urls',[]))]
    if not isinstance(items,list):raise ValueError('media 必须是列表')
    old=json.loads(previous['media_json']) if previous else []
    if not items:return old  # 本次缺失不删除已保存的附件。
    old_by_position={m['position']:m for m in old}
    result=[]
    for position,media in enumerate(items):
        item=dict(media)
        item['position']=position
        item.setdefault('media_type','image')
        item['local_path']=storage_path(item.get('local_path'),source.parent)
        item.pop('local_url',None)
        item.setdefault('download_status','not_requested')
        prior=old_by_position.get(position)
        identity=('media_type','original_url','preview_url','animation_url')
        if (prior and prior.get('download_status')=='downloaded' and item['download_status']!='downloaded'
                and all(prior.get(k)==item.get(k) for k in identity)):
            for key in ('local_path','download_status','download_url','content_type','bytes','final_url'):
                if key in prior:item[key]=prior[key]
            item.pop('error',None)
        result.append(item)
    return result


def import_root_comments(json_path,database=DEFAULT_DATABASE,task_id=1):
    source=Path(json_path).resolve()
    data=json.loads(source.read_text(encoding='utf-8'))
    post_id=data.get('post_id')
    if not isinstance(post_id,str) or not re.fullmatch(r't3_[a-z0-9]+',post_id):
        raise ValueError('无效帖子 ID')
    if not isinstance(data.get('comments'),list):raise ValueError('缺少评论列表')
    run_path=source.parent/'run.json'
    run=json.loads(run_path.read_text(encoding='utf-8')) if run_path.is_file() else {}
    captured_value=data.get('captured_at') or run.get('root_captured_at') or run.get('started_at')
    if not captured_value:raise ValueError('缺少采集时间，不能把旧评论误标为刚采集')
    captured=utc_timestamp(captured_value)
    complete=data.get('root_stage_complete') is True
    if not data['comments'] and not complete:raise ValueError('未取得评论且一级采集尚未完成，不入库')
    path=validate_comment_target(database,task_id,post_id)
    initialize_database(path)
    conn=connect_database(path)
    counts={'comments_inserted':0,'comments_updated':0,'older_comments_skipped':0,'media_items_processed':0}
    latest=captured
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not conn.execute('SELECT 1 FROM task_posts WHERE task_id=? AND post_id=?',(task_id,post_id)).fetchone():
                raise ValueError('任务与帖子的关联已不存在')
            seen=set()
            for comment in data['comments']:
                ident=comment.get('id')
                if not isinstance(ident,str) or not re.fullmatch(r't1_[a-z0-9]+',ident) or ident=='t1_deleted':
                    raise ValueError('无效评论 ID 或删除占位')
                if comment.get('post_id')!=post_id or comment.get('depth')!=0 or comment.get('parent_comment_id') is not None:
                    raise ValueError('只允许同一帖子的一级评论入库')
                if ident in seen:raise ValueError('来源 JSON 含重复评论 ID')
                seen.add(ident)
                stamp=utc_timestamp(comment.get('captured_at') or captured)
                latest=max(latest,stamp)
                row=conn.execute('SELECT * FROM comments WHERE id=?',(ident,)).fetchone()
                old=dict(row) if row else None
                if old and old['post_id']!=post_id:raise ValueError('评论 ID 与已有帖子关联冲突')
                if old and stamp<old['last_seen_at']:
                    conn.execute('UPDATE comments SET first_seen_at=min(first_seen_at,?) WHERE id=?',(stamp,ident))
                    counts['older_comments_skipped']+=1
                    continue
                media=normalized_media(comment,source,old)
                values={'id':ident,'post_id':post_id,'parent_comment_id':None,'depth':0,
                    'author_id':comment.get('author_id'),'author_name':comment.get('author'),
                    'body':comment.get('body'),'score':comment.get('score'),'url':comment.get('url'),
                    'created_at':utc_timestamp(comment['created_at']) if comment.get('created_at') else None,
                    'content_type':comment.get('content_type'),'media_json':json.dumps(media,ensure_ascii=False),
                    'source_json_path':storage_path(str(source),source.parent),
                    'first_seen_at':min(stamp,old['first_seen_at']) if old else stamp,'last_seen_at':stamp}
                if old:values={k:old.get(k) if v is None else v for k,v in values.items()}
                upsert(conn,'comments',values,('id',))
                counts['comments_updated' if old else 'comments_inserted']+=1
                counts['media_items_processed']+=len(media)
            post=dict(conn.execute('SELECT * FROM posts WHERE id=?',(post_id,)).fetchone())
            if post['root_comments_collected_at'] is None or latest>=post['root_comments_collected_at']:
                conn.execute('''UPDATE posts SET root_comments_status=?,root_comments_collected_at=?,
                    comments_status=CASE WHEN comments_status='complete' THEN 'complete' ELSE 'partial' END WHERE id=?''',
                    ('complete' if complete else 'partial',latest,post_id))
            state=dict(conn.execute('SELECT root_comments_status,comments_status FROM posts WHERE id=?',(post_id,)).fetchone())
            task=dict(conn.execute('SELECT id,name FROM tasks WHERE id=?',(task_id,)).fetchone())
        return {'database':str(path),'post_id':post_id,'task_id':task['id'],'task_name':task['name'],
                'source_json':str(source),**counts,**state}
    finally:conn.close()
