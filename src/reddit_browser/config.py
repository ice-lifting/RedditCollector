"""项目内独立存储，不共享旧项目数据库。"""
from pathlib import Path
import os
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ADDRESS = os.environ.get('REDDIT_COLLECTOR_ADDRESS', '127.0.0.1:9222')
DATA_ROOT = (PROJECT_ROOT / 'data' if (PROJECT_ROOT / 'pyproject.toml').is_file() else
             Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share')) / 'RedditCollector')
DEFAULT_DATABASE = Path(os.environ.get('REDDIT_COLLECTOR_DATABASE', DATA_ROOT / 'reddit.db')).resolve()
DEFAULT_EXPORT_ROOT = Path(os.environ.get('REDDIT_COLLECTOR_OUTPUT_ROOT', DATA_ROOT / 'captures')).resolve()
EXTRACT_SCRIPT = Path(__file__).with_name('extract_posts.js')
