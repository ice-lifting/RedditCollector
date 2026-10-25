-- 时间使用 UTC ISO 8601 文本；缺失或尚未采集的内容用 NULL。
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'completed')),
    created_at TEXT NOT NULL,
    completed_at TEXT
);

-- 所有任务共用的帖子总表，保存最近一次采集的数据。
-- 原帖可能尚未采集，所以 original_post_id 不要求外键。
CREATE TABLE IF NOT EXISTS posts (
    id TEXT PRIMARY KEY NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    post_type TEXT NOT NULL,
    url TEXT NOT NULL,
    content_url TEXT,
    thumbnail_url TEXT,
    subreddit_id TEXT,
    subreddit_name TEXT,
    author_id TEXT,
    author_name TEXT,
    created_at TEXT,
    score INTEGER,
    comment_count INTEGER,
    upvote_ratio REAL,
    original_post_id TEXT,
    original_post_url TEXT,
    parse_status TEXT NOT NULL DEFAULT 'parsed'
        CHECK (parse_status IN ('parsed', 'unsupported_type')),
    comments_status TEXT NOT NULL DEFAULT 'not_collected'
        CHECK (comments_status IN ('not_collected', 'partial', 'complete')),
    root_comments_status TEXT NOT NULL DEFAULT 'not_collected' CHECK (root_comments_status IN ('not_collected', 'partial', 'complete')),
    root_comments_collected_at TEXT,
    body_local_path TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_posts (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    acquisition_method TEXT NOT NULL
        CHECK (acquisition_method IN ('home_snapshot', 'single_post', 'feed_batch', 'search', 'community')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (task_id, post_id)
);

-- 一张图片一行；相册用 position 保存顺序。视频一行，下载文件为封面。
CREATE TABLE IF NOT EXISTS post_media (
    id INTEGER PRIMARY KEY,
    post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    media_type TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
    original_url TEXT,
    preview_url TEXT,
    alt_text TEXT,
    stream_url TEXT,
    poster_url TEXT,
    preview_video_url TEXT,
    caption_url TEXT,
    download_url TEXT,
    local_path TEXT,
    download_status TEXT NOT NULL DEFAULT 'not_requested'
        CHECK (download_status IN ('not_requested', 'pending', 'downloaded',
            'failed', 'missing_url', 'skipped_after_block')),
    download_error TEXT,
    downloaded_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE (post_id, position)
);

-- 父评论可能未被加载，因此只记录 parent_comment_id，不设自关联外键。
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY NOT NULL,
    post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    parent_comment_id TEXT,
    depth INTEGER CHECK (depth IS NULL OR depth >= 0),
    content_type TEXT,
    media_json TEXT NOT NULL DEFAULT '[]',
    source_json_path TEXT,
    author_id TEXT,
    author_name TEXT,
    body TEXT,
    score INTEGER,
    url TEXT,
    created_at TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_task_posts_post ON task_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_posts_subreddit ON posts(subreddit_name);
CREATE INDEX IF NOT EXISTS idx_posts_type ON posts(post_type);
CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent ON comments(parent_comment_id);

PRAGMA user_version = 5;
COMMIT;
