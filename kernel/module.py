"""kernel.module：模块生命周期管理（加载 / 卸载 / 重载）。

Kernel 不知道模块做什么（GPT / 记忆 / 采集都不关心），
它只负责：从目录发现 .py 模块、加载进名字空间、按名字卸载或重载。

只依赖标准库；本文件由 core.py re-export 保持旧引用兼容。
"""

import importlib.util
import sys
from pathlib import Path


def default_plugin_dirs():
    """模块查找顺序：exe 旁边（可热加）→ 内置目录。"""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent / "plugins")
    dirs.append(Path(__file__).parent.parent / "plugins")
    return dirs


def discover_plugins(plugin_dirs=None):
    """
    加载插件目录里的所有 .py 文件（下划线开头除外）。
    插件只需暴露 collect(settings) -> [{"title": str, "text": str}]，
    可选暴露 SETTINGS 配置项和 suggest(settings, entries, state) 规则发言。
    后面的目录不覆盖已加载的同名插件（用户插件优先）。
    """
    plugin_dirs = plugin_dirs if plugin_dirs is not None else default_plugin_dirs()
    plugins = {}
    for directory in plugin_dirs:
        folder = Path(directory)
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.py")):
            if path.name.startswith("_"):
                continue
            name = path.stem
            if name in plugins:
                continue
            try:
                spec = importlib.util.spec_from_file_location(
                    f"heartbeat_plugin_{name}", path
                )
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception:
                continue
            if callable(getattr(module, "collect", None)):
                plugins[name] = module
    return plugins


class ModuleManager:
    """模块生命周期管理：加载 / 卸载 / 重载。

    模块以 ``heartbeat_plugin_<name>`` 名字从文件加载（不注册进 sys.modules，
    重载即按原路径重新执行模块代码，与 discover_plugins 语义一致）。
    后续 updater（自进化）将基于此接口做 candidate → test → switch。
    """

    def __init__(self, modules=None):
        self._modules = dict(modules or {})

    @property
    def modules(self):
        return self._modules

    def names(self):
        return sorted(self._modules)

    def load(self, dirs=None):
        """发现并加载所有模块（后面的目录不覆盖已加载的同名模块）。"""
        self._modules.update(discover_plugins(dirs))
        return self._modules

    def unload(self, name):
        """卸载模块：从注册表移除。"""
        self._modules.pop(name, None)

    def reload(self, name):
        """重载单个模块：按原路径重新执行模块代码，collect 接口有效才替换。

        返回新模块；加载失败或接口不完整时保持原模块并返回原模块。
        """
        module = self._modules.get(name)
        if module is None:
            return None
        path = getattr(module, "__file__", None)
        if not path:
            return module
        try:
            spec = importlib.util.spec_from_file_location(
                f"heartbeat_plugin_{name}", path
            )
            if spec is None or spec.loader is None:
                return module
            reloaded = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(reloaded)
        except Exception:
            return module
        if callable(getattr(reloaded, "collect", None)):
            self._modules[name] = reloaded
            return reloaded
        return module
