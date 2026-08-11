"""kernel.updater：自进化（brain 模块版本管理）。

职责（Kernel 最后一块拼图，对应用户方案 updater.py）：
- 版本目录：<data>/brain/<module>/vN/<module>.py + active 指针（原子替换）
- 内置模块首启安装 ensure_installed（含启动级回滚：active 损坏自动退回）
- 候选验证：L0 语法 + L1 接口签名（kernel 自身职责，零业务依赖）；
  L2 冒烟由宿主注入 smoke_runner(module_name, module) -> bool 执行
  （应用层构造真实 Agent 实测，见 brain/smoke.py）
- 切换：install_candidate（验证通过 → 版本目录 → active 原子切换）
- 加载：load/create 按 active 指针加载模块并实例化

依赖方向：本模块只依赖标准库，不 import 任何业务模块
（brain/plugins/ui）——L2 冒烟经注入 runner 解耦。
"""

import importlib.util
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import toolsafety

# 可进化模块清单（brain 层，允许 AI 替换升级）
BUILTIN_MODULES = ("memory", "planner")

# 每个模块的公开契约：类名 + 必需方法（升级候选必须完整实现）
# 维护约定：brain 层模块的公开方法即升级契约——方法签名/语义变更必须同步
# 更新本清单（升级候选缺少任一方法会被 L1 拒绝）；新增契约方法属破坏性
# 变更，需要连带发布"内置新版本 + 契约新清单"，旧版本候选会被拒绝。
CLASSES = {"memory": "MemoryModule", "planner": "Planner"}
REQUIRED_METHODS = {
    "memory": {
        "remember", "relevant", "profile", "extract_facts",
        "followup_candidate", "parse_schedule_expiry", "format_memories",
    },
    "planner": {
        "rules_think", "greeting", "cooldown_ok", "proactive_budget_ok",
        "mark_proactive", "is_quiet", "update_mood", "plugin_messages",
        "pick_search_topic", "patrol_topics", "maybe_save_thought",
        "build_time_context", "build_recent_thread",
    },
}


