"""tools_declarations：13 工具声明（web + bash/list/read/write/edit/glob/grep/todo/bg/skill/backup/note）。"""

from tools_common import _params_decl, SHELL_MODE_CONFIRM, SHELL_MODE_OFF

def _agent_declarations():
    """13 工具方案里的项目/通用工具（不含 web）。"""
    return [
        {
            "type": "function",
            "function": {
                "name": "bash",
                "description": (
                    "在主人电脑上执行 shell 命令（工作目录按 shell_workdir/project_dir 配置）。"
                    "只读命令（ls/cat/rg/git status/git diff）直接执行；"
                    "写/破坏性命令需要主人确认。遵守当前 Shell 语法。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {"type": "string", "description": "完整 shell 命令"},
                        "timeout": {"type": "integer", "description": "超时秒数，默认 15，最大 60"},
                    },
                    "required": ["command"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "list",
                "description": (
                    "列出项目目录树（目录/文件、大小），跳过 VCS/依赖/构建产物等噪音。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "项目内相对路径，默认当前根目录"},
                        "depth": {"type": "integer", "description": "递归深度，默认 2，最大 4"},
                    },
                    "required": [],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "read",
                "description": "读取项目目录内的文本文件，返回带行号内容；二进制/超大文件会跳过或截断。",
                "parameters": _params_decl("path", "项目内相对路径，如 src/main.py"),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "write",
                "description": (
                    "在项目内创建或整体覆盖一个文本文件。覆盖旧文件前会自动备份；需要主人确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "项目内相对路径"},
                        "content": {"type": "string", "description": "完整文件内容"},
                    },
                    "required": ["path", "content"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "edit",
                "description": (
                    "在项目内文件中做锚点替换：search 必须是唯一原文片段"
                    "（或多处匹配时用 expected_occurrences 指定数量）。写前自动备份；需要主人确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "项目内相对路径"},
                        "search": {"type": "string", "description": "要替换的唯一原文锚点"},
                        "replace": {"type": "string", "description": "替换后的文本"},
                        "expected_occurrences": {
                            "type": "integer",
                            "description": "可选：预期匹配次数（>1 时批量替换）",
                        },
                    },
                    "required": ["path", "search", "replace"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "glob",
                "description": "按文件名模式匹配项目内文件（支持 ** 递归，如 **/*.py）。",
                "parameters": _params_decl("pattern", "glob 模式，如 src/**/*.py"),
            },
        },
        {
            "type": "function",
            "function": {
                "name": "grep",
                "description": "在项目文件内容里按正则搜索，返回 文件:行号: 内容。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "正则表达式"},
                        "file_glob": {"type": "string", "description": "可选：限定文件名模式"},
                    },
                    "required": ["pattern"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "todo",
                "description": (
                    "管理编码任务待办清单（按项目隔离）。"
                    "action 支持 list / add / done / clear；add 需要 item，done 需要 id。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "add", "done", "clear"],
                            "description": "操作类型",
                        },
                        "item": {"type": "string", "description": "add 时的待办内容"},
                        "id": {"type": "integer", "description": "done 时的待办编号"},
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "bg",
                "description": (
                    "后台长任务：exec 启动构建/测试类命令（立即返回任务 ID），"
                    "check 轮询状态与最近输出，cancel 取消。并发最多 3 个。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["exec", "check", "cancel"],
                            "description": "操作类型",
                        },
                        "command": {"type": "string", "description": "exec 时的完整 shell 命令"},
                        "task_id": {"type": "string", "description": "check/cancel 时的任务 ID"},
                        "timeout": {"type": "integer", "description": "exec 超时秒数，默认 300，上限 1800"},
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "skill",
                "description": (
                    "技能包全生命周期：list 查看已安装；download 下载 zip；install 安装；"
                    "status/setup 检查与初始化；auth 配置认证；exec 调用技能 CLI。"
                    "下载/安装/认证/写类调用需要主人确认。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "download", "install", "status", "setup", "auth", "exec"],
                            "description": "操作类型",
                        },
                        "name": {"type": "string", "description": "技能名"},
                        "url": {"type": "string", "description": "download 时的 zip 地址"},
                        "zip_path": {"type": "string", "description": "install 时的 zip 路径"},
                        "binary": {"type": "string", "description": "auth 时的 CLI 可执行文件路径"},
                        "secret": {"type": "string", "description": "auth 时的 Access Secret"},
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "exec 时的 CLI 参数列表",
                        },
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "backup",
                "description": (
                    "编码写前备份管理：list 列出最近备份；preview 对比当前文件与备份；"
                    "restore 从备份恢复（需要主人确认）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "preview", "restore"],
                            "description": "操作类型",
                        },
                        "path": {"type": "string", "description": "项目内相对路径"},
                        "backup_id": {"type": "string", "description": "list 返回的备份 ID"},
                    },
                    "required": ["action"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "note",
                "description": (
                    "项目约定记忆（按项目隔离）：list 查看；add 记录约定（如测试命令、不要动的目录）；"
                    "clear 清空。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["list", "add", "clear"],
                            "description": "操作类型",
                        },
                        "text": {"type": "string", "description": "add 时要记住的约定"},
                    },
                    "required": ["action"],
                },
            },
        },
    ]


