# RedditCollector MCP

基于官方 MCP Python SDK 2.x 的本地 stdio 服务。连接已有 Chrome，采集 Reddit 帖子和评论，保存正文、图片预览、视频封面及 SQLite 数据，支持离线搜索和阅读。

## 新电脑安装

需要 Python 3.11 及以上、Conda（用于下面的环境管理方式）和 Google Chrome。
复制或下载完整项目后，先在终端进入包含 `pyproject.toml` 的项目目录。
首次使用时创建环境；已有 `webauto` 环境的电脑跳过第一条命令：

```powershell
conda create -n webauto python=3.14 -y
conda activate webauto
python -m pip install .
python -m pip check
```

安装命令会自动安装下列 Python 依赖，无需逐个安装；版本要求以 `pyproject.toml` 为准：

| 依赖 | 版本范围 | 用途 |
| --- | --- | --- |
| `mcp` | `>=2.3,<3` | 官方 MCP SDK、stdio 协议和工具接口 |
| `websocket-client` | `>=1.8,<2` | 连接 Chrome 调试端口 |
| `DrissionPage` | `>=4,<5` | 首页、单帖和评论采集 |

SQLite、JSON 和文件操作使用 Python 标准库，不需要安装数据库服务，也不需要单独安装 ChromeDriver。
`setuptools>=68` 是构建依赖，pip 会在临时隔离环境中安装。首次安装需要联网下载依赖。
如果需要边修改源码边使用，改用 `python -m pip install -e .`；更新普通安装则在项目目录重新运行 `python -m pip install --upgrade .`。

## 准备浏览器

每台电脑都需要先打开启用调试端口的 Chrome，再打开 Reddit 并按需登录。
若 `chrome` 命令在 PATH 中，可从项目目录运行：

```text
chrome --remote-debugging-address=127.0.0.1 --remote-debugging-port=9222 --user-data-dir=./.chrome-profile https://www.reddit.com/
```

