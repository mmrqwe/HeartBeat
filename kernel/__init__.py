"""kernel：最小内核（AI Runtime OS 底座）。

职责（只做这四件事，不包含 LLM / 记忆 / 规划）：
- boot：      启动路径、数据迁移、配置加载/保存
- module：    模块生命周期（发现 / 加载 / 卸载 / 重载）
- permission：安全边界（shell 命令分级、敏感过滤、硬边界执行）
- runtime：   事件循环上的任务调度（定时 / 看门狗 / 线程 / epoch 保护）
- eventbus：  发布/订阅事件总线（kernel 系统事件与 brain 旁路通知）

依赖方向：kernel 不 import brain / plugins / ui 的任何模块。
自进化已于 2026-08-13 移除（updater/monitor 随同删除）。
"""

from pathlib import Path

from . import boot, module, permission, workspace  # noqa: F401


class Kernel:
    """最小内核门面：启动配置、模块发现、运行时调度、权限边界。"""

    def __init__(self, config_path=None, data_dir=None):
        if data_dir is not None:
            # 显式注入（测试隔离 / 嵌入式场景）：跳过旧数据迁移，
            # 直接使用注入目录（与 config_path 正交——config 决定配置位置）
            self.data_dir = Path(data_dir)
            self.data_dir.mkdir(parents=True, exist_ok=True)
        else:
            # 旧数据迁移（源码目录 / app bundle → 用户数据目录）
            self.data_dir = boot.migrate_legacy_data(boot.legacy_data_dirs())
        self.config_path = (
            Path(config_path) if config_path else boot.default_config_path()
        )
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.config_path.exists():
            boot.save_config(boot.load_config(), self.config_path)
        self.cfg = boot.load_config(self.config_path)
        # 补全缺失的默认键（迁移来的旧 config 可能缺新键），保证磁盘配置完整
        boot.save_config(self.cfg, self.config_path)
        # 模块发现与生命周期管理
        self.plugins = module.discover_plugins()
        self.modules = module.ModuleManager(self.plugins)
        # 运行时（延迟 import：CLI / 测试等无 GUI 场景不加载 PySide6）
        from .runtime import Runtime
        from .eventbus import EventBus

        self.runtime = Runtime()
        self.eventbus = EventBus()

    def save_settings(self, cfg):
        """保存配置并更新内存副本（由 UI / CLI 调用）。发布 config.saved 事件。"""
        self.cfg = cfg
        boot.save_config(cfg, self.config_path)
        self.eventbus.emit("config.saved", cfg)

    def stop(self):
        self.runtime.stop_all()
