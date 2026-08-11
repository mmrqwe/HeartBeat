"""brain.evolver：自我进化生成器（LLM 产出候选代码 → 走 updater 验证安装流水线）。

架构决策（MVP，经架构评审）：
- 只允许 updater.BUILTIN_MODULES（memory/planner）进化，agent.py 核心控制流锁定不可自改；
- 完整文件生成（非 diff）：读当前 active 源码 + 用户需求 → LLM 生成 vN+1 完整文件
  → 落盘候选目录 → 复用 updater.validate_candidate + install_candidate；
- 安全边界：AST import 白名单 + 危险内置调用检查（L0 前）+ token 预算 + 重试上限；
- 触发方式：用户显式指令 + tool_confirm 确认（Agent 层），或 CLI evolve 命令。

依赖方向：本模块只依赖 stdlib + 宿主注入（core.Brain 实例 / kernel.updater.Updater 实例），
不 import kernel（brain 层红线）——契约常量经 updater 实例的类属性别名访问。
"""

import ast
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

# 允许的 import 根：stdlib 安全子集 + 项目内模块（planner/memory 现有用法全覆盖）。
# 进化候选只允许在这里面 import；os/sys/subprocess/socket/网络/文件写入一律拒绝。
ALLOWED_IMPORTS = {
    # stdlib 安全子集
    "json", "re", "datetime", "time", "typing", "dataclasses", "pathlib",
    "collections", "math", "random", "calendar", "logging", "functools",
    "itertools", "enum", "string", "statistics", "bisect", "heapq",
    # 项目内模块（现有 brain 模块的既有依赖）
    "search", "core", "db", "brain", "gui", "kernel", "plugins", "rag", "tools",
}
# 危险内置调用（AST Call 检查：Name 与 Attribute 形式）。
# getattr 是反射属性访问（planner 现有代码用于插件方法探测），不执行任意代码，放行。
FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input",
    "breakpoint", "globals", "locals",
}
# 生成 token 预算上限（超过视为失败，防白花钱）
MAX_GEN_TOKENS = 8000
# 验证失败后的重试次数（总计最多 MAX_ATTEMPTS+1 次生成，带错误反馈）
MAX_ATTEMPTS = 2

_CODE_FENCE = re.compile(r"`{3,}(?:python|py|python3)?\s*(.*?)`{3,}", re.DOTALL)
_REQUIREMENT_RE = re.compile(r"[：:，,。！!？?\s]+$")
# 工具升级语法："升级 <工具名>：需求"（工具名必须已安装）
_UPGRADE_RE = re.compile(r"^升级\s*([A-Za-z_][A-Za-z0-9_]*)[：:，,]?\s*(.*)$")