def _web_decl():
    return {
        "type": "function",
        "function": {
            "name": "web",
            "description": (
                "联网搜索。category 支持 web/news/stock/weather/wiki/arxiv，"
                "query 为关键词。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": ["web", "news", "stock", "weather", "wiki", "arxiv"],
                        "description": "搜索来源分类",
                    },
                    "query": {"type": "string", "description": "搜索关键词"},
                },
                "required": ["query"],
            },
        },
    }


def _sandbox_decl():
    return {
        "type": "function",
        "function": {
            "name": "sandbox",
            "description": (
                "在你的私有沙盒（用户数据目录/sandbox）里自娱自乐：list 列目录、"
                "read 读文件、write 写文件、run 执行命令。全部限制在沙盒内，"
                "不需要主人确认；危险命令会被拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "read", "write", "run"],
                        "description": "操作类型",
                    },
                    "path": {"type": "string", "description": "list/read/write 时的沙盒内相对路径"},
                    "content": {"type": "string", "description": "write 时的文件内容"},
                    "command": {"type": "string", "description": "run 时的完整 shell 命令"},
                    "timeout": {"type": "integer", "description": "run 超时秒数，默认 60，最大 300"},
                },
                "required": ["action"],
            },
        },
    }


def coding_declarations(cfg):
    """Coding 模式工具声明（与 tool_declarations 一致：13 个一级工具）。"""
    return tool_declarations(cfg)


def patrol_declarations(cfg):
    """主动思考专用工具：搜索 + 私有沙盒（不碰项目文件，不弹确认）。"""
    decls = [_web_decl()]
    if cfg.get("shell_tools_mode", SHELL_MODE_CONFIRM) != SHELL_MODE_OFF:
        decls.append(_sandbox_decl())
    return decls


def tool_declarations(cfg):
    """13 工具声明：web + bash/list/read/write/edit/glob/grep/todo/bg/skill/backup/note。

    配置了 project_dir 时给完整 13 个；未配置时只给不依赖项目的通用工具。
    off 档只保留只读搜索。
    """
    decls = [_web_decl()]
    mode = cfg.get("shell_tools_mode", SHELL_MODE_CONFIRM)
    if mode != SHELL_MODE_OFF:
        if str(cfg.get("project_dir", "") or "").strip():
            decls.extend(_agent_declarations())
        else:
            decls.extend([
                d for d in _agent_declarations()
                if d["function"]["name"] in ("bash", "todo", "note", "skill")
            ])
    return decls


