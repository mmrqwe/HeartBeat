"""agent（brain 层）兼容 shim：实体在 brain/agent.py。

自进化约定：brain 是允许 AI 替换升级的进化层；本 shim 保持根目录
`import agent` 的旧引用（main / cli / 测试）不变。
"""

from brain.agent import (  # noqa: F401
    Agent,
    CURIOSITY_QUESTIONS,
    FOLLOW_KEYWORDS,
    Memory,
)