class Evolver:
    """自我进化流水线：生成 → 安全检查 → updater 验证 → 原子安装。"""

    def __init__(self, brain, updater: Any):
        self.brain = brain          # core.Brain（LLM 客户端，宿主注入）
        self.updater = updater      # kernel.updater.Updater（版本流水线，宿主注入）
        self.candidate_root = Path(updater.root).parent / "evolve"

    # ---------- 当前源码 ----------

    def current_source(self, name):
        """读当前 active 版本的完整源码（进化基准 = 正在运行的实现）。"""
        version = self.updater.active_version(name)
        if not version:
            raise ValueError(f"{name} 尚未安装（先 ensure_installed）")
        src = self.updater.root / name / version / f"{name}.py"
        if not src.is_file():
            raise FileNotFoundError(f"active 版本源码缺失：{src}")
        return src.read_text(encoding="utf-8")

    # ---------- Prompt 构建 ----------

    def _build_prompt(self, name, requirement, current_src):
        contract = sorted(self.updater.REQUIRED_METHODS[name])
        cls = self.updater.CLASSES[name]
        allowed = ", ".join(sorted(ALLOWED_IMPORTS))
        system = (
            "你是桌宠「小跳」的自我进化引擎。主人要求你升级自己的某个领域模块。"
            "你会拿到该模块的当前完整源码和升级需求，请输出升级后的【完整模块源码】。\n"
            "硬性要求：\n"
            f"1. 类名必须是 {cls}，构造函数签名 __init__(self, agent) 保持不变；\n"
            f"2. 必须完整保留以下公开方法（可改进实现，不可删改签名）：{', '.join(contract)}\n"
            "3. 只允许 import 白名单内的模块：" + allowed +
            "（禁止 os/sys/subprocess/shutil/socket/ctypes/pickle/importlib/网络/文件写入）；\n"
            "4. 通过 self.agent 组合访问共享状态（self.agent.state/db/cfg/clock/memory/"
            "brain/planner/embedder/stats），不要 import agent；\n"
            "5. 保持原有方法的默认行为与返回值类型，不得破坏其他模块对你的调用；"
            "模块级常量（如现有规则表）尽量保留；\n"
            "6. 只输出纯 Python 源码（可用 ```python 围栏包裹），不要任何解释文字。"
        )
        user = (
            f"模块：{name}\n升级需求：{requirement}\n\n"
            f"当前完整源码（{len(current_src)} 字符）：\n```python\n{current_src}\n```"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    # ---------- 生成与落盘 ----------

    @staticmethod
    def _extract_code(text):
        """从模型输出提取代码：优先最长围栏（3+ 反引号，兼容 ```` 嵌套）；
        无围栏时先试整段（可能是带 docstring/import 的完整模块）；语法失败
        则从最早出现的模块声明（TOOL_NAME/def handler/class/import）截到末尾。"""
        if not text:
            return ""
        fences = _CODE_FENCE.findall(text)
        if fences:
            code = max(fences, key=len).strip()
            if code:
                return code
        candidate = text.strip()
        if candidate:
            try:
                ast.parse(candidate)
                return candidate
            except SyntaxError:
                pass
        marks = [m for m in ("TOOL_NAME", "TOOL_DESCRIPTION", "def handler", "class ", "import ") if m in text]
        if marks:
            idx = min(text.find(m) for m in marks)
            return text[idx:].strip()
        return ""

    def generate_candidate(self, name, requirement, feedback=""):
        """LLM 生成候选源码并落盘。返回候选目录 Path。"""
        messages = self._build_prompt(name, requirement, self.current_source(name))
        if feedback:
            messages.append({
                "role": "user",
                "content": "上次生成的代码验证失败：\n" + feedback +
                           "\n请修正后重新输出完整源码（保持契约方法与安全要求不变）。",
            })
        raw = self.brain.complete(messages, max_tokens=MAX_GEN_TOKENS) or ""
        code = self._extract_code(raw)
        if not code:
            raise ValueError("模型未返回代码")
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        candidate_dir = self.candidate_root / f"{name}_{stamp}"
        candidate_dir.mkdir(parents=True, exist_ok=True)
        (candidate_dir / f"{name}.py").write_text(code, encoding="utf-8")
        return candidate_dir

    # ---------- 安全检查（L0 前） ----------

    def check_safety(self, code):
        """AST 静态检查：import 白名单 + 危险调用。返回错误列表（空=通过）。"""
        errors = []
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return [f"语法错误：{exc}"]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root not in ALLOWED_IMPORTS:
                        errors.append(f"禁止 import：{alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root not in ALLOWED_IMPORTS:
                    errors.append(f"禁止 import：{node.module}")
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                    errors.append(f"禁止调用：{func.id}()")
                elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CALLS:
                    errors.append(f"禁止调用：{func.attr}()")
        return errors

    # ---------- 完整流水线 ----------

    def evolve(self, name, requirement, on_status=None):
        """生成 → 安全检查 → updater 验证（L0/L1/L2）→ 原子安装。

        返回新版本号（如 v1.1）。验证失败带错误反馈重试（最多 MAX_ATTEMPTS 次），
        仍失败抛 ValueError（不安装任何东西）。安装后 updater 广播 brain.switched，
        宿主（main.py 热切换订阅）会自动重载领域模块。
        """
        def status(msg):
            if on_status is not None:
                on_status(msg)

        if name not in self.updater.BUILTIN_MODULES and name != "tool":
            raise ValueError(
                f"不可进化：{name}（仅允许 memory/planner 与新增工具 tool）"
            )
        if name == "tool":
            return self._evolve_tool(requirement, on_status=on_status)
        requirement = _REQUIREMENT_RE.sub("", requirement).strip()
        if len(requirement) < 4:
            raise ValueError("需求描述太短，请具体说明要加什么功能")
        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 2):
            status(f"生成候选代码（第 {attempt} 次尝试）…")
            candidate_dir = self.generate_candidate(name, requirement, feedback)
            try:
                status("安全检查…")
                errors = self.check_safety(
                    (candidate_dir / f"{name}.py").read_text(encoding="utf-8")
                )
                if errors:
                    feedback = "；".join(errors)
                    continue
                status("运行验证（语法/接口契约/冒烟）…")
                ok, v_errors = self.updater.validate_candidate(name, candidate_dir)
                if ok:
                    status("验证通过，安装中…")
                    version = self.updater.install_candidate(name, candidate_dir)
                    return version
                feedback = "；".join(v_errors)
            finally:
                shutil.rmtree(candidate_dir, ignore_errors=True)
        raise ValueError(
            f"生成 {MAX_ATTEMPTS + 1} 次均未通过验证，已放弃：{feedback}"
        )

    # ---------- 进化工具（能力层：<data>/tools/，受限沙箱） ----------

    def _tool_prompt(self, requirement, upgrade_of=None, existing=(), base_src=None):
        prims = (
            "ctx.web_search(query, limit=6) 网页搜索\n"
            "ctx.news_search(query, limit=6) 新闻搜索\n"
            "ctx.stock_quote(code) 股票行情\n"
            "ctx.weather(city) 天气\n"
            "ctx.wiki_search(query, limit=5) 百科\n"
            "ctx.arxiv_search(query, limit=5) 学术搜索\n"
            "ctx.http_text(url, timeout=10) 只读 HTTP 文本\n"
            "ctx.http_json(url, timeout=10) 只读 HTTP JSON\n"
            "ctx.run_bash(command) 执行 shell 命令（写操作需主人确认）\n"
            "ctx.download_file(url, filename=None) 下载文件（需主人确认）\n"
            "ctx.install_skill(zip_path) 安装技能包（需主人确认）\n"
            "ctx.now 当前时间（datetime）"
        )
        system = (
            "你是桌宠「小跳」的自我进化引擎。主人要求你为自己编写一个新的工具模块。\n"
            "工具模块是纯 Python 文件，契约：\n"
            "TOOL_NAME = \"英文小写加下划线的新工具名\"（不得与现有工具重名）\n"
            "TOOL_DESCRIPTION = \"一句话说明工具用途（LLM 据此决定何时调用它）\"\n"
            "TOOL_PARAMETERS = {JSON Schema dict，type=\"object\"，properties 描述每个参数，"
            "required 列必填参数}\n"
            "def handler(args, ctx) -> str: ...（校验参数后返回中文结果文本；"
            "参数缺失时返回错误提示）\n\n"
            "安全硬性要求：\n"
            "1. 只允许 import：json re datetime time typing dataclasses collections math random "
            "string itertools functools enum bisect heapq statistics decimal fractions\n"
            "2. 禁止 import 其他任何模块（尤其 os/sys/subprocess/socket/shutil/ctypes/pickle/"
            "importlib/requests/项目模块）；\n"
            "3. 禁止调用 eval/exec/compile/open/input/getattr/setattr/type/super/globals/"
            "locals/breakpoint/__import__；\n"
            "4. 禁止访问任何下划线开头的属性（__class__/__globals__/__builtins__ 等）；\n"
            "5. 网络、文件、命令只能通过 ctx 原语，可用原语：\n" + prims + "\n"
            "6. handler 必须稳健：异常用 try/except 包裹后返回中文错误文本；\n"
            "7. 只输出纯 Python 源码（```python 围栏包裹），不要任何解释文字。"
        )
        if upgrade_of:
            system += (
                f"\n你要升级现有工具「{upgrade_of}」：必须保留 TOOL_NAME 不变，"
                "可改进 TOOL_DESCRIPTION / TOOL_PARAMETERS / handler；"
                "完整输出升级后的模块源码。"
            )
        elif existing:
            system += "\n现有工具名（新工具不得重名）：" + ", ".join(existing)
        user = f"新工具需求：{requirement}"
        if upgrade_of and base_src:
            user += (
                f"\n\n工具「{upgrade_of}」当前完整源码（升级基准）：\n"
                f"```python\n{base_src}\n```"
            )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def _evolve_tool(self, requirement, on_status=None):
        """生成/升级工具模块 → updater 验证（受限加载/契约/AST 安全/冒烟）→ 安装。

        升级语法："升级 <工具名>：需求"（以现有 active 源码为基准，版本 vN+1）；
        否则视为新增。返回 "工具名@版本号"（如 ping_check@v0.2）。
        """
        def status(msg):
            if on_status is not None:
                on_status(msg)

        requirement = _REQUIREMENT_RE.sub("", requirement).strip()
        existing = self.updater.list_tools()
        upgrade_of = None
        m = _UPGRADE_RE.match(requirement)  # "升级 <工具名>：需求"（CLI 直连形式）
        if m:
            candidate_name = m.group(1)
            if candidate_name not in existing:
                raise ValueError(
                    f"没有已安装的工具「{candidate_name}」——新增工具请直接描述需求，不要带“升级”"
                )
            upgrade_of = candidate_name
            requirement = m.group(2).strip()
            if not requirement:
                raise ValueError("请说明升级点，例如：升级 ping_check：支持超时参数")
        else:
            # 聊天路径：agent 清洗时“升级”已被剥掉，剩余 "<工具名>：需求" 开头
            for tn in existing:
                if requirement.startswith(tn):
                    rest = requirement[len(tn):]
                    if rest and rest[0] in "：:，, ":
                        upgrade_of = tn
                        requirement = rest[1:].strip()
                        break
            if upgrade_of is not None and not requirement:
                raise ValueError("请说明升级点，例如：升级 ping_check：支持超时参数")
        if len(requirement) < 4:
            raise ValueError("需求描述太短，请具体说明要加什么工具")
        base_src = self.updater.tool_source(upgrade_of) if upgrade_of else None
        feedback = ""
        for attempt in range(1, MAX_ATTEMPTS + 2):
            status(f"生成工具候选代码（第 {attempt} 次尝试）…")
            messages = self._tool_prompt(
                requirement, upgrade_of=upgrade_of, existing=existing, base_src=base_src
            )
            if feedback:
                messages.append({
                    "role": "user",
                    "content": "上次生成的代码验证失败：\n" + feedback +
                               "\n请修正后重新输出完整源码（保持契约与安全要求不变）。",
                })
            raw = self.brain.complete(messages, max_tokens=MAX_GEN_TOKENS, timeout=180) or ""
            code = self._extract_code(raw)
            if not code:
                preview = raw.strip()[:120].replace("\n", " ")
                feedback = "模型未返回可识别的代码"
                if preview:
                    feedback += f"（输出开头：{preview}）"
                continue
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            candidate_dir = self.candidate_root / f"tool_{stamp}"
            candidate_dir.mkdir(parents=True, exist_ok=True)
            (candidate_dir / "tool.py").write_text(code, encoding="utf-8")
            try:
                status("运行验证（受限加载/契约/AST 安全/冒烟）…")
                ok, v_errors = self.updater.validate_candidate(
                    "tool", candidate_dir, upgrade_of=upgrade_of
                )
                if ok:
                    status("验证通过，安装中…")
                    if upgrade_of:
                        version = self.updater.install_candidate(
                            "tool", candidate_dir, upgrade_of=upgrade_of
                        )
                        return f"{upgrade_of}@{version}"
                    before = set(self.updater.list_tools())
                    version = self.updater.install_candidate("tool", candidate_dir)
                    new_names = set(self.updater.list_tools()) - before
                    tool_name = sorted(new_names)[0] if new_names else "?"
                    return f"{tool_name}@{version}"
                feedback = "；".join(v_errors)
            finally:
                shutil.rmtree(candidate_dir, ignore_errors=True)
        raise ValueError(f"生成 {MAX_ATTEMPTS + 1} 次均未通过验证，已放弃：{feedback}")
