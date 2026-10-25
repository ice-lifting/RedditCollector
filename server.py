"""源码目录启动入口：无需安装项目，不依赖客户端工作目录。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent / 'src'))
from reddit_browser.mcp_server import main

if __name__ == '__main__':
    main()
