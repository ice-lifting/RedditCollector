本目录保存正式业务数据：reddit.db 为 SQLite 数据库，captures/ 为正文、媒体及 JSON 导出。

MCP 首次写入时会初始化或迁移数据库，保留已有记录。搜索和阅读使用只读连接。
工具、配置和启动方式见仓库根目录 README.md。
