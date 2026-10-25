"""Reddit 本地 SQLite 存储：建表、连接，以及 importer 中的 JSON 入库入口。"""

from pathlib import Path
import sqlite3


from ..config import DEFAULT_DATABASE
SCHEMA_VERSION = 5


def connect_database(path=DEFAULT_DATABASE):
    """每个连接均启用外键，调用方负责提交和关闭连接。"""
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def initialize_database(path=DEFAULT_DATABASE):
    """幂等建表，或迁移已知旧版本；保留原有业务数据。"""
    database = Path(path).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_database(database)
    try:
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4, SCHEMA_VERSION):
            raise RuntimeError(f'数据库版本 {version} 不兼容，当前版本为 {SCHEMA_VERSION}')
        if version == 1:
            migration = Path(__file__).parent / 'migrations' / '002_feed_batch.sql'
            conn.executescript(migration.read_text(encoding='utf-8'))
            version = 2
        if version == 2:
            migration = Path(__file__).parent / 'migrations' / '003_root_comments.sql'
            conn.executescript(migration.read_text(encoding='utf-8'))
            version = 3
        if version == 3:
            migration = Path(__file__).parent / 'migrations' / '004_search.sql'
            conn.executescript(migration.read_text(encoding='utf-8'))
            version = 4
        if version == 4:
            migration = Path(__file__).parent / 'migrations' / '005_community.sql'
            conn.executescript(migration.read_text(encoding='utf-8'))
        schema = Path(__file__).with_name('schema.sql').read_text(encoding='utf-8')
        conn.executescript(schema)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return database
