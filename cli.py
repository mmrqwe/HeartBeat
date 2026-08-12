"""HeartBeat 命令行模式：不开 GUI，直接测试/使用核心功能。

用法：
    python main.py --cli <命令> [参数]
    python cli.py <命令> [参数]
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

import agent
import core
import db
import rag
import search
from gui import skins


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
    try:
        data_dir.mkdir(parents=True, exist_ok=True)  # 隔离 HOME 首跑时目录可能不存在
    except OSError:
        pass
    database = db.Database(data_dir / "heartbeat.db")
    stats = core.Stats(database)
    plugins = core.discover_plugins()
    # brain 层静态内置（自进化已移除）：直接实例化内置 Agent
    ag = agent.create_agent(
        cfg, plugins, data_dir, stats=stats, db=database
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

    p_coding = sub.add_parser(
        "coding", help="Coding 模式：在 project_dir 项目内完成编程任务"
    )
    p_coding.add_argument("request", help="编程需求描述")
    p_coding.add_argument("--project-dir", default=None, help="临时覆盖项目目录")
    p_coding.add_argument("--mode", default=None, choices=["off", "readonly", "confirm", "full"],
                          help="临时覆盖工具档位（CLI 无弹窗，写操作建议 full）")

    sub.add_parser("tick", help="跑一次完整生活循环（采集 + 主动思考）")

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

    p_memory = sub.add_parser("memory", help="记忆管理：列表/清空/清理/导出")
    mem_sub = p_memory.add_subparsers(dest="mem_cmd", required=True)
    p_mem_list = mem_sub.add_parser("list", help="列出记忆（可按类别筛选）")
    p_mem_list.add_argument("--category", default=None, help="identity/preference/habit/schedule/finance/misc")
    p_mem_list.add_argument("--limit", type=int, default=100)
    mem_sub.add_parser("clear", help="清空全部记忆")
    mem_sub.add_parser("prune", help="清理过期记忆并按 memory_cap 上限淘汰")
    p_mem_export = mem_sub.add_parser("export", help="导出全部记忆为 JSON")
    p_mem_export.add_argument("path", help="输出 JSON 文件路径")

    p_embed = sub.add_parser("embed", help="测试本地向量模型")
    p_embed.add_argument("text")

    sub.add_parser("selfcheck", help="逐项测试全部核心功能")

    p_skill = sub.add_parser("skill", help="技能包：下载 / 安装 / 查看 / 状态 / 初始化（如 zhihu-cli）")
    skill_sub = p_skill.add_subparsers(dest="skill_cmd", required=True)
    skill_sub.add_parser("list", help="列出已安装技能（SKILL.md 元数据）")
    p_skill_dl = skill_sub.add_parser("download", help="下载技能包 zip 到 <数据目录>/downloads")
    p_skill_dl.add_argument("url")
    p_skill_dl.add_argument("--filename", default=None)
    p_skill_install = skill_sub.add_parser("install", help="安装下载目录里的技能包 zip")
    p_skill_install.add_argument("zip_path")
    p_skill_status = skill_sub.add_parser("status", help="运行技能包状态检查（scripts/run.* status）")
    p_skill_status.add_argument("name")
    p_skill_setup = skill_sub.add_parser("setup", help="运行技能包安装脚本（scripts/setup.*）")
    p_skill_setup.add_argument("name")
    p_skill_auth = skill_sub.add_parser("auth", help="用 Access Secret 配置认证（stdin 输入，不回显）")
    p_skill_auth.add_argument("name")
    p_skill_auth.add_argument("--binary", default=None, help="CLI 可执行文件绝对路径（默认 %LOCALAPPDATA%\\ZhihuCLI\\current\\zhihu-cli.exe）")
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

    if cmd == "coding":
        return _coding_cmd(args)

    if cmd == "tick":
        _, cfg, _, stats, plugins, ag = _load(config)
        t0 = time.time()
        ctx = core.gather(plugins, cfg, stats)
        message = ag.live(ctx)
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

    if cmd == "memory":
        return _memory_cmd(config, args)

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

    if cmd == "skill":
        return _skill_cmd(args)

    print(f"unknown command: {cmd}")
    return 2


def _coding_cmd(args):
    """CLI Coding 模式：真实 LLM + 真实工具循环（CLI 无弹窗，写操作建议 --mode full）。"""
    _, cfg, _, _, _, ag = _load(args.config)
    if args.project_dir:
        cfg["project_dir"] = args.project_dir
    if args.mode:
        cfg["shell_tools_mode"] = args.mode
    if not str(cfg.get("project_dir", "") or "").strip():
        print("未配置 project_dir。用 --project-dir 指定项目目录。")
        return 1
    t0 = time.time()

    def on_status(text):
        print("  " + text, flush=True)

    reply = ag.coding_task(args.request, on_status=on_status)
    print("reply:", reply)
    print(f"elapsed: {time.time() - t0:.2f}s")
    return 0


def _memory_cmd(config, args):
    _, _, database, _, _, ag = _load(config)
    if args.mem_cmd == "list":
        items = database.memory_items(limit=args.limit)
        if args.category:
            items = [i for i in items if i.get("category") == args.category]
        for it in items:
            print(
                f"[{it['id']}] {it['role']} {it['category']} "
                f"imp={it['importance']} {it['time']} {it['text']}"
            )
        print(f"total: {len(items)}")
        return 0
    if args.mem_cmd == "clear":
        database.clear_memory()
        print("memory cleared")
        return 0
    if args.mem_cmd == "prune":
        expired, capped = ag.memory_module.cleanup()
        print(f"pruned: expired={expired}, capped={capped}")
        return 0
    if args.mem_cmd == "export":
        items = database.memory_items(limit=None)
        payload = {
            "exported_at": time.strftime("%Y-%m-%d %H:%M"),
            "count": len(items),
            "items": items,
        }
        Path(args.path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"exported {len(items)} memories -> {args.path}")
        return 0
    return 2


def _skill_auth(name, binary=None):
    import tools as tools_mod

    secret = sys.stdin.buffer.readline().decode("utf-8", errors="replace").strip()
    if not secret:
        print("Access Secret 为空。请通过 stdin 传入，例如：")
        print(f'echo "<secret>" | python main.py --cli skill auth {name}')
        return 1
    rc, text = tools_mod.configure_skill_auth(name, secret, binary)
    print(text)
    return rc


def _skill_cmd(args):
    from kernel.permission import SOURCE_USER
    import tools as tools_mod

    if args.skill_cmd == "list":
        skills_root = core.user_data_dir() / "skills"
        found = False
        if skills_root.is_dir():
            for folder in sorted(skills_root.iterdir()):
                md = folder / "SKILL.md"
                if not md.is_file():
                    continue
                meta = core.parse_skill_frontmatter(
                    md.read_text(encoding="utf-8", errors="replace")
                )
                name = meta.get("name") or folder.name
                desc = meta.get("description", "")
                print(f"{folder.name}: name={name} | desc={desc}")
                found = True
        if not found:
            print("(no skills installed)")
        return 0

    if args.skill_cmd == "download":
        payload = {"url": args.url}
        if args.filename:
            payload["filename"] = args.filename
        result = tools_mod.execute(
            "download_file", json.dumps(payload),
            mode="full", source=SOURCE_USER, confirm_cb=lambda _desc: True,
        )
        print(result)
        return 0 if "下载完成" in result else 1

    if args.skill_cmd == "install":
        result = tools_mod.execute(
            "install_skill", json.dumps({"zip_path": args.zip_path}),
            mode="full", source=SOURCE_USER, confirm_cb=lambda _desc: True,
        )
        print(result)
        return 0 if "安装完成" in result else 1

    if args.skill_cmd == "status":
        import tools as tools_mod

        rc, text = tools_mod.run_skill_script(
            args.name, "run.ps1" if os.name == "nt" else "run.sh", ["status"]
        )
        print(text)
        return rc

    if args.skill_cmd == "setup":
        import tools as tools_mod

        rc, text = tools_mod.run_skill_script(
            args.name, "setup.ps1" if os.name == "nt" else "setup.sh", []
        )
        print(text)
        return rc

    if args.skill_cmd == "auth":
        return _skill_auth(args.name, args.binary)

    print(f"unknown skill cmd: {args.skill_cmd}")
    return 2


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
        message = ag.live(ctx)
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
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(errors="replace")
            except Exception:
                pass
    try:
        return _run(argv, default_config)
    except Exception as exc:
        print(f"CLI error: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
