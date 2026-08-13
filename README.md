# HeartBeat 像素桌宠

一个常驻桌面右下角的像素小猫：它不是“你问一句它答一句”的机器人，而是会**自己醒过来**——周期性运行各个内容源插件，有值得说的事就主动弹气泡找你聊天。

## 特性

- 像素动画桌宠：待机眨眼、说话张嘴，透明置顶窗口，可拖动
- 自主循环：默认每 10 分钟主动思考一次，间隔可在设置里改
- 内容源插件化：天气、RSS 新闻、每日一言都是插件，`plugins/` 目录加一个 `.py` 就是新内容源
- 两种大脑：配置 OpenAI 兼容 API Key 走 LLM 自主判断；不配 Key 走插件规则（天冷提醒、下雨带伞、新头条播报、随机分享一言）
- 界面配置：右键桌宠 → 设置，名字/性格底色/说话方式/间隔/API/每个内容源全部可视化编辑
- Agent 化大脑：长期记忆、想法日记、主动关心、作息节律、发言冷却
- 完整聊天窗口：气泡式多轮对话、时间戳、思考中状态、聊天记录持久化
- 统计面板：LLM 调用/token/缓存率、内容采集量、聊天与自主行为、在线时长
- SQLite 存储 + 向量检索：记忆/聊天/状态/统计全部入库，sqlite-vec 做语义检索
- 本地 RAG：Hugging Face 小模型（约 95MB，ONNX 推理，无需 PyTorch）
- 多皮肤系统：橘猫/蓝猫/兔子/熊猫/幽灵/女生/男生，设置里实时预览一键切换
- 角色设定：每款皮肤自带完整人设（身份/性格/说话方式/示例台词），切换即用；自我认知也可单独自定义，联动可选
- 丰富动作：待机眨眼、说话、开心跳跃、思考、睡觉（Zzz）、挥手
- PySide6 现代界面：页签式设置、圆角卡片、流式聊天气泡、固定保存栏
- 搜索能力：网页（多引擎回退）/ 新闻 / 热点 / 股票 / 天气 / 百科 / 学术，免费接口无需 Key
- 自主调用搜索：LLM 模式支持工具调用，规则模式也会自己选话题主动搜索
- 可打包 exe：`build.bat` 一键构建，双击即用

## 项目结构

- `main.py` / `cli.py` —— 入口（GUI / 命令行）
- `core.py` `db.py` `rag.py` `search.py` `tools.py` `agent.py` —— 核心逻辑（配置/HTTP/数据库/向量/搜索/工具/Agent；`agent.py` 是兼容 shim，实体在 `brain/`）
- `kernel/` —— 最小内核：引导与数据迁移（boot）、插件管理（module）、权限分级与 Shell 工具（permission/toolsafety）、运行时线程池与看门狗（runtime）、事件总线（eventbus）、异步向量队列（embedqueue）
- `brain/` —— 智能层：Agent 控制流（agent.py + agent_chat.py + agent_think.py 混入）、记忆（memory.py）、规则决策（planner.py）、技能元数据（skills.py）、编码协作（coding_agent.py）
- `gui/` —— 皮肤、主题与桌宠/聊天/设置/搜索窗口
- `plugins/` —— 内容源插件（天气/RSS/行情/百科等）
- `tests/` —— 单元测试（`python -m tests.test_xxx`）
- `assets/` —— 图标与界面截图
- `build_mac.sh` / `build.bat` / `build-release.command` / `HeartBeat.spec` —— 打包脚本

## 运行

源码方式（Python 3.10+，需要 PySide6）：

```powershell
py -3.12 -m pip install -r requirements.txt
py -3.12 main.py
```

打包方式（无需装 Python）：

```
dist\HeartBeat.exe
```

macOS 打包（Apple Silicon / Intel 均可）：

```bash
./build_mac.sh        # 产出 ~/HeartBeat-mac/dist/HeartBeat.app
```

或者在 Finder 中**双击项目根目录的 `build-release.command`**，会自动打开终端完成 Release 构建（构建日志：`~/HeartBeat-mac/build-release.log`），完成后在 Finder 中定位产物。

> ⚠️ 本项目若放在 OneDrive/iCloud 同步目录，打包产物会被云同步破坏（Qt framework 的 symlink 被清空，app 启动即退）。
> `build_mac.sh` 已自动把虚拟环境和构建产物输出到 OneDrive 之外（`~/HeartBeat-mac/`），并在构建后自动校验 Qt 库完整性和做启动冒烟测试。

