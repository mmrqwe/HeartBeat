"""brain：可进化层（Agent 的智能部分）。

Kernel 视角：brain 只是“一个需要运行的模块”。它内部拆分为：
- agent：   核心控制流（聊天 / 工具循环 / 思考调度 / 触发门控）
- memory：  记忆领域（事实提取 / 画像 / 跟进 / 向量检索）
- planner： 规则决策原语（问候 / 预算 / 冷却 / 时间感知 / 规则发言）

2026-08-13 起自进化已移除：本包全部静态内置（agent/memory/planner 直接
import 生效），不再有版本目录、动态加载、契约验证与热切换。根目录
agent.py 是兼容 shim，`import agent` 的旧引用继续可用。

组合模式不变：memory/planner 经 self.agent 访问共享状态，公开方法签名
即宿主调用契约（修改前须同步更新 main/cli/测试调用方）。
"""
