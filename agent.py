"""agent（brain 层）兼容 shim：实体在 brain/agent.py。

本 shim 保持根目录 `import agent` 的旧引用（main / cli / 测试）不变。
2026-08-13 起自进化已移除：brain 层全部静态内置，create_agent 直接
实例化内置 Agent，不再有版本加载/宿主委托注入。
"""

from brain.agent import Agent, Memory  # noqa: F401
from brain.memory import FOLLOW_KEYWORDS  # noqa: F401
from brain.planner import CURIOSITY_QUESTIONS  # noqa: F401


def create_agent(cfg, plugins=None, data_dir=None, stats=None, db=None,
                 clock=None, embed_queue=None):
    """Agent 工厂：直接实例化内置 Agent（自进化已移除，无版本加载分支）。"""
    return Agent(
        cfg, plugins, data_dir, stats=stats, db=db,
        clock=clock, embed_queue=embed_queue,
    )

