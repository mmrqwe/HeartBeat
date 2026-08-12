"""test_evolve：自我进化流水线测试（brain.evolver + Agent 意图识别）。

覆盖：成功全链路（生成→安全→验证→安装→active 切换→新方法生效）、
import 白名单拒绝、失败重试带反馈、不可进化模块拒绝、意图解析与确认流程。
LLM 全部 mock（FakeBrain），不碰真实 API。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent as agent_mod
import core
import db as dbmod

ROOT = Path(__file__).parent.parent

PLANNER_SRC = (ROOT / "brain" / "planner.py").read_text(encoding="utf-8")
# 模拟 LLM 的"进化结果"：插入一个新方法（验证新版本真的带上了新功能）
_NEW_METHOD = '\n    def drink_reminder(self):\n        return "该喝水啦"\n'
PLANNER_EVOLVED = PLANNER_SRC.replace(
    "    def maybe_save_thought(self, ctx):",
    _NEW_METHOD + "    def maybe_save_thought(self, ctx):",
)
assert "drink_reminder" in PLANNER_EVOLVED
# 语法坏代码（def 缺参数括号 → SyntaxError）
BAD_SRC = PLANNER_SRC.replace(
    "    def greeting(self, now):",
    "    def broken(:\n        pass\n\n    def greeting(self, now):",
)


class FakeBrain:
    """mock core.Brain：按序返回预设响应，记录收到的 prompt。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts = []

    def complete(self, messages, max_tokens=None, **kw):
        self.prompts.append(messages)
        if not self.responses:
            raise AssertionError("FakeBrain 响应已耗尽")
        return self.responses.pop(0)


def _make_updater(tmp_path):
    from brain.smoke import smoke_test_module
    from kernel.updater import Updater

    upd = Updater(str(tmp_path))
    upd.smoke_runner = smoke_test_module
    upd.ensure_installed()
    return upd


def _make_agent(tmp_path, with_updater=False):
    cfg = core.load_config(tmp_path / "config.json")
    cfg["api"]["api_key"] = ""
    cfg["embedding_enabled"] = False
    cfg["tools_enabled"] = False
    database = dbmod.Database(tmp_path / "heartbeat.db")
    ag = agent_mod.Agent(
        cfg, {}, tmp_path, stats=core.Stats(database), db=database,
        brain_loader=_make_updater(tmp_path) if with_updater else None,
    )
    return ag


def _make_evolver(tmp_path, fake_brain):
    from brain.evolver import Evolver

    return Evolver(fake_brain, _make_updater(tmp_path))


def test_evolve_pipeline_success(tmp_path):
    """全链路：生成候选 → 安全/契约/冒烟验证 → 安装 → 新方法生效。

    阶段2 包模式：brain 包 active 时 evolve('planner') = 升级包内 planner.py，
    整体安装为新包版本（v1.0 → v1.1）。
    """
    ev = _make_evolver(tmp_path, FakeBrain([PLANNER_EVOLVED]))
    version = ev.evolve("planner", "每天上午9点提醒我喝水")
    assert version == "v1.1"
    assert ev.updater.active_version("brain") == "v1.1"
    # active 包已切换且可加载，新方法真实存在
    module = ev.updater.load("brain")
    sub = sys.modules[f"{module.__name__}.planner"]
    assert hasattr(getattr(sub, "Planner"), "drink_reminder")
    # 候选目录已清理
    assert not list(ev.candidate_root.glob("*"))
    # 审计有 install 记录
    log = (ev.updater.root / "updates.log").read_text(encoding="utf-8")
    assert '"action": "install"' in log and '"version": "v1.1"' in log


def test_evolve_rejects_dangerous_import(tmp_path):
    """候选代码含禁止 import（os）→ 重试耗尽后失败，版本未安装。"""
    bad = "import os\n" + PLANNER_SRC
    ev = _make_evolver(tmp_path, FakeBrain([bad, bad, bad]))
    try:
        ev.evolve("planner", "任意需求")
    except ValueError as exc:
        assert "禁止 import：os" in str(exc)
    else:
        raise AssertionError("应当拒绝含 os import 的候选")
    assert ev.updater.list_versions("planner") == ["v1.0"]
    assert ev.updater.active_version("planner") == "v1.0"