**用户数据目录**：所有用户数据（`config.json` 设置、`heartbeat.db` 聊天/记忆数据库、`models/` 嵌入模型缓存）都保存在用户数据目录，重编译 / 升级 / 重新打包不会丢失：

- macOS：`~/Library/Application Support/HeartBeat/`
- Windows：`%APPDATA%/HeartBeat/`
- Linux：`$XDG_DATA_HOME/HeartBeat` 或 `~/.local/share/HeartBeat/`

首次启动会自动把旧位置（旧版 app 内 / 源码目录）的配置、数据库和模型缓存迁移过去，无需手动处理。

## 命令行模式（CLI）

不开 GUI 直接测试/使用核心功能（源码方式）：

```powershell
py -3.12 main.py --cli config
py -3.12 main.py --cli collect
py -3.12 main.py --cli chat "你好"
py -3.12 main.py --cli tick
py -3.12 main.py --cli search "人工智能" web
py -3.12 main.py --cli skin list
py -3.12 main.py --cli embed "测试向量"
py -3.12 main.py --cli selfcheck
```

所有命令都支持 `--config <配置文件>` 指定要用的配置。

## 交互

- 左键点击小猫：打开聊天输入框
- 拖动：移动小猫位置
- 右键：主动思考一下 / 打声招呼 / 设置 / 退出
- 窗口右下角小字：当前状态（待机中 / 思考中 / 刚看了什么）
- **macOS 状态栏常驻**：app 不在程序坞显示图标，常驻顶部菜单栏（小猫图标）——左键点击显示/隐藏桌宠，右键菜单：显示/隐藏、主动思考一下、设置、搜索、退出。macOS 27 上 Qt 托盘点击必崩（QTBUG-147449，Qt 官方未修复），故状态栏用 PyObjC 原生 NSStatusItem 实现（不依赖 Qt 托盘）

## 自主性怎么工作

```
定时器（默认 10 分钟）
  → 后台运行所有启用的内容源插件（天气/新闻/一言…）
  → Agent 结合记忆、时间、心情做判断（需要时可用工具搜索/查看文件）：有值得说的才说话，没有就保持安静（还会私下记想法）
  → 弹出气泡 + 说话动画 + 状态更新
```

- **LLM 模式**：在设置里填 `API 地址 / API Key / 模型`，支持任何 OpenAI 兼容接口。模型结合所有内容源的信息自主决定说什么，或回复 `SILENT` 保持安静。
- **规则模式**：Key 留空时自动使用。每个插件可以提供自己的 `suggest()` 规则，例如天气插件负责极端天气提醒，新闻插件保证同一条头条只说一次。
- **主动行动**：主动思考时可以调用工具（搜索 + 私有沙盒），默认最多 10 轮，可在“设置 → 基本 → 主动思考工具轮数”调整；沙盒里的写文件和 bash 命令不弹确认，危险命令仍会被拒绝。

### Agent 的“自己的想法”

- **长期记忆**：你聊天时说的重要事情会被记住（如“我叫小明”“我喜欢喝咖啡”“明天要开会”），LLM 模式由模型自己判断并写 `[FACT]`，规则模式用关键词提取，存在 `heartbeat.db` 的 `memory` 表
- **想法日记**：每次主动思考即使不说话，也可能把当天看到的东西记成一条想法（`[THINK]` / 规则模式随机记）
- **主动关心**：你之前说过要考试/开会/面试/加班，过一段时间它会主动问进展
- **作息节律**：深夜（默认 23:00–7:00）进入睡眠、不主动思考；心情会随天气和时间变化（晴→开心、下雨→有点蔫、深夜→困了）
- **体力驱动**：主动思考和说话由每日体力（LLM 调用次数）和当前情绪决定，不设固定发言间隔
- 所有动态数据（记忆/状态/聊天/统计）都存 SQLite（见下文“数据与记忆”），重装不丢

### 聊天窗口

- 左键点击桌宠打开完整聊天窗口
- 用户消息右侧蓝色气泡，桌宠左侧白色气泡，带时间戳
- 等待回复时显示“正在思考…”
- **流式输出**：LLM 回复逐字显示在气泡里（SSE），桌宠同时切换说话动作；接口不支持流式时自动回退为一次性输出
- 设置里可以关闭流式（“流式输出回复”开关）
- Enter 发送、Shift+Enter 换行
- 聊天记录持久化，重启后还能看到之前聊了什么；可一键清空

### 统计信息

设置窗口的“统计”页签会展示：

