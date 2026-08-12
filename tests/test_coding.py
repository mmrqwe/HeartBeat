"""Coding Agent P0 测试：文件工具 / 档位门控 / 备份 / 后台进程池 / 控制循环。

覆盖：
- tools.execute 的 9 个 coding 工具（路径校验、敏感拒绝、confirm 门控、备份）；
- coding_declarations / tool_declarations 的声明接入；
- kernel.processpool 后台进程（轮询 / 取消 / 并发上限）；
- brain.coding_agent.run_coding_task 循环（mock LLM：多步 / 异常 / 轮次耗尽）。
"""

import json
import os
import subprocess as sp
import time
from pathlib import Path

import pytest

import tools
from brain import coding_agent
from kernel import processpool


def _signals_permitted():
    """探测当前环境是否允许向子进程发信号。

    CodePapr 沙箱等受限环境会返回 EPERM（连 kill(pid, 0) 都拒绝）——
    这种环境下 kill 类断言必须跳过；真实桌面应用无此限制。
    """
    proc = sp.Popen(
        ["sleep", "5"], stdout=sp.PIPE, stderr=sp.STDOUT, stdin=sp.DEVNULL,
    )
    try:
        os.kill(proc.pid, 0)
        try:
            proc.terminate()
        except OSError:
            pass
        return True
    except (PermissionError, OSError):
        return False
    finally:
        try:
            proc.wait(timeout=8)
        except Exception:
            pass


SIGNALS_OK = _signals_permitted()


# ---------- 公共工具 ----------


def _cfg(mode="confirm", project_dir=None):
    cfg = {
        "shell_tools_mode": mode,
        "shell_workdir": "",
        "project_dir": project_dir or "",
        "api": {"base_url": "", "api_key": "", "model": ""},
    }
    return cfg


def _exec(name, args, mode="confirm", project_dir=None, source="user",
          confirm=None, audit=None):
    """tools.execute 的便捷封装：confirm 默认放行写操作。"""
    return tools.execute(
        name, json.dumps(args), mode=mode,
        source=source,
        confirm_cb=confirm if confirm is not None else (lambda _d: True),
        project_dir=str(project_dir) if project_dir else None,
        audit=audit,
    )


def _make_project(tmp_path):
    proj = tmp_path / "proj"
    (proj / "src").mkdir(parents=True)
    (proj / "src" / "main.py").write_text(
        "def hello():\n    return 'hi'\n\nprint(hello())\n", encoding="utf-8"
    )
    (proj / "src" / "util.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8"
    )
    (proj / "README.md").write_text("# Demo\n", encoding="utf-8")
    (proj / ".git").mkdir()  # 应被目录遍历跳过
    (proj / "bin.dat").write_bytes(b"\x00\x01\x02" + b"x" * 20)
    return proj


# ---------- 只读文件工具 ----------