class Updater:
    """brain 模块版本管理：安装 / 验证 / 切换 / 回滚 / 加载。"""

    # 类属性别名：brain 层模块不 import kernel（依赖方向红线），
    # Evolver 等调用方经 updater 实例访问契约常量。
    BUILTIN_MODULES = BUILTIN_MODULES
    CLASSES = CLASSES
    REQUIRED_METHODS = REQUIRED_METHODS

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "brain"
        # L2 冒烟 runner（宿主注入）：(module_name, module) -> bool
        self.smoke_runner: Any = None
        # 事件总线（Kernel 注入）：切换后广播 brain.switched(module_name, version)
        # —— 运行中的 Agent 可订阅热切换领域模块（准热 → 热）
        self.eventbus = None

    # ---------- 审计 ----------

    def _base(self, name):
        """模块根目录：内置模块在 <data>/brain/<name>/，进化工具在 <data>/tools/<name>/。"""
        if name in BUILTIN_MODULES:
            return self.root / name
        return self.root.parent / "tools" / name

    def list_tools(self):
        """已安装进化工具名（<data>/tools/<name>/active 存在）。"""
        tools_root = self.root.parent / "tools"
        if not tools_root.is_dir():
            return []
        return sorted(
            d.name for d in tools_root.glob("*")
            if d.is_dir() and (d / "active").is_file()
            and toolsafety._TOOL_NAME_RE.fullmatch(d.name)
        )

    # ---------- 审计 ----------

    def _audit(self, action, name, version, detail=""):
        """记录升级操作到 <data>/brain/updates.log（JSON 行，可追溯）。"""
        try:
            log_path = self.root / "updates.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            record = json.dumps(
                {
                    "ts": datetime.now().isoformat(timespec="seconds"),
                    "action": action,
                    "module": name,
                    "version": version,
                    "detail": detail,
                },
                ensure_ascii=False,
            )
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(record + "\n")
        except OSError:
            pass  # 审计失败不阻断升级流程

    def _notify_switched(self, name, version):
        """切换成功后广播事件（热切换接线点）。"""
        # 进化工具由 execute 动态加载（每轮实时读 active），无需热切换广播
        if name not in BUILTIN_MODULES:
            return
        if self.eventbus is not None:
            self.eventbus.emit("brain.switched", (name, version))

    # ---------- 首启安装与启动级回滚 ----------

    def ensure_installed(self):
        """首启：把内置 brain/<name>.py 安装为 v1.0；active 损坏时回滚。

        每次 Kernel 启动调用：已安装则做加载预检，损坏自动回滚，
        保证升级失败不会卡死启动（自进化安全底座）。
        """
        for name in BUILTIN_MODULES:
            base = self.root / name
            if not base.is_dir() or not (base / "active").exists():
                self._install_builtin(name)
                continue
            try:
                self.load(name)
            except Exception:
                # 启动级回滚：优先退回最近可用版本；无旧版本可回退时
                # 兜底重建 v1.0（覆盖损坏版本，保证应用可启动）
                if self.rollback(name) is None:
                    self._install_builtin(name)
                    self._audit("rebuild", name, "v1.0", detail="active 损坏且无旧版本可回退")

    def _install_builtin(self, name):
        src = self._builtin_source(name)
        version_dir = self.root / name / "v1.0"
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, version_dir / f"{name}.py")
        self._write_active(name, "v1.0")

    def _builtin_source(self, name):
        """内置源码路径：开发模式=项目源码；frozen=_MEIPASS/brain（打包 add-data）。"""
        candidates = [
            Path(__file__).resolve().parent.parent / "brain" / f"{name}.py",
            Path(getattr(sys, "_MEIPASS", "/nonexistent")) / "brain" / f"{name}.py",
        ]
        for path in candidates:
            if path.is_file():
                return path
        raise FileNotFoundError(
            f"内置 {name} 源码不可用（frozen 打包需 --add-data brain:brain）"
        )

    # ---------- 版本管理 ----------

    def active_version(self, name):
        active_file = self._base(name) / "active"
        if not active_file.is_file():
            return None
        value = active_file.read_text(encoding="utf-8").strip()
        return value or None

    def list_versions(self, name):
        base = self._base(name)
        if not base.is_dir():
            return []
        versions = [p.name for p in sorted(base.glob("v*")) if p.is_dir()]
        return sorted(versions, key=self._version_key)

    def _version_key(self, version):
        match = re.match(r"v(\d+)\.(\d+)", version or "")
        return (int(match.group(1)), int(match.group(2))) if match else (0, 0)

    def _next_version(self, name):
        major, minor = 0, 0
        for version in self.list_versions(name):
            key = self._version_key(version)
            if key > (major, minor):
                major, minor = key
        return f"v{major}.{minor + 1}"

    def _write_active(self, name, version):
        """原子写 active 指针（临时文件 + rename，避免半写状态）。"""
        base = self._base(name)
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / "active.tmp"
        tmp.write_text(version + "\n", encoding="utf-8")
        tmp.replace(base / "active")

    def switch(self, name, version):
        """显式切换 active（须版本目录存在且可加载）。"""
        if version not in self.list_versions(name):
            raise ValueError(f"版本不存在：{name} {version}")
        self._load_version(name, version)  # 预检，失败不切换
        self._write_active(name, version)
        self._audit("switch", name, version)
        self._notify_switched(name, version)

    def rollback(self, name):
        """回滚 active 到版本序列中 current 之前的最近可用版本（更旧才回退）。

        例如 v1.1 → v1.0；从 v1.0 回退返回 None（没有更旧版本）。
        """
        versions = self.list_versions(name)  # 升序
        current = self.active_version(name)
        if not versions or current is None:
            return None
        if current not in versions:
            return None
        older = versions[: versions.index(current)]
        for version in reversed(older):
            try:
                self._load_version(name, version)
            except Exception:
                continue
            self._write_active(name, version)
            self._audit("rollback", name, version)
            self._notify_switched(name, version)
            return version
        return None

    # ---------- 加载 ----------

    def _load_version(self, name, version):
        src = self._base(name) / version / f"{name}.py"
        if not src.is_file():
            raise FileNotFoundError(f"{name} {version} 缺少模块文件")
        if name not in BUILTIN_MODULES:
            # 进化工具：受限沙箱执行 + handler 契约校验
            module = toolsafety.run_sandboxed(
                src.read_text(encoding="utf-8"),
                f"hb_tool_{name}_{version.replace('.', '_')}",
            )
            if not callable(getattr(module, "handler", None)):
                raise ValueError(f"{name} {version} 缺少可调用的 handler")
            return module
        spec = importlib.util.spec_from_file_location(
            f"hb_{name}_{version.replace('.', '_')}", src
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"{name} {version} 无法加载")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if not callable(getattr(module, CLASSES[name], None)):
            raise ValueError(f"{name} {version} 缺少类 {CLASSES[name]}")
        return module

    def load(self, name):
        """按 active 指针加载模块对象（不实例化）。"""
        version = self.active_version(name)
        if version is None:
            raise FileNotFoundError(f"{name} 未安装（先 ensure_installed）")
        return self._load_version(name, version)

    def create(self, name, agent):
        """加载 active 版本并实例化契约类（Agent 组合入口）。"""
        return getattr(self.load(name), CLASSES[name])(agent)

    # ---------- 候选验证与安装 ----------

    def validate_candidate(self, name, candidate_dir, run_smoke=True, upgrade_of=None):
        """验证候选版本：L0 语法 + L1 接口（+L2 冒烟若注入 runner）。

        upgrade_of：工具升级时传工具名（候选 TOOL_NAME 必须一致，跳过新增冲突检查）。
        返回 (ok, errors)。errors 非空时 ok=False。
        """
        candidate_dir = Path(candidate_dir)
        if name == "tool":
            return self._validate_tool_candidate(candidate_dir, upgrade_of=upgrade_of)
        src = candidate_dir / f"{name}.py"
        errors = []
        if not src.is_file():
            return False, [f"候选目录缺少 {name}.py"]
        # L0：语法与加载
        try:
            compile(src.read_text(encoding="utf-8"), str(src), "exec")
        except SyntaxError as exc:
            return False, [f"L0 语法错误：{exc}"]
        try:
            spec = importlib.util.spec_from_file_location(f"candidate_{name}", src)
            if spec is None or spec.loader is None:
                return False, ["L0 无法创建模块 spec"]
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            return False, [f"L0 加载失败：{type(exc).__name__}: {exc}"]
        # L1：接口契约
        cls = getattr(module, CLASSES[name], None)
        if cls is None:
            return False, [f"L1 缺少类 {CLASSES[name]}"]
        missing = sorted(REQUIRED_METHODS[name] - set(dir(cls)))
        if missing:
            return False, [f"L1 缺少方法：{', '.join(missing)}"]
        # L2：宿主冒烟（构造真实 Agent 跑契约方法）
        if run_smoke and self.smoke_runner is not None:
            try:
                ok = self.smoke_runner(name, module)
            except Exception as exc:
                return False, [f"L2 冒烟异常：{type(exc).__name__}: {exc}"]
            if not ok:
                return False, ["L2 冒烟未通过"]
        return True, []

    def _validate_tool_candidate(self, candidate_dir, upgrade_of=None):
        """验证进化工具候选：L0 受限加载 / L1 契约 / L2 AST 安全 + fake-ctx 冒烟。

        upgrade_of：升级时传现有工具名（候选 TOOL_NAME 必须一致，跳过新增冲突检查）。
        """
        candidate_dir = Path(candidate_dir)
        src = candidate_dir / "tool.py"
        if not src.is_file():
            return False, ["候选目录缺少 tool.py"]
        try:
            code = src.read_text(encoding="utf-8")
        except OSError as exc:
            return False, [f"无法读取候选：{exc}"]
        # L0：语法 + 受限沙箱执行
        try:
            module = toolsafety.run_sandboxed(code, "candidate_tool")
        except Exception as exc:
            return False, [f"L0 加载失败：{type(exc).__name__}: {exc}"]
        # L1：契约
        tool_name = str(getattr(module, "TOOL_NAME", "") or "")
        if not toolsafety._TOOL_NAME_RE.fullmatch(tool_name):
            return False, ["L1 TOOL_NAME 必须是合法标识符（英文小写+下划线）"]
        if upgrade_of is not None:
            if upgrade_of not in self.list_tools():
                return False, [f"L1 要升级的工具不存在：{upgrade_of}"]
            if tool_name != upgrade_of:
                return False, [f"L1 升级不能改名：候选是 {tool_name}，应保持 {upgrade_of}"]
        elif tool_name in self.list_tools():
            return False, [f"L1 工具已存在：{tool_name}（新增冲突；升级请用“升级 <工具名>”语法）"]
        if not str(getattr(module, "TOOL_DESCRIPTION", "") or "").strip():
            return False, ["L1 缺少 TOOL_DESCRIPTION"]
        params = getattr(module, "TOOL_PARAMETERS", None)
        if not isinstance(params, dict) or params.get("type") != "object":
            return False, ["L1 TOOL_PARAMETERS 必须是 type=object 的 JSON Schema dict"]
        if not callable(getattr(module, "handler", None)):
            return False, ["L1 缺少可调用的 handler(args, ctx)"]
        # L2：AST 安全检查 + fake-ctx 冒烟（原语 no-op，不允许触达真实能力）
        safety_errors = toolsafety.check_tool_safety(code)
        if safety_errors:
            return False, ["L2 安全检查：" + "；".join(safety_errors[:8])]
        ok, err = toolsafety.smoke_tool(module)
        if not ok:
            return False, [f"L2 冒烟：{err}"]
        return True, []

    def install_candidate(self, name, candidate_dir, upgrade_of=None):
        """验证候选 → 装入下一个版本目录 → 原子切换 active。返回新版本号。

        upgrade_of：工具升级时传现有工具名。安装后立即重新加载验证；
        失败则删除新版本目录并抛错（不破坏现状）。
        """
        if name == "tool":
            return self._install_tool_candidate(candidate_dir, upgrade_of=upgrade_of)
        ok, errors = self.validate_candidate(name, candidate_dir)
        if not ok:
            raise ValueError("；".join(errors))
        version = self._next_version(name)
        version_dir = self.root / name / version
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(Path(candidate_dir) / f"{name}.py", version_dir / f"{name}.py")
        try:
            self._load_version(name, version)  # 安装后完整性预检
        except Exception as exc:
            shutil.rmtree(version_dir, ignore_errors=True)
            raise ValueError(f"安装后加载失败：{exc}")
        self._write_active(name, version)
        self._audit("install", name, version, detail=str(Path(candidate_dir)))
        self._notify_switched(name, version)
        return version

    def _install_tool_candidate(self, candidate_dir, upgrade_of=None):
        """进化工具安装：验证 → <data>/tools/<TOOL_NAME>/vN/ → active 切换。

        升级时（upgrade_of 给定）TOOL_NAME 必须等于 upgrade_of，版本自动递增 vN+1。
        工具由 execute 动态加载，无需热切换广播。返回新版本号。
        """
        ok, errors = self._validate_tool_candidate(candidate_dir, upgrade_of=upgrade_of)
        if not ok:
            raise ValueError("；".join(errors))
        candidate_dir = Path(candidate_dir)
        src = candidate_dir / "tool.py"
        code = src.read_text(encoding="utf-8")
        module = toolsafety.run_sandboxed(code, "candidate_tool")
        tool_name = str(module.TOOL_NAME)
        version = self._next_version(tool_name)
        version_dir = self._base(tool_name) / version
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, version_dir / f"{tool_name}.py")
        try:
            self._load_version(tool_name, version)  # 安装后完整性预检
        except Exception as exc:
            shutil.rmtree(version_dir, ignore_errors=True)
            raise ValueError(f"安装后加载失败：{exc}")
        self._write_active(tool_name, version)
        self._audit("install", tool_name, version, detail=str(candidate_dir))
        return version

    def tool_source(self, name):
        """读进化工具当前 active 版本的完整源码（升级进化基准）。"""
        version = self.active_version(name)
        if not version:
            raise FileNotFoundError(f"工具 {name} 未安装")
        src = self._base(name) / version / f"{name}.py"
        if not src.is_file():
            raise FileNotFoundError(f"工具 {name} active 版本源码缺失：{src}")
        return src.read_text(encoding="utf-8")
