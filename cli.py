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
    # 统一数据目录（与 GUI/kernel.boot 一致）：配置、数据库、brain 版本全在
    # user_data_dir。frozen 与开发模式同一位置，重编译/升级不丢数据。
    return core.user_data_dir() / "config.json"


def _load(config_path=None):
    cfg_path = Path(config_path) if config_path else _default_config_path()
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        core.save_config(core.load_config(), cfg_path)
    cfg = core.load_config(cfg_path)
    # 数据目录统一 user_data_dir（GUI Kernel 同源）；config 路径可 --config 自定义
    data_dir = core.user_data_dir()
    database = db.Database(data_dir / "heartbeat.db")
    stats = core.Stats(database)
    plugins = core.discover_plugins()
    # 注入 updater 作 brain_loader：CLI 与 GUI 一致，updater 切换的 brain
    # 版本在 chat/tick 中同样生效（未注入时 Agent 用内置实现）
    from brain.smoke import smoke_test_module
    from kernel.updater import Updater

    updater = Updater(data_dir)
    updater.smoke_runner = smoke_test_module
    updater.ensure_installed()
    ag = agent.Agent(
        cfg, plugins, data_dir, stats=stats, db=database, brain_loader=updater
    )
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

    p_probe = sub.add_parser(
        "probe", help="frozen 环境自检：外部 .py 动态加载（updater 技术前提）"
    )
    p_probe.add_argument("--clean", action="store_true", help="先清理旧探针文件")

    p_updater = sub.add_parser("updater", help="自进化：brain 模块版本管理（memory/planner）")
    upd_sub = p_updater.add_subparsers(dest="upd_cmd", required=True)
    upd_sub.add_parser("status", help="查看各模块已安装版本与 active 指针")
    p_upd_validate = upd_sub.add_parser("validate", help="验证候选版本（L0 语法 + L1 接口 + L2 冒烟）")
    p_upd_validate.add_argument("module", choices=["memory", "planner"])
    p_upd_validate.add_argument("candidate_dir", help="候选版本目录（内含 <module>.py）")
    p_upd_install = upd_sub.add_parser("install", help="验证并安装候选版本（自动切换 active，下次启动生效）")
    p_upd_install.add_argument("module", choices=["memory", "planner"])
    p_upd_install.add_argument("candidate_dir")
    p_upd_switch = upd_sub.add_parser("switch", help="显式切换 active 版本")
    p_upd_switch.add_argument("module", choices=["memory", "planner"])
    p_upd_switch.add_argument("version")
    p_upd_rollback = upd_sub.add_parser("rollback", help="回滚 active 到最近可用旧版本")
    p_upd_rollback.add_argument("module", choices=["memory", "planner"])
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
        stream_count = 0
        if not args.no_stream:
            deltas: list = []

            def on_delta(text):
                deltas.append(text)

            reply = ag.chat(args.text, on_delta=on_delta)
            stream_count = len(deltas)
        else:
            reply = ag.chat(args.text)
        print("reply:", reply)
        print("stream_deltas:", stream_count)
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
        if not entries and args.category in (None, "web") and search.web_search_diag().get("errors"):
            print("搜索服务暂时不可用：" + "; ".join(search.web_search_diag()["errors"]))
        else:
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

    if cmd == "probe":
        return _probe(config, clean=args.clean)

    if cmd == "updater":
        return _updater_cmd(args)

    print(f"unknown command: {cmd}")
    return 2