- **今日 LLM 用量**：调用次数、错误数、输入/输出 token、缓存 token、缓存率（兼容 OpenAI `cached_tokens` 和 Anthropic `cache_read_input_tokens`）
- **内容采集**：每个插件成功/失败次数、条目数、字符量、缓存命中率（内容与上次相同时算命中，例如新闻没更新）
- **行为统计**：聊天消息数、主动说话次数、想法条数、记住的事实条数、主动思考次数、在线时长
- **近 7 天走势**：每天模型调用、总 token、对话条数、主动思考次数、信息条数
- 可刷新、可一键清空；数据存在 `stats.json`
- 聊天窗口顶部也会显示今日简况：模型调用 / 信息条数 / 想法条数

### 数据与记忆（SQLite + RAG）

所有动态数据都写入 `heartbeat.db`（SQLite，WAL 模式），不再用散落的 JSON 文件：

- `memory`：事实、想法（长期记忆）
- `chat_messages`：完整聊天记录
- `agent_state`：心情、问候日期、冷却时间等状态
- `stats_daily` / `stats_collectors`：按天统计
- `content_hashes`：内容缓存命中标记
- `memory_vec` / `chat_vec`：sqlite-vec 向量表，配合 `memory` / `chat_messages` 做语义检索

向量检索：

- 嵌入模型默认 `BAAI/bge-small-zh-v1.5`（512 维，中文效果好，ONNX 约 95MB）
- 打包机已预下载（`models/fastembed/`，HF 缓存布局）时，`build.bat` 会把它打进 `_internal\models\fastembed`，用户首启自动注入 `%APPDATA%\HeartBeat\models\`，无需联网；未预下载则首次使用时自动从 Hugging Face 下载
- 模型未下载、下载失败或未启用时自动降级为“最近记忆”，不影响聊天
- 每次记住事实/想法、收到用户消息时自动生成向量入库；旧数据在设置保存后自动补索引
- 聊天和自主思考时，会用当前问题做向量检索，把相关记忆带进上下文

依赖：`fastembed`（ONNX 推理）、`sqlite_vec`（向量扩展），见 `requirements.txt`。

### 皮肤与动作

设置 → “外观”页签可以预览并切换皮肤（点击立即生效）：

- 小橘猫 / 小蓝猫 / 小兔 / 小熊猫 / 小幽灵 / 女生（棕发、发饰、粉裙） / 男生（短发、衬衫领带）
- 每款皮肤 6 组动作：待机眨眼、说话张嘴、开心跳跃、思考、睡觉（带 Zzz 飘动）、挥手
- **每款皮肤自带完整人设**（身份/性格/说话方式/示例台词）：勾选“切换皮肤时同步人设”后，切换即换一套开箱即用的角色，无需手动修改设置；你自定义过的性格/说话方式不会被皮肤覆盖
- 深夜自动睡觉，聊天时思考，主动说话时开心，右键“打声招呼”时会用角色语气回应

皮肤结构在 [skins.py](skins.py)：一款皮肤 = 底座像素 + 部件（耳朵/眼睛/嘴/爪/Zzz）+ 调色板 + 静态覆盖。新增皮肤只需往 `SKINS` 字典加一项，无需重画所有帧。

### 角色设定

“宠物”不一定认知自己是动物：

- 设置 → 基本设置里有“自我认知（角色）”字段，例如“女生”“小橘猫”“我的小管家”，LLM 和规则聊天都会按这个角色说话
- 每款皮肤自带默认角色（女生/男生/小橘猫/小兔……），勾选“切换皮肤时同步人设”即自动应用
- 设置 → 外观页有“切换皮肤时同步角色设定”开关：勾选时换皮肤会同步角色，取消勾选可以保留你自定义的角色

## 写一个内容源插件

在 `plugins/` 目录新建一个 `.py` 文件，例如 `plugins/stock.py`：

```python
"""股票内容源示例。"""

META = {"name": "stock", "label": "股票", "default_enabled": True}

SETTINGS = [
    {"key": "code", "label": "股票代码", "type": "text"},
    {"key": "max_change", "label": "涨跌幅提醒阈值", "type": "number", "default": 3},
]

def collect(settings):
    # 返回条目列表；每条 {"title": 标题, "text": 正文}
    # 可以附带任意自定义字段，例如 {"data": {"change": 5.2}}
    return [{"title": "股票", "text": "600519 当前 1700 元，涨 5.2%", "data": {"change": 5.2}}]

def suggest(settings, entries, state):
    # 可选：规则模式下的自主发言，返回 str 或 None
    change = entries[0].get("data", {}).get("change", 0)
    if change >= float(settings.get("max_change", 3)):
        return "你关注的股票今天涨了不少！"
    return None
