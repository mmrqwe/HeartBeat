"""记忆纠错测试：用户纠正之前的说法时更新旧记忆而非新增。

覆盖：规则层正反向句式、旧说法黑名单、LLM [FIX] 三级定位、
语义合并兜底、重叠字修复、结构校验边界。可直接运行，也可用 pytest。
"""

import inspect
import json
import sys
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent
import core
import db as dbmod
from brain.memory import MemoryModule, _fact_terms, _texts_same_event
from db import _merge_replace


class _Patch:
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


def _cfg(with_key=False):
    cfg = json.loads(json.dumps(core.DEFAULT_CONFIG))
    cfg["embedding_enabled"] = False
    cfg["api"]["api_key"] = "test-key" if with_key else ""
    return cfg


def _make_agent(tmp_dir, with_key=False):
    return agent.Agent(
        _cfg(with_key=with_key),
        data_dir=tmp_dir,
        clock=lambda: datetime(2026, 8, 10, 10, 0),
    )


def _facts(a):
    return [i["text"] for i in a.db.memory_items(roles=("fact",), limit=None)]


def _first_id(a):
    return a.db.memory_items(roles=("fact",), limit=None)[0]["id"]


# ---------- 规则层：双向句式 ----------

def test_forward_swap_updates(tmp_path):
    """“我说错了，是Y，不是X”原地更新，不新增。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    saved = a.memory_module.extract_facts("我说错了，是长电科技，不是长江电力")
    assert saved == 1
    assert _facts(a) == ["主人持有长电科技"]


def test_reverse_swap_updates(tmp_path):
    """“买的是Y，不是X”（反向句式、无触发词）原地更新。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    saved = a.memory_module.extract_facts("买的是长电科技，不是长江电力")
    assert saved == 1
    assert _facts(a) == ["主人持有长电科技"]


def test_reverse_bare_updates(tmp_path):
    """“Y，不是X”（句首反向、无“是”前导）原地更新。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    saved = a.memory_module.extract_facts("长电科技，不是长江电力")
    assert saved == 1
    assert _facts(a) == ["主人持有长电科技"]


def test_correction_cue_variants(tmp_path):
    m = _make_agent(tmp_path).memory_module
    assert m._has_correction_cue("长电科技，不是长江电力") is True
    assert m._has_correction_cue("买的是长电科技，不是长江电力") is True
    assert m._has_correction_cue("不是长江电力，是长电科技") is True
    assert m._has_correction_cue("我记错了，是长电科技") is True
    assert m._has_correction_cue("今天天气不错") is False


def test_mixed_sentence_correction_plus_fact(tmp_path):
    """混合句子：纠正与普通规则提取互不吞并。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    saved = a.memory_module.extract_facts(
        "我喜欢打羽毛球。对了，我买的是长电科技，不是长江电力"
    )
    assert saved == 2
    assert sorted(_facts(a)) == sorted(["主人持有长电科技", "主人喜欢打羽毛球"])


def test_old_term_not_reintroduced(tmp_path):
    """被纠正的旧说法不得以裸词重新入库（excluded 黑名单）。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人喜欢喝咖啡", category="preference")
    a.memory_module.extract_facts("不是咖啡，是喝茶")
    assert _facts(a) == ["主人喜欢喝茶"]


def test_forget_plus_new_preference(tmp_path):
    """“忘掉X，新偏好”删除 X 且同句新偏好照常入库。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人喜欢喝咖啡", category="preference")
    saved = a.memory_module.extract_facts("忘掉咖啡，我喜欢喝茶")
    assert saved == 1
    assert _facts(a) == ["主人喜欢喝茶"]


def test_reverse_short_term_guard(tmp_path):
    """反向句式短词/指示代词不误伤旧记忆（LLM 路径兜底）。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人的老师是张三", category="misc")
    a.memory_module.extract_facts("我是学生，不是老师")
    assert "主人的老师是张三" in _facts(a)

    (tmp_path / "b").mkdir()
    b = _make_agent(tmp_path / "b")
    b.db.add_memory("fact", "主人喜欢这样的设计", category="preference")
    b.memory_module.extract_facts("这个功能不是这样的")
    assert _facts(b) == ["主人喜欢这样的设计"]


def test_merge_replace_overlap():
    """重叠字修复：喝咖啡→喝茶 不产生“喝喝茶”。"""
    assert _merge_replace("主人喜欢喝咖啡", "咖啡", "喝茶") == "主人喜欢喝茶"
    assert _merge_replace("主人持有长江电力", "长江电力", "长电科技") == "主人持有长电科技"
    assert _merge_replace("主人喜欢咖啡豆", "咖啡", "咖啡豆") == "主人喜欢咖啡豆"


# ---------- LLM [FIX] 定位链 ----------

def test_apply_fix_exact(tmp_path):
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    ok = a.memory_module._apply_fix("主人持有长江电力", "主人持有长电科技")
    assert ok and _facts(a) == ["主人持有长电科技"]


def test_apply_fix_rewording_token_locate(tmp_path):
    """LLM 引用的旧原文与库内措辞不同 → 核心词定位更新。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    ok = a.memory_module._apply_fix("主人买了长江电力", "主人买了长电科技")
    assert ok and _facts(a) == ["主人买了长电科技"]