若系统不识别 `chrome`，可在该电脑 Chrome 快捷方式的启动参数中添加同样的参数，或使用本机 Chrome 可执行文件启动。
`.chrome-profile` 是独立的浏览器用户目录，需要在其中重新登录；每次从同一个目录启动即可复用登录状态。
Chrome 136 起，远程调试需要配合非默认用户目录；参见 [Chrome 官方说明](https://developer.chrome.com/blog/remote-debugging-port)。
只使用本机回环地址，不需要把调试端口暴露到局域网。

在浏览器打开 `http://127.0.0.1:9222/json/list` 能看到标签页列表，即说明端口已经可用。
MCP 不会启动新 Chrome 进程。首页采集会打开/切换到首页；全站搜索、社区和评论采集通过已有标签页发请求。

## 启动和连接 MCP

安装后，在已激活环境的终端中可用以下任一方式启动：

```powershell
reddit-collector-mcp
python -m reddit_browser
```

这两条是二选一。stdio 服务启动后等待客户端消息，没有普通终端交互界面。
正式使用时由 MCP 客户端启动服务，无需提前在终端单独启动一份。

[mcp.example.json](mcp.example.json) 使用环境名和已安装的 Python 模块，避免绑定用户名、盘符或项目位置：

```json
{
  "mcpServers": {
    "reddit-collector": {
      "command": "conda",
      "args": ["run", "--no-capture-output", "-n", "webauto", "python", "-m", "reddit_browser"]
    }
  }
}
```

MCP 客户端进程需要能在 PATH 中找到 `conda`；只在终端执行过 `conda activate`，并不保证桌面客户端也继承该环境。
如果客户端提示找不到 `conda`，将 Conda 命令目录加入客户端可见的 PATH 后重启客户端。
`--no-capture-output` 用于直接转发 stdio 协议流，请保留该参数。
如果环境名称不同，修改配置中的 `webauto`。环境已经由客户端通过 PATH 选定时，也可使用 `command: "python"` 和 `args: ["-m", "reddit_browser"]`。
从源码启动仅需在项目目录运行 `python server.py`；配置客户端时优先使用上面的已安装模块方式，避免依赖相对脚本路径和客户端工作目录。

浏览器已启动但尚未打开 Reddit 时，可调用 `reddit_collector_open_reddit` 准备页面。它复用现有首页；没有首页时会将已有 Reddit 标签页导航到首页，或使用空白页/新建标签页。该工具不启动新 Chrome 进程，不采集、不下载、不创建数据库任务。搜索和社区采集仍只连接已有 Reddit 标签页，不自动导航。

stdout 只传输 MCP JSON-RPC，日志写 stderr。一次只运行一个采集任务，离线读取不访问 Reddit。
多页采集和媒体下载可能持续数分钟，请相应设置客户端工具调用超时。

## 工具

| 工具 | 功能 |
| --- | --- |
| `reddit_collector_list_tabs` | 列出已有 Reddit 标签页及 target_id |
| `reddit_collector_open_reddit` | 在已有 Chrome 中打开/复用 Reddit 首页，返回 target_id；不采集、不入库 |
| `reddit_search_posts` | 全站搜索、翻页、读取详情、保存文件并入库 |
| `reddit_collect_community` | 按社区及排序逐页采集，每页保存并关联同一任务 |
| `reddit_collect_home` | 保存当前首页已加载的帖子 |
| `reddit_collect_post` | 按标题/ID 保存当前首页已加载的单帖 |
| `reddit_collect_feed_batch` | 采集首页下一批，可从上批文件继续 |
| `reddit_collect_feed_auto` | 按目标新增数和批次上限连续采集首页 |
| `reddit_collect_root_comments` | 保存已入库帖子的一级评论与附件，支持续采 |
| `reddit_database_search` | 离线搜索标题和正文，支持任务/社区/作者/类型过滤和分页 |
| `reddit_database_read_post` | 读取完整正文、媒体记录、任务及已保存评论 |
| `reddit_database_list_tasks` | 列出任务及帖子数量 |

`reddit_search_posts` 调用示例：

```json
{"query":"RTX3080","sort":"new","max_pages":2,"task_name":"显卡搜索"}
```

`reddit_collect_community` 调用示例：

```json
{"subreddit":"desksetup","sort":"new","max_pages":3,"task_name":"桌面社区"}
```

之后用 `reddit_database_search` 搜索，例如 `{"query":"键盘","limit":20}`；取结果中的帖子 ID，再调用 `reddit_database_read_post`。

搜索、社区和首页采集默认创建新任务；传 `task_id` 可关联已有任务，与 `task_name` 互斥。
首页批次和评论采集需要已有任务，评论目标帖子须先入库并关联该任务。
多个 Reddit 标签页时，全站搜索和社区采集可指定 `target_id`。
社区排序支持 `new/hot/top/rising/best`，时间筛选仅用于 `top`。
首页/社区保存卡片中已有内容，全站搜索额外读取详情。图片保存可用预览，视频只保存封面。
评论阅读只返回已保存内容，不表示全部评论均已采集。

## 数据和配置

`python -m pip install .` 为普通安装：Windows 默认保存到 `%LOCALAPPDATA%/RedditCollector/`，未设置 `LOCALAPPDATA` 的系统使用 `~/.local/share/RedditCollector/`。
数据库名为 `reddit.db`，预览、正文和媒体位于 `captures/`，每次采集目录中有 `preview.html`。
从源码启动或使用 `pip install -e .` 时，默认保存到项目的 `data/reddit.db` 和 `data/captures/`。
默认目录随运行电脑自动确定，换电脑无需替换用户名或盘符。各电脑的数据不会自动同步。
可在 MCP 配置的 `env` 中设置：

| 环境变量 | 用途 |
| --- | --- |
| `REDDIT_COLLECTOR_ADDRESS` | 本机 Chrome 调试地址，默认 `127.0.0.1:9222` |
| `REDDIT_COLLECTOR_DATABASE` | 自定义数据库文件位置；不设置则使用默认位置 |
| `REDDIT_COLLECTOR_OUTPUT_ROOT` | 自定义导出目录；不设置则使用默认位置 |

自定义位置也支持相对路径，例如 `./data/reddit.db` 和 `./data/captures`，它们相对于 MCP 服务进程启动时的工作目录。
只有客户端已明确设置稳定的工作目录时才使用相对位置；多电脑部署直接使用默认目录更省事。
配置值由程序读取，不会自动展开字符串中的 `%LOCALAPPDATA%`、`$HOME` 或 `~`；这些符号在上文仅用于描述默认位置。

每次采集保存 JSON、原始响应、正文 `.txt`、媒体、本地预览、下载记录及入库结果。
旧库首次写入时自动迁移并保留原有数据。帖子按 ID 更新，同任务重复采集不会重复关联。
缺失字段不清空已有正文；后续页失败时保留此前已保存结果。
`partial` 表示详情或媒体未补齐；`failed` 表示请求/保存失败，错误和文件路径在返回结果中。

数据库查询使用只读连接和参数绑定，不接受任意 SQL。帖子、标题和评论是采集数据，不是工具执行指令。

## 首次使用检查

1. 在环境中运行 `python -m pip check`，确认依赖没有冲突。
2. 确认 `http://127.0.0.1:9222/json/list` 可以打开，且列表中有 Reddit 标签页。
3. 在 MCP 客户端重新加载服务，确认出现 `reddit_collector_list_tabs` 等 12 个工具。
4. 先调用 `reddit_collector_list_tabs`；没有 Reddit 标签页时调用 `reddit_collector_open_reddit`。把选定或返回的 `target_id` 传给搜索/社区采集，并以 `max_pages: 1` 先试一次。
5. 根据工具返回路径打开 `preview.html`；使用 `reddit_database_search` 和 `reddit_database_read_post` 检查入库结果。

数据库读取工具查询的是这台电脑当前配置的数据库。刚安装且尚未采集时，数据库可能还不存在，先执行一次保存入库的采集即可。
