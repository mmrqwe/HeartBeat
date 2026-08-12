"""test_smoke_diff.py：P4 行为差分冒烟测试。

覆盖：同源候选通过；返回收窄/新异常/丢字段 FAIL 拦截；语义拓宽 WARNING 放行。
纯本地（无网络无 LLM），用 func_replacer 构造"行为被破坏"的候选。

跑法：python test_smoke_diff.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from brain.func_replacer import replace_function
from brain.smoke import smoke_test_module
from kernel.updater import Updater

ROOT = Path(__file__).parent.parent


def _make_updater(data_dir):
    upd = Updater(data_dir)
    upd.smoke_runner = smoke_test_module
    upd.ensure_installed()
    return upd


def _candidate(tmp_path, name, target, new_func):
    """宿主源码替换 target 方法 → 候选目录。"""
    src = (ROOT / "brain" / f"{name}.py").read_text(encoding="utf-8")
    new_src = replace_function(src, target, new_func)
    cand = Path(tmp_path) / "cand"
    cand.mkdir(exist_ok=True)
    (cand / f"{name}.py").write_text(new_src, encoding="utf-8")
    return cand


def test_diff_same_source_passes(tmp_path):
    upd = _make_updater(tmp_path)
    cand = Path(tmp_path) / "cand"
    cand.mkdir(exist_ok=True)
    (cand / "memory.py").write_text(
        (ROOT / "brain" / "memory.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    ok, errors = upd.validate_candidate("memory", cand)
    assert ok, errors


def test_diff_return_narrowing_rejected(tmp_path):
    """relevant 返回从 list 收窄为 None → FAIL 拦截。"""
    upd = _make_updater(tmp_path)
    cand = _candidate(tmp_path, "memory", "relevant",
                      "    def relevant(self, text, limit=5):\n        return None\n")
    ok, errors = upd.validate_candidate("memory", cand)
    assert not ok, "返回收窄应被拦截"
    assert any("行为差分" in e or "记忆检索为空" in e for e in errors), errors


def test_diff_new_exception_rejected(tmp_path):
    """profile 从正常返回改为抛异常 → 拦截（基础冒烟或差分）。"""
    upd = _make_updater(tmp_path)
    cand = _candidate(tmp_path, "memory", "profile",
                      "    def profile(self):\n        raise RuntimeError('炸了')\n")
    ok, errors = upd.validate_candidate("memory", cand)
    assert not ok, "新异常应被拦截"
    assert any("冒烟" in e or "行为差分" in e for e in errors), errors


def test_diff_exception_type_change_rejected(tmp_path):
    """greeting 改为抛异常 → 拦截（基础冒烟或差分）。"""
    upd = _make_updater(tmp_path)
    cand = _candidate(tmp_path, "planner", "greeting",
                      "    def greeting(self, now):\n        raise ValueError('x')\n")
    ok, errors = upd.validate_candidate("planner", cand)
    assert not ok, "异常行为应被拦截"
    assert any("冒烟" in e or "行为差分" in e for e in errors), errors


def test_diff_warning_allowed(tmp_path):
    """语义拓宽（greeting 从 None 变为固定字符串）→ WARNING 放行。"""
    upd = _make_updater(tmp_path)
    cand = _candidate(tmp_path, "planner", "greeting",
                      "    def greeting(self, now):\n        return '嗨！'\n")
    ok, errors = upd.validate_candidate("planner", cand)
    assert ok, f"语义拓宽应放行：{errors}"


def test_diff_missing_dict_field_rejected(tmp_path):
    """profile 返回形状变化（active 返回 str）→ FAIL 拦截。"""
    upd = _make_updater(tmp_path)
    cand = _candidate(tmp_path, "memory", "profile",
                      "    def profile(self):\n        return {'name': '测试员'}\n")
    ok, errors = upd.validate_candidate("memory", cand)
    assert not ok, "返回形状变化应被差分拦截"
    assert any("行为差分" in e and ("缺少字段" in e or "返回类型变化" in e) for e in errors), errors


if __name__ == "__main__":
    passed = failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                with tempfile.TemporaryDirectory() as tmp:
                    fn(Path(tmp))
                print(f"PASS {name}")
                passed += 1
            except Exception as exc:
                print(f"FAIL {name}: {exc}")
                failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
