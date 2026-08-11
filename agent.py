"""agent（brain 层）兼容 shim：实体在 brain/agent.py。

自进化约定：brain 是允许 AI 替换升级的进化层；本 shim 保持根目录
`import agent` 的旧引用（main / cli / 测试）不变。
阶段2（2026-08-12）包化后：Agent 主类在 brain/agent.py（ChatMixin +
ThinkMixin 组合），契约常量改从领域模块导出（brain.memory /
brain.planner）。create_agent 工厂在 brain 包 active 时实例化包内
Agent 类（控制流自进化生效点）。
"""

import sys

from brain.agent import Agent, Memory  # noqa: F401
from brain.memory import FOLLOW_KEYWORDS  # noqa: F401
from brain.planner import CURIOSITY_QUESTIONS  # noqa: F401


def create_agent(cfg, plugins=None, data_dir=None, stats=None, db=None,
                 brain_loader=None, clock=None):
    """Agent 工厂：brain 包 active 且可加载 → 实例化包内 Agent 类
    （控制流进入进化域）；否则用内置 Agent（零行为变更）。"""
    if brain_loader is not None:
        try:
            if "brain" in brain_loader.BUILTIN_MODULES and brain_loader.active_version("brain"):
                pkg = brain_loader.load("brain")
                if pkg is not None:
                    cls = getattr(sys.modules[f"{pkg.__name__}.agent"], "Agent")
                    return cls(
                        cfg, plugins, data_dir, stats=stats, db=db,
                        brain_loader=brain_loader, clock=clock,
                    )
        except Exception:
            pass
    return Agent(
        cfg, plugins, data_dir, stats=stats, db=db,
        brain_loader=brain_loader, clock=clock,
    )
