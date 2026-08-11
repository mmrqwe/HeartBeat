"""HeartBeat 命令行模式：不开 GUI，直接测试/使用核心功能。

用法：
    python main.py --cli <命令> [参数]
    python cli.py <命令> [参数]
"""

import argparse
import json
import sys
import time
from pathlib import Path

import agent
import core
import db
import rag
import search
import skins


def _default_config_path():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent / "config.json"
    return Path(__file__).with_name("config.json")


def _load(config_path=None):
    cfg_path = Path(config_path) if config_path else _default_config_path()
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        core.save_config(core.load_config(), cfg_path)
    cfg = core.load_config(cfg_path)
    database = db.Database(cfg_path.parent / "heartbeat.db")
    stats = core.Stats(database)
    plugins = core.discover_plugins()
    ag = agent.Agent(cfg, plugins, cfg_path.parent, stats=stats, db=database)
    return cfg_path, cfg, database, stats, plugins, ag


def _mask_key(key):
    if not key:
        return "(empty)"
    if len(key) <= 8:
        return "***"
    return key[:6] + "***" + key[-4:]


def _make_parser():
    parser = argparse.ArgumentParser(prog="HeartBeat CLI")
    parser.add_argument("--config", default=None, help="配置文件路径")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config", help="打印当前配置（API Key 打码）")
    sub.add_parser("plugins", help="列出已发现的插件")
    sub.add_parser("collect", help="运行所有内容源采集")

    p_chat = sub.add_parser("chat", help="和桌宠对话（走真实 LLM 配置）")
    p_chat.add_argument("text", help="用户说的话")
    p_chat.add_argument("--no-stream", action="store_true", help="关闭流式输出")

    sub.add_parser("tick", help="跑一次完整巡视（采集 + 思考）")

    p_search = sub.add_parser("search", help="搜索网络/新闻/股票/天气等")
    p_search.add_argument("query")
    p_search.add_argument("category", nargs="?", default="web")
    p_search.add_argument("--limit", type=int, default=5)

    p_skin = sub.add_parser("skin", help="皮肤列表/切换")
    skin_sub = p_skin.add_subparsers(dest="skin_cmd", required=True)
    skin_sub.add_parser("list", help="列出所有皮肤并验证动画帧")
    p_skin_apply = skin_sub.add_parser("apply", help="切换皮肤")
    p_skin_apply.add_argument("name")

    p_db = sub.add_parser("db", help="数据库信息/清理")
    db_sub = p_db.add_subparsers(dest="db_cmd", required=True)
    db_sub.add_parser("info", help="数据库统计")
    db_sub.add_parser("chat-clear", help="清空聊天记录")

    p_embed = sub.add_parser("embed", help="测试本地向量模型")
    p_embed.add_argument("text")

    sub.add_parser("selfcheck", help="逐项测试全部核心功能")
    return parser


