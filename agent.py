"""agent（brain 层）兼容 shim：实体在 brain/agent.py。

自进化约定：brain 是允许 AI 替换升级的进化层；本 shim 保持根目录
`import agent` 的旧引用（main / cli / 测试）不变。
阶段2（2026-08-12）包化后：Agent 主类在 brain/agent.py（ChatMixin +
ThinkMixin 组合），契约常量改从领域模块导出（brain.memory /
brain.planner）。create_agent 工厂在 brain 包 active 时实例化包内
Agent 类（控制流自进化生效点）。

契约分层（阶段6 宿主委托层）：包内核心契约 = kernel/updater 的
REQUIRED_METHODS（L1 验证候选、启动预检）。coding_task 等"薄包装 +
策略在宿主侧"的宿主委托方法**不进 REQUIRED_METHODS**——由本工厂在
实例出口注入（_inject_host_delegates）：旧快照/进化版本没有时补上，
用户自定义时尊重其实现。这样宿主新增委托能力不需要发布新包版本，
也不破坏老用户的进化包（控制流不因宿主升级被强制刷新）。
"""

import inspect
import logging
import sys
import types

from brain.agent import Agent, Memory  # noqa: F401
from brain.memory import FOLLOW_KEYWORDS  # noqa: F401
from brain.planner import CURIOSITY_QUESTIONS  # noqa: F401

_logger = logging.getLogger(__name__)


def _host_tool_execute(self, name, arguments):
    """宿主锁定层直连：tools.execute 的规范接线（与内置 _run_tool 一致）。

    旧包快照的 _run_tool 无法表达宿主后加的 project_dir 透传维度时，
    编码路径退化为直连锁定层——安全分级 / 确认弹窗 / 审计 / 项目目录
    全部在 kernel/tools（锁定层）内，不会因绕过旧 _run_tool 而失守。
    """
    import tools as tools_mod

    mode = self.cfg.get("shell_tools_mode", tools_mod.SHELL_MODE_CONFIRM)
    if mode not in tools_mod.SHELL_MODES:
        mode = tools_mod.SHELL_MODE_CONFIRM
    return tools_mod.execute(
        name,
        arguments,
        mode=mode,
        source=tools_mod.SOURCE_USER,
        confirm_cb=getattr(self, "tool_confirm_cb", None),
        cwd=tools_mod.resolve_workdir(self.cfg),
        audit=getattr(self, "_audit_tool", None),
        project_dir=self.cfg.get("project_dir", ""),
    )


def _host_coding_task(self, user_text, on_status=None, on_delta=None, max_rounds=None):
    """宿主委托：Coding 模式入口（与内置 Agent 类方法语义完全一致）。

    控制循环在宿主 brain.coding_agent（策略层，绝对导入）——不在进化包
    内，包版本升级/回退不影响该能力。

    工具分发自适应：新包 _run_tool 支持 project_dir → 正常透传；旧包
    快照不支持 → 直连宿主锁定层 _host_tool_execute（保证文件工具的项目
    边界不丢）。绝不用 try/except TypeError 探测——工具执行内部抛
    TypeError 会误判并造成重复执行。
    """
    from brain.coding_agent import run_coding_task
    from tools import SOURCE_USER

    try:
        supports_project_dir = (
            "project_dir" in inspect.signature(self._run_tool).parameters
        )
    except (AttributeError, TypeError, ValueError):
        supports_project_dir = False

    def run(name, arguments):
        if supports_project_dir:
            return self._run_tool(
                name, arguments, source=SOURCE_USER,
                project_dir=self.cfg.get("project_dir", ""),
            )
        return _host_tool_execute(self, name, arguments)

    return run_coding_task(
        self.brain, self.cfg, user_text, run,
        on_status=on_status, on_delta=on_delta, max_rounds=max_rounds,
    )


def _inject_host_delegates(instance):
    """宿主委托层注入：实例缺 coding_task 时绑定宿主实现。

    - 新包快照/内置类自带 coding_task → hasattr 命中，跳过（零开销）；
    - 旧包快照（如 v1.0 无 coding_task）→ 注入等价实现，能力不随包版本漂移；
    - 用户进化版本自定义了 coding_task → 尊重用户实现，不覆盖。

    只注入薄包装（策略在宿主侧）；带实质逻辑的方法必须走
    REQUIRED_METHODS 契约，禁止以注入方式绕过 L1。
    """
    if not hasattr(instance, "coding_task"):
        instance.coding_task = types.MethodType(_host_coding_task, instance)
        _logger.info("coding_task 已由宿主注入（active 包/类无此方法）")
    return instance


def create_agent(cfg, plugins=None, data_dir=None, stats=None, db=None,
                 brain_loader=None, clock=None, embed_queue=None):
    """Agent 工厂：brain 包 active 且可加载 → 实例化包内 Agent 类
    （控制流进入进化域）；否则用内置 Agent（零行为变更）。
    所有路径经 _inject_host_delegates 补齐宿主委托方法。"""
    if brain_loader is not None:
        try:
            if "brain" in brain_loader.BUILTIN_MODULES and brain_loader.active_version("brain"):
                pkg = brain_loader.load("brain")
                if pkg is not None:
                    cls = getattr(sys.modules[f"{pkg.__name__}.agent"], "Agent")
                    return _inject_host_delegates(cls(
                        cfg, plugins, data_dir, stats=stats, db=db,
                        brain_loader=brain_loader, clock=clock, embed_queue=embed_queue,
                    ))
        except Exception:
            pass
    return _inject_host_delegates(Agent(
        cfg, plugins, data_dir, stats=stats, db=db,
        brain_loader=brain_loader, clock=clock, embed_queue=embed_queue,
    ))

