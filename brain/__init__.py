"""brain：可进化层（Agent 的智能部分）。

Kernel 视角：brain 只是“一个需要运行的模块”。它内部拆分为：
- agent：   核心控制流（聊天 / 工具循环 / 思考调度 / 触发门控）
- memory：  记忆领域（事实提取 / 画像 / 跟进 / 向量检索）
- planner： 规则决策原语（问候 / 预算 / 冷却 / 时间感知 / 规则发言）

自进化约定（阶段 4 updater + 阶段5 P2 拆包）：memory / planner 是允许 AI
替换升级的独立版本单元（<data>/brain/<name>/vN/，不随 brain 包漂移）；
brain 包只含控制流三件套 + skills（整体版本化）。根目录 agent.py 是
兼容 shim，`import agent` 的旧引用继续可用。

契约：memory / planner 的**公开方法签名与语义**即升级契约（清单见
kernel/updater.REQUIRED_METHODS）。新增公开方法属破坏性变更，需同步发布
新契约清单；模块内部实现可自由演进（组合模式：经 self.agent 访问共享状态）。
升级候选通过 L0 语法 + L1 接口 + L2 冒烟（brain/smoke.py）后由 updater
安装/切换/回滚；每次切换写入 <data>/brain/updates.log 审计并可广播
brain.switched 事件做运行中热切换。
"""