def test_read_file_happy(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("read_file", {"path": "src/main.py"}, project_dir=proj)
    assert "def hello():" in text
    assert "|" in text  # 带行号
    assert "return 'hi'" in text


def test_read_file_not_found(tmp_path):
    proj = _make_project(tmp_path)
    assert "文件不存在" in _exec("read_file", {"path": "nope.py"}, project_dir=proj)


def test_read_file_traversal_rejected(tmp_path):
    proj = _make_project(tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    for bad in ("../secret.txt", "../../secret.txt", "/etc/passwd", "~/x"):
        text = _exec("read_file", {"path": bad}, project_dir=proj)
        assert "SECRET" not in text
        assert "拒绝" in text or "越界" in text or "相对路径" in text, bad


def test_read_file_sensitive_rejected(tmp_path):
    proj = _make_project(tmp_path)
    (proj / ".env").write_text("KEY=abc", encoding="utf-8")
    text = _exec("read_file", {"path": ".env"}, project_dir=proj)
    assert "敏感" in text


def test_read_file_binary_skipped(tmp_path):
    proj = _make_project(tmp_path)
    assert "二进制" in _exec("read_file", {"path": "bin.dat"}, project_dir=proj)


def test_read_file_off_mode_denied(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("read_file", {"path": "src/main.py"}, mode="off", project_dir=proj)
    assert "已关闭" in text


def test_read_file_no_project_dir(tmp_path):
    text = _exec("read_file", {"path": "src/main.py"}, project_dir=None)
    assert "项目目录" in text


def test_list_files_tree_and_skips(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("list_files", {"path": "."}, project_dir=proj)
    assert "src/" in text
    assert "main.py" in text
    assert ".git" not in text
    assert "bin.dat" in text  # 文件不按扩展名过滤，只有目录被跳过


def test_list_files_depth_cap(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("list_files", {"path": ".", "depth": 1}, project_dir=proj)
    assert "src/" in text
    assert "main.py" not in text  # 深度 1 不应进入 src 内部


def test_search_files_match(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("search_files", {"pattern": "return"}, project_dir=proj)
    assert "src/main.py:" in text
    assert "src/util.py:" in text


def test_search_files_glob_and_no_match(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec(
        "search_files", {"pattern": "return", "file_glob": "util.py"},
        project_dir=proj,
    )
    assert "util.py:" in text
    assert "main.py:" not in text
    text = _exec("search_files", {"pattern": "zzz_nothing"}, project_dir=proj)
    assert "没有匹配" in text


def test_search_files_invalid_regex(tmp_path):
    proj = _make_project(tmp_path)
    assert "正则无效" in _exec("search_files", {"pattern": "("}, project_dir=proj)


def test_glob_match(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("glob_match", {"pattern": "**/*.py"}, project_dir=proj)
    assert "src/main.py" in text
    assert "src/util.py" in text
    assert "README.md" not in text


def test_glob_match_escape_rejected(tmp_path):
    proj = _make_project(tmp_path)
    for bad in ("/etc/passwd", "~/.ssh", "../secret.txt"):
        text = _exec("glob_match", {"pattern": bad}, project_dir=proj)
        assert "限制在项目目录内" in text, bad


# ---------- 写文件工具（confirm 档 + 备份） ----------


def test_write_file_create(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    backups = tmp_path / "backups"
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    text = _exec(
        "write_file", {"path": "new.txt", "content": "hello"},
        project_dir=proj,
    )
    assert "已写入" in text
    assert (proj / "new.txt").read_text(encoding="utf-8") == "hello"
    assert not backups.exists() or list(backups.iterdir()) == []  # 新建无需备份


def test_write_file_overwrite_backs_up(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    backups = tmp_path / "backups"
    _exec(
        "write_file", {"path": "src/main.py", "content": "NEW CONTENT"},
        project_dir=proj,
    )
    assert (proj / "src" / "main.py").read_text(encoding="utf-8") == "NEW CONTENT"
    ts_dirs = sorted(p.name for p in backups.iterdir())
    assert len(ts_dirs) == 1
    backup_file = backups / ts_dirs[0] / "src" / "main.py"
    assert backup_file.is_file()
    assert "def hello()" in backup_file.read_text(encoding="utf-8")


def test_write_file_confirm_declined(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    text = _exec(
        "write_file", {"path": "src/main.py", "content": "X"},
        project_dir=proj, confirm=lambda _d: False,
    )
    assert "未确认" in text
    assert "def hello()" in (proj / "src" / "main.py").read_text(encoding="utf-8")


def test_write_file_full_mode_auto_approve(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    # full 档与 run_bash 语义一致：不弹确认（confirm_cb 返回 False 也不拦）
    text = _exec(
        "write_file", {"path": "src/main.py", "content": "AUTO"},
        mode="full", project_dir=proj, confirm=lambda _d: False,
    )
    assert "已写入" in text
    assert (proj / "src" / "main.py").read_text(encoding="utf-8") == "AUTO"


def test_edit_file_full_mode_auto_approve(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    text = _exec(
        "edit_file",
        {"path": "src/main.py", "search": "return 'hi'", "replace": "return 'auto'"},
        mode="full", project_dir=proj, confirm=lambda _d: False,
    )
    assert "已编辑" in text
    assert "return 'auto'" in (proj / "src" / "main.py").read_text(encoding="utf-8")


def test_write_file_readonly_mode_denied(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("write_file", {"path": "a.txt", "content": "x"},
                 mode="readonly", project_dir=proj)
    assert "不允许写文件" in text
    assert not (proj / "a.txt").exists()


def test_write_file_auto_source_denied(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("write_file", {"path": "a.txt", "content": "x"},
                 source="auto", project_dir=proj)
    assert "主人在场" in text
    assert not (proj / "a.txt").exists()


def test_write_file_too_large_rejected(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("write_file", {"path": "a.txt", "content": "x" * (2 * 1024 * 1024 + 1)},
                 project_dir=proj)
    assert "上限" in text


def test_write_file_traversal_rejected(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("write_file", {"path": "../outside.txt", "content": "x"},
                 project_dir=proj)
    assert "拒绝" in text or "越界" in text
    assert not (tmp_path / "outside.txt").exists()


def test_write_file_dir_target_rejected(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("write_file", {"path": "src", "content": "x"}, project_dir=proj)
    assert "目录" in text


def test_edit_file_unique_anchor(tmp_path, monkeypatch):
    proj = _make_project(tmp_path)
    monkeypatch.setattr(tools, "user_data_dir", lambda: str(tmp_path))
    backups = tmp_path / "backups"
    text = _exec(
        "edit_file",
        {"path": "src/main.py", "search": "return 'hi'", "replace": "return 'hello'"},
        project_dir=proj,
    )
    assert "已编辑" in text
    assert "备份" in text
    content = (proj / "src" / "main.py").read_text(encoding="utf-8")
    assert "return 'hello'" in content
    assert "return 'hi'" not in content
    assert any((backups / d / "src" / "main.py").is_file()
               for d in [p.name for p in backups.iterdir()])


def test_edit_file_ambiguous_anchor(tmp_path):
    proj = _make_project(tmp_path)
    (proj / "src" / "util.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
    text = _exec(
        "edit_file", {"path": "src/util.py", "search": "= 1", "replace": "= 2"},
        project_dir=proj,
    )
    assert "不唯一" in text


def test_edit_file_expected_occurrences_batch(tmp_path):
    proj = _make_project(tmp_path)
    (proj / "src" / "util.py").write_text("x = 1\ny = 1\n", encoding="utf-8")
    text = _exec(
        "edit_file",
        {"path": "src/util.py", "search": "= 1", "replace": "= 2",
         "expected_occurrences": 2},
        project_dir=proj,
    )
    assert "替换 2 处" in text
    content = (proj / "src" / "util.py").read_text(encoding="utf-8")
    assert "= 1" not in content


def test_edit_file_anchor_not_found(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec(
        "edit_file", {"path": "src/main.py", "search": "不存在的内容", "replace": "x"},
        project_dir=proj,
    )
    assert "锚点未找到" in text


def test_edit_file_no_change(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec(
        "edit_file", {"path": "src/main.py", "search": "print(hello())",
                      "replace": "print(hello())"},
        project_dir=proj,
    )
    assert "无变化" in text


# ---------- 声明 ----------


def test_coding_declarations_include_9_tools():
    cfg = _cfg("confirm")
    names = {d["function"]["name"] for d in tools.coding_declarations(cfg)}
    for t in tools.CODING_TOOLS:
        assert t in names
    assert "web_search" in names
    assert "run_bash" in names


def test_coding_declarations_off_mode_readonly_only():
    cfg = _cfg("off")
    names = {d["function"]["name"] for d in tools.coding_declarations(cfg)}
    assert names & tools.CODING_TOOLS == set()
    assert "run_bash" not in names
    assert "web_search" in names


def test_tool_declarations_coding_gated_by_project_dir():
    cfg = _cfg("confirm")
    names = {d["function"]["name"] for d in tools.tool_declarations(cfg)}
    assert "read_file" not in names
    cfg["project_dir"] = "/tmp"
    names = {d["function"]["name"] for d in tools.tool_declarations(cfg)}
    assert "read_file" in names
    assert "bg_exec" in names


# ---------- 后台进程工具（kernel.processpool） ----------


def test_bg_exec_check_done(tmp_path):
    proj = _make_project(tmp_path)
    pid_text = _exec("bg_exec", {"command": "sleep 0.3 && echo DONE_MARK"},
                     project_dir=proj)
    assert "后台任务已启动" in pid_text
    pid = pid_text.split("：")[1].split("（")[0].strip()
    deadline = time.time() + 8
    result = ""
    while time.time() < deadline:
        result = _exec("bg_check", {"task_id": pid}, project_dir=proj)
        if "exit=0" in result:
            break
        time.sleep(0.2)
    assert "exit=0" in result
    assert "DONE_MARK" in result


def test_bg_exec_cancel(tmp_path):
    proj = _make_project(tmp_path)
    pid_text = _exec("bg_exec", {"command": "sleep 0.3"}, project_dir=proj)
    pid = pid_text.split("：")[1].split("（")[0].strip()
    assert "已请求取消" in _exec("bg_cancel", {"task_id": pid}, project_dir=proj)
    deadline = time.time() + 8
    status = ""
    while time.time() < deadline:
        status = _exec("bg_check", {"task_id": pid}, project_dir=proj)
        if "killed" in status or "exit=0" in status:
            break
        time.sleep(0.1)
    # 信号可用时进程被取消（killed）；沙箱禁止信号时进程自然结束（exit=0）
    assert "killed" in status or "exit=0" in status


@pytest.mark.skipif(not SIGNALS_OK, reason="环境禁止向子进程发信号（沙箱）")
def test_bg_exec_cancel_kills_process(tmp_path):
    proj = _make_project(tmp_path)
    pid_text = _exec("bg_exec", {"command": "sleep 30"}, project_dir=proj)
    pid = pid_text.split("：")[1].split("（")[0].strip()
    assert "已请求取消" in _exec("bg_cancel", {"task_id": pid}, project_dir=proj)
    deadline = time.time() + 8
    status = ""
    while time.time() < deadline:
        status = _exec("bg_check", {"task_id": pid}, project_dir=proj)
        if "killed" in status:
            break
        time.sleep(0.2)
    assert "killed" in status


def test_bg_exec_classify_hard_block(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("bg_exec", {"command": "sudo rm -rf /"}, project_dir=proj)
    assert "拒绝" in text


def test_bg_exec_readonly_mode_denied(tmp_path):
    proj = _make_project(tmp_path)
    text = _exec("bg_exec", {"command": "echo hi"}, mode="readonly", project_dir=proj)
    assert "不允许" in text


def test_processpool_concurrency_limit(tmp_path):
    pool = processpool.BgPool(max_concurrency=1, default_timeout=30)
    pid = pool.start("sleep 2", str(tmp_path))
    try:
        with pytest.raises(processpool.PoolError):
            pool.start("sleep 1", str(tmp_path))
    finally:
        pool.cancel(pid)
        pool.cancel_all()


def test_processpool_unknown_id():
    pool = processpool.BgPool()
    assert pool.poll("nope") is None
    assert "不存在" in pool.cancel("nope")


# ---------- 控制循环（mock LLM） ----------


class _FakeBrain:
    def __init__(self, script, final_reply="完成", raise_on_call=None):
        self.script = list(script)   # 每项：(content, tool_calls)
        self.final_reply = final_reply
        self.raise_on_call = raise_on_call
        self.calls = []

    def complete_tools(self, messages, decls):
        self.calls.append(("tools", len(messages)))
        if self.raise_on_call and len(self.calls) >= self.raise_on_call:
            raise RuntimeError("boom")
        if not self.script:
            return "", []
        return self.script.pop(0)

    def complete(self, messages, max_tokens=None):
        self.calls.append(("complete", max_tokens))
        return self.final_reply


def _tc(name, args):
    return {"id": f"call_{name}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def test_coding_loop_multi_step(tmp_path):
    proj = _make_project(tmp_path)
    cfg = _cfg("confirm", project_dir=str(proj))
    brain = _FakeBrain([
        ("", [_tc("read_file", {"path": "src/main.py"})]),
        ("", [_tc("write_file", {"path": "out.txt", "content": "ok"})]),
        ("任务完成，改了 out.txt", []),
    ])
    calls = []

    def run_tool(name, arguments):
        calls.append((name, json.loads(arguments)))
        if name == "read_file":
            return "def hello():\n    pass\n"
        return "已写入"

    statuses = []
    reply = coding_agent.run_coding_task(
        brain, cfg, "帮我看看 main.py", run_tool, on_status=statuses.append,
    )
    assert reply == "任务完成，改了 out.txt"
    assert [c[0] for c in calls] == ["read_file", "write_file"]
    assert len(statuses) == 2
    assert "第 1 步" in statuses[0] and "读取文件" in statuses[0]


def test_coding_loop_llm_error_graceful(tmp_path):
    proj = _make_project(tmp_path)
    cfg = _cfg("confirm", project_dir=str(proj))
    brain = _FakeBrain([("", [_tc("read_file", {"path": "src/main.py"})])],
                       raise_on_call=2)
    reply = coding_agent.run_coding_task(
        brain, cfg, "任务", lambda n, a: "x",
    )
    assert "任务中断" in reply


def test_coding_loop_round_cap_summary(tmp_path):
    proj = _make_project(tmp_path)
    cfg = _cfg("confirm", project_dir=str(proj))
    brain = _FakeBrain(
        [("", [_tc("read_file", {"path": "src/main.py"})])],
        final_reply="收尾总结",
    )
    reply = coding_agent.run_coding_task(
        brain, cfg, "任务", lambda n, a: "x", max_rounds=2,
    )
    assert reply == "收尾总结"
    assert any(c[0] == "complete" for c in brain.calls)


def test_coding_loop_missing_project_dir(tmp_path):
    brain = _FakeBrain([])
    reply = coding_agent.run_coding_task(
        brain, _cfg("confirm"), "任务", lambda n, a: "x",
    )
    assert "project_dir" in reply
    assert not brain.calls


def test_coding_loop_project_dir_not_exist(tmp_path):
    brain = _FakeBrain([])
    cfg = _cfg("confirm", project_dir=str(tmp_path / "ghost"))
    reply = coding_agent.run_coding_task(brain, cfg, "任务", lambda n, a: "x")
    assert "不存在" in reply


def test_coding_loop_tool_exception_isolated(tmp_path):
    proj = _make_project(tmp_path)
    cfg = _cfg("confirm", project_dir=str(proj))
    brain = _FakeBrain([
        ("", [_tc("read_file", {"path": "src/main.py"})]),
        ("工具失败了，我来说明", []),
    ])

    def run_tool(name, arguments):
        raise RuntimeError("inner")

    reply = coding_agent.run_coding_task(brain, cfg, "任务", run_tool)
    assert "工具失败了，我来说明" == reply


def test_coding_loop_empty_final_falls_to_summary(tmp_path):
    proj = _make_project(tmp_path)
    cfg = _cfg("confirm", project_dir=str(proj))
    brain = _FakeBrain([("", [])], final_reply="兜底总结")
    reply = coding_agent.run_coding_task(brain, cfg, "任务", lambda n, a: "x")
    assert reply == "兜底总结"
