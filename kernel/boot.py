"""kernel.boot：启动层（类似 BIOS）。

职责：数据目录解析与旧数据迁移、配置加载/保存、启动路径探测。
只依赖标准库，不 import 任何业务模块（brain/plugins/ui）。

本文件由 core.py re-export 保持旧引用兼容（brain 层）。
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = {
    "pet_name": "小跳",
    "owner_title": "",
    "role": "小橘猫",
    "personality": "活泼、有点毒舌，但很关心主人",
    "speaking_style": "",
    "example_lines": "",
    "interval_minutes": 10,
    "quiet_start": 23,
    "quiet_end": 7,
    "embedding_enabled": True,
    "embedding_model": "BAAI/bge-small-zh-v1.5",
    "skin": "orange_cat",
    "stream": True,
    "thinking_enabled": True,
    "thinking_effort": "medium",
    "tools_enabled": True,
    "shell_tools_mode": "confirm",
    "shell_workdir": "",
    "project_dir": "",  # Coding 协作项目根目录（文件工具/后台命令的边界）
    "memory_cap": 500,
    "max_context_tokens": 400000,
    "context_compress_ratio": 0.75,
    "keep_recent_messages": 20,
    "conversation_summary_enabled": True,
    "daily_energy_budget": 1000,
    "proactive_energy_daily_cap": 150,
    "max_llm_calls_per_tick": 3,
    "wake_greeting_enabled": True,
    "collectors": {
        "weather": {"enabled": True},
        "rss_news": {"enabled": True},
        "quote": {"enabled": True},
    },
    "api": {
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "gpt-4o-mini",
    },
    # LLM 网络重连：连接失败/SSL 被掐断/5xx/429 时指数退避重试
    "retry": {
        "max_attempts": 3,    # 总尝试次数（含首次）
        "backoff_base": 0.5,  # 退避基数（秒），指数增长
        "backoff_max": 8.0,   # 退避上限（秒）
    },
}


def user_data_dir():
    """用户数据目录（跨平台）。配置、数据库、模型缓存都放这里，重编译/升级不丢数据。

    - macOS:   ~/Library/Application Support/HeartBeat
    - Windows: %APPDATA%/HeartBeat
    - Linux:   $XDG_DATA_HOME/HeartBeat 或 ~/.local/share/HeartBeat
    """
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "HeartBeat"
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        return base / "HeartBeat"
    base = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return base / "HeartBeat"


def default_config_path():
    """frozen 与开发模式统一：数据放用户数据目录，重编译/升级不丢。"""
    return user_data_dir() / "config.json"


def legacy_data_dirs():
    """旧版数据所在位置（app bundle 内 / 源码目录），用于首启自动迁移。"""
    dirs = []
    if getattr(sys, "frozen", False):
        dirs.append(Path(sys.executable).parent)
    dirs.append(Path(__file__).parent.parent)
    return dirs


def migrate_legacy_data(legacy_dirs, data_dir=None):
    """首启迁移：把旧位置（源码目录 / app bundle 内）的配置与数据库搬到用户数据目录。

    只在用户目录尚无数据且未执行过迁移时进行；迁移后写 .migrated 标记，避免重复扫描。
    bundle 内数据随重编译消失，迁移主要覆盖开发模式与首次升级场景。
    """
    data_dir = Path(data_dir) if data_dir else user_data_dir()
    marker = data_dir / ".migrated"
    if marker.exists():
        return data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    for legacy in legacy_dirs:
        for name in ("config.json", "heartbeat.db", "heartbeat.db-wal", "heartbeat.db-shm"):
            src = Path(legacy) / name
            dst = data_dir / name
            if src.is_file() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except OSError:
                    pass
        # 模型缓存（约 95MB）：整体迁移，避免首启重新下载
        src_models = Path(legacy) / "models"
        dst_models = data_dir / "models"
        if src_models.is_dir() and not dst_models.exists():
            try:
                shutil.copytree(src_models, dst_models)
            except OSError:
                pass
    try:
        marker.write_text(time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
    except OSError:
        pass
    return data_dir


def load_config(path: "str | os.PathLike" = "config.json"):
    """读取配置，缺失字段用默认值补齐，并迁移旧版顶层字段。"""
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    p = Path(path)
    if p.exists():
        user = json.loads(p.read_text(encoding="utf-8"))
        _deep_merge(cfg, _migrate_legacy(user))
    return cfg


def save_config(cfg, path: "str | os.PathLike" = "config.json"):
    Path(path).write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _migrate_legacy(user):
    """旧版 config.json 把 city/feeds 放在顶层，迁移进插件配置。"""
    if "weather_city" in user:
        user.setdefault("collectors", {}).setdefault("weather", {}).setdefault(
            "city", user["weather_city"]
        )
    if "news_feeds" in user:
        user.setdefault("collectors", {}).setdefault("rss_news", {}).setdefault(
            "feeds", user["news_feeds"]
        )
    return user


def _deep_merge(base, extra):
    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