def _updater_cmd(args):
    """自进化版本管理：status / validate / install / switch / rollback。"""
    from brain.smoke import smoke_test_module
    from kernel.updater import BUILTIN_MODULES, Updater

    upd = Updater(core.user_data_dir())
    upd.smoke_runner = smoke_test_module
    upd.ensure_installed()  # 幂等：CLI 下也保证基线版本存在

    if args.upd_cmd == "status":
        for name in BUILTIN_MODULES:
            versions = upd.list_versions(name)
            active = upd.active_version(name)
            print(f"{name}: active={active or '-'} 已装版本={','.join(versions) or '-'}")
        return 0

    if args.upd_cmd == "validate":
        ok, errors = upd.validate_candidate(args.module, args.candidate_dir)
        if not ok:
            print(f"验证失败：{args.module} {args.candidate_dir}")
            for err in errors:
                print("  -", err)
            return 1
        print(f"验证通过：{args.module} {args.candidate_dir}（L0 语法 + L1 接口 + L2 冒烟）")
        return 0

    if args.upd_cmd == "install":
        try:
            version = upd.install_candidate(args.module, args.candidate_dir)
        except ValueError as exc:
            print(f"安装失败：{exc}")
            return 1
        print(f"已安装 {args.module} {version} 并激活（下次启动或 reload 生效）")
        return 0

    if args.upd_cmd == "switch":
        try:
            upd.switch(args.module, args.version)
        except ValueError as exc:
            print(f"切换失败：{exc}")
            return 1
        print(f"已切换 {args.module} -> {args.version}（下次启动或 reload 生效）")
        return 0

    if args.upd_cmd == "rollback":
        version = upd.rollback(args.module)
        if version is None:
            print(f"无可回滚版本（当前 {upd.active_version(args.module) or '无'}）")
            return 1
        print(f"已回滚 {args.module} -> {version}")
        return 0

    print(f"unknown updater cmd: {args.upd_cmd}")
    return 2


def _probe(config_path=None, clean=False):
    """frozen 环境自检：验证外部 .py 动态加载（updater 的技术前提）。

    updater（自进化）需要在打包后的 .app 里从用户数据目录加载外部模块，
    本命令验证 spec_from_file_location 在 frozen 解释器下可用：
    在 user_data_dir/.probe 写入探针模块 → 动态加载 → 调用函数 → 输出 JSON。
    """
    import importlib.util
    import json

    report = {
        "frozen": bool(getattr(sys, "frozen", False)),
        "executable": sys.executable,
        "probe_dir": "",
        "loaded": False,
        "answer": None,
        "error": None,
    }
    try:
        probe_dir = core.user_data_dir() / ".probe"
        probe_dir.mkdir(parents=True, exist_ok=True)
        report["probe_dir"] = str(probe_dir)
        if clean:
            for old in probe_dir.glob("*.py"):
                old.unlink(missing_ok=True)
        probe_file = probe_dir / "probe_mod.py"
        probe_file.write_text(
            "import core\n"
            "def answer():\n"
            "    return 'probe-ok:' + __file__\n"
            "def uses_packed():\n"
            "    # 外部模块 import 打包内模块（updater 生成模块的核心约束）\n"
            "    return bool(core.user_data_dir())\n",
            encoding="utf-8",
        )
        spec = importlib.util.spec_from_file_location("heartbeat_probe", probe_file)
        if spec is None or spec.loader is None:
            raise RuntimeError("spec_from_file_location returned None")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        report["answer"] = mod.answer()
        report["uses_packed"] = bool(mod.uses_packed())
        report["loaded"] = True
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["loaded"] else 1


def _web_search_selfcheck():
    """selfcheck 的 web 搜索步骤（模块级以便单测）。

    返回 (ok, detail)：全源故障时重试一次；持续故障 detail 含各源原因，
    由调用方决定降级（WARN）而非 FAIL。
    """
    entries = search.search_all("AI", "web", 3)
    if entries:
        return True, ""
    diag = search.web_search_diag()
    if diag.get("errors"):
        print(f"  (web search 全源失败: {'; '.join(diag['errors'])}，重试一次)")
        time.sleep(1)
        entries = search.search_all("AI", "web", 3)
        if entries:
            return True, ""
        return False, "web search 全源失败: " + "; ".join(diag.get("errors", []))
    return False, "no search results"


def _selfcheck(config):
    results = []
    warnings = []

    def check(name, fn, soft=False):
        t0 = time.time()
        try:
            fn()
            print(f"PASS {name} ({time.time() - t0:.2f}s)")
            results.append(True)
        except Exception as exc:
            if soft:
                # 依赖外部服务的检查：失败降级为 WARN，不阻断 selfcheck
                print(f"WARN {name}: {type(exc).__name__}: {exc}")
                results.append(True)
                warnings.append(f"{name}: {exc}")
            else:
                print(f"FAIL {name} ({time.time() - t0:.2f}s): {type(exc).__name__}: {exc}")
                results.append(False)

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
        ok, detail = _web_search_selfcheck()
        _require(ok, detail)
    check("web search", search_step, soft=True)

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
    if warnings:
        print(f"warnings ({len(warnings)}):")
        for item in warnings:
            print(" -", item)
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
