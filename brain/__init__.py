"""brain：可进化层（Agent 的智能部分）。

Kernel 视角：brain 只是“一个需要运行的模块”。它内部拆分为：
- agent：   核心控制流（聊天 / 工具循环 / 思考调度 / 触发门控）
- memory：  记忆领域（事实提取 / 画像 / 跟进 / 向量检索）
- planner： 规则决策原语（问候 / 预算 / 冷却 / 时间感知 / 规则发言）

自进化约定（阶段 4 updater）：memory / planner 是允许 AI 替换升级的独立模块；
根目录 agent.py 是兼容 shim，`import agent` 的旧引用继续可用。
"""
