BEGIN IMMEDIATE;
CREATE TABLE task_posts_v5 (
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    post_id TEXT NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    acquisition_method TEXT NOT NULL
        CHECK (acquisition_method IN ('home_snapshot', 'single_post', 'feed_batch', 'search', 'community')),
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (task_id, post_id)
);
INSERT INTO task_posts_v5 SELECT * FROM task_posts;
DROP TABLE task_posts;
ALTER TABLE task_posts_v5 RENAME TO task_posts;
CREATE INDEX idx_task_posts_post ON task_posts(post_id);
PRAGMA user_version = 5;
COMMIT;
