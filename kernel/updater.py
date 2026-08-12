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
BUILTIN_MODULES = ("memory", "planner", "brain")

# 包模块：版本单元是目录（多文件 + __init__.py + _contract.py），
# 整体版本化（active 指向整个包版本，禁止包内混版本）。
# 阶段2（2026-08-12）：brain 包化后 agent 控制流也进入进化域。
# 阶段5（P2 拆包）：brain 包只含控制流三件套 + skills；memory/planner
# 回到独立版本单元（<data>/brain/<name>/vN/）——三级进化粒度：
# Policy 级升级只动独立目录，不再整体重建 Brain 包。
PACKAGE_MODULES = ("brain",)
# 包内子模块固定布局：文件名 -> 公开类名（内核强制，候选不可改——
# 防止候选把类藏到任意文件里逃避契约检查）。
# 只声明契约类 agent.py（Agent）；agent_chat/agent_think 是混入、
# skills 是纯函数——缺失时 agent.py 的相对导入在 L0 包加载即失败。
PACKAGE_LAYOUT = {
    "brain": (
        ("agent.py", "Agent"),
    ),
}

# 每个模块的公开契约：类名 + 必需方法（升级候选必须完整实现）
# 维护约定：brain 层模块的公开方法即升级契约——方法签名/语义变更必须同步
# 更新本清单（升级候选缺少任一方法会被 L1 拒绝）；新增契约方法属破坏性
# 变更，需要连带发布"内置新版本 + 契约新清单"，旧版本候选会被拒绝。
# 契约分层（阶段6）：本清单是"包内核心契约"（带实质逻辑的方法）。coding_task
# 等"薄包装 + 策略在宿主侧"的宿主委托方法**不进本清单**——由宿主工厂
# agent._inject_host_delegates 在实例出口注入（旧快照无则补、有则尊重），
# 宿主新增委托能力不触发包版本刷新，老用户进化包不受干扰。新增方法时先
# 判断层级：实质逻辑 → REQUIRED_METHODS；薄包装转发宿主 → 注入层。
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
    # brain 包内 agent.py 的 Agent 类（控制流契约 = 宿主调用面：
    # main.py/runtime/UI 直接调用的公开方法；阶段3 冒烟再扩展）
    "Agent": {
        "chat", "think", "live", "greet", "reload", "reload_brain_modules",
        "append_chat", "clear_chat_history", "reindex_async", "patrol_topics",
    },
    "MemoryModule": {
        "remember", "relevant", "profile", "extract_facts",
        "followup_candidate", "parse_schedule_expiry", "format_memories",
    },
    "Planner": {
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
    PACKAGE_MODULES = PACKAGE_MODULES
    PACKAGE_LAYOUT = PACKAGE_LAYOUT

    def __init__(self, data_dir):
        self.root = Path(data_dir) / "brain"
        # L2 冒烟 runner（宿主注入）：(module_name, module) -> bool
        self.smoke_runner: Any = None
        # 事件总线（Kernel 注入）：切换后广播 brain.switched(module_name, version)
        # —— 运行中的 Agent 可订阅热切换领域模块（准热 → 热）
        self.eventbus = None

    # ---------- 审计 ----------

    def _base(self, name):
        """模块根目录：内置/包模块在 <data>/brain/<name>/，进化工具在 <data>/tools/<name>/。"""
        if name in BUILTIN_MODULES or name in PACKAGE_MODULES:
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
        # P2 拆包迁移（旧布局 brain 包内含 policy → 独立版本单元），
        # 幂等：新布局包（无 policy 文件）直接跳过。
        self._migrate_legacy_brain_package()
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

    # ---------- 旧布局迁移（P2 拆包） ----------

    def _migrate_legacy_brain_package(self):
        """旧布局（brain 包内含 memory.py/planner.py）→ 新布局（包只含控制流）。

        拆包（P2 三级进化粒度）：policy 独立版本化。规则：
        - 包内 policy 文件存在 = 旧布局信号（幂等：迁移后无残留即跳过）；
        - 导出包内 memory/planner 到独立目录 v<包版本>（以包内为准——
          包是 active，是用户实际在用的实现）；
        - 独立目录已有更高版本 → 不覆盖，active 指向更高版本（尊重用户进化）；
        - 重写包 __init__.py/_contract.py 为新布局 + 删除包内 policy 文件；
        - 任何一步失败：保留原样 + 审计，不阻断启动（旧布局仍可加载）。
        """
        if "brain" not in PACKAGE_MODULES:
            return
        version = self.active_version("brain")
        if not version:
            return
        pkg_dir = self._base("brain") / version
        if not pkg_dir.is_dir():
            return
        legacy = [n for n in ("memory", "planner")
                  if (pkg_dir / f"{n}.py").is_file()]
        if not legacy:
            return
        # 旧布局包内 agent.py 用相对导入（from .memory import ...）访问 policy——
        # 拆包删除 policy 文件后无法加载。检测到即把控制流四件套整体回退
        # 宿主源码（旧版控制流与 policy 静态纠缠，无法独立存活）。
        try:
            agent_src = (pkg_dir / "agent.py").read_text(encoding="utf-8")
            if re.search(r"from \.memory import|from \.planner import", agent_src):
                src_dir = self._builtin_package_dir("brain")
                for fname in self._PACKAGE_BUNDLE:
                    shutil.copy2(src_dir / fname, pkg_dir / fname)
                self._audit("migrate_control", "brain", version,
                            detail="旧 agent.py 相对导入 policy，控制流回退宿主源码")
        except OSError as exc:
            self._audit("migrate_failed", "brain", version,
                        detail=f"控制流回退失败：{exc}")
            return  # 保留原样，不继续
        for name in legacy:
            src = pkg_dir / f"{name}.py"
            try:
                versions = self.list_versions(name)
                highest = versions[-1] if versions else None
                if highest and self._version_key(highest) > self._version_key(version):
                    # 独立目录已有更高版本：包内副本弃用（用户侧可能已进化）
                    self._audit("migrate_keep", name, highest,
                                detail="独立目录已有更高版本，包内副本弃用")
                else:
                    target = self._base(name) / version
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, target / f"{name}.py")
                    self._audit("migrate", name, version,
                                detail="从 brain 包导出（P2 拆包）")
                self._write_active(
                    name,
                    highest if highest and self._version_key(highest)
                    > self._version_key(version) else version,
                )
                src.unlink(missing_ok=True)
            except OSError as exc:
                self._audit("migrate_failed", name, version, detail=str(exc))
                return  # 保留原样，不继续删其他文件
        try:
            (pkg_dir / "__init__.py").write_text(
                self._PACKAGE_INIT, encoding="utf-8")
            (pkg_dir / "_contract.py").write_text(
                self._PACKAGE_CONTRACT, encoding="utf-8")
            self._audit("migrate", "brain", version,
                        detail="包重写为新布局（控制流）")
        except OSError as exc:
            self._audit("migrate_failed", "brain", version, detail=str(exc))

    def _install_builtin(self, name):
        if name in PACKAGE_MODULES:
            self._install_builtin_package(name)
            return
        src = self._builtin_source(name)
        version_dir = self.root / name / "v1.0"
        version_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, version_dir / f"{name}.py")
        self._write_active(name, "v1.0")

    # 包模块内置文件清单（P2 拆包后）：控制流四件套——Agent 主类 +
    # 聊天/思考混入 + 技能元数据（agent_chat 相对导入的依赖）。
    # memory/planner 是独立版本单元（<data>/brain/<name>/vN/），不随包漂移；
    # evolver 是核心锁定集（进化引擎自身），agent.py 绝对导入宿主实现，
    # 不随包版本漂移；__init__/_contract 为包入口与契约声明。
    _PACKAGE_BUNDLE = ("agent.py", "agent_chat.py", "agent_think.py",
                       "skills.py")
    _PACKAGE_INIT = "from .agent import Agent\n"
    _PACKAGE_CONTRACT = (
        "# 候选包契约声明：必须与内核 PACKAGE_LAYOUT 完全一致\n"
        "EXPORTS = {'agent.py': 'Agent'}\n"
    )

    def _install_builtin_package(self, name):
        """包模块首启安装：从内置源（开发=项目 brain/，frozen=_MEIPASS/brain）
        拷 PACKAGE_BUNDLE 文件 + 生成 __init__.py/_contract.py → v1.0。"""
        version_dir = self.root / name / "v1.0"
        version_dir.mkdir(parents=True, exist_ok=True)
        src_dir = self._builtin_package_dir(name)
        for file_name in self._PACKAGE_BUNDLE:
            src = src_dir / file_name
            if not src.is_file():
                raise FileNotFoundError(f"内置包源码缺失：{src}")
            shutil.copy2(src, version_dir / file_name)
        (version_dir / "__init__.py").write_text(self._PACKAGE_INIT, encoding="utf-8")
        (version_dir / "_contract.py").write_text(self._PACKAGE_CONTRACT, encoding="utf-8")
        self._write_active(name, "v1.0")

    def _builtin_package_dir(self, name):
        """内置包源目录：开发模式=项目 brain/；frozen=_MEIPASS/brain。"""
        candidates = [
            Path(__file__).resolve().parent.parent / "brain",
            Path(getattr(sys, "_MEIPASS", "/nonexistent")) / "brain",
        ]
        for path in candidates:
            if (path / "agent.py").is_file():
                return path
        raise FileNotFoundError(
            f"内置包 {name} 源码不可用（frozen 打包需 --add-data brain:brain）"
        )

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
        if name in PACKAGE_MODULES:
            return self._load_package_version(name, version)
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

    def _load_package_version(self, name, version):
        """加载包版本：版本目录作为包（__init__.py 入口），
        子模块相对导入（from .memory import ...）经 sys.modules 注册解析。"""
        return self._load_package_dir(name, self._base(name) / version, version)

    def _load_package_dir(self, name, pkg_dir, tag):
        """从任意目录加载包（版本加载 tag=vN，候选验证 tag='candidate'）。"""
        pkg_dir = Path(pkg_dir)
        init = pkg_dir / "__init__.py"
        if not init.is_file():
            raise FileNotFoundError(f"{name} 包目录缺少 __init__.py：{pkg_dir}")
        mod_name = f"hb_pkg_{tag}_{name.replace('.', '_')}"
        # 先清理同名残留，避免候选/多版本间污染
        for key in list(sys.modules):
            if key == mod_name or key.startswith(mod_name + "."):
                del sys.modules[key]
        spec = importlib.util.spec_from_file_location(
            mod_name, init, submodule_search_locations=[str(pkg_dir)]
        )
        if spec is None or spec.loader is None:
            raise ImportError(f"{name} 包无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(mod_name, None)
            raise
        self._check_package_contract(name, module)
        return module

    def _check_package_contract(self, name, module):
        """包级契约：PACKAGE_LAYOUT 固定文件名->类名，逐项校验存在性。

        子模块经 getattr(module, file) 不可达（模块不是属性），
        直接按 spec.name 前缀从 sys.modules 取子模块。"""
        base_name = module.__name__
        for file_name, cls_name in PACKAGE_LAYOUT.get(name, ()):
            sub = sys.modules.get(f"{base_name}.{file_name[:-3]}")
            if sub is None:
                raise ValueError(f"{name} 包缺少子模块 {file_name}")
            if not callable(getattr(sub, cls_name, None)):
                raise ValueError(f"{name} 包子模块 {file_name} 缺少类 {cls_name}")

    def _package_exports(self, name, pkg_dir):
        """读取候选包 _contract.py 的 EXPORTS 声明，与内核 PACKAGE_LAYOUT 比对。

        返回 (ok, errors)。EXPORTS 必须与内核固定布局完全一致——
        候选自声明只允许"确认"布局，不允许"修改"布局（防自我验收）。
        """
        contract = pkg_dir / "_contract.py"
        if not contract.is_file():
            return False, ["包候选缺少 _contract.py（声明 EXPORTS 与内核布局一致）"]
        namespace: dict = {}
        try:
            code = contract.read_text(encoding="utf-8")
            compile(code, str(contract), "exec")
            exec(code, namespace)  # 仅读 EXPORTS 字面量（不执行候选业务代码）
        except Exception as exc:
            return False, [f"_contract.py 解析失败：{type(exc).__name__}: {exc}"]
        declared = namespace.get("EXPORTS")
        expected = dict(PACKAGE_LAYOUT.get(name, ()))
        if not isinstance(declared, dict) or declared != expected:
            return False, [
                f"_contract.py EXPORTS 与内核布局不一致（期望 {expected}，声明 {declared}）"
            ]
        return True, []

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
        if name in PACKAGE_MODULES:
            return self._validate_package_candidate(name, candidate_dir, run_smoke)
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
        # L2：宿主冒烟（构造真实 Agent 跑契约方法；P4 差分：候选 vs active
        # 在固定输入序列上对比输出形状——结构性破坏拦截，语义变更警告）
        if run_smoke and self.smoke_runner is not None:
            active_mod = None
            if name in BUILTIN_MODULES and self.active_version(name):
                try:
                    active_mod = self.load(name)
                except Exception:
                    active_mod = None  # active 不可加载（如迁移期）→ 跳过差分
            try:
                ok = self.smoke_runner(name, module, active_mod)
            except Exception as exc:
                return False, [f"L2 冒烟异常：{type(exc).__name__}: {exc}"]
            if not ok:
                return False, ["L2 冒烟未通过"]
        return True, []

    def _validate_package_candidate(self, name, candidate_dir, run_smoke=True):
        """验证包候选：L0 逐文件编译 + 包加载；L1 布局/契约；L2 冒烟。

        候选包目录即版本单元（__init__.py + 子模块 + _contract.py），
        校验通过后整体拷入版本目录。"""
        candidate_dir = Path(candidate_dir)
        if not (candidate_dir / "__init__.py").is_file():
            return False, ["包候选目录缺少 __init__.py"]
        # L0：逐文件语法 + 包加载
        for py in sorted(candidate_dir.glob("*.py")):
            if py.name == "_contract.py":
                continue
            try:
                compile(py.read_text(encoding="utf-8"), str(py), "exec")
            except SyntaxError as exc:
                return False, [f"L0 语法错误（{py.name}）：{exc}"]
        try:
            module = self._load_package_dir(name, candidate_dir, "candidate")
        except Exception as exc:
            return False, [f"L0 包加载失败：{type(exc).__name__}: {exc}"]
        # L1：_contract 布局 + 类契约
        ok, errs = self._package_exports(name, candidate_dir)
        if not ok:
            return False, errs
        for file_name, cls_name in PACKAGE_LAYOUT.get(name, ()):
            sub = sys.modules.get(f"hb_pkg_candidate_{name.replace('.', '_')}.{file_name[:-3]}")
            if sub is None:
                return False, [f"L1 包缺少子模块 {file_name}"]
            cls = getattr(sub, cls_name, None)
            if not callable(cls):
                return False, [f"L1 子模块 {file_name} 缺少类 {cls_name}"]
            missing = sorted(REQUIRED_METHODS.get(cls_name, set()) - set(dir(cls)))
            if missing:
                return False, [f"L1 {cls_name} 缺少方法：{', '.join(missing)}"]
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
        if name in PACKAGE_MODULES:
            return self._install_package_candidate(name, candidate_dir)
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

    def _install_package_candidate(self, name, candidate_dir):
        """包候选安装：验证 → 整体拷入版本目录（仅 .py，含 _contract.py）
        → 加载预检 → active 原子切换。失败删除新版本，不破坏现状。"""
        ok, errors = self.validate_candidate(name, candidate_dir)
        if not ok:
            raise ValueError("；".join(errors))
        candidate_dir = Path(candidate_dir)
        version = self._next_version(name)
        version_dir = self._base(name) / version
        version_dir.mkdir(parents=True, exist_ok=True)
        for py in candidate_dir.glob("*.py"):
            shutil.copy2(py, version_dir / py.name)
        try:
            self._load_version(name, version)  # 安装后完整性预检
        except Exception as exc:
            shutil.rmtree(version_dir, ignore_errors=True)
            raise ValueError(f"安装后加载失败：{exc}")
        self._write_active(name, version)
        self._audit("install", name, version, detail=str(candidate_dir))
        self._notify_switched(name, version)
        return version

    def source_files(self, name):
        """读当前 active 版本的全部源码：单文件 -> {f'{name}.py': text}；
        包 -> {相对路径: text}（含 __init__.py 与 _contract.py）。
        进化基准（升级 = 基于 active 源码生成新版本）。"""
        version = self.active_version(name)
        if not version:
            raise FileNotFoundError(f"模块 {name} 未安装")
        version_dir = self._base(name) / version
        if not version_dir.is_dir():
            raise FileNotFoundError(f"模块 {name} active 版本目录缺失：{version_dir}")
        if name in PACKAGE_MODULES:
            files = sorted(version_dir.glob("*.py"))
        else:
            files = [version_dir / f"{name}.py"]
        result: dict = {}
        for path in files:
            if not path.is_file():
                raise FileNotFoundError(f"模块 {name} active 版本源码缺失：{path}")
            result[path.name] = path.read_text(encoding="utf-8")
        return result

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