def test_check_safety_blocks_pathlib_io():
    """pathlib 放行但不许文件 IO：read_text/write_text/目录遍历均拒绝。"""
    from brain.evolver import Evolver

    assert Evolver.check_safety(None, PLANNER_SRC) == []
    bad_read = (
        "from pathlib import Path\n"
        "class M:\n"
        "    def profile(self):\n"
        "        return Path('config.json').read_text()\n"
    )
    errors = Evolver.check_safety(None, bad_read)
    assert any("read_text" in e for e in errors), errors

    bad_write = "from pathlib import Path\nPath('x').write_text('y')\n"
    errors = Evolver.check_safety(None, bad_write)
    assert any("write_text" in e for e in errors), errors

    bad_iter = "from pathlib import Path\nfor p in Path('.').iterdir():\n    pass\n"
    errors = Evolver.check_safety(None, bad_iter)
    assert any("iterdir" in e for e in errors), errors


def test_evolve_retry_with_feedback(tmp_path):
    """首次生成语法错误 → 带反馈重试 → 第二次成功。"""
    fake = FakeBrain([BAD_SRC, PLANNER_EVOLVED])
    ev = _make_evolver(tmp_path, fake)
    version = ev.evolve("planner", "每天上午9点提醒我喝水")
    assert version == "v1.1"
    # 第二次请求带上了上次失败反馈
    assert "验证失败" in fake.prompts[1][-1]["content"]


def test_evolve_rejects_non_evolvable_module(tmp_path):
    """agent 模块不可自进化（核心控制流锁定）。"""
    fake = FakeBrain([])
    ev = _make_evolver(tmp_path, fake)
    try:
        ev.evolve("agent", "任意需求")
    except ValueError as exc:
        assert "不可进化" in str(exc)
    else:
        raise AssertionError("应当拒绝 agent 模块")
    assert fake.responses == []  # 未发起任何生成


def test_evolve_requirement_too_short(tmp_path):
    ev = _make_evolver(tmp_path, FakeBrain([]))
    try:
        ev.evolve("planner", "喝水")
    except ValueError as exc:
        assert "需求描述太短" in str(exc)
    else:
        raise AssertionError("应当拒绝过短需求")


def test_evolve_intent_parse(tmp_path):
    """Agent 意图识别：关键词/模块解析/确认回调/取消/引导。"""
    ag = _make_agent(tmp_path, with_updater=True)
    # 非进化文本不拦截
    assert ag._try_evolve_intent("今天天气怎么样") is None
    # 无确认回调（CLI）→ 走到 evolve（mock 掉，避免真实 LLM）
    ag.evolver.evolve = lambda name, req, on_status=None: "v9.9"
    reply = ag._try_evolve_intent("进化 planner：每天上午9点提醒我喝水")
    assert "进化成功" in reply and "v9.9" in reply and "planner" in reply
    # memory 关键词解析
    seen = {}
    ag.evolver.evolve = lambda name, req, on_status=None: seen.update(name=name, req=req) or "v1.1"
    ag._try_evolve_intent("升级记忆：多记住一些日程")
    assert seen["name"] == "memory" and "多记住一些日程" in seen["req"]
    # 用户拒绝确认 → 取消
    ag.tool_confirm_cb = lambda desc: False
    reply = ag._try_evolve_intent("进化 planner：每天上午9点提醒我喝水")
    assert "已取消" in reply
    # 需求太短 → 引导
    reply = ag._try_evolve_intent("进化 planner")
    assert "请说具体一点" in reply
    # 无 brain_loader（测试直连）→ 引擎不可用
    ag2 = _make_agent(tmp_path, with_updater=False)
    reply = ag2._try_evolve_intent("进化 planner：每天上午9点提醒我喝水")
    assert "进化引擎不可用" in reply


# ---------- 进化工具（tool 类型） ----------

TOOL_SRC = '''\
TOOL_NAME = "ping_check"
TOOL_DESCRIPTION = "检查网络连通性（示例进化工具）"
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {"host": {"type": "string"}},
    "required": ["host"],
}


def handler(args, ctx):
    host = str(args.get("host", "")).strip()
    if not host:
        return "缺少 host 参数"
    try:
        text = ctx.web_search(host + " 状态", limit=1)
        return "查询结果：" + text
    except Exception as exc:
        return "查询失败：" + str(exc)
'''

