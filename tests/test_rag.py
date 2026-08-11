"""rag.py 包内模型注入逻辑测试。

覆盖：开发模式不注入 / frozen 注入到用户目录 / 已有模型跳过 / 拷贝失败降级。
运行：QT_QPA_PLATFORM=offscreen python -m tests.test_rag
"""

import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import rag


def _fake_bundled_model(root):
    """构造包内模型目录：models/fastembed/models--X--y/{model.onnx, tokenizer.json}。"""
    base = root / "models" / "fastembed" / "models--Qdrant--bge-small-zh-v1.5"
    (base / "snapshots" / "abc").mkdir(parents=True)
    (base / "snapshots" / "abc" / "model_optimized.onnx").write_bytes(b"x" * 100)
    (base / "snapshots" / "abc" / "tokenizer.json").write_text("{}")
    (base / "refs").mkdir()
    (base / "refs" / "main").write_text("abc")
    return base


def test_dev_mode_no_inject():
    """开发模式（无 _MEIPASS）：不拷贝任何东西，cache_dir 仍指向用户 models 目录。"""
    with TemporaryDirectory() as td:
        os.environ["HB_MODELS_DIR"] = td + "/models"
        assert not hasattr(sys, "_MEIPASS")
        emb = rag.default_embedder({}, td)
        # 开发模式：懒加载，不建目录也不拷贝
        assert not (Path(td + "/models") / "models--Qdrant--bge-small-zh-v1.5").exists()


def test_frozen_inject_to_user_dir():
    """frozen + 包内带模型：首启注入用户缓存目录，cache_dir 指向用户目录。"""
    with TemporaryDirectory() as td:
        bundled = _fake_bundled_model(Path(td))
        os.environ["HB_MODELS_DIR"] = td + "/user_models"
        setattr(sys, "_MEIPASS", td)  # 模拟 PyInstaller frozen
        try:
            emb = rag.default_embedder({}, td)
        finally:
            delattr(sys, "_MEIPASS")
        dst = Path(td + "/user_models") / "models--Qdrant--bge-small-zh-v1.5"
        assert dst.is_dir()
        assert (dst / "snapshots" / "abc" / "model_optimized.onnx").read_bytes() == b"x" * 100
        assert (dst / "refs" / "main").read_text() == "abc"
        assert emb.cache_dir == td + "/user_models"


def test_frozen_existing_skipped():
    """用户目录已有模型：不重复拷贝（幂等）。"""
    with TemporaryDirectory() as td:
        bundled = _fake_bundled_model(Path(td))
        os.environ["HB_MODELS_DIR"] = td + "/user_models"
        existing = Path(td + "/user_models") / "models--Qdrant--bge-small-zh-v1.5"
        existing.mkdir(parents=True)
        marker = existing / "MARKER"
        marker.write_text("keep")
        setattr(sys, "_MEIPASS", td)
        try:
            rag.default_embedder({}, td)
        finally:
            delattr(sys, "_MEIPASS")
        assert marker.read_text() == "keep"  # 未被覆盖


def test_frozen_copy_failure_degrades():
    """拷贝失败（只读源）：不抛异常，default_embedder 正常返回。"""
    with TemporaryDirectory() as td:
        bundled = _fake_bundled_model(Path(td))
        # 目标目录不可写
        os.environ["HB_MODELS_DIR"] = td + "/ro_models"
        ro = Path(td + "/ro_models")
        ro.mkdir(parents=True)
        os.chmod(ro, 0o444)
        setattr(sys, "_MEIPASS", td)
        try:
            emb = rag.default_embedder({}, td)
            assert emb is not None  # 降级：不阻断启动
        finally:
            os.chmod(ro, 0o755)
            delattr(sys, "_MEIPASS")


def _run_plain():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e!r}")
    print("ALL TESTS PASSED" if failed == 0 else f"{failed} FAILED")
    return failed


if __name__ == "__main__":
    sys.exit(1 if _run_plain() else 0)