def test_apply_fix_missing_returns_false(tmp_path):
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人养了一只猫", category="preference")
    ok = a.memory_module._apply_fix("主人持有长江电力", "主人持有长电科技")
    assert ok is False
    assert _facts(a) == ["主人养了一只猫"]


def test_remember_fact_correction_fallback_add(tmp_path):
    """无向量环境：定位失败时回退普通新增（不丢事实）。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人喜欢喝咖啡", category="preference")
    ok = a.memory_module._remember_fact_correction("主人喜欢喝茶", "preference")
    assert ok
    assert sorted(_facts(a)) == sorted(["主人喜欢喝咖啡", "主人喜欢喝茶"])


def test_remember_fact_correction_semantic_update(tmp_path):
    """语义近邻命中时更新旧记忆而非新增。"""
    a = _make_agent(tmp_path)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    old_id = _first_id(a)
    a.memory_module._vec_locate_fact = lambda new_text: old_id
    ok = a.memory_module._remember_fact_correction("主人持有长电科技", "finance")
    assert ok and _facts(a) == ["主人持有长电科技"]


def test_analyze_and_remember_fix_updates(tmp_path, monkeypatch):
    """LLM 分析器输出 [FIX:旧] 新 → 更新而非新增。"""
    a = _make_agent(tmp_path, with_key=True)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")

    class FakeBrain:
        def complete(self, messages, max_tokens=None, **kw):
            return "[FIX:主人持有长江电力] 主人持有长电科技"

    a.brain = FakeBrain()
    saved = a.memory_module.analyze_and_remember("我说错了，买的是长电科技")
    assert saved == 1
    assert _facts(a) == ["主人持有长电科技"]


def test_analyze_and_remember_fix_rewording_updates(tmp_path):
    """LLM 旧原文措辞有差异 → 核心词定位仍更新。"""
    a = _make_agent(tmp_path, with_key=True)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")

    class FakeBrain:
        def complete(self, messages, max_tokens=None, **kw):
            return "[FIX:主人买了长江电力] 主人买了长电科技"

    a.brain = FakeBrain()
    saved = a.memory_module.analyze_and_remember("我说错了，买的是长电科技")
    assert saved == 1
    assert _facts(a) == ["主人买了长电科技"]


def test_analyze_and_remember_injects_memory_brief(tmp_path):
    """纠正场景注入现有记忆清单 + 强化指令，供 LLM 定位。"""
    a = _make_agent(tmp_path, with_key=True)
    a.db.add_memory("fact", "主人持有长江电力", category="finance")
    seen = {}

    class FakeBrain:
        def complete(self, messages, max_tokens=None, **kw):
            seen["user"] = messages[-1]["content"]
            return "[NONE]"

    a.brain = FakeBrain()
    a.memory_module.analyze_and_remember("我说错了")
    assert "现有记忆" in seen["user"]
    assert "主人持有长江电力" in seen["user"]
    assert "禁止输出 FACT" in seen["user"]


# ---------- 结构校验 / 核心词 ----------

def test_texts_same_event_boundaries():
    # 只差专名 → 接受
    assert _texts_same_event("主人持有长江电力", "主人持有长电科技") is True
    # 措辞改写 → 接受
    assert _texts_same_event("主人喜欢喝咖啡", "主人爱喝咖啡") is True
    assert _texts_same_event("主人明天开会", "主人明天考试") is True
    # 动作不同 + 专名不同 → 拒绝
    assert _texts_same_event("主人关注长江电力", "主人持有长电科技") is False
    # 完全无关 → 拒绝
    assert _texts_same_event("主人明天开会", "主人养了一只猫") is False
    # 空 / 相同 → 拒绝
    assert _texts_same_event("", "主人喜欢喝茶") is False
    assert _texts_same_event("主人喜欢喝茶", "主人喜欢喝茶") is False


def test_fact_terms():
    assert _fact_terms("主人买了长江电力") == ["长江电力"]
    assert _fact_terms("主人持有长江电力") == ["长江电力"]
    assert _fact_terms("") == []


# ---------- runner ----------

def _run_plain():
    failures = []
    patch = _Patch()
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            params = list(inspect.signature(fn).parameters)
            try:
                if "tmp_path" in params:
                    with TemporaryDirectory() as d:
                        kwargs = {"tmp_path": Path(d)}
                        if "monkeypatch" in params:
                            kwargs["monkeypatch"] = patch
                        fn(**kwargs)
                elif "monkeypatch" in params:
                    fn(patch)
                elif params and params[0] == "db_file":
                    with TemporaryDirectory() as d:
                        fn(dbmod.Database(Path(d) / "t.db"))
                else:
                    fn()
                print(f"PASS {name}")
            except Exception as exc:
                failures.append((name, exc))
                print(f"FAIL {name}: {exc}")
        patch.restore()
    if failures:
        raise SystemExit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    _run_plain()