# 升级版候选：加 timeout 参数（TOOL_NAME 保持不变）
TOOL_SRC_V2 = TOOL_SRC.replace(
    '"properties": {"host": {"type": "string"}},'
    , '"properties": {"host": {"type": "string"}, "timeout": {"type": "number"}},'
)


def _tool_cand(root, src):
    d = Path(root) / "cand"
    d.mkdir(exist_ok=True)
    (d / "tool.py").write_text(src, encoding="utf-8")
    return d


def test_evolve_tool_pipeline(tmp_path):
    """生成工具模块 → 验证（受限加载/契约/AST/冒烟）→ 安装 → 可加载。"""
    ev = _make_evolver(tmp_path, FakeBrain(["```python\n" + TOOL_SRC + "\n```"]))
    result = ev.evolve("tool", "查快递物流")
    assert result == "ping_check@v0.1"
    assert ev.updater.list_tools() == ["ping_check"]
    mod = ev.updater.load("ping_check")
    assert mod.TOOL_NAME == "ping_check" and callable(mod.handler)
    assert not list(ev.candidate_root.glob("*"))


def test_evolve_tool_rejects_dangerous(tmp_path):
    bad = "import os\n" + TOOL_SRC
    ev = _make_evolver(tmp_path, FakeBrain([bad, bad, bad]))
    try:
        ev.evolve("tool", "查快递物流")
        raise AssertionError("应拒绝危险工具")
    except ValueError as exc:
        assert "禁止 import" in str(exc)
    assert ev.updater.list_tools() == []


def test_evolve_tool_retry_with_feedback(tmp_path):
    bad = "import os\n" + TOOL_SRC
    fake = FakeBrain([bad, "```python\n" + TOOL_SRC + "\n```"])
    ev = _make_evolver(tmp_path, fake)
    result = ev.evolve("tool", "查快递物流")
    assert result == "ping_check@v0.1"
    assert "验证失败" in fake.prompts[1][-1]["content"]


def test_evolve_tool_prompt_mentions_primitives(tmp_path):
    ev = _make_evolver(tmp_path, FakeBrain([]))
    text = ev._tool_prompt("查快递")[0]["content"]
    assert "ctx.web_search" in text and "禁止 import" in text and "TOOL_NAME" in text
    assert "ctx.skill_status" in text and "ctx.skill_setup" in text and "ctx.skill_auth" in text
    assert "ctx.skill_exec" in text
    assert "ctx.sandbox_read" in text and "ctx.sandbox_write" in text
    assert "ctx.sandbox_list" in text and "ctx.sandbox_run" in text


def test_evolve_tool_skill_lifecycle(tmp_path, monkeypatch):
    """自升级验证：程序自己生成技能管理工具（只用 ctx.skill_* 原语），
    不修改任何源码，安装后即可通过 tools.execute 使用。"""
    import json

    import tools as tools_mod
    from kernel.permission import SOURCE_USER

    src = '''\
TOOL_NAME = "zhihu_cli"
TOOL_DESCRIPTION = "管理知乎技能：状态检查 / 初始化 / 认证"
TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["status", "setup", "auth"]},
        "name": {"type": "string"},
        "secret": {"type": "string"},
    },
    "required": ["action", "name"],
}


def handler(args, ctx):
    action = str(args.get("action", "")).strip()
    name = str(args.get("name", "") or "zhihu").strip()
    if action == "status":
        return ctx.skill_status(name)
    if action == "setup":
        return ctx.skill_setup(name)
    if action == "auth":
        return ctx.skill_auth(name, str(args.get("secret", "")).strip())
    return "未知 action: " + action
'''
    fake = FakeBrain(["```python\n" + src + "\n```"])
    ev = _make_evolver(tmp_path, fake)
    result = ev.evolve("tool", "给 zhihu 技能增加管理工具：状态检查/初始化/认证")
    assert result == "zhihu_cli@v0.1"
    assert ev.updater.list_tools() == ["zhihu_cli"]

    # 模拟真实技能目录（含 scripts），并让 tools 从 updater 的 tools 目录发现新工具
    skills = tmp_path / "skills"
    (skills / "zhihu" / "scripts").mkdir(parents=True)
    (skills / "zhihu" / "SKILL.md").write_text(
        "---\nname: zhihu\ndescription: 测试\n---\n", encoding="utf-8"
    )
    (skills / "zhihu" / "scripts" / "run.ps1").write_text(
        'Write-Output "EVOLVED-STATUS"\n', encoding="utf-8"
    )
    (skills / "zhihu" / "scripts" / "run.sh").write_text(
        '#!/bin/sh\necho EVOLVED-STATUS\n', encoding="utf-8"
    )
    (skills / "zhihu" / "scripts" / "setup.ps1").write_text(
        'Write-Output "EVOLVED-SETUP"\n', encoding="utf-8"
    )
    (skills / "zhihu" / "scripts" / "setup.sh").write_text(
        '#!/bin/sh\necho EVOLVED-SETUP\n', encoding="utf-8"
    )

    monkeypatch.setattr(tools_mod, "_skills_dir", lambda: skills)
    monkeypatch.setattr(tools_mod, "_tools_dir", lambda: ev.updater.root.parent / "tools")
    out = tools_mod.execute(
        "zhihu_cli", json.dumps({"action": "status", "name": "zhihu"}),
        mode="confirm", source=SOURCE_USER, confirm_cb=lambda _d: True,
    )
    assert "EVOLVED-STATUS" in out