```

- `META`：插件名和界面显示名
- `SETTINGS`：界面配置项，目前支持 `text` / `number` / `list` 三种类型
- `collect()`：必填，返回内容条目
- `suggest()`：可选，规则模式自主发言
- 常用工具：`core.http_text(url)`、`core.http_json(url)`、`core.parse_rss(xml)`
- 单个插件失败只标记错误，不影响其他内容源

## 打包 exe

双击 `build.bat`，或手动执行：

```powershell
py -3.12 -m pip install -r requirements.txt pyinstaller pillow
py -3.12 -m PyInstaller --noconfirm --clean --workpath "$env:LOCALAPPDATA\Temp\HeartBeat-build" --distpath "dist" HeartBeat.spec
```

产物在 `dist\HeartBeat\HeartBeat.exe`（目录版，已内置 PySide6、ONNX 运行时和 sqlite-vec）。内置插件会被打进 `_internal\plugins`；exe 旁边的 `plugins\` 目录优先级更高，可随时加新插件。首次启用向量记忆时会自动下载模型。

### 界面

基于 PySide6（Qt）的现代浅色主题：

- 设置窗口：页签式布局（基本设置 / 内容源 / 外观 / 统计），底部固定保存/取消栏，不会再有按钮被截掉的问题
- 聊天窗口：圆角气泡、时间戳、思考中状态、流式逐字输出
- 桌宠窗口：透明置顶 + QPainter 像素动画 + 右键菜单

### 搜索

桌宠右键 → “搜索…”打开搜索窗口，支持七类：

- 综合：网页搜索，依次尝试 Bing / DuckDuckGo（HTML 轻量版）（标题/摘要/链接，双击或点按钮在浏览器打开）
- 新闻：Bing 新闻 RSS，失败自动回退 Google News RSS
- 热点：Google News 中文头条
- 股票：腾讯行情（A 股/港股）+ 新浪兜底（含美股），支持代码或名称/拼音，例如 `600519`、`sh600519`、`hk00700`、`AAPL`、`长电科技`、`gzmt`
- 天气：wttr.in 中文天气，失败自动回退 Open-Meteo
- 百科：中文维基百科词条
- 学术：arXiv 论文搜索

聊天里也可以直接搜：输入“搜索 人工智能”“新闻 芯片”“股票 600519”“股票长电科技”“天气 上海”，桌宠会把结果直接回给你。

### 桌宠自主调用搜索

桌宠不只会念 RSS 和天气，它有自己的“搜索工具”：

- **LLM 模式**：通过 OpenAI 兼容 tool calling，桌宠可以在思考时自主决定调用 `web_search` / `news_search` / `stock_quote` / `weather` / `wiki_search` / `arxiv_search`，最多连续调用 4 轮；接口不支持工具时自动退回普通模式
- **规则模式**：没有 Key 时，桌宠会按概率自己选话题（你的兴趣爱好、新闻头条、热门话题）主动搜索，发现有意思的内容就告诉你
- 每次工具调用都会记入统计（`tool_calls`），设置里可以关掉“允许桌宠自主调用搜索工具”

### 聊天里调用工具 & Shell 工具

聊天时 LLM 同样可以使用全部工具（搜索 + bash），直接说“帮我查一下今天 AI 的新闻”“看看这个目录里有什么文件”即可；接口不支持工具时自动回退普通聊天。

### 下载与安装技能包

桌宠还内置受控的下载/安装通道（不经过 shell，curl/wget 仍被禁用）：

- **下载**：说“下载 https://…/xxx.zip”，桌宠会先把下载地址弹窗给你确认（60 秒超时自动拒绝），确认后保存到 `<用户数据目录>/downloads/`。仅支持 http/https、大小上限 200MB，并阻断内网/元数据地址。
- **安装**：说“把下载的 xxx.zip 装上”，确认后解压到 `<用户数据目录>/skills/`。只允许安装下载目录里的 zip，解压带 zip-slip / 符号链接 / 膨胀上限防护，目标目录已存在时自动备份为 `.old`。
- **安装即生效**：装好后桌宠聊天的上下文会自动带上技能清单（仅 SKILL.md 的 name/description 元数据，外部文本不进入提示词），它会知道“装了知乎技能”，需要细节时自己 `cat` 技能文档细读——获得新能力不需要改代码，装个技能包就行。
- **技能初始化**：内核提供 `skill_status` / `skill_setup` / `skill_auth` 三个 ctx 原语（不直接暴露给 LLM），CLI 用 `--cli skill status/setup/auth` 操作。初始化/认证会先经你确认，Secret 不回显。
- 主动思考触发时下载/安装会被直接拒绝（写操作必须用户确认）；`off` / `readonly` 档位不可用。

### 编码协作（绑定文件夹的会话即编码模式）

桌宠只有一个 Agent：会话绑定了项目目录时，该会话里的所有发言都按编码任务处理（读代码、改文件、跑构建与测试）；没绑目录的会话正常聊天。不需要关键词识别，也不需要手动切换模式：

- 点击聊天窗右上角 **📁 目录** 选择一个文件夹（或设置 → 基本 → 编码项目目录），会新建/切换到绑定该目录的会话；
- 在这个会话里发“写一个 html 快速排序页面”“把背景改成蓝色”“继续”等任何话，都会在该目录里执行；
- 切到未绑定目录的会话就回到普通聊天，互不干扰；
- 写文件/执行命令前会弹窗经你确认；长命令自动放后台执行并轮询结果。

CLI 方式：`python main.py --cli coding "编写一个 html 快速排序实例" --project-dir /path/to/dir --mode full`（`--mode` 可选 off/readonly/confirm/full）。

- **技能包**：`python main.py --cli skill download <zip 地址>` 下载到 `<数据目录>/downloads`，`--cli skill install <zip 路径>` 安装到 `<数据目录>/skills`，`--cli skill list` 查看已发现技能；初始化执行 `--cli skill status <技能名>` / `--cli skill setup <技能名>`，认证用 `--cli skill auth <技能名>`（Access Secret 从 stdin 传入，不回显）。编译版用 `HeartBeat.app/Contents/MacOS/HeartBeat --cli …` 同样可用。

**Shell 工具（`bash`）**：桌宠可以在你授权后执行本机命令，安全性分 4 档（设置 → 基本 → Shell 工具）：

| 档位 | 只读命令（ls/cat/date/git status 等） | 写操作（rm/mv/编辑文件等） |
|---|---|---|
| 关闭 off | 不可用 | 不可用 |
| 只读 readonly | 自动执行 | 拒绝 |
| 写操作需确认 confirm（默认） | 自动执行 | 弹窗确认（60 秒不点自动拒绝） |
| 全部自动 full | 自动执行 | 自动执行（风险自负） |

硬性安全边界（任何档位生效）：不使用 shell 解析（杜绝管道/重定向/命令注入）、15 秒超时、输出截断 4KB；禁止 sudo/提权、网络下载（curl/wget）、进程管理（kill）、任意代码解释器（python/bash/node）、包安装等命令；密钥与隐私文件路径（`~/.ssh`、`id_rsa`、`.aws`、`.env`、`config.json`、`heartbeat.db` 等）直接拒绝；**桌宠自主思考时写操作一律拒绝**（用户不在场，不做无人确认的写操作）。所有执行记录（含被拒绝的）都会写入审计日志。

搜索实现在 [search.py](search.py)，全部免费接口、无需 API Key；Shell 工具与安全策略在 [tools.py](tools.py)。

## 测试

```bash
# 全量回归（21 个套件，无 GUI/无网络依赖，需项目 .venv）
for t in tests/test_*.py; do
  python -m "${t%.py}" || break