def _run(argv, default_config=None):
    if not argv:
        argv = ["selfcheck"]
    parser = _make_parser()
    args = parser.parse_args(argv)
    config = default_config or args.config
    cmd = args.command

    if cmd == "config":
        _, cfg, _, _, _, _ = _load(config)
        out = json.loads(json.dumps(cfg, ensure_ascii=False))
        out["api"]["api_key"] = _mask_key(out["api"].get("api_key", ""))
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    if cmd == "plugins":
        _, _, _, _, plugins, _ = _load(config)
        for name in sorted(plugins):
            mod = plugins[name]
            meta = getattr(mod, "META", {})
            print(f"{name}: {meta.get('label', '')}")
        return 0

    if cmd == "collect":
        _, cfg, _, stats, plugins, _ = _load(config)
        t0 = time.time()
        ctx = core.gather(plugins, cfg, stats)
        for coll in ctx["collections"]:
            print(f"[{coll['plugin']}] {len(coll['entries'])} entries")
            for entry in coll["entries"][:3]:
                print("  -", (entry.get("text") or "")[:100])
        if ctx["errors"]:
            print("errors:", ctx["errors"])
        print(f"elapsed: {time.time() - t0:.2f}s")
        return 0

    if cmd == "chat":
        _, _, _, _, _, ag = _load(config)
        t0 = time.time()
        if args.no_stream:
            reply = ag.chat(args.text)
            deltas = 0
        else:
            deltas = []

            def on_delta(text):
                deltas.append(text)

            reply = ag.chat(args.text, on_delta=on_delta)
            deltas = len(deltas)
        print("reply:", reply)
        print("stream_deltas:", deltas)
        print(f"elapsed: {time.time() - t0:.2f}s")
        return 0

    if cmd == "tick":
        _, cfg, _, stats, plugins, ag = _load(config)
        t0 = time.time()
        ctx = core.gather(plugins, cfg, stats)
        message = ag.think(ctx)
        print("message:", message)
        if ctx["errors"]:
            print("errors:", ctx["errors"])
        print(f"elapsed: {time.time() - t0:.2f}s")
        return 0

    if cmd == "search":
        t0 = time.time()
        entries = search.search_all(args.query, args.category, args.limit)
        for entry in entries:
            print("-", entry.get("title", ""))
            if entry.get("url"):
                print("  ", entry["url"])
        print(f"elapsed: {time.time() - t0:.2f}s, results: {len(entries)}")
        return 0

    if cmd == "skin":
        cfg_path, cfg, _, _, _, _ = _load(config)
        if args.skin_cmd == "list":
            for name, skin in skins.SKINS.items():
                frames = skins.build_frames(skin)
                anims = ",".join(f"{k}:{len(v)}" for k, v in frames.items())
                print(f"{name} [{skin['label']}] {anims}")
            return 0
        name = args.name
        if name not in skins.SKINS:
            print(f"unknown skin: {name}")
            return 1
        cfg["skin"] = name
        cfg["role"] = skins.SKINS[name].get("role", cfg.get("role", ""))
        core.save_config(cfg, cfg_path)
        skins.build_frames(skins.SKINS[name])
        print(f"applied skin: {name} ({skins.SKINS[name]['label']})")
        return 0

    if cmd == "db":
        _, _, database, _, _, _ = _load(config)
        if args.db_cmd == "info":
            print("memory:", len(database.memory_items(limit=10_000)))
            print("chat_messages:", len(database.chat_items(limit=10_000)))
            print("vec_ready:", database.vec_ready)
            return 0
        database.clear_chat()
        print("chat cleared")
        return 0

    if cmd == "embed":
        cfg_path, cfg, _, _, _, _ = _load(config)
        embedder = rag.default_embedder(cfg, cfg_path.parent)
        t0 = time.time()
        vec = embedder.embed_one(args.text)
        if vec is None:
            print("embedding unavailable")
            return 1
        print(f"dim: {len(vec)}, first3: {[round(float(x), 4) for x in vec[:3]]}")
        print(f"elapsed: {time.time() - t0:.2f}s")
        return 0

    if cmd == "selfcheck":
        return _selfcheck(config)

    print(f"unknown command: {cmd}")
    return 2


def _check(name, fn):
    t0 = time.time()
    try:
        fn()
        print(f"PASS {name} ({time.time() - t0:.2f}s)")
        return True
    except Exception as exc:
        print(f"FAIL {name} ({time.time() - t0:.2f}s): {type(exc).__name__}: {exc}")
        return False


def _selfcheck(config):
    results = []

    def check(name, fn):
        results.append(_check(name, fn))

    check("config load", lambda: _load(config))
    cfg_path, cfg, database, stats, plugins, ag = _load(config)

    check("plugin discovery", lambda: _require(len(plugins) >= 3, f"plugins={len(plugins)}"))

    def db_info():
        _require(database.vec_ready, "sqlite-vec not ready")
    check("database + vec", db_info)

    def skin_build():
        for name, skin in skins.SKINS.items():
            frames = skins.build_frames(skin)
            _require("idle" in frames, f"{name} missing idle")
    check("skins build", skin_build)

    def embed_step():
        embedder = rag.default_embedder(cfg, cfg_path.parent)
        vec = embedder.embed_one("selfcheck")
        _require(vec is not None and len(vec) == 512, "embedding failed")
    check("embedding", embed_step)

    def collect_step():
        ctx = core.gather(plugins, cfg, stats)
        _require(len(ctx["collections"]) >= 3, "not all plugins collected")
    check("collect all plugins", collect_step)

    def search_step():
        entries = search.search_all("AI", "web", 3)
        _require(len(entries) > 0, "no search results")
    check("web search", search_step)

    def chat_step():
        reply = ag.chat("Please reply with OK only")
        _require(bool(reply), "empty chat reply")
    check("chat (real LLM)", chat_step)

    def tick_step():
        ctx = core.gather(plugins, cfg, stats)
        message = ag.think(ctx)
        _require(True, "tick finished")
    check("tick/think", tick_step)

    passed = sum(results)
    total = len(results)
    print(f"selfcheck: {passed}/{total} passed")
    return 0 if passed == total else 1


def _require(cond, msg):
    if not cond:
        raise AssertionError(msg)


def run(argv, default_config=None):
    try:
        return _run(argv, default_config)
    except Exception as exc:
        print(f"CLI error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