def test_extract_code_variants(tmp_path):
    from brain.evolver import Evolver

    src = "TOOL_NAME = \"x\"\ndef handler(args, ctx):\n    return \"ok\""
    # 标准 ```python 围栏
    assert Evolver._extract_code(f"好的：\n```python\n{src}\n```") == src
    # 嵌套用四反引号（升级 prompt 内嵌现有源码时的常见行为）
    assert Evolver._extract_code(f"```python\n{src}\n````") == src
    # 无语言标签
    assert Evolver._extract_code(f"```\n{src}\n```") == src
    # 无围栏兜底：从 TOOL_NAME 截取
    assert Evolver._extract_code("这是你要的工具：\n" + src) == src
    # 纯解释文字 → 空
    assert Evolver._extract_code("抱歉，我不能生成代码。") == ""


def test_evolve_tool_upgrade(tmp_path):
    """升级语法：先装 v0.1 → '升级 ping_check：支持超时' → v0.2，prompt 带现有源码。"""
    fake = FakeBrain([
        "```python\n" + TOOL_SRC + "\n```",
        "```python\n" + TOOL_SRC_V2 + "\n```",
    ])
    ev = _make_evolver(tmp_path, fake)
    assert ev.evolve("tool", "查快递物流") == "ping_check@v0.1"
    result = ev.evolve("tool", "升级 ping_check：支持超时参数")
    assert result == "ping_check@v0.2"
    assert ev.updater.list_versions("ping_check") == ["v0.1", "v0.2"]
    assert ev.updater.active_version("ping_check") == "v0.2"
    # 升级 prompt 以现有 active 源码为基准
    assert "当前完整源码" in fake.prompts[1][-1]["content"]
    assert "升级基准" in fake.prompts[1][-1]["content"]


def test_evolve_tool_upgrade_after_agent_clean(tmp_path):
    """agent 聊天清洗后形式（无“升级”前缀）：'ping_check：支持超时' 仍识别为升级。"""
    fake = FakeBrain([
        "```python\n" + TOOL_SRC + "\n```",
        "```python\n" + TOOL_SRC_V2 + "\n```",
    ])
    ev = _make_evolver(tmp_path, fake)
    assert ev.evolve("tool", "查快递物流") == "ping_check@v0.1"
    result = ev.evolve("tool", "ping_check：支持超时参数")
    assert result == "ping_check@v0.2"


def test_evolve_tool_upgrade_unknown(tmp_path):
    ev = _make_evolver(tmp_path, FakeBrain([]))
    try:
        ev.evolve("tool", "升级 nope_tool：加参数")
        raise AssertionError("应拒绝升级不存在的工具")
    except ValueError as exc:
        assert "没有已安装的工具「nope_tool」" in str(exc)
    assert ev.updater.list_tools() == []