done
# GUI 集成套件需 offscreen 平台（macOS 无显示器环境）
QT_QPA_PLATFORM=offscreen HB_NO_MAC_TRAY=1 python -m tests.test_app_integration
```

单套件：`python -m tests.test_coding`（编码协作工具链）、`python -m tests.test_memory_correction`（记忆纠错）、`python -m tests.test_tools`（Shell 工具安全分级）等。Windows 用 `py -3.12 -m tests.test_xxx`。

## 下一步可以加

- 更多插件：日历、邮件、股票、待办、系统状态
- 插件支持定时类型（如每天固定时间提醒）
- 心情/睡眠系统：晚上打盹、连续下雨低落
- 像素动画扩展：走路、跳跃、自定义精灵
- 声音和系统托盘

## 许可证

- 本项目源码以 **MIT License** 发布，详见 [LICENSE](LICENSE)。
- 项目使用了 PySide6（LGPL-3.0）、fastembed（Apache-2.0）、sqlite-vec（MIT）、onnxruntime（MIT）、PyObjC（MIT）等第三方组件，完整声明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。其中 PySide6 按 LGPL-3.0 动态链接方式使用，打包产物保留 Qt 库文件可替换重链接。
- fastembed 首次启动会从 HuggingFace 下载 embedding 模型，模型文件受各自模型卡许可约束（多为 Apache-2.0/MIT），与本项目 MIT 许可相互独立。