def test_evolve_tool_upgrade_rename_rejected(tmp_path):
    """升级候选改名 → 验证失败重试耗尽后放弃，v0.1 保持 active。"""
    renamed = TOOL_SRC.replace('TOOL_NAME = "ping_check"', 'TOOL_NAME = "rename_check"')
    fake = FakeBrain([
        "```python\n" + TOOL_SRC + "\n```",
        "```python\n" + renamed + "\n```",
        "```python\n" + renamed + "\n```",
        "```python\n" + renamed + "\n```",
    ])
    ev = _make_evolver(tmp_path, fake)
    assert ev.evolve("tool", "查快递物流") == "ping_check@v0.1"
    try:
        ev.evolve("tool", "升级 ping_check：支持超时参数")
        raise AssertionError("改名升级应被拒绝")
    except ValueError as exc:
        assert "不能改名" in str(exc)
    assert ev.updater.active_version("ping_check") == "v0.1"


def test_evolve_intent_tool_upgrade_shortcut(tmp_path):
    """'升级 ping_check：加超时'（无“工具”字样）→ 按已装工具名解析为 tool 升级。"""
    a = _make_agent(tmp_path, with_updater=True)
    a.evolver.updater.install_candidate("tool", _tool_cand(tmp_path, TOOL_SRC))
    seen = {}
    a.evolver.evolve = (
        lambda name, req, on_status=None: seen.update(name=name, req=req) or "ping_check@v0.2"
    )
    reply = a._try_evolve_intent("升级 ping_check：支持超时参数")
    assert seen.get("name") == "tool", seen
    assert "已升级到 v0.2" in reply and "ping_check" in reply



# ---------- 阶段3：brain 包级进化（子模块级整文件重写） ----------

AGENT_CHAT_SRC = (ROOT / "brain" / "agent_chat.py").read_text(encoding="utf-8")
# 模拟 LLM 输出：TARGET 声明 + 围栏代码（带一个标记注释，验证确实替换了该文件）
AGENT_CHAT_EVOLVED = (
    "# 进化标记：2026-08-12 包级生成测试\n" + AGENT_CHAT_SRC
)
BRAIN_TARGET_OUTPUT = (
    "TARGET: agent_chat.py\n```python\n" + AGENT_CHAT_EVOLVED + "\n```"
)


def test_evolve_brain_package(tmp_path):
    """包级进化：LLM 选 agent_chat.py 重写 → 组装候选包 → 验证安装 v1.1。"""
    ev = _make_evolver(tmp_path, FakeBrain([BRAIN_TARGET_OUTPUT]))
    version = ev.evolve("brain", "聊天时更热情一点")
    assert version == "v1.1"
    assert ev.updater.active_version("brain") == "v1.1"
    files = ev.updater.source_files("brain")
    assert "进化标记：2026-08-12 包级生成测试" in files["agent_chat.py"]
    # 其它文件未被修改
    assert "class Agent" in files["agent.py"]
    assert "class Planner" in files["planner.py"]
    # 候选目录已清理
    assert not list(ev.candidate_root.glob("*"))


def test_evolve_brain_rejects_unknown_target(tmp_path):
    """LLM 输出无法确定 TARGET → 拒绝（不安装任何东西）。"""
    fake = FakeBrain([
        "```python\nclass SomethingElse:\n    pass\n```"
    ])
    ev = _make_evolver(tmp_path, fake)
    try:
        ev.evolve("brain", "随便改改")
        raise AssertionError("应拒绝无法确定 TARGET 的输出")
    except ValueError as exc:
        assert "无法确定要替换的包内文件" in str(exc)
    assert ev.updater.active_version("brain") == "v1.0"


class _Patch:
    """迷你 monkeypatch：记录 setattr 并在 restore 时还原（供 runner 传参）。"""

    def __init__(self):
        self._saved = []

    def setattr(self, target, name, value):
        self._saved.append((target, name, getattr(target, name, None)))
        setattr(target, name, value)

    def restore(self):
        for target, name, old in reversed(self._saved):
            if old is None and hasattr(target, name):
                delattr(target, name)
            else:
                setattr(target, name, old)


if __name__ == "__main__":
    import inspect
    import tempfile
    import traceback

    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            tmp = tempfile.mkdtemp(prefix="evolve_test_")
            patch = _Patch()
            try:
                params = list(inspect.signature(fn).parameters)
                if "monkeypatch" in params:
                    fn(Path(tmp), patch)
                else:
                    fn(Path(tmp))
                print(f"PASS {name}")
            except Exception:
                failed += 1
                print(f"FAIL {name}")
                traceback.print_exc()
            finally:
                patch.restore()
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} FAILED")
    sys.exit(1 if failed else 0)
